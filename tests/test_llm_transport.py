"""Exercise real HTTPX streaming, deadlines, retries and cancellation offline."""
import asyncio
import json
import threading

import httpx
import pytest

from sayink.llm_transport import Cancellation, LLMContextError, completion
from sayink.text_polisher import LLMCancelled, LLMResponseError, LLMTruncatedError


def event(content=None, finish=None):
    delta = {} if content is None else {"content": content}
    return ("data: " + json.dumps({"choices": [{"delta": delta, "finish_reason": finish}]}, ensure_ascii=False)
            + "\n\n").encode()


class ByteStream(httpx.AsyncByteStream):
    def __init__(self, chunks, *, delay=0):
        self.chunks, self.delay, self.closed = chunks, delay, False

    async def __aiter__(self):
        for index, chunk in enumerate(self.chunks):
            if index and self.delay:
                await asyncio.sleep(self.delay)
            yield chunk

    async def aclose(self):
        self.closed = True


def streaming(chunks, **kwargs):
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=ByteStream(chunks, **kwargs))


def run(handler, **kwargs):
    async def request():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await completion(client, httpx, api_url="https://example.test/v1", api_key="key", model_name="m",
                                    messages=[{"role": "user", "content": "meeting"}], max_tokens=4096,
                                    retry_delay_sec=0, **kwargs)
    return asyncio.run(request())


def test_sse_handles_split_utf8_comments_usage_and_done():
    raw = b": keepalive\n\n" + event("中文") + b'data: {"choices":[],"usage":{}}\n\n' + event(finish="stop") + b"data: [DONE]\n\n"
    response = streaming([raw[i:i + 1] for i in range(len(raw))])
    stream = response.stream
    deltas = []
    assert run(lambda request: response, on_delta=deltas.append) == "中文"
    assert "".join(deltas) == "中文" and stream.closed


def test_broken_attempt_is_reset_and_not_joined_to_retry():
    responses = [streaming([event("discard")]), streaming([event("complete"), event(finish="stop")])]
    previews = []
    assert run(lambda request: responses.pop(0), on_reset=lambda: previews.append([]),
               on_delta=lambda delta: previews[-1].append(delta)) == "complete"
    assert previews == [["discard"], ["complete"]]


@pytest.mark.parametrize("finish", ["length", "max_tokens", "content_filter", "tool_calls"])
def test_unsuccessful_finish_is_rejected_even_for_valid_json(finish):
    error = LLMTruncatedError if finish in ("length", "max_tokens") else LLMResponseError
    with pytest.raises(error):
        run(lambda request: streaming([event('{"summary":"ok"}'), event(finish=finish)]))


def test_done_without_finish_is_retried_then_rejected():
    calls = []
    def handler(request):
        calls.append(request)
        return streaming([event("incomplete"), b"data: [DONE]\n\n"])
    with pytest.raises(LLMResponseError, match="提前结束"):
        run(handler)
    assert len(calls) == 2


def test_explicit_stream_rejection_downgrades_once():
    payloads = []
    def handler(request):
        payloads.append(json.loads(request.content))
        if len(payloads) == 1:
            return httpx.Response(400, json={"error": {"message": "stream is not supported"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ordinary"}, "finish_reason": "stop"}]})
    assert run(handler) == "ordinary"
    assert [p["stream"] for p in payloads] == [True, False]


def test_gateway_ignoring_stream_can_return_json():
    assert run(lambda request: httpx.Response(200, json={
        "choices": [{"message": {"content": "plain"}, "finish_reason": "stop"}]
    })) == "plain"


def test_reasoning_model_parameter_compatibility_is_bounded_and_remembered():
    payloads, capabilities = [], {}
    def handler(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        if "max_tokens" in payload:
            return httpx.Response(400, json={"error": {"message": "Use max_completion_tokens instead of max_tokens"}})
        if "temperature" in payload:
            return httpx.Response(400, json={"error": {"message": "temperature is unsupported"}})
        return streaming([event("ok"), event(finish="stop")])
    assert run(handler, capabilities=capabilities) == "ok"
    assert len(payloads) == 3
    assert run(handler, capabilities=capabilities) == "ok" and len(payloads) == 4
    assert payloads[-1]["max_completion_tokens"] == 4096 and "temperature" not in payloads[-1]


@pytest.mark.parametrize("url,model,expected", [
    ("https://api.deepseek.com/v1", "deepseek-flash", True),
    ("https://api.deepseek.com", "deepseek-v4-pro", True),
    ("https://proxy.example/v1", "deepseek-flash", False),
    ("https://api.deepseek.com", "other-model", False),
])
def test_refinement_thinking_toggle_only_targets_supported_official_models(url, model, expected):
    payloads = []
    async def request():
        def handler(request):
            payloads.append(json.loads(request.content))
            return streaming([event("edited"), event(finish="stop")])
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await completion(client, httpx, api_url=url, api_key="", model_name=model,
                                    messages=[], max_tokens=4096, thinking_mode="disabled")
    assert asyncio.run(request()) == "edited"
    assert (payloads[0].get("thinking") == {"type": "disabled"}) == expected


def test_rate_limit_retries_but_billing_error_does_not():
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "0"}, json={"error": {"code": "rate_limit_error"}})
        return streaming([event("ok"), event(finish="stop")])
    assert run(handler) == "ok" and len(calls) == 2
    calls.clear()
    def billing(request):
        calls.append(request)
        return httpx.Response(429, json={"error": {"code": "insufficient_quota"}})
    with pytest.raises(httpx.HTTPStatusError):
        run(billing)
    assert len(calls) == 1


def test_context_error_is_returned_for_adaptive_splitting_without_retry():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(400, json={"error": {"code": "context_length_exceeded"}})
    with pytest.raises(LLMContextError):
        run(handler)
    assert len(calls) == 1


def test_total_deadline_includes_server_requested_retry_delay():
    with pytest.raises(LLMResponseError, match="超时"):
        run(lambda request: httpx.Response(503, headers={"retry-after": "3600"}, json={"error": {}}),
            total_timeout_sec=0.02)


def test_idle_timeout_closes_stream_and_retries_with_fresh_preview():
    streams = []
    def handler(request):
        response = streaming([event("begin"), event(finish="stop")], delay=1)
        streams.append(response.stream)
        return response
    with pytest.raises(LLMResponseError, match="超时"):
        run(handler, idle_timeout_sec=0.01)
    assert len(streams) == 2 and all(stream.closed for stream in streams)


def test_cancel_interrupts_waiting_for_response_headers():
    cancellation = Cancellation()
    started, closed = threading.Event(), threading.Event()
    errors = []
    async def handler(request):
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            closed.set()
    async def work():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await completion(client, httpx, api_url="https://example.test", api_key="", model_name="m",
                             messages=[], max_tokens=100)
    def target():
        try:
            cancellation.run(work())
        except LLMCancelled as exc:
            errors.append(exc)
    thread = threading.Thread(target=target)
    thread.start()
    assert started.wait(2)
    cancellation.cancel()
    thread.join(2)
    assert not thread.is_alive() and closed.is_set() and len(errors) == 1

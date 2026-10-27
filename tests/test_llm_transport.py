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


def reasoning_event():
    return b'data: {"choices":[{"delta":{"reasoning_content":"hidden thought"},"finish_reason":null}]}\n\n'


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


def test_think_blocks_are_removed_before_minutes_parse_the_answer():
    """A draft JSON object inside <think> must not become the saved minutes."""
    from sayink.minutes_render import extract_json

    body = '<think>{"summary":"draft"}</think>{"summary":"final"}'
    text = run(lambda request: streaming([event(body), event(finish="stop")]))
    assert text == '{"summary":"final"}'
    assert extract_json(text) == {"summary": "final"}


def test_a_json_body_also_drops_a_think_block():
    assert run(lambda request: httpx.Response(200, json={
        "choices": [{"message": {"content": "<think>草稿</think>plain"}, "finish_reason": "stop"}]
    })) == "plain"


def test_a_think_block_with_no_answer_is_empty():
    with pytest.raises(LLMResponseError, match="空内容"):
        run(lambda request: streaming([event("<think>only</think>"), event(finish="stop")]))


def test_continuous_reasoning_does_not_trigger_first_content_timeout():
    calls, deltas = [], []

    def handler(request):
        calls.append(request)
        return streaming([reasoning_event()] * 8 + [event("answer"), event(finish="stop")], delay=0.02)

    assert run(handler, timeout_sec=0.06, idle_timeout_sec=0.5, on_delta=deltas.append) == "answer"
    assert len(calls) == 1 and deltas == ["answer"]


@pytest.mark.parametrize("failure,expected", [
    (httpx.ConnectError("private connection detail"), "连接异常"),
    (httpx.ReadTimeout("private timeout detail"), "等待模型响应超时"),
    (httpx.RemoteProtocolError("private protocol detail"), "响应中断"),
    ("broken", "响应中断"),
    (429, "限流"),
    (500, "HTTP 500"),
    (502, "HTTP 502"),
    (503, "HTTP 503"),
    (504, "HTTP 504"),
])
def test_retry_reason_and_status_track_recovered_stream(failure, expected, caplog):
    calls, statuses = [], []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            if isinstance(failure, Exception):
                raise failure
            if failure == "broken":
                return streaming([event("discarded")])
            return httpx.Response(failure, headers={"retry-after": "0"}, json={"error": {}})
        return streaming([reasoning_event(), event("answer"), event(finish="stop")])

    assert run(handler, on_status=statuses.append, on_retry=statuses.append) == "answer"
    retry_at = next(i for i, text in enumerate(statuses) if "重试" in text)
    assert expected in statuses[retry_at]
    assert statuses[retry_at + 1:] == [
        "正在连接模型…", "已连接，等待模型响应…", "模型正在思考…", "正在接收正文…", "正文已接收，正在检查…",
    ]
    assert len(calls) == 2
    assert "cause=" in caplog.text and "private" not in caplog.text and "hidden thought" not in caplog.text


@pytest.mark.parametrize("reasoning", [False, True])
def test_heartbeats_do_not_mask_stalled_model_output(reasoning):
    streams = []

    def handler(request):
        chunks = ([reasoning_event()] if reasoning else []) + [b": keepalive\n\n"] * 100
        response = streaming(chunks, delay=0.01)
        streams.append(response.stream)
        return response

    with pytest.raises(LLMResponseError, match="超时"):
        run(handler, timeout_sec=0.05, idle_timeout_sec=0.05, total_timeout_sec=2)
    assert len(streams) == 2 and all(stream.closed for stream in streams)


def test_active_reasoning_still_obeys_total_deadline():
    streams = []

    def handler(request):
        response = streaming([reasoning_event()] * 100, delay=0.01)
        streams.append(response.stream)
        return response

    with pytest.raises(LLMResponseError, match="超时"):
        run(handler, timeout_sec=1, idle_timeout_sec=1, total_timeout_sec=0.05)
    assert len(streams) == 1 and streams[0].closed


@pytest.mark.parametrize("cancel_at", ["模型正在思考…", "正在接收正文…"])
def test_cancel_interrupts_active_stream_and_closes_it(cancel_at):
    cancellation, streams = Cancellation(), []

    def handler(request):
        response = streaming([reasoning_event(), event("answer")] * 100 + [event(finish="stop")])
        streams.append(response.stream)
        return response

    async def work():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await completion(client, httpx, api_url="https://example.test", api_key="", model_name="m",
                             messages=[], max_tokens=100,
                             on_status=lambda text: cancellation.cancel() if text == cancel_at else None)

    with pytest.raises(LLMCancelled):
        cancellation.run(work())
    assert len(streams) == 1 and streams[0].closed


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
    from sayink.llm_transport import LLMDeadlineError

    with pytest.raises(LLMDeadlineError, match="超时"):
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

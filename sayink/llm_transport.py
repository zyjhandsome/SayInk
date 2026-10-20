"""Cancellable, bounded Chat Completions transport for long-running jobs."""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from sayink.text_polisher import (
    LLMCancelled, LLMResponseError, LLMTruncatedError, _request_headers, chat_completions_url,
    thinking_payload_extra,
)

log = logging.getLogger("SayInk")


class LLMContextError(LLMResponseError):
    """The provider rejected the input budget."""


class LLMBrokenStream(LLMResponseError):
    """The stream ended without a successful model finish event."""


class Cancellation:
    """Cancel a worker's asyncio task safely from the Qt main thread."""

    def __init__(self):
        self.event = threading.Event()
        self._lock = threading.Lock()
        self._loop = self._task = None

    def cancel(self):
        self.event.set()
        with self._lock:
            if self._loop is not None and not self._loop.is_closed():
                self._loop.call_soon_threadsafe(self._task.cancel)

    def run(self, coroutine):
        async def bound():
            with self._lock:
                self._loop, self._task = asyncio.get_running_loop(), asyncio.current_task()
            try:
                if self.event.is_set():
                    coroutine.close()
                    raise LLMCancelled()
                return await coroutine
            except asyncio.CancelledError as exc:
                raise LLMCancelled() from exc
            finally:
                with self._lock:
                    self._loop = self._task = None
        return asyncio.run(bound())


def _error_detail(response):
    try:
        data = response.json()
        error = data.get("error", data) if isinstance(data, dict) else {}
        if isinstance(error, str):
            return error.lower(), ""
        return str(error.get("message", "")).lower(), str(error.get("code", "")).lower()
    except (ValueError, AttributeError):
        return "", ""


def _retry_after(response, fallback):
    value = response.headers.get("retry-after", "")
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            stamp = parsedate_to_datetime(value)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            return max(0.0, (stamp - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return fallback


def _check_choice(choice, *, streamed=False):
    if not isinstance(choice, dict):
        raise LLMResponseError("API 响应格式异常")
    reason = choice.get("finish_reason")
    if reason in ("length", "max_tokens"):
        raise LLMTruncatedError("模型输出被截断")
    if reason not in (None, "stop"):
        raise LLMResponseError("模型未正常完成输出（" + str(reason) + "）")
    message = choice.get("delta" if streamed else "message")
    if not isinstance(message, dict):
        raise LLMResponseError("API 响应格式异常")
    if message.get("refusal"):
        raise LLMResponseError("模型拒绝生成本次内容")
    content = message.get("content")
    if content is not None and not isinstance(content, str):
        raise LLMResponseError("API 返回了不支持的正文格式")
    return content or "", reason


async def _stream_text(response, *, first_timeout, idle_timeout, on_delta, on_status=None):
    pieces, event_lines = [], []
    finished = False
    visible_chars = reasoning_chars = 0
    activity_deadline = time.monotonic() + first_timeout
    phase = None
    lines = response.aiter_lines().__aiter__()

    def consume():
        nonlocal finished, visible_chars, reasoning_chars, activity_deadline, phase
        if not event_lines:
            return
        raw = "\n".join(event_lines)
        event_lines.clear()
        if raw == "[DONE]":
            if not finished:
                raise LLMBrokenStream("流式连接提前结束，结果未保存，请重试")
            return True
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise LLMResponseError("流式响应格式异常") from exc
        if not isinstance(data, dict) or data.get("error"):
            raise LLMResponseError("流式响应返回错误")
        choices = data.get("choices")
        if choices == []:  # Optional usage event.
            return
        if not isinstance(choices, list) or not choices:
            raise LLMResponseError("流式响应缺少正文")
        choice = choices[0]
        reasoning = ""
        if isinstance(choice, dict) and isinstance(choice.get("delta"), dict):
            delta = choice["delta"]
            reasoning = delta.get("reasoning_content")
            if not isinstance(reasoning, str):
                reasoning = ""
            reasoning_chars += len(reasoning)
            if choice.get("finish_reason") in ("length", "max_tokens"):
                log.warning("模型输出被截断 visible_chars=%d reasoning_chars=%d finish_reason=%s",
                            visible_chars, reasoning_chars, choice["finish_reason"])
        content, reason = _check_choice(choice, streamed=True)
        if finished and content:
            raise LLMResponseError("流式响应在结束后仍返回正文")
        if content or reasoning:
            # Hidden reasoning is model activity too. Heartbeats and metadata
            # alone must not keep a stalled generation alive indefinitely.
            activity_deadline = time.monotonic() + idle_timeout
            next_phase = "content" if content else "reasoning"
            if next_phase != phase:
                phase = next_phase
                if on_status:
                    on_status("正在接收正文…" if content else "模型正在思考…")
        if content:
            visible_chars += len(content)
            pieces.append(content)
            if on_delta:
                on_delta(content)
        if reason == "stop":
            finished = True
            return True

    while True:
        try:
            wait = max(0.001, activity_deadline - time.monotonic())
            # Python 3.10's nested wait_for can swallow cancellation when a
            # line arrives at the same time as the outer job deadline.
            read = asyncio.ensure_future(lines.__anext__())
            try:
                done, _ = await asyncio.wait({read}, timeout=wait)
                if not done:
                    raise asyncio.TimeoutError()
                line = read.result()
            finally:
                if not read.done():
                    read.cancel()
                await asyncio.gather(read, return_exceptions=True)
        except StopAsyncIteration:
            consume()
            break
        if not line:
            if consume():
                break
        elif line.startswith("data:"):
            event_lines.append(line[5:].lstrip(" "))
        # SSE comments and event/id fields are intentionally ignored.
    if not finished:
        raise LLMBrokenStream("流式连接提前结束，结果未保存，请重试")
    text = "".join(pieces).strip()
    if not text:
        raise LLMResponseError("API 返回空内容")
    return text


async def completion(client, httpx, *, api_url, api_key, model_name, messages,
                     max_tokens, timeout_sec=120, idle_timeout_sec=45, total_timeout_sec=600,
                     retry_delay_sec=1, on_delta=None, on_retry=None, on_reset=None, on_status=None, capabilities=None,
                     thinking_mode=None):
    """Stream by default; downgrade only on an explicit unsupported-stream error.

    Each attempt has its own preview. A failed attempt can never be joined
    to the next one's output. The deadline includes retries and server delays.
    Status changes replace retry notices as soon as the next attempt starts.
    """
    capabilities = capabilities if capabilities is not None else {}
    payload = dict(model=model_name, messages=messages, stream=capabilities.get("stream", True))
    payload[capabilities.get("token_parameter", "max_tokens")] = max_tokens
    if capabilities.get("temperature", True):
        payload["temperature"] = 0.2
    # These official dual-mode models otherwise default to high-effort
    # thinking, which shares the small output budget with the actual edit.
    payload.update(thinking_payload_extra(api_url, model_name, thinking_mode))
    url, headers = chat_completions_url(api_url), _request_headers(api_key)
    started = time.monotonic()

    async def perform():
        attempts = 0
        downgraded = False
        while True:
            attempts += 1
            if on_status:
                on_status("正在连接模型…")
            if on_reset:
                on_reset()
            response = None
            try:
                async with client.stream("POST", url, headers=headers, json=payload) as response:
                    log.info("纪要请求 model=%s input_chars=%d attempt=%d status=%d request_id=%s",
                             model_name, sum(len(m["content"]) for m in messages), attempts,
                             response.status_code, response.headers.get("x-request-id", "")[:100])
                    if response.status_code >= 400:
                        await response.aread()
                        message, code = _error_detail(response)
                        if code in ("context_length_exceeded", "context_window_exceeded") or any(
                            term in message for term in ("maximum context", "context length", "too many tokens", "上下文长度")
                        ):
                            raise LLMContextError("输入超过模型上下文上限")
                        unsupported = ("stream" in message and any(
                            term in message for term in ("not support", "unsupported", "not allowed", "not implemented", "不支持")
                        ))
                        if payload["stream"] and not downgraded and response.status_code in (400, 422, 501) and unsupported:
                            payload["stream"] = False
                            capabilities["stream"] = False
                            downgraded = True
                            attempts -= 1
                            if on_retry:
                                on_retry("接口不支持流式，正在使用普通请求…")
                            continue
                        if response.status_code == 400 and "max_tokens" in payload and "max_completion_tokens" in message:
                            payload["max_completion_tokens"] = payload.pop("max_tokens")
                            capabilities["token_parameter"] = "max_completion_tokens"
                            attempts -= 1
                            continue
                        if response.status_code == 400 and "temperature" in payload and "temperature" in message and any(
                            term in message for term in ("not support", "unsupported", "only the default", "不支持")
                        ):
                            del payload["temperature"]
                            capabilities["temperature"] = False
                            attempts -= 1
                            continue
                        billing = any(term in code + " " + message for term in (
                            "quota", "credit", "balance", "billing", "spend_limit", "usage_limit", "余额", "额度不足",
                        ))
                        retryable = response.status_code in (500, 502, 503, 504) or (response.status_code == 429 and not billing)
                        if not retryable or attempts >= 2:
                            response.raise_for_status()
                        delay = _retry_after(response, retry_delay_sec * attempts)
                        retry_cause = f"HTTP {response.status_code}"
                        retry_reason = ("模型服务限流" if response.status_code == 429
                                        else f"模型服务暂时异常（{retry_cause}）")
                    else:
                        if on_status:
                            on_status("已连接，等待模型响应…")
                        if "text/event-stream" in response.headers.get("content-type", "").lower():
                            text = await _stream_text(response, first_timeout=timeout_sec,
                                                      idle_timeout=idle_timeout_sec, on_delta=on_delta,
                                                      on_status=on_status)
                        else:
                            # Some compatible gateways ignore stream=True and return JSON.
                            await response.aread()
                            data = response.json()
                            choices = data.get("choices") if isinstance(data, dict) else None
                            if not isinstance(choices, list) or not choices:
                                raise LLMResponseError("API 响应格式异常")
                            text, _ = _check_choice(choices[0])
                            text = text.strip()
                            if not text:
                                raise LLMResponseError("API 返回空内容")
                            if on_status:
                                on_status("正在接收正文…")
                            if on_delta:
                                on_delta(text)
                        if on_status:
                            on_status("正文已接收，正在检查…")
                        log.info("纪要请求完成 model=%s output_chars=%d elapsed=%.2fs finish_reason=stop",
                                 model_name, len(text), time.monotonic() - started)
                        return text
            except (httpx.NetworkError, httpx.RemoteProtocolError, httpx.TimeoutException, asyncio.TimeoutError, LLMBrokenStream) as exc:
                retry_cause = type(exc).__name__
                if isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError)):
                    retry_reason = "等待模型响应超时"
                elif isinstance(exc, (httpx.RemoteProtocolError, LLMBrokenStream)):
                    retry_reason = "模型响应中断"
                else:
                    retry_reason = "模型连接异常"
                if attempts >= 2:
                    log.warning("纪要请求失败 model=%s attempt=%d cause=%s elapsed=%.2fs",
                                model_name, attempts, retry_cause, time.monotonic() - started)
                    raise
                delay = retry_delay_sec * attempts
            log.warning("纪要请求准备重试 model=%s attempt=%d cause=%s delay=%.2fs elapsed=%.2fs",
                        model_name, attempts, retry_cause, delay, time.monotonic() - started)
            if on_retry:
                on_retry(f"{retry_reason}，{delay:g} 秒后重试…")
            await asyncio.sleep(delay)

    try:
        return await asyncio.wait_for(perform(), total_timeout_sec)
    except asyncio.TimeoutError as exc:
        raise LLMResponseError("模型响应超时，已完成的分段可在重试时继续使用") from exc

"""Unified synchronous and asynchronous OpenAI-compatible Chat Completions transport."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from sayink.llm.endpoint import (
    CHAT_RETRY_DELAY_SEC,
    CHAT_RETRY_STATUS,
    STREAM_TOTAL_TIMEOUT,
    chat_completions_url,
    http_client_options,
    http_stream_timeout,
    normalize_model_name,
    redact_url_query_secrets,
    request_headers,
    thinking_payload_extra,
)
from sayink.llm.errors import (
    BILLING_TERMS,
    LLMBrokenStream,
    LLMCancelled,
    LLMContextError,
    LLMDeadlineError,
    LLMResponseError,
    body_error_message,
    chat_message_text,
    check_choice,
    http_status_message,
    response_error_body,
    response_error_detail,
    retry_after_seconds,
    visible_text,
)
from sayink.llm.sse import (
    collect_async_stream_text,
    collect_stream_text,
    stream_showed_activity,
)

log = logging.getLogger("SayInk")

_STREAM_REJECT_TERMS = ("not support", "unsupported", "not allowed", "not implemented", "不支持")
_TEMPERATURE_REJECT_TERMS = ("not support", "unsupported", "only the default", "不支持")
_CONTEXT_LIMIT_CODES = ("context_length_exceeded", "context_window_exceeded")
_CONTEXT_LIMIT_TERMS = ("maximum context", "context length", "too many tokens", "上下文长度")


def rejects_stream(message: str) -> bool:
    text = (message or "").lower()
    return "stream" in text and any(term in text for term in _STREAM_REJECT_TERMS)


def is_context_limit_error(message: str, code: str = "") -> bool:
    low_msg = (message or "").lower()
    low_code = (code or "").lower()
    return low_code in _CONTEXT_LIMIT_CODES or any(term in low_msg for term in _CONTEXT_LIMIT_TERMS)


def adapt_chat_flags(
    message: str,
    token_parameter: str,
    include_temperature: bool,
) -> tuple[str, bool] | None:
    """Return adjusted ``(token_parameter, include_temperature)`` on 400/422 parameter rejection."""
    text = (message or "").lower()
    if token_parameter == "max_tokens" and "max_completion_tokens" in text:
        return "max_completion_tokens", include_temperature
    if (
        include_temperature
        and "temperature" in text
        and any(term in text for term in _TEMPERATURE_REJECT_TERMS)
    ):
        return token_parameter, False
    return None


def build_chat_payload(
    model_name: str,
    messages: list[dict[str, Any]],
    temperature: float,
    max_tokens: int,
    extra_payload: dict[str, Any] | None,
    *,
    stream: bool,
    token_parameter: str,
    include_temperature: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model_name,
        "messages": messages,
        "temperature": temperature,
        token_parameter: max_tokens,
        **(extra_payload or {}),
    }
    other = "max_completion_tokens" if token_parameter == "max_tokens" else "max_tokens"
    payload.pop(other, None)
    if not include_temperature:
        payload.pop("temperature", None)
    if stream:
        payload["stream"] = True
    else:
        payload.pop("stream", None)
    return payload


class Cancellation:
    """Cancel a worker's asyncio task safely from the Qt main thread."""

    def __init__(self) -> None:
        self.event = threading.Event()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[Any] | None = None

    def cancel(self) -> None:
        self.event.set()
        with self._lock:
            if self._loop is not None and not self._loop.is_closed() and self._task is not None:
                self._loop.call_soon_threadsafe(self._task.cancel)

    def run(self, coroutine: Any) -> Any:
        async def bound() -> Any:
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


def _scrub_exception_secrets(exc: BaseException) -> None:
    if not exc.args:
        return
    scrubbed = tuple(redact_url_query_secrets(arg) if isinstance(arg, str) else arg for arg in exc.args)
    if scrubbed != exc.args:
        try:
            exc.args = scrubbed
        except Exception:
            pass


def _raise_redacted_for_status(response: Any) -> None:
    try:
        response.raise_for_status()
    except Exception as exc:
        _scrub_exception_secrets(exc)
        raise


def _handle_sync_post_error(
    response: Any,
    *,
    code: int,
    attempt: int,
    token_parameter: str,
    include_temperature: bool,
    label: str,
    retry_delay_sec: float,
) -> tuple[str, str, bool]:
    if code in CHAT_RETRY_STATUS and attempt == 1:
        log.warning("LLM %s 返回 HTTP %s，%.1f 秒后重试一次", label, code, retry_delay_sec)
        time.sleep(retry_delay_sec)
        return "retry", token_parameter, include_temperature
    err_body = response_error_body(response)
    adapted = adapt_chat_flags(body_error_message(err_body), token_parameter, include_temperature)
    if adapted is not None and adapted != (token_parameter, include_temperature):
        return "adapt", adapted[0], adapted[1]
    raise LLMResponseError(redact_url_query_secrets(http_status_message(code, err_body)))


def chat_completion(
    client: Any,
    httpx_mod: Any,
    *,
    api_url: str,
    api_key: str,
    model_name: str,
    messages: list[dict[str, Any]],
    temperature: float,
    max_tokens: int,
    is_cancelled: Callable[[], bool] | None = None,
    retry_delay_sec: float = CHAT_RETRY_DELAY_SEC,
    label: str = "LLM",
    reject_truncated: bool = False,
    extra_payload: dict[str, Any] | None = None,
    token_parameter: str = "max_tokens",
    include_temperature: bool = True,
) -> str:
    """Synchronous non-streaming OpenAI-compatible chat completion with one retry."""
    cancelled = is_cancelled or (lambda: False)
    norm_model = normalize_model_name(model_name)
    url = chat_completions_url(api_url)
    headers = request_headers(api_key)
    transient = (httpx_mod.NetworkError, httpx_mod.RemoteProtocolError)
    response = None
    attempt = 0
    while attempt < 2:
        attempt += 1
        if cancelled():
            raise LLMCancelled()
        payload = build_chat_payload(
            norm_model,
            messages,
            temperature,
            max_tokens,
            extra_payload,
            stream=False,
            token_parameter=token_parameter,
            include_temperature=include_temperature,
        )
        try:
            response = client.post(url, headers=headers, json=payload)
        except transient as exc:
            _scrub_exception_secrets(exc)
            if cancelled():
                raise LLMCancelled() from exc
            if attempt == 2:
                raise
            log.warning(
                "LLM %s 连接异常，%.1f 秒后重试一次: %s",
                label,
                retry_delay_sec,
                redact_url_query_secrets(str(exc)),
            )
            time.sleep(retry_delay_sec)
            continue
        if cancelled():
            raise LLMCancelled()
        code = response.status_code if isinstance(getattr(response, "status_code", None), int) else None
        if code is not None and code >= 400:
            action, token_parameter, include_temperature = _handle_sync_post_error(
                response,
                code=code,
                attempt=attempt,
                token_parameter=token_parameter,
                include_temperature=include_temperature,
                label=label,
                retry_delay_sec=retry_delay_sec,
            )
            if action == "adapt":
                attempt -= 1
            continue
        break
    if response is None:
        raise LLMResponseError("API 响应格式异常")
    return chat_message_text(response.json(), reject_truncated=reject_truncated)


def _handle_sync_stream_error(
    response: Any,
    *,
    code: int,
    attempt: int,
    token_parameter: str,
    include_temperature: bool,
    cancelled: Callable[[], bool],
    label: str,
    retry_delay_sec: float,
) -> tuple[str, str, bool]:
    """Classify sync stream HTTP >=400; returns ``(action, token_parameter, include_temperature)``."""
    err_body = response_error_body(response)
    message = body_error_message(err_body)
    if code in (400, 422, 501) and rejects_stream(message):
        log.info("LLM %s 接口不支持流式，改用普通请求", label)
        return "downgrade", token_parameter, include_temperature
    adapted = adapt_chat_flags(message, token_parameter, include_temperature)
    if adapted is not None and adapted != (token_parameter, include_temperature):
        return "adapt", adapted[0], adapted[1]
    if attempt == 1 and code in CHAT_RETRY_STATUS and not cancelled():
        log.warning("LLM %s 返回 HTTP %s，%.1f 秒后重试一次", label, code, retry_delay_sec)
        time.sleep(retry_delay_sec)
        return "retry", token_parameter, include_temperature
    if cancelled():
        raise LLMCancelled()
    raise LLMResponseError(redact_url_query_secrets(http_status_message(code, err_body)))


def stream_chat_completion(
    client: Any,
    httpx_mod: Any,
    *,
    api_url: str,
    api_key: str,
    model_name: str,
    messages: list[dict[str, Any]],
    temperature: float,
    max_tokens: int,
    is_cancelled: Callable[[], bool] | None = None,
    retry_delay_sec: float = CHAT_RETRY_DELAY_SEC,
    label: str = "LLM",
    reject_truncated: bool = False,
    extra_payload: dict[str, Any] | None = None,
    total_timeout: float = STREAM_TOTAL_TIMEOUT,
) -> str:
    """Synchronous streaming chat completion with automatic non-stream downgrade and 1 retry."""
    cancelled = is_cancelled or (lambda: False)
    norm_model = normalize_model_name(model_name)
    url = chat_completions_url(api_url)
    headers = request_headers(api_key)
    transient = (httpx_mod.NetworkError, httpx_mod.RemoteProtocolError, LLMBrokenStream)
    token_parameter = "max_tokens"
    include_temperature = True
    downgrade = False
    attempt = 0
    while attempt < 2:
        attempt += 1
        if cancelled():
            raise LLMCancelled()
        payload = build_chat_payload(
            norm_model,
            messages,
            temperature,
            max_tokens,
            extra_payload,
            stream=True,
            token_parameter=token_parameter,
            include_temperature=include_temperature,
        )
        try:
            with client.stream("POST", url, headers=headers, json=payload) as response:
                code = response.status_code
                if isinstance(code, int) and code >= 400:
                    action, token_parameter, include_temperature = _handle_sync_stream_error(
                        response,
                        code=code,
                        attempt=attempt,
                        token_parameter=token_parameter,
                        include_temperature=include_temperature,
                        cancelled=cancelled,
                        label=label,
                        retry_delay_sec=retry_delay_sec,
                    )
                    if action == "downgrade":
                        downgrade = True
                        break
                    if action == "adapt":
                        attempt -= 1
                    continue
                if cancelled():
                    raise LLMCancelled()
                try:
                    return collect_stream_text(
                        response.iter_lines(),
                        is_cancelled=cancelled,
                        total_timeout=total_timeout,
                        reject_truncated=reject_truncated,
                    )
                except TimeoutError as exc:
                    raise httpx_mod.TimeoutException(str(exc)) from exc
        except transient as exc:
            _scrub_exception_secrets(exc)
            if cancelled():
                raise LLMCancelled() from exc
            if attempt == 2:
                raise
            msg = (
                "流式连接提前结束"
                if isinstance(exc, LLMBrokenStream)
                else f"连接异常: {redact_url_query_secrets(str(exc))}"
            )
            log.warning("LLM %s %s，%.1f 秒后重试一次", label, msg, retry_delay_sec)
            time.sleep(retry_delay_sec)
    if downgrade:
        return chat_completion(
            client,
            httpx_mod,
            api_url=api_url,
            api_key=api_key,
            model_name=model_name,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            is_cancelled=cancelled,
            retry_delay_sec=retry_delay_sec,
            label=label,
            reject_truncated=reject_truncated,
            extra_payload=extra_payload,
            token_parameter=token_parameter,
            include_temperature=include_temperature,
        )
    raise LLMResponseError("API 响应格式异常")


def _adapt_async_http_error(
    response: Any,
    payload: dict[str, Any],
    capabilities: dict[str, Any],
    *,
    downgraded: bool,
    on_retry: Callable[[str], None] | None,
) -> tuple[str, bool, str, str]:
    """Inspect an async >=400 response; returns ``(outcome, downgraded, message, code)``."""
    message, code = response_error_detail(response)
    if is_context_limit_error(message, code):
        raise LLMContextError("输入超过模型上下文上限")
    status = response.status_code
    if payload.get("stream") and not downgraded and status in (400, 422, 501) and rejects_stream(message):
        payload["stream"] = False
        capabilities["stream"] = False
        if on_retry is not None:
            on_retry("接口不支持流式，正在使用普通请求…")
        return "adapt", True, message, code
    if status in (400, 422) and "max_tokens" in payload and "max_completion_tokens" in message:
        payload["max_completion_tokens"] = payload.pop("max_tokens")
        capabilities["token_parameter"] = "max_completion_tokens"
        return "adapt", downgraded, message, code
    if (
        status in (400, 422)
        and "temperature" in payload
        and adapt_chat_flags(message, "max_completion_tokens", True) == ("max_completion_tokens", False)
    ):
        del payload["temperature"]
        capabilities["temperature"] = False
        return "adapt", downgraded, message, code
    return "error", downgraded, message, code


async def _read_async_success_body(
    response: Any,
    *,
    timeout_sec: float,
    idle_timeout_sec: float,
    on_delta: Callable[[str], None] | None,
    on_status: Callable[[str], None] | None,
) -> tuple[str, str]:
    """Read either an SSE event-stream or a single JSON fallback body asynchronously."""
    if on_status is not None:
        on_status("已连接，等待模型响应…")
    ctype = response.headers.get("content-type", "").lower()
    if "text/event-stream" in ctype:
        finish_box: list[str | None] = []
        text = await collect_async_stream_text(
            response,
            first_timeout=timeout_sec,
            idle_timeout=idle_timeout_sec,
            on_delta=on_delta,
            on_status=on_status,
            finish_out=finish_box,
        )
        finish_reason = str(finish_box[0]) if (finish_box and finish_box[0]) else "stop"
    else:
        await response.aread()
        data = response.json()
        choices = data.get("choices") if isinstance(data, dict) else None
        if not isinstance(choices, list) or not choices:
            raise LLMResponseError("API 响应格式异常")
        raw_content, reason = check_choice(choices[0], streamed=False, reject_truncated=True)
        finish_reason = str(reason or "stop")
        text = visible_text(raw_content)
        if on_status is not None:
            on_status("正在接收正文…")
        if on_delta is not None:
            on_delta(text)
    if on_status is not None:
        on_status("正文已接收，正在检查…")
    return text, finish_reason


def _classify_async_exception(exc: BaseException, httpx_mod: Any) -> tuple[str, str]:
    cause = type(exc).__name__
    if isinstance(exc, (httpx_mod.TimeoutException, asyncio.TimeoutError)):
        return cause, "等待模型响应超时"
    if isinstance(exc, (httpx_mod.RemoteProtocolError, LLMBrokenStream)):
        return cause, "模型响应中断"
    return cause, "模型连接异常"


async def async_completion(
    client: Any,
    httpx_mod: Any,
    *,
    api_url: str,
    api_key: str,
    model_name: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    timeout_sec: float = 120,
    idle_timeout_sec: float = 45,
    total_timeout_sec: float = 600,
    retry_delay_sec: float = 1,
    on_delta: Callable[[str], None] | None = None,
    on_retry: Callable[[str], None] | None = None,
    on_reset: Callable[[], None] | None = None,
    on_status: Callable[[str], None] | None = None,
    capabilities: dict[str, Any] | None = None,
    thinking_mode: str | None = None,
    action_label: str = "纪要",
) -> str:
    """Asynchronous streaming Chat Completion with adaptive flag downgrade and bounded deadline."""
    caps = capabilities if capabilities is not None else {}
    norm_model = normalize_model_name(model_name)
    payload: dict[str, Any] = {
        "model": norm_model,
        "messages": messages,
        "stream": caps.get("stream", True),
        caps.get("token_parameter", "max_tokens"): max_tokens,
    }
    if caps.get("temperature", True):
        payload["temperature"] = 0.2
    payload.update(thinking_payload_extra(api_url, norm_model, thinking_mode))
    url = chat_completions_url(api_url)
    headers = request_headers(api_key)
    started = time.monotonic()
    input_chars = sum(len(str(m.get("content") or "")) for m in messages)
    transient = (
        httpx_mod.NetworkError,
        httpx_mod.RemoteProtocolError,
        httpx_mod.TimeoutException,
        asyncio.TimeoutError,
        LLMBrokenStream,
    )

    async def perform() -> str:
        attempts = 0
        downgraded = False
        while True:
            attempts += 1
            if on_status is not None:
                on_status("正在连接模型…")
            if on_reset is not None:
                on_reset()
            try:
                async with client.stream("POST", url, headers=headers, json=payload) as response:
                    log.info(
                        "%s请求 model=%s input_chars=%d attempt=%d status=%d request_id=%s",
                        action_label,
                        norm_model,
                        input_chars,
                        attempts,
                        response.status_code,
                        response.headers.get("x-request-id", "")[:100],
                    )
                    if response.status_code >= 400:
                        await response.aread()
                        outcome, downgraded, msg, code = _adapt_async_http_error(
                            response, payload, caps, downgraded=downgraded, on_retry=on_retry
                        )
                        if outcome == "adapt":
                            attempts -= 1
                            continue
                        billing = any(term in f"{code} {msg}" for term in BILLING_TERMS)
                        retryable = response.status_code in (500, 502, 503, 504) or (
                            response.status_code == 429 and not billing
                        )
                        if not retryable or attempts >= 2:
                            _raise_redacted_for_status(response)
                        delay = retry_after_seconds(response, retry_delay_sec * attempts)
                        retry_cause = f"HTTP {response.status_code}"
                        retry_reason = (
                            "模型服务限流"
                            if response.status_code == 429
                            else f"模型服务暂时异常（{retry_cause}）"
                        )
                    else:
                        text, finish_reason = await _read_async_success_body(
                            response,
                            timeout_sec=timeout_sec,
                            idle_timeout_sec=idle_timeout_sec,
                            on_delta=on_delta,
                            on_status=on_status,
                        )
                        log.info(
                            "%s请求完成 model=%s output_chars=%d elapsed=%.2fs finish_reason=%s",
                            action_label,
                            norm_model,
                            len(text),
                            time.monotonic() - started,
                            finish_reason,
                        )
                        return text
            except transient as exc:
                _scrub_exception_secrets(exc)
                retry_cause, retry_reason = _classify_async_exception(exc, httpx_mod)
                if attempts >= 2:
                    log.warning(
                        "%s请求失败 model=%s attempt=%d cause=%s elapsed=%.2fs",
                        action_label,
                        norm_model,
                        attempts,
                        retry_cause,
                        time.monotonic() - started,
                    )
                    raise
                delay = retry_delay_sec * attempts
            log.warning(
                "%s请求准备重试 model=%s attempt=%d cause=%s delay=%.2fs elapsed=%.2fs",
                action_label,
                norm_model,
                attempts,
                retry_cause,
                delay,
                time.monotonic() - started,
            )
            if on_retry is not None:
                on_retry(f"{retry_reason}，{delay:g} 秒后重试…")
            await asyncio.sleep(delay)

    try:
        return await asyncio.wait_for(perform(), total_timeout_sec)
    except asyncio.TimeoutError as exc:
        raise LLMDeadlineError("模型响应超时，已完成的分段可在重试时继续使用") from exc


def _probe_single_attempt(
    client: Any,
    url: str,
    headers: dict[str, str],
    base_payload: dict[str, Any],
    *,
    token_parameter: str,
    downgrade: bool,
) -> tuple[str, str, bool, str]:
    """Run one probe attempt; returns ``(outcome, message, downgrade, token_parameter)``."""
    body = dict(base_payload)
    body[token_parameter] = 16
    if downgrade:
        response = client.post(url, headers=headers, json=body)
        code = response.status_code if isinstance(getattr(response, "status_code", None), int) else None
        if code is not None and code >= 400:
            err_body = response_error_body(response)
            adapted = adapt_chat_flags(body_error_message(err_body), token_parameter, False)
            if adapted is not None and adapted[0] != token_parameter:
                return "retry", "", downgrade, adapted[0]
            return "fail", http_status_message(code, err_body), downgrade, token_parameter
        chat_message_text(response.json())
        return "ok", "连接成功", downgrade, token_parameter

    body["stream"] = True
    with client.stream("POST", url, headers=headers, json=body) as response:
        code = response.status_code
        if isinstance(code, int) and code >= 400:
            err_body = response_error_body(response)
            message = body_error_message(err_body)
            if code in (400, 422, 501) and rejects_stream(message):
                return "retry", "", True, token_parameter
            adapted = adapt_chat_flags(message, token_parameter, False)
            if adapted is not None and adapted[0] != token_parameter:
                return "retry", "", downgrade, adapted[0]
            return "fail", http_status_message(code, err_body), downgrade, token_parameter
        if not stream_showed_activity(response.iter_lines(), total_timeout=15.0):
            return "fail", "API 返回空内容", downgrade, token_parameter
        return "ok", "连接成功", downgrade, token_parameter


def test_endpoint_connection(
    api_url: str,
    api_key: str,
    model_name: str,
    *,
    proxy_mode: str = "system",
    proxy_bypass_suffixes: str = "",
) -> tuple[bool, str]:
    """Probe an OpenAI-compatible endpoint cleanly using the shared transport rules."""
    from sayink.llm.endpoint import api_url_error

    problem = api_url_error(api_url, proxy_bypass_suffixes=proxy_bypass_suffixes)
    if problem:
        return False, problem

    try:
        import httpx

        model = normalize_model_name(model_name)
        headers = request_headers(api_key)
        url = chat_completions_url(api_url)
        base_payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": "Hello"}],
            **thinking_payload_extra(api_url, model, "disabled"),
        }
        token_parameter = "max_tokens"
        downgrade = False
        with httpx.Client(
            timeout=http_stream_timeout(httpx),
            **http_client_options(proxy_mode, api_url, proxy_bypass_suffixes),
        ) as client:
            for _attempt in range(3):
                outcome, msg, downgrade, token_parameter = _probe_single_attempt(
                    client,
                    url,
                    headers,
                    base_payload,
                    token_parameter=token_parameter,
                    downgrade=downgrade,
                )
                if outcome == "ok":
                    return True, msg
                if outcome == "fail":
                    return False, redact_url_query_secrets(msg)
        return False, "连接失败: 未收到响应"
    except (httpx.TimeoutException, TimeoutError):
        return False, "连接超时"
    except httpx.HTTPStatusError as exc:
        resp = getattr(exc, "response", None)
        return False, redact_url_query_secrets(
            http_status_message(
                getattr(resp, "status_code", None),
                response_error_body(resp),
            )
        )
    except LLMResponseError as exc:
        return False, redact_url_query_secrets(str(exc))
    except Exception as exc:  # httpx / ssl / socket errors outside HTTPStatusError
        return False, f"连接失败: {redact_url_query_secrets(str(exc))}"

"""Unified exception hierarchy, HTTP error classification, and response text extraction for LLM calls."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any


class LLMCancelled(Exception):
    """The caller cancelled while a request was in flight."""


class LLMResponseError(RuntimeError):
    """The endpoint answered, but not with a usable chat completion."""


class LLMTruncatedError(LLMResponseError):
    """The provider stopped at its output limit; the text is incomplete."""


class LLMBrokenStream(LLMResponseError):
    """The stream ended without a successful model finish event."""


class LLMContextError(LLMResponseError):
    """The provider rejected the input budget."""


class LLMDeadlineError(LLMResponseError):
    """The whole call, retries included, ran past its total deadline."""


BILLING_TERMS: tuple[str, ...] = (
    "insufficient_quota",
    "quota",
    "credit",
    "balance",
    "billing",
    "arrears",
    "spend_limit",
    "usage_limit",
    "余额",
    "额度",
    "欠费",
)

DONE_REASONS: tuple[str, ...] = ("stop", "eos", "end_turn")

_THINK_RE = re.compile(r"<think\b[^>]*>.*?</think>", re.DOTALL | re.IGNORECASE)
_ORPHAN_THINK_CLOSE_RE = re.compile(r"^.*?</think>", re.DOTALL | re.IGNORECASE)
_UNCLOSED_THINK_OPEN_RE = re.compile(r"<think\b[^>]*>.*$", re.DOTALL | re.IGNORECASE)


def strip_model_thinking(text: str) -> str:
    """Drop provider reasoning blocks (including prefilled orphan ``...</think>``)."""
    cleaned = _THINK_RE.sub("", text or "")
    cleaned = _ORPHAN_THINK_CLOSE_RE.sub("", cleaned)
    cleaned = _UNCLOSED_THINK_OPEN_RE.sub("", cleaned)
    return cleaned.strip()


def visible_text(text: str) -> str:
    """Drop a provider's ``<think>`` block and require non-empty visible content."""
    cleaned = strip_model_thinking(text)
    if not cleaned:
        raise LLMResponseError("API 返回空内容")
    return cleaned


def content_text(content: Any) -> str:
    """Extract plain text from a string or OpenAI multimodal text-part list."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            else:
                raise LLMResponseError("API 响应格式异常")
        return "".join(parts)
    raise LLMResponseError("API 响应格式异常")


def check_choice(choice: Any, *, streamed: bool = False, reject_truncated: bool = True) -> tuple[str, str | None]:
    """Validate one ``choices[0]`` entry and return ``(text_part, finish_reason)``."""
    if not isinstance(choice, dict):
        raise LLMResponseError("API 响应格式异常")
    reason_raw = choice.get("finish_reason")
    reason = str(reason_raw) if reason_raw is not None else None
    if reason in ("length", "max_tokens"):
        if reject_truncated:
            raise LLMTruncatedError("模型输出被截断")
    elif reason not in (None, *DONE_REASONS):
        raise LLMResponseError(f"模型未正常完成输出（{reason}）")
    message = choice.get("delta" if streamed else "message")
    if not isinstance(message, dict):
        raise LLMResponseError("API 响应格式异常")
    if message.get("refusal"):
        raise LLMResponseError("模型拒绝生成本次内容")
    content = message.get("content")
    if content is None:
        return "", reason
    if isinstance(content, str):
        return content, reason
    if isinstance(content, list):
        try:
            return content_text(content), reason
        except LLMResponseError as exc:
            raise LLMResponseError("API 返回了不支持的正文格式") from exc
    raise LLMResponseError("API 返回了不支持的正文格式")


def chat_message_text(data: Any, *, reject_truncated: bool = False) -> str:
    """Message text from a non-streamed chat-completion JSON body."""
    choices = data.get("choices") if isinstance(data, dict) else None
    if not choices or not isinstance(choices, list) or not isinstance(choices[0], dict):
        raise LLMResponseError("API 响应格式异常")
    choice = choices[0]
    reason = choice.get("finish_reason")
    if reason in ("length", "max_tokens"):
        if reject_truncated:
            raise LLMTruncatedError("模型输出被截断")
    elif reason not in (None, *DONE_REASONS):
        raise LLMResponseError(f"模型未正常完成输出（{reason}）")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise LLMResponseError("API 响应格式异常")
    if message.get("refusal"):
        raise LLMResponseError("模型拒绝生成本次内容")
    content = message.get("content")
    text = strip_model_thinking(content_text(content)) if content is not None else ""
    if not text:
        raise LLMResponseError("API 返回空内容")
    return text


def body_error_message(body: Any) -> str:
    """Extract primary error message string from a decoded JSON error body or text."""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and isinstance(err.get("message"), str):
            return err["message"]
        if isinstance(err, str):
            return err
    if isinstance(body, str):
        return body
    return ""


def extract_error_detail(body: Any) -> str:
    """Extract combined message/code/type string from a decoded error payload."""
    if isinstance(body, str):
        return body
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            parts = [
                err[key]
                for key in ("message", "code", "type")
                if isinstance(err.get(key), str) and err[key]
            ]
            if parts:
                return " ".join(parts)
        if isinstance(err, str):
            return err
        for key in ("message", "detail"):
            msg = body.get(key)
            if isinstance(msg, str) and msg:
                return msg
    return ""


def is_quota_or_balance_error(body: Any) -> bool:
    """True when the error body indicates account quota, balance, or billing exhaustion."""
    detail = extract_error_detail(body)
    if not detail:
        return False
    lowered = detail.lower()
    return any(term in lowered for term in BILLING_TERMS)


def response_error_body(response: Any) -> object:
    """Best-effort decode of an HTTP error response body without raising."""
    if response is None:
        return ""
    raw_bytes = b""
    read_fn = getattr(response, "read", None)
    if callable(read_fn):
        try:
            read_out = read_fn()
            if isinstance(read_out, (bytes, bytearray)):
                raw_bytes = bytes(read_out)
        except (OSError, RuntimeError, ValueError, TypeError):
            pass
    json_fn = getattr(response, "json", None)
    if callable(json_fn):
        try:
            return json_fn()
        except (ValueError, RuntimeError, TypeError):
            pass
    text = getattr(response, "text", None)
    if isinstance(text, str) and text:
        try:
            return json.loads(text)
        except ValueError:
            return text
    if raw_bytes:
        decoded = raw_bytes.decode("utf-8", errors="ignore")
        try:
            return json.loads(decoded)
        except ValueError:
            return decoded
    return ""


def response_error_detail(response: Any) -> tuple[str, str]:
    """Return ``(lowercased_message, lowercased_code)`` from a response's JSON body."""
    try:
        data = response.json()
    except (ValueError, AttributeError, RuntimeError, TypeError):
        return "", ""
    if not isinstance(data, dict):
        return "", ""
    error = data.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or "").lower(), str(error.get("code") or "").lower()
    if isinstance(error, str):
        return error.lower(), ""
    msg = data.get("message") or data.get("detail") or ""
    code = data.get("code") or ""
    return (
        msg.lower() if isinstance(msg, str) else "",
        str(code).lower() if code is not None else "",
    )


def is_billing_error(response: Any) -> bool:
    """Quota / balance problems on an HTTP response: waiting will not fix them."""
    message, code = response_error_detail(response)
    combined = f"{code} {message}"
    return any(term in combined for term in BILLING_TERMS)


def http_status_message(code: int | None, body: object = "") -> str:
    """User-facing Chinese error message for an HTTP status code."""
    if code in (401, 403):
        return f"密钥被拒绝（HTTP {code}）。请检查 API 密钥。"
    if code == 404:
        return "接口路径不存在（HTTP 404）。请检查接口地址是否为 OpenAI 兼容的基址。"
    if code == 400:
        return "请求被拒绝（HTTP 400）。请检查模型名称是否与该接口匹配。"
    if code == 429:
        if is_quota_or_balance_error(body):
            return "API 额度或余额不足（HTTP 429），请检查账户余额或账单设置。"
        return "请求过于频繁（HTTP 429），请稍后重试。"
    if code is None:
        return "HTTP 错误"
    return f"HTTP 错误: {code}"


def retry_after_seconds(response: Any, fallback: float) -> float:
    """Parse ``Retry-After`` header (delta-seconds or HTTP-date), falling back cleanly."""
    headers = getattr(response, "headers", None)
    value = headers.get("retry-after", "") if headers is not None else ""
    try:
        return max(0.0, float(value))
    except (ValueError, TypeError):
        try:
            stamp = parsedate_to_datetime(value)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            return max(0.0, (stamp - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return fallback

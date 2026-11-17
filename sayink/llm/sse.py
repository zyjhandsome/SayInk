"""Unified Server-Sent Events (SSE) parser and stream state machine for sync and async LLM calls."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from sayink.llm.errors import (
    DONE_REASONS,
    LLMBrokenStream,
    LLMCancelled,
    LLMResponseError,
    LLMTruncatedError,
    body_error_message,
    content_text,
    visible_text,
)

log = logging.getLogger("SayInk")


def parse_sse_json(raw: str) -> Any:
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise LLMResponseError("流式响应格式异常") from exc


def extract_choice_delta(choice: Any) -> tuple[str, str, str | None]:
    """Extract ``(content, reasoning, finish_reason)`` from one streamed or non-streamed choice."""
    if not isinstance(choice, dict):
        raise LLMResponseError("API 响应格式异常")
    reason_raw = choice.get("finish_reason")
    reason = str(reason_raw) if reason_raw is not None else None
    delta = choice.get("delta")
    if not isinstance(delta, dict):
        message = choice.get("message")
        if not isinstance(message, dict):
            raise LLMResponseError("API 响应格式异常")
        delta = message
        if reason is None:
            reason = "stop"
    if delta.get("refusal"):
        raise LLMResponseError("模型拒绝生成本次内容")
    raw_content = delta.get("content")
    if raw_content is None:
        content = ""
    elif isinstance(raw_content, str):
        content = raw_content
    elif isinstance(raw_content, list):
        try:
            content = content_text(raw_content)
        except LLMResponseError as exc:
            raise LLMResponseError("API 返回了不支持的正文格式") from exc
    else:
        raise LLMResponseError("API 返回了不支持的正文格式")

    raw_reasoning = delta.get("reasoning_content") or ""
    if isinstance(raw_reasoning, list):
        reasoning = content_text(raw_reasoning)
    elif isinstance(raw_reasoning, str):
        reasoning = raw_reasoning
    else:
        reasoning = ""
    return content, reasoning, reason


def iter_sse_payloads(lines: Iterable[str | None], *, include_done: bool = False) -> Iterator[Any]:
    """Yield decoded JSON payloads (and optionally ``'[DONE]'``) from a synchronous line stream."""
    event: list[str] = []
    raw_lines: list[str] = []
    saw_data = False

    def flush() -> Any:
        nonlocal saw_data
        if not event:
            return None
        raw = "\n".join(event)
        event.clear()
        saw_data = True
        return "[DONE]" if raw == "[DONE]" else parse_sse_json(raw)

    for line in lines:
        if line is None:
            continue
        raw_lines.append(line)
        if line == "":
            item = flush()
            if item == "[DONE]":
                if include_done:
                    yield "[DONE]"
                return
            if item is not None:
                yield item
        elif line.startswith("data:"):
            event.append(line[5:].lstrip(" "))

    item = flush()
    if item == "[DONE]":
        if include_done:
            yield "[DONE]"
        return
    if item is not None:
        yield item
        return
    if not saw_data:
        blob = "\n".join(ln for ln in raw_lines if ln).strip()
        if blob.startswith(("{", "[")):
            yield parse_sse_json(blob)


_OPTIONAL_EMPTY_CHOICES_KEYS = ("usage", "prompt_filter_results", "system_fingerprint", "id", "model")


def parse_sse_payload_piece(data: Any) -> tuple[str, str, str | None] | None:
    """Validate one SSE JSON object and return ``(content, reasoning, reason)`` or ``None`` for usage events."""
    if not isinstance(data, dict) or data.get("error"):
        detail = body_error_message(data).strip() if isinstance(data, dict) else ""
        raise LLMResponseError(f"流式响应返回错误：{detail[:200]}" if detail else "流式响应返回错误")
    choices = data.get("choices")
    if choices == [] and any(key in data for key in _OPTIONAL_EMPTY_CHOICES_KEYS):
        return None
    if not isinstance(choices, list) or not choices:
        raise LLMResponseError("API 响应格式异常")
    return extract_choice_delta(choices[0])


def iter_stream_pieces(
    lines: Iterable[str | None],
    *,
    include_done: bool = False,
) -> Iterator[tuple[str, str, str | None]]:
    """Yield ``(content, reasoning, finish_reason)`` tuples from an SSE line iterator."""
    for data in iter_sse_payloads(lines, include_done=include_done):
        if data == "[DONE]":
            yield "", "", "[DONE]"
            return
        piece = parse_sse_payload_piece(data)
        if piece is not None:
            yield piece


def stream_showed_activity(lines: Iterable[str | None]) -> bool:
    """True once any non-blank visible token or reasoning token arrives."""
    for content, reasoning, _reason in iter_stream_pieces(lines):
        if content.strip() or reasoning.strip():
            return True
    return False


@dataclass
class SSEStreamAccumulator:
    """Shared stateful accumulator enforcing finish, truncation, and post-finish invariants."""

    reject_truncated: bool = True
    on_delta: Callable[[str], None] | None = None
    on_status: Callable[[str], None] | None = None
    pieces: list[str] = field(default_factory=list)
    finished: bool = False
    last_reason: str | None = None
    visible_chars: int = 0
    reasoning_chars: int = 0
    phase: str | None = None

    def on_done_marker(self, *, strict_empty: bool) -> bool:
        if strict_empty and not self.finished and not self.pieces and not self.reasoning_chars:
            raise LLMBrokenStream("流式连接提前结束，结果未保存，请重试")
        if self.pieces or self.reasoning_chars or self.finished:
            if self.last_reason is None:
                self.last_reason = "done"
            self.finished = True
        return True

    def _check_reason(self, reason: str | None) -> None:
        if reason in ("length", "max_tokens"):
            log.warning(
                "模型输出被截断 visible_chars=%d reasoning_chars=%d finish_reason=%s",
                self.visible_chars,
                self.reasoning_chars,
                reason,
            )
            if self.reject_truncated:
                raise LLMTruncatedError("模型输出被截断")
        elif reason is not None and reason not in DONE_REASONS:
            raise LLMResponseError(f"模型未正常完成输出（{reason}）")

    def _emit_activity(self, content: str, reasoning: str) -> bool:
        if not (content or reasoning):
            return False
        next_phase = "content" if content else "reasoning"
        if next_phase != self.phase:
            self.phase = next_phase
            if self.on_status is not None:
                self.on_status("正在接收正文…" if content else "模型正在思考…")
        if content:
            self.visible_chars += len(content)
            self.pieces.append(content)
            if self.on_delta is not None:
                self.on_delta(content)
        return True

    def feed_piece(self, content: str, reasoning: str, reason: str | None) -> tuple[bool, bool]:
        """Process one piece; returns ``(had_activity, stream_complete)``."""
        self.reasoning_chars += len(reasoning)
        self._check_reason(reason)
        if self.finished and content:
            raise LLMResponseError("流式响应在结束后仍返回正文")
        had_activity = self._emit_activity(content, reasoning)
        if reason is not None:
            self.last_reason = str(reason)
        if reason in (*DONE_REASONS, "length", "max_tokens"):
            self.finished = True
            return had_activity, True
        return had_activity, False

    def finalize_text(self) -> str:
        if not self.finished:
            raise LLMBrokenStream("流式连接提前结束，结果未保存，请重试")
        return visible_text("".join(self.pieces))


def collect_stream_text(
    lines: Iterable[str | None],
    *,
    is_cancelled: Callable[[], bool],
    total_timeout: float,
    reject_truncated: bool,
) -> str:
    """Synchronously collect verified text from an SSE line stream."""
    acc = SSEStreamAccumulator(reject_truncated=reject_truncated)
    deadline = time.monotonic() + total_timeout
    for content, reasoning, reason in iter_stream_pieces(lines, include_done=True):
        if is_cancelled():
            raise LLMCancelled()
        if time.monotonic() > deadline:
            raise TimeoutError("总时长超时")
        if reason == "[DONE]":
            acc.on_done_marker(strict_empty=False)
            break
        acc.feed_piece(content, reasoning, reason)
    return acc.finalize_text()


async def _next_async_line(aiter: Any, timeout: float) -> str:
    """Await one line with Python 3.10-safe task cancellation on timeout."""
    read = asyncio.ensure_future(aiter.__anext__())
    try:
        done, _ = await asyncio.wait({read}, timeout=max(0.001, timeout))
        if not done:
            raise asyncio.TimeoutError()
        return read.result()
    finally:
        if not read.done():
            read.cancel()
        await asyncio.gather(read, return_exceptions=True)


async def collect_async_stream_text(
    response: Any,
    *,
    first_timeout: float,
    idle_timeout: float,
    on_delta: Callable[[str], None] | None,
    on_status: Callable[[str], None] | None = None,
    finish_out: list[str | None] | None = None,
) -> str:
    """Asynchronously collect verified text from an ``httpx.Response`` SSE stream."""
    acc = SSEStreamAccumulator(reject_truncated=True, on_delta=on_delta, on_status=on_status)
    event_lines: list[str] = []
    activity_deadline = time.monotonic() + first_timeout
    lines = response.aiter_lines().__aiter__()

    def consume_event() -> bool:
        nonlocal activity_deadline
        if not event_lines:
            return False
        raw = "\n".join(event_lines)
        event_lines.clear()
        if raw == "[DONE]":
            return acc.on_done_marker(strict_empty=True)
        piece = parse_sse_payload_piece(parse_sse_json(raw))
        if piece is None:
            return False
        had_activity, done_now = acc.feed_piece(*piece)
        if had_activity:
            activity_deadline = time.monotonic() + idle_timeout
        return done_now

    while True:
        try:
            line = await _next_async_line(lines, activity_deadline - time.monotonic())
        except StopAsyncIteration:
            consume_event()
            break
        if not line:
            if consume_event():
                break
        elif line.startswith("data:"):
            event_lines.append(line[5:].lstrip(" "))

    text = acc.finalize_text()
    if finish_out is not None:
        finish_out.append(acc.last_reason)
    return text

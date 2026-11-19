"""Cancellable, bounded Chat Completions transport facade backed by ``sayink.llm``."""

from __future__ import annotations

from sayink.llm import (
    Cancellation,
    LLMBrokenStream,
    LLMCancelled,
    LLMContextError,
    LLMDeadlineError,
    LLMResponseError,
    LLMTruncatedError,
    async_completion as completion,
    chat_completion,
    chat_completions_url,
    collect_async_stream_text as _stream_text,
    is_billing_error,
    normalize_api_key,
    normalize_model_name,
    stream_chat_completion,
    strip_model_thinking,
    thinking_payload_extra,
)

__all__ = [
    "Cancellation",
    "LLMBrokenStream",
    "LLMCancelled",
    "LLMContextError",
    "LLMDeadlineError",
    "LLMResponseError",
    "LLMTruncatedError",
    "_stream_text",
    "chat_completion",
    "chat_completions_url",
    "completion",
    "is_billing_error",
    "normalize_api_key",
    "normalize_model_name",
    "stream_chat_completion",
    "strip_model_thinking",
    "thinking_payload_extra",
]

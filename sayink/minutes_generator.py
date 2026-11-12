"""Stream, validate and checkpoint bounded meeting-summary requests.

Long meetings use bounded hierarchical merges. Completed, validated steps
can be resumed; partial streams never become saved meeting documents.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Sequence

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from sayink.minutes_prompts import (
    DEFAULT_CHUNK_CHARS,
    SECTION_ACTION_ITEMS,
    SECTION_CHAPTERS,
    SECTION_DECISIONS,
    SECTION_OPEN_QUESTIONS,
    SECTION_SPEAKERS,
    SECTION_SUMMARY,
    SECTION_TITLES,
    SECTION_TOPICS,
    MeetingContext,
    MinutesTemplate,
    TranscriptLine,
    build_markdown_system_prompt,
    build_merge_prompt,
    build_merge_system_prompt,
    build_system_prompt,
    build_user_prompt,
    chunk_transcript_with_spans,
    parse_timestamp,
    render_transcript,
    section_title,
)
from sayink.minutes_render import (
    apply_speaker_names, clear_timeline, extract_json, render_markdown, strip_clock_citations,
)
from sayink.text_polisher import (
    LLMCancelled, LLMResponseError, LLMTruncatedError, api_url_error, strip_model_thinking, http_client_options,
)
from sayink.llm_transport import (
    Cancellation, LLMBrokenStream, LLMContextError, LLMDeadlineError, completion, is_billing_error,
)
from sayink.minutes_validation import validate_markdown, validate_minutes
from sayink.transcript_refiner import (
    REFINE_SYSTEM_PROMPT,
    RefinementValidationError,
    refinement_chunks,
    refinement_user_prompt,
    render_refinement,
    split_refinement_units,
    validate_refinement,
)

log = logging.getLogger("SayInk")

REQUEST_TIMEOUT_SEC = 120.0
MAX_OUTPUT_TOKENS = 4096
# Minutes requests are long; give a gateway a full second before the one retry.
_RETRY_DELAY_SEC = 1.0


@dataclass
class MinutesRequest:
    api_url: str
    api_key: str
    model_name: str
    template: MinutesTemplate
    context: MeetingContext
    lines: Sequence[TranscriptLine]
    speaker_names: dict[int, str] = field(default_factory=dict)
    duration_ms: int = 0
    title_fallback: str = "会议纪要"
    output_language: str = "auto"
    chunk_chars: int = DEFAULT_CHUNK_CHARS
    timeout_sec: float = REQUEST_TIMEOUT_SEC
    meta_lines: tuple[str, ...] = ()
    context_window_tokens: int = 32768
    max_output_tokens: int = MAX_OUTPUT_TOKENS
    idle_timeout_sec: float = 45.0
    total_timeout_sec: float = 600.0
    checkpoints: dict[str, str] = field(default_factory=dict)
    save_checkpoint: Callable[[str, str], None] | None = None
    proxy_mode: str = "system"
    proxy_bypass_suffixes: str = ""
    speakers_distinguished: bool = True
    timestamps_available: bool = True
    label_unknown: bool = True


def request_fingerprint(request: MinutesRequest) -> str:
    """Invalidate checkpoints whenever any generation input or contract changes."""
    data = {
        "version": 4, "api_url": request.api_url.rstrip("/"),
        "credential": hashlib.sha256(request.api_key.encode()).hexdigest(),
        "model": request.model_name, "template": asdict(request.template),
        "context": asdict(request.context), "lines": [asdict(line) for line in request.lines],
        "names": request.speaker_names, "language": request.output_language,
        "chunk_chars": request.chunk_chars, "context_tokens": request.context_window_tokens,
        "output_tokens": request.max_output_tokens,
        "prompts": [
            build_system_prompt(
                request.template, output_language=request.output_language,
                speakers_distinguished=request.speakers_distinguished,
                timestamps_available=request.timestamps_available,
            ),
            build_merge_system_prompt(
                request.template, output_language=request.output_language,
                speakers_distinguished=request.speakers_distinguished,
                timestamps_available=request.timestamps_available,
            ),
            REFINE_SYSTEM_PROMPT,
        ],
    }
    if (
        not request.speakers_distinguished
        or not request.timestamps_available
        or (request.speakers_distinguished and not request.label_unknown)
    ):
        shape = [request.speakers_distinguished, request.timestamps_available]
        if request.speakers_distinguished and not request.label_unknown:
            shape.append(False)
        data["transcript_shape"] = shape
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def is_transient_failure(exc: BaseException) -> bool:
    """Network, timeout, overload and rate-limit failures: the same request
    may succeed later. Credentials, quota, budget and validation do not."""
    import httpx

    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError,
                        LLMBrokenStream, LLMDeadlineError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 429:
            return not is_billing_error(exc.response)
        return status in (408, 500, 502, 503, 504)
    return False


_PREVIEW_FIELD_RE = re.compile(r'"(?:summary|text|task)"\s*:\s*"((?:\\.|[^"\\])*)(?:"|$)')


def _estimate_text_tokens(text: str) -> float:
    if not text:
        return 0.0
    chars = len(text)
    utf8_bytes = len(text.encode("utf-8"))
    # ASCII (1B) ≈ 1 / 3.5 tok/char; 3-byte BMP/CJK ≈ 1.2 tok/char;
    # 2-byte / 4-byte UTF-8 interpolate smoothly without per-character loops.
    non_ascii_units = (utf8_bytes - chars) / 2.0
    ascii_equiv = max(0.0, chars - non_ascii_units)
    return non_ascii_units * 1.2 + ascii_equiv / 3.5


def estimate_prompt_tokens(system: str, user: str) -> int:
    # CJK-aware, tokenizer-independent upper bound plus message framing overhead.
    return int(math.ceil(_estimate_text_tokens(system) + _estimate_text_tokens(user))) + 64


@dataclass(frozen=True)
class MinutesResult:
    json_payload: str
    markdown: str
    chunked: bool
    duration_ms: int
    model: str
    template_id: str


class MinutesWorker(QThread):
    progress = pyqtSignal(str)
    preview = pyqtSignal(str)
    finished_ok = pyqtSignal(object)  # MinutesResult
    error = pyqtSignal(str)

    def __init__(self, request: MinutesRequest):
        super().__init__()
        self._req = request
        self._cancelled = False
        self._cancellation = Cancellation()
        self._preview_raw = ""
        self._preview_scan_pos = 0
        self._preview_anchor = ""
        self._preview_completed: list[str] = []
        self._preview_emitted = ""
        self._last_preview_at = 0.0
        self._stage = ""
        self._split_occurred = False
        self._preview_markdown = False
        self._chunk_spans: list[tuple[int, int]] = []
        self._capabilities = {}
        # Set before ``error`` is emitted: whether retrying later can help.
        self.transient_failure = False

    def cancel(self) -> None:
        self._cancelled = True
        self._cancellation.cancel()

    # -- HTTP ------------------------------------------------------------

    def _fits(self, system_prompt: str, user_prompt: str) -> bool:
        return estimate_prompt_tokens(system_prompt, user_prompt) <= (
            self._req.context_window_tokens - self._req.max_output_tokens - 1024
        )

    @staticmethod
    def _step_key(system_prompt, user_prompt):
        return hashlib.sha256((system_prompt + "\0" + user_prompt).encode()).hexdigest()

    def _remember(self, system_prompt, user_prompt, raw):
        if self._cancelled:
            raise LLMCancelled()
        key = self._step_key(system_prompt, user_prompt)
        if self._req.save_checkpoint and key not in self._req.checkpoints:
            try:
                self._req.save_checkpoint(key, raw)
            except Exception as exc:
                # The answer is valid; only its resume point is lost. Keep it
                # in memory and let the run finish instead of failing it.
                log.warning("分段进度未能保存，本次结果仅保留在内存中: %s", exc)
        self._req.checkpoints[key] = raw

    def _reset_preview(self):
        self._preview_raw = ""
        self._preview_scan_pos = 0
        self._preview_anchor = ""
        self._preview_completed = []
        self._preview_emitted = ""
        self._last_preview_at = 0
        self.preview.emit("")

    def _on_delta(self, delta):
        self._preview_raw += delta
        raw_len = len(self._preview_raw)
        if raw_len > self._req.max_output_tokens * 32:
            raise LLMResponseError("模型输出异常过长，本次结果未保存")
        interval = 0.15 if raw_len < 4096 else (0.30 if raw_len < 16384 else 0.50)
        if time.monotonic() - self._last_preview_at >= interval:
            self._publish_preview()

    def _publish_preview(self):
        raw = strip_model_thinking(self._preview_raw).lstrip()
        pos = self._preview_scan_pos
        if pos > len(raw) or (pos and raw[max(0, pos - 16):pos] != self._preview_anchor):
            pos = 0
            self._preview_completed = []
        tail_readable: list[str] = []
        for match in _PREVIEW_FIELD_RE.finditer(raw, pos):
            value = match.group(1)
            try:
                decoded = json.loads('"' + value + '"')
            except ValueError:
                continue  # A split escape sequence is completed in the next event.
            if match.group(0).endswith('"'):
                self._preview_completed.append(decoded)
                pos = match.end()
            else:
                tail_readable.append(decoded)
        self._preview_scan_pos = pos
        self._preview_anchor = raw[max(0, pos - 16):pos] if pos else ""
        readable = self._preview_completed + tail_readable
        if not readable and self._preview_markdown and not raw.startswith(("{", "[", "```")):
            readable = [raw]
        if readable:
            text = "生成预览 · 尚未保存\n\n" + self._stage + "\n\n" + "\n\n".join(readable)[-24000:]
            if text != self._preview_emitted:
                self._preview_emitted = text
                self.preview.emit(text)
        self._last_preview_at = time.monotonic()

    async def _chat(self, client, httpx, system_prompt: str, user_prompt: str) -> str:
        if self._cancelled:
            raise LLMCancelled()
        if not self._fits(system_prompt, user_prompt):
            raise LLMContextError("提示词与笔记超过当前输入预算，请缩短笔记或调整模型上下文设置")
        key = self._step_key(system_prompt, user_prompt)
        if key in self._req.checkpoints:
            self.progress.emit(self._stage + " · 复用已完成结果")
            self._reset_preview()
            self._on_delta(self._req.checkpoints[key])
            return self._req.checkpoints[key]
        raw = await completion(
            client, httpx,
            api_url=self._req.api_url,
            api_key=self._req.api_key,
            model_name=self._req.model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=self._req.max_output_tokens,
            timeout_sec=self._req.timeout_sec,
            idle_timeout_sec=self._req.idle_timeout_sec,
            total_timeout_sec=self._req.total_timeout_sec,
            retry_delay_sec=_RETRY_DELAY_SEC,
            on_delta=self._on_delta,
            on_reset=self._reset_preview,
            on_retry=lambda text: self.progress.emit(self._stage + " · " + text),
            on_status=lambda text: self.progress.emit(self._stage + " · " + text),
            capabilities=self._capabilities,
            thinking_mode="disabled" if self._req.template.is_refinement else None,
        )
        self._publish_preview()
        return raw

    def _refine_prompt(self, chunks, index, *, retry_reason: str = ""):
        return refinement_user_prompt(
            chunks, index, self._req.speaker_names, self._req.context.glossary_terms,
            retry_reason=retry_reason, anonymous_speakers=not self._req.speakers_distinguished,
        )

    async def _refine(self, client, httpx) -> tuple[dict, bool]:
        req = self._req
        size = min(req.chunk_chars, max(200, req.max_output_tokens * 2 // 3))
        while True:
            chunks = refinement_chunks(req.lines, max_chars=size)
            if all(self._fits(REFINE_SYSTEM_PROMPT, self._refine_prompt(chunks, i)) for i in range(len(chunks))):
                break
            if size <= 200:
                raise LLMContextError("原文精整提示词或术语表超过输入预算，请调整模型上下文设置")
            size = max(200, size // 2)
        paragraphs = []
        for index in range(len(chunks)):
            paragraphs.extend(await self._refine_group(client, httpx, chunks, index,
                              f"第 {index + 1} 段", f"第 {index + 1}/{len(chunks)} 段"))
        payload = {"kind": "refine", "paragraphs": paragraphs}
        if not render_refinement(payload).strip():
            raise RefinementValidationError("文字稿中没有可整理的有效内容，本次结果未保存")
        return payload, len(chunks) > 1 or self._split_occurred

    async def _refine_group(self, client, httpx, chunks, index, label, progress_label, depth=0):
        req, units = self._req, chunks[index]
        base_prompt = self._refine_prompt(chunks, index)
        split_prompt = "refinement-split-v1\n" + base_prompt
        split_key = self._step_key(REFINE_SYSTEM_PROMPT, split_prompt)
        # A failed parent should not be sent again when resuming its children.
        if (self._step_key(REFINE_SYSTEM_PROMPT, base_prompt) not in req.checkpoints
                and req.checkpoints.get(split_key) == '{"split":true}'):
            return await self._refine_split(client, httpx, chunks, index, label, progress_label, depth, split_prompt)
        reason = ""
        for attempt in range(2):
            if self._cancelled:
                raise LLMCancelled()
            self._stage = "正在整理原文… " + progress_label
            self.progress.emit(self._stage + (" · 正在重新核对" if attempt else ""))
            try:
                prompt = self._refine_prompt(chunks, index, retry_reason=reason)
                raw = await self._chat(client, httpx, REFINE_SYSTEM_PROMPT, prompt)
                paragraphs = validate_refinement(extract_json(raw), units)
                self._remember(REFINE_SYSTEM_PROMPT, base_prompt, raw)
                return paragraphs
            except (LLMTruncatedError, LLMContextError) as exc:
                if isinstance(exc, LLMTruncatedError) and split_refinement_units(units) is None and not attempt:
                    reason = str(exc)
                    continue
                return await self._refine_split(client, httpx, chunks, index, label, progress_label,
                                                depth, split_prompt, cause=exc)
            except RefinementValidationError as exc:
                reason = exc.retry_hint
                if attempt:
                    if depth < 8 and split_refinement_units(units) is not None:
                        return await self._refine_split(client, httpx, chunks, index, label, progress_label,
                                                       depth, split_prompt, cause=exc)
                    raise RefinementValidationError(
                        f"{label}未通过完整性检查（{reason}）。本次结果未保存，请重试或更换模型"
                    ) from exc

    async def _refine_split(self, client, httpx, chunks, index, label, progress_label, depth, split_prompt, cause=None):
        units = chunks[index]
        parts = split_refinement_units(units) if depth < 8 else None
        if parts is None:
            raise RefinementValidationError(
                f"{label}已缩小至最小处理片段，模型仍未完整输出。本次结果未保存；请增加输出预算或换用支持更长输出的模型"
            ) from cause
        self._split_occurred = True
        detail = "未通过核对" if isinstance(cause, RefinementValidationError) else "输出空间不足"
        self.progress.emit("正在整理原文… " + progress_label + f" · {detail}，自动拆小继续…")
        self._remember(REFINE_SYSTEM_PROMPT, split_prompt, '{"split":true}')
        children = list(chunks[:index]) + list(parts) + list(chunks[index + 1:])
        paragraphs = []
        for child in range(2):
            paragraphs.extend(await self._refine_group(
                client, httpx, children, index + child, label, progress_label + f" · 子段 {child + 1}/2", depth + 1,
            ))
        if len(units) == 1:
            # Temporary child ids never leak into the saved source coverage.
            paragraphs = [{"source_ids": [units[0].source_id],
                           "text": "\n\n".join(p["text"] for p in paragraphs if p["text"])}]
        paragraphs = validate_refinement({"paragraphs": paragraphs}, units)
        base_prompt = self._refine_prompt(chunks, index)
        self._remember(REFINE_SYSTEM_PROMPT, base_prompt, json.dumps({"paragraphs": paragraphs}, ensure_ascii=False))
        return paragraphs

    # -- pipeline ----------------------------------------------------------

    def _chunk_context(self, index: int, total: int, *, spans: list[tuple[int, int]] | None = None) -> MeetingContext:
        if total <= 1:
            return self._req.context
        span_list = spans if spans is not None else self._chunk_spans
        if index < len(span_list):
            start_ms, end_ms = span_list[index]
        else:
            start_ms, end_ms = 0, 10**12
        return _slice_context_for_span(self._req.context, start_ms, end_ms, chunked=True)

    def _chunk_prompt(self, chunks, index, *, spans: list[tuple[int, int]] | None = None):
        context = ""
        if index:
            context += "前文（仅供理解，不重复整理）：\n" + chunks[index - 1][-200:] + "\n"
        if index + 1 < len(chunks):
            context += "后文（仅供理解，不提前整理）：\n" + chunks[index + 1][:200] + "\n"
        chunk_ctx = self._chunk_context(index, len(chunks), spans=spans)
        return context + build_user_prompt(
            chunk_ctx,
            chunks[index],
            chunk_label=f"第 {index + 1}/{len(chunks)} 段" if len(chunks) > 1 else "",
        )

    def _plan_chunks(self, system):
        size = max(64, int(self._req.chunk_chars))
        merge_system: str | None = None
        while True:
            chunk_items = chunk_transcript_with_spans(
                self._req.lines, self._req.speaker_names, max_chars=size,
                show_speakers=self._req.speakers_distinguished, show_times=self._req.timestamps_available,
                label_unknown=self._req.label_unknown,
            )
            chunks = [item[0] for item in chunk_items]
            spans = [(item[1], item[2]) for item in chunk_items]
            if len(chunks) > 1:
                if merge_system is None:
                    merge_system = build_merge_system_prompt(
                        self._req.template,
                        output_language=self._req.output_language,
                        speakers_distinguished=self._req.speakers_distinguished,
                        timestamps_available=self._req.timestamps_available,
                    )
                if not self._fits(merge_system, build_merge_prompt(self._req.context, [{"summary": ""}])):
                    raise LLMContextError("提示词、笔记或术语表过长，请缩短内容或调整模型上下文设置")
            if all(self._fits(system, self._chunk_prompt(chunks, i, spans=spans)) for i in range(len(chunks))):
                self._chunk_spans = spans
                return chunks
            if size <= 64:
                raise LLMContextError("提示词、笔记或术语表过长，请缩短内容或调整模型上下文设置")
            size = max(64, size // 2)

    async def _summarize(
        self,
        client,
        httpx,
        system,
        text,
        prompt,
        depth=0,
        *,
        allow_markdown_fallback: bool = False,
        chunk_context: MeetingContext | None = None,
    ):
        active_ctx = chunk_context if chunk_context is not None else self._req.context
        try:
            raw = await self._chat(client, httpx, system, prompt)
        except (LLMContextError, LLMTruncatedError):
            if len(text) < 128 or depth >= 8:
                raise
            self._split_occurred = True
            self.progress.emit(self._stage + " · 内容较长，正在细分处理…")
            left_text, right_text = _split_chunk(text)
            left_start, left_end, right_start, right_end = _split_text_spans(left_text, right_text)
            left_ctx = _slice_context_for_span(active_ctx, left_start, left_end, chunked=True)
            right_ctx = _slice_context_for_span(active_ctx, right_start, right_end, chunked=True)
            left = await self._summarize(
                client, httpx, system, left_text,
                build_user_prompt(left_ctx, left_text), depth + 1,
                allow_markdown_fallback=True, chunk_context=left_ctx,
            )
            right = await self._summarize(
                client, httpx, system, right_text,
                build_user_prompt(right_ctx, right_text), depth + 1,
                allow_markdown_fallback=True, chunk_context=right_ctx,
            )
            merge_ctx = active_ctx if chunk_context is not None else self._req.context
            payload = await self._merge(client, httpx, [left, right], merge_context=merge_ctx)
            self._remember(system, prompt, json.dumps(payload, ensure_ascii=False))
            return payload
        payload = extract_json(raw)
        if payload is None:
            if not allow_markdown_fallback:
                raise LLMResponseError(self._stage + "返回的不是 JSON")
            payload = await self._markdown_chunk_fallback(client, httpx, raw, text, active_ctx)
            self._remember(system, prompt, json.dumps(payload, ensure_ascii=False))
            return payload
        validate_minutes(payload)
        self._remember(system, prompt, raw)
        return payload

    async def _markdown_chunk_fallback(
        self,
        client,
        httpx,
        initial_raw: str,
        chunk_text: str,
        chunk_ctx: MeetingContext,
    ) -> dict[str, Any]:
        """Recover a structured partial when a model answers a chunk in Markdown."""
        try:
            md_text = validate_markdown(initial_raw)
        except LLMResponseError:
            log.warning("%s未返回 JSON，改用 Markdown 兜底重试", self._stage)
            self.progress.emit(self._stage + " · 改用 Markdown 兜底重试…")
            md_system = build_markdown_system_prompt(
                self._req.template,
                output_language=self._req.output_language,
                speakers_distinguished=self._req.speakers_distinguished,
                timestamps_available=self._req.timestamps_available,
            )
            md_user = build_user_prompt(chunk_ctx, chunk_text)
            md_raw = await self._chat(client, httpx, md_system, md_user)
            try:
                md_text = validate_markdown(md_raw)
            except LLMResponseError as exc:
                raise LLMResponseError(f"{self._stage}返回的不是 JSON") from exc
        return validate_minutes(_markdown_to_partial(md_text, self._req.template))

    async def _merge(self, client, httpx, partials, *, merge_context: MeetingContext | None = None):
        ctx = merge_context if merge_context is not None else self._req.context
        system = build_merge_system_prompt(
            self._req.template, output_language=self._req.output_language,
            speakers_distinguished=self._req.speakers_distinguished,
            timestamps_available=self._req.timestamps_available,
        )
        level = 0
        while len(partials) > 1:
            level += 1
            groups, current = [], []
            for part in partials:
                if current and not self._fits(system, build_merge_prompt(ctx, current + [part])):
                    groups.append(current)
                    current = []
                current.append(part)
            if current:
                groups.append(current)
            if len(groups) == len(partials):
                # Never send an unbounded final merge or discard already valid sections.
                self.progress.emit("各段纪要已完成，正在按顺序保留内容…")
                return _concat_partials(partials)
            merged = []
            for index, group in enumerate(groups):
                if len(group) == 1:
                    merged.append(group[0])
                    continue
                self._stage = f"正在合并各段纪要… 第 {level} 轮 {index + 1}/{len(groups)} 组"
                self.progress.emit(self._stage)
                prompt = build_merge_prompt(ctx, group)
                try:
                    raw = await self._chat(client, httpx, system, prompt)
                    payload = validate_minutes(extract_json(raw))
                    self._remember(system, prompt, raw)
                    merged.append(_preserve_facts(payload, group))
                except (LLMContextError, LLMTruncatedError):
                    if len(group) > 2:
                        mid = len(group) // 2
                        merged.extend([await self._merge(client, httpx, group[:mid], merge_context=ctx),
                                       await self._merge(client, httpx, group[mid:], merge_context=ctx)])
                    else:
                        merged.append(_concat_partials(group))
                except LLMResponseError:
                    self.progress.emit("合并结果未通过校验，正在保留已完成的分段纪要…")
                    merged.append(_concat_partials(group))
            partials = merged
        return partials[0]

    def run(self) -> None:
        if self._cancelled:
            return
        req = self._req
        action = "原文精整" if req.template.is_refinement else "纪要生成"
        started = time.monotonic()
        try:
            import httpx

            problem = api_url_error(
                req.api_url,
                proxy_bypass_suffixes=req.proxy_bypass_suffixes,
            )
            if problem:
                self.error.emit(problem)
                return
            if not req.lines:
                self.error.emit("文字稿为空，无法生成纪要")
                return

            if req.max_output_tokens <= 0 or req.context_window_tokens <= req.max_output_tokens + 2048:
                raise LLMContextError("模型上下文预算不足，请检查输入与输出上限设置")
            self._cancellation.run(self._run_pipeline(httpx, started))
        except LLMCancelled:
            log.info("纪要生成已取消")
        except Exception as exc:
            if self._cancelled:
                return
            self.transient_failure = is_transient_failure(exc)
            try:
                import httpx
                if isinstance(exc, httpx.TimeoutException):
                    self.error.emit(f"{action}超时，已完成的分段可在重试时继续使用")
                    return
                if isinstance(exc, httpx.HTTPStatusError):
                    status = exc.response.status_code
                    hint = {401: "请检查接口密钥", 403: "请检查模型访问权限", 429: "请检查限流、余额或配额"}.get(
                        status, "请稍后重试，已完成的分段会保留")
                    self.error.emit(f"{action}失败: HTTP {status}；{hint}")
                    return
            except ImportError:
                pass
            log.error("纪要生成失败: %s", exc)
            self.error.emit(f"{action}失败: {exc}")

    async def _run_pipeline(self, httpx, started):
        req = self._req
        transcript_text = render_transcript(
            req.lines, req.speaker_names,
            show_speakers=req.speakers_distinguished, show_times=req.timestamps_available,
            label_unknown=req.label_unknown,
        )
        system_prompt = build_system_prompt(
            req.template, output_language=req.output_language,
            speakers_distinguished=req.speakers_distinguished, timestamps_available=req.timestamps_available,
        )
        chunks = self._plan_chunks(system_prompt) if not req.template.is_refinement else []
        chunked = len(chunks) > 1
        timeout = httpx.Timeout(req.timeout_sec, connect=min(10, req.timeout_sec), write=30, pool=10)
        async with httpx.AsyncClient(
            timeout=timeout,
            **http_client_options(req.proxy_mode, req.api_url, req.proxy_bypass_suffixes),
        ) as client:
            payload: dict[str, Any] | None
            markdown_text = ""
            if req.template.is_refinement:
                payload, chunked = await self._refine(client, httpx)
            elif not chunked:
                self._stage = "正在生成纪要…"
                self.progress.emit(self._stage)
                try:
                    payload = await self._summarize(client, httpx, system_prompt, chunks[0], self._chunk_prompt(chunks, 0))
                except LLMResponseError as exc:
                    if isinstance(exc, (LLMContextError, LLMTruncatedError)) or "不是 JSON" not in str(exc):
                        raise
                    payload = None
                    log.warning("纪要模型未返回 JSON，改用纯 Markdown 提示词重试")
                    self.progress.emit("模型未返回结构化结果，改用 Markdown 重试…")
                    self._preview_markdown = True
                    markdown_text = await self._chat(
                        client, httpx,
                        build_markdown_system_prompt(
                            req.template, output_language=req.output_language,
                            speakers_distinguished=req.speakers_distinguished,
                            timestamps_available=req.timestamps_available,
                        ),
                        build_user_prompt(req.context, chunks[0]),
                    )
                    markdown_text = validate_markdown(markdown_text)
            else:
                partials: list[dict[str, Any]] = []
                for index, chunk in enumerate(chunks, start=1):
                    self._stage = f"正在生成纪要… 第 {index}/{len(chunks)} 段"
                    self.progress.emit(self._stage)
                    chunk_ctx = self._chunk_context(index - 1, len(chunks))
                    part = await self._summarize(
                        client,
                        httpx,
                        system_prompt,
                        chunk,
                        self._chunk_prompt(chunks, index - 1),
                        allow_markdown_fallback=True,
                        chunk_context=chunk_ctx,
                    )
                    partials.append(part)
                payload = await self._merge(client, httpx, partials)

        if self._cancelled:
            return
        if payload is not None:
            # Owned metadata: do not accept a name map supplied by the model.
            payload.pop("_speaker_names", None)
            if not req.timestamps_available:
                clear_timeline(payload)
            if req.speaker_names:
                payload["_speaker_names"] = {str(sid): name for sid, name in req.speaker_names.items()}
            markdown = render_markdown(
                payload,
                template=req.template,
                title_fallback=req.title_fallback,
                duration_ms=req.duration_ms,
                transcript_text=transcript_text,
                speaker_names=req.speaker_names,
                meta_lines=req.meta_lines,
            )
            json_payload = json.dumps(payload, ensure_ascii=False)
        else:
            if not req.timestamps_available:
                markdown_text = strip_clock_citations(markdown_text)
            markdown = apply_speaker_names(markdown_text.strip(), req.speaker_names) + "\n"
            json_payload = ""
        self.finished_ok.emit(
            MinutesResult(
                json_payload=json_payload,
                markdown=markdown,
                chunked=chunked or self._split_occurred,
                duration_ms=int((time.monotonic() - started) * 1000),
                model=req.model_name,
                template_id=req.template.id,
            )
        )


_FACT_NORM_RE = re.compile(r"[\s,，。；;：:！!？?、\-—·()（）\[\]\"'“”‘’]+")
_MD_TS_TRAIL_RE = re.compile(r"\s*\[(\d{1,}:\d{2}:\d{2})\]\s*$")
_MD_ACTION_RE = re.compile(
    r"^(?:-\s*(?:\[[ xX]\]\s*)?)?(?P<task>.+?)"
    r"(?:\s+[—\-]+\s*@?(?P<owner>[^·\[\n]+?))?"
    r"(?:\s*·\s*(?P<due>[^\[\n]+?))?"
    r"(?:\s*\[(?P<ts>\d{1,}:\d{2}:\d{2})\])?\s*$"
)
_INLINE_TS_RE = re.compile(r"\[(\d{1,}:\d{2}:\d{2})\]")


def _slice_context_for_span(
    context: MeetingContext, start_ms: int, end_ms: int, *, chunked: bool
) -> MeetingContext:
    if not chunked:
        return context
    sliced_marks = [
        (offset_ms, note)
        for offset_ms, note in context.marks
        if start_ms <= int(offset_ms) <= end_ms
    ]
    return MeetingContext(
        title=context.title,
        started_at_text=context.started_at_text,
        participants=context.participants,
        agenda=context.agenda,
        notes_user="",
        marks=sliced_marks,
        glossary_terms=list(context.glossary_terms),
    )


def _split_text_spans(left_text: str, right_text: str) -> tuple[int, int, int, int]:
    left_ts = [t for m in _INLINE_TS_RE.finditer(left_text) if (t := parse_timestamp(m.group(1))) is not None]
    right_ts = [t for m in _INLINE_TS_RE.finditer(right_text) if (t := parse_timestamp(m.group(1))) is not None]
    left_start = min(left_ts) if left_ts else 0
    if right_ts:
        right_start = min(right_ts)
        left_end = max(max(left_ts) if left_ts else 0, right_start - 1) if right_start > left_start else max(left_ts or [0])
        right_end = max(max(right_ts), 10**12)
    else:
        left_end = max(left_ts) if left_ts else 10**12
        right_start = left_end + 1 if left_ts else 0
        right_end = 10**12
    return left_start, left_end, right_start, right_end


def _markdown_to_partial(markdown_text: str, template: MinutesTemplate) -> dict[str, Any]:
    """Parse a fallback Markdown chunk summary into the canonical minutes dict."""
    partial: dict[str, Any] = {
        "title": "",
        "summary": "",
        "topics": [],
        "decisions": [],
        "action_items": [],
        "open_questions": [],
        "chapters": [],
        "speakers": [],
    }
    heading_map: dict[str, str] = {}
    for sec_key, default_title in SECTION_TITLES.items():
        heading_map[default_title] = sec_key
        heading_map[section_title(template, sec_key)] = sec_key

    current_sec = ""
    current_topic: dict[str, Any] | None = None
    summary_lines: list[str] = []
    fallback_body: list[str] = []

    for raw_line in markdown_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("```"):
            continue
        if line.startswith("# ") and not partial["title"]:
            partial["title"] = line[2:].strip()
            continue
        if line.startswith("## "):
            heading = line[3:].strip()
            current_sec = heading_map.get(heading, "")
            current_topic = None
            continue
        if line == "本场未识别到":
            continue
        if current_sec == SECTION_SUMMARY:
            summary_lines.append(re.sub(r"^[-*+]\s+", "", line))
        elif current_sec == SECTION_TOPICS:
            if line.startswith("### "):
                current_topic = {"title": line[4:].strip(), "points": []}
                partial["topics"].append(current_topic)
            else:
                bullet = re.sub(r"^[-*+]\s+", "", line)
                ts_match = _MD_TS_TRAIL_RE.search(bullet)
                ts = ts_match.group(1) if ts_match else ""
                text = bullet[:ts_match.start()].strip() if ts_match else bullet.strip()
                if text:
                    if current_topic is None:
                        current_topic = {"title": "讨论要点", "points": []}
                        partial["topics"].append(current_topic)
                    current_topic["points"].append({"text": text, "ts": ts})
        elif current_sec in (SECTION_DECISIONS, SECTION_OPEN_QUESTIONS):
            bullet = re.sub(r"^[-*+]\s+", "", line)
            ts_match = _MD_TS_TRAIL_RE.search(bullet)
            ts = ts_match.group(1) if ts_match else ""
            text = bullet[:ts_match.start()].strip() if ts_match else bullet.strip()
            if text:
                partial[current_sec].append({"text": text, "ts": ts})
        elif current_sec == SECTION_ACTION_ITEMS:
            m = _MD_ACTION_RE.match(line)
            if m and m.group("task").strip():
                owner = (m.group("owner") or "").strip()
                due = (m.group("due") or "").strip()
                partial["action_items"].append({
                    "task": m.group("task").strip(),
                    "owner": "" if owner == "未指定" else owner,
                    "due": "" if due == "未提及" else due,
                    "ts": (m.group("ts") or "").strip(),
                })
        elif current_sec == SECTION_CHAPTERS:
            bullet = re.sub(r"^[-*+]\s+", "", line).replace("**", "")
            if bullet:
                partial["chapters"].append({"title": bullet, "start": "", "end": "", "summary": ""})
        elif current_sec == SECTION_SPEAKERS:
            bullet = re.sub(r"^[-*+]\s+", "", line).replace("**", "")
            if "：" in bullet:
                sp_label, sp_sum = bullet.split("：", 1)
            elif ":" in bullet:
                sp_label, sp_sum = bullet.split(":", 1)
            else:
                sp_label, sp_sum = bullet, ""
            sp_label = re.sub(r"（可能是.*?）", "", sp_label).strip()
            if sp_label:
                partial["speakers"].append({"label": sp_label, "summary": sp_sum.strip()})
        else:
            if not line.startswith("#"):
                fallback_body.append(re.sub(r"^[-*+]\s+", "", line))

    if summary_lines:
        partial["summary"] = "\n".join(summary_lines).strip()
    elif not any(partial[k] for k in ("topics", "decisions", "action_items", "open_questions", "chapters", "speakers")):
        partial["summary"] = "\n".join(fallback_body).strip()
    return partial


def _fact_signature(item: Any, key: str) -> str:
    if not isinstance(item, dict):
        return _FACT_NORM_RE.sub("", str(item or "")).lower()
    primary_field = "task" if key == "action_items" else "text"
    primary = _FACT_NORM_RE.sub("", str(item.get(primary_field, "") or "")).lower()
    if key == "action_items":
        owner = _FACT_NORM_RE.sub("", str(item.get("owner", "") or "")).lower()
        return f"{primary}@{owner}" if owner else primary
    return primary


def _dedup_fact_items(items: Sequence[Any], key: str, source_lookup: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    pos_by_sig: dict[str, int] = {}
    for raw in items:
        if not isinstance(raw, dict):
            continue
        entry = dict(raw)
        sig = _fact_signature(entry, key)
        if not sig:
            continue
        if source_lookup and sig in source_lookup:
            src = source_lookup[sig]
            for attr in ("ts", "owner", "due"):
                if attr in src and not str(entry.get(attr, "") or "").strip() and str(src.get(attr, "") or "").strip():
                    entry[attr] = src[attr]
        if sig in pos_by_sig:
            existing = deduped[pos_by_sig[sig]]
            for attr in ("ts", "owner", "due"):
                if not str(existing.get(attr, "") or "").strip() and str(entry.get(attr, "") or "").strip():
                    existing[attr] = entry[attr]
            continue
        pos_by_sig[sig] = len(deduped)
        deduped.append(entry)
    return deduped


def _preserve_facts(payload, partials):
    """Respect the merge model's consolidated conclusions (so overturned early
    items stay dropped), deduplicate paraphrased/repeated entries, and only
    backfill from source chunks when a section was accidentally omitted."""
    multi_chunk = len(partials) > 1
    for key in ("decisions", "action_items", "open_questions"):
        source_items: list[dict[str, Any]] = []
        for part in partials:
            if isinstance(part, dict):
                for item in part.get(key) or []:
                    if isinstance(item, dict):
                        source_items.append(item)
        deduped_sources = _dedup_fact_items(source_items, key)
        source_by_sig = {_fact_signature(it, key): it for it in deduped_sources}
        merged_items = payload.get(key) if isinstance(payload.get(key), list) else None
        if merged_items:
            payload[key] = _dedup_fact_items(merged_items, key, source_lookup=source_by_sig)
        elif key == "action_items":
            # Keep tasks when a merge model omits action_items altogether.
            payload[key] = deduped_sources
        elif merged_items is None or not multi_chunk:
            payload[key] = deduped_sources
        else:
            payload[key] = []
    return payload


# Matched with ``match(text, pos)``: anchored at ``pos`` (a line start).
_LINE_PREFIX_RE = re.compile(r"(?:\[\d{2,}:\d{2}:\d{2}\][^\n：]*：|[^\n\[\]：]{1,24}：)")


def _split_chunk(text):
    cut = len(text) // 2
    newline = text.rfind("\n", 0, cut)
    if newline > len(text) // 4:
        return text[:newline], text[newline + 1:]
    # Mid-line cut: the right half keeps the ``[ts] speaker：`` prefix of the
    # line it came from, never the first line's (that would hand one
    # speaker's words to another, at the wrong time).
    line_start = text.rfind("\n", 0, cut) + 1
    prefix = _LINE_PREFIX_RE.match(text, line_start)
    if prefix is not None and prefix.end() <= cut:
        return text[:cut], prefix.group(0) + text[cut:]
    return text[:cut], text[cut:]


def _concat_partials(partials: Sequence[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {
        "title": "",
        "summary": "",
        "topics": [],
        "decisions": [],
        "action_items": [],
        "open_questions": [],
        "chapters": [],
        "speakers": [],
    }
    summaries = []
    for part in partials:
        if not isinstance(part, dict):
            continue
        if not merged["title"] and part.get("title"):
            merged["title"] = part["title"]
        if part.get("summary"):
            summaries.append(str(part["summary"]).strip())
        for key in ("topics", "decisions", "action_items", "open_questions", "chapters", "speakers"):
            value = part.get(key)
            if isinstance(value, list):
                seen = {json.dumps(item, sort_keys=True, ensure_ascii=False) for item in merged[key]}
                for item in value:
                    identity = json.dumps(item, sort_keys=True, ensure_ascii=False)
                    if identity not in seen:
                        merged[key].append(item)
                        seen.add(identity)
    merged["summary"] = "\n\n".join(dict.fromkeys(s for s in summaries if s))
    return merged


class MinutesGenerator(QObject):
    """Owns one worker at a time; cancel() supersedes the previous run."""

    progress = pyqtSignal(str)
    preview = pyqtSignal(str)
    finished_ok = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker: MinutesWorker | None = None
        self._workers: list[MinutesWorker] = []
        # Of the last ``error``: whether the same request may succeed later.
        self.last_error_transient = False

    @property
    def is_running(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    def generate(self, request: MinutesRequest) -> None:
        self.cancel()
        worker = MinutesWorker(request)
        worker.progress.connect(self._on_progress)
        worker.preview.connect(self._on_preview)
        worker.finished_ok.connect(self._on_done)
        worker.error.connect(self._on_error)
        if hasattr(worker, "finished"):
            worker.finished.connect(lambda w=worker: self._reap_worker(w))
        self._worker = worker
        self._workers.append(worker)
        worker.start()

    def _reap_worker(self, worker: MinutesWorker) -> None:
        if worker.isRunning():
            return
        try:
            self._workers.remove(worker)
        except ValueError:
            pass
        if worker is self._worker and not worker.isRunning():
            self._worker = None

    def cancel(self) -> None:
        worker = self._worker
        if worker is not None and worker.isRunning():
            worker.cancel()
            for sig, slot in (
                (worker.progress, self._on_progress),
                (worker.preview, self._on_preview),
                (worker.finished_ok, self._on_done),
                (worker.error, self._on_error),
            ):
                try:
                    sig.disconnect(slot)
                except TypeError:
                    pass
        elif worker is not None:
            self._reap_worker(worker)
        self._worker = None

    def _on_done(self, result) -> None:
        if self._worker is None or self.sender() is not self._worker:
            return
        self._worker = None
        self.finished_ok.emit(result)

    def _on_error(self, message: str) -> None:
        if self._worker is None or self.sender() is not self._worker:
            return
        self.last_error_transient = bool(self._worker.transient_failure)
        self._worker = None
        self.error.emit(message)

    def _on_progress(self, text: str) -> None:
        if self._worker is not None and self.sender() is self._worker:
            self.progress.emit(text)

    def _on_preview(self, text: str) -> None:
        if self._worker is not None and self.sender() is self._worker:
            self.preview.emit(text)

    @property
    def running_workers(self):
        return tuple(worker for worker in self._workers if worker.isRunning())

    def shutdown(self, *, wait_ms: int = 2000) -> None:
        self.cancel()
        for worker in list(self._workers):
            if worker.isRunning():
                worker.cancel()
                if wait_ms:
                    worker.wait(wait_ms)
            self._reap_worker(worker)

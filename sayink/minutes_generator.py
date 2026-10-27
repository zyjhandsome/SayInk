"""Stream, validate and checkpoint bounded meeting-summary requests.

Long meetings use bounded hierarchical merges. Completed, validated steps
can be resumed; partial streams never become saved meeting documents.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Sequence

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from sayink.minutes_prompts import (
    DEFAULT_CHUNK_CHARS,
    MeetingContext,
    MinutesTemplate,
    TranscriptLine,
    build_markdown_system_prompt,
    build_merge_prompt,
    build_merge_system_prompt,
    build_system_prompt,
    build_user_prompt,
    chunk_transcript,
    render_transcript,
)
from sayink.minutes_render import apply_speaker_names, extract_json, render_markdown
from sayink.text_polisher import (
    LLMCancelled, LLMResponseError, LLMTruncatedError, api_url_error, strip_model_thinking,
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


def request_fingerprint(request: MinutesRequest) -> str:
    """Invalidate checkpoints whenever any generation input or contract changes."""
    data = {
        "version": 3, "api_url": request.api_url.rstrip("/"),
        "credential": hashlib.sha256(request.api_key.encode()).hexdigest(),
        "model": request.model_name, "template": asdict(request.template),
        "context": asdict(request.context), "lines": [asdict(line) for line in request.lines],
        "names": request.speaker_names, "language": request.output_language,
        "chunk_chars": request.chunk_chars, "context_tokens": request.context_window_tokens,
        "output_tokens": request.max_output_tokens,
        "prompts": [build_system_prompt(request.template, output_language=request.output_language),
                    build_merge_system_prompt(request.template, output_language=request.output_language),
                    REFINE_SYSTEM_PROMPT],
    }
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


def estimate_prompt_tokens(system: str, user: str) -> int:
    # Unknown compatible models need a conservative, tokenizer-independent
    # budget. UTF-8 bytes bound byte-token counts; reserve message overhead too.
    return len(system.encode("utf-8")) + len(user.encode("utf-8")) + 64


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
        self._last_preview_at = 0.0
        self._stage = ""
        self._split_occurred = False
        self._preview_markdown = False
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
        self._last_preview_at = 0
        self.preview.emit("")

    def _on_delta(self, delta):
        self._preview_raw += delta
        if len(self._preview_raw) > self._req.max_output_tokens * 32:
            raise LLMResponseError("模型输出异常过长，本次结果未保存")
        if time.monotonic() - self._last_preview_at >= 0.15:
            self._publish_preview()

    def _publish_preview(self):
        raw = strip_model_thinking(self._preview_raw).lstrip()
        readable = []
        for match in re.finditer(r'"(?:summary|text|task)"\s*:\s*"((?:\\.|[^"\\])*)(?:"|$)', raw):
            value = match.group(1)
            try:
                readable.append(json.loads('"' + value + '"'))
            except ValueError:
                continue  # A split escape sequence is completed in the next event.
        if not readable and self._preview_markdown and not raw.startswith(("{", "[", "```")):
            readable = [raw]
        if readable:
            self.preview.emit("生成预览 · 尚未保存\n\n" + self._stage + "\n\n" + "\n\n".join(readable)[-24000:])
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

    async def _refine(self, client, httpx) -> tuple[dict, bool]:
        req = self._req
        size = min(req.chunk_chars, max(200, req.max_output_tokens // 3))
        while True:
            chunks = refinement_chunks(req.lines, max_chars=size)
            if all(self._fits(REFINE_SYSTEM_PROMPT, refinement_user_prompt(
                chunks, i, req.speaker_names, req.context.glossary_terms)) for i in range(len(chunks))):
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
        base_prompt = refinement_user_prompt(chunks, index, req.speaker_names, req.context.glossary_terms)
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
                prompt = refinement_user_prompt(chunks, index, req.speaker_names, req.context.glossary_terms,
                                                retry_reason=reason)
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
        base_prompt = refinement_user_prompt(chunks, index, self._req.speaker_names, self._req.context.glossary_terms)
        self._remember(REFINE_SYSTEM_PROMPT, base_prompt, json.dumps({"paragraphs": paragraphs}, ensure_ascii=False))
        return paragraphs

    # -- pipeline ----------------------------------------------------------

    def _chunk_prompt(self, chunks, index):
        context = ""
        if index:
            context += "前文（仅供理解，不重复整理）：\n" + chunks[index - 1][-200:] + "\n"
        if index + 1 < len(chunks):
            context += "后文（仅供理解，不提前整理）：\n" + chunks[index + 1][:200] + "\n"
        return context + build_user_prompt(self._req.context, chunks[index],
                                          chunk_label=f"第 {index + 1}/{len(chunks)} 段" if len(chunks) > 1 else "")

    def _plan_chunks(self, system):
        size = max(64, int(self._req.chunk_chars))
        while True:
            chunks = chunk_transcript(self._req.lines, self._req.speaker_names, max_chars=size)
            if all(self._fits(system, self._chunk_prompt(chunks, i)) for i in range(len(chunks))):
                return chunks
            if size <= 64:
                raise LLMContextError("提示词、笔记或术语表过长，请缩短内容或调整模型上下文设置")
            size = max(64, size // 2)

    async def _summarize(self, client, httpx, system, text, prompt, depth=0):
        try:
            raw = await self._chat(client, httpx, system, prompt)
        except (LLMContextError, LLMTruncatedError):
            if len(text) < 128 or depth >= 8:
                raise
            self._split_occurred = True
            self.progress.emit(self._stage + " · 内容较长，正在细分处理…")
            left_text, right_text = _split_chunk(text)
            left = await self._summarize(client, httpx, system, left_text,
                                         build_user_prompt(self._req.context, left_text), depth + 1)
            right = await self._summarize(client, httpx, system, right_text,
                                          build_user_prompt(self._req.context, right_text), depth + 1)
            payload = await self._merge(client, httpx, [left, right])
            self._remember(system, prompt, json.dumps(payload, ensure_ascii=False))
            return payload
        payload = extract_json(raw)
        if payload is None:
            raise LLMResponseError(self._stage + "返回的不是 JSON")
        validate_minutes(payload)
        self._remember(system, prompt, raw)
        return payload

    async def _merge(self, client, httpx, partials):
        system = build_merge_system_prompt(self._req.template, output_language=self._req.output_language)
        level = 0
        while len(partials) > 1:
            level += 1
            groups, current = [], []
            for part in partials:
                if current and not self._fits(system, build_merge_prompt(self._req.context, current + [part])):
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
                prompt = build_merge_prompt(self._req.context, group)
                try:
                    raw = await self._chat(client, httpx, system, prompt)
                    payload = validate_minutes(extract_json(raw))
                    self._remember(system, prompt, raw)
                    merged.append(_preserve_facts(payload, group))
                except (LLMContextError, LLMTruncatedError):
                    if len(group) > 2:
                        mid = len(group) // 2
                        merged.extend([await self._merge(client, httpx, group[:mid]),
                                       await self._merge(client, httpx, group[mid:])])
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

            problem = api_url_error(req.api_url)
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
        transcript_text = render_transcript(req.lines, req.speaker_names)
        system_prompt = build_system_prompt(req.template, output_language=req.output_language)
        chunks = self._plan_chunks(system_prompt) if not req.template.is_refinement else []
        chunked = len(chunks) > 1
        timeout = httpx.Timeout(req.timeout_sec, connect=min(10, req.timeout_sec), write=30, pool=10)
        async with httpx.AsyncClient(timeout=timeout) as client:
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
                        build_markdown_system_prompt(req.template, output_language=req.output_language),
                        build_user_prompt(req.context, chunks[0]),
                    )
                    markdown_text = validate_markdown(markdown_text)
            else:
                partials: list[dict[str, Any]] = []
                for index, chunk in enumerate(chunks, start=1):
                    self._stage = f"正在生成纪要… 第 {index}/{len(chunks)} 段"
                    self.progress.emit(self._stage)
                    part = await self._summarize(client, httpx, system_prompt, chunk, self._chunk_prompt(chunks, index - 1))
                    partials.append(part)
                payload = await self._merge(client, httpx, partials)

        if self._cancelled:
            return
        if payload is not None:
            # Owned metadata: do not accept a name map supplied by the model.
            payload.pop("_speaker_names", None)
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


def _preserve_facts(payload, partials):
    """Keep source decisions and tasks verbatim; let the model unify prose."""
    for key in ("decisions", "action_items", "open_questions"):
        originals = []
        seen = set()
        for part in partials:
            for item in part.get(key, []):
                identity = json.dumps(item, sort_keys=True, ensure_ascii=False)
                if identity not in seen:
                    seen.add(identity)
                    originals.append(item)
        payload[key] = originals
    return payload


# Matched with ``match(text, pos)``: anchored at ``pos`` (a line start).
_LINE_PREFIX_RE = re.compile(r"\[\d{2,}:\d{2}:\d{2}\][^\n：]*：")


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

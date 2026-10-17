"""Generate meeting minutes with the configured OpenAI-compatible endpoint.

Single request when the transcript fits; otherwise map-reduce: each chunk is
summarised to the same JSON schema, then one merge request produces the
final JSON. When the model will not return JSON, one more attempt asks for
plain Markdown (``json_payload`` stays empty). docs/meeting-notes-design.md §5.4.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from sayink.minutes_prompts import (
    DEFAULT_CHUNK_CHARS,
    MERGE_SYSTEM_PROMPT,
    MeetingContext,
    MinutesTemplate,
    TranscriptLine,
    build_markdown_system_prompt,
    build_merge_prompt,
    build_system_prompt,
    build_user_prompt,
    chunk_transcript,
    render_transcript,
)
from sayink.minutes_render import extract_json, render_markdown
from sayink.text_polisher import INSECURE_URL_ERROR, LLMCancelled, LLMTruncatedError, chat_completion, is_secure_or_local_url
from sayink.transcript_refiner import (
    REFINE_SYSTEM_PROMPT,
    RefinementValidationError,
    refinement_chunks,
    refinement_user_prompt,
    render_refinement,
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
    finished_ok = pyqtSignal(object)  # MinutesResult
    error = pyqtSignal(str)

    def __init__(self, request: MinutesRequest):
        super().__init__()
        self._req = request
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    # -- HTTP ------------------------------------------------------------

    def _chat(self, client, httpx, system_prompt: str, user_prompt: str) -> str:
        """One request through the polisher's shared chat_completion()."""
        return chat_completion(
            client, httpx,
            api_url=self._req.api_url,
            api_key=self._req.api_key,
            model_name=self._req.model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            max_tokens=MAX_OUTPUT_TOKENS,
            is_cancelled=lambda: self._cancelled,
            retry_delay_sec=_RETRY_DELAY_SEC,
            label="原文精整" if self._req.template.is_refinement else "纪要生成",
            reject_truncated=self._req.template.is_refinement,
        )

    def _refine(self, client, httpx) -> tuple[dict, bool]:
        req = self._req
        chunks = refinement_chunks(req.lines, max_chars=req.chunk_chars)
        paragraphs = []
        for index, units in enumerate(chunks):
            reason = ""
            for attempt in range(2):
                if self._cancelled:
                    raise LLMCancelled()
                self.progress.emit(f"正在整理原文… 第 {index + 1}/{len(chunks)} 段" + (" · 正在重新核对" if attempt else ""))
                try:
                    raw = self._chat(
                        client, httpx, REFINE_SYSTEM_PROMPT,
                        refinement_user_prompt(chunks, index, req.speaker_names, req.context.glossary_terms,
                                               retry_reason=reason),
                    )
                    paragraphs.extend(validate_refinement(extract_json(raw), units))
                    break
                except (RefinementValidationError, LLMTruncatedError) as exc:
                    reason = str(exc)
                    if attempt:
                        raise RefinementValidationError(
                            f"第 {index + 1} 段未通过完整性检查（{reason}）。本次结果未保存，请重试或更换模型"
                        ) from exc
        payload = {"kind": "refine", "paragraphs": paragraphs}
        if not render_refinement(payload).strip():
            raise RefinementValidationError("文字稿中没有可整理的有效内容，本次结果未保存")
        return payload, len(chunks) > 1

    # -- pipeline ----------------------------------------------------------

    def run(self) -> None:
        if self._cancelled:
            return
        req = self._req
        action = "原文精整" if req.template.is_refinement else "纪要生成"
        started = time.monotonic()
        try:
            import httpx

            if not is_secure_or_local_url(req.api_url):
                self.error.emit(INSECURE_URL_ERROR)
                return
            if not req.lines:
                self.error.emit("文字稿为空，无法生成纪要")
                return

            transcript_text = render_transcript(req.lines, req.speaker_names)
            chunks = chunk_transcript(req.lines, req.speaker_names, max_chars=max(1000, int(req.chunk_chars)))
            chunked = len(chunks) > 1
            system_prompt = build_system_prompt(req.template, output_language=req.output_language)

            with httpx.Client(timeout=req.timeout_sec) as client:
                payload: dict[str, Any] | None
                markdown_text = ""
                if req.template.is_refinement:
                    payload, chunked = self._refine(client, httpx)
                elif not chunked:
                    self.progress.emit("正在生成纪要…")
                    raw = self._chat(client, httpx, system_prompt, build_user_prompt(req.context, chunks[0]))
                    payload = extract_json(raw)
                    if payload is None:
                        log.warning("纪要模型未返回 JSON，改用纯 Markdown 提示词重试")
                        self.progress.emit("模型未返回结构化结果，改用 Markdown 重试…")
                        markdown_text = self._chat(
                            client, httpx,
                            build_markdown_system_prompt(req.template, output_language=req.output_language),
                            build_user_prompt(req.context, chunks[0]),
                        )
                else:
                    partials: list[dict[str, Any]] = []
                    for index, chunk in enumerate(chunks, start=1):
                        self.progress.emit(f"正在生成纪要… 第 {index}/{len(chunks)} 段")
                        raw = self._chat(
                            client, httpx, system_prompt,
                            build_user_prompt(req.context, chunk, chunk_label=f"第 {index}/{len(chunks)} 段"),
                        )
                        part = extract_json(raw)
                        if part is None:
                            raise RuntimeError(f"第 {index} 段返回的不是 JSON")
                        partials.append(part)
                    self.progress.emit("正在合并各段纪要…")
                    raw = self._chat(client, httpx, MERGE_SYSTEM_PROMPT, build_merge_prompt(req.context, partials))
                    payload = extract_json(raw)
                    if payload is None:
                        # Merge failed to produce JSON: render the partials
                        # back to back rather than losing everything.
                        log.warning("合并请求未返回 JSON，按分段结果拼接渲染")
                        payload = _concat_partials(partials)

            if self._cancelled:
                return
            if payload is not None:
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
                markdown = markdown_text.strip() + "\n"
                json_payload = ""
            self.finished_ok.emit(
                MinutesResult(
                    json_payload=json_payload,
                    markdown=markdown,
                    chunked=chunked,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    model=req.model_name,
                    template_id=req.template.id,
                )
            )
        except LLMCancelled:
            log.info("纪要生成已取消")
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            if self._cancelled:
                return
            try:
                import httpx

                if isinstance(exc, httpx.TimeoutException):
                    self.error.emit(f"{action}超时，请稍后重试")
                    return
                if isinstance(exc, httpx.HTTPStatusError):
                    self.error.emit(f"{action}失败: HTTP {exc.response.status_code}")
                    return
            except ImportError:
                pass
            log.error("纪要生成失败: %s", exc)
            self.error.emit(f"{action}失败: {exc}")


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
                merged[key].extend(value)
    merged["summary"] = "\n\n".join(s for s in summaries if s)
    return merged


class MinutesGenerator(QObject):
    """Owns one worker at a time; cancel() supersedes the previous run."""

    progress = pyqtSignal(str)
    finished_ok = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker: MinutesWorker | None = None

    @property
    def is_running(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    def generate(self, request: MinutesRequest) -> None:
        self.cancel()
        worker = MinutesWorker(request)
        worker.progress.connect(self.progress)
        worker.finished_ok.connect(self._on_done)
        worker.error.connect(self._on_error)
        self._worker = worker
        worker.start()

    def cancel(self) -> None:
        worker = self._worker
        if worker is not None and worker.isRunning():
            worker.cancel()
            try:
                worker.disconnect()
            except TypeError:
                pass
        self._worker = None

    def _on_done(self, result) -> None:
        self._worker = None
        self.finished_ok.emit(result)

    def _on_error(self, message: str) -> None:
        self._worker = None
        self.error.emit(message)

    def shutdown(self) -> None:
        worker = self._worker
        self.cancel()
        if worker is not None and worker.isRunning():
            worker.wait(2000)

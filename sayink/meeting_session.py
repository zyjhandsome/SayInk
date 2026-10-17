"""Meeting lifecycle: state machine, options, minutes generation, renaming.

The controller owns *what* a meeting is (record, marks, speakers, notes,
timing) and talks to the database and the LLM. It does not touch audio or
the recognizer: :class:`sayink.app.App` drives those and calls into here at
the seams (``begin`` / ``request_end`` / ``finalize``).

State machine (docs/meeting-notes-design.md §5.3)::

    IDLE → LIVE ⇄ PAUSED → DRAINING → ENDED → (GENERATING) → ENDED
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from sayink.config import Config
from sayink.history_store import HistoryStore
from sayink.meeting_store import STATUS_LIVE, MeetingNotes, MeetingRecord, MeetingStore
from sayink.minutes_generator import MinutesGenerator, MinutesRequest, MinutesResult
from sayink.minutes_prompts import (
    GENERAL_TEMPLATE_ID,
    MeetingContext,
    TranscriptLine,
    format_timestamp,
    get_template,
    speaker_label,
    transcript_lines,
)
from sayink.minutes_render import render_markdown, replace_names_in_markdown
from sayink.speaker_embedding import STICKY_GAP_MS, SpeakerEmbedder, make_embedder
from sayink.speaker_session import SpeakerSession, recluster
from sayink.speech_recognizer import (
    AUX_MODEL_SILERO_VAD,
    AUX_MODEL_SPEAKER,
    SUPPORTS_HOTWORDS,
    SUPPORTS_LANGUAGE,
    effective_language,
    format_hotwords,
    get_model_dir,
    is_model_downloaded,
    model_supports,
)
from sayink.text_polisher import is_local_endpoint, polish_settings_complete
from sayink.vad_segmenter import VAD_ENERGY, VAD_SILERO

log = logging.getLogger("SayInk")


class MeetingState(Enum):
    IDLE = "idle"
    LIVE = "live"
    PAUSED = "paused"
    DRAINING = "draining"
    ENDED = "ended"


@dataclass
class MeetingOptions:
    """What the start card lets the user set for this one meeting."""

    title: str = ""
    participants: str = ""
    agenda: str = ""
    input_source: str = "mixed"
    model_id: str = ""        # "" → the dictation model
    glossary: list[dict[str, Any]] = field(default_factory=list)
    vad: str = VAD_SILERO
    language: str = "auto"

    @classmethod
    def from_config(cls, config: Config) -> "MeetingOptions":
        # Glossary / VAD / language are shared with dictation (stt.*).
        glossary = config.get("stt.glossary", []) or []
        return cls(
            input_source=str(config.get("meeting.input_source", "mixed") or "mixed"),
            model_id=str(config.get("meeting.model_id", "") or ""),
            glossary=[g for g in glossary if isinstance(g, dict) and g.get("term")],
            vad=str(config.get("stt.vad", VAD_SILERO) or VAD_SILERO),
            language=str(config.get("stt.language", "auto") or "auto"),
        )


@dataclass(frozen=True)
class MeetingCapabilities:
    """What the chosen model / aux models can do for this meeting (start card copy)."""

    model_id: str
    hotwords: str
    hotwords_supported: bool
    language: str
    language_supported: bool
    vad: str
    speaker_model_available: bool

    @property
    def notes(self) -> list[str]:
        out: list[str] = []
        if self.hotwords and not self.hotwords_supported:
            out.append("当前识别模型不支持识别期热词，本场术语只用于纪要。")
        if self.vad == VAD_ENERGY:
            out.append("Silero VAD 模型未下载，使用能量断句（引擎页可下载）。")
        if not self.speaker_model_available:
            out.append("说话人识别模型未下载，说话人区分较粗略（引擎页可下载）。")
        return out


def aux_model_path(model_id: str) -> Path | None:
    """Path to a downloaded aux model's single file, or None."""
    if not is_model_downloaded(model_id):
        return None
    try:
        model_dir = get_model_dir(model_id)
    except ValueError:
        return None
    from sayink.speech_recognizer import get_model_info

    info = get_model_info(model_id)
    if not info:
        return None
    return model_dir / info["files"][0]


def resolve_capabilities(config: Config, options: MeetingOptions, *, dictation_model_id: str) -> MeetingCapabilities:
    model_id = options.model_id or dictation_model_id
    hotwords = format_hotwords(options.glossary)
    language = effective_language(options.language)
    vad = options.vad
    if vad == VAD_SILERO and aux_model_path(AUX_MODEL_SILERO_VAD) is None:
        vad = VAD_ENERGY
    speaker_available = bool(config.get("stt.speaker_model_enabled", True)) and aux_model_path(AUX_MODEL_SPEAKER) is not None
    return MeetingCapabilities(
        model_id=model_id,
        hotwords=hotwords,
        hotwords_supported=model_supports(model_id, SUPPORTS_HOTWORDS),
        language=language,
        language_supported=model_supports(model_id, SUPPORTS_LANGUAGE),
        vad=vad,
        speaker_model_available=speaker_available,
    )


def safe_filename(title: str, *, fallback: str = "会议纪要") -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", title or "").strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return (cleaned or fallback)[:80]


def minutes_settings(config: Config) -> tuple[str, str, str]:
    """(api_url, api_key, model_name) for minutes: the polish endpoint, with
    ``meeting.model_name`` overriding the model when set."""
    api_url = str(config.get("llm.api_url", "") or "")
    api_key = str(config.get("llm.api_key", "") or "")
    model_name = str(config.get("meeting.model_name", "") or config.get("llm.model_name", "") or "")
    return api_url, api_key, model_name


def minutes_settings_complete(config: Config) -> bool:
    return polish_settings_complete(*minutes_settings(config))


class MeetingController(QObject):
    state_changed = pyqtSignal(object)          # MeetingState
    meeting_started = pyqtSignal(str)           # meeting_id
    meeting_ended = pyqtSignal(str)             # meeting_id (ENDED, segments all stored)
    meeting_changed = pyqtSignal(str)           # transcript / marks / speakers / notes changed
    clock_tick = pyqtSignal(str)                # "hh:mm:ss" once a second while LIVE/PAUSED
    generation_progress = pyqtSignal(str, str)  # meeting_id, text
    generation_finished = pyqtSignal(str)       # meeting_id
    generation_failed = pyqtSignal(str, str)    # meeting_id, message
    notice = pyqtSignal(str, str)               # title, detail (for bar / tray)

    def __init__(
        self,
        config: Config,
        history: HistoryStore,
        *,
        pending_segments: Callable[[], int],
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self._config = config
        self._history = history
        self._store = MeetingStore(history)
        self._pending_segments = pending_segments
        self._state = MeetingState.IDLE
        self._current: MeetingRecord | None = None
        self._options: MeetingOptions | None = None
        self._capabilities: MeetingCapabilities | None = None
        self._embedder: SpeakerEmbedder | None = None
        self._speakers: SpeakerSession | None = None
        self._started_wall_ms = 0
        self._paused_detail = ""
        self._clock = QTimer(self)
        self._clock.setInterval(1000)
        self._clock.timeout.connect(self._tick)
        self._drain_timer = QTimer(self)
        self._drain_timer.setInterval(300)
        self._drain_timer.timeout.connect(self._poll_drain)
        self._generator = MinutesGenerator(self)
        self._generator.progress.connect(self._on_generation_progress)
        self._generator.finished_ok.connect(self._on_generation_done)
        self._generator.error.connect(self._on_generation_error)
        self._generating_for = ""
        self._history.add_committed_callback(self._on_history_committed_threadsafe)

    # -- accessors -----------------------------------------------------------

    @property
    def store(self) -> MeetingStore:
        return self._store

    @property
    def state(self) -> MeetingState:
        return self._state

    @property
    def is_active(self) -> bool:
        """LIVE / PAUSED / DRAINING: audio belongs to a meeting."""
        return self._state in (MeetingState.LIVE, MeetingState.PAUSED, MeetingState.DRAINING)

    @property
    def current(self) -> MeetingRecord | None:
        return self._current

    @property
    def current_id(self) -> str:
        return self._current.id if self._current else ""

    @property
    def options(self) -> MeetingOptions | None:
        return self._options

    @property
    def capabilities(self) -> MeetingCapabilities | None:
        return self._capabilities

    @property
    def speakers(self) -> SpeakerSession | None:
        return self._speakers

    @property
    def embedder(self) -> SpeakerEmbedder | None:
        return self._embedder

    @property
    def generating_for(self) -> str:
        return self._generating_for

    def elapsed_ms(self) -> int:
        if not self._started_wall_ms:
            return 0
        return max(0, int(time.time() * 1000) - self._started_wall_ms)

    def elapsed_text(self) -> str:
        return format_timestamp(self.elapsed_ms())

    def _set_state(self, state: MeetingState) -> None:
        if state is self._state:
            return
        self._state = state
        self.state_changed.emit(state)

    # -- lifecycle ------------------------------------------------------------

    def begin(self, options: MeetingOptions, capabilities: MeetingCapabilities) -> MeetingRecord:
        """Create the meeting row and the in-memory speaker session."""
        if self.is_active:
            raise RuntimeError("已有会议在进行中")
        self._options = options
        self._capabilities = capabilities
        speaker_path = aux_model_path(AUX_MODEL_SPEAKER) if capabilities.speaker_model_available else None
        self._embedder = make_embedder(speaker_path, enabled=capabilities.speaker_model_available)
        self._speakers = SpeakerSession(
            threshold=self._embedder.threshold,
            keep_history=True,
            sticky_threshold=self._embedder.sticky_threshold,
            sticky_gap_ms=STICKY_GAP_MS,
        )
        record = self._store.create_meeting(
            title=options.title,
            participants=options.participants,
            agenda=options.agenda,
            source=options.input_source,
            model_id=capabilities.model_id,
            vad=capabilities.vad,
            glossary=options.glossary,
        )
        self._current = record
        # The row must exist before the first transcript segment references it.
        self._store.flush()
        self._started_wall_ms = record.started_at
        self._paused_detail = ""
        self._set_state(MeetingState.LIVE)
        self._clock.start()
        self.meeting_started.emit(record.id)
        self.meeting_changed.emit(record.id)
        return record

    def pause(self, detail: str = "") -> None:
        if self._state is not MeetingState.LIVE:
            return
        self._paused_detail = detail
        self._set_state(MeetingState.PAUSED)

    def resume(self) -> None:
        if self._state is not MeetingState.PAUSED:
            return
        self._paused_detail = ""
        self._set_state(MeetingState.LIVE)

    @property
    def paused_detail(self) -> str:
        return self._paused_detail

    def request_end(self) -> None:
        """Audio capture has stopped; wait for queued segments, then finalize."""
        if self._state not in (MeetingState.LIVE, MeetingState.PAUSED):
            return
        self._set_state(MeetingState.DRAINING)
        self._drain_timer.start()
        self._poll_drain()

    def _poll_drain(self) -> None:
        if self._state is not MeetingState.DRAINING:
            self._drain_timer.stop()
            return
        if self._pending_segments() > 0:
            return
        self._drain_timer.stop()
        self.finalize()

    def finalize(self) -> None:
        """Mark the meeting ended, re-cluster speakers, drop the embeddings."""
        if self._current is None or self._state is MeetingState.ENDED:
            return
        meeting_id = self._current.id
        self._clock.stop()
        self._drain_timer.stop()
        ended_at = int(time.time() * 1000)
        self._store.end_meeting(meeting_id, ended_at=ended_at)
        self._apply_recluster(meeting_id)
        self._store.flush()
        self._speakers = None
        self._embedder = None
        self._current = self._store.get_meeting(meeting_id) or MeetingRecord(
            id=meeting_id, started_at=self._started_wall_ms, ended_at=ended_at, status="ended"
        )
        self._set_state(MeetingState.ENDED)
        self.meeting_ended.emit(meeting_id)
        self.meeting_changed.emit(meeting_id)
        # Back to idle for the next meeting; the page keeps showing this one.
        self._current = None
        self._options = None
        self._capabilities = None
        self._started_wall_ms = 0
        self._set_state(MeetingState.IDLE)

    def _apply_recluster(self, meeting_id: str) -> None:
        speakers = self._speakers
        if speakers is None or speakers.speaker_count < 2:
            return
        embedder = self._embedder
        threshold = embedder.recluster_threshold if embedder is not None else speakers.threshold
        mapping = recluster(speakers.history, threshold=threshold)
        if mapping and any(old != new for old, new in mapping.items()):
            log.info("会议 %s 说话人重聚类: %s", meeting_id[:8], mapping)
            self._store.relabel_segments(meeting_id, mapping)
            self._store.flush()
            # Speaker rows were written under the old ids; move display names.
            existing = {s.speaker_id: s for s in self._store.get_speakers(meeting_id)}
            for old, new in mapping.items():
                row = existing.get(old)
                if row is not None and old != new and row.display_name:
                    self._store.set_speaker(meeting_id, new, display_name=row.display_name)

    def abandon_on_exit(self) -> None:
        """App is quitting mid-meeting: close the record without waiting."""
        if self._current is None or not self.is_active:
            return
        self._clock.stop()
        self._drain_timer.stop()
        self._store.end_meeting(self._current.id)
        self._store.flush(timeout=3.0)
        self._set_state(MeetingState.IDLE)
        self._current = None

    def mark_interrupted(self) -> None:
        self._store.mark_interrupted_meetings()

    def _tick(self) -> None:
        self.clock_tick.emit(self.elapsed_text())

    def _on_history_committed_threadsafe(self) -> None:
        # Called on the writer thread; hop to the GUI thread via the signal.
        meeting_id = self.current_id
        if meeting_id:
            self.meeting_changed.emit(meeting_id)

    # -- live edits -----------------------------------------------------------

    def add_mark(self, note: str = "") -> int | None:
        if self._current is None or not self.is_active:
            return None
        offset = self.elapsed_ms()
        self._store.add_mark(self._current.id, offset, note.strip())
        self.notice.emit("已标记", format_timestamp(offset))
        self.meeting_changed.emit(self._current.id)
        return offset

    def update_meeting(self, meeting_id: str, **fields: str) -> None:
        self._store.update_meeting(meeting_id, **fields)
        if self._current is not None and self._current.id == meeting_id:
            self._current = replace(self._current, **{k: v for k, v in fields.items() if v is not None})
        self.meeting_changed.emit(meeting_id)

    def rename_speaker(self, meeting_id: str, speaker_id: int, name: str) -> int:
        """Store the name; re-render JSON-backed notes, or whole-word replace
        user-edited notes. Returns the number of replacements made in
        edited notes (0 when re-rendered)."""
        name = (name or "").strip()
        existing = {s.speaker_id: s for s in self._store.get_speakers(meeting_id)}
        old_label = existing[speaker_id].label if speaker_id in existing else speaker_label(speaker_id)
        self._store.set_speaker(meeting_id, speaker_id, display_name=name)
        self._store.flush()
        replaced = 0
        notes = self._store.get_notes(meeting_id)
        if notes is not None and notes.markdown:
            new_label = name or speaker_label(speaker_id)
            if notes.user_edited or not notes.json_payload:
                new_md, replaced = replace_names_in_markdown(notes.markdown, old_label, new_label)
                if replaced and notes.user_edited:
                    self._store.save_user_markdown(meeting_id, new_md)
                elif replaced:
                    self._store.save_notes(
                        meeting_id, template_id=notes.template_id, model=notes.model,
                        json_payload="", markdown=new_md, duration_ms=notes.duration_ms,
                        chunked=notes.chunked, generated_at=notes.generated_at, keep_previous=True,
                    )
            else:
                self._rerender_notes(meeting_id, notes)
            self._store.flush()
        self.meeting_changed.emit(meeting_id)
        return replaced

    def speaker_names(self, meeting_id: str) -> dict[int, str]:
        return {s.speaker_id: s.display_name for s in self._store.get_speakers(meeting_id) if s.display_name}

    def correct_segment_speaker(self, meeting_id: str, seq: int, speaker_id: int) -> bool:
        record = self._store.get_meeting(meeting_id)
        if record is None or record.status == STATUS_LIVE or self._generating_for == meeting_id or speaker_id <= 0:
            return False
        segment = next((s for s in self._store.get_segments(meeting_id) if s.seq == seq), None)
        if segment is None:
            return False
        self._store.set_segment_speaker(meeting_id, seq, speaker_id)
        if not self._store.flush():
            return False
        saved = next((s for s in self._store.get_segments(meeting_id) if s.seq == seq), None)
        if saved is None or saved.speaker_id != speaker_id:
            return False
        self.meeting_changed.emit(meeting_id)
        return True

    def transcript_text(self, meeting_id: str) -> str:
        record = self._store.get_meeting(meeting_id)
        if record is None:
            return ""
        names = self.speaker_names(meeting_id)
        return "\n".join(line.render(names) for line in transcript_lines(self._store.get_segments(meeting_id), record.started_at))

    def export_transcript_markdown(self, meeting_id: str) -> str:
        record = self._store.get_meeting(meeting_id)
        if record is None:
            return ""
        metadata = "\n".join(f"- {line}" for line in self._meeting_meta_lines(record))
        return f"# {record.display_title}\n\n{metadata}\n\n## 文字稿\n\n{self.transcript_text(meeting_id)}\n"

    def _meeting_meta_lines(self, record: MeetingRecord) -> tuple[str, ...]:
        lines = [f"时间：{time.strftime('%Y-%m-%d %H:%M', time.localtime(record.started_at / 1000))}"]
        if record.ended_at:
            lines.append(f"时长：{format_timestamp(record.duration_ms)}")
        if record.participants.strip():
            lines.append(f"参会人：{record.participants.strip()}")
        return tuple(lines)

    def _rerender_notes(self, meeting_id: str, notes: MeetingNotes) -> None:
        record = self._store.get_meeting(meeting_id)
        if record is None:
            return
        payload = notes.payload()
        lines = transcript_lines(self._store.get_segments(meeting_id), record.started_at)
        names = self.speaker_names(meeting_id)
        markdown = render_markdown(
            payload,
            template=get_template(notes.template_id or GENERAL_TEMPLATE_ID, self._custom_templates()),
            title_fallback=record.display_title,
            duration_ms=record.duration_ms,
            transcript_text="\n".join(line.render(names) for line in lines),
            speaker_names=names,
            meta_lines=self._meeting_meta_lines(record),
        )
        self._store.save_notes(
            meeting_id, template_id=notes.template_id, model=notes.model,
            json_payload=notes.json_payload, markdown=markdown,
            duration_ms=notes.duration_ms, chunked=notes.chunked, generated_at=notes.generated_at,
            keep_previous=True,
        )

    def save_user_markdown(self, meeting_id: str, markdown: str) -> bool:
        self._store.save_user_markdown(meeting_id, markdown)
        # The page re-reads the notes right away to show 「已手动编辑」.
        if not self._store.flush():
            return False
        saved = self._store.get_notes(meeting_id)
        if saved is None or saved.markdown != markdown:
            return False
        self.meeting_changed.emit(meeting_id)
        return True

    def restore_previous_notes(self, meeting_id: str) -> None:
        self._store.restore_previous_notes(meeting_id)
        self._store.flush()
        self.meeting_changed.emit(meeting_id)

    def delete_meeting(self, meeting_id: str) -> None:
        if self._generating_for == meeting_id:
            self._generator.cancel()
            self._generating_for = ""
        self._store.delete_meeting(meeting_id)
        self._store.flush()
        self.meeting_changed.emit(meeting_id)

    # -- export ----------------------------------------------------------------

    def export_markdown(self, meeting_id: str, *, include_transcript: bool = True) -> str:
        record = self._store.get_meeting(meeting_id)
        if record is None:
            return ""
        notes = self._store.get_notes(meeting_id)
        names = self.speaker_names(meeting_id)
        parts: list[str] = []
        if notes is not None and notes.markdown.strip():
            parts.append(notes.markdown.rstrip())
        else:
            parts.append(f"# {record.display_title}")
            parts.extend(f"- {line}" for line in self._meeting_meta_lines(record))
        if include_transcript:
            lines = transcript_lines(self._store.get_segments(meeting_id), record.started_at)
            if lines:
                parts.append("\n## 文字稿\n")
                parts.extend(line.render(names) for line in lines)
        return "\n".join(parts).rstrip() + "\n"

    # -- minutes generation ----------------------------------------------------

    def _custom_templates(self) -> list[dict[str, Any]]:
        custom = self._config.get("meeting.custom_templates", []) or []
        return [t for t in custom if isinstance(t, dict)]

    def remote_host_needing_confirmation(self) -> str:
        """Hostname when the minutes endpoint is remote and not yet confirmed."""
        api_url, _key, _model = minutes_settings(self._config)
        if not api_url or is_local_endpoint(api_url):
            return ""
        host = (urlparse(api_url).hostname or "").lower()
        confirmed = {str(h).lower() for h in (self._config.get("meeting.confirmed_remote_hosts", []) or [])}
        return "" if not host or host in confirmed else host

    def confirm_remote_host(self, host: str) -> None:
        hosts = list(self._config.get("meeting.confirmed_remote_hosts", []) or [])
        if host and host not in hosts:
            hosts.append(host)
            self._config.set("meeting.confirmed_remote_hosts", hosts)

    def transcript_char_count(self, meeting_id: str) -> int:
        record = self._store.get_meeting(meeting_id)
        if record is None:
            return 0
        lines = transcript_lines(self._store.get_segments(meeting_id), record.started_at)
        return sum(len(line.text) for line in lines)

    def generate(self, meeting_id: str, *, template_id: str | None = None, model_name: str | None = None) -> bool:
        """Start minutes generation; False (with ``generation_failed``) when
        the configuration or the transcript makes it impossible."""
        record = self._store.get_meeting(meeting_id)
        if record is None:
            self.generation_failed.emit(meeting_id, "会议不存在")
            return False
        api_url, api_key, default_model = minutes_settings(self._config)
        model = (model_name or "").strip() or default_model
        if not polish_settings_complete(api_url, api_key, model):
            self.generation_failed.emit(meeting_id, "未配置纪要模型：请在 设置 → AI 服务 填写接口地址、密钥与模型")
            return False
        template = get_template(
            template_id or record_template_id(self._config, self._store.get_notes(meeting_id)),
            self._custom_templates(),
        )
        segments = self._store.get_segments(meeting_id)
        lines: list[TranscriptLine] = transcript_lines(segments, record.started_at, prefer_polished=not template.is_refinement)
        if not lines:
            self.generation_failed.emit(meeting_id, "文字稿为空，无法整理原文" if template.is_refinement else "文字稿为空，无法生成纪要")
            return False
        names = self.speaker_names(meeting_id)
        marks = [(m.offset_ms, m.note) for m in self._store.get_marks(meeting_id)]
        context = MeetingContext(
            title=record.title,
            started_at_text=time.strftime("%Y-%m-%d %H:%M", time.localtime(record.started_at / 1000)),
            participants=record.participants,
            agenda=record.agenda,
            notes_user=record.notes_user,
            marks=marks,
            glossary_terms=[str(g.get("term")) for g in record.glossary if isinstance(g, dict) and g.get("term")],
        )
        request = MinutesRequest(
            api_url=api_url,
            api_key=api_key,
            model_name=model,
            template=template,
            context=context,
            lines=lines,
            speaker_names=names,
            duration_ms=record.duration_ms,
            title_fallback=record.display_title,
            output_language=str(self._config.get("meeting.output_language", "auto") or "auto"),
            chunk_chars=int(self._config.get("meeting.single_request_char_limit", 20000) or 20000) // 2,
            meta_lines=self._meeting_meta_lines(record),
        )
        self._generating_for = meeting_id
        self.generation_progress.emit(meeting_id, "正在整理原文…" if template.is_refinement else "正在生成纪要…")
        self._generator.generate(request)
        return True

    def cancel_generation(self) -> None:
        if self._generating_for:
            meeting_id = self._generating_for
            self._generating_for = ""
            self._generator.cancel()
            self.generation_failed.emit(meeting_id, "已取消")

    @property
    def is_generating(self) -> bool:
        return bool(self._generating_for) and self._generator.is_running

    def _on_generation_progress(self, text: str) -> None:
        if self._generating_for:
            self.generation_progress.emit(self._generating_for, text)

    def _on_generation_done(self, result: MinutesResult) -> None:
        meeting_id = self._generating_for
        self._generating_for = ""
        if not meeting_id:
            return
        self._store.save_notes(
            meeting_id,
            template_id=result.template_id,
            model=result.model,
            json_payload=result.json_payload,
            markdown=result.markdown,
            duration_ms=result.duration_ms,
            chunked=result.chunked,
        )
        self._store.flush()
        self.generation_finished.emit(meeting_id)
        self.meeting_changed.emit(meeting_id)

    def _on_generation_error(self, message: str) -> None:
        meeting_id = self._generating_for
        self._generating_for = ""
        if meeting_id:
            self.generation_failed.emit(meeting_id, message)

    def shutdown(self) -> None:
        self.abandon_on_exit()
        self._clock.stop()
        self._drain_timer.stop()
        self._generator.shutdown()


def record_template_id(config: Config, notes: MeetingNotes | None) -> str:
    if notes is not None and notes.template_id:
        return notes.template_id
    return str(config.get("meeting.template_id", GENERAL_TEMPLATE_ID) or GENERAL_TEMPLATE_ID)

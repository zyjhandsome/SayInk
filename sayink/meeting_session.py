"""Meeting lifecycle: state machine, options, minutes generation, renaming.

The controller owns *what* a meeting is (record, marks, speakers, notes,
timing) and talks to the database and the LLM. It does not touch audio or
the recognizer: :class:`sayink.app.App` drives those and calls into here at
the seams (``begin`` / ``request_end`` / ``finalize``).

State machine (docs/meeting-notes-design.md §5.3)::

    IDLE → LIVE ⇄ PAUSED → DRAINING → ENDED → (GENERATING) → ENDED

Minutes run one meeting at a time from a persistent queue: user requests
first, then automatic end-of-meeting jobs in order
(docs/meeting-generation-reliability.md「排队与自动生成」).
"""

from __future__ import annotations

import ipaddress
import logging
import json
import re
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Callable
from urllib.parse import urlparse

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from sayink.config import Config
from sayink.history_store import HistoryStore
from sayink.meeting_store import (
    QUEUE_PRIORITY_AUTO,
    QUEUE_PRIORITY_USER,
    STATUS_ENDED,
    STATUS_INTERRUPTED,
    STATUS_LIVE,
    MeetingNotes,
    MeetingRecord,
    MeetingStore,
    QueuedGeneration,
    now_ms,
)
from sayink.minutes_generator import MinutesGenerator, MinutesRequest, MinutesResult, request_fingerprint
from sayink.minutes_prompts import (
    GENERAL_TEMPLATE_ID,
    REFINE_TEMPLATE_ID,
    DEFAULT_CHUNK_CHARS,
    MeetingContext,
    TranscriptLine,
    find_template,
    format_timestamp,
    get_template,
    render_srt,
    speaker_label,
    transcript_lines,
)
from sayink.minutes_render import apply_speaker_names, render_markdown, replace_names_in_markdown
from sayink.recognition_settings import (
    ADVANCED_RECOGNITION_PATH,
    RecognitionSettings,
    audio_source,
    aux_model_path,
)
from sayink.speaker_embedding import SpeakerEmbedder, make_embedder
from sayink.meeting_speakers import MeetingSpeakerSession
from sayink.speaker_clustering import final_assignments
from sayink.speaker_session import SpeakerSession, recluster
from sayink.speaker_naming import infer_speaker_names
from sayink.speech_recognizer import (
    AUX_MODEL_SILERO_VAD,
    AUX_MODEL_SPEAKER,
)
from sayink.text_polisher import is_local_endpoint, polish_settings_complete
from sayink.vad_segmenter import VAD_ENERGY, VAD_SILERO

log = logging.getLogger("SayInk")

# Shorter transcripts are not worth an automatic request (and make hollow minutes).
AUTO_MIN_TRANSCRIPT_CHARS = 200
# Waits before re-running an automatic job after a transient failure; after
# the last one the job is left for the user.
AUTO_RETRY_DELAYS_MS = (60_000, 300_000)


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
        shared = RecognitionSettings.from_config(config)
        return cls(
            input_source=audio_source(config, meeting=True),
            model_id=str(config.get("meeting.model_id", "") or ""),
            glossary=shared.glossary,
            vad=shared.vad,
            language=shared.language,
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
    vad_requested: str = VAD_SILERO
    vad_error: str = ""
    speaker_enabled: bool = True
    speaker_load_failed: bool = False
    speaker_activity_enabled: bool = False

    @property
    def notes(self) -> list[str]:
        out: list[str] = []
        if self.hotwords and not self.hotwords_supported:
            out.append("当前识别模型不支持识别期热词，本场术语只用于纪要。")
        if not self.language_supported:
            out.append("当前模型不支持指定识别语言，将按模型自身能力识别。")
        if self.vad_requested == VAD_SILERO and self.vad == VAD_ENERGY:
            if self.vad_error:
                out.append(f"Silero VAD 加载失败，已回退能量分段；可在 {ADVANCED_RECOGNITION_PATH} 查看状态。")
            else:
                out.append("Silero VAD 模型未下载，使用能量断句（引擎页可下载）。")
        if not self.speaker_enabled:
            out.append("CAM++ 说话人增强已关闭，使用频谱粗略区分。")
        elif self.speaker_load_failed:
            out.append("CAM++ 加载失败，已回退频谱粗略区分。")
        elif not self.speaker_model_available:
            out.append("说话人识别模型未下载，说话人区分较粗略（引擎页可下载）。")
        return out


def resolve_capabilities(config: Config, options: MeetingOptions, *, dictation_model_id: str) -> MeetingCapabilities:
    model_id = options.model_id or dictation_model_id
    shared = replace(
        RecognitionSettings.from_config(config), model_id=model_id,
        glossary=options.glossary, language=options.language, vad=options.vad,
    )
    vad = options.vad
    if vad == VAD_SILERO and aux_model_path(AUX_MODEL_SILERO_VAD) is None:
        vad = VAD_ENERGY
    speaker_available = shared.speaker_model_enabled and aux_model_path(AUX_MODEL_SPEAKER) is not None
    return MeetingCapabilities(
        model_id=model_id,
        hotwords=shared.hotwords,
        hotwords_supported=shared.hotwords_supported,
        language=shared.effective_language,
        language_supported=shared.language_supported,
        vad=vad,
        speaker_model_available=speaker_available,
        vad_requested=options.vad,
        speaker_enabled=shared.speaker_model_enabled,
        speaker_activity_enabled=shared.speaker_activity_enabled,
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


def remote_host_needing_confirmation(config: Config) -> str:
    """Hostname when the minutes endpoint is remote and not yet confirmed."""
    api_url, _key, _model = minutes_settings(config)
    if not api_url or is_local_endpoint(api_url):
        return ""
    host = (urlparse(api_url).hostname or "").lower()
    confirmed = {str(h).lower() for h in (config.get("meeting.confirmed_remote_hosts", []) or [])}
    return "" if not host or host in confirmed else host


def confirm_remote_host(config: Config, host: str) -> None:
    hosts = list(config.get("meeting.confirmed_remote_hosts", []) or [])
    if host and host not in hosts:
        hosts.append(host)
        config.set("meeting.confirmed_remote_hosts", hosts)


def runs_on_this_machine(url: str) -> bool:
    """A loopback model shares the CPU / GPU with live speech recognition."""
    host = (urlparse((url or "").strip()).hostname or "").strip("[]").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class MeetingController(QObject):
    state_changed = pyqtSignal(object)          # MeetingState
    meeting_started = pyqtSignal(str)           # meeting_id
    meeting_ended = pyqtSignal(str)             # meeting_id (ENDED, segments all stored)
    meeting_changed = pyqtSignal(str)           # transcript / marks / speakers / notes changed
    clock_tick = pyqtSignal(str)                # "hh:mm:ss" once a second while LIVE/PAUSED
    generation_progress = pyqtSignal(str, str)  # meeting_id, text
    generation_preview = pyqtSignal(str, str)   # meeting_id, unsaved readable preview
    generation_finished = pyqtSignal(str)       # meeting_id
    generation_failed = pyqtSignal(str, str)    # meeting_id, message
    generation_cancelled = pyqtSignal(str)      # meeting_id (user cancelled; not a failure)
    generation_queued = pyqtSignal(str)         # meeting_id waits in the queue (new, deferred or retrying)
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
        self._generator.preview.connect(self._on_generation_preview)
        self._generator.finished_ok.connect(self._on_generation_done)
        self._generator.error.connect(self._on_generation_error)
        self._generating_for = ""
        self.generation_preview_text = ""
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._pump_queue)
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
        if self._store.disabled:
            # Without the DB every segment write is a silent no-op.
            raise RuntimeError("历史数据库不可用，会议文字稿无法保存")
        self._options = options
        speaker_path = aux_model_path(AUX_MODEL_SPEAKER) if capabilities.speaker_model_available else None
        self._embedder = make_embedder(speaker_path, enabled=capabilities.speaker_model_available,
                                      activity_enabled=capabilities.speaker_activity_enabled)
        self._capabilities = replace(
            capabilities,
            speaker_model_available=self._embedder.name == "campplus",
            speaker_load_failed=capabilities.speaker_model_available and self._embedder.name != "campplus",
        )
        self._speakers = MeetingSpeakerSession.for_embedder(self._embedder)
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
        self._yield_local_model()
        self.meeting_started.emit(record.id)
        self.meeting_changed.emit(record.id)
        return record

    def _yield_local_model(self) -> None:
        """A model on this machine would slow live recognition into a backlog
        pause. An automatic job steps aside (finished parts stay checkpointed)
        and resumes when the meeting ends; a user request keeps running."""
        meeting_id = self._generating_for
        if not meeting_id or not runs_on_this_machine(minutes_settings(self._config)[0]):
            return
        job = self._store.queued_generation(meeting_id)
        if job is None or not job.automatic:
            return
        log.info("会议开始，本机纪要模型让出资源，会议 %s 的纪要稍后继续", meeting_id[:8])
        self._generating_for = ""
        self.generation_preview_text = ""
        self._generator.cancel()
        self.generation_queued.emit(meeting_id)

    def pause(self, detail: str = "") -> None:
        if self._state is MeetingState.PAUSED:
            if detail != self._paused_detail:
                self._paused_detail = detail
                # Same state, but the page and bar read the new reason.
                self.state_changed.emit(MeetingState.PAUSED)
            return
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
        # The last segments may still sit in the writer queue; the re-cluster
        # reads the table, so they must be committed first or they keep
        # their greedy live label.
        self._store.flush()
        self._apply_recluster(meeting_id)
        self._store.flush()
        self.auto_name_speakers(meeting_id)
        self._enqueue_automatic(meeting_id)
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
        self._pump_queue()

    def _apply_recluster(self, meeting_id: str) -> None:
        """End-of-meeting pass: cluster every turn and re-assign each segment.

        Named or manually corrected speakers keep their numbers and locked
        segments never move. Falls back to the centroid merge when the
        session cannot provide per-turn evidence.
        """
        speakers = self._speakers
        if speakers is None:
            return
        embedder = self._embedder
        protected = {s.speaker_id for s in self._store.get_speakers(meeting_id) if s.name_locked or s.display_name}
        segments = self._store.get_segments(meeting_id)
        locked = {s.seq for s in segments if s.speaker_locked}
        protected.update(s.speaker_id for s in segments if s.speaker_locked)
        if isinstance(speakers, MeetingSpeakerSession) and any(t.key >= 0 for t in speakers.turns):
            threshold = embedder.cluster_threshold if embedder is not None else speakers.threshold
            assignments = final_assignments(speakers, threshold, protected_ids=protected, locked_keys=locked)
            current = {s.seq: s.speaker_id for s in segments}
            changed = {seq: sid for seq, sid in assignments.items() if seq >= 0 and current.get(seq) not in (None, sid)}
            if changed:
                log.info("会议 %s 说话人重新归属 %d 段", meeting_id[:8], len(changed))
                self._store.set_segment_speakers(meeting_id, changed)
                self._store.flush()
            return
        if speakers.speaker_count < 2:
            return
        threshold = embedder.recluster_threshold if embedder is not None else speakers.threshold
        mapping = recluster(speakers.history, threshold=threshold, protected_ids=protected)
        if mapping and any(old != new for old, new in mapping.items()):
            log.info("会议 %s 说话人重聚类: %s", meeting_id[:8], mapping)
            self._store.relabel_segments(meeting_id, mapping)
            self._store.flush()

    def abandon_on_exit(self) -> None:
        """App is quitting mid-meeting: close the record without waiting."""
        if self._current is None or not self.is_active:
            return
        self._clock.stop()
        self._drain_timer.stop()
        # The exit dialog tells the user this meeting is「已中断」, not a normal end.
        self._store.end_meeting(self._current.id, status=STATUS_INTERRUPTED)
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

    def rename_speaker(self, meeting_id: str, speaker_id: int, name: str) -> int | None:
        """Store the name; re-render JSON-backed notes, or whole-word replace
        user-edited notes. Returns the number of replacements made in
        edited notes (0 when re-rendered).

        ``None`` when the meeting is draining/generating, the identity no
        longer exists, or the name write cannot be verified. Dialogs can stay
        open while capture continues, so their targets must be rechecked.
        """
        if self._generating_for == meeting_id:
            return None
        record = self._store.get_meeting(meeting_id)
        if (record is None or record.status not in (STATUS_LIVE, STATUS_ENDED, STATUS_INTERRUPTED)
                or speaker_id <= 0):
            return None
        if record.status == STATUS_LIVE and (self.current_id != meeting_id or self._state is MeetingState.DRAINING):
            return None
        if speaker_id not in {s.speaker_id for s in self._store.get_segments(meeting_id)}:
            return None
        name = (name or "").strip()
        existing = {s.speaker_id: s for s in self._store.get_speakers(meeting_id)}
        old_label = existing[speaker_id].label if speaker_id in existing else speaker_label(speaker_id)
        self._store.set_speaker(meeting_id, speaker_id, display_name=name)
        if not self._store.flush():
            return None
        saved = next((s for s in self._store.get_speakers(meeting_id) if s.speaker_id == speaker_id), None)
        if saved is None or saved.display_name != name or not saved.name_locked:
            return None
        replaced = 0
        notes = self._store.get_notes(meeting_id)
        if notes is not None and notes.markdown:
            new_label = name or speaker_label(speaker_id)
            markdown = None
            payload_json = notes.json_payload
            if not notes.user_edited and notes.json_payload:
                payload = notes.payload()
                payload.setdefault("_speaker_names", {str(sid): row.label for sid, row in existing.items()})
                payload_json = json.dumps(payload, ensure_ascii=False)
                markdown = self._render_notes(meeting_id, replace(notes, json_payload=payload_json))
            if markdown is not None:
                self._save_rendered_notes(meeting_id, notes, markdown, json_payload=payload_json)
            else:
                # Hand-edited notes, Markdown-only notes, or a document whose
                # template no longer exists: patch the names in the text.
                new_md, replaced = replace_names_in_markdown(notes.markdown, old_label, new_label)
                if replaced and notes.user_edited:
                    self._store.save_user_markdown(meeting_id, new_md)
                elif replaced:
                    self._save_rendered_notes(meeting_id, notes, new_md, json_payload=notes.json_payload)
            self._store.flush()
        self.auto_name_speakers(meeting_id)
        self.meeting_changed.emit(meeting_id)
        return replaced

    def speaker_names(self, meeting_id: str) -> dict[int, str]:
        return {
            s.speaker_id: s.label for s in self._store.get_speakers(meeting_id)
            if s.display_name or (s.suggested_name and not s.name_locked)
        }

    def auto_name_speakers(self, meeting_id: str) -> bool:
        """Resolve only explicit introductions in raw speech, after clustering.

        Polished/model-generated text is never evidence for a person's name.
        This also upgrades older meetings when they are next organised.
        """
        record = self._store.get_meeting(meeting_id)
        if record is None or record.status == STATUS_LIVE or self._generating_for == meeting_id:
            return False
        segments = self._store.get_segments(meeting_id)
        rows = self._store.get_speakers(meeting_id)
        manual = {s.speaker_id: s.display_name for s in rows if s.display_name or s.name_locked}
        inferred = infer_speaker_names(
            transcript_lines(segments, record.started_at, prefer_polished=False), manual_names=manual,
        )
        previous = {s.speaker_id: s.suggested_name for s in rows if s.suggested_name and not s.display_name and not s.name_locked}
        if inferred == previous:
            return True
        names = {**inferred, **{sid: name for sid, name in manual.items() if name}}
        notes = self._store.get_notes(meeting_id)
        markdown = None
        if notes is not None and notes.markdown:
            if not notes.user_edited and notes.json_payload:
                markdown = self._render_notes(meeting_id, notes, names=names)
            if markdown is None:
                markdown = notes.markdown
                for sid in previous.keys() | inferred.keys():
                    old = previous.get(sid) or speaker_label(sid)
                    new = names.get(sid) or speaker_label(sid)
                    markdown, _ = replace_names_in_markdown(markdown, old, new)
        self._store.save_inferred_names(meeting_id, inferred, markdown=markdown)
        if not self._store.flush():
            return False
        if self.speaker_names(meeting_id) != names:
            return False
        saved = self._store.get_notes(meeting_id) if markdown is not None else None
        if markdown is not None and (saved is None or saved.markdown != markdown):
            return False
        self.meeting_changed.emit(meeting_id)
        return True

    def save_segment_text(self, meeting_id: str, seq: int, text: str | None) -> bool:
        """Edit one stored segment. Live and paused meetings can edit segments already written."""
        record = self._store.get_meeting(meeting_id)
        if record is None or self._generating_for == meeting_id or (text is not None and not text.strip()):
            return False
        if record.status == STATUS_LIVE:
            if self.current_id != meeting_id or self._state not in (MeetingState.LIVE, MeetingState.PAUSED):
                return False
        elif record.status not in (STATUS_ENDED, STATUS_INTERRUPTED):
            return False
        segment = next((s for s in self._store.get_segments(meeting_id) if s.seq == seq), None)
        if segment is None:
            return False
        original = (segment.polished_text or segment.raw_text).strip()
        corrected = text.strip() if text is not None else ""
        if corrected == original:
            corrected = ""
        if segment.corrected_text == corrected:
            return True
        self._store.set_segment_text(meeting_id, seq, corrected or None)
        if not self._store.flush():
            return False
        saved = next((s for s in self._store.get_segments(meeting_id) if s.seq == seq), None)
        if saved is None or saved.corrected_text != corrected:
            return False
        self.meeting_changed.emit(meeting_id)
        return True

    def correct_segment_speaker(self, meeting_id: str, seq: int, speaker_id: int | None) -> bool:
        """Lock a persisted segment's speaker; None allocates a new manual identity."""
        record = self._store.get_meeting(meeting_id)
        if (record is None or record.status not in (STATUS_LIVE, STATUS_ENDED, STATUS_INTERRUPTED)
                or self._generating_for == meeting_id or (speaker_id is not None and speaker_id <= 0)):
            return False
        live = record.status == STATUS_LIVE
        if live and (self.current_id != meeting_id or self._state is MeetingState.DRAINING):
            return False
        segments = self._store.get_segments(meeting_id)
        segment = next((s for s in segments if s.seq == seq), None)
        if segment is None:
            return False
        used_ids = {s.speaker_id for s in segments} | {s.speaker_id for s in self._store.get_speakers(meeting_id)}
        if speaker_id is None:
            minimum = max(used_ids, default=0) + 1
            speaker_id = self._speakers.reserve_speaker_id(minimum) if live and self._speakers else minimum
        elif live and speaker_id not in used_ids:
            return False
        self._store.set_segment_speaker(meeting_id, seq, speaker_id)
        if not self._store.flush():
            return False
        saved = next((s for s in self._store.get_segments(meeting_id) if s.seq == seq), None)
        if saved is None or saved.speaker_id != speaker_id or not saved.speaker_locked:
            return False
        if live and isinstance(self._speakers, MeetingSpeakerSession):
            self._speakers.confirm(seq, speaker_id)
        self.auto_name_speakers(meeting_id)
        self.meeting_changed.emit(meeting_id)
        return True

    def transcript_text(self, meeting_id: str) -> str:
        record = self._store.get_meeting(meeting_id)
        if record is None:
            return ""
        names = self.speaker_names(meeting_id)
        return "\n".join(line.render(names) for line in transcript_lines(self._store.get_segments(meeting_id), record.started_at))

    def export_transcript_srt(self, meeting_id: str) -> str:
        record = self._store.get_meeting(meeting_id)
        if record is None:
            return ""
        lines = transcript_lines(self._store.get_segments(meeting_id), record.started_at)
        return render_srt(lines, self.speaker_names(meeting_id))

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

    def _save_rendered_notes(self, meeting_id: str, notes: MeetingNotes, markdown: str, *, json_payload: str) -> None:
        self._store.save_notes(
            meeting_id, template_id=notes.template_id, model=notes.model,
            json_payload=json_payload, markdown=markdown,
            duration_ms=notes.duration_ms, chunked=notes.chunked, generated_at=notes.generated_at,
            keep_previous=True,
        )

    def _notes_template(self, notes: MeetingNotes, payload: dict):
        """The template the saved notes were rendered with, or ``None`` when
        it is gone (a deleted custom template): re-rendering through 「通用会议」
        would replace 原文精整 prose with empty sections, or change the
        section set of a custom minutes template."""
        template = find_template(notes.template_id or GENERAL_TEMPLATE_ID, self._custom_templates())
        refine_payload = payload.get("kind") == "refine"
        if template is not None and template.is_refinement == refine_payload:
            return template
        # Refinement prose renders the same through any refinement template.
        return get_template(REFINE_TEMPLATE_ID) if refine_payload else None

    def _render_notes(self, meeting_id: str, notes: MeetingNotes, *, names: dict[int, str] | None = None) -> str | None:
        record = self._store.get_meeting(meeting_id)
        if record is None:
            return None
        payload = notes.payload()
        template = self._notes_template(notes, payload)
        if template is None:
            return None
        lines = transcript_lines(self._store.get_segments(meeting_id), record.started_at)
        names = self.speaker_names(meeting_id) if names is None else names
        return render_markdown(
            payload,
            template=template,
            title_fallback=record.display_title,
            duration_ms=record.duration_ms,
            transcript_text="\n".join(line.render(names) for line in lines),
            speaker_names=names,
            meta_lines=self._meeting_meta_lines(record),
        )

    def save_user_markdown(self, meeting_id: str, markdown: str) -> bool:
        self._store.save_user_markdown(meeting_id, markdown)
        # The page re-reads the notes right away to show 「已手动编辑」.
        if not self._store.flush():
            return False
        saved = self._store.get_notes(meeting_id)
        if saved is None or saved.markdown != markdown:
            return False
        job = self._store.queued_generation(meeting_id)
        if job is not None and meeting_id != self._generating_for:
            # Hand-written notes are the version the user wants. A job that
            # has not started, automatic or requested, must not replace them.
            self._store.dequeue_generation(meeting_id)
            self._store.flush()
        self.meeting_changed.emit(meeting_id)
        return True

    def restore_previous_notes(self, meeting_id: str) -> bool:
        """Swap in the previous minutes. False when the saved text did not change."""
        before = self._store.get_notes(meeting_id)
        if before is None or not before.previous_markdown:
            return False
        expected = before.previous_markdown
        self._store.restore_previous_notes(meeting_id)
        if not self._store.flush():
            return False
        notes = self._store.get_notes(meeting_id)
        if notes is None or not notes.markdown:
            return False
        names = self.speaker_names(meeting_id)
        # Speakers may have been named since that version was written.
        # Patch the labels; whether it counts as hand-edited is the
        # restored version's own flag, not a side effect of restoring.
        markdown = apply_speaker_names(notes.markdown, names)
        if markdown != notes.markdown:
            if notes.user_edited:
                self._store.save_user_markdown(meeting_id, markdown)
            else:
                self._save_rendered_notes(meeting_id, notes, markdown, json_payload=notes.json_payload)
            if not self._store.flush():
                return False
            notes = self._store.get_notes(meeting_id)
            if notes is None or not notes.markdown:
                return False
        if notes.markdown not in {expected, apply_speaker_names(expected, names)}:
            return False
        self.meeting_changed.emit(meeting_id)
        return True

    def supplements_markdown(self, meeting_id: str) -> str:
        """Agenda, the user's own notes and marks. Empty when the meeting has none."""
        record = self._store.get_meeting(meeting_id)
        if record is None:
            return ""
        parts: list[str] = []
        if record.agenda.strip():
            parts.append("## 议程\n\n" + record.agenda.strip())
        if record.notes_user.strip():
            parts.append("## 我的笔记\n\n" + record.notes_user.strip())
        marks = self._store.get_marks(meeting_id)
        if marks:
            lines = [f"- [{format_timestamp(mark.offset_ms)}] {mark.note.strip() or '标记'}" for mark in marks]
            parts.append("## 标记\n\n" + "\n".join(lines))
        return "\n\n".join(parts)

    def delete_meeting(self, meeting_id: str) -> None:
        if self._generating_for == meeting_id:
            self._generator.cancel()
            self._generating_for = ""
            self.generation_preview_text = ""
        self._store.delete_meeting(meeting_id)
        self._store.flush()
        self.meeting_changed.emit(meeting_id)
        self._pump_queue()

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
        supplement = self.supplements_markdown(meeting_id)
        if supplement:
            parts.append(supplement)
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
        return remote_host_needing_confirmation(self._config)

    def confirm_remote_host(self, host: str) -> None:
        confirm_remote_host(self._config, host)

    def transcript_char_count(self, meeting_id: str) -> int:
        record = self._store.get_meeting(meeting_id)
        if record is None:
            return 0
        lines = transcript_lines(self._store.get_segments(meeting_id), record.started_at)
        return sum(len(line.text) for line in lines)

    def auto_generate_enabled(self) -> bool:
        return bool(self._config.get("meeting.auto_generate", False))

    def queued_generation(self, meeting_id: str) -> QueuedGeneration | None:
        return self._store.queued_generation(meeting_id)

    def defers_automatic_jobs(self) -> bool:
        """Automatic jobs on a model on this machine wait for the live meeting to end."""
        return self.is_active and runs_on_this_machine(minutes_settings(self._config)[0])

    def queue_position(self, meeting_id: str) -> int:
        """1-based place among jobs waiting to run; 0 when not waiting."""
        waiting = [job.meeting_id for job in self._store.generation_queue()
                   if not job.failed and job.meeting_id != self._generating_for]
        return waiting.index(meeting_id) + 1 if meeting_id in waiting else 0

    def generate(self, meeting_id: str, *, template_id: str | None = None, model_name: str | None = None) -> bool:
        """User request: start now, or queue ahead of automatic jobs while
        another meeting generates. False (with ``generation_failed``) when
        the configuration or the transcript makes it impossible."""
        if self._generating_for == meeting_id:
            return False
        template_id = template_id or record_template_id(self._config, self._store.get_notes(meeting_id))
        if self._generating_for:
            # One worker, and its result is stored against the running id.
            problem = self._generation_problem(self._store.get_meeting(meeting_id), template_id, model_name)
        else:
            problem = self._start(meeting_id, template_id, model_name)
        if problem is None:
            return False
        if problem:
            self.generation_failed.emit(meeting_id, problem)
            return False
        self._store.enqueue_generation(meeting_id, template_id, priority=QUEUE_PRIORITY_USER)
        self._store.flush()
        if self._generating_for != meeting_id:
            self.generation_queued.emit(meeting_id)
        return True

    def resume_generation_queue(self) -> None:
        """Startup: continue jobs a quit or crash interrupted."""
        self._pump_queue()

    def _enqueue_automatic(self, meeting_id: str) -> None:
        if not self.auto_generate_enabled() or not minutes_settings_complete(self._config):
            return
        if self.transcript_char_count(meeting_id) < AUTO_MIN_TRANSCRIPT_CHARS:
            log.info("会议 %s 内容过短，不自动生成纪要", meeting_id[:8])
            return
        template_id = str(self._config.get("meeting.template_id", GENERAL_TEMPLATE_ID) or GENERAL_TEMPLATE_ID)
        self._store.enqueue_generation(meeting_id, template_id, priority=QUEUE_PRIORITY_AUTO)
        self._store.flush()
        self.generation_queued.emit(meeting_id)

    def _pump_queue(self) -> None:
        """Start the next runnable job: one at a time, user requests first."""
        self._retry_timer.stop()
        if self._generating_for or self._store.disabled:
            return
        now = now_ms()
        wake = 0
        for job in self._store.generation_queue():
            if job.automatic:
                notes = self._store.get_notes(job.meeting_id)
                if not self.auto_generate_enabled() or (notes is not None and notes.markdown.strip()):
                    # Switched off since, or the user already has notes: never overwrite them.
                    self._store.dequeue_generation(job.meeting_id)
                    continue
            if job.failed or (job.automatic and self.defers_automatic_jobs()):
                continue
            if job.not_before > now:
                wake = min(wake or job.not_before, job.not_before)
                continue
            host = self.remote_host_needing_confirmation()
            problem = (f"文字稿未发送：{host} 尚未确认。可在会议页点「生成纪要」确认后发送" if host
                       else self._start(job.meeting_id, job.template_id))
            if problem is None or not problem:
                return
            self._record_failure(job.meeting_id, problem, transient=False)
        self._store.flush()
        if wake:
            self._retry_timer.start(max(1000, wake - now))

    def _record_failure(self, meeting_id: str, message: str, *, transient: bool) -> None:
        """Automatic jobs retry transient failures after a wait, then stay
        queued as failed for the user; user requests are reported and dropped."""
        job = self._store.queued_generation(meeting_id)
        automatic = job is not None and job.automatic
        if automatic and transient and job.attempts < len(AUTO_RETRY_DELAYS_MS):
            delay = AUTO_RETRY_DELAYS_MS[job.attempts]
            self._store.reschedule_generation(meeting_id, attempts=job.attempts + 1,
                                              not_before=now_ms() + delay, last_error=message)
            self._store.flush()
            log.info("会议 %s 纪要自动生成暂时失败，%d 秒后重试: %s", meeting_id[:8], delay // 1000, message)
            self.generation_queued.emit(meeting_id)
            return
        if automatic:
            self._store.fail_generation(meeting_id, message)
        else:
            self._store.dequeue_generation(meeting_id)
        self._store.flush()
        self.generation_failed.emit(meeting_id, message)

    def _generation_problem(self, record: MeetingRecord | None, template_id: str, model_name: str | None) -> str:
        if record is None:
            return "会议不存在"
        if record.status == STATUS_LIVE:
            return "会议仍在进行，请先结束后再生成"
        api_url, api_key, default_model = minutes_settings(self._config)
        if not polish_settings_complete(api_url, api_key, (model_name or "").strip() or default_model):
            return "未配置纪要模型：请在 设置 → AI 服务 填写接口地址、密钥与模型"
        template = get_template(template_id, self._custom_templates())
        if not transcript_lines(self._store.get_segments(record.id), record.started_at,
                                prefer_polished=not template.is_refinement):
            return "文字稿为空，无法整理原文" if template.is_refinement else "文字稿为空，无法生成纪要"
        return ""

    def _start(self, meeting_id: str, template_id: str, model_name: str | None = None) -> str | None:
        """Dispatch one job to the worker; the reason when it cannot start."""
        record = self._store.get_meeting(meeting_id)
        problem = self._generation_problem(record, template_id, model_name)
        if problem:
            return problem
        api_url, api_key, default_model = minutes_settings(self._config)
        model = (model_name or "").strip() or default_model
        template = get_template(template_id, self._custom_templates())
        segments = self._store.get_segments(meeting_id)
        lines: list[TranscriptLine] = transcript_lines(segments, record.started_at, prefer_polished=not template.is_refinement)
        if not self.auto_name_speakers(meeting_id):
            return "说话人名称保存失败，请重试"
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
            proxy_mode=self._config.get("llm.proxy_mode", "system"),
            proxy_bypass_suffixes=str(self._config.get("llm.proxy_bypass_suffixes", "") or ""),
            template=template,
            context=context,
            lines=lines,
            speaker_names=names,
            duration_ms=record.duration_ms,
            title_fallback=record.display_title,
            output_language=str(self._config.get("meeting.output_language", "auto") or "auto"),
            # The config may lower the per-request cap, never raise it above
            # the generator's ceiling (see docs/meeting-generation-reliability.md).
            chunk_chars=min(
                DEFAULT_CHUNK_CHARS,
                max(64, int(self._config.get("meeting.single_request_char_limit", DEFAULT_CHUNK_CHARS) or DEFAULT_CHUNK_CHARS)),
            ),
            context_window_tokens=int(self._config.get("meeting.context_window_tokens", 32768)),
            max_output_tokens=int(self._config.get("meeting.max_output_tokens", 4096)),
            meta_lines=self._meeting_meta_lines(record),
        )
        fingerprint = request_fingerprint(request)
        request.checkpoints = self._store.generation_checkpoints(meeting_id, fingerprint)
        request.save_checkpoint = lambda key, raw: self._store.save_generation_checkpoint(meeting_id, fingerprint, key, raw)
        self._generating_for = meeting_id
        self.generation_preview_text = ""
        self.generation_progress.emit(meeting_id, "正在整理原文…" if template.is_refinement else "正在生成纪要…")
        # An open notes draft can cancel this run from the progress signal
        # before the worker is asked to start.
        if self._generating_for != meeting_id:
            return None
        self._generator.generate(request)
        return ""

    def cancel_generation(self) -> None:
        if self._generating_for:
            meeting_id = self._generating_for
            self._generating_for = ""
            self.generation_preview_text = ""
            self._generator.cancel()
            self._store.dequeue_generation(meeting_id)
            self._store.flush()
            self.generation_cancelled.emit(meeting_id)
            self._pump_queue()

    def remove_from_queue(self, meeting_id: str) -> None:
        """Cancel this meeting's job, running or waiting."""
        if meeting_id and meeting_id == self._generating_for:
            self.cancel_generation()
            return
        if self._store.queued_generation(meeting_id) is None:
            return
        self._store.dequeue_generation(meeting_id)
        self._store.flush()
        self.generation_cancelled.emit(meeting_id)

    @property
    def is_generating(self) -> bool:
        return bool(self._generating_for) and self._generator.is_running

    def _on_generation_progress(self, text: str) -> None:
        if self._generating_for:
            self.generation_progress.emit(self._generating_for, text)

    def _on_generation_preview(self, text: str) -> None:
        if self._generating_for:
            self.generation_preview_text = text
            self.generation_preview.emit(self._generating_for, text)

    def _on_generation_done(self, result: MinutesResult) -> None:
        meeting_id = self._generating_for
        self._generating_for = ""
        self.generation_preview_text = ""
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
        flushed = self._store.flush()
        saved = self._store.get_notes(meeting_id)
        if not flushed or saved is None or saved.markdown != result.markdown or saved.json_payload != result.json_payload:
            self._record_failure(meeting_id, "纪要保存失败，已完成的分段会保留，请重试", transient=True)
            self._pump_queue()
            return
        self._store.clear_generation_checkpoints(meeting_id)
        self._store.dequeue_generation(meeting_id)
        self._store.flush()
        self.generation_finished.emit(meeting_id)
        self.meeting_changed.emit(meeting_id)
        self._pump_queue()

    def _on_generation_error(self, message: str) -> None:
        meeting_id = self._generating_for
        self._generating_for = ""
        self.generation_preview_text = ""
        if meeting_id:
            self._record_failure(meeting_id, message, transient=self._generator.last_error_transient)
        self._pump_queue()

    @property
    def running_workers(self):
        return self._generator.running_workers

    def shutdown(self, *, wait_ms: int = 2000) -> None:
        """Queued and interrupted jobs stay in the database for the next start."""
        self.abandon_on_exit()
        self._clock.stop()
        self._drain_timer.stop()
        self._retry_timer.stop()
        self._generator.shutdown(wait_ms=wait_ms)


def record_template_id(config: Config, notes: MeetingNotes | None) -> str:
    if notes is not None and notes.template_id:
        return notes.template_id
    return str(config.get("meeting.template_id", GENERAL_TEMPLATE_ID) or GENERAL_TEMPLATE_ID)

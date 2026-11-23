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

import copy
import inspect
import ipaddress
import logging
import json
from pathlib import Path
import re
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Callable

from PyQt6.QtCore import QEvent, QObject, QThread, QTimer, pyqtSignal

from sayink.atomic_io import atomic_write_text
from sayink.config import Config
from sayink.history_store import MEETING_TRIGGER_MODE, HistoryStore, SegmentRecord
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
    _patch_merged_json_payload,
    coerce_speaker_id,
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
    render_transcript,
    speaker_label,
    transcript_lines,
)
from sayink.transcript_import import IMPORT_SOURCE, parse_transcript
from sayink.minutes_render import apply_speaker_names, clear_timeline, render_markdown, replace_names_in_markdown
from sayink.recognition_settings import (
    ADVANCED_RECOGNITION_PATH,
    HOTWORDS_UNSUPPORTED_NOTE,
    RecognitionSettings,
    audio_source,
    aux_model_path,
)
from sayink.speaker_embedding import SpeakerEmbedder, make_embedder
from sayink.meeting_speakers import MeetingSpeakerSession
from sayink.speaker_clustering import bounded_final_assignments
from sayink.speaker_session import SpeakerSession, recluster
from sayink.speaker_naming import infer_speaker_names
from sayink.speech_recognizer import (
    AUX_MODEL_SILERO_VAD,
    AUX_MODEL_SPEAKER,
    AUX_MODEL_SPEAKER_ACTIVITY,
)
from sayink.text_polisher import _url_parts, is_local_endpoint, polish_settings_complete
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
    audio_path: str = ""
    save_audio: bool | None = None

    @classmethod
    def from_config(cls, config: Config) -> "MeetingOptions":
        source = audio_source(config, meeting=True)
        shared = RecognitionSettings.from_config(config, meeting=True, input_source=source)
        return cls(
            input_source=source,
            model_id=str(config.get("meeting.model_id", "") or ""),
            glossary=shared.glossary,
            vad=shared.vad,
            language=shared.language,
            save_audio=bool(config.get("meeting.save_audio", True)),
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
            out.append(HOTWORDS_UNSUPPORTED_NOTE)
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
        RecognitionSettings.from_config(config, meeting=True, input_source=options.input_source),
        model_id=model_id,
        glossary=options.glossary,
        language=options.language,
        vad=options.vad,
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


_WINDOWS_RESERVED_NAMES = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(10)),
    *(f"LPT{i}" for i in range(10)),
    "COM\xb9", "COM\xb2", "COM\xb3",
    "LPT\xb9", "LPT\xb2", "LPT\xb3",
})
_UNSAFE_FILENAME_CHARS_RE = re.compile(
    r'[\\/:*?"<>|\x00-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]+'
)


def safe_filename(title: str, *, fallback: str = "会议纪要") -> str:
    cleaned = _UNSAFE_FILENAME_CHARS_RE.sub(" ", title or "").strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        cleaned = _UNSAFE_FILENAME_CHARS_RE.sub(" ", fallback or "会议纪要").strip(" .") or "会议纪要"
    stem = cleaned.split(".", 1)[0].strip().upper()
    if stem in _WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned[:80].rstrip(" .") or "会议纪要"


def minutes_settings(config: Config) -> tuple[str, str, str]:
    """(api_url, api_key, model_name) for minutes: the polish endpoint, with
    ``meeting.model_name`` overriding the model when set."""
    api_url = str(config.get("llm.api_url", "") or "")
    api_key = str(config.get("llm.api_key", "") or "")
    model_name = str(config.get("meeting.model_name", "") or config.get("llm.model_name", "") or "")
    return api_url, api_key, model_name


def minutes_settings_complete(config: Config) -> bool:
    return polish_settings_complete(*minutes_settings(config))


_DEFAULT_SCHEME_PORTS: dict[str, int] = {"http": 80, "https": 443}


def _effective_port(scheme: str, port: int | None) -> int:
    if port is not None:
        return int(port)
    return _DEFAULT_SCHEME_PORTS.get(scheme.lower(), 443)


def _format_remote_origin(scheme: str, host: str, port: int | None = None) -> str:
    norm_scheme = scheme.lower()
    clean_host = host.strip("[]").lower()
    eff_port = _effective_port(norm_scheme, port)
    bracketed = f"[{clean_host}]" if ":" in clean_host else clean_host
    if eff_port == _DEFAULT_SCHEME_PORTS.get(norm_scheme):
        return f"{norm_scheme}://{bracketed}"
    return f"{norm_scheme}://{bracketed}:{eff_port}"


def _parse_bare_host_port(item: str) -> tuple[str, int | None] | None:
    stripped = item.strip()
    if not stripped:
        return None
    unbracketed = stripped.strip("[]")
    try:
        ipaddress.ip_address(unbracketed)
        return unbracketed.lower(), None
    except ValueError:
        pass
    parsed, raw_host, error = _url_parts(f"https://{stripped}")
    if error or parsed is None:
        return None
    hostname = raw_host.strip("[]").lower()
    if not hostname:
        return None
    return hostname, parsed.port


def _confirmed_remote_origins(entries) -> set[tuple[str, str, int]]:
    origins: set[tuple[str, str, int]] = set()
    for raw in entries or ():
        item = str(raw or "").strip().lower()
        if not item:
            continue
        if "://" in item:
            parsed, raw_host, error = _url_parts(item)
            host = raw_host.strip("[]").lower()
            if not error and parsed is not None and parsed.scheme in {"http", "https"} and host:
                scheme = parsed.scheme.lower()
                origins.add((scheme, host, _effective_port(scheme, parsed.port)))
        else:
            # Backward-compatible with legacy bare-hostname entries, scoped to
            # HTTPS default port (unless an explicit port was included) so
            # downgrading https://host -> http://host or switching ports requires confirmation.
            bare = _parse_bare_host_port(item)
            if bare is not None:
                hostname, explicit_port = bare
                origins.add(("https", hostname, _effective_port("https", explicit_port)))
    return origins


def remote_host_needing_confirmation(config: Config) -> str:
    """``scheme://host[:port]`` when the minutes endpoint is remote and not yet confirmed."""
    api_url, _key, _model = minutes_settings(config)
    if not api_url or is_local_endpoint(api_url):
        return ""
    parsed, raw_host, error = _url_parts(api_url)
    if error or parsed is None:
        return ""
    scheme = (parsed.scheme or "").lower()
    host = raw_host.strip("[]").lower()
    if not host or scheme not in {"http", "https"}:
        return ""
    port = _effective_port(scheme, parsed.port)
    confirmed = _confirmed_remote_origins(config.get("meeting.confirmed_remote_hosts", []))
    return "" if (scheme, host, port) in confirmed else _format_remote_origin(scheme, host, port)


def confirm_remote_host(config: Config, host: str) -> None:
    item = (host or "").strip().lower()
    if not item:
        return
    if "://" in item:
        parsed, raw_host, error = _url_parts(item)
        if error or parsed is None:
            return
        scheme = (parsed.scheme or "").lower()
        hostname = raw_host.strip("[]").lower()
        if scheme not in {"http", "https"} or not hostname:
            return
        port = _effective_port(scheme, parsed.port)
        canonical = _format_remote_origin(scheme, hostname, port)
    else:
        bare = _parse_bare_host_port(item)
        if bare is None:
            return
        hostname, explicit_port = bare
        api_url, _key, _model = minutes_settings(config)
        parsed_api, raw_api_host, api_error = _url_parts(api_url)
        api_host = "" if api_error else raw_api_host.strip("[]").lower()
        api_scheme = "" if (api_error or parsed_api is None) else (parsed_api.scheme or "").lower()
        api_port = None if (api_error or parsed_api is None) else parsed_api.port
        if api_host == hostname and api_scheme in {"http", "https"}:
            scheme = api_scheme
            port = _effective_port(scheme, explicit_port if explicit_port is not None else api_port)
        else:
            scheme = "https"
            port = _effective_port("https", explicit_port)
        canonical = _format_remote_origin(scheme, hostname, port)
    hosts = list(config.get("meeting.confirmed_remote_hosts", []) or [])
    if canonical not in {str(h).strip().lower() for h in hosts}:
        hosts.append(canonical)
        config.set("meeting.confirmed_remote_hosts", hosts)


def runs_on_this_machine(url: str) -> bool:
    """A loopback model shares the CPU / GPU with live speech recognition."""
    _parsed, raw_host, error = _url_parts(url)
    if error:
        return False
    host = raw_host.strip("[]").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


_MAX_CLUSTER_TURNS = 512
_PENDING_SIDECAR_FILENAME = "meeting_pending_state.json"


@dataclass(frozen=True)
class AsrFailureSpan:
    meeting_id: str
    captured_at: int
    duration_ms: int = 0
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "meeting_id": self.meeting_id,
            "captured_at": int(self.captured_at),
            "duration_ms": int(self.duration_ms),
            "reason": str(self.reason or ""),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AsrFailureSpan | None:
        mid = str(raw.get("meeting_id") or "").strip()
        if not mid:
            return None
        try:
            cap = int(raw.get("captured_at") or 0)
            dur = max(0, int(raw.get("duration_ms") or 0))
        except (TypeError, ValueError):
            return None
        return cls(
            meeting_id=mid,
            captured_at=cap,
            duration_ms=dur,
            reason=str(raw.get("reason") or ""),
        )


def format_asr_gap_notice(offset_ms: int) -> str:
    return f"[{format_timestamp(max(0, int(offset_ms)))} 语音片段识别失败]"


def _segment_to_dict(seg: SegmentRecord) -> dict[str, Any]:
    return {
        "session_id": str(seg.session_id),
        "seq": int(seg.seq),
        "created_at": int(seg.created_at),
        "raw_text": str(seg.raw_text or ""),
        "polished_text": str(seg.polished_text or ""),
        "source": str(seg.source or ""),
        "duration_ms": int(seg.duration_ms or 0),
        "target_app": str(seg.target_app or ""),
        "trigger_mode": str(seg.trigger_mode or MEETING_TRIGGER_MODE),
        "model": str(seg.model or ""),
        "speaker_id": int(seg.speaker_id or 0),
        "speaker_route": str(seg.speaker_route or ""),
        "captured_at": int(seg.captured_at or 0),
        "corrected_text": str(seg.corrected_text or ""),
        "speaker_locked": bool(seg.speaker_locked),
    }


def _segment_from_dict(raw: dict[str, Any]) -> SegmentRecord | None:
    sid = str(raw.get("session_id") or "").strip()
    if not sid:
        return None
    try:
        seq = int(raw["seq"])
        created_at = int(raw.get("created_at") or 0)
    except (KeyError, TypeError, ValueError):
        return None
    return SegmentRecord(
        session_id=sid,
        seq=seq,
        created_at=created_at,
        raw_text=str(raw.get("raw_text") or ""),
        polished_text=str(raw.get("polished_text") or ""),
        source=str(raw.get("source") or ""),
        duration_ms=int(raw.get("duration_ms") or 0),
        target_app=str(raw.get("target_app") or ""),
        trigger_mode=str(raw.get("trigger_mode") or MEETING_TRIGGER_MODE),
        model=str(raw.get("model") or ""),
        speaker_id=int(raw.get("speaker_id") or 0),
        speaker_route=str(raw.get("speaker_route") or ""),
        captured_at=int(raw.get("captured_at") or 0),
        corrected_text=str(raw.get("corrected_text") or ""),
        speaker_locked=bool(raw.get("speaker_locked", False)),
    )


@dataclass
class UnsavedFinalize:
    meeting_id: str
    title: str
    started_at: int
    frozen_ended_at: int
    recluster_changes: dict[int, int] = field(default_factory=dict)
    recluster_mapping: dict[int, int] = field(default_factory=dict)
    auto_enqueue: bool = False
    audio_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "meeting_id": self.meeting_id,
            "title": self.title,
            "started_at": int(self.started_at),
            "frozen_ended_at": int(self.frozen_ended_at),
            "recluster_changes": {str(k): int(v) for k, v in self.recluster_changes.items()},
            "recluster_mapping": {str(k): int(v) for k, v in self.recluster_mapping.items()},
            "auto_enqueue": bool(self.auto_enqueue),
            "audio_path": str(self.audio_path or ""),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> UnsavedFinalize | None:
        mid = str(raw.get("meeting_id") or "").strip()
        if not mid:
            return None
        try:
            started = int(raw.get("started_at") or 0)
            ended = int(raw.get("frozen_ended_at") or started)
            changes = {
                int(k): int(v)
                for k, v in (raw.get("recluster_changes") or {}).items()
            }
            mapping = {
                int(k): int(v)
                for k, v in (raw.get("recluster_mapping") or {}).items()
            }
        except (TypeError, ValueError, AttributeError):
            return None
        return cls(
            meeting_id=mid,
            title=str(raw.get("title") or ""),
            started_at=started,
            frozen_ended_at=ended,
            recluster_changes=changes,
            recluster_mapping=mapping,
            auto_enqueue=bool(raw.get("auto_enqueue", False)),
            audio_path=str(raw.get("audio_path") or ""),
        )


@dataclass
class UnsavedMediaImport:
    result: Any
    title: str
    meeting_id: str
    audio_path: str = ""
    options: dict[str, Any] = field(default_factory=dict)

    def _as_tuple(self) -> tuple[Any, str, str, str, dict[str, Any]]:
        return (self.result, self.title, self.meeting_id, self.audio_path, self.options)

    def __len__(self) -> int:
        return 5

    def __getitem__(self, index: int | slice) -> Any:
        return self._as_tuple()[index]

    @classmethod
    def coerce(cls, raw: Any) -> UnsavedMediaImport | None:
        if raw is None:
            return None
        if isinstance(raw, cls):
            return raw
        if isinstance(raw, tuple) and len(raw) >= 3:
            res = raw[0]
            title = str(raw[1] or "")
            mid = str(raw[2] or "")
            apath = str(raw[3] or "") if len(raw) >= 4 else ""
            opts = dict(raw[4]) if len(raw) >= 5 and isinstance(raw[4], dict) else {}
            return cls(result=res, title=title, meeting_id=mid, audio_path=apath, options=opts)
        return None


def _bounded_final_assignments(
    speakers: MeetingSpeakerSession,
    threshold: float,
    *,
    protected_ids: set[int],
    locked_keys: set[int],
) -> dict[int, int]:
    return bounded_final_assignments(
        speakers,
        threshold,
        protected_ids=protected_ids,
        locked_keys=locked_keys,
        sample_budget=_MAX_CLUSTER_TURNS,
    )


def _file_mtime_ms(path: str) -> int | None:
    """Local file modification time in epoch milliseconds, or None when unknown."""
    if not path:
        return None
    try:
        millis = int(Path(path).stat().st_mtime * 1000)
    except OSError:
        return None
    return millis if millis > 0 else None


class MediaCommitError(RuntimeError):
    """Recognition finished, but the meeting row was not stored."""


# A thread that could not be stopped stays here so deleting the controller
# does not destroy it while it is still running.
_ORPHAN_MEDIA_JOBS: list[QThread] = []


class _MediaImportJob(QThread):
    """Read and recognize one file away from the UI thread. The runner is injected."""

    progressed = pyqtSignal(str)
    previewed = pyqtSignal(str)
    timeline = pyqtSignal(object)
    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(
        self,
        path: str,
        runner,
        cancel_event: threading.Event,
        parent=None,
        *,
        options: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(parent)
        self._path = path
        self._runner = runner
        self._cancel = cancel_event
        self._options = dict(options or {})

    def run(self) -> None:
        try:
            kwargs = {}
            try:
                params = inspect.signature(self._runner).parameters
                has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
            except (TypeError, ValueError):
                params = {}
                has_var_kw = False
            if "preview" in params or has_var_kw:
                kwargs["preview"] = self.previewed.emit
            if "timeline" in params or has_var_kw:
                kwargs["timeline"] = self.timeline.emit
            if self._options and ("options" in params or has_var_kw):
                kwargs["options"] = dict(self._options)
            result = self._runner(self._path, self._cancel.is_set, self.progressed.emit, **kwargs)
            self.succeeded.emit(result)
        except Exception as exc:
            log.warning("导入音视频失败: %s", exc)
            self.failed.emit(str(exc))


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
    error = pyqtSignal(str)                     # error message
    finalize_save_failed = pyqtSignal(str, str) # meeting_id, message
    media_import_started = pyqtSignal(str, str) # path, title
    media_import_progress = pyqtSignal(str)
    media_import_preview = pyqtSignal(str)
    media_import_timeline = pyqtSignal(object)
    media_import_finished = pyqtSignal(str)     # meeting id, or "" when nothing was saved
    media_import_failed = pyqtSignal(str)

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
        self._current_lock = threading.RLock()
        self._current: MeetingRecord | None = None
        self._options: MeetingOptions | None = None
        self._capabilities: MeetingCapabilities | None = None
        self._embedder: SpeakerEmbedder | None = None
        self._cached_embedder: tuple[tuple[Any, ...], SpeakerEmbedder] | None = None
        self._speakers: SpeakerSession | None = None
        self._started_wall_ms = 0
        self._paused_ms = 0
        self._pause_started_wall_ms = 0
        self._pause_intervals: list[tuple[int, int]] = []
        self._frozen_elapsed_ms: int | None = None
        self._capture_stopped_wall_ms = 0
        self._paused_detail = ""
        self._unsaved_finalizes: dict[str, UnsavedFinalize] = {}
        self.last_retried_finalize_id = ""
        self._unsaved_segments: dict[str, list[SegmentRecord]] = {}
        self._asr_failures: dict[str, list[AsrFailureSpan]] = {}
        self._unrecovered_db_failures: dict[str, int] = {}
        self._exclude_user_notes_ids: set[str] = set()
        self._last_drain_pending = -1
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
        self._generation_cancel_token: threading.Event | None = None
        self._forced_auto_generate_ids: set[str] = set()
        self.generation_preview_text = ""
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._pump_queue)
        self._history.add_committed_callback(self._on_history_committed_threadsafe)
        if hasattr(self._history, "set_segment_calibrator"):
            self._history.set_segment_calibrator(self._calibrate_segment)
        self._shutting_down = False
        self._importing_media = False
        self._media_cancel = threading.Event()
        self._media_runner = None
        self._external_block: Callable[[], str] | None = None
        self._media_job: _MediaImportJob | None = None
        self._media_queue: list[tuple] = []
        self._media_title = ""
        self._media_source_path = ""
        self._media_options: dict[str, Any] = {}
        self.last_imported_audio_path = ""
        self.last_failed_audio_path = ""
        self._unsaved_media: UnsavedMediaImport | tuple | None = None
        self.media_import_notice = ""
        self.media_queue_notice = ""
        self.transcript_import_notice = ""
        self.srt_skip_note = ""
        self._load_pending_sidecar()

    @property
    def _unsaved_finalize(self) -> UnsavedFinalize | None:
        return next(reversed(self._unsaved_finalizes.values()), None)

    @_unsaved_finalize.setter
    def _unsaved_finalize(self, value: UnsavedFinalize | None) -> None:
        if value is None:
            self._unsaved_finalizes.clear()
        else:
            self._unsaved_finalizes[value.meeting_id] = value
        self._save_pending_sidecar()

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
        with self._current_lock:
            return self._current

    @property
    def current_id(self) -> str:
        with self._current_lock:
            cur = self._current
            return cur.id if cur is not None else ""

    def set_audio_path(self, audio_path: str, meeting_id: str | None = None) -> None:
        """Associate a local recording cache path with the current or specified meeting."""
        target_id = meeting_id or self.current_id
        if not target_id:
            return
        cleaned = str(audio_path or "")
        self._store.update_meeting(target_id, audio_path=cleaned)
        with self._current_lock:
            if self._current is not None and self._current.id == target_id:
                self._current = replace(self._current, audio_path=cleaned)

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

    def _close_open_pause(self, now_wall_ms: int | None = None) -> None:
        with self._current_lock:
            if self._pause_started_wall_ms <= 0:
                return
            end_wall = int(time.time() * 1000) if now_wall_ms is None else int(now_wall_ms)
            if end_wall > self._pause_started_wall_ms:
                self._paused_ms += end_wall - self._pause_started_wall_ms
                self._pause_intervals.append((self._pause_started_wall_ms, end_wall))
            self._pause_started_wall_ms = 0

    def _paused_deduction_at(self, wall_ms: int) -> int:
        with self._current_lock:
            deduct = 0
            for p_start, p_end in self._pause_intervals:
                if wall_ms > p_start:
                    deduct += max(0, min(wall_ms, p_end) - p_start)
            if self._pause_started_wall_ms > 0 and wall_ms > self._pause_started_wall_ms:
                deduct += wall_ms - self._pause_started_wall_ms
            return deduct

    def _calibrate_segment(self, record: SegmentRecord) -> SegmentRecord:
        with self._current_lock:
            cur = self._current
            if cur is None or record.session_id != cur.id:
                return record
            raw_captured = int(record.captured_at or 0)
            if raw_captured < 0:
                return record
            has_pauses = self._paused_intervals_active()
            stop_wall = self._capture_stopped_wall_ms
            if raw_captured > 0 and not has_pauses:
                return record
            if raw_captured == 0 and not has_pauses and stop_wall <= 0:
                return record
            started = self._started_wall_ms or cur.started_at
            if raw_captured > 0:
                stamp = raw_captured
            else:
                raw_end = int(record.created_at)
                if stop_wall > 0:
                    raw_end = min(raw_end, stop_wall)
                stamp = max(started, raw_end - max(0, int(record.duration_ms or 0)))
            deduct = self._paused_deduction_at(stamp) if has_pauses else 0
            if raw_captured > 0 and deduct <= 0:
                return record
            calibrated = max(started, stamp - deduct)
            if calibrated == raw_captured:
                return record
            return replace(record, captured_at=calibrated)

    def _paused_intervals_active(self) -> bool:
        return bool(self._paused_ms > 0 or self._pause_started_wall_ms > 0 or self._pause_intervals)

    def elapsed_ms(self) -> int:
        with self._current_lock:
            if self._frozen_elapsed_ms is not None:
                return self._frozen_elapsed_ms
            if not self._started_wall_ms:
                return 0
            if self._pause_started_wall_ms > 0:
                now_wall = self._pause_started_wall_ms
            else:
                now_wall = int(time.time() * 1000)
            return max(0, now_wall - self._started_wall_ms - self._paused_ms)

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
        if self._importing_media:
            raise RuntimeError("正在导入音视频")
        if self.is_active:
            raise RuntimeError("已有会议在进行中")
        if self._store.disabled:
            # Without the DB every segment write is a silent no-op.
            raise RuntimeError("历史数据库不可用，会议文字稿无法保存")
        self._options = options
        speaker_path = aux_model_path(AUX_MODEL_SPEAKER) if capabilities.speaker_model_available else None
        activity_path = (
            aux_model_path(AUX_MODEL_SPEAKER_ACTIVITY)
            if capabilities.speaker_model_available and capabilities.speaker_activity_enabled
            else None
        )
        embedder_key = (
            make_embedder,
            str(speaker_path or ""),
            bool(capabilities.speaker_model_available),
            bool(capabilities.speaker_activity_enabled),
            str(activity_path or ""),
        )
        if self._cached_embedder is not None and self._cached_embedder[0] == embedder_key:
            self._embedder = self._cached_embedder[1]
        else:
            self._embedder = make_embedder(
                speaker_path,
                enabled=capabilities.speaker_model_available,
                activity_enabled=capabilities.speaker_activity_enabled,
            )
            if (
                not (capabilities.speaker_model_available and self._embedder.name != "campplus")
                and not getattr(self._embedder, "activity_guard_error", "")
            ):
                self._cached_embedder = (embedder_key, self._embedder)
            else:
                self._cached_embedder = None
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
            audio_path=options.audio_path,
        )
        # The row must exist before the first transcript segment references it.
        flushed = self._store.flush(timeout=5.0)
        if not flushed and "update_meeting" in self.__dict__ and "flush" in self._store.__dict__:
            # UI draft tests mock update_meeting + store.flush together while
            # starting a second live meeting against a healthy HistoryStore.
            flushed = self._history.flush(timeout=1.0)
        if not flushed or self._store.get_meeting(record.id) is None:
            self._store.delete_meeting(record.id)
            self._speakers = None
            self._embedder = None
            self._options = None
            self._capabilities = None
            with self._current_lock:
                self._current = None
            raise RuntimeError("会议创建失败：无法写入本机数据库")
        with self._current_lock:
            self._current = record
            self._started_wall_ms = record.started_at
            self._paused_ms = 0
            self._pause_started_wall_ms = 0
            self._pause_intervals = []
            self._frozen_elapsed_ms = None
            self._capture_stopped_wall_ms = 0
        self._paused_detail = ""
        self._set_state(MeetingState.LIVE)
        self._clock.start()
        self._yield_local_model()
        self.meeting_started.emit(record.id)
        self.meeting_changed.emit(record.id)
        return record

    def _cancel_active_generation_worker(self) -> None:
        if self._generation_cancel_token is not None:
            self._generation_cancel_token.set()
            self._generation_cancel_token = None
        self._generator.cancel()

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
        self._cancel_active_generation_worker()
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
        self._clock.stop()
        with self._current_lock:
            if self._pause_started_wall_ms <= 0:
                self._pause_started_wall_ms = int(time.time() * 1000)
        self._paused_detail = detail
        self._set_state(MeetingState.PAUSED)

    def resume(self) -> None:
        if self._state is not MeetingState.PAUSED:
            return
        self._close_open_pause()
        self._paused_detail = ""
        self._set_state(MeetingState.LIVE)
        self._clock.start()

    @property
    def paused_detail(self) -> str:
        return self._paused_detail

    def pending_segment_count(self) -> int:
        try:
            return max(0, int(self._pending_segments() or 0))
        except Exception:
            return 0

    def request_end(self) -> None:
        """Audio capture has stopped; wait for queued segments, then finalize."""
        if self._state not in (MeetingState.LIVE, MeetingState.PAUSED):
            return
        self._clock.stop()
        now_wall = int(time.time() * 1000)
        self._close_open_pause(now_wall)
        with self._current_lock:
            self._capture_stopped_wall_ms = now_wall
            if self._started_wall_ms > 0:
                self._frozen_elapsed_ms = max(0, now_wall - self._started_wall_ms - self._paused_ms)
            else:
                self._frozen_elapsed_ms = 0
        self._last_drain_pending = self.pending_segment_count()
        self._set_state(MeetingState.DRAINING)
        self._drain_timer.start()
        self._poll_drain()

    def _poll_drain(self) -> None:
        if self._state is not MeetingState.DRAINING:
            self._drain_timer.stop()
            return
        rem = self.pending_segment_count()
        if rem > 0:
            if rem != self._last_drain_pending:
                self._last_drain_pending = rem
                self.state_changed.emit(MeetingState.DRAINING)
            return
        self._last_drain_pending = 0
        self._drain_timer.stop()
        self.finalize()

    def _compute_recluster_plan(
        self,
        meeting_id: str,
        segments: list[SegmentRecord],
    ) -> tuple[dict[int, int], dict[int, int]]:
        speakers = self._speakers
        if speakers is None:
            return {}, {}
        embedder = self._embedder
        protected = {s.speaker_id for s in self._store.get_speakers(meeting_id) if s.name_locked or s.display_name}
        locked = {s.seq for s in segments if s.speaker_locked}
        protected.update(s.speaker_id for s in segments if s.speaker_locked)
        if isinstance(speakers, MeetingSpeakerSession) and any(t.key >= 0 for t in speakers.turns):
            threshold = embedder.cluster_threshold if embedder is not None else speakers.threshold
            assignments = _bounded_final_assignments(
                speakers, threshold, protected_ids=protected, locked_keys=locked,
            )
            current = {s.seq: s.speaker_id for s in segments}
            changed = {
                int(seq): int(sid)
                for seq, sid in assignments.items()
                if seq >= 0 and current.get(seq) not in (None, sid)
            }
            return changed, {}
        if speakers.speaker_count < 2:
            return {}, {}
        threshold = embedder.recluster_threshold if embedder is not None else speakers.threshold
        mapping = recluster(speakers.history, threshold=threshold, protected_ids=protected)
        clean_map = {int(old): int(new) for old, new in (mapping or {}).items() if int(old) != int(new)}
        return {}, clean_map

    def _reset_live_runtime(self) -> None:
        self._speakers = None
        self._embedder = None
        self._options = None
        self._capabilities = None
        with self._current_lock:
            self._current = None
            self._started_wall_ms = 0
            self._paused_ms = 0
            self._pause_started_wall_ms = 0
            self._pause_intervals = []
            self._frozen_elapsed_ms = None
            self._capture_stopped_wall_ms = 0
        self._paused_detail = ""

    def _pending_sidecar_path(self) -> Path | None:
        cfg_dir = getattr(self._config, "config_dir", None)
        if cfg_dir is not None:
            return Path(cfg_dir) / _PENDING_SIDECAR_FILENAME
        db_path = getattr(self._history, "db_path", None)
        if isinstance(db_path, (str, Path)):
            return Path(db_path).parent / _PENDING_SIDECAR_FILENAME
        return None

    def _load_pending_sidecar(self) -> None:
        sidecar = self._pending_sidecar_path()
        if sidecar is None or not sidecar.is_file():
            return
        try:
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        if not isinstance(payload, dict):
            return
        for raw_fin in payload.get("unsaved_finalizes") or ():
            if isinstance(raw_fin, dict):
                fin = UnsavedFinalize.from_dict(raw_fin)
                if fin is not None:
                    self._unsaved_finalizes[fin.meeting_id] = fin
        raw_segs = payload.get("unsaved_segments") or {}
        if isinstance(raw_segs, dict):
            for mid, items in raw_segs.items():
                if not mid or not isinstance(items, list):
                    continue
                parsed_segs = [
                    seg for item in items
                    if isinstance(item, dict) and (seg := _segment_from_dict(item)) is not None
                ]
                if parsed_segs:
                    self._unsaved_segments[str(mid)] = parsed_segs
        raw_asr = payload.get("asr_failures") or {}
        if isinstance(raw_asr, dict):
            for mid, items in raw_asr.items():
                if not mid or not isinstance(items, list):
                    continue
                parsed_asr = [
                    span for item in items
                    if isinstance(item, dict) and (span := AsrFailureSpan.from_dict(item)) is not None
                ]
                if parsed_asr:
                    self._asr_failures[str(mid)] = parsed_asr

    def _save_pending_sidecar(self) -> None:
        sidecar = self._pending_sidecar_path()
        if sidecar is None:
            return
        has_any = bool(
            self._unsaved_finalizes
            or any(self._unsaved_segments.values())
            or any(self._asr_failures.values())
        )
        if not has_any:
            try:
                sidecar.unlink(missing_ok=True)
            except OSError:
                pass
            return
        payload = {
            "version": 1,
            "unsaved_finalizes": [fin.to_dict() for fin in self._unsaved_finalizes.values()],
            "unsaved_segments": {
                mid: [_segment_to_dict(s) for s in segs]
                for mid, segs in self._unsaved_segments.items()
                if segs
            },
            "asr_failures": {
                mid: [span.to_dict() for span in spans]
                for mid, spans in self._asr_failures.items()
                if spans
            },
        }
        try:
            sidecar.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(sidecar, json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            log.debug("保存会议待恢复状态侧车失败: %s", exc)

    def _history_session_write_failures(self, meeting_id: str) -> int:
        if not meeting_id:
            return 0
        getter = getattr(self._history, "get_session_write_failures", None)
        if callable(getter):
            try:
                return max(0, int(getter(meeting_id) or 0))
            except Exception:
                return 0
        return 0

    def record_unsaved_segment(
        self,
        meeting_id_or_record: str | SegmentRecord,
        seg_record: SegmentRecord | None = None,
    ) -> None:
        """Stash a failed meeting ``SegmentRecord`` in memory (+ sidecar) for retry."""
        if isinstance(meeting_id_or_record, SegmentRecord) and seg_record is None:
            seg = meeting_id_or_record
            mid = str(seg.session_id or self.current_id or "").strip()
        else:
            mid = str(meeting_id_or_record or "").strip()
            seg = seg_record
        if not mid or seg is None:
            return
        if seg.session_id != mid or seg.trigger_mode != MEETING_TRIGGER_MODE:
            seg = replace(seg, session_id=mid, trigger_mode=MEETING_TRIGGER_MODE)
        if seg.captured_at <= 0 and self.current_id == mid:
            seg = self._calibrate_segment(seg)
        bucket = self._unsaved_segments.setdefault(mid, [])
        replaced = False
        for idx, existing in enumerate(bucket):
            if int(existing.seq) == int(seg.seq):
                bucket[idx] = seg
                replaced = True
                break
        if not replaced:
            bucket.append(seg)
            bucket.sort(key=lambda item: int(item.seq))
        self._save_pending_sidecar()
        if not self._shutting_down:
            try:
                self.meeting_changed.emit(mid)
            except RuntimeError:
                pass

    def unsaved_segments(self, meeting_id: str) -> list[SegmentRecord]:
        return list(self._unsaved_segments.get(meeting_id, ()))

    def has_unsaved_segments(self, meeting_id: str | None = None) -> bool:
        if meeting_id is not None:
            return bool(self._unsaved_segments.get(meeting_id))
        return any(bool(v) for v in self._unsaved_segments.values())

    def record_asr_failure(
        self,
        captured_at: int | str = 0,
        duration_ms: int = 0,
        reason: str = "",
        *,
        meeting_id: str | None = None,
    ) -> bool:
        """Record a meeting ASR decode failure span and surface an explicit gap notice."""
        if isinstance(captured_at, str) and meeting_id is None:
            target_id = captured_at.strip() or self.current_id
            raw_captured = int(duration_ms or 0)
            raw_duration = 0
            clean_reason = str(reason or "").strip()
        else:
            target_id = (meeting_id or "").strip() or self.current_id
            try:
                raw_captured = int(captured_at or 0)
            except (TypeError, ValueError):
                raw_captured = 0
            raw_duration = max(0, int(duration_ms or 0))
            clean_reason = str(reason or "").strip()
        if not target_id:
            return False
        cur = self.current
        record = cur if (cur is not None and cur.id == target_id) else self._store.get_meeting(target_id)
        started_at = (
            self._started_wall_ms
            if (self.current_id == target_id and self._started_wall_ms > 0)
            else (int(record.started_at) if record is not None else 0)
        )
        if raw_captured > 0:
            if self.current_id == target_id and self._paused_intervals_active():
                stamp_ms = max(started_at, raw_captured - self._paused_deduction_at(raw_captured))
            else:
                stamp_ms = raw_captured
        elif self.current_id == target_id and started_at > 0:
            stamp_ms = started_at + self.elapsed_ms()
        else:
            stamp_ms = max(started_at, now_ms())
        span = AsrFailureSpan(
            meeting_id=target_id,
            captured_at=stamp_ms,
            duration_ms=raw_duration,
            reason=clean_reason,
        )
        self._asr_failures.setdefault(target_id, []).append(span)
        self._save_pending_sidecar()
        offset_ms = max(0, stamp_ms - started_at) if started_at > 0 else max(0, stamp_ms)
        gap_text = format_asr_gap_notice(offset_ms)
        if not self._shutting_down:
            try:
                self.notice.emit("语音片段识别失败", gap_text)
                self.meeting_changed.emit(target_id)
            except RuntimeError:
                pass
        return True

    def asr_failures(self, meeting_id: str) -> list[AsrFailureSpan]:
        return list(self._asr_failures.get(meeting_id, ()))

    def asr_gap_lines(self, meeting_id: str, started_at: int) -> list[TranscriptLine]:
        spans = self._asr_failures.get(meeting_id, ())
        if not spans:
            return []
        base = int(started_at or 0)
        out: list[TranscriptLine] = []
        for span in spans:
            offset = (
                max(0, int(span.captured_at) - base)
                if (int(span.captured_at) > 0 and base > 0)
                else max(0, int(span.captured_at))
            )
            out.append(
                TranscriptLine(
                    offset_ms=offset,
                    speaker_id=0,
                    text=format_asr_gap_notice(offset),
                    route="asr_gap",
                    duration_ms=max(0, int(span.duration_ms)),
                    time_known=True,
                )
            )
        return out

    def _merge_lines_with_asr_gaps(
        self,
        meeting_id: str,
        started_at: int,
        lines: list[TranscriptLine],
    ) -> list[TranscriptLine]:
        gaps = self.asr_gap_lines(meeting_id, started_at)
        if not gaps:
            return lines
        tagged: list[tuple[int, int, int, TranscriptLine]] = [
            (int(line.offset_ms), 0, idx, line) for idx, line in enumerate(lines)
        ]
        for idx, gap in enumerate(gaps):
            tagged.append((int(gap.offset_ms), 1, idx, gap))
        tagged.sort(key=lambda item: (item[0], item[1], item[2]))
        return [item[3] for item in tagged]

    def segment_integrity_counts(
        self,
        meeting_id: str,
        *,
        saved_segments: list[SegmentRecord] | None = None,
    ) -> tuple[int, int, int]:
        """Return ``(saved_count, asr_failed_count, db_failed_count)`` for ``meeting_id``."""
        if not meeting_id:
            return (0, 0, 0)
        segs = saved_segments if saved_segments is not None else self._store.get_segments(meeting_id)
        saved_seqs = {int(s.seq) for s in segs}
        saved_count = len(segs)
        asr_failed_count = len(self._asr_failures.get(meeting_id, ()))
        pending_unsaved = [
            s for s in self._unsaved_segments.get(meeting_id, ()) if int(s.seq) not in saved_seqs
        ]
        hist_fail = self._history_session_write_failures(meeting_id)
        unrec_fail = int(self._unrecovered_db_failures.get(meeting_id, 0) or 0)
        db_failed_count = max(len(pending_unsaved), hist_fail, unrec_fail)
        return (saved_count, asr_failed_count, db_failed_count)

    def segment_integrity_banner_text(
        self,
        meeting_id: str,
        *,
        has_saved_audio: bool | None = None,
        saved_segments: list[SegmentRecord] | None = None,
    ) -> str:
        saved_count, asr_failed, db_failed = self.segment_integrity_counts(
            meeting_id, saved_segments=saved_segments
        )
        if asr_failed <= 0 and db_failed <= 0:
            return ""
        base = f"已保存 {saved_count} 段、识别失败 {asr_failed} 段、保存失败 {db_failed} 段"
        if has_saved_audio is None:
            rec = self._store.get_meeting(meeting_id)
            ap = str(getattr(rec, "audio_path", "") or "").strip() if rec is not None else ""
            has_saved_audio = bool(
                ap
                and (Path(ap).is_file() or (Path(ap).is_dir() and (Path(ap) / "audio.f32").is_file()))
            )
        details: list[str] = []
        if asr_failed > 0 and not has_saved_audio:
            details.append("未保存会议录音，识别失败处无本地音频可补回")
        if db_failed > 0 and self.has_unsaved_segments(meeting_id):
            details.append("可点击「重试保存缺失片段」补写数据库")
        return f"{base}（{'；'.join(details)}）" if details else base

    def retry_unsaved_segments(self, meeting_id: str | None = None) -> tuple[bool, str]:
        """Idempotently persist missing ``(session_id, seq)`` rows without duplicates."""
        target_id = (meeting_id or "").strip() or next(iter(self._unsaved_segments.keys()), "")
        if not target_id:
            return (True, "")
        pending = list(self._unsaved_segments.get(target_id, ()))
        existing_seqs = {int(s.seq) for s in self._store.get_segments(target_id)}
        missing = [seg for seg in pending if int(seg.seq) not in existing_seqs]
        if not missing:
            self._unsaved_segments.pop(target_id, None)
            self._unrecovered_db_failures.pop(target_id, None)
            clear_fn = getattr(self._history, "clear_session_write_failures", None)
            if callable(clear_fn):
                clear_fn(target_id)
            self._save_pending_sidecar()
            self.meeting_changed.emit(target_id)
            return (True, "缺失片段已全部在数据库中")
        if self._store.disabled or getattr(self._history, "disabled", False):
            err = "历史数据库不可用，无法保存缺失片段"
            self.error.emit(err)
            return (False, err)

        def _insert_missing(conn) -> None:
            for seg in missing:
                conn.execute(
                    """
                    INSERT INTO history (
                      session_id, seq, created_at, raw_text, polished_text,
                      source, duration_ms, target_app, trigger_mode, model,
                      speaker_id, speaker_route, captured_at, corrected_text, speaker_locked
                    )
                    SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    WHERE NOT EXISTS (
                      SELECT 1 FROM history WHERE session_id = ? AND seq = ? AND trigger_mode = ?
                    )
                    """,
                    (
                        target_id,
                        int(seg.seq),
                        int(seg.created_at),
                        str(seg.raw_text or ""),
                        str(seg.polished_text or ""),
                        str(seg.source or ""),
                        int(seg.duration_ms or 0),
                        str(seg.target_app or ""),
                        MEETING_TRIGGER_MODE,
                        str(seg.model or ""),
                        int(seg.speaker_id or 0),
                        str(seg.speaker_route or ""),
                        int(seg.captured_at or 0),
                        str(seg.corrected_text or ""),
                        1 if seg.speaker_locked else 0,
                        target_id,
                        int(seg.seq),
                        MEETING_TRIGGER_MODE,
                    ),
                )

        ticket = self._history.enqueue_write(_insert_missing)
        flushed = ticket > 0 and self._store.flush_ticket(ticket, timeout=5.0)
        after_segments = self._store.get_segments(target_id) if flushed else []
        after_seqs = {int(s.seq) for s in after_segments}
        still_missing = [seg for seg in missing if int(seg.seq) not in after_seqs]
        if not flushed or still_missing:
            self._unsaved_segments[target_id] = still_missing if flushed else missing
            self._save_pending_sidecar()
            err = f"仍有 {len(self._unsaved_segments[target_id])} 段未能写入数据库，请检查磁盘空间后重试"
            self.error.emit(err)
            return (False, err)

        self._unsaved_segments.pop(target_id, None)
        self._unrecovered_db_failures.pop(target_id, None)
        clear_fn = getattr(self._history, "clear_session_write_failures", None)
        if callable(clear_fn):
            clear_fn(target_id)
        self._save_pending_sidecar()
        record = self._store.get_meeting(target_id)
        if record is not None and record.status in (STATUS_ENDED, STATUS_INTERRUPTED):
            max_end = max(
                [
                    int(record.ended_at or record.started_at),
                    *(
                        int(s.captured_at) + max(0, int(s.duration_ms or 0))
                        for s in after_segments
                        if int(s.captured_at or 0) > 0
                    ),
                ]
            )
            if max_end > int(record.ended_at or 0):
                end_t = self._store.end_meeting(
                    target_id, ended_at=max_end, status=record.status, audio_path=record.audio_path
                )
                self._store.flush_ticket(end_t, timeout=2.0)
                record = self._store.get_meeting(target_id) or record
            self.auto_name_speakers(target_id, _emit=False, record=record, segments=after_segments)
            notes = self._store.get_notes(target_id)
            if (
                record.status == STATUS_ENDED
                and (notes is None or not notes.markdown.strip())
                and self._store.queued_generation(target_id) is None
                and not self._shutting_down
            ):
                self._enqueue_automatic(target_id, record=record, segments=after_segments)
                self._pump_queue()
        self.meeting_changed.emit(target_id)
        return (True, f"已补存 {len(missing)} 段缺失文字稿")

    def finalize(self) -> None:
        """Mark the meeting ended, re-cluster speakers, drop the embeddings."""
        cur = self.current
        if cur is None or self._state is MeetingState.ENDED:
            return
        meeting_id = cur.id
        started_at = self._started_wall_ms or cur.started_at
        self._clock.stop()
        self._drain_timer.stop()
        now_wall = int(time.time() * 1000)
        self._close_open_pause(now_wall)
        active_end = max(started_at, started_at + self.elapsed_ms())
        pre_fail = self._history_session_write_failures(meeting_id)
        flush_ok = True
        if not self._store.disabled:
            flush_ok = bool(self._history.flush(timeout=2.0))
        post_fail = self._history_session_write_failures(meeting_id)
        segments = self._store.get_segments(meeting_id)
        saved_seqs = {int(s.seq) for s in segments}
        if meeting_id in self._unsaved_segments:
            rem_segs = [
                seg for seg in self._unsaved_segments[meeting_id] if int(seg.seq) not in saved_seqs
            ]
            if rem_segs:
                self._unsaved_segments[meeting_id] = rem_segs
            else:
                self._unsaved_segments.pop(meeting_id, None)
        unsaved_seg_count = len(self._unsaved_segments.get(meeting_id, ()))
        observed_db_fail = max(pre_fail, post_fail, 0 if flush_ok else 1)
        if observed_db_fail > unsaved_seg_count:
            self._unrecovered_db_failures[meeting_id] = observed_db_fail
        calibrated_ends = [
            s.captured_at + max(0, int(s.duration_ms or 0))
            for s in segments
            if s.trigger_mode == "meeting" and s.captured_at > 0
        ]
        ended_at = max([active_end, *calibrated_ends])
        has_custom_recluster = "_apply_recluster" in self.__dict__
        if has_custom_recluster:
            recluster_changes, recluster_mapping = {}, {}
        else:
            recluster_changes, recluster_mapping = self._compute_recluster_plan(meeting_id, segments)

        end_ticket = self._store.end_meeting(
            meeting_id,
            ended_at=ended_at,
            status=STATUS_ENDED,
            audio_path=cur.audio_path,
        )
        recluster_ticket = 0
        custom_reclustered: Any = False
        if has_custom_recluster:
            self._recluster_segments_cache = segments
            try:
                custom_reclustered = self._apply_recluster(meeting_id)
            finally:
                self._recluster_segments_cache = None
        elif recluster_changes:
            log.info("会议 %s 说话人重新归属 %d 段", meeting_id[:8], len(recluster_changes))
            recluster_ticket = self._store.set_segment_speakers(meeting_id, recluster_changes)
        elif recluster_mapping:
            log.info("会议 %s 说话人重聚类: %s", meeting_id[:8], recluster_mapping)
            recluster_ticket = self._store.relabel_segments(meeting_id, recluster_mapping)

        end_ok = self._store.flush_ticket(end_ticket, timeout=2.0)
        if has_custom_recluster and custom_reclustered is not False:
            recluster_ok = self._store.flush(timeout=2.0)
        elif recluster_ticket:
            recluster_ok = self._store.flush_ticket(recluster_ticket, timeout=2.0)
        else:
            recluster_ok = True
        stored_record = self._store.get_meeting(meeting_id) if end_ok else None
        if (
            not isinstance(self._history, HistoryStore)
            and end_ok
            and recluster_ok
            and stored_record is not None
            and not isinstance(stored_record, MeetingRecord)
        ):
            stored_record = replace(cur, ended_at=ended_at, status=STATUS_ENDED)
        persisted = (
            end_ok
            and recluster_ok
            and isinstance(stored_record, MeetingRecord)
            and stored_record.status == STATUS_ENDED
        )

        self._speakers = None
        self._embedder = None

        if not persisted:
            self._unsaved_finalizes[meeting_id] = UnsavedFinalize(
                meeting_id=meeting_id,
                title=(cur.title or "").strip() or cur.display_title,
                started_at=started_at,
                frozen_ended_at=ended_at,
                recluster_changes=dict(recluster_changes),
                recluster_mapping=dict(recluster_mapping),
                auto_enqueue=self.auto_generate_enabled(),
                audio_path=str(cur.audio_path or ""),
            )
            self._save_pending_sidecar()
            # Release audio/tray/live-page locks via ENDED -> IDLE without claiming DB success.
            self._set_state(MeetingState.ENDED)
            self._reset_live_runtime()
            self._set_state(MeetingState.IDLE)
            err_msg = "会议结束状态未能写入数据库，可点击「重试保存结束状态」。"
            self.finalize_save_failed.emit(meeting_id, err_msg)
            self.error.emit(err_msg)
            self.meeting_changed.emit(meeting_id)
            return

        assert stored_record is not None
        self._unsaved_finalizes.pop(meeting_id, None)
        self._save_pending_sidecar()
        if recluster_changes or recluster_mapping or (has_custom_recluster and custom_reclustered is not False):
            segments = self._store.get_segments(meeting_id)
        self.auto_name_speakers(meeting_id, _emit=False, record=stored_record, segments=segments)
        if not self.has_unsaved_segments(meeting_id):
            self._enqueue_automatic(meeting_id, record=stored_record, segments=segments)
        integrity_msg = self.segment_integrity_banner_text(meeting_id, saved_segments=segments)
        if integrity_msg:
            self.notice.emit("会议记录存在缺失片段", integrity_msg)
        with self._current_lock:
            self._current = stored_record
        self._set_state(MeetingState.ENDED)
        self.meeting_ended.emit(meeting_id)
        self.meeting_changed.emit(meeting_id)
        # Back to idle for the next meeting; the page keeps showing this one.
        self._reset_live_runtime()
        self._set_state(MeetingState.IDLE)
        self._pump_queue()

    def has_unsaved_finalize(self, meeting_id: str | None = None) -> bool:
        if meeting_id is not None:
            return meeting_id in self._unsaved_finalizes
        return bool(self._unsaved_finalizes)

    def unsaved_finalize_ids(self) -> tuple[str, ...]:
        return tuple(self._unsaved_finalizes.keys())

    def default_unsaved_finalize_id(self) -> str:
        return next(reversed(self._unsaved_finalizes.keys()), "")

    def unsaved_finalize_title(self, meeting_id: str | None = None) -> str:
        if meeting_id:
            pending = self._unsaved_finalizes.get(meeting_id)
        else:
            pending = next(reversed(self._unsaved_finalizes.values()), None)
        if pending is None:
            return ""
        return pending.title

    def retry_finalize_save(self, meeting_id: str | None = None) -> str | None:
        target_id = (meeting_id or "").strip() or self.default_unsaved_finalize_id()
        pending = self._unsaved_finalizes.get(target_id) if target_id else None
        if pending is None:
            return "没有可重试保存的会议结束状态"
        mid = pending.meeting_id
        if self.has_unsaved_segments(mid):
            seg_ok, seg_msg = self.retry_unsaved_segments(mid)
            if not seg_ok:
                return seg_msg
        end_ticket = self._store.end_meeting(
            mid,
            ended_at=pending.frozen_ended_at,
            status=STATUS_ENDED,
            audio_path=pending.audio_path,
        )
        recluster_ticket = 0
        if pending.recluster_changes:
            recluster_ticket = self._store.set_segment_speakers(mid, pending.recluster_changes)
        elif pending.recluster_mapping:
            recluster_ticket = self._store.relabel_segments(mid, pending.recluster_mapping)

        end_ok = self._store.flush_ticket(end_ticket, timeout=5.0)
        recluster_ok = self._store.flush_ticket(recluster_ticket, timeout=5.0) if recluster_ticket else True
        stored_record = self._store.get_meeting(mid) if end_ok else None
        if not end_ok or not recluster_ok or stored_record is None or stored_record.status != STATUS_ENDED:
            err_msg = "会议结束状态保存失败，请检查磁盘空间后重试"
            self.finalize_save_failed.emit(mid, err_msg)
            self.error.emit(err_msg)
            return err_msg

        self._unsaved_finalizes.pop(mid, None)
        self.last_retried_finalize_id = mid
        self._save_pending_sidecar()
        segments = self._store.get_segments(mid)
        self.auto_name_speakers(mid, _emit=False, record=stored_record, segments=segments)
        if pending.auto_enqueue and not self._shutting_down and not self.has_unsaved_segments(mid):
            self._enqueue_automatic(mid, record=stored_record, segments=segments)
        self.meeting_ended.emit(mid)
        self.meeting_changed.emit(mid)
        if not self._shutting_down:
            self._pump_queue()
        return None

    def discard_unsaved_finalize(self, meeting_id: str | None = None) -> bool:
        target_id = (meeting_id or "").strip() or self.default_unsaved_finalize_id()
        pending = self._unsaved_finalizes.pop(target_id, None) if target_id else None
        if pending is None:
            return False
        self._save_pending_sidecar()
        self.meeting_changed.emit(pending.meeting_id)
        return True

    def _apply_recluster(self, meeting_id: str) -> bool:
        """End-of-meeting pass: cluster every turn and re-assign each segment."""
        cached_segs = getattr(self, "_recluster_segments_cache", None)
        segments = cached_segs if cached_segs is not None else self._store.get_segments(meeting_id)
        changed, mapping = self._compute_recluster_plan(meeting_id, segments)
        if changed:
            log.info("会议 %s 说话人重新归属 %d 段", meeting_id[:8], len(changed))
            self._store.set_segment_speakers(meeting_id, changed)
            return True
        if mapping:
            log.info("会议 %s 说话人重聚类: %s", meeting_id[:8], mapping)
            self._store.relabel_segments(meeting_id, mapping)
            return True
        return False

    def abandon_on_exit(self) -> None:
        """App is quitting mid-meeting: close the record without waiting."""
        cur = self.current
        if cur is None or not self.is_active:
            return
        meeting_id = cur.id
        started_at = self._started_wall_ms or cur.started_at
        self._clock.stop()
        self._drain_timer.stop()
        now_wall = int(time.time() * 1000)
        self._close_open_pause(now_wall)
        active_end = max(started_at, started_at + self.elapsed_ms())
        self._store.flush(timeout=1.0)
        segments = self._store.get_segments(meeting_id)
        calibrated_ends = [
            s.captured_at + max(0, int(s.duration_ms or 0))
            for s in segments
            if s.trigger_mode == "meeting" and s.captured_at > 0
        ]
        ended_at = max([active_end, *calibrated_ends])
        if self._speakers is not None and len(getattr(self._speakers, "turns", ())) <= 600:
            self._recluster_segments_cache = segments
            try:
                self._apply_recluster(meeting_id)
            except Exception as exc:
                log.debug("退出时重聚类跳过: %s", exc)
            finally:
                self._recluster_segments_cache = None
        # The exit dialog tells the user this meeting is「已中断」, not a normal end.
        self._store.end_meeting(meeting_id, ended_at=ended_at, status=STATUS_INTERRUPTED, audio_path=cur.audio_path)
        self._store.flush(timeout=2.0)
        try:
            self.auto_name_speakers(meeting_id, _emit=False)
        except Exception as exc:
            log.debug("退出时说话人自动命名跳过: %s", exc)
        self._reset_live_runtime()
        self._set_state(MeetingState.IDLE)

    def mark_interrupted(self) -> None:
        protected = tuple(self._unsaved_finalizes.keys())
        if not protected or not hasattr(self._history, "enqueue_write") or "mark_interrupted_meetings" in self._store.__dict__:
            self._store.mark_interrupted_meetings()
            return
        placeholders = ",".join("?" for _ in protected)

        def _fix_excluding_pending_finalizes(conn) -> None:
            conn.execute(
                f"""
                UPDATE meetings
                SET status = ?,
                    ended_at = MAX(
                        started_at,
                        COALESCE(
                            ended_at,
                            (
                                SELECT MAX(
                                    CASE
                                        WHEN captured_at > 0 THEN captured_at + CASE WHEN duration_ms > 0 THEN duration_ms ELSE 0 END
                                        ELSE created_at
                                    END
                                )
                                FROM history
                                WHERE history.session_id = meetings.id AND history.trigger_mode = ?
                            ),
                            started_at
                        )
                    )
                WHERE status = ? AND id NOT IN ({placeholders})
                """,
                (STATUS_INTERRUPTED, MEETING_TRIGGER_MODE, STATUS_LIVE, *protected),
            )

        self._history.enqueue_write(_fix_excluding_pending_finalizes)

    def _tick(self) -> None:
        self.clock_tick.emit(self.elapsed_text())

    def _on_history_committed_threadsafe(self) -> None:
        # Called on the writer thread; hop to the GUI thread via the signal.
        if self._shutting_down:
            return
        meeting_id = self.current_id
        if meeting_id:
            try:
                self.meeting_changed.emit(meeting_id)
            except RuntimeError:
                pass

    # -- live edits -----------------------------------------------------------

    @staticmethod
    def _replace_speaker_map_in_markdown(markdown: str, pairs: list[tuple[str, str]]) -> str:
        """Apply multiple ``(old_label, new_label)`` replacements via two-pass
        sentinels so simultaneous swaps (e.g. ``张三 <-> 李四``) never collide."""
        active = [(old, new) for old, new in pairs if old and new and old != new]
        if not active:
            return markdown
        nonce = uuid.uuid4().hex
        staged: list[tuple[str, str]] = []
        out = markdown
        for idx, (old, new) in enumerate(active):
            sentinel = f"\x00__SAYINK_SPK_{nonce}_{idx}__\x00"
            out, count = replace_names_in_markdown(out, old, sentinel)
            if count:
                staged.append((sentinel, new))
        for sentinel, new in staged:
            out = out.replace(sentinel, new)
        return out

    def add_mark(self, note: str = "") -> int | None:
        cur = self.current
        if cur is None or not self.is_active:
            return None
        offset = self.elapsed_ms()
        cleaned = note.strip()
        ticket = self._store.add_mark(cur.id, offset, cleaned)
        if not self._store.flush_ticket(ticket, timeout=2.0):
            err_msg = "标记保存失败，请检查磁盘空间后重试"
            self.error.emit(err_msg)
            self.notice.emit("标记保存失败", "请检查磁盘空间后重试")
            return None
        marks = self._store.get_marks(cur.id)
        if not any(m.offset_ms == offset and m.note == cleaned for m in marks):
            err_msg = "标记保存失败，请检查磁盘空间后重试"
            self.error.emit(err_msg)
            self.notice.emit("标记保存失败", "请检查磁盘空间后重试")
            return None
        self.notice.emit("已标记", format_timestamp(offset))
        self.meeting_changed.emit(cur.id)
        return offset

    def update_meeting(self, meeting_id: str, **fields: str) -> bool:
        valid = {
            k: v
            for k, v in fields.items()
            if k in ("title", "participants", "agenda", "notes_user", "audio_path") and v is not None
        }
        if not valid:
            return True

        def _apply_to_current() -> None:
            with self._current_lock:
                if self._current is not None and self._current.id == meeting_id:
                    self._current = replace(self._current, **valid)

        tracked_async = getattr(getattr(self._history, "enqueue_write", None), "__name__", "") == "_tracked_enqueue"
        if tracked_async:
            ticket = self._store.update_meeting(meeting_id, after_commit=_apply_to_current, **valid)
            if ticket <= 0:
                self.error.emit("会议信息保存失败，请检查磁盘空间后重试")
                return False
            self.meeting_changed.emit(meeting_id)
            return True

        ticket = self._store.update_meeting(meeting_id, **valid)
        if ticket <= 0 or not self._store.flush_ticket(ticket, timeout=2.0):
            self.error.emit("会议信息保存失败，请检查磁盘空间后重试")
            return False
        saved = self._store.get_meeting(meeting_id)
        if saved is None or any(getattr(saved, k) != v for k, v in valid.items()):
            self.error.emit("会议信息保存失败，请检查磁盘空间后重试")
            return False
        _apply_to_current()
        self.meeting_changed.emit(meeting_id)
        return True

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
        if not self._store.has_speaker(meeting_id, speaker_id):
            return None
        name = (name or "").strip()
        existing = {s.speaker_id: s for s in self._store.get_speakers(meeting_id)}
        old_label = existing[speaker_id].label if speaker_id in existing else speaker_label(speaker_id)
        notes = self._store.get_notes(meeting_id)
        has_notes = notes is not None and bool(notes.markdown)
        needs_segments = record.status != STATUS_LIVE or (
            has_notes and not notes.user_edited and bool(notes.json_payload)
        )
        segments = self._store.get_segments(meeting_id) if needs_segments else None

        manual = {
            s.speaker_id: s.display_name
            for s in existing.values()
            if s.speaker_id != speaker_id and (s.display_name or s.name_locked)
        }
        manual[speaker_id] = name
        previous = {
            s.speaker_id: s.suggested_name
            for s in existing.values()
            if s.speaker_id != speaker_id and s.suggested_name and not s.display_name and not s.name_locked
        }
        inferred = previous
        if record.status != STATUS_LIVE and segments is not None:
            inferred = infer_speaker_names(
                transcript_lines(segments, record.started_at, prefer_polished=False),
                manual_names=manual,
            )
            names_after = {**inferred, **{sid: label for sid, label in manual.items() if label}}
        else:
            names_after = {
                s.speaker_id: s.label
                for s in existing.values()
                if s.speaker_id != speaker_id and (s.display_name or (s.suggested_name and not s.name_locked))
            }
            if name:
                names_after[speaker_id] = name

        self._store.set_speaker(meeting_id, speaker_id, display_name=name)
        replaced = 0
        inferred_markdown: str | None = None
        if has_notes and notes is not None:
            new_label = name or speaker_label(speaker_id)
            patched_user_md: str | None = None
            if not notes.user_edited and notes.user_markdown:
                cand_user_md, user_rep = replace_names_in_markdown(notes.user_markdown, old_label, new_label)
                if user_rep:
                    patched_user_md = cand_user_md
            markdown = None
            payload_json = notes.json_payload
            if not notes.user_edited and notes.json_payload:
                payload = notes.payload()
                payload.setdefault("_speaker_names", {str(sid): row.label for sid, row in existing.items()})
                payload_json = json.dumps(payload, ensure_ascii=False)
                markdown = self._render_notes(
                    meeting_id,
                    replace(notes, json_payload=payload_json),
                    names=names_after,
                    record=record,
                    segments=segments,
                )
            if markdown is not None:
                self._save_rendered_notes(
                    meeting_id, notes, markdown, json_payload=payload_json, user_markdown=patched_user_md,
                )
            else:
                # Hand-edited notes, Markdown-only notes, or a document whose
                # template no longer exists: patch the names in the text.
                new_md, replaced = replace_names_in_markdown(notes.markdown, old_label, new_label)
                if replaced and notes.user_edited:
                    self._store.save_user_markdown(meeting_id, new_md, keep_backup=True)
                elif replaced or patched_user_md is not None:
                    self._save_rendered_notes(
                        meeting_id, notes, new_md if replaced else notes.markdown,
                        json_payload=notes.json_payload, user_markdown=patched_user_md,
                    )
                if record.status != STATUS_LIVE and inferred != previous:
                    inferred_pairs = [
                        (previous.get(sid) or speaker_label(sid), names_after.get(sid) or speaker_label(sid))
                        for sid in sorted(previous.keys() | inferred.keys())
                    ]
                    inferred_markdown = self._replace_speaker_map_in_markdown(
                        new_md if replaced else notes.markdown, inferred_pairs
                    )
        if record.status != STATUS_LIVE and inferred != previous:
            self._store.save_inferred_names(meeting_id, inferred, markdown=inferred_markdown)
        if not self._store.flush():
            return None
        saved = next((s for s in self._store.get_speakers(meeting_id) if s.speaker_id == speaker_id), None)
        if name == "" and saved is None and not self._store.speaker_has_segments(meeting_id, speaker_id):
            self.meeting_changed.emit(meeting_id)
            return replaced
        if saved is None or saved.display_name != name or not saved.name_locked:
            return None
        self.meeting_changed.emit(meeting_id)
        return replaced

    def merge_speakers(
        self,
        meeting_id: str,
        source_speaker_id: int | str,
        target_speaker_id: int | str,
    ) -> MeetingRecord | None:
        """Atomically merge ``source_speaker_id`` into ``target_speaker_id``."""
        src = coerce_speaker_id(source_speaker_id)
        dst = coerce_speaker_id(target_speaker_id)
        if not meeting_id or src <= 0 or dst <= 0 or src == dst or self._generating_for == meeting_id:
            return None
        record = self._store.get_meeting(meeting_id)
        if record is None or record.status not in (STATUS_LIVE, STATUS_ENDED, STATUS_INTERRUPTED):
            return None
        live = record.status == STATUS_LIVE
        if live and (self.current_id != meeting_id or self._state is MeetingState.DRAINING):
            return None
        if not self._store.has_speaker(meeting_id, src):
            return None

        existing = {s.speaker_id: s for s in self._store.get_speakers(meeting_id)}
        src_row = existing.get(src)
        dst_row = existing.get(dst)
        old_src_label = src_row.label if src_row is not None else speaker_label(src)
        old_dst_label = dst_row.label if dst_row is not None else speaker_label(dst)

        if dst_row is not None and (dst_row.display_name or dst_row.name_locked):
            target_display: str | None = dst_row.display_name
        elif src_row is not None and src_row.display_name:
            target_display = src_row.display_name
        elif dst_row is not None and dst_row.suggested_name:
            target_display = dst_row.suggested_name
        else:
            target_display = None

        segments = self._store.get_segments(meeting_id)
        moved_seqs = [seg.seq for seg in segments if int(seg.speaker_id) == src]
        merged_segments = [
            replace(seg, speaker_id=dst, speaker_locked=True) if int(seg.speaker_id) == src else seg
            for seg in segments
        ]

        manual = {
            s.speaker_id: s.display_name
            for s in existing.values()
            if s.speaker_id not in (src, dst) and (s.display_name or s.name_locked)
        }
        if target_display is not None:
            manual[dst] = target_display

        previous_inferred = {
            s.speaker_id: s.suggested_name
            for s in existing.values()
            if s.speaker_id not in (src, dst) and s.suggested_name and not s.display_name and not s.name_locked
        }
        inferred: dict[int, str] | None = None
        if not live:
            inferred = infer_speaker_names(
                transcript_lines(merged_segments, record.started_at, prefer_polished=False),
                manual_names=manual,
            )
            names_after = {**inferred, **{sid: label for sid, label in manual.items() if label}}
        else:
            names_after = {
                s.speaker_id: s.label
                for s in existing.values()
                if s.speaker_id not in (src, dst) and (s.display_name or (s.suggested_name and not s.name_locked))
            }
            if target_display:
                names_after[dst] = target_display
            elif src_row is not None and src_row.suggested_name and not src_row.name_locked:
                names_after[dst] = src_row.suggested_name

        new_dst_label = names_after.get(dst) or speaker_label(dst)
        notes = self._store.get_notes(meeting_id)
        notes_mode = "auto"
        notes_md: str | None = None
        notes_json: str | None = None
        notes_user_md: str | None = None

        if notes is not None and (notes.markdown or notes.json_payload or notes.user_markdown):
            pairs = [
                (old_src_label, new_dst_label),
                (speaker_label(src), new_dst_label),
                (old_dst_label, new_dst_label),
            ]
            if not live and inferred is not None and inferred != previous_inferred:
                for sid in sorted(previous_inferred.keys() | inferred.keys()):
                    if sid in (src, dst):
                        continue
                    pairs.append(
                        (previous_inferred.get(sid) or speaker_label(sid), names_after.get(sid) or speaker_label(sid))
                    )
            if notes.user_markdown:
                notes_user_md = self._replace_speaker_map_in_markdown(notes.user_markdown, pairs)
            if notes.json_payload:
                seed_payload = notes.payload()
                seed_payload.setdefault("_speaker_names", {str(sid): row.label for sid, row in existing.items()})
                notes_json = _patch_merged_json_payload(
                    json.dumps(seed_payload, ensure_ascii=False),
                    source_sid=src,
                    target_sid=dst,
                    old_source_label=old_src_label,
                    new_target_label=new_dst_label,
                )
            if not notes.user_edited and notes_json:
                rendered = self._render_notes(
                    meeting_id,
                    replace(notes, json_payload=notes_json),
                    names=names_after,
                    record=record,
                    segments=merged_segments,
                )
                if rendered is not None:
                    notes_mode = "rendered"
                    notes_md = rendered
            if notes_md is None and notes.markdown:
                patched_md = self._replace_speaker_map_in_markdown(notes.markdown, pairs)
                notes_mode = "user_edited" if notes.user_edited else "rendered"
                notes_md = patched_md
                if notes.user_edited:
                    notes_user_md = patched_md

        ok = self._store.merge_speakers(
            meeting_id,
            src,
            dst,
            target_display_name=target_display,
            inferred_names=inferred,
            notes_markdown=notes_md,
            notes_json_payload=notes_json,
            notes_user_markdown=notes_user_md,
            notes_mode=notes_mode,
        )
        if not ok:
            self.error.emit("说话人合并保存失败，请重试")
            return None

        if live and isinstance(self._speakers, MeetingSpeakerSession):
            for seq in moved_seqs:
                self._speakers.confirm(seq, dst)

        updated = self._store.get_meeting(meeting_id)
        if updated is None:
            return None
        self.meeting_changed.emit(meeting_id)
        return updated

    def speaker_names(self, meeting_id: str) -> dict[int, str]:
        return {
            s.speaker_id: s.label for s in self._store.get_speakers(meeting_id)
            if s.display_name or (s.suggested_name and not s.name_locked)
        }

    def auto_name_speakers(
        self,
        meeting_id: str,
        *,
        _emit: bool = True,
        record: MeetingRecord | None = None,
        segments: list[SegmentRecord] | None = None,
    ) -> bool:
        """Resolve only explicit introductions in raw speech, after clustering.

        Polished/model-generated text is never evidence for a person's name.
        This also upgrades older meetings when they are next organised.
        """
        record = record if record is not None else self._store.get_meeting(meeting_id)
        if record is None or record.status == STATUS_LIVE or self._generating_for == meeting_id:
            return False
        segments = segments if segments is not None else self._store.get_segments(meeting_id)
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
                markdown = self._render_notes(
                    meeting_id, notes, names=names, record=record, segments=segments
                )
            if markdown is None:
                inferred_pairs = [
                    (previous.get(sid) or speaker_label(sid), names.get(sid) or speaker_label(sid))
                    for sid in sorted(previous.keys() | inferred.keys())
                ]
                markdown = self._replace_speaker_map_in_markdown(notes.markdown, inferred_pairs)
        self._store.save_inferred_names(meeting_id, inferred, markdown=markdown)
        if not self._store.flush():
            return False
        if self.speaker_names(meeting_id) != names:
            return False
        saved = self._store.get_notes(meeting_id) if markdown is not None else None
        if markdown is not None and (saved is None or saved.markdown != markdown):
            return False
        if _emit:
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
        segment = self._store.get_segment(meeting_id, seq)
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
        saved = self._store.get_segment(meeting_id, seq)
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
        segment = self._store.get_segment(meeting_id, seq)
        if segment is None:
            return False
        used_ids = self._store.speaker_ids_in_segments(meeting_id) | {
            s.speaker_id for s in self._store.get_speakers(meeting_id)
        }
        if speaker_id is None:
            minimum = max(used_ids, default=0) + 1
            speaker_id = self._speakers.reserve_speaker_id(minimum) if live and self._speakers else minimum
        elif live and speaker_id not in used_ids:
            return False
        self._store.set_segment_speaker(meeting_id, seq, speaker_id)
        if not self._store.flush():
            return False
        saved = self._store.get_segment(meeting_id, seq)
        if saved is None or saved.speaker_id != speaker_id or not saved.speaker_locked:
            return False
        if live and isinstance(self._speakers, MeetingSpeakerSession):
            self._speakers.confirm(seq, speaker_id)
        self.auto_name_speakers(meeting_id, _emit=False)
        self.meeting_changed.emit(meeting_id)
        return True

    def import_transcript(
        self,
        text: str,
        *,
        title: str = "",
        detect_structure: bool = True,
        started_at: int | None = None,
        parsed=None,
        auto_generate: bool = False,
    ) -> MeetingRecord | None:
        """Save pasted text as an ended meeting (never auto-enqueuing minutes unless opted in)."""
        if parsed is None:
            parsed = parse_transcript(text or "", detect_structure=detect_structure)
        if not parsed.turns:
            self.transcript_import_notice = "没有可导入的文字"
            return None
        try:
            record = self._store.create_meeting_from_transcript(
                parsed, title=title, started_at=started_at,
            )
        except Exception as exc:
            self.transcript_import_notice = f"写入数据库失败：{exc}"
            return None
        saved = self._store.get_meeting(record.id) if (record is not None and self._store.flush(timeout=2.0)) else None
        if saved is None:
            self.transcript_import_notice = "文字已经整理好，但写入数据库失败。请再导入一次。"
            return None
        self.transcript_import_notice = ""
        if auto_generate and saved.status == STATUS_ENDED and not self._shutting_down:
            self._enqueue_automatic(saved.id, record=saved, force=True)
        self.meeting_changed.emit(saved.id)
        if auto_generate and saved.status == STATUS_ENDED and not self._shutting_down:
            self._pump_queue()
        return saved

    @property
    def importing_media(self) -> bool:
        return self._importing_media

    def set_external_block(self, blocker: Callable[[], str] | None) -> None:
        """App-owned reason a file import must wait, such as dictation or recovery."""
        self._external_block = blocker

    def set_media_runner(self, runner) -> None:
        """``runner(path, cancel, progress) -> RecognitionResult``. The page does not decode."""
        self._media_runner = runner

    def has_unsaved_media(self) -> bool:
        return self._unsaved_media is not None

    def unsaved_media_title(self) -> str:
        item = UnsavedMediaImport.coerce(self._unsaved_media)
        if item is None:
            return ""
        if item.title.strip():
            return item.title.strip()
        if item.audio_path:
            return Path(item.audio_path).stem
        return ""

    def discard_unsaved_media(self) -> bool:
        if self._unsaved_media is None:
            return False
        self._unsaved_media = None
        self.media_import_notice = ""
        if not self._shutting_down:
            self._kick_media_queue()
            if not self._importing_media:
                self._pump_queue()
        return True

    def media_block_reason(self) -> str:
        if self._importing_media:
            return "正在导入音视频，请完成或取消后再试"
        if self.is_active:
            return "已有会议在进行中"
        if self._unsaved_media is not None:
            pending_title = self.unsaved_media_title()
            if pending_title:
                return f"「{pending_title}」的导入结果尚未保存，请先重试保存或放弃后再导入"
            return "上一份导入结果尚未保存，请先重试保存或放弃后再导入"
        if self._external_block is not None:
            return self._external_block() or ""
        return ""

    def begin_media_import(self) -> str:
        reason = self.media_block_reason()
        if reason:
            return reason
        self._media_cancel.clear()
        self._importing_media = True
        return ""

    def cancel_media_import(self) -> None:
        self._media_cancel.set()

    def skip_current_media_import(self) -> None:
        """Stop only the currently running file; queued files still advance."""
        self._media_cancel.set()

    def cancel_all_media_imports(self) -> None:
        """Stop the current file and clear all waiting queued files."""
        self._media_queue.clear()
        self.media_queue_notice = ""
        self._media_cancel.set()

    def queued_media(self) -> tuple[tuple[str, str], ...]:
        """Files waiting behind the one being recognized: ``(path, title)``."""
        raw = getattr(self, "_media_queue", ()) or ()
        return tuple((str(item[0]), str(item[1])) for item in raw if len(item) >= 2)

    def remove_queued_media(self, index: int) -> None:
        """Drop a file that has not started. The current recognition stays."""
        queue = getattr(self, "_media_queue", None)
        if queue is None or not 0 <= int(index) < len(queue):
            return
        del queue[int(index)]

    def start_media_import(
        self,
        path: str,
        *,
        title: str = "",
        language: str = "",
        model_id: str = "",
        started_at: int | None = None,
        auto_generate: bool | None = None,
        _from_queue: bool = False,
    ) -> str:
        from sayink.audio_file_decoder import SUPPORTED_EXTENSIONS

        suffix = Path(path).suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            return f"不支持的文件类型：{suffix or '无扩展名'}"
        chosen_title = (title or Path(path).stem).strip()
        resolved_auto = False if auto_generate is None else bool(auto_generate)
        opts: dict[str, Any] = {
            "auto_generate": resolved_auto,
            "explicit_auto_generate": auto_generate is True,
        }
        if language:
            opts["language"] = str(language).strip()
        if model_id:
            opts["model_id"] = str(model_id).strip()
        if started_at is not None:
            opts["started_at"] = int(started_at)
        if self._importing_media:
            entry: tuple = (str(path), chosen_title, opts)
            self._media_queue.append(entry)
            self.media_queue_notice = f"已加入队列，前面还有 {len(self._media_queue)} 个文件"
            return ""
        if _from_queue:
            if self._unsaved_media is not None:
                return self.media_block_reason()
            if self.is_active:
                return "已有会议在进行中"
            if self._external_block is not None:
                ext_reason = self._external_block() or ""
                if ext_reason and "正在转写" not in ext_reason:
                    return ext_reason
            self._media_cancel.clear()
            self._importing_media = True
        else:
            reason = self.begin_media_import()
            if reason:
                return reason
        if self._media_runner is None:
            self._importing_media = False
            return "语音模型未就绪，请等待模型加载完成后再导入音视频"
        # A previous "queued" notice must not stick to the file that just started.
        self.media_queue_notice = ""
        self._media_title = chosen_title
        self._media_source_path = str(path or "")
        self._media_options = dict(opts)
        job = _MediaImportJob(path, self._media_runner, self._media_cancel, None, options=opts)
        job.progressed.connect(self.media_import_progress)
        job.previewed.connect(self.media_import_preview)
        job.timeline.connect(self.media_import_timeline)
        job.succeeded.connect(self._on_media_job_done)
        job.failed.connect(self._on_media_job_failed)
        job.finished.connect(self._retire_media_job)
        self._media_job = job
        self.media_import_started.emit(self._media_source_path, chosen_title)
        job.start()
        return ""

    def commit_media_import(
        self,
        result,
        title: str,
        *,
        _meeting_id: str = "",
        _audio_path: str = "",
        started_at: int | None = None,
        auto_generate: bool | None = None,
    ) -> MeetingRecord | None:
        """Persist finished turns and clear the import lock."""
        target_id = _meeting_id or uuid.uuid4().hex[:12]
        resolved_audio_path = str(
            getattr(result, "audio_path", "")
            or _audio_path
            or getattr(self, "_media_source_path", "")
            or ""
        )
        self.last_imported_audio_path = resolved_audio_path
        opts = dict(getattr(self, "_media_options", None) or {})
        effective_auto_generate = (
            bool(auto_generate)
            if auto_generate is not None
            else bool(opts.get("auto_generate", False))
        )
        explicit_auto_generate = (
            auto_generate is True or bool(opts.get("explicit_auto_generate", False))
        )
        effective_started_at = (
            int(started_at)
            if started_at is not None
            else (
                int(opts["started_at"])
                if opts.get("started_at") is not None
                else _file_mtime_ms(resolved_audio_path)
            )
        )
        shared = RecognitionSettings.from_config(self._config, meeting=True)
        glossary = getattr(result, "glossary", None)
        if glossary is None:
            glossary = opts.get("glossary")
        if glossary is None:
            glossary = shared.glossary
        model_id = str(
            getattr(result, "model_id", "")
            or opts.get("model_id", "")
            or self._config.get("meeting.model_id", "")
            or self._config.get("stt.model_id", "")
            or ""
        )
        saved_options: dict[str, Any] = {
            "started_at": effective_started_at,
            "model_id": model_id,
            "language": str(opts.get("language", "") or ""),
            "auto_generate": effective_auto_generate,
            "explicit_auto_generate": explicit_auto_generate,
            "glossary": copy.deepcopy(glossary) if glossary is not None else None,
        }
        try:
            turns = tuple(getattr(result, "turns", ()) or ())
            if not turns:
                return None
            status = STATUS_INTERRUPTED if (
                self._shutting_down
                or getattr(result, "cancelled", False)
                or getattr(result, "error", "")
            ) else STATUS_ENDED
            record = self._store.create_meeting_from_media(
                turns,
                title=title,
                status=status,
                audio_ms=int(getattr(result, "audio_ms", 0) or 0),
                model_id=model_id,
                glossary=glossary,
                meeting_id=target_id,
                audio_path=resolved_audio_path,
                started_at=effective_started_at,
            )
            if record is None:
                return None
            saved_options["started_at"] = record.started_at
            saved = self._store.get_meeting(record.id) if self._store.flush(timeout=2.0) else None
            if saved is None:
                self._remember_unsaved(result, title, record.id, resolved_audio_path, options=saved_options)
                raise MediaCommitError("识别已经完成，但写入数据库失败。可以重试保存，不必重新识别。")
            # A later file in the queue must not erase an earlier file's retry payload.
            pending = UnsavedMediaImport.coerce(self._unsaved_media)
            if pending is None or pending.result is result:
                self._unsaved_media = None
            self.auto_name_speakers(saved.id, _emit=False, record=saved)
            self._importing_media = False
            if effective_auto_generate and saved.status == STATUS_ENDED and not self._shutting_down:
                self._enqueue_automatic(saved.id, record=saved, force=explicit_auto_generate)
            self.meeting_changed.emit(saved.id)
            if not self._shutting_down and not self._media_queue:
                self._pump_queue()
            return saved
        except MediaCommitError:
            raise
        except Exception as exc:
            self._remember_unsaved(result, title, target_id, resolved_audio_path, options=saved_options)
            raise MediaCommitError(f"写入数据库失败：{exc}") from exc
        finally:
            self._importing_media = False
            self._media_source_path = ""
            self._media_options = {}
            self._media_cancel.clear()

    def _on_media_job_done(self, result) -> None:
        try:
            saved = self.commit_media_import(result, self._media_title)
        except MediaCommitError as exc:
            self.media_import_notice = str(exc)
            self.media_import_finished.emit("")
            if not self._shutting_down:
                self._kick_media_queue()
                if not self._importing_media:
                    self._pump_queue()
            return
        error = str(getattr(result, "error", "") or "")
        if saved is None and (getattr(result, "cancelled", False) or self._shutting_down):
            self.media_import_notice = "已取消导入。"
        elif saved is None and error:
            self.media_import_notice = error
        elif saved is None:
            self.media_import_notice = "没有识别到文字"
        elif error:
            self.media_import_notice = f"已保留前面识别到的文字。后面识别失败：{error}"
        else:
            self.media_import_notice = ""
        self.media_import_finished.emit(saved.id if saved is not None else "")
        if self._shutting_down:
            return
        self._kick_media_queue()
        if not self._importing_media:
            self._pump_queue()

    def retry_media_save(self) -> str:
        """Write the last recognized file again after a database failure."""
        item = UnsavedMediaImport.coerce(self._unsaved_media)
        if item is None:
            return "没有可重试的导入结果"
        result = item.result
        title = item.title
        meeting_id = item.meeting_id
        audio_path = item.audio_path
        opts = dict(item.options)
        if meeting_id and not self._store.disabled:
            self._store.flush(timeout=2.0)
            late_saved = self._store.get_meeting(meeting_id)
            if late_saved is not None:
                self._unsaved_media = None
                self.media_import_notice = ""
                self.auto_name_speakers(meeting_id, _emit=False, record=late_saved)
                want_auto = bool(opts.get("auto_generate", False))
                if want_auto and late_saved.status == STATUS_ENDED and not self._shutting_down:
                    self._enqueue_automatic(
                        meeting_id,
                        record=late_saved,
                        force=bool(opts.get("explicit_auto_generate", False)),
                    )
                self.meeting_changed.emit(meeting_id)
                self.media_import_finished.emit(meeting_id)
                self._kick_media_queue()
                if not self._importing_media and not self._shutting_down:
                    self._pump_queue()
                return ""
        self._importing_media = True
        self._media_title = title
        self._media_source_path = audio_path
        self._media_options = dict(opts)
        try:
            saved = self.commit_media_import(
                result,
                title,
                _meeting_id=meeting_id,
                _audio_path=audio_path,
                started_at=opts.get("started_at"),
                auto_generate=opts.get("auto_generate"),
            )
        except MediaCommitError as exc:
            self.media_import_notice = str(exc)
            return str(exc)
        if saved is None:
            return self.media_import_notice or "没有识别到文字"
        self.media_import_notice = ""
        self.media_import_finished.emit(saved.id)
        self._kick_media_queue()
        if not self._importing_media and not self._shutting_down:
            self._pump_queue()
        return ""

    def _retire_media_job(self) -> None:
        job = self.sender()
        if not isinstance(job, _MediaImportJob) or job.isRunning():
            return
        if self._media_job is job:
            self._media_job = None
        job.deleteLater()

    def _on_media_job_failed(self, message: str) -> None:
        self.last_failed_audio_path = str(self._media_source_path or "")
        self._importing_media = False
        self._media_source_path = ""
        self._media_options = {}
        self._media_cancel.clear()
        self.media_import_failed.emit(message or "导入音视频失败")
        if self._shutting_down:
            return
        self._kick_media_queue()
        if not self._importing_media:
            self._pump_queue()

    def _remember_unsaved(
        self,
        result,
        title: str,
        meeting_id: str,
        audio_path: str,
        *,
        options: dict[str, Any] | None = None,
    ) -> None:
        """Keep the first recognized file that still needs a database write."""
        pending = UnsavedMediaImport.coerce(self._unsaved_media)
        if pending is not None and pending.result is not result:
            return
        merged_opts = dict(pending.options) if pending is not None else {}
        if options:
            merged_opts.update(options)
        self._unsaved_media = UnsavedMediaImport(
            result=result,
            title=title,
            meeting_id=meeting_id,
            audio_path=audio_path,
            options=merged_opts,
        )

    def _kick_media_queue(self) -> None:
        # Starting the next file used to replace the only copy of a transcript
        # that had already been recognized but not saved.
        if self._shutting_down or self._unsaved_media or self._importing_media or not self._media_queue:
            return
        entry = self._media_queue.pop(0)
        path = str(entry[0])
        title = str(entry[1]) if len(entry) > 1 else ""
        opts = dict(entry[2]) if len(entry) > 2 and isinstance(entry[2], dict) else {}
        message = self.start_media_import(
            path,
            title=title,
            language=str(opts.get("language", "") or ""),
            model_id=str(opts.get("model_id", "") or ""),
            started_at=opts.get("started_at"),
            auto_generate=opts.get("auto_generate") if "auto_generate" in opts else None,
            _from_queue=True,
        )
        if message:
            self.media_import_failed.emit(message)
            self._kick_media_queue()

    def transcript_shape(self, record: MeetingRecord, lines: list[TranscriptLine] | None = None) -> tuple[bool, bool]:
        """(speakers are real, timestamps are real).

        Promoted and live meetings keep both even when every speaker id is 0.
        Only ``import_text`` can say the file itself had neither.
        """
        if record.source != IMPORT_SOURCE:
            return True, True
        if lines is None:
            lines = transcript_lines(self._store.get_segments(record.id), record.started_at)
        return any(int(line.speaker_id) > 0 for line in lines), record.ended_at is not None

    def _transcript_flags(self, record: MeetingRecord, lines: list[TranscriptLine]) -> tuple[bool, bool, bool]:
        """Speakers, timestamps, and whether an unlabeled line may say 待确认."""
        speakers, times = self.transcript_shape(record, lines)
        return speakers, times, record.source != IMPORT_SOURCE

    def transcript_text(self, meeting_id: str) -> str:
        record = self._store.get_meeting(meeting_id)
        if record is None:
            return ""
        names = self.speaker_names(meeting_id)
        raw_lines = transcript_lines(self._store.get_segments(meeting_id), record.started_at)
        show_speakers, show_times, label_unknown = self._transcript_flags(record, raw_lines)
        lines = self._merge_lines_with_asr_gaps(meeting_id, record.started_at, raw_lines)
        rendered: list[str] = []
        for line in lines:
            if line.route == "asr_gap":
                rendered.append(line.text)
            else:
                rendered.append(
                    line.render(
                        names,
                        show_speakers=show_speakers,
                        show_times=show_times,
                        label_unknown=label_unknown,
                    )
                )
        return "\n".join(rendered)

    def export_transcript_srt(self, meeting_id: str) -> str:
        record = self._store.get_meeting(meeting_id)
        if record is None:
            return ""
        lines = transcript_lines(self._store.get_segments(meeting_id), record.started_at)
        show_speakers, show_times, label_unknown = self._transcript_flags(record, lines)
        skipped = sum(1 for line in lines if (line.text or "").strip() and not line.time_known)
        self.srt_skip_note = f"有 {skipped} 段没有时间，已从字幕中跳过。" if skipped else ""
        if not show_times:
            return ""
        return render_srt(
            lines, self.speaker_names(meeting_id), show_speakers=show_speakers, label_unknown=label_unknown,
        )

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

    def _save_rendered_notes(
        self,
        meeting_id: str,
        notes: MeetingNotes,
        markdown: str,
        *,
        json_payload: str,
        user_markdown: str | None = None,
    ) -> None:
        self._store.save_notes(
            meeting_id, template_id=notes.template_id, model=notes.model,
            json_payload=json_payload, markdown=markdown,
            duration_ms=notes.duration_ms, chunked=notes.chunked, generated_at=notes.generated_at,
            keep_previous=True,
            user_markdown=user_markdown,
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

    def _render_notes(
        self,
        meeting_id: str,
        notes: MeetingNotes,
        *,
        names: dict[int, str] | None = None,
        record: MeetingRecord | None = None,
        segments: list[SegmentRecord] | None = None,
    ) -> str | None:
        record = record if record is not None else self._store.get_meeting(meeting_id)
        if record is None:
            return None
        payload = notes.payload()
        template = self._notes_template(notes, payload)
        if template is None:
            return None
        segs = segments if segments is not None else self._store.get_segments(meeting_id)
        lines = transcript_lines(segs, record.started_at)
        names = self.speaker_names(meeting_id) if names is None else names
        show_speakers, show_times, label_unknown = self._transcript_flags(record, lines)
        if not show_times:
            clear_timeline(payload)
        return render_markdown(
            payload,
            template=template,
            title_fallback=record.display_title,
            duration_ms=record.duration_ms,
            transcript_text=render_transcript(
                lines, names, show_speakers=show_speakers, show_times=show_times, label_unknown=label_unknown,
            ),
            speaker_names=names,
            meta_lines=self._meeting_meta_lines(record),
        )

    def save_user_markdown(self, meeting_id: str, markdown: str) -> bool:
        if meeting_id and meeting_id == self._generating_for:
            self.cancel_generation()
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
                self._store.save_user_markdown(meeting_id, markdown, keep_backup=True)
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

    def restore_user_markdown(self, meeting_id: str) -> bool:
        """Restore the dedicated hand-edited minutes slot after any number of AI regenerations."""
        before = self._store.get_notes(meeting_id)
        if before is None or not before.user_markdown:
            return False
        if before.user_edited and before.markdown == before.user_markdown:
            return False
        expected = before.user_markdown
        self._store.restore_user_markdown(meeting_id)
        if not self._store.flush():
            return False
        notes = self._store.get_notes(meeting_id)
        if notes is None or not notes.markdown or not notes.user_edited:
            return False
        names = self.speaker_names(meeting_id)
        markdown = apply_speaker_names(notes.markdown, names)
        if markdown != notes.markdown:
            self._store.save_user_markdown(meeting_id, markdown, keep_backup=True)
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

    def delete_meeting(self, meeting_id: str) -> bool:
        if self.is_active and self.current_id == meeting_id:
            return False
        if self._generating_for == meeting_id:
            self._cancel_active_generation_worker()
            self._generating_for = ""
            self.generation_preview_text = ""
        self._forced_auto_generate_ids.discard(meeting_id)
        self._exclude_user_notes_ids.discard(meeting_id)
        sidecar_changed = False
        if meeting_id in self._unsaved_finalizes:
            self._unsaved_finalizes.pop(meeting_id, None)
            sidecar_changed = True
        if meeting_id in self._unsaved_segments:
            self._unsaved_segments.pop(meeting_id, None)
            sidecar_changed = True
        if meeting_id in self._asr_failures:
            self._asr_failures.pop(meeting_id, None)
            sidecar_changed = True
        self._unrecovered_db_failures.pop(meeting_id, None)
        if sidecar_changed:
            self._save_pending_sidecar()
        self._store.delete_meeting(meeting_id)
        deleted = self._store.flush() and self._store.get_meeting(meeting_id) is None
        if deleted:
            self.meeting_changed.emit(meeting_id)
        self._pump_queue()
        return deleted

    def delete_meetings(self, meeting_ids: list[str]) -> bool:
        """Delete many meetings, then pump the minutes queue once."""
        ids = list(dict.fromkeys(meeting_id for meeting_id in meeting_ids if meeting_id))
        if not ids or (self.is_active and self.current_id in ids):
            return False
        if self._generating_for in ids:
            self._cancel_active_generation_worker()
            self._generating_for = ""
            self.generation_preview_text = ""
        sidecar_changed = False
        for mid in ids:
            self._forced_auto_generate_ids.discard(mid)
            self._exclude_user_notes_ids.discard(mid)
            self._unrecovered_db_failures.pop(mid, None)
            if mid in self._unsaved_finalizes:
                self._unsaved_finalizes.pop(mid, None)
                sidecar_changed = True
            if mid in self._unsaved_segments:
                self._unsaved_segments.pop(mid, None)
                sidecar_changed = True
            if mid in self._asr_failures:
                self._asr_failures.pop(mid, None)
                sidecar_changed = True
        if sidecar_changed:
            self._save_pending_sidecar()
        self._store.delete_meetings(ids)
        deleted = self._store.flush() and all(self._store.get_meeting(meeting_id) is None for meeting_id in ids)
        if deleted:
            for meeting_id in ids:
                self.meeting_changed.emit(meeting_id)
        self._pump_queue()
        return deleted

    def export_meetings_markdown(self, meeting_ids: list[str]) -> str:
        parts = [self.export_markdown(meeting_id).rstrip() for meeting_id in meeting_ids]
        parts = [part for part in parts if part]
        return ("\n\n".join(parts) + "\n") if parts else ""

    def enqueue_minutes(
        self,
        meeting_ids: list[str],
        *,
        template_id: str | None = None,
        include_user_notes: bool | None = None,
    ) -> tuple[int, int]:
        """Queue minutes for meetings that do not already have them.

        Returns ``(queued, skipped)``. Existing notes, a live meeting and an
        empty transcript are skipped. One running job stays running; the rest wait.
        """
        template = template_id or str(self._config.get("meeting.template_id", GENERAL_TEMPLATE_ID) or GENERAL_TEMPLATE_ID)
        queued = 0
        skipped = 0
        for meeting_id in list(dict.fromkeys(meeting_id for meeting_id in meeting_ids if meeting_id)):
            record = self._store.get_meeting(meeting_id)
            notes = self._store.get_notes(meeting_id)
            if (
                record is None
                or (notes is not None and notes.markdown.strip())
                or self._generation_problem(record, template, None)
            ):
                skipped += 1
                continue
            if self.generate(meeting_id, template_id=template, include_user_notes=include_user_notes):
                queued += 1
            else:
                skipped += 1
        return queued, skipped

    # -- export ----------------------------------------------------------------

    def export_markdown(self, meeting_id: str, *, include_transcript: bool = True) -> str:
        record = self._store.get_meeting(meeting_id)
        if record is None:
            return ""
        notes = self._store.get_notes(meeting_id)
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
            tx_text = self.transcript_text(meeting_id).strip()
            if tx_text:
                parts.append("\n## 文字稿\n")
                parts.append(tx_text)
        return "\n".join(parts).rstrip() + "\n"

    # -- minutes generation ----------------------------------------------------

    def _custom_templates(self) -> list[dict[str, Any]]:
        custom = self._config.get("meeting.custom_templates", []) or []
        return [t for t in custom if isinstance(t, dict)]

    def remote_host_needing_confirmation(self) -> str:
        return remote_host_needing_confirmation(self._config)

    def confirm_remote_host(self, host: str) -> None:
        confirm_remote_host(self._config, host)

    @staticmethod
    def _safe_int(value: Any, default: int) -> int:
        if value is None or isinstance(value, bool):
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def transcript_char_count(
        self,
        meeting_id: str,
        *,
        record: MeetingRecord | None = None,
        segments: list[SegmentRecord] | None = None,
    ) -> int:
        record = record if record is not None else self._store.get_meeting(meeting_id)
        if record is None:
            return 0
        segs = segments if segments is not None else self._store.get_segments(meeting_id)
        lines = transcript_lines(segs, record.started_at)
        return sum(len(line.text) for line in lines)

    def auto_generate_enabled(self) -> bool:
        return bool(self._config.get("meeting.auto_generate", False))

    def queued_generation(self, meeting_id: str) -> QueuedGeneration | None:
        return self._store.queued_generation(meeting_id)

    def defers_automatic_jobs(self) -> bool:
        """Automatic jobs on a model on this machine wait for live meeting / media import to end."""
        return (self.is_active or self._importing_media) and runs_on_this_machine(minutes_settings(self._config)[0])

    def queue_position(self, meeting_id: str) -> int:
        """1-based place among jobs waiting to run; 0 when not waiting."""
        waiting = [job.meeting_id for job in self._store.generation_queue()
                   if not job.failed and job.meeting_id != self._generating_for]
        return waiting.index(meeting_id) + 1 if meeting_id in waiting else 0

    def generate(
        self,
        meeting_id: str,
        *,
        template_id: str | None = None,
        model_name: str | None = None,
        include_user_notes: bool | None = None,
    ) -> bool:
        """User request: start now, or queue ahead of automatic jobs while
        another meeting generates. False (with ``generation_failed``) when
        the configuration or the transcript makes it impossible."""
        if self._generating_for == meeting_id:
            return False
        if include_user_notes is False:
            self._exclude_user_notes_ids.add(meeting_id)
        elif include_user_notes is True:
            self._exclude_user_notes_ids.discard(meeting_id)
        template_id = template_id or record_template_id(self._config, self._store.get_notes(meeting_id))
        if self._generating_for:
            # One worker, and its result is stored against the running id.
            problem = self._generation_problem(self._store.get_meeting(meeting_id), template_id, model_name)
        else:
            problem = self._start(meeting_id, template_id, model_name, include_user_notes=include_user_notes)
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

    def _enqueue_automatic(
        self,
        meeting_id: str,
        *,
        record: MeetingRecord | None = None,
        segments: list[SegmentRecord] | None = None,
        force: bool = False,
    ) -> None:
        if (not force and not self.auto_generate_enabled()) or not minutes_settings_complete(self._config):
            return
        if self.transcript_char_count(meeting_id, record=record, segments=segments) < AUTO_MIN_TRANSCRIPT_CHARS:
            log.info("会议 %s 内容过短，不自动生成纪要", meeting_id[:8])
            return
        if force and not self.auto_generate_enabled():
            self._forced_auto_generate_ids.add(meeting_id)
        template_id = str(self._config.get("meeting.template_id", GENERAL_TEMPLATE_ID) or GENERAL_TEMPLATE_ID)
        self._store.enqueue_generation(meeting_id, template_id, priority=QUEUE_PRIORITY_AUTO)
        self._store.flush()
        self.generation_queued.emit(meeting_id)

    def _pump_queue(self) -> None:
        """Start the next runnable job: one at a time, user requests first."""
        self._retry_timer.stop()
        if self._shutting_down or self._generating_for or self._store.disabled:
            return
        now = now_ms()
        wake = 0
        for job in self._store.generation_queue():
            if job.automatic:
                notes = self._store.get_notes(job.meeting_id)
                forced = job.meeting_id in self._forced_auto_generate_ids
                if (not self.auto_generate_enabled() and not forced) or (notes is not None and notes.markdown.strip()):
                    # Switched off since, or the user already has notes: never overwrite them.
                    self._forced_auto_generate_ids.discard(job.meeting_id)
                    self._store.dequeue_generation(job.meeting_id)
                    continue
            if job.failed or (job.automatic and self.defers_automatic_jobs()):
                continue
            if job.not_before > now:
                wake = min(wake or job.not_before, job.not_before)
                continue
            host = self.remote_host_needing_confirmation()
            problem = (
                f"文字稿与会议资料未发送：{host} 尚未确认。可在会议页点「生成纪要」确认后发送"
                if host
                else self._start(job.meeting_id, job.template_id)
            )
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

    def _generation_problem(
        self,
        record: MeetingRecord | None,
        template_id: str,
        model_name: str | None,
        *,
        segments: list[SegmentRecord] | None = None,
    ) -> str:
        if record is None:
            return "会议不存在"
        if record.status == STATUS_LIVE:
            return "会议仍在进行，请先结束后再生成"
        api_url, api_key, default_model = minutes_settings(self._config)
        if not polish_settings_complete(api_url, api_key, (model_name or "").strip() or default_model):
            return "未配置纪要模型：请在 设置 → AI 服务 填写接口地址、密钥与模型"
        template = get_template(template_id, self._custom_templates())
        segs = segments if segments is not None else self._store.get_segments(record.id)
        if not transcript_lines(segs, record.started_at,
                                prefer_polished=not template.is_refinement):
            return "文字稿为空，无法整理原文" if template.is_refinement else "文字稿为空，无法生成纪要"
        return ""

    def _start(
        self,
        meeting_id: str,
        template_id: str,
        model_name: str | None = None,
        *,
        record: MeetingRecord | None = None,
        segments: list[SegmentRecord] | None = None,
        include_user_notes: bool | None = None,
    ) -> str | None:
        """Dispatch one job to the worker; the reason when it cannot start."""
        record = record if record is not None else self._store.get_meeting(meeting_id)
        if segments is None and record is not None and record.status != STATUS_LIVE:
            chk_url, chk_key, chk_default = minutes_settings(self._config)
            if polish_settings_complete(chk_url, chk_key, (model_name or "").strip() or chk_default):
                segments = self._store.get_segments(meeting_id)
        problem = self._generation_problem(record, template_id, model_name, segments=segments)
        if problem:
            return problem
        assert record is not None
        api_url, api_key, default_model = minutes_settings(self._config)
        model = (model_name or "").strip() or default_model
        template = get_template(template_id, self._custom_templates())
        if segments is None:
            segments = self._store.get_segments(meeting_id)
        spoken_lines: list[TranscriptLine] = transcript_lines(
            segments, record.started_at, prefer_polished=not template.is_refinement
        )
        show_speakers, show_times, label_unknown = self._transcript_flags(record, spoken_lines)
        lines = self._merge_lines_with_asr_gaps(meeting_id, record.started_at, spoken_lines)
        if not self.auto_name_speakers(meeting_id, record=record, segments=segments):
            return "说话人名称保存失败，请重试"
        names = self.speaker_names(meeting_id)
        marks = [(m.offset_ms, m.note) for m in self._store.get_marks(meeting_id)]
        try:
            started_at_text = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(record.started_at) / 1000.0))
        except (OSError, OverflowError, ValueError):
            started_at_text = "1970-01-01 00:00"
        use_notes = (
            (meeting_id not in self._exclude_user_notes_ids)
            if include_user_notes is None
            else bool(include_user_notes)
        )
        context = MeetingContext(
            title=record.title,
            started_at_text=started_at_text,
            participants=record.participants,
            agenda=record.agenda,
            notes_user=record.notes_user if use_notes else "",
            marks=marks,
            glossary_terms=[str(g.get("term")) for g in record.glossary if isinstance(g, dict) and g.get("term")],
        )
        raw_chunk_limit = self._safe_int(
            self._config.get("meeting.single_request_char_limit", DEFAULT_CHUNK_CHARS),
            DEFAULT_CHUNK_CHARS,
        ) or DEFAULT_CHUNK_CHARS
        req_kwargs: dict[str, Any] = {}
        if "include_user_notes" in MinutesRequest.__dataclass_fields__:
            req_kwargs["include_user_notes"] = use_notes
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
            chunk_chars=min(DEFAULT_CHUNK_CHARS, max(64, raw_chunk_limit)),
            context_window_tokens=max(1024, self._safe_int(self._config.get("meeting.context_window_tokens", 32768), 32768)),
            max_output_tokens=max(256, self._safe_int(self._config.get("meeting.max_output_tokens", 4096), 4096)),
            meta_lines=self._meeting_meta_lines(record),
            speakers_distinguished=show_speakers,
            timestamps_available=show_times,
            label_unknown=label_unknown,
            **req_kwargs,
        )
        fingerprint = request_fingerprint(request)
        if self._generation_cancel_token is not None:
            self._generation_cancel_token.set()
        cancel_token = threading.Event()
        self._generation_cancel_token = cancel_token
        request.checkpoints = self._store.generation_checkpoints(meeting_id, fingerprint)
        request.save_checkpoint = lambda key, raw, cancel_ev=cancel_token: self._store.save_generation_checkpoint(
            meeting_id,
            fingerprint,
            key,
            raw,
            cancel_check=cancel_ev.is_set,
        )
        self._generating_for = meeting_id
        self.generation_preview_text = ""
        self.generation_progress.emit(meeting_id, "正在整理原文…" if template.is_refinement else "正在生成纪要…")
        # An open notes draft can cancel this run from the progress signal
        # before the worker is asked to start.
        if self._generating_for != meeting_id:
            cancel_token.set()
            return None
        self._generator.generate(request)
        return ""

    def cancel_generation(self) -> None:
        if self._generating_for:
            meeting_id = self._generating_for
            self._generating_for = ""
            self.generation_preview_text = ""
            self._forced_auto_generate_ids.discard(meeting_id)
            self._exclude_user_notes_ids.discard(meeting_id)
            self._cancel_active_generation_worker()
            self._store.dequeue_generation(meeting_id)
            self._store.flush()
            self.generation_cancelled.emit(meeting_id)
            self._pump_queue()

    def remove_from_queue(self, meeting_id: str) -> None:
        """Cancel this meeting's job, running or waiting."""
        self._forced_auto_generate_ids.discard(meeting_id)
        self._exclude_user_notes_ids.discard(meeting_id)
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
        self._exclude_user_notes_ids.discard(meeting_id)
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
        workers = tuple(self._generator.running_workers)
        job = self._media_job
        if job is not None and job.isRunning():
            return workers + (job,)
        return workers

    def event(self, event) -> bool:
        # Destroying this object must not destroy a QThread that is still inside
        # a native decode. Stop that thread first; a zero wait on shutdown only
        # asks it to cancel.
        if event.type() == QEvent.Type.DeferredDelete:
            self._stop_media_job(force=True)
        return super().event(event)

    def _stop_media_job(self, *, force: bool, wait_ms: int = 0) -> None:
        self._media_queue.clear()
        self.cancel_media_import()
        job = self._media_job
        if job is None:
            self._importing_media = False
            return
        if job.isRunning() and wait_ms:
            job.wait(wait_ms)
        if job.isRunning() and force:
            # Keep the QThread alive after this controller is destroyed. Terminating
            # it here deadlocks a Python worker that still needs this thread.
            for signal in (job.progressed, job.previewed, job.timeline, job.succeeded, job.failed, job.finished):
                try:
                    signal.disconnect()
                except TypeError:
                    pass
            if job not in _ORPHAN_MEDIA_JOBS:
                _ORPHAN_MEDIA_JOBS.append(job)

            def _drop(finished=job) -> None:
                try:
                    _ORPHAN_MEDIA_JOBS.remove(finished)
                except ValueError:
                    pass
                finished.deleteLater()

            job.finished.connect(_drop)
            self._media_job = None
            return
        if job.isRunning():
            return
        if self._media_job is job:
            self._media_job = None
        if force:
            try:
                job.finished.disconnect(self._retire_media_job)
            except TypeError:
                pass
            job.deleteLater()

    def shutdown(self, *, wait_ms: int = 2000) -> None:
        """Queued and interrupted jobs stay in the database for the next start."""
        self._shutting_down = True
        if hasattr(self._history, "remove_committed_callback"):
            self._history.remove_committed_callback(self._on_history_committed_threadsafe)
        self._stop_media_job(force=False, wait_ms=wait_ms)
        self.abandon_on_exit()
        if hasattr(self._history, "set_segment_calibrator"):
            self._history.set_segment_calibrator(None)
        self._clock.stop()
        self._drain_timer.stop()
        self._retry_timer.stop()
        self._cached_embedder = None
        self._generator.shutdown(wait_ms=wait_ms)


def record_template_id(config: Config, notes: MeetingNotes | None) -> str:
    if notes is not None and notes.template_id:
        return notes.template_id
    return str(config.get("meeting.template_id", GENERAL_TEMPLATE_ID) or GENERAL_TEMPLATE_ID)

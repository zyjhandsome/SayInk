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
from pathlib import Path
import re
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Callable

from PyQt6.QtCore import QEvent, QObject, QThread, QTimer, pyqtSignal

from sayink.config import Config
from sayink.history_store import HistoryStore, SegmentRecord
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
from sayink.speaker_clustering import final_assignments
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


def _format_remote_origin(scheme: str, host: str) -> str:
    bracketed = f"[{host}]" if ":" in host else host
    return f"{scheme}://{bracketed}"


def _confirmed_remote_origins(entries) -> set[tuple[str, str]]:
    origins: set[tuple[str, str]] = set()
    for raw in entries or ():
        item = str(raw or "").strip().lower()
        if not item:
            continue
        if "://" in item:
            parsed, raw_host, error = _url_parts(item)
            host = raw_host.strip("[]").lower()
            if not error and parsed is not None and parsed.scheme in {"http", "https"} and host:
                origins.add((parsed.scheme, host))
        else:
            # Backward-compatible with legacy bare-hostname entries, scoped to
            # HTTPS so downgrading https://host -> http://host requires confirmation.
            origins.add(("https", item.strip("[]")))
    return origins


def remote_host_needing_confirmation(config: Config) -> str:
    """``scheme://host`` when the minutes endpoint is remote and not yet confirmed."""
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
    confirmed = _confirmed_remote_origins(config.get("meeting.confirmed_remote_hosts", []))
    return "" if (scheme, host) in confirmed else _format_remote_origin(scheme, host)


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
        canonical = _format_remote_origin(scheme, hostname)
    else:
        api_url, _key, _model = minutes_settings(config)
        parsed_api, raw_api_host, api_error = _url_parts(api_url)
        api_host = "" if api_error else raw_api_host.strip("[]").lower()
        api_scheme = "" if (api_error or parsed_api is None) else (parsed_api.scheme or "").lower()
        hostname = item.strip("[]")
        scheme = api_scheme if api_host == hostname and api_scheme in {"http", "https"} else "https"
        canonical = _format_remote_origin(scheme, hostname)
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


class _SampledSpeakerView:
    """Bounded turn view passed into ``final_assignments`` on multi-hour meetings."""

    def __init__(self, turns, confirmed: dict[int, int], speaker_count: int) -> None:
        self.turns = tuple(turns)
        self.confirmed = dict(confirmed)
        self.speaker_count = int(speaker_count)


def _bounded_final_assignments(
    speakers: MeetingSpeakerSession,
    threshold: float,
    *,
    protected_ids: set[int],
    locked_keys: set[int],
) -> dict[int, int]:
    turns = speakers.turns
    keyed = [t for t in turns if t.key >= 0]
    if len(keyed) <= 600:
        return final_assignments(
            speakers, threshold, protected_ids=protected_ids, locked_keys=locked_keys,
        )
    import numpy as np

    confirmed = speakers.confirmed
    must_keep: list[int] = []
    optional: list[int] = []
    seen_first_for_speaker: set[int] = set()
    for idx, turn in enumerate(turns):
        is_anchor = turn.speaker_id > 0 and turn.speaker_id not in seen_first_for_speaker
        if is_anchor:
            seen_first_for_speaker.add(turn.speaker_id)
        if (
            turn.key in locked_keys
            or turn.key in confirmed
            or turn.speaker_id in protected_ids
            or is_anchor
        ):
            must_keep.append(idx)
        else:
            optional.append(idx)

    budget = max(0, _MAX_CLUSTER_TURNS - len(must_keep))
    if budget >= len(optional):
        chosen_indices = sorted(must_keep + optional)
    elif budget <= 0:
        chosen_indices = sorted(must_keep)
    else:
        step = len(optional) / budget
        sampled_opt = [optional[min(len(optional) - 1, int(i * step))] for i in range(budget)]
        chosen_indices = sorted(set(must_keep) | set(sampled_opt))

    sampled_turns = [turns[i] for i in chosen_indices]
    sampled_keys = {t.key for t in sampled_turns if t.key >= 0}
    sampled_confirmed = {k: v for k, v in confirmed.items() if k in sampled_keys}
    core = final_assignments(
        _SampledSpeakerView(sampled_turns, sampled_confirmed, speakers.speaker_count),
        threshold,
        protected_ids=protected_ids,
        locked_keys=locked_keys & sampled_keys,
    )

    cluster_vecs: dict[int, list[np.ndarray]] = {}
    speaker_vote: dict[int, dict[int, int]] = {}
    for turn in sampled_turns:
        sid = core.get(turn.key, turn.speaker_id)
        if sid > 0 and turn.learnable and turn.vector is not None and len(turn.vector) > 0:
            cluster_vecs.setdefault(sid, []).append(np.asarray(turn.vector, dtype=np.float32))
        if turn.speaker_id > 0 and sid > 0:
            votes = speaker_vote.setdefault(turn.speaker_id, {})
            votes[sid] = votes.get(sid, 0) + 1

    centroids: dict[int, np.ndarray] = {}
    for sid, vecs in cluster_vecs.items():
        mean = np.mean(np.stack(vecs, axis=0), axis=0)
        norm = float(np.linalg.norm(mean))
        if norm > 1e-8:
            centroids[sid] = (mean / norm).astype(np.float32)

    fallback_map = {
        old_sid: max(votes.items(), key=lambda kv: kv[1])[0]
        for old_sid, votes in speaker_vote.items()
        if votes
    }

    full: dict[int, int] = dict(core)
    for turn in keyed:
        if turn.key in full:
            continue
        if turn.key in confirmed:
            full[turn.key] = confirmed[turn.key]
            continue
        best_sid = 0
        best_sim = -1.0
        if centroids and turn.vector is not None and len(turn.vector) > 0:
            vec = np.asarray(turn.vector, dtype=np.float32)
            vnorm = float(np.linalg.norm(vec))
            if vnorm > 1e-8:
                uvec = vec / vnorm
                for sid, cvec in centroids.items():
                    if cvec.shape == uvec.shape:
                        sim = float(np.dot(uvec, cvec))
                        if sim > best_sim:
                            best_sim = sim
                            best_sid = sid
        if best_sid > 0 and best_sim >= threshold:
            full[turn.key] = best_sid
        elif turn.speaker_id in fallback_map:
            full[turn.key] = fallback_map[turn.speaker_id]
        else:
            full[turn.key] = turn.speaker_id
    return full


class MediaCommitError(RuntimeError):
    """Recognition finished, but the meeting row was not stored."""


# A thread that could not be stopped stays here so deleting the controller
# does not destroy it while it is still running.
_ORPHAN_MEDIA_JOBS: list[QThread] = []


class _MediaImportJob(QThread):
    """Read and recognize one file away from the UI thread. The runner is injected."""

    progressed = pyqtSignal(str)
    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, path: str, runner, cancel_event: threading.Event, parent=None) -> None:
        super().__init__(parent)
        self._path = path
        self._runner = runner
        self._cancel = cancel_event

    def run(self) -> None:
        try:
            result = self._runner(self._path, self._cancel.is_set, self.progressed.emit)
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
    media_import_progress = pyqtSignal(str)
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
        if hasattr(self._history, "set_segment_calibrator"):
            self._history.set_segment_calibrator(self._calibrate_segment)
        self._shutting_down = False
        self._importing_media = False
        self._media_cancel = threading.Event()
        self._media_runner = None
        self._external_block: Callable[[], str] | None = None
        self._media_job: _MediaImportJob | None = None
        self._media_title = ""
        self._media_source_path = ""
        self._unsaved_media: tuple | None = None
        self.media_import_notice = ""
        self.transcript_import_notice = ""
        self.srt_skip_note = ""

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
            if not self._paused_intervals_active():
                return record
            started = self._started_wall_ms or cur.started_at
            raw_captured = int(record.captured_at or 0)
            if raw_captured < 0:
                return record
            stamp = (
                raw_captured
                if raw_captured > 0
                else max(started, int(record.created_at) - max(0, int(record.duration_ms or 0)))
            )
            deduct = self._paused_deduction_at(stamp)
            if deduct <= 0:
                return record
            calibrated = max(started, stamp - deduct)
            return replace(record, captured_at=calibrated)

    def _paused_intervals_active(self) -> bool:
        return bool(self._paused_ms > 0 or self._pause_started_wall_ms > 0 or self._pause_intervals)

    def elapsed_ms(self) -> int:
        with self._current_lock:
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

    def request_end(self) -> None:
        """Audio capture has stopped; wait for queued segments, then finalize."""
        if self._state not in (MeetingState.LIVE, MeetingState.PAUSED):
            return
        self._clock.stop()
        self._close_open_pause()
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
        cur = self.current
        if cur is None or self._state is MeetingState.ENDED:
            return
        meeting_id = cur.id
        started_at = self._started_wall_ms or cur.started_at
        self._clock.stop()
        self._drain_timer.stop()
        self._close_open_pause()
        active_end = max(started_at, started_at + self.elapsed_ms())
        self._store.end_meeting(meeting_id, ended_at=active_end)
        # The last segments may still sit in the writer queue; commit them and
        # the provisional ended_at together with a bounded wait so UI never hangs.
        self._store.flush(timeout=2.0)
        segments = self._store.get_segments(meeting_id)
        calibrated_ends = [
            s.captured_at + max(0, int(s.duration_ms or 0))
            for s in segments
            if s.trigger_mode == "meeting" and s.captured_at > 0
        ]
        ended_at = max([active_end, *calibrated_ends])
        ended_at_updated = ended_at != active_end
        if ended_at_updated:
            self._store.end_meeting(meeting_id, ended_at=ended_at)
        self._recluster_segments_cache = segments
        try:
            reclustered = self._apply_recluster(meeting_id)
        finally:
            self._recluster_segments_cache = None
        if reclustered is not False or ended_at_updated:
            self._store.flush(timeout=2.0)
        if reclustered is not False:
            segments = self._store.get_segments(meeting_id)
        stored_record = self._store.get_meeting(meeting_id)
        self.auto_name_speakers(meeting_id, _emit=False, record=stored_record, segments=segments)
        self._enqueue_automatic(meeting_id, record=stored_record, segments=segments)
        self._speakers = None
        self._embedder = None
        ended_record = stored_record or MeetingRecord(
            id=meeting_id, started_at=started_at, ended_at=ended_at, status="ended"
        )
        with self._current_lock:
            self._current = ended_record
        self._set_state(MeetingState.ENDED)
        self.meeting_ended.emit(meeting_id)
        self.meeting_changed.emit(meeting_id)
        # Back to idle for the next meeting; the page keeps showing this one.
        with self._current_lock:
            self._current = None
            self._started_wall_ms = 0
            self._paused_ms = 0
            self._pause_started_wall_ms = 0
            self._pause_intervals = []
        self._paused_detail = ""
        self._options = None
        self._capabilities = None
        self._set_state(MeetingState.IDLE)
        self._pump_queue()

    def _apply_recluster(self, meeting_id: str) -> bool:
        """End-of-meeting pass: cluster every turn and re-assign each segment.

        Named or manually corrected speakers keep their numbers and locked
        segments never move. Falls back to the centroid merge when the
        session cannot provide per-turn evidence, and bounds pairwise
        clustering on multi-hour meetings so the UI thread never stalls.
        """
        speakers = self._speakers
        if speakers is None:
            return False
        embedder = self._embedder
        protected = {s.speaker_id for s in self._store.get_speakers(meeting_id) if s.name_locked or s.display_name}
        cached_segs = getattr(self, "_recluster_segments_cache", None)
        segments = cached_segs if cached_segs is not None else self._store.get_segments(meeting_id)
        locked = {s.seq for s in segments if s.speaker_locked}
        protected.update(s.speaker_id for s in segments if s.speaker_locked)
        if isinstance(speakers, MeetingSpeakerSession) and any(t.key >= 0 for t in speakers.turns):
            threshold = embedder.cluster_threshold if embedder is not None else speakers.threshold
            assignments = _bounded_final_assignments(
                speakers, threshold, protected_ids=protected, locked_keys=locked,
            )
            current = {s.seq: s.speaker_id for s in segments}
            changed = {seq: sid for seq, sid in assignments.items() if seq >= 0 and current.get(seq) not in (None, sid)}
            if changed:
                log.info("会议 %s 说话人重新归属 %d 段", meeting_id[:8], len(changed))
                self._store.set_segment_speakers(meeting_id, changed)
                return True
            return False
        if speakers.speaker_count < 2:
            return False
        threshold = embedder.recluster_threshold if embedder is not None else speakers.threshold
        mapping = recluster(speakers.history, threshold=threshold, protected_ids=protected)
        if mapping and any(old != new for old, new in mapping.items()):
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
        self._close_open_pause()
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
        self._store.end_meeting(meeting_id, ended_at=ended_at, status=STATUS_INTERRUPTED)
        self._store.flush(timeout=2.0)
        try:
            self.auto_name_speakers(meeting_id, _emit=False)
        except Exception as exc:
            log.debug("退出时说话人自动命名跳过: %s", exc)
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
        self._paused_detail = ""
        self._set_state(MeetingState.IDLE)

    def mark_interrupted(self) -> None:
        self._store.mark_interrupted_meetings()

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
        self._store.add_mark(cur.id, offset, note.strip())
        self.notice.emit("已标记", format_timestamp(offset))
        self.meeting_changed.emit(cur.id)
        return offset

    def update_meeting(self, meeting_id: str, **fields: str) -> None:
        self._store.update_meeting(meeting_id, **fields)
        with self._current_lock:
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
        if saved is None or saved.display_name != name or not saved.name_locked:
            return None
        self.meeting_changed.emit(meeting_id)
        return replaced

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
    ) -> MeetingRecord | None:
        """Save pasted text as an ended meeting. The user generates minutes later."""
        parsed = parse_transcript(text or "", detect_structure=detect_structure)
        if not parsed.turns:
            self.transcript_import_notice = "没有可导入的文字"
            return None
        try:
            record = self._store.create_meeting_from_transcript(parsed, title=title)
        except Exception as exc:
            self.transcript_import_notice = f"写入数据库失败：{exc}"
            return None
        saved = self._store.get_meeting(record.id) if (record is not None and self._store.flush()) else None
        if saved is None:
            self.transcript_import_notice = "文字已经整理好，但写入数据库失败。请再导入一次。"
            return None
        self.transcript_import_notice = ""
        self.meeting_changed.emit(saved.id)
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

    def media_block_reason(self) -> str:
        if self._importing_media:
            return "正在导入音视频，请完成或取消后再试"
        if self.is_active:
            return "已有会议在进行中"
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

    def start_media_import(self, path: str, *, title: str = "") -> str:
        from sayink.audio_file_decoder import SUPPORTED_EXTENSIONS

        suffix = Path(path).suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            return f"不支持的文件类型：{suffix or '无扩展名'}"
        reason = self.begin_media_import()
        if reason:
            return reason
        if self._media_runner is None:
            self._importing_media = False
            return "语音模型未就绪，请等待模型加载完成后再导入音视频"
        self._media_title = (title or Path(path).stem).strip()
        self._media_source_path = str(path or "")
        job = _MediaImportJob(path, self._media_runner, self._media_cancel, None)
        job.progressed.connect(self.media_import_progress)
        job.succeeded.connect(self._on_media_job_done)
        job.failed.connect(self._on_media_job_failed)
        job.finished.connect(self._retire_media_job)
        self._media_job = job
        job.start()
        return ""

    def commit_media_import(
        self,
        result,
        title: str,
        *,
        _meeting_id: str = "",
        _audio_path: str = "",
    ) -> MeetingRecord | None:
        """Persist finished turns and clear the import lock. Minutes stay manual."""
        target_id = _meeting_id or uuid.uuid4().hex[:12]
        resolved_audio_path = str(
            getattr(result, "audio_path", "")
            or _audio_path
            or getattr(self, "_media_source_path", "")
            or ""
        )
        try:
            turns = tuple(getattr(result, "turns", ()) or ())
            if not turns:
                return None
            status = STATUS_INTERRUPTED if (
                getattr(result, "cancelled", False) or getattr(result, "error", "")
            ) else STATUS_ENDED
            shared = RecognitionSettings.from_config(self._config, meeting=True)
            glossary = getattr(result, "glossary", None)
            if glossary is None:
                glossary = shared.glossary
            model_id = str(
                getattr(result, "model_id", "")
                or self._config.get("meeting.model_id", "")
                or self._config.get("stt.model_id", "")
                or ""
            )
            record = self._store.create_meeting_from_media(
                turns,
                title=title,
                status=status,
                audio_ms=int(getattr(result, "audio_ms", 0) or 0),
                model_id=model_id,
                glossary=glossary,
                meeting_id=target_id,
                audio_path=resolved_audio_path,
            )
            if record is None:
                return None
            saved = self._store.get_meeting(record.id) if self._store.flush() else None
            if saved is None:
                self._unsaved_media = (result, title, record.id, resolved_audio_path)
                raise MediaCommitError("识别已经完成，但写入数据库失败。可以重试保存，不必重新识别。")
            self._unsaved_media = None
            self.auto_name_speakers(saved.id, _emit=False, record=saved)
            self.meeting_changed.emit(saved.id)
            return saved
        except MediaCommitError:
            raise
        except Exception as exc:
            self._unsaved_media = (result, title, target_id, resolved_audio_path)
            raise MediaCommitError(f"写入数据库失败：{exc}") from exc
        finally:
            self._importing_media = False
            self._media_source_path = ""
            self._media_cancel.clear()

    def _on_media_job_done(self, result) -> None:
        try:
            saved = self.commit_media_import(result, self._media_title)
        except MediaCommitError as exc:
            self.media_import_notice = str(exc)
            self.media_import_finished.emit("")
            return
        error = str(getattr(result, "error", "") or "")
        if saved is None and getattr(result, "cancelled", False):
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

    def retry_media_save(self) -> str:
        """Write the last recognized file again after a database failure."""
        pending = self._unsaved_media
        if not pending:
            return "没有可重试的导入结果"
        if len(pending) >= 4:
            result, title, meeting_id, audio_path = pending[:4]
        else:
            result, title, meeting_id = pending
            audio_path = ""
        if meeting_id and self._store.flush() and self._store.get_meeting(meeting_id) is not None:
            self._unsaved_media = None
            self.media_import_notice = ""
            self.auto_name_speakers(meeting_id, _emit=False)
            self.meeting_changed.emit(meeting_id)
            self.media_import_finished.emit(meeting_id)
            return ""
        self._importing_media = True
        self._media_title = title
        try:
            saved = self.commit_media_import(
                result, title, _meeting_id=meeting_id, _audio_path=audio_path
            )
        except MediaCommitError as exc:
            self.media_import_notice = str(exc)
            return str(exc)
        if saved is None:
            return self.media_import_notice or "没有识别到文字"
        self.media_import_notice = ""
        self.media_import_finished.emit(saved.id)
        return ""

    def _retire_media_job(self) -> None:
        job = self.sender()
        if not isinstance(job, _MediaImportJob) or job.isRunning():
            return
        if self._media_job is job:
            self._media_job = None
        job.deleteLater()

    def _on_media_job_failed(self, message: str) -> None:
        self._importing_media = False
        self._media_cancel.clear()
        self.media_import_failed.emit(message or "导入音视频失败")

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
        lines = transcript_lines(self._store.get_segments(meeting_id), record.started_at)
        show_speakers, show_times, label_unknown = self._transcript_flags(record, lines)
        return render_transcript(
            lines, names, show_speakers=show_speakers, show_times=show_times, label_unknown=label_unknown,
        )

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
            self._generator.cancel()
            self._generating_for = ""
            self.generation_preview_text = ""
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
            self._generator.cancel()
            self._generating_for = ""
            self.generation_preview_text = ""
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

    def enqueue_minutes(self, meeting_ids: list[str], *, template_id: str | None = None) -> tuple[int, int]:
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
            if self.generate(meeting_id, template_id=template):
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
                show_speakers, show_times, label_unknown = self._transcript_flags(record, lines)
                parts.append("\n## 文字稿\n")
                parts.append(render_transcript(
                    lines, names, show_speakers=show_speakers, show_times=show_times, label_unknown=label_unknown,
                ))
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

    def _enqueue_automatic(
        self,
        meeting_id: str,
        *,
        record: MeetingRecord | None = None,
        segments: list[SegmentRecord] | None = None,
    ) -> None:
        if not self.auto_generate_enabled() or not minutes_settings_complete(self._config):
            return
        if self.transcript_char_count(meeting_id, record=record, segments=segments) < AUTO_MIN_TRANSCRIPT_CHARS:
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
        lines: list[TranscriptLine] = transcript_lines(segments, record.started_at, prefer_polished=not template.is_refinement)
        show_speakers, show_times, label_unknown = self._transcript_flags(record, lines)
        if not self.auto_name_speakers(meeting_id, record=record, segments=segments):
            return "说话人名称保存失败，请重试"
        names = self.speaker_names(meeting_id)
        marks = [(m.offset_ms, m.note) for m in self._store.get_marks(meeting_id)]
        try:
            started_at_text = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(record.started_at) / 1000.0))
        except (OSError, OverflowError, ValueError):
            started_at_text = "1970-01-01 00:00"
        context = MeetingContext(
            title=record.title,
            started_at_text=started_at_text,
            participants=record.participants,
            agenda=record.agenda,
            notes_user=record.notes_user,
            marks=marks,
            glossary_terms=[str(g.get("term")) for g in record.glossary if isinstance(g, dict) and g.get("term")],
        )
        raw_chunk_limit = self._safe_int(
            self._config.get("meeting.single_request_char_limit", DEFAULT_CHUNK_CHARS),
            DEFAULT_CHUNK_CHARS,
        ) or DEFAULT_CHUNK_CHARS
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
        self.cancel_media_import()
        job = self._media_job
        if job is None:
            return
        if job.isRunning() and wait_ms:
            job.wait(wait_ms)
        if job.isRunning() and force:
            # Keep the QThread alive after this controller is destroyed. Terminating
            # it here deadlocks a Python worker that still needs this thread.
            for signal in (job.progressed, job.succeeded, job.failed, job.finished):
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

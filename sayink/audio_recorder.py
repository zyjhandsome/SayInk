import logging
from pathlib import Path
import threading
import time
import re
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np
import sounddevice as sd
from PyQt6.QtCore import QObject, pyqtSignal, QTimer

from sayink.audio_devices import (
    INPUT_SOURCE_MICROPHONE,
    INPUT_SOURCE_MIXED,
    INPUT_SOURCE_SYSTEM,
    RecordingPlan,
    StreamEndpoint,
    _device_info_by_index,
    build_recording_plan,
    describe_active_loopback_device,
    input_source_label,
    invalidate_device_cache,
    is_unreliable_loopback_output,
    mark_portaudio_stream_active,
    ordered_system_devices,
    pick_default_system_capture,
    preferred_windows_loopback_names,
    refresh_audio_devices,
    should_use_wasapi_loopback,
)
from sayink.audio_utils import (
    TARGET_SAMPLE_RATE,
    LaneBalancer,
    StreamResampler,
    mix_to_mono,
    rms_volume,
    to_mono,
)
from sayink.pawp_capture import (
    endpoint_names_match,
    is_encoded_pawp_device_index,
    resolve_pawp_stream_device_info,
    strip_loopback_suffix,
)
from sayink.lane_echo import DuplicateLaneSuppressor
from sayink.runtime_status import SegmentationStatus
from sayink.audio_recovery import RecordingJournal
from sayink.vad_segmenter import (
    VAD_ENERGY,
    VAD_SILERO,
    SpeechSegmenter,
    attach_segment_acoustics,
    make_meeting_segmenter,
    make_speech_gate,
)

# A lane this quiet is digital silence, not a person. Mixing it in still
# runs the loudness balancer on the only real lane and moves the cut points.
_DIGITAL_SILENCE_RMS = 1e-4

log = logging.getLogger("SayInk")


def _render_playback_peak() -> float:
    """Loudest level any Windows playback endpoint reports right now.

    0.0 off Windows or when the endpoints cannot be queried.
    """
    from sayink import audio_devices

    try:
        endpoints = audio_devices._cached_windows_render_endpoints()
        return float(max(
            endpoints.console_peak, endpoints.communications_peak, endpoints.active_render_peak,
        ))
    except Exception:
        log.debug("读取播放端点音量失败", exc_info=True)
        return 0.0


# Cache last-known working (sample_rate, try_loopback) per (device_index, device_name, want_loopback)
# so hotkeys open WASAPI streams on the first attempt instead of failing 16 kHz probes each time.
_WORKING_STREAM_PARAMS: dict[tuple[int, str, bool], tuple[int, bool]] = {}


@dataclass(frozen=True)
class HoldCaptureStats:
    first_block_ms: int | None
    captured_ms: int


class _CaptureLane:
    def __init__(self, endpoint: StreamEndpoint):
        self.endpoint = endpoint
        self.chunks: list[np.ndarray] = []
        # Resampled to TARGET_SAMPLE_RATE, not yet mixed with the other lanes.
        self.pending = np.zeros(0, dtype=np.float32)
        self.last_chunk_at = time.monotonic()
        self.first_chunk_at: float | None = None
        self.captured_samples = 0
        self.sample_rate = TARGET_SAMPLE_RATE
        self.resampler: Optional[StreamResampler] = None
        self.stream: Optional[sd.InputStream] = None
        self.stream_registered: bool = False
        self.stream_failed: bool = False
        self.pawp_stream: object | None = None
        self.pawp_stop: threading.Event | None = None
        self.pawp_thread: threading.Thread | None = None
        self.pawp_lock = threading.Lock()
        self.drift_ticks: int = 0

    def append_chunk(self, block: np.ndarray) -> None:
        self.chunks.append(block)
        self.last_chunk_at = time.monotonic()
        if block.size:
            if self.first_chunk_at is None:
                self.first_chunk_at = self.last_chunk_at
            self.captured_samples += block.size


class AudioRecorder(QObject):
    segmentation_changed = pyqtSignal(object)
    volume_changed = pyqtSignal(float)
    lane_levels_changed = pyqtSignal(float, float)
    system_silent_warning = pyqtSignal(str)
    recording_finished = pyqtSignal(np.ndarray)
    segment_ready = pyqtSignal(np.ndarray)
    utterance_opened = pyqtSignal(int, str)
    error = pyqtSignal(str)
    warning = pyqtSignal(str)
    no_speech_warning = pyqtSignal()

    NO_SPEECH_WARN_SEC = 30.0
    SYSTEM_SILENT_WARN_SEC = 15.0
    SYSTEM_ROUTE_POLL_SEC = 2.0
    SYSTEM_ACTIVE_RMS = 4e-4
    LANE_STALL_SEC = 3.0
    # Mixing waits for a lane that delivered within this window; a quieter
    # one (loopback while nothing plays) is filled with silence instead.
    MIX_WAIT_SEC = 0.3
    MAX_MIX_SKEW_SEC = 1.0
    DRIFT_TOLERANCE_SAMPLES = int(0.02 * TARGET_SAMPLE_RATE)
    DRIFT_SUSTAIN_TICKS = 8
    # Share of the remaining distance a lane's mix gain covers per block.
    LANE_GAIN_GLIDE = 0.25
    # A microphone-only meeting that keeps hearing playback is probably a call
    # whose remote side is not recorded: this many polls above the peak within
    # the window, so one cue or notification is not taken for a call.
    UNRECORDED_PLAYBACK_PEAK = 1e-3
    UNRECORDED_PLAYBACK_HITS = 4
    UNRECORDED_PLAYBACK_WINDOW_SEC = 60.0

    SAMPLE_RATE = TARGET_SAMPLE_RATE
    CHANNELS = 1
    CHUNK_DURATION = 0.1

    def __init__(self, parent=None):
        super().__init__(parent)
        self._is_recording = False
        self._is_cancelled = False
        self._is_paused = False
        self._route_watch_enabled = False
        self._lanes: list[_CaptureLane] = []
        self._lock = threading.Lock()
        self._overflow_lock = threading.Lock()
        self._input_source = INPUT_SOURCE_MICROPHONE
        self._mic_device_index = -1
        self._system_device_index = -1
        self._plan: Optional[RecordingPlan] = None
        self._last_start_warning: str = ""
        self._pawp: object | None = None
        self._retired_pawp: list[object] = []
        self._continuous_mode = False
        self._segment_live = False
        self._segmenter = SpeechSegmenter()
        self._continuous_timer = QTimer(self)
        self._continuous_timer.setInterval(100)
        self._continuous_timer.timeout.connect(self._on_continuous_tick)
        self._last_speech_at = 0.0
        self._no_speech_warned = False
        self._mic_level = 0.0
        self._system_level = 0.0
        self._last_mic_speech_at = 0.0
        self._last_system_speech_at = 0.0
        self._system_ever_active = False
        self._system_silent_warned = False
        self._mixed_unpaused_since = 0.0
        self._mixed_elapsed_before_pause = 0.0
        self._last_route_poll_at = 0.0
        self._block_mic_energy = 0.0
        self._block_system_energy = 0.0
        self._segment_route = ""
        self._segment_route_reliable = False
        self._vad_kind = VAD_ENERGY
        self._vad_model_path: str | None = None
        # Silero's ONNX session stays here. Rebuilding the segmenter (a mode
        # or source change) must not load the model again on the UI thread.
        self._silero_gate = None
        self._silero_gate_path: str | None = None
        self._split_silero_gates: dict[str, object] = {}
        self._split_silero_gate_paths: dict[str, str] = {}
        self._meeting_profile = False
        self._segmentation_status = SegmentationStatus(VAD_ENERGY, VAD_ENERGY)
        # Per-lane loudness balancing for mixed capture (every profile).
        self._balancer: LaneBalancer | None = LaneBalancer()
        # Mix gain each lane ended its last block with (see _glide_lane_gain).
        self._lane_gains: dict[str, float] = {}
        self._playback_hits: list[float] = []
        self._playback_warned = False
        self._last_playback_poll_at = time.monotonic()
        self._duplicate_suppressor = DuplicateLaneSuppressor()
        # Wall-clock ms at which the block that opened the current utterance
        # arrived; handed out with the segment (history.captured_at).
        self._segment_started_at = 0
        self._block_started_at = 0
        self._stream_anchor_ms = 0
        self._segment_captured_at = 0
        self._open_target = (0, "")
        self._segment_target = (0, "")
        self._recovery_root = None
        self._recovery_session_id: str = ""
        self._last_session_journal: tuple[str, object] | None = None
        self._mute_system_until: float = 0.0
        self._journal: RecordingJournal | None = None
        self._closing_journals = []
        self._boundary_requested = False
        self._boundary_buffer = None
        self._separate_lanes_requested = False
        self._split_segmenters = {}
        self._split_boundaries = {}
        self._split_targets = {}
        self._block_tracks = {}
        self._recovery_tracks = {}
        self._split_ready = []
        self._quiet_speech: bool | None = None
        self._last_overflow_warning = float("-inf")
        self._suppress_empty_finished = False
        self._last_emitted_volume = 0.0
        self._last_real_block_at = 0.0
        self._last_loopback_pad_at = 0.0
        self.last_hold_capture: HoldCaptureStats | None = None
        self._hold_capture_started_at = 0.0
        self._hold_segments_emitted = 0

    @property
    def sample_rate(self) -> int:
        return int(self.SAMPLE_RATE)

    @property
    def lane_levels(self) -> tuple[float, float]:
        """Independent real-time RMS levels ``(mic_level, system_level)``."""
        return float(self._mic_level), float(self._system_level)

    def discard_current_utterance(self) -> None:
        """Drop buffered audio, VAD state, and boundary lookahead for the active utterance."""
        with self._lock:
            for lane in self._lanes:
                lane.chunks = []
                lane.pending = np.zeros(0, dtype=np.float32)
        self._segmenter.reset()
        for seg in self._split_segmenters.values():
            seg.reset()
        if self._boundary_buffer is not None:
            self._boundary_buffer = type(self._boundary_buffer)()
        self._split_boundaries = {
            role: type(buffer)() for role, buffer in self._split_boundaries.items()
        }
        self._split_ready = []
        self._split_targets = {role: (0, "") for role in self._split_segmenters}
        self._open_target = (0, "")
        self._segment_target = (0, "")
        self._segment_route = ""
        self._segment_route_reliable = False
        self._segment_captured_at = 0
        self._segment_started_at = 0
        self._block_started_at = 0
        self._stream_anchor_ms = 0
        self._last_loopback_pad_at = 0.0

    def set_paused(self, paused: bool) -> None:
        """Pause or resume mixed-mode silent-loopback elapsed time tracking."""
        want = bool(paused)
        if want == self._is_paused:
            return
        now = time.monotonic()
        if want:
            if self._mixed_unpaused_since > 0.0:
                self._mixed_elapsed_before_pause += max(0.0, now - self._mixed_unpaused_since)
            self._mixed_unpaused_since = 0.0
        else:
            self._mixed_unpaused_since = now
        self._is_paused = want

    def _mixed_unpaused_elapsed(self, now: float) -> float:
        elapsed = self._mixed_elapsed_before_pause
        if not self._is_paused and self._mixed_unpaused_since > 0.0:
            elapsed += max(0.0, now - self._mixed_unpaused_since)
        return elapsed

    def set_recovery_session_id(self, session_id: str) -> None:
        """Associate the next recovery journal with a meeting/session ID for cleanup."""
        cleaned = str(session_id or "").strip()
        if cleaned != self._recovery_session_id:
            self._last_session_journal = None
            # A new meeting may be reminded once about unrecorded playback;
            # pausing and resuming the same one must not repeat it.
            self._playback_hits = []
            self._playback_warned = False
        self._recovery_session_id = cleaned

    def suppress_system_loopback_ms(self, duration_ms: int = 180) -> None:
        """Briefly mute the system loopback lane so SayInk's own start beep is not captured."""
        self._mute_system_until = max(
            self._mute_system_until,
            time.monotonic() + max(0.0, float(duration_ms) / 1000.0),
        )

    def seconds_since_last_speech(self) -> float | None:
        """How long the open stream has been without speech, or None if idle."""
        if not self._is_recording or self._last_speech_at <= 0:
            return None
        return max(0.0, time.monotonic() - self._last_speech_at)

    def _warn_overflow(self, role):
        now = time.monotonic()
        with self._overflow_lock:
            if now - self._last_overflow_warning < 5:
                return
            self._last_overflow_warning = now
        self.warning.emit(f"音频设备报告输入溢出（{role}），这一时刻可能有录音缺口；已继续采集后续声音。")

    def set_quiet_speech(self, enabled: bool | None = None):
        self._quiet_speech = None if enabled is None else bool(enabled)
        self._apply_quiet_mode(self._segmenter)
        for seg in self._split_segmenters.values():
            self._apply_quiet_mode(seg)

    def _apply_quiet_mode(self, segmenter: SpeechSegmenter) -> None:
        if segmenter.gate_name == VAD_SILERO:
            segmenter.quiet_boost = self._quiet_speech
        else:
            segmenter.quiet_boost = False

    def set_separate_lanes(self, enabled: bool) -> None:
        self._separate_lanes_requested = bool(enabled)

    def _prepare_split_segmenters(self):
        self._split_segmenters = {}
        self._split_boundaries = {}
        self._split_targets = {}
        self._split_ready = []
        if not self._separate_lanes_requested or {lane.endpoint.role for lane in self._lanes} != {"microphone", "system"}:
            return
        from sayink.asr_context import BoundaryContextBuffer
        for role, threshold in (("microphone", 0.002), ("system", 0.0006)):
            if self._meeting_profile:
                segmenter = make_meeting_segmenter(
                    speech_threshold=threshold,
                    vad=self._vad_kind,
                    model_path=self._vad_model_path,
                    gate_factory=lambda seg, r=role: self._split_speech_gate(r, seg),
                )
            else:
                segmenter = SpeechSegmenter(speech_threshold=threshold)
                segmenter.set_gate(self._split_speech_gate(role, segmenter))
            self._split_segmenters[role] = segmenter
            if self._vad_kind == VAD_SILERO and segmenter.gate_name != VAD_SILERO:
                self.warning.emit(f"{role} 的语音检测模型未能启用，使用能量断句；本路轻声增强未启用。")
            self._apply_quiet_mode(segmenter)
            self._split_targets[role] = (0, "")
            if self._boundary_requested:
                self._split_boundaries[role] = BoundaryContextBuffer()

    def _emit_split_segment(self, role, segment):
        seg = self._split_segmenters[role]
        self._segment_route = "mic" if role == "microphone" else "system"
        self._segment_captured_at = self._stream_anchor_ms + int(seg.last_bounds[0] * 1000 / TARGET_SAMPLE_RATE)
        self._segment_target = self._split_targets[role]
        acoustics = (getattr(segment, "noise_floor", None), float(getattr(segment, "quiet_level", 0.0)))
        metadata = (self._segment_route, self._segment_captured_at, self._segment_target, acoustics)
        boundary = self._split_boundaries.get(role)
        if boundary is not None:
            boundary.capture(segment, seg.last_bounds, seg.last_forced, metadata)
            self._release_split_context(role)
        else:
            self._split_ready.append((segment, metadata))

    def _release_split_context(self, role, *, ending=False):
        boundary = self._split_boundaries.get(role)
        if boundary is not None:
            for audio, metadata in boundary.release(ending=ending):
                self._split_ready.append((audio, metadata))

    def _release_split_ready(self, *, ending=False):
        starts = [seg.pending_start for seg in self._split_segmenters.values()]
        starts.extend(boundary.pending_start for boundary in self._split_boundaries.values()
                      if boundary.pending_start is not None)
        watermark = self._stream_anchor_ms + min(starts) * 1000 / TARGET_SAMPLE_RATE
        self._split_ready.sort(key=lambda item: item[1][1])
        released = 0
        while self._split_ready and (ending or self._split_ready[0][1][1] <= watermark):
            audio, metadata = self._split_ready.pop(0)
            self._segment_route, self._segment_captured_at, self._segment_target, acoustics = metadata
            audio = attach_segment_acoustics(audio, noise_floor=acoustics[0], quiet_level=acoustics[1])
            self._segment_route_reliable = True
            self._emit_segment_ready(audio)
            released += 1
        return released

    def _feed_split_tracks(self, mixed):
        if self._journal is not None:
            self._journal.append(mixed, tracks=self._recovery_tracks if len(self._recovery_tracks) == 2 else None)
        if not self._stream_anchor_ms:
            self._stream_anchor_ms = int(time.time() * 1000) - int(len(mixed) * 1000 / TARGET_SAMPLE_RATE)
        emitted = []
        for role, seg in self._split_segmenters.items():
            track = self._block_tracks.get(role, np.zeros(len(mixed), np.float32))
            boundary = self._split_boundaries.get(role)
            if boundary is not None:
                boundary.feed(track)
                self._release_split_context(role)
            was_in_speech = seg.in_speech
            audio = seg.feed(track)
            if not was_in_speech and (seg.in_speech or audio is not None):
                self._remember_open_target()
                self._split_targets[role] = self._open_target
            if audio is not None:
                emitted.append((seg.last_bounds[0], role, audio))
        # Segments becoming ready on the same tick follow capture time.
        for _start, role, audio in sorted(emitted, key=lambda item: (item[0], item[1])):
            self._emit_split_segment(role, audio)
        return self._release_split_ready()

    def set_boundary_context(self, enabled: bool) -> None:
        self._boundary_requested = bool(enabled)

    def set_recovery_directory(self, root) -> None:
        """Apply to the next recording, never start persisting an active one."""
        self._recovery_root = root

    @property
    def recovery_available(self) -> bool:
        return self._journal is not None and self._journal.healthy

    @property
    def recovery_journal_path(self):
        return self._journal.path if self._journal is not None else None

    @property
    def recovery_pending_dir(self):
        return self._journal.path / "pending" if self._journal is not None else None

    def _close_journal(self) -> None:
        if self._journal is not None:
            if not self._journal.close():
                self._closing_journals.append(self._journal)
            self._journal = None
        if not self._meeting_profile:
            self._recovery_session_id = ""
            self._last_session_journal = None

    @property
    def storage_busy(self) -> bool:
        self._closing_journals = [journal for journal in self._closing_journals if journal.is_writing]
        return bool(self._closing_journals)

    def configure(
        self,
        input_source: str = INPUT_SOURCE_MICROPHONE,
        mic_device_index: int = -1,
        system_device_index: int = -1,
    ):
        self._input_source = input_source
        self._mic_device_index = int(mic_device_index)
        self._system_device_index = int(system_device_index)
        self._rebuild_segmenter()

    def set_segmenter_profile(
        self,
        *,
        vad: str = VAD_ENERGY,
        vad_model_path: str | None = None,
        meeting: bool = False,
    ) -> None:
        """Use the shared VAD choice with mode-specific pause lengths.
        Meeting mode holds longer; both profiles can use Silero."""
        self._vad_kind = vad or VAD_ENERGY
        self._vad_model_path = str(vad_model_path) if vad_model_path else None
        self._meeting_profile = bool(meeting)
        # Mixed capture balances the two lanes in every profile: dictation
        # listening to a call has the same quiet-remote-party problem.
        self._balancer = LaneBalancer()
        self._lane_gains = {}
        self._duplicate_suppressor.reset()
        self._rebuild_segmenter()

    def _rebuild_segmenter(self) -> None:
        if self._is_recording and not self._is_cancelled:
            # A settings change while listening (source / device / VAD): the
            # utterance buffered in the old segmenter must come out before the
            # new one takes over, or those words vanish without a trace.
            emitted = self._flush_continuous_segments()
            emitted += self._release_boundary_context(ending=True)
            if self._boundary_buffer is not None:
                self._boundary_buffer = type(self._boundary_buffer)()
            # configure() starts a new sample clock. Any active independent
            # lanes and their context must start that same clock too.
            for active in self._split_segmenters.values():
                active.reset()
            self._split_boundaries = {role: type(buffer)() for role, buffer in self._split_boundaries.items()}
            self._split_ready = []
            if emitted:
                log.info("录音中重建断句器，先送出 %d 段已缓冲语音", emitted)
        if self._vad_kind == "silero" and self._vad_model_path:
            self._segmentation_status = SegmentationStatus(
                self._vad_kind, "", True, meeting=self._meeting_profile,
            )
            self.segmentation_changed.emit(self._segmentation_status)
        threshold = 0.0006 if self._input_source == INPUT_SOURCE_SYSTEM else 0.002
        if self._meeting_profile:
            segmenter = make_meeting_segmenter(
                speech_threshold=threshold, gate_factory=self._speech_gate,
            )
        else:
            segmenter = SpeechSegmenter(speech_threshold=threshold)
            segmenter.set_gate(self._speech_gate(segmenter))
        gate = segmenter.gate
        self._apply_quiet_mode(segmenter)
        self._segmenter = segmenter
        self._stream_anchor_ms = 0
        if self._is_recording and not self._is_cancelled and self._lanes:
            self._prepare_split_segmenters()
        self._segmentation_status = SegmentationStatus(
            self._vad_kind, gate.name, bool(self._vad_model_path),
            gate.fallback_reason, self._meeting_profile,
        )
        self.segmentation_changed.emit(self._segmentation_status)

    def _speech_gate(self, segmenter: SpeechSegmenter):
        path = self._vad_model_path or ""
        if self._vad_kind == VAD_SILERO and path:
            cached = self._silero_gate
            if cached is not None and self._silero_gate_path == path:
                return cached
        gate = make_speech_gate(self._vad_kind, segmenter, model_path=self._vad_model_path)
        if gate.name == VAD_SILERO and path:
            self._silero_gate = gate
            self._silero_gate_path = path
        return gate

    def _split_speech_gate(self, role: str, segmenter: SpeechSegmenter):
        path = self._vad_model_path or ""
        if self._vad_kind == VAD_SILERO and path:
            cached = self._split_silero_gates.get(role)
            if cached is not None and self._split_silero_gate_paths.get(role) == path:
                cached.reset()
                return cached
        gate = make_speech_gate(self._vad_kind, segmenter, model_path=self._vad_model_path)
        if gate.name == VAD_SILERO and path:
            self._split_silero_gates[role] = gate
            self._split_silero_gate_paths[role] = path
        return gate

    @property
    def segmentation_status(self) -> SegmentationStatus:
        return self._segmentation_status

    @property
    def vad_name(self) -> str:
        """Which gate the segmenter is actually using (energy / silero)."""
        return self._segmenter.gate_name

    @property
    def vad_backend(self) -> str:
        """Alias of :attr:`vad_name` for pipeline/coordinator queries."""
        return self._segmenter.gate_name

    @property
    def input_source(self) -> str:
        return self._input_source

    @property
    def input_source_display(self) -> str:
        return input_source_label(self._input_source)

    def _make_callback(self, lane: _CaptureLane) -> Callable:
        def _callback(indata, _frames, _time_info, status):
            if status and getattr(status, "input_overflow", False) is True:
                self._warn_overflow(lane.endpoint.role)
            if not self._is_recording or self._is_cancelled:
                return
            # The level meter is fed once per tick from the balanced mix
            # (_on_continuous_tick); emitting the raw per-lane block here as
            # well made the meter flicker between two scales.
            mono = to_mono(indata)
            block = (
                np.array(mono, dtype=np.float32, copy=True)
                if mono.dtype != np.float32 or np.shares_memory(mono, indata)
                else mono
            )
            with self._lock:
                lane.append_chunk(block)
        return _callback

    def _ensure_pawp(self):
        if self._pawp is None:
            import pyaudiowpatch as pyaudio

            self._pawp = pyaudio.PyAudio()
        return self._pawp

    def _retire_pawp(self, thread: object | None = None) -> None:
        if self._pawp is not None:
            self._retired_pawp.append((self._pawp, thread))
            self._pawp = None

    def _terminate_pawp(self):
        if self._pawp is not None:
            try:
                self._pawp.terminate()
            except Exception:
                log.debug("释放 PyAudioWPatch 失败", exc_info=True)
            self._pawp = None
        if self._retired_pawp:
            still_alive = []
            for entry in self._retired_pawp:
                pa, th = entry if isinstance(entry, tuple) else (entry, None)
                if th is not None and getattr(th, "is_alive", lambda: False)():
                    still_alive.append((pa, th))
                    continue
                try:
                    pa.terminate()
                except Exception:
                    log.debug("释放延迟退役 PyAudioWPatch 失败", exc_info=True)
            self._retired_pawp = still_alive

    def _pawp_read_loop(self, lane: _CaptureLane, stream, channels: int, rate: int):
        """Blocking read loop — stream_callback is unreliable on some Windows drivers."""
        frame_count = max(1, int(rate * self.CHUNK_DURATION))
        stop_event = lane.pawp_stop
        while (
            self._is_recording
            and not self._is_cancelled
            and stop_event is not None
            and not stop_event.is_set()
        ):
            try:
                with lane.pawp_lock:
                    if stop_event.is_set() or lane.pawp_stream is None:
                        break
                    avail = stream.get_read_available()
                    if avail <= 0:
                        data = None
                    else:
                        to_read = min(avail, frame_count)
                        data = stream.read(to_read, exception_on_overflow=True)
                if data is None:
                    time.sleep(0.02)
                    continue
            except Exception as e:
                if getattr(e, "errno", None) == -9981 or "overflow" in str(e).lower():
                    self._warn_overflow(lane.endpoint.role)
                    continue
                if stop_event is not None and not stop_event.is_set():
                    lane.stream_failed = True
                log.debug("PyAudioWPatch 读取失败: %s", e)
                break
            if not data:
                continue
            arr = np.frombuffer(data, dtype=np.float32)
            if channels > 1:
                block_mono = to_mono(arr.reshape(-1, channels))
            else:
                block_mono = arr.copy()
            with self._lock:
                lane.append_chunk(block_mono)

    def _open_pawp_lane(self, lane: _CaptureLane):
        import pyaudiowpatch as pyaudio

        ep = lane.endpoint
        p = self._ensure_pawp()
        info = resolve_pawp_stream_device_info(p, ep.device.index, expected_name=ep.device.name)
        pawp_idx = int(info["index"])
        rate = int(info.get("defaultSampleRate") or ep.device.sample_rate or 48000)
        channels = int(info.get("maxInputChannels", 0) or ep.device.channels or 1)
        channels = max(1, channels)
        frame_count = max(1, int(rate * self.CHUNK_DURATION))

        stream = p.open(
            format=pyaudio.paFloat32,
            channels=channels,
            rate=rate,
            input=True,
            input_device_index=pawp_idx,
            frames_per_buffer=frame_count,
        )
        stream.start_stream()
        lane.pawp_stream = stream
        lane.sample_rate = rate
        lane.stream_failed = False
        lane.pawp_stop = threading.Event()
        lane.pawp_thread = threading.Thread(
            target=self._pawp_read_loop,
            args=(lane, stream, channels, rate),
            daemon=True,
            name=f"pawp-{ep.role}",
        )
        lane.pawp_thread.start()
        log.info(
            "已打开音频流 [%s] %s @ %d Hz, %d 声道 (PyAudioWPatch 环回)",
            ep.role,
            ep.device.name,
            rate,
            channels,
        )

    def _open_lane_stream(self, lane: _CaptureLane):
        ep = lane.endpoint
        if is_encoded_pawp_device_index(ep.device.index):
            self._open_pawp_lane(lane)
            return
        info = _device_info_by_index(ep.device.index)
        device = ep.device.index

        in_ch = int(info.get("max_input_channels", 0) or 0)
        out_ch = int(info.get("max_output_channels", 0) or 0)
        channels = ep.device.channels
        if ep.use_wasapi_loopback and out_ch > 0:
            channels = out_ch
        elif in_ch > 0:
            channels = min(in_ch, 2)
        channels = max(1, channels)

        default_rate = int(info.get("default_samplerate") or ep.device.sample_rate or 48000)
        # WASAPI shared-mode loopback only opens at the render mix rate (almost always 48 kHz),
        # so probing 16 kHz first wasted a guaranteed-failing COM open on every start.
        if ep.use_wasapi_loopback:
            base_rates = (default_rate, 48000, 44100, TARGET_SAMPLE_RATE)
        else:
            base_rates = (TARGET_SAMPLE_RATE, default_rate, 48000, 44100)
        sample_rates = tuple(dict.fromkeys(base_rates))
        loopback_attempts = (True, False) if ep.use_wasapi_loopback else (False,)

        cache_key = (int(device), ep.device.name, bool(ep.use_wasapi_loopback))
        cached = _WORKING_STREAM_PARAMS.get(cache_key)
        attempts: list[tuple[bool, int]] = []
        if cached is not None:
            cached_rate, cached_loopback = cached
            if cached_loopback in loopback_attempts:
                attempts.append((cached_loopback, cached_rate))
        for try_loopback in loopback_attempts:
            for rate in sample_rates:
                pair = (try_loopback, rate)
                if pair not in attempts:
                    attempts.append(pair)

        last_err: Optional[Exception] = None
        wasapi_unsupported = False

        for try_loopback, rate in attempts:
            extra = None
            if try_loopback:
                if wasapi_unsupported:
                    continue
                try:
                    extra = sd.WasapiSettings(loopback=True)
                except TypeError:
                    wasapi_unsupported = True
                    log.debug("此 sounddevice 不支持 WASAPI loopback 参数，改用普通输入流")
                    continue
            stream = None
            try:
                kwargs = dict(
                    samplerate=rate,
                    channels=channels,
                    dtype="float32",
                    blocksize=int(rate * self.CHUNK_DURATION),
                    callback=self._make_callback(lane),
                )
                if extra is not None:
                    kwargs["extra_settings"] = extra
                stream = sd.InputStream(device=device, **kwargs)
                stream.start()
                lane.stream = stream
                lane.sample_rate = rate
                lane.stream_failed = False
                mark_portaudio_stream_active(+1)
                lane.stream_registered = True
                _WORKING_STREAM_PARAMS[cache_key] = (rate, try_loopback)
                log.info(
                    "已打开音频流 [%s] %s @ %d Hz (loopback=%s)",
                    ep.role,
                    ep.device.name,
                    rate,
                    try_loopback,
                )
                return
            except Exception as e:
                last_err = e
                if cached is not None and (rate, try_loopback) == cached:
                    _WORKING_STREAM_PARAMS.pop(cache_key, None)
                log.debug(
                    "打开设备失败 %s @ %d loopback=%s: %s",
                    ep.device.name,
                    rate,
                    try_loopback,
                    e,
                )
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        log.debug("关闭半开的音频流失败", exc_info=True)

        raise RuntimeError(f"无法打开音频设备：{ep.device.name}") from last_err

    def _system_device_candidates(self, primary: StreamEndpoint) -> list[StreamEndpoint]:

        ordered = ordered_system_devices(self._system_device_index)
        reliable = [d for d in ordered if not is_unreliable_loopback_output(d)]
        pool = reliable if reliable else ordered
        pool = [primary.device] + [d for d in pool if d.index != primary.device.index]
        return [
            StreamEndpoint("system", d, should_use_wasapi_loopback(d))
            for d in pool
        ]

    def _open_system_lane_with_fallback(self, primary: StreamEndpoint) -> Optional[_CaptureLane]:
        last_err: Optional[Exception] = None
        for ep in self._system_device_candidates(primary):
            lane = _CaptureLane(ep)
            try:
                self._open_lane_stream(lane)
                if ep.device.index != primary.device.index:
                    warn = (
                        f"电脑声音改用「{ep.device.name}」。"
                        "如果正在播放的声音不从这台设备出来，将录不到声音。"
                    )
                    log.info(warn)
                    self._last_start_warning = warn
                    self.warning.emit(warn)
                return lane
            except Exception as e:
                last_err = e
                log.debug("系统设备打开失败 %s: %s", ep.device.name, e)
        if last_err is not None:
            log.warning("所有系统声音设备均无法打开（末次错误: %s）", last_err)
        return None

    @property
    def last_start_warning(self) -> str:
        return self._last_start_warning

    @property
    def capture_device_labels(self) -> tuple[str, ...]:
        """Devices actually opened, including a successful fallback device."""
        return tuple(
            f"{'麦克风' if lane.endpoint.role == 'microphone' else '电脑声音'}：{lane.endpoint.device.name}"
            for lane in self._lanes
        )

    @staticmethod
    def _time_warp_slice(samples: np.ndarray, target_len: int) -> np.ndarray:
        """Gentle linear time-warp for compensating inter-device crystal clock drift."""
        if samples.size == target_len or target_len <= 0 or samples.size <= 1:
            return samples[:target_len]
        src_x = np.linspace(0.0, float(samples.size - 1), target_len, dtype=np.float64)
        return np.interp(src_x, np.arange(samples.size, dtype=np.float64), samples).astype(np.float32)

    @staticmethod
    def _pad_stalled_track(track: np.ndarray, take: int) -> np.ndarray:
        """Pad a stalled/silent lane to ``take`` with a tiny 2 ms fade-out to avoid step clicks."""
        if track.size >= take:
            return track[:take]
        if track.size > 0 and abs(float(track[-1])) > 1e-4:
            fade_len = min(32, track.size)
            track = track.copy()
            track[-fade_len:] *= np.linspace(1.0, 0.0, fade_len, endpoint=False, dtype=np.float32)
        return np.pad(track, (0, take - track.size))

    def _drain_mixed_mono(self, final: bool = False) -> Optional[np.ndarray]:
        """Mix what every lane has delivered so far with drift compensation."""
        now = time.monotonic()
        drained: list[tuple[_CaptureLane, Optional[np.ndarray], float]] = []
        with self._lock:
            for lane in self._lanes:
                new = lane.chunks
                lane.chunks = []
                drained.append((lane, np.concatenate(new) if new else None, lane.last_chunk_at))
        for lane, raw, _last in drained:
            if raw is None or raw.size == 0:
                continue
            if lane.resampler is None or lane.resampler.source_rate != lane.sample_rate:
                lane.resampler = StreamResampler(lane.sample_rate, TARGET_SAMPLE_RATE)
            track = lane.resampler.process(raw)
            lane.pending = np.concatenate([lane.pending, track]) if lane.pending.size else track

        longest = max((lane.pending.size for lane, _raw, _last in drained), default=0)
        both_live = (
            not final
            and len(drained) > 1
            and all((now - last) < self.MIX_WAIT_SEC for _lane, _raw, last in drained)
        )
        if final or len(drained) <= 1:
            take = longest
        else:
            waiting = [
                lane.pending.size
                for lane, _raw, last in drained
                if now - last < self.MIX_WAIT_SEC
            ]
            take = min(waiting) if waiting else longest
            take = max(take, longest - int(self.MAX_MIX_SKEW_SEC * TARGET_SAMPLE_RATE))
        if take <= 0:
            self._block_mic_energy = 0.0
            self._block_system_energy = 0.0
            self._recovery_tracks = {}
            return None

        lane_tracks = []
        populated_roles = set()
        for lane, _raw, _last in drained:
            excess = lane.pending.size - take
            if both_live and take >= 160 and excess > self.DRIFT_TOLERANCE_SAMPLES:
                lane.drift_ticks = getattr(lane, "drift_ticks", 0) + 1
            else:
                lane.drift_ticks = 0
            # When all lanes are actively delivering callbacks and excess persists across
            # sustained ticks (real crystal clock drift rather than 1-block callback jitter),
            # gently absorb drift via <=0.5% linear resampling instead of accumulating to
            # 1.0s and injecting hard zero-padded dropouts.
            if lane.drift_ticks >= self.DRIFT_SUSTAIN_TICKS:
                extra = min(
                    excess - (self.DRIFT_TOLERANCE_SAMPLES // 2),
                    max(1, take // 200),
                )
                consume = take + max(1, extra)
                raw_slice = lane.pending[:consume]
                lane.pending = lane.pending[consume:]
                track = self._time_warp_slice(raw_slice, take)
            else:
                track = lane.pending[:take]
                lane.pending = lane.pending[take:]
            if track.size:
                populated_roles.add(lane.endpoint.role)
            if track.size < take:
                track = self._pad_stalled_track(track, take)
            if lane.endpoint.role == "system" and now < self._mute_system_until:
                track = np.zeros_like(track)
            lane_tracks.append((lane.endpoint.role, track))

        roles = [role for role, _track in lane_tracks]
        self._recovery_tracks = dict(lane_tracks)

        # Measure raw/cleaned per-lane levels before mixing so mic speech never masks a dead system lane.
        raw_sys_rms = 0.0
        for role, track in lane_tracks:
            if role == "system" and role in populated_roles:
                raw_sys_rms = rms_volume(track)

        if sorted(roles) == ["microphone", "system"]:
            mic_index, system_index = roles.index("microphone"), roles.index("system")
            cleaned = self._duplicate_suppressor.process(lane_tracks[mic_index][1], lane_tracks[system_index][1])
            lane_tracks[mic_index] = ("microphone", cleaned)
        else:
            self._duplicate_suppressor.reset()
        self._block_tracks = {role: track for role, track in lane_tracks}

        mic_rms = 0.0
        for role, track in lane_tracks:
            if role != "system" and role in populated_roles:
                mic_rms = max(mic_rms, rms_volume(track))
        self._mic_level = mic_rms
        self._system_level = raw_sys_rms
        if mic_rms >= 0.002:
            self._last_mic_speech_at = now
        if raw_sys_rms >= self.SYSTEM_ACTIVE_RMS:
            self._last_system_speech_at = now
            self._system_ever_active = True

        tracks: list[np.ndarray] = []
        mix_roles: list[str] = []
        gains: list[float] = []
        mic_energy = 0.0
        system_energy = 0.0
        for role, track in lane_tracks:
            if role not in populated_roles:
                continue
            energy = float(np.dot(track, track))
            if role == "system":
                system_energy += energy
            else:
                mic_energy += energy
            tracks.append(track)
            mix_roles.append(role)
            if self._balancer is not None:
                gains.append(self._balancer.observe(role, track))
        self._block_mic_energy = mic_energy
        self._block_system_energy = system_energy
        if not tracks:
            return np.zeros(take, dtype=np.float32)
        audible = [index for index, track in enumerate(tracks) if rms_volume(track) >= _DIGITAL_SILENCE_RMS]
        if len(tracks) == 1:
            role = mix_roles[0]
            only = (
                self._glide_lane_gain(role, tracks[0], 1.0)
                if (audible or role in self._lane_gains)
                else tracks[0]
            )
            if only.size < take:
                only = np.concatenate([only, np.zeros(take - only.size, dtype=np.float32)])
            return only
        if len(audible) == 1:
            # The only lane with sound is not balanced against a silent one.
            index = audible[0]
            only = self._glide_lane_gain(mix_roles[index], tracks[index], 1.0)
            if only.size < take:
                only = np.concatenate([only, np.zeros(take - only.size, dtype=np.float32)])
            return only
        if self._balancer is not None and len(gains) == len(tracks):
            if not audible:
                # Two-lane silence: do not ramp gains back toward 4.0x balancer
                # targets, and do not return digital zeros (preserve VAD noise
                # floor tracking and SileroGate input_gain). Glide already-heard
                # lanes gently toward 1.0 while leaving unheard lanes unseeded.
                scaled = [
                    self._glide_lane_gain(role, track, 1.0) if role in self._lane_gains else track
                    for role, track in zip(mix_roles, tracks, strict=True)
                ]
            else:
                scaled = [
                    self._glide_lane_gain(role, track, gain)
                    for role, track, gain in zip(mix_roles, tracks, gains, strict=True)
                ]
            return mix_to_mono(scaled, TARGET_SAMPLE_RATE, gains=[1.0] * len(scaled))
        return mix_to_mono(tracks, TARGET_SAMPLE_RATE)

    def _glide_lane_gain(self, role: str, track: np.ndarray, target: float) -> np.ndarray:
        """Scale one lane, moving its gain toward ``target`` without a step.

        A lane is balanced (up to x4 or down to x1/4) only while the other lane
        also carries sound. Switching straight between the two gains stepped the
        level by up to 12 dB between 100 ms blocks every time the other party
        started or stopped. The gain now ramps inside each block and covers a
        quarter of the remaining distance per block; a lane heard for the first
        time starts at its target, so nothing is faded in.
        """
        start = self._lane_gains.get(role, target)
        end = start + self.LANE_GAIN_GLIDE * (target - start)
        if abs(end - target) < 1e-3:
            end = target
        self._lane_gains[role] = end
        if start == end:
            return track if end == 1.0 else track * np.float32(end)
        return track * np.linspace(start, end, track.size, endpoint=False, dtype=np.float32)

    def _reset_continuous_speech_watch(self) -> None:
        now = time.monotonic()
        self._last_speech_at = now
        self._no_speech_warned = False

    def _lost_lane(self) -> Optional[_CaptureLane]:
        """Detect a capture lane whose hardware stream died or stalled mid-session."""
        now = time.monotonic()
        for lane in self._lanes:
            if getattr(lane, "stream_failed", False):
                return lane
            if lane.pawp_thread is not None and not lane.pawp_thread.is_alive():
                return lane
            stream = lane.stream
            if stream is not None and (
                getattr(stream, "active", True) is False
                or getattr(stream, "stopped", False) is True
            ):
                return lane
            if stream is not None and lane.endpoint.role != "system":
                if now - lane.last_chunk_at > self.LANE_STALL_SEC:
                    return lane
        return None

    def _recover_system_lane(self, lost_lane: _CaptureLane, *, target_device=None, reason: str = "lost") -> bool:
        """Attempt seamless self-healing / hot-rebind of the system loopback lane."""
        old_name = strip_loopback_suffix(lost_lane.endpoint.device.name)
        self._close_lane(lost_lane)
        if not any(
            ln is not lost_lane and getattr(ln, "pawp_stream", None) is not None
            for ln in self._lanes
        ):
            self._terminate_pawp()
        invalidate_device_cache()
        if not any(
            ln is not lost_lane and ln.stream is not None
            for ln in self._lanes
        ):
            refresh_audio_devices()

        active_mic_name = ""
        for ln in self._lanes:
            if ln is not lost_lane and ln.endpoint.role == "microphone":
                active_mic_name = ln.endpoint.device.name
                break

        candidate_dev = target_device
        if candidate_dev is None:
            try:
                candidate_dev = pick_default_system_capture(active_mic_name=active_mic_name)
            except TypeError:
                candidate_dev = pick_default_system_capture()
        if candidate_dev is None:
            return False

        primary = StreamEndpoint("system", candidate_dev, should_use_wasapi_loopback(candidate_dev))
        reopened = self._open_system_lane_with_fallback(primary)
        if reopened is None:
            return False

        with self._lock:
            for idx, ln in enumerate(self._lanes):
                if ln is lost_lane:
                    self._lanes[idx] = reopened
                    break
            else:
                self._lanes.append(reopened)
        self._duplicate_suppressor.reset()
        new_name = strip_loopback_suffix(reopened.endpoint.device.name)
        if reason == "switch" or new_name != old_name:
            msg = f"电脑声音输出已切换至「{new_name}」，已自动跟随采集。"
        else:
            msg = f"电脑声音通道「{new_name}」已自动恢复连接。"
        log.info(msg)
        self.warning.emit(msg)
        return True

    def _poll_system_route_and_silence(self) -> None:
        """Follow active Bluetooth/speaker output switches and warn after 15s of dead system audio."""
        now = time.monotonic()
        sys_lane = next((ln for ln in self._lanes if ln.endpoint.role == "system"), None)

        # 1. Auto-follow output endpoint switches (e.g. Stereo -> Hands-Free AG Audio eCommunications)
        if (
            self._continuous_mode
            and self._route_watch_enabled
            and sys_lane is not None
            and self._system_device_index < 0
            and (now - self._last_route_poll_at) >= self.SYSTEM_ROUTE_POLL_SEC
            and (self._last_system_speech_at <= 0.0 or (now - self._last_system_speech_at) >= 1.5)
        ):
            self._last_route_poll_at = now
            active_mic_name = next(
                (ln.endpoint.device.name for ln in self._lanes if ln.endpoint.role == "microphone"),
                "",
            )
            preferred = preferred_windows_loopback_names(active_mic_name=active_mic_name)
            if preferred and not endpoint_names_match(sys_lane.endpoint.device.name, preferred[0]):
                try:
                    invalidate_device_cache(keep_win_endpoints=True)
                except TypeError:
                    invalidate_device_cache()
                try:
                    next_dev = pick_default_system_capture(active_mic_name=active_mic_name)
                except TypeError:
                    next_dev = pick_default_system_capture()
                if next_dev is not None and not endpoint_names_match(
                    sys_lane.endpoint.device.name, next_dev.name
                ):
                    self._recover_system_lane(sys_lane, target_device=next_dev, reason="switch")
                    sys_lane = next((ln for ln in self._lanes if ln.endpoint.role == "system"), None)

        # 2. Independent 15s mixed-mode silent-system warning (never masked by user's own mic speech)
        if (
            self._input_source == INPUT_SOURCE_MIXED
            and not self._is_paused
            and not self._system_silent_warned
            and not self._system_ever_active
            and self._mixed_unpaused_elapsed(now) >= self.SYSTEM_SILENT_WARN_SEC
        ):
            self._system_silent_warned = True
            if sys_lane is not None:
                dev_label = strip_loopback_suffix(sys_lane.endpoint.device.name)
            else:
                dev_label = describe_active_loopback_device(
                    self._system_device_index, self._mic_device_index
                ) or "未连接"
            warn_msg = (
                f"混合录音已持续 {int(self.SYSTEM_SILENT_WARN_SEC)} 秒，但电脑声音通道（{dev_label}）始终接近静音。"
                "线下会议可忽略；若您正佩戴蓝牙耳机开线上会议，请确认系统或会议软件输出已切至 Hands-Free 通话通道。"
            )
            log.info(warn_msg)
            self.system_silent_warning.emit(warn_msg)

        self._poll_unrecorded_playback(now)

    def _poll_unrecorded_playback(self, now: float) -> None:
        """Remind a microphone-only meeting once that the computer keeps playing sound.

        With headphones on, the remote side of a call never reaches the
        microphone, and the transcript silently holds one voice only.
        """
        if (
            self._playback_warned
            or not self._meeting_profile
            or not self._continuous_mode
            or self._input_source != INPUT_SOURCE_MICROPHONE
            or self._is_paused
            or 0.0 <= now - self._last_playback_poll_at < self.SYSTEM_ROUTE_POLL_SEC
        ):
            return
        self._last_playback_poll_at = now
        if _render_playback_peak() < self.UNRECORDED_PLAYBACK_PEAK:
            return
        window = self.UNRECORDED_PLAYBACK_WINDOW_SEC
        self._playback_hits = [at for at in self._playback_hits if 0.0 <= now - at <= window]
        self._playback_hits.append(now)
        if len(self._playback_hits) < self.UNRECORDED_PLAYBACK_HITS:
            return
        self._playback_warned = True
        self._playback_hits = []
        self.warning.emit(
            "电脑正在播放声音，但本场会议只录麦克风：戴耳机开线上会议时，对方的声音不会被录到。"
            f"如需同时录下电脑声音，请在开始会议时把音频来源选为「{input_source_label(INPUT_SOURCE_MIXED)}」。"
        )

    def _handle_lost_device(self, lane: _CaptureLane) -> None:
        name = lane.endpoint.device.name
        log.warning("音频设备已断开: %s", name)
        if lane.endpoint.role == "system" and self._continuous_mode:
            if self._route_watch_enabled and self._recover_system_lane(lane, reason="lost"):
                return
            if self._input_source == INPUT_SOURCE_MIXED and any(
                ln is not lane and ln.endpoint.role == "microphone" for ln in self._lanes
            ):
                self._close_lane(lane)
                with self._lock:
                    self._lanes = [ln for ln in self._lanes if ln is not lane]
                msg = f"电脑声音设备「{strip_loopback_suffix(name)}」已断开，当前继续使用麦克风录音。"
                self.warning.emit(msg)
                return
        if self._continuous_mode:
            self.stop_continuous()
        else:
            # Keep buffered speech, but let the device error finish an empty
            # hold. An extra "heard nothing" completion would mask that error.
            self._suppress_empty_finished = True
            try:
                self.stop()
            finally:
                self._suppress_empty_finished = False
        self.error.emit(f"音频设备已断开：{name}")

    def _single_system_lane_anchor(self) -> float | None:
        if len(self._lanes) > 1:
            return None
        if len(self._lanes) == 1:
            lane = self._lanes[0]
            if lane.endpoint.role != "system":
                return None
            return max(
                float(getattr(lane, "last_chunk_at", 0.0) or 0.0),
                float(self._last_real_block_at),
            )
        if self._input_source == INPUT_SOURCE_SYSTEM:
            return float(self._last_real_block_at)
        return None

    def _has_pending_loopback_speech(self) -> bool:
        seg = self._segmenter
        if bool(getattr(seg, "in_speech", False)):
            return True
        if int(getattr(seg, "_total_samples", 0) or 0) > 0:
            return True
        if bool(getattr(seg, "_buffer", None)):
            return True
        if self._boundary_buffer is not None and self._boundary_buffer.pending_start is not None:
            return True
        return False

    def _on_continuous_tick(self):
        if not self._is_recording or self._is_cancelled:
            return
        if not self._continuous_mode and not self._segment_live:
            return
        lost = self._lost_lane()
        if lost is not None:
            self._handle_lost_device(lost)
            if not self._is_recording:
                return
        block = self._drain_mixed_mono()
        self._poll_system_route_and_silence()
        now = time.monotonic()
        if block is None or block.size == 0:
            sys_anchor = self._single_system_lane_anchor()
            if sys_anchor is not None:
                if (now - sys_anchor) >= self.MIX_WAIT_SEC:
                    had_lane_level = self._mic_level > 0.0 or self._system_level > 0.0
                    self._mic_level = 0.0
                    self._system_level = 0.0
                    vol_was_nonzero = self._last_emitted_volume != 0.0
                    if vol_was_nonzero:
                        self._last_emitted_volume = 0.0
                        self.volume_changed.emit(0.0)
                    if had_lane_level or vol_was_nonzero:
                        self.lane_levels_changed.emit(0.0, 0.0)
                    if not self._is_paused and self._has_pending_loopback_speech():
                        step_sec = 0.1
                        pad_samples = max(1, int(self.sample_rate * step_sec))
                        cursor = self._last_loopback_pad_at
                        if cursor <= 0.0:
                            cursor = (
                                max(sys_anchor, now - 2.0)
                                if sys_anchor > 0.0
                                else (now - step_sec)
                            )
                        self._block_mic_energy = 0.0
                        self._block_system_energy = 0.0
                        self._recovery_tracks = {}
                        emitted_any = False
                        while (now - cursor) >= (step_sec - 1e-6) and self._has_pending_loopback_speech():
                            cursor += step_sec
                            zero_block = np.zeros(pad_samples, dtype=np.float32)
                            flushed_seg = self._feed_segmenter(zero_block)
                            if flushed_seg is not None and flushed_seg.size > 0:
                                emitted_any = True
                                self._last_speech_at = now
                                log.debug("环回暂停收尾切分片段: %d 采样点", flushed_seg.size)
                                self._emit_segment(flushed_seg)
                        self._last_loopback_pad_at = (
                            cursor if self._has_pending_loopback_speech() else 0.0
                        )
                        if not emitted_any:
                            self._warn_if_silent()
                        return
                    self._last_loopback_pad_at = 0.0
                self._warn_if_silent()
                return
            if self._mic_level > 0.0 or self._system_level > 0.0:
                self._mic_level = 0.0
                self._system_level = 0.0
                self.lane_levels_changed.emit(0.0, 0.0)
            self._warn_if_silent()
            return
        vol = rms_volume(block)
        self._last_real_block_at = now
        self._last_loopback_pad_at = 0.0
        self._last_emitted_volume = float(vol)
        self.volume_changed.emit(vol)
        self.lane_levels_changed.emit(self._mic_level, self._system_level)
        gate = getattr(self._segmenter, "effective_threshold", self._segmenter.speech_threshold)
        if vol >= gate:
            self._last_speech_at = now
        if self._split_segmenters:
            if self._feed_split_tracks(block):
                self._last_speech_at = now
            elif self._warn_if_silent():
                return
            return
        segment = self._feed_segmenter(block)
        if segment is not None and segment.size > 0:
            self._last_speech_at = now
            log.debug("持续监听切分片段: %d 采样点", segment.size)
            self._emit_segment(segment)
        else:
            self._warn_if_silent()

    def _warn_if_silent(self) -> bool:
        if (self._no_speech_warned
                or time.monotonic() - self._last_speech_at < self.NO_SPEECH_WARN_SEC):
            return False
        self._no_speech_warned = True
        self.no_speech_warning.emit()
        return True

    def start(self, *, continuous: bool = False):
        if self._is_recording:
            return

        self.last_hold_capture = None
        self._hold_capture_started_at = time.monotonic()
        self._hold_segments_emitted = 0

        # A hold can leave a window here, and hold-to-talk never consumes it.
        self._open_target = (0, "")
        self._segment_target = (0, "")
        self._continuous_mode = continuous
        from sayink.asr_context import BoundaryContextBuffer
        self._boundary_buffer = BoundaryContextBuffer() if self._boundary_requested else None
        self._segment_live = not continuous
        if continuous or self._segment_live:
            self._segmenter.reset()
            self._reset_continuous_speech_watch()
        self._is_cancelled = False
        self._is_paused = False
        self._route_watch_enabled = False
        self._mic_level = 0.0
        self._system_level = 0.0
        self._last_mic_speech_at = 0.0
        self._last_system_speech_at = 0.0
        self._system_ever_active = False
        self._system_silent_warned = False
        self._mixed_elapsed_before_pause = 0.0
        self._mixed_unpaused_since = time.monotonic()
        self._last_route_poll_at = self._mixed_unpaused_since
        self._last_playback_poll_at = self._mixed_unpaused_since
        self._last_emitted_volume = 0.0
        self._last_real_block_at = self._mixed_unpaused_since
        self._last_loopback_pad_at = 0.0
        self._lanes = []
        self._lane_gains = {}
        self._duplicate_suppressor.reset()
        self._stream_anchor_ms = 0
        self._last_start_warning = ""
        self._last_overflow_warning = float("-inf")

        try:
            self._plan = build_recording_plan(
                self._input_source,
                self._mic_device_index,
                self._system_device_index,
            )
        except RuntimeError as e:
            self.error.emit(str(e))
            return

        self._is_recording = True
        opened: list[_CaptureLane] = []
        try:
            for endpoint in self._plan.endpoints:
                if endpoint.role == "system":
                    lane = self._open_system_lane_with_fallback(endpoint)
                    if lane is None:
                        if self._input_source == INPUT_SOURCE_MIXED:
                            from sayink.pawp_capture import pawp_available

                            if pawp_available():
                                warn = (
                                    "无法打开电脑播放声（已尝试系统环回设备）。"
                                    "请确认扬声器或耳机处于启用状态，并在设置中刷新设备列表。"
                                )
                            else:
                                warn = (
                                    "无法采集电脑播放声；本次仅录麦克风。"
                                    "请在系统声音设置中确认扬声器或耳机已启用，"
                                    "或启用「立体声混音」后点「恢复自动选择」。"
                                )
                            self._last_start_warning = warn
                            log.warning(warn)
                            self.warning.emit(warn)
                            continue
                        raise RuntimeError(
                            f"无法打开音频设备：{endpoint.device.name}"
                        )
                    opened.append(lane)
                else:
                    lane = _CaptureLane(endpoint)
                    self._open_lane_stream(lane)
                    opened.append(lane)
            if not opened:
                raise RuntimeError("未打开任何音频采集通道")
            self._lanes = opened
            self._route_watch_enabled = bool(continuous)
            self._prepare_split_segmenters()
            if self._recovery_root is not None:
                try:
                    journal_meta = {
                        "source": self._input_source,
                        "meeting": self._meeting_profile,
                        "continuous": continuous,
                    }
                    resume_dir = None
                    if self._recovery_session_id:
                        journal_meta["session_id"] = self._recovery_session_id
                        if self._meeting_profile:
                            journal_meta["meeting_id"] = self._recovery_session_id
                        if (
                            self._last_session_journal is not None
                            and self._last_session_journal[0] == self._recovery_session_id
                        ):
                            prev_path = Path(self._last_session_journal[1])
                            if prev_path.is_dir() and not any(
                                j.path == prev_path and j.is_writing
                                for j in self._closing_journals
                            ):
                                resume_dir = prev_path
                    self._journal = RecordingJournal(
                        self._recovery_root,
                        metadata=journal_meta,
                        on_error=self.warning.emit,
                        resume_dir=resume_dir,
                    )
                    if self._recovery_session_id:
                        self._last_session_journal = (
                            self._recovery_session_id,
                            self._journal.path,
                        )
                except OSError as exc:
                    self.warning.emit(f"未能启用录音缓存：{exc}；本次仅在内存中处理")
            self._continuous_timer.start()
            if continuous:
                log.info("持续监听已开启（来源: %s）", self.input_source_display)
            else:
                log.info("按住录音已开启，识别结果会随分段出现（来源: %s）", self.input_source_display)
        except Exception as e:
            self._is_recording = False
            self._continuous_mode = False
            self._route_watch_enabled = False
            self._segment_live = False
            self._continuous_timer.stop()
            for lane in opened:
                self._close_lane(lane)
            self._lanes = []
            self.error.emit(self._friendly_open_error(str(e)))

    def start_continuous(self):
        self.start(continuous=True)

    def _flush_continuous_segments(self) -> int:
        """Drain VAD buffer so trailing speech is not lost on stop."""
        emitted = 0
        block = self._drain_mixed_mono(final=True)
        if self._split_segmenters:
            if block is not None and block.size:
                emitted += self._feed_split_tracks(block)
            for role, seg in self._split_segmenters.items():
                if (audio := seg.flush()) is not None:
                    self._emit_split_segment(role, audio)
                self._release_split_context(role, ending=True)
            emitted += self._release_split_ready(ending=True)
            return emitted
        if block is not None and block.size > 0:
            segment = self._feed_segmenter(block)
            if segment is not None and segment.size > 0:
                log.debug("持续监听收尾片段: %d 采样点", segment.size)
                emitted += self._emit_segment(segment)
        flushed = self._segmenter.flush()
        if flushed is not None and flushed.size > 0:
            log.debug("持续监听 flush 片段: %d 采样点", flushed.size)
            self._block_started_at = self._segment_started_at
            # flush() never passes through _feed_segmenter, so the window
            # captured when this utterance opened would otherwise be lost.
            self._segment_target = self._open_target
            emitted += self._emit_segment(flushed)
        return emitted

    def _feed_segmenter(self, block: np.ndarray) -> Optional[np.ndarray]:
        if self._journal is not None:
            self._journal.append(block, tracks=self._recovery_tracks if len(self._recovery_tracks) == 2 else None)
        if self._boundary_buffer is not None:
            self._boundary_buffer.feed(block)
            self._release_boundary_context()
        segmenter = self._segmenter
        was_in_speech = bool(getattr(segmenter, "in_speech", False))
        now_ms = int(time.time() * 1000)
        if not self._stream_anchor_ms:
            self._stream_anchor_ms = now_ms - int(block.size * 1000 / TARGET_SAMPLE_RATE)
        segment = segmenter.feed(
            block,
            mic_energy=self._block_mic_energy,
            system_energy=self._block_system_energy,
        )
        in_speech = bool(getattr(segmenter, "in_speech", False))
        if not was_in_speech and (segment is not None or in_speech):
            # The gate opened inside this block; the utterance started about
            # one block (plus pre-roll) before the block arrived.
            self._remember_open_target()
            self._segment_started_at = now_ms - int(self.CHUNK_DURATION * 1000)
        if segment is not None:
            self._block_started_at = self._segment_started_at
            self._segment_target = self._open_target
            if in_speech:
                # A carried tail already belongs to the next utterance.
                self._remember_open_target()
                self._segment_started_at = now_ms
        return segment

    def _remember_open_target(self) -> None:
        """Record the foreground window on the UI thread when speech opens."""
        from sayink.text_paster import (
            TextPaster,
            get_foreground_process_name,
            get_foreground_window_info,
            target_rejects_synthetic_input,
        )

        info = get_foreground_window_info()
        hwnd = int(info[0] or 0)
        name = get_foreground_process_name() if hwnd else ""
        if hwnd and (
            TextPaster._is_own_window(info)
            or target_rejects_synthetic_input(info)
        ):
            hwnd = 0
        self._open_target = (hwnd, name)
        self.utterance_opened.emit(hwnd, name)

    def _emit_segment(self, segment: np.ndarray) -> int:
        self._segment_route = self._segmenter.last_route
        self._segment_captured_at = self._block_started_at or int(time.time() * 1000)
        bounds = getattr(self._segmenter, "last_bounds", None)
        if self._stream_anchor_ms and bounds is not None and bounds[1] - bounds[0] == segment.size:
            self._segment_captured_at = self._stream_anchor_ms + int(bounds[0] * 1000 / TARGET_SAMPLE_RATE)
        if self._boundary_buffer is not None and bounds is not None:
            acoustics = (getattr(segment, "noise_floor", None), float(getattr(segment, "quiet_level", 0.0)))
            self._boundary_buffer.capture(
                segment,
                bounds,
                self._segmenter.last_forced,
                (self._segment_route, self._segment_captured_at, self._segment_target, acoustics),
            )
            return self._release_boundary_context()
        else:
            self._segment_route_reliable = False
            self._emit_segment_ready(segment)
            return 1

    def _release_boundary_context(self, *, ending=False):
        if self._boundary_buffer is None:
            return 0
        released = 0
        for audio, metadata in self._boundary_buffer.release(ending=ending):
            self._segment_route, self._segment_captured_at, self._segment_target, acoustics = metadata
            audio = attach_segment_acoustics(audio, noise_floor=acoustics[0], quiet_level=acoustics[1])
            self._segment_route_reliable = False
            self._emit_segment_ready(audio)
            released += 1
        return released

    def _emit_segment_ready(self, audio: np.ndarray) -> None:
        if not self._continuous_mode and audio.size:
            self._hold_segments_emitted += 1
        self.segment_ready.emit(audio)

    def consume_segment_route(self) -> str:
        """Return the route of the segment just emitted, then clear it."""
        route = self._segment_route
        self._segment_route = ""
        return route

    def consume_segment_route_reliable(self) -> bool:
        """True when the route is the lane that captured this segment.

        A mixed recording's dominant-energy guess is not reliable: splitting
        that chunk into turns must not stamp every turn with the chunk route.
        """
        reliable = bool(self._segment_route_reliable)
        self._segment_route_reliable = False
        return reliable

    def consume_segment_captured_at(self) -> int:
        """Wall-clock ms when the segment just emitted started (0 if unknown)."""
        stamp = self._segment_captured_at
        self._segment_captured_at = 0
        return int(stamp or 0)

    def consume_segment_target(self) -> tuple[int, str]:
        """Foreground window captured when this segment's speech opened."""
        target = self._segment_target
        self._segment_target = (0, "")
        if not isinstance(target, tuple) or len(target) != 2:
            return 0, ""
        return int(target[0] or 0), str(target[1] or "")

    def stop_continuous(self, *, flush: bool = True):
        if not self._continuous_mode and not self._is_recording:
            return
        self._continuous_timer.stop()
        should_flush = bool(flush) and not self._is_cancelled
        self._close_lanes_then_flush(flush=should_flush)
        with self._lock:
            for lane in self._lanes:
                lane.chunks = []
                lane.pending = np.zeros(0, dtype=np.float32)
        self._continuous_mode = False
        self._route_watch_enabled = False
        self._is_cancelled = False
        self._is_paused = False
        self._is_recording = False
        self._mic_level = 0.0
        self._system_level = 0.0
        self._last_emitted_volume = 0.0
        self._last_real_block_at = 0.0
        self._last_loopback_pad_at = 0.0
        self._lanes = []
        self._terminate_pawp()
        self._segmenter.reset()

    def _close_lanes_then_flush(self, flush: bool = True) -> int:
        """Stopping a stream hands over the block still in the driver; close
        while callbacks still accept data so the last words reach the flush."""
        for lane in self._lanes:
            self._close_lane(lane)
        try:
            emitted = self._flush_continuous_segments() if flush else 0
            if flush:
                emitted += self._release_boundary_context(ending=True)
            return emitted
        finally:
            self._boundary_buffer = None
            self._split_segmenters = {}
            self._split_boundaries = {}
            self._block_tracks = {}
            self._recovery_tracks = {}
            self._split_ready = []
            self._close_journal()

    @property
    def is_continuous(self) -> bool:
        return self._continuous_mode and self._is_recording

    @staticmethod
    def _strip_raw_error_codes(text: str) -> str:
        cleaned = re.sub(r"\b0x[0-9a-fA-F]{6,8}\b", "", text or "")
        cleaned = re.sub(r"\[Errno\s*-?\d+\]\s*", "", cleaned)
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" ：:;；,，")
        return cleaned

    def _friendly_open_error(self, msg: str) -> str:
        lower = msg.lower()
        if msg.startswith(("无法打开音频设备", "音频设备启动失败")):
            if "audio device" in lower and "realtek" not in lower:
                return (
                    "无法打开「Audio Device」类虚拟扬声器。"
                    "请点「恢复自动选择」，或将 Windows 默认扬声器改为 Realtek 物理声卡。"
                )
            return self._strip_raw_error_codes(msg) or "无法打开音频设备，请检查设备连接后重试"
        if "未找到可用的" in msg:
            return msg
        if "permission" in lower or "access" in lower:
            return "无法访问音频设备，请检查系统隐私与麦克风权限"
        if (
            "no device" in lower
            or "invalid device" in lower
            or "painvaliddevice" in lower
            or "there is no device" in lower
        ):
            return "未检测到可用的音频设备，请在设置中点「恢复自动选择」后重试"
        if "loopback" in lower or "wasapi" in lower:
            return (
                "无法打开系统声音采集。可尝试在设置中更换「电脑声音设备」，"
                "或在系统声音设置中启用「立体声混音」。"
            )
        if "网易" in msg or "netease" in lower or "虚拟" in msg:
            return (
                "无法打开该虚拟扬声器采集电脑播放声。"
                "请在「手动选择音频设备」中将「电脑声音」改为系统默认物理扬声器，或启用「立体声混音」。"
            )
        cleaned = self._strip_raw_error_codes(msg)
        return f"音频设备启动失败：{cleaned}" if cleaned else "音频设备启动失败，请检查音频设备设置"

    def _close_lane(self, lane: _CaptureLane):
        pawp_stop = getattr(lane, "pawp_stop", None)
        if pawp_stop is not None:
            pawp_stop.set()
        pawp_thread = getattr(lane, "pawp_thread", None)
        thread_stopped = True
        if pawp_thread is not None:
            if pawp_thread is not threading.current_thread():
                pawp_thread.join(timeout=2.0)
                thread_stopped = not pawp_thread.is_alive()
            lane.pawp_thread = None
        lane.pawp_stop = None

        pawp_stream = getattr(lane, "pawp_stream", None)
        if pawp_stream is not None:
            pawp_lock = getattr(lane, "pawp_lock", None)
            acquired = pawp_lock.acquire(timeout=1.0) if pawp_lock is not None else True
            try:
                if acquired or thread_stopped:
                    lane.pawp_stream = None
                    try:
                        pawp_stream.stop_stream()
                    except Exception:
                        log.debug("停止 PyAudioWPatch 流 [%s] 失败", lane.endpoint.role, exc_info=True)
                    try:
                        pawp_stream.close()
                    except Exception:
                        log.debug("关闭 PyAudioWPatch 流 [%s] 失败", lane.endpoint.role, exc_info=True)
                else:
                    log.warning("PyAudioWPatch 读取线程未在超时内退出，跳过同步 close 防止驱动冲突")
                    lane.pawp_stream = None
                    self._retire_pawp(pawp_thread)
            finally:
                if pawp_lock is not None and acquired:
                    pawp_lock.release()

        stream = getattr(lane, "stream", None)
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                log.debug("关闭音频流 [%s] 失败", lane.endpoint.role, exc_info=True)
            finally:
                lane.stream = None
                if getattr(lane, "stream_registered", False):
                    mark_portaudio_stream_active(-1)
                    lane.stream_registered = False

    def stop(self):
        """End a hold-to-talk recording.

        Hold-to-talk runs through the same segmenter as continuous listening
        (``start()`` sets ``_segment_live``), so stopping only flushes the
        utterance tail as one more ``segment_ready``. ``recording_finished``
        therefore never carries audio any more: an empty array signals that
        the stop emitted no segment. The app can check ongoing recognition
        and last_hold_capture before deciding what to show.
        """
        if self._continuous_mode:
            self.stop_continuous(flush=not self._is_cancelled)
            return
        self._segment_live = False
        self._continuous_timer.stop()
        if not self._is_recording:
            return
        cancelled = self._is_cancelled
        emitted = self._close_lanes_then_flush(flush=not cancelled)
        with self._lock:
            if not cancelled:
                first_block_at = min(
                    (lane.first_chunk_at for lane in self._lanes if lane.first_chunk_at is not None),
                    default=None,
                )
                # Parallel input lanes overlap in time. Use the longest capture.
                captured_ms = max(
                    (lane.captured_samples * 1000 // lane.sample_rate for lane in self._lanes),
                    default=0,
                )
                first_block_ms = (
                    round((first_block_at - self._hold_capture_started_at) * 1000)
                    if first_block_at is not None else None
                )
                self.last_hold_capture = HoldCaptureStats(first_block_ms, captured_ms)
            self._is_cancelled = False
            for lane in self._lanes:
                lane.chunks = []
        if self.last_hold_capture is not None and not cancelled:
            capture = self.last_hold_capture
            log.info(
                "单次录音结束: 首块=%s, 采集=%d ms, 送出=%d 段",
                f"{capture.first_block_ms} ms" if capture.first_block_ms is not None else "未收到",
                capture.captured_ms,
                self._hold_segments_emitted,
            )
        self._is_paused = False
        self._route_watch_enabled = False
        self._is_recording = False
        self._mic_level = 0.0
        self._system_level = 0.0
        self._last_emitted_volume = 0.0
        self._last_real_block_at = 0.0
        self._last_loopback_pad_at = 0.0
        self._lanes = []
        self._terminate_pawp()
        self._segmenter.reset()
        if not cancelled and emitted == 0 and not self._suppress_empty_finished:
            self.recording_finished.emit(np.array([], dtype=np.float32))

    def cancel(self):
        self._is_cancelled = True
        if self._continuous_mode:
            self.stop_continuous(flush=False)
            return
        self.stop()

    @property
    def is_recording(self) -> bool:
        return self._is_recording

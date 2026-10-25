import logging
import threading
import time
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
    build_recording_plan,
    input_source_label,
    is_unreliable_loopback_output,
    ordered_system_devices,
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
from sayink.pawp_capture import decode_pawp_device_index, is_encoded_pawp_device_index
from sayink.lane_echo import DuplicateLaneSuppressor
from sayink.runtime_status import SegmentationStatus
from sayink.audio_recovery import RecordingJournal
from sayink.vad_segmenter import (
    VAD_ENERGY,
    VAD_SILERO,
    SpeechSegmenter,
    make_meeting_segmenter,
    make_speech_gate,
)

# A lane this quiet is digital silence, not a person. Mixing it in still
# runs the loudness balancer on the only real lane and moves the cut points.
_DIGITAL_SILENCE_RMS = 1e-4

log = logging.getLogger("SayInk")


class _CaptureLane:
    def __init__(self, endpoint: StreamEndpoint):
        self.endpoint = endpoint
        self.chunks: list[np.ndarray] = []
        # Resampled to TARGET_SAMPLE_RATE, not yet mixed with the other lanes.
        self.pending = np.zeros(0, dtype=np.float32)
        self.last_chunk_at = time.monotonic()
        self.sample_rate = TARGET_SAMPLE_RATE
        self.resampler: Optional[StreamResampler] = None
        self.stream: Optional[sd.InputStream] = None
        self.pawp_stream: object | None = None
        self.pawp_stop: threading.Event | None = None
        self.pawp_thread: threading.Thread | None = None


class AudioRecorder(QObject):
    segmentation_changed = pyqtSignal(object)
    volume_changed = pyqtSignal(float)
    recording_finished = pyqtSignal(np.ndarray)
    segment_ready = pyqtSignal(np.ndarray)
    utterance_opened = pyqtSignal(int, str)
    error = pyqtSignal(str)
    warning = pyqtSignal(str)
    no_speech_warning = pyqtSignal()

    NO_SPEECH_WARN_SEC = 30.0
    LANE_STALL_SEC = 3.0
    # Mixing waits for a lane that delivered within this window; a quieter
    # one (loopback while nothing plays) is filled with silence instead.
    MIX_WAIT_SEC = 0.3
    MAX_MIX_SKEW_SEC = 1.0

    SAMPLE_RATE = TARGET_SAMPLE_RATE
    CHANNELS = 1
    CHUNK_DURATION = 0.1

    def __init__(self, parent=None):
        super().__init__(parent)
        self._is_recording = False
        self._is_cancelled = False
        self._lanes: list[_CaptureLane] = []
        self._lock = threading.Lock()
        self._input_source = INPUT_SOURCE_MICROPHONE
        self._mic_device_index = -1
        self._system_device_index = -1
        self._plan: Optional[RecordingPlan] = None
        self._last_start_warning: str = ""
        self._pawp: object | None = None
        self._continuous_mode = False
        self._segment_live = False
        self._segmenter = SpeechSegmenter()
        self._continuous_timer = QTimer(self)
        self._continuous_timer.setInterval(100)
        self._continuous_timer.timeout.connect(self._on_continuous_tick)
        self._last_speech_at = 0.0
        self._no_speech_warned = False
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
        self._meeting_profile = False
        self._segmentation_status = SegmentationStatus(VAD_ENERGY, VAD_ENERGY)
        # Per-lane loudness balancing for mixed capture (every profile).
        self._balancer: LaneBalancer | None = LaneBalancer()
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
        self._quiet_speech = False
        self._last_overflow_warning = float("-inf")
        self._suppress_empty_finished = False

    def seconds_since_last_speech(self) -> float | None:
        """How long the open stream has been without speech, or None if idle."""
        if not self._is_recording or self._last_speech_at <= 0:
            return None
        return max(0.0, time.monotonic() - self._last_speech_at)

    def _warn_overflow(self, role):
        now = time.monotonic()
        if now - self._last_overflow_warning >= 5:
            self._last_overflow_warning = now
            self.warning.emit(f"音频设备报告输入溢出（{role}），这一时刻可能有录音缺口；已继续采集后续声音。")

    def set_quiet_speech(self, enabled: bool):
        self._quiet_speech = bool(enabled)

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
                segmenter = make_meeting_segmenter(speech_threshold=threshold, vad=self._vad_kind,
                                                   model_path=self._vad_model_path)
            else:
                segmenter = SpeechSegmenter(speech_threshold=threshold)
                segmenter.set_gate(make_speech_gate(self._vad_kind, segmenter, model_path=self._vad_model_path))
            self._split_segmenters[role] = segmenter
            if self._vad_kind == VAD_SILERO and segmenter.gate_name != VAD_SILERO:
                self.warning.emit(f"{role} 的语音检测模型未能启用，使用能量断句；本路轻声增强未启用。")
            segmenter.quiet_speech = self._quiet_speech and segmenter.gate_name == VAD_SILERO
            if segmenter.gate_name == VAD_SILERO:
                segmenter.gate.input_gain = 4.0 if self._quiet_speech else 1.0
            self._split_targets[role] = (0, "")
            if self._boundary_requested:
                self._split_boundaries[role] = BoundaryContextBuffer()

    def _emit_split_segment(self, role, segment):
        seg = self._split_segmenters[role]
        self._segment_route = "mic" if role == "microphone" else "system"
        self._segment_captured_at = self._stream_anchor_ms + int(seg.last_bounds[0] * 1000 / TARGET_SAMPLE_RATE)
        self._segment_target = self._split_targets[role]
        boundary = self._split_boundaries.get(role)
        if boundary is not None:
            boundary.capture(segment, seg.last_bounds, seg.last_forced,
                             (self._segment_route, self._segment_captured_at, self._segment_target))
            self._release_split_context(role)
        else:
            self._split_ready.append((segment, (self._segment_route, self._segment_captured_at, self._segment_target)))

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
            self._segment_route, self._segment_captured_at, self._segment_target = metadata
            self._segment_route_reliable = True
            self.segment_ready.emit(audio)
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
        self._release_split_ready()
        return len(emitted)

    def set_boundary_context(self, enabled: bool) -> None:
        self._boundary_requested = bool(enabled)

    def set_recovery_directory(self, root) -> None:
        """Apply to the next recording, never start persisting an active one."""
        self._recovery_root = root

    @property
    def recovery_available(self) -> bool:
        return self._journal is not None and self._journal.healthy

    @property
    def recovery_pending_dir(self):
        return self._journal.path / "pending" if self._journal is not None else None

    def _close_journal(self) -> None:
        if self._journal is not None:
            if not self._journal.close():
                self._closing_journals.append(self._journal)
            self._journal = None

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
        segmenter.quiet_speech = self._quiet_speech and gate.name == VAD_SILERO
        if gate.name == VAD_SILERO:
            gate.input_gain = 4.0 if self._quiet_speech else 1.0
        self._segmenter = segmenter
        self._stream_anchor_ms = 0
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

    @property
    def segmentation_status(self) -> SegmentationStatus:
        return self._segmentation_status

    @property
    def vad_name(self) -> str:
        """Which gate the segmenter is actually using (energy / silero)."""
        return self._segmenter.gate_name

    @property
    def input_source(self) -> str:
        return self._input_source

    @property
    def input_source_display(self) -> str:
        return input_source_label(self._input_source)

    def _make_callback(self, lane: _CaptureLane) -> Callable:
        def _callback(indata, _frames, _time_info, status):
            if status:
                log.debug("音频流状态 [%s]: %s", lane.endpoint.role, status)
                if getattr(status, "input_overflow", False) is True:
                    self._warn_overflow(lane.endpoint.role)
            if not self._is_recording or self._is_cancelled:
                return
            # The level meter is fed once per tick from the balanced mix
            # (_on_continuous_tick); emitting the raw per-lane block here as
            # well made the meter flicker between two scales.
            block = to_mono(indata.copy())
            with self._lock:
                lane.chunks.append(block)
                lane.last_chunk_at = time.monotonic()
        return _callback

    def _ensure_pawp(self):
        if self._pawp is None:
            import pyaudiowpatch as pyaudio

            self._pawp = pyaudio.PyAudio()
        return self._pawp

    def _terminate_pawp(self):
        if self._pawp is not None:
            try:
                self._pawp.terminate()
            except Exception:
                log.debug("释放 PyAudioWPatch 失败", exc_info=True)
            self._pawp = None

    def _pawp_read_loop(self, lane: _CaptureLane, stream, channels: int, rate: int):
        """Blocking read loop — stream_callback is unreliable on some Windows drivers."""
        frame_count = max(1, int(rate * self.CHUNK_DURATION))
        while (
            self._is_recording
            and not self._is_cancelled
            and lane.pawp_stop is not None
            and not lane.pawp_stop.is_set()
        ):
            try:
                avail = stream.get_read_available()
                if avail <= 0:
                    time.sleep(0.02)
                    continue
                to_read = min(avail, frame_count)
                data = stream.read(to_read, exception_on_overflow=True)
            except Exception as e:
                if getattr(e, "errno", None) == -9981 or "overflow" in str(e).lower():
                    self._warn_overflow(lane.endpoint.role)
                    continue
                log.debug("PyAudioWPatch 读取失败: %s", e)
                break
            if not data:
                continue
            arr = np.frombuffer(data, dtype=np.float32)
            if channels > 1:
                arr = arr.reshape(-1, channels)
            # No backend-specific gain here: PyAudioWPatch and the sounddevice
            # WASAPI path both read the same float32 render mix, and both feed
            # the same 0.0006 gate, balancer and adaptive floor. A former 2.5×
            # boost only clipped loud playback and made identical audio
            # segment differently depending on which backend opened the device.
            block_mono = to_mono(arr.copy())
            with self._lock:
                lane.chunks.append(block_mono)
                lane.last_chunk_at = time.monotonic()

    def _open_pawp_lane(self, lane: _CaptureLane):
        import pyaudiowpatch as pyaudio

        ep = lane.endpoint
        pawp_idx = decode_pawp_device_index(ep.device.index)
        p = self._ensure_pawp()
        info = p.get_device_info_by_index(pawp_idx)
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
        info = sd.query_devices(ep.device.index)
        device = ep.device.index

        in_ch = int(info.get("max_input_channels", 0) or 0)
        out_ch = int(info.get("max_output_channels", 0) or 0)
        channels = ep.device.channels
        if ep.use_wasapi_loopback and out_ch > 0:
            channels = min(out_ch, 2)
        elif in_ch > 0:
            channels = min(in_ch, 2)
        channels = max(1, channels)

        # Deduplicated: the device default is usually 48000 already, and a
        # repeated rate meant a second identical open attempt in the logs.
        sample_rates = tuple(dict.fromkeys((
            TARGET_SAMPLE_RATE,
            int(info.get("default_samplerate") or 48000),
            48000,
            44100,
        )))
        loopback_attempts = (True, False) if ep.use_wasapi_loopback else (False,)
        last_err: Optional[Exception] = None

        for try_loopback in loopback_attempts:
            extra = None
            if try_loopback:
                try:
                    extra = sd.WasapiSettings(loopback=True)
                except TypeError:
                    log.debug("此 sounddevice 不支持 WASAPI loopback 参数，改用普通输入流")
                    continue
            for rate in sample_rates:
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
                    log.debug(
                        "打开设备失败 %s @ %d loopback=%s: %s",
                        ep.device.name,
                        rate,
                        try_loopback,
                        e,
                    )
                    # A stream that opened but failed to start() is a live
                    # PortAudio handle; lane.stream is still None here, so
                    # close the local one (it would otherwise leak until exit).
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

    def _drain_mixed_mono(self, final: bool = False) -> Optional[np.ndarray]:
        """Mix what every lane has delivered so far. Lanes call back on their
        own clocks, so a tick can find two blocks on one lane and none on the
        other; only the span both have covered is mixed, the rest waits for
        the next tick (or ``final``, which takes everything)."""
        now = time.monotonic()
        drained: list[tuple[_CaptureLane, Optional[np.ndarray], float]] = []
        with self._lock:
            for lane in self._lanes:
                # Hand drained blocks to the segmenter and drop them here, so a
                # long session holds only the current utterance in memory.
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
            return None

        lane_tracks = []
        populated_roles = set()
        for lane, _raw, _last in drained:
            track = lane.pending[:take]
            lane.pending = lane.pending[take:]
            if track.size:
                populated_roles.add(lane.endpoint.role)
            if track.size < take:
                track = np.pad(track, (0, take - track.size))
            lane_tracks.append((lane.endpoint.role, track))
        roles = [role for role, _track in lane_tracks]
        self._recovery_tracks = dict(lane_tracks)
        if sorted(roles) == ["microphone", "system"]:
            mic_index, system_index = roles.index("microphone"), roles.index("system")
            cleaned = self._duplicate_suppressor.process(lane_tracks[mic_index][1], lane_tracks[system_index][1])
            lane_tracks[mic_index] = ("microphone", cleaned)
        else:
            self._duplicate_suppressor.reset()
        self._block_tracks = {role: track for role, track in lane_tracks}
        tracks: list[np.ndarray] = []
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
            if self._balancer is not None:
                gains.append(self._balancer.observe(role, track))
        self._block_mic_energy = mic_energy
        self._block_system_energy = system_energy
        if not tracks:
            return np.zeros(take, dtype=np.float32)
        if len(tracks) == 1:
            track = tracks[0]
            if track.size < take:
                track = np.concatenate([track, np.zeros(take - track.size, dtype=np.float32)])
            return track
        if len(tracks) > 1:
            audible = [track for track in tracks if rms_volume(track) >= _DIGITAL_SILENCE_RMS]
            if len(audible) == 1:
                only = audible[0]
                if only.size < take:
                    only = np.concatenate([only, np.zeros(take - only.size, dtype=np.float32)])
                return only
        if self._balancer is not None and len(gains) == len(tracks):
            return mix_to_mono(tracks, TARGET_SAMPLE_RATE, gains=gains)
        return mix_to_mono(tracks, TARGET_SAMPLE_RATE)

    def _reset_continuous_speech_watch(self) -> None:
        now = time.monotonic()
        self._last_speech_at = now
        self._no_speech_warned = False

    def _lost_lane(self) -> Optional[_CaptureLane]:
        """A capture lane whose device went away mid-session."""
        now = time.monotonic()
        for lane in self._lanes:
            if lane.pawp_thread is not None and not lane.pawp_thread.is_alive():
                return lane
            stream = lane.stream
            if stream is not None and getattr(stream, "active", True) is False:
                return lane
            # Loopback capture delivers nothing while the PC is silent, so only
            # the microphone lane is expected to call back every block.
            if lane.endpoint.role != "system" and stream is not None:
                if now - lane.last_chunk_at > self.LANE_STALL_SEC:
                    return lane
        return None

    def _handle_lost_device(self, lane: _CaptureLane) -> None:
        name = lane.endpoint.device.name
        log.warning("音频设备已断开: %s", name)
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

    def _on_continuous_tick(self):
        if not self._is_recording or self._is_cancelled:
            return
        if not self._continuous_mode and not self._segment_live:
            return
        lost = self._lost_lane()
        if lost is not None:
            self._handle_lost_device(lost)
            return
        block = self._drain_mixed_mono()
        if block is None or block.size == 0:
            self._warn_if_silent()
            return
        vol = rms_volume(block)
        self.volume_changed.emit(vol)
        gate = getattr(self._segmenter, "effective_threshold", self._segmenter.speech_threshold)
        if vol >= gate:
            self._last_speech_at = time.monotonic()
        if self._split_segmenters:
            if self._feed_split_tracks(block):
                self._last_speech_at = time.monotonic()
            elif self._warn_if_silent():
                return
            return
        segment = self._feed_segmenter(block)
        if segment is not None and segment.size > 0:
            self._last_speech_at = time.monotonic()
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
        self._lanes = []
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
                                    "无法打开电脑播放声（已尝试 WASAPI 环回设备）。"
                                    "请确认正在用系统默认扬声器播放视频，并在设置中刷新设备列表。"
                                )
                            else:
                                warn = (
                                    "无法采集电脑播放声；本次仅录麦克风。"
                                    "请安装依赖：pip install PyAudioWPatch，"
                                    "或在 Windows 启用「立体声混音」后点「恢复自动选择」。"
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
            self._prepare_split_segmenters()
            if self._recovery_root is not None:
                try:
                    self._journal = RecordingJournal(
                        self._recovery_root,
                        metadata={"source": self._input_source, "meeting": self._meeting_profile,
                                  "continuous": continuous},
                        on_error=self.warning.emit,
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
                    emitted += 1
                self._release_split_context(role, ending=True)
            emitted += self._release_split_ready(ending=True)
            return emitted
        if block is not None and block.size > 0:
            segment = self._feed_segmenter(block)
            if segment is not None and segment.size > 0:
                log.debug("持续监听收尾片段: %d 采样点", segment.size)
                self._emit_segment(segment)
                emitted += 1
        flushed = self._segmenter.flush()
        if flushed is not None and flushed.size > 0:
            log.debug("持续监听 flush 片段: %d 采样点", flushed.size)
            self._block_started_at = self._segment_started_at
            # flush() never passes through _feed_segmenter, so the window
            # captured when this utterance opened would otherwise be lost.
            self._segment_target = self._open_target
            self._emit_segment(flushed)
            emitted += 1
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
            TextPaster._is_own_window(TextPaster, info)
            or target_rejects_synthetic_input(info)
        ):
            hwnd = 0
        self._open_target = (hwnd, name)
        self.utterance_opened.emit(hwnd, name)

    def _emit_segment(self, segment: np.ndarray) -> None:
        self._segment_route = self._segmenter.last_route
        self._segment_captured_at = self._block_started_at or int(time.time() * 1000)
        bounds = getattr(self._segmenter, "last_bounds", None)
        if self._stream_anchor_ms and bounds is not None and bounds[1] - bounds[0] == segment.size:
            self._segment_captured_at = self._stream_anchor_ms + int(bounds[0] * 1000 / TARGET_SAMPLE_RATE)
        if self._boundary_buffer is not None and bounds is not None:
            self._boundary_buffer.capture(segment, bounds, self._segmenter.last_forced,
                                          (self._segment_route, self._segment_captured_at, self._segment_target))
            self._release_boundary_context()
        else:
            self._segment_route_reliable = False
            self.segment_ready.emit(segment)

    def _release_boundary_context(self, *, ending=False):
        if self._boundary_buffer is None:
            return 0
        released = 0
        for audio, metadata in self._boundary_buffer.release(ending=ending):
            self._segment_route, self._segment_captured_at, self._segment_target = metadata
            self._segment_route_reliable = False
            self.segment_ready.emit(audio)
            released += 1
        return released

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

    def stop_continuous(self):
        if not self._continuous_mode and not self._is_recording:
            return
        self._continuous_timer.stop()
        self._close_lanes_then_flush()
        self._continuous_mode = False
        self._is_cancelled = False
        self._is_recording = False
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

    def _friendly_open_error(self, msg: str) -> str:
        lower = msg.lower()
        if msg.startswith(("无法打开音频设备", "音频设备启动失败")):
            if "audio device" in lower and "realtek" not in lower:
                return (
                    "无法打开「Audio Device」类虚拟扬声器。"
                    "请点「恢复自动选择」，或将 Windows 默认扬声器改为 Realtek 物理声卡。"
                )
            return msg
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
                "无法打开系统声音采集。可尝试在设置中更换「系统声音设备」，"
                "或启用立体声混音 / 安装 VB-Audio Virtual Cable"
            )
        if "网易" in msg or "netease" in lower or "虚拟" in msg:
            return (
                "无法打开该虚拟扬声器采集电脑播放声。"
                "请在「手动选择音频设备」中将「电脑声音」改为系统默认 Realtek 扬声器，或启用「立体声混音」。"
            )
        return f"音频设备启动失败: {msg}"

    def _close_lane(self, lane: _CaptureLane):
        if lane.pawp_stop is not None:
            lane.pawp_stop.set()
        if lane.pawp_thread is not None:
            lane.pawp_thread.join(timeout=2.0)
            lane.pawp_thread = None
        lane.pawp_stop = None
        if lane.pawp_stream is not None:
            try:
                lane.pawp_stream.stop_stream()
                lane.pawp_stream.close()
            except Exception:
                log.debug("关闭 PyAudioWPatch 流 [%s] 失败", lane.endpoint.role, exc_info=True)
            lane.pawp_stream = None
        if lane.stream is not None:
            try:
                lane.stream.stop()
                lane.stream.close()
            except Exception:
                log.debug("关闭音频流 [%s] 失败", lane.endpoint.role, exc_info=True)
            lane.stream = None

    def stop(self):
        """End a hold-to-talk recording.

        Hold-to-talk runs through the same segmenter as continuous listening
        (``start()`` sets ``_segment_live``), so stopping only flushes the
        utterance tail as one more ``segment_ready``. ``recording_finished``
        therefore never carries audio any more: an empty array means the gate
        heard nothing during the whole hold.
        """
        if self._continuous_mode:
            self.stop_continuous()
            return
        self._segment_live = False
        self._continuous_timer.stop()
        if not self._is_recording:
            return
        cancelled = self._is_cancelled
        emitted = self._close_lanes_then_flush(flush=not cancelled)
        with self._lock:
            self._is_cancelled = False
            for lane in self._lanes:
                lane.chunks = []
        self._is_recording = False
        self._lanes = []
        self._terminate_pawp()
        self._segmenter.reset()
        if not cancelled and emitted == 0 and not self._suppress_empty_finished:
            self.recording_finished.emit(np.array([], dtype=np.float32))

    def cancel(self):
        self._is_cancelled = True
        if self._continuous_mode:
            self.stop_continuous()
            return
        self.stop()

    @property
    def is_recording(self) -> bool:
        return self._is_recording

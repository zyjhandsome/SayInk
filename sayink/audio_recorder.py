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
    StreamResampler,
    mix_to_mono,
    resample_mono,
    rms_volume,
    to_mono,
)
from sayink.pawp_capture import decode_pawp_device_index, is_encoded_pawp_device_index
from sayink.vad_segmenter import SpeechSegmenter

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
    volume_changed = pyqtSignal(float)
    recording_finished = pyqtSignal(np.ndarray)
    segment_ready = pyqtSignal(np.ndarray)
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

    def configure(
        self,
        input_source: str = INPUT_SOURCE_MICROPHONE,
        mic_device_index: int = -1,
        system_device_index: int = -1,
    ):
        self._input_source = input_source
        self._mic_device_index = int(mic_device_index)
        self._system_device_index = int(system_device_index)
        threshold = 0.0006 if input_source == INPUT_SOURCE_SYSTEM else 0.002
        self._segmenter = SpeechSegmenter(speech_threshold=threshold)

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
            if not self._is_recording or self._is_cancelled:
                return
            block = to_mono(indata.copy())
            vol = rms_volume(block)
            self.volume_changed.emit(vol)
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
        import time

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
                data = stream.read(to_read, exception_on_overflow=False)
            except Exception as e:
                log.debug("PyAudioWPatch 读取失败: %s", e)
                break
            if not data:
                continue
            arr = np.frombuffer(data, dtype=np.float32)
            if channels > 1:
                arr = arr.reshape(-1, channels)
            block_mono = to_mono(arr.copy())
            if self._input_source == INPUT_SOURCE_SYSTEM:
                block_mono = np.clip(block_mono * 2.5, -1.0, 1.0)
            vol = rms_volume(block_mono)
            self.volume_changed.emit(vol)
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

        sample_rates = (
            TARGET_SAMPLE_RATE,
            int(info.get("default_samplerate") or 48000),
            48000,
            44100,
        )
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
                    if lane.stream is not None:
                        try:
                            lane.stream.close()
                        except Exception:
                            log.debug("关闭半开的音频流失败", exc_info=True)
                        lane.stream = None

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
                    log.info("系统声音改用备用设备: %s", ep.device.name)
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

        tracks: list[np.ndarray] = []
        mic_energy = 0.0
        system_energy = 0.0
        for lane, _raw, _last in drained:
            track = lane.pending[:take]
            lane.pending = lane.pending[take:]
            if track.size == 0:
                continue
            energy = float(np.dot(track, track))
            if lane.endpoint.role == "system":
                system_energy += energy
            else:
                mic_energy += energy
            tracks.append(track)
        self._block_mic_energy = mic_energy
        self._block_system_energy = system_energy
        if len(tracks) == 1:
            track = tracks[0]
            if track.size < take:
                track = np.concatenate([track, np.zeros(take - track.size, dtype=np.float32)])
            return track
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
            self.cancel()
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
            return
        vol = rms_volume(block)
        self.volume_changed.emit(vol)
        gate = getattr(self._segmenter, "effective_threshold", self._segmenter.speech_threshold)
        if vol >= gate:
            self._last_speech_at = time.monotonic()
        segment = self._feed_segmenter(block)
        if segment is not None and segment.size > 0:
            self._last_speech_at = time.monotonic()
            log.debug("持续监听切分片段: %d 采样点", segment.size)
            self._emit_segment(segment)
        elif (
            not self._no_speech_warned
            and time.monotonic() - self._last_speech_at >= self.NO_SPEECH_WARN_SEC
        ):
            self._no_speech_warned = True
            self.no_speech_warning.emit()

    def start(self, *, continuous: bool = False):
        if self._is_recording:
            return

        self._continuous_mode = continuous
        self._segment_live = not continuous
        if continuous or self._segment_live:
            self._segmenter.reset()
            self._reset_continuous_speech_watch()
        self._is_cancelled = False
        self._lanes = []
        self._last_start_warning = ""

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
        if block is not None and block.size > 0:
            segment = self._feed_segmenter(block)
            if segment is not None and segment.size > 0:
                log.debug("持续监听收尾片段: %d 采样点", segment.size)
                self._emit_segment(segment)
                emitted += 1
        flushed = self._segmenter.flush()
        if flushed is not None and flushed.size > 0:
            log.debug("持续监听 flush 片段: %d 采样点", flushed.size)
            self._emit_segment(flushed)
            emitted += 1
        return emitted

    def _feed_segmenter(self, block: np.ndarray) -> Optional[np.ndarray]:
        return self._segmenter.feed(
            block,
            mic_energy=self._block_mic_energy,
            system_energy=self._block_system_energy,
        )

    def _emit_segment(self, segment: np.ndarray) -> None:
        self._segment_route = self._segmenter.last_route
        self.segment_ready.emit(segment)

    def consume_segment_route(self) -> str:
        """Return the route of the segment just emitted, then clear it."""
        route = self._segment_route
        self._segment_route = ""
        return route

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
        return self._flush_continuous_segments() if flush else 0

    @property
    def is_continuous(self) -> bool:
        return self._continuous_mode and self._is_recording

    def _friendly_open_error(self, msg: str) -> str:
        lower = msg.lower()
        if msg.startswith("无法打开音频设备") or msg.startswith("音频设备启动失败"):
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
                "请在「手动选择音频设备」中将「电脑播放」改为系统默认 Realtek 扬声器，或启用「立体声混音」。"
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
        if self._continuous_mode:
            self.stop_continuous()
            return
        segment_live = self._segment_live
        self._segment_live = False
        self._continuous_timer.stop()
        if not self._is_recording:
            return
        if segment_live:
            cancelled = self._is_cancelled
            emitted = self._close_lanes_then_flush(flush=not cancelled)
            self._is_cancelled = False
            self._is_recording = False
            self._lanes = []
            self._terminate_pawp()
            self._segmenter.reset()
            if not cancelled and emitted == 0:
                self.recording_finished.emit(np.array([], dtype=np.float32))
            return

        self._is_recording = False
        lanes = self._lanes
        self._lanes = []

        for lane in lanes:
            self._close_lane(lane)

        with self._lock:
            is_cancelled = self._is_cancelled
            self._is_cancelled = False
            lane_chunks = [(lane.endpoint.role, list(lane.chunks), lane.sample_rate) for lane in lanes]
            for lane in lanes:
                lane.chunks = []

        if is_cancelled:
            self._terminate_pawp()
            return

        self._terminate_pawp()

        tracks = []
        for role, chunks, sample_rate in lane_chunks:
            if not chunks:
                continue
            raw = np.concatenate(chunks)
            tracks.append(resample_mono(raw, sample_rate, TARGET_SAMPLE_RATE))

        if not tracks:
            self.recording_finished.emit(np.array([], dtype=np.float32))
            return

        if len(tracks) == 1:
            full_audio = tracks[0]
        else:
            full_audio = mix_to_mono(tracks, TARGET_SAMPLE_RATE)

        self.recording_finished.emit(full_audio)

    def cancel(self):
        self._is_cancelled = True
        if self._continuous_mode:
            self.stop_continuous()
            return
        self.stop()

    @property
    def is_recording(self) -> bool:
        return self._is_recording

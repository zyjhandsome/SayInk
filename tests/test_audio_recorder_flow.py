"""Flow-level tests for AudioRecorder without real hardware.

Covers mixing/draining, continuous flush (FR-VAD-05), stop output, the
continuous tick loop, and start() failure handling.
"""

from __future__ import annotations

import numpy as np
import pytest

from sayink.audio_devices import AudioDeviceInfo, StreamEndpoint
from sayink.audio_recorder import AudioRecorder, _CaptureLane


def _make_lane(role="microphone", chunks=None, sample_rate=16000):
    device = AudioDeviceInfo(0, "Mock", "mock", 16000, 1, False, False)
    lane = _CaptureLane(StreamEndpoint(role, device, False))
    lane.chunks = list(chunks or [])
    lane.sample_rate = sample_rate
    return lane


class _FakeSegmenter:
    def __init__(self, feed_result=None, flush_result=None, threshold=0.002):
        self.speech_threshold = threshold
        self._feed_result = feed_result
        self._flush_result = flush_result
        self.reset_called = False
        self.last_route = ""

    def feed(self, block, **_kwargs):
        return self._feed_result

    def flush(self):
        return self._flush_result

    def reset(self):
        self.reset_called = True


class TestConfigureThresholds:
    def test_system_source_uses_low_threshold(self):
        rec = AudioRecorder()
        rec.configure(input_source="system")
        assert rec._segmenter.speech_threshold == pytest.approx(0.0006)

    def test_mic_source_uses_default_threshold(self):
        rec = AudioRecorder()
        rec.configure(input_source="microphone")
        assert rec._segmenter.speech_threshold == pytest.approx(0.002)


class TestDrainMixedMono:
    def test_returns_none_when_no_new_chunks(self):
        rec = AudioRecorder()
        rec._lanes = [_make_lane(chunks=[])]
        assert rec._drain_mixed_mono() is None

    def test_single_lane_returns_track(self):
        rec = AudioRecorder()
        rec._lanes = [_make_lane(chunks=[np.ones(1600, dtype=np.float32)])]
        out = rec._drain_mixed_mono()
        assert out is not None and out.size == 1600

    def test_two_lanes_are_mixed(self):
        rec = AudioRecorder()
        rec._lanes = [
            _make_lane(role="microphone", chunks=[np.ones(1600, dtype=np.float32)]),
            _make_lane(role="system", chunks=[np.ones(1600, dtype=np.float32)]),
        ]
        out = rec._drain_mixed_mono()
        assert out is not None and out.size == 1600

    def test_drain_advances_index(self):
        rec = AudioRecorder()
        lane = _make_lane(chunks=[np.ones(1600, dtype=np.float32)])
        rec._lanes = [lane]
        rec._drain_mixed_mono()
        assert rec._drain_mixed_mono() is None  # nothing new second time

    def test_drain_releases_blocks_so_long_sessions_stay_bounded(self):
        rec = AudioRecorder()
        lane = _make_lane()
        rec._lanes = [lane]
        for _ in range(600):  # one minute of 100 ms blocks
            lane.chunks.append(np.ones(1600, dtype=np.float32))
            rec._drain_mixed_mono()
        assert lane.chunks == []


class _FakeStream:
    def __init__(self, active=True):
        self.active = active


class TestLostDevice:
    def _recording(self, lane, continuous=True):
        rec = AudioRecorder()
        rec._lanes = [lane]
        rec._is_recording = True
        rec._continuous_mode = continuous
        rec._segment_live = not continuous
        rec._segmenter = _FakeSegmenter()
        return rec

    def test_microphone_lane_without_callbacks_is_reported(self):
        lane = _make_lane()
        lane.stream = _FakeStream()
        lane.last_chunk_at -= AudioRecorder.LANE_STALL_SEC + 1
        rec = self._recording(lane)
        errors = []
        rec.error.connect(errors.append)

        rec._on_continuous_tick()

        assert errors and "音频设备已断开" in errors[0]
        assert rec.is_recording is False

    def test_silent_loopback_lane_is_not_reported(self):
        lane = _make_lane(role="system")
        lane.stream = _FakeStream()
        lane.last_chunk_at -= AudioRecorder.LANE_STALL_SEC + 10
        rec = self._recording(lane)
        errors = []
        rec.error.connect(errors.append)

        rec._on_continuous_tick()

        assert errors == []
        assert rec.is_recording is True

    def test_inactive_stream_stops_hold_recording(self):
        lane = _make_lane(role="system")
        lane.stream = _FakeStream(active=False)
        rec = self._recording(lane, continuous=False)
        errors = []
        finished = []
        rec.error.connect(errors.append)
        rec.recording_finished.connect(finished.append)

        rec._on_continuous_tick()

        assert errors and rec.is_recording is False
        assert finished == []


class TestContinuousFlush:
    """FR-VAD-05: trailing speech must be flushed on stop, not lost."""

    def test_flush_emits_trailing_segment(self):
        rec = AudioRecorder()
        seg = np.ones(2000, dtype=np.float32)
        rec._segmenter = _FakeSegmenter(feed_result=None, flush_result=seg)
        rec._lanes = [_make_lane(chunks=[np.ones(1600, dtype=np.float32)])]
        emitted = []
        rec.segment_ready.connect(emitted.append)
        rec._flush_continuous_segments()
        assert len(emitted) == 1
        assert emitted[0].size == 2000

    def test_stop_continuous_flushes_then_resets(self):
        rec = AudioRecorder()
        seg = np.ones(1800, dtype=np.float32)
        seg_obj = _FakeSegmenter(feed_result=None, flush_result=seg)
        rec._segmenter = seg_obj
        rec._continuous_mode = True
        rec._is_recording = True
        rec._lanes = [_make_lane(chunks=[np.ones(1600, dtype=np.float32)])]
        emitted = []
        rec.segment_ready.connect(emitted.append)
        rec.stop_continuous()
        assert emitted and emitted[-1].size == 1800
        assert rec.is_continuous is False
        assert seg_obj.reset_called is True


class TestContinuousTick:
    def test_tick_emits_segment_when_speech_detected(self):
        rec = AudioRecorder()
        rec._is_recording = True
        rec._continuous_mode = True
        seg = np.ones(1600, dtype=np.float32)
        rec._segmenter = _FakeSegmenter(feed_result=seg)
        rec._drain_mixed_mono = lambda: np.ones(1600, dtype=np.float32)
        emitted = []
        rec.segment_ready.connect(emitted.append)
        rec._on_continuous_tick()
        assert len(emitted) == 1

    def test_tick_noop_when_not_recording(self):
        rec = AudioRecorder()
        rec._is_recording = False
        emitted = []
        rec.segment_ready.connect(emitted.append)
        rec._on_continuous_tick()
        assert emitted == []

    def test_tick_emits_no_speech_warning(self, monkeypatch):
        rec = AudioRecorder()
        rec._is_recording = True
        rec._continuous_mode = True
        rec._segmenter = _FakeSegmenter(feed_result=None)
        rec._drain_mixed_mono = lambda: np.zeros(1600, dtype=np.float32)
        rec._no_speech_warned = False
        rec._last_speech_at = 0.0  # far in the past → warning fires
        warnings = []
        rec.no_speech_warning.connect(lambda: warnings.append(True))
        rec._on_continuous_tick()
        assert warnings == [True]
        assert rec._no_speech_warned is True


class TestHoldLiveSegments:
    def test_stop_flushes_the_tail_instead_of_the_whole_buffer(self):
        rec = AudioRecorder()
        rec._is_recording = True
        rec._continuous_mode = False
        rec._segment_live = True
        rec._segmenter.feed(np.full(16000, 0.5, dtype=np.float32))
        rec._lanes = [_make_lane(chunks=[])]
        segments = []
        finished = []
        rec.segment_ready.connect(segments.append)
        rec.recording_finished.connect(finished.append)
        rec.stop()
        assert finished == []
        assert segments and segments[0].size >= 16000
        assert rec.is_recording is False


class TestStopOutput:
    def test_stop_emits_recording_finished_with_audio(self):
        rec = AudioRecorder()
        rec._is_recording = True
        rec._continuous_mode = False
        rec._lanes = [_make_lane(chunks=[np.ones(1600, dtype=np.float32)])]
        results = []
        rec.recording_finished.connect(results.append)
        rec.stop()
        assert results and results[0].size == 1600
        assert rec.is_recording is False

    def test_stop_emits_empty_when_no_chunks(self):
        rec = AudioRecorder()
        rec._is_recording = True
        rec._continuous_mode = False
        rec._lanes = [_make_lane(chunks=[])]
        results = []
        rec.recording_finished.connect(results.append)
        rec.stop()
        assert results and results[0].size == 0

    def test_cancelled_stop_emits_nothing(self):
        rec = AudioRecorder()
        rec._is_recording = True
        rec._continuous_mode = False
        rec._is_cancelled = True
        rec._lanes = [_make_lane(chunks=[np.ones(1600, dtype=np.float32)])]
        results = []
        rec.recording_finished.connect(results.append)
        rec.stop()
        assert results == []


class TestStartErrors:
    def test_start_emits_error_when_plan_fails(self, monkeypatch):
        rec = AudioRecorder()

        def _boom(*a, **k):
            raise RuntimeError("无计划可用")

        monkeypatch.setattr("sayink.audio_recorder.build_recording_plan", _boom)
        errors = []
        rec.error.connect(errors.append)
        rec.start()
        assert errors == ["无计划可用"]
        assert rec.is_recording is False


class TestFriendlyOpenErrorBranches:
    @pytest.mark.parametrize(
        "msg,needle",
        [
            ("permission denied on device", "隐私"),
            ("no device available", "恢复自动选择"),
            ("wasapi loopback failed", "系统声音"),
            ("网易虚拟声卡打开失败", "立体声混音"),
            ("something odd happened", "音频设备启动失败"),
        ],
    )
    def test_branches(self, msg, needle):
        rec = AudioRecorder()
        out = rec._friendly_open_error(msg)
        assert needle in out

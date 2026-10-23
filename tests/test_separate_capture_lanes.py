"""The two real capture lanes keep independent VAD, audio and sample clocks."""
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from sayink.audio_recorder import AudioRecorder
from sayink.audio_recovery import RecordingJournal, recordings, RecoveryRecording
from sayink.vad_segmenter import SpeechGate, SpeechSegmenter
from tests.helpers.app_harness import app_harness

RATE = 16000


@pytest.mark.parametrize("meeting", [False, True])
@pytest.mark.parametrize("context", [False, True])
def test_two_lanes_preserve_overlapping_speech_and_emit_capture_time_order(meeting, context):
    recorder = AudioRecorder()
    recorder.set_separate_lanes(True)
    recorder.set_segmenter_profile(meeting=meeting)
    recorder.set_boundary_context(context)
    recorder._lanes = [SimpleNamespace(endpoint=SimpleNamespace(role=role)) for role in ("microphone", "system")]
    recorder._prepare_split_segmenters()
    recorder._stream_anchor_ms = 1000
    for seg in recorder._split_segmenters.values():
        seg._adaptive = False
    total = 20 * RATE
    mic = np.full(total, 0.05, np.float32)
    system = np.zeros(total, np.float32)
    system[2 * RATE:3 * RATE] = 0.003
    results = []

    def collect(audio):
        results.append((audio.copy(), recorder.consume_segment_route(), recorder.consume_segment_captured_at()))

    recorder.segment_ready.connect(collect)
    with patch.object(recorder, "_remember_open_target"):
        for start in range(0, total, 1600):
            recorder._block_tracks = {"microphone": mic[start:start + 1600], "system": system[start:start + 1600]}
            recorder._feed_split_tracks((mic[start:start + 1600] + system[start:start + 1600]) / 2)
        with patch.object(recorder, "_drain_mixed_mono", return_value=None):
            recorder._flush_continuous_segments()
    assert [stamp for _, _, stamp in results] == sorted(stamp for _, _, stamp in results)
    for route, original in (("mic", mic), ("system", system)):
        coverage = np.zeros(total, np.int8)
        for audio, source, stamp in results:
            if source == route:
                begin = (stamp - 1000) * 16
                np.testing.assert_array_equal(audio, original[begin:begin + len(audio)])
                coverage[begin:begin + len(audio)] += 1
        assert np.all(coverage[original != 0] == 1)
    assert not recorder._split_ready


def test_recovery_keeps_raw_tracks_before_echo_suppression_and_mixing(tmp_path):
    mic = np.linspace(-0.05, 0.05, RATE, dtype=np.float32)
    system = np.linspace(0.01, 0.03, RATE, dtype=np.float32)
    journal = RecordingJournal(tmp_path)
    assert journal.append((mic + system) / 2, tracks={"microphone": mic, "system": system})
    assert journal.close()
    item, = recordings(tmp_path)
    assert item.available_tracks == ["audio", "microphone", "system"]
    assert item.cache_bytes == RATE * 4 * 3
    for role, original in (("microphone", mic), ("system", system)):
        track = RecoveryRecording(item.path, item.metadata, role)
        np.testing.assert_array_equal(np.concatenate(list(track.blocks())), original)


def test_quiet_neural_speech_can_open_below_energy_floor_but_digital_silence_cannot():
    class PositiveGate(SpeechGate):
        def is_speech(self, *_args, **_kwargs):
            return True

    for quiet, amplitude, expected in ((False, 0.0002, False), (True, 0.0002, True), (True, 0, False)):
        seg = SpeechSegmenter(gate=PositiveGate())
        seg.quiet_speech = quiet
        for _ in range(40):
            seg.feed(np.full(1600, amplitude, np.float32))
        assert (seg.flush() is not None) == expected


def test_energy_fallback_and_normal_recording_remain_unchanged_with_quiet_setting():
    recorder = AudioRecorder()
    recorder.set_quiet_speech(True)
    recorder.set_segmenter_profile(vad="silero", vad_model_path=None)
    assert recorder._segmenter.gate_name == "energy"
    assert not recorder._segmenter.quiet_speech


def test_immediate_stop_releases_ctc_boundary_and_short_tail_without_empty_finished(mock_recording_hardware):
    recorder = AudioRecorder()
    recorder.set_boundary_context(True)
    recorder.start()
    recorder._segmenter._adaptive = False
    output, empty = [], []
    recorder.segment_ready.connect(output.append)
    recorder.recording_finished.connect(empty.append)
    with patch.object(recorder, "_remember_open_target"):
        for _ in range(151):
            audio = recorder._feed_segmenter(np.full(1600, 0.05, np.float32))
            if audio is not None:
                recorder._emit_segment(audio)
        recorder.stop()
    assert sum(len(part) for part in output) == round(15.1 * RATE)
    assert len(output[0].suffix) == 1600
    assert len(output[1].prefix) == 4800
    assert not empty


def test_audio_options_do_not_discard_queued_work_or_stop_recording():
    with app_harness({"audio.recovery_enabled": True, "audio.separate_lanes": True}) as h:
        app = h["app"]
        app._enqueue_audio(np.ones(RATE, np.float32))
        h["recorder"].is_recording = True
        app._apply_audio_options()
        assert len(app._segment_queue) == 1
        h["recorder"].stop.assert_not_called()
        h["recorder"].set_separate_lanes.assert_called_with(True)


def test_driver_overflow_is_visible_throttled_and_does_not_drop_delivered_frames(mock_recording_hardware):
    recorder = AudioRecorder()
    recorder.start()
    lane = recorder._lanes[0]
    warnings = []
    recorder.warning.connect(warnings.append)
    callback = recorder._make_callback(lane)
    block = np.ones((1600, 1), np.float32)
    status = SimpleNamespace(input_overflow=True)
    with patch("sayink.audio_recorder.time.monotonic", return_value=20):
        for _ in range(3):
            callback(block, 1600, None, status)
    assert len(warnings) == 1 and "缺口" in warnings[0]
    assert sum(len(chunk) for chunk in lane.chunks) == 4800
    recorder.stop()


def test_device_disconnect_flushes_hold_speech_before_reporting_error(mock_recording_hardware):
    recorder = AudioRecorder()
    recorder.start()
    recorder._segmenter._adaptive = False
    output = []
    recorder.segment_ready.connect(output.append)
    with patch.object(recorder, "_remember_open_target"):
        for _ in range(7):
            recorder._feed_segmenter(np.full(1600, 0.1, np.float32))
        recorder._handle_lost_device(recorder._lanes[0])
    assert sum(len(audio) for audio in output) == 11200
    assert not recorder.is_recording


def test_rebuild_flushes_held_boundary_and_resets_its_sample_clock(mock_recording_hardware):
    recorder = AudioRecorder()
    recorder.set_boundary_context(True)
    recorder.start()
    recorder._segmenter._adaptive = False
    output = []
    recorder.segment_ready.connect(output.append)
    with patch.object(recorder, "_remember_open_target"):
        for _ in range(150):
            audio = recorder._feed_segmenter(np.full(1600, 0.05, np.float32))
            if audio is not None:
                recorder._emit_segment(audio)
        assert not output
        recorder.set_segmenter_profile()
    assert len(output) == 1 and len(output[0]) == 15 * RATE
    assert recorder._boundary_buffer._stop == 0
    assert recorder._segmenter.pending_start == 0
    recorder.stop()

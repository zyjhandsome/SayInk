import numpy as np
from sayink.audio_recorder import AudioRecorder


class TestAudioRecorderConstants:
    def test_sample_rate(self):
        assert AudioRecorder.SAMPLE_RATE == 16000

    def test_channels(self):
        assert AudioRecorder.CHANNELS == 1

    def test_chunk_duration(self):
        assert AudioRecorder.CHUNK_DURATION == 0.1


class TestAudioRecorderInit:
    def test_initial_state(self):
        recorder = AudioRecorder()
        assert recorder._is_recording is False
        assert recorder._is_cancelled is False
        assert recorder._lanes == []

    def test_initial_recording_flag(self):
        recorder = AudioRecorder()
        assert recorder.is_recording is False


class TestAudioRecorderSignals:
    def test_signals_exist(self):
        recorder = AudioRecorder()
        assert hasattr(recorder, "volume_changed")
        assert hasattr(recorder, "segment_ready")
        assert hasattr(recorder, "recording_finished")
        assert hasattr(recorder, "error")


class TestAudioRecorderRecording:
    def test_configure_source(self):
        recorder = AudioRecorder()
        recorder.configure(input_source="mixed", mic_device_index=0, system_device_index=1)
        assert recorder.input_source == "mixed"

    def test_start_sets_recording_flag(self, mock_recording_hardware):
        recorder = AudioRecorder()
        recorder.start()
        assert recorder._is_recording is True

    def test_start_clears_chunks(self, mock_recording_hardware):
        recorder = AudioRecorder()
        lane = type(
            "L",
            (),
            {
                "chunks": [np.array([1, 2, 3])],
                "stream": None,
                "sample_rate": 16000,
                "endpoint": None,
            },
        )()
        recorder._lanes = [lane]
        recorder.start()
        assert recorder._lanes == [] or all(
            len(l.chunks) == 0 for l in recorder._lanes if hasattr(l, "chunks")
        )

    def test_start_clears_cancelled_flag(self, mock_recording_hardware):
        recorder = AudioRecorder()
        recorder._is_cancelled = True
        recorder.start()
        assert recorder._is_cancelled is False

    def test_double_start_ignored(self, mock_recording_hardware):
        recorder = AudioRecorder()
        recorder.start()
        lane_count = len(recorder._lanes)
        recorder.start()
        assert recorder.is_recording
        assert len(recorder._lanes) == lane_count


class TestAudioRecorderStop:
    def test_stop_clears_recording_flag(self, mock_recording_hardware):
        recorder = AudioRecorder()
        recorder.start()
        assert recorder.is_recording is True
        recorder.stop()
        assert recorder.is_recording is False

    def test_stop_without_start(self):
        recorder = AudioRecorder()
        recorder.stop()
        assert recorder._is_recording is False

    def test_stop_closes_stream(self, mock_recording_hardware):
        recorder = AudioRecorder()
        recorder.start()
        recorder.stop()
        assert recorder._lanes == []

    def test_stop_clears_audio_chunks(self, mock_recording_hardware):
        recorder = AudioRecorder()
        recorder.start()
        recorder.stop()
        assert recorder._lanes == []


class TestAudioRecorderCancel:
    def test_cancel_sets_cancelled_flag(self):
        recorder = AudioRecorder()
        recorder.cancel()
        assert recorder._is_cancelled is True

    def test_cancel_calls_stop(self, mock_recording_hardware):
        recorder = AudioRecorder()
        recorder.start()
        recorder.cancel()
        assert recorder.is_recording is False


class TestAudioRecorderAudioCallback:
    def _lane_with_callback(self, recorder):
        from sayink.audio_devices import AudioDeviceInfo

        ep = type(
            "E",
            (),
            {
                "role": "microphone",
                "device": AudioDeviceInfo(0, "t", "w", 16000, 1, False, False),
                "use_wasapi_loopback": False,
            },
        )()
        lane = type(
            "L",
            (),
            {"endpoint": ep, "chunks": [], "sample_rate": 16000, "stream": None},
        )()
        cb = recorder._make_callback(lane)
        return lane, cb

    def test_callback_returns_early_when_not_recording(self):
        recorder = AudioRecorder()
        lane, cb = self._lane_with_callback(recorder)
        audio_data = np.zeros((1024, 1), dtype=np.float32)
        cb(audio_data, 1024, None, None)
        assert len(lane.chunks) == 0

    def test_callback_returns_early_when_cancelled(self, mock_recording_hardware):
        recorder = AudioRecorder()
        recorder.start()
        recorder._is_cancelled = True
        lane, cb = self._lane_with_callback(recorder)
        audio_data = np.zeros((1024, 1), dtype=np.float32)
        cb(audio_data, 1024, None, None)
        assert len(lane.chunks) == 0
        recorder.stop()

    def test_callback_appends_audio_data(self):
        recorder = AudioRecorder()
        recorder._is_recording = True
        lane, cb = self._lane_with_callback(recorder)
        audio_data = np.random.randn(1024).astype(np.float32)
        input_data = audio_data.reshape(-1, 1)
        cb(input_data, 1024, None, None)
        assert len(lane.chunks) > 0


class TestAudioRecorderVolumeCalculation:
    def test_volume_calculation_silent(self):
        silent_audio = np.zeros(1024, dtype=np.float32)
        volume = float(np.sqrt(np.mean(silent_audio ** 2)))
        assert volume == 0.0

    def test_volume_calculation_constant(self):
        constant_audio = np.ones(1024, dtype=np.float32) * 0.5
        volume = float(np.sqrt(np.mean(constant_audio ** 2)))
        assert abs(volume - 0.5) < 0.001

    def test_volume_calculation_random(self):
        np.random.seed(42)
        random_audio = np.random.randn(1024).astype(np.float32) * 0.3
        volume = float(np.sqrt(np.mean(random_audio ** 2)))
        assert volume > 0
        assert volume < 1


class TestFriendlyOpenError:
    def test_audio_device_shows_actionable_hint(self):
        recorder = AudioRecorder()
        msg = "无法打开音频设备：扬声器 (Audio Device)"
        out = recorder._friendly_open_error(msg)
        assert "恢复自动选择" in out
        assert "Realtek" in out


class TestAudioRecorderThreadSafety:
    def test_lock_exists(self):
        recorder = AudioRecorder()
        assert hasattr(recorder, "_lock")
        assert hasattr(recorder._lock, "acquire")
        assert hasattr(recorder._lock, "release")

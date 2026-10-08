"""AudioRecorder unit tests that avoid opening real hardware (README 声音收录)."""

import pytest

from sayink.audio_recorder import AudioRecorder
from sayink.vad_segmenter import SPEECH_RMS_THRESHOLD


class TestAudioRecorderConfigure:
    def test_system_source_uses_lower_vad_threshold(self):
        rec = AudioRecorder()
        rec.configure(input_source="system")
        assert rec._segmenter._speech_threshold == 0.0006

    def test_microphone_source_uses_default_vad_threshold(self):
        rec = AudioRecorder()
        rec.configure(input_source="microphone")
        assert rec._segmenter._speech_threshold == SPEECH_RMS_THRESHOLD

    def test_mixed_source_uses_default_vad_threshold(self):
        rec = AudioRecorder()
        rec.configure(input_source="mixed")
        assert rec._segmenter._speech_threshold == SPEECH_RMS_THRESHOLD

    def test_input_source_display_labels(self):
        rec = AudioRecorder()
        rec.configure(input_source="system")
        assert "电脑" in rec.input_source_display or "播放" in rec.input_source_display

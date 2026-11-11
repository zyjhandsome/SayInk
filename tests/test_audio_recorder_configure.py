"""AudioRecorder unit tests that avoid opening real hardware (README 声音收录)."""


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

    def test_quiet_speech_tri_state_and_vad_backend_alias(self, monkeypatch):
        from sayink.vad_segmenter import SpeechGate, VAD_ENERGY, VAD_SILERO

        class FakeSilero(SpeechGate):
            name = VAD_SILERO

            def __init__(self):
                self.input_gain = 1.0

            def is_speech(self, *_args, **_kwargs):
                return False

        rec = AudioRecorder()
        assert rec._quiet_speech is None
        assert rec.vad_backend == VAD_ENERGY

        monkeypatch.setattr("sayink.audio_recorder.make_speech_gate", lambda *_a, **_kw: FakeSilero())
        rec.set_segmenter_profile(vad=VAD_SILERO, vad_model_path="silero.onnx")
        assert rec.vad_backend == VAD_SILERO
        assert rec._segmenter.quiet_boost is None
        assert rec._segmenter.gate.input_gain == 1.0

        rec.set_quiet_speech(True)
        assert rec._segmenter.quiet_boost is True
        assert rec._segmenter.gate.input_gain == 4.0

        rec.set_quiet_speech(False)
        assert rec._segmenter.quiet_boost is False
        assert rec._segmenter.gate.input_gain == 1.0

        rec.set_quiet_speech(None)
        assert rec._segmenter.quiet_boost is None
        assert rec._segmenter.gate.input_gain == 1.0

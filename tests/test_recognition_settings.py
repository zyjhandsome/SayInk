"""Shared capabilities: inheritance, real gate state, and safe session changes."""

from unittest.mock import patch

import numpy as np
import pytest

from sayink.audio_recorder import AudioRecorder
from sayink.meeting_session import MeetingOptions, resolve_capabilities
from sayink.recognition_settings import RecognitionSettings, audio_source
from sayink.runtime_status import SegmentationStatus
from sayink.vad_segmenter import EnergyGate
from tests.helpers.app_harness import app_harness


@pytest.mark.parametrize("source", ["microphone", "system", "mixed"])
def test_meeting_follows_shared_audio_until_explicitly_overridden(config, source):
    config.set("audio.input_source", source)
    assert audio_source(config, meeting=True) == source
    assert MeetingOptions.from_config(config).input_source == source
    config.set("meeting.input_source", "system")
    assert MeetingOptions.from_config(config).input_source == "system"
    config.set("meeting.input_source", "")
    assert MeetingOptions.from_config(config).input_source == source


def test_shared_settings_are_snapshots_and_match_meeting_parameters(config, monkeypatch):
    config.set("stt.glossary", [{"term": "SayInk"}])
    config.set("stt.language", "en")
    shared = RecognitionSettings.from_config(config)
    monkeypatch.setattr("sayink.meeting_session.aux_model_path", lambda _mid: None)
    caps = resolve_capabilities(config, MeetingOptions.from_config(config), dictation_model_id=shared.model_id)
    assert (caps.hotwords, caps.language) == (shared.hotwords, shared.effective_language)
    config.get("stt.glossary")[0]["term"] = "changed"
    assert shared.hotwords == "SayInk"


def test_explicit_energy_choice_does_not_claim_silero_is_missing(config, monkeypatch):
    monkeypatch.setattr("sayink.meeting_session.aux_model_path", lambda _mid: None)
    caps = resolve_capabilities(config, MeetingOptions(vad="energy"), dictation_model_id="funasr-nano")
    assert not any("Silero" in note for note in caps.notes)


def test_recorder_reports_downloaded_model_load_failure(monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("invalid onnx")

    monkeypatch.setattr("sayink.vad_segmenter.SileroGate", broken)
    recorder = AudioRecorder()
    changes = []
    recorder.segmentation_changed.connect(changes.append)
    recorder.set_segmenter_profile(vad="silero", vad_model_path="broken.onnx")
    assert changes[0].effective == ""  # loading, then the real fallback
    assert recorder.vad_name == "energy"
    assert recorder.segmentation_status.error == "invalid onnx"
    assert "加载失败" in recorder.segmentation_status.label
    assert "已回退" in recorder.segmentation_status.label


def test_recorder_reports_success_only_after_gate_loads(monkeypatch):
    class WorkingGate(EnergyGate):
        name = "silero"

    monkeypatch.setattr("sayink.audio_recorder.make_speech_gate", lambda _kind, seg, **_kw: WorkingGate(seg))
    recorder = AudioRecorder()
    recorder.set_segmenter_profile(vad="silero", vad_model_path="working.onnx")
    assert recorder.vad_name == "silero"
    assert "已加载" in recorder.segmentation_status.label


@pytest.mark.parametrize("continuous", [False, True])
def test_recognition_changes_wait_for_capture_and_queued_speech(continuous):
    with app_harness({"audio.trigger_mode": "continuous" if continuous else "hotkey"}) as h:
        app, recorder = h["app"], h["recorder"]
        recorder.is_recording = True
        recorder.is_continuous = continuous
        recorder.set_segmenter_profile.reset_mock()
        h["recognizer"].configure.reset_mock()
        app._config.set("stt.vad", "energy")
        app._config.set("stt.language", "en")
        app._on_recognition_settings_changed()
        assert app._recognition_change_pending
        recorder.set_segmenter_profile.assert_not_called()
        recorder.stop_continuous.assert_not_called()
        h["recognizer"].configure.assert_not_called()

        app._on_segment_ready(np.ones(3200, dtype=np.float32))
        app._on_segment_ready(np.ones(3200, dtype=np.float32))
        recorder.is_recording = recorder.is_continuous = False
        app._apply_pending_recognition_settings()
        assert app._recognition_change_pending  # recognition and queue still belong to this session
        paste_count = 0
        for text in ("第一句", "第二句"):
            app._on_final_result(text)
            if h["paster"].paste_async.call_count > paste_count:
                h["paster"].paste_async.call_args.args[1]("sent")
                paste_count = h["paster"].paste_async.call_count
            app._pump_segment_queue()
        saved = "".join(call.args[0].raw_text for call in h["history"].enqueue.call_args_list)
        assert "第一句" in saved and "第二句" in saved
        assert not app._recognition_change_pending
        recorder.set_segmenter_profile.assert_called_once_with(vad="energy", vad_model_path=None, meeting=False)
        assert h["recognizer"].configure.call_args.kwargs["language"] == "en"


def test_meeting_uses_actual_gate_when_downloaded_model_fails():
    with app_harness() as h:
        app, recorder = h["app"], h["recorder"]
        recorder.segmentation_status = SegmentationStatus("silero", "energy", True, "bad model", True)
        recorder.start_continuous.side_effect = lambda: setattr(recorder, "is_continuous", True)
        assert app.start_meeting(MeetingOptions())
        assert app.meeting.capabilities.vad == "energy"
        assert app.meeting.current.vad == "energy"
        assert any("加载失败" in note for note in app.meeting.capabilities.notes)


def test_meeting_validates_the_session_model_override():
    with app_harness({"meeting.model_id": "missing-model"}) as h:
        recorder = h["recorder"]
        recorder.start_continuous.side_effect = lambda: setattr(recorder, "is_continuous", True)
        with patch("sayink.speech_recognizer.is_model_downloaded", side_effect=lambda mid: mid != "missing-model"):
            assert h["app"].start_meeting(MeetingOptions(model_id="funasr-nano"))


def test_pending_model_change_keeps_captured_segments_on_the_old_model():
    with app_harness({"audio.trigger_mode": "continuous"}) as h:
        app = h["app"]
        old_model = app._applied_recognition_settings.model_id
        h["recorder"].is_recording = True
        h["recorder"].is_continuous = True
        app._on_tray_model_switch("sensevoice")
        assert app._capture_segment_context().model == old_model
        h["recorder"].stop_continuous.assert_not_called()


def test_meeting_keeps_settings_until_finalize():
    with app_harness() as h:
        app, recorder = h["app"], h["recorder"]
        recorder.start_continuous.side_effect = lambda: setattr(recorder, "is_continuous", True)
        assert app.start_meeting(MeetingOptions())
        recorder.set_segmenter_profile.reset_mock()
        app._config.set("stt.vad", "energy")
        app._on_recognition_settings_changed()
        assert app._recognition_change_pending
        recorder.set_segmenter_profile.assert_not_called()
        recorder.is_recording = recorder.is_continuous = False
        with patch("sayink.app.aux_model_path", return_value=None):
            app.meeting.finalize()
        assert not app._recognition_change_pending
        recorder.set_segmenter_profile.assert_called_with(vad="energy", vad_model_path=None, meeting=False)


def test_dictation_spells_english_the_way_the_glossary_does():
    with app_harness({"audio.trigger_mode": "hotkey", "stt.glossary": [{"term": "GitHub", "variants": []}]}) as h:
        app = h["app"]
        app._on_recording_finished(np.ones(16000, dtype=np.float32))
        app._on_final_result("代码在GITHUB上")
        assert h["paster"].paste_async.call_args.args[0] == "代码在GitHub上"


def test_meeting_rows_follow_the_meetings_own_glossary():
    with app_harness({"stt.glossary": [{"term": "Github"}]}) as h:
        app, recorder = h["app"], h["recorder"]
        recorder.start_continuous.side_effect = lambda: setattr(recorder, "is_continuous", True)
        assert app.start_meeting(MeetingOptions(glossary=[{"term": "GitHub", "variants": []}]))
        app._on_segment_ready(np.ones(6400, dtype=np.float32))
        app._on_final_result("提交到GITHUB")
        rows = [call.args[0] for call in h["history"].enqueue.call_args_list]
        assert rows[-1].raw_text == "提交到GitHub"

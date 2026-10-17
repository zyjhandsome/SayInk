"""App ⇄ MeetingController seams: one microphone owner, meeting rows tagged
and stored even with history off, hotkeys parked, end → drain → restore."""

from __future__ import annotations

from unittest.mock import patch

import numpy as np

from sayink.audio_utils import TARGET_SAMPLE_RATE
from sayink.meeting_session import MeetingOptions, MeetingState
from sayink.vad_segmenter import VAD_ENERGY
from tests.helpers.app_harness import app_harness


def _audio(seconds: float = 0.4) -> np.ndarray:
    return np.ones(int(TARGET_SAMPLE_RATE * seconds), dtype=np.float32)


def _enqueued_records(history):
    return [call.args[0] for call in history.enqueue.call_args_list]


def _start(app, h, **options):
    h["recorder"].consume_segment_captured_at.return_value = 1_700_000_000_000

    def _opened():
        h["recorder"].is_continuous = True

    h["recorder"].start_continuous.side_effect = _opened
    with patch("sayink.meeting_session.aux_model_path", return_value=None):
        ok = app.start_meeting(MeetingOptions(**options))
    assert ok, "meeting should start"
    return app.meeting.current_id


def test_start_meeting_switches_pipeline_and_tags_rows() -> None:
    with app_harness({"audio.trigger_mode": "hotkey", "audio.input_source": "microphone",
                      "history.enabled": False}) as h:
        app = h["app"]
        meeting_id = _start(app, h, input_source="mixed", title="周会")

        assert app.meeting.state is MeetingState.LIVE
        assert app._is_continuous_mode()
        h["recorder"].set_segmenter_profile.assert_called_with(vad=VAD_ENERGY, vad_model_path=None, meeting=True)
        assert h["recorder"].configure.call_args.kwargs["input_source"] == "mixed"
        recognizer_call = h["recognizer"].configure.call_args
        assert recognizer_call.kwargs["language"] == "zh"
        h["recorder"].start_continuous.assert_called_once()
        h["floating"].show_meeting.assert_called()
        h["tray"].set_meeting_in_progress.assert_called_with(True)

        # A segment captured during the meeting is a meeting row: history is
        # off, but meeting transcripts are not dictation history.
        h["recorder"].input_source = "mixed"
        app._on_segment_ready(_audio())
        app._on_final_result("第一句话")

        h["paster"].paste_async.assert_not_called()
        records = _enqueued_records(h["history"])
        assert len(records) == 1
        row = records[0]
        assert row.session_id == meeting_id
        assert row.trigger_mode == "meeting"
        assert row.captured_at == 1_700_000_000_000
        assert row.raw_text == "第一句话"
        assert row.speaker_id >= 1
        assert h["floating"].show_success.call_args[0][0] == "已记录"


def test_meeting_segments_skip_per_sentence_polish() -> None:
    overrides = {
        "llm.enabled": True, "llm.api_url": "http://localhost:1234/v1",
        "llm.api_key": "k", "llm.model_name": "m",
    }
    with app_harness(overrides) as h:
        app = h["app"]
        _start(app, h)
        app._on_segment_ready(_audio())
        app._on_final_result("不润色")
        h["polisher"].polish.assert_not_called()
        assert _enqueued_records(h["history"])[0].raw_text == "不润色"


def test_hotkeys_are_parked_during_a_meeting() -> None:
    with app_harness({"audio.trigger_mode": "hotkey"}) as h:
        app = h["app"]
        _start(app, h)
        h["recorder"].start.reset_mock()
        app._on_recording_start()
        h["recorder"].start.assert_not_called()
        h["floating"].show_meeting_paused.assert_called()
        app._on_esc_pressed()
        assert app.meeting.is_active
        app._on_recording_cancel()
        h["recorder"].cancel.assert_not_called()


def test_continuous_hotkey_cannot_start_second_session_during_meeting() -> None:
    with app_harness({"audio.trigger_mode": "continuous"}) as h:
        app = h["app"]
        _start(app, h)
        with patch.object(app, "_start_continuous_listening") as start:
            app._on_continuous_hotkey_start()
            start.assert_not_called()


def test_end_meeting_drains_then_restores_dictation() -> None:
    with app_harness({"audio.trigger_mode": "hotkey", "audio.input_source": "microphone"}) as h:
        app = h["app"]
        meeting_id = _start(app, h, input_source="mixed")
        ended = []
        app.meeting.meeting_ended.connect(ended.append)

        # One utterance still in flight when the user ends the meeting.
        app._on_segment_ready(_audio())
        h["recorder"].configure.reset_mock()
        h["recognizer"].configure.reset_mock()
        from PyQt6.QtCore import QCoreApplication
        import time

        # Pin "Silero not downloaded" so the assertion below does not depend
        # on this machine's models directory.
        with patch("sayink.app.aux_model_path", return_value=None):
            app._end_meeting()
            h["recorder"].stop_continuous.assert_called()
            h["recorder"].is_continuous = False
            assert app.meeting.state is MeetingState.DRAINING
            assert ended == []

            app._on_final_result("最后一句")
            deadline = time.monotonic() + 2.0
            while not ended and time.monotonic() < deadline:
                QCoreApplication.processEvents()
                time.sleep(0.01)
        assert ended == [meeting_id]
        assert not app.meeting.is_active
        assert not app._is_continuous_mode()
        # Dictation gets its own device and model back; the segmenter follows
        # the shared stt.vad choice (silero model not downloaded → energy).
        h["recorder"].set_segmenter_profile.assert_called_with(vad="energy", vad_model_path=None, meeting=False)
        assert h["recorder"].configure.call_args.kwargs["input_source"] == "microphone"
        h["recognizer"].configure.assert_called()
        h["tray"].set_meeting_in_progress.assert_called_with(False)
        h["floating"].end_meeting_state.assert_called()
        rows = _enqueued_records(h["history"])
        assert [r.raw_text for r in rows] == ["最后一句"]
        assert rows[0].session_id == meeting_id


def test_recorder_error_pauses_meeting_instead_of_ending_it() -> None:
    with app_harness() as h:
        app = h["app"]
        _start(app, h)
        app._on_recorder_error("device gone")
        assert app.meeting.state is MeetingState.PAUSED
        assert app.meeting.is_active
        h["recorder"].stop_continuous.assert_called()
        h["recorder"].is_continuous = False
        h["recorder"].start_continuous.reset_mock()
        app.resume_meeting_capture()
        h["recorder"].start_continuous.assert_called_once()
        assert app.meeting.state is MeetingState.LIVE


def test_device_open_failure_on_resume_keeps_meeting_paused() -> None:
    with app_harness() as h:
        app = h["app"]
        _start(app, h)
        app._on_recorder_error("device gone")
        h["recorder"].is_continuous = False
        h["recorder"].start_continuous.side_effect = None  # device stays closed
        app.resume_meeting_capture()
        assert app.meeting.state is MeetingState.PAUSED
        assert "无法开启" in h["tray"].showMessage.call_args[0][1]


def test_start_meeting_takes_over_a_running_continuous_session() -> None:
    with app_harness({"audio.trigger_mode": "continuous"}) as h:
        app = h["app"]
        h["recorder"].is_continuous = True
        app._current_session_id = "dictation-session"
        _start(app, h)
        h["recorder"].stop_continuous.assert_called()
        assert app._current_session_id == app.meeting.current_id
        assert app._current_seq == 0


def test_start_meeting_refused_while_holding_the_hotkey() -> None:
    with app_harness() as h:
        app = h["app"]
        h["recorder"].is_recording = True
        with patch("sayink.meeting_session.aux_model_path", return_value=None):
            assert app.start_meeting(MeetingOptions()) is False
        assert not app.meeting.is_active
        h["floating"].show_error.assert_called()


def test_confirm_exit_mentions_the_live_meeting() -> None:
    with app_harness() as h:
        app = h["app"]
        _start(app, h)
        with patch("sayink.app.QMessageBox") as box_cls:
            box = box_cls.return_value
            discard = object()
            box.addButton.side_effect = [object(), discard]
            box.clickedButton.return_value = discard
            assert app._confirm_exit() is True
            assert "会议" in box.setText.call_args[0][0]


def test_settings_save_during_meeting_does_not_touch_audio() -> None:
    with app_harness() as h:
        app = h["app"]
        _start(app, h)
        h["recorder"].configure.reset_mock()
        h["recognizer"].configure.reset_mock()
        app._on_settings_changed()
        h["recorder"].configure.assert_not_called()
        h["recognizer"].configure.assert_not_called()
        h["recorder"].stop_continuous.assert_not_called()


def test_request_meeting_start_prompts_once_for_aux_models() -> None:
    missing = [{"id": "speaker-campplus", "name": "说话人嵌入 CAM++", "size_mb": 28}]
    with app_harness({"meeting.aux_prompted": False}) as h:
        app = h["app"]
        h["recorder"].start_continuous.side_effect = lambda: setattr(h["recorder"], "is_continuous", True)
        asked = []

        def _ask(models):
            asked.append([m["id"] for m in models])
            return "cancel"

        with patch("sayink.aux_models.missing_aux_models", return_value=missing), \
             patch.object(app, "_ask_aux_download", side_effect=_ask), \
             patch("sayink.meeting_session.aux_model_path", return_value=None):
            app._request_meeting_start(MeetingOptions())
            assert asked == [["speaker-campplus"]]
            assert not app.meeting.is_active                   # cancel → no meeting
            assert app._config.get("meeting.aux_prompted") is False   # may ask again

            asked.clear()
            with patch.object(app, "_ask_aux_download", return_value="start") as ask:
                app._request_meeting_start(MeetingOptions())
                ask.assert_called_once()
            assert app.meeting.is_active
            assert app._config.get("meeting.aux_prompted") is True
            app._end_meeting()
            app.meeting.finalize()

            # Asked once per install: the second meeting starts straight away.
            with patch.object(app, "_ask_aux_download") as ask:
                app._request_meeting_start(MeetingOptions())
                ask.assert_not_called()
            assert app.meeting.is_active


def test_request_meeting_start_skips_prompt_when_nothing_missing() -> None:
    with app_harness({"meeting.aux_prompted": False}) as h:
        app = h["app"]
        h["recorder"].start_continuous.side_effect = lambda: setattr(h["recorder"], "is_continuous", True)
        with patch("sayink.aux_models.missing_aux_models", return_value=[]), \
             patch.object(app, "_ask_aux_download") as ask, \
             patch("sayink.meeting_session.aux_model_path", return_value=None):
            app._request_meeting_start(MeetingOptions())
            ask.assert_not_called()
            assert app.meeting.is_active


def test_download_then_start_waits_for_the_batch() -> None:
    from PyQt6.QtCore import QObject, pyqtSignal

    class _FakeBatch(QObject):
        progress = pyqtSignal(int)
        finished = pyqtSignal(bool, str)
        instances: list = []

        def __init__(self, source, parent=None):
            super().__init__(parent)
            self.cancelled = False
            _FakeBatch.instances.append(self)

        def start(self, ids=None):
            return True

        def cancel(self):
            self.cancelled = True

    with app_harness({"meeting.aux_prompted": False}) as h:
        app = h["app"]
        h["recorder"].start_continuous.side_effect = lambda: setattr(h["recorder"], "is_continuous", True)
        with patch("sayink.aux_models.AuxModelDownloads", _FakeBatch), \
             patch("sayink.meeting_session.aux_model_path", return_value=None):
            app._download_aux_then_start(MeetingOptions(title="等模型"))
            assert not app.meeting.is_active                   # still downloading
            batch = _FakeBatch.instances[-1]
            batch.progress.emit(60)
            batch.finished.emit(True, "")
            assert app.meeting.is_active
            assert app.meeting.current.title == "等模型"

"""App ⇄ MeetingController seams: one microphone owner, meeting rows tagged
and stored even with history off, hotkeys parked, end → drain → restore."""

from __future__ import annotations

import sqlite3
import tempfile
import time
from unittest.mock import patch

import numpy as np
from PyQt6.QtCore import QCoreApplication

from sayink.audio_utils import TARGET_SAMPLE_RATE
from sayink.meeting_session import MeetingOptions, MeetingState
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
        h["recorder"].set_segmenter_profile.assert_called_with(vad="silero", vad_model_path=None, meeting=True)
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
        # This fixture is too short to establish a voice identity. Its words
        # are kept while attribution stays explicitly unknown.
        assert row.speaker_id == 0
        h["recognizer"].transcribe_final.assert_called_once()
        call = h["recognizer"].transcribe_final.call_args
        assert call.kwargs == {"retry_empty": True}
        assert np.array_equal(call.args[0], _audio())
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
        h["floating"].show_meeting_paused.assert_not_called()
        assert "转写快捷键" in h["floating"].show_meeting.call_args.kwargs["detail"]
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
        h["recorder"].set_segmenter_profile.assert_called_with(vad="silero", vad_model_path=None, meeting=False)
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


def test_user_pause_stops_capture_without_ending_or_auto_resuming() -> None:
    with app_harness() as h:
        app = h["app"]
        meeting_id = _start(app, h)
        app._meeting_capture_pending = True
        app.pause_meeting_capture()
        assert app.meeting.state is MeetingState.PAUSED
        assert app.meeting.current_id == meeting_id
        assert app._meeting_user_paused
        assert not app._meeting_capture_pending
        h["recorder"].stop_continuous.assert_called()
        h["recorder"].is_continuous = False
        h["recorder"].start_continuous.reset_mock()
        # Finishing recognition/model loading must not reopen a user-paused mic.
        app._meeting_capture_pending = True
        app._resume_meeting_capture_if_pending()
        app._start_meeting_capture()
        h["recorder"].start_continuous.assert_not_called()
        app.resume_meeting_capture()
        h["recorder"].start_continuous.assert_called_once()
        assert app.meeting.state is MeetingState.LIVE
        assert app.meeting.current_id == meeting_id
        app.pause_meeting_capture()
        app._end_meeting()
        assert not app.meeting.is_active
        assert not app._meeting_user_paused


def test_failed_auto_resume_replaces_the_auto_continue_promise() -> None:
    with app_harness() as h:
        app = h["app"]
        _start(app, h)
        app._meeting.pause("识别跟不上，已暂停采集 · 处理完会自动继续")
        app._meeting_capture_pending = True
        h["recorder"].is_continuous = False
        h["recorder"].start_continuous.side_effect = None
        h["recognizer"].is_ready = True
        app._segment_queue.clear()
        app._resume_meeting_capture_if_pending()
        assert app.meeting.state is MeetingState.PAUSED
        assert app.meeting.paused_detail == "音频设备打开失败 · 请检查设备后在会议页「继续采集」"
        assert not app._meeting_capture_pending


def test_recorder_error_while_user_paused_does_not_hide_the_meeting_bar() -> None:
    with app_harness() as h:
        app = h["app"]
        _start(app, h)
        app.pause_meeting_capture()
        h["floating"].show_error.reset_mock()
        h["recorder"].is_continuous = False
        app._on_recorder_error("device gone")
        h["floating"].show_error.assert_not_called()
        assert app.meeting.state is MeetingState.PAUSED
        assert app._meeting_user_paused
        assert "你已暂停" in app.meeting.paused_detail


def test_recognizer_error_during_meeting_keeps_the_bar() -> None:
    with app_harness() as h:
        app = h["app"]
        _start(app, h)
        h["floating"].show_error.reset_mock()
        h["recognizer"].is_ready = True
        app._on_recognizer_error("decode failed")
        h["floating"].show_error.assert_not_called()
        assert app.meeting.state is MeetingState.LIVE
        assert app.meeting.is_active


def test_failed_model_load_while_draining_ends_the_meeting() -> None:
    from sayink.app import _QueuedSegment
    from sayink.runtime_status import ModelLoadPhase

    with app_harness() as h:
        app = h["app"]
        _start(app, h)
        h["recognizer"].is_ready = False
        h["recognizer"].is_loading = True
        app._segment_queue.append(_QueuedSegment(audio=_audio()))
        app._end_meeting()
        assert app.meeting.state is MeetingState.DRAINING
        h["recognizer"].is_loading = False
        h["floating"].show_error.reset_mock()
        app._on_model_load_progress(ModelLoadPhase.FAILED, "boom")
        deadline = time.monotonic() + 0.6
        while time.monotonic() < deadline and app.meeting.is_active:
            QCoreApplication.processEvents()
            time.sleep(0.01)
        h["floating"].show_error.assert_not_called()
        assert not app.meeting.is_active


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


def test_start_meeting_refused_when_the_history_db_is_unavailable() -> None:
    """Every transcript row goes through the history DB: with it disabled the
    meeting would run with a blank transcript and save nothing."""
    with app_harness() as h:
        app = h["app"]
        h["history"].disabled = True
        with patch("sayink.meeting_session.aux_model_path", return_value=None):
            assert app.start_meeting(MeetingOptions()) is False
        assert not app.meeting.is_active
        assert "历史数据库不可用" in h["floating"].show_error.call_args[0][0]
        h["recorder"].start_continuous.assert_not_called()


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
    from typing import ClassVar

    from PyQt6.QtCore import QObject, pyqtSignal

    class _FakeBatch(QObject):
        progress = pyqtSignal(int)
        finished = pyqtSignal(bool, str)
        instances: ClassVar[list] = []

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


AUTO_MINUTES = {
    "llm.api_url": "http://192.168.1.10:8000/v1", "llm.api_key": "k", "llm.model_name": "m",
    "meeting.auto_generate": True, "audio.trigger_mode": "hotkey",
}
# Varied on purpose: repeated phrases are dropped as ASR hallucinations.
LONG_TEXT = (
    "今天先过一下第三季度的预算执行情况，市场部超支了大约百分之八。"
    "主要原因是线下活动比计划多了两场，下个月会压缩差旅。"
    "研发这边的新版本推迟一周上线，测试发现支付回调在弱网下偶尔丢失。"
    "客服反馈退款流程太长，希望把人工审核改成金额阈值以下自动通过。"
    "法务提醒隐私协议需要在十一月前更新，涉及第三方统计组件的说明。"
    "运维提出机房迁移窗口定在月底周六凌晨，各业务方需要提前确认回滚方案。"
    "最后确认下周三下午做一次跨部门复盘，每个团队准备十分钟的汇报材料。"
)


def _end_and_wait(app, h, meeting_id) -> None:
    ended = []
    app.meeting.meeting_ended.connect(ended.append)
    app._end_meeting()
    h["recorder"].is_continuous = False
    deadline = time.monotonic() + 2.0
    while not ended and time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.01)
    assert ended == [meeting_id]


def test_meeting_end_with_auto_minutes_says_they_run_in_the_background() -> None:
    with app_harness(AUTO_MINUTES, real_history=True) as h:
        app = h["app"]
        requests = []
        with patch.object(app._meeting._generator, "generate", requests.append):
            meeting_id = _start(app, h)
            app._on_segment_ready(_audio())
            app._on_final_result(LONG_TEXT)
            _end_and_wait(app, h, meeting_id)
        assert len(requests) == 1
        assert "后台" in h["tray"].showMessage.call_args[0][1]


def test_unconfirmed_remote_minutes_do_not_claim_they_already_started() -> None:
    overrides = {
        **AUTO_MINUTES,
        "llm.api_url": "https://api.example.com/v1",
        "meeting.confirmed_remote_hosts": [],
    }
    with app_harness(overrides, real_history=True) as h:
        app = h["app"]
        requests = []
        with patch.object(app._meeting._generator, "generate", requests.append):
            meeting_id = _start(app, h)
            app._on_segment_ready(_audio())
            app._on_final_result(LONG_TEXT)
            _end_and_wait(app, h, meeting_id)
        texts = [call.args[1] for call in h["tray"].showMessage.call_args_list]
        assert requests == []
        assert texts
        assert all("正在后台生成" not in text for text in texts)
        assert any("尚未确认" in text for text in texts)


def test_meeting_end_without_auto_minutes_points_to_the_meeting_page() -> None:
    with app_harness({**AUTO_MINUTES, "meeting.auto_generate": False}, real_history=True) as h:
        app = h["app"]
        meeting_id = _start(app, h)
        app._on_segment_ready(_audio())
        app._on_final_result(LONG_TEXT)
        _end_and_wait(app, h, meeting_id)
        assert "可到「会议」页生成纪要" in h["tray"].showMessage.call_args[0][1]


def test_failed_automatic_minutes_tell_the_user_where_to_retry() -> None:
    with app_harness(AUTO_MINUTES, real_history=True) as h:
        app = h["app"]
        with patch.object(app._meeting._generator, "generate", lambda _request: None):
            meeting_id = _start(app, h)
            app._on_segment_ready(_audio())
            app._on_final_result(LONG_TEXT)
            _end_and_wait(app, h, meeting_id)
            app._meeting._generator.last_error_transient = False
            app._meeting._on_generation_error("HTTP 401：密钥无效")
        text = h["tray"].showMessage.call_args[0][1]
        assert "自动生成" in text and "密钥无效" in text and "会议" in text


def test_start_resumes_minutes_queued_before_the_last_quit() -> None:
    from sayink.meeting_store import QUEUE_PRIORITY_AUTO

    with app_harness({**AUTO_MINUTES, "meeting.auto_generate": False}, real_history=True) as h:
        app = h["app"]
        meeting_id = _start(app, h)
        app._on_segment_ready(_audio())
        app._on_final_result(LONG_TEXT)
        _end_and_wait(app, h, meeting_id)
        # Left in the queue by the previous run.
        h["config"].set("meeting.auto_generate", True)
        app.meeting.store.enqueue_generation(meeting_id, "general", priority=QUEUE_PRIORITY_AUTO)
        app.meeting.store.flush()
        requests = []
        with patch.object(app._meeting._generator, "generate", requests.append):
            app.start()
        assert len(requests) == 1
        assert app.meeting.generating_for == meeting_id


def _tone(freq: float, seconds: float = 1.2) -> np.ndarray:
    t = np.arange(int(TARGET_SAMPLE_RATE * seconds), dtype=np.float32) / TARGET_SAMPLE_RATE
    return (0.25 * np.sin(2.0 * np.pi * freq * t)).astype(np.float32)


def _blob_cells(db_path) -> list[str]:
    con = sqlite3.connect(db_path)
    try:
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        found = []
        for table in tables:
            for column in [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]:
                count = con.execute(
                    f'SELECT COUNT(*) FROM "{table}" WHERE typeof("{column}") = \'blob\''
                ).fetchone()[0]
                if count:
                    found.append(f"{table}.{column}")
        return found
    finally:
        con.close()


def test_meeting_keeps_text_only_no_audio_or_voiceprint_on_disk(tmp_path, monkeypatch) -> None:
    """P0: a meeting persists its transcript and speaker labels, never the
    audio or the voice embeddings used to tell speakers apart."""
    with app_harness({"audio.trigger_mode": "hotkey"}, real_history=True) as h:
        app = h["app"]
        data_dir = app._config.config_dir
        scratch = tmp_path / "tempfiles"
        scratch.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(scratch))

        meeting_id = _start(app, h, input_source="mixed", title="周会")
        ended = []
        app.meeting.meeting_ended.connect(ended.append)
        for freq, text in ((140, "先看预算"), (520, "下周上线")):
            app._on_segment_ready(_tone(freq))
            app._on_final_result(text)
        with patch("sayink.app.aux_model_path", return_value=None):
            app._end_meeting()
            h["recorder"].is_continuous = False
            deadline = time.monotonic() + 2.0
            while not ended and time.monotonic() < deadline:
                QCoreApplication.processEvents()
                time.sleep(0.01)
        assert ended == [meeting_id]
        assert app._history.flush()

        db_path = data_dir / "history.db"
        con = sqlite3.connect(db_path)
        try:
            texts = [r[0] for r in con.execute(
                "SELECT raw_text FROM history WHERE session_id = ? ORDER BY seq", (meeting_id,))]
            speakers = {r[0] for r in con.execute(
                "SELECT speaker_id FROM history WHERE session_id = ?", (meeting_id,))}
        finally:
            con.close()
        assert texts == ["先看预算", "下周上线"]
        assert len(speakers) == 2, "two voices should be told apart, or no embedding was computed"

        assert _blob_cells(db_path) == []
        assert sorted(p.name for p in data_dir.rglob("*") if p.is_file()) == sorted(
            p.name for p in data_dir.glob("history.db*"))
        assert list(scratch.iterdir()) == []

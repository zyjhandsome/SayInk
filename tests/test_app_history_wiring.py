from __future__ import annotations

from unittest.mock import patch

import numpy as np

from tests.helpers.app_harness import app_harness
from sayink.audio_utils import TARGET_SAMPLE_RATE


def _audio(seconds: float = 0.2) -> np.ndarray:
    return np.ones(int(TARGET_SAMPLE_RATE * seconds), dtype=np.float32)


def _start_continuous_user_session(app) -> None:
    with patch.object(app, "_start_continuous_listening") as start:
        app._on_continuous_hotkey_start()
        start.assert_called_once()


def _begin_and_finish_asr(app, paster, text: str, audio: np.ndarray | None = None):
    app._begin_transcription(audio if audio is not None else _audio())
    app._on_final_result(text)
    return paster.paste_async.call_args[0][1]


def _drive_segment(app, paster, text: str, result: str = "pasted") -> None:
    callback = _begin_and_finish_asr(app, paster, text)
    callback(result)


def _enqueued_records(history):
    return [call.args[0] for call in history.enqueue.call_args_list]


def test_start_enqueues_history_cleanup_once_with_active_session() -> None:
    with app_harness(
        {
            "history.retention_days": 7,
            "history.max_entries": 123,
        }
    ) as h:
        app = h["app"]
        app._current_session_id = "active-session"

        app.start()

        h["history"].enqueue_cleanup.assert_called_once_with(
            retention_days=7,
            max_entries=123,
            active_session_id="active-session",
        )


def test_start_shows_history_startup_notice_in_tray() -> None:
    """F-21: a rebuilt or unopenable history.db is announced once at startup."""
    with app_harness() as h:
        h["history"].startup_notice = "历史记录数据库已损坏，原文件已备份为 history.corrupt-x.db"
        h["app"].start()
        assert h["tray"].showMessage.called
        assert "已损坏" in h["tray"].showMessage.call_args[0][1]


def test_start_stays_quiet_when_history_is_healthy() -> None:
    with app_harness() as h:
        h["history"].startup_notice = ""
        h["app"].start()
        h["tray"].showMessage.assert_not_called()


def test_history_write_failure_warns_once_per_run() -> None:
    """F-21: the writer-thread failure callback reaches the tray exactly once."""
    with app_harness() as h:
        app = h["app"]
        callback = h["history"].add_failed_callback.call_args[0][0]
        callback("历史记录保存失败")
        callback("历史记录保存失败")
        from PyQt6.QtWidgets import QApplication

        QApplication.processEvents()
        assert h["tray"].showMessage.call_count == 1
        assert "保存失败" in h["tray"].showMessage.call_args[0][1]
        assert app._history_failure_notified is True


def test_quit_closes_history_before_saving_config() -> None:
    with app_harness() as h:
        events: list[str] = []
        h["history"].close.side_effect = lambda **_: events.append("history.close")
        h["config"].save_immediate.side_effect = lambda: events.append("config.save")

        h["app"]._quit()

        assert events[:2] == ["history.close", "config.save"]


def test_continuous_three_segments_share_session_and_increment_seq_with_texts() -> None:
    overrides = {
        "audio.trigger_mode": "continuous",
        "audio.input_source": "mixed",
        "llm.enabled": True,
        "llm.api_url": "https://api.example.test/v1",
        "llm.api_key": "key",
        "llm.model_name": "gpt-test",
        "stt.model_id": "fire-red-asr2-ctc-zh_en-int8",
    }
    with app_harness(overrides) as h:
        app = h["app"]
        app._current_session_id = "stale"
        app._current_seq = 42
        _start_continuous_user_session(app)

        for raw, polished in (
            ("raw one", "polished one"),
            ("raw two", "polished two"),
            ("raw three", "raw three"),
        ):
            app._begin_transcription(_audio())
            app._on_final_result(raw)
            if raw == "raw three":
                app._on_polish_error("network down")
            else:
                app._on_polish_complete(polished)
            h["paster"].paste_async.call_args[0][1]("pasted")

        records = _enqueued_records(h["history"])
        session_ids = {record.session_id for record in records}
        assert len(records) == 3
        assert len(session_ids) == 1
        assert "stale" not in session_ids
        assert [record.seq for record in records] == [0, 1, 2]
        assert [(r.raw_text, r.polished_text) for r in records] == [
            ("raw one", "polished one"),
            ("raw two", "polished two"),
            ("raw three", "raw three"),
        ]
        assert all(record.source == "mixed" for record in records)
        assert all(record.trigger_mode == "continuous" for record in records)
        assert all(record.model == "fire-red-asr2-ctc-zh_en-int8" for record in records)


def test_asr_error_auto_restart_keeps_continuous_session_id() -> None:
    with app_harness({"audio.trigger_mode": "continuous"}) as h:
        app = h["app"]
        _drive_segment(app, h["paster"], "before error")
        first_record = _enqueued_records(h["history"])[0]

        h["recorder"].is_continuous = False
        app._continuous_user_stopped = False
        app._on_recognizer_error("ASR crashed")
        _drive_segment(app, h["paster"], "after restart")

        records = _enqueued_records(h["history"])
        assert [record.seq for record in records] == [0, 1]
        assert records[1].session_id == first_record.session_id


def test_pending_paste_callback_freezes_segment_values_across_race() -> None:
    with app_harness({"audio.trigger_mode": "continuous"}) as h:
        app = h["app"]

        first_callback = _begin_and_finish_asr(app, h["paster"], "segment n")
        second_callback = _begin_and_finish_asr(app, h["paster"], "segment n plus one")

        first_callback("pasted")
        second_callback("pasted")

        records = _enqueued_records(h["history"])
        assert [record.raw_text for record in records] == ["segment n", "segment n plus one"]
        assert [record.seq for record in records] == [0, 1]


def test_history_disabled_skips_enqueue_and_mid_run_disable_affects_next_segment() -> None:
    with app_harness({"history.enabled": False}) as h:
        _drive_segment(h["app"], h["paster"], "disabled from start")
        h["history"].enqueue.assert_not_called()

    with app_harness() as h:
        _drive_segment(h["app"], h["paster"], "enabled segment")
        assert h["history"].enqueue.call_count == 1

        h["config"].set("history.enabled", False)
        _drive_segment(h["app"], h["paster"], "disabled segment")

        assert h["history"].enqueue.call_count == 1


def test_created_at_is_segment_finalization_time_not_begin() -> None:
    with app_harness() as h:
        app = h["app"]
        app._begin_transcription(_audio())
        with patch("sayink.app.time.time", return_value=1_005.5):
            app._on_final_result("finalized later")
            h["paster"].paste_async.call_args[0][1]("pasted")

        record = _enqueued_records(h["history"])[0]
        assert record.created_at == 1_005_500


def test_queued_segments_after_user_stop_keep_same_session() -> None:
    """Late/queued segments after Esc must keep the same session_id (ADR-0009/0001)."""
    with app_harness({"audio.trigger_mode": "continuous"}) as h:
        app = h["app"]
        _start_continuous_user_session(app)
        h["recorder"].is_continuous = True

        first_cb = _begin_and_finish_asr(app, h["paster"], "first")
        queued = _audio()
        app._segment_queue.append(queued)
        session_before_stop = app._current_session_id

        app._stop_continuous_user_session()
        first_cb("pasted")

        app._begin_transcription(app._segment_queue.pop(0))
        app._on_final_result("late after esc")
        h["paster"].paste_async.call_args[0][1]("pasted")

        records = _enqueued_records(h["history"])
        assert [r.raw_text for r in records] == ["first", "late after esc"]
        assert records[0].session_id == session_before_stop
        assert records[1].session_id == session_before_stop
        assert [r.seq for r in records] == [0, 1]


def test_restart_keeps_queued_old_session_audio_in_old_session() -> None:
    with app_harness({"audio.trigger_mode": "continuous", "audio.input_source": "mixed"}) as h:
        app, paster, recorder = h["app"], h["paster"], h["recorder"]
        recorder.consume_segment_route.return_value = ""
        _start_continuous_user_session(app)
        recorder.is_continuous = True
        old_speakers = app._speakers

        app._on_segment_ready(_audio())
        app._on_segment_ready(_audio())
        app._on_final_result("旧场第一句")
        first_callback = paster.paste_async.call_args[0][1]

        app._stop_continuous_user_session()
        recorder.is_continuous = False
        recorder.input_source = "microphone"
        _start_continuous_user_session(app)
        recorder.is_continuous = True
        app._on_segment_ready(_audio())

        assert app._segment_contexts[0].speakers is old_speakers
        assert app._speakers is not old_speakers

        first_callback("pasted")
        for text in ("旧场第二句", "新场第一句"):
            app._pump_segment_queue()
            app._on_final_result(text)
            paster.paste_async.call_args[0][1]("pasted")

        records = _enqueued_records(h["history"])
        assert [r.raw_text for r in records] == ["旧场第一句", "旧场第二句", "新场第一句"]
        old_session, new_session = records[0].session_id, records[2].session_id
        assert old_session != new_session
        assert [(r.session_id, r.seq) for r in records] == [
            (old_session, 0),
            (old_session, 1),
            (new_session, 0),
        ]
        assert [r.source for r in records] == ["mixed", "mixed", "mic"]


def _backlog_session(h):
    app, recorder = h["app"], h["recorder"]
    recorder.consume_segment_route.return_value = ""
    _start_continuous_user_session(app)
    recorder.is_continuous = True
    recorder.stop_continuous.side_effect = lambda: setattr(recorder, "is_continuous", False)
    app._output_busy = True
    return app


def test_backlog_at_limit_keeps_listening() -> None:
    from sayink.app import MAX_BACKLOG_AUDIO_SECONDS

    with app_harness({"audio.trigger_mode": "continuous"}) as h:
        app = _backlog_session(h)
        for _ in range(MAX_BACKLOG_AUDIO_SECONDS // 5):
            app._on_segment_ready(_audio(5.0))

        h["recorder"].stop_continuous.assert_not_called()
        assert app._continuous_user_stopped is False


def test_backlog_over_limit_pauses_listening_without_dropping_audio() -> None:
    from sayink.app import MAX_BACKLOG_AUDIO_SECONDS

    with app_harness({"audio.trigger_mode": "continuous"}) as h:
        app = _backlog_session(h)
        count = MAX_BACKLOG_AUDIO_SECONDS // 5 + 1
        for _ in range(count):
            app._on_segment_ready(_audio(5.0))

        h["recorder"].stop_continuous.assert_called_once()
        assert app._continuous_user_stopped is True
        assert len(app._segment_queue) == count
        assert "跟不上" in h["tray"].showMessage.call_args[0][1]
        assert "暂停监听" in h["floating"].show_continuous_stopped.call_args[0][0]


def test_history_commit_refreshes_open_main_window() -> None:
    from PyQt6.QtWidgets import QApplication
    from unittest.mock import MagicMock

    with app_harness() as h:
        app = h["app"]
        history_ui = MagicMock()
        app._main = MagicMock()
        app._main._history = history_ui

        callback = h["history"].add_committed_callback.call_args[0][0]
        callback()
        QApplication.processEvents()

        history_ui.refresh.assert_called()


def test_hold_records_one_history_row_for_the_whole_utterance() -> None:
    with app_harness({"audio.trigger_mode": "hotkey", "llm.enabled": False}) as h:
        app = h["app"]
        h["recorder"].is_recording = True
        app._begin_transcription(_audio(0.2))
        app._on_final_result("前一段")
        app._begin_transcription(_audio(0.3))
        app._on_final_result("后一段")
        h["paster"].paste_async.assert_not_called()
        h["history"].enqueue.assert_not_called()

        h["recorder"].is_recording = False
        app._is_transcribing = False
        app._on_recording_finished(np.zeros(1, dtype=np.float32))
        h["paster"].paste_async.call_args[0][1]("pasted")

        records = _enqueued_records(h["history"])
        assert h["paster"].paste_async.call_args[0][0] == "前一段后一段"
        assert len(records) == 1
        assert records[0].raw_text == "前一段后一段"
        assert records[0].polished_text == ""
        assert records[0].trigger_mode == "hotkey"
        assert records[0].seq == 0
        assert records[0].duration_ms == 500


def test_hold_tail_and_polish_save_the_full_utterance() -> None:
    overrides = {
        "audio.trigger_mode": "hotkey",
        "llm.enabled": True,
        "llm.api_url": "https://api.example.test/v1",
        "llm.api_key": "key",
        "llm.model_name": "gpt-test",
    }
    with app_harness(overrides) as h:
        app = h["app"]
        h["recorder"].is_recording = True
        app._begin_transcription(_audio(0.2))
        app._on_final_result("前一段")
        h["polisher"].polish.assert_not_called()

        h["recorder"].is_recording = False
        app._begin_transcription(_audio(0.2))
        app._on_final_result("尾巴")
        h["polisher"].polish.assert_called_once()
        assert h["polisher"].polish.call_args[0][0] == "前一段尾巴"

        app._on_polish_complete("润色后的全文")
        h["paster"].paste_async.call_args[0][1]("pasted")

        records = _enqueued_records(h["history"])
        assert h["paster"].paste_async.call_args[0][0] == "润色后的全文"
        assert len(records) == 1
        assert records[0].raw_text == "前一段尾巴"
        assert records[0].polished_text == "润色后的全文"
        assert records[0].duration_ms == 400
        assert records[0].trigger_mode == "hotkey"


def test_hold_next_recording_during_polish_keeps_each_utterance_raw_text() -> None:
    overrides = {
        "audio.trigger_mode": "hotkey",
        "llm.enabled": True,
        "llm.api_url": "https://api.example.test/v1",
        "llm.api_key": "key",
        "llm.model_name": "gpt-test",
    }
    with app_harness(overrides) as h:
        app, paster, polisher = h["app"], h["paster"], h["polisher"]
        h["recorder"].is_recording = True
        app._begin_transcription(_audio(0.2))
        app._on_final_result("A句原文")
        h["recorder"].is_recording = False
        app._on_recording_finished(np.zeros(1, dtype=np.float32))
        assert polisher.polish.call_args[0][0] == "A句原文"

        app._on_recording_start()
        h["recorder"].start.assert_called_once_with(continuous=False)
        h["recorder"].is_recording = True
        app._on_segment_ready(_audio(0.3))
        assert len(app._segment_queue) == 1

        app._on_polish_error("network down")
        assert [c.args[0] for c in paster.paste_async.call_args_list] == ["A句原文"]
        paster.paste_async.call_args[0][1]("pasted")

        app._pump_segment_queue()
        app._on_final_result("B句原文")
        h["recorder"].is_recording = False
        app._on_recording_finished(np.zeros(1, dtype=np.float32))
        assert polisher.polish.call_args[0][0] == "B句原文"
        app._on_polish_complete("B句润色")
        paster.paste_async.call_args[0][1]("pasted")

        assert [c.args[0] for c in paster.paste_async.call_args_list] == ["A句原文", "B句润色"]
        records = _enqueued_records(h["history"])
        assert [(r.raw_text, r.polished_text) for r in records] == [
            ("A句原文", "A句原文"),
            ("B句原文", "B句润色"),
        ]


def test_hold_error_after_release_still_pastes_and_records_earlier_text() -> None:
    with app_harness({"audio.trigger_mode": "hotkey", "llm.enabled": False}) as h:
        app = h["app"]
        h["recorder"].is_recording = True
        app._begin_transcription(_audio(0.2))
        app._on_final_result("已经说了")

        h["recorder"].is_recording = False
        app._begin_transcription(_audio(0.2))
        app._on_recognizer_error("ASR crashed")
        h["paster"].paste_async.call_args[0][1]("pasted")

        records = _enqueued_records(h["history"])
        assert h["paster"].paste_async.call_args[0][0] == "已经说了"
        assert [record.raw_text for record in records] == ["已经说了"]
        assert records[0].duration_ms == 400
        h["floating"].show_error.assert_not_called()


def test_hold_cancel_does_not_paste_or_record() -> None:
    with app_harness({"audio.trigger_mode": "hotkey", "llm.enabled": False}) as h:
        app = h["app"]
        h["recorder"].is_recording = True
        app._begin_transcription(_audio(0.2))
        app._on_final_result("不要了")
        app._on_recording_cancel()
        app._on_final_result("迟到的结果")

        h["paster"].paste_async.assert_not_called()
        h["history"].enqueue.assert_not_called()


_POLISH_ON = {
    "audio.trigger_mode": "continuous",
    "llm.enabled": True,
    "llm.api_url": "https://api.example.test/v1",
    "llm.api_key": "key",
    "llm.model_name": "gpt-test",
}


def test_continuous_segment_waits_while_previous_segment_is_polishing() -> None:
    with app_harness(_POLISH_ON) as h:
        app = h["app"]
        _start_continuous_user_session(app)
        h["recorder"].is_continuous = True
        h["recorder"].consume_segment_route.return_value = ""

        app._on_segment_ready(_audio())
        app._on_final_result("AAA")
        app._on_segment_ready(_audio())

        assert h["recognizer"].transcribe_final.call_count == 1
        assert len(app._segment_queue) == 1
        assert app._pending_segment_count() == 2
        h["polisher"].polish.assert_called_once()

        app._on_polish_complete("polished AAA")
        h["paster"].paste_async.call_args[0][1]("pasted")
        app._pump_segment_queue()
        assert h["recognizer"].transcribe_final.call_count == 2
        app._on_final_result("BBB")
        app._on_polish_complete("polished BBB")
        h["paster"].paste_async.call_args[0][1]("pasted")

        pasted = [call.args[0] for call in h["paster"].paste_async.call_args_list]
        assert pasted == ["polished AAA", "polished BBB"]
        records = _enqueued_records(h["history"])
        assert [(r.seq, r.raw_text, r.polished_text) for r in records] == [
            (0, "AAA", "polished AAA"),
            (1, "BBB", "polished BBB"),
        ]


def test_stuck_output_is_released_by_watchdog() -> None:
    with app_harness(_POLISH_ON) as h:
        app = h["app"]
        app._begin_transcription(_audio())
        app._on_final_result("AAA")
        app._enqueue_audio(_audio())
        assert app._output_busy is True

        app._release_stuck_output(app._output_token)

        assert app._output_busy is False
        assert h["recognizer"].transcribe_final.call_count == 2


def test_stale_watchdog_does_not_release_a_newer_output() -> None:
    with app_harness(_POLISH_ON) as h:
        app = h["app"]
        app._mark_output_busy()
        stale = app._output_token
        app._mark_output_busy()

        app._release_stuck_output(stale)

        assert app._output_busy is True


def test_polish_reply_far_longer_than_speech_falls_back_to_raw() -> None:
    with app_harness(_POLISH_ON) as h:
        app = h["app"]
        app._begin_transcription(_audio())
        app._on_final_result("今天开会")
        app._on_polish_complete("好的！以下是关于开会的建议：" + "内容" * 60)
        h["paster"].paste_async.call_args[0][1]("pasted")

        assert h["paster"].paste_async.call_args[0][0] == "今天开会"
        record = _enqueued_records(h["history"])[0]
        assert record.polished_text == "今天开会"


def test_local_polish_service_without_key_is_used() -> None:
    overrides = {
        "llm.enabled": True,
        "llm.api_url": "http://localhost:11434/v1",
        "llm.api_key": "",
        "llm.model_name": "local-model",
    }
    with app_harness(overrides) as h:
        h["app"]._deliver_recognized_text("本地润色")
        h["polisher"].polish.assert_called_once()
        assert h["polisher"].polish.call_args[0][2] == ""
        h["paster"].paste_async.assert_not_called()


def test_remote_polish_service_without_key_outputs_raw_text() -> None:
    overrides = {
        "llm.enabled": True,
        "llm.api_url": "https://api.example.test/v1",
        "llm.api_key": "",
        "llm.model_name": "gpt-test",
    }
    with app_harness(overrides) as h:
        h["app"]._deliver_recognized_text("远程未配置")
        h["polisher"].polish.assert_not_called()
        assert h["paster"].paste_async.call_args[0][0] == "远程未配置"


def test_clipboard_and_error_paste_results_enqueue_history() -> None:
    with app_harness() as h:
        _drive_segment(h["app"], h["paster"], "copied text", result="clipboard")
        _drive_segment(h["app"], h["paster"], "failed text", result="error:target locked")

        records = _enqueued_records(h["history"])
        assert [record.raw_text for record in records] == ["copied text", "failed text"]


def test_esc_does_not_end_continuous_session_when_disabled() -> None:
    with app_harness({"audio.trigger_mode": "continuous", "audio.esc_stops_continuous": False}) as h:
        app = h["app"]
        h["recorder"].is_continuous = True
        app._on_esc_pressed()
        h["recorder"].stop_continuous.assert_not_called()


def test_esc_ends_continuous_session_by_default() -> None:
    with app_harness({"audio.trigger_mode": "continuous"}) as h:
        app = h["app"]
        h["recorder"].is_continuous = True
        app._on_esc_pressed()
        h["recorder"].stop_continuous.assert_called_once()


def test_model_load_hint_uses_last_measured_duration() -> None:
    with app_harness() as h:
        app = h["app"]
        h["recognizer"].current_model_id = "sensevoice"
        h["recorder"].is_continuous = False

        app._on_model_load_progress("正在加载 SenseVoice…")
        assert "约 10–40 秒" in h["floating"].show_model_loading.call_args[0][0]
        app._load_started_at -= 12.0
        app._on_model_load_progress("模型已就绪")
        assert round(h["store"]["stt_load_seconds"]["sensevoice"]) == 12

        app._on_model_load_progress("正在加载 SenseVoice…")
        assert "上次用时约 12 秒" in h["floating"].show_model_loading.call_args[0][0]

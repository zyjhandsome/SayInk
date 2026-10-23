import sys

import pytest
import numpy as np
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon
from unittest.mock import MagicMock

from sayink.app import App, MIN_AUDIO_SAMPLES, _QueuedSegment
from sayink.runtime_status import ModelLoadPhase, RuntimeState
from sayink.text_paster import PasteResult
from tests.helpers.app_harness import app_harness


def _queued(*audios: np.ndarray) -> list[_QueuedSegment]:
    return [_QueuedSegment(audio=a) for a in audios]


class TestAppConstants:
    def test_min_audio_samples(self):
        assert MIN_AUDIO_SAMPLES == 1600


class TestAppErrorHints:
    def test_error_hints_defined(self):
        assert isinstance(App.ERROR_HINTS, dict)

    @pytest.mark.parametrize(
        "keyword",
        [
            "麦克风",
            "模型未就绪",
            "模型未下载",
            "录音过短",
            "未识别",
            "输出失败",
        ],
    )
    def test_error_hints_cover_common_cases(self, keyword):
        assert keyword in App.ERROR_HINTS


class TestIslandUserCopy:
    def test_error_hints_point_to_engine_not_model_nav(self):
        for hint in App.ERROR_HINTS.values():
            assert "设置 → 模型" not in hint
        for key in ("模型未就绪", "模型未下载"):
            assert "打开 SayInk" in App.ERROR_HINTS[key]
            assert "→ 引擎" in App.ERROR_HINTS[key]
            assert "右键托盘图标 → 设置" not in App.ERROR_HINTS[key]

    def test_model_load_progress_copy_is_single_line(self):
        with app_harness() as h:
            h["recorder"].is_continuous = False
            h["app"]._on_model_load_progress(ModelLoadPhase.LOADING, "正在加载 Fun-ASR-Nano…")
            detail = h["floating"].show_model_loading.call_args[0][0]
            assert "\n" not in detail
            assert "正在加载 Fun-ASR-Nano…" in detail

    def test_loading_status_uses_zai_ru_vocab(self):
        with app_harness() as h:
            h["recognizer"].is_loading = True
            h["recognizer"].is_ready = False
            assert h["app"]._runtime_status_label() == "模型载入中…"
            assert h["app"]._model_not_ready_message() == "模型载入中，请稍候"
            audio = np.zeros(MIN_AUDIO_SAMPLES + 8, dtype=np.float32)
            h["app"]._on_segment_ready(audio)
            detail = h["floating"].show_model_loading.call_args[0][0]
            assert "模型载入中" in detail
            assert "模型加载中" not in detail

    def test_not_ready_tray_points_to_engine(self):
        with app_harness() as h:
            h["recognizer"].is_ready = False
            h["recognizer"].is_loading = False
            h["app"]._show_model_not_ready()
            msg = h["tray"].showMessage.call_args[0][1]
            assert "打开 SayInk" in msg and "→ 引擎" in msg
            assert "右键托盘图标 → 设置" not in msg
            assert "设置 → 模型" not in msg

    def test_model_load_failure_publishes_unavailable_state(self):
        with app_harness() as h:
            settings = type(
                "SettingsSpy",
                (),
                {"set_runtime_status": lambda self, state, label: setattr(self, "value", (state, label))},
            )()
            h["app"]._main = type("MainSpy", (), {"settings_panel": settings})()
            h["app"]._on_model_load_progress(ModelLoadPhase.FAILED, "模型加载失败: boom")
            assert settings.value == (RuntimeState.UNAVAILABLE, "模型载入失败")
            h["tray"].set_status_summary.assert_any_call("模型载入失败")


class TestAppFriendlyError:
    def test_friendly_error_with_keyword(self):
        result = App._friendly_error("无法访问麦克风")
        assert "麦克风" in result or "检查" in result

    def test_friendly_error_without_keyword(self):
        result = App._friendly_error("未知错误")
        assert result == "未知错误"

    def test_friendly_error_returns_hint(self):
        result = App._friendly_error("模型加载失败，模型未就绪")
        assert "模型未就绪" in result


class TestAppInit:
    def test_init_creates_modules(self):
        with app_harness() as h:
            app = h["app"]
            assert hasattr(app, "_config")
            assert hasattr(app, "_hotkey_mgr")
            assert hasattr(app, "_recorder")
            assert hasattr(app, "_recognizer")
            assert hasattr(app, "_polisher")
            assert hasattr(app, "_paster")
            assert hasattr(app, "_sound")

    def test_init_creates_ui(self):
        with app_harness() as h:
            app = h["app"]
            assert hasattr(app, "_floating")
            assert hasattr(app, "_tray")
            assert app._main is None


class TestAppSignals:
    def test_signals_connected(self):
        with app_harness() as h:
            assert hasattr(h["app"], "_connect_signals")

    def test_island_signals_connected(self):
        with app_harness() as h:
            app = h["app"]
            h["tray"].wake_island.connect.assert_called_with(app._show_main_window)
            h["floating"].settings_requested.connect.assert_called_with(
                app._show_settings
            )
            h["floating"].history_requested.connect.assert_called_with(
                app._show_history_window
            )


class TestTrayMenuDoesNotLeaveMainWindowStuck:
    def test_closing_the_tray_menu_reactivates_the_main_window(self, monkeypatch):
        QApplication.instance() or QApplication(sys.argv)
        app = App.__new__(App)
        main = MagicMock()
        main.isVisible.return_value = True
        app._main = main
        app._restore_main_after_menu = False
        monkeypatch.setattr(QApplication, "activeWindow", staticmethod(lambda: main))
        monkeypatch.setattr(
            "sayink.app.QTimer.singleShot",
            lambda _delay, callback: callback(),
        )

        app._note_main_before_tray_menu()
        app._restore_main_after_tray_menu()

        main.raise_.assert_called_once()
        main.activateWindow.assert_called_once()

    def test_menu_close_does_not_steal_focus_from_another_window(self):
        QApplication.instance() or QApplication(sys.argv)
        app = App.__new__(App)
        main = MagicMock()
        main.isVisible.return_value = True
        app._main = main
        app._restore_main_after_menu = False

        app._note_main_before_tray_menu()
        app._restore_main_after_tray_menu()

        main.raise_.assert_not_called()
        main.activateWindow.assert_not_called()


class TestAppState:
    def test_initial_transcription_empty(self):
        with app_harness() as h:
            app = h["app"]
            assert app._current_transcription == ""
            assert app._is_transcribing is False


class TestAppMethods:
    def test_has_show_settings(self):
        with app_harness() as h:
            app = h["app"]
            assert hasattr(app, "_show_settings")
            assert callable(app._show_settings)

    def test_has_quit(self):
        with app_harness() as h:
            app = h["app"]
            assert hasattr(app, "_quit")
            assert callable(app._quit)

    def test_has_start(self):
        with app_harness() as h:
            app = h["app"]
            assert hasattr(app, "start")
            assert callable(app.start)


class TestCoreSignalIntegrity:
    """NFR-REL-03: the four core signals must be wired in _connect_signals."""

    def test_core_signals_connected(self):
        with app_harness() as h:
            h["hotkey"].esc_pressed.connect.assert_called()
            h["recognizer"].ready.connect.assert_called()
            h["recognizer"].model_load_progress.connect.assert_called()
            h["recorder"].segment_ready.connect.assert_called()

    def test_recognizer_final_and_error_connected(self):
        with app_harness() as h:
            h["recognizer"].final_result.connect.assert_called()
            h["recognizer"].error.connect.assert_called()


class TestHandlePasteResult:
    def test_pasted_non_continuous_shows_success(self):
        with app_harness() as h:
            app = h["app"]
            app._handle_paste_result("pasted")
            h["floating"].show_success.assert_any_call(
                "已发送", "请确认目标应用已接收"
            )

    def test_paste_feedback_names_captured_target(self):
        with app_harness() as h:
            h["app"]._handle_paste_result(
                PasteResult("sent", target_app="notepad.exe")
            )
            h["floating"].show_success.assert_any_call(
                "已发送", "发送到 notepad.exe"
            )

    def test_clipboard_shows_copied_hint(self):
        with app_harness() as h:
            app = h["app"]
            app._handle_paste_result("clipboard")
            assert h["floating"].show_success.called
            args = h["floating"].show_success.call_args[0]
            assert "已复制" in args[0]

    def test_unverified_send_is_not_called_copied(self):
        """README: after Ctrl+V went out, a focus change shows 「已发送 · 未确认」."""
        with app_harness() as h:
            app = h["app"]
            app._handle_paste_result(
                PasteResult("unverified", target_app="notepad.exe", detail="focus_changed_after_send")
            )
            assert not h["floating"].show_success.called
            title, hint = h["floating"].show_info.call_args[0]
            assert title == "已发送 · 未确认"
            assert "notepad.exe" in hint and "Ctrl+V" in hint

    def test_error_shows_friendly_error(self):
        with app_harness() as h:
            app = h["app"]
            app._handle_paste_result("error:目标不可写")
            assert h["floating"].show_error.called

    def test_degraded_from_polish_shows_info(self):
        with app_harness() as h:
            app = h["app"]
            app._handle_paste_result("pasted", degraded_from_polish=True)
            h["floating"].show_info.assert_any_call(
                "已发送（原文）", "请确认目标应用已接收"
            )


class TestHotkeyTapTooShortHint:
    def test_hint_uses_continuous_hold_duration_in_continuous_mode(self):
        # Continuous mode uses the longer anti-accidental threshold; no idle float.
        with app_harness({"audio.trigger_mode": "continuous"}) as h:
            app = h["app"]
            h["recorder"].is_continuous = False
            app._on_hotkey_tap_too_short()
            assert h["tray"].showMessage.called
            hint = h["tray"].showMessage.call_args[0][1]
            assert "0.30" in hint
            h["floating"].show_continuous_idle.assert_not_called()

    def test_hint_uses_hotkey_hold_duration_in_hotkey_mode(self):
        with app_harness({"audio.trigger_mode": "hotkey"}) as h:
            app = h["app"]
            app._on_hotkey_tap_too_short()
            assert h["tray"].showMessage.called
            hint = h["tray"].showMessage.call_args[0][1]
            assert "0.18" in hint


class TestSettingsChangedQueue:
    def test_pending_queue_cleared_on_settings_change(self):
        with app_harness() as h:
            app = h["app"]
            app._segment_queue = _queued(*[np.zeros(1600, dtype=np.float32)] * 3)
            h["recorder"].is_continuous = False
            app._on_settings_changed()
            assert app._segment_queue == []

    def test_sentence_cut_off_by_the_settings_change_is_kept(self):
        """Regression: the confirmation counted the queue, then stopping the
        session flushed one more sentence that was cleared without notice."""
        with app_harness({"audio.trigger_mode": "continuous"}) as h:
            app = h["app"]
            app._is_transcribing = True
            app._segment_queue = _queued(*[np.zeros(1600, dtype=np.float32)] * 2)
            tail = np.ones(1600, dtype=np.float32)
            h["recorder"].is_continuous = True

            def flush_tail():
                h["recorder"].is_continuous = False
                app._on_segment_ready(tail)

            h["recorder"].stop_continuous.side_effect = flush_tail
            app._on_settings_changed()
            assert [s.audio for s in app._segment_queue] == [tail]

    def test_pending_segment_count_counts_queue_and_active(self):
        with app_harness() as h:
            app = h["app"]
            app._segment_queue = _queued(*[np.zeros(1600, dtype=np.float32)] * 2)
            app._is_transcribing = True
            assert app._pending_segment_count() == 3


class TestSegmentReadyQueueing:
    def test_empty_segment_ignored(self):
        with app_harness() as h:
            app = h["app"]
            app._on_segment_ready(np.zeros(0, dtype=np.float32))
            assert app._segment_queue == []
            h["recognizer"].transcribe_final.assert_not_called()

    def test_segment_queued_when_transcribing(self):
        with app_harness() as h:
            app = h["app"]
            h["recognizer"].is_ready = True
            app._is_transcribing = True
            app._on_segment_ready(np.zeros(1600, dtype=np.float32))
            assert len(app._segment_queue) == 1

    def test_new_segment_waits_behind_backlog_after_paste(self):
        """Regression: in the 300 ms pause after a paste the pipeline is idle,
        and a fresh segment used to start before the ones already queued."""
        with app_harness() as h:
            app = h["app"]
            older = np.full(1600, 0.1, dtype=np.float32)
            newer = np.full(1600, 0.2, dtype=np.float32)
            app._enqueue_audio(older)
            app._on_segment_ready(newer)
            h["recognizer"].transcribe_final.assert_not_called()
            assert [s.audio for s in app._segment_queue] == [older, newer]
            app._pump_segment_queue()
            assert app._segment_queue[0].audio is newer

    def test_segment_queued_while_model_loading(self):
        with app_harness() as h:
            app = h["app"]
            h["recognizer"].is_ready = False
            h["recognizer"].is_loading = True
            app._on_segment_ready(np.zeros(1600, dtype=np.float32))
            assert len(app._segment_queue) == 1

    def test_segment_kept_when_model_not_ready(self):
        with app_harness() as h:
            app = h["app"]
            h["recognizer"].is_ready = False
            h["recognizer"].is_loading = False
            audio = np.ones(1600, dtype=np.float32)
            app._on_segment_ready(audio)
            assert len(app._segment_queue) == 1
            assert app._segment_queue[0].audio is audio
            h["recognizer"].transcribe_final.assert_not_called()
            h["floating"].show_error.assert_called()

    def test_pump_keeps_queue_when_model_not_ready(self):
        with app_harness() as h:
            app = h["app"]
            h["recognizer"].is_ready = False
            h["recognizer"].is_loading = False
            audio = np.ones(1600, dtype=np.float32)
            app._segment_queue = _queued(audio)
            app._pump_segment_queue()
            assert app._queued_audio() == [audio]
            h["recognizer"].transcribe_final.assert_not_called()

    def test_recognizer_error_while_loading_keeps_hud_lock(self):
        with app_harness() as h:
            app = h["app"]
            h["recognizer"].is_loading = True
            app._is_transcribing = True
            app._on_recognizer_error("识别失败: boom")
            assert app._is_transcribing is False
            h["floating"].clear_model_loading_lock.assert_not_called()
            h["floating"].show_error.assert_not_called()

    def test_load_failure_does_not_drop_queue_and_reports_once(self):
        with app_harness() as h:
            app = h["app"]
            h["recognizer"].is_ready = False
            h["recognizer"].is_loading = False
            audio = np.ones(1600, dtype=np.float32)
            app._segment_queue = _queued(audio)
            app._is_transcribing = True
            app._on_model_load_progress(ModelLoadPhase.FAILED, "模型加载失败: boom")
            assert app._is_transcribing is False
            assert app._queued_audio() == [audio]
            h["recognizer"].transcribe_final.assert_not_called()
            h["floating"].show_error.assert_called_once()


class TestFinalResultFlow:
    def test_empty_result_non_continuous_shows_error(self):
        with app_harness(config_overrides={"audio.trigger_mode": "hotkey"}) as h:
            app = h["app"]
            h["recognizer"].is_ready = True
            h["recognizer"].is_loading = False
            app._is_transcribing = True
            app._on_final_result("   ")
            assert h["floating"].show_error.called
            assert app._is_transcribing is False

    def test_hold_pastes_the_whole_utterance_once_on_release(self):
        with app_harness({"audio.trigger_mode": "hotkey", "llm.enabled": False}) as h:
            app = h["app"]
            h["recorder"].is_recording = True
            app._on_final_result("前十五秒")
            app._on_final_result("后十五秒")
            h["paster"].paste_async.assert_not_called()

            h["recorder"].is_recording = False
            app._is_transcribing = False
            app._on_recording_finished(np.array([], dtype=np.float32))

            h["paster"].paste_async.assert_called_once()
            assert h["paster"].paste_async.call_args[0][0] == "前十五秒后十五秒"
            h["floating"].show_error.assert_not_called()

    def test_hold_release_tail_pastes_every_slice(self):
        with app_harness({"audio.trigger_mode": "hotkey", "llm.enabled": False}) as h:
            app = h["app"]
            h["recorder"].is_recording = True
            app._on_final_result("前十五秒")
            h["paster"].paste_async.assert_not_called()

            h["recorder"].is_recording = False
            app._on_final_result("松开后的尾巴")

            h["paster"].paste_async.assert_called_once()
            assert h["paster"].paste_async.call_args[0][0] == "前十五秒松开后的尾巴"

    def test_new_hold_is_refused_while_the_previous_utterance_is_still_queued(self):
        """Repro from the evaluation: hold → release → hold again at once.
        The queued previous utterance must not merge into the new hold."""
        with app_harness({"audio.trigger_mode": "hotkey", "llm.enabled": False}) as h:
            app = h["app"]
            h["recognizer"].is_ready = True
            h["recorder"].is_recording = False
            app._is_transcribing = False
            app._segment_queue = _queued(np.ones(1600, dtype=np.float32))

            app._on_recording_start()

            h["recorder"].start.assert_not_called()
            h["floating"].show_recording.assert_not_called()
            h["floating"].show_busy_transcribing.assert_called_once()
            assert len(app._segment_queue) == 1

    def test_new_hold_is_refused_while_the_previous_utterance_is_recognizing(self):
        with app_harness({"audio.trigger_mode": "hotkey", "llm.enabled": False}) as h:
            app = h["app"]
            h["recognizer"].is_ready = True
            app._is_transcribing = True

            app._on_recording_start()

            h["recorder"].start.assert_not_called()
            h["floating"].show_busy_transcribing.assert_called_once()

    def test_hold_starts_normally_when_the_pipeline_is_idle(self):
        with app_harness({"audio.trigger_mode": "hotkey", "llm.enabled": False}) as h:
            app = h["app"]
            h["recognizer"].is_ready = True
            app._is_transcribing = False
            app._segment_queue = []

            app._on_recording_start()

            h["recorder"].start.assert_called_once()
            h["floating"].show_recording.assert_called_once()
            h["floating"].show_busy_transcribing.assert_not_called()

    @staticmethod
    def _defer_paint(h) -> list:
        painted = []
        h["floating"].call_when_painted.side_effect = painted.append
        return painted

    def test_device_opens_only_after_the_bar_painted_recording(self):
        """Opening the device blocks ~0.5 s; before 「录音中」 is painted the bar
        would keep showing the previous 「已发送」."""
        with app_harness({"audio.trigger_mode": "hotkey", "llm.enabled": False}) as h:
            app = h["app"]
            painted = self._defer_paint(h)

            app._on_recording_start()

            h["floating"].show_recording.assert_called_once()
            h["recorder"].start.assert_not_called()
            painted.pop()()
            h["recorder"].start.assert_called_once_with(continuous=False)

    def test_release_before_the_device_opened_never_opens_it(self):
        with app_harness({"audio.trigger_mode": "hotkey", "llm.enabled": False}) as h:
            app = h["app"]
            painted = self._defer_paint(h)
            app._on_recording_start()

            app._on_recording_stop()
            painted.pop()()

            h["recorder"].start.assert_not_called()
            h["recorder"].stop.assert_not_called()
            assert "录音过短" in h["floating"].show_error.call_args[0][0]

    def test_long_hold_without_speech_says_nothing_was_heard(self):
        """Three seconds of silence is not a 'too short' recording: the gate
        never opened, and the hint must say so."""
        with app_harness({"audio.trigger_mode": "hotkey", "llm.enabled": False}) as h:
            app = h["app"]
            app._on_recording_start()
            app._hold_opened_at -= 3.0  # held for three seconds
            h["recorder"].is_recording = False

            app._on_recording_finished(np.zeros(0, dtype=np.float32))

            msg = h["floating"].show_error.call_args[0][0]
            assert "未识别到语音内容" in msg
            assert "录音过短" not in msg

    def test_esc_discards_the_slice_being_recognised(self):
        """Esc during a hold must not leave 「正在识别上一轮语音」 behind."""
        with app_harness({"audio.trigger_mode": "hotkey", "llm.enabled": False}) as h:
            app, recognizer = h["app"], h["recognizer"]
            recognizer.cancel_current = lambda: True
            app._on_recording_start()
            h["recorder"].is_recording = True
            app._begin_transcription(np.ones(16000, dtype=np.float32))
            assert app._is_transcribing

            app._on_recording_cancel()

            assert not app._is_transcribing
            assert app._pending_record is None
            h["recorder"].cancel.assert_called_once()
            h["recorder"].is_recording = False
            h["floating"].show_error.reset_mock()
            h["recorder"].start.reset_mock()
            app._on_recording_start()
            h["recorder"].start.assert_called_once()
            h["floating"].show_busy_transcribing.assert_not_called()

    def test_a_failed_slice_inside_a_hold_is_reported_with_the_result(self):
        """A 15 s slice that failed to recognise is left out of the paste; the
        result toast must say so instead of hiding the gap."""
        with app_harness({"audio.trigger_mode": "hotkey", "llm.enabled": False}) as h:
            app = h["app"]
            h["recognizer"].is_loading = False
            app._on_recording_start()
            h["recorder"].is_recording = True
            app._on_final_result("第一段")
            app._on_recognizer_error("识别失败: boom")
            h["floating"].show_error.assert_not_called()
            h["recorder"].is_recording = False
            app._on_final_result("第三段")

            h["paster"].paste_async.assert_called_once()
            assert h["paster"].paste_async.call_args[0][0] == "第一段第三段"
            app._handle_paste_result(PasteResult("sent", target_app="notepad.exe"))
            hint = h["floating"].show_success.call_args[0][1]
            assert "有 1 段未识别" in hint

            # The next hold starts clean.
            h["floating"].show_success.reset_mock()
            app._on_recording_start()
            h["recorder"].is_recording = False
            app._on_final_result("干净的一句")
            app._handle_paste_result(PasteResult("sent", target_app="notepad.exe"))
            assert "未识别" not in h["floating"].show_success.call_args[0][1]

    def test_paste_result_keeps_the_tray_on_recording_while_the_next_hold_records(self):
        with app_harness({"audio.trigger_mode": "hotkey", "llm.enabled": False}) as h:
            app = h["app"]
            h["recorder"].is_recording = True
            h["tray"].set_activity_tooltip.reset_mock()

            app._handle_paste_result(PasteResult("sent", target_app="notepad.exe"))

            assert h["tray"].set_activity_tooltip.call_args[0][0] == "recording"

    def test_esc_before_the_device_opened_cancels_the_hold(self):
        with app_harness({"audio.trigger_mode": "hotkey", "llm.enabled": False}) as h:
            app = h["app"]
            painted = self._defer_paint(h)
            app._on_recording_start()

            app._on_recording_cancel()
            painted.pop()()

            h["recorder"].start.assert_not_called()
            h["floating"].show_cancelled.assert_called_once()

    def test_esc_on_a_refused_hold_keeps_the_previous_utterance(self):
        """hold → release → hold again (refused) → Esc: the queued previous
        utterance must survive; Esc only cancels a hold that is recording."""
        with app_harness({"audio.trigger_mode": "hotkey", "llm.enabled": False}) as h:
            app = h["app"]
            h["recognizer"].is_ready = True
            h["recorder"].is_recording = False
            queued = np.ones(1600, dtype=np.float32)
            app._segment_queue = _queued(queued)
            app._live_committed = "上一句"
            app._hold_paste_sent = False

            app._on_recording_start()
            app._on_recording_cancel()

            assert app._queued_audio() == [queued]
            assert app._live_committed == "上一句"
            assert app._hold_paste_sent is False
            h["recorder"].cancel.assert_not_called()
            h["floating"].show_cancelled.assert_not_called()

    def test_esc_during_a_live_hold_still_cancels(self):
        with app_harness({"audio.trigger_mode": "hotkey", "llm.enabled": False}) as h:
            app = h["app"]
            h["recorder"].is_recording = True
            app._enqueue_audio(np.ones(1600, dtype=np.float32), "mic")

            app._on_recording_cancel()

            assert app._segment_queue == []
            h["recorder"].cancel.assert_called_once()
            h["floating"].show_cancelled.assert_called_once()

    def test_queue_entries_carry_route_and_context_together(self):
        """Q-02: one dataclass per segment, no parallel lists to keep in step."""
        with app_harness({"audio.trigger_mode": "hotkey", "llm.enabled": False}) as h:
            app = h["app"]
            first, second = np.ones(1600, dtype=np.float32), np.zeros(1600, dtype=np.float32)
            app._enqueue_audio(first, "mic")
            app._enqueue_audio(second, "system", front=True)

            assert [s.route for s in app._segment_queue] == ["system", "mic"]
            audio, route, context = app._pop_queued_audio()
            assert audio is second and route == "system" and context is None
            assert app._queued_audio() == [first]
            assert app._queued_audio_seconds() == pytest.approx(0.1)

    def test_release_tells_the_bar_the_key_is_up_before_stopping(self):
        with app_harness({"audio.trigger_mode": "hotkey", "llm.enabled": False}) as h:
            app = h["app"]
            h["recorder"].is_recording = True
            order: list[str] = []
            h["floating"].end_capture.side_effect = lambda: order.append("end_capture")
            h["recorder"].stop.side_effect = lambda: order.append("stop")

            app._on_recording_stop()

            assert order == ["end_capture", "stop"]

    def test_hold_waits_until_the_queued_tail_is_recognized(self):
        with app_harness({"audio.trigger_mode": "hotkey", "llm.enabled": False}) as h:
            app = h["app"]
            h["recorder"].is_recording = False
            app._segment_queue = _queued(np.ones(1600, dtype=np.float32))
            app._on_final_result("前十五秒")
            h["paster"].paste_async.assert_not_called()
            assert app._live_committed == "前十五秒"

    def test_continuous_still_pastes_each_segment(self):
        with app_harness({"audio.trigger_mode": "continuous", "llm.enabled": False}) as h:
            app = h["app"]
            app._on_final_result("第一段")
            app._on_final_result("第二段")
            pasted = [call.args[0] for call in h["paster"].paste_async.call_args_list]
            assert pasted == ["第一段", "第二段"]

    def test_result_outputs_directly_when_llm_disabled(self):
        with app_harness() as h:
            app = h["app"]
            h["recognizer"].is_ready = True
            app._on_final_result("你好世界")
            h["paster"].paste_async.assert_called()

    def test_result_triggers_polish_when_enabled(self):
        overrides = {
            "llm.enabled": True,
            "llm.api_url": "https://api.example.com/v1",
            "llm.api_key": "k",
            "llm.model_name": "m",
        }
        with app_harness(config_overrides=overrides) as h:
            app = h["app"]
            h["recognizer"].is_ready = True
            app._on_final_result("你好世界")
            h["polisher"].polish.assert_called()
            h["paster"].paste_async.assert_not_called()

    def test_incomplete_polish_config_outputs_original_with_notice(self):
        with app_harness({"llm.enabled": True}) as h:
            app = h["app"]
            h["recognizer"].is_ready = True
            app._on_final_result("你好世界")
            h["polisher"].polish.assert_not_called()
            callback = h["paster"].paste_async.call_args[0][1]
            callback("pasted")
            h["floating"].show_info.assert_any_call(
                "已发送（原文）", "请确认目标应用已接收"
            )


class TestBeginTranscription:
    def test_begin_transcription_calls_recognizer(self):
        with app_harness() as h:
            app = h["app"]
            h["recognizer"].is_ready = True
            audio = np.ones(1600, dtype=np.float32)
            app._begin_transcription(audio)
            assert app._is_transcribing is True
            h["recognizer"].transcribe_final.assert_called_once()

    def test_begin_transcription_requeues_when_loading(self):
        with app_harness() as h:
            app = h["app"]
            h["recognizer"].is_ready = False
            h["recognizer"].is_loading = True
            audio = np.ones(1600, dtype=np.float32)
            app._begin_transcription(audio)
            assert app._is_transcribing is False
            assert len(app._segment_queue) == 1

    def test_begin_transcription_requeues_when_not_ready(self):
        with app_harness() as h:
            app = h["app"]
            h["recognizer"].is_ready = False
            h["recognizer"].is_loading = False
            audio = np.ones(1600, dtype=np.float32)
            app._begin_transcription(audio)
            assert app._is_transcribing is False
            assert app._queued_audio() == [audio]
            h["recognizer"].transcribe_final.assert_not_called()
            h["floating"].show_error.assert_called()


def test_main_window_build_failure_is_reported_and_retried(monkeypatch):
    attempts = []

    def broken_main_window(*_args, **_kwargs):
        attempts.append(1)
        raise UnicodeEncodeError("locale", "会", 0, 1, "encoding error")

    monkeypatch.setattr("sayink.app.MainWindow", broken_main_window)
    app = App.__new__(App)
    app._config = object()
    app._history = object()
    app._main = None
    app._tray = MagicMock()

    app._show_main_window()
    app._show_main_window("history")

    assert app._main is None
    assert len(attempts) == 2
    assert app._tray.showMessage.call_count == 2
    _title, message, icon, _ms = app._tray.showMessage.call_args[0]
    assert "主窗口" in message and "sayink.log" in message
    assert icon == QSystemTrayIcon.MessageIcon.Warning


def test_hold_output_uses_the_window_from_recording_start(monkeypatch):
    with app_harness() as h:
        current = {"info": (111, "Notepad", 50)}
        monkeypatch.setattr(
            "sayink.text_paster.get_foreground_window_info", lambda: current["info"],
        )
        monkeypatch.setattr(
            "sayink.text_paster.get_foreground_process_name", lambda: "notepad.exe",
        )
        h["app"]._on_recording_start()
        current["info"] = (222, "Chat", 60)
        h["app"]._output_text("你好")
        kwargs = h["paster"].paste_async.call_args.kwargs
        assert kwargs.get("target_hwnd") == 111
        assert kwargs.get("target_app") == "notepad.exe"


def test_changed_target_notice_names_the_process_and_does_not_ask(monkeypatch):
    with app_harness() as h:
        dialogs = []
        monkeypatch.setattr(
            "PyQt6.QtWidgets.QMessageBox.exec",
            lambda *args, **kwargs: dialogs.append(args),
        )
        h["app"]._handle_paste_result(
            PasteResult("clipboard", target_app="notepad.exe", detail="focus_changed"),
        )
        notices = []
        for name in ("show_info", "show_success"):
            mock = getattr(h["floating"], name)
            if mock.called:
                notices.extend(" ".join(str(part) for part in call.args) for call in mock.call_args_list)
        text = " ".join(notices)
        assert "notepad.exe" in text
        assert dialogs == []


def test_listening_bar_shows_process_name_not_a_window_title():
    from sayink.ui.floating_window import FloatingWindow

    win = FloatingWindow()
    try:
        win.show_listening(target_app="notepad.exe")
        label = win._status_label.text()
        assert "notepad.exe" in label
        assert "记事本" not in label
        # Refreshing the listening state must not drop the utterance still open.
        win.show_listening()
        assert "notepad.exe" in win._status_label.text()
        win.show_listening("")
        assert "notepad.exe" not in win._status_label.text()
    finally:
        win.close()


def test_continuous_ready_notice_uses_the_real_pause():
    from sayink.vad_segmenter import SILENCE_HOLD_SEC

    with app_harness({"audio.trigger_mode": "continuous"}) as h:
        h["recognizer"].is_ready = True
        h["app"]._on_stt_ready()
        text = " ".join(
            str(part) for call in h["tray"].showMessage.call_args_list for part in call.args
        )
        assert f"{SILENCE_HOLD_SEC:g} 秒" in text


def test_new_continuous_session_clears_a_previous_paste_target(monkeypatch):
    with app_harness({"audio.trigger_mode": "continuous"}) as h:
        app = h["app"]
        h["recognizer"].is_ready = True
        h["recognizer"].is_loading = False

        def start_continuous():
            h["recorder"].is_continuous = True

        h["recorder"].start_continuous.side_effect = start_continuous
        h["recorder"].is_continuous = False
        monkeypatch.setattr("sayink.app.QTimer.singleShot", lambda _ms, fn: fn())
        app._start_continuous_listening()
        h["floating"].show_listening.assert_called_with("")

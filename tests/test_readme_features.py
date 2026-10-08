"""
Integration tests aligned with README「操作指南」与「声音收录」。

覆盖：按住快捷键录音、松开识别、Esc 取消、短按防误触、
持续转写模式、三种音频来源配置、ASR 标签清洗后输出。
"""

from __future__ import annotations

import sys

import numpy as np
import pytest
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication
from pynput import keyboard
from unittest.mock import patch

from tests.helpers.app_harness import app_harness
from voiceink.app import App, MIN_AUDIO_SAMPLES
from voiceink.hotkey_manager import HotKeyManager


class TestReadmeExitProtection:
    """Returning from exit allows the current session to finish normally."""

    def test_return_then_stop_and_finish_before_exit(self):
        with app_harness({"audio.trigger_mode": "continuous", "llm.enabled": False}) as h:
            app = h["app"]
            app._current_session_id = "exit-session"
            h["recorder"].is_recording = True
            h["recorder"].is_continuous = True
            audio = np.ones(MIN_AUDIO_SAMPLES * 2, dtype=np.float32)
            app._on_segment_ready(audio)
            app._on_segment_ready(audio)
            with patch("voiceink.app.QMessageBox") as boxes, patch("voiceink.app.QApplication.quit") as quit_app:
                dialog = boxes.return_value
                dialog.addButton.side_effect = ["return", "discard"]
                dialog.clickedButton.return_value = "return"
                app._quit()
                quit_app.assert_not_called()

            app._stop_continuous_user_session()
            h["recorder"].is_continuous = False
            h["recorder"].is_recording = False
            for text in ("第一句", "第二句"):
                app._on_final_result(text)
                h["paster"].paste_async.call_args.args[1]("sent")
                app._pump_segment_queue()
            records = [call.args[0] for call in h["history"].enqueue.call_args_list]
            assert [record.raw_text for record in records] == ["第一句", "第二句"]
            assert {record.session_id for record in records} == {"exit-session"}
            with patch("voiceink.app.QMessageBox") as boxes, patch("voiceink.app.QApplication.quit") as quit_app:
                app._quit()
                boxes.assert_not_called()
                quit_app.assert_called_once()


class TestReadmeHoldHotkeyFlow:
    """README: 按住 Ctrl+Space 开始录音，松开停止。"""

    def test_hotkey_mode_starts_recording_when_model_ready(self):
        with app_harness({"audio.trigger_mode": "hotkey"}) as h:
            app = h["app"]
            app._on_recording_start()

            h["floating"].show_recording.assert_called_once()
            h["recorder"].start.assert_called_once_with(continuous=False)
            h["sound"].play_start.assert_called_once()
            h["tray"].set_recording.assert_called_with(True)

    def test_hotkey_mode_blocked_when_model_not_ready(self):
        with app_harness({"audio.trigger_mode": "hotkey"}) as h:
            h["recognizer"].is_ready = False
            h["recognizer"].is_loading = False
            h["app"]._on_recording_start()

            h["floating"].show_recording.assert_not_called()
            h["recorder"].start.assert_not_called()
            h["floating"].show_error.assert_called_once()

    def test_continuous_mode_uses_hotkey_start_handler(self):
        with app_harness({"audio.trigger_mode": "continuous"}) as h:
            h["app"]._on_recording_start()

            h["floating"].show_recording.assert_not_called()
            h["recorder"].start.assert_not_called()

    def test_continuous_hotkey_start_begins_listening(self):
        with app_harness({"audio.trigger_mode": "continuous"}) as h:
            with patch.object(h["app"], "_start_continuous_listening") as start_cont:
                h["app"]._on_continuous_hotkey_start()
                start_cont.assert_called_once()

    def test_release_stops_recording_and_starts_asr(self):
        with app_harness({"audio.trigger_mode": "hotkey"}) as h:
            h["recorder"].is_recording = True
            h["app"]._on_recording_stop()

            h["sound"].play_stop.assert_called_once()
            h["recorder"].stop.assert_called_once()
            h["tray"].set_recording.assert_called_with(False)

    def test_short_release_without_recording_resets_ui(self):
        with app_harness({"audio.trigger_mode": "hotkey"}) as h:
            h["recorder"].is_recording = False
            h["app"]._on_recording_stop()

            h["recorder"].stop.assert_not_called()
            h["floating"].dismiss_if_idle.assert_called_once()

    def test_esc_cancels_recording(self):
        with app_harness({"audio.trigger_mode": "hotkey"}) as h:
            h["recorder"].is_recording = True
            h["app"]._on_recording_cancel()

            h["recorder"].cancel.assert_called_once()
            h["floating"].show_cancelled.assert_called_once()
            h["floating"].dismiss_if_idle.assert_called()

    def test_esc_without_a_live_recording_cancels_nothing(self):
        """README: 录音中 Esc 取消。A hold that never started (refused while
        the previous utterance was still recognizing) leaves nothing to cancel."""
        with app_harness({"audio.trigger_mode": "hotkey"}) as h:
            h["recorder"].is_recording = False
            h["app"]._on_recording_cancel()

            h["recorder"].cancel.assert_not_called()
            h["floating"].show_cancelled.assert_not_called()
            h["floating"].dismiss_if_idle.assert_called()

    def test_recording_too_short_shows_friendly_error(self):
        with app_harness({"audio.trigger_mode": "hotkey"}) as h:
            short = np.zeros(MIN_AUDIO_SAMPLES - 1, dtype=np.float32)
            h["app"]._on_recording_finished(short)

            h["floating"].show_error.assert_called_once()
            err_msg = h["floating"].show_error.call_args[0][0]
            assert "录音过短" in err_msg

    def test_valid_recording_begins_transcription(self):
        with app_harness({"audio.trigger_mode": "hotkey"}) as h:
            audio = np.zeros(MIN_AUDIO_SAMPLES, dtype=np.float32)
            h["app"]._on_recording_finished(audio)

            h["recognizer"].transcribe_final.assert_called_once()
            h["floating"].show_recognizing.assert_called_once()
            assert h["app"]._is_transcribing is True

    def test_confirmed_text_accumulates_on_the_listening_bar(self):
        with app_harness({"audio.trigger_mode": "continuous", "llm.enabled": False}) as h:
            h["app"]._on_final_result("今天天气不错")
            h["app"]._on_final_result("我们出发吧")
            shown = [
                call.args[0]
                for call in h["floating"].show_live_transcript.call_args_list
            ]
            assert shown[-1] == "今天天气不错我们出发吧"

    def test_hold_slice_text_shows_while_the_key_is_still_down(self):
        with app_harness({"audio.trigger_mode": "hotkey", "llm.enabled": False}) as h:
            h["recorder"].is_recording = True
            h["app"]._on_partial_result("前十五秒")
            h["app"]._on_final_result("前十五秒还在说")
            shown = [
                call.args[0]
                for call in h["floating"].show_live_transcript.call_args_list
            ]
            assert shown[0] == "前十五秒"
            assert shown[-1] == "前十五秒还在说"
            h["floating"].dismiss_if_idle.assert_not_called()


class TestReadmeHotkeyManagerToApp:
    """README: 按住说话约 0.18 秒、持续转写约 0.30 秒；短按不进入。"""

    def test_ctrl_space_hold_emits_recording_start(self):
        app = QApplication.instance() or QApplication(sys.argv)
        mgr = HotKeyManager("ctrl+space", parent=app)
        started = []
        mgr.recording_start.connect(lambda: started.append(True))
        mgr._on_press(keyboard.Key.ctrl_l)
        mgr._on_press(keyboard.Key.space)
        assert mgr._hold_pending
        mgr._on_hold_timeout()
        assert started

    def test_app_connects_hotkey_recording_start_signal(self):
        with app_harness({"audio.trigger_mode": "hotkey"}) as h:
            h["hotkey"].recording_start.connect.assert_called()


class TestReadmeContinuousMode:
    """README FAQ: 自动持续转写 — 按住快捷键开始，Esc 或听写条「结束」结束。"""

    def test_stt_ready_does_not_auto_start_continuous(self):
        with app_harness({"audio.trigger_mode": "continuous", "hotkey": "ctrl+space"}) as h:
            with patch.object(h["app"], "_start_continuous_listening") as start_cont:
                h["app"]._on_stt_ready()
                start_cont.assert_not_called()
                h["floating"].show_continuous_idle.assert_not_called()
                h["floating"].clear_model_loading_lock.assert_called()
                h["floating"].dismiss_if_idle.assert_called()
                h["tray"].showMessage.assert_called()
                msg = h["tray"].showMessage.call_args[0][1]
                assert "持续转写" in msg or "监听" in msg
                assert "Esc 或听写条「结束」" in msg
                assert "浮窗" not in msg
                assert "×" not in msg

    def test_continuous_short_tap_does_not_show_idle_float(self):
        with app_harness({"audio.trigger_mode": "continuous", "hotkey": "ctrl+space"}) as h:
            h["recorder"].is_continuous = False
            h["app"]._on_hotkey_tap_too_short()
            h["floating"].show_continuous_idle.assert_not_called()
            h["tray"].showMessage.assert_called()

    def test_close_button_stops_continuous_session(self):
        with app_harness({"audio.trigger_mode": "continuous"}) as h:
            h["recorder"].is_continuous = True
            h["app"]._stop_continuous_user_session()
            h["recorder"].stop_continuous.assert_called_once()
            h["floating"].show_continuous_stopped.assert_called_once()
            assert h["app"]._continuous_user_stopped is True

    def test_stt_ready_shows_hotkey_hint_in_hotkey_mode(self):
        with app_harness({"audio.trigger_mode": "hotkey", "hotkey": "ctrl+space"}) as h:
            with patch.object(h["app"], "_start_continuous_listening") as start_cont:
                h["app"]._on_stt_ready()
                start_cont.assert_not_called()
                h["floating"].show_success.assert_called_once()
                h["tray"].showMessage.assert_called()
                msg = h["tray"].showMessage.call_args[0][1]
                assert "Ctrl" in msg or "ctrl" in msg.lower()

    def test_settings_switch_stops_continuous_listening(self):
        with app_harness({"audio.trigger_mode": "hotkey"}) as h:
            h["recorder"].is_continuous = True
            with patch.object(h["app"], "_stop_continuous_listening") as stop_cont:
                h["app"]._on_settings_changed()
                stop_cont.assert_called_once()

    def test_segment_queue_processed_after_transcription(self):
        with app_harness({"audio.trigger_mode": "continuous"}) as h:
            app = h["app"]
            app._is_transcribing = True
            seg = np.zeros(MIN_AUDIO_SAMPLES, dtype=np.float32)
            app._segment_queue.append(seg)
            app._is_transcribing = False
            app._recorder.is_continuous = True

            app._pump_segment_queue()
            h["recognizer"].transcribe_final.assert_called_once_with(seg)


class TestReadmeAsrOutputAndPaste:
    """README: 识别结果去掉 <asr_text>；可选润色后粘贴。"""

    def test_final_result_strips_asr_tags_before_output(self):
        with app_harness({"audio.trigger_mode": "hotkey", "llm.enabled": False}) as h:
            with patch.object(h["app"], "_output_text") as out:
                h["app"]._on_final_result("<asr_text>你好世界</asr_text>")
                out.assert_called_once_with("你好世界")

    def test_empty_recognition_shows_error_in_hotkey_mode(self):
        with app_harness({"audio.trigger_mode": "hotkey"}) as h:
            h["app"]._on_final_result("   ")
            h["floating"].show_error.assert_called()
            assert h["app"]._is_transcribing is False

    def test_llm_enabled_routes_to_polisher(self):
        with app_harness(
            {
                "audio.trigger_mode": "hotkey",
                "llm.enabled": True,
                "llm.api_url": "http://localhost/v1",
                "llm.api_key": "k",
                "llm.model_name": "m",
            }
        ) as h:
            h["app"]._on_final_result("测试文本")
            h["polisher"].polish.assert_called_once()
            h["floating"].show_polishing.assert_called_once()

    def test_output_text_pastes_without_polish(self):
        with app_harness({"audio.trigger_mode": "hotkey"}) as h:
            with patch.object(h["app"], "_on_polish_complete", wraps=h["app"]._on_polish_complete):
                h["app"]._output_text("直接粘贴")
                h["paster"].paste_async.assert_called_once()
                assert h["paster"].paste_async.call_args[0][0] == "直接粘贴"


class TestReadmeAudioSources:
    """README: 麦克风 / 电脑播放 / 混合 三种音频来源。"""

    def test_apply_audio_config_microphone(self):
        with app_harness({"audio.input_source": "microphone"}) as h:
            h["recorder"].configure.assert_called()
            args, kwargs = h["recorder"].configure.call_args
            assert kwargs.get("input_source") == "microphone" or args[0] == "microphone"

    def test_apply_audio_config_system(self):
        with app_harness(
            {"audio.input_source": "system", "audio.system_device_index": 17}
        ) as h:
            h["recorder"].configure.reset_mock()
            h["app"]._apply_audio_config()
            kwargs = h["recorder"].configure.call_args.kwargs
            assert kwargs["input_source"] == "system"
            assert kwargs["mic_device_index"] == -1
            assert "system_device_index" in kwargs

    def test_apply_audio_config_mixed(self):
        with app_harness(
            {
                "audio.input_source": "mixed",
                "audio.mic_device_index": 0,
                "audio.system_device_index": 17,
            }
        ) as h:
            h["app"]._apply_audio_config()
            kwargs = h["recorder"].configure.call_args.kwargs
            assert kwargs["input_source"] == "mixed"

    def test_invalid_source_falls_back_to_microphone(self):
        with app_harness({"audio.input_source": "invalid"}) as h:
            h["app"]._apply_audio_config()
            kwargs = h["recorder"].configure.call_args.kwargs
            assert kwargs["input_source"] == "microphone"


class TestReadmeIslandCopy:
    def test_readme_and_build_point_to_engine_nav(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        readme = (root / "README.md").read_text(encoding="utf-8")
        build = (root / "build.py").read_text(encoding="utf-8")
        assert "设置 → 模型" not in readme
        assert "设置 → 引擎" in readme
        assert "模型加载中" not in readme
        assert "模型载入中" in readme
        assert "设置 → 模型" not in build
        assert "设置 → 引擎" in build

    def test_readme_describes_main_window_not_island(self):
        from pathlib import Path

        text = Path("README.md").read_text(encoding="utf-8")
        assert "打开 VoiceInk" in text
        assert "听写条" in text or "薄" in text
        assert "双击托盘会唤醒空间岛" not in text


class TestReadmeReliabilityPromises:
    """README states the guarantees covered by the reliability regression tests."""

    def _readme(self) -> str:
        from pathlib import Path

        return (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")

    def test_output_and_polish_guarantees_are_documented(self):
        text = self._readme()
        assert "就不发送粘贴键" in text
        assert "回退它自己的原文" in text
        assert "删改了原话里的阿拉伯数字" in text
        assert "Key 可留空" in text

    def test_storage_and_update_guarantees_are_documented(self):
        text = self._readme()
        assert "只在本次运行中有效、同样不写入 `config.json`" in text
        assert "没有公布该安装包 SHA-256 时，VoiceInk 不会下载或自动安装" in text
        assert "长时间监听只在内存中保留当前这句话" not in text
        assert "自动暂停监听" in text

    def test_unverified_claims_are_marked(self):
        text = self._readme()
        assert "准确率最高" not in text
        assert "（实验性）" in text
        assert "尚未做系统验证" in text


class TestReadmeSettingsLifecycle:
    """README: 修改触发方式/音频来源后保存生效；关闭设置恢复快捷键。"""

    def test_settings_closed_resumes_hotkey_listener(self):
        with app_harness({"audio.trigger_mode": "hotkey"}) as h:
            settings = type("W", (), {"cancel_hotkey_capture": lambda self: None})()
            h["app"]._main = type("M", (), {"_settings": settings})()
            h["hotkey"].pause()
            h["app"]._on_settings_closed()
            h["hotkey"].resume.assert_called_once()

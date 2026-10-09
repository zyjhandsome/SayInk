"""
Integration tests aligned with README「操作指南」与「声音收录」。

覆盖：按住快捷键录音、松开识别、Esc 取消、短按防误触、
持续转写模式、三种音频来源配置、ASR 标签清洗后输出。
"""

from __future__ import annotations

import sys

import numpy as np
from PyQt6.QtWidgets import QApplication
from pynput import keyboard
from unittest.mock import patch

from tests.helpers.app_harness import app_harness
from sayink.app import MIN_AUDIO_SAMPLES
from sayink.hotkey_manager import HotKeyManager
from sayink.text_paster import PasteResult


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
            with patch("sayink.app.QMessageBox") as boxes, patch("sayink.app.QApplication.quit") as quit_app:
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
            with patch("sayink.app.QMessageBox") as boxes, patch("sayink.app.QApplication.quit") as quit_app:
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
            h["tray"].set_recording.assert_called_with(False)
            h["tray"].set_activity_tooltip.assert_called_with(None)

    def test_release_of_refused_hold_keeps_the_reason_on_screen(self):
        """README P0: 加载中不被其它错误盖住. Letting go of a hold refused
        while the model loads (or the previous utterance is still being
        recognized) used to hide the bar and drop the loading lock."""
        with app_harness({"audio.trigger_mode": "hotkey"}) as h:
            h["recorder"].is_recording = False
            h["recognizer"].is_loading = True
            h["floating"].reset_mock()
            h["tray"].reset_mock()
            h["app"]._on_recording_stop()
            h["floating"].dismiss_if_idle.assert_not_called()
            h["tray"].set_activity_tooltip.assert_not_called()

            h["recognizer"].is_loading = False
            h["app"]._is_transcribing = True
            h["app"]._on_recording_stop()
            h["floating"].dismiss_if_idle.assert_not_called()
            h["tray"].set_activity_tooltip.assert_not_called()

    def test_esc_during_hold_cancels_once_and_keeps_cancelled_visible(self):
        with app_harness({"audio.trigger_mode": "hotkey"}) as h:
            app = h["app"]
            h["recorder"].is_recording = True
            h["recorder"].cancel.side_effect = lambda: setattr(h["recorder"], "is_recording", False)
            h["floating"].reset_mock()
            # The hotkey manager emits both signals for one Esc press.
            app._on_esc_pressed()
            app._on_recording_cancel()

            h["recorder"].cancel.assert_called_once()
            h["floating"].show_cancelled.assert_called_once()
            assert h["floating"].mock_calls[-1][0] == "show_cancelled"

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
            h["floating"].reset_mock()
            h["app"]._on_recording_cancel()

            h["recorder"].cancel.assert_not_called()
            h["floating"].show_cancelled.assert_not_called()
            h["floating"].dismiss_if_idle.assert_not_called()

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


class TestReadmeHotkeyBindingRules:
    """README: 录制支持 F1–F12；Esc 不能作为录音键；短按先松 Shift 也输入 X。"""

    def test_function_keys_parse_with_a_modifier(self):
        from sayink.hotkey_manager import parse_hotkey

        for n in (1, 5, 12):
            assert parse_hotkey(f"ctrl+f{n}") == {keyboard.Key.ctrl_l, getattr(keyboard.Key, f"f{n}")}

    def test_capture_box_never_produces_an_esc_binding(self):
        from sayink.ui.hotkey_edit import _qt_key_to_name
        from PyQt6.QtCore import Qt

        assert _qt_key_to_name(Qt.Key.Key_Escape) == ""

    def test_shift_first_short_tap_does_not_hint_too_short(self, monkeypatch):
        app = QApplication.instance() or QApplication(sys.argv)
        mgr = HotKeyManager("shift+x", parent=app)
        hints = []
        mgr.hotkey_tap_too_short.connect(lambda: hints.append(True))
        mgr._on_press(keyboard.Key.shift_l)
        mgr._on_press(keyboard.KeyCode.from_char("x"))
        assert mgr._hold_pending
        mgr._hold_started_at -= 0.1
        mgr._on_release(keyboard.Key.shift_l)
        assert not mgr._hold_pending
        assert hints == []


class TestReadmeAutoStart:
    """README: 开机自启命令须能从任意目录启动，源码运行时经由解释器执行 run.py。"""

    def test_registry_command_is_absolute_and_quoted(self, monkeypatch):
        import os
        from sayink.app import auto_start_command

        monkeypatch.delattr(sys, "frozen", raising=False)
        monkeypatch.delattr(sys, "_MEIPASS", raising=False)
        command = auto_start_command()
        parts = [p for p in command.split('"') if p.strip()]
        assert all(os.path.isabs(p) for p in parts)
        assert parts[-1].endswith("run.py")


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

    def test_recognition_error_after_session_ended_does_not_reopen_the_mic(self):
        """Settings save / model reload / device loss end the session; a late
        recognition error must not start listening again on its own."""
        with app_harness({"audio.trigger_mode": "continuous"}) as h:
            h["recorder"].is_continuous = False
            h["app"]._continuous_user_stopped = False
            with patch("sayink.app.QTimer.singleShot") as later:
                h["app"]._on_recognizer_error("识别失败")
            assert not any(
                c.args[1] == h["app"]._start_continuous_listening
                for c in later.call_args_list
            )

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
            app._enqueue_audio(seg)
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

    def test_user_spoken_html_tags_survive_while_asr_markers_go(self):
        """README: 只去识别器自身的标记，口述的 <div> 保留。"""
        from sayink.speech_recognizer import normalize_asr_output

        assert normalize_asr_output("<|zh|><asr_text>写一个 <div> 标签<sil>") == "写一个 <div> 标签"

    def test_whole_japanese_utterance_is_kept(self):
        """README: 整句都是日语时原样保留。"""
        from sayink.speech_recognizer import normalize_asr_output

        text = "こんにちは、今日はいい天気ですね。"
        assert normalize_asr_output(text) == text

    def test_unverified_paste_tells_the_user_to_check(self):
        """README: 粘贴键发出后前台变了 → 「已发送 · 未确认」。"""
        with app_harness({"audio.trigger_mode": "hotkey", "llm.enabled": False}) as h:
            app = h["app"]
            app._output_text("你好")
            h["paster"].paste_async.call_args[0][1](PasteResult("unverified", detail="focus_changed_after_send"))
            title = h["floating"].show_info.call_args[0][0]
            assert "未确认" in title

    def test_long_hold_is_sliced_at_a_quiet_frame(self):
        """README: 超过 15 秒的长句在最安静处分片。"""
        from sayink.speech_recognizer import SAMPLE_RATE, plan_audio_slices

        audio = np.full(int(20 * SAMPLE_RATE), 0.5, dtype=np.float32)
        quiet = slice(int(13.0 * SAMPLE_RATE), int(13.1 * SAMPLE_RATE))
        audio[quiet] = 0.01
        first = plan_audio_slices(audio, SAMPLE_RATE, slice_sec=15.0, overlap_sec=0.4)[0]
        assert 12.9 * SAMPLE_RATE <= first.size <= 13.2 * SAMPLE_RATE


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
        assert "打开 SayInk" in text
        assert "听写条" in text or "薄" in text
        assert "双击托盘会唤醒空间岛" not in text


class TestReadmeReliabilityPromises:
    """README states the guarantees covered by the reliability regression tests."""

    def _readme(self) -> str:
        from pathlib import Path

        return (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")

    def test_positioning_and_output_mode_are_documented(self):
        """P-01: dictation first; meeting capture never types into the front window."""
        from sayink.config import DEFAULT_CONFIG, TRIGGER_MODE_HOTKEY, OUTPUT_MODE_PASTE

        text = self._readme()
        assert DEFAULT_CONFIG["audio"]["trigger_mode"] == TRIGGER_MODE_HOTKEY
        assert DEFAULT_CONFIG["output"]["mode"] == OUTPUT_MODE_PASTE
        assert "**按住说话**（默认）" in text
        assert "持续转写（默认）" not in text
        assert "只记录到历史，不粘贴" in text
        assert "改回「仅麦克风」会自动恢复粘贴" in text
        assert "手动改过的选择不会再被来源切换覆盖" in text
        assert "未保存 · 历史已关闭" in text

    def test_expected_latency_is_documented_and_matches_vad_hold(self):
        """F-19: README states measured latency; the silence figure tracks the code."""
        from sayink.vad_segmenter import SILENCE_HOLD_SEC

        text = self._readme()
        assert "预期延迟（本机实测" in text
        assert "实时率约 **0.5–0.8**" in text
        assert f"约 **{SILENCE_HOLD_SEC:.2f} 秒**静音判定" in text
        assert "说完要等几秒才出字，正常吗" in text

    def test_feedback_entry_and_truth_source_are_documented(self):
        """P-06 / P-10: README points at the issue tracker and names itself the truth source.
        The About page feedback row is hidden, so the README must not send users to it."""
        from pathlib import Path

        from sayink.updater import ISSUES_URL

        text = self._readme()
        assert "「GitHub 反馈」" not in text
        assert ISSUES_URL in text
        assert "**真相源：**" in text
        assert "不必再起 OpenSpec 变更" in text
        root = Path(__file__).resolve().parents[1]
        assert (root / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml").exists()
        assert (root / ".github" / "ISSUE_TEMPLATE" / "feature_request.yml").exists()
        # No live (unarchived) OpenSpec change directories remain.
        live = [p for p in (root / "openspec" / "changes").iterdir() if p.is_dir() and p.name != "archive"]
        assert live == []

    def test_output_and_polish_guarantees_are_documented(self):
        text = self._readme()
        assert "就不发送粘贴键" in text
        assert "回退它自己的原文" in text
        assert "删改了原话里的数字" in text
        assert "「三万五」等中文数字换算后比对" in text
        assert "少了否定词" in text
        assert "已发送 · 未确认" in text
        assert "输出超时 · 已复制" in text
        assert "Key 可留空" in text

    def test_storage_and_update_guarantees_are_documented(self):
        text = self._readme()
        assert "Key 只在本次运行中有效" in text
        assert "残留的明文 Key 也会被清掉" in text
        assert "旧版 VoiceInk 还在运行时 SayInk 不会再开一个实例" in text
        assert "没有公布该安装包 SHA-256 时，SayInk 不会下载或自动安装" in text
        assert "长时间监听只在内存中保留当前这句话" not in text
        assert "自动暂停监听" in text

    # P-04: lite installer is the default download and the update channel.
    def test_lite_and_full_installers_are_documented(self):
        text = self._readme()
        assert "轻量包，默认" in text
        assert "-full.exe" in text
        assert "应用内自动更新一律下载轻量包" in text
        assert "升级会不会重新下载模型" in text
        assert "build_release.py --with-model" in text

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
            h["app"]._main = type("M", (), {"settings_panel": settings})()
            h["hotkey"].pause()
            h["app"]._on_settings_closed()
            h["hotkey"].resume.assert_called_once()

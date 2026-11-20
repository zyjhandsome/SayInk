"""Regression tests for HUD state machine, two-step meeting end, tray menus, and Settings UX/consent."""

from __future__ import annotations

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QLabel

from sayink.config import OUTPUT_MODE_HISTORY, OUTPUT_MODE_PASTE, TRIGGER_MODE_CONTINUOUS, Config
from sayink.ui.floating_window import MEETING_END_CONFIRM_TIMEOUT_MS, FloatingWindow
from sayink.ui.settings_window import (
    SettingsWindow,
    confirm_llm_remote_host,
    llm_remote_host_needing_confirmation,
)
from sayink.ui.tray_icon import TrayIcon


@pytest.fixture
def hud(qapp):
    w = FloatingWindow()
    yield w
    w.close()
    w.deleteLater()


@pytest.fixture
def tray(qapp):
    t = TrayIcon()
    yield t
    t.hide()
    t.deleteLater()


@pytest.fixture
def settings_bundle(qapp, tmp_path, monkeypatch):
    for name in ("_rebuild_model_cards", "_refresh_about_info", "_refresh_audio_device_lists"):
        monkeypatch.setattr(SettingsWindow, name, lambda self: None)
    cfg = Config(config_dir=tmp_path / ".sayink")
    win = SettingsWindow(cfg)
    yield win, cfg
    win.close()
    win.deleteLater()
    cfg._save_timer.stop()


class TestFloatingWindowStateAndCancelScoping:
    def test_terminal_states_clear_capture_active_hide_cancel_and_enable_passive_dismiss(self, hud):
        hud.show_recording()
        assert hud._capture_active is True
        assert hud._cancel_btn.isVisible()
        assert hud._cancel_btn.toolTip() == "取消本次转写 · Esc"

        hud.end_capture()
        assert hud._capture_active is True
        assert hud._capture_released is True
        assert hud._cancel_btn.isVisible()
        assert hud._cancel_btn.toolTip() == "取消上一句识别 · Esc"

        hud.show_success("已发送", "发送到 notepad.exe")
        assert hud._capture_active is False
        assert hud._capture_released is False
        assert not hud._cancel_btn.isVisible()
        assert hud._dismiss_btn.isVisible()
        hud._dismiss_btn.click()
        assert not hud.isVisible()

        hud.show_recording()
        hud.end_capture()
        hud.show_info("未输入到 notepad.exe", "已复制，回 notepad.exe 按 Ctrl+V")
        assert hud._capture_active is False
        assert hud._capture_released is False
        assert not hud._cancel_btn.isVisible()
        assert hud._dismiss_btn.isVisible()

        hud.show_recording()
        hud.end_capture()
        hud.show_warning("剪贴板已变化", "已取消自动粘贴")
        assert hud._capture_active is False
        assert hud._capture_released is False
        assert not hud._cancel_btn.isVisible()
        assert hud._dismiss_btn.isVisible()

    def test_show_busy_transcribing_defaults_to_no_cancel_button(self, hud):
        hud.show_recording()
        hud.end_capture()
        assert hud._cancel_btn.isVisible()

        # Refused 2nd hotkey press must NOT expose a cancel button that kills Sentence A.
        hud.show_busy_transcribing()
        assert "请稍候" in hud.status_text
        assert not hud._cancel_btn.isVisible()

        # Explicit allow_cancel=True shows post-release cancel with clear Esc tooltip.
        hud.show_busy_transcribing(allow_cancel=True)
        assert hud._cancel_btn.isVisible()
        assert hud._cancel_btn.toolTip() == "取消上一句识别 · Esc"


class TestFloatingWindowSubtitlePriorityAndRecovery:
    def test_live_text_never_clobbers_explicit_subtitle_and_restores_cleanly(self, hud):
        # Single-shot toast with explicit subtitle
        hud.show_recording()
        hud.show_live_transcript("这是刚刚说出的第一段文字")
        hud.end_capture()
        hud.show_info("未输入到 Code.exe", "已复制，回 Code.exe 按 Ctrl+V")
        assert "已复制，回 Code.exe 按 Ctrl+V" in hud.excerpt_text
        assert "第一段文字" not in hud.excerpt_text

        hud.show_recording()
        hud.show_live_transcript("这是第一段有效内容")
        hud.end_capture()
        hud.show_success("已发送", "发送到 Code.exe · 有 1 段未识别")
        assert "有 1 段未识别" in hud.excerpt_text

        # Continuous dictation toast with explicit subtitle -> restores _live_text on refresh
        hud.show_listening()
        hud.show_live_transcript("持续转写中已经上屏的句子")
        hud.show_info("未输入到 Code.exe", "已复制，回 Code.exe 按 Ctrl+V")
        assert hud._listening_active is True
        assert "已复制，回 Code.exe 按 Ctrl+V" in hud.excerpt_text
        assert hud._live_text == "持续转写中已经上屏的句子"

        hud.refresh_continuous_ui()
        assert "持续转写中已经上屏的句子" in hud.excerpt_text
        assert "正在听" in hud.status_text

    def test_error_and_warning_keep_continuous_listening_and_preserve_live_text(self, hud):
        hud.show_listening()
        hud.show_live_transcript("保留的上一句内容")

        hud.show_error("上一段未识别，请重说", keep_continuous=True, auto_dismiss_ms=2500)
        assert hud._listening_active is True
        assert hud._live_text == "保留的上一句内容"
        assert hud.status_text == "上一段未识别"
        assert "请重说" in hud.excerpt_text
        assert hud._continuous_toast_timer.isActive()
        assert not hud._hide_timer.isActive()

        hud._on_continuous_toast_timeout()
        assert "正在听" in hud.status_text
        assert "保留的上一句内容" in hud.excerpt_text

        hud.show_warning("保存失败（2 段未保存）", "请检查磁盘空间", keep_continuous=True, auto_dismiss_ms=2500)
        assert hud._listening_active is True
        assert hud._live_text == "保留的上一句内容"
        assert "保存失败（2 段未保存）" in hud.status_text
        assert "请检查磁盘空间" in hud.excerpt_text
        assert hud._continuous_toast_timer.isActive()

        hud._on_continuous_toast_timeout()
        assert "正在听" in hud.status_text
        assert "保留的上一句内容" in hud.excerpt_text

    def test_unsaved_history_error_buttons_emit_copy_and_retry_signals(self, hud):
        copied = []
        retried = []
        hud.copy_unsaved_requested.connect(lambda: copied.append(1))
        hud.retry_save_requested.connect(lambda: retried.append(1))

        # Single-shot unsaved DB error
        hud.show_unsaved_history_error("保存历史失败", "数据库只读", count=1, keep_continuous=False)
        assert hud._copy_unsaved_btn.isVisible()
        assert hud._retry_save_btn.isVisible()
        assert hud._copy_unsaved_btn.text() == "复制原文"
        assert hud._retry_save_btn.text() == "重试保存"
        assert hud._copy_unsaved_btn.focusPolicy() == Qt.FocusPolicy.NoFocus
        assert hud._retry_save_btn.focusPolicy() == Qt.FocusPolicy.NoFocus
        assert hud._dismiss_btn.isVisible()

        hud._copy_unsaved_btn.click()
        hud._retry_save_btn.click()
        assert copied == [1]
        assert retried == [1]

        # Continuous unsaved DB error with count > 1
        hud.show_listening()
        hud.show_live_transcript("未落盘前保留的字幕")
        hud.show_unsaved_history_error("保存历史失败", "磁盘繁忙", count=3)
        assert hud._listening_active is True
        assert "3 段未保存" in hud.status_text
        assert hud._copy_unsaved_btn.isVisible()
        assert hud._retry_save_btn.isVisible()
        assert hud._end_btn.isVisible()

        hud.clear_unsaved_history_error()
        assert not hud._copy_unsaved_btn.isVisible()
        assert not hud._retry_save_btn.isVisible()
        assert "未落盘前保留的字幕" in hud.excerpt_text


class TestSpeechCapturingAndRightAnchoredEnd:
    def test_discard_button_tracks_live_speech_without_shifting_end_button(self, hud, qapp):
        cancels = []
        ends = []
        hud.cancel_requested.connect(lambda: cancels.append(1))
        hud.end_requested.connect(lambda: ends.append(1))

        hud.show_listening()
        qapp.processEvents()
        assert not hud._cancel_btn.isVisible()
        end_geo_before = hud._end_btn.geometry()

        # Sentence 1 speech opens while _live_text is still empty -> 放弃本句 appears immediately!
        hud.set_speech_capturing(True)
        qapp.processEvents()
        assert hud._cancel_btn.isVisible()
        assert hud._cancel_btn.text() == "放弃本句"
        assert hud._end_btn.geometry() == end_geo_before

        # Clicking 放弃本句 on sentence 1 emits cancel_requested, NOT end_requested!
        hud._cancel_btn.click()
        assert cancels == [1]
        assert ends == []

        # Sentence 1 commits to _live_text, then silence begins -> 放弃本句 hides during idle silence!
        hud.show_live_transcript("第一句话已经识别完毕")
        qapp.processEvents()
        end_geo_excerpt = hud._end_btn.geometry()
        assert end_geo_excerpt.x() == end_geo_before.x()
        assert end_geo_excerpt.width() == end_geo_before.width()

        hud.set_speech_capturing(False)
        qapp.processEvents()
        assert not hud._cancel_btn.isVisible()
        assert hud._end_btn.geometry() == end_geo_excerpt

        hud.set_speech_capturing(True)
        qapp.processEvents()
        assert hud._cancel_btn.isVisible()
        assert hud._end_btn.geometry() == end_geo_excerpt


class TestTwoStepMeetingEndAndStopMenus:
    def test_floating_meeting_end_two_step_confirmation_and_timeout(self, hud):
        ends = []
        hud.end_requested.connect(lambda: ends.append(1))

        hud.show_meeting("00:08:30")
        assert hud._end_btn.isVisible()
        assert not hud._meeting_end_confirm_btn.isVisible()
        assert not hud._meeting_end_cancel_btn.isVisible()

        # 1st click enters focusless inline armed state without emitting end_requested
        hud._end_btn.click()
        assert ends == []
        assert hud.status_text == "确认结束？"
        assert not hud._end_btn.isVisible()
        assert not hud._mark_btn.isVisible()
        assert hud._meeting_end_confirm_btn.isVisible()
        assert hud._meeting_end_cancel_btn.isVisible()
        assert hud._meeting_end_confirm_btn.focusPolicy() == Qt.FocusPolicy.NoFocus
        assert hud._meeting_end_cancel_btn.focusPolicy() == Qt.FocusPolicy.NoFocus
        assert hud._meeting_end_confirm_timer.isActive()
        assert hud._meeting_end_confirm_timer.interval() == MEETING_END_CONFIRM_TIMEOUT_MS

        # Clicking [取消] reverts immediately
        hud._meeting_end_cancel_btn.click()
        assert ends == []
        assert not hud._meeting_end_confirm_timer.isActive()
        assert "00:08:30" in hud.status_text
        assert hud._end_btn.isVisible()
        assert hud._mark_btn.isVisible()

        # Auto-revert after 3000ms timeout
        hud._end_btn.click()
        assert hud._meeting_end_armed is True
        hud._disarm_meeting_end_confirm()
        assert ends == []
        assert hud._meeting_end_armed is False
        assert "00:08:30" in hud.status_text

        # Clicking [确认] emits end_requested
        hud._end_btn.click()
        hud._meeting_end_confirm_btn.click()
        assert ends == [1]

        # Continuous dictation mode remains 1-click
        hud.end_meeting_state()
        hud.show_listening()
        hud._end_btn.click()
        assert ends == [1, 1]

    def test_floating_context_menu_stop_continuous_action(self, hud):
        ends = []
        hud.end_requested.connect(lambda: ends.append(1))

        hud.show_listening()
        menu = hud._position_menu()
        labels = [a.text() for a in menu.actions() if not a.isSeparator()]
        assert "结束持续转写并保留内容" in labels
        stop_act = next(a for a in menu.actions() if a.text() == "结束持续转写并保留内容")
        stop_act.trigger()
        assert ends == [1]

        # Meeting mode must not show dictation stop action in context menu
        hud.show_meeting("00:01:00")
        meeting_labels = [a.text() for a in hud._position_menu().actions() if not a.isSeparator()]
        assert "结束持续转写并保留内容" not in meeting_labels

    def test_tray_continuous_stop_and_meeting_end_submenu(self, tray):
        cont_stops = []
        meeting_starts = []
        meeting_ends = []
        tray.stop_continuous_requested.connect(lambda: cont_stops.append(1))
        tray.meeting_start_requested.connect(lambda: meeting_starts.append(1))
        tray.end_meeting_requested.connect(lambda: meeting_ends.append(1))

        idle_texts = [a.text() for a in tray.contextMenu().actions() if not a.isSeparator()]
        assert "结束持续转写并保留内容" not in idle_texts

        # Continuous dictation active -> renders stop item
        tray.set_continuous_listening(True)
        active_texts = [a.text() for a in tray.contextMenu().actions() if not a.isSeparator()]
        assert "结束持续转写并保留内容" in active_texts
        stop_act = next(a for a in tray.contextMenu().actions() if a.text() == "结束持续转写并保留内容")
        stop_act.trigger()
        assert cont_stops == [1]

        tray.set_continuous_listening(False)
        assert "结束持续转写并保留内容" not in [
            a.text() for a in tray.contextMenu().actions() if not a.isSeparator()
        ]

        # Idle 1-click 开始会议
        tray._meeting_toggle_action.trigger()
        assert meeting_starts == [1]
        assert tray._meeting_toggle_action.menu() is None

        # Active meeting -> submenu 结束会议 -> 确认结束本场会议 + 继续记录
        tray.set_meeting_in_progress(True)
        assert tray._meeting_toggle_action.text() == "结束会议"
        sub = tray._meeting_toggle_action.menu()
        assert sub is not None
        sub_labels = [a.text() for a in sub.actions()]
        assert sub_labels == ["确认结束本场会议", "继续记录"]

        # Direct trigger or 继续记录 must NOT end meeting
        tray._meeting_toggle_action.trigger()
        sub.actions()[1].trigger()
        assert meeting_ends == []

        # Clicking 确认结束本场会议 emits end_meeting_requested / meeting_end_requested
        sub.actions()[0].trigger()
        assert meeting_ends == [1]


class TestSettingsUxConflictPreventionAndConsent:
    def test_esc_description_mentions_hotkey_hold_and_tray_menu(self, settings_bundle):
        win, _cfg = settings_bundle
        texts = " ".join(
            lb.text() for lb in win.general_page.esc_stop_row.findChildren(QLabel) if lb.text()
        )
        assert "快捷键" in texts
        assert "托盘菜单" in texts
        assert "只能点转写条" not in texts

    def test_active_capture_changes_show_next_turn_hint_without_false_discard_dialog(
        self, settings_bundle, monkeypatch
    ):
        win, cfg = settings_bundle
        prompts = []
        monkeypatch.setattr(win, "_confirm", lambda title, text, **kw: prompts.append((title, text)) or True)
        win.set_recording_busy_checker(lambda: True)
        win.set_pending_segment_counter(lambda: 2)

        # Output mode change mid-recording
        combo = win.general_page.output_mode_combo
        combo.setCurrentIndex(combo.findData(OUTPUT_MODE_HISTORY))
        assert cfg.output_mode() == OUTPUT_MODE_HISTORY
        assert "已保存，下次转写生效" in win.general_page.output_mode_hint.text()
        assert prompts == []

        # Trigger mode change mid-recording
        win.general_page.trigger_continuous_rb.setChecked(True)
        assert cfg.get("audio.trigger_mode") == TRIGGER_MODE_CONTINUOUS
        assert "已保存，下次转写生效" in win.general_page.hotkey_hint.text()
        assert prompts == []

    def test_prevents_silently_creating_history_only_plus_history_disabled(
        self, settings_bundle, monkeypatch
    ):
        win, cfg = settings_bundle
        gp = win.general_page
        combo = gp.output_mode_combo

        # 1) Destination is history-only, user tries to disable history -> declines -> stays enabled
        combo.setCurrentIndex(combo.findData(OUTPUT_MODE_HISTORY))
        assert cfg.history_only_output() is True
        monkeypatch.setattr(win, "_confirm", lambda *a, **kw: False)
        gp.history_enabled_row.setChecked(False)
        assert cfg.get("history.enabled") is True
        assert gp.history_enabled_row.isChecked() is True

        # Accepting reconciles destination to paste and disables history
        monkeypatch.setattr(win, "_confirm", lambda *a, **kw: True)
        gp.history_enabled_row.setChecked(False)
        assert cfg.get("history.enabled") is False
        assert cfg.output_mode() == OUTPUT_MODE_PASTE
        assert combo.currentData() == OUTPUT_MODE_PASTE

        # 2) History is disabled, user tries to select history-only -> declines -> stays paste
        monkeypatch.setattr(win, "_confirm", lambda *a, **kw: False)
        combo.setCurrentIndex(combo.findData(OUTPUT_MODE_HISTORY))
        assert cfg.get("history.enabled") is False
        assert combo.currentData() == OUTPUT_MODE_PASTE

        # Accepting re-enables history and saves history-only destination
        monkeypatch.setattr(win, "_confirm", lambda *a, **kw: True)
        combo.setCurrentIndex(combo.findData(OUTPUT_MODE_HISTORY))
        assert cfg.get("history.enabled") is True
        assert gp.history_enabled_row.isChecked() is True
        assert cfg.output_mode() == OUTPUT_MODE_HISTORY

    def test_auto_minutes_consent_discloses_all_sent_fields(self, settings_bundle, monkeypatch):
        win, cfg = settings_bundle
        row_text = " ".join(
            lb.text() for lb in win.polish_page.minutes_auto_row.findChildren(QLabel) if lb.text()
        )
        for field in ("完整文字稿", "会议标题", "参会人", "议程", "标记", "我的笔记", "自定义词汇", "说话人称呼"):
            assert field in row_text

        cfg.set("llm.api_url", "https://api.example.com/v1")
        asked = []
        monkeypatch.setattr(win, "_confirm", lambda title, text, **kw: asked.append(text) or False)
        win.polish_page.minutes_auto_row.setChecked(True)
        assert len(asked) == 1
        for field in ("完整文字稿", "会议标题", "参会人", "议程", "标记", "我的笔记", "自定义词汇", "说话人称呼"):
            assert field in asked[0]

    def test_dictation_llm_remote_consent_and_loopback_exemption(self, settings_bundle, monkeypatch):
        win, cfg = settings_bundle
        pp = win.polish_page

        row_text = " ".join(lb.text() for lb in pp.llm_enable_row.findChildren(QLabel) if lb.text())
        assert "自定义词汇" in row_text
        assert "直出原文" in row_text

        # Loopback endpoint never prompts
        pp.llm_url_edit.setText("http://127.0.0.1:11434/v1")
        win.flush_llm_fields()
        monkeypatch.setattr(win, "_confirm", lambda *a, **kw: pytest.fail("loopback must not prompt"))
        pp.llm_enable_row.setChecked(True)
        assert cfg.get("llm.enabled") is True
        pp.llm_enable_row.setChecked(False)

        # Remote non-loopback endpoint prompts first time
        pp.llm_url_edit.setText("https://api.deepseek.com/v1")
        pp.llm_key_edit.setText("sk-demo")
        pp.llm_model_edit.setText("deepseek-flash")
        win.flush_llm_fields()
        assert llm_remote_host_needing_confirmation(cfg) == "https://api.deepseek.com"

        prompts = []
        monkeypatch.setattr(win, "_confirm", lambda title, text, **kw: prompts.append(text) or False)
        pp.llm_enable_row.setChecked(True)
        assert len(prompts) == 1
        assert "https://api.deepseek.com" in prompts[0]
        assert "自定义词汇" in prompts[0]
        assert "近期上下文" in prompts[0]
        assert "直出原文" in prompts[0]
        assert cfg.get("llm.enabled") is False
        assert not pp.llm_enable_row.isChecked()
        assert cfg.get("llm.confirmed_remote_hosts", []) in ([], None)

        # Accepting persists in llm.confirmed_remote_hosts and enables polish
        monkeypatch.setattr(win, "_confirm", lambda *a, **kw: True)
        pp.llm_enable_row.setChecked(True)
        assert cfg.get("llm.enabled") is True
        assert "https://api.deepseek.com" in cfg.get("llm.confirmed_remote_hosts", [])
        assert llm_remote_host_needing_confirmation(cfg) == ""

        # Subsequent toggles do not prompt again for the confirmed host
        pp.llm_enable_row.setChecked(False)
        monkeypatch.setattr(win, "_confirm", lambda *a, **kw: pytest.fail("already confirmed"))
        pp.llm_enable_row.setChecked(True)
        assert cfg.get("llm.enabled") is True

        # Helper idempotency check
        confirm_llm_remote_host(cfg, "https://api.deepseek.com:443")
        assert cfg.get("llm.confirmed_remote_hosts") == ["https://api.deepseek.com"]

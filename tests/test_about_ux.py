"""Regressions for truthful recording hints and the About page's task links."""

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest

from sayink.config import OUTPUT_MODE_HISTORY, OUTPUT_MODE_PASTE, TRIGGER_MODE_CONTINUOUS
from sayink.history_store import HistoryStore
from sayink.ui import design_tokens as tok
from sayink.ui.dictation_hints import dictation_hints
from sayink.ui.main_window import MainWindow
from sayink.ui.settings_window import SettingsWindow
from sayink.ui.theme import apply_theme


@pytest.fixture
def about_window(config, tmp_path, monkeypatch, _qapp_session):
    monkeypatch.setattr(SettingsWindow, "_rebuild_model_cards", lambda self: None)
    monkeypatch.setattr(SettingsWindow, "_refresh_audio_device_lists", lambda self: None)
    monkeypatch.setattr("sayink.speech_recognizer.is_model_downloaded", lambda _mid: False)
    store = HistoryStore(tmp_path / "about-history.db")
    win = MainWindow(config, store)
    win.show_page("about")
    win.show()
    _qapp_session.processEvents()
    yield win
    win.hide()
    win.deleteLater()
    config._save_timer.stop()
    store.close()


@pytest.mark.parametrize("source", ["system", "mixed", "microphone"])
def test_hold_hint_names_the_actual_audio_source_and_output(config, source):
    config.set("audio.input_source", source)
    config.set("history.enabled", True)
    config.set_output_mode(OUTPUT_MODE_HISTORY if source != "microphone" else OUTPUT_MODE_PASTE)
    hint = dictation_hints(config)
    assert hint.mode == "单次转写"
    if source != "microphone":
        assert "电脑声音" in hint.source
        assert "保存到历史" in hint.detail
        assert "粘贴" not in hint.detail
    else:
        assert "输入当前光标" in hint.detail


def test_output_hint_respects_a_manual_paste_override_for_system_audio(config):
    config.set("audio.input_source", "system")
    config.set_output_mode(OUTPUT_MODE_PASTE)
    hint = dictation_hints(config)
    assert hint.output == "输入当前光标"
    assert "输入当前光标" in hint.detail
    assert not hint.output_warning


@pytest.mark.parametrize("continuous", [False, True])
def test_disabled_history_is_never_presented_as_saved_output(config, continuous):
    config.set("audio.trigger_mode", "continuous" if continuous else "hotkey")
    config.set_output_mode(OUTPUT_MODE_HISTORY)
    config.set("history.enabled", False)
    hint = dictation_hints(config)
    assert hint.output_warning
    assert "未保存" in hint.output
    assert "不会保存或输入" in hint.detail


def test_continuous_hint_respects_the_esc_preference(config):
    config.set("audio.trigger_mode", TRIGGER_MODE_CONTINUOUS)
    config.set("audio.esc_stops_continuous", False)
    hint = dictation_hints(config)
    assert "Esc" not in hint.shortcut + hint.detail
    assert "松开后继续" in hint.shortcut
    assert "不用一直按住" in hint.detail
    assert "「结束」" in hint.shortcut


def test_config_links_switch_the_page_and_sidebar_together(about_window):
    win = about_window
    settings = win.settings_panel
    rows = settings._about_summary_rows
    rows["语音识别模型"].action_button.click()
    assert win.current_page() == "engine"
    assert win._nav_buttons[3].isChecked()
    rows["录音快捷键"].action_button.click()
    assert win.current_page() == "general"
    assert win._nav_buttons[2].isChecked()
    assert settings._hotkey_edit.hasFocus()
    rows["文字润色"].action_button.click()
    assert win.current_page() == "polish"
    assert win._nav_buttons[4].isChecked()


def test_sidebar_and_summary_refresh_without_reconfiguring_audio(about_window):
    win = about_window
    settings = win.settings_panel
    audio_changes = []
    settings.settings_changed.connect(lambda: audio_changes.append(True))
    settings._history_enabled_row.setChecked(False)
    settings._output_mode_combo.setCurrentIndex(settings._output_mode_combo.findData("history"))
    assert "未保存" in win._output_label.text()
    assert "未保存" in settings._about_summary_rows["输出去向"].value_label.text()
    settings._history_enabled_row.setChecked(True)
    assert win._output_label.text() == "仅保存到历史"
    assert "保存到历史" in settings._hotkey_hint.text()
    settings._esc_stop_row.setChecked(False)
    assert audio_changes == []


def test_reloading_preferences_updates_sidebar_and_about(about_window, config):
    win = about_window
    config.set("audio.input_source", "system")
    config.set("audio.trigger_mode", TRIGGER_MODE_CONTINUOUS)
    config.set("audio.esc_stops_continuous", False)
    config.set("history.enabled", True)
    config.set_output_mode(OUTPUT_MODE_HISTORY)
    win.settings_panel.reload_settings()
    assert "电脑声音" in win._mode_label.text()
    assert "Esc" not in win._shortcut_label.text()
    assert win.settings_panel._about_summary_rows["输出去向"].value_label.text() == "仅保存到历史"


def test_file_locations_toggle_works_from_the_keyboard_and_survives_theme_changes(about_window, _qapp_session):
    settings = about_window.settings_panel
    toggle = settings._about_paths_toggle
    toggle.setFocus()
    QTest.keyClick(toggle, Qt.Key.Key_Space)
    assert settings._about_paths_wrap.isVisible()
    assert toggle.accessibleName() == "收起文件位置"
    apply_theme(_qapp_session, mode="dark", surfaces=[about_window, settings])
    assert not toggle.icon().isNull()
    assert settings._about_paths_wrap.isVisible()
    QTest.keyClick(toggle, Qt.Key.Key_Space)
    assert settings._about_paths_wrap.isHidden()
    assert toggle.accessibleName() == "展开文件位置"


def test_missing_history_warning_keeps_its_semantics_after_a_theme_switch(about_window, _qapp_session):
    win = about_window
    settings = win.settings_panel
    settings._history_enabled_row.setChecked(False)
    settings._output_mode_combo.setCurrentIndex(settings._output_mode_combo.findData("history"))
    apply_theme(_qapp_session, mode="dark", surfaces=[win, settings])
    assert tok.RED in win._output_label.styleSheet()
    assert tok.RED in settings._about_summary_rows["输出去向"].value_label.styleSheet()


def test_busy_update_button_prevents_duplicate_actions_and_recovers(about_window):
    settings = about_window.settings_panel
    checks, installs = [], []
    settings.update_check_requested.connect(lambda: checks.append(True))
    settings.update_install_requested.connect(lambda: installs.append(True))
    settings.set_update_status("正在检查…", action="busy")
    settings._about_update_btn.click()
    assert checks == []
    settings.set_update_status("检查失败，请稍后再试", action="check")
    settings._about_update_btn.click()
    assert checks == [True]
    settings.set_update_status("发现新版本", action="install")
    settings._about_update_btn.click()
    assert installs == [True]
    settings.set_update_status("正在下载 5 / 10 MB", action="busy")
    assert not settings._about_update_btn.isEnabled()

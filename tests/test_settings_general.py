"""General settings page UI and behavior tests (reference layout)."""

from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QLabel, QMessageBox, QWidget

from sayink.audio_devices import (
    INPUT_SOURCE_MICROPHONE,
    INPUT_SOURCE_MIXED,
    INPUT_SOURCE_SYSTEM,
)
from sayink.config import TRIGGER_MODE_CONTINUOUS, TRIGGER_MODE_HOTKEY, Config
from sayink.ui.design_tokens import (
    ACCENT,
    SURFACE_PEARL,
    TEXT_DIM,
    TEXT_SEC,
)
from sayink.ui.settings_window import SettingsWindow

_REAL_DEVICE_REFRESH = SettingsWindow._refresh_audio_device_lists


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


_INERT_WINDOW_METHODS = ("_rebuild_model_cards", "_refresh_about_info", "_refresh_audio_device_lists")


@pytest.fixture(scope="module")
def _shared_settings(qapp, tmp_path_factory):
    """One SettingsWindow for the whole module (Q-18: building it costs 2–4 s
    per test). Tests get it through ``settings_window`` / ``config`` below,
    which reset the config to defaults and reload the form before each test."""
    # Patches only wrap construction; tests that build their own window with
    # the real methods (e.g. the About paths) must not see them.
    with pytest.MonkeyPatch.context() as mp:
        for name in _INERT_WINDOW_METHODS:
            mp.setattr(SettingsWindow, name, lambda self: None)
        if sys.platform == "win32":
            import winreg

            def _no_entry(*_args, **_kwargs):
                raise FileNotFoundError

            mp.setattr(winreg, "QueryValueEx", _no_entry)
        home = tmp_path_factory.mktemp("settings_general") / ".sayink"
        home.mkdir(parents=True)
        cfg = Config(config_dir=home)
        win = SettingsWindow(cfg)
    yield win, cfg
    win.close()


def _reset_config(cfg: Config) -> None:
    """Back to a fresh-install Config: no file on disk, defaults in memory."""
    cfg._save_timer.stop()
    cfg._config_file.unlink(missing_ok=True)
    cfg._secret_value = None
    cfg._secret_persist_failed = False
    cfg._secret_read_failed = False
    cfg._load()


@pytest.fixture
def config(_shared_settings):
    _win, cfg = _shared_settings
    _reset_config(cfg)
    return cfg


@pytest.fixture
def settings_window(_shared_settings, config, monkeypatch):
    win, _cfg = _shared_settings
    for name in _INERT_WINDOW_METHODS:
        monkeypatch.setattr(SettingsWindow, name, lambda self: None)
    win.hide()
    win._loading = False
    win.set_pending_segment_counter(None)
    win._segmentation_status = None
    win._recognition_pending = False
    win._speaker_runtime_text = "尚未获取说话人增强运行状态"
    win.reload_settings()
    yield win


class TestGeneralPageLayout:
    def test_island_nav_labels(self, settings_window):
        assert isinstance(settings_window, QWidget)
        assert settings_window._pages.count() == 4
        assert not hasattr(settings_window, "_sidebar")
        assert not hasattr(settings_window, "_general_hero")

    def test_general_footer_note_combines_save_and_hotkey_guidance(self, settings_window):
        assert settings_window._general_footer_note.text() == (
            "快捷键与输入法冲突时，可尝试其他组合；修改录音快捷键后自动保存。"
        )
        sheet = settings_window._general_footer_note.styleSheet()
        assert f"color: {TEXT_DIM}" in sheet
        assert f"color: {ACCENT}" not in sheet

    def test_settings_shell_has_no_redundant_action_strip(self, settings_window):
        assert not hasattr(settings_window, "_action_bar")
        assert not hasattr(settings_window, "_done_btn")
        assert not hasattr(settings_window, "_footer_hint")

    def test_general_copy_matches_prototype_v3(self, settings_window):
        """Prototype v3 titles; chrome keeps product action bar."""
        from PyQt6.QtWidgets import QLabel

        assert settings_window._mic_test_btn.text() == "测试声音（约 2 秒）"
        assert settings_window._advanced_audio_btn.text() == "手动选择音频设备"

        # Simple preferences remain terse; history explains what is retained.
        for row, title in (
            (settings_window._auto_start_row, "开机时自动启动"),
            (settings_window._sound_row, "录音提示音"),
            (settings_window._restore_clipboard_row, "粘贴后恢复剪贴板"),
        ):
            texts = [lb.text() for lb in row.findChildren(QLabel) if lb.text()]
            assert texts == [title], texts
        history_text = " ".join(lb.text() for lb in settings_window._history_enabled_row.findChildren(QLabel))
        assert "保存转写历史" in history_text
        assert "不保存音频" in history_text

        footnotes = [
            lb.text()
            for lb in settings_window.findChildren(QLabel)
            if "输入法冲突" in lb.text()
        ]
        assert footnotes
        assert footnotes[0] == (
            "快捷键与输入法冲突时，可尝试其他组合；修改录音快捷键后自动保存。"
        )

    def test_about_version_is_plain_information_without_duplicate_usage_callout(self, settings_window):
        version_sheet = settings_window._about_version_label.styleSheet()
        assert "background: transparent" in version_sheet
        assert "border: none" in version_sheet
        assert f"color: {TEXT_SEC}" in version_sheet
        assert f"color: {ACCENT}" not in version_sheet
        assert settings_window._about_version_label.text().startswith("当前版本 ")
        assert not hasattr(settings_window, "_about_usage_tip")

    def test_settings_stack_contains_only_native_pages(self, settings_window):
        from sayink.ui.settings_components import SettingsPage

        pages = settings_window._pages
        assert pages.count() == 4
        assert all(
            isinstance(pages.widget(index), SettingsPage)
            for index in range(pages.count())
        )
        assert not hasattr(settings_window, "_general_web")
        assert not hasattr(settings_window, "_models_web")
        assert not hasattr(settings_window, "_polish_web")
        assert not hasattr(settings_window, "_about_web")

    def test_general_stacks_three_sections_top_to_bottom(self, settings_window, qapp):
        """Prototype v3: 录音 → 音频 → 偏好 all visible in one scroll page."""
        from sayink.ui.settings_components import (
            AudioSourcePicker,
            CompactPickCard,
            ThemeModeSegment,
            TriggerModePicker,
        )

        settings_window.resize(1000, 900)
        settings_window.show()
        qapp.processEvents()

        titles = {
            label.text(): label
            for label in settings_window.findChildren(QLabel)
            if label.objectName() == "settingsGroupTitle"
        }
        ordered = ["录音与输入", "识别", "会议默认设置", "应用与历史"]
        assert all(name in titles for name in ordered)
        ys = [titles[name].mapToGlobal(titles[name].rect().center()).y() for name in ordered]
        assert ys == sorted(ys)

        trigger = settings_window.findChild(TriggerModePicker)
        audio = settings_window.findChild(AudioSourcePicker)
        assert trigger is not None and trigger.isVisible()
        assert audio is not None and audio.isVisible()
        assert len(trigger.findChildren(CompactPickCard)) == 2
        assert len(audio.findChildren(CompactPickCard)) == 3
        assert settings_window._hotkey_edit.isVisible()
        assert settings_window._mic_test_btn.isVisible()
        assert isinstance(settings_window._theme_combo, ThemeModeSegment)
        assert settings_window._theme_combo.isVisible()
        assert settings_window._auto_start_row.isVisible()
        assert settings_window._history_enabled_row.isVisible()

    def test_general_section_groups_use_bordered_cards(self, settings_window):
        from PyQt6.QtWidgets import QFrame

        from sayink.ui.design_tokens import BORDER, RADIUS_LG, SURFACE

        groups = [
            frame for frame in settings_window.findChildren(QFrame)
            if frame.objectName() == "settingsGroup"
        ]
        assert groups
        for group in groups:
            sheet = group.styleSheet()
            assert f"background: {SURFACE}" in sheet
            assert f"border: 1px solid {BORDER}" in sheet
            assert f"border-radius: {RADIUS_LG}px" in sheet

    def test_settings_content_column_is_transparent_on_sheet(self, settings_window):
        from sayink.ui import design_tokens as tok

        for widget in (
            settings_window._content_wrap,
            settings_window._pages_host,
            settings_window._pages,
        ):
            sheet = widget.styleSheet()
            assert "transparent" in sheet.lower()
            assert f"background: {tok.BG}" not in sheet
        assert not hasattr(settings_window, "_sheet")

    def test_about_feedback_row_is_hidden_for_now(self, settings_window):
        from sayink.ui.settings_pages import about as about_page

        assert about_page.SHOW_FEEDBACK_ENTRY is False
        assert settings_window._about_feedback_group.isHidden()

    def test_about_feedback_button_opens_issue_tracker(self, settings_window, monkeypatch):
        """P-06: the (currently hidden) feedback entry goes to GitHub issues."""
        from sayink.ui.settings_pages import about as about_page
        from sayink.updater import GITHUB_REPO, ISSUES_URL

        opened = []
        monkeypatch.setattr(
            about_page.QDesktopServices, "openUrl", lambda url: opened.append(url.toString()) or True
        )
        btn = settings_window._about_feedback_btn
        assert btn.text() == "GitHub 反馈"
        assert "日志" in settings_window._about_feedback_hint.text()
        btn.click()
        assert opened == [ISSUES_URL]
        assert ISSUES_URL == f"https://github.com/{GITHUB_REPO}/issues/new/choose"
        # Same pill look as 检查更新 so the two actions read as one family.
        assert f"background: {SURFACE_PEARL}" in btn.styleSheet()

    def test_about_paths_start_hidden_and_reveal_on_toggle(
        self, config, qapp, monkeypatch
    ):
        monkeypatch.setattr(SettingsWindow, "_rebuild_model_cards", lambda self: None)
        monkeypatch.setattr(
            SettingsWindow, "_refresh_audio_device_lists", lambda self: None
        )
        win = SettingsWindow(config)
        try:
            win.show_page(3)
            win.show()
            qapp.processEvents()
            assert win._about_paths_wrap.objectName() == "aboutPaths"
            assert not win._about_paths_wrap.isVisible()
            assert win._about_paths_toggle.accessibleName() == "展开文件位置"
            win._about_paths_toggle.click()
            qapp.processEvents()
            assert win._about_paths_wrap.isVisible()
            assert win._about_paths_toggle.accessibleName() == "收起文件位置"
            labels = [lb.text() for lb in win._about_paths_wrap.findChildren(QLabel)]
            assert "模型目录" in labels
            assert "配置文件" in labels
        finally:
            win.close()

    def test_incomplete_llm_fields_write_status_not_messagebox(
        self, settings_window, monkeypatch
    ):
        settings_window._llm_url_edit.clear()
        settings_window._llm_key_edit.clear()
        settings_window._llm_model_edit.clear()
        boxes = []
        monkeypatch.setattr(
            QMessageBox, "warning", staticmethod(lambda *a, **k: boxes.append("warning"))
        )
        monkeypatch.setattr(
            QMessageBox,
            "information",
            staticmethod(lambda *a, **k: boxes.append("information")),
        )
        monkeypatch.setattr(
            QMessageBox,
            "critical",
            staticmethod(lambda *a, **k: boxes.append("critical")),
        )
        settings_window._test_llm()
        assert settings_window._llm_test_status.text() == "请填写完整的接口信息。"
        assert boxes == []

    def test_polish_preview_stays_visible_when_enabled(self, settings_window):
        settings_window._on_llm_enable_toggled(True)

        assert not settings_window._llm_preview_card.isHidden()

    def test_enabling_unconfigured_polish_shows_inline_status(self, settings_window):
        settings_window._llm_url_edit.clear()
        settings_window._llm_key_edit.clear()
        settings_window._llm_model_edit.clear()
        settings_window._llm_enable_row.setChecked(True)
        status = settings_window._llm_test_status.text()
        assert "未配置" in status
        assert "直接输出原文" in status

    def test_password_toggle_updates_its_text(self, settings_window):
        settings_window._llm_key_toggle.setChecked(True)
        assert settings_window._llm_key_toggle.text() == "隐藏"
        settings_window._llm_key_toggle.setChecked(False)
        assert settings_window._llm_key_toggle.text() == "显示"

    def test_audio_source_cards_are_horizontal(self, settings_window):
        from PyQt6.QtWidgets import QHBoxLayout
        from sayink.ui.settings_components import AudioSourcePicker, CompactPickCard

        ap = settings_window.findChild(AudioSourcePicker)
        assert ap is not None
        assert isinstance(ap.layout(), QHBoxLayout)
        assert ap.layout().spacing() == 8
        assert ap.layout().count() == 3
        assert len(ap.findChildren(CompactPickCard)) == 3

    def test_trigger_mode_uses_compact_picks(self, settings_window):
        from PyQt6.QtWidgets import QHBoxLayout
        from sayink.ui.settings_components import CompactPickCard, TriggerModePicker

        picker = settings_window.findChild(TriggerModePicker)
        assert picker is not None
        assert isinstance(picker.layout(), QHBoxLayout)
        assert picker.layout().spacing() == 8
        assert picker.layout().count() == 2
        assert len(picker.findChildren(CompactPickCard)) == 2

        assert settings_window._mixed_audio_callout.parent() is not None

    def test_retention_stays_editable_for_existing_history_when_new_history_is_off(self, settings_window):
        settings_window.show()
        settings_window._history_enabled_row.setChecked(False)
        assert settings_window._history_retention_row.isVisible()
        assert settings_window._history_max_entries_row.isVisible()
        assert settings_window._history_retention_days_spin.isEnabled()
        assert "已停止新增" in settings_window._history_cleanup_hint.text()
        settings_window._history_enabled_row.setChecked(True)
        assert settings_window._history_retention_row.isVisible()

    def test_mixed_callout_only_when_mixed(self, settings_window):
        settings_window.show()
        settings_window._src_mic_rb.setChecked(True)
        settings_window._sync_source_device_widgets()
        assert not settings_window._mixed_audio_callout.isVisible()
        settings_window._src_mixed_rb.setChecked(True)
        settings_window._sync_source_device_widgets()
        assert settings_window._mixed_audio_callout.isVisible()

    def test_hotkey_hint_is_one_sentence(self, settings_window, config):
        # Default (hold-to-talk) hint right after load, not the continuous one.
        text = settings_window._hotkey_hint.text()
        assert "松开后识别" in text and "取消本次录音" in text
        assert "0.18" in settings_window._hotkey_hint.toolTip()
        assert settings_window._esc_stop_row.isHidden()
        assert text.count("。") <= 2
        assert "浮窗" not in text

        config.set("audio.trigger_mode", TRIGGER_MODE_CONTINUOUS)
        settings_window.reload_settings()
        text = settings_window._hotkey_hint.text()
        assert "已录内容继续识别" in text
        assert not settings_window._esc_stop_row.isHidden()
        assert text.count("。") <= 2
        assert "浮窗" not in text


class TestTriggerMode:
    def test_default_is_hold_to_talk(self, settings_window):
        """P-01: dictation is the product; hold-to-talk is the default."""
        assert settings_window._trigger_hotkey_rb.isChecked()
        assert not settings_window._trigger_continuous_rb.isChecked()
        assert settings_window._selected_trigger_mode() == TRIGGER_MODE_HOTKEY

    def test_continuous_mode_mutually_exclusive(self, settings_window, monkeypatch):
        persisted = []
        monkeypatch.setattr(
            settings_window,
            "_confirm_discard_pending",
            lambda: True,
        )
        monkeypatch.setattr(
            settings_window,
            "_persist_runtime_settings",
            lambda: persisted.append(settings_window._selected_trigger_mode()),
        )
        settings_window._trigger_continuous_rb.setChecked(True)
        assert settings_window._trigger_continuous_rb.isChecked()
        assert not settings_window._trigger_hotkey_rb.isChecked()
        assert persisted == [TRIGGER_MODE_CONTINUOUS]


class TestOutputMode:
    """P-01: meeting capture goes to history, not into the front window."""

    def test_default_pastes(self, settings_window, config):
        assert config.history_only_output() is False
        assert settings_window._output_mode_combo.currentData() == "auto"
        assert "输入当前光标" in settings_window._output_mode_hint.text()

    def test_system_or_mixed_source_switches_to_history_only(self, settings_window, config, monkeypatch):
        monkeypatch.setattr(settings_window, "_confirm_discard_pending", lambda: True)
        settings_window._src_sys_rb.setChecked(True)
        assert config.history_only_output() is True
        assert settings_window._output_mode_combo.currentData() == "auto"
        assert "仅保存到历史" in settings_window._output_mode_hint.text()
        assert not settings_window._mixed_audio_callout.isHidden()

    def test_back_to_microphone_restores_paste_only_if_app_chose_it(self, settings_window, config, monkeypatch):
        monkeypatch.setattr(settings_window, "_confirm_discard_pending", lambda: True)
        settings_window._src_mixed_rb.setChecked(True)
        assert config.history_only_output() is True
        settings_window._src_mic_rb.setChecked(True)
        assert config.history_only_output() is False
        assert settings_window._output_mode_combo.currentData() == "auto"

        # The user turns history-only on by hand: the source no longer touches it.
        combo = settings_window._output_mode_combo
        combo.setCurrentIndex(combo.findData("history"))
        assert config.get("output.mode_auto") is False
        settings_window._src_mixed_rb.setChecked(True)
        settings_window._src_mic_rb.setChecked(True)
        assert config.history_only_output() is True

    def test_user_can_switch_back_to_paste_while_on_mixed(self, settings_window, config, monkeypatch):
        monkeypatch.setattr(settings_window, "_confirm_discard_pending", lambda: True)
        settings_window._src_mixed_rb.setChecked(True)
        combo = settings_window._output_mode_combo
        combo.setCurrentIndex(combo.findData("paste"))
        assert config.history_only_output() is False
        assert config.get("audio.input_source") == INPUT_SOURCE_MIXED

    def test_explicit_paste_survives_later_source_changes_and_reload(self, settings_window, config, monkeypatch):
        monkeypatch.setattr(settings_window, "_confirm_discard_pending", lambda: True)
        combo = settings_window._output_mode_combo
        combo.setCurrentIndex(combo.findData("paste"))
        for radio in (settings_window._src_sys_rb, settings_window._src_mic_rb, settings_window._src_mixed_rb):
            radio.setChecked(True)
            assert config.history_only_output() is False
        settings_window.reload_settings()
        assert combo.currentData() == "paste"
        assert "保持不变" in settings_window._output_mode_hint.text()

    def test_automatic_choice_resolves_immediately_for_current_source(self, settings_window, config):
        config.set_output_mode("paste")
        config.set("audio.input_source", "system")
        settings_window.reload_settings()
        combo = settings_window._output_mode_combo
        combo.setCurrentIndex(combo.findData("auto"))
        assert config.history_only_output()
        assert config.output_follows_input_source()

    def test_missing_history_has_local_recovery_and_clipboard_dependency(self, settings_window, config):
        combo = settings_window._output_mode_combo
        combo.setCurrentIndex(combo.findData("history"))
        settings_window._history_enabled_row.setChecked(False)
        assert "不会保存" in settings_window._output_mode_hint.text()
        assert not settings_window._enable_history_wrap.isHidden()
        assert not settings_window._restore_clipboard_row.isEnabled()
        settings_window._enable_history_btn.click()
        assert config.get("history.enabled")
        assert settings_window._enable_history_wrap.isHidden()
        combo.setCurrentIndex(combo.findData("paste"))
        assert settings_window._restore_clipboard_row.isEnabled()


class TestInputSource:
    def test_source_selection_persists(self, settings_window, config, monkeypatch):
        monkeypatch.setattr(settings_window, "_confirm_discard_pending", lambda: True)
        settings_window._src_sys_rb.setChecked(True)
        assert config.get("audio.input_source") == INPUT_SOURCE_SYSTEM

    def test_mixed_warning_stays_visible(self, settings_window, qapp):
        settings_window.show()
        qapp.processEvents()
        settings_window._src_mic_rb.setChecked(True)
        settings_window._sync_source_device_widgets()
        assert settings_window._mixed_audio_callout.isHidden()
        settings_window._src_mixed_rb.setChecked(True)
        settings_window._sync_source_device_widgets()
        assert not settings_window._mixed_audio_callout.isHidden()

    def test_revert_on_cancel_pending(self, settings_window, config, monkeypatch):
        config.set("audio.input_source", INPUT_SOURCE_MICROPHONE)
        monkeypatch.setattr(settings_window, "_confirm_discard_pending", lambda: False)
        settings_window._loading = False
        settings_window._src_mixed_rb.setChecked(True)
        assert settings_window._src_mic_rb.isChecked()
        assert config.get("audio.input_source") == INPUT_SOURCE_MICROPHONE

    def test_reset_to_auto_asks_before_dropping_queued_speech(
            self, settings_window, config, monkeypatch):
        persisted = []
        monkeypatch.setattr(settings_window, "_persist_runtime_settings",
                            lambda: persisted.append(True))
        monkeypatch.setattr(settings_window, "_confirm_discard_pending", lambda: False)
        settings_window._loading = False
        settings_window._reset_audio_devices_to_auto()
        assert persisted == []
        monkeypatch.setattr(settings_window, "_confirm_discard_pending", lambda: True)
        settings_window._reset_audio_devices_to_auto()
        assert persisted == [True]


class TestToggles:
    def test_toggle_rows_render_title_and_positive_height(self, settings_window, qapp):
        """QCheckBox-based rows collapse to 0 height; content must stay visible."""
        from PyQt6.QtWidgets import QLabel

        settings_window.resize(1000, 900)
        settings_window.show()
        qapp.processEvents()

        for row, title in (
            (settings_window._auto_start_row, "开机时自动启动"),
            (settings_window._sound_row, "录音提示音"),
            (settings_window._history_enabled_row, "保存转写历史"),
        ):
            assert row.height() >= 40, f"{title} row height={row.height()}"
            labels = [lb for lb in row.findChildren(QLabel) if lb.text() == title]
            assert labels, f"missing title label: {title}"
            assert labels[0].height() >= 14, f"{title} label height={labels[0].height()}"

    def test_row_click_toggles_from_anywhere(self, settings_window, qapp):
        from PyQt6.QtTest import QTest

        settings_window.resize(860, 620)
        settings_window.show()
        qapp.processEvents()

        row = settings_window._sound_row
        initial = row.isChecked()
        QTest.mouseClick(row, Qt.MouseButton.LeftButton, pos=row.rect().center())
        assert row.isChecked() is not initial

    def test_auto_start_emits_and_persists(self, settings_window, config):
        signals = []
        settings_window.auto_start_changed.connect(signals.append)
        settings_window._auto_start_row.setChecked(True)
        assert config.get("auto_start") is True
        assert signals == [True]

    def test_sound_toggle_persists(self, settings_window, config):
        signals = []
        settings_window.sound_enabled_changed.connect(signals.append)
        settings_window._sound_row.setChecked(False)
        assert config.get("sound_enabled") is False
        assert signals == [False]

    def test_restore_clipboard_toggle_emits_runtime_update(self, settings_window, config):
        signals = []
        settings_window.restore_clipboard_changed.connect(signals.append)
        settings_window._restore_clipboard_row.setChecked(True)
        assert config.get("output.restore_clipboard") is True
        assert signals == [True]


class TestConfirmDialogs:
    """U-02: confirmations show Chinese buttons, not Qt's English Yes/No."""

    def _fake_box(self, monkeypatch, events, *, click: str):
        from sayink.ui import settings_window as module

        class FakeBox:
            def __init__(self, parent=None):
                events["parent"] = parent
                self._buttons: list[tuple[str, object]] = []

            def setWindowTitle(self, title):
                events["title"] = title

            def setText(self, text):
                events["text"] = text

            def setIcon(self, icon):
                pass

            def addButton(self, text, role):
                button = object()
                self._buttons.append((text, button))
                events.setdefault("buttons", []).append((text, role))
                return button

            def setDefaultButton(self, button):
                events["default"] = [t for t, b in self._buttons if b is button][0]

            def setEscapeButton(self, button):
                events["escape"] = [t for t, b in self._buttons if b is button][0]

            def exec(self):
                return 0

            def clickedButton(self):
                return [b for t, b in self._buttons if t == click][0]

        FakeBox.Icon = QMessageBox.Icon
        FakeBox.ButtonRole = QMessageBox.ButtonRole
        monkeypatch.setattr(module, "QMessageBox", FakeBox)

    def test_delete_model_asks_in_chinese_and_defaults_to_cancel(self, settings_window, monkeypatch):
        events: dict = {}
        self._fake_box(monkeypatch, events, click="取消")
        deleted = []
        monkeypatch.setattr("sayink.speech_recognizer.delete_model", lambda mid: deleted.append(mid) or True)
        settings_window._delete_model("sense-voice-small")

        assert [t for t, _r in events["buttons"]] == ["删除", "取消"]
        assert events["buttons"][0][1] == QMessageBox.ButtonRole.DestructiveRole
        assert events["default"] == events["escape"] == "取消"
        assert events["parent"] is settings_window
        assert deleted == []

    def test_discard_pending_confirms_only_on_accept(self, settings_window, monkeypatch):
        settings_window.set_pending_segment_counter(lambda: 2)
        events: dict = {}
        self._fake_box(monkeypatch, events, click="丢弃并应用")
        assert settings_window._confirm_discard_pending() is True
        assert "2 段语音" in events["text"]
        self._fake_box(monkeypatch, events, click="取消")
        assert settings_window._confirm_discard_pending() is False


class TestHotkeyBinding:
    def test_rejects_modifierless_hotkey(self, settings_window, config, monkeypatch):
        warnings = []
        monkeypatch.setattr(
            QMessageBox,
            "warning",
            lambda *args, **kwargs: warnings.append(args[2]),
        )
        settings_window._apply_hotkey_setting("space")
        assert warnings
        assert config.get("hotkey") != "space"

    def test_accepts_valid_hotkey(self, settings_window, config):
        emitted = []
        settings_window.hotkey_updated.connect(emitted.append)
        settings_window._apply_hotkey_setting("ctrl+shift+a")
        assert config.get("hotkey") == "ctrl+shift+a"
        assert emitted == ["ctrl+shift+a"]

    def test_escape_cancels_capture_instead_of_binding_esc(self, settings_window, config):
        """Esc already cancels recording / ends listening in HotKeyManager, so a
        hotkey containing Esc could never fire. The capture box used to bind it."""
        from PyQt6.QtTest import QTest

        edit = settings_window._hotkey_edit
        before = config.get("hotkey")
        ended = []
        edit.capture_ended.connect(lambda: ended.append(1))
        edit._begin_capture()
        assert edit._capturing is True
        QTest.keyClick(edit, Qt.Key.Key_Escape, Qt.KeyboardModifier.ShiftModifier)
        assert edit._capturing is False
        assert ended == [1]
        assert config.get("hotkey") == before
        assert edit.text() == settings_window._hotkey_edit.text()
        assert "Esc" not in edit.text()

    def test_function_key_capture_round_trips_through_the_parser(self, settings_window, config):
        from PyQt6.QtTest import QTest
        from pynput import keyboard

        from sayink.hotkey_manager import parse_hotkey

        edit = settings_window._hotkey_edit
        edit._begin_capture()
        QTest.keyClick(edit, Qt.Key.Key_F3, Qt.KeyboardModifier.ControlModifier)
        assert config.get("hotkey") == "ctrl+f3"
        assert parse_hotkey(config.get("hotkey")) == {keyboard.Key.ctrl_l, keyboard.Key.f3}


class TestAdvancedDevices:
    def test_device_refresh_preserves_selection_without_applying_transient_entries(self, settings_window, config, monkeypatch):
        from types import SimpleNamespace

        win = settings_window
        monkeypatch.setattr("sayink.ui.settings_window.list_microphone_devices",
                            lambda: [SimpleNamespace(index=0, label="USB 麦克风")])
        monkeypatch.setattr("sayink.ui.settings_window.list_system_capture_devices_for_settings", list)
        win._loading = True
        win._mic_device_combo.clear()
        win._mic_device_combo.addItem("自动选择", -1)
        win._mic_device_combo.addItem("USB 麦克风", 0)
        win._mic_device_combo.setCurrentIndex(1)
        config.set("audio.mic_device_index", 0)
        win._loading = False
        applied = []
        win.settings_changed.connect(lambda: applied.append(True))
        _REAL_DEVICE_REFRESH(win)
        assert win._mic_device_combo.currentData() == 0
        assert config.get("audio.mic_device_index") == 0
        assert applied == []
        monkeypatch.setattr("sayink.ui.settings_window.list_microphone_devices", list)
        _REAL_DEVICE_REFRESH(win)
        assert win._mic_device_combo.currentData() == 0
        assert "不可用" in win._audio_device_summary.text()
        assert applied == []

    def test_advanced_recognition_expands_from_keyboard_and_pending_stays_visible(self, settings_window, qapp):
        from PyQt6.QtTest import QTest
        from sayink.runtime_status import SegmentationStatus

        settings_window.show()
        button = settings_window._advanced_recognition_btn
        button.setChecked(False)
        assert settings_window._advanced_recognition_panel.isHidden()
        button.setFocus()
        QTest.keyClick(button, Qt.Key.Key_Space)
        qapp.processEvents()
        assert settings_window._advanced_recognition_panel.isVisible()
        QTest.keyClick(button, Qt.Key.Key_Space)
        settings_window.set_recognition_runtime_status(
            SegmentationStatus("silero", "energy", False), speaker_status="待加载", pending=True,
        )
        assert settings_window._advanced_recognition_panel.isHidden()
        assert settings_window._recognition_apply_label.isVisible()
        assert "已保存" in settings_window._recognition_apply_label.text()
        assert "待本场结束" in settings_window._recognition_apply_label.text()

    def test_first_device_index_zero_is_kept(self, settings_window, config):
        combo = settings_window._mic_device_combo
        settings_window._loading = True
        combo.clear()
        combo.addItem("自动选择", -1)
        combo.addItem("麦克风 (设备 0)", 0)
        combo.setCurrentIndex(1)
        settings_window._loading = False

        settings_window._persist_runtime_settings()

        assert config.get("audio.mic_device_index") == 0

    def test_automatic_device_still_saves_minus_one(self, settings_window, config):
        combo = settings_window._mic_device_combo
        settings_window._loading = True
        combo.clear()
        combo.addItem("自动选择", -1)
        combo.setCurrentIndex(0)
        settings_window._loading = False

        settings_window._persist_runtime_settings()

        assert config.get("audio.mic_device_index") == -1

    def test_device_link_expands_panel(self, settings_window):
        assert settings_window._advanced_audio_panel.isHidden()
        settings_window._toggle_advanced_audio(True)
        assert not settings_window._advanced_audio_panel.isHidden()
        assert "收起" in settings_window._advanced_audio_btn.text()


class TestPolishConfigurationStatus:
    def _fill(self, win, url, key, model):
        win._llm_enable_row.setChecked(True)
        win._llm_url_edit.setText(url)
        win._llm_key_edit.setText(key)
        win._llm_model_edit.setText(model)
        win._update_llm_configuration_status()

    def test_local_endpoint_without_key_is_complete(self, settings_window):
        self._fill(settings_window, "http://localhost:11434/v1", "", "qwen2.5")
        assert settings_window._llm_missing_fields() == []
        assert "未配置" not in settings_window._llm_test_status.text()

    def test_remote_endpoint_without_key_reports_missing_key(self, settings_window):
        self._fill(settings_window, "https://api.deepseek.com/v1", "", "deepseek-chat")
        assert settings_window._llm_missing_fields() == ["API 密钥"]

    def test_missing_fields_reported_with_polish_off(self, settings_window):
        settings_window._llm_enable_row.setChecked(False)
        settings_window._llm_url_edit.clear()
        settings_window._llm_key_edit.clear()
        settings_window._llm_model_edit.clear()
        settings_window._update_llm_configuration_status()
        text = settings_window._llm_test_status.text()
        assert "接口地址" in text and "会议纪要" in text

    def test_insecure_remote_url_rejected_with_polish_off(self, settings_window, config, monkeypatch):
        warnings = []
        monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: warnings.append(a)))
        settings_window._llm_enable_row.setChecked(False)
        settings_window._llm_url_edit.setText("http://api.example.com/v1")
        settings_window._flush_llm_fields()
        assert warnings
        assert config.get("llm.api_url", "") != "http://api.example.com/v1"

    def test_minutes_model_test_uses_shared_endpoint(self, settings_window, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "sayink.text_polisher.TextPolisher.test_connection",
            staticmethod(lambda u, k, m: (calls.append((u, k, m)), (True, ""))[1]),
        )
        settings_window._llm_url_edit.setText("https://api.deepseek.com/v1")
        settings_window._llm_key_edit.setText("sk-x")
        settings_window._llm_model_edit.setText("deepseek-chat")
        settings_window._minutes_model_edit.setText("deepseek-reasoner")
        settings_window._test_minutes_model()
        settings_window._minutes_test_worker.wait(3000)
        QApplication.processEvents()
        assert calls == [("https://api.deepseek.com/v1", "sk-x", "deepseek-reasoner")]
        assert "deepseek-reasoner" in settings_window._minutes_test_status.text()

    def test_unsaved_credential_is_reported(self, settings_window, config):
        config._secret_persist_failed = True
        self._fill(settings_window, "https://api.deepseek.com/v1", "sk-x", "deepseek-chat")
        text = settings_window._llm_test_status.text()
        assert "凭据管理器" in text and "重启后需重新填写" in text


class TestMicProbe:
    def test_source_change_discards_a_previous_test_conclusion(self, settings_window):
        settings_window._set_mic_test_status("已检测到声音。")
        settings_window._src_sys_rb.setChecked(True)
        assert "已检测到声音" not in settings_window._mic_test_status.text()
        assert "约 2 秒" in settings_window._mic_test_status.text()

    def test_probe_runs_two_second_flow(self, settings_window, monkeypatch):
        monkeypatch.setattr(settings_window, "_refresh_audio_device_lists", lambda: None)
        recorder = settings_window._mic_test_recorder
        recorder.configure = MagicMock()
        recorder.start = MagicMock()
        recorder.stop = MagicMock()

        settings_window._run_mic_probe()
        assert settings_window._mic_probe_active
        assert not settings_window._mic_test_btn.isEnabled()
        recorder.configure.assert_called_once()
        recorder.start.assert_called_once()

        settings_window._on_mic_probe_volume(0.01)
        settings_window._finish_mic_probe()
        assert not settings_window._mic_probe_active
        assert settings_window._mic_test_btn.isEnabled()
        assert "已检测到声音" in settings_window._mic_test_status.text()
        assert "可以正常使用" not in settings_window._mic_test_status.text()
        assert "峰值" not in settings_window._mic_test_status.text()

    def test_probe_failure_restores_controls_and_cancels_its_timer(self, settings_window, monkeypatch):
        monkeypatch.setattr(settings_window._mic_test_recorder, "start", lambda: None)
        settings_window._run_mic_probe()
        assert settings_window._mic_probe_timer.isActive()
        assert not settings_window._src_sys_rb.parentWidget().isEnabled()
        settings_window._on_mic_probe_error("设备不可用")
        assert not settings_window._mic_probe_timer.isActive()
        assert settings_window._mic_level_bar.isHidden()
        assert settings_window._src_sys_rb.parentWidget().isEnabled()
        assert "设备不可用" in settings_window._mic_test_status.text()
        settings_window._finish_mic_probe()
        assert "测试失败" in settings_window._mic_test_status.text()

    def test_quiet_mixed_input_keeps_fallback_warning(self, settings_window, config, monkeypatch):
        monkeypatch.setattr(settings_window._mic_test_recorder, "start", lambda: None)
        config.set("audio.input_source", "mixed")
        settings_window.reload_settings()
        settings_window._run_mic_probe()
        settings_window._on_mic_probe_volume(0.003)
        assert settings_window._mic_level_bar.value() > 0
        settings_window._on_mic_probe_warning("电脑声音不可用，本次仅麦克风")
        settings_window._finish_mic_probe()
        message = settings_window._mic_test_status.text()
        assert "音量偏低" in message
        assert "分别" in message
        assert "本次仅麦克风" in message

    def test_no_input_provides_next_steps(self, settings_window, monkeypatch):
        monkeypatch.setattr(settings_window._mic_test_recorder, "start", lambda: None)
        settings_window._run_mic_probe()
        settings_window._finish_mic_probe()
        assert "未检测到" in settings_window._mic_test_status.text()
        assert "手动选择设备" in settings_window._mic_test_status.text()


class TestNavigation:
    def test_back_closes_window(self, settings_window, qapp):
        closed = []
        settings_window.finished.connect(lambda _: closed.append(True))
        settings_window._on_done()
        assert settings_window.isVisible() is False or closed

    def test_done_closes_window(self, settings_window):
        settings_window.show()
        settings_window._on_done()
        assert not settings_window.isVisible()


class TestConfigReload:
    def test_reload_restores_ui_from_disk(self, config_home, qapp, monkeypatch):
        cfg_file = config_home / "config.json"
        cfg_file.write_text(
            json.dumps(
                {
                    "hotkey": "ctrl+space",
                    "auto_start": True,
                    "sound_enabled": False,
                    "audio": {
                        "input_source": "mixed",
                        "trigger_mode": "hotkey",
                        "mic_device_index": -1,
                        "system_device_index": -1,
                    },
                }
            ),
            encoding="utf-8",
        )
        config = Config(config_dir=config_home)
        monkeypatch.setattr(SettingsWindow, "_rebuild_model_cards", lambda self: None)
        monkeypatch.setattr(SettingsWindow, "_refresh_about_info", lambda self: None)
        monkeypatch.setattr(SettingsWindow, "_refresh_audio_device_lists", lambda self: None)
        win = SettingsWindow(config)
        assert win._auto_start_row.isChecked()
        assert not win._sound_row.isChecked()
        assert win._src_mixed_rb.isChecked()
        assert win._trigger_hotkey_rb.isChecked()
        win.close()


class TestModelDownloadDone:
    """Only the active model is loaded into memory after a download; the
    completion dialog must not promise that for a second, inactive model."""

    @pytest.fixture
    def done_env(self, settings_window, config, monkeypatch):
        shown = []
        monkeypatch.setattr(
            QMessageBox, "information", lambda *args, **kwargs: shown.append(args[2])
        )
        monkeypatch.setattr(
            "sayink.speech_recognizer.get_model_info",
            lambda model_id: {"name": f"模型 {model_id}"},
        )
        monkeypatch.setattr("sayink.speech_recognizer.is_model_downloaded", lambda _mid: False)
        return shown

    def test_second_model_download_says_it_is_not_in_use(self, settings_window, config, done_env, monkeypatch):
        monkeypatch.setattr(
            "sayink.speech_recognizer.get_downloaded_models", lambda: ["active", "other"]
        )
        config.set("stt.model_id", "active")
        settings_window._on_dl_done("other")
        assert config.get("stt.model_id") == "active"
        assert len(done_env) == 1
        assert "载入内存" not in done_env[0]
        assert "使用此模型" in done_env[0]

    def test_only_model_download_becomes_active_and_loads(self, settings_window, config, done_env, monkeypatch):
        monkeypatch.setattr(
            "sayink.speech_recognizer.get_downloaded_models", lambda: ["first"]
        )
        settings_window._on_dl_done("first")
        assert config.get("stt.model_id") == "first"
        assert "载入内存" in done_env[0]

    def test_model_waiting_for_current_session_does_not_claim_it_is_loading(self, settings_window, config, done_env, monkeypatch):
        monkeypatch.setattr("sayink.speech_recognizer.get_downloaded_models", lambda: ["first"])
        settings_window._recognition_pending = True
        settings_window._on_dl_done("first")
        assert "待本场结束" in done_env[0]
        assert "正在载入内存" not in done_env[0]


class TestMeetingSettings:
    """公共识别 / 会议默认设置 / 会议纪要 round-trip through config.

    Glossary / language / VAD are shared by dictation and meetings, so they
    live under ``stt.*``; only the meeting-specific choices stay in ``meeting.*``.
    """

    def test_meeting_defaults_load(self, settings_window, config):
        assert settings_window._meeting_source_combo.currentData() == ""
        assert settings_window._meeting_model_combo.currentData() == ""
        assert settings_window._meeting_vad_combo.currentData() == "silero"
        assert settings_window._meeting_language_combo.currentData() == "auto"
        assert settings_window._meeting_start_card_row.isChecked()
        assert settings_window._meeting_glossary_edit.toPlainText() == ""
        assert settings_window._minutes_template_combo.currentData() == "general"

    def test_meeting_choices_persist(self, settings_window, config):
        win = settings_window
        win._meeting_source_combo.setCurrentIndex(win._meeting_source_combo.findData("microphone"))
        win._meeting_vad_combo.setCurrentIndex(win._meeting_vad_combo.findData("energy"))
        win._meeting_language_combo.setCurrentIndex(win._meeting_language_combo.findData("en"))
        win._meeting_start_card_row.setChecked(False)
        assert config.get("meeting.input_source") == "microphone"
        assert config.get("stt.vad") == "energy"
        assert config.get("stt.language") == "en"
        assert config.get("meeting.show_start_card") is False
        assert config.get("meeting.vad") is None and config.get("meeting.language") is None

    def test_recognition_changes_emit_shared_signal(self, settings_window, config):
        win = settings_window
        fired = []
        win.recognition_changed.connect(lambda: fired.append(True))
        win._meeting_vad_combo.setCurrentIndex(win._meeting_vad_combo.findData("energy"))
        win._meeting_language_combo.setCurrentIndex(win._meeting_language_combo.findData("en"))
        win._meeting_glossary_edit.setPlainText("SayInk")
        win._flush_meeting_glossary()
        assert len(fired) == 3
        # Meeting-only defaults never reload the shared recognizer.
        win._meeting_source_combo.setCurrentIndex(win._meeting_source_combo.findData("microphone"))
        win._flush_meeting_glossary()
        assert len(fired) == 3

    def test_speaker_enhancement_round_trips_through_shared_settings(self, settings_window, config):
        settings_window._speaker_enabled_row.setChecked(False)
        assert config.get("stt.speaker_model_enabled") is False
        settings_window.reload_settings()
        assert not settings_window._speaker_enabled_row.isChecked()

    def test_actual_vad_failure_and_pending_settings_are_visible(self, settings_window, config, monkeypatch):
        from sayink.runtime_status import SegmentationStatus

        monkeypatch.setattr("sayink.speech_recognizer.is_model_downloaded", lambda _mid: True)
        settings_window.set_recognition_runtime_status(
            SegmentationStatus("silero", "energy", True, "bad onnx", True),
            speaker_status="CAM++ 已加载", pending=True,
        )
        assert "当前会议" in settings_window._vad_runtime_label.text()
        assert "加载失败" in settings_window._vad_runtime_label.text()
        assert "bad onnx" == settings_window._vad_runtime_label.toolTip()
        assert "待本场结束" in settings_window._recognition_apply_label.text()
        assert "CAM++ 已加载" in settings_window._speaker_runtime_label.text()

    def test_downloaded_alone_is_not_reported_as_loaded(self, settings_window, monkeypatch):
        monkeypatch.setattr("sayink.speech_recognizer.is_model_downloaded", lambda _mid: True)
        settings_window._segmentation_status = None
        settings_window._refresh_recognition_feedback()
        text = settings_window._vad_runtime_label.text()
        assert "已下载" in text and "尚未确认加载状态" in text
        assert "已加载" not in text

    def test_download_during_capture_does_not_claim_a_load_failure(self, settings_window, monkeypatch):
        from sayink.runtime_status import SegmentationStatus

        monkeypatch.setattr("sayink.speech_recognizer.is_model_downloaded", lambda _mid: True)
        settings_window.set_recognition_runtime_status(
            SegmentationStatus("silero", "energy", False), speaker_status="待加载", pending=True,
        )
        assert "待本场结束后加载" in settings_window._vad_runtime_label.text()
        assert "加载失败" not in settings_window._vad_runtime_label.text()

    def test_glossary_lines_become_terms_capped_and_deduped(self, settings_window, config):
        win = settings_window
        lines = ["SayInk", "  ", "SayInk", "FireRedASR2", "x" * 30] + [f"t{i}" for i in range(60)]
        win._meeting_glossary_edit.setPlainText("\n".join(lines))
        win._flush_meeting_glossary()
        terms = [g["term"] for g in config.get("stt.glossary")]
        assert terms[:3] == ["SayInk", "FireRedASR2", "x" * 20]
        assert len(terms) == 50
        win.reload_settings()
        assert win._meeting_glossary_edit.toPlainText().splitlines()[:2] == ["SayInk", "FireRedASR2"]

    def test_minutes_fields_persist(self, settings_window, config):
        win = settings_window
        win._minutes_model_edit.setText("  big-model ")
        win._minutes_template_combo.setCurrentIndex(win._minutes_template_combo.findData("standup"))
        win._minutes_language_combo.setCurrentIndex(win._minutes_language_combo.findData("zh"))
        win._flush_minutes_fields()
        assert config.get("meeting.model_name") == "big-model"
        assert config.get("meeting.template_id") == "standup"
        assert config.get("meeting.output_language") == "zh"

    def test_aux_model_download_message_does_not_touch_dictation_model(self, settings_window, config, monkeypatch):
        shown = []
        monkeypatch.setattr(QMessageBox, "information", lambda *args, **kwargs: shown.append(args[2]))
        monkeypatch.setattr("sayink.speech_recognizer.get_downloaded_models", list)
        config.set("stt.model_id", "funasr-nano")
        settings_window._on_dl_done("speaker-campplus")
        assert config.get("stt.model_id") == "funasr-nano"
        assert "通用 → 识别 → 高级识别设置" in shown[0]

    def test_cancel_all_downloads_cancels_before_waiting(self, settings_window):
        events: list[str] = []

        class _FakeDl:
            def isRunning(self):
                return True

            def cancel(self):
                events.append("cancel")

            def wait(self, ms):
                events.append(f"wait:{ms}")

        settings_window._dl_workers["funasr-nano"] = _FakeDl()
        settings_window.cancel_all_downloads()
        assert events == ["cancel", "wait:3000"]
        assert settings_window._dl_workers == {}

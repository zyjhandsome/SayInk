"""Comprehensive regression tests for Swarm UI Foundation upgrades."""

from __future__ import annotations

import ctypes
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest
from PyQt6.QtCore import QPoint, QRect, Qt
from PyQt6.QtGui import QColor
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import (
    QDialog,
    QLabel,
    QMessageBox,
)

from sayink.audio_recovery import RecordingJournal
from sayink.config import Config
from sayink.onboarding import OnboardingController, _SetupDialog
from sayink.ui import design_tokens as tok
from sayink.ui import settings_styles as ss
from sayink.ui.audio_recovery_dialog import AudioRecoveryDialog
from sayink.ui.floating_window import COMPACT_HEIGHT, FloatingWindow
from sayink.ui.media_import_dialog import MediaImportDialog
from sayink.ui.prompts import alert_error, confirm_box, style_message_box
from sayink.ui.settings_components import ThemedCheckBox, _qcolor
from sayink.ui.settings_pages.general import GeneralSettingsPage
from sayink.ui.settings_pages.model import ModelSettingsPage
from sayink.ui.theme import (
    ThemeAware,
    apply_theme,
    apply_window_dark_titlebar,
)
from sayink.ui.transcript_import_dialog import TranscriptImportDialog


def _rel_lum(hex_or_token: str) -> float:
    c = _qcolor(hex_or_token)
    channels = []
    for raw in (c.redF(), c.greenF(), c.blueF()):
        channels.append(raw / 12.92 if raw <= 0.04045 else ((raw + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _contrast(fg: str, bg: str) -> float:
    l1, l2 = _rel_lum(fg), _rel_lum(bg)
    hi, lo = (l1, l2) if l1 >= l2 else (l2, l1)
    return (hi + 0.05) / (lo + 0.05)


def _composite_hex(rgba_token: str, bg_hex: str) -> str:
    fg = _qcolor(rgba_token)
    bg = _qcolor(bg_hex)
    a = fg.alphaF()
    r = round(fg.red() * a + bg.red() * (1.0 - a))
    g = round(fg.green() * a + bg.green() * (1.0 - a))
    b = round(fg.blue() * a + bg.blue() * (1.0 - a))
    return f"#{r:02X}{g:02X}{b:02X}"


class TestWCAGNonTextControlContrast:
    @pytest.mark.parametrize("axis", ["light", "dark"])
    def test_control_border_and_toggle_off_pass_wcag_1_4_11_on_bg_and_pearl(self, axis: str):
        palette = tok.tokens_for(axis)
        for token_name in (
            "CONTROL_BORDER",
            "CONTROL_BORDER_HOVER",
            "TOGGLE_OFF",
            "TOGGLE_OFF_HOVER",
        ):
            fg = palette[token_name]
            for surface_name in ("BG", "SURFACE_PEARL", "INPUT_BG", "NAV_BG"):
                surface = palette[surface_name]
                ratio = _contrast(fg, surface)
                assert ratio >= 3.05, (
                    f"{axis}.{token_name} ({fg}) vs {surface_name} ({surface}) = {ratio:.3f}:1 (< 3.05:1)"
                )

        # Switch white knob (#FFFFFF) also maintains >= 3.05:1 against off-track tokens.
        for track_token in ("TOGGLE_OFF", "TOGGLE_OFF_HOVER"):
            knob_ratio = _contrast("#FFFFFF", palette[track_token])
            assert knob_ratio >= 3.05, (
                f"{axis}.{track_token} ({palette[track_token]}) vs #FFFFFF knob = {knob_ratio:.3f}:1"
            )

    def test_dark_nav_selected_and_row_hover_are_distinct(self):
        dark = tok.tokens_for("dark")
        sel_hex = _composite_hex(dark["NAV_SELECTED_BG"], dark["NAV_BG"])
        hov_hex = _composite_hex(dark["ROW_HOVER"], dark["NAV_BG"])
        sel_c, hov_c = QColor(sel_hex), QColor(hov_hex)
        channel_delta = abs(sel_c.red() - hov_c.red()) + abs(sel_c.green() - hov_c.green()) + abs(sel_c.blue() - hov_c.blue())
        assert channel_delta >= 36, f"Expected distinct dark nav wash vs hover, got {sel_hex} vs {hov_hex}"

    def test_master_doc_tracks_upgraded_tokens(self):
        master = (Path(__file__).resolve().parents[1] / "design-system" / "MASTER.md").read_text(encoding="utf-8")
        assert tok.tokens_for("light")["CONTROL_BORDER"] in master
        assert tok.tokens_for("dark")["CONTROL_BORDER"] in master
        assert "280px" in master


class TestWin32DarkTitlebarAndLocalizedPrompts:
    def test_apply_window_dark_titlebar_noop_in_offscreen_and_sets_dwm_when_live(
        self, qapp, monkeypatch
    ):
        dlg = QDialog()
        try:
            # Default test environment uses QT_QPA_PLATFORM=offscreen -> safe no-op
            monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
            assert apply_window_dark_titlebar(dlg, True) is False

            # Simulate live Windows desktop surface and capture DwmSetWindowAttribute calls
            calls: list[tuple[int, int, int]] = []

            def fake_dwm_set(hwnd, attr, val_ptr, byte_len):
                val = ctypes.cast(val_ptr, ctypes.POINTER(ctypes.c_int)).contents.value
                calls.append((int(hwnd), int(attr), int(val)))
                return 0

            fake_dwmapi = MagicMock()
            fake_dwmapi.DwmSetWindowAttribute = fake_dwm_set
            monkeypatch.setattr("sys.platform", "win32")
            monkeypatch.setenv("QT_QPA_PLATFORM", "windows")
            monkeypatch.setattr(ctypes, "windll", MagicMock(dwmapi=fake_dwmapi), raising=False)
            monkeypatch.setattr(dlg, "winId", lambda: 0x123456)

            assert apply_window_dark_titlebar(dlg, True) is True
            assert (0x123456, 19, 1) in calls
            assert (0x123456, 20, 1) in calls

            calls.clear()
            assert apply_window_dark_titlebar(dlg, False) is True
            assert (0x123456, 19, 0) in calls
            assert (0x123456, 20, 0) in calls

            # Child non-window widget is safely ignored
            child = QLabel(dlg)
            calls.clear()
            assert apply_window_dark_titlebar(child, True) is False
            assert calls == []
        finally:
            dlg.close()
            dlg.deleteLater()

    def test_global_event_filter_updates_visible_dialogs_and_localizes_message_boxes(
        self, qapp, monkeypatch
    ):
        titlebar_states: list[tuple[str, bool]] = []
        monkeypatch.setattr(
            "sayink.ui.theme.apply_window_dark_titlebar",
            lambda w, is_dark: titlebar_states.append((w.objectName() or type(w).__name__, bool(is_dark))) or True,
        )

        apply_theme(qapp, mode="dark")
        dlg = QDialog()
        dlg.setObjectName("sampleTopDialog")
        box = QMessageBox()
        box.setObjectName("sampleRawMsgBox")
        box.setStandardButtons(
            QMessageBox.StandardButton.Ok
            | QMessageBox.StandardButton.Cancel
            | QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No
        )
        try:
            dlg.show()
            box.show()
            qapp.processEvents()

            assert ("sampleTopDialog", True) in titlebar_states
            assert ("sampleRawMsgBox", True) in titlebar_states
            texts = [b.text() for b in box.buttons()]
            assert texts.count("确定") == 2
            assert texts.count("取消") == 2
            assert tok.tokens_for("dark")["BG"].lower() in box.styleSheet().lower()

            # Live switch to light updates already-visible dialogs & message boxes!
            titlebar_states.clear()
            apply_theme(qapp, mode="light")
            qapp.processEvents()
            assert ("sampleTopDialog", False) in titlebar_states
            assert ("sampleRawMsgBox", False) in titlebar_states
            assert tok.tokens_for("light")["BG"].lower() in box.styleSheet().lower()
        finally:
            box.close()
            box.deleteLater()
            dlg.close()
            dlg.deleteLater()
            apply_theme(qapp, mode="light")

    def test_prompts_helpers_style_and_localize_buttons(self, qapp, monkeypatch):
        apply_theme(qapp, mode="dark")
        cbox, accept_btn = confirm_box(
            None, "确认删除", "确定要删除吗？", accept="删除", cancel="取消", destructive=True
        )
        try:
            assert {b.text() for b in cbox.buttons()} == {"删除", "取消"}
            assert accept_btn.styleSheet() == ss.BTN_DANGER_SOLID_SM
            exec_called = []
            monkeypatch.setattr(QMessageBox, "exec", lambda self: exec_called.append(self) or 0)
            alert_error(None, "导出失败", "磁盘空间不足")
            assert len(exec_called) == 1
            err_box = exec_called[0]
            assert [b.text() for b in err_box.buttons()] == ["确定"]
            assert err_box.buttons()[0].styleSheet() == ss.BTN_ACCENT_SM

            std_box = QMessageBox()
            ok = std_box.addButton(QMessageBox.StandardButton.Ok)
            cancel = std_box.addButton(QMessageBox.StandardButton.Cancel)
            style_message_box(std_box)
            assert ok.text() == "确定"
            assert cancel.text() == "取消"
            assert ok.styleSheet() == ss.BTN_ACCENT_SM
            assert cancel.styleSheet() == ss.BTN_GHOST_SM
            std_box.deleteLater()
        finally:
            cbox.deleteLater()
            apply_theme(qapp, mode="light")


class TestDisabledButtonAffordanceAndDateTimeCSS:
    @pytest.mark.parametrize("mode", ["light", "dark"])
    def test_disabled_buttons_keep_bordered_surface_pill_and_weight_400(self, mode: str):
        tok.activate(mode)
        ss.reload_styles()
        for name, css in (
            ("BTN_ACCENT_SM", ss.BTN_ACCENT_SM),
            ("BTN_PRIMARY", ss.BTN_PRIMARY),
            ("BTN_GHOST_SM", ss.BTN_GHOST_SM),
            ("BTN_GHOST", ss.BTN_GHOST),
            ("BTN_DANGER_SM", ss.BTN_DANGER_SM),
        ):
            disabled_rule = css.split("QPushButton:disabled", 1)[1].split("}", 1)[0]
            assert "background: transparent" not in disabled_rule, f"{mode}.{name} lost disabled pill fill"
            assert "border-color: transparent" not in disabled_rule, f"{mode}.{name} lost disabled border"
            assert tok.SURFACE_PEARL.lower() in disabled_rule.lower()
            assert "font-weight: 400" in disabled_rule

    def test_reusable_combo_and_datetime_css_builders(self):
        tok.activate("dark")
        ss.reload_styles()
        dt_css = ss.build_date_time_edit_css()
        combo_css = ss.build_combo_css()
        assert "QDateTimeEdit {" in dt_css
        assert "QDateTimeEdit:focus" in dt_css
        assert "QDateTimeEdit::drop-down" in dt_css
        assert "QDateTimeEdit::down-arrow" in dt_css
        assert f"border: 1px solid {tok.CONTROL_BORDER}" in dt_css
        assert "QComboBox::drop-down" in combo_css
        assert "QComboBox::down-arrow" in combo_css
        tok.activate("light")
        ss.reload_styles()


class TestSecondaryDialogsAndOnboardingPolish:
    def test_media_import_dialog_combo_css_screen_fit_languages_and_empty_hint(
        self, tmp_path: Path, qapp, monkeypatch
    ):
        fake_screen = MagicMock()
        fake_screen.availableGeometry.return_value = QRect(0, 0, 520, 400)
        fake_screen.isEmpty = lambda: False
        monkeypatch.setattr(MediaImportDialog, "screen", lambda self: fake_screen)

        apply_theme(qapp, mode="dark")
        dlg = MediaImportDialog((), config=Config(config_dir=tmp_path))
        try:
            dlg.show()
            qapp.processEvents()

            # Responsive _fit_to_screen clamping instead of overflowing 560x440
            assert dlg.width() <= 496
            assert dlg.height() <= 376

            # Unified language labels matching General settings
            lang_labels = [dlg._lang_combo.itemText(i) for i in range(dlg._lang_combo.count())]
            assert lang_labels == ["跟随设置 / 自动检测", "中文", "English", "日本語"]

            # Styled flat combo drop-down
            assert "QComboBox::drop-down" in dlg.styleSheet()
            assert "spin_chevron_down.png" in dlg.styleSheet()

            # Empty-state overlay visible when 0 files, hidden once a file is added
            assert dlg._count_label.text() == "待导入文件（0）"
            assert dlg._list.count() == 0
            assert dlg._empty_list_hint.isVisible()
            assert not dlg._ok_btn.isEnabled()

            sample = tmp_path / "demo.wav"
            sample.write_bytes(b"RIFF")
            assert dlg.add_paths([sample]) == 1
            assert dlg._count_label.text() == "待导入文件（1）"
            assert not dlg._empty_list_hint.isVisible()
            assert dlg._ok_btn.isEnabled()
        finally:
            dlg.close()
            dlg.deleteLater()
            apply_theme(qapp, mode="light")

    def test_transcript_import_dialog_datetime_alignment_and_updated_hint_copy(self, qapp):
        apply_theme(qapp, mode="dark")
        dlg = TranscriptImportDialog()
        try:
            dlg.show()
            qapp.processEvents()

            assert "导入后可在会议页生成纪要" in dlg._hint.text()
            assert "开启自动生成时将自动排队" not in dlg._hint.text()

            assert dlg._title.height() == tok.CONTROL_FIELD_HEIGHT
            assert dlg._when.height() == tok.CONTROL_FIELD_HEIGHT
            title_top = dlg._title.mapTo(dlg, QPoint(0, 0)).y()
            when_top = dlg._when.mapTo(dlg, QPoint(0, 0)).y()
            assert abs(title_top - when_top) <= 1

            dlg._when.setFocus()
            qapp.processEvents()
            assert dlg._when.height() == tok.CONTROL_FIELD_HEIGHT
        finally:
            dlg.close()
            dlg.deleteLater()
            apply_theme(qapp, mode="light")

    def test_audio_recovery_dialog_top_empty_banner_and_theme_reapply(
        self, tmp_path: Path, qapp
    ):
        empty_root = tmp_path / "empty_rec"
        empty_root.mkdir()
        apply_theme(qapp, mode="dark")
        dlg_empty = AudioRecoveryDialog(empty_root)
        try:
            dlg_empty.show()
            qapp.processEvents()
            assert isinstance(dlg_empty, ThemeAware)
            assert dlg_empty._empty_banner.isVisible()
            assert dlg_empty._empty_list_hint.isVisible()
            assert not dlg_empty.transcribe_btn.isEnabled()
            assert "background: transparent" not in dlg_empty.transcribe_btn.styleSheet().split("QPushButton:disabled", 1)[1]

            apply_theme(qapp, mode="light", surfaces=[dlg_empty])
            assert tok.tokens_for("light")["BG"].lower() in dlg_empty.styleSheet().lower()
        finally:
            dlg_empty.close()
            dlg_empty.deleteLater()

        filled_root = tmp_path / "filled_rec"
        filled_root.mkdir()
        journal = RecordingJournal(filled_root)
        audio = np.full(3200, 0.05, np.float32)
        assert journal.append(audio)
        journal.close()
        dlg_filled = AudioRecoveryDialog(filled_root, recognizer=MagicMock())
        try:
            dlg_filled.show()
            qapp.processEvents()
            assert not dlg_filled._empty_banner.isVisible()
            assert not dlg_filled._empty_list_hint.isVisible()
            assert dlg_filled.transcribe_btn.isEnabled()
        finally:
            dlg_filled.close()
            dlg_filled.deleteLater()
            apply_theme(qapp, mode="light")

    def test_onboarding_setup_dialog_themed_checkbox_and_live_theme_switch(
        self, tmp_path: Path, qapp
    ):
        cfg = Config(config_dir=tmp_path)
        cfg.set("history.onboarded", False)
        ctl = OnboardingController(
            cfg,
            parent=None,
            dialog_parent=lambda: None,
            is_continuous_mode=lambda: False,
            show_main_window=lambda _p: None,
            model_installed=lambda: False,
        )
        dlg = _SetupDialog(ctl)
        try:
            dlg.show()
            qapp.processEvents()
            assert isinstance(dlg, ThemeAware)
            assert isinstance(dlg.history_checkbox, ThemedCheckBox)
            assert not dlg.trial_btn.isEnabled()
            assert not dlg.finish_btn.isEnabled()

            apply_theme(qapp, mode="dark", surfaces=[dlg])
            qapp.processEvents()
            assert tok.tokens_for("dark")["SURFACE"].lower() in dlg.styleSheet().lower()
            assert tok.tokens_for("dark")["SURFACE_PEARL"].lower() in dlg.trial_btn.styleSheet().lower()
            assert not dlg.history_checkbox.grab().isNull()

            apply_theme(qapp, mode="light", surfaces=[dlg])
            qapp.processEvents()
            assert tok.tokens_for("light")["SURFACE"].lower() in dlg.styleSheet().lower()
        finally:
            dlg.close()
            dlg.deleteLater()
            cfg._save_timer.stop()


class TestFloatingWindowErrorUXAndSettingsPolish:
    def test_show_error_clears_stale_chips_and_wraps_multi_clause_hint_without_truncation(
        self, qapp
    ):
        win = FloatingWindow()
        try:
            # Simulate active continuous listening with live utterance, then fatal device error
            win.show_listening("notepad.exe")
            win.set_speech_capturing(True)
            win.show_live_transcript("正在说的一句话")
            assert win._cancel_btn.isVisible()
            assert win._end_btn.isVisible()

            msg = (
                "无法采集系统声音\n"
                "Windows 可启用立体声混音或安装 PyAudioWPatch；混合模式需配置电脑播放设备"
            )
            win.show_error(msg)
            qapp.processEvents()

            # Stale 放弃本句 / 结束 chips are hidden; passive × dismiss button is shown
            assert not win._cancel_btn.isVisible()
            assert not win._end_btn.isVisible()
            assert win._dismiss_btn.isVisible()

            # Full 2-clause guidance fits across 2 lines without truncating the second clause!
            assert win._status_label.text() == "无法采集系统声音"
            shown_detail = win._text_label.text()
            assert "PyAudioWPatch" in shown_detail
            assert "混合模式需配置电脑播放设备" in shown_detail
            assert "…" not in shown_detail
            assert win.height() <= COMPACT_HEIGHT + 40
            assert msg in win.toolTip()
            assert msg in win._status_label.toolTip()
            assert msg in win._text_label.toolTip()

            # When continuous session is genuinely still live (keep_continuous=True),
            # 结束 stays available while 放弃本句 still hides on error frame.
            win.show_listening()
            win.show_live_transcript("上一句")
            win.show_error("单段识别超时：已跳过该片段", keep_continuous=True)
            assert win._end_btn.isVisible()
            assert not win._cancel_btn.isVisible()
        finally:
            win.close()
            win.deleteLater()

    def test_general_hotkey_reset_alignment_and_280px_download_source_combo(
        self, tmp_path: Path, qapp
    ):
        cfg = Config(config_dir=tmp_path)
        gp = GeneralSettingsPage()
        mp = ModelSettingsPage()
        try:
            gp.resize(700, 600)
            gp.show()
            mp.resize(700, 600)
            mp.show()
            mp.load_config(cfg)
            qapp.processEvents()

            # Hotkey reset sits cleanly beside hotkey_edit with hand cursor & focus ring
            edit_y = gp.hotkey_edit.mapTo(gp, QPoint(0, gp.hotkey_edit.height() // 2)).y()
            btn_y = gp.hotkey_reset_btn.mapTo(gp, QPoint(0, gp.hotkey_reset_btn.height() // 2)).y()
            assert abs(edit_y - btn_y) <= 2
            assert gp.hotkey_reset_btn.cursor().shape() == Qt.CursorShape.PointingHandCursor
            assert ":focus" in gp.hotkey_reset_btn.styleSheet()
            field_labels = [
                lb for lb in gp.findChildren(QLabel) if lb.text() == "录音快捷键"
            ]
            assert field_labels and field_labels[0].buddy() is gp.hotkey_edit

            # 280px download source combo shows full auto-mirror label without eliding!
            assert tok.CONTROL_SETTINGS_COMBO_WIDTH == 280
            assert mp.download_source_combo.width() == 280
            mp.download_source_combo.setCurrentIndex(mp.download_source_combo.findData("auto"))
            qapp.processEvents()
            assert "官方优先" in mp.download_source_combo.currentText()
            assert not mp.download_source_combo.isTextElided()

            # Keyboard focus ring activates cleanly when enabled
            QTest.mouseMove(gp.hotkey_reset_btn, QPoint(8, 8))
        finally:
            gp.close()
            gp.deleteLater()
            mp.close()
            mp.deleteLater()
            cfg._save_timer.stop()

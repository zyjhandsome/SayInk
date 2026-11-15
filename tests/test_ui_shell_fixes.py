"""Regression tests for Settings, ElidedComboBox, FloatingWindow contrast, AudioRecoveryDialog, and MainWindow shell polish."""

from __future__ import annotations

import os
import re
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest
from PyQt6.QtCore import QPoint, QRect, Qt
from PyQt6.QtGui import QColor, QFontDatabase, QImage, QPainter, QTextLayout
from PyQt6.QtWidgets import (
    QComboBox,
    QLabel,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sayink.audio_recovery import RecordingJournal
from sayink.config import DEFAULT_HOTKEY, Config
from sayink.history_store import HistoryStore, SegmentRecord
from sayink.ui import design_tokens as tok
from sayink.ui import settings_styles
from sayink.ui.audio_recovery_dialog import AudioRecoveryDialog
from sayink.ui.floating_window import FloatingWindow
from sayink.ui.history_window import HistoryWindow
from sayink.ui.main_window import MainWindow
from sayink.ui.settings_components import (
    ElidedComboBox,
    SwitchControl,
    ThemedCheckBox,
    _qcolor,
)
from sayink.ui.settings_pages.general import GeneralSettingsPage
from sayink.ui.settings_pages.model import ModelSettingsPage
from sayink.ui.settings_pages.polish import PolishSettingsPage
from sayink.ui.settings_window import SettingsWindow
from sayink.ui.theme import apply_theme


@pytest.fixture(scope="module", autouse=True)
def _load_windows_fonts(_qapp_session):
    for font_name in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf", "seguisb.ttf"):
        font_path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / font_name
        if font_path.exists():
            QFontDatabase.addApplicationFont(str(font_path))


def _make_shell(tmp_path: Path, monkeypatch) -> tuple[MainWindow, Config, HistoryStore]:
    monkeypatch.setattr(SettingsWindow, "_rebuild_model_cards", lambda self: None)
    monkeypatch.setattr(SettingsWindow, "_refresh_about_info", lambda self: None)
    monkeypatch.setattr(SettingsWindow, "_refresh_audio_device_lists", lambda self: None)
    monkeypatch.setattr("sayink.speech_recognizer.is_model_downloaded", lambda _mid: False)
    monkeypatch.setattr("sayink.config.default_secret_store", lambda: None)
    monkeypatch.setattr("sayink.config.Config._sync_registry_auto_start", lambda self: None)
    store = HistoryStore(tmp_path / "shell_history.db")
    cfg = Config(config_dir=tmp_path)
    win = MainWindow(cfg, store)
    return win, cfg, store


def _composite_rgba(fg: QColor, bg: tuple[float, float, float]) -> tuple[float, float, float]:
    alpha = fg.alphaF()
    inv = 1.0 - alpha
    return (
        fg.red() * alpha + bg[0] * inv,
        fg.green() * alpha + bg[1] * inv,
        fg.blue() * alpha + bg[2] * inv,
    )


def _relative_luminance(rgb: tuple[float, float, float]) -> float:
    channels = []
    for channel in rgb:
        srgb = max(0.0, min(1.0, channel / 255.0))
        if srgb <= 0.04045:
            channels.append(srgb / 12.92)
        else:
            channels.append(((srgb + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _contrast_ratio(c1: tuple[float, float, float], c2: tuple[float, float, float]) -> float:
    l1 = _relative_luminance(c1)
    l2 = _relative_luminance(c2)
    hi, lo = (l1, l2) if l1 >= l2 else (l2, l1)
    return (hi + 0.05) / (lo + 0.05)


# ── UI-01 (part 2): _qcolor & valid QColor across tokens and controls ─────────


class TestQColorTokenParsing:
    def test_qcolor_parses_hex_rgb_and_rgba_variants(self, monkeypatch):
        c_hex = _qcolor("#2563EB")
        assert c_hex.isValid()
        assert (c_hex.red(), c_hex.green(), c_hex.blue(), c_hex.alpha()) == (37, 99, 235, 255)

        c_rgb = _qcolor("rgb(13, 148, 136)")
        assert c_rgb.isValid()
        assert (c_rgb.red(), c_rgb.green(), c_rgb.blue(), c_rgb.alpha()) == (13, 148, 136, 255)

        c_rgba_float = _qcolor("rgba(13, 13, 13, 0.08)")
        assert c_rgba_float.isValid()
        assert (c_rgba_float.red(), c_rgba_float.green(), c_rgba_float.blue(), c_rgba_float.alpha()) == (13, 13, 13, 20)

        c_rgba_int = _qcolor("rgba(255, 255, 255, 236)")
        assert c_rgba_int.isValid()
        assert (c_rgba_int.red(), c_rgba_int.green(), c_rgba_int.blue(), c_rgba_int.alpha()) == (255, 255, 255, 236)

        c_rgba_pct = _qcolor("rgba(235, 235, 245, 50%)")
        assert c_rgba_pct.isValid()
        assert c_rgba_pct.alpha() == 128

        seen = []

        def fake_tok_parser(val: str) -> QColor:
            seen.append(val)
            return QColor(12, 34, 56, 78)

        monkeypatch.setattr(tok, "qcolor_from_token", fake_tok_parser, raising=False)
        delegated = _qcolor("rgba(1, 2, 3, 0.5)")
        assert seen == ["rgba(1, 2, 3, 0.5)"]
        assert (delegated.red(), delegated.green(), delegated.blue(), delegated.alpha()) == (12, 34, 56, 78)

    @pytest.mark.parametrize("mode", ["light", "dark"])
    def test_all_design_token_colors_and_disabled_checkbox_are_valid(self, _qapp_session, mode):
        tok.activate(mode)
        palette_dict = tok._LIGHT if mode == "light" else tok._DARK
        for name, raw in palette_dict.items():
            entries = raw if isinstance(raw, tuple) else (raw,)
            for item in entries:
                parsed = _qcolor(item)
                assert parsed.isValid(), f"{mode}.{name}={item!r} produced an invalid QColor"

        host = QWidget()
        host.setStyleSheet(f"background: {tok.SURFACE};")
        lay = QVBoxLayout(host)
        cb_disabled = ThemedCheckBox("禁用勾选项")
        cb_disabled.setEnabled(False)
        cb_checked = ThemedCheckBox("禁用已勾选项")
        cb_checked.setChecked(True)
        cb_checked.setEnabled(False)
        sw = SwitchControl()
        lay.addWidget(cb_disabled)
        lay.addWidget(cb_checked)
        lay.addWidget(sw)
        host.resize(220, 120)

        border_color = _qcolor(tok.HAIRLINE)
        assert border_color.isValid()
        assert border_color.alpha() > 0

        img = QImage(220, 120, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(_qcolor(tok.SURFACE))
        painter = QPainter(img)
        host.render(painter)
        painter.end()
        assert not img.isNull()
        host.deleteLater()

    def test_floating_window_rgba_state_tokens_produce_valid_qcolors_in_dark_mode(self, _qapp_session):
        tok.activate("dark")
        bar = FloatingWindow()
        try:
            bar._set_state("正在整理…", "STATE_POLISH")
            assert bar._dot._color.isValid()
            assert bar._waveform._accent.isValid()
            bar._set_state("未启用", "STATE_MUTED")
            assert bar._dot._color.isValid()
        finally:
            bar.close()
            bar.deleteLater()


# ── UI-06 (part 2): ElidedComboBox elision & automatic tooltip ───────────────


class TestElidedComboBox:
    def test_elides_long_chinese_text_and_preserves_custom_tooltip(self, _qapp_session):
        tok.activate("light")
        settings_styles.reload_styles()
        combo = ElidedComboBox()
        combo.setStyleSheet(settings_styles.WINDOW_CSS)
        combo.setFixedWidth(180)
        short_text = "国内镜像"
        long_text = "国内镜像（ModelScope / HF-Mirror 推荐无代理高速下载语音识别模型）"
        combo.addItem(short_text, "mirror")
        combo.addItem(long_text, "custom")
        combo.show()
        _qapp_session.processEvents()

        combo.setCurrentIndex(0)
        _qapp_session.processEvents()
        assert not combo.isTextElided()
        assert combo.toolTip() == ""

        combo.setCurrentIndex(1)
        _qapp_session.processEvents()
        assert combo.isTextElided()
        assert combo.elidedCurrentText().endswith("…")
        assert combo.toolTip() == long_text

        # Explicit custom tooltip is preserved when not elided, and combined when elided.
        combo.setToolTip("选择模型下载路线")
        combo.setCurrentIndex(0)
        _qapp_session.processEvents()
        assert not combo.isTextElided()
        assert combo.toolTip() == "选择模型下载路线"

        combo.setCurrentIndex(1)
        _qapp_session.processEvents()
        assert combo.isTextElided()
        assert long_text in combo.toolTip()
        assert "选择模型下载路线" in combo.toolTip()

        # Dynamic setItemText on active index refreshes tooltip immediately.
        combo.setCurrentIndex(0)
        combo.setToolTip("")
        updated_long = "与转写相同（当前：SenseVoice Small 多语种离线高精度模型）"
        combo.setItemText(0, updated_long)
        _qapp_session.processEvents()
        assert combo.isTextElided()
        assert combo.toolTip() == updated_long
        assert not combo.grab().isNull()

        combo.close()
        combo.deleteLater()

    def test_settings_pages_use_elided_combobox(self, tmp_path):
        cfg = Config(config_dir=tmp_path / ".sayink")
        gp = GeneralSettingsPage()
        mp = ModelSettingsPage()
        pp = PolishSettingsPage(cfg)
        try:
            assert isinstance(mp.download_source_combo, ElidedComboBox)
            for combo in (
                gp.output_mode_combo,
                gp.mic_device_combo,
                gp.system_device_combo,
                gp.listen_bar_combo,
                gp.llm_proxy_combo,
                gp.meeting_language_combo,
                gp.meeting_source_combo,
                gp.meeting_model_combo,
            ):
                assert isinstance(combo, ElidedComboBox)
            for combo in (
                pp.llm_mode_combo,
                pp.minutes_template_combo,
                pp.minutes_language_combo,
            ):
                assert isinstance(combo, ElidedComboBox)
        finally:
            gp.deleteLater()
            mp.deleteLater()
            pp.deleteLater()
            cfg._save_timer.stop()


# ── UI-07 (part 2): Narrow <880px / <845px HistoryWindow & MainWindow ────────


class TestNarrowViewportHistoryReflow:
    def test_narrow_screen_relaxes_history_pane_minimums_without_clipping_buttons(
        self, tmp_path, monkeypatch, _qapp_session
    ):
        fake_screen = MagicMock()
        fake_screen.availableGeometry.return_value = QRect(0, 0, 780, 560)
        monkeypatch.setattr(MainWindow, "screen", lambda self: fake_screen)

        win, cfg, store = _make_shell(tmp_path, monkeypatch)
        standalone = HistoryWindow(store, config=cfg)
        try:
            store.enqueue(
                SegmentRecord(
                    session_id="s-narrow",
                    seq=0,
                    created_at=1_700_000_000_000,
                    raw_text="窄屏下的历史详情按钮不应被裁切到窗口外。",
                    polished_text="窄屏下的历史详情按钮不应被裁切到窗口外。",
                    source="microphone",
                    duration_ms=3000,
                )
            )
            store.flush()
            win.show()
            win.show_page("history")
            win._history.refresh()
            _qapp_session.processEvents()

            assert win.width() <= 780
            assert win.width() >= win.layout().minimumSize().width()
            assert win._history._left_pane.minimumWidth() < tok.WORKSPACE_LIST_MIN_WIDTH
            assert win._history._right_pane.minimumWidth() < tok.WORKSPACE_DETAIL_MIN_WIDTH

            for btn in (
                win._history._copy_btn,
                win._history._export_btn,
                win._history._detail_more_btn,
                win._history._delete_btn,
            ):
                if btn.isVisible():
                    right_in_win = btn.mapTo(win, btn.rect().bottomRight()).x()
                    assert right_in_win <= win.width(), (btn.text(), right_in_win, win.width())

            standalone.resize(780, 560)
            standalone.show()
            _qapp_session.processEvents()
            assert standalone._left_pane.minimumWidth() < tok.WORKSPACE_LIST_MIN_WIDTH
            assert standalone._right_pane.minimumWidth() < tok.WORKSPACE_DETAIL_MIN_WIDTH

            # Restoring normal >=880px width restores standard pane minimums.
            fake_screen.availableGeometry.return_value = QRect(0, 0, 1440, 900)
            win._fit_to_available_screen()
            win.resize(960, 640)
            standalone.resize(900, 600)
            _qapp_session.processEvents()
            assert win._history._left_pane.minimumWidth() == tok.WORKSPACE_LIST_MIN_WIDTH
            assert win._history._right_pane.minimumWidth() == tok.WORKSPACE_DETAIL_MIN_WIDTH
            assert standalone._left_pane.minimumWidth() == tok.WORKSPACE_LIST_MIN_WIDTH
            assert standalone._right_pane.minimumWidth() == tok.WORKSPACE_DETAIL_MIN_WIDTH
        finally:
            standalone.close()
            standalone.deleteLater()
            win.close()
            cfg._save_timer.stop()
            store.close()


# ── UI-09: FloatingWindow._cancel_btn WCAG AA >= 4.5:1 contrast ──────────────


class TestFloatingCancelContrast:
    @pytest.mark.parametrize("mode", ["dark", "light"])
    def test_cancel_button_meets_wcag_aa_contrast_in_dark_and_light_modes(self, _qapp_session, mode):
        bar = FloatingWindow()
        try:
            apply_theme(_qapp_session, mode=mode, surfaces=[bar])
            css = bar._cancel_btn.styleSheet()
            normal_block = css.split("QPushButton:hover")[0]
            bg_match = re.search(r"background:\s*([^;]+);", normal_block)
            fg_match = re.search(r"color:\s*([^;]+);", normal_block)
            border_match = re.search(r"border:\s*([^;]+);", normal_block)
            assert bg_match and fg_match and border_match
            assert "1px solid" in border_match.group(1)

            chip_bg = _qcolor(bg_match.group(1).strip())
            fg_color = _qcolor(fg_match.group(1).strip())
            float_bg = _qcolor(tok.FLOAT_BG)
            assert chip_bg.isValid() and fg_color.isValid() and float_bg.isValid()

            for desktop_rgb in ((0.0, 0.0, 0.0), (255.0, 255.0, 255.0)):
                surface_rgb = _composite_rgba(float_bg, desktop_rgb)
                pill_rgb = _composite_rgba(chip_bg, surface_rgb)
                text_rgb = _composite_rgba(fg_color, pill_rgb)
                ratio = _contrast_ratio(text_rgb, pill_rgb)
                assert ratio >= 4.5, f"{mode} contrast over {desktop_rgb} was {ratio:.2f}:1 (< 4.5:1)"
        finally:
            bar.close()
            bar.deleteLater()


# ── UI-10: AudioRecoveryDialog design-system polish ──────────────────────────


class TestAudioRecoveryDialogPolish:
    def test_dialog_preserves_attributes_and_applies_design_system(self, tmp_path, _qapp_session):
        journal = RecordingJournal(tmp_path)
        audio = np.full(4000, 0.05, np.float32)
        assert journal.append(audio, tracks={"microphone": audio})
        journal.close()

        dlg = AudioRecoveryDialog(tmp_path, recognizer=MagicMock())
        try:
            dlg.show()
            _qapp_session.processEvents()

            # Attribute & type compatibility
            assert isinstance(dlg.items, QListWidget)
            assert isinstance(dlg.track, QComboBox)
            assert isinstance(dlg.track, ElidedComboBox)
            assert isinstance(dlg.export_btn, QPushButton)
            assert isinstance(dlg.transcribe_btn, QPushButton)
            assert isinstance(dlg.stop_btn, QPushButton)
            assert isinstance(dlg.delete_btn, QPushButton)
            assert isinstance(dlg.output, QPlainTextEdit)
            assert isinstance(dlg.status, QLabel)
            assert isinstance(dlg.save_btn, QPushButton)
            assert isinstance(dlg.close_btn, QPushButton)

            # Margins, accessible names, cursors
            margins = dlg.layout().contentsMargins()
            assert (margins.left(), margins.top(), margins.right(), margins.bottom()) == (
                tok.SPACE_MD,
                tok.SPACE_MD,
                tok.SPACE_MD,
                tok.SPACE_MD,
            )
            assert dlg.items.accessibleName() == "可恢复录音列表"
            assert dlg.output.accessibleName() == "恢复的识别文字"
            for btn in (
                dlg.transcribe_btn,
                dlg.export_btn,
                dlg.stop_btn,
                dlg.delete_btn,
                dlg.save_btn,
                dlg.close_btn,
            ):
                assert btn.cursor().shape() == Qt.CursorShape.PointingHandCursor

            # Primary vs ghost vs danger button hierarchy & soft list item selection
            apply_theme(_qapp_session, mode="dark", surfaces=[dlg])
            assert tok.PRIMARY_CONTAINER in dlg.transcribe_btn.styleSheet()
            assert dlg.transcribe_btn.styleSheet() != dlg.export_btn.styleSheet()
            assert tok.RED in dlg.delete_btn.styleSheet()
            assert "QListWidget::item:selected" in dlg.styleSheet()
            assert tok.ACCENT_SOFT in dlg.styleSheet()

            # Right-aligned compact footer (save_btn not stretched full width)
            assert dlg.save_btn.width() < dlg.width() // 2
            assert dlg.close_btn.geometry().left() > dlg.save_btn.geometry().left()
            dlg.close_btn.click()
            _qapp_session.processEvents()
            assert not dlg.isVisible()
        finally:
            dlg.close()
            dlg.deleteLater()


# ── UI-11: Sidebar shortcut wrap/padding & _size_grip scrollbar clearance ────


class TestSidebarHintAndSizeGrip:
    @pytest.mark.parametrize("hotkey", ["alt+x", "ctrl+shift+space"])
    @pytest.mark.parametrize("mode", ["hotkey", "continuous"])
    def test_sidebar_shortcut_has_no_orphan_char_wrap_and_comfortable_bottom_padding(
        self, tmp_path, monkeypatch, _qapp_session, hotkey, mode
    ):
        fake_screen = MagicMock()
        fake_screen.availableGeometry.return_value = QRect(0, 0, 910, 472)
        monkeypatch.setattr(MainWindow, "screen", lambda self: fake_screen)

        win, cfg, store = _make_shell(tmp_path, monkeypatch)
        try:
            cfg.set("hotkey", hotkey)
            cfg.set("audio.trigger_mode", mode)
            win.settings_panel.reload_settings()
            win.show()
            _qapp_session.processEvents()

            assert win.minimumHeight() <= 472
            assert win.height() <= 472

            shortcut_lbl = win._shortcut_label
            shortcut_lbl.ensurePolished()
            inner_w = max(80, shortcut_lbl.contentsRect().width() - 12)
            for paragraph in shortcut_lbl.text().split("\n"):
                tl = QTextLayout(paragraph, shortcut_lbl.font())
                tl.beginLayout()
                wrapped_lines = []
                while True:
                    line = tl.createLine()
                    if not line.isValid():
                        break
                    line.setLineWidth(inner_w)
                    wrapped_lines.append(paragraph[line.textStart() : line.textStart() + line.textLength()])
                tl.endLayout()
                for seg in wrapped_lines:
                    assert len(seg.strip()) >= 2, (
                        f"Single-char orphan wrap {wrapped_lines!r} in inner_w={inner_w}"
                    )

            bottom_in_sidebar = shortcut_lbl.mapTo(win._sidebar, shortcut_lbl.rect().bottomLeft()).y()
            # Layout bottom margin (10px) + label bottom padding (2px) >= 10px, never clipped.
            assert bottom_in_sidebar < win._sidebar.height()
            clearance = (win._sidebar.height() - 1 - bottom_in_sidebar) + 2
            assert clearance >= 10, f"Expected >= 10px bottom clearance, got {clearance}px"
        finally:
            win.close()
            cfg._save_timer.stop()
            store.close()

    def test_size_grip_docked_in_corner_without_overlapping_settings_scrollbar(
        self, tmp_path, monkeypatch, _qapp_session
    ):
        win, cfg, store = _make_shell(tmp_path, monkeypatch)
        try:
            win.resize(960, 640)
            win.show()
            win.show_page("general")
            _qapp_session.processEvents()

            grip = win._size_grip
            assert grip.x() == win.width() - grip.width() - 3
            assert grip.y() == win.height() - grip.height() - 3

            page = win._settings.general_page
            vbar = page.verticalScrollBar()
            assert vbar.maximum() > 0
            vbar_top_left = vbar.mapTo(win, QPoint(0, 0))
            vbar_rect = QRect(vbar_top_left, vbar.size())
            assert not grip.geometry().intersects(vbar_rect), (grip.geometry(), vbar_rect)
        finally:
            win.close()
            cfg._save_timer.stop()
            store.close()


# ── UI-12: hotkey_reset_btn state/compact width & jump pill label ────────────


class TestGeneralHotkeyResetAndJumpBar:
    def test_hotkey_reset_compact_disabled_on_default_and_jump_pill_matches_header(
        self, tmp_path, monkeypatch, _qapp_session
    ):
        win, cfg, store = _make_shell(tmp_path, monkeypatch)
        try:
            win.resize(960, 640)
            win.show()
            win.show_page("general")
            _qapp_session.processEvents()

            gp = win._settings.general_page
            reset_btn = gp.hotkey_reset_btn

            # Jump pill matches section header "会议默认设置"
            jump_labels = [b.text() for b in gp.section_jump_bar.jump_buttons]
            assert jump_labels == ["录音与输入", "识别", "会议默认设置", "应用与历史"]

            # Compact left-aligned (not stretched across ~622px card)
            assert reset_btn.width() < 220

            # Disabled when active hotkey equals DEFAULT_HOTKEY (case-insensitive)
            assert cfg.get("hotkey").lower() == DEFAULT_HOTKEY.lower()
            assert not reset_btn.isEnabled()

            cfg.set("hotkey", "Alt+X")
            win._settings.reload_settings()
            assert not reset_btn.isEnabled()

            # Enabled dynamically when changed away from default
            win._settings._apply_hotkey_setting("ctrl+shift+z")
            assert cfg.get("hotkey") == "ctrl+shift+z"
            assert reset_btn.isEnabled()

            # Disabled again when restored to default
            monkeypatch.setattr(win._settings, "_confirm", lambda *_a, **_kw: True)
            reset_btn.click()
            _qapp_session.processEvents()
            assert cfg.get("hotkey") == DEFAULT_HOTKEY
            assert not reset_btn.isEnabled()
        finally:
            win.close()
            cfg._save_timer.stop()
            store.close()

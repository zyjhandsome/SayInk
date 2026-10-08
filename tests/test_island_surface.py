"""Spatial Island surface: morph modes, chrome, and HUD actions."""

from __future__ import annotations

import sys

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from sayink.ui.floating_window import COMPACT_HEIGHT, FloatingWindow
from sayink.ui.island_chrome import ISLAND_SHEET_WIDTH, position_island


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication(sys.argv)


@pytest.fixture
def win(qapp):
    w = FloatingWindow()
    yield w
    w.close()


class TestIslandMorph:
    def test_listening_uses_compact_capsule(self, win):
        win.show_listening()
        assert win.height() <= COMPACT_HEIGHT + 8
        assert "正在听" in win._status_label.text()

    def test_partial_text_stays_listen_bar(self, win):
        from sayink.ui.floating_window import BAR_EXCERPT_HEIGHT

        win.show_listening()
        win.update_partial_text("下一步把这份纪要贴到会议群里。")
        assert "纪要" in win._text_label.text()
        assert win.height() <= BAR_EXCERPT_HEIGHT + 8
        assert not hasattr(win, "_history_btn")
        assert not hasattr(win, "_settings_btn")
        assert win._end_btn.isVisible()
        assert win._end_btn.text() == "结束"

    def test_end_button_stops_continuous(self, win):
        stops = []
        win.continuous_stop_requested.connect(lambda: stops.append(True))
        win.show_listening()
        win.update_partial_text("hello")
        win._end_btn.click()
        assert stops == [True]

    def test_success_keeps_live_transcript_while_listening(self, win):
        from sayink.ui.floating_window import BAR_EXCERPT_HEIGHT

        win.show_listening()
        win.update_partial_text("done")
        win.show_success("已输入")
        assert "done" in win._text_label.text()
        assert win.height() <= BAR_EXCERPT_HEIGHT + 8
        assert win.height() > COMPACT_HEIGHT

    def test_container_uses_island_object_name(self, win):
        assert win._container.objectName() == "islandContainer"

    def test_end_is_ink_primary(self, win):
        from sayink.ui import design_tokens as tok

        win.show_listening()
        css = win._end_btn.styleSheet().lower()
        assert tok.PRIMARY_CONTAINER.lower() in css
        assert tok.PRIMARY_ON.lower() in css


class TestIslandChrome:
    def test_hud_is_frameless_tool(self, win):
        flags = win.windowFlags()
        assert flags & Qt.WindowType.FramelessWindowHint
        assert flags & Qt.WindowType.WindowStaysOnTopHint
        assert flags & Qt.WindowType.Tool

    def test_position_island_centers_near_top(self, qapp, win):
        win.resize(320, 56)
        position_island(win, width=320)
        screen = qapp.primaryScreen().availableGeometry()
        assert win.x() >= screen.x()
        assert win.y() <= screen.y() + 140

    def test_listen_bar_has_no_source_chips(self, win):
        win.set_input_source("mixed")
        win.show_listening()
        win.update_partial_text("hello")
        assert not hasattr(win, "_mic_chip") or not win._mic_chip.isVisible()
        assert not hasattr(win, "_sys_chip") or not win._sys_chip.isVisible()


class TestHistoryTimeStream:
    def test_history_uses_single_column_stream(self, qapp):
        from sayink.ui.history_window import HistoryWindow
        from tests.test_history_window import FakeHistoryStore

        window = HistoryWindow(FakeHistoryStore())
        try:
            assert window._session_list.objectName() == "historyTimeStream"
            assert window._left_pane is window._stream_host
            first = window.session_items()[0]
            widget = window._session_list.itemWidget(first)
            assert widget is not None
            from PyQt6.QtWidgets import QLabel
            texts = [lab.text() for lab in widget.findChildren(QLabel)]
            assert any("newer preview" in t for t in texts)
        finally:
            window.close()


class TestIslandSheetConstants:
    def test_sheet_width_constant(self):
        from sayink.ui.island_chrome import ISLAND_SETTINGS_WIDTH

        assert ISLAND_SHEET_WIDTH >= 440
        assert ISLAND_SHEET_WIDTH <= 560
        assert ISLAND_SETTINGS_WIDTH >= ISLAND_SHEET_WIDTH
        assert ISLAND_SETTINGS_WIDTH <= 760


def _qdialog_block(css: str) -> str:
    import re

    match = re.search(r"QDialog\s*\{([^}]*)\}", css)
    assert match, "island sheet must style the QDialog root"
    return match.group(1)


class TestIslandSheetGlass:
    def test_settings_dialog_root_is_transparent(self, qapp, tmp_path, monkeypatch):
        from PyQt6.QtWidgets import QWidget

        from sayink.config import Config
        from sayink.ui import design_tokens as tok
        from sayink.ui.settings_window import SettingsWindow

        monkeypatch.setattr(SettingsWindow, "_rebuild_model_cards", lambda self: None)
        monkeypatch.setattr(SettingsWindow, "_refresh_about_info", lambda self: None)
        monkeypatch.setattr(SettingsWindow, "_refresh_audio_device_lists", lambda self: None)
        win = SettingsWindow(Config(config_dir=tmp_path))
        try:
            assert isinstance(win, QWidget)
            assert not hasattr(win, "_sheet")
            body = _qdialog_block(win.styleSheet())
            assert "transparent" in body
            assert tok.BG not in body
        finally:
            win.close()

    def test_history_dialog_and_right_pane_are_transparent(self, qapp):
        from PyQt6.QtWidgets import QWidget

        from sayink.ui import design_tokens as tok
        from sayink.ui.history_window import HistoryWindow
        from tests.test_history_window import FakeHistoryStore

        win = HistoryWindow(FakeHistoryStore())
        try:
            assert isinstance(win, QWidget)
            assert not hasattr(win, "_sheet")
            assert not hasattr(win, "_close_btn")
            body = _qdialog_block(win.styleSheet())
            assert "transparent" in body
            assert tok.BG not in body
            right = win._right_pane.styleSheet()
            assert "transparent" in right
            assert tok.BG not in right
        finally:
            win.close()

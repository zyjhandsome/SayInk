# tests/test_main_window.py
import sys
import pytest
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt

@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication(sys.argv)


def _make_main_window(tmp_path, monkeypatch):
    from sayink.config import Config
    from sayink.history_store import HistoryStore
    from sayink.ui.main_window import MainWindow
    from sayink.ui.settings_window import SettingsWindow
    monkeypatch.setattr(SettingsWindow, "_rebuild_model_cards", lambda self: None)
    monkeypatch.setattr(SettingsWindow, "_refresh_about_info", lambda self: None)
    monkeypatch.setattr(SettingsWindow, "_refresh_audio_device_lists", lambda self: None)
    store = HistoryStore(tmp_path / "h.db")
    win = MainWindow(Config(config_dir=tmp_path), store)
    return win, store

def _use_screen(monkeypatch, width, height):
    from PyQt6.QtCore import QRect
    from sayink.ui.main_window import MainWindow

    class _Screen:
        def availableGeometry(self):
            return QRect(0, 0, width, height)

    screen = _Screen()
    monkeypatch.setattr(MainWindow, "screen", lambda self: screen)


def test_small_scaled_screen_lowers_minimum_to_fit(qapp, tmp_path, monkeypatch):
    # 1366×768 at 150% leaves about 910×472 logical pixels above the taskbar.
    _use_screen(monkeypatch, 910, 472)
    win, store = _make_main_window(tmp_path, monkeypatch)
    try:
        win.show()
        assert win.minimumHeight() <= 472
        assert win.height() <= 472
        assert win.width() <= 910
    finally:
        win.close()
        store.close()


def test_narrow_screen_does_not_clip_below_layout_minimum(qapp, tmp_path, monkeypatch):
    _use_screen(monkeypatch, 800, 800)
    win, store = _make_main_window(tmp_path, monkeypatch)
    try:
        win.show()
        win.show_page("history")
        QApplication.processEvents()
        assert win.width() >= win.layout().minimumSize().width()
    finally:
        win.close()
        store.close()


def test_large_screen_keeps_preferred_minimum(qapp, tmp_path, monkeypatch):
    _use_screen(monkeypatch, 1920, 1040)
    win, store = _make_main_window(tmp_path, monkeypatch)
    try:
        assert (win.minimumWidth(), win.minimumHeight()) == (880, 580)
    finally:
        win.close()
        store.close()


def test_chrome_size_and_nav(qapp, tmp_path, monkeypatch):
    from sayink.ui.main_window import NAV_LABELS
    from sayink.ui import design_tokens as tok
    _use_screen(monkeypatch, 1920, 1040)
    win, store = _make_main_window(tmp_path, monkeypatch)
    try:
        assert win.width() == 960
        assert win.height() == 640
        assert [b.text() for b in win._nav_buttons] == list(NAV_LABELS)
        assert win._sidebar.width() == tok.SIDEBAR_WIDTH
        assert win.windowFlags() & Qt.WindowType.FramelessWindowHint
        assert win.current_page() == "general"
        win.show_page("history")
        assert win.current_page() == "history"
    finally:
        win.close()
        store.close()


def test_outer_border_uses_explicit_window_frame(qapp, tmp_path, monkeypatch):
    from sayink.ui import design_tokens as tok

    win, store = _make_main_window(tmp_path, monkeypatch)
    try:
        css = win.styleSheet()
        margins = win.layout().contentsMargins()
        assert win.objectName() == "mainWindow"
        assert "QWidget#mainWindow" in css
        assert f"border: 1px solid {tok.TEXT_DIM}" in css
        assert f"border-radius: {tok.WINDOW_RADIUS}px" in css
        assert not win.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        assert f"background: {tok.BG}" in css
        assert f"background: {tok.BG}" in win._stack.styleSheet()
        assert (margins.left(), margins.top(), margins.right(), margins.bottom()) == (
            1,
            1,
            1,
            1,
        )
        assert "border: 1px solid" not in win._sidebar.styleSheet()
    finally:
        win.close()
        store.close()


def test_maximize_toggle_does_not_restyle_the_whole_window(qapp, tmp_path, monkeypatch):
    win, store = _make_main_window(tmp_path, monkeypatch)
    try:
        calls: list[str] = []
        win.reapply_theme = lambda: calls.append("theme")
        monkeypatch.setattr(win, "isMaximized", lambda: False)
        monkeypatch.setattr(win, "showMaximized", lambda: None)
        monkeypatch.setattr(win, "showNormal", lambda: None)
        win._toggle_maximized()
        assert calls == []
    finally:
        win.close()
        store.close()


def test_maximized_chrome_drops_the_round_mask(qapp, tmp_path, monkeypatch):
    win, store = _make_main_window(tmp_path, monkeypatch)
    try:
        monkeypatch.setattr(win, "isMaximized", lambda: True)
        win._apply_chrome_radius()
        assert "border-radius: 0px" in win.styleSheet()
        assert "border: none" in win.styleSheet()
        assert win.layout().contentsMargins().left() == 0
        assert win.mask().isEmpty()
    finally:
        win.close()
        store.close()


def test_maximized_caption_uses_stacked_restore_glyph(qapp, tmp_path, monkeypatch):
    win, store = _make_main_window(tmp_path, monkeypatch)
    try:
        assert win._max_btn.shows_restore is False
        monkeypatch.setattr(win, "isMaximized", lambda: True)
        win._sync_max_glyph()
        assert win._max_btn.shows_restore is True
        assert win._max_btn.toolTip() == "还原"
        monkeypatch.setattr(win, "isMaximized", lambda: False)
        win._sync_max_glyph()
        assert win._max_btn.shows_restore is False
        assert win._max_btn.toolTip() == "最大化"
    finally:
        win.close()
        store.close()


def test_caption_chrome_is_distinct_from_content(qapp, tmp_path, monkeypatch):
    from sayink.ui import design_tokens as tok

    win, store = _make_main_window(tmp_path, monkeypatch)
    try:
        css = win._caption.styleSheet()
        assert win._caption.objectName() == "mainCaption"
        assert f"background: {tok.BG}" in css
        assert f"border-bottom: 1px solid {tok.CONTROL_BORDER}" in css
        assert win._caption.height() == 42
    finally:
        win.close()
        store.close()


def test_caption_drag_moves_window(qapp, tmp_path, monkeypatch):
    from PyQt6.QtCore import QEvent, QPoint, QPointF
    from PyQt6.QtGui import QMouseEvent
    from PyQt6.QtWidgets import QApplication

    win, store = _make_main_window(tmp_path, monkeypatch)
    try:
        win.show()
        win.move(200, 160)
        QApplication.processEvents()
        origin = QPoint(win.pos())
        local = QPoint(40, 18)
        press_global = win._caption.mapToGlobal(local)
        press = QMouseEvent(
            QEvent.Type.MouseButtonPress,
            QPointF(local),
            QPointF(press_global),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(win._caption, press)
        delta = QPoint(80, 40)
        moved_local = local + delta
        move = QMouseEvent(
            QEvent.Type.MouseMove,
            QPointF(moved_local),
            QPointF(press_global + delta),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(win._caption, move)
        QApplication.processEvents()
        assert win.pos() == origin + delta
    finally:
        win.close()
        store.close()


def test_close_hides_does_not_quit(qapp, tmp_path, monkeypatch):
    win, store = _make_main_window(tmp_path, monkeypatch)
    try:
        win.show()
        win.close()
        assert win.isHidden()
        win.deleteLater()
    finally:
        store.close()


def test_show_page_embeds_hosts(qapp, tmp_path, monkeypatch):
    from sayink.config import Config
    from sayink.history_store import HistoryStore
    from sayink.ui.main_window import MainWindow
    from sayink.ui.settings_window import SettingsWindow
    monkeypatch.setattr(SettingsWindow, "_rebuild_model_cards", lambda self: None)
    monkeypatch.setattr(SettingsWindow, "_refresh_about_info", lambda self: None)
    monkeypatch.setattr(SettingsWindow, "_refresh_audio_device_lists", lambda self: None)
    store = HistoryStore(tmp_path / "h.db")
    win = MainWindow(Config(config_dir=tmp_path), store)
    try:
        assert not hasattr(win._settings, "_island_nav")
        win.show_page("engine")
        assert win._settings._pages.currentIndex() == 1
        win.show_page("history")
        assert win._stack.currentWidget() is win._history
        assert win._history._title_label.text() == "历史"
        assert not hasattr(win._history, "_close_btn")
    finally:
        win.close()
        store.close()


def test_show_page_history_refreshes_list(qapp, tmp_path, monkeypatch):
    win, store = _make_main_window(tmp_path, monkeypatch)
    try:
        refreshes: list[int] = []
        original = win._history.refresh

        def spy():
            refreshes.append(1)
            original()

        win._history.refresh = spy
        win.show_page("general")
        win.show_page("history")
        assert refreshes
    finally:
        win.close()
        store.close()

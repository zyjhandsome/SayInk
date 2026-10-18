# tests/test_main_window.py
import sys
import pytest
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt

@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication(sys.argv)


def _make_main_window(tmp_path, monkeypatch, *, with_meeting=False):
    from sayink.config import Config
    from sayink.history_store import HistoryStore
    from sayink.ui.main_window import MainWindow
    from sayink.ui.settings_window import SettingsWindow
    monkeypatch.setattr(SettingsWindow, "_rebuild_model_cards", lambda self: None)
    monkeypatch.setattr(SettingsWindow, "_refresh_about_info", lambda self: None)
    monkeypatch.setattr(SettingsWindow, "_refresh_audio_device_lists", lambda self: None)
    store = HistoryStore(tmp_path / "h.db")
    config = Config(config_dir=tmp_path)
    meeting = None
    if with_meeting:
        from sayink.meeting_session import MeetingController

        meeting = MeetingController(config, store, pending_segments=lambda: 1)
    win = MainWindow(config, store, meeting=meeting)
    return win, store

def _use_screen(monkeypatch, width, height):
    from PyQt6.QtCore import QRect
    from sayink.ui.main_window import MainWindow

    class _Screen:
        def availableGeometry(self):
            return QRect(0, 0, width, height)

    screen = _Screen()
    monkeypatch.setattr(MainWindow, "screen", lambda self: screen)


@pytest.mark.parametrize("source,mode", [
    ("microphone", "hotkey"), ("system", "hotkey"), ("mixed", "hotkey"),
    ("microphone", "continuous"), ("system", "continuous"), ("mixed", "continuous"),
])
def test_small_scaled_screen_lowers_minimum_to_fit(qapp, tmp_path, monkeypatch, source, mode):
    # 1366×768 at 150% leaves about 910×472 logical pixels above the taskbar.
    _use_screen(monkeypatch, 910, 472)
    win, store = _make_main_window(tmp_path, monkeypatch)
    try:
        win._config.set("audio.input_source", source)
        win._config.set("audio.trigger_mode", mode)
        win._config.set_output_mode("history")
        win._config.set("history.enabled", False)
        win.settings_panel.reload_settings()
        win.show()
        assert win.minimumHeight() <= 472
        assert win.height() <= 472
        assert win.width() <= 910
    finally:
        win.close()
        store.close()


@pytest.mark.parametrize("state", ["browse", "start", "live", "paused", "draining"])
def test_real_meeting_states_fit_small_screen(qapp, tmp_path, monkeypatch, state):
    from sayink.meeting_session import MeetingOptions, resolve_capabilities

    _use_screen(monkeypatch, 910, 472)
    win, store = _make_main_window(tmp_path, monkeypatch, with_meeting=True)
    controller = win._meeting_controller
    try:
        win.show()
        win.show_page("meeting")
        page = win.meeting_panel
        if state == "start":
            page.show_start_card()
        elif state != "browse":
            options = MeetingOptions(input_source="mixed")
            caps = resolve_capabilities(win._config, options, dictation_model_id="funasr-nano")
            controller.begin(options, caps)
            if state == "paused":
                controller.pause("设备不可用，请检查设备后继续")
            elif state == "draining":
                controller.request_end()
        qapp.processEvents()
        win._fit_to_available_screen()
        qapp.processEvents()
        assert win.minimumHeight() <= 472
        assert win.height() <= 472
        assert win.width() <= 910
        if state != "browse":
            button = page._card_start if state == "start" else page._end_btn
            assert button.isVisible()
            assert button.mapTo(win, button.rect().bottomRight()).y() < win.height()
        win.show_page("general")
        qapp.processEvents()
        assert win.height() <= 472
    finally:
        win.hide()
        controller.shutdown()
        store.close()


def test_meeting_sidebar_tracks_session_and_restores_dictation(qapp, tmp_path, monkeypatch):
    from sayink.meeting_session import MeetingOptions, resolve_capabilities
    from sayink.runtime_status import RuntimeState

    win, store = _make_main_window(tmp_path, monkeypatch, with_meeting=True)
    controller = win._meeting_controller
    try:
        win._settings.set_runtime_status(RuntimeState.READY, "就绪")
        options = MeetingOptions(input_source="mixed")
        controller.begin(options, resolve_capabilities(win._config, options, dictation_model_id="funasr-nano"))
        assert win._runtime_label.text() == "会议录制中"
        assert "电脑" in win._mode_label.text()
        assert win._output_label.text() == "仅保存会议记录"
        assert "Esc" not in win._shortcut_label.text()
        win.show_page("general")
        controller.pause("手动暂停")
        assert win._runtime_label.text() == "会议已暂停"
        win._settings.set_runtime_status(RuntimeState.READY, "模型已就绪")
        assert win._runtime_label.text() == "会议已暂停"
        controller.resume()
        assert win._runtime_label.text() == "会议录制中"
        controller.finalize()
        assert win._runtime_label.text() == "模型已就绪"
        assert "单次转写" in win._mode_label.text()
        assert "粘贴" in win._output_label.text()
    finally:
        win.hide()
        controller.shutdown()
        store.close()


def test_ctrl_f_uses_meeting_search_and_live_find(qapp, tmp_path, monkeypatch):
    from PyQt6.QtTest import QTest
    from sayink.meeting_session import MeetingOptions, resolve_capabilities

    win, store = _make_main_window(tmp_path, monkeypatch, with_meeting=True)
    controller = win._meeting_controller
    try:
        win.show()
        QApplication.setActiveWindow(win)
        win.show_page("meeting")
        win.setFocus()
        QTest.keyClick(win, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
        assert win.current_page() == "meeting"
        assert win.meeting_panel._search_edit.hasFocus()
        options = MeetingOptions()
        controller.begin(options, resolve_capabilities(win._config, options, dictation_model_id="funasr-nano"))
        QTest.keyClick(win, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
        assert win.current_page() == "meeting"
        assert win.meeting_panel._live_find_edit.hasFocus()
        assert win.meeting_panel._live_find_bar.isVisible()
    finally:
        win.hide()
        controller.shutdown()
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
    from PyQt6.QtGui import QColor

    from sayink.ui import design_tokens as tok

    win, store = _make_main_window(tmp_path, monkeypatch)
    try:
        css = win.styleSheet()
        margins = win.layout().contentsMargins()
        assert win.objectName() == "mainWindow"
        assert "QWidget#mainWindow" in css
        assert "border: none" in css
        assert "border-radius" not in css
        assert win._chrome_radius() == tok.WINDOW_RADIUS
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
        assert len(win._frame_edges) == 4
        color = QColor(tok.TEXT_DIM)
        for edge in win._frame_edges:
            assert edge.palette().color(edge.backgroundRole()) == color
            assert edge.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        assert win._frame_edges[0].height() == 1
        assert win._frame_edges[0].width() == win.width()
    finally:
        win.close()
        store.close()


def test_maximize_toggle_does_not_restyle_the_whole_window(qapp, tmp_path, monkeypatch):
    from PyQt6.QtCore import QEvent

    win, store = _make_main_window(tmp_path, monkeypatch)
    try:
        calls: list[str] = []
        sheets: list[str] = []
        win.reapply_theme = lambda: calls.append("theme")
        original = win.setStyleSheet

        def tracked(sheet: str) -> None:
            sheets.append(sheet)
            original(sheet)

        win.setStyleSheet = tracked
        css = win.styleSheet()
        monkeypatch.setattr(win, "isMaximized", lambda: False)
        monkeypatch.setattr(win, "showMaximized", lambda: None)
        monkeypatch.setattr(win, "showNormal", lambda: None)
        win._toggle_maximized()
        win.changeEvent(QEvent(QEvent.Type.WindowStateChange))
        assert calls == []
        assert sheets == []
        assert win.styleSheet() == css
    finally:
        win.close()
        store.close()


def test_maximized_chrome_drops_the_round_mask(qapp, tmp_path, monkeypatch):
    win, store = _make_main_window(tmp_path, monkeypatch)
    try:
        css = win.styleSheet()
        monkeypatch.setattr(win, "isMaximized", lambda: True)
        win._apply_chrome_radius()
        assert "border-radius" not in win.styleSheet()
        assert "border: none" in win.styleSheet()
        assert win.styleSheet() == css
        assert win.layout().contentsMargins().left() == 0
        assert win.mask().isEmpty()
        assert all(edge.geometry().isEmpty() for edge in win._frame_edges)
        monkeypatch.setattr(win, "isMaximized", lambda: False)
        win._sync_frame_margin()
        win._position_frame_edges()
        assert win.layout().contentsMargins().left() == 1
        assert win._frame_edges[0].height() == 1
    finally:
        win.close()
        store.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 sizing message")
def test_min_max_info_reports_minimum_track_size(qapp, tmp_path, monkeypatch):
    import ctypes
    from ctypes import wintypes

    win, store = _make_main_window(tmp_path, monkeypatch)
    try:
        monkeypatch.setattr(win, "devicePixelRatioF", lambda: 1.5)
        info = (ctypes.c_long * 10)()
        msg = wintypes.MSG()
        msg.hWnd = int(win.winId())
        msg.lParam = ctypes.addressof(info)
        assert win._fill_min_max_info(msg)
        assert (info[6], info[7]) == (
            -(-win.minimumWidth() * 3 // 2),
            -(-win.minimumHeight() * 3 // 2),
        )
    finally:
        win.close()
        store.close()


def test_live_resize_keeps_the_round_mask_off(qapp, tmp_path, monkeypatch):
    win, store = _make_main_window(tmp_path, monkeypatch)
    try:
        win.show()
        QApplication.processEvents()
        assert not win.mask().isEmpty()
        win._live_resizing = True
        win.resize(win.width() + 20, win.height() + 20)
        QApplication.processEvents()
        assert win.mask().isEmpty()
        win._live_resizing = False
        win._apply_window_shape()
        assert not win.mask().isEmpty()
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
        QApplication.processEvents()
        css = win.styleSheet()
        win._close_btn.click()
        QApplication.processEvents()
        assert win.isHidden()
        assert win.styleSheet() == css
        win.show()
        QApplication.processEvents()
        assert win.isVisible()
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


def test_selected_meeting_row_is_highlighted_inside_main_window(qapp, tmp_path, monkeypatch):
    """The page stack's background must not paint over list item widgets."""
    from sayink.config import Config
    from sayink.history_store import HistoryStore
    from sayink.meeting_session import MeetingController
    from sayink.ui.main_window import MainWindow
    from sayink.ui.settings_window import SettingsWindow

    monkeypatch.setattr(SettingsWindow, "_rebuild_model_cards", lambda self: None)
    monkeypatch.setattr(SettingsWindow, "_refresh_about_info", lambda self: None)
    monkeypatch.setattr(SettingsWindow, "_refresh_audio_device_lists", lambda self: None)
    store = HistoryStore(tmp_path / "h.db")
    config = Config(config_dir=tmp_path)
    ctrl = MeetingController(config, store, pending_segments=lambda: 0)
    for title in ("周会", "评审"):
        record = ctrl.store.create_meeting(title=title)
        ctrl.store.end_meeting(record.id)
    ctrl.store.flush()
    win = MainWindow(config, store, meeting=ctrl)
    try:
        win.resize(1024, 680)
        win.show()
        win.show_page("meeting")
        lst = win.meeting_panel._list
        lst.setCurrentRow(1)
        for _ in range(10):
            qapp.processEvents()
        def color_at(image, row):
            rect = lst.visualItemRect(lst.item(row))
            # grab() is in device pixels; item rects are logical.
            dpr = image.devicePixelRatio() or 1.0
            x = min(image.width() - 1, max(0, int((rect.right() - 4) * dpr)))
            y = min(image.height() - 1, max(0, int(rect.center().y() * dpr)))
            return image.pixelColor(x, y).name()

        image = lst.viewport().grab().toImage()
        for _ in range(20):
            qapp.processEvents()
            image = lst.viewport().grab().toImage()
            if image.width() > 0 and color_at(image, 1) != color_at(image, 0):
                break

        assert lst.item(1).isSelected()
        assert color_at(image, 1) != color_at(image, 0)
    finally:
        win.close()
        ctrl.shutdown()
        store.close()


def test_history_promote_to_meeting_navigates_to_meeting_page_and_selects_it(qapp, tmp_path, monkeypatch):
    from sayink.config import Config
    from sayink.history_store import HistoryStore, SegmentRecord
    from sayink.meeting_session import MeetingController
    from sayink.ui.main_window import MainWindow
    from sayink.ui.settings_window import SettingsWindow

    monkeypatch.setattr(SettingsWindow, "_rebuild_model_cards", lambda self: None)
    monkeypatch.setattr(SettingsWindow, "_refresh_about_info", lambda self: None)
    monkeypatch.setattr(SettingsWindow, "_refresh_audio_device_lists", lambda self: None)
    store = HistoryStore(tmp_path / "h.db")
    store.enqueue(
        SegmentRecord(
            session_id="dictation-1",
            seq=0,
            created_at=1_700_000_000_000,
            raw_text="一段要整理成会议纪要的听写内容",
            polished_text="",
            source="mic",
            duration_ms=2000,
            target_app="Notepad.exe",
            trigger_mode="continuous",
            model="funasr-nano",
        )
    )
    store.flush(timeout=2.0)
    config = Config(config_dir=tmp_path)
    ctrl = MeetingController(config, store, pending_segments=lambda: 0)
    win = MainWindow(config, store, meeting=ctrl)
    try:
        win.show()
        win.show_page("history")
        assert win.current_page() == "history"
        assert win.history_panel.session_count() == 1

        win.history_panel._to_meeting_action.trigger()
        qapp.processEvents()

        assert win.current_page() == "meeting"
        meeting_id = win.meeting_panel.current_meeting_id
        assert meeting_id != ""
        segs = ctrl.store.get_segments(meeting_id)
        assert [s.raw_text for s in segs] == ["一段要整理成会议纪要的听写内容"]
        # Dictation history still keeps the original session.
        assert [s.session_id for s in store.list_sessions()] == ["dictation-1"]
    finally:
        win.close()
        ctrl.shutdown()
        store.close()


def test_hiding_main_window_cleans_up_settings_panel_transient_state(qapp, tmp_path, monkeypatch):
    win, store = _make_main_window(tmp_path, monkeypatch)
    try:
        win.show()
        calls: list[str] = []
        monkeypatch.setattr(win.settings_panel, "_cancel_mic_probe_if_active", lambda: calls.append("mic"))
        monkeypatch.setattr(win.settings_panel, "cancel_hotkey_capture", lambda: calls.append("hotkey"))
        monkeypatch.setattr(win.settings_panel, "_flush_llm_fields", lambda: calls.append("llm"))
        win.close()
        assert calls == ["mic", "hotkey", "llm"]
    finally:
        store.close()

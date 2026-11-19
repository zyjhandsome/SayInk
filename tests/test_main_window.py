# tests/test_main_window.py
import sys
import pytest
from PyQt6 import sip
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QCoreApplication, QEvent, QTimer, Qt


_CREATED_WINDOWS: list[tuple[object, object, object]] = []


@pytest.fixture(autouse=True)
def _cleanup_main_window_handles():
    yield
    from sayink.ui.main_window import MainWindow

    app = QApplication.instance()
    seen_wins: list[object] = []
    for win, meeting, store in list(_CREATED_WINDOWS):
        if meeting is not None and hasattr(meeting, "shutdown"):
            try:
                meeting.shutdown(wait_ms=0)
            except Exception:
                pass
        if store is not None and hasattr(store, "close"):
            try:
                store.close()
            except Exception:
                pass
        if win is not None and win not in seen_wins:
            seen_wins.append(win)
    _CREATED_WINDOWS.clear()

    if app is not None:
        for top in list(app.topLevelWidgets()):
            if isinstance(top, MainWindow) and top not in seen_wins:
                seen_wins.append(top)

    for win in seen_wins:
        if sip.isdeleted(win):
            continue
        meeting_attr = getattr(win, "_meeting", None)
        if meeting_attr is not None and hasattr(meeting_attr, "shutdown"):
            try:
                meeting_attr.shutdown(wait_ms=0)
            except Exception:
                pass
        for timer in win.findChildren(QTimer):
            if not sip.isdeleted(timer):
                timer.stop()
        if hasattr(win, "_release_theme_brush"):
            win._release_theme_brush()
        win.hide()
        win.deleteLater()

    if seen_wins and app is not None:
        QCoreApplication.sendPostedEvents(None, int(QEvent.Type.DeferredDelete.value))


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
    _CREATED_WINDOWS.append((win, meeting, store))
    return win, store

def _use_screen(monkeypatch, width, height):
    from PyQt6.QtCore import QRect
    from sayink.ui.main_window import MainWindow

    class _Screen:
        def availableGeometry(self):
            return QRect(0, 0, width, height)

    screen = _Screen()
    monkeypatch.setattr(MainWindow, "screen", lambda self: screen)


def test_file_dialog_close_keeps_the_main_window(qapp, tmp_path, monkeypatch):
    win, store = _make_main_window(tmp_path, monkeypatch)
    hidden = []
    win._hide_window = lambda: hidden.append(True)
    try:
        win.begin_native_dialog()
        win.close()
        assert hidden == []
        win.end_native_dialog()
        win.close()
        assert hidden == []
        win._release_close_hold()
        win.close()
        assert hidden == [True]
    finally:
        store.close()
        win.deleteLater()


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 presentation")
def test_first_show_is_the_page_not_a_caption_sized_frame(qapp, tmp_path, monkeypatch):
    """Startup used to present the main window at ~264×143, then grow it."""
    import ctypes
    import threading
    import time
    from ctypes import wintypes

    if qapp.platformName() != "windows":
        pytest.skip("requires the native Windows Qt platform")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
    hook_type = ctypes.WINFUNCTYPE(
        None, ctypes.c_void_p, wintypes.DWORD, wintypes.HWND,
        ctypes.c_long, ctypes.c_long, wintypes.DWORD, wintypes.DWORD,
    )
    user32.SetWinEventHook.argtypes = [
        wintypes.UINT, wintypes.UINT, wintypes.HMODULE, hook_type,
        wintypes.DWORD, wintypes.DWORD, wintypes.UINT,
    ]
    user32.SetWinEventHook.restype = ctypes.c_void_p
    user32.UnhookWinEvent.argtypes = [ctypes.c_void_p]
    user32.PeekMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT,
    ]
    sizes: list[tuple[int, int]] = []

    @hook_type
    def on_show(_hook, _event, hwnd, obj, child, _thread, _when):
        if obj != 0 or child != 0:
            return
        name = ctypes.create_unicode_buffer(120)
        user32.GetClassNameW(hwnd, name, 120)
        if "QWindowIcon" not in name.value:
            return
        rect = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        sizes.append((rect.right - rect.left, rect.bottom - rect.top))

    stop = threading.Event()

    def pump():
        hook = user32.SetWinEventHook(0x8002, 0x8002, None, on_show, 0, 0, 0)
        message = wintypes.MSG()
        while not stop.is_set():
            while user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
            time.sleep(0.005)
        user32.UnhookWinEvent(hook)

    thread = threading.Thread(target=pump, daemon=True)
    thread.start()
    time.sleep(0.15)
    win, store = _make_main_window(tmp_path, monkeypatch)
    try:
        win.show()
        qapp.processEvents()
        time.sleep(0.2)
        ratio = win.devicePixelRatioF() or 1.0
        floor_w = int(win.width() * ratio) - 32
        floor_h = int(win.height() * ratio) - 32
        assert sizes, "the main window never became visible"
        assert all(width >= floor_w and height >= floor_h for width, height in sizes)
    finally:
        stop.set()
        thread.join(1)
        win.hide()
        store.close()
        win.deleteLater()


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
        assert win._runtime_label.text() == "会议记录中"
        assert win._status_tone() == "STATE_RECORD"
        # Same source name as the dictation hints / source picker.
        assert win._mode_label.text() == "麦克风 + 电脑声音"
        assert win._output_label.text() == "仅保存会议记录"
        assert "Esc" not in win._shortcut_label.text()
        win.show_page("general")
        controller.pause("手动暂停")
        assert win._runtime_label.text() == "会议已暂停"
        assert win._status_tone() == "AMBER"
        win._settings.set_runtime_status(RuntimeState.READY, "模型已就绪")
        assert win._runtime_label.text() == "会议已暂停"
        controller.resume()
        assert win._runtime_label.text() == "会议记录中"
        controller.finalize()
        assert win._runtime_label.text() == "模型已就绪"
        assert win._status_tone() == "GREEN"
        assert "单次转写" in win._mode_label.text()
        assert win._output_label.text() == "输入当前光标"
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
        assert (win.minimumWidth(), win.minimumHeight()) == (560, 480)
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
        expects_region = not win._dwm_round_corners
        assert win.mask().isEmpty() is not expects_region
        win._live_resizing = True
        win.resize(win.width() + 20, win.height() + 20)
        QApplication.processEvents()
        assert win.mask().isEmpty()
        win._live_resizing = False
        win._apply_window_shape()
        assert win.mask().isEmpty() is not expects_region
    finally:
        win.close()
        store.close()


def _post_native(win, message: int) -> None:
    import ctypes
    from ctypes import wintypes

    msg = wintypes.MSG()
    msg.hWnd = int(win.winId())
    msg.message = message
    win.nativeEvent(b"windows_generic_MSG", ctypes.addressof(msg))


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 move loop")
def test_caption_move_keeps_the_round_mask(qapp, tmp_path, monkeypatch):
    """A title-bar drag must not drop the mask: that invalidates the whole window."""
    from sayink.ui.main_window import _WM_ENTERSIZEMOVE, _WM_EXITSIZEMOVE, _WM_SIZING

    win, store = _make_main_window(tmp_path, monkeypatch)
    try:
        win.show()
        QApplication.processEvents()
        expects_region = not win._dwm_round_corners
        assert win.mask().isEmpty() is not expects_region
        _post_native(win, _WM_ENTERSIZEMOVE)
        assert win._live_resizing is True
        # A title-bar move must not drop a region: that invalidates every pixel.
        assert win.mask().isEmpty() is not expects_region
        _post_native(win, _WM_EXITSIZEMOVE)
        assert win._live_resizing is False
        assert win.mask().isEmpty() is not expects_region
        _post_native(win, _WM_ENTERSIZEMOVE)
        _post_native(win, _WM_SIZING)
        assert win.mask().isEmpty()
    finally:
        win.close()
        store.close()


def _nccalc_message(win, *, bottom: int, old_bottom: int):
    import ctypes
    from ctypes import wintypes

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    class PARAMS(ctypes.Structure):
        _fields_ = [("rgrc", RECT * 3), ("lppos", ctypes.c_void_p)]

    params = PARAMS()
    params.rgrc[0].left = 10
    params.rgrc[0].top = 20
    params.rgrc[0].right = 410
    params.rgrc[0].bottom = bottom
    params.rgrc[1].left = 10
    params.rgrc[1].top = 20
    params.rgrc[1].right = 410
    params.rgrc[1].bottom = old_bottom
    msg = wintypes.MSG()
    msg.hWnd = int(win.winId())
    msg.message = 0x0083  # WM_NCCALCSIZE
    msg.wParam = 1
    msg.lParam = ctypes.addressof(params)
    return msg, params


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 client rect")
def test_resting_client_rect_extends_one_pixel_past_the_bottom(qapp, tmp_path, monkeypatch):
    win, store = _make_main_window(tmp_path, monkeypatch)
    try:
        msg, params = _nccalc_message(win, bottom=300, old_bottom=300)
        handled, result = win.nativeEvent(b"windows_generic_MSG", __import__("ctypes").addressof(msg))
        assert handled is True
        assert result == 0
        assert params.rgrc[0].bottom == 301

        msg, params = _nccalc_message(win, bottom=300, old_bottom=300)
        win.nativeEvent(b"windows_generic_MSG", __import__("ctypes").addressof(msg))
        assert params.rgrc[0].bottom == 301
    finally:
        win.close()
        store.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 client rect")
def test_maximized_client_rect_is_not_extended(qapp, tmp_path, monkeypatch):
    win, store = _make_main_window(tmp_path, monkeypatch)
    try:
        monkeypatch.setattr(win, "_is_zoomed", lambda hwnd: True)
        msg, params = _nccalc_message(win, bottom=300, old_bottom=280)
        handled, result = win.nativeEvent(b"windows_generic_MSG", __import__("ctypes").addressof(msg))
        assert handled is True
        assert result == 0
        assert params.rgrc[0].bottom == 300
    finally:
        win.close()
        store.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 client rect")
@pytest.mark.parametrize("edge", [1, 2, 3, 4, 5, 6, 7, 8])
def test_live_edge_drag_keeps_existing_client_pixels_pinned_top_left(qapp, tmp_path, monkeypatch, edge):
    win, store = _make_main_window(tmp_path, monkeypatch)
    try:
        win._live_resizing = True
        win._sizing_edge = edge
        msg, params = _nccalc_message(win, bottom=320, old_bottom=300)
        handled, result = win.nativeEvent(b"windows_generic_MSG", __import__("ctypes").addressof(msg))
        assert handled is True
        # Top-left alignment (0) across all 8 edges keeps the custom titlebar,
        # left sidebar, and page headers pinned to (0, 0) instead of shifting
        # them by (dx, dy) before QBackingStore flushes.
        assert result == 0
        # The resting one-pixel extension is off while an edge drag is in progress.
        assert params.rgrc[0].bottom == 320
        assert params.rgrc[1].bottom == 300
        assert params.rgrc[2].bottom == 0
    finally:
        win.close()
        store.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 client rect")
def test_caption_move_preserves_resting_bottom_nudge(qapp, tmp_path, monkeypatch):
    """WM_ENTERSIZEMOVE without WM_SIZING (caption drag) must not toggle the 1px nudge."""
    from sayink.ui.main_window import _WM_ENTERSIZEMOVE

    win, store = _make_main_window(tmp_path, monkeypatch)
    try:
        _post_native(win, _WM_ENTERSIZEMOVE)
        assert win._live_resizing is True
        assert win._sizing_edge == 0
        msg, params = _nccalc_message(win, bottom=300, old_bottom=300)
        handled, result = win.nativeEvent(b"windows_generic_MSG", __import__("ctypes").addressof(msg))
        assert handled is True
        assert result == 0
        assert params.rgrc[0].bottom == 301
    finally:
        win.close()
        store.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 resize bits")
def test_live_resize_does_not_discard_client_bits(qapp, tmp_path, monkeypatch):
    import ctypes
    from ctypes import wintypes

    win, store = _make_main_window(tmp_path, monkeypatch)
    try:
        class WINDOWPOS(ctypes.Structure):
            _fields_ = [
                ("hwnd", wintypes.HWND),
                ("hwndInsertAfter", wintypes.HWND),
                ("x", ctypes.c_int),
                ("y", ctypes.c_int),
                ("cx", ctypes.c_int),
                ("cy", ctypes.c_int),
                ("flags", ctypes.c_uint),
            ]

        pos = WINDOWPOS()
        pos.cx, pos.cy = 900, 640
        pos.flags = 0x0100  # SWP_NOCOPYBITS
        msg = wintypes.MSG()
        msg.hWnd = int(win.winId())
        msg.message = 0x0046  # WM_WINDOWPOSCHANGING
        msg.lParam = ctypes.addressof(pos)
        win._live_resizing = True
        handled, result = win.nativeEvent(b"windows_generic_MSG", ctypes.addressof(msg))
        assert handled is True
        assert result == 0
        assert pos.flags & 0x0100 == 0

        pos.flags = 0x0001 | 0x0100  # a move keeps Qt's own handling
        handled, _result = win.nativeEvent(b"windows_generic_MSG", ctypes.addressof(msg))
        assert handled is False

        win._live_resizing = False
        pos.flags = 0x0100
        handled, _result = win.nativeEvent(b"windows_generic_MSG", ctypes.addressof(msg))
        assert handled is False
    finally:
        win.close()
        store.close()


def test_size_grip_drag_does_not_swap_the_round_mask(qapp, tmp_path, monkeypatch):
    from PyQt6.QtCore import QEvent, QPointF
    from PyQt6.QtGui import QMouseEvent

    win, store = _make_main_window(tmp_path, monkeypatch)
    try:
        win.show()
        qapp.processEvents()
        expects_region = not win._dwm_round_corners
        assert win.mask().isEmpty() is not expects_region
        local = QPointF(8, 8)
        press = QMouseEvent(
            QEvent.Type.MouseButtonPress,
            local,
            local,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(win._size_grip, press)
        assert win._live_resizing is True
        win.resize(win.width() + 24, win.height() + 16)
        qapp.processEvents()
        assert win.mask().isEmpty()
        release = QMouseEvent(
            QEvent.Type.MouseButtonRelease,
            local,
            local,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(win._size_grip, release)
        assert win._live_resizing is False
        assert win.mask().isEmpty() is not expects_region
    finally:
        win.close()
        store.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 erase")
def test_erase_background_is_handled_with_the_theme_color(qapp, tmp_path, monkeypatch):
    import ctypes
    from ctypes import wintypes

    from PyQt6.QtGui import QColor

    from sayink.ui import design_tokens as tok
    from sayink.ui.main_window import _win32_draw

    win = store = user32 = gdi32 = mem = bitmap = None
    try:
        tok.activate("dark")
        win, store = _make_main_window(tmp_path, monkeypatch)
        win.reapply_theme()
        user32, gdi32, rect_type, byref = _win32_draw()
        gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
        gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
        gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
        gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        gdi32.SelectObject.restype = ctypes.c_void_p
        gdi32.GetPixel.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        gdi32.GetPixel.restype = ctypes.c_uint
        gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
        gdi32.DeleteDC.restype = ctypes.c_int
        screen = user32.GetDC(0)
        mem = gdi32.CreateCompatibleDC(screen)
        bitmap = gdi32.CreateCompatibleBitmap(screen, 8, 8)
        assert mem and bitmap
        gdi32.SelectObject(mem, bitmap)
        user32.ReleaseDC(0, screen)
        white = gdi32.CreateSolidBrush(0x00FFFFFF)
        rect = rect_type(0, 0, 8, 8)
        user32.FillRect(mem, byref(rect), white)
        gdi32.DeleteObject(white)
        assert gdi32.GetPixel(mem, 1, 1) == 0x00FFFFFF
        win._fill_dc(mem, rect)
        color = QColor(tok.BG)
        expected = color.red() | (color.green() << 8) | (color.blue() << 16)
        assert gdi32.GetPixel(mem, 1, 1) == expected

        msg = wintypes.MSG()
        msg.message = 0x0014
        handled, result = win.nativeEvent(b"windows_generic_MSG", ctypes.addressof(msg))
        assert handled is True
        assert result == 1
        msg.message = 0x0085  # WM_NCPAINT
        handled, result = win.nativeEvent(b"windows_generic_MSG", ctypes.addressof(msg))
        assert handled is True
        assert result == 0
        msg.message = 0x0086  # WM_NCACTIVATE
        handled, result = win.nativeEvent(b"windows_generic_MSG", ctypes.addressof(msg))
        assert handled is True
        assert result == 1
        win._bury_surrogate_titlebars()
        win._sync_native_size()
    finally:
        tok.activate("light")
        if gdi32 is not None and mem:
            gdi32.DeleteDC(mem)
        if gdi32 is not None and bitmap:
            gdi32.DeleteObject(bitmap)
        if win is not None:
            win._release_theme_brush()
            win.close()
        if store is not None:
            store.close()


def test_uncloak_repaints_so_the_desktop_cannot_show_through(qapp, tmp_path, monkeypatch):
    win, store = _make_main_window(tmp_path, monkeypatch)
    paints: list[bool] = []
    try:
        win.update = lambda *args, **kwargs: paints.append(True)
        win._native_frame_is_page_sized = lambda: True
        win._set_presentation_cloak = lambda cloaked: None
        win._sync_native_size = lambda: None
        win._finish_presentation()
        assert paints == [True]
        sheet = win._settings.general_page.viewport().styleSheet()
        assert "settingsPageViewport" in sheet
        assert "transparent" not in sheet
        from sayink.ui import design_tokens as tok

        assert tok.BG in sheet
    finally:
        store.close()
        win.deleteLater()


def test_dwm_frame_uses_the_theme_background(qapp, tmp_path, monkeypatch):
    from PyQt6.QtGui import QColor, QPalette

    from sayink.ui import design_tokens as tok

    def colorref(value: str) -> int:
        color = QColor(value)
        return color.red() | (color.green() << 8) | (color.blue() << 16)

    win, store = _make_main_window(tmp_path, monkeypatch)
    try:
        tok.activate("dark")
        win.reapply_theme()
        QApplication.processEvents()
        attrs = win._dwm_frame_attributes()
        assert attrs[3] == 1
        assert attrs[20] == 1
        assert attrs[19] == 1
        assert attrs[35] == colorref(tok.BG)
        assert attrs[34] == colorref(tok.BG)
        assert attrs[38] == 1
        assert win.palette().color(QPalette.ColorRole.Window) == QColor(tok.BG)
        assert win.testAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        assert win.testAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        tok.activate("light")
        win.reapply_theme()
        QApplication.processEvents()
        attrs = win._dwm_frame_attributes()
        assert attrs[20] == 0
        assert attrs[35] == colorref(tok.BG)
        assert win.palette().color(QPalette.ColorRole.Window) == QColor(tok.BG)
    finally:
        tok.activate("light")
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


def test_window_is_built_hidden_and_stays_one_size_after_show(qapp, tmp_path, monkeypatch):
    win, store = _make_main_window(tmp_path, monkeypatch)
    try:
        assert not win.isVisible()
        assert win.updatesEnabled()
        size_before_show = (win.width(), win.height())
        win.show()
        qapp.processEvents()
        assert win.isVisible()
        assert win.updatesEnabled()
        for _ in range(4):
            qapp.processEvents()
        assert (win.width(), win.height()) == size_before_show
        assert win.mask().isEmpty() == bool(win._dwm_round_corners)
    finally:
        win.close()
        store.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 window region")
def test_rounded_corners_do_not_eat_the_resize_handles(qapp, tmp_path, monkeypatch):
    """The four corners must stay inside the window, or a drag there hits whatever is behind."""
    import ctypes
    from ctypes import wintypes

    win, store = _make_main_window(tmp_path, monkeypatch)
    try:
        win.show()
        qapp.processEvents()
        if not win._dwm_round_corners:
            pytest.skip("DWM rounded corners are unavailable")
        assert win.mask().isEmpty()
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.WindowFromPoint.argtypes = [wintypes.POINT]
        user32.WindowFromPoint.restype = wintypes.HWND
        user32.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, ctypes.c_uint,
        ]
        hwnd = int(win.winId())
        user32.SetWindowPos(hwnd, wintypes.HWND(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
        qapp.processEvents()
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        points = (
            (rect.left + 2, rect.top + 2),
            (rect.right - 3, rect.top + 2),
            (rect.left + 2, rect.bottom - 3),
            (rect.right - 3, rect.bottom - 3),
            (rect.left + 2, (rect.top + rect.bottom) // 2),
            ((rect.left + rect.right) // 2, rect.bottom - 3),
        )
        try:
            for x, y in points:
                assert int(user32.WindowFromPoint(wintypes.POINT(x, y)) or 0) == hwnd
        finally:
            user32.SetWindowPos(hwnd, wintypes.HWND(-2), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
        width, height = win.width(), win.height()
        for point in (
            (1, 1),
            (width - 2, 1),
            (1, height - 2),
            (width - 2, height - 2),
            (1, height // 2),
            (width // 2, height - 2),
        ):
            assert win._hit_test(*point) not in (None, 1, 2)
    finally:
        win.close()
        store.close()


def test_top_resize_border_does_not_drag_the_caption(qapp, tmp_path, monkeypatch):
    from PyQt6.QtCore import QEvent, QPoint, QPointF
    from PyQt6.QtGui import QMouseEvent

    win, store = _make_main_window(tmp_path, monkeypatch)
    try:
        win.show()
        win.move(200, 160)
        qapp.processEvents()
        origin = win.pos()
        local = QPoint(40, 0)
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
        move = QMouseEvent(
            QEvent.Type.MouseMove,
            QPointF(local + QPoint(30, 24)),
            QPointF(press_global + QPoint(30, 24)),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(win._caption, move)
        qapp.processEvents()
        assert win.pos() == origin
        assert win._drag_offset is None
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


@pytest.mark.parametrize("maximized", [False, True])
def test_reopening_minimized_window_restores_previous_size(qapp, tmp_path, monkeypatch, maximized):
    win, store = _make_main_window(tmp_path, monkeypatch)
    try:
        if maximized:
            win.showMaximized()
        else:
            win.show()
        qapp.processEvents()
        previous_size = win.size()

        win.showMinimized()
        qapp.processEvents()
        assert win.isMinimized()

        win.show()
        qapp.processEvents()
        assert win.isVisible()
        assert not win.isMinimized()
        assert win.isMaximized() is maximized
        assert win.size() == previous_size
    finally:
        win.close()
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
            raw_text="一段要整理成会议纪要的转写内容",
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
        assert [s.raw_text for s in segs] == ["一段要整理成会议纪要的转写内容"]
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


def test_failed_minutes_save_keeps_the_meeting_page(qapp, tmp_path, monkeypatch):
    win, store = _make_main_window(tmp_path, monkeypatch, with_meeting=True)
    controller = win._meeting_controller
    try:
        record = controller.store.create_meeting(title="评审")
        controller.store.save_notes(
            record.id, template_id="general", model="m", json_payload="", markdown="原纪要",
        )
        controller.store.flush()
        win.show()
        win.show_page("meeting")
        page = win.meeting_panel
        page.show_meeting(record.id)
        page._toggle_edit()
        page._notes_edit.setPlainText("不能丢的草稿")
        real_save = controller.save_user_markdown
        monkeypatch.setattr(controller, "save_user_markdown", lambda *_args, **_kwargs: False)
        win._nav_buttons[2].click()
        qapp.processEvents()
        assert win.current_page() == "meeting"
        assert win._nav_buttons[1].isChecked()
        assert page.isVisible()
        assert page._editing_notes
        assert page._notes_edit.toPlainText() == "不能丢的草稿"
        assert page._status_label.isVisible()
        assert "尚未保存" in page._status_label.text()
        monkeypatch.setattr(controller, "save_user_markdown", real_save)
        win.show_page("general")
        assert win.current_page() == "general"
    finally:
        win.close()
        controller.shutdown()
        store.close()


def test_caption_hit_keeps_buttons_clickable_and_title_draggable(qapp, tmp_path, monkeypatch):
    from PyQt6.QtCore import QPoint

    from sayink.ui.main_window import _HTCAPTION, _HTMAXBUTTON

    win, store = _make_main_window(tmp_path, monkeypatch)
    try:
        win.show()
        qapp.processEvents()
        title = win._title.mapTo(win, QPoint(2, 2))
        assert win._hit_test(title.x(), title.y()) == _HTCAPTION
        # Caption buttons stay in the client area. Reporting HTMAXBUTTON makes
        # Windows 11 paint its own glyph over the close button and open snap
        # layouts instead of delivering the click that maximizes the window.
        for btn in (win._min_btn, win._max_btn, win._close_btn):
            point = btn.mapTo(win, QPoint(4, 4))
            assert win._hit_test(point.x(), point.y()) is None
        calls: list[str] = []
        monkeypatch.setattr(win, "isMaximized", lambda: False)
        monkeypatch.setattr(win, "showMaximized", lambda: calls.append("max"))
        win._max_btn.click()
        assert calls == ["max"]
        edge = win._hit_test(2, win.height() // 2)
        assert edge not in (None, _HTCAPTION, _HTMAXBUTTON)
        monkeypatch.setattr(win, "isMaximized", lambda: True)
        assert win._hit_test(title.x(), title.y()) == _HTCAPTION
        assert win._hit_test(2, win.height() // 2) is None
    finally:
        win.close()
        store.close()


def test_post_meeting_tools_stay_on_one_row_at_minimum_window(qapp, tmp_path, monkeypatch):
    from PyQt6.QtCore import QPoint

    win, store = _make_main_window(tmp_path, monkeypatch, with_meeting=True)
    controller = win._meeting_controller
    try:
        record = controller.store.create_meeting(title="评审")
        controller.store.end_meeting(record.id, ended_at=record.started_at + 1000)
        controller.store.save_notes(
            record.id, template_id="general", model="m", json_payload="", markdown="纪要正文",
        )
        controller.store.flush()
        win.show()
        win.resize(880, 580)
        win.show_page("meeting")
        page = win.meeting_panel
        page.show_meeting(record.id)
        qapp.processEvents()
        action_y = page._generate_btn.mapTo(page, QPoint(0, 0)).y()
        menu_y = page._template_menu_btn.mapTo(page, QPoint(0, 0)).y()
        assert page._template_menu_btn.isVisible()
        assert not page._template_label.isVisible()
        assert abs(menu_y - action_y) <= 4
        assert page._notes_bar.height() <= page._generate_btn.height() + 8
        assert "管理模板" in [action.text() for action in page._template_menu.actions()]
        assert page._copy_btn.isEnabled()
        from sayink.ui import settings_styles as ss
        disabled = ss.BTN_ACCENT_SM.split("QPushButton:disabled", 1)[1]
        ghost_disabled = ss.BTN_GHOST_SM.split("QPushButton:disabled", 1)[1]
        assert "font-weight: 400" in disabled
        assert "font-weight: 400" in ghost_disabled or "font-weight: 700" not in ghost_disabled
    finally:
        win.close()
        controller.shutdown()
        store.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 erase during live resize")
def test_erase_background_fills_exposed_strip_during_live_resize(qapp, tmp_path, monkeypatch):
    """Outward edge drags must erase newly exposed strips with tok.BG instead of leaving black."""
    import ctypes
    from ctypes import wintypes
    from PyQt6.QtGui import QColor
    from sayink.ui import design_tokens as tok
    from sayink.ui.main_window import _WMSZ_TOPRIGHT, _win32_draw

    if qapp.platformName() != "windows":
        pytest.skip("requires the native Windows Qt platform")

    win = store = user32 = gdi32 = mem = bitmap = None
    try:
        tok.activate("dark")
        win, store = _make_main_window(tmp_path, monkeypatch)
        win.reapply_theme()
        user32, gdi32, rect_type, byref = _win32_draw()
        gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
        gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
        gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
        gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        gdi32.SelectObject.restype = ctypes.c_void_p
        gdi32.GetPixel.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        gdi32.GetPixel.restype = ctypes.c_uint
        gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
        gdi32.DeleteDC.restype = ctypes.c_int

        screen = user32.GetDC(0)
        mem = gdi32.CreateCompatibleDC(screen)
        bitmap = gdi32.CreateCompatibleBitmap(screen, 16, 16)
        gdi32.SelectObject(mem, bitmap)
        user32.ReleaseDC(0, screen)

        black = gdi32.CreateSolidBrush(0x00000000)
        user32.FillRect(mem, byref(rect_type(0, 0, 16, 16)), black)
        gdi32.DeleteObject(black)
        assert gdi32.GetPixel(mem, 4, 4) == 0x00000000

        win._live_resizing = True
        win._sizing_edge = _WMSZ_TOPRIGHT
        msg = wintypes.MSG()
        msg.hWnd = int(win.winId())
        msg.message = 0x0014  # WM_ERASEBKGND
        msg.wParam = int(mem)
        handled, result = win.nativeEvent(b"windows_generic_MSG", ctypes.addressof(msg))
        assert (handled, result) == (True, 1)

        c = QColor(tok.BG)
        expected = c.red() | (c.green() << 8) | (c.blue() << 16)
        assert gdi32.GetPixel(mem, 4, 4) == expected
    finally:
        tok.activate("light")
        if gdi32 is not None and mem:
            gdi32.DeleteDC(mem)
        if gdi32 is not None and bitmap:
            gdi32.DeleteObject(bitmap)
        if win is not None:
            win._release_theme_brush()
            win.close()
        if store is not None:
            store.close()


def test_follow_resize_and_size_grip_release_flush_live_resize_callbacks(qapp, tmp_path, monkeypatch):
    from PyQt6.QtCore import QEvent, QPointF
    from PyQt6.QtGui import QMouseEvent
    from sayink.ui import flow_layout

    win, store = _make_main_window(tmp_path, monkeypatch)
    flushed: list[object] = []
    monkeypatch.setattr(flow_layout, "flush_live_resize", lambda w: flushed.append(w), raising=False)
    try:
        win.show()
        qapp.processEvents()
        flushed.clear()

        local = QPointF(8, 8)
        press = QMouseEvent(
            QEvent.Type.MouseButtonPress,
            local,
            local,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(win._size_grip, press)
        assert win._live_resizing is True
        win.resize(win.width() + 20, win.height() + 12)
        assert getattr(win, "_resize_follow_timer", None) is not None
        assert win._resize_follow_timer.isActive()
        assert flushed == []

        release = QMouseEvent(
            QEvent.Type.MouseButtonRelease,
            local,
            local,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(win._size_grip, release)
        assert win._live_resizing is False
        assert flushed == [win]
        assert not win._resize_follow_timer.isActive()
    finally:
        win.close()
        store.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 live resize synchronization")
def test_win32_live_resize_top_right_grow_and_shrink_syncs_surface_and_restores_nudge(qapp, tmp_path, monkeypatch):
    """Real Win32 sizing sequence across TOPRIGHT grow & shrink must not shift chrome,
    flash black, skip shrink WM_PAINT, or lose the 1px resting bottom nudge on exit.
    """
    import ctypes
    from ctypes import wintypes
    from sayink.ui import design_tokens as tok
    from sayink.ui.main_window import _WMSZ_TOPRIGHT, _WM_ENTERSIZEMOVE, _WM_EXITSIZEMOVE, _WM_SIZING

    if qapp.platformName() != "windows":
        pytest.skip("requires the native Windows Qt platform")

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, ctypes.c_uint,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.UpdateWindow.argtypes = [wintypes.HWND]
    user32.UpdateWindow.restype = wintypes.BOOL
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetUpdateRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT), wintypes.BOOL]
    user32.GetUpdateRect.restype = wintypes.BOOL

    tok.activate("dark")
    win, store = _make_main_window(tmp_path, monkeypatch)
    try:
        win.reapply_theme()
        win.move(200, 160)
        win.resize(900, 600)
        win.show()
        for _ in range(6):
            qapp.processEvents()

        hwnd = int(win.winId())
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        swp_flags = 0x0004 | 0x0010  # SWP_NOZORDER | SWP_NOACTIVATE

        user32.SendMessageW(hwnd, _WM_ENTERSIZEMOVE, 0, 0)
        # 1) Outward TOPRIGHT step (+24w, +24h, top -24)
        grow = wintypes.RECT(rect.left, rect.top - 24, rect.right + 24, rect.bottom)
        user32.SendMessageW(hwnd, _WM_SIZING, _WMSZ_TOPRIGHT, ctypes.addressof(grow))
        user32.SetWindowPos(hwnd, 0, grow.left, grow.top, grow.right - grow.left, grow.bottom - grow.top, swp_flags)
        user32.UpdateWindow(hwnd)

        # 2) Inward TOPRIGHT shrink step (-24w, -24h, top +24): must STILL mark an invalid
        # rect despite SWP_NOCOPYBITS being cleared so UpdateWindow dispatches WM_PAINT!
        shrink = wintypes.RECT(rect.left, rect.top, rect.right, rect.bottom)
        user32.SendMessageW(hwnd, _WM_SIZING, _WMSZ_TOPRIGHT, ctypes.addressof(shrink))
        user32.SetWindowPos(hwnd, 0, shrink.left, shrink.top, shrink.right - shrink.left, shrink.bottom - shrink.top, swp_flags)
        upd = wintypes.RECT()
        assert bool(user32.GetUpdateRect(hwnd, ctypes.byref(upd), False)) is True
        user32.UpdateWindow(hwnd)
        assert (win.width(), win.height()) == (900, 600)

        user32.SendMessageW(hwnd, _WM_EXITSIZEMOVE, 0, 0)
        qapp.processEvents()

        wrect = wintypes.RECT()
        crect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(wrect))
        user32.GetClientRect(hwnd, ctypes.byref(crect))
        assert crect.bottom - (wrect.bottom - wrect.top) == 1
    finally:
        tok.activate("light")
        win.close()
        store.close()


def test_live_resize_clears_timers_on_hide_or_ungrab_and_dedupes_exit_follow(qapp, tmp_path, monkeypatch):
    from PyQt6.QtCore import QEvent
    from sayink.ui import flow_layout

    win, store = _make_main_window(tmp_path, monkeypatch)
    flushed: list[object] = []
    orig_flush = flow_layout.flush_live_resize

    def track_flush(w):
        flushed.append(w)
        orig_flush(w)

    monkeypatch.setattr(flow_layout, "flush_live_resize", track_flush)
    try:
        win.show()
        qapp.processEvents()
        flushed.clear()

        # 1) Hiding window mid-drag clears _live_resizing, stops both 32ms timers, and flushes callbacks
        flow_layout.set_live_resizing(win, True)
        flow_layout.defer_while_live_resize(win.history_panel, lambda: None)
        win.resize(win.width() + 18, win.height() + 10)
        assert win._live_resizing is True
        assert win._resize_follow_timer.isActive()
        assert win._live_resize_timer.isActive()

        win._hide_window()
        assert win._live_resizing is False
        assert not win._resize_follow_timer.isActive()
        assert not win._live_resize_timer.isActive()
        assert win in flushed

        # 2) UngrabMouse mid-drag also ends live resize cleanly
        win.show()
        qapp.processEvents()
        flushed.clear()
        flow_layout.set_live_resizing(win, True)
        flow_layout.defer_while_live_resize(win.history_panel, lambda: None)
        win.resize(win.width() + 12, win.height() + 8)
        assert win._live_resizing is True
        assert win._live_resize_timer.isActive()
        QApplication.sendEvent(win, QEvent(QEvent.Type.UngrabMouse))
        assert win._live_resizing is False
        assert not win._resize_follow_timer.isActive()
        assert not win._live_resize_timer.isActive()
        assert win in flushed

        # 3) If _restore_resting_nudge synchronously triggers _follow_resize, _end_live_resize
        # does not invoke _follow_resize a second time.
        follow_calls = 0
        orig_follow = win._follow_resize

        def counted_follow():
            nonlocal follow_calls
            follow_calls += 1
            orig_follow()

        monkeypatch.setattr(win, "_follow_resize", counted_follow)
        monkeypatch.setattr(win, "_restore_resting_nudge", counted_follow)
        win._live_resizing = True
        win._nudge_suspended_for_resize = True
        win._end_live_resize()
        assert follow_calls == 1
    finally:
        win.close()
        store.close()


"""Tray icon activation behavior tests."""

from __future__ import annotations

import sys

import pytest
from PyQt6.QtCore import QRect, QSize
from PyQt6.QtWidgets import QSystemTrayIcon

from sayink.ui.tray_icon import TrayIcon, tray_menu_top_left


@pytest.fixture
def tray(qapp):
    icon = TrayIcon()
    yield icon
    icon.hide()
    icon.deleteLater()


class TestTrayMenuOpensAboveTheIcon:
    def test_bottom_tray_menu_sits_above_the_icon(self):
        menu = QSize(180, 220)
        anchor = QRect(1700, 1040, 24, 40)
        available = QRect(0, 0, 1920, 1040)

        pos = tray_menu_top_left(menu, anchor, available)

        assert pos.y() + menu.height() <= anchor.y()
        assert pos.y() >= available.top()
        assert pos.x() + menu.width() <= available.right() + 1

    def test_top_tray_menu_opens_downward(self):
        menu = QSize(180, 220)
        anchor = QRect(1700, 0, 24, 40)
        available = QRect(0, 48, 1920, 1032)

        pos = tray_menu_top_left(menu, anchor, available)

        assert pos.y() >= available.top()
        assert pos.y() + menu.height() <= available.top() + available.height()

    def test_context_menu_uses_the_upward_popup(self, tray):
        from sayink.ui.tray_icon import _UpwardContextMenu

        assert isinstance(tray.contextMenu(), _UpwardContextMenu)


class TestTrayActivation:
    def test_windows_double_click_opens_once(self, tray, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        emitted: list[object] = []
        tray.wake_island.connect(lambda: emitted.append(True))

        tray._on_activated(QSystemTrayIcon.ActivationReason.Trigger)
        tray._on_activated(QSystemTrayIcon.ActivationReason.DoubleClick)

        assert len(emitted) == 1
        assert not tray._single_click_timer.isActive()

    def test_windows_single_click_opens_immediately_without_double_click_delay(self, tray, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        emitted: list[object] = []
        tray.wake_island.connect(lambda: emitted.append(True))

        tray._on_activated(QSystemTrayIcon.ActivationReason.Trigger)

        assert len(emitted) == 1
        assert not tray._single_click_timer.isActive()

    def test_right_click_cancels_queued_wake(self, tray, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        emitted: list[object] = []
        tray.wake_island.connect(lambda: emitted.append(True))
        tray._request_wake(defer_from_sync_win32=True)
        assert tray._wake_queued is True

        tray._on_activated(QSystemTrayIcon.ActivationReason.Context)

        assert tray._wake_queued is False
        assert emitted == []

    def test_double_click_waits_until_the_context_menu_closes(self, tray, qapp, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        emitted: list[object] = []
        tray.wake_island.connect(lambda: emitted.append(True))
        tray._on_menu_about_to_show()

        tray._on_activated(QSystemTrayIcon.ActivationReason.DoubleClick)

        assert emitted == []
        tray._on_menu_about_to_hide()
        qapp.processEvents()
        assert emitted == [True]

    def test_non_windows_uses_single_trigger(self, tray, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        emitted: list[object] = []
        tray.wake_island.connect(lambda: emitted.append(True))

        tray._on_activated(QSystemTrayIcon.ActivationReason.Trigger)

        assert len(emitted) == 1

    def test_status_summary_is_disabled_first_menu_action_and_updates_tooltip(self, tray):
        tray.set_status_summary("就绪 · FireRedASR2")

        action = tray.contextMenu().actions()[0]
        assert action.text() == "就绪 · FireRedASR2"
        assert not action.isEnabled()
        assert "FireRedASR2" in tray.toolTip()

    def test_activity_tooltip_uses_capsule_words(self, tray):
        tray.set_status_summary("就绪")
        tray.set_activity_tooltip("listening")
        assert tray.toolTip() == "SayInk - 正在听"
        tray.set_activity_tooltip("recognizing")
        assert tray.toolTip() == "SayInk - 正在识别"
        tray.set_activity_tooltip("loading")
        assert tray.toolTip() == "SayInk - 模型载入中"
        tray.set_activity_tooltip(None)
        assert tray.toolTip() == "SayInk - 就绪"


class TestTrayMenuStyleAndGrouping:
    def test_menu_stylesheet_uses_reference_style_tokens(self, tray):
        from sayink.ui import design_tokens as t

        css = tray.contextMenu().styleSheet()
        assert t.TRAY_MENU_RADIUS == 8
        assert f"border-radius: {t.TRAY_MENU_RADIUS}px" in css
        assert "border-radius: 4px" not in css
        assert "border-radius: 12px" not in css
        assert t.TRAY_MENU_HOVER in css
        assert t.TRAY_MENU_SEPARATOR in css
        assert t.TRAY_MENU_BORDER in css
        assert f"font-size: {t.TRAY_MENU_FONT}px" in css
        assert t.TRAY_MENU_FONT == t.TYPE_FOOTNOTE
        assert f"font-size: {t.TYPE_BODY_SM}px" not in css
        assert "QMenu::item" in css
        assert f"font-size: {t.TYPE_CAPTION}px" in css
        assert "margin-left: 6px" in css
        assert t.TEXT_DIM in css
        assert "QMenu::item:disabled:selected" in css
        assert "margin: 6px 12px" in css
        assert f"padding: {t.TRAY_MENU_PAD_V}px" in css
        assert tray.contextMenu().font().pixelSize() == t.TRAY_MENU_FONT
        # No Stitch tray look leftovers
        assert "rgba(0, 80, 203" not in css

    def test_menu_groups_match_spec_order(self, tray):
        actions = tray.contextMenu().actions()
        labels = []
        for a in actions:
            if a.isSeparator():
                labels.append("---")
            else:
                labels.append(a.text())

        assert labels[0]  # status (dynamic)
        assert not actions[0].isEnabled()
        assert labels[1] == "---"
        assert labels[2] == "打开 SayInk"
        assert labels[3] == "检查更新"
        assert labels[4] == "历史"
        # Meeting group: start/end toggle + the 会议 page, between 历史 and 切换模型.
        assert labels[5] == "---"
        assert labels[6] == "开始会议"
        assert labels[7] == "会议记录"
        assert labels[8] == "---"
        assert labels[9] == "切换模型"
        assert labels[10] == "开机时自动启动"  # same wording as the settings row
        assert actions[10].isCheckable()
        assert labels[11] == "---"
        assert labels[12] == "退出"


def test_model_switch_keeps_the_active_name_beside_the_label(tray):
    tray.update_models(
        [
            {"id": "firered", "name": "FireRedASR2", "languages": "中/英/方言", "choice_note": "均衡 CTC"},
            {"id": "qwen", "name": "Qwen3-ASR 0.6B", "languages": "中/英/多语种"},
            {"id": "fun", "name": "Fun-ASR-Nano", "languages": "中/英/日/方言"},
        ],
        "fun",
    )

    switch = next(a for a in tray.contextMenu().actions() if a.menu() is not None)
    assert switch.text() == "切换模型 · Fun-ASR-Nano"
    assert "  " not in switch.text()

    choices = switch.menu().actions()
    assert [a.text() for a in choices] == [
        "FireRedASR2 · 均衡 CTC · 中/英/方言",
        "Qwen3-ASR 0.6B · 中/英/多语种",
        "Fun-ASR-Nano · 中/英/日/方言",
    ]
    assert [a.isChecked() for a in choices] == [False, False, True]
    assert all("  " not in a.text() for a in choices)


def test_ready_status_republishes_a_small_tray_glyph(tray, qapp):
    tray.show()
    try:
        qapp.processEvents()
        before = tray.icon().cacheKey()
        tray.set_status_summary("就绪 · Fun-ASR-Nano")
        icon = tray.icon()
        widths = {size.width() for size in icon.availableSizes()}
        assert tray.toolTip() == "SayInk - 就绪 · Fun-ASR-Nano"
        assert not icon.isNull()
        assert icon.cacheKey() != before
        assert {16, 32}.issubset(widths)
        from PyQt6.QtCore import QSize

        pixmap = icon.pixmap(QSize(16, 16), 1.0)
        assert not pixmap.isNull()
        assert pixmap.devicePixelRatio() == 1.0
        assert pixmap.width() == 16
        assert pixmap.toImage().pixelColor(8, 8).alpha() > 200
    finally:
        tray.hide()


def test_tray_glyph_matches_shell_size_and_keeps_mic_contrast(tray):
    from PyQt6.QtCore import QSize

    from sayink.ui import design_tokens as tok
    from sayink.ui.tray_icon import _windows_small_icon_px

    tok.activate("dark")
    try:
        icon = tray.icon()
        metric = _windows_small_icon_px() or 16
        pixmap = icon.pixmap(icon.actualSize(QSize(metric, metric)))
        assert pixmap.devicePixelRatio() == 1.0
        assert pixmap.width() == metric
        assert pixmap.height() == metric
        image = pixmap.toImage()
        plate = image.pixelColor(int(metric * 0.20), int(metric * 0.80))
        mic = image.pixelColor(metric // 2, int(metric * 0.28))
        assert plate.alpha() > 200
        assert mic.alpha() > 200
        assert abs(plate.lightness() - mic.lightness()) > 80
    finally:
        tok.activate("light")


def test_microphone_icon_includes_windows_small_sizes():
    from sayink.ui.tray_icon import create_microphone_icon

    icon = create_microphone_icon()
    widths = {size.width() for size in icon.availableSizes()}
    assert {16, 32}.issubset(widths)


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 helper window")
def test_guarded_tray_delivers_native_double_click_once(qapp):
    import ctypes
    import os
    from ctypes import wintypes

    if qapp.platformName() != "windows":
        pytest.skip("requires the native Windows Qt platform")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    owners = []

    @callback_type
    def collect(hwnd, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        name = ctypes.create_unicode_buffer(96)
        user32.GetClassNameW(hwnd, name, len(name))
        if pid.value == os.getpid() and name.value.endswith("TrayIconMessageWindowClass"):
            owners.append(int(hwnd))
        return True

    user32.EnumWindows(collect, 0)
    previous_owners = set(owners)
    owners.clear()
    tray = TrayIcon()
    try:
        tray.show()
        user32.EnumWindows(collect, 0)
        owners[:] = [hwnd for hwnd in owners if hwnd not in previous_owners]
        assert len(owners) == 1
        opened = []
        tray.wake_island.connect(lambda: opened.append(True))
        # Qt's native tray callback: NIN_SELECT, WM_LBUTTONDBLCLK, NIN_SELECT.
        # This crosses the subclassed Win32 procedure, not just the Python slot.
        for message in (0x0400, 0x0203, 0x0400):
            user32.SendMessageW(owners[0], 0x8065, 0, message)
        qapp.processEvents()
        assert opened == [True]
        assert not tray._single_click_timer.isActive()
        assert not user32.IsWindowVisible(owners[0])
    finally:
        tray.hide()
        tray.deleteLater()


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 helper window")
def test_tray_owner_is_created_without_a_caption(qapp):
    """The shell can show the tray owner before our subclass exists.

    Windows assigns the class procedure after HCBT_CREATEWND, which wipes a
    subclass installed there. The caption has to be gone from CREATESTRUCT
    itself, or the first double-click after startup paints it and only hides
    it once the cold-start window build returns.
    """
    import ctypes
    from ctypes import wintypes

    from sayink.ui.tray_icon import _remove_helper_creation_guard

    if qapp.platformName() != "windows":
        pytest.skip("requires the native Windows Qt platform")
    # Earlier tests leave the creation guard installed. A hook added after it
    # runs first and reads CREATESTRUCT before the rewrite. Drop that guard so
    # TrayIcon() installs a fresh one in front of this test's hook.
    _remove_helper_creation_guard()

    class CREATESTRUCTW(ctypes.Structure):
        _fields_ = [
            ("lpCreateParams", ctypes.c_void_p),
            ("hInstance", ctypes.c_void_p),
            ("hMenu", ctypes.c_void_p),
            ("hwndParent", wintypes.HWND),
            ("cy", ctypes.c_int),
            ("cx", ctypes.c_int),
            ("y", ctypes.c_int),
            ("x", ctypes.c_int),
            ("style", ctypes.c_long),
            ("lpszName", ctypes.c_void_p),
            ("lpszClass", ctypes.c_void_p),
            ("dwExStyle", ctypes.c_uint),
        ]

    class CBT_CREATEWND(ctypes.Structure):
        _fields_ = [
            ("lpcs", ctypes.POINTER(CREATESTRUCTW)),
            ("hwndInsertAfter", wintypes.HWND),
        ]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    hook_type = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM,
    )
    user32.SetWindowsHookExW.argtypes = [
        ctypes.c_int, hook_type, wintypes.HINSTANCE, wintypes.DWORD,
    ]
    user32.SetWindowsHookExW.restype = wintypes.HANDLE
    user32.CallNextHookEx.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM,
    ]
    user32.CallNextHookEx.restype = ctypes.c_ssize_t
    user32.UnhookWindowsHookEx.argtypes = [wintypes.HANDLE]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    seen: list[tuple[int, int, int, int]] = []

    @hook_type
    def on_create(code, wparam, lparam):
        result = user32.CallNextHookEx(None, code, wparam, lparam)
        if code == 3 and lparam:
            name = ctypes.create_unicode_buffer(96)
            user32.GetClassNameW(wparam, name, len(name))
            if name.value.endswith("TrayIconMessageWindowClass"):
                created = ctypes.cast(lparam, ctypes.POINTER(CBT_CREATEWND)).contents
                cs = created.lpcs.contents
                seen.append((int(cs.style), int(cs.dwExStyle), int(cs.x), int(cs.y)))
        return result

    thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
    handle = user32.SetWindowsHookExW(5, on_create, None, thread_id)
    tray = TrayIcon()
    try:
        assert handle
        tray.show()
        qapp.processEvents()
        assert seen, "tray owner was not created"
        style, exstyle, x, y = seen[-1]
        assert style & 0x00C00000 == 0
        assert style & 0x80000000
        assert exstyle & 0x00000080
        assert exstyle & 0x08000000
        assert (x, y) == (-32000, -32000)
    finally:
        if handle:
            user32.UnhookWindowsHookEx(handle)
        tray.hide()
        tray.deleteLater()


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 helper window")
def test_replaced_tray_procedure_still_cannot_show_the_owner(qapp):
    """A double-click shows the owner through DefWindowProc once Qt restores it."""
    import ctypes
    import os
    from ctypes import wintypes

    if qapp.platformName() != "windows":
        pytest.skip("requires the native Windows Qt platform")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.GetProcAddress.argtypes = [wintypes.HMODULE, wintypes.LPCSTR]
    kernel32.GetProcAddress.restype = ctypes.c_void_p
    owners = []

    @callback_type
    def collect(hwnd, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        name = ctypes.create_unicode_buffer(96)
        user32.GetClassNameW(hwnd, name, len(name))
        if pid.value == os.getpid() and name.value.endswith("TrayIconMessageWindowClass"):
            owners.append(int(hwnd))
        return True

    previous = set()
    user32.EnumWindows(collect, 0)
    previous.update(owners)
    owners.clear()
    tray = TrayIcon()
    try:
        tray.show()
        qapp.processEvents()
        user32.EnumWindows(collect, 0)
        owners[:] = [hwnd for hwnd in owners if hwnd not in previous]
        assert len(owners) == 1
        defproc = kernel32.GetProcAddress(kernel32.GetModuleHandleW("user32"), b"DefWindowProcW")
        assert defproc
        user32.SetWindowLongPtrW(owners[0], -4, int(defproc))
        user32.ShowWindow(owners[0], 5)
        qapp.processEvents()
        # IsWindowVisible can stay set. The window is still the off-screen
        # captionless popup created above, so the shell cannot paint the
        # microphone / maximize / close box on the desktop.
        style = int(user32.GetWindowLongPtrW(owners[0], -16) or 0)
        assert style & 0x00C00000 == 0

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
        rect = RECT()
        assert user32.GetWindowRect(owners[0], ctypes.byref(rect))
        assert rect.left <= -16000
        assert rect.top <= -16000
    finally:
        tray.hide()
        tray.deleteLater()


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 helper window")
def test_startup_guards_titlebars_created_after_the_tray(qapp, tray):
    """Qt can create and show its titlebar before our main-window scan runs."""
    import ctypes
    import os
    from ctypes import wintypes

    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QWindow

    from sayink.ui.tray_icon import _PINNED_HWNDS

    if qapp.platformName() != "windows":
        pytest.skip("requires the native Windows Qt platform")
    expanded = getattr(Qt.WindowType, "ExpandedClientAreaHint", None)
    if expanded is None:
        pytest.skip("this Qt version does not create the titlebar helper")

    # Use an independent DLL binding: production code also binds Win32 APIs.
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumChildWindows.argtypes = [wintypes.HWND, callback_type, wintypes.LPARAM]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t

    tray.show()
    window = QWindow()
    window.setFlags(Qt.WindowType.Window | expanded)
    helpers = []

    @callback_type
    def collect(hwnd, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        name = ctypes.create_unicode_buffer(96)
        user32.GetClassNameW(hwnd, name, len(name))
        if pid.value == os.getpid() and name.value == "_q_titlebar":
            helpers.append(int(hwnd))
        return True

    try:
        window.create()
        user32.EnumChildWindows(int(window.winId()), collect, 0)
        assert helpers, "Qt must have created a real titlebar for this regression"
        # Check WS_VISIBLE, even while its owner is hidden; IsWindowVisible
        # would conceal the pending popup until the owner is first shown.
        assert all(user32.GetWindowLongPtrW(h, -16) & 0x10000000 == 0 for h in helpers)
        window.show()
        qapp.processEvents()
        assert window.isVisible()
        assert all(user32.GetWindowLongPtrW(h, -16) & 0x10000000 == 0 for h in helpers)
    finally:
        window.destroy()
    assert all(h not in _PINNED_HWNDS for h in helpers)


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 helper window")
def test_pinned_caption_window_stays_hidden_when_shown():
    """A double-click shows Qt's captioned tray owner unless the show is refused."""
    import ctypes
    from ctypes import wintypes

    from sayink.ui.tray_icon import keep_hwnd_hidden

    user32 = ctypes.windll.user32
    user32.CreateWindowExW.argtypes = [
        ctypes.c_uint, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_uint,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t

    default = ctypes.c_int(0x80000000 - 2**32).value  # CW_USEDEFAULT
    hwnd = user32.CreateWindowExW(
        0, "Static", "SayInk", 0,
        default, default, default, default,
        None, None, None, None,
    )
    assert hwnd
    try:
        assert user32.GetWindowLongPtrW(hwnd, -16) & 0x00C00000
        keep_hwnd_hidden(int(hwnd))
        assert user32.IsWindowVisible(hwnd) == 0
        assert user32.GetWindowLongPtrW(hwnd, -16) & 0x00C00000 == 0
        user32.ShowWindow(hwnd, 5)  # SW_SHOW
        assert user32.IsWindowVisible(hwnd) == 0
    finally:
        user32.DestroyWindow(hwnd)


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 helper window")
def test_startup_observers_cannot_appear_as_a_caption(qapp):
    """QApplication creates captioned observers before the tray exists.

    A double-click while startup is still busy shows that caption and leaves
    it up until the UI thread is free. Burying them with the tray must stick.
    """
    import ctypes
    import os
    from ctypes import wintypes

    if qapp.platformName() != "windows":
        pytest.skip("requires the native Windows Qt platform")
    from sayink.ui.tray_icon import TrayIcon, _hide_tray_message_window

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
    observers = []

    @callback_type
    def collect(hwnd, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        name = ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(hwnd, name, len(name))
        if pid.value == os.getpid() and "ObserverWindow" in name.value:
            observers.append(int(hwnd))
        return True

    tray = TrayIcon()
    try:
        tray.show()
        _hide_tray_message_window()
        user32.EnumWindows(collect, 0)
        assert observers, "Qt did not create its screen/theme observer windows"
        for hwnd in observers:
            assert user32.GetWindowLongPtrW(hwnd, -16) & 0x00C00000 == 0
            rect = RECT()
            assert user32.GetWindowRect(hwnd, ctypes.byref(rect))
            assert rect.left <= -16000 and rect.top <= -16000
            user32.ShowWindow(hwnd, 5)
            assert user32.IsWindowVisible(hwnd) == 0
    finally:
        tray.hide()
        tray.deleteLater()


def test_idle_mic_uses_text_token():
    from PyQt6.QtGui import QColor

    from sayink.ui import design_tokens as tok
    from sayink.ui.tray_icon import _microphone_pixmap

    tok.activate("light")
    # Sample the plate near the top-left inner corner (outside the center mic glyph).
    img = _microphone_pixmap(None, False, 32).toImage()
    expected = QColor(tok.TEXT)
    pixel = img.pixelColor(6, 6)
    assert abs(pixel.red() - expected.red()) <= 2
    assert abs(pixel.green() - expected.green()) <= 2
    assert abs(pixel.blue() - expected.blue()) <= 2


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 SendMessage")
def test_native_double_click_defers_wake_out_of_win32_send_message(qapp):
    """Wake must run after WndProc returns so OLE/COM never hits 0x8001010D."""
    import ctypes
    import os
    from ctypes import wintypes

    from sayink.ui.tray_icon import in_win32_input_sync_call

    if qapp.platformName() != "windows":
        pytest.skip("requires the native Windows Qt platform")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = ctypes.c_ssize_t
    owners: list[int] = []

    @callback_type
    def collect(hwnd, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        name = ctypes.create_unicode_buffer(96)
        user32.GetClassNameW(hwnd, name, len(name))
        if pid.value == os.getpid() and name.value.endswith("TrayIconMessageWindowClass"):
            owners.append(int(hwnd))
        return True

    user32.EnumWindows(collect, 0)
    before = set(owners)
    owners.clear()
    tray = TrayIcon()
    try:
        tray.show()
        user32.EnumWindows(collect, 0)
        owners[:] = [h for h in owners if h not in before]
        assert len(owners) == 1
        in_sync_during_slot: list[bool] = []
        tray.wake_island.connect(lambda: in_sync_during_slot.append(in_win32_input_sync_call()))

        for message in (0x0400, 0x0203, 0x0400):
            user32.SendMessageW(owners[0], 0x8065, 0, message)
        # Must NOT have fired synchronously inside SendMessageW / hook.
        assert in_sync_during_slot == []
        qapp.processEvents()
        assert in_sync_during_slot == [False]
    finally:
        tray.hide()
        tray.deleteLater()

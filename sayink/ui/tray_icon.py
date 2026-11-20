import sys

from PyQt6.QtWidgets import QSystemTrayIcon, QMenu
from PyQt6.QtGui import (
    QIcon, QIconEngine, QPixmap, QPainter, QColor, QBrush, QPen,
    QRadialGradient, QPainterPath, QAction, QActionGroup
)
from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot, QSize, QRect, QRectF, QPoint, QPointF

from sayink.runtime_status import MEETING_STATE_LABELS

# Windows turns a CW_USEDEFAULT overlapped window into a real caption
# (WS_CAPTION). The tray owner is that window. A double-click asks the shell
# to show it after our hide returns, which is the small icon / minimize /
# close / white client. The proc below refuses that show.
_WM_WINDOWPOSCHANGING = 0x0046
_SWP_NOMOVE = 0x0002
_SWP_NOACTIVATE = 0x0010
_SWP_SHOWWINDOW = 0x0040
_SWP_HIDEWINDOW = 0x0080
_GWL_STYLE = -16
_GWL_EXSTYLE = -20
_GWLP_WNDPROC = -4
_WS_CAPTION_BITS = 0x00C00000 | 0x00040000 | 0x00080000 | 0x00020000 | 0x00010000
_WS_POPUP = 0x80000000
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_NOACTIVATE = 0x08000000
_PINNED_PROCS: list = []
_PINNED_HWNDS: dict[int, int] = {}
_HELPER_HWNDS: set[int] = set()
_NON_HELPER_HWNDS: set[int] = set()
_PENDING_SUBCLASS_HWNDS: set[int] = set()
_CLOAKED_HWNDS: set[int] = set()
_HELPER_CREATION_GUARD = None
_TRANSIENT_CALL_GUARD = None
_ACCESSIBILITY_WARMED_APP_REF = None
_DWMWA_CLOAK = 13
_ISMEX_PENDING_MASK = 0x00000007  # ISMEX_SEND | ISMEX_NOTIFY | ISMEX_CALLBACK
_IN_NATIVE_TRAY_PROC = 0


def _is_helper_class(name: str) -> bool:
    if not name:
        return False
    if name == "_q_titlebar" or name.startswith("QEventDispatcherWin32_Internal_Widget"):
        return True
    return name.endswith((
        "TrayIconMessageWindowClass",
        "PowerDummyWindow",
        "ClipboardView",
        "ThemeChangeObserverWindow",
        "ScreenChangeObserverWindow",
    ))


def _cwpstruct_type():
    import ctypes
    from ctypes import wintypes

    cached = getattr(_cwpstruct_type, "cls", None)
    if cached is not None:
        return cached

    class CWPSTRUCT(ctypes.Structure):
        _fields_ = [
            ("lParam", ctypes.c_ssize_t),
            ("wParam", ctypes.c_size_t),
            ("message", ctypes.c_uint),
            ("hwnd", wintypes.HWND),
        ]

    _cwpstruct_type.cls = CWPSTRUCT
    return CWPSTRUCT


def _windowpos_type():
    import ctypes
    from ctypes import wintypes

    cached = getattr(_windowpos_type, "cls", None)
    if cached is not None:
        return cached

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

    _windowpos_type.cls = WINDOWPOS
    return WINDOWPOS


def _pointer_address(value) -> int:
    import ctypes

    bits = ctypes.sizeof(ctypes.c_void_p) * 8
    return int(value) & ((1 << bits) - 1)


def _force_hide_pos(lparam) -> None:
    """Turn this position change into a hide, before the window is painted."""
    if not lparam:
        return
    base = _pointer_address(lparam)
    windowpos = _windowpos_type()
    import ctypes

    flags = ctypes.c_uint.from_address(base + windowpos.flags.offset)
    flags.value = (flags.value & ~(_SWP_SHOWWINDOW | _SWP_NOMOVE)) | (
        _SWP_HIDEWINDOW | _SWP_NOACTIVATE
    )
    ctypes.c_int.from_address(base + windowpos.x.offset).value = -32000
    ctypes.c_int.from_address(base + windowpos.y.offset).value = -32000


def _class_name(user32, hwnd: int) -> str:
    import ctypes

    name = ctypes.create_unicode_buffer(128)
    user32.GetClassNameW(hwnd, name, len(name))
    return name.value


def _cloak_hwnd(hwnd: int) -> None:
    """Keep DWM from presenting the helper, including as a tray preview."""
    if not hwnd or hwnd in _CLOAKED_HWNDS:
        return
    try:
        import ctypes
        from ctypes import wintypes

        dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
        dwmapi.DwmSetWindowAttribute.argtypes = [
            wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
        ]
        dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
        cloaked = ctypes.c_int(1)
        hr = dwmapi.DwmSetWindowAttribute(
            hwnd, _DWMWA_CLOAK, ctypes.byref(cloaked), ctypes.sizeof(cloaked),
        )
        if hr == 0:
            _CLOAKED_HWNDS.add(int(hwnd))
    except Exception:
        return


def _bury_helper_create_struct(lparam) -> None:
    """Make the helper a captionless popup before Windows builds its frame.

    HCBT_CREATEWND runs before the class window procedure is assigned, so a
    subclass installed here is wiped. The style written into CREATESTRUCT is
    not: the window is never the overlapped caption that later paints the
    microphone, maximize box, close mark, and white client.
    """
    if not lparam:
        return
    import ctypes
    from ctypes import wintypes

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

    cbt = ctypes.cast(lparam, ctypes.POINTER(CBT_CREATEWND)).contents
    if not cbt.lpcs:
        return
    base = _pointer_address(ctypes.cast(cbt.lpcs, ctypes.c_void_p).value or 0)
    if not base:
        return
    style = ctypes.c_long.from_address(base + CREATESTRUCTW.style.offset)
    style.value = (style.value & ~_WS_CAPTION_BITS & ~0x10000000) | _WS_POPUP
    exstyle = ctypes.c_uint.from_address(base + CREATESTRUCTW.dwExStyle.offset)
    exstyle.value = exstyle.value | _WS_EX_TOOLWINDOW | _WS_EX_NOACTIVATE
    ctypes.c_int.from_address(base + CREATESTRUCTW.x.offset).value = -32000
    ctypes.c_int.from_address(base + CREATESTRUCTW.y.offset).value = -32000


def _win32_user32():
    cached = getattr(_win32_user32, "_dll", None)
    if cached is not None:
        return cached
    import ctypes
    from ctypes import wintypes

    # Keep these signatures private. Other UI modules bind the same APIs
    # with their own ctypes structures while a native callback is in flight.
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    wndenumproc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    if hasattr(user32.EnumWindows, "argtypes"):
        user32.EnumWindows.argtypes = [wndenumproc, wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND, ctypes.POINTER(wintypes.DWORD),
    ]
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, ctypes.c_uint,
    ]
    user32.CallWindowProcW.argtypes = [
        ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    ]
    user32.CallWindowProcW.restype = ctypes.c_ssize_t
    if hasattr(user32, "InSendMessageEx") and hasattr(user32.InSendMessageEx, "argtypes"):
        user32.InSendMessageEx.argtypes = [wintypes.LPVOID]
        user32.InSendMessageEx.restype = wintypes.DWORD
    _win32_user32._dll = user32
    return user32


def in_win32_input_sync_call() -> bool:
    """True while dispatching a native tray message or cross-thread Win32 SendMessage.

    Constructing or showing MainWindow inside an input-synchronous message from
    Explorer triggers COM RPC_E_CANTCALLOUT_ININPUTSYNCCALL (0x8001010D).
    """
    if sys.platform != "win32":
        return False
    if _IN_NATIVE_TRAY_PROC > 0:
        return True
    try:
        user32 = _win32_user32()
        in_send = getattr(user32, "InSendMessageEx", None)
        if callable(in_send):
            return bool(int(in_send(None)) & _ISMEX_PENDING_MASK)
    except Exception:
        return False
    return False


def _disarm_transient_call_guard() -> None:
    global _TRANSIENT_CALL_GUARD
    guard = _TRANSIENT_CALL_GUARD
    _TRANSIENT_CALL_GUARD = None
    if guard is None:
        return
    user32, handle, _callback = guard
    try:
        user32.UnhookWindowsHookEx(handle)
    except Exception:
        pass


def _install_helper_creation_guard() -> None:
    """Pin new Qt helpers before their first show, on the UI thread only.

    Install this before ``QApplication``: Qt creates captioned observer
    windows inside that constructor, and a tray click during startup
    shows one of them until the UI thread is free to hide it.

    Performance note: ``WH_CBT`` only runs on window lifecycle events
    (never on mouse move / hit-test / paint / timers). ``WH_CALLWNDPROC`` is
    armed ONLY transiently during ``CreateWindowExW`` of a helper window so its
    per-HWND ``GWLP_WNDPROC`` subclass is installed right after Windows assigns
    the class procedure, then unhooked immediately—zero steady-state GIL overhead.
    """
    global _HELPER_CREATION_GUARD
    if sys.platform != "win32" or _HELPER_CREATION_GUARD is not None:
        return
    import ctypes
    import logging
    from ctypes import wintypes

    try:
        user32 = _win32_user32()
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
        user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        thread_id = ctypes.windll.kernel32.GetCurrentThreadId()

        @hook_type
        def on_call(code, wparam, lparam):
            try:
                if code == 0 and lparam:  # HC_ACTION
                    info = ctypes.cast(lparam, ctypes.POINTER(_cwpstruct_type())).contents
                    hwnd = int(info.hwnd)
                    if hwnd in _PENDING_SUBCLASS_HWNDS:
                        _PENDING_SUBCLASS_HWNDS.discard(hwnd)
                        _install_hide_hook(user32, hwnd)
                        _cloak_hwnd(hwnd)
                        if not _PENDING_SUBCLASS_HWNDS:
                            _disarm_transient_call_guard()
                    if info.message == _WM_WINDOWPOSCHANGING and info.lParam and hwnd in _HELPER_HWNDS:
                        _cloak_hwnd(hwnd)
                        windowpos = _windowpos_type()
                        base = _pointer_address(info.lParam)
                        flags = ctypes.c_uint.from_address(base + windowpos.flags.offset).value
                        visible = bool(user32.IsWindowVisible(hwnd))
                        if flags & _SWP_SHOWWINDOW or (visible and not flags & _SWP_HIDEWINDOW):
                            _force_hide_pos(info.lParam)
                    elif not _PENDING_SUBCLASS_HWNDS:
                        _disarm_transient_call_guard()
            except Exception:
                logging.getLogger("SayInk").debug("拦截辅助窗口显示失败", exc_info=True)
            return user32.CallNextHookEx(None, code, wparam, lparam)

        def _arm_transient_call_guard() -> None:
            global _TRANSIENT_CALL_GUARD
            if _TRANSIENT_CALL_GUARD is not None:
                return
            handle = user32.SetWindowsHookExW(4, on_call, None, thread_id)  # WH_CALLWNDPROC
            if handle:
                _TRANSIENT_CALL_GUARD = (user32, handle, on_call)

        @hook_type
        def on_create(code, wparam, lparam):
            try:
                if code == 3:  # HCBT_CREATEWND; wparam is the new HWND.
                    hwnd = int(wparam)
                    name = _class_name(user32, hwnd)
                    if _is_helper_class(name):
                        _NON_HELPER_HWNDS.discard(hwnd)
                        _HELPER_HWNDS.add(hwnd)
                        if name == "_q_titlebar" or name.endswith("TrayIconMessageWindowClass"):
                            _bury_helper_create_struct(lparam)
                        _cloak_hwnd(hwnd)
                        _PENDING_SUBCLASS_HWNDS.add(hwnd)
                        _arm_transient_call_guard()
                    else:
                        _NON_HELPER_HWNDS.add(hwnd)
                elif code == 4:  # HCBT_DESTROYWND
                    hwnd = int(wparam)
                    _NON_HELPER_HWNDS.discard(hwnd)
                    _HELPER_HWNDS.discard(hwnd)
                    _CLOAKED_HWNDS.discard(hwnd)
                    if hwnd in _PENDING_SUBCLASS_HWNDS:
                        _PENDING_SUBCLASS_HWNDS.discard(hwnd)
                        if not _PENDING_SUBCLASS_HWNDS:
                            _disarm_transient_call_guard()
                elif code == 5:  # HCBT_ACTIVATE
                    hwnd = int(wparam)
                    if hwnd in _HELPER_HWNDS:
                        return 1
                    if hwnd not in _NON_HELPER_HWNDS:
                        if _is_helper_class(_class_name(user32, hwnd)):
                            _HELPER_HWNDS.add(hwnd)
                            return 1
                        _NON_HELPER_HWNDS.add(hwnd)
            except Exception:
                logging.getLogger("SayInk").debug("保护新建辅助窗口失败", exc_info=True)
            return user32.CallNextHookEx(None, code, wparam, lparam)

        cbt = user32.SetWindowsHookExW(5, on_create, None, thread_id)  # WH_CBT
        if not cbt:
            raise ctypes.WinError(ctypes.get_last_error())
        # ctypes callbacks must outlive their native registrations.
        _HELPER_CREATION_GUARD = (user32, (cbt,), (on_create, on_call))
        _bind_helper_guard()
    except Exception:
        logging.getLogger("SayInk").warning("安装辅助窗口保护失败", exc_info=True)


def _bind_helper_guard() -> None:
    """Drop the hooks when the application exits. Safe before it exists."""
    if sys.platform != "win32":
        return
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance()
    platform_fn = getattr(app, "platformName", None) if app is not None else None
    if app is None or not callable(platform_fn) or platform_fn() != "windows":
        return
    warm_win32_accessibility()
    if _HELPER_CREATION_GUARD is None or getattr(_bind_helper_guard, "bound", False):
        return
    app.aboutToQuit.connect(_remove_helper_creation_guard)
    app.destroyed.connect(_remove_helper_creation_guard)
    _bind_helper_guard.bound = True


def warm_win32_accessibility() -> None:
    """Pre-warm Qt/STA accessibility marshaling outside Win32 SendMessage.

    Before ``MainWindow`` is constructed (e.g. during ``--autostart`` silent boot
    while the ASR model loads), ``FloatingWindow`` can be the first visible Qt
    window. When an external process (IME, Explorer, UI Automation, screen
    reader, or launcher tool) sends cross-process ``WM_GETOBJECT`` via
    ``SendMessage``, ``oleacc`` / ``combase`` lazily registers the UI STA
    channel inside that input-synchronous call and raises caught COM exception
    ``RPC_E_CANTCALLOUT_ININPUTSYNCCALL`` (``0x8001010D``), which Python's
    first-chance ``faulthandler`` VEH logs to ``crash.log`` as a false-positive
    fatal exception. Querying ``OBJID_CLIENT`` once on the UI thread upfront
    initializes the STA marshaler cleanly before any external ``SendMessage``.
    """
    global _ACCESSIBILITY_WARMED_APP_REF
    if sys.platform != "win32":
        return
    import weakref

    from PyQt6.QtCore import Qt, QTimer
    from PyQt6.QtWidgets import QApplication, QWidget

    app = QApplication.instance()
    platform_fn = getattr(app, "platformName", None) if app is not None else None
    if app is None or not callable(platform_fn) or platform_fn() != "windows":
        return
    if _ACCESSIBILITY_WARMED_APP_REF is not None and _ACCESSIBILITY_WARMED_APP_REF() is app:
        return
    if in_win32_input_sync_call():
        QTimer.singleShot(0, warm_win32_accessibility)
        return
    _ACCESSIBILITY_WARMED_APP_REF = weakref.ref(app)

    probe = None
    try:
        import ctypes
        from ctypes import wintypes

        class _GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        probe = QWidget(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        probe.setAccessibleName("SayInk")
        hwnd = int(probe.winId() or 0)
        if not hwnd:
            return
        oleacc = ctypes.WinDLL("oleacc", use_last_error=True)
        oleacc.AccessibleObjectFromWindow.argtypes = [
            wintypes.HWND,
            wintypes.DWORD,
            ctypes.POINTER(_GUID),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        oleacc.AccessibleObjectFromWindow.restype = ctypes.c_long
        # IID_IAccessible = {618736e0-3c3d-11cf-810c-00aa00389b71}, OBJID_CLIENT = 0xFFFFFFFC
        iid_iaccessible = _GUID(
            0x618736E0,
            0x3C3D,
            0x11CF,
            (ctypes.c_ubyte * 8)(0x81, 0x0C, 0x00, 0xAA, 0x00, 0x38, 0x9B, 0x71),
        )
        acc_ptr = ctypes.c_void_p()
        hr = oleacc.AccessibleObjectFromWindow(
            wintypes.HWND(hwnd),
            wintypes.DWORD(0xFFFFFFFC),
            ctypes.byref(iid_iaccessible),
            ctypes.byref(acc_ptr),
        )
        if hr == 0 and acc_ptr.value:
            vtbl = ctypes.cast(
                ctypes.c_void_p.from_address(acc_ptr.value).value,
                ctypes.POINTER(ctypes.c_void_p),
            )
            release_fn = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(vtbl[2])
            release_fn(acc_ptr)
    except Exception:
        import logging

        logging.getLogger("SayInk").debug("预热 Windows 无障碍通道失败", exc_info=True)
    finally:
        if probe is not None:
            try:
                probe.destroy()
                probe.deleteLater()
            except Exception:
                pass


def _remove_helper_creation_guard() -> None:
    global _HELPER_CREATION_GUARD
    _disarm_transient_call_guard()
    _PENDING_SUBCLASS_HWNDS.clear()
    guard = _HELPER_CREATION_GUARD
    _HELPER_CREATION_GUARD = None
    if guard is None:
        return
    user32, handles, _callbacks = guard
    for handle in handles:
        user32.UnhookWindowsHookEx(handle)


def keep_hwnd_hidden(hwnd: int) -> None:
    """Stop a helper window from appearing as a captioned box.

    Hiding once is not enough: the shell shows the tray owner again when the
    icon is double-clicked, after the click handler has already returned.
    """
    if sys.platform != "win32" or not hwnd:
        return
    try:
        user32 = _win32_user32()
        if not user32.IsWindow(hwnd):
            return
        _install_hide_hook(user32, int(hwnd))
        _HELPER_HWNDS.add(int(hwnd))
        _cloak_hwnd(int(hwnd))
        style = user32.GetWindowLongPtrW(hwnd, _GWL_STYLE)
        buried = (style & ~_WS_CAPTION_BITS) | _WS_POPUP
        if buried != style:
            user32.SetWindowLongPtrW(hwnd, _GWL_STYLE, buried)
        exstyle = user32.GetWindowLongPtrW(hwnd, _GWL_EXSTYLE)
        tool = exstyle | _WS_EX_TOOLWINDOW | _WS_EX_NOACTIVATE
        if tool != exstyle:
            user32.SetWindowLongPtrW(hwnd, _GWL_EXSTYLE, tool)
        user32.ShowWindow(hwnd, 0)
        user32.SetWindowPos(
            hwnd, 0, -32000, -32000, 0, 0,
            0x0001 | 0x0004 | _SWP_NOACTIVATE | _SWP_HIDEWINDOW,
        )
    except Exception:
        return


def _install_hide_hook(user32, hwnd: int) -> None:
    import ctypes
    from ctypes import wintypes

    current = int(user32.GetWindowLongPtrW(hwnd, _GWLP_WNDPROC) or 0)
    if not current or _PINNED_HWNDS.get(hwnd) == current:
        return

    original = ctypes.c_void_p(current)
    wndproc = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    )

    @wndproc
    def hook(h, msg, wparam, lparam, _orig=original, _user=user32):
        try:
            if msg == 0x0082:  # WM_NCDESTROY
                _PINNED_HWNDS.pop(int(h), None)
                _HELPER_HWNDS.discard(int(h))
                _CLOAKED_HWNDS.discard(int(h))
            elif msg == _WM_WINDOWPOSCHANGING and lparam:
                import ctypes

                windowpos = _windowpos_type()
                base = _pointer_address(lparam)
                flags = ctypes.c_uint.from_address(base + windowpos.flags.offset).value
                if flags & _SWP_SHOWWINDOW or (
                    _user.IsWindowVisible(h) and not flags & _SWP_HIDEWINDOW
                ):
                    _force_hide_pos(lparam)
        except Exception:
            pass
        global _IN_NATIVE_TRAY_PROC
        _IN_NATIVE_TRAY_PROC += 1
        try:
            return _user.CallWindowProcW(_orig, h, msg, wparam, lparam)
        except Exception:
            return 0
        finally:
            _IN_NATIVE_TRAY_PROC = max(0, _IN_NATIVE_TRAY_PROC - 1)

    _PINNED_PROCS.append(hook)
    user32.SetWindowLongPtrW(
        hwnd, _GWLP_WNDPROC, ctypes.cast(hook, ctypes.c_void_p).value,
    )
    _PINNED_HWNDS[hwnd] = int(user32.GetWindowLongPtrW(hwnd, _GWLP_WNDPROC) or 0)


def _hide_tray_message_window() -> None:
    """Keep Qt's tray owner off the desktop.

    Qt creates it as an overlapped window so it can hear TaskbarCreated.
    Windows then gives that default-sized window a system caption. Showing it
    paints the app icon, minimize, maximize, close, and a white client.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        import os
        from ctypes import wintypes

        user32 = _win32_user32()
        pid = os.getpid()
        found: list[int] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def collect(hwnd, _lparam):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value != pid:
                return True
            name = ctypes.create_unicode_buffer(96)
            user32.GetClassNameW(hwnd, name, 96)
            if _is_helper_class(name.value):
                found.append(int(hwnd))
            return True

        user32.EnumWindows(collect, 0)
        for hwnd in found:
            keep_hwnd_hidden(hwnd)
    except Exception:
        return


def _menu_font():
    from PyQt6.QtGui import QFont

    from sayink.ui import design_tokens as tok

    font = QFont(tok.UI_FONT_FAMILY)
    font.setPixelSize(tok.TRAY_MENU_FONT)
    return font


def _menu_stylesheet() -> str:
    from sayink.ui import design_tokens as tok

    # Windows styled menus do not inherit QMenu's font onto items, and they
    # do not reserve a check column. Pad every row the same so labels line up
    # past the check, with a matching gutter for the submenu chevron.
    pad_right = tok.TRAY_MENU_PAD_H + 4
    return f"""
    QMenu {{
        background-color: {tok.SURFACE};
        color: {tok.TEXT};
        border: 1px solid {tok.TRAY_MENU_BORDER};
        border-radius: {tok.TRAY_MENU_RADIUS}px;
        padding: 4px 0px;
        font-family: {tok.FONT};
        font-size: {tok.TRAY_MENU_FONT}px;
    }}
    QMenu::item {{
        font-family: {tok.FONT};
        font-size: {tok.TRAY_MENU_FONT}px;
        padding: {tok.TRAY_MENU_PAD_V}px {pad_right}px {tok.TRAY_MENU_PAD_V}px {tok.TRAY_MENU_PAD_H}px;
        margin: 0px;
        border-radius: 0px;
        background: transparent;
    }}
    QMenu::item:selected {{
        background-color: {tok.TRAY_MENU_HOVER};
        color: {tok.TEXT};
    }}
    QMenu::item:disabled {{
        color: {tok.TEXT_DIM};
        background: transparent;
        font-size: {tok.TYPE_CAPTION}px;
    }}
    QMenu::item:disabled:selected {{
        background: transparent;
    }}
    QMenu::separator {{
        height: 1px;
        background: {tok.TRAY_MENU_SEPARATOR};
        margin: 6px 12px;
    }}
    QMenu::indicator {{
        width: 12px;
        height: 12px;
        margin-left: 6px;
    }}
    QMenu::right-arrow {{
        width: 8px;
        height: 8px;
        margin-right: 12px;
    }}
    """


_MENU_JOIN = " · "


def _paired_label(lead: str, detail: str) -> str:
    """Keep a caption and its value adjacent. Extra menu width stays to the right."""
    lead = (lead or "").strip()
    detail = (detail or "").strip()
    if lead and detail:
        return f"{lead}{_MENU_JOIN}{detail}"
    return lead or detail


def _model_switch_title(active_name: str) -> str:
    return _paired_label("切换模型", active_name) or "切换模型"


def _model_choice_label(model: dict) -> str:
    """Name, then a short role when one exists, then languages.

    Two FireRed builds share a family name; the role is what tells them apart
    in a menu that has no room for the engine-page description.
    """
    name = str(model.get("name") or "")
    note = str(model.get("choice_note") or "").strip()
    languages = str(model.get("languages") or "").strip()
    detail = _MENU_JOIN.join(part for part in (note, languages) if part)
    return _paired_label(name, detail)


def _paint_menu(menu) -> None:
    if menu is None:
        return
    menu.setFont(_menu_font())
    menu.setStyleSheet(_menu_stylesheet())


def _windows_small_icon_px() -> int | None:
    """Notification-area glyph size. A mismatch here is what leaves the slot blank."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        px = int(ctypes.windll.user32.GetSystemMetrics(49))  # SM_CXSMICON
    except (AttributeError, OSError, ValueError):
        return None
    if 16 <= px <= 64:
        return px
    return None


_PIXMAP_CACHE: dict[tuple, QPixmap] = {}


def _microphone_pixmap(color: str | None, recording: bool, size: int) -> QPixmap:
    from sayink.ui import design_tokens as tok

    cache_key = (
        getattr(tok, "ACTIVE_THEME", "light"),
        color or tok.TEXT,
        tok.STATE_RECORD,
        bool(recording),
        int(size),
    )
    cached = _PIXMAP_CACHE.get(cache_key)
    if cached is not None and not cached.isNull():
        return cached

    pixmap = QPixmap(QSize(size, size))
    # Windows drops the tray glyph when the bitmap's device pixel ratio is not 1.
    pixmap.setDevicePixelRatio(1.0)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    s = size
    cx = s / 2

    if recording:
        record = QColor(tok.STATE_RECORD)
        darker = QColor(record)
        darker = darker.darker(120)
        bg_grad = QRadialGradient(QPointF(cx, cx), s * 0.45)
        bg_grad.setColorAt(0, record)
        bg_grad.setColorAt(1, darker)
    else:
        top = QColor(color or tok.TEXT)
        bottom = QColor(color or tok.TEXT)
        bg_grad = QRadialGradient(QPointF(cx, cx), s * 0.45)
        bg_grad.setColorAt(0, top)
        bg_grad.setColorAt(1, bottom)

    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(bg_grad))
    margin = s * 0.06
    painter.drawRoundedRect(
        QRectF(margin, margin, s - 2 * margin, s - 2 * margin),
        s * 0.22, s * 0.22
    )

    # Dark theme plate is near-white (#F9FAFB). A white mic on that plate
    # has no silhouette, so the tray glyph reads as empty.
    plate = QColor(tok.STATE_RECORD if recording else (color or tok.TEXT))
    glyph = QColor(26, 26, 26, 245) if plate.lightness() > 200 else QColor(255, 255, 255, 245)
    grille = QColor(255, 255, 255, 90) if glyph.lightness() < 128 else QColor(26, 26, 26, 90)

    mic_w = s * 0.22
    mic_h = s * 0.32
    mic_x = cx - mic_w / 2
    mic_y = s * 0.16

    mic_path = QPainterPath()
    mic_path.addRoundedRect(QRectF(mic_x, mic_y, mic_w, mic_h), mic_w / 2, mic_w / 2)
    painter.setBrush(QBrush(glyph))
    painter.drawPath(mic_path)

    painter.setPen(QPen(grille, 1))
    grille_top = mic_y + mic_h * 0.3
    grille_bottom = mic_y + mic_h * 0.7
    for i in range(3):
        y = grille_top + (grille_bottom - grille_top) * i / 2
        painter.drawLine(QPointF(mic_x + mic_w * 0.25, y), QPointF(mic_x + mic_w * 0.75, y))

    arc_pen = QPen(glyph, s * 0.035)
    arc_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(arc_pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    arc_w = s * 0.38
    arc_h = s * 0.28
    arc_rect = QRectF(cx - arc_w / 2, s * 0.26, arc_w, arc_h)
    painter.drawArc(arc_rect, 0, -180 * 16)

    stem_top = s * 0.26 + arc_h / 2
    stem_bottom = s * 0.68
    painter.drawLine(QPointF(cx, stem_top), QPointF(cx, stem_bottom))

    base_w = s * 0.22
    painter.drawLine(QPointF(cx - base_w / 2, stem_bottom), QPointF(cx + base_w / 2, stem_bottom))

    if recording:
        wave_pen = QPen(QColor(255, 255, 255, 140), s * 0.02)
        wave_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(wave_pen)
        for i, offset in enumerate([s * 0.06, s * 0.12]):
            alpha = 140 - i * 50
            wave_pen.setColor(QColor(255, 255, 255, alpha))
            painter.setPen(wave_pen)
            painter.drawArc(
                QRectF(cx - mic_w / 2 - offset - s * 0.04, s * 0.22, s * 0.08, s * 0.2),
                45 * 16, 90 * 16
            )
            painter.drawArc(
                QRectF(cx + mic_w / 2 + offset - s * 0.04, s * 0.22, s * 0.08, s * 0.2),
                135 * 16, -90 * 16
            )

    painter.end()
    pixmap.setDevicePixelRatio(1.0)
    if len(_PIXMAP_CACHE) >= 64:
        _PIXMAP_CACHE.clear()
    _PIXMAP_CACHE[cache_key] = pixmap
    return pixmap


def _icon_pixel_sizes(size: int) -> list[int]:
    sizes = {16, 20, 24, 32, size}
    small = _windows_small_icon_px()
    if small is not None:
        sizes.add(small)
    return sorted(s for s in sizes if 16 <= s <= max(size, 16))


def create_microphone_icon(color: str | None = None, recording: bool = False, size: int = 64) -> QIcon:
    icon = QIcon()
    for px in _icon_pixel_sizes(size):
        icon.addPixmap(_microphone_pixmap(color, recording, px))
    return icon


class _TrayMicrophoneEngine(QIconEngine):
    """Hand Windows a shell-sized bitmap at 1x.

    QIcon.pixmap() otherwise multiplies by the screen scale. On a 200% display
    the tray asks for 32px and receives a 64px image at devicePixelRatio 2,
    and the notification area draws nothing.
    """

    def __init__(self, color: str | None = None, recording: bool = False):
        super().__init__()
        self._color = color
        self._recording = recording

    def clone(self):
        return _TrayMicrophoneEngine(self._color, self._recording)

    def availableSizes(self, mode, state):
        metric = _windows_small_icon_px() or 16
        return [QSize(side, side) for side in sorted({16, 32, metric})]

    def actualSize(self, size, mode, state):
        metric = _windows_small_icon_px()
        if metric is None:
            side = max(size.width(), size.height())
        else:
            side = metric
        return QSize(side, side)

    def pixmap(self, size, mode, state):
        side = max(16, int(min(size.width(), size.height()) or 16))
        return _microphone_pixmap(self._color, self._recording, side)

    def scaledPixmap(self, size, mode, state, scale):
        # Default engine does pixmap(size * scale), which reintroduces the 2x bitmap.
        del scale
        return self.pixmap(size, mode, state)

    def paint(self, painter, rect, mode, state):
        side = max(16, int(min(rect.width(), rect.height())))
        painter.drawPixmap(rect, self.pixmap(QSize(side, side), mode, state))


def _fresh_microphone_icon(kind: str) -> QIcon:
    """A new QIcon each call. Reusing one cache key makes Windows ignore NIM_MODIFY."""
    from sayink.ui import design_tokens as tok

    if kind == "recording":
        return QIcon(_TrayMicrophoneEngine(recording=True))
    if kind == "attention":
        return QIcon(_TrayMicrophoneEngine(color=tok.ATTENTION, recording=False))
    return QIcon(_TrayMicrophoneEngine())


def tray_menu_top_left(menu_size: QSize, anchor: QRect, available: QRect) -> QPoint:
    """Place a tray menu above the icon, or below it when the icon is at the top."""
    width = max(1, int(menu_size.width()))
    height = max(1, int(menu_size.height()))
    icon_top = int(anchor.y())
    icon_right = int(anchor.x() + max(1, anchor.width()))
    icon_bottom = int(anchor.y() + max(1, anchor.height()))

    x = icon_right - width
    y = icon_top - height
    if y < int(available.top()):
        y = icon_bottom

    left = int(available.left())
    top = int(available.top())
    right = left + int(available.width())
    bottom = top + int(available.height())
    if x + width > right:
        x = right - width
    if x < left:
        x = left
    if y + height > bottom:
        y = bottom - height
    if y < top:
        y = top
    return QPoint(x, y)


class _UpwardContextMenu(QMenu):
    """Tray menu that opens above the icon instead of dropping below the cursor."""

    def __init__(self, tray: QSystemTrayIcon):
        super().__init__()
        self._tray = tray

    def popup(self, pos, at=None):
        from PyQt6.QtWidgets import QApplication

        self.ensurePolished()
        self.adjustSize()
        size = self.sizeHint()
        anchor = self._tray.geometry()
        if anchor.isNull() or anchor.width() <= 0 or anchor.height() <= 0:
            anchor = QRect(int(pos.x()), int(pos.y()), 1, 1)
        screen = QApplication.screenAt(pos) or QApplication.primaryScreen()
        available = screen.availableGeometry() if screen is not None else QRect(0, 0, 1920, 1080)
        target = tray_menu_top_left(size, anchor, available)
        if at is None:
            super().popup(target)
        else:
            super().popup(target, at)


class TrayIcon(QSystemTrayIcon):
    open_settings = pyqtSignal()
    check_update_requested = pyqtSignal()
    wake_island = pyqtSignal()
    history_requested = pyqtSignal()
    quit_app = pyqtSignal()
    auto_start_toggled = pyqtSignal(bool)
    model_switched = pyqtSignal(str)
    menu_about_to_show = pyqtSignal()
    menu_closed = pyqtSignal()
    meeting_start_requested = pyqtSignal()
    meeting_end_requested = pyqtSignal()
    end_meeting_requested = meeting_end_requested
    stop_continuous_requested = pyqtSignal()
    meetings_requested = pyqtSignal()

    def __init__(self, parent=None):
        # Before QSystemTrayIcon builds its message window. That window is an
        # overlapped CW_USEDEFAULT caption; the shell shows it on the first
        # double-click, and it stays up while the cold-start window is built.
        _install_helper_creation_guard()
        super().__init__(parent)

        self._icon_kind = "normal"
        self._rebuild_icons()
        self._apply_icon_kind("normal")
        self._idle_tooltip = "SayInk - 就绪"
        self._status_summary = "就绪"
        self._activity_state: str | None = None
        self.setToolTip(self._idle_tooltip)
        self._flash_timer = None
        self._flash_restore_recording = False

        from PyQt6.QtCore import QTimer

        self._model_menu = None
        self._end_meeting_menu = None
        self._model_group = None
        self._menu = None
        self._continuous_listening = False
        self._menu_is_open = False
        self._wake_after_menu = False
        self._wake_queued = False
        self._suppress_wake_until = 0.0
        self._single_click_timer = QTimer(self)
        self._single_click_timer.setSingleShot(True)
        self._notice_kind = ""
        self._setup_menu()
        self.activated.connect(self._on_activated)

    def _rebuild_icons(self) -> None:
        self._normal_icon = _fresh_microphone_icon("normal")
        self._recording_icon = _fresh_microphone_icon("recording")
        self._attention_icon = _fresh_microphone_icon("attention")

    def _apply_icon_kind(self, kind: str) -> None:
        self._icon_kind = kind
        # A newly painted icon changes the cache key. Windows ignores
        # NIM_MODIFY when Qt repeats the QIcon it already failed to draw,
        # which leaves a blank slot after the tooltip already says 就绪.
        self.setIcon(_fresh_microphone_icon(kind))

    def show(self) -> None:
        _install_helper_creation_guard()
        super().show()
        self._schedule_icon_republish()
        _hide_tray_message_window()

    def _schedule_icon_republish(self) -> None:
        if sys.platform != "win32":
            return
        from PyQt6.QtCore import QTimer

        for old in getattr(self, "_icon_republish_timers", ()):
            old.stop()
            old.deleteLater()
        timers: list[QTimer] = []
        for delay in (0, 400):
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self._republish_icon)
            timer.timeout.connect(timer.deleteLater)
            timer.start(delay)
            timers.append(timer)
        self._icon_republish_timers = timers

    def _republish_icon(self) -> None:
        self._apply_icon_kind(self._icon_kind)

    def reapply_theme(self) -> None:
        _paint_menu(self._menu)
        _paint_menu(self._model_menu)
        _paint_menu(self._end_meeting_menu)
        kind = getattr(self, "_icon_kind", "normal")
        self._rebuild_icons()
        self._apply_icon_kind(kind)
        self._publish_window_icon()

    def _publish_window_icon(self) -> None:
        from PyQt6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is None:
            return
        icon = _fresh_microphone_icon("normal")
        app.setWindowIcon(icon)
        for widget in app.topLevelWidgets():
            try:
                widget.setWindowIcon(icon)
            except RuntimeError:
                continue

    def _setup_menu(self):
        menu = _UpwardContextMenu(self)
        self._menu = menu
        _paint_menu(menu)

        self._status_action = menu.addAction(self._status_summary)
        self._status_action.setEnabled(False)
        menu.addSeparator()

        settings_action = menu.addAction("打开 SayInk")
        settings_action.triggered.connect(self.open_settings.emit)

        update_action = menu.addAction("检查更新")
        update_action.triggered.connect(self.check_update_requested.emit)

        history_action = menu.addAction("历史")
        history_action.triggered.connect(self.history_requested.emit)

        menu.addSeparator()
        self._stop_continuous_action = QAction("结束持续转写并保留内容", menu)
        self._stop_continuous_action.triggered.connect(self.stop_continuous_requested.emit)
        self._end_meeting_menu = QMenu("结束会议", menu)
        _paint_menu(self._end_meeting_menu)
        self._confirm_end_meeting_action = self._end_meeting_menu.addAction("确认结束本场会议")
        self._confirm_end_meeting_action.triggered.connect(self._on_confirm_end_meeting)
        self._continue_meeting_action = self._end_meeting_menu.addAction("继续记录")
        self._meeting_toggle_action = menu.addAction("开始会议")
        self._meeting_toggle_action.triggered.connect(self._on_meeting_toggle)
        meetings_action = menu.addAction("会议记录")
        meetings_action.triggered.connect(self.meetings_requested.emit)
        self._meeting_in_progress = False
        menu.addSeparator()

        self._model_menu = menu.addMenu("切换模型")
        _paint_menu(self._model_menu)
        empty = self._model_menu.addAction("正在加载")
        empty.setEnabled(False)

        self._auto_start_action = menu.addAction("开机时自动启动")
        self._auto_start_action.setCheckable(True)
        self._auto_start_action.toggled.connect(self.auto_start_toggled.emit)

        menu.addSeparator()

        quit_action = menu.addAction("退出")
        quit_action.triggered.connect(self.quit_app.emit)

        menu.aboutToShow.connect(self._on_menu_about_to_show)
        menu.aboutToHide.connect(self._on_menu_about_to_hide)
        self.setContextMenu(menu)

    def _on_meeting_toggle(self) -> None:
        if not self._meeting_in_progress:
            self.meeting_start_requested.emit()

    def _on_confirm_end_meeting(self) -> None:
        if self._meeting_in_progress:
            self.end_meeting_requested.emit()

    def _sync_dynamic_session_actions(self) -> None:
        if self._menu is None:
            return
        actions = self._menu.actions()
        present = self._stop_continuous_action in actions
        want_stop = bool(self._continuous_listening and not self._meeting_in_progress)
        if want_stop and not present:
            self._menu.insertAction(self._meeting_toggle_action, self._stop_continuous_action)
        elif not want_stop and present:
            self._menu.removeAction(self._stop_continuous_action)

    def set_continuous_listening(self, active: bool) -> None:
        """Show or hide 「结束持续转写并保留内容」 in the tray menu."""
        self._continuous_listening = bool(active)
        self._sync_dynamic_session_actions()

    def set_meeting_in_progress(self, active: bool) -> None:
        """Flip the tray entry between 1-click 「开始会议」 and submenu 「结束会议」."""
        self._meeting_in_progress = bool(active)
        if self._meeting_in_progress:
            self._meeting_toggle_action.setText("结束会议")
            self._meeting_toggle_action.setMenu(self._end_meeting_menu)
        else:
            self._meeting_toggle_action.setMenu(None)
            self._meeting_toggle_action.setText("开始会议")
        self._sync_dynamic_session_actions()

    def show_update_notice(self, body: str) -> None:
        """Tray balloon for a new version. Other notices clear this kind."""
        super().showMessage(
            "SayInk",
            body,
            QSystemTrayIcon.MessageIcon.Information,
            8000,
        )
        self._notice_kind = "update"

    def showMessage(self, title, message, icon=QSystemTrayIcon.MessageIcon.Information, msecs=10000):
        self._notice_kind = ""
        super().showMessage(title, message, icon, msecs)

    @property
    def notice_kind(self) -> str:
        return self._notice_kind

    def update_models(self, downloaded_models: list[dict], active_id: str):
        active_name = ""
        for model in downloaded_models:
            if model.get("id") == active_id:
                active_name = str(model.get("name") or "")
                break
        entries = tuple((m.get("id"), _model_choice_label(m)) for m in downloaded_models)
        if (
            getattr(self, "_rendered_model_entries", None) == entries
            and (
                (not downloaded_models and self._model_group is None)
                or (bool(downloaded_models) and self._model_group is not None)
            )
        ):
            self._model_menu.setTitle(_model_switch_title(active_name))
            if self._model_group is not None:
                for act in self._model_group.actions():
                    act.setChecked(act.data() == active_id)
            return

        if self._model_group is not None:
            try:
                self._model_group.triggered.disconnect()
            except (TypeError, RuntimeError):
                pass

        self._model_menu.clear()
        self._model_menu.setTitle(_model_switch_title(active_name))
        self._rendered_model_entries = entries

        if not downloaded_models:
            empty = self._model_menu.addAction("暂无已下载模型")
            empty.setEnabled(False)
            self._model_group = None
            return

        self._model_group = QActionGroup(self._model_menu)
        self._model_group.setExclusive(True)

        for m in downloaded_models:
            action = self._model_menu.addAction(_model_choice_label(m))
            action.setCheckable(True)
            action.setChecked(m["id"] == active_id)
            action.setData(m["id"])
            self._model_group.addAction(action)

        self._model_group.triggered.connect(
            lambda a: self.model_switched.emit(a.data())
        )

    @staticmethod
    def _double_click_window_s() -> float:
        from PyQt6.QtWidgets import QApplication

        app = QApplication.instance()
        interval_ms = app.doubleClickInterval() if app is not None else 400
        return max(0.18, min(0.55, float(interval_ms) / 1000.0))

    @pyqtSlot(QSystemTrayIcon.ActivationReason)
    def _on_activated(self, reason):
        import time

        # Respond immediately on the first left-click (Trigger) with zero
        # doubleClickInterval delay, and debounce any follow-up DoubleClick /
        # trailing NIN_SELECT within the double-click window so a double-click
        # still opens the window exactly once without toggling it back shut.
        if sys.platform == "win32":
            if reason == QSystemTrayIcon.ActivationReason.Context:
                self._disarm_single_click()
                return
            _hide_tray_message_window()
            now = time.monotonic()
            if reason == QSystemTrayIcon.ActivationReason.Trigger:
                if now < getattr(self, "_suppress_wake_until", 0.0):
                    return
                self._suppress_wake_until = now + self._double_click_window_s()
                self._request_wake()
            elif reason == QSystemTrayIcon.ActivationReason.DoubleClick:
                if now < getattr(self, "_suppress_wake_until", 0.0):
                    self._suppress_wake_until = max(self._suppress_wake_until, now + 0.25)
                    return
                self._suppress_wake_until = now + 0.25
                self._request_wake()
            return
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._request_wake()

    def _request_wake(self, *, defer_from_sync_win32: bool = False) -> None:
        """Open the main window after the tray menu releases the UI thread.

        A visible tray menu runs a modal loop on Windows. Showing or activating
        the main window inside that loop leaves the window under a spinning
        cursor until the menu closes.
        """
        # The tray's hidden owner is a real captioned window. Hide it before
        # the main window is built. The shell shows that owner again after
        # this handler returns, so pin it once more on the next turn.
        _hide_tray_message_window()
        self._schedule_tray_owner_hide()
        if self._menu_is_open:
            self._wake_after_menu = True
            menu = self._menu
            if menu is not None and menu.isVisible():
                menu.close()
            return
        if defer_from_sync_win32 or in_win32_input_sync_call():
            self._queue_wake()
            return
        self.wake_island.emit()

    def _queue_wake(self) -> None:
        if getattr(self, "_wake_queued", False):
            return
        self._wake_queued = True
        from PyQt6.QtCore import QTimer

        QTimer.singleShot(0, self._emit_queued_wake)

    def _emit_queued_wake(self) -> None:
        if not getattr(self, "_wake_queued", False):
            return
        self._wake_queued = False
        if self._menu_is_open:
            self._wake_after_menu = True
            return
        self.wake_island.emit()

    def _schedule_tray_owner_hide(self) -> None:
        from PyQt6.QtCore import QTimer

        QTimer.singleShot(0, _hide_tray_message_window)

    def _on_menu_about_to_show(self) -> None:
        self._menu_is_open = True
        self._disarm_single_click()
        self.menu_about_to_show.emit()

    def _on_menu_about_to_hide(self) -> None:
        self._menu_is_open = False
        self.menu_closed.emit()
        if not self._wake_after_menu:
            return
        self._wake_after_menu = False
        from PyQt6.QtCore import QTimer

        QTimer.singleShot(0, self.wake_island.emit)

    def _disarm_single_click(self) -> None:
        self._wake_queued = False
        self._suppress_wake_until = 0.0
        timer = getattr(self, "_single_click_timer", None)
        if timer is not None:
            timer.stop()

    def set_recording(self, is_recording: bool):
        if self._flash_timer is not None:
            self._flash_timer.stop()
            self._flash_timer.deleteLater()
            self._flash_timer = None
        self._flash_restore_recording = bool(is_recording)
        self._apply_icon_kind("recording" if is_recording else "normal")

    def flash_attention(self, duration_ms: int = 450) -> None:
        """Brief tray icon flash for soft feedback (e.g. short-tap during cooldown)."""
        from PyQt6.QtCore import QTimer

        if self._flash_timer is not None:
            self._flash_timer.stop()
            self._flash_timer.deleteLater()
        if self._icon_kind != "attention":
            self._flash_restore_recording = self._icon_kind == "recording"
        self._apply_icon_kind("attention")
        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)

        def _restore():
            timer = self._flash_timer
            self._flash_timer = None
            if timer is not None:
                timer.deleteLater()
            self._apply_icon_kind(
                "recording" if self._flash_restore_recording else "normal"
            )

        self._flash_timer.timeout.connect(_restore)
        self._flash_timer.start(max(120, int(duration_ms)))

    def set_status_summary(self, text: str) -> None:
        self._status_summary = (text or "就绪").strip()
        self._status_action.setText(self._status_summary)
        self._idle_tooltip = f"SayInk - {self._status_summary}"
        if self._activity_state is None:
            self.setToolTip(self._idle_tooltip)
        # Tooltip updates succeed even when the shell never accepted the glyph.
        # Push the icon again once we know the tray slot exists.
        self._republish_icon()

    def set_activity_tooltip(self, state: str | None):
        """Brief tray hint for background work. None = idle."""
        if state is None:
            self._activity_state = None
            self.setToolTip(self._idle_tooltip)
            return
        lines = {
            "recording": "录音中",
            "recognizing": "正在识别",
            "polishing": "润色中",
            "correcting": "纠错中",
            "listening": "正在听",
            "loading": "模型载入中",
            "meeting": MEETING_STATE_LABELS["live"],
            "meeting_paused": MEETING_STATE_LABELS["paused"],
        }
        label = lines.get(state, "")
        if label:
            self._activity_state = state
            self.setToolTip(f"SayInk - {label}")
        else:
            self._activity_state = None
            self.setToolTip(self._idle_tooltip)

    def set_auto_start(self, enabled: bool):
        self._auto_start_action.blockSignals(True)
        self._auto_start_action.setChecked(enabled)
        self._auto_start_action.blockSignals(False)

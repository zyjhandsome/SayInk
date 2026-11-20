import hashlib
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, ClassVar

import pyperclip
from PyQt6.QtCore import QTimer

from sayink.platform.window_input import (
    CONSOLE_HOST_EXES as _CONSOLE_HOST_EXES,
    TERMINAL_APP_EXES as _TERMINAL_APP_EXES,
    TERMINAL_CTRL_SHIFT_V_EXES as _TERMINAL_CTRL_SHIFT_V_EXES,
    TERMINAL_SHIFT_INSERT_EXES as _TERMINAL_SHIFT_INSERT_EXES,
    _VK_MENU_MASK as _VK_MENU_MASK,
    get_foreground_window_win32 as _get_foreground_window_win32,
    held_stray_modifiers as _held_stray_modifiers,
    is_ctrl_physically_held as _is_ctrl_physically_held,
    is_terminal_process,
    normalize_process_exe,
    send_paste_shortcut_win32,
)

log = logging.getLogger("SayInk")

PASTE_DELAY_MS = 150
VERIFY_AFTER_PASTE_MS = 120
# Heavy Electron/Office/IDE targets can consume Ctrl+V lazily (>500ms); restoring
# at 800ms prevents pasting the user's old clipboard instead of the transcript.
RESTORE_CLIPBOARD_DELAY_MS = 800
RESTORE_CLIPBOARD_RETRY_DELAY_MS = 150
MAX_RESTORE_CLIPBOARD_RETRIES = 2
# Hard cap on multi-format Win32 clipboard backup to prevent OOM on huge bitmaps.
MAX_CLIPBOARD_SNAPSHOT_BYTES = 32 * 1024 * 1024

_REAL_PYPERCLIP_COPY = pyperclip.copy
_REAL_PYPERCLIP_PASTE = pyperclip.paste

# Standard Win32 HGLOBAL-backed clipboard formats safe to snapshot & restore,
# plus GDI/handle/synthesized formats that must never be GlobalLock'd or re-set.
_CF_TEXT = 1
_CF_BITMAP = 2
_CF_METAFILEPICT = 3
_CF_OEMTEXT = 7
_CF_DIB = 8
_CF_PALETTE = 9
_CF_UNICODETEXT = 13
_CF_ENHMETAFILE = 14
_CF_HDROP = 15
_CF_LOCALE = 16
_CF_DIBV5 = 17
_STANDARD_SNAPSHOT_FORMATS = (_CF_UNICODETEXT, _CF_DIBV5, _CF_DIB, _CF_HDROP)
_UNSAFE_STANDARD_FORMATS = frozenset(
    {
        _CF_TEXT,
        _CF_BITMAP,
        _CF_METAFILEPICT,
        _CF_OEMTEXT,
        _CF_PALETTE,
        _CF_ENHMETAFILE,
        _CF_LOCALE,
    }
)
_NAMED_SNAPSHOT_FORMATS = (
    "PNG",
    "image/png",
    "HTML Format",
    "Rich Text Format",
    "Preferred DropEffect",
)
CLIPBOARD_EXCLUSION_FORMAT_NAMES = (
    "ExcludeClipboardContentFromMonitorProcessing",
    "CanIncludeInClipboardHistory",
    "CanUploadToCloudClipboard",
)
_SKIPPED_REGISTERED_FORMAT_NAMES = frozenset(
    {name.lower() for name in CLIPBOARD_EXCLUSION_FORMAT_NAMES}
    | {
        "clipboard viewer ignore",
        "dataobject",
        "ole private data",
    }
)
_GMEM_MOVEABLE = 0x0002
_GCS_COMPSTR = 0x0008
_GWL_STYLE = -16
_ES_READONLY = 0x0800
_DESKTOP_SWITCHDESKTOP = 0x0100
_ERROR_ACCESS_DENIED = 5
_READONLY_EDIT_CLASSES = frozenset(
    {
        "edit",
        "richedit",
        "richedit20a",
        "richedit20w",
        "richedit50w",
        "msftedit_class",
    }
)
_TERMINAL_WINDOW_CLASSES = frozenset(
    {
        "CASCADIA_HOSTING_WINDOW_CLASS",
        "mintty",
        "PuTTY",
        "ConsoleWindowClass",
        "PseudoConsoleWindow",
    }
)


@dataclass(frozen=True)
class PasteTargetSnapshot:
    """Privacy-safe snapshot of target window & sub-focus at utterance start."""

    hwnd: int
    focus_hwnd: int = 0
    title_digest: str = ""
    captured_at: float = 0.0
    desktop_locked: bool = False


@dataclass
class ClipboardSnapshot:
    """Multi-format Win32 clipboard backup (or text fallback when mocked)."""

    win32_entries: list[tuple[int, str, bytes] | tuple[int, bytes]] = field(default_factory=list)
    text_fallback: str | None = None
    use_win32: bool = False
    oversized: bool = False
    open_failed: bool = False

    def __bool__(self) -> bool:
        if self.open_failed or self.oversized:
            return False
        if self.use_win32:
            return bool(self.win32_entries)
        return bool(self.text_fallback)


_SNAPSHOT_LOCK = threading.Lock()
_REMEMBERED_TARGET_SNAPSHOTS: dict[int, PasteTargetSnapshot] = {}
_MAX_REMEMBERED_SNAPSHOTS = 16
_SNAPSHOT_TTL_SEC = 300.0
_ACTIVE_PASTE_MODE = "ctrl_v"


@dataclass(frozen=True)
class PasteResult:
    """Evidence-based output result; `sent` does not claim content verification."""

    status: str
    target_app: str = ""
    detail: str = ""


def get_foreground_window_info() -> tuple:
    """Return ``(hwnd, title, pid)`` on Windows 10/11, or ``(0, "", 0)`` in headless non-Windows environments."""
    if sys.platform == "win32":
        return _get_foreground_window_win32()
    return 0, "", 0


def _process_name_from_window_info(info: tuple) -> str:
    """Resolve a captured window's process basename without retaining its title."""
    try:
        if sys.platform != "win32":
            return ""
        if len(info) < 3:
            return ""
        pid = info[2]
        if not pid:
            return ""
        import win32api
        import win32con
        import win32process

        handle = win32api.OpenProcess(
            win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ,
            False,
            pid,
        )
        try:
            path = win32process.GetModuleFileNameEx(handle, 0)
        finally:
            try:
                win32api.CloseHandle(handle)
            except Exception:
                log.debug("关闭进程句柄失败", exc_info=True)
        return os.path.basename(path) if path else ""
    except Exception:
        log.debug("完整进程查询失败，改用受限查询", exc_info=True)
        return _process_name_limited(info)


_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TOKEN_QUERY = 0x0008
_TOKEN_INTEGRITY_LEVEL = 25
_configured_token_kernel32 = None
_configured_token_advapi32 = None


def _configure_token_kernel32(dll):
    import ctypes
    from ctypes import wintypes

    open_proc = getattr(dll, "OpenProcess", None)
    if open_proc is not None and hasattr(open_proc, "argtypes"):
        open_proc.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_proc.restype = wintypes.HANDLE
    query_img = getattr(dll, "QueryFullProcessImageNameW", None)
    if query_img is not None and hasattr(query_img, "argtypes"):
        query_img.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        query_img.restype = wintypes.BOOL
    close_fn = getattr(dll, "CloseHandle", None)
    if close_fn is not None and hasattr(close_fn, "argtypes"):
        close_fn.argtypes = [wintypes.HANDLE]
        close_fn.restype = wintypes.BOOL
    return dll


def _configure_token_advapi32(dll):
    import ctypes
    from ctypes import wintypes

    open_tok = getattr(dll, "OpenProcessToken", None)
    if open_tok is not None and hasattr(open_tok, "argtypes"):
        open_tok.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        open_tok.restype = wintypes.BOOL
    get_info = getattr(dll, "GetTokenInformation", None)
    if get_info is not None and hasattr(get_info, "argtypes"):
        get_info.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        get_info.restype = wintypes.BOOL
    get_count = getattr(dll, "GetSidSubAuthorityCount", None)
    if get_count is not None and hasattr(get_count, "argtypes"):
        get_count.argtypes = [ctypes.c_void_p]
        get_count.restype = ctypes.POINTER(ctypes.c_ubyte)
    get_sub = getattr(dll, "GetSidSubAuthority", None)
    if get_sub is not None and hasattr(get_sub, "argtypes"):
        get_sub.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        get_sub.restype = ctypes.POINTER(wintypes.DWORD)
    return dll


def _resolve_token_kernel32():
    import ctypes

    global _configured_token_kernel32
    current = getattr(getattr(ctypes, "windll", None), "kernel32", None)
    if (
        sys.platform == "win32"
        and hasattr(ctypes, "WinDLL")
        and isinstance(current, ctypes.WinDLL)
    ):
        if _configured_token_kernel32 is None:
            _configured_token_kernel32 = _configure_token_kernel32(
                ctypes.WinDLL("kernel32", use_last_error=True)
            )
        return _configured_token_kernel32
    return _configure_token_kernel32(current) if current is not None else None


def _resolve_token_advapi32():
    import ctypes

    global _configured_token_advapi32
    current = getattr(getattr(ctypes, "windll", None), "advapi32", None)
    if (
        sys.platform == "win32"
        and hasattr(ctypes, "WinDLL")
        and isinstance(current, ctypes.WinDLL)
    ):
        if _configured_token_advapi32 is None:
            _configured_token_advapi32 = _configure_token_advapi32(
                ctypes.WinDLL("advapi32", use_last_error=True)
            )
        return _configured_token_advapi32
    return _configure_token_advapi32(current) if current is not None else None


def _process_name_limited(info: tuple) -> str:
    """Elevated processes refuse VM_READ but allow a limited image-name query."""
    if sys.platform != "win32" or len(info) < 3 or not info[2]:
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = _resolve_token_kernel32()
        if kernel32 is None:
            return ""
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(info[2]))
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return ""
            return os.path.basename(buf.value)
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        log.debug("受限进程名查询失败", exc_info=True)
        return ""


def _integrity_rid(pid: int) -> int | None:
    """Mandatory integrity RID of a process; -1 when its token is off limits."""
    import ctypes
    from ctypes import wintypes

    kernel32 = _resolve_token_kernel32()
    advapi32 = _resolve_token_advapi32()
    if kernel32 is None or advapi32 is None:
        return None

    process = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not process:
        return None
    token = wintypes.HANDLE()
    try:
        if not advapi32.OpenProcessToken(process, _TOKEN_QUERY, ctypes.byref(token)):
            return -1
        try:
            needed = wintypes.DWORD()
            advapi32.GetTokenInformation(token, _TOKEN_INTEGRITY_LEVEL, None, 0, ctypes.byref(needed))
            buf = ctypes.create_string_buffer(needed.value or 64)
            if not advapi32.GetTokenInformation(
                token, _TOKEN_INTEGRITY_LEVEL, buf, len(buf), ctypes.byref(needed)
            ):
                return None
            sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
            if not sid:
                return None
            count_ptr = advapi32.GetSidSubAuthorityCount(sid)
            if not count_ptr or count_ptr[0] == 0:
                return None
            sub_ptr = advapi32.GetSidSubAuthority(sid, count_ptr[0] - 1)
            return int(sub_ptr[0]) if sub_ptr else None
        finally:
            kernel32.CloseHandle(token)
    finally:
        kernel32.CloseHandle(process)


def target_rejects_synthetic_input(info: tuple) -> bool:
    """True when Windows UIPI will silently drop our Ctrl+V (target is elevated).

    Unknown cases return False so paste behaves as before.
    """
    if sys.platform != "win32" or len(info) < 3 or not info[2]:
        return False
    try:
        own = _integrity_rid(os.getpid())
        target = _integrity_rid(int(info[2]))
    except Exception:
        log.debug("读取完整性级别失败，按可粘贴处理", exc_info=True)
        return False
    if own is None or own < 0 or target is None:
        return False
    return target < 0 or target > own


def _hash_window_title(title: str) -> str:
    cleaned = (title or "").strip()
    if not cleaned:
        return ""
    return hashlib.blake2b(cleaned.encode("utf-8", errors="replace"), digest_size=8).hexdigest()


def _get_gui_focus_hwnd(hwnd: int) -> int:
    """Return child focus HWND inside the foreground window's thread via GetGUIThreadInfo."""
    if sys.platform != "win32" or not hwnd:
        return 0
    try:
        import ctypes
        from ctypes import wintypes

        class _RECT(ctypes.Structure):
            _fields_ = [
                ("left", wintypes.LONG),
                ("top", wintypes.LONG),
                ("right", wintypes.LONG),
                ("bottom", wintypes.LONG),
            ]

        class _GUITHREADINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("hwndActive", wintypes.HWND),
                ("hwndFocus", wintypes.HWND),
                ("hwndCapture", wintypes.HWND),
                ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND),
                ("hwndCaret", wintypes.HWND),
                ("rcCaret", _RECT),
            ]

        user32 = ctypes.WinDLL("user32")
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(_GUITHREADINFO)]
        user32.GetGUIThreadInfo.restype = wintypes.BOOL

        pid = wintypes.DWORD(0)
        tid = user32.GetWindowThreadProcessId(wintypes.HWND(int(hwnd)), ctypes.byref(pid))
        if not tid:
            return 0
        gui = _GUITHREADINFO()
        gui.cbSize = ctypes.sizeof(_GUITHREADINFO)
        if not user32.GetGUIThreadInfo(tid, ctypes.byref(gui)):
            return 0
        return int(gui.hwndFocus or 0)
    except Exception:
        return 0


def _is_desktop_locked() -> bool:
    """True when the active Windows input desktop is locked (Win+L) or UAC secure desktop."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        user32.OpenInputDesktop.restype = wintypes.HANDLE
        user32.CloseDesktop.argtypes = [wintypes.HANDLE]
        user32.CloseDesktop.restype = wintypes.BOOL

        hdesk = user32.OpenInputDesktop(0, False, _DESKTOP_SWITCHDESKTOP)
        if hdesk:
            user32.CloseDesktop(hdesk)
            return False
        return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
    except Exception:
        return False


def build_paste_target_snapshot(info: tuple) -> PasteTargetSnapshot:
    locked = _is_desktop_locked()
    if not info:
        return PasteTargetSnapshot(hwnd=0, desktop_locked=locked)
    hwnd = int(info[0] or 0)
    title = str(info[1] or "") if len(info) >= 2 else ""
    if not hwnd:
        return PasteTargetSnapshot(hwnd=0, desktop_locked=locked)
    return PasteTargetSnapshot(
        hwnd=hwnd,
        focus_hwnd=_get_gui_focus_hwnd(hwnd),
        title_digest=_hash_window_title(title),
        captured_at=time.monotonic(),
        desktop_locked=locked,
    )


def capture_paste_target_snapshot(hwnd: int | None = None) -> PasteTargetSnapshot | None:
    """Capture an immutable ``PasteTargetSnapshot`` without mutating global state."""
    if hwnd is not None:
        target = int(hwnd or 0)
        if target <= 0:
            return None
        info = get_foreground_window_info()
        if info and int(info[0] or 0) == target:
            if TextPaster.is_own_window(info):
                return None
            return build_paste_target_snapshot(info)
        return PasteTargetSnapshot(
            hwnd=target,
            focus_hwnd=_get_gui_focus_hwnd(target),
            title_digest="",
            captured_at=time.monotonic(),
            desktop_locked=_is_desktop_locked(),
        )
    info = get_foreground_window_info()
    if not info:
        return None
    fg_hwnd = int(info[0] or 0)
    if not fg_hwnd or TextPaster.is_own_window(info):
        return None
    return build_paste_target_snapshot(info)


def remember_paste_target(info: tuple | PasteTargetSnapshot | None = None) -> PasteTargetSnapshot:
    """Capture and remember child-focus & title digest for the foreground window."""
    if isinstance(info, PasteTargetSnapshot):
        snap = PasteTargetSnapshot(
            hwnd=info.hwnd,
            focus_hwnd=info.focus_hwnd,
            title_digest=info.title_digest,
            captured_at=info.captured_at or time.monotonic(),
            desktop_locked=info.desktop_locked,
        )
    else:
        resolved = get_foreground_window_info() if info is None else info
        snap = build_paste_target_snapshot(resolved)
    if not snap.hwnd:
        return snap
    now = snap.captured_at or time.monotonic()
    with _SNAPSHOT_LOCK:
        expired = [
            k for k, v in _REMEMBERED_TARGET_SNAPSHOTS.items()
            if (now - v.captured_at) > _SNAPSHOT_TTL_SEC
        ]
        for k in expired:
            _REMEMBERED_TARGET_SNAPSHOTS.pop(k, None)
        if len(_REMEMBERED_TARGET_SNAPSHOTS) >= _MAX_REMEMBERED_SNAPSHOTS and snap.hwnd not in _REMEMBERED_TARGET_SNAPSHOTS:
            _REMEMBERED_TARGET_SNAPSHOTS.pop(next(iter(_REMEMBERED_TARGET_SNAPSHOTS)))
        _REMEMBERED_TARGET_SNAPSHOTS[snap.hwnd] = snap
    return snap


def get_remembered_paste_target(hwnd: int) -> PasteTargetSnapshot | None:
    if not hwnd:
        return None
    now = time.monotonic()
    with _SNAPSHOT_LOCK:
        snap = _REMEMBERED_TARGET_SNAPSHOTS.get(int(hwnd))
        if snap is None:
            return None
        if (now - snap.captured_at) > _SNAPSHOT_TTL_SEC:
            _REMEMBERED_TARGET_SNAPSHOTS.pop(int(hwnd), None)
            return None
        return snap


def detect_session_change(
    before: PasteTargetSnapshot | None,
    after: PasteTargetSnapshot | None,
    *,
    check_subfocus: bool = True,
) -> str:
    """Classify focus/session transitions; returns ``''`` when safe to paste.

    Never aborts solely because ``title_digest`` changed (editor dirty ``*``
    markers, IM unread ``(1)`` badges, or shell OSC title updates) when ``hwnd``
    and (if ``check_subfocus`` is True) ``focus_hwnd`` still match.
    """
    if before is None or after is None:
        return ""
    if before.desktop_locked != after.desktop_locked or after.desktop_locked:
        return "session_changed"
    if not before.hwnd:
        return ""
    if before.hwnd != after.hwnd:
        if after.hwnd == 0 and _is_desktop_locked():
            return "session_changed"
        return "focus_changed"
    if (
        check_subfocus
        and before.focus_hwnd
        and after.focus_hwnd
        and before.focus_hwnd != after.focus_hwnd
    ):
        return "subfocus_changed"
    return ""


_IME_POPUP_CLASSES = (
    "Microsoft.IME.UIManager.CandidateWindow.Host",
    "MSCandUIWindow_Candidate",
    "SoPY_Comp",
    "Sogou_CompWnd",
    "QQPinyinCompWnd",
    "WXIME_Comp",
    "BaiduPinyinCompWnd",
)
_DWMWA_CLOAKED = 14


def _has_visible_ime_composition_window() -> bool:
    """Cross-process fallback: detect an uncloaked, visible CJK IME candidate/composition popup."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class _RECT(ctypes.Structure):
            _fields_ = [
                ("left", wintypes.LONG),
                ("top", wintypes.LONG),
                ("right", wintypes.LONG),
                ("bottom", wintypes.LONG),
            ]

        user32 = ctypes.WinDLL("user32")
        user32.FindWindowExW.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
        ]
        user32.FindWindowExW.restype = wintypes.HWND
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(_RECT)]
        user32.GetWindowRect.restype = wintypes.BOOL

        try:
            dwmapi = ctypes.WinDLL("dwmapi")
            dwmapi.DwmGetWindowAttribute.argtypes = [
                wintypes.HWND,
                wintypes.DWORD,
                ctypes.c_void_p,
                wintypes.DWORD,
            ]
            dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
        except Exception:
            dwmapi = None

        for cls_name in _IME_POPUP_CLASSES:
            popup = wintypes.HWND(0)
            for _ in range(8):
                popup = user32.FindWindowExW(None, popup, cls_name, None)
                if not popup:
                    break
                if not user32.IsWindowVisible(popup):
                    continue
                rect = _RECT()
                if not user32.GetWindowRect(popup, ctypes.byref(rect)):
                    continue
                if (rect.right - rect.left) <= 4 or (rect.bottom - rect.top) <= 4:
                    continue
                if dwmapi is not None:
                    cloaked = wintypes.DWORD(0)
                    hr = dwmapi.DwmGetWindowAttribute(
                        popup,
                        _DWMWA_CLOAKED,
                        ctypes.byref(cloaked),
                        ctypes.sizeof(cloaked),
                    )
                    if hr == 0 and cloaked.value != 0:
                        continue
                return True
    except Exception:
        return False
    return False


def _is_ime_composing(hwnd: int, focus_hwnd: int = 0) -> bool:
    """True when Win32 IMM32 or TSF/IME popup reports an uncommitted CJK composition."""
    if sys.platform != "win32" or (not hwnd and not focus_hwnd):
        return False
    try:
        import ctypes
        from ctypes import wintypes

        imm32 = ctypes.WinDLL("imm32")
        imm32.ImmGetContext.argtypes = [wintypes.HWND]
        imm32.ImmGetContext.restype = wintypes.HANDLE
        imm32.ImmGetCompositionStringW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        imm32.ImmGetCompositionStringW.restype = wintypes.LONG
        imm32.ImmReleaseContext.argtypes = [wintypes.HWND, wintypes.HANDLE]
        imm32.ImmReleaseContext.restype = wintypes.BOOL

        candidates = []
        if focus_hwnd:
            candidates.append(int(focus_hwnd))
        if hwnd and int(hwnd) not in candidates:
            candidates.append(int(hwnd))

        for target in candidates:
            himc = imm32.ImmGetContext(wintypes.HWND(target))
            if not himc:
                continue
            try:
                comp_bytes = imm32.ImmGetCompositionStringW(himc, _GCS_COMPSTR, None, 0)
                if comp_bytes and comp_bytes > 0:
                    return True
            finally:
                imm32.ImmReleaseContext(wintypes.HWND(target), himc)
    except Exception:
        pass
    return _has_visible_ime_composition_window()


def _get_window_class_name(hwnd: int) -> str:
    if sys.platform != "win32" or not hwnd:
        return ""
    try:
        import ctypes

        windll_u32 = getattr(getattr(ctypes, "windll", None), "user32", None)
        get_cls = getattr(windll_u32, "GetClassNameW", None) if windll_u32 is not None else None
        if get_cls is not None and not isinstance(windll_u32, getattr(ctypes, "WinDLL", ())):
            buf = ctypes.create_unicode_buffer(256)
            nlen = int(get_cls(int(hwnd), buf, len(buf)) or 0)
            return str(buf.value[:nlen] if nlen > 0 else buf.value or "")
    except Exception:
        pass
    try:
        import win32gui

        cls_fn = getattr(win32gui, "GetClassName", None)
        if callable(cls_fn):
            return str(cls_fn(int(hwnd)) or "")
    except Exception:
        pass
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32")
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetClassNameW.restype = ctypes.c_int
        buf = ctypes.create_unicode_buffer(256)
        nlen = int(user32.GetClassNameW(wintypes.HWND(int(hwnd)), buf, len(buf)) or 0)
        return str(buf.value[:nlen]) if nlen > 0 else ""
    except Exception:
        return ""


def _get_win32_window_style(hwnd: int) -> int:
    if sys.platform != "win32" or not hwnd:
        return 0
    try:
        import ctypes
        from ctypes import wintypes

        windll_u32 = getattr(getattr(ctypes, "windll", None), "user32", None)
        if windll_u32 is not None and not isinstance(windll_u32, getattr(ctypes, "WinDLL", ())):
            get_long = getattr(windll_u32, "GetWindowLongW", None)
            if callable(get_long):
                return int(get_long(int(hwnd), _GWL_STYLE) or 0)
        user32 = ctypes.WinDLL("user32")
        user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowLongW.restype = wintypes.LONG
        return int(user32.GetWindowLongW(wintypes.HWND(int(hwnd)), _GWL_STYLE) or 0)
    except Exception:
        return 0


def is_native_readonly_edit(hwnd: int, focus_hwnd: int = 0) -> bool:
    """Zero-IPC Win32 ``ES_READONLY`` check strictly for classic ``Edit``/``RichEdit*`` controls.

    Never invokes cross-process UI Automation and never blocks terminals,
    browsers, Electron/WPF apps, or custom widgets.
    """
    check_hwnd = int(focus_hwnd or hwnd or 0)
    if sys.platform != "win32" or not check_hwnd:
        return False
    cls_lower = _get_window_class_name(check_hwnd).strip().lower()
    if not cls_lower:
        return False
    if cls_lower in _READONLY_EDIT_CLASSES or cls_lower.startswith("richedit"):
        style = _get_win32_window_style(check_hwnd)
        return bool(style & _ES_READONLY)
    return False


def _extract_hwnd(info: tuple | int | None) -> int:
    if isinstance(info, int):
        return info
    if isinstance(info, tuple) and info:
        return int(info[0] or 0)
    return 0


def detect_terminal_paste_mode(info: tuple | int, target_app: str = "") -> str:
    """Identify terminal windows and return 'ctrl_shift_v', 'shift_insert', or 'ctrl_v'."""
    hwnd = _extract_hwnd(info)
    cls_name = _get_window_class_name(hwnd)
    exe_lower = normalize_process_exe(target_app)
    if cls_name == "CASCADIA_HOSTING_WINDOW_CLASS" or exe_lower in _TERMINAL_CTRL_SHIFT_V_EXES:
        return "ctrl_shift_v"
    if (
        cls_name in ("mintty", "PuTTY", "ConsoleWindowClass", "PseudoConsoleWindow")
        or exe_lower in _TERMINAL_SHIFT_INSERT_EXES
        or exe_lower in _CONSOLE_HOST_EXES
    ):
        return "shift_insert"
    return "ctrl_v"


def is_terminal_paste_target(info: tuple | int | None, target_app: str = "") -> bool:
    """True when ``target_app`` or window class is any terminal/console host."""
    exe_lower = normalize_process_exe(target_app)
    if exe_lower in _CONSOLE_HOST_EXES or exe_lower in _TERMINAL_APP_EXES or is_terminal_process(target_app):
        return True
    hwnd = _extract_hwnd(info)
    if hwnd and _get_window_class_name(hwnd) in _TERMINAL_WINDOW_CLASSES:
        return True
    return False


def _is_safe_snapshot_format(fmt_int: int, format_name: str) -> bool:
    """True when ``fmt_int`` is a safe HGLOBAL format (not GDI/handle/synthesized/OLE internal)."""
    if fmt_int in _UNSAFE_STANDARD_FORMATS:
        return False
    if 0x0080 <= fmt_int <= 0x008F:  # CF_OWNERDISPLAY / CF_DSP*
        return False
    if 0x0200 <= fmt_int <= 0x03FF:  # CF_PRIVATE* / CF_GDIOBJ*
        return False
    if fmt_int < 0xC000:
        return fmt_int in _STANDARD_SNAPSHOT_FORMATS
    if fmt_int > 0xFFFF or not format_name:
        return False
    return format_name.lower() not in _SKIPPED_REGISTERED_FORMAT_NAMES


def _set_win32_clipboard_exclusion_formats(ctypes_mod, user32, kernel32) -> None:
    """Mark clipboard write transient so Win+V history and Cloud Clipboard ignore it."""
    register_fn = getattr(user32, "RegisterClipboardFormatW", None)
    if not callable(register_fn):
        return
    zero_dword = b"\x00\x00\x00\x00"
    for fmt_name in CLIPBOARD_EXCLUSION_FORMAT_NAMES:
        fmt_id = int(register_fn(fmt_name) or 0)
        if fmt_id <= 0:
            continue
        hmem = kernel32.GlobalAlloc(_GMEM_MOVEABLE, len(zero_dword))
        if not hmem:
            continue
        ptr = kernel32.GlobalLock(hmem)
        if not ptr:
            kernel32.GlobalFree(hmem)
            continue
        try:
            ctypes_mod.memmove(ptr, zero_dword, len(zero_dword))
        finally:
            kernel32.GlobalUnlock(hmem)
        if not user32.SetClipboardData(fmt_id, hmem):
            kernel32.GlobalFree(hmem)


def _win32_clipboard_apis():
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.EnumClipboardFormats.argtypes = [wintypes.UINT]
    user32.EnumClipboardFormats.restype = wintypes.UINT
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.GetClipboardFormatNameW.argtypes = [wintypes.UINT, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClipboardFormatNameW.restype = ctypes.c_int
    user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
    user32.RegisterClipboardFormatW.restype = wintypes.UINT
    user32.GetClipboardSequenceNumber.argtypes = []
    user32.GetClipboardSequenceNumber.restype = wintypes.DWORD

    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
    kernel32.GlobalSize.restype = ctypes.c_size_t

    return ctypes, user32, kernel32


def get_win32_clipboard_sequence_number() -> int | None:
    if sys.platform != "win32":
        return None
    try:
        _ctypes, user32, _kernel32 = _win32_clipboard_apis()
        seq = int(user32.GetClipboardSequenceNumber())
        return seq if seq > 0 else None
    except Exception:
        return None


def capture_win32_clipboard_snapshot(max_bytes: int = MAX_CLIPBOARD_SNAPSHOT_BYTES) -> ClipboardSnapshot:
    """Capture safe multi-format & custom HGLOBAL Win32 clipboard payloads up to ``max_bytes``."""
    snap = ClipboardSnapshot(use_win32=True)
    if sys.platform != "win32":
        return snap
    ctypes, user32, kernel32 = _win32_clipboard_apis()
    opened = False
    for attempt in range(3):
        if user32.OpenClipboard(None):
            opened = True
            break
        time.sleep(0.01 * (attempt + 1))
    if not opened:
        snap.open_failed = True
        return snap

    total_bytes = 0
    try:
        fmt = user32.EnumClipboardFormats(0)
        seen_formats: set[int] = set()
        while fmt:
            fmt_int = int(fmt)
            fmt = user32.EnumClipboardFormats(fmt_int)
            if fmt_int in seen_formats:
                continue
            seen_formats.add(fmt_int)

            format_name = ""
            if fmt_int >= 0xC000:
                name_buf = ctypes.create_unicode_buffer(256)
                nlen = user32.GetClipboardFormatNameW(fmt_int, name_buf, len(name_buf))
                if nlen > 0:
                    format_name = name_buf.value
            if not _is_safe_snapshot_format(fmt_int, format_name):
                continue

            handle = user32.GetClipboardData(fmt_int)
            if not handle:
                continue
            size = int(kernel32.GlobalSize(handle))
            if size <= 0:
                continue
            if total_bytes + size > max_bytes:
                snap.oversized = True
                log.info("原剪贴板内容超过 %d MB 安全上限，跳过覆写恢复以防破坏原剪贴板", max_bytes // (1024 * 1024))
                continue
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                continue
            try:
                raw = ctypes.string_at(ptr, size)
            finally:
                kernel32.GlobalUnlock(handle)
            # Ignore empty CF_UNICODETEXT (just a 2-byte UTF-16 NUL terminator).
            if fmt_int == _CF_UNICODETEXT and raw == b"\x00\x00":
                continue
            snap.win32_entries.append((fmt_int, format_name if fmt_int >= 0xC000 else "", raw))
            total_bytes += size
    finally:
        user32.CloseClipboard()
    return snap


snapshot_win32_clipboard = capture_win32_clipboard_snapshot


def restore_win32_clipboard_snapshot(
    snapshot: ClipboardSnapshot,
    *,
    exclude_from_history: bool = True,
) -> bool:
    """Restore a multi-format Win32 ``ClipboardSnapshot`` atomically."""
    if (
        sys.platform != "win32"
        or snapshot.open_failed
        or snapshot.oversized
        or not snapshot.win32_entries
    ):
        return False
    ctypes, user32, kernel32 = _win32_clipboard_apis()
    opened = False
    for attempt in range(3):
        if user32.OpenClipboard(None):
            opened = True
            break
        time.sleep(0.01 * (attempt + 1))
    if not opened:
        return False
    register_fn = getattr(user32, "RegisterClipboardFormatW", None)
    restored_any = False
    try:
        if not user32.EmptyClipboard():
            return False
        for entry in snapshot.win32_entries:
            if len(entry) == 3:
                fmt_int, format_name, payload = entry
            elif len(entry) == 2:
                fmt_int, payload = entry
                format_name = ""
            else:
                continue
            if not payload:
                continue
            target_fmt = int(fmt_int)
            if target_fmt >= 0xC000 and format_name and callable(register_fn):
                registered = int(register_fn(format_name) or 0)
                if registered > 0:
                    target_fmt = registered
            hmem = kernel32.GlobalAlloc(_GMEM_MOVEABLE, len(payload))
            if not hmem:
                continue
            ptr = kernel32.GlobalLock(hmem)
            if not ptr:
                kernel32.GlobalFree(hmem)
                continue
            try:
                ctypes.memmove(ptr, payload, len(payload))
            finally:
                kernel32.GlobalUnlock(hmem)
            if user32.SetClipboardData(target_fmt, hmem):
                restored_any = True
            else:
                kernel32.GlobalFree(hmem)
        if restored_any and exclude_from_history:
            _set_win32_clipboard_exclusion_formats(ctypes, user32, kernel32)
    finally:
        user32.CloseClipboard()
    return restored_any


def set_win32_clipboard_text(text: str, *, exclude_from_history: bool = False) -> bool:
    """Write ``CF_UNICODETEXT`` via Win32 clipboard APIs with optional Win+V/cloud exclusion."""
    if sys.platform != "win32":
        raise OSError("Win32 clipboard API unavailable off Windows")
    ctypes, user32, kernel32 = _win32_clipboard_apis()
    opened = False
    for attempt in range(3):
        if user32.OpenClipboard(None):
            opened = True
            break
        if attempt < 2:
            time.sleep(0.015 * (attempt + 1))
    if not opened:
        raise RuntimeError("OpenClipboard temporarily locked")

    payload = text.encode("utf-16-le") + b"\x00\x00"
    try:
        if not user32.EmptyClipboard():
            raise RuntimeError("EmptyClipboard failed")
        hmem = kernel32.GlobalAlloc(_GMEM_MOVEABLE, len(payload))
        if not hmem:
            raise MemoryError("GlobalAlloc failed for clipboard text")
        ptr = kernel32.GlobalLock(hmem)
        if not ptr:
            kernel32.GlobalFree(hmem)
            raise MemoryError("GlobalLock failed for clipboard text")
        try:
            ctypes.memmove(ptr, payload, len(payload))
        finally:
            kernel32.GlobalUnlock(hmem)
        if not user32.SetClipboardData(_CF_UNICODETEXT, hmem):
            kernel32.GlobalFree(hmem)
            raise RuntimeError("SetClipboardData(CF_UNICODETEXT) failed")
        if exclude_from_history:
            _set_win32_clipboard_exclusion_formats(ctypes, user32, kernel32)
    finally:
        user32.CloseClipboard()
    return True


def get_foreground_process_name(*, remember_target: bool = True) -> str:
    """Return foreground process basename only (D4 privacy: no window title).

    Records legacy global snapshot only when ``remember_target=True``.
    """
    info = get_foreground_window_info()
    if remember_target:
        remember_paste_target(info)
    return _process_name_from_window_info(info)


def _paste_shortcut() -> None:
    """Trigger native Windows paste shortcut (raises OSError in headless non-Windows tests)."""
    if sys.platform != "win32":
        raise OSError("当前非 Windows 环境不支持原生按键粘贴")
    _paste_shortcut_win32()


def _paste_shortcut_win32(mode: str | None = None) -> None:
    """Send paste keystroke chord with the Win32 keyboard API."""
    send_paste_shortcut_win32(
        mode or _ACTIVE_PASTE_MODE or "ctrl_v",
        held_modifiers_fn=_held_stray_modifiers,
        ctrl_held_fn=_is_ctrl_physically_held,
    )


def _verify_paste_target(hwnd_before: int) -> bool:
    """Best-effort check that focus did not move away before paste completed."""
    if hwnd_before == 0:
        return False
    info_after = get_foreground_window_info()
    return info_after[0] == hwnd_before


class TextPaster:
    OWN_TITLES: ClassVar[set[str]] = {"SayInk 设置", "SayInk"}

    def __init__(self, restore_clipboard: bool = False, *, keep_on_unverified: bool = False):
        self.restore_clipboard = restore_clipboard
        self.keep_on_unverified = keep_on_unverified
        self._paste_seq = 0
        # The user's own clipboard while a delayed restore has not run yet;
        # the next paste must not mistake the previous transcript for it.
        self._saved_snapshot: ClipboardSnapshot | None = None
        self._restore_expected: tuple[int | None, str] | None = None
        self._last_fallback_seq_num: int | None = None
        self._last_fallback_text: str = ""

    @property
    def _restore_snapshot(self) -> ClipboardSnapshot | None:
        return self._saved_snapshot

    @_restore_snapshot.setter
    def _restore_snapshot(self, value: ClipboardSnapshot | None) -> None:
        self._saved_snapshot = value

    def _clear_clipboard_restore(self) -> None:
        self._saved_snapshot = None
        self._restore_expected = None

    def reset_fallback_accumulator(self) -> None:
        """Clear consecutive continuous-mode clipboard fallback accumulation state."""
        self._last_fallback_seq_num = None
        self._last_fallback_text = ""

    def cancel(self) -> None:
        """Forget a paste whose shortcut has not been sent yet and clear pending restore."""
        self._paste_seq += 1
        self._clear_clipboard_restore()

    @staticmethod
    def _use_native_win32_clipboard() -> bool:
        return (
            sys.platform == "win32"
            and pyperclip.copy is _REAL_PYPERCLIP_COPY
            and pyperclip.paste is _REAL_PYPERCLIP_PASTE
        )

    def _write_clipboard_text(self, text: str, *, exclude_from_history: bool) -> None:
        if sys.platform == "win32" and pyperclip.copy is _REAL_PYPERCLIP_COPY:
            set_win32_clipboard_text(text, exclude_from_history=exclude_from_history)
            return
        self._copy_clipboard_with_retry(text)

    def _can_append_to_fallback_clipboard(self, append_fallback: bool) -> bool:
        if not append_fallback or not self._last_fallback_text:
            return False
        current_seq = self._current_clipboard_seq()
        if self._last_fallback_seq_num is not None and current_seq is not None:
            return current_seq == self._last_fallback_seq_num
        if self._last_fallback_seq_num is not None or current_seq is not None:
            return False
        try:
            return pyperclip.paste() == self._last_fallback_text
        except Exception:
            return False

    def _prepare_fallback_text(self, text: str, *, append_fallback: bool) -> str:
        if self._can_append_to_fallback_clipboard(append_fallback):
            return f"{self._last_fallback_text}\n{text}"
        return text

    def _record_fallback_clipboard(self, written_text: str, *, enabled: bool) -> None:
        if not enabled:
            self.reset_fallback_accumulator()
            return
        self._last_fallback_text = written_text
        self._last_fallback_seq_num = self._current_clipboard_seq()

    def _capture_clipboard_snapshot(self) -> ClipboardSnapshot:
        if self._use_native_win32_clipboard():
            try:
                return capture_win32_clipboard_snapshot()
            except Exception:
                log.debug("读取 Win32 多格式剪贴板失败，回退纯文本读取", exc_info=True)
        try:
            old_text = pyperclip.paste()
        except Exception:
            log.debug("读取原剪贴板失败，粘贴后不恢复", exc_info=True)
            old_text = None
        return ClipboardSnapshot(text_fallback=old_text, use_win32=False)

    def _restore_clipboard_snapshot(self, snapshot: ClipboardSnapshot, expected_text: str) -> bool:
        if snapshot.use_win32:
            return bool(restore_win32_clipboard_snapshot(snapshot))
        if snapshot.text_fallback:
            if pyperclip.paste() == expected_text:
                pyperclip.copy(snapshot.text_fallback)
            return True
        return False

    def _current_clipboard_seq(self) -> int | None:
        # The Win32 sequence is available even after pyperclip lazily replaces
        # its copy/paste functions on the first use.
        if sys.platform == "win32":
            return get_win32_clipboard_sequence_number()
        return None

    def _clipboard_paste_blocker(self, expected_seq: int | None, text: str) -> str:
        current_seq = self._current_clipboard_seq()
        if expected_seq is not None and current_seq is not None:
            return "clipboard_changed" if current_seq != expected_seq else ""
        try:
            return "clipboard_changed" if pyperclip.paste() != text else ""
        except Exception:
            log.debug("无法核对剪贴板内容", exc_info=True)
            return "clipboard_unavailable"

    @classmethod
    def is_own_window(cls, info: tuple) -> bool:
        """Check if the foreground window belongs to this process."""
        if sys.platform == "win32" and len(info) >= 3:
            _, title, pid = info[:3]
            if pid == os.getpid():
                return True
        else:
            _, title = info[:2]
        return title in cls.OWN_TITLES

    @classmethod
    def _is_own_window(cls, info: tuple) -> bool:
        return cls.is_own_window(info)

    @staticmethod
    def _copy_clipboard_with_retry(text: str) -> None:
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                pyperclip.copy(text)
                return
            except Exception as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(0.015 * (attempt + 1))
        if last_exc is not None:
            raise last_exc

    def _resolve_initial_paste_target(
        self,
        info: tuple,
        target_hwnd: int | None,
        target_app: str | None,
        *,
        baseline_snapshot: PasteTargetSnapshot | None = None,
        strict_subfocus: bool = True,
    ) -> tuple[int, str, bool, PasteTargetSnapshot | None, str, bool]:
        current_snapshot = build_paste_target_snapshot(info)
        if target_hwnd is None:
            hwnd = info[0]
            has_target = hwnd != 0 and not self._is_own_window(info)
            resolved_app = _process_name_from_window_info(info) if has_target else ""
            if not has_target:
                if current_snapshot.desktop_locked:
                    return hwnd, resolved_app, False, None, "session_changed", False
                return hwnd, resolved_app, False, None, "", False
            effective_baseline = baseline_snapshot or current_snapshot
            change = detect_session_change(
                effective_baseline, current_snapshot, check_subfocus=strict_subfocus
            )
            if change:
                return hwnd, resolved_app, False, effective_baseline, change, False
            if _is_ime_composing(hwnd, current_snapshot.focus_hwnd):
                return hwnd, resolved_app, False, effective_baseline, "ime_active", False
            if is_native_readonly_edit(hwnd, current_snapshot.focus_hwnd):
                return hwnd, resolved_app, False, effective_baseline, "readonly_target", False
            return hwnd, resolved_app, True, effective_baseline, "", False

        hwnd = int(target_hwnd)
        resolved_app = target_app or ""
        baseline = baseline_snapshot or get_remembered_paste_target(hwnd) or PasteTargetSnapshot(hwnd=hwnd)
        if hwnd == 0:
            return hwnd, resolved_app, False, baseline, "", False
        if current_snapshot.desktop_locked or (info[0] == 0 and _is_desktop_locked()):
            return hwnd, resolved_app, False, baseline, "session_changed", False
        if info[0] != hwnd:
            return hwnd, resolved_app, False, baseline, "focus_changed", False
        if self._is_own_window(info):
            return hwnd, resolved_app, False, baseline, "", False
        if target_rejects_synthetic_input(info):
            return hwnd, resolved_app, False, baseline, "elevated", True
        change = detect_session_change(baseline, current_snapshot, check_subfocus=strict_subfocus)
        if change:
            return hwnd, resolved_app, False, baseline, change, True
        if _is_ime_composing(hwnd, current_snapshot.focus_hwnd):
            return hwnd, resolved_app, False, baseline, "ime_active", True
        if is_native_readonly_edit(hwnd, current_snapshot.focus_hwnd):
            return hwnd, resolved_app, False, baseline, "readonly_target", True
        return hwnd, resolved_app, True, baseline, "", True

    def _prepare_clipboard_backup(self) -> ClipboardSnapshot | None:
        if not self.restore_clipboard:
            return None
        if self._restore_expected is not None and not self._clipboard_paste_blocker(*self._restore_expected):
            old_snapshot = self._saved_snapshot
        else:
            old_snapshot = self._capture_clipboard_snapshot()
        if old_snapshot is not None and not bool(old_snapshot):
            self._saved_snapshot = None
            return None
        self._saved_snapshot = old_snapshot
        return old_snapshot

    @staticmethod
    def _recheck_target_before_send(
        hwnd: int,
        baseline_snapshot: PasteTargetSnapshot | None,
        *,
        strict_subfocus: bool = True,
    ) -> tuple[tuple, str]:
        info_now = get_foreground_window_info()
        now_snapshot = build_paste_target_snapshot(info_now)
        if now_snapshot.desktop_locked or (hwnd != 0 and info_now[0] == 0 and _is_desktop_locked()):
            return info_now, "session_changed"
        if hwnd == 0 or info_now[0] != hwnd:
            return info_now, "focus_changed"
        change = detect_session_change(
            baseline_snapshot, now_snapshot, check_subfocus=strict_subfocus
        )
        if change:
            return info_now, change
        if _is_ime_composing(hwnd, now_snapshot.focus_hwnd):
            return info_now, "ime_active"
        if is_native_readonly_edit(hwnd, now_snapshot.focus_hwnd):
            return info_now, "readonly_target"
        return info_now, ""

    def _run_delayed_restore(
        self,
        seq: int,
        old_snapshot: ClipboardSnapshot | None,
        expected_clip_seq: int | None,
        text: str,
        retry_count: int = 0,
    ) -> None:
        if seq != self._paste_seq:
            return
        if old_snapshot is None or not bool(old_snapshot):
            self._clear_clipboard_restore()
            return
        blocker = self._clipboard_paste_blocker(expected_clip_seq, text)
        if blocker == "clipboard_changed":
            log.debug("剪贴板内容已变化，跳过恢复旧剪贴板")
            self._clear_clipboard_restore()
            return
        if blocker == "clipboard_unavailable":
            if retry_count < MAX_RESTORE_CLIPBOARD_RETRIES:
                QTimer.singleShot(
                    RESTORE_CLIPBOARD_RETRY_DELAY_MS,
                    lambda: self._run_delayed_restore(
                        seq, old_snapshot, expected_clip_seq, text, retry_count + 1
                    ),
                )
                return
            self._clear_clipboard_restore()
            return

        restored_ok = False
        try:
            restored_ok = self._restore_clipboard_snapshot(old_snapshot, text)
        except Exception:
            log.debug("恢复原剪贴板尝试失败 (retry=%d)", retry_count, exc_info=True)
            restored_ok = False

        if not restored_ok and retry_count < MAX_RESTORE_CLIPBOARD_RETRIES:
            log.debug("原剪贴板恢复遇到占用，%d ms 后重试 (%d/%d)", RESTORE_CLIPBOARD_RETRY_DELAY_MS, retry_count + 1, MAX_RESTORE_CLIPBOARD_RETRIES)
            QTimer.singleShot(
                RESTORE_CLIPBOARD_RETRY_DELAY_MS,
                lambda: self._run_delayed_restore(
                    seq, old_snapshot, expected_clip_seq, text, retry_count + 1
                ),
            )
            return
        self._clear_clipboard_restore()

    def _run_delayed_verify(
        self,
        seq: int,
        hwnd: int,
        target_app: str,
        old_snapshot: ClipboardSnapshot | None,
        expected_clip_seq: int | None,
        text: str,
        keep_on_unverified: bool,
        finish: Callable[[PasteResult], None],
    ) -> None:
        if seq != self._paste_seq:
            return
        if _verify_paste_target(hwnd):
            if self.restore_clipboard and old_snapshot:
                QTimer.singleShot(
                    RESTORE_CLIPBOARD_DELAY_MS,
                    lambda: self._run_delayed_restore(seq, old_snapshot, expected_clip_seq, text),
                )
            finish(PasteResult("sent", target_app=target_app))
            return
        log.info("粘贴键已发出，但随后焦点切换，无法确认是否已插入")
        clip_changed = bool(self._clipboard_paste_blocker(expected_clip_seq, text))
        detail = "clipboard_changed_after_send" if clip_changed else "focus_changed_after_send"
        if (
            self.restore_clipboard
            and old_snapshot
            and not keep_on_unverified
            and not clip_changed
        ):
            QTimer.singleShot(
                RESTORE_CLIPBOARD_DELAY_MS,
                lambda: self._run_delayed_restore(seq, old_snapshot, expected_clip_seq, text),
            )
        finish(PasteResult("unverified", target_app=target_app, detail=detail))

    def _publish_delayed_fallback(
        self,
        text: str,
        *,
        prior_fallback: str,
        append_fallback: bool,
    ) -> None:
        fallback_text = f"{prior_fallback}\n{text}" if (append_fallback and prior_fallback) else text
        try:
            self._write_clipboard_text(fallback_text, exclude_from_history=False)
            self._record_fallback_clipboard(fallback_text, enabled=append_fallback)
        except Exception:
            log.debug("回退写入剪贴板（保留 Win+V）失败", exc_info=True)

    def _run_delayed_paste(
        self,
        seq: int,
        hwnd: int,
        target_app: str,
        baseline_snapshot: PasteTargetSnapshot | None,
        strict_subfocus: bool,
        append_fallback: bool,
        prior_fallback: str,
        keep_on_unverified: bool,
        old_snapshot: ClipboardSnapshot | None,
        expected_clip_seq: int | None,
        text: str,
        finish: Callable[[PasteResult], None],
    ) -> None:
        global _ACTIVE_PASTE_MODE
        if seq != self._paste_seq:
            return
        info_now, skip_now = self._recheck_target_before_send(
            hwnd, baseline_snapshot, strict_subfocus=strict_subfocus
        )
        clipboard_blocker = self._clipboard_paste_blocker(expected_clip_seq, text)
        if clipboard_blocker:
            self.reset_fallback_accumulator()
            finish(PasteResult("skipped", target_app=target_app, detail=clipboard_blocker))
            return
        if not skip_now and is_terminal_paste_target(info_now, target_app or ""):
            stripped = text.rstrip("\r\n")
            if "\r" in stripped or "\n" in stripped:
                self._publish_delayed_fallback(
                    stripped, prior_fallback=prior_fallback, append_fallback=append_fallback
                )
                finish(PasteResult("skipped", target_app=target_app, detail="terminal_multiline"))
                return
        if skip_now:
            self._publish_delayed_fallback(
                text, prior_fallback=prior_fallback, append_fallback=append_fallback
            )
            status = "skipped" if skip_now in ("readonly_target", "terminal_multiline") else "clipboard"
            finish(PasteResult(status, target_app=target_app, detail=skip_now))
            return
        paste_mode = detect_terminal_paste_mode(info_now, target_app or "")
        prev_mode = _ACTIVE_PASTE_MODE
        _ACTIVE_PASTE_MODE = paste_mode
        try:
            _paste_shortcut()
        except Exception as exc:
            log.warning("模拟粘贴失败: %s", exc)
            self._publish_delayed_fallback(
                text, prior_fallback=prior_fallback, append_fallback=append_fallback
            )
            finish(PasteResult("clipboard", target_app=target_app))
            return
        finally:
            _ACTIVE_PASTE_MODE = prev_mode
        self.reset_fallback_accumulator()
        QTimer.singleShot(
            VERIFY_AFTER_PASTE_MS,
            lambda: self._run_delayed_verify(
                seq,
                hwnd,
                target_app,
                old_snapshot,
                expected_clip_seq,
                text,
                keep_on_unverified,
                finish,
            ),
        )

    def paste_async(
        self,
        text: str,
        callback: Callable[[PasteResult], None],
        *,
        target_hwnd: int | None = None,
        target_app: str | None = None,
        baseline_snapshot: PasteTargetSnapshot | None = None,
        strict_subfocus: bool = True,
        append_fallback: bool = False,
        keep_on_unverified: bool | None = None,
    ) -> None:
        """Copy text, synthesize paste after a short delay, and verify target focus."""
        if not text:
            callback(PasteResult("error", detail="空文本"))
            return

        effective_keep_unverified = (
            self.keep_on_unverified if keep_on_unverified is None else bool(keep_on_unverified)
        )
        info = get_foreground_window_info()
        (
            hwnd,
            resolved_app,
            has_target,
            resolved_baseline,
            skip_detail,
            checked_uipi,
        ) = self._resolve_initial_paste_target(
            info,
            target_hwnd,
            target_app,
            baseline_snapshot=baseline_snapshot,
            strict_subfocus=strict_subfocus,
        )

        if has_target and not checked_uipi and target_rejects_synthetic_input(info):
            log.info("目标窗口以更高权限运行，系统会拦截模拟粘贴；已复制到剪贴板")
            has_target = False
            skip_detail = "elevated"

        if is_terminal_paste_target(hwnd or info, resolved_app):
            text = text.rstrip("\r\n")
            if not text:
                callback(PasteResult("error", target_app=resolved_app, detail="空文本"))
                return
            if has_target and ("\r" in text or "\n" in text):
                has_target = False
                skip_detail = "terminal_multiline"

        self._paste_seq += 1
        seq = self._paste_seq

        if not has_target:
            self._clear_clipboard_restore()
            fallback_text = self._prepare_fallback_text(text, append_fallback=append_fallback)
            try:
                self._write_clipboard_text(fallback_text, exclude_from_history=False)
                self._record_fallback_clipboard(fallback_text, enabled=append_fallback)
            except Exception as exc:
                log.error("写入剪贴板失败: %s", exc)
                callback(PasteResult("error", target_app=resolved_app, detail=str(exc)))
                return
            status = "skipped" if skip_detail in ("readonly_target", "terminal_multiline") else "clipboard"
            callback(PasteResult(status, target_app=resolved_app or "", detail=skip_detail))
            return

        prior_fallback = (
            self._last_fallback_text
            if self._can_append_to_fallback_clipboard(append_fallback)
            else ""
        )
        old_snapshot = self._prepare_clipboard_backup()

        def finish(result: PasteResult) -> None:
            restoring = bool(
                self.restore_clipboard
                and old_snapshot
                and (
                    result.status == "sent"
                    or (
                        result.status == "unverified"
                        and not effective_keep_unverified
                        and result.detail != "clipboard_changed_after_send"
                    )
                )
            )
            if not restoring and seq == self._paste_seq:
                self._clear_clipboard_restore()
            callback(result)

        try:
            self._write_clipboard_text(text, exclude_from_history=True)
        except Exception as exc:
            log.error("写入剪贴板失败: %s", exc)
            finish(PasteResult("error", target_app=resolved_app, detail=str(exc)))
            return

        expected_clip_seq = self._current_clipboard_seq()
        self._restore_expected = (expected_clip_seq, text) if old_snapshot else None

        QTimer.singleShot(
            PASTE_DELAY_MS,
            lambda: self._run_delayed_paste(
                seq,
                hwnd,
                resolved_app,
                resolved_baseline,
                strict_subfocus,
                append_fallback,
                prior_fallback,
                effective_keep_unverified,
                old_snapshot,
                expected_clip_seq,
                text,
                finish,
            ),
        )

    def paste(
        self,
        text: str,
        *,
        target_hwnd: int | None = None,
        target_app: str | None = None,
        baseline_snapshot: PasteTargetSnapshot | None = None,
        strict_subfocus: bool = True,
        append_fallback: bool = False,
        keep_on_unverified: bool | None = None,
    ) -> PasteResult:
        """Synchronous paste entrypoint sharing target, terminal, readonly, and fallback policies."""
        if not text:
            return PasteResult("error", detail="空文本")

        effective_keep_unverified = (
            self.keep_on_unverified if keep_on_unverified is None else bool(keep_on_unverified)
        )
        info = get_foreground_window_info()
        (
            hwnd,
            resolved_app,
            has_target,
            resolved_baseline,
            skip_detail,
            checked_uipi,
        ) = self._resolve_initial_paste_target(
            info,
            target_hwnd,
            target_app,
            baseline_snapshot=baseline_snapshot,
            strict_subfocus=strict_subfocus,
        )
        if has_target and not checked_uipi and target_rejects_synthetic_input(info):
            has_target = False
            skip_detail = "elevated"

        if is_terminal_paste_target(hwnd or info, resolved_app):
            text = text.rstrip("\r\n")
            if not text:
                return PasteResult("error", target_app=resolved_app, detail="空文本")
            if has_target and ("\r" in text or "\n" in text):
                has_target = False
                skip_detail = "terminal_multiline"

        self._paste_seq += 1
        seq = self._paste_seq

        if not has_target:
            self._clear_clipboard_restore()
            fallback_text = self._prepare_fallback_text(text, append_fallback=append_fallback)
            try:
                self._write_clipboard_text(fallback_text, exclude_from_history=False)
                self._record_fallback_clipboard(fallback_text, enabled=append_fallback)
            except Exception as exc:
                return PasteResult("error", target_app=resolved_app, detail=str(exc))
            status = "skipped" if skip_detail in ("readonly_target", "terminal_multiline") else "clipboard"
            return PasteResult(status, target_app=resolved_app or "", detail=skip_detail)

        prior_fallback = (
            self._last_fallback_text
            if self._can_append_to_fallback_clipboard(append_fallback)
            else ""
        )
        old_snapshot = self._prepare_clipboard_backup()
        try:
            self._write_clipboard_text(text, exclude_from_history=True)
        except Exception as exc:
            self._clear_clipboard_restore()
            return PasteResult("error", target_app=resolved_app, detail=str(exc))

        expected_clip_seq = self._current_clipboard_seq()
        self._restore_expected = (expected_clip_seq, text) if old_snapshot else None

        global _ACTIVE_PASTE_MODE
        paste_mode = detect_terminal_paste_mode(info, resolved_app or "")
        prev_mode = _ACTIVE_PASTE_MODE
        _ACTIVE_PASTE_MODE = paste_mode
        try:
            _paste_shortcut()
        except Exception:
            self._clear_clipboard_restore()
            self._publish_delayed_fallback(
                text, prior_fallback=prior_fallback, append_fallback=append_fallback
            )
            return PasteResult("clipboard", target_app=resolved_app)
        finally:
            _ACTIVE_PASTE_MODE = prev_mode

        self.reset_fallback_accumulator()
        if _verify_paste_target(hwnd):
            if self.restore_clipboard and old_snapshot:
                QTimer.singleShot(
                    RESTORE_CLIPBOARD_DELAY_MS,
                    lambda: self._run_delayed_restore(seq, old_snapshot, expected_clip_seq, text),
                )
            return PasteResult("sent", target_app=resolved_app)

        clip_changed = bool(self._clipboard_paste_blocker(expected_clip_seq, text))
        detail = "clipboard_changed_after_send" if clip_changed else "focus_changed_after_send"
        if self.restore_clipboard and old_snapshot and not effective_keep_unverified and not clip_changed:
            QTimer.singleShot(
                RESTORE_CLIPBOARD_DELAY_MS,
                lambda: self._run_delayed_restore(seq, old_snapshot, expected_clip_seq, text),
            )
        else:
            self._clear_clipboard_restore()
        return PasteResult("unverified", target_app=resolved_app, detail=detail)

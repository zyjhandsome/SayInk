import hashlib
import logging
import os
import sys
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, ClassVar

import pyperclip
from PyQt6.QtCore import QTimer

from sayink.platform import SYNTHETIC_KEY_TAG

log = logging.getLogger("SayInk")

PASTE_DELAY_MS = 150
VERIFY_AFTER_PASTE_MS = 120
# Some apps read the clipboard lazily after Ctrl+V; restoring sooner can paste
# the user's old clipboard instead of the transcript.
RESTORE_CLIPBOARD_DELAY_MS = 500
# Hard cap on multi-format Win32 clipboard backup to prevent OOM on huge bitmaps.
MAX_CLIPBOARD_SNAPSHOT_BYTES = 32 * 1024 * 1024

_REAL_PYPERCLIP_COPY = pyperclip.copy
_REAL_PYPERCLIP_PASTE = pyperclip.paste

# Standard Win32 HGLOBAL-backed clipboard formats safe to snapshot & restore.
_CF_DIB = 8
_CF_UNICODETEXT = 13
_CF_HDROP = 15
_CF_DIBV5 = 17
_STANDARD_SNAPSHOT_FORMATS = (_CF_UNICODETEXT, _CF_DIBV5, _CF_DIB, _CF_HDROP)
_NAMED_SNAPSHOT_FORMATS = (
    "PNG",
    "image/png",
    "HTML Format",
    "Rich Text Format",
    "Preferred DropEffect",
)
_GMEM_MOVEABLE = 0x0002
_GCS_COMPSTR = 0x0008

_TERMINAL_CTRL_SHIFT_V_EXES = frozenset({
    "windowsterminal.exe",
    "wt.exe",
    "wezterm-gui.exe",
    "alacritty.exe",
    "kitty.exe",
    "hyper.exe",
    "tabby.exe",
    "rio.exe",
})
_TERMINAL_SHIFT_INSERT_EXES = frozenset({
    "mintty.exe",
    "putty.exe",
})
_TERMINAL_CONSOLE_EXES = frozenset({
    "cmd.exe",
    "powershell.exe",
    "pwsh.exe",
    "conhost.exe",
})


@dataclass(frozen=True)
class PasteTargetSnapshot:
    """Privacy-safe snapshot of target window & sub-focus at utterance start."""

    hwnd: int
    focus_hwnd: int = 0
    title_digest: str = ""
    captured_at: float = 0.0


@dataclass
class ClipboardSnapshot:
    """Multi-format Win32 clipboard backup (or text fallback when mocked)."""

    win32_entries: list[tuple[int, bytes]] = field(default_factory=list)
    text_fallback: str | None = None
    use_win32: bool = False
    oversized: bool = False

    def __bool__(self) -> bool:
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


def _get_foreground_window_win32():
    """Windows: get foreground window info via win32gui. Returns (hwnd, title, pid)."""
    try:
        import win32gui
        import win32process
        hwnd = win32gui.GetForegroundWindow()
        title = win32gui.GetWindowText(hwnd)
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return hwnd, title, pid
    except Exception:
        log.debug("读取前台窗口失败", exc_info=True)
        return 0, "", 0


def _get_foreground_window_macos():
    """macOS: get frontmost process PID and name via osascript."""
    try:
        out = subprocess.check_output(
            [
                "osascript",
                "-e",
                'tell application "System Events" to tell (first process whose frontmost is true) '
                "to return (unix id as text) & linefeed & name",
            ],
            timeout=2,
            text=True,
        )
        pid_text, sep, app_name = out.partition("\n")
        pid = int(pid_text.strip())
        if pid <= 0 or not sep:
            return 0, ""
        return pid, app_name.strip()
    except Exception:
        log.debug("osascript 读取前台应用失败", exc_info=True)
        return 0, ""


def _get_foreground_window_linux():
    """Linux/X11: get active window title via xdotool."""
    try:
        wid = subprocess.check_output(
            ["xdotool", "getactivewindow"], timeout=2, text=True,
        ).strip()
        title = subprocess.check_output(
            ["xdotool", "getactivewindow", "getwindowname"], timeout=2, text=True,
        ).strip()
        return int(wid), title
    except Exception:
        log.debug("xdotool 读取活动窗口失败", exc_info=True)
        return 0, ""


def get_foreground_window_info():
    """Returns (handle, title) of the foreground window, cross-platform.
    On Windows, also provides PID as 3rd element."""
    if sys.platform == "win32":
        return _get_foreground_window_win32()
    elif sys.platform == "darwin":
        return _get_foreground_window_macos()
    else:
        return _get_foreground_window_linux()


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


def _process_name_limited(info: tuple) -> str:
    """Elevated processes refuse VM_READ but allow a limited image-name query."""
    if sys.platform != "win32" or len(info) < 3 or not info[2]:
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
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

    kernel32 = ctypes.windll.kernel32
    advapi32 = ctypes.windll.advapi32
    advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
    advapi32.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    advapi32.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]

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
            count = advapi32.GetSidSubAuthorityCount(sid)[0]
            return int(advapi32.GetSidSubAuthority(sid, count - 1)[0])
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


def build_paste_target_snapshot(info: tuple) -> PasteTargetSnapshot:
    if not info:
        return PasteTargetSnapshot(hwnd=0)
    hwnd = int(info[0] or 0)
    title = str(info[1] or "") if len(info) >= 2 else ""
    if not hwnd:
        return PasteTargetSnapshot(hwnd=0)
    return PasteTargetSnapshot(
        hwnd=hwnd,
        focus_hwnd=_get_gui_focus_hwnd(hwnd),
        title_digest=_hash_window_title(title),
        captured_at=time.monotonic(),
    )


def remember_paste_target(info: tuple | PasteTargetSnapshot | None = None) -> PasteTargetSnapshot:
    """Capture and remember child-focus & title digest for the foreground window."""
    if isinstance(info, PasteTargetSnapshot):
        snap = PasteTargetSnapshot(
            hwnd=info.hwnd,
            focus_hwnd=info.focus_hwnd,
            title_digest=info.title_digest,
            captured_at=info.captured_at or time.monotonic(),
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
) -> bool:
    """True when same top-level HWND switched child focus or chat/tab title."""
    if before is None or after is None:
        return False
    if not before.hwnd or before.hwnd != after.hwnd:
        return False
    if before.focus_hwnd and after.focus_hwnd and before.focus_hwnd != after.focus_hwnd:
        return True
    if before.title_digest and after.title_digest and before.title_digest != after.title_digest:
        return True
    return False


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
        import win32gui

        cls_fn = getattr(win32gui, "GetClassName", None)
        if callable(cls_fn):
            return str(cls_fn(int(hwnd)) or "")
    except Exception:
        pass
    return ""


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
    exe_lower = (target_app or "").strip().lower()
    if cls_name == "CASCADIA_HOSTING_WINDOW_CLASS" or exe_lower in _TERMINAL_CTRL_SHIFT_V_EXES:
        return "ctrl_shift_v"
    if cls_name in ("mintty", "PuTTY") or exe_lower in _TERMINAL_SHIFT_INSERT_EXES:
        return "shift_insert"
    return "ctrl_v"


def is_terminal_window(info: tuple | int, target_app: str = "") -> bool:
    """True for modern terminals, SSH/mintty terminals, and classic Win32 consoles."""
    mode = detect_terminal_paste_mode(info, target_app)
    if mode != "ctrl_v":
        return True
    hwnd = _extract_hwnd(info)
    cls_name = _get_window_class_name(hwnd)
    exe_lower = (target_app or "").strip().lower()
    return cls_name == "ConsoleWindowClass" or exe_lower in _TERMINAL_CONSOLE_EXES


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
        return int(user32.GetClipboardSequenceNumber())
    except Exception:
        return None


def capture_win32_clipboard_snapshot(max_bytes: int = MAX_CLIPBOARD_SNAPSHOT_BYTES) -> ClipboardSnapshot:
    """Capture supported multi-format Win32 HGLOBAL clipboard payloads up to ``max_bytes``."""
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
        return snap

    allowed_names = {name.lower() for name in _NAMED_SNAPSHOT_FORMATS}
    allowed_standard = set(_STANDARD_SNAPSHOT_FORMATS)
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

            keep = fmt_int in allowed_standard
            if not keep and fmt_int >= 0xC000:
                name_buf = ctypes.create_unicode_buffer(256)
                nlen = user32.GetClipboardFormatNameW(fmt_int, name_buf, len(name_buf))
                if nlen > 0 and name_buf.value.lower() in allowed_names:
                    keep = True
            if not keep:
                continue

            handle = user32.GetClipboardData(fmt_int)
            if not handle:
                continue
            size = int(kernel32.GlobalSize(handle))
            if size <= 0:
                continue
            if total_bytes + size > max_bytes:
                snap.oversized = True
                log.info("原剪贴板内容超过 %d MB 安全上限，跳过超限格式备份", max_bytes // (1024 * 1024))
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
            snap.win32_entries.append((fmt_int, raw))
            total_bytes += size
    finally:
        user32.CloseClipboard()
    return snap


def restore_win32_clipboard_snapshot(snapshot: ClipboardSnapshot) -> bool:
    """Restore a multi-format Win32 ``ClipboardSnapshot`` atomically."""
    if sys.platform != "win32" or not snapshot.win32_entries:
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
    restored_any = False
    try:
        if not user32.EmptyClipboard():
            return False
        for fmt_int, payload in snapshot.win32_entries:
            if not payload:
                continue
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
            if user32.SetClipboardData(int(fmt_int), hmem):
                restored_any = True
            else:
                kernel32.GlobalFree(hmem)
    finally:
        user32.CloseClipboard()
    return restored_any


def get_foreground_process_name() -> str:
    """Return foreground process basename only (D4 privacy: no window title).

    Also records a privacy-safe target snapshot (focus HWND + title hash) so
    later ``paste_async`` can detect chat/tab switches within the same HWND.
    """
    info = get_foreground_window_info()
    remember_paste_target(info)
    return _process_name_from_window_info(info)


def _paste_shortcut():
    """Trigger the system paste shortcut, platform-aware.

    Does not use pyautogui: that import loads Pillow, and the frozen app
    crashes while decompressing a Pillow module from the PyInstaller archive.
    """
    if sys.platform == "win32":
        _paste_shortcut_win32()
    elif sys.platform == "darwin":
        subprocess.run(
            [
                "osascript",
                "-e",
                'tell application "System Events" to keystroke "v" using command down',
            ],
            timeout=2,
            check=True,
        )
    else:
        subprocess.run(["xdotool", "key", "--clearmodifiers", "ctrl+v"], timeout=2, check=True)


# Sided VKs: re-pressing the generic VK_SHIFT / VK_MENU lands on the left key,
# so a held right Alt would come back as a left Alt that is never released.
_VK_LSHIFT, _VK_RSHIFT = 0xA0, 0xA1
_VK_LCONTROL, _VK_RCONTROL = 0xA2, 0xA3
_VK_LMENU, _VK_RMENU = 0xA4, 0xA5  # Alt
_VK_LWIN, _VK_RWIN = 0x5B, 0x5C
_VK_INSERT = 0x2D
_SHIFT_VKS = (_VK_LSHIFT, _VK_RSHIFT)
_CTRL_VKS = (0x11, _VK_LCONTROL, _VK_RCONTROL)
_EXTENDED_VKS = frozenset((_VK_RCONTROL, _VK_RMENU, _VK_LWIN, _VK_RWIN, _VK_INSERT))
_KEYEVENTF_EXTENDEDKEY = 0x0001
# Unassigned VK AutoHotkey uses as a "menu mask": pressed between an Alt/Win
# down and up it keeps Windows from opening the menu bar / Start on release.
_VK_MENU_MASK = 0xE8
_STRAY_MODIFIER_VKS = (_VK_LSHIFT, _VK_RSHIFT, _VK_LMENU, _VK_RMENU, _VK_LWIN, _VK_RWIN)


def _held_stray_modifiers(user32) -> list[int]:
    return [vk for vk in _STRAY_MODIFIER_VKS if user32.GetAsyncKeyState(vk) & 0x8000]


def _is_ctrl_physically_held(user32) -> bool:
    return any(bool(user32.GetAsyncKeyState(vk) & 0x8000) for vk in _CTRL_VKS)


def _paste_shortcut_win32(mode: str | None = None):
    """Send paste keystroke with the Win32 keyboard API.

    Preserves physically held Ctrl (never forcefully releases user's physical Ctrl),
    lifts stray Alt/Shift/Win modifiers around the shortcut, and supports
    terminal modes (``ctrl_shift_v`` and ``shift_insert``).
    """
    import ctypes

    effective_mode = mode or _ACTIVE_PASTE_MODE or "ctrl_v"
    user32 = ctypes.windll.user32
    vk_control = 0x11
    vk_shift = 0x10
    vk_v = 0x56
    key_up = 0x0002

    def _tap(vk: int, flags: int) -> None:
        scan = user32.MapVirtualKeyW(vk, 0)
        if vk in _EXTENDED_VKS:
            flags |= _KEYEVENTF_EXTENDEDKEY
        user32.keybd_event(vk, scan, flags, SYNTHETIC_KEY_TAG)

    held = _held_stray_modifiers(user32)
    ctrl_held = _is_ctrl_physically_held(user32)
    for vk in held:
        _tap(vk, key_up)

    if effective_mode == "shift_insert":
        if ctrl_held:
            for cvk in (_VK_LCONTROL, _VK_RCONTROL):
                if user32.GetAsyncKeyState(cvk) & 0x8000:
                    _tap(cvk, key_up)
        _tap(vk_shift, 0)
        _tap(_VK_INSERT, 0)
        _tap(_VK_INSERT, key_up)
        _tap(vk_shift, key_up)
        if ctrl_held:
            for cvk in (_VK_LCONTROL, _VK_RCONTROL):
                if user32.GetAsyncKeyState(cvk) & 0x8000:
                    _tap(cvk, 0)
    elif effective_mode == "ctrl_shift_v":
        if not ctrl_held:
            _tap(vk_control, 0)
        _tap(vk_shift, 0)
        _tap(vk_v, 0)
        _tap(vk_v, key_up)
        _tap(vk_shift, key_up)
        if not ctrl_held:
            _tap(vk_control, key_up)
    else:
        if not ctrl_held:
            _tap(vk_control, 0)
        _tap(vk_v, 0)
        _tap(vk_v, key_up)
        if not ctrl_held:
            _tap(vk_control, key_up)

    for vk in held:
        _tap(vk, 0)
    if any(vk not in _SHIFT_VKS for vk in held):
        _tap(_VK_MENU_MASK, 0)
        _tap(_VK_MENU_MASK, key_up)


def _verify_paste_target(hwnd_before: int) -> bool:
    """Best-effort check that focus did not move away before paste completed."""
    if hwnd_before == 0:
        return False
    info_after = get_foreground_window_info()
    return info_after[0] == hwnd_before


class TextPaster:
    OWN_TITLES: ClassVar[set[str]] = {"SayInk 设置", "SayInk"}

    def __init__(self, restore_clipboard: bool = False):
        self.restore_clipboard = restore_clipboard
        self._paste_seq = 0
        # The user's own clipboard while a delayed restore has not run yet;
        # the next paste must not mistake the previous transcript for it.
        self._saved_clipboard: str | None = None
        self._saved_snapshot: ClipboardSnapshot | None = None
        self._restore_pending = False

    def cancel(self) -> None:
        """Forget a paste whose shortcut has not been sent yet and clear pending restore."""
        self._paste_seq += 1
        self._restore_pending = False
        self._saved_clipboard = None
        self._saved_snapshot = None

    @staticmethod
    def _use_native_win32_clipboard() -> bool:
        return (
            sys.platform == "win32"
            and pyperclip.copy is _REAL_PYPERCLIP_COPY
            and pyperclip.paste is _REAL_PYPERCLIP_PASTE
        )

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

    def _restore_clipboard_snapshot(self, snapshot: ClipboardSnapshot, expected_text: str) -> None:
        if snapshot.use_win32:
            restore_win32_clipboard_snapshot(snapshot)
            return
        if snapshot.text_fallback:
            if pyperclip.paste() == expected_text:
                pyperclip.copy(snapshot.text_fallback)

    def _current_clipboard_seq(self) -> int | None:
        if self._use_native_win32_clipboard():
            return get_win32_clipboard_sequence_number()
        return None

    def _is_own_window(self, info: tuple) -> bool:
        """Check if the foreground window belongs to this process."""
        if sys.platform == "win32" and len(info) >= 3:
            _, title, pid = info
            if pid == os.getpid():
                return True
        else:
            _, title = info[:2]
        return title in self.OWN_TITLES

    def paste(self, text: str) -> str:
        """
        Synchronous paste (no verification). Prefer paste_async in the UI thread.
        Returns: 'pasted', 'clipboard', or 'error:<msg>'.
        """
        if not text:
            return "error:空文本"

        info = get_foreground_window_info()
        hwnd = info[0]
        has_target = hwnd != 0 and not self._is_own_window(info)

        pyperclip.copy(text)

        if has_target:
            _paste_shortcut()
            return "pasted"
        return "clipboard"

    def paste_async(
        self,
        text: str,
        callback: Callable[[PasteResult], None],
        *,
        target_hwnd: int | None = None,
        target_app: str | None = None,
    ) -> None:
        """
        Copy text and attempt paste after a short delay, then verify focus
        stayed on the target window. Invokes callback with result status.

        ``target_hwnd is None`` keeps the legacy behaviour of reading the
        foreground window now. A remembered handle is the window from the
        start of the utterance: keys are sent only if that same window is
        still in front and can accept them.
        """
        if not text:
            callback(PasteResult("error", detail="空文本"))
            return

        info = get_foreground_window_info()
        current_snapshot = build_paste_target_snapshot(info)
        baseline_snapshot: PasteTargetSnapshot | None = None
        skip_detail = ""
        if target_hwnd is None:
            hwnd = info[0]
            has_target = hwnd != 0 and not self._is_own_window(info)
            target_app = _process_name_from_window_info(info) if has_target else ""
            if has_target:
                baseline_snapshot = current_snapshot
                if _is_ime_composing(hwnd, current_snapshot.focus_hwnd):
                    has_target = False
                    skip_detail = "ime_active"
        else:
            hwnd = int(target_hwnd)
            target_app = target_app or ""
            baseline_snapshot = get_remembered_paste_target(hwnd) or current_snapshot
            if hwnd == 0 or info[0] != hwnd:
                has_target = False
                skip_detail = "focus_changed" if hwnd and info[0] != hwnd else ""
            elif self._is_own_window(info) or target_rejects_synthetic_input(info):
                has_target = False
                skip_detail = "elevated" if target_rejects_synthetic_input(info) else ""
            elif detect_session_change(baseline_snapshot, current_snapshot):
                has_target = False
                skip_detail = "session_changed"
            elif _is_ime_composing(hwnd, current_snapshot.focus_hwnd):
                has_target = False
                skip_detail = "ime_active"
            else:
                has_target = True

        self._paste_seq += 1
        seq = self._paste_seq
        old_snapshot: ClipboardSnapshot | None = None
        if self.restore_clipboard:
            if self._restore_pending and self._saved_snapshot is not None:
                old_snapshot = self._saved_snapshot
            else:
                old_snapshot = self._capture_clipboard_snapshot()
                self._saved_snapshot = old_snapshot
                self._saved_clipboard = old_snapshot.text_fallback
            self._restore_pending = bool(old_snapshot)

        report = callback

        def callback(result: PasteResult) -> None:
            restoring = bool(result.status == "sent" and self.restore_clipboard and old_snapshot)
            if not restoring and seq == self._paste_seq:
                self._restore_pending = False
                self._saved_snapshot = None
                self._saved_clipboard = None
            report(result)

        try:
            pyperclip.copy(text)
        except Exception as e:
            log.error("写入剪贴板失败: %s", e)
            callback(PasteResult("error", target_app=target_app, detail=str(e)))
            return

        expected_clip_seq = self._current_clipboard_seq()

        if not has_target:
            callback(PasteResult("clipboard", target_app=target_app or "", detail=skip_detail))
            return

        if target_rejects_synthetic_input(info):
            log.info("目标窗口以更高权限运行，系统会拦截模拟粘贴；已复制到剪贴板")
            callback(PasteResult("clipboard", target_app=target_app, detail="elevated"))
            return

        def _restore_clipboard():
            if seq != self._paste_seq:
                return  # a later paste restores the same original
            self._restore_pending = False
            self._saved_snapshot = None
            self._saved_clipboard = None
            if old_snapshot is None:
                return
            try:
                if expected_clip_seq is not None:
                    now_clip_seq = self._current_clipboard_seq()
                    if now_clip_seq is not None and now_clip_seq != expected_clip_seq:
                        log.debug("检测到粘贴后用户更新了剪贴板，跳过恢复旧剪贴板")
                        return
                self._restore_clipboard_snapshot(old_snapshot, text)
            except Exception:
                log.debug("恢复原剪贴板失败", exc_info=True)

        def _keep_for_manual_paste(detail: str = ""):
            try:
                pyperclip.copy(text)
            except Exception:
                log.debug("重新写入剪贴板失败", exc_info=True)
            callback(PasteResult("clipboard", target_app=target_app, detail=detail))

        def _do_paste():
            global _ACTIVE_PASTE_MODE
            if seq != self._paste_seq:
                return
            info_now = get_foreground_window_info()
            if hwnd == 0 or info_now[0] != hwnd:
                log.info("粘贴前焦点已切换到其他窗口，未发送粘贴键；已保留剪贴板内容")
                _keep_for_manual_paste("focus_changed")
                return
            now_snapshot = build_paste_target_snapshot(info_now)
            if detect_session_change(baseline_snapshot, now_snapshot):
                log.info("粘贴前目标窗口子焦点或会话已切换，未发送粘贴键；已保留剪贴板内容")
                _keep_for_manual_paste("session_changed")
                return
            if _is_ime_composing(hwnd, now_snapshot.focus_hwnd):
                log.info("粘贴前检测到输入法正在拼音组词，未发送粘贴键；已保留剪贴板内容")
                _keep_for_manual_paste("ime_active")
                return
            paste_mode = detect_terminal_paste_mode(info_now, target_app or "")
            prev_mode = _ACTIVE_PASTE_MODE
            _ACTIVE_PASTE_MODE = paste_mode
            try:
                _paste_shortcut()
            except Exception as e:
                log.warning("模拟粘贴失败: %s", e)
                callback(PasteResult("clipboard", target_app=target_app))
                return
            finally:
                _ACTIVE_PASTE_MODE = prev_mode
            QTimer.singleShot(VERIFY_AFTER_PASTE_MS, _verify)

        def _verify():
            if seq != self._paste_seq:
                return
            if _verify_paste_target(hwnd):
                if self.restore_clipboard and old_snapshot:
                    QTimer.singleShot(RESTORE_CLIPBOARD_DELAY_MS, _restore_clipboard)
                callback(PasteResult("sent", target_app=target_app))
            else:
                log.info("粘贴键已发出，但随后焦点切换，无法确认是否已插入；保留剪贴板内容")
                try:
                    pyperclip.copy(text)
                except Exception:
                    log.debug("重新写入剪贴板失败", exc_info=True)
                callback(PasteResult("unverified", target_app=target_app, detail="focus_changed_after_send"))

        QTimer.singleShot(PASTE_DELAY_MS, _do_paste)

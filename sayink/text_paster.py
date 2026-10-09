import logging
import os
import sys
import subprocess
from dataclasses import dataclass
from typing import Callable

import pyperclip
from PyQt6.QtCore import QTimer

log = logging.getLogger("SayInk")

PASTE_DELAY_MS = 150
VERIFY_AFTER_PASTE_MS = 120
# Some apps read the clipboard lazily after Ctrl+V; restoring sooner can paste
# the user's old clipboard instead of the transcript.
RESTORE_CLIPBOARD_DELAY_MS = 500


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
    """macOS: get frontmost application name via osascript."""
    try:
        out = subprocess.check_output(
            ["osascript", "-e",
             'tell application "System Events" to get name of first process whose frontmost is true'],
            timeout=2, text=True,
        ).strip()
        return 1, out
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


def get_foreground_process_name() -> str:
    """Return foreground process basename only (D4 privacy: no window title)."""
    return _process_name_from_window_info(get_foreground_window_info())


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
        subprocess.run(["xdotool", "key", "ctrl+v"], timeout=2, check=True)


# Sided VKs: re-pressing the generic VK_SHIFT / VK_MENU lands on the left key,
# so a held right Alt would come back as a left Alt that is never released.
_VK_LSHIFT, _VK_RSHIFT = 0xA0, 0xA1
_VK_LMENU, _VK_RMENU = 0xA4, 0xA5  # Alt
_VK_LWIN, _VK_RWIN = 0x5B, 0x5C
_SHIFT_VKS = (_VK_LSHIFT, _VK_RSHIFT)
_EXTENDED_VKS = frozenset((_VK_RMENU, _VK_LWIN, _VK_RWIN))
_KEYEVENTF_EXTENDEDKEY = 0x0001
# Unassigned VK AutoHotkey uses as a "menu mask": pressed between an Alt/Win
# down and up it keeps Windows from opening the menu bar / Start on release.
_VK_MENU_MASK = 0xE8
_STRAY_MODIFIER_VKS = (_VK_LSHIFT, _VK_RSHIFT, _VK_LMENU, _VK_RMENU, _VK_LWIN, _VK_RWIN)


def _held_stray_modifiers(user32) -> list[int]:
    return [vk for vk in _STRAY_MODIFIER_VKS if user32.GetAsyncKeyState(vk) & 0x8000]


def _paste_shortcut_win32():
    """Send Ctrl+V with the Win32 keyboard API.

    The hotkey's modifier (Alt in the default Alt+X) is often still
    physically down when the first result arrives, and Ctrl+Shift+V or
    Ctrl+Alt+V mean something else in many apps (paste format only, paste
    special, ...). Lift any stray modifier for the shortcut, then press it
    again so its physical release stays consistent.
    """
    import ctypes

    user32 = ctypes.windll.user32
    vk_control = 0x11
    vk_v = 0x56
    key_up = 0x0002

    def _tap(vk: int, flags: int) -> None:
        scan = user32.MapVirtualKeyW(vk, 0)
        if vk in _EXTENDED_VKS:
            flags |= _KEYEVENTF_EXTENDEDKEY
        user32.keybd_event(vk, scan, flags, 0)

    held = _held_stray_modifiers(user32)
    for vk in held:
        _tap(vk, key_up)
    _tap(vk_control, 0)
    _tap(vk_v, 0)
    _tap(vk_v, key_up)
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
    OWN_TITLES = {"SayInk 设置", "SayInk"}

    def __init__(self, restore_clipboard: bool = False):
        self.restore_clipboard = restore_clipboard
        self._paste_seq = 0
        # The user's own clipboard while a delayed restore has not run yet;
        # the next paste must not mistake the previous transcript for it.
        self._saved_clipboard: str | None = None
        self._restore_pending = False

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

    def paste_async(self, text: str, callback: Callable[[PasteResult], None]) -> None:
        """
        Copy text and attempt paste after a short delay, then verify focus
        stayed on the target window. Invokes callback with result status.
        """
        if not text:
            callback(PasteResult("error", detail="空文本"))
            return

        info = get_foreground_window_info()
        hwnd = info[0]
        has_target = hwnd != 0 and not self._is_own_window(info)
        target_app = _process_name_from_window_info(info) if has_target else ""

        self._paste_seq += 1
        seq = self._paste_seq
        old_clipboard = None
        if self.restore_clipboard:
            if self._restore_pending:
                old_clipboard = self._saved_clipboard
            else:
                try:
                    old_clipboard = pyperclip.paste()
                except Exception:
                    log.debug("读取原剪贴板失败，粘贴后不恢复", exc_info=True)
                self._saved_clipboard = old_clipboard
            self._restore_pending = True

        report = callback

        def callback(result: PasteResult) -> None:
            restoring = result.status == "sent" and self.restore_clipboard and old_clipboard
            if not restoring and seq == self._paste_seq:
                self._restore_pending = False
            report(result)

        try:
            pyperclip.copy(text)
        except Exception as e:
            log.error("写入剪贴板失败: %s", e)
            callback(PasteResult("error", target_app=target_app, detail=str(e)))
            return

        if not has_target:
            callback(PasteResult("clipboard"))
            return

        if target_rejects_synthetic_input(info):
            log.info("目标窗口以更高权限运行，系统会拦截模拟粘贴；已复制到剪贴板")
            callback(PasteResult("clipboard", target_app=target_app, detail="elevated"))
            return

        def _restore_clipboard():
            if seq != self._paste_seq:
                return  # a later paste restores the same original
            self._restore_pending = False
            try:
                if pyperclip.paste() == text:
                    pyperclip.copy(old_clipboard)
            except Exception:
                log.debug("恢复原剪贴板失败", exc_info=True)

        def _keep_for_manual_paste(detail: str = ""):
            try:
                pyperclip.copy(text)
            except Exception:
                log.debug("重新写入剪贴板失败", exc_info=True)
            callback(PasteResult("clipboard", target_app=target_app, detail=detail))

        def _do_paste():
            # Keystrokes cannot be recalled once sent, so a focus change during
            # the delay must stop the shortcut rather than be reported afterwards.
            if not _verify_paste_target(hwnd):
                log.info("粘贴前焦点已切换到其他窗口，未发送粘贴键；已保留剪贴板内容")
                _keep_for_manual_paste("focus_changed")
                return
            try:
                _paste_shortcut()
            except Exception as e:
                log.warning("模拟粘贴失败: %s", e)
                callback(PasteResult("clipboard", target_app=target_app))
                return
            QTimer.singleShot(VERIFY_AFTER_PASTE_MS, _verify)

        def _verify():
            if _verify_paste_target(hwnd):
                # pyperclip only reads text; an empty read may be an image we
                # cannot put back, so leave the transcript rather than wipe it.
                if self.restore_clipboard and old_clipboard:
                    QTimer.singleShot(RESTORE_CLIPBOARD_DELAY_MS, _restore_clipboard)
                callback(PasteResult("sent", target_app=target_app))
            else:
                # The shortcut is already out; the target may well have taken
                # it before the focus moved. Keep the words on the clipboard
                # but do not call it 「已复制」, or the user pastes them twice.
                log.info("粘贴键已发出，但随后焦点切换，无法确认是否已插入；保留剪贴板内容")
                try:
                    pyperclip.copy(text)
                except Exception:
                    log.debug("重新写入剪贴板失败", exc_info=True)
                callback(PasteResult("unverified", target_app=target_app, detail="focus_changed_after_send"))

        QTimer.singleShot(PASTE_DELAY_MS, _do_paste)

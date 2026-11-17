"""Windows native window, synthetic keystroke, and side-effect-free UIA selection primitives."""

from __future__ import annotations

import ctypes
import logging
import sys
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from sayink.platform import SYNTHETIC_KEY_TAG

log = logging.getLogger("SayInk")

TERMINAL_CTRL_SHIFT_V_EXES: frozenset[str] = frozenset(
    {
        "windowsterminal.exe",
        "wt.exe",
        "wezterm-gui.exe",
        "alacritty.exe",
        "kitty.exe",
        "hyper.exe",
        "tabby.exe",
        "rio.exe",
    }
)
TERMINAL_SHIFT_INSERT_EXES: frozenset[str] = frozenset(
    {
        "mintty.exe",
        "putty.exe",
    }
)
CONSOLE_HOST_EXES: frozenset[str] = frozenset(
    {
        "cmd.exe",
        "powershell.exe",
        "pwsh.exe",
        "conhost.exe",
        "wsl.exe",
        "bash.exe",
    }
)
ALL_TERMINAL_EXES: frozenset[str] = (
    TERMINAL_CTRL_SHIFT_V_EXES | TERMINAL_SHIFT_INSERT_EXES | CONSOLE_HOST_EXES
)

_VK_LSHIFT, _VK_RSHIFT = 0xA0, 0xA1
_VK_LCONTROL, _VK_RCONTROL = 0xA2, 0xA3
_VK_LMENU, _VK_RMENU = 0xA4, 0xA5
_VK_LWIN, _VK_RWIN = 0x5B, 0x5C
_VK_INSERT = 0x2D
_SHIFT_VKS = (_VK_LSHIFT, _VK_RSHIFT)
_CTRL_VKS = (0x11, _VK_LCONTROL, _VK_RCONTROL)
_EXTENDED_VKS = frozenset((_VK_RCONTROL, _VK_RMENU, _VK_LWIN, _VK_RWIN, _VK_INSERT))
_KEYEVENTF_EXTENDEDKEY = 0x0001
_VK_MENU_MASK = 0xE8
_STRAY_MODIFIER_VKS = (_VK_LSHIFT, _VK_RSHIFT, _VK_LMENU, _VK_RMENU, _VK_LWIN, _VK_RWIN)


def is_terminal_process(process_name: str) -> bool:
    """True when ``process_name`` is a terminal/shell where simulated Ctrl+C sends SIGINT."""
    exe = (process_name or "").strip().replace("/", "\\").rsplit("\\", 1)[-1].casefold()
    return exe in ALL_TERMINAL_EXES


@runtime_checkable
class WindowInputBackend(Protocol):
    """Platform boundary for foreground window inspection, synthetic paste, and selection reading."""

    def get_foreground_window_info(self) -> tuple[int, str, int]:
        """Return ``(hwnd, title, pid)`` of the active foreground window."""
        ...

    def foreground_target(self) -> tuple[int, str]:
        """Return ``(hwnd, process_name)`` and snapshot target focus for later paste verification."""
        ...

    def send_paste_shortcut(self, mode: str = "ctrl_v") -> None:
        """Synthesize the requested paste key chord (``ctrl_v``, ``ctrl_shift_v``, ``shift_insert``)."""
        ...

    def read_selected_text(self, process_name: str = "") -> str | None:
        """Read currently selected text without mutating clipboard or sending Ctrl+C to terminals."""
        ...


def get_foreground_window_win32() -> tuple[int, str, int]:
    """Query ``(hwnd, title, pid)`` via Win32 ``GetForegroundWindow``."""
    try:
        import win32gui
        import win32process

        hwnd = int(win32gui.GetForegroundWindow() or 0)
        title = str(win32gui.GetWindowText(hwnd) or "")
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return hwnd, title, int(pid or 0)
    except Exception:
        log.debug("读取 Win32 前台窗口失败", exc_info=True)
        return 0, "", 0


def _configure_user32_keybd(user32: object) -> None:
    from ctypes import wintypes

    specs = (
        ("MapVirtualKeyW", [wintypes.UINT, wintypes.UINT], wintypes.UINT),
        ("GetAsyncKeyState", [ctypes.c_int], wintypes.SHORT),
        ("keybd_event", [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t], None),
    )
    for fn_name, argtypes, restype in specs:
        fn = getattr(user32, fn_name, None)
        if fn is not None and hasattr(fn, "argtypes"):
            fn.argtypes = argtypes
            fn.restype = restype


def _emit_paste_chord(user32: Any, tap_fn: Callable[[int, int], None], effective_mode: str, ctrl_held: bool) -> None:
    vk_control, vk_shift, vk_v, key_up = 0x11, 0x10, 0x56, 0x0002
    get_state = user32.GetAsyncKeyState
    if effective_mode == "shift_insert":
        held_ctrls = (
            [cvk for cvk in (_VK_LCONTROL, _VK_RCONTROL) if get_state(cvk) & 0x8000]
            if ctrl_held
            else []
        )
        for cvk in held_ctrls:
            tap_fn(cvk, key_up)
        tap_fn(vk_shift, 0)
        tap_fn(_VK_INSERT, 0)
        tap_fn(_VK_INSERT, key_up)
        tap_fn(vk_shift, key_up)
        for cvk in held_ctrls:
            tap_fn(cvk, 0)
        return

    if not ctrl_held:
        tap_fn(vk_control, 0)
    if effective_mode == "ctrl_shift_v":
        tap_fn(vk_shift, 0)
    tap_fn(vk_v, 0)
    tap_fn(vk_v, key_up)
    if effective_mode == "ctrl_shift_v":
        tap_fn(vk_shift, key_up)
    if not ctrl_held:
        tap_fn(vk_control, key_up)


def held_stray_modifiers(user32: Any) -> list[int]:
    return [vk for vk in _STRAY_MODIFIER_VKS if user32.GetAsyncKeyState(vk) & 0x8000]


def is_ctrl_physically_held(user32: Any) -> bool:
    return any(bool(user32.GetAsyncKeyState(vk) & 0x8000) for vk in _CTRL_VKS)


def send_paste_shortcut_win32(
    mode: str | None = None,
    *,
    held_modifiers_fn: Callable[[Any], list[int]] = held_stray_modifiers,
    ctrl_held_fn: Callable[[Any], bool] = is_ctrl_physically_held,
) -> None:
    """Send paste keystroke chord with Win32 ``keybd_event``, masking Alt/Win menu activation."""
    effective_mode = mode or "ctrl_v"
    user32 = ctypes.windll.user32
    _configure_user32_keybd(user32)
    key_up = 0x0002

    def _tap(vk: int, flags: int) -> None:
        scan = user32.MapVirtualKeyW(vk, 0)
        if vk in _EXTENDED_VKS:
            flags |= _KEYEVENTF_EXTENDEDKEY
        user32.keybd_event(vk, scan, flags, SYNTHETIC_KEY_TAG)

    held = held_modifiers_fn(user32)
    ctrl_held = ctrl_held_fn(user32)
    for vk in held:
        _tap(vk, key_up)

    _emit_paste_chord(user32, _tap, effective_mode, ctrl_held)

    for vk in held:
        _tap(vk, 0)
    if any(vk not in _SHIFT_VKS for vk in held):
        _tap(_VK_MENU_MASK, 0)
        _tap(_VK_MENU_MASK, key_up)


def read_selected_text_uia() -> str | None:
    """Side-effect-free Windows UI Automation ``TextPattern.GetSelection`` query.

    Never touches the clipboard and never synthesizes keystrokes. Returns ``None``
    when the focused control does not expose UIA ``TextPattern`` or has an empty selection.
    """
    if sys.platform != "win32":
        return None
    com_inited = False
    try:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        com_inited = True
        uia = win32com.client.Dispatch("UIAutomationCore.CUIAutomation")
        focused = uia.GetFocusedElement()
        if focused is None:
            return None
        # UIA_TextPatternId == 10014
        pattern = focused.GetCurrentPattern(10014)
        if pattern is None:
            return None
        ranges = pattern.GetSelection()
        if ranges is None or ranges.Length <= 0:
            return None
        parts: list[str] = []
        for idx in range(int(ranges.Length)):
            rng = ranges.GetElement(idx)
            if rng is not None:
                chunk = rng.GetText(32768)
                if isinstance(chunk, str) and chunk:
                    parts.append(chunk)
        selected = "".join(parts)
        return selected if selected.strip() else None
    except Exception:
        log.debug("UI Automation 读取选区不可用或无选区", exc_info=True)
        return None
    finally:
        if com_inited:
            try:
                import pythoncom

                pythoncom.CoUninitialize()
            except Exception:
                log.debug("释放 UIA COM 上下文失败", exc_info=True)


class Win32WindowInputBackend:
    """Production Windows 10/11 window & input backend."""

    def get_foreground_window_info(self) -> tuple[int, str, int]:
        return get_foreground_window_win32()

    def foreground_target(self) -> tuple[int, str]:
        from sayink.text_paster import TextPaster

        hwnd, app_name = TextPaster.capture_target_window()
        return int(hwnd or 0), str(app_name or "")

    def send_paste_shortcut(self, mode: str = "ctrl_v") -> None:
        send_paste_shortcut_win32(mode)

    def read_selected_text(self, process_name: str = "") -> str | None:
        if is_terminal_process(process_name):
            # Terminals often expose the entire scrollback buffer or behave unpredictably;
            # still safe via pure UIA, but we never fall back to synthetic Ctrl+C.
            return read_selected_text_uia()
        return read_selected_text_uia()


class HeadlessWindowInputBackend:
    """Deterministic no-op backend for headless offscreen tests off Windows."""

    def get_foreground_window_info(self) -> tuple[int, str, int]:
        return 0, "", 0

    def foreground_target(self) -> tuple[int, str]:
        return 0, ""

    def send_paste_shortcut(self, mode: str = "ctrl_v") -> None:
        raise OSError(f"当前非 Windows 无头环境不支持原生按键粘贴 ({mode})")

    def read_selected_text(self, process_name: str = "") -> str | None:
        return None


def default_window_input_backend() -> WindowInputBackend:
    if sys.platform == "win32":
        return Win32WindowInputBackend()
    return HeadlessWindowInputBackend()

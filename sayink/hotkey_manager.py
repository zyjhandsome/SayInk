import logging
import sys
import threading
import time

from PyQt6.QtCore import QObject, pyqtSignal, QTimer
from pynput import keyboard

log = logging.getLogger("SayInk")

# 短按防误触（毫秒）；计时器必须在 Qt 主线程启动，否则 Windows 上可能永不触发
MIN_HOLD_MS = 180  # 按住说话
MIN_HOLD_CONTINUOUS_MS = 300  # 持续转写：误触代价更高，门槛更长
# 松开早于该时长视为输入法/系统误触（如 Ctrl+Space 切换输入法），不提示用户
MIN_SHORT_TAP_MS = 50

KEY_MAP = {
    "alt": keyboard.Key.alt_l,
    "alt_l": keyboard.Key.alt_l,
    "alt_r": keyboard.Key.alt_r,
    "ctrl": keyboard.Key.ctrl_l,
    "ctrl_l": keyboard.Key.ctrl_l,
    "ctrl_r": keyboard.Key.ctrl_r,
    "shift": keyboard.Key.shift_l,
    "shift_l": keyboard.Key.shift_l,
    "shift_r": keyboard.Key.shift_r,
    "space": keyboard.Key.space,
    "tab": keyboard.Key.tab,
    "enter": keyboard.Key.enter,
    "esc": keyboard.Key.esc,
    "win": keyboard.Key.cmd,
    "cmd": keyboard.Key.cmd,
}
# The settings capture box offers F1–F12; a name parse_hotkey does not know
# would silently drop the main key and leave a modifier-only hotkey behind.
KEY_MAP.update({f"f{n}": getattr(keyboard.Key, f"f{n}") for n in range(1, 13)})


_WM_KEYDOWN = 0x0100
_WM_KEYUP = 0x0101
_WM_SYSKEYDOWN = 0x0104
_WM_SYSKEYUP = 0x0105
_LLKHF_INJECTED = 0x10
_KEYEVENTF_KEYUP = 0x0002
# Unassigned VK used by AutoHotkey as a "menu mask": an event between Alt/Win
# down and up stops Windows from opening the menu bar / Start on release.
_VK_MENU_MASK = 0xE8

# Async-state VKs that satisfy each canonical modifier of a hotkey.
_MODIFIER_VKS = {
    keyboard.Key.alt_l: (0x12,),
    keyboard.Key.ctrl_l: (0x11,),
    keyboard.Key.shift_l: (0x10,),
    keyboard.Key.cmd: (0x5B, 0x5C),
}
_MASKED_MODIFIERS = (keyboard.Key.alt_l, keyboard.Key.cmd)


def _main_key_vk(key) -> int | None:
    if isinstance(key, keyboard.Key):
        return getattr(key.value, "vk", None)
    vk = getattr(key, "vk", None)
    if vk:
        return int(vk)
    char = getattr(key, "char", None)
    if not char or sys.platform != "win32":
        return None
    import ctypes

    vk_key_scan = ctypes.windll.user32.VkKeyScanW
    vk_key_scan.argtypes = [ctypes.c_wchar]
    vk_key_scan.restype = ctypes.c_short
    scan = vk_key_scan(char[0])
    if scan == -1:
        return None
    return scan & 0xFF


def parse_hotkey(hotkey_str: str) -> set:
    """Parse a hotkey string like 'alt+space' into a set of pynput keys.

    Modifiers map to a single canonical key (left variant); _normalize_key
    maps left/right to the same key so either physical key satisfies the combo.
    """
    keys = set()
    for part in hotkey_str.lower().split("+"):
        part = part.strip()
        if part == "ctrl":
            keys.add(KEY_MAP["ctrl_l"])
        elif part == "alt":
            keys.add(KEY_MAP["alt_l"])
        elif part == "shift":
            keys.add(KEY_MAP["shift_l"])
        elif part in KEY_MAP:
            keys.add(KEY_MAP[part])
        elif len(part) == 1:
            try:
                keys.add(keyboard.KeyCode.from_char(part))
            except Exception:
                pass
    return keys


class HotKeyManager(QObject):
    recording_start = pyqtSignal()
    recording_stop = pyqtSignal()
    recording_cancel = pyqtSignal()
    continuous_listen_start = pyqtSignal()
    esc_pressed = pyqtSignal()
    hotkey_tap_too_short = pyqtSignal()
    listener_status = pyqtSignal(bool, str)
    # 从 pynput 线程投递到 Qt 主线程，再启动 QTimer
    _arm_hold_on_main = pyqtSignal()

    def __init__(self, hotkey_str: str = "alt+x", parent=None):
        super().__init__(parent)
        self._hotkey_keys = parse_hotkey(hotkey_str)
        self._hotkey_str = hotkey_str
        self._pressed_keys = set()
        self._is_recording = False
        self._continuous_trigger_mode = False
        self._paused = False
        self._listener = None
        self._lock = threading.Lock()
        self._hold_timer = QTimer(self)
        self._hold_timer.setSingleShot(True)
        self._hold_timer.timeout.connect(self._on_hold_timeout)
        self._hold_pending = False
        self._hold_activated = False
        self._hold_started_at = 0.0
        self._suppressed_vks: set[int] = set()
        self._main_keys_by_vk: dict[int, object] = {}
        self._hotkey_modifiers: tuple = ()
        self._rebuild_suppress_plan()
        self._arm_hold_on_main.connect(self._start_hold_timer_on_main_thread)

    def _rebuild_suppress_plan(self) -> None:
        """Caller holds ``self._lock`` or is still in __init__."""
        modifiers = tuple(k for k in self._hotkey_keys if k in _MODIFIER_VKS)
        mains: dict[int, object] = {}
        if modifiers:
            for key in self._hotkey_keys:
                if key in _MODIFIER_VKS:
                    continue
                vk = _main_key_vk(key)
                if vk:
                    mains[vk] = key
        self._hotkey_modifiers = modifiers
        self._main_keys_by_vk = mains
        self._suppressed_vks = set()

    @staticmethod
    def _async_key_down(vk: int) -> bool:
        import ctypes

        return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)

    def _modifiers_held(self, modifiers: tuple) -> bool:
        return all(
            any(self._async_key_down(vk) for vk in _MODIFIER_VKS[mod])
            for mod in modifiers
        )

    @staticmethod
    def _send_menu_mask() -> None:
        import ctypes

        user32 = ctypes.windll.user32
        user32.keybd_event(_VK_MENU_MASK, 0, 0, 0)
        user32.keybd_event(_VK_MENU_MASK, 0, 0x0002, 0)

    @staticmethod
    def _tap_is_typing(modifiers: tuple) -> bool:
        """Shift+letter tapped briefly is the user typing a capital letter.

        Alt / Win / Ctrl combos are commands, not text: a bare Alt+Z tap types
        nothing (and replaying Alt would pop the menu bar), so those keep the
        「按住时间过短」 hint instead.
        """
        return bool(modifiers) and all(mod == keyboard.Key.shift_l for mod in modifiers)

    def _taps_type_text(self) -> bool:
        """True when the Win32 filter replays short taps of this hotkey as typing.

        Then no part of the combo should raise the 「录音过短」 hint, whichever
        key the user lets go of first.
        """
        return bool(self._main_keys_by_vk) and self._tap_is_typing(self._hotkey_modifiers)

    def _replay_swallowed_tap(self, vk: int) -> None:
        """Type the key a too-short tap swallowed.

        With a Shift+letter hotkey such as Shift+X the filter has to eat X
        while Shift is held or a recording would type XXXX into the focused
        app, but then a
        normal capital X typed by the user would vanish too. When the hold
        never reached the threshold, re-send the keystroke as an injected
        event (which the filter lets through).
        """
        try:
            # Shift normally is still physically down when the letter goes up;
            # if the user already let go, hold it for the replay so the case
            # matches what they typed.
            shift_vk = _MODIFIER_VKS[keyboard.Key.shift_l][0]
            press_shift = not self._async_key_down(shift_vk)
            if press_shift:
                self._keybd_event(shift_vk, 0)
            self._keybd_event(vk, 0)
            self._keybd_event(vk, _KEYEVENTF_KEYUP)
            if press_shift:
                self._keybd_event(shift_vk, _KEYEVENTF_KEYUP)
        except Exception:
            log.exception("补发被吞掉的按键失败")

    @staticmethod
    def _keybd_event(vk: int, flags: int) -> None:
        import ctypes

        ctypes.windll.user32.keybd_event(vk, 0, flags, 0)

    def _win32_event_filter(self, msg, data):
        """Keep the hotkey's main key (and its auto-repeat) out of the focused app.

        Suppressing an event also skips pynput's own on_press/on_release, so
        this path feeds the hotkey state machine directly.
        """
        try:
            if data.flags & _LLKHF_INJECTED:
                return True
            vk = int(data.vkCode)
            with self._lock:
                if self._paused:
                    return True
                main_key = self._main_keys_by_vk.get(vk)
                modifiers = self._hotkey_modifiers
                already = vk in self._suppressed_vks
            if main_key is None:
                return True
            if msg in (_WM_KEYDOWN, _WM_SYSKEYDOWN):
                held = self._modifiers_held(modifiers)
                if not already and not held:
                    return True
                with self._lock:
                    self._suppressed_vks.add(vk)
                # Auto-repeat of the main key keeps arriving after the user
                # let go of the modifier; feeding the modifier then would
                # re-arm a hold the user is not making.
                if held:
                    for mod in modifiers:
                        self._on_press(mod)
                self._on_press(main_key)
                if not already and any(m in _MASKED_MODIFIERS for m in modifiers):
                    self._send_menu_mask()
            elif msg in (_WM_KEYUP, _WM_SYSKEYUP):
                if not already:
                    return True
                with self._lock:
                    self._suppressed_vks.discard(vk)
                # A tap that gets typed into the app is plain typing, not a
                # failed hold: no 「录音过短」 hint for it.
                replayable = self._tap_is_typing(modifiers)
                self._on_release(main_key, quiet=replayable)
                # The hold may already have been cut short by letting go of
                # Shift first; the swallowed letter still has to be typed.
                if replayable and not self._hold_activated:
                    self._replay_swallowed_tap(vk)
            else:
                return True
        except Exception:
            log.exception("快捷键过滤失败，按键照常传给前台应用")
            return True
        listener = self._listener
        if listener is not None:
            listener.suppress_event()
        return True

    def start(self):
        if self._listener is not None:
            return
        kwargs = {}
        if sys.platform == "win32":
            kwargs["win32_event_filter"] = self._win32_event_filter
        # pynput 1.8 counts keyword-only parameters when deciding whether to
        # pass ``injected``, and stops the listener when a callback returns
        # False; ``_on_release`` has both, so pynput only sees these wrappers.
        def on_press(key):
            self._on_press(key)

        def on_release(key):
            self._on_release(key)

        self._listener = keyboard.Listener(
            on_press=on_press,
            on_release=on_release,
            **kwargs,
        )
        self._listener.daemon = True
        try:
            self._listener.start()
        except Exception as e:
            log.error("全局快捷键监听启动失败: %s", e)
            self._listener = None
            self.listener_status.emit(False, f"快捷键监听启动失败: {e}")
            return
        if self._listener.is_alive():
            log.info("全局快捷键监听已启动: %s", self._hotkey_str)
            self.listener_status.emit(True, "")
        else:
            log.error("全局快捷键监听未运行: %s", self._hotkey_str)
            self._listener = None
            self.listener_status.emit(
                False,
                "快捷键监听未能启动，请在设置中更换快捷键后重试",
            )

    def stop(self):
        self._cancel_hold_pending()
        if self._listener:
            self._listener.stop()
            try:
                self._listener.join(timeout=2.0)
            except Exception:
                pass
            self._listener = None
        with self._lock:
            self._pressed_keys.clear()

    def pause(self):
        self._cancel_hold_pending()
        with self._lock:
            self._paused = True
            self._pressed_keys.clear()
            self._suppressed_vks.clear()

    def resume(self):
        self._cancel_hold_pending()
        with self._lock:
            self._paused = False
            self._pressed_keys.clear()
            self._suppressed_vks.clear()

    def update_hotkey(self, hotkey_str: str):
        self._cancel_hold_pending()
        with self._lock:
            self._hotkey_str = hotkey_str
            self._hotkey_keys = parse_hotkey(hotkey_str)
            self._rebuild_suppress_plan()
            self._pressed_keys.clear()
        log.info("快捷键已更新: %s", hotkey_str)

    def set_continuous_trigger_mode(self, enabled: bool) -> None:
        """When True, hold hotkey starts continuous listening (release does not stop)."""
        self._cancel_hold_pending()
        with self._lock:
            self._continuous_trigger_mode = enabled
            self._pressed_keys.clear()
            self._is_recording = False

    @property
    def hold_threshold_ms(self) -> int:
        if self._continuous_trigger_mode:
            return MIN_HOLD_CONTINUOUS_MS
        return MIN_HOLD_MS

    def _cancel_hold_pending(self):
        self._hold_timer.stop()
        self._hold_pending = False

    def _hotkey_still_held(self) -> bool:
        with self._lock:
            return self._hotkey_still_held_locked()

    def _hotkey_still_held_locked(self) -> bool:
        """Caller must hold ``self._lock``."""
        return bool(
            self._hotkey_keys
            and self._hotkey_keys.issubset(self._pressed_keys)
        )

    def _on_hold_timeout(self):
        if not self._hold_pending or self._paused:
            self._hold_pending = False
            return
        if not self._hotkey_still_held():
            self._hold_pending = False
            return

        if self._continuous_trigger_mode:
            self._hold_pending = False
            self._hold_activated = True
            log.debug("快捷键按住达标，请求开启持续监听")
            self.continuous_listen_start.emit()
            return

        with self._lock:
            if self._is_recording:
                self._hold_pending = False
                return
            self._is_recording = True
        self._hold_pending = False
        self._hold_activated = True
        log.debug("快捷键按住达标，开始录音")
        self.recording_start.emit()

    def _normalize_key(self, key):
        # A hotkey written with the generic modifier ("shift+x") accepts either
        # physical key; only an explicit right variant ("shift_r+x") keeps the
        # left and right keys apart.
        if key in (keyboard.Key.alt_l, keyboard.Key.alt_r, keyboard.Key.alt_gr):
            if keyboard.Key.alt_r in self._hotkey_keys:
                return key
            return keyboard.Key.alt_l
        if key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
            if keyboard.Key.ctrl_r in self._hotkey_keys:
                return key
            return keyboard.Key.ctrl_l
        if key in (keyboard.Key.shift_l, keyboard.Key.shift_r):
            if keyboard.Key.shift_r in self._hotkey_keys:
                return key
            return keyboard.Key.shift_l
        return key

    @staticmethod
    def _is_menu_mask(key) -> bool:
        return getattr(key, "vk", None) == _VK_MENU_MASK

    def _on_press(self, key):
        if self._is_menu_mask(key):
            return
        normalized = self._normalize_key(key)
        newly_armed = False

        with self._lock:
            if self._paused:
                return
            # Auto-repeat re-sends keys that are already down; only the press
            # that completes the combo starts a hold. Re-arming on repeats
            # would clear ``_hold_activated`` after continuous listening began
            # and make the release replay the swallowed key as typed text.
            was_complete = self._hotkey_still_held_locked()
            self._pressed_keys.add(normalized)

            if key == keyboard.Key.esc:
                self.esc_pressed.emit()
                if self._is_recording:
                    self._is_recording = False
                    self.recording_cancel.emit()
                return

            if (
                not was_complete
                and not self._is_recording
                and not self._hold_pending
                and self._hotkey_still_held_locked()
            ):
                self._hold_pending = True
                self._hold_activated = False
                self._hold_started_at = time.monotonic()
                newly_armed = True
        # Auto-repeat keeps calling here; restarting the timer on every repeat
        # could postpone the hold threshold forever.
        if newly_armed:
            self._arm_hold_on_main.emit()

    def _on_release(self, key, *, quiet: bool = False) -> bool:
        """Returns True when this release ended a hold before it activated,
        i.e. the combo was tapped rather than held. ``quiet`` skips the
        ``hotkey_tap_too_short`` hint for such a tap."""
        if self._is_menu_mask(key):
            return False
        normalized = self._normalize_key(key)
        emit_stop = False
        sync_hold_timer = False
        emit_short_tap = False
        tapped = False

        with self._lock:
            if self._paused:
                return False
            self._pressed_keys.discard(normalized)
            self._pressed_keys.discard(key)
            # Only letting go of part of the hotkey ends a pending hold.
            releases_hotkey = normalized in self._hotkey_keys or key in self._hotkey_keys

            if self._hold_pending and releases_hotkey:
                self._hold_pending = False
                sync_hold_timer = True
                if not self._hold_activated:
                    tapped = True
                    held_ms = (time.monotonic() - self._hold_started_at) * 1000
                    quiet = quiet or self._taps_type_text()
                    if held_ms >= MIN_SHORT_TAP_MS and not quiet:
                        emit_short_tap = True
                    else:
                        log.debug(
                            "忽略极短快捷键组合 (%.0f ms)，可能为输入法占用",
                            held_ms,
                        )

            if self._is_recording and not self._hotkey_still_held_locked():
                self._is_recording = False
                emit_stop = True

        if emit_short_tap:
            self.hotkey_tap_too_short.emit()
        if sync_hold_timer:
            self._arm_hold_on_main.emit()
        if emit_stop:
            log.debug("快捷键松开，停止录音")
            self.recording_stop.emit()
        return tapped

    def _start_hold_timer_on_main_thread(self):
        """QTimer 只能在 Qt 主线程 start/stop。"""
        if self._hold_pending:
            self._hold_timer.start(self.hold_threshold_ms)
        else:
            self._hold_timer.stop()

    @property
    def hotkey_str(self) -> str:
        return self._hotkey_str

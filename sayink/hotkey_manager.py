import logging
import sys
import threading
import time
import traceback

from PyQt6.QtCore import QObject, Qt, pyqtSignal, QTimer
from pynput import keyboard

from sayink.platform import SYNTHETIC_KEY_TAG

log = logging.getLogger("SayInk")

# 短按防误触（毫秒）；计时器必须在 Qt 主线程启动，否则 Windows 上可能永不触发
MIN_HOLD_MS = 180  # 单次转写
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
# The settings capture box offers function keys; a name parse_hotkey does not know
# would silently drop the main key and leave a modifier-only hotkey behind.
KEY_MAP.update(
    {f"f{n}": getattr(keyboard.Key, f"f{n}") for n in range(1, 25) if hasattr(keyboard.Key, f"f{n}")}
)

_ASCII_OEM_VK_FALLBACK = {
    "`": 0xC0,
    "-": 0xBD,
    "=": 0xBB,
    "[": 0xDB,
    "]": 0xDD,
    "\\": 0xDC,
    ";": 0xBA,
    "'": 0xDE,
    ",": 0xBC,
    ".": 0xBE,
    "/": 0xBF,
}
_HOLD_WATCH_POLL_MS = 100
_HEALTH_CHECK_INTERVAL_MS = 5000
_SLEEP_WAKE_GAP_S = 20.0


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
    keyboard.Key.alt_r: (0xA5,),
    keyboard.Key.ctrl_l: (0x11,),
    keyboard.Key.ctrl_r: (0xA3,),
    keyboard.Key.shift_l: (0x10,),
    keyboard.Key.shift_r: (0xA1,),
    keyboard.Key.cmd: (0x5B, 0x5C),
}
_MASKED_MODIFIERS = (keyboard.Key.alt_l, keyboard.Key.alt_r, keyboard.Key.cmd)
_ALL_MODIFIER_VKS = frozenset(vk for vks in _MODIFIER_VKS.values() for vk in vks)
# Hardware typematic repeat intervals are <= 0.5 s after <= 1.0 s delay. Any gap
# beyond this without modifiers held means WM_KEYUP was lost (e.g. Win+L / UAC).
_SUPPRESSED_REPEAT_MAX_GAP_S = 1.2


def _main_key_vk(key) -> int | None:
    if isinstance(key, keyboard.Key):
        return getattr(key.value, "vk", None)
    vk = getattr(key, "vk", None)
    if vk:
        return int(vk)
    char = getattr(key, "char", None)
    if not char:
        return None
    ch = char[0]
    if "a" <= ch <= "z" or "A" <= ch <= "Z":
        return ord(ch.upper())
    if "0" <= ch <= "9":
        return ord(ch)
    if sys.platform != "win32":
        return _ASCII_OEM_VK_FALLBACK.get(ch)
    try:
        import ctypes

        vk_key_scan = ctypes.windll.user32.VkKeyScanW
        vk_key_scan.argtypes = [ctypes.c_wchar]
        vk_key_scan.restype = ctypes.c_short
        scan = vk_key_scan(ch)
        if scan != -1:
            return scan & 0xFF
    except Exception:
        pass
    return _ASCII_OEM_VK_FALLBACK.get(ch)


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
    # 将钩子线程日志异步投递到主线程，避免 RotatingFileHandler 同步磁盘 I/O 触发 LowLevelHooksTimeout
    _hook_log = pyqtSignal(int, str)

    def __init__(self, hotkey_str: str = "alt+x", parent=None):
        super().__init__(parent)
        self._hotkey_keys = parse_hotkey(hotkey_str)
        self._hotkey_str = hotkey_str
        self._pressed_keys = set()
        self._is_recording = False
        self._continuous_trigger_mode = False
        self._paused = False
        self._stopped = False
        self._listener = None
        self._verify_async_state: bool | None = None
        self._lock = threading.Lock()
        self._hold_timer = QTimer(self)
        self._hold_timer.setSingleShot(True)
        self._hold_timer.timeout.connect(self._on_hold_timeout)
        self._hold_watch_timer = QTimer(self)
        self._hold_watch_timer.setInterval(_HOLD_WATCH_POLL_MS)
        self._hold_watch_timer.timeout.connect(self._poll_recording_keys_held)
        self._health_timer = QTimer(self)
        self._health_timer.setInterval(_HEALTH_CHECK_INTERVAL_MS)
        self._health_timer.timeout.connect(self._on_health_tick)
        self._last_health_wall = time.time()
        self._last_health_mono = time.monotonic()
        self._hold_pending = False
        self._hold_activated = False
        self._hold_started_at = 0.0
        self._suppressed_vks: set[int] = set()
        self._suppressed_vk_at: dict[int, float] = {}
        self._suppressed_vk_async_visible: set[int] = set()
        self._main_keys_by_vk: dict[int, object] = {}
        self._hotkey_modifiers: tuple = ()
        self._rebuild_suppress_plan()
        self._arm_hold_on_main.connect(self._start_hold_timer_on_main_thread)
        self._hook_log.connect(self._emit_hook_log, Qt.ConnectionType.QueuedConnection)
        self.destroyed.connect(self._stop_listener_quietly)

    @staticmethod
    def _emit_hook_log(level: int, message: str) -> None:
        log.log(level, "%s", message)

    def _defer_hook_log(self, level: int, message: str, *args) -> None:
        if not log.isEnabledFor(level):
            return
        formatted = message % args if args else message
        self._hook_log.emit(level, formatted)

    def _rebuild_suppress_plan(self) -> None:
        """Caller holds ``self._lock`` or is still in __init__."""
        modifiers = tuple(k for k in self._hotkey_keys if k in _MODIFIER_VKS)
        mains: dict[int, object] = {}
        for key in self._hotkey_keys:
            if key in _MODIFIER_VKS:
                continue
            vk = _main_key_vk(key)
            if vk:
                mains[vk] = key
        self._hotkey_modifiers = modifiers
        self._main_keys_by_vk = mains
        self._suppressed_vks = set()
        self._suppressed_vk_at = {}
        self._suppressed_vk_async_visible = set()

    @staticmethod
    def _async_key_down(vk: int) -> bool:
        import ctypes

        return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)

    def _modifiers_held(self, modifiers: tuple) -> bool:
        return all(
            any(self._async_key_down(vk) for vk in _MODIFIER_VKS[mod])
            for mod in modifiers
        )

    def _should_reconcile_async_state(self) -> bool:
        if self._verify_async_state is not None:
            return bool(self._verify_async_state)
        return (
            sys.platform == "win32"
            and isinstance(self._listener, keyboard.Listener)
            and getattr(self._listener, "ident", None) is not None
        )

    def _prune_stale_modifiers_locked(self) -> None:
        """Remove modifiers whose WM_KEYUP was lost across Win+L / UAC."""
        for key in list(self._pressed_keys):
            if key in _MODIFIER_VKS and not self._modifiers_held((key,)):
                self._pressed_keys.discard(key)

    def _prune_stale_pressed_keys_locked(self, *, incoming_key=None) -> None:
        """Reconcile tracked pressed keys against OS async key state."""
        for key in list(self._pressed_keys):
            if key == incoming_key:
                continue
            if key in _MODIFIER_VKS:
                if not self._modifiers_held((key,)):
                    self._pressed_keys.discard(key)
                continue
            vk = _main_key_vk(key)
            if vk and vk not in self._suppressed_vks and not self._async_key_down(vk):
                self._pressed_keys.discard(key)

    @staticmethod
    def _send_menu_mask() -> None:
        import ctypes

        user32 = ctypes.windll.user32
        user32.keybd_event(_VK_MENU_MASK, 0, 0, SYNTHETIC_KEY_TAG)
        user32.keybd_event(_VK_MENU_MASK, 0, 0x0002, SYNTHETIC_KEY_TAG)

    @staticmethod
    def _tap_is_typing(modifiers: tuple) -> bool:
        """Shift+letter tapped briefly is the user typing a capital letter.

        Alt / Win / Ctrl combos are commands, not text: a bare Alt+Z tap types
        nothing (and replaying Alt would pop the menu bar), so those keep the
        「按住时间过短」 hint instead.
        """
        return bool(modifiers) and all(
            mod in (keyboard.Key.shift_l, keyboard.Key.shift_r) for mod in modifiers
        )

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
            self._defer_hook_log(
                logging.ERROR,
                "补发被吞掉的按键失败\n%s",
                traceback.format_exc(),
            )

    @staticmethod
    def _keybd_event(vk: int, flags: int) -> None:
        import ctypes

        ctypes.windll.user32.keybd_event(vk, 0, flags, SYNTHETIC_KEY_TAG)

    def _win32_event_filter(self, msg, data):
        """Keep the hotkey's main key (and its auto-repeat) out of the focused app.

        Suppressing an event also skips pynput's own on_press/on_release, so
        this path feeds the hotkey state machine directly.
        """
        try:
            if data.dwExtraInfo == SYNTHETIC_KEY_TAG:
                # Paste lifts and re-presses the held Alt; seen as the user's
                # keys, that would cancel the hold the user is making.
                return False
            if data.flags & _LLKHF_INJECTED:
                return True
            vk = int(data.vkCode)
            with self._lock:
                if self._paused:
                    return True
                main_key = self._main_keys_by_vk.get(vk)
                modifiers = self._hotkey_modifiers
                already = vk in self._suppressed_vks
                has_suppressed = bool(self._suppressed_vks)
            if main_key is None:
                if (
                    has_suppressed
                    and msg in (_WM_KEYDOWN, _WM_SYSKEYDOWN)
                    and vk not in _ALL_MODIFIER_VKS
                    and not self._modifiers_held(modifiers)
                ):
                    with self._lock:
                        self._suppressed_vks.clear()
                        self._suppressed_vk_at.clear()
                        self._prune_stale_modifiers_locked()
                return True
            if msg in (_WM_KEYDOWN, _WM_SYSKEYDOWN):
                now = time.monotonic()
                held = self._modifiers_held(modifiers)
                if not held:
                    with self._lock:
                        for mod in modifiers:
                            self._pressed_keys.discard(mod)
                        self._prune_stale_modifiers_locked()
                        if (
                            already
                            and now - self._suppressed_vk_at.get(vk, 0.0)
                            > _SUPPRESSED_REPEAT_MAX_GAP_S
                        ):
                            self._suppressed_vks.discard(vk)
                            self._suppressed_vk_at.pop(vk, None)
                            already = False
                if not already and not held:
                    return True
                with self._lock:
                    self._suppressed_vks.add(vk)
                    self._suppressed_vk_at[vk] = now
                    if self._async_key_down(vk):
                        self._suppressed_vk_async_visible.add(vk)
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
                    self._suppressed_vk_at.pop(vk, None)
                    self._suppressed_vk_async_visible.discard(vk)
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
            self._defer_hook_log(
                logging.ERROR,
                "快捷键过滤失败，按键照常传给前台应用\n%s",
                traceback.format_exc(),
            )
            return True
        listener = self._listener
        if listener is not None:
            listener.suppress_event()
        return True

    def _stop_listener_quietly(self, *_args) -> None:
        listener = getattr(self, "_listener", None)
        if listener is not None and isinstance(listener, keyboard.Listener):
            try:
                listener.stop()
            except Exception:
                pass
            self._listener = None

    def __del__(self):
        try:
            self._stop_listener_quietly()
        except Exception:
            pass

    def _start_health_timer_if_possible(self) -> None:
        from PyQt6.QtCore import QCoreApplication, QThread

        app = QCoreApplication.instance()
        if app is not None and QThread.currentThread() is self._health_timer.thread():
            self._last_health_wall = time.time()
            self._last_health_mono = time.monotonic()
            if not self._health_timer.isActive():
                self._health_timer.start(_HEALTH_CHECK_INTERVAL_MS)

    def _stop_timers_if_possible(self) -> None:
        from PyQt6.QtCore import QCoreApplication, QThread

        app = QCoreApplication.instance()
        if app is None or QThread.currentThread() is self._hold_watch_timer.thread():
            self._hold_watch_timer.stop()
            self._health_timer.stop()

    def _restart_listener(self) -> None:
        old = self._listener
        self._listener = None
        if old is not None:
            try:
                old.stop()
                old.join(timeout=1.0)
            except Exception:
                pass
        with self._lock:
            self._pressed_keys.clear()
            self._suppressed_vks.clear()
            self._suppressed_vk_at.clear()
            self._suppressed_vk_async_visible.clear()
        self.start()

    def _on_health_tick(self) -> None:
        if self._stopped:
            self._health_timer.stop()
            return
        now_wall = time.time()
        now_mono = time.monotonic()
        wall_delta = now_wall - self._last_health_wall
        self._last_health_wall = now_wall
        self._last_health_mono = now_mono

        woke_from_sleep = wall_delta >= _SLEEP_WAKE_GAP_S
        listener = self._listener
        is_alive_fn = getattr(listener, "is_alive", None) if listener is not None else None
        listener_dead = listener is None or (callable(is_alive_fn) and not is_alive_fn())

        if woke_from_sleep:
            log.info("检测到系统睡眠唤醒 (间隔 %.1fs)，正在自愈全局热键监听", wall_delta)
            self._poll_recording_keys_held()
            self._restart_listener()
        elif listener_dead:
            log.warning("检测到全局热键监听线程已停止，正在自动恢复")
            self._restart_listener()

    def _poll_recording_keys_held(self) -> None:
        """100ms GetAsyncKeyState fallback while recording in hold-to-talk mode.

        Ensures releasing the hotkey inside an elevated Administrator window
        (where Windows UIPI drops WM_KEYUP to our low-level hook) stops recording
        immediately instead of latching the microphone open.
        """
        if not self._should_reconcile_async_state():
            self._hold_watch_timer.stop()
            return
        emit_stop = False
        now = time.monotonic()
        with self._lock:
            if not self._is_recording or self._continuous_trigger_mode or self._paused:
                self._hold_watch_timer.stop()
                return
            released = False
            if self._hotkey_modifiers and not self._modifiers_held(self._hotkey_modifiers):
                released = True
            if not released:
                for key in self._hotkey_keys:
                    if key in _MODIFIER_VKS:
                        continue
                    vk = _main_key_vk(key)
                    if not vk:
                        continue
                    if vk not in self._suppressed_vks or vk in self._suppressed_vk_async_visible:
                        if not self._async_key_down(vk):
                            released = True
                            break
                    elif not self._hotkey_modifiers:
                        # Single-key suppressed hotkey (e.g. F8): hardware typematic
                        # repeat stops arriving once UIPI / lock screen eats events.
                        last_at = self._suppressed_vk_at.get(vk, now)
                        if (now - last_at) > _SUPPRESSED_REPEAT_MAX_GAP_S:
                            released = True
                            break
            if released:
                self._is_recording = False
                self._hold_pending = False
                self._pressed_keys.clear()
                self._suppressed_vks.clear()
                self._suppressed_vk_at.clear()
                self._suppressed_vk_async_visible.clear()
                emit_stop = True
        if emit_stop:
            self._hold_watch_timer.stop()
            log.debug("GetAsyncKeyState 轮询检测到热键已松开（可能位于提权窗口），立即停止录音")
            self.recording_stop.emit()

    def start(self):
        self._stopped = False
        if self._listener is not None:
            is_alive_fn = getattr(self._listener, "is_alive", None)
            if not callable(is_alive_fn) or is_alive_fn():
                return
            self._stop_listener_quietly()
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
            self._start_health_timer_if_possible()
            self.listener_status.emit(True, "")
        else:
            log.error("全局快捷键监听未运行: %s", self._hotkey_str)
            self._listener = None
            self.listener_status.emit(
                False,
                "快捷键监听未能启动，请在设置中更换快捷键后重试",
            )

    def stop(self):
        self._stopped = True
        self._cancel_hold_pending()
        self._stop_timers_if_possible()
        if self._listener:
            self._listener.stop()
            try:
                self._listener.join(timeout=2.0)
            except Exception:
                pass
            self._listener = None
        with self._lock:
            self._pressed_keys.clear()
            self._suppressed_vks.clear()
            self._suppressed_vk_at.clear()
            self._suppressed_vk_async_visible.clear()

    def pause(self):
        self._cancel_hold_pending()
        if self._hold_watch_timer.isActive():
            self._hold_watch_timer.stop()
        with self._lock:
            self._paused = True
            self._pressed_keys.clear()
            self._suppressed_vks.clear()
            self._suppressed_vk_at.clear()
            self._suppressed_vk_async_visible.clear()

    def resume(self):
        self._cancel_hold_pending()
        with self._lock:
            self._paused = False
            self._pressed_keys.clear()
            self._suppressed_vks.clear()
            self._suppressed_vk_at.clear()
            self._suppressed_vk_async_visible.clear()

    def update_hotkey(self, hotkey_str: str):
        self._cancel_hold_pending()
        with self._lock:
            self._hotkey_str = hotkey_str
            self._hotkey_keys = parse_hotkey(hotkey_str)
            self._rebuild_suppress_plan()
            self._pressed_keys.clear()
        log.info("快捷键已更新: %s", hotkey_str)
        listener = self._listener
        is_alive_fn = getattr(listener, "is_alive", None) if listener is not None else None
        if listener is None and not self._stopped:
            self.start()
        elif listener is not None and callable(is_alive_fn) and not is_alive_fn() and not self._stopped:
            self._restart_listener()

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
        # The "still held" check and the state flip happen under one lock:
        # a release slipping in between would see a pending, not-yet-active
        # hold (so it only reports a short tap, never a stop) while this
        # thread goes on to start a recording that nothing will stop.
        start_continuous = start_recording = False
        reconcile = self._should_reconcile_async_state()
        with self._lock:
            if reconcile:
                self._prune_stale_pressed_keys_locked()
            if not self._hold_pending or self._paused or not self._hotkey_still_held_locked():
                self._hold_pending = False
                return
            self._hold_pending = False
            self._hold_activated = True
            if self._continuous_trigger_mode:
                start_continuous = True
            elif not self._is_recording:
                self._is_recording = True
                start_recording = True
        if start_continuous:
            log.debug("快捷键按住达标，请求开启持续监听")
            self.continuous_listen_start.emit()
        elif start_recording:
            log.debug("快捷键按住达标，开始录音")
            if reconcile:
                self._hold_watch_timer.start(_HOLD_WATCH_POLL_MS)
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
        if isinstance(key, keyboard.KeyCode) and key not in self._hotkey_keys:
            vk = getattr(key, "vk", None)
            if vk is not None:
                mapped = self._main_keys_by_vk.get(int(vk))
                if mapped is not None:
                    return mapped
            char = getattr(key, "char", None)
            if char and len(char) == 1:
                lower_kc = keyboard.KeyCode.from_char(char.lower())
                if lower_kc in self._hotkey_keys:
                    return lower_kc
        return key

    @staticmethod
    def _is_menu_mask(key) -> bool:
        return getattr(key, "vk", None) == _VK_MENU_MASK

    def _on_press(self, key):
        if self._is_menu_mask(key):
            return
        normalized = self._normalize_key(key)
        newly_armed = False
        emit_stale_stop = False
        sync_hold_timer = False
        reconcile = self._should_reconcile_async_state()

        with self._lock:
            if self._paused:
                return
            if reconcile:
                self._prune_stale_pressed_keys_locked(incoming_key=normalized)
                if self._hold_pending and not self._hotkey_still_held_locked():
                    self._hold_pending = False
                    sync_hold_timer = True
                if self._is_recording and not self._hotkey_still_held_locked():
                    self._is_recording = False
                    emit_stale_stop = True
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
        if emit_stale_stop:
            self._defer_hook_log(logging.DEBUG, "检测到失焦/锁屏后修饰键已释放，结束残留录音状态")
            self.recording_stop.emit()
        # Auto-repeat keeps calling here; restarting the timer on every repeat
        # could postpone the hold threshold forever.
        if newly_armed or sync_hold_timer:
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
        short_tap_held_ms: float | None = None
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
                        short_tap_held_ms = held_ms

            if self._is_recording and not self._hotkey_still_held_locked():
                self._is_recording = False
                emit_stop = True

        if short_tap_held_ms is not None:
            self._defer_hook_log(
                logging.DEBUG,
                "忽略极短快捷键组合 (%.0f ms)，可能为输入法占用",
                short_tap_held_ms,
            )
        if emit_short_tap:
            self.hotkey_tap_too_short.emit()
        if sync_hold_timer:
            self._arm_hold_on_main.emit()
        if emit_stop:
            if self._hold_watch_timer.isActive():
                self._stop_timers_if_possible()
                self._start_health_timer_if_possible()
            self._defer_hook_log(logging.DEBUG, "快捷键松开，停止录音")
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

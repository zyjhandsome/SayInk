import pytest
from sayink.config import DEFAULT_HOTKEY
from sayink.hotkey_manager import parse_hotkey, HotKeyManager, KEY_MAP
from pynput import keyboard
import threading


class TestParseHotkey:
    def test_parse_ctrl_space(self):
        result = parse_hotkey("ctrl+space")
        assert keyboard.Key.ctrl_l in result
        assert keyboard.Key.space in result
        assert keyboard.Key.ctrl_r not in result

    def test_parse_ctrl_space_matches_single_physical_ctrl(self):
        """Hotkey set must not require both left and right modifier keys."""
        hotkey = parse_hotkey("ctrl+space")
        assert hotkey.issubset({keyboard.Key.ctrl_l, keyboard.Key.space})
        assert len(hotkey) == 2

    def test_parse_single_key(self):
        result = parse_hotkey("space")
        assert keyboard.Key.space in result

    def test_parse_with_uppercase(self):
        result = parse_hotkey("CTRL+SPACE")
        assert keyboard.Key.ctrl_l in result
        assert keyboard.Key.space in result

    def test_parse_with_spaces(self):
        result = parse_hotkey(" ctrl + space ")
        assert keyboard.Key.ctrl_l in result
        assert keyboard.Key.space in result

    def test_parse_alt_key(self):
        result = parse_hotkey("alt+space")
        assert keyboard.Key.alt_l in result

    def test_parse_shift_key(self):
        result = parse_hotkey("shift+space")
        assert keyboard.Key.shift_l in result

    def test_parse_three_keys(self):
        result = parse_hotkey("ctrl+shift+space")
        assert keyboard.Key.ctrl_l in result
        assert keyboard.Key.shift_l in result
        assert keyboard.Key.space in result

    def test_parse_character_key(self):
        result = parse_hotkey("a")
        char_key = keyboard.KeyCode.from_char("a")
        assert char_key in result

    def test_parse_mixed_modifier_and_char(self):
        result = parse_hotkey("ctrl+a")
        assert keyboard.Key.ctrl_l in result
        char_key = keyboard.KeyCode.from_char("a")
        assert char_key in result

    def test_parse_invalid_key_ignored(self):
        result = parse_hotkey("ctrl+invalidkey123")
        assert keyboard.Key.ctrl_l in result
        assert len(result) == 1

    @pytest.mark.parametrize("name", [f"f{n}" for n in range(1, 13)])
    def test_parse_function_keys_offered_by_the_capture_box(self, name):
        """Regression: Ctrl+F1 parsed to {ctrl} alone, so holding Ctrl by
        itself started a recording."""
        result = parse_hotkey(f"ctrl+{name}")
        assert result == {keyboard.Key.ctrl_l, getattr(keyboard.Key, name)}


class TestHotKeyManagerInit:
    def test_default_hotkey(self):
        mgr = HotKeyManager()
        assert mgr._hotkey_str == "alt+x"
        assert mgr._hotkey_keys is not None

    def test_custom_hotkey(self):
        mgr = HotKeyManager("alt+space")
        assert mgr._hotkey_str == "alt+space"

    def test_initial_state(self):
        mgr = HotKeyManager()
        assert mgr._is_recording is False
        assert mgr._paused is False
        assert mgr._pressed_keys == set()

    def test_hotkey_str_property(self):
        mgr = HotKeyManager("ctrl+b")
        assert mgr.hotkey_str == "ctrl+b"


class TestHotKeyManagerStartStop:
    def test_start_creates_listener(self):
        from PyQt6.QtWidgets import QApplication
        import sys

        app = QApplication.instance() or QApplication(sys.argv)
        mgr = HotKeyManager(parent=app)
        mgr.start()
        assert mgr._listener is not None
        mgr.stop()

    def test_double_start_ignored(self):
        from PyQt6.QtWidgets import QApplication
        import sys

        app = QApplication.instance() or QApplication(sys.argv)
        mgr = HotKeyManager(parent=app)
        mgr.start()
        first_listener = mgr._listener
        mgr.start()
        assert mgr._listener is first_listener
        mgr.stop()

    def test_stop_clears_keys(self):
        mgr = HotKeyManager()
        mgr.start()
        mgr._pressed_keys.add(keyboard.Key.ctrl_l)
        mgr.stop()
        assert len(mgr._pressed_keys) == 0


class TestHotKeyManagerPauseResume:
    def test_pause_sets_flag(self):
        mgr = HotKeyManager()
        mgr.pause()
        assert mgr._paused is True

    def test_pause_clears_keys(self):
        mgr = HotKeyManager()
        mgr._pressed_keys.add(keyboard.Key.ctrl_l)
        mgr.pause()
        assert len(mgr._pressed_keys) == 0

    def test_resume_clears_flag(self):
        mgr = HotKeyManager()
        mgr.pause()
        mgr.resume()
        assert mgr._paused is False

    def test_resume_clears_keys(self):
        mgr = HotKeyManager()
        mgr.resume()
        assert len(mgr._pressed_keys) == 0


class TestHotKeyManagerUpdate:
    def test_update_hotkey(self):
        mgr = HotKeyManager("ctrl+space")
        mgr.update_hotkey("alt+space")
        assert mgr._hotkey_str == "alt+space"
        assert keyboard.Key.alt_l in mgr._hotkey_keys

    def test_update_clears_pressed_keys(self):
        mgr = HotKeyManager("ctrl+space")
        mgr._pressed_keys.add(keyboard.Key.ctrl_l)
        mgr.update_hotkey("alt+space")
        assert len(mgr._pressed_keys) == 0


class TestHotKeyManagerRelease:
    def test_release_while_recording_emits_stop(self):
        mgr = HotKeyManager("ctrl+space")
        stops = []
        mgr.recording_stop.connect(lambda: stops.append(True))

        with mgr._lock:
            mgr._is_recording = True
            mgr._pressed_keys.update({keyboard.Key.ctrl_l, keyboard.Key.space})

        mgr._on_release(keyboard.Key.space)
        assert stops, "releasing any combo key should stop recording"

    def test_release_does_not_deadlock(self):
        """Regression: _on_release must not re-enter _lock via _hotkey_still_held()."""
        mgr = HotKeyManager("ctrl+space")
        done = threading.Event()

        def _release_worker():
            mgr._on_release(keyboard.Key.space)
            mgr._on_release(keyboard.Key.ctrl_l)
            done.set()

        with mgr._lock:
            mgr._is_recording = True
            mgr._pressed_keys.update({keyboard.Key.ctrl_l, keyboard.Key.space})

        t = threading.Thread(target=_release_worker)
        t.start()
        t.join(timeout=1.0)
        assert done.is_set(), "release handler deadlocked"


class TestHotKeyManagerSignals:
    def test_signals_defined(self):
        mgr = HotKeyManager()
        assert hasattr(mgr, "recording_start")
        assert hasattr(mgr, "recording_stop")
        assert hasattr(mgr, "recording_cancel")


class TestKeyMapCompleteness:
    def test_key_map_has_basic_modifiers(self):
        assert "ctrl" in KEY_MAP
        assert "alt" in KEY_MAP
        assert "shift" in KEY_MAP
        assert "space" in KEY_MAP

    def test_key_map_has_letter_modifiers(self):
        assert "ctrl_l" in KEY_MAP
        assert "ctrl_r" in KEY_MAP
        assert "alt_l" in KEY_MAP
        assert "alt_r" in KEY_MAP
        assert "shift_l" in KEY_MAP
        assert "shift_r" in KEY_MAP

    def test_key_map_has_special_keys(self):
        assert "tab" in KEY_MAP
        assert "enter" in KEY_MAP
        assert "esc" in KEY_MAP
        assert "win" in KEY_MAP
        assert "cmd" in KEY_MAP


class _Suppressed(Exception):
    pass


class _Event:
    def __init__(self, vk, flags=0, extra=None):
        self.vkCode = vk
        self.flags = flags
        self.dwExtraInfo = extra


class TestWin32HotkeySuppression:
    VK_Z = 0x5A

    def _manager(self, monkeypatch, held=True):
        from unittest.mock import MagicMock

        mgr = HotKeyManager("alt+z")
        mgr._listener = MagicMock()
        mgr._listener.suppress_event.side_effect = _Suppressed
        monkeypatch.setattr(mgr, "_modifiers_held", lambda _mods: held)
        masks = []
        monkeypatch.setattr(mgr, "_send_menu_mask", lambda: masks.append(1))
        mgr._masks = masks
        return mgr

    def test_main_key_with_modifier_is_suppressed_and_arms_hold(self, monkeypatch):
        mgr = self._manager(monkeypatch)
        with pytest.raises(_Suppressed):
            mgr._win32_event_filter(0x0104, _Event(self.VK_Z))
        assert mgr._hold_pending is True
        assert mgr._masks == [1]

    def test_auto_repeat_is_suppressed_without_second_mask(self, monkeypatch):
        mgr = self._manager(monkeypatch)
        for _ in range(3):
            with pytest.raises(_Suppressed):
                mgr._win32_event_filter(0x0104, _Event(self.VK_Z))
        assert mgr._masks == [1]

    def test_release_of_suppressed_key_is_suppressed(self, monkeypatch):
        mgr = self._manager(monkeypatch)
        with pytest.raises(_Suppressed):
            mgr._win32_event_filter(0x0104, _Event(self.VK_Z))
        with pytest.raises(_Suppressed):
            mgr._win32_event_filter(0x0101, _Event(self.VK_Z))
        assert self.VK_Z not in mgr._suppressed_vks

    def test_plain_letter_without_modifier_passes_through(self, monkeypatch):
        mgr = self._manager(monkeypatch, held=False)
        assert mgr._win32_event_filter(0x0100, _Event(self.VK_Z)) is True
        assert mgr._win32_event_filter(0x0101, _Event(self.VK_Z)) is True
        mgr._listener.suppress_event.assert_not_called()

    def test_paused_capture_passes_through(self, monkeypatch):
        mgr = self._manager(monkeypatch)
        mgr.pause()
        assert mgr._win32_event_filter(0x0104, _Event(self.VK_Z)) is True

    def test_injected_and_unrelated_keys_pass_through(self, monkeypatch):
        mgr = self._manager(monkeypatch)
        assert mgr._win32_event_filter(0x0104, _Event(self.VK_Z, flags=0x10)) is True
        assert mgr._win32_event_filter(0x0104, _Event(0x41)) is True

    def test_own_synthetic_keys_never_reach_the_hotkey_state(self, monkeypatch):
        """Paste lifts and re-presses the held Alt mid-hold (SYNTHETIC_KEY_TAG)."""
        from sayink.platform import SYNTHETIC_KEY_TAG

        mgr = self._manager(monkeypatch)
        mgr.update_hotkey(DEFAULT_HOTKEY)
        mgr.set_continuous_trigger_mode(True)
        mgr._on_press(keyboard.Key.alt_l)
        with pytest.raises(_Suppressed):
            mgr._win32_event_filter(0x0104, _Event(0x58))
        assert mgr._hold_pending is True
        mgr._listener.suppress_event.reset_mock()
        vk_lmenu = 0xA4
        for msg in (0x0105, 0x0104):  # Alt up, Alt down from the paste
            tagged = _Event(vk_lmenu, flags=0x10, extra=SYNTHETIC_KEY_TAG)
            assert mgr._win32_event_filter(msg, tagged) is False
        assert mgr._hold_pending is True
        mgr._listener.suppress_event.assert_not_called()

    def test_default_alt_x_swallows_x_and_masks_the_menu(self, monkeypatch):
        mgr = self._manager(monkeypatch)
        mgr.update_hotkey(DEFAULT_HOTKEY)
        vk_x = 0x58
        with pytest.raises(_Suppressed):
            mgr._win32_event_filter(0x0104, _Event(vk_x))
        assert mgr._hold_pending is True
        assert mgr._masks == [1]
        with pytest.raises(_Suppressed):
            mgr._win32_event_filter(0x0105, _Event(vk_x))

    def test_ctrl_hotkey_does_not_send_menu_mask(self, monkeypatch):
        mgr = self._manager(monkeypatch)
        mgr.update_hotkey("ctrl+space")
        with pytest.raises(_Suppressed):
            mgr._win32_event_filter(0x0100, _Event(0x20))
        assert mgr._masks == []


class TestShiftLetterTapTypesTheLetter:
    """README: with Shift+X bound, a short tap still types a capital X, with
    no 「录音过短」 hint; only a hold records."""

    VK_X = 0x58

    def _manager(self, monkeypatch, hotkey="shift+x", shift_down=True):
        from unittest.mock import MagicMock

        mgr = HotKeyManager(hotkey)
        mgr._listener = MagicMock()
        mgr._listener.suppress_event.side_effect = _Suppressed
        monkeypatch.setattr(mgr, "_modifiers_held", lambda _mods: True)
        monkeypatch.setattr(mgr, "_async_key_down", lambda _vk: shift_down)
        monkeypatch.setattr(mgr, "_send_menu_mask", lambda: None)
        replayed = []
        monkeypatch.setattr(mgr, "_replay_swallowed_tap", lambda vk: replayed.append(vk))
        short = []
        mgr.hotkey_tap_too_short.connect(lambda: short.append(1))
        return mgr, replayed, short

    def _tap(self, mgr, vk):
        with pytest.raises(_Suppressed):
            mgr._win32_event_filter(0x0100, _Event(vk))
        mgr._hold_started_at -= 0.1  # 100 ms: a keystroke, not a hold
        with pytest.raises(_Suppressed):
            mgr._win32_event_filter(0x0101, _Event(vk))

    def test_short_tap_is_replayed_and_stays_quiet(self, monkeypatch):
        mgr, replayed, short = self._manager(monkeypatch)
        self._tap(mgr, self.VK_X)
        assert replayed == [self.VK_X]
        assert short == []
        assert mgr._hold_pending is False

    def test_hold_that_activated_is_not_replayed(self, monkeypatch):
        mgr, replayed, _short = self._manager(monkeypatch)
        started = []
        mgr.recording_start.connect(lambda: started.append(1))
        with pytest.raises(_Suppressed):
            mgr._win32_event_filter(0x0100, _Event(self.VK_X))
        mgr._on_hold_timeout()
        assert started == [1]
        with pytest.raises(_Suppressed):
            mgr._win32_event_filter(0x0101, _Event(self.VK_X))
        assert replayed == []

    def test_continuous_hold_with_auto_repeat_does_not_type_the_letter(self, monkeypatch):
        """Regression: auto-repeat after continuous listening started re-armed
        the hold, so letting go typed an extra capital X into the app."""
        mgr, replayed, short = self._manager(monkeypatch)
        mgr.set_continuous_trigger_mode(True)
        started = []
        mgr.continuous_listen_start.connect(lambda: started.append(1))
        with pytest.raises(_Suppressed):
            mgr._win32_event_filter(0x0100, _Event(self.VK_X))
        mgr._on_hold_timeout()
        for _ in range(3):
            with pytest.raises(_Suppressed):
                mgr._win32_event_filter(0x0100, _Event(self.VK_X))
        assert mgr._hold_pending is False
        with pytest.raises(_Suppressed):
            mgr._win32_event_filter(0x0101, _Event(self.VK_X))
        assert started == [1]
        assert replayed == []
        assert short == []

    def test_alt_hotkey_tap_keeps_the_hint_and_is_not_replayed(self, monkeypatch):
        mgr, replayed, short = self._manager(monkeypatch, hotkey="alt+z")
        self._tap(mgr, 0x5A)
        assert replayed == []
        assert short == [1]

    def test_ctrl_shift_combo_is_a_command_not_typing(self, monkeypatch):
        mgr, replayed, short = self._manager(monkeypatch, hotkey="ctrl+shift+x")
        self._tap(mgr, self.VK_X)
        assert replayed == []
        assert short == [1]

    def test_replay_presses_shift_only_when_the_user_let_go(self, monkeypatch):
        sent: list[tuple[int, int]] = []
        mgr = HotKeyManager("shift+x")
        monkeypatch.setattr(mgr, "_keybd_event", lambda vk, flags: sent.append((vk, flags)))

        monkeypatch.setattr(mgr, "_async_key_down", lambda _vk: True)
        mgr._replay_swallowed_tap(self.VK_X)
        assert sent == [(self.VK_X, 0), (self.VK_X, 0x0002)]

        sent.clear()
        monkeypatch.setattr(mgr, "_async_key_down", lambda _vk: False)
        mgr._replay_swallowed_tap(self.VK_X)
        assert sent == [(0x10, 0), (self.VK_X, 0), (self.VK_X, 0x0002), (0x10, 0x0002)]

    def test_shift_released_before_the_letter_still_types_it(self, monkeypatch):
        """Fast typists often let go of Shift first. That release used to end
        the hold with a 「录音过短」 hint, and the swallowed X never came back."""
        mgr, replayed, short = self._manager(monkeypatch)
        with pytest.raises(_Suppressed):
            mgr._win32_event_filter(0x0100, _Event(self.VK_X))
        mgr._hold_started_at -= 0.1
        mgr._on_release(keyboard.Key.shift_l)  # pynput's own path for Shift
        assert mgr._hold_pending is False
        assert short == []
        with pytest.raises(_Suppressed):
            mgr._win32_event_filter(0x0101, _Event(self.VK_X))
        assert replayed == [self.VK_X]

    def test_letter_auto_repeat_after_shift_is_up_does_not_rearm(self, monkeypatch):
        mgr, _replayed, _short = self._manager(monkeypatch)
        with pytest.raises(_Suppressed):
            mgr._win32_event_filter(0x0100, _Event(self.VK_X))
        mgr._on_release(keyboard.Key.shift_l)
        monkeypatch.setattr(mgr, "_modifiers_held", lambda _mods: False)
        for _ in range(5):
            with pytest.raises(_Suppressed):
                mgr._win32_event_filter(0x0100, _Event(self.VK_X))
        assert mgr._hold_pending is False
        assert keyboard.Key.shift_l not in mgr._pressed_keys

    def test_alt_hotkey_still_hints_when_alt_is_released_first(self, monkeypatch):
        mgr, replayed, short = self._manager(monkeypatch, hotkey="alt+z")
        with pytest.raises(_Suppressed):
            mgr._win32_event_filter(0x0104, _Event(0x5A))
        mgr._hold_started_at -= 0.1
        mgr._on_release(keyboard.Key.alt_l)
        assert short == [1]
        with pytest.raises(_Suppressed):
            mgr._win32_event_filter(0x0105, _Event(0x5A))
        assert replayed == []

    def test_right_shift_satisfies_a_generic_shift_hotkey(self):
        mgr = HotKeyManager("shift+x")
        mgr._on_press(keyboard.Key.shift_r)
        mgr._on_press(keyboard.KeyCode.from_char("x"))
        assert mgr._hold_pending is True

    def test_explicit_right_shift_hotkey_ignores_left_shift(self):
        mgr = HotKeyManager("shift_r+x")
        mgr._on_press(keyboard.Key.shift_l)
        mgr._on_press(keyboard.KeyCode.from_char("x"))
        assert mgr._hold_pending is False
        mgr._on_press(keyboard.Key.shift_r)
        assert mgr._hold_pending is True


class TestListenerCallbackArity:
    """Regression: pynput 1.8 counted ``_on_release``'s keyword-only ``quiet``
    and called it as ``(key, injected)``, and its ``False`` return (not a tap)
    is pynput's stop signal; either way the listener died on the first key
    release and the hotkey never worked again."""

    def test_real_listener_callbacks_accept_pynput_call(self, monkeypatch):
        monkeypatch.setattr(keyboard.Listener, "start", lambda self: None)
        monkeypatch.setattr(keyboard.Listener, "is_alive", lambda self: True)
        mgr = HotKeyManager("shift+x")
        mgr.start()
        listener = mgr._listener
        try:
            listener.on_press(keyboard.Key.shift_l, False)
            listener.on_press(keyboard.KeyCode.from_char("x"), False)
            assert mgr._hold_pending is True
            listener.on_release(keyboard.KeyCode.from_char("x"), False)
            listener.on_release(keyboard.Key.shift_l, False)
            assert mgr._pressed_keys == set()
        finally:
            mgr._listener = None


class TestHoldSurvivesUnrelatedKeys:
    """Regression: the injected menu-mask key (vkE8) released mid-hold made
    every hold report 录音过短 and nothing started."""

    def _armed(self):
        mgr = HotKeyManager("alt+z")
        arms = []
        mgr._arm_hold_on_main.connect(lambda: arms.append(mgr._hold_pending))
        short = []
        mgr.hotkey_tap_too_short.connect(lambda: short.append(1))
        mgr._on_press(keyboard.Key.alt_l)
        mgr._on_press(keyboard.KeyCode.from_char("z"))
        return mgr, arms, short

    def test_menu_mask_release_keeps_hold_pending(self):
        mgr, _arms, short = self._armed()
        mask = keyboard.KeyCode.from_vk(0xE8)
        mgr._on_press(mask)
        mgr._on_release(mask)
        assert mgr._hold_pending is True
        assert short == []

    def test_unrelated_key_release_keeps_hold_pending(self):
        mgr, _arms, short = self._armed()
        mgr._on_release(keyboard.Key.shift_l)
        assert mgr._hold_pending is True
        assert short == []

    def test_auto_repeat_does_not_restart_hold_timer(self):
        mgr, arms, _short = self._armed()
        for _ in range(10):
            mgr._on_press(keyboard.KeyCode.from_char("z"))
            mgr._on_press(keyboard.Key.alt_l)
        assert arms == [True]

    def test_releasing_hotkey_key_still_reports_short_tap(self, monkeypatch):

        mgr, _arms, short = self._armed()
        mgr._hold_started_at -= 1.0
        mgr._on_release(keyboard.KeyCode.from_char("z"))
        assert mgr._hold_pending is False
        assert short == [1]

    def test_filter_path_hold_reaches_recording_start(self, monkeypatch):
        from unittest.mock import MagicMock

        mgr = HotKeyManager("alt+z")
        mgr._listener = MagicMock()
        mgr._listener.suppress_event.side_effect = _Suppressed
        monkeypatch.setattr(mgr, "_modifiers_held", lambda _mods: True)
        monkeypatch.setattr(
            mgr, "_send_menu_mask",
            lambda: (mgr._on_press(keyboard.KeyCode.from_vk(0xE8)),
                     mgr._on_release(keyboard.KeyCode.from_vk(0xE8))),
        )
        started = []
        mgr.recording_start.connect(lambda: started.append(1))
        mgr._on_press(keyboard.Key.alt_l)
        with pytest.raises(_Suppressed):
            mgr._win32_event_filter(0x0104, _Event(0x5A))
        assert mgr._hold_pending is True
        mgr._on_hold_timeout()
        assert started == [1]

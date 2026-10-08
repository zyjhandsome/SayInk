import pytest
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


class TestHotKeyManagerInit:
    def test_default_hotkey(self):
        mgr = HotKeyManager()
        assert mgr._hotkey_str == "alt+z"
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
    def __init__(self, vk, flags=0):
        self.vkCode = vk
        self.flags = flags


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

    def test_ctrl_hotkey_does_not_send_menu_mask(self, monkeypatch):
        mgr = self._manager(monkeypatch)
        mgr.update_hotkey("ctrl+space")
        with pytest.raises(_Suppressed):
            mgr._win32_event_filter(0x0100, _Event(0x20))
        assert mgr._masks == []


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
        import sayink.hotkey_manager as hm

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

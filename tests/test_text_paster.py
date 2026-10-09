import sys

import pytest

import sayink.text_paster as tp
from sayink.text_paster import TextPaster, get_foreground_window_info

_REAL_PASTE_SHORTCUT = tp._paste_shortcut


class TestTextPasterInit:
    def test_init(self):
        paster = TextPaster()
        assert paster is not None

    def test_own_titles_defined(self):
        paster = TextPaster()
        assert hasattr(paster, "OWN_TITLES")
        assert isinstance(paster.OWN_TITLES, set)
        assert "SayInk 设置" in paster.OWN_TITLES
        assert "SayInk" in paster.OWN_TITLES


class TestGetForegroundWindowInfo:
    def test_returns_tuple(self):
        info = get_foreground_window_info()
        assert isinstance(info, tuple)
        assert len(info) >= 2


class TestTextPasterIsOwnWindow:
    def test_windows_with_pid(self):
        if sys.platform != "win32":
            pytest.skip("Windows-specific test")
        paster = TextPaster()
        import os
        info = (0, "Test", os.getpid())
        assert paster._is_own_window(info) is True

    def test_different_pid(self):
        paster = TextPaster()
        import os
        info = (0, "Test", os.getpid() + 9999)
        assert paster._is_own_window(info) is False

    def test_own_title(self):
        paster = TextPaster()
        info = (123, "SayInk", 9999)
        assert paster._is_own_window(info) is True

    def test_own_settings_title(self):
        paster = TextPaster()
        info = (123, "SayInk 设置", 9999)
        assert paster._is_own_window(info) is True

    def test_other_title(self):
        paster = TextPaster()
        info = (123, "Notepad", 9999)
        assert paster._is_own_window(info) is False


class TestTextPasterPaste:
    def test_empty_text_returns_error(self):
        paster = TextPaster()
        result = paster.paste("")
        assert result.startswith("error:")

    def test_paste_returns_status(self, paste_env):
        paste_env["set_foreground"]([(1234, "Editor", 4242)])
        paster = TextPaster()
        result = paster.paste("测试文本")
        assert result == "pasted"
        assert paste_env["clipboard"] == "测试文本"

    def test_paste_async_empty_text(self):
        paster = TextPaster()
        results = []
        paster.paste_async("", lambda r: results.append(r))
        assert [result.status for result in results] == ["error"]
        assert results[0].detail == "空文本"

    def test_restore_clipboard_flag(self):
        paster = TextPaster(restore_clipboard=True)
        assert paster.restore_clipboard is True


class TestPasteShortcut:
    def test_module_imports(self):
        from sayink import text_paster
        assert hasattr(text_paster, "get_foreground_window_info")
        assert hasattr(text_paster, "_paste_shortcut")
        assert not hasattr(text_paster, "pyautogui")

    @pytest.mark.parametrize("platform", ["darwin", "linux"])
    def test_failed_system_command_falls_back_to_clipboard(self, platform, paste_env, monkeypatch):
        import subprocess
        from unittest.mock import Mock

        def run(command, **kwargs):
            if kwargs.get("check"):
                raise subprocess.CalledProcessError(1, command)
            return subprocess.CompletedProcess(command, 1)

        monkeypatch.setattr(tp.sys, "platform", platform)
        monkeypatch.setattr(tp.subprocess, "run", Mock(side_effect=run))
        monkeypatch.setattr(tp, "_paste_shortcut", _REAL_PASTE_SHORTCUT)
        paste_env["set_foreground"]([(1234, "Editor", 4242)])
        results = []
        TextPaster().paste_async("keep this text", results.append)
        assert [result.status for result in results] == ["clipboard"]
        assert paste_env["clipboard"] == "keep this text"


class TestCrossPlatformSupport:
    def test_platform_detection(self):
        assert sys.platform in ["win32", "darwin", "linux"]


@pytest.fixture
def paste_env(monkeypatch):
    """Drive paste_async deterministically without a Qt event loop."""
    state = {
        "copied": [],
        "shortcut_calls": 0,
        "clipboard": "OLD",
        "fg_sequence": None,
    }

    monkeypatch.setattr(tp.QTimer, "singleShot", lambda ms, fn: fn())

    def _copy(text):
        state["copied"].append(text)
        state["clipboard"] = text

    monkeypatch.setattr(tp.pyperclip, "copy", _copy)
    monkeypatch.setattr(tp.pyperclip, "paste", lambda: state["clipboard"])

    def _shortcut():
        state["shortcut_calls"] += 1

    monkeypatch.setattr(tp, "_paste_shortcut", _shortcut)
    monkeypatch.setattr(tp, "_process_name_from_window_info", lambda _info: "editor.exe")
    monkeypatch.setattr(tp, "target_rejects_synthetic_input", lambda _info: False)

    def set_foreground(sequence):
        seq = list(sequence)

        def _fg():
            return seq.pop(0) if len(seq) > 1 else seq[0]

        monkeypatch.setattr(tp, "get_foreground_window_info", _fg)

    state["set_foreground"] = set_foreground
    return state


class TestPasteAsyncFlow:
    def test_verified_paste_returns_pasted(self, paste_env):
        # Same foreground window before and after → verified paste.
        paste_env["set_foreground"]([(1234, "Notepad", 4242)])
        paster = TextPaster()
        results = []
        paster.paste_async("你好", results.append)
        assert [result.status for result in results] == ["sent"]
        assert results[0].target_app == "editor.exe"
        assert paste_env["shortcut_calls"] == 1
        assert "你好" in paste_env["copied"]

    def test_focus_changed_downgrades_to_clipboard(self, paste_env):
        # Foreground changes after paste → cannot confirm → clipboard.
        paste_env["set_foreground"]([(1234, "Editor", 1), (9999, "Other", 2)])
        paster = TextPaster()
        results = []
        paster.paste_async("文本", results.append)
        assert [result.status for result in results] == ["clipboard"]

    def test_focus_switch_before_send_never_sends_shortcut_to_new_window(self, paste_env, monkeypatch):
        foreground = [(111, "Target A", 11111)]
        pending = []
        sent_to = []
        monkeypatch.setattr(tp, "get_foreground_window_info", lambda: foreground[0])
        monkeypatch.setattr(tp.QTimer, "singleShot", lambda ms, fn: pending.append(fn))
        monkeypatch.setattr(tp, "_paste_shortcut", lambda: sent_to.append(foreground[0][0]))
        paster = TextPaster()
        results = []

        paster.paste_async("口述内容", results.append)
        foreground[0] = (222, "Target B", 22222)
        while pending:
            pending.pop(0)()

        assert sent_to == []
        assert [(r.status, r.detail) for r in results] == [("clipboard", "focus_changed")]
        assert paste_env["clipboard"] == "口述内容"

    def test_focus_switch_after_send_is_reported_as_unverified_not_clipboard(self, paste_env):
        """README: Ctrl+V already went out, so the bar must not say 「已复制」
        (a second manual paste would duplicate the words)."""
        paste_env["set_foreground"]([(1234, "Editor", 1), (1234, "Editor", 1), (9999, "Other", 2)])
        paster = TextPaster()
        results = []
        paster.paste_async("文本", results.append)
        assert paste_env["shortcut_calls"] == 1
        assert [(r.status, r.detail) for r in results] == [("unverified", "focus_changed_after_send")]
        assert results[0].target_app == "editor.exe"
        assert paste_env["clipboard"] == "文本"

    def test_own_window_skips_paste(self, paste_env):
        paste_env["set_foreground"]([(1, "SayInk", 1)])
        paster = TextPaster()
        results = []
        paster.paste_async("文本", results.append)
        assert [result.status for result in results] == ["clipboard"]
        assert paste_env["shortcut_calls"] == 0

    def test_no_target_wayland_hwnd_zero(self, paste_env):
        # hwnd == 0 (e.g. Wayland/no xdotool) → honest clipboard fallback.
        paste_env["set_foreground"]([(0, "", 0)])
        paster = TextPaster()
        results = []
        paster.paste_async("文本", results.append)
        assert [result.status for result in results] == ["clipboard"]
        assert paste_env["shortcut_calls"] == 0

    def test_shortcut_exception_downgrades_to_clipboard(self, paste_env, monkeypatch):
        paste_env["set_foreground"]([(1234, "Editor", 1)])

        def _boom():
            raise RuntimeError("blocked")

        monkeypatch.setattr(tp, "_paste_shortcut", _boom)
        paster = TextPaster()
        results = []
        paster.paste_async("文本", results.append)
        assert [result.status for result in results] == ["clipboard"]

    def test_restore_clipboard_after_verified_paste(self, paste_env):
        paste_env["set_foreground"]([(1234, "Editor", 1)])
        paster = TextPaster(restore_clipboard=True)
        results = []
        paster.paste_async("新文本", results.append)
        assert [result.status for result in results] == ["sent"]
        # Original clipboard restored after a verified paste.
        assert paste_env["clipboard"] == "OLD"


    def test_elevated_target_is_reported_as_clipboard_without_keys(self, paste_env, monkeypatch):
        paste_env["set_foreground"]([(1234, "Administrator: cmd", 4242)])
        monkeypatch.setattr(tp, "target_rejects_synthetic_input", lambda _info: True)
        paster = TextPaster()
        results = []
        paster.paste_async("文本", results.append)
        assert [result.status for result in results] == ["clipboard"]
        assert results[0].target_app == "editor.exe"
        assert paste_env["shortcut_calls"] == 0
        assert paste_env["clipboard"] == "文本"

    def test_empty_old_clipboard_is_not_restored(self, paste_env):
        paste_env["clipboard"] = ""
        paste_env["set_foreground"]([(1234, "Editor", 1)])
        paster = TextPaster(restore_clipboard=True)
        paster.paste_async("新文本", lambda _r: None)
        assert paste_env["clipboard"] == "新文本"

    def test_restore_skipped_when_user_copied_something_else(self, paste_env, monkeypatch):
        paste_env["set_foreground"]([(1234, "Editor", 1)])
        pending = []
        monkeypatch.setattr(
            tp.QTimer,
            "singleShot",
            lambda ms, fn: pending.append(fn) if ms == tp.RESTORE_CLIPBOARD_DELAY_MS else fn(),
        )
        paster = TextPaster(restore_clipboard=True)
        paster.paste_async("新文本", lambda _r: None)
        paste_env["clipboard"] = "用户刚复制的"
        pending[0]()
        assert paste_env["clipboard"] == "用户刚复制的"

    def test_back_to_back_pastes_restore_the_users_clipboard(self, paste_env, monkeypatch):
        """Regression: the second segment started before the first restore ran,
        saved the first transcript as 「原剪贴板」 and put it back at the end."""
        paste_env["set_foreground"]([(1234, "Editor", 1)])
        pending = []
        monkeypatch.setattr(
            tp.QTimer,
            "singleShot",
            lambda ms, fn: pending.append(fn) if ms == tp.RESTORE_CLIPBOARD_DELAY_MS else fn(),
        )
        paster = TextPaster(restore_clipboard=True)
        paster.paste_async("第一句", lambda _r: None)
        paster.paste_async("第二句", lambda _r: None)
        for restore in pending:
            restore()
        assert paste_env["clipboard"] == "OLD"


class TestForegroundProcessName:
    """Q-13: the Win32 process-name path, with pywin32 / ctypes replaced by fakes.
    Only the EXE basename may leave this module (no window title, no path)."""

    @pytest.fixture
    def win32(self, monkeypatch):
        from types import SimpleNamespace

        monkeypatch.setattr(tp.sys, "platform", "win32")
        closed = []
        win32api = SimpleNamespace(
            OpenProcess=lambda _access, _inherit, pid: f"h{pid}",
            CloseHandle=lambda h: closed.append(h),
        )
        win32con = SimpleNamespace(PROCESS_QUERY_INFORMATION=0x400, PROCESS_VM_READ=0x10)
        win32process = SimpleNamespace(
            GetModuleFileNameEx=lambda _h, _mod: r"C:\Program Files\Editor\editor.exe",
            GetWindowThreadProcessId=lambda _hwnd: (7, 4242),
        )
        win32gui = SimpleNamespace(GetForegroundWindow=lambda: 99, GetWindowText=lambda _h: "secret.txt - Editor")
        for name, mod in (
            ("win32api", win32api),
            ("win32con", win32con),
            ("win32process", win32process),
            ("win32gui", win32gui),
        ):
            monkeypatch.setitem(sys.modules, name, mod)
        return SimpleNamespace(closed=closed, api=win32api, process=win32process, gui=win32gui)

    def test_foreground_window_info_carries_hwnd_title_and_pid(self, win32):
        assert tp._get_foreground_window_win32() == (99, "secret.txt - Editor", 4242)
        assert tp.get_foreground_window_info() == (99, "secret.txt - Editor", 4242)

    def test_foreground_window_failure_yields_no_target(self, win32, monkeypatch):
        def boom():
            raise OSError("no desktop")

        monkeypatch.setattr(win32.gui, "GetForegroundWindow", boom)
        assert tp._get_foreground_window_win32() == (0, "", 0)

    def test_process_name_is_the_exe_basename_and_the_handle_is_closed(self, win32):
        assert tp._process_name_from_window_info((99, "secret.txt - Editor", 4242)) == "editor.exe"
        assert win32.closed == ["h4242"]
        assert tp.get_foreground_process_name() == "editor.exe"

    def test_no_pid_or_short_info_gives_empty_name(self, win32):
        assert tp._process_name_from_window_info((99, "x", 0)) == ""
        assert tp._process_name_from_window_info((99, "x")) == ""

    def test_elevated_target_falls_back_to_the_limited_query(self, win32, monkeypatch):
        import ctypes
        from types import SimpleNamespace

        def denied(*_a):
            raise PermissionError("access denied")

        monkeypatch.setattr(win32.api, "OpenProcess", denied)

        class _Kernel32:
            def __init__(self):
                self.closed = []

            def OpenProcess(self, access, _inherit, pid):
                assert access == tp._PROCESS_QUERY_LIMITED_INFORMATION
                return 0x55 if pid == 4242 else 0

            def QueryFullProcessImageNameW(self, _h, _flags, buf, _size_ref):
                buf.value = r"C:\Windows\regedit.exe"
                return 1

            def CloseHandle(self, h):
                self.closed.append(h)

        kernel32 = _Kernel32()
        monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=kernel32), raising=False)

        assert tp._process_name_from_window_info((99, "Registry Editor", 4242)) == "regedit.exe"
        assert kernel32.closed == [0x55]
        # Handle could not be opened at all → still no exception, just empty.
        assert tp._process_name_limited((99, "x", 1)) == ""

    def test_limited_query_failure_is_empty_not_an_exception(self, win32, monkeypatch):
        import ctypes
        from types import SimpleNamespace

        class _Kernel32:
            def OpenProcess(self, *_a):
                return 0x55

            def QueryFullProcessImageNameW(self, *_a):
                return 0

            def CloseHandle(self, _h):
                pass

        monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=_Kernel32()), raising=False)
        assert tp._process_name_limited((99, "x", 4242)) == ""
        assert tp._process_name_limited((99, "x", 0)) == ""


class TestIntegrityCheck:
    def test_own_process_is_not_rejected(self):
        import os

        assert tp.target_rejects_synthetic_input((1, "self", os.getpid())) is False

    def test_missing_pid_is_not_rejected(self):
        assert tp.target_rejects_synthetic_input((1, "x", 0)) is False

    def test_higher_integrity_target_is_rejected(self, monkeypatch):
        import os

        monkeypatch.setattr(tp.sys, "platform", "win32")
        levels = {os.getpid(): 0x2000, 777: 0x3000, 778: -1, 779: None}
        monkeypatch.setattr(tp, "_integrity_rid", lambda pid: levels[pid])
        assert tp.target_rejects_synthetic_input((1, "a", 777)) is True
        assert tp.target_rejects_synthetic_input((1, "b", 778)) is True
        assert tp.target_rejects_synthetic_input((1, "c", 779)) is False


class TestVerifyPasteTarget:
    def test_hwnd_zero_is_not_verified(self):
        assert tp._verify_paste_target(0) is False


class _FakeUser32:
    """Records keybd_event calls; ``down`` lists the VKs GetAsyncKeyState reports held."""

    def __init__(self, down=()):
        self.down = set(down)
        self.events: list[tuple[int, int]] = []
        self.extras: list[int] = []

    def GetAsyncKeyState(self, vk):
        return 0x8000 if vk in self.down else 0

    def MapVirtualKeyW(self, vk, _kind):
        return vk

    def keybd_event(self, vk, _scan, flags, extra):
        self.events.append((vk, flags))
        self.extras.append(extra)


@pytest.fixture
def fake_user32(monkeypatch):
    import ctypes
    from types import SimpleNamespace

    fake = _FakeUser32()
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(user32=fake), raising=False)
    return fake


class TestWin32PasteShortcut:
    """The hotkey modifier is often still down when the paste fires. Ctrl+Shift+V
    / Ctrl+Alt+V mean something else in many apps, so lift it for the shortcut."""

    CTRL, V, SHIFT, ALT, RALT, UP, EXT = 0x11, 0x56, 0xA0, 0xA4, 0xA5, 0x0002, 0x0001

    def test_plain_ctrl_v_when_nothing_else_is_held(self, fake_user32):
        tp._paste_shortcut_win32()
        assert fake_user32.events == [
            (self.CTRL, 0), (self.V, 0), (self.V, self.UP), (self.CTRL, self.UP),
        ]

    def test_held_shift_is_lifted_around_ctrl_v_and_pressed_again(self, fake_user32):
        fake_user32.down = {self.SHIFT}
        tp._paste_shortcut_win32()
        assert fake_user32.events == [
            (self.SHIFT, self.UP),
            (self.CTRL, 0), (self.V, 0), (self.V, self.UP), (self.CTRL, self.UP),
            (self.SHIFT, 0),
        ]

    def test_every_synthetic_key_is_tagged_for_the_hotkey_listener(self, fake_user32):
        """Untagged, the lifted Alt would cancel an Alt+X hold in progress."""
        from sayink.platform import SYNTHETIC_KEY_TAG

        fake_user32.down = {self.ALT}
        tp._paste_shortcut_win32()
        assert fake_user32.extras
        assert set(fake_user32.extras) == {SYNTHETIC_KEY_TAG}

    def test_held_alt_gets_the_menu_mask_so_release_does_not_open_a_menu(self, fake_user32):
        fake_user32.down = {self.ALT}
        tp._paste_shortcut_win32()
        assert fake_user32.events[0] == (self.ALT, self.UP)
        assert fake_user32.events[-3:] == [
            (self.ALT, 0), (tp._VK_MENU_MASK, 0), (tp._VK_MENU_MASK, self.UP),
        ]

    def test_held_right_alt_is_pressed_again_as_right_alt(self, fake_user32):
        """Re-pressing the generic VK_MENU lands on the left Alt; the user then
        lets go of the right one and the left Alt stays stuck down."""
        fake_user32.down = {self.RALT}
        tp._paste_shortcut_win32()
        assert fake_user32.events[0] == (self.RALT, self.UP | self.EXT)
        assert (self.RALT, self.EXT) in fake_user32.events
        assert all(vk != self.ALT for vk, _ in fake_user32.events)

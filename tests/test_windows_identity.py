"""Tests for Windows shell identity helpers (sayink/platform/windows_identity.py)."""

from __future__ import annotations

import types
from pathlib import Path


import sayink.platform.windows_identity as wi


class TestAppUserModelId:
    def test_constants(self):
        assert wi.APP_USER_MODEL_ID == "SayInk.SayInkApp"
        assert wi.APP_DISPLAY_NAME == "SayInk"

    def test_set_id_noop_on_non_win32(self, monkeypatch):
        monkeypatch.setattr(wi.sys, "platform", "linux")
        # Should return without touching ctypes.
        assert wi.set_windows_app_user_model_id() is None

    def test_set_id_calls_shell32_on_win32(self, monkeypatch):
        monkeypatch.setattr(wi.sys, "platform", "win32")
        calls = []
        fake_shell32 = types.SimpleNamespace(
            SetCurrentProcessExplicitAppUserModelID=lambda x: calls.append(x)
        )
        fake_ctypes = types.SimpleNamespace(
            windll=types.SimpleNamespace(shell32=fake_shell32)
        )
        monkeypatch.setitem(__import__("sys").modules, "ctypes", fake_ctypes)
        wi.set_windows_app_user_model_id()
        assert calls == [wi.APP_USER_MODEL_ID]

    def test_set_id_swallows_errors(self, monkeypatch):
        monkeypatch.setattr(wi.sys, "platform", "win32")

        def _boom(x):
            raise OSError("nope")

        fake_shell32 = types.SimpleNamespace(
            SetCurrentProcessExplicitAppUserModelID=_boom
        )
        fake_ctypes = types.SimpleNamespace(
            windll=types.SimpleNamespace(shell32=fake_shell32)
        )
        monkeypatch.setitem(__import__("sys").modules, "ctypes", fake_ctypes)
        # Must not raise.
        wi.set_windows_app_user_model_id()


class TestConfigureIdentity:
    def test_sets_qt_display_names(self, monkeypatch):
        monkeypatch.setattr(wi.sys, "platform", "linux")
        recorded = {}

        app = types.SimpleNamespace(
            setOrganizationName=lambda n: recorded.setdefault("org", n),
            setApplicationDisplayName=lambda n: recorded.setdefault("disp", n),
        )
        wi.configure_windows_app_identity(app)
        assert recorded["org"] == wi.APP_DISPLAY_NAME
        assert recorded["disp"] == wi.APP_DISPLAY_NAME

    def test_win32_invokes_shortcut(self, monkeypatch):
        monkeypatch.setattr(wi.sys, "platform", "win32")
        called = []
        monkeypatch.setattr(wi, "_ensure_start_menu_shortcut", lambda: called.append(True))
        app = types.SimpleNamespace(
            setOrganizationName=lambda n: None,
            setApplicationDisplayName=lambda n: None,
        )
        wi.configure_windows_app_identity(app)
        assert called == [True]


class TestLauncherTarget:
    def test_project_root_is_path(self):
        assert isinstance(wi._project_root(), Path)

    def test_frozen_uses_executable(self, monkeypatch):
        monkeypatch.setattr(wi.sys, "frozen", True, raising=False)
        monkeypatch.setattr(wi.sys, "executable", r"C:\app\SayInk.exe")
        target, args, workdir = wi._launcher_target()
        assert target.endswith("SayInk.exe")
        assert args == ""

    def test_dev_uses_run_py(self, monkeypatch):
        monkeypatch.setattr(wi.sys, "frozen", False, raising=False)
        target, args, workdir = wi._launcher_target()
        assert "run.py" in args

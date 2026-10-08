"""The Run-key value must relaunch SayInk from any working directory.

Before: it stored ``sys.argv[0]`` as typed (often a relative ``run.py``) and,
when running from source, pointed at a bare ``python.exe`` that would just open
an interpreter prompt at login.
"""

from __future__ import annotations

import os
import sys

import pytest

from sayink import app as app_module
from sayink.app import auto_start_command


def _unquote_parts(command: str) -> list[str]:
    assert command.count('"') % 2 == 0
    return [part for part in command.split('"') if part.strip()]


class TestAutoStartCommand:
    def test_frozen_build_runs_the_exe_alone(self, monkeypatch):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", r"C:\Program Files\SayInk\SayInk.exe")
        assert auto_start_command() == r'"C:\Program Files\SayInk\SayInk.exe"'

    def test_source_checkout_runs_run_py_through_the_interpreter(self, monkeypatch):
        monkeypatch.delattr(sys, "frozen", raising=False)
        monkeypatch.delattr(sys, "_MEIPASS", raising=False)
        monkeypatch.setattr(sys, "executable", "python.exe")
        parts = _unquote_parts(auto_start_command())
        assert len(parts) == 2
        exe, script = parts
        assert os.path.isabs(exe) and exe.lower().endswith("python.exe")
        assert os.path.isabs(script)
        assert os.path.basename(script) == "run.py"
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(app_module.__file__)))
        assert os.path.dirname(script) == repo_root

    @pytest.mark.parametrize("argv0", ["run.py", r".\run.py", "sayink"])
    def test_does_not_depend_on_how_the_process_was_started(self, monkeypatch, argv0):
        monkeypatch.delattr(sys, "frozen", raising=False)
        monkeypatch.delattr(sys, "_MEIPASS", raising=False)
        monkeypatch.setattr(sys, "argv", [argv0])
        first = auto_start_command()
        monkeypatch.setattr(sys, "argv", [r"D:\elsewhere\run.py"])
        assert auto_start_command() == first

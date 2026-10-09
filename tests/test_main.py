"""Tests for single-instance locking and crash hooks (sayink/main.py)."""

from __future__ import annotations

import logging
import tempfile
import types

import pytest

import sayink.main as main


@pytest.fixture
def temp_lock(monkeypatch, tmp_path):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(main, "_lock_file_path", None, raising=False)
    monkeypatch.setattr(main, "_win_mutex", None, raising=False)
    yield tmp_path
    main.cleanup_lock()


class TestFileLockFallback:
    def test_first_instance_acquires_lock(self, monkeypatch, temp_lock):
        monkeypatch.setattr(main.sys, "platform", "linux")
        assert main.check_single_instance() is True
        assert (temp_lock / "sayink.lock").exists()

    def test_second_instance_blocked_when_pid_alive(self, monkeypatch, temp_lock):
        monkeypatch.setattr(main.sys, "platform", "linux")
        # Windows implements os.kill(pid, 0) as TerminateProcess rather than
        # the POSIX existence probe. Model the intended live-process result so
        # this fallback test is safe and deterministic on the Win11 CI host.
        monkeypatch.setattr(main.os, "kill", lambda _pid, _sig: None)
        assert main.check_single_instance() is True
        # A living PID (our own) in the lock → second check refuses to start.
        (temp_lock / "sayink.lock").write_text(str(__import__("os").getpid()))
        assert main.check_single_instance() is False

    def test_stale_pid_allows_start(self, monkeypatch, temp_lock):
        monkeypatch.setattr(main.sys, "platform", "linux")

        def _dead_pid(pid, sig):
            raise OSError("no such process")

        monkeypatch.setattr(main.os, "kill", _dead_pid)
        (temp_lock / "sayink.lock").write_text("999999")
        assert main.check_single_instance() is True

    def test_cleanup_removes_lock_file(self, monkeypatch, temp_lock):
        monkeypatch.setattr(main.sys, "platform", "linux")
        main.check_single_instance()
        assert (temp_lock / "sayink.lock").exists()
        main.cleanup_lock()
        assert not (temp_lock / "sayink.lock").exists()


class TestWindowsMutex:
    def test_mutex_first_instance(self, monkeypatch):
        monkeypatch.setattr(main.sys, "platform", "win32")

        fake_kernel = types.SimpleNamespace(
            CreateMutexW=lambda a, b, name: 111,
            OpenMutexW=lambda access, inherit, name: 0,
            GetLastError=lambda: 0,
            CloseHandle=lambda h: True,
        )
        fake_windll = types.SimpleNamespace(kernel32=fake_kernel)
        monkeypatch.setattr(main.ctypes, "windll", fake_windll, raising=False)
        monkeypatch.setattr(main, "_win_mutex", None, raising=False)

        assert main.check_single_instance() is True
        assert main._win_mutex == 111
        main._win_mutex = None

    def test_running_legacy_voiceink_blocks_sayink(self, monkeypatch):
        """README: 旧版 VoiceInk 还在运行时，SayInk 不会再起第二个实例。"""
        monkeypatch.setattr(main.sys, "platform", "win32")

        closed = []
        opened = []

        def _open(access, inherit, name):
            opened.append(name)
            return 333 if name == main.LEGACY_MUTEX_NAME else 0

        fake_kernel = types.SimpleNamespace(
            CreateMutexW=lambda a, b, name: 111,
            OpenMutexW=_open,
            GetLastError=lambda: 0,
            CloseHandle=lambda h: closed.append(h),
        )
        fake_windll = types.SimpleNamespace(kernel32=fake_kernel)
        monkeypatch.setattr(main.ctypes, "windll", fake_windll, raising=False)
        monkeypatch.setattr(main, "_win_mutex", None, raising=False)

        assert main.check_single_instance() is False
        assert opened == [main.LEGACY_MUTEX_NAME]
        assert sorted(closed) == [111, 333]
        assert main._win_mutex is None

    def test_mutex_already_exists_blocks(self, monkeypatch):
        monkeypatch.setattr(main.sys, "platform", "win32")

        closed = []
        fake_kernel = types.SimpleNamespace(
            CreateMutexW=lambda a, b, name: 222,
            OpenMutexW=lambda access, inherit, name: 0,
            GetLastError=lambda: 183,  # ERROR_ALREADY_EXISTS
            CloseHandle=lambda h: closed.append(h),
        )
        fake_windll = types.SimpleNamespace(kernel32=fake_kernel)
        monkeypatch.setattr(main.ctypes, "windll", fake_windll, raising=False)
        monkeypatch.setattr(main, "_win_mutex", None, raising=False)

        assert main.check_single_instance() is False
        assert closed == [222]


class TestLogging:
    def test_setup_logging_writes_rotating_file_and_crash_log(self, tmp_path):
        root = logging.getLogger()
        saved = root.handlers[:]
        try:
            path = main.setup_logging(str(tmp_path))
            logging.getLogger("SayInk").info("落盘测试")
            for handler in root.handlers:
                handler.flush()
            assert path is not None
            assert "落盘测试" in (tmp_path / "sayink.log").read_text(encoding="utf-8")
            assert (tmp_path / "crash.log").exists()
        finally:
            import faulthandler

            faulthandler.disable()
            for handler in root.handlers:
                if handler not in saved:
                    handler.close()
            root.handlers[:] = saved
            if main._crash_file is not None:
                main._crash_file.close()
                main._crash_file = None

    def test_setup_logging_without_console_stream(self, tmp_path, monkeypatch):
        monkeypatch.setattr(main.sys, "stderr", None)
        root = logging.getLogger()
        saved = root.handlers[:]
        try:
            main.setup_logging(str(tmp_path))
            assert all(
                not isinstance(h, logging.StreamHandler) or hasattr(h, "baseFilename")
                for h in root.handlers
            )
        finally:
            import faulthandler

            faulthandler.disable()
            for handler in root.handlers:
                if handler not in saved:
                    handler.close()
            root.handlers[:] = saved
            if main._crash_file is not None:
                main._crash_file.close()
                main._crash_file = None


class TestActivation:
    def test_second_instance_reaches_running_server(self, _qapp_session, monkeypatch):
        monkeypatch.setattr(main, "activation_server_name", lambda: "SayInk-activate-test")
        calls = []
        server = main.start_activation_server(lambda: calls.append("show"))
        assert server is not None
        try:
            import threading

            result = {}
            worker = threading.Thread(
                target=lambda: result.setdefault("ok", main._activate_running_instance())
            )
            worker.start()
            from PyQt6.QtCore import QDeadlineTimer

            deadline = QDeadlineTimer(3000)
            while worker.is_alive() and not deadline.hasExpired():
                _qapp_session.processEvents()
            worker.join(1)
            for _ in range(20):
                _qapp_session.processEvents()
            assert result.get("ok") is True
            assert calls == ["show"]
        finally:
            server.close()

    def test_no_running_server_returns_false(self, _qapp_session, monkeypatch):
        monkeypatch.setattr(main, "activation_server_name", lambda: "SayInk-activate-none")
        assert main._activate_running_instance() is False


class TestExceptionHooks:
    def test_hooks_installed(self):
        log = logging.getLogger("SayInk-test-hooks")
        main._install_exception_hooks(log)
        import sys as _sys
        import threading as _threading

        assert _sys.excepthook is not None
        assert _threading.excepthook is not None

    def test_excepthook_logs_non_keyboard_interrupt(self, caplog):
        log = logging.getLogger("SayInk-test-hooks2")
        main._install_exception_hooks(log)
        import sys as _sys

        with caplog.at_level(logging.CRITICAL):
            try:
                raise ValueError("boom")
            except ValueError:
                _sys.excepthook(*_sys.exc_info())
        assert any("未捕获异常" in r.message for r in caplog.records)

    def test_threading_hook_ignores_system_exit(self):
        log = logging.getLogger("SayInk-test-hooks3")
        main._install_exception_hooks(log)
        import threading as _threading

        args = types.SimpleNamespace(
            exc_type=SystemExit,
            exc_value=SystemExit(),
            exc_traceback=None,
            thread=None,
        )
        # Should not raise.
        _threading.excepthook(args)

"""Regression tests for shutdown config flush, early single-instance lock release, and bounded native drain."""

from __future__ import annotations

import json
import logging
import threading
import time
import types
from unittest.mock import patch

from PyQt6.QtCore import QThread

import sayink.app as app_mod
import sayink.main as main_mod
from sayink.config import Config
from tests.helpers.app_harness import app_harness


class _BlockedNativeWorker(QThread):
    def __init__(self, gate: threading.Event):
        super().__init__()
        self._gate = gate

    def run(self) -> None:
        self._gate.wait(5.0)


def test_finish_quit_flushes_debounced_config_immediately_while_native_worker_drains(tmp_path):
    """Doubt 5B: Inno Setup ``taskkill /F`` runs while native workers may still be draining;
    ``_finish_quit`` must persist debounced config changes to disk immediately."""
    gate = threading.Event()
    worker = _BlockedNativeWorker(gate)
    worker.start()
    try:
        real_cfg = Config(config_dir=str(tmp_path))
        with app_harness() as h:
            app = h["app"]
            app._config = real_cfg
            h["recognizer"].running_workers = (worker,)

            # Debounced 500ms write not yet on disk.
            real_cfg.set("update.last_check_at", "2026-11-11T15:00:00Z")
            assert real_cfg._save_timer.isActive() is True

            events: list[str] = []
            h["history"].close.side_effect = lambda **_: events.append("history.close")

            with (
                patch("sayink.app.QTimer.singleShot") as later,
                patch("sayink.app.QApplication.quit") as quit_app,
            ):
                app._finish_quit()
                # Process has NOT quit yet because native worker is still running...
                quit_app.assert_not_called()
                assert later.called
                # ...but history is closed AND config.json is ALREADY flushed to disk!
                assert events == ["history.close"]
                assert real_cfg._save_timer.isActive() is False
                saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
                assert saved["update"]["last_check_at"] == "2026-11-11T15:00:00Z"

                gate.set()
                assert worker.wait(2000)
                later.call_args.args[1]()
                quit_app.assert_called_once()
    finally:
        gate.set()
        assert worker.wait(2000)


def test_early_lock_release_waits_for_storage_busy_then_fires_once_before_worker_drain():
    """Doubt 5A: Release single-instance lock only after ``storage_busy`` clears,
    but BEFORE waiting on read-only native C++ worker threads."""
    gate = threading.Event()
    worker = _BlockedNativeWorker(gate)
    worker.start()
    try:
        with app_harness() as h:
            app = h["app"]
            released: list[str] = []
            app._on_release_instance_lock = lambda: released.append("released")
            h["recorder"].storage_busy = True
            h["recognizer"].running_workers = (worker,)

            with (
                patch("sayink.app.QTimer.singleShot") as later,
                patch("sayink.app.QApplication.quit") as quit_app,
            ):
                app._finish_quit()
                # Still flushing audio-recovery journal -> lock must stay held!
                assert released == []
                quit_app.assert_not_called()

                # Audio-recovery finishes flushing, while native ONNX worker is still running.
                h["recorder"].storage_busy = False
                later.call_args.args[1]()
                assert released == ["released"]
                quit_app.assert_not_called()

                # Subsequent poll ticks while worker still runs must NOT double-release.
                later.call_args.args[1]()
                assert released == ["released"]

                gate.set()
                assert worker.wait(2000)
                later.call_args.args[1]()
                quit_app.assert_called_once()
                assert released == ["released"]
    finally:
        gate.set()
        assert worker.wait(2000)


def test_relaunch_during_dying_instance_native_drain_is_not_ghost_swallowed(
    _qapp_session, monkeypatch
):
    """End-to-end reproduction of Doubt 5A: relaunching SayInk while a quitting instance
    is still draining a native worker must acquire the mutex and bind the activation pipe
    instead of sending ``show`` into ``_exit_started`` oblivion."""
    monkeypatch.setattr(main_mod.sys, "platform", "win32")
    pipe_name = f"SayInk-activate-relaunch-{int(time.time() * 1000)}"
    monkeypatch.setattr(main_mod, "activation_server_name", lambda: pipe_name)
    monkeypatch.setattr(main_mod, "_win_mutex", None, raising=False)

    # Stateful simulated Windows Named Mutex table.
    held_handles: dict[int, str] = {}
    next_handle = {"v": 500}
    last_error = {"v": 0}

    def _create_mutex(_a, _b, name):
        hid = next_handle["v"]
        next_handle["v"] += 1
        already = name in held_handles.values()
        held_handles[hid] = name
        last_error["v"] = 183 if already else 0
        return hid

    def _close_handle(hid):
        held_handles.pop(hid, None)
        return True

    fake_kernel = types.SimpleNamespace(
        CreateMutexW=_create_mutex,
        OpenMutexW=lambda _acc, _inh, _name: 0,
        GetLastError=lambda: last_error["v"],
        CloseHandle=_close_handle,
    )
    monkeypatch.setattr(
        main_mod.ctypes,
        "windll",
        types.SimpleNamespace(kernel32=fake_kernel),
        raising=False,
    )

    gate = threading.Event()
    worker = _BlockedNativeWorker(gate)
    worker.start()
    server_1 = None
    server_2 = None
    try:
        assert main_mod.check_single_instance() is True
        with app_harness() as h:
            app_1 = h["app"]
            h["recognizer"].running_workers = (worker,)
            activated_1: list[str] = []
            server_1 = main_mod.start_activation_server(
                lambda: (activated_1.append("hit_1"), app_1._show_main_window())
            )
            assert server_1 is not None

            released_1 = False

            def _release_once():
                nonlocal server_1, released_1
                if released_1:
                    return
                released_1 = True
                srv, server_1 = server_1, None
                main_mod.release_instance_lock(srv)

            app_1._on_release_instance_lock = _release_once

            with (
                patch("sayink.app.QTimer.singleShot"),
                patch("sayink.app.QApplication.quit") as quit_app,
            ):
                # User quits Instance 1 while C++ worker is still running!
                app_1._finish_quit()
                quit_app.assert_not_called()
                assert worker.isRunning() is True

                # User immediately relaunches Instance 2 before worker finishes:
                # 1. Dying pipe is closed (cannot swallow activation);
                assert main_mod._activate_running_instance() is False
                assert activated_1 == []
                # 2. Instance 2 acquires the single-instance mutex cleanly!
                assert main_mod.check_single_instance() is True
                activated_2: list[str] = []
                server_2 = main_mod.start_activation_server(lambda: activated_2.append("hit_2"))
                assert server_2 is not None

                # Post-exec cleanup of Instance 1 is a true once-only no-op and
                # never disturbs Instance 2's newly acquired mutex or pipe!
                _release_once()
                assert main_mod._win_mutex is not None
                assert server_2.isListening() is True
    finally:
        gate.set()
        assert worker.wait(2000)
        if server_1 is not None:
            server_1.close()
        if server_2 is not None:
            server_2.close()
        main_mod.cleanup_lock()


def test_wedged_native_worker_hits_bounded_drain_timeout_and_exits_cleanly(caplog):
    gate = threading.Event()
    worker = _BlockedNativeWorker(gate)
    worker.start()
    try:
        with app_harness() as h:
            app = h["app"]
            app._quit_drain_timeout_s = 0.05
            h["recognizer"].running_workers = (worker,)

            with (
                caplog.at_level(logging.WARNING, logger="SayInk"),
                patch("sayink.app.QTimer.singleShot") as later,
                patch("sayink.app.QApplication.quit") as quit_app,
                patch("sayink.app._force_process_exit") as force_exit,
            ):
                app._finish_quit()
                # First tick sets deadline in the future and reschedules poll.
                quit_app.assert_not_called()
                force_exit.assert_not_called()
                assert later.called

                time.sleep(0.07)
                later.call_args.args[1]()

                quit_app.assert_called_once()
                force_exit.assert_called_once_with(0)
                assert any("退出等待后台工作线程超时" in r.message for r in caplog.records)
    finally:
        gate.set()
        assert worker.wait(2000)


def test_force_process_exit_flushes_handlers_and_calls_os_exit(monkeypatch):
    flushed = []

    class _DummyHandler(logging.Handler):
        def emit(self, record):
            pass

        def flush(self):
            flushed.append("flushed")

    exited = []
    monkeypatch.setattr(app_mod.os, "_exit", lambda code: exited.append(code))
    root = logging.getLogger()
    handler = _DummyHandler()
    root.addHandler(handler)
    try:
        app_mod._force_process_exit(0)
    finally:
        root.removeHandler(handler)

    assert "flushed" in flushed
    assert exited == [0]

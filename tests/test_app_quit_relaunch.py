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
    """Doubt 5B: When only read-only C++ recognizer workers are draining (no meeting workers),
    ``_finish_quit`` finalizes HistoryStore & debounced Config immediately before releasing
    the mutex, while ``QApplication.quit`` still waits for the C++ worker."""
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
            app._on_release_instance_lock = lambda: events.append("lock.released")

            with (
                patch("sayink.app.QTimer.singleShot") as later,
                patch("sayink.app.QApplication.quit") as quit_app,
            ):
                app._finish_quit()
                quit_app.assert_not_called()
                assert later.called
                # Debounced Config flushes immediately before lock release; HistoryStore stays
                # open until pending workers finish draining!
                assert events == ["lock.released"]
                assert real_cfg._save_timer.isActive() is False
                saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
                assert saved["update"]["last_check_at"] == "2026-11-11T15:00:00Z"

                gate.set()
                assert worker.wait(2000)
                later.call_args.args[1]()
                quit_app.assert_called_once()
                assert events == ["lock.released", "history.close"]
    finally:
        gate.set()
        assert worker.wait(2000)


def test_lock_release_holds_for_storage_and_meeting_workers_and_always_follows_persistence():
    """Active ``storage_busy`` or ``meeting.running_workers`` holds both HistoryStore and the
    single-instance mutex until drained, then closes HistoryStore strictly before releasing."""
    gate = threading.Event()
    worker = _BlockedNativeWorker(gate)
    worker.start()
    try:
        with app_harness() as h:
            app = h["app"]
            events: list[str] = []
            h["history"].close.side_effect = lambda **_: events.append("history.close")
            app._on_release_instance_lock = lambda: events.append("lock.released")
            h["recorder"].storage_busy = True
            fake_meeting = types.SimpleNamespace(
                shutdown=lambda **_: None,
                running_workers=(worker,),
            )
            app._meeting = fake_meeting  # type: ignore[assignment]

            with (
                patch("sayink.app.QTimer.singleShot") as later,
                patch("sayink.app.QApplication.quit") as quit_app,
            ):
                app._finish_quit()
                # Still flushing audio-recovery journal -> lock must stay held!
                assert events == []
                quit_app.assert_not_called()

                # Audio-recovery finishes flushing, while meeting worker is still running ->
                # both persistence and mutex stay held so checkpoints can still write!
                h["recorder"].storage_busy = False
                later.call_args.args[1]()
                assert events == []
                quit_app.assert_not_called()

                gate.set()
                assert worker.wait(2000)
                fake_meeting.running_workers = ()
                later.call_args.args[1]()
                quit_app.assert_called_once()
                assert events == ["history.close", "lock.released"]
    finally:
        gate.set()
        assert worker.wait(2000)


def test_relaunch_during_dying_instance_native_drain_finalizes_persistence_before_releasing(
    _qapp_session, monkeypatch
):
    """Relaunching SayInk while a quitting instance drains a read-only C++ recognizer worker:
    Instance 1 closes HistoryStore & flushes Config FIRST, then releases mutex + pipe so
    Instance 2 starts cleanly without split-brain or ghost-swallowed activation."""
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
            order: list[str] = []
            h["history"].close.side_effect = lambda **_: order.append("history.close")
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
                order.append("lock.released")
                srv, server_1 = server_1, None
                main_mod.release_instance_lock(srv)

            app_1._on_release_instance_lock = _release_once

            with (
                patch("sayink.app.QTimer.singleShot") as later,
                patch("sayink.app.QApplication.quit") as quit_app,
            ):
                app_1._finish_quit()
                quit_app.assert_not_called()
                assert worker.isRunning() is True
                assert order == ["lock.released"]

                assert main_mod._activate_running_instance() is False
                assert activated_1 == []
                assert main_mod.check_single_instance() is True
                activated_2: list[str] = []
                server_2 = main_mod.start_activation_server(lambda: activated_2.append("hit_2"))
                assert server_2 is not None

                gate.set()
                assert worker.wait(2000)
                later.call_args.args[1]()
                quit_app.assert_called_once()
                assert order == ["lock.released", "history.close"]

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


def test_wake_pipe_quit_command_and_session_commit_flush_persistence(
    _qapp_session, tmp_path, monkeypatch
):
    pipe_name = f"SayInk-quit-pipe-{int(time.time() * 1000)}"
    monkeypatch.setattr(main_mod, "activation_server_name", lambda: pipe_name)
    calls: list[str] = []
    server = main_mod.start_activation_server(
        lambda: calls.append("show"),
        on_quit=lambda: calls.append("quit"),
    )
    assert server is not None
    try:
        from PyQt6.QtCore import QDeadlineTimer

        res: dict[str, bool] = {}
        t = threading.Thread(target=lambda: res.setdefault("ok", main_mod._quit_running_instance()))
        t.start()
        deadline = QDeadlineTimer(3000)
        while t.is_alive() and not deadline.hasExpired():
            _qapp_session.processEvents()
        t.join(1)
        for _ in range(20):
            _qapp_session.processEvents()
        assert res.get("ok") is True
        assert calls == ["quit"]
    finally:
        server.close()

    # Clean config skips disk rewrite on commit_session_data; dirty config flushes immediately
    cfg = Config(config_dir=str(tmp_path / "cfg"))
    cfg.save_immediate()
    assert cfg.is_dirty is False
    with app_harness() as h:
        app = h["app"]
        app._config = cfg
        with patch.object(cfg, "save", wraps=cfg.save) as save_spy:
            app.commit_session_data()
            save_spy.assert_not_called()

    cfg2 = Config(config_dir=str(tmp_path / "cfg2"))
    cfg2.set("sound_enabled", False)
    assert cfg2.is_dirty is True
    with app_harness() as h:
        app = h["app"]
        app._config = cfg2
        released: list[int] = []
        app._on_release_instance_lock = lambda: released.append(1)
        app.commit_session_data()
        assert cfg2.is_dirty is False
        assert released == [1]
        h["history"].close.assert_called_once()


def test_confirm_exit_history_inline_edit_and_meeting_audio_retention_and_recovery_copy(tmp_path):
    from unittest.mock import MagicMock
    from sayink.audio_recorder import AudioRecorder

    rec = AudioRecorder()
    rec.set_recovery_keep_completed(True)
    rec.set_recovery_directory(tmp_path / "audio-recovery")
    assert rec._recovery_keep_completed is True
    rec.set_recovery_directory(None)
    assert rec._recovery_keep_completed is False

    with app_harness() as h:
        app = h["app"]
        fake_main = MagicMock()
        fake_main.isVisible.return_value = True
        fake_main.meeting_panel.unsaved_exit_draft.return_value = None
        fake_main.history_panel.has_unsaved_inline_edit.return_value = True
        fake_main.history_panel.try_leave.return_value = True
        app._main = fake_main
        assert app._confirm_exit() is True
        fake_main.history_panel.try_leave.assert_called_once()

        # Startup interrupted-recording prompt points to 会议默认设置 -> 查看 / 恢复未完成录音
        fake_item = types.SimpleNamespace(metadata={"complete": False})
        captured_notices: list[list[str]] = []
        with (
            patch("sayink.audio_recovery.recordings", return_value=[fake_item]),
            patch.object(app, "_show_history_startup_notice", side_effect=captured_notices.append),
            patch("sayink.app.QTimer.singleShot"),
        ):
            app.start(show_main=False)
        assert captured_notices == [
            ["发现 1 段可恢复录音。请到通用 → 会议默认设置 → 查看 / 恢复未完成录音。"]
        ]

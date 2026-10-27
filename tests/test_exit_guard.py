"""Exit and update must make unfinished transcription loss explicit."""

from unittest.mock import patch

import numpy as np
import pytest

from tests.helpers.app_harness import app_harness


@pytest.mark.parametrize("pending", ["recording", "listening", "asr", "polish", "queue"])
def test_return_from_exit_preserves_pending_work(pending):
    with app_harness() as h:
        app = h["app"]
        if pending == "recording":
            h["recorder"].is_recording = True
        elif pending == "listening":
            h["recorder"].is_continuous = True
        elif pending == "asr":
            app._is_transcribing = True
        elif pending == "polish":
            app._output_busy = True
        else:
            app._enqueue_audio(np.ones(1600, dtype=np.float32))
        before = list(app._segment_queue)
        with patch("sayink.app.QMessageBox") as boxes, patch("sayink.app.QApplication.quit") as quit_app:
            dialog = boxes.return_value
            dialog.addButton.side_effect = ["return", "discard"]
            dialog.clickedButton.return_value = "return"
            app._quit()
            dialog.exec.assert_called_once()
            dialog.setDefaultButton.assert_called_once_with("return")
            quit_app.assert_not_called()
        h["hotkey"].stop.assert_not_called()
        h["recorder"].stop_continuous.assert_not_called()
        h["recorder"].cancel.assert_not_called()
        h["recognizer"].shutdown.assert_not_called()
        h["polisher"].cancel.assert_not_called()
        h["history"].close.assert_not_called()
        assert app._segment_queue == before


def test_explicit_discard_exits_once():
    with app_harness() as h:
        h["app"]._is_transcribing = True
        with patch("sayink.app.QMessageBox") as boxes, patch("sayink.app.QApplication.quit") as quit_app:
            dialog = boxes.return_value
            dialog.addButton.side_effect = ["return", "discard"]
            dialog.clickedButton.return_value = "discard"
            h["app"]._quit()
            dialog.exec.assert_called_once()
            quit_app.assert_called_once()
        h["recognizer"].shutdown.assert_called_once()
        h["history"].close.assert_called_once()


def test_idle_exit_needs_no_confirmation():
    with app_harness() as h:
        with patch("sayink.app.QMessageBox") as boxes, patch("sayink.app.QApplication.quit") as quit_app:
            h["app"]._quit()
            boxes.assert_not_called()
            quit_app.assert_called_once()


def test_finish_quit_cancels_a_paste_still_waiting_to_send():
    """Paste and clipboard-restore timers are not children of the app.

    Stopping the app's own timers is not enough: a Ctrl+V already queued
    inside TextPaster must be dropped before the process goes away.
    """
    with app_harness() as h:
        with patch("sayink.app.QApplication.quit"):
            h["app"]._finish_quit()
        h["paster"].cancel.assert_called_once()


def test_exit_waits_for_native_worker_without_blocking_the_event_loop():
    import threading
    from PyQt6.QtCore import QThread

    release = threading.Event()

    class Worker(QThread):
        def run(self):
            release.wait(5)

    worker = Worker()
    worker.start()
    try:
        with app_harness() as h:
            h["recognizer"].running_workers = (worker,)
            with patch("sayink.app.QTimer.singleShot") as later, patch("sayink.app.QApplication.quit") as quit_app:
                h["app"]._finish_quit()
                quit_app.assert_not_called()
                callback = later.call_args.args[1]
                release.set()
                assert worker.wait(2000)
                callback()
                quit_app.assert_called_once()
    finally:
        release.set()
        assert worker.wait(2000)


def test_closing_confirmation_preserves_pending_work():
    with app_harness() as h:
        h["app"]._is_transcribing = True
        with patch("sayink.app.QMessageBox") as boxes, patch("sayink.app.QApplication.quit") as quit_app:
            dialog = boxes.return_value
            dialog.addButton.side_effect = ["return", "discard"]
            dialog.clickedButton.return_value = None
            h["app"]._quit()
            dialog.setEscapeButton.assert_called_once_with("return")
            quit_app.assert_not_called()
        h["recognizer"].shutdown.assert_not_called()


def test_late_callbacks_after_exit_cannot_start_work_or_write_to_closed_history():
    with app_harness() as h:
        app = h["app"]
        app._exit_started = True
        h["history"].reset_mock()
        h["recognizer"].reset_mock()
        h["paster"].reset_mock()
        h["recorder"].reset_mock()
        h["floating"].reset_mock()
        app._on_recording_start()
        app._start_continuous_listening()
        app._on_recorder_error("late error")
        app._on_recorder_warning("late warning")
        app._enqueue_audio(np.ones(1600, dtype=np.float32))
        app._on_recording_finished(np.ones(1600, dtype=np.float32))
        app._on_partial_result("late partial")
        app._on_final_result("late result")
        app._output_text("late output")
        app._handle_paste_result("sent")
        assert not app._segment_queue
        assert not h["recognizer"].mock_calls
        assert not h["paster"].mock_calls
        assert not h["history"].mock_calls
        assert not h["recorder"].mock_calls
        assert not h["floating"].mock_calls


def test_reentrant_exit_does_not_open_another_confirmation():
    with app_harness() as h:
        app = h["app"]
        app._is_transcribing = True
        with patch("sayink.app.QMessageBox") as boxes, patch("sayink.app.QApplication.quit") as quit_app:
            dialog = boxes.return_value
            dialog.addButton.side_effect = ["return", "discard"]
            dialog.clickedButton.return_value = "return"
            dialog.exec.side_effect = app._quit
            app._quit()
            boxes.assert_called_once()
            quit_app.assert_not_called()


def test_native_confirmation_defaults_to_return():
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication

    observed = []

    def close_dialog():
        dialog = QApplication.activeModalWidget()
        if dialog is not None:
            observed.append((dialog.defaultButton().text(), dialog.escapeButton().text()))
            dialog.reject()

    with app_harness() as h:
        h["app"]._is_transcribing = True
        QTimer.singleShot(0, close_dialog)
        assert h["app"]._confirm_exit() is False
        assert observed == [("返回继续处理", "返回继续处理")]
        assert h["app"]._is_transcribing is True


def test_update_does_not_launch_installer_when_exit_is_cancelled():
    with app_harness() as h:
        h["app"]._output_busy = True
        with patch("sayink.app.QMessageBox") as boxes, patch("sayink.updater.launch_installer") as launch:
            dialog = boxes.return_value
            dialog.addButton.side_effect = ["return", "discard"]
            dialog.clickedButton.return_value = "return"
            h["app"]._updates.on_downloaded("downloaded.exe")
            launch.assert_not_called()
        h["recognizer"].shutdown.assert_not_called()
        h["history"].close.assert_not_called()


def test_update_confirms_discard_before_launch_and_does_not_confirm_twice():
    events = []
    with app_harness() as h:
        h["app"]._is_transcribing = True
        with patch("sayink.app.QMessageBox") as boxes, patch("sayink.updater.launch_installer") as launch, patch("sayink.app.QApplication.quit") as quit_app:
            dialog = boxes.return_value
            dialog.addButton.side_effect = ["return", "discard"]
            dialog.clickedButton.return_value = "discard"
            dialog.exec.side_effect = lambda: events.append("confirm")
            launch.side_effect = lambda _path: events.append("launch")
            quit_app.side_effect = lambda: events.append("quit")
            h["app"]._updates.on_downloaded("downloaded.exe")
            assert events == ["confirm", "launch", "quit"]
            dialog.exec.assert_called_once()


def test_installer_launch_failure_keeps_app_running():
    with app_harness() as h:
        with patch("sayink.updater.launch_installer", side_effect=OSError("cannot launch")), patch("sayink.app.QApplication.quit") as quit_app:
            h["app"]._updates.on_downloaded("downloaded.exe")
            quit_app.assert_not_called()
        h["recognizer"].shutdown.assert_not_called()

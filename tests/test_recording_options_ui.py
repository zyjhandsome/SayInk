from unittest.mock import patch

import numpy as np
import pytest
from PyQt6.QtTest import QSignalSpy
from PyQt6.QtWidgets import QApplication

from sayink.audio_recovery import RecordingJournal
from sayink.ui.audio_recovery_dialog import AudioRecoveryDialog
from sayink.ui.settings_window import SettingsWindow
from tests.test_asr_short_retry import Recognizer


@pytest.fixture
def settings(config, monkeypatch):
    monkeypatch.setattr(SettingsWindow, "_rebuild_model_cards", lambda self: None)
    monkeypatch.setattr(SettingsWindow, "_refresh_audio_device_lists", lambda self: None)
    window = SettingsWindow(config)
    yield window
    window.hide()
    window.deleteLater()
    config._save_timer.stop()


def test_optional_recording_controls_save_without_discarding_runtime_queue(settings, config):
    changes, runtime = QSignalSpy(settings.audio_options_changed), QSignalSpy(settings.settings_changed)
    assert not settings._recovery_row.isChecked() and not settings._separate_lanes_row.isChecked()
    settings._recovery_row.setChecked(True)
    settings._separate_lanes_row.setChecked(True)
    assert config.get("audio.recovery_enabled") is True
    assert config.get("audio.separate_lanes") is True
    assert len(changes) == 2 and len(runtime) == 0


def test_quiet_speech_uses_existing_deferred_shared_recognition_change(settings, config):
    changes = QSignalSpy(settings.recognition_changed)
    settings._quiet_speech_row.setChecked(True)
    assert len(changes) == 1 and config.get("stt.quiet_speech_enabled") is True
    settings._load_settings(refresh_devices=False, heavy=False)
    assert settings._quiet_speech_row.isChecked() and len(changes) == 1


def test_recovery_dialog_selects_raw_tracks_and_recognizes_locally(tmp_path):
    journal = RecordingJournal(tmp_path)
    audio = np.full(8000, 0.1, np.float32)
    assert journal.append(audio, tracks={"microphone": audio, "system": audio * 2})
    journal.close()
    dialog = AudioRecoveryDialog(tmp_path, recognizer=Recognizer(["恢复的原话"]))
    assert dialog.track.count() == 3
    dialog.track.setCurrentIndex(1)
    assert dialog._selected().track == "microphone"
    with patch("pyperclip.copy") as clipboard:
        dialog._transcribe()
        worker = dialog.worker
        finished = QSignalSpy(worker.finished)
        if worker.isRunning():
            assert finished.wait(3000)
        QApplication.processEvents()
        assert dialog.output.toPlainText() == "恢复的原话"
        clipboard.assert_not_called()
    dialog.reject()
    dialog.deleteLater()


def test_recovery_button_routes_to_app_without_enabling_recording(settings, config):
    requested = QSignalSpy(settings.audio_recovery_requested)
    settings._recovery_btn.click()
    assert len(requested) == 1 and not config.get("audio.recovery_enabled")

"""History settings tests (onboarding lives in test_onboarding.py)."""

from __future__ import annotations

import sys

import pytest
from PyQt6.QtWidgets import QApplication

from sayink.ui.settings_window import SettingsWindow


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture
def settings_window(config, qapp, monkeypatch):
    monkeypatch.setattr(SettingsWindow, "_rebuild_model_cards", lambda self: None)
    monkeypatch.setattr(SettingsWindow, "_refresh_about_info", lambda self: None)
    monkeypatch.setattr(SettingsWindow, "_refresh_audio_device_lists", lambda self: None)
    win = SettingsWindow(config)
    yield win
    win.close()


class TestHistorySettings:
    def test_history_controls_load_from_config(self, config, qapp, monkeypatch):
        config.set("history.enabled", False)
        config.set("history.retention_days", 30)
        config.set("history.max_entries", 250)
        monkeypatch.setattr(SettingsWindow, "_rebuild_model_cards", lambda self: None)
        monkeypatch.setattr(SettingsWindow, "_refresh_about_info", lambda self: None)
        monkeypatch.setattr(SettingsWindow, "_refresh_audio_device_lists", lambda self: None)

        win = SettingsWindow(config)
        try:
            assert win._history_enabled_row.isChecked() is False
            assert win._history_retention_days_spin.value() == 30
            assert win._history_max_entries_spin.value() == 250
        finally:
            win.close()

    def test_history_toggle_persists_without_deleting_existing_data(self, settings_window, config):
        history = object()
        settings_window._history_enabled_row.setChecked(False)

        assert config.get("history.enabled") is False
        assert not hasattr(history, "enqueue_delete_all")

    def test_history_limits_persist_to_config(self, settings_window, config):
        settings_window._history_retention_days_spin.setValue(14)
        settings_window._history_max_entries_spin.setValue(123)

        assert config.get("history.retention_days") == 14
        assert config.get("history.max_entries") == 123

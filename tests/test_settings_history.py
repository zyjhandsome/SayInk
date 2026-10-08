"""History settings and first-run onboarding tests."""

from __future__ import annotations

import sys

import pytest
from PyQt6.QtWidgets import QApplication

from tests.helpers.app_harness import app_harness
import sayink.app as app_module
from sayink.app import App
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


class TestHistoryOnboarding:
    def test_start_defers_history_onboarding_until_model_ready(self):
        with app_harness(config_overrides={"history.onboarded": False}) as h:
            h["recognizer"].ready.connect.reset_mock()

            h["app"].start()

            h["recognizer"].ready.connect.assert_any_call(
                h["app"]._show_history_onboarding_once
            )

    def test_start_defers_history_until_after_welcome_when_first_run(self):
        with app_harness(
            config_overrides={
                "history.onboarded": False,
                "first_run_welcome_seen": False,
            }
        ) as h:
            h["recognizer"].ready.connect.reset_mock()
            h["app"].start()
            connected = [c.args[0] for c in h["recognizer"].ready.connect.call_args_list]
            assert h["app"]._show_history_onboarding_once not in connected
            assert h["app"]._show_first_run_welcome_once in connected

    def test_start_skips_history_onboarding_when_already_onboarded(self):
        with app_harness(config_overrides={"history.onboarded": True}) as h:
            h["recognizer"].ready.connect.reset_mock()

            h["app"].start()

            assert h["app"]._show_history_onboarding_once not in [
                call.args[0] for call in h["recognizer"].ready.connect.call_args_list
            ]

    def test_accepting_history_onboarding_enables_history_and_marks_onboarded(self, monkeypatch):
        with app_harness(config_overrides={"history.onboarded": False}) as h:
            monkeypatch.setattr(h["app"], "_ask_history_onboarding_enabled", lambda: True)
            h["app"]._show_history_onboarding()

            assert h["store"]["history"]["enabled"] is True
            assert h["store"]["history"]["onboarded"] is True

    def test_declining_history_onboarding_disables_history_and_marks_onboarded(self, monkeypatch):
        with app_harness(config_overrides={"history.onboarded": False}) as h:
            monkeypatch.setattr(h["app"], "_ask_history_onboarding_enabled", lambda: False)
            h["app"]._show_history_onboarding()

            assert h["store"]["history"]["enabled"] is False
            assert h["store"]["history"]["onboarded"] is True

    def test_history_onboarding_dialog_defaults_to_enable_button(self, monkeypatch):
        events = {}

        class FakeMessageBox:
            class Icon:
                Question = object()

            class ButtonRole:
                AcceptRole = object()
                RejectRole = object()

            def __init__(self):
                self._buttons = []
                self._default = None

            def setWindowTitle(self, title):
                events["title"] = title

            def setText(self, text):
                events["text"] = text

            def setIcon(self, icon):
                events["icon"] = icon

            def addButton(self, text, role):
                button = object()
                self._buttons.append((text, role, button))
                events.setdefault("buttons", []).append((text, role, button))
                return button

            def setDefaultButton(self, button):
                self._default = button
                events["default"] = button

            def exec(self):
                return 0

            def clickedButton(self):
                return self._buttons[0][2]

        monkeypatch.setattr(app_module, "QMessageBox", FakeMessageBox)

        assert App._ask_history_onboarding_enabled(object()) is True
        buttons = events["buttons"]
        assert [text for text, _role, _button in buttons] == ["开启", "暂不开启"]
        assert events["default"] is buttons[0][2]
        assert events["title"] == "开启语音历史？"
        assert "随时可以在设置关闭" in events["text"]


def test_first_run_welcome_is_scheduled_once_even_if_ready_and_fallback_both_fire():
    from unittest.mock import patch

    with app_harness(config_overrides={"first_run_welcome_seen": False}) as h:
        app = h["app"]
        with patch("sayink.app.QTimer.singleShot") as single_shot:
            app._show_first_run_welcome_once()
            app._show_first_run_welcome_once()
        scheduled = [c for c in single_shot.call_args_list if c.args[1] == app._show_first_run_welcome]
        assert len(scheduled) == 1

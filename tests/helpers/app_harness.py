"""Shared mocks for App integration tests (README feature flows)."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from sayink.config import DEFAULT_CONFIG, Config


def _config_get(store: dict, key: str, default=None):
    keys = key.split(".")
    value = store
    for k in keys:
        if isinstance(value, dict) and k in value:
            value = value[k]
        else:
            return default
    return value


def _config_set(store: dict, key: str, value):
    keys = key.split(".")
    node = store
    for k in keys[:-1]:
        if k not in node or not isinstance(node[k], dict):
            node[k] = {}
        node = node[k]
    node[keys[-1]] = value


@contextmanager
def app_harness(config_overrides: dict | None = None):
    """Build a real App with mocked heavy dependencies and configurable store."""
    store = deepcopy(DEFAULT_CONFIG)
    if config_overrides:
        for key, value in config_overrides.items():
            _config_set(store, key, value)

    config_mock = MagicMock()
    config_mock.get.side_effect = lambda key, default=None: _config_get(store, key, default)
    config_mock.set.side_effect = lambda key, value: _config_set(store, key, value)
    config_mock.get_all.return_value = store
    config_mock.models_dir = store.get("stt", {}).get("models_dir") or None
    # Output-mode helpers are real logic on Config; run them against the
    # store so tests see the same rules as the app.
    config_mock.output_mode.side_effect = lambda: Config.output_mode(config_mock)
    config_mock.history_only_output.side_effect = lambda: Config.history_only_output(config_mock)
    config_mock.set_output_mode.side_effect = (
        lambda mode, auto=False: Config.set_output_mode(config_mock, mode, auto=auto)
    )
    config_mock.follow_input_source_for_output.side_effect = (
        lambda source: Config.follow_input_source_for_output(config_mock, source)
    )
    with TemporaryDirectory() as temp_dir:
        config_mock.config_dir = Path(temp_dir)

        patches = [
            patch("sayink.app.Config", return_value=config_mock),
            patch("sayink.app.HotKeyManager"),
            patch("sayink.app.AudioRecorder"),
            patch("sayink.app.SpeechRecognizer"),
            patch("sayink.app.TextPolisher"),
            patch("sayink.app.TextPaster"),
            patch("sayink.app.SoundManager"),
            patch("sayink.app.FloatingWindow"),
            patch("sayink.app.TrayIcon"),
            patch("sayink.app.HistoryStore", create=True),
            # App.__init__ probes the models directory on disk; without this the
            # suite's outcome depends on whether the machine has the model
            # downloaded (startup show_error fires on a clean checkout).
            patch("sayink.speech_recognizer.is_model_downloaded", return_value=True),
        ]
        started = [p.start() for p in patches]

        from sayink.app import App

        app = App()
        # start() schedules these with QTimer; if they fired during a later
        # test's processEvents, a modal dialog or a real network check would
        # block the suite.
        app._show_first_run_welcome = lambda: None
        app._ask_history_onboarding_enabled = lambda: False
        app._updates.maybe_auto_check = lambda: None
        harness = {
            "app": app,
            "config": config_mock,
            "store": store,
            "hotkey": app._hotkey_mgr,
            "recorder": app._recorder,
            "recognizer": app._recognizer,
            "polisher": app._polisher,
            "paster": app._paster,
            "sound": app._sound,
            "floating": app._floating,
            "tray": app._tray,
            "history": getattr(app, "_history", started[-1].return_value),
        }

        # Sensible defaults for README flows
        harness["recognizer"].is_ready = True
        harness["recognizer"].is_loading = False
        harness["recorder"].is_recording = False
        harness["recorder"].is_continuous = False
        harness["recorder"].input_source = _config_get(store, "audio.input_source", "microphone")
        harness["recorder"].input_source_display = "麦克风"

        try:
            yield harness
        finally:
            for p in reversed(patches):
                p.stop()

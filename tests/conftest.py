"""Shared pytest fixtures."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

from sayink.audio_devices import AudioDeviceInfo, RecordingPlan, StreamEndpoint
from sayink.audio_recorder import AudioRecorder
from sayink.config import Config


@pytest.fixture(scope="session", autouse=True)
def _qapp_session():
    """Keep a single QApplication alive for the whole test session.

    Prevents ``wrapped C/C++ object ... has been deleted`` flakiness when Qt
    objects (HotKeyManager, SettingsWindow, ...) are created across modules
    without a persistent application instance.
    """
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture(autouse=True)
def _restore_light_design_tokens():
    """Keep design-token module aliases on the light axis between tests."""
    yield
    try:
        from sayink.ui import design_tokens as dt
        from sayink.ui import settings_styles as ss

        dt.activate("light")
        ss.reload_styles()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _isolate_registry_auto_start(monkeypatch):
    """Stop tests from reading the real machine's Windows ``Run`` key.

    Without this, a developer machine that has SayInk registered for
    auto-start would leak ``auto_start=True`` into every isolated Config,
    making toggle/signal assertions non-deterministic. We simulate an empty
    ``Run`` key so the real sync code path still runs (and stays covered).
    """
    if sys.platform == "win32":
        import winreg

        def _no_entry(*_args, **_kwargs):
            raise FileNotFoundError

        monkeypatch.setattr(winreg, "QueryValueEx", _no_entry)


@pytest.fixture(autouse=True)
def _no_real_credential_manager(monkeypatch):
    """Never read or write the developer's real Windows credentials."""
    monkeypatch.setattr("sayink.config.default_secret_store", lambda: None)


@pytest.fixture
def config_home(tmp_path):
    """Isolated ~/.sayink directory for config tests."""
    home = tmp_path / ".sayink"
    home.mkdir(parents=True)
    return home


@pytest.fixture
def config(config_home):
    """Config instance backed by a temporary directory."""
    return Config(config_dir=config_home)


@pytest.fixture
def mock_recording_hardware(monkeypatch):
    """Avoid opening real microphones during AudioRecorder.start() tests."""
    device = AudioDeviceInfo(0, "Mock Mic", "mock", 16000, 1, False, False)
    endpoint = StreamEndpoint("microphone", device, False)
    plan = RecordingPlan(input_source="microphone", endpoints=(endpoint,))

    def fake_build_recording_plan(*_args, **_kwargs):
        return plan

    def fake_open_lane_stream(self, lane):
        lane.stream = MagicMock()
        lane.stream.close = MagicMock()
        lane.sample_rate = 16000

    monkeypatch.setattr(
        "sayink.audio_recorder.build_recording_plan",
        fake_build_recording_plan,
    )
    monkeypatch.setattr(AudioRecorder, "_open_lane_stream", fake_open_lane_stream)

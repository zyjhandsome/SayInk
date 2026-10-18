"""Shared pytest fixtures."""

from __future__ import annotations

import locale
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


def _point_home_at(mp: pytest.MonkeyPatch, home) -> None:
    """Point ``~`` and ``%APPDATA%`` at an empty folder.

    Otherwise Config(), user_data_dir() and the models directory resolve to
    the developer's real ``~/.sayink``: tests read real settings and
    downloaded models (green locally, red in CI) and can rewrite them.
    Callers use their own MonkeyPatch so a test's ``monkeypatch.undo()``
    cannot drop the isolation.
    """
    mp.setenv("USERPROFILE", str(home))
    mp.setenv("HOME", str(home))
    mp.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    mp.setattr("sayink.user_data._resolved", {})
    mp.setattr("sayink.speech_recognizer._custom_models_dir", None)


@pytest.fixture(scope="session", autouse=True)
def _isolate_user_home_for_shared_fixtures(tmp_path_factory):
    """Module-scoped fixtures are built before any function-scoped one."""
    with pytest.MonkeyPatch.context() as mp:
        _point_home_at(mp, tmp_path_factory.mktemp("session_home"))
        yield


@pytest.fixture(autouse=True)
def _isolate_user_home(tmp_path_factory):
    with pytest.MonkeyPatch.context() as mp:
        _point_home_at(mp, tmp_path_factory.mktemp("home"))
        yield


@pytest.fixture(autouse=True)
def _no_real_credential_manager(monkeypatch):
    """Never read or write the developer's real Windows credentials."""
    monkeypatch.setattr("sayink.config.default_secret_store", lambda: None)


_ANSI_LOCALES = ("English_United States.1252", "en_US.ISO8859-1")


def _switch_to_ansi_locale() -> bool:
    """Set LC_CTYPE to an English Windows code page, as on the CI runner.

    On Windows ``time.strftime`` encodes its format string with this code
    page, so Chinese text in a format only breaks on non-Chinese systems
    and never on a Chinese development machine.
    """
    for name in _ANSI_LOCALES:
        try:
            locale.setlocale(locale.LC_CTYPE, name)
            return True
        except locale.Error:
            continue
    return False


@pytest.fixture(scope="session", autouse=True)
def _ansi_code_page_like_ci():
    previous = locale.setlocale(locale.LC_CTYPE)
    _switch_to_ansi_locale()
    yield
    locale.setlocale(locale.LC_CTYPE, previous)


@pytest.fixture
def ansi_locale():
    """Pin the code page for tests that exist to guard against it."""
    previous = locale.setlocale(locale.LC_CTYPE)
    if not _switch_to_ansi_locale():
        pytest.skip("no ANSI code page locale available")
    yield
    locale.setlocale(locale.LC_CTYPE, previous)


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

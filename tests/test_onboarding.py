"""First-run welcome and history opt-in (OnboardingController) + App wiring."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import Qt

import sayink.onboarding as onboarding_module
from sayink.onboarding import OnboardingController
from tests.helpers.app_harness import app_harness


class _Config:
    def __init__(self, **values):
        self.values = dict(values)

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value


class _Box:
    """Minimal QMessageBox stand-in: records calls, clicks the button named ``click``."""

    class Icon:
        Information = object()
        Question = object()

    class ButtonRole:
        AcceptRole = object()
        RejectRole = object()
        ActionRole = object()

    click: str | None = None
    last = None

    def __init__(self, parent=None):
        self.parent = parent
        self.buttons = []
        self.text = ""
        self.title = ""
        self.default = None
        self.flags = []
        _Box.last = self

    def setWindowFlag(self, flag, on=True):
        self.flags.append((flag, on))

    def setWindowTitle(self, title):
        self.title = title

    def setText(self, text):
        self.text = text

    def setIcon(self, icon):
        pass

    def addButton(self, text, role):
        button = (text, role)
        self.buttons.append(button)
        return button

    def setDefaultButton(self, button):
        self.default = button

    def exec(self):
        return 0

    def clickedButton(self):
        for button in self.buttons:
            if button[0] == _Box.click:
                return button
        return self.buttons[-1]


@pytest.fixture
def box(monkeypatch):
    monkeypatch.setattr(onboarding_module, "QMessageBox", _Box)
    _Box.click = None
    _Box.last = None
    return _Box


def _controller(config, *, continuous=False, model_installed=True, parent_widget=None):
    opened = []
    ctl = OnboardingController(
        config,
        parent=None,
        dialog_parent=lambda: parent_widget,
        is_continuous_mode=lambda: continuous,
        show_main_window=opened.append,
        model_installed=lambda: model_installed,
    )
    ctl.opened = opened
    return ctl


class TestWelcome:
    def test_offers_model_download_when_none_is_installed(self, box):
        """P-04: the lite installer ships without a model; the welcome box must lead there."""
        box.click = "去下载模型"
        cfg = _Config(first_run_welcome_seen=False)
        cfg.set("history.onboarded", True)
        ctl = _controller(cfg, model_installed=False)

        ctl.show_welcome()

        assert [t for t, _ in box.last.buttons] == ["去下载模型", "知道了"]
        assert "本机还没有语音模型" in box.last.text
        assert ctl.opened == ["engine"]
        assert cfg.get("first_run_welcome_seen") is True

    def test_has_no_download_button_when_a_model_is_installed(self, box):
        cfg = _Config(first_run_welcome_seen=False)
        cfg.set("history.onboarded", True)
        ctl = _controller(cfg, model_installed=True)

        ctl.show_welcome()

        assert [t for t, _ in box.last.buttons] == ["知道了"]
        assert "语音模型已下载" in box.last.text
        assert ctl.opened == []

    def test_missing_model_text_quotes_the_registry_size(self):
        from sayink.speech_recognizer import DEFAULT_MODEL_ID, get_model_info

        info = get_model_info(DEFAULT_MODEL_ID)
        text = _controller(_Config()).welcome_text(model_missing=True)
        assert f"{info['name']}（约 {info['size_mb']} MB，仅需一次）" in text

    def test_text_follows_trigger_mode(self):
        cfg = _Config(hotkey="alt+x")
        assert "单次转写" in _controller(cfg).welcome_text(model_missing=False)
        assert "持续转写" in _controller(cfg, continuous=True).welcome_text(model_missing=False)

    def test_text_follows_source_output_and_escape_preference(self):
        cfg = _Config(hotkey="ctrl+shift+z")
        cfg.set("audio.input_source", "system")
        cfg.set("output.mode", "history")
        cfg.set("history.enabled", False)
        text = _controller(cfg).welcome_text(model_missing=False)
        assert "单次转写" in text
        assert "结果当前不会保存" in text
        assert "偏好" not in text
        assert "Alt+X" not in text
        cfg.set("audio.esc_stops_continuous", False)
        cfg.set("history.enabled", True)
        text = _controller(cfg, continuous=True).welcome_text(model_missing=False)
        assert "结果仅保存到「历史」" in text
        assert "Esc" not in text
        assert "松开后继续录制" in text

    def test_is_scheduled_once_even_if_ready_and_fallback_both_fire(self):
        cfg = _Config(first_run_welcome_seen=False)
        ctl = _controller(cfg)
        ready = MagicMock()
        with patch("sayink.onboarding.QTimer.singleShot") as single_shot:
            ctl.schedule(ready)
            ctl.show_welcome_once()
            ctl.show_welcome_once()
        ready.connect.assert_called_once_with(ctl.show_welcome_once)
        ready.disconnect.assert_called_once_with(ctl.show_welcome_once)
        scheduled = [c for c in single_shot.call_args_list if c.args[1] == ctl.show_welcome]
        assert len(scheduled) == 1

    def test_history_question_follows_the_welcome(self, box):
        cfg = _Config(first_run_welcome_seen=False)
        cfg.set("history.onboarded", False)
        ctl = _controller(cfg)
        with patch("sayink.onboarding.QTimer.singleShot") as single_shot:
            ctl.show_welcome()
        assert [c.args[1] for c in single_shot.call_args_list] == [ctl.show_history_onboarding]


class TestHistoryOnboarding:
    def test_schedule_waits_for_model_ready_when_welcome_already_seen(self):
        cfg = _Config(first_run_welcome_seen=True)
        cfg.set("history.onboarded", False)
        ctl = _controller(cfg)
        ready = MagicMock()
        ctl.schedule(ready)
        ready.connect.assert_called_once_with(ctl.show_history_onboarding_once)

    def test_schedule_does_nothing_when_already_onboarded(self):
        cfg = _Config(first_run_welcome_seen=True)
        cfg.set("history.onboarded", True)
        ready = MagicMock()
        _controller(cfg).schedule(ready)
        ready.connect.assert_not_called()

    @pytest.mark.parametrize("answer", [True, False])
    def test_answer_sets_enabled_and_marks_onboarded(self, answer):
        cfg = _Config()
        cfg.set("history.onboarded", False)
        ctl = _controller(cfg)
        ctl.ask_history_enabled = lambda: answer
        ctl.show_history_onboarding()
        assert cfg.get("history.enabled") is answer
        assert cfg.get("history.onboarded") is True

    def test_dialog_defaults_to_enable_and_stays_on_top_without_main_window(self, box):
        box.click = "开启"
        ctl = _controller(_Config(), parent_widget=None)
        assert ctl.ask_history_enabled() is True
        assert [t for t, _ in box.last.buttons] == ["开启", "暂不开启"]
        assert box.last.default == box.last.buttons[0]
        assert box.last.title == "开启转写历史？"
        assert "随时可以在设置关闭" in box.last.text
        # U-03: no main window yet, so the box must not open behind other apps.
        assert box.last.parent is None
        assert box.last.flags == [(Qt.WindowType.WindowStaysOnTopHint, True)]

    def test_dialog_is_owned_by_the_main_window_when_it_exists(self, box):
        box.click = "暂不开启"
        anchor = object()
        ctl = _controller(_Config(), parent_widget=anchor)
        assert ctl.ask_history_enabled() is False
        assert box.last.parent is anchor
        assert box.last.flags == []


class TestAppWiring:
    def test_start_schedules_onboarding_on_the_recognizer_ready_signal(self):
        with app_harness(config_overrides={"history.onboarded": False}) as h:
            h["recognizer"].ready.connect.reset_mock()
            h["app"].start()
            h["recognizer"].ready.connect.assert_any_call(
                h["app"]._onboarding.show_history_onboarding_once
            )

    def test_first_run_connects_welcome_not_history(self):
        with app_harness(
            config_overrides={"history.onboarded": False, "first_run_welcome_seen": False}
        ) as h:
            h["recognizer"].ready.connect.reset_mock()
            h["app"].start()
            connected = [c.args[0] for c in h["recognizer"].ready.connect.call_args_list]
            assert h["app"]._onboarding.show_welcome_once in connected
            assert h["app"]._onboarding.show_history_onboarding_once not in connected


def test_missing_model_schedules_setup_without_the_long_fallback(monkeypatch):
    cfg = _Config(first_run_welcome_seen=False)
    ctl = _controller(cfg, model_installed=False)
    ready = MagicMock()
    shots = []
    monkeypatch.setattr(onboarding_module.QTimer, "singleShot", lambda ms, fn: shots.append((ms, fn)))
    ctl.schedule(ready)
    assert onboarding_module.WELCOME_FALLBACK_MS not in [ms for ms, _fn in shots]
    assert onboarding_module.WELCOME_DELAY_MS in [ms for ms, _fn in shots]
    ready.connect.assert_not_called()


def test_setup_trial_joins_every_captured_segment():
    import numpy as np

    ctl = _controller(_Config())
    ctl._setup_mode = "trial"
    first = np.array([0.1, 0.2], dtype=np.float32)
    ctl._on_setup_segment(first)
    first[:] = 0
    ctl._on_setup_segment(np.array([0.3], dtype=np.float32))
    assert np.allclose(ctl._trial_audio, [0.1, 0.2, 0.3])


def test_setup_trial_stays_on_the_page_and_close_marks_welcome_seen(monkeypatch):
    from PyQt6.QtWidgets import QDialog, QLabel, QLineEdit, QPushButton

    pasted = []
    monkeypatch.setattr(
        "sayink.text_paster.TextPaster.paste_async",
        lambda *args, **kwargs: pasted.append(args),
    )
    cfg = _Config(first_run_welcome_seen=False)
    cfg.set("history.onboarded", True)
    ctl = _controller(cfg, model_installed=False)
    seen = {}

    def exec_(dialog):
        buttons = dialog.findChildren(QPushButton)
        seen["buttons"] = [button.text() for button in buttons]
        seen["names"] = [button.accessibleName() for button in buttons]
        seen["fields"] = dialog.findChildren(QLineEdit)
        ctl.note_mic_level(0)
        seen["mic"] = dialog.findChild(QLabel, "setupMicStatus").text()
        ctl.submit_trial("准备页试说")
        seen["trial"] = dialog.findChild(QLabel, "setupTrial").text()
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(QDialog, "exec", exec_)
    ctl.show_setup()
    assert "下载模型" in seen["buttons"]
    assert "下载模型" in seen["names"]
    assert seen["fields"] == []
    assert "没有听到" in seen["mic"]
    assert "准备页试说" in seen["trial"]
    assert pasted == []
    assert cfg.get("first_run_welcome_seen") is False
    again = _controller(cfg, model_installed=False)
    shown = []
    monkeypatch.setattr(again, "show_setup", lambda: shown.append("setup"))
    again.show_setup_once()
    assert shown == ["setup"]


def test_setup_download_opens_the_engine_page_without_marking_welcome(monkeypatch):
    from PyQt6.QtWidgets import QDialog, QPushButton

    cfg = _Config(first_run_welcome_seen=False)
    cfg.set("history.onboarded", True)
    ctl = _controller(cfg, model_installed=False)

    def exec_(dialog):
        button = next(item for item in dialog.findChildren(QPushButton) if item.text() == "下载模型")
        button.click()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QDialog, "exec", exec_)
    activated = []
    monkeypatch.setattr(QDialog, "show", lambda dialog: None)
    monkeypatch.setattr(QDialog, "raise_", lambda dialog: activated.append("raise"))
    monkeypatch.setattr(QDialog, "activateWindow", lambda dialog: activated.append("activate"))
    ctl.show_setup()
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication
    QApplication.processEvents()
    assert ctl.opened == ["engine"]
    assert cfg.get("first_run_welcome_seen") is False
    assert ctl._setup_dialog.download_btn.isDefault()
    assert not ctl._setup_dialog.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    assert activated == []


def test_closing_setup_schedules_the_history_question(monkeypatch):
    from PyQt6.QtWidgets import QDialog

    cfg = _Config(first_run_welcome_seen=False)
    ctl = _controller(cfg, model_installed=False)
    shots = []
    monkeypatch.setattr(onboarding_module.QTimer, "singleShot", lambda ms, fn: shots.append((ms, fn)))
    monkeypatch.setattr(QDialog, "exec", lambda dialog: QDialog.DialogCode.Rejected)
    ctl.show_setup()
    assert shots == []
    assert cfg.get("first_run_welcome_seen") is False

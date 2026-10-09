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
        assert "语音模型已就绪" in box.last.text
        assert ctl.opened == []

    def test_text_follows_trigger_mode(self):
        cfg = _Config(hotkey="alt+x")
        assert "按住说话" in _controller(cfg).welcome_text(model_missing=False)
        assert "自动持续转写" in _controller(cfg, continuous=True).welcome_text(model_missing=False)

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
        assert box.last.title == "开启语音历史？"
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

"""UpdateController: state machine and UI hooks, independent of App (Q-01/Q-07)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from sayink.update_controller import UpdateController, UpdateState
from sayink.updater import MISSING_DIGEST_MESSAGE, ReleaseInfo

_URL = "https://github.com/zyjhandsome/SayInk/releases/download/v2.0.9/SayInk-Setup-2.0.9.exe"


class _Config:
    def __init__(self, **values):
        self.values = dict(values)

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value


@pytest.fixture
def ctl(_qapp_session):
    sink = MagicMock()
    tray = MagicMock()
    hooks = {
        "about": MagicMock(),
        "confirm": MagicMock(return_value=True),
        "quit": MagicMock(),
        "exit_started": False,
    }
    controller = UpdateController(
        _Config(),
        parent=None,
        status_sink=lambda: sink,
        tray=tray,
        show_about=hooks["about"],
        confirm_exit=hooks["confirm"],
        finish_quit=hooks["quit"],
        exit_started=lambda: hooks["exit_started"],
    )
    controller.sink = sink
    controller.tray = tray
    controller.hooks = hooks
    return controller


def _release(sha256="ab" * 32):
    return ReleaseInfo("2.0.9", "SayInk-Setup-2.0.9.exe", _URL, size=10, sha256=sha256)


class TestCheck:
    def test_interactive_check_shows_busy_and_starts_worker(self, ctl):
        with patch("sayink.updater.UpdateCheckWorker") as worker_cls:
            ctl.check(interactive=True)
        assert ctl.state is UpdateState.CHECKING
        ctl.sink.set_update_status.assert_called_with("正在检查…", action="busy")
        worker_cls.return_value.start.assert_called_once()

    def test_background_check_is_silent_until_a_release_shows_up(self, ctl):
        with patch("sayink.updater.UpdateCheckWorker"):
            ctl.check(interactive=False)
        ctl.sink.set_update_status.assert_not_called()
        ctl.on_check_result(_release())
        assert ctl.state is UpdateState.AVAILABLE
        ctl.tray.show_update_notice.assert_called_once_with("2.0.9 可以安装")
        ctl.sink.set_update_status.assert_called_with("发现 2.0.9", action="install")
        assert ctl._config.get("update.last_check_at") > 0

    def test_interactive_result_does_not_raise_a_tray_notice(self, ctl):
        with patch("sayink.updater.UpdateCheckWorker"):
            ctl.check(interactive=True)
        ctl.on_check_result(_release())
        ctl.tray.show_update_notice.assert_not_called()

    def test_no_release_means_current(self, ctl):
        ctl.on_check_result(None)
        assert ctl.state is UpdateState.CURRENT
        ctl.sink.set_update_status.assert_called_with("已是最新版本", action="check")

    def test_failure_only_surfaces_when_the_user_asked(self, ctl):
        ctl.on_check_failed("boom")
        assert ctl.state is UpdateState.ERROR
        ctl.sink.set_update_status.assert_not_called()
        with patch("sayink.updater.UpdateCheckWorker"):
            ctl.check(interactive=True)
        ctl.on_check_failed("boom")
        ctl.sink.set_update_status.assert_called_with("检查失败，请稍后再试", action="check")

    def test_tray_entry_opens_about_then_checks(self, ctl):
        with patch("sayink.updater.UpdateCheckWorker"):
            ctl.check_from_tray()
        ctl.hooks["about"].assert_called_once()
        assert ctl.state is UpdateState.CHECKING

    def test_auto_check_respects_config(self, ctl):
        ctl._config.set("update.auto_check", False)
        with patch("sayink.updater.UpdateCheckWorker") as worker_cls:
            ctl.maybe_auto_check()
        worker_cls.assert_not_called()


class TestInstall:
    def test_missing_digest_blocks_download(self, ctl):
        ctl.pending_release = _release(sha256="")
        with patch("sayink.updater.UpdateDownloadWorker") as worker_cls:
            ctl.install()
        worker_cls.assert_not_called()
        ctl.sink.set_update_status.assert_called_with(MISSING_DIGEST_MESSAGE, action="check")

    def test_download_progress_and_failure_go_to_the_about_page(self, ctl):
        ctl.pending_release = _release()
        with patch("sayink.updater.UpdateDownloadWorker"):
            ctl.install()
        assert ctl.state is UpdateState.DOWNLOADING
        ctl.on_download_progress(5 * 1024 * 1024, 10 * 1024 * 1024)
        ctl.sink.set_update_status.assert_called_with("正在下载 5 / 10 MB", action="busy")
        # While downloading, re-opening the About page must not overwrite the progress text.
        ctl.sink.reset_mock()
        ctl.sync_status()
        ctl.sink.set_update_status.assert_not_called()
        ctl.on_download_failed("网络中断")
        assert ctl.state is UpdateState.AVAILABLE
        ctl.sink.set_update_status.assert_called_with("网络中断", action="install")

    def test_downloaded_installer_waits_for_exit_confirmation(self, ctl):
        ctl.hooks["confirm"].return_value = False
        with patch("sayink.updater.launch_installer") as launch:
            ctl.on_downloaded("x.exe")
        launch.assert_not_called()
        assert ctl.state is UpdateState.AVAILABLE
        ctl.hooks["quit"].assert_not_called()

    def test_downloaded_installer_launches_then_quits(self, ctl):
        with patch("sayink.updater.launch_installer") as launch:
            ctl.on_downloaded("x.exe")
        launch.assert_called_once_with("x.exe")
        ctl.hooks["quit"].assert_called_once()

    def test_nothing_happens_once_exit_has_started(self, ctl):
        ctl.hooks["exit_started"] = True
        with patch("sayink.updater.launch_installer") as launch:
            ctl.on_downloaded("x.exe")
        launch.assert_not_called()
        ctl.hooks["confirm"].assert_not_called()


def test_status_writes_are_skipped_without_an_about_page(_qapp_session):
    controller = UpdateController(
        _Config(),
        parent=None,
        status_sink=lambda: None,
        tray=MagicMock(),
        show_about=lambda: None,
        confirm_exit=lambda: True,
        finish_quit=lambda: None,
        exit_started=lambda: False,
    )
    controller.on_check_result(None)  # must not raise
    controller.on_download_progress(1, 0)
    assert controller.state is UpdateState.CURRENT

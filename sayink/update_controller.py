"""Update check / download lifecycle, kept out of the App orchestrator.

The controller owns the two worker threads and the state machine; the App
only supplies the UI hooks it needs (where to show status, how to confirm an
exit, how to open the About page).
"""

from __future__ import annotations

import logging
import tempfile
import time
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol

from PyQt6.QtCore import QObject

log = logging.getLogger("SayInk")


class UpdateState(str, Enum):
    IDLE = "idle"
    CHECKING = "checking"
    CURRENT = "current"
    AVAILABLE = "available"
    ERROR = "error"
    DOWNLOADING = "downloading"


class UpdateStatusSink(Protocol):
    def set_update_status(self, text: str, *, action: str = "check") -> None: ...


class UpdateController(QObject):
    """Checks GitHub releases and downloads the installer on request.

    ``status_sink`` returns the About page (or ``None`` when the main window
    has not been created yet); every status write goes through it so a
    tray-only session never touches a missing widget.
    """

    def __init__(
        self,
        config,
        *,
        parent: QObject | None,
        status_sink: Callable[[], UpdateStatusSink | None],
        tray,
        show_about: Callable[[], None],
        confirm_exit: Callable[[], bool],
        finish_quit: Callable[[], None],
        exit_started: Callable[[], bool],
    ) -> None:
        super().__init__(parent)
        self._config = config
        self.status_sink = status_sink
        self._tray = tray
        self._show_about = show_about
        self._confirm_exit = confirm_exit
        self._finish_quit = finish_quit
        self._exit_started = exit_started
        self.state = UpdateState.IDLE
        self.pending_release = None
        self._interactive = False
        self._check_worker = None
        self._download_worker = None

    # ── Entry points ───────────────────────────────────

    def maybe_auto_check(self) -> None:
        from sayink.updater import should_auto_check

        enabled = bool(self._config.get("update.auto_check", True))
        last = float(self._config.get("update.last_check_at", 0) or 0)
        if should_auto_check(enabled=enabled, last_check_at=last, now=time.time()):
            self.check(interactive=False)

    def check_from_tray(self) -> None:
        self._show_about()
        self.check(interactive=True)

    def on_tray_message_clicked(self) -> None:
        if getattr(self._tray, "notice_kind", "") != "update":
            return
        self._show_about()

    def check(self, *, interactive: bool) -> None:
        from sayink.updater import UpdateCheckWorker
        from sayink.version import __version__

        worker = self._check_worker
        if worker is not None and worker.isRunning():
            self._interactive = self._interactive or interactive
            if interactive:
                self.state = UpdateState.CHECKING
                self.sync_status()
            return
        self._interactive = interactive
        self.state = UpdateState.CHECKING
        if interactive:
            self.sync_status()
        worker = UpdateCheckWorker(__version__, self)
        worker.result_ready.connect(self.on_check_result)
        worker.failed.connect(self.on_check_failed)
        self._check_worker = worker
        worker.start()

    def install(self) -> None:
        from sayink.updater import MISSING_DIGEST_MESSAGE, UpdateDownloadWorker

        release = self.pending_release
        if release is None:
            return
        worker = self._download_worker
        if worker is not None and worker.isRunning():
            return
        if not release.sha256:
            self._set_status(MISSING_DIGEST_MESSAGE, action="check")
            return
        dest = Path(tempfile.gettempdir()) / "SayInk" / release.asset_name
        self.state = UpdateState.DOWNLOADING
        self._set_status("正在下载…", action="busy")
        worker = UpdateDownloadWorker(
            release.asset_url,
            dest,
            self,
            expected_size=release.size,
            sha256=release.sha256,
        )
        worker.progress.connect(self.on_download_progress)
        worker.finished_path.connect(self.on_downloaded)
        worker.failed.connect(self.on_download_failed)
        self._download_worker = worker
        worker.start()

    # ── Status ─────────────────────────────────────────

    def sync_status(self) -> None:
        """Re-paint the About page from the current state (e.g. when it opens)."""
        if self.status_sink() is None or self.state is UpdateState.DOWNLOADING:
            return
        if self.state is UpdateState.AVAILABLE and self.pending_release is not None:
            self._set_status(f"发现 {self.pending_release.version}", action="install")
        elif self.state is UpdateState.CURRENT:
            self._set_status("已是最新版本", action="check")
        elif self.state is UpdateState.ERROR:
            self._set_status("检查失败，请稍后再试", action="check")
        elif self.state is UpdateState.CHECKING:
            self._set_status("正在检查…", action="busy")

    def _set_status(self, text: str, *, action: str) -> None:
        sink = self.status_sink()
        if sink is not None:
            sink.set_update_status(text, action=action)

    def _remember_check(self) -> None:
        self._config.set("update.last_check_at", time.time())

    # ── Worker slots ───────────────────────────────────

    def on_check_result(self, info) -> None:
        self._remember_check()
        self.pending_release = info
        if info is None:
            self.state = UpdateState.CURRENT
            self.sync_status()
            return
        self.state = UpdateState.AVAILABLE
        self.sync_status()
        if not self._interactive:
            self._tray.show_update_notice(f"{info.version} 可以安装")

    def on_check_failed(self, _message: str) -> None:
        self._remember_check()
        self.state = UpdateState.ERROR
        if self._interactive:
            self.sync_status()

    def on_download_progress(self, got: int, total: int) -> None:
        got_mb = got / (1024 * 1024)
        if total:
            total_mb = total / (1024 * 1024)
            text = f"正在下载 {got_mb:.0f} / {total_mb:.0f} MB"
        else:
            text = f"正在下载 {got_mb:.0f} MB"
        self._set_status(text, action="busy")

    def on_download_failed(self, message: str) -> None:
        self.state = UpdateState.AVAILABLE
        self._set_status(message, action="install")

    def on_downloaded(self, path: str) -> None:
        from sayink.updater import launch_installer

        if self._exit_started():
            return
        if not self._confirm_exit():
            self.state = UpdateState.AVAILABLE
            self._set_status(
                "下载完成。请结束监听并等待处理完成，再安装更新。", action="install"
            )
            return
        try:
            launch_installer(path)
        except Exception as exc:
            log.warning("无法启动安装包: %s", exc)
            self.state = UpdateState.AVAILABLE
            self._set_status("下载完成，但无法启动安装包", action="install")
            return
        self._finish_quit()

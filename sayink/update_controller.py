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

from PyQt6.QtCore import QObject, QThread

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
        self._check_status_visible = False
        self._check_worker = None
        self._download_worker = None
        self._download_status: tuple[str, str] | None = None

    @property
    def running_workers(self):
        return tuple(worker for worker in self.findChildren(QThread) if worker.isRunning())

    def shutdown(self) -> None:
        for worker in self.running_workers:
            if hasattr(worker, "cancel"):
                worker.cancel()

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

        if self.state is UpdateState.DOWNLOADING:
            if interactive:
                self.sync_status()
            return
        worker = self._check_worker
        if worker is not None and worker.isRunning():
            self._interactive = self._interactive or interactive
            if interactive:
                self.state = UpdateState.CHECKING
                self.sync_status()
            return
        self._interactive = interactive
        self._check_status_visible = False
        self._download_status = None
        self.state = UpdateState.CHECKING
        if interactive:
            self.sync_status()
        worker = UpdateCheckWorker(
            __version__,
            self,
            proxy_mode=str(self._config.get("llm.proxy_mode", "system") or "system"),
            proxy_bypass_suffixes=str(self._config.get("llm.proxy_bypass_suffixes", "") or ""),
        )
        worker.result_ready.connect(self.on_check_result)
        worker.failed.connect(self.on_check_failed)
        finished_sig = getattr(worker, "finished", None)
        if finished_sig is not None and hasattr(finished_sig, "connect"):
            finished_sig.connect(lambda w=worker: self._reap_worker("_check_worker", w))
        self._check_worker = worker
        worker.start()

    def _reap_worker(self, attr: str, worker: object) -> None:
        if getattr(self, attr, None) is worker:
            setattr(self, attr, None)
        if isinstance(worker, QObject):
            worker.deleteLater()

    def install(self) -> None:
        from sayink.updater import (
            MISSING_DIGEST_MESSAGE,
            UpdateDownloadWorker,
            cleanup_stale_installers,
        )

        release = self.pending_release
        if release is None:
            return
        worker = self._download_worker
        if worker is not None and worker.isRunning():
            return
        if not release.sha256:
            self._set_download_status(MISSING_DIGEST_MESSAGE, action="check")
            return
        cache_dir = Path(tempfile.gettempdir()) / "SayInk"
        cleanup_stale_installers(cache_dir, keep_asset_name=release.asset_name)
        dest = cache_dir / release.asset_name
        self.state = UpdateState.DOWNLOADING
        self._set_download_status("正在下载…", action="busy")
        worker = UpdateDownloadWorker(
            release.asset_url,
            dest,
            self,
            expected_size=release.size,
            sha256=release.sha256,
            signature_url=getattr(release, "signature_url", ""),
            proxy_mode=str(self._config.get("llm.proxy_mode", "system") or "system"),
            proxy_bypass_suffixes=str(self._config.get("llm.proxy_bypass_suffixes", "") or ""),
        )
        worker.progress.connect(self.on_download_progress)
        worker.finished_path.connect(self.on_downloaded)
        worker.failed.connect(self.on_download_failed)
        finished_sig = getattr(worker, "finished", None)
        if finished_sig is not None and hasattr(finished_sig, "connect"):
            finished_sig.connect(lambda w=worker: self._reap_worker("_download_worker", w))
        self._download_worker = worker
        worker.start()

    # ── Status ─────────────────────────────────────────

    def sync_status(self) -> None:
        """Re-paint the About page from the current state (e.g. when it opens)."""
        if self.status_sink() is None:
            return
        if self._download_status is not None:
            text, action = self._download_status
            self._set_status(text, action=action)
            return
        if self.state is UpdateState.AVAILABLE and self.pending_release is not None:
            self._set_status(f"发现 {self.pending_release.version}", action="install")
        elif self.state is UpdateState.CURRENT:
            self._set_status("已是最新版本", action="check")
        elif self.state is UpdateState.ERROR:
            self._set_status("检查失败，请稍后再试", action="check")
        elif self.state is UpdateState.CHECKING:
            self._check_status_visible = True
            self._set_status("正在检查…", action="busy")

    def _set_status(self, text: str, *, action: str) -> None:
        sink = self.status_sink()
        if sink is not None:
            sink.set_update_status(text, action=action)

    def _set_download_status(self, text: str, *, action: str) -> None:
        # Reopening About must retain progress and a failed download's reason.
        self._download_status = (text, action)
        self._set_status(text, action=action)

    def _remember_check(self) -> None:
        self._config.set("update.last_check_at", int(time.time()))

    # ── Worker slots ───────────────────────────────────

    def on_check_result(self, info) -> None:
        if self._exit_started():
            return
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
        if self._exit_started():
            return
        # Not remembered: a start while offline must not push the next real
        # check out by 24 h, and About's 「上次检查」 only names checks that
        # actually reached the release feed.
        self.state = UpdateState.ERROR
        if self._interactive or self._check_status_visible:
            self.sync_status()

    def on_download_progress(self, got: int, total: int) -> None:
        if self._exit_started() or self.state is not UpdateState.DOWNLOADING:
            return
        got_mb = got / (1024 * 1024)
        if total:
            total_mb = total / (1024 * 1024)
            text = f"正在下载 {got_mb:.0f} / {total_mb:.0f} MB"
        else:
            text = f"正在下载 {got_mb:.0f} MB"
        self._set_download_status(text, action="busy")

    def on_download_failed(self, message: str) -> None:
        if self._exit_started():
            return
        self.state = UpdateState.AVAILABLE
        self._set_download_status(message, action="install")

    def _flush_before_installer_handoff(self) -> None:
        """Persist drafts, SQLite WAL, and config non-destructively before spawning Setup."""
        parent = self.parent()
        if parent is not None:
            mw = getattr(parent, "_main", None) or getattr(parent, "_main_window", None)
            if mw is not None:
                mp = getattr(mw, "meeting_panel", None)
                for owner in (mp, mw):
                    if owner is None:
                        continue
                    flush_notes = getattr(owner, "flush_pending_user_notes", None)
                    if callable(flush_notes):
                        try:
                            flush_notes()
                        except Exception:
                            log.debug("更新前刷新会议笔记草稿失败", exc_info=True)
                        break
                if mp is not None:
                    flush_title = getattr(mp, "_flush_bound_title", None)
                    if callable(flush_title):
                        try:
                            flush_title(sync=True)
                        except Exception:
                            log.debug("更新前刷新会议标题草稿失败", exc_info=True)
                    has_rec_notes = getattr(mp, "_has_unsaved_record_notes", None)
                    save_rec_notes = getattr(mp, "_save_record_notes", None)
                    if callable(save_rec_notes):
                        try:
                            if not callable(has_rec_notes) or has_rec_notes():
                                save_rec_notes()
                        except Exception:
                            log.debug("更新前保存会后笔记草稿失败", exc_info=True)
                hp = getattr(mw, "history_panel", None) or getattr(mw, "_history_page", None)
                flush_del = getattr(hp, "flush_pending_delete", None) if hp is not None else None
                if callable(flush_del):
                    try:
                        flush_del()
                    except Exception:
                        log.debug("更新前提交待删除历史记录失败", exc_info=True)
            history = getattr(parent, "_history", None)
            if history is not None:
                flush_history = getattr(history, "flush", None)
                if callable(flush_history):
                    try:
                        flush_history(timeout=3.0)
                    except TypeError:
                        flush_history()
                    except Exception:
                        log.debug("更新前同步刷写 HistoryStore 失败", exc_info=True)
                else:
                    conn = getattr(history, "_conn", None)
                    if conn is not None:
                        try:
                            conn.commit()
                        except Exception:
                            log.debug("更新前提交 SQLite 事务失败", exc_info=True)
        save_now = getattr(self._config, "save_immediate", None)
        if callable(save_now):
            try:
                save_now()
            except Exception:
                log.debug("更新前立即保存配置失败", exc_info=True)

    def on_downloaded(self, path: str) -> None:
        from sayink.updater import launch_installer

        if self._exit_started():
            return
        if not self._confirm_exit():
            self.state = UpdateState.AVAILABLE
            self._set_download_status(
                "下载完成。请结束监听并等待处理完成，再安装更新。", action="install"
            )
            return
        self._flush_before_installer_handoff()
        try:
            launch_installer(path)
        except Exception as exc:
            log.warning("无法启动安装包: %s", exc)
            self.state = UpdateState.AVAILABLE
            self._set_download_status("下载完成，但无法启动安装包", action="install")
            return
        parent = self.parent()
        if parent is not None:
            try:
                parent._updating_to_installer = True
            except Exception:
                pass
        self._finish_quit()

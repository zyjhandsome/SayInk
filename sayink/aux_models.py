"""Meeting helper models (CAM++ speaker embedding, Silero VAD, speaker-activity).

Which of them are missing, and one sequential downloader shared by the
会议 page buttons and the first-meeting prompt in :mod:`sayink.app`. The
engine page keeps its own per-card download flow; this module only reuses
its :class:`~sayink.speech_recognizer.ModelDownloadWorker`.
"""

from __future__ import annotations

import logging
import re
from typing import Callable

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from sayink.speech_recognizer import (
    AUX_MODEL_REGISTRY,
    DOWNLOAD_SOURCE_AUTO,
    ModelDownloadWorker,
    is_model_downloaded,
)

log = logging.getLogger("SayInk")
_LEADING_DOWNLOAD_FAIL_RE = re.compile(r"^\s*下载失败\s*[:：]\s*")


def missing_aux_models(*, include_optional: bool = True) -> list[dict]:
    """Registry entries of helper models not yet on disk, in registry order."""
    return [m for m in AUX_MODEL_REGISTRY if (include_optional or not m.get("optional"))
            and not is_model_downloaded(m["id"])]


def aux_models_size_mb(models: list[dict]) -> int:
    return sum(int(m.get("size_mb") or 0) for m in models)


def describe_aux_models(models: list[dict]) -> str:
    """「说话人嵌入 CAM++、Silero VAD（约 30 MB）」 for prompts and buttons."""
    if not models:
        return ""
    names = "、".join(str(m["name"]) for m in models)
    return f"{names}（约 {aux_models_size_mb(models)} MB）"


WorkerFactory = Callable[[str, str], object]


class AuxModelDownloads(QObject):
    """Download several aux models one after another with one progress bar.

    ``progress`` is 0–100 over the whole batch, weighted by ``size_mb``.
    ``finished(ok, message)`` fires once: ``ok`` when every model landed,
    otherwise ``message`` explains the first failure (or 「已取消」).
    """

    progress = pyqtSignal(int)
    finished = pyqtSignal(bool, str)

    def __init__(
        self,
        source: str = DOWNLOAD_SOURCE_AUTO,
        parent: QObject | None = None,
        *,
        worker_factory: WorkerFactory | None = None,
    ) -> None:
        super().__init__(parent)
        self._source = source
        self._factory = worker_factory or (lambda mid, src: ModelDownloadWorker(mid, source=src))
        self._queue: list[dict] = []
        self._done: list[dict] = []
        self._worker: object | None = None
        self._workers: list[object] = []
        self._current: dict | None = None
        self._total_mb = 0
        self._cancelled = False

    @property
    def running(self) -> bool:
        return self._worker is not None

    @property
    def running_workers(self):
        return tuple(worker for worker in self._workers if isinstance(worker, QThread) and worker.isRunning())

    @property
    def downloaded_any(self) -> bool:
        """Whether this batch placed any complete models on disk."""
        return bool(self._done)

    def start(self, model_ids: list[str] | None = None) -> bool:
        """Begin downloading ``model_ids`` (default: everything missing).
        Returns False when there is nothing to do or a batch is running."""
        if self.running:
            return False
        wanted = set(model_ids) if model_ids is not None else None
        missing = missing_aux_models(include_optional=True)
        self._queue = [m for m in missing if wanted is None or m["id"] in wanted]
        if not self._queue:
            return False
        self._done = []
        self._cancelled = False
        self._total_mb = max(1, aux_models_size_mb(self._queue))
        self._next()
        return True

    def cancel(self) -> None:
        self._cancelled = True
        worker = self._worker
        if worker is not None and hasattr(worker, "cancel"):
            worker.cancel()

    def _next(self) -> None:
        if not self._queue:
            self._worker = None
            self._current = None
            self.progress.emit(100)
            # A modal progress dialog may dispatch a cancel while being updated.
            self.finished.emit(not self._cancelled, "已取消" if self._cancelled else "")
            return
        self._current = self._queue.pop(0)
        worker = self._factory(str(self._current["id"]), self._source)
        self._worker = worker
        self._workers.append(worker)
        worker.progress.connect(lambda pct, w=worker: self._on_worker_progress(pct, w))
        worker.finished_ok.connect(lambda mid, w=worker: self._on_worker_done(mid, w))
        worker.error.connect(lambda msg, w=worker: self._on_worker_error(msg, w))
        if hasattr(worker, "finished"):
            worker.finished.connect(lambda w=worker: self._reap_worker(w))
        log.info("下载会议辅助模型 %s", self._current["id"])
        worker.start()

    def _reap_worker(self, worker: object) -> None:
        try:
            self._workers.remove(worker)
        except ValueError:
            pass

    def _release_if_not_qthread(self, worker: object | None) -> None:
        if worker is not None and not hasattr(worker, "finished"):
            self._reap_worker(worker)

    def _on_worker_progress(self, pct: int, worker: object | None = None) -> None:
        if worker is not None and worker is not self._worker:
            return
        current_mb = int((self._current or {}).get("size_mb") or 0)
        done_mb = aux_models_size_mb(self._done)
        overall = (done_mb + current_mb * max(0, min(100, int(pct))) / 100.0) / self._total_mb * 100.0
        self.progress.emit(min(99, int(overall)))

    def _on_worker_done(self, model_id: str, worker: object | None = None) -> None:
        if worker is not None and worker is not self._worker:
            return
        if self._current is not None:
            self._done.append(self._current)
        if self._cancelled:
            self._on_worker_error("已取消")
            return
        prev = self._worker
        self._worker = None
        self._release_if_not_qthread(prev)
        self._next()

    def _on_worker_error(self, message: str, worker: object | None = None) -> None:
        if worker is not None and worker is not self._worker:
            return
        prev = self._worker
        self._worker = None
        self._release_if_not_qthread(prev)
        self._queue.clear()
        current = self._current
        self._current = None
        if self._cancelled:
            # The worker may have written its last file just before seeing cancel.
            if current is not None and current not in self._done and is_model_downloaded(current["id"]):
                self._done.append(current)
            self.finished.emit(False, "已取消")
            return
        name = str((current or {}).get("name") or "")
        clean_msg = _LEADING_DOWNLOAD_FAIL_RE.sub("", str(message or "")).strip() or str(message or "")
        log.warning("会议辅助模型 %s 下载失败: %s", name, clean_msg)
        self.finished.emit(False, f"{name} 下载失败：{clean_msg}" if name else clean_msg)

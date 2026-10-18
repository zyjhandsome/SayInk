"""Meeting helper models (CAM++ speaker embedding, Silero VAD).

Which of them are missing, and one sequential downloader shared by the
会议 page buttons and the first-meeting prompt in :mod:`sayink.app`. The
engine page keeps its own per-card download flow; this module only reuses
its :class:`~sayink.speech_recognizer.ModelDownloadWorker`.
"""

from __future__ import annotations

import logging
from typing import Callable

from PyQt6.QtCore import QObject, pyqtSignal

from sayink.speech_recognizer import (
    AUX_MODEL_REGISTRY,
    DOWNLOAD_SOURCE_AUTO,
    ModelDownloadWorker,
    is_model_downloaded,
)

log = logging.getLogger("SayInk")


def missing_aux_models() -> list[dict]:
    """Registry entries of helper models not yet on disk, in registry order."""
    return [m for m in AUX_MODEL_REGISTRY if not is_model_downloaded(m["id"])]


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

    def start(self, model_ids: list[str] | None = None) -> bool:
        """Begin downloading ``model_ids`` (default: everything missing).
        Returns False when there is nothing to do or a batch is running."""
        if self.running:
            return False
        wanted = set(model_ids) if model_ids is not None else None
        self._queue = [m for m in missing_aux_models() if wanted is None or m["id"] in wanted]
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
            self.finished.emit(True, "")
            return
        self._current = self._queue.pop(0)
        worker = self._factory(str(self._current["id"]), self._source)
        self._worker = worker
        self._workers.append(worker)
        worker.progress.connect(self._on_worker_progress)
        worker.finished_ok.connect(self._on_worker_done)
        worker.error.connect(self._on_worker_error)
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

    def _on_worker_progress(self, pct: int) -> None:
        current_mb = int((self._current or {}).get("size_mb") or 0)
        done_mb = aux_models_size_mb(self._done)
        overall = (done_mb + current_mb * max(0, min(100, int(pct))) / 100.0) / self._total_mb * 100.0
        self.progress.emit(min(99, int(overall)))

    def _on_worker_done(self, model_id: str) -> None:
        if self._current is not None:
            self._done.append(self._current)
        prev = self._worker
        self._worker = None
        self._release_if_not_qthread(prev)
        self._next()

    def _on_worker_error(self, message: str) -> None:
        prev = self._worker
        self._worker = None
        self._release_if_not_qthread(prev)
        self._queue.clear()
        name = str((self._current or {}).get("name") or "")
        self._current = None
        if self._cancelled:
            self.finished.emit(False, "已取消")
            return
        log.warning("会议辅助模型 %s 下载失败: %s", name, message)
        self.finished.emit(False, f"{name} 下载失败：{message}" if name else str(message))

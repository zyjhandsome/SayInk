"""Meeting helper-model downloads: which are missing, sequential batch with
one progress bar, failure and cancel paths."""

from __future__ import annotations

from PyQt6.QtCore import QObject, pyqtSignal

from sayink.aux_models import (
    AuxModelDownloads,
    aux_models_size_mb,
    describe_aux_models,
    missing_aux_models,
)
from sayink.speech_recognizer import AUX_MODEL_REGISTRY, AUX_MODEL_SILERO_VAD, AUX_MODEL_SPEAKER


class _FakeWorker(QObject):
    progress = pyqtSignal(int)
    finished_ok = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, model_id: str, source: str):
        super().__init__()
        self.model_id = model_id
        self.source = source
        self.started = False
        self.cancelled = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True


def _factory(created: list):
    def make(model_id: str, source: str):
        worker = _FakeWorker(model_id, source)
        created.append(worker)
        return worker

    return make


def test_missing_and_description(monkeypatch):
    monkeypatch.setattr("sayink.aux_models.is_model_downloaded", lambda mid: mid == AUX_MODEL_SILERO_VAD)
    missing = missing_aux_models()
    assert [m["id"] for m in missing] == [AUX_MODEL_SPEAKER]
    assert aux_models_size_mb(missing) == 28
    assert describe_aux_models(missing) == "说话人嵌入 CAM++（约 28 MB）"
    assert describe_aux_models([]) == ""
    monkeypatch.setattr("sayink.aux_models.is_model_downloaded", lambda mid: True)
    assert missing_aux_models() == []


def test_sequential_batch_with_weighted_progress(monkeypatch):
    monkeypatch.setattr("sayink.aux_models.is_model_downloaded", lambda mid: False)
    created: list[_FakeWorker] = []
    batch = AuxModelDownloads("mirror", worker_factory=_factory(created))
    seen: list[int] = []
    done: list[tuple[bool, str]] = []
    batch.progress.connect(seen.append)
    batch.finished.connect(lambda ok, msg: done.append((ok, msg)))

    assert batch.start() is True
    assert batch.running and batch.start() is False       # no overlapping batches
    assert [w.model_id for w in created] == [AUX_MODEL_SPEAKER]
    assert created[0].source == "mirror" and created[0].started

    created[0].progress.emit(50)
    assert seen[-1] == int(28 * 0.5 / 30 * 100)            # 46 % of the whole batch
    created[0].finished_ok.emit(AUX_MODEL_SPEAKER)
    assert [w.model_id for w in created] == [AUX_MODEL_SPEAKER, AUX_MODEL_SILERO_VAD]
    created[1].progress.emit(100)
    assert seen[-1] == 99                                   # capped until the last file lands
    created[1].finished_ok.emit(AUX_MODEL_SILERO_VAD)
    assert seen[-1] == 100
    assert done == [(True, "")]
    assert not batch.running


def test_subset_and_nothing_to_do(monkeypatch):
    monkeypatch.setattr("sayink.aux_models.is_model_downloaded", lambda mid: False)
    created: list[_FakeWorker] = []
    batch = AuxModelDownloads(worker_factory=_factory(created))
    assert batch.start([AUX_MODEL_SILERO_VAD]) is True
    assert [w.model_id for w in created] == [AUX_MODEL_SILERO_VAD]
    monkeypatch.setattr("sayink.aux_models.is_model_downloaded", lambda mid: True)
    assert AuxModelDownloads(worker_factory=_factory([])).start() is False


def test_error_stops_batch_with_model_name(monkeypatch):
    monkeypatch.setattr("sayink.aux_models.is_model_downloaded", lambda mid: False)
    created: list[_FakeWorker] = []
    batch = AuxModelDownloads(worker_factory=_factory(created))
    done: list[tuple[bool, str]] = []
    batch.finished.connect(lambda ok, msg: done.append((ok, msg)))
    batch.start()
    created[0].error.emit("HTTP 503")
    assert done == [(False, "说话人嵌入 CAM++ 下载失败：HTTP 503")]
    assert len(created) == 1 and not batch.running


def test_cancel_reports_cancelled(monkeypatch):
    monkeypatch.setattr("sayink.aux_models.is_model_downloaded", lambda mid: False)
    created: list[_FakeWorker] = []
    batch = AuxModelDownloads(worker_factory=_factory(created))
    done: list[tuple[bool, str]] = []
    batch.finished.connect(lambda ok, msg: done.append((ok, msg)))
    batch.start()
    batch.cancel()
    assert created[0].cancelled
    created[0].error.emit("cancelled")                      # the real worker reports via error
    assert done == [(False, "已取消")]


def test_registry_models_are_all_aux():
    assert all(m.get("aux") for m in AUX_MODEL_REGISTRY)


def test_running_qthread_worker_stays_retained_until_finished(monkeypatch):
    """When ``error`` fires just before ``run()`` returns, dropping the sole
    reference to a running QThread aborts Qt if GC runs in between."""
    import threading
    from PyQt6.QtCore import QCoreApplication
    from sayink.speech_recognizer import ModelDownloadWorker

    monkeypatch.setattr("sayink.aux_models.is_model_downloaded", lambda mid: False)
    release = threading.Event()

    def slow_error(self):
        self.error.emit("HTTP 503")
        release.wait(2.0)

    monkeypatch.setattr(ModelDownloadWorker, "run", slow_error)
    batch = AuxModelDownloads()
    assert batch.start([AUX_MODEL_SILERO_VAD]) is True
    worker = batch._worker
    assert isinstance(worker, ModelDownloadWorker)

    deadline = threading.Event()
    batch.finished.connect(lambda _ok, _msg: deadline.set())
    while not deadline.is_set():
        QCoreApplication.processEvents()

    assert batch._worker is None
    assert worker in batch._workers
    release.set()
    assert worker.wait(2000)
    QCoreApplication.processEvents()
    assert worker not in batch._workers

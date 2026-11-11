"""Unit and integration tests for fine-grained recognition diffing and silent hotword warm-swap."""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

import numpy as np
import pytest
from PyQt6.QtCore import QObject, pyqtSignal

from sayink.recognition_settings import RecognitionSettings
from sayink.runtime_status import ModelLoadPhase
from sayink.speech_recognizer import SpeechRecognizer
from tests.helpers.app_harness import app_harness


class _FakeStream:
    def __init__(self, text: str):
        self._text = text
        self.result = type("_Res", (), {"text": text})()

    def accept_waveform(self, sample_rate: int, audio: np.ndarray) -> None:
        pass


class _FakeOfflineRecognizer:
    def __init__(self, label: str, *, model_id: str = "", hotwords: str = "", language: str = ""):
        self.label = label
        self.model_id = model_id
        self.hotwords = hotwords
        self.language = language
        self.decode_calls = 0

    def create_stream(self) -> _FakeStream:
        return _FakeStream(f"decoded-{self.label}")

    def decode_stream(self, stream: _FakeStream) -> None:
        self.decode_calls += 1


class _ControlledLoadWorker(QObject):
    """Deterministic stand-in for ``ModelLoadWorker`` to step async reloads cleanly."""

    loaded = pyqtSignal(object)
    error = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(
        self,
        model_id: str,
        num_threads: int = 4,
        *,
        hotwords: str = "",
        language: str = "",
    ):
        super().__init__()
        self.model_id = model_id
        self.num_threads = num_threads
        self.hotwords = hotwords
        self.language = language
        self._running = False

    def start(self) -> None:
        self._running = True

    def isRunning(self) -> bool:
        return self._running

    def wait(self, _ms: int = 0) -> bool:
        return not self._running

    def complete(self, recognizer: _FakeOfflineRecognizer | None = None) -> _FakeOfflineRecognizer:
        rec = recognizer or _FakeOfflineRecognizer(
            f"{self.model_id}:{self.hotwords}:{self.language}",
            model_id=self.model_id,
            hotwords=self.hotwords,
            language=self.language,
        )
        self._running = False
        self.loaded.emit(rec)
        self.finished.emit()
        return rec

    def fail(self, message: str = "模型加载失败: mock error") -> None:
        self._running = False
        self.error.emit(message)
        self.finished.emit()


@contextmanager
def _real_sr_app_harness(config_overrides: dict | None = None):
    """Run ``App`` with a real ``SpeechRecognizer`` backed by controlled load workers."""
    workers: list[_ControlledLoadWorker] = []

    def _make_worker(model_id: str, num_threads: int = 4, *, hotwords: str = "", language: str = ""):
        w = _ControlledLoadWorker(model_id, num_threads, hotwords=hotwords, language=language)
        workers.append(w)
        return w

    overrides = {"stt.model_id": "funasr-nano", **(config_overrides or {})}
    with (
        patch("sayink.speech_recognizer.ModelLoadWorker", side_effect=_make_worker),
        patch("sayink.speech_recognizer.has_ram_for_warm_swap", return_value=True),
        app_harness(overrides) as h,
    ):
        app = h["app"]
        real_sr = SpeechRecognizer()
        app._recognizer = real_sr
        h["recognizer"] = real_sr
        real_sr.final_result.connect(app._on_final_result)
        real_sr.meeting_result.connect(app._on_speaker_turn_result)
        real_sr.partial_result.connect(app._on_partial_result)
        real_sr.error.connect(app._on_recognizer_error)
        real_sr.ready.connect(app._on_stt_ready)
        real_sr.model_load_progress.connect(app._on_model_load_progress)

        h["floating"].reset_mock()
        h["tray"].reset_mock()
        app._configure_stt()
        h["workers"] = workers
        yield h
        real_sr.shutdown(wait_ms=500)


# ── 1. RecognitionSettings & App fine-grained diffing ─────────────────────


def test_recognition_settings_keys_isolate_variants_hotwords_speaker_and_vad():
    base = RecognitionSettings(
        model_id="funasr-nano",
        glossary=[{"term": "SayInk", "variants": ["say ink"]}],
        language="auto",
        vad="silero",
        speaker_model_enabled=True,
        speaker_activity_enabled=False,
        quiet_speech_enabled=False,
        boundary_context_enabled=False,
    )
    variant_edited = RecognitionSettings(
        model_id="funasr-nano",
        glossary=[{"term": "SayInk", "variants": ["say ink", "セイインク", "塞音"]}],
        language="auto",
        vad="silero",
    )
    assert variant_edited.asr_key(4) == base.asr_key(4)
    assert variant_edited.speaker_key == base.speaker_key
    assert variant_edited.segmenter_key == base.segmenter_key

    hotword_edited = RecognitionSettings(
        model_id="funasr-nano",
        glossary=[{"term": "SayInk", "variants": []}, {"term": "SherpaONNX", "variants": []}],
    )
    assert hotword_edited.asr_key(4) != base.asr_key(4)
    assert hotword_edited.speaker_key == base.speaker_key
    assert hotword_edited.segmenter_key == base.segmenter_key

    # Models that do not support decoder hotwords keep effective_hotwords empty.
    ctc_a = RecognitionSettings(model_id="fireredasr2", glossary=[{"term": "Foo", "variants": []}])
    ctc_b = RecognitionSettings(model_id="fireredasr2", glossary=[{"term": "Bar", "variants": ["巴"]}])
    assert ctc_a.effective_hotwords == ""
    assert ctc_b.effective_hotwords == ""
    assert ctc_a.asr_key(4) == ctc_b.asr_key(4)


def test_variant_only_edit_applies_in_zero_seconds_without_reloading_stt_or_embedder():
    initial_glossary = [{"term": "SayInk", "variants": ["say ink"]}]
    with app_harness(
        {
            "audio.trigger_mode": "hotkey",
            "stt.model_id": "funasr-nano",
            "stt.glossary": initial_glossary,
        }
    ) as h:
        app = h["app"]
        embedder_before = app._speaker_embedder()
        h["recognizer"].configure.reset_mock()
        h["recorder"].set_segmenter_profile.reset_mock()
        h["floating"].reset_mock()
        h["tray"].reset_mock()

        # Edit ONLY right-hand variants (`词=误写`).
        app._config.set(
            "stt.glossary",
            [{"term": "SayInk", "variants": ["say ink", "塞音客"]}],
        )
        app._on_recognition_settings_changed()

        assert not app._recognition_change_pending
        h["recognizer"].configure.assert_not_called()
        h["recorder"].set_segmenter_profile.assert_not_called()
        assert app._embedder is embedder_before
        assert app._speaker_embedder() is embedder_before
        h["floating"].show_model_loading.assert_not_called()

        # New variant replacement takes effect immediately on the next recognition result.
        app._on_recording_finished(np.ones(16000, dtype=np.float32))
        app._on_final_result("欢迎使用塞音客输入法")
        assert h["paster"].paste_async.call_args.args[0] == "欢迎使用SayInk输入法"


def test_speaker_and_vad_are_isolated_from_glossary_and_language_changes():
    with app_harness({"stt.model_id": "funasr-nano", "stt.vad": "silero"}) as h:
        app = h["app"]
        embedder_before = app._speaker_embedder()
        h["recognizer"].configure.reset_mock()
        h["recorder"].set_segmenter_profile.reset_mock()

        # 1) Canonical hotword change -> reconfigures STT only; keeps embedder & segmenter.
        app._config.set("stt.glossary", [{"term": "FunASR", "variants": []}])
        app._on_recognition_settings_changed()
        h["recognizer"].configure.assert_called_once()
        h["recorder"].set_segmenter_profile.assert_not_called()
        assert app._embedder is embedder_before

        # 2) Language change -> reconfigures STT only; keeps embedder & segmenter.
        h["recognizer"].configure.reset_mock()
        app._config.set("stt.language", "en")
        app._on_recognition_settings_changed()
        h["recognizer"].configure.assert_called_once()
        h["recorder"].set_segmenter_profile.assert_not_called()
        assert app._embedder is embedder_before

        # 3) Segmenter policy change -> rebuilds segmenter only; keeps embedder & STT.
        h["recognizer"].configure.reset_mock()
        app._config.set("stt.debug_force_quiet_speech", False)
        app._on_recognition_settings_changed()
        h["recorder"].set_segmenter_profile.assert_called_once()
        h["recognizer"].configure.assert_not_called()
        assert app._embedder is embedder_before

        # 4) Speaker policy override -> rebuilds embedder only; keeps segmenter & STT.
        h["recorder"].set_segmenter_profile.reset_mock()
        app._config.set("stt.debug_force_speaker_model", True)
        app._on_recognition_settings_changed()
        assert app._embedder is not embedder_before
        h["recorder"].set_segmenter_profile.assert_not_called()
        h["recognizer"].configure.assert_not_called()


# ── 2. Silent warm hot-swap for Fun-ASR-Nano hotword updates ──────────────


def test_funasr_hotword_change_triggers_silent_warm_swap_without_interrupting_recognition():
    with _real_sr_app_harness(
        {
            "audio.trigger_mode": "hotkey",
            "stt.glossary": [{"term": "Foo", "variants": []}],
        }
    ) as h:
        app = h["app"]
        sr: SpeechRecognizer = h["recognizer"]
        workers: list[_ControlledLoadWorker] = h["workers"]

        # Finish initial cold load.
        assert len(workers) == 1
        rec_v1 = _FakeOfflineRecognizer("v1", model_id="funasr-nano", hotwords="Foo", language="中文")
        workers[0].complete(rec_v1)
        assert sr.is_ready is True
        assert sr.is_loading is False
        assert sr.loaded_target == ("funasr-nano", 4, "Foo", "中文")

        embedder_before = app._speaker_embedder()
        h["floating"].reset_mock()
        h["tray"].reset_mock()
        h["recorder"].set_segmenter_profile.reset_mock()
        h["recorder"].start.reset_mock()

        # Modify canonical hotwords on ready Fun-ASR-Nano.
        app._config.set(
            "stt.glossary",
            [{"term": "Foo", "variants": []}, {"term": "Bar", "variants": []}],
        )
        app._on_recognition_settings_changed()

        assert len(workers) == 2
        warm_worker = workers[1]
        assert warm_worker.isRunning() is True
        assert warm_worker.hotwords == "Foo,Bar"

        # During the 7s background build: recognizer stays ready & non-blocking, UI stays silent.
        assert sr.is_ready is True
        assert sr.is_loading is False
        assert sr.is_silent_loading is True
        assert sr.loaded_target == ("funasr-nano", 4, "Foo", "中文")
        h["floating"].show_model_loading.assert_not_called()
        h["recorder"].set_segmenter_profile.assert_not_called()
        assert app._embedder is embedder_before

        # Pressing hotkey during warm-swap starts recording normally (never blocked by loading HUD).
        app._on_recording_start()
        h["recorder"].start.assert_called_once_with(continuous=False)
        h["floating"].show_model_loading.assert_not_called()
        h["recorder"].is_recording = False

        # Transcribing during warm-swap uses the resident v1 recognizer cleanly.
        app._on_recording_finished(np.ones(16000, dtype=np.float32))
        assert sr._current_worker is not None
        sr._current_worker.wait(2000)
        from PyQt6.QtWidgets import QApplication

        QApplication.processEvents()
        assert rec_v1.decode_calls >= 1
        assert h["paster"].paste_async.call_args.args[0] == "decoded-v1"
        h["paster"].paste_async.call_args.args[1]("sent")

        # Complete warm-swap: atomically swaps in v2 with zero "已就绪" floating bar or tray balloon.
        h["floating"].reset_mock()
        h["tray"].reset_mock()
        rec_v2 = _FakeOfflineRecognizer("v2", model_id="funasr-nano", hotwords="Foo,Bar", language="中文")
        warm_worker.complete(rec_v2)

        assert sr._recognizer is rec_v2
        assert sr.loaded_target == ("funasr-nano", 4, "Foo,Bar", "中文")
        assert sr.is_ready is True
        assert sr.is_loading is False
        assert sr.is_silent_loading is False
        assert sr.last_ready_silent is True
        h["floating"].show_model_loading.assert_not_called()
        h["floating"].show_success.assert_not_called()
        h["tray"].showMessage.assert_not_called()
        assert app._embedder is embedder_before

        # Subsequent transcription uses v2.
        app._on_recording_start()
        h["recorder"].is_recording = False
        app._on_recording_finished(np.ones(16000, dtype=np.float32))
        assert sr._current_worker is not None
        sr._current_worker.wait(2000)
        QApplication.processEvents()
        assert rec_v2.decode_calls >= 1
        assert h["paster"].paste_async.call_args.args[0] == "decoded-v2"


# ── 3. Cold start and model_id / language switch keep visible load & ready UI ──


@pytest.mark.parametrize(
    ("config_key", "new_value"),
    [
        ("stt.model_id", "sense-voice"),
        ("stt.language", "en"),
        ("stt.num_threads", 8),
    ],
)
def test_cold_start_and_non_hotword_switches_show_loading_and_ready_notifications(config_key, new_value):
    with _real_sr_app_harness({"audio.trigger_mode": "hotkey"}) as h:
        app = h["app"]
        sr: SpeechRecognizer = h["recognizer"]
        workers: list[_ControlledLoadWorker] = h["workers"]

        # Cold start: not ready, loading HUD shown, ready toast + tray shown on completion.
        assert sr.is_ready is False
        assert sr.is_loading is True
        assert sr.is_silent_loading is False
        assert h["floating"].show_model_loading.called

        workers[0].complete(_FakeOfflineRecognizer("cold"))
        assert sr.is_ready is True
        assert sr.last_ready_silent is False
        h["floating"].show_success.assert_called_once()
        assert h["floating"].show_success.call_args.args[0] == "已就绪"
        h["tray"].showMessage.assert_called_once()

        # Switch model_id / language / num_threads: blocking visible reload + ready notifications.
        h["floating"].reset_mock()
        h["tray"].reset_mock()
        app._config.set(config_key, new_value)
        if config_key == "stt.model_id":
            app._on_models_changed()
        else:
            app._configure_stt() if config_key == "stt.num_threads" else app._on_recognition_settings_changed()

        assert len(workers) == 2
        assert sr.is_ready is False
        assert sr.is_loading is True
        assert sr.is_silent_loading is False
        assert h["floating"].show_model_loading.called

        workers[1].complete(_FakeOfflineRecognizer("switched"))
        assert sr.is_ready is True
        assert sr.is_loading is False
        assert sr.last_ready_silent is False
        h["floating"].show_success.assert_called_once()
        assert h["floating"].show_success.call_args.args[0] == "已就绪"
        h["tray"].showMessage.assert_called_once()


# ── 4. Edge cases A, B, and D ─────────────────────────────────────────────


def test_case_a_reverting_hotwords_mid_warm_swap_discards_build_and_spawns_no_extra_reload():
    with _real_sr_app_harness(
        {
            "stt.glossary": [{"term": "Alpha", "variants": []}],
        }
    ) as h:
        app = h["app"]
        sr: SpeechRecognizer = h["recognizer"]
        workers: list[_ControlledLoadWorker] = h["workers"]

        rec_alpha = _FakeOfflineRecognizer("alpha")
        workers[0].complete(rec_alpha)
        h["floating"].reset_mock()
        h["tray"].reset_mock()

        # Start warm-swap to "Alpha,Beta".
        app._config.set(
            "stt.glossary",
            [{"term": "Alpha", "variants": []}, {"term": "Beta", "variants": []}],
        )
        app._on_recognition_settings_changed()
        assert len(workers) == 2
        assert sr.is_silent_loading is True

        # Revert back to "Alpha" BEFORE worker 1 finishes.
        app._config.set("stt.glossary", [{"term": "Alpha", "variants": []}])
        app._on_recognition_settings_changed()
        assert sr._reload_after_current is False
        assert sr.is_ready is True
        assert sr.is_loading is False

        # Stale worker finishes: intermediate recognizer is discarded, no follow-up load starts.
        stale_rec = _FakeOfflineRecognizer("stale-alpha-beta")
        workers[1].complete(stale_rec)

        assert len(workers) == 2
        assert sr._recognizer is rec_alpha
        assert sr.loaded_target == ("funasr-nano", 4, "Alpha", "中文")
        assert sr.is_ready is True
        assert sr.is_loading is False
        assert sr.is_silent_loading is False
        h["floating"].show_model_loading.assert_not_called()
        h["floating"].show_success.assert_not_called()
        h["tray"].showMessage.assert_not_called()


@pytest.mark.parametrize(
    ("interrupt_key", "interrupt_val", "expected_model"),
    [
        ("stt.model_id", "sense-voice", "sense-voice"),
        ("stt.language", "en", "funasr-nano"),
    ],
)
def test_case_b_switching_model_or_language_mid_warm_swap_degrades_to_visible_reload(
    interrupt_key, interrupt_val, expected_model
):
    with _real_sr_app_harness(
        {
            "audio.trigger_mode": "hotkey",
            "stt.glossary": [{"term": "Alpha", "variants": []}],
        }
    ) as h:
        app = h["app"]
        sr: SpeechRecognizer = h["recognizer"]
        workers: list[_ControlledLoadWorker] = h["workers"]

        rec_initial = _FakeOfflineRecognizer("initial")
        workers[0].complete(rec_initial)
        h["floating"].reset_mock()
        h["tray"].reset_mock()

        # Start silent warm-swap.
        app._config.set(
            "stt.glossary",
            [{"term": "Alpha", "variants": []}, {"term": "Beta", "variants": []}],
        )
        app._on_recognition_settings_changed()
        assert len(workers) == 2
        assert sr.is_ready is True
        assert sr.is_silent_loading is True
        h["floating"].show_model_loading.assert_not_called()

        # Mid warm-swap, user switches model_id or language -> degrades immediately!
        app._config.set(interrupt_key, interrupt_val)
        if interrupt_key == "stt.model_id":
            app._on_models_changed()
        else:
            app._on_recognition_settings_changed()

        assert sr.is_ready is False
        assert sr.is_loading is True
        assert sr.is_silent_loading is False
        assert sr._reload_after_current is True
        assert h["floating"].show_model_loading.called

        # Warm-swap worker finishes -> discarded; queued visible reload worker starts.
        workers[1].complete(_FakeOfflineRecognizer("discarded-warm"))
        assert len(workers) == 3
        assert workers[2].model_id == expected_model
        assert sr.is_ready is False
        assert sr.is_loading is True

        # Queued reload worker finishes -> swaps in new recognizer and shows normal ready UI.
        rec_final = _FakeOfflineRecognizer("final")
        workers[2].complete(rec_final)
        assert sr._recognizer is rec_final
        assert sr.is_ready is True
        assert sr.is_loading is False
        assert sr.last_ready_silent is False
        h["floating"].show_success.assert_called_once()
        assert h["floating"].show_success.call_args.args[0] == "已就绪"
        h["tray"].showMessage.assert_called_once()


def test_case_d_warm_swap_worker_error_keeps_previous_recognizer_alive_and_silent():
    phases: list[tuple[ModelLoadPhase, str]] = []
    with _real_sr_app_harness(
        {
            "stt.glossary": [{"term": "Alpha", "variants": []}],
        }
    ) as h:
        app = h["app"]
        sr: SpeechRecognizer = h["recognizer"]
        sr.model_load_progress.connect(lambda phase, msg: phases.append((phase, msg)))
        workers: list[_ControlledLoadWorker] = h["workers"]

        rec_alpha = _FakeOfflineRecognizer("alpha")
        workers[0].complete(rec_alpha)
        phases.clear()
        h["floating"].reset_mock()
        h["tray"].reset_mock()

        # Trigger warm-swap that unexpectedly fails in background thread.
        app._config.set(
            "stt.glossary",
            [{"term": "Alpha", "variants": []}, {"term": "Beta", "variants": []}],
        )
        app._on_recognition_settings_changed()
        assert len(workers) == 2
        workers[1].fail("模型加载失败: OOM")

        # Existing recognizer stays ready and operational; no FAILED signal or error toast.
        assert sr.is_ready is True
        assert sr.is_loading is False
        assert sr.is_silent_loading is False
        assert sr._recognizer is rec_alpha
        assert sr.current_hotwords == "Alpha"
        assert phases == []
        h["floating"].show_error.assert_not_called()
        h["tray"].showMessage.assert_not_called()


def test_real_qthread_model_load_worker_silent_warm_swap(monkeypatch):
    """Verify real ``ModelLoadWorker`` QThread execution during silent warm hot-swap."""
    import threading
    from PyQt6.QtWidgets import QApplication
    from sayink import speech_recognizer as sr_mod

    monkeypatch.setattr(sr_mod, "is_model_downloaded", lambda _mid: True)
    monkeypatch.setattr(sr_mod, "has_ram_for_warm_swap", lambda _mid: True)
    gate = threading.Event()
    started = threading.Event()

    def _fake_create(model_id: str, num_threads: int, *, hotwords: str = "", language: str = ""):
        if hotwords == "Foo,Bar":
            started.set()
            gate.wait(timeout=2.0)
        return _FakeOfflineRecognizer(
            f"{model_id}:{hotwords}", model_id=model_id, hotwords=hotwords, language=language,
        )

    monkeypatch.setattr(sr_mod, "_create_recognizer", _fake_create)

    sr = SpeechRecognizer()
    phases: list[ModelLoadPhase] = []
    sr.model_load_progress.connect(lambda phase, _msg: phases.append(phase))
    try:
        sr.configure("funasr-nano", 4, hotwords="Foo", language="zh")
        assert sr._load_worker is not None
        sr._load_worker.wait(2000)
        QApplication.processEvents()
        assert sr.is_ready is True
        assert phases == [ModelLoadPhase.LOADING, ModelLoadPhase.READY]
        phases.clear()

        # Trigger silent warm hot-swap on real QThread.
        sr.configure("funasr-nano", 4, hotwords="Foo,Bar", language="zh")
        assert started.wait(timeout=2.0)
        assert sr.is_ready is True
        assert sr.is_loading is False
        assert sr.is_silent_loading is True
        assert phases == []

        gate.set()
        if sr._load_worker is not None:
            sr._load_worker.wait(2000)
        QApplication.processEvents()

        assert sr.is_ready is True
        assert sr.is_loading is False
        assert sr.is_silent_loading is False
        assert sr.last_ready_silent is True
        assert sr.loaded_target == ("funasr-nano", 4, "Foo,Bar", "中文")
        assert phases == []
    finally:
        gate.set()
        sr.shutdown(wait_ms=1000)


# ── 4. Pre-commit safety guards (RAM, recovery dialog, shutdown races) ─────


def test_has_ram_for_warm_swap_checks_windows_available_physical_ram(monkeypatch):
    import types
    from sayink import speech_recognizer as sr_mod

    monkeypatch.setattr(sr_mod.sys, "platform", "win32")
    avail_holder = {"bytes": 8 * 1024 * 1024 * 1024, "ok": True}

    def _fake_mem_status(ptr):
        if not avail_holder["ok"]:
            return 0
        # Access underlying structure via ctypes pointer._obj
        ptr._obj.ullAvailPhys = avail_holder["bytes"]
        return 1

    fake_kernel = types.SimpleNamespace(GlobalMemoryStatusEx=_fake_mem_status)
    monkeypatch.setattr(
        sr_mod.ctypes if hasattr(sr_mod, "ctypes") else __import__("ctypes"),
        "windll",
        types.SimpleNamespace(kernel32=fake_kernel),
        raising=False,
    )

    assert sr_mod.available_physical_ram_bytes() == 8 * 1024 * 1024 * 1024
    assert sr_mod.has_ram_for_warm_swap("funasr-nano") is True
    assert sr_mod.has_ram_for_warm_swap("qwen3-asr-1.7b") is True

    # 3.0 GiB free: enough for Fun-ASR-Nano (950 MB * 2.2 ≈ 2.04 GiB),
    # NOT enough for Qwen3-ASR 1.7B (2400 MB * 2.2 ≈ 5.16 GiB).
    avail_holder["bytes"] = 3 * 1024 * 1024 * 1024
    assert sr_mod.has_ram_for_warm_swap("funasr-nano") is True
    assert sr_mod.has_ram_for_warm_swap("qwen3-asr-1.7b") is False

    # 1.2 GiB free: not enough even for Fun-ASR-Nano warm-swap.
    avail_holder["bytes"] = int(1.2 * 1024 * 1024 * 1024)
    assert sr_mod.has_ram_for_warm_swap("funasr-nano") is False

    # API failure or non-Windows platform degrades permissively rather than blocking.
    avail_holder["ok"] = False
    assert sr_mod.available_physical_ram_bytes() is None
    assert sr_mod.has_ram_for_warm_swap("qwen3-asr-1.7b") is True


def test_low_ram_hotword_edit_releases_resident_recognizer_and_falls_back_to_visible_reload():
    phases: list[tuple[ModelLoadPhase, str]] = []
    with _real_sr_app_harness(
        {
            "stt.glossary": [{"term": "Alpha", "variants": []}],
        }
    ) as h:
        app = h["app"]
        sr: SpeechRecognizer = h["recognizer"]
        sr.model_load_progress.connect(lambda phase, msg: phases.append((phase, msg)))
        workers: list[_ControlledLoadWorker] = h["workers"]

        rec_alpha = _FakeOfflineRecognizer("alpha")
        workers[0].complete(rec_alpha)
        phases.clear()
        h["floating"].reset_mock()
        h["tray"].reset_mock()

        # Simulate low physical RAM: warm-swap must NOT hold two recognizers in memory.
        with patch("sayink.speech_recognizer.has_ram_for_warm_swap", return_value=False):
            app._config.set(
                "stt.glossary",
                [{"term": "Alpha", "variants": []}, {"term": "Beta", "variants": []}],
            )
            app._on_recognition_settings_changed()

        assert len(workers) == 2
        assert sr._recognizer is None
        assert sr.loaded_target is None
        assert sr.is_ready is False
        assert sr.is_loading is True
        assert sr.is_silent_loading is False
        assert [p for p, _ in phases] == [ModelLoadPhase.LOADING]
        assert h["floating"].show_model_loading.called

        rec_beta = _FakeOfflineRecognizer("alpha-beta")
        workers[1].complete(rec_beta)
        assert sr._recognizer is rec_beta
        assert sr.is_ready is True
        assert sr.last_ready_silent is False
        assert [p for p, _ in phases] == [ModelLoadPhase.LOADING, ModelLoadPhase.READY]
        h["floating"].show_success.assert_called_once()


def test_audio_recovery_opens_during_idle_silent_reload_and_blocks_during_active_transcribe():
    from unittest.mock import MagicMock

    with _real_sr_app_harness(
        {
            "stt.glossary": [{"term": "Alpha", "variants": []}],
        }
    ) as h:
        app = h["app"]
        sr: SpeechRecognizer = h["recognizer"]
        workers: list[_ControlledLoadWorker] = h["workers"]

        rec_alpha = _FakeOfflineRecognizer("alpha")
        workers[0].complete(rec_alpha)
        h["tray"].reset_mock()

        # Start an idle background silent hotword reload.
        app._config.set(
            "stt.glossary",
            [{"term": "Alpha", "variants": []}, {"term": "Beta", "variants": []}],
        )
        app._on_recognition_settings_changed()
        assert sr.is_silent_loading is True
        assert sr.running_workers == (workers[1],)
        assert sr.running_transcribe_workers == ()

        created_dialogs = []

        class _FakeDialog:
            def __init__(self, path, *, recognizer=None, **_kwargs):
                self.path = path
                self.recognizer = recognizer
                created_dialogs.append(self)

            def exec(self):
                return 0

        with patch("sayink.ui.audio_recovery_dialog.AudioRecoveryDialog", _FakeDialog):
            app._show_audio_recovery()

        assert len(created_dialogs) == 1
        assert created_dialogs[0].recognizer is rec_alpha
        h["tray"].showMessage.assert_not_called()

        # If a TranscribeWorker IS running, AudioRecoveryDialog must still be blocked.
        fake_transcribe = MagicMock()
        fake_transcribe.isRunning.return_value = True
        sr._workers.append(fake_transcribe)
        try:
            with patch("sayink.ui.audio_recovery_dialog.AudioRecoveryDialog", _FakeDialog):
                app._show_audio_recovery()
            assert len(created_dialogs) == 1
            h["tray"].showMessage.assert_called_once()
            assert "请先结束当前录音" in h["tray"].showMessage.call_args.args[1]
        finally:
            sr._workers.remove(fake_transcribe)


def test_shutdown_hide_glossary_flush_persists_config_without_spawning_load_worker():
    from PyQt6.QtWidgets import QWidget

    with _real_sr_app_harness(
        {
            "stt.glossary": [{"term": "Alpha", "variants": []}],
        }
    ) as h:
        app = h["app"]
        workers: list[_ControlledLoadWorker] = h["workers"]
        workers[0].complete(_FakeOfflineRecognizer("alpha"))
        assert len(workers) == 1

        events: list[str] = []
        h["config"].save_immediate.side_effect = lambda: events.append("save_immediate")

        class _FakeSettingsPanel:
            running_workers = ()

            def cancel_all_downloads(self):
                pass

        class _FakeHistoryPanel:
            def flush_pending_delete(self):
                pass

        class _FakeMain(QWidget):
            def __init__(self):
                super().__init__()
                self.settings_panel = _FakeSettingsPanel()
                self.history_panel = _FakeHistoryPanel()
                self.meeting_panel = None

            def hide(self):
                # Mirror SettingsWindow.hideEvent -> _flush_meeting_glossary():
                # persists stt.glossary to config and emits recognition_changed.
                app._config.set(
                    "stt.glossary",
                    [{"term": "Alpha", "variants": []}, {"term": "ShutdownTerm", "variants": []}],
                )
                events.append("glossary_flushed_to_config")
                app._on_recognition_settings_changed()
                super().hide()

        fake_main = _FakeMain()
        app._main = fake_main
        try:
            with patch("sayink.app.QApplication.quit"):
                app._finish_quit()
            # Config keeps the flushed glossary and saves it immediately, while
            # _exit_started prevents spawning a brand-new 950 MB ModelLoadWorker!
            assert len(workers) == 1
            assert [item["term"] for item in app._config.get("stt.glossary")] == [
                "Alpha",
                "ShutdownTerm",
            ]
            assert events == ["glossary_flushed_to_config", "save_immediate"]
        finally:
            app._main = None
            fake_main.deleteLater()


def test_quit_during_idle_silent_reload_skips_confirmation_dialog():
    with _real_sr_app_harness(
        {
            "stt.glossary": [{"term": "Alpha", "variants": []}],
        }
    ) as h:
        app = h["app"]
        sr: SpeechRecognizer = h["recognizer"]
        workers: list[_ControlledLoadWorker] = h["workers"]
        workers[0].complete(_FakeOfflineRecognizer("alpha"))

        app._config.set(
            "stt.glossary",
            [{"term": "Alpha", "variants": []}, {"term": "Beta", "variants": []}],
        )
        app._on_recognition_settings_changed()
        assert sr.is_silent_loading is True

        with (
            patch("sayink.app.QMessageBox") as boxes,
            patch("sayink.app.QTimer.singleShot"),
            patch("sayink.app.QApplication.quit"),
        ):
            app._quit()
            boxes.assert_not_called()
        assert app._exit_started is True



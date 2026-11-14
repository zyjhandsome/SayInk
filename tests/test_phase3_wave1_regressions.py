"""Focused deterministic regression tests for Phase 3 Wave 1 fixes (#1-#11)."""

from __future__ import annotations

from dataclasses import replace
from email.message import Message
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.request import Request

import numpy as np
import pytest
from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QFileDialog

from sayink.audio_devices import (
    INPUT_SOURCE_MIXED,
    INPUT_SOURCE_SYSTEM,
    AudioDeviceInfo,
    RecordingPlan,
    StreamEndpoint,
)
from sayink.audio_recorder import AudioRecorder, TARGET_SAMPLE_RATE, _CaptureLane
from sayink.audio_utils import LaneBalancer
from sayink.config import Config
from sayink.history_store import HistoryStore, SegmentRecord, SessionSummary
from sayink.meeting_session import MeetingController
from sayink.minutes_generator import MinutesRequest, request_fingerprint
from sayink.minutes_prompts import (
    BUILTIN_TEMPLATES,
    GENERAL_TEMPLATE_ID,
    REFINE_TEMPLATE_ID,
    MeetingContext,
    TranscriptLine,
)
from sayink.recognition_settings import RecognitionSettings
from sayink.runtime_status import RuntimeState, RuntimeStatus
from sayink.speaker_embedding import make_embedder
from sayink.speech_recognizer import MODEL_REGISTRY, SpeechRecognizer
from sayink.text_paster import (
    ClipboardSnapshot,
    capture_win32_clipboard_snapshot,
    restore_win32_clipboard_snapshot,
)
from sayink.text_polisher import (
    LLMResponseError,
    chat_completion,
    http_status_message,
    stream_chat_completion,
)
from sayink.ui import design_tokens as tok
from sayink.ui.floating_window import BAR_EXCERPT_WIDTH, BAR_WIDTH, MIN_BAR_WIDTH
from sayink.ui.history_window import (
    HistoryWindow,
    _segment_has_polished,
    _session_has_polished,
)
from sayink.ui.main_window import MainWindow
from sayink.ui.meeting_page import MeetingPage, _AudioPlayerBar
from sayink.ui.settings_pages.model import ModelSettingsPage
from sayink.ui.settings_window import SettingsWindow
from sayink.update_controller import UpdateController
from sayink.updater import UpdateError, _TrustedRedirectHandler, build_update_urlopen
from tests.helpers.app_harness import app_harness


def _get_builtin_template(template_id: str):
    for tmpl in BUILTIN_TEMPLATES:
        if tmpl.id == template_id:
            return tmpl
    raise KeyError(template_id)


def _retire_widget(widget, qapp) -> None:
    for timer in widget.findChildren(QTimer):
        if timer.isActive():
            timer.stop()
    widget.hide()
    widget.deleteLater()
    qapp.processEvents()


# ── #1: Startup prebuild scheduling & model download card sync ──────────────


class TestWave1Item1StartupAndModelCards:
    def test_app_start_schedules_prebuild_main_window_at_250ms(self):
        with app_harness() as h:
            app = h["app"]
            scheduled: list[tuple[int, object]] = []
            app._later = lambda ms, fn: scheduled.append((ms, fn))
            app.start()

            prebuild_calls = [
                ms for ms, fn in scheduled if fn == app._prebuild_main_window
            ]
            assert prebuild_calls == [250]

    def test_on_models_changed_refreshes_prebuilt_hidden_main_window(self):
        with app_harness() as h:
            app = h["app"]
            calls: list[str] = []
            fake_settings = SimpleNamespace(
                _rebuild_model_cards=lambda: calls.append("rebuild_cards"),
                _refresh_about_info=lambda: calls.append("refresh_about"),
            )
            fake_main = SimpleNamespace(
                isVisible=lambda: False,
                settings_panel=fake_settings,
            )
            app._main = fake_main
            coordinator = MagicMock()
            app._reload_coordinator = lambda: coordinator

            app._on_models_changed()

            coordinator.on_models_changed.assert_called_once_with()
            assert calls == ["rebuild_cards", "refresh_about"]

    def test_model_settings_page_cards_match_config_flips_on_download(
        self, qapp, config, monkeypatch
    ):
        active_id = MODEL_REGISTRY[0]["id"]
        config.set("stt.model_id", active_id)
        downloaded_state = {"flag": False}
        monkeypatch.setattr(
            "sayink.speech_recognizer.is_model_downloaded",
            lambda mid: downloaded_state["flag"] if mid == active_id else False,
        )
        page = ModelSettingsPage()
        try:
            status = RuntimeStatus(state=RuntimeState.UNAVAILABLE, label="未下载")
            page.rebuild_cards(config, status)
            assert page.model_cards_downloaded is False
            assert page.cards_match_config(config) is True

            downloaded_state["flag"] = True
            assert page.cards_match_config(config) is False

            page.rebuild_cards(config, RuntimeStatus(state=RuntimeState.READY, label="已就绪"))
            assert page.model_cards_downloaded is True
            assert page.cards_match_config(config) is True
        finally:
            if config._save_timer is not None:
                config._save_timer.stop()
            _retire_widget(page, qapp)


# ── #2: MainWindow Win32 HBRUSH theme sync & single-release finalizer ───────


class TestWave1Item2MainWindowThemeBrush:
    def test_reapply_theme_updates_brush_color_and_destroy_deletes_once(
        self, qapp, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("sayink.ui.main_window.sys.platform", "win32")
        monkeypatch.setattr(SettingsWindow, "_rebuild_model_cards", lambda self: None)
        monkeypatch.setattr(SettingsWindow, "_refresh_about_info", lambda self: None)
        monkeypatch.setattr(SettingsWindow, "_refresh_audio_device_lists", lambda self: None)

        created_brushes: list[tuple[int, int]] = []
        deleted_brushes: list[int] = []
        next_handle = {"id": 100}

        class _FakeGdi32:
            def CreateSolidBrush(self, colorref: int) -> int:
                next_handle["id"] += 1
                handle = next_handle["id"]
                created_brushes.append((handle, int(colorref)))
                return handle

            def DeleteObject(self, handle: int) -> int:
                deleted_brushes.append(int(handle))
                return 1

        class _FakeUser32:
            def GetClientRect(self, _hwnd, _rect) -> int:
                return 0

            def FillRect(self, _hdc, _rect, _brush) -> int:
                return 1

            def GetDC(self, _hwnd) -> int:
                return 0

            def ReleaseDC(self, _hwnd, _hdc) -> int:
                return 1

        fake_gdi = _FakeGdi32()
        fake_user = _FakeUser32()
        monkeypatch.setattr(
            "sayink.ui.main_window._win32_draw",
            lambda: (fake_user, fake_gdi, lambda: SimpleNamespace(), lambda x: x),
        )

        store = HistoryStore(tmp_path / "brush_test.db")
        cfg = Config(config_dir=tmp_path / "cfg")
        tok.activate("light")
        light_bg = QColor(tok.BG)
        expected_light_ref = (
            light_bg.red() | (light_bg.green() << 8) | (light_bg.blue() << 16)
        )

        win = MainWindow(cfg, store)
        try:
            assert win._bg_brush_color == expected_light_ref
            first_brush = win._bg_brush
            assert first_brush is not None

            tok.activate("dark")
            dark_bg = QColor(tok.BG)
            expected_dark_ref = (
                dark_bg.red() | (dark_bg.green() << 8) | (dark_bg.blue() << 16)
            )
            assert expected_dark_ref != expected_light_ref

            win.reapply_theme()
            second_brush = win._bg_brush
            assert win._bg_brush_color == expected_dark_ref
            assert second_brush is not None and second_brush != first_brush
            assert deleted_brushes == [first_brush]

            # Simulate both Qt destroyed signal and weakref finalizer firing:
            # second_brush must be deleted once and never double-freed.
            win.destroyed.emit(win)
            assert deleted_brushes == [first_brush, second_brush]
            assert win._bg_brush is None
            assert win._bg_brush_cell == [None]

            # A subsequent _release_theme_brush() or finalizer invocation is a no-op.
            win._release_theme_brush()
            assert deleted_brushes == [first_brush, second_brush]
        finally:
            tok.activate("light")
            if cfg._save_timer is not None:
                cfg._save_timer.stop()
            _retire_widget(win, qapp)
            store.close(timeout=2.0)
            assert deleted_brushes.count(second_brush) == 1


# ── #3: Two-lane !audible glides seeded gains toward 1.0 & keeps audio ──────


class TestWave1Item3TwoLaneSilenceGainGlide:
    def test_two_lane_inaudible_glides_seeded_toward_one_and_preserves_noise_floor(self):
        recorder = AudioRecorder()
        recorder._input_source = INPUT_SOURCE_MIXED
        recorder._balancer = LaneBalancer()

        mic_dev = AudioDeviceInfo(1, "Mic", "wasapi", TARGET_SAMPLE_RATE, 1, True, False)
        sys_dev = AudioDeviceInfo(2, "Loopback", "wasapi", TARGET_SAMPLE_RATE, 1, False, True)
        mic_lane = _CaptureLane(endpoint=StreamEndpoint("microphone", mic_dev, False))
        sys_lane = _CaptureLane(endpoint=StreamEndpoint("system", sys_dev, True))

        # Low-level floor below _DIGITAL_SILENCE_RMS (1e-4), non-zero.
        n = 1600
        rng = np.random.default_rng(42)
        mic_quiet = rng.uniform(-3e-5, 3e-5, size=n).astype(np.float32)
        sys_quiet = rng.uniform(-2e-5, 2e-5, size=n).astype(np.float32)

        now = 100.0
        mic_lane.pending = mic_quiet.copy()
        sys_lane.pending = sys_quiet.copy()
        mic_lane.last_chunk_at = now
        sys_lane.last_chunk_at = now
        recorder._lanes = [mic_lane, sys_lane]

        # Seed microphone at elevated 3.0x gain; leave system unheard/unseeded.
        recorder._lane_gains = {"microphone": 3.0}

        mixed = recorder._drain_mixed_mono()
        assert mixed is not None
        assert mixed.size == n
        # Microphone gain glided toward 1.0 (3.0 + 0.25 * (1.0 - 3.0) = 2.5), NOT 4.0x!
        assert recorder._lane_gains["microphone"] == pytest.approx(2.5, abs=1e-4)
        # Unheard system lane stays unseeded in _lane_gains.
        assert "system" not in recorder._lane_gains
        # Natural low-level floor is preserved (never replaced by digital np.zeros).
        assert np.any(mixed != 0.0)


# ── #4a: Single-lane loopback stall zero-padding flush & idle stop ──────────


class TestWave1Item4aLoopbackStallFlush:
    def test_single_system_lane_stall_emits_zero_volume_flushes_and_stops_when_idle(
        self, monkeypatch
    ):
        recorder = AudioRecorder()
        recorder._input_source = INPUT_SOURCE_SYSTEM
        recorder._is_recording = True
        recorder._continuous_mode = True
        recorder._is_paused = False
        recorder._last_emitted_volume = 0.35
        recorder._system_level = 0.25

        sys_dev = AudioDeviceInfo(2, "Loopback", "wasapi", TARGET_SAMPLE_RATE, 1, False, True)
        sys_lane = _CaptureLane(endpoint=StreamEndpoint("system", sys_dev, True))
        sys_lane.stream = MagicMock()
        sys_lane.stream.active = True
        sys_lane.last_chunk_at = 10.0
        recorder._lanes = [sys_lane]
        recorder._plan = RecordingPlan(
            input_source=INPUT_SOURCE_SYSTEM,
            endpoints=(sys_lane.endpoint,),
        )
        recorder._last_real_block_at = 10.0

        fed_blocks: list[np.ndarray] = []
        emitted_segments: list[np.ndarray] = []
        volumes: list[float] = []
        lane_levels: list[tuple[float, float]] = []
        recorder.volume_changed.connect(volumes.append)
        recorder.lane_levels_changed.connect(lambda m, s: lane_levels.append((m, s)))

        class _FakeSegmenter:
            def __init__(self) -> None:
                self.in_speech = True
                self._total_samples = 3200
                self._buffer = [np.ones(1600, dtype=np.float32)]

        fake_seg = _FakeSegmenter()
        recorder._segmenter = fake_seg

        def _fake_feed(block: np.ndarray):
            fed_blocks.append(block.copy())
            if len(fed_blocks) >= 2:
                fake_seg.in_speech = False
                fake_seg._total_samples = 0
                fake_seg._buffer.clear()
                return np.ones(3200, dtype=np.float32) * 0.1
            return None

        monkeypatch.setattr(recorder, "_feed_segmenter", _fake_feed)
        monkeypatch.setattr(recorder, "_emit_segment", lambda seg: emitted_segments.append(seg))
        monkeypatch.setattr(recorder, "_poll_system_route_and_silence", lambda: None)
        monkeypatch.setattr(recorder, "_warn_if_silent", lambda: None)

        # Stall >= MIX_WAIT_SEC (0.35s > 0.20s): should feed two 100ms zero blocks & flush.
        clock = {"now": 10.35}
        monkeypatch.setattr("sayink.audio_recorder.time.monotonic", lambda: clock["now"])

        recorder._on_continuous_tick()

        assert volumes == [0.0]
        assert lane_levels == [(0.0, 0.0)]
        assert len(fed_blocks) == 2
        expected_pad = int(recorder.sample_rate * 0.1)
        assert all(b.size == expected_pad and not np.any(b) for b in fed_blocks)
        assert len(emitted_segments) == 1
        assert recorder._last_loopback_pad_at == 0.0

        # Subsequent ticks while idle must NOT keep feeding synthetic zeros.
        clock["now"] = 10.65
        recorder._on_continuous_tick()
        assert len(fed_blocks) == 2


# ── #5: Updater redirect validation & proxy_bypass_suffixes wiring ──────────


class TestWave1Item5UpdaterRedirectsAndBypass:
    @pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
    @pytest.mark.parametrize(
        "bad_target",
        [
            "http://github.com/Sprb1224/SayInk/releases/download/v1.0.0/SayInk-Setup.exe",
            "https://evil.example.com/SayInk-Setup.exe",
        ],
    )
    def test_redirect_handler_rejects_http_and_untrusted_hosts(
        self, code: int, bad_target: str
    ):
        handler = _TrustedRedirectHandler(trusted_only=True)
        req = Request("https://github.com/Sprb1224/SayInk/releases/download/v1.0.0/a.exe")
        headers = Message()
        headers["Location"] = bad_target
        fp = io.BytesIO(b"")

        method = getattr(handler, f"http_error_{code}")
        with pytest.raises(UpdateError):
            method(req, fp, code, "Redirect", headers)
        assert fp.closed

    def test_redirect_handler_allows_trusted_github_asset_and_switches_bypass_hop(
        self, monkeypatch
    ):
        built_openers: list[tuple[bool, str]] = []

        class _FakeResponse:
            def __init__(self, url: str) -> None:
                self._url = url

            def geturl(self) -> str:
                return self._url

        def _fake_build_opener(*handlers):
            has_direct_proxy = any(
                type(h).__name__ == "ProxyHandler" for h in handlers
            )
            redirect_h = next(
                h for h in handlers if isinstance(h, _TrustedRedirectHandler)
            )

            class _Opener:
                def open(self, req, timeout=30):
                    del timeout
                    url = req.full_url
                    built_openers.append((has_direct_proxy, url))
                    if "github.com/" in url:
                        hdrs = Message()
                        hdrs["Location"] = (
                            "https://objects.githubusercontent.com/github-production-release-asset/SayInk-Setup.exe"
                        )
                        return redirect_h.http_error_302(
                            req, io.BytesIO(b""), 302, "Found", hdrs
                        )
                    return _FakeResponse(url)

            return _Opener()

        monkeypatch.setattr("sayink.updater.build_opener", _fake_build_opener)
        open_fn = build_update_urlopen(
            "system",
            proxy_bypass_suffixes="githubusercontent.com",
            trusted_only=True,
        )
        resp = open_fn(
            "https://github.com/Sprb1224/SayInk/releases/download/v1.0.0/SayInk-Setup.exe"
        )
        assert resp.geturl().startswith("https://objects.githubusercontent.com/")
        # Hop 1 (github.com) uses system proxy (False); Hop 2 (githubusercontent.com) switches to direct (True)!
        assert built_openers == [
            (
                False,
                "https://github.com/Sprb1224/SayInk/releases/download/v1.0.0/SayInk-Setup.exe",
            ),
            (
                True,
                "https://objects.githubusercontent.com/github-production-release-asset/SayInk-Setup.exe",
            ),
        ]

    def test_update_controller_forwards_proxy_bypass_suffixes(
        self, config, monkeypatch
    ):
        config.set("llm.proxy_mode", "system")
        config.set("llm.proxy_bypass_suffixes", "github.com,githubusercontent.com")
        captured: dict[str, tuple[str, str]] = {}

        class _FakeCheckWorker:
            def __init__(
                self, _ver, _parent=None, *, proxy_mode="system", proxy_bypass_suffixes=""
            ):
                captured["check"] = (proxy_mode, proxy_bypass_suffixes)
                self.result_ready = SimpleNamespace(connect=lambda _fn: None)
                self.failed = SimpleNamespace(connect=lambda _fn: None)
                self.finished = SimpleNamespace(connect=lambda _fn: None)

            def isRunning(self) -> bool:
                return False

            def start(self) -> None:
                pass

        class _FakeDownloadWorker:
            def __init__(
                self,
                _url,
                _dest,
                _parent=None,
                *,
                expected_size=0,
                sha256="",
                signature_url="",
                proxy_mode="system",
                proxy_bypass_suffixes="",
            ):
                del expected_size, sha256, signature_url
                captured["download"] = (proxy_mode, proxy_bypass_suffixes)
                self.progress = SimpleNamespace(connect=lambda _fn: None)
                self.finished_path = SimpleNamespace(connect=lambda _fn: None)
                self.failed = SimpleNamespace(connect=lambda _fn: None)
                self.finished = SimpleNamespace(connect=lambda _fn: None)

            def isRunning(self) -> bool:
                return False

            def start(self) -> None:
                pass

        monkeypatch.setattr("sayink.updater.UpdateCheckWorker", _FakeCheckWorker)
        monkeypatch.setattr("sayink.updater.UpdateDownloadWorker", _FakeDownloadWorker)
        monkeypatch.setattr("sayink.updater.cleanup_stale_installers", lambda *_a, **_kw: 0)

        ctrl = UpdateController(
            config,
            parent=None,
            status_sink=lambda: None,
            tray=None,
            show_about=lambda: None,
            confirm_exit=lambda: True,
            finish_quit=lambda: None,
            exit_started=lambda: False,
        )
        try:
            ctrl.check(interactive=True)
            ctrl.state = ctrl.state.AVAILABLE
            ctrl.pending_release = SimpleNamespace(
                asset_name="SayInk-Setup-1.2.0.exe",
                asset_url="https://github.com/Sprb1224/SayInk/releases/download/v1.2.0/SayInk-Setup-1.2.0.exe",
                size=1024,
                sha256="a" * 64,
                signature_url="",
            )
            ctrl.install()

            expected = ("system", "github.com,githubusercontent.com")
            assert captured["check"] == expected
            assert captured["download"] == expected
        finally:
            if config._save_timer is not None:
                config._save_timer.stop()
            ctrl.deleteLater()


# ── #6: make_embedder alias & _run_media_import progress/cancel/RAM guard ───


class TestWave1Item6SpeakerAliasAndMediaImport:
    def test_make_embedder_accepts_activity_path_and_activity_model_path_aliases(
        self, tmp_path, monkeypatch
    ):
        model_file = tmp_path / "campplus.onnx"
        model_file.write_bytes(b"onnx")
        act_a = tmp_path / "act_a.onnx"
        act_b = tmp_path / "act_b.onnx"
        act_a.write_bytes(b"a")
        act_b.write_bytes(b"b")

        guard_paths: list[Path] = []

        class _FakeModelEmbedder:
            def __init__(self, path) -> None:
                self.path = Path(path)
                self.activity_mode = ""
                self.activity_guard = None

        monkeypatch.setattr("sayink.speaker_embedding.ModelEmbedder", _FakeModelEmbedder)
        monkeypatch.setattr(
            "sayink.speaker_activity.SpeakerActivityGuard",
            lambda p: guard_paths.append(Path(p)) or f"guard:{Path(p).name}",
        )

        emb1 = make_embedder(
            model_file, enabled=True, activity_enabled=True, activity_path=act_a
        )
        emb2 = make_embedder(
            model_file, enabled=True, activity_enabled=True, activity_model_path=act_b
        )
        emb3 = make_embedder(
            model_file,
            enabled=True,
            activity_enabled=True,
            activity_path=act_a,
            activity_model_path=act_b,
        )
        emb4 = make_embedder(
            model_file,
            enabled=True,
            activity_enabled=True,
            activity_path=None,
            activity_model_path=act_b,
        )

        assert emb1.activity_guard == "guard:act_a.onnx"
        assert emb2.activity_guard == "guard:act_b.onnx"
        assert emb3.activity_guard == "guard:act_a.onnx"
        assert emb4.activity_guard == "guard:act_b.onnx"
        assert guard_paths == [act_a, act_b, act_a, act_b]

    def test_run_media_import_progress_cancel_and_low_ram_fallback(
        self, tmp_path, monkeypatch
    ):
        audio_file = tmp_path / "sample.wav"
        audio_file.write_bytes(b"RIFF")

        with app_harness() as h:
            app = h["app"]
            monkeypatch.setattr("sayink.app.SpeechRecognizer", SpeechRecognizer)

            # Real SpeechRecognizer subclass instance so isinstance(..., SpeechRecognizer) passes.
            class _ResidentRecognizer(SpeechRecognizer):
                current_model_id = "sense-voice"
                is_ready = True

                def __init__(self) -> None:
                    super().__init__()
                    self._recognizer = object()
                    self._decode_lock = MagicMock()

            resident = _ResidentRecognizer()
            try:
                app._recognizer = resident
                app._resolve_media_import_model_id = lambda: "funasr-nano"

                # Part A: Warm-swap allowed -> builds _DedicatedImportRecognizer, forwards 0..30%
                # decode progress, and wires worker._external_cancelled = cancelled.
                monkeypatch.setattr(
                    "sayink.speech_recognizer.has_ram_for_warm_swap", lambda _m: True
                )
                create_calls: list[str] = []
                monkeypatch.setattr(
                    "sayink.speech_recognizer._create_recognizer",
                    lambda mid, *_a, **_kw: create_calls.append(mid) or "native_handle",
                )
                seen_cancelled_hooks: list[object] = []

                class _FakeTranscribeWorker:
                    def __init__(self, *_a, **_kw) -> None:
                        self.native_lock = None
                        self.quiet_speech = False
                        self._external_cancelled = None
                        self.meeting_ready = SimpleNamespace(connect=lambda _fn: None)
                        self.error = SimpleNamespace(connect=lambda _fn: None)

                    def run(self) -> None:
                        seen_cancelled_hooks.append(self._external_cancelled)

                monkeypatch.setattr(
                    "sayink.speech_recognizer.TranscribeWorker", _FakeTranscribeWorker
                )

                def cancel_fn() -> bool:
                    return False

                progress_messages: list[str] = []

                def _fake_recognize_file(_path, *, recognizer, progress_cb=None, **_kw):
                    if progress_cb is not None:
                        progress_cb(0, "正在解码音视频")
                        progress_cb(25, "正在解码音视频")
                    recognizer.decode_meeting_blocking(
                        np.zeros(1600, dtype=np.float32),
                        turn_analyzer=None,
                        cancelled=cancel_fn,
                    )
                    return SimpleNamespace(segments=[], speaker_count=0, error="")

                monkeypatch.setattr(
                    "sayink.media_import.recognize_file", _fake_recognize_file
                )

                app._run_media_import(audio_file, cancel_fn, progress_messages.append)
                assert create_calls == ["funasr-nano"]
                assert progress_messages == [
                    "正在解码音视频（0%）",
                    "正在解码音视频（25%）",
                ]
                assert seen_cancelled_hooks == [cancel_fn]

                # Part B: Low RAM -> falls back to resident self._recognizer without _create_recognizer.
                create_calls.clear()
                used_recognizers: list[object] = []
                monkeypatch.setattr(
                    "sayink.speech_recognizer.has_ram_for_warm_swap", lambda _m: False
                )
                monkeypatch.setattr(
                    "sayink.media_import.recognize_file",
                    lambda _p, *, recognizer, **_kw: used_recognizers.append(recognizer)
                    or "ok",
                )

                result = app._run_media_import(audio_file, cancel_fn, None)
                assert result == "ok"
                assert create_calls == []
                assert used_recognizers == [resident]
            finally:
                resident.deleteLater()


# ── #7a/#7b/#7c: Retired vad config strip, clipboard 3-tuples, run-dev.bat ──


class TestWave1Item7ConfigClipboardAndLauncher:
    def test_retired_vad_keys_stripped_from_stt_and_meeting_on_load(self, tmp_path):
        cfg_dir = tmp_path / ".sayink"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "config.json").write_text(
            json.dumps(
                {
                    "stt": {"vad": "energy", "language": "en"},
                    "meeting": {"vad": "energy", "save_audio": True},
                }
            ),
            encoding="utf-8",
        )
        cfg = Config(config_dir=cfg_dir)
        try:
            raw = cfg.get_all()
            assert "vad" not in raw.get("stt", {})
            assert "vad" not in raw.get("meeting", {})
            assert cfg.get("stt.vad") is None
            assert cfg.get("meeting.vad") is None
            assert RecognitionSettings.from_config(cfg).vad == "silero"
            assert RecognitionSettings.from_config(cfg, meeting=True).vad == "silero"
        finally:
            if cfg._save_timer is not None:
                cfg._save_timer.stop()

    def test_win32_clipboard_snapshot_3tuples_reregister_named_formats_and_accept_2tuples(
        self, monkeypatch
    ):
        monkeypatch.setattr("sayink.text_paster.sys.platform", "win32")
        html_bytes = b"Version:0.9\r\n<html><body>hi</body></html>\x00"
        text_bytes = "你好\x00".encode("utf-16-le")

        old_html_id = 0xC123
        new_html_id = 0xC456

        class _FakeUnicodeBuf:
            def __init__(self, size: int) -> None:
                self.value = ""
                self._size = size

            def __len__(self) -> int:
                return self._size

        class _FakeCtypes:
            @staticmethod
            def create_unicode_buffer(size: int):
                return _FakeUnicodeBuf(size)

            @staticmethod
            def string_at(ptr: int, size: int) -> bytes:
                return mem_blocks[ptr][:size]

            @staticmethod
            def memmove(dst: int, src: bytes, count: int) -> None:
                mem_blocks[dst] = bytes(src[:count])

        mem_blocks: dict[int, bytes] = {
            1: text_bytes,
            2: html_bytes,
        }
        next_alloc = {"id": 100}
        set_calls: list[tuple[int, bytes]] = []
        registered_names: list[str] = []

        class _FakeUser32:
            def OpenClipboard(self, _hwnd) -> int:
                return 1

            def CloseClipboard(self) -> int:
                return 1

            def EmptyClipboard(self) -> int:
                return 1

            def EnumClipboardFormats(self, prev: int) -> int:
                order = {0: 13, 13: old_html_id, old_html_id: 0}
                return order.get(prev, 0)

            def GetClipboardFormatNameW(self, fmt: int, buf, _max_len: int) -> int:
                if fmt == old_html_id:
                    buf.value = "HTML Format"
                    return len(buf.value)
                return 0

            def GetClipboardData(self, fmt: int) -> int:
                return {13: 1, old_html_id: 2}.get(fmt, 0)

            def RegisterClipboardFormatW(self, name: str) -> int:
                registered_names.append(name)
                return new_html_id if name == "HTML Format" else 0

            def SetClipboardData(self, fmt: int, hmem: int) -> int:
                set_calls.append((fmt, mem_blocks[hmem]))
                return hmem

        class _FakeKernel32:
            def GlobalSize(self, handle: int) -> int:
                return len(mem_blocks.get(handle, b""))

            def GlobalLock(self, handle: int) -> int:
                return handle

            def GlobalUnlock(self, _handle: int) -> int:
                return 1

            def GlobalAlloc(self, _flags: int, size: int) -> int:
                next_alloc["id"] += 1
                hid = next_alloc["id"]
                mem_blocks[hid] = b"\x00" * size
                return hid

            def GlobalFree(self, handle: int) -> int:
                mem_blocks.pop(handle, None)
                return 0

        monkeypatch.setattr(
            "sayink.text_paster._win32_clipboard_apis",
            lambda: (_FakeCtypes, _FakeUser32(), _FakeKernel32()),
        )

        snap = capture_win32_clipboard_snapshot()
        assert snap.win32_entries == [
            (13, "", text_bytes),
            (old_html_id, "HTML Format", html_bytes),
        ]

        assert restore_win32_clipboard_snapshot(snap) is True
        assert registered_names == ["HTML Format"]
        assert set_calls == [
            (13, text_bytes),
            (new_html_id, html_bytes),
        ]

        # Legacy 2-tuple compatibility check
        set_calls.clear()
        legacy_snap = ClipboardSnapshot(
            use_win32=True,
            win32_entries=[(13, text_bytes)],  # type: ignore[list-item]
        )
        assert restore_win32_clipboard_snapshot(legacy_snap) is True
        assert set_calls == [(13, text_bytes)]

    def test_run_dev_bat_python_and_pyqt6_fallback_order(self):
        bat_path = Path(__file__).resolve().parent.parent / "run-dev.bat"
        content = bat_path.read_text(encoding="utf-8")
        probes = [
            'if exist ".venv\\Scripts\\python.exe"',
            'python -c "import PyQt6"',
            'py -3.10 -c "import PyQt6"',
            'py -3 -c "import PyQt6"',
        ]
        positions = [content.index(probe) for probe in probes]
        assert positions == sorted(positions)


# ── #8 & #10: HistoryWindow unchanged polish state & JSON exports ───────────


class _MinimalHistoryStore:
    def __init__(
        self, sessions: list[SessionSummary], segments: dict[str, list[SegmentRecord]]
    ) -> None:
        self._sessions = sessions
        self.segments = segments

    def list_sessions(self, limit: int = 50, offset: int = 0) -> list[SessionSummary]:
        return self._sessions[offset : offset + limit]

    def search_sessions(self, q: str, limit: int | None = None) -> list[SessionSummary]:
        del q, limit
        return []

    def get_session_segments(self, session_id: str) -> list[SegmentRecord]:
        return list(self.segments.get(session_id, []))


class TestWave1Item8And10HistoryWindow:
    def test_unchanged_polish_hides_toggle_bar_and_keeps_initial_polish_button_text(
        self, qapp
    ):
        same_seg = SegmentRecord(
            "s_same", 0, 1_700_000_000_000, "今天开会讨论发布计划。", "  今天开会讨论发布计划。  "
        )
        diff_seg = SegmentRecord(
            "s_diff", 0, 1_700_000_100_000, "嗯今天开会讨论发布计划", "今天开会讨论发布计划。"
        )
        assert _segment_has_polished(same_seg) is False
        assert _session_has_polished([same_seg]) is False
        assert _segment_has_polished(diff_seg) is True

        store = _MinimalHistoryStore(
            sessions=[
                SessionSummary("s_same", 1_700_000_000_000, 1, "mic", "Code.exe", "今天开会"),
                SessionSummary("s_diff", 1_700_000_100_000, 1, "mic", "Code.exe", "今天开会"),
            ],
            segments={"s_same": [same_seg], "s_diff": [diff_seg]},
        )
        win = HistoryWindow(store)
        try:
            win.show()
            qapp.processEvents()

            # Select "s_same" (row index 1 since day header is row 0 or check UserRole)
            items = win.session_items()
            same_item = next(
                it for it in items if it.data(0x0100) == "s_same"
            )
            diff_item = next(
                it for it in items if it.data(0x0100) == "s_diff"
            )

            win._session_list.blockSignals(True)
            win._session_list.clearSelection()
            win._session_list.setCurrentItem(same_item)
            same_item.setSelected(True)
            win._session_list.blockSignals(False)
            win._on_selection_changed()
            assert win._view_bar.isHidden() is True
            assert win._polish_btn.text() == "润色"

            win._session_list.blockSignals(True)
            win._session_list.clearSelection()
            win._session_list.setCurrentItem(diff_item)
            diff_item.setSelected(True)
            win._session_list.blockSignals(False)
            win._on_selection_changed()
            assert win._view_bar.isHidden() is False
            assert win._polish_btn.text() == "重新润色"
        finally:
            _retire_widget(win, qapp)

    def test_single_and_batch_export_write_valid_utf8_json(
        self, qapp, tmp_path, monkeypatch
    ):
        s1_seg = SegmentRecord(
            "s1", 0, 1_700_000_000_000, "第一条原始文本", "第一条润色文本", "mic", 1200, "Code.exe"
        )
        s2_seg = SegmentRecord(
            "s2", 0, 1_700_000_100_000, "第二条原始文本", "", "system", 800, "Teams.exe"
        )
        store = _MinimalHistoryStore(
            sessions=[
                SessionSummary("s2", 1_700_000_100_000, 1, "system", "Teams.exe", "第二条"),
                SessionSummary("s1", 1_700_000_000_000, 1, "mic", "Code.exe", "第一条"),
            ],
            segments={"s1": [s1_seg], "s2": [s2_seg]},
        )
        win = HistoryWindow(store)
        seen_filters: list[str] = []
        single_out = tmp_path / "single_export"
        batch_out = tmp_path / "batch_export"
        targets = [str(single_out), str(batch_out)]

        def _fake_save_dialog(_parent, _title, _default_name, file_filter):
            seen_filters.append(file_filter)
            return targets.pop(0), "JSON (*.json)"

        monkeypatch.setattr(QFileDialog, "getSaveFileName", _fake_save_dialog)
        try:
            s1_summary = store._sessions[1]
            win._export_single(s1_summary)
            single_json_path = tmp_path / "single_export.json"
            assert single_json_path.is_file()
            single_payload = json.loads(single_json_path.read_text(encoding="utf-8"))
            assert single_payload["session_id"] == "s1"
            assert single_payload["segments"][0]["polished_text"] == "第一条润色文本"

            # Batch export across both sessions
            win._export_batch(list(store._sessions))

            batch_json_path = tmp_path / "batch_export.json"
            assert batch_json_path.is_file()
            batch_payload = json.loads(batch_json_path.read_text(encoding="utf-8"))
            assert batch_payload["session_count"] == 2
            exported_ids = [s["session_id"] for s in batch_payload["sessions"]]
            assert exported_ids == ["s1", "s2"]
            assert all("JSON (*.json)" in f for f in seen_filters)
        finally:
            _retire_widget(win, qapp)


# ── #9: HTTP 429 quota vs rate-limit & refinement request_fingerprint ───────


class TestWave1Item9Http429AndRefinementFingerprint:
    def test_http_429_distinguishes_quota_from_rate_limit_in_helper_sync_and_stream(self):
        # 1-arg backward compatibility remains rate-limit
        rate_msg = "请求过于频繁（HTTP 429），请稍后重试。"
        quota_msg = "API 额度或余额不足（HTTP 429），请检查账户余额或账单设置。"

        assert http_status_message(429) == rate_msg
        assert (
            http_status_message(429, {"error": {"message": "Rate limit reached for requests"}})
            == rate_msg
        )
        assert (
            http_status_message(
                429,
                {
                    "error": {
                        "code": "insufficient_quota",
                        "message": "You exceeded your current quota, please check your plan and billing details.",
                    }
                },
            )
            == quota_msg
        )
        assert http_status_message(429, {"error": "账户余额不足，请充值"}) == quota_msg

        class _FakeHttpx:
            class NetworkError(Exception):
                pass

            class RemoteProtocolError(Exception):
                pass

            class TimeoutException(Exception):
                pass

        class _FakeResponse:
            def __init__(self, status_code: int, body: dict) -> None:
                self.status_code = status_code
                self._body = body
                self.text = json.dumps(body, ensure_ascii=False)

            def json(self):
                return self._body

            def read(self) -> bytes:
                return self.text.encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        class _FakeClient:
            def __init__(self, body: dict) -> None:
                self._body = body

            def post(self, *_a, **_kw):
                return _FakeResponse(429, self._body)

            def stream(self, *_a, **_kw):
                return _FakeResponse(429, self._body)

        common_kwargs = dict(
            api_url="https://api.example.com/v1",
            api_key="sk-test",
            model_name="gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.2,
            max_tokens=64,
            retry_delay_sec=0.0,
        )

        quota_body = {"error": {"type": "insufficient_quota", "message": "Quota exceeded"}}
        rate_body = {"error": {"type": "rate_limit_error", "message": "Too many requests"}}

        with pytest.raises(LLMResponseError, match="额度或余额不足"):
            chat_completion(_FakeClient(quota_body), _FakeHttpx, **common_kwargs)
        with pytest.raises(LLMResponseError, match="请求过于频繁"):
            chat_completion(_FakeClient(rate_body), _FakeHttpx, **common_kwargs)

        with pytest.raises(LLMResponseError, match="额度或余额不足"):
            stream_chat_completion(_FakeClient(quota_body), _FakeHttpx, **common_kwargs)
        with pytest.raises(LLMResponseError, match="请求过于频繁"):
            stream_chat_completion(_FakeClient(rate_body), _FakeHttpx, **common_kwargs)

    def test_request_fingerprint_ignores_output_language_only_when_is_refinement(self):
        refine_tmpl = _get_builtin_template(REFINE_TEMPLATE_ID)
        general_tmpl = _get_builtin_template(GENERAL_TEMPLATE_ID)
        assert refine_tmpl.is_refinement is True
        assert general_tmpl.is_refinement is False

        base_req = MinutesRequest(
            api_url="https://api.example.com/v1",
            api_key="sk-test",
            model_name="qwen-plus",
            template=refine_tmpl,
            context=MeetingContext(title="周会"),
            lines=[TranscriptLine(offset_ms=0, speaker_id=1, text="讨论本周进度")],
            output_language="zh",
        )
        refine_en = replace(base_req, output_language="en")
        refine_auto = replace(base_req, output_language="auto")
        assert request_fingerprint(base_req) == request_fingerprint(refine_en)
        assert request_fingerprint(base_req) == request_fingerprint(refine_auto)

        general_zh = replace(base_req, template=general_tmpl, output_language="zh")
        general_en = replace(base_req, template=general_tmpl, output_language="en")
        assert request_fingerprint(general_zh) != request_fingerprint(general_en)


# ── #11a/#11b/#11c: Playback temp WAV hash/cleanup, follow label & MASTER ───


class TestWave1Item11MeetingPlaybackFollowLabelAndDocs:
    def test_temp_wav_path_avoids_collisions_across_distinct_folders(self, tmp_path):
        dir_a = tmp_path / "workspace_a" / "journal"
        dir_b = tmp_path / "workspace_b" / "journal"
        dir_a.mkdir(parents=True)
        dir_b.mkdir(parents=True)
        payload = np.zeros(400, dtype="<f4").tobytes()
        f32_a = dir_a / "audio.f32"
        f32_b = dir_b / "audio.f32"
        f32_a.write_bytes(payload)
        f32_b.write_bytes(payload)

        wav_a = _AudioPlayerBar._temp_wav_path_for_raw_f32(f32_a)
        wav_b = _AudioPlayerBar._temp_wav_path_for_raw_f32(f32_b)

        assert wav_a != wav_b
        assert wav_a.name.startswith("journal-") and wav_a.name.endswith(f"-{len(payload)}.wav")
        assert wav_b.name.startswith("journal-") and wav_b.name.endswith(f"-{len(payload)}.wav")

    def test_meeting_delete_and_page_cleanup_remove_temp_playback_wavs(
        self, qapp, config, tmp_path, monkeypatch
    ):
        history = HistoryStore(db_path=tmp_path / "meetings.db")
        controller = MeetingController(config, history, pending_segments=lambda: 0)
        page = MeetingPage(config, controller)

        # Redirect temp playback folder into tmp_path so test leaves zero artifacts in %TEMP%.
        playback_root = tmp_path / "temp_root"
        playback_root.mkdir(parents=True)
        monkeypatch.setattr(
            "sayink.ui.meeting_page.tempfile.gettempdir", lambda: str(playback_root)
        )
        # Stub Qt multimedia setup so offscreen test never initializes native audio sinks.
        monkeypatch.setattr(_AudioPlayerBar, "_ensure_backend", lambda self: False)

        try:
            # 1) Create meeting 1 with journal audio.f32, materialize temp WAV, then delete meeting 1.
            j1 = tmp_path / "m1_journal"
            j1.mkdir()
            (j1 / "audio.f32").write_bytes(np.zeros(800, dtype="<f4").tobytes())
            rec1 = controller.store.create_meeting(title="会议一", audio_path=str(j1))
            controller.store.end_meeting(rec1.id, ended_at=rec1.started_at + 5000)
            controller.store.flush()

            page.show_meeting(rec1.id)
            wav1_str = page._audio_player._materialize_playable_path()
            wav1 = Path(wav1_str)
            assert wav1.is_file()

            # Delete via MeetingPage's deletion cleanup path (collect + delete_meeting + remove).
            targets = page._audio_player.collect_temp_wavs_for_audio_paths([str(j1)])
            assert wav1 in targets
            assert controller.delete_meeting(rec1.id) is True
            page._audio_player.remove_temp_wavs(targets)
            assert not wav1.exists()
            assert page._audio_player._playable_path == ""

            # 2) Create meeting 2, materialize temp WAV, then verify MeetingPage cleanup/destruction deletes it.
            j2 = tmp_path / "m2_journal"
            j2.mkdir()
            (j2 / "audio.f32").write_bytes(np.ones(800, dtype="<f4").tobytes())
            rec2 = controller.store.create_meeting(title="会议二", audio_path=str(j2))
            controller.store.end_meeting(rec2.id, ended_at=rec2.started_at + 5000)
            controller.store.flush()

            page.show_meeting(rec2.id)
            wav2 = Path(page._audio_player._materialize_playable_path())
            assert wav2.is_file()

            # Check _follow_model_label (#11b) on the same MeetingPage before retiring it!
            known = MODEL_REGISTRY[0]
            config.set("stt.model_id", known["id"])
            assert page._follow_model_label() == f"与转写相同（当前：{known['name']}）"
            config.set("stt.model_id", "nonexistent-model-id")
            assert page._follow_model_label() == "与转写相同（当前：未选择）"

            # Trigger page cleanup / destruction hook and verify wav2 is removed.
            page.cleanup()
            assert not wav2.exists()
        finally:
            if config._save_timer is not None:
                config._save_timer.stop()
            _retire_widget(page, qapp)
            controller.shutdown()
            history.close(timeout=2.0)

    def test_design_system_master_documents_480px_floating_bar_and_no_retired_type_tokens(self):
        master_path = Path(__file__).resolve().parent.parent / "design-system" / "MASTER.md"
        master_text = master_path.read_text(encoding="utf-8")

        assert BAR_WIDTH == 480
        assert BAR_EXCERPT_WIDTH == 480
        assert MIN_BAR_WIDTH == 420
        assert "480×44px" in master_text
        assert "BAR_WIDTH = 480" in master_text
        assert "480×68px" in master_text
        assert "BAR_EXCERPT_WIDTH = 480" in master_text
        assert "MIN_BAR_WIDTH = 420" in master_text
        assert "TYPE_ICON_LG" not in master_text
        assert "TYPE_DISPLAY" not in master_text

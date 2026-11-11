"""Batch 3 regression tests for core runtime reliability, latency, and recovery polish."""

from __future__ import annotations

import json
import logging
import threading
import time
from unittest.mock import MagicMock

import numpy as np
import pytest
from pynput import keyboard
from PyQt6.QtTest import QSignalSpy
from PyQt6.QtWidgets import QApplication

from sayink.audio_devices import AudioDeviceInfo, INPUT_SOURCE_MIXED, StreamEndpoint
from sayink.audio_recorder import AudioRecorder, _CaptureLane
from sayink.audio_recovery import (
    RecordingJournal,
    recordings,
    store_pending,
    sweep_orphan_pending,
)
from sayink.config import OUTPUT_MODE_HISTORY, OUTPUT_MODE_PASTE
from sayink.hotkey_manager import HotKeyManager, _SUPPRESSED_REPEAT_MAX_GAP_S
from sayink.speech_recognizer import SpeechRecognizer
from sayink.ui.audio_recovery_dialog import AudioRecoveryDialog
from sayink.vad_segmenter import EnergyGate, VAD_SILERO
from tests.helpers.app_harness import app_harness
from tests.test_app_history_wiring import _start_continuous_user_session
from tests.test_app_meeting_turns import make_context


class _Suppressed(Exception):
    pass


class _WinEvent:
    def __init__(self, vk: int, flags: int = 0, extra=None):
        self.vkCode = vk
        self.flags = flags
        self.dwExtraInfo = extra


class TestHotKeySecureDesktopRecovery:
    """Lost WM_KEYUP across Win+L / UAC must not cause false triggers or eaten keys."""

    VK_X = 0x58
    VK_A = 0x41

    def test_stale_modifier_after_win_l_does_not_trigger_on_bare_letter_via_filter(self, monkeypatch):
        mgr = HotKeyManager("alt+x")
        mgr._listener = MagicMock()
        mgr._listener.suppress_event.side_effect = _Suppressed
        # Simulate Alt pressed before Win+L, whose WM_KEYUP was never delivered.
        mgr._pressed_keys.add(keyboard.Key.alt_l)
        monkeypatch.setattr(mgr, "_modifiers_held", lambda _mods: False)

        # User unlocks desktop and types bare 'x': filter passes it through AND prunes Alt.
        assert mgr._win32_event_filter(0x0100, _WinEvent(self.VK_X)) is True
        mgr._listener.suppress_event.assert_not_called()
        assert keyboard.Key.alt_l not in mgr._pressed_keys

        # pynput subsequently delivers on_press('x'): must NOT arm hold!
        mgr._on_press(keyboard.KeyCode.from_char("x"))
        assert mgr._hold_pending is False
        assert mgr._is_recording is False

    def test_live_async_reconciliation_prunes_ghost_modifier_and_stops_orphaned_hold(self, monkeypatch):
        mgr = HotKeyManager("win+z")
        mgr._verify_async_state = True
        mgr._pressed_keys.update({keyboard.Key.cmd, keyboard.KeyCode.from_char("z")})
        mgr._is_recording = True

        # After returning from Win+L, neither Win nor Z is physically down.
        monkeypatch.setattr(mgr, "_async_key_down", lambda _vk: False)
        stops = []
        starts = []
        mgr.recording_stop.connect(lambda: stops.append(1))
        mgr.recording_start.connect(lambda: starts.append(1))

        # Typing 'z' alone ends the orphaned recording and does NOT start a new hold.
        mgr._on_press(keyboard.KeyCode.from_char("z"))
        assert stops == [1]
        assert mgr._hold_pending is False
        assert starts == []

    def test_stale_suppressed_vk_expires_after_lost_keyup_while_fast_autorepeat_stays_suppressed(
        self, monkeypatch
    ):
        mgr = HotKeyManager("alt+x")
        mgr._listener = MagicMock()
        mgr._listener.suppress_event.side_effect = _Suppressed
        monkeypatch.setattr(mgr, "_send_menu_mask", lambda: None)

        # Press Alt+X: suppressed.
        monkeypatch.setattr(mgr, "_modifiers_held", lambda _mods: True)
        with pytest.raises(_Suppressed):
            mgr._win32_event_filter(0x0104, _WinEvent(self.VK_X))
        assert self.VK_X in mgr._suppressed_vks

        # Immediate auto-repeat right after releasing Alt (< 1.2s) stays suppressed.
        monkeypatch.setattr(mgr, "_modifiers_held", lambda _mods: False)
        with pytest.raises(_Suppressed):
            mgr._win32_event_filter(0x0100, _WinEvent(self.VK_X))

        # Simulate UAC / secure-desktop gap where WM_KEYUP was lost (> 1.2s elapsed).
        mgr._suppressed_vk_at[self.VK_X] -= _SUPPRESSED_REPEAT_MAX_GAP_S + 0.5
        mgr._listener.suppress_event.reset_mock()
        assert mgr._win32_event_filter(0x0100, _WinEvent(self.VK_X)) is True
        mgr._listener.suppress_event.assert_not_called()
        assert self.VK_X not in mgr._suppressed_vks

    def test_intervening_other_key_clears_stale_suppressed_vk_immediately(self, monkeypatch):
        mgr = HotKeyManager("alt+x")
        mgr._listener = MagicMock()
        mgr._listener.suppress_event.side_effect = _Suppressed
        monkeypatch.setattr(mgr, "_send_menu_mask", lambda: None)

        monkeypatch.setattr(mgr, "_modifiers_held", lambda _mods: True)
        with pytest.raises(_Suppressed):
            mgr._win32_event_filter(0x0104, _WinEvent(self.VK_X))
        assert self.VK_X in mgr._suppressed_vks

        # Modifiers up + user taps 'a' -> typematic repeat of 'x' is dead; next 'x' is not eaten.
        monkeypatch.setattr(mgr, "_modifiers_held", lambda _mods: False)
        assert mgr._win32_event_filter(0x0100, _WinEvent(self.VK_A)) is True
        mgr._listener.suppress_event.reset_mock()
        assert mgr._win32_event_filter(0x0100, _WinEvent(self.VK_X)) is True
        mgr._listener.suppress_event.assert_not_called()

    def test_hook_callbacks_defer_logging_off_caller_thread(self):
        app = QApplication.instance() or QApplication([])
        mgr = HotKeyManager("ctrl+space", parent=app)
        caller_threads: list[int] = []

        class _ThreadSpyHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                caller_threads.append(threading.get_ident())

        spy_handler = _ThreadSpyHandler(level=logging.DEBUG)
        sayink_log = logging.getLogger("SayInk")
        old_level = sayink_log.level
        sayink_log.setLevel(logging.DEBUG)
        sayink_log.addHandler(spy_handler)
        hook_tid_box: list[int] = []
        try:
            def _hook_thread_work():
                hook_tid_box.append(threading.get_ident())
                with mgr._lock:
                    mgr._is_recording = True
                    mgr._pressed_keys.update({keyboard.Key.ctrl_l, keyboard.Key.space})
                mgr._on_release(keyboard.Key.space)

            worker = threading.Thread(target=_hook_thread_work)
            worker.start()
            worker.join(timeout=2.0)
            # Before pumping Qt events, zero synchronous log emissions happened on hook thread!
            assert hook_tid_box[0] not in caller_threads
            app.processEvents()
            assert caller_threads and all(tid != hook_tid_box[0] for tid in caller_threads)
        finally:
            sayink_log.removeHandler(spy_handler)
            sayink_log.setLevel(old_level)


class TestSplitLaneSileroGateCaching:
    """_prepare_split_segmenters must cache per-role SileroGate ONNX sessions."""

    def test_split_lanes_reuse_per_role_silero_gates_and_reset_state(self, monkeypatch):
        created = []

        class _FakeSileroGate:
            name = VAD_SILERO
            fallback_reason = ""

            def __init__(self, path: str):
                self.path = path
                self.input_gain = 1.0
                self.resets = 0
                created.append(self)

            def reset(self) -> None:
                self.resets += 1

        def _fake_make_gate(kind, segmenter, *, model_path=None):
            if kind != VAD_SILERO:
                return EnergyGate(segmenter)
            return _FakeSileroGate(str(model_path))

        monkeypatch.setattr("sayink.audio_recorder.make_speech_gate", _fake_make_gate)
        monkeypatch.setattr("sayink.vad_segmenter.make_speech_gate", _fake_make_gate)

        rec = AudioRecorder()
        rec.configure(input_source=INPUT_SOURCE_MIXED)
        rec.set_separate_lanes(True)
        rec.set_segmenter_profile(vad=VAD_SILERO, vad_model_path="C:/models/silero_vad.onnx", meeting=False)
        # 1 primary gate built so far.
        assert len(created) == 1

        rec._lanes = [
            _CaptureLane(
                StreamEndpoint(
                    role="microphone",
                    device=AudioDeviceInfo(1, "Mic", "WASAPI", 16000, 1, False, False),
                    use_wasapi_loopback=False,
                )
            ),
            _CaptureLane(
                StreamEndpoint(
                    role="system",
                    device=AudioDeviceInfo(2, "Loop", "WASAPI", 16000, 2, True, True),
                    use_wasapi_loopback=True,
                )
            ),
        ]
        rec._prepare_split_segmenters()
        assert len(created) == 3  # +1 mic split gate, +1 system split gate
        mic_gate = rec._split_segmenters["microphone"].gate
        sys_gate = rec._split_segmenters["system"].gate
        assert mic_gate is not sys_gate

        # Repeated starts in dictation AND meeting profiles reuse both cached split gates!
        rec._prepare_split_segmenters()
        rec.set_segmenter_profile(vad=VAD_SILERO, vad_model_path="C:/models/silero_vad.onnx", meeting=True)
        rec._prepare_split_segmenters()
        assert len(created) == 3
        assert rec._split_segmenters["microphone"].gate is mic_gate
        assert rec._split_segmenters["system"].gate is sys_gate
        assert mic_gate.resets >= 2
        assert sys_gate.resets >= 2

        # Changing vad_model_path invalidates and loads fresh per-role gates.
        rec.set_segmenter_profile(vad=VAD_SILERO, vad_model_path="C:/models/silero_v2.onnx", meeting=True)
        rec._prepare_split_segmenters()
        assert len(created) == 6


class TestCorruptModelFailureDiagnosis:
    """Zero-startup-cost SHA-256 check runs ONLY when ModelLoadWorker fails."""

    def test_healthy_load_skips_sha256_and_failed_load_reports_corrupt_files(self, tmp_path, monkeypatch):
        from sayink import speech_recognizer as sr

        sha_calls = []

        def _spy_mismatch(info, folder):
            sha_calls.append((info["id"], folder))
            return ["model.int8.onnx"]

        monkeypatch.setattr(sr, "mismatched_model_files", _spy_mismatch)
        model_dir = tmp_path / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
        model_dir.mkdir(parents=True)
        (model_dir / "model.int8.onnx").write_bytes(b"corrupt")
        monkeypatch.setattr(sr, "get_model_dir", lambda _mid: model_dir)

        # 1. Healthy load: zero SHA-256 / mismatched_model_files calls!
        monkeypatch.setattr(sr, "_create_recognizer", lambda *_a, **_kw: object())
        ok_worker = sr.ModelLoadWorker("sensevoice", 4)
        ok_worker.run()
        assert sha_calls == []

        # 2. Failed load with damaged file on disk: surfaces actionable hint!
        def _fail_load(*_a, **_kw):
            raise RuntimeError("onnx protobuf parse failed")

        monkeypatch.setattr(sr, "_create_recognizer", _fail_load)
        bad_worker = sr.ModelLoadWorker("sensevoice", 4)
        errors: list[str] = []
        bad_worker.error.connect(errors.append)
        bad_worker.run()
        assert len(sha_calls) == 1
        assert errors
        assert "模型加载失败" in errors[0]
        assert "model.int8.onnx" in errors[0]
        assert "设置 → 引擎" in errors[0]


class TestUnifiedRecoveryQuotaAndDialogPolish:
    """Unified 2 GiB cap, orphan sweep, glossary post-processing, progress/stop, and _decode_lock."""

    def test_journal_and_pending_share_single_quota_in_both_directions(self, tmp_path):
        root = tmp_path / "audio-recovery"
        errors: list[str] = []
        # Cap total recovery directory at 400 bytes.
        journal = RecordingJournal(root, max_bytes=400, on_error=errors.append)
        try:
            # Write 240 bytes (60 float32 samples) to active journal.
            assert journal.append(np.ones(60, dtype=np.float32))
            deadline = time.monotonic() + 2.0
            while journal.total_recovery_bytes < 240 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert journal.total_recovery_bytes == 240

            # Spilling 200 bytes (50 float32 samples) into journal/pending exceeds 400B combined cap!
            with pytest.raises(OSError, match="上限"):
                store_pending(journal.path / "pending", np.ones(50, dtype=np.float32), max_bytes=400)

            # Spilling 120 bytes (30 samples) fits (240 + 120 = 360 <= 400).
            spilled = store_pending(journal.path / "pending", np.ones(30, dtype=np.float32), max_bytes=400)
            assert journal.total_recovery_bytes == 360

            # Now appending 80 more bytes (20 samples) to journal exceeds 360 + 80 > 400!
            assert journal.append(np.ones(20, dtype=np.float32))
            deadline = time.monotonic() + 2.0
            while not errors and time.monotonic() < deadline:
                time.sleep(0.01)
            assert errors and "上限" in errors[0]
            spilled.delete()
        finally:
            journal.close()

    def test_sweep_orphan_pending_cleans_root_and_crashed_session_pending_only(self, tmp_path):
        root = tmp_path / "audio-recovery"
        root_pending = root / "pending"
        root_pending.mkdir(parents=True)
        (root_pending / "stale1.f32").write_bytes(b"\x00" * 64)
        (root_pending / "keep.txt").write_text("keep", encoding="utf-8")

        crashed = root / ("e" * 32)
        (crashed / "pending").mkdir(parents=True)
        (crashed / "session.json").write_text(
            json.dumps({"sample_rate": 16000, "complete": False, "started_at": time.time()}),
            encoding="utf-8",
        )
        np.ones(100, dtype="<f4").tofile(crashed / "audio.f32")
        (crashed / "pending" / "stale2.f32").write_bytes(b"\x00" * 64)

        removed = sweep_orphan_pending(root)
        assert removed == 2
        assert not (root_pending / "stale1.f32").exists()
        assert (root_pending / "keep.txt").exists()
        assert not (crashed / "pending").exists()
        # Raw journal audio.f32 is untouched!
        assert (crashed / "audio.f32").is_file()
        assert len(recordings(root)) == 1

    def test_recovery_dialog_applies_glossary_tracks_progress_and_serializes_with_decode_lock(
        self, tmp_path
    ):
        from tests.test_asr_short_retry import Recognizer

        app = QApplication.instance() or QApplication([])
        journal = RecordingJournal(tmp_path)
        # 1.2s of audible tone so SpeechSegmenter emits a speech slice on flush.
        t = np.linspace(0, 1.2, 19200, dtype=np.float32)
        tone = (0.25 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
        assert journal.append(tone)
        assert journal.close()

        sr = SpeechRecognizer()
        rec_old = Recognizer(["<asr_text>代码在GITHUB上用sayink_v2转写</asr_text>"])
        rec_new = Recognizer(["<asr_text>代码在GITHUB上用sayink_v2新模型</asr_text>"])
        sr._recognizer = rec_old
        sr._is_ready = True

        # Swap in rec_new via _on_model_loaded while checking _decode_lock safety.
        sr._on_model_loaded(rec_new)
        assert sr._recognizer is rec_new

        glossary = [
            {"term": "GitHub", "variants": []},
            {"term": "SayInk", "variants": ["sayink_v2"]},
        ]
        dialog = AudioRecoveryDialog(
            tmp_path,
            recognizer=rec_old,
            glossary=glossary,
            decode_lock=sr._decode_lock,
            recognizer_getter=lambda: sr._recognizer if sr.is_ready else None,
        )
        try:
            assert dialog.stop_btn.isEnabled() is False
            dialog._transcribe()
            worker = dialog.worker
            assert worker is not None
            progress_spy = QSignalSpy(worker.progress)
            finished_spy = QSignalSpy(worker.finished)
            if worker.isRunning():
                assert finished_spy.wait(4000)
            app.processEvents()

            # Used latest recognizer from recognizer_getter AND applied glossary casing + variants!
            assert dialog.output.toPlainText().strip() == "代码在GitHub上用SayInk新模型"
            assert len(progress_spy) >= 1
            assert dialog.status.text() == "处理完成"
            assert dialog.stop_btn.isEnabled() is False
        finally:
            dialog.reject()
            dialog.deleteLater()


class TestContinuousDiarizationGuardrail:
    """Skip SpeakerTurnDetector in continuous mode ONLY when history is off AND paste mode is active."""

    def test_continuous_paste_with_history_disabled_skips_turn_detector_and_ui_embedder(self):
        with app_harness(
            {
                "audio.trigger_mode": "continuous",
                "history.enabled": False,
                "output.mode": OUTPUT_MODE_PASTE,
            }
        ) as h:
            app = h["app"]
            _start_continuous_user_session(app)
            h["recorder"].is_continuous = True
            context = make_context(app, 10_000)
            embed_spy = MagicMock(side_effect=AssertionError("embedder must not run on UI thread"))
            embed_spy.supports_turn_detection = True
            context.embed = embed_spy

            app._begin_transcription(np.full(48000, 0.1, dtype=np.float32), context=context)
            kwargs = h["recognizer"].transcribe_final.call_args.kwargs
            assert kwargs == {"retry_empty": True}
            assert "turn_analyzer" not in kwargs
            embed_spy.assert_not_called()

    @pytest.mark.parametrize(
        ("history_enabled", "output_mode"),
        [
            (True, OUTPUT_MODE_PASTE),
            (True, OUTPUT_MODE_HISTORY),
            (False, OUTPUT_MODE_HISTORY),
        ],
    )
    def test_continuous_keeps_turn_detector_whenever_history_is_enabled_or_selected(
        self, history_enabled: bool, output_mode: str
    ):
        with app_harness(
            {
                "audio.trigger_mode": "continuous",
                "history.enabled": history_enabled,
                "output.mode": output_mode,
            }
        ) as h:
            app = h["app"]
            _start_continuous_user_session(app)
            h["recorder"].is_continuous = True
            context = make_context(app, 10_000)

            app._begin_transcription(np.full(48000, 0.1, dtype=np.float32), context=context)
            kwargs = h["recognizer"].transcribe_final.call_args.kwargs
            assert kwargs["retry_empty"] is True
            assert kwargs.get("turn_analyzer") is not None

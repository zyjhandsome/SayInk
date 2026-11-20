"""Stage-2 end-to-end integration tests for core orchestration (36/36 items).

All assertions go through public ``App`` / ``OutputPipeline`` / ``ReloadCoordinator``
contracts and collaborator Qt signals (zero cross-module private ``app._*`` probes).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest
from PyQt6.QtCore import QObject, pyqtSignal

from sayink.audio_utils import TARGET_SAMPLE_RATE
from sayink.config import (
    OUTPUT_MODE_HISTORY,
    OUTPUT_MODE_PASTE,
    TRIGGER_MODE_CONTINUOUS,
    TRIGGER_MODE_HOTKEY,
    Config,
)
from sayink.history_store import HistoryStore, SegmentRecord
from sayink.meeting_session import MeetingOptions, MeetingState
from sayink.output_pipeline import OutputPipeline, UtteranceContext, _PendingHistoryRecord
from sayink.runtime_status import SegmentationStatus
from sayink.text_paster import PasteResult, PasteTargetSnapshot


class _SignalHotkey(QObject):
    recording_start = pyqtSignal()
    recording_stop = pyqtSignal()
    recording_cancel = pyqtSignal()
    continuous_listen_start = pyqtSignal()
    continuous_listen_stop = pyqtSignal()
    hotkey_tap_too_short = pyqtSignal()
    esc_pressed = pyqtSignal()
    esc_cancel_requested = pyqtSignal()
    paste_last_triggered = pyqtSignal()
    rewrite_start = pyqtSignal()
    rewrite_stop = pyqtSignal()
    rewrite_cancel = pyqtSignal()
    listener_status = pyqtSignal(bool, str)

    def __init__(self) -> None:
        super().__init__()
        self.continuous_trigger = False
        self.continuous_active = False
        self.cancellable_processing = False
        self.paused = False

    def set_continuous_trigger_mode(self, enabled: bool) -> None:
        self.continuous_trigger = bool(enabled)

    def set_continuous_listening_active(self, active: bool) -> None:
        self.continuous_active = bool(active)

    def set_cancellable_processing(self, active: bool) -> None:
        self.cancellable_processing = bool(active)

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def update_hotkey(self, _hk: str) -> None:
        return None


class _SignalRecorder(QObject):
    volume_changed = pyqtSignal(float)
    recording_finished = pyqtSignal(object)
    segment_ready = pyqtSignal(object)
    utterance_opened = pyqtSignal(int, str)
    speech_active_changed = pyqtSignal(bool)
    segmentation_changed = pyqtSignal(object)
    error = pyqtSignal(str)
    warning = pyqtSignal(str)
    no_speech_warning = pyqtSignal()
    lane_levels_changed = pyqtSignal(float, float)
    system_silent_warning = pyqtSignal(str)

    def __init__(self, source: str = "microphone") -> None:
        super().__init__()
        self.is_recording = False
        self.is_continuous = False
        self.storage_busy = False
        self.recovery_available = False
        self.input_source = source
        self.input_source_display = "麦克风"
        self.mic_device_index = -1
        self.system_device_index = -1
        self.meeting_profile = False
        self.vad_kind = "silero"
        self.segmentation_status = SegmentationStatus("silero", "silero", "")
        self.open_targets: list[tuple[int, str, object]] = []
        self.discard_calls = 0
        self.fail_open_msg = ""
        self.recovery_journal_path = ""

    def configure(
        self,
        input_source: str = "microphone",
        mic_device_index: int = -1,
        system_device_index: int = -1,
    ) -> None:
        self.input_source = input_source
        self.mic_device_index = int(mic_device_index)
        self.system_device_index = int(system_device_index)

    def set_segmenter_profile(
        self, *, vad: str = "silero", vad_model_path: object = None, meeting: bool = False
    ) -> None:
        self.vad_kind = vad
        self.meeting_profile = bool(meeting)

    def set_quiet_speech(self, _enabled: bool) -> None:
        return None

    def set_boundary_context(self, _enabled: bool) -> None:
        return None

    def set_recovery_keep_completed(self, _keep: bool) -> None:
        return None

    def set_recovery_directory(self, _root: object) -> None:
        return None

    def set_recovery_session_id(self, _sid: str) -> None:
        return None

    def set_separate_lanes(self, _enabled: bool) -> None:
        return None

    def set_paused(self, _paused: bool) -> None:
        return None

    def start(self, *, continuous: bool = False) -> None:
        if self.fail_open_msg:
            self.is_recording = False
            self.is_continuous = False
            self.error.emit(self.fail_open_msg)
            return
        self.is_recording = True
        self.is_continuous = bool(continuous)

    def start_continuous(self) -> None:
        self.start(continuous=True)

    def stop(self) -> None:
        self.is_recording = False

    def stop_continuous(self) -> None:
        self.is_recording = False
        self.is_continuous = False

    def cancel(self) -> None:
        self.is_recording = False
        self.is_continuous = False

    def discard_current_utterance(self) -> None:
        self.discard_calls += 1
        self.speech_active_changed.emit(False)

    def pop_open_target_details(self) -> tuple[int, str, object]:
        if self.open_targets:
            return self.open_targets.pop(0)
        return (0, "", None)

    def consume_segment_target(self) -> tuple[int, str]:
        hwnd, app_name, _snap = self.pop_open_target_details()
        return hwnd, app_name

    def consume_segment_route(self) -> str:
        return "mic"

    def consume_segment_route_reliable(self) -> bool:
        return True

    def consume_segment_captured_at(self) -> int:
        return 1_700_000_000_000

    def seconds_since_last_speech(self) -> float:
        return 0.0


class _SignalRecognizer(QObject):
    final_result = pyqtSignal(str)
    meeting_result = pyqtSignal(object)
    partial_result = pyqtSignal(str)
    error = pyqtSignal(str)
    ready = pyqtSignal()
    model_load_progress = pyqtSignal(object, str)

    def __init__(self, model_id: str = "funasr-nano") -> None:
        super().__init__()
        self.is_ready = True
        self.is_loading = False
        self.is_silent_loading = False
        self.current_model_id = model_id
        self.quiet_speech = False
        self.running_workers = ()
        self.running_transcribe_workers = ()
        self.transcribe_calls: list[np.ndarray] = []
        self.cancel_calls = 0
        self.configured_targets: list[tuple[str, int, str, str]] = []
        self.cancel_on_configure = False

    def configure(
        self, model_id: str, num_threads: int = 4, *, hotwords: str = "", language: str = ""
    ) -> None:
        self.configured_targets.append((model_id, num_threads, hotwords, language))
        if self.cancel_on_configure:
            self.cancel_current()
        self.current_model_id = model_id

    def transcribe_final(
        self, audio: np.ndarray, *, retry_empty: bool = False, turn_analyzer: object = None
    ) -> None:
        _ = (retry_empty, turn_analyzer)
        self.transcribe_calls.append(audio)

    def cancel_current(self) -> bool:
        self.cancel_calls += 1
        return True

    def shutdown(self, *, wait_ms: int = 0) -> None:
        _ = wait_ms


class _SignalPolisher(QObject):
    polish_complete = pyqtSignal(str)
    polish_error = pyqtSignal(str)
    rewrite_complete = pyqtSignal(str)
    rewrite_error = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self.running_workers = ()
        self.polish_calls: list[dict] = []
        self.rewrite_calls: list[tuple[str, str]] = []
        self.cancel_calls = 0

    def polish(
        self,
        text: str,
        api_url: str,
        api_key: str,
        model_name: str,
        prompt: str = "",
        **kwargs: object,
    ) -> None:
        self.polish_calls.append(
            {"text": text, "api_url": api_url, "model": model_name, "prompt": prompt, **kwargs}
        )

    def rewrite(
        self,
        selected_text: str,
        instruction: str,
        api_url: str,
        api_key: str,
        model_name: str,
        **_kwargs: object,
    ) -> None:
        _ = (api_url, api_key, model_name)
        self.rewrite_calls.append((selected_text, instruction))

    def cancel(self) -> None:
        self.cancel_calls += 1

    def cancel_rewrite(self) -> None:
        return None


class _SignalFloating(QObject):
    continuous_stop_requested = pyqtSignal()
    end_requested = continuous_stop_requested
    cancel_requested = pyqtSignal()
    retry_save_requested = pyqtSignal()
    copy_unsaved_requested = pyqtSignal()
    settings_requested = pyqtSignal()
    history_requested = pyqtSignal()
    meeting_mark_requested = pyqtSignal()
    meeting_open_requested = pyqtSignal()
    position_committed = pyqtSignal(dict)
    placement_reset = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.events: list[tuple] = []
        self.speech_capturing: list[bool] = []
        self.busy_allow_cancel: list[bool] = []
        self.unsaved_calls: list[tuple[str, str, int, str]] = []
        self.live_texts: list[str] = []

    def __getattr__(self, name: str):
        def _record(*args, **kwargs):
            self.events.append((name, args, kwargs))

        return _record

    def set_speech_capturing(self, active: bool) -> None:
        self.speech_capturing.append(bool(active))

    def show_busy_transcribing(self, *, allow_cancel: bool = False) -> None:
        self.busy_allow_cancel.append(bool(allow_cancel))
        self.events.append(("show_busy_transcribing", (), {"allow_cancel": allow_cancel}))

    def show_unsaved_history_error(
        self, title: str = "保存历史失败", subtitle: str = "", count: int = 1, **kwargs: object
    ) -> None:
        self.unsaved_calls.append((title, subtitle, count, str(kwargs.get("copy_label", ""))))
        self.events.append(("show_unsaved_history_error", (title, subtitle, count), kwargs))

    def show_live_transcript(self, text: str) -> None:
        self.live_texts.append(text)
        self.events.append(("show_live_transcript", (text,), {}))


class _SignalTray(QObject):
    open_settings = pyqtSignal()
    wake_island = pyqtSignal()
    history_requested = pyqtSignal()
    meeting_start_requested = pyqtSignal()
    meeting_end_requested = pyqtSignal()
    end_meeting_requested = meeting_end_requested
    stop_continuous_requested = pyqtSignal()
    meetings_requested = pyqtSignal()
    quit_app = pyqtSignal()
    auto_start_toggled = pyqtSignal(bool)
    model_switched = pyqtSignal(str)
    menu_about_to_show = pyqtSignal()
    menu_closed = pyqtSignal()
    check_update_requested = pyqtSignal()
    messageClicked = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.continuous_states: list[bool] = []
        self.messages: list[tuple[str, str]] = []
        self.recording_states: list[bool] = []
        self.meeting_states: list[bool] = []

    def set_continuous_listening(self, active: bool) -> None:
        self.continuous_states.append(bool(active))

    def set_recording(self, active: bool) -> None:
        self.recording_states.append(bool(active))

    def set_meeting_in_progress(self, active: bool) -> None:
        self.meeting_states.append(bool(active))

    def showMessage(self, title: str, msg: str, *_args: object) -> None:
        self.messages.append((title, msg))

    def __getattr__(self, _name: str):
        return lambda *_a, **_kw: None


class _RecordingPaster:
    def __init__(self) -> None:
        self.restore_clipboard = True
        self.calls: list[dict] = []
        self.reset_count = 0

    def reset_fallback_accumulator(self) -> None:
        self.reset_count += 1

    def cancel(self) -> None:
        return None

    def paste_async(
        self,
        text: str,
        callback,
        *,
        target_hwnd: int | None = None,
        target_app: str | None = None,
        baseline_snapshot: object = None,
        strict_subfocus: bool = True,
        append_fallback: bool = False,
        keep_on_unverified: bool | None = None,
    ) -> None:
        self.calls.append(
            {
                "text": text,
                "callback": callback,
                "target_hwnd": target_hwnd,
                "target_app": target_app,
                "baseline_snapshot": baseline_snapshot,
                "strict_subfocus": strict_subfocus,
                "append_fallback": append_fallback,
                "keep_on_unverified": keep_on_unverified,
            }
        )


def _make_bundle(tmp_path: Path, overrides: dict | None = None, *, history: object = None, meeting: object = None):
    from sayink.app import App, AppModules

    cfg = Config(config_dir=tmp_path / "cfg", sync_auto_start=False)
    for k, v in (overrides or {}).items():
        cfg.set(k, v)
    hotkey = _SignalHotkey()
    recorder = _SignalRecorder(str(cfg.get("audio.input_source", "microphone")))
    recognizer = _SignalRecognizer()
    polisher = _SignalPolisher()
    paster = _RecordingPaster()
    floating = _SignalFloating()
    tray = _SignalTray()
    sound = MagicMock()
    app = App.create_headless(
        cfg,
        modules=AppModules(
            hotkey_mgr=hotkey,  # type: ignore[arg-type]
            recorder=recorder,  # type: ignore[arg-type]
            recognizer=recognizer,  # type: ignore[arg-type]
            polisher=polisher,  # type: ignore[arg-type]
            paster=paster,  # type: ignore[arg-type]
            floating=floating,  # type: ignore[arg-type]
            tray=tray,  # type: ignore[arg-type]
            sound=sound,
            history=history,  # type: ignore[arg-type]
            meeting=meeting,  # type: ignore[arg-type]
        ),
    )
    return app, cfg, hotkey, recorder, recognizer, polisher, paster, floating, tray, sound


def _speech_audio(seconds: float = 0.5) -> np.ndarray:
    t = np.linspace(0.0, seconds, int(seconds * TARGET_SAMPLE_RATE), endpoint=False, dtype=np.float32)
    envelope = (0.5 + 0.5 * np.sin(2.0 * np.pi * 4.0 * t)).astype(np.float32)
    return (0.18 * envelope * np.sin(2.0 * np.pi * 220.0 * t)).astype(np.float32)


def test_single_shot_freezes_utterance_context_across_mid_hold_mode_and_destination_switches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snap_a = PasteTargetSnapshot(hwnd=101, focus_hwnd=201, title_digest="a1")
    monkeypatch.setattr("sayink.text_paster.get_foreground_window_info", lambda: (101, "Doc", 999))
    monkeypatch.setattr("sayink.text_paster.get_foreground_process_name", lambda **_kw: "Code.exe")
    monkeypatch.setattr("sayink.app.capture_paste_target_snapshot", lambda _hwnd=None: snap_a)
    timers: list[tuple[int, object]] = []
    monkeypatch.setattr("sayink.app.QTimer.singleShot", lambda ms, fn: timers.append((ms, fn)))

    app, cfg, hotkey, recorder, recognizer, _polisher, paster, _floating, _tray, _sound = _make_bundle(
        tmp_path,
        {"audio.trigger_mode": TRIGGER_MODE_HOTKEY, "output.mode": OUTPUT_MODE_PASTE},
    )
    hotkey.recording_start.emit()
    assert recorder.is_recording is True

    # Segment 1 finishes mid-hold and commits to the live hold buffer.
    recorder.segment_ready.emit(_speech_audio(0.6))
    recognizer.final_result.emit("第一句内容")

    # Mid-hold: user switches both trigger_mode -> continuous AND output.mode -> history!
    cfg.set("audio.trigger_mode", TRIGGER_MODE_CONTINUOUS)
    cfg.set_output_mode(OUTPUT_MODE_HISTORY, auto=False)

    # Key release flushes segment 2 -> both sentences MUST be joined and pasted to snap_a!
    hotkey.recording_stop.emit()
    assert hotkey.cancellable_processing is True
    recorder.recording_finished.emit(_speech_audio(0.6))
    recognizer.final_result.emit("第二句内容")

    assert len(paster.calls) == 1
    call = paster.calls[0]
    assert call["text"] == "第一句内容第二句内容"
    assert call["target_hwnd"] == 101
    assert call["target_app"] == "Code.exe"
    assert call["baseline_snapshot"] == snap_a
    assert call["strict_subfocus"] is True
    assert call["append_fallback"] is False
    assert hotkey.cancellable_processing is False


def test_hold_b_during_sentence_a_polish_preserves_a_target_and_cancels_only_b(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snaps = [
        PasteTargetSnapshot(hwnd=500, focus_hwnd=501, title_digest="box1"),
        PasteTargetSnapshot(hwnd=500, focus_hwnd=502, title_digest="box2"),
    ]
    monkeypatch.setattr("sayink.text_paster.get_foreground_window_info", lambda: (500, "Editor", 888))
    monkeypatch.setattr("sayink.text_paster.get_foreground_process_name", lambda **_kw: "Editor.exe")
    monkeypatch.setattr("sayink.app.capture_paste_target_snapshot", lambda _hwnd=None: snaps.pop(0))
    monkeypatch.setattr("sayink.app.QTimer.singleShot", lambda _ms, _fn: None)

    app, _cfg, hotkey, recorder, recognizer, polisher, paster, floating, _tray, _sound = _make_bundle(
        tmp_path,
        {
            "llm.enabled": True,
            "llm.api_url": "http://127.0.0.1:11434/v1",
            "llm.api_key": "local",
            "llm.model_name": "qwen",
        },
    )
    # Sentence A recorded in box1 -> enters LLM Polish
    hotkey.recording_start.emit()
    hotkey.recording_stop.emit()
    recorder.recording_finished.emit(_speech_audio(0.5))
    recognizer.final_result.emit("第一句待润色")
    assert len(polisher.polish_calls) == 1

    # Hold B starts in box2 while A is still in Polish, then releases (queued behind A)
    hotkey.recording_start.emit()
    hotkey.recording_stop.emit()
    recorder.recording_finished.emit(_speech_audio(0.5))

    # User clicks 取消 on Hold B -> cancels B only; Sentence A in Polish MUST survive!
    floating.cancel_requested.emit()
    assert polisher.cancel_calls == 0

    polisher.polish_complete.emit("第一句已润色。")
    assert len(paster.calls) == 1
    assert paster.calls[0]["text"] == "第一句已润色。"
    assert paster.calls[0]["baseline_snapshot"] == PasteTargetSnapshot(
        hwnd=500, focus_hwnd=501, title_digest="box1"
    )


def test_async_history_settlement_waits_for_sqlite_and_recovers_unsaved_idempotently(
    tmp_path: Path, qapp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sayink.app.QTimer.singleShot", lambda ms, fn: fn() if ms < 5000 else None)
    copied: list[str] = []
    monkeypatch.setattr("pyperclip.copy", copied.append)

    store = HistoryStore(db_path=tmp_path / "history.db")
    try:
        app, cfg, hotkey, recorder, recognizer, _polisher, _paster, floating, _tray, _sound = _make_bundle(
            tmp_path,
            {"output.mode": OUTPUT_MODE_HISTORY, "history.enabled": True},
            history=store,
        )
        cfg.set_output_mode(OUTPUT_MODE_HISTORY, auto=False)

        # Force writer failure on first attempt
        fail_state = {"fail": True}
        real_insert = store._do_insert

        def _flaky_insert(conn, payload, **kw):
            if fail_state["fail"]:
                raise RuntimeError("disk full")
            return real_insert(conn, payload, **kw)

        monkeypatch.setattr(store, "_do_insert", _flaky_insert)

        hotkey.recording_start.emit()
        hotkey.recording_stop.emit()
        recorder.recording_finished.emit(_speech_audio(0.5))
        recognizer.final_result.emit("仅保存历史第一句")
        store.flush(timeout=2.0)
        qapp.processEvents()

        # Must NOT claim 已保存到历史; must surface unsaved recovery bar!
        success_titles = [ev[1][0] for ev in floating.events if ev[0] == "show_success" and ev[1]]
        assert "已保存到历史" not in success_titles
        assert len(floating.unsaved_calls) == 1
        assert "保存历史失败" in floating.unsaved_calls[-1][0]

        # Copy unsaved action copies raw text to clipboard
        floating.copy_unsaved_requested.emit()
        assert copied[-1] == "仅保存历史第一句"

        # Restore DB and click Retry twice -> idempotent WHERE NOT EXISTS writes exactly 1 row!
        fail_state["fail"] = False
        floating.retry_save_requested.emit()
        store.flush(timeout=2.0)
        qapp.processEvents()
        # Even if retried directly a second time with same (session_id, seq), no duplicate row!
        sessions = store.list_sessions(limit=10, offset=0)
        assert len(sessions) == 1
        segs = store.get_session_segments(sessions[0].session_id)
        assert len(segs) == 1
        assert segs[0].raw_text == "仅保存历史第一句"
        success_titles_after = [ev[1][0] for ev in floating.events if ev[0] == "show_success" and ev[1]]
        assert "已保存到历史" in success_titles_after
    finally:
        store.close()


def test_conflict_cancel_joins_homeless_segments_in_seq_order(tmp_path: Path) -> None:
    copied: list[str] = []
    cfg = Config(config_dir=tmp_path / "cfg", sync_auto_start=False)
    cfg.set("audio.trigger_mode", TRIGGER_MODE_CONTINUOUS)
    cfg.set("history.enabled", False)
    cfg.set_output_mode(OUTPUT_MODE_HISTORY, auto=False)
    rec_mock = MagicMock(is_continuous=True, is_recording=True)
    queued = [True]

    pipe = OutputPipeline(
        config=cfg,
        polisher=MagicMock(),
        paster=MagicMock(),
        history=MagicMock(disabled=False),
        floating=MagicMock(),
        tray=MagicMock(),
        recorder=rec_mock,
        ask_history_output_conflict=lambda **_kw: "cancel",
        stop_continuous_user_session=lambda: setattr(rec_mock, "is_continuous", False),
        has_queued_segments=lambda: queued[0],
        copy_to_clipboard=copied.append,
    )
    pipe.pending_record = _PendingHistoryRecord(
        session_id="s1", seq=2, raw_text="第二句", polished_text="", source="mic",
        duration_ms=500, trigger_mode=TRIGGER_MODE_CONTINUOUS, model="funasr-nano",
    )
    pipe.output_text("第二句")
    queued[0] = False
    pipe.pending_record = _PendingHistoryRecord(
        session_id="s1", seq=0, raw_text="第零句", polished_text="", source="mic",
        duration_ms=500, trigger_mode=TRIGGER_MODE_CONTINUOUS, model="funasr-nano",
    )
    pipe.output_text("第零句")

    assert copied[-1] == "第零句\n第二句"
    assert pipe.last_output_text == "第零句\n第二句"


def test_capture_open_guards_disconnect_warning_and_new_paste_detail_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sayink.app.QTimer.singleShot", lambda ms, fn: fn() if ms < 5000 else None)
    app, _cfg, hotkey, recorder, recognizer, _polisher, paster, floating, _tray, sound = _make_bundle(
        tmp_path, {"audio.trigger_mode": TRIGGER_MODE_HOTKEY}
    )

    # 1. Open failure: no start cue, no stuck show_recording clobbering the error!
    recorder.fail_open_msg = "无法访问麦克风"
    hotkey.recording_start.emit()
    sound.play_start.assert_not_called()
    assert floating.events[-1][0] == "show_error"

    # 2. Mid-hold mic disconnect after segment 1 recognized -> warning instead of green 已发送!
    recorder.fail_open_msg = ""
    hotkey.recording_start.emit()
    recorder.segment_ready.emit(_speech_audio(0.5))
    recognizer.final_result.emit("断开前的话")
    recorder.stop()
    recorder.error.emit("音频设备已断开")
    app.output_pipeline.deliver_recognized_text("断开前的话")
    paster.calls[-1]["callback"](PasteResult("sent", target_app="Notepad.exe"))
    warnings = [ev[1][0] for ev in floating.events if ev[0] == "show_warning" and ev[1]]
    assert any("已发送前半段 · 麦克风已断开" in w for w in warnings)

    # 3. Exact PasteResult detail mapping in OutputPipeline
    mapping_cases = [
        (PasteResult("clipboard", target_app="Code.exe", detail="subfocus_changed"), "输入位置已变化，已复制，请回原位置按 Ctrl+V"),
        (PasteResult("skipped", target_app="App.exe", detail="readonly_target"), "目标不可编辑，已复制到剪贴板"),
        (PasteResult("skipped", target_app="pwsh.exe", detail="terminal_multiline"), "多行内容已复制，请在终端确认后粘贴"),
        (PasteResult("clipboard", target_app="WeChat.exe", detail="focus_changed"), "未输入到 WeChat.exe · 已复制，可按 Ctrl+V 粘贴"),
    ]
    for res, expected in mapping_cases:
        floating.events.clear()
        app.output_pipeline.handle_paste_result(res)
        info_titles = [ev[1][0] for ev in floating.events if ev[0] == "show_info" and ev[1]]
        assert info_titles == [expected]


def test_continuous_start_staging_non_clobbered_decode_error_empty_speech_hint_and_stop_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pending_timers: list[tuple[int, object]] = []
    monkeypatch.setattr("sayink.app.QTimer.singleShot", lambda ms, fn: pending_timers.append((ms, fn)))

    app, _cfg, hotkey, recorder, recognizer, _polisher, paster, floating, tray, sound = _make_bundle(
        tmp_path, {"audio.trigger_mode": TRIGGER_MODE_CONTINUOUS}
    )

    # Start stages 准备监听… first without playing start cue until device opens
    hotkey.continuous_listen_start.emit()
    sound.play_start.assert_not_called()
    assert floating.events[-1][0] == "show_recording"
    assert floating.events[-1][2].get("title") == "准备监听…"

    # Run delayed open -> now start cue plays and tray/hotkey mark active
    while pending_timers:
        _ms, fn = pending_timers.pop(0)
        fn()
    sound.play_start.assert_called_once()
    assert tray.continuous_states[-1] is True
    assert hotkey.continuous_active is True

    # Speech open/close drives set_speech_capturing; 放弃本句 only discards recorder utterance
    recorder.speech_active_changed.emit(True)
    assert floating.speech_capturing[-1] is True
    snap_seg1 = PasteTargetSnapshot(hwnd=301, focus_hwnd=401)
    recorder.open_targets.append((301, "Slack.exe", snap_seg1))
    recorder.utterance_opened.emit(301, "Slack.exe")
    recorder.segment_ready.emit(_speech_audio(0.6))
    assert len(recognizer.transcribe_calls) == 1

    # While seg 1 is in ASR, user speaks seg 2 and clicks 放弃本句 -> recognizer NOT cancelled!
    floating.cancel_requested.emit()
    assert recorder.discard_calls == 1
    assert recognizer.cancel_calls == 0

    recognizer.final_result.emit("保留下来的第一句")
    assert paster.calls[-1]["strict_subfocus"] is False
    assert paster.calls[-1]["append_fallback"] is True
    assert paster.calls[-1]["baseline_snapshot"] == snap_seg1
    paster.calls[-1]["callback"](PasteResult("sent", target_app="Slack.exe"))

    # Decode error in continuous mode is NOT clobbered by synchronous _pump_segment_queue!
    floating.events.clear()
    recorder.segment_ready.emit(_speech_audio(0.5))
    recognizer.error.emit("ASR decode exception")
    names = [ev[0] for ev in floating.events]
    assert "show_error" in names
    assert "show_listening" not in names

    # Brief cough (<1.2s) returning "" stays quiet; >=1.5s clear speech shows non-beep hint
    pending_timers.clear()
    floating.events.clear()
    recorder.segment_ready.emit(np.zeros(4000, dtype=np.float32))
    recognizer.final_result.emit("")
    assert not [ev for ev in floating.events if ev[0] == "show_info"]

    monkeypatch.setattr("sayink.app.voice_sample_is_clear", lambda _a: True)
    floating.events.clear()
    recorder.segment_ready.emit(_speech_audio(1.5))
    recognizer.final_result.emit("")
    info_titles = [ev[1][0] for ev in floating.events if ev[0] == "show_info" and ev[1]]
    assert "刚才的声音未识别出文字" in info_titles

    # Hotkey toggle-stop ends session and summarizes failed segment count
    hotkey.continuous_listen_stop.emit()
    assert recorder.is_continuous is False
    assert tray.continuous_states[-1] is False
    stopped_details = [ev[1][0] for ev in floating.events if ev[0] == "show_continuous_stopped" and ev[1]]
    assert any("1 段未识别" in d for d in stopped_details)


def test_llm_remote_consent_raw_isolation_hwnd_isolation_and_rewrite_refusal_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sayink.app.QTimer.singleShot", lambda ms, fn: fn() if ms < 5000 else None)
    win_backend = MagicMock()
    win_backend.foreground_target.return_value = (700, "Word.exe")
    win_backend.read_selected_text.return_value = "需要润色的原始段落"

    app, cfg, _hotkey, recorder, recognizer, polisher, paster, floating, tray, _sound = _make_bundle(
        tmp_path,
        {
            "llm.enabled": True,
            "llm.api_url": "https://api.unconfirmed.example/v1",
            "llm.api_key": "sk-secret",
            "llm.model_name": "deepseek-chat",
            "llm.mode": "correct",
            "llm.app_presets": [{"app": "KeePass.exe", "preset": "raw"}],
        },
    )
    assert cfg.get("llm.confirmed_remote_hosts") == []

    # 1. Unconfirmed remote host: polisher NOT called, raw text output directly, tray warned once
    app.output_pipeline.pending_record = _PendingHistoryRecord(
        session_id="u1", seq=0, raw_text="机密口述", polished_text="", source="mic",
        duration_ms=400, trigger_mode=TRIGGER_MODE_HOTKEY, model="funasr-nano",
        target_hwnd=10, target_app="Word.exe",
    )
    app.output_pipeline.deliver_recognized_text("机密口述")
    assert polisher.polish_calls == []
    assert paster.calls[-1]["text"] == "机密口述"
    paster.calls[-1]["callback"](PasteResult("sent", target_app="Word.exe"))
    assert len(tray.messages) == 1

    # 2. Confirm host -> KeePass.exe (preset="raw") never enters LLM context; cross-HWND isolated!
    cfg.set("llm.confirmed_remote_hosts", ["https://api.unconfirmed.example"])
    raw_utt = UtteranceContext(
        session_id="u2", trigger_mode=TRIGGER_MODE_HOTKEY, history_only=False,
        target_hwnd=10, target_app="KeePass.exe", preset_id="raw", allow_llm_context=False,
    )
    app.output_pipeline.pending_record = _PendingHistoryRecord(
        session_id="u2", seq=0, raw_text="密码123456", polished_text="", source="mic",
        duration_ms=400, trigger_mode=TRIGGER_MODE_HOTKEY, model="funasr-nano",
        target_hwnd=10, target_app="KeePass.exe", utterance=raw_utt,
    )
    app.output_pipeline.deliver_recognized_text("密码123456")
    assert polisher.polish_calls == []
    paster.calls[-1]["callback"](PasteResult("sent", target_app="KeePass.exe"))
    assert app.output_pipeline.recent_output_context(10) == ""

    # LLM sentence in HWND=10 -> visible in HWND=10, hidden from HWND=20!
    ok_utt = UtteranceContext(
        session_id="u3", trigger_mode=TRIGGER_MODE_HOTKEY, history_only=False,
        target_hwnd=10, target_app="Word.exe", preset_id="", allow_llm_context=True,
    )
    app.output_pipeline.pending_record = _PendingHistoryRecord(
        session_id="u3", seq=0, raw_text="窗口一上文", polished_text="", source="mic",
        duration_ms=400, trigger_mode=TRIGGER_MODE_HOTKEY, model="funasr-nano",
        target_hwnd=10, target_app="Word.exe", utterance=ok_utt,
    )
    app.output_pipeline.deliver_recognized_text("窗口一上文")
    polisher.polish_complete.emit("窗口一上文")
    paster.calls[-1]["callback"](PasteResult("sent", target_app="Word.exe"))
    assert app.output_pipeline.recent_output_context(10) == "窗口一上文"
    assert app.output_pipeline.recent_output_context(20) == ""

    # 3. Select-and-Rewrite blocks LLM refusal without overwriting selection!
    cfg.set("llm.rewrite_confirmed_origins", ["https://api.unconfirmed.example"])
    app2, *_ = _make_bundle(tmp_path / "rw")
    # Build rewrite bundle with window_input + confirmed origin
    from sayink.app import App, AppModules

    rw_polisher = _SignalPolisher()
    rw_paster = _RecordingPaster()
    rw_rec = _SignalRecorder()
    rw_asr = _SignalRecognizer()
    rw_float = _SignalFloating()
    rw_app = App.create_headless(
        cfg,
        modules=AppModules(
            recorder=rw_rec,  # type: ignore[arg-type]
            recognizer=rw_asr,  # type: ignore[arg-type]
            polisher=rw_polisher,  # type: ignore[arg-type]
            paster=rw_paster,  # type: ignore[arg-type]
            floating=rw_float,  # type: ignore[arg-type]
            window_input=win_backend,
            confirm_rewrite_remote_fn=lambda _o: True,
        ),
    )
    _ = app2
    assert rw_app.start_select_and_rewrite() is True
    rw_app.stop_select_and_rewrite()
    rw_rec.recording_finished.emit(_speech_audio(0.5))
    rw_asr.final_result.emit("翻译成英文")
    assert len(rw_polisher.rewrite_calls) == 1
    rw_polisher.rewrite_complete.emit("抱歉，我无法处理该请求。")
    assert rw_paster.calls == []
    err_msgs = [ev[1][0] for ev in rw_float.events if ev[0] == "show_error" and ev[1]]
    assert any("原选区未改动" in m for m in err_msgs)


def test_meeting_begin_rollback_device_hotswap_on_resume_takeover_and_cold_paste_last(
    tmp_path: Path, qapp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sayink.app.QTimer.singleShot", lambda ms, fn: fn() if ms < 5000 else None)

    store = HistoryStore(db_path=tmp_path / "meet.db")
    try:
        app, cfg, hotkey, recorder, recognizer, _polisher, paster, floating, tray, _sound = _make_bundle(
            tmp_path,
            {"audio.input_source": "microphone", "audio.trigger_mode": TRIGGER_MODE_CONTINUOUS},
            history=store,
        )
        meeting_ctrl = app.meeting

        # 1. Meeting begin() RuntimeError rolls back AudioRecorder to dictation profile!
        orig_begin = meeting_ctrl.begin
        monkeypatch.setattr(
            meeting_ctrl, "begin", lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("数据库只读"))
        )
        assert app.start_meeting(MeetingOptions(title="周会", input_source="mixed")) is False
        assert recorder.input_source == "microphone"
        assert recorder.meeting_profile is False
        monkeypatch.setattr(meeting_ctrl, "begin", orig_begin)

        # 2. Takeover of active continuous session with in-flight ASR slice preserves it & notifies!
        recognizer.cancel_on_configure = True
        hotkey.continuous_listen_start.emit()
        recorder.segment_ready.emit(_speech_audio(0.6))
        assert len(recognizer.transcribe_calls) == 1
        assert app.start_meeting(MeetingOptions(title="正式会议", input_source="mixed", model_id="sensevoice")) is True
        assert any("持续转写剩余 1 段将保存到历史" in msg for _, msg in tray.messages)
        # Re-queued continuous slice was NOT lost — pumped right back into recognizer!
        assert len(recognizer.transcribe_calls) == 2
        recognizer.final_result.emit("接管前正在识别的句子")
        store.flush(timeout=2.0)
        qapp.processEvents()

        # 3. Meeting ASR failure records gap on MeetingController; pause + settings device change hot-swaps on resume!
        recorder.segment_ready.emit(_speech_audio(0.8))
        recognizer.error.emit("decode crash")
        assert len(meeting_ctrl.asr_failures(meeting_ctrl.current_id)) == 1

        recorder.error.emit("麦克风已断开")
        assert meeting_ctrl.state is MeetingState.PAUSED
        cfg.set("audio.mic_device_index", 7)
        app.reload_coordinator.on_settings_changed()
        assert app.reload_snapshot.deferred_meeting_audio_settings is True

        # First resume attempt fails to open -> stays PAUSED!
        recorder.fail_open_msg = "设备仍不可用"
        app.resume_meeting_capture()
        assert meeting_ctrl.state is MeetingState.PAUSED
        assert recorder.mic_device_index == 7
        assert recorder.input_source == "mixed"
        assert recorder.meeting_profile is True

        # Second resume succeeds -> LIVE!
        recorder.fail_open_msg = ""
        app.resume_meeting_capture()
        assert meeting_ctrl.state is MeetingState.LIVE

        tray.end_meeting_requested.emit()
        store.flush(timeout=2.0)
        qapp.processEvents()

        # 4. Cold-start paste_last_transcript picks ONLY last non-deleted segment (corrected > polished != \u200b > raw)
        import time as _time

        base_ts = int(_time.time() * 1000) + 10_000
        store.enqueue(
            SegmentRecord(
                session_id="cold-1", seq=0, created_at=base_ts,
                raw_text="早期段落", polished_text="早期段落",
                source="mic", duration_ms=500, trigger_mode=TRIGGER_MODE_CONTINUOUS, model="funasr-nano",
            )
        )
        store.enqueue(
            SegmentRecord(
                session_id="cold-1", seq=1, created_at=base_ts + 1000,
                raw_text="保留段落原稿", polished_text="", corrected_text="手动修订后的最后一句",
                source="mic", duration_ms=500, trigger_mode=TRIGGER_MODE_CONTINUOUS, model="funasr-nano",
            )
        )
        store.enqueue(
            SegmentRecord(
                session_id="cold-1", seq=2, created_at=base_ts + 2000,
                raw_text="已删除段落", polished_text="\u200b",
                source="mic", duration_ms=500, trigger_mode=TRIGGER_MODE_CONTINUOUS, model="funasr-nano",
            )
        )
        store.flush(timeout=2.0)
        app.output_pipeline.last_output_text = ""
        paster.calls.clear()
        assert app.paste_last_transcript() is True
        assert paster.calls[-1]["text"] == "手动修订后的最后一句"
        _ = floating
    finally:
        store.close()

"""Standalone unit and constructor-DI tests for OutputPipeline."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from sayink.app import App, AppModules
from sayink.config import (
    DEFAULT_CONFIG,
    OUTPUT_MODE_HISTORY,
    TRIGGER_MODE_CONTINUOUS,
    TRIGGER_MODE_HOTKEY,
    TRIGGER_MODE_MEETING,
    Config,
)
from sayink.history_store import SegmentRecord
from sayink.output_pipeline import (
    OutputPipeline,
    _PendingHistoryRecord,
)
from sayink.runtime_status import OutputStage
from sayink.text_paster import PasteResult


def _dot_get(store: dict, key: str, default=None):
    node = store
    for part in key.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return default
    return node


def _dot_set(store: dict, key: str, value) -> None:
    node = store
    parts = key.split(".")
    for part in parts[:-1]:
        if not isinstance(node.get(part), dict):
            node[part] = {}
        node = node[part]
    node[parts[-1]] = value


class _FakeConfig:
    """Pure in-memory Config double supporting dotted get/set and destination helpers."""

    def __init__(self, overrides: dict | None = None, *, config_dir: Path | None = None) -> None:
        self.store = deepcopy(DEFAULT_CONFIG)
        self.config_dir = config_dir or Path(".")
        if overrides:
            for key, value in overrides.items():
                self.set(key, value)

    def get(self, key: str, default=None):
        return _dot_get(self.store, key, default)

    def set(self, key: str, value) -> None:
        _dot_set(self.store, key, value)
        if key == "output.mode":
            trigger = Config._active_output_trigger(self)
            by_mode = self.get("output.by_mode")
            if isinstance(by_mode, dict) and isinstance(by_mode.get(trigger), dict):
                by_mode[trigger]["mode"] = value


@dataclass
class _FakePolisher:
    calls: list[dict] = field(default_factory=list)
    cancelled: int = 0

    def polish(
        self,
        text: str,
        api_url: str,
        api_key: str,
        model_name: str,
        prompt: str,
        **kwargs,
    ) -> None:
        self.calls.append(
            {
                "text": text,
                "api_url": api_url,
                "api_key": api_key,
                "model_name": model_name,
                "prompt": prompt,
                **kwargs,
            }
        )

    def cancel(self) -> None:
        self.cancelled += 1


@dataclass
class _FakePaster:
    calls: list[dict] = field(default_factory=list)
    cancelled: int = 0
    restore_clipboard: bool = False

    def paste_async(self, text: str, callback, *, target_hwnd: int = 0, target_app: str = "") -> None:
        self.calls.append(
            {
                "text": text,
                "callback": callback,
                "target_hwnd": target_hwnd,
                "target_app": target_app,
            }
        )

    def cancel(self) -> None:
        self.cancelled += 1


@dataclass
class _FakeHistory:
    records: list[SegmentRecord] = field(default_factory=list)
    disabled: bool = False
    startup_notice: str = ""

    def enqueue(self, record: SegmentRecord) -> None:
        self.records.append(record)

    def add_committed_callback(self, _cb) -> None:
        pass

    def add_failed_callback(self, _cb) -> None:
        pass


@dataclass
class _FakeFloating:
    events: list[tuple] = field(default_factory=list)

    def show_polishing(self, text: str, *, action: str = "润色") -> None:
        self.events.append(("polishing", text, action))

    def show_continuous_stopped(self, detail: str = "") -> None:
        self.events.append(("continuous_stopped", detail))

    def show_info(self, title: str, hint: str = "") -> None:
        self.events.append(("info", title, hint))

    def show_success(self, title: str, hint: str = "") -> None:
        self.events.append(("success", title, hint))

    def show_error(self, message: str) -> None:
        self.events.append(("error", message))

    def show_recording(self) -> None:
        self.events.append(("recording",))


@dataclass
class _FakeTray:
    tooltips: list[str | None] = field(default_factory=list)
    messages: list[tuple] = field(default_factory=list)

    def set_activity_tooltip(self, value: str | None) -> None:
        self.tooltips.append(value)

    def showMessage(self, title: str, message: str, icon=None, msecs: int = 0) -> None:
        self.messages.append((title, message, icon, msecs))


@dataclass
class _FakeRecorder:
    is_recording: bool = False
    is_continuous: bool = False


def _build_pipeline(
    overrides: dict | None = None,
    **kwargs,
) -> tuple[OutputPipeline, dict]:
    cfg = _FakeConfig(overrides)
    polisher = _FakePolisher()
    paster = _FakePaster()
    history = _FakeHistory()
    floating = _FakeFloating()
    tray = _FakeTray()
    recorder = _FakeRecorder()
    scheduled: list[tuple[int, object]] = []
    copied: list[str] = []
    clock = [1000.0]
    wall = [1_700_000_000.0]
    pumped = [0]
    empty_finished = [0]

    pipe = OutputPipeline(
        config=cfg,
        polisher=polisher,
        paster=paster,
        history=history,
        floating=floating,
        tray=tray,
        recorder=recorder,
        schedule_later=lambda ms, fn: scheduled.append((ms, fn)),
        pump_queue=lambda: pumped.__setitem__(0, pumped[0] + 1),
        finish_empty_recognition=lambda: empty_finished.__setitem__(0, empty_finished[0] + 1),
        copy_to_clipboard=copied.append,
        now_monotonic=lambda: clock[0],
        now_wall=lambda: wall[0],
        platform_name=lambda: "win32",
        **kwargs,
    )
    ctx = {
        "config": cfg,
        "polisher": polisher,
        "paster": paster,
        "history": history,
        "floating": floating,
        "tray": tray,
        "recorder": recorder,
        "scheduled": scheduled,
        "copied": copied,
        "clock": clock,
        "wall": wall,
        "pumped": pumped,
        "empty_finished": empty_finished,
    }
    return pipe, ctx


class TestOutputPipelineDirectAndLLM:
    def test_direct_output_normalizes_tags_preserves_target_and_enqueues_history(self) -> None:
        pipe, ctx = _build_pipeline()
        pipe.pending_record = _PendingHistoryRecord(
            session_id="s1",
            seq=0,
            raw_text="原始文本",
            polished_text="",
            source="mic",
            duration_ms=1200,
            trigger_mode=TRIGGER_MODE_HOTKEY,
            model="funasr-nano",
            target_hwnd=4242,
            target_app="Code.exe",
        )

        pipe.deliver_recognized_text("<asr_text>你好，SayInk</asr_text>")

        assert len(ctx["paster"].calls) == 1
        call = ctx["paster"].calls[0]
        assert call["text"] == "你好，SayInk"
        assert call["target_hwnd"] == 4242
        assert call["target_app"] == "Code.exe"
        assert pipe.stage is OutputStage.PASTE
        assert pipe.busy is True

        call["callback"](PasteResult("sent", target_app="Code.exe"))

        assert pipe.busy is False
        assert pipe.stage is OutputStage.IDLE
        assert len(ctx["history"].records) == 1
        saved = ctx["history"].records[0]
        assert saved.raw_text == "原始文本"
        assert saved.polished_text == "你好，SayInk"
        assert saved.target_app == "Code.exe"
        assert ("success", "已发送", "发送到 Code.exe") in ctx["floating"].events

    def test_claim_pending_record_isolates_polish_sentence_from_newer_hold_cancel(self) -> None:
        pipe, ctx = _build_pipeline(
            {
                "llm.enabled": True,
                "llm.api_url": "https://api.example.com/v1",
                "llm.api_key": "sk-test",
                "llm.model_name": "gpt-test",
                "llm.mode": "polish",
            }
        )
        pipe.pending_record = _PendingHistoryRecord(
            session_id="s1",
            seq=0,
            raw_text="第一句",
            polished_text="",
            source="mic",
            duration_ms=900,
            trigger_mode=TRIGGER_MODE_HOTKEY,
            model="funasr-nano",
            target_hwnd=101,
            target_app="Notepad.exe",
        )

        pipe.deliver_recognized_text("第一句")
        # Simulate a second hold starting and getting cancelled with Esc while polish runs
        pipe.pending_record = _PendingHistoryRecord(
            session_id="s2",
            seq=0,
            raw_text="第二句",
            polished_text="",
            source="mic",
            duration_ms=400,
            trigger_mode=TRIGGER_MODE_HOTKEY,
            model="funasr-nano",
            target_hwnd=999,
            target_app="Other.exe",
        )
        pipe.pending_record = None

        pipe.on_polish_complete("第一句。")

        assert len(ctx["paster"].calls) == 1
        assert ctx["paster"].calls[0]["target_hwnd"] == 101
        assert ctx["paster"].calls[0]["target_app"] == "Notepad.exe"

    def test_polish_fidelity_rejection_falls_back_to_raw_text(self) -> None:
        pipe, ctx = _build_pipeline(
            {
                "llm.enabled": True,
                "llm.api_url": "https://api.example.com/v1",
                "llm.api_key": "sk-test",
                "llm.model_name": "gpt-test",
                "llm.mode": "polish",
            }
        )
        pipe.pending_record = _PendingHistoryRecord(
            session_id="s1",
            seq=0,
            raw_text="预算是 350 万元",
            polished_text="",
            source="mic",
            duration_ms=1000,
            trigger_mode=TRIGGER_MODE_HOTKEY,
            model="funasr-nano",
        )

        pipe.deliver_recognized_text("预算是 350 万元")
        # Mutated number triggers polish_rejection_reason fallback to raw text
        pipe.on_polish_complete("预算是 500 万元")

        assert ctx["paster"].calls[0]["text"] == "预算是 350 万元"
        ctx["paster"].calls[0]["callback"](PasteResult("sent", target_app="Word.exe"))
        assert ("info", "已发送（原文）", "发送到 Word.exe") in ctx["floating"].events

    def test_correction_mode_uses_expiring_recent_context_and_rejects_rewrites(self) -> None:
        glossary = [{"term": "SayInk", "variants": ["say ink"]}]
        pipe, ctx = _build_pipeline(
            {
                "llm.enabled": True,
                "llm.api_url": "https://api.example.com/v1",
                "llm.api_key": "sk-test",
                "llm.model_name": "gpt-test",
                "llm.mode": "correct",
            },
            glossary_for_pending_segment=lambda: glossary,
        )
        # First sentence sets recent output context at t=1000
        pipe.output_text("上半句上下文")
        ctx["paster"].calls.pop()["callback"](PasteResult("sent"))

        ctx["clock"][0] = 1050.0
        pipe.deliver_recognized_text("我们今天测试一下这个语音输入工具")
        assert ctx["polisher"].calls[-1]["context"] == "上半句上下文"
        assert ctx["tray"].tooltips[-1] == "correcting"

        # Aggressive rewrite gets rejected by correction_rejection_reason
        pipe.on_polish_complete("完全重写成了毫不相干的总结段落，删除了原本所有词汇。")
        assert ctx["paster"].calls[-1]["text"] == "我们今天测试一下这个语音输入工具"
        ctx["paster"].calls.pop()["callback"](PasteResult("sent"))

        # Advance past RECENT_OUTPUT_CONTEXT_S (120s) -> context expires
        ctx["clock"][0] = 1300.0
        assert pipe.recent_output_context() == ""


class TestOutputPipelineWatchdogAndConflicts:
    def test_polish_watchdog_salvages_raw_text_and_arms_fresh_paste_watchdog(self) -> None:
        pipe, ctx = _build_pipeline(
            {
                "llm.enabled": True,
                "llm.api_url": "https://api.example.com/v1",
                "llm.api_key": "sk-test",
                "llm.model_name": "gpt-test",
            },
            watchdog_ms=lambda: 75,
        )
        pipe.pending_record = _PendingHistoryRecord(
            session_id="s-wd",
            seq=1,
            raw_text="超时也要保住这句话",
            polished_text="",
            source="mic",
            duration_ms=1500,
            trigger_mode=TRIGGER_MODE_HOTKEY,
            model="funasr-nano",
        )

        pipe.deliver_recognized_text("超时也要保住这句话")
        polish_watchdog_ms, polish_watchdog_fn = ctx["scheduled"][0]
        assert polish_watchdog_ms == 75

        # Fire polish watchdog: cancels polisher, outputs raw text, arms paste watchdog
        polish_watchdog_fn()
        assert ctx["polisher"].cancelled == 1
        assert pipe.stage is OutputStage.PASTE
        assert ctx["paster"].calls[0]["text"] == "超时也要保住这句话"

        # Late polish reply after timeout is ignored
        pipe.on_polish_complete("迟到的润色结果")
        assert len(ctx["paster"].calls) == 1

        # Fire the fresh paste watchdog when paster never calls back
        paste_watchdogs = [fn for ms, fn in ctx["scheduled"][1:] if ms == 75]
        assert paste_watchdogs
        paste_watchdogs[-1]()

        assert pipe.busy is False
        assert pipe.stage is OutputStage.IDLE
        assert ctx["paster"].cancelled == 1
        assert ctx["copied"] == ["超时也要保住这句话"]
        assert ("info", "输出超时 · 已复制", "可按 Ctrl+V 粘贴") in ctx["floating"].events
        assert len(ctx["history"].records) == 1
        assert ctx["history"].records[0].raw_text == "超时也要保住这句话"

    def test_continuous_history_conflict_cancel_copies_tail_without_reprompting(self) -> None:
        prompts: list[dict] = []
        queued = [True]
        stopped_sessions = [0]
        pipe, ctx = _build_pipeline(
            {
                "audio.trigger_mode": TRIGGER_MODE_CONTINUOUS,
                "output.mode": OUTPUT_MODE_HISTORY,
                "history.enabled": False,
            },
            ask_history_output_conflict=lambda **kw: prompts.append(kw) or "cancel",
            has_queued_segments=lambda: queued[0],
            stop_continuous_user_session=lambda: (
                setattr(ctx["recorder"], "is_continuous", False),
                stopped_sessions.__setitem__(0, stopped_sessions[0] + 1),
            )[-1],
        )
        ctx["recorder"].is_continuous = True

        # First sentence prompts once; user chooses cancel -> copies & stops continuous capture
        pipe.pending_record = _PendingHistoryRecord(
            session_id="cont-1",
            seq=0,
            raw_text="第一句",
            polished_text="",
            source="system",
            duration_ms=800,
            trigger_mode=TRIGGER_MODE_CONTINUOUS,
            model="funasr-nano",
        )
        pipe.output_text("第一句")
        assert len(prompts) == 1
        assert stopped_sessions[0] == 1
        assert pipe.keep_unsaved_continuous_text is True
        assert ctx["copied"] == ["第一句"]

        # Second queued tail sentence drains after capture stopped: copied without re-prompting,
        # and once queue is empty, keep_unsaved_continuous_text resets.
        queued[0] = False
        pipe.pending_record = _PendingHistoryRecord(
            session_id="cont-1",
            seq=1,
            raw_text="尾句",
            polished_text="",
            source="system",
            duration_ms=700,
            trigger_mode=TRIGGER_MODE_CONTINUOUS,
            model="funasr-nano",
        )
        pipe.output_text("尾句")
        assert len(prompts) == 1
        assert ctx["copied"] == ["第一句", "尾句"]
        assert pipe.keep_unsaved_continuous_text is False

    def test_meeting_rows_persist_even_when_history_disabled_and_warn_once_on_db_error(self) -> None:
        pipe, ctx = _build_pipeline({"history.enabled": False})
        pipe.pending_record = _PendingHistoryRecord(
            session_id="meet-1",
            seq=2,
            raw_text="会议发言",
            polished_text="",
            source="mixed",
            duration_ms=2000,
            trigger_mode=TRIGGER_MODE_MEETING,
            model="funasr-nano",
            speaker_id=2,
            speaker_route="system",
        )

        pipe.output_text("会议发言")

        assert ctx["paster"].calls == []
        assert len(ctx["history"].records) == 1
        assert ctx["history"].records[0].trigger_mode == TRIGGER_MODE_MEETING
        assert ctx["history"].records[0].speaker_id == 2

        pipe.on_history_write_failed("历史数据库写入失败")
        pipe.on_history_write_failed("第二次错误不再重复弹窗")
        assert len(ctx["tray"].messages) == 1

    def test_empty_normalized_output_releases_slot_cleanly(self) -> None:
        pipe, ctx = _build_pipeline()
        pipe.mark_busy()
        pipe.stage = OutputStage.POLISH
        pipe.output_text("<asr_text>   </asr_text>")
        assert pipe.busy is False
        assert pipe.stage is OutputStage.IDLE
        assert ctx["empty_finished"][0] == 1


class TestAppHeadlessOutputDI:
    def test_create_headless_runs_recognition_to_history_without_global_patches(
        self, tmp_path: Path
    ) -> None:
        cfg = Config(config_dir=tmp_path / "profile")
        cfg.set("stt.glossary", [{"term": "SayInk", "variants": ["say ink"]}])
        history = _FakeHistory()
        paster = _FakePaster()

        app = App.create_headless(
            cfg,
            modules=AppModules(history=history, paster=paster),  # type: ignore[arg-type]
        )
        try:
            app._on_recording_finished(np.ones(3200, dtype=np.float32))
            app._on_final_result("欢迎使用 say ink")

            assert len(paster.calls) == 1
            assert paster.calls[0]["text"] == "欢迎使用 SayInk"
            paster.calls[0]["callback"](PasteResult("sent", target_app="Terminal.exe"))

            assert len(history.records) == 1
            assert history.records[0].raw_text == "欢迎使用 say ink"
            assert history.records[0].polished_text == "欢迎使用 SayInk"
            assert history.records[0].target_app == "Terminal.exe"
        finally:
            app.deleteLater()

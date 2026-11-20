"""Output pipeline collaborator for dictation and meeting segments.

Owns the single-slot post-ASR delivery pipeline:
- Pending history row handoff (separating active hold capture from polish/paste)
- Optional LLM polish / correction dispatch and fidelity guardrails
- Stage-aware watchdog recovery (polish -> raw text; paste -> clipboard + history)
- Destination routing (cursor paste vs history-only vs unsaved conflict choices)
- Post-output listen-bar / tray feedback and ``HistoryStore`` enqueue
"""

from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np
import pyperclip
from PyQt6.QtWidgets import QSystemTrayIcon

from sayink.asr_correction import correction_rejection_reason
from sayink.config import (
    OUTPUT_MODE_PASTE,
    TRIGGER_MODE_CONTINUOUS,
    TRIGGER_MODE_MEETING,
    Config as _ConfigRules,
    format_hotkey,
)
from sayink.contracts import OutputPipelineSnapshot
from sayink.history_store import SegmentRecord
from sayink.polish_fidelity import polish_rejection_reason
from sayink.runtime_status import OutputStage
from sayink.speaker_session import SpeakerSession
from sayink.speech_recognizer import normalize_asr_output
from sayink.text_paster import PasteResult
from sayink.text_polisher import (
    LLM_MODE_CORRECT,
    LLM_MODE_POLISH,
    app_preset_instruction,
    app_preset_label,
    normalize_llm_mode,
    polish_settings_complete,
    prompt_config_key,
    resolve_app_style_rule,
)

log = logging.getLogger("SayInk")

OUTPUT_WATCHDOG_MS = 45_000  # 流式润色可读更久；到点仍改出原文，避免后面的句子一直排队
# A previous output older than this is no longer context for the next one.
RECENT_OUTPUT_CONTEXT_S = 120.0
HISTORY_FAILURE_WARN_COOLDOWN_S = 300.0

# UI pacing (ms). Queued slices start on the next event-loop turn so a burst
# of VAD cuts keeps spoken order; result toasts stay readable (a hint with a
# second line gets the longer one) before the bar returns to its live state.
SEGMENT_PUMP_DELAY_MS = 0
RESULT_TOAST_MS = 1700
RESULT_TOAST_WITH_HINT_MS = 2200
# Bounded bookkeeping for history tickets awaiting async settlement.
MAX_TRACKED_HISTORY_TICKETS = 1024
CAPTURE_LOST_NOTICE = "麦克风已断开"

# Paste details that already copied the text and need their own wording.
SKIPPED_COPIED_TITLES: dict[str, str] = {
    "readonly_target": "目标不可编辑，已复制到剪贴板",
    "terminal_multiline": "多行内容已复制，请在终端确认后粘贴",
}


@dataclass(frozen=True)
class UtteranceContext:
    """Delivery rules frozen when an utterance (hold or continuous segment) is captured.

    Destination, trigger mode, target window and LLM-context eligibility must
    never be re-read from live config at delivery time.
    """

    session_id: str = ""
    trigger_mode: str = ""
    history_only: bool | None = None
    target_hwnd: int = 0
    target_app: str = ""
    baseline_snapshot: object = None
    strict_subfocus: bool = True
    append_fallback: bool = False
    preset_id: str = ""
    allow_llm_context: bool = True


@dataclass
class _PendingHistoryRecord:
    session_id: str
    seq: int
    raw_text: str
    polished_text: str
    source: str
    duration_ms: int
    trigger_mode: str
    model: str
    speaker_id: int = 0
    speaker_route: str = ""
    captured_at: int = 0
    target_hwnd: int = 0
    target_app: str = ""
    utterance: UtteranceContext | None = None


@dataclass
class _SegmentContext:
    """Continuous-session identity fixed when a segment is captured, not when it is recognized."""

    session_id: str
    seq: int
    source: str
    speakers: SpeakerSession
    trigger_mode: str = TRIGGER_MODE_CONTINUOUS
    model: str = ""
    # Wall-clock ms when the utterance started (0 = unknown).
    captured_at: int = 0
    # True when speaker_route is the lane that captured the audio, not a
    # guess from which mixed lane was louder.
    route_reliable: bool = False
    target_hwnd: int = 0
    target_app: str = ""
    # Speaker embedding function; None = dictation's log-mel summary.
    embed: object = None
    recognized_text: str | None = None
    speaker_vector: np.ndarray | None = None
    speaker_learnable: bool | None = None
    utterance: UtteranceContext | None = None


@dataclass
class _QueuedSegment:
    """One captured utterance waiting for the recognizer.

    Everything that belongs to the audio travels with it, so the queue cannot
    fall out of step with a parallel list of routes or contexts.
    """

    audio: np.ndarray
    route: str = ""
    context: _SegmentContext | None = None
    recognized_turns: list | None = None
    prefetch_attempted: bool = False

    def load_audio(self) -> np.ndarray:
        from sayink.audio_recovery import StoredAudio

        return self.audio.load() if isinstance(self.audio, StoredAudio) else self.audio


def _noop(*_args: Any, **_kwargs: Any) -> None:
    return None


def _false() -> bool:
    return False


def _zero_target() -> tuple[int, str]:
    return (0, "")


def _cancel_conflict(**_kw: Any) -> str:
    return "cancel"


def _identity_str(msg: str) -> str:
    return msg


def _default_platform() -> str:
    return sys.platform


def _default_watchdog_ms() -> int:
    return OUTPUT_WATCHDOG_MS


def _default_context_window() -> float:
    return RECENT_OUTPUT_CONTEXT_S


def _empty_str() -> str:
    return ""


_PIPELINE_CALLBACK_DEFAULTS: dict[str, Callable[..., Any]] = {
    "schedule_later": _noop,
    "pump_queue": _noop,
    "finish_empty_recognition": _noop,
    "refresh_continuous_ui": _noop,
    "maybe_finish_dictation_speakers": _noop,
    "stop_continuous_user_session": _noop,
    "ask_history_output_conflict": _cancel_conflict,
    "glossary_for_pending_segment": list,
    "continuous_session_active": _false,
    "continuous_user_stopped": _false,
    "has_queued_segments": _false,
    "meeting_active": _false,
    "exit_started": _false,
    "hold_target": _zero_target,
    "clear_transcribing": _noop,
    "on_watchdog_reset": _noop,
    "friendly_error": _identity_str,
    "copy_to_clipboard": pyperclip.copy,
    "now_monotonic": time.monotonic,
    "now_wall": time.time,
    "platform_name": _default_platform,
    "watchdog_ms": _default_watchdog_ms,
    "recent_context_window_s": _default_context_window,
    "polish_reject_fn": polish_rejection_reason,
    "correction_reject_fn": correction_rejection_reason,
    # Remote LLM consent: returns the unconfirmed ``scheme://host`` or "".
    "llm_remote_host_pending": _empty_str,
    "notify_llm_consent_required": _noop,
    # A meeting segment whose history write failed (or was refused).
    "record_unsaved_meeting_segment": _noop,
}


class OutputPipeline:
    """Serializes LLM polish/correction, paste dispatch, watchdogs, and history writes."""

    def __init__(
        self,
        *,
        config: Any = None,
        polisher: Any = None,
        paster: Any = None,
        history: Any = None,
        floating: Any = None,
        tray: Any = None,
        recorder: Any = None,
        output_text_dispatch: Callable[..., None] | None = None,
        handle_paste_result_dispatch: Callable[..., None] | None = None,
        schedule_queue_pump: Callable[[], None] | None = None,
        **callbacks: Callable[..., Any] | None,
    ) -> None:
        self.config = config
        self.polisher = polisher
        self.paster = paster
        self.history = history
        self.floating = floating
        self.tray = tray
        self.recorder = recorder
        self.output_text_dispatch = output_text_dispatch
        self.handle_paste_result_dispatch = handle_paste_result_dispatch
        self._bind_callbacks(schedule_queue_pump, callbacks)

        self.busy: bool = False
        self.token: int = 0
        self.invalidated_token: int = 0
        self.raw_text: str = ""
        self.stage: OutputStage = OutputStage.IDLE
        self.stage_text: str = ""
        self.stage_record: SegmentRecord | None = None
        self.output_pending_record: _PendingHistoryRecord | None = None
        self.pending_record: _PendingHistoryRecord | None = None
        self.llm_mode: str = LLM_MODE_POLISH
        self.active_preset_label: str = ""
        self.glossary: list = []
        self.last_output_text: str = ""
        self.last_output_at: float = 0.0
        self.dropped_slices: int = 0
        self.keep_unsaved_continuous_text: bool = False
        self.history_failure_notified: bool = False
        self.last_history_failure_warn_at: float | None = None
        self.polish_error_failed: bool = False
        # LLM context is only what actually went to the LLM, per target window.
        self._last_llm_context_text: str = ""
        self._last_llm_context_hwnd: int = 0
        self._last_llm_context_at: float = 0.0
        self.utterance_llm_used: bool | None = None
        # Async history settlement (enabled once the App bridges the writer callback).
        self.ticket_settlement_enabled: bool = False
        self.tracked_tickets: dict[int, tuple[SegmentRecord, str, dict | None]] = {}
        self.unsaved_records: list[SegmentRecord] = []
        self.retry_tickets: set[int] = set()
        self.retry_saved_count: int = 0
        # Conflict-cancel: every homeless segment of the stopped session, in seq order.
        self.homeless_entries: list[tuple[int, int, str]] = []
        # A continuous destination chosen in the conflict dialog after capture.
        self.continuous_destination_override: bool | None = None
        # Set when the microphone dropped while buffered speech is still draining.
        self.capture_lost_notice: str = ""

    def _bind_callbacks(
        self,
        schedule_queue_pump: Callable[[], None] | None,
        callbacks: dict[str, Callable[..., Any] | None],
    ) -> None:
        for name, default_fn in _PIPELINE_CALLBACK_DEFAULTS.items():
            provided = callbacks.pop(name, None)
            setattr(self, name, provided if provided is not None else default_fn)
        self.schedule_queue_pump = schedule_queue_pump or (
            lambda: self.schedule_later(SEGMENT_PUMP_DELAY_MS, self.pump_queue)
        )
        if callbacks:
            unknown = ", ".join(sorted(callbacks))
            raise TypeError(f"Unexpected OutputPipeline callback argument(s): {unknown}")

    def snapshot(self) -> OutputPipelineSnapshot:
        return OutputPipelineSnapshot(
            busy=bool(self.busy),
            stage=self.stage,
            stage_text=str(self.stage_text or ""),
            last_output_text=str(self.last_output_text or ""),
            last_output_at=float(self.last_output_at or 0.0),
            dropped_slices=int(self.dropped_slices or 0),
        )

    def _dispatch_output(self, text: str, *, degraded_from_polish: bool = False) -> None:
        target = self.output_text_dispatch or self.output_text
        if degraded_from_polish:
            target(text, degraded_from_polish=True)
        else:
            target(text)

    def _dispatch_paste_result(
        self,
        result: PasteResult | str,
        *,
        degraded_from_polish: bool = False,
        record: SegmentRecord | None = None,
        token: int | None = None,
    ) -> None:
        if token is not None and token <= self.invalidated_token:
            log.warning(
                "忽略已超时失效的粘贴回调 (token=%s, invalidated=%s)",
                token,
                self.invalidated_token,
            )
            return
        if token is not None and token < self.token:
            target_app = result.target_app if isinstance(result, PasteResult) else ""
            self.enqueue_history_record(record, target_app=target_app)
            self.maybe_finish_dictation_speakers()
            return
        target = self.handle_paste_result_dispatch or self.handle_paste_result
        if token is not None:
            try:
                target(result, degraded_from_polish=degraded_from_polish, record=record, token=token)
                return
            except TypeError:
                pass
        target(result, degraded_from_polish=degraded_from_polish, record=record)

    def record_for_output(self) -> _PendingHistoryRecord | None:
        """Row owned by the sentence in polish/paste, else the hold still being recognized."""
        if self.output_pending_record is not None:
            return self.output_pending_record
        return self.pending_record

    def claim_pending_record_for_output(self) -> None:
        """Detach the current row from the hold slot before polish starts.

        A new hold is allowed while polish runs. Esc on that hold clears
        ``pending_record``; this sentence must already live elsewhere, with
        the window captured when it was spoken.
        """
        if self.output_pending_record is None and self.pending_record is not None:
            self.output_pending_record = self.pending_record
            self.pending_record = None

    def paste_target_for_output(self) -> tuple[int, str]:
        """Window remembered for the utterance still waiting to go out.

        An explicit empty target (SayInk itself, or no foreground window)
        must not fall through to an older hold: that would type into the
        wrong application.
        """
        pending = self.record_for_output()
        if pending is not None:
            return int(getattr(pending, "target_hwnd", 0) or 0), str(getattr(pending, "target_app", "") or "")
        held = self.hold_target()
        if isinstance(held, tuple) and len(held) == 2:
            return int(held[0] or 0), str(held[1] or "")
        return 0, ""

    def utterance_for_output(self) -> UtteranceContext | None:
        """Frozen delivery rules of the utterance about to go out (None for legacy rows)."""
        utterance = getattr(self.record_for_output(), "utterance", None)
        return utterance if isinstance(utterance, UtteranceContext) else None

    def utterance_history_only(
        self, record: Any, utterance: UtteranceContext | None = None
    ) -> bool:
        """History-only for this utterance, not for a setting that changed later."""
        if utterance is not None and utterance.history_only is not None:
            override = self.continuous_destination_override
            if override is not None and utterance.trigger_mode == TRIGGER_MODE_CONTINUOUS:
                return override
            return bool(utterance.history_only)
        source = getattr(record, "source", None) if record is not None else None
        trigger = getattr(record, "trigger_mode", None) if record is not None else None
        if trigger == TRIGGER_MODE_MEETING:
            trigger = None
        return _ConfigRules.history_only_for_capture(self.config, source, trigger_mode=trigger)

    def history_output_blocked(self) -> bool:
        if self.meeting_active():
            return False
        fn = getattr(self.config, "history_only_output", None)
        only_history = fn() if callable(fn) else _ConfigRules.history_only_output(self.config)
        return bool(only_history) and not bool(self.config.get("history.enabled", True))

    def ensure_history_output_ready(self) -> bool:
        """Block a new recording until history-only with history off is resolved."""
        if not self.history_output_blocked():
            return True
        choice = self.ask_history_output_conflict()
        if choice == "history":
            self.config.set("history.enabled", True)
            return True
        if choice == "paste":
            self._set_output_mode_paste()
            return True
        return False

    def _set_output_mode_paste(self) -> None:
        setter = getattr(self.config, "set_output_mode", None)
        if callable(setter):
            setter(OUTPUT_MODE_PASTE)
        else:
            _ConfigRules.set_output_mode(self.config, OUTPUT_MODE_PASTE)

    def copy_clipboard(self, text: str) -> bool:
        try:
            self.copy_to_clipboard(text)
        except Exception:
            log.warning("写入剪贴板失败", exc_info=True)
            return False
        return True

    def release_unsaved_hold(self) -> None:
        """Drop the cancel-and-copy hold once the stopped session has no tail left."""
        if not self.keep_unsaved_continuous_text:
            return
        if getattr(self.recorder, "is_continuous", False):
            return
        if self.has_queued_segments():
            return
        self.keep_unsaved_continuous_text = False

    def recent_output_context(self, target_hwnd: int | None = None) -> str:
        """The previous LLM-processed output for the same target window, if recent.

        ``直出原文`` (``preset="raw"``) and non-LLM utterances are never recorded
        here, and an old sentence from another window would only mislead (or
        leak private text into an unrelated app's prompt).
        """
        if not self._last_llm_context_text:
            return ""
        stamped = (
            min(self._last_llm_context_at, self.last_output_at)
            if self.last_output_at
            else self._last_llm_context_at
        )
        if self.now_monotonic() - stamped > self.recent_context_window_s():
            return ""
        if target_hwnd is not None and int(target_hwnd or 0) != self._last_llm_context_hwnd:
            return ""
        return self._last_llm_context_text

    def _direct_output_llm_eligible(self, target_app: str) -> bool:
        """Eligibility when ``output_text`` is invoked directly without ``deliver_recognized_text``."""
        if self.config is None or not bool(self.config.get("llm.enabled", False)):
            return False
        rule = resolve_app_style_rule(target_app, self.config.get("llm.app_presets", []))
        if rule is not None and rule.preset_id == "raw":
            return False
        if not polish_settings_complete(
            self.config.get("llm.api_url", ""),
            self.config.get("llm.api_key", ""),
            self.config.get("llm.model_name", ""),
        ):
            return False
        try:
            return not str(self.llm_remote_host_pending() or "")
        except Exception:
            return False

    def _remember_llm_context(
        self,
        text: str,
        hwnd: int,
        target_app: str,
        utterance: UtteranceContext | None,
    ) -> None:
        used = self.utterance_llm_used
        self.utterance_llm_used = None
        if utterance is not None and not utterance.allow_llm_context:
            return
        if used is False:
            return
        if used is None and not self._direct_output_llm_eligible(target_app):
            return
        self._last_llm_context_text = text
        self._last_llm_context_hwnd = int(hwnd or 0)
        self._last_llm_context_at = float(self.now_monotonic())

    def mark_busy(self) -> None:
        self.busy = True
        self.token += 1
        token = self.token
        self.schedule_later(self.watchdog_ms(), lambda: self.release_stuck_output(token))

    def _cancel_polisher_on_watchdog(self) -> None:
        cancel_rw = getattr(self.polisher, "cancel_rewrite", None)
        if callable(cancel_rw):
            cancel_rw()
        cancel_fn = getattr(self.polisher, "cancel", None)
        if callable(cancel_fn):
            cancel_fn()

    def _salvage_timed_out_stage_text(self, timed_out_polish: bool) -> None:
        if self.stage_text.strip():
            self.last_output_text = self.stage_text
            self.last_output_at = float(self.now_monotonic())
            try:
                self.copy_to_clipboard(self.stage_text)
            except Exception as exc:
                log.warning("复制超时句子到剪贴板失败: %s", exc)
            else:
                self.floating.show_info("输出超时 · 已复制", "可按 Ctrl+V 粘贴")
            return
        if timed_out_polish:
            self.floating.show_error("改写超时，原选区未改动")

    def release_stuck_output(self, token: int) -> None:
        if not self.busy or token != self.token:
            return
        raw = self.raw_text
        if self.stage is OutputStage.POLISH and raw.strip():
            # The polisher never answered. Its result would have been this
            # sentence anyway, so output the raw words instead of losing them.
            log.error("润色超时未回调，改为输出原文")
            self.polish_error_failed = True
            self.polisher.cancel()
            self.stage = OutputStage.PASTE
            # The paste stage gets its own watchdog window; the one that just
            # fired must not be the last line of defence for it.
            self._dispatch_output(raw, degraded_from_polish=True)
            if self.stage is OutputStage.PASTE and self.token == token:
                self.mark_busy()
            return
        timed_out_polish = self.stage is OutputStage.POLISH
        if timed_out_polish:
            self._cancel_polisher_on_watchdog()
        log.error("输出流程超时未回调，释放队列以免后续语音卡住")
        self.busy = False
        self.invalidated_token = self.token
        self.token += 1
        self.stage = OutputStage.IDLE
        # The shortcut may still be waiting out its short delay. Sending it
        # after telling the user to paste by hand inserts the sentence twice.
        self.paster.cancel()
        self.on_watchdog_reset()
        if self.tray is not None:
            self.tray.set_activity_tooltip(None)
        record = self.stage_record
        self.stage_record = None
        self._salvage_timed_out_stage_text(timed_out_polish)
        self.stage_text = ""
        if record is not None:
            # The paster's callback (which normally saves the row) is gone with
            # the cancel; the words are still worth keeping in history.
            self.enqueue_history_record(record, target_app="")
        self.pump_queue()

    def _resolve_target_polish_plan(
        self,
        pending: _PendingHistoryRecord | None,
        base_mode: str,
    ) -> tuple[bool, str, str, str]:
        """Return ``(skip_llm, effective_mode, preset_label, style_instruction)`` for ``pending``."""
        is_meeting = pending is not None and pending.trigger_mode == TRIGGER_MODE_MEETING
        if is_meeting:
            return False, base_mode, "", ""
        target_app = pending.target_app if pending is not None else self.paste_target_for_output()[1]
        rule = resolve_app_style_rule(target_app, self.config.get("llm.app_presets", []))
        if rule is None:
            return False, base_mode, "", ""
        label = app_preset_label(rule.preset_id)
        if rule.preset_id == "raw":
            return True, base_mode, label, ""
        if rule.preset_id == "correct":
            return False, LLM_MODE_CORRECT, label, ""
        inst = app_preset_instruction(rule.preset_id)
        if rule.custom_instruction:
            inst = f"{inst}\n{rule.custom_instruction}".strip()
        return False, base_mode, label, inst

    def _show_polishing_hud(self, text: str, *, action: str, preset_label: str) -> None:
        if self.continuous_user_stopped():
            suffix = f" · {preset_label}" if preset_label else ""
            self.floating.show_continuous_stopped(f"正在处理剩余内容 · 正在{action}{suffix}")
            return
        try:
            self.floating.show_polishing(text, action=action, preset_label=preset_label)
        except TypeError:
            self.floating.show_polishing(text, action=action)

    def _invoke_polisher(
        self,
        text: str,
        *,
        api_url: str,
        api_key: str,
        model_name: str,
        prompt: str,
        mode: str,
        style_instruction: str,
    ) -> None:
        common = dict(
            mode=mode,
            glossary=self.glossary,
            context=self.recent_output_context(self.paste_target_for_output()[0]),
            target_app="",
            proxy_mode=self.config.get("llm.proxy_mode", "system"),
            proxy_bypass_suffixes=str(self.config.get("llm.proxy_bypass_suffixes", "") or ""),
        )
        if style_instruction:
            try:
                self.polisher.polish(
                    text,
                    api_url,
                    api_key,
                    model_name,
                    prompt,
                    style_instruction=style_instruction,
                    **common,
                )
                return
            except TypeError:
                prompt = f"{prompt}\n\n{style_instruction}".strip() if prompt else style_instruction
        self.polisher.polish(text, api_url, api_key, model_name, prompt, **common)

    def deliver_recognized_text(self, text: str) -> None:
        self.claim_pending_record_for_output()
        self.mark_busy()
        self.polish_error_failed = False
        self.raw_text = text
        self.stage = OutputStage.POLISH
        self.stage_text = ""
        llm_enabled = bool(self.config.get("llm.enabled", False))
        pending = self.record_for_output()
        if pending is not None and pending.trigger_mode == TRIGGER_MODE_MEETING:
            llm_enabled = bool(self.config.get("meeting.polish_segments", False)) and llm_enabled
        base_mode = normalize_llm_mode(self.config.get("llm.mode", LLM_MODE_POLISH))
        skip_llm, mode, preset_label, style_instruction = self._resolve_target_polish_plan(
            pending, base_mode
        )
        self.active_preset_label = preset_label
        if skip_llm:
            llm_enabled = False
        api_url = self.config.get("llm.api_url", "")
        api_key = self.config.get("llm.api_key", "")
        model_name = self.config.get("llm.model_name", "")
        prompt = self.config.get(prompt_config_key(mode), "")
        self.llm_mode = mode
        self.glossary = list(self.glossary_for_pending_segment() or [])
        self.utterance_llm_used = False
        ready = llm_enabled and polish_settings_complete(api_url, api_key, model_name)

        if ready and not self._remote_llm_consented():
            self._dispatch_output(text, degraded_from_polish=True)
        elif ready:
            action = "纠错" if mode == LLM_MODE_CORRECT else "润色"
            self.utterance_llm_used = True
            self.tray.set_activity_tooltip("correcting" if mode == LLM_MODE_CORRECT else "polishing")
            self._show_polishing_hud(text, action=action, preset_label=preset_label)
            self._invoke_polisher(
                text,
                api_url=api_url,
                api_key=api_key,
                model_name=model_name,
                prompt=prompt,
                mode=mode,
                style_instruction=style_instruction,
            )
        elif llm_enabled:
            log.warning("文字润色已开启但配置不完整，直接输出原文")
            self._dispatch_output(text, degraded_from_polish=True)
        else:
            self._dispatch_output(text)
        if self.busy:
            self.schedule_queue_pump()

    def _remote_llm_consented(self) -> bool:
        """Dictation text goes to a remote LLM only after the host was confirmed."""
        try:
            host = str(self.llm_remote_host_pending() or "")
        except Exception:
            log.warning("检查远程 AI 服务确认状态失败，本句不发送", exc_info=True)
            return False
        if not host:
            return True
        log.warning("文字润色的远程 AI 服务尚未确认，本句直接输出原文")
        self.notify_llm_consent_required(host)
        return False

    def on_polish_complete(self, polished_text: str) -> None:
        if self.stage is not OutputStage.POLISH:
            log.warning("润色结果晚于超时到达，已输出原文，忽略")
            return
        self.polish_error_failed = False
        raw = self.raw_text
        # Check what would actually be pasted: normalisation can strip a reply
        # down to nothing (「。」) or drop a numbered clause, and a reply that is
        # empty after normalisation must not take the user's sentence with it.
        polished = normalize_asr_output(polished_text or "")
        if not polished.strip():
            reason = "结果为空"
        elif self.llm_mode == LLM_MODE_CORRECT:
            # Correction promised to change almost nothing, so a reply that
            # rewrites or trims the sentence is a failed correction, not a
            # style choice; the raw words are the safer output.
            reason = self.correction_reject_fn(raw, polished, self.glossary)
        else:
            reason = self.polish_reject_fn(raw, polished)
        if reason:
            log.warning(
                "%s结果未通过保真检查（%s；原文 %d 字，结果 %d 字），改为输出原文",
                "纠错" if self.llm_mode == LLM_MODE_CORRECT else "润色",
                reason,
                len(raw),
                len(polished),
            )
            self._dispatch_output(raw, degraded_from_polish=True)
            return
        self._dispatch_output(polished)

    def on_polish_error(self, error_msg: str) -> None:
        if self.stage is not OutputStage.POLISH:
            return
        log.warning("后处理失败，降级输出原文: %s", error_msg)
        self.polish_error_failed = True
        self._dispatch_output(
            self.raw_text,
            degraded_from_polish=True,
        )

    def _release_empty_output_slot(self) -> None:
        self.busy = False
        self.polish_error_failed = False
        self.stage = OutputStage.IDLE
        self.stage_text = ""
        self.stage_record = None
        if self.output_pending_record is not None:
            self.output_pending_record = None
        else:
            self.pending_record = None
        self.finish_empty_recognition()

    def _copy_homeless(self, text: str, record: SegmentRecord | None = None) -> bool:
        """Append ``text`` to the stopped session's homeless segments and copy them all in ``seq`` order."""
        order_idx = len(self.homeless_entries)
        seq = int(record.seq) if record is not None and record.seq is not None else (1_000_000 + order_idx)
        self.homeless_entries.append((seq, order_idx, text))
        ordered = [t for _, _, t in sorted(self.homeless_entries) if t.strip()]
        joined = "\n".join(ordered)
        self.last_output_text = joined
        self.last_output_at = float(self.now_monotonic())
        return self.copy_clipboard(joined)

    def _resolve_continuous_unsaved_conflict(
        self,
        text: str,
        *,
        degraded_from_polish: bool,
        record: SegmentRecord | None,
        preserve_tail: bool,
    ) -> str:
        """Continuous history-only output while history is off.

        Returns ``"handled"`` (copied out, nothing more to do), or the
        destination the user picked: ``"history"`` / ``"paste"``.
        """
        if not preserve_tail:
            self.token += 1
            self.stage = OutputStage.PASTE
            choice = self.ask_history_output_conflict(listening=True)
            if choice == "history":
                self.config.set("history.enabled", True)
                return "history"
            if choice == "paste":
                self._set_output_mode_paste()
                self.continuous_destination_override = False
                return "paste"
            self.keep_unsaved_continuous_text = True
            self.homeless_entries = []
        copied = self._copy_homeless(text, record)
        if not preserve_tail:
            self.stop_continuous_user_session()
        self._dispatch_paste_result(
            PasteResult(
                "clipboard" if copied else "failed",
                detail="history_conflict" if copied else "clipboard_failed",
            ),
            degraded_from_polish=degraded_from_polish,
            record=record,
        )
        self.release_unsaved_hold()
        return "handled"

    def _begin_async_paste(
        self,
        text: str,
        *,
        paste_hwnd: int,
        paste_app: str,
        degraded_from_polish: bool,
        record: SegmentRecord | None,
        utterance: UtteranceContext | None = None,
    ) -> None:
        self.stage = OutputStage.PASTE
        self.stage_text = text
        self.stage_record = record
        self.mark_busy()
        paste_token = self.token
        called = False

        def _on_paste_done(
            result: PasteResult | str,
            _record: SegmentRecord | None = record,
            _tok: int = paste_token,
        ) -> None:
            nonlocal called
            if called:
                log.warning("忽略重复的粘贴回调 (token=%s)", _tok)
                return
            called = True
            self._dispatch_paste_result(
                result,
                degraded_from_polish=degraded_from_polish,
                record=_record,
                token=_tok,
            )

        self._paste_with_utterance(
            text,
            _on_paste_done,
            paste_hwnd=paste_hwnd,
            paste_app=paste_app,
            utterance=utterance,
        )

    def _paste_with_utterance(
        self,
        text: str,
        callback: Callable[..., None],
        *,
        paste_hwnd: int,
        paste_app: str,
        utterance: UtteranceContext | None,
    ) -> None:
        """Paste with the utterance's own snapshot and focus policy (legacy pasters: plain call)."""
        if utterance is None:
            self.paster.paste_async(text, callback, target_hwnd=paste_hwnd, target_app=paste_app)
            return
        try:
            self.paster.paste_async(
                text,
                callback,
                target_hwnd=paste_hwnd,
                target_app=paste_app,
                baseline_snapshot=utterance.baseline_snapshot,
                strict_subfocus=utterance.strict_subfocus,
                append_fallback=utterance.append_fallback,
            )
        except TypeError:
            self.paster.paste_async(text, callback, target_hwnd=paste_hwnd, target_app=paste_app)

    def output_text(self, text: str, *, degraded_from_polish: bool = False) -> None:
        if self.exit_started():
            return
        self.clear_transcribing()
        text = normalize_asr_output(text)
        if not text.strip():
            self._release_empty_output_slot()
            return

        paste_hwnd, paste_app = self.paste_target_for_output()
        utterance = self.utterance_for_output()
        record = self.freeze_pending_history_record(text, degraded_from_polish)
        is_meeting_row = record is not None and record.trigger_mode == TRIGGER_MODE_MEETING
        forced_meeting_history = self.meeting_active() and not is_meeting_row
        history_only = self.utterance_history_only(record, utterance) or forced_meeting_history
        if not is_meeting_row:
            self.last_output_text = text
            self.last_output_at = self.now_monotonic()
            self._remember_llm_context(text, paste_hwnd, paste_app, utterance)
        if self._needs_unsaved_continuous_prompt(is_meeting_row, history_only):
            preserve_tail = self.keep_unsaved_continuous_text and not getattr(
                self.recorder, "is_continuous", False
            )
            outcome = self._resolve_continuous_unsaved_conflict(
                text,
                degraded_from_polish=degraded_from_polish,
                record=record,
                preserve_tail=preserve_tail,
            )
            if outcome == "handled":
                return
            forced_meeting_history = self.meeting_active() and not is_meeting_row
            history_only = outcome == "history" or forced_meeting_history
        if is_meeting_row or history_only:
            self._record_only_without_paste(
                text,
                is_meeting_row=is_meeting_row,
                forced_meeting_history=forced_meeting_history,
                degraded_from_polish=degraded_from_polish,
                record=record,
            )
            return
        self._begin_async_paste(
            text,
            paste_hwnd=paste_hwnd,
            paste_app=paste_app,
            degraded_from_polish=degraded_from_polish,
            record=record,
            utterance=utterance,
        )

    def _needs_unsaved_continuous_prompt(self, is_meeting_row: bool, history_only: bool) -> bool:
        if is_meeting_row or self.meeting_active() or not history_only:
            return False
        if bool(self.config.get("history.enabled", True)):
            return False
        is_cont = getattr(self.recorder, "is_continuous", False) is True
        return is_cont or self.keep_unsaved_continuous_text

    def _record_only_without_paste(
        self,
        text: str,
        *,
        is_meeting_row: bool,
        forced_meeting_history: bool,
        degraded_from_polish: bool,
        record: SegmentRecord | None,
    ) -> None:
        history_on = bool(self.config.get("history.enabled", True))
        store_ok = getattr(self.history, "disabled", False) is not True
        saved = is_meeting_row or (history_on and store_ok)
        if forced_meeting_history and not saved:
            self.copy_clipboard(text)
        if saved:
            detail = ""
        else:
            detail = "history_unavailable" if history_on else "history_disabled"
        log.info("只记录不粘贴：%d 字%s", len(text), "" if saved else "（未保存）")
        self._dispatch_paste_result(
            PasteResult("recorded", detail=detail),
            degraded_from_polish=degraded_from_polish,
            record=record,
        )

    def freeze_pending_history_record(
        self,
        output_text: str,
        degraded_from_polish: bool,
    ) -> SegmentRecord | None:
        pending = self.output_pending_record
        if pending is not None:
            self.output_pending_record = None
        else:
            pending = self.pending_record
            self.pending_record = None
        if pending is None:
            return None
        raw_text = pending.raw_text or output_text
        if degraded_from_polish:
            polished_text = output_text
        elif pending.raw_text and output_text != pending.raw_text:
            polished_text = output_text
        else:
            polished_text = pending.polished_text
        return SegmentRecord(
            session_id=pending.session_id,
            seq=pending.seq,
            created_at=int(self.now_wall() * 1000),
            raw_text=raw_text,
            polished_text=polished_text,
            source=pending.source,
            duration_ms=pending.duration_ms,
            target_app="",
            trigger_mode=pending.trigger_mode,
            model=pending.model,
            speaker_id=int(pending.speaker_id),
            speaker_route=pending.speaker_route or "",
            captured_at=int(pending.captured_at or 0),
        )

    def show_post_output_floating(
        self,
        title: str,
        hint: str,
        *,
        stopped_text: str,
        use_info: bool,
        refresh_ms: int,
    ) -> None:
        if self.continuous_session_active():
            if use_info:
                self.floating.show_info(title, hint)
            else:
                self.floating.show_success(title, hint)
            self.schedule_later(refresh_ms, self.refresh_continuous_ui)
        elif self.continuous_user_stopped():
            self.floating.show_continuous_stopped(stopped_text)
            self.schedule_later(RESULT_TOAST_WITH_HINT_MS, self.refresh_continuous_ui)
        elif getattr(self.recorder, "is_recording", False):
            self.floating.show_recording()
        elif use_info:
            self.floating.show_info(title, hint)
        else:
            self.floating.show_success(title, hint)

    @staticmethod
    def _unpack_paste_result(result: PasteResult | str) -> tuple[str, str, str]:
        if isinstance(result, PasteResult):
            return result.status, result.target_app, result.detail
        # Compatibility for tests and third-party integrations using the old callback.
        return result.split(":", 1)[0], "", result.partition(":")[2]

    def _update_post_paste_tray_tooltip(self) -> None:
        if getattr(self.recorder, "is_recording", False) and not getattr(
            self.recorder, "is_continuous", False
        ):
            # The next hold is already being recorded.
            self.tray.set_activity_tooltip("recording")
        elif self.meeting_active():
            # Preserve active meeting tooltip while draining leftover segments.
            return
        else:
            self.tray.set_activity_tooltip(
                "listening" if self.continuous_session_active() else None
            )

    @staticmethod
    def _recorded_title_suffix(polish_failed: bool, degraded_from_polish: bool) -> str:
        if polish_failed:
            return " · 润色未生效（密钥/网络异常），已保留原文"
        return "（原文）" if degraded_from_polish else ""

    def _show_recorded_paste_feedback(
        self,
        *,
        record: SegmentRecord | None,
        error_detail: str,
        polish_failed: bool,
        degraded_from_polish: bool,
        dropped: int,
        dropped_suffix: str,
    ) -> None:
        saved = error_detail != "history_disabled"
        meeting_row = getattr(record, "trigger_mode", None) == TRIGGER_MODE_MEETING
        if self.meeting_active() and not meeting_row:
            log.info("会议进行中：遗留口述分片已归档至历史（不发送粘贴键）")
            return
        done = "已记录" if meeting_row else "已保存到历史"
        suffix = self._recorded_title_suffix(polish_failed, degraded_from_polish)
        title = (done if saved else "未保存 · 历史已关闭") + suffix
        hint = (
            "当前为「仅保存到历史」，但历史已关闭；请在设置中打开历史或改为输入当前光标"
            if not saved
            else ("" if meeting_row else "未输入其他应用")
        )
        if dropped and not meeting_row:
            hint = f"{hint}{dropped_suffix}" if hint else f"有 {dropped} 段未识别"
        is_clean_save = saved and not degraded_from_polish and not polish_failed
        use_info = (not is_clean_save) or bool(dropped and not meeting_row)
        self.show_post_output_floating(
            title,
            hint,
            stopped_text=f"剩余内容：{title}",
            use_info=use_info,
            refresh_ms=RESULT_TOAST_MS,
        )

    def _show_unverified_paste_feedback(
        self,
        *,
        error_detail: str,
        target_app: str,
        paste_hint: str,
        polish_failed: bool,
        dropped: int,
        dropped_suffix: str,
    ) -> None:
        # Ctrl+V already went out, then the foreground changed. Saying
        # 「已复制」 here invites a second manual paste of the same words.
        log.info("已发送粘贴快捷键，但焦点随后切换，无法确认是否已插入")
        if error_detail == "clipboard_changed_after_send":
            unverified_hint = "焦点已切换，剪贴板内容已变化或无法核对"
        else:
            unverified_hint = (
                f"焦点已切换，若 {target_app} 未出字{paste_hint}"
                if target_app else f"焦点已切换，若未出字{paste_hint}"
            )
        if polish_failed:
            unverified_hint = f"{unverified_hint} · 润色未生效（密钥/网络异常）"
        if dropped:
            unverified_hint = f"{unverified_hint}{dropped_suffix}"
        self.show_post_output_floating(
            "已发送 · 未确认",
            unverified_hint,
            stopped_text=f"剩余内容：已发送 · 未确认 · {unverified_hint}",
            use_info=True,
            refresh_ms=RESULT_TOAST_WITH_HINT_MS,
        )

    def _history_saves_record(self, record: SegmentRecord | None) -> bool:
        if record is None:
            return False
        if bool(getattr(self.history, "disabled", False)):
            return False
        if record.trigger_mode == TRIGGER_MODE_MEETING:
            return True
        return bool(self.config.get("history.enabled", True)) if self.config is not None else True

    def _unsaved_last_output_hint(self) -> str:
        if not self.last_output_text.strip():
            return "本次转写未保存到历史，可重新转写"
        raw_hk = (
            str(self.config.get("audio.paste_last_hotkey", "") or "").strip()
            if self.config is not None
            else ""
        )
        if raw_hk:
            return f"本次转写未保存到历史，已暂存为「上一句」，可按 {format_hotkey(raw_hk)} 重贴"
        return "本次转写未保存到历史，已暂存为「上一句」，可在设置绑定重贴快捷键后重贴"

    def _paste_error_message(self, record: SegmentRecord | None) -> str:
        base = self.friendly_error("输出失败")
        if self._history_saves_record(record):
            return f"{base}\n本次转写已保存到历史，也可稍后重贴上一句"
        if self.last_output_text.strip():
            return f"{base}\n{self._unsaved_last_output_hint()}"
        return base

    def _show_skipped_paste_feedback(
        self,
        *,
        record: SegmentRecord | None,
        error_detail: str,
        target_app: str,
        polish_failed: bool,
        dropped_suffix: str,
        paste_hint: str = "可按 Ctrl+V 粘贴",
    ) -> None:
        copied_title = SKIPPED_COPIED_TITLES.get(error_detail)
        if copied_title:
            title = copied_title
        elif error_detail == "clipboard_changed":
            title = "剪贴板已变化，已取消自动粘贴"
        elif error_detail == "clipboard_unavailable":
            title = "无法核对剪贴板，已取消自动粘贴"
        else:
            title = "已跳过剪贴板写入，已取消自动粘贴"
        log.info("%s%s", title, f"（目标：{target_app}）" if target_app else "")
        if copied_title:
            hint = f"目标应用：{target_app}" if target_app else paste_hint
        elif self._history_saves_record(record):
            hint = "本次转写可在历史中复制"
        else:
            hint = self._unsaved_last_output_hint()
        if polish_failed:
            hint += " · 润色未生效（密钥/网络异常）"
        hint += dropped_suffix
        self.show_post_output_floating(
            title,
            hint,
            stopped_text=f"剩余内容：{title} · {hint}",
            use_info=True,
            refresh_ms=RESULT_TOAST_WITH_HINT_MS,
        )

    def _show_clipboard_paste_feedback(
        self,
        *,
        error_detail: str,
        target_app: str,
        paste_key: str,
        paste_hint: str,
        polish_failed: bool,
        dropped: int,
        dropped_suffix: str,
    ) -> None:
        contextual = {
            "elevated": (
                "目标窗口为管理员权限，已复制到剪贴板",
                f"目标为管理员窗口，已复制，请按 {paste_key}",
            ),
            "ime_active": (
                "检测到输入法候选窗活跃，已复制到剪贴板",
                f"检测到正在拼音选词，已复制，请按 {paste_key}",
            ),
            "session_changed": (
                "桌面会话已切换或锁屏，已复制到剪贴板",
                f"桌面会话已切换，已复制，请按 {paste_key}",
            ),
            "subfocus_changed": (
                "开口后窗口内的输入位置已变化，未发送粘贴键",
                f"输入位置已变化，已复制，请回原位置按 {paste_key}",
            ),
        }
        if error_detail in contextual:
            log_msg, title = contextual[error_detail]
            log.info(log_msg)
            hint = (f"目标应用：{target_app}" if target_app else paste_hint) + dropped_suffix
            if polish_failed:
                hint = f"{hint} · 润色未生效（密钥/网络异常），已保留原文"
            self.show_post_output_floating(
                title,
                hint,
                stopped_text=f"剩余内容：{title}",
                use_info=True,
                refresh_ms=RESULT_TOAST_WITH_HINT_MS,
            )
            return
        if error_detail == "focus_changed":
            log.info("开口后的目标窗口已变化，未发送粘贴键")
            where = target_app or "原来的窗口"
            title = f"未输入到 {where} · 已复制，可按 {paste_key} 粘贴"
            hint = "文字已在剪贴板，切回目标位置粘贴即可" + dropped_suffix
            if polish_failed:
                hint = f"{hint} · 润色未生效（密钥/网络异常），已保留原文"
            self.show_post_output_floating(
                title,
                hint,
                stopped_text=f"剩余内容：{title} · {hint}",
                use_info=True,
                refresh_ms=RESULT_TOAST_WITH_HINT_MS,
            )
            return
        log.info("已复制到剪贴板（粘贴未确认成功）")
        idle_title = "已复制" if self.continuous_session_active() else "已复制到剪贴板"
        hint = paste_hint + dropped_suffix
        if polish_failed:
            hint = f"{hint} · 润色未生效（密钥/网络异常），已保留原文"
        self.show_post_output_floating(
            idle_title,
            hint,
            stopped_text=f"剩余内容：已复制 · {hint}",
            use_info=polish_failed or bool(dropped),
            refresh_ms=RESULT_TOAST_WITH_HINT_MS,
        )

    def _reset_paster_fallback(self) -> None:
        reset = getattr(self.paster, "reset_fallback_accumulator", None)
        if callable(reset):
            reset()

    def _show_capture_lost_feedback(self, target_hint: str) -> None:
        """Buffered speech reached the target after the microphone dropped mid-capture."""
        notice = self.capture_lost_notice or CAPTURE_LOST_NOTICE
        self.capture_lost_notice = ""
        title = f"已发送前半段 · {notice}"
        log.warning("麦克风中途断开，已发送断开前的内容")
        if self.continuous_user_stopped() or self.continuous_session_active():
            self.floating.show_info(title, target_hint)
            self.schedule_later(RESULT_TOAST_WITH_HINT_MS, self.refresh_continuous_ui)
            return
        self.floating.show_warning(title, target_hint)

    def _show_sent_feedback(
        self,
        *,
        target_app: str,
        polish_failed: bool,
        degraded_from_polish: bool,
        dropped: int,
        dropped_suffix: str,
    ) -> None:
        if polish_failed:
            success_msg = "润色未生效（密钥/网络异常），已发送原文"
        else:
            success_msg = "已发送（原文）" if degraded_from_polish else "已发送"
        target_hint = f"发送到 {target_app}" if target_app else "请确认目标应用已接收"
        if dropped:
            target_hint = f"{target_hint}{dropped_suffix}"
        log.info("已向目标窗口发送粘贴快捷键%s", f": {target_app}" if target_app else "")
        self._reset_paster_fallback()
        if self.capture_lost_notice:
            self._show_capture_lost_feedback(target_hint)
            return
        needs_attention = bool(polish_failed or dropped)
        self.show_post_output_floating(
            success_msg,
            target_hint,
            stopped_text=f"剩余内容：{success_msg} · {target_hint}",
            use_info=degraded_from_polish or needs_attention,
            refresh_ms=RESULT_TOAST_WITH_HINT_MS if needs_attention else RESULT_TOAST_MS,
        )

    def _route_paste_status_feedback(
        self,
        status: str,
        *,
        record: SegmentRecord | None,
        target_app: str,
        error_detail: str,
        polish_failed: bool,
        degraded_from_polish: bool,
        dropped: int,
        dropped_suffix: str,
    ) -> None:
        paste_key = "Ctrl+V"
        paste_hint = f"可按 {paste_key} 粘贴"
        if status in {"sent", "pasted"}:
            self._show_sent_feedback(
                target_app=target_app,
                polish_failed=polish_failed,
                degraded_from_polish=degraded_from_polish,
                dropped=dropped,
                dropped_suffix=dropped_suffix,
            )
            return
        if status == "recorded":
            self._show_recorded_paste_feedback(
                record=record,
                error_detail=error_detail,
                polish_failed=polish_failed,
                degraded_from_polish=degraded_from_polish,
                dropped=dropped,
                dropped_suffix=dropped_suffix,
            )
            return
        if status == "unverified":
            self._show_unverified_paste_feedback(
                error_detail=error_detail,
                target_app=target_app,
                paste_hint=paste_hint,
                polish_failed=polish_failed,
                dropped=dropped,
                dropped_suffix=dropped_suffix,
            )
            return
        if status == "skipped":
            self._show_skipped_paste_feedback(
                record=record,
                error_detail=error_detail,
                target_app=target_app,
                polish_failed=polish_failed,
                dropped_suffix=dropped_suffix,
                paste_hint=paste_hint,
            )
            return
        if status in {"clipboard", "copied"}:
            self._show_clipboard_paste_feedback(
                error_detail=error_detail,
                target_app=target_app,
                paste_key=paste_key,
                paste_hint=paste_hint,
                polish_failed=polish_failed,
                dropped=dropped,
                dropped_suffix=dropped_suffix,
            )
            return
        log.error("输出失败: %s", error_detail)
        self.floating.show_error(self._paste_error_message(record))

    def handle_paste_result(
        self,
        result: PasteResult | str,
        *,
        degraded_from_polish: bool = False,
        record: SegmentRecord | None = None,
        token: int | None = None,
    ) -> None:
        if self.exit_started():
            return
        if token is not None and token <= self.invalidated_token:
            log.warning(
                "忽略已超时失效的粘贴回调 (token=%s, invalidated=%s)",
                token,
                self.invalidated_token,
            )
            return
        if token is not None and token < self.token:
            target_app = result.target_app if isinstance(result, PasteResult) else ""
            self.enqueue_history_record(record, target_app=target_app)
            self.maybe_finish_dictation_speakers()
            return
        polish_failed = bool(self.polish_error_failed and degraded_from_polish)
        self.polish_error_failed = False
        self.busy = False
        self.stage = OutputStage.IDLE
        sent_text = self.stage_text
        self.stage_text = ""
        self.stage_record = None
        status, target_app, error_detail = self._unpack_paste_result(result)
        is_meeting_row = record is not None and record.trigger_mode == TRIGGER_MODE_MEETING
        if not is_meeting_row and sent_text.strip() and status in {"skipped", "error", "failed"}:
            self.last_output_text = sent_text
            self.last_output_at = float(self.now_monotonic())
        dropped = self.dropped_slices
        self.dropped_slices = 0
        dropped_suffix = f" · 有 {dropped} 段未识别" if dropped else ""
        self._update_post_paste_tray_tooltip()
        feedback = dict(
            record=record,
            target_app=target_app,
            error_detail=error_detail,
            polish_failed=polish_failed,
            degraded_from_polish=degraded_from_polish,
            dropped=dropped,
            dropped_suffix=dropped_suffix,
        )
        if status == "recorded":
            self._settle_recorded_result(record, target_app, error_detail, feedback)
        else:
            self._route_paste_status_feedback(status, **feedback)
            self.enqueue_history_record(record, target_app=target_app)
        self.maybe_finish_dictation_speakers()
        self.schedule_later(SEGMENT_PUMP_DELAY_MS, self.pump_queue)

    # ── History write settlement & unsaved recovery ───────────────

    def _settle_recorded_result(
        self,
        record: SegmentRecord | None,
        target_app: str,
        error_detail: str,
        feedback: dict,
    ) -> None:
        """「仅保存到历史」 says 已保存 only once SQLite confirms the row."""
        is_meeting_row = record is not None and record.trigger_mode == TRIGGER_MODE_MEETING
        if record is None or is_meeting_row or error_detail == "history_disabled":
            self._route_paste_status_feedback("recorded", **feedback)
            self.enqueue_history_record(record, target_app=target_app)
            return
        if self.meeting_active():
            # Leftover dictation archived under a running meeting (notice shown at takeover).
            self.enqueue_history_record(record, target_app=target_app)
            return
        if error_detail == "history_unavailable":
            # The history DB failed to open: nothing was saved, keep the words for recovery.
            self._mark_unsaved(self.history_row(record, target_app))
            return
        ticket = self.enqueue_history_record(
            record, target_app=target_app, kind="recorded", feedback=feedback
        )
        if self._is_real_ticket(ticket) and ticket == 0:
            self._mark_unsaved(self.history_row(record, target_app))
            return
        if self._is_real_ticket(ticket) and ticket in self.tracked_tickets:
            return  # green feedback waits for on_history_ticket_settled(ok=True)
        self._route_paste_status_feedback("recorded", **feedback)

    @staticmethod
    def _is_real_ticket(ticket: Any) -> bool:
        return isinstance(ticket, int) and not isinstance(ticket, bool)

    @staticmethod
    def history_row(record: SegmentRecord, target_app: str = "") -> SegmentRecord:
        return SegmentRecord(
            session_id=record.session_id,
            seq=record.seq,
            created_at=record.created_at,
            raw_text=record.raw_text,
            polished_text=record.polished_text,
            source=record.source,
            duration_ms=record.duration_ms,
            target_app=target_app or record.target_app or "",
            trigger_mode=record.trigger_mode,
            model=record.model,
            speaker_id=int(record.speaker_id),
            speaker_route=record.speaker_route or "",
            captured_at=int(record.captured_at or 0),
        )

    def enqueue_history_record(
        self,
        record: SegmentRecord | None,
        *,
        target_app: str = "",
        kind: str = "",
        feedback: dict | None = None,
        deduplicate: bool = False,
    ) -> Any:
        """Queue ``record`` for the history DB; returns the writer ticket (None if skipped)."""
        if record is None:
            return None
        # Meeting transcripts are the meeting itself, not dictation history:
        # they are kept even when the user has switched history off.
        is_meeting_row = record.trigger_mode == TRIGGER_MODE_MEETING
        if not is_meeting_row and not self.config.get("history.enabled", True):
            return None
        if not (record.raw_text.strip() or record.polished_text.strip()):
            return None
        row = self.history_row(record, target_app)
        if deduplicate:
            ticket = self.history.enqueue(row, deduplicate_session_seq=True)
        else:
            ticket = self.history.enqueue(row)
        track_kind = "meeting" if is_meeting_row else kind
        if track_kind:
            self._track_ticket(ticket, row, track_kind, feedback)
        return ticket

    def _track_ticket(
        self, ticket: Any, row: SegmentRecord, kind: str, feedback: dict | None
    ) -> None:
        if not self._is_real_ticket(ticket):
            return
        if ticket == 0:
            if kind == "meeting":
                self.record_unsaved_meeting_segment(row)
            return
        if not self.ticket_settlement_enabled:
            return
        self.tracked_tickets[int(ticket)] = (row, kind, feedback)
        while len(self.tracked_tickets) > MAX_TRACKED_HISTORY_TICKETS:
            self.tracked_tickets.pop(next(iter(self.tracked_tickets)))

    def on_history_ticket_settled(self, ticket: int, ok: bool, error_msg: str = "") -> None:
        """GUI-thread slot: a queued history insert committed (``ok``) or failed."""
        entry = self.tracked_tickets.pop(int(ticket), None)
        if entry is None:
            return
        row, kind, feedback = entry
        if kind == "meeting":
            if not ok:
                log.warning("会议片段写入历史失败: %s", error_msg)
                self.record_unsaved_meeting_segment(row)
            return
        if kind == "retry":
            self._settle_retry(int(ticket), row, ok)
            return
        if ok:
            if feedback is not None and not self.unsaved_records:
                self._route_paste_status_feedback("recorded", **feedback)
            return
        log.warning("「仅保存到历史」写入失败: %s", error_msg)
        self._mark_unsaved(row)

    def _mark_unsaved(self, row: SegmentRecord) -> None:
        key = (row.session_id, row.seq)
        if all((r.session_id, r.seq) != key for r in self.unsaved_records):
            self.unsaved_records.append(row)
        self.show_unsaved_history_error()

    def show_unsaved_history_error(self) -> None:
        count = len(self.unsaved_records)
        if not count:
            return
        title = f"保存历史失败（{count} 段未保存）"
        subtitle = "可点「复制全部」或「重试保存」" if count > 1 else "可点「复制原文」或「重试保存」"
        show = getattr(self.floating, "show_unsaved_history_error", None)
        if not callable(show):
            self.floating.show_error(f"{title}\n{subtitle}")
            return
        show(title, subtitle, count, copy_label="复制全部" if count > 1 else "复制原文")

    def unsaved_text(self) -> str:
        """All unsaved segments, sessions in first-seen order and ``seq`` order within each."""
        order: dict[str, int] = {}
        for row in self.unsaved_records:
            order.setdefault(row.session_id, len(order))
        rows = sorted(self.unsaved_records, key=lambda r: (order[r.session_id], r.seq))
        parts = [r.polished_text.strip() or r.raw_text.strip() for r in rows]
        return "\n".join([p for p in parts if p])

    def copy_unsaved_records(self) -> bool:
        text = self.unsaved_text()
        if not text:
            return False
        self.last_output_text = text
        self.last_output_at = float(self.now_monotonic())
        if not self.copy_clipboard(text):
            self.floating.show_error("复制失败：无法写入剪贴板")
            return False
        count = len(self.unsaved_records)
        self.floating.show_info(f"已复制 {count} 段原文", "可按 Ctrl+V 粘贴；仍可点「重试保存」")
        return True

    def retry_unsaved_records(self) -> int:
        """Idempotently re-queue every unsaved segment (``WHERE NOT EXISTS (session_id, seq)``)."""
        rows = list(self.unsaved_records)
        if not rows:
            return 0
        self.unsaved_records = []
        self.retry_saved_count = 0
        queued = 0
        for row in rows:
            ticket = self.enqueue_history_record(row, target_app=row.target_app, kind="retry", deduplicate=True)
            if self._is_real_ticket(ticket) and ticket in self.tracked_tickets:
                self.retry_tickets.add(int(ticket))
                queued += 1
            elif self._is_real_ticket(ticket) and ticket == 0:
                self.unsaved_records.append(row)
        if self.unsaved_records:
            self.show_unsaved_history_error()
        elif not queued:
            self._finish_retry(len(rows))
        return queued

    def _settle_retry(self, ticket: int, row: SegmentRecord, ok: bool) -> None:
        self.retry_tickets.discard(ticket)
        if ok:
            self.retry_saved_count += 1
        else:
            self.unsaved_records.append(row)
        if self.retry_tickets:
            return
        if self.unsaved_records:
            self.show_unsaved_history_error()
            return
        self._finish_retry(self.retry_saved_count)

    def _finish_retry(self, saved: int) -> None:
        clear = getattr(self.floating, "clear_unsaved_history_error", None)
        if callable(clear):
            clear()
        self.show_post_output_floating(
            "已保存到历史",
            f"已补存 {saved} 段",
            stopped_text="剩余内容：已保存到历史",
            use_info=False,
            refresh_ms=RESULT_TOAST_MS,
        )

    def on_history_write_failed(self, message: str) -> None:
        """Rate-limited tray warning so sustained DB write failures stay visible without spamming."""
        now = float(self.now_monotonic())
        if (
            self.last_history_failure_warn_at is not None
            and (now - self.last_history_failure_warn_at) < HISTORY_FAILURE_WARN_COOLDOWN_S
        ):
            return
        self.history_failure_notified = True
        self.last_history_failure_warn_at = now
        self.tray.showMessage(
            "SayInk", message, QSystemTrayIcon.MessageIcon.Warning, 8000
        )

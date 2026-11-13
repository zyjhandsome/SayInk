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
)
from sayink.history_store import SegmentRecord
from sayink.polish_fidelity import polish_rejection_reason
from sayink.runtime_status import OutputStage
from sayink.speaker_session import SpeakerSession
from sayink.speech_recognizer import normalize_asr_output
from sayink.text_paster import PasteResult
from sayink.text_polisher import (
    LLM_MODE_CORRECT,
    LLM_MODE_POLISH,
    normalize_llm_mode,
    polish_settings_complete,
    prompt_config_key,
)

log = logging.getLogger("SayInk")

OUTPUT_WATCHDOG_MS = 45_000  # 流式润色可读更久；到点仍改出原文，避免后面的句子一直排队
# A previous output older than this is no longer context for the next one.
RECENT_OUTPUT_CONTEXT_S = 120.0

# UI pacing (ms). Queued slices start on the next event-loop turn so a burst
# of VAD cuts keeps spoken order; result toasts stay readable (a hint with a
# second line gets the longer one) before the bar returns to its live state.
SEGMENT_PUMP_DELAY_MS = 0
RESULT_TOAST_MS = 1700
RESULT_TOAST_WITH_HINT_MS = 2200


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
        schedule_later: Callable[[int, Callable[[], None]], None] | None = None,
        pump_queue: Callable[[], None] | None = None,
        schedule_queue_pump: Callable[[], None] | None = None,
        finish_empty_recognition: Callable[[], None] | None = None,
        refresh_continuous_ui: Callable[[], None] | None = None,
        maybe_finish_dictation_speakers: Callable[[], None] | None = None,
        stop_continuous_user_session: Callable[[], None] | None = None,
        ask_history_output_conflict: Callable[..., str] | None = None,
        glossary_for_pending_segment: Callable[[], list] | None = None,
        continuous_session_active: Callable[[], bool] | None = None,
        continuous_user_stopped: Callable[[], bool] | None = None,
        has_queued_segments: Callable[[], bool] | None = None,
        meeting_active: Callable[[], bool] | None = None,
        exit_started: Callable[[], bool] | None = None,
        hold_target: Callable[[], tuple[int, str]] | None = None,
        clear_transcribing: Callable[[], None] | None = None,
        friendly_error: Callable[[str], str] | None = None,
        output_text_dispatch: Callable[..., None] | None = None,
        handle_paste_result_dispatch: Callable[..., None] | None = None,
        copy_to_clipboard: Callable[[str], None] | None = None,
        now_monotonic: Callable[[], float] | None = None,
        now_wall: Callable[[], float] | None = None,
        platform_name: Callable[[], str] | None = None,
        watchdog_ms: Callable[[], int] | None = None,
        recent_context_window_s: Callable[[], float] | None = None,
        polish_reject_fn: Callable[[str, str], str | None] | None = None,
        correction_reject_fn: Callable[[str, str, list], str | None] | None = None,
    ) -> None:
        self.config = config
        self.polisher = polisher
        self.paster = paster
        self.history = history
        self.floating = floating
        self.tray = tray
        self.recorder = recorder

        self.schedule_later = schedule_later or (lambda _ms, _fn: None)
        self.pump_queue = pump_queue or (lambda: None)
        self.schedule_queue_pump = schedule_queue_pump or (
            lambda: self.schedule_later(SEGMENT_PUMP_DELAY_MS, self.pump_queue)
        )
        self.finish_empty_recognition = finish_empty_recognition or (lambda: None)
        self.refresh_continuous_ui = refresh_continuous_ui or (lambda: None)
        self.maybe_finish_dictation_speakers = maybe_finish_dictation_speakers or (lambda: None)
        self.stop_continuous_user_session = stop_continuous_user_session or (lambda: None)
        self.ask_history_output_conflict = ask_history_output_conflict or (lambda **_kw: "cancel")
        self.glossary_for_pending_segment = glossary_for_pending_segment or list
        self.continuous_session_active = continuous_session_active or (lambda: False)
        self.continuous_user_stopped = continuous_user_stopped or (lambda: False)
        self.has_queued_segments = has_queued_segments or (lambda: False)
        self.meeting_active = meeting_active or (lambda: False)
        self.exit_started = exit_started or (lambda: False)
        self.hold_target = hold_target or (lambda: (0, ""))
        self.clear_transcribing = clear_transcribing or (lambda: None)
        self.friendly_error = friendly_error or (lambda msg: msg)
        self.output_text_dispatch = output_text_dispatch
        self.handle_paste_result_dispatch = handle_paste_result_dispatch
        self.copy_to_clipboard = copy_to_clipboard or pyperclip.copy
        self.now_monotonic = now_monotonic or time.monotonic
        self.now_wall = now_wall or time.time
        self.platform_name = platform_name or (lambda: sys.platform)
        self.watchdog_ms = watchdog_ms or (lambda: OUTPUT_WATCHDOG_MS)
        self.recent_context_window_s = recent_context_window_s or (lambda: RECENT_OUTPUT_CONTEXT_S)
        self.polish_reject_fn = polish_reject_fn or polish_rejection_reason
        self.correction_reject_fn = correction_reject_fn or correction_rejection_reason

        # Output stays serial. One next segment may be decoded into the queue
        # while the pending history record and polisher own the current one.
        self.busy: bool = False
        self.token: int = 0
        self.invalidated_token: int = 0
        # Raw text of the segment currently being output. A new hold recording
        # may start meanwhile and reset _current_transcription.
        self.raw_text: str = ""
        # "polish" while the polisher owns the sentence, "paste" once it was
        # handed to the paster; the watchdog decides from this what to salvage.
        self.stage: OutputStage = OutputStage.IDLE
        self.stage_text: str = ""
        # The history row of the sentence being pasted, so the watchdog can
        # still save it when the paster's callback never arrives.
        self.stage_record: SegmentRecord | None = None
        # Pending row owned by polish/paste. Esc on a newer hold clears
        # `pending_record` and must not drop this sentence or its window.
        self.output_pending_record: _PendingHistoryRecord | None = None
        self.pending_record: _PendingHistoryRecord | None = None
        # Which LLM mode owns the in-flight sentence and the glossary it was
        # sent with, so the right acceptance guard runs when the reply lands.
        self.llm_mode: str = LLM_MODE_POLISH
        self.glossary: list = []
        # Tail of the previous output and when it went out; correction mode
        # shows it to the model as read-only context for a split sentence.
        self.last_output_text: str = ""
        self.last_output_at: float = 0.0
        self.dropped_slices: int = 0
        self.keep_unsaved_continuous_text: bool = False
        self.history_failure_notified: bool = False
        self.polish_error_failed: bool = False

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

    def utterance_history_only(self, record: Any) -> bool:
        """History-only for this utterance, not for a setting that changed later."""
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

    def recent_output_context(self) -> str:
        """The previous output, if it went out recently enough to be the same
        conversation; an old sentence from another window would only mislead."""
        if not self.last_output_text:
            return ""
        if self.now_monotonic() - self.last_output_at > self.recent_context_window_s():
            return ""
        return self.last_output_text

    def mark_busy(self) -> None:
        self.busy = True
        self.token += 1
        token = self.token
        self.schedule_later(self.watchdog_ms(), lambda: self.release_stuck_output(token))

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
        log.error("输出流程超时未回调，释放队列以免后续语音卡住")
        self.busy = False
        self.invalidated_token = self.token
        self.token += 1
        self.stage = OutputStage.IDLE
        # The shortcut may still be waiting out its short delay. Sending it
        # after telling the user to paste by hand inserts the sentence twice.
        self.paster.cancel()
        record = self.stage_record
        self.stage_record = None
        if self.stage_text.strip():
            # Ctrl+V may or may not have gone out; leave the words on the
            # clipboard rather than risk pasting them twice.
            try:
                self.copy_to_clipboard(self.stage_text)
            except Exception as exc:
                log.warning("复制超时句子到剪贴板失败: %s", exc)
            else:
                self.floating.show_info("输出超时 · 已复制", "可按 Ctrl+V 粘贴")
        if record is not None:
            # The paster's callback (which normally saves the row) is gone with
            # the cancel; the words are still worth keeping in history.
            self.enqueue_history_record(record, target_app="")
        self.pump_queue()

    def deliver_recognized_text(self, text: str) -> None:
        self.claim_pending_record_for_output()
        self.mark_busy()
        self.polish_error_failed = False
        self.raw_text = text
        self.stage = OutputStage.POLISH
        self.stage_text = ""
        llm_enabled = self.config.get("llm.enabled", False)
        pending = self.record_for_output()
        if pending is not None and pending.trigger_mode == TRIGGER_MODE_MEETING:
            # Meeting rows go to the minutes model later, in one pass; a
            # per-sentence polish would only add latency and API cost.
            llm_enabled = bool(self.config.get("meeting.polish_segments", False)) and llm_enabled
        api_url = self.config.get("llm.api_url", "")
        api_key = self.config.get("llm.api_key", "")
        model_name = self.config.get("llm.model_name", "")
        mode = normalize_llm_mode(self.config.get("llm.mode", LLM_MODE_POLISH))
        prompt = self.config.get(prompt_config_key(mode), "")
        self.llm_mode = mode
        self.glossary = list(self.glossary_for_pending_segment() or [])

        if llm_enabled and polish_settings_complete(api_url, api_key, model_name):
            action = "纠错" if mode == LLM_MODE_CORRECT else "润色"
            self.tray.set_activity_tooltip("correcting" if mode == LLM_MODE_CORRECT else "polishing")
            if self.continuous_user_stopped():
                self.floating.show_continuous_stopped(f"正在处理剩余内容 · 正在{action}")
            else:
                self.floating.show_polishing(text, action=action)
            target_app = pending.target_app if pending is not None else ""
            self.polisher.polish(
                text,
                api_url,
                api_key,
                model_name,
                prompt,
                mode=mode,
                glossary=self.glossary,
                context=self.recent_output_context(),
                target_app=target_app,
                proxy_mode=self.config.get("llm.proxy_mode", "system"),
                proxy_bypass_suffixes=str(self.config.get("llm.proxy_bypass_suffixes", "") or ""),
            )
        elif llm_enabled:
            log.warning("文字润色已开启但配置不完整，直接输出原文")
            self._dispatch_output(text, degraded_from_polish=True)
        else:
            self._dispatch_output(text)
        if self.busy:
            self.schedule_queue_pump()

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

    def output_text(self, text: str, *, degraded_from_polish: bool = False) -> None:
        if self.exit_started():
            return
        self.clear_transcribing()
        text = normalize_asr_output(text)
        if not text.strip():
            # Nothing to paste or save: release the output slot completely so
            # the next sentence does not inherit this one's stage or record.
            self.busy = False
            self.stage = OutputStage.IDLE
            self.stage_text = ""
            self.stage_record = None
            if self.output_pending_record is not None:
                self.output_pending_record = None
            else:
                self.pending_record = None
            self.finish_empty_recognition()
            return

        # Freeze drops the pending row, and that row is the only place a
        # continuous utterance keeps the window from when speech started.
        paste_hwnd, paste_app = self.paste_target_for_output()
        record = self.freeze_pending_history_record(text, degraded_from_polish)
        is_meeting_row = record is not None and record.trigger_mode == TRIGGER_MODE_MEETING
        # H-01: Leftover dictation slices draining after a meeting has started
        # must NEVER fire Ctrl+V into the foreground window mid-meeting.
        forced_meeting_history = self.meeting_active() and not is_meeting_row
        history_only = self.utterance_history_only(record) or forced_meeting_history
        if not is_meeting_row:
            # Context for the next correction: only dictation that is about to
            # go out, never a meeting row the user is not typing anywhere.
            self.last_output_text = text
            self.last_output_at = self.now_monotonic()
        preserve_tail = self.keep_unsaved_continuous_text and not getattr(self.recorder, "is_continuous", False)
        if (
            not is_meeting_row
            and not self.meeting_active()
            and history_only
            and not bool(self.config.get("history.enabled", True))
            and (getattr(self.recorder, "is_continuous", False) is True or preserve_tail)
        ):
            if preserve_tail:
                copied = self.copy_clipboard(text)
                self._dispatch_paste_result(
                    PasteResult(
                        "clipboard" if copied else "failed",
                        detail="history_conflict" if copied else "clipboard_failed",
                    ),
                    degraded_from_polish=degraded_from_polish,
                    record=record,
                )
                self.release_unsaved_hold()
                return
            # The dialog runs a nested event loop. Drop the polish watchdog
            # first, or a slow answer re-enters this sentence and pastes it twice.
            self.token += 1
            self.stage = OutputStage.PASTE
            choice = self.ask_history_output_conflict(listening=True)
            if choice == "history":
                self.config.set("history.enabled", True)
            elif choice == "paste":
                self._set_output_mode_paste()
            else:
                self.keep_unsaved_continuous_text = True
                copied = self.copy_clipboard(text)
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
                return
            # 「改为输入光标」/「开启历史」apply to this sentence. Re-read after
            # the dialog; the captured-source rule still applies if the user
            # only turned history back on.
            forced_meeting_history = self.meeting_active() and not is_meeting_row
            history_only = self.utterance_history_only(record) or forced_meeting_history
        if is_meeting_row or history_only:
            # Meeting / playback capture: the words go to history, never to
            # whatever window happens to be in front.
            saved = is_meeting_row or bool(self.config.get("history.enabled", True))
            if forced_meeting_history and not saved:
                # Never silently lose draining dictation words when a meeting
                # started while history was off: copy to clipboard safely.
                self.copy_clipboard(text)
            log.info("只记录不粘贴：%d 字%s", len(text), "" if saved else "（历史已关闭，未保存）")
            self._dispatch_paste_result(
                PasteResult("recorded", detail="" if saved else "history_disabled"),
                degraded_from_polish=degraded_from_polish,
                record=record,
            )
            return
        self.stage = OutputStage.PASTE
        self.stage_text = text
        # Kept outside the paste callback so the watchdog can still save the
        # row when the paster never answers. Always renew the watchdog when
        # entering PASTE so slow polish time never steals PASTE's budget.
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

        self.paster.paste_async(
            text,
            _on_paste_done,
            target_hwnd=paste_hwnd,
            target_app=paste_app,
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
        self.stage_text = ""
        self.stage_record = None
        if isinstance(result, PasteResult):
            status = result.status
            target_app = result.target_app
            error_detail = result.detail
        else:
            # Compatibility for tests and third-party integrations using the old callback.
            status = result.split(":", 1)[0]
            target_app = ""
            error_detail = result.partition(":")[2]
        paste_key = "Cmd+V" if self.platform_name() == "darwin" else "Ctrl+V"
        paste_hint = f"可按 {paste_key} 粘贴"
        polish_fail_notice = "润色未生效（密钥/网络异常），已发送原文"
        if polish_failed:
            success_msg = polish_fail_notice
        else:
            success_msg = "已发送（原文）" if degraded_from_polish else "已发送"
        target_hint = f"发送到 {target_app}" if target_app else "请确认目标应用已接收"
        dropped = self.dropped_slices
        self.dropped_slices = 0
        if dropped:
            # A slice of the hold failed to recognise and was left out; the
            # user would otherwise only notice the gap in the pasted text.
            target_hint = f"{target_hint} · 有 {dropped} 段未识别"
        if getattr(self.recorder, "is_recording", False):
            # The next hold is already being recorded.
            self.tray.set_activity_tooltip("recording")
        elif self.meeting_active():
            # Preserve active meeting tooltip while draining leftover segments.
            pass
        else:
            self.tray.set_activity_tooltip(
                "listening" if self.continuous_session_active() else None
            )

        if status in {"sent", "pasted"}:
            log.info("已向目标窗口发送粘贴快捷键%s", f": {target_app}" if target_app else "")
            self.show_post_output_floating(
                success_msg,
                target_hint,
                stopped_text=f"剩余内容：{success_msg} · {target_hint}",
                use_info=degraded_from_polish or polish_failed,
                refresh_ms=RESULT_TOAST_WITH_HINT_MS if polish_failed else RESULT_TOAST_MS,
            )
        elif status == "recorded":
            saved = error_detail != "history_disabled"
            meeting_row = getattr(record, "trigger_mode", None) == TRIGGER_MODE_MEETING
            if self.meeting_active() and not meeting_row:
                # Leftover dictation segment archived silently without disturbing meeting bar.
                log.info("会议进行中：遗留口述分片已归档至历史（不发送粘贴键）")
            else:
                done = "已记录" if meeting_row else "已保存到历史"
                if polish_failed:
                    suffix = " · 润色未生效（密钥/网络异常），已保留原文"
                elif degraded_from_polish:
                    suffix = "（原文）"
                else:
                    suffix = ""
                title = (done if saved else "未保存 · 历史已关闭") + suffix
                if not saved:
                    hint = "当前为「仅保存到历史」，但历史已关闭；请在设置中打开历史或改为输入当前光标"
                else:
                    hint = "" if meeting_row else "未输入其他应用"
                self.show_post_output_floating(
                    title,
                    hint,
                    stopped_text=f"剩余内容：{title}",
                    use_info=not (saved and not degraded_from_polish and not polish_failed),
                    refresh_ms=RESULT_TOAST_MS,
                )
        elif status == "unverified":
            # Ctrl+V already went out, then the foreground changed. Saying
            # 「已复制」 here invites a second manual paste of the same words.
            log.info("已发送粘贴快捷键，但焦点随后切换，无法确认是否已插入")
            unverified_hint = (
                f"焦点已切换，若 {target_app} 未出字{paste_hint}"
                if target_app
                else f"焦点已切换，若未出字{paste_hint}"
            )
            if polish_failed:
                unverified_hint = f"{unverified_hint} · 润色未生效（密钥/网络异常）"
            self.show_post_output_floating(
                "已发送 · 未确认",
                unverified_hint,
                stopped_text=f"剩余内容：已发送 · 未确认 · {unverified_hint}",
                use_info=True,
                refresh_ms=RESULT_TOAST_WITH_HINT_MS,
            )
        elif status == "clipboard" and error_detail == "elevated":
            log.info("目标窗口为管理员权限，已复制到剪贴板")
            title = f"目标为管理员窗口，已复制，请按 {paste_key}"
            hint = f"目标应用：{target_app}" if target_app else paste_hint
            if polish_failed:
                hint = f"{hint} · 润色未生效（密钥/网络异常），已保留原文"
            self.show_post_output_floating(
                title,
                hint,
                stopped_text=f"剩余内容：{title}",
                use_info=True,
                refresh_ms=RESULT_TOAST_WITH_HINT_MS,
            )
        elif status == "clipboard" and error_detail == "ime_active":
            log.info("检测到输入法候选窗活跃，已复制到剪贴板")
            title = f"检测到正在拼音选词，已复制，请按 {paste_key}"
            hint = f"目标应用：{target_app}" if target_app else paste_hint
            if polish_failed:
                hint = f"{hint} · 润色未生效（密钥/网络异常），已保留原文"
            self.show_post_output_floating(
                title,
                hint,
                stopped_text=f"剩余内容：{title}",
                use_info=True,
                refresh_ms=RESULT_TOAST_WITH_HINT_MS,
            )
        elif status == "clipboard" and error_detail == "session_changed":
            log.info("桌面会话已切换或锁屏，已复制到剪贴板")
            title = f"桌面会话已切换，已复制，请按 {paste_key}"
            hint = f"目标应用：{target_app}" if target_app else paste_hint
            if polish_failed:
                hint = f"{hint} · 润色未生效（密钥/网络异常），已保留原文"
            self.show_post_output_floating(
                title,
                hint,
                stopped_text=f"剩余内容：{title}",
                use_info=True,
                refresh_ms=RESULT_TOAST_WITH_HINT_MS,
            )
        elif status == "clipboard" and error_detail == "focus_changed":
            log.info("开口后的目标窗口已变化，未发送粘贴键")
            title = f"未输入到 {target_app}" if target_app else "未输入到原来的窗口"
            hint = paste_hint
            if polish_failed:
                hint = f"{hint} · 润色未生效（密钥/网络异常），已保留原文"
            self.show_post_output_floating(
                title,
                hint,
                stopped_text=f"剩余内容：{title} · {hint}",
                use_info=True,
                refresh_ms=RESULT_TOAST_WITH_HINT_MS,
            )
        elif status == "clipboard":
            log.info("已复制到剪贴板（粘贴未确认成功）")
            idle_title = "已复制" if self.continuous_session_active() else "已复制到剪贴板"
            hint = paste_hint
            if polish_failed:
                hint = f"{hint} · 润色未生效（密钥/网络异常），已保留原文"
            self.show_post_output_floating(
                idle_title,
                hint,
                stopped_text=f"剩余内容：已复制 · {hint}",
                use_info=polish_failed,
                refresh_ms=RESULT_TOAST_WITH_HINT_MS,
            )
        else:
            log.error("输出失败: %s", error_detail)
            self.floating.show_error(self.friendly_error("输出失败"))

        self.enqueue_history_record(record, target_app=target_app)
        # Last words of a stopped continuous session are in: settle speakers.
        self.maybe_finish_dictation_speakers()
        self.schedule_later(SEGMENT_PUMP_DELAY_MS, self.pump_queue)

    def enqueue_history_record(
        self,
        record: SegmentRecord | None,
        *,
        target_app: str = "",
    ) -> None:
        if record is None:
            return
        # Meeting transcripts are the meeting itself, not dictation history:
        # they are kept even when the user has switched history off.
        is_meeting_row = record.trigger_mode == TRIGGER_MODE_MEETING
        if not is_meeting_row and not self.config.get("history.enabled", True):
            return
        if not (record.raw_text.strip() or record.polished_text.strip()):
            return
        self.history.enqueue(
            SegmentRecord(
                session_id=record.session_id,
                seq=record.seq,
                created_at=record.created_at,
                raw_text=record.raw_text,
                polished_text=record.polished_text,
                source=record.source,
                duration_ms=record.duration_ms,
                target_app=target_app or "",
                trigger_mode=record.trigger_mode,
                model=record.model,
                speaker_id=int(record.speaker_id),
                speaker_route=record.speaker_route or "",
                captured_at=int(record.captured_at or 0),
            )
        )

    def on_history_write_failed(self, message: str) -> None:
        """One tray warning per run; the log keeps every failure."""
        if self.history_failure_notified:
            return
        self.history_failure_notified = True
        self.tray.showMessage(
            "SayInk", message, QSystemTrayIcon.MessageIcon.Warning, 8000
        )

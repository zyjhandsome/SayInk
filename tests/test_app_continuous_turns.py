"""Shared speaker turns keep continuous dictation's output and session rules."""
from __future__ import annotations

from dataclasses import replace
from unittest.mock import patch

import numpy as np
import pytest

from sayink.config import OUTPUT_MODE_HISTORY
from sayink.meeting_speakers import MeetingSpeakerSession
from tests.helpers.app_harness import app_harness
from tests.test_app_history_wiring import _enqueued_records, _start_continuous_user_session
from tests.test_app_meeting_turns import batch, make_context


def start(h):
    app = h["app"]
    _start_continuous_user_session(app)
    h["recorder"].is_continuous = True
    h["recorder"].stop_continuous.side_effect = lambda: setattr(h["recorder"], "is_continuous", False)
    return app


def begin(app, stamp=10_000):
    context = make_context(app, stamp)
    app._begin_transcription(np.full(48000, 0.1, dtype=np.float32), context=context)
    return context


def acknowledge(h):
    h["paster"].paste_async.call_args.args[1]("pasted")


def test_dismissing_a_mid_session_history_conflict_keeps_the_words_and_stops(monkeypatch):
    copied = []
    monkeypatch.setattr("pyperclip.copy", copied.append)
    with app_harness({"audio.trigger_mode": "continuous", "history.enabled": True}) as h:
        app = start(h)
        h["recorder"].is_continuous = True
        app._config.set("history.enabled", False)
        app._config.set("output.mode", "history")
        monkeypatch.setattr(app, "_ask_history_output_conflict", lambda **_kwargs: "cancel", raising=False)
        with patch("sayink.app.QTimer.singleShot"):
            begin(app)
            app._on_speaker_turn_result(batch()[:1])
        h["paster"].paste_async.assert_not_called()
        assert copied == ["甲说方案可行"]
        h["recorder"].stop_continuous.assert_called()
        assert app._config.get("history.enabled") is False
        assert app._config.history_only_output()


def test_words_that_finish_after_the_conflict_cancel_are_copied_too(monkeypatch):
    copied = []
    monkeypatch.setattr("pyperclip.copy", copied.append)
    with app_harness({"audio.trigger_mode": "continuous", "history.enabled": True}) as h:
        app = start(h)
        marker = object()

        def stop():
            h["recorder"].is_continuous = False
            app._segment_queue.append(marker)

        h["recorder"].stop_continuous.side_effect = stop
        app._config.set("history.enabled", False)
        app._config.set("output.mode", "history")
        monkeypatch.setattr(app, "_ask_history_output_conflict", lambda **_kwargs: "cancel", raising=False)
        with patch("sayink.app.QTimer.singleShot"):
            begin(app)
            app._on_speaker_turn_result(batch()[:1])
        app._segment_queue.remove(marker)
        app._output_text("后面这句还在识别")
        assert copied == ["甲说方案可行", "后面这句还在识别"]
        h["paster"].paste_async.assert_not_called()


def test_history_conflict_cancel_does_not_claim_a_copy_that_failed(monkeypatch):
    def deny(_text):
        raise OSError("clipboard denied")

    monkeypatch.setattr("pyperclip.copy", deny)
    with app_harness({"audio.trigger_mode": "continuous", "history.enabled": True}) as h:
        app = start(h)
        app._config.set("history.enabled", False)
        app._config.set("output.mode", "history")
        monkeypatch.setattr(app, "_ask_history_output_conflict", lambda **_kwargs: "cancel", raising=False)
        with patch("sayink.app.QTimer.singleShot"):
            begin(app)
            app._on_speaker_turn_result(batch()[:1])
        h["floating"].show_success.assert_not_called()
        assert h["floating"].show_error.called


def test_mid_session_history_conflict_does_not_say_recording_has_not_started(monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    captured = {}

    def exec_(box):
        captured["text"] = box.text()
        captured["info"] = box.informativeText()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", exec_)
    with app_harness({"audio.trigger_mode": "continuous", "history.enabled": False}) as h:
        assert h["app"]._ask_history_output_conflict(listening=True) == "cancel"
    assert "不会开始录音" not in captured["text"]
    assert "复制" in captured["info"]


def test_continuous_segments_keep_the_process_captured_when_speech_started():
    with app_harness({"audio.trigger_mode": "continuous"}) as h:
        app = h["app"]
        targets = [(11, "alpha.exe"), (22, "beta.exe")]
        app._recorder.consume_segment_target.side_effect = lambda: targets.pop(0)
        first = app._capture_segment_context()
        second = app._capture_segment_context()
        assert (getattr(first, "target_hwnd", None), getattr(first, "target_app", None)) == (11, "alpha.exe")
        assert (getattr(second, "target_hwnd", None), getattr(second, "target_app", None)) == (22, "beta.exe")


def test_continuous_output_pastes_into_the_window_open_at_speech_start():
    """A previous hold must not steal the window captured for this utterance."""
    with app_harness({"audio.trigger_mode": "continuous", "llm.enabled": False}) as h:
        app = start(h)
        app._hold_target = (999, "stale.exe")
        app._recorder.consume_segment_target.return_value = (11, "alpha.exe")
        begin(app)
        with patch("sayink.app.QTimer.singleShot"):
            app._on_speaker_turn_result(batch()[:1])
        kwargs = h["paster"].paste_async.call_args.kwargs
        assert kwargs.get("target_hwnd") == 11
        assert kwargs.get("target_app") == "alpha.exe"


def test_continuous_without_an_external_window_does_not_reuse_the_last_hold():
    with app_harness({"audio.trigger_mode": "continuous", "llm.enabled": False}) as h:
        app = start(h)
        app._hold_target = (999, "stale.exe")
        app._recorder.consume_segment_target.return_value = (0, "")
        begin(app)
        with patch("sayink.app.QTimer.singleShot"):
            app._on_speaker_turn_result(batch()[:1])
        kwargs = h["paster"].paste_async.call_args.kwargs
        assert kwargs.get("target_hwnd") == 0
        assert kwargs.get("target_app") in (None, "")


@pytest.mark.parametrize("mode", ["continuous", "hotkey"])
def test_single_and_continuous_modes_share_retry_without_turn_analysis_on_spectral_fallback(mode):
    with app_harness({"audio.trigger_mode": mode}) as h:
        h["app"]._begin_transcription(np.full(16000, 0.1, dtype=np.float32))
        assert h["recognizer"].transcribe_final.call_args.kwargs == {"retry_empty": True}


def test_continuous_uses_shared_background_analyzer_and_does_not_extract_on_gui():
    with app_harness({"audio.trigger_mode": "continuous"}) as h:
        app = start(h)
        context = begin(app)
        assert isinstance(context.speakers, MeetingSpeakerSession)
        args = h["recognizer"].transcribe_final.call_args.kwargs
        assert args["retry_empty"] and args["turn_analyzer"] is not None
        assert app._pending_turn_context is context and app._pending_segment_count() == 1


def test_turns_wait_for_each_paste_and_keep_original_session_times_and_plain_words():
    with app_harness({"audio.trigger_mode": "continuous", "llm.enabled": False}) as h:
        app = start(h)
        context = begin(app)
        later = make_context(app, 20_000)
        app._enqueue_audio(np.full(16000, 0.1, dtype=np.float32), context=later)
        with patch("sayink.app.QTimer.singleShot"):
            app._on_speaker_turn_result(batch())
            assert later.seq == 3 and app._current_seq == 4
            app._pump_segment_queue()
            assert h["paster"].paste_async.call_count == 1  # still waiting for first paste
            for index in range(3):
                assert h["paster"].paste_async.call_args.args[0] == batch()[index].text
                acknowledge(h)
                if index < 2:
                    app._pump_segment_queue()
        records = _enqueued_records(h["history"])
        assert [r.seq for r in records] == [0, 1, 2]
        assert [r.speaker_id for r in records] == [1, 2, 1]
        assert [r.captured_at for r in records] == [10_000, 11_000, 12_000]
        assert [r.duration_ms for r in records] == [1000, 1000, 1000]
        assert all(r.session_id == context.session_id and r.trigger_mode == "continuous" for r in records)
        assert h["recognizer"].transcribe_final.call_count == 1  # no repeated ASR on ready turns
        assert app._pending_turn_audio is None


def test_stopping_during_analysis_drains_all_turns_before_reclustering():
    with app_harness({"audio.trigger_mode": "continuous"}) as h:
        app = start(h)
        begin(app)
        with patch("sayink.app.QTimer.singleShot"), patch.object(app, "_finish_dictation_speakers", wraps=app._finish_dictation_speakers) as finish:
            app._stop_continuous_user_session()
            assert app._continuous_user_stopped and app._pending_segment_count() == 1
            finish.assert_not_called()
            app._on_speaker_turn_result(batch())
            for index in range(3):
                finish.assert_not_called()
                acknowledge(h)
                if index < 2:
                    app._pump_segment_queue()
            finish.assert_called_once()
        assert len(_enqueued_records(h["history"])) == 3
        assert app._pending_segment_count() == 0


def test_old_analysis_can_finish_after_restart_without_renumbering_new_captures_or_teaching_new_matcher():
    with app_harness({"audio.trigger_mode": "continuous"}) as h:
        app = start(h)
        old = begin(app)
        with patch("sayink.app.QTimer.singleShot"):
            app._stop_continuous_user_session()
            start(h)
            new = make_context(app, 30_000)
            app._enqueue_audio(np.full(16000, 0.1, dtype=np.float32), context=new)
            app._on_speaker_turn_result(batch())
            assert old.session_id != new.session_id
            assert new.seq == 0 and app._current_seq == 1
            assert [q.context.session_id for q in app._segment_queue] == [old.session_id, old.session_id, new.session_id]
            for index in range(3):
                acknowledge(h)
                if index < 2:
                    app._pump_segment_queue()
        records = _enqueued_records(h["history"])
        assert [r.seq for r in records] == [0, 1, 2]
        assert all(r.session_id == old.session_id for r in records)
        assert old.speakers.speaker_count == 2 and new.speakers.speaker_count == 0


def test_empty_final_turn_releases_queue_and_finishes_stopped_session():
    with app_harness({"audio.trigger_mode": "continuous"}) as h:
        app = start(h)
        context = begin(app)
        turns = batch()
        turns[-1] = replace(turns[-1], text="")
        with patch("sayink.app.QTimer.singleShot"):
            app._stop_continuous_user_session()
            app._on_speaker_turn_result(turns)
            acknowledge(h)
            app._pump_segment_queue()
            acknowledge(h)
            app._pump_segment_queue()
        assert h["paster"].paste_async.call_count == 2
        assert len(_enqueued_records(h["history"])) == 2
        assert app._pending_segment_count() == 0
        assert app._reclustered_session_id == context.session_id


@pytest.mark.parametrize("history_enabled", [True, False])
def test_history_only_output_still_obeys_continuous_history_preference(history_enabled, monkeypatch):
    with app_harness({"audio.trigger_mode": "continuous", "output.mode": OUTPUT_MODE_HISTORY, "history.enabled": history_enabled}) as h:
        app = start(h)
        if not history_enabled:
            # The session was already open; refusing the conflict must not paste.
            monkeypatch.setattr(app, "_ask_history_output_conflict", lambda **_kwargs: "cancel")
        begin(app)
        with patch("sayink.app.QTimer.singleShot"):
            app._on_speaker_turn_result(batch())
            app._pump_segment_queue()
            app._pump_segment_queue()
        h["paster"].paste_async.assert_not_called()
        assert len(_enqueued_records(h["history"])) == (3 if history_enabled else 0)


def test_polishing_remains_serial_and_does_not_lose_ready_turns():
    overrides = {"audio.trigger_mode": "continuous", "llm.enabled": True,
                 "llm.api_url": "https://api.example.test/v1", "llm.api_key": "key", "llm.model_name": "test"}
    with app_harness(overrides) as h:
        app = start(h)
        begin(app)
        with patch("sayink.app.QTimer.singleShot"):
            app._on_speaker_turn_result(batch())
            for index, turn in enumerate(batch()):
                assert h["polisher"].polish.call_count == index + 1
                app._pump_segment_queue()
                assert h["polisher"].polish.call_count == index + 1
                app._on_polish_complete(turn.text)
                acknowledge(h)
                if index < 2:
                    app._pump_segment_queue()
        assert [r.raw_text for r in _enqueued_records(h["history"])] == [t.text for t in batch()]
        assert h["recognizer"].transcribe_final.call_count == 1


def test_history_conflict_dialog_does_not_let_the_watchdog_paste_twice():
    """The conflict dialog runs a nested event loop while the output watchdog
    is armed. It must not open a second dialog or paste the same sentence twice."""
    from PyQt6.QtCore import QEventLoop, QTimer

    import sayink.app as app_mod

    def spin(ms: int) -> None:
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    with app_harness({
        "audio.trigger_mode": "continuous",
        "output.mode": "history",
        "history.enabled": False,
    }) as h:
        app, rec, paster = h["app"], h["recorder"], h["paster"]
        rec.is_continuous = True
        rec.is_recording = True
        dialogs = []

        def answer_after_the_watchdog(**_kwargs):
            dialogs.append(1)
            spin(200 if len(dialogs) == 1 else 20)
            return "paste"

        app._ask_history_output_conflict = answer_after_the_watchdog
        app_mod.OUTPUT_WATCHDOG_MS = 40
        try:
            app._on_segment_ready(np.full(16000, 0.1, np.float32))
            app._on_final_result("请把周报发给我")
        finally:
            app_mod.OUTPUT_WATCHDOG_MS = 45_000

        assert len(dialogs) == 1
        assert paster.paste_async.call_count == 1
        assert paster.paste_async.call_args[0][0] == "请把周报发给我"


def test_turn_result_during_model_reload_releases_the_pipeline():
    """A model reload that lands while turn analysis is in flight must not
    leave `_is_transcribing` set, or every later segment waits forever."""
    with app_harness({"audio.trigger_mode": "continuous"}) as h:
        app = start(h)
        begin(app)
        assert app._is_transcribing
        h["recognizer"].is_ready = False
        h["recognizer"].is_loading = True
        app._on_speaker_turn_result(batch()[:1])
        assert app._is_transcribing is False
        assert app._segment_queue
        h["recognizer"].is_ready = True
        h["recognizer"].is_loading = False
        app._on_stt_ready()
        assert h["paster"].paste_async.called
        assert app._segment_queue == []

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
def test_history_only_output_still_obeys_continuous_history_preference(history_enabled):
    with app_harness({"audio.trigger_mode": "continuous", "output.mode": OUTPUT_MODE_HISTORY, "history.enabled": history_enabled}) as h:
        app = start(h)
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

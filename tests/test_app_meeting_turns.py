"""Background turn results preserve captured order and use ordinary history output."""
from __future__ import annotations

from unittest.mock import patch

import numpy as np

from sayink.meeting_session import MeetingState
from sayink.history_store import SegmentRecord
from sayink.speaker_turns import RecognizedTurn
from tests.helpers.app_harness import app_harness
from tests.test_app_meeting_wiring import _enqueued_records, _start


class VoiceModel:
    supports_turn_detection = True
    threshold = 0.55

    def __call__(self, audio):
        raise AssertionError("GUI must not re-extract a voice after background analysis")


def make_context(app, captured_at):
    context = app._capture_segment_context()
    context.embed = VoiceModel()
    context.captured_at = captured_at
    return context


def batch():
    return [RecognizedTurn(0, 16000, "甲说方案可行", np.array([1, 0], dtype=np.float32), True),
            RecognizedTurn(16000, 32000, "乙说预算不足", np.array([0, 1], dtype=np.float32), True),
            RecognizedTurn(32000, 48000, "甲继续说明", np.array([1, 0], dtype=np.float32), True)]


def test_analysis_is_requested_before_any_gui_voice_extraction():
    with app_harness() as h:
        app = h["app"]
        _start(app, h)
        context = make_context(app, 10_000)
        app._begin_transcription(np.full(48000, 0.1, dtype=np.float32), route="mic", context=context)
        assert app._is_transcribing
        assert app._pending_turn_context is context
        assert h["recognizer"].transcribe_final.call_args.kwargs["turn_analyzer"] is not None
        assert app._pending_segment_count() == 1


def test_a_b_a_results_keep_timestamps_and_move_only_unsaved_later_sequences():
    with app_harness({"history.enabled": False}) as h:
        app = h["app"]
        meeting_id = _start(app, h, input_source="mixed")
        h["recorder"].input_source = "mixed"
        first = make_context(app, 10_000)
        app._begin_transcription(np.full(48000, 0.1, dtype=np.float32), route="mic", context=first)
        later = make_context(app, 20_000)
        app._enqueue_audio(np.full(16000, 0.1, dtype=np.float32), "system", context=later)
        with patch("sayink.app.QTimer.singleShot"):
            app._on_speaker_turn_result(batch())
            assert later.seq == 3 and app._current_seq == 4
            assert [q.context.seq for q in app._segment_queue] == [1, 2, 3]
            app._pump_segment_queue()
            app._pump_segment_queue()
        records = _enqueued_records(h["history"])
        assert [r.seq for r in records] == [0, 1, 2]
        assert [r.captured_at for r in records] == [10_000, 11_000, 12_000]
        assert [r.duration_ms for r in records] == [1000, 1000, 1000]
        assert [r.speaker_id for r in records] == [1, 2, 1]
        assert all(r.session_id == meeting_id and r.trigger_mode == "meeting" for r in records)
        # Whole-chunk mic dominance cannot prove where each split turn came
        # from. Do not invent per-turn source provenance.
        assert [r.speaker_route for r in records] == ["", "", ""]
        assert h["recognizer"].transcribe_final.call_count == 1
        h["paster"].paste_async.assert_not_called()
        assert app._pending_turn_audio is None


def test_draining_waits_for_analysis_and_accepts_its_final_words():
    with app_harness() as h:
        app = h["app"]
        _start(app, h)
        context = make_context(app, 10_000)
        app._begin_transcription(np.full(48000, 0.1, dtype=np.float32), context=context)
        app._end_meeting()
        assert app.meeting.state is MeetingState.DRAINING
        with patch("sayink.app.QTimer.singleShot"):
            app._on_speaker_turn_result(batch())
            app._pump_segment_queue()
            app._pump_segment_queue()
        assert len(_enqueued_records(h["history"])) == 3
        assert app._pending_segment_count() == 0


def test_error_releases_batch_audio_and_context():
    with app_harness() as h:
        app = h["app"]
        _start(app, h)
        app._begin_transcription(np.full(48000, 0.1, dtype=np.float32), context=make_context(app, 10_000))
        app._on_recognizer_error("识别失败")
        assert app._pending_turn_audio is None and app._pending_turn_context is None
        assert not app._is_transcribing


def test_splitting_later_audio_never_rewrites_an_existing_manual_assignment():
    with app_harness(real_history=True) as h:
        app = h["app"]
        meeting_id = _start(app, h)
        vector = np.array([1, 0], dtype=np.float32)
        app.meeting.speakers.assign(vector, sample_key=0)
        h["history"].enqueue(SegmentRecord(meeting_id, 0, 9000, raw_text="已人工确认的一段",
                                           trigger_mode="meeting", speaker_id=1, captured_at=9000))
        assert h["history"].flush()
        assert app.meeting.correct_segment_speaker(meeting_id, 0, None)
        saved = app.meeting.store.get_segments(meeting_id)[0]
        app._current_seq = 1
        app._begin_transcription(np.full(48000, 0.1, dtype=np.float32), context=make_context(app, 10_000))
        with patch("sayink.app.QTimer.singleShot"):
            app._on_speaker_turn_result(batch())
            app._pump_segment_queue()
            app._pump_segment_queue()
        assert h["history"].flush()
        rows = app.meeting.store.get_segments(meeting_id)
        assert rows[0].seq == saved.seq == 0
        assert rows[0].speaker_id == saved.speaker_id and rows[0].speaker_locked
        assert [r.seq for r in rows] == [0, 1, 2, 3]


def test_late_batch_after_exit_does_not_create_records_or_shift_queue_ids():
    with app_harness() as h:
        app = h["app"]
        _start(app, h)
        app._begin_transcription(np.full(48000, 0.1, dtype=np.float32), context=make_context(app, 10_000))
        before = app._current_seq
        app._exit_started = True
        app._on_speaker_turn_result(batch())
        assert _enqueued_records(h["history"]) == [] and app._current_seq == before

"""Continuous-session speaker labels live in history, not in pasted text."""

from __future__ import annotations

import numpy as np

from tests.helpers.app_harness import app_harness
from tests.test_app_history_wiring import (
    _enqueued_records,
    _start_continuous_user_session,
)
from sayink.audio_utils import TARGET_SAMPLE_RATE
from sayink.history_store import HistoryStore, SegmentRecord
from sayink.speaker_session import MAX_SPEAKERS, SpeakerSession, dominant_route, voice_embedding
from sayink.ui.history_window import HistoryWindow, _session_body
from sayink.vad_segmenter import SpeechSegmenter


def _tone(freq: float, seconds: float = 0.55) -> np.ndarray:
    n = int(TARGET_SAMPLE_RATE * seconds)
    t = np.arange(n, dtype=np.float32) / TARGET_SAMPLE_RATE
    return (0.25 * np.sin(2.0 * np.pi * freq * t)).astype(np.float32)


def test_same_voice_stays_one_speaker_and_a_different_voice_opens_the_next():
    session = SpeakerSession()
    first = session.assign(voice_embedding(_tone(140)))
    again = session.assign(voice_embedding(_tone(140)))
    other = session.assign(voice_embedding(_tone(280)))
    assert first == 1
    assert again == 1
    assert other == 2


def test_mic_and_system_routes_stay_different_people_even_with_the_same_voice():
    session = SpeakerSession()
    emb = voice_embedding(_tone(180))
    assert session.assign(emb, "mic") == 1
    assert session.assign(emb, "system") == 2
    assert session.assign(emb, "mic") == 1


def test_session_reset_starts_numbering_at_one():
    session = SpeakerSession()
    session.assign(voice_embedding(_tone(140)))
    session.assign(voice_embedding(_tone(280)))
    session.reset()
    assert session.assign(voice_embedding(_tone(280))) == 1


def test_ninth_distinct_voice_merges_into_an_existing_speaker():
    session = SpeakerSession()
    freqs = (80, 260, 520, 750, 1100, 1500, 2100, 2800, 90)
    ids = [session.assign(voice_embedding(_tone(freq))) for freq in freqs]
    assert max(ids) <= MAX_SPEAKERS
    assert len(set(ids[:MAX_SPEAKERS])) == MAX_SPEAKERS
    assert ids[-1] in ids[:MAX_SPEAKERS]


def test_quiet_audio_does_not_open_a_new_speaker():
    session = SpeakerSession()
    assert session.assign(voice_embedding(np.zeros(TARGET_SAMPLE_RATE, dtype=np.float32))) == 1
    spoken = session.assign(voice_embedding(_tone(200)))
    assert spoken == 1
    assert session.assign(voice_embedding(np.zeros(800, dtype=np.float32))) == 1


def test_dominant_route_needs_a_clear_energy_gap():
    assert dominant_route(9.0, 1.0) == "mic"
    assert dominant_route(1.0, 9.0) == "system"
    assert dominant_route(4.0, 3.0) == ""
    assert dominant_route(0.0, 0.0) == ""


def test_segmenter_reports_the_louder_route_for_the_emitted_segment():
    seg = SpeechSegmenter(speech_threshold=0.002, silence_hold_sec=0.2, min_speech_sec=0.1)
    assert seg.feed(_tone(180, 0.3), mic_energy=12.0, system_energy=0.2) is None
    out = seg.feed(np.zeros(int(TARGET_SAMPLE_RATE * 0.25), dtype=np.float32))
    assert out is not None and out.size > 0
    assert seg.last_route == "mic"


def test_history_roundtrip_keeps_speaker_fields(tmp_path):
    store = HistoryStore(db_path=tmp_path / "history.db")
    store.enqueue(
        SegmentRecord(
            "s1",
            0,
            1000,
            raw_text="甲",
            source="mixed",
            trigger_mode="continuous",
            speaker_id=1,
            speaker_route="mic",
        )
    )
    store.close(timeout=2.0)
    reopened = HistoryStore(db_path=tmp_path / "history.db")
    try:
        saved = reopened.get_session_segments("s1")
    finally:
        reopened.close(timeout=2.0)
    assert saved[0].speaker_id == 1
    assert saved[0].speaker_route == "mic"


def test_history_hides_speaker_until_a_second_person_appears():
    from tests.test_history_window import FakeHistoryStore

    store = FakeHistoryStore()
    store.segments["newer"] = [
        SegmentRecord(
            "newer", 0, 1_700_000_300_000, "只有我", "", "mic", 1000,
            "Notepad.exe", "continuous", "funasr-nano", speaker_id=1,
        ),
        SegmentRecord(
            "newer", 1, 1_700_000_301_000, "还是我", "", "mic", 1000,
            "Notepad.exe", "continuous", "funasr-nano", speaker_id=1,
        ),
    ]
    window = HistoryWindow(store)
    window._expand_session(window.session_items()[0])
    assert "说话人" not in window._details.toPlainText()

    store.segments["newer"][1] = SegmentRecord(
        "newer", 1, 1_700_000_301_000, "换了一个人", "", "mic", 1000,
        "Notepad.exe", "continuous", "funasr-nano", speaker_id=2,
    )
    window._expand_session(window.session_items()[0])
    detail = window._details.toPlainText()
    assert "说话人 1" in detail
    assert "说话人 2" in detail
    assert "只有我" in detail
    window.close()


def test_history_copy_includes_speaker_only_for_a_multi_speaker_session():
    solo = [
        SegmentRecord("s", 0, 1_700_000_300_000, "甲", "", "mic", 1000, "", "continuous", "m", speaker_id=1),
    ]
    assert _session_body(solo) == "甲"
    duo = [
        SegmentRecord("s", 0, 1_700_000_300_000, "甲", "", "mixed", 4000, "", "continuous", "m", speaker_id=1, speaker_route="mic"),
        SegmentRecord("s", 1, 1_700_000_304_000, "乙", "", "mixed", 5000, "", "continuous", "m", speaker_id=2, speaker_route="system"),
    ]
    body = _session_body(duo)
    assert body.index("说话人 1") < body.index("甲") < body.index("说话人 2") < body.index("乙")
    assert "麦克风" in body
    assert "电脑播放" in body


def test_continuous_paste_has_no_speaker_but_history_does():
    with app_harness({"audio.trigger_mode": "continuous", "llm.enabled": False}) as h:
        app = h["app"]
        _start_continuous_user_session(app)
        for text, audio in (("甲说的", _tone(140)), ("乙说的", _tone(280))):
            app._begin_transcription(audio)
            app._on_final_result(text)
            assert h["paster"].paste_async.call_args[0][0] == text
            h["paster"].paste_async.call_args[0][1]("pasted")
        records = _enqueued_records(h["history"])
        assert [record.speaker_id for record in records] == [1, 2]
        assert [record.speaker_route for record in records] == ["", ""]

        _start_continuous_user_session(app)
        app._begin_transcription(_tone(280))
        app._on_final_result("下一场")
        h["paster"].paste_async.call_args[0][1]("pasted")
        assert _enqueued_records(h["history"])[-1].speaker_id == 1


def test_mixed_continuous_history_keeps_the_capture_route():
    with app_harness(
        {"audio.trigger_mode": "continuous", "llm.enabled": False, "audio.input_source": "mixed"}
    ) as h:
        app = h["app"]
        _start_continuous_user_session(app)
        tone = _tone(180)
        app._begin_transcription(tone, route="mic")
        app._on_final_result("我这边")
        h["paster"].paste_async.call_args[0][1]("pasted")
        app._begin_transcription(tone, route="system")
        app._on_final_result("对方那边")
        assert h["paster"].paste_async.call_args[0][0] == "对方那边"
        h["paster"].paste_async.call_args[0][1]("pasted")
        records = _enqueued_records(h["history"])
        assert [(r.speaker_id, r.speaker_route) for r in records] == [(1, "mic"), (2, "system")]


def test_hold_to_talk_history_has_no_speaker_id():
    with app_harness({"audio.trigger_mode": "hotkey", "llm.enabled": False}) as h:
        app = h["app"]
        app._begin_transcription(_tone(140))
        app._on_final_result("短句")
        h["paster"].paste_async.call_args[0][1]("pasted")
        assert _enqueued_records(h["history"])[0].speaker_id == 0

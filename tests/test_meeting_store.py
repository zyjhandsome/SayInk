"""MeetingStore + the history_store hooks it relies on (flush / enqueue_write /
meeting rows hidden from dictation history and exempt from cleanup)."""

from __future__ import annotations

import sqlite3
import time

import pytest

from sayink.history_store import MEETING_TRIGGER_MODE, HistoryStore, SegmentRecord
from sayink.meeting_store import (
    STATUS_ENDED,
    STATUS_INTERRUPTED,
    STATUS_LIVE,
    MeetingStore,
)


def _seg(session_id: str, seq: int, text: str, *, created_at: int, meeting: bool = True,
         speaker_id: int = 0, captured_at: int = 0) -> SegmentRecord:
    return SegmentRecord(
        session_id=session_id,
        seq=seq,
        created_at=created_at,
        raw_text=text,
        source="mixed",
        duration_ms=1200,
        trigger_mode=MEETING_TRIGGER_MODE if meeting else "continuous",
        model="funasr-nano",
        speaker_id=speaker_id,
        captured_at=captured_at,
    )


@pytest.fixture
def history(tmp_path):
    s = HistoryStore(db_path=tmp_path / "history.db")
    yield s
    s.close(timeout=2.0)


@pytest.fixture
def meetings(history):
    return MeetingStore(history)


def test_generation_checkpoints_survive_restart_and_are_invalidated(tmp_path):
    path = tmp_path / "resumable.db"
    first = HistoryStore(db_path=path)
    store = MeetingStore(first)
    meeting = store.create_meeting()
    assert store.flush()
    store.save_generation_checkpoint(meeting.id, "config1", "step1", '{"summary":"done"}')
    first.close(timeout=2)
    second = HistoryStore(db_path=path)
    try:
        store = MeetingStore(second)
        assert store.generation_checkpoints(meeting.id, "config1") == {"step1": '{"summary":"done"}'}
        assert store.generation_checkpoints(meeting.id, "config2") == {}
        store.save_generation_checkpoint(meeting.id, "config2", "step2", '{"summary":"new"}')
        assert store.generation_checkpoints(meeting.id, "config1") == {}
        store.delete_meeting(meeting.id)
        assert store.flush()
        store.save_generation_checkpoint(meeting.id, "config2", "late", "ignored")
        assert store.generation_checkpoints(meeting.id, "config2") == {}
    finally:
        second.close(timeout=2)


class TestHistoryHooks:
    def test_flush_is_a_barrier_for_queued_writes(self, history):
        history.enqueue(_seg("m1", 0, "a", created_at=1000))
        assert history.flush(timeout=2.0)
        segs = history.get_session_segments("m1")
        assert [s.raw_text for s in segs] == ["a"]

    def test_captured_at_round_trips(self, history):
        history.enqueue(_seg("m1", 0, "a", created_at=5000, captured_at=4200))
        history.flush(timeout=2.0)
        assert history.get_session_segments("m1")[0].captured_at == 4200

    def test_relabel_session_speakers_renumbers_dictation_rows_too(self, history):
        """Continuous dictation re-clusters at session end through the same
        history hook meetings use; other sessions are untouched."""
        history.enqueue(_seg("d1", 0, "a", created_at=1, meeting=False, speaker_id=1))
        history.enqueue(_seg("d1", 1, "b", created_at=2, meeting=False, speaker_id=2))
        history.enqueue(_seg("d1", 2, "c", created_at=3, meeting=False, speaker_id=3))
        history.enqueue(_seg("d2", 0, "x", created_at=4, meeting=False, speaker_id=2))
        history.flush(timeout=2.0)
        history.relabel_session_speakers("d1", {1: 1, 2: 1, 3: 2})
        history.flush(timeout=2.0)
        assert [s.speaker_id for s in history.get_session_segments("d1")] == [1, 1, 2]
        assert [s.speaker_id for s in history.get_session_segments("d2")] == [2]
        # Identity / empty mappings are no-ops.
        history.relabel_session_speakers("d1", {1: 1, 2: 2})
        history.relabel_session_speakers("d1", {})
        history.flush(timeout=2.0)
        assert [s.speaker_id for s in history.get_session_segments("d1")] == [1, 1, 2]

    def test_old_db_without_captured_at_is_migrated(self, tmp_path):
        db = tmp_path / "history.db"
        conn = sqlite3.connect(db)
        conn.executescript(
            """
            CREATE TABLE history (
              id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, seq INTEGER NOT NULL,
              created_at INTEGER NOT NULL, raw_text TEXT NOT NULL DEFAULT '',
              polished_text TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
              duration_ms INTEGER NOT NULL DEFAULT 0, target_app TEXT NOT NULL DEFAULT '',
              trigger_mode TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT ''
            );
            INSERT INTO history (session_id, seq, created_at, raw_text) VALUES ('old', 0, 1, 'x');
            """
        )
        conn.commit()
        conn.close()
        store = HistoryStore(db_path=db)
        try:
            assert not store.disabled
            segs = store.get_session_segments("old")
            assert segs[0].captured_at == 0
            assert store.list_sessions()[0].session_id == "old"
        finally:
            store.close(timeout=2.0)

    def test_enqueue_write_runs_on_writer_and_commits(self, history):
        def _fn(conn):
            conn.execute("CREATE TABLE IF NOT EXISTS probe (v INTEGER)")
            conn.execute("INSERT INTO probe VALUES (7)")

        history.enqueue_write(_fn)
        history.flush(timeout=2.0)
        assert history.read(lambda c: c.execute("SELECT v FROM probe").fetchone()[0]) == 7

    def test_enqueue_write_failure_rolls_back_and_notifies(self, history):
        messages: list[str] = []
        history.add_failed_callback(messages.append)

        def _bad(conn):
            conn.execute("CREATE TABLE IF NOT EXISTS probe (v INTEGER)")
            conn.execute("INSERT INTO probe VALUES (1)")
            raise RuntimeError("boom")

        history.enqueue_write(_bad, failure_notice="自定义失败")
        history.flush(timeout=2.0)
        assert messages == ["自定义失败"]
        count = history.read(lambda c: c.execute("SELECT COUNT(*) FROM probe").fetchone()[0])
        assert count == 0

    def test_meeting_rows_hidden_from_dictation_history(self, history, meetings):
        meetings.create_meeting(meeting_id="m1", title="周会")
        history.enqueue(_seg("m1", 0, "会议内容关键词", created_at=1000))
        history.enqueue(_seg("d1", 0, "听写内容", created_at=2000, meeting=False))
        history.flush(timeout=2.0)
        assert [s.session_id for s in history.list_sessions()] == ["d1"]
        assert history.search_sessions("关键词") == []
        assert [s.session_id for s in history.search_sessions("听写")] == ["d1"]

    def test_cleanup_and_delete_all_exempt_meetings(self, history, meetings):
        meetings.create_meeting(meeting_id="m1", title="周会", started_at=1000)
        history.enqueue(_seg("m1", 0, "会议", created_at=1000))
        for i in range(3):
            history.enqueue(_seg(f"d{i}", 0, f"听写{i}", created_at=2000 + i, meeting=False))
        history.flush(timeout=2.0)

        history.enqueue_cleanup(retention_days=1, max_entries=1, active_session_id=None)
        history.flush(timeout=2.0)
        assert history.get_session_segments("m1")
        assert len(history.list_sessions()) <= 1

        history.enqueue_delete_all()
        history.flush(timeout=2.0)
        assert history.get_session_segments("m1")
        assert history.list_sessions() == []


class TestMeetingStore:
    def test_create_end_and_read_back(self, meetings):
        rec = meetings.create_meeting(
            title="产品周会", participants="张三, 李四", agenda="进度;风险", source="mixed",
            model_id="funasr-nano", vad="silero",
            glossary=[{"term": "SayInk", "variants": ["say ink"]}], started_at=1_000_000,
        )
        assert rec.status == STATUS_LIVE
        meetings.end_meeting(rec.id, ended_at=1_600_000)
        meetings.flush()
        got = meetings.get_meeting(rec.id)
        assert got is not None
        assert got.status == STATUS_ENDED
        assert got.duration_ms == 600_000
        assert got.glossary == [{"term": "SayInk", "variants": ["say ink"]}]
        assert got.display_title == "产品周会"
        assert [m.id for m in meetings.list_meetings()] == [rec.id]

    def test_display_title_falls_back_to_time(self, meetings):
        rec = meetings.create_meeting(started_at=int(time.time() * 1000))
        assert rec.display_title.endswith("会议")

    def test_display_title_fallback_on_an_ansi_code_page(self, meetings, ansi_locale):
        started = int(time.mktime((2026, 3, 5, 9, 7, 0, 0, 0, -1)) * 1000)
        rec = meetings.create_meeting(started_at=started)
        assert rec.display_title == "03-05 09:07 会议"

    def test_marks_speakers_notes(self, meetings):
        rec = meetings.create_meeting(meeting_id="m1")
        meetings.add_mark("m1", 12_000, "重点")
        meetings.add_mark("m1", 3_000)
        meetings.set_speaker("m1", 1, suggested_name="张三")
        meetings.set_speaker("m1", 1, display_name="张三(PM)")
        meetings.set_speaker("m1", 2)
        meetings.save_notes(
            "m1", template_id="general", model="gpt", json_payload='{"summary": "x"}',
            markdown="# v1", duration_ms=5000,
        )
        meetings.save_notes(
            "m1", template_id="general", model="gpt", json_payload='{"summary": "y"}',
            markdown="# v2", chunked=True,
        )
        meetings.flush()
        assert [m.offset_ms for m in meetings.get_marks("m1")] == [3_000, 12_000]
        speakers = meetings.get_speakers("m1")
        assert speakers[0].label == "张三(PM)"
        assert speakers[0].suggested_name == "张三"
        assert speakers[1].label == "说话人2"
        notes = meetings.get_notes("m1")
        assert notes.markdown == "# v2"
        assert notes.previous_markdown == "# v1"
        assert notes.chunked is True
        assert notes.payload() == {"summary": "y"}
        assert notes.user_edited is False

        meetings.save_user_markdown("m1", "# edited")
        meetings.flush()
        assert meetings.get_notes("m1").user_edited is True
        assert meetings.get_notes("m1").markdown == "# edited"
        assert rec.id == "m1"

    def test_search_covers_title_transcript_and_notes(self, history, meetings):
        meetings.create_meeting(meeting_id="m1", title="预算评审")
        meetings.create_meeting(meeting_id="m2", title="站会")
        meetings.create_meeting(meeting_id="m3", title="其它")
        history.enqueue(_seg("m2", 0, "今天讨论上线窗口", created_at=1000))
        meetings.save_notes("m3", template_id="g", model="m", json_payload="{}", markdown="决定延期")
        meetings.flush()
        assert [m.id for m in meetings.search_meetings("预算")] == ["m1"]
        assert [m.id for m in meetings.search_meetings("上线")] == ["m2"]
        assert [m.id for m in meetings.search_meetings("延期")] == ["m3"]
        assert len(meetings.search_meetings("")) == 3
        assert meetings.search_meetings("%") == []
        assert meetings.search_meetings("_") == []

    def test_relabel_segments_swaps_without_collision(self, history, meetings):
        meetings.create_meeting(meeting_id="m1")
        history.enqueue(_seg("m1", 0, "a", created_at=1, speaker_id=1))
        history.enqueue(_seg("m1", 1, "b", created_at=2, speaker_id=2))
        history.enqueue(_seg("m1", 2, "c", created_at=3, speaker_id=3))
        history.flush(timeout=2.0)
        meetings.relabel_segments("m1", {1: 2, 2: 1})
        meetings.flush()
        assert [s.speaker_id for s in meetings.get_segments("m1")] == [2, 1, 3]

    def test_delete_meeting_removes_everything(self, history, meetings):
        meetings.create_meeting(meeting_id="m1")
        history.enqueue(_seg("m1", 0, "a", created_at=1))
        meetings.add_mark("m1", 1)
        meetings.set_speaker("m1", 1)
        meetings.save_notes("m1", template_id="g", model="m", json_payload="{}", markdown="x")
        meetings.flush()
        meetings.delete_meeting("m1")
        meetings.flush()
        assert meetings.get_meeting("m1") is None
        assert meetings.get_segments("m1") == []
        assert meetings.get_marks("m1") == []
        assert meetings.get_speakers("m1") == []
        assert meetings.get_notes("m1") is None
        assert meetings.segment_count("m1") == 0

    def test_interrupted_meetings_marked_on_startup(self, history, meetings):
        meetings.create_meeting(meeting_id="m1", started_at=1000)
        history.enqueue(_seg("m1", 0, "a", created_at=5000))
        meetings.flush()
        meetings.mark_interrupted_meetings()
        meetings.flush()
        got = meetings.get_meeting("m1")
        assert got.status == STATUS_INTERRUPTED
        assert got.ended_at == 5000

    def test_create_meeting_from_sessions_copies_segments_and_keeps_dictation_history(self, history, meetings):
        history.enqueue(
            SegmentRecord(
                session_id="d2",
                seq=0,
                created_at=20_000,
                raw_text="第二条原始",
                polished_text="第二条润色",
                source="mic",
                duration_ms=2000,
                target_app="WeChat.exe",
                trigger_mode="hotkey",
                model="funasr-nano",
                speaker_id=2,
                speaker_route="mic",
                captured_at=18_000,
            )
        )
        history.enqueue(
            SegmentRecord(
                session_id="d1",
                seq=0,
                created_at=10_000,
                raw_text="第一条原始",
                polished_text="",
                source="mixed",
                duration_ms=1500,
                target_app="Code.exe",
                trigger_mode="continuous",
                model="funasr-nano",
                speaker_id=1,
                speaker_route="system",
                captured_at=8_500,
            )
        )
        history.flush(timeout=2.0)

        rec = meetings.create_meeting_from_sessions(["d2", "d1"])
        meetings.flush(timeout=2.0)

        assert rec is not None
        got = meetings.get_meeting(rec.id)
        assert got is not None
        assert got.status == STATUS_ENDED
        assert got.started_at == 8_500
        assert got.ended_at == 20_000
        assert got.source == "mixed"
        assert got.model_id == "funasr-nano"

        m_segs = meetings.get_segments(rec.id)
        assert [(s.seq, s.raw_text, s.polished_text, s.trigger_mode, s.speaker_id) for s in m_segs] == [
            (0, "第一条原始", "", MEETING_TRIGGER_MODE, 1),
            (1, "第二条原始", "第二条润色", MEETING_TRIGGER_MODE, 2),
        ]
        # Original dictation sessions remain intact in dictation history.
        dictation_ids = [s.session_id for s in history.list_sessions()]
        assert dictation_ids == ["d2", "d1"]
        assert len(history.get_session_segments("d1")) == 1
        assert len(history.get_session_segments("d2")) == 1

    def test_disabled_store_is_inert(self, tmp_path, monkeypatch):
        db_dir = tmp_path / "ro"
        db_dir.write_text("not a dir")
        history = HistoryStore(db_path=db_dir / "history.db")
        assert history.disabled
        store = MeetingStore(history)
        rec = store.create_meeting(title="x")
        assert rec.title == "x"
        assert store.list_meetings() == []
        assert store.get_meeting(rec.id) is None
        assert history.flush() is True

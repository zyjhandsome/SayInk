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


def _ended(store: MeetingStore, meeting_id: str) -> None:
    store.create_meeting(meeting_id=meeting_id, started_at=1_000)
    store.end_meeting(meeting_id, ended_at=61_000)


def test_generation_queue_survives_restart_and_orders_user_requests_first(tmp_path):
    from sayink.meeting_store import QUEUE_FAILED, QUEUE_PENDING, QUEUE_PRIORITY_AUTO, QUEUE_PRIORITY_USER

    path = tmp_path / "queue.db"
    first = HistoryStore(db_path=path)
    store = MeetingStore(first)
    for meeting_id in ("auto1", "auto2", "user1", "failed"):
        _ended(store, meeting_id)
    store.enqueue_generation("auto1", "general", priority=QUEUE_PRIORITY_AUTO, enqueued_at=10)
    store.enqueue_generation("auto2", "standup", priority=QUEUE_PRIORITY_AUTO, enqueued_at=20)
    store.enqueue_generation("user1", "general", priority=QUEUE_PRIORITY_USER, enqueued_at=30)
    store.enqueue_generation("failed", "general", priority=QUEUE_PRIORITY_AUTO, enqueued_at=5)
    store.fail_generation("failed", "HTTP 401；请检查接口密钥")
    store.reschedule_generation("auto2", attempts=1, not_before=99_000, last_error="超时")
    assert store.flush()
    first.close(timeout=2)

    second = HistoryStore(db_path=path)
    try:
        store = MeetingStore(second)
        queue = store.generation_queue()
        assert [job.meeting_id for job in queue] == ["user1", "failed", "auto1", "auto2"]
        assert queue[0].priority == QUEUE_PRIORITY_USER and not queue[0].automatic
        auto2 = store.queued_generation("auto2")
        assert auto2 is not None and auto2.template_id == "standup"
        assert (auto2.attempts, auto2.not_before, auto2.last_error, auto2.state) == (1, 99_000, "超时", QUEUE_PENDING)
        failed = store.queued_generation("failed")
        assert failed is not None and failed.state == QUEUE_FAILED and "401" in failed.last_error
        store.dequeue_generation("auto1")
        store.delete_meeting("auto2")
        assert store.flush()
        assert [job.meeting_id for job in store.generation_queue()] == ["user1", "failed"]
    finally:
        second.close(timeout=2)


def test_user_request_upgrades_an_automatic_job_but_not_the_reverse(meetings):
    from sayink.meeting_store import QUEUE_PENDING, QUEUE_PRIORITY_AUTO, QUEUE_PRIORITY_USER

    _ended(meetings, "m")
    meetings.enqueue_generation("m", "general", priority=QUEUE_PRIORITY_AUTO, enqueued_at=10)
    meetings.fail_generation("m", "余额不足")
    meetings.enqueue_generation("m", "review", priority=QUEUE_PRIORITY_USER, enqueued_at=20)
    assert meetings.flush()
    job = meetings.queued_generation("m")
    assert job is not None
    assert (job.priority, job.template_id, job.state, job.attempts, job.last_error) == (
        QUEUE_PRIORITY_USER, "review", QUEUE_PENDING, 0, "")
    meetings.enqueue_generation("m", "general", priority=QUEUE_PRIORITY_AUTO, enqueued_at=30)
    assert meetings.flush()
    job = meetings.queued_generation("m")
    assert job is not None and job.priority == QUEUE_PRIORITY_USER and job.template_id == "review"


def test_queue_ignores_live_and_deleted_meetings(meetings):
    from sayink.meeting_store import QUEUE_PRIORITY_AUTO

    meetings.create_meeting(meeting_id="live")
    _ended(meetings, "gone")
    meetings.delete_meeting("gone")
    meetings.enqueue_generation("live", "general", priority=QUEUE_PRIORITY_AUTO)
    meetings.enqueue_generation("gone", "general", priority=QUEUE_PRIORITY_AUTO)
    assert meetings.flush()
    assert meetings.generation_queue() == []


def test_legacy_transcript_migration_and_corrections_survive_restart(tmp_path):
    from sayink.history_store import _DDL

    path = tmp_path / "legacy-transcript.db"
    legacy_ddl = _DDL.replace(",\n  corrected_text TEXT NOT NULL DEFAULT ''", "")
    with sqlite3.connect(path) as conn:
        conn.executescript(legacy_ddl)
        conn.execute("INSERT INTO meetings (id, status, started_at) VALUES ('legacy', 'interrupted', 1000)")
        conn.execute("INSERT INTO history (session_id, seq, created_at, raw_text, trigger_mode) "
                     "VALUES ('legacy', 7, 2000, '需要核对的识别原文', 'meeting')")
    history = HistoryStore(db_path=path)
    try:
        store = MeetingStore(history)
        original = store.get_segments("legacy")[0]
        assert original.raw_text == "需要核对的识别原文" and original.corrected_text == ""
        store.set_segment_text("legacy", 7, "人工修正后的文字")
        assert store.flush()
    finally:
        history.close(timeout=2)
    reopened = HistoryStore(db_path=path)
    try:
        segment = MeetingStore(reopened).get_segments("legacy")[0]
        assert segment.raw_text == original.raw_text and segment.corrected_text == "人工修正后的文字"
        assert reopened.get_session_segments("legacy")[0] == segment
        assert reopened.list_sessions() == []
    finally:
        reopened.close(timeout=2)


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

    def test_relabel_session_segments_moves_single_rows_but_not_locked_ones(self, history):
        """The end-of-session clustering pass re-assigns individual rows by
        seq; a row the user corrected keeps its speaker, and other sessions
        are untouched."""
        history.enqueue(_seg("d1", 0, "a", created_at=1, meeting=False, speaker_id=1))
        history.enqueue(_seg("d1", 1, "b", created_at=2, meeting=False, speaker_id=2))
        locked = _seg("d1", 2, "c", created_at=3, meeting=False, speaker_id=2)
        history.enqueue(SegmentRecord(**{**locked.__dict__, "speaker_locked": True}))
        history.enqueue(_seg("d2", 0, "x", created_at=4, meeting=False, speaker_id=2))
        history.flush(timeout=2.0)
        history.relabel_session_segments("d1", {0: 1, 1: 1, 2: 1})
        history.relabel_session_segments("d1", {})
        history.relabel_session_segments("", {0: 3})
        history.flush(timeout=2.0)
        assert [s.speaker_id for s in history.get_session_segments("d1")] == [1, 1, 2]
        assert [s.speaker_id for s in history.get_session_segments("d2")] == [2]

    def test_set_segment_speakers_respects_locks_and_meeting_rows_only(self, history, meetings):
        meeting = meetings.create_meeting()
        for seq, sid in enumerate((1, 2, 1, 2)):
            history.enqueue(_seg(meeting.id, seq, f"t{seq}", created_at=seq + 1, speaker_id=sid))
        history.enqueue(_seg(meeting.id, 9, "dictation row with same id", created_at=10, meeting=False, speaker_id=2))
        assert meetings.flush()
        meetings.set_segment_speaker(meeting.id, 3, 2)  # user says seq 3 really is speaker 2
        assert meetings.flush()
        meetings.set_segment_speakers(meeting.id, {0: 1, 1: 1, 2: 1, 3: 1, 9: 1})
        meetings.set_segment_speakers(meeting.id, {})
        assert meetings.flush()
        rows = meetings.get_segments(meeting.id)
        assert [s.speaker_id for s in rows] == [1, 1, 1, 2]
        assert rows[3].speaker_locked
        assert [s.speaker_id for s in history.get_session_segments(meeting.id) if s.seq == 9] == [2]

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
        history.enqueue(_seg("d1", 0, "转写内容", created_at=2000, meeting=False))
        history.flush(timeout=2.0)
        assert [s.session_id for s in history.list_sessions()] == ["d1"]
        assert history.search_sessions("关键词") == []
        assert [s.session_id for s in history.search_sessions("转写")] == ["d1"]

    def test_cleanup_and_delete_all_exempt_meetings(self, history, meetings):
        meetings.create_meeting(meeting_id="m1", title="周会", started_at=1000)
        history.enqueue(_seg("m1", 0, "会议", created_at=1000))
        for i in range(3):
            history.enqueue(_seg(f"d{i}", 0, f"转写{i}", created_at=2000 + i, meeting=False))
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
        rows = history.read(lambda conn: conn.execute(
            "SELECT COUNT(*) FROM history WHERE session_id = ?", ("m1",)).fetchone()[0])
        assert rows == 0

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

"""TDD tests for HistoryStore (Phase 1 voice library)."""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from voiceink.history_store import HistoryStore, SegmentRecord


def _record(
    session_id: str,
    seq: int,
    *,
    created_at: int | None = None,
    raw_text: str = "",
    polished_text: str = "",
    source: str = "mic",
    duration_ms: int = 1000,
    target_app: str = "notepad.exe",
    trigger_mode: str = "continuous",
    model: str = "fireredasr2-ctc",
) -> SegmentRecord:
    return SegmentRecord(
        session_id=session_id,
        seq=seq,
        created_at=created_at if created_at is not None else int(time.time() * 1000),
        raw_text=raw_text,
        polished_text=polished_text,
        source=source,
        duration_ms=duration_ms,
        target_app=target_app,
        trigger_mode=trigger_mode,
        model=model,
    )


@pytest.fixture
def store(tmp_path):
    s = HistoryStore(db_path=tmp_path / "history.db")
    yield s
    s.close(timeout=2.0)


class TestWriteAndList:
    def test_enqueue_segments_then_list_by_session(self, store):
        store.enqueue(_record("s1", 0, raw_text="今天天气很好", created_at=1000))
        store.enqueue(_record("s1", 1, raw_text="我们去公园散步", created_at=2000))
        store.enqueue(_record("s2", 0, raw_text="另一场", created_at=3000, source="system"))
        store.close(timeout=2.0)

        sessions = store.list_sessions(limit=10, offset=0)
        assert [s.session_id for s in sessions] == ["s2", "s1"]
        assert sessions[1].segment_count == 2
        assert sessions[1].created_at == 1000

        segs = store.get_session_segments("s1")
        assert [r.seq for r in segs] == [0, 1]
        assert segs[0].raw_text == "今天天气很好"
        assert segs[1].raw_text == "我们去公园散步"


class TestLikeSearch:
    def test_chinese_substring_hits_two_char_words(self, store):
        # Unspaced Chinese sentence — LIKE must find 1–2 char substrings (ADR-0002).
        store.enqueue(
            _record(
                "s1",
                0,
                raw_text="今天天气很好我们去公园散步",
                created_at=1000,
            )
        )
        store.close(timeout=2.0)

        weather = store.search_sessions("天气")
        walk = store.search_sessions("散步")
        assert [s.session_id for s in weather] == ["s1"]
        assert [s.session_id for s in walk] == ["s1"]
        assert store.search_sessions("不存在") == []


class TestCleanup:
    def test_cleanup_by_max_sessions_deletes_oldest_whole_session(self, store):
        # retention_days=0 skips age cleanup; only max_entries applies.
        store.enqueue(_record("old", 0, raw_text="a", created_at=1000))
        store.enqueue(_record("old", 1, raw_text="b", created_at=1100))
        store.enqueue(_record("mid", 0, raw_text="c", created_at=2000))
        store.enqueue(_record("new", 0, raw_text="d", created_at=3000))
        store.enqueue_cleanup(retention_days=0, max_entries=2, active_session_id=None)
        store.close(timeout=2.0)

        ids = [s.session_id for s in store.list_sessions(limit=10, offset=0)]
        assert ids == ["new", "mid"]
        assert store.get_session_segments("old") == []

    def test_cleanup_by_age_uses_session_min_created_at(self, store, monkeypatch):
        now_ms = 1_000_000_000_000
        monkeypatch.setattr(
            "voiceink.history_store.time.time",
            lambda: now_ms / 1000.0,
        )
        # Session age = MIN(created_at); keep whole session or delete whole.
        store.enqueue(_record("aged", 0, raw_text="old", created_at=now_ms - 10 * 86_400_000))
        store.enqueue(
            _record("aged", 1, raw_text="late", created_at=now_ms - 1 * 86_400_000)
        )
        store.enqueue(_record("fresh", 0, raw_text="new", created_at=now_ms - 1 * 86_400_000))
        store.enqueue_cleanup(retention_days=5, max_entries=5000, active_session_id=None)
        store.close(timeout=2.0)

        ids = [s.session_id for s in store.list_sessions(limit=10, offset=0)]
        assert ids == ["fresh"]
        assert store.get_session_segments("aged") == []

    def test_cleanup_exempts_active_session(self, store):
        store.enqueue(_record("active", 0, raw_text="a", created_at=1000))
        store.enqueue(_record("other", 0, raw_text="b", created_at=2000))
        store.enqueue_cleanup(retention_days=0, max_entries=1, active_session_id="active")
        store.close(timeout=2.0)

        ids = {s.session_id for s in store.list_sessions(limit=10, offset=0)}
        assert "active" in ids

    def test_cleanup_deletes_newest_non_active_when_active_is_oldest(self, store):
        # Active is oldest; max_entries=1 must still drop the newer non-active
        # session so the store returns to the limit (ADR-0005).
        store.enqueue(_record("active", 0, raw_text="a", created_at=1000))
        store.enqueue(_record("newer", 0, raw_text="b", created_at=2000))
        store.enqueue_cleanup(retention_days=0, max_entries=1, active_session_id="active")
        store.close(timeout=2.0)

        ids = [s.session_id for s in store.list_sessions(limit=10, offset=0)]
        assert ids == ["active"]
        assert store.get_session_segments("newer") == []


class TestEnqueueSemantics:
    def test_search_sessions_builds_summaries_for_all_matches(self, store):
        store.enqueue(
            _record("hit", 0, raw_text="今天天气很好", polished_text="", created_at=1)
        )
        store.close(timeout=2.0)
        results = store.search_sessions("天气")
        assert len(results) == 1
        assert results[0].session_id == "hit"
        assert "天气" in results[0].preview

    def test_close_flushes_queued_writes(self, tmp_path):
        s = HistoryStore(db_path=tmp_path / "flush.db")
        for i in range(30):
            s.enqueue(_record("s", i, raw_text=f"t{i}", created_at=1000 + i))
        s.close(timeout=5.0)
        assert len(s.get_session_segments("s")) == 30

    def test_enqueue_notifies_after_commit_when_readers_can_see_row(self, store):
        seen = threading.Event()

        def on_committed():
            if store.list_sessions():
                seen.set()

        store.add_committed_callback(on_committed)
        store.enqueue(_record("live", 0, raw_text="实时写入", created_at=1000))
        assert seen.wait(timeout=2.0)
        assert store.list_sessions()[0].session_id == "live"

    def test_enqueue_is_nonblocking_and_flush_preserves_queue(self, store):
        start = time.perf_counter()
        for i in range(50):
            store.enqueue(_record("s", i, raw_text=f"t{i}", created_at=1000 + i))
        elapsed = time.perf_counter() - start
        assert elapsed < 0.5  # put-only; must not wait on disk

        store.close(timeout=5.0)
        assert len(store.get_session_segments("s")) == 50


class TestConcurrency:
    def test_concurrent_enqueue_and_delete_no_lock_errors(self, store):
        errors: list[BaseException] = []

        def writer():
            try:
                for i in range(80):
                    store.enqueue(
                        _record("live", i % 5, raw_text=f"x{i}", created_at=10_000 + i)
                    )
            except BaseException as exc:  # noqa: BLE001 — collect any failure
                errors.append(exc)

        def deleter():
            try:
                for i in range(20):
                    store.enqueue_delete_sessions([f"dead-{i}"])
                    store.enqueue(
                        _record(f"dead-{i}", 0, raw_text="tmp", created_at=i)
                    )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=writer), threading.Thread(target=deleter)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)
        store.close(timeout=5.0)

        assert errors == []
        # Survived without raising; live session still readable.
        assert store.get_session_segments("live")  # at least some rows


class TestFailureIsolation:
    def test_writer_execute_failure_does_not_raise_to_caller(self, store, monkeypatch):
        def boom(_conn, _record):
            raise sqlite3.OperationalError("injected write failure")

        monkeypatch.setattr(store, "_do_insert", boom)
        store.enqueue(_record("s", 0, raw_text="x", created_at=1))
        store.close(timeout=2.0)  # must not raise

    def test_writer_failure_notifies_failed_callback(self, store, monkeypatch):
        """F-21: a failed write is reported (once per failure) instead of only logged."""
        from voiceink.history_store import WRITE_FAILED_NOTICE

        def boom(_conn, _record):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(store, "_do_insert", boom)
        seen = []
        store.add_failed_callback(seen.append)
        store.enqueue(_record("s", 0, raw_text="x", created_at=1))
        store.close(timeout=2.0)
        assert seen == [WRITE_FAILED_NOTICE]

    def test_corrupt_db_is_quarantined_and_recreated(self, tmp_path):
        """F-21: a non-SQLite history.db is renamed like config.corrupt-*, then rebuilt."""
        db = tmp_path / "history.db"
        db.write_bytes(b"this is not a sqlite database, just junk bytes" * 40)
        (tmp_path / "history.db-wal").write_bytes(b"wal")

        s = HistoryStore(db_path=db)
        try:
            assert s.disabled is False
            assert "已损坏" in s.startup_notice and "history.corrupt-" in s.startup_notice
            backups = list(tmp_path.glob("history.corrupt-*.db"))
            assert len(backups) == 1
            assert backups[0].read_bytes().startswith(b"this is not a sqlite database")
            s.enqueue(_record("s1", 0, raw_text="写入新库", created_at=1))
        finally:
            s.close(timeout=2.0)
        assert [x.session_id for x in s.list_sessions(10, 0)] == ["s1"]

    def test_healthy_db_has_no_startup_notice(self, store):
        assert store.disabled is False
        assert store.startup_notice == ""

    def test_locked_or_unopenable_db_is_not_quarantined(self, tmp_path, monkeypatch):
        """OperationalError means the file is fine (locked / permissions); keep it."""
        db = tmp_path / "history.db"
        db.write_bytes(b"keep me")

        def fail_connect(*_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(sqlite3, "connect", fail_connect)
        s = HistoryStore(db_path=db)
        assert s.disabled is True
        assert s.startup_notice
        assert db.read_bytes() == b"keep me"
        assert not list(tmp_path.glob("history.corrupt-*"))
        s.close(timeout=1.0)

    def test_init_failure_disables_all_apis(self, tmp_path, monkeypatch):
        def fail_connect(*_args, **_kwargs):
            raise sqlite3.OperationalError("cannot open")

        monkeypatch.setattr(sqlite3, "connect", fail_connect)
        s = HistoryStore(db_path=tmp_path / "nope.db")
        assert s.disabled is True
        assert s.startup_notice
        s.enqueue(_record("s", 0, raw_text="x", created_at=1))
        s.enqueue_delete_sessions(["s"])
        s.enqueue_delete_all()
        s.enqueue_cleanup(90, 5000, None)
        assert s.list_sessions(10, 0) == []
        assert s.search_sessions("x") == []
        assert s.get_session_segments("s") == []
        s.close(timeout=1.0)

    def test_user_version_is_one(self, store, tmp_path):
        store.close(timeout=2.0)
        conn = sqlite3.connect(tmp_path / "history.db")
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()
        assert version == 1

    def test_schema_has_no_summary_column(self, store, tmp_path):
        store.close(timeout=2.0)
        conn = sqlite3.connect(tmp_path / "history.db")
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(history)").fetchall()]
        finally:
            conn.close()
        assert "summary" not in cols
        assert "session_id" in cols
        assert "seq" in cols

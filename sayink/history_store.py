"""Local voice-history store: SQLite + single background writer thread."""

from __future__ import annotations

import logging
import queue
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS history (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id    TEXT    NOT NULL,
  seq           INTEGER NOT NULL,
  created_at    INTEGER NOT NULL,
  raw_text      TEXT    NOT NULL DEFAULT '',
  polished_text TEXT    NOT NULL DEFAULT '',
  source        TEXT    NOT NULL DEFAULT '',
  duration_ms   INTEGER NOT NULL DEFAULT 0,
  target_app    TEXT    NOT NULL DEFAULT '',
  trigger_mode  TEXT    NOT NULL DEFAULT '',
  model         TEXT    NOT NULL DEFAULT '',
  speaker_id    INTEGER NOT NULL DEFAULT 0,
  speaker_route TEXT    NOT NULL DEFAULT '',
  captured_at   INTEGER NOT NULL DEFAULT 0,
  corrected_text TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_history_session ON history(session_id);
CREATE INDEX IF NOT EXISTS idx_history_created ON history(created_at);

-- Meetings (docs/meeting-notes-design.md §5.2). Transcript rows stay in
-- ``history`` with session_id = meetings.id and trigger_mode = 'meeting'.
CREATE TABLE IF NOT EXISTS meetings (
  id            TEXT PRIMARY KEY,
  title         TEXT NOT NULL DEFAULT '',
  participants  TEXT NOT NULL DEFAULT '',
  agenda        TEXT NOT NULL DEFAULT '',
  notes_user    TEXT NOT NULL DEFAULT '',
  source        TEXT NOT NULL DEFAULT '',
  model_id      TEXT NOT NULL DEFAULT '',
  vad           TEXT NOT NULL DEFAULT '',
  glossary      TEXT NOT NULL DEFAULT '',
  started_at    INTEGER NOT NULL,
  ended_at      INTEGER,
  status        TEXT NOT NULL DEFAULT 'live',
  deleted_at    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_meetings_started ON meetings(started_at);
CREATE TABLE IF NOT EXISTS meeting_marks (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  meeting_id TEXT    NOT NULL,
  offset_ms  INTEGER NOT NULL,
  note       TEXT    NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS meeting_speakers (
  meeting_id     TEXT    NOT NULL,
  speaker_id     INTEGER NOT NULL,
  display_name   TEXT    NOT NULL DEFAULT '',
  suggested_name TEXT    NOT NULL DEFAULT '',
  name_locked    INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (meeting_id, speaker_id)
);
CREATE TABLE IF NOT EXISTS meeting_notes (
  meeting_id        TEXT PRIMARY KEY,
  template_id       TEXT    NOT NULL DEFAULT '',
  model             TEXT    NOT NULL DEFAULT '',
  json_payload      TEXT    NOT NULL DEFAULT '',
  markdown          TEXT    NOT NULL DEFAULT '',
  previous_markdown TEXT    NOT NULL DEFAULT '',
  user_edited       INTEGER NOT NULL DEFAULT 0,
  generated_at      INTEGER NOT NULL DEFAULT 0,
  duration_ms       INTEGER NOT NULL DEFAULT 0,
  chunked           INTEGER NOT NULL DEFAULT 0,
  transcript_changed INTEGER NOT NULL DEFAULT 0,
  previous_transcript_changed INTEGER NOT NULL DEFAULT 0,
  previous_metadata TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS meeting_generation_cache (
  meeting_id TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  step_key TEXT NOT NULL,
  payload TEXT NOT NULL,
  PRIMARY KEY (meeting_id, fingerprint, step_key)
);
"""

# Sessions that belong to a meeting are kept out of the dictation history
# list and never touched by retention cleanup (the meeting page owns them).
_PROTECTED_SESSIONS_SQL = "SELECT id FROM meetings WHERE status != 'deleted'"
MEETING_TRIGGER_MODE = "meeting"


def _ensure_speaker_columns(conn: sqlite3.Connection) -> None:
    """Add columns on databases created before continuous diarization / meetings."""
    present = {row[1] for row in conn.execute("PRAGMA table_info(history)")}
    if "speaker_id" not in present:
        conn.execute("ALTER TABLE history ADD COLUMN speaker_id INTEGER NOT NULL DEFAULT 0")
    if "speaker_route" not in present:
        conn.execute("ALTER TABLE history ADD COLUMN speaker_route TEXT NOT NULL DEFAULT ''")
    if "captured_at" not in present:
        conn.execute("ALTER TABLE history ADD COLUMN captured_at INTEGER NOT NULL DEFAULT 0")
    if "corrected_text" not in present:
        conn.execute("ALTER TABLE history ADD COLUMN corrected_text TEXT NOT NULL DEFAULT ''")


def _ensure_meeting_note_columns(conn: sqlite3.Connection) -> None:
    speaker_columns = {row[1] for row in conn.execute("PRAGMA table_info(meeting_speakers)")}
    if "name_locked" not in speaker_columns:
        conn.execute("ALTER TABLE meeting_speakers ADD COLUMN name_locked INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE meeting_speakers SET name_locked = 1 WHERE display_name != ''")
    present = {row[1] for row in conn.execute("PRAGMA table_info(meeting_notes)")}
    for name in ("transcript_changed", "previous_transcript_changed"):
        if name not in present:
            conn.execute(f"ALTER TABLE meeting_notes ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")
    if "previous_metadata" not in present:
        conn.execute("ALTER TABLE meeting_notes ADD COLUMN previous_metadata TEXT NOT NULL DEFAULT ''")

_SESSION_SUMMARY_SQL = """
SELECT
  h.session_id,
  MIN(h.created_at) AS session_created,
  COUNT(*) AS segment_count,
  (
    SELECT source FROM history h2
    WHERE h2.session_id = h.session_id
    ORDER BY h2.seq ASC LIMIT 1
  ) AS source,
  (
    SELECT target_app FROM history h2
    WHERE h2.session_id = h.session_id
    ORDER BY h2.seq ASC LIMIT 1
  ) AS target_app,
  (
    SELECT CASE
      WHEN polished_text != '' THEN polished_text
      ELSE raw_text
    END
    FROM history h2
    WHERE h2.session_id = h.session_id
    ORDER BY h2.seq ASC LIMIT 1
  ) AS preview
FROM history h
"""


@dataclass(frozen=True)
class SegmentRecord:
    session_id: str
    seq: int
    created_at: int
    raw_text: str = ""
    polished_text: str = ""
    source: str = ""
    duration_ms: int = 0
    target_app: str = ""
    trigger_mode: str = ""
    model: str = ""
    speaker_id: int = 0
    speaker_route: str = ""
    # Wall-clock ms when the segment's speech started (0 for rows written by
    # older versions, which only know when recognition finished).
    captured_at: int = 0
    # A manual meeting correction; empty means use the untouched ASR text.
    corrected_text: str = ""


@dataclass(frozen=True)
class SessionSummary:
    session_id: str
    created_at: int
    segment_count: int
    source: str
    target_app: str
    preview: str


def _row_to_segment(r: tuple) -> SegmentRecord:
    return SegmentRecord(
        session_id=r[0],
        seq=r[1],
        created_at=r[2],
        raw_text=r[3],
        polished_text=r[4],
        source=r[5],
        duration_ms=r[6],
        target_app=r[7],
        trigger_mode=r[8],
        model=r[9],
        speaker_id=int(r[10] or 0),
        speaker_route=r[11] or "",
        captured_at=int(r[12] or 0) if len(r) > 12 else 0,
        corrected_text=(r[13] or "") if len(r) > 13 else "",
    )


def _rows_to_summaries(rows: list[tuple]) -> list[SessionSummary]:
    return [
        SessionSummary(
            session_id=r[0],
            created_at=r[1],
            segment_count=r[2],
            source=r[3] or "",
            target_app=r[4] or "",
            preview=r[5] or "",
        )
        for r in rows
    ]


DISABLED_NOTICE = "历史记录数据库无法打开，本次运行不会保存转写历史。"
RECOVERED_NOTICE = "历史记录数据库已损坏，原文件已备份为 {name}，已重新建库（之前的记录不会显示）。"
WRITE_FAILED_NOTICE = "历史记录保存失败，最近的转写可能未写入本机历史。"
DELETE_FAILED_NOTICE = "历史记录删除或清理失败，相关记录可能仍保留在本机。"


# How long a reader / writer waits on a locked database before giving up.
BUSY_TIMEOUT_MS = 5000
# Exiting waits this long for queued history writes; the writer is a daemon
# thread, so anything still pending after that is lost.
CLOSE_TIMEOUT_SEC = 10.0


def _escape_like(text: str) -> str:
    """Make ``%`` and ``_`` literal in a LIKE pattern (searching 100% or a_b)."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _is_corruption_error(exc: BaseException) -> bool:
    """SQLITE_CORRUPT / SQLITE_NOTADB surface as a bare ``sqlite3.DatabaseError``.

    Locked, read-only or unopenable files raise ``OperationalError`` instead and
    must not be quarantined: the file itself is fine.
    """
    if type(exc) is sqlite3.DatabaseError:
        return True
    msg = str(exc).lower()
    return "not a database" in msg or "malformed" in msg


class HistoryStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.disabled = False
        # Non-empty when init hit a problem the user should hear about once.
        self.startup_notice = ""
        self._queue: queue.Queue[Any] = queue.Queue()
        self._stop = object()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._init_error: BaseException | None = None
        self._committed_callbacks: list[Callable[[], None]] = []
        self._failed_callbacks: list[Callable[[str], None]] = []

        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                self._bootstrap_schema()
            except sqlite3.DatabaseError as exc:
                if not (_is_corruption_error(exc) and self.db_path.is_file()):
                    raise
                backup = self._quarantine_corrupt_db()
                if backup is None:
                    raise
                logger.warning(
                    "HistoryStore database corrupt (%s); moved to %s and recreated",
                    exc, backup.name,
                )
                self._bootstrap_schema()
                self.startup_notice = RECOVERED_NOTICE.format(name=backup.name)
        except Exception:
            logger.exception("HistoryStore init failed; disabling history")
            self._disable()
            return

        self._thread = threading.Thread(
            target=self._writer_loop, name="HistoryStoreWriter", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout=5.0):
            logger.error("HistoryStore writer thread failed to start")
            self._disable()
            return
        if self._init_error is not None:
            logger.exception(
                "HistoryStore writer connection failed; disabling history",
                exc_info=self._init_error,
            )
            self._disable()

    def _bootstrap_schema(self) -> None:
        # Bootstrap schema on a short-lived connection, then hand exclusive
        # write ownership to the daemon writer thread. The connection must be
        # closed explicitly (``with`` only commits) or Windows keeps the file
        # locked and a corrupt database could not be renamed afterwards.
        conn = sqlite3.connect(self.db_path, timeout=BUSY_TIMEOUT_MS / 1000)
        try:
            conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS};")
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.executescript(_DDL)
            _ensure_speaker_columns(conn)
            _ensure_meeting_note_columns(conn)
            conn.execute("PRAGMA user_version=1;")
            conn.commit()
        finally:
            conn.close()

    def _quarantine_corrupt_db(self) -> Path | None:
        """Rename ``history.db`` (plus WAL/SHM side files) like config does for
        a corrupt ``config.json``, so the data is kept for manual recovery."""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = self.db_path.with_name(f"{self.db_path.stem}.corrupt-{stamp}{self.db_path.suffix}")
        try:
            self.db_path.replace(backup)
        except OSError:
            logger.exception("HistoryStore could not back up corrupt database")
            return None
        for suffix in ("-wal", "-shm"):
            side = self.db_path.with_name(self.db_path.name + suffix)
            if side.exists():
                try:
                    side.replace(backup.with_name(backup.name + suffix))
                except OSError:
                    logger.warning("HistoryStore could not move %s", side.name)
        return backup

    def _disable(self) -> None:
        self.disabled = True
        self.startup_notice = DISABLED_NOTICE

    def add_committed_callback(self, callback: Callable[[], None]) -> None:
        self._committed_callbacks.append(callback)

    def add_failed_callback(self, callback: Callable[[str], None]) -> None:
        """Called from the writer thread with a user-facing message when a
        queued write could not be committed."""
        self._failed_callbacks.append(callback)

    def _notify_committed(self) -> None:
        for callback in list(self._committed_callbacks):
            try:
                callback()
            except Exception:
                logger.exception("HistoryStore committed callback failed")

    def _notify_failed(self, message: str) -> None:
        for callback in list(self._failed_callbacks):
            try:
                callback(message)
            except Exception:
                logger.exception("HistoryStore failed callback failed")

    def enqueue(self, record: SegmentRecord) -> None:
        if self.disabled:
            return
        self._queue.put(("insert", record))

    def enqueue_delete_sessions(self, session_ids: list[str]) -> None:
        if self.disabled:
            return
        self._queue.put(("delete_sessions", list(session_ids)))

    def enqueue_delete_all(self) -> None:
        if self.disabled:
            return
        self._queue.put(("delete_all", None))

    def enqueue_cleanup(
        self,
        retention_days: int,
        max_entries: int,
        active_session_id: str | None,
    ) -> None:
        if self.disabled:
            return
        self._queue.put(
            ("cleanup", (retention_days, max_entries, active_session_id))
        )

    def enqueue_write(
        self,
        fn: Callable[[sqlite3.Connection], None],
        *,
        failure_notice: str = WRITE_FAILED_NOTICE,
    ) -> None:
        """Run ``fn(conn)`` on the single writer thread (meeting tables etc.).

        ``fn`` must not commit; the writer commits after it returns and rolls
        back if it raises. Keeps the "one writer" invariant for everything
        stored in ``history.db``.
        """
        if self.disabled:
            return
        self._queue.put(("write", (fn, failure_notice)))

    def relabel_session_speakers(
        self,
        session_id: str,
        mapping: dict[int, int],
        *,
        failure_notice: str = WRITE_FAILED_NOTICE,
    ) -> None:
        """Renumber speakers of one session after a re-clustering pass
        (``{old_id: new_id}``); used by meetings and continuous dictation."""
        if not mapping or not session_id:
            return
        pairs = [(int(new), session_id, int(old)) for old, new in mapping.items() if old != new]
        if not pairs:
            return

        def _relabel(conn: sqlite3.Connection) -> None:
            # Two passes through a temporary offset so a→b, b→a does not collide.
            conn.executemany(
                "UPDATE history SET speaker_id = -(? + 1) WHERE session_id = ? AND speaker_id = ?",
                pairs,
            )
            conn.execute(
                "UPDATE history SET speaker_id = -speaker_id - 1 WHERE session_id = ? AND speaker_id < 0",
                (session_id,),
            )

        self.enqueue_write(_relabel, failure_notice=failure_notice)

    def enqueue_update_polished(
        self,
        session_id: str,
        polished_by_seq: dict[int, str],
        *,
        failure_notice: str = WRITE_FAILED_NOTICE,
    ) -> None:
        """Update ``polished_text`` for specified ``seq`` rows in ``session_id``
        without touching ``raw_text``."""
        if self.disabled or not session_id or not polished_by_seq:
            return
        rows = [(str(text), session_id, int(seq)) for seq, text in polished_by_seq.items()]

        def _update(conn: sqlite3.Connection) -> None:
            conn.executemany(
                "UPDATE history SET polished_text = ? WHERE session_id = ? AND seq = ?",
                rows,
            )

        self.enqueue_write(_update, failure_notice=failure_notice)

    def enqueue_merge_sessions(
        self,
        target_session_id: str,
        session_ids: list[str],
        *,
        failure_notice: str = WRITE_FAILED_NOTICE,
    ) -> None:
        """Merge non-meeting segments of ``session_ids`` into ``target_session_id``
        in chronological order ``(created_at ASC, seq ASC, id ASC)`` and
        resequence ``seq`` to ``0..N-1``."""
        if self.disabled or not target_session_id:
            return
        ids = list(dict.fromkeys(s for s in session_ids if s))
        if len(ids) < 2:
            return
        if target_session_id not in ids:
            ids.insert(0, target_session_id)

        def _merge(conn: sqlite3.Connection) -> None:
            placeholders = ",".join("?" for _ in ids)
            ordered_ids = [
                row[0]
                for row in conn.execute(
                    f"""
                    SELECT id FROM history
                    WHERE session_id IN ({placeholders})
                      AND trigger_mode != ?
                    ORDER BY created_at ASC, seq ASC, id ASC
                    """,
                    (*ids, MEETING_TRIGGER_MODE),
                ).fetchall()
            ]
            if not ordered_ids:
                return
            conn.executemany(
                "UPDATE history SET session_id = ?, seq = ? WHERE id = ?",
                [(target_session_id, new_seq, row_id) for new_seq, row_id in enumerate(ordered_ids)],
            )

        self.enqueue_write(_merge, failure_notice=failure_notice)

    def flush(self, timeout: float = CLOSE_TIMEOUT_SEC) -> bool:
        """Block until every write queued before this call has been applied.

        Readers use a separate connection; a caller that reads right after
        enqueueing (meeting end → render transcript) needs this barrier.
        """
        if self.disabled or self._thread is None:
            return True
        done = threading.Event()
        self._queue.put(("barrier", done))
        return done.wait(timeout=timeout)

    def read(self, fn: Callable[[sqlite3.Connection], Any], default: Any = None) -> Any:
        """Run ``fn(conn)`` on a short-lived read-only connection."""
        if self.disabled:
            return default
        try:
            conn = self._readonly_conn()
            try:
                return fn(conn)
            finally:
                conn.close()
        except Exception:
            logger.exception("HistoryStore read failed")
            return default

    def close(self, timeout: float = CLOSE_TIMEOUT_SEC) -> None:
        if self.disabled:
            return
        thread = self._thread
        if thread is None:
            return
        self._thread = None
        pending = self._queue.qsize()
        self._queue.put(self._stop)
        thread.join(timeout=timeout)
        if thread.is_alive():
            logger.warning(
                "HistoryStore writer did not finish within %.1fs (%d writes were queued); "
                "leaving connection with writer thread",
                timeout, pending,
            )

    def list_sessions(self, limit: int = 50, offset: int = 0) -> list[SessionSummary]:
        if self.disabled:
            return []
        try:
            with self._readonly_conn() as conn:
                rows = conn.execute(
                    _SESSION_SUMMARY_SQL
                    + f"""
                    WHERE h.trigger_mode != '{MEETING_TRIGGER_MODE}'
                    GROUP BY h.session_id
                    ORDER BY session_created DESC
                    LIMIT ? OFFSET ?
                    """,
                    (limit, offset),
                ).fetchall()
            return _rows_to_summaries(rows)
        except Exception:
            logger.exception("HistoryStore list_sessions failed")
            return []

    def search_sessions(self, q: str) -> list[SessionSummary]:
        if self.disabled or not q:
            return []
        pattern = f"%{_escape_like(q)}%"
        try:
            with self._readonly_conn() as conn:
                rows = conn.execute(
                    f"""
                    WITH matching AS (
                        SELECT session_id, seq,
                               CASE WHEN polished_text LIKE ? ESCAPE '\\'
                                    THEN polished_text ELSE raw_text END AS matched_text
                        FROM history
                        WHERE trigger_mode != '{MEETING_TRIGGER_MODE}'
                          AND (raw_text LIKE ? ESCAPE '\\' OR polished_text LIKE ? ESCAPE '\\')
                    )
                    SELECT summaries.session_id, summaries.session_created, summaries.segment_count,
                           summaries.source, summaries.target_app,
                           (SELECT matched_text FROM matching m
                            WHERE m.session_id = summaries.session_id ORDER BY m.seq LIMIT 1)
                    FROM (
                    """
                    + _SESSION_SUMMARY_SQL
                    + f"""
                    WHERE h.trigger_mode != '{MEETING_TRIGGER_MODE}'
                      AND h.session_id IN (SELECT session_id FROM matching)
                    GROUP BY h.session_id
                    ) summaries
                    ORDER BY session_created DESC
                    """,
                    (pattern, pattern, pattern),
                ).fetchall()
            return _rows_to_summaries(rows)
        except Exception:
            logger.exception("HistoryStore search_sessions failed")
            return []

    def count_sessions(self) -> int | None:
        """Count all dictation sessions, including those outside the loaded page."""
        if self.disabled:
            return None
        try:
            with self._readonly_conn() as conn:
                return conn.execute(
                    "SELECT COUNT(DISTINCT session_id) FROM history WHERE trigger_mode != ?",
                    (MEETING_TRIGGER_MODE,),
                ).fetchone()[0]
        except Exception:
            logger.exception("HistoryStore count_sessions failed")
            return None

    def get_session_segments(self, session_id: str) -> list[SegmentRecord]:
        if self.disabled:
            return []
        try:
            with self._readonly_conn() as conn:
                rows = conn.execute(
                    """
                    SELECT session_id, seq, created_at, raw_text, polished_text,
                           source, duration_ms, target_app, trigger_mode, model,
                           speaker_id, speaker_route, captured_at, corrected_text
                    FROM history
                    WHERE session_id = ?
                    ORDER BY seq ASC
                    """,
                    (session_id,),
                ).fetchall()
            return [_row_to_segment(r) for r in rows]
        except Exception:
            logger.exception("HistoryStore get_session_segments failed")
            return []

    def _readonly_conn(self) -> sqlite3.Connection:
        # Short-lived read-only URI connection (WAL allows concurrent readers).
        uri = self.db_path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=BUSY_TIMEOUT_MS / 1000)
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS};")
        conn.execute("PRAGMA query_only=ON;")
        return conn

    def _writer_loop(self) -> None:
        conn: sqlite3.Connection | None = None
        try:
            # Writer thread exclusively owns this connection for its lifetime.
            conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=BUSY_TIMEOUT_MS / 1000)
            conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS};")
            conn.execute("PRAGMA journal_mode=WAL;")
        except Exception as exc:
            self._init_error = exc
            self._ready.set()
            return
        self._ready.set()

        try:
            while True:
                item = self._queue.get()
                if item is self._stop:
                    while True:
                        try:
                            more = self._queue.get_nowait()
                        except queue.Empty:
                            break
                        if more is self._stop:
                            continue
                        self._dispatch(conn, more)
                    return
                self._dispatch(conn, item)
        finally:
            try:
                conn.close()
            except Exception:
                logger.exception("HistoryStore writer connection close failed")

    def _dispatch(self, conn: sqlite3.Connection, item: Any) -> None:
        op = ""
        try:
            op, payload = item
            if op == "insert":
                self._do_insert(conn, payload)
            elif op == "delete_sessions":
                self._do_delete_sessions(conn, payload)
            elif op == "delete_all":
                self._do_delete_all(conn)
            elif op == "cleanup":
                retention_days, max_entries, active_session_id = payload
                self._do_cleanup(conn, retention_days, max_entries, active_session_id)
            elif op == "barrier":
                payload.set()
                return
            elif op == "write":
                fn, notice = payload
                try:
                    fn(conn)
                    conn.commit()
                except Exception:
                    logger.exception("HistoryStore custom write failed")
                    try:
                        conn.rollback()
                    except Exception:
                        logger.exception("HistoryStore rollback after custom write failed")
                    self._notify_failed(notice)
                    return
            else:
                return
            self._notify_committed()
        except Exception:
            logger.exception("HistoryStore writer op failed")
            try:
                conn.rollback()
            except Exception:
                logger.exception("HistoryStore rollback after writer failure failed")
            self._notify_failed(
                WRITE_FAILED_NOTICE if op == "insert" else DELETE_FAILED_NOTICE
            )

    def _do_insert(self, conn: sqlite3.Connection, record: SegmentRecord) -> None:
        conn.execute(
            """
            INSERT INTO history (
              session_id, seq, created_at, raw_text, polished_text,
              source, duration_ms, target_app, trigger_mode, model,
              speaker_id, speaker_route, captured_at, corrected_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.session_id,
                record.seq,
                record.created_at,
                record.raw_text,
                record.polished_text,
                record.source,
                record.duration_ms,
                record.target_app,
                record.trigger_mode,
                record.model,
                int(record.speaker_id),
                record.speaker_route or "",
                int(record.captured_at or 0),
                record.corrected_text,
            ),
        )
        conn.commit()

    def _do_delete_sessions(self, conn: sqlite3.Connection, session_ids: list[str]) -> None:
        if not session_ids:
            return
        placeholders = ",".join("?" for _ in session_ids)
        # Meeting transcripts share this table. A history delete must not orphan
        # a meeting by removing its rows, even if a caller passes the meeting id.
        conn.execute(
            f"""
            DELETE FROM history
            WHERE session_id IN ({placeholders})
              AND trigger_mode != ?
              AND session_id NOT IN ({_PROTECTED_SESSIONS_SQL})
            """,
            (*session_ids, MEETING_TRIGGER_MODE),
        )
        conn.commit()

    def _do_delete_all(self, conn: sqlite3.Connection) -> None:
        # 「清空历史」 is the dictation history's button; meetings have their own
        # delete action on the meeting page.
        conn.execute(
            f"DELETE FROM history WHERE session_id NOT IN ({_PROTECTED_SESSIONS_SQL})"
        )
        conn.commit()

    def _do_cleanup(
        self,
        conn: sqlite3.Connection,
        retention_days: int,
        max_entries: int,
        active_session_id: str | None,
    ) -> None:
        if retention_days > 0:
            cutoff_ms = int(time.time() * 1000 - retention_days * 86_400_000)
            rows = conn.execute(
                f"""
                SELECT session_id
                FROM history
                WHERE session_id NOT IN ({_PROTECTED_SESSIONS_SQL})
                GROUP BY session_id
                HAVING MIN(created_at) < ?
                """,
                (cutoff_ms,),
            ).fetchall()
            to_delete = [r[0] for r in rows if r[0] != active_session_id]
            if to_delete:
                placeholders = ",".join("?" for _ in to_delete)
                conn.execute(
                    f"DELETE FROM history WHERE session_id IN ({placeholders})",
                    to_delete,
                )
                conn.commit()

        if max_entries > 0:
            # Delete oldest non-active sessions until within limit (ADR-0005).
            # Active session is never deleted, even if that leaves us briefly
            # unable to shrink further when only active remains.
            while True:
                rows = conn.execute(
                    f"""
                    SELECT session_id
                    FROM history
                    WHERE session_id NOT IN ({_PROTECTED_SESSIONS_SQL})
                    GROUP BY session_id
                    ORDER BY MIN(created_at) ASC
                    """
                ).fetchall()
                session_ids = [r[0] for r in rows]
                if len(session_ids) <= max_entries:
                    break
                victim = next(
                    (s for s in session_ids if s != active_session_id),
                    None,
                )
                if victim is None:
                    break
                conn.execute(
                    "DELETE FROM history WHERE session_id = ?",
                    (victim,),
                )
                conn.commit()

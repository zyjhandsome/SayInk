"""Meeting records: metadata, marks, speakers and generated minutes.

Everything lives in the same ``history.db`` as dictation history and goes
through :class:`sayink.history_store.HistoryStore`'s single writer thread
(``enqueue_write``) so there is exactly one writer for the file. The
transcript itself is ordinary ``history`` rows with ``session_id`` equal to
the meeting id and ``trigger_mode = 'meeting'``.

Design: docs/meeting-notes-design.md §5.2.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sayink.history_store import MEETING_TRIGGER_MODE, HistoryStore, SegmentRecord, _row_to_segment

logger = logging.getLogger(__name__)

MEETING_WRITE_FAILED_NOTICE = "会议记录保存失败，相关内容可能未写入本机数据库。"

STATUS_LIVE = "live"
STATUS_ENDED = "ended"
STATUS_DELETED = "deleted"
# A meeting still marked live after a restart: the app crashed mid-meeting.
STATUS_INTERRUPTED = "interrupted"


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True)
class MeetingRecord:
    id: str
    title: str = ""
    participants: str = ""
    agenda: str = ""
    notes_user: str = ""
    source: str = ""
    model_id: str = ""
    vad: str = ""
    glossary: list[dict[str, Any]] = field(default_factory=list)
    started_at: int = 0
    ended_at: int | None = None
    status: str = STATUS_LIVE
    deleted_at: int | None = None

    @property
    def duration_ms(self) -> int:
        if self.ended_at is None:
            return 0
        return max(0, int(self.ended_at) - int(self.started_at))

    @property
    def display_title(self) -> str:
        if self.title.strip():
            return self.title.strip()
        # strftime formats stay ASCII: Windows encodes them with the ANSI code page.
        return time.strftime("%m-%d %H:%M", time.localtime(self.started_at / 1000)) + " 会议"


@dataclass(frozen=True)
class MeetingMark:
    id: int
    meeting_id: str
    offset_ms: int
    note: str = ""


@dataclass(frozen=True)
class MeetingSpeaker:
    meeting_id: str
    speaker_id: int
    display_name: str = ""
    suggested_name: str = ""

    @property
    def label(self) -> str:
        return self.display_name or self.suggested_name or f"说话人{self.speaker_id}"


@dataclass(frozen=True)
class MeetingNotes:
    meeting_id: str
    template_id: str = ""
    model: str = ""
    json_payload: str = ""
    markdown: str = ""
    previous_markdown: str = ""
    user_edited: bool = False
    generated_at: int = 0
    duration_ms: int = 0
    chunked: bool = False
    transcript_changed: bool = False
    previous_transcript_changed: bool = False
    previous_metadata: str = ""

    def payload(self) -> dict[str, Any]:
        if not self.json_payload:
            return {}
        try:
            data = json.loads(self.json_payload)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}


_MEETING_COLUMNS = (
    "id, title, participants, agenda, notes_user, source, model_id, vad, glossary, "
    "started_at, ended_at, status, deleted_at"
)

_NOTE_METADATA_COLUMNS = ("template_id", "model", "json_payload", "generated_at", "duration_ms", "chunked")


def _note_metadata(conn: sqlite3.Connection, meeting_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT " + ", ".join(_NOTE_METADATA_COLUMNS) + " FROM meeting_notes WHERE meeting_id = ?", (meeting_id,)
    ).fetchone()
    return dict(zip(_NOTE_METADATA_COLUMNS, row, strict=True)) if row else {}


def _row_to_meeting(r: tuple) -> MeetingRecord:
    try:
        glossary = json.loads(r[8]) if r[8] else []
    except ValueError:
        glossary = []
    return MeetingRecord(
        id=r[0],
        title=r[1] or "",
        participants=r[2] or "",
        agenda=r[3] or "",
        notes_user=r[4] or "",
        source=r[5] or "",
        model_id=r[6] or "",
        vad=r[7] or "",
        glossary=glossary if isinstance(glossary, list) else [],
        started_at=int(r[9] or 0),
        ended_at=int(r[10]) if r[10] is not None else None,
        status=r[11] or STATUS_LIVE,
        deleted_at=int(r[12]) if r[12] is not None else None,
    )


class MeetingStore:
    """Thin API over the meeting tables; all writes go through ``history``."""

    def __init__(self, history: HistoryStore):
        self._history = history

    @property
    def disabled(self) -> bool:
        return self._history.disabled

    # -- lifecycle -------------------------------------------------------

    def _write(self, fn) -> None:
        self._history.enqueue_write(fn, failure_notice=MEETING_WRITE_FAILED_NOTICE)

    def flush(self, timeout: float = 10.0) -> bool:
        return self._history.flush(timeout=timeout)

    def create_meeting(
        self,
        *,
        title: str = "",
        participants: str = "",
        agenda: str = "",
        source: str = "",
        model_id: str = "",
        vad: str = "",
        glossary: list[dict[str, Any]] | None = None,
        started_at: int | None = None,
        meeting_id: str | None = None,
    ) -> MeetingRecord:
        record = MeetingRecord(
            id=meeting_id or uuid.uuid4().hex,
            title=title,
            participants=participants,
            agenda=agenda,
            source=source,
            model_id=model_id,
            vad=vad,
            glossary=list(glossary or []),
            started_at=now_ms() if started_at is None else int(started_at),
            status=STATUS_LIVE,
        )

        def _insert(conn: sqlite3.Connection) -> None:
            conn.execute(
                f"INSERT INTO meetings ({_MEETING_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.id, record.title, record.participants, record.agenda,
                    record.notes_user, record.source, record.model_id, record.vad,
                    json.dumps(record.glossary, ensure_ascii=False),
                    record.started_at, None, record.status, None,
                ),
            )

        self._write(_insert)
        return record

    def create_meeting_from_sessions(
        self,
        session_ids: list[str],
        *,
        title: str = "",
        meeting_id: str | None = None,
    ) -> MeetingRecord | None:
        """Promote one or more dictation history sessions into a new ended meeting
        by copying their segments in chronological order (leaving the original
        dictation history untouched)."""
        if self.disabled:
            return None
        ids = list(dict.fromkeys(s for s in session_ids if s))
        if not ids:
            return None

        segments: list[SegmentRecord] = []
        for sid in ids:
            segments.extend(self._history.get_session_segments(sid))
        if not segments:
            return None
        segments.sort(key=lambda s: (int(s.created_at), int(s.seq)))

        started_candidates = [int(s.captured_at) if int(s.captured_at) > 0 else int(s.created_at) for s in segments]
        started_at = min(started_candidates)
        ended_at = max(int(s.created_at) for s in segments)
        if ended_at < started_at:
            ended_at = started_at

        source = next((s.source for s in segments if s.source), "")
        model_id = next((s.model for s in segments if s.model), "")
        stamp_str = time.strftime("%m-%d %H:%M", time.localtime(started_at / 1000))
        if not title.strip():
            title = f"{stamp_str} 听写整理" if len(ids) == 1 else f"{stamp_str} 等 {len(ids)} 条听写整理"

        record = MeetingRecord(
            id=meeting_id or uuid.uuid4().hex,
            title=title.strip(),
            source=source,
            model_id=model_id,
            started_at=started_at,
            ended_at=ended_at,
            status=STATUS_ENDED,
        )

        def _promote(conn: sqlite3.Connection) -> None:
            conn.execute(
                f"INSERT INTO meetings ({_MEETING_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.id, record.title, record.participants, record.agenda,
                    record.notes_user, record.source, record.model_id, record.vad,
                    json.dumps(record.glossary, ensure_ascii=False),
                    record.started_at, record.ended_at, record.status, None,
                ),
            )
            conn.executemany(
                """
                INSERT INTO history (
                  session_id, seq, created_at, raw_text, polished_text,
                  source, duration_ms, target_app, trigger_mode, model,
                  speaker_id, speaker_route, captured_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        record.id,
                        new_seq,
                        seg.created_at,
                        seg.raw_text,
                        "" if seg.polished_text == "\u200b" else seg.polished_text,
                        seg.source,
                        seg.duration_ms,
                        seg.target_app,
                        MEETING_TRIGGER_MODE,
                        seg.model,
                        int(seg.speaker_id),
                        seg.speaker_route or "",
                        int(seg.captured_at or 0),
                    )
                    for new_seq, seg in enumerate(
                        s for s in segments
                        if (("" if s.polished_text == "\u200b" else s.polished_text) or ("" if s.polished_text == "\u200b" else s.raw_text)).strip()
                    )
                ],
            )

        self._write(_promote)
        return record

    def end_meeting(
        self,
        meeting_id: str,
        ended_at: int | None = None,
        *,
        status: str = STATUS_ENDED,
    ) -> None:
        stamp = now_ms() if ended_at is None else int(ended_at)
        if status not in (STATUS_ENDED, STATUS_INTERRUPTED):
            status = STATUS_ENDED

        def _end(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE meetings SET ended_at = ?, status = ? WHERE id = ? AND status != ?",
                (stamp, status, meeting_id, STATUS_DELETED),
            )

        self._write(_end)

    def mark_interrupted_meetings(self) -> None:
        """Startup: any meeting still ``live`` did not end cleanly."""

        def _fix(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                UPDATE meetings
                SET status = ?,
                    ended_at = COALESCE(
                        ended_at,
                        (SELECT MAX(created_at) FROM history WHERE history.session_id = meetings.id),
                        started_at
                    )
                WHERE status = ?
                """,
                (STATUS_INTERRUPTED, STATUS_LIVE),
            )

        self._write(_fix)

    def update_meeting(
        self,
        meeting_id: str,
        *,
        title: str | None = None,
        participants: str | None = None,
        agenda: str | None = None,
        notes_user: str | None = None,
    ) -> None:
        fields: dict[str, str] = {}
        if title is not None:
            fields["title"] = title
        if participants is not None:
            fields["participants"] = participants
        if agenda is not None:
            fields["agenda"] = agenda
        if notes_user is not None:
            fields["notes_user"] = notes_user
        if not fields:
            return
        assignments = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [meeting_id]

        def _update(conn: sqlite3.Connection) -> None:
            conn.execute(f"UPDATE meetings SET {assignments} WHERE id = ?", values)

        self._write(_update)

    def delete_meeting(self, meeting_id: str) -> None:
        """Hard-delete the meeting: metadata, transcript rows, marks, speakers, notes."""

        def _delete(conn: sqlite3.Connection) -> None:
            conn.execute("DELETE FROM history WHERE session_id = ?", (meeting_id,))
            conn.execute("DELETE FROM meeting_marks WHERE meeting_id = ?", (meeting_id,))
            conn.execute("DELETE FROM meeting_speakers WHERE meeting_id = ?", (meeting_id,))
            conn.execute("DELETE FROM meeting_notes WHERE meeting_id = ?", (meeting_id,))
            conn.execute("DELETE FROM meeting_generation_cache WHERE meeting_id = ?", (meeting_id,))
            conn.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))

        self._write(_delete)

    # -- marks / speakers --------------------------------------------------

    def add_mark(self, meeting_id: str, offset_ms: int, note: str = "") -> None:
        def _insert(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO meeting_marks (meeting_id, offset_ms, note) VALUES (?, ?, ?)",
                (meeting_id, int(offset_ms), note),
            )

        self._write(_insert)

    def set_speaker(
        self,
        meeting_id: str,
        speaker_id: int,
        *,
        display_name: str | None = None,
        suggested_name: str | None = None,
    ) -> None:
        def _upsert(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO meeting_speakers (meeting_id, speaker_id, display_name, suggested_name)
                VALUES (?, ?, COALESCE(?, ''), COALESCE(?, ''))
                ON CONFLICT(meeting_id, speaker_id) DO UPDATE SET
                  display_name = COALESCE(?, display_name),
                  suggested_name = COALESCE(?, suggested_name)
                """,
                (
                    meeting_id, int(speaker_id), display_name, suggested_name,
                    display_name, suggested_name,
                ),
            )

        self._write(_upsert)

    def relabel_segments(self, meeting_id: str, mapping: dict[int, int]) -> None:
        """Apply a post-meeting re-clustering (old speaker_id → new speaker_id).

        Transcript rows live in ``history``; the relabel itself is shared with
        dictation sessions (:meth:`HistoryStore.relabel_session_speakers`)."""
        self._history.relabel_session_speakers(
            meeting_id, mapping, failure_notice=MEETING_WRITE_FAILED_NOTICE
        )

    def set_segment_speaker(self, meeting_id: str, seq: int, speaker_id: int) -> None:
        """Correct one completed meeting segment, and mark its notes for review."""
        def _update(conn: sqlite3.Connection) -> None:
            changed = conn.execute(
                "UPDATE history SET speaker_id = ? "
                "WHERE session_id = ? AND seq = ? AND trigger_mode = ? AND speaker_id != ? "
                "AND EXISTS (SELECT 1 FROM meetings WHERE id = ? AND status IN (?, ?))",
                (int(speaker_id), meeting_id, int(seq), MEETING_TRIGGER_MODE, int(speaker_id),
                 meeting_id, STATUS_ENDED, STATUS_INTERRUPTED),
            )
            if changed.rowcount:
                conn.execute("UPDATE meeting_notes SET transcript_changed = 1 WHERE meeting_id = ?", (meeting_id,))

        self._write(_update)

    # -- notes -------------------------------------------------------------

    def generation_checkpoints(self, meeting_id: str, fingerprint: str) -> dict[str, str]:
        def _query(conn):
            return dict(conn.execute(
                "SELECT step_key, payload FROM meeting_generation_cache WHERE meeting_id = ? AND fingerprint = ?",
                (meeting_id, fingerprint),
            ).fetchall())
        return self._history.read(_query, default={}) or {}

    def save_generation_checkpoint(self, meeting_id: str, fingerprint: str, step_key: str, payload: str) -> None:
        saved = []
        def _save(conn):
            # A deleted meeting must never be resurrected by an old worker.
            row = conn.execute("SELECT status FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
            if row is None or row[0] == STATUS_DELETED:
                saved.append(False)
                return
            conn.execute("DELETE FROM meeting_generation_cache WHERE meeting_id = ? AND fingerprint != ?",
                         (meeting_id, fingerprint))
            conn.execute("INSERT OR REPLACE INTO meeting_generation_cache VALUES (?, ?, ?, ?)",
                         (meeting_id, fingerprint, step_key, payload))
            saved.append(True)
        self._write(_save)
        if not self.flush(timeout=2.0):
            raise RuntimeError("分段进度未能保存，请重试")
        if not saved or (saved[0] and self.generation_checkpoints(meeting_id, fingerprint).get(step_key) != payload):
            raise RuntimeError("分段进度保存失败，请重试")

    def clear_generation_checkpoints(self, meeting_id: str) -> None:
        self._write(lambda conn: conn.execute("DELETE FROM meeting_generation_cache WHERE meeting_id = ?", (meeting_id,)))

    def save_notes(
        self,
        meeting_id: str,
        *,
        template_id: str,
        model: str,
        json_payload: str,
        markdown: str,
        duration_ms: int = 0,
        chunked: bool = False,
        generated_at: int | None = None,
        keep_previous: bool = False,
    ) -> None:
        """Upsert the notes. The replaced markdown becomes ``previous_markdown``
        unless ``keep_previous`` (a re-render of the same version, e.g. after a
        speaker rename, must not push the real previous version out)."""
        stamp = now_ms() if generated_at is None else int(generated_at)

        def _upsert(conn: sqlite3.Connection) -> None:
            previous_metadata = json.dumps(_note_metadata(conn, meeting_id), ensure_ascii=False)
            conn.execute(
                """
                INSERT INTO meeting_notes (
                  meeting_id, template_id, model, json_payload, markdown,
                  previous_markdown, user_edited, generated_at, duration_ms, chunked
                ) VALUES (?, ?, ?, ?, ?, '', 0, ?, ?, ?)
                ON CONFLICT(meeting_id) DO UPDATE SET
                  previous_markdown = CASE WHEN ? THEN meeting_notes.previous_markdown
                                           ELSE meeting_notes.markdown END,
                  previous_transcript_changed = CASE WHEN ? THEN meeting_notes.previous_transcript_changed
                                                     ELSE meeting_notes.transcript_changed END,
                  previous_metadata = CASE WHEN ? THEN meeting_notes.previous_metadata ELSE ? END,
                  transcript_changed = CASE WHEN ? THEN meeting_notes.transcript_changed ELSE 0 END,
                  template_id = excluded.template_id,
                  model = excluded.model,
                  json_payload = excluded.json_payload,
                  markdown = excluded.markdown,
                  user_edited = 0,
                  generated_at = excluded.generated_at,
                  duration_ms = excluded.duration_ms,
                  chunked = excluded.chunked
                """,
                (
                    meeting_id, template_id, model, json_payload, markdown,
                    stamp, int(duration_ms), 1 if chunked else 0,
                    1 if keep_previous else 0,
                    1 if keep_previous else 0,
                    1 if keep_previous else 0, previous_metadata,
                    1 if keep_previous else 0,
                ),
            )

        self._write(_upsert)

    def restore_previous_notes(self, meeting_id: str) -> None:
        """Swap the current and previous markdown, so restoring twice is a no-op.

        The restored text no longer matches ``json_payload``; it is flagged as
        user-edited so speaker renames patch the text instead of re-rendering.
        """

        def _swap(conn: sqlite3.Connection) -> None:
            row = conn.execute(
                "SELECT previous_metadata FROM meeting_notes WHERE meeting_id = ?", (meeting_id,)
            ).fetchone()
            if row is None:
                return
            current = _note_metadata(conn, meeting_id)
            try:
                previous = json.loads(row[0] or "{}")
            except (ValueError, TypeError):
                previous = {}
            if not isinstance(previous, dict):
                previous = {}
            restored = {key: previous.get(key, current[key]) for key in _NOTE_METADATA_COLUMNS}
            conn.execute(
                """
                UPDATE meeting_notes
                SET markdown = previous_markdown, previous_markdown = markdown, user_edited = 1,
                    transcript_changed = previous_transcript_changed,
                    previous_transcript_changed = transcript_changed,
                    template_id = ?, model = ?, json_payload = ?, generated_at = ?, duration_ms = ?, chunked = ?,
                    previous_metadata = ?
                WHERE meeting_id = ? AND previous_markdown != ''
                """,
                (*restored.values(), json.dumps(current, ensure_ascii=False), meeting_id),
            )

        self._write(_swap)

    def save_user_markdown(self, meeting_id: str, markdown: str) -> None:
        """The user edited the rendered notes; keep the edit, mark it as such."""

        def _update(conn: sqlite3.Connection) -> None:
            cur = conn.execute(
                "UPDATE meeting_notes SET markdown = ?, user_edited = 1 WHERE meeting_id = ?",
                (markdown, meeting_id),
            )
            if cur.rowcount == 0:
                conn.execute(
                    """
                    INSERT INTO meeting_notes (meeting_id, markdown, user_edited, generated_at)
                    VALUES (?, ?, 1, ?)
                    """,
                    (meeting_id, markdown, now_ms()),
                )

        self._write(_update)

    # -- reads -------------------------------------------------------------

    def list_meetings(self, limit: int = 100, offset: int = 0) -> list[MeetingRecord]:
        def _q(conn: sqlite3.Connection) -> list[MeetingRecord]:
            rows = conn.execute(
                f"""
                SELECT {_MEETING_COLUMNS} FROM meetings
                WHERE status != ?
                ORDER BY started_at DESC
                LIMIT ? OFFSET ?
                """,
                (STATUS_DELETED, limit, offset),
            ).fetchall()
            return [_row_to_meeting(r) for r in rows]

        return self._history.read(_q, default=[]) or []

    def search_meetings(self, q: str) -> list[MeetingRecord]:
        q = q.strip()
        if not q:
            return self.list_meetings()
        from sayink.history_store import _escape_like

        pattern = f"%{_escape_like(q)}%"

        def _q(conn: sqlite3.Connection) -> list[MeetingRecord]:
            rows = conn.execute(
                f"""
                SELECT {_MEETING_COLUMNS} FROM meetings m
                WHERE m.status != ?
                  AND (
                    m.title LIKE ? ESCAPE '\\' OR m.participants LIKE ? ESCAPE '\\' OR m.agenda LIKE ? ESCAPE '\\'
                    OR m.id IN (
                      SELECT session_id FROM history
                      WHERE raw_text LIKE ? ESCAPE '\\' OR polished_text LIKE ? ESCAPE '\\'
                    )
                    OR m.id IN (SELECT meeting_id FROM meeting_notes WHERE markdown LIKE ? ESCAPE '\\')
                  )
                ORDER BY m.started_at DESC
                """,
                (STATUS_DELETED, pattern, pattern, pattern, pattern, pattern, pattern),
            ).fetchall()
            return [_row_to_meeting(r) for r in rows]

        return self._history.read(_q, default=[]) or []

    def get_meeting(self, meeting_id: str) -> MeetingRecord | None:
        def _q(conn: sqlite3.Connection) -> MeetingRecord | None:
            row = conn.execute(
                f"SELECT {_MEETING_COLUMNS} FROM meetings WHERE id = ?", (meeting_id,)
            ).fetchone()
            return _row_to_meeting(row) if row else None

        return self._history.read(_q, default=None)

    def get_segments(self, meeting_id: str) -> list[SegmentRecord]:
        def _q(conn: sqlite3.Connection) -> list[SegmentRecord]:
            rows = conn.execute(
                """
                SELECT session_id, seq, created_at, raw_text, polished_text,
                       source, duration_ms, target_app, trigger_mode, model,
                       speaker_id, speaker_route, captured_at
                FROM history
                WHERE session_id = ? AND trigger_mode = ?
                ORDER BY seq ASC
                """,
                (meeting_id, MEETING_TRIGGER_MODE),
            ).fetchall()
            return [_row_to_segment(r) for r in rows]

        return self._history.read(_q, default=[]) or []

    def get_marks(self, meeting_id: str) -> list[MeetingMark]:
        def _q(conn: sqlite3.Connection) -> list[MeetingMark]:
            rows = conn.execute(
                "SELECT id, meeting_id, offset_ms, note FROM meeting_marks "
                "WHERE meeting_id = ? ORDER BY offset_ms ASC",
                (meeting_id,),
            ).fetchall()
            return [MeetingMark(int(r[0]), r[1], int(r[2]), r[3] or "") for r in rows]

        return self._history.read(_q, default=[]) or []

    def get_speakers(self, meeting_id: str) -> list[MeetingSpeaker]:
        def _q(conn: sqlite3.Connection) -> list[MeetingSpeaker]:
            rows = conn.execute(
                "SELECT meeting_id, speaker_id, display_name, suggested_name FROM meeting_speakers "
                "WHERE meeting_id = ? ORDER BY speaker_id ASC",
                (meeting_id,),
            ).fetchall()
            return [MeetingSpeaker(r[0], int(r[1]), r[2] or "", r[3] or "") for r in rows]

        return self._history.read(_q, default=[]) or []

    def get_notes(self, meeting_id: str) -> MeetingNotes | None:
        def _q(conn: sqlite3.Connection) -> MeetingNotes | None:
            r = conn.execute(
                """
                SELECT meeting_id, template_id, model, json_payload, markdown,
                       previous_markdown, user_edited, generated_at, duration_ms, chunked,
                       transcript_changed, previous_transcript_changed, previous_metadata
                FROM meeting_notes WHERE meeting_id = ?
                """,
                (meeting_id,),
            ).fetchone()
            if not r:
                return None
            return MeetingNotes(
                meeting_id=r[0],
                template_id=r[1] or "",
                model=r[2] or "",
                json_payload=r[3] or "",
                markdown=r[4] or "",
                previous_markdown=r[5] or "",
                user_edited=bool(r[6]),
                generated_at=int(r[7] or 0),
                duration_ms=int(r[8] or 0),
                chunked=bool(r[9]),
                transcript_changed=bool(r[10]),
                previous_transcript_changed=bool(r[11]),
                previous_metadata=r[12] or "",
            )

        return self._history.read(_q, default=None)

    def segment_count(self, meeting_id: str) -> int:
        def _q(conn: sqlite3.Connection) -> int:
            row = conn.execute(
                "SELECT COUNT(*) FROM history WHERE session_id = ?", (meeting_id,)
            ).fetchone()
            return int(row[0]) if row else 0

        return int(self._history.read(_q, default=0) or 0)

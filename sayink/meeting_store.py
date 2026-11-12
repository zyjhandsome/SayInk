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
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sayink.history_store import MEETING_TRIGGER_MODE, HistoryStore, SegmentRecord, escape_like, row_to_segment
from sayink.media_import import MEDIA_SOURCE
from sayink.transcript_import import IMPORT_SOURCE, ImportedTranscript, assign_speakers

logger = logging.getLogger(__name__)

MEETING_WRITE_FAILED_NOTICE = "会议记录保存失败，相关内容可能未写入本机数据库。"

STATUS_LIVE = "live"
STATUS_ENDED = "ended"
STATUS_DELETED = "deleted"
# A meeting still marked live after a restart: the app crashed mid-meeting.
STATUS_INTERRUPTED = "interrupted"

# Minutes queue: user requests run before automatic end-of-meeting jobs.
QUEUE_PRIORITY_AUTO = 0
QUEUE_PRIORITY_USER = 1
QUEUE_PENDING = "pending"
# Needs the user (key, quota, unconfirmed host…); never retried on its own.
QUEUE_FAILED = "failed"


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
    audio_path: str = ""

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
    name_locked: bool = False

    @property
    def label(self) -> str:
        return self.display_name or (self.suggested_name if not self.name_locked else "") or f"说话人{self.speaker_id}"


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
    user_markdown: str = ""
    user_metadata: str = ""
    user_transcript_changed: bool = False

    @property
    def has_user_backup(self) -> bool:
        return bool(self.user_markdown)

    def payload(self) -> dict[str, Any]:
        if not self.json_payload:
            return {}
        try:
            data = json.loads(self.json_payload)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}


@dataclass(frozen=True)
class QueuedGeneration:
    meeting_id: str
    template_id: str = ""
    priority: int = QUEUE_PRIORITY_AUTO
    enqueued_at: int = 0
    attempts: int = 0
    not_before: int = 0
    state: str = QUEUE_PENDING
    last_error: str = ""

    @property
    def automatic(self) -> bool:
        return self.priority == QUEUE_PRIORITY_AUTO

    @property
    def failed(self) -> bool:
        return self.state == QUEUE_FAILED


_QUEUE_COLUMNS = "meeting_id, template_id, priority, enqueued_at, attempts, not_before, state, last_error"


def _row_to_queued(r: tuple) -> QueuedGeneration:
    return QueuedGeneration(
        meeting_id=r[0], template_id=r[1] or "", priority=int(r[2] or 0), enqueued_at=int(r[3] or 0),
        attempts=int(r[4] or 0), not_before=int(r[5] or 0), state=r[6] or QUEUE_PENDING, last_error=r[7] or "",
    )


_MEETING_COLUMNS = (
    "id, title, participants, agenda, notes_user, source, model_id, vad, glossary, "
    "started_at, ended_at, status, deleted_at, audio_path"
)

_NOTE_METADATA_COLUMNS = ("template_id", "model", "json_payload", "generated_at", "duration_ms", "chunked", "user_edited")
_OWNED_AUDIO_SUFFIXES = frozenset({
    ".wav", ".f32", ".pcm", ".raw", ".ogg", ".opus", ".m4a", ".mp3", ".flac", ".aac", ".webm", ".tmp",
})
_CACHE_DIR_NAMES = frozenset({
    "audio-recovery",
    "meeting-audio",
    "meeting_audio",
    "recordings",
    "audio_cache",
    "sayink_audio_cache",
    "cache",
})


def _cleanup_owned_meeting_audio(db_path: Path, meeting_id: str, source: str, audio_path: str) -> None:
    """Best-effort removal of SayInk-owned cached audio for a deleted meeting.

    External user media/transcript files and symlinks are never touched.
    """
    try:
        db_resolved = db_path.resolve()
        db_root = db_resolved.parent
    except OSError:
        return

    allowed_roots: list[Path] = [db_root]
    temp_root = Path(tempfile.gettempdir())
    for extra in (Path.home() / ".sayink", temp_root / "SayInk", temp_root / "sayink"):
        try:
            allowed_roots.append(extra.resolve())
        except OSError:
            continue

    raw = (audio_path or "").strip()
    if raw:
        try:
            candidate = Path(raw)
            if not candidate.is_symlink():
                resolved = candidate.resolve()
                in_allowed = any(
                    resolved != root and resolved.is_relative_to(root)
                    for root in allowed_roots
                )
                in_named_cache = any(part.lower() in _CACHE_DIR_NAMES for part in resolved.parts)
                # Never delete user-imported media files unless explicitly stored in an owned cache folder.
                owned_source = source not in (MEDIA_SOURCE, IMPORT_SOURCE) or in_named_cache
                if in_allowed and owned_source and resolved != db_resolved:
                    if candidate.is_file() and candidate.suffix.lower() in _OWNED_AUDIO_SUFFIXES:
                        candidate.unlink(missing_ok=True)
                    elif candidate.is_dir():
                        markers = ("audio.f32", "microphone.f32", "system.f32", "session.json")
                        if any((candidate / m).exists() for m in markers):
                            shutil.rmtree(candidate, ignore_errors=True)
        except OSError:
            pass

    recovery_root = db_root / "audio-recovery"
    try:
        if recovery_root.is_dir() and not recovery_root.is_symlink():
            for child in recovery_root.iterdir():
                if not child.is_dir() or child.is_symlink() or len(child.name) != 32:
                    continue
                meta_file = child / "session.json"
                if not meta_file.is_file() or meta_file.is_symlink():
                    continue
                try:
                    meta = json.loads(meta_file.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if isinstance(meta, dict) and (
                    meta.get("meeting_id") == meeting_id or meta.get("session_id") == meeting_id
                ):
                    shutil.rmtree(child, ignore_errors=True)
    except OSError:
        pass


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
        audio_path=(r[13] or "") if len(r) > 13 else "",
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
        audio_path: str = "",
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
            audio_path=str(audio_path or ""),
        )

        def _insert(conn: sqlite3.Connection) -> None:
            conn.execute(
                f"INSERT INTO meetings ({_MEETING_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.id, record.title, record.participants, record.agenda,
                    record.notes_user, record.source, record.model_id, record.vad,
                    json.dumps(record.glossary, ensure_ascii=False),
                    record.started_at, None, record.status, None, record.audio_path,
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
            title = f"{stamp_str} 转写整理" if len(ids) == 1 else f"{stamp_str} 等 {len(ids)} 条转写整理"

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
                f"INSERT INTO meetings ({_MEETING_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.id, record.title, record.participants, record.agenda,
                    record.notes_user, record.source, record.model_id, record.vad,
                    json.dumps(record.glossary, ensure_ascii=False),
                    record.started_at, record.ended_at, record.status, None, record.audio_path,
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

    def create_meeting_from_transcript(
        self,
        transcript: ImportedTranscript,
        *,
        title: str = "",
        meeting_id: str | None = None,
    ) -> MeetingRecord | None:
        """Create an ended meeting from parsed text. One write covers rows and names.

        Timestamps are stored as ``captured_at``. A transcript without them
        still uses ``captured_at = started_at`` so the old offset fallback
        cannot invent a clock from ``created_at``. ``ended_at`` stays null
        in that case, which is how the UI knows not to show a duration.
        """
        if self.disabled or transcript is None:
            return None
        assigned, names = assign_speakers(tuple(turn for turn in transcript.turns if turn.text.strip()))
        if not assigned:
            return None
        started = now_ms()
        if transcript.has_timestamps:
            span = 0
            for _speaker_id, turn in assigned:
                if turn.offset_ms is None:
                    continue
                span = max(span, int(turn.offset_ms) + int(turn.duration_ms or 0))
            ended: int | None = started + span
        else:
            ended = None
        meeting_title = title.strip() if title and title.strip() else transcript.title.strip()
        record = MeetingRecord(
            id=meeting_id or uuid.uuid4().hex,
            title=meeting_title,
            source=IMPORT_SOURCE,
            started_at=started,
            ended_at=ended,
            status=STATUS_ENDED,
        )

        def _insert(conn: sqlite3.Connection) -> None:
            conn.execute(
                f"INSERT INTO meetings ({_MEETING_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.id, record.title, record.participants, record.agenda,
                    record.notes_user, record.source, record.model_id, record.vad,
                    json.dumps(record.glossary, ensure_ascii=False),
                    record.started_at, record.ended_at, record.status, None, record.audio_path,
                ),
            )
            rows = []
            for seq, (speaker_id, turn) in enumerate(assigned):
                if transcript.has_timestamps and turn.offset_ms is not None:
                    captured = started + int(turn.offset_ms)
                    duration = int(turn.duration_ms or 0)
                elif transcript.has_timestamps:
                    # Negative captured_at means this paragraph has no clock.
                    # Zero would fall back to created_at and invent one.
                    captured = -1
                    duration = 0
                else:
                    captured = started
                    duration = 0
                rows.append((
                    record.id, seq, started + seq, turn.text.strip(), "", IMPORT_SOURCE, duration, "",
                    MEETING_TRIGGER_MODE, "", int(speaker_id), "", captured,
                ))
            conn.executemany(
                """
                INSERT INTO history (
                  session_id, seq, created_at, raw_text, polished_text,
                  source, duration_ms, target_app, trigger_mode, model,
                  speaker_id, speaker_route, captured_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            if names:
                conn.executemany(
                    """
                    INSERT INTO meeting_speakers
                      (meeting_id, speaker_id, display_name, suggested_name, name_locked)
                    VALUES (?, ?, ?, '', 1)
                    """,
                    [(record.id, speaker_id, display_name) for speaker_id, display_name in names.items()],
                )

        self._write(_insert)
        return record

    def create_meeting_from_media(
        self,
        turns,
        *,
        title: str = "",
        status: str = STATUS_ENDED,
        audio_ms: int = 0,
        model_id: str = "",
        vad: str = "",
        glossary: list[dict[str, Any]] | None = None,
        meeting_id: str | None = None,
        audio_path: str = "",
    ) -> MeetingRecord | None:
        """Save recognized file turns. The source media itself is not stored.

        A cancelled import is ``interrupted`` and its duration stops at the
        last kept turn. A finished import can extend that to the file length.
        """
        usable = [turn for turn in turns or [] if str(getattr(turn, "text", "") or "").strip()]
        if self.disabled or not usable:
            return None
        if status not in (STATUS_ENDED, STATUS_INTERRUPTED):
            status = STATUS_ENDED
        started = now_ms()
        span = 0
        for turn in usable:
            span = max(span, int(turn.offset_ms) + int(turn.duration_ms or 0))
        if status == STATUS_ENDED:
            span = max(span, int(audio_ms or 0))
        record = MeetingRecord(
            id=meeting_id or uuid.uuid4().hex,
            title=title.strip(),
            source=MEDIA_SOURCE,
            model_id=model_id,
            vad=vad,
            glossary=list(glossary or []),
            started_at=started,
            ended_at=started + span,
            status=status,
            audio_path=str(audio_path or ""),
        )

        def _insert(conn: sqlite3.Connection) -> None:
            if conn.execute("SELECT 1 FROM meetings WHERE id = ?", (record.id,)).fetchone() is not None:
                return
            conn.execute(
                f"INSERT INTO meetings ({_MEETING_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.id, record.title, record.participants, record.agenda,
                    record.notes_user, record.source, record.model_id, record.vad,
                    json.dumps(record.glossary, ensure_ascii=False),
                    record.started_at, record.ended_at, record.status, None, record.audio_path,
                ),
            )
            rows = []
            for seq, turn in enumerate(usable):
                rows.append((
                    record.id, seq, started + seq, str(turn.text).strip(), "", MEDIA_SOURCE,
                    int(turn.duration_ms or 0), "", MEETING_TRIGGER_MODE, model_id,
                    int(turn.speaker_id), "", started + int(turn.offset_ms),
                ))
            conn.executemany(
                """
                INSERT INTO history (
                  session_id, seq, created_at, raw_text, polished_text,
                  source, duration_ms, target_app, trigger_mode, model,
                  speaker_id, speaker_route, captured_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

        self._write(_insert)
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
                    ended_at = MAX(
                        started_at,
                        COALESCE(
                            ended_at,
                            (
                                SELECT MAX(
                                    CASE
                                        WHEN captured_at > 0 THEN captured_at + CASE WHEN duration_ms > 0 THEN duration_ms ELSE 0 END
                                        ELSE created_at
                                    END
                                )
                                FROM history
                                WHERE history.session_id = meetings.id AND history.trigger_mode = ?
                            ),
                            started_at
                        )
                    )
                WHERE status = ?
                """,
                (STATUS_INTERRUPTED, MEETING_TRIGGER_MODE, STATUS_LIVE),
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
        audio_path: str | None = None,
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
        if audio_path is not None:
            fields["audio_path"] = audio_path
        if not fields:
            return
        assignments = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [meeting_id]

        def _update(conn: sqlite3.Connection) -> None:
            conn.execute(f"UPDATE meetings SET {assignments} WHERE id = ?", values)

        self._write(_update)

    def delete_meeting(self, meeting_id: str) -> None:
        """Hard-delete the meeting: metadata, transcript rows, marks, speakers, notes, and owned cached audio."""
        db_path = self._history.db_path

        def _delete(conn: sqlite3.Connection) -> None:
            row = conn.execute("SELECT source, audio_path FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
            conn.execute("DELETE FROM history WHERE session_id = ?", (meeting_id,))
            conn.execute("DELETE FROM meeting_marks WHERE meeting_id = ?", (meeting_id,))
            conn.execute("DELETE FROM meeting_speakers WHERE meeting_id = ?", (meeting_id,))
            conn.execute("DELETE FROM meeting_notes WHERE meeting_id = ?", (meeting_id,))
            conn.execute("DELETE FROM meeting_generation_cache WHERE meeting_id = ?", (meeting_id,))
            conn.execute("DELETE FROM meeting_generation_queue WHERE meeting_id = ?", (meeting_id,))
            conn.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
            source = str(row[0] or "") if row else ""
            audio_path = str(row[1] or "") if row else ""
            _cleanup_owned_meeting_audio(db_path, meeting_id, source, audio_path)

        self._write(_delete)

    def delete_meetings(self, meeting_ids: list[str]) -> None:
        """Hard-delete every id in one writer transaction.

        A per-meeting delete would commit and pump the minutes queue between
        rows, so removing one meeting could start generation of the next.
        """
        ids = list(dict.fromkeys(meeting_id for meeting_id in meeting_ids if meeting_id))
        if not ids or self.disabled:
            return
        db_path = self._history.db_path

        def _delete_all(conn: sqlite3.Connection) -> None:
            audio_rows: list[tuple[str, str, str]] = []
            for meeting_id in ids:
                row = conn.execute("SELECT source, audio_path FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
                if row is not None:
                    audio_rows.append((meeting_id, str(row[0] or ""), str(row[1] or "")))
                else:
                    audio_rows.append((meeting_id, "", ""))
                conn.execute("DELETE FROM history WHERE session_id = ?", (meeting_id,))
                conn.execute("DELETE FROM meeting_marks WHERE meeting_id = ?", (meeting_id,))
                conn.execute("DELETE FROM meeting_speakers WHERE meeting_id = ?", (meeting_id,))
                conn.execute("DELETE FROM meeting_notes WHERE meeting_id = ?", (meeting_id,))
                conn.execute("DELETE FROM meeting_generation_cache WHERE meeting_id = ?", (meeting_id,))
                conn.execute("DELETE FROM meeting_generation_queue WHERE meeting_id = ?", (meeting_id,))
                conn.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
            for meeting_id, source, audio_path in audio_rows:
                _cleanup_owned_meeting_audio(db_path, meeting_id, source, audio_path)

        self._write(_delete_all)

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
                INSERT INTO meeting_speakers (meeting_id, speaker_id, display_name, suggested_name, name_locked)
                VALUES (?, ?, COALESCE(?, ''), COALESCE(?, ''), COALESCE(?, 0))
                ON CONFLICT(meeting_id, speaker_id) DO UPDATE SET
                  display_name = COALESCE(?, display_name),
                  suggested_name = COALESCE(?, suggested_name),
                  name_locked = COALESCE(?, name_locked)
                """,
                (
                    meeting_id, int(speaker_id), display_name, suggested_name,
                    1 if display_name is not None else None,
                    display_name, suggested_name, 1 if display_name is not None else None,
                ),
            )

        self._write(_upsert)

    def save_inferred_names(self, meeting_id: str, names: dict[int, str], *, markdown: str | None = None) -> None:
        """Commit validated automatic labels and their rendered notes together.

        Manual names (including an explicitly restored number) are protected.
        Renaming keeps notes metadata, edits and the previous version intact.
        """
        def _save(conn: sqlite3.Connection) -> None:
            row = conn.execute("SELECT status FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
            if row is None or row[0] not in (STATUS_ENDED, STATUS_INTERRUPTED):
                return
            conn.execute(
                "UPDATE meeting_speakers SET suggested_name = '' WHERE meeting_id = ? "
                "AND display_name = '' AND name_locked = 0", (meeting_id,),
            )
            for sid, name in names.items():
                conn.execute(
                    "INSERT INTO meeting_speakers (meeting_id, speaker_id, suggested_name) VALUES (?, ?, ?) "
                    "ON CONFLICT(meeting_id, speaker_id) DO UPDATE SET suggested_name = excluded.suggested_name "
                    "WHERE meeting_speakers.display_name = '' AND meeting_speakers.name_locked = 0",
                    (meeting_id, sid, name),
                )
            if markdown is not None:
                conn.execute(
                    "UPDATE meeting_notes SET markdown = ?, "
                    "user_markdown = CASE WHEN user_edited = 1 THEN ? ELSE user_markdown END "
                    "WHERE meeting_id = ?",
                    (markdown, markdown, meeting_id),
                )
        self._write(_save)

    def relabel_segments(self, meeting_id: str, mapping: dict[int, int]) -> None:
        """Recluster automatic meeting assignments; preserve manual corrections.

        Same SQL as continuous dictation (one implementation in the history
        store), restricted to this meeting's rows and to unlocked segments."""
        self._history.relabel_session_speakers(
            meeting_id, mapping, trigger_mode=MEETING_TRIGGER_MODE, respect_locks=True,
            failure_notice=MEETING_WRITE_FAILED_NOTICE,
        )

    def set_segment_speakers(self, meeting_id: str, assignments: dict[int, int]) -> None:
        """Apply an end-of-meeting re-assignment ``{seq: speaker_id}``.

        Automatic numbers only: a segment the user locked keeps its speaker.
        """
        self._history.relabel_session_segments(
            meeting_id, assignments, trigger_mode=MEETING_TRIGGER_MODE,
            failure_notice=MEETING_WRITE_FAILED_NOTICE,
        )

    def set_segment_speaker(self, meeting_id: str, seq: int, speaker_id: int) -> None:
        """Lock one already-transcribed segment to the user's chosen speaker."""
        def _update(conn: sqlite3.Connection) -> None:
            changed = conn.execute(
                "UPDATE history SET speaker_id = ?, speaker_locked = 1 "
                "WHERE session_id = ? AND seq = ? AND trigger_mode = ? "
                "AND (speaker_id != ? OR speaker_locked = 0) "
                "AND EXISTS (SELECT 1 FROM meetings WHERE id = ? AND status IN (?, ?, ?))",
                (int(speaker_id), meeting_id, int(seq), MEETING_TRIGGER_MODE, int(speaker_id),
                 meeting_id, STATUS_LIVE, STATUS_ENDED, STATUS_INTERRUPTED),
            )
            if changed.rowcount:
                conn.execute(
                    "UPDATE meeting_notes SET transcript_changed = 1, "
                    "previous_transcript_changed = CASE WHEN previous_markdown != '' THEN 1 "
                    "ELSE previous_transcript_changed END, "
                    "user_transcript_changed = CASE WHEN user_markdown != '' THEN 1 "
                    "ELSE user_transcript_changed END "
                    "WHERE meeting_id = ?",
                    (meeting_id,),
                )

        self._write(_update)

    # -- notes -------------------------------------------------------------

    def set_segment_text(self, meeting_id: str, seq: int, text: str | None) -> None:
        """Save a manual override, or clear it to restore the original transcript."""
        if text is not None and not text.strip():
            raise ValueError("文字稿内容不能为空")
        corrected = text.strip() if text is not None else ""

        def _update(conn: sqlite3.Connection) -> None:
            changed = conn.execute(
                "UPDATE history SET corrected_text = ? "
                "WHERE session_id = ? AND seq = ? AND trigger_mode = ? AND corrected_text != ? "
                "AND EXISTS (SELECT 1 FROM meetings WHERE id = ? AND status IN (?, ?, ?))",
                (corrected, meeting_id, int(seq), MEETING_TRIGGER_MODE, corrected,
                 meeting_id, STATUS_LIVE, STATUS_ENDED, STATUS_INTERRUPTED),
            )
            if changed.rowcount:
                conn.execute(
                    "UPDATE meeting_notes SET transcript_changed = 1, "
                    "previous_transcript_changed = CASE WHEN previous_markdown != '' THEN 1 "
                    "ELSE previous_transcript_changed END, "
                    "user_transcript_changed = CASE WHEN user_markdown != '' THEN 1 "
                    "ELSE user_transcript_changed END "
                    "WHERE meeting_id = ?",
                    (meeting_id,),
                )

        self._write(_update)

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

    # -- minutes queue -----------------------------------------------------

    def enqueue_generation(self, meeting_id: str, template_id: str, *, priority: int,
                           enqueued_at: int | None = None) -> None:
        """Queue an ended meeting. A user request replaces any earlier job for
        the meeting (failed ones included); an automatic job never displaces
        an existing one."""
        stamp = now_ms() if enqueued_at is None else int(enqueued_at)
        user = priority != QUEUE_PRIORITY_AUTO

        def _enqueue(conn: sqlite3.Connection) -> None:
            row = conn.execute("SELECT status FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
            if row is None or row[0] not in (STATUS_ENDED, STATUS_INTERRUPTED):
                return
            conn.execute(
                f"INSERT INTO meeting_generation_queue ({_QUEUE_COLUMNS}) VALUES (?, ?, ?, ?, 0, 0, ?, '') "
                + ("ON CONFLICT(meeting_id) DO UPDATE SET template_id = excluded.template_id, "
                   "priority = excluded.priority, enqueued_at = excluded.enqueued_at, attempts = 0, "
                   "not_before = 0, state = excluded.state, last_error = ''" if user else
                   "ON CONFLICT(meeting_id) DO NOTHING"),
                (meeting_id, template_id, int(priority), stamp, QUEUE_PENDING),
            )

        self._write(_enqueue)

    def reschedule_generation(self, meeting_id: str, *, attempts: int, not_before: int, last_error: str) -> None:
        self._write(lambda conn: conn.execute(
            "UPDATE meeting_generation_queue SET attempts = ?, not_before = ?, last_error = ?, state = ? "
            "WHERE meeting_id = ?", (int(attempts), int(not_before), last_error, QUEUE_PENDING, meeting_id),
        ))

    def fail_generation(self, meeting_id: str, last_error: str) -> None:
        self._write(lambda conn: conn.execute(
            "UPDATE meeting_generation_queue SET state = ?, last_error = ? WHERE meeting_id = ?",
            (QUEUE_FAILED, last_error, meeting_id),
        ))

    def dequeue_generation(self, meeting_id: str) -> None:
        self._write(lambda conn: conn.execute(
            "DELETE FROM meeting_generation_queue WHERE meeting_id = ?", (meeting_id,)
        ))

    def generation_queue(self) -> list[QueuedGeneration]:
        """Every queued job, in the order they should run (failed rows included)."""
        def _q(conn: sqlite3.Connection) -> list[QueuedGeneration]:
            rows = conn.execute(
                f"SELECT {_QUEUE_COLUMNS} FROM meeting_generation_queue "
                "ORDER BY priority DESC, enqueued_at ASC, meeting_id ASC"
            ).fetchall()
            return [_row_to_queued(r) for r in rows]

        return self._history.read(_q, default=[]) or []

    def queued_generation(self, meeting_id: str) -> QueuedGeneration | None:
        def _q(conn: sqlite3.Connection) -> QueuedGeneration | None:
            row = conn.execute(
                f"SELECT {_QUEUE_COLUMNS} FROM meeting_generation_queue WHERE meeting_id = ?", (meeting_id,)
            ).fetchone()
            return _row_to_queued(row) if row else None

        return self._history.read(_q, default=None)

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
        user_markdown: str | None = None,
    ) -> None:
        """Upsert the notes. The replaced markdown becomes ``previous_markdown``
        unless ``keep_previous`` (a re-render of the same version, e.g. after a
        speaker rename, must not push the real previous version out) or a
        protected hand-edited backup already sits in ``previous_markdown``."""
        stamp = now_ms() if generated_at is None else int(generated_at)

        def _upsert(conn: sqlite3.Connection) -> None:
            existing_row = conn.execute(
                "SELECT markdown, previous_markdown, user_edited, transcript_changed, "
                "previous_transcript_changed, previous_metadata, user_markdown "
                "FROM meeting_notes WHERE meeting_id = ?",
                (meeting_id,),
            ).fetchone()
            preserve_prev = bool(keep_previous)
            if not preserve_prev and existing_row is not None:
                ex_user_edited = bool(existing_row[2])
                ex_prev_md = existing_row[1] or ""
                ex_user_md = existing_row[6] or ""
                if not ex_user_edited and ex_user_md and ex_prev_md:
                    try:
                        prev_meta = json.loads(existing_row[5] or "{}")
                    except (ValueError, TypeError):
                        prev_meta = {}
                    if isinstance(prev_meta, dict) and prev_meta.get("user_edited"):
                        # Keep the user's hand-edited draft in previous_markdown across
                        # multiple consecutive template trials so 恢复上一版 never loses it.
                        preserve_prev = True
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
                  user_markdown = COALESCE(?, meeting_notes.user_markdown),
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
                    1 if preserve_prev else 0,
                    1 if preserve_prev else 0,
                    1 if preserve_prev else 0, previous_metadata,
                    1 if keep_previous else 0,
                    user_markdown,
                ),
            )

        self._write(_upsert)

    def restore_previous_notes(self, meeting_id: str) -> None:
        """Swap the current and previous markdown, so restoring twice is a no-op.

        The metadata (template, payload, and whether that version was hand
        edited) travels with each version, so a restored AI version reads
        「AI 生成」 again and can be re-rendered on a speaker rename. Rows
        written before the flag was recorded are treated as edited, which
        makes renames patch the text instead of re-rendering it.
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
            restored["user_edited"] = 1 if previous.get("user_edited", 1) else 0
            conn.execute(
                """
                UPDATE meeting_notes
                SET markdown = previous_markdown, previous_markdown = markdown,
                    transcript_changed = previous_transcript_changed,
                    previous_transcript_changed = transcript_changed,
                    template_id = ?, model = ?, json_payload = ?, generated_at = ?, duration_ms = ?, chunked = ?,
                    user_edited = ?, previous_metadata = ?
                WHERE meeting_id = ? AND previous_markdown != ''
                """,
                (*restored.values(), json.dumps(current, ensure_ascii=False), meeting_id),
            )

        self._write(_swap)

    def save_user_markdown(self, meeting_id: str, markdown: str, *, keep_backup: bool = False) -> None:
        """The user edited the rendered notes; keep the edit, mark it as such,
        and back up an unbacked-up initial AI draft before overwriting it."""

        def _update(conn: sqlite3.Connection) -> None:
            row = conn.execute(
                "SELECT markdown, previous_markdown, user_edited, transcript_changed "
                "FROM meeting_notes WHERE meeting_id = ?",
                (meeting_id,),
            ).fetchone()
            stamp = now_ms()
            if row is None:
                user_meta = json.dumps(
                    {
                        "template_id": "", "model": "", "json_payload": "",
                        "generated_at": stamp, "duration_ms": 0, "chunked": 0, "user_edited": 1,
                    },
                    ensure_ascii=False,
                )
                conn.execute(
                    """
                    INSERT INTO meeting_notes (
                      meeting_id, markdown, user_edited, generated_at,
                      user_markdown, user_metadata, user_transcript_changed
                    ) VALUES (?, ?, 1, ?, ?, ?, 0)
                    """,
                    (meeting_id, markdown, stamp, markdown, user_meta),
                )
                return

            ex_markdown, ex_prev_markdown, ex_user_edited, ex_tx_changed = (
                row[0] or "", row[1] or "", bool(row[2]), int(row[3] or 0)
            )
            current_meta = _note_metadata(conn, meeting_id)
            user_meta = json.dumps({**current_meta, "user_edited": 1}, ensure_ascii=False)
            if not keep_backup and not ex_prev_markdown and ex_markdown and not ex_user_edited:
                conn.execute(
                    """
                    UPDATE meeting_notes
                    SET previous_markdown = ?,
                        previous_metadata = ?,
                        previous_transcript_changed = ?,
                        markdown = ?,
                        user_edited = 1,
                        user_markdown = ?,
                        user_metadata = ?,
                        user_transcript_changed = ?
                    WHERE meeting_id = ?
                    """,
                    (
                        ex_markdown,
                        json.dumps(current_meta, ensure_ascii=False),
                        ex_tx_changed,
                        markdown,
                        markdown,
                        user_meta,
                        ex_tx_changed,
                        meeting_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE meeting_notes
                    SET markdown = ?,
                        user_edited = 1,
                        user_markdown = ?,
                        user_metadata = ?,
                        user_transcript_changed = ?
                    WHERE meeting_id = ?
                    """,
                    (markdown, markdown, user_meta, ex_tx_changed, meeting_id),
                )

        self._write(_update)

    def restore_user_markdown(self, meeting_id: str) -> None:
        """Restore the dedicated hand-edited minutes slot regardless of how many
        AI template regenerations occurred afterwards."""

        def _restore(conn: sqlite3.Connection) -> None:
            row = conn.execute(
                "SELECT markdown, user_edited, transcript_changed, "
                "user_markdown, user_metadata, user_transcript_changed "
                "FROM meeting_notes WHERE meeting_id = ?",
                (meeting_id,),
            ).fetchone()
            if row is None or not (row[3] or ""):
                return
            cur_md, cur_edited, cur_tx_changed, user_md, raw_user_meta, user_tx_changed = (
                row[0] or "", bool(row[1]), int(row[2] or 0),
                row[3] or "", row[4] or "", int(row[5] or 0),
            )
            if cur_edited and cur_md == user_md:
                return
            current_meta = _note_metadata(conn, meeting_id)
            try:
                parsed_user_meta = json.loads(raw_user_meta or "{}")
            except (ValueError, TypeError):
                parsed_user_meta = {}
            if not isinstance(parsed_user_meta, dict):
                parsed_user_meta = {}
            restored = {key: parsed_user_meta.get(key, current_meta.get(key)) for key in _NOTE_METADATA_COLUMNS}
            restored["user_edited"] = 1
            if cur_md:
                conn.execute(
                    """
                    UPDATE meeting_notes
                    SET previous_markdown = ?,
                        previous_transcript_changed = ?,
                        previous_metadata = ?,
                        markdown = ?,
                        transcript_changed = ?,
                        template_id = ?, model = ?, json_payload = ?, generated_at = ?, duration_ms = ?, chunked = ?,
                        user_edited = ?
                    WHERE meeting_id = ?
                    """,
                    (
                        cur_md,
                        cur_tx_changed,
                        json.dumps(current_meta, ensure_ascii=False),
                        user_md,
                        user_tx_changed,
                        *restored.values(),
                        meeting_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE meeting_notes
                    SET markdown = ?,
                        transcript_changed = ?,
                        template_id = ?, model = ?, json_payload = ?, generated_at = ?, duration_ms = ?, chunked = ?,
                        user_edited = ?
                    WHERE meeting_id = ?
                    """,
                    (user_md, user_tx_changed, *restored.values(), meeting_id),
                )

        self._write(_restore)

    restore_user_notes = restore_user_markdown

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

    def count_meetings(self) -> int:
        """Meetings the default list can show, excluding deleted rows."""

        def _q(conn: sqlite3.Connection) -> int:
            row = conn.execute(
                "SELECT COUNT(*) FROM meetings WHERE status != ?",
                (STATUS_DELETED,),
            ).fetchone()
            return int(row[0]) if row else 0

        return int(self._history.read(_q, default=0) or 0)

    def search_meetings(self, q: str, limit: int = 200) -> list[MeetingRecord]:
        q = q.strip()
        if not q:
            return self.list_meetings(limit=limit)
        if limit <= 0:
            return []

        pattern = f"%{escape_like(q)}%"

        def _q(conn: sqlite3.Connection) -> list[MeetingRecord]:
            rows = conn.execute(
                f"""
                SELECT {_MEETING_COLUMNS} FROM meetings m
                WHERE m.status != ?
                  AND (
                    m.title LIKE ? ESCAPE '\\' OR m.participants LIKE ? ESCAPE '\\' OR m.agenda LIKE ? ESCAPE '\\'
                    OR m.notes_user LIKE ? ESCAPE '\\'
                    OR m.id IN (
                      SELECT session_id FROM history
                      WHERE trigger_mode = ?
                        AND polished_text != '\u200b'
                        AND (
                          corrected_text LIKE ? ESCAPE '\\'
                          OR (
                            corrected_text = ''
                            AND (raw_text LIKE ? ESCAPE '\\' OR polished_text LIKE ? ESCAPE '\\')
                          )
                        )
                    )
                    OR m.id IN (SELECT meeting_id FROM meeting_notes WHERE markdown LIKE ? ESCAPE '\\')
                    OR m.id IN (SELECT meeting_id FROM meeting_marks WHERE note LIKE ? ESCAPE '\\')
                    OR m.id IN (
                      SELECT meeting_id FROM meeting_speakers
                      WHERE display_name LIKE ? ESCAPE '\\'
                         OR (display_name = '' AND name_locked = 0 AND suggested_name LIKE ? ESCAPE '\\')
                    )
                  )
                ORDER BY m.started_at DESC
                LIMIT ?
                """,
                (
                    STATUS_DELETED,
                    pattern, pattern, pattern, pattern,
                    MEETING_TRIGGER_MODE, pattern, pattern, pattern,
                    pattern, pattern,
                    pattern, pattern,
                    int(limit),
                ),
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
                       speaker_id, speaker_route, captured_at, corrected_text, speaker_locked
                FROM history
                WHERE session_id = ? AND trigger_mode = ?
                ORDER BY seq ASC
                """,
                (meeting_id, MEETING_TRIGGER_MODE),
            ).fetchall()
            return [row_to_segment(r) for r in rows]

        return self._history.read(_q, default=[]) or []

    def get_segment(self, meeting_id: str, seq: int) -> SegmentRecord | None:
        def _q(conn: sqlite3.Connection) -> SegmentRecord | None:
            row = conn.execute(
                """
                SELECT session_id, seq, created_at, raw_text, polished_text,
                       source, duration_ms, target_app, trigger_mode, model,
                       speaker_id, speaker_route, captured_at, corrected_text, speaker_locked
                FROM history
                WHERE session_id = ? AND seq = ? AND trigger_mode = ?
                LIMIT 1
                """,
                (meeting_id, int(seq), MEETING_TRIGGER_MODE),
            ).fetchone()
            return row_to_segment(row) if row else None

        return self._history.read(_q, default=None)

    def has_speaker(self, meeting_id: str, speaker_id: int) -> bool:
        def _q(conn: sqlite3.Connection) -> bool:
            row = conn.execute(
                "SELECT 1 FROM history WHERE session_id = ? AND trigger_mode = ? AND speaker_id = ? LIMIT 1",
                (meeting_id, MEETING_TRIGGER_MODE, int(speaker_id)),
            ).fetchone()
            return row is not None

        return bool(self._history.read(_q, default=False))

    def speaker_ids_in_segments(self, meeting_id: str) -> set[int]:
        def _q(conn: sqlite3.Connection) -> set[int]:
            rows = conn.execute(
                "SELECT DISTINCT speaker_id FROM history WHERE session_id = ? AND trigger_mode = ?",
                (meeting_id, MEETING_TRIGGER_MODE),
            ).fetchall()
            return {int(r[0]) for r in rows if r[0] is not None}

        return self._history.read(_q, default=set()) or set()

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
                "SELECT meeting_id, speaker_id, display_name, suggested_name, name_locked FROM meeting_speakers "
                "WHERE meeting_id = ? ORDER BY speaker_id ASC",
                (meeting_id,),
            ).fetchall()
            return [MeetingSpeaker(r[0], int(r[1]), r[2] or "", r[3] or "", bool(r[4])) for r in rows]

        return self._history.read(_q, default=[]) or []

    def get_notes(self, meeting_id: str) -> MeetingNotes | None:
        def _q(conn: sqlite3.Connection) -> MeetingNotes | None:
            r = conn.execute(
                """
                SELECT meeting_id, template_id, model, json_payload, markdown,
                       previous_markdown, user_edited, generated_at, duration_ms, chunked,
                       transcript_changed, previous_transcript_changed, previous_metadata,
                       user_markdown, user_metadata, user_transcript_changed
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
                user_markdown=r[13] or "",
                user_metadata=r[14] or "",
                user_transcript_changed=bool(r[15]),
            )

        return self._history.read(_q, default=None)

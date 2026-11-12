"""Draft, dirty-tracking, stashed live-note, and optimistic revision state for MeetingPage."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Callable

from sayink.meeting_store import MeetingRecord


@dataclass
class MeetingDraftBuffer:
    """Cohesive draft & optimistic revision state decoupled from Qt widgets."""

    editing_notes: bool = False
    pending_templates: dict[str, str] = field(default_factory=dict)
    pending_title: tuple[str, str] | None = None
    title_save_token: int = 0
    title_meeting_id: str = ""
    user_markdown_backups: dict[str, str] = field(default_factory=dict)
    record_notes_meeting_id: str = ""
    record_notes_dirty: bool = False
    record_notes_save_token: int = 0
    unsaved_live_notes: dict[str, str] = field(default_factory=dict)
    record_marks_html: str | None = None
    live_marks_html: str | None = None
    live_html: str = ""
    live_meeting_id: str = ""
    live_segment_count: int = 0

    # ── optimistic templates ──────────────────────────────────────

    def set_pending_template(self, meeting_id: str, template_id: str) -> None:
        if meeting_id:
            self.pending_templates[meeting_id] = template_id

    def pop_pending_template(self, meeting_id: str) -> str | None:
        return self.pending_templates.pop(meeting_id, None)

    def effective_template_id(self, meeting_id: str, default_template: str) -> str:
        return self.pending_templates.get(meeting_id, default_template)

    # ── optimistic title revisions ────────────────────────────────

    def apply_optimistic_titles(self, records: list[MeetingRecord]) -> list[MeetingRecord]:
        pending = self.pending_title
        if not pending:
            return records
        meeting_id, title = pending
        return [
            replace(record, title=title)
            if record.id == meeting_id and record.title != title
            else record
            for record in records
        ]

    def clear_pending_title_for(self, meeting_id: str) -> None:
        if self.pending_title and self.pending_title[0] == meeting_id:
            self.pending_title = None

    def begin_title_save(self, meeting_id: str, title: str) -> int:
        self.pending_title = (meeting_id, title)
        self.title_save_token += 1
        return self.title_save_token

    def matches_pending_title(self, meeting_id: str, title: str, token: int) -> bool:
        return token == self.title_save_token and self.pending_title == (meeting_id, title)

    @staticmethod
    def enqueue_tracked(history, write_action: Callable[[], None]) -> bool:
        """Run ``write_action`` and report whether it scheduled a background DB write."""
        enqueued = False
        if history is not None and hasattr(history, "enqueue_write"):
            orig_enqueue = history.enqueue_write

            def _tracked_enqueue(fn, **kwargs):
                nonlocal enqueued
                enqueued = True
                return orig_enqueue(fn, **kwargs)

            history.enqueue_write = _tracked_enqueue
            try:
                write_action()
            finally:
                history.enqueue_write = orig_enqueue
        else:
            write_action()
        return enqueued

    # ── live view cache ───────────────────────────────────────────

    def enter_live_session(self, meeting_id: str) -> bool:
        """Reset cached live HTML when switching into a different live meeting."""
        if meeting_id == self.live_meeting_id:
            return False
        self.live_meeting_id = meeting_id
        self.live_html = ""
        self.live_marks_html = None
        self.live_segment_count = 0
        return True

    # ── post-meeting record notes & stashed live notes ────────────

    def mark_record_notes_edited(self) -> None:
        self.record_notes_dirty = True

    def reset_record_notes(self) -> None:
        self.record_marks_html = None
        self.record_notes_meeting_id = ""
        self.record_notes_dirty = False

    def has_unsaved_record_notes(self, store, current_text: str) -> bool:
        meeting_id = self.record_notes_meeting_id
        if not self.record_notes_dirty or not meeting_id or store is None:
            return False
        record = store.get_meeting(meeting_id)
        return record is not None and (record.notes_user or "") != current_text

    def pop_promotable_live_note(self, store, meeting_id: str, current_text: str) -> str | None:
        """Return a stashed live note for ``meeting_id`` when safe to load into the editor."""
        if meeting_id not in self.unsaved_live_notes:
            return None
        if self.has_unsaved_record_notes(store, current_text):
            return None
        text = self.unsaved_live_notes.pop(meeting_id)
        self.record_notes_meeting_id = meeting_id
        self.record_notes_dirty = True
        return text

    def stash_live_note_for_other_meeting(self, store, meeting_id: str, text: str) -> None:
        record = store.get_meeting(meeting_id) if store is not None else None
        if record is None or (record.notes_user or "") != text:
            self.unsaved_live_notes[meeting_id] = text

    def adopt_live_note(self, meeting_id: str) -> None:
        self.unsaved_live_notes.pop(meeting_id, None)
        self.record_notes_meeting_id = meeting_id
        self.record_notes_dirty = True

    def begin_record_notes_autosave(self) -> int:
        self.record_notes_save_token += 1
        return self.record_notes_save_token

    def settle_record_notes_write(
        self,
        store,
        meeting_id: str,
        text: str,
        token: int,
        current_editor_text: str,
    ) -> bool:
        """Return False when the background autosave failed to persist ``text``."""
        if token != self.record_notes_save_token or self.record_notes_meeting_id != meeting_id:
            return True
        saved = store.get_meeting(meeting_id) if store is not None else None
        if saved is None or (saved.notes_user or "") != text:
            return False
        if current_editor_text == text:
            self.record_notes_dirty = False
        return True

    def flush_stashed_live_notes(
        self,
        store,
        update_meeting: Callable[..., None],
    ) -> bool:
        if not self.unsaved_live_notes:
            return True
        if store is None:
            self.unsaved_live_notes.clear()
            return True
        for meeting_id, text in list(self.unsaved_live_notes.items()):
            record = store.get_meeting(meeting_id)
            if record is None or (record.notes_user or "") == text:
                self.unsaved_live_notes.pop(meeting_id, None)
                continue
            update_meeting(meeting_id, notes_user=text)
            saved = store.get_meeting(meeting_id) if store.flush() else None
            if saved is None or (saved.notes_user or "") != text:
                return False
            self.unsaved_live_notes.pop(meeting_id, None)
        return True

    @staticmethod
    def flush_live_user_notes(
        store,
        meeting_id: str,
        text: str,
        update_meeting: Callable[..., None],
    ) -> bool:
        """Persist live scratchpad notes at a session/navigation boundary.

        Skips ``store.flush()`` when SQLite already holds ``text``, avoiding an
        unnecessary synchronous UI-thread wait on unrelated background writes.
        """
        for _attempt in (1, 2):
            record = store.get_meeting(meeting_id)
            if record is None:
                return True
            if (record.notes_user or "") == text:
                return True
            update_meeting(meeting_id, notes_user=text)
            if not store.flush():
                continue
            saved = store.get_meeting(meeting_id)
            if saved is not None and (saved.notes_user or "") == text:
                return True
        return False


__all__ = ["MeetingDraftBuffer"]

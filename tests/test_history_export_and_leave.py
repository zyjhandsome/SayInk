"""Unit tests for sayink.ui.history_export and HistoryWindow.try_leave() navigation guard."""

from __future__ import annotations

import sys

import pytest
from PyQt6.QtWidgets import QApplication

from sayink.config import Config
from sayink.history_store import HistoryStore, SegmentRecord, SessionSummary
from sayink.ui import history_export, history_window
from sayink.ui.history_export import (
    _SUPPRESSED_POLISHED_MARKER,
    build_batch_export_markdown,
    build_single_session_markdown,
    build_single_session_plain,
    history_shows_speakers,
    suggest_batch_export_filename,
    suggest_single_export_filename,
)
from sayink.ui.history_window import HistoryWindow
from sayink.ui.main_window import MainWindow


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication(sys.argv)


class _FakeHistoryStore:
    def __init__(self) -> None:
        self.disabled = False
        self.flush_result = True
        self.sessions = [
            SessionSummary(
                session_id="s-two",
                created_at=1_700_000_300_000,
                segment_count=2,
                source="mixed",
                target_app="Code.exe",
                preview="first preview",
            ),
            SessionSummary(
                session_id="s-one",
                created_at=1_700_000_000_000,
                segment_count=1,
                source="mic",
                target_app="Notepad.exe",
                preview="second preview",
            ),
        ]
        self.segments = {
            "s-two": [
                SegmentRecord(
                    "s-two", 0, 1_700_000_300_000, "第一段原文", "第一段润色",
                    "mixed", 1100, "Code.exe", "continuous", "funasr-nano",
                ),
                SegmentRecord(
                    "s-two", 1, 1_700_000_302_000, "第二段原文", "",
                    "mixed", 900, "Code.exe", "continuous", "funasr-nano",
                ),
            ],
            "s-one": [
                SegmentRecord(
                    "s-one", 0, 1_700_000_000_000, "单段原文", "",
                    "mic", 500, "Notepad.exe", "hotkey", "tiny",
                ),
            ],
        }
        self.updated_polished: list[tuple[str, dict[int, str]]] = []

    def list_sessions(self, limit: int = 50, offset: int = 0) -> list[SessionSummary]:
        return self.sessions[offset : offset + limit]

    def search_sessions(self, q: str, limit: int | None = None) -> list[SessionSummary]:
        return [s for s in self.sessions if q in s.preview][:limit]

    def get_session_segments(self, session_id: str) -> list[SegmentRecord]:
        return list(self.segments.get(session_id, []))

    def enqueue_update_polished(self, session_id: str, polished_by_seq: dict[int, str]) -> None:
        self.updated_polished.append((session_id, dict(polished_by_seq)))
        self.segments[session_id] = [
            SegmentRecord(
                s.session_id,
                s.seq,
                s.created_at,
                s.raw_text,
                polished_by_seq.get(s.seq, s.polished_text),
                s.source,
                s.duration_ms,
                s.target_app,
                s.trigger_mode,
                s.model,
                s.speaker_id,
                s.speaker_route,
                s.captured_at,
            )
            for s in self.segments.get(session_id, [])
        ]

    def flush(self, timeout: float = 2.0) -> bool:
        return self.flush_result


def test_history_window_reexports_stateless_export_helpers():
    assert history_window.build_single_session_plain is history_export.build_single_session_plain
    assert history_window.build_single_session_markdown is history_export.build_single_session_markdown
    assert history_window.build_batch_export_markdown is history_export.build_batch_export_markdown
    assert history_window.suggest_single_export_filename is history_export.suggest_single_export_filename
    assert history_window.suggest_batch_export_filename is history_export.suggest_batch_export_filename


def test_history_export_single_and_batch_builders_handle_speakers_and_suppressed_markers():
    summary = SessionSummary(
        session_id="sess:1",
        created_at=1_700_000_000_000,
        segment_count=2,
        source="mixed",
        target_app="Notes\nPro.exe",
        preview="preview",
    )
    segments = [
        SegmentRecord(
            "sess:1", 1, 1_700_000_005_000, "远端发言", _SUPPRESSED_POLISHED_MARKER,
            "mixed", 1200, "Notes\nPro.exe", "continuous", "funasr-nano",
            speaker_id=2, speaker_route="system", captured_at=1_700_000_004_000,
        ),
        SegmentRecord(
            "sess:1", 0, 1_700_000_001_000, "本端原始", "本端润色",
            "mixed", 800, "Notes\nPro.exe", "continuous", "funasr-nano",
            speaker_id=1, speaker_route="mic", captured_at=1_700_000_000_000,
        ),
    ]
    assert history_shows_speakers(segments) is True

    plain = build_single_session_plain(summary, segments)
    assert "说话人 1 · 麦克风" in plain
    assert "本端润色" in plain
    # Segment 1 has _SUPPRESSED_POLISHED_MARKER, so its text is omitted from exported output.
    assert "远端发言" not in plain

    md = build_single_session_markdown(summary, segments)
    assert md.startswith("---\n")
    assert "source: mixed" in md
    assert "target_app: Notes Pro.exe" in md
    assert "duration_ms: 2000" in md
    assert "model: funasr-nano" in md

    batch_md = build_batch_export_markdown([summary], {"sess:1": segments})
    assert "session_count: 1" in batch_md
    assert "· preview" in batch_md
    assert suggest_single_export_filename(summary).startswith("sayink-")
    assert suggest_single_export_filename(summary).endswith(".md")
    assert suggest_batch_export_filename(now_ms=1_700_000_100_000).startswith("sayink-export-")
    assert suggest_batch_export_filename(now_ms=1_700_000_100_000).endswith(".md")


def test_history_try_leave_closes_untouched_and_saves_valid_inline_edits(qapp):
    store = _FakeHistoryStore()
    window = HistoryWindow(store)
    try:
        assert window.try_leave() is True

        # Untouched inline editor closes without writing to store.
        window._start_inline_edit()
        assert window._editing_session is True
        assert window.try_leave() is True
        assert window._editing_session is False
        assert window._details.isReadOnly() is True
        assert store.updated_polished == []

        # Valid modified inline edit persists and exits edit mode on leave.
        window._start_inline_edit()
        window._details.setPlainText("修改后的第一段\n\n修改后的第二段")
        assert window.try_leave() is True
        assert window._editing_session is False
        assert store.updated_polished == [
            ("s-two", {0: "修改后的第一段", 1: "修改后的第二段"})
        ]
        assert "已保存修改" in window._feedback_label.text()
    finally:
        window.close()


def test_history_try_leave_blocks_unmappable_or_failed_writes_preserving_draft(qapp):
    store = _FakeHistoryStore()
    window = HistoryWindow(store)
    try:
        # Unmappable paragraph count (> segment count) blocks leaving and preserves draft.
        window._start_inline_edit()
        draft = "第一段\n\n第二段\n\n多出来的第三段"
        window._details.setPlainText(draft)
        assert window.try_leave() is False
        assert window._editing_session is True
        assert window._details.toPlainText() == draft
        assert "空行比段落多" in window._feedback_label.text()
        assert store.updated_polished == []

        # Fix paragraph count, but simulate a failed store flush -> still blocks leaving.
        valid_draft = "可映射第一段\n\n可映射第二段"
        window._details.setPlainText(valid_draft)
        store.flush_result = False
        assert window.try_leave() is False
        assert window._editing_session is True
        assert window._details.toPlainText() == valid_draft
        assert "未能保存修改" in window._feedback_label.text()

        # Once store flush succeeds, try_leave commits and allows navigation.
        store.flush_result = True
        assert window.try_leave() is True
        assert window._editing_session is False
    finally:
        window.close()


def test_main_window_show_page_guards_unsaved_history_inline_edit(qapp, tmp_path, monkeypatch):
    from sayink.ui.settings_window import SettingsWindow

    for attr in ("_rebuild_model_cards", "_refresh_about_info", "_refresh_audio_device_lists"):
        if hasattr(SettingsWindow, attr):
            monkeypatch.setattr(SettingsWindow, attr, lambda self: None)

    store = HistoryStore(tmp_path / "history.db")
    config = Config(config_dir=tmp_path)
    store.enqueue(
        SegmentRecord(
            "nav-sess", 0, 1_700_000_000_000, "原第一段", "",
            "mic", 800, "Code.exe", "continuous", "funasr-nano",
        )
    )
    store.enqueue(
        SegmentRecord(
            "nav-sess", 1, 1_700_000_002_000, "原第二段", "",
            "mic", 900, "Code.exe", "continuous", "funasr-nano",
        )
    )
    assert store.flush()

    win = MainWindow(config, store)
    try:
        win.show_page("history")
        assert win.current_page() == "history"
        history_panel = win.history_panel
        history_panel._start_inline_edit()
        assert history_panel._editing_session is True

        # Unmappable edit blocks switching away from History and restores sidebar selection.
        bad_draft = "段一\n\n段二\n\n多余段三"
        history_panel._details.setPlainText(bad_draft)
        win._nav_buttons[2].click()  # Attempt switching to "general"
        assert win.current_page() == "history"
        assert win._nav_buttons[0].isChecked() is True
        assert win._nav_buttons[2].isChecked() is False
        assert history_panel._editing_session is True
        assert history_panel._details.toPlainText() == bad_draft

        # Valid edit saves cleanly and allows switching to "general".
        history_panel._details.setPlainText("已保存段一\n\n已保存段二")
        win.show_page("general")
        assert win.current_page() == "general"
        assert win._nav_buttons[2].isChecked() is True
        assert history_panel._editing_session is False
        saved = store.get_session_segments("nav-sess")
        by_seq = {seg.seq: seg.polished_text for seg in saved}
        assert by_seq == {0: "已保存段一", 1: "已保存段二"}
    finally:
        win.close()
        store.close()

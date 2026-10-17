"""History window UI and Markdown export behavior."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QCheckBox, QDialog, QFileDialog, QMessageBox, QPushButton

from sayink.history_store import SegmentRecord, SessionSummary
from sayink.app import App
from sayink.ui.history_window import (
    HistoryWindow,
    build_batch_export_markdown,
    build_single_session_markdown,
    suggest_batch_export_filename,
    suggest_single_export_filename,
)
from sayink.ui.tray_icon import TrayIcon


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication(sys.argv)


class FakeHistoryStore:
    def __init__(self) -> None:
        self.sessions = [
            SessionSummary(
                session_id="newer",
                created_at=1_700_000_300_000,
                segment_count=2,
                source="mixed",
                target_app="Code.exe",
                preview="newer preview",
            ),
            SessionSummary(
                session_id="older",
                created_at=1_700_000_000_000,
                segment_count=1,
                source="mic",
                target_app="Notepad.exe",
                preview="older preview",
            ),
        ]
        self.segments = {
            "newer": [
                SegmentRecord("newer", 1, 1_700_000_301_000, "raw second", "", "mixed", 900, "Code.exe", "continuous", "fire-red"),
                SegmentRecord("newer", 0, 1_700_000_300_000, "raw first", "polished first", "mixed", 1100, "Code.exe", "continuous", "fire-red"),
            ],
            "older": [
                SegmentRecord("older", 0, 1_700_000_000_000, "older raw", "", "mic", 500, "Notepad.exe", "hotkey", "tiny"),
            ],
        }
        self.search_terms: list[str] = []
        self.deleted_sessions: list[list[str]] = []
        self.deleted_all = False
        self.updated_polished: list[tuple[str, dict[int, str]]] = []
        self.merged_sessions: list[tuple[str, list[str]]] = []

    def list_sessions(self, limit: int = 50, offset: int = 0) -> list[SessionSummary]:
        return self.sessions[offset : offset + limit]

    def search_sessions(self, q: str) -> list[SessionSummary]:
        self.search_terms.append(q)
        return [self.sessions[1]]

    def get_session_segments(self, session_id: str) -> list[SegmentRecord]:
        return self.segments[session_id]

    def enqueue_delete_sessions(self, session_ids: list[str]) -> None:
        self.deleted_sessions.append(session_ids)

    def enqueue_delete_all(self) -> None:
        self.deleted_all = True

    def enqueue_update_polished(self, session_id: str, polished_by_seq: dict[int, str]) -> None:
        self.updated_polished.append((session_id, dict(polished_by_seq)))
        segs = self.segments.get(session_id, [])
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
            for s in segs
        ]

    def enqueue_merge_sessions(self, target_session_id: str, session_ids: list[str]) -> None:
        self.merged_sessions.append((target_session_id, list(session_ids)))
        combined: list[SegmentRecord] = []
        for sid in session_ids:
            combined.extend(self.segments.get(sid, []))
        combined.sort(key=lambda s: (s.created_at, s.seq))
        self.segments[target_session_id] = [
            SegmentRecord(
                target_session_id,
                idx,
                s.created_at,
                s.raw_text,
                s.polished_text,
                s.source,
                s.duration_ms,
                s.target_app,
                s.trigger_mode,
                s.model,
                s.speaker_id,
                s.speaker_route,
                s.captured_at,
            )
            for idx, s in enumerate(combined)
        ]
        for sid in session_ids:
            if sid != target_session_id:
                self.segments.pop(sid, None)
        target_segs = self.segments[target_session_id]
        first = target_segs[0]
        preview = (first.polished_text or first.raw_text).strip()
        new_sessions: list[SessionSummary] = []
        for summary in self.sessions:
            if summary.session_id == target_session_id:
                new_sessions.append(
                    SessionSummary(
                        target_session_id,
                        first.created_at,
                        len(target_segs),
                        first.source,
                        first.target_app,
                        preview,
                    )
                )
            elif summary.session_id not in session_ids:
                new_sessions.append(summary)
        self.sessions = sorted(new_sessions, key=lambda s: s.created_at, reverse=True)


def test_renders_sessions_as_reverse_order_group_rows(qapp):
    store = FakeHistoryStore()
    window = HistoryWindow(store)

    assert not hasattr(window, "_history_web")
    assert window.session_count() == 2
    # Same-day sessions share one non-selectable group header item.
    header = window._session_list.item(0)
    assert not header.data(Qt.ItemDataRole.UserRole)
    assert not (header.flags() & Qt.ItemFlag.ItemIsSelectable)
    first, second = window.session_items()

    assert first.data(Qt.ItemDataRole.UserRole) == "newer"
    assert "newer preview" in first.text()
    assert "2 段" in first.text()
    assert "mixed" not in first.text()
    assert "Code.exe" not in first.text()
    assert "混合" in first.toolTip()
    assert "Code.exe" in first.toolTip()
    assert second.data(Qt.ItemDataRole.UserRole) == "older"


def test_expand_session_renders_effective_segments(qapp):
    store = FakeHistoryStore()
    window = HistoryWindow(store)

    window._expand_session(window.session_items()[0])

    detail = window._details.toPlainText()
    assert "polished first" in detail
    assert "raw second" in detail
    assert "raw first" not in detail
    chips = [c.text() for c in window._detail_chip_labels]
    assert any("麦克风 + 电脑播放" in t for t in chips)
    assert "来源：" not in detail
    # Title carries the session identity instead of a static caption.
    assert window._detail_title.text() != "会话详情"
    assert ":" in window._detail_title.text()
    # Raw / polished is a view toggle, not a doubled-up wall of text.
    assert not window._view_bar.isHidden()
    window._view_raw_btn.setChecked(True)
    detail = window._details.toPlainText()
    assert "raw first" in detail
    assert "polished first" not in detail


def test_detail_chips_wrap_instead_of_clipping_in_a_narrow_pane(qapp):
    window = HistoryWindow(FakeHistoryStore())
    window._set_detail_chips(["麦克风", "会议记录（示例）", "持续转写", "Fun-ASR-Nano"])
    bar = window._detail_chip_bar
    one_row = window._detail_chip_labels[0].sizeHint().height()
    total = sum(c.sizeHint().width() + 6 for c in window._detail_chip_labels)
    width = total // 2
    bar.resize(width, bar.heightForWidth(width))
    bar.layout().setGeometry(bar.rect())

    assert bar.heightForWidth(width) > one_row
    for chip in window._detail_chip_labels:
        assert chip.geometry().right() < width
        assert chip.width() >= chip.sizeHint().width()
    assert bar.heightForWidth(total * 2) == one_row


def _long_turn(seq: int, text: str) -> SegmentRecord:
    return SegmentRecord(
        "long",
        seq,
        1_700_000_500_000 + seq * 1000,
        text,
        "",
        "mic",
        800,
        "Cursor.exe",
        "continuous",
        "funasr-nano",
    )


def _settle_detail(qapp, window: HistoryWindow) -> None:
    window.resize(980, 640)
    window.show()
    for _ in range(6):
        qapp.processEvents()


def test_detail_follows_latest_turn_and_keeps_place_when_scrolled_up(qapp):
    paragraph = "这一轮转写内容比较长，用来把详情撑出滚动条。" * 4
    segments = [_long_turn(seq, f"{seq} {paragraph}") for seq in range(12)]
    store = FakeHistoryStore()
    store.sessions.insert(
        0,
        SessionSummary(
            session_id="long",
            created_at=1_700_000_500_000,
            segment_count=len(segments),
            source="mic",
            target_app="Cursor.exe",
            preview=paragraph,
        ),
    )
    store.segments["long"] = segments
    window = HistoryWindow(store)
    try:
        _settle_detail(qapp, window)
        bar = window._details.verticalScrollBar()
        assert bar.maximum() > 0
        assert bar.value() == bar.maximum()
        assert window._detail_follow_tail
        assert "11 " in window._details.toPlainText()

        bar.setValue(0)
        qapp.processEvents()
        assert not window._detail_follow_tail

        segments.append(_long_turn(12, "最新一轮在最下方"))
        store.sessions[0] = SessionSummary(
            session_id="long",
            created_at=1_700_000_500_000,
            segment_count=len(segments),
            source="mic",
            target_app="Cursor.exe",
            preview=paragraph,
        )
        window.refresh()
        _settle_detail(qapp, window)
        assert "最新一轮在最下方" in window._details.toPlainText()
        assert bar.value() <= window._DETAIL_TAIL_SLACK
        assert bar.value() < bar.maximum()

        bar.setValue(bar.maximum())
        qapp.processEvents()
        assert window._detail_follow_tail
        segments.append(_long_turn(13, "再来一轮仍然贴底"))
        store.sessions[0] = SessionSummary(
            session_id="long",
            created_at=1_700_000_500_000,
            segment_count=len(segments),
            source="mic",
            target_app="Cursor.exe",
            preview=paragraph,
        )
        window.refresh()
        _settle_detail(qapp, window)
        assert "再来一轮仍然贴底" in window._details.toPlainText()
        assert bar.maximum() > 0
        assert bar.value() == bar.maximum()
    finally:
        window.close()


def test_legacy_file_import_history_labels_are_preserved(qapp):
    """Withdrawn file-transcription capability still shows legacy history metadata."""
    store = FakeHistoryStore()
    store.sessions.insert(
        0,
        SessionSummary(
            session_id="file1",
            created_at=1_700_000_400_000,
            segment_count=1,
            source="file",
            target_app="",
            preview="from media file",
        ),
    )
    store.segments["file1"] = [
        SegmentRecord(
            "file1",
            0,
            1_700_000_400_000,
            "file raw text",
            "",
            "file",
            1200,
            "",
            "file_import",
            "fire-red",
        ),
    ]
    window = HistoryWindow(store)
    window._expand_session(window.session_items()[0])
    detail = window._details.toPlainText()
    chips = [c.text() for c in window._detail_chip_labels]
    assert any("文件转写" in t for t in chips)
    assert any("导入文件" in t for t in chips)
    assert "来源：" not in detail
    assert "file raw text" in detail


def test_history_chrome_copy(qapp):
    window = HistoryWindow(FakeHistoryStore())
    try:
        assert window._title_label.text() == "历史"
        assert window._search_edit.placeholderText() == "搜索全部听写历史"
        assert window._search_edit.parentWidget() is window._stream_host
        from sayink.ui import settings_styles as ss
        from sayink.ui import design_tokens as tok
        assert tok.ACCENT.lower() not in window._export_btn.styleSheet().lower() or (
            ss.BTN_PRIMARY not in (window._export_btn.styleSheet(),)
        )
    finally:
        window.close()


def test_detail_collapses_technical_metadata(qapp):
    window = HistoryWindow(FakeHistoryStore())
    try:
        window._session_list.setCurrentItem(window.session_items()[0])
        window._expand_session(window.session_items()[0])
        body = window._details.toPlainText()
        assert "来源：" not in body
        assert "polished first" in body
        chips = [c.text() for c in window._detail_chip_labels]
        assert any("麦克风 + 电脑播放" in t for t in chips)
        assert any("持续" in t or "持续转写" in t for t in chips)
        assert window._detail_chip_bar.isHidden()
        assert "Code" in window._detail_context.toolTip()
        assert "Code.exe" not in window._detail_context.toolTip()
        window._metadata_btn.setChecked(True)
        assert not window._detail_chip_bar.isHidden()
        assert any("识别模型：fire-red" in t for t in chips)
    finally:
        window.close()


def test_batch_selection_checkboxes_and_exit_restore_single_preview(qapp):
    window = HistoryWindow(FakeHistoryStore())
    try:
        first, second = window.session_items()
        checkbox = window._session_list.itemWidget(second).findChild(QCheckBox)
        assert checkbox.isHidden()
        window._selection_mode_btn.click()
        assert not checkbox.isHidden()
        assert window._list_summary.text() == "已选 1 条"
        checkbox.click()
        assert set(window._selected_session_ids()) == {"newer", "older"}
        assert window._list_summary.text() == "已选 2 条"
        assert window._delete_btn.text() == "删除 2 条"
        checkbox.click()
        assert window._selected_session_ids() == ["newer"]
        window._selection_mode_btn.click()
        assert checkbox.isHidden()
        assert not window._selection_mode
        assert first.isSelected()
        assert "polished first" in window._details.toPlainText()
    finally:
        window.close()


def test_history_toolbar_buttons_never_clip_text_at_minimum_window_size(qapp):
    store = FakeHistoryStore()
    window = HistoryWindow(store)
    window.resize(740, 520)
    window.show()
    qapp.processEvents()
    try:
        # Single-selection polished state
        window._session_list.setCurrentItem(window.session_items()[0])
        qapp.processEvents()
        for btn in (window._view_polished_btn, window._view_raw_btn, window._copy_btn,
                    window._edit_btn, window._export_btn, window._detail_more_btn):
            if btn.isVisible():
                assert btn.width() >= btn.fontMetrics().horizontalAdvance(btn.text()) + 20

        # Batch selection state (5 buttons visible)
        window._selection_mode_btn.click()
        for item in window.session_items():
            item.setSelected(True)
        qapp.processEvents()
        for btn in (window._copy_btn, window._merge_btn, window._to_meeting_btn,
                    window._export_btn, window._delete_btn):
            assert btn.isVisible()
            assert btn.width() >= btn.fontMetrics().horizontalAdvance(btn.text()) + 20
    finally:
        window.close()


def test_batch_row_click_toggles_and_shift_keeps_native_range_selection(qapp):
    from PyQt6.QtTest import QTest

    store = FakeHistoryStore()
    store.sessions.append(SessionSummary("third", 1_699_999_900_000, 1, "mic", "", "third preview"))
    store.segments["third"] = [SegmentRecord("third", 0, 1_699_999_900_000, "third raw")]
    window = HistoryWindow(store)
    try:
        window.resize(960, 640)
        window.show()
        qapp.processEvents()
        first, second, third = window.session_items()
        window._selection_mode_btn.click()
        viewport = window._session_list.viewport()
        QTest.mouseClick(viewport, Qt.MouseButton.LeftButton,
                         pos=window._session_list.visualItemRect(second).center())
        assert set(window._selected_session_ids()) == {"newer", "older"}
        QTest.mouseClick(viewport, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.ShiftModifier, window._session_list.visualItemRect(third).center())
        assert second.isSelected() and third.isSelected()
        was_selected = first.isSelected()
        QTest.mouseClick(viewport, Qt.MouseButton.LeftButton,
                         pos=window._session_list.visualItemRect(first).center())
        assert first.isSelected() != was_selected
        assert second.isSelected() and third.isSelected()
    finally:
        window.close()


def test_batch_one_item_keeps_effective_copy_after_raw_preview(qapp, monkeypatch):
    copied = []
    monkeypatch.setattr("sayink.ui.history_window.copy_text", lambda text: copied.append(text) or True)
    window = HistoryWindow(FakeHistoryStore())
    try:
        window._view_raw_btn.setChecked(True)
        window._selection_mode_btn.click()
        assert window._copy_btn.text() == "复制 1 条"
        window._copy_btn.click()
        assert copied == ["polished first\n\nraw second"]
        window._session_list.clearSelection()
        window.refresh()
        assert window._selected_session_ids() == []
        assert window._list_summary.text() == "已选 0 条"
        assert not window._copy_btn.isEnabled()
        assert not window._delete_btn.isEnabled()
    finally:
        window.close()


def test_refresh_keeps_batch_selection_when_new_session_arrives(qapp):
    store = FakeHistoryStore()
    window = HistoryWindow(store)
    try:
        for item in window.session_items():
            item.setSelected(True)
        store.sessions.insert(0, SessionSummary("incoming", 1_700_000_400_000, 1, "mic", "", "new text"))
        store.segments["incoming"] = [SegmentRecord("incoming", 0, 1_700_000_400_000, "new text")]
        window.refresh()
        assert set(window._selected_session_ids()) == {"newer", "older"}
        checkboxes = [window._session_list.itemWidget(item).findChild(QCheckBox) for item in window.session_items()]
        assert [box.isChecked() for box in checkboxes] == [False, True, True]
    finally:
        window.close()


def test_refresh_keeps_visible_list_position_when_new_session_arrives(qapp):
    from PyQt6.QtWidgets import QAbstractItemView

    store = FakeHistoryStore()
    store.sessions = [SessionSummary(f"s{i}", 1_700_000_500_000 - i * 1000, 1, "mic", "", f"preview {i}")
                      for i in range(35)]
    store.segments = {s.session_id: [SegmentRecord(s.session_id, 0, s.created_at, s.preview)] for s in store.sessions}
    window = HistoryWindow(store)
    try:
        window.resize(960, 640)
        window.show()
        qapp.processEvents()
        anchor = window.session_items()[15]
        window._session_list.setCurrentItem(anchor)
        window._session_list.scrollToItem(anchor, QAbstractItemView.ScrollHint.PositionAtTop)
        qapp.processEvents()
        before = window._session_list.visualItemRect(anchor).top()
        store.sessions.insert(0, SessionSummary("incoming", 1_700_000_600_000, 1, "mic", "", "incoming"))
        store.segments["incoming"] = [SegmentRecord("incoming", 0, 1_700_000_600_000, "incoming")]
        window.refresh()
        qapp.processEvents()
        anchor = next(item for item in window.session_items() if item.data(Qt.ItemDataRole.UserRole) == "s15")
        assert window._session_list.visualItemRect(anchor).top() == before
        assert window._selected_session_ids() == ["s15"]
        window._session_list.verticalScrollBar().setValue(0)
        store.sessions.insert(0, SessionSummary("next", 1_700_000_700_000, 1, "mic", "", "next"))
        store.segments["next"] = [SegmentRecord("next", 0, 1_700_000_700_000, "next")]
        window.refresh()
        qapp.processEvents()
        assert window._session_list.verticalScrollBar().value() == 0
        assert window._selected_session_ids() == ["s15"]
    finally:
        window.close()


def test_ctrl_c_in_detail_copies_view_or_selected_text(qapp, monkeypatch):
    from PyQt6.QtTest import QTest

    copied = []
    monkeypatch.setattr("sayink.ui.history_window.copy_text", lambda text: copied.append(text) or True)
    window = HistoryWindow(FakeHistoryStore())
    try:
        window._view_raw_btn.setChecked(True)
        QTest.keyClick(window._details, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        assert copied == ["raw first\n\nraw second"]
        window._details.selectAll()
        QTest.keyClick(window._details, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        assert len(copied) == 1  # Native selection copy is not intercepted.
    finally:
        window.close()


def test_clear_all_confirmation_states_total_and_scope(qapp, monkeypatch):
    from PyQt6.QtWidgets import QLabel

    store = FakeHistoryStore()
    store.count_sessions = lambda: 120
    window = HistoryWindow(store)
    seen = []

    def inspect(dialog):
        seen.append(dialog.findChild(QLabel, "clearHistoryBody").text())
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr("sayink.ui.history_window._ClearHistoryDialog.exec", inspect)
    try:
        window._search_edit.setText("older")
        window._perform_search()
        window._clear_all_history()
        assert "120 条" in seen[0]
        assert "搜索结果之外和尚未加载" in seen[0]
        assert "会议记录不受影响" in seen[0]
        assert not store.deleted_all
    finally:
        window.close()


def test_search_opens_matching_version_and_preserves_manual_choice(qapp):
    store = FakeHistoryStore()
    store.search_sessions = lambda _query: [store.sessions[0]]
    window = HistoryWindow(store)
    try:
        window._search_edit.setText("raw first")
        window._perform_search()
        assert window._view_raw_btn.isChecked()
        assert window._copy_btn.text() == "复制原文"
        assert "raw first" in window._details.toPlainText()
        window._view_polished_btn.setChecked(True)
        window.refresh()
        assert window._view_polished_btn.isChecked()
        assert "polished first" in window._details.toPlainText()
    finally:
        window.close()


def test_search_debounces_into_like_search(qapp):
    store = FakeHistoryStore()
    window = HistoryWindow(store)

    window._search_edit.setText("older")
    window._search_timer.stop()
    window._perform_search()

    assert store.search_terms == ["older"]
    assert window.session_count() == 1
    assert window.session_items()[0].data(Qt.ItemDataRole.UserRole) == "older"


def test_delete_selected_sessions_commits_after_undo_timeout(qapp, monkeypatch):
    store = FakeHistoryStore()
    window = HistoryWindow(store)
    for item in window.session_items():
        item.setSelected(True)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: pytest.fail("delete must not open a modal; undo is the safety net"),
    )

    window._delete_selected_sessions()
    assert store.deleted_sessions == []
    assert window.session_count() == 0
    assert not window._undo_bar.isHidden()
    assert not window._undo_btn.isHidden()
    assert "撤销" in window._feedback_label.text()

    window._commit_pending_delete()
    assert store.deleted_sessions == [["newer", "older"]]
    assert window._undo_bar.isHidden()


def test_flush_pending_delete_commits_inside_undo_window(qapp):
    store = FakeHistoryStore()
    window = HistoryWindow(store)
    window.session_items()[0].setSelected(True)
    window._delete_selected_sessions()

    window.flush_pending_delete()

    assert store.deleted_sessions == [["newer"]]
    assert not window._undo_timer.isActive()
    window.flush_pending_delete()
    assert store.deleted_sessions == [["newer"]]


def test_quit_commits_pending_history_delete_before_closing_store() -> None:
    from unittest.mock import MagicMock
    from tests.helpers.app_harness import app_harness

    with app_harness() as h:
        app = h["app"]
        events: list[str] = []
        app._main = MagicMock()
        app._main.history_panel.flush_pending_delete.side_effect = lambda: events.append("flush")
        h["history"].close.side_effect = lambda **_: events.append("close")
        app._quit()
        assert events == ["flush", "close"]


def test_delete_without_selection_is_noop(qapp):
    store = FakeHistoryStore()
    window = HistoryWindow(store)
    window._session_list.clearSelection()

    window._delete_selected_sessions()

    assert store.deleted_sessions == []
    assert window.session_count() == 2


def test_undo_pending_delete_restores_sessions_without_store_delete(qapp):
    store = FakeHistoryStore()
    window = HistoryWindow(store)
    window.session_items()[0].setSelected(True)

    window._delete_selected_sessions()
    window._undo_pending_delete()

    assert store.deleted_sessions == []
    assert window.session_count() == 2


def test_export_is_ghost_and_copy_primary_follows_polish(qapp):
    from sayink.ui import settings_styles as ss

    window = HistoryWindow(FakeHistoryStore())
    try:
        assert ss.BTN_PRIMARY not in (window._export_btn.styleSheet(),)
        newer, older = window.session_items()
        window._session_list.setCurrentItem(newer)
        assert window._copy_btn.styleSheet() == ss.BTN_PRIMARY
        assert window._copy_btn.text() == "复制润色"
        window._view_raw_btn.setChecked(True)
        assert window._copy_btn.text() == "复制原文"
        window._session_list.setCurrentItem(older)
        assert window._copy_btn.styleSheet() == ss.BTN_PRIMARY
        # Without a polished text there is nothing to contrast「原文」against.
        assert window._copy_btn.text() == "复制"
    finally:
        window.close()


def test_copy_button_copies_the_visible_version(qapp, monkeypatch):
    copied = []
    monkeypatch.setattr("sayink.ui.history_window.copy_text", lambda text: copied.append(text) or True)
    window = HistoryWindow(FakeHistoryStore())
    try:
        newer, older = window.session_items()
        window._session_list.setCurrentItem(older)
        window._on_selection_changed()
        assert window._view_bar.isHidden()
        window._copy_btn.click()
        assert copied[-1] == "older raw"
        window._session_list.setCurrentItem(newer)
        window._on_selection_changed()
        window._copy_btn.click()
        assert copied[-1] == "polished first\n\nraw second"
        window._view_raw_btn.setChecked(True)
        window._copy_btn.click()
        assert copied[-1] == "raw first\n\nraw second"
    finally:
        window.close()


def _select_polished_session(window):
    newer, _older = window.session_items()
    window._session_list.setCurrentItem(newer)
    window._on_selection_changed()


def _clipboard_text() -> str:
    """Read the clipboard after letting clipboard managers finish their read.

    Qt hands the data to Windows as an OLE object served by *our* event loop;
    Ditto/Listary/clipboard history fetch it cross-process right after the
    write. Without pumping events that fetch blocks (holding the clipboard
    open ~30 s) and the next test's write fails silently.
    """
    from PyQt6.QtTest import QTest

    QTest.qWait(50)
    return QApplication.clipboard().text()


def test_ctrl_c_in_raw_view_copies_raw_text(qapp):
    from PyQt6.QtTest import QTest

    window = HistoryWindow(FakeHistoryStore())
    try:
        window.show()
        window.activateWindow()
        _select_polished_session(window)
        window._view_raw_btn.setChecked(True)
        window._session_list.setFocus()
        QApplication.processEvents()
        QApplication.clipboard().setText("")

        QTest.keyClick(window._session_list, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)

        assert _clipboard_text() == "raw first\n\nraw second"
        assert "原文" in window._feedback_label.text()
    finally:
        window.close()


def test_ctrl_c_in_polished_view_copies_polished_text(qapp):
    window = HistoryWindow(FakeHistoryStore())
    try:
        _select_polished_session(window)
        window._view_polished_btn.setChecked(True)
        window._copy_selected_effective()
        assert _clipboard_text() == "polished first\n\nraw second"
    finally:
        window.close()


def test_copy_tooltips_advertise_ctrl_c_on_the_version_being_viewed(qapp):
    window = HistoryWindow(FakeHistoryStore())
    try:
        _select_polished_session(window)
        assert "Ctrl+C" in window._copy_btn.toolTip()
        assert "润色" in window._copy_btn.toolTip()

        window._view_raw_btn.setChecked(True)
        assert "Ctrl+C" in window._copy_btn.toolTip()
        assert "原文" in window._copy_btn.toolTip()

        for item in window.session_items():
            item.setSelected(True)
        window._on_selection_changed()
        assert "最终文本" in window._copy_btn.toolTip()
        assert "Ctrl+C" in window._copy_btn.toolTip()
    finally:
        window.close()


def test_multi_select_copy_joins_effective_text(qapp):
    store = FakeHistoryStore()
    window = HistoryWindow(store)
    for item in window.session_items():
        item.setSelected(True)
    window._on_selection_changed()

    assert "已选 2 条" in window._details.toPlainText()
    assert "newer preview" in window._details.toPlainText()
    assert "已选 2 条" in window._detail_title.text()
    assert window._copy_btn.isEnabled()
    assert window._copy_btn.text() == "复制 2 条"
    assert window._export_btn.text() == "导出 2 条"
    assert window._delete_btn.text() == "删除 2 条"
    assert "可一起复制" in window._details.toPlainText()
    assert window._export_btn.isEnabled()
    assert window._delete_btn.isEnabled()

    window._copy_selected_effective()
    copied = _clipboard_text()
    assert copied == "older raw\n\npolished first\n\nraw second"
    assert "已复制 2 项" in window._feedback_label.text()
    # Action bar is fixed: buttons disable rather than disappear.
    assert not window._copy_btn.isHidden()
    assert not window._delete_btn.isHidden()
    assert not window._export_btn.isHidden()


def test_empty_history_shows_helpful_placeholder_and_disables_actions(qapp):
    store = FakeHistoryStore()
    store.sessions = []
    window = HistoryWindow(store)

    assert "还没有会话。完成一次转写后会出现在这里。" in window._details.toPlainText()
    assert not window._copy_btn.isEnabled()
    assert not window._export_btn.isEnabled()
    assert not window._delete_btn.isEnabled()


def test_copy_feedback_is_shown_for_single_selection(qapp):
    store = FakeHistoryStore()
    window = HistoryWindow(store)

    window._copy_selected_raw()

    assert "已复制" in window._feedback_label.text()
    # Feedback is an overlay toast, not a layout row, so nothing shifts.
    assert window._feedback_label.parentWidget() is window._undo_bar
    assert window._undo_bar.parentWidget() is window
    assert window.layout().indexOf(window._undo_bar) == -1
    assert window._undo_btn.isHidden()


def test_clear_all_is_in_the_list_management_menu(qapp):
    window = HistoryWindow(FakeHistoryStore())
    try:
        assert window._clear_all_action.text() == "清空全部听写历史…"
        assert window._clear_all_action in window._manage_menu.actions()
        assert window._summary_row.indexOf(window._manage_btn) >= 0
        assert window._summary_row.indexOf(window._list_summary) >= 0
        assert window._clear_all_action.isEnabled()
        assert window._delete_action in window._detail_menu.actions()
        assert window._delete_btn.isHidden()
    finally:
        window.close()


def test_selected_session_row_is_not_an_accent_capsule(qapp):
    window = HistoryWindow(FakeHistoryStore())
    try:
        item_css = window._session_list.styleSheet().split("QScrollBar")[0]
        assert "border-radius" not in item_css
        assert "outline: none" in item_css
    finally:
        window.close()


def test_session_list_focus_ring_is_keyboard_only_and_on_the_row(qapp):
    from PyQt6.QtTest import QTest

    window = HistoryWindow(FakeHistoryStore())
    try:
        window.resize(900, 600)
        window.show()
        lst = window._session_list
        css = lst.styleSheet()
        assert "QListWidget:focus" not in css
        assert "border: 2px solid transparent" in css

        rows = [i for i in range(lst.count()) if lst.item(i).flags() & Qt.ItemFlag.ItemIsSelectable]
        assert len(rows) >= 2
        QTest.mouseClick(lst.viewport(), Qt.MouseButton.LeftButton,
                         pos=lst.visualItemRect(lst.item(rows[0])).center())
        qapp.processEvents()
        assert lst.hasFocus() or not window.isActiveWindow()
        assert not lst.focus_ring_visible

        lst.setFocus(Qt.FocusReason.TabFocusReason)
        QTest.keyClick(lst, Qt.Key.Key_Down)
        qapp.processEvents()
        assert lst.focus_ring_visible
        assert lst._focus_ring.geometry() == lst.visualItemRect(lst.currentItem())

        QTest.mouseClick(lst.viewport(), Qt.MouseButton.LeftButton,
                         pos=lst.visualItemRect(lst.item(rows[0])).center())
        qapp.processEvents()
        assert not lst.focus_ring_visible
    finally:
        window.close()


def test_selected_rows_keep_an_accent_bar_when_multi_selected(qapp):
    from sayink.ui import design_tokens as tok

    window = HistoryWindow(FakeHistoryStore())
    try:
        items = window.session_items()
        assert len(items) >= 2
        items[0].setSelected(True)
        items[1].setSelected(True)
        qapp.processEvents()

        selected_css = [
            window._session_list.itemWidget(item).styleSheet() for item in items
        ]
        bar = f"border-left: {tok.NAV_SELECTED_BAR_PX}px solid {tok.ACCENT}"
        assert all(bar in css for css in selected_css)
        assert all(tok.ACCENT_SOFT in css for css in selected_css)
        assert all("border-radius: 0" in css for css in selected_css)

        items[1].setSelected(False)
        qapp.processEvents()
        idle = window._session_list.itemWidget(items[1]).styleSheet()
        assert tok.ACCENT_SOFT not in idle
        assert f"solid {tok.ACCENT}" not in idle
        assert "solid transparent" in idle
    finally:
        window.close()


def test_history_splitter_does_not_look_like_a_second_scrollbar(qapp):
    window = HistoryWindow(FakeHistoryStore())
    try:
        splitter_css = window._splitter.styleSheet()
        list_css = window._session_list.styleSheet()
        assert "margin: 8px 0" not in splitter_css
        assert "background: transparent" in splitter_css
        assert window._session_list.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        assert "border: none" in list_css
        assert "QScrollBar::add-page:vertical" in list_css
    finally:
        window.close()


def test_clear_all_disabled_when_history_is_empty(qapp):
    store = FakeHistoryStore()
    store.sessions = []
    window = HistoryWindow(store)
    try:
        assert not window._clear_all_action.isEnabled()
    finally:
        window.close()


def test_export_failure_shows_error_message(qapp, monkeypatch, tmp_path):
    store = FakeHistoryStore()
    window = HistoryWindow(store)
    errors = []
    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        lambda *args, **kwargs: (str(tmp_path / "history.md"), "Markdown (*.md)"),
    )
    monkeypatch.setattr(
        Path,
        "write_text",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        lambda *args, **kwargs: errors.append(args[2]),
    )

    window._export_selected()

    assert errors and "disk full" in errors[0]


def test_clear_all_history_uses_store_enqueue_after_confirmation(qapp, monkeypatch):
    store = FakeHistoryStore()
    window = HistoryWindow(store)
    seen: list[list[str]] = []

    def _accept(dialog):
        seen.append([button.text() for button in dialog.findChildren(QPushButton)])
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr("sayink.ui.history_window._ClearHistoryDialog.exec", _accept)

    window._clear_all_history()

    assert store.deleted_all is True
    assert seen == [["取消", "清空"]]


def test_clear_all_history_cancel_keeps_sessions(qapp, monkeypatch):
    store = FakeHistoryStore()
    window = HistoryWindow(store)
    monkeypatch.setattr(
        "sayink.ui.history_window._ClearHistoryDialog.exec",
        lambda _dialog: QDialog.DialogCode.Rejected,
    )

    window._clear_all_history()

    assert store.deleted_all is False


def test_single_export_markdown_frontmatter_and_effective_text_by_seq():
    session = SessionSummary("newer", 1_700_000_300_000, 2, "mixed", "Code.exe", "preview")
    segments = [
        SegmentRecord("newer", 2, 1_700_000_302_000, "third raw", "third polished", "mixed", 700, "Code.exe", "continuous", "fire-red"),
        SegmentRecord("newer", 0, 1_700_000_300_000, "first raw", "", "mixed", 1100, "Code.exe", "continuous", "fire-red"),
        SegmentRecord("newer", 1, 1_700_000_301_000, "second raw", "second polished", "mixed", 900, "Code.exe", "continuous", "fire-red"),
    ]

    markdown = build_single_session_markdown(session, segments)

    assert markdown.startswith("---\n")
    assert "source: mixed" in markdown
    assert "target_app: Code.exe" in markdown
    assert "duration_ms: 2700" in markdown
    assert "model: fire-red" in markdown
    assert "segment_count: 2" in markdown
    assert markdown.index("first raw") < markdown.index("second polished") < markdown.index("third polished")
    assert "second raw" not in markdown


def test_batch_export_markdown_uses_one_section_per_session():
    newer = SessionSummary("newer", 1_700_000_300_000, 2, "mixed", "Code.exe", "newer preview")
    older = SessionSummary("older", 1_700_000_000_000, 1, "mic", "Notepad.exe", "older preview")
    segments_by_session = {
        "newer": [SegmentRecord("newer", 0, 1_700_000_300_000, "new raw", "", "mixed", 100, "Code.exe", "continuous", "fire-red")],
        "older": [SegmentRecord("older", 0, 1_700_000_000_000, "old raw", "", "mic", 100, "Notepad.exe", "hotkey", "tiny")],
    }

    markdown = build_batch_export_markdown([newer, older], segments_by_session)

    assert markdown.count("\n## ") == 2
    assert "## " in markdown
    assert "old raw" in markdown
    assert "new raw" in markdown
    assert markdown.index("older preview") < markdown.index("newer preview")


def test_export_filenames_follow_adr_patterns():
    session = SessionSummary("newer", 1_700_000_300_000, 2, "mixed", "Code.exe", "preview")

    assert suggest_single_export_filename(session).startswith("sayink-")
    assert suggest_single_export_filename(session).endswith(".md")
    assert suggest_batch_export_filename(now_ms=1_700_000_300_000).startswith("sayink-export-")
    assert suggest_batch_export_filename(now_ms=1_700_000_300_000).endswith(".md")


def test_tray_menu_has_history_entry_signal(qapp):
    tray = TrayIcon()
    emitted: list[bool] = []
    tray.history_requested.connect(lambda: emitted.append(True))

    history_action = next(
        action for action in tray.contextMenu().actions() if action.text() == "历史"
    )
    history_action.trigger()

    assert emitted == [True]
    tray.hide()


def test_app_show_history_window_reuses_single_window(qapp, monkeypatch):
    from unittest.mock import MagicMock

    created: list[object] = []

    class FakeHistoryWidget:
        def __init__(self):
            self.refresh_calls = 0

        def refresh(self):
            self.refresh_calls += 1

    class FakeMainWindow:
        def __init__(self, config, history_store, *, meeting=None):
            self.config = config
            self.store = history_store
            self.meeting = meeting
            self.history_panel = FakeHistoryWidget()
            self.settings_panel = MagicMock()
            self._page = "general"
            self.shown = 0
            self.raised = 0
            self.activated = 0
            created.append(self)

        def show_page(self, page):
            self._page = page

        def current_page(self):
            return self._page

        def show(self):
            self.shown += 1

        def raise_(self):
            self.raised += 1

        def activateWindow(self):
            self.activated += 1

        def installEventFilter(self, obj):
            pass

    monkeypatch.setattr("sayink.app.MainWindow", FakeMainWindow)
    app = App.__new__(App)
    app._config = object()
    app._history = object()
    app._main = None
    app._pending_segment_count = 0
    app._hotkey_mgr = MagicMock()
    app._updates = MagicMock()
    app.apply_appearance_theme = lambda: None
    app._sync_settings_runtime_status = lambda: None

    app._show_history_window()
    first = app._main
    app._show_history_window()

    assert len(created) == 1
    assert app._main is first
    assert created[0].store is app._history
    assert created[0].current_page() == "history"
    assert created[0].shown == 2
    assert created[0].raised == 2
    assert created[0].activated == 2
    assert created[0].history_panel.refresh_calls == 2

    app._show_main_window(None)
    assert created[0].history_panel.refresh_calls == 3


def test_single_session_inline_edit_saves_to_polished_and_preserves_raw(qapp):
    store = FakeHistoryStore()
    window = HistoryWindow(store)
    window.show()
    # Select the single-segment session "older"
    items = window.session_items()
    window._session_list.setCurrentItem(items[1])

    assert not window._edit_btn.isHidden()
    assert window._edit_btn.text() == "编辑"
    window._edit_btn.click()

    assert window._editing_session is True
    assert not window._details.isReadOnly()
    assert window._edit_btn.text() == "保存"
    assert not window._cancel_edit_btn.isHidden()

    window._details.setPlainText("手动修正后的文本")
    window._edit_btn.click()

    assert window._editing_session is False
    assert window._details.isReadOnly()
    assert store.updated_polished == [("older", {0: "手动修正后的文本"})]
    assert "手动修正后的文本" in window._details.toPlainText()
    # Raw text is still preserved when toggling to 原文
    window._view_raw_btn.setChecked(True)
    assert "older raw" in window._details.toPlainText()


def test_single_session_inline_edit_cancel_discards_changes(qapp):
    store = FakeHistoryStore()
    window = HistoryWindow(store)
    window.show()
    items = window.session_items()
    window._session_list.setCurrentItem(items[1])

    window._edit_btn.click()
    window._details.setPlainText("不应该被保存的修改")
    window._cancel_edit_btn.click()

    assert window._editing_session is False
    assert window._details.isReadOnly()
    assert store.updated_polished == []
    assert "older raw" in window._details.toPlainText()


def test_single_session_on_demand_ai_polish_and_error_protection(qapp):
    from PyQt6.QtCore import QObject, pyqtSignal

    class FakePolisher(QObject):
        polish_complete = pyqtSignal(str)
        polish_error = pyqtSignal(str)

        def __init__(self):
            super().__init__()
            self.calls: list[tuple[str, str, str, str]] = []

        def polish(self, text, api_url, api_key, model_name, prompt="", *, mode="polish"):
            self.calls.append((text, api_url, api_key, model_name))

        def cancel(self):
            pass

    class FakeConfig:
        def __init__(self, complete=True):
            self.complete = complete

        def get(self, key, default=None):
            if not self.complete:
                return ""
            return {
                "llm.api_url": "https://api.example.com/v1",
                "llm.api_key": "sk-test",
                "llm.model_name": "deepseek-chat",
                "llm.custom_prompt": "",
            }.get(key, default)

    store = FakeHistoryStore()
    polisher = FakePolisher()
    window = HistoryWindow(store, config=FakeConfig(complete=True), polisher=polisher)
    window.show()
    items = window.session_items()
    window._session_list.setCurrentItem(items[1])

    assert window._polish_btn.text() == "润色"
    window._polish_btn.click()
    assert len(polisher.calls) == 1
    assert polisher.calls[0][0] == "older raw"

    # Simulate error first: existing text must remain untouched
    polisher.polish_error.emit("润色超时，请稍后重试")
    assert store.updated_polished == []
    assert "超时" in window._feedback_label.text()

    # Now trigger again and succeed
    window._polish_btn.click()
    polisher.polish_complete.emit("AI 润色后的新文本")
    assert store.updated_polished == [("older", {0: "AI 润色后的新文本"})]
    assert "AI 润色后的新文本" in window._details.toPlainText()
    assert window._polish_action.text() == "重新润色"
    assert window._polish_btn.isHidden()


def test_batch_merge_sessions_with_8s_undo_and_commit(qapp):
    store = FakeHistoryStore()
    window = HistoryWindow(store)
    window.show()

    window._selection_mode_btn.click()
    for item in window.session_items():
        item.setSelected(True)

    assert not window._merge_btn.isHidden()
    assert window._merge_btn.isEnabled()
    assert window._merge_btn.text() == "合并"

    window._merge_btn.click()

    # Immediately previews 1 merged session and shows undo toast
    assert window.session_count() == 1
    assert window._toast.isVisible()
    assert "已合并 2 项" in window._feedback_label.text()
    assert store.merged_sessions == []

    # Undo within 8s restores both sessions
    window._undo_btn.click()
    assert window.session_count() == 2
    assert store.merged_sessions == []

    # Merge again and commit on timer timeout
    window._selection_mode_btn.setChecked(True)
    for item in window.session_items():
        item.setSelected(True)
    window._merge_btn.click()
    window._commit_pending_undo()

    assert len(store.merged_sessions) == 1
    target_id, merged_ids = store.merged_sessions[0]
    assert target_id == "older"
    assert set(merged_ids) == {"older", "newer"}


def test_batch_promote_to_meeting_emits_meeting_created(qapp):
    from types import SimpleNamespace

    store = FakeHistoryStore()
    promoted_batches: list[list[str]] = []

    def fake_create_meeting_from_sessions(session_ids):
        promoted_batches.append(list(session_ids))
        return SimpleNamespace(id="promoted-m1")

    store.create_meeting_from_sessions = fake_create_meeting_from_sessions
    window = HistoryWindow(store)
    window.show()
    emitted: list[str] = []
    window.meeting_created.connect(emitted.append)

    window._selection_mode_btn.click()
    for item in window.session_items():
        item.setSelected(True)

    assert window._to_meeting_btn.text() == "转为会议"
    window._to_meeting_btn.click()

    assert promoted_batches == [["newer", "older"]]
    assert emitted == ["promoted-m1"]
    assert window._selection_mode is False


def test_multi_segment_inline_edit_collapse_suppresses_trailing_segments_without_phantom_captions(qapp):
    store = FakeHistoryStore()
    window = HistoryWindow(store)
    window.show()
    # Select the 2-segment session "newer"
    window._session_list.setCurrentItem(window.session_items()[0])

    window._edit_btn.click()
    window._details.setPlainText("整段合并修改后的唯一段落")
    window._edit_btn.click()

    assert store.updated_polished == [("newer", {0: "整段合并修改后的唯一段落", 1: "\u200b"})]
    rendered = window._details.toPlainText()
    assert "整段合并修改后的唯一段落" in rendered
    assert "\u200b" not in rendered
    # Toggling to 原文 still shows both original raw segments
    window._view_raw_btn.setChecked(True)
    raw_rendered = window._details.toPlainText()
    assert "raw first" in raw_rendered
    assert "raw second" in raw_rendered




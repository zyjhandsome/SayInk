"""Batch select, hard-delete, export and minutes queue for loaded meetings."""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QCheckBox, QFileDialog, QListWidget

from sayink.history_store import HistoryStore, SegmentRecord
from sayink.meeting_session import MeetingController
from sayink.ui.meeting_batch import batch_delete_copy
from sayink.ui.meeting_page import MeetingPage


def test_delete_key_removes_the_selection(config, tmp_path, monkeypatch):
    page, controller, history, first, second = _page(config, tmp_path)
    try:
        page._set_batch_mode(True)
        for index in range(page._list.count()):
            item = page._list.item(index)
            item.setSelected(item.data(Qt.ItemDataRole.UserRole) == first.id)
        monkeypatch.setattr("sayink.ui.meeting_page.confirm_batch_delete", lambda *_args, **_kwargs: True)
        page._delete_shortcut.activated.emit()
        assert controller.store.get_meeting(first.id) is None
        assert controller.store.get_meeting(second.id) is not None
    finally:
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_rebuilding_meeting_rows_does_not_open_a_window(config, tmp_path):
    from PyQt6.QtCore import QEvent, QObject
    from PyQt6.QtWidgets import QApplication, QWidget

    page, controller, history, _first, _second = _page(config, tmp_path)
    shown: list[str] = []

    class _WindowShowFilter(QObject):
        def eventFilter(self, watched, event):
            if (
                event.type() == QEvent.Type.Show
                and isinstance(watched, QWidget)
                and watched.isWindow()
                and watched is not page
            ):
                shown.append(watched.__class__.__name__)
            return False

    app = QApplication.instance()
    watcher = _WindowShowFilter()
    app.installEventFilter(watcher)
    try:
        page.show()
        app.processEvents()
        shown.clear()
        page._meeting_rows_key = None
        page.refresh()
        app.processEvents()
        assert shown == []
        assert page._list.count() == 2
    finally:
        QApplication.instance().removeEventFilter(watcher)
        page.close()
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_generating_label_updates_without_replacing_the_row(config, tmp_path):
    page, controller, history, first, _second = _page(config, tmp_path)
    try:
        page.show_meeting(first.id)
        item = next(
            page._list.item(index)
            for index in range(page._list.count())
            if page._list.item(index).data(Qt.ItemDataRole.UserRole) == first.id
        )
        widget = page._list.itemWidget(item)
        controller._generating_for = first.id
        page.refresh()
        assert page._list.itemWidget(item) is widget
        assert "纪要生成中" in item.data(Qt.ItemDataRole.AccessibleTextRole)
    finally:
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_batch_delete_copy_says_the_delete_cannot_be_undone():
    text, info = batch_delete_copy(2, 1)
    assert text == "删除 2 场已结束的会议？"
    assert "无法撤销" in info
    assert "另有 1 场正在进行，不会删除。" in info


def test_delete_meetings_removes_them_together_and_pumps_the_queue_once(config, tmp_path, monkeypatch):
    history = HistoryStore(db_path=tmp_path / "history.db")
    controller = MeetingController(config, history, pending_segments=lambda: 0)
    pumps = []
    monkeypatch.setattr(controller, "_pump_queue", lambda: pumps.append(1))
    try:
        kept = controller.store.create_meeting(title="留下")
        first = controller.store.create_meeting(title="甲")
        second = controller.store.create_meeting(title="乙")
        history.enqueue(SegmentRecord(first.id, 1, first.started_at, raw_text="甲的发言", trigger_mode="meeting"))
        assert controller.store.flush()
        assert controller.delete_meetings([first.id, second.id]) is True
        assert pumps == [1]
        assert controller.store.get_meeting(first.id) is None
        assert controller.store.get_segments(first.id) == []
        assert controller.store.get_meeting(second.id) is None
        assert controller.store.get_meeting(kept.id) is not None
        assert controller.store.get_meeting(kept.id).status != "deleted"
    finally:
        controller.shutdown()
        history.close(timeout=2)


def test_enqueue_minutes_skips_notes_and_empty_transcripts(config, tmp_path, monkeypatch):
    config.set("llm.api_url", "http://localhost:11434/v1")
    config.set("llm.model_name", "local-model")
    history = HistoryStore(db_path=tmp_path / "history.db")
    controller = MeetingController(config, history, pending_segments=lambda: 0)
    called = []
    monkeypatch.setattr(controller, "generate", lambda meeting_id, **kwargs: called.append(meeting_id) or True)
    try:
        ready = controller.store.create_meeting(title="可以生成")
        controller.store.end_meeting(ready.id, ended_at=ready.started_at + 1000)
        history.enqueue(SegmentRecord(
            ready.id, 1, ready.started_at, raw_text="今天讨论预算。", trigger_mode="meeting",
            captured_at=ready.started_at,
        ))
        done = controller.store.create_meeting(title="已有纪要")
        controller.store.end_meeting(done.id, ended_at=done.started_at + 1000)
        controller.store.save_notes(
            done.id, template_id="general", model="m", json_payload="", markdown="# 已有\n", duration_ms=1,
        )
        empty = controller.store.create_meeting(title="没有文字稿")
        controller.store.end_meeting(empty.id, ended_at=empty.started_at + 1000)
        assert controller.store.flush()
        queued, skipped = controller.enqueue_minutes([ready.id, done.id, empty.id])
        assert queued == 1 and skipped == 2
        assert called == [ready.id]
    finally:
        controller.shutdown()
        history.close(timeout=2)


def _page(config, tmp_path):
    config.set("llm.api_url", "http://localhost:11434/v1")
    config.set("llm.model_name", "local-model")
    history = HistoryStore(db_path=tmp_path / "meetings.db")
    controller = MeetingController(config, history, pending_segments=lambda: 0)
    first = controller.store.create_meeting(title="甲")
    second = controller.store.create_meeting(title="乙")
    for record, text in ((first, "甲的发言。"), (second, "乙的发言。")):
        controller.store.end_meeting(record.id, ended_at=record.started_at + 1000)
        history.enqueue(SegmentRecord(
            record.id, 1, record.started_at, raw_text=text, trigger_mode="meeting", captured_at=record.started_at,
        ))
    controller.store.flush()
    page = MeetingPage(config, controller)
    page.show_meeting(first.id)
    return page, controller, history, first, second


def test_select_all_covers_only_loaded_rows(config, tmp_path):
    page, controller, history, _first, _second = _page(config, tmp_path)
    try:
        older = controller.store.create_meeting(title="更早", started_at=1_000)
        controller.store.end_meeting(older.id, ended_at=2_000)
        assert controller.store.flush()
        page._list_limit = 2
        page.refresh()
        assert page.meeting_count() == 2
        assert controller.store.count_meetings() == 3
        assert page._list.selectionMode() == QListWidget.SelectionMode.ExtendedSelection
        assert page._selection_mode_btn.text() == "选择"
        assert page._selection_mode_btn.minimumWidth() >= page._selection_mode_btn.fontMetrics().horizontalAdvance("选择")
        page._select_loaded_meetings()
        assert page._batch_mode is True
        assert page._batch_export_btn.text().startswith("导出")
        assert page._batch_export_btn.minimumWidth() >= page._batch_export_btn.fontMetrics().horizontalAdvance(page._batch_export_btn.text())
        assert len(page._selected_meeting_ids()) == 2
        assert older.id not in page._selected_meeting_ids()
        box = page._list.itemWidget(page._list.item(0)).findChild(QCheckBox, "meetingBatchCheck")
        assert box is not None and not box.isHidden() and box.isChecked()
    finally:
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_batch_export_writes_one_markdown_and_delete_removes_only_the_selection(config, tmp_path, monkeypatch):
    page, controller, history, first, second = _page(config, tmp_path)
    try:
        row = page._list.itemWidget(page._list.item(0))
        page._set_batch_mode(True)
        assert page._list.itemWidget(page._list.item(0)) is row
        page._select_loaded_meetings()
        target = tmp_path / "batch.md"

        def _save(*_args, **_kwargs):
            return str(target), "Markdown (*.md)"

        monkeypatch.setattr(QFileDialog, "getSaveFileName", _save)
        page._export_selected_meetings()
        text = target.read_text(encoding="utf-8")
        assert "甲的发言" in text and "乙的发言" in text
        monkeypatch.setattr("sayink.ui.meeting_page.confirm_batch_delete", lambda *_args, **_kwargs: True)
        page._list.item(0).setSelected(False)
        kept = page._selected_meeting_ids()
        assert len(kept) == 1
        page._delete_selected_meetings()
        assert controller.store.get_meeting(kept[0]) is None
        remaining = {first.id, second.id} - set(kept)
        assert controller.store.get_meeting(remaining.pop()) is not None
        assert page._list_summary.text().startswith("已选")
    finally:
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_deleting_the_edited_meeting_does_not_overwrite_another(config, tmp_path, monkeypatch):
    page, controller, history, first, second = _page(config, tmp_path)
    try:
        controller.store.save_notes(
            first.id, template_id="general", model="m", json_payload="", markdown="# 甲的纪要\n", duration_ms=1,
        )
        assert controller.store.flush()
        page.show_meeting(first.id)
        page._notes_edit.setPlainText("# 甲的未保存草稿\n")
        page._editing_notes = True
        page._set_batch_mode(True)
        for index in range(page._list.count()):
            item = page._list.item(index)
            item.setSelected(item.data(Qt.ItemDataRole.UserRole) == first.id)
        monkeypatch.setattr("sayink.ui.meeting_page.confirm_batch_delete", lambda *_args, **_kwargs: True)
        page._delete_selected_meetings()
        page._save_notes_draft()
        saved = controller.store.get_notes(second.id)
        assert saved is None or "甲的未保存草稿" not in saved.markdown
    finally:
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_batch_generate_uses_the_default_template_not_the_open_meeting(config, tmp_path, monkeypatch):
    page, controller, history, first, _second = _page(config, tmp_path)
    try:
        config.set("meeting.template_id", "general")
        page.show_meeting(first.id)
        page._template_combo.setCurrentIndex(page._template_combo.findData("refine"))
        seen = []
        monkeypatch.setattr(
            controller, "enqueue_minutes",
            lambda ids, template_id=None: seen.append(template_id) or (1, 0),
        )
        page._set_batch_mode(True)
        page._select_loaded_meetings()
        page._generate_selected_meetings()
        assert seen == ["general"]
    finally:
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_batch_mode_hides_the_single_meeting_export(config, tmp_path):
    page, controller, history, _first, _second = _page(config, tmp_path)
    try:
        assert not page._export_btn.isHidden()
        for index in range(page._list.count()):
            page._list.item(index).setSelected(True)
        page._on_selection_changed()
        assert page._batch_mode
        assert page._export_btn.isHidden()
        assert page._content_stack.isHidden()
        assert not page._batch_export_btn.isHidden()
        assert not page._batch_notice_scroll.isHidden()
    finally:
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)

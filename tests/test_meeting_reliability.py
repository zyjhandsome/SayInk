"""Regressions for meeting save boundaries, selection, copy and retrieval."""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QTextCursor
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QLabel

from sayink.history_store import HistoryStore, SegmentRecord
from sayink.meeting_session import MeetingController, MeetingOptions, MeetingState, resolve_capabilities
from sayink.ui import settings_styles as ss
from sayink.ui.meeting_page import (
    PAGE_BROWSE,
    PAGE_LIVE,
    MeetingPage,
    _SegmentTextDialog,
    _TranscriptView,
)
from sayink.ui.minutes_templates import TemplateEditorDialog
from sayink.minutes_prompts import get_template


def _page(config, tmp_path):
    config.set("llm.api_url", "http://localhost:11434/v1")
    config.set("llm.model_name", "local-model")
    history = HistoryStore(db_path=tmp_path / "meetings.db")
    controller = MeetingController(config, history, pending_segments=lambda: 0)
    record = controller.store.create_meeting(title="设计评审", agenda="确认范围")
    history.enqueue(SegmentRecord(
        record.id, 0, record.started_at, raw_text="采用方案 A", speaker_id=1,
        trigger_mode="meeting", captured_at=record.started_at,
    ))
    history.enqueue(SegmentRecord(
        record.id, 1, record.started_at + 1000, raw_text="下周开始验证", speaker_id=2,
        trigger_mode="meeting", captured_at=record.started_at + 1000,
    ))
    controller.store.end_meeting(record.id, ended_at=record.started_at + 60_000)
    controller.store.save_notes(
        record.id, template_id="general", model="m", json_payload="", markdown="第一版纪要", duration_ms=1,
    )
    controller.store.save_notes(
        record.id, template_id="general", model="m", json_payload="", markdown="第二版纪要", duration_ms=1,
    )
    controller.store.flush()
    page = MeetingPage(config, controller)
    page.resize(1050, 700)
    page.show()
    page.show_meeting(record.id)
    return page, controller, history, record


def _live(page, controller, history):
    options = MeetingOptions(title="实时核对", input_source="microphone")
    caps = resolve_capabilities(page._config, options, dictation_model_id="funasr-nano")
    record = controller.begin(options, caps)
    return record


def test_partial_copy_keeps_text_after_an_emoji():
    view = _TranscriptView()
    view.setHtml("<p>😀方案 ABC</p>")
    cursor = view.document().find("方案")
    view.setTextCursor(cursor)
    assert cursor.selectedText() == "方案"
    assert view.createMimeDataFromSelection().text() == "方案"
    view.deleteLater()


def test_rebuilding_a_long_transcript_lays_out_every_paragraph():
    """纠正发言人 replaces the whole document. Qt only lays out the first
    screen after setHtml, so the rest of a long meeting paints blank."""
    view = _TranscriptView()
    view.resize(640, 480)
    view.show()
    view.document().setTextWidth(600)
    view.setHtml("".join(
        f"<p style='margin:0 0 6px 0'><a name='seg{i}'></a>"
        f"<span style='color:#888'>[{i // 60:02d}:{i % 60:02d}]</span> "
        f"第{i}段，这里是一句用于计时的示例发言内容。</p>"
        for i in range(400)
    ))
    QApplication.processEvents()
    blank = 0
    block = view.document().begin()
    while block.isValid():
        layout = block.layout()
        if layout is None or layout.boundingRect().height() <= 0:
            blank += 1
        block = block.next()
    view.deleteLater()
    assert blank == 0


def test_restore_original_stays_undoable(config, tmp_path):
    page, controller, history, record = _page(config, tmp_path)
    try:
        segment = controller.store.get_segments(record.id)[0]
        dialog = _SegmentTextDialog(segment, position="00:00", speaker="说话人1", save_text=lambda text: True)
        dialog.editor.moveCursor(QTextCursor.MoveOperation.End)
        dialog.editor.insertPlainText(" 重要草稿")
        assert dialog.editor.document().isUndoAvailable()
        dialog.restore_btn.click()
        assert dialog.editor.toPlainText() == segment.raw_text
        assert dialog.editor.document().isUndoAvailable()
        dialog.editor.undo()
        assert dialog.editor.toPlainText().endswith("重要草稿")
        dialog.deleteLater()
    finally:
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_ctrl_enter_saves_the_segment_dialog(config, tmp_path):
    page, controller, history, record = _page(config, tmp_path)
    try:
        segment = controller.store.get_segments(record.id)[0]
        saved = []
        dialog = _SegmentTextDialog(
            segment, position="00:00", speaker="说话人1", save_text=lambda text: saved.append(text) or True,
        )
        dialog.editor.moveCursor(QTextCursor.MoveOperation.End)
        dialog.editor.insertPlainText(" 补充")
        dialog.editor.setFocus()
        QTest.keyClick(dialog.editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        assert saved == ["采用方案 A 补充"]
        assert dialog.result() == QDialog.DialogCode.Accepted
        dialog.deleteLater()
    finally:
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_ctrl_s_saves_minutes_draft(config, tmp_path):
    page, controller, history, record = _page(config, tmp_path)
    try:
        page._toggle_edit()
        page._notes_edit.moveCursor(QTextCursor.MoveOperation.End)
        page._notes_edit.insertPlainText(" 已改")
        page._notes_edit.setFocus()
        QTest.keyClick(page._notes_edit, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
        assert controller.store.get_notes(record.id).markdown == "第二版纪要 已改"
        assert not page._editing_notes
    finally:
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_live_notes_are_written_again_when_the_first_save_does_not_land(config, tmp_path, monkeypatch):
    page, controller, history, _record = _page(config, tmp_path)
    try:
        live = _live(page, controller, history)
        real = controller.update_meeting
        calls = {"n": 0}

        def flaky(meeting_id, **fields):
            calls["n"] += 1
            if calls["n"] == 1:
                return
            return real(meeting_id, **fields)

        monkeypatch.setattr(controller, "update_meeting", flaky)
        page._user_notes.setPlainText("不能丢")
        assert page.flush_pending_user_notes() is True
        assert controller.store.get_meeting(live.id).notes_user == "不能丢"
        assert calls["n"] == 2
    finally:
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_external_end_saves_the_note_still_waiting_for_autosave(config, tmp_path):
    page, controller, history, _record = _page(config, tmp_path)
    try:
        live = _live(page, controller, history)
        page._user_notes.setPlainText("结束前的最后一句")
        assert page._user_notes_timer.isActive()
        controller.request_end()
        assert page._record_notes.toPlainText() == "结束前的最后一句"
        assert controller.store.get_meeting(live.id).notes_user == "结束前的最后一句"
    finally:
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_show_meeting_clears_search_and_opens_the_requested_record(config, tmp_path):
    page, controller, history, record = _page(config, tmp_path)
    try:
        page._search_edit.setText("设计")
        page._perform_search()
        assert page.current_meeting_id == record.id
        newer = controller.store.create_meeting(title="周会", started_at=record.started_at + 500_000)
        controller.store.end_meeting(newer.id, ended_at=newer.started_at + 1000)
        controller.store.flush()
        page.show_meeting(newer.id)
        assert page._search_query == ""
        assert page._search_edit.text() == ""
        assert page.current_meeting_id == newer.id
        assert page._detail_title.text() == "周会"
    finally:
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_meeting_list_tells_the_truth_past_one_hundred_and_can_open_the_oldest(config, tmp_path):
    page, controller, history, record = _page(config, tmp_path)
    try:
        latest = record
        for n in range(101):
            latest = controller.store.create_meeting(title=f"稍后 {n}", started_at=record.started_at + 100_000 + n)
        controller.store.flush()
        page.show_meeting(latest.id)
        assert page.meeting_count() == 100
        assert "最近 100" in page._list_summary.text()
        assert "共 102" in page._list_summary.text()
        assert page._load_more_btn.isVisible()
        page.show_meeting(record.id)
        assert page.current_meeting_id == record.id
        assert page._detail_title.text() == "设计评审"
    finally:
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_load_more_reveals_the_older_meetings(config, tmp_path):
    page, controller, history, record = _page(config, tmp_path)
    try:
        latest = record
        for n in range(101):
            latest = controller.store.create_meeting(title=f"稍后 {n}", started_at=record.started_at + 100_000 + n)
        controller.store.flush()
        page.show_meeting(latest.id)
        assert page.meeting_count() == 100
        page._load_more_btn.click()
        assert page.meeting_count() == 102
        assert "共 102" in page._list_summary.text()
        assert "最近" not in page._list_summary.text()
        assert page._load_more_btn.isHidden()
    finally:
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_transcript_refresh_keeps_the_selection(config, tmp_path):
    page, controller, history, record = _page(config, tmp_path)
    try:
        page._switch_tab(1)
        page._transcript_view.setTextCursor(page._transcript_view.document().find("下周开始验证"))
        controller.save_segment_text(record.id, 0, "采用方案 A，范围扩大")
        page._render_detail()
        assert page._transcript_view.textCursor().selectedText() == "下周开始验证"
    finally:
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_live_rename_keeps_the_selected_words(config, tmp_path):
    page, controller, history, _record = _page(config, tmp_path)
    try:
        live = _live(page, controller, history)
        for seq, speaker, text in ((0, 1, "第一位发言"), (1, 2, "TARGET SELECTED 这段要留住")):
            history.enqueue(SegmentRecord(
                live.id, seq, live.started_at + seq * 1000, raw_text=text, speaker_id=speaker,
                trigger_mode="meeting",
            ))
        history.flush()
        page._render_live()
        page._live_transcript.setTextCursor(page._live_transcript.document().find("TARGET SELECTED"))
        controller.rename_speaker(live.id, 1, "一个明显更长的说话人姓名")
        page._render_live()
        assert page._live_transcript.textCursor().selectedText() == "TARGET SELECTED"
    finally:
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_unchanged_mark_list_keeps_a_selection(config, tmp_path):
    page, controller, history, _record = _page(config, tmp_path)
    try:
        _live(page, controller, history)
        controller.add_mark("MARK TO COPY")
        controller.store.flush()
        page._render_live()
        page._marks_view.setTextCursor(page._marks_view.document().find("MARK TO COPY"))
        page._render_live()
        assert page._marks_view.textCursor().selectedText() == "MARK TO COPY"
    finally:
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_empty_marks_use_a_wrapping_document(config, tmp_path):
    page, controller, history, _record = _page(config, tmp_path)
    try:
        _live(page, controller, history)
        page._render_live()
        assert page._marks_view.placeholderText() == ""
        assert "标记此刻" in page._marks_view.toPlainText()
        assert "纪要会优先覆盖" in page._marks_view.toPlainText()
        assert page._marks_view.document().blockCount() >= 2 or "\n" in page._marks_view.toPlainText()
    finally:
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_saved_notes_marks_and_agenda_remain_available(config, tmp_path):
    page, controller, history, record = _page(config, tmp_path)
    try:
        controller.update_meeting(record.id, notes_user="我的会中笔记")
        controller.store.add_mark(record.id, 1500, "关键标记")
        controller.store.flush()
        page.refresh()
        assert page._materials.isVisible()
        assert not page._materials_body.isVisible()
        page._materials_toggle.click()
        assert page._materials_notes_label.text() == "我的笔记"
        assert page._materials_marks_label.isVisible()
        assert page._record_notes.toPlainText() == "我的会中笔记"
        assert "关键标记" in page._record_marks.toPlainText()
        assert "确认范围" in page._materials_agenda.text()
        exported = controller.export_markdown(record.id)
        assert "## 议程" in exported and "确认范围" in exported
        assert "## 我的笔记" in exported and "我的会中笔记" in exported
        assert "## 标记" in exported and "关键标记" in exported
        assert "## 文字稿" in exported
    finally:
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_failed_restore_does_not_claim_success(config, tmp_path, monkeypatch):
    page, controller, history, record = _page(config, tmp_path)
    try:
        monkeypatch.setattr(controller.store, "restore_previous_notes", lambda _meeting_id: None)
        page._restore_previous()
        assert controller.store.get_notes(record.id).markdown == "第二版纪要"
        assert "已恢复" not in page._status_label.text()
        assert "尚未恢复" in page._status_label.text()
    finally:
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_successful_restore_still_announces_the_swap(config, tmp_path):
    page, controller, history, record = _page(config, tmp_path)
    try:
        page._restore_previous()
        assert controller.store.get_notes(record.id).markdown == "第一版纪要"
        assert "已恢复上一版" in page._status_label.text()
    finally:
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_title_stays_when_the_write_does_not_land(config, tmp_path, monkeypatch):
    page, controller, history, record = _page(config, tmp_path)
    try:
        monkeypatch.setattr(controller, "update_meeting", lambda *args, **kwargs: None)
        monkeypatch.setattr(controller.store, "flush", lambda timeout=10: False)
        page._detail_title.setText("用户刚输入的标题")
        page._detail_title.clearFocus()
        page._save_title()
        assert page._detail_title.text() == "用户刚输入的标题"
        assert controller.store.get_meeting(record.id).title == "设计评审"
        assert "尚未保存" in page._status_label.text()
    finally:
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_download_buttons_use_the_same_action_style(config, tmp_path):
    page, controller, history, _record = _page(config, tmp_path)
    try:
        assert page._card_dl_btn.styleSheet() == ss.BTN_GHOST_SM
        assert page._live_dl_btn.styleSheet() == ss.BTN_GHOST_SM
    finally:
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_template_editor_says_the_list_is_not_saved_yet():
    editor = TemplateEditorDialog(get_template("general"))
    button = editor.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Save)
    assert button.text() == "加入待保存列表"
    labels = [label.text() for label in editor.findChildren(QLabel)]
    assert any("保存并使用所选模板" in text for text in labels)
    editor.deleteLater()


def test_notes_do_not_leak_onto_the_next_meeting(config, tmp_path):
    page, controller, history, record = _page(config, tmp_path)
    try:
        controller.update_meeting(record.id, notes_user="只属于这场")
        controller.store.flush()
        page.refresh()
        other = controller.store.create_meeting(title="另一场", started_at=record.started_at + 10_000)
        controller.store.end_meeting(other.id, ended_at=other.started_at + 1000)
        controller.store.flush()
        page.show_meeting(other.id)
        page._save_record_notes()
        assert controller.store.get_meeting(other.id).notes_user == ""
        assert controller.store.get_meeting(record.id).notes_user == "只属于这场"
    finally:
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_refresh_keeps_notes_that_have_not_been_saved_yet(config, tmp_path):
    page, controller, history, _record = _page(config, tmp_path)
    try:
        assert page._materials.isVisible()
        page._record_notes.setPlainText("正在输入")
        page._record_notes.clearFocus()
        assert page._record_notes_timer.isActive()
        page.refresh()
        assert page._record_notes.toPlainText() == "正在输入"
    finally:
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_failed_record_note_save_stays_on_this_meeting(config, tmp_path, monkeypatch):
    page, controller, history, record = _page(config, tmp_path)
    try:
        other = controller.store.create_meeting(title="另一场", started_at=record.started_at + 10_000)
        controller.store.end_meeting(other.id, ended_at=other.started_at + 1000)
        controller.store.flush()
        page.refresh()
        page._record_notes.setPlainText("不能丢掉的笔记")
        page._record_notes.clearFocus()
        monkeypatch.setattr(controller, "update_meeting", lambda *args, **kwargs: None)
        monkeypatch.setattr(controller.store, "flush", lambda timeout=10: False)
        page.show_meeting(other.id)
        assert page.current_meeting_id == record.id
        assert page._record_notes.toPlainText() == "不能丢掉的笔记"
        assert controller.store.get_meeting(record.id).notes_user in ("", None)
        assert "笔记尚未保存" in page._status_label.text()
        page.refresh()
        assert page._record_notes.toPlainText() == "不能丢掉的笔记"
    finally:
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_export_keeps_notes_that_have_not_reached_the_database(config, tmp_path, monkeypatch):
    page, controller, history, record = _page(config, tmp_path)
    try:
        page._record_notes.setPlainText("导出前的笔记")
        dest = tmp_path / "out.md"
        monkeypatch.setattr(
            "sayink.ui.meeting_page.QFileDialog.getSaveFileName",
            lambda *args, **kwargs: (str(dest), "Markdown (*.md)"),
        )
        page._export("all")
        assert "导出前的笔记" in dest.read_text(encoding="utf-8")
        assert controller.store.get_meeting(record.id).notes_user == "导出前的笔记"

        page._record_notes.setPlainText("还没写进去")
        monkeypatch.setattr(controller, "update_meeting", lambda *args, **kwargs: None)
        monkeypatch.setattr(controller.store, "flush", lambda timeout=10: False)
        opened = {}
        monkeypatch.setattr(
            "sayink.ui.meeting_page.QFileDialog.getSaveFileName",
            lambda *args, **kwargs: opened.setdefault("path", "should-not-open"),
        )
        page._export("all")
        assert "path" not in opened
        assert page._record_notes.toPlainText() == "还没写进去"
        assert "笔记尚未保存" in page._status_label.text()
    finally:
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_the_open_meeting_stays_selected_after_a_newer_one_arrives(config, tmp_path):
    page, controller, history, record = _page(config, tmp_path)
    try:
        for n in range(101):
            controller.store.create_meeting(title=f"稍后 {n}", started_at=record.started_at + 100_000 + n)
        controller.store.flush()
        page.show_meeting(record.id)
        assert page.current_meeting_id == record.id
        controller.store.create_meeting(title="更新的一场", started_at=record.started_at + 900_000)
        controller.store.flush()
        page.refresh()
        assert page.current_meeting_id == record.id
        assert page._detail_title.text() == "设计评审"
    finally:
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_exit_reports_a_minutes_draft_that_could_not_be_saved(config, tmp_path, monkeypatch):
    page, controller, history, _record = _page(config, tmp_path)
    try:
        page._toggle_edit()
        page._notes_edit.setPlainText("不能丢的草稿")
        monkeypatch.setattr(controller, "save_user_markdown", lambda *_args: False)
        assert page.unsaved_exit_draft() == "不能丢的草稿"
        assert page._editing_notes
    finally:
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_external_end_with_failed_live_note_save_lands_on_browse_with_visible_warning(
    config, tmp_path, monkeypatch
):
    page, controller, history, _record = _page(config, tmp_path)
    try:
        live = _live(page, controller, history)
        QApplication.processEvents()
        assert page._stack.currentIndex() == PAGE_LIVE
        page._user_notes.setPlainText("托盘结束前的随手笔记")
        monkeypatch.setattr(controller, "update_meeting", lambda *args, **kwargs: None)
        monkeypatch.setattr(controller.store, "flush", lambda timeout=10: False)
        controller.request_end()
        QApplication.processEvents()

        assert controller.state == MeetingState.IDLE
        assert page._stack.currentIndex() == PAGE_BROWSE
        assert page.current_meeting_id == live.id
        assert page._materials.isVisible()
        assert page._record_notes.toPlainText() == "托盘结束前的随手笔记"
        assert page._status_label.isVisible()
        assert "笔记尚未保存" in page._status_label.text()
    finally:
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_failed_record_note_save_blocks_start_card_search_and_external_clobber(
    config, tmp_path, monkeypatch
):
    page, controller, history, record = _page(config, tmp_path)
    try:
        orig_update = controller.update_meeting
        orig_flush = controller.store.flush
        page._record_notes.setPlainText("会后未落盘笔记")
        page._record_notes.clearFocus()
        monkeypatch.setattr(controller, "update_meeting", lambda *args, **kwargs: None)
        monkeypatch.setattr(controller.store, "flush", lambda timeout=10: False)

        page.show_start_card()
        assert page._stack.currentIndex() == PAGE_BROWSE
        assert page.current_meeting_id == record.id
        assert page._record_notes.toPlainText() == "会后未落盘笔记"
        assert "笔记尚未保存" in page._status_label.text()

        page._search_edit.setText("过滤词")
        page._perform_search()
        assert page._search_query == ""
        assert page._search_edit.text() == ""
        assert page.current_meeting_id == record.id
        assert page._record_notes.toPlainText() == "会后未落盘笔记"

        live = _live(page, controller, history)
        QApplication.processEvents()
        page._user_notes.setPlainText("第二场实时笔记")
        controller.request_end()
        QApplication.processEvents()

        assert page._unsaved_live_notes.get(live.id) == "第二场实时笔记"
        assert page._record_notes_meeting_id == record.id
        page.show_meeting(record.id)
        assert page._record_notes.toPlainText() == "会后未落盘笔记"
        assert page.unsaved_exit_draft() is not None

        monkeypatch.setattr(controller, "update_meeting", orig_update)
        monkeypatch.setattr(controller.store, "flush", orig_flush)
        assert page.try_leave() is True
        assert controller.store.get_meeting(record.id).notes_user == "会后未落盘笔记"
        assert controller.store.get_meeting(live.id).notes_user == "第二场实时笔记"
        assert not page._unsaved_live_notes
    finally:
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_save_title_is_optimistic_without_blocking_ui_on_store_flush(
    config, tmp_path, monkeypatch
):
    page, controller, history, record = _page(config, tmp_path)
    try:
        def _forbid_flush(*_args, **_kwargs):
            raise AssertionError("UI thread must not call store.flush during _save_title")

        monkeypatch.setattr(controller.store, "flush", _forbid_flush)
        page._detail_title.setText("异步不阻塞新标题")
        page._save_title()

        assert page._detail_title.text() == "异步不阻塞新标题"
        page.refresh()
        assert page._detail_title.text() == "异步不阻塞新标题"

        assert history.flush(timeout=5.0)
        QApplication.processEvents()
        assert controller.store.get_meeting(record.id).title == "异步不阻塞新标题"
        assert page._pending_title is None
    finally:
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_single_segment_edit_patches_paragraph_in_place_and_keeps_anchors(
    config, tmp_path, monkeypatch
):
    page, controller, history, record = _page(config, tmp_path)
    try:
        long_rec = controller.store.create_meeting(
            title="长文字稿局部修补", started_at=record.started_at + 50_000
        )
        for i in range(320):
            history.enqueue(SegmentRecord(
                long_rec.id,
                i,
                long_rec.started_at + i * 1000,
                raw_text=f"核对第{i}段，这里是一句用于局部修补验证的示例发言。",
                speaker_id=(i % 2) + 1,
                trigger_mode="meeting",
                captured_at=long_rec.started_at + i * 1000,
            ))
        controller.store.end_meeting(long_rec.id, ended_at=long_rec.started_at + 400_000)
        controller.store.flush()

        page.show_meeting(long_rec.id)
        page._switch_tab(1)
        QApplication.processEvents()

        cursor = page._transcript_view.document().find("核对第5段")
        page._transcript_view.setTextCursor(cursor)
        assert page._transcript_view.textCursor().selectedText() == "核对第5段"

        orig_set_html = page._transcript_view.setHtml
        full_rebuilds = []

        def _counted_set_html(html):
            full_rebuilds.append(len(html))
            return orig_set_html(html)

        monkeypatch.setattr(page._transcript_view, "setHtml", _counted_set_html)
        assert controller.save_segment_text(long_rec.id, 180, "第180段已原地修补")
        page._render_detail()
        QApplication.processEvents()

        assert full_rebuilds == []
        plain = page._transcript_view.toPlainText()
        assert "第180段已原地修补" in plain
        assert "已编辑" in plain
        assert page._transcript_view.textCursor().selectedText() == "核对第5段"

        patched_block = page._transcript_view.document().find("第180段已原地修补").block()
        assert patched_block.isValid()
        assert patched_block.layout() is not None
        assert patched_block.layout().boundingRect().height() > 0
        it = patched_block.begin()
        action_alphas = []
        while not it.atEnd():
            frag = it.fragment()
            if frag.isValid():
                fmt = frag.charFormat()
                href = fmt.anchorHref()
                if fmt.isAnchor() and href.startswith(("edit-segment:", "details-segment:")):
                    action_alphas.append(fmt.foreground().color().alpha())
            it += 1
        assert action_alphas and all(alpha > 0 for alpha in action_alphas)

        page._transcript_view.scrollToAnchor("seg180")
        page._transcript_view.place_cursor_at_segment(180)
        assert page._transcript_view.textCursor().position() >= patched_block.position()
    finally:
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_meeting_page_reexports_views_and_draft_buffer_state(tmp_path):
    from sayink.ui import meeting_page, meeting_views
    from sayink.ui.meeting_drafts import MeetingDraftBuffer

    assert meeting_page._ReadingView is meeting_views._ReadingView
    assert meeting_page._TranscriptView is meeting_views._TranscriptView
    assert meeting_page._SegmentSpeakerDialog is meeting_views._SegmentSpeakerDialog
    assert meeting_page._SegmentTextDialog is meeting_views._SegmentTextDialog
    assert meeting_page._CurrentPageStack is meeting_views._CurrentPageStack
    assert meeting_page.speaker_color is meeting_views.speaker_color

    history = HistoryStore(db_path=tmp_path / "drafts.db")
    try:
        from sayink.meeting_store import MeetingStore

        store = MeetingStore(history)
        rec = store.create_meeting(title="原名")
        store.flush()

        buf = MeetingDraftBuffer()
        buf.set_pending_template(rec.id, "refinement")
        assert buf.effective_template_id(rec.id, "general") == "refinement"
        assert buf.pop_pending_template(rec.id) == "refinement"
        assert buf.effective_template_id(rec.id, "general") == "general"

        tok_id = buf.begin_title_save(rec.id, "乐观新名")
        assert buf.matches_pending_title(rec.id, "乐观新名", tok_id)
        updated = buf.apply_optimistic_titles([rec])
        assert updated[0].title == "乐观新名"

        assert buf.enter_live_session(rec.id) is True
        buf.live_html = "<p>live</p>"
        assert buf.enter_live_session(rec.id) is False
        assert buf.live_html == "<p>live</p>"
        assert buf.enter_live_session("other-live") is True
        assert buf.live_html == ""

        buf.stash_live_note_for_other_meeting(store, rec.id, "暂存实时笔记")
        assert buf.pop_promotable_live_note(store, rec.id, "") == "暂存实时笔记"
        assert buf.record_notes_dirty is True
    finally:
        history.close(timeout=2)


def test_record_notes_autosave_and_clean_leave_do_not_block_on_store_flush(
    config, tmp_path, monkeypatch
):
    page, controller, history, record = _page(config, tmp_path)
    real_flush = controller.store.flush
    try:
        def _forbid_flush(*_args, **_kwargs):
            raise AssertionError("Routine autosave / already-synced leave must not call store.flush")

        monkeypatch.setattr(controller.store, "flush", _forbid_flush)
        page._record_notes.setPlainText("异步自动保存且不卡界面")
        assert page._record_notes_timer.isActive()
        page._record_notes_timer.stop()
        page._autosave_record_notes()

        assert history.flush(timeout=5.0)
        QApplication.processEvents()
        assert controller.store.get_meeting(record.id).notes_user == "异步自动保存且不卡界面"
        assert page._drafts.record_notes_dirty is False

        # Because SQLite already holds the latest notes, leaving never calls store.flush().
        assert page.try_leave() is True

        # Same non-blocking fast path for live notes once autosave has landed.
        monkeypatch.setattr(controller.store, "flush", real_flush)
        live = _live(page, controller, history)
        monkeypatch.setattr(controller.store, "flush", _forbid_flush)
        page._user_notes.setPlainText("实时已落盘笔记")
        page._user_notes_timer.stop()
        page._save_user_notes()
        assert history.flush(timeout=5.0)
        assert controller.store.get_meeting(live.id).notes_user == "实时已落盘笔记"
        assert page.flush_pending_user_notes() is True
    finally:
        monkeypatch.setattr(controller.store, "flush", real_flush)
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)

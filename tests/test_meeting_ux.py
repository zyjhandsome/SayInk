"""Interaction contracts for meeting review, edits, exports and corrections."""
from __future__ import annotations

import sqlite3

import pytest
from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtWidgets import QApplication, QDialog, QFileDialog

from sayink.history_store import HistoryStore, SegmentRecord
from sayink.meeting_session import MeetingController
from sayink.meeting_store import MeetingStore
from sayink.ui.meeting_page import MeetingPage, notes_for_display


@pytest.fixture
def meeting(config, tmp_path):
    config.set("llm.api_url", "http://localhost:11434/v1")
    config.set("llm.model_name", "local-model")
    history = HistoryStore(db_path=tmp_path / "meetings.db")
    controller = MeetingController(config, history, pending_segments=lambda: 0)
    record = controller.store.create_meeting(title="设计评审")
    for seq, speaker_id, text in ((2, 1, "采用方案 A"), (7, 2, "下周开始验证"), (9, 2, "完成后再发布")):
        history.enqueue(SegmentRecord(record.id, seq, record.started_at + seq * 1000,
                                      raw_text=text, speaker_id=speaker_id, trigger_mode="meeting",
                                      captured_at=record.started_at + seq * 1000))
    controller.store.end_meeting(record.id, ended_at=record.started_at + 85_000)
    controller.store.save_notes(record.id, template_id="general", model="m", json_payload="",
                                markdown="# 设计评审\n\n## 摘要\n采用方案 A。[00:00:02]\n", duration_ms=1)
    controller.store.flush()
    page = MeetingPage(config, controller)
    page.show_meeting(record.id)
    yield page, controller, history, record
    page.deleteLater()
    controller.shutdown()
    history.close(timeout=2)


def test_copy_tracks_tab_and_works_without_minutes(meeting, monkeypatch):
    page, controller, _history, record = meeting
    copied = []
    monkeypatch.setattr("sayink.ui.meeting_page.copy_text", lambda text: copied.append(text) or True)
    page._copy_btn.click()
    assert copied[-1].startswith("# 设计评审")
    page._tab_transcript.click()
    assert page._copy_btn.text() == "复制文字稿"
    page._copy_btn.click()
    assert copied[-1] == controller.transcript_text(record.id)
    assert "说话人2：下周开始验证" in copied[-1]
    controller.store.save_notes(record.id, template_id="general", model="m", json_payload="", markdown="")
    controller.store.flush()
    page._render_detail()
    assert page._copy_btn.isEnabled()
    page._tab_notes.click()
    assert not page._copy_btn.isEnabled()


def test_draft_copy_and_export_use_unsaved_text_without_saving_it(meeting, monkeypatch, tmp_path):
    page, controller, _history, record = meeting
    page._toggle_edit()
    page._notes_edit.setPlainText("当前草稿")
    copied = []
    monkeypatch.setattr("sayink.ui.meeting_page.copy_text", lambda text: copied.append(text) or True)
    page._copy_btn.click()
    assert page._copy_btn.text() == "复制草稿"
    assert copied == ["当前草稿"]
    for scope in ("notes", "transcript", "all"):
        out = tmp_path / f"{scope}.md"
        monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args, path=out: (str(path), "Markdown (*.md)"))
        page._export(scope)
    assert (tmp_path / "notes.md").read_text(encoding="utf-8") == "当前草稿"
    transcript = (tmp_path / "transcript.md").read_text(encoding="utf-8")
    assert "当前草稿" not in transcript and "## 文字稿" in transcript
    complete = (tmp_path / "all.md").read_text(encoding="utf-8")
    assert "当前草稿" in complete and "说话人1：采用方案 A" in complete
    assert "当前草稿" not in controller.store.get_notes(record.id).markdown
    page._toggle_edit()
    assert controller.store.get_notes(record.id).markdown == "当前草稿"


def test_template_choice_survives_refresh_and_is_used_for_generation(meeting, monkeypatch):
    page, controller, _history, record = meeting
    page._template_combo.setCurrentIndex(page._template_combo.findData("standup"))
    page.refresh()
    assert page._template_combo.currentData() == "standup"
    assert "下次生成" in page._content_hint.text()
    assert controller.store.get_notes(record.id).template_id == "general"
    called = []
    monkeypatch.setattr(page, "_confirm_regenerate", lambda: True)
    monkeypatch.setattr(controller, "generate", lambda mid, **kwargs: called.append((mid, kwargs)))
    page._toggle_edit()
    page._notes_edit.setPlainText("修改之后再生成")
    page._generate()
    assert called == [(record.id, {"template_id": "standup"})]
    assert controller.store.get_notes(record.id).markdown == "修改之后再生成"


def test_refinement_choice_changes_next_action_but_keeps_current_result_labels(meeting):
    page, controller, _history, record = meeting
    page._template_combo.setCurrentIndex(page._template_combo.findData("refine"))
    assert page._generate_btn.text() == "整理原文"
    assert page._copy_btn.text() == "复制纪要"
    assert page._tab_notes.text() == "纪要"
    assert "下次生成" in page._content_hint.text()
    assert controller.store.get_notes(record.id).template_id == "general"
    page.refresh()
    assert page._template_combo.currentData() == "refine"
    assert page._generate_btn.text() == "整理原文"


def test_refined_body_copy_export_edit_and_render_preserve_prose(meeting, monkeypatch, tmp_path):
    page, controller, _history, record = meeting
    body = "时间：下周再讨论。\n\n保留 <div>、API 和 * 原文符号。"
    controller.store.save_notes(record.id, template_id="refine", model="m", json_payload="", markdown=body)
    controller.store.flush()
    page._on_generation_finished(record.id)
    assert page._tab_notes.text() == "精整正文"
    assert page._copy_btn.text() == "复制精整正文"
    assert page._generate_btn.text() == "重新整理"
    assert page._export_actions["notes"].text() == "仅导出精整正文"
    assert "时间：下周再讨论。" in page._notes_view.toPlainText()
    assert "<div>" in page._notes_view.toPlainText()
    assert "* 原文符号" in page._notes_view.toPlainText()
    assert "原文已精整" in page._status_label.text()
    copied = []
    monkeypatch.setattr("sayink.ui.meeting_page.copy_text", lambda text: copied.append(text) or True)
    page._copy_btn.click()
    assert copied == [body]
    dialogs = []
    output = tmp_path / "refined.md"
    def choose(*args):
        dialogs.append(args)
        return str(output), "Markdown (*.md)"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", choose)
    page._export("notes")
    assert "精整正文" in dialogs[0][1] and "精整正文" in dialogs[0][2]
    assert output.read_text(encoding="utf-8") == body
    page._template_combo.setCurrentIndex(page._template_combo.findData("general"))
    assert page._copy_btn.text() == "复制精整正文"
    assert page._generate_btn.text() == "重新生成"
    page._toggle_edit()
    page._notes_edit.setPlainText("精整正文的手动修改。")
    assert page._copy_btn.text() == "复制草稿"
    page._toggle_edit()
    assert controller.store.get_notes(record.id).markdown == "精整正文的手动修改。"


def test_cross_template_restore_swaps_body_and_metadata_and_survives_rerender(meeting):
    page, controller, _history, record = meeting
    before = controller.store.get_notes(record.id)
    controller.store.save_notes(record.id, template_id="refine", model="other", json_payload='{"kind":"refine","paragraphs":[]}',
                                markdown="精整正文。", generated_at=before.generated_at + 100, chunked=True)
    controller.store.flush()
    page._on_generation_finished(record.id)
    assert page._restore_action.text() == "恢复上一版纪要"
    # Rerendering the current version must not overwrite the real backup.
    current = controller.store.get_notes(record.id)
    controller.store.save_notes(record.id, template_id=current.template_id, model=current.model,
                                json_payload=current.json_payload, markdown=current.markdown, keep_previous=True,
                                generated_at=current.generated_at, chunked=True)
    controller.store.flush()
    page._restore_previous()
    restored = controller.store.get_notes(record.id)
    assert restored.template_id == "general" and restored.model == before.model
    assert restored.markdown == before.markdown and restored.generated_at == before.generated_at
    assert restored.json_payload == before.json_payload and not restored.chunked
    assert page._copy_btn.text() == "复制纪要"
    assert page._template_combo.currentData() == "general"
    assert page._restore_action.text() == "恢复上一版精整正文"
    page._restore_previous()
    restored = controller.store.get_notes(record.id)
    assert restored.template_id == "refine" and restored.model == "other" and restored.chunked
    assert page._copy_btn.text() == "复制精整正文" and page._generate_btn.text() == "重新整理"


def test_refinement_dispatch_uses_raw_transcript_and_failure_preserves_saved_result(meeting, monkeypatch):
    _page, controller, history, record = meeting
    history.enqueue(SegmentRecord(record.id, 20, record.started_at + 20_000, raw_text="原文不能删。",
                                  polished_text="改过的文字。", trigger_mode="meeting", speaker_id=1))
    history.flush()
    seen = []
    monkeypatch.setattr(controller._generator, "generate", seen.append)
    assert controller.generate(record.id, template_id="refine")
    assert seen[0].lines[-1].text == "原文不能删。"
    before = controller.store.get_notes(record.id)
    controller._on_generation_error("第2段未通过完整性检查")
    assert controller.store.get_notes(record.id) == before


def test_generation_locks_edit_template_and_restore_then_unlocks_on_failure(meeting):
    page, controller, _history, record = meeting
    controller.store.save_notes(record.id, template_id="refine", model="m", json_payload="", markdown="精整正文。")
    controller.store.flush()
    controller._generating_for = record.id
    controller.generation_progress.emit(record.id, "正在整理原文…")
    assert not page._template_combo.isEnabled()
    assert not page._edit_btn.isEnabled()
    assert not page._restore_action.isEnabled()
    assert not page._generate_btn.isEnabled()
    page._restore_previous()
    assert controller.store.get_notes(record.id).template_id == "refine"
    controller._on_generation_error("本次结果未保存")
    assert page._template_combo.isEnabled() and page._edit_btn.isEnabled()
    assert page._restore_action.isEnabled() and page._generate_btn.isEnabled()


def test_switching_meeting_saves_draft_to_its_original_record(meeting):
    page, controller, _history, record = meeting
    other = controller.store.create_meeting(title="另一场")
    controller.store.end_meeting(other.id)
    controller.store.flush()
    page._toggle_edit()
    page._notes_edit.setPlainText("属于第一场的修改")
    page.refresh()
    other_item = next(page._list.item(i) for i in range(page._list.count())
                      if page._list.item(i).data(Qt.ItemDataRole.UserRole) == other.id)
    page._list.setCurrentItem(other_item)
    assert controller.store.get_notes(record.id).markdown == "属于第一场的修改"
    assert controller.store.get_notes(other.id) is None
    assert page.current_meeting_id == other.id


def test_save_failure_preserves_draft_and_meeting_selection(meeting, monkeypatch):
    page, controller, _history, record = meeting
    other = controller.store.create_meeting(title="另一场")
    controller.store.end_meeting(other.id)
    controller.store.flush()
    page.refresh()
    page._toggle_edit()
    page._notes_edit.setPlainText("必须保留的草稿")
    monkeypatch.setattr(controller, "save_user_markdown", lambda *_args: False)
    other_item = next(page._list.item(i) for i in range(page._list.count())
                      if page._list.item(i).data(Qt.ItemDataRole.UserRole) == other.id)
    page._list.setCurrentItem(other_item)
    assert page.current_meeting_id == record.id
    assert page._editing_notes
    assert page._notes_edit.toPlainText() == "必须保留的草稿"
    assert "尚未保存" in page._status_label.text()


def test_single_segment_correction_keeps_other_rows_and_marks_minutes(meeting):
    page, controller, history, record = meeting
    history.enqueue(SegmentRecord(record.id, 7, record.started_at, raw_text="听写隔离", trigger_mode="hotkey", speaker_id=2))
    history.flush()
    controller.rename_speaker(record.id, 1, "小林")
    assert controller.correct_segment_speaker(record.id, 7, 1)
    segments = controller.store.get_segments(record.id)
    assert [(s.seq, s.speaker_id) for s in segments] == [(2, 1), (7, 1), (9, 2)]
    assert controller.store.get_notes(record.id).transcript_changed
    assert controller.speaker_names(record.id) == {1: "小林"}
    dictation = [s for s in history.get_session_segments(record.id) if s.trigger_mode == "hotkey"]
    assert dictation[0].speaker_id == 2
    page._render_detail()
    assert "需核对" in page._notes_state.text()
    page._correct_speaker_btn.click()
    assert "segment:7" in page._transcript_view.toHtml()
    assert "segment:1\"" not in page._transcript_view.toHtml()


def test_correction_is_refused_during_live_recording_or_generation(meeting):
    _page, controller, history, record = meeting
    controller._generating_for = record.id
    assert not controller.correct_segment_speaker(record.id, 7, 1)
    controller._generating_for = ""
    live = controller.store.create_meeting()
    history.enqueue(SegmentRecord(live.id, 0, live.started_at, raw_text="仍在录制", trigger_mode="meeting", speaker_id=2))
    controller.store.flush()
    assert not controller.correct_segment_speaker(live.id, 0, 1)


def test_correction_link_applies_only_the_selected_segment(meeting, monkeypatch):
    page, controller, _history, record = meeting

    def choose(dialog):
        dialog.speaker_combo.setCurrentIndex(dialog.speaker_combo.findData(1))
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr("sayink.ui.meeting_page._SegmentSpeakerDialog.exec", choose)
    page._tab_transcript.click()
    page._correct_speaker_btn.click()
    page._on_transcript_link(QUrl("segment:7"))
    assert [(s.seq, s.speaker_id) for s in controller.store.get_segments(record.id)] == [(2, 1), (7, 1), (9, 2)]
    assert "已更正这一段" in page._status_label.text()
    assert "需核对" in page._notes_state.text()


def test_controller_detects_failed_draft_write(meeting, monkeypatch):
    page, controller, _history, record = meeting
    original = controller.store.get_notes(record.id).markdown
    monkeypatch.setattr(controller.store, "save_user_markdown", lambda *_args: None)
    page._toggle_edit()
    page._notes_edit.setPlainText("未能写入的草稿")
    page._toggle_edit()
    assert page._editing_notes
    assert page._notes_edit.toPlainText() == "未能写入的草稿"
    assert controller.store.get_notes(record.id).markdown == original


def test_stale_status_survives_regeneration_and_version_restore(meeting):
    _page, controller, _history, record = meeting
    assert controller.correct_segment_speaker(record.id, 7, 1)
    controller.store.save_notes(record.id, template_id="general", model="m", json_payload="", markdown="新纪要")
    controller.store.flush()
    notes = controller.store.get_notes(record.id)
    assert not notes.transcript_changed and notes.previous_transcript_changed
    controller.restore_previous_notes(record.id)
    assert controller.store.get_notes(record.id).transcript_changed
    controller.restore_previous_notes(record.id)
    assert not controller.store.get_notes(record.id).transcript_changed


def test_note_review_flags_upgrade_an_existing_database_without_changing_text(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE meeting_notes (
            meeting_id TEXT PRIMARY KEY, template_id TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '', json_payload TEXT NOT NULL DEFAULT '',
            markdown TEXT NOT NULL DEFAULT '', previous_markdown TEXT NOT NULL DEFAULT '',
            user_edited INTEGER NOT NULL DEFAULT 0, generated_at INTEGER NOT NULL DEFAULT 0,
            duration_ms INTEGER NOT NULL DEFAULT 0, chunked INTEGER NOT NULL DEFAULT 0)""")
        conn.execute("INSERT INTO meeting_notes (meeting_id, markdown, previous_markdown) VALUES (?, ?, ?)",
                     ("legacy", "保留当前纪要", "保留上一版"))
    history = HistoryStore(db_path=path)
    try:
        notes = MeetingStore(history).get_notes("legacy")
        assert notes.markdown == "保留当前纪要" and notes.previous_markdown == "保留上一版"
        assert not notes.transcript_changed and not notes.previous_transcript_changed
    finally:
        history.close(timeout=2)


def test_citation_return_restores_position_and_refresh_keeps_reading(meeting):
    page, controller, _history, record = meeting
    text = "\n\n".join(f"## 议题 {index}\n" + "这是一段需要核对的讨论内容。" * 12 for index in range(30))
    controller.store.save_user_markdown(record.id, text)
    controller.store.flush()
    page._render_detail()
    page.resize(960, 640)
    page.show()
    for _ in range(6):
        QApplication.processEvents()
    bar = page._notes_view.verticalScrollBar()
    assert bar.maximum() > 500
    bar.setValue(420)
    page._on_notes_link(QUrl("ts:7"))
    assert page._return_notes_btn.isVisibleTo(page)
    assert page._transcript_highlight == 1
    page._return_notes_btn.click()
    for _ in range(4):
        QApplication.processEvents()
    assert bar.value() == 420
    page.refresh()
    QApplication.processEvents()
    assert bar.value() == 420


def test_only_empty_decision_sections_are_compacted_for_display():
    markdown = "## 摘要\n记录。\n\n## 决策\n\n本场未识别到\n\n## 待办\n\n本场未识别到\n\n## 遗留问题\n需要评审。\n"
    shown = notes_for_display(markdown, "会议")
    assert "## 决策" not in shown and "## 待办" not in shown
    assert "决策 / 待办：暂未提取到明确内容" in shown
    assert "需要评审。" in shown
    assert "通过方案" in notes_for_display("## 决策\n通过方案。\n", "会议")

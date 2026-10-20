"""Speaker edits during capture, and their survival after automatic regrouping."""
from __future__ import annotations

import sqlite3
from dataclasses import replace

import numpy as np
import pytest
from PyQt6.QtCore import QUrl
from PyQt6.QtWidgets import QDialog

from sayink.history_store import HistoryStore, SegmentRecord, _DDL
from sayink.meeting_session import MeetingController, MeetingOptions, MeetingState, resolve_capabilities
from sayink.meeting_store import MeetingStore
from sayink.ui.meeting_page import MeetingPage, _SegmentSpeakerDialog


def _unit(*values):
    vector = np.array(values, dtype=np.float32)
    return vector / np.linalg.norm(vector)


@pytest.fixture
def live_meeting(config, tmp_path):
    history = HistoryStore(db_path=tmp_path / "live.db")
    pending = {"n": 0}
    controller = MeetingController(config, history, pending_segments=lambda: pending["n"])
    controller._test_pending = pending
    options = MeetingOptions(title="会中纠错", vad="energy")
    caps = resolve_capabilities(config, options, dictation_model_id="funasr-nano")
    record = controller.begin(options, replace(caps, speaker_model_available=False))
    for seq, vector, text in ((2, _unit(1, 0, 0), "采用方案 A"),
                              (7, _unit(0, 1, 0), "周三验证"),
                              (11, _unit(1, 0.6, 0), "接着讨论下一步")):
        sid = controller.speakers.assign(vector)
        history.enqueue(SegmentRecord(record.id, seq, record.started_at + seq * 1000,
                                      raw_text=text, speaker_id=sid, trigger_mode="meeting",
                                      captured_at=record.started_at + seq * 1000))
    assert history.flush()
    page = MeetingPage(config, controller)
    yield page, controller, history, record
    page.deleteLater()
    controller.shutdown()
    history.close(timeout=2)


def test_live_naming_updates_existing_and_future_lines_without_stopping_capture(live_meeting, monkeypatch):
    page, controller, history, record = live_meeting
    page.show()
    monkeypatch.setattr("sayink.ui.meeting_page.QInputDialog.getText", lambda *_args: ("小林", True))
    page._populate_live_speaker_menu()
    page._live_speaker_menu.actions()[0].trigger()
    assert controller.speaker_names(record.id) == {1: "小林"}
    assert "小林" in page._live_transcript.toPlainText()
    assert "live-speaker:1" in page._live_transcript.toHtml()
    assert "已将说话人1" in page._live_speaker_feedback.text()
    sid = controller.speakers.assign(_unit(1, 0, 0))
    history.enqueue(SegmentRecord(record.id, 15, record.started_at + 15_000, raw_text="同一位继续发言",
                                  speaker_id=sid, trigger_mode="meeting"))
    assert history.flush()
    page._render_live()
    assert sid == 1 and controller.state is MeetingState.LIVE
    assert "同一位继续发言" in page._live_transcript.toPlainText()
    controller.finalize()
    assert controller.speaker_names(record.id)[1] == "小林"
    assert "小林" in controller.export_transcript_markdown(record.id)


def test_distinct_manual_names_cannot_be_merged_or_overwritten_on_finalize(live_meeting):
    _page, controller, _history, record = live_meeting
    assert controller.rename_speaker(record.id, 1, "小林") is not None
    assert controller.rename_speaker(record.id, 3, "小周") is not None
    controller.finalize()
    assert controller.speaker_names(record.id) == {1: "小林", 3: "小周"}
    assert [s.speaker_id for s in controller.store.get_segments(record.id)] == [1, 2, 3]


def test_live_correction_locks_only_the_selected_segment_through_finalize(live_meeting, monkeypatch):
    page, controller, history, record = live_meeting
    history.enqueue(SegmentRecord(record.id, 5, record.started_at, raw_text="", speaker_id=1, trigger_mode="meeting"))
    history.enqueue(SegmentRecord(record.id, 11, record.started_at, raw_text="普通听写", speaker_id=3, trigger_mode="hotkey"))
    assert history.flush()

    def choose(dialog):
        dialog.speaker_combo.setCurrentIndex(dialog.speaker_combo.findData(2))
        dialog.save_btn.click()
        return dialog.result()

    monkeypatch.setattr(_SegmentSpeakerDialog, "exec", choose)
    page._live_correct_speaker_btn.click()
    assert "live-segment:11" in page._live_transcript.toHtml()
    assert "live-segment:5" not in page._live_transcript.toHtml()
    page._on_live_transcript_link(QUrl("live-segment:11"))
    assert "归属已更正" in page._live_transcript.toPlainText()
    assert controller.state is MeetingState.LIVE
    sid = controller.speakers.assign(_unit(1, 0.6, 0))
    history.enqueue(SegmentRecord(record.id, 15, record.started_at, raw_text="后续仍自动分组",
                                  speaker_id=sid, trigger_mode="meeting"))
    assert history.flush()
    controller.finalize()
    rows = {s.seq: s for s in controller.store.get_segments(record.id)}
    assert rows[11].speaker_id == 2 and rows[11].speaker_locked
    assert rows[15].speaker_id == 1 and not rows[15].speaker_locked
    assert not rows[2].speaker_locked and rows[2].speaker_id == 1
    dictation = next(s for s in history.get_session_segments(record.id) if s.trigger_mode == "hotkey")
    assert dictation.speaker_id == 3 and not dictation.speaker_locked


def test_later_named_group_keeps_its_identity_when_earlier_group_merges_into_it(live_meeting):
    _page, controller, _history, record = live_meeting
    assert controller.rename_speaker(record.id, 3, "小林") is not None
    controller.finalize()
    rows = controller.store.get_segments(record.id)
    assert [s.speaker_id for s in rows] == [3, 2, 3]
    assert controller.speaker_names(record.id) == {3: "小林"}


def test_new_manual_identity_is_not_reused_by_later_automatic_assignment(live_meeting):
    _page, controller, history, record = live_meeting
    assert controller.correct_segment_speaker(record.id, 7, None)
    manual = next(s.speaker_id for s in controller.store.get_segments(record.id) if s.seq == 7)
    assert controller.speakers.speaker_count == 3
    automatic = controller.speakers.assign(_unit(0, 0, 1))
    assert automatic > manual
    history.enqueue(SegmentRecord(record.id, 15, record.started_at, raw_text="另一位发言",
                                  speaker_id=automatic, trigger_mode="meeting"))
    assert history.flush()
    assert controller.rename_speaker(record.id, manual, "手动新增") is not None
    controller.finalize()
    rows = {s.seq: s for s in controller.store.get_segments(record.id)}
    assert rows[7].speaker_id == manual and rows[7].speaker_locked
    assert rows[15].speaker_id == automatic
    assert controller.speaker_names(record.id)[manual] == "手动新增"


def test_saved_live_correction_teaches_the_next_clear_utterance(live_meeting):
    _page, controller, history, record = live_meeting
    voice = _unit(0, 0, 1)
    previous = controller.speakers.assign(voice, sample_key=23, learnable=True)
    history.enqueue(SegmentRecord(record.id, 23, record.started_at, raw_text="清晰的发言片段",
                                  speaker_id=previous, trigger_mode="meeting"))
    assert history.flush()
    assert controller.correct_segment_speaker(record.id, 23, 2)
    assert controller.speakers.assign(voice, sample_key=24, learnable=True) == 2
    assert not any(sid == previous for sid, _ in controller.speakers.history)


def test_unknown_turn_can_be_corrected_without_becoming_speaker_one_by_default(live_meeting, monkeypatch):
    page, controller, history, record = live_meeting
    sid = controller.speakers.assign(np.zeros(1), sample_key=23, learnable=False)
    assert sid == 0
    history.enqueue(SegmentRecord(record.id, 23, record.started_at, raw_text="好的",
                                  speaker_id=sid, trigger_mode="meeting"))
    assert history.flush()
    page._render_live()
    assert "说话人待确认" in page._live_transcript.toPlainText()
    assert "说话人待确认：好的" in controller.export_transcript_markdown(record.id)

    def choose(dialog):
        assert dialog.speaker_combo.findData(0) == -1
        assert dialog.speaker_combo.currentIndex() == -1
        dialog.save_btn.click()
        assert dialog.result() != QDialog.DialogCode.Accepted
        assert "请先选择" in dialog.error.text()
        dialog.speaker_combo.setCurrentIndex(dialog.speaker_combo.findData(2))
        dialog.save_btn.click()
        return dialog.result()

    monkeypatch.setattr(_SegmentSpeakerDialog, "exec", choose)
    page._prompt_segment_speaker(23, meeting_id=record.id)
    row = next(s for s in controller.store.get_segments(record.id) if s.seq == 23)
    assert row.speaker_id == 2 and row.speaker_locked
    assert controller.speakers.assign(np.zeros(1), sample_key=24, learnable=False) == 0


def test_new_speaker_is_allocated_after_incoming_voice_arrives_during_dialog(live_meeting, monkeypatch):
    page, controller, history, record = live_meeting
    arrivals = []

    def choose_new_after_arrival(dialog):
        sid = controller.speakers.assign(_unit(0, 0, 1))
        arrivals.append(sid)
        history.enqueue(SegmentRecord(record.id, 15, record.started_at, raw_text="弹窗打开期间的新发言",
                                      speaker_id=sid, trigger_mode="meeting"))
        dialog.speaker_combo.setCurrentIndex(dialog.speaker_combo.count() - 1)
        assert dialog.speaker_id is None
        dialog.save_btn.click()
        return dialog.result()

    monkeypatch.setattr(_SegmentSpeakerDialog, "exec", choose_new_after_arrival)
    page._live_correct_speaker_btn.click()
    page._on_live_transcript_link(QUrl("live-segment:7"))
    rows = {s.seq: s for s in controller.store.get_segments(record.id)}
    assert rows[7].speaker_locked and rows[7].speaker_id > arrivals[0]
    assert rows[15].speaker_id == arrivals[0] and not rows[15].speaker_locked
    assert controller.state is MeetingState.LIVE


def test_speaker_edits_work_while_paused_and_preserve_pause_reason(live_meeting, monkeypatch):
    page, controller, _history, record = live_meeting
    controller.pause("设备断开，等待继续")
    monkeypatch.setattr("sayink.ui.meeting_page.QInputDialog.getText", lambda *_args: ("主持人", True))
    page._on_live_transcript_link(QUrl("live-speaker:1"))
    assert controller.correct_segment_speaker(record.id, 11, 2)
    assert controller.state is MeetingState.PAUSED
    assert "设备断开" in page._live_status.text()
    assert controller.speaker_names(record.id)[1] == "主持人"
    assert page._live_speaker_btn.isEnabled() and page._live_correct_speaker_btn.isEnabled()


def test_draining_and_generation_reject_late_speaker_writes(live_meeting):
    page, controller, _history, record = live_meeting
    controller._test_pending["n"] = 1
    controller.request_end()
    assert controller.state is MeetingState.DRAINING
    assert not page._live_speaker_btn.isEnabled() and not page._live_correct_speaker_btn.isEnabled()
    assert controller.rename_speaker(record.id, 1, "不应保存") is None
    assert not controller.correct_segment_speaker(record.id, 11, 2)
    controller.finalize()
    controller._generating_for = record.id
    assert controller.rename_speaker(record.id, 1, "不应保存") is None
    assert not controller.correct_segment_speaker(record.id, 11, 2)


def test_correction_dialog_retains_selection_after_failed_write_and_retries(live_meeting, monkeypatch):
    page, controller, _history, record = live_meeting
    segment = controller.store.get_segments(record.id)[2]
    write = controller.store.set_segment_speaker
    monkeypatch.setattr(controller.store, "set_segment_speaker", lambda *_args: None)
    dialog = _SegmentSpeakerDialog(segment, [(1, "说话人1"), (2, "说话人2"), (3, "说话人3")], page,
                                   live=True, save_speaker=lambda sid: controller.correct_segment_speaker(record.id, 11, sid))
    try:
        dialog.show()
        dialog.speaker_combo.setCurrentIndex(dialog.speaker_combo.findData(2))
        dialog.save_btn.click()
        assert dialog.isVisible() and dialog.error.isVisibleTo(dialog) and dialog.speaker_id == 2
        assert not controller.store.get_segments(record.id)[2].speaker_locked
        monkeypatch.setattr(controller.store, "set_segment_speaker", write)
        dialog.save_btn.click()
        assert dialog.result() == QDialog.DialogCode.Accepted
        assert controller.store.get_segments(record.id)[2].speaker_locked
    finally:
        dialog.deleteLater()


def test_meeting_ending_during_dialog_does_not_apply_stale_selection(live_meeting, monkeypatch):
    page, controller, _history, record = live_meeting

    def end_during_dialog(dialog):
        controller.finalize()
        dialog.speaker_combo.setCurrentIndex(dialog.speaker_combo.findData(2))
        dialog.save_btn.click()
        assert dialog.result() != QDialog.DialogCode.Accepted
        assert not dialog.error.isHidden()
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(_SegmentSpeakerDialog, "exec", end_during_dialog)
    page._live_correct_speaker_btn.click()
    page._on_live_transcript_link(QUrl("live-segment:11"))
    assert not next(s for s in controller.store.get_segments(record.id) if s.seq == 11).speaker_locked


def test_new_meeting_resets_live_correction_and_names(live_meeting):
    page, controller, _history, record = live_meeting
    page._live_correct_speaker_btn.click()
    page._rename_speaker(1, "第一场", meeting_id=record.id)
    page._populate_live_speaker_menu()
    assert page._live_speaker_menu.actions()
    controller.finalize()
    options = MeetingOptions(vad="energy")
    caps = resolve_capabilities(page._config, options, dictation_model_id="funasr-nano")
    next_record = controller.begin(options, replace(caps, speaker_model_available=False))
    assert not page._live_correcting_speakers
    assert page._live_speaker_feedback.isHidden()
    assert not page._live_speaker_btn.isEnabled()
    assert not page._live_speaker_menu.actions()
    assert controller.speaker_names(next_record.id) == {}
    assert controller.speakers.assign(_unit(1, 0, 0)) == 1


def test_old_database_adds_assignment_lock_without_changing_existing_text(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(_DDL.replace(",\n  speaker_locked INTEGER NOT NULL DEFAULT 0", ""))
        conn.execute("INSERT INTO meetings (id, status, started_at) VALUES ('m', 'interrupted', 1)")
        conn.execute("INSERT INTO history (session_id, seq, created_at, raw_text, corrected_text, speaker_id, trigger_mode) "
                     "VALUES ('m', 7, 1, '识别原文', '人工文字修正', 1, 'meeting')")
    history = HistoryStore(db_path=path)
    try:
        store = MeetingStore(history)
        before = store.get_segments("m")[0]
        assert not before.speaker_locked and before.corrected_text == "人工文字修正"
        store.set_segment_speaker("m", 7, 2)
        assert store.flush()
    finally:
        history.close(timeout=2)
    history = HistoryStore(db_path=path)
    try:
        saved = MeetingStore(history).get_segments("m")[0]
        assert saved.speaker_locked and saved.speaker_id == 2
        assert saved.raw_text == before.raw_text and saved.corrected_text == before.corrected_text
    finally:
        history.close(timeout=2)

"""Human-factor behavior for meeting import: browse while recognizing, and a transcript dialog that does not trap or discard input."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PyQt6.QtCore import QCoreApplication, QMimeData, QPointF, Qt, QUrl
from PyQt6.QtGui import QDropEvent
from PyQt6.QtWidgets import QDialog, QPushButton

from sayink.history_store import HistoryStore, SegmentRecord
from sayink.minutes_prompts import segment_offset_ms
from sayink.media_import import AudioChunk, MediaTurn, RecognitionResult, recognize_with_session
from sayink.meeting_session import MeetingController
from sayink.speaker_turns import RecognizedTurn
from sayink.ui.meeting_page import MeetingPage
from sayink.ui.transcript_import_dialog import TranscriptImportDialog
from tests.helpers.app_harness import app_harness


def _wait_until(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        QCoreApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _controller(config, tmp_path):
    history = HistoryStore(db_path=tmp_path / "history.db")
    return MeetingController(config, history, pending_segments=lambda: 0), history


def test_failed_file_open_can_keep_the_current_draft_without_editing_it():
    dialog = TranscriptImportDialog()
    try:
        dialog._edit.setPlainText("张三：还在。\n李四：也在。")
        dialog._set_file_error("无法读取文件：拒绝访问")
        assert not dialog._ok.isEnabled()
        assert "拒绝访问" in dialog._preview.text()
        keep = dialog._keep_current_btn
        assert not keep.isHidden()
        keep.click()
        assert dialog._file_error == ""
        assert dialog._ok.isEnabled()
        assert dialog.transcript_text().startswith("张三")
    finally:
        dialog.deleteLater()


def test_dialog_opens_on_the_transcript_and_states_the_size_limit(qapp):
    dialog = TranscriptImportDialog()
    dialog.show()
    try:
        assert _wait_until(lambda: dialog.focusWidget() is dialog._edit)
        assert "200 万" in dialog._hint.text()
        assert "纯文本" in dialog._detect.toolTip()
    finally:
        dialog.close()
        dialog.deleteLater()


def test_replacing_or_closing_a_draft_asks_first(tmp_path, qapp):
    dialog = TranscriptImportDialog()
    dialog.show()
    dialog._edit.setPlainText("原来的文字稿")
    path = tmp_path / "other.txt"
    path.write_text("新的一段。\n另一段。", encoding="utf-8")
    try:
        with patch("sayink.ui.transcript_import_dialog.QMessageBox") as box_cls:
            stay = object()
            box_cls.return_value.addButton.side_effect = [stay, object()]
            box_cls.return_value.clickedButton.return_value = stay
            assert dialog._load_file_path(path) is False
        assert dialog.transcript_text() == "原来的文字稿"
        with patch("sayink.ui.transcript_import_dialog.QMessageBox") as box_cls:
            stay = object()
            box_cls.return_value.addButton.side_effect = [stay, object()]
            box_cls.return_value.clickedButton.return_value = stay
            dialog.reject()
        assert dialog.isVisible()
    finally:
        dialog._edit.clear()
        dialog._title.clear()
        dialog.close()
        dialog.deleteLater()


def test_drop_reports_ignored_files_and_refreshes_an_autofilled_title(tmp_path, qapp):
    dialog = TranscriptImportDialog()
    try:
        first = tmp_path / "alpha.txt"
        second = tmp_path / "beta.txt"
        first.write_text("甲：第一句。\n乙：第二句。", encoding="utf-8")
        second.write_text("丙：第三句。", encoding="utf-8")
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(first)), QUrl.fromLocalFile(str(second))])
        dialog.dropEvent(QDropEvent(
            QPointF(4, 4), Qt.DropAction.CopyAction, mime,
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
        ))
        assert "甲" in dialog.transcript_text()
        assert "忽略" in dialog._preview.text()
        assert dialog._title.text() == "alpha"
        third = tmp_path / "gamma.txt"
        third.write_text("丁：手写标题不能被盖掉。\n戊：还有一句。", encoding="utf-8")
        dialog._title.setText("手写标题")
        with patch("sayink.ui.transcript_import_dialog.QMessageBox") as box_cls:
            replace = object()
            box_cls.return_value.addButton.side_effect = [object(), replace]
            box_cls.return_value.clickedButton.return_value = replace
            assert dialog._load_file_path(third)
        assert dialog._title.text() == "手写标题"
    finally:
        dialog.deleteLater()


def test_preview_callback_sees_text_as_chunks_finish():
    chunks = [
        AudioChunk(0, 1600, np.ones(1600, dtype=np.float32)),
        AudioChunk(1600, 3200, np.ones(1600, dtype=np.float32)),
    ]
    seen: list[str] = []

    def decode(chunk):
        return [RecognizedTurn(0, len(chunk.audio), "已经听到", np.ones(4, dtype=np.float32), True)]

    recognize_with_session(
        chunks, decode, session=object(), threshold=0.5, cancel=lambda: False, preview=seen.append,
    )
    assert seen and seen[-1].count("已经听到") == 2


def test_import_uses_the_file_time_and_can_be_moved_without_changing_offsets(config, tmp_path):
    controller, history = _controller(config, tmp_path)
    try:
        source = tmp_path / "old.wav"
        source.write_bytes(b"pcm")
        stamp = 1_700_000_000
        os.utime(source, (stamp, stamp))
        controller._media_source_path = str(source)
        controller._importing_media = True
        saved = controller.commit_media_import(
            RecognitionResult((MediaTurn(1, 2_000, 1_000, "你好"),), cancelled=False, audio_path=str(source)),
            title="旧录音",
        )
        assert saved is not None
        assert abs(saved.started_at - stamp * 1000) < 1500
        segments = controller.store.get_segments(saved.id)
        offset = segments[0].captured_at - saved.started_at
        moved = saved.started_at - 86_400_000
        controller.store.shift_meeting_start(saved.id, moved)
        assert controller.store.flush()
        again = controller.store.get_meeting(saved.id)
        assert again.started_at == moved
        assert controller.store.get_segments(saved.id)[0].captured_at - again.started_at == offset
    finally:
        controller.shutdown()
        history.close(timeout=2)


def test_shift_keeps_legacy_and_untimed_rows_distinct(config, tmp_path):
    controller, history = _controller(config, tmp_path)
    try:
        record = controller.store.create_meeting(title="旧会议")
        controller.store.end_meeting(record.id, ended_at=record.started_at + 8000)
        history.enqueue(SegmentRecord(
            record.id, 0, record.started_at + 5000, raw_text="旧句",
            trigger_mode="meeting", duration_ms=1000, captured_at=0,
        ))
        history.enqueue(SegmentRecord(
            record.id, 1, record.started_at + 1, raw_text="没有时间",
            trigger_mode="meeting", captured_at=-1,
        ))
        assert controller.store.flush()
        before = controller.store.get_segments(record.id)
        old_offset = segment_offset_ms(before[0], record.started_at)
        untimed_created = before[1].created_at
        moved = record.started_at - 3_600_000
        controller.store.shift_meeting_start(record.id, moved)
        assert controller.store.flush()
        again = controller.store.get_meeting(record.id)
        after = controller.store.get_segments(record.id)
        assert segment_offset_ms(after[0], again.started_at) == old_offset
        assert after[0].captured_at == 0
        assert after[1].captured_at == -1
        assert after[1].created_at == untimed_created
    finally:
        controller.shutdown()
        history.close(timeout=2)


def test_browsing_another_meeting_stays_available_during_import(config, tmp_path, qapp, monkeypatch):
    controller, history = _controller(config, tmp_path)
    page = MeetingPage(config, controller)
    try:
        first = controller.store.create_meeting(title="上一场")
        controller.store.end_meeting(first.id, ended_at=first.started_at + 1000)
        history.enqueue(SegmentRecord(
            first.id, 1, first.started_at, raw_text="上一场的发言", trigger_mode="meeting",
            captured_at=first.started_at,
        ))
        second = controller.store.create_meeting(title="另一场")
        controller.store.end_meeting(second.id, ended_at=second.started_at + 1000)
        history.enqueue(SegmentRecord(
            second.id, 1, second.started_at, raw_text="另一场的发言", trigger_mode="meeting",
            captured_at=second.started_at,
        ))
        assert controller.store.flush()
        page.show_meeting(first.id)

        def runner(path, cancel, progress):
            progress("正在识别 1/4 段（25%）")
            while not cancel():
                time.sleep(0.02)
            return RecognitionResult((MediaTurn(1, 0, 400, "导入中的一句"),), cancelled=True)

        controller.set_media_runner(runner)
        source = tmp_path / "clip.mp4"
        source.write_bytes(b"video")
        monkeypatch.setattr(
            "sayink.ui.meeting_page.QFileDialog.getOpenFileName",
            lambda *args, **kwargs: (str(source), ""),
        )
        next(item for item in page.findChildren(QPushButton) if item.accessibleName() == "导入音视频").click()
        assert _wait_until(lambda: "25%" in page._import_progress.text())
        assert "段" not in page._import_progress.text()
        assert not page._import_kicker.isHidden() and "正在识别" in page._import_kicker.text()
        assert page._list.selectedItems() == []
        assert page._import_btn.isEnabled()
        assert not page._start_btn.isEnabled()
        assert not page._start_block.isHidden()
        assert "结束后可以开始" in page._start_block.text()
        for index in range(page._list.count()):
            if page._list.item(index).data(Qt.ItemDataRole.UserRole) == second.id:
                page._list.setCurrentRow(index)
                break
        assert "另一场的发言" in page._transcript_view.toPlainText()
        assert not page._import_progress.isHidden()
        assert page._detail_title.text() == "另一场"
        cancel = next(item for item in page.findChildren(QPushButton) if item.accessibleName() == "取消导入音视频")
        cancel.click()
        assert cancel.text() == "正在停止…"
        assert "保留已有文字" in page._import_progress.text()
        assert not cancel.isEnabled()
        assert _wait_until(lambda: not controller.importing_media)
        assert page._detail_title.text() == "另一场"
    finally:
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_cancel_past_halfway_asks_and_can_be_declined(config, tmp_path, qapp):
    controller, history = _controller(config, tmp_path)
    page = MeetingPage(config, controller)
    try:
        controller._importing_media = True
        page._import_progress_bar.setRange(0, 100)
        page._import_progress_bar.setValue(80)
        page._sync_media_buttons()
        cancel = next(item for item in page.findChildren(QPushButton) if item.accessibleName() == "取消导入音视频")
        with patch("sayink.ui.meeting_page.QMessageBox") as box_cls:
            stay = object()
            box_cls.return_value.addButton.side_effect = [stay, object()]
            box_cls.return_value.clickedButton.return_value = stay
            cancel.click()
        assert not controller._media_cancel.is_set()
        assert cancel.text() == "取消导入"
    finally:
        controller._importing_media = False
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_a_second_file_waits_in_the_queue(config, tmp_path, qapp):
    controller, history = _controller(config, tmp_path)
    try:
        seen: list[str] = []
        hold = threading.Event()

        def runner(path, cancel, progress):
            seen.append(Path(path).name)
            progress("正在识别 1/1 段（40%）")
            if Path(path).name == "one.mp3":
                hold.wait(3)
            return RecognitionResult((MediaTurn(1, 0, 200, Path(path).stem),), cancelled=False)

        controller.set_media_runner(runner)
        first = tmp_path / "one.mp3"
        second = tmp_path / "two.mp3"
        first.write_bytes(b"a")
        second.write_bytes(b"b")
        assert controller.start_media_import(str(first), title="one") == ""
        assert _wait_until(lambda: controller.importing_media)
        assert controller.start_media_import(str(second), title="two") == ""
        assert "队列" in controller.media_queue_notice
        hold.set()
        assert _wait_until(lambda: seen == ["one.mp3", "two.mp3"] and not controller.importing_media)
        titles = {item.title for item in controller.store.list_meetings()}
        assert titles == {"one", "two"}
    finally:
        controller.shutdown()
        history.close(timeout=2)


def test_failed_import_is_an_error_and_can_be_retried(config, tmp_path, qapp):
    controller, history = _controller(config, tmp_path)
    page = MeetingPage(config, controller)
    try:
        source = tmp_path / "broken.mp3"
        source.write_bytes(b"nope")
        page._pending_media_import_path = str(source)
        page._on_media_failed("导入音视频失败：无法解码")
        assert page._status_tone == "error"
        assert "无法解码" in page._status_label.text()
        assert not page._media_again_btn.isHidden()
        record = controller.store.create_meeting_from_media(
            (MediaTurn(1, 0, 1000, "有声音"),),
            title="丢了文件",
            audio_path=str(tmp_path / "missing.wav"),
        )
        assert record is not None and controller.store.flush()
        page.show_meeting(record.id)
        assert not page._audio_missing.isHidden()
        assert "没有复制" in page._audio_missing.text()
    finally:
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_pasted_transcript_keeps_the_time_the_dialog_chose(config, tmp_path, qapp, monkeypatch):
    controller, history = _controller(config, tmp_path)
    page = MeetingPage(config, controller)
    try:
        existing = controller.store.create_meeting(title="已有")
        controller.store.end_meeting(existing.id, ended_at=existing.started_at + 1000)
        assert controller.store.flush()
        page.show_meeting(existing.id)
        chosen = 1_600_000_000_000
        monkeypatch.setattr(
            page, "_ask_import",
            lambda: ("张三：本周完成了检索。\n李四：下周联调。", "导入周会", True, chosen),
        )
        page._import_transcript()
        record = controller.store.get_meeting(page._detail_id)
        assert record.started_at == chosen
    finally:
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_a_saved_file_keeps_an_earlier_retry_payload(config, tmp_path):
    controller, history = _controller(config, tmp_path)
    try:
        failed = RecognitionResult((MediaTurn(1, 0, 200, "甲"),), cancelled=False)
        controller._unsaved_media = (failed, "甲", "keep-me", str(tmp_path / "a.wav"))
        saved = controller.commit_media_import(
            RecognitionResult((MediaTurn(1, 0, 200, "乙"),), cancelled=False, audio_path=str(tmp_path / "b.wav")),
            "乙",
        )
        assert saved is not None and saved.title == "乙"
        assert controller._unsaved_media is not None
        assert controller._unsaved_media[0] is failed
    finally:
        controller.shutdown()
        history.close(timeout=2)


def test_starting_a_file_clears_a_stale_queue_notice(config, tmp_path, qapp):
    controller, history = _controller(config, tmp_path)
    try:
        controller.media_queue_notice = "已加入队列，前面还有 1 个文件"
        controller.set_media_runner(
            lambda path, cancel, progress: RecognitionResult(
                (MediaTurn(1, 0, 200, "丙"),), cancelled=False,
            )
        )
        source = tmp_path / "c.mp3"
        source.write_bytes(b"c")
        assert controller.start_media_import(str(source), title="丙") == ""
        assert controller.media_queue_notice == ""
        assert _wait_until(lambda: not controller.importing_media)
    finally:
        controller.shutdown()
        history.close(timeout=2)


def test_queue_waits_until_the_unsaved_transcript_is_retried(config, tmp_path, qapp):
    controller, history = _controller(config, tmp_path)
    try:
        failed = RecognitionResult((MediaTurn(1, 0, 200, "甲"),), cancelled=False)
        second = tmp_path / "two.mp3"
        second.write_bytes(b"b")
        controller._unsaved_media = (failed, "甲", "keep-me", str(tmp_path / "a.wav"))
        controller._media_queue.append((str(second), "two"))
        controller._kick_media_queue()
        assert controller._media_queue == [(str(second), "two")]
        assert controller._unsaved_media[0] is failed

        controller.set_media_runner(
            lambda path, cancel, progress: RecognitionResult((MediaTurn(1, 0, 200, "乙"),), cancelled=False)
        )
        controller._unsaved_media = None
        controller._kick_media_queue()
        assert _wait_until(lambda: not controller.importing_media and not controller._media_queue)
        assert {item.title for item in controller.store.list_meetings()} == {"two"}
    finally:
        controller.shutdown()
        history.close(timeout=2)


def test_queued_failure_can_be_retried_after_another_file_finishes(config, tmp_path, qapp):
    controller, history = _controller(config, tmp_path)
    page = MeetingPage(config, controller)
    try:
        failed = tmp_path / "broken.mp3"
        other = tmp_path / "other.mp3"
        failed.write_bytes(b"a")
        other.write_bytes(b"b")
        controller._media_source_path = str(failed)
        page._pending_media_import_path = ""
        page._on_media_failed("无法解码")
        assert page._failed_media_path == str(failed)
        page._pending_media_import_path = str(other)
        page._on_media_finished("")
        assert page._failed_media_path == str(failed)
        assert not page._media_again_btn.isHidden()
    finally:
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_mixed_drop_does_not_claim_import_started(config, tmp_path, qapp):
    controller, history = _controller(config, tmp_path)
    page = MeetingPage(config, controller)
    try:
        controller.set_external_block(lambda: "已有会议在进行中")
        media = tmp_path / "clip.mp3"
        note = tmp_path / "note.txt"
        media.write_bytes(b"a")
        note.write_text("文字", encoding="utf-8")
        page._accept_dropped_files([media, note])
        assert "已开始导入" not in page._status_label.text()
        assert "已有会议在进行中" in page._status_label.text()
        assert "已忽略 1 个" in page._status_label.text()
        assert not controller.importing_media
    finally:
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_exit_while_importing_names_the_import():
    with app_harness() as harness:
        app = harness["app"]
        assert app.meeting.begin_media_import() == ""
        with patch("sayink.app.QMessageBox") as box_cls:
            stay = object()
            box_cls.return_value.addButton.side_effect = [stay, object()]
            box_cls.return_value.clickedButton.return_value = stay
            assert app._confirm_exit() is False
        assert "导入" in box_cls.return_value.setText.call_args[0][0]
        app.meeting._importing_media = False


def test_dropped_media_starts_an_import(config, tmp_path, qapp, monkeypatch):
    controller, history = _controller(config, tmp_path)
    page = MeetingPage(config, controller)
    try:
        source = tmp_path / "dragged.mp3"
        source.write_bytes(b"audio")
        started: list[str] = []
        monkeypatch.setattr(
            controller, "start_media_import",
            lambda path, title="": started.append(path) or "",
        )
        monkeypatch.setattr(controller, "media_block_reason", lambda: "")
        page._accept_dropped_files([source])
        assert started == [str(source)]
    finally:
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_unreadable_dropped_transcript_imports_text_the_user_keeps(config, tmp_path, qapp, monkeypatch):
    controller, history = _controller(config, tmp_path)
    page = MeetingPage(config, controller)
    bad = tmp_path / "broken.txt"
    bad.write_bytes(b"\xff\xfe\x61")

    def accept_replacement(self):
        self._file_error = ""
        self._edit.setPlainText("张三：补上的文字。\n李四：第二句。")
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(TranscriptImportDialog, "exec", accept_replacement)
    try:
        page._import_dropped_transcript(bad, ignored=0)
        record = controller.store.get_meeting(page._detail_id)
        assert record is not None
        assert "补上的文字" in controller.transcript_text(record.id)
    finally:
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_closing_a_failed_transcript_drop_does_not_import(config, tmp_path, qapp, monkeypatch):
    controller, history = _controller(config, tmp_path)
    page = MeetingPage(config, controller)
    bad = tmp_path / "broken.txt"
    bad.write_bytes(b"\xff\xfe\x61")
    monkeypatch.setattr(TranscriptImportDialog, "exec", lambda self: QDialog.DialogCode.Rejected)
    try:
        page._import_dropped_transcript(bad, ignored=0)
        assert controller.store.list_meetings() == []
        assert page._detail_id == ""
    finally:
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_transcript_dialog_only_prompts_discard_when_text_edited(tmp_path, qapp, monkeypatch):
    """M-UX-06: Changing time on an empty dialog or loading an untouched file closes without a nagging popup."""
    from PyQt6.QtCore import QDateTime

    sample = tmp_path / "weekly.txt"
    sample.write_text("张三：内容一\n李四：内容二", encoding="utf-8")

    asked = []
    monkeypatch.setattr(TranscriptImportDialog, "_confirm_discard", lambda self: asked.append(True) or False)

    # 1. Empty dialog with edited clock -> closes cleanly without asking
    d1 = TranscriptImportDialog()
    try:
        d1._when.setDateTime(QDateTime.currentDateTime().addSecs(-3600))
        d1.reject()
        assert asked == []
        assert d1.result() == QDialog.DialogCode.Rejected
    finally:
        d1.deleteLater()

    # 2. Loaded file without manual edits -> closes cleanly without asking
    d2 = TranscriptImportDialog()
    try:
        assert d2._load_file_path(sample) is True
        d2.reject()
        assert asked == []
        assert d2.result() == QDialog.DialogCode.Rejected
    finally:
        d2.deleteLater()

    # 3. User edits text after loading -> prompts before discarding
    d3 = TranscriptImportDialog()
    try:
        assert d3._load_file_path(sample) is True
        d3._edit.setPlainText("张三：手动改过的内容")
        d3.reject()
        assert asked == [True]
    finally:
        d3._close_confirmed = True
        d3.deleteLater()


def test_transcript_load_and_import_parses_once_and_drag_move_caches_urls(
    config, tmp_path, qapp, monkeypatch
):
    """M-PERF-01 & M-PERF-04: Single parse across file load + import, and cached DragMove validation."""
    from PyQt6.QtCore import QEvent, QMimeData, QUrl
    import sayink.transcript_import as ti

    sample = tmp_path / "meeting.txt"
    sample.write_text("张三：第一句\n李四：第二句", encoding="utf-8")

    parse_calls = 0
    real_parse = ti.parse_transcript

    def counting_parse(text, *, detect_structure=True):
        nonlocal parse_calls
        parse_calls += 1
        return real_parse(text, detect_structure=detect_structure)

    monkeypatch.setattr("sayink.ui.transcript_import_dialog.parse_transcript", counting_parse)
    monkeypatch.setattr("sayink.meeting_session.parse_transcript", counting_parse)

    controller, history = _controller(config, tmp_path)
    page = MeetingPage(config, controller)
    dialog = TranscriptImportDialog()
    try:
        assert dialog._load_file_path(sample) is True
        assert parse_calls == 1

        monkeypatch.setattr(
            page,
            "_ask_import",
            lambda: (
                dialog.transcript_text(),
                dialog.meeting_title(),
                dialog.detect_structure(),
                dialog.meeting_started_ms(),
                dialog.parsed(),
            ),
        )
        page._import_transcript()
        assert parse_calls == 1

        # DragEnter validates & paints border once; DragMove reuses cache without re-statting or re-painting
        stat_calls = 0
        paint_calls = 0
        orig_dropped = TranscriptImportDialog._dropped_local_files
        orig_paint = dialog._paint_drag_border

        def counting_dropped(mime):
            nonlocal stat_calls
            stat_calls += 1
            return orig_dropped(mime)

        def counting_paint():
            nonlocal paint_calls
            paint_calls += 1
            orig_paint()

        monkeypatch.setattr(TranscriptImportDialog, "_dropped_local_files", staticmethod(counting_dropped))
        monkeypatch.setattr(dialog, "_paint_drag_border", counting_paint)

        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(sample))])

        class _FakeDragEvent:
            def __init__(self, ev_type):
                self._type = ev_type
                self.accepted = False

            def type(self):
                return self._type

            def mimeData(self):
                return mime

            def acceptProposedAction(self):
                self.accepted = True

        assert dialog._handle_drag_enter(_FakeDragEvent(QEvent.Type.DragEnter)) is True
        assert dialog._handle_drag_enter(_FakeDragEvent(QEvent.Type.DragMove)) is True
        assert dialog._handle_drag_enter(_FakeDragEvent(QEvent.Type.DragMove)) is True
        assert stat_calls == 1
        assert paint_calls == 1
    finally:
        dialog._close_confirmed = True
        dialog.deleteLater()
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_media_preview_incremental_append_and_scroll_preservation(config, tmp_path, qapp):
    """M-PERF-03: Incremental delta append keeps bottom when at bottom, and preserves scroll when scrolled up."""
    controller, history = _controller(config, tmp_path)
    page = MeetingPage(config, controller)
    page.resize(960, 520)
    page.show()
    try:
        controller._importing_media = True
        page._show_import_detail()
        chunk_1 = "\n".join(f"第 {i} 行实时识别内容预览文本" for i in range(60))
        page._on_media_preview(chunk_1)
        qapp.processEvents()
        sb = page._import_live.verticalScrollBar()
        assert sb.maximum() > 0
        assert sb.value() == sb.maximum()

        # User scrolls up to the top to read early lines
        sb.setValue(0)
        qapp.processEvents()
        chunk_2 = chunk_1 + "\n" + "\n".join(f"新增第 {i} 行内容" for i in range(60, 90))
        page._on_media_preview(chunk_2)
        qapp.processEvents()
        assert page._import_live.toPlainText() == chunk_2
        assert sb.value() == 0
    finally:
        controller._importing_media = False
        page.hide()
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_right_detail_pane_accepts_drag_and_drop_imports(config, tmp_path, qapp, monkeypatch):
    """M-UX-03: Dragging onto right-hand detail widgets triggers file import."""
    from PyQt6.QtCore import QEvent, QMimeData, QUrl

    controller, history = _controller(config, tmp_path)
    page = MeetingPage(config, controller)
    try:
        sample = tmp_path / "clip.wav"
        sample.write_bytes(b"RIFF")
        dropped_targets: list[list[Path]] = []
        monkeypatch.setattr(page, "_accept_dropped_files", lambda paths: dropped_targets.append(list(paths)))

        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(sample))])

        class _FakeDrop:
            def type(self):
                return QEvent.Type.Drop

            def mimeData(self):
                return mime

            def acceptProposedAction(self):
                pass

        for widget in (page._detail_pane, page._notes_view.viewport(), page._transcript_view.viewport()):
            assert widget in page._import_drop_widgets
            assert page.eventFilter(widget, _FakeDrop()) is True
        assert len(dropped_targets) == 3
    finally:
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_imported_meetings_honor_auto_generate_and_land_on_transcript_tab_when_empty(
    config, tmp_path, qapp, monkeypatch
):
    """M-UX-04 + Privacy Contract: Imported transcripts and media NEVER auto-enqueue AI minutes by default
    even when ``meeting.auto_generate=True`` (landing on 文字稿 tab); explicit ``auto_generate=True`` opts in."""
    controller, history = _controller(config, tmp_path)
    page = MeetingPage(config, controller)
    try:
        # Case 1: auto_generate=False -> lands directly on 文字稿 tab (index 1)
        config.set("meeting.auto_generate", False)
        short_rec = controller.import_transcript("张三：这是一段导入的文字稿。\n李四：收到。")
        assert short_rec is not None
        page.show_meeting(short_rec.id)
        assert page._content_stack.currentIndex() == 1
        assert page._tab_transcript.isChecked()

        # Case 2: meeting.auto_generate=True + LLM configured + >= 200 chars -> default imports STILL
        # do NOT auto-enqueue remote LLM generation and land on 文字稿 tab (index 1)!
        config.set("meeting.auto_generate", True)
        config.set("llm.api_url", "https://api.example.com/v1")
        config.set("llm.api_key", "sk-test")
        config.set("llm.model_name", "gpt-test")
        controller.confirm_remote_host("https://api.example.com")
        started_gen: list[str] = []
        monkeypatch.setattr(controller, "_start", lambda mid, *_a, **_kw: started_gen.append(mid) or "")

        long_body = "\n".join(f"张三：这是第 {i} 条完整的项目进度讨论记录与技术方案复盘。" for i in range(15))
        assert len(long_body) >= 200
        default_text_rec = controller.import_transcript(long_body, title="长文字稿")
        assert default_text_rec is not None
        assert default_text_rec.id not in started_gen
        assert controller.queued_generation(default_text_rec.id) is None
        page.show_meeting(default_text_rec.id)
        assert page._content_stack.currentIndex() == 1
        assert page._tab_transcript.isChecked()

        media_turns = tuple(
            MediaTurn(1, idx * 2000, 1500, f"这是第 {idx} 段足够长的音视频转写内容用于验证默认不自动生成。")
            for idx in range(12)
        )
        default_media_rec = controller.commit_media_import(
            RecognitionResult(media_turns, cancelled=False, audio_ms=24_000),
            "长录音导入",
        )
        assert default_media_rec is not None
        assert default_media_rec.id not in started_gen
        assert controller.queued_generation(default_media_rec.id) is None
        page.show_meeting(default_media_rec.id)
        assert page._content_stack.currentIndex() == 1
        assert page._tab_transcript.isChecked()

        # Case 3: Explicit opt-in ``auto_generate=True`` enqueues AI minutes and opens on 纪要 tab (index 0).
        opt_in_text_rec = controller.import_transcript(long_body, title="显式开启文字稿", auto_generate=True)
        assert opt_in_text_rec is not None
        assert opt_in_text_rec.id in started_gen
        assert controller.queued_generation(opt_in_text_rec.id) is not None
        page.show_meeting(opt_in_text_rec.id)
        assert page._content_stack.currentIndex() == 0
        assert page._tab_notes.isChecked()
        controller._store.dequeue_generation(opt_in_text_rec.id)
        controller._store.flush()

        opt_in_media_rec = controller.commit_media_import(
            RecognitionResult(media_turns, cancelled=False, audio_ms=24_000),
            "显式开启录音导入",
            auto_generate=True,
        )
        assert opt_in_media_rec is not None
        assert opt_in_media_rec.id in started_gen
        assert controller.queued_generation(opt_in_media_rec.id) is not None
    finally:
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_foreground_main_window_suppresses_redundant_media_tray_notifications():
    """M-UX-01: Active visible MainWindow suppresses duplicate tray toast; hidden window notifies on queue drain."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    with app_harness() as harness:
        app = harness["app"]
        fake_tray = MagicMock()
        app._tray = fake_tray
        fake_rec = SimpleNamespace(display_title="评审录音")
        app._meeting = SimpleNamespace(
            store=SimpleNamespace(get_meeting=lambda _mid: fake_rec),
            media_import_notice="",
            queued_media=lambda: (),
            importing_media=False,
        )
        # Foreground active MainWindow -> suppressed!
        app._main = SimpleNamespace(
            isVisible=lambda: True,
            isMinimized=lambda: False,
            isActiveWindow=lambda: True,
        )
        app._on_media_import_settled("m1")
        app._on_media_import_failed_notify("解码失败")
        assert fake_tray.showMessage.call_count == 0

        # Background / hidden MainWindow with remaining queue -> waits until queue drains!
        app._main = SimpleNamespace(
            isVisible=lambda: False,
            isMinimized=lambda: False,
            isActiveWindow=lambda: False,
        )
        app._meeting.queued_media = lambda: (("b.wav", "b"),)
        app._on_media_import_settled("m1")
        assert fake_tray.showMessage.call_count == 0

        # Queue drained in background -> sends single tray notification!
        app._meeting.queued_media = lambda: ()
        app._on_media_import_settled("m1")
        assert fake_tray.showMessage.call_count == 1

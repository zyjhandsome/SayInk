"""Transcript/media import queue, progress chrome, and drag-and-drop mixin for MeetingPage."""

from __future__ import annotations

from pathlib import Path
import re
import time

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QWidget,
)

from sayink.media_import import file_dialog_filter
from sayink.meeting_store import STATUS_INTERRUPTED
from sayink.ui.media_import_dialog import MediaImportDialog, natural_media_sort_key
from sayink.ui.meeting_dialogs import _quiet_tooltips, _style_dialog_box
from sayink.ui.native_dialog import native_file_dialog

_ORIG_GET_OPEN_FILE_NAME = QFileDialog.getOpenFileName


def _format_import_remain(seconds: float) -> str:
    remaining = max(0, int(round(seconds)))
    if remaining < 60:
        return f"大约还需 {remaining} 秒"
    minutes, secs = divmod(remaining, 60)
    if minutes < 60:
        return f"大约还需 {minutes} 分 {secs} 秒" if secs else f"大约还需 {minutes} 分钟"
    hours, minutes = divmod(minutes, 60)
    return f"大约还需 {hours} 小时 {minutes} 分钟"


class _MeetingImportMixin:
    """Import dialog orchestration, background media queue chrome, and file drop handling."""

    def _imported_status_message(self, meeting_id: str, kind_label: str) -> str:
        if self._ctrl.generating_for == meeting_id:
            return f"已导入{kind_label}，正在自动生成纪要。"
        job = self._ctrl.queued_generation(meeting_id)
        if job is not None and not job.failed:
            return f"已导入{kind_label}，已自动加入纪要生成队列。"
        if kind_label == "音视频":
            return "已导入音视频。可先核对文字稿，或切换到「纪要」点击「生成纪要」。"
        return "已导入文字稿。确认内容后，可切换到「纪要」点击「生成纪要」。"

    def _ask_import(self) -> tuple | None:
        from sayink.ui.transcript_import_dialog import TranscriptImportDialog

        dialog = TranscriptImportDialog(self)
        with _quiet_tooltips(self):
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
        if not accepted:
            return None
        return (
            dialog.transcript_text(),
            dialog.meeting_title(),
            dialog.detect_structure(),
            dialog.meeting_started_ms(),
            dialog.parsed(),
        )

    def _import_transcript(self) -> None:
        if self._ctrl.is_active:
            self._set_status("会议进行中，请结束后再导入文字稿")
            return
        asked = self._ask_import()
        if not asked:
            return
        text, title, detect = asked[:3]
        started = asked[3] if len(asked) > 3 else None
        preparsed = asked[4] if len(asked) > 4 else None
        try:
            record = self._ctrl.import_transcript(
                text, title=title, detect_structure=detect, started_at=started, parsed=preparsed,
            )
        except TypeError:
            record = self._ctrl.import_transcript(
                text, title=title, detect_structure=detect, started_at=started,
            )
        if record is None:
            self._set_status(getattr(self._ctrl, "transcript_import_notice", "") or "没有可导入的文字")
            return
        self.show_meeting(record.id)
        self._set_status(self._imported_status_message(record.id, "文字稿"))

    def _import_is_recognizing(self) -> bool:
        return "正在识别" in (getattr(self, "_media_progress_text", "") or "")

    def _queued_media(self) -> tuple[tuple[str, str], ...]:
        queued = getattr(self._ctrl, "queued_media", None)
        if callable(queued):
            return tuple(queued())
        return tuple(getattr(self._ctrl, "_media_queue", ()) or ())

    def _sync_import_queue(self, queued) -> None:
        signature = tuple((str(path), str(title)) for path, title in queued)
        if signature == getattr(self, "_queue_signature", None):
            self._import_queue.setVisible(bool(signature))
            return
        self._queue_signature = signature
        while self._import_queue_lay.count():
            item = self._import_queue_lay.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        from sayink.ui import settings_styles as ss

        for index, (path, title) in enumerate(signature):
            row = QWidget()
            lay = QHBoxLayout(row)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(8)
            name = title or Path(path).name
            label = QLabel(name)
            label.setObjectName("meetingImportQueueItem")
            remove = QPushButton("移除")
            remove.setAccessibleName(f"移除排队文件 {name}")
            remove.setStyleSheet(ss.BTN_GHOST_SM)
            remove.setCursor(Qt.CursorShape.PointingHandCursor)
            remove.clicked.connect(lambda _checked=False, item_index=index: self._remove_queued_media(item_index))
            lay.addWidget(label, 1)
            lay.addWidget(remove, 0)
            self._import_queue_lay.addWidget(row)
        self._import_queue.setVisible(bool(signature))

    def _remove_queued_media(self, index: int) -> None:
        remove = getattr(self._ctrl, "remove_queued_media", None)
        if callable(remove):
            remove(index)
        self._queue_signature = None
        self._sync_media_buttons()

    def _sync_media_buttons(self) -> None:
        from sayink.ui import meeting_page as mp

        busy = bool(getattr(self._ctrl, "importing_media", False))
        browsing = self._stack.currentIndex() == mp.PAGE_BROWSE
        active = bool(getattr(self._ctrl, "is_active", False))
        self._import_btn.setVisible(browsing)
        self._media_btn.setVisible(browsing)
        self._media_btn.setEnabled(not active)
        self._import_btn.setEnabled(not active)
        self._start_btn.setEnabled(not busy and not active)
        self._media_btn.setText("加入队列" if busy else "导入音视频")
        self._media_btn.setToolTip(
            "正在识别。再选或拖入的文件会排在后面。" if busy else self._media_tip
        )
        recognizing = self._import_is_recognizing()
        block = "正在识别，结束后可以开始会议" if recognizing else "正在读取，结束后可以开始会议"
        self._start_btn.setToolTip(block if busy else self._start_tip)
        self._start_block.setText(block if busy else "")
        self._start_block.setVisible(busy and browsing)
        self._import_btn.setToolTip(
            "会议进行中，请结束后再导入文字稿" if active else self._import_tip
        )
        pending = busy and self._media_cancel_pending
        self._media_cancel_btn.setVisible(busy and browsing)
        self._media_cancel_btn.setEnabled(not pending)
        if pending:
            stop_label = "正在停止…"
        elif recognizing:
            stop_label = "停止识别"
        else:
            stop_label = "取消导入"
        self._media_cancel_btn.setText(stop_label)
        self._media_cancel_btn.setToolTip(
            "停在当前这段之后。已经识别的文字会留下，后面不再识别。"
            if recognizing or pending else
            "还没开始识别，取消后不会留下文字。"
        )
        if not busy:
            self._media_cancel_pending = False
        self._media_retry_btn.setVisible(
            browsing and not busy and bool(getattr(self._ctrl, "_unsaved_media", None))
        )
        self._media_again_btn.setVisible(
            browsing and not busy and bool(self._failed_media_path) and Path(self._failed_media_path).is_file()
        )
        queued = self._queued_media()
        phase = "正在识别" if recognizing else "正在读取"
        if queued:
            phase += f" · 还有 {len(queued)} 个在排队"
        if busy and browsing:
            self._import_kicker.setText(phase)
            self._import_kicker.setVisible(True)
            self._import_filename.set_full_text(getattr(self, "_media_import_title", "") or "")
            self._import_filename.setVisible(bool(getattr(self, "_media_import_title", "")))
        else:
            self._import_kicker.hide()
            self._import_filename.hide()
        self._sync_import_queue(queued if busy else ())

    def _existing_media_audio_paths(self) -> set[str]:
        existing: set[str] = set()
        for rec in getattr(self, "_records", ()) or ():
            ap = str(getattr(rec, "audio_path", "") or "").strip()
            if ap:
                existing.add(ap)
        for ap in getattr(self, "_meeting_audio_paths", {}).values():
            if ap:
                existing.add(str(ap))
        cur_src = str(getattr(self._ctrl, "_media_source_path", "") or "").strip()
        if cur_src:
            existing.add(cur_src)
        for queued_path, _title in self._queued_media():
            if queued_path:
                existing.add(str(queued_path))
        return existing

    def _import_media(self) -> None:
        from sayink.ui import meeting_page as mp

        if not getattr(self._ctrl, "importing_media", False):
            reason = self._ctrl.media_block_reason()
            if reason:
                self._set_status(reason, tone="error")
                return
        file_dialog = mp.QFileDialog
        if file_dialog.getOpenFileName is not _ORIG_GET_OPEN_FILE_NAME:
            with native_file_dialog(self):
                path, _selected = file_dialog.getOpenFileName(
                    self, "导入音视频", "", file_dialog_filter()
                )
            if not path:
                return
            self._begin_media_paths([Path(path)])
            return
        with native_file_dialog(self):
            paths, _selected = file_dialog.getOpenFileNames(
                self, "导入音视频", "", file_dialog_filter()
            )
        if not paths:
            return
        self._confirm_and_begin_media_paths([Path(p) for p in paths])

    def _confirm_and_begin_media_paths(self, paths: list[Path]) -> tuple[bool, str]:
        dialog = MediaImportDialog(
            paths,
            config=self._config,
            existing_audio_paths=self._existing_media_audio_paths(),
            queueing=bool(getattr(self._ctrl, "importing_media", False)),
            parent=self,
        )
        with _quiet_tooltips(self):
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
        if not accepted:
            return False, ""
        requests = dialog.import_requests()
        if not requests:
            return False, ""
        return True, self._begin_media_requests(requests)

    def _begin_media_paths(self, paths: list[Path]) -> str:
        ordered = sorted(paths, key=natural_media_sort_key) if len(paths) > 1 else list(paths)
        return self._begin_media_requests([(path, path.stem, {}) for path in ordered])

    def _begin_media_requests(self, requests: list[tuple[Path, str, dict]]) -> str:
        if not getattr(self._ctrl, "importing_media", False):
            self._last_batch_saved_id = ""
        for path, title, opts in requests:
            was_importing = bool(getattr(self._ctrl, "importing_media", False))
            kwargs: dict = {"title": title or path.stem}
            if opts:
                for key in ("language", "model_id", "started_at"):
                    if key in opts and opts[key] not in ("", None):
                        kwargs[key] = opts[key]
            try:
                message = self._ctrl.start_media_import(str(path), **kwargs)
            except TypeError:
                message = self._ctrl.start_media_import(str(path), title=title or path.stem)
            if message:
                self._set_status(message, tone="error")
                return message
            notice = str(getattr(self._ctrl, "media_queue_notice", "") or "")
            if notice or was_importing:
                self._ctrl.media_queue_notice = ""
                if notice:
                    self._set_status(notice)
                self._sync_media_buttons()
                continue
            self._arm_import_view(path, title=title or path.stem)
        return ""

    def _clear_import_live(self) -> None:
        self._last_media_preview_text = ""
        self._import_live.clear()

    def _arm_import_view(self, path: Path, *, title: str = "") -> None:
        self._failed_media_path = ""
        self._media_cancel_pending = False
        self._pending_media_import_path = str(path)
        self._media_import_started = time.monotonic()
        self._import_owns_detail = True
        self._media_import_title = (title or path.stem).strip()
        self._media_progress_text = "正在读取音频…"
        self._clear_import_live()
        self._sync_media_buttons()
        self._show_import_detail()

    def _on_media_started(self, path: str, title: str) -> None:
        resolved_title = (title or (Path(path).stem if path else "")).strip()
        self._failed_media_path = ""
        self._media_cancel_pending = False
        self._pending_media_import_path = str(path or "")
        self._media_import_started = time.monotonic()
        if resolved_title:
            self._media_import_title = resolved_title
        self._media_progress_text = "正在读取音频…"
        self._clear_import_live()
        if self._import_owns_detail or not self._detail_id:
            self._show_import_detail()
        else:
            self._detail_pane.setVisible(True)
            self._paint_import_banner()

    def _apply_progress_widgets(self) -> None:
        raw_text = getattr(self, "_media_progress_text", "") or "正在读取音频…"
        explicit = re.findall(r"[（(]\s*(\d+)\s*%\s*[）)]", raw_text)
        match = re.search(r"(\d+)\s*/\s*(\d+)", raw_text)
        pct = None
        if explicit:
            pct = max(0, min(100, int(explicit[-1])))
            display_text = raw_text
            self._import_progress_bar.setRange(0, 100)
            self._import_progress_bar.setValue(pct)
        elif match and int(match.group(2)) > 0:
            done, total = int(match.group(1)), int(match.group(2))
            pct = max(0, min(100, int(round(done * 100.0 / total))))
            display_text = raw_text if "%" in raw_text else f"{raw_text}（{pct}%）"
            self._import_progress_bar.setRange(0, 100)
            self._import_progress_bar.setValue(pct)
        else:
            pct_match = re.search(r"(\d+)\s*%", raw_text)
            if pct_match:
                pct = max(0, min(100, int(pct_match.group(1))))
                self._import_progress_bar.setRange(0, 100)
                self._import_progress_bar.setValue(pct)
            else:
                self._import_progress_bar.setRange(0, 0)
            display_text = raw_text
        remain = ""
        if pct is not None and 5 <= pct <= 95 and self._media_import_started:
            elapsed = time.monotonic() - self._media_import_started
            if elapsed >= 3:
                remain = _format_import_remain(elapsed * (100 - pct) / max(pct, 1))
        if self._media_cancel_pending:
            display_text = "正在停止，当前这段完成后保留已有文字"
        elif "正在识别" in raw_text:
            parts = ["正在识别"]
            if pct is not None:
                parts.append(f"{pct}%")
            if remain:
                parts.append(remain)
            display_text = " · ".join(parts)
        else:
            parts = ["正在读取"]
            if pct is not None:
                parts.append(f"{pct}%")
            if remain:
                parts.append(remain)
            display_text = " · ".join(parts)
        self._import_progress.setVisible(True)
        self._import_progress.setText(display_text)
        self._import_progress_bar.setVisible(True)

    def _show_import_detail(self) -> None:
        """Recognition progress owns the detail until the user opens another meeting."""
        self._import_owns_detail = True
        self._set_single_detail_visible(False)
        self._batch_tools.hide()
        self._batch_notice_scroll.hide()
        self._audio_missing.hide()
        self._status_label.hide()
        self._detail_pane.setVisible(True)
        self._detail_title.setReadOnly(True)
        self._detail_title.hide()
        self._detail_meta.setVisible(True)
        self._detail_meta.setText(
            "点左侧其他会议可以先看。识别会在后台继续（支持随时按快捷键语音输入）；开始新会议需等导入结束。"
        )
        self._list.blockSignals(True)
        self._list.clearSelection()
        self._list.setCurrentItem(None)
        self._list.blockSignals(False)
        self._apply_progress_widgets()
        self._sync_media_buttons()

    def _paint_import_banner(self) -> None:
        self._apply_progress_widgets()
        self._sync_media_buttons()

    def _hide_import_chrome(self) -> None:
        self._import_kicker.hide()
        self._import_filename.hide()
        self._import_progress.hide()
        self._import_progress_bar.hide()
        self._import_queue.hide()
        self._import_live_caption.hide()
        self._import_live.hide()
        self._media_cancel_btn.hide()
        self._start_block.hide()

    def _cancel_media_import(self) -> None:
        if self._media_cancel_pending:
            return
        queued = self._queued_media()
        if queued:
            action = self._confirm_cancel_queued_import(len(queued))
            if not action:
                return
            self._media_cancel_pending = True
            if action == "cancel_all":
                cancel_all = getattr(self._ctrl, "cancel_all_media_imports", None)
                if callable(cancel_all):
                    cancel_all()
                else:
                    self._ctrl.cancel_media_import()
                self._queue_signature = None
            else:
                skip_cur = getattr(self._ctrl, "skip_current_media_import", None)
                if callable(skip_cur):
                    skip_cur()
                else:
                    self._ctrl.cancel_media_import()
            self._sync_media_buttons()
            self._apply_progress_widgets()
            return
        if self._stop_needs_confirm() and not self._confirm_cancel_import():
            return
        self._media_cancel_pending = True
        self._ctrl.cancel_media_import()
        self._sync_media_buttons()
        self._apply_progress_widgets()

    def _stop_needs_confirm(self) -> bool:
        if self._import_live.toPlainText().strip():
            return True
        bar = self._import_progress_bar
        return bar.maximum() > 0 and bar.value() >= 50

    def _confirm_cancel_import(self) -> bool:
        from sayink.ui import meeting_page as mp

        box_cls = mp.QMessageBox
        box = box_cls(self)
        box.setWindowTitle("停止识别")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText("要停止识别吗？")
        box.setInformativeText("停在当前这段。下面已经出现的文字会留下，后面不再识别，也不能从这里接着做。")
        stay = box.addButton("继续识别", QMessageBox.ButtonRole.RejectRole)
        stop = box.addButton("停止并保留已识别的文字", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(stay)
        box.setEscapeButton(stay)
        _style_dialog_box(box, danger_btn=stop)
        with _quiet_tooltips(self):
            box.exec()
        return box.clickedButton() is stop

    def _confirm_cancel_queued_import(self, queued_count: int) -> str:
        from sayink.ui import meeting_page as mp

        box_cls = mp.QMessageBox
        box = box_cls(self)
        box.setWindowTitle("停止或跳过当前文件")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(f"当前后面还有 {queued_count} 个文件在排队，要怎样处理？")
        box.setInformativeText(
            "可以选择仅跳过当前正在处理的文件并继续后续排队任务，或一次性全部停止并清空排队列表。"
        )
        stay = box.addButton("继续识别", QMessageBox.ButtonRole.RejectRole)
        skip_one = box.addButton("仅跳过当前文件", QMessageBox.ButtonRole.ActionRole)
        stop_all = box.addButton(
            f"全部停止并清空排队（共 {queued_count} 个）",
            QMessageBox.ButtonRole.DestructiveRole,
        )
        box.setDefaultButton(stay)
        box.setEscapeButton(stay)
        _style_dialog_box(box, danger_btn=stop_all)
        with _quiet_tooltips(self):
            box.exec()
        clicked = box.clickedButton()
        if clicked is skip_one:
            return "skip_current"
        if clicked is stop_all:
            return "cancel_all"
        return ""

    def _on_media_progress(self, text: str) -> None:
        if not text:
            return
        title = str(getattr(self._ctrl, "_media_title", "") or "")
        if title and title != getattr(self, "_media_import_title", ""):
            self._media_import_started = time.monotonic()
            self._clear_import_live()
        if title:
            self._media_import_title = title
        self._media_progress_text = text
        if not getattr(self._ctrl, "importing_media", False):
            return
        if self._import_owns_detail or not self._detail_id:
            self._show_import_detail()
        else:
            self._detail_pane.setVisible(True)
            self._paint_import_banner()

    def _on_media_preview(self, text: str) -> None:
        incoming = text or ""
        prev = getattr(self, "_last_media_preview_text", "")
        sb = self._import_live.verticalScrollBar()
        at_bottom = (not prev) or (sb.maximum() <= 0) or (sb.value() >= max(0, sb.maximum() - 24))
        saved_scroll = sb.value()
        if incoming != prev:
            if prev and incoming.startswith(prev):
                delta = incoming[len(prev):]
                cursor = QTextCursor(self._import_live.document())
                cursor.movePosition(QTextCursor.MoveOperation.End)
                cursor.insertText(delta)
            else:
                self._import_live.setPlainText(incoming)
            self._last_media_preview_text = incoming
            if at_bottom:
                sb.setValue(sb.maximum())
            else:
                sb.setValue(saved_scroll)
        visible = bool(incoming) and bool(getattr(self._ctrl, "importing_media", False))
        self._import_live.setVisible(visible)
        self._import_live_caption.setVisible(visible)

    def _on_media_finished(self, meeting_id: str) -> None:
        imported_path = str(
            getattr(self._ctrl, "last_imported_audio_path", "")
            or self._pending_media_import_path
            or ""
        )
        was_watching = self._import_owns_detail
        more_queued = bool(self._queued_media()) and not getattr(self._ctrl, "_unsaved_media", None)
        self._pending_media_import_path = ""
        if not self._failed_media_path or self._failed_media_path == imported_path:
            self._failed_media_path = ""
        self._media_cancel_pending = False
        if meeting_id:
            self._last_batch_saved_id = meeting_id
            if imported_path:
                self._meeting_audio_paths[meeting_id] = imported_path
        if more_queued:
            self._import_owns_detail = was_watching
            self.refresh()
            notice = getattr(self._ctrl, "media_import_notice", "") or ""
            if notice:
                self._set_status(notice, tone="error" if "失败" in notice else "info")
            return
        self._import_owns_detail = False
        self._hide_import_chrome()
        self._sync_media_buttons()
        last_batch_id = getattr(self, "_last_batch_saved_id", "")
        self._last_batch_saved_id = ""
        if not meeting_id:
            if was_watching and last_batch_id and self._store.get_meeting(last_batch_id) is not None:
                self.show_meeting(last_batch_id)
            else:
                self._render_detail()
                self._detail_pane.setVisible(bool(self._records) or self._editing_notes)
            notice = getattr(self._ctrl, "media_import_notice", "") or "没有识别到文字"
            self._set_status(notice, tone="error" if "失败" in notice else "info")
            return
        if was_watching:
            self.show_meeting(meeting_id)
        else:
            self.refresh()
        notice = getattr(self._ctrl, "media_import_notice", "") or ""
        if notice:
            self._set_status(notice, tone="error" if "失败" in notice else "info")
            return
        record = self._store.get_meeting(meeting_id)
        if record is not None and record.status == STATUS_INTERRUPTED:
            self._set_status("已停止识别。已识别的部分保留为已中断的会议。")
        elif was_watching:
            self._set_status(self._imported_status_message(meeting_id, "音视频"), tone="ok")
        else:
            self._set_status("识别完成，可在列表中打开。", tone="ok")

    def _retry_media_save(self) -> None:
        message = self._ctrl.retry_media_save()
        self._sync_media_buttons()
        if message:
            self._set_status(message, tone="error")

    def _retry_failed_media(self) -> None:
        path = self._failed_media_path
        if not path or not Path(path).is_file():
            self._failed_media_path = ""
            self._sync_media_buttons()
            self._set_status("找不到刚才的文件，无法重新导入。", tone="error")
            return
        self._failed_media_path = ""
        self._begin_media_paths([Path(path)])

    def _on_media_failed(self, message: str) -> None:
        failed = str(
            getattr(self._ctrl, "last_failed_audio_path", "")
            or self._pending_media_import_path
            or getattr(self._ctrl, "_media_source_path", "")
            or ""
        )
        self._failed_media_path = failed
        self._pending_media_import_path = ""
        self._media_cancel_pending = False
        more_queued = bool(self._queued_media()) and not getattr(self._ctrl, "_unsaved_media", None)
        if more_queued:
            self._set_status(message or "导入音视频失败", tone="error")
            return
        was_watching = self._import_owns_detail
        self._import_owns_detail = False
        self._hide_import_chrome()
        self._sync_media_buttons()
        last_batch_id = getattr(self, "_last_batch_saved_id", "")
        self._last_batch_saved_id = ""
        if was_watching and last_batch_id and self._store.get_meeting(last_batch_id) is not None:
            self.show_meeting(last_batch_id)
        else:
            self._render_detail()
            self._detail_pane.setVisible(bool(self._records) or self._editing_notes)
        self._set_status(message or "导入音视频失败", tone="error")

    def _install_import_drop_targets(self) -> None:
        candidates = (
            self._list.viewport(),
            self._search_edit,
            self._browse_left,
            self._empty_label,
            self._splitter,
            self._detail_pane,
            self._notes_stack,
            self._notes_view,
            self._notes_view.viewport(),
            self._notes_empty,
            self._transcript_view,
            self._transcript_view.viewport(),
            self._import_live,
            self._import_live.viewport(),
        )
        self._import_drop_widgets = tuple(w for w in candidates if w is not None)
        for widget in self._import_drop_widgets:
            widget.setAcceptDrops(True)
            widget.installEventFilter(self)

    def dragLeaveEvent(self, event) -> None:
        self._drag_url_cache = None
        super().dragLeaveEvent(event)

    def dragEnterEvent(self, event) -> None:
        if not self._handle_import_drag(event, dropping=False):
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        if not self._handle_import_drag(event, dropping=False):
            super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:
        if not self._handle_import_drag(event, dropping=True):
            super().dropEvent(event)

    def _handle_import_drag(self, event, *, dropping: bool) -> bool:
        mime = event.mimeData()
        if mime is None or not mime.hasUrls():
            return False
        raw_urls = tuple(url.toString() for url in mime.urls())
        cache = getattr(self, "_drag_url_cache", None)
        refresh = dropping or event.type() == QEvent.Type.DragEnter or cache is None or cache[0] != raw_urls
        if not refresh:
            paths = list(cache[1])
        else:
            paths = []
            for url in mime.urls():
                if not url.isLocalFile():
                    continue
                path = Path(url.toLocalFile())
                if path.is_file():
                    paths.append(path)
            self._drag_url_cache = None if dropping else (raw_urls, list(paths))
        if not paths:
            return False
        event.acceptProposedAction()
        if dropping:
            self._accept_dropped_files(paths)
        return True

    def _accept_dropped_files(self, paths: list[Path]) -> None:
        from sayink.audio_file_decoder import SUPPORTED_EXTENSIONS

        text_suffixes = {".txt", ".md", ".srt", ".vtt", ".json"}
        media = [path for path in paths if path.suffix.lower() in SUPPORTED_EXTENSIONS]
        texts = [path for path in paths if path.suffix.lower() in text_suffixes]
        if media:
            ignored = len(paths) - len(media)
            if self.isVisible():
                accepted, error = self._confirm_and_begin_media_paths(media)
                if not accepted:
                    return
            else:
                error = self._begin_media_paths(media)
            if error:
                if ignored:
                    self._set_status(f"{error} 已忽略 {ignored} 个不是音视频的文件。", tone="error")
                return
            if ignored:
                self._set_status(f"已开始导入音视频，已忽略 {ignored} 个不是音视频的文件。")
            return
        if texts:
            self._import_dropped_transcript(texts[0], ignored=max(0, len(paths) - 1))
            return
        self._set_status(
            "不能导入这些文件。音视频请用支持的格式，文字稿用 .txt、.md、.srt、.vtt 或 .json。",
            tone="error",
        )

    def _import_dropped_transcript(self, path: Path, *, ignored: int) -> None:
        if self._ctrl.is_active:
            self._set_status("会议进行中，请结束后再导入文字稿")
            return
        from sayink.ui.transcript_import_dialog import TranscriptImportDialog

        dialog = TranscriptImportDialog(self)
        loaded = dialog._load_file_path(path)
        if loaded and ignored:
            dialog._extra_note = f"一次只能导入一个文件，已忽略其余 {ignored} 个。"
            dialog._refresh()
        with _quiet_tooltips(self):
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
        if not accepted:
            return
        try:
            record = self._ctrl.import_transcript(
                dialog.transcript_text(),
                title=dialog.meeting_title(),
                detect_structure=dialog.detect_structure(),
                started_at=dialog.meeting_started_ms(),
                parsed=dialog.parsed(),
            )
        except TypeError:
            record = self._ctrl.import_transcript(
                dialog.transcript_text(),
                title=dialog.meeting_title(),
                detect_structure=dialog.detect_structure(),
                started_at=dialog.meeting_started_ms(),
            )
        if record is None:
            self._set_status(getattr(self._ctrl, "transcript_import_notice", "") or "没有可导入的文字", tone="error")
            return
        self.show_meeting(record.id)
        self._set_status(self._imported_status_message(record.id, "文字稿"), tone="ok")


__all__ = ["_MeetingImportMixin", "_ORIG_GET_OPEN_FILE_NAME", "_format_import_remain"]

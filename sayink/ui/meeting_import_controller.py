"""Composed transcript/media import queue, progress chrome, and drag-and-drop controller for ``MeetingPage``.

Replaces inheritance-based ``_MeetingImportMixin`` with explicit composition.
"""

from __future__ import annotations

from pathlib import Path
import re
import time
from typing import Any

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
_DROPPABLE_TRANSCRIPT_SUFFIXES = frozenset({".txt", ".md", ".docx", ".srt", ".vtt", ".json"})


def _format_import_remain(seconds: float) -> str:
    remaining = max(0, int(round(seconds)))
    if remaining < 60:
        return f"大约还需 {remaining} 秒"
    minutes, secs = divmod(remaining, 60)
    if minutes < 60:
        return f"大约还需 {minutes} 分 {secs} 秒" if secs else f"大约还需 {minutes} 分钟"
    hours, minutes = divmod(minutes, 60)
    return f"大约还需 {hours} 小时 {minutes} 分钟"


def _media_stop_button_state(recognizing: bool, pending: bool) -> tuple[str, str]:
    if pending:
        label = "正在停止…"
    elif recognizing:
        label = "停止识别"
    else:
        label = "取消导入"
    tip = (
        "停在当前这段之后。已经识别的文字会留下，后面不再识别。"
        if recognizing or pending
        else "还没开始识别，取消后不会留下文字。"
    )
    return label, tip


def _parse_progress_percent(raw_text: str) -> tuple[int | None, bool]:
    """Return ``(pct_or_none, determinate)`` extracted from progress text."""
    explicit = re.findall(r"[（(]\s*(\d+)\s*%\s*[）)]", raw_text)
    if explicit:
        return max(0, min(100, int(explicit[-1]))), True
    match = re.search(r"(\d+)\s*/\s*(\d+)", raw_text)
    if match and int(match.group(2)) > 0:
        done, total = int(match.group(1)), int(match.group(2))
        return max(0, min(100, int(round(done * 100.0 / total)))), True
    pct_match = re.search(r"(\d+)\s*%", raw_text)
    if pct_match:
        return max(0, min(100, int(pct_match.group(1)))), True
    return None, False


def _format_progress_display(
    raw_text: str,
    pct: int | None,
    remain: str,
    *,
    cancel_pending: bool,
) -> str:
    if cancel_pending:
        return "正在停止，当前这段完成后保留已有文字"
    head = "正在识别" if "正在识别" in raw_text else "正在读取"
    parts = [head]
    if pct is not None:
        parts.append(f"{pct}%")
    if remain:
        parts.append(remain)
    return " · ".join(parts)


class MeetingImportController:
    """Composed import dialog orchestration, media queue UI, and file drop handler."""

    def __init__(self, host: Any) -> None:
        self._host = host

    def imported_status_message(self, meeting_id: str, kind_label: str) -> str:
        h = self._host
        if h._ctrl.generating_for == meeting_id:
            return f"已导入{kind_label}，正在自动生成纪要。"
        job = h._ctrl.queued_generation(meeting_id)
        if job is not None and not job.failed:
            return f"已导入{kind_label}，已自动加入纪要生成队列。"
        if kind_label == "音视频":
            return "已导入音视频。可先核对文字稿，或切换到「纪要」点击「生成纪要」。"
        return "已导入文字稿。确认内容后，可切换到「纪要」点击「生成纪要」。"

    def _commit_transcript_payload(
        self,
        text: str,
        title: str,
        detect: bool,
        started: int | None,
        preparsed: Any,
    ) -> Any:
        h = self._host
        try:
            return h._ctrl.import_transcript(
                text,
                title=title,
                detect_structure=detect,
                started_at=started,
                parsed=preparsed,
            )
        except TypeError:
            return h._ctrl.import_transcript(
                text,
                title=title,
                detect_structure=detect,
                started_at=started,
            )

    def _transcript_commit_error(self) -> str:
        notice = str(getattr(self._host._ctrl, "transcript_import_notice", "") or "").strip()
        return notice or "无法写入数据库，请重试；框内文字已保留。"

    def ask_import(self) -> tuple[Any, ...] | None:
        from sayink.ui.transcript_import_dialog import TranscriptImportDialog

        h = self._host
        dialog = TranscriptImportDialog(h)
        with _quiet_tooltips(h):
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

    def _import_transcript_via_hook(self) -> None:
        h = self._host
        asked = h._ask_import()
        if not asked:
            return
        text, title, detect = asked[:3]
        started = asked[3] if len(asked) > 3 else None
        preparsed = asked[4] if len(asked) > 4 else None
        record = self._commit_transcript_payload(text, title, detect, started, preparsed)
        if record is None:
            h._unsaved_transcript_draft = (text, title, detect, started)
            h._set_status(getattr(h._ctrl, "transcript_import_notice", "") or "没有可导入的文字", tone="error")
            return
        h._unsaved_transcript_draft = None
        h.show_meeting(record.id)
        h._set_status(self.imported_status_message(record.id, "文字稿"))

    def import_transcript(self) -> None:
        from sayink.ui.transcript_import_dialog import TranscriptImportDialog

        h = self._host
        if h._ctrl.is_active:
            h._set_status("会议进行中，请结束后再导入文字稿")
            return
        if "_ask_import" in h.__dict__:
            self._import_transcript_via_hook()
            return
        dialog = TranscriptImportDialog(
            h,
            commit_fn=self._commit_transcript_payload,
            commit_error_fn=self._transcript_commit_error,
        )
        draft = getattr(h, "_unsaved_transcript_draft", None)
        if isinstance(draft, tuple) and len(draft) >= 3 and str(draft[0]).strip():
            dialog._edit.setPlainText(str(draft[0]))
            if str(draft[1]).strip():
                dialog._title.setText(str(draft[1]))
            dialog._detect.setChecked(bool(draft[2]))
        with _quiet_tooltips(h):
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
        if not accepted:
            return
        record = dialog.committed_record()
        if record is None:
            record = self._commit_transcript_payload(
                dialog.transcript_text(),
                dialog.meeting_title(),
                dialog.detect_structure(),
                dialog.meeting_started_ms(),
                dialog.parsed(),
            )
        if record is None:
            h._unsaved_transcript_draft = (
                dialog.transcript_text(),
                dialog.meeting_title(),
                dialog.detect_structure(),
                dialog.meeting_started_ms(),
            )
            h._set_status(getattr(h._ctrl, "transcript_import_notice", "") or "没有可导入的文字", tone="error")
            return
        h._unsaved_transcript_draft = None
        h.show_meeting(record.id)
        h._set_status(self.imported_status_message(record.id, "文字稿"))

    def import_is_recognizing(self) -> bool:
        return "正在识别" in (getattr(self._host, "_media_progress_text", "") or "")

    def queued_media(self) -> tuple[tuple[str, str], ...]:
        h = self._host
        queued = getattr(h._ctrl, "queued_media", None)
        if callable(queued):
            return tuple(queued())
        return tuple(getattr(h._ctrl, "_media_queue", ()) or ())

    def sync_import_queue(self, queued: Any) -> None:
        from sayink.ui import settings_styles as ss

        h = self._host
        signature = tuple((str(path), str(title)) for path, title in queued)
        if signature == getattr(h, "_queue_signature", None):
            h._import_queue.setVisible(bool(signature))
            return
        h._queue_signature = signature
        while h._import_queue_lay.count():
            item = h._import_queue_lay.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
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
            remove.clicked.connect(lambda _checked=False, item_index=index: h._remove_queued_media(item_index))
            lay.addWidget(label, 1)
            lay.addWidget(remove, 0)
            h._import_queue_lay.addWidget(row)
        h._import_queue.setVisible(bool(signature))

    def remove_queued_media(self, index: int) -> None:
        h = self._host
        remove = getattr(h._ctrl, "remove_queued_media", None)
        if callable(remove):
            remove(index)
        h._queue_signature = None
        self.sync_media_buttons()

    def _sync_import_header_banner(
        self,
        *,
        busy: bool,
        browsing: bool,
        recognizing: bool,
        queued: tuple[tuple[str, str], ...],
    ) -> None:
        h = self._host
        phase = "正在识别" if recognizing else "正在读取"
        if queued:
            phase += f" · 还有 {len(queued)} 个在排队"
        if busy and browsing:
            h._import_kicker.setText(phase)
            h._import_kicker.setVisible(True)
            title = getattr(h, "_media_import_title", "") or ""
            h._import_filename.set_full_text(title)
            h._import_filename.setVisible(bool(title))
        else:
            h._import_kicker.hide()
            h._import_filename.hide()

    def _has_unsaved_media(self) -> bool:
        return bool(getattr(self._host._ctrl, "_unsaved_media", None))

    def _unsaved_media_title(self) -> str:
        fn = getattr(self._host._ctrl, "unsaved_media_title", None)
        if callable(fn):
            return str(fn() or "").strip()
        raw = getattr(self._host._ctrl, "_unsaved_media", None)
        if isinstance(raw, tuple) and len(raw) >= 2:
            return str(raw[1] or "").strip()
        return str(getattr(raw, "title", "") or "").strip()

    def _has_unsaved_finalize(self) -> bool:
        fn = getattr(self._host._ctrl, "has_unsaved_finalize", None)
        return bool(fn()) if callable(fn) else False

    def _unsaved_finalize_title(self) -> str:
        fn = getattr(self._host._ctrl, "unsaved_finalize_title", None)
        return str(fn() or "").strip() if callable(fn) else ""

    def _sync_unsaved_media_buttons(self, *, busy: bool, browsing: bool) -> None:
        h = self._host
        show_media = browsing and not busy and self._has_unsaved_media()
        media_title = self._unsaved_media_title() if show_media else ""
        short_media = (media_title[:16] + "…") if len(media_title) > 17 else media_title
        h._media_retry_btn.setText(f"重试保存（{short_media}）" if short_media else "重试保存")
        h._media_retry_btn.setToolTip(
            f"「{media_title}」上次识别的文字还在，再写入一次数据库"
            if media_title
            else "上次识别的文字还在，再写入一次数据库"
        )
        h._media_retry_btn.setVisible(show_media)
        if hasattr(h, "_media_discard_btn"):
            h._media_discard_btn.setVisible(show_media)

    def _sync_unsaved_finalize_buttons(self, *, busy: bool, browsing: bool) -> None:
        h = self._host
        active = bool(getattr(h._ctrl, "is_active", False))
        show_fin = browsing and not busy and not active and self._has_unsaved_finalize()
        fin_title = self._unsaved_finalize_title() if show_fin else ""
        if hasattr(h, "_finalize_retry_btn"):
            h._finalize_retry_btn.setText("重试保存结束状态")
            h._finalize_retry_btn.setToolTip(
                f"「{fin_title}」结束状态尚未保存，点击重试写入数据库"
                if fin_title
                else "刚才结束的会议尚未保存终态，点击重试写入数据库"
            )
            h._finalize_retry_btn.setVisible(show_fin)
        if hasattr(h, "_finalize_discard_btn"):
            h._finalize_discard_btn.setToolTip(
                f"放弃「{fin_title}」未保存的结束状态更新"
                if fin_title
                else "放弃未保存的会议结束状态更新"
            )
            h._finalize_discard_btn.setVisible(show_fin)

    def _sync_unsaved_recovery_buttons(self, *, busy: bool, browsing: bool) -> None:
        self._sync_unsaved_media_buttons(busy=busy, browsing=browsing)
        self._sync_unsaved_finalize_buttons(busy=busy, browsing=browsing)

    def _sync_retry_and_cancel_buttons(
        self,
        *,
        busy: bool,
        browsing: bool,
        recognizing: bool,
    ) -> None:
        h = self._host
        pending = busy and h._media_cancel_pending
        h._media_cancel_btn.setVisible(busy and browsing)
        h._media_cancel_btn.setEnabled(not pending)
        stop_label, stop_tip = _media_stop_button_state(recognizing, pending)
        h._media_cancel_btn.setText(stop_label)
        h._media_cancel_btn.setToolTip(stop_tip)
        if not busy:
            h._media_cancel_pending = False
        self._sync_unsaved_recovery_buttons(busy=busy, browsing=browsing)
        failed_path = getattr(h, "_failed_media_path", "") or ""
        h._media_again_btn.setVisible(
            browsing and not busy and bool(failed_path) and Path(failed_path).is_file()
        )

    def sync_media_buttons(self) -> None:
        from sayink.ui import meeting_page as mp

        h = self._host
        busy = bool(getattr(h._ctrl, "importing_media", False))
        browsing = h._stack.currentIndex() == mp.PAGE_BROWSE
        active = bool(getattr(h._ctrl, "is_active", False))
        h._import_btn.setVisible(browsing)
        h._media_btn.setVisible(browsing)
        h._media_btn.setEnabled(not active)
        h._import_btn.setEnabled(not active)
        h._start_btn.setEnabled(not busy and not active)
        h._media_btn.setText("加入队列" if busy else "导入音视频")
        h._media_btn.setToolTip("正在识别。再选或拖入的文件会排在后面。" if busy else h._media_tip)
        recognizing = self.import_is_recognizing()
        block = "正在识别，结束后可以开始会议" if recognizing else "正在读取，结束后可以开始会议"
        h._start_btn.setToolTip(block if busy else h._start_tip)
        h._start_block.setText(block if busy else "")
        h._start_block.setVisible(busy and browsing)
        h._import_btn.setToolTip("会议进行中，请结束后再导入文字稿" if active else h._import_tip)
        self._sync_retry_and_cancel_buttons(busy=busy, browsing=browsing, recognizing=recognizing)
        queued = self.queued_media()
        self._sync_import_header_banner(busy=busy, browsing=browsing, recognizing=recognizing, queued=queued)
        self.sync_import_queue(queued if busy else ())

    def existing_media_audio_paths(self) -> set[str]:
        h = self._host
        existing: set[str] = set()
        for rec in (getattr(h, "_records", {}) or {}).values():
            ap = str(getattr(rec, "audio_path", "") or "").strip()
            if ap:
                existing.add(ap)
        for ap in getattr(h, "_meeting_audio_paths", {}).values():
            if ap:
                existing.add(str(ap))
        cur_src = str(getattr(h._ctrl, "_media_source_path", "") or "").strip()
        if cur_src:
            existing.add(cur_src)
        for queued_path, _title in self.queued_media():
            if queued_path:
                existing.add(str(queued_path))
        return existing

    def import_media(self) -> None:
        from sayink.ui import meeting_page as mp

        h = self._host
        if self._has_unsaved_media() or not getattr(h._ctrl, "importing_media", False):
            reason = h._ctrl.media_block_reason()
            if reason:
                h._set_status(reason, tone="error")
                return
        file_dialog = mp.QFileDialog
        if file_dialog.getOpenFileName is not _ORIG_GET_OPEN_FILE_NAME:
            with native_file_dialog(h):
                path, _selected = file_dialog.getOpenFileName(h, "导入音视频", "", file_dialog_filter())
            if path:
                h._begin_media_paths([Path(path)])
            return
        with native_file_dialog(h):
            paths, _selected = file_dialog.getOpenFileNames(h, "导入音视频", "", file_dialog_filter())
        if paths:
            h._confirm_and_begin_media_paths([Path(p) for p in paths])

    def confirm_and_begin_media_paths(self, paths: list[Path]) -> tuple[bool, str]:
        h = self._host
        dialog = MediaImportDialog(
            paths,
            config=h._config,
            existing_audio_paths=self.existing_media_audio_paths(),
            queueing=bool(getattr(h._ctrl, "importing_media", False)),
            parent=h,
        )
        with _quiet_tooltips(h):
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
        if not accepted:
            return False, ""
        requests = dialog.import_requests()
        if not requests:
            return False, ""
        return True, self.begin_media_requests(requests)

    def begin_media_paths(self, paths: list[Path]) -> str:
        ordered = sorted(paths, key=natural_media_sort_key) if len(paths) > 1 else list(paths)
        return self.begin_media_requests([(path, path.stem, {}) for path in ordered])

    @staticmethod
    def _build_start_kwargs(path: Path, title: str, opts: dict[str, Any]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"title": title or path.stem}
        for key in ("language", "model_id", "started_at", "auto_generate"):
            if opts and key in opts and opts[key] not in ("", None):
                kwargs[key] = opts[key]
        return kwargs

    def begin_media_requests(self, requests: list[tuple[Path, str, dict[str, Any]]]) -> str:
        h = self._host
        if not getattr(h._ctrl, "importing_media", False):
            h._last_batch_saved_id = ""
        for path, title, opts in requests:
            was_importing = bool(getattr(h._ctrl, "importing_media", False))
            kwargs = self._build_start_kwargs(path, title, opts)
            try:
                message = h._ctrl.start_media_import(str(path), **kwargs)
            except TypeError:
                message = h._ctrl.start_media_import(str(path), title=title or path.stem)
            if message:
                h._set_status(message, tone="error")
                return message
            notice = str(getattr(h._ctrl, "media_queue_notice", "") or "")
            if notice or was_importing:
                h._ctrl.media_queue_notice = ""
                if notice:
                    h._set_status(notice)
                self.sync_media_buttons()
                continue
            self.arm_import_view(path, title=title or path.stem)
        return ""

    def clear_import_live(self) -> None:
        h = self._host
        h._last_media_preview_text = ""
        h._import_live.clear()

    def arm_import_view(self, path: Path, *, title: str = "") -> None:
        h = self._host
        h._failed_media_path = ""
        h._media_cancel_pending = False
        h._pending_media_import_path = str(path)
        h._media_import_started = time.monotonic()
        h._import_owns_detail = True
        h._media_import_title = (title or path.stem).strip()
        h._media_progress_text = "正在读取音频…"
        self.clear_import_live()
        self.sync_media_buttons()
        self.show_import_detail()

    def on_media_started(self, path: str, title: str) -> None:
        h = self._host
        resolved_title = (title or (Path(path).stem if path else "")).strip()
        h._failed_media_path = ""
        h._media_cancel_pending = False
        h._pending_media_import_path = str(path or "")
        h._media_import_started = time.monotonic()
        if resolved_title:
            h._media_import_title = resolved_title
        h._media_progress_text = "正在读取音频…"
        self.clear_import_live()
        if h._import_owns_detail or not h._detail_id:
            self.show_import_detail()
        else:
            h._detail_pane.setVisible(True)
            self.paint_import_banner()

    def apply_progress_widgets(self) -> None:
        h = self._host
        raw_text = getattr(h, "_media_progress_text", "") or "正在读取音频…"
        pct, determinate = _parse_progress_percent(raw_text)
        if determinate and pct is not None:
            h._import_progress_bar.setRange(0, 100)
            h._import_progress_bar.setValue(pct)
        else:
            h._import_progress_bar.setRange(0, 0)
        remain = ""
        if pct is not None and 5 <= pct <= 95 and h._media_import_started:
            elapsed = time.monotonic() - h._media_import_started
            if elapsed >= 3:
                remain = _format_import_remain(elapsed * (100 - pct) / max(pct, 1))
        display_text = _format_progress_display(
            raw_text, pct, remain, cancel_pending=bool(h._media_cancel_pending)
        )
        h._import_progress.setVisible(True)
        h._import_progress.setText(display_text)
        h._import_progress_bar.setVisible(True)

    def show_import_detail(self) -> None:
        h = self._host
        h._import_owns_detail = True
        h._set_single_detail_visible(False)
        h._batch_tools.hide()
        h._batch_notice_scroll.hide()
        h._audio_missing.hide()
        h._status_label.hide()
        h._detail_pane.setVisible(True)
        h._detail_title.setReadOnly(True)
        h._detail_title.hide()
        h._detail_meta.setVisible(True)
        h._detail_meta.setText(
            "点左侧其他会议可以先看。识别会在后台继续（支持随时按快捷键语音输入）；开始新会议需等导入结束。"
        )
        h._list.blockSignals(True)
        h._list.clearSelection()
        h._list.setCurrentItem(None)
        h._list.blockSignals(False)
        self.apply_progress_widgets()
        self.sync_media_buttons()

    def paint_import_banner(self) -> None:
        self.apply_progress_widgets()
        self.sync_media_buttons()

    def hide_import_chrome(self) -> None:
        h = self._host
        for widget in (
            h._import_kicker,
            h._import_filename,
            h._import_progress,
            h._import_progress_bar,
            h._import_queue,
            h._import_live_caption,
            h._import_live,
            h._media_cancel_btn,
            h._start_block,
        ):
            widget.hide()

    def _cancel_queued_media_flow(self, queued_count: int) -> None:
        h = self._host
        action = h._confirm_cancel_queued_import(queued_count)
        if not action:
            return
        h._media_cancel_pending = True
        if action == "cancel_all":
            cancel_all = getattr(h._ctrl, "cancel_all_media_imports", None)
            if callable(cancel_all):
                cancel_all()
            else:
                h._ctrl.cancel_media_import()
            h._queue_signature = None
        else:
            skip_cur = getattr(h._ctrl, "skip_current_media_import", None)
            if callable(skip_cur):
                skip_cur()
            else:
                h._ctrl.cancel_media_import()
        self.sync_media_buttons()
        self.apply_progress_widgets()

    def cancel_media_import(self) -> None:
        h = self._host
        if h._media_cancel_pending:
            return
        queued = self.queued_media()
        if queued:
            self._cancel_queued_media_flow(len(queued))
            return
        if self.stop_needs_confirm() and not h._confirm_cancel_import():
            return
        h._media_cancel_pending = True
        h._ctrl.cancel_media_import()
        self.sync_media_buttons()
        self.apply_progress_widgets()

    def stop_needs_confirm(self) -> bool:
        h = self._host
        if h._import_live.toPlainText().strip():
            return True
        bar = h._import_progress_bar
        return bar.maximum() > 0 and bar.value() >= 50

    def confirm_cancel_import(self) -> bool:
        from sayink.ui import meeting_page as mp

        h = self._host
        box = mp.QMessageBox(h)
        box.setWindowTitle("停止识别")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText("要停止识别吗？")
        box.setInformativeText("停在当前这段。下面已经出现的文字会留下，后面不再识别，也不能从这里接着做。")
        stay = box.addButton("继续识别", QMessageBox.ButtonRole.RejectRole)
        stop = box.addButton("停止并保留已识别的文字", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(stay)
        box.setEscapeButton(stay)
        _style_dialog_box(box, danger_btn=stop)
        with _quiet_tooltips(h):
            box.exec()
        return box.clickedButton() is stop

    def confirm_cancel_queued_import(self, queued_count: int) -> str:
        from sayink.ui import meeting_page as mp

        h = self._host
        box = mp.QMessageBox(h)
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
        with _quiet_tooltips(h):
            box.exec()
        clicked = box.clickedButton()
        if clicked is skip_one:
            return "skip_current"
        if clicked is stop_all:
            return "cancel_all"
        return ""

    def on_media_progress(self, text: str) -> None:
        h = self._host
        if not text:
            return
        title = str(getattr(h._ctrl, "_media_title", "") or "")
        if title and title != getattr(h, "_media_import_title", ""):
            h._media_import_started = time.monotonic()
            self.clear_import_live()
        if title:
            h._media_import_title = title
        h._media_progress_text = text
        if not getattr(h._ctrl, "importing_media", False):
            return
        if h._import_owns_detail or not h._detail_id:
            self.show_import_detail()
        else:
            h._detail_pane.setVisible(True)
            self.paint_import_banner()

    def on_media_preview(self, text: str) -> None:
        h = self._host
        incoming = text or ""
        prev = getattr(h, "_last_media_preview_text", "")
        sb = h._import_live.verticalScrollBar()
        at_bottom = (not prev) or (sb.maximum() <= 0) or (sb.value() >= max(0, sb.maximum() - 24))
        saved_scroll = sb.value()
        if incoming != prev:
            if prev and incoming.startswith(prev):
                cursor = QTextCursor(h._import_live.document())
                cursor.movePosition(QTextCursor.MoveOperation.End)
                cursor.insertText(incoming[len(prev) :])
            else:
                h._import_live.setPlainText(incoming)
            h._last_media_preview_text = incoming
            sb.setValue(sb.maximum() if at_bottom else saved_scroll)
        visible = bool(incoming) and bool(getattr(h._ctrl, "importing_media", False))
        h._import_live.setVisible(visible)
        h._import_live_caption.setVisible(visible)

    def _finish_empty_media_import(self, was_watching: bool, last_batch_id: str) -> None:
        h = self._host
        if was_watching and last_batch_id and h._store.get_meeting(last_batch_id) is not None:
            h.show_meeting(last_batch_id)
        else:
            h._render_detail()
            h._detail_pane.setVisible(bool(h._records) or h._editing_notes)
        notice = getattr(h._ctrl, "media_import_notice", "") or "没有识别到文字"
        h._set_status(notice, tone="error" if "失败" in notice else "info")

    def _finish_saved_media_import(self, meeting_id: str, was_watching: bool) -> None:
        h = self._host
        if was_watching:
            h.show_meeting(meeting_id)
        else:
            h.refresh()
        notice = getattr(h._ctrl, "media_import_notice", "") or ""
        if notice:
            h._set_status(notice, tone="error" if "失败" in notice else "info")
            return
        record = h._store.get_meeting(meeting_id)
        if record is not None and record.status == STATUS_INTERRUPTED:
            h._set_status("已停止识别。已识别的部分保留为已中断的会议。")
        elif was_watching:
            h._set_status(self.imported_status_message(meeting_id, "音视频"), tone="ok")
        else:
            h._set_status("识别完成，可在列表中打开。", tone="ok")

    def on_media_finished(self, meeting_id: str) -> None:
        h = self._host
        imported_path = str(
            getattr(h._ctrl, "last_imported_audio_path", "") or h._pending_media_import_path or ""
        )
        was_watching = h._import_owns_detail
        more_queued = bool(self.queued_media()) and not getattr(h._ctrl, "_unsaved_media", None)
        h._pending_media_import_path = ""
        if not h._failed_media_path or h._failed_media_path == imported_path:
            h._failed_media_path = ""
        h._media_cancel_pending = False
        if meeting_id:
            h._last_batch_saved_id = meeting_id
            if imported_path:
                h._meeting_audio_paths[meeting_id] = imported_path
        if more_queued:
            h._import_owns_detail = was_watching
            h.refresh()
            notice = getattr(h._ctrl, "media_import_notice", "") or ""
            if notice:
                h._set_status(notice, tone="error" if "失败" in notice else "info")
            return
        h._import_owns_detail = False
        self.hide_import_chrome()
        self.sync_media_buttons()
        last_batch_id = getattr(h, "_last_batch_saved_id", "")
        h._last_batch_saved_id = ""
        if not meeting_id:
            self._finish_empty_media_import(was_watching, last_batch_id)
            return
        self._finish_saved_media_import(meeting_id, was_watching)

    def retry_media_save(self) -> None:
        h = self._host
        message = h._ctrl.retry_media_save()
        self.sync_media_buttons()
        if message:
            h._set_status(message, tone="error")

    def discard_unsaved_media(self) -> None:
        from sayink.ui import meeting_page as mp

        h = self._host
        if not self._has_unsaved_media():
            self.sync_media_buttons()
            return
        title = self._unsaved_media_title()
        box = mp.QMessageBox(h)
        box.setWindowTitle("放弃未保存结果")
        box.setIcon(mp.QMessageBox.Icon.Warning)
        box.setText(
            f"放弃「{title}」尚未保存的导入识别结果？"
            if title
            else "放弃尚未保存的导入识别结果？"
        )
        box.setInformativeText("放弃后本次已识别的文字将被清除，后续若仍需要须重新识别音视频文件。")
        stay = box.addButton("保留并稍后重试", QMessageBox.ButtonRole.RejectRole)
        discard = box.addButton("放弃未保存结果", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(stay)
        box.setEscapeButton(stay)
        _style_dialog_box(box, danger_btn=discard)
        with _quiet_tooltips(h):
            box.exec()
        if box.clickedButton() is not discard:
            return
        discard_fn = getattr(h._ctrl, "discard_unsaved_media", None)
        if callable(discard_fn):
            ok = bool(discard_fn())
        else:
            h._ctrl._unsaved_media = None
            ok = True
        self.sync_media_buttons()
        h.refresh()
        if ok:
            h._set_status("已放弃未保存的导入结果。")

    def on_finalize_save_failed(self, meeting_id: str, message: str) -> None:
        from sayink.ui import meeting_page as mp

        h = self._host
        if h._stack.currentIndex() == mp.PAGE_LIVE:
            h._show_ended_meeting(meeting_id)
        else:
            h.refresh()
        self.sync_media_buttons()
        h._set_status(
            message or "会议结束状态未能写入数据库，可点击「重试保存结束状态」。",
            tone="error",
        )

    def retry_finalize_save(self) -> None:
        h = self._host
        if not self._has_unsaved_finalize():
            self.sync_media_buttons()
            return
        retry_fn = getattr(h._ctrl, "retry_finalize_save", None)
        if not callable(retry_fn):
            return
        res = retry_fn()
        still_unsaved = self._has_unsaved_finalize()
        self.sync_media_buttons()
        if still_unsaved:
            err = res.strip() if isinstance(res, str) and res.strip() else "会议结束状态仍未保存，请稍后重试。"
            h._set_status(err, tone="error")
            return
        mid = res.strip() if isinstance(res, str) else ""
        if mid and h._store.get_meeting(mid) is not None:
            h.show_meeting(mid)
        else:
            h.refresh()
        h._set_status("已保存会议结束状态。", tone="ok")

    def discard_unsaved_finalize(self) -> None:
        from sayink.ui import meeting_page as mp

        h = self._host
        if not self._has_unsaved_finalize():
            self.sync_media_buttons()
            return
        title = self._unsaved_finalize_title()
        box = mp.QMessageBox(h)
        box.setWindowTitle("放弃未保存的结束状态")
        box.setIcon(mp.QMessageBox.Icon.Warning)
        box.setText(
            f"放弃「{title}」尚未保存的会议结束状态？"
            if title
            else "放弃尚未保存的会议结束状态？"
        )
        box.setInformativeText("已落盘的会议文字稿仍会保留在列表中，但本次结束时间与说话人整理将不再重试写入。")
        stay = box.addButton("保留并稍后重试", QMessageBox.ButtonRole.RejectRole)
        discard = box.addButton("放弃", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(stay)
        box.setEscapeButton(stay)
        _style_dialog_box(box, danger_btn=discard)
        with _quiet_tooltips(h):
            box.exec()
        if box.clickedButton() is not discard:
            return
        discard_fn = getattr(h._ctrl, "discard_unsaved_finalize", None)
        ok = bool(discard_fn()) if callable(discard_fn) else False
        self.sync_media_buttons()
        h.refresh()
        if ok:
            h._set_status("已放弃未保存的会议结束状态。")

    def retry_failed_media(self) -> None:
        h = self._host
        path = h._failed_media_path
        if not path or not Path(path).is_file():
            h._failed_media_path = ""
            self.sync_media_buttons()
            h._set_status("找不到刚才的文件，无法重新导入。", tone="error")
            return
        if self._has_unsaved_media() or not getattr(h._ctrl, "importing_media", False):
            reason = h._ctrl.media_block_reason()
            if reason:
                h._set_status(reason, tone="error")
                return
        h._failed_media_path = ""
        err = self.begin_media_paths([Path(path)])
        if err:
            h._failed_media_path = path
            self.sync_media_buttons()

    def on_media_failed(self, message: str) -> None:
        h = self._host
        failed = str(
            getattr(h._ctrl, "last_failed_audio_path", "")
            or h._pending_media_import_path
            or getattr(h._ctrl, "_media_source_path", "")
            or ""
        )
        h._failed_media_path = failed
        h._pending_media_import_path = ""
        h._media_cancel_pending = False
        more_queued = bool(self.queued_media()) and not getattr(h._ctrl, "_unsaved_media", None)
        if more_queued:
            h._set_status(message or "导入音视频失败", tone="error")
            return
        was_watching = h._import_owns_detail
        h._import_owns_detail = False
        self.hide_import_chrome()
        self.sync_media_buttons()
        last_batch_id = getattr(h, "_last_batch_saved_id", "")
        h._last_batch_saved_id = ""
        if was_watching and last_batch_id and h._store.get_meeting(last_batch_id) is not None:
            h.show_meeting(last_batch_id)
        else:
            h._render_detail()
            h._detail_pane.setVisible(bool(h._records) or h._editing_notes)
        h._set_status(message or "导入音视频失败", tone="error")

    def install_import_drop_targets(self) -> None:
        h = self._host
        candidates = (
            h._list.viewport(),
            h._search_edit,
            h._browse_left,
            h._empty_label,
            h._splitter,
            h._detail_pane,
            h._notes_stack,
            h._notes_view,
            h._notes_view.viewport(),
            h._notes_empty,
            h._transcript_view,
            h._transcript_view.viewport(),
            h._import_live,
            h._import_live.viewport(),
        )
        h._import_drop_widgets = tuple(w for w in candidates if w is not None)
        for widget in h._import_drop_widgets:
            widget.setAcceptDrops(True)
            widget.installEventFilter(h)

    def handle_import_drag(self, event: Any, *, dropping: bool) -> bool:
        h = self._host
        mime = event.mimeData()
        if mime is None or not mime.hasUrls():
            return False
        raw_urls = tuple(url.toString() for url in mime.urls())
        cache = getattr(h, "_drag_url_cache", None)
        refresh = (
            dropping or event.type() == QEvent.Type.DragEnter or cache is None or cache[0] != raw_urls
        )
        if not refresh and cache is not None:
            paths = list(cache[1])
        else:
            paths = [
                Path(url.toLocalFile())
                for url in mime.urls()
                if url.isLocalFile() and Path(url.toLocalFile()).is_file()
            ]
            h._drag_url_cache = None if dropping else (raw_urls, list(paths))
        if not paths:
            return False
        event.acceptProposedAction()
        if dropping:
            h._accept_dropped_files(paths)
        return True

    def _accept_dropped_media(self, media: list[Path], ignored: int) -> None:
        h = self._host
        if self._has_unsaved_media() or not getattr(h._ctrl, "importing_media", False):
            reason = h._ctrl.media_block_reason()
            if reason:
                msg = f"{reason} 已忽略 {ignored} 个不是音视频的文件。" if ignored else reason
                h._set_status(msg, tone="error")
                return
        if h.isVisible():
            accepted, error = h._confirm_and_begin_media_paths(media)
            if not accepted:
                return
        else:
            error = h._begin_media_paths(media)
        if error:
            if ignored:
                h._set_status(f"{error} 已忽略 {ignored} 个不是音视频的文件。", tone="error")
            return
        if ignored:
            h._set_status(f"已开始导入音视频，已忽略 {ignored} 个不是音视频的文件。")

    def accept_dropped_files(self, paths: list[Path]) -> None:
        from sayink.audio_file_decoder import SUPPORTED_EXTENSIONS

        h = self._host
        media = [path for path in paths if path.suffix.lower() in SUPPORTED_EXTENSIONS]
        texts = [path for path in paths if path.suffix.lower() in _DROPPABLE_TRANSCRIPT_SUFFIXES]
        if media:
            self._accept_dropped_media(media, len(paths) - len(media))
            return
        if texts:
            h._import_dropped_transcript(texts[0], ignored=max(0, len(paths) - 1))
            return
        h._set_status(
            "不能导入这些文件。音视频请用支持的格式，文字稿用 .txt、.md、.docx、.srt、.vtt 或 .json。",
            tone="error",
        )

    def import_dropped_transcript(self, path: Path, *, ignored: int) -> None:
        from sayink.ui.transcript_import_dialog import TranscriptImportDialog

        h = self._host
        if h._ctrl.is_active:
            h._set_status("会议进行中，请结束后再导入文字稿")
            return
        dialog = TranscriptImportDialog(
            h,
            commit_fn=self._commit_transcript_payload,
            commit_error_fn=self._transcript_commit_error,
        )
        loaded = dialog._load_file_path(path)
        if loaded and ignored:
            dialog._extra_note = f"一次只能导入一个文件，已忽略其余 {ignored} 个。"
            dialog._refresh()
        with _quiet_tooltips(h):
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
        if not accepted:
            return
        record = dialog.committed_record()
        if record is None:
            record = self._commit_transcript_payload(
                dialog.transcript_text(),
                dialog.meeting_title(),
                dialog.detect_structure(),
                dialog.meeting_started_ms(),
                dialog.parsed(),
            )
        if record is None:
            h._unsaved_transcript_draft = (
                dialog.transcript_text(),
                dialog.meeting_title(),
                dialog.detect_structure(),
                dialog.meeting_started_ms(),
            )
            h._set_status(getattr(h._ctrl, "transcript_import_notice", "") or "没有可导入的文字", tone="error")
            return
        h._unsaved_transcript_draft = None
        h.show_meeting(record.id)
        h._set_status(self.imported_status_message(record.id, "文字稿"), tone="ok")


__all__ = [
    "MeetingImportController",
    "_ORIG_GET_OPEN_FILE_NAME",
    "_format_import_remain",
]

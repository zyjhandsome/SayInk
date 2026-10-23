"""The 「会议」 page: meeting list + detail, start card, and the live view.

Three states (docs/meeting-notes-design.md §4.3):

* **browse** — list of past meetings on the left, the selected meeting's
  minutes / transcript on the right;
* **start card** — title / participants / agenda / source / model before a
  meeting starts (skipped when ``meeting.show_start_card`` is off);
* **live** — the running transcript, a mark button and a notes box while a
  meeting is recording.

The page never touches audio or the recognizer: it asks the
:class:`~sayink.app.App` through ``start_requested`` / ``end_requested`` /
``resume_requested`` and reads everything else from the
:class:`~sayink.meeting_session.MeetingController`.
"""

from __future__ import annotations

import bisect
import html
import json
import logging
import re
import time
from pathlib import Path

from PyQt6.QtCore import QEvent, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices, QFont, QKeySequence, QTextBlockFormat, QTextCharFormat, QTextCursor, QTextFormat
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from sayink.aux_models import (
    AuxModelDownloads,
    aux_models_size_mb,
    describe_aux_models,
    missing_aux_models,
)
from sayink.meeting_session import (
    MeetingController,
    MeetingOptions,
    MeetingState,
    minutes_settings_complete,
    resolve_capabilities,
    safe_filename,
)
from sayink.meeting_store import STATUS_ENDED, STATUS_INTERRUPTED, STATUS_LIVE, MeetingRecord, QueuedGeneration, now_ms
from sayink.audio_devices import input_source_label
from sayink.recognition_settings import ADVANCED_RECOGNITION_PATH, audio_source
from sayink.minutes_prompts import describe_template, format_timestamp, get_template, list_templates, transcript_lines
from sayink.speech_recognizer import DEFAULT_MODEL_ID, MODEL_REGISTRY, is_model_downloaded
from sayink.ui.clipboard import copy_text
from sayink.ui import design_tokens as tok
from sayink.ui.flow_layout import PairedToolBar
from sayink.ui.reading import fit_reading_width
from sayink.ui.history_window import _ElidedLabel, _SessionList, session_list_item_css

log = logging.getLogger("SayInk")

PAGE_BROWSE = 0
PAGE_START = 1
PAGE_LIVE = 2

_SOURCE_LABELS = (("mixed", "混合音频（麦克风 + 电脑声音）"), ("microphone", "麦克风"), ("system", "电脑声音"))
_LANGUAGE_LABELS = (("auto", "自动（中文优先）"), ("zh", "中文"), ("en", "English"), ("ja", "日本語"))
_STATUS_TEXT = {STATUS_LIVE: "进行中", STATUS_INTERRUPTED: "已中断", STATUS_ENDED: "已结束"}


def _duration_label(ms: int) -> str:
    seconds = max(0, ms // 1000)
    if seconds >= 3600:
        return f"{seconds // 3600} 小时 {seconds % 3600 // 60} 分"
    if seconds >= 60:
        return f"{seconds // 60} 分 {seconds % 60} 秒"
    return f"{seconds} 秒"


def _position_label(ms: int) -> str:
    stamp = format_timestamp(ms)
    return stamp[3:] if stamp.startswith("00:") else stamp


def _format_dt(ms: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ms / 1000))


def _meeting_row_meta(record: MeetingRecord, minutes: str = "") -> str:
    status = _STATUS_TEXT.get(record.status, "")
    duration = _duration_label(record.duration_ms) if record.ended_at else ""
    stamp = time.localtime(record.started_at / 1000)
    today = time.localtime()
    # strftime formats stay ASCII: Windows encodes them with the ANSI code page.
    if stamp[:3] == today[:3]:
        when = "今天 " + time.strftime("%H:%M", stamp)
    elif stamp.tm_year == today.tm_year:
        when = time.strftime("%m-%d %H:%M", stamp)
    else:
        when = time.strftime("%Y-%m-%d %H:%M", stamp)
    bits = [status, when] if status else [when]
    if duration:
        bits.append(duration)
    if minutes:
        bits.append(minutes)
    return " · ".join(bits)


def _meeting_row_text(record: MeetingRecord, minutes: str = "") -> str:
    return f"{record.display_title}\n{_meeting_row_meta(record, minutes)}"


_HEADER_META_PREFIXES = ("- 时间：", "- 时长：")
_CITATION = re.compile(r"\[(\d{1,2}):(\d{2}):(\d{2})\](?!\()")


def speaker_color(speaker_id: int) -> str:
    from sayink.ui import design_tokens as tok

    if speaker_id <= 0:
        return tok.TEXT_SEC
    return tok.SPEAKER_COLORS[(speaker_id - 1) % len(tok.SPEAKER_COLORS)]


def link_citations(markdown: str) -> str:
    """Turn ``[00:01:23]`` citations into ``ts:`` links to the transcript."""

    def repl(match: re.Match) -> str:
        h, m, s = (int(g) for g in match.groups())
        return f"[\\[{match.group(1)}:{match.group(2)}:{match.group(3)}\\]](ts:{h * 3600 + m * 60 + s})"

    return _CITATION.sub(repl, markdown)


def notes_for_display(markdown: str, title: str) -> str:
    """Drop what the page header already shows: an H1 equal to the meeting
    title and the 时间 / 时长 lines under it. Export keeps the full text."""
    lines = markdown.splitlines()
    out: list[str] = []
    in_header = True
    for line in lines:
        stripped = line.strip()
        if in_header and stripped.startswith("## "):
            in_header = False
        if in_header:
            if stripped.startswith("# ") and stripped[2:].strip() == title.strip():
                continue
            if stripped.startswith(_HEADER_META_PREFIXES):
                continue
            if not stripped and (not out or not out[-1].strip()):
                continue
        out.append(line)
    rendered = "\n".join(out).strip()
    empty = []

    def compact(match):
        empty.append(match.group(1))
        return ""

    rendered = re.sub(r"(?m)^## (决策|待办)\n(?:[ \t]*\n)*本场未识别到[ \t]*\n*(?=## |\Z)", compact, rendered)
    if empty:
        rendered = rendered.rstrip() + "\n\n" + " / ".join(empty) + "：暂未提取到明确内容，可对照文字稿核对。"
    return rendered.strip() + "\n" if rendered.strip() else ""


class _ReadingView(QTextBrowser):
    """QTextBrowser tuned for long CJK text: looser leading, a heading scale
    below the page title, and text that wraps to the current detail width."""

    copy_document_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._reading_key = None
        self._reading_anchor = 0
        self._reading_timer = QTimer(self)
        self._reading_timer.setSingleShot(True)
        self._reading_timer.timeout.connect(self._restore_reading_position)

    def restore_reading_position(self, value: int) -> None:
        self._reading_anchor = value
        self._restore_reading_position()
        self._reading_timer.start(0)

    def _restore_reading_position(self) -> None:
        self.verticalScrollBar().setValue(self._reading_anchor)

    def _set_reading(self, text: str, *, markdown: bool, plain: bool = False) -> None:
        from sayink.ui import design_tokens as tok

        key = (markdown, plain, text, tok.ACCENT_TEXT, tok.TEXT_SEC)
        if self._reading_key == key:
            return
        value = self.verticalScrollBar().value()
        self._reading_key = key
        if plain:
            self.setPlainText(text)
        elif markdown:
            self.setMarkdown(text)
        else:
            self.setHtml(text)
        self._polish_blocks()
        self._fit_text_width()
        self.restore_reading_position(value)

    def set_reading_markdown(self, markdown: str) -> None:
        self._set_reading(markdown, markdown=True)

    def set_reading_html(self, text: str) -> None:
        self._set_reading(text, markdown=False)

    def set_reading_plain(self, text: str) -> None:
        self._set_reading(text, markdown=False, plain=True)

    def clear(self) -> None:
        self._reading_key = None
        self._reading_timer.stop()
        super().clear()

    def keyPressEvent(self, event) -> None:
        if (event.matches(QKeySequence.StandardKey.Copy) and not self.textCursor().hasSelection()
                and self.receivers(self.copy_document_requested)):
            self.copy_document_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def _polish_blocks(self) -> None:
        from sayink.ui import design_tokens as tok

        heading_steps = {1: 0, 2: 1}
        block = self.document().begin()
        while block.isValid():
            cursor = QTextCursor(block)
            fmt = block.blockFormat()
            level = fmt.headingLevel()
            fmt.setLineHeight(float(tok.READING_LINE_HEIGHT), QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
            if level:
                fmt.setTopMargin(14 if level <= 2 else 10)
                fmt.setBottomMargin(2)
            cursor.setBlockFormat(fmt)
            if level:
                char = QTextCharFormat()
                # Markdown headings only honor the relative size step (~1.2x per
                # step); explicit point/pixel sizes on these blocks are ignored.
                char.setProperty(QTextFormat.Property.FontSizeAdjustment, heading_steps.get(level, 0))
                if level == 1:
                    # An LLM title that differs from the meeting name: a subtitle, not a second heading.
                    char.setFontWeight(QFont.Weight.Normal)
                    char.setForeground(QColor(tok.TEXT_SEC))
                cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
                cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
                cursor.mergeCharFormat(char)
            self._style_links(block, tok.ACCENT_TEXT)
            block = block.next()

    @staticmethod
    def _style_links(block, color: str) -> None:
        ranges = []
        it = block.begin()
        while not it.atEnd():
            fragment = it.fragment()
            fmt = fragment.charFormat()
            if fmt.isAnchor() and fmt.anchorHref().startswith("ts:"):
                ranges.append((fragment.position(), fragment.position() + fragment.length()))
            it += 1
        link = QTextCharFormat()
        link.setForeground(QColor(color))
        link.setFontUnderline(False)
        for start, end in ranges:
            cursor = QTextCursor(block.document())
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            cursor.mergeCharFormat(link)

    def _fit_text_width(self) -> None:
        fit_reading_width(self, fill_available=True)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_text_width()


class _TranscriptView(_ReadingView):
    """Reveal paragraph actions without replacing the document or moving text."""

    _CONTEXT_LINKS = ("edit-segment:", "live-edit:", "details-segment:", "live-details:")
    _SEGMENT_LINKS = (
        "live-edit:", "edit-segment:", "live-details:", "details-segment:",
        "live-segment:", "segment:", "edited-segment:",
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self._action_block = None
        self._keyboard_actions = False
        self.setMouseTracking(True)
        self.cursorPositionChanged.connect(self._reveal_cursor_actions)

    def reset_actions(self) -> None:
        self._activate_actions()
        if self.hasFocus() and self._keyboard_actions:
            self._activate_actions(self.textCursor().block())

    def setHtml(self, text: str) -> None:
        # Drop the old block before the document is replaced, then hide every
        # action. A full rebuild used to return early (both blocks were None)
        # and left the first character of「编辑」in the body color.
        saved = self._capture_body_selection()
        self._action_block = None
        super().setHtml(text)
        self._hide_all_actions()
        self.reset_actions()
        self._restore_body_selection(saved)

    def _seq_in_block(self, block) -> int | None:
        it = block.begin()
        while not it.atEnd():
            href = it.fragment().charFormat().anchorHref()
            for prefix in self._SEGMENT_LINKS:
                if href.startswith(prefix) and href[len(prefix):].isdigit():
                    return int(href[len(prefix):])
            it += 1
        return None

    def _plain_offset(self, block, position: int) -> int:
        """UTF-16 units of non-link text before ``position``.

        Speaker names and hover actions are links, so a longer name does not
        move this offset. The timestamp and the spoken text are not links.
        """
        count = 0
        it = block.begin()
        while not it.atEnd():
            fragment = it.fragment()
            if not fragment.isValid() or fragment.charFormat().anchorHref():
                it += 1
                continue
            start = fragment.position()
            end = start + fragment.length()
            if position <= start:
                break
            count += min(position, end) - start
            if position <= end:
                break
            it += 1
        return count

    def _position_at_plain_offset(self, block, offset: int) -> int:
        last = block.position()
        it = block.begin()
        while not it.atEnd():
            fragment = it.fragment()
            if not fragment.isValid() or fragment.charFormat().anchorHref():
                it += 1
                continue
            last = fragment.position() + fragment.length()
            if offset <= fragment.length():
                return fragment.position() + max(0, offset)
            offset -= fragment.length()
            it += 1
        return max(block.position(), last - 1 if last else block.position())

    def _body_point(self, position: int) -> tuple[int, int] | None:
        last = max(0, self.document().characterCount() - 1)
        cursor = QTextCursor(self.document())
        cursor.setPosition(min(max(0, position), last))
        seq = self._seq_in_block(cursor.block())
        if seq is None:
            return None
        return seq, self._plain_offset(cursor.block(), cursor.position())

    def _body_position(self, seq: int, offset: int) -> int | None:
        block = self.document().begin()
        while block.isValid():
            if self._seq_in_block(block) == seq:
                return self._position_at_plain_offset(block, offset)
            block = block.next()
        return None

    def _capture_body_selection(self):
        cursor = self.textCursor()
        if not cursor.hasSelection() and cursor.position() == 0:
            return None
        start = self._body_point(min(cursor.selectionStart(), cursor.selectionEnd()))
        end = self._body_point(max(cursor.selectionStart(), cursor.selectionEnd()))
        if start is None and end is None:
            return None
        return start, end

    def _restore_body_selection(self, saved) -> None:
        if not saved:
            return
        start, end = saved
        start_pos = self._body_position(*start) if start else None
        end_pos = self._body_position(*end) if end else None
        if start_pos is None and end_pos is None:
            return
        if start_pos is None:
            start_pos = end_pos
        if end_pos is None:
            end_pos = start_pos
        cursor = self.textCursor()
        cursor.setPosition(start_pos)
        cursor.setPosition(end_pos, QTextCursor.MoveMode.KeepAnchor)
        self.setTextCursor(cursor)

    def place_cursor_at_segment(self, seq: int) -> None:
        position = self._body_position(seq, 0)
        if position is None:
            return
        cursor = self.textCursor()
        cursor.setPosition(position)
        self.setTextCursor(cursor)
        self.ensureCursorVisible()

    def _hide_all_actions(self) -> None:
        block = self.document().begin()
        while block.isValid():
            self._paint_actions(block, False)
            block = block.next()
        self._action_block = None

    def position_before_action(self, position: int) -> int:
        """Pull a caret out of a hover action so that word cannot stay on screen."""
        last = max(0, self.document().characterCount() - 1)
        cursor = QTextCursor(self.document())
        cursor.setPosition(min(max(0, position), last))
        while cursor.position() > 0 and cursor.charFormat().anchorHref().startswith(self._CONTEXT_LINKS):
            cursor.setPosition(cursor.position() - 1)
        return cursor.position()

    def _paint_actions(self, block, visible: bool) -> None:
        if block is None or not block.isValid():
            return
        ranges = []
        it = block.begin()
        while not it.atEnd():
            fragment = it.fragment()
            if fragment.isValid() and fragment.charFormat().anchorHref().startswith(self._CONTEXT_LINKS):
                ranges.append((fragment.position(), fragment.length()))
            it += 1
        for start, length in ranges:
            cursor = QTextCursor(self.document())
            cursor.setPosition(start)
            cursor.setPosition(start + length, QTextCursor.MoveMode.KeepAnchor)
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(tok.ACCENT_TEXT) if visible else QColor(Qt.GlobalColor.transparent))
            cursor.mergeCharFormat(fmt)

    def _activate_actions(self, block=None) -> None:
        if self._action_block == block:
            return
        previous, self._action_block = self._action_block, block
        self._paint_actions(previous, False)
        self._paint_actions(block, True)

    def _reveal_cursor_actions(self) -> None:
        if self.hasFocus() and self._keyboard_actions:
            self._activate_actions(self.textCursor().block())

    def mouseMoveEvent(self, event) -> None:
        self._keyboard_actions = False
        self._activate_actions(self.cursorForPosition(event.pos()).block())
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        if not (self.hasFocus() and self._keyboard_actions):
            self._activate_actions()
        super().leaveEvent(event)

    def focusInEvent(self, event) -> None:
        # A dialog closing hands focus back with ActiveWindow/Popup, not Tab.
        # Treating that as keyboard navigation painted「编辑」onto the paragraph
        # the caret had been restored into.
        self._keyboard_actions = event.reason() in (
            Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason,
        )
        super().focusInEvent(event)
        self._reveal_cursor_actions()

    def focusOutEvent(self, event) -> None:
        self._activate_actions()
        super().focusOutEvent(event)

    def focusNextPrevChild(self, next: bool) -> bool:
        self._keyboard_actions = True
        handled = super().focusNextPrevChild(next)
        self._reveal_cursor_actions()
        return handled

    def keyPressEvent(self, event) -> None:
        self._keyboard_actions = True
        super().keyPressEvent(event)
        self._reveal_cursor_actions()

    def createMimeDataFromSelection(self):
        """Copy the spoken text. Hover actions stay clickable but out of the clipboard."""
        from PyQt6.QtCore import QMimeData

        cursor = self.textCursor()
        if not cursor.hasSelection():
            return super().createMimeDataFromSelection()
        start = min(cursor.selectionStart(), cursor.selectionEnd())
        end = max(cursor.selectionStart(), cursor.selectionEnd())
        skip = self._CONTEXT_LINKS + ("edited-segment:", "segment:", "live-segment:")
        lines: list[str] = []
        block = self.document().begin()
        while block.isValid() and block.position() < end:
            if block.position() + block.length() <= start:
                block = block.next()
                continue
            pieces: list[str] = []
            it = block.begin()
            while not it.atEnd():
                fragment = it.fragment()
                if fragment.isValid():
                    frag_start = fragment.position()
                    left = max(start, frag_start)
                    right = min(end, frag_start + fragment.length())
                    if left < right:
                        href = fragment.charFormat().anchorHref()
                        if not href.startswith(skip):
                            # Qt positions count UTF-16 units; slice the fragment
                            # with a cursor so an emoji does not shift the text.
                            piece = QTextCursor(self.document())
                            piece.setPosition(left)
                            piece.setPosition(right, QTextCursor.MoveMode.KeepAnchor)
                            pieces.append(piece.selectedText().replace("\u2029", "\n").replace("\u2028", "\n"))
                it += 1
            lines.append("".join(pieces))
            block = block.next()
        mime = QMimeData()
        mime.setText("\n".join(lines).strip("\n"))
        return mime


def _speaker_source(speaker_id: int, names: dict[int, str], auto_names: set[int]) -> str:
    if speaker_id <= 0:
        return "说话人待确认"
    if speaker_id in auto_names:
        return "姓名来自原文自报，仅作本场称呼"
    if speaker_id in names:
        return "此姓名由你设置"
    return "按声音自动区分，尚未命名"


class _SegmentSpeakerDialog(QDialog):
    def __init__(self, segment, choices: list[tuple[int | None, str]], parent=None, *, save_speaker=None, live=False):
        super().__init__(parent)
        self._save_speaker = save_speaker
        from sayink.ui import design_tokens as tok
        from sayink.ui import settings_styles as ss

        self.setWindowTitle("修改本段说话人")
        self.setObjectName("meetingSpeakerCorrection")
        self.setFixedWidth(420)
        self.setStyleSheet(ss.WINDOW_CSS + f"""
            QDialog#meetingSpeakerCorrection {{ background: {tok.SURFACE}; color: {tok.TEXT}; }}
            QLabel {{ background: transparent; color: {tok.TEXT}; font-size: {tok.TYPE_BODY_SM}px; }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(12)
        preview = QLabel((segment.corrected_text or segment.polished_text or segment.raw_text)[:220])
        preview.setTextFormat(Qt.TextFormat.PlainText)
        preview.setWordWrap(True)
        lay.addWidget(preview)
        self.speaker_combo = QComboBox()
        self.speaker_combo.setAccessibleName("这一段的发言人")
        self.speaker_combo.setPlaceholderText("请选择这一段的发言人")
        for speaker_id, label in choices:
            self.speaker_combo.addItem(label, speaker_id)
        self.speaker_combo.setCurrentIndex(self.speaker_combo.findData(segment.speaker_id))
        lay.addWidget(self.speaker_combo)
        hint = QLabel(
            "更正这一段，人工更正会保留。清晰的声音会帮助判断后续发言；其他已有段落保持原归属。" if live else
            "仅更改这一段的归属。现有纪要中的发言人需要核对，或重新生成纪要。"
        )
        hint.setWordWrap(True)
        lay.addWidget(hint)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.hide()
        lay.addWidget(self.error)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setStyleSheet(ss.BTN_GHOST_SM)
        cancel.clicked.connect(self.reject)
        self.save_btn = QPushButton("应用更正")
        self.save_btn.setStyleSheet(ss.BTN_PRIMARY)
        self.save_btn.clicked.connect(self._save)
        buttons.addWidget(cancel)
        buttons.addWidget(self.save_btn)
        lay.addLayout(buttons)

    @property
    def speaker_id(self) -> int | None:
        value = self.speaker_combo.currentData()
        return int(value) if value is not None else None

    def _save(self) -> None:
        if self.speaker_combo.currentIndex() < 0:
            self.error.setText("请先选择这一段的发言人。")
            self.error.show()
            return
        if self._save_speaker is not None and not self._save_speaker(self.speaker_id):
            self.error.setText("更正尚未保存，选择已保留。请重试；会议已结束或正在整理时，请关闭后重新选择。")
            self.error.show()
            return
        self.accept()


class _SegmentTextDialog(QDialog):
    """Keep a segment draft open until its write has been verified."""

    def __init__(self, segment, *, position: str, speaker: str, save_text, parent=None,
                 details: str = "", read_only: bool = False):
        super().__init__(parent)
        from sayink.ui import settings_styles as ss

        self._save_text = save_text
        self._original = (segment.polished_text or segment.raw_text).strip()
        self._initial = (segment.corrected_text or self._original).strip()
        self._read_only = read_only
        self.setWindowTitle("修改详情" if details else "编辑本段文字稿")
        self.setObjectName("meetingTextCorrection")
        self.setStyleSheet(ss.WINDOW_CSS + f"""
            QDialog#meetingTextCorrection {{ background: {tok.SURFACE}; color: {tok.TEXT}; }}
            QLabel {{ background: transparent; color: {tok.TEXT}; font-size: {tok.TYPE_BODY_SM}px; }}
            QPlainTextEdit {{ background: {tok.SURFACE_PEARL}; color: {tok.TEXT};
                border: 1px solid {tok.HAIRLINE}; border-radius: {tok.RADIUS_MD}px;
                padding: 8px; font-size: {tok.READING_FONT_PX}px;
                selection-background-color: {tok.ACCENT_SOFT}; selection-color: {tok.TEXT}; }}
        """)
        self.setMinimumSize(320, 300)
        available = self.screen().availableGeometry()
        self.resize(min(600, available.width() - 48), min(500, available.height() - 64))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(12)
        context = QLabel(f"[{position}]  {speaker}")
        context.setTextFormat(Qt.TextFormat.PlainText)
        context.setWordWrap(True)
        lay.addWidget(context)
        if details:
            self.details_label = QLabel(details)
            self.details_label.setTextFormat(Qt.TextFormat.PlainText)
            self.details_label.setWordWrap(True)
            lay.addWidget(self.details_label)
        hint = QLabel("当前只能查看。生成或收尾结束后可以再改这一段。" if read_only else
                      "仅修改这一段。保存后用于纪要、原文精整、复制和导出，时间与发言人保持不变。")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        self.editor = QPlainTextEdit()
        self.editor.setAccessibleName("本段文字稿内容")
        self.editor.setPlainText(self._initial)
        self.editor.setReadOnly(read_only)
        lay.addWidget(self.editor, 1)
        self.original_view = QPlainTextEdit()
        self.original_view.setAccessibleName("修改前的原文")
        self.original_view.setReadOnly(True)
        self.original_view.setPlainText(self._original)
        self.original_view.setMaximumHeight(110)
        self.original_view.hide()
        lay.addWidget(self.original_view)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setAccessibleName("文字稿保存状态")
        self.error.hide()
        lay.addWidget(self.error)
        buttons = QHBoxLayout()
        self.original_btn = QPushButton("查看原文")
        self.original_btn.setCheckable(True)
        self.original_btn.setStyleSheet(ss.BTN_GHOST_SM)
        self.original_btn.toggled.connect(self.original_view.setVisible)
        self.original_btn.toggled.connect(lambda shown: self.original_btn.setText("收起原文" if shown else "查看原文"))
        buttons.addWidget(self.original_btn)
        self.restore_btn = QPushButton("恢复原文")
        self.restore_btn.setStyleSheet(ss.BTN_GHOST_SM)
        self.restore_btn.setToolTip("将本段恢复为修改前的文字，点击「保存修改」后生效")
        self.restore_btn.clicked.connect(self._restore_original)
        buttons.addWidget(self.restore_btn)
        buttons.addStretch(1)
        lay.insertLayout(lay.indexOf(self.original_view), buttons)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("取消")
        if read_only:
            cancel.setText("关闭")
        cancel.setStyleSheet(ss.BTN_GHOST_SM)
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.save_btn = QPushButton("保存修改")
        self.save_btn.setStyleSheet(ss.BTN_PRIMARY)
        self.save_btn.clicked.connect(self._save)
        self.save_btn.setDefault(True)
        buttons.addWidget(self.save_btn)
        lay.addLayout(buttons)
        self.editor.textChanged.connect(self._sync_actions)
        self._sync_actions()
        self.restore_btn.setVisible(not read_only)
        self.save_btn.setVisible(not read_only)
        if details and segment.corrected_text:
            self.original_btn.setChecked(True)
        self.editor.setFocus()
        self.editor.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:
        if obj is self.editor and event.type() == QEvent.Type.KeyPress:
            if (event.modifiers() == Qt.KeyboardModifier.ControlModifier
                    and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)):
                self._save()
                return True
        return super().eventFilter(obj, event)

    def _restore_original(self) -> None:
        """One undoable edit, so Ctrl+Z brings back what was just replaced."""
        cursor = self.editor.textCursor()
        cursor.beginEditBlock()
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.insertText(self._original)
        cursor.endEditBlock()
        self.editor.setTextCursor(cursor)

    @property
    def corrected_text(self) -> str | None:
        text = self.editor.toPlainText().strip()
        return None if text == self._original else text

    def _sync_actions(self) -> None:
        text = self.editor.toPlainText().strip()
        self.save_btn.setEnabled(not self._read_only and bool(text) and text != self._initial)
        self.restore_btn.setEnabled(text != self._original)

    def _save(self) -> None:
        if not self.save_btn.isEnabled():
            return
        if not self._save_text(self.corrected_text):
            self.error.setText("尚未保存。修改内容仍保留在这里，请重试或复制备份。")
            self.error.show()
            return
        self.accept()


class _CurrentPageStack(QStackedWidget):
    """Height follows the visible page, pinned so a shorter page cannot drift.

    A plain stack keeps the tallest page. Qt then centers the shorter page in
    that cell, which moves its buttons away from the tabs. Pinning the height
    keeps every tab's action row on the same baseline.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._syncing = False
        self.currentChanged.connect(lambda _index: self.sync_height())

    def hasHeightForWidth(self) -> bool:
        current = self.currentWidget()
        return bool(current is not None and current.hasHeightForWidth())

    def heightForWidth(self, width: int) -> int:
        current = self.currentWidget()
        if current is not None and current.hasHeightForWidth():
            return current.heightForWidth(width)
        hint = current.sizeHint() if current is not None else super().sizeHint()
        return hint.height()

    def sizeHint(self):
        current = self.currentWidget()
        return current.sizeHint() if current is not None else super().sizeHint()

    def minimumSizeHint(self):
        current = self.currentWidget()
        return current.minimumSizeHint() if current is not None else super().minimumSizeHint()

    def sync_height(self) -> None:
        if self._syncing:
            return
        current = self.currentWidget()
        width = self.width()
        if current is None or width <= 0:
            return
        height = self.heightForWidth(width)
        if height <= 0 or (self.minimumHeight() == height and self.maximumHeight() == height):
            return
        self._syncing = True
        try:
            self.setFixedHeight(height)
        finally:
            self._syncing = False

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if event.oldSize().width() != event.size().width():
            self.sync_height()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.sync_height()

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.LayoutRequest:
            self.sync_height()
        return super().event(event)


class MeetingPage(QWidget):
    start_requested = pyqtSignal(object)   # MeetingOptions
    end_requested = pyqtSignal()
    pause_requested = pyqtSignal()
    resume_requested = pyqtSignal()
    configure_requested = pyqtSignal()     # minutes need the AI service connection
    aux_models_changed = pyqtSignal()      # a helper model was downloaded here

    def __init__(self, config, controller: MeetingController, parent=None) -> None:
        super().__init__(parent)
        self._config = config
        self._ctrl = controller
        self._store = controller.store
        self._aux_dl = AuxModelDownloads(str(config.get("stt.download_source", "auto") or "auto"), self)
        self._aux_dl.progress.connect(self._on_aux_progress)
        self._aux_dl.finished.connect(self._on_aux_finished)
        self._records: dict[str, MeetingRecord] = {}
        self._detail_id = ""
        self._search_query = ""
        self._editing_notes = False
        self._transcript_offsets: list[int] = []
        self._transcript_highlight = -1
        self._transcript_sequences: list[int] = []
        self._corrected_sequences: set[int] = set()
        self._speaker_locked_sequences: set[int] = set()
        self._transcript_editable = False
        self._pending_templates: dict[str, str] = {}
        self._citation_return: tuple[str, int] | None = None
        self._correcting_speakers = False
        self._live_correcting_speakers = False
        self._live_transcript_sequences: list[int] = []
        self._live_locked_sequences: set[int] = set()
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(400)
        self._refresh_timer.timeout.connect(self._refresh_now)
        self._pending_refresh_id = ""
        self._listed_generating = ""
        self._live_html = ""
        self._marks_html = None
        self._live_meeting_id = ""
        self._list_limit = 100
        self._pending_title: tuple[str, str] | None = None
        self._record_notes_meeting_id = ""
        self._record_marks_html = None
        self._setup_ui()
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(self._status_label.hide)
        self._live_feedback_timer = QTimer(self)
        self._live_feedback_timer.setSingleShot(True)
        self._live_feedback_timer.timeout.connect(self._live_speaker_feedback.hide)
        self._ctrl.state_changed.connect(self._on_state_changed)
        self._ctrl.meeting_changed.connect(self._on_meeting_changed)
        self._ctrl.clock_tick.connect(self._live_clock.setText)
        self._ctrl.generation_progress.connect(self._on_generation_progress)
        self._ctrl.generation_preview.connect(self._on_generation_preview)
        self._ctrl.generation_finished.connect(self._on_generation_finished)
        self._ctrl.generation_failed.connect(self._on_generation_failed)
        self._ctrl.generation_cancelled.connect(self._on_generation_cancelled)
        self._ctrl.generation_queued.connect(self._on_generation_queued)
        self.reapply_theme()
        self.refresh()
        if self._ctrl.is_active:
            self._enter_live()

    # ── construction ──────────────────────────────────────────────

    def _setup_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(*tok.WORKSPACE_MARGINS)
        outer.setSpacing(14)

        top = QHBoxLayout()
        top.setSpacing(12)
        from sayink.ui.settings_components import page_title_label

        self._title_label = page_title_label("会议")
        top.addWidget(self._title_label, 1, Qt.AlignmentFlag.AlignVCenter)
        self._start_btn = QPushButton("开始会议")
        self._start_btn.setAccessibleName("开始会议")
        self._start_btn.setToolTip("开始一场新的会议记录")
        self._start_btn.clicked.connect(self.show_start_card)
        top.addWidget(self._start_btn, 0)
        outer.addLayout(top)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_browse())
        self._stack.addWidget(self._build_start_card())
        self._stack.addWidget(self._build_live())
        outer.addWidget(self._stack, 1)
        for button in self.findChildren(QPushButton):
            button.setCursor(Qt.CursorShape.PointingHandCursor)

    def _build_browse(self) -> QWidget:
        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.setChildrenCollapsible(False)
        self._splitter.setHandleWidth(5)

        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(6)
        left.setMinimumWidth(tok.WORKSPACE_LIST_MIN_WIDTH)
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("搜索标题、参会人或文字稿")
        self._search_edit.setAccessibleName("搜索会议")
        self._search_edit.setClearButtonEnabled(True)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(200)
        self._search_edit.textChanged.connect(lambda: self._search_timer.start())
        self._search_timer.timeout.connect(self._perform_search)
        left_lay.addWidget(self._search_edit)
        left_lay.addSpacing(4)
        self._list_summary = QLabel()
        self._list_summary.setContentsMargins(4, 0, 0, 0)
        left_lay.addWidget(self._list_summary)
        self._list = _SessionList()
        self._list.setObjectName("meetingList")
        self._list.setAccessibleName("会议列表")
        self._list.setFrameShape(QFrame.Shape.NoFrame)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self._list.setWordWrap(True)
        self._list.itemSelectionChanged.connect(self._on_selection_changed)
        left_lay.addWidget(self._list, 1)
        self._load_more_btn = QPushButton("加载更早的会议")
        self._load_more_btn.setAccessibleName("加载更早的会议")
        self._load_more_btn.clicked.connect(self._load_more)
        self._load_more_btn.hide()
        left_lay.addWidget(self._load_more_btn, 0, Qt.AlignmentFlag.AlignHCenter)
        self._empty_label = QLabel("还没有会议记录。\n点击右上角「开始会议」，或从托盘菜单开始。")
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setWordWrap(True)
        left_lay.addWidget(self._empty_label, 1)
        self._clear_search_btn = QPushButton("清除搜索")
        self._clear_search_btn.setMaximumWidth(160)
        self._clear_search_btn.clicked.connect(self._search_edit.clear)
        self._clear_search_btn.hide()
        left_lay.addWidget(self._clear_search_btn, 0, Qt.AlignmentFlag.AlignHCenter)
        self._splitter.addWidget(left)

        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(20, 0, 0, 0)
        right_lay.setSpacing(8)

        # Identity first (title + meta), then the content tabs; each tab's tools
        # sit in the tab row so they read as acting on that tab only.
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        self._detail_title = QLineEdit()
        self._detail_title.setPlaceholderText("会议标题")
        self._detail_title.setAccessibleName("会议标题")
        self._detail_title.setToolTip("点击修改会议标题")
        self._detail_title.editingFinished.connect(self._save_title)
        title_row.addWidget(self._detail_title, 1)
        self._copy_btn = QPushButton("复制")
        self._copy_btn.setMinimumWidth(108)
        self._copy_btn.setToolTip("复制纪要 Markdown")
        self._copy_btn.clicked.connect(self._copy_current)
        self._export_btn = QPushButton("导出")
        self._export_btn.setToolTip("选择要导出的会议内容")
        self._export_menu = QMenu(self._export_btn)
        self._export_actions = {}
        for scope, label in (("notes", "仅导出纪要"), ("transcript", "仅导出文字稿"), ("all", "导出完整会议记录")):
            action = self._export_menu.addAction(label)
            action.triggered.connect(lambda _checked=False, kind=scope: self._export(kind))
            self._export_actions[scope] = action
        self._export_btn.setMenu(self._export_menu)
        self._more_btn = QPushButton("更多")
        self._more_btn.setAccessibleName("更多操作")
        self._more_btn.setToolTip("更多操作")
        self._more_menu = QMenu(self._more_btn)
        self._restore_action = self._more_menu.addAction("恢复上一版纪要")
        self._restore_action.triggered.connect(self._restore_previous)
        self._more_menu.addSeparator()
        self._delete_action = self._more_menu.addAction("删除会议")
        self._delete_action.triggered.connect(self._delete)
        self._more_btn.setMenu(self._more_menu)
        title_row.addWidget(self._more_btn)
        right_lay.addLayout(title_row)

        self._detail_meta = QLabel("")
        self._detail_meta.setObjectName("meetingDetailMeta")
        self._detail_meta.setWordWrap(True)
        self._detail_meta.setContentsMargins(4, 0, 0, 0)
        self._notes_state = QLabel()
        self._notes_state.setWordWrap(True)
        self._notes_state.setAccessibleName("纪要来源与保存状态")
        self._state_row = QWidget()
        self._state_row.setObjectName("meetingState")
        state_lay = QHBoxLayout(self._state_row)
        state_lay.setContentsMargins(0, 0, 0, 0)
        state_lay.setSpacing(8)
        state_lay.addWidget(self._notes_state, 1)
        self._cancel_btn = QPushButton("取消")
        self._cancel_btn.clicked.connect(self._cancel_job)
        self._cancel_btn.hide()
        state_lay.addWidget(self._cancel_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        right_lay.addWidget(self._detail_meta)
        right_lay.addWidget(self._state_row)
        self._materials = QWidget()
        materials_lay = QVBoxLayout(self._materials)
        materials_lay.setContentsMargins(0, 0, 0, 0)
        materials_lay.setSpacing(6)
        self._materials_toggle = QPushButton("会议资料")
        self._materials_toggle.setCheckable(True)
        self._materials_toggle.setChecked(True)
        self._materials_toggle.setAccessibleName("会议资料")
        self._materials_toggle.setToolTip("议程、随手笔记和标记。笔记在离开时自动保存。")
        materials_lay.addWidget(self._materials_toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self._materials_body = QWidget()
        body_lay = QVBoxLayout(self._materials_body)
        body_lay.setContentsMargins(0, 0, 0, 0)
        body_lay.setSpacing(6)
        self._materials_agenda = QLabel()
        self._materials_agenda.setWordWrap(True)
        self._materials_agenda.setTextFormat(Qt.TextFormat.PlainText)
        body_lay.addWidget(self._materials_agenda)
        self._materials_notes_label = QLabel("我的笔记")
        body_lay.addWidget(self._materials_notes_label)
        self._record_notes = QPlainTextEdit()
        self._record_notes.setAccessibleName("我的笔记")
        self._record_notes.setPlaceholderText("会中记下的内容；离开时自动保存。")
        self._record_notes.setFixedHeight(96)
        self._record_notes_timer = QTimer(self)
        self._record_notes_timer.setSingleShot(True)
        self._record_notes_timer.setInterval(800)
        self._record_notes_timer.timeout.connect(self._save_record_notes)
        self._record_notes.textChanged.connect(lambda: self._record_notes_timer.start())
        body_lay.addWidget(self._record_notes)
        self._materials_marks_label = QLabel("标记")
        body_lay.addWidget(self._materials_marks_label)
        self._record_marks = QTextBrowser()
        self._record_marks.setAccessibleName("标记")
        self._record_marks.setMaximumHeight(120)
        body_lay.addWidget(self._record_marks)
        self._materials_toggle.toggled.connect(self._materials_body.setVisible)
        materials_lay.addWidget(self._materials_body)
        self._materials.hide()
        right_lay.addWidget(self._materials)

        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        self._status_label.hide()
        right_lay.addWidget(self._status_label)

        tab_row = QHBoxLayout()
        tab_row.setSpacing(8)
        tab_row.setContentsMargins(0, 6, 0, 0)
        self._tab_bar = QWidget()
        self._tab_bar.setObjectName("meetingTabs")
        tabs_lay = QHBoxLayout(self._tab_bar)
        tabs_lay.setContentsMargins(3, 2, 3, 2)
        tabs_lay.setSpacing(2)
        self._tab_group = QButtonGroup(self)
        self._tab_group.setExclusive(True)
        self._tab_notes = QPushButton("纪要")
        self._tab_transcript = QPushButton("文字稿")
        for index, button in enumerate((self._tab_notes, self._tab_transcript)):
            button.setCheckable(True)
            button.setObjectName("meetingTab")
            button.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
            self._tab_group.addButton(button, index)
            tabs_lay.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)
        self._tab_notes.setChecked(True)
        self._tab_group.idClicked.connect(self._switch_tab)
        tab_row.addWidget(self._tab_bar, 0, Qt.AlignmentFlag.AlignVCenter)
        tab_row.addStretch(1)
        tab_row.addWidget(self._export_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        right_lay.addLayout(tab_row)

        self._tab_tools = _CurrentPageStack()
        notes_tools = QWidget()
        notes_outer = QVBoxLayout(notes_tools)
        notes_outer.setContentsMargins(0, 0, 0, 0)
        notes_outer.setSpacing(8)
        self._template_label = QLabel("模板")
        self._template_combo = QComboBox()
        self._template_combo.setAccessibleName("会议模板")
        self._template_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        for template in list_templates(self._custom_templates()):
            self._template_combo.addItem(template.name, template.id)
            self._template_combo.setItemData(
                self._template_combo.count() - 1, describe_template(template), Qt.ItemDataRole.ToolTipRole
            )
        self._template_combo.currentIndexChanged.connect(self._on_template_changed)
        self._template_combo.setMinimumWidth(120)
        self._template_combo.setMaximumWidth(240)
        self._template_combo.setToolTip(str(self._template_combo.itemData(0, Qt.ItemDataRole.ToolTipRole) or ""))
        self._template_label.setBuddy(self._template_combo)
        self._manage_templates_btn = QPushButton("管理模板")
        self._manage_templates_btn.setToolTip("新建自定义模板，或复制内置模板后修改")
        self._manage_templates_btn.clicked.connect(self._manage_templates)
        self._generate_btn = QPushButton("生成纪要")
        self._generate_btn.setAccessibleName("生成纪要")
        self._generate_btn.clicked.connect(self._generate)
        self._edit_btn = QPushButton("编辑正文")
        self._edit_btn.setToolTip("直接修改纪要文本")
        self._edit_btn.clicked.connect(self._toggle_edit)
        self._notes_bar = PairedToolBar(spacing=8)
        self._notes_bar.add_left(self._template_label)
        self._notes_bar.add_left(self._template_combo)
        self._notes_bar.add_left(self._manage_templates_btn)
        self._notes_bar.add_right(self._generate_btn)
        self._notes_bar.add_right(self._edit_btn)
        self._notes_bar.add_right(self._copy_btn)
        notes_outer.addWidget(self._notes_bar)
        self._tab_tools.addWidget(notes_tools)
        tools_policy = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        tools_policy.setHeightForWidth(True)
        self._tab_tools.setSizePolicy(tools_policy)

        transcript_tools = QWidget()
        transcript_lay = QVBoxLayout(transcript_tools)
        transcript_lay.setContentsMargins(0, 0, 0, 0)
        transcript_lay.setSpacing(8)
        self._transcript_bar = PairedToolBar(spacing=8)
        self._speaker_btn = QPushButton("命名说话人")
        self._speaker_btn.setAccessibleName("为整组说话人命名")
        self._speaker_btn.setToolTip("为同一组发言设置显示名称；名称来自手动设置，不表示已验证身份")
        self._speaker_menu = QMenu(self._speaker_btn)
        self._speaker_btn.setMenu(self._speaker_menu)
        self._transcript_bar.add_right(self._speaker_btn)
        self._correct_speaker_btn = QPushButton("纠正发言人")
        self._correct_speaker_btn.setCheckable(True)
        self._correct_speaker_btn.toggled.connect(self._toggle_speaker_correction)
        self._transcript_bar.add_right(self._correct_speaker_btn)
        self._return_notes_btn = QPushButton("返回纪要")
        self._return_notes_btn.setAccessibleName("返回纪要引用处")
        self._return_notes_btn.setToolTip("回到刚才阅读的纪要引用位置")
        self._return_notes_btn.clicked.connect(self._return_to_notes)
        self._return_notes_btn.hide()
        self._transcript_bar.add_right(self._return_notes_btn)
        self._transcript_copy_btn = QPushButton("复制文字稿")
        self._transcript_copy_btn.setMinimumWidth(108)
        self._transcript_copy_btn.setAccessibleName("复制文字稿")
        self._transcript_copy_btn.setToolTip("复制完整文字稿（含时间与说话人名称） · Ctrl+C")
        self._transcript_copy_btn.clicked.connect(self._copy_transcript)
        self._transcript_bar.add_right(self._transcript_copy_btn)
        transcript_lay.addWidget(self._transcript_bar)
        self._tab_tools.addWidget(transcript_tools)
        right_lay.addWidget(self._tab_tools)
        self._content_hint = QLabel()
        self._content_hint.setWordWrap(True)
        self._content_hint.hide()
        right_lay.addWidget(self._content_hint)

        self._content_stack = QStackedWidget()
        self._notes_stack = QStackedWidget()
        self._notes_view = _ReadingView()
        self._notes_view.setOpenLinks(False)
        self._notes_view.copy_document_requested.connect(self._copy_current)
        self._notes_view.anchorClicked.connect(self._on_notes_link)
        self._notes_view.setAccessibleName("会议纪要")
        self._notes_view.setPlaceholderText("还没有纪要。选择模板后点击「生成纪要」。")
        self._notes_edit = QPlainTextEdit()
        self._notes_edit.setAccessibleName("编辑会议纪要")
        self._notes_edit.textChanged.connect(self._sync_copy_action)
        self._notes_edit.installEventFilter(self)
        self._notes_stack.addWidget(self._notes_view)
        self._notes_stack.addWidget(self._notes_edit)
        # QTextEdit placeholders are drawn on one line by Qt. Use a real label
        # for an empty document so instructions wrap with the detail pane.
        self._notes_empty = QLabel()
        self._notes_empty.setWordWrap(True)
        self._notes_empty.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._notes_empty.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._notes_stack.addWidget(self._notes_empty)
        self._content_stack.addWidget(self._notes_stack)
        self._transcript_view = _TranscriptView()
        self._transcript_view.setAccessibleName("会议文字稿")
        self._transcript_view.setPlaceholderText("文字稿为空")
        self._transcript_view.setOpenLinks(False)
        self._transcript_view.copy_document_requested.connect(self._copy_current)
        self._transcript_view.anchorClicked.connect(self._on_transcript_link)
        self._content_stack.addWidget(self._transcript_view)
        right_lay.addWidget(self._content_stack, 1)

        self._detail_pane = right
        right.setMinimumWidth(tok.WORKSPACE_DETAIL_MIN_WIDTH)
        self._splitter.addWidget(right)
        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 2)
        self._splitter.setSizes(list(tok.WORKSPACE_SPLIT_SIZES))
        return self._splitter

    def _build_start_card(self) -> QWidget:
        page = QWidget()
        page_lay = QVBoxLayout(page)
        page_lay.setContentsMargins(0, 0, 0, 0)
        page_lay.setSpacing(10)
        self._start_scroll = QScrollArea()
        self._start_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._start_scroll.setWidgetResizable(True)
        self._start_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        self._start_scroll.setWidget(content)
        page_lay.addWidget(self._start_scroll, 1)
        lay = QVBoxLayout(content)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        self._card_title = QLabel("开始会议")
        lay.addWidget(self._card_title)
        self._card_hint = QLabel(
            "填写的信息会写进纪要并帮助模型理解上下文；全部可留空。"
            "本卡片的音源、模型和语言仅用于本场。默认不保存音频；开启「录音恢复」时会在本机缓存原声。"
        )
        self._card_hint.setWordWrap(True)
        lay.addWidget(self._card_hint)

        self._opt_title = QLineEdit()
        self._opt_title.setPlaceholderText("会议标题（留空按时间命名）")
        self._opt_title.setAccessibleName("会议标题")
        lay.addWidget(self._opt_title)
        self._opt_participants = QLineEdit()
        self._opt_participants.setPlaceholderText("参会人（逗号分隔）")
        self._opt_participants.setAccessibleName("参会人")
        lay.addWidget(self._opt_participants)
        self._opt_agenda = QPlainTextEdit()
        self._opt_agenda.setPlaceholderText("议程 / 背景（每行一条）")
        self._opt_agenda.setAccessibleName("议程")
        self._opt_agenda.setFixedHeight(90)
        lay.addWidget(self._opt_agenda)

        row = QHBoxLayout()
        row.setSpacing(10)
        self._opt_source = QComboBox()
        self._opt_source.setAccessibleName("会议音频来源")
        self._opt_source.addItem("跟随转写设置", "")
        for key, label in _SOURCE_LABELS:
            self._opt_source.addItem(label, key)
        self._opt_model = QComboBox()
        self._opt_model.setAccessibleName("会议识别模型")
        self._opt_language = QComboBox()
        self._opt_language.setAccessibleName("会议语言")
        for key, label in _LANGUAGE_LABELS:
            self._opt_language.addItem(label, key)
        for caption, field, stretch in (("音频来源", self._opt_source, 2),
                                        ("转写模型", self._opt_model, 2),
                                        ("识别语言", self._opt_language, 1)):
            column = QVBoxLayout()
            column.setSpacing(4)
            label = QLabel(caption)
            label.setBuddy(field)
            column.addWidget(label)
            column.addWidget(field)
            row.addLayout(column, stretch)
        lay.addLayout(row)

        self._card_notes = QLabel("")
        self._card_notes.setWordWrap(True)
        self._card_notes.hide()
        lay.addWidget(self._card_notes)
        dl_row = QHBoxLayout()
        self._card_dl_btn = QPushButton("下载辅助模型")
        self._card_dl_btn.setAccessibleName("下载会议辅助模型")
        self._card_dl_btn.clicked.connect(self._download_aux_models)
        self._card_dl_btn.hide()
        dl_row.addWidget(self._card_dl_btn, 0)
        dl_row.addStretch(1)
        lay.addLayout(dl_row)
        lay.addStretch(1)

        actions = QHBoxLayout()
        actions.addStretch(1)
        self._card_cancel = QPushButton("取消")
        self._card_cancel.clicked.connect(self._leave_start_card)
        actions.addWidget(self._card_cancel)
        self._card_start = QPushButton("开始")
        self._card_start.setAccessibleName("开始会议记录")
        self._card_start.setDefault(True)
        self._card_start.clicked.connect(self._start_from_card)
        actions.addWidget(self._card_start)
        page_lay.addLayout(actions)
        self._opt_source.currentIndexChanged.connect(self._update_card_notes)
        self._opt_model.currentIndexChanged.connect(self._update_card_notes)
        return page

    def _build_live(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        head = QHBoxLayout()
        head.setSpacing(12)
        self._live_title = QLabel("会议进行中")
        head.addWidget(self._live_title, 1)
        self._live_clock = QLabel("00:00:00")
        self._live_clock.setObjectName("meetingLiveClock")
        head.addWidget(self._live_clock, 0)
        lay.addLayout(head)
        self._live_status = QLabel("")
        self._live_status.setWordWrap(True)
        self._live_status.hide()
        lay.addWidget(self._live_status)

        speaker_tools = PairedToolBar(spacing=8)
        self._live_speaker_hint = QLabel("点击姓名可命名；纠正发言人只影响选中的段落，人工修改会保留。")
        self._live_speaker_hint.setWordWrap(True)
        self._live_speaker_btn = QPushButton("命名说话人")
        self._live_speaker_btn.setAccessibleName("会议记录中命名说话人")
        self._live_speaker_menu = QMenu(self._live_speaker_btn)
        self._live_speaker_menu.aboutToShow.connect(self._populate_live_speaker_menu)
        self._live_speaker_btn.setMenu(self._live_speaker_menu)
        speaker_tools.add_right(self._live_speaker_btn)
        self._live_correct_speaker_btn = QPushButton("纠正发言人")
        self._live_correct_speaker_btn.setCheckable(True)
        self._live_correct_speaker_btn.toggled.connect(self._toggle_live_speaker_correction)
        speaker_tools.add_right(self._live_correct_speaker_btn)
        lay.addWidget(speaker_tools)
        lay.addWidget(self._live_speaker_hint)
        self._live_speaker_feedback = QLabel()
        self._live_speaker_feedback.setWordWrap(True)
        self._live_speaker_feedback.hide()
        lay.addWidget(self._live_speaker_feedback)

        self._live_find_bar = QWidget()
        find_lay = QHBoxLayout(self._live_find_bar)
        find_lay.setContentsMargins(0, 0, 0, 0)
        self._live_find_edit = QLineEdit()
        self._live_find_edit.setPlaceholderText("查找本场文字稿；按 Enter 查找下一处")
        self._live_find_edit.setAccessibleName("查找本场文字稿")
        self._live_find_edit.setClearButtonEnabled(True)
        self._live_find_edit.textChanged.connect(lambda: self._find_live_text(restart=True))
        self._live_find_edit.returnPressed.connect(self._find_live_text)
        find_lay.addWidget(self._live_find_edit, 1)
        self._live_find_status = QLabel()
        find_lay.addWidget(self._live_find_status)
        self._live_find_close_btn = QPushButton("关闭查找")
        self._live_find_close_btn.clicked.connect(self._close_live_find)
        find_lay.addWidget(self._live_find_close_btn)
        self._live_find_bar.hide()
        lay.addWidget(self._live_find_bar)

        self._live_split = QSplitter(Qt.Orientation.Horizontal)
        self._live_split.setChildrenCollapsible(False)
        self._live_transcript = _TranscriptView()
        self._live_transcript.setOpenLinks(False)
        self._live_transcript.anchorClicked.connect(self._on_live_transcript_link)
        self._live_transcript.setAccessibleName("实时文字稿")
        self._live_transcript.setPlaceholderText("开始说话后，这里会按说话人逐条出现文字…")
        self._live_split.addWidget(self._live_transcript)
        side = QWidget()
        side_lay = QVBoxLayout(side)
        side_lay.setContentsMargins(12, 0, 0, 0)
        side_lay.setSpacing(8)
        self._marks_title = QLabel("标记")
        side_lay.addWidget(self._marks_title)
        mark_row = QHBoxLayout()
        mark_row.setSpacing(6)
        self._mark_note = QLineEdit()
        self._mark_note.setPlaceholderText("备注（可空）")
        self._mark_note.setAccessibleName("标记备注")
        self._mark_note.returnPressed.connect(self._add_mark)
        mark_row.addWidget(self._mark_note, 1)
        self._mark_btn = QPushButton("标记此刻")
        self._mark_btn.setAccessibleName("标记此刻")
        self._mark_btn.clicked.connect(self._add_mark)
        mark_row.addWidget(self._mark_btn)
        side_lay.addLayout(mark_row)
        self._marks_view = QTextBrowser()
        self._marks_view.setAccessibleName("标记列表")
        self._marks_view.setPlaceholderText("")
        self._marks_view.setMaximumHeight(160)
        side_lay.addWidget(self._marks_view)
        self._notes_title = QLabel("我的笔记")
        side_lay.addWidget(self._notes_title)
        self._user_notes = QPlainTextEdit()
        self._user_notes.setAccessibleName("我的笔记")
        self._user_notes.setPlaceholderText("随手记；生成纪要时会一并参考。")
        self._user_notes_timer = QTimer(self)
        self._user_notes_timer.setSingleShot(True)
        self._user_notes_timer.setInterval(800)
        self._user_notes_timer.timeout.connect(self._save_user_notes)
        self._user_notes.textChanged.connect(lambda: self._user_notes_timer.start())
        side_lay.addWidget(self._user_notes, 1)
        self._live_side_scroll = QScrollArea()
        self._live_side_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._live_side_scroll.setWidgetResizable(True)
        self._live_side_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._live_side_scroll.setMinimumWidth(240)
        self._live_side_scroll.setWidget(side)
        self._live_split.addWidget(self._live_side_scroll)
        self._live_split.setStretchFactor(0, 3)
        self._live_split.setStretchFactor(1, 2)
        lay.addWidget(self._live_split, 1)

        actions = QHBoxLayout()
        self._live_caps = QLabel("")
        self._live_caps.setWordWrap(True)
        actions.addWidget(self._live_caps, 1)
        self._live_dl_btn = QPushButton("下载辅助模型")
        self._live_dl_btn.setAccessibleName("下载会议辅助模型（进行中）")
        self._live_dl_btn.clicked.connect(self._download_aux_models)
        self._live_dl_btn.hide()
        actions.addWidget(self._live_dl_btn)
        self._pause_btn = QPushButton("暂停采集")
        self._pause_btn.setAccessibleName("暂停会议采集")
        self._pause_btn.setToolTip("暂停期间不采集新音频；已录内容继续识别，会议记录保留。")
        self._pause_btn.clicked.connect(self.pause_requested.emit)
        actions.addWidget(self._pause_btn)
        self._resume_btn = QPushButton("继续采集")
        self._resume_btn.setAccessibleName("继续会议采集")
        self._resume_btn.setToolTip("继续采集新的会议音频。")
        self._resume_btn.clicked.connect(self.resume_requested.emit)
        self._resume_btn.hide()
        actions.addWidget(self._resume_btn)
        self._end_btn = QPushButton("结束会议")
        self._end_btn.setAccessibleName("结束会议")
        self._end_btn.clicked.connect(self._confirm_end)
        actions.addWidget(self._end_btn)
        lay.addLayout(actions)
        return page

    # ── theme ─────────────────────────────────────────────────────

    def reapply_theme(self) -> None:
        from sayink.ui import design_tokens as tok
        from sayink.ui import settings_styles as ss

        self.setStyleSheet(ss.WINDOW_CSS)
        title_css = (
            f"font-family: {tok.FONT_DISPLAY}; font-size: {tok.TYPE_TITLE_LG}px; font-weight: 700;"
            f" background: transparent; color: {tok.TEXT};"
        )
        from sayink.ui.settings_components import PAGE_TITLE

        self._title_label.setStyleSheet(PAGE_TITLE)
        self._card_title.setStyleSheet(title_css)
        self._live_title.setStyleSheet(
            f"font-size: {tok.TYPE_TITLE}px; font-weight: 700; color: {tok.TEXT}; background: transparent;"
        )
        self._live_clock.setStyleSheet(
            f"font-size: {tok.TYPE_TITLE_LG}px; font-weight: 700; color: {tok.ACCENT}; background: transparent;"
        )
        dim = f"color: {tok.TEXT_SEC}; font-size: {tok.TYPE_FOOTNOTE}px; background: transparent;"
        for label in (self._list_summary, self._detail_meta, self._card_hint, self._live_caps,
                      self._card_notes, self._empty_label, self._template_label, self._content_hint,
                      self._notes_empty, self._live_speaker_hint, self._live_speaker_feedback,
                      self._materials_agenda):
            label.setStyleSheet(dim)
        for label in (self._marks_title, self._notes_title, self._materials_notes_label, self._materials_marks_label):
            label.setStyleSheet(
                f"color: {tok.TEXT}; font-size: {tok.TYPE_FOOTNOTE}px; font-weight: 700; background: transparent;"
            )
        status_css = (
            f"color: {tok.TEXT}; font-size: {tok.TYPE_FOOTNOTE}px; background: {tok.SURFACE_PEARL};"
            f" border: 1px solid {tok.HAIRLINE}; border-radius: {tok.RADIUS_MD}px; padding: 6px 10px;"
        )
        self._status_label.setStyleSheet(status_css)
        self._live_status.setStyleSheet(status_css)
        edit_css = f"""
            QLineEdit {{
                background: {tok.INPUT_BG}; color: {tok.TEXT};
                border: 1px solid {tok.CONTROL_BORDER}; border-radius: {tok.RADIUS_MD}px;
                padding: 6px 12px; min-height: {tok.CONTROL_FIELD_HEIGHT - 14}px; font-size: {tok.TYPE_BODY_SM}px;
            }}
            QLineEdit:focus {{ border: 2px solid {tok.ACCENT}; padding: 5px 11px; }}
        """
        self._search_edit.setStyleSheet(ss.build_search_field_css())
        for edit in (self._opt_title, self._opt_participants, self._mark_note, self._live_find_edit):
            edit.setStyleSheet(edit_css)
        # Reads as a heading; the frame only appears once the user goes to edit it.
        self._detail_title.setStyleSheet(f"""
            QLineEdit {{
                background: transparent; color: {tok.TEXT};
                border: 1px solid transparent; border-radius: {tok.RADIUS_MD}px;
                padding: 4px 4px; min-height: 32px;
                font-size: {tok.TYPE_HERO}px; font-weight: 700;
            }}
            QLineEdit:hover {{ border: 1px solid {tok.HAIRLINE}; }}
            QLineEdit:focus {{ background: {tok.SURFACE_PEARL}; border: 2px solid {tok.ACCENT}; padding: 3px 3px; }}
        """)
        self._paint_notes_state()
        self._template_combo.setMinimumHeight(tok.CONTROL_BTN_SM_HEIGHT)
        from sayink.ui.tray_icon import _menu_font, _menu_stylesheet

        for menu in (self._more_menu, self._speaker_menu, self._export_menu, self._live_speaker_menu):
            menu.setFont(_menu_font())
            menu.setStyleSheet(_menu_stylesheet())
        text_css = f"""
            QTextBrowser, QPlainTextEdit {{
                background: {tok.SURFACE_PEARL}; color: {tok.TEXT};
                border: 1px solid {tok.HAIRLINE}; border-radius: {tok.RADIUS_MD}px;
                padding: 8px; font-size: {tok.TYPE_BODY_SM}px;
                selection-background-color: {tok.ACCENT_SOFT}; selection-color: {tok.TEXT};
            }}
            QPlainTextEdit:focus {{ border: 2px solid {tok.ACCENT}; }}
        """
        for view in (self._notes_edit, self._live_transcript, self._marks_view, self._user_notes,
                     self._opt_agenda, self._record_notes, self._record_marks):
            view.setStyleSheet(text_css)
        self._notes_edit.setStyleSheet(text_css + f"QPlainTextEdit {{ font-size: {tok.READING_FONT_PX}px; }}")
        # Reading a saved meeting matches 历史: text sits on the page, and only
        # the editor gets a field border, so "can I type here" is visible.
        reading_css = f"""
            QTextBrowser {{
                background: transparent; color: {tok.TEXT}; border: none; padding: 4px 0;
                font-size: {tok.READING_FONT_PX}px;
                selection-background-color: {tok.ACCENT_SOFT}; selection-color: {tok.TEXT};
            }}
        """
        for view in (self._notes_view, self._transcript_view):
            view.setStyleSheet(reading_css)
            view.document().setDocumentMargin(0)
        self._list.setStyleSheet(f"""
            QListWidget {{ background: transparent; border: 2px solid transparent; outline: none;
                           color: {tok.TEXT}; font-size: {tok.TYPE_BODY_SM}px; }}
            {session_list_item_css()}
            QLabel#meetingRowTitle {{ color: {tok.TEXT}; font-size: {tok.TYPE_BODY_SM}px; background: transparent; }}
            QLabel#meetingRowMeta {{ color: {tok.TEXT_SEC}; font-size: {tok.TYPE_CAPTION}px;
                                     background: transparent; }}
        """)
        for combo in (self._opt_source, self._opt_model, self._opt_language, self._template_combo):
            combo.setStyleSheet("")  # Inherit the shared form and popup styles.
        # Match the small action buttons so 纪要 and 文字稿 share one tool-row height.
        self._template_combo.setStyleSheet(ss.build_toolbar_combo_css())
        for btn in (self._start_btn, self._card_start, self._mark_btn, self._resume_btn):
            btn.setStyleSheet(ss.BTN_ACCENT_SM)
        for btn in (self._generate_btn, self._edit_btn, self._copy_btn, self._transcript_copy_btn, self._export_btn, self._card_cancel,
                    self._cancel_btn, self._correct_speaker_btn, self._return_notes_btn, self._clear_search_btn,
                    self._pause_btn, self._live_find_close_btn, self._manage_templates_btn, self._live_correct_speaker_btn,
                    self._card_dl_btn, self._live_dl_btn, self._load_more_btn, self._materials_toggle):
            btn.setStyleSheet(ss.BTN_GHOST_SM)
        # Match 导出's real box (style min-height plus its border) so the tab
        # capsule and the button share one line. Both labels use the same weight.
        tab_h = max(self._export_btn.sizeHint().height(), tok.CONTROL_BTN_SM_HEIGHT)
        chip_h = max(20, tab_h - 8)
        self._tab_bar.setStyleSheet(f"""
            QWidget#meetingTabs {{ background: {tok.SURFACE_PEARL}; border: 1px solid {tok.HAIRLINE};
                                   border-radius: {tok.RADIUS_MD}px; }}
            QPushButton#meetingTab {{ background: transparent; color: {tok.TEXT_SEC};
                                      border: 1px solid transparent; border-radius: {tok.RADIUS_SM}px;
                                      padding: 0 12px; min-height: {chip_h}px; max-height: {chip_h}px;
                                      font-size: {tok.TYPE_BODY_SM}px; font-weight: 400; }}
            QPushButton#meetingTab:hover:!checked {{ color: {tok.TEXT}; background: {tok.ROW_HOVER}; }}
            QPushButton#meetingTab:checked {{ background: {tok.SECONDARY_CONTAINER}; color: {tok.TEXT};
                                              border-color: transparent; font-weight: 400; }}
            QPushButton#meetingTab:focus {{ border-color: {tok.ACCENT_FOCUS}; }}
        """)
        self._tab_bar.setFixedHeight(tab_h)
        no_indicator = "QPushButton::menu-indicator { image: none; width: 0px; }"
        self._more_btn.setStyleSheet(ss.BTN_GHOST_SM + no_indicator)
        self._speaker_btn.setStyleSheet(ss.BTN_GHOST_SM + no_indicator)
        self._live_speaker_btn.setStyleSheet(ss.BTN_GHOST_SM + no_indicator)
        self._end_btn.setStyleSheet(ss.BTN_DANGER_SM)
        for splitter in (self._splitter, self._live_split):
            splitter.setStyleSheet(
                "QSplitter::handle { background: transparent; }"
                f"QSplitter::handle:hover {{ background: {tok.ACCENT_FOCUS}; }}"
            )
        if self._detail_id:
            self._render_detail()

    # ── list / browse ─────────────────────────────────────────────

    def _custom_templates(self) -> list[dict]:
        custom = self._config.get("meeting.custom_templates", []) or []
        return [t for t in custom if isinstance(t, dict)]

    def _reload_templates(self) -> None:
        from sayink.ui.minutes_templates import populate_template_combo

        populate_template_combo(self._template_combo, self._config, self._template_combo.currentData())

    def _manage_templates(self) -> None:
        from sayink.ui.minutes_templates import MinutesTemplatesDialog

        dialog = MinutesTemplatesDialog(self._config, self._template_combo.currentData(), self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._reload_templates()
            self._template_combo.setCurrentIndex(self._template_combo.findData(dialog.selected_template_id))
            self._on_template_changed(self._template_combo.currentIndex())

    def refresh(self) -> None:
        self._reload_templates()
        if self._stack.currentIndex() == PAGE_START:
            self._sync_follow_source_label()
        if self._store.disabled:
            self._list_summary.setText("历史数据库不可用，会议记录无法保存")
            return
        keep = self._detail_id
        records = (self._store.search_meetings(self._search_query) if self._search_query
                   else self._store.list_meetings(limit=self._list_limit))
        if (keep and not self._search_query and records
                and all(record.id != keep for record in records)
                and self._store.get_meeting(keep) is not None):
            # A meeting opened past the first page stays selected when newer ones arrive.
            total = self._store.count_meetings()
            if total > self._list_limit:
                self._list_limit = total
                records = self._store.list_meetings(limit=self._list_limit)
        self._records = {r.id: r for r in records}
        minutes = self._minutes_markers()
        rows_key = (tuple(records), tuple(_meeting_row_text(record, minutes.get(record.id, "")) for record in records),
                    tok.FONT, tok.TEXT, tok.TEXT_SEC, tok.TEXT_DIM)
        self._list.blockSignals(True)
        try:
            if getattr(self, "_meeting_rows_key", None) != rows_key:
                self._list.clear()
                for record in records:
                    item = QListWidgetItem()
                    item.setData(Qt.ItemDataRole.UserRole, record.id)
                    item.setData(Qt.ItemDataRole.AccessibleTextRole, _meeting_row_text(record, minutes.get(record.id, "")))
                    item.setToolTip(record.display_title)
                    row = self._build_list_row(record, minutes.get(record.id, ""))
                    self._list.addItem(item)
                    self._list.setItemWidget(item, row)
                    row.ensurePolished()
                    for label in row.findChildren(QLabel):
                        label.ensurePolished()
                    item.setSizeHint(row.sizeHint())
                self._meeting_rows_key = rows_key
            if keep in self._records:
                for index in range(self._list.count()):
                    item = self._list.item(index)
                    if item.data(Qt.ItemDataRole.UserRole) == keep:
                        item.setSelected(True)
                        self._list.setCurrentItem(item)
                        break
            else:
                self._list.setCurrentItem(None)
        finally:
            self._list.blockSignals(False)
        count = len(records)
        if self._search_query:
            self._list_summary.setText(f"找到 {count} 场会议")
            self._load_more_btn.hide()
        else:
            total = self._store.count_meetings()
            if count < total:
                self._list_summary.setText(f"最近 {count} 场，共 {total} 场会议")
            else:
                self._list_summary.setText(f"共 {total} 场会议")
            self._load_more_btn.setVisible(count < total and count > 0)
        self._empty_label.setText(
            "未找到匹配的会议。\n试试其他关键词，或清除搜索查看全部会议。" if self._search_query else
            "还没有会议记录。\n点击右上角「开始会议」，或从托盘菜单开始。"
        )
        self._empty_label.setVisible(count == 0)
        self._list.setVisible(count > 0)
        self._clear_search_btn.setVisible(count == 0 and bool(self._search_query))
        if keep and keep in self._records:
            self._render_detail()
        elif records:
            self._list.setCurrentRow(0)
        elif not records:
            if not self._save_notes_draft() or not self._save_record_notes():
                return
            self._detail_id = ""
            self._clear_detail()
        self._detail_pane.setVisible(bool(records) or self._editing_notes)

    def _minutes_markers(self) -> dict[str, str]:
        """Meeting id → minutes job state shown in its list row."""
        markers = {job.meeting_id: "纪要生成失败" if job.failed else "纪要排队中"
                   for job in self._store.generation_queue()}
        self._listed_generating = self._ctrl.generating_for
        if self._listed_generating:
            markers[self._listed_generating] = "纪要生成中"
        return markers

    def _build_list_row(self, record: MeetingRecord, minutes: str = "") -> QWidget:
        row = QWidget()
        row.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        lay = QVBoxLayout(row)
        lay.setContentsMargins(13, 8, 10, 8)
        lay.setSpacing(2)
        title = _ElidedLabel(record.display_title)
        title.setObjectName("meetingRowTitle")
        lay.addWidget(title)
        meta = _ElidedLabel(_meeting_row_meta(record, minutes))
        meta.setObjectName("meetingRowMeta")
        lay.addWidget(meta)
        return row

    def _perform_search(self) -> None:
        if not self._save_notes_draft():
            self._search_edit.blockSignals(True)
            self._search_edit.setText(self._search_query)
            self._search_edit.blockSignals(False)
            return
        self._search_query = self._search_edit.text().strip()
        self.refresh()

    def focus_search(self) -> None:
        if self._stack.currentIndex() == PAGE_LIVE:
            self._live_find_bar.show()
            self._live_find_edit.setFocus()
            self._live_find_edit.selectAll()
        elif self._stack.currentIndex() == PAGE_START:
            self._opt_title.setFocus()
        else:
            self._search_edit.setFocus()
            self._search_edit.selectAll()

    def _close_live_find(self) -> None:
        self._live_find_bar.hide()
        self._live_transcript.setFocus()

    def _find_live_text(self, *, restart: bool = False) -> None:
        query = self._live_find_edit.text()
        view = self._live_transcript
        if restart or not query:
            cursor = view.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            view.setTextCursor(cursor)
        found = bool(query) and view.find(query)
        if query and not found and not restart:
            cursor = view.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            view.setTextCursor(cursor)
            found = view.find(query)
        self._live_find_status.setText("未找到" if query and not found else "")

    def meeting_count(self) -> int:
        return self._list.count()

    def _on_selection_changed(self) -> None:
        items = self._list.selectedItems()
        if not items:
            return
        meeting_id = str(items[0].data(Qt.ItemDataRole.UserRole) or "")
        if meeting_id != self._detail_id:
            if not self._save_notes_draft():
                self._list.blockSignals(True)
                for index in range(self._list.count()):
                    item = self._list.item(index)
                    if item.data(Qt.ItemDataRole.UserRole) == self._detail_id:
                        self._list.setCurrentItem(item)
                        break
                self._list.blockSignals(False)
                return
            if not self._save_record_notes():
                self._list.blockSignals(True)
                for index in range(self._list.count()):
                    item = self._list.item(index)
                    if item.data(Qt.ItemDataRole.UserRole) == self._detail_id:
                        self._list.setCurrentItem(item)
                        break
                self._list.blockSignals(False)
                return
            self._transcript_highlight = -1
            self._citation_return = None
            self._correcting_speakers = False
            self._status_label.hide()
            self._detail_id = meeting_id
            self._notes_view.restore_reading_position(0)
            self._transcript_view.restore_reading_position(0)
            self._render_detail()

    def _load_more(self) -> None:
        self._list_limit += 100
        self.refresh()

    def show_meeting(self, meeting_id: str) -> None:
        """Open a meeting in the browse view (after it ended, or from a notice)."""
        if not self._save_notes_draft() or not self._save_record_notes():
            return
        self._search_timer.stop()
        self._search_query = ""
        self._search_edit.blockSignals(True)
        self._search_edit.clear()
        self._search_edit.blockSignals(False)
        if meeting_id and self._store.get_meeting(meeting_id) is not None:
            visible = self._store.list_meetings(limit=self._list_limit)
            if all(item.id != meeting_id for item in visible):
                self._list_limit = max(self._list_limit, self._store.count_meetings())
        self._transcript_highlight = -1
        self._citation_return = None
        self._correcting_speakers = False
        self._status_label.hide()
        self._detail_id = meeting_id
        self._notes_view.restore_reading_position(0)
        self._transcript_view.restore_reading_position(0)
        self._stack.setCurrentIndex(PAGE_BROWSE)
        self._start_btn.setVisible(True)
        self.refresh()

    @property
    def current_meeting_id(self) -> str:
        return self._detail_id

    def _clear_detail(self) -> None:
        if not self._save_notes_draft() or not self._save_record_notes():
            return
        self._materials.hide()
        self._record_notes.blockSignals(True)
        self._record_notes.clear()
        self._record_notes.blockSignals(False)
        self._record_marks.clear()
        self._record_marks_html = None
        self._record_notes_meeting_id = ""
        self._detail_title.clear()
        self._detail_title.setPlaceholderText("会议标题")
        self._detail_meta.clear()
        self._notes_state.clear()
        self._paint_notes_state()
        self._content_hint.hide()
        self._return_notes_btn.hide()
        self._notes_view.clear()
        self._notes_empty.clear()
        self._notes_edit.clear()
        self._transcript_view.clear()
        self._speaker_menu.clear()
        self._status_label.hide()
        for widget in (self._generate_btn, self._edit_btn, self._copy_btn, self._transcript_copy_btn, self._export_btn,
                       self._more_btn, self._speaker_btn, self._correct_speaker_btn, self._template_combo, self._detail_title):
            widget.setEnabled(False)
        from sayink.ui import settings_styles as ss
        self._start_btn.setStyleSheet(ss.BTN_ACCENT_SM)

    def _render_detail(self) -> None:
        record = self._store.get_meeting(self._detail_id) if self._detail_id else None
        if record is None:
            self._clear_detail()
            return
        for widget in (self._generate_btn, self._edit_btn, self._copy_btn, self._transcript_copy_btn, self._export_btn,
                       self._more_btn, self._speaker_btn, self._template_combo, self._detail_title):
            widget.setEnabled(True)
        pending = self._pending_title
        if pending and pending[0] == self._detail_id and pending[1] != record.title:
            if self._detail_title.text() != pending[1]:
                self._detail_title.setText(pending[1])
        elif not self._detail_title.hasFocus():
            self._detail_title.setText(record.title)
            if pending and pending[0] == self._detail_id:
                self._pending_title = None
        self._detail_title.setPlaceholderText(record.display_title)
        meta = [_format_dt(record.started_at)]
        if record.ended_at:
            meta.append(_duration_label(record.duration_ms))
        if record.status in _STATUS_TEXT:
            meta.append(_STATUS_TEXT[record.status])
        self._detail_meta.setText(" · ".join(meta))
        self._show_materials(record)

        notes = self._store.get_notes(self._detail_id)
        generating = self._ctrl.generating_for == self._detail_id
        job = None if generating else self._ctrl.queued_generation(self._detail_id)
        waiting = job is not None and not job.failed
        retry_now = waiting and job.not_before > now_ms()
        blocked = generating or (waiting and not retry_now)
        from sayink.ui import settings_styles as ss

        has_notes = notes is not None and bool(notes.markdown)
        self._start_btn.setStyleSheet(ss.BTN_GHOST_SM)
        configured = minutes_settings_complete(self._config)
        if not configured:
            self._generate_btn.setText("配置 AI 接口")
            self._generate_btn.setAccessibleName("配置 AI 接口")
            self._generate_btn.setToolTip("打开 设置 → AI 服务，填写接口地址、密钥与模型")
        else:
            self._generate_btn.setText("重新生成" if has_notes else "生成纪要")
            self._generate_btn.setAccessibleName(self._generate_btn.text())
            self._generate_btn.setToolTip(
                "另一场会议正在生成；点击后排队，它完成后自动开始"
                if self._ctrl.generating_for and not generating and not waiting else ""
            )
        self._generate_btn.setStyleSheet(ss.BTN_GHOST_SM if has_notes and configured else ss.BTN_ACCENT_SM)
        self._generate_btn.setEnabled(not blocked and record.status != STATUS_LIVE)
        self._restore_action.setEnabled(not generating and notes is not None and bool(notes.previous_markdown))
        self._cancel_btn.setVisible(generating or waiting)
        self._cancel_btn.setText("取消" if generating else "取消排队")
        default_template = notes.template_id if notes is not None and notes.template_id else self._config.get("meeting.template_id", "general")
        template_id = self._pending_templates.get(self._detail_id, default_template)
        index = self._template_combo.findData(template_id)
        self._template_combo.blockSignals(True)
        if index >= 0:
            self._template_combo.setCurrentIndex(index)
        self._template_combo.blockSignals(False)
        self._template_combo.setEnabled(not blocked)
        self._manage_templates_btn.setEnabled(not self._ctrl.generating_for)
        self._sync_result_actions(notes)
        if (generating or waiting) and configured:
            if generating:
                busy = "整理中…" if self._is_refinement() else "生成中…"
            else:
                busy = "立即重试" if retry_now else "排队中…"
            self._generate_btn.setText(busy)
            self._generate_btn.setAccessibleName(busy)
        markdown = notes.markdown if notes is not None else ""
        if self._editing_notes:
            self._notes_stack.setCurrentWidget(self._notes_edit)
            self._edit_btn.setText("保存修改")
        else:
            self._notes_stack.setCurrentWidget(self._notes_view)
            self._edit_btn.setText("编辑正文")
            if generating and self._ctrl.generation_preview_text:
                self._notes_view.set_reading_plain(self._ctrl.generation_preview_text)
            elif markdown and notes is not None and self._is_refinement(notes.template_id):
                self._notes_view.set_reading_html("".join(
                    "<p>" + html.escape(p).replace("\n", "<br>") + "</p>"
                    for p in markdown.strip().split("\n\n") if p.strip()
                ))
            elif markdown:
                self._notes_view.set_reading_markdown(
                    link_citations(notes_for_display(markdown, record.display_title))
                )
            else:
                self._notes_view.clear()
                if record.status == STATUS_LIVE:
                    self._notes_view.setPlaceholderText("会议进行中，结束后可生成纪要或整理原文。")
                elif not configured:
                    self._notes_view.setPlaceholderText(
                        f"还没有{self._result_label()}。点击「配置 AI 接口」填写接口地址、密钥和模型，无需开启文字润色。"
                    )
                else:
                    self._notes_view.setPlaceholderText(
                        "融合多人发言，保留细节与个人语气，整理成通顺的书面文章。点击「整理原文」开始。"
                        if self._is_refinement() else "还没有纪要。选择模板后点击「生成纪要」。"
                    )
                self._notes_empty.setText(self._notes_view.placeholderText())
                self._notes_stack.setCurrentWidget(self._notes_empty)
        self._edit_btn.setEnabled(not generating and (bool(markdown) or self._editing_notes))
        self._sync_notes_state(notes)

        segments = self._store.get_segments(self._detail_id)
        names = self._ctrl.speaker_names(self._detail_id)
        lines = transcript_lines(segments, record.started_at)
        self._transcript_offsets = [line.offset_ms for line in lines]
        self._transcript_sequences = [s.seq for s in segments if (s.corrected_text or s.polished_text or s.raw_text).strip()]
        self._corrected_sequences = {s.seq for s in segments if s.corrected_text}
        self._speaker_locked_sequences = {s.seq for s in segments if s.speaker_locked}
        can_correct = record.status in (STATUS_ENDED, STATUS_INTERRUPTED) and not generating and bool(lines)
        self._transcript_editable = can_correct
        self._correct_speaker_btn.setEnabled(can_correct)
        self._correcting_speakers = self._correcting_speakers and can_correct
        self._correct_speaker_btn.blockSignals(True)
        self._correct_speaker_btn.setChecked(self._correcting_speakers)
        self._correct_speaker_btn.blockSignals(False)
        self._correct_speaker_btn.setText("完成纠正" if self._correcting_speakers else "纠正发言人")
        if self._transcript_highlight >= len(lines):
            self._transcript_highlight = -1
        self._transcript_view.set_reading_html(
            self._transcript_html(lines, names, links=True, highlight=self._transcript_highlight)
        )
        speaker_ids = sorted({int(s.speaker_id) for s in segments if int(s.speaker_id) > 0})
        self._speaker_menu.clear()
        for speaker_id in speaker_ids:
            label = names.get(speaker_id) or f"说话人{speaker_id}"
            action = self._speaker_menu.addAction(label)
            action.triggered.connect(lambda _=False, sid=speaker_id: self._prompt_rename_speaker(sid))
        self._speaker_btn.setVisible(bool(speaker_ids))
        self._speaker_btn.setEnabled(not generating)
        self._sync_copy_action()
        self._sync_content_hint()

    def _on_template_changed(self, index: int) -> None:
        self._template_combo.setToolTip(str(self._template_combo.itemData(index, Qt.ItemDataRole.ToolTipRole) or ""))
        if self._detail_id:
            self._pending_templates[self._detail_id] = str(self._template_combo.currentData() or "general")
            self._render_detail()

    def _is_refinement(self, template_id: str | None = None) -> bool:
        return get_template(template_id or str(self._template_combo.currentData() or "general"), self._custom_templates()).is_refinement

    def _result_label(self) -> str:
        notes = self._store.get_notes(self._detail_id) if self._detail_id else None
        template_id = notes.template_id if notes is not None and notes.markdown else None
        return "精整正文" if self._is_refinement(template_id) else "纪要"

    def _sync_result_actions(self, notes) -> None:
        label = self._result_label()
        has_notes = notes is not None and bool(notes.markdown)
        if minutes_settings_complete(self._config):
            if self._is_refinement():
                action = "重新整理" if has_notes and self._is_refinement(notes.template_id) else "整理原文"
            else:
                action = "重新生成" if has_notes else "生成纪要"
            self._generate_btn.setText(action)
            self._generate_btn.setAccessibleName(action)
        self._template_combo.setToolTip(str(self._template_combo.currentData(Qt.ItemDataRole.ToolTipRole) or ""))
        self._tab_notes.setText(label)
        self._notes_view.setAccessibleName("会议" + label)
        self._notes_edit.setAccessibleName("编辑会议" + label)
        self._edit_btn.setToolTip("直接修改" + label + "文本")
        self._export_actions["notes"].setText("仅导出" + label)
        previous_label = label
        if notes is not None and notes.previous_metadata:
            try:
                metadata = json.loads(notes.previous_metadata)
                if isinstance(metadata, dict):
                    previous_label = "精整正文" if self._is_refinement(metadata.get("template_id", "general")) else "纪要"
            except (ValueError, TypeError):
                pass
        self._restore_action.setText("恢复上一版" + previous_label)

    def _sync_notes_state(self, notes) -> None:
        if self._ctrl.generating_for == self._detail_id:
            progress = self._status_label.text().strip()
            if not progress:
                progress = "正在整理原文…" if self._is_refinement() else "正在生成纪要…"
            self._notes_state.setText(progress)
            running = "整理" if self._is_refinement() else "生成"
            self._notes_state.setToolTip(
                f"{running}进行中。这里显示的是尚未保存的预览；复制和导出仍用上一版，点「取消」可停下。"
            )
            self._status_label.hide()
            self._paint_notes_state()
            return
        if self._editing_notes:
            state = "正在编辑 · 尚未保存"
        elif notes is None or not notes.markdown:
            state = "尚未整理原文" if self._is_refinement() else "尚未生成纪要"
        else:
            state = "已手动编辑" if notes.user_edited else "AI 生成"
        if notes is not None and notes.transcript_changed:
            state += f" · 文字稿已更正，{self._result_label()}需核对，建议重新生成"
        elif notes is not None and "（待核对）" in notes.markdown:
            state += " · 含待核对内容"
        job = self._ctrl.queued_generation(self._detail_id) if self._detail_id else None
        if job is not None:
            state += " · " + self._queue_state(job)
        self._notes_state.setText(state)
        if job is not None:
            self._notes_state.setToolTip(
                "自动生成没有成功，不会再自动重试；修正问题后可手动生成。" if job.failed else
                "多场会议逐场生成，手动点的排在会后自动生成的前面；点「取消排队」可移出队列。"
            )
        else:
            self._notes_state.setToolTip(
                "保留细节与语气的 AI 整理稿，可切换到文字稿核对；手动编辑不等于内容已经核实。"
                if self._result_label() == "精整正文" else
                "纪要根据文字稿生成；点击引用可核对来源。手动编辑不等于内容已经核实。"
            )
        self._paint_notes_state()

    def _queue_state(self, job: QueuedGeneration) -> str:
        reason = job.last_error.rstrip("。")
        if job.failed:
            return f"自动生成失败：{reason}"
        if job.not_before > now_ms():
            at = time.strftime("%H:%M", time.localtime(job.not_before / 1000))
            return f"自动生成暂时失败（{reason}），{at} 自动重试"
        if job.automatic and self._ctrl.defers_automatic_jobs():
            return "会议进行中，本机模型空闲后自动生成"
        position = self._ctrl.queue_position(job.meeting_id)
        return f"排队中 · 第 {position} 位，前面的完成后自动开始" if position else "排队中"

    def _paint_notes_state(self) -> None:
        from sayink.ui import design_tokens as tok

        generating = bool(self._detail_id) and self._ctrl.generating_for == self._detail_id
        row = self._state_row.layout()
        if generating:
            self._state_row.setStyleSheet(
                f"QWidget#meetingState {{ background: {tok.SURFACE_PEARL}; border: 1px solid {tok.HAIRLINE};"
                f" border-radius: {tok.RADIUS_MD}px; }}"
            )
            self._notes_state.setStyleSheet(
                f"color: {tok.TEXT}; font-size: {tok.TYPE_BODY_SM}px; font-weight: 700;"
                f" background: transparent; border: none; padding: 0;"
            )
            row.setContentsMargins(10, 4, 6, 4)
            return
        self._state_row.setStyleSheet("QWidget#meetingState { background: transparent; border: none; }")
        self._notes_state.setStyleSheet(
            f"color: {tok.TEXT_SEC}; font-size: {tok.TYPE_FOOTNOTE}px; background: transparent; border: none; padding: 0;"
        )
        row.setContentsMargins(0, 0, 0, 0)

    def _sync_content_hint(self) -> None:
        transcript = self._content_stack.currentIndex() == 1
        self._return_notes_btn.setVisible(transcript and bool(self._citation_return and self._citation_return[0] == self._detail_id))
        if transcript:
            hint = ("点击发言旁的「更正」修改这一段的归属。" if self._correcting_speakers else
                    "移到段落上或用 Tab 聚焦可编辑、查看详情；点击姓名可命名。"
                    if self._transcript_editable else
                    "正在生成，完成前不能修改文字稿。" if self._ctrl.generating_for == self._detail_id else
                    "有可编辑的段落后，可以把鼠标移到段落上修改。")
        elif self._editing_notes:
            hint = "复制与导出使用当前草稿。切换会议或离开本页时自动保存。"
        elif self._ctrl.generating_for == self._detail_id:
            hint = "生成预览尚未保存；复制和导出仍使用上一版。点「取消」可停下。"
        else:
            notes = self._store.get_notes(self._detail_id) if self._detail_id else None
            if notes is not None and notes.markdown and self._template_combo.currentData() != notes.template_id:
                hint = f"下次生成将使用「{self._template_combo.currentText()}」；当前{self._result_label()}保持原模板。"
            elif self._is_refinement():
                hint = "融合成文，保留细节与口吻；重要内容请核对文字稿。"
            else:
                hint = ""
        self._content_hint.setText(hint)
        self._content_hint.setVisible(bool(hint))

    def _current_notes_text(self) -> str:
        if self._editing_notes:
            return self._notes_edit.toPlainText()
        notes = self._store.get_notes(self._detail_id) if self._detail_id else None
        return notes.markdown if notes is not None else ""

    def _sync_copy_action(self) -> None:
        if not hasattr(self, "_transcript_view"):
            return
        from sayink.ui import settings_styles as ss

        label = "复制草稿" if self._editing_notes else "复制" + self._result_label()
        self._copy_btn.setText(label)
        self._copy_btn.setAccessibleName(label)
        shortcut = "" if self._editing_notes else " · Ctrl+C"
        self._copy_btn.setToolTip(label + ("" if self._result_label() == "精整正文" else " Markdown") + shortcut)
        has_notes = bool(self._current_notes_text().strip())
        has_transcript = bool(self._detail_id and self._transcript_offsets)
        self._copy_btn.setEnabled(has_notes)
        self._transcript_copy_btn.setEnabled(has_transcript)
        # Same filled action as 历史's copy button. The large primary style is
        # taller than the shared small-button box and made the two toolbars disagree.
        self._copy_btn.setStyleSheet(ss.BTN_ACCENT_SM)
        self._transcript_copy_btn.setStyleSheet(ss.BTN_ACCENT_SM)
        self._export_actions["notes"].setEnabled(has_notes)
        self._export_actions["transcript"].setEnabled(has_transcript)
        self._export_actions["all"].setEnabled(bool(self._detail_id))

    def _transcript_html(self, lines, names: dict[int, str], *, links: bool = False, highlight: int = -1, live: bool = False) -> str:
        from sayink.ui import design_tokens as tok

        parts = []
        meeting_id = self._ctrl.current_id if live else self._detail_id
        sequences = self._live_transcript_sequences if live else self._transcript_sequences
        locked = self._live_locked_sequences if live else self._speaker_locked_sequences
        correcting = self._live_correcting_speakers if live else self._correcting_speakers
        speaker_link = "live-speaker" if live else "speaker"
        segment_link = "live-segment" if live else "segment"
        auto_names = {
            s.speaker_id for s in self._store.get_speakers(meeting_id)
            if s.suggested_name and not s.display_name and not s.name_locked
        } if links and meeting_id else set()
        previous_speaker = None
        for index, line in enumerate(lines):
            label = (names.get(line.speaker_id) or f"说话人{line.speaker_id}") if line.speaker_id > 0 else "说话人待确认"
            color = speaker_color(line.speaker_id)
            source = _speaker_source(line.speaker_id, names, auto_names)
            speaker_tip = html.escape(source + "；点击为这一组发言命名", quote=True)
            head = f"<a name='seg{index}'></a><span style='color:{tok.TEXT_DIM}'>[{_position_label(line.offset_ms)}]</span>"
            if line.speaker_id <= 0:
                head += f" <span style='color:{tok.TEXT_DIM}'>说话人待确认</span>"
            elif label and links and not live:
                if line.speaker_id != previous_speaker:
                    parts.append(
                        f"<p style='margin:10px 0 2px 0'><a href='{speaker_link}:{line.speaker_id}' "
                        f"title='{speaker_tip}' style='color:{color}; text-decoration:none'>"
                        f"<b>{html.escape(label)}</b></a></p>"
                    )
            elif label:
                if live and links:
                    head += (f" <a href='{speaker_link}:{line.speaker_id}' title='{speaker_tip}' "
                             f"style='color:{color}; text-decoration:none'><b>{html.escape(label)}</b></a>")
                else:
                    head += f" <b style='color:{color}'>{html.escape(label)}</b>"
            background = f" background-color:{tok.HIGHLIGHT_BG};" if index == highlight else ""
            correction = ""
            if links and index < len(sequences):
                seq = sequences[index]
                if not live and seq in self._corrected_sequences:
                    head += (f" <a href='edited-segment:{seq}' title='查看修改详情与原文，可恢复原文' "
                             f"style='color:{tok.TEXT_DIM}; font-size:12px; text-decoration:none'>已编辑</a>")
                if correcting:
                    correction = f" <a href='{segment_link}:{seq}' style='color:{tok.ACCENT_TEXT}'>更正</a>"
                elif not live and self._transcript_editable:
                    correction = (f" <a href='edit-segment:{seq}' title='编辑这一段文字稿' "
                                  "style='color:transparent; text-decoration:none'>编辑</a>")
                elif live and self._ctrl.state in (MeetingState.LIVE, MeetingState.PAUSED):
                    correction = (f" <a href='live-edit:{seq}' title='编辑这一段文字稿' "
                                  "style='color:transparent; text-decoration:none'>编辑</a>")
                if seq in locked and (live or seq not in self._corrected_sequences):
                    detail_link = "live-details" if live else "details-segment"
                    correction += (f" <a href='{detail_link}:{seq}' title='查看姓名来源与归属更正' "
                                   "style='color:transparent; text-decoration:none'>修改详情</a>")
            text = html.escape(line.text).replace("\n", "<br>")
            parts.append(f"<p style='margin:0 0 6px 0;{background}'>{head}&nbsp; {text}{correction}</p>")
            previous_speaker = line.speaker_id
        return "".join(parts)

    def _on_notes_link(self, url) -> None:
        if url.scheme() == "ts" and url.path().isdigit():
            self._jump_to_transcript(int(url.path()) * 1000)
        elif url.scheme() in ("http", "https"):
            QDesktopServices.openUrl(url)

    def _jump_to_transcript(self, offset_ms: int) -> None:
        """Show the transcript line a minutes citation points at."""
        index = bisect.bisect_right(self._transcript_offsets, offset_ms + 999) - 1
        if index < 0:
            return
        if self._content_stack.currentIndex() == 0:
            self._citation_return = (self._detail_id, self._notes_view.verticalScrollBar().value())
        self._transcript_highlight = index
        self._switch_tab(1)
        self._render_detail()
        self._transcript_view._reading_timer.stop()
        self._transcript_view.scrollToAnchor(f"seg{index}")

    def _return_to_notes(self) -> None:
        if self._citation_return is None or self._citation_return[0] != self._detail_id:
            return
        _, value = self._citation_return
        self._switch_tab(0)
        self._notes_view.restore_reading_position(value)

    def _set_status(self, text: str, *, transient: bool = False) -> None:
        self._status_timer.stop()
        self._status_label.setText(text)
        self._status_label.setVisible(bool(text))
        if text and transient:
            self._status_timer.start(4000)

    # ── detail actions ────────────────────────────────────────────

    def _save_title(self) -> None:
        if not self._detail_id:
            return
        record = self._records.get(self._detail_id) or self._store.get_meeting(self._detail_id)
        title = self._detail_title.text().strip()
        if record is None or title == record.title:
            return
        self._pending_title = (self._detail_id, title)
        self._ctrl.update_meeting(self._detail_id, title=title)
        if not self._store.flush() or (saved := self._store.get_meeting(self._detail_id)) is None or saved.title != title:
            self._render_detail()
            self._set_status("标题尚未保存，输入已保留。")
            return
        self._pending_title = None
        self.refresh()

    def _generate(self) -> None:
        if not self._detail_id or self._ctrl.generating_for == self._detail_id:
            return
        job = self._ctrl.queued_generation(self._detail_id)
        if job is not None and not job.failed and job.not_before <= now_ms():
            return
        if not minutes_settings_complete(self._config):
            self.configure_requested.emit()
            return
        if self._editing_notes:
            self._toggle_edit()
            if self._editing_notes:
                return
        if not self._confirm_regenerate():
            return
        host = self._ctrl.remote_host_needing_confirmation()
        if host:
            box = QMessageBox(self)
            box.setWindowTitle("发送到远端模型")
            box.setIcon(QMessageBox.Icon.Question)
            purpose = "整理原文" if self._is_refinement() else "生成纪要"
            box.setText(f"这场会议的完整文字稿将发送到 {host} {purpose}。")
            box.setInformativeText("不会发送音频。确认后本机记住该地址，之后不再询问。")
            ok = box.addButton("发送并生成", QMessageBox.ButtonRole.AcceptRole)
            box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(ok)
            box.exec()
            if box.clickedButton() != ok:
                return
            self._ctrl.confirm_remote_host(host)
        template_id = str(self._template_combo.currentData() or "general")
        self._ctrl.generate(self._detail_id, template_id=template_id)

    def _confirm_regenerate(self) -> bool:
        notes = self._store.get_notes(self._detail_id)
        if notes is None or not notes.markdown:
            return True
        box = QMessageBox(self)
        label = self._result_label()
        box.setWindowTitle("重新整理原文" if self._is_refinement() else "重新生成纪要")
        box.setIcon(QMessageBox.Icon.Question)
        if notes.user_edited:
            box.setText(f"当前{label}包含你的手动修改，重新生成会替换它。")
        else:
            box.setText(f"重新生成会替换当前{label}。")
        box.setInformativeText(f"将使用「{self._template_combo.currentText()}」重新生成。替换前的版本可在「更多 → 恢复上一版{label}」找回。")
        go = box.addButton("重新生成", QMessageBox.ButtonRole.AcceptRole)
        back = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(back if notes.user_edited else go)
        box.exec()
        return box.clickedButton() == go

    def _restore_previous(self) -> None:
        if self._ctrl.generating_for == self._detail_id:
            return
        notes = self._store.get_notes(self._detail_id) if self._detail_id else None
        if notes is None or not notes.previous_markdown:
            return
        if self._editing_notes:
            self._toggle_edit()
            if self._editing_notes:
                return
        self._pending_templates.pop(self._detail_id, None)
        restored = self._ctrl.restore_previous_notes(self._detail_id)
        self._switch_tab(0)
        self._render_detail()
        if not restored:
            self._set_status("尚未恢复上一版，当前内容保持不变。")
            return
        self._set_status(f"已恢复上一版{self._result_label()}；再次恢复可切回刚才的版本。")

    def _switch_tab(self, index: int) -> None:
        self._tab_group.button(index).setChecked(True)
        self._content_stack.setCurrentIndex(index)
        self._tab_tools.setCurrentIndex(index)
        self._sync_copy_action()
        self._sync_content_hint()

    def _cancel_job(self) -> None:
        if self._detail_id:
            self._ctrl.remove_from_queue(self._detail_id)

    def _keep_open_draft(self, meeting_id: str) -> None:
        """A draft the user is still typing must not be replaced by the model."""
        text = self._notes_edit.toPlainText()
        notes = self._store.get_notes(meeting_id)
        if notes is not None and text == notes.markdown:
            self._editing_notes = False
            return
        saved = self._save_notes_draft()
        self._ctrl.remove_from_queue(meeting_id)
        if meeting_id != self._detail_id:
            return
        # Cancellation announces itself first. Say what actually happened to
        # this draft, or the status bar keeps claiming generation is running.
        if saved:
            self._set_status(f"已保留正在编辑的{self._result_label()}，未用生成结果覆盖。")
        else:
            self._set_status("修改尚未保存，请重试；可先复制草稿保留内容。")

    def _on_generation_progress(self, meeting_id: str, text: str) -> None:
        if meeting_id == self._detail_id and self._editing_notes:
            self._keep_open_draft(meeting_id)
        if self._ctrl.generating_for != meeting_id:
            if meeting_id != self._listed_generating:
                self.refresh()
            return
        if meeting_id == self._detail_id:
            self._set_status(text)
        if meeting_id != self._listed_generating:
            # A queued job just started: its list row changes too.
            self.refresh()
        elif meeting_id == self._detail_id:
            self._render_detail()

    def _on_generation_preview(self, meeting_id: str, text: str) -> None:
        if meeting_id != self._detail_id or self._editing_notes:
            return
        if not text:
            self._render_detail()
            return
        self._notes_stack.setCurrentWidget(self._notes_view)
        bar = self._notes_view.verticalScrollBar()
        position = bar.value()
        at_end = position >= bar.maximum() - 24
        self._notes_view.set_reading_plain(text)
        self._notes_view.restore_reading_position(bar.maximum() if at_end else position)

    def _on_generation_finished(self, meeting_id: str) -> None:
        self._pending_templates.pop(meeting_id, None)
        if meeting_id == self._detail_id:
            if self._editing_notes and not self._save_notes_draft():
                self.refresh()
                return
            self._editing_notes = False
            self._switch_tab(0)
            self._render_detail()
            self._set_status(
                "原文已精整，可切换到文字稿核对重要内容。" if self._result_label() == "精整正文" else
                "纪要已生成，可点击时间戳核对文字稿。"
            )
        self.refresh()

    def _on_generation_failed(self, meeting_id: str, message: str) -> None:
        self.refresh()
        if meeting_id == self._detail_id:
            self._set_status(f"{'原文整理' if self._is_refinement() else '纪要生成'}失败：{message}")

    def _on_generation_cancelled(self, meeting_id: str) -> None:
        self.refresh()
        if meeting_id == self._detail_id:
            label = self._result_label()
            action = "原文整理" if label == "精整正文" else "纪要生成"
            self._set_status(f"已取消{action}，上一版{label}保持不变。")

    def _on_generation_queued(self, meeting_id: str) -> None:
        self.refresh()

    def _save_notes_draft(self) -> bool:
        if not self._editing_notes or not self._detail_id:
            return True
        text = self._notes_edit.toPlainText()
        notes = self._store.get_notes(self._detail_id)
        if notes is None or text != notes.markdown:
            if not self._ctrl.save_user_markdown(self._detail_id, text):
                self._set_status("修改尚未保存，请重试；可先复制草稿保留内容。")
                return False
        self._editing_notes = False
        return True

    def hideEvent(self, event) -> None:
        if hasattr(self, "_notes_edit"):
            editing = self._editing_notes
            self._save_record_notes()
            if self._save_notes_draft() and editing:
                self._render_detail()
        super().hideEvent(event)

    def eventFilter(self, obj, event) -> bool:
        if obj is getattr(self, "_notes_edit", None) and event.type() == QEvent.Type.KeyPress:
            if event.matches(QKeySequence.StandardKey.Save):
                self._save_open_minutes()
                return True
        return super().eventFilter(obj, event)

    def _save_open_minutes(self) -> None:
        if not self._editing_notes:
            return
        if not self._save_notes_draft():
            return
        self._set_status("修改已保存")
        self._render_detail()

    def unsaved_exit_draft(self) -> str | None:
        """Save what we can. Return text that did not land, so exit can ask first."""
        self.flush_pending_user_notes()
        if not self._save_record_notes():
            return self._record_notes.toPlainText() or " "
        if not self._editing_notes or not self._detail_id:
            return None
        text = self._notes_edit.toPlainText()
        if self._save_notes_draft():
            return None
        return text

    def _show_materials(self, record) -> None:
        marks = self._store.get_marks(record.id)
        agenda = (record.agenda or "").strip()
        has = bool(agenda or (record.notes_user or "").strip() or marks)
        self._materials.setVisible(has)
        self._materials_agenda.setVisible(bool(agenda))
        self._materials_agenda.setText(("议程：" + agenda) if agenda else "")
        # A refresh must not copy one meeting's notes into the next, or wipe
        # keystrokes that have not reached the database yet.
        same_meeting = self._record_notes_meeting_id == record.id
        pending = same_meeting and self._record_notes_timer.isActive()
        if not pending and (not same_meeting or not self._record_notes.hasFocus()):
            self._record_notes.blockSignals(True)
            self._record_notes.setPlainText(record.notes_user or "")
            self._record_notes.blockSignals(False)
        self._record_notes_meeting_id = record.id
        marks_html = "".join(
            f"<p style='margin:0 0 4px 0'>[{format_timestamp(mark.offset_ms)}] {html.escape(mark.note) or '标记'}</p>"
            for mark in marks
        )
        self._materials_marks_label.setVisible(bool(marks))
        self._record_marks.setVisible(bool(marks))
        if marks_html != self._record_marks_html:
            self._record_marks_html = marks_html
            self._record_marks.setHtml(marks_html)

    def _save_record_notes(self) -> bool:
        """Write the post-meeting notes. False leaves the typed text in place."""
        timer = getattr(self, "_record_notes_timer", None)
        if timer is not None:
            timer.stop()
        meeting_id = self._record_notes_meeting_id
        if not meeting_id or not hasattr(self, "_record_notes"):
            return True
        text = self._record_notes.toPlainText()
        record = self._store.get_meeting(meeting_id)
        if record is None or record.notes_user == text:
            return True
        # Keep the debounce running across the write. A refresh that sneaks in
        # while the writer is behind must not replace these keystrokes.
        if timer is not None:
            timer.start()
        self._ctrl.update_meeting(meeting_id, notes_user=text)
        saved = self._store.get_meeting(meeting_id) if self._store.flush() else None
        if saved is None or saved.notes_user != text:
            self._set_status("笔记尚未保存，输入已保留。")
            return False
        if timer is not None:
            timer.stop()
        return True

    def _toggle_edit(self) -> None:
        if not self._detail_id:
            return
        if self._editing_notes:
            if not self._save_notes_draft():
                return
            self._set_status("修改已保存")
        else:
            notes = self._store.get_notes(self._detail_id)
            self._notes_edit.setPlainText(notes.markdown if notes is not None else "")
            self._editing_notes = True
            self._notes_edit.setFocus()
        self._render_detail()

    def _copy_notes(self) -> None:
        text = self._current_notes_text()
        if not text:
            return
        done = "已复制当前草稿" if self._editing_notes else (
            "已复制精整正文" if self._result_label() == "精整正文" else "已复制纪要 Markdown"
        )
        self._set_status(done if copy_text(text) else "复制失败，请重试")

    def _copy_current(self) -> None:
        if self._content_stack.currentIndex() == 0:
            self._copy_notes()
        else:
            self._copy_transcript()

    def _copy_transcript(self) -> None:
        if self._detail_id:
            text = self._ctrl.transcript_text(self._detail_id)
            if text:
                self._set_status("已复制文字稿" if copy_text(text) else "复制失败，请重试")

    def _export(self, scope: str = "all") -> None:
        if not self._save_record_notes():
            return
        record = self._store.get_meeting(self._detail_id) if self._detail_id else None
        if record is None:
            return
        label = {"notes": self._result_label(), "transcript": "文字稿", "all": "完整会议记录"}[scope]
        default = f"{safe_filename(record.display_title)}-{label}-{time.strftime('%Y%m%d', time.localtime(record.started_at / 1000))}.md"
        path, _filter = QFileDialog.getSaveFileName(self, "导出" + label, default, "Markdown (*.md)")
        if not path:
            return
        try:
            if scope == "transcript":
                text = self._ctrl.export_transcript_markdown(record.id)
            elif scope == "notes":
                text = self._current_notes_text()
            elif self._editing_notes:
                supplement = self._ctrl.supplements_markdown(record.id)
                text = self._current_notes_text().rstrip()
                if supplement:
                    text += "\n\n" + supplement
                text += "\n\n## 文字稿\n\n" + self._ctrl.transcript_text(record.id) + "\n"
            else:
                text = self._ctrl.export_markdown(record.id)
            Path(path).write_text(text, encoding="utf-8")
        except OSError as exc:
            QMessageBox.critical(self, "导出失败", f"无法写入文件：{exc}")
            return
        self._set_status("已导出" + label + ("（包含当前草稿）" if self._editing_notes and scope != "transcript" else ""))

    def _delete(self) -> None:
        record = self._store.get_meeting(self._detail_id) if self._detail_id else None
        if record is None:
            return
        if record.status == STATUS_LIVE and self._ctrl.current_id == record.id:
            QMessageBox.information(self, "会议进行中", "请先结束会议，再删除记录。")
            return
        box = QMessageBox(self)
        box.setWindowTitle("删除会议")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(f"删除「{record.display_title}」？")
        box.setInformativeText("文字稿、笔记、标记与纪要会一起删除，无法撤销。")
        delete = box.addButton("删除", QMessageBox.ButtonRole.DestructiveRole)
        back = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(back)
        box.exec()
        if box.clickedButton() != delete:
            return
        self._ctrl.delete_meeting(record.id)
        self._detail_id = ""
        self.refresh()

    def _on_transcript_link(self, url) -> None:
        if url.scheme() == "speaker" and url.path().isdigit():
            self._prompt_rename_speaker(int(url.path()))
        elif url.scheme() == "segment" and url.path().isdigit() and self._correcting_speakers:
            self._prompt_segment_speaker(int(url.path()))
        elif url.scheme() == "edit-segment" and url.path().isdigit():
            self._prompt_segment_text(int(url.path()))
        elif url.scheme() in ("edited-segment", "details-segment") and url.path().isdigit():
            self._prompt_segment_text(int(url.path()), details=True)

    def _prompt_segment_text(self, seq: int, *, details: bool = False, meeting_id: str | None = None) -> None:
        meeting_id = meeting_id or self._detail_id
        record = self._store.get_meeting(meeting_id) if meeting_id else None
        if record is None:
            return
        generating = self._ctrl.generating_for == record.id
        live_session = record.status == STATUS_LIVE and self._ctrl.current_id == record.id
        draining = live_session and self._ctrl.state is MeetingState.DRAINING
        live_editable = live_session and self._ctrl.state in (MeetingState.LIVE, MeetingState.PAUSED) and not generating
        read_only = generating or draining or (record.status == STATUS_LIVE and (details or not live_editable))
        if read_only and not details:
            return
        if not read_only and not self._save_notes_draft():
            return
        segment = next((s for s in self._store.get_segments(record.id) if s.seq == seq), None)
        if segment is None:
            return
        names = self._ctrl.speaker_names(record.id)
        line = transcript_lines([segment], record.started_at)
        if not line:
            return
        speaker = ((names.get(line[0].speaker_id) or f"说话人{line[0].speaker_id}")
                   if line[0].speaker_id > 0 else "说话人待确认")
        detail_text = ""
        if details:
            auto_names = {s.speaker_id for s in self._store.get_speakers(record.id)
                          if s.suggested_name and not s.display_name and not s.name_locked}
            detail_text = "姓名来源：" + _speaker_source(segment.speaker_id, names, auto_names)
            if segment.speaker_locked:
                detail_text += "\n发言归属：已手动更正，仅作用于本段。"
            if segment.corrected_text:
                detail_text += "\n正文：已编辑，修改前的原文保留如下。"
        dialog = _SegmentTextDialog(
            segment, position=_position_label(line[0].offset_ms), speaker=speaker,
            save_text=lambda text: self._ctrl.save_segment_text(record.id, seq, text), parent=self,
            details=detail_text, read_only=read_only,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            action = "已恢复这一段的原文" if dialog.corrected_text is None else "已保存这一段的修改"
            if self._stack.currentIndex() == PAGE_LIVE and self._ctrl.current_id == record.id:
                self._render_live()
                self._live_transcript.place_cursor_at_segment(seq)
                self._live_feedback_timer.stop()
                self._live_speaker_feedback.setText(action + "。")
                self._live_speaker_feedback.show()
                self._live_feedback_timer.start(4000)
                return
            self._render_detail()
            self._transcript_view.place_cursor_at_segment(seq)
            notes = self._store.get_notes(record.id)
            hint = f"；已有{self._result_label()}保留，建议重新生成。" if notes is not None and notes.markdown else "。"
            self._set_status(action + hint, transient=True)

    def _toggle_speaker_correction(self, enabled: bool) -> None:
        self._correcting_speakers = enabled
        self._render_detail()

    def _prompt_segment_speaker(self, seq: int, *, meeting_id: str | None = None) -> None:
        meeting_id = meeting_id or self._detail_id
        record = self._store.get_meeting(meeting_id) if meeting_id else None
        if record is None or self._ctrl.generating_for == record.id:
            return
        live = record.status == STATUS_LIVE
        if live and (self._ctrl.current_id != record.id or self._ctrl.state is MeetingState.DRAINING):
            return
        segments = self._store.get_segments(record.id)
        segment = next((s for s in segments if s.seq == seq), None)
        if segment is None:
            return
        names = self._ctrl.speaker_names(record.id)
        ids = sorted({int(s.speaker_id) for s in segments if s.speaker_id > 0})
        choices = [(sid, f"{names[sid]} · 说话人{sid}" if sid in names else f"说话人{sid}") for sid in ids]
        choices.append((None, "新增说话人" if live else "新增说话人（仅本段）"))

        def save(speaker_id):
            if live and (self._ctrl.current_id != record.id or self._ctrl.state is MeetingState.DRAINING):
                return False
            return self._ctrl.correct_segment_speaker(record.id, seq, speaker_id)

        dialog = _SegmentSpeakerDialog(segment, choices, self, save_speaker=save, live=live)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._speaker_edit_status(record.id, "已更正这一段的发言人，人工更正会保留。" if live else
                                      "已更正这一段的发言人；请核对纪要中的相关归属。", transient=True)

    def _prompt_rename_speaker(self, speaker_id: int, *, meeting_id: str | None = None) -> None:
        meeting_id = meeting_id or self._detail_id
        if not meeting_id:
            return
        record = self._store.get_meeting(meeting_id)
        if record is None:
            return
        live = record.status == STATUS_LIVE
        if self._ctrl.generating_for == meeting_id:
            self._speaker_edit_status(meeting_id, "纪要生成中，请在完成后为说话人命名。")
            return
        if live and (self._ctrl.current_id != meeting_id or self._ctrl.state is MeetingState.DRAINING):
            return
        current = self._ctrl.speaker_names(meeting_id).get(speaker_id, "")
        scope = ("这组已有发言和后续归入本组的发言都会使用该姓名；留空恢复编号。" if live else
                 "会更新这一组发言的名称和纪要中的称呼；留空恢复编号。")
        name, ok = QInputDialog.getText(
            self, "为整组说话人命名",
            f"「说话人{speaker_id}」的显示名称\n{scope}",
            QLineEdit.EchoMode.Normal, current,
        )
        if ok:
            if live and (self._ctrl.current_id != meeting_id or self._ctrl.state is MeetingState.DRAINING):
                self._speaker_edit_status(meeting_id, "会议已结束或正在整理，姓名尚未保存，请在会后重新命名。")
                return
            self._rename_speaker(speaker_id, name, meeting_id=meeting_id)

    def _rename_speaker(self, speaker_id: int, name: str, *, meeting_id: str | None = None) -> None:
        meeting_id = meeting_id or self._detail_id
        if not meeting_id:
            return
        if meeting_id == self._detail_id and not self._save_notes_draft():
            return
        name = name.strip()
        if self._ctrl.rename_speaker(meeting_id, speaker_id, name) is None:
            self._speaker_edit_status(meeting_id, "姓名尚未保存，请在整理完成后重试。")
            return
        self._speaker_edit_status(meeting_id, f"已将说话人{speaker_id}改为「{name}」" if name else
                                  f"已恢复「说话人{speaker_id}」", transient=True)

    def _speaker_edit_status(self, meeting_id: str, text: str, *, transient: bool = False) -> None:
        if self._stack.currentIndex() == PAGE_LIVE and self._live_meeting_id == meeting_id:
            self._live_feedback_timer.stop()
            self._render_live()
            self._live_speaker_feedback.setText(text)
            self._live_speaker_feedback.show()
            if transient:
                self._live_feedback_timer.start(4000)
        elif self._detail_id == meeting_id:
            self._render_detail()
            self._set_status(text, transient=transient)

    def _populate_live_speaker_menu(self) -> None:
        meeting_id = self._ctrl.current_id
        self._live_speaker_menu.clear()
        if not meeting_id or self._ctrl.state is MeetingState.DRAINING:
            return
        names = self._ctrl.speaker_names(meeting_id)
        for sid in sorted({s.speaker_id for s in self._store.get_segments(meeting_id) if s.speaker_id > 0}):
            action = self._live_speaker_menu.addAction(names.get(sid) or f"说话人{sid}")
            action.triggered.connect(lambda _=False, sid=sid, mid=meeting_id:
                                     self._prompt_rename_speaker(sid, meeting_id=mid))

    def _toggle_live_speaker_correction(self, enabled: bool) -> None:
        self._live_correcting_speakers = enabled
        self._render_live()

    def _on_live_transcript_link(self, url) -> None:
        meeting_id = self._ctrl.current_id
        if not meeting_id or self._ctrl.state is MeetingState.DRAINING or not url.path().isdigit():
            return
        if url.scheme() == "live-speaker":
            self._prompt_rename_speaker(int(url.path()), meeting_id=meeting_id)
        elif url.scheme() == "live-segment" and self._live_correcting_speakers:
            self._prompt_segment_speaker(int(url.path()), meeting_id=meeting_id)
        elif url.scheme() == "live-edit":
            self._prompt_segment_text(int(url.path()), meeting_id=meeting_id)
        elif url.scheme() == "live-details":
            self._prompt_segment_text(int(url.path()), details=True, meeting_id=meeting_id)

    # ── start card ────────────────────────────────────────────────

    def show_start_card(self) -> None:
        if not self._save_notes_draft():
            return
        if self._ctrl.is_active:
            self._enter_live()
            return
        defaults = MeetingOptions.from_config(self._config)
        self._opt_title.clear()
        self._opt_participants.clear()
        self._opt_agenda.clear()
        self._sync_follow_source_label()
        index = self._opt_source.findData(self._config.get("meeting.input_source", "") or "")
        self._opt_source.setCurrentIndex(max(0, index))
        self._opt_model.blockSignals(True)
        self._opt_model.clear()
        self._opt_model.addItem("与转写相同", "")
        for model in MODEL_REGISTRY:
            if is_model_downloaded(model["id"]):
                self._opt_model.addItem(model["name"], model["id"])
        index = self._opt_model.findData(defaults.model_id)
        self._opt_model.setCurrentIndex(max(0, index))
        self._opt_model.blockSignals(False)
        index = self._opt_language.findData(defaults.language)
        self._opt_language.setCurrentIndex(max(0, index))
        self._update_card_notes()
        self._start_btn.setVisible(False)
        self._stack.setCurrentIndex(PAGE_START)
        self._opt_title.setFocus()

    def _sync_follow_source_label(self) -> None:
        """The follow option names the dictation source; a card choice stays put."""
        self._opt_source.setItemText(
            0, f"跟随转写（当前：{input_source_label(audio_source(self._config))}）"
        )

    def _update_card_notes(self) -> None:
        options = self._options_from_card()
        dictation = str(self._config.get("stt.model_id", DEFAULT_MODEL_ID) or DEFAULT_MODEL_ID)
        caps = resolve_capabilities(self._config, options, dictation_model_id=dictation)
        notes = list(caps.notes)
        if self._store.disabled:
            notes.insert(0, "历史数据库不可用：会议文字稿无法保存，现在无法开始会议。请重启应用或更换历史位置。")
        if not minutes_settings_complete(self._config):
            notes.append("尚未配置纪要模型（设置 → AI 服务）；会议结束后仍可先保存文字稿。")
        self._card_start.setEnabled(not self._store.disabled)
        self._card_notes.setText("\n".join(f"· {n}" for n in notes))
        self._card_notes.setVisible(bool(notes))
        self._sync_aux_buttons()

    # ── aux models (speaker embedding / Silero VAD) ───────────────

    def _sync_aux_buttons(self) -> None:
        """Show 「下载辅助模型」 wherever a helper model is still missing."""
        missing = missing_aux_models()
        if self._aux_dl.running:
            return
        label = f"下载辅助模型（约 {aux_models_size_mb(missing)} MB）" if missing else ""
        what = describe_aux_models(missing)
        self._card_dl_btn.setToolTip(
            f"{what}：下载后按共享识别设置在开始时加载；实际状态可在 {ADVANCED_RECOGNITION_PATH} 查看"
        )
        self._live_dl_btn.setToolTip(f"{what}：本场会议已按当前方式进行，下载后从下一场开始生效")
        for button in (self._card_dl_btn, self._live_dl_btn):
            button.setText(label)
            button.setEnabled(True)
            button.setVisible(bool(missing))

    def _download_aux_models(self) -> None:
        if self._aux_dl.running:
            return
        if not self._aux_dl.start():
            self._sync_aux_buttons()
            return
        for button in (self._card_dl_btn, self._live_dl_btn):
            button.setEnabled(False)
            button.setText("下载中 0%")

    def cancel_downloads(self) -> None:
        """Cancel any in-progress helper-model download on app exit."""
        self._aux_dl.cancel()

    @property
    def running_workers(self):
        return self._aux_dl.running_workers

    def _on_aux_progress(self, pct: int) -> None:
        for button in (self._card_dl_btn, self._live_dl_btn):
            button.setText(f"下载中 {int(pct)}%")

    def _on_aux_finished(self, ok: bool, message: str) -> None:
        if ok:
            self.aux_models_changed.emit()
            if self._stack.currentIndex() == PAGE_START:
                self._update_card_notes()
                self._card_notes.setText(
                    "· 辅助模型已下载，将按共享识别设置在开始时加载。\n" + self._card_notes.text()
                )
                self._card_notes.show()
            elif self._stack.currentIndex() == PAGE_LIVE:
                self._live_caps.setText(f"{self._live_caps.text()} · 辅助模型已下载，下一场会议生效")
        else:
            if self._stack.currentIndex() == PAGE_START:
                self._card_notes.setText(f"· {message}；可稍后重试，或在 引擎 页下载。")
                self._card_notes.show()
            elif self._stack.currentIndex() == PAGE_LIVE:
                self._live_status.setText(f"{message}；可稍后重试，或在 引擎 页下载。")
                self._live_status.show()
        self._sync_aux_buttons()

    def _options_from_card(self) -> MeetingOptions:
        defaults = MeetingOptions.from_config(self._config)
        return MeetingOptions(
            title=self._opt_title.text().strip(),
            participants=self._opt_participants.text().strip(),
            agenda=self._opt_agenda.toPlainText().strip(),
            input_source=str(self._opt_source.currentData() or audio_source(self._config)),
            model_id=str(self._opt_model.currentData() or ""),
            glossary=defaults.glossary,
            vad=defaults.vad,
            language=str(self._opt_language.currentData() or "auto"),
        )

    def _leave_start_card(self) -> None:
        self._start_btn.setVisible(True)
        self._stack.setCurrentIndex(PAGE_BROWSE)

    def _start_from_card(self) -> None:
        options = self._options_from_card()
        # These are session overrides. Only Settings changes saved defaults.
        self.start_requested.emit(options)

    # ── live view ─────────────────────────────────────────────────

    def _enter_live(self) -> None:
        record = self._ctrl.current
        meeting_id = record.id if record is not None else ""
        if meeting_id != self._live_meeting_id:
            self._live_meeting_id = meeting_id
            self._live_html = ""
            self._marks_html = None
            self._live_transcript.clear()
            self._live_find_edit.clear()
            self._live_find_bar.hide()
            self._live_correcting_speakers = False
            self._live_speaker_menu.clear()
            self._live_speaker_feedback.clear()
            self._live_speaker_feedback.hide()
            self._live_feedback_timer.stop()
        self._live_title.setText(record.display_title if record is not None else "会议进行中")
        self._live_clock.setText(self._ctrl.elapsed_text())
        caps = self._ctrl.capabilities
        if caps is not None:
            vad_label = "Silero VAD 已加载" if caps.vad == "silero" else "能量断句"
            if caps.vad_error:
                vad_label += "（Silero 加载失败，已回退）"
            elif caps.vad_requested == "silero" and caps.vad == "energy":
                vad_label += "（Silero 未下载）"
            bits = [f"模型 {caps.model_id}", vad_label]
            bits.append("说话人模型" if caps.speaker_model_available else "说话人（粗略）")
            if caps.speaker_model_available:
                bits.append("会内换人检测（实验性）；短促插话可能仍需确认")
            if caps.hotwords and caps.hotwords_supported:
                bits.append("热词已启用")
            self._live_caps.setToolTip(" · ".join(bits))
            self._live_caps.setText("说话人区分已增强" if caps.speaker_model_available else "说话人区分较粗略")
        self._sync_aux_buttons()
        self._user_notes.blockSignals(True)
        self._user_notes.setPlainText(record.notes_user if record is not None else "")
        self._user_notes.blockSignals(False)
        self._mark_note.clear()
        self._start_btn.setVisible(False)
        self._stack.setCurrentIndex(PAGE_LIVE)
        self._sync_live_state()
        self._render_live()

    def _sync_live_state(self) -> None:
        state = self._ctrl.state
        if state is MeetingState.PAUSED:
            self._live_status.setText(f"采集已暂停：{self._ctrl.paused_detail or '等待继续'}")
            self._live_status.show()
            self._resume_btn.show()
            self._pause_btn.hide()
            self._end_btn.setEnabled(True)
        elif state is MeetingState.DRAINING:
            self._live_status.setText("正在整理文字稿，请稍候…")
            self._live_status.show()
            self._resume_btn.hide()
            self._pause_btn.hide()
            self._end_btn.setEnabled(False)
            self._mark_btn.setEnabled(False)
        else:
            self._live_status.hide()
            self._resume_btn.hide()
            self._pause_btn.show()
            self._end_btn.setEnabled(True)
            self._mark_btn.setEnabled(True)

    def _on_state_changed(self, state: MeetingState) -> None:
        if state in (MeetingState.DRAINING, MeetingState.ENDED):
            # current_id is still this meeting. The debounce timer must not
            # fire later, after the controller has cleared it.
            self.flush_pending_user_notes()
        if state is MeetingState.LIVE and self._stack.currentIndex() != PAGE_LIVE:
            self._enter_live()
        elif state in (MeetingState.PAUSED, MeetingState.DRAINING, MeetingState.LIVE):
            self._sync_live_state()
            self._render_live()
        elif state is MeetingState.ENDED:
            self.show_meeting(self._ctrl.current_id)

    def _on_meeting_changed(self, meeting_id: str) -> None:
        self._pending_refresh_id = meeting_id
        self._refresh_timer.start()

    def _refresh_now(self) -> None:
        if self._stack.currentIndex() == PAGE_LIVE and self._ctrl.is_active:
            self._render_live()
            return
        if self._pending_refresh_id == self._detail_id or self._pending_refresh_id not in self._records:
            self.refresh()
        else:
            self._render_detail()

    def _render_live(self) -> None:
        meeting_id = self._ctrl.current_id
        if not meeting_id:
            return
        record = self._ctrl.current
        started_at = record.started_at if record is not None else 0
        segments = self._store.get_segments(meeting_id)
        names = self._ctrl.speaker_names(meeting_id)
        self._live_transcript_sequences = [s.seq for s in segments if (s.corrected_text or s.polished_text or s.raw_text).strip()]
        self._live_locked_sequences = {s.seq for s in segments if s.speaker_locked}
        editable = bool(self._live_transcript_sequences) and self._ctrl.state in (MeetingState.LIVE, MeetingState.PAUSED)
        self._live_speaker_btn.setEnabled(editable)
        self._live_correct_speaker_btn.setEnabled(editable)
        self._live_correcting_speakers = self._live_correcting_speakers and editable
        self._live_correct_speaker_btn.blockSignals(True)
        self._live_correct_speaker_btn.setChecked(self._live_correcting_speakers)
        self._live_correct_speaker_btn.blockSignals(False)
        self._live_correct_speaker_btn.setText("完成纠正" if self._live_correcting_speakers else "纠正发言人")
        self._live_speaker_hint.setText(
            "点击目标段落旁的「更正」修改归属，人工更正会保留。" if self._live_correcting_speakers else
            "移到已出现的段落上可改文字；点击姓名可命名。还在识别的这一句要等落段后才能改。"
        )
        bar = self._live_transcript.verticalScrollBar()
        cursor = self._live_transcript.textCursor()
        position, anchor = cursor.position(), cursor.anchor()
        scroll = bar.value()
        at_end = bar.value() >= bar.maximum() - 24
        rendered = self._transcript_html(transcript_lines(segments, started_at), names, links=True, live=True)
        if rendered != self._live_html:
            previous = self._live_html
            self._live_html = rendered
            if previous and rendered.startswith(previous):
                # Normal live transcription only adds paragraphs. Keep the
                # existing document instead of parsing and laying it out again.
                tail = QTextCursor(self._live_transcript.document())
                tail.movePosition(QTextCursor.MoveOperation.End)
                tail.insertBlock()
                tail.insertHtml(rendered[len(previous):])
                cursor = self._live_transcript.textCursor()
                last = self._live_transcript.document().characterCount() - 1
                anchor = self._live_transcript.position_before_action(min(anchor, last))
                position = self._live_transcript.position_before_action(min(position, last))
                cursor.setPosition(anchor)
                cursor.setPosition(position, QTextCursor.MoveMode.KeepAnchor)
                self._live_transcript.setTextCursor(cursor)
                bar.setValue(bar.maximum() if at_end and position == anchor else scroll)
            else:
                # Corrections and renames rebuild the document. setHtml puts the
                # selection back by paragraph, not by the old absolute offset.
                self._live_transcript.setHtml(rendered)
                if at_end and position == anchor:
                    end = self._live_transcript.textCursor()
                    end.movePosition(QTextCursor.MoveOperation.End)
                    self._live_transcript.setTextCursor(end)
                    bar.setValue(bar.maximum())
                else:
                    bar.setValue(scroll)
        marks = self._store.get_marks(meeting_id)
        if marks:
            marks_html = "".join(
                f"<p style='margin:0 0 4px 0'>[{format_timestamp(m.offset_ms)}] {html.escape(m.note) or '标记'}</p>"
                for m in marks
            )
        else:
            marks_html = (
                "<p style='margin:0 0 6px 0'>重要时刻按一下「标记此刻」。</p>"
                "<p style='margin:0'>纪要会优先覆盖这些点。</p>"
            )
        if marks_html != self._marks_html:
            self._marks_html = marks_html
            self._marks_view.setHtml(marks_html)

    def _add_mark(self) -> None:
        offset = self._ctrl.add_mark(self._mark_note.text())
        if offset is not None:
            self._mark_note.clear()
            self._render_live()

    def flush_pending_user_notes(self) -> None:
        if hasattr(self, "_user_notes_timer"):
            self._user_notes_timer.stop()
        self._save_user_notes()
        # Browse opens on ENDED and reads the database. The queued note has to
        # be visible before that, not only eventually.
        store = getattr(self, "_store", None)
        if store is not None:
            store.flush()

    def _save_user_notes(self) -> None:
        meeting_id = self._ctrl.current_id
        if not meeting_id:
            return
        text = self._user_notes.toPlainText()
        record = self._ctrl.current
        if record is not None and record.notes_user == text:
            return
        self._ctrl.update_meeting(meeting_id, notes_user=text)

    def _confirm_end(self) -> None:
        self._user_notes_timer.stop()
        self._save_user_notes()
        box = QMessageBox(self)
        box.setWindowTitle("结束会议")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText("结束这场会议？")
        box.setInformativeText("会先把剩余语音识别完，然后可以生成纪要。")
        end = box.addButton("结束会议", QMessageBox.ButtonRole.AcceptRole)
        back = box.addButton("继续记录", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(end)
        box.setEscapeButton(back)
        box.exec()
        if box.clickedButton() == end:
            self.end_requested.emit()


__all__ = ["MeetingPage", "PAGE_BROWSE", "PAGE_LIVE", "PAGE_START"]

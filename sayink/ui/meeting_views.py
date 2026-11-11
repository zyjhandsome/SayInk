"""Reusable reading/transcript views, segment correction dialogs, and HTML helpers for MeetingPage."""

from __future__ import annotations

import html
from pathlib import Path
import re
import time

from PyQt6.QtCore import QEvent, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QFont,
    QKeySequence,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextDocumentFragment,
    QTextFormat,
)
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QTextBrowser,
    QVBoxLayout,
)

from sayink.meeting_store import STATUS_ENDED, STATUS_INTERRUPTED, STATUS_LIVE, MeetingRecord
from sayink.minutes_prompts import format_timestamp
from sayink.ui.reading import fit_reading_width

_STATUS_TEXT = {STATUS_LIVE: "进行中", STATUS_INTERRUPTED: "已中断", STATUS_ENDED: "已结束"}
_HEADER_META_PREFIXES = ("- 时间：", "- 时长：")
_CITATION = re.compile(r"\[(\d{1,2}):(\d{2}):(\d{2})\](?!\()")
_PARAGRAPH_RE = re.compile(r"<p\b[^>]*>.*?</p>", re.DOTALL)
_SPEAKER_ANCHOR_RE = re.compile(
    r"<a href='((?:live-)?speaker:\d+)' title='([^']*)' style='[^']*'><b>([^<]*)</b></a>"
)


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


def _split_top_paragraphs(source: str) -> list[str] | None:
    if not source:
        return None
    parts = _PARAGRAPH_RE.findall(source)
    if not parts or sum(len(part) for part in parts) != len(source):
        return None
    return parts


def _speaker_anchor_skeleton(source: str) -> str:
    return _SPEAKER_ANCHOR_RE.sub(r"<a href='\1'><b></b></a>", source)


def _speaker_anchor_map(source: str) -> dict[str, tuple[str, str]]:
    found: dict[str, tuple[str, str]] = {}
    for href, title, label in _SPEAKER_ANCHOR_RE.findall(source):
        item = (html.unescape(label), html.unescape(title))
        if href in found and found[href] != item:
            return {}
        found[href] = item
    return found


def _speaker_source(speaker_id: int, names: dict[int, str], auto_names: set[int]) -> str:
    if speaker_id <= 0:
        return "说话人待确认"
    if speaker_id in auto_names:
        return "姓名来自原文自报，仅作本场称呼"
    if speaker_id in names:
        return "此姓名由你设置"
    return "按声音自动区分，尚未命名"


def build_transcript_html(
    lines,
    names: dict[int, str],
    *,
    links: bool = False,
    highlight: int = -1,
    live: bool = False,
    sequences: list[int] = (),
    corrected_sequences: set[int] | frozenset[int] = frozenset(),
    locked_sequences: set[int] | frozenset[int] = frozenset(),
    correcting: bool = False,
    editable: bool = False,
    live_editable: bool = False,
    auto_names: set[int] | frozenset[int] = frozenset(),
) -> str:
    """Build transcript HTML paragraphs for post-meeting or live views."""
    from sayink.ui import design_tokens as tok

    parts: list[str] = []
    speaker_link = "live-speaker" if live else "speaker"
    segment_link = "live-segment" if live else "segment"
    auto_set = set(auto_names)
    previous_speaker = None
    for index, line in enumerate(lines):
        label = (names.get(line.speaker_id) or f"说话人{line.speaker_id}") if line.speaker_id > 0 else "说话人待确认"
        color = speaker_color(line.speaker_id)
        source = _speaker_source(line.speaker_id, names, auto_set)
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
                head += (
                    f" <a href='{speaker_link}:{line.speaker_id}' title='{speaker_tip}' "
                    f"style='color:{color}; text-decoration:none'><b>{html.escape(label)}</b></a>"
                )
            else:
                head += f" <b style='color:{color}'>{html.escape(label)}</b>"
        background = f" background-color:{tok.HIGHLIGHT_BG};" if index == highlight else ""
        correction = ""
        if links and index < len(sequences):
            seq = sequences[index]
            if not live and seq in corrected_sequences:
                head += (
                    f" <a href='edited-segment:{seq}' title='查看修改详情与原文，可恢复原文' "
                    f"style='color:{tok.TEXT_DIM}; font-size:12px; text-decoration:none'>已编辑</a>"
                )
            if correcting:
                correction = f" <a href='{segment_link}:{seq}' style='color:{tok.ACCENT_TEXT}'>更正</a>"
            elif not live and editable:
                correction = (
                    f" <a href='edit-segment:{seq}' title='编辑这一段文字稿' "
                    "style='color:transparent; text-decoration:none'>编辑</a>"
                )
            elif live and live_editable:
                correction = (
                    f" <a href='live-edit:{seq}' title='编辑这一段文字稿' "
                    "style='color:transparent; text-decoration:none'>编辑</a>"
                )
            if seq in locked_sequences and (live or seq not in corrected_sequences):
                detail_link = "live-details" if live else "details-segment"
                correction += (
                    f" <a href='{detail_link}:{seq}' title='查看姓名来源与归属更正' "
                    "style='color:transparent; text-decoration:none'>修改详情</a>"
                )
        text = html.escape(line.text).replace("\n", "<br>")
        parts.append(f"<p style='margin:0 0 6px 0;{background}'>{head}&nbsp; {text}{correction}</p>")
        previous_speaker = line.speaker_id
    return "".join(parts)


class _ReadingView(QTextBrowser):
    """QTextBrowser tuned for long CJK text: looser leading, a heading scale
    below the page title, and text that wraps to the current detail width."""

    copy_document_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._reading_key = None
        self._reading_anchor = 0
        self._defer_layout = False
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
        previous = self._reading_key
        if (
            not plain and not markdown and previous
            and previous[0] is False and previous[1] is False
            and previous[3] == tok.ACCENT_TEXT and previous[4] == tok.TEXT_SEC
            and (
                self.retarget_speaker_labels(previous[2], text)
                or self.retarget_paragraphs(previous[2], text, polish_line_height=True)
            )
        ):
            # Renames and localized segment edits rewrite in place so a long
            # transcript does not re-parse and re-layout every paragraph.
            self._reading_key = key
            self._fit_text_width()
            return
        value = self.verticalScrollBar().value()
        self._reading_key = key
        self._defer_layout = True
        try:
            if plain:
                self.setPlainText(text)
            elif markdown:
                self.setMarkdown(text)
            else:
                self.setHtml(text)
            self._polish_blocks()
            self._fit_text_width()
        finally:
            self._defer_layout = False
        self._after_full_reading_rebuild()
        self.restore_reading_position(value)

    def _after_full_reading_rebuild(self) -> None:
        pass

    def _before_retarget(self):
        return None

    def _finish_retarget_block(self, _cursor: QTextCursor, _block) -> None:
        pass

    def _after_retarget(self, _saved) -> None:
        pass

    def set_reading_markdown(self, markdown: str) -> None:
        self._set_reading(markdown, markdown=True)

    def set_reading_html(self, text: str) -> None:
        self._set_reading(text, markdown=False)

    def retarget_speaker_labels(self, old_html: str, new_html: str) -> bool:
        """Replace speaker-link text in place when the paragraphs did not change.

        Returns False when the caller should rebuild the document.
        """
        if not old_html or _speaker_anchor_skeleton(old_html) != _speaker_anchor_skeleton(new_html):
            return False
        old_map = _speaker_anchor_map(old_html)
        new_map = _speaker_anchor_map(new_html)
        if not old_map or set(old_map) != set(new_map):
            return False
        changed = {href: new_map[href] for href in old_map if old_map[href] != new_map[href]}
        if not changed:
            return False
        edits = []
        seen: set[str] = set()
        block = self.document().begin()
        while block.isValid():
            iterator = block.begin()
            while not iterator.atEnd():
                fragment = iterator.fragment()
                href = fragment.charFormat().anchorHref()
                if href in changed:
                    old_label = old_map[href][0]
                    if fragment.text() == old_label:
                        edits.append((fragment.position(), fragment.position() + fragment.length(), href))
                        seen.add(href)
                    elif fragment.text() != changed[href][0]:
                        return False
                iterator += 1
            block = block.next()
        label_changes = {href for href, (label, _title) in changed.items() if label != old_map[href][0]}
        if not label_changes.issubset(seen):
            return False
        cursor = QTextCursor(self.document())
        cursor.beginEditBlock()
        for start, end, href in reversed(edits):
            label, title = changed[href]
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            fmt = cursor.charFormat()
            fmt.setAnchor(True)
            fmt.setAnchorHref(href)
            fmt.setToolTip(title)
            cursor.insertText(label, fmt)
        cursor.endEditBlock()
        return True

    def retarget_paragraphs(self, old_html: str, new_html: str, *, polish_line_height: bool = True) -> bool:
        """Patch a small set of changed ``<p>`` blocks in place without rebuilding.

        Returns False when paragraph count changes (e.g. speaker runs split or
        merge) or when many blocks changed at once.
        """
        from sayink.ui import design_tokens as tok

        old_paras = _split_top_paragraphs(old_html)
        new_paras = _split_top_paragraphs(new_html)
        doc = self.document()
        if (
            old_paras is None or new_paras is None
            or len(old_paras) != len(new_paras)
            or doc.blockCount() != len(old_paras)
        ):
            return False
        changed = [
            idx
            for idx, (old_p, new_p) in enumerate(zip(old_paras, new_paras, strict=True))
            if old_p != new_p
        ]
        if not changed or len(changed) > 12:
            return False
        default_font = doc.defaultFont()
        line_height = float(tok.READING_LINE_HEIGHT)
        prop_height = QTextBlockFormat.LineHeightTypes.ProportionalHeight.value
        prepared: list[tuple[int, QTextBlockFormat, QTextDocumentFragment]] = []
        for idx in changed:
            frag_doc = QTextDocument()
            frag_doc.setDefaultFont(default_font)
            frag_doc.setHtml(new_paras[idx])
            if frag_doc.blockCount() != 1:
                return False
            fmt = frag_doc.begin().blockFormat()
            if polish_line_height:
                fmt.setLineHeight(line_height, prop_height)
            prepared.append((idx, fmt, QTextDocumentFragment(frag_doc)))
        saved = self._before_retarget()
        cursor = QTextCursor(doc)
        cursor.beginEditBlock()
        try:
            for idx, fmt, fragment in reversed(prepared):
                block = doc.findBlockByNumber(idx)
                if not block.isValid():
                    continue
                cursor.setPosition(block.position())
                cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
                cursor.removeSelectedText()
                cursor.setBlockFormat(fmt)
                cursor.insertFragment(fragment)
                updated = doc.findBlockByNumber(idx)
                if polish_line_height:
                    self._style_links_with_cursor(cursor, updated, tok.ACCENT_TEXT)
                self._finish_retarget_block(cursor, updated)
        finally:
            cursor.endEditBlock()
        layout = doc.documentLayout()
        for idx, _, _ in prepared:
            block = doc.findBlockByNumber(idx)
            if block.isValid():
                layout.blockBoundingRect(block)
        self._after_retarget(saved)
        return True

    def set_reading_plain(self, text: str) -> None:
        self._set_reading(text, markdown=False, plain=True)

    def clear(self) -> None:
        self._reading_key = None
        self._reading_timer.stop()
        super().clear()

    def keyPressEvent(self, event) -> None:
        if (
            event.matches(QKeySequence.StandardKey.Copy)
            and not self.textCursor().hasSelection()
            and self.receivers(self.copy_document_requested)
        ):
            self.copy_document_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def _polish_blocks(self) -> None:
        from sayink.ui import design_tokens as tok

        doc = self.document()
        heading_steps = {1: 0, 2: 1}
        line_height = float(tok.READING_LINE_HEIGHT)
        prop_height = QTextBlockFormat.LineHeightTypes.ProportionalHeight.value
        cursor = QTextCursor(doc)
        cursor.beginEditBlock()
        try:
            block = doc.begin()
            while block.isValid():
                cursor.setPosition(block.position())
                fmt = block.blockFormat()
                level = fmt.headingLevel()
                fmt.setLineHeight(line_height, prop_height)
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
                self._style_links_with_cursor(cursor, block, tok.ACCENT_TEXT)
                block = block.next()
        finally:
            cursor.endEditBlock()

    @staticmethod
    def _style_links_with_cursor(cursor: QTextCursor, block, color: str) -> None:
        ranges = []
        it = block.begin()
        while not it.atEnd():
            fragment = it.fragment()
            fmt = fragment.charFormat()
            if fmt.isAnchor() and fmt.anchorHref().startswith("ts:"):
                ranges.append((fragment.position(), fragment.position() + fragment.length()))
            it += 1
        if not ranges:
            return
        link = QTextCharFormat()
        link.setForeground(QColor(color))
        link.setFontUnderline(False)
        for start, end in ranges:
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            cursor.mergeCharFormat(link)

    @staticmethod
    def _style_links(block, color: str) -> None:
        cursor = QTextCursor(block.document())
        _ReadingView._style_links_with_cursor(cursor, block, color)

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
        if not self._defer_layout:
            # setHtml only lays out the first screen. A long meeting then shows a
            # scrollbar full of blank paragraphs until something touches each block.
            self._layout_every_block()
        self.reset_actions()
        self._restore_body_selection(saved)

    def _after_full_reading_rebuild(self) -> None:
        self._layout_every_block()

    def _before_retarget(self):
        saved = self._capture_body_selection()
        self._activate_actions()
        return saved

    def _finish_retarget_block(self, cursor: QTextCursor, block) -> None:
        self._paint_actions_with_cursor(cursor, block, False)

    def _after_retarget(self, saved) -> None:
        self.reset_actions()
        self._restore_body_selection(saved)

    def _layout_every_block(self) -> None:
        layout = self.document().documentLayout()
        block = self.document().begin()
        while block.isValid():
            layout.blockBoundingRect(block)
            block = block.next()

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
        doc = self.document()
        ranges: list[tuple[int, int]] = []
        block = doc.begin()
        while block.isValid():
            it = block.begin()
            while not it.atEnd():
                fragment = it.fragment()
                if fragment.isValid() and fragment.charFormat().anchorHref().startswith(self._CONTEXT_LINKS):
                    ranges.append((fragment.position(), fragment.position() + fragment.length()))
                it += 1
            block = block.next()
        if ranges:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(Qt.GlobalColor.transparent))
            cursor = QTextCursor(doc)
            cursor.beginEditBlock()
            try:
                for start, end in ranges:
                    cursor.setPosition(start)
                    cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
                    cursor.mergeCharFormat(fmt)
            finally:
                cursor.endEditBlock()
        self._action_block = None

    def position_before_action(self, position: int) -> int:
        """Pull a caret out of a hover action so that word cannot stay on screen."""
        last = max(0, self.document().characterCount() - 1)
        cursor = QTextCursor(self.document())
        cursor.setPosition(min(max(0, position), last))
        while cursor.position() > 0 and cursor.charFormat().anchorHref().startswith(self._CONTEXT_LINKS):
            cursor.setPosition(cursor.position() - 1)
        return cursor.position()

    def _paint_actions_with_cursor(self, cursor: QTextCursor, block, visible: bool) -> None:
        from sayink.ui import design_tokens as tok

        if block is None or not block.isValid():
            return
        ranges = []
        it = block.begin()
        while not it.atEnd():
            fragment = it.fragment()
            if fragment.isValid() and fragment.charFormat().anchorHref().startswith(self._CONTEXT_LINKS):
                ranges.append((fragment.position(), fragment.length()))
            it += 1
        if not ranges:
            return
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(tok.ACCENT_TEXT) if visible else QColor(Qt.GlobalColor.transparent))
        for start, length in ranges:
            cursor.setPosition(start)
            cursor.setPosition(start + length, QTextCursor.MoveMode.KeepAnchor)
            cursor.mergeCharFormat(fmt)

    def _paint_actions(self, block, visible: bool) -> None:
        if block is None or not block.isValid():
            return
        cursor = QTextCursor(self.document())
        self._paint_actions_with_cursor(cursor, block, visible)

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

    def __init__(
        self,
        segment,
        *,
        position: str,
        speaker: str,
        save_text,
        parent=None,
        details: str = "",
        read_only: bool = False,
    ):
        super().__init__(parent)
        from sayink.ui import design_tokens as tok
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
        hint = QLabel(
            "当前只能查看。生成或收尾结束后可以再改这一段。" if read_only else
            "仅修改这一段。保存后用于纪要、原文精整、复制和导出，时间与发言人保持不变。"
        )
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
            if (
                event.modifiers() == Qt.KeyboardModifier.ControlModifier
                and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
            ):
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


def _export_kind(path: str, selected: str) -> str:
    chosen = selected or ""
    lower = path.lower()
    if chosen.startswith("SRT") or lower.endswith(".srt"):
        return "srt"
    if chosen.startswith("纯文本") or lower.endswith(".txt"):
        return "txt"
    return "md"


def _with_export_suffix(path: str, kind: str) -> str:
    suffix = {"md": ".md", "txt": ".txt", "srt": ".srt"}[kind]
    target = Path(path)
    if target.suffix.lower() == suffix:
        return str(target)
    return str(target.with_suffix(suffix))


__all__ = [
    "_CurrentPageStack",
    "_ReadingView",
    "_STATUS_TEXT",
    "_SegmentSpeakerDialog",
    "_SegmentTextDialog",
    "_TranscriptView",
    "_duration_label",
    "_export_kind",
    "_format_dt",
    "_meeting_row_meta",
    "_meeting_row_text",
    "_position_label",
    "_speaker_anchor_map",
    "_speaker_anchor_skeleton",
    "_speaker_source",
    "_split_top_paragraphs",
    "_with_export_suffix",
    "build_transcript_html",
    "link_citations",
    "notes_for_display",
    "speaker_color",
]

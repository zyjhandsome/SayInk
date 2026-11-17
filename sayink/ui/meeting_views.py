"""Reusable reading/transcript views, segment correction dialogs, and HTML helpers for MeetingPage."""

from __future__ import annotations

import html
from pathlib import Path
import re
import time

from PyQt6.QtCore import QEvent, Qt, QTimer, QUrl, pyqtSignal
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
    QStackedWidget,
    QTextBrowser,
    QTextEdit,
)

from sayink.meeting_store import STATUS_ENDED, STATUS_INTERRUPTED, STATUS_LIVE, MeetingRecord
from sayink.minutes_prompts import format_timestamp
from sayink.media_import import MEDIA_SOURCE
from sayink.transcript_import import IMPORT_SOURCE
from sayink.ui.meeting_dialogs import _SegmentSpeakerDialog, _SegmentTextDialog
from sayink.ui.reading import fit_reading_width

_STATUS_TEXT = {STATUS_LIVE: "进行中", STATUS_INTERRUPTED: "已中断", STATUS_ENDED: "已结束"}
_HEADER_META_PREFIXES = ("- 时间：", "- 时长：")
_CITATION = re.compile(r"\[(\d{1,2}):(\d{2}):(\d{2})\](?!\()")
_PARAGRAPH_RE = re.compile(r"<p\b[^>]*>.*?</p>", re.DOTALL)
_SPEAKER_ANCHOR_RE = re.compile(
    r"<a href='((?:live-)?speaker:\d+)' title='([^']*)' style='[^']*'><b>([^<]*)</b></a>"
)
# Longer prefixes first so ``edit-segment:`` is not read as ``segment:``.
_SWAPPABLE_ACTION_RE = re.compile(
    r"[ \t]*<a href='((?:live-edit|live-segment|live-details|edit-segment|details-segment|segment):\d+)'"
    r"(?: title='([^']*)')?[^>]*>([^<]*)</a>"
)
_SWAPPABLE_ACTION_PREFIXES = (
    "live-edit:", "live-segment:", "live-details:",
    "edit-segment:", "details-segment:", "segment:",
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
    if record.source == IMPORT_SOURCE:
        status = "导入文字稿"
    elif record.source == MEDIA_SOURCE:
        status = "导入音视频 · 已中断" if record.status == STATUS_INTERRUPTED else "导入音视频"
    else:
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


def _is_swappable_action(href: str) -> bool:
    return bool(href) and href.startswith(_SWAPPABLE_ACTION_PREFIXES)


def _action_anchors(paragraph: str) -> list[tuple[str, str, str]]:
    return [
        (href, html.unescape(title or ""), html.unescape(label))
        for href, title, label in _SWAPPABLE_ACTION_RE.findall(paragraph)
    ]


def _without_swappable_actions(paragraph: str) -> str:
    return _SWAPPABLE_ACTION_RE.sub("", paragraph)


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


def _segment_body_html(text: str, correction: str) -> str:
    """Keep the edit action on every line of a segment.

    Qt turns ``<br>`` into a line break inside one paragraph, and a single
    trailing action then sits only on the last line. The lines above look
    like they cannot be edited.
    """
    parts = [html.escape(part) for part in (text or "").split("\n")] or [""]
    if not correction or len(parts) == 1:
        return "<br>".join(parts) + correction
    return (correction + "<br>").join(parts) + correction


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
    show_speakers: bool = True,
    show_times: bool = True,
    label_unknown: bool = True,
    playable: bool = False,
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
        head = f"<a name='seg{index}'></a>"
        if show_times:
            pos_str = _position_label(line.offset_ms)
            if playable and not live:
                seek_ms = max(0, int(line.offset_ms))
                head += (
                    f"<a href='seek-ms:{seek_ms}' title='点击从 {pos_str} 处回放录音' "
                    f"style='color:{tok.ACCENT_TEXT}; text-decoration:none'>[{pos_str}]</a>"
                )
            else:
                head += f"<span style='color:{tok.TEXT_DIM}'>[{pos_str}]</span>"
        if show_speakers and line.speaker_id <= 0 and label_unknown:
            head += f" <span style='color:{tok.TEXT_DIM}'>说话人待确认</span>"
        elif show_speakers and label and links and not live:
            if line.speaker_id != previous_speaker:
                parts.append(
                    f"<p style='margin:10px 0 2px 0'><a href='{speaker_link}:{line.speaker_id}' "
                    f"title='{speaker_tip}' style='color:{color}; text-decoration:none'>"
                    f"<b>{html.escape(label)}</b></a></p>"
                )
        elif show_speakers and label:
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
                    f"style='color:{tok.TEXT_DIM}; font-size:{tok.TYPE_CAPTION}px; text-decoration:none'>已编辑</a>"
                )
            if correcting:
                correction = f" <a href='{segment_link}:{seq}' style='color:{tok.ACCENT_TEXT}'>更正</a>"
            elif not live and editable:
                correction = (
                    f" <a href='edit-segment:{seq}' title='编辑这一段文字稿' "
                    f"style='color:{tok.TEXT_DIM}; text-decoration:none'>编辑</a>"
                )
            elif live and live_editable:
                correction = (
                    f" <a href='live-edit:{seq}' title='编辑这一段文字稿' "
                    f"style='color:{tok.TEXT_DIM}; text-decoration:none'>编辑</a>"
                )
            if seq in locked_sequences and (live or seq not in corrected_sequences):
                detail_link = "live-details" if live else "details-segment"
                correction += (
                    f" <a href='{detail_link}:{seq}' title='查看姓名来源与归属更正' "
                    f"style='color:{tok.TEXT_DIM}; text-decoration:none'>修改详情</a>"
                )
        text = _segment_body_html(line.text, correction)
        parts.append(f"<p style='margin:0 0 6px 0;{background}'>{head}&nbsp; {text}</p>")
        previous_speaker = line.speaker_id
    return "".join(parts)


class _CompactTextBrowser(QTextBrowser):
    """QTextBrowser whose Tab key moves widget focus instead of cycling inline anchors."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTabChangesFocus(True)

    def focusNextPrevChild(self, next: bool) -> bool:
        if self.tabChangesFocus():
            return QTextEdit.focusNextPrevChild(self, next)
        return super().focusNextPrevChild(next)


class _ReadingView(QTextBrowser):
    """QTextBrowser tuned for long CJK text: looser leading, a heading scale
    below the page title, and text that wraps to the current detail width."""

    copy_document_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTabChangesFocus(True)
        self.setTextInteractionFlags(
            self.textInteractionFlags() | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self._reading_key = None
        self._reading_anchor = 0
        self._defer_layout = False
        self._pending_scroll: tuple[str, int] | None = None
        self._scroll_guard = 0
        self._reading_timer = QTimer(self)
        self._reading_timer.setSingleShot(True)
        self._reading_timer.timeout.connect(self._restore_reading_position)

    def focusNextPrevChild(self, next: bool) -> bool:
        if self.tabChangesFocus():
            return QTextEdit.focusNextPrevChild(self, next)
        return super().focusNextPrevChild(next)

    def restore_reading_position(self, value: int) -> None:
        self._reading_anchor = value
        self.pin_scroll(value)
        self._reading_timer.start(0)

    def pin_scroll(self, value: int | None = None, *, follow_bottom: bool = False) -> None:
        """Remember a scroll target and reapply it as later layout batches grow the document."""
        if follow_bottom:
            self._pending_scroll = ("bottom", 0)
        else:
            self._pending_scroll = ("value", max(0, int(value or 0)))
        self._apply_pending_scroll()

    def _apply_pending_scroll(self) -> None:
        pending = self._pending_scroll
        if pending is None:
            return
        bar = self.verticalScrollBar()
        self._scroll_guard += 1
        try:
            if pending[0] == "bottom":
                bar.setValue(bar.maximum())
            else:
                bar.setValue(min(pending[1], bar.maximum()))
        finally:
            self._scroll_guard -= 1

    def note_user_scroll(self) -> None:
        if self._scroll_guard:
            return
        self._pending_scroll = None

    def _restore_reading_position(self) -> None:
        self._apply_pending_scroll()

    def _set_reading(self, text: str, *, markdown: bool, plain: bool = False) -> None:
        from sayink.ui import design_tokens as tok

        key = (markdown, plain, text, tok.ACCENT_TEXT, tok.TEXT_SEC, tok.TEXT_DIM)
        if self._reading_key == key:
            return
        previous = self._reading_key
        if (
            not plain and not markdown and previous
            and previous[0] is False and previous[1] is False
            and previous[3] == tok.ACCENT_TEXT and previous[4] == tok.TEXT_SEC
            and len(previous) >= 6 and previous[5] == tok.TEXT_DIM
            and (
                self.retarget_speaker_labels(previous[2], text)
                or self.retarget_action_links(previous[2], text)
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

    def retarget_action_links(self, old_html: str, new_html: str) -> bool:
        """Swap edit/correct anchors in place when the spoken text did not change.

        A correction toggle rewrites every row, which is too many paragraphs for
        ``retarget_paragraphs`` and used to rebuild the whole document. Speaker
        headings and multiline rows are kept: only the action anchors move.
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
        planned: list[tuple[int, int, str, str, str]] = []
        changed = False
        block = doc.begin()
        for old_p, new_p in zip(old_paras, new_paras, strict=True):
            if not block.isValid() or _without_swappable_actions(old_p) != _without_swappable_actions(new_p):
                return False
            old_actions = _action_anchors(old_p)
            new_actions = _action_anchors(new_p)
            if len(old_actions) != len(new_actions):
                return False
            fragments = []
            iterator = block.begin()
            while not iterator.atEnd():
                fragment = iterator.fragment()
                href = fragment.charFormat().anchorHref() if fragment.isValid() else ""
                if _is_swappable_action(href):
                    fragments.append(fragment)
                iterator += 1
            if len(fragments) != len(new_actions):
                return False
            for fragment, old_action, new_action in zip(fragments, old_actions, new_actions, strict=True):
                if old_action == new_action:
                    continue
                href, title, label = new_action
                planned.append((fragment.position(), fragment.position() + fragment.length(), href, title, label))
                changed = True
            block = block.next()
        if block.isValid() or not changed:
            return False
        accent = tok.qcolor_from_token(tok.ACCENT_TEXT)
        dim = tok.qcolor_from_token(tok.TEXT_DIM)
        saved_scroll = self.verticalScrollBar().value()
        saved = self._before_retarget()
        cursor = QTextCursor(doc)
        cursor.beginEditBlock()
        try:
            for start, end, href, title, label in reversed(planned):
                cursor.setPosition(start)
                cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
                fmt = cursor.charFormat()
                fmt.setAnchor(True)
                fmt.setAnchorHref(href)
                fmt.setToolTip(title)
                fmt.setFontUnderline(False)
                fmt.setForeground(accent if href.startswith(("segment:", "live-segment:")) else dim)
                cursor.insertText(label, fmt)
        finally:
            cursor.endEditBlock()
        self._after_retarget(saved)
        self._scroll_guard += 1
        try:
            self.verticalScrollBar().setValue(saved_scroll)
        finally:
            self._scroll_guard -= 1
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
                        char.setForeground(tok.qcolor_from_token(tok.TEXT_SEC))
                    cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
                    cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
                    cursor.mergeCharFormat(char)
                self._style_links_with_cursor(cursor, block, tok.ACCENT_TEXT)
                block = block.next()
        finally:
            cursor.endEditBlock()

    @staticmethod
    def _style_links_with_cursor(cursor: QTextCursor, block, color: str) -> None:
        from sayink.ui import design_tokens as tok

        ranges = []
        it = block.begin()
        while not it.atEnd():
            fragment = it.fragment()
            fmt = fragment.charFormat()
            if fmt.isAnchor() and fmt.anchorHref().startswith(("ts:", "seek-ms:")):
                ranges.append((fragment.position(), fragment.position() + fragment.length()))
            it += 1
        if not ranges:
            return
        link = QTextCharFormat()
        link.setForeground(tok.qcolor_from_token(color))
        link.setFontUnderline(False)
        for start, end in ranges:
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

    _INITIAL_LAYOUT_BLOCKS = 400
    _INCREMENTAL_LAYOUT_BATCH = 250

    def __init__(self, parent=None):
        super().__init__(parent)
        self._action_block = None
        self._keyboard_actions = False
        self._next_layout_block_num = 0
        self._repair_scheduled = False
        self._laid_out_viewport_width = 0
        self._layout_timer = QTimer(self)
        self._layout_timer.setSingleShot(True)
        self._layout_timer.timeout.connect(self._layout_next_batch)
        self.setMouseTracking(True)
        self.cursorPositionChanged.connect(self._reveal_cursor_actions)
        self.verticalScrollBar().valueChanged.connect(lambda _value: self.note_user_scroll())

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
        self._layout_timer.stop()
        doc = self.document()
        layout = doc.documentLayout()
        block = doc.begin()
        count = 0
        while block.isValid() and count < self._INITIAL_LAYOUT_BLOCKS:
            layout.blockBoundingRect(block)
            block = block.next()
            count += 1
        if block.isValid():
            self._next_layout_block_num = block.blockNumber()
            self._layout_timer.start(0)
        else:
            self._schedule_blank_repair()
        self._apply_pending_scroll()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        width = self.viewport().width()
        # A scrollbar gutter or window resize invalidates off-screen blocks.
        # The batch cursor must start over, or the middle of a long meeting
        # stays at height 0 forever.
        if (
            self._laid_out_viewport_width
            and width != self._laid_out_viewport_width
            and self.document().blockCount()
        ):
            self._repair_scheduled = False
            self._next_layout_block_num = 0
            self._layout_timer.start(0)
        self._laid_out_viewport_width = width

    def flush_layout(self) -> None:
        """Finish any pending batches before a scroll target is applied."""
        self._layout_timer.stop()
        self._repair_scheduled = False
        doc = self.document()
        layout = doc.documentLayout()
        block = doc.findBlockByNumber(self._next_layout_block_num)
        while block.isValid():
            layout.blockBoundingRect(block)
            block = block.next()
        self._next_layout_block_num = 0
        self._repair_blank_blocks()

    def _schedule_blank_repair(self) -> None:
        self._next_layout_block_num = 0
        self._repair_scheduled = True
        self._layout_timer.start(0)

    def _repair_blank_blocks(self) -> bool:
        doc = self.document()
        layout = doc.documentLayout()
        found = False
        block = doc.begin()
        while block.isValid():
            if block.text().strip():
                block_layout = block.layout()
                if block_layout is None or block_layout.boundingRect().height() <= 0:
                    layout.blockBoundingRect(block)
                    found = True
            block = block.next()
        return found

    def _layout_next_batch(self) -> None:
        if self._repair_scheduled:
            self._repair_scheduled = False
            self._repair_blank_blocks()
            self._apply_pending_scroll()
            return
        doc = self.document()
        layout = doc.documentLayout()
        block = doc.findBlockByNumber(self._next_layout_block_num)
        count = 0
        while block.isValid() and count < self._INCREMENTAL_LAYOUT_BATCH:
            layout.blockBoundingRect(block)
            block = block.next()
            count += 1
        if block.isValid():
            self._next_layout_block_num = block.blockNumber()
            self._layout_timer.start(0)
        else:
            self._schedule_blank_repair()
        self._apply_pending_scroll()

    def scrollToAnchor(self, name: str) -> None:
        if self._layout_timer.isActive():
            self._layout_timer.stop()
            doc = self.document()
            layout = doc.documentLayout()
            block = doc.findBlockByNumber(self._next_layout_block_num)
            while block.isValid():
                layout.blockBoundingRect(block)
                block = block.next()
            self._next_layout_block_num = 0
        super().scrollToAnchor(name)

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
        move this offset. Timestamps (even when playable ``seek-ms:``) and
        spoken text count identically.
        """
        count = 0
        it = block.begin()
        while not it.atEnd():
            fragment = it.fragment()
            href = fragment.charFormat().anchorHref() if fragment.isValid() else ""
            if not fragment.isValid() or (href and not href.startswith("seek-ms:")):
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
            href = fragment.charFormat().anchorHref() if fragment.isValid() else ""
            if not fragment.isValid() or (href and not href.startswith("seek-ms:")):
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
        from sayink.ui import design_tokens as tok

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
            fmt.setForeground(tok.qcolor_from_token(tok.TEXT_DIM))
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
        fmt.setForeground(
            tok.qcolor_from_token(tok.ACCENT_TEXT) if visible else tok.qcolor_from_token(tok.TEXT_DIM)
        )
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
        if self.tabChangesFocus():
            return super().focusNextPrevChild(next)
        self._keyboard_actions = True
        handled = super().focusNextPrevChild(next)
        self._reveal_cursor_actions()
        return handled

    def _primary_block_action_href(self, block) -> str:
        if block is None or not block.isValid():
            return ""
        anchors: list[str] = []
        it = block.begin()
        while not it.atEnd():
            fragment = it.fragment()
            if fragment.isValid():
                href = fragment.charFormat().anchorHref()
                if href:
                    anchors.append(href)
            it += 1
        for prefixes in (
            ("segment:", "live-segment:"),
            ("edit-segment:", "live-edit:"),
            ("edited-segment:", "details-segment:", "live-details:"),
            ("speaker:", "live-speaker:"),
        ):
            for href in anchors:
                if href.startswith(prefixes):
                    return href
        return ""

    def keyPressEvent(self, event) -> None:
        self._keyboard_actions = True
        if (
            event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
            and event.modifiers() in (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.KeypadModifier)
            and not self.textCursor().hasSelection()
        ):
            href = self.textCursor().charFormat().anchorHref() or self._primary_block_action_href(
                self.textCursor().block()
            )
            if href:
                self.anchorClicked.emit(QUrl(href))
                event.accept()
                return
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
                        if href.startswith(skip):
                            if pieces and pieces[-1].endswith(" "):
                                pieces[-1] = pieces[-1][:-1]
                        else:
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


def clear_reading_search(view) -> None:
    """Clear in-view search highlights on a QTextBrowser or QPlainTextEdit."""
    if view is not None and callable(getattr(view, "setExtraSelections", None)):
        view.setExtraSelections([])


def find_in_reading_view(
    view,
    query: str,
    *,
    backward: bool = False,
    step: bool = True,
) -> tuple[int, int]:
    """Find ``query`` inside ``view`` while skipping transcript action anchors.

    Returns ``(active_1based_index, total_matches)`` and highlights all matches.
    """
    if view is None:
        return 0, 0
    needle = (query or "").strip()
    if not needle:
        clear_reading_search(view)
        return 0, 0

    skip_prefixes = _TranscriptView._SEGMENT_LINKS
    needle_cf = needle.casefold()
    n_len = len(needle)
    doc = view.document()
    matches: list[tuple[int, int]] = []

    block = doc.begin()
    while block.isValid():
        buf_chars: list[str] = []
        pos_map: list[tuple[int, int]] = []
        it = block.begin()
        while not it.atEnd():
            fragment = it.fragment()
            if fragment.isValid():
                href = fragment.charFormat().anchorHref()
                if not (href and href.startswith(skip_prefixes)):
                    u16_pos = fragment.position()
                    frag_text = fragment.text()
                    for ch in frag_text:
                        step_u16 = 2 if ord(ch) > 0xFFFF else 1
                        buf_chars.append(ch)
                        pos_map.append((u16_pos, u16_pos + step_u16))
                        u16_pos += step_u16
            it += 1
        if buf_chars:
            hay = "".join(buf_chars)
            hay_cf = hay.casefold()
            # Keep 1-to-1 UTF-16 index safety when casefold preserves length;
            # fall back to re.IGNORECASE when rare ligatures expand length.
            if len(hay_cf) == len(hay) and len(needle_cf) == n_len:
                pos = 0
                while True:
                    found = hay_cf.find(needle_cf, pos)
                    if found < 0:
                        break
                    end_idx = found + n_len - 1
                    if end_idx < len(pos_map):
                        matches.append((pos_map[found][0], pos_map[end_idx][1]))
                    pos = found + 1
            else:
                for m in re.finditer(re.escape(needle), hay, flags=re.IGNORECASE):
                    s_i, e_i = m.start(), m.end() - 1
                    if s_i < len(pos_map) and e_i < len(pos_map) and e_i >= s_i:
                        matches.append((pos_map[s_i][0], pos_map[e_i][1]))
        block = block.next()

    if not matches:
        clear_reading_search(view)
        return 0, 0

    cur = view.textCursor()
    cur_start = min(cur.selectionStart(), cur.selectionEnd()) if cur.hasSelection() else cur.position()

    if backward:
        chosen_idx = len(matches) - 1
        for i in range(len(matches) - 1, -1, -1):
            if matches[i][0] < cur_start:
                chosen_idx = i
                break
    elif step:
        chosen_idx = 0
        for i, (m_start, _m_end) in enumerate(matches):
            if m_start > cur_start or (not cur.hasSelection() and m_start >= cur_start):
                chosen_idx = i
                break
    else:
        chosen_idx = 0
        for i, (m_start, _m_end) in enumerate(matches):
            if m_start >= cur_start:
                chosen_idx = i
                break

    extra: list[QTextEdit.ExtraSelection] = []
    active_bg, passive_bg, text_fg = search_highlight_colors()
    for i, (m_start, m_end) in enumerate(matches):
        sel = QTextEdit.ExtraSelection()
        c = QTextCursor(doc)
        c.setPosition(m_start)
        c.setPosition(m_end, QTextCursor.MoveMode.KeepAnchor)
        sel.cursor = c
        fmt = QTextCharFormat()
        fmt.setBackground(active_bg if i == chosen_idx else passive_bg)
        fmt.setForeground(text_fg)
        sel.format = fmt
        extra.append(sel)
    view.setExtraSelections(extra)

    active_cursor = QTextCursor(doc)
    active_cursor.setPosition(matches[chosen_idx][0])
    active_cursor.setPosition(matches[chosen_idx][1], QTextCursor.MoveMode.KeepAnchor)
    view.setTextCursor(active_cursor)
    view.ensureCursorVisible()
    return chosen_idx + 1, len(matches)


def search_highlight_colors() -> tuple[QColor, QColor, QColor]:
    """Return ``(active_bg, passive_bg, text_fg)`` with strong contrast in Light & Dark."""
    from sayink.ui import design_tokens as tok

    is_dark = getattr(tok, "ACTIVE_THEME", "light") == "dark"
    active_bg = QColor("#1D4ED8" if is_dark else "#93C5FD")
    passive_bg = QColor("#92400E" if is_dark else "#FDE68A")
    text_fg = tok.qcolor_from_token(tok.TEXT)
    return active_bg, passive_bg, text_fg


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
    if chosen.startswith("Word") or lower.endswith(".docx"):
        return "docx"
    if chosen.startswith("SRT") or lower.endswith(".srt"):
        return "srt"
    if chosen.startswith("纯文本") or lower.endswith(".txt"):
        return "txt"
    return "md"


_KNOWN_MEETING_EXPORT_SUFFIXES = {".md", ".docx", ".txt", ".srt", ".json", ".csv"}


def _with_export_suffix(path: str, kind: str) -> str:
    suffix = {"md": ".md", "docx": ".docx", "txt": ".txt", "srt": ".srt"}[kind]
    target = Path(path)
    if target.suffix.lower() == suffix:
        return str(target)
    if target.suffix.lower() in _KNOWN_MEETING_EXPORT_SUFFIXES:
        return str(target.with_suffix(suffix))
    return f"{target}{suffix}"


__all__ = [
    "_CompactTextBrowser",
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
    "clear_reading_search",
    "find_in_reading_view",
    "link_citations",
    "notes_for_display",
    "search_highlight_colors",
    "speaker_color",
]

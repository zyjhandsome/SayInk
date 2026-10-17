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

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
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
from sayink.meeting_store import STATUS_ENDED, STATUS_INTERRUPTED, STATUS_LIVE, MeetingRecord
from sayink.minutes_prompts import describe_template, format_timestamp, get_template, list_templates, transcript_lines
from sayink.speech_recognizer import DEFAULT_MODEL_ID, MODEL_REGISTRY, is_model_downloaded
from sayink.ui.clipboard import copy_text
from sayink.ui.history_window import _ElidedLabel, _SessionList, session_list_item_css

log = logging.getLogger("SayInk")

PAGE_BROWSE = 0
PAGE_START = 1
PAGE_LIVE = 2

_SOURCE_LABELS = (("mixed", "混合音频（麦克风 + 电脑播放）"), ("microphone", "麦克风"), ("system", "电脑播放"))
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


def _meeting_row_meta(record: MeetingRecord) -> str:
    status = _STATUS_TEXT.get(record.status, "")
    duration = _duration_label(record.duration_ms) if record.ended_at else ""
    stamp = time.localtime(record.started_at / 1000)
    today = time.localtime()
    when = time.strftime("今天 %H:%M" if stamp[:3] == today[:3] else "%m-%d %H:%M" if stamp.tm_year == today.tm_year else "%Y-%m-%d %H:%M", stamp)
    bits = [status, when] if status else [when]
    if duration:
        bits.append(duration)
    return " · ".join(bits)


def _meeting_row_text(record: MeetingRecord) -> str:
    return f"{record.display_title}\n{_meeting_row_meta(record)}"


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
    below the page title, and a capped line length on wide windows."""

    MAX_TEXT_WIDTH = 720
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

    def _set_reading(self, text: str, *, markdown: bool) -> None:
        from sayink.ui import design_tokens as tok

        key = (markdown, text, tok.ACCENT_TEXT, tok.TEXT_SEC)
        if self._reading_key == key:
            return
        value = self.verticalScrollBar().value()
        self._reading_key = key
        if markdown:
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
            fmt.setLineHeight(160.0, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
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
        frame = self.document().rootFrame()
        fmt = frame.frameFormat()
        extra = max(0, self.viewport().width() - self.MAX_TEXT_WIDTH)
        if fmt.rightMargin() != extra:
            fmt.setRightMargin(extra)
            frame.setFrameFormat(fmt)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_text_width()


class _SegmentSpeakerDialog(QDialog):
    def __init__(self, segment, choices: list[tuple[int, str]], parent=None):
        super().__init__(parent)
        from sayink.ui import design_tokens as tok
        from sayink.ui import settings_styles as ss

        self.setWindowTitle("修改本段说话人")
        self.setObjectName("meetingSpeakerCorrection")
        self.setFixedWidth(420)
        self.setStyleSheet(ss.WINDOW_CSS + f"""
            QDialog#meetingSpeakerCorrection {{ background: {tok.SURFACE}; color: {tok.TEXT}; }}
            QLabel {{ background: transparent; color: {tok.TEXT}; font-size: {tok.TYPE_BODY_SM}px; }}
            QComboBox {{ background: {tok.SURFACE_PEARL}; color: {tok.TEXT};
                         border: 1px solid {tok.CONTROL_BORDER}; border-radius: {tok.RADIUS_SM}px;
                         min-height: 30px; padding: 4px 8px; font-size: {tok.TYPE_BODY_SM}px; }}
            QComboBox QAbstractItemView {{ background: {tok.SURFACE}; color: {tok.TEXT};
                                          selection-background-color: {tok.ACCENT_SOFT}; }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(12)
        preview = QLabel((segment.polished_text or segment.raw_text)[:220])
        preview.setTextFormat(Qt.TextFormat.PlainText)
        preview.setWordWrap(True)
        lay.addWidget(preview)
        self.speaker_combo = QComboBox()
        self.speaker_combo.setAccessibleName("这一段的发言人")
        for speaker_id, label in choices:
            self.speaker_combo.addItem(label, speaker_id)
        self.speaker_combo.setCurrentIndex(max(0, self.speaker_combo.findData(segment.speaker_id)))
        lay.addWidget(self.speaker_combo)
        hint = QLabel("仅更改这一段的归属。现有纪要中的发言人需要核对，或重新生成纪要。")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setStyleSheet(ss.BTN_GHOST_SM)
        cancel.clicked.connect(self.reject)
        save = QPushButton("应用更正")
        save.setStyleSheet(ss.BTN_PRIMARY)
        save.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        lay.addLayout(buttons)

    @property
    def speaker_id(self) -> int:
        return int(self.speaker_combo.currentData())


class MeetingPage(QWidget):
    start_requested = pyqtSignal(object)   # MeetingOptions
    end_requested = pyqtSignal()
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
        self._pending_templates: dict[str, str] = {}
        self._citation_return: tuple[str, int] | None = None
        self._correcting_speakers = False
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(400)
        self._refresh_timer.timeout.connect(self._refresh_now)
        self._pending_refresh_id = ""
        self._setup_ui()
        self._ctrl.state_changed.connect(self._on_state_changed)
        self._ctrl.meeting_changed.connect(self._on_meeting_changed)
        self._ctrl.clock_tick.connect(self._live_clock.setText)
        self._ctrl.generation_progress.connect(self._on_generation_progress)
        self._ctrl.generation_finished.connect(self._on_generation_finished)
        self._ctrl.generation_failed.connect(self._on_generation_failed)
        self.reapply_theme()
        self.refresh()
        if self._ctrl.is_active:
            self._enter_live()

    # ── construction ──────────────────────────────────────────────

    def _setup_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(14)

        top = QHBoxLayout()
        top.setSpacing(12)
        self._title_label = QLabel("会议")
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
        left.setMinimumWidth(220)
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
        self._empty_label = QLabel("还没有会议记录。\n点击右上角「开始会议」，或从托盘菜单开始。")
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setWordWrap(True)
        left_lay.addWidget(self._empty_label, 0)
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
        self._delete_action = self._more_menu.addAction("删除会议…")
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
        right_lay.addWidget(self._detail_meta)
        right_lay.addWidget(self._notes_state)

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
        tabs_lay.setContentsMargins(3, 3, 3, 3)
        tabs_lay.setSpacing(2)
        self._tab_group = QButtonGroup(self)
        self._tab_group.setExclusive(True)
        self._tab_notes = QPushButton("纪要")
        self._tab_transcript = QPushButton("文字稿")
        for index, button in enumerate((self._tab_notes, self._tab_transcript)):
            button.setCheckable(True)
            button.setObjectName("meetingTab")
            self._tab_group.addButton(button, index)
            tabs_lay.addWidget(button)
        self._tab_notes.setChecked(True)
        self._tab_group.idClicked.connect(self._switch_tab)
        tab_row.addWidget(self._tab_bar, 0)
        tab_row.addWidget(self._copy_btn)
        tab_row.addStretch(1)
        tab_row.addWidget(self._export_btn)
        right_lay.addLayout(tab_row)

        self._tab_tools = QStackedWidget()
        notes_tools = QWidget()
        notes_lay = QHBoxLayout(notes_tools)
        notes_lay.setContentsMargins(0, 0, 0, 0)
        notes_lay.setSpacing(8)
        self._template_label = QLabel("模板")
        notes_lay.addWidget(self._template_label)
        self._template_combo = QComboBox()
        self._template_combo.setAccessibleName("会议模板")
        self._template_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        for template in list_templates(self._custom_templates()):
            self._template_combo.addItem(template.name, template.id)
            self._template_combo.setItemData(
                self._template_combo.count() - 1, describe_template(template), Qt.ItemDataRole.ToolTipRole
            )
        self._template_combo.currentIndexChanged.connect(self._on_template_changed)
        self._template_combo.setMinimumWidth(90)
        self._template_combo.setMaximumWidth(150)
        self._template_combo.setToolTip(str(self._template_combo.itemData(0, Qt.ItemDataRole.ToolTipRole) or ""))
        self._template_label.setBuddy(self._template_combo)
        notes_lay.addWidget(self._template_combo)
        self._generate_btn = QPushButton("生成纪要")
        self._generate_btn.setAccessibleName("生成纪要")
        self._generate_btn.clicked.connect(self._generate)
        notes_lay.addWidget(self._generate_btn)
        self._cancel_btn = QPushButton("取消")
        self._cancel_btn.clicked.connect(self._ctrl.cancel_generation)
        self._cancel_btn.hide()
        notes_lay.addWidget(self._cancel_btn)
        self._edit_btn = QPushButton("编辑")
        self._edit_btn.setToolTip("直接修改纪要文本")
        self._edit_btn.clicked.connect(self._toggle_edit)
        notes_lay.addWidget(self._edit_btn)
        notes_lay.addStretch(1)
        self._tab_tools.addWidget(notes_tools)

        transcript_tools = QWidget()
        transcript_lay = QHBoxLayout(transcript_tools)
        transcript_lay.setContentsMargins(0, 0, 0, 0)
        transcript_lay.setSpacing(8)
        transcript_lay.addStretch(1)
        self._speaker_btn = QPushButton("命名说话人")
        self._speaker_btn.setAccessibleName("为整组说话人命名")
        self._speaker_btn.setToolTip("为同一组发言设置显示名称；名称来自手动设置，不表示已验证身份")
        self._speaker_menu = QMenu(self._speaker_btn)
        self._speaker_btn.setMenu(self._speaker_menu)
        transcript_lay.addWidget(self._speaker_btn)
        self._correct_speaker_btn = QPushButton("纠正发言人")
        self._correct_speaker_btn.setCheckable(True)
        self._correct_speaker_btn.toggled.connect(self._toggle_speaker_correction)
        transcript_lay.addWidget(self._correct_speaker_btn)
        self._return_notes_btn = QPushButton("返回纪要")
        self._return_notes_btn.setAccessibleName("返回纪要引用处")
        self._return_notes_btn.setToolTip("回到刚才阅读的纪要引用位置")
        self._return_notes_btn.clicked.connect(self._return_to_notes)
        self._return_notes_btn.hide()
        transcript_lay.addWidget(self._return_notes_btn)
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
        self._notes_stack.addWidget(self._notes_view)
        self._notes_stack.addWidget(self._notes_edit)
        self._content_stack.addWidget(self._notes_stack)
        self._transcript_view = _ReadingView()
        self._transcript_view.setAccessibleName("会议文字稿")
        self._transcript_view.setPlaceholderText("文字稿为空")
        self._transcript_view.setOpenLinks(False)
        self._transcript_view.copy_document_requested.connect(self._copy_current)
        self._transcript_view.anchorClicked.connect(self._on_transcript_link)
        self._content_stack.addWidget(self._transcript_view)
        right_lay.addWidget(self._content_stack, 1)

        self._detail_pane = right
        right.setMinimumWidth(360)
        self._splitter.addWidget(right)
        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 3)
        self._splitter.setSizes([240, 560])
        return self._splitter

    def _build_start_card(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        self._card_title = QLabel("开始会议")
        lay.addWidget(self._card_title)
        self._card_hint = QLabel(
            "填写的信息会写进纪要并帮助模型理解上下文；全部可留空。"
            "不保存音频，只保存文字。"
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
        for key, label in _SOURCE_LABELS:
            self._opt_source.addItem(label, key)
        row.addWidget(self._opt_source, 2)
        self._opt_model = QComboBox()
        self._opt_model.setAccessibleName("会议识别模型")
        row.addWidget(self._opt_model, 2)
        self._opt_language = QComboBox()
        self._opt_language.setAccessibleName("会议语言")
        for key, label in _LANGUAGE_LABELS:
            self._opt_language.addItem(label, key)
        row.addWidget(self._opt_language, 1)
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
        lay.addLayout(actions)
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

        self._live_split = QSplitter(Qt.Orientation.Horizontal)
        self._live_split.setChildrenCollapsible(False)
        self._live_transcript = QTextBrowser()
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
        self._marks_view.setPlaceholderText("重要时刻按一下「标记此刻」，纪要会优先覆盖这些点。")
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
        side.setMinimumWidth(240)
        self._live_split.addWidget(side)
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
        self._resume_btn = QPushButton("继续采集")
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
        self._title_label.setStyleSheet(title_css)
        self._card_title.setStyleSheet(title_css)
        self._live_title.setStyleSheet(
            f"font-size: {tok.TYPE_TITLE}px; font-weight: 700; color: {tok.TEXT}; background: transparent;"
        )
        self._live_clock.setStyleSheet(
            f"font-size: {tok.TYPE_TITLE_LG}px; font-weight: 700; color: {tok.ACCENT}; background: transparent;"
        )
        dim = f"color: {tok.TEXT_SEC}; font-size: {tok.TYPE_FOOTNOTE}px; background: transparent;"
        for label in (self._list_summary, self._detail_meta, self._card_hint, self._live_caps,
                      self._card_notes, self._empty_label, self._template_label, self._notes_state, self._content_hint):
            label.setStyleSheet(dim)
        for label in (self._marks_title, self._notes_title):
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
                background: {tok.SURFACE_PEARL}; color: {tok.TEXT};
                border: 1px solid {tok.HAIRLINE}; border-radius: {tok.RADIUS_MD}px;
                padding: 6px 12px; min-height: 32px; font-size: {tok.TYPE_BODY_SM}px;
            }}
            QLineEdit:focus {{ border: 2px solid {tok.ACCENT}; padding: 5px 11px; }}
        """
        for edit in (self._search_edit, self._opt_title, self._opt_participants, self._mark_note):
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
        self._tab_bar.setStyleSheet(f"""
            QWidget#meetingTabs {{ background: {tok.SURFACE_PEARL}; border: 1px solid {tok.HAIRLINE};
                                   border-radius: {tok.RADIUS_MD}px; }}
            QPushButton#meetingTab {{ background: transparent; color: {tok.TEXT_SEC}; border: none;
                                      border-radius: {tok.RADIUS_SM}px; padding: 0px 14px;
                                      min-height: {tok.CONTROL_BTN_SM_HEIGHT - 8}px;
                                      max-height: {tok.CONTROL_BTN_SM_HEIGHT - 8}px;
                                      font-size: {tok.TYPE_BODY_SM}px; }}
            QPushButton#meetingTab:hover:!checked {{ color: {tok.TEXT}; background: {tok.ROW_HOVER}; }}
            QPushButton#meetingTab:checked {{ background: {tok.SURFACE}; color: {tok.TEXT}; font-weight: 700; }}
            QPushButton#meetingTab:focus {{ border: {tok.FOCUS_RING}; }}
        """)
        self._tab_bar.setFixedHeight(tok.CONTROL_BTN_SM_HEIGHT)
        self._template_combo.setFixedHeight(tok.CONTROL_BTN_SM_HEIGHT)
        self._more_btn.setFixedWidth(tok.CONTROL_BTN_SM_HEIGHT + 8)
        from sayink.ui.tray_icon import _menu_font, _menu_stylesheet

        for menu in (self._more_menu, self._speaker_menu):
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
                     self._opt_agenda):
            view.setStyleSheet(text_css)
        # Reading a saved meeting matches 历史: text sits on the page, and only
        # the editor gets a field border, so "can I type here" is visible.
        reading_css = f"""
            QTextBrowser {{
                background: transparent; color: {tok.TEXT}; border: none; padding: 4px 0;
                font-size: {tok.TYPE_BODY_SM}px;
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
        def combo_css(box: str) -> str:
            return f"""
                QComboBox {{ background: {tok.SURFACE_PEARL}; color: {tok.TEXT};
                             border: 1px solid {tok.HAIRLINE}; border-radius: {tok.RADIUS_MD}px;
                             {box} font-size: {tok.TYPE_BODY_SM}px; }}
                QComboBox:focus {{ border: 2px solid {tok.ACCENT}; }}
                QComboBox QAbstractItemView {{ background: {tok.SURFACE}; color: {tok.TEXT};
                                               selection-background-color: {tok.ACCENT_SOFT}; }}
            """

        for combo in (self._opt_source, self._opt_model, self._opt_language):
            combo.setStyleSheet(combo_css("padding: 4px 10px; min-height: 30px;"))
        self._template_combo.setStyleSheet(combo_css("padding: 0px 10px;"))
        for btn in (self._start_btn, self._card_start, self._mark_btn, self._resume_btn):
            btn.setStyleSheet(ss.BTN_ACCENT_SM)
        for btn in (self._generate_btn, self._edit_btn, self._copy_btn, self._export_btn, self._card_cancel,
                    self._cancel_btn, self._correct_speaker_btn, self._return_notes_btn):
            btn.setStyleSheet(ss.BTN_GHOST_SM)
        no_indicator = "QPushButton::menu-indicator { image: none; width: 0px; }"
        self._more_btn.setStyleSheet(ss.BTN_GHOST_SM + no_indicator)
        self._speaker_btn.setStyleSheet(ss.BTN_GHOST_SM + no_indicator)
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

    def refresh(self) -> None:
        if self._store.disabled:
            self._list_summary.setText("历史数据库不可用，会议记录无法保存")
            return
        records = self._store.search_meetings(self._search_query) if self._search_query else self._store.list_meetings()
        self._records = {r.id: r for r in records}
        keep = self._detail_id
        self._list.blockSignals(True)
        self._list.clear()
        for record in records:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, record.id)
            item.setData(Qt.ItemDataRole.AccessibleTextRole, _meeting_row_text(record))
            item.setToolTip(record.display_title)
            row = self._build_list_row(record)
            self._list.addItem(item)
            self._list.setItemWidget(item, row)
            row.ensurePolished()
            for label in row.findChildren(QLabel):
                label.ensurePolished()
            item.setSizeHint(row.sizeHint())
            if record.id == keep:
                item.setSelected(True)
                self._list.setCurrentItem(item)
        self._list.blockSignals(False)
        count = len(records)
        self._list_summary.setText(f"共 {count} 场会议" if count else "")
        self._empty_label.setVisible(count == 0 and not self._search_query)
        if keep and keep in self._records:
            self._render_detail()
        elif records:
            self._list.setCurrentRow(0)
        elif not records:
            if not self._save_notes_draft():
                return
            self._detail_id = ""
            self._clear_detail()

    def _build_list_row(self, record: MeetingRecord) -> QWidget:
        row = QWidget()
        row.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        lay = QVBoxLayout(row)
        lay.setContentsMargins(13, 8, 10, 8)
        lay.setSpacing(2)
        title = _ElidedLabel(record.display_title)
        title.setObjectName("meetingRowTitle")
        lay.addWidget(title)
        meta = _ElidedLabel(_meeting_row_meta(record))
        meta.setObjectName("meetingRowMeta")
        lay.addWidget(meta)
        return row

    def _perform_search(self) -> None:
        self._search_query = self._search_edit.text().strip()
        self.refresh()

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
            self._transcript_highlight = -1
            self._citation_return = None
            self._correcting_speakers = False
            self._status_label.hide()
            self._detail_id = meeting_id
            self._notes_view.restore_reading_position(0)
            self._transcript_view.restore_reading_position(0)
            self._render_detail()

    def show_meeting(self, meeting_id: str) -> None:
        """Open a meeting in the browse view (after it ended, or from a notice)."""
        if not self._save_notes_draft():
            return
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
        self._save_notes_draft()
        self._detail_title.clear()
        self._detail_meta.clear()
        self._notes_state.clear()
        self._content_hint.hide()
        self._return_notes_btn.hide()
        self._notes_view.clear()
        self._notes_edit.clear()
        self._transcript_view.clear()
        self._speaker_menu.clear()
        self._status_label.hide()
        for widget in (self._generate_btn, self._edit_btn, self._copy_btn, self._export_btn,
                       self._more_btn, self._speaker_btn, self._correct_speaker_btn, self._template_combo, self._detail_title):
            widget.setEnabled(False)
        from sayink.ui import settings_styles as ss
        self._start_btn.setStyleSheet(ss.BTN_ACCENT_SM)

    def _render_detail(self) -> None:
        record = self._store.get_meeting(self._detail_id) if self._detail_id else None
        if record is None:
            self._clear_detail()
            return
        for widget in (self._generate_btn, self._edit_btn, self._copy_btn, self._export_btn,
                       self._more_btn, self._speaker_btn, self._template_combo, self._detail_title):
            widget.setEnabled(True)
        if not self._detail_title.hasFocus():
            self._detail_title.setText(record.title)
            self._detail_title.setPlaceholderText(record.display_title)
        meta = [_format_dt(record.started_at)]
        if record.ended_at:
            meta.append(_duration_label(record.duration_ms))
        if record.status in _STATUS_TEXT:
            meta.append(_STATUS_TEXT[record.status])
        self._detail_meta.setText(" · ".join(meta))

        notes = self._store.get_notes(self._detail_id)
        generating = self._ctrl.generating_for == self._detail_id
        from sayink.ui import settings_styles as ss

        has_notes = notes is not None and bool(notes.markdown)
        self._start_btn.setStyleSheet(ss.BTN_GHOST_SM)
        configured = minutes_settings_complete(self._config)
        if not configured:
            self._generate_btn.setText("去配置")
            self._generate_btn.setAccessibleName("去配置纪要模型")
            self._generate_btn.setToolTip("打开 设置 → AI 服务，填写接口地址、密钥与模型")
        else:
            self._generate_btn.setText("重新生成" if has_notes else "生成纪要")
            self._generate_btn.setAccessibleName(self._generate_btn.text())
            self._generate_btn.setToolTip("")
        self._generate_btn.setStyleSheet(ss.BTN_GHOST_SM if has_notes and configured else ss.BTN_ACCENT_SM)
        self._generate_btn.setEnabled(not generating and record.status != STATUS_LIVE)
        self._restore_action.setEnabled(not generating and notes is not None and bool(notes.previous_markdown))
        self._cancel_btn.setVisible(generating)
        default_template = notes.template_id if notes is not None and notes.template_id else self._config.get("meeting.template_id", "general")
        template_id = self._pending_templates.get(self._detail_id, default_template)
        index = self._template_combo.findData(template_id)
        self._template_combo.blockSignals(True)
        if index >= 0:
            self._template_combo.setCurrentIndex(index)
        self._template_combo.blockSignals(False)
        self._template_combo.setEnabled(not generating)
        self._sync_result_actions(notes)
        markdown = notes.markdown if notes is not None else ""
        if self._editing_notes:
            self._notes_stack.setCurrentWidget(self._notes_edit)
            self._edit_btn.setText("保存修改")
        else:
            self._notes_stack.setCurrentWidget(self._notes_view)
            self._edit_btn.setText("编辑")
            if markdown and notes is not None and self._is_refinement(notes.template_id):
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
                        f"还没有{self._result_label()}。需要一个大模型接口：点击「去配置」填写，不需要开启文字润色。"
                    )
                else:
                    self._notes_view.setPlaceholderText(
                        "融合多人发言，保留细节与个人语气，整理成通顺的书面文章。点击「整理原文」开始。"
                        if self._is_refinement() else "还没有纪要。选择模板后点击「生成纪要」。"
                    )
        self._edit_btn.setEnabled(not generating and (bool(markdown) or self._editing_notes))
        self._sync_notes_state(notes)

        segments = self._store.get_segments(self._detail_id)
        names = self._ctrl.speaker_names(self._detail_id)
        lines = transcript_lines(segments, record.started_at)
        self._transcript_offsets = [line.offset_ms for line in lines]
        self._transcript_sequences = [s.seq for s in segments if (s.polished_text or s.raw_text).strip()]
        can_correct = record.status != STATUS_LIVE and not generating and bool(lines)
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
        if self._editing_notes:
            state = "正在编辑 · 尚未保存"
        elif notes is None or not notes.markdown:
            state = "尚未整理原文" if self._is_refinement() else "尚未生成纪要"
        else:
            state = "已手动编辑" if notes.user_edited else "AI 生成"
            template_index = self._template_combo.findData(notes.template_id)
            if template_index >= 0:
                state += " · " + self._template_combo.itemText(template_index)
        if notes is not None and notes.transcript_changed:
            state += f" · 文字稿已更正，{self._result_label()}发言人需核对"
        elif notes is not None and "（待核对）" in notes.markdown:
            state += " · 含待核对内容"
        self._notes_state.setText(self._result_label() + " · " + state)
        self._notes_state.setToolTip(
            "保留细节与语气的 AI 整理稿，可切换到文字稿核对；手动编辑不等于内容已经核实。"
            if self._result_label() == "精整正文" else
            "纪要根据文字稿生成；点击引用可核对来源。手动编辑不等于内容已经核实。"
        )

    def _sync_content_hint(self) -> None:
        transcript = self._content_stack.currentIndex() == 1
        self._return_notes_btn.setVisible(transcript and bool(self._citation_return and self._citation_return[0] == self._detail_id))
        if transcript:
            hint = ("点击发言旁的「更正」修改这一段的归属。" if self._correcting_speakers else
                    "说话人由系统自动区分；姓名来自手动命名。")
        elif self._editing_notes:
            hint = "复制与导出使用当前草稿。切换会议或离开本页时自动保存。"
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

        transcript = self._content_stack.currentIndex() == 1
        label = "复制文字稿" if transcript else ("复制草稿" if self._editing_notes else "复制" + self._result_label())
        self._copy_btn.setText(label)
        self._copy_btn.setAccessibleName(label)
        shortcut = "" if self._editing_notes and not transcript else " · Ctrl+C"
        self._copy_btn.setToolTip(label + ("" if transcript or self._result_label() == "精整正文" else " Markdown") + shortcut)
        has_notes = bool(self._current_notes_text().strip())
        has_transcript = bool(self._detail_id and self._transcript_offsets)
        self._copy_btn.setEnabled(has_transcript if transcript else has_notes)
        self._copy_btn.setStyleSheet(ss.BTN_PRIMARY)
        self._export_actions["notes"].setEnabled(has_notes)
        self._export_actions["transcript"].setEnabled(has_transcript)
        self._export_actions["all"].setEnabled(bool(self._detail_id))

    def _transcript_html(self, lines, names: dict[int, str], *, links: bool = False, highlight: int = -1) -> str:
        from sayink.ui import design_tokens as tok

        parts = []
        previous_speaker = None
        for index, line in enumerate(lines):
            label = names.get(line.speaker_id) or f"说话人{line.speaker_id}" if line.speaker_id > 0 else ""
            color = speaker_color(line.speaker_id)
            head = f"<a name='seg{index}'></a><span style='color:{tok.TEXT_DIM}'>[{_position_label(line.offset_ms)}]</span>"
            if label and links:
                if line.speaker_id != previous_speaker:
                    suffix = " · 手动命名" if line.speaker_id in names else " · 自动区分"
                    parts.append(
                        f"<p style='margin:10px 0 2px 0'><a href='speaker:{line.speaker_id}' "
                        f"title='为这一组发言命名' style='color:{color}; text-decoration:none'>"
                        f"<b>{html.escape(label)}</b></a>"
                        f"<span style='color:{tok.TEXT_DIM}; font-size:12px'>{suffix}</span></p>"
                    )
            elif label:
                head += f" <b style='color:{color}'>{html.escape(label)}</b>"
            background = f" background-color:{tok.HIGHLIGHT_BG};" if index == highlight else ""
            correction = ""
            if links and self._correcting_speakers and index < len(self._transcript_sequences):
                correction = (f" <a href='segment:{self._transcript_sequences[index]}' "
                              f"style='color:{tok.ACCENT_TEXT}'>更正</a>")
            parts.append(f"<p style='margin:0 0 6px 0;{background}'>{head}&nbsp; {html.escape(line.text)}{correction}</p>")
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

    def _set_status(self, text: str) -> None:
        self._status_label.setText(text)
        self._status_label.setVisible(bool(text))

    # ── detail actions ────────────────────────────────────────────

    def _save_title(self) -> None:
        if not self._detail_id:
            return
        record = self._records.get(self._detail_id) or self._store.get_meeting(self._detail_id)
        title = self._detail_title.text().strip()
        if record is None or title == record.title:
            return
        self._ctrl.update_meeting(self._detail_id, title=title)
        self.refresh()

    def _generate(self) -> None:
        if not self._detail_id:
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
        self._ctrl.restore_previous_notes(self._detail_id)
        self._switch_tab(0)
        self._render_detail()
        self._set_status(f"已恢复上一版{self._result_label()}；再次恢复可切回刚才的版本。")

    def _switch_tab(self, index: int) -> None:
        self._tab_group.button(index).setChecked(True)
        self._content_stack.setCurrentIndex(index)
        self._tab_tools.setCurrentIndex(index)
        self._sync_copy_action()
        self._sync_content_hint()

    def _on_generation_progress(self, meeting_id: str, text: str) -> None:
        if meeting_id == self._detail_id:
            self._set_status(text)
            self._render_detail()

    def _on_generation_finished(self, meeting_id: str) -> None:
        self._pending_templates.pop(meeting_id, None)
        if meeting_id == self._detail_id:
            self._editing_notes = False
            self._switch_tab(0)
            self._render_detail()
            self._set_status(
                "原文已精整，可切换到文字稿核对重要内容。" if self._result_label() == "精整正文" else
                "纪要已生成，可点击时间戳核对文字稿。"
            )
        self.refresh()

    def _on_generation_failed(self, meeting_id: str, message: str) -> None:
        if meeting_id == self._detail_id:
            self._render_detail()
            self._set_status(f"{'原文整理' if self._is_refinement() else '纪要生成'}失败：{message}")

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
            if self._save_notes_draft() and editing:
                self._render_detail()
        super().hideEvent(event)

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
        elif self._detail_id:
            text = self._ctrl.transcript_text(self._detail_id)
            if text:
                self._set_status("已复制文字稿" if copy_text(text) else "复制失败，请重试")

    def _export(self, scope: str = "all") -> None:
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
                text = self._current_notes_text().rstrip() + "\n\n## 文字稿\n\n" + self._ctrl.transcript_text(record.id) + "\n"
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
        box.setInformativeText("文字稿、标记与纪要会一起删除，无法撤销。")
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

    def _toggle_speaker_correction(self, enabled: bool) -> None:
        self._correcting_speakers = enabled
        self._render_detail()

    def _prompt_segment_speaker(self, seq: int) -> None:
        record = self._store.get_meeting(self._detail_id) if self._detail_id else None
        if record is None or record.status == STATUS_LIVE or self._ctrl.generating_for == record.id:
            return
        segments = self._store.get_segments(record.id)
        segment = next((s for s in segments if s.seq == seq), None)
        if segment is None:
            return
        names = self._ctrl.speaker_names(record.id)
        ids = sorted({int(s.speaker_id or 1) for s in segments})
        choices = [(sid, f"{names[sid]} · 说话人{sid}" if sid in names else f"说话人{sid}") for sid in ids]
        new_id = max(ids, default=0) + 1
        choices.append((new_id, f"新增说话人{new_id}"))
        dialog = _SegmentSpeakerDialog(segment, choices, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            if self._ctrl.correct_segment_speaker(record.id, seq, dialog.speaker_id):
                self._render_detail()
                self._set_status("已更正这一段的发言人；请核对纪要中的相关归属。")
            else:
                self._set_status("更正未保存，请重试。")

    def _prompt_rename_speaker(self, speaker_id: int) -> None:
        if not self._detail_id:
            return
        if self._ctrl.generating_for == self._detail_id:
            self._set_status("纪要生成中，请在完成后为说话人命名。")
            return
        current = self._ctrl.speaker_names(self._detail_id).get(speaker_id, "")
        name, ok = QInputDialog.getText(
            self, "为整组说话人命名",
            f"「说话人{speaker_id}」的显示名称\n会更新这一组发言的名称和纪要中的称呼；留空恢复编号。",
            QLineEdit.EchoMode.Normal, current,
        )
        if ok:
            self._rename_speaker(speaker_id, name)

    def _rename_speaker(self, speaker_id: int, name: str) -> None:
        if not self._detail_id:
            return
        if not self._save_notes_draft():
            return
        name = name.strip()
        self._ctrl.rename_speaker(self._detail_id, speaker_id, name)
        self._render_detail()
        self._set_status(f"已将说话人{speaker_id}改为「{name}」" if name else f"已恢复「说话人{speaker_id}」")

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
        index = self._opt_source.findData(defaults.input_source)
        self._opt_source.setCurrentIndex(max(0, index))
        self._opt_model.blockSignals(True)
        self._opt_model.clear()
        self._opt_model.addItem("与听写相同", "")
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

    def _update_card_notes(self) -> None:
        options = self._options_from_card()
        dictation = str(self._config.get("stt.model_id", DEFAULT_MODEL_ID) or DEFAULT_MODEL_ID)
        caps = resolve_capabilities(self._config, options, dictation_model_id=dictation)
        notes = list(caps.notes)
        if not minutes_settings_complete(self._config):
            notes.append("尚未配置纪要模型（设置 → AI 服务）；会议结束后仍可先保存文字稿。")
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
        self._card_dl_btn.setToolTip(f"{what}：下载完成后本场会议即可区分说话人、使用 Silero 断句")
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

    def _on_aux_progress(self, pct: int) -> None:
        for button in (self._card_dl_btn, self._live_dl_btn):
            button.setText(f"下载中 {int(pct)}%")

    def _on_aux_finished(self, ok: bool, message: str) -> None:
        if ok:
            self.aux_models_changed.emit()
            if self._stack.currentIndex() == PAGE_START:
                self._update_card_notes()
                self._card_notes.setText("· 辅助模型已下载，本场会议将区分说话人并使用 Silero 断句。")
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
            input_source=str(self._opt_source.currentData() or defaults.input_source),
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
        # Remember the picks that are preferences, not per-meeting facts.
        self._config.set("meeting.input_source", options.input_source)
        self._config.set("meeting.model_id", options.model_id)
        self._config.set("stt.language", options.language)
        self.start_requested.emit(options)

    # ── live view ─────────────────────────────────────────────────

    def _enter_live(self) -> None:
        record = self._ctrl.current
        self._live_title.setText(record.display_title if record is not None else "会议进行中")
        self._live_clock.setText(self._ctrl.elapsed_text())
        caps = self._ctrl.capabilities
        if caps is not None:
            bits = [f"模型 {caps.model_id}", "Silero 断句" if caps.vad == "silero" else "能量断句"]
            bits.append("说话人模型" if caps.speaker_model_available else "说话人（粗略）")
            if caps.hotwords and caps.hotwords_supported:
                bits.append("热词已启用")
            self._live_caps.setText(" · ".join(bits))
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
            self._end_btn.setEnabled(True)
        elif state is MeetingState.DRAINING:
            self._live_status.setText("正在整理文字稿，请稍候…")
            self._live_status.show()
            self._resume_btn.hide()
            self._end_btn.setEnabled(False)
            self._mark_btn.setEnabled(False)
        else:
            self._live_status.hide()
            self._resume_btn.hide()
            self._end_btn.setEnabled(True)
            self._mark_btn.setEnabled(True)

    def _on_state_changed(self, state: MeetingState) -> None:
        if state is MeetingState.LIVE and self._stack.currentIndex() != PAGE_LIVE:
            self._enter_live()
        elif state in (MeetingState.PAUSED, MeetingState.DRAINING, MeetingState.LIVE):
            self._sync_live_state()
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
        bar = self._live_transcript.verticalScrollBar()
        at_end = bar.value() >= bar.maximum() - 24
        self._live_transcript.setHtml(self._transcript_html(transcript_lines(segments, started_at), names))
        if at_end:
            bar.setValue(bar.maximum())
        marks = self._store.get_marks(meeting_id)
        self._marks_view.setHtml(
            "".join(
                f"<p style='margin:0 0 4px 0'>[{format_timestamp(m.offset_ms)}] {html.escape(m.note) or '标记'}</p>"
                for m in marks
            )
        )

    def _add_mark(self) -> None:
        offset = self._ctrl.add_mark(self._mark_note.text())
        if offset is not None:
            self._mark_note.clear()
            self._render_live()

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

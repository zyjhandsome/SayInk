"""History browser window and Markdown export helpers."""

from __future__ import annotations

import html
import logging
import re
import time
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QEvent, QItemSelectionModel, QPointF, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFontMetrics, QKeySequence, QPainter, QPen, QPolygonF, QShortcut, QTextLayout, QTextOption
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from sayink.history_store import SegmentRecord, SessionSummary
from sayink.ui import settings_styles as _settings_styles
from sayink.ui import design_tokens as tok
from sayink.ui.clipboard import copy_text
from sayink.ui.flow_layout import FlowLayout, WrappingToolBar
from sayink.ui.reading import fit_reading_width

logger = logging.getLogger(__name__)

COPY_FAILED_TEXT = "复制失败，请再试一次"


class _ElidedLabel(QLabel):
    """A single-line preview that keeps the full text available to assistive tools."""
    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.set_full_text(text)

    def set_full_text(self, text: str) -> None:
        self._full_text = " ".join(text.split())
        self.setToolTip(text)
        self.setAccessibleName(text)
        self._elide()

    def _elide(self):
        self.setText(QFontMetrics(self.font()).elidedText(
            self._full_text, Qt.TextElideMode.ElideRight, max(0, self.contentsRect().width())))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange) and hasattr(self, "_full_text"):
            self._elide()


class _PreviewLabel(_ElidedLabel):
    """Two lines of useful context, elided using the actual available width."""

    def _elide(self):
        width = max(1, self.contentsRect().width())
        layout = QTextLayout(self._full_text, self.font())
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        layout.setTextOption(option)
        layout.beginLayout()
        first = layout.createLine()
        if first.isValid():
            first.setLineWidth(width)
            end = first.textLength()
        else:
            end = len(self._full_text)
        layout.endLayout()
        lines = [self._full_text[:end].rstrip()]
        if end < len(self._full_text):
            lines.append(QFontMetrics(self.font()).elidedText(
                self._full_text[end:].lstrip(), Qt.TextElideMode.ElideRight, width))
        self.setText("\n".join(lines))
        self.setFixedHeight(QFontMetrics(self.font()).lineSpacing() * 2)


def session_list_item_css() -> str:
    """Row states shared by every master list (历史, 会议): square full-width
    wash plus a left accent bar when selected, plain wash on hover."""
    from sayink.ui import design_tokens as tok

    return f"""
        QListWidget::item {{
            color: transparent;
            padding: 0;
            margin: 1px 0;
            border: none;
            outline: none;
        }}
        QListWidget::item:selected,
        QListWidget::item:selected:focus,
        QListWidget::item:selected:!active {{
            background: {tok.ACCENT_SOFT};
            color: transparent;
            border: none;
            border-left: {tok.NAV_SELECTED_BAR_PX}px solid {tok.ACCENT};
            outline: none;
        }}
        QListWidget::item:hover:!selected {{
            background: {tok.ROW_HOVER};
            border: none;
            outline: none;
        }}
    """


class _RowFocusRing(QWidget):
    """Keyboard focus outline drawn above the row's item widget."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.hide()

    def paintEvent(self, event):
        from sayink.ui import design_tokens as tok

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(tok.ACCENT_FOCUS), 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), tok.RADIUS_SM, tok.RADIUS_SM)


class _SessionList(QListWidget):
    """Session list whose focus ring marks the current row, keyboard only.

    A mouse click already shows where the user is; a ring around the whole
    list would then outshine the selected row.
    """

    batch_mode = False
    _KEYBOARD_REASONS = (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason,
                         Qt.FocusReason.ShortcutFocusReason)
    _NAV_KEYS = (Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_Home, Qt.Key.Key_End,
                 Qt.Key.Key_PageUp, Qt.Key.Key_PageDown, Qt.Key.Key_Space)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._keyboard_focus = False
        self._focus_ring = _RowFocusRing(self.viewport())
        self.currentItemChanged.connect(lambda *_: self._place_focus_ring())
        self.verticalScrollBar().valueChanged.connect(lambda *_: self._place_focus_ring())

    @property
    def focus_ring_visible(self) -> bool:
        return not self._focus_ring.isHidden()

    def _place_focus_ring(self) -> None:
        item = self.currentItem()
        rect = self.visualItemRect(item) if item is not None else None
        if not (self._keyboard_focus and self.hasFocus() and rect is not None and rect.isValid()):
            self._focus_ring.hide()
            return
        self._focus_ring.setGeometry(rect)
        self._focus_ring.show()
        self._focus_ring.raise_()
        self._focus_ring.update()

    def focusInEvent(self, event):
        if event.reason() in self._KEYBOARD_REASONS:
            self._keyboard_focus = True
        elif event.reason() == Qt.FocusReason.MouseFocusReason:
            self._keyboard_focus = False
        super().focusInEvent(event)
        self._place_focus_ring()

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self._place_focus_ring()

    def mousePressEvent(self, event):
        self._keyboard_focus = False
        super().mousePressEvent(event)
        self._place_focus_ring()

    def keyPressEvent(self, event):
        if event.key() in self._NAV_KEYS:
            self._keyboard_focus = True
        super().keyPressEvent(event)
        self._place_focus_ring()

    def updateGeometries(self):
        super().updateGeometries()
        self._place_focus_ring()

    def selectionCommand(self, index, event=None):
        mouse_or_key = event is not None and event.type() in (
            QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease, QEvent.Type.KeyPress
        )
        if self.batch_mode and mouse_or_key and event.modifiers() == Qt.KeyboardModifier.NoModifier:
            if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                return QItemSelectionModel.SelectionFlag.Toggle
            if event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
                return QItemSelectionModel.SelectionFlag.NoUpdate
            if event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Space:
                return QItemSelectionModel.SelectionFlag.Toggle
        return super().selectionCommand(index, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Qt can retain the pre-show width of setItemWidget children after a
        # splitter resize. Reflow before eliding their text to the new width.
        self.doItemsLayout()


class _SessionCheckBox(QCheckBox):
    """Native checkbox behavior with an indicator visible in either theme."""

    def hitButton(self, pos):
        return self.rect().contains(pos)

    def paintEvent(self, event):
        from sayink.ui import design_tokens as tok

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        box = QRectF((self.width() - 16) / 2, (self.height() - 16) / 2, 16, 16)
        painter.setPen(QPen(QColor(tok.ACCENT if self.isChecked() else tok.TEXT_DIM), 1))
        painter.setBrush(QColor(tok.ACCENT if self.isChecked() else tok.SURFACE_PEARL))
        painter.drawRoundedRect(box, 3, 3)
        if self.isChecked():
            painter.setPen(QPen(QColor("#FFFFFF"), 2, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.drawPolyline(QPolygonF([
                QPointF(box.left() + 4, box.top() + 8),
                QPointF(box.left() + 7, box.top() + 11),
                QPointF(box.left() + 12, box.top() + 5),
            ]))
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(tok.ACCENT_FOCUS), 2))
            painter.drawRoundedRect(box.adjusted(-3, -3, 3, 3), 5, 5)


class _DetailTextEdit(QTextEdit):
    copy_session_requested = pyqtSignal()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_reading_width()

    def setHtml(self, text: str) -> None:
        super().setHtml(text)
        self._fit_reading_width()

    def setPlainText(self, text: str) -> None:
        super().setPlainText(text)
        self._fit_reading_width()

    def _fit_reading_width(self) -> None:
        # Editors use their full width. Document-frame changes during editing
        # would add formatting commands to the user's undo history.
        if self.isReadOnly():
            fit_reading_width(self)

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.StandardKey.Copy) and not self.textCursor().hasSelection():
            self.copy_session_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)


def _format_dt(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


def _day_label(ms: int) -> str:
    day = datetime.fromtimestamp(ms / 1000).date()
    today = datetime.now().date()
    delta = (today - day).days
    if delta == 0:
        return "今天"
    if delta == 1:
        return "昨天"
    return day.strftime("%Y-%m-%d")


def _filename_dt(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%Y%m%d-%H%M%S")


_SUPPRESSED_POLISHED_MARKER = "\u200b"


def _effective_text(segment: SegmentRecord) -> str:
    if segment.polished_text == _SUPPRESSED_POLISHED_MARKER:
        return ""
    return segment.polished_text or segment.raw_text


def _yaml_value(value: object) -> str:
    return str(value).replace("\n", " ").strip()


def _segments_in_order(segments: list[SegmentRecord]) -> list[SegmentRecord]:
    return sorted(segments, key=lambda s: s.seq)


def _session_duration_ms(segments: list[SegmentRecord]) -> int:
    return sum(max(0, int(s.duration_ms)) for s in segments)


def _session_model(segments: list[SegmentRecord]) -> str:
    return next((s.model for s in segments if s.model), "")


def history_shows_speakers(segments: list[SegmentRecord]) -> bool:
    """Label multiple voices, or uncertainty alongside an identified voice."""
    ids = {int(segment.speaker_id) for segment in segments if int(segment.speaker_id) > 0}
    return len(ids) >= 2 or bool(ids) and any(
        segment.trigger_mode == "continuous" and int(segment.speaker_id) == 0 for segment in segments
    )


def _segment_clock_ms(segment: SegmentRecord) -> int:
    """When the person spoke, or when the line was saved if that is unknown."""
    captured = int(getattr(segment, "captured_at", 0) or 0)
    if captured > 0:
        return captured
    return int(segment.created_at)


def _route_caption(route: str) -> str:
    return {"mic": "麦克风", "system": "电脑声音"}.get((route or "").strip(), "")


def _speaker_heading(segment: SegmentRecord) -> str:
    heading = f"说话人 {int(segment.speaker_id)}" if int(segment.speaker_id) > 0 else "说话人待确认"
    route = _route_caption(segment.speaker_route)
    if route:
        return f"{heading} · {route}"
    return heading


def _labeled_text(segment: SegmentRecord, text: str, *, show_speakers: bool) -> str:
    body = text.strip()
    if not body:
        return ""
    if show_speakers:
        return f"{_speaker_heading(segment)}\n{body}"
    return body


def _session_body(segments: list[SegmentRecord]) -> str:
    ordered = _segments_in_order(segments)
    show_speakers = history_shows_speakers(ordered)
    chunks = [
        _labeled_text(segment, _effective_text(segment), show_speakers=show_speakers)
        for segment in ordered
    ]
    return "\n\n".join(chunk for chunk in chunks if chunk)


_SOURCE_CHIP = {
    "mic": "麦克风",
    "microphone": "麦克风",
    "system": "电脑声音",
    "mixed": "混合",
    "file": "文件转写",
}

_TRIGGER_CHIP = {
    "continuous": "持续转写",
    "hotkey": "单次转写",
    "file_import": "导入文件",
}


def _source_chip_text(source: str | None) -> str:
    key = (source or "").strip()
    return _SOURCE_CHIP.get(key, key or "未知")


def _friendly_app_name(name: str) -> str:
    return re.sub(r"\.exe$", "", name or "", flags=re.IGNORECASE)


def _source_description(source: str) -> str:
    return "麦克风 + 电脑声音" if source == "mixed" else _source_chip_text(source)


def _search_excerpt(text: str, query: str) -> str:
    text = " ".join(text.split())
    index = text.casefold().find(query.casefold()) if query else -1
    start = max(0, index - 12)
    return ("…" if start else "") + text[start:]


def _trigger_chip_text(mode: str | None) -> str:
    key = (mode or "").strip()
    return _TRIGGER_CHIP.get(key, key)


def _session_has_polished(segments: list[SegmentRecord]) -> bool:
    return any((s.polished_text or "").strip() for s in segments)


def _chip_qss() -> str:
    from sayink.ui import design_tokens as tok

    return (
        f"color: {tok.TEXT_DIM}; font-size: {tok.TYPE_CAPTION}px;"
        " background: transparent; padding: 2px 0;"
    )


def suggest_single_export_filename(session: SessionSummary) -> str:
    return f"sayink-{_filename_dt(session.created_at)}.md"


def suggest_batch_export_filename(*, now_ms: int | None = None) -> str:
    stamp = int(time.time() * 1000) if now_ms is None else now_ms
    return f"sayink-export-{_filename_dt(stamp)}.md"


def build_single_session_plain(
    session: SessionSummary,
    segments: list[SegmentRecord],
) -> str:
    del session
    return _session_body(segments).rstrip() + "\n"


def build_single_session_markdown(
    session: SessionSummary,
    segments: list[SegmentRecord],
) -> str:
    frontmatter = [
        "---",
        f"created_at: {_yaml_value(_format_dt(session.created_at))}",
        f"source: {_yaml_value(session.source)}",
        f"target_app: {_yaml_value(session.target_app)}",
        f"duration_ms: {_session_duration_ms(segments)}",
        f"model: {_yaml_value(_session_model(segments))}",
        f"segment_count: {session.segment_count}",
        "---",
        "",
    ]
    return "\n".join(frontmatter) + _session_body(segments).rstrip() + "\n"


def build_batch_export_markdown(
    sessions: list[SessionSummary],
    segments_by_session: dict[str, list[SegmentRecord]],
) -> str:
    lines = [
        "---",
        f"exported_at: {_yaml_value(_format_dt(int(time.time() * 1000)))}",
        f"session_count: {len(sessions)}",
        "---",
        "",
    ]
    for session in sorted(sessions, key=lambda s: s.created_at):
        segments = segments_by_session.get(session.session_id, [])
        lines.extend(
            [
                f"## {_format_dt(session.created_at)} · {session.preview}",
                "",
                f"- source: {session.source}",
                f"- target_app: {session.target_app}",
                f"- duration_ms: {_session_duration_ms(segments)}",
                f"- model: {_session_model(segments)}",
                f"- segment_count: {session.segment_count}",
                "",
                _session_body(segments).rstrip(),
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"

def _format_duration(ms: int) -> str:
    seconds = max(0, int(ms)) // 1000
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {seconds:02d} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 时 {minutes:02d} 分"


def _session_title(created_at: int) -> str:
    return f"{_day_label(created_at)} {datetime.fromtimestamp(created_at / 1000).strftime('%H:%M')}"


def _highlight_html(text: str, query: str, mark_css: str) -> str:
    escaped = html.escape(text)
    q = (query or "").strip()
    if not q:
        return escaped
    pattern = re.compile(re.escape(html.escape(q)), re.IGNORECASE)
    return pattern.sub(lambda m: f'<span style="{mark_css}">{m.group(0)}</span>', escaped)


class _Toast(QFrame):
    """Non-layout overlay for transient feedback (copy done / undo delete)."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("historyToast")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 8, 8, 8)
        lay.setSpacing(12)
        self.label = QLabel("")
        lay.addWidget(self.label, 1)
        self.button = QPushButton("撤销")
        self.button.setCursor(Qt.CursorShape.PointingHandCursor)
        lay.addWidget(self.button)
        self.hide()

    def reposition(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        self.adjustSize()
        w = min(max(self.sizeHint().width(), 240), max(240, parent.width() - 48))
        h = self.sizeHint().height()
        self.setFixedSize(w, h)
        self.move((parent.width() - w) // 2, parent.height() - h - 18)
        self.raise_()


class _ClearHistoryDialog(QDialog):
    """In-app confirm card. The stock question box uses English Yes/No and a system icon."""

    def __init__(self, parent=None, *, session_count: int | None = None):
        super().__init__(parent)
        self.setWindowTitle("清空全部历史")
        self.setModal(True)
        self.setFixedWidth(360)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        from sayink.ui import design_tokens as tok
        from sayink.ui import settings_styles as ss

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("clearHistoryCard")
        outer.addWidget(card)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(8)

        title = QLabel("清空全部历史")
        title.setObjectName("clearHistoryTitle")
        scope = f"全部 {session_count} 条转写历史" if session_count is not None else "全部转写历史"
        body = QLabel(f"将永久删除{scope}，包括搜索结果之外和尚未加载的记录。\n\n会议记录不受影响。此操作不可撤销。")
        body.setWordWrap(True)
        body.setObjectName("clearHistoryBody")
        lay.addWidget(title)
        lay.addWidget(body)
        lay.addSpacing(8)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch(1)
        cancel = QPushButton("取消")
        confirm = QPushButton("清空")
        cancel.setStyleSheet(ss.BTN_GHOST_SM)
        confirm.setStyleSheet(ss.BTN_DANGER_SM)
        cancel.clicked.connect(self.reject)
        confirm.clicked.connect(self.accept)
        cancel.setAutoDefault(True)
        cancel.setDefault(True)
        confirm.setAutoDefault(False)
        buttons.addWidget(cancel)
        buttons.addWidget(confirm)
        lay.addLayout(buttons)

        self.setStyleSheet(f"""
            QDialog {{ background: transparent; }}
            QFrame#clearHistoryCard {{
                background: {tok.SURFACE};
                border: 1px solid {tok.CONTROL_BORDER};
                border-radius: {tok.RADIUS_LG}px;
            }}
            QLabel#clearHistoryTitle {{
                color: {tok.TEXT};
                font-size: {tok.TYPE_TITLE_SM}px;
                font-weight: 700;
                background: transparent;
            }}
            QLabel#clearHistoryBody {{
                color: {tok.TEXT_SEC};
                font-size: {tok.TYPE_BODY_SM}px;
                background: transparent;
            }}
        """)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        parent = self.parentWidget()
        if parent is None or not parent.isVisible():
            return
        center = parent.mapToGlobal(parent.rect().center())
        frame = self.frameGeometry()
        self.move(center.x() - frame.width() // 2, center.y() - frame.height() // 2)


class HistoryWindow(QWidget):
    history_preferences_requested = pyqtSignal()
    meeting_created = pyqtSignal(str)
    ai_settings_requested = pyqtSignal()

    ROW_HEIGHT = 80
    GROUP_HEADER_HEIGHT = 30
    # Treat a scrollbar this close to the end as "watching the latest turn".
    _DETAIL_TAIL_SLACK = 24

    def __init__(self, store, parent=None, *, config=None, polisher=None):
        super().__init__(parent)
        self._store = store
        self._config = config
        self._polisher = polisher
        if self._polisher is not None:
            self._polisher.polish_complete.connect(self._on_polish_complete)
            self._polisher.polish_error.connect(self._on_polish_error)
        self._polishing_session_id: str | None = None
        self._editing_session = False
        self._editing_session_id: str | None = None
        self._editing_original_text = ""
        self._sessions_by_id: dict[str, SessionSummary] = {}
        self._pending_delete: list[SessionSummary] = []
        self._pending_merge: list[SessionSummary] = []
        self._merge_committed_by_delete = False
        self._open_session_id: str | None = None
        self._search_query = ""
        self._history_enabled = True
        self._session_limit = 50
        self._has_more = False
        self._view_polished = True
        self._selection_mode = False
        self._list_summary_text = ""
        self._detail_search_context: tuple[str, str] | None = None
        self._loaded_search_query = ""
        self._transient_active = False
        self._detail_session_id: str | None = None
        self._detail_follow_tail = True
        self._detail_pending_anchor: int | None = None
        self._detail_scroll_lock = False
        self._detail_scroll_settle = False
        self._detail_scroll_gen = 0
        self._setup_window()
        self._setup_ui()
        self.refresh()
        self.reapply_theme()

    # ── chrome / theme ────────────────────────────────────────────

    def _setup_window(self) -> None:
        self.setWindowTitle("历史")
        self.setStyleSheet(_settings_styles.WINDOW_CSS)

    def reapply_theme(self) -> None:
        from sayink.ui import settings_styles as ss

        self.setStyleSheet(ss.WINDOW_CSS)
        self._paint_history_styles()
        # Detail HTML embeds token colors; re-render for the active axis.
        if hasattr(self, "_details"):
            self._on_selection_changed()

    def _paint_history_styles(self) -> None:
        """Apply styles from the active token axis (also used by reapply)."""
        from sayink.ui import design_tokens as tok
        from sayink.ui import settings_styles as ss

        if hasattr(self, "_left_pane"):
            self._left_pane.setStyleSheet(
                "QWidget#historyLeftPane { background: transparent; border: none; }"
            )
        if hasattr(self, "_title_label"):
            from sayink.ui.settings_components import PAGE_TITLE

            self._title_label.setStyleSheet(PAGE_TITLE)
        if hasattr(self, "_right_pane"):
            self._right_pane.setStyleSheet("background: transparent;")
        if hasattr(self, "_search_edit"):
            self._search_edit.setStyleSheet(ss.build_search_field_css())
        if hasattr(self, "_session_list"):
            self._session_list.setStyleSheet(f"""
                QListWidget {{
                    background: transparent;
                    color: transparent;
                    border: 2px solid transparent;
                    padding: 0;
                    outline: none;
                }}
                {session_list_item_css()}
                QScrollBar:vertical {{
                    background: transparent;
                    width: 6px;
                    margin: 0;
                }}
                QScrollBar::handle:vertical {{
                    background: {tok.HAIRLINE};
                    border-radius: 3px;
                    min-height: 30px;
                }}
                QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
                QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
                    height: 0;
                    border: none;
                    background: none;
                }}
                QScrollBar:horizontal {{
                    height: 0;
                    background: none;
                    border: none;
                }}
            """)
            self._restyle_rows()
        if hasattr(self, "_detail_title"):
            self._detail_title.setStyleSheet(
                f"font-size: {tok.TYPE_TITLE}px; font-weight: 700; color: {tok.TEXT};"
                f" background: transparent;"
            )
            self._detail_stats.setStyleSheet(
                f"font-size: {tok.TYPE_FOOTNOTE}px; color: {tok.TEXT_DIM}; background: transparent;"
                f" padding-bottom: 2px;"
            )
        if hasattr(self, "_toast"):
            self._toast.setStyleSheet(f"""
                QFrame#historyToast {{
                    background: {tok.SURFACE_PEARL};
                    border: 1px solid {tok.CONTROL_BORDER};
                    border-radius: {tok.RADIUS_MD}px;
                }}
            """)
            self._feedback_label.setStyleSheet(
                f"color: {tok.TEXT}; font-size: {tok.TYPE_BODY_SM}px; background: transparent;"
            )
            self._undo_btn.setStyleSheet(ss.BTN_ACCENT_SM)
        if hasattr(self, "_details"):
            if getattr(self, "_editing_session", False):
                self._details.setStyleSheet(f"""
                    QTextEdit {{
                        background: {tok.SURFACE_PEARL};
                        color: {tok.TEXT};
                        border: 1px solid {tok.CONTROL_BORDER};
                        border-radius: {tok.RADIUS_MD}px;
                        padding: 8px;
                        font-size: {tok.READING_FONT_PX}px;
                        selection-background-color: {tok.ACCENT_SOFT};
                        selection-color: {tok.TEXT};
                    }}
                    QTextEdit:focus {{
                        border: 2px solid {tok.ACCENT};
                    }}
                """)
            else:
                self._details.setStyleSheet(f"""
                    QTextEdit {{
                        background: transparent;
                        color: {tok.TEXT};
                        border: none;
                        padding: 4px 0;
                        font-size: {tok.READING_FONT_PX}px;
                        selection-background-color: {tok.ACCENT_SOFT};
                        selection-color: {tok.TEXT};
                    }}
                """)
        if hasattr(self, "_list_summary"):
            self._list_summary.setStyleSheet(
                f"color: {tok.TEXT_SEC}; font-size: {tok.TYPE_FOOTNOTE}px; background: transparent;")
        if hasattr(self, "_splitter"):
            # A filled, vertically inset handle reads as a second scrollbar thumb
            # sitting beside the list's real bar.
            self._splitter.setStyleSheet(
                f"QSplitter::handle {{ background: transparent; }}"
                f"QSplitter::handle:hover {{ background: {tok.ACCENT_FOCUS}; }}")
        if hasattr(self, "_export_btn"):
            self._export_btn.setStyleSheet(ss.BTN_GHOST_SM)
        if hasattr(self, "_polish_btn"):
            for btn in (self._polish_btn, self._edit_btn, self._cancel_edit_btn, self._merge_btn, self._to_meeting_btn):
                btn.setStyleSheet(ss.BTN_GHOST_SM)
        if hasattr(self, "_more_btn"):
            self._more_btn.setStyleSheet(ss.BTN_GHOST_SM)
            self._history_preferences_btn.setStyleSheet(ss.BTN_ACCENT_SM)
        if hasattr(self, "_selection_mode_btn"):
            no_indicator = "QPushButton::menu-indicator { image: none; width: 0px; }"
            for button in (self._selection_mode_btn, self._metadata_btn):
                button.setStyleSheet(ss.BTN_GHOST_SM)
            for menu_btn in (self._manage_btn, self._detail_more_btn):
                menu_btn.setStyleSheet(ss.BTN_GHOST_SM + no_indicator)
            from sayink.ui.tray_icon import _menu_font, _menu_stylesheet

            for menu in (self._manage_menu, self._detail_menu):
                menu.setFont(_menu_font())
                menu.setStyleSheet(_menu_stylesheet())
            self._detail_context.setStyleSheet(
                f"color: {tok.TEXT_DIM}; font-size: {tok.TYPE_FOOTNOTE}px; background: transparent;"
            )
        if hasattr(self, "_detail_chip_labels"):
            self._detail_chip_bar.setStyleSheet("QWidget#historyMetadata { background: transparent; }")
            chip_css = _chip_qss()
            for lab in self._detail_chip_labels:
                lab.setStyleSheet(chip_css)
        if hasattr(self, "_view_bar"):
            self._paint_view_toggle()
        if hasattr(self, "_delete_btn"):
            self._delete_btn.setStyleSheet(ss.BTN_DANGER_SM)
        if hasattr(self, "_copy_btn"):
            self._apply_copy_action_styles(self._current_selection_has_polished())

    def _paint_view_toggle(self) -> None:
        from sayink.ui import design_tokens as tok

        css = (
            f"QPushButton {{ background: transparent; color: {tok.TEXT_SEC}; border: 2px solid transparent;"
            f" border-radius: {tok.RADIUS_SM}px; padding: 2px 10px; min-height: 26px;"
            f" font-size: {tok.TYPE_FOOTNOTE}px; font-weight: 400; }}"
            f"QPushButton:hover {{ color: {tok.TEXT}; }}"
            f"QPushButton:checked {{ background: {tok.SURFACE}; color: {tok.TEXT}; font-weight: 700; }}"
            f"QPushButton:focus {{ border-color: {tok.ACCENT_FOCUS}; }}"
        )
        for btn in (self._view_polished_btn, self._view_raw_btn):
            btn.setStyleSheet(css)
        self._view_bar.setStyleSheet(
            f"QWidget#historyViewBar {{ background: {tok.SURFACE_PEARL};"
            f" border: 1px solid {tok.HAIRLINE}; border-radius: {tok.RADIUS_MD}px; }}"
        )

    def _restyle_rows(self) -> None:
        from sayink.ui import design_tokens as tok

        preview_css = f"color: {tok.TEXT}; font-size: {tok.TYPE_BODY_SM}px; background: transparent;"
        meta_css = f"color: {tok.TEXT_DIM}; font-size: {tok.TYPE_CAPTION}px; background: transparent;"
        day_css = (
            f"color: {tok.TEXT_DIM}; font-size: {tok.TYPE_CAPTION}px; font-weight: 700;"
            f" letter-spacing: 0; background: transparent;"
        )
        time_css = (
            f"color: {tok.TEXT_SEC}; font-size: {tok.TYPE_FOOTNOTE}px; font-weight: 400;"
            f" font-family: {tok.FONT_MONO}; background: transparent;"
        )
        for i in range(self._session_list.count()):
            row = self._session_list.itemWidget(self._session_list.item(i))
            if row is None:
                continue
            checkbox = row.findChild(QCheckBox)
            if checkbox is not None:
                checkbox.update()
            for lab in row.findChildren(QLabel):
                name = lab.objectName()
                if name == "streamPreview":
                    lab.setStyleSheet(preview_css)
                elif name == "streamDay":
                    lab.setStyleSheet(day_css)
                elif name == "streamTime":
                    lab.setStyleSheet(time_css)
                else:
                    lab.setStyleSheet(meta_css)
        self._sync_row_selection()

    def _paint_stream_row(self, row: QWidget, *, selected: bool) -> None:
        from sayink.ui import design_tokens as tok

        # The item widget covers QListWidget::item:selected, so the wash and
        # left bar have to live on the row itself. A transparent bar on the
        # idle row keeps the text from jumping when the selection moves.
        bg = tok.ACCENT_SOFT if selected else "transparent"
        bar = tok.ACCENT if selected else "transparent"
        css = (
            "QWidget#historyStreamRow {"
            f" background: {bg};"
            " border: none;"
            f" border-left: {tok.NAV_SELECTED_BAR_PX}px solid {bar};"
            " border-radius: 0;"
            "}"
        )
        if row.styleSheet() != css:
            row.setStyleSheet(css)

    def _sync_row_selection(self) -> None:
        if not hasattr(self, "_session_list"):
            return
        selected = set(self._selected_session_ids())
        for i in range(self._session_list.count()):
            item = self._session_list.item(i)
            row = self._session_list.itemWidget(item)
            if row is None or row.objectName() != "historyStreamRow":
                continue
            session_id = item.data(Qt.ItemDataRole.UserRole)
            self._paint_stream_row(row, selected=bool(session_id) and session_id in selected)
            checkbox = row.findChild(QCheckBox, "historySessionCheckbox")
            if checkbox is not None:
                checkbox.setVisible(self._selection_mode)
                checkbox.setChecked(item.isSelected())

    def _get_session_segments_effective(self, session_id: str) -> list[SegmentRecord]:
        if self._pending_merge:
            ordered_merge = sorted(self._pending_merge, key=lambda s: s.created_at)
            if ordered_merge and session_id == ordered_merge[0].session_id:
                combined: list[SegmentRecord] = []
                for s in ordered_merge:
                    combined.extend(self._store.get_session_segments(s.session_id))
                combined.sort(key=lambda seg: (seg.created_at, seg.seq))
                return [
                    SegmentRecord(
                        session_id,
                        idx,
                        seg.created_at,
                        seg.raw_text,
                        seg.polished_text,
                        seg.source,
                        seg.duration_ms,
                        seg.target_app,
                        seg.trigger_mode,
                        seg.model,
                        seg.speaker_id,
                        seg.speaker_route,
                        seg.captured_at,
                    )
                    for idx, seg in enumerate(combined)
                ]
        return self._store.get_session_segments(session_id)

    def _current_selection_has_polished(self) -> bool:
        selected = self._selected_session_ids()
        if len(selected) != 1:
            return False
        return _session_has_polished(self._get_session_segments_effective(selected[0]))

    def _apply_copy_action_styles(self, has_polished: bool) -> None:
        from sayink.ui import settings_styles as ss

        if self._selection_mode:
            self._copy_btn.setText(f"复制 {len(self._selected_session_ids())} 条")
        elif has_polished:
            self._copy_btn.setText("复制润色" if self._view_polished else "复制原文")
        else:
            self._copy_btn.setText("复制")
        self._copy_btn.setStyleSheet(ss.BTN_ACCENT_SM)
        self._sync_copy_tooltips(has_polished)

    def _copy_follows_raw_view(self, has_polished: bool) -> bool:
        return not has_polished or not self._view_polished

    def _sync_copy_tooltips(self, has_polished: bool) -> None:
        """Describe the current copy action and its keyboard shortcut."""
        shortcut = " · Ctrl+C"
        if self._selection_mode:
            self._copy_btn.setToolTip(
                "按时间合并复制所选会话的最终文本（有润色时用润色版）" + shortcut
            )
            return
        raw_view = self._copy_follows_raw_view(has_polished)
        self._copy_btn.setToolTip(("复制识别原文" if raw_view else "复制润色后的全文") + shortcut)

    def _set_selection_mode(self, enabled: bool) -> None:
        self._selection_mode = enabled
        self._selection_mode_btn.blockSignals(True)
        self._selection_mode_btn.setChecked(enabled)
        self._selection_mode_btn.blockSignals(False)
        self._selection_mode_btn.setText("完成" if enabled else "选择")
        # Keep native Ctrl / Shift range selection in both modes. In batch
        # mode, an unmodified row click (or Space) also toggles its checkbox.
        self._session_list.batch_mode = enabled
        if not enabled:
            selected = self._session_list.selectedItems()
            current = self._session_list.currentItem()
            keep = current if current in selected else next(iter(selected or self.session_items()), None)
            self._session_list.blockSignals(True)
            try:
                self._session_list.clearSelection()
                if keep is not None:
                    self._session_list.setCurrentItem(keep)
                    keep.setSelected(True)
            finally:
                self._session_list.blockSignals(False)
        if not enabled and not self.session_count():
            self.refresh()
        else:
            self._on_selection_changed()

    def _toggle_session_selection(self, session_id: str, checked: bool) -> None:
        for item in self.session_items():
            if item.data(Qt.ItemDataRole.UserRole) == session_id:
                item.setSelected(checked)
                break

    def _update_list_controls(self) -> None:
        count = len(self._selected_session_ids())
        self._list_summary.setText(f"已选 {count} 条" if self._selection_mode else self._list_summary_text)
        self._list_summary.setToolTip(self._list_summary_text)
        self._selection_mode_btn.setEnabled(self.session_count() > 0 or self._selection_mode)

    def _toggle_metadata(self, expanded: bool) -> None:
        self._metadata_btn.setText("收起信息" if expanded else "详细信息")
        self._metadata_btn.setAccessibleName("收起会话详细信息" if expanded else "展开会话详细信息")
        self._detail_chip_bar.setVisible(expanded and bool(self._detail_chip_labels))

    # ── layout ────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(*tok.WORKSPACE_MARGINS)
        outer.setSpacing(14)

        top = QHBoxLayout()
        top.setSpacing(16)
        from sayink.ui.settings_components import page_title_label

        self._title_label = page_title_label("历史")
        top.addWidget(self._title_label, 1, Qt.AlignmentFlag.AlignVCenter)
        outer.addLayout(top)

        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.setChildrenCollapsible(False)
        self._splitter.setHandleWidth(5)
        split = self._splitter

        # Search and selection management belong to the history list.
        stream = QWidget()
        self._stream_host = stream
        self._left_pane = stream
        stream.setObjectName("historyLeftPane")
        stream_lay = QVBoxLayout(stream)
        stream_lay.setContentsMargins(0, 0, 0, 0)
        stream_lay.setSpacing(8)
        stream.setMinimumWidth(tok.WORKSPACE_LIST_MIN_WIDTH)
        self._search_edit = QLineEdit(stream)
        self._search_edit.setPlaceholderText("搜索全部转写历史")
        self._search_edit.setAccessibleName("搜索全部转写历史")
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.setToolTip("搜索全部转写历史的原文与润色文本，不含会议记录 · Ctrl+F")
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(200)
        self._search_edit.textChanged.connect(lambda: self._search_timer.start())
        self._search_timer.timeout.connect(self._perform_search)
        stream_lay.addWidget(self._search_edit)
        self._summary_row = WrappingToolBar(spacing=8)
        self._list_summary = QLabel()
        self._list_summary.setContentsMargins(3, 0, 0, 0)
        self._summary_row.add(self._list_summary, role="flex")
        self._selection_mode_btn = QPushButton("选择")
        self._selection_mode_btn.setCheckable(True)
        self._selection_mode_btn.setToolTip("勾选多条会话，一起复制、导出或删除；也可使用 Ctrl / Shift 多选")
        self._selection_mode_btn.setAccessibleName("批量选择会话")
        self._selection_mode_btn.toggled.connect(self._set_selection_mode)
        self._summary_row.add(self._selection_mode_btn, role="right")
        self._manage_btn = QPushButton("更多")
        self._manage_btn.setAccessibleName("历史列表管理")
        self._manage_menu = QMenu(self._manage_btn)
        self._clear_all_action = self._manage_menu.addAction("清空全部转写历史")
        self._clear_all_action.triggered.connect(self._clear_all_history)
        self._manage_btn.setMenu(self._manage_menu)
        self._summary_row.add(self._manage_btn, role="right")
        stream_lay.addWidget(self._summary_row)
        self._session_list = _SessionList()
        self._session_list.setObjectName("historyTimeStream")
        self._session_list.setAccessibleName("转写会话列表")
        self._session_list.setToolTip(
            "Ctrl / Shift 可多选；Delete 删除；Ctrl+C 复制当前查看的版本（多选时复制各项最终文本）"
        )
        self._session_list.setFrameShape(QFrame.Shape.NoFrame)
        self._session_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._session_list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._session_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self._session_list.itemSelectionChanged.connect(self._on_selection_changed)
        stream_lay.addWidget(self._session_list, 1)
        self._list_footer = QHBoxLayout()
        self._list_footer.setContentsMargins(0, 0, 0, 0)
        self._list_footer.setSpacing(8)
        self._more_btn = QPushButton("加载更早的会话")
        self._more_btn.clicked.connect(self._load_more)
        self._more_btn.hide()
        self._list_footer.addWidget(self._more_btn, 0, Qt.AlignmentFlag.AlignLeft)
        stream_lay.addLayout(self._list_footer)
        split.addWidget(stream)

        # ── right: detail ──
        self._right_pane = QWidget()
        right_lay = QVBoxLayout(self._right_pane)
        right_lay.setContentsMargins(20, 0, 0, 0)
        right_lay.setSpacing(10)

        title_row = QHBoxLayout()
        title_row.setSpacing(12)
        self._detail_title = QLabel("会话详情")
        self._detail_title.setWordWrap(True)
        title_row.addWidget(self._detail_title, 1)
        self._detail_stats = QLabel("")
        self._detail_stats.setObjectName("historyDetailStats")
        title_row.addWidget(self._detail_stats, 0, Qt.AlignmentFlag.AlignBottom)
        right_lay.addLayout(title_row)

        context_row = WrappingToolBar(spacing=8)
        self._detail_context = _ElidedLabel("")
        context_row.add(self._detail_context, role="flex")
        self._metadata_btn = QPushButton("详细信息")
        self._metadata_btn.setCheckable(True)
        self._metadata_btn.setAccessibleName("展开会话详细信息")
        self._metadata_btn.toggled.connect(self._toggle_metadata)
        context_row.add(self._metadata_btn, role="right")
        right_lay.addWidget(context_row)

        self._detail_chip_bar = QWidget()
        self._detail_chip_bar.setObjectName("historyMetadata")
        self._detail_chips = FlowLayout(self._detail_chip_bar, spacing=6)
        self._detail_chips.setContentsMargins(0, 0, 0, 0)
        self._detail_chip_host = self._detail_chips
        self._detail_chip_labels: list[QLabel] = []
        right_lay.addWidget(self._detail_chip_bar)

        self._view_bar = QWidget()
        self._view_bar.setObjectName("historyViewBar")
        view_lay = QHBoxLayout(self._view_bar)
        view_lay.setContentsMargins(3, 3, 3, 3)
        view_lay.setSpacing(2)
        self._view_group = QButtonGroup(self)
        self._view_group.setExclusive(True)
        self._view_polished_btn = QPushButton("润色")
        self._view_raw_btn = QPushButton("原文")
        for btn in (self._view_polished_btn, self._view_raw_btn):
            btn.setCheckable(True)
            btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            self._view_group.addButton(btn)
            view_lay.addWidget(btn)
        self._view_bar.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._view_polished_btn.setChecked(True)
        self._view_polished_btn.toggled.connect(self._on_view_toggled)
        tools = WrappingToolBar(spacing=6)
        self._detail_tools = tools
        tools.add(self._view_bar)
        self._copy_btn = QPushButton("复制")
        self._copy_btn.clicked.connect(self._copy_selected_effective)
        tools.add(self._copy_btn)
        self._polish_btn = QPushButton("润色")
        self._polish_btn.setToolTip("按「AI 服务」中的处理方式（润色或纠错）处理当前会话，原文保留")
        self._polish_btn.clicked.connect(self._polish_selected_session)
        tools.add(self._polish_btn)
        self._edit_btn = QPushButton("编辑")
        self._edit_btn.setToolTip("直接编辑当前会话文本（修改保存为润色版，原文保持不变）")
        self._edit_btn.clicked.connect(self._on_edit_clicked)
        tools.add(self._edit_btn)
        self._cancel_edit_btn = QPushButton("取消")
        self._cancel_edit_btn.setToolTip("放弃未保存的修改")
        self._cancel_edit_btn.clicked.connect(self._cancel_inline_edit)
        self._cancel_edit_btn.hide()
        tools.add(self._cancel_edit_btn)
        self._merge_btn = QPushButton("合并")
        self._merge_btn.setToolTip("将选中的多条会话按时间合并为一条会话，8 秒内可撤销")
        self._merge_btn.clicked.connect(self._merge_selected_sessions)
        self._merge_btn.hide()
        tools.add(self._merge_btn)
        self._to_meeting_btn = QPushButton("转为会议")
        self._to_meeting_btn.setToolTip("复制选中会话的分段生成会议记录，并打开会议工作台整理纪要")
        self._to_meeting_btn.clicked.connect(self._promote_selected_to_meeting)
        self._to_meeting_btn.hide()
        tools.add(self._to_meeting_btn)
        self._export_btn = QPushButton("导出")
        self._export_btn.setToolTip("将选中的会话导出为 Markdown 文件")
        self._export_btn.clicked.connect(self._export_selected)
        tools.add(self._export_btn, role="right")
        self._detail_more_btn = QPushButton("更多")
        self._detail_more_btn.setAccessibleName("当前会话的更多操作")
        self._detail_menu = QMenu(self._detail_more_btn)
        self._to_meeting_action = self._detail_menu.addAction("转为会议并整理")
        self._to_meeting_action.triggered.connect(self._promote_selected_to_meeting)
        self._detail_menu.addSeparator()
        self._delete_action = self._detail_menu.addAction("删除这条会话")
        self._delete_action.triggered.connect(self._delete_selected_sessions)
        self._detail_more_btn.setMenu(self._detail_menu)
        tools.add(self._detail_more_btn, role="right")
        self._delete_btn = QPushButton("删除")
        self._delete_btn.setToolTip("删除勾选的会话，8 秒内可撤销 · Delete")
        self._delete_btn.clicked.connect(self._delete_selected_sessions)
        tools.add(self._delete_btn, role="right")
        for btn in (
            self._copy_btn,
            self._polish_btn,
            self._edit_btn,
            self._cancel_edit_btn,
            self._merge_btn,
            self._to_meeting_btn,
            self._export_btn,
            self._detail_more_btn,
            self._delete_btn,
        ):
            btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        right_lay.addWidget(tools)
        self._view_bar.hide()

        self._details = _DetailTextEdit()
        self._details.copy_session_requested.connect(self._copy_selected_effective)
        self._details.setReadOnly(True)
        self._details.setAccessibleName("会话转写全文")
        self._details.setPlaceholderText("选择一条转写查看分段内容")
        self._details.document().setDocumentMargin(0)
        detail_bar = self._details.verticalScrollBar()
        detail_bar.rangeChanged.connect(self._on_detail_scrollbar_range_changed)
        detail_bar.valueChanged.connect(self._on_detail_scrollbar_value_changed)
        self._detail_scroll_timer = QTimer(self)
        self._detail_scroll_timer.setSingleShot(True)
        self._detail_scroll_timer.timeout.connect(self._on_detail_scroll_timeout)
        right_lay.addWidget(self._details, 1)
        self._history_preferences_btn = QPushButton("设置历史记录")
        self._history_preferences_btn.clicked.connect(self.history_preferences_requested.emit)
        self._history_preferences_btn.hide()
        right_lay.addWidget(self._history_preferences_btn, 0, Qt.AlignmentFlag.AlignLeft)

        split.addWidget(self._right_pane)
        self._right_pane.setMinimumWidth(tok.WORKSPACE_DETAIL_MIN_WIDTH)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 2)
        split.setSizes(list(tok.WORKSPACE_SPLIT_SIZES))
        outer.addWidget(split, 1)

        # ── overlay toast (copy feedback + undo delete) ──
        self._toast = _Toast(self)
        self._undo_bar = self._toast
        self._feedback_label = self._toast.label
        self._undo_label = self._feedback_label
        self._undo_btn = self._toast.button
        self._undo_btn.clicked.connect(self._undo_pending_action)
        self._feedback_timer = QTimer(self)
        self._feedback_timer.setSingleShot(True)
        self._feedback_timer.timeout.connect(self._end_transient_feedback)
        self._undo_timer = QTimer(self)
        self._undo_timer.setSingleShot(True)
        self._undo_timer.setInterval(8000)
        self._undo_timer.timeout.connect(self._commit_pending_undo)

        # ── keyboard ──
        for sequence, slot in (
            (QKeySequence.StandardKey.Delete, self._delete_selected_sessions),
            (QKeySequence.StandardKey.Copy, self._copy_selected_effective),
        ):
            shortcut = QShortcut(QKeySequence(sequence), self._session_list)
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(slot)

        for button in self.findChildren(QPushButton):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self._paint_history_styles()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "_toast") and self._toast.isVisible():
            self._toast.reposition()

    def _sync_clear_all_enabled(self) -> None:
        self._clear_all_action.setEnabled(
            self.session_count() > 0 or bool(self._search_query) or bool(self._pending_delete)
        )

    # ── data → list ───────────────────────────────────────────────

    def refresh(self) -> None:
        # Both branches page by ``_session_limit``: every row is a widget, so a
        # common search word matching thousands of sessions must not freeze
        # the window while it builds them all.
        if self._search_query:
            sessions = self._store.search_sessions(self._search_query, limit=self._session_limit + 1)
        else:
            sessions = self._store.list_sessions(limit=self._session_limit + 1)
        self._has_more = len(sessions) > self._session_limit
        self._load_sessions(sessions[:self._session_limit])

    def set_history_enabled(self, enabled: bool) -> None:
        self._history_enabled = enabled
        self.refresh()

    def _load_more(self) -> None:
        self._session_limit += 50
        self.refresh()

    def session_items(self) -> list[QListWidgetItem]:
        """Session rows only (group headers are non-selectable filler items)."""
        return [
            self._session_list.item(i)
            for i in range(self._session_list.count())
            if self._session_list.item(i).data(Qt.ItemDataRole.UserRole)
        ]

    def session_count(self) -> int:
        return len(self.session_items())

    def _load_sessions(self, sessions: list[SessionSummary]) -> None:
        selected = self._selected_session_ids()
        current = self._session_list.currentItem()
        current_id = current.data(Qt.ItemDataRole.UserRole) if current is not None else None
        anchor_id, anchor_top = None, 0
        same_query = self._loaded_search_query == self._search_query
        list_at_top = self._session_list.verticalScrollBar().value() == 0
        if same_query and not list_at_top:
            for item in self.session_items():
                rect = self._session_list.visualItemRect(item)
                if rect.bottom() >= 0:
                    anchor_id = item.data(Qt.ItemDataRole.UserRole)
                    anchor_top = rect.top()
                    break
        self._loaded_search_query = self._search_query
        pending = {s.session_id for s in self._pending_delete}
        sessions = [s for s in sessions if s.session_id not in pending]
        if len(self._pending_merge) >= 2:
            ordered_merge = sorted(self._pending_merge, key=lambda s: s.created_at)
            target_id = ordered_merge[0].session_id
            merge_ids = {s.session_id for s in ordered_merge}
            total_segs = sum(s.segment_count for s in ordered_merge)
            merged_summary = SessionSummary(
                session_id=target_id,
                created_at=ordered_merge[0].created_at,
                segment_count=total_segs,
                source=ordered_merge[0].source,
                target_app=ordered_merge[0].target_app,
                preview=ordered_merge[0].preview,
            )
            rebuilt: list[SessionSummary] = []
            inserted = False
            for s in sessions:
                if s.session_id in merge_ids:
                    if not inserted:
                        rebuilt.append(merged_summary)
                        inserted = True
                else:
                    rebuilt.append(s)
            sessions = sorted(rebuilt, key=lambda s: s.created_at, reverse=True)
        from sayink.ui import design_tokens as tok

        rows_key = (tuple(sessions), tuple(_day_label(session.created_at) for session in sessions),
                    self._search_query, self._has_more, self._history_enabled,
                    getattr(self._store, "disabled", False) is True,
                    tok.FONT, tok.FONT_MONO, tok.TEXT, tok.TEXT_SEC, tok.TEXT_DIM, tok.ACCENT, tok.ACCENT_SOFT)
        if getattr(self, "_session_rows_key", None) == rows_key:
            # Details can change without changing a session's preview/count.
            self._on_selection_changed()
            return
        # Rebuilding the list clears the selection first. That signal would
        # treat the open session as closed and snap the detail back to the top
        # on the next paint. Restore selection, then refresh the detail once.
        self._session_list.blockSignals(True)
        try:
            self._session_list.clear()
            self._history_preferences_btn.setVisible(
                not sessions and not self._search_query and not self._history_enabled
            )
            self._more_btn.setText("加载更多结果" if self._search_query else "加载更早的会话")
            self._more_btn.setVisible(self._has_more)
            self._sessions_by_id = {s.session_id: s for s in sessions}
            last_day = None
            for session in sessions:
                day = _day_label(session.created_at)
                if day != last_day:
                    self._add_group_header(day, first=last_day is None)
                    last_day = day
                item = QListWidgetItem(self._session_item_text(session))
                item.setData(Qt.ItemDataRole.UserRole, session.session_id)
                item.setToolTip(
                    f"来源：{_source_chip_text(session.source)} · "
                    f"应用：{session.target_app or '未知应用'} · {session.segment_count} 段"
                )
                item.setForeground(Qt.GlobalColor.transparent)
                item.setSizeHint(QSize(0, self.ROW_HEIGHT))
                self._session_list.addItem(item)
                self._session_list.setItemWidget(item, self._build_stream_row(session))
            items = self.session_items()
            if items:
                retained = [it for it in items if it.data(Qt.ItemDataRole.UserRole) in selected]
                if retained:
                    for it in retained:
                        it.setSelected(True)
                    current = next((it for it in retained if it.data(Qt.ItemDataRole.UserRole) == current_id), retained[0])
                    self._session_list.setCurrentItem(current, QItemSelectionModel.SelectionFlag.NoUpdate)
                elif not self._selection_mode:
                    self._session_list.setCurrentItem(items[0])
            else:
                self._set_detail_chips([])
                self._view_bar.hide()
                store_disabled = getattr(self._store, "disabled", False) is True
                if self._search_query:
                    title, empty = "没有搜索结果", "没有匹配的转写。试试更短的关键词，或清除搜索。"
                elif store_disabled:
                    title = "历史记录不可用"
                    empty = "历史记录数据库无法打开，本次运行不会保存转写历史。\n请查看日志，或删除配置目录下的 history.db 后重启。"
                elif not self._history_enabled:
                    title = "历史记录未开启"
                    empty = "历史记录未开启。\n可在通用设置中开启，之后的转写文本会保存在本机。"
                else:
                    title, empty = "还没有会话", "还没有会话。完成一次转写后会出现在这里。"
                self._detail_title.setText(title)
                self._set_detail_document(plain=empty)
        finally:
            self._session_list.blockSignals(False)
        if anchor_id is not None:
            anchor = next((it for it in self.session_items() if it.data(Qt.ItemDataRole.UserRole) == anchor_id), None)
            if anchor is not None:
                self._session_list.doItemsLayout()
                self._session_list.scrollToItem(anchor, QAbstractItemView.ScrollHint.PositionAtTop)
                bar = self._session_list.verticalScrollBar()
                bar.setValue(bar.value() - anchor_top)
        elif same_query and list_at_top:
            self._session_list.verticalScrollBar().setValue(0)
        count = f"{len(sessions)}+" if self._has_more and self._search_query else str(len(sessions))
        self._list_summary_text = f"找到 {count} 条" if self._search_query else f"最近 {count} 条"
        self._sync_clear_all_enabled()
        self._restyle_rows()
        self._on_selection_changed()
        self._session_rows_key = rows_key

    def _add_group_header(self, day: str, *, first: bool) -> None:
        item = QListWidgetItem("")
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        height = self.GROUP_HEADER_HEIGHT - (8 if first else 0)
        item.setSizeHint(QSize(0, height))
        self._session_list.addItem(item)
        header = QWidget()
        lay = QHBoxLayout(header)
        lay.setContentsMargins(13, 0 if first else 8, 10, 2)
        lab = QLabel(day)
        lab.setObjectName("streamDay")
        lay.addWidget(lab, 0, Qt.AlignmentFlag.AlignBottom)
        lay.addStretch(1)
        self._session_list.setItemWidget(item, header)

    def _build_stream_row(self, session: SessionSummary) -> QWidget:
        row = QWidget()
        row.setObjectName("historyStreamRow")
        row.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        outer = QHBoxLayout(row)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(8)
        checkbox = _SessionCheckBox()
        checkbox.setFixedSize(24, 32)
        checkbox.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        checkbox.setObjectName("historySessionCheckbox")
        checkbox.setAccessibleName(f"选择会话：{_session_title(session.created_at)}，{session.preview}")
        checkbox.setVisible(self._selection_mode)
        checkbox.clicked.connect(lambda checked, sid=session.session_id: self._toggle_session_selection(sid, checked))
        outer.addWidget(checkbox, 0, Qt.AlignmentFlag.AlignVCenter)
        lay = QVBoxLayout()
        lay.setSpacing(4)
        outer.addLayout(lay, 1)

        line = QHBoxLayout()
        line.setSpacing(10)
        time_lab = QLabel(datetime.fromtimestamp(session.created_at / 1000).strftime("%H:%M"))
        time_lab.setObjectName("streamTime")
        time_lab.setFixedWidth(42)
        line.addWidget(time_lab)
        meta_parts: list[str] = []
        if session.target_app:
            meta_parts.append(_friendly_app_name(session.target_app))
        elif session.source:
            meta_parts.append(_source_description(session.source))
        meta_parts.append(f"{session.segment_count} 段")
        meta = _ElidedLabel(" · ".join(meta_parts))
        meta.setObjectName("streamMeta")
        line.addWidget(meta, 1)
        lay.addLayout(line)
        preview = _PreviewLabel(_search_excerpt(session.preview or "(无内容)", self._search_query))
        preview.setObjectName("streamPreview")
        lay.addWidget(preview)
        return row

    def _session_item_text(self, session: SessionSummary) -> str:
        preview = session.preview or "(无内容)"
        return f"{preview}\n{_format_dt(session.created_at)} · {session.segment_count} 段"

    def _perform_search(self) -> None:
        q = self._search_edit.text().strip()
        self._search_query = q
        self._session_limit = 50
        self.refresh()

    # ── detail ────────────────────────────────────────────────────

    def _set_detail_chips(self, texts: list[str]) -> None:
        while self._detail_chips.count():
            item = self._detail_chips.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._detail_chip_labels = []
        chip_css = _chip_qss()
        metrics = QFontMetrics(self.font())
        for text in texts:
            if not text:
                continue
            lab = QLabel(metrics.elidedText(text, Qt.TextElideMode.ElideMiddle, 240))
            lab.setToolTip(text)
            lab.setObjectName("streamChip")
            lab.setStyleSheet(chip_css)
            # Chips size to their content and wrap to a new row when the
            # detail pane is narrow; they are never squeezed or clipped.
            lab.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
            self._detail_chip_labels.append(lab)
            self._detail_chips.addWidget(lab)
        has_metadata = bool(self._detail_chip_labels)
        self._metadata_btn.setVisible(has_metadata)
        self._detail_context.setVisible(has_metadata)
        self._detail_chip_bar.setVisible(has_metadata and self._metadata_btn.isChecked())

    def _on_view_toggled(self, polished_checked: bool) -> None:
        self._view_polished = polished_checked
        item = self._single_selected_item()
        if item is not None:
            self._expand_session(item)
        self._apply_copy_action_styles(self._current_selection_has_polished())

    def _single_selected_item(self) -> QListWidgetItem | None:
        selected = [it for it in self._session_list.selectedItems() if it.data(Qt.ItemDataRole.UserRole)]
        return selected[0] if len(selected) == 1 else None

    def _expand_session(self, item: QListWidgetItem | None) -> None:
        if item is None:
            return
        session_id = item.data(Qt.ItemDataRole.UserRole)
        segments = _segments_in_order(self._get_session_segments_effective(session_id))
        summary = self._sessions_by_id.get(session_id)
        chip_texts: list[str] = []
        if summary is not None:
            self._detail_title.setText(_session_title(summary.created_at))
            context = [_friendly_app_name(summary.target_app), _source_description(summary.source)]
            self._detail_context.set_full_text(" · ".join(part for part in context if part))
            if summary.source:
                chip_texts.append(f"音频来源：{_source_description(summary.source)}")
            if summary.target_app:
                chip_texts.append(f"目标应用：{summary.target_app}")
        stats: list[str] = []
        if segments:
            first = segments[0]
            trigger = _trigger_chip_text(first.trigger_mode)
            if trigger:
                chip_texts.append(f"录音方式：{trigger}")
            if first.model:
                chip_texts.append(f"识别模型：{first.model}")
            stats.append(f"{len(segments)} 段")
            duration = _session_duration_ms(segments)
            if duration > 0:
                stats.append(_format_duration(duration))
        self._detail_stats.setText(" · ".join(stats))
        self._set_detail_chips(chip_texts)
        has_polished = _session_has_polished(segments)
        search_context = (session_id, self._search_query)
        if self._search_query and self._detail_search_context != search_context:
            query = self._search_query.casefold()
            matches_polished = any(query in _effective_text(segment).casefold() for segment in segments)
            matches_raw = any(query in segment.raw_text.casefold() for segment in segments)
            if has_polished and matches_polished != matches_raw:
                self._view_polished = matches_polished
                self._view_polished_btn.blockSignals(True)
                self._view_polished_btn.setChecked(matches_polished)
                self._view_raw_btn.setChecked(not matches_polished)
                self._view_polished_btn.blockSignals(False)
                self._apply_copy_action_styles(has_polished)
        self._detail_search_context = search_context
        self._view_bar.setVisible(has_polished)
        show_polished = self._view_polished if has_polished else False
        self._set_detail_document(
            html=self._render_segments_html(segments, show_polished),
            session_id=session_id,
        )

    def _set_detail_document(
        self,
        *,
        html: str | None = None,
        plain: str | None = None,
        session_id: str | None = None,
    ) -> None:
        """Replace the detail document and keep the latest turn in view.

        Segments render oldest-first, so the newest turn sits at the bottom.
        ``setHtml`` resets the scrollbar to the top; during a multi-turn
        session that refresh happens on every committed segment. Stay at the
        bottom while the reader is following the latest turn. If they have
        scrolled up into earlier turns, keep that offset across the refresh.
        """
        key = (html, plain, session_id)
        revision = self._details.document().revision()
        if getattr(self, "_detail_document_key", None) == (key, revision):
            return
        bar = self._details.verticalScrollBar()
        if session_id is not None:
            same = session_id == self._detail_session_id
            if same and not self._detail_follow_tail:
                self._detail_pending_anchor = bar.value()
            else:
                self._detail_follow_tail = True
                self._detail_pending_anchor = None
            self._detail_session_id = session_id
        else:
            self._detail_session_id = None
            self._detail_follow_tail = False
            self._detail_pending_anchor = None
        self._detail_scroll_lock = True
        self._detail_scroll_settle = True
        self._detail_scroll_gen += 1
        try:
            if html is not None:
                self._details.setHtml(html)
            else:
                self._details.setPlainText(plain or "")
        finally:
            self._detail_scroll_lock = False
        self._detail_document_key = (key, self._details.document().revision())
        # setHtml moves the caret to the start on a later event-loop turn,
        # which would undo a synchronous scroll. Re-apply after that reset.
        self._detail_scroll_timer.start(0)
        self._apply_detail_scroll()

    def _on_detail_scroll_timeout(self) -> None:
        self._finish_detail_scroll(self._detail_scroll_gen)

    def _finish_detail_scroll(self, generation: int) -> None:
        if generation != self._detail_scroll_gen:
            return
        self._apply_detail_scroll()
        if generation == self._detail_scroll_gen:
            self._detail_scroll_settle = False

    def _apply_detail_scroll(self) -> None:
        bar = self._details.verticalScrollBar()
        if self._detail_follow_tail:
            target = bar.maximum()
        elif self._detail_pending_anchor is not None:
            target = min(self._detail_pending_anchor, bar.maximum())
        else:
            return
        if bar.value() == target:
            return
        self._detail_scroll_lock = True
        bar.setValue(target)
        self._detail_scroll_lock = False

    def _on_detail_scrollbar_range_changed(self, _minimum: int, _maximum: int) -> None:
        if self._detail_scroll_lock:
            return
        # Layout after setHtml grows the range on a later pass. Re-apply once
        # the document height is known, including the first time the page is shown.
        self._apply_detail_scroll()

    def _on_detail_scrollbar_value_changed(self, value: int) -> None:
        if self._detail_scroll_lock or self._detail_scroll_settle:
            return
        bar = self._details.verticalScrollBar()
        maximum = bar.maximum()
        # setHtml briefly collapses the range to 0 before layout. That is not
        # the reader moving; leave the follow/anchor decision alone.
        if maximum <= 0:
            return
        self._detail_pending_anchor = None
        self._detail_follow_tail = value >= maximum - self._DETAIL_TAIL_SLACK

    def _render_segments_html(self, segments: list[SegmentRecord], show_polished: bool) -> str:
        from sayink.ui import design_tokens as tok

        mark_css = f"background: {tok.AMBER_SOFT}; color: {tok.TEXT};"
        blocks: list[str] = []
        many = len(segments) > 1
        show_speakers = history_shows_speakers(segments)
        for index, segment in enumerate(segments, start=1):
            if show_polished and segment.polished_text == _SUPPRESSED_POLISHED_MARKER:
                continue
            text = (segment.polished_text if show_polished else segment.raw_text) or ""
            text = text.strip() or (segment.raw_text or "").strip()
            if not text:
                continue
            if many or show_speakers:
                stamp = datetime.fromtimestamp(_segment_clock_ms(segment) / 1000).strftime("%H:%M:%S")
                if show_speakers:
                    head = _speaker_heading(segment)
                else:
                    head = str(index)
                caption = f"{head} · {stamp}"
                if segment.duration_ms:
                    caption += f" · {_format_duration(segment.duration_ms)}"
                blocks.append(
                    f'<p style="margin:0 0 4px 0; color:{tok.TEXT_DIM};'
                    f' font-size:{tok.TYPE_CAPTION}px; letter-spacing:0;">{html.escape(caption)}</p>'
                )
            body = _highlight_html(text, self._search_query, mark_css).replace("\n", "<br>")
            blocks.append(
                f'<p style="margin:0 0 {20 if many else 0}px 0; color:{tok.TEXT};'
                f' font-size:{tok.READING_FONT_PX}px; line-height:{tok.READING_LINE_HEIGHT}%;">{body}</p>'
            )
        return "".join(blocks)

    def _render_multi_selection_html(self, sessions: list[SessionSummary]) -> str:
        from sayink.ui import design_tokens as tok

        rows = [
            f'<p style="margin:0 0 14px 0; color:{tok.TEXT_SEC}; font-size:{tok.TYPE_BODY_SM}px;">'
            f'已选 {len(sessions)} 条会话，可一起复制、合并、转为会议、导出或删除。'
            '复制与合并会按时间从早到晚拼接最终文本，有润色时使用润色版。</p>'
        ]
        for session in sorted(sessions, key=lambda s: s.created_at, reverse=True):
            preview = html.escape(" ".join((session.preview or "(无内容)").split()))
            rows.append(
                f'<p style="margin:0 0 8px 0; font-size:{tok.TYPE_BODY_SM}px;">'
                f'<span style="color:{tok.TEXT_DIM};">{html.escape(_session_title(session.created_at))}</span>'
                f'&nbsp;&nbsp;<span style="color:{tok.TEXT};">{preview}</span></p>'
            )
        return "".join(rows)

    def _on_selection_changed(self) -> None:
        selected = self._selected_session_ids()
        count = len(selected)
        if self._editing_session and (self._selection_mode or count != 1 or selected[0] != self._editing_session_id):
            self._leave_inline_edit_on_row_change()
        if count > 1 and not self._selection_mode:
            self._set_selection_mode(True)
            return
        self._sync_row_selection()
        self._update_list_controls()
        has_polished = False
        if count == 1:
            has_polished = _session_has_polished(self._get_session_segments_effective(selected[0]))
        self._copy_btn.setEnabled(count >= 1 and not self._editing_session)
        self._apply_copy_action_styles(count == 1 and has_polished)
        polishing = self._polishing_session_id is not None
        self._polish_btn.setVisible(not self._selection_mode and not self._editing_session)
        self._polish_btn.setEnabled(count == 1 and not polishing)
        action = self._llm_action_label()
        if polishing and count == 1 and selected[0] == self._polishing_session_id:
            self._polish_btn.setText(f"正在{action}")
        else:
            self._polish_btn.setText(f"重新{action}" if has_polished else action)
        self._edit_btn.setVisible(not self._selection_mode)
        self._edit_btn.setEnabled(count == 1 and not polishing)
        self._edit_btn.setText("保存" if self._editing_session else "编辑")
        self._cancel_edit_btn.setVisible(self._editing_session and not self._selection_mode)
        self._merge_btn.setVisible(self._selection_mode)
        self._merge_btn.setEnabled(count >= 2)
        self._merge_btn.setText("合并")
        self._to_meeting_btn.setVisible(self._selection_mode)
        self._to_meeting_btn.setEnabled(count >= 1)
        self._to_meeting_btn.setText("转为会议")
        self._to_meeting_btn.setToolTip(
            "将勾选的多条会话按时间合并复制为一场会议，并打开会议工作台整理纪要"
            if self._selection_mode and count >= 2
            else "复制当前会话的分段生成会议记录，并打开会议工作台整理纪要"
        )
        self._to_meeting_action.setEnabled(count >= 1)
        self._export_btn.setVisible(not self._editing_session)
        self._export_btn.setEnabled(count > 0 and not self._editing_session)
        self._export_btn.setText(f"导出 {count} 条" if self._selection_mode else "导出")
        self._delete_btn.setEnabled(count > 0)
        self._delete_btn.setText(f"删除 {count} 条")
        self._delete_btn.setVisible(self._selection_mode)
        self._delete_action.setEnabled(count > 0)
        self._detail_more_btn.setVisible(not self._selection_mode and not self._editing_session)
        self._detail_more_btn.setEnabled(count > 0)
        for btn in (
            self._view_polished_btn,
            self._view_raw_btn,
            self._copy_btn,
            self._polish_btn,
            self._edit_btn,
            self._cancel_edit_btn,
            self._merge_btn,
            self._to_meeting_btn,
            self._export_btn,
            self._detail_more_btn,
            self._delete_btn,
            self._selection_mode_btn,
            self._manage_btn,
            self._metadata_btn,
        ):
            btn.ensurePolished()
            pad = 28 if btn is self._copy_btn else 24
            btn.setMinimumWidth(btn.fontMetrics().horizontalAdvance(btn.text()) + pad)
        self._view_bar.updateGeometry()
        if self._selection_mode:
            self._set_detail_chips([])
            self._detail_stats.clear()
            self._view_bar.hide()
            self._detail_title.setText(f"已选 {count} 条会话" if count else "选择要管理的会话")
            self._set_detail_document(html=self._render_multi_selection_html(self._selected_sessions()))
        elif count == 1:
            if not self._editing_session:
                self._expand_session(self._single_selected_item())
        elif self.session_count():
            self._set_detail_chips([])
            self._detail_stats.clear()
            self._view_bar.hide()
            self._detail_title.setText("会话详情")
            self._set_detail_document(plain="选择一条转写查看分段内容")
        else:
            self._detail_stats.clear()

    def _selected_session_ids(self) -> list[str]:
        return [
            item.data(Qt.ItemDataRole.UserRole)
            for item in self._session_list.selectedItems()
            if item.data(Qt.ItemDataRole.UserRole)
        ]

    def _selected_sessions(self) -> list[SessionSummary]:
        return [
            self._sessions_by_id[sid]
            for sid in self._selected_session_ids()
            if sid in self._sessions_by_id
        ]

    def set_open_session(self, session_id: str | None) -> None:
        """Session still receiving transcript rows; delete and merge leave it alone."""
        self._open_session_id = session_id or None

    def _active_session_id(self) -> str | None:
        ids = self._selected_session_ids()
        if len(ids) == 1:
            return ids[0]
        return None

    # ── actions ───────────────────────────────────────────────────

    def _map_edited_text_to_segments(self, segments: list[SegmentRecord], text: str) -> dict[int, str] | None:
        ordered = _segments_in_order(segments)
        if not ordered:
            return {}
        cleaned = text.strip()
        if len(ordered) == 1:
            return {ordered[0].seq: cleaned}
        parts = [p.strip() for p in re.split(r"\n\s*\n", cleaned) if p.strip()]
        if len(parts) > len(ordered):
            # A blank line inside one segment must not swallow every later segment.
            return None
        if len(parts) == len(ordered):
            return {seg.seq: part for seg, part in zip(ordered, parts, strict=True)}
        out: dict[int, str] = {ordered[0].seq: cleaned}
        for seg in ordered[1:]:
            out[seg.seq] = "\u200b"
        return out

    def _on_edit_clicked(self) -> None:
        if self._editing_session:
            self._save_inline_edit()
        else:
            self._start_inline_edit()

    def _settle_pending_merge(self) -> bool:
        """Write a merge still inside its undo window before anything edits,
        re-polishes, promotes or deletes those rows. While the merge is only
        previewed, the detail pane shows re-numbered virtual rows that do not
        exist in the DB; an edit mapped onto them would land on the wrong
        rows. Returns True when a merge was committed."""
        if not self._pending_merge:
            return False
        self._undo_timer.stop()
        self._commit_pending_merge_now()
        flush = getattr(self._store, "flush", None)
        if callable(flush):
            flush(timeout=2.0)
        self._toast.hide()
        return True

    def _start_inline_edit(self) -> None:
        session_id = self._active_session_id()
        if not session_id:
            return
        if self._settle_pending_merge():
            self.refresh()
            session_id = self._active_session_id() or session_id
        segments = _segments_in_order(self._get_session_segments_effective(session_id))
        if not segments:
            return
        show_polished = self._view_polished if _session_has_polished(segments) else False
        chunks = []
        for seg in segments:
            if show_polished and seg.polished_text == _SUPPRESSED_POLISHED_MARKER:
                continue
            t = (seg.polished_text if show_polished else seg.raw_text) or ""
            t = t.strip() or (seg.raw_text or "").strip()
            if t:
                chunks.append(t)
        self._editing_session = True
        self._editing_session_id = session_id
        self._editing_original_text = "\n\n".join(chunks)
        self._details.setReadOnly(False)
        self._paint_history_styles()
        self._details.setPlainText(self._editing_original_text)
        self._details.setFocus()
        self._on_selection_changed()

    def _exit_inline_edit_state(self) -> None:
        self._editing_session = False
        self._editing_session_id = None
        self._editing_original_text = ""
        self._details.setReadOnly(True)
        self._paint_history_styles()

    def _leave_inline_edit_on_row_change(self) -> None:
        """The user clicked another row mid-edit: keep what they typed (like
        the meeting page's draft autosave) instead of discarding it silently."""
        changed = self._details.toPlainText().strip() != self._editing_original_text.strip()
        if changed and self._details.toPlainText().strip():
            self._save_inline_edit(refresh=False)
            QTimer.singleShot(0, self.refresh)
        else:
            self._exit_inline_edit_state()

    def _cancel_inline_edit(self) -> None:
        if not self._editing_session:
            return
        self._exit_inline_edit_state()
        self._on_selection_changed()

    def _save_inline_edit(self, *, refresh: bool = True) -> None:
        if not self._editing_session or not self._editing_session_id:
            return
        session_id = self._editing_session_id
        new_text = self._details.toPlainText().strip()
        segments = _segments_in_order(self._get_session_segments_effective(session_id))
        if not new_text or not segments:
            self._exit_inline_edit_state()
        else:
            mapping = self._map_edited_text_to_segments(segments, new_text)
            if mapping is None:
                self._show_feedback("空行比段落多，修改未保存。请去掉多余空行后再保存。")
                return
            updater = getattr(self._store, "enqueue_update_polished", None)
            if callable(updater):
                updater(session_id, mapping)
            flush = getattr(self._store, "flush", None)
            saved = flush(timeout=2.0) if callable(flush) else True
            if saved is False:
                self._show_feedback("未能保存修改")
                return
            self._exit_inline_edit_state()
            self._view_polished = True
            self._view_polished_btn.blockSignals(True)
            self._view_polished_btn.setChecked(True)
            self._view_raw_btn.setChecked(False)
            self._view_polished_btn.blockSignals(False)
            self._show_feedback("已保存修改")
        if refresh:
            self.refresh()

    def _ensure_polisher(self):
        if self._polisher is None:
            from sayink.text_polisher import TextPolisher

            self._polisher = TextPolisher(self)
            self._polisher.polish_complete.connect(self._on_polish_complete)
            self._polisher.polish_error.connect(self._on_polish_error)
        return self._polisher

    def _llm_mode(self) -> str:
        from sayink.text_polisher import LLM_MODE_POLISH, normalize_llm_mode

        if self._config is None:
            return LLM_MODE_POLISH
        return normalize_llm_mode(self._config.get("llm.mode", LLM_MODE_POLISH))

    def _llm_action_label(self) -> str:
        """The AI action is whatever「AI 服务 → 处理方式」says, same as live dictation."""
        from sayink.text_polisher import LLM_MODE_CORRECT

        return "纠错" if self._llm_mode() == LLM_MODE_CORRECT else "润色"

    def _polish_selected_session(self) -> None:
        session_id = self._active_session_id()
        if not session_id or self._polishing_session_id is not None:
            return
        from sayink.text_polisher import polish_settings_complete, prompt_config_key

        config = self._config
        api_url = str(config.get("llm.api_url", "") or "") if config is not None else ""
        api_key = str(config.get("llm.api_key", "") or "") if config is not None else ""
        model_name = str(config.get("llm.model_name", "") or "") if config is not None else ""
        # Same prompt key, mode and glossary as the live pipeline, so the stored
        # text matches what the user would have got at dictation time.
        mode = self._llm_mode()
        prompt = str(config.get(prompt_config_key(mode), "") or "") if config is not None else ""
        glossary = config.get("stt.glossary", []) if config is not None else []
        if not polish_settings_complete(api_url, api_key, model_name):
            self._show_feedback("请先在 AI 服务中配置接口与模型")
            self.ai_settings_requested.emit()
            return
        if self._settle_pending_merge():
            self.refresh()
            session_id = self._active_session_id() or session_id
        segments = _segments_in_order(self._get_session_segments_effective(session_id))
        raw_parts = [s.raw_text.strip() for s in segments if s.raw_text.strip()]
        if not raw_parts:
            return
        self._polishing_session_id = session_id
        self._polishing_raw = "\n\n".join(raw_parts)
        self._polishing_mode = mode
        self._polishing_glossary = list(glossary) if isinstance(glossary, list) else []
        self._on_selection_changed()
        polisher = self._ensure_polisher()
        polisher.polish(
            self._polishing_raw, api_url, api_key, model_name, prompt,
            mode=self._polishing_mode, glossary=self._polishing_glossary,
            proxy_mode=config.get("llm.proxy_mode", "system") if config is not None else "system",
            proxy_bypass_suffixes=str(config.get("llm.proxy_bypass_suffixes", "") or "") if config is not None else "",
        )

    def _polish_rejection(self, polished_text: str) -> str:
        """Why the reply must not replace the stored text; empty when it may.

        The live pipeline falls back to the raw words when a reply fails the
        fidelity guard; the history action keeps the same bar instead of
        saving a rewrite that dropped a number or flipped a negation.
        """
        from sayink.asr_correction import correction_rejection_reason
        from sayink.polish_fidelity import polish_rejection_reason
        from sayink.text_polisher import LLM_MODE_CORRECT

        raw = getattr(self, "_polishing_raw", "")
        if not raw:
            return ""
        if getattr(self, "_polishing_mode", "") == LLM_MODE_CORRECT:
            return correction_rejection_reason(raw, polished_text, getattr(self, "_polishing_glossary", ()))
        return polish_rejection_reason(raw, polished_text)

    def _on_polish_complete(self, polished_text: str) -> None:
        session_id = self._polishing_session_id
        self._polishing_session_id = None
        if not session_id:
            self._on_selection_changed()
            return
        cleaned = (polished_text or "").strip()
        label = "纠错" if getattr(self, "_polishing_mode", "") == "correct" else "润色"
        reason = self._polish_rejection(cleaned) if cleaned else ""
        if reason:
            logger.warning("历史%s结果未通过保真检查（%s），未保存", label, reason)
            self._show_feedback(f"{label}结果未通过保真检查（{reason}），已保留原文")
            self.refresh()
            return
        if cleaned:
            segments = _segments_in_order(self._get_session_segments_effective(session_id))
            mapping = self._map_edited_text_to_segments(segments, cleaned)
            if mapping is None:
                self._show_feedback(f"{label}结果分段与原文不一致，未保存")
                self.refresh()
                return
            updater = getattr(self._store, "enqueue_update_polished", None)
            if callable(updater):
                updater(session_id, mapping)
            flush = getattr(self._store, "flush", None)
            if callable(flush):
                flush(timeout=2.0)
            self._view_polished = True
            self._view_polished_btn.blockSignals(True)
            self._view_polished_btn.setChecked(True)
            self._view_raw_btn.setChecked(False)
            self._view_polished_btn.blockSignals(False)
            self._show_feedback(f"已完成{label}")
        self.refresh()

    def _on_polish_error(self, error_msg: str) -> None:
        self._polishing_session_id = None
        self._on_selection_changed()
        self._show_feedback(error_msg or f"{self._llm_action_label()}失败，请稍后重试")

    def _promote_selected_to_meeting(self) -> None:
        ids = self._selected_session_ids()
        if not ids:
            return
        # The promoted meeting reads the rows on another connection: a merge
        # still in the writer queue would leave the newer sessions out of it.
        self._settle_pending_merge()
        from sayink.meeting_store import MeetingStore

        promoter = getattr(self._store, "create_meeting_from_sessions", None)
        if callable(promoter):
            record = promoter(ids)
        else:
            meeting_store = MeetingStore(self._store)
            record = meeting_store.create_meeting_from_sessions(ids)
            meeting_store.flush(timeout=2.0)
        if record is None:
            self._show_feedback("无法转为会议：未找到有效分段")
            return
        if self._selection_mode:
            self._set_selection_mode(False)
        self._show_feedback("已转为会议")
        self.meeting_created.emit(record.id)

    def _joined_effective_text(self, sessions: list[SessionSummary]) -> str:
        parts: list[str] = []
        for session in sorted(sessions, key=lambda item: item.created_at):
            body = _session_body(self._get_session_segments_effective(session.session_id)).strip()
            if body:
                parts.append(body)
        return "\n\n".join(parts)

    def _copy_joined_effective(self, sessions: list[SessionSummary]) -> None:
        text = self._joined_effective_text(sessions)
        if not text:
            return
        self._copy_with_feedback(text, f"已复制 {len(sessions)} 项")

    def _copy_with_feedback(self, text: str, done_text: str) -> None:
        self._show_feedback(done_text if copy_text(text) else COPY_FAILED_TEXT)

    def _copy_selected_raw(self) -> None:
        sessions = self._selected_sessions()
        if len(sessions) > 1:
            self._copy_joined_effective(sessions)
            return
        session_id = self._active_session_id()
        if not session_id:
            return
        segments = self._get_session_segments_effective(session_id)
        ordered = _segments_in_order(segments)
        show_speakers = history_shows_speakers(ordered)
        text = "\n\n".join(
            _labeled_text(segment, segment.raw_text, show_speakers=show_speakers)
            for segment in ordered
            if segment.raw_text.strip()
        )
        self._copy_with_feedback(text, "已复制原文" if _session_has_polished(segments) else "已复制")

    def _copy_selected_polished(self) -> None:
        session_id = self._active_session_id()
        if not session_id:
            return
        segments = self._get_session_segments_effective(session_id)
        self._copy_with_feedback(_session_body(segments), "已复制润色文本")

    def _copy_selected_effective(self) -> None:
        sessions = self._selected_sessions()
        if self._selection_mode:
            self._copy_joined_effective(sessions)
            return
        if self._copy_follows_raw_view(self._current_selection_has_polished()):
            self._copy_selected_raw()
        else:
            self._copy_selected_polished()

    def _show_feedback(self, text: str) -> None:
        self._transient_active = True
        self._feedback_label.setText(text)
        self._undo_btn.setVisible(False)
        self._toast.show()
        self._toast.reposition()
        self._feedback_timer.start(2200)

    def _end_transient_feedback(self) -> None:
        self._transient_active = False
        self._refresh_toast()

    def _refresh_toast(self) -> None:
        if self._transient_active:
            return
        if self._pending_delete:
            merged = "（之前的合并已生效）" if self._merge_committed_by_delete else ""
            self._feedback_label.setText(f"已删除 {len(self._pending_delete)} 项{merged} · 8 秒内可撤销删除")
            self._undo_btn.setVisible(True)
            self._toast.show()
            self._toast.reposition()
        elif self._pending_merge:
            self._feedback_label.setText(f"已合并 {len(self._pending_merge)} 项 · 8 秒内可撤销")
            self._undo_btn.setVisible(True)
            self._toast.show()
            self._toast.reposition()
        else:
            self._toast.hide()

    def _merge_selected_sessions(self) -> None:
        sessions = self._selected_sessions()
        if len(sessions) < 2:
            return
        if self._open_session_id and any(s.session_id == self._open_session_id for s in sessions):
            self._show_feedback("这场正在转写，请先结束再合并")
            return
        if self._pending_delete:
            self._commit_pending_delete_now()
        if self._pending_merge:
            self._commit_pending_merge_now()
        ordered = sorted(sessions, key=lambda s: s.created_at)
        target_id = ordered[0].session_id
        self._pending_merge = ordered
        self._set_selection_mode(False)
        self.refresh()
        for item in self.session_items():
            if item.data(Qt.ItemDataRole.UserRole) == target_id:
                self._session_list.setCurrentItem(item)
                item.setSelected(True)
                break
        self._transient_active = False
        self._feedback_timer.stop()
        self._refresh_toast()
        self._undo_timer.start()

    def _delete_selected_sessions(self) -> None:
        selected = self._selected_sessions()
        if not selected:
            return
        blocked = [s for s in selected if s.session_id == self._open_session_id]
        deletable = [s for s in selected if s not in blocked]
        if not deletable:
            self._show_feedback("这场正在转写，请先结束再删除")
            return
        # Deleting the merged row means the merge itself is final; the toast
        # says so, since its own undo window is forfeited here.
        self._merge_committed_by_delete = self._settle_pending_merge()
        # Undo replaces the confirmation dialog: one safety net, not two.
        ids = {s.session_id for s in deletable}
        self._pending_delete.extend(deletable)
        self._load_sessions([
            session for session in self._sessions_by_id.values()
            if session.session_id not in ids
        ])
        self._transient_active = False
        self._feedback_timer.stop()
        self._refresh_toast()
        self._undo_timer.start()

    def _undo_pending_action(self) -> None:
        if self._pending_delete:
            self._undo_pending_delete()
        elif self._pending_merge:
            self._undo_pending_merge()

    def _undo_pending_delete(self) -> None:
        if not self._pending_delete:
            return
        self._undo_timer.stop()
        self._pending_delete = []
        self._toast.hide()
        self.refresh()

    def _undo_pending_merge(self) -> None:
        if not self._pending_merge:
            return
        self._undo_timer.stop()
        self._pending_merge = []
        self._toast.hide()
        self.refresh()

    def flush_pending_delete(self) -> None:
        """Apply a delete or merge still inside its undo window, e.g. before quitting."""
        self._undo_timer.stop()
        self._commit_pending_undo()

    def _commit_pending_undo(self) -> None:
        if self._pending_delete:
            self._commit_pending_delete()
        elif self._pending_merge:
            self._commit_pending_merge()

    def _commit_pending_delete_now(self) -> None:
        if not self._pending_delete:
            return
        ids = [session.session_id for session in self._pending_delete]
        self._pending_delete = []
        self._store.enqueue_delete_sessions(ids)

    def _commit_pending_delete(self) -> None:
        if not self._pending_delete:
            return
        self._commit_pending_delete_now()
        self._toast.hide()
        QTimer.singleShot(250, self.refresh)

    def _commit_pending_merge_now(self) -> None:
        if len(self._pending_merge) < 2:
            self._pending_merge = []
            return
        ordered = sorted(self._pending_merge, key=lambda s: s.created_at)
        target_id = ordered[0].session_id
        ids = [s.session_id for s in ordered]
        self._pending_merge = []
        merger = getattr(self._store, "enqueue_merge_sessions", None)
        if callable(merger):
            merger(target_id, ids)

    def _commit_pending_merge(self) -> None:
        if not self._pending_merge:
            return
        self._commit_pending_merge_now()
        self._toast.hide()
        QTimer.singleShot(250, self.refresh)

    def _clear_all_history(self) -> None:
        counter = getattr(self._store, "count_sessions", None)
        count = counter() if callable(counter) else None
        if _ClearHistoryDialog(self, session_count=count).exec() != QDialog.DialogCode.Accepted:
            return
        self._undo_timer.stop()
        self._feedback_timer.stop()
        self._transient_active = False
        self._pending_delete = []
        self._pending_merge = []
        self._toast.hide()
        self._store.enqueue_delete_all()
        QTimer.singleShot(250, self.refresh)

    def _export_selected(self) -> None:
        sessions = self._selected_sessions()
        if not sessions:
            return
        if len(sessions) == 1:
            self._export_single(sessions[0])
        else:
            self._export_batch(sessions)

    def _export_single(self, session: SessionSummary) -> None:
        default = suggest_single_export_filename(session)
        path, selected_filter = QFileDialog.getSaveFileName(
            self,
            "导出历史",
            default,
            "Markdown (*.md);;纯文本 (*.txt)",
        )
        if not path:
            return
        kind = "txt" if (selected_filter or "").startswith("纯文本") or path.lower().endswith(".txt") else "md"
        if kind == "txt" and not path.lower().endswith(".txt"):
            path = str(Path(path).with_suffix(".txt"))
        segments = self._get_session_segments_effective(session.session_id)
        try:
            text = (
                build_single_session_plain(session, segments)
                if kind == "txt"
                else build_single_session_markdown(session, segments)
            )
            Path(path).write_text(text, encoding="utf-8")
        except OSError as exc:
            QMessageBox.critical(self, "导出失败", f"无法写入文件：{exc}")
            return
        self._show_feedback("已导出历史")

    def _export_batch(self, sessions: list[SessionSummary]) -> None:
        default = suggest_batch_export_filename()
        path, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "批量导出历史",
            default,
            "Markdown (*.md)",
        )
        if not path:
            return
        segments_by_session = {
            s.session_id: self._get_session_segments_effective(s.session_id)
            for s in sessions
        }
        try:
            Path(path).write_text(
                build_batch_export_markdown(sessions, segments_by_session),
                encoding="utf-8",
            )
        except OSError as exc:
            QMessageBox.critical(self, "导出失败", f"无法写入文件：{exc}")
            return
        self._show_feedback("已导出历史")

"""History browser window and Markdown export helpers."""

from __future__ import annotations

import html
import re
import time
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QEvent, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFontMetrics, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
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
from sayink.ui.clipboard import copy_text

COPY_FAILED_TEXT = "复制失败，请再试一次"


class _ElidedLabel(QLabel):
    """A single-line preview that keeps the full text available to assistive tools."""
    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self._full_text = " ".join(text.split())
        self.setToolTip(text)
        self.setAccessibleName(text)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
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


class _SessionList(QListWidget):
    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Qt can retain the pre-show width of setItemWidget children after a
        # splitter resize. Reflow before eliding their text to the new width.
        self.doItemsLayout()


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


def _effective_text(segment: SegmentRecord) -> str:
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
    """Labels appear only after a second person shows up in this session."""
    ids = {int(segment.speaker_id) for segment in segments if int(segment.speaker_id) > 0}
    return len(ids) >= 2


def _route_caption(route: str) -> str:
    return {"mic": "麦克风", "system": "电脑播放"}.get((route or "").strip(), "")


def _speaker_heading(segment: SegmentRecord) -> str:
    heading = f"说话人 {int(segment.speaker_id)}"
    route = _route_caption(segment.speaker_route)
    if route:
        return f"{heading} · {route}"
    return heading


def _labeled_text(segment: SegmentRecord, text: str, *, show_speakers: bool) -> str:
    body = text.strip()
    if not body:
        return ""
    if show_speakers and int(segment.speaker_id) > 0:
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
    "system": "电脑播放",
    "mixed": "混合",
    "file": "文件转写",
}

_TRIGGER_CHIP = {
    "continuous": "持续转写",
    "hotkey": "按住录音",
    "file_import": "导入文件",
}


def _source_chip_text(source: str | None) -> str:
    key = (source or "").strip()
    return _SOURCE_CHIP.get(key, key or "未知")


def _trigger_chip_text(mode: str | None) -> str:
    key = (mode or "").strip()
    return _TRIGGER_CHIP.get(key, key)


def _session_has_polished(segments: list[SegmentRecord]) -> bool:
    return any((s.polished_text or "").strip() for s in segments)


def _chip_qss() -> str:
    from sayink.ui import design_tokens as tok

    return (
        f"color: {tok.TEXT_DIM}; font-size: {tok.TYPE_CAPTION}px;"
        f" background: {tok.SURFACE_PEARL}; border-radius: {tok.RADIUS_XS}px;"
        f" padding: 2px 8px;"
    )


def suggest_single_export_filename(session: SessionSummary) -> str:
    return f"sayink-{_filename_dt(session.created_at)}.md"


def suggest_batch_export_filename(*, now_ms: int | None = None) -> str:
    stamp = int(time.time() * 1000) if now_ms is None else now_ms
    return f"sayink-export-{_filename_dt(stamp)}.md"


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

    def __init__(self, parent=None):
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
        body = QLabel("已保存的转写会全部删除，而且不能撤销。")
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

    ROW_HEIGHT = 58
    GROUP_HEADER_HEIGHT = 30
    # Treat a scrollbar this close to the end as "watching the latest turn".
    _DETAIL_TAIL_SLACK = 24

    def __init__(self, store, parent=None):
        super().__init__(parent)
        self._store = store
        self._sessions_by_id: dict[str, SessionSummary] = {}
        self._pending_delete: list[SessionSummary] = []
        self._search_query = ""
        self._history_enabled = True
        self._session_limit = 50
        self._has_more = False
        self._view_polished = True
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
            self._title_label.setStyleSheet(
                f"font-family: {tok.FONT_DISPLAY}; font-size: {tok.TYPE_TITLE_LG}px; font-weight: 700;"
                f" background: transparent; color: {tok.TEXT};"
            )
        if hasattr(self, "_right_pane"):
            self._right_pane.setStyleSheet("background: transparent;")
        if hasattr(self, "_search_edit"):
            self._search_edit.setStyleSheet(f"""
                QLineEdit {{
                    background: {tok.SURFACE_PEARL};
                    color: {tok.TEXT};
                    border: 1px solid {tok.HAIRLINE};
                    border-radius: {tok.RADIUS_MD}px;
                    padding: 6px 12px;
                    min-height: 36px;
                    font-size: {tok.TYPE_BODY_SM}px;
                }}
                QLineEdit:focus {{
                    border: 2px solid {tok.ACCENT};
                    padding: 5px 11px;
                }}
            """)
        if hasattr(self, "_session_list"):
            self._session_list.setStyleSheet(f"""
                QListWidget {{
                    background: transparent;
                    color: transparent;
                    border: 2px solid transparent;
                    padding: 0;
                    outline: none;
                }}
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
                    border-left: 3px solid {tok.ACCENT};
                    outline: none;
                }}
                QListWidget::item:hover:!selected {{
                    background: {tok.ROW_HOVER};
                    border: none;
                    outline: none;
                }}
                QListWidget:focus {{ border: {tok.FOCUS_RING}; outline: none; }}
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
            self._details.setStyleSheet(f"""
                QTextEdit {{
                    background: transparent;
                    color: {tok.TEXT};
                    border: none;
                    padding: 4px 0;
                    font-size: {tok.TYPE_TITLE_SM}px;
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
        if hasattr(self, "_more_btn"):
            self._more_btn.setStyleSheet(ss.BTN_GHOST_SM)
            self._history_preferences_btn.setStyleSheet(ss.BTN_ACCENT_SM)
        if hasattr(self, "_clear_all_btn"):
            self._clear_all_btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent;
                    color: {tok.TEXT_DIM};
                    border: none;
                    padding: 0 4px;
                    font-size: {tok.TYPE_FOOTNOTE}px;
                    min-height: 22px;
                    max-height: 22px;
                }}
                QPushButton:hover {{ color: {tok.RED}; }}
                QPushButton:focus {{
                    border: 2px solid {tok.ACCENT_FOCUS};
                    padding: 0 2px;
                }}
                QPushButton:disabled {{ color: {tok.TEXT_DIM}; }}
            """)
        if hasattr(self, "_detail_chip_labels"):
            self._detail_chip_bar.setStyleSheet("QWidget#historyMetadata { background: transparent; }")
            chip_css = _chip_qss()
            for lab in self._detail_chip_labels:
                lab.setStyleSheet(chip_css)
        if hasattr(self, "_view_bar"):
            self._paint_view_toggle()
        if hasattr(self, "_delete_btn"):
            self._delete_btn.setStyleSheet(ss.BTN_DANGER_SM)
        if hasattr(self, "_copy_raw_btn") and hasattr(self, "_copy_polished_btn"):
            self._apply_copy_action_styles(self._current_selection_has_polished())

    def _paint_view_toggle(self) -> None:
        from sayink.ui import design_tokens as tok

        css = (
            f"QPushButton {{ background: transparent; color: {tok.TEXT_SEC}; border: none;"
            f" border-radius: {tok.RADIUS_SM}px; padding: 0 12px; min-height: 26px; max-height: 26px;"
            f" font-size: {tok.TYPE_FOOTNOTE}px; font-weight: 400; }}"
            f"QPushButton:hover {{ color: {tok.TEXT}; }}"
            f"QPushButton:checked {{ background: {tok.SURFACE}; color: {tok.TEXT}; font-weight: 700; }}"
            f"QPushButton:focus {{ border: 2px solid {tok.ACCENT_FOCUS}; padding: 0 10px; }}"
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
        row.setStyleSheet(
            "QWidget#historyStreamRow {"
            f" background: {bg};"
            " border: none;"
            f" border-left: {tok.NAV_SELECTED_BAR_PX}px solid {bar};"
            " border-radius: 0;"
            "}"
        )

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

    def _current_selection_has_polished(self) -> bool:
        selected = self._selected_session_ids()
        if len(selected) != 1:
            return False
        return _session_has_polished(self._store.get_session_segments(selected[0]))

    def _apply_copy_action_styles(self, has_polished: bool) -> None:
        from sayink.ui import settings_styles as ss

        # The primary copy action is always the leftmost button; its label
        # says what it copies. The secondary「复制原文」only exists when a
        # polished text exists to contrast with.
        if has_polished:
            self._copy_polished_btn.setText("复制润色")
            self._copy_polished_btn.setStyleSheet(ss.BTN_PRIMARY)
            self._copy_raw_btn.setText("复制原文")
            self._copy_raw_btn.setStyleSheet(ss.BTN_GHOST_SM)
        else:
            self._copy_raw_btn.setText("复制")
            self._copy_raw_btn.setStyleSheet(ss.BTN_PRIMARY)
            self._copy_polished_btn.setStyleSheet(ss.BTN_GHOST_SM)
        self._copy_polished_btn.setVisible(has_polished)
        self._sync_copy_tooltips(has_polished)

    def _copy_follows_raw_view(self, has_polished: bool) -> bool:
        return not has_polished or not self._view_polished

    def _sync_copy_tooltips(self, has_polished: bool) -> None:
        """Ctrl+C copies what is on screen; only the matching button advertises it."""
        shortcut = " · Ctrl+C"
        if len(self._selected_session_ids()) > 1:
            self._copy_raw_btn.setToolTip(
                "按时间合并复制所选会话的最终文本（有润色时用润色版）" + shortcut
            )
            return
        raw_view = self._copy_follows_raw_view(has_polished)
        self._copy_raw_btn.setToolTip("复制识别原文" + (shortcut if raw_view else ""))
        self._copy_polished_btn.setToolTip("复制润色后的全文" + ("" if raw_view else shortcut))

    # ── layout ────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 20, 24, 20)
        outer.setSpacing(14)

        top = QHBoxLayout()
        top.setSpacing(16)
        self._title_label = QLabel("历史")
        top.addWidget(self._title_label, 1, Qt.AlignmentFlag.AlignVCenter)
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("搜索转写内容")
        self._search_edit.setAccessibleName("搜索转写内容")
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.setToolTip("搜索转写内容 · Ctrl+F")
        self._search_edit.setFixedWidth(280)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(200)
        self._search_edit.textChanged.connect(lambda: self._search_timer.start())
        self._search_timer.timeout.connect(self._perform_search)
        top.addWidget(self._search_edit, 0, Qt.AlignmentFlag.AlignVCenter)
        outer.addLayout(top)

        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.setChildrenCollapsible(False)
        self._splitter.setHandleWidth(5)
        split = self._splitter

        # ── left: session stream ──
        stream = QWidget()
        self._stream_host = stream
        self._left_pane = stream
        stream.setObjectName("historyLeftPane")
        stream_lay = QVBoxLayout(stream)
        stream_lay.setContentsMargins(0, 0, 0, 0)
        stream_lay.setSpacing(6)
        stream.setMinimumWidth(240)
        self._summary_row = QHBoxLayout()
        self._summary_row.setContentsMargins(0, 0, 0, 0)
        self._summary_row.setSpacing(8)
        self._list_summary = QLabel()
        self._list_summary.setContentsMargins(13, 0, 0, 0)
        self._summary_row.addWidget(self._list_summary, 1)
        self._clear_all_btn = QPushButton("清空全部历史")
        self._clear_all_btn.setToolTip("删除全部会话，此操作不可撤销")
        self._clear_all_btn.setAccessibleName("清空全部历史")
        self._clear_all_btn.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self._clear_all_btn.clicked.connect(self._clear_all_history)
        self._summary_row.addWidget(self._clear_all_btn, 0, Qt.AlignmentFlag.AlignRight)
        stream_lay.addLayout(self._summary_row)
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

        tools = QHBoxLayout()
        tools.setSpacing(8)
        self._copy_polished_btn = QPushButton("复制润色")
        self._copy_polished_btn.setToolTip("复制润色后的全文")
        self._copy_polished_btn.clicked.connect(self._copy_selected_polished)
        tools.addWidget(self._copy_polished_btn)
        self._copy_raw_btn = QPushButton("复制")
        self._copy_raw_btn.setToolTip("复制识别原文 · Ctrl+C")
        self._copy_raw_btn.clicked.connect(self._copy_selected_raw)
        tools.addWidget(self._copy_raw_btn)
        tools.addStretch(1)
        self._export_btn = QPushButton("导出")
        self._export_btn.setToolTip("将选中的会话导出为 Markdown 文件")
        self._export_btn.clicked.connect(self._export_selected)
        tools.addWidget(self._export_btn)
        self._delete_btn = QPushButton("删除")
        self._delete_btn.setToolTip("删除选中的会话，8 秒内可撤销 · Delete")
        self._delete_btn.clicked.connect(self._delete_selected_sessions)
        tools.addWidget(self._delete_btn)
        right_lay.addLayout(tools)

        title_row = QHBoxLayout()
        title_row.setSpacing(12)
        self._detail_title = QLabel("会话详情")
        self._detail_title.setWordWrap(True)
        title_row.addWidget(self._detail_title, 1)
        self._detail_stats = QLabel("")
        self._detail_stats.setObjectName("historyDetailStats")
        title_row.addWidget(self._detail_stats, 0, Qt.AlignmentFlag.AlignBottom)
        right_lay.addLayout(title_row)

        self._detail_chip_bar = QWidget()
        self._detail_chip_bar.setObjectName("historyMetadata")
        self._detail_chips = QHBoxLayout(self._detail_chip_bar)
        self._detail_chips.setContentsMargins(0, 0, 0, 0)
        self._detail_chips.setSpacing(6)
        self._detail_chip_host = self._detail_chips
        self._detail_chip_labels: list[QLabel] = []
        self._detail_chips.addStretch()
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
            self._view_group.addButton(btn)
            view_lay.addWidget(btn)
        self._view_polished_btn.setChecked(True)
        self._view_polished_btn.toggled.connect(self._on_view_toggled)
        view_wrap = QHBoxLayout()
        view_wrap.setContentsMargins(0, 0, 0, 0)
        view_wrap.addWidget(self._view_bar, 0, Qt.AlignmentFlag.AlignLeft)
        view_wrap.addStretch(1)
        right_lay.addLayout(view_wrap)
        self._view_bar.hide()

        self._details = QTextEdit()
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
        self._right_pane.setMinimumWidth(360)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        split.setSizes([280, 440])
        outer.addWidget(split, 1)

        # ── overlay toast (copy feedback + undo delete) ──
        self._toast = _Toast(self)
        self._undo_bar = self._toast
        self._feedback_label = self._toast.label
        self._undo_label = self._feedback_label
        self._undo_btn = self._toast.button
        self._undo_btn.clicked.connect(self._undo_pending_delete)
        self._feedback_timer = QTimer(self)
        self._feedback_timer.setSingleShot(True)
        self._feedback_timer.timeout.connect(self._end_transient_feedback)
        self._undo_timer = QTimer(self)
        self._undo_timer.setSingleShot(True)
        self._undo_timer.setInterval(8000)
        self._undo_timer.timeout.connect(self._commit_pending_delete)

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
        self._paint_history_styles()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "_toast") and self._toast.isVisible():
            self._toast.reposition()

    def _sync_clear_all_enabled(self) -> None:
        self._clear_all_btn.setEnabled(
            self.session_count() > 0 or bool(self._search_query) or bool(self._pending_delete)
        )

    # ── data → list ───────────────────────────────────────────────

    def refresh(self) -> None:
        if self._search_query:
            sessions = self._store.search_sessions(self._search_query)
            self._has_more = False
        else:
            sessions = self._store.list_sessions(limit=self._session_limit + 1)
            self._has_more = len(sessions) > self._session_limit
            sessions = sessions[:self._session_limit]
        self._load_sessions(sessions)

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
        pending = {s.session_id for s in self._pending_delete}
        sessions = [s for s in sessions if s.session_id not in pending]
        # Rebuilding the list clears the selection first. That signal would
        # treat the open session as closed and snap the detail back to the top
        # on the next paint. Restore selection, then refresh the detail once.
        self._session_list.blockSignals(True)
        try:
            self._session_list.clear()
            self._history_preferences_btn.setVisible(
                not sessions and not self._search_query and not self._history_enabled
            )
            self._more_btn.setVisible(self._has_more and not self._search_query)
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
                else:
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
        count = len(sessions)
        summary = f"{count} 场匹配" if self._search_query else f"{count} 场最近会话"
        if count > 1:
            summary += " · 可多选"
        self._list_summary.setText(summary)
        self._sync_clear_all_enabled()
        self._restyle_rows()
        self._on_selection_changed()

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
        lay = QVBoxLayout(row)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(3)

        line = QHBoxLayout()
        line.setSpacing(10)
        time_lab = QLabel(datetime.fromtimestamp(session.created_at / 1000).strftime("%H:%M"))
        time_lab.setObjectName("streamTime")
        time_lab.setFixedWidth(42)
        line.addWidget(time_lab)
        preview = _ElidedLabel(session.preview or "(无内容)")
        preview.setObjectName("streamPreview")
        line.addWidget(preview, 1)
        lay.addLayout(line)

        meta_parts: list[str] = []
        if session.target_app:
            meta_parts.append(session.target_app)
        source = _source_chip_text(session.source)
        if source not in ("麦克风", "未知"):
            meta_parts.append(source)
        meta_parts.append(f"{session.segment_count} 段")
        meta = _ElidedLabel(" · ".join(meta_parts))
        meta.setObjectName("streamMeta")
        meta_line = QHBoxLayout()
        meta_line.setSpacing(10)
        meta_line.addSpacing(52)
        meta_line.addWidget(meta, 1)
        lay.addLayout(meta_line)
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
            lab = QLabel(metrics.elidedText(text, Qt.TextElideMode.ElideMiddle, 150))
            lab.setToolTip(text)
            lab.setObjectName("streamChip")
            lab.setStyleSheet(chip_css)
            # Chips size to their content; they must never be squeezed into
            # truncation by the layout.
            lab.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
            self._detail_chip_labels.append(lab)
            self._detail_chips.addWidget(lab)
        self._detail_chips.addStretch()
        self._detail_chip_bar.setVisible(bool(self._detail_chip_labels))

    def _on_view_toggled(self, polished_checked: bool) -> None:
        self._view_polished = polished_checked
        item = self._single_selected_item()
        if item is not None:
            self._expand_session(item)
        self._sync_copy_tooltips(self._current_selection_has_polished())

    def _single_selected_item(self) -> QListWidgetItem | None:
        selected = [it for it in self._session_list.selectedItems() if it.data(Qt.ItemDataRole.UserRole)]
        return selected[0] if len(selected) == 1 else None

    def _expand_session(self, item: QListWidgetItem | None) -> None:
        if item is None:
            return
        session_id = item.data(Qt.ItemDataRole.UserRole)
        segments = _segments_in_order(self._store.get_session_segments(session_id))
        summary = self._sessions_by_id.get(session_id)
        chip_texts: list[str] = []
        if summary is not None:
            self._detail_title.setText(_session_title(summary.created_at))
            if summary.source:
                chip_texts.append(_source_chip_text(summary.source))
            if summary.target_app:
                chip_texts.append(summary.target_app)
        stats: list[str] = []
        if segments:
            first = segments[0]
            trigger = _trigger_chip_text(first.trigger_mode)
            if trigger:
                chip_texts.append(trigger)
            if first.model:
                chip_texts.append(first.model)
            stats.append(f"{len(segments)} 段")
            duration = _session_duration_ms(segments)
            if duration > 0:
                stats.append(_format_duration(duration))
        self._detail_stats.setText(" · ".join(stats))
        self._set_detail_chips(chip_texts)
        has_polished = _session_has_polished(segments)
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
            text = (segment.polished_text if show_polished else segment.raw_text) or ""
            text = text.strip() or (segment.raw_text or "").strip()
            if not text:
                continue
            if many or show_speakers:
                stamp = datetime.fromtimestamp(segment.created_at / 1000).strftime("%H:%M:%S")
                if show_speakers and int(segment.speaker_id) > 0:
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
                f' font-size:{tok.TYPE_TITLE_SM}px; line-height:165%;">{body}</p>'
            )
        return "".join(blocks)

    def _render_multi_selection_html(self, sessions: list[SessionSummary]) -> str:
        from sayink.ui import design_tokens as tok

        rows = [
            f'<p style="margin:0 0 14px 0; color:{tok.TEXT_SEC}; font-size:{tok.TYPE_BODY_SM}px;">'
            f'已选 {len(sessions)} 项，可一起复制、导出或删除。</p>'
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
        self._sync_row_selection()
        selected = self._selected_session_ids()
        count = len(selected)
        has_polished = False
        if count == 1:
            has_polished = _session_has_polished(self._store.get_session_segments(selected[0]))
        self._copy_raw_btn.setEnabled(count >= 1)
        self._copy_polished_btn.setEnabled(count == 1 and has_polished)
        self._apply_copy_action_styles(count == 1 and has_polished)
        self._export_btn.setEnabled(count > 0)
        self._delete_btn.setEnabled(count > 0)
        if count == 1:
            self._expand_session(self._single_selected_item())
        elif count > 1:
            self._set_detail_chips([])
            self._detail_stats.clear()
            self._view_bar.hide()
            self._detail_title.setText(f"已选 {count} 项")
            self._set_detail_document(html=self._render_multi_selection_html(self._selected_sessions()))
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

    def _active_session_id(self) -> str | None:
        ids = self._selected_session_ids()
        if len(ids) == 1:
            return ids[0]
        return None

    # ── actions ───────────────────────────────────────────────────

    def _joined_effective_text(self, sessions: list[SessionSummary]) -> str:
        parts: list[str] = []
        for session in sorted(sessions, key=lambda item: item.created_at):
            body = _session_body(self._store.get_session_segments(session.session_id)).strip()
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
        segments = self._store.get_session_segments(session_id)
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
        segments = self._store.get_session_segments(session_id)
        self._copy_with_feedback(_session_body(segments), "已复制润色文本")

    def _copy_selected_effective(self) -> None:
        sessions = self._selected_sessions()
        if len(sessions) > 1:
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
            self._feedback_label.setText(f"已删除 {len(self._pending_delete)} 项 · 8 秒内可撤销")
            self._undo_btn.setVisible(True)
            self._toast.show()
            self._toast.reposition()
        else:
            self._toast.hide()

    def _delete_selected_sessions(self) -> None:
        ids = self._selected_session_ids()
        if not ids:
            return
        # Undo replaces the confirmation dialog: one safety net, not two.
        self._pending_delete.extend(self._selected_sessions())
        self._load_sessions([
            session for session in self._sessions_by_id.values()
            if session.session_id not in ids
        ])
        self._transient_active = False
        self._feedback_timer.stop()
        self._refresh_toast()
        self._undo_timer.start()

    def _undo_pending_delete(self) -> None:
        if not self._pending_delete:
            return
        self._undo_timer.stop()
        self._pending_delete = []
        self._toast.hide()
        self.refresh()

    def flush_pending_delete(self) -> None:
        """Apply a delete still inside its undo window, e.g. before quitting."""
        self._undo_timer.stop()
        self._commit_pending_delete()

    def _commit_pending_delete(self) -> None:
        if not self._pending_delete:
            return
        ids = [session.session_id for session in self._pending_delete]
        self._pending_delete = []
        self._toast.hide()
        self._store.enqueue_delete_sessions(ids)
        QTimer.singleShot(250, self.refresh)

    def _clear_all_history(self) -> None:
        if _ClearHistoryDialog(self).exec() != QDialog.DialogCode.Accepted:
            return
        self._undo_timer.stop()
        self._feedback_timer.stop()
        self._transient_active = False
        self._pending_delete = []
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
        path, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "导出历史",
            default,
            "Markdown (*.md)",
        )
        if not path:
            return
        segments = self._store.get_session_segments(session.session_id)
        try:
            Path(path).write_text(
                build_single_session_markdown(session, segments),
                encoding="utf-8",
            )
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
            s.session_id: self._store.get_session_segments(s.session_id)
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

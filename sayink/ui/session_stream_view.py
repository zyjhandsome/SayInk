"""Shared virtualized stream list (``QListView`` + ``QStyledItemDelegate``) and row chrome helpers.

Provides zero-widget-per-row virtualized rendering for large session/meeting streams
with keyboard selection, batch-check painting, group section headers, and theme-token
adherence, alongside shared stream-row CSS helpers used by ``HistoryWindow`` and ``MeetingPage``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from PyQt6.QtCore import QAbstractListModel, QModelIndex, QRect, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QFont, QPainter, QPen
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QListView,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QWidget,
)

from sayink.ui import design_tokens as tok

ROLE_ITEM_ID = int(Qt.ItemDataRole.UserRole)
ROLE_IS_HEADER = int(Qt.ItemDataRole.UserRole) + 1
ROLE_TITLE = int(Qt.ItemDataRole.UserRole) + 2
ROLE_META = int(Qt.ItemDataRole.UserRole) + 3
ROLE_BADGE = int(Qt.ItemDataRole.UserRole) + 4


@dataclass(frozen=True)
class StreamRowSpec:
    """Immutable row model entry for a session/meeting item or a date group header."""

    item_id: str
    title: str
    meta: str = ""
    badge: str = ""
    tooltip: str = ""
    is_header: bool = False


def build_stream_row_wash_css(
    row_object_name: str,
    *,
    selected: bool,
    title_object_name: str = "",
    meta_object_name: str = "",
) -> str:
    """Shared left-accent wash stylesheet shared across History and Meeting stream rows."""
    bg = tok.ACCENT_SOFT if selected else "transparent"
    bar = tok.ACCENT if selected else "transparent"
    css = (
        f"QWidget#{row_object_name} {{"
        f" background: {bg};"
        " border: none;"
        f" border-left: {tok.NAV_SELECTED_BAR_PX}px solid {bar};"
        " border-radius: 0;"
        "}"
    )
    if title_object_name:
        css += (
            f"QLabel#{title_object_name} {{"
            f" color: {tok.TEXT}; font-size: {tok.TYPE_BODY_SM}px; background: transparent;"
            "}"
        )
    if meta_object_name:
        css += (
            f"QLabel#{meta_object_name} {{"
            f" color: {tok.TEXT_SEC}; font-size: {tok.TYPE_CAPTION}px; background: transparent;"
            "}"
        )
    return css


class SessionStreamModel(QAbstractListModel):
    """Lightweight flat list model storing ``StreamRowSpec`` items without per-row QWidgets."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[StreamRowSpec] = []

    @property
    def rows(self) -> tuple[StreamRowSpec, ...]:
        return tuple(self._rows)

    def set_rows(self, rows: Sequence[StreamRowSpec]) -> None:
        incoming = list(rows)
        if incoming == self._rows:
            return
        self.beginResetModel()
        self._rows = incoming
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        if parent.isValid():
            return 0
        return len(self._rows)

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return Qt.ItemFlag.NoItemFlags
        spec = self._rows[index.row()]
        if spec.is_header:
            return Qt.ItemFlag.ItemIsEnabled
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def data(self, index: QModelIndex, role: int = int(Qt.ItemDataRole.DisplayRole)) -> Any:
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        spec = self._rows[index.row()]
        if role in (int(Qt.ItemDataRole.DisplayRole), int(Qt.ItemDataRole.AccessibleTextRole)):
            return spec.title if spec.is_header else f"{spec.title}\n{spec.meta}".strip()
        if role == int(Qt.ItemDataRole.ToolTipRole):
            return spec.tooltip or None
        if role == ROLE_ITEM_ID:
            return "" if spec.is_header else spec.item_id
        if role == ROLE_IS_HEADER:
            return spec.is_header
        if role == ROLE_TITLE:
            return spec.title
        if role == ROLE_META:
            return spec.meta
        if role == ROLE_BADGE:
            return spec.badge
        return None


class SessionStreamDelegate(QStyledItemDelegate):
    """Zero-allocation painter for ``SessionStreamModel`` rows and date headers."""

    HEADER_HEIGHT = 28
    ROW_HEIGHT = 56

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.batch_mode = False

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        is_header = bool(index.data(ROLE_IS_HEADER))
        return QSize(max(120, option.rect.width()), self.HEADER_HEIGHT if is_header else self.ROW_HEIGHT)

    def _paint_header(self, painter: QPainter, rect: QRect, title: str) -> None:
        font = QFont(tok.UI_FONT_FAMILY)
        font.setPixelSize(tok.TYPE_CAPTION)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(tok.qcolor_from_token(tok.TEXT_DIM))
        text_rect = rect.adjusted(10, 6, -10, -2)
        elided = painter.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, max(20, text_rect.width()))
        painter.drawText(text_rect, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), elided)

    def _paint_batch_box(self, painter: QPainter, box_rect: QRect, selected: bool) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        border_col = tok.qcolor_from_token(tok.ACCENT if selected else tok.CONTROL_BORDER)
        pen = QPen(border_col, 1.5)
        painter.setPen(pen)
        painter.setBrush(tok.qcolor_from_token(tok.ACCENT) if selected else Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(box_rect, 4.0, 4.0)
        if selected:
            mark_pen = QPen(tok.qcolor_from_token("#FFFFFF"), 1.8)
            painter.setPen(mark_pen)
            x, y, w, h = box_rect.x(), box_rect.y(), box_rect.width(), box_rect.height()
            painter.drawLine(x + w // 4, y + h // 2, x + (w * 9) // 20, y + (h * 3) // 4)
            painter.drawLine(x + (w * 9) // 20, y + (h * 3) // 4, x + (w * 3) // 4, y + h // 4)
        painter.restore()

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        painter.save()
        rect = option.rect
        if bool(index.data(ROLE_IS_HEADER)):
            self._paint_header(painter, rect, str(index.data(ROLE_TITLE) or ""))
            painter.restore()
            return

        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if selected:
            painter.fillRect(rect, tok.qcolor_from_token(tok.ACCENT_SOFT))
            painter.fillRect(
                QRect(rect.left(), rect.top(), tok.NAV_SELECTED_BAR_PX, rect.height()),
                tok.qcolor_from_token(tok.ACCENT),
            )
        elif hovered:
            painter.fillRect(rect, tok.qcolor_from_token(tok.ROW_HOVER))

        left_pad = 10
        if self.batch_mode:
            box_side = 16
            box_rect = QRect(
                rect.left() + left_pad,
                rect.top() + (rect.height() - box_side) // 2,
                box_side,
                box_side,
            )
            self._paint_batch_box(painter, box_rect, selected)
            left_pad += box_side + 8

        title = str(index.data(ROLE_TITLE) or "")
        meta = str(index.data(ROLE_META) or "")
        title_font = QFont(tok.UI_FONT_FAMILY)
        title_font.setPixelSize(tok.TYPE_BODY_SM)
        painter.setFont(title_font)
        painter.setPen(tok.qcolor_from_token(tok.TEXT))
        title_rect = QRect(rect.left() + left_pad, rect.top() + 7, max(20, rect.width() - left_pad - 10), 22)
        painter.drawText(
            title_rect,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            painter.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, title_rect.width()),
        )

        meta_font = QFont(tok.UI_FONT_FAMILY)
        meta_font.setPixelSize(tok.TYPE_CAPTION)
        painter.setFont(meta_font)
        painter.setPen(tok.qcolor_from_token(tok.TEXT_SEC))
        meta_rect = QRect(rect.left() + left_pad, rect.top() + 30, max(20, rect.width() - left_pad - 10), 18)
        painter.drawText(
            meta_rect,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            painter.fontMetrics().elidedText(meta, Qt.TextElideMode.ElideRight, meta_rect.width()),
        )
        painter.restore()


class SessionStreamListView(QListView):
    """Virtualized ``QListView`` owning a ``SessionStreamModel`` and ``SessionStreamDelegate``."""

    selection_ids_changed = pyqtSignal(tuple)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._stream_model = SessionStreamModel(self)
        self._delegate = SessionStreamDelegate(self)
        self.setModel(self._stream_model)
        self.setItemDelegate(self._delegate)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setUniformItemSizes(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setMouseTracking(True)
        sel_model = self.selectionModel()
        if sel_model is not None:
            sel_model.selectionChanged.connect(self._emit_selected_ids)

    @property
    def batch_mode(self) -> bool:
        return self._delegate.batch_mode

    @batch_mode.setter
    def batch_mode(self, enabled: bool) -> None:
        self._delegate.batch_mode = bool(enabled)
        self.viewport().update()

    def set_rows(self, rows: Sequence[StreamRowSpec]) -> None:
        self._stream_model.set_rows(rows)

    def selected_ids(self) -> list[str]:
        sel_model = self.selectionModel()
        if sel_model is None:
            return []
        chosen = {idx.row() for idx in sel_model.selectedIndexes()}
        out: list[str] = []
        for row_idx, spec in enumerate(self._stream_model.rows):
            if row_idx in chosen and not spec.is_header and spec.item_id:
                out.append(spec.item_id)
        return out

    def select_id(self, item_id: str) -> bool:
        sel_model = self.selectionModel()
        if sel_model is None or not item_id:
            return False
        for row_idx, spec in enumerate(self._stream_model.rows):
            if not spec.is_header and spec.item_id == item_id:
                idx = self._stream_model.index(row_idx, 0)
                self.setCurrentIndex(idx)
                return True
        return False

    def _emit_selected_ids(self, *_args: object) -> None:
        self.selection_ids_changed.emit(tuple(self.selected_ids()))


__all__ = [
    "ROLE_BADGE",
    "ROLE_IS_HEADER",
    "ROLE_ITEM_ID",
    "ROLE_META",
    "ROLE_TITLE",
    "SessionStreamDelegate",
    "SessionStreamListView",
    "SessionStreamModel",
    "StreamRowSpec",
    "build_stream_row_wash_css",
]

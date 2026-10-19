"""Left-to-right layout that wraps onto new rows instead of clipping."""

from PyQt6.QtCore import QEvent, QPoint, QRect, QSize, Qt, QTimer
from PyQt6.QtWidgets import QLayout, QLayoutItem, QSizePolicy, QWidget


class FlowLayout(QLayout):
    def __init__(self, parent: QWidget | None = None, spacing: int = 6) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self.setSpacing(spacing)

    def addItem(self, item: QLayoutItem) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int) -> QLayoutItem | None:
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientation:
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._arrange(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._arrange(rect, apply=True)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(), margins.top() + margins.bottom())

    def _arrange(self, rect: QRect, *, apply: bool) -> int:
        margins = self.contentsMargins()
        area = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        x, y, row_height = area.x(), area.y(), 0
        gap = self.spacing()
        for item in self._items:
            if item.isEmpty():
                continue
            hint = item.sizeHint()
            if x > area.x() and x + hint.width() > area.right() + 1:
                x = area.x()
                y += row_height + gap
                row_height = 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + gap
            row_height = max(row_height, hint.height())
        return y + row_height - rect.y() + margins.bottom()


class WrappingToolBar(QWidget):
    """One row of actions when they fit; otherwise the extras wrap onto the next row.

    ``left`` packs from the start, ``right`` stays on the trailing edge while the
    row still fits, and ``flex`` absorbs the space between them. A row that
    cannot hold every visible control wraps instead of drawing the controls on
    top of each other.
    """

    def __init__(self, parent: QWidget | None = None, *, spacing: int = 6) -> None:
        super().__init__(parent)
        self._spacing = spacing
        self._entries: list[tuple[QWidget, str]] = []
        self._pending = False
        self._arranging = False
        policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def add(self, widget: QWidget, *, role: str = "left") -> None:
        widget.setParent(self)
        widget.installEventFilter(self)
        self._entries.append((widget, role))
        self._schedule()

    def indexOf(self, widget: QWidget) -> int:
        for index, (entry, _role) in enumerate(self._entries):
            if entry is widget:
                return index
        return -1

    def event(self, event: QEvent) -> bool:
        # Text and visibility changes post LayoutRequest here. The bar is not a
        # QLayout, so nothing else will reflow the buttons.
        if event.type() == QEvent.Type.LayoutRequest and not self._arranging:
            self._schedule()
        return super().event(event)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._arrange(width, apply=False)

    def sizeHint(self) -> QSize:
        return QSize(self._row_width(self._visible()), self._arrange(10**6, apply=False))

    def minimumSizeHint(self) -> QSize:
        visible = self._visible()
        if not visible:
            return QSize(0, 0)
        widest = max(self._hint(widget).width() for widget, _role in visible)
        tallest = max(self._hint(widget).height() for widget, _role in visible)
        return QSize(widest, tallest)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._arrange(self.width(), apply=True)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._arrange(self.width(), apply=True)

    def eventFilter(self, obj, event) -> bool:
        if event.type() in (QEvent.Type.Show, QEvent.Type.Hide, QEvent.Type.LayoutRequest):
            self._schedule()
        return False

    def _schedule(self) -> None:
        if self._pending:
            return
        self._pending = True
        QTimer.singleShot(0, self._flush)

    def _flush(self) -> None:
        self._pending = False
        self.updateGeometry()
        if self.isVisible() or self.width() > 0:
            self._arrange(self.width(), apply=True)

    def _visible(self) -> list[tuple[QWidget, str]]:
        return [(widget, role) for widget, role in self._entries if not widget.isHidden()]

    def _hint(self, widget: QWidget) -> QSize:
        hint = widget.sizeHint()
        if hint.width() <= 0 or hint.height() <= 0:
            hint = hint.expandedTo(widget.minimumSizeHint())
        width = hint.width()
        if widget.minimumWidth() > 0:
            width = max(width, widget.minimumWidth())
        if 0 < widget.maximumWidth() < 16_777_215:
            width = min(width, widget.maximumWidth())
        hint.setWidth(width)
        return hint

    def _flex_min(self, widget: QWidget) -> int:
        if widget.minimumWidth() > 0:
            return widget.minimumWidth()
        if widget.sizePolicy().horizontalPolicy() == QSizePolicy.Policy.Ignored:
            return 0
        return min(widget.minimumSizeHint().width(), 120)

    def _row_width(self, items: list[tuple[QWidget, str]]) -> int:
        if not items:
            return 0
        gaps = self._spacing * (len(items) - 1)
        return sum(self._hint(widget).width() for widget, _role in items) + gaps

    def _arrange(self, width: int, *, apply: bool) -> int:
        if self._arranging:
            return self.height()
        items = self._visible()
        if not items:
            return 0
        self._arranging = True
        try:
            return self._arrange_items(items, width, apply=apply)
        finally:
            self._arranging = False

    def _arrange_items(self, items: list[tuple[QWidget, str]], width: int, *, apply: bool) -> int:
        gap = self._spacing
        hints = [(widget, role, self._hint(widget)) for widget, role in items]
        row_h = max(hint.height() for _widget, _role, hint in hints)
        if width <= 0:
            return row_h

        left = [(widget, hint) for widget, role, hint in hints if role == "left"]
        right = [(widget, hint) for widget, role, hint in hints if role == "right"]
        flex = [(widget, hint) for widget, role, hint in hints if role == "flex"]

        def packed(group: list[tuple[QWidget, QSize]]) -> int:
            if not group:
                return 0
            return sum(hint.width() for _widget, hint in group) + gap * (len(group) - 1)

        left_w = packed(left)
        right_w = packed(right)
        flex_min = sum(self._flex_min(widget) for widget, _hint in flex)
        groups = sum(1 for group in (left, right, flex) if group)
        needed = left_w + right_w + flex_min + gap * max(0, groups - 1)
        if needed <= width:
            flex_w = width - left_w - right_w - gap * max(0, groups - 1)
            height = row_h
            flex_heights = []
            for widget, hint in flex:
                item_h = hint.height()
                if widget.hasHeightForWidth():
                    item_h = max(item_h, widget.heightForWidth(max(flex_w, 1)))
                flex_heights.append(item_h)
            if flex_heights:
                height = max(height, max(flex_heights))
            if apply:
                x = 0
                for widget, hint in left:
                    widget.setGeometry(x, 0, hint.width(), height)
                    x += hint.width() + gap
                flex_x = x
                for index, (widget, _hint) in enumerate(flex):
                    share = flex_w if len(flex) == 1 else max(self._flex_min(widget), flex_w // len(flex))
                    widget.setGeometry(flex_x, 0, share, flex_heights[index])
                    flex_x += share + gap
                x = width - right_w
                for widget, hint in right:
                    widget.setGeometry(x, 0, hint.width(), height)
                    x += hint.width() + gap
            return height

        x = y = line_h = 0
        for widget, role, hint in hints:
            item_w = hint.width()
            item_h = hint.height()
            if role == "flex":
                if x > 0:
                    y += line_h + gap
                    x = 0
                    line_h = 0
                item_w = max(self._flex_min(widget), width)
                if widget.hasHeightForWidth():
                    item_h = max(item_h, widget.heightForWidth(item_w))
            elif x > 0 and x + item_w > width:
                y += line_h + gap
                x = 0
                line_h = 0
            if apply:
                widget.setGeometry(x, y, min(item_w, max(width, item_w)), item_h)
            if role == "flex":
                y += item_h + gap
                x = 0
                line_h = 0
            else:
                x += item_w + gap
                line_h = max(line_h, item_h)
        return y + line_h


class PairedToolBar(QWidget):
    """Two clusters. They share a row when they fit.

    When they do not, the trailing cluster stays on the first row and the
    leading cluster drops intact. Action buttons live in the trailing cluster,
    so swapping the leading tools (another tab) does not move them.
    """

    def __init__(self, parent: QWidget | None = None, *, spacing: int = 8) -> None:
        super().__init__(parent)
        self._spacing = spacing
        self._left: list[QWidget] = []
        self._right: list[QWidget] = []
        self._pending = False
        self._arranging = False
        policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def add_left(self, widget: QWidget) -> None:
        self._adopt(widget, self._left)

    def add_right(self, widget: QWidget) -> None:
        self._adopt(widget, self._right)

    def _adopt(self, widget: QWidget, group: list[QWidget]) -> None:
        widget.setParent(self)
        widget.installEventFilter(self)
        group.append(widget)
        self._schedule()

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._arrange(width, apply=False)

    def sizeHint(self) -> QSize:
        return QSize(self._span(self._shown(self._left)) + self._spacing + self._span(self._shown(self._right)),
                     self._arrange(10**6, apply=False))

    def minimumSizeHint(self) -> QSize:
        shown = self._shown(self._left) + self._shown(self._right)
        if not shown:
            return QSize(0, 0)
        return QSize(max(self._box(widget).width() for widget in shown),
                     max(self._box(widget).height() for widget in shown))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._sync_height()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._sync_height()

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.LayoutRequest and not self._arranging:
            self._schedule()
        return super().event(event)

    def eventFilter(self, obj, event) -> bool:
        if event.type() in (QEvent.Type.Show, QEvent.Type.Hide, QEvent.Type.LayoutRequest):
            self._schedule()
        return False

    def _schedule(self) -> None:
        if self._pending:
            return
        self._pending = True
        QTimer.singleShot(0, self._flush)

    def _flush(self) -> None:
        self._pending = False
        self.updateGeometry()
        if self.isVisible() or self.width() > 0:
            self._sync_height()

    def _sync_height(self) -> None:
        if self._arranging:
            return
        height = self._arrange(self.width(), apply=True)
        if height != self.minimumHeight():
            self.setMinimumHeight(height)
            self.updateGeometry()

    def _shown(self, widgets: list[QWidget]) -> list[QWidget]:
        return [widget for widget in widgets if not widget.isHidden()]

    def _box(self, widget: QWidget) -> QSize:
        hint = widget.sizeHint()
        if hint.width() <= 0 or hint.height() <= 0:
            hint = hint.expandedTo(widget.minimumSizeHint())
        width = hint.width()
        if widget.minimumWidth() > 0:
            width = max(width, widget.minimumWidth())
        if 0 < widget.maximumWidth() < 16_777_215:
            width = min(width, widget.maximumWidth())
        height = max(hint.height(), widget.minimumHeight())
        if 0 < widget.maximumHeight() < 16_777_215:
            height = min(height, widget.maximumHeight())
        return QSize(width, height)

    def _span(self, widgets: list[QWidget]) -> int:
        if not widgets:
            return 0
        return sum(self._box(widget).width() for widget in widgets) + self._spacing * (len(widgets) - 1)

    def _arrange(self, width: int, *, apply: bool) -> int:
        if self._arranging:
            return self.height()
        self._arranging = True
        try:
            return self._place(width, apply=apply)
        finally:
            self._arranging = False

    def _place(self, width: int, *, apply: bool) -> int:
        left = self._shown(self._left)
        right = self._shown(self._right)
        if not left and not right:
            return 0
        gap = self._spacing
        row_h = max(self._box(widget).height() for widget in left + right)
        left_w = self._span(left)
        right_w = self._span(right)
        together = left_w + right_w + (gap if left and right else 0)
        if width <= 0 or together <= width:
            if apply:
                self._pack(left, 0, 0, row_h)
                self._pack(right, max(0, width - right_w), 0, row_h)
            return row_h
        # Trailing actions stay on the first row. The leading cluster drops
        # as a whole, so a tab that only has actions does not sit higher.
        y = 0
        if right:
            if right_w <= width:
                if apply:
                    self._pack(right, max(0, width - right_w), 0, row_h)
                y = row_h
            else:
                y = self._wrap_line(right, 0, row_h, width, gap, apply)
        if not left:
            return max(y, row_h)
        if y:
            y += gap
        if apply:
            self._pack(left, 0, y, row_h)
        return y + row_h

    def _wrap_line(self, widgets: list[QWidget], y: int, row_h: int, width: int, gap: int, apply: bool) -> int:
        x = 0
        line = y
        for widget in widgets:
            item_w = self._box(widget).width()
            if x > 0 and x + item_w > width:
                line += row_h + gap
                x = 0
            if apply:
                box = self._box(widget)
                item_h = min(box.height(), row_h)
                widget.setGeometry(x, line + max(0, (row_h - item_h) // 2), item_w, item_h)
            x += item_w + gap
        return line + row_h

    def _pack(self, widgets: list[QWidget], x: int, y: int, height: int) -> None:
        for widget in widgets:
            box = self._box(widget)
            item_h = min(box.height(), height)
            widget.setGeometry(x, y + max(0, (height - item_h) // 2), box.width(), item_h)
            x += box.width() + self._spacing

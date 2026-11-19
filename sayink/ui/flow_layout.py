"""Left-to-right layout that wraps onto new rows instead of clipping."""

from __future__ import annotations

import weakref
from collections.abc import Callable

from PyQt6.QtCore import QEvent, QPoint, QRect, QSize, Qt, QTimer
from PyQt6.QtWidgets import QApplication, QLayout, QLayoutItem, QSizePolicy, QWidget

LIVE_RESIZE_POLL_MS = 32


def is_live_resizing(widget: QWidget | None) -> bool:
    """Return True while the widget's top-level window is in an interactive resize drag."""
    if widget is None:
        return False
    from PyQt6 import sip

    if sip.isdeleted(widget):
        return False
    win = widget.window()
    if win is None or sip.isdeleted(win):
        return False
    return bool(getattr(win, "_live_resizing", False))


def cancel_live_resize(widget: QWidget | None, callback: Callable[[], object] | None = None) -> None:
    """Clear one or all pending live-resize callbacks queued on ``widget``."""
    if widget is None:
        return
    from PyQt6 import sip

    if sip.isdeleted(widget):
        return
    callbacks: list[Callable[[], object]] | None = getattr(widget, "_live_resize_callbacks", None)
    if not callbacks:
        return
    if callback is None:
        callbacks.clear()
    elif callback in callbacks:
        callbacks.remove(callback)


def _widget_depth(widget: QWidget) -> int:
    depth = 0
    cur = widget.parentWidget()
    while cur is not None:
        depth += 1
        cur = cur.parentWidget()
    return depth


def _is_within_subtree(widget: QWidget, root: QWidget) -> bool:
    cur: QWidget | None = widget
    while cur is not None:
        if cur is root:
            return True
        cur = cur.parentWidget()
    return False


def flush_live_resize(root: QWidget | None) -> int:
    """Flush deferred live-resize callbacks under ``root`` once live resize has ended."""
    if root is None:
        return 0
    from PyQt6 import sip

    if sip.isdeleted(root) or is_live_resizing(root):
        return 0
    win = root.window() or root
    if sip.isdeleted(win):
        return 0

    pending_map: dict[int, weakref.ReferenceType[QWidget]] = dict(
        getattr(win, "_live_resize_pending_widgets", None) or {}
    )
    if getattr(root, "_live_resize_callbacks", None):
        pending_map.setdefault(id(root), weakref.ref(root))
    if not pending_map:
        timer = getattr(win, "_live_resize_timer", None)
        if timer is not None and not sip.isdeleted(timer):
            timer.stop()
        return 0

    keep_map: dict[int, weakref.ReferenceType[QWidget]] = {}
    targets: list[QWidget] = []
    for wid, ref in pending_map.items():
        w = ref()
        if w is None or sip.isdeleted(w):
            continue
        cbs = getattr(w, "_live_resize_callbacks", None)
        if not cbs:
            continue
        if _is_within_subtree(w, root):
            targets.append(w)
        else:
            keep_map[wid] = ref

    win._live_resize_pending_widgets = keep_map
    if not keep_map:
        timer = getattr(win, "_live_resize_timer", None)
        if timer is not None and not sip.isdeleted(timer):
            timer.stop()
    if not targets:
        return 0

    tiers: dict[int, list[QWidget]] = {}
    for w in targets:
        tiers.setdefault(_widget_depth(w), []).append(w)

    app = QApplication.instance()
    if app is not None:
        QApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest)
    ran = 0
    sorted_depths = sorted(tiers)
    for idx, depth in enumerate(sorted_depths):
        tier_ran = 0
        for w in tiers[depth]:
            if sip.isdeleted(w):
                continue
            callbacks = list(getattr(w, "_live_resize_callbacks", ()) or ())
            w._live_resize_callbacks = []
            for cb in callbacks:
                if not sip.isdeleted(w):
                    cb()
                    ran += 1
                    tier_ran += 1
        if tier_ran and app is not None and idx + 1 < len(sorted_depths):
            QApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest)

    if ran and app is not None:
        QApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest)
    return ran


def set_live_resizing(window: QWidget | None, active: bool) -> None:
    """Set live-resize state on ``window`` and immediately flush deferred updates on end."""
    if window is None:
        return
    from PyQt6 import sip

    if sip.isdeleted(window):
        return
    win = window.window() or window
    if sip.isdeleted(win):
        return
    win._live_resizing = bool(active)
    if not active:
        flush_live_resize(win)
        timer = getattr(win, "_live_resize_timer", None)
        if timer is not None and not sip.isdeleted(timer):
            timer.stop()


def _on_window_live_resize_timer(weak_win: weakref.ReferenceType[QWidget]) -> None:
    from PyQt6 import sip

    win = weak_win()
    if win is None or sip.isdeleted(win):
        return
    pending_map: dict[int, weakref.ReferenceType[QWidget]] | None = getattr(
        win, "_live_resize_pending_widgets", None
    )
    if not pending_map:
        return
    if bool(getattr(win, "_live_resizing", False)):
        # Still inside an active drag (e.g. >32ms mouse pause): keep holding!
        timer = getattr(win, "_live_resize_timer", None)
        if timer is not None and not sip.isdeleted(timer):
            timer.start(LIVE_RESIZE_POLL_MS)
        return
    flush_live_resize(win)


def defer_while_live_resize(widget: QWidget, callback: Callable[[], object]) -> bool:
    """Hold a structural layout follow-up until an interactive edge drag ends.

    Changing a size hint or pane minimum width on every pixel makes Qt pull
    the window back while Windows is still moving it, and firing a short timer
    during a mid-drag mouse pause jumps the layout under the cursor.
    """
    if not is_live_resizing(widget):
        cancel_live_resize(widget, callback)
        return False
    callbacks: list[Callable[[], object]] | None = getattr(widget, "_live_resize_callbacks", None)
    if callbacks is None:
        callbacks = [callback]
        widget._live_resize_callbacks = callbacks
    elif callback not in callbacks:
        callbacks.append(callback)

    win = widget.window() or widget
    pending: dict[int, weakref.ReferenceType[QWidget]] | None = getattr(
        win, "_live_resize_pending_widgets", None
    )
    if pending is None:
        pending = {}
        win._live_resize_pending_widgets = pending
    wid = id(widget)
    existing = pending.get(wid)
    if existing is None or existing() is not widget:
        pending[wid] = weakref.ref(widget)

    timer = getattr(win, "_live_resize_timer", None)
    if timer is None:
        timer = QTimer(win)
        timer.setSingleShot(True)
        weak_win = weakref.ref(win)
        timer.timeout.connect(lambda _w=weak_win: _on_window_live_resize_timer(_w))
        win._live_resize_timer = timer
    if not timer.isActive():
        timer.start(LIVE_RESIZE_POLL_MS)
    return True


defer_resize = defer_while_live_resize


class FlowLayout(QLayout):
    def __init__(self, parent: QWidget | None = None, spacing: int = 6, *, margin: int = -1) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self._last_height = 0
        if margin >= 0:
            self.setContentsMargins(margin, margin, margin, margin)
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
        parent = self.parentWidget()
        if self._last_height > 0 and is_live_resizing(parent) and parent is not None:
            defer_while_live_resize(parent, self._flush_live_resize)
            return self._last_height
        return self._arrange(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        parent = self.parentWidget()
        if self._last_height > 0 and is_live_resizing(parent) and parent is not None:
            defer_while_live_resize(parent, self._flush_live_resize)
            return
        self._last_height = self._arrange(rect, apply=True)

    def _flush_live_resize(self) -> None:
        from PyQt6 import sip

        if sip.isdeleted(self):
            return
        rect = self.geometry()
        if rect.width() > 0:
            self._last_height = self._arrange(rect, apply=True)
        self.invalidate()
        parent = self.parentWidget()
        if parent is not None and not sip.isdeleted(parent):
            parent.updateGeometry()

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
        if not defer_while_live_resize(self, self._sync_height):
            self._sync_height()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._sync_height()

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
        from PyQt6 import sip

        if sip.isdeleted(self):
            return
        self._pending = False
        if defer_while_live_resize(self, self._flush):
            return
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
        flex_mins = [self._flex_min(widget) for widget, _hint in flex]
        flex_gaps = gap * max(0, len(flex) - 1)
        flex_min = sum(flex_mins) + flex_gaps
        groups = sum(1 for group in (left, right, flex) if group)
        needed = left_w + right_w + flex_min + gap * max(0, groups - 1)
        if needed <= width:
            flex_w = width - left_w - right_w - gap * max(0, groups - 1)
            flex_avail = max(0, flex_w - flex_gaps)
            extra = max(0, flex_avail - sum(flex_mins))
            height = row_h
            flex_heights = []
            shares = []
            for index, (widget, hint) in enumerate(flex):
                share = flex_mins[index] + (
                    (extra // len(flex)) + (1 if index < (extra % len(flex)) else 0)
                    if flex
                    else 0
                )
                shares.append(share)
                item_h = hint.height()
                if widget.hasHeightForWidth():
                    item_h = max(item_h, widget.heightForWidth(max(share, 1)))
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
                    share = shares[index]
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
                widget.setGeometry(x, y, min(item_w, width), item_h)
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
        left, right = self._shown(self._left), self._shown(self._right)
        gap = self._spacing if (left and right) else 0
        return QSize(self._span(left) + gap + self._span(right),
                     self._arrange(10**6, apply=False))

    def minimumSizeHint(self) -> QSize:
        shown = self._shown(self._left) + self._shown(self._right)
        if not shown:
            return QSize(0, 0)
        return QSize(max(self._box(widget).width() for widget in shown),
                     max(self._box(widget).height() for widget in shown))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if not defer_while_live_resize(self, self._sync_height):
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
        from PyQt6 import sip

        if sip.isdeleted(self):
            return
        self._pending = False
        if defer_while_live_resize(self, self._flush):
            return
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
        if left_w <= width:
            if apply:
                self._pack(left, 0, y, row_h)
            return y + row_h
        return self._wrap_line(left, y, row_h, width, gap, apply)

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

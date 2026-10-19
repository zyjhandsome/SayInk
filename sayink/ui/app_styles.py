"""Global application QSS built from design tokens."""

from __future__ import annotations

from PyQt6.QtCore import QEvent, QObject, QPointF, Qt
from PyQt6.QtGui import QPainter, QPen
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QAbstractScrollArea,
    QApplication,
    QComboBox,
    QFrame,
    QProxyStyle,
    QStyle,
    QStyledItemDelegate,
)

from sayink.ui import design_tokens as dt


def combo_popup_width_hint(combo: QComboBox) -> int:
    """Width a list-mode popup needs to show its longest item unelided.

    Qt only widens menu-style popups; list mode stays as wide as the field.
    Computed from margins rather than live geometry so it is valid before the
    popup has ever been shown.
    """
    if combo.count() == 0:
        return 0
    view = combo.view()
    container = view.window()
    view.ensurePolished()
    margins = view.viewportMargins()
    width = view.sizeHintForColumn(0) + 2 * view.frameWidth() + margins.left() + margins.right()
    if isinstance(container, QFrame):
        width += 2 * container.frameWidth()
    layout = container.layout() if container is not None else None
    if layout is not None:
        outer = layout.contentsMargins()
        width += outer.left() + outer.right()
    if combo.count() > combo.maxVisibleItems():
        width += view.verticalScrollBar().sizeHint().width()
    return width


def prepare_combo_popup(combo: QComboBox) -> None:
    """Size the popup before it opens, so it appears once at its final width."""
    container = combo.view().window()
    if container is not None and container is not combo.window():
        container.setMinimumWidth(combo_popup_width_hint(combo))


def widen_combo_popup(view: QAbstractItemView) -> None:
    """Fallback for popups opened without a press (``showPopup()`` from code)."""
    container = view.window()
    combo = container.parentWidget() if container is not None else None
    if not isinstance(combo, QComboBox) or combo.count() == 0:
        return
    needed = combo_popup_width_hint(combo)
    if needed <= 0:
        return
    # Minimum width survives the geometry Qt assigns while opening the list.
    container.setMinimumWidth(needed)
    if needed <= container.width():
        return
    geometry = container.geometry()
    geometry.setWidth(needed)
    screen = combo.screen()
    if screen is not None:
        available = screen.availableGeometry()
        if geometry.right() > available.right():
            geometry.moveRight(available.right())
        if geometry.left() < available.left():
            geometry.moveLeft(available.left())
    container.setGeometry(geometry)


_POPUP_TRIGGERS = (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick, QEvent.Type.KeyPress)


def _forward_wheel_to_scroll_area(combo: QComboBox, event) -> bool:
    """Keep a closed combo from changing while the page scrolls.

    The open popup list still scrolls itself. Retention days and the session
    cap are spin boxes, so they are not affected.
    """
    view = combo.view()
    popup = view.window() if view is not None else None
    if popup is not None and popup is not combo.window() and popup.isVisible():
        return False
    event.ignore()
    parent = combo.parentWidget()
    while parent is not None:
        if isinstance(parent, QAbstractScrollArea):
            QApplication.sendEvent(parent.viewport(), event)
            return True
        parent = parent.parentWidget()
    return True


class _PopupWidthFilter(QObject):
    def __init__(self):
        super().__init__()
        self._adjusting = False

    def eventFilter(self, obj, event):
        kind = event.type()
        if kind == QEvent.Type.Wheel and isinstance(obj, QComboBox):
            return _forward_wheel_to_scroll_area(obj, event)
        if self._adjusting:
            return False
        if kind in _POPUP_TRIGGERS and isinstance(obj, QComboBox):
            prepare_combo_popup(obj)
        elif kind in (QEvent.Type.Show, QEvent.Type.Resize) and isinstance(obj, (QAbstractItemView, QFrame)):
            # List-mode popups often resize into place without a Show on the
            # view. Apply the width there, or the list stays as narrow as the field.
            self._adjusting = True
            try:
                view = obj if isinstance(obj, QAbstractItemView) else obj.findChild(QAbstractItemView)
                if view is not None:
                    widen_combo_popup(view)
            finally:
                self._adjusting = False
        return False


def _combo_for_view(view) -> QComboBox | None:
    widget = view
    while widget is not None:
        if isinstance(widget, QComboBox):
            return widget
        widget = widget.parentWidget()
    return None


class ComboValueDelegate(QStyledItemDelegate):
    """Keep a check on the saved value while hover only tints the row under the pointer."""

    def paint(self, painter: QPainter, option, index) -> None:
        super().paint(painter, option, index)
        combo = _combo_for_view(option.widget)
        if combo is None or index.row() != combo.currentIndex():
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(option.palette.text().color())
        pen.setWidthF(1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        rect = option.rect
        x = rect.right() - 16
        y = rect.center().y()
        painter.drawPolyline(
            QPointF(x, y),
            QPointF(x + 3.5, y + 3.5),
            QPointF(x + 9, y - 4),
        )
        painter.restore()


class DropDownComboStyle(QProxyStyle):
    """Fusion, but combo boxes open below the field instead of over it."""

    def __init__(self, *args):
        super().__init__(*args)
        self._popup_filter = _PopupWidthFilter()

    def styleHint(self, hint, option=None, widget=None, returnData=None):
        if hint == QStyle.StyleHint.SH_ComboBox_Popup:
            return 0
        return super().styleHint(hint, option, widget, returnData)

    def polish(self, target):
        if isinstance(target, QComboBox):
            # The list-mode popup otherwise gets a QItemDelegate, which ignores the
            # QComboBox QAbstractItemView::item rules in our stylesheets.
            if not isinstance(target.itemDelegate(), ComboValueDelegate):
                target.setItemDelegate(ComboValueDelegate(target))
            if not target.property("_popupWidthFilter"):
                target.installEventFilter(self._popup_filter)
                view = target.view()
                view.installEventFilter(self._popup_filter)
                container = view.window()
                if container is not None and container is not target.window():
                    container.installEventFilter(self._popup_filter)
                target.setProperty("_popupWidthFilter", True)
        return super().polish(target)


def build_global_stylesheet(effective: str = "light") -> str:
    """Return the theme-independent application typography baseline.

    Theme colors belong to QApplication's palette and surface-local QSS. Keeping
    this stylesheet static avoids an expensive full widget-tree repolish on
    every light/dark switch. FONT is read at call time so apply_theme's
    refresh_ui_font() is visible.
    """
    del effective
    return f"""
    QWidget {{
        font-family: {dt.FONT};
        font-size: {dt.TYPE_BODY}px;
    }}
"""


# Backward-compatible name: light axis at import; prefer build_global_stylesheet.
GLOBAL_APP_STYLESHEET = build_global_stylesheet("light")

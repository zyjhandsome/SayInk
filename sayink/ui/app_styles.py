"""Global application QSS built from design tokens."""

from __future__ import annotations

from PyQt6.QtCore import QEvent, QObject
from PyQt6.QtWidgets import QAbstractItemView, QComboBox, QFrame, QProxyStyle, QStyle, QStyledItemDelegate

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


class _PopupWidthFilter(QObject):
    def eventFilter(self, obj, event):
        kind = event.type()
        if kind in _POPUP_TRIGGERS and isinstance(obj, QComboBox):
            prepare_combo_popup(obj)
        elif kind == QEvent.Type.Show and isinstance(obj, QAbstractItemView):
            widen_combo_popup(obj)
        return False


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
            if not isinstance(target.itemDelegate(), QStyledItemDelegate):
                target.setItemDelegate(QStyledItemDelegate(target))
            target.installEventFilter(self._popup_filter)
            target.view().installEventFilter(self._popup_filter)
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

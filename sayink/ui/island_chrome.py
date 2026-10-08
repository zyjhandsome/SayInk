"""Shared Spatial Island chrome: glass sheet, top-center placement."""

from __future__ import annotations

from PyQt6.QtCore import Qt, QPoint
from PyQt6.QtGui import QCursor
from PyQt6.QtWidgets import QApplication, QWidget

ISLAND_SHEET_WIDTH = 520
ISLAND_SETTINGS_WIDTH = 720
ISLAND_SHEET_HEIGHT = 640
ISLAND_TOP_MARGIN = 72


def position_island(widget: QWidget, width: int | None = None) -> None:
    """Place a surface as a top-center island on the screen under the cursor."""
    screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
    if screen is None:
        return
    geo = screen.availableGeometry()
    w = width if width is not None else widget.width()
    x = geo.x() + (geo.width() - w) // 2
    y = geo.y() + ISLAND_TOP_MARGIN
    widget.move(QPoint(x, y))


def position_listen_bar(widget: QWidget, width: int | None = None) -> None:
    """Place the listen bar at the top-center of the screen under the cursor."""
    position_island(widget, width=width)


def island_window_flags() -> Qt.WindowType:
    return (
        Qt.WindowType.FramelessWindowHint
        | Qt.WindowType.WindowStaysOnTopHint
        | Qt.WindowType.Tool
    )


def apply_island_sheet_flags(widget: QWidget) -> None:
    widget.setWindowFlags(
        Qt.WindowType.FramelessWindowHint
        | Qt.WindowType.Window
        | Qt.WindowType.WindowStaysOnTopHint
    )
    widget.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)


def island_container_css() -> str:
    from sayink.ui import design_tokens as tok

    return f"""
        QWidget#islandContainer {{
            background-color: {tok.FLOAT_BG};
            border-radius: {tok.RADIUS_PILL}px;
            border: 1px solid {tok.FLOAT_BORDER};
        }}
        QWidget#islandSheet {{
            background-color: {tok.FLOAT_BG};
            border-radius: 28px;
            border: 1px solid {tok.FLOAT_BORDER};
        }}
    """

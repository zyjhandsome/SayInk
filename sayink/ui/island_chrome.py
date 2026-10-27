"""Shared Spatial Island chrome: glass sheet and listen-bar placement."""

from __future__ import annotations

import math

from PyQt6.QtCore import Qt, QPoint, QRect
from PyQt6.QtGui import QCursor
from PyQt6.QtWidgets import QApplication, QWidget

ISLAND_SHEET_WIDTH = 520
ISLAND_SETTINGS_WIDTH = 720
ISLAND_SHEET_HEIGHT = 640
ISLAND_TOP_MARGIN = 72
# Sits above a typical Office status bar; the taskbar is already outside
# availableGeometry. Long sessions use this instead of the ribbon band.
ISLAND_BOTTOM_MARGIN = 36
ISLAND_EDGE_MARGIN = 16

LISTEN_BAR_PLACEMENTS = (
    "auto",
    "top_center",
    "bottom_center",
    "bottom_right",
    "last",
)
LISTEN_BAR_PLACEMENT_LABELS = (
    ("auto", "自动（单次在上方）"),
    ("top_center", "顶部居中"),
    ("bottom_center", "底部居中"),
    ("bottom_right", "右下角"),
    ("last", "上次拖动的位置"),
)


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


def normalize_listen_bar_placement(value: object) -> str:
    text = str(value or "").strip()
    if text in LISTEN_BAR_PLACEMENTS:
        return text
    return "auto"


def resolve_listen_placement(preference: str, *, long_session: bool) -> str:
    """Concrete spot for a session. ``auto`` / ``last`` fall back by duration."""
    pref = normalize_listen_bar_placement(preference)
    if pref in ("auto", "last"):
        return "bottom_center" if long_session else "top_center"
    return pref


def placement_anchor(placement: str) -> str:
    if placement in ("bottom_center", "bottom_right"):
        return "bottom"
    return "top"


def clamp_top_left(x: float, y: float, width: int, height: int, geo: QRect) -> tuple[int, int]:
    """Keep the whole widget inside ``geo`` when it fits."""
    left = math.floor(x)
    top = math.floor(y)
    max_x = geo.x() + max(0, geo.width() - max(1, width))
    max_y = geo.y() + max(0, geo.height() - max(1, height))
    return min(max(left, geo.x()), max_x), min(max(top, geo.y()), max_y)


def default_listen_bar_origin(
    geo: QRect, width: int, height: int, placement: str
) -> tuple[int, int]:
    if placement == "bottom_center":
        x = geo.x() + (geo.width() - width) // 2
        y = geo.y() + geo.height() - height - ISLAND_BOTTOM_MARGIN
    elif placement == "bottom_right":
        x = geo.x() + geo.width() - width - ISLAND_EDGE_MARGIN
        y = geo.y() + geo.height() - height - ISLAND_BOTTOM_MARGIN
    else:
        x = geo.x() + (geo.width() - width) // 2
        y = geo.y() + ISLAND_TOP_MARGIN
    return clamp_top_left(x, y, width, height, geo)


def screen_storage_key(screen) -> str:
    name = (screen.name() or "").strip()
    if name:
        return name
    geo = screen.geometry()
    return f"{geo.x()},{geo.y()},{geo.width()}x{geo.height()}"


def relative_listen_position(
    geo: QRect, x: int, y: int, width: int, height: int, anchor: str
) -> dict:
    rel_x = (x - geo.x()) / max(1, geo.width())
    edge = (y + height) if anchor == "bottom" else y
    rel_y = (edge - geo.y()) / max(1, geo.height())
    return {"rel_x": rel_x, "rel_y": rel_y, "anchor": anchor}


def origin_from_relative(
    geo: QRect, width: int, height: int, saved: dict
) -> tuple[int, int]:
    try:
        rel_x = float(saved.get("rel_x", 0))
        rel_y = float(saved.get("rel_y", 0))
    except (TypeError, ValueError):
        rel_x, rel_y = 0.0, 0.0
    if not math.isfinite(rel_x):
        rel_x = 0.0
    if not math.isfinite(rel_y):
        rel_y = 0.0
    anchor = saved.get("anchor")
    x = geo.x() + rel_x * geo.width()
    if anchor == "bottom":
        y = geo.y() + rel_y * geo.height() - height
    else:
        y = geo.y() + rel_y * geo.height()
    return clamp_top_left(x, y, width, height, geo)


def coerce_saved_positions(raw: object) -> dict:
    if not isinstance(raw, dict):
        return {}
    cleaned: dict = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        try:
            rel_x = float(value["rel_x"])
            rel_y = float(value["rel_y"])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(rel_x) or not math.isfinite(rel_y):
            continue
        anchor = value.get("anchor")
        if anchor not in ("top", "bottom"):
            anchor = "top"
        cleaned[key] = {
            "rel_x": max(-0.5, min(1.5, rel_x)),
            "rel_y": max(-0.5, min(1.5, rel_y)),
            "anchor": anchor,
        }
    return cleaned


def position_listen_bar(
    widget: QWidget,
    width: int | None = None,
    *,
    placement: str = "top_center",
    saved: dict | None = None,
    screen=None,
) -> None:
    """Place the listen bar. Pass ``screen`` so a later state does not follow the cursor."""
    screen = screen or QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
    if screen is None:
        return
    geo = screen.availableGeometry()
    w = width if width is not None else widget.width()
    h = max(1, widget.height())
    if saved:
        x, y = origin_from_relative(geo, w, h, saved)
    else:
        concrete = placement if placement in ("top_center", "bottom_center", "bottom_right") else "top_center"
        x, y = default_listen_bar_origin(geo, w, h, concrete)
    widget.move(QPoint(x, y))


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

"""QSS style fragments for the settings window (theme-rebuildable)."""

from __future__ import annotations

import tempfile
from pathlib import Path

from sayink.ui import design_tokens as t
from sayink.ui import settings_components as sc

_SPIN_ICON_DIR = Path(__file__).resolve().parent / "icons"
_SPIN_URL_CACHE: dict[str, tuple[str, str]] = {}
_LOADED_AXIS: str | None = None
_LOADED_FONT: str | None = None


def _ensure_theme_spin_chevrons(axis: str) -> tuple[str, str] | None:
    """Render WCAG AA (>= 4.5:1) chevron PNGs for the active light/dark axis."""
    try:
        from PyQt6.QtCore import QPointF, Qt
        from PyQt6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPen, QPolygonF

        if QGuiApplication.instance() is None:
            return None
        stroke = "#E5E7EB" if axis == "dark" else "#374151"
        cache_dir = Path(tempfile.gettempdir()) / "sayink_ui_icons" / axis
        up_path = cache_dir / "spin_chevron_up.png"
        down_path = cache_dir / "spin_chevron_down.png"
        cached = _SPIN_URL_CACHE.get(axis)
        if cached is not None and up_path.is_file() and down_path.is_file():
            return cached
        if not (up_path.is_file() and down_path.is_file()):
            cache_dir.mkdir(parents=True, exist_ok=True)
            w, h = 20, 12
            specs = (
                (up_path, (QPointF(4.0, 8.5), QPointF(10.0, 3.5), QPointF(16.0, 8.5))),
                (down_path, (QPointF(4.0, 3.5), QPointF(10.0, 8.5), QPointF(16.0, 3.5))),
            )
            for out_path, pts in specs:
                img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
                img.fill(Qt.GlobalColor.transparent)
                painter = QPainter(img)
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                pen = QPen(QColor(stroke))
                pen.setWidthF(2.2)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                painter.setPen(pen)
                painter.drawPolyline(QPolygonF(pts))
                painter.end()
                if not img.save(str(out_path)):
                    return None
        pair = (up_path.resolve().as_posix(), down_path.resolve().as_posix())
        _SPIN_URL_CACHE[axis] = pair
        return pair
    except Exception:
        return None


def _spin_arrow_urls() -> tuple[str, str]:
    """POSIX paths for PNG chevrons — Qt QSS loads PNG reliably; SVG often fails."""
    axis = "dark" if getattr(t, "ACTIVE_THEME", "light") == "dark" else "light"
    themed = _ensure_theme_spin_chevrons(axis)
    if themed is not None:
        return themed
    up = (_SPIN_ICON_DIR / "spin_chevron_up.png").resolve().as_posix()
    down = (_SPIN_ICON_DIR / "spin_chevron_down.png").resolve().as_posix()
    return up, down


def build_spinbox_css() -> str:
    """Flat numeric stepper shared by every settings QSpinBox / QDoubleSpinBox."""
    up, down = _spin_arrow_urls()
    return f"""
    QDoubleSpinBox, QSpinBox {{
        background: {t.INPUT_BG};
        color: {t.TEXT};
        border: 1px solid {t.CONTROL_BORDER};
        border-radius: {t.RADIUS_MD}px;
        padding: 6px 28px 6px 12px;
        font-size: {t.TYPE_BODY_SM}px;
        min-height: {t.CONTROL_FIELD_HEIGHT - 14}px;
        max-height: {t.CONTROL_FIELD_HEIGHT - 14}px;
    }}
    QSpinBox:focus, QDoubleSpinBox:focus {{
        border: 2px solid {t.ACCENT_FOCUS};
        padding: 5px 27px 5px 11px;
    }}
    QSpinBox::up-button, QSpinBox::down-button,
    QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
        subcontrol-origin: border;
        width: 22px;
        background: {t.SURFACE_PEARL};
        border: none;
        border-left: 1px solid {t.HAIRLINE};
    }}
    QSpinBox::up-button, QDoubleSpinBox::up-button {{
        subcontrol-position: top right;
        margin: 1px 1px 0 0;
        border-top-right-radius: {t.RADIUS_MD - 1}px;
    }}
    QSpinBox::down-button, QDoubleSpinBox::down-button {{
        subcontrol-position: bottom right;
        margin: 0 1px 1px 0;
        border-bottom-right-radius: {t.RADIUS_MD - 1}px;
    }}
    QSpinBox::up-button:hover, QSpinBox::down-button:hover,
    QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {{
        background: {t.SURFACE};
    }}
    QSpinBox::up-button:pressed, QSpinBox::down-button:pressed,
    QDoubleSpinBox::up-button:pressed, QDoubleSpinBox::down-button:pressed {{
        background: {t.ACCENT_SOFT};
    }}
    QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
        image: url("{up}");
        width: 10px;
        height: 6px;
    }}
    QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
        image: url("{down}");
        width: 10px;
        height: 6px;
    }}
"""


def build_window_css() -> str:
    down = _spin_arrow_urls()[1]
    return f"""
    QDialog {{
        background: transparent;
        color: {t.TEXT};
        font-family: {t.FONT};
        font-size: {t.TYPE_BODY}px;
    }}
    QLabel {{
        color: {t.TEXT};
        background: transparent;
    }}
    QLineEdit {{
        background: {t.INPUT_BG};
        color: {t.TEXT};
        border: 1px solid {t.CONTROL_BORDER};
        border-radius: {t.RADIUS_MD}px;
        padding: 6px 12px;
        font-size: {t.TYPE_BODY_SM}px;
        min-height: {t.CONTROL_FIELD_HEIGHT - 14}px;
        selection-background-color: {t.PRIMARY_CONTAINER};
        selection-color: {t.PRIMARY_ON};
    }}
    QLineEdit:focus {{
        border: 2px solid {t.ACCENT_FOCUS};
        padding: 5px 11px;
    }}
    HotkeyEdit {{
        background: {t.INPUT_BG};
        color: {t.TEXT};
        border: 1px solid {t.CONTROL_BORDER};
        border-radius: {t.RADIUS_MD}px;
        padding: 6px 12px;
        font-size: {t.TYPE_BODY_SM}px;
        font-weight: 700;
        font-family: {t.FONT_MONO};
        min-height: {t.CONTROL_FIELD_HEIGHT - 14}px;
    }}
    HotkeyEdit:focus {{
        border: 2px solid {t.ACCENT_FOCUS};
        padding: 5px 11px;
        background: {t.INPUT_BG};
    }}
    QScrollArea {{
        border: none;
        background: transparent;
    }}
    QComboBox {{
        background: {t.INPUT_BG};
        color: {t.TEXT};
        border: 1px solid {t.CONTROL_BORDER};
        border-radius: {t.RADIUS_MD}px;
        padding: 6px 28px 6px 12px;
        font-size: {t.TYPE_BODY_SM}px;
        min-height: {t.CONTROL_FIELD_HEIGHT - 14}px;
    }}
    QComboBox:hover {{
        border-color: {t.TEXT_DIM};
    }}
    QComboBox:focus, QComboBox:on {{
        border: 2px solid {t.ACCENT_FOCUS};
        padding: 5px 27px 5px 11px;
    }}
    QComboBox:disabled {{
        color: {t.TEXT_DIM};
        background: {t.BAR_OFF};
        border-color: {t.HAIRLINE};
    }}
    QComboBox::drop-down {{
        subcontrol-origin: padding;
        subcontrol-position: center right;
        width: 28px;
        border: none;
        background: transparent;
    }}
    QComboBox::down-arrow {{
        image: url("{down}");
        width: 10px;
        height: 6px;
    }}
    QComboBox QAbstractItemView {{
        background: {t.SURFACE_PEARL};
        color: {t.TEXT};
        border: 1px solid {t.HAIRLINE};
        border-radius: {t.RADIUS_LG}px;
        padding: 4px;
        selection-background-color: {t.ROW_SELECTED};
        selection-color: {t.TEXT};
        outline: none;
    }}
    QComboBox QAbstractItemView::item {{
        min-height: 28px;
        padding: 4px 28px 4px 10px;
        border-radius: {t.RADIUS_SM}px;
        color: {t.TEXT};
    }}
    QComboBox QAbstractItemView::item:hover,
    QComboBox QAbstractItemView::item:selected {{
        background: {t.ROW_SELECTED};
        color: {t.TEXT};
    }}
    {build_spinbox_css()}
    QScrollBar:vertical {{
        background: transparent;
        width: 6px;
        margin: 0;
    }}
    QScrollBar:vertical:disabled {{
        background: transparent;
    }}
    QScrollBar::handle:vertical:disabled {{
        background: transparent;
    }}
    QScrollBar::handle:vertical {{
        background: {t.HAIRLINE};
        border-radius: 3px;
        min-height: 30px;
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0;
    }}
"""


def build_btn_primary() -> str:
    return f"""
    QPushButton {{
        background: {t.PRIMARY_CONTAINER}; color: {t.PRIMARY_ON};
        border: 1px solid {t.PRIMARY_CONTAINER};
        border-radius: {t.RADIUS_MD}px; padding: 9px 14px; font-size: {t.TYPE_BODY_SM}px; font-weight: 700;
    }}
    QPushButton:hover {{
        background: {t.PRIMARY_CONTAINER_HOVER};
        border-color: {t.PRIMARY_CONTAINER_HOVER};
    }}
    QPushButton:pressed {{
        background: {t.PRIMARY_CONTAINER_PRESSED};
        border-color: {t.PRIMARY_CONTAINER_PRESSED};
    }}
    QPushButton:disabled {{ background: {t.BAR_OFF}; border-color: {t.BAR_OFF}; color: {t.TEXT_DIM}; }}
    QPushButton:focus {{ border: 2px solid {t.ACCENT_FOCUS}; padding: 8px 13px; }}
"""


def build_search_field_css() -> str:
    """Search uses a compact shared field in both list/detail workspaces."""
    return f"""
    QLineEdit {{
        background: {t.SURFACE_PEARL}; color: {t.TEXT};
        border: 1px solid {t.HAIRLINE}; border-radius: {t.RADIUS_MD}px;
        padding: 6px 12px; min-height: {t.SEARCH_CONTENT_HEIGHT}px;
        font-size: {t.TYPE_BODY_SM}px;
    }}
    QLineEdit:focus {{ border: 2px solid {t.ACCENT_FOCUS}; padding: 5px 11px; }}
"""


def build_btn_ghost() -> str:
    return f"""
    QPushButton {{
        background: {t.SURFACE_PEARL}; color: {t.TEXT}; border: 1px solid {t.HAIRLINE};
        border-radius: {t.RADIUS_MD}px; padding: 10px 22px; font-size: {t.TYPE_BODY}px;
    }}
    QPushButton:hover {{ background: {t.SURFACE}; color: {t.TEXT}; border-color: {t.TEXT_DIM}; }}
    QPushButton:focus {{ outline: none; border: 2px solid {t.ACCENT_FOCUS}; padding: 9px 21px; }}
"""


def _btn_sm_metrics() -> tuple[int, int, int, int]:
    """height, pad_h, focus_pad_h, font_px — shared accent/ghost SM box."""
    h = t.CONTROL_BTN_SM_HEIGHT
    pad_h = t.CONTROL_BTN_SM_PAD_H
    return h, pad_h, max(0, pad_h - 1), t.CONTROL_BTN_SM_FONT_PX


def build_btn_ghost_sm() -> str:
    # Same outer box as accent SM (1px border + fixed height + 0 vertical pad).
    h, pad_h, focus_pad_h, font_px = _btn_sm_metrics()
    return f"""
    QPushButton {{
        background: {t.SURFACE_PEARL}; color: {t.TEXT_SEC};
        border: 1px solid {t.HAIRLINE};
        border-radius: {t.RADIUS_SM}px; padding: 0px {pad_h}px; font-size: {font_px}px;
        font-weight: 400; min-height: {h}px; max-height: {h}px;
    }}
    QPushButton:hover {{ background: {t.SURFACE}; color: {t.TEXT}; border-color: {t.CONTROL_BORDER}; }}
    QPushButton:checked {{
        background: {t.SURFACE}; color: {t.TEXT}; border: 1px solid {t.CONTROL_BORDER};
    }}
    QPushButton:focus {{
        border: 2px solid {t.ACCENT_FOCUS}; padding: 0px {focus_pad_h}px;
    }}
    QPushButton:disabled {{ color: {t.TEXT_DIM}; background: {t.BAR_OFF}; border-color: {t.BAR_OFF}; }}
"""


def build_btn_danger_sm() -> str:
    h, pad_h, focus_pad_h, font_px = _btn_sm_metrics()
    return f"""
    QPushButton {{
        background: transparent; color: {t.RED}; border: 1px solid {t.RED_BG};
        border-radius: {t.RADIUS_SM}px; padding: 0px {pad_h}px; font-size: {font_px}px;
        font-weight: 400; min-height: {h}px; max-height: {h}px;
    }}
    QPushButton:hover {{ background: {t.RED_BG}; color: {t.RED_TEXT}; border-color: {t.RED}; }}
    QPushButton:focus {{
        border: 2px solid {t.ACCENT_FOCUS}; padding: 0px {focus_pad_h}px;
    }}
    QPushButton:disabled {{ color: {t.TEXT_DIM}; background: transparent; border-color: {t.HAIRLINE}; }}
"""


def build_toolbar_combo_css() -> str:
    """Template picker in a meeting toolbar: same outer box as the small buttons.

    The shared form combo is 40px. Leaving it at that height centers the 34px
    action buttons under a taller row, so switching to a button-only toolbar
    moves those buttons closer to the tabs.
    """
    down = _spin_arrow_urls()[1]
    h = t.CONTROL_BTN_SM_HEIGHT
    return f"""
    QComboBox {{
        background: {t.INPUT_BG};
        color: {t.TEXT};
        border: 1px solid {t.CONTROL_BORDER};
        border-radius: {t.RADIUS_SM}px;
        padding: 0px 28px 0px 12px;
        font-size: {t.TYPE_BODY_SM}px;
        min-height: {h}px;
        max-height: {h}px;
    }}
    QComboBox:hover {{
        border-color: {t.TEXT_DIM};
    }}
    QComboBox:focus, QComboBox:on {{
        border: 2px solid {t.ACCENT_FOCUS};
        padding: 0px 27px 0px 11px;
    }}
    QComboBox:disabled {{
        color: {t.TEXT_DIM};
        background: {t.BAR_OFF};
        border-color: {t.HAIRLINE};
    }}
    QComboBox::drop-down {{
        subcontrol-origin: padding;
        subcontrol-position: center right;
        width: 28px;
        border: none;
        background: transparent;
    }}
    QComboBox::down-arrow {{
        image: url("{down}");
        width: 10px;
        height: 6px;
    }}
    QComboBox QAbstractItemView {{
        background: {t.SURFACE_PEARL};
        color: {t.TEXT};
        border: 1px solid {t.HAIRLINE};
        border-radius: {t.RADIUS_LG}px;
        padding: 4px;
        selection-background-color: {t.ROW_SELECTED};
        selection-color: {t.TEXT};
        outline: none;
    }}
    QComboBox QAbstractItemView::item {{
        min-height: 28px;
        padding: 4px 28px 4px 10px;
        border-radius: {t.RADIUS_SM}px;
        color: {t.TEXT};
    }}
    QComboBox QAbstractItemView::item:hover,
    QComboBox QAbstractItemView::item:selected {{
        background: {t.ROW_SELECTED};
        color: {t.TEXT};
    }}
"""


def build_btn_accent_sm() -> str:
    # 1px border matching fill keeps the box identical to ghost SM.
    h, pad_h, focus_pad_h, font_px = _btn_sm_metrics()
    return f"""
    QPushButton {{
        background: {t.PRIMARY_CONTAINER}; color: {t.PRIMARY_ON};
        border: 1px solid {t.PRIMARY_CONTAINER};
        border-radius: {t.RADIUS_SM}px; padding: 0px {pad_h}px; font-size: {font_px}px;
        font-weight: 700; min-height: {h}px; max-height: {h}px;
    }}
    QPushButton:hover {{
        background: {t.PRIMARY_CONTAINER_HOVER};
        border-color: {t.PRIMARY_CONTAINER_HOVER};
    }}
    QPushButton:pressed {{
        background: {t.PRIMARY_CONTAINER_PRESSED};
        border-color: {t.PRIMARY_CONTAINER_PRESSED};
    }}
    QPushButton:focus {{
        border: 2px solid {t.ACCENT_FOCUS}; padding: 0px {focus_pad_h}px;
    }}
    QPushButton:disabled {{ color: {t.TEXT_DIM}; background: {t.BAR_OFF}; border-color: {t.BAR_OFF}; font-weight: 400; }}
"""


def build_btn_danger_solid_sm() -> str:
    """Filled end-meeting control. Same outer box as the other small buttons.

    The light theme's red is dark, so the label uses the page background
    (white). The dark theme's red is light, so that same background token
    is the dark ink.
    """
    h, pad_h, focus_pad_h, font_px = _btn_sm_metrics()
    return f"""
    QPushButton {{
        background: {t.RED}; color: {t.BG};
        border: 1px solid {t.RED};
        border-radius: {t.RADIUS_SM}px; padding: 0px {pad_h}px; font-size: {font_px}px;
        font-weight: 700; min-height: {h}px; max-height: {h}px;
    }}
    QPushButton:hover {{ background: {t.RED_BG}; color: {t.RED_TEXT}; border-color: {t.RED}; }}
    QPushButton:focus {{
        border: 2px solid {t.ACCENT_FOCUS}; padding: 0px {focus_pad_h}px;
    }}
    QPushButton:disabled {{ color: {t.TEXT_DIM}; background: {t.BAR_OFF}; border-color: {t.BAR_OFF}; font-weight: 400; }}
"""


def reload_styles() -> None:
    """Rebuild module-level style aliases from the active token axis."""
    global _LOADED_AXIS, _LOADED_FONT
    sc.reload_styles()
    g = globals()
    g["WINDOW_CSS"] = build_window_css()
    g["BTN_PRIMARY"] = build_btn_primary()
    g["BTN_GHOST"] = build_btn_ghost()
    g["BTN_GHOST_SM"] = build_btn_ghost_sm()
    g["BTN_DANGER_SM"] = build_btn_danger_sm()
    g["BTN_DANGER_SOLID_SM"] = build_btn_danger_solid_sm()
    g["BTN_ACCENT_SM"] = build_btn_accent_sm()
    _LOADED_AXIS = getattr(t, "ACTIVE_THEME", None)
    _LOADED_FONT = getattr(t, "UI_FONT_FAMILY", None)


WINDOW_CSS = ""
BTN_PRIMARY = ""
BTN_GHOST = ""
BTN_GHOST_SM = ""
BTN_DANGER_SM = ""
BTN_DANGER_SOLID_SM = ""
BTN_ACCENT_SM = ""

reload_styles()

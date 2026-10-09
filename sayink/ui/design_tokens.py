"""SayInk design tokens — light/dark axes from design-system/MASTER.md."""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional, Sequence

log = logging.getLogger("sayink")

# Shared non-color tokens. Qt stylesheets accept one family, not a CSS-style
# fallback list. Prefer YaHei so common CJK stays visible; Latin-only
# families such as Segoe UI Variable are skipped when a CJK family exists.
FONT_STACK = (
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "Segoe UI Variable",
    "Segoe UI",
)
_CJK_SAMPLE = "设置听写"
_DEFAULT_UI_FONT = "Microsoft YaHei UI"
UI_FONT_FAMILY = _DEFAULT_UI_FONT
FONT = f'"{_DEFAULT_UI_FONT}"'
FONT_DISPLAY = FONT
# Qt stylesheets accept one family. Resolve at runtime; do not ship a CSS list.
MONO_STACK = (
    "Cascadia Mono",
    "Consolas",
    "JetBrains Mono",
)
_DEFAULT_MONO_FONT = "Cascadia Mono"
FONT_MONO = f'"{_DEFAULT_MONO_FONT}"'

# Typography size ladder (px in QSS; same ints passed to QFont where used today).
TYPE_CAPTION = 12
TYPE_FOOTNOTE = 13
TYPE_BODY_SM = 14
TYPE_BODY = 14
TYPE_TITLE_SM = 15
TYPE_TITLE = 16
TYPE_ICON_LG = 17
TYPE_HERO = 18
TYPE_TITLE_LG = 20
TYPE_DISPLAY = 22


def _probe_system_font_families() -> tuple[str, ...]:
    """Return installed family names, or () when no QApplication / probe fails."""
    try:
        from PyQt6.QtGui import QFontDatabase
        from PyQt6.QtWidgets import QApplication

        if QApplication.instance() is None:
            return ()
        return tuple(QFontDatabase.families())
    except Exception as exc:  # noqa: BLE001 — probe must never crash startup
        log.warning("读取系统字体列表失败，回落 YaHei: %s", exc)
        return ()


def _family_covers_cjk(family: str) -> bool:
    """Return True when family can paint common Simplified Chinese, or if we cannot probe."""
    try:
        from PyQt6.QtGui import QFont, QFontDatabase, QFontMetrics
        from PyQt6.QtWidgets import QApplication

        if QApplication.instance() is None:
            return True
        installed = {name.casefold() for name in QFontDatabase.families()}
        if family.casefold() not in installed:
            return False
        names = {str(system) for system in QFontDatabase.writingSystems(family)}
        if any("Chinese" in name for name in names):
            return True
        metrics = QFontMetrics(QFont(family))
        return all(metrics.inFont(ch) for ch in _CJK_SAMPLE)
    except Exception as exc:  # noqa: BLE001 — probe must never crash startup
        log.warning("检测字体中文覆盖失败 %s: %s", family, exc)
        return True


def resolve_ui_font_family(
    *,
    available_families: Optional[Sequence[str]] = None,
    preferred: Optional[Sequence[str]] = None,
    covers_cjk: Optional[Callable[[str], bool]] = None,
) -> str:
    """Pick the first preferred family present on the host that can cover CJK."""
    stack = tuple(preferred) if preferred is not None else FONT_STACK
    probed_system = available_families is None
    if available_families is None:
        available_families = _probe_system_font_families()
    if covers_cjk is None and probed_system:
        covers_cjk = _family_covers_cjk
    available = {name.casefold(): name for name in available_families}
    for name in stack:
        hit = available.get(name.casefold())
        if hit is None:
            continue
        if covers_cjk is not None and not covers_cjk(hit):
            continue
        return hit
    return _DEFAULT_UI_FONT


def resolve_mono_font_family(
    *,
    available_families: Optional[Sequence[str]] = None,
    preferred: Optional[Sequence[str]] = None,
) -> str:
    """Pick one installed monospace family. QSS cannot use a fallback list."""
    stack = tuple(preferred) if preferred is not None else MONO_STACK
    if available_families is None:
        available_families = _probe_system_font_families()
    available = {name.casefold(): name for name in available_families}
    for name in stack:
        hit = available.get(name.casefold())
        if hit is not None:
            return hit
    return _DEFAULT_MONO_FONT


def refresh_ui_font(
    *,
    available_families: Optional[Sequence[str]] = None,
    preferred: Optional[Sequence[str]] = None,
) -> str:
    """Resolve and publish FONT / FONT_DISPLAY / FONT_MONO for QSS and QFont."""
    global UI_FONT_FAMILY, FONT, FONT_DISPLAY, FONT_MONO
    family = resolve_ui_font_family(
        available_families=available_families,
        preferred=preferred,
    )
    mono = resolve_mono_font_family(available_families=available_families)
    UI_FONT_FAMILY = family
    FONT = f'"{family}"'
    FONT_DISPLAY = FONT
    FONT_MONO = f'"{mono}"'
    return family

RADIUS_XS = 4
RADIUS_SM = 6
RADIUS_MD = 8
RADIUS_LG = 10
RADIUS_PILL = 999
WINDOW_RADIUS = 12

SPACE_XS = 8
SPACE_SM = 12
SPACE_MD = 16
SPACE_LG = 24
SPACE_XL = 32
PAGE_MARGIN_H = 20
PAGE_MARGIN_V = 16
CONTENT_MAX_WIDTH = 900
SIDEBAR_WIDTH = 184

# Wide enough for values like「5500 场」plus a 22px flat stepper column.
CONTROL_NUMERIC_WIDTH = 120
CONTROL_DEVICE_COMBO_WIDTH = 320

# Shared box for settings small actions (accent + ghost) so paired
# buttons like「使用此模型」/「删除」paint at the same outer height.
CONTROL_BTN_SM_HEIGHT = 32
CONTROL_BTN_SM_PAD_H = 14
CONTROL_BTN_SM_FONT_PX = TYPE_BODY_SM

NAV_SELECTED_BAR_PX = 3
TRAY_MENU_RADIUS = 8
# 13px rows. Left inset is the check glyph plus a 4px gap, not a wide gutter.
TRAY_MENU_PAD_V = 5
TRAY_MENU_PAD_H = 22
TRAY_MENU_FONT = TYPE_FOOTNOTE

TOGGLE_OFF_TRACK = (60, 60, 67, 48)
TOGGLE_OFF_TRACK_HOVER = (60, 60, 67, 72)

_LIGHT: dict[str, Any] = {
    "ACCENT": "#2563EB",
    "ACCENT_HV": "#1D4ED8",
    "ACCENT_FOCUS": "#2563EB",
    "ACCENT_TEXT": "#2563EB",
    "ACCENT_TEXT_HOVER": "#1D4ED8",
    "ACCENT_ON_DARK": "#FFFFFF",
    "ACCENT_BG": "#DBEAFE",
    "ACCENT_SOFT": "rgba(37, 99, 235, 0.08)",
    "PRIMARY_CONTAINER": "#0D0D0D",
    "PRIMARY_CONTAINER_HOVER": "#262626",
    "PRIMARY_CONTAINER_PRESSED": "#404040",
    "PRIMARY_ON": "#FFFFFF",
    "SECONDARY_CONTAINER": "#E5E7EB",
    "BG": "#FFFFFF",
    "NAV_BG": "#F6F7F8",
    "SURFACE": "#FFFFFF",
    "SURFACE_PEARL": "#F7F7F7",
    "BORDER": "rgba(13,13,13,0.08)",
    "HAIRLINE": "rgba(13,13,13,0.08)",
    "OUTLINE_VARIANT": "#D1D5DB",
    "DIVIDER_SOFT": "#E5E7EB",
    "ROW_SELECTED": "rgba(13,13,13,0.06)",
    "INPUT_BG": "#FFFFFF",
    "BAR_OFF": "#E5E7EB",
    "SETTINGS_SIDEBAR_BG": "#FFFFFF",
    "NAV_SELECTED_BG": "rgba(13,13,13,0.06)",
    "TEXT": "#111827",
    "TEXT_SEC": "#4B5563",
    "TEXT_DIM": "#667085",
    # Keep AA contrast as small text on BG; visual weight of switches is
    # reduced via track size / shadow, not by lightening this green.
    "GREEN": "#15803D",
    "GREEN_TEXT": "#166534",
    "GREEN_BG": "#DCFCE7",
    "RED": "#C81E1E",
    "RED_BG": "#FEE2E2",
    "AMBER": "#D97706",
    "AMBER_TEXT": "#92400E",
    "AMBER_SOFT": "#FFFBEB",
    "CALLOUT_BORDER": "#F5E6B8",
    "ATTENTION": "#D97706",
    "CONTROL_BORDER": "#D1D5DB",
    "CONTROL_BORDER_HOVER": "#9CA3AF",
    # Cooler off-track than prototype's warm #E5E2E3 so the white knob reads
    # on SURFACE rows; still a flat gray pill like .toggle.
    "TOGGLE_OFF": "#D1D5DB",
    "TOGGLE_OFF_HOVER": "#9CA3AF",
    "TOGGLE_ON": "#15803D",
    "TOGGLE_ON_HOVER": "#166534",
    "ROW_HOVER": "rgba(0, 0, 0, 0.03)",
    "FOCUS_RING": "2px solid #2563EB",
    "TRAY_MENU_BORDER": "#E5E7EB",
    "TRAY_MENU_SEPARATOR": "#E5E7EB",
    "TRAY_MENU_HOVER": "#F3F4F6",
    "TRAY_MENU_CHECK": "#333333",
    "TRAY_MENU_ARROW": "#9CA3AF",
    "FLOAT_BG": "rgba(255, 255, 255, 236)",
    "FLOAT_TILE": "#FFFFFF",
    "FLOAT_BORDER": "rgba(17, 24, 39, 0.10)",
    "CHIP_BG": "rgba(17, 24, 39, 0.08)",
    "CHIP_BG_HOVER": "rgba(17, 24, 39, 0.16)",
    "CHIP_BG_PRESS": "rgba(17, 24, 39, 0.12)",
    "FLOAT_TEXT": "#111827",
    "FLOAT_TEXT_SEC": "#4B5563",
    "STATE_RECORD": "#DC2626",
}

_DARK: dict[str, Any] = {
    "ACCENT": "#3B82F6",
    "ACCENT_HV": "#60A5FA",
    "ACCENT_FOCUS": "#3B82F6",
    "ACCENT_TEXT": "#60A5FA",
    "ACCENT_TEXT_HOVER": "#93C5FD",
    "ACCENT_ON_DARK": "#FFFFFF",
    "ACCENT_BG": "#1E3A5F",
    "ACCENT_SOFT": "rgba(59, 130, 246, 0.16)",
    "PRIMARY_CONTAINER": "#FFFFFF",
    "PRIMARY_CONTAINER_HOVER": "#E8E8E8",
    "PRIMARY_CONTAINER_PRESSED": "#D0D0D0",
    "PRIMARY_ON": "#0D0D0D",
    "SECONDARY_CONTAINER": "#374151",
    "BG": "#181818",
    "NAV_BG": "#141618",
    "SURFACE": "#181818",
    "SURFACE_PEARL": "#222528",
    "BORDER": "rgba(255,255,255,0.08)",
    "HAIRLINE": "rgba(255,255,255,0.08)",
    "OUTLINE_VARIANT": "#4B5563",
    "DIVIDER_SOFT": "#374151",
    "ROW_SELECTED": "rgba(255,255,255,0.06)",
    "INPUT_BG": "#202326",
    "BAR_OFF": "#4B5563",
    "SETTINGS_SIDEBAR_BG": "#181818",
    "NAV_SELECTED_BG": "rgba(255,255,255,0.06)",
    "TEXT": "#F9FAFB",
    "TEXT_SEC": "#D1D5DB",
    "TEXT_DIM": "#9CA3AF",
    # Mid green (not neon #22C55E) so dark-theme ON switches stay quieter.
    "GREEN": "#16A34A",
    "GREEN_TEXT": "#86EFAC",
    "GREEN_BG": "#14532D",
    "RED": "#F87171",
    "RED_BG": "#7F1D1D",
    "AMBER": "#FBBF24",
    "AMBER_TEXT": "#FCD34D",
    "AMBER_SOFT": "#422006",
    "CALLOUT_BORDER": "#78350F",
    "ATTENTION": "#FBBF24",
    "CONTROL_BORDER": "#4B5563",
    "CONTROL_BORDER_HOVER": "#6B7280",
    "TOGGLE_OFF": "#4B5563",
    "TOGGLE_OFF_HOVER": "#6B7280",
    "TOGGLE_ON": "#16A34A",
    "TOGGLE_ON_HOVER": "#15803D",
    "ROW_HOVER": "rgba(255, 255, 255, 0.06)",
    "FOCUS_RING": "2px solid #3B82F6",
    "TRAY_MENU_BORDER": "#374151",
    "TRAY_MENU_SEPARATOR": "#4B5563",
    "TRAY_MENU_HOVER": "#374151",
    "TRAY_MENU_CHECK": "#F9FAFB",
    "TRAY_MENU_ARROW": "#9CA3AF",
    "FLOAT_BG": "rgba(24, 24, 24, 236)",
    "FLOAT_TILE": "#181818",
    "FLOAT_BORDER": "rgba(255, 255, 255, 0.12)",
    "CHIP_BG": "rgba(210, 210, 215, 0.40)",
    "CHIP_BG_HOVER": "rgba(210, 210, 215, 0.55)",
    "CHIP_BG_PRESS": "rgba(210, 210, 215, 0.48)",
    "FLOAT_TEXT": "#FFFFFF",
    "FLOAT_TEXT_SEC": "rgba(235, 235, 245, 0.72)",
    "STATE_RECORD": "#F87171",
}

_COLOR_KEYS = tuple(_LIGHT.keys())


def tokens_for(effective: str) -> dict[str, Any]:
    """Return a copy of the token map for an effective theme (`light`|`dark`)."""
    axis = _DARK if effective == "dark" else _LIGHT
    return dict(axis)


def activate(effective: str) -> None:
    """Publish the effective theme onto module-level color aliases."""
    vals = tokens_for(effective)
    g = globals()
    for key in _COLOR_KEYS:
        g[key] = vals[key]
    # Float state aliases: listen uses semantic green; others follow float text
    g["STATE_LISTEN"] = vals["GREEN"]
    g["STATE_RECOGNIZE"] = vals["FLOAT_TEXT"]
    g["STATE_POLISH"] = vals["FLOAT_TEXT"]
    g["STATE_SUCCESS"] = vals["FLOAT_TEXT"]
    # Warnings get the semantic amber so they read as warnings; stopped /
    # cancelled stay quiet grey on purpose.
    g["STATE_WARN"] = vals["AMBER"]
    g["STATE_MUTED"] = vals["FLOAT_TEXT_SEC"]
    g["STATE_ERROR"] = vals["STATE_RECORD"]


# Initialize light as the default module namespace (imports before activate).
activate("light")

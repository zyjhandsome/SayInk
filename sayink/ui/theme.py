"""Appearance theme resolution and application."""

from __future__ import annotations

import logging
from typing import Iterable, Literal, Optional, Protocol, runtime_checkable, cast

from PyQt6.QtCore import QSettings
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication

from sayink.ui import design_tokens as dt
from sayink.ui.app_styles import build_global_stylesheet

log = logging.getLogger("sayink")

ThemeMode = Literal["light", "dark", "system"]
EffectiveTheme = Literal["light", "dark"]

VALID_MODES = frozenset({"light", "dark", "system"})

_current_effective: EffectiveTheme = "light"


@runtime_checkable
class ThemeAware(Protocol):
    def reapply_theme(self) -> None: ...


def normalize_theme_mode(mode: object) -> ThemeMode:
    if isinstance(mode, str) and mode in VALID_MODES:
        return cast(ThemeMode, mode)
    return "system"


def probe_system_is_light() -> bool:
    """Return True when Windows apps prefer light theme; fail open to light."""
    try:
        settings = QSettings(
            r"HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            QSettings.Format.NativeFormat,
        )
        value = settings.value("AppsUseLightTheme")
        if value is None:
            return True
        return bool(int(value))
    except Exception as exc:  # noqa: BLE001 — probe must never crash startup
        log.warning("读取系统外观失败，回落 light: %s", exc)
        return True


def resolve_effective_theme(
    mode: object,
    *,
    system_is_light: Optional[bool] = None,
) -> EffectiveTheme:
    """Map theme mode to effective light/dark. Unknown modes behave as system."""
    normalized = normalize_theme_mode(mode)
    if normalized == "light":
        return "light"
    if normalized == "dark":
        return "dark"
    if system_is_light is None:
        system_is_light = probe_system_is_light()
    return "light" if system_is_light else "dark"


def current_effective_theme() -> EffectiveTheme:
    return _current_effective


def watch_system_color_scheme(callback) -> bool:
    """Call ``callback()`` when Windows switches light/dark (Qt 6.5+)."""
    from PyQt6.QtGui import QGuiApplication

    app = QGuiApplication.instance()
    hints = app.styleHints() if app is not None else None
    signal = getattr(hints, "colorSchemeChanged", None)
    if signal is None:
        log.info("当前 Qt 不支持系统配色变化通知，「跟随系统」仅在启动和切换时生效")
        return False
    signal.connect(lambda *_args: callback())
    return True


def _application_palette(effective: EffectiveTheme) -> QPalette:
    """Build a native Qt palette for dialogs not owned by a styled surface."""
    t = dt.tokens_for(effective)
    palette = QPalette()

    def set_color(role: QPalette.ColorRole, token: str) -> None:
        palette.setColor(role, QColor(t[token]))

    set_color(QPalette.ColorRole.Window, "BG")
    set_color(QPalette.ColorRole.WindowText, "TEXT")
    set_color(QPalette.ColorRole.Base, "INPUT_BG")
    set_color(QPalette.ColorRole.AlternateBase, "SURFACE_PEARL")
    set_color(QPalette.ColorRole.Text, "TEXT")
    set_color(QPalette.ColorRole.Button, "SURFACE")
    set_color(QPalette.ColorRole.ButtonText, "TEXT")
    set_color(QPalette.ColorRole.ToolTipBase, "SURFACE")
    set_color(QPalette.ColorRole.ToolTipText, "TEXT")
    set_color(QPalette.ColorRole.Highlight, "ACCENT")
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#FFFFFF"))
    palette.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.Text,
        QColor(t["TEXT_DIM"]),
    )
    palette.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.ButtonText,
        QColor(t["TEXT_DIM"]),
    )
    return palette


def apply_theme(
    app: Optional[QApplication] = None,
    *,
    mode: object = "system",
    surfaces: Iterable[object] = (),
    system_is_light: Optional[bool] = None,
) -> EffectiveTheme:
    """Activate tokens, set global stylesheet, and reapply registered surfaces."""
    global _current_effective
    effective = resolve_effective_theme(mode, system_is_light=system_is_light)
    _current_effective = effective
    dt.activate(effective)
    dt.refresh_ui_font()
    try:
        from sayink.ui import settings_styles as ss

        ss.reload_styles()
    except Exception as exc:  # noqa: BLE001
        log.warning("重建设置样式失败: %s", exc)

    qt_app = app or QApplication.instance()
    if qt_app is not None:
        qt_app.setPalette(_application_palette(effective))
        # Rebuild when missing or when the resolved UI font family changed.
        prev_font = qt_app.property("sayinkUiFontFamily")
        need_style = (
            not qt_app.property("sayinkStaticStyleApplied")
            or prev_font != dt.UI_FONT_FAMILY
        )
        if need_style:
            qt_app.setStyleSheet(build_global_stylesheet(effective))
            qt_app.setProperty("sayinkStaticStyleApplied", True)
            qt_app.setProperty("sayinkUiFontFamily", dt.UI_FONT_FAMILY)

    for surface in surfaces:
        reapply = getattr(surface, "reapply_theme", None)
        if callable(reapply):
            try:
                reapply()
            except Exception as exc:  # noqa: BLE001
                log.warning("表面换肤失败 %s: %s", type(surface).__name__, exc)
    return effective

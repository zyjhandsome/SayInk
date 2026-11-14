"""Appearance theme resolution and application."""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import time
from typing import Iterable, Literal, Optional, Protocol, runtime_checkable, cast

from PyQt6.QtCore import QSettings
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication

from sayink.ui import design_tokens as dt
from sayink.ui.app_styles import build_global_stylesheet

log = logging.getLogger("SayInk")

_SPI_GETCLIENTAREAANIMATION = 0x1042
_REDUCED_MOTION_CACHE: tuple[int, float, bool] | None = None
_REDUCED_MOTION_TTL_SEC = 2.0


def prefers_reduced_motion() -> bool:
    """Return True when OS or SAYINK_REDUCED_MOTION disables UI animations."""
    global _REDUCED_MOTION_CACHE
    env = os.environ.get("SAYINK_REDUCED_MOTION")
    if env is not None:
        return env.strip().lower() in {"1", "true", "yes", "on"}
    if os.environ.get("QT_QPA_PLATFORM", "").strip().lower() == "offscreen":
        return False
    if sys.platform == "win32":
        try:
            from ctypes import wintypes

            spi = ctypes.windll.user32.SystemParametersInfoW
            now = time.monotonic()
            cached = _REDUCED_MOTION_CACHE
            if (
                cached is not None
                and cached[0] == id(spi)
                and 0.0 <= now - cached[1] < _REDUCED_MOTION_TTL_SEC
            ):
                return cached[2]
            if hasattr(spi, "argtypes"):
                spi.argtypes = [wintypes.UINT, wintypes.UINT, wintypes.LPVOID, wintypes.UINT]
                spi.restype = wintypes.BOOL
            enabled = ctypes.c_int(1)
            ok = spi(
                _SPI_GETCLIENTAREAANIMATION,
                0,
                ctypes.byref(enabled),
                0,
            )
            result = (not bool(enabled.value)) if ok else False
            _REDUCED_MOTION_CACHE = (id(spi), now, result)
            return result
        except Exception:
            return False
    return False

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
    except Exception as exc:
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


_scheme_callback = None


def _color_scheme_signal():
    from PyQt6.QtGui import QGuiApplication

    app = QGuiApplication.instance()
    hints = app.styleHints() if app is not None else None
    return getattr(hints, "colorSchemeChanged", None)


def watch_system_color_scheme(callback) -> bool:
    """Call ``callback()`` when Windows switches light/dark (Qt 6.5+).

    The callback is connected directly and remembered. A lambda closed over
    the window was never disconnected, so every exited session stayed alive
    on the process-wide style-hints object.
    """
    global _scheme_callback
    signal = _color_scheme_signal()
    if signal is None:
        log.info("当前 Qt 不支持系统配色变化通知，「跟随系统」仅在启动和切换时生效")
        return False
    if _scheme_callback is not None:
        try:
            signal.disconnect(_scheme_callback)
        except (RuntimeError, TypeError):
            pass
    signal.connect(callback)
    _scheme_callback = callback
    return True


def unwatch_system_color_scheme() -> None:
    """Drop the Auto-theme callback before the application object is destroyed."""
    global _scheme_callback
    callback = _scheme_callback
    _scheme_callback = None
    if callback is None:
        return
    signal = _color_scheme_signal()
    if signal is None:
        return
    try:
        signal.disconnect(callback)
    except (RuntimeError, TypeError):
        pass


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
    set_color(QPalette.ColorRole.Highlight, "ACCENT_SOLID")
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
    try:
        dt.activate(effective, _notify=False)
    except TypeError:
        dt.activate(effective)
    try:
        dt.refresh_ui_font(_notify=False)
    except TypeError:
        dt.refresh_ui_font()
    try:
        from sayink.ui import settings_styles as ss

        if (
            getattr(ss, "_LOADED_AXIS", None) != effective
            or getattr(ss, "_LOADED_FONT", None) != dt.UI_FONT_FAMILY
        ):
            ss.reload_styles()
    except Exception as exc:
        log.warning("重建设置样式失败: %s", exc)

    qt_app = app or QApplication.instance()
    if qt_app is not None:
        qt_app.setPalette(_application_palette(effective))
        # Rebuild when missing, when the effective theme changed, or when UI font changed.
        prev_font = qt_app.property("sayinkUiFontFamily")
        prev_effective = qt_app.property("sayinkEffectiveTheme")
        need_style = (
            not qt_app.property("sayinkStaticStyleApplied")
            or prev_font != dt.UI_FONT_FAMILY
            or prev_effective != effective
        )
        if need_style:
            qt_app.setStyleSheet(build_global_stylesheet(effective))
            qt_app.setProperty("sayinkStaticStyleApplied", True)
            qt_app.setProperty("sayinkUiFontFamily", dt.UI_FONT_FAMILY)
            qt_app.setProperty("sayinkEffectiveTheme", effective)

    for surface in surfaces:
        reapply = getattr(surface, "reapply_theme", None)
        if callable(reapply):
            try:
                reapply()
            except Exception as exc:
                log.warning("表面换肤失败 %s: %s", type(surface).__name__, exc)
    return effective

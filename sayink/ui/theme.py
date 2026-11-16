"""Appearance theme resolution and application."""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import time
from typing import Iterable, Literal, Optional, Protocol, runtime_checkable, cast

from PyQt6.QtCore import QEvent, QObject, QSettings, Qt
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox, QWidget

from sayink.ui import design_tokens as dt
from sayink.ui.app_styles import build_global_stylesheet
from sayink.ui.design_tokens import qcolor_from_token

log = logging.getLogger("SayInk")

_SPI_GETCLIENTAREAANIMATION = 0x1042
_REDUCED_MOTION_CACHE: tuple[int, float, bool] | None = None
_REDUCED_MOTION_TTL_SEC = 2.0
_DWMWA_USE_IMMERSIVE_DARK_MODE_BEFORE_20H1 = 19
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20


def apply_window_dark_titlebar(widget: object, is_dark: bool) -> bool:
    """Set Win32 DWM immersive dark mode on a top-level window's native caption bar.

    Safe no-op off Windows, under ``QT_QPA_PLATFORM=offscreen``, or on child/deleted widgets.
    """
    if widget is None or sys.platform != "win32":
        return False
    if os.environ.get("QT_QPA_PLATFORM", "").strip().lower() == "offscreen":
        return False
    try:
        from PyQt6 import sip

        if isinstance(widget, QWidget) and sip.isdeleted(widget):
            return False
    except Exception:
        pass
    is_win = getattr(widget, "isWindow", None)
    if callable(is_win) and not is_win():
        return False
    win_id_fn = getattr(widget, "winId", None)
    if not callable(win_id_fn):
        return False
    try:
        from ctypes import byref, c_int, c_long, c_void_p, sizeof, windll, wintypes

        hwnd = int(win_id_fn() or 0)
        if hwnd == 0:
            return False
        dwm_set = windll.dwmapi.DwmSetWindowAttribute
        if hasattr(dwm_set, "argtypes"):
            dwm_set.argtypes = [wintypes.HWND, wintypes.DWORD, c_void_p, wintypes.DWORD]
            dwm_set.restype = c_long
        val = c_int(1 if is_dark else 0)
        for attr in (_DWMWA_USE_IMMERSIVE_DARK_MODE_BEFORE_20H1, _DWMWA_USE_IMMERSIVE_DARK_MODE):
            dwm_set(hwnd, attr, byref(val), sizeof(c_int))
        return True
    except Exception:
        return False


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
        palette.setColor(role, qcolor_from_token(t[token]))

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
        qcolor_from_token(t["TEXT_DIM"]),
    )
    palette.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.ButtonText,
        qcolor_from_token(t["TEXT_DIM"]),
    )
    return palette


class _DialogThemeFilter(QObject):
    """Apply Win32 dark/light caption bars and QMessageBox theming on top-level dialogs."""

    def eventFilter(self, obj: QObject | None, event: QEvent | None) -> bool:
        if event is None or obj is None:
            return False
        kind = event.type()
        if kind not in (QEvent.Type.Show, QEvent.Type.WinIdChange):
            return False
        if not isinstance(obj, QDialog) or not obj.isWindow():
            return False
        is_dark = _current_effective == "dark"
        if kind == QEvent.Type.Show and isinstance(obj, QMessageBox):
            if obj.property("_sayinkMessageBoxStyled") != _current_effective:
                try:
                    from sayink.ui.prompts import style_message_box

                    style_message_box(
                        obj,
                        primary=obj.property("_sayinkStyledPrimary"),
                        destructive=bool(obj.property("_sayinkStyledDestructive")),
                    )
                except Exception as exc:
                    log.debug("自动样式化消息框失败: %s", exc)
        if not (obj.windowFlags() & Qt.WindowType.FramelessWindowHint):
            apply_window_dark_titlebar(obj, is_dark)
        return False


_DIALOG_THEME_FILTER: _DialogThemeFilter | None = None


def _ensure_dialog_theme_filter(qt_app: QApplication) -> None:
    global _DIALOG_THEME_FILTER
    if _DIALOG_THEME_FILTER is None:
        _DIALOG_THEME_FILTER = _DialogThemeFilter()
    if not qt_app.property("_sayinkDialogThemeFilterInstalled"):
        qt_app.installEventFilter(_DIALOG_THEME_FILTER)
        qt_app.setProperty("_sayinkDialogThemeFilterInstalled", True)


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
        _ensure_dialog_theme_filter(qt_app)
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

    seen_surface_ids: set[int] = set()
    for surface in surfaces:
        seen_surface_ids.add(id(surface))
        reapply = getattr(surface, "reapply_theme", None)
        if callable(reapply):
            try:
                reapply()
            except Exception as exc:
                log.warning("表面换肤失败 %s: %s", type(surface).__name__, exc)

    if qt_app is not None:
        is_dark = effective == "dark"
        for top in qt_app.topLevelWidgets():
            if not isinstance(top, QDialog) or not top.isVisible():
                continue
            if not (top.windowFlags() & Qt.WindowType.FramelessWindowHint):
                apply_window_dark_titlebar(top, is_dark)
            if isinstance(top, QMessageBox):
                try:
                    from sayink.ui.prompts import style_message_box

                    style_message_box(
                        top,
                        primary=top.property("_sayinkStyledPrimary"),
                        destructive=bool(top.property("_sayinkStyledDestructive")),
                    )
                except Exception as exc:
                    log.debug("刷新活动消息框外观失败: %s", exc)
            elif id(top) not in seen_surface_ids:
                reapply = getattr(top, "reapply_theme", None)
                if callable(reapply):
                    try:
                        reapply()
                    except Exception as exc:
                        log.warning("对话框换肤失败 %s: %s", type(top).__name__, exc)
    return effective

"""Frameless SayInk main-window chrome (sidebar + stack)."""

from __future__ import annotations

import sys
import weakref

from PyQt6.QtCore import QEvent, QLineF, QPoint, QRect, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QCursor,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QRegion,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizeGrip,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from sayink.runtime_status import MEETING_STATE_LABELS, RuntimeState, coerce_runtime_status
from sayink.ui import design_tokens as tok
from sayink.ui.settings_components import sync_group_tab_stop

_STATE_TONES = {
    RuntimeState.READY: "GREEN",
    RuntimeState.LOADING: "AMBER",
    RuntimeState.UNAVAILABLE: "RED",
}

NAV_LABELS = ("历史", "会议", "通用", "引擎", "AI 服务", "关于")
PAGE_KEYS = ("history", "meeting", "general", "engine", "polish", "about")
PAGE_OBJECT_NAMES = (
    "pageHistory",
    "pageMeeting",
    "pageGeneral",
    "pageEngine",
    "pagePolish",
    "pageAbout",
)
# Stack slots: 0 = 历史, 1 = 会议, 2 = settings pages (通用/引擎/AI 服务/关于).
_STACK_HISTORY, _STACK_MEETING, _STACK_SETTINGS = 0, 1, 2

_CAPTION_H = 42
_CAPTION_BTN_W = 46
_NAV_BTN_H = 40
_NAV_FONT_PX = 14
_INK_DOT_PX = 8
_STATUS_DOT_PX = 8
_RESIZE_BORDER_PX = 6
_WINDOW_W = 960
_WINDOW_H = 640
_MIN_W = 880
_MIN_H = 580

# Win32 messages used for native edge resizing and a stable maximize rect.
_WM_NCHITTEST = 0x0084
_WM_NCCALCSIZE = 0x0083
_WM_GETMINMAXINFO = 0x0024
_WM_ERASEBKGND = 0x0014
_WM_SHOWWINDOW = 0x0018
_WM_NCPAINT = 0x0085
_WM_NCACTIVATE = 0x0086
_WM_ENTERSIZEMOVE = 0x0231
_WM_EXITSIZEMOVE = 0x0232
_WM_SIZING = 0x0214
_WM_WINDOWPOSCHANGING = 0x0046
# Qt sets this on every size change. It throws away the client bits, and the
# following erase is the full-window flash on an edge or corner drag.
_SWP_NOSIZE = 0x0001
_SWP_NOCOPYBITS = 0x0100
# DWMWA_USE_IMMERSIVE_DARK_MODE (20) and the pre-20H1 value (19).
_DWMWA_TRANSITIONS_FORCEDISABLED = 3
_DWMWA_USE_IMMERSIVE_DARK_MODE_BEFORE_20H1 = 19
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_WINDOW_CORNER_PREFERENCE = 33
_DWMWA_BORDER_COLOR = 34
_DWMWA_CAPTION_COLOR = 35
_DWMWA_SYSTEMBACKDROP_TYPE = 38
_DWMSBT_NONE = 1
# Keep already-drawn pixels against the edges that did not move.
_WVR_ALIGNTOP = 0x0010
_WVR_ALIGNLEFT = 0x0020
_WVR_ALIGNBOTTOM = 0x0040
_WVR_ALIGNRIGHT = 0x0080
# WM_SIZING hit edges. Top and left drags must not anchor to the moving side.
_WMSZ_LEFT, _WMSZ_RIGHT, _WMSZ_TOP = 1, 2, 3
_WMSZ_TOPLEFT, _WMSZ_TOPRIGHT = 4, 5
_WMSZ_BOTTOM, _WMSZ_BOTTOMLEFT, _WMSZ_BOTTOMRIGHT = 6, 7, 8
_MONITOR_DEFAULTTONEAREST = 2


def _win32_draw():
    """user32/gdi32 with pointer-sized handles.

    The defaults truncate a 64-bit HDC, and FillRect then writes off the stack.
    """
    cached = getattr(_win32_draw, "_api", None)
    if cached is not None:
        return cached
    import ctypes
    from ctypes import byref, c_int, c_long, c_uint, c_void_p, windll

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", c_long),
            ("top", c_long),
            ("right", c_long),
            ("bottom", c_long),
        ]

    user32, gdi32 = windll.user32, windll.gdi32
    user32.GetDC.argtypes = [c_void_p]
    user32.GetDC.restype = c_void_p
    user32.ReleaseDC.argtypes = [c_void_p, c_void_p]
    user32.ReleaseDC.restype = c_int
    user32.GetClientRect.argtypes = [c_void_p, ctypes.POINTER(RECT)]
    user32.GetClientRect.restype = c_int
    user32.FillRect.argtypes = [c_void_p, ctypes.POINTER(RECT), c_void_p]
    user32.FillRect.restype = c_int
    gdi32.CreateSolidBrush.argtypes = [c_uint]
    gdi32.CreateSolidBrush.restype = c_void_p
    gdi32.DeleteObject.argtypes = [c_void_p]
    gdi32.DeleteObject.restype = c_int
    cached = (user32, gdi32, RECT, byref)
    _win32_draw._api = cached
    return cached


_HTLEFT, _HTRIGHT, _HTTOP, _HTTOPLEFT, _HTTOPRIGHT = 10, 11, 12, 13, 14
_HTBOTTOM, _HTBOTTOMLEFT, _HTBOTTOMRIGHT = 15, 16, 17
_HTCAPTION = 2
_HTMAXBUTTON = 9
_WM_NCLBUTTONDBLCLK = 0x00A3
# WS_CAPTION | WS_THICKFRAME | WS_SYSMENU | WS_MINIMIZEBOX | WS_MAXIMIZEBOX
_WS_SNAP_STYLE = 0x00C00000 | 0x00040000 | 0x00080000 | 0x00020000 | 0x00010000
_SWP_FRAMECHANGED = 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020
# Hide without moving, resizing, or activating another window first.
_SWP_HIDE_ONLY = 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0080


_CAPTION_GLYPH = 10.0
_CAPTION_STROKE = 1.15


class _CaptionIconButton(QPushButton):
    """Minimize, maximize, restore, and close, drawn in one optical box."""

    def __init__(self, kind: str, parent=None) -> None:
        super().__init__(parent)
        self._kind = kind
        self.setText("")

    @property
    def shows_restore(self) -> bool:
        return self._kind == "restore"

    def set_restore_glyph(self, restore: bool) -> None:
        self._kind = "restore" if restore else "max"
        label = "还原" if restore else "最大化"
        self.setAccessibleName(label)
        self.setToolTip(label)
        self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        from sayink.ui import design_tokens as live

        color = QColor(live.TEXT if self.underMouse() else live.TEXT_SEC)
        pen = QPen(color)
        pen.setWidthF(_CAPTION_STROKE)
        pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        half = _CAPTION_STROKE / 2
        side = _CAPTION_GLYPH - _CAPTION_STROKE
        left = (self.width() - _CAPTION_GLYPH) / 2 + half
        top = (self.height() - _CAPTION_GLYPH) / 2 + half
        radius = 1.2
        if self._kind == "min":
            y = self.height() / 2
            painter.drawLine(QLineF(left, y, left + side, y))
        elif self._kind == "close":
            painter.drawLine(QLineF(left, top, left + side, top + side))
            painter.drawLine(QLineF(left + side, top, left, top + side))
        elif self._kind == "max":
            painter.drawRoundedRect(QRectF(left, top, side, side), radius, radius)
        else:
            overlap = 2.4
            inner = side - overlap
            back = QRectF(left, top, inner, inner)
            front = QRectF(left + overlap, top + overlap, inner, inner)
            cover = QPainterPath()
            cover.addRoundedRect(front.adjusted(-0.7, -0.7, 0.7, 0.7), radius, radius)
            clip = QPainterPath()
            clip.addRect(QRectF(self.rect()))
            painter.save()
            painter.setClipPath(clip.subtracted(cover))
            painter.drawRoundedRect(back, radius, radius)
            painter.restore()
            painter.drawRoundedRect(front, radius, radius)
        painter.end()


class _FrameEdge(QWidget):
    """Straight 1px border in the layout gutter.

    The root window must not grow or drop this line via setStyleSheet: that
    repolishes the whole page tree and is the hitch on maximize and restore.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.palette().color(self.backgroundRole()))
        painter.end()


class MainWindow(QWidget):
    closed_to_tray_first_time = pyqtSignal()

    def __init__(self, config, history_store, parent=None, *, meeting=None) -> None:
        super().__init__(parent)
        self._config = config
        self._history_store = history_store
        # MeetingController; None (tests / tools) builds a page-less stand-in.
        self._meeting_controller = meeting
        self._page = "general"
        self._drag_offset = None
        self._live_resizing = False
        self._sizing_edge = 0
        self._model_runtime_text = "正在启动…"
        self._model_runtime_state = RuntimeState.LOADING
        # A live meeting owns the dot while it runs (see _refresh_usage_summary).
        self._status_tone_override = ""
        self._screen_watchers: list[tuple[object, object, object]] = []
        self._setup_window()
        self._setup_ui()
        self._connect_screen_watchers()
        self.reapply_theme()
        self.show_page("general")
        # Build the native frame while the window is still hidden. Doing it
        # from showEvent runs after Windows has already painted the default
        # caption, which is the white flash on the first open.
        self._prepare_presentation()
        # No programmatic focus on a nav button: the focus ring must only
        # appear for keyboard navigation, never as a second "selected" state.

    @property
    def running_workers(self) -> tuple:
        workers: list = []
        for owner in (self._settings, self._meeting, self._history):
            workers.extend(getattr(owner, "running_workers", ()) or ())
        return tuple(workers)

    def _connect_screen_watchers(self) -> None:
        app = QApplication.instance()
        if app is None:
            return
        weak_self = weakref.ref(self)

        def _on_metrics(*_args) -> None:
            win = weak_self()
            if win is not None:
                win._on_screen_metrics_changed()

        def _on_screen_added(scr) -> None:
            win = weak_self()
            if win is not None:
                win._watch_screen_signals(scr)
                win._on_screen_metrics_changed()

        for sig_name, handler in (
            ("screenAdded", _on_screen_added),
            ("screenRemoved", _on_metrics),
            ("primaryScreenChanged", _on_metrics),
        ):
            sig = getattr(app, sig_name, None)
            if sig is not None:
                sig.connect(handler)
                self._screen_watchers.append((app, sig_name, handler))
        self._screen_metric_handler = _on_metrics
        for scr in app.screens():
            self._watch_screen_signals(scr)
        self.destroyed.connect(self._disconnect_screen_watchers)

    def _watch_screen_signals(self, scr) -> None:
        if scr is None:
            return
        handler = getattr(self, "_screen_metric_handler", None)
        if handler is None:
            return
        for sig_name in ("availableGeometryChanged", "logicalDotsPerInchChanged"):
            sig = getattr(scr, sig_name, None)
            if sig is not None:
                try:
                    sig.connect(handler)
                    self._screen_watchers.append((scr, sig_name, handler))
                except (RuntimeError, TypeError):
                    pass

    def _disconnect_screen_watchers(self, *_args) -> None:
        watchers = getattr(self, "_screen_watchers", ())
        self._screen_watchers = []
        for obj, sig_name, handler in watchers:
            try:
                sig = getattr(obj, sig_name, None)
                if sig is not None:
                    sig.disconnect(handler)
            except (RuntimeError, TypeError):
                pass

    def _on_screen_metrics_changed(self) -> None:
        self._fit_to_available_screen()
        if self.isVisible() and not self.isMaximized() and not self.isMinimized():
            self._clamp_into_visible_screen()

    def _clamp_into_visible_screen(self) -> None:
        from sayink.ui.island_chrome import clamp_top_left, is_screen_alive

        screen = self.screen()
        if not is_screen_alive(screen):
            screen = QApplication.primaryScreen()
        if screen is None:
            return
        try:
            avail = screen.availableGeometry()
        except RuntimeError:
            return
        if avail.isEmpty():
            return
        frame = self.frameGeometry()
        # Re-clamp whenever any screen boundary clips the window or leaves <64px visible.
        if not avail.contains(frame):
            cx, cy = clamp_top_left(self.x(), self.y(), self.width(), self.height(), avail)
            if (cx, cy) != (self.x(), self.y()):
                self.move(cx, cy)

    def _setup_window(self) -> None:
        self.setWindowTitle("SayInk")
        self.setObjectName("mainWindow")
        # Focusable but invisible focus target (see showEvent).
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.setWindowFlags(
            Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # The system brush is the light window color. Skip Qt's own erase;
        # WM_ERASEBKGND paints the theme color instead. A drag that repaints
        # otherwise flashes that white brush under the dark stylesheet.
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self._bg_brush = None
        self._bg_brush_color = None
        self._bg_brush_cell: list[object] = [None]
        brush_cell = self._bg_brush_cell
        owner = weakref.ref(self)

        def release_theme_brush(*_args, _cell=brush_cell, _owner=owner) -> None:
            brush = _cell[0]
            _cell[0] = None
            window = _owner()
            if window is not None:
                d = window.__dict__
                brush = d.get("_bg_brush") or brush
                d["_bg_brush"] = None
                d["_bg_brush_color"] = None
            if brush and sys.platform == "win32":
                try:
                    _user32, gdi32, _rect, _byref = _win32_draw()
                    gdi32.DeleteObject(brush)
                except Exception:
                    pass

        self.destroyed.connect(release_theme_brush)
        weakref.finalize(self, release_theme_brush)
        self.resize(_WINDOW_W, _WINDOW_H)
        self._fit_to_available_screen()

    def _fit_to_available_screen(self) -> None:
        """Small or highly scaled screens may offer less than the preferred size."""
        screen = self.screen()
        if screen is None:
            return
        try:
            avail = screen.availableGeometry()
        except RuntimeError:
            return
        if avail.isEmpty():
            return
        # Never below what the pages need when the screen can hold them, and
        # never wider/taller than the available desktop work area (C-09).
        layout = self.layout()
        floor = layout.minimumSize() if layout is not None else None
        floor_w = floor.width() if floor is not None else 0
        floor_h = floor.height() if floor is not None else 0
        self.setMinimumSize(
            min(avail.width(), max(_MIN_W, floor_w)),
            min(avail.height(), max(_MIN_H, floor_h)),
        )
        if self.isMaximized():
            return
        width, height = min(self.width(), avail.width()), min(self.height(), avail.height())
        if (width, height) != (self.width(), self.height()):
            self.resize(width, height)

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(1, 1, 1, 1)
        root.setSpacing(0)

        self._caption = QWidget()
        self._caption.setObjectName("mainCaption")
        self._caption.setFixedHeight(_CAPTION_H)
        cap = QHBoxLayout(self._caption)
        cap.setContentsMargins(12, 0, 8, 0)
        cap.setSpacing(8)

        self._ink_dot = QLabel()
        self._ink_dot.setFixedSize(_INK_DOT_PX, _INK_DOT_PX)
        self._title = QLabel("SayInk")

        self._min_btn = _CaptionIconButton("min")
        self._max_btn = _CaptionIconButton("max")
        self._close_btn = _CaptionIconButton("close")
        for btn in (self._min_btn, self._max_btn, self._close_btn):
            # Match the Windows caption-button hit target (46×32).
            btn.setFixedSize(_CAPTION_BTN_W, 32)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            # Like native caption buttons: mouse targets, not tab stops.
            # Alt+F4 / Ctrl+W remain the keyboard path.
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        for btn, text in ((self._min_btn, "最小化"),
                          (self._close_btn, "关闭窗口，保留托盘运行")):
            btn.setAccessibleName(text)
            btn.setToolTip(text)

        self._min_btn.clicked.connect(self.showMinimized)
        self._max_btn.clicked.connect(self._toggle_maximized)
        self._close_btn.clicked.connect(self._hide_window)

        cap.addWidget(self._ink_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        cap.addWidget(self._title, 0, Qt.AlignmentFlag.AlignVCenter)
        cap.addStretch(1)
        cap.addWidget(self._min_btn)
        cap.addWidget(self._max_btn)
        cap.addWidget(self._close_btn)
        self._sync_max_glyph()
        for handle in (self._caption, self._title, self._ink_dot):
            handle.installEventFilter(self)
        root.addWidget(self._caption)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        self._sidebar = QWidget()
        self._sidebar.setObjectName("mainSidebar")
        self._sidebar.setFixedWidth(tok.SIDEBAR_WIDTH)
        side = QVBoxLayout(self._sidebar)
        # Vertical margins stay tight so the status block still fits a
        # 1366×768 screen at 150% (about 472 logical pixels).
        side.setContentsMargins(12, 8, 12, 6)
        side.setSpacing(3)

        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)
        self._nav_buttons: list[QPushButton] = []
        for index, label in enumerate(NAV_LABELS):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setFixedHeight(_NAV_BTN_H)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            # TabFocus: keyboard users get a focus ring, mouse clicks do not
            # leave a stale ring competing with the checked state.
            btn.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            btn.setAccessibleName(label)
            btn.setToolTip(f"{label}  ·  Ctrl+{index + 1}")
            key = PAGE_KEYS[index]
            btn.clicked.connect(lambda _checked=False, k=key: self.show_page(k))
            self._nav_group.addButton(btn, index)
            self._nav_buttons.append(btn)
            side.addWidget(btn)
        side.addStretch(1)

        # Runtime status: a separator + status dot + text. Deliberately not a
        # filled card, so it cannot be mistaken for a sixth (selected) nav item.
        self._status_rule = QWidget()
        self._status_rule.setFixedHeight(1)
        side.addWidget(self._status_rule)
        side.addSpacing(4)
        status_row = QHBoxLayout()
        status_row.setContentsMargins(8, 0, 8, 0)
        status_row.setSpacing(8)
        self._status_dot = QLabel()
        self._status_dot.setFixedSize(_STATUS_DOT_PX, _STATUS_DOT_PX)
        status_row.addWidget(self._status_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        self._runtime_label = QLabel("正在启动…")
        self._runtime_label.setWordWrap(True)
        self._runtime_label.setAccessibleName("当前运行状态")
        status_row.addWidget(self._runtime_label, 1, Qt.AlignmentFlag.AlignVCenter)
        side.addLayout(status_row)
        side.addSpacing(4)
        self._mode_label = QLabel()
        self._mode_label.setWordWrap(True)
        self._shortcut_label = QLabel()
        self._shortcut_label.setWordWrap(True)
        self._output_label = QLabel()
        self._output_label.setWordWrap(True)
        self._output_label.setAccessibleName("转写输出去向")
        side.addWidget(self._mode_label)
        side.addWidget(self._output_label)
        side.addWidget(self._shortcut_label)
        body.addWidget(self._sidebar)

        from sayink.ui.history_window import HistoryWindow
        from sayink.ui.settings_window import SettingsWindow

        self._stack = QStackedWidget()
        self._stack.setObjectName("mainStack")
        self._stack.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self._history = HistoryWindow(self._history_store, self, config=self._config)
        self._history.setObjectName(PAGE_OBJECT_NAMES[0])
        self._meeting = self._build_meeting_page()
        self._meeting.setObjectName(PAGE_OBJECT_NAMES[1])
        self._settings = SettingsWindow(self._config, self)
        self._settings.setObjectName(PAGE_OBJECT_NAMES[2])
        set_history_store = getattr(self._settings, "set_history_store", None)
        if callable(set_history_store):
            set_history_store(self._history_store)
        self._stack.addWidget(self._history)
        self._stack.addWidget(self._meeting)
        self._stack.addWidget(self._settings)
        body.addWidget(self._stack, 1)
        root.addLayout(body, 1)
        self._size_grip = QSizeGrip(self)
        self._size_grip.setFixedSize(16, 16)
        self._size_grip.setToolTip("拖动调整窗口大小")
        # The grip resizes through Qt, not the native sizing loop. Without
        # this, every move installs a new rounded region and the corner flashes.
        self._size_grip.installEventFilter(self)
        self._settings.runtime_status_changed.connect(self._set_runtime_status)
        self._settings.navigation_requested.connect(self.show_page)
        self._settings.settings_changed.connect(self._refresh_usage_summary)
        self._settings.hotkey_updated.connect(self._refresh_usage_summary)
        self._settings.usage_hint_changed.connect(self._refresh_usage_summary)
        self._history.history_preferences_requested.connect(self._open_history_preferences)
        self._history.meeting_created.connect(self._open_promoted_meeting)
        self._history.ai_settings_requested.connect(self._open_ai_connection)
        self._settings._history_enabled_row.toggled.connect(self._history.set_history_enabled)
        self._history.set_history_enabled(bool(self._config.get("history.enabled", True)))
        if self._meeting_controller is not None:
            self._meeting_controller.state_changed.connect(self._refresh_usage_summary)
            self._meeting_controller.meeting_changed.connect(self._refresh_usage_summary)
        self._refresh_usage_summary()
        self._shortcuts = []
        for index, key in enumerate(PAGE_KEYS):
            shortcut = QShortcut(QKeySequence(f"Ctrl+{index + 1}"), self)
            shortcut.activated.connect(lambda k=key: self.show_page(k))
            self._shortcuts.append(shortcut)
        for sequence, callback in (("Ctrl+F", self._focus_current_search), ("Ctrl+W", self._hide_window)):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(callback)
            self._shortcuts.append(shortcut)
        self._settings.hotkey_capture_started.connect(lambda: self._enable_shortcuts(False))
        self._settings.hotkey_capture_ended.connect(lambda: self._enable_shortcuts(True))

        # Border lives on these four strips so maximize can hide it without
        # restyling the window that owns every page.
        self._frame_edges = tuple(_FrameEdge(self) for _ in range(4))
        self.resize(_WINDOW_W, _WINDOW_H)
        self._fit_to_available_screen()
        root.activate()

    def _build_meeting_page(self) -> QWidget:
        if self._meeting_controller is None:
            # Without a controller (unit tests, style tools) the slot is a
            # placeholder; the App always passes one.
            placeholder = QWidget()
            lay = QVBoxLayout(placeholder)
            lay.addWidget(QLabel("会议记录在应用内可用"))
            return placeholder
        from sayink.ui.meeting_page import MeetingPage

        page = MeetingPage(self._config, self._meeting_controller, self)
        # Engine-page cards must flip to 「已启用」 when the download ran here.
        # (The settings page is built right after this one; resolve it lazily.)
        page.aux_models_changed.connect(lambda: self._settings._rebuild_model_cards())
        page.configure_requested.connect(self._open_ai_connection)
        return page

    def _enable_shortcuts(self, enabled: bool) -> None:
        for shortcut in self._shortcuts:
            shortcut.setEnabled(enabled)

    def _focus_history_search(self) -> None:
        self.show_page("history")
        self._history._search_edit.setFocus()
        self._history._search_edit.selectAll()

    def _focus_current_search(self) -> None:
        if self._page == "meeting" and self._meeting_controller is not None:
            self._meeting.focus_search()
        else:
            self._focus_history_search()

    def _open_history_preferences(self) -> None:
        self.show_page("general")
        row = self._settings._history_enabled_row
        self._settings._pages.widget(0).ensureWidgetVisible(row)
        row.setFocus()

    def _open_ai_connection(self) -> None:
        self.show_page("polish")
        self._settings.focus_llm_connection()

    def _open_promoted_meeting(self, meeting_id: str) -> None:
        self.show_page("meeting")
        show_meeting = getattr(self._meeting, "show_meeting", None)
        if callable(show_meeting):
            show_meeting(meeting_id)

    def _status_tone(self) -> str:
        """Semantic token for the status dot, from the typed state the engine
        page uses for its own pill — never guessed from the label text."""
        return self._status_tone_override or _STATE_TONES.get(self._model_runtime_state, "TEXT_DIM")

    def _set_runtime_status(self, status) -> None:
        status = coerce_runtime_status(status)
        self._model_runtime_text = status.label
        self._model_runtime_state = status.state
        self._refresh_usage_summary()

    def _paint_status_dot(self) -> None:
        from sayink.ui import design_tokens as live

        color = getattr(live, self._status_tone(), live.TEXT_DIM)
        self._status_dot.setStyleSheet(
            f"background: {color}; border-radius: {_STATUS_DOT_PX // 2}px;"
        )

    def _refresh_usage_summary(self, *_args) -> None:
        from sayink.ui.dictation_hints import dictation_hints
        from sayink.meeting_session import MeetingState
        from sayink.audio_devices import input_source_label

        hints = dictation_hints(self._config)
        meeting = self._meeting_controller
        if meeting is not None and meeting.is_active:
            state = meeting.state
            label = {
                MeetingState.LIVE: MEETING_STATE_LABELS["live"],
                MeetingState.PAUSED: MEETING_STATE_LABELS["paused"],
                MeetingState.DRAINING: MEETING_STATE_LABELS["draining"],
            }.get(state, MEETING_STATE_LABELS["active"])
            record = meeting.current
            source = input_source_label(record.source) if record is not None else "会议音频"
            self._runtime_label.setText(label)
            self._runtime_label.setToolTip(meeting.paused_detail if state is MeetingState.PAUSED else self._model_runtime_text)
            self._status_tone_override = "STATE_RECORD" if state is MeetingState.LIVE else "AMBER"
            self._mode_label.setText(source)
            self._output_label.setText("仅保存会议记录")
            self._output_label.setProperty("outputWarning", False)
            self._shortcut_label.setText("在会议页暂停或结束\n转写快捷键暂不可用")
        else:
            self._status_tone_override = ""
            self._runtime_label.setText(self._model_runtime_text)
            self._runtime_label.setToolTip("")
            self._mode_label.setText(f"{hints.source} · {hints.mode}")
            self._shortcut_label.setText(hints.shortcut)
            self._output_label.setText(hints.output)
            self._output_label.setProperty("outputWarning", hints.output_warning)
        self._paint_status_dot()
        self._paint_output_hint()

    def _paint_output_hint(self) -> None:
        color = tok.RED if self._output_label.property("outputWarning") else tok.TEXT_DIM
        self._output_label.setStyleSheet(
            f"color: {color}; font-size: {tok.TYPE_CAPTION}px;"
            " background: transparent; padding: 2px 8px;"
        )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # The iconic size is not a layout the user sees. Rebuilding the mask
        # and the 1px border for it flashes the full window on the way down
        # and again on the way back.
        if self.isMinimized():
            return
        if hasattr(self, "_size_grip"):
            inset = 6 if self.isMaximized() else 10
            self._size_grip.move(self.width() - 16 - inset, self.height() - 16 - inset)
            self._size_grip.setVisible(not self.isMaximized())
        self._sync_frame_margin()
        self._position_frame_edges()
        if self.isMaximized():
            self._clear_round_mask()
            return
        self._apply_window_shape()

    def show(self) -> None:
        # The tray can reopen a window that is still visible to Qt but minimized
        # by Windows. Restore its previous normal or maximized state first.
        if self.isMinimized():
            self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        # The native frame is applied while the widget is still hidden.
        # SetWindowPos(SWP_FRAMECHANGED) after the window is on screen is the
        # flash where the system caption appears and disappears.
        if not self.isVisible():
            self._prepare_presentation()
            self._bury_surrogate_titlebars()
            self._sync_native_size()
        super().show()

    def _prepare_presentation(self) -> None:
        """Native frame, theme colors, and mask, safe to run before show."""
        self._enable_native_snap()
        self._apply_dwm_theme_colors()
        if self.isMinimized():
            return
        self._fit_to_available_screen()
        if not self.isMaximized():
            self._clamp_into_visible_screen()
        self._sync_frame_margin()
        self._position_frame_edges()
        self._apply_window_shape()
        # The hidden native surface is still the light system color. Paint
        # over it before ShowWindow so the first presented frame is the theme.
        self._fill_native_client()

    def paintEvent(self, event) -> None:
        # Newly exposed pixels start white in the backing store. Fill them
        # before the stylesheet so a resize cannot present that white.
        painter = QPainter(self)
        painter.fillRect(event.rect(), QColor(tok.BG))
        painter.end()
        super().paintEvent(event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._prepare_presentation()
        # Show can parent Qt's helper caption back onto the desktop.
        # Qt may show it again after this handler returns.
        self._bury_surrogate_titlebars()
        self._schedule_bury_surrogates()
        # Park focus on the window itself: otherwise Qt hands it to the first
        # tab stop and a nav button shows a focus ring nobody asked for. Tab
        # from here reaches the nav / page controls as usual.
        fw = self.focusWidget()
        if fw is None or fw in self._nav_buttons:
            self.setFocus(Qt.FocusReason.OtherFocusReason)

    def _sync_max_glyph(self) -> None:
        self._max_btn.set_restore_glyph(self.isMaximized())

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() != QEvent.Type.WindowStateChange:
            return
        if hasattr(self, "_max_btn"):
            self._sync_max_glyph()
        # State changes already resize. Do not restyle or remask here: both
        # run on the UI thread and show up as a pause, then a corner pop.
        self._sync_frame_margin()
        self._position_frame_edges()

    def _toggle_maximized(self) -> None:
        # One paint of the final size. Clearing the rounded region or growing
        # the window with updates on shows a square frame, then waits.
        self.setUpdatesEnabled(False)
        try:
            if self.isMaximized():
                self.showNormal()
            else:
                self._clear_round_mask()
                self.showMaximized()
            self._sync_frame_margin()
            self._position_frame_edges()
            self._apply_window_shape()
        finally:
            self.setUpdatesEnabled(True)

    def _chrome_radius(self) -> int:
        return 0 if self.isMaximized() else tok.WINDOW_RADIUS

    def _dwm_frame_attributes(self) -> dict[int, int]:
        """System frame colors sampled while the stylesheet is still painting.

        Caption and border use the page background. A light-mode DWM window
        otherwise fills the client with white for the first frames of a drag.
        """
        from sayink.ui import design_tokens as live

        bg = QColor(live.BG)
        dark = 1 if bg.lightness() < 128 else 0
        colorref = bg.red() | (bg.green() << 8) | (bg.blue() << 16)
        return {
            # Opening animation draws a small system caption before our frame exists.
            _DWMWA_TRANSITIONS_FORCEDISABLED: 1,
            _DWMWA_USE_IMMERSIVE_DARK_MODE_BEFORE_20H1: dark,
            _DWMWA_USE_IMMERSIVE_DARK_MODE: dark,
            _DWMWA_BORDER_COLOR: colorref,
            _DWMWA_CAPTION_COLOR: colorref,
            _DWMWA_SYSTEMBACKDROP_TYPE: _DWMSBT_NONE,
        }

    def _apply_dwm_attribute(self, attr: int, value: int) -> None:
        if sys.platform != "win32":
            return
        try:
            from ctypes import byref, c_int, c_long, c_void_p, sizeof, windll, wintypes

            hwnd = int(self.winId())
            if hwnd == 0:
                return
            dwm_set = windll.dwmapi.DwmSetWindowAttribute
            if hasattr(dwm_set, "argtypes"):
                dwm_set.argtypes = [wintypes.HWND, wintypes.DWORD, c_void_p, wintypes.DWORD]
                dwm_set.restype = c_long
            dwm_set(hwnd, attr, byref(c_int(value)), sizeof(c_int))
        except Exception:
            return

    def _apply_dwm_theme_colors(self) -> None:
        for attr, value in self._dwm_frame_attributes().items():
            self._apply_dwm_attribute(attr, value)

    def _apply_native_round_corners(self, radius: int) -> None:
        preference = 1 if radius <= 0 else 2
        self._apply_dwm_attribute(_DWMWA_WINDOW_CORNER_PREFERENCE, preference)

    def _clear_round_mask(self) -> None:
        self._mask_key = None
        if not self.mask().isEmpty():
            # Dropping the region invalidates every pixel. The system would
            # erase that with white; paint the theme color in the same turn.
            self.clearMask()
            self._fill_native_client()

    def _apply_chrome_radius(self) -> None:
        """Theme colors for the frame. Maximize does not call this."""
        from sayink.ui import design_tokens as live

        self.setStyleSheet(
            f"QWidget#mainWindow {{ background: {live.BG}; border: none; }}"
        )
        # Keep the erase color on the same background. Stylesheet polish can
        # leave Window on the light style default, which is the flash on drag.
        bg = QColor(live.BG)
        palette = self.palette()
        if palette.color(QPalette.ColorRole.Window) != bg:
            palette.setColor(QPalette.ColorRole.Window, bg)
            self.setPalette(palette)
        self._caption.setStyleSheet(
            f"QWidget#mainCaption {{"
            f" background: {live.BG};"
            f" border: none;"
            f" border-bottom: 1px solid {live.CONTROL_BORDER};"
            f"}}"
        )
        self._sidebar.setStyleSheet(
            f"QWidget#mainSidebar {{ background: {live.NAV_BG};"
            f" border-right: 1px solid {live.HAIRLINE}; }}"
        )
        if hasattr(self, "_frame_edges"):
            color = QColor(live.TEXT_DIM)
            for edge in self._frame_edges:
                palette = edge.palette()
                palette.setColor(edge.backgroundRole(), color)
                edge.setPalette(palette)
        self._sync_frame_margin()
        self._position_frame_edges()
        self._apply_window_shape()
        self._apply_dwm_theme_colors()

    def _sync_frame_margin(self) -> None:
        """1px gutter while windowed so the border is not covered; flush when maximized.

        Done from the resize that maximize already causes, so it shares that
        layout pass instead of shifting the old frame first.
        """
        lay = self.layout()
        if lay is None:
            return
        margin = 0 if self.isMaximized() else 1
        current = lay.contentsMargins()
        if (current.left(), current.top(), current.right(), current.bottom()) == (
            margin,
            margin,
            margin,
            margin,
        ):
            return
        lay.setContentsMargins(margin, margin, margin, margin)

    def _position_frame_edges(self) -> None:
        if not hasattr(self, "_frame_edges"):
            return
        width, height = self.width(), self.height()
        # Collapse instead of hide(): QWidget.show() on a child would also
        # show this window, including during a close that is already in flight.
        if self.isMaximized() or width <= 0 or height <= 0:
            geos = ((0, 0, 0, 0),) * 4
        else:
            geos = (
                (0, 0, width, 1),
                (0, height - 1, width, 1),
                (0, 0, 1, height),
                (width - 1, 0, 1, height),
            )
        for edge, geo in zip(self._frame_edges, geos, strict=True):
            if edge.geometry().getRect() != geo:
                edge.setGeometry(*geo)

    def _apply_window_shape(self) -> None:
        radius = self._chrome_radius()
        if radius != getattr(self, "_native_radius", None):
            self._native_radius = radius
            self._apply_native_round_corners(radius)
        # Swapping the window region on every frame of a drag makes the edges jitter.
        if radius <= 0 or self.isMaximized() or self._live_resizing:
            self._clear_round_mask()
            return
        key = (self.width(), self.height(), radius)
        if key == getattr(self, "_mask_key", None):
            return
        self._mask_key = key
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), float(radius), float(radius))
        self.setMask(QRegion(path.toFillPolygon().toPolygon()))

    @property
    def settings_panel(self):
        """The settings pages (通用 / 引擎 / AI 服务 / 关于); the App talks to it only through this."""
        return self._settings

    @property
    def history_panel(self):
        """The 历史 page."""
        return self._history

    @property
    def meeting_panel(self):
        """The 会议 page (a :class:`~sayink.ui.meeting_page.MeetingPage` inside the App)."""
        return self._meeting

    def show_page(self, key: str) -> None:
        if key not in PAGE_KEYS:
            return
        if key != self._page and not self._current_page_can_leave():
            self._check_nav(self._page)
            return
        self._page = key
        settings_index = {
            "general": 0,
            "engine": 1,
            "polish": 2,
            "about": 3,
        }
        if key == "history":
            self._stack.setCurrentIndex(_STACK_HISTORY)
            self._history.refresh()
        elif key == "meeting":
            self._stack.setCurrentIndex(_STACK_MEETING)
            refresh = getattr(self._meeting, "refresh", None)
            if callable(refresh):
                refresh()
        else:
            self._stack.setCurrentIndex(_STACK_SETTINGS)
            self._settings.show_page(settings_index[key])
        self._check_nav(key)

    def _current_page_can_leave(self) -> bool:
        if self._page == "meeting":
            return self._meeting_can_leave()
        if self._page == "history":
            return self._history_can_leave()
        return True

    def _history_can_leave(self) -> bool:
        leave = getattr(self._history, "try_leave", None)
        if not callable(leave):
            return True
        return bool(leave())

    def _meeting_can_leave(self) -> bool:
        leave = getattr(self._meeting, "try_leave", None)
        if not callable(leave):
            return True
        return bool(leave())

    def _check_nav(self, key: str) -> None:
        btn = self._nav_buttons[PAGE_KEYS.index(key)]
        btn.blockSignals(True)
        btn.setChecked(True)
        btn.blockSignals(False)
        # Tab enters the sidebar on the current page's button (U-07).
        sync_group_tab_stop(self._nav_group)

    def current_page(self) -> str:
        return self._page

    def _caption_drag_target(self, obj) -> bool:
        return obj in (self._caption, self._title, self._ink_dot)

    def eventFilter(self, obj, event):
        if obj is getattr(self, "_size_grip", None):
            etype = event.type()
            if (
                etype == QEvent.Type.MouseButtonPress
                and event.button() == Qt.MouseButton.LeftButton
            ):
                self._live_resizing = True
                self._clear_round_mask()
            elif (
                etype == QEvent.Type.MouseButtonRelease
                and event.button() == Qt.MouseButton.LeftButton
            ):
                self._live_resizing = False
                self._apply_window_shape()
            return super().eventFilter(obj, event)
        # The caption covers the top resize border. A Qt drag from those
        # pixels fights the native top-edge resize and the window shakes.
        if self._caption_drag_target(obj) and event.type() in (
            QEvent.Type.MouseButtonPress,
            QEvent.Type.MouseMove,
            QEvent.Type.MouseButtonRelease,
            QEvent.Type.MouseButtonDblClick,
        ):
            local = self.mapFromGlobal(event.globalPosition().toPoint())
            if self._hit_test_edge(local.x(), local.y()) is not None:
                self._drag_offset = None
                return False
        if self._caption_drag_target(obj) and event.type() == QEvent.Type.MouseButtonDblClick:
            if event.button() == Qt.MouseButton.LeftButton:
                self._drag_offset = None
                self._toggle_maximized()
                return True
        if not self._caption_drag_target(obj) or self.isMaximized():
            if self.isMaximized():
                self._drag_offset = None
            return super().eventFilter(obj, event)
        etype = event.type()
        if (
            etype == QEvent.Type.MouseButtonPress
            and event.button() == Qt.MouseButton.LeftButton
        ):
            self._drag_offset = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )
            return False
        if etype == QEvent.Type.MouseMove and self._drag_offset is not None:
            if event.buttons() & Qt.MouseButton.LeftButton:
                self.move(event.globalPosition().toPoint() - self._drag_offset)
                return True
            self._drag_offset = None
        if (
            etype == QEvent.Type.MouseButtonRelease
            and event.button() == Qt.MouseButton.LeftButton
        ):
            self._drag_offset = None
        return super().eventFilter(obj, event)

    def _hide_window(self) -> None:
        """Leave the app in the tray. The window should already be gone before Qt tears state down.

        ShowWindow(SW_HIDE) activates another window and, on a maximized
        frameless window, can restore the normal rect for a frame first.
        SetWindowPos hides in place.
        """
        if getattr(self, "_hiding", False):
            return
        self._hiding = True
        self._drag_offset = None
        was_visible = self.isVisible()
        try:
            if sys.platform == "win32" and was_visible:
                try:
                    from ctypes import c_int, c_uint, windll, wintypes

                    hwnd = int(self.winId())
                    if hwnd:
                        set_pos = windll.user32.SetWindowPos
                        if hasattr(set_pos, "argtypes"):
                            set_pos.argtypes = [
                                wintypes.HWND, wintypes.HWND, c_int, c_int, c_int, c_int, c_uint,
                            ]
                            set_pos.restype = wintypes.BOOL
                        set_pos(hwnd, 0, 0, 0, 0, 0, _SWP_HIDE_ONLY)
                except Exception:
                    pass
            self.hide()
            on_hidden = getattr(self._settings, "on_window_hidden", None)
            if callable(on_hidden):
                on_hidden()
            if was_visible and not bool(self._config.get("tray_close_hint_shown", False)):
                self._config.set("tray_close_hint_shown", True)
                self.closed_to_tray_first_time.emit()
        finally:
            self._hiding = False

    def closeEvent(self, event) -> None:
        event.ignore()
        self._hide_window()

    def reapply_theme(self) -> None:
        from sayink.ui import design_tokens as live

        self._apply_chrome_radius()
        self._title.setStyleSheet(
            f"color: {live.TEXT}; font-size: {live.TYPE_BODY_SM}px;"
            f" font-weight: 700; background: transparent;"
        )
        self._ink_dot.setStyleSheet(
            f"background: {live.TEXT}; border-radius: {_INK_DOT_PX // 2}px;"
        )
        cap_btn = (
            f"QPushButton {{ background: transparent; color: {live.TEXT_SEC};"
            f" border: none; font-size: {_NAV_FONT_PX}px; }}"
            f"QPushButton:hover {{ color: {live.TEXT};"
            f" background: {live.NAV_SELECTED_BG}; }}"
            f"QPushButton:focus {{ border: 2px solid {live.ACCENT_FOCUS}; }}"
        )
        for btn in (self._min_btn, self._max_btn, self._close_btn):
            btn.setStyleSheet(cap_btn)
        nav_css = (
            f"QPushButton {{ background: transparent; color: {live.TEXT_SEC};"
            f" border: 2px solid transparent; border-radius: {live.RADIUS_MD}px;"
            f" font-size: {_NAV_FONT_PX}px; text-align: left; padding: 0 10px; }}"
            f"QPushButton:checked {{ background: {live.NAV_SELECTED_BG};"
            f" color: {live.TEXT}; font-weight: 700; }}"
            f"QPushButton:hover:!checked {{ background: {live.ROW_HOVER}; color: {live.TEXT}; }}"
            f"QPushButton:focus {{ border-color: {live.ACCENT_FOCUS}; }}"
        )
        for btn in self._nav_buttons:
            btn.setStyleSheet(nav_css)
        for label in (self._mode_label, self._shortcut_label):
            label.setStyleSheet(f"color: {live.TEXT_SEC}; font-size: {live.TYPE_CAPTION}px;"
                               " background: transparent; padding: 2px 8px;")
        self._paint_output_hint()
        self._status_rule.setStyleSheet(f"background: {live.HAIRLINE};")
        self._runtime_label.setStyleSheet(
            f"color: {live.TEXT}; font-size: {live.TYPE_FOOTNOTE}px; font-weight: 700;"
            f" background: transparent; padding: 0;")
        self._paint_status_dot()
        # Scoped: a bare declaration cascades to every descendant QWidget and
        # paints list item widgets opaque, hiding ::item:selected under them.
        self._stack.setStyleSheet(f"QStackedWidget#mainStack {{ background: {live.BG}; }}")
        self._theme_brush()
        if self.isVisible():
            self._fill_native_client()

    # ── native edge resize (Windows) ─────────────────────────────

    def _hit_test_edge(self, x: int, y: int) -> int | None:
        """Return the HT* code for a window-local point on the resize border."""
        if self.isMaximized():
            return None
        b = _RESIZE_BORDER_PX
        w, h = self.width(), self.height()
        left, right = x < b, x >= w - b
        top, bottom = y < b, y >= h - b
        if top and left:
            return _HTTOPLEFT
        if top and right:
            return _HTTOPRIGHT
        if bottom and left:
            return _HTBOTTOMLEFT
        if bottom and right:
            return _HTBOTTOMRIGHT
        if left:
            return _HTLEFT
        if right:
            return _HTRIGHT
        if top:
            return _HTTOP
        if bottom:
            return _HTBOTTOM
        return None

    def _widget_rect(self, widget) -> QRect:
        return QRect(widget.mapTo(self, QPoint(0, 0)), widget.size())

    def _hit_test(self, x: int, y: int) -> int | None:
        """Edge resize, or the caption behaviors a frameless window otherwise loses.

        Empty caption reports HTCAPTION so a drag snaps to the screen edge and
        a drag from the maximized state restores. Min, max, and close stay
        client widgets: HTMAXBUTTON makes Windows 11 draw its own maximize
        glyph over the close button and open snap layouts, and the Qt button
        never receives the click.
        """
        edge = self._hit_test_edge(x, y)
        if edge is not None:
            return edge
        if not hasattr(self, "_caption"):
            return None
        point = QPoint(x, y)
        buttons = (self._min_btn, self._max_btn, self._close_btn)
        if any(self._widget_rect(btn).contains(point) for btn in buttons):
            return None
        if self._widget_rect(self._caption).contains(point):
            return _HTCAPTION
        return None

    def _enable_native_snap(self) -> None:
        """Give the frameless window back Aero Snap and the system menu.

        The style bits are what Windows uses for those gestures. WM_NCCALCSIZE
        then keeps the native caption from painting over the custom one.
        The maximize button stays a client widget; see _hit_test.
        """
        if sys.platform != "win32" or getattr(self, "_native_snap_ready", False):
            return
        try:
            import ctypes
            from ctypes import windll, wintypes

            hwnd = int(self.winId())
            if not hwnd:
                return
            get_long = windll.user32.GetWindowLongPtrW
            set_long = windll.user32.SetWindowLongPtrW
            set_pos = windll.user32.SetWindowPos
            if hasattr(get_long, "argtypes"):
                get_long.restype = ctypes.c_ssize_t
                get_long.argtypes = [wintypes.HWND, ctypes.c_int]
            if hasattr(set_long, "argtypes"):
                set_long.restype = ctypes.c_ssize_t
                set_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
            if hasattr(set_pos, "argtypes"):
                set_pos.argtypes = [
                    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                    ctypes.c_int, ctypes.c_int, ctypes.c_uint,
                ]
                set_pos.restype = wintypes.BOOL
            style = get_long(hwnd, -16)
            set_long(hwnd, -16, style | _WS_SNAP_STYLE)
            set_pos(hwnd, 0, 0, 0, 0, 0, _SWP_FRAMECHANGED)
            self._native_snap_ready = True
            # Qt's helper caption is a real overlapped window at (0, 0). Showing
            # the main window can flash it: system icon, maximize, close, white.
            self._bury_surrogate_titlebars()
            self._sync_native_size()
        except Exception:
            return

    def _fill_min_max_info(self, msg) -> bool:
        """Report the minimum track size and the maximized work-area rect to Windows.

        Handling WM_GETMINMAXINFO here bypasses Qt's own handler, so the minimum
        size must be filled in too; otherwise shrinking from a top/left edge
        moves the window first and Qt snaps it back afterwards.
        """
        import math
        from ctypes import POINTER, Structure, c_long, cast

        class POINT(Structure):
            _fields_ = [("x", c_long), ("y", c_long)]

        class MINMAXINFO(Structure):
            _fields_ = [
                ("ptReserved", POINT),
                ("ptMaxSize", POINT),
                ("ptMaxPosition", POINT),
                ("ptMinTrackSize", POINT),
                ("ptMaxTrackSize", POINT),
            ]

        info = cast(msg.lParam, POINTER(MINMAXINFO)).contents
        ratio = self.devicePixelRatioF() or 1.0
        minimum = self.minimumSize()
        info.ptMinTrackSize.x = math.ceil(minimum.width() * ratio)
        info.ptMinTrackSize.y = math.ceil(minimum.height() * ratio)
        self._fit_maximized_to_work_area(msg)
        return True

    @staticmethod
    def _nccalc_params(msg):
        from ctypes import POINTER, Structure, c_long, c_void_p, cast

        class RECT(Structure):
            _fields_ = [
                ("left", c_long),
                ("top", c_long),
                ("right", c_long),
                ("bottom", c_long),
            ]

        class NCCALCSIZE_PARAMS(Structure):
            _fields_ = [("rgrc", RECT * 3), ("lppos", c_void_p)]

        return cast(msg.lParam, POINTER(NCCALCSIZE_PARAMS)).contents

    def _is_zoomed(self, hwnd) -> bool:
        if self.isFullScreen():
            return True
        try:
            from ctypes import windll, wintypes

            is_zoomed = windll.user32.IsZoomed
            if hasattr(is_zoomed, "argtypes"):
                is_zoomed.argtypes = [wintypes.HWND]
                is_zoomed.restype = wintypes.BOOL
            return bool(is_zoomed(hwnd))
        except Exception:
            return self.isMaximized()

    def _proposed_size_changed(self, msg) -> bool:
        params = self._nccalc_params(msg)
        new, old = params.rgrc[0], params.rgrc[1]
        return (new.right - new.left, new.bottom - new.top) != (
            old.right - old.left,
            old.bottom - old.top,
        )

    def _nudge_client_bottom(self, msg) -> None:
        """Break the top-edge correction loop.

        A thick-frame window whose client rectangle equals the window rectangle
        is nudged down by a pixel whenever the cursor rests on the top sizing
        border, then nudged back, because Windows still expects a non-client
        edge. One extra client pixel past the bottom is clipped and is enough
        for Windows to stop correcting. The proposed rect is replaced on every
        message, so the extra pixel does not accumulate.
        """
        self._nccalc_params(msg).rgrc[0].bottom += 1

    def _keep_resize_bits(self, msg) -> bool:
        """Keep the pixels already on screen while the user drags an edge.

        Qt's platform window sets ``SWP_NOCOPYBITS`` on every size change so a
        framed window does not smear. Here the client is the window, so that
        flag discards the whole page. The erase that follows paints the theme
        color over every pixel: the flash on all four edges and all four corners.
        Consuming the message also skips the frame-margin correction Qt would
        apply from the one-pixel resting nudge.
        """
        if not self._live_resizing:
            return False
        import ctypes
        from ctypes import wintypes

        class WINDOWPOS(ctypes.Structure):
            _fields_ = [
                ("hwnd", wintypes.HWND),
                ("hwndInsertAfter", wintypes.HWND),
                ("x", ctypes.c_int),
                ("y", ctypes.c_int),
                ("cx", ctypes.c_int),
                ("cy", ctypes.c_int),
                ("flags", ctypes.c_uint),
            ]

        pos = ctypes.cast(msg.lParam, ctypes.POINTER(WINDOWPOS)).contents
        if pos.flags & _SWP_NOSIZE:
            return False
        pos.flags &= ~_SWP_NOCOPYBITS
        return True

    def _client_alignment(self) -> int:
        """Anchor existing pixels to the edges that are not being dragged.

        The default copy pins them to the top-left. A top or left drag then
        slides the whole page for a frame. Anchoring to the stable edges
        leaves those pixels where they are, so only the new strip is painted.
        """
        edge = self._sizing_edge
        if edge in (_WMSZ_TOP, _WMSZ_TOPRIGHT):
            return _WVR_ALIGNBOTTOM | _WVR_ALIGNLEFT
        if edge == _WMSZ_TOPLEFT:
            return _WVR_ALIGNBOTTOM | _WVR_ALIGNRIGHT
        if edge in (_WMSZ_LEFT, _WMSZ_BOTTOMLEFT):
            return _WVR_ALIGNTOP | _WVR_ALIGNRIGHT
        return 0

    def _release_theme_brush(self) -> None:
        cell = getattr(self, "_bg_brush_cell", None)
        cell_brush = cell[0] if isinstance(cell, list) and cell else None
        if isinstance(cell, list) and cell:
            cell[0] = None
        brush = getattr(self, "_bg_brush", None) or cell_brush
        self._bg_brush = None
        self._bg_brush_color = None
        if not brush or sys.platform != "win32":
            return
        try:
            _user32, gdi32, _rect, _byref = _win32_draw()
            gdi32.DeleteObject(brush)
        except Exception:
            return

    def _theme_brush(self):
        if sys.platform != "win32":
            return None
        from sayink.ui import design_tokens as live

        color = QColor(live.BG)
        colorref = color.red() | (color.green() << 8) | (color.blue() << 16)
        if self._bg_brush is not None and self._bg_brush_color == colorref:
            return self._bg_brush
        self._release_theme_brush()
        try:
            _user32, gdi32, _rect, _byref = _win32_draw()
            brush = gdi32.CreateSolidBrush(colorref)
        except Exception:
            return None
        if not brush:
            return None
        self._bg_brush = brush
        self._bg_brush_color = colorref
        cell = getattr(self, "_bg_brush_cell", None)
        if isinstance(cell, list) and cell:
            cell[0] = brush
        return brush

    def _fill_dc(self, hdc, rect) -> None:
        brush = self._theme_brush()
        if not brush or not hdc:
            return
        _user32, _gdi32, _rect_type, byref = _win32_draw()
        _user32.FillRect(hdc, byref(rect), brush)

    def _fill_client_dc(self, hwnd, hdc) -> None:
        if not hwnd or not hdc:
            return
        user32, _gdi32, rect_type, byref = _win32_draw()
        rect = rect_type()
        if user32.GetClientRect(hwnd, byref(rect)):
            self._fill_dc(hdc, rect)

    def _uses_win32_surface(self) -> bool:
        if sys.platform != "win32":
            return False
        app = QApplication.instance()
        return app is None or app.platformName() == "windows"

    def _bury_surrogate_titlebars(self) -> None:
        """Keep Qt's `_q_titlebar` window off the desktop.

        Qt creates one overlapped helper per top-level window, at (0, 0), with
        a real system caption. It stays hidden until a show path parents it
        back. That caption is the small box: the app icon, an empty maximize
        square, a close mark, and an unpainted white client.
        """
        if not self._uses_win32_surface() or getattr(self, "_burying_titlebars", False):
            return
        self._burying_titlebars = True
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            wndenumproc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            if hasattr(user32.EnumWindows, "argtypes"):
                user32.EnumWindows.argtypes = [wndenumproc, wintypes.LPARAM]
                user32.EnumWindows.restype = wintypes.BOOL
            if hasattr(user32.EnumChildWindows, "argtypes"):
                user32.EnumChildWindows.argtypes = [wintypes.HWND, wndenumproc, wintypes.LPARAM]
                user32.EnumChildWindows.restype = wintypes.BOOL
            user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
            user32.GetWindowThreadProcessId.argtypes = [
                wintypes.HWND, ctypes.POINTER(wintypes.DWORD),
            ]
            user32.IsWindow.argtypes = [wintypes.HWND]
            user32.IsWindow.restype = wintypes.BOOL
            user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.SetParent.argtypes = [wintypes.HWND, wintypes.HWND]
            user32.SetParent.restype = wintypes.HWND
            user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
            user32.SetWindowLongPtrW.argtypes = [
                wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t,
            ]
            user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
            user32.SetWindowPos.argtypes = [
                wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                ctypes.c_int, ctypes.c_int, ctypes.c_uint,
            ]
            pid = ctypes.windll.kernel32.GetCurrentProcessId()
            hwnd_message = ctypes.c_void_p(-3)  # HWND_MESSAGE
            titlebars: list[int] = []
            trays: list[int] = []

            @wndenumproc
            def collect(hwnd, _lparam):
                owner = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
                if owner.value != pid:
                    return True
                name = ctypes.create_unicode_buffer(96)
                user32.GetClassNameW(hwnd, name, 96)
                if name.value == "_q_titlebar":
                    titlebars.append(int(hwnd))
                elif name.value.endswith("TrayIconMessageWindowClass"):
                    trays.append(int(hwnd))
                return True

            user32.EnumWindows(collect, 0)
            # Once buried, the helper is a child (or message-only) and EnumWindows
            # no longer sees it. A later show parents it back onto this window.
            owner_hwnd = int(self.winId() or 0)
            if owner_hwnd:
                try:
                    user32.EnumChildWindows(owner_hwnd, collect, 0)
                except Exception:
                    pass
            # The tray owner is an overlapped window. A double-click can show
            # it: system icon, maximize, close, and an unpainted client.
            # keep_hwnd_hidden also refuses a later ShowWindow from the shell.
            from sayink.ui.tray_icon import keep_hwnd_hidden

            for hwnd in trays:
                keep_hwnd_hidden(hwnd)
            # Caption, thick frame, system menu, minimize, maximize.
            caption_bits = 0x00C00000 | 0x00040000 | 0x00080000 | 0x00020000 | 0x00010000
            for hwnd in titlebars:
                if not user32.IsWindow(hwnd):
                    continue
                user32.ShowWindow(hwnd, 0)
                style = user32.GetWindowLongPtrW(hwnd, -16)
                buried = (style & ~caption_bits) | 0x80000000  # WS_POPUP
                if buried != style:
                    user32.SetWindowLongPtrW(hwnd, -16, buried)
                keep_hwnd_hidden(hwnd)
                user32.SetParent(hwnd, hwnd_message)
                user32.SetWindowPos(
                    hwnd, 0, -32000, -32000, 0, 0,
                    0x0001 | 0x0004 | 0x0010 | 0x0080,
                )
        except Exception:
            return
        finally:
            self._burying_titlebars = False

    def _schedule_bury_surrogates(self) -> None:
        """Hide helper captions again after show() returns them to the desktop."""
        from PyQt6.QtCore import QTimer

        timer = getattr(self, "_bury_timer", None)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self._bury_surrogate_titlebars)
            self._bury_timer = timer
        timer.start(0)

    def _sync_native_size(self) -> None:
        """Grow a caption-sized native frame up to the widget before it is shown."""
        if (
            not self._uses_win32_surface()
            or self.isMaximized()
            or self.isFullScreen()
            or self._live_resizing
            or getattr(self, "_syncing_native_size", False)
        ):
            return
        self._syncing_native_size = True
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32

            class RECT(ctypes.Structure):
                _fields_ = [
                    ("left", wintypes.LONG),
                    ("top", wintypes.LONG),
                    ("right", wintypes.LONG),
                    ("bottom", wintypes.LONG),
                ]

            user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
            user32.SetWindowPos.argtypes = [
                wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                ctypes.c_int, ctypes.c_int, ctypes.c_uint,
            ]
            hwnd = int(self.winId())
            if not hwnd:
                return
            rect = RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return
            ratio = self.devicePixelRatioF() or 1.0
            want_w = max(1, round(self.width() * ratio))
            want_h = max(1, round(self.height() * ratio))
            have_w, have_h = rect.right - rect.left, rect.bottom - rect.top
            if have_w + 8 >= want_w and have_h + 8 >= want_h:
                return
            user32.SetWindowPos(
                hwnd, 0, 0, 0, want_w, want_h,
                0x0002 | 0x0004 | 0x0010,
            )
        except Exception:
            return
        finally:
            self._syncing_native_size = False

    def _fill_native_client(self) -> None:
        """Paint the theme color into the Win32 client, which starts out white."""
        if not self._uses_win32_surface():
            return
        try:
            user32, _gdi32, _rect, _byref = _win32_draw()
            hwnd = int(self.winId())
            if not hwnd:
                return
            hdc = user32.GetDC(hwnd)
            if not hdc:
                return
            try:
                self._fill_client_dc(hwnd, hdc)
            finally:
                user32.ReleaseDC(hwnd, hdc)
        except Exception:
            return

    def _erase_background(self, msg) -> None:
        if not self._uses_win32_surface():
            return
        try:
            hwnd = int(msg.hWnd)
            hdc = int(msg.wParam)
        except Exception:
            return
        if not hwnd or not hdc:
            return
        try:
            self._fill_client_dc(hwnd, hdc)
        except Exception:
            return

    def _fit_maximized_to_work_area(self, msg) -> bool:
        """Pin the maximized frame to the monitor work area, without the frame offset."""
        from ctypes import POINTER, Structure, byref, c_long, c_ulong, cast, sizeof, windll, wintypes

        class POINT(Structure):
            _fields_ = [("x", c_long), ("y", c_long)]

        class MINMAXINFO(Structure):
            _fields_ = [
                ("ptReserved", POINT),
                ("ptMaxSize", POINT),
                ("ptMaxPosition", POINT),
                ("ptMinTrackSize", POINT),
                ("ptMaxTrackSize", POINT),
            ]

        class RECT(Structure):
            _fields_ = [
                ("left", c_long),
                ("top", c_long),
                ("right", c_long),
                ("bottom", c_long),
            ]

        class MONITORINFO(Structure):
            _fields_ = [
                ("cbSize", c_ulong),
                ("rcMonitor", RECT),
                ("rcWork", RECT),
                ("dwFlags", c_ulong),
            ]

        mon_from_win = windll.user32.MonitorFromWindow
        get_mon_info = windll.user32.GetMonitorInfoW
        if hasattr(mon_from_win, "argtypes"):
            mon_from_win.argtypes = [wintypes.HWND, wintypes.DWORD]
            mon_from_win.restype = wintypes.HMONITOR
        if hasattr(get_mon_info, "argtypes"):
            get_mon_info.argtypes = [wintypes.HMONITOR, POINTER(MONITORINFO)]
            get_mon_info.restype = wintypes.BOOL

        info = MONITORINFO()
        info.cbSize = sizeof(MONITORINFO)
        monitor = mon_from_win(msg.hWnd, _MONITOR_DEFAULTTONEAREST)
        if not monitor or not get_mon_info(monitor, byref(info)):
            return False
        fitted = cast(msg.lParam, POINTER(MINMAXINFO)).contents
        work, mon = info.rcWork, info.rcMonitor
        fitted.ptMaxPosition.x = work.left - mon.left
        fitted.ptMaxPosition.y = work.top - mon.top
        fitted.ptMaxSize.x = work.right - work.left
        fitted.ptMaxSize.y = work.bottom - work.top
        return True

    def nativeEvent(self, event_type, message):
        # Note: deliberately never delegates to super().nativeEvent(); the
        # PyQt6 base implementation faults when re-entered with the voidptr.
        # QWidget's default is a no-op, so (False, 0) is equivalent.
        if sys.platform == "win32" and event_type in (b"windows_generic_MSG", "windows_generic_MSG"):
            try:
                from ctypes import wintypes

                msg = wintypes.MSG.from_address(int(message))
                if msg.message == _WM_ERASEBKGND:
                    # Windows erases with the light class brush before Qt paints.
                    # That full-window white is the flash on show. During a drag
                    # the old pixels are still valid; filling them is the flicker.
                    if not self._live_resizing:
                        self._erase_background(msg)
                    return True, 1
                if msg.message == _WM_WINDOWPOSCHANGING and self._keep_resize_bits(msg):
                    return True, 0
                if msg.message == _WM_NCPAINT:
                    # WS_CAPTION is on for snap. Painting that non-client frame
                    # is the small system caption (icon, maximize, close).
                    return True, 0
                if msg.message == _WM_NCACTIVATE:
                    # Activation otherwise draws the default title bar for a frame.
                    return True, 1
                if msg.message == _WM_SHOWWINDOW and msg.wParam:
                    self._fill_native_client()
                    self._bury_surrogate_titlebars()
                    self._sync_native_size()
                if msg.message == _WM_GETMINMAXINFO:
                    if self._fill_min_max_info(msg):
                        return True, 0
                if msg.message == _WM_NCLBUTTONDBLCLK and int(msg.wParam) == _HTCAPTION:
                    self._toggle_maximized()
                    return True, 0
                if msg.message == _WM_NCCALCSIZE and msg.wParam:
                    # Decide from the rect Windows proposed. The one-pixel client
                    # extension below would otherwise look like a size change.
                    # A caption move has no sizing edge and keeps the resting rect.
                    # An edge drag anchors the existing pixels instead of throwing
                    # them away: a full discard is erased white for one frame.
                    resizing = bool(self._sizing_edge) and self._proposed_size_changed(msg)
                    # The extra pixel is only for a resting top border. During a
                    # drag it makes the client one pixel taller than the window
                    # Qt is sizing, so the two rects chase each other.
                    if (
                        not self._is_zoomed(msg.hWnd)
                        and not self._live_resizing
                        and not self._sizing_edge
                    ):
                        self._nudge_client_bottom(msg)
                    if resizing:
                        return True, self._client_alignment()
                    # WS_CAPTION is set so snap and Alt+Space work. The nudged
                    # client stays one pixel past the bottom while windowed;
                    # maximized keeps the work-area rect from WM_GETMINMAXINFO.
                    return True, 0
                if msg.message == _WM_ENTERSIZEMOVE:
                    # Caption moves and edge resizes both enter here. Dropping
                    # the region now repaints every pixel. Wait until the size
                    # actually changes.
                    self._drag_offset = None
                    self._sizing_edge = 0
                    self._live_resizing = True
                elif msg.message == _WM_SIZING:
                    self._sizing_edge = int(msg.wParam)
                    self._live_resizing = True
                    self._clear_round_mask()
                elif msg.message == _WM_EXITSIZEMOVE:
                    self._drag_offset = None
                    self._sizing_edge = 0
                    self._live_resizing = False
                    self._apply_window_shape()
                if msg.message == _WM_NCHITTEST and self.windowHandle() is not None:
                    # QCursor.pos() is already in logical coordinates, which
                    # keeps the hit test correct on mixed-DPI monitor setups.
                    local = self.mapFromGlobal(QCursor.pos())
                    hit = self._hit_test(local.x(), local.y())
                    if hit is not None:
                        return True, hit
            except Exception:
                pass
        return False, 0

    def focusNextPrevChild(self, next: bool) -> bool:
        if self.current_page() == "general":
            self._settings.general_page.prepare_tab_navigation(next)
        return super().focusNextPrevChild(next)

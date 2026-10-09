"""Frameless SayInk main-window chrome (sidebar + stack)."""

from __future__ import annotations

import sys

from PyQt6.QtCore import QEvent, QLineF, QRectF, Qt
from PyQt6.QtGui import (
    QColor,
    QCursor,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPen,
    QRegion,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QSizeGrip,
    QVBoxLayout,
    QWidget,
)

from sayink.config import DEFAULT_HOTKEY, format_hotkey
from sayink.ui import design_tokens as tok

NAV_LABELS = ("历史", "通用", "引擎", "润色", "关于")
PAGE_KEYS = ("history", "general", "engine", "polish", "about")
PAGE_OBJECT_NAMES = (
    "pageHistory",
    "pageGeneral",
    "pageEngine",
    "pagePolish",
    "pageAbout",
)

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
_WM_ENTERSIZEMOVE = 0x0231
_WM_EXITSIZEMOVE = 0x0232
_WVR_VALIDRECTS = 0x0400
_MONITOR_DEFAULTTONEAREST = 2
_HTLEFT, _HTRIGHT, _HTTOP, _HTTOPLEFT, _HTTOPRIGHT = 10, 11, 12, 13, 14
_HTBOTTOM, _HTBOTTOMLEFT, _HTBOTTOMRIGHT = 15, 16, 17


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


class MainWindow(QWidget):
    def __init__(self, config, history_store, parent=None) -> None:
        super().__init__(parent)
        self._config = config
        self._history_store = history_store
        self._page = "general"
        self._drag_offset = None
        self._live_resizing = False
        self._setup_window()
        self._setup_ui()
        self.reapply_theme()
        self.show_page("general")
        # No programmatic focus on a nav button: the focus ring must only
        # appear for keyboard navigation, never as a second "selected" state.

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
        self.resize(_WINDOW_W, _WINDOW_H)
        self._fit_to_available_screen()

    def _fit_to_available_screen(self) -> None:
        """Small or highly scaled screens may offer less than the preferred size."""
        screen = self.screen()
        if screen is None:
            return
        avail = screen.availableGeometry()
        if avail.isEmpty():
            return
        # Never below what the pages need, or controls get clipped instead.
        layout = self.layout()
        floor = layout.minimumSize() if layout is not None else None
        floor_w = floor.width() if floor is not None else 0
        floor_h = floor.height() if floor is not None else 0
        self.setMinimumSize(
            min(_MIN_W, max(avail.width(), floor_w)),
            min(_MIN_H, max(avail.height(), floor_h)),
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
        self._close_btn.clicked.connect(self.hide)

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
        side.setContentsMargins(12, 16, 12, 16)
        side.setSpacing(4)

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
        side.addSpacing(10)
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
        side.addSpacing(6)
        self._mode_label = QLabel()
        self._mode_label.setWordWrap(True)
        self._shortcut_label = QLabel()
        self._shortcut_label.setWordWrap(True)
        side.addWidget(self._mode_label)
        side.addWidget(self._shortcut_label)
        body.addWidget(self._sidebar)

        from sayink.ui.history_window import HistoryWindow
        from sayink.ui.settings_window import SettingsWindow

        self._stack = QStackedWidget()
        self._history = HistoryWindow(self._history_store, self)
        self._history.setObjectName(PAGE_OBJECT_NAMES[0])
        self._settings = SettingsWindow(self._config, self)
        self._settings.setObjectName(PAGE_OBJECT_NAMES[1])
        self._stack.addWidget(self._history)
        self._stack.addWidget(self._settings)
        body.addWidget(self._stack, 1)
        root.addLayout(body, 1)
        self._size_grip = QSizeGrip(self)
        self._size_grip.setFixedSize(16, 16)
        self._size_grip.setToolTip("拖动调整窗口大小")
        self._settings.runtime_status_changed.connect(self._set_runtime_status)
        self._settings.settings_changed.connect(self._refresh_usage_summary)
        self._settings.hotkey_updated.connect(self._refresh_usage_summary)
        self._history.history_preferences_requested.connect(self._open_history_preferences)
        self._settings._history_enabled_row.toggled.connect(self._history.set_history_enabled)
        self._history.set_history_enabled(bool(self._config.get("history.enabled", False)))
        self._refresh_usage_summary()
        self._shortcuts = []
        for index, key in enumerate(PAGE_KEYS):
            shortcut = QShortcut(QKeySequence(f"Ctrl+{index + 1}"), self)
            shortcut.activated.connect(lambda k=key: self.show_page(k))
            self._shortcuts.append(shortcut)
        for sequence, callback in (("Ctrl+F", self._focus_history_search), ("Ctrl+W", self.hide)):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(callback)
            self._shortcuts.append(shortcut)
        self._settings.hotkey_capture_started.connect(lambda: self._enable_shortcuts(False))
        self._settings.hotkey_capture_ended.connect(lambda: self._enable_shortcuts(True))

        self.resize(_WINDOW_W, _WINDOW_H)
        self._fit_to_available_screen()
        root.activate()

    def _enable_shortcuts(self, enabled: bool) -> None:
        for shortcut in self._shortcuts:
            shortcut.setEnabled(enabled)

    def _focus_history_search(self) -> None:
        self.show_page("history")
        self._history._search_edit.setFocus()
        self._history._search_edit.selectAll()

    def _open_history_preferences(self) -> None:
        self.show_page("general")
        row = self._settings._history_enabled_row
        self._settings._pages.widget(0).ensureWidgetVisible(row)
        row.setFocus()

    @staticmethod
    def _status_tone(text: str) -> str:
        """Map a runtime status string to a semantic token name."""
        if any(word in text for word in ("失败", "错误", "不可用")):
            return "RED"
        if any(word in text for word in ("载入", "加载", "启动", "未就绪", "下载")):
            return "AMBER"
        if "就绪" in text:
            return "GREEN"
        return "TEXT_DIM"

    def _set_runtime_status(self, text: str) -> None:
        self._runtime_label.setText(text)
        self._paint_status_dot()

    def _paint_status_dot(self) -> None:
        from sayink.ui import design_tokens as live

        color = getattr(live, self._status_tone(self._runtime_label.text()), live.TEXT_DIM)
        self._status_dot.setStyleSheet(
            f"background: {color}; border-radius: {_STATUS_DOT_PX // 2}px;"
        )

    def _refresh_usage_summary(self, *_args) -> None:
        source = {"microphone": "麦克风", "system": "电脑播放", "mixed": "混合音频"}.get(
            self._config.get("audio.input_source", "microphone"), "麦克风")
        continuous = self._config.get("audio.trigger_mode", "continuous") == "continuous"
        self._mode_label.setText(f"{source} · {'持续转写' if continuous else '按住说话'}")
        hotkey = format_hotkey(self._config.get("hotkey", DEFAULT_HOTKEY))
        self._shortcut_label.setText(f"按住 {hotkey}\n" + (
            "停顿出字 · Esc /「结束」停止" if continuous else "松开出字 · Esc 取消"))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "_size_grip"):
            inset = 6 if self.isMaximized() else 10
            self._size_grip.move(self.width() - 16 - inset, self.height() - 16 - inset)
            self._size_grip.setVisible(not self.isMaximized())
        if self.isMaximized():
            self._clear_round_mask()
            return
        self._apply_window_shape()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._fit_to_available_screen()
        self._apply_window_shape()
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
        if hasattr(self, "_caption"):
            self._apply_chrome_radius()

    def _toggle_maximized(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            # Drop the rounded clip before the window grows, so intermediate
            # sizes are not masked and then snapped square.
            self._clear_round_mask()
            self.showMaximized()
        self._apply_chrome_radius()
        self._sync_max_glyph()

    def _chrome_radius(self) -> int:
        return 0 if self.isMaximized() else tok.WINDOW_RADIUS

    def _apply_native_round_corners(self, radius: int) -> None:
        if sys.platform != "win32":
            return
        try:
            from ctypes import byref, c_int, sizeof, windll

            hwnd = int(self.winId())
            if hwnd == 0:
                return
            preference = 1 if radius <= 0 else 2
            windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 33, byref(c_int(preference)), sizeof(c_int)
            )
        except Exception:
            pass

    def _clear_round_mask(self) -> None:
        self._mask_key = None
        if not self.mask().isEmpty():
            self.clearMask()

    def _apply_chrome_radius(self) -> None:
        """Update only the corner chrome. A full theme pass flashes on maximize."""
        from sayink.ui import design_tokens as live

        radius = self._chrome_radius()
        border = "none" if radius <= 0 else f"1px solid {live.TEXT_DIM}"
        self.setStyleSheet(
            f"QWidget#mainWindow {{ background: {live.BG};"
            f" border: {border};"
            f" border-radius: {radius}px; }}"
        )
        self._caption.setStyleSheet(
            f"QWidget#mainCaption {{"
            f" background: {live.BG};"
            f" border: none;"
            f" border-bottom: 1px solid {live.CONTROL_BORDER};"
            f" border-top-left-radius: {radius}px;"
            f" border-top-right-radius: {radius}px;"
            f"}}"
        )
        self._sidebar.setStyleSheet(
            f"QWidget#mainSidebar {{ background: {live.NAV_BG};"
            f" border-right: 1px solid {live.HAIRLINE};"
            f" border-bottom-left-radius: {radius}px; }}"
        )
        inset = 0 if radius <= 0 else 1
        lay = self.layout()
        if lay is not None:
            lay.setContentsMargins(inset, inset, inset, inset)
        self._apply_window_shape()

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

    def show_page(self, key: str) -> None:
        if key not in PAGE_KEYS:
            return
        self._page = key
        settings_index = {
            "general": 0,
            "engine": 1,
            "polish": 2,
            "about": 3,
        }
        if key == "history":
            self._stack.setCurrentIndex(0)
            self._history.refresh()
        else:
            self._stack.setCurrentIndex(1)
            self._settings.show_page(settings_index[key])
        nav_index = PAGE_KEYS.index(key)
        btn = self._nav_buttons[nav_index]
        btn.blockSignals(True)
        btn.setChecked(True)
        btn.blockSignals(False)

    def current_page(self) -> str:
        return self._page

    def _caption_drag_target(self, obj) -> bool:
        return obj in (self._caption, self._title, self._ink_dot)

    def eventFilter(self, obj, event):
        if self._caption_drag_target(obj) and event.type() == QEvent.Type.MouseButtonDblClick:
            if event.button() == Qt.MouseButton.LeftButton:
                self._toggle_maximized()
                return True
        if not self._caption_drag_target(obj) or self.isMaximized():
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
        if (
            etype == QEvent.Type.MouseMove
            and self._drag_offset is not None
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            return True
        if (
            etype == QEvent.Type.MouseButtonRelease
            and event.button() == Qt.MouseButton.LeftButton
        ):
            self._drag_offset = None
        return super().eventFilter(obj, event)

    def closeEvent(self, event) -> None:
        event.ignore()
        self.hide()

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
        self._status_rule.setStyleSheet(f"background: {live.HAIRLINE};")
        self._runtime_label.setStyleSheet(
            f"color: {live.TEXT}; font-size: {live.TYPE_FOOTNOTE}px; font-weight: 700;"
            f" background: transparent; padding: 0;")
        self._paint_status_dot()
        self._stack.setStyleSheet(f"background: {live.BG};")

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
    def _skip_stale_client_copy(msg) -> bool:
        """Stop Windows copying old client pixels while resizing from a top/left edge.

        The frameless client area already equals the window rect, so rgrc[0] stays
        as proposed; identical 1px source/destination rects make the copy a no-op
        and Qt repaints the whole client area instead.
        """
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

        params = cast(msg.lParam, POINTER(NCCALCSIZE_PARAMS)).contents
        new, old = params.rgrc[0], params.rgrc[1]
        if (new.right - new.left, new.bottom - new.top) == (
            old.right - old.left, old.bottom - old.top
        ):
            return False
        for index in (1, 2):
            params.rgrc[index].left = new.left
            params.rgrc[index].top = new.top
            params.rgrc[index].right = new.left + 1
            params.rgrc[index].bottom = new.top + 1
        return True

    def _fit_maximized_to_work_area(self, msg) -> bool:
        """Pin the maximized frame to the monitor work area, without the frame offset."""
        from ctypes import POINTER, Structure, byref, c_long, c_ulong, cast, sizeof, windll

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

        info = MONITORINFO()
        info.cbSize = sizeof(MONITORINFO)
        monitor = windll.user32.MonitorFromWindow(msg.hWnd, _MONITOR_DEFAULTTONEAREST)
        if not monitor or not windll.user32.GetMonitorInfoW(monitor, byref(info)):
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
                if msg.message == _WM_GETMINMAXINFO:
                    if self._fill_min_max_info(msg):
                        return True, 0
                if msg.message == _WM_NCCALCSIZE and msg.wParam:
                    if self._skip_stale_client_copy(msg):
                        return True, _WVR_VALIDRECTS
                if msg.message == _WM_ENTERSIZEMOVE:
                    self._live_resizing = True
                    self._clear_round_mask()
                elif msg.message == _WM_EXITSIZEMOVE:
                    self._live_resizing = False
                    self._apply_window_shape()
                if msg.message == _WM_NCHITTEST and self.windowHandle() is not None:
                    # QCursor.pos() is already in logical coordinates, which
                    # keeps the hit test correct on mixed-DPI monitor setups.
                    local = self.mapFromGlobal(QCursor.pos())
                    hit = self._hit_test_edge(local.x(), local.y())
                    if hit is not None:
                        return True, hit
            except Exception:
                pass
        return False, 0

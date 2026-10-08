import sys

from PyQt6.QtWidgets import QSystemTrayIcon, QMenu
from PyQt6.QtGui import (
    QIcon, QIconEngine, QPixmap, QPainter, QColor, QBrush, QPen,
    QRadialGradient, QPainterPath, QActionGroup
)
from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot, QSize, QRect, QRectF, QPoint, QPointF

def _menu_font():
    from PyQt6.QtGui import QFont

    from sayink.ui import design_tokens as tok

    font = QFont(tok.UI_FONT_FAMILY)
    font.setPixelSize(tok.TRAY_MENU_FONT)
    return font


def _menu_stylesheet() -> str:
    from sayink.ui import design_tokens as tok

    # Windows styled menus do not inherit QMenu's font onto items, and they
    # do not reserve a check column. Pad every row the same so labels line up
    # past the check, with a matching gutter for the submenu chevron.
    pad_right = tok.TRAY_MENU_PAD_H + 4
    return f"""
    QMenu {{
        background-color: {tok.SURFACE};
        color: {tok.TEXT};
        border: 1px solid {tok.TRAY_MENU_BORDER};
        border-radius: {tok.TRAY_MENU_RADIUS}px;
        padding: 4px 0px;
        font-family: {tok.FONT};
        font-size: {tok.TRAY_MENU_FONT}px;
    }}
    QMenu::item {{
        font-family: {tok.FONT};
        font-size: {tok.TRAY_MENU_FONT}px;
        padding: {tok.TRAY_MENU_PAD_V}px {pad_right}px {tok.TRAY_MENU_PAD_V}px {tok.TRAY_MENU_PAD_H}px;
        margin: 0px;
        border-radius: 0px;
        background: transparent;
    }}
    QMenu::item:selected {{
        background-color: {tok.TRAY_MENU_HOVER};
        color: {tok.TEXT};
    }}
    QMenu::item:disabled {{
        color: {tok.TEXT_DIM};
        background: transparent;
        font-size: {tok.TYPE_CAPTION}px;
    }}
    QMenu::item:disabled:selected {{
        background: transparent;
    }}
    QMenu::separator {{
        height: 1px;
        background: {tok.TRAY_MENU_SEPARATOR};
        margin: 6px 12px;
    }}
    QMenu::indicator {{
        width: 12px;
        height: 12px;
        margin-left: 6px;
    }}
    QMenu::right-arrow {{
        width: 8px;
        height: 8px;
        margin-right: 12px;
    }}
    """


_MENU_JOIN = " · "


def _paired_label(lead: str, detail: str) -> str:
    """Keep a caption and its value adjacent. Extra menu width stays to the right."""
    lead = (lead or "").strip()
    detail = (detail or "").strip()
    if lead and detail:
        return f"{lead}{_MENU_JOIN}{detail}"
    return lead or detail


def _model_switch_title(active_name: str) -> str:
    return _paired_label("切换模型", active_name) or "切换模型"


def _model_choice_label(model: dict) -> str:
    return _paired_label(str(model.get("name") or ""), str(model.get("languages") or ""))


def _paint_menu(menu) -> None:
    if menu is None:
        return
    menu.setFont(_menu_font())
    menu.setStyleSheet(_menu_stylesheet())


def _windows_small_icon_px() -> int | None:
    """Notification-area glyph size. A mismatch here is what leaves the slot blank."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        px = int(ctypes.windll.user32.GetSystemMetrics(49))  # SM_CXSMICON
    except (AttributeError, OSError, ValueError):
        return None
    if 16 <= px <= 64:
        return px
    return None


def _microphone_pixmap(color: str | None, recording: bool, size: int) -> QPixmap:
    from sayink.ui import design_tokens as tok

    pixmap = QPixmap(QSize(size, size))
    # Windows drops the tray glyph when the bitmap's device pixel ratio is not 1.
    pixmap.setDevicePixelRatio(1.0)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    s = size
    cx = s / 2

    if recording:
        record = QColor(tok.STATE_RECORD)
        darker = QColor(record)
        darker = darker.darker(120)
        bg_grad = QRadialGradient(QPointF(cx, cx), s * 0.45)
        bg_grad.setColorAt(0, record)
        bg_grad.setColorAt(1, darker)
    else:
        top = QColor(color or tok.TEXT)
        bottom = QColor(color or tok.TEXT)
        bg_grad = QRadialGradient(QPointF(cx, cx), s * 0.45)
        bg_grad.setColorAt(0, top)
        bg_grad.setColorAt(1, bottom)

    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(bg_grad))
    margin = s * 0.06
    painter.drawRoundedRect(
        QRectF(margin, margin, s - 2 * margin, s - 2 * margin),
        s * 0.22, s * 0.22
    )

    # Dark theme plate is near-white (#F9FAFB). A white mic on that plate
    # has no silhouette, so the tray glyph reads as empty.
    plate = QColor(tok.STATE_RECORD if recording else (color or tok.TEXT))
    glyph = QColor(26, 26, 26, 245) if plate.lightness() > 200 else QColor(255, 255, 255, 245)
    grille = QColor(255, 255, 255, 90) if glyph.lightness() < 128 else QColor(26, 26, 26, 90)

    mic_w = s * 0.22
    mic_h = s * 0.32
    mic_x = cx - mic_w / 2
    mic_y = s * 0.16

    mic_path = QPainterPath()
    mic_path.addRoundedRect(QRectF(mic_x, mic_y, mic_w, mic_h), mic_w / 2, mic_w / 2)
    painter.setBrush(QBrush(glyph))
    painter.drawPath(mic_path)

    painter.setPen(QPen(grille, 1))
    grille_top = mic_y + mic_h * 0.3
    grille_bottom = mic_y + mic_h * 0.7
    for i in range(3):
        y = grille_top + (grille_bottom - grille_top) * i / 2
        painter.drawLine(QPointF(mic_x + mic_w * 0.25, y), QPointF(mic_x + mic_w * 0.75, y))

    arc_pen = QPen(glyph, s * 0.035)
    arc_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(arc_pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    arc_w = s * 0.38
    arc_h = s * 0.28
    arc_rect = QRectF(cx - arc_w / 2, s * 0.26, arc_w, arc_h)
    painter.drawArc(arc_rect, 0, -180 * 16)

    stem_top = s * 0.26 + arc_h / 2
    stem_bottom = s * 0.68
    painter.drawLine(QPointF(cx, stem_top), QPointF(cx, stem_bottom))

    base_w = s * 0.22
    painter.drawLine(QPointF(cx - base_w / 2, stem_bottom), QPointF(cx + base_w / 2, stem_bottom))

    if recording:
        wave_pen = QPen(QColor(255, 255, 255, 140), s * 0.02)
        wave_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(wave_pen)
        for i, offset in enumerate([s * 0.06, s * 0.12]):
            alpha = 140 - i * 50
            wave_pen.setColor(QColor(255, 255, 255, alpha))
            painter.setPen(wave_pen)
            painter.drawArc(
                QRectF(cx - mic_w / 2 - offset - s * 0.04, s * 0.22, s * 0.08, s * 0.2),
                45 * 16, 90 * 16
            )
            painter.drawArc(
                QRectF(cx + mic_w / 2 + offset - s * 0.04, s * 0.22, s * 0.08, s * 0.2),
                135 * 16, -90 * 16
            )

    painter.end()
    pixmap.setDevicePixelRatio(1.0)
    return pixmap


def _icon_pixel_sizes(size: int) -> list[int]:
    sizes = {16, 20, 24, 32, size}
    small = _windows_small_icon_px()
    if small is not None:
        sizes.add(small)
    return sorted(s for s in sizes if 16 <= s <= max(size, 16))


def create_microphone_icon(color: str | None = None, recording: bool = False, size: int = 64) -> QIcon:
    icon = QIcon()
    for px in _icon_pixel_sizes(size):
        icon.addPixmap(_microphone_pixmap(color, recording, px))
    return icon


class _TrayMicrophoneEngine(QIconEngine):
    """Hand Windows a shell-sized bitmap at 1x.

    QIcon.pixmap() otherwise multiplies by the screen scale. On a 200% display
    the tray asks for 32px and receives a 64px image at devicePixelRatio 2,
    and the notification area draws nothing.
    """

    def __init__(self, color: str | None = None, recording: bool = False):
        super().__init__()
        self._color = color
        self._recording = recording

    def clone(self):
        return _TrayMicrophoneEngine(self._color, self._recording)

    def availableSizes(self, mode, state):
        metric = _windows_small_icon_px() or 16
        return [QSize(side, side) for side in sorted({16, 32, metric})]

    def actualSize(self, size, mode, state):
        metric = _windows_small_icon_px()
        if metric is None:
            side = max(size.width(), size.height())
        else:
            side = metric
        return QSize(side, side)

    def pixmap(self, size, mode, state):
        side = max(16, int(min(size.width(), size.height()) or 16))
        return _microphone_pixmap(self._color, self._recording, side)

    def scaledPixmap(self, size, mode, state, scale):
        # Default engine does pixmap(size * scale), which reintroduces the 2x bitmap.
        del scale
        return self.pixmap(size, mode, state)

    def paint(self, painter, rect, mode, state):
        side = max(16, int(min(rect.width(), rect.height())))
        painter.drawPixmap(rect, self.pixmap(QSize(side, side), mode, state))


def _fresh_microphone_icon(kind: str) -> QIcon:
    """A new QIcon each call. Reusing one cache key makes Windows ignore NIM_MODIFY."""
    from sayink.ui import design_tokens as tok

    if kind == "recording":
        return QIcon(_TrayMicrophoneEngine(recording=True))
    if kind == "attention":
        return QIcon(_TrayMicrophoneEngine(color=tok.ATTENTION, recording=False))
    return QIcon(_TrayMicrophoneEngine())


def tray_menu_top_left(menu_size: QSize, anchor: QRect, available: QRect) -> QPoint:
    """Place a tray menu above the icon, or below it when the icon is at the top."""
    width = max(1, int(menu_size.width()))
    height = max(1, int(menu_size.height()))
    icon_top = int(anchor.y())
    icon_right = int(anchor.x() + max(1, anchor.width()))
    icon_bottom = int(anchor.y() + max(1, anchor.height()))

    x = icon_right - width
    y = icon_top - height
    if y < int(available.top()):
        y = icon_bottom

    left = int(available.left())
    top = int(available.top())
    right = left + int(available.width())
    bottom = top + int(available.height())
    if x + width > right:
        x = right - width
    if x < left:
        x = left
    if y + height > bottom:
        y = bottom - height
    if y < top:
        y = top
    return QPoint(x, y)


class _UpwardContextMenu(QMenu):
    """Tray menu that opens above the icon instead of dropping below the cursor."""

    def __init__(self, tray: QSystemTrayIcon):
        super().__init__()
        self._tray = tray

    def popup(self, pos, at=None):
        from PyQt6.QtWidgets import QApplication

        self.ensurePolished()
        self.adjustSize()
        size = self.sizeHint()
        anchor = self._tray.geometry()
        if anchor.isNull() or anchor.width() <= 0 or anchor.height() <= 0:
            anchor = QRect(int(pos.x()), int(pos.y()), 1, 1)
        screen = QApplication.screenAt(pos) or QApplication.primaryScreen()
        available = screen.availableGeometry() if screen is not None else QRect(0, 0, 1920, 1080)
        target = tray_menu_top_left(size, anchor, available)
        if at is None:
            super().popup(target)
        else:
            super().popup(target, at)


class TrayIcon(QSystemTrayIcon):
    open_settings = pyqtSignal()
    check_update_requested = pyqtSignal()
    wake_island = pyqtSignal()
    history_requested = pyqtSignal()
    quit_app = pyqtSignal()
    auto_start_toggled = pyqtSignal(bool)
    model_switched = pyqtSignal(str)
    menu_about_to_show = pyqtSignal()
    menu_closed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)

        self._icon_kind = "normal"
        self._rebuild_icons()
        self._apply_icon_kind("normal")
        self._idle_tooltip = "SayInk - 就绪"
        self._status_summary = "就绪"
        self.setToolTip(self._idle_tooltip)
        self._flash_timer = None
        self._flash_restore_recording = False

        self._model_menu = None
        self._model_group = None
        self._menu = None
        self._menu_is_open = False
        self._wake_after_menu = False
        self._notice_kind = ""
        self._setup_menu()
        self.activated.connect(self._on_activated)

    def _rebuild_icons(self) -> None:
        from sayink.ui import design_tokens as tok

        self._normal_icon = create_microphone_icon(recording=False)
        self._recording_icon = create_microphone_icon(recording=True)
        self._attention_icon = create_microphone_icon(color=tok.ATTENTION, recording=False)

    def _apply_icon_kind(self, kind: str) -> None:
        self._icon_kind = kind
        # A newly painted icon changes the cache key. Windows ignores
        # NIM_MODIFY when Qt repeats the QIcon it already failed to draw,
        # which leaves a blank slot after the tooltip already says 就绪.
        self.setIcon(_fresh_microphone_icon(kind))

    def show(self) -> None:
        super().show()
        self._schedule_icon_republish()

    def _schedule_icon_republish(self) -> None:
        if sys.platform != "win32":
            return
        from PyQt6.QtCore import QTimer

        timers: list[QTimer] = []
        for delay in (0, 400):
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self._republish_icon)
            timer.start(delay)
            timers.append(timer)
        self._icon_republish_timers = timers

    def _republish_icon(self) -> None:
        self._apply_icon_kind(self._icon_kind)

    def reapply_theme(self) -> None:
        _paint_menu(self._menu)
        _paint_menu(self._model_menu)
        kind = getattr(self, "_icon_kind", "normal")
        self._rebuild_icons()
        self._apply_icon_kind(kind)
        self._publish_window_icon()

    def _publish_window_icon(self) -> None:
        from PyQt6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is None:
            return
        icon = _fresh_microphone_icon("normal")
        app.setWindowIcon(icon)
        for widget in app.topLevelWidgets():
            try:
                widget.setWindowIcon(icon)
            except RuntimeError:
                continue

    def _setup_menu(self):
        menu = _UpwardContextMenu(self)
        self._menu = menu
        _paint_menu(menu)

        self._status_action = menu.addAction(self._status_summary)
        self._status_action.setEnabled(False)
        menu.addSeparator()

        settings_action = menu.addAction("打开 SayInk")
        settings_action.triggered.connect(self.open_settings.emit)

        update_action = menu.addAction("检查更新")
        update_action.triggered.connect(self.check_update_requested.emit)

        history_action = menu.addAction("历史")
        history_action.triggered.connect(self.history_requested.emit)

        self._model_menu = menu.addMenu("切换模型")
        _paint_menu(self._model_menu)
        empty = self._model_menu.addAction("加载中...")
        empty.setEnabled(False)

        self._auto_start_action = menu.addAction("开机自启")
        self._auto_start_action.setCheckable(True)
        self._auto_start_action.toggled.connect(self.auto_start_toggled.emit)

        menu.addSeparator()

        quit_action = menu.addAction("退出")
        quit_action.triggered.connect(self.quit_app.emit)

        menu.aboutToShow.connect(self._on_menu_about_to_show)
        menu.aboutToHide.connect(self._on_menu_about_to_hide)
        self.setContextMenu(menu)

    def show_update_notice(self, body: str) -> None:
        """Tray balloon for a new version. Other notices clear this kind."""
        super().showMessage(
            "SayInk",
            body,
            QSystemTrayIcon.MessageIcon.Information,
            8000,
        )
        self._notice_kind = "update"

    def showMessage(self, title, message, icon=QSystemTrayIcon.MessageIcon.Information, msecs=10000):
        self._notice_kind = ""
        super().showMessage(title, message, icon, msecs)

    @property
    def notice_kind(self) -> str:
        return self._notice_kind

    def update_models(self, downloaded_models: list[dict], active_id: str):
        if self._model_group is not None:
            try:
                self._model_group.triggered.disconnect()
            except (TypeError, RuntimeError):
                pass

        self._model_menu.clear()
        active_name = ""
        for model in downloaded_models:
            if model.get("id") == active_id:
                active_name = str(model.get("name") or "")
                break
        self._model_menu.setTitle(_model_switch_title(active_name))

        if not downloaded_models:
            empty = self._model_menu.addAction("暂无已下载模型")
            empty.setEnabled(False)
            self._model_group = None
            return

        self._model_group = QActionGroup(self._model_menu)
        self._model_group.setExclusive(True)

        for m in downloaded_models:
            action = self._model_menu.addAction(_model_choice_label(m))
            action.setCheckable(True)
            action.setChecked(m["id"] == active_id)
            action.setData(m["id"])
            self._model_group.addAction(action)

        self._model_group.triggered.connect(
            lambda a: self.model_switched.emit(a.data())
        )

    @pyqtSlot(QSystemTrayIcon.ActivationReason)
    def _on_activated(self, reason):
        # Windows emits Trigger on the first click of a double-click, then
        # DoubleClick. Most users single-click a tray icon, so respond to a
        # single click too — but defer it by the double-click interval so a
        # double-click still opens the window exactly once.
        if sys.platform == "win32":
            if reason == QSystemTrayIcon.ActivationReason.Context:
                # Right-click owns the modal menu. Don't also open the window
                # from the left click that arrived in the same gesture.
                self._disarm_single_click()
                return
            if reason == QSystemTrayIcon.ActivationReason.Trigger:
                self._arm_single_click()
            elif reason == QSystemTrayIcon.ActivationReason.DoubleClick:
                self._disarm_single_click()
                self._request_wake()
            return
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._request_wake()

    def _request_wake(self) -> None:
        """Open the main window after the tray menu releases the UI thread.

        A visible tray menu runs a modal loop on Windows. Showing or activating
        the main window inside that loop leaves the window under a spinning
        cursor until the menu closes.
        """
        if self._menu_is_open:
            self._wake_after_menu = True
            menu = self._menu
            if menu is not None and menu.isVisible():
                menu.close()
            return
        self.wake_island.emit()

    def _on_menu_about_to_show(self) -> None:
        self._menu_is_open = True
        self._disarm_single_click()
        self.menu_about_to_show.emit()

    def _on_menu_about_to_hide(self) -> None:
        self._menu_is_open = False
        self.menu_closed.emit()
        if not self._wake_after_menu:
            return
        self._wake_after_menu = False
        from PyQt6.QtCore import QTimer

        QTimer.singleShot(0, self.wake_island.emit)

    def _arm_single_click(self) -> None:
        from PyQt6.QtCore import QTimer
        from PyQt6.QtWidgets import QApplication

        timer = getattr(self, "_single_click_timer", None)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self._request_wake)
            self._single_click_timer = timer
        app = QApplication.instance()
        interval = app.doubleClickInterval() if app is not None else 400
        timer.start(max(150, int(interval)))

    def _disarm_single_click(self) -> None:
        timer = getattr(self, "_single_click_timer", None)
        if timer is not None:
            timer.stop()

    def set_recording(self, is_recording: bool):
        self._apply_icon_kind("recording" if is_recording else "normal")

    def flash_attention(self, duration_ms: int = 450) -> None:
        """Brief tray icon flash for soft feedback (e.g. short-tap during cooldown)."""
        from PyQt6.QtCore import QTimer

        if self._flash_timer is not None:
            self._flash_timer.stop()
            self._flash_timer.deleteLater()
        self._flash_restore_recording = self._icon_kind == "recording"
        self._apply_icon_kind("attention")
        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)

        def _restore():
            self._apply_icon_kind(
                "recording" if self._flash_restore_recording else "normal"
            )
            self._flash_timer = None

        self._flash_timer.timeout.connect(_restore)
        self._flash_timer.start(max(120, int(duration_ms)))

    def set_status_summary(self, text: str) -> None:
        self._status_summary = (text or "就绪").strip()
        self._status_action.setText(self._status_summary)
        self._idle_tooltip = f"SayInk - {self._status_summary}"
        self.setToolTip(self._idle_tooltip)
        # Tooltip updates succeed even when the shell never accepted the glyph.
        # Push the icon again once we know the tray slot exists.
        self._republish_icon()

    def set_activity_tooltip(self, state: str | None):
        """Brief tray hint for background work. None = idle."""
        if state is None:
            self.setToolTip(self._idle_tooltip)
            return
        lines = {
            "recording": "录音中",
            "recognizing": "正在识别",
            "polishing": "润色中",
            "listening": "正在听",
            "loading": "模型载入中",
        }
        label = lines.get(state, "")
        if label:
            self.setToolTip(f"SayInk - {label}")
        else:
            self.setToolTip(self._idle_tooltip)

    def set_auto_start(self, enabled: bool):
        self._auto_start_action.blockSignals(True)
        self._auto_start_action.setChecked(enabled)
        self._auto_start_action.blockSignals(False)

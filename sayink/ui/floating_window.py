from PyQt6.QtWidgets import (
    QWidget, QLabel, QVBoxLayout, QHBoxLayout, QPushButton, QMenu,
    QApplication, QSizePolicy,
)
from PyQt6.QtCore import Qt, QTimer, QPoint, QPointF, QRectF, QEvent, pyqtSignal
from PyQt6.QtGui import QFontMetrics, QPainter, QPainterPath, QPen, QPolygonF, QColor, QFont, QCursor
import math
import weakref
from sayink.runtime_status import MEETING_STATE_LABELS
from sayink.ui import design_tokens as tok
from sayink.ui.theme import prefers_reduced_motion
from sayink.ui.island_chrome import (
    clamp_top_left,
    coerce_saved_positions,
    is_screen_alive,
    island_window_flags,
    normalize_listen_bar_placement,
    placement_anchor,
    position_listen_bar,
    relative_listen_position,
    resolve_listen_placement,
    screen_storage_key,
)

# A few pixels separate a click (open the meeting) from a drag.
DRAG_THRESHOLD_PX = 6
# Long sessions tuck the live line away after this much silence.
LISTEN_BAR_IDLE_MS = 12_000

BAR_HEIGHT = 44
BAR_EXCERPT_HEIGHT = 68
# One width for both modes: the bar used to jump 360 ↔ 420 (and re-center)
# every time an excerpt appeared or cleared (U-12). Only the height grows.
MIN_BAR_WIDTH = 420
BAR_WIDTH = 480
BAR_EXCERPT_WIDTH = BAR_WIDTH
COMPACT_HEIGHT = BAR_HEIGHT
MODEL_LOADING_TITLE = "模型载入中 · 请勿录音"
_MEETING_SHORT = MEETING_STATE_LABELS["live_short"]
# call_when_painted() gives up waiting for a paint after this long (the bar
# may be off screen or unchanged, and then no paint arrives).
PAINT_WAIT_MAX_MS = 100


def _ui_font(family, pixels, weight=QFont.Weight.Normal):
    font = QFont(family)
    font.setPixelSize(pixels)
    font.setWeight(weight)
    return font


def _error_headline(first_line: str) -> tuple[str, str]:
    """Split an error title at the first sentence mark, not at a fixed width."""
    best = None
    for mark in ("：", "。", "；", "，", "！", "？"):
        index = first_line.find(mark)
        if index > 0 and (best is None or index < best[0]):
            best = (index, mark)
    if best is None:
        return first_line.strip(), ""
    index, mark = best
    return first_line[:index].strip(), first_line[index + len(mark):].strip()


_STATE_GLYPH_MAP = {
    "STATE_RECORD": "record",
    "STATE_LISTEN": "listen",
    "STATE_RECOGNIZE": "recognize",
    "STATE_POLISH": "polish",
    "STATE_SUCCESS": "success",
    "STATE_WARN": "warn",
    "STATE_ERROR": "error",
    "STATE_MUTED": "muted",
}


_DIAMOND_POLY = QPolygonF([
    QPointF(8.0, 2.0),
    QPointF(13.5, 8.0),
    QPointF(8.0, 14.0),
    QPointF(2.5, 8.0),
])
_WARN_TRI_POLY = QPolygonF([
    QPointF(8.0, 2.3),
    QPointF(13.8, 13.0),
    QPointF(2.2, 13.0),
])


def _build_check_path() -> QPainterPath:
    path = QPainterPath()
    path.moveTo(5.0, 8.2)
    path.lineTo(7.1, 10.4)
    path.lineTo(11.1, 5.8)
    return path


_CHECK_PATH = _build_check_path()


class _DotIndicator(QWidget):
    """Dual-channel status glyph (color + distinct geometric form) for the island."""

    def __init__(self, color: str = "", parent=None):
        super().__init__(parent)
        # Resolve at construction time so the live theme axis wins over the
        # value that happened to be active when this module was imported.
        self._color = QColor(color or tok.STATE_RECORD)
        self._state_token = "STATE_RECORD"
        self._pulse = 0.0
        self._pulsing = False
        self._suspended = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self.setFixedSize(16, 16)

    def set_color(self, color: str):
        self._color = QColor(color)
        self.update()

    def set_state_token(self, token: str) -> None:
        self._state_token = token or "STATE_RECORD"
        self.update()

    def state_glyph_kind(self) -> str:
        return _STATE_GLYPH_MAP.get(self._state_token, "record")

    def start_pulse(self):
        self._pulsing = True
        if prefers_reduced_motion():
            self._timer.stop()
            self._pulse = 0.0
            self.update()
            return
        if self._suspended or self._timer.isActive():
            return
        self._pulse = 0.0
        self._timer.start(50)

    def stop_pulse(self):
        self._pulsing = False
        self._timer.stop()
        self._pulse = 0.0
        self.update()

    def set_suspended(self, suspended: bool) -> None:
        self._suspended = bool(suspended)
        if self._suspended or prefers_reduced_motion():
            self._timer.stop()
        elif self._pulsing and not self._timer.isActive():
            self._timer.start(50)

    def _tick(self):
        if prefers_reduced_motion():
            self._timer.stop()
            self.update()
            return
        self._pulse += 0.15
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)

        if self._timer.isActive():
            alpha = int(40 + 40 * abs(math.sin(self._pulse)))
            outer = QColor(self._color)
            outer.setAlpha(alpha)
            p.setBrush(outer)
            p.drawEllipse(0, 0, 16, 16)

        kind = self.state_glyph_kind()
        stroke = QPen(self._color)
        stroke.setWidthF(1.8)
        stroke.setCapStyle(Qt.PenCapStyle.RoundCap)
        stroke.setJoinStyle(Qt.PenJoinStyle.RoundJoin)

        if kind == "record":
            p.setPen(stroke)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QRectF(2.0, 2.0, 12.0, 12.0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(self._color)
            p.drawEllipse(QRectF(5.2, 5.2, 5.6, 5.6))
        elif kind == "listen":
            p.setPen(stroke)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QRectF(2.0, 2.0, 12.0, 12.0))
            p.drawLine(QPointF(6.3, 5.6), QPointF(6.3, 10.4))
            p.drawLine(QPointF(9.7, 5.6), QPointF(9.7, 10.4))
        elif kind == "recognize":
            p.setPen(stroke)
            p.setBrush(Qt.BrushStyle.NoBrush)
            start_angle_deg = (
                (45.0 - math.degrees(self._pulse) * 2.5) % 360.0
                if self._timer.isActive()
                else 45.0
            )
            p.drawArc(QRectF(2.2, 2.2, 11.6, 11.6), int(round(start_angle_deg * 16)), 270 * 16)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(self._color)
            p.drawEllipse(QRectF(6.5, 6.5, 3.0, 3.0))
        elif kind == "polish":
            p.setPen(stroke)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPolygon(_DIAMOND_POLY)
        elif kind == "success":
            p.setPen(stroke)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QRectF(2.0, 2.0, 12.0, 12.0))
            p.drawPath(_CHECK_PATH)
        elif kind == "warn":
            p.setPen(stroke)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPolygon(_WARN_TRI_POLY)
            p.drawLine(QPointF(8.0, 6.4), QPointF(8.0, 9.4))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(self._color)
            p.drawEllipse(QRectF(7.2, 10.6, 1.6, 1.6))
        elif kind == "error":
            p.setPen(stroke)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QRectF(2.0, 2.0, 12.0, 12.0))
            p.drawLine(QPointF(5.5, 5.5), QPointF(10.5, 10.5))
            p.drawLine(QPointF(10.5, 5.5), QPointF(5.5, 10.5))
        else:
            muted_pen = QPen(self._color)
            muted_pen.setWidthF(1.6)
            p.setPen(muted_pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QRectF(2.5, 2.5, 11.0, 11.0))
        p.end()


class WaveformWidget(QWidget):

    NUM_BARS = 14
    _SILENT_VOLUME_EPS = 0.005
    _SILENT_BAR_EPS = 0.01

    def __init__(self, parent=None):
        super().__init__(parent)
        self._volume = 0.0
        self._bar_heights = [0.0] * self.NUM_BARS
        self._phase = 0.0
        self._accent = QColor(tok.FLOAT_TEXT)
        self._visible_bars = self.NUM_BARS
        self._running = False
        self._suspended = False
        self.setFixedHeight(18)
        self.setFixedWidth(72)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._animate)

    def set_accent(self, color: str):
        self._accent = QColor(color)

    def start(self):
        self._running = True
        if prefers_reduced_motion():
            self._timer.stop()
            self.update()
            return
        if self._suspended or self._timer.isActive():
            return
        self._phase = 0.0
        self._timer.start(50)

    def stop(self):
        self._running = False
        self._timer.stop()
        self._bar_heights = [0.0] * self.NUM_BARS
        self.update()

    def set_suspended(self, suspended: bool) -> None:
        self._suspended = bool(suspended)
        if self._suspended or prefers_reduced_motion():
            self._timer.stop()
        elif self._running and not self._timer.isActive():
            self._timer.start(50)

    def set_compact(self, compact: bool) -> None:
        self._visible_bars = 5 if compact else self.NUM_BARS
        self.setFixedWidth(28 if compact else 72)

    def set_volume(self, volume: float):
        self._volume = min(max(0.0, float(volume)) * 8, 1.0)
        if prefers_reduced_motion():
            self._timer.stop()
            level = self._volume if self._running else 0.0
            self._bar_heights = [level * 0.7] * self.NUM_BARS
            self.update()
            return
        if self._running and not self._suspended and self._volume >= self._SILENT_VOLUME_EPS and not self._timer.isActive():
            self._timer.start(50)

    def _animate(self):
        if prefers_reduced_motion():
            self._timer.stop()
            self.update()
            return
        self._phase += 0.3
        peak = 0.0
        for i in range(self.NUM_BARS):
            wave = math.sin(self._phase + i * 0.5) * 0.5 + 0.5
            target = wave * self._volume
            height = self._bar_heights[i] + (target - self._bar_heights[i]) * 0.4
            self._bar_heights[i] = height
            if height > peak:
                peak = height
        if self._volume < self._SILENT_VOLUME_EPS and peak < self._SILENT_BAR_EPS:
            self._bar_heights = [0.0] * self.NUM_BARS
            self._timer.stop()
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()
        gap = 2
        count = self._visible_bars
        bar_width = max(2, (w - (count - 1) * gap) // count)
        total_width = count * bar_width + (count - 1) * gap
        x_offset = (w - total_width) // 2

        for i in range(count):
            bar_h = max(3, int(self._bar_heights[i] * (h - 2)))
            x = x_offset + i * (bar_width + gap)
            y = (h - bar_h) // 2

            fill = QColor(self._accent)
            fill.setAlpha(int(100 + self._bar_heights[i] * 155))
            painter.setBrush(fill)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(x, y, bar_width, bar_h, 1, 1)

        painter.end()


class FloatingWindow(QWidget):

    continuous_stop_requested = pyqtSignal()
    cancel_requested = pyqtSignal()
    settings_requested = pyqtSignal()
    history_requested = pyqtSignal()
    # Meeting state: 「标记」 button and a click on the bar body.
    meeting_mark_requested = pyqtSignal()
    meeting_open_requested = pyqtSignal()
    # Drag committed a screen-relative spot. Payload: screen, rel_x, rel_y, anchor.
    position_committed = pyqtSignal(dict)
    # Context menu asked for the automatic spot again.
    placement_reset = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._listening_active = False
        self._listen_target = ""
        self._esc_stops_continuous = True
        self._meeting_active = False
        self._meeting_elapsed = "00:00:00"
        self._meeting_paused_detail = ""
        self._meeting_source = ""
        self._capture_active = False
        # Set by end_capture() once the hold key is up; cleared by show_recording().
        self._capture_released = False
        self._model_loading_active = False
        self._live_text = ""
        self._full_status_text = "准备中..."
        self._pre_collapse_tooltip = ""
        self._active_context_menu: QMenu | None = None
        self._mode = "compact"
        self._setup_window()
        self._setup_ui()
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._on_hide_timer_timeout)
        self._transient_restore_timer = QTimer(self)
        self._transient_restore_timer.setSingleShot(True)
        self._transient_restore_timer.timeout.connect(self._restore_after_transient_meeting_notice)
        self._after_paint: list = []
        self._after_paint_timer = QTimer(self)
        self._after_paint_timer.setSingleShot(True)
        self._after_paint_timer.timeout.connect(self._run_after_paint)
        self._placement_pref = "auto"
        self._saved_positions: dict = {}
        self._position_pinned = False
        self._pin_screen = None
        self._pin_anchor = "top"
        self._pin_x = 0
        self._pin_edge = 0
        self._pinned_long = False
        self._pinned_token = ""
        self._user_moved = False
        self._drag_tracking = False
        self._moved_past_threshold = False
        self._suppress_click = False
        self._press_global = QPoint()
        self._press_origin = QPoint()
        self._autohide_enabled = True
        self._autohide_idle_ms = LISTEN_BAR_IDLE_MS
        self._idle_collapsed = False
        self._idle_timer = QTimer(self)
        self._idle_timer.setSingleShot(True)
        self._idle_timer.timeout.connect(self._collapse_idle_excerpt)
        self._meeting_click_timer = QTimer(self)
        self._meeting_click_timer.setSingleShot(True)
        self._meeting_click_timer.timeout.connect(self._open_meeting_from_click)
        self._screen_watchers: list[tuple[object, str, object]] = []
        self._connect_screen_watchers()

    def _connect_screen_watchers(self) -> None:
        app = QApplication.instance()
        if app is None:
            return
        weak_self = weakref.ref(self)

        def _on_removed(removed=None) -> None:
            w = weak_self()
            if w is not None:
                w._on_screen_removed(removed)

        def _on_metrics(*_args) -> None:
            w = weak_self()
            if w is not None:
                w._on_screen_metrics_changed()

        def _on_added(scr) -> None:
            w = weak_self()
            if w is not None:
                w._watch_screen_signals(scr)
                w._on_screen_metrics_changed()

        self._screen_metric_handler = _on_metrics
        for sig_name, handler in (
            ("screenRemoved", _on_removed),
            ("screenAdded", _on_added),
            ("primaryScreenChanged", _on_metrics),
        ):
            sig = getattr(app, sig_name, None)
            if sig is not None:
                sig.connect(handler)
                self._screen_watchers.append((app, sig_name, handler))
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
        self._sync_text_line_height()
        if not self.isVisible():
            return
        if self._position_pinned:
            self._hold_pinned_edges()
        else:
            self._place_initial(self._is_long_session())

    def _on_hide_timer_timeout(self) -> None:
        if self._is_long_session() or self._hold_capture():
            self._note_bar_activity()
            return
        self.hide()

    def call_when_painted(self, fn) -> None:
        """Run ``fn`` once the bar's current state is on screen.

        A translucent window reappears with the frame it last drew (「已发送」)
        until Qt repaints it, which needs the event loop; work that blocks the
        UI thread (opening the audio device ≈ 0.5 s) must wait for that paint.
        """
        self._after_paint.append(fn)
        self._after_paint_timer.start(PAINT_WAIT_MAX_MS)

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._after_paint and not getattr(self, "_after_paint_queued", False):
            self._after_paint_queued = True
            # The frame is flushed after paintEvent returns.
            QTimer.singleShot(0, self._run_after_paint)

    def _run_after_paint(self) -> None:
        from PyQt6 import sip

        if sip.isdeleted(self):
            return
        self._after_paint_queued = False
        self._after_paint_timer.stop()
        pending, self._after_paint = self._after_paint, []
        for fn in pending:
            fn()

    def set_input_source(self, source: str) -> None:
        self._source = source or "microphone"

    def set_esc_stops_continuous(self, enabled: bool) -> None:
        self._esc_stops_continuous = bool(enabled)
        if self._meeting_active:
            return
        self._end_btn.setAccessibleName("结束持续转写并保留已录内容")
        shortcut = " · Esc" if enabled else ""
        self._end_btn.setToolTip(f"结束录音，已录内容继续识别{shortcut}")

    @staticmethod
    def _set_btn_css(btn: QPushButton, css: str) -> None:
        if btn.styleSheet() != css:
            btn.setStyleSheet(css)

    def _apply_action_button_styles(self) -> None:
        from sayink.ui import design_tokens as tok

        self._set_btn_css(self._cancel_btn, f"""
            QPushButton {{
                background: {tok.CHIP_BG};
                color: {tok.FLOAT_TEXT_SEC};
                border: 1px solid {tok.FLOAT_BORDER};
                font-size: {tok.TYPE_CAPTION}px;
                font-weight: 700;
                border-radius: 14px;
                padding: 0px 10px;
            }}
            QPushButton:hover {{
                background: {tok.ROW_HOVER};
                color: {tok.FLOAT_TEXT};
            }}
        """)
        if self._meeting_active:
            rec_color = QColor(tok.STATE_RECORD)
            end_fg = "#0D0D0D" if rec_color.lightness() > 145 else "#FFFFFF"
            end_hover = rec_color.lighter(108).name() if rec_color.lightness() > 145 else rec_color.darker(112).name()
            self._set_btn_css(self._mark_btn, f"""
                QPushButton {{
                    background: {tok.CHIP_BG};
                    color: {tok.FLOAT_TEXT};
                    border: 1px solid {tok.FLOAT_BORDER};
                    font-size: {tok.TYPE_CAPTION}px;
                    font-weight: 700;
                    border-radius: 15px;
                    padding: 0px 12px;
                }}
                QPushButton:hover {{
                    background: {tok.ROW_HOVER};
                    color: {tok.FLOAT_TEXT};
                    border-color: {tok.FLOAT_TEXT_SEC};
                }}
            """)
            self._set_btn_css(self._end_btn, f"""
                QPushButton {{
                    background: {tok.STATE_RECORD};
                    color: {end_fg};
                    border: none;
                    font-size: {tok.TYPE_CAPTION}px;
                    font-weight: 700;
                    border-radius: 15px;
                    padding: 0px 12px;
                }}
                QPushButton:hover {{
                    background: {end_hover};
                    color: {end_fg};
                }}
            """)
        else:
            end_css = f"""
                QPushButton {{
                    background: {tok.PRIMARY_CONTAINER};
                    color: {tok.PRIMARY_ON};
                    border: none;
                    font-size: {tok.TYPE_CAPTION}px;
                    font-weight: 700;
                    border-radius: 15px;
                    padding: 0px 12px;
                }}
                QPushButton:hover {{
                    background: {tok.PRIMARY_CONTAINER_HOVER};
                    color: {tok.PRIMARY_ON};
                }}
                QPushButton:pressed {{
                    background: {tok.PRIMARY_CONTAINER_PRESSED};
                }}
            """
            self._set_btn_css(self._end_btn, end_css)
            self._set_btn_css(self._mark_btn, end_css)

    @staticmethod
    def _set_widget_css(widget: QWidget, css: str) -> None:
        if widget.styleSheet() != css:
            widget.setStyleSheet(css)

    def reapply_theme(self) -> None:
        from sayink.ui import design_tokens as tok

        self._paint_container()
        self._status_label.setFont(
            _ui_font(tok.UI_FONT_FAMILY, tok.TYPE_BODY_SM, QFont.Weight.Bold)
        )
        self._set_widget_css(
            self._status_label,
            f"color: {tok.FLOAT_TEXT}; background: transparent;"
            f" font-family: {tok.FONT_DISPLAY};",
        )
        self._apply_action_button_styles()
        self._text_label.setFont(_ui_font(tok.UI_FONT_FAMILY, tok.TYPE_BODY_SM))
        self._set_widget_css(
            self._text_label,
            f"color: {tok.FLOAT_TEXT}; background: transparent;"
            f" font-family: {tok.FONT};",
        )
        self._sync_text_line_height()
        if hasattr(self, "_state_token"):
            self._set_state(self._full_status_text or self._status_label.text(), self._state_token,
                            pulse=self._dot._pulsing)

    def _paint_container(self) -> None:
        radius = 16 if self._mode == "excerpt" else BAR_HEIGHT // 2
        self._set_widget_css(
            self._container,
            f"QWidget#islandContainer {{ background: {tok.FLOAT_BG};"
            f" border: 1px solid {tok.FLOAT_BORDER}; border-radius: {radius}px; }}",
        )

    def _setup_window(self):
        self.setWindowFlags(island_window_flags() | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setMinimumSize(MIN_BAR_WIDTH, BAR_HEIGHT)
        self.resize(BAR_WIDTH, BAR_HEIGHT)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._container = QWidget()
        self._container.setObjectName("islandContainer")

        container_layout = QVBoxLayout(self._container)
        container_layout.setContentsMargins(10, 6, 8, 6)
        container_layout.setSpacing(2)

        header_row = QHBoxLayout()
        header_row.setSpacing(8)
        header_row.setContentsMargins(0, 0, 0, 0)

        self._dot = _DotIndicator()
        header_row.addWidget(self._dot)

        self._waveform = WaveformWidget()
        header_row.addWidget(self._waveform)

        from sayink.ui import design_tokens as tok

        self._status_label = QLabel("准备中...")
        self._status_label.setFont(
            _ui_font(tok.UI_FONT_FAMILY, tok.TYPE_BODY_SM, QFont.Weight.Bold)
        )
        self._status_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self._status_label.setMinimumWidth(40)
        header_row.addWidget(self._status_label, 1)
        self._source = "microphone"

        actions_row = QHBoxLayout()
        actions_row.setContentsMargins(2, 0, 0, 0)
        actions_row.setSpacing(14)

        self._cancel_btn = QPushButton("取消")
        self._cancel_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._cancel_btn.setFixedHeight(28)
        self._cancel_btn.setMinimumWidth(46)
        self._cancel_btn.setAccessibleName("取消本次转写")
        self._cancel_btn.setToolTip("取消本次转写 · Esc")
        self._cancel_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._cancel_btn.clicked.connect(self._on_close_clicked)
        self._cancel_btn.hide()

        self._end_btn = QPushButton("结束")
        self._end_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._end_btn.setFixedHeight(30)
        self._end_btn.setMinimumWidth(50)
        self._end_btn.setAccessibleName("结束持续转写并保留已录内容")
        self._end_btn.setToolTip("结束录音，已录内容继续识别 · Esc")
        self._end_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._end_btn.clicked.connect(self._on_end_clicked)
        self._mark_btn = QPushButton("标记")
        self._mark_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._mark_btn.setFixedHeight(30)
        self._mark_btn.setMinimumWidth(46)
        self._mark_btn.setAccessibleName("标记会议时刻")
        self._mark_btn.setToolTip("记下当前时刻，纪要会重点覆盖")
        self._mark_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._mark_btn.clicked.connect(self.meeting_mark_requested.emit)
        self._mark_btn.hide()
        actions_row.addWidget(self._cancel_btn)
        actions_row.addWidget(self._mark_btn)
        actions_row.addWidget(self._end_btn)
        header_row.addLayout(actions_row)
        container_layout.addLayout(header_row)

        self._text_label = QLabel("")
        self._text_label.setFont(_ui_font(tok.UI_FONT_FAMILY, tok.TYPE_BODY_SM))
        self._text_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._text_label.setWordWrap(False)
        self._sync_text_line_height()
        self._text_label.hide()
        container_layout.addWidget(self._text_label)

        layout.addWidget(self._container)
        self.reapply_theme()
        self.set_input_source("microphone")
        self._set_mode("compact")
        self._use_drag_cursor()
        self._install_position_menu()

    def _sync_text_line_height(self) -> int:
        line_h = max(18, QFontMetrics(self._text_label.font()).height())
        self._text_label.setMaximumHeight(line_h)
        return line_h

    def _available_status_width(self) -> int:
        used = 10 + 8 + self._dot.width() + 8
        if not self._waveform.isHidden():
            used += self._waveform.width() + 8
        visible_btns = [
            btn for btn in (self._cancel_btn, self._mark_btn, self._end_btn)
            if not btn.isHidden()
        ]
        if visible_btns:
            used += 8 + 2 + sum(
                max(btn.minimumWidth(), QFontMetrics(btn.font()).horizontalAdvance(btn.text()) + 20)
                for btn in visible_btns
            )
            used += 14 * max(0, len(visible_btns) - 1)
        return max(88, BAR_WIDTH - used)

    def _update_status_elision(self) -> None:
        raw = self._full_status_text or ""
        if not raw:
            self._status_label.setText("")
            return
        metrics = QFontMetrics(self._status_label.font())
        avail = self._available_status_width()
        if metrics.horizontalAdvance(raw) <= avail:
            self._status_label.setText(raw)
        else:
            self._status_label.setText(
                metrics.elidedText(raw, Qt.TextElideMode.ElideRight, avail)
            )
            if raw not in (self._status_label.toolTip() or ""):
                tip = self._status_label.toolTip().strip()
                self._status_label.setToolTip(f"{raw} · {tip}" if tip else raw)

    def _sync_cancel_button(self) -> None:
        if self._meeting_active or self._model_loading_active:
            self._cancel_btn.hide()
            return
        if self._capture_active and not self._listening_active:
            self._cancel_btn.setText("取消")
            self._cancel_btn.setAccessibleName("取消本次转写")
            self._cancel_btn.setToolTip("取消本次转写 · Esc")
            self._cancel_btn.show()
            return
        if self._listening_active and bool(self._live_text):
            self._cancel_btn.setText("放弃本句")
            self._cancel_btn.setAccessibleName("放弃当前句")
            self._cancel_btn.setToolTip("放弃当前正在说的这一句，继续监听")
            self._cancel_btn.show()
            return
        self._cancel_btn.hide()

    def _set_mode(self, mode: str) -> None:
        self._mode = mode
        self._paint_container()
        line_h = self._sync_text_line_height()
        extra_h = max(0, line_h - 18)
        excerpt = mode == "excerpt"
        if excerpt:
            target_h = BAR_EXCERPT_HEIGHT + extra_h
            self.setMinimumWidth(MIN_BAR_WIDTH)
            self.setFixedHeight(target_h)
            self.resize(BAR_EXCERPT_WIDTH, target_h)
        else:
            target_h = BAR_HEIGHT + extra_h
            self.setMinimumWidth(MIN_BAR_WIDTH)
            self.setFixedHeight(target_h)
            self.resize(BAR_WIDTH, target_h)

    def _on_close_clicked(self) -> None:
        if self._capture_active and not self._listening_active:
            self.cancel_requested.emit()
        elif self._listening_active and not self._meeting_active and self._live_text:
            self.cancel_requested.emit()
        elif self._listening_active:
            self.continuous_stop_requested.emit()
        else:
            self.dismiss_if_idle()

    def _on_end_clicked(self) -> None:
        self.continuous_stop_requested.emit()

    def set_placement_preference(self, placement: str) -> None:
        self._placement_pref = normalize_listen_bar_placement(placement)

    def set_saved_positions(self, positions) -> None:
        self._saved_positions = coerce_saved_positions(positions)

    def set_autohide(self, enabled: bool) -> None:
        self._autohide_enabled = bool(enabled)
        if not self._autohide_enabled:
            self._idle_timer.stop()
            self._restore_idle_excerpt()

    def set_autohide_idle_ms(self, milliseconds: int) -> None:
        self._autohide_idle_ms = max(1, int(milliseconds))

    def reposition_for_preference(self) -> None:
        """Settings changed the preset: drop the in-session pin and place again."""
        self._user_moved = False
        self._position_pinned = False
        self._pinned_token = ""
        if self.isVisible():
            self._place_initial(self._is_long_session())

    def _is_long_session(self) -> bool:
        return bool(self._listening_active or self._meeting_active)

    def _screen_for_placement(self):
        if self._position_pinned and self._pin_screen is not None:
            if is_screen_alive(self._pin_screen):
                return self._pin_screen
            self._pin_screen = None
            self._position_pinned = False
            self._pinned_token = ""
            self._user_moved = False
        cursor_screen = QApplication.screenAt(QCursor.pos())
        if is_screen_alive(cursor_screen):
            return cursor_screen
        primary = QApplication.primaryScreen()
        return primary if is_screen_alive(primary) else None

    def _on_screen_removed(self, removed_screen=None) -> None:
        if self._pin_screen is not None and (
            removed_screen is self._pin_screen or not is_screen_alive(self._pin_screen)
        ):
            self._pin_screen = None
            self._position_pinned = False
            self._pinned_token = ""
            self._user_moved = False
            if self.isVisible():
                self._place_initial(self._is_long_session())

    def _token_for(self, long_session: bool, screen) -> tuple[str, dict | None]:
        pref = normalize_listen_bar_placement(self._placement_pref)
        if pref == "last" and screen is not None:
            saved = self._saved_positions.get(screen_storage_key(screen))
            if isinstance(saved, dict):
                return "saved", saved
        return resolve_listen_placement(pref, long_session=long_session), None

    def _ensure_position(self) -> None:
        long_session = self._is_long_session()
        screen = self._screen_for_placement()
        token, saved = self._token_for(long_session, screen)
        entering_long = long_session and not self._pinned_long
        if self._position_pinned and (
            self._user_moved or not (entering_long and token != self._pinned_token)
        ):
            self._pinned_long = long_session
            self._hold_pinned_edges()
            return
        self._place_initial(long_session, token, saved, screen)

    def _place_initial(self, long_session: bool, token=None, saved=None, screen=None) -> None:
        screen = screen or self._screen_for_placement()
        if screen is None:
            return
        if token is None:
            token, saved = self._token_for(long_session, screen)
        if token == "saved" and saved:
            position_listen_bar(self, width=self.width(), saved=saved, screen=screen)
            anchor = saved.get("anchor")
            self._pin_anchor = anchor if anchor in ("top", "bottom") else "top"
        else:
            placement = token if token in ("top_center", "bottom_center", "bottom_right") else (
                resolve_listen_placement(self._placement_pref, long_session=long_session)
            )
            position_listen_bar(self, width=self.width(), placement=placement, screen=screen)
            self._pin_anchor = placement_anchor(placement)
            token = placement
        self._pin_screen = screen
        self._position_pinned = True
        self._pinned_long = long_session
        self._pinned_token = token
        self._user_moved = False
        self._capture_pin_from_widget()

    def _capture_pin_from_widget(self) -> None:
        self._pin_x = self.x()
        if self._pin_anchor == "bottom":
            self._pin_edge = self.y() + self.height()
        else:
            self._pin_edge = self.y()

    def _hold_pinned_edges(self) -> None:
        if self._pin_screen is not None and not is_screen_alive(self._pin_screen):
            self._pin_screen = None
            self._position_pinned = False
            self._pinned_token = ""
            self._user_moved = False
            self._place_initial(self._is_long_session())
            return
        screen = self._pin_screen if is_screen_alive(self._pin_screen) else QApplication.primaryScreen()
        if not is_screen_alive(screen):
            return
        try:
            geo = screen.availableGeometry()
        except RuntimeError:
            self._pin_screen = None
            self._position_pinned = False
            return
        width = max(1, self.width())
        height = max(1, self.height())
        y = self._pin_edge - height if self._pin_anchor == "bottom" else self._pin_edge
        x, y = clamp_top_left(self._pin_x, y, width, height, geo)
        self._pin_x = x
        self._pin_edge = y + height if self._pin_anchor == "bottom" else y
        if self.x() != x or self.y() != y:
            self.move(x, y)

    def _present(self) -> None:
        self._hide_timer.stop()
        self._end_btn.setVisible(self._listening_active)
        self._mark_btn.setVisible(self._meeting_active)
        self._sync_cancel_button()
        self._apply_action_button_styles()
        self._update_status_elision()
        self._dot.set_suspended(False)
        self._waveform.set_suspended(False)
        self._ensure_position()
        self.show()
        self._note_bar_activity()

    def _note_bar_activity(self) -> None:
        if not self._autohide_enabled or not self._is_long_session() or not self.isVisible():
            self._idle_timer.stop()
            return
        self._idle_timer.start(self._autohide_idle_ms)

    def _collapse_idle_excerpt(self) -> None:
        if not self._autohide_enabled or not self._is_long_session():
            return
        if self.underMouse():
            self._note_bar_activity()
            return
        if self._mode != "excerpt" or not self._text_label.isVisible():
            return
        self._idle_collapsed = True
        self._pre_collapse_tooltip = self.toolTip()
        self.setToolTip("字幕已自动收起，鼠标移入展开")
        if " · 悬停展开" not in self._full_status_text:
            self._full_status_text = f"{self._full_status_text} · 悬停展开"
            self._update_status_elision()
        self._text_label.hide()
        self._set_mode("compact")
        self._hold_pinned_edges()

    def _restore_idle_excerpt(self) -> None:
        if not self._idle_collapsed:
            return
        self._idle_collapsed = False
        if self._full_status_text.endswith(" · 悬停展开"):
            self._full_status_text = self._full_status_text[: -len(" · 悬停展开")]
            self._update_status_elision()
        self.setToolTip(self._pre_collapse_tooltip)
        if self._text_label.text():
            self._text_label.show()
            self._set_mode("excerpt")
            self._hold_pinned_edges()

    def _use_drag_cursor(self) -> None:
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def _remember_move_hint(self) -> None:
        hint = "拖动可挪开，右键可打开菜单或重置位置。"
        current = self._status_label.toolTip()
        if hint in current:
            return
        self._status_label.setToolTip(f"{current} {hint}".strip() if current else hint)

    def _install_position_menu(self) -> None:
        for widget in (self._container, *self._container.findChildren(QWidget)):
            widget.installEventFilter(self)

    def _position_menu(self) -> QMenu:
        from sayink.ui.tray_icon import _paint_menu

        menu = QMenu(self)
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        menu.setWindowFlags(
            menu.windowFlags()
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.NoDropShadowWindowHint
        )
        menu.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        menu.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        _paint_menu(menu)
        hist_action = menu.addAction("打开历史记录")
        hist_action.triggered.connect(self.history_requested.emit)
        settings_action = menu.addAction("打开设置")
        settings_action.triggered.connect(self.settings_requested.emit)
        menu.addSeparator()
        reset_action = menu.addAction("重置悬浮窗位置")
        reset_action.triggered.connect(self.reset_to_default_position)
        return menu

    def _show_position_menu(self, global_pos: QPoint) -> None:
        prev = getattr(self, "_active_context_menu", None)
        if prev is not None:
            try:
                prev.close()
                prev.deleteLater()
            except RuntimeError:
                pass
        menu = self._position_menu()
        self._active_context_menu = menu
        menu.destroyed.connect(
            lambda *_: setattr(self, "_active_context_menu", None)
            if getattr(self, "_active_context_menu", None) is menu
            else None
        )
        menu.popup(global_pos)

    def reset_to_default_position(self) -> None:
        self._placement_pref = "auto"
        self._user_moved = False
        self._position_pinned = False
        self._pinned_token = ""
        self.placement_reset.emit()
        if self.isVisible():
            self._place_initial(self._is_long_session())

    def _commit_drag(self) -> None:
        center = self.frameGeometry().center()
        candidate = QApplication.screenAt(center)
        if not is_screen_alive(candidate):
            candidate = self._pin_screen if is_screen_alive(self._pin_screen) else QApplication.primaryScreen()
        screen = candidate
        if not is_screen_alive(screen):
            return
        try:
            geo = screen.availableGeometry()
        except RuntimeError:
            return
        self._pin_anchor = "bottom" if center.y() >= geo.y() + geo.height() / 2 else "top"
        x, y = clamp_top_left(self.x(), self.y(), self.width(), self.height(), geo)
        self.move(x, y)
        self._pin_screen = screen
        self._position_pinned = True
        self._user_moved = True
        self._pinned_long = self._is_long_session()
        self._pinned_token = "saved"
        self._capture_pin_from_widget()
        saved = relative_listen_position(
            geo, self.x(), self.y(), self.width(), self.height(), self._pin_anchor
        )
        key = screen_storage_key(screen)
        self._saved_positions[key] = saved
        self._placement_pref = "last"
        self.position_committed.emit({"screen": key, **saved})

    def _open_meeting_from_click(self) -> None:
        if self._meeting_active:
            self.meeting_open_requested.emit()

    # ── Meeting state ─────────────────────────────────────

    @property
    def meeting_active(self) -> bool:
        return self._meeting_active

    def set_meeting_source(self, label: str) -> None:
        """Audio source of the meeting about to start; cleared when it ends."""
        self._meeting_source = label

    def show_meeting(self, elapsed: str | None = None, detail: str = "") -> None:
        """Red-dot meeting bar: 「会议中 · hh:mm:ss」 + 标记 + 结束."""
        if elapsed is not None:
            self._meeting_elapsed = elapsed
        self._transient_restore_timer.stop()
        self._meeting_active = True
        self._meeting_paused_detail = ""
        self._model_loading_active = False
        self._listening_active = True
        self._capture_active = False
        self._capture_released = False
        self._end_btn.setAccessibleName("结束会议")
        self._end_btn.setToolTip("结束会议并整理文字稿")
        self._set_state(f"{_MEETING_SHORT} · {self._meeting_elapsed}", "STATE_RECORD", pulse=True)
        self._remember_move_hint()
        self._use_drag_cursor()
        self._waveform.set_compact(True)
        self._waveform.show()
        self._waveform.start()
        if detail:
            self._apply_excerpt(detail)
        elif self._live_text:
            self._apply_excerpt(self._live_text, keep_tail=True)
        elif self._meeting_source:
            self._apply_excerpt(f"音频来源：{self._meeting_source} · 点此打开会议页")
        else:
            self._apply_excerpt("点此打开会议页 · 停顿后出字")
        self._present()

    def update_meeting_clock(self, elapsed: str) -> None:
        self._meeting_elapsed = elapsed
        if not self._meeting_active:
            return
        suffix = " · 悬停展开" if self._idle_collapsed else ""
        base = self._full_status_text
        if suffix and base.endswith(suffix):
            base = base[: -len(suffix)]
        _prefix, sep, rest = base.partition(" · ")
        # Only the clock changes; other feedback (「正在听 · 已记录」) keeps its text.
        if base.startswith(_MEETING_SHORT) and sep and rest.count(":") == 2 and len(rest) == 8:
            self._full_status_text = f"{_MEETING_SHORT} · {elapsed}{suffix}"
            self._update_status_elision()

    def show_meeting_paused(
        self, detail: str = "", *, title: str = MEETING_STATE_LABELS["paused"]
    ) -> None:
        self._transient_restore_timer.stop()
        self._meeting_active = True
        self._listening_active = True
        self._meeting_paused_detail = detail
        self._set_state(title, "STATE_WARN", pulse=False)
        self._waveform.stop()
        self._waveform.hide()
        self._apply_excerpt(detail or "采集已暂停，可在会议页继续或结束")
        self._present()

    def end_meeting_state(self, detail: str = "") -> None:
        self._transient_restore_timer.stop()
        self._meeting_active = False
        self._meeting_paused_detail = ""
        self._meeting_source = ""
        self._listening_active = False
        self._capture_active = False
        self.set_esc_stops_continuous(self._esc_stops_continuous)
        self._mark_btn.hide()
        self._live_text = ""
        self.show_success("会议已结束", detail or "可到「会议」页生成纪要")

    def _set_state(self, text: str, color: str, pulse: bool = False):
        self._state_token = color
        resolved = getattr(tok, color, color)
        # AMBER on white is about 3.2:1. Warning text uses the darker text
        # token; the dot keeps the brighter amber so it still reads as a warning.
        label_color = tok.AMBER_TEXT if self._state_token == "STATE_WARN" else resolved
        self._full_status_text = text
        self._set_widget_css(
            self._status_label,
            f"color: {label_color}; background: transparent;"
            f" font-family: {tok.FONT_DISPLAY};",
        )
        self._dot.set_state_token(self._state_token)
        self._dot.set_color(resolved)
        self._waveform.set_accent(resolved)
        if pulse:
            self._dot.start_pulse()
        else:
            self._dot.stop_pulse()
        self._update_status_elision()

    def _apply_excerpt(self, text: str, *, keep_tail: bool = False) -> None:
        stripped = " ".join((text or "").split())
        if not stripped:
            self._text_label.setText("")
            self._text_label.setToolTip("")
            self._text_label.hide()
            self._set_mode("compact")
            return
        metrics = QFontMetrics(self._text_label.font())
        avail = max(80, BAR_EXCERPT_WIDTH - 48)
        mode = Qt.TextElideMode.ElideLeft if keep_tail else Qt.TextElideMode.ElideRight
        display = metrics.elidedText(stripped, mode, avail)
        self._text_label.setText(display)
        self._text_label.setToolTip(stripped)
        self._text_label.show()
        self._idle_collapsed = False
        self._set_mode("excerpt")

    def _hold_capture(self) -> bool:
        """True while the hold key is still down (bar must stay put)."""
        return self._capture_active and not self._listening_active and not self._capture_released

    def _show_capture_transcript(self, text: str) -> None:
        """Update hold-to-talk words without resizing, recoloring, or restarting."""
        cleaned = " ".join((text or "").split())
        if not cleaned:
            return
        self._live_text = cleaned
        if self._status_label.text() != "录音中":
            self._set_state("录音中", "STATE_RECORD", pulse=True)
        if self._waveform._visible_bars != 5:
            self._waveform.set_compact(True)
        self._waveform.show()
        self._waveform.start()
        metrics = QFontMetrics(self._text_label.font())
        avail = max(80, BAR_EXCERPT_WIDTH - 48)
        display = metrics.elidedText(cleaned, Qt.TextElideMode.ElideLeft, avail)
        if self._text_label.text() != display or not self._text_label.isVisible() or self._mode != "excerpt":
            self._apply_excerpt(cleaned, keep_tail=True)
        self._hide_timer.stop()
        if not self.isVisible():
            self._present()

    def _restore_live_line(self) -> None:
        if not self._live_text:
            return
        if self._listening_active or self._capture_active:
            self._waveform.set_compact(True)
            self._waveform.show()
            self._waveform.start()
        self._apply_excerpt(self._live_text, keep_tail=True)

    def _restore_compact_height(self) -> None:
        self._text_label.setToolTip("")
        self._text_label.hide()
        self._set_mode("compact")

    def show_listening(self, target_app: str | None = None):
        """Show the listening bar.

        ``None`` keeps the process name of the utterance already open.
        ``""`` clears it, which a new session does before any speech starts.
        """
        if self._meeting_active:
            if self._meeting_paused_detail:
                self.show_meeting_paused(self._meeting_paused_detail)
            else:
                self.show_meeting()
            return
        self._model_loading_active = False
        self._listening_active = True
        self._capture_active = False
        self._capture_released = False
        self.set_esc_stops_continuous(self._esc_stops_continuous)
        self._status_label.setToolTip(self._end_btn.toolTip())
        self._remember_move_hint()
        if target_app is not None:
            self._listen_target = target_app
        self._waveform.set_compact(bool(self._live_text or self._listen_target))
        self._waveform.show()
        self._waveform.start()
        if self._live_text:
            label = f"正在听 · {self._listen_target}" if self._listen_target else "正在听"
        elif self._listen_target:
            full_candidate = f"正在听 · 停顿后出字 · {self._listen_target}"
            metrics = QFontMetrics(self._status_label.font())
            if metrics.horizontalAdvance(full_candidate) <= self._available_status_width():
                label = full_candidate
            else:
                label = f"正在听 · {self._listen_target}"
        else:
            label = "正在听 · 停顿后出字"
        self._set_state(label, "STATE_LISTEN", pulse=False)
        self._use_drag_cursor()
        if self._live_text:
            self._apply_excerpt(self._live_text, keep_tail=True)
        else:
            self._restore_compact_height()
        self._present()

    def show_live_transcript(self, text: str) -> None:
        cleaned = " ".join((text or "").split())
        if self._meeting_bar_stays(cleaned):
            return
        if self._hold_capture():
            self._show_capture_transcript(cleaned)
            return
        self._live_text = cleaned
        if self._listening_active:
            label = "正在听"
            if self._listen_target:
                label = f"{label} · {self._listen_target}"
            self._set_state(label, "STATE_LISTEN", pulse=False)
        elif self._capture_active and not self._capture_released:
            self._set_state("录音中", "STATE_RECORD", pulse=False)
        else:
            # Key already up (or no capture at all): the result arriving must
            # not flip the bar back to a red 「录音中」 before paste finishes.
            self._set_state("正在识别", "STATE_RECOGNIZE", pulse=False)
        self._restore_live_line()
        self._present()

    def clear_live_transcript(self) -> None:
        self._live_text = ""

    def show_continuous_idle(self, hotkey: str):
        self._model_loading_active = False
        self._restore_compact_height()
        self._listening_active = False
        self._set_state("待开始", "STATE_MUTED", pulse=False)
        self._apply_excerpt(f"按住 {hotkey} 开始持续监听")
        self._use_drag_cursor()
        self._waveform.stop()
        self._waveform.hide()
        self._present()

    def show_continuous_stopped(self, detail: str = ""):
        if self._meeting_bar_stays(detail, title="已停止监听", transient=True):
            return
        self._restore_compact_height()
        self._listening_active = False
        self._set_state("已停止监听", "STATE_MUTED", pulse=False)
        self._apply_excerpt(detail)
        self._use_drag_cursor()
        self._waveform.stop()
        self._waveform.hide()
        self._present()

    def end_capture(self) -> None:
        """Hold key released: drop the 「松开结束」 hint and show recognition.

        The words recognized so far stay on the bar; ``show_recognizing`` /
        ``show_polishing`` may now update the title (they stay silent while the
        key is held so the bar does not flicker).
        """
        if not self._capture_active or self._listening_active or self._capture_released:
            return
        self._capture_released = True
        self._set_state("正在识别", "STATE_RECOGNIZE", pulse=False)
        self._waveform.stop()
        self._waveform.hide()
        if self._live_text:
            self._apply_excerpt(self._live_text, keep_tail=True)
        else:
            self._apply_excerpt("松开了，正在识别…")
        self._hide_timer.stop()
        self._present()

    def show_recording(self):
        continuing = self._hold_capture() and self._status_label.text() == "录音中" and self.isVisible()
        self._listening_active = False
        self._capture_active = True
        self._capture_released = False
        if self._live_text:
            self._show_capture_transcript(self._live_text)
            return
        if continuing and "松开识别" in self._text_label.text():
            return
        self._set_state("录音中", "STATE_RECORD", pulse=True)
        self._waveform.set_compact(False)
        self._waveform.show()
        self._waveform.start()
        self._apply_excerpt("松开识别 · Esc 取消本次录音，不保留内容")
        self._status_label.setToolTip("松开快捷键后识别；Esc 取消本次录音，不保留本次内容。")
        self._present()

    def dismiss_if_idle(self):
        if self._meeting_active:
            # The meeting bar stays up for the whole meeting.
            self.show_listening()
            return
        self._model_loading_active = False
        self._listening_active = False
        self._capture_active = False
        self._capture_released = False
        self._set_mode("compact")
        self._use_drag_cursor()
        self._waveform.stop()
        self._waveform.hide()
        self._dot.stop_pulse()
        self._hide_timer.stop()
        self.hide()

    def _restore_after_transient_meeting_notice(self) -> None:
        if not self._meeting_active:
            return
        if self._live_text:
            self._apply_excerpt(self._live_text, keep_tail=True)
        elif self._meeting_paused_detail:
            self._apply_excerpt(self._meeting_paused_detail, keep_tail=False)
        elif self._meeting_source:
            self._apply_excerpt(f"音频来源：{self._meeting_source} · 点此打开会议页")
        else:
            self._apply_excerpt("点此打开会议页 · 停顿后出字")

    def _meeting_bar_stays(
        self,
        excerpt: str = "",
        *,
        title: str = "",
        transient: bool = False,
        duration_ms: int = 3500,
    ) -> bool:
        """A meeting keeps 「会议中」 and the clock. Transient notices do not pollute _live_text."""
        if not self._meeting_active:
            return False
        cleaned = " ".join((excerpt or "").split())
        clean_title = " ".join((title or "").split())
        if transient:
            if clean_title and cleaned and cleaned != clean_title:
                notice = f"{clean_title} · {cleaned}"
            else:
                notice = clean_title or cleaned
            if notice:
                self._apply_excerpt(notice, keep_tail=False)
                if duration_ms > 0:
                    self._transient_restore_timer.start(duration_ms)
                else:
                    self._transient_restore_timer.stop()
        else:
            self._transient_restore_timer.stop()
            if cleaned:
                self._live_text = cleaned
                self._apply_excerpt(cleaned, keep_tail=True)
        self._hide_timer.stop()
        self._present()
        return True

    def show_recognizing(self, partial_text: str = ""):
        if self._meeting_bar_stays(partial_text):
            return
        if self._hold_capture():
            if partial_text:
                self._show_capture_transcript(partial_text)
            return
        if partial_text:
            self._live_text = " ".join(partial_text.split())
        keep_line = (self._listening_active or self._capture_active) and bool(self._live_text)
        if not keep_line:
            self._restore_compact_height()
        if self._listening_active or self._capture_active:
            self._set_state("正在听 · 识别中", "STATE_LISTEN", pulse=False)
            self._waveform.set_compact(bool(self._live_text))
            self._waveform.show()
            self._waveform.start()
            if self._capture_active and not self._listening_active:
                self._set_state(
                    "正在识别" if self._capture_released else "录音中 · 识别中",
                    "STATE_RECOGNIZE",
                    pulse=False,
                )
        else:
            self._set_state("正在识别", "STATE_RECOGNIZE", pulse=False)
            self._waveform.stop()
            self._waveform.hide()
        if keep_line:
            self._apply_excerpt(self._live_text, keep_tail=True)
        elif partial_text:
            display = partial_text if len(partial_text) <= 50 else "..." + partial_text[-47:]
            self._apply_excerpt(display)
        self._present()

    def show_polishing(self, text: str = "", *, action: str = "润色"):
        """``action`` is the configured LLM step: 润色 or 纠错."""
        if self._meeting_bar_stays(text or self._live_text):
            return
        busy = f"{action}中"
        if self._hold_capture():
            if self._live_text:
                self._show_capture_transcript(self._live_text)
            return
        keep_line = (self._listening_active or self._capture_active) and bool(self._live_text)
        if not keep_line:
            self._restore_compact_height()
        if self._listening_active or (self._capture_active and self._live_text):
            if self._listening_active:
                label, color = f"正在听 · {busy}", "STATE_LISTEN"
            elif self._capture_released:
                label, color = busy, "STATE_POLISH"
            else:
                label, color = f"录音中 · {busy}", "STATE_LISTEN"
            self._set_state(label, color, pulse=False)
            self._waveform.set_compact(bool(self._live_text))
            self._waveform.show()
            self._waveform.start()
            if self._live_text:
                self._apply_excerpt(self._live_text, keep_tail=True)
                self._present()
                return
        else:
            self._set_state(busy, "STATE_POLISH", pulse=False)
            self._waveform.stop()
            self._waveform.hide()
        if text:
            display = text if len(text) <= 50 else "..." + text[-47:]
            self._apply_excerpt(display)
        self._present()

    def show_success(self, message: str = "已输入", subtitle: str = ""):
        if self._meeting_bar_stays(subtitle or self._live_text):
            return
        self._model_loading_active = False
        if self._listening_active:
            self._set_state(f"正在听 · {message}", "STATE_LISTEN", pulse=False)
            self._waveform.set_compact(bool(self._live_text))
            self._waveform.show()
            self._waveform.start()
            if self._live_text:
                self._apply_excerpt(self._live_text, keep_tail=True)
                self._present()
                return
            self._restore_compact_height()
        elif self._capture_active and self._live_text:
            self._set_state(message, "STATE_SUCCESS")
            self._waveform.stop()
            self._waveform.hide()
            self._apply_excerpt(self._live_text, keep_tail=True)
            self._present()
            self._hide_timer.start(2200)
            return
        else:
            self._restore_compact_height()
            self._waveform.stop()
            self._waveform.hide()
            self._set_state(message, "STATE_SUCCESS")
        self._apply_excerpt(subtitle)
        self._present()
        if not self._listening_active:
            dur = 2200 if subtitle else 1500
            self._hide_timer.start(dur)

    def show_info(self, message: str, subtitle: str = ""):
        if self._meeting_bar_stays(subtitle, title=message, transient=True):
            return
        if (self._listening_active or self._capture_active) and self._live_text:
            if self._listening_active:
                self._set_state(f"正在听 · {message}", "STATE_LISTEN", pulse=False)
            else:
                self._set_state(message, "STATE_RECOGNIZE", pulse=False)
            self._restore_live_line()
            self._present()
            if self._capture_active and not self._listening_active:
                self._hide_timer.start(2200)
            return
        self._restore_compact_height()
        if self._listening_active:
            self._set_state(f"正在听 · {message}", "STATE_LISTEN", pulse=False)
            self._waveform.show()
            self._waveform.start()
        else:
            self._waveform.stop()
            self._waveform.hide()
            self._set_state(message, "STATE_RECOGNIZE", pulse=False)
        self._apply_excerpt(subtitle)
        self._present()
        if not self._listening_active:
            dur = 2200 if subtitle else 1800
            self._hide_timer.start(dur)

    def show_warning(self, message: str, subtitle: str = ""):
        if self._model_loading_active:
            return
        if self._meeting_bar_stays(subtitle, title=message, transient=True, duration_ms=5000):
            return
        self._restore_compact_height()
        self._waveform.stop()
        self._waveform.hide()
        self._set_state(message, "STATE_WARN", pulse=False)
        self._apply_excerpt(subtitle, keep_tail=False)
        self._present()
        self._hide_timer.start(5000)

    def show_cancelled(self):
        self._capture_active = False
        self._capture_released = False
        self._live_text = ""
        self._restore_compact_height()
        self._waveform.stop()
        self._waveform.hide()
        self._set_state("已取消", "STATE_MUTED")
        self._text_label.setText("")
        self._present()
        self._hide_timer.start(1000)

    def show_error(self, message: str, *, auto_dismiss_ms: int = 5000):
        if self._model_loading_active:
            return
        if self._meeting_bar_stays(message, transient=True, duration_ms=max(0, int(auto_dismiss_ms or 0))):
            return
        self._set_mode("compact")
        self._waveform.stop()
        self._waveform.hide()
        first_line, _, body_after_newline = message.partition("\n")
        status, colon_remainder = _error_headline(first_line)
        parts = [p for p in (colon_remainder, body_after_newline) if p]
        remainder = "\n".join(parts)
        self._set_state(status, "STATE_ERROR")
        self.setToolTip(message)
        remainder = remainder.strip()
        if remainder:
            self._apply_excerpt(remainder)
            self._text_label.setToolTip(message)
        else:
            self._text_label.setText("")
            self._text_label.setToolTip("")
            self._text_label.hide()
            self._set_mode("compact")
        self._present()
        if auto_dismiss_ms and auto_dismiss_ms > 0:
            self._hide_timer.start(auto_dismiss_ms)
        else:
            self._hide_timer.stop()

    def update_volume(self, volume: float):
        self._waveform.set_volume(volume)

    def show_busy_transcribing(self):
        self._restore_compact_height()
        self._set_state("请稍候", "STATE_RECOGNIZE", pulse=False)
        self._apply_excerpt("正在识别上一轮语音，稍后重试")
        self._waveform.stop()
        self._waveform.hide()
        self._present()
        self._hide_timer.start(2200)

    def update_partial_text(self, text: str):
        cleaned = " ".join((text or "").split())
        if self._listening_active:
            self._live_text = cleaned
            self._waveform.set_compact(bool(cleaned))
            self._waveform.show()
            self._waveform.start()
            self._set_state("正在听" if cleaned else "正在听 · 停顿后出字", "STATE_LISTEN", pulse=False)
        self._apply_excerpt(cleaned, keep_tail=self._listening_active)
        self._present()

    def show_model_loading(self, detail: str = ""):
        if self._meeting_active:
            self._model_loading_active = True
            self._meeting_bar_stays(
                detail or "正在切换识别模型，请稍候…",
                title=MODEL_LOADING_TITLE,
                transient=True,
                duration_ms=0,
            )
            return
        self._listening_active = False
        self._restore_compact_height()
        self._model_loading_active = True
        # The one thing the user must know lives in the title, where nothing
        # elides it; the detail line only carries progress / ETA.
        self._set_state(MODEL_LOADING_TITLE, "STATE_RECOGNIZE", pulse=True)
        self._waveform.hide()
        self._apply_excerpt(
            detail or "模型文件已下载，正在载入内存，请稍候…"
        )
        self._present()

    def clear_model_loading_lock(self) -> None:
        self._model_loading_active = False
        if self._meeting_active:
            self._restore_after_transient_meeting_notice()

    def showEvent(self, event):
        self._dot.set_suspended(False)
        self._waveform.set_suspended(False)
        super().showEvent(event)

    def hideEvent(self, event):
        self._position_pinned = False
        self._user_moved = False
        self._pinned_token = ""
        self._idle_timer.stop()
        self._transient_restore_timer.stop()
        self._meeting_click_timer.stop()
        self._dot.set_suspended(True)
        self._waveform.set_suspended(True)
        super().hideEvent(event)

    def closeEvent(self, event):
        self._disconnect_screen_watchers()
        self._dot.stop_pulse()
        self._waveform.stop()
        super().closeEvent(event)

    def enterEvent(self, event):
        self._restore_idle_excerpt()
        self._note_bar_activity()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._note_bar_activity()
        super().leaveEvent(event)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.ContextMenu:
            pos = event.globalPosition().toPoint() if hasattr(event, "globalPosition") else event.globalPos()
            self._show_position_menu(pos)
            return True
        return super().eventFilter(watched, event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._meeting_click_timer.stop()
            self._drag_tracking = True
            self._moved_past_threshold = False
            self._press_global = event.globalPosition().toPoint()
            self._press_origin = self.pos()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_tracking and event.buttons() & Qt.MouseButton.LeftButton:
            delta = event.globalPosition().toPoint() - self._press_global
            if not self._moved_past_threshold:
                if delta.manhattanLength() < DRAG_THRESHOLD_PX:
                    return
                self._moved_past_threshold = True
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
            self.move(self._press_origin + delta)
            self._user_moved = True
            self._capture_pin_from_widget()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            super().mouseReleaseEvent(event)
            return
        if self._suppress_click:
            self._suppress_click = False
            self._drag_tracking = False
            self._moved_past_threshold = False
            self._use_drag_cursor()
            event.accept()
            return
        moved = self._moved_past_threshold
        tracking = self._drag_tracking
        self._drag_tracking = False
        self._moved_past_threshold = False
        self._use_drag_cursor()
        if tracking and moved:
            self._commit_drag()
            event.accept()
            return
        if tracking and self._meeting_active:
            self._open_meeting_from_click()
        event.accept()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._suppress_click = True
            self._meeting_click_timer.stop()
            self._drag_tracking = False
            self._moved_past_threshold = False
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event):
        pos = event.globalPosition().toPoint() if hasattr(event, "globalPosition") else event.globalPos()
        self._show_position_menu(pos)
        event.accept()

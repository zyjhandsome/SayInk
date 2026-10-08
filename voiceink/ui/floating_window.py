from PyQt6.QtWidgets import (
    QWidget, QLabel, QVBoxLayout, QHBoxLayout, QPushButton,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFontMetrics, QPainter, QColor, QFont, QCursor
import math
from voiceink.ui import design_tokens as tok
from voiceink.ui.island_chrome import (
    island_window_flags,
    position_listen_bar,
)

BAR_HEIGHT = 44
BAR_EXCERPT_HEIGHT = 68
BAR_WIDTH = 360
BAR_EXCERPT_WIDTH = 420
COMPACT_HEIGHT = BAR_HEIGHT


def _ui_font(family, pixels, weight=QFont.Weight.Normal):
    font = QFont(family)
    font.setPixelSize(pixels)
    font.setWeight(weight)
    return font


class _DotIndicator(QWidget):
    """Listening ring that pulses while a session is active."""

    def __init__(self, color: str = "", parent=None):
        super().__init__(parent)
        # Resolve at construction time so the live theme axis wins over the
        # value that happened to be active when this module was imported.
        self._color = QColor(color or tok.STATE_RECORD)
        self._pulse = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self.setFixedSize(16, 16)

    def set_color(self, color: str):
        self._color = QColor(color)
        self.update()

    def start_pulse(self):
        if self._timer.isActive():
            return
        self._pulse = 0.0
        self._timer.start(50)

    def stop_pulse(self):
        self._timer.stop()
        self._pulse = 0.0
        self.update()

    def _tick(self):
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

        pen = self._color
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        from PyQt6.QtGui import QPen
        ring = QPen(self._color)
        ring.setWidth(2)
        p.setPen(ring)
        p.drawEllipse(2, 2, 12, 12)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._color)
        p.drawEllipse(6, 6, 4, 4)
        p.end()


class WaveformWidget(QWidget):

    NUM_BARS = 14

    def __init__(self, parent=None):
        super().__init__(parent)
        self._volume = 0.0
        self._bar_heights = [0.0] * self.NUM_BARS
        self._phase = 0.0
        self._accent = QColor(tok.FLOAT_TEXT)
        self._visible_bars = self.NUM_BARS
        self.setFixedHeight(18)
        self.setFixedWidth(72)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._animate)

    def set_accent(self, color: str):
        self._accent = QColor(color)

    def start(self):
        if self._timer.isActive():
            return
        self._phase = 0.0
        self._timer.start(50)

    def stop(self):
        self._timer.stop()
        self._bar_heights = [0.0] * self.NUM_BARS
        self.update()

    def set_compact(self, compact: bool) -> None:
        self._visible_bars = 5 if compact else self.NUM_BARS
        self.setFixedWidth(28 if compact else 72)

    def set_volume(self, volume: float):
        self._volume = min(volume * 8, 1.0)

    def _animate(self):
        self._phase += 0.3
        for i in range(self.NUM_BARS):
            wave = math.sin(self._phase + i * 0.5) * 0.5 + 0.5
            target = wave * self._volume
            self._bar_heights[i] += (target - self._bar_heights[i]) * 0.4
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
    settings_requested = pyqtSignal()
    history_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._listening_active = False
        self._capture_active = False
        # Set by end_capture() once the hold key is up; cleared by show_recording().
        self._capture_released = False
        self._model_loading_active = False
        self._live_text = ""
        self._current_accent = tok.FLOAT_TEXT
        self._mode = "compact"
        self._setup_window()
        self._setup_ui()
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)

    def island_mode(self) -> str:
        return self._mode

    def set_input_source(self, source: str) -> None:
        self._source = source or "microphone"

    def reapply_theme(self) -> None:
        from voiceink.ui import design_tokens as tok

        self._paint_container()
        self._status_label.setFont(
            _ui_font(tok.UI_FONT_FAMILY, tok.TYPE_BODY_SM, QFont.Weight.Bold)
        )
        self._status_label.setStyleSheet(
            f"color: {tok.FLOAT_TEXT}; background: transparent;"
            f" font-family: {tok.FONT_DISPLAY};"
        )
        self._end_btn.setStyleSheet(f"""
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
        """)
        self._text_label.setFont(_ui_font(tok.UI_FONT_FAMILY, tok.TYPE_BODY_SM))
        self._text_label.setStyleSheet(
            f"color: {tok.FLOAT_TEXT}; background: transparent;"
            f" font-family: {tok.FONT};"
        )
        if hasattr(self, "_state_token"):
            self._set_state(self._status_label.text(), self._state_token,
                            pulse=self._dot._timer.isActive())
        else:
            self._current_accent = tok.FLOAT_TEXT

    def _paint_container(self) -> None:
        radius = 16 if self._mode == "excerpt" else BAR_HEIGHT // 2
        self._container.setStyleSheet(
            f"QWidget#islandContainer {{ background: {tok.FLOAT_BG};"
            f" border: 1px solid {tok.FLOAT_BORDER}; border-radius: {radius}px; }}")

    def _setup_window(self):
        self.setWindowFlags(island_window_flags() | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setMinimumSize(BAR_WIDTH, BAR_HEIGHT)
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
        self._wave = self._waveform
        header_row.addWidget(self._waveform)

        from voiceink.ui import design_tokens as tok

        self._status_label = QLabel("准备中...")
        self._status_label.setFont(
            _ui_font(tok.UI_FONT_FAMILY, tok.TYPE_BODY_SM, QFont.Weight.Bold)
        )
        header_row.addWidget(self._status_label)
        self._source = "microphone"
        header_row.addStretch()

        self._end_btn = QPushButton("结束")
        self._end_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._end_btn.setFixedHeight(30)
        self._end_btn.setMinimumWidth(64)
        self._end_btn.setAccessibleName("结束持续转写")
        self._end_btn.setToolTip("结束整场监听 · Esc")
        self._end_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._end_btn.clicked.connect(self._on_end_clicked)
        header_row.addWidget(self._end_btn)
        container_layout.addLayout(header_row)

        self._text_label = QLabel("")
        self._text_label.setFont(_ui_font(tok.UI_FONT_FAMILY, tok.TYPE_BODY_SM))
        self._text_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._text_label.setWordWrap(False)
        self._text_label.setMaximumHeight(18)
        self._text_label.hide()
        container_layout.addWidget(self._text_label)

        layout.addWidget(self._container)
        self.reapply_theme()
        self.set_input_source("microphone")
        self._set_mode("compact")

    def _set_mode(self, mode: str) -> None:
        self._mode = mode
        self._paint_container()
        excerpt = mode == "excerpt"
        if excerpt:
            self._text_label.setMaximumHeight(18)
            self.setMinimumWidth(BAR_WIDTH)
            self.setFixedHeight(BAR_EXCERPT_HEIGHT)
            self.resize(BAR_EXCERPT_WIDTH, BAR_EXCERPT_HEIGHT)
        else:
            self._text_label.setMaximumHeight(18)
            self.setMinimumWidth(BAR_WIDTH)
            self.setFixedHeight(BAR_HEIGHT)
            self.resize(BAR_WIDTH, BAR_HEIGHT)

    def expand_live(self, text: str = "") -> None:
        self._hide_timer.stop()
        if text:
            self._apply_excerpt(text)
        else:
            self._text_label.show()
            self._set_mode("excerpt")
        self._present()

    def collapse_live(self) -> None:
        if not self._listening_active:
            return
        self._text_label.hide()
        self._set_mode("compact")
        self._present()

    def _on_close_clicked(self) -> None:
        if self._listening_active:
            self.continuous_stop_requested.emit()
        else:
            self.dismiss_if_idle()

    def _on_end_clicked(self) -> None:
        self.continuous_stop_requested.emit()

    def _position_on_screen(self):
        position_listen_bar(self, width=self.width())

    def _present(self) -> None:
        self._hide_timer.stop()
        self._end_btn.setVisible(self._listening_active)
        self._position_on_screen()
        self.show()

    def _set_state(self, text: str, color: str, pulse: bool = False):
        self._state_token = color
        color = getattr(tok, color, color)
        self._current_accent = color
        self._status_label.setText(text)
        self._status_label.setStyleSheet(
            f"color: {color}; background: transparent;"
            f" font-family: {tok.FONT_DISPLAY};"
        )
        self._dot.set_color(color)
        self._waveform.set_accent(color)
        if pulse:
            self._dot.start_pulse()
        else:
            self._dot.stop_pulse()

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

    def show_listening(self):
        self._model_loading_active = False
        self._listening_active = True
        self._capture_active = False
        self._capture_released = False
        label = "正在听" if self._live_text else "正在听 · 停顿后出字"
        self._set_state(label, "STATE_LISTEN", pulse=False)
        self.unsetCursor()
        self._waveform.set_compact(bool(self._live_text))
        self._waveform.show()
        self._waveform.start()
        if self._live_text:
            self._apply_excerpt(self._live_text, keep_tail=True)
        else:
            self._restore_compact_height()
        self._present()

    def show_live_transcript(self, text: str) -> None:
        cleaned = " ".join((text or "").split())
        if self._hold_capture():
            self._show_capture_transcript(cleaned)
            return
        self._live_text = cleaned
        if self._listening_active:
            self._set_state("正在听", "STATE_LISTEN", pulse=False)
        elif self._capture_active:
            self._set_state("录音中", "STATE_RECORD", pulse=False)
        else:
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
        self.unsetCursor()
        self._waveform.stop()
        self._waveform.hide()
        self._present()

    def show_continuous_stopped(self, detail: str = ""):
        self._restore_compact_height()
        self._listening_active = False
        self._set_state("已停止监听", "STATE_MUTED", pulse=False)
        self._apply_excerpt(detail)
        self.unsetCursor()
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
        if continuing and "松开结束" in self._text_label.text():
            return
        self._set_state("录音中", "STATE_RECORD", pulse=True)
        self._waveform.set_compact(False)
        self._waveform.show()
        self._waveform.start()
        self._apply_excerpt("松开结束，Esc 取消")
        self._present()

    def dismiss_if_idle(self):
        self._model_loading_active = False
        self._listening_active = False
        self._capture_active = False
        self._capture_released = False
        self._set_mode("compact")
        self.unsetCursor()
        self._waveform.stop()
        self._waveform.hide()
        self._dot.stop_pulse()
        self._hide_timer.stop()
        self.hide()

    def show_recognizing(self, partial_text: str = ""):
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

    def show_polishing(self, text: str = ""):
        if self._hold_capture():
            if self._live_text:
                self._show_capture_transcript(self._live_text)
            return
        keep_line = (self._listening_active or self._capture_active) and bool(self._live_text)
        if not keep_line:
            self._restore_compact_height()
        if self._listening_active or (self._capture_active and self._live_text):
            if self._listening_active:
                label, color = "正在听 · 润色中", "STATE_LISTEN"
            elif self._capture_released:
                label, color = "润色中", "STATE_POLISH"
            else:
                label, color = "录音中 · 润色中", "STATE_LISTEN"
            self._set_state(label, color, pulse=False)
            self._waveform.set_compact(bool(self._live_text))
            self._waveform.show()
            self._waveform.start()
            if self._live_text:
                self._apply_excerpt(self._live_text, keep_tail=True)
                self._present()
                return
        else:
            self._set_state("润色中", "STATE_POLISH", pulse=False)
            self._waveform.stop()
            self._waveform.hide()
        if text:
            display = text if len(text) <= 50 else "..." + text[-47:]
            self._apply_excerpt(display)
        self._present()

    def show_success(self, message: str = "已输入", subtitle: str = ""):
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
        self._restore_compact_height()
        self._waveform.stop()
        self._waveform.hide()
        self._set_state(message, "STATE_WARN", pulse=False)
        self._apply_excerpt(subtitle)
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
        self._set_mode("compact")
        self._waveform.stop()
        self._waveform.hide()
        first_line, _, body_after_newline = message.partition("\n")
        if "：" in first_line:
            status, colon_remainder = first_line.split("：", 1)
        else:
            status = first_line[:12]
            colon_remainder = first_line[12:]
        parts = [p for p in (colon_remainder, body_after_newline) if p]
        remainder = "\n".join(parts)
        self._set_state(status, "STATE_ERROR")
        self.setToolTip(message)
        remainder = remainder.strip()
        if remainder:
            self._apply_excerpt(remainder)
            self._text_label.setToolTip(message)
            self.setFixedHeight(min(self.height(), BAR_EXCERPT_HEIGHT))
        else:
            self._text_label.setText("")
            self._text_label.setToolTip("")
            self._text_label.hide()
            self.setFixedHeight(COMPACT_HEIGHT)
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
        self._listening_active = False
        self._restore_compact_height()
        self._model_loading_active = True
        self._set_state("模型载入中", "STATE_RECOGNIZE", pulse=False)
        self._waveform.hide()
        self._apply_excerpt(
            detail or "模型文件已下载，正在载入内存，请稍候…"
        )
        self._present()

    def clear_model_loading_lock(self) -> None:
        self._model_loading_active = False

    def mousePressEvent(self, event):
        super().mousePressEvent(event)

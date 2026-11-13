"""The 「会议」 page: meeting list + detail, start card, and the live view.

Three states (docs/meeting-notes-design.md §4.3):

* **browse** — list of past meetings on the left, the selected meeting's
  minutes / transcript on the right;
* **start card** — title / participants / agenda / source / model before a
  meeting starts (skipped when ``meeting.show_start_card`` is off);
* **live** — the running transcript, a mark button and a notes box while a
  meeting is recording.

The page never touches audio or the recognizer: it asks the
:class:`~sayink.app.App` through ``start_requested`` / ``end_requested`` /
``resume_requested`` and reads everything else from the
:class:`~sayink.meeting_session.MeetingController`.
"""

from __future__ import annotations

import bisect
import html
import json
import logging
from pathlib import Path
import re
import tempfile
import time
import wave
import weakref

from PyQt6.QtCore import QEvent, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QDesktopServices,
    QKeySequence,
    QPainter,
    QShortcut,
    QTextCursor,
)
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSplitter,
    QStackedWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from sayink.audio_devices import input_source_label
from sayink.aux_models import (
    AuxModelDownloads,
    aux_models_size_mb,
    describe_aux_models,
    missing_aux_models,
)
from sayink.meeting_session import (
    MeetingController,
    MeetingOptions,
    MeetingState,
    minutes_settings_complete,
    resolve_capabilities,
    safe_filename,
)
from sayink.meeting_store import STATUS_ENDED, STATUS_INTERRUPTED, STATUS_LIVE, MeetingRecord, QueuedGeneration, now_ms
from sayink.minutes_prompts import describe_template, format_timestamp, get_template, list_templates, transcript_lines
from sayink.media_import import MEDIA_SOURCE, file_dialog_filter
from sayink.transcript_import import IMPORT_SOURCE
from sayink.ui.history_export import export_save_path
from sayink.recognition_settings import ADVANCED_RECOGNITION_PATH, audio_source
from sayink.speech_recognizer import DEFAULT_MODEL_ID, MODEL_REGISTRY, is_model_downloaded
from sayink.ui import design_tokens as tok
from sayink.ui.clipboard import copy_text
from sayink.ui.flow_layout import PairedToolBar, WrappingToolBar
from sayink.ui.history_window import (
    _ElidedLabel, _SessionCheckBox, _SessionList, release_list_item_widgets, session_list_item_css,
)
from sayink.ui.meeting_batch import confirm_batch_delete, fit_action_button
from sayink.ui.meeting_drafts import MeetingDraftBuffer
from sayink.ui.meeting_views import (
    _STATUS_TEXT,
    _CurrentPageStack,
    _duration_label,
    _export_kind,
    _format_dt,
    _meeting_row_meta,
    _meeting_row_text,
    _position_label,
    _ReadingView,
    _SegmentSpeakerDialog,
    _SegmentTextDialog,
    _speaker_anchor_map as _speaker_anchor_map,
    _speaker_anchor_skeleton as _speaker_anchor_skeleton,
    _speaker_source,
    _split_top_paragraphs as _split_top_paragraphs,
    _TranscriptView,
    _with_export_suffix,
    build_transcript_html,
    clear_reading_search,
    find_in_reading_view,
    link_citations,
    notes_for_display,
    speaker_color as speaker_color,
)

log = logging.getLogger("SayInk")

PAGE_BROWSE = 0
PAGE_START = 1
PAGE_LIVE = 2

_SOURCE_LABELS = tuple((key, input_source_label(key)) for key in ("mixed", "microphone", "system"))
_LANGUAGE_LABELS = (("auto", "自动（中文优先）"), ("zh", "中文"), ("en", "English"), ("ja", "日本語"))
_EMPTY_MEETINGS = "还没有会议记录。\n点击右上角「导入文字稿」「导入音视频」或「开始会议」，也可以从托盘菜单开始。"


class _MeetingStreamRow(QWidget):
    """Row chrome matches 历史: the wash is painted here, clicks fall through."""

    def mousePressEvent(self, event):
        event.ignore()


def _paint_meeting_row(row: QWidget, *, selected: bool) -> None:
    from sayink.ui import design_tokens as tok

    bg = tok.ACCENT_SOFT if selected else "transparent"
    bar = tok.ACCENT if selected else "transparent"
    css = (
        "QWidget#meetingStreamRow {"
        f" background: {bg};"
        " border: none;"
        f" border-left: {tok.NAV_SELECTED_BAR_PX}px solid {bar};"
        " border-radius: 0;"
        "}"
        f"QLabel#meetingRowTitle {{ color: {tok.TEXT}; font-size: {tok.TYPE_BODY_SM}px; background: transparent; }}"
        f"QLabel#meetingRowMeta {{ color: {tok.TEXT_SEC}; font-size: {tok.TYPE_CAPTION}px; background: transparent; }}"
    )
    if row.styleSheet() != css:
        row.setStyleSheet(css)


def _style_dialog_box(box: QMessageBox, *, danger_btn: QPushButton | None = None) -> None:
    from sayink.ui import design_tokens as tok
    from sayink.ui import settings_styles as ss

    box.setStyleSheet(
        ss.WINDOW_CSS
        + f" QMessageBox {{ background: {tok.BG}; color: {tok.TEXT}; }}"
        + f" QMessageBox QLabel {{ color: {tok.TEXT}; background: transparent; }}"
    )
    for btn in box.buttons():
        if danger_btn is not None and btn is danger_btn:
            btn.setStyleSheet(ss.BTN_DANGER_SOLID_SM)
        elif box.buttonRole(btn) == QMessageBox.ButtonRole.AcceptRole:
            btn.setStyleSheet(ss.BTN_ACCENT_SM)
        else:
            btn.setStyleSheet(ss.BTN_GHOST_SM)


class _CleanComboBox(QComboBox):
    """ComboBox that elides overflowing closed-box text cleanly instead of mid-glyph clipping."""

    def paintEvent(self, event) -> None:
        from PyQt6.QtWidgets import QStyle, QStyleOptionComboBox, QStylePainter

        painter = QStylePainter(self)
        opt = QStyleOptionComboBox()
        self.initStyleOption(opt)
        edit_rect = self.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox,
            opt,
            QStyle.SubControl.SC_ComboBoxEditField,
            self,
        )
        avail = max(24, edit_rect.width() - 4)
        if opt.fontMetrics.horizontalAdvance(opt.currentText) > avail:
            opt.currentText = opt.fontMetrics.elidedText(
                opt.currentText, Qt.TextElideMode.ElideRight, avail
            )
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, opt)
        painter.drawControl(QStyle.ControlElement.CE_ComboBoxLabel, opt)


class _LevelMeterBar(QFrame):
    """Lightweight single-lane real-time audio level meter."""

    def __init__(self, accessible_name: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._level = 0.0
        self._active = True
        self.setFixedHeight(8)
        self.setMinimumWidth(64)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        if accessible_name:
            self.setAccessibleName(accessible_name)

    @property
    def level(self) -> float:
        return self._level

    def set_active(self, active: bool) -> None:
        active = bool(active)
        if self._active != active:
            self._active = active
            if not active:
                self._level = 0.0
            self.update()

    def set_level(self, value: float) -> None:
        try:
            clamped = max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            clamped = 0.0
        if not self._active:
            clamped = 0.0
        if abs(self._level - clamped) >= 0.005:
            self._level = clamped
            self.update()

    def paintEvent(self, event) -> None:
        from sayink.ui import design_tokens as tok

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.rect()
        track_color = QColor(tok.HAIRLINE)
        if not self._active:
            track_color.setAlpha(90)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(track_color)
        painter.drawRoundedRect(rect, 4.0, 4.0)
        if self._active and self._level > 0.005:
            fill_w = max(4, int(round(rect.width() * self._level)))
            bar_color = QColor(tok.STATE_WARN if self._level > 0.85 else tok.ACCENT)
            painter.setBrush(bar_color)
            painter.drawRoundedRect(0, 0, min(rect.width(), fill_w), rect.height(), 4.0, 4.0)
        painter.end()


def _format_audio_clock(ms: int) -> str:
    sec = max(0, int(ms) // 1000)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _probe_wav_duration_ms(path: Path) -> int:
    try:
        if path.suffix.lower() == ".wav" and path.is_file():
            with wave.open(str(path), "rb") as wf:
                rate = wf.getframerate()
                frames = wf.getnframes()
                if rate > 0 and frames >= 0:
                    return int(round(frames * 1000.0 / rate))
        elif path.suffix.lower() == ".f32" and path.is_file():
            samples = path.stat().st_size // 4
            return int(round(samples * 1000.0 / 16000.0))
        elif path.is_dir() and (path / "audio.f32").is_file():
            samples = (path / "audio.f32").stat().st_size // 4
            return int(round(samples * 1000.0 / 16000.0))
    except Exception:
        pass
    return 0


class _AudioPlayerBar(QFrame):
    """Lightweight embedded audio playback bar for local meeting / imported / recovery audio."""

    seeked = pyqtSignal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("meetingAudioPlayer")
        self._source_path = ""
        self._playable_path = ""
        self._duration_ms = 0
        self._position_ms = 0
        self._playing = False
        self._rate = 1.0
        self._dragging_slider = False
        self._player = None
        self._audio_out = None
        self._fallback_timer = QTimer(self)
        self._fallback_timer.setInterval(200)
        self._fallback_timer.timeout.connect(self._on_fallback_tick)
        self._fallback_last_ts = 0.0

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 4, 10, 4)
        lay.setSpacing(8)
        self._play_btn = QPushButton("播放")
        self._play_btn.setAccessibleName("播放或暂停会议录音")
        self._play_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._play_btn.clicked.connect(self.toggle_play)
        lay.addWidget(self._play_btn, 0)

        self._time_label = QLabel("00:00 / 00:00")
        self._time_label.setAccessibleName("音频回放进度")
        lay.addWidget(self._time_label, 0)

        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setAccessibleName("拖动音频进度")
        self._slider.setRange(0, 0)
        self._slider.sliderPressed.connect(self._on_slider_pressed)
        self._slider.sliderReleased.connect(self._on_slider_released)
        self._slider.valueChanged.connect(self._on_slider_changed)
        lay.addWidget(self._slider, 1)

        self._speed_combo = QComboBox()
        self._speed_combo.setAccessibleName("倍速播放")
        self._speed_combo.setToolTip("调整音频回放倍速")
        for rate in (0.75, 1.0, 1.25, 1.5, 2.0):
            label = f"{rate:g}x" if rate != 1.0 else "1.0x"
            self._speed_combo.addItem(label, rate)
        self._speed_combo.setCurrentIndex(1)
        self._speed_combo.currentIndexChanged.connect(self._on_speed_changed)
        self._speed_combo.setFixedWidth(96)
        lay.addWidget(self._speed_combo, 0)

    @property
    def audio_path(self) -> str:
        return self._source_path

    @property
    def is_playing(self) -> bool:
        return self._playing

    @property
    def position_ms(self) -> int:
        return self._position_ms

    @property
    def duration_ms(self) -> int:
        return self._duration_ms

    @property
    def playback_rate(self) -> float:
        return self._rate

    def reapply_theme(self) -> None:
        from sayink.ui import design_tokens as tok
        from sayink.ui import settings_styles as ss

        self.setStyleSheet(
            f"QFrame#meetingAudioPlayer {{ background: {tok.SURFACE_PEARL}; border: 1px solid {tok.HAIRLINE};"
            f" border-radius: {tok.RADIUS_MD}px; }}"
        )
        self._play_btn.setStyleSheet(ss.BTN_GHOST_SM)
        self._time_label.setStyleSheet(
            f"color: {tok.TEXT_SEC}; font-size: {tok.TYPE_CAPTION}px; background: transparent;"
        )
        self._speed_combo.setStyleSheet(ss.build_toolbar_combo_css())
        self._slider.setStyleSheet(
            f"QSlider::groove:horizontal {{ height: 4px; background: {tok.HAIRLINE}; border-radius: 2px; }}"
            f"QSlider::sub-page:horizontal {{ background: {tok.ACCENT}; border-radius: 2px; }}"
            f"QSlider::handle:horizontal {{ width: 12px; height: 12px; margin: -4px 0;"
            f" border-radius: 6px; background: {tok.ACCENT}; }}"
        )

    def _ensure_backend(self) -> bool:
        if self._player is not None:
            return True
        try:
            from PyQt6.QtMultimedia import QAudioOutput, QMediaDevices, QMediaPlayer

            if not QMediaDevices.audioOutputs():
                return False
            self._audio_out = QAudioOutput(self)
            self._player = QMediaPlayer(self)
            self._player.setAudioOutput(self._audio_out)
            self._player.positionChanged.connect(self._on_backend_position)
            self._player.durationChanged.connect(self._on_backend_duration)
            self._player.playbackStateChanged.connect(self._on_backend_state)
            self._player.setPlaybackRate(self._rate)
            return True
        except Exception:
            self._player = None
            self._audio_out = None
            return False

    def _materialize_playable_path(self) -> str:
        if self._playable_path and Path(self._playable_path).is_file():
            return self._playable_path
        if not self._source_path:
            return ""
        src = Path(self._source_path)
        if src.is_file() and src.suffix.lower() != ".f32":
            self._playable_path = str(src)
            return self._playable_path
        # Export raw float32 journal / cache to a temporary PCM WAV on demand.
        try:
            import numpy as np

            raw_f32 = src / "audio.f32" if src.is_dir() else src
            if not raw_f32.is_file():
                return ""
            out_dir = Path(tempfile.gettempdir()) / "sayink-playback"
            out_dir.mkdir(parents=True, exist_ok=True)
            tag = f"{raw_f32.parent.name}-{raw_f32.stat().st_size}"
            wav_path = out_dir / f"{tag}.wav"
            if not wav_path.is_file():
                samples = np.fromfile(raw_f32, dtype="<f4")
                pcm = (np.clip(np.nan_to_num(samples), -1.0, 1.0) * 32767.0).astype("<i2")
                with wave.open(str(wav_path), "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(16000)
                    wf.writeframes(pcm.tobytes())
            self._playable_path = str(wav_path)
            return self._playable_path
        except Exception:
            return ""

    def load_audio(self, path: str | Path | None, *, fallback_duration_ms: int = 0) -> bool:
        resolved = str(path or "").strip()
        if not resolved:
            self.stop()
            self._source_path = ""
            self._playable_path = ""
            self._duration_ms = 0
            self._position_ms = 0
            self._sync_ui()
            return False
        p = Path(resolved)
        valid = p.is_file() or (p.is_dir() and (p / "audio.f32").is_file())
        if not valid:
            self.stop()
            self._source_path = ""
            self._playable_path = ""
            self._duration_ms = 0
            self._position_ms = 0
            self._sync_ui()
            return False
        if resolved == self._source_path:
            if self._duration_ms <= 0 and fallback_duration_ms > 0:
                self._duration_ms = int(fallback_duration_ms)
                self._sync_ui()
            return True
        self.stop()
        self._source_path = resolved
        self._playable_path = str(p) if p.is_file() and p.suffix.lower() != ".f32" else ""
        probed = _probe_wav_duration_ms(p)
        self._duration_ms = max(probed, max(0, int(fallback_duration_ms)))
        self._position_ms = 0
        if self._player is not None and self._playable_path:
            try:
                self._player.setSource(QUrl.fromLocalFile(self._playable_path))
            except Exception:
                pass
        self._sync_ui()
        return True

    def toggle_play(self) -> None:
        if self._playing:
            self.pause()
        else:
            self.play()

    def play(self) -> None:
        if not self._source_path:
            return
        if self._duration_ms > 0 and self._position_ms >= self._duration_ms:
            self._position_ms = 0
        playable = self._materialize_playable_path()
        used_backend = False
        if playable and self._ensure_backend() and self._player is not None:
            try:
                target_url = QUrl.fromLocalFile(playable)
                if self._player.source() != target_url:
                    self._player.setSource(target_url)
                self._player.setPlaybackRate(self._rate)
                self._player.setPosition(self._position_ms)
                self._player.play()
                from PyQt6.QtMultimedia import QMediaPlayer

                used_backend = self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
            except Exception:
                used_backend = False
        self._playing = True
        if not used_backend:
            self._fallback_last_ts = time.monotonic()
            self._fallback_timer.start()
        else:
            self._fallback_timer.stop()
        self._sync_ui()

    def pause(self) -> None:
        self._playing = False
        self._fallback_timer.stop()
        if self._player is not None:
            try:
                self._player.pause()
            except Exception:
                pass
        self._sync_ui()

    def stop(self) -> None:
        self._playing = False
        self._fallback_timer.stop()
        if self._player is not None:
            try:
                self._player.stop()
            except Exception:
                pass
        self._position_ms = 0
        self._sync_ui()

    def seek_ms(self, offset_ms: int, *, autoplay: bool = False) -> None:
        target = max(0, int(offset_ms))
        if self._duration_ms > 0:
            target = min(target, self._duration_ms)
        self._position_ms = target
        if self._player is not None:
            try:
                self._player.setPosition(target)
            except Exception:
                pass
        if autoplay:
            self.play()
        else:
            self._sync_ui()
        self.seeked.emit(target)

    def seek_and_play(self, offset_ms: int) -> None:
        self.seek_ms(offset_ms, autoplay=True)

    def _on_fallback_tick(self) -> None:
        if not self._playing:
            self._fallback_timer.stop()
            return
        now = time.monotonic()
        elapsed_ms = int(round((now - self._fallback_last_ts) * 1000.0 * self._rate))
        self._fallback_last_ts = now
        self._position_ms += max(0, elapsed_ms)
        if self._duration_ms > 0 and self._position_ms >= self._duration_ms:
            self._position_ms = self._duration_ms
            self._playing = False
            self._fallback_timer.stop()
        self._sync_ui()

    def _on_backend_position(self, pos: int) -> None:
        if not self._dragging_slider:
            self._position_ms = max(0, int(pos))
            self._sync_ui()

    def _on_backend_duration(self, dur: int) -> None:
        if dur > 0:
            self._duration_ms = int(dur)
            self._sync_ui()

    def _on_backend_state(self, state) -> None:
        try:
            from PyQt6.QtMultimedia import QMediaPlayer

            if state == QMediaPlayer.PlaybackState.StoppedState and not self._fallback_timer.isActive():
                self._playing = False
                self._sync_ui()
        except Exception:
            pass

    def _on_slider_pressed(self) -> None:
        self._dragging_slider = True

    def _on_slider_released(self) -> None:
        self._dragging_slider = False
        self.seek_ms(self._slider.value(), autoplay=self._playing)

    def _on_slider_changed(self, value: int) -> None:
        if self._dragging_slider:
            self._position_ms = max(0, int(value))
            self._time_label.setText(
                f"{_format_audio_clock(self._position_ms)} / {_format_audio_clock(self._duration_ms)}"
            )

    def _on_speed_changed(self, _index: int) -> None:
        rate = float(self._speed_combo.currentData() or 1.0)
        self._rate = rate
        if self._player is not None:
            try:
                self._player.setPlaybackRate(rate)
            except Exception:
                pass

    def _sync_ui(self) -> None:
        self._play_btn.setText("暂停" if self._playing else "播放")
        upper = max(self._duration_ms, self._position_ms, 0)
        self._slider.blockSignals(True)
        self._slider.setRange(0, upper)
        if not self._dragging_slider:
            self._slider.setValue(min(self._position_ms, upper))
        self._slider.blockSignals(False)
        self._time_label.setText(
            f"{_format_audio_clock(self._position_ms)} / {_format_audio_clock(self._duration_ms)}"
        )
class MeetingPage(QWidget):
    start_requested = pyqtSignal(object)   # MeetingOptions
    end_requested = pyqtSignal()
    pause_requested = pyqtSignal()
    resume_requested = pyqtSignal()
    configure_requested = pyqtSignal()     # minutes need the AI service connection
    aux_models_changed = pyqtSignal()      # a helper model was downloaded here
    _title_write_settled = pyqtSignal(str, str, int)
    _record_notes_write_settled = pyqtSignal(str, str, int)
    _history_write_failed = pyqtSignal()

    def __init__(self, config, controller: MeetingController, parent=None) -> None:
        super().__init__(parent)
        self._config = config
        self._ctrl = controller
        self._store = controller.store
        self._aux_dl = AuxModelDownloads(str(config.get("stt.download_source", "auto") or "auto"), self)
        self._aux_dl.progress.connect(self._on_aux_progress)
        self._aux_dl.finished.connect(self._on_aux_finished)
        self._drafts = MeetingDraftBuffer()
        self._records: dict[str, MeetingRecord] = {}
        self._detail_id = ""
        self._search_query = ""
        self._meeting_audio_paths: dict[str, str] = {}
        self._pending_media_import_path = ""
        self._live_remote_active_ts = time.monotonic()
        self._system_silent_dismissed = False
        self._transcript_offsets: list[int] = []
        self._transcript_time_known: list[bool] = []
        self._transcript_has_speakers = True
        self._transcript_has_times = True
        self._transcript_highlight = -1
        self._transcript_sequences: list[int] = []
        self._corrected_sequences: set[int] = set()
        self._speaker_locked_sequences: set[int] = set()
        self._transcript_editable = False
        self._citation_return: tuple[str, int] | None = None
        self._correcting_speakers = False
        self._live_correcting_speakers = False
        self._live_transcript_sequences: list[int] = []
        self._live_locked_sequences: set[int] = set()
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(400)
        self._refresh_timer.timeout.connect(self._refresh_now)
        self._pending_refresh_id = ""
        self._listed_generating = ""
        self._list_limit = 100
        self._batch_mode = False
        self._setup_ui()
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(self._status_label.hide)
        self._live_feedback_timer = QTimer(self)
        self._live_feedback_timer.setSingleShot(True)
        self._live_feedback_timer.timeout.connect(self._live_speaker_feedback.hide)
        self._live_silence_timer = QTimer(self)
        self._live_silence_timer.setInterval(1000)
        self._live_silence_timer.timeout.connect(self._check_live_remote_silence)
        self._title_write_settled.connect(self._on_title_write_settled)
        self._record_notes_write_settled.connect(self._on_record_notes_write_settled)
        self._history_write_failed.connect(self._on_history_write_failed)
        history = getattr(self._store, "_history", None)
        if history is not None and hasattr(history, "add_failed_callback"):
            weak_self = weakref.ref(self)
            history.add_failed_callback(
                lambda _msg: (page := weak_self()) is not None and page._history_write_failed.emit()
            )
        self._ctrl.state_changed.connect(self._on_state_changed)
        self._ctrl.meeting_changed.connect(self._on_meeting_changed)
        self._ctrl.clock_tick.connect(self._live_clock.setText)
        self._ctrl.generation_progress.connect(self._on_generation_progress)
        self._ctrl.generation_preview.connect(self._on_generation_preview)
        self._ctrl.generation_finished.connect(self._on_generation_finished)
        self._ctrl.generation_failed.connect(self._on_generation_failed)
        self._ctrl.generation_cancelled.connect(self._on_generation_cancelled)
        self._ctrl.generation_queued.connect(self._on_generation_queued)
        self._ctrl.media_import_progress.connect(self._on_media_progress)
        self._ctrl.media_import_finished.connect(self._on_media_finished)
        self._ctrl.media_import_failed.connect(self._on_media_failed)
        self.reapply_theme()
        self.refresh()
        if self._ctrl.is_active:
            self._enter_live()

    # ── draft state compatibility delegates ───────────────────────

    @property
    def _editing_notes(self) -> bool:
        return self._drafts.editing_notes

    @_editing_notes.setter
    def _editing_notes(self, value: bool) -> None:
        self._drafts.editing_notes = bool(value)

    @property
    def _pending_title(self) -> tuple[str, str] | None:
        return self._drafts.pending_title

    @_pending_title.setter
    def _pending_title(self, value: tuple[str, str] | None) -> None:
        self._drafts.pending_title = value

    @property
    def _record_notes_meeting_id(self) -> str:
        return self._drafts.record_notes_meeting_id

    @_record_notes_meeting_id.setter
    def _record_notes_meeting_id(self, value: str) -> None:
        self._drafts.record_notes_meeting_id = value

    @property
    def _unsaved_live_notes(self) -> dict[str, str]:
        return self._drafts.unsaved_live_notes

    # ── construction ──────────────────────────────────────────────

    def _setup_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(*tok.WORKSPACE_MARGINS)
        outer.setSpacing(14)

        top = QHBoxLayout()
        top.setSpacing(12)
        from sayink.ui.settings_components import page_title_label

        self._title_label = page_title_label("会议")
        top.addWidget(self._title_label, 1, Qt.AlignmentFlag.AlignVCenter)
        self._import_btn = QPushButton("导入文字稿")
        self._import_btn.setAccessibleName("导入文字稿")
        self._import_btn.setToolTip("粘贴或打开已有文字稿，导入为一场会议")
        self._import_btn.clicked.connect(self._import_transcript)
        from sayink.ui import settings_styles as ss
        self._import_btn.setStyleSheet(ss.BTN_GHOST_SM)
        top.addWidget(self._import_btn, 0)
        self._media_btn = QPushButton("导入音视频")
        self._media_btn.setAccessibleName("导入音视频")
        self._media_btn.setToolTip("打开本机音视频，识别成一场会议。不复制原文件。")
        self._media_btn.clicked.connect(self._import_media)
        self._media_btn.setStyleSheet(ss.BTN_GHOST_SM)
        top.addWidget(self._media_btn, 0)
        self._media_cancel_btn = QPushButton("取消导入")
        self._media_cancel_btn.setAccessibleName("取消导入音视频")
        self._media_cancel_btn.setToolTip("停在当前这段之后，已经识别的文字会留下")
        self._media_cancel_btn.clicked.connect(self._cancel_media_import)
        self._media_cancel_btn.setStyleSheet(ss.BTN_GHOST_SM)
        self._media_cancel_btn.hide()
        top.addWidget(self._media_cancel_btn, 0)
        self._media_retry_btn = QPushButton("重试保存")
        self._media_retry_btn.setAccessibleName("重试保存导入结果")
        self._media_retry_btn.setToolTip("上次识别的文字还在，再写入一次数据库")
        self._media_retry_btn.clicked.connect(self._retry_media_save)
        self._media_retry_btn.setStyleSheet(ss.BTN_GHOST_SM)
        self._media_retry_btn.hide()
        top.addWidget(self._media_retry_btn, 0)
        self._start_btn = QPushButton("开始会议")
        self._start_btn.setAccessibleName("开始会议")
        self._start_btn.setToolTip("开始一场新的会议记录")
        self._start_btn.clicked.connect(self.show_start_card)
        top.addWidget(self._start_btn, 0)
        outer.addLayout(top)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_browse())
        self._stack.addWidget(self._build_start_card())
        self._stack.addWidget(self._build_live())
        outer.addWidget(self._stack, 1)
        for button in self.findChildren(QPushButton):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._sync_media_buttons()

    def _build_browse(self) -> QWidget:
        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.setChildrenCollapsible(False)
        self._splitter.setHandleWidth(5)

        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(6)
        left.setMinimumWidth(tok.WORKSPACE_LIST_MIN_WIDTH)
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("搜索标题、参会人或文字稿")
        self._search_edit.setAccessibleName("搜索会议")
        self._search_edit.setClearButtonEnabled(True)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(200)
        self._search_edit.textChanged.connect(lambda: self._search_timer.start())
        self._search_timer.timeout.connect(self._perform_search)
        left_lay.addWidget(self._search_edit)
        left_lay.addSpacing(4)
        self._summary_row = WrappingToolBar(spacing=8)
        self._list_summary = QLabel()
        self._list_summary.setContentsMargins(4, 0, 0, 0)
        self._summary_row.add(self._list_summary, role="flex")
        self._selection_mode_btn = QPushButton("选择")
        self._selection_mode_btn.setCheckable(True)
        self._selection_mode_btn.setAccessibleName("选择会议")
        self._selection_mode_btn.setToolTip("勾选多场会议，一起导出、生成或删除；也可使用 Ctrl / Shift 多选，Delete 删除")
        self._selection_mode_btn.toggled.connect(self._set_batch_mode)
        self._summary_row.add(self._selection_mode_btn, role="right")
        left_lay.addWidget(self._summary_row)
        self._list = _SessionList()
        self._list.setObjectName("meetingList")
        self._list.setAccessibleName("会议列表")
        self._list.setToolTip("Ctrl 点选，Shift 连选。点「选择」后，单击一行即可勾选或取消。Delete 删除所选")
        self._list.setFrameShape(QFrame.Shape.NoFrame)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self._list.setWordWrap(True)
        self._list.itemSelectionChanged.connect(self._on_selection_changed)
        self._delete_shortcut = QShortcut(QKeySequence.StandardKey.Delete, self._list)
        self._delete_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._delete_shortcut.activated.connect(self._delete_selected_meetings)
        left_lay.addWidget(self._list, 1)
        self._load_more_btn = QPushButton("加载更早的会议")
        self._load_more_btn.setAccessibleName("加载更早的会议")
        self._load_more_btn.clicked.connect(self._load_more)
        self._load_more_btn.hide()
        left_lay.addWidget(self._load_more_btn, 0, Qt.AlignmentFlag.AlignHCenter)
        self._empty_label = QLabel(_EMPTY_MEETINGS)
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setWordWrap(True)
        left_lay.addWidget(self._empty_label, 1)
        self._clear_search_btn = QPushButton("清除搜索")
        self._clear_search_btn.setMaximumWidth(160)
        self._clear_search_btn.clicked.connect(self._search_edit.clear)
        self._clear_search_btn.hide()
        left_lay.addWidget(self._clear_search_btn, 0, Qt.AlignmentFlag.AlignHCenter)
        self._splitter.addWidget(left)

        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(20, 0, 0, 0)
        right_lay.setSpacing(8)

        # Identity first (title + meta), then the content tabs; each tab's tools
        # sit in the tab row so they read as acting on that tab only.
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        self._detail_title = QLineEdit()
        self._detail_title.setPlaceholderText("会议标题")
        self._detail_title.setAccessibleName("会议标题")
        self._detail_title.setToolTip("点击修改会议标题")
        self._detail_title.editingFinished.connect(self._save_title)
        title_row.addWidget(self._detail_title, 1)
        self._copy_btn = QPushButton("复制")
        self._copy_btn.setToolTip("复制纪要 Markdown")
        self._copy_btn.clicked.connect(self._copy_current)
        self._export_btn = QPushButton("导出")
        self._export_btn.setToolTip("选择要导出的会议内容")
        self._export_menu = QMenu(self._export_btn)
        self._export_actions = {}
        for scope, label in (("notes", "仅导出纪要"), ("transcript", "仅导出文字稿"), ("all", "导出完整会议记录")):
            action = self._export_menu.addAction(label)
            action.triggered.connect(lambda _checked=False, kind=scope: self._export(kind))
            self._export_actions[scope] = action
        self._export_btn.setMenu(self._export_menu)
        self._more_btn = QPushButton("更多")
        self._more_btn.setAccessibleName("更多操作")
        self._more_btn.setToolTip("更多操作")
        self._more_menu = QMenu(self._more_btn)
        self._restore_action = self._more_menu.addAction("恢复上一版纪要")
        self._restore_action.triggered.connect(self._restore_previous)
        self._restore_user_action = self._more_menu.addAction("恢复手动修改版")
        self._restore_user_action.triggered.connect(self._restore_user_backup)
        self._more_menu.addSeparator()
        self._delete_action = self._more_menu.addAction("删除会议")
        self._delete_action.triggered.connect(self._delete)
        self._more_btn.setMenu(self._more_menu)
        title_row.addWidget(self._more_btn)
        right_lay.addLayout(title_row)

        self._batch_tools = WrappingToolBar(spacing=8)
        self._batch_all_btn = QPushButton("全选已加载")
        self._batch_all_btn.setAccessibleName("全选已加载的会议")
        self._batch_all_btn.setToolTip("只选择当前列表里已经加载的会议。更早的要先点「加载更早的会议」")
        self._batch_all_btn.clicked.connect(self._select_loaded_meetings)
        self._batch_export_btn = QPushButton("导出")
        self._batch_export_btn.setAccessibleName("导出选中的会议")
        self._batch_export_btn.setToolTip("把所选会议写入一个 Markdown 文件")
        self._batch_export_btn.clicked.connect(self._export_selected_meetings)
        self._batch_generate_btn = QPushButton("生成纪要")
        self._batch_generate_btn.setAccessibleName("为选中的会议生成纪要")
        self._batch_generate_btn.setToolTip("只排队还没有纪要的会议。已有纪要的会跳过")
        self._batch_generate_btn.clicked.connect(self._generate_selected_meetings)
        self._batch_delete_btn = QPushButton("删除")
        self._batch_delete_btn.setAccessibleName("删除选中的会议")
        self._batch_delete_btn.setToolTip("删除勾选的已结束会议，无法撤销")
        self._batch_delete_btn.clicked.connect(self._delete_selected_meetings)
        for button in (self._batch_all_btn, self._batch_export_btn, self._batch_generate_btn, self._batch_delete_btn):
            self._batch_tools.add(button)
        self._batch_tools.hide()
        right_lay.addWidget(self._batch_tools)
        self._batch_notice = QLabel("选择要一起处理的会议。")
        self._batch_notice.setWordWrap(True)
        self._batch_notice.setAccessibleName("已选会议说明")
        self._batch_notice.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._batch_notice_scroll = QScrollArea()
        self._batch_notice_scroll.setWidgetResizable(True)
        self._batch_notice_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._batch_notice_scroll.setWidget(self._batch_notice)
        # Grow only to the notice's own height; the trailing stretch keeps the
        # notice and any status line directly under the batch tools.
        self._batch_notice_scroll.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self._batch_notice_scroll.hide()
        right_lay.addWidget(self._batch_notice_scroll)
        self._import_progress = QLabel("")
        self._import_progress.setWordWrap(True)
        self._import_progress.setAccessibleName("导入进度")
        self._import_progress.hide()
        right_lay.addWidget(self._import_progress)
        self._import_progress_bar = QProgressBar()
        self._import_progress_bar.setAccessibleName("导入进度条")
        self._import_progress_bar.setRange(0, 100)
        self._import_progress_bar.setValue(0)
        self._import_progress_bar.setFixedHeight(10)
        self._import_progress_bar.setTextVisible(False)
        self._import_progress_bar.hide()
        right_lay.addWidget(self._import_progress_bar)

        self._detail_meta = QLabel("")
        self._detail_meta.setObjectName("meetingDetailMeta")
        self._detail_meta.setWordWrap(True)
        self._detail_meta.setContentsMargins(4, 0, 0, 0)
        self._notes_state = QLabel()
        self._notes_state.setWordWrap(True)
        self._notes_state.setAccessibleName("纪要来源与保存状态")
        self._state_row = QWidget()
        self._state_row.setObjectName("meetingState")
        state_lay = QHBoxLayout(self._state_row)
        state_lay.setContentsMargins(0, 0, 0, 0)
        state_lay.setSpacing(8)
        state_lay.addWidget(self._notes_state, 1)
        self._cancel_btn = QPushButton("取消")
        self._cancel_btn.clicked.connect(self._cancel_job)
        self._cancel_btn.hide()
        state_lay.addWidget(self._cancel_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        right_lay.addWidget(self._detail_meta)
        right_lay.addWidget(self._state_row)
        self._materials = QWidget()
        self._materials.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        materials_lay = QVBoxLayout(self._materials)
        materials_lay.setContentsMargins(0, 0, 0, 0)
        materials_lay.setSpacing(4)
        self._materials_summary_text = "会议资料"
        self._materials_toggle = QPushButton("▸ 会议资料")
        self._materials_toggle.setCheckable(True)
        self._materials_toggle.setChecked(False)
        self._materials_toggle.setAccessibleName("会议资料")
        self._materials_toggle.setToolTip("点击展开或收起议程、随手笔记和标记。笔记在离开时自动保存。")
        materials_lay.addWidget(self._materials_toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self._materials_body = QWidget()
        body_lay = QVBoxLayout(self._materials_body)
        body_lay.setContentsMargins(0, 2, 4, 2)
        body_lay.setSpacing(4)
        self._materials_agenda = QLabel()
        self._materials_agenda.setWordWrap(True)
        self._materials_agenda.setTextFormat(Qt.TextFormat.PlainText)
        body_lay.addWidget(self._materials_agenda)
        self._materials_notes_label = QLabel("我的笔记")
        body_lay.addWidget(self._materials_notes_label)
        self._record_notes = QPlainTextEdit()
        self._record_notes.setAccessibleName("我的笔记")
        self._record_notes.setPlaceholderText("会中记下的内容；离开时自动保存。")
        self._record_notes.setMinimumHeight(38)
        self._record_notes.setMaximumHeight(84)
        self._record_notes_timer = QTimer(self)
        self._record_notes_timer.setSingleShot(True)
        self._record_notes_timer.setInterval(800)
        self._record_notes_timer.timeout.connect(self._autosave_record_notes)
        self._record_notes.textChanged.connect(self._on_record_notes_changed)
        body_lay.addWidget(self._record_notes)
        self._materials_marks_label = QLabel("标记")
        body_lay.addWidget(self._materials_marks_label)
        self._record_marks = QTextBrowser()
        self._record_marks.setAccessibleName("标记")
        self._record_marks.setOpenLinks(False)
        self._record_marks.setMinimumHeight(32)
        self._record_marks.setMaximumHeight(76)
        self._record_marks.anchorClicked.connect(self._on_notes_link)
        body_lay.addWidget(self._record_marks)
        self._materials_scroll = QScrollArea()
        self._materials_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._materials_scroll.setWidgetResizable(True)
        self._materials_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._materials_scroll.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self._materials_scroll.setMaximumHeight(168)
        self._materials_scroll.setWidget(self._materials_body)
        self._materials_scroll.hide()
        self._materials_toggle.toggled.connect(self._on_materials_toggled)
        materials_lay.addWidget(self._materials_scroll)
        self._materials.hide()
        right_lay.addWidget(self._materials)

        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        self._status_label.hide()
        right_lay.addWidget(self._status_label)

        self._audio_player = _AudioPlayerBar()
        self._audio_player.hide()
        right_lay.addWidget(self._audio_player)

        tab_row = QHBoxLayout()
        tab_row.setSpacing(8)
        tab_row.setContentsMargins(0, 6, 0, 0)
        self._tab_bar = QWidget()
        self._tab_bar.setObjectName("meetingTabs")
        tabs_lay = QHBoxLayout(self._tab_bar)
        tabs_lay.setContentsMargins(3, 2, 3, 2)
        tabs_lay.setSpacing(2)
        self._tab_group = QButtonGroup(self)
        self._tab_group.setExclusive(True)
        self._tab_notes = QPushButton("纪要")
        self._tab_transcript = QPushButton("文字稿")
        for index, button in enumerate((self._tab_notes, self._tab_transcript)):
            button.setCheckable(True)
            button.setObjectName("meetingTab")
            button.setMinimumWidth(88)
            button.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
            self._tab_group.addButton(button, index)
            tabs_lay.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)
        self._tab_notes.setChecked(True)
        self._tab_group.idClicked.connect(self._switch_tab)
        tab_row.addWidget(self._tab_bar, 0, Qt.AlignmentFlag.AlignVCenter)
        tab_row.addStretch(1)
        tab_row.addWidget(self._export_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        right_lay.addLayout(tab_row)

        self._tab_tools = _CurrentPageStack()
        notes_tools = QWidget()
        notes_outer = QVBoxLayout(notes_tools)
        notes_outer.setContentsMargins(0, 0, 0, 0)
        notes_outer.setSpacing(8)
        self._template_label = QLabel("模板")
        self._template_combo = QComboBox()
        self._template_combo.setAccessibleName("会议模板")
        self._template_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        for template in list_templates(self._custom_templates()):
            self._template_combo.addItem(template.name, template.id)
            self._template_combo.setItemData(
                self._template_combo.count() - 1, describe_template(template), Qt.ItemDataRole.ToolTipRole
            )
        self._template_combo.currentIndexChanged.connect(self._on_template_changed)
        self._template_combo.setMinimumWidth(96)
        self._template_combo.setMaximumWidth(168)
        self._template_combo.setToolTip(str(self._template_combo.itemData(0, Qt.ItemDataRole.ToolTipRole) or ""))
        self._template_label.setBuddy(self._template_combo)
        self._manage_templates_btn = QPushButton("管理模板")
        self._manage_templates_btn.setToolTip("新建自定义模板，或复制内置模板后修改")
        self._manage_templates_btn.clicked.connect(self._manage_templates)
        self._template_menu_btn = QPushButton("模板")
        self._template_menu_btn.setAccessibleName("选择会议模板")
        self._template_menu_btn.setToolTip("选择纪要模板")
        self._template_menu = QMenu(self._template_menu_btn)
        self._template_menu_btn.setMenu(self._template_menu)
        self._template_menu_btn.hide()
        self._fitting_templates = False
        self._generate_btn = QPushButton("生成纪要")
        self._generate_btn.setAccessibleName("生成纪要")
        self._generate_btn.clicked.connect(self._generate)
        self._edit_btn = QPushButton("编辑正文")
        self._edit_btn.setToolTip("直接修改纪要文本")
        self._edit_btn.clicked.connect(self._toggle_edit)
        self._cancel_notes_edit_btn = QPushButton("取消")
        self._cancel_notes_edit_btn.setAccessibleName("取消编辑纪要")
        self._cancel_notes_edit_btn.setToolTip("放弃本次未保存的修改并恢复修改前内容")
        self._cancel_notes_edit_btn.clicked.connect(self._cancel_edit_notes)
        self._cancel_notes_edit_btn.hide()
        self._notes_bar = PairedToolBar(spacing=8)
        self._notes_bar.add_left(self._template_label)
        self._notes_bar.add_left(self._template_combo)
        self._notes_bar.add_left(self._manage_templates_btn)
        self._notes_bar.add_left(self._template_menu_btn)
        self._notes_bar.installEventFilter(self)
        self._notes_bar.add_right(self._generate_btn)
        self._notes_bar.add_right(self._edit_btn)
        self._notes_bar.add_right(self._cancel_notes_edit_btn)
        self._notes_bar.add_right(self._copy_btn)
        notes_outer.addWidget(self._notes_bar)
        self._tab_tools.addWidget(notes_tools)
        tools_policy = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        tools_policy.setHeightForWidth(True)
        self._tab_tools.setSizePolicy(tools_policy)

        transcript_tools = QWidget()
        transcript_lay = QVBoxLayout(transcript_tools)
        transcript_lay.setContentsMargins(0, 0, 0, 0)
        transcript_lay.setSpacing(8)
        self._transcript_bar = PairedToolBar(spacing=8)
        self._speaker_btn = QPushButton("命名说话人")
        self._speaker_btn.setAccessibleName("为整组说话人命名")
        self._speaker_btn.setToolTip("为同一组发言设置显示名称；名称来自手动设置，不表示已验证身份")
        self._speaker_menu = QMenu(self._speaker_btn)
        self._speaker_btn.setMenu(self._speaker_menu)
        self._transcript_bar.add_right(self._speaker_btn)
        self._correct_speaker_btn = QPushButton("纠正发言人")
        self._correct_speaker_btn.setCheckable(True)
        self._correct_speaker_btn.toggled.connect(self._toggle_speaker_correction)
        self._transcript_bar.add_right(self._correct_speaker_btn)
        self._return_notes_btn = QPushButton("返回纪要")
        self._return_notes_btn.setAccessibleName("返回纪要引用处")
        self._return_notes_btn.setToolTip("回到刚才阅读的纪要引用位置")
        self._return_notes_btn.clicked.connect(self._return_to_notes)
        self._return_notes_btn.hide()
        self._transcript_bar.add_right(self._return_notes_btn)
        self._transcript_copy_btn = QPushButton("复制文字稿")
        self._transcript_copy_btn.setAccessibleName("复制文字稿")
        self._transcript_copy_btn.setToolTip("复制完整文字稿（含时间与说话人名称） · Ctrl+C")
        self._transcript_copy_btn.clicked.connect(self._copy_transcript)
        self._transcript_bar.add_right(self._transcript_copy_btn)
        transcript_lay.addWidget(self._transcript_bar)
        self._tab_tools.addWidget(transcript_tools)
        right_lay.addWidget(self._tab_tools)
        self._content_hint = QLabel()
        self._content_hint.setWordWrap(True)
        self._content_hint.hide()
        right_lay.addWidget(self._content_hint)

        self._detail_find_bar = QWidget()
        detail_find_lay = QHBoxLayout(self._detail_find_bar)
        detail_find_lay.setContentsMargins(0, 0, 0, 0)
        detail_find_lay.setSpacing(6)
        self._detail_find_edit = QLineEdit()
        self._detail_find_edit.setPlaceholderText("查找当前纪要或文字稿；Enter 下一处，Shift+Enter 上一处")
        self._detail_find_edit.setAccessibleName("查找当前纪要或文字稿")
        self._detail_find_edit.setClearButtonEnabled(True)
        self._detail_find_edit.textChanged.connect(lambda: self._find_detail_text(restart=True))
        self._detail_find_edit.returnPressed.connect(self._find_detail_text)
        self._detail_find_edit.installEventFilter(self)
        detail_find_lay.addWidget(self._detail_find_edit, 1)
        self._detail_find_status = QLabel()
        detail_find_lay.addWidget(self._detail_find_status, 0)
        self._detail_find_prev_btn = QPushButton("上一处")
        self._detail_find_prev_btn.setAccessibleName("查找上一处")
        self._detail_find_prev_btn.clicked.connect(lambda: self._find_detail_text(backward=True))
        detail_find_lay.addWidget(self._detail_find_prev_btn, 0)
        self._detail_find_next_btn = QPushButton("下一处")
        self._detail_find_next_btn.setAccessibleName("查找下一处")
        self._detail_find_next_btn.clicked.connect(lambda: self._find_detail_text(backward=False))
        detail_find_lay.addWidget(self._detail_find_next_btn, 0)
        self._detail_find_close_btn = QPushButton("关闭查找")
        self._detail_find_close_btn.clicked.connect(self._close_detail_find)
        detail_find_lay.addWidget(self._detail_find_close_btn, 0)
        self._detail_find_bar.hide()
        right_lay.addWidget(self._detail_find_bar)

        self._content_stack = QStackedWidget()
        self._content_stack.setMinimumHeight(140)
        self._notes_stack = QStackedWidget()
        self._notes_view = _ReadingView()
        self._notes_view.setOpenLinks(False)
        self._notes_view.copy_document_requested.connect(self._copy_current)
        self._notes_view.anchorClicked.connect(self._on_notes_link)
        self._notes_view.setAccessibleName("会议纪要")
        self._notes_view.setPlaceholderText("还没有纪要。选择模板后点击「生成纪要」。")
        self._notes_view.installEventFilter(self)
        self._notes_edit = QPlainTextEdit()
        self._notes_edit.setAccessibleName("编辑会议纪要")
        self._notes_edit.textChanged.connect(self._sync_copy_action)
        self._notes_edit.installEventFilter(self)
        self._notes_stack.addWidget(self._notes_view)
        self._notes_stack.addWidget(self._notes_edit)
        # QTextEdit placeholders are drawn on one line by Qt. Use a real label
        # for an empty document so instructions wrap with the detail pane.
        self._notes_empty = QLabel()
        self._notes_empty.setWordWrap(True)
        self._notes_empty.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._notes_empty.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._notes_stack.addWidget(self._notes_empty)
        self._content_stack.addWidget(self._notes_stack)
        self._transcript_view = _TranscriptView()
        self._transcript_view.setAccessibleName("会议文字稿")
        self._transcript_view.setPlaceholderText("文字稿为空")
        self._transcript_view.setOpenLinks(False)
        self._transcript_view.copy_document_requested.connect(self._copy_current)
        self._transcript_view.anchorClicked.connect(self._on_transcript_link)
        self._transcript_view.installEventFilter(self)
        self._content_stack.addWidget(self._transcript_view)
        right_lay.addWidget(self._content_stack, 1)
        # Takes the spare height only when the content stack is hidden
        # (batch selection, media import), so those views stay top-aligned.
        right_lay.addStretch(0)

        self._detail_pane = right
        right.setMinimumWidth(tok.WORKSPACE_DETAIL_MIN_WIDTH)
        self._splitter.addWidget(right)
        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 2)
        self._splitter.setSizes(list(tok.WORKSPACE_SPLIT_SIZES))
        return self._splitter

    def _build_start_card(self) -> QWidget:
        page = QWidget()
        page_lay = QVBoxLayout(page)
        page_lay.setContentsMargins(0, 0, 0, 0)
        page_lay.setSpacing(8)
        self._start_scroll = QScrollArea()
        self._start_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._start_scroll.setWidgetResizable(True)
        self._start_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        self._start_scroll.setWidget(content)
        page_lay.addWidget(self._start_scroll, 1)
        lay = QVBoxLayout(content)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self._card_title = QLabel("开始会议")
        lay.addWidget(self._card_title)
        self._card_hint = QLabel(
            "音源、模型和语言仅作用于本场会议；下方标题、参会人与议程均为选填，写进纪要并帮助模型理解上下文。"
        )
        self._card_hint.setWordWrap(True)
        lay.addWidget(self._card_hint)

        row = QHBoxLayout()
        row.setSpacing(10)
        self._opt_source = _CleanComboBox()
        self._opt_source.setAccessibleName("会议音频来源")
        self._opt_source.addItem("跟随转写设置", "")
        for key, label in _SOURCE_LABELS:
            self._opt_source.addItem(label, key)
        self._opt_model = _CleanComboBox()
        self._opt_model.setAccessibleName("会议识别模型")
        self._opt_language = _CleanComboBox()
        self._opt_language.setAccessibleName("会议语言")
        for key, label in _LANGUAGE_LABELS:
            self._opt_language.addItem(label, key)
        for caption, field, stretch in (
            ("音频来源", self._opt_source, 3),
            ("转写模型", self._opt_model, 5),
            ("识别语言", self._opt_language, 2),
        ):
            field.setMinimumWidth(0)
            field.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            field.currentTextChanged.connect(field.setToolTip)
            field.setToolTip(field.currentText())
            column = QVBoxLayout()
            column.setSpacing(4)
            label = QLabel(caption)
            label.setBuddy(field)
            column.addWidget(label)
            column.addWidget(field)
            row.addLayout(column, stretch)
        lay.addLayout(row)

        self._opt_loopback_hint = QLabel("")
        self._opt_loopback_hint.setWordWrap(True)
        self._opt_loopback_hint.setAccessibleName("电脑声回环输出设备")
        self._opt_loopback_hint.hide()
        lay.addWidget(self._opt_loopback_hint)

        self._opt_save_audio = QCheckBox(
            "本机保存会议录音（支持点击文字回放核对与断电恢复，纯离线存储）"
        )
        self._opt_save_audio.setAccessibleName("本机保存会议录音")
        self._opt_save_audio.setChecked(True)
        self._opt_save_audio.toggled.connect(self._on_save_audio_toggled)
        lay.addWidget(self._opt_save_audio)

        meta_row = QHBoxLayout()
        meta_row.setSpacing(10)
        self._opt_title = QLineEdit()
        self._opt_title.setPlaceholderText("会议标题（选填，留空按时间命名）")
        self._opt_title.setAccessibleName("会议标题")
        meta_row.addWidget(self._opt_title, 1)
        self._opt_participants = QLineEdit()
        self._opt_participants.setPlaceholderText("参会人（选填，逗号分隔）")
        self._opt_participants.setAccessibleName("参会人")
        meta_row.addWidget(self._opt_participants, 1)
        lay.addLayout(meta_row)

        self._opt_agenda = QPlainTextEdit()
        self._opt_agenda.setPlaceholderText("议程 / 背景（选填，每行一条）")
        self._opt_agenda.setAccessibleName("议程")
        self._opt_agenda.setFixedHeight(60)
        lay.addWidget(self._opt_agenda)

        self._card_notes = QLabel("")
        self._card_notes.setWordWrap(True)
        self._card_notes.hide()
        lay.addWidget(self._card_notes)
        dl_row = QHBoxLayout()
        self._card_dl_btn = QPushButton("下载辅助模型")
        self._card_dl_btn.setAccessibleName("下载会议辅助模型")
        self._card_dl_btn.clicked.connect(self._download_aux_models)
        self._card_dl_btn.hide()
        dl_row.addWidget(self._card_dl_btn, 0)
        dl_row.addStretch(1)
        lay.addLayout(dl_row)
        lay.addStretch(1)

        actions = QHBoxLayout()
        actions.addStretch(1)
        self._card_cancel = QPushButton("取消")
        self._card_cancel.clicked.connect(self._leave_start_card)
        actions.addWidget(self._card_cancel)
        self._card_start = QPushButton("开始")
        self._card_start.setAccessibleName("开始会议记录")
        self._card_start.setDefault(True)
        self._card_start.clicked.connect(self._start_from_card)
        actions.addWidget(self._card_start)
        page_lay.addLayout(actions)
        self._opt_source.currentIndexChanged.connect(self._update_card_notes)
        self._opt_model.currentIndexChanged.connect(self._update_card_notes)
        return page

    def _build_live(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        head = QHBoxLayout()
        head.setSpacing(12)
        self._live_title = QLabel("会议进行中")
        self._live_title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        head.addWidget(self._live_title, 1)
        self._live_rec_dot = QLabel()
        self._live_rec_dot.setFixedSize(8, 8)
        self._live_rec_dot.setAccessibleName("正在记录")
        head.addWidget(self._live_rec_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        self._live_clock = QLabel("00:00:00")
        self._live_clock.setObjectName("meetingLiveClock")
        self._live_clock.setAccessibleName("会议已进行时间")
        head.addWidget(self._live_clock, 0)
        self._pause_btn = QPushButton("暂停采集")
        self._pause_btn.setAccessibleName("暂停会议采集")
        self._pause_btn.setToolTip("暂停期间不采集新音频；已录内容继续识别，会议记录保留。")
        self._pause_btn.clicked.connect(self.pause_requested.emit)
        head.addWidget(self._pause_btn, 0)
        self._resume_btn = QPushButton("继续采集")
        self._resume_btn.setAccessibleName("继续会议采集")
        self._resume_btn.setToolTip("继续采集新的会议音频。")
        self._resume_btn.clicked.connect(self.resume_requested.emit)
        self._resume_btn.hide()
        head.addWidget(self._resume_btn, 0)
        self._end_btn = QPushButton("结束会议")
        self._end_btn.setAccessibleName("结束会议")
        self._end_btn.clicked.connect(self._confirm_end)
        head.addWidget(self._end_btn, 0)
        lay.addLayout(head)

        self._system_silent_banner = QFrame()
        self._system_silent_banner.setObjectName("meetingSilentBanner")
        banner_lay = QHBoxLayout(self._system_silent_banner)
        banner_lay.setContentsMargins(10, 6, 8, 6)
        banner_lay.setSpacing(8)
        self._system_silent_label = QLabel()
        self._system_silent_label.setWordWrap(True)
        self._system_silent_label.setAccessibleName("电脑声静音提醒")
        banner_lay.addWidget(self._system_silent_label, 1)
        self._system_silent_dismiss_btn = QPushButton("知道了")
        self._system_silent_dismiss_btn.setAccessibleName("关闭电脑声静音提醒")
        self._system_silent_dismiss_btn.clicked.connect(self._dismiss_system_silent_warning)
        banner_lay.addWidget(self._system_silent_dismiss_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        self._system_silent_banner.hide()
        lay.addWidget(self._system_silent_banner)

        self._live_meters_bar = QWidget()
        self._live_meters_bar.setObjectName("meetingLiveMeters")
        meters_lay = QHBoxLayout(self._live_meters_bar)
        meters_lay.setContentsMargins(0, 0, 0, 0)
        meters_lay.setSpacing(8)
        self._live_mic_label = QLabel("麦克风")
        meters_lay.addWidget(self._live_mic_label, 0)
        self._live_mic_meter = _LevelMeterBar("麦克风实时电平")
        meters_lay.addWidget(self._live_mic_meter, 1)
        meters_lay.addSpacing(8)
        self._live_sys_label = QLabel("电脑声（对方）")
        meters_lay.addWidget(self._live_sys_label, 0)
        self._live_sys_meter = _LevelMeterBar("电脑声（对方）实时电平")
        meters_lay.addWidget(self._live_sys_meter, 1)
        lay.addWidget(self._live_meters_bar)

        self._live_status = QLabel("")
        self._live_status.setWordWrap(True)
        self._live_status.hide()
        lay.addWidget(self._live_status)

        speaker_tools = PairedToolBar(spacing=8)
        self._live_speaker_hint = QLabel("点击姓名可命名；纠正发言人只影响选中的段落，人工修改会保留。")
        self._live_speaker_hint.setWordWrap(True)
        self._live_speaker_btn = QPushButton("命名说话人")
        self._live_speaker_btn.setAccessibleName("会议记录中命名说话人")
        self._live_speaker_menu = QMenu(self._live_speaker_btn)
        self._live_speaker_menu.aboutToShow.connect(self._populate_live_speaker_menu)
        self._live_speaker_btn.setMenu(self._live_speaker_menu)
        speaker_tools.add_right(self._live_speaker_btn)
        self._live_correct_speaker_btn = QPushButton("纠正发言人")
        self._live_correct_speaker_btn.setCheckable(True)
        self._live_correct_speaker_btn.toggled.connect(self._toggle_live_speaker_correction)
        speaker_tools.add_right(self._live_correct_speaker_btn)
        lay.addWidget(speaker_tools)
        lay.addWidget(self._live_speaker_hint)
        self._live_speaker_feedback = QLabel()
        self._live_speaker_feedback.setWordWrap(True)
        self._live_speaker_feedback.hide()
        lay.addWidget(self._live_speaker_feedback)

        self._live_find_bar = QWidget()
        find_lay = QHBoxLayout(self._live_find_bar)
        find_lay.setContentsMargins(0, 0, 0, 0)
        find_lay.setSpacing(6)
        self._live_find_edit = QLineEdit()
        self._live_find_edit.setPlaceholderText("查找本场文字稿；Enter 下一处，Shift+Enter 上一处")
        self._live_find_edit.setAccessibleName("查找本场文字稿")
        self._live_find_edit.setClearButtonEnabled(True)
        self._live_find_edit.textChanged.connect(lambda: self._find_live_text(restart=True))
        self._live_find_edit.returnPressed.connect(self._find_live_text)
        self._live_find_edit.installEventFilter(self)
        find_lay.addWidget(self._live_find_edit, 1)
        self._live_find_status = QLabel()
        find_lay.addWidget(self._live_find_status)
        self._live_find_prev_btn = QPushButton("上一处")
        self._live_find_prev_btn.setAccessibleName("查找上一处")
        self._live_find_prev_btn.clicked.connect(lambda: self._find_live_text(backward=True))
        find_lay.addWidget(self._live_find_prev_btn)
        self._live_find_next_btn = QPushButton("下一处")
        self._live_find_next_btn.setAccessibleName("查找下一处")
        self._live_find_next_btn.clicked.connect(lambda: self._find_live_text(backward=False))
        find_lay.addWidget(self._live_find_next_btn)
        self._live_find_close_btn = QPushButton("关闭查找")
        self._live_find_close_btn.clicked.connect(self._close_live_find)
        find_lay.addWidget(self._live_find_close_btn)
        self._live_find_bar.hide()
        lay.addWidget(self._live_find_bar)

        self._live_split = QSplitter(Qt.Orientation.Horizontal)
        self._live_split.setChildrenCollapsible(False)
        self._live_transcript = _TranscriptView()
        self._live_transcript.setOpenLinks(False)
        self._live_transcript.anchorClicked.connect(self._on_live_transcript_link)
        self._live_transcript.setAccessibleName("实时文字稿")
        self._live_transcript.setPlaceholderText("开始说话后，这里会按说话人逐条出现文字…")
        self._live_transcript.viewport().installEventFilter(self)
        self._live_transcript.verticalScrollBar().valueChanged.connect(self._on_live_scroll_changed)
        self._live_new_msg_btn = QPushButton("↓ 底部有新发言", self._live_transcript.viewport())
        self._live_new_msg_btn.setAccessibleName("跳到底部查看新发言")
        self._live_new_msg_btn.clicked.connect(self._scroll_live_to_bottom)
        self._live_new_msg_btn.hide()
        self._live_split.addWidget(self._live_transcript)
        side = QWidget()
        side_lay = QVBoxLayout(side)
        side_lay.setContentsMargins(12, 0, 0, 0)
        side_lay.setSpacing(8)
        self._marks_title = QLabel("标记")
        side_lay.addWidget(self._marks_title)
        mark_row = QHBoxLayout()
        mark_row.setSpacing(6)
        self._mark_note = QLineEdit()
        self._mark_note.setPlaceholderText("备注（可空）")
        self._mark_note.setAccessibleName("标记备注")
        self._mark_note.returnPressed.connect(self._add_mark)
        mark_row.addWidget(self._mark_note, 1)
        self._mark_btn = QPushButton("标记此刻")
        self._mark_btn.setAccessibleName("标记此刻")
        self._mark_btn.clicked.connect(self._add_mark)
        mark_row.addWidget(self._mark_btn)
        side_lay.addLayout(mark_row)
        self._marks_view = QTextBrowser()
        self._marks_view.setAccessibleName("标记列表")
        self._marks_view.setPlaceholderText("")
        self._marks_view.setMaximumHeight(160)
        side_lay.addWidget(self._marks_view)
        self._notes_title = QLabel("我的笔记")
        side_lay.addWidget(self._notes_title)
        self._user_notes = QPlainTextEdit()
        self._user_notes.setAccessibleName("我的笔记")
        self._user_notes.setPlaceholderText("随手记；生成纪要时会一并参考。")
        self._user_notes_timer = QTimer(self)
        self._user_notes_timer.setSingleShot(True)
        self._user_notes_timer.setInterval(800)
        self._user_notes_timer.timeout.connect(self._save_user_notes)
        self._user_notes.textChanged.connect(lambda: self._user_notes_timer.start())
        side_lay.addWidget(self._user_notes, 1)
        self._live_side_scroll = QScrollArea()
        self._live_side_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._live_side_scroll.setWidgetResizable(True)
        self._live_side_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._live_side_scroll.setMinimumWidth(240)
        self._live_side_scroll.setWidget(side)
        self._live_split.addWidget(self._live_side_scroll)
        self._live_split.setStretchFactor(0, 3)
        self._live_split.setStretchFactor(1, 2)
        lay.addWidget(self._live_split, 1)

        actions = QHBoxLayout()
        self._live_caps = QLabel("")
        self._live_caps.setWordWrap(True)
        actions.addWidget(self._live_caps, 1)
        self._live_dl_btn = QPushButton("下载辅助模型")
        self._live_dl_btn.setAccessibleName("下载会议辅助模型（进行中）")
        self._live_dl_btn.clicked.connect(self._download_aux_models)
        self._live_dl_btn.hide()
        actions.addWidget(self._live_dl_btn)
        lay.addLayout(actions)
        return page

    # ── theme ─────────────────────────────────────────────────────

    def reapply_theme(self) -> None:
        from sayink.ui import design_tokens as tok
        from sayink.ui import settings_styles as ss

        self.setStyleSheet(ss.WINDOW_CSS)
        title_css = (
            f"font-family: {tok.FONT_DISPLAY}; font-size: {tok.TYPE_TITLE_LG}px; font-weight: 700;"
            f" background: transparent; color: {tok.TEXT};"
        )
        from sayink.ui.settings_components import PAGE_TITLE

        self._title_label.setStyleSheet(PAGE_TITLE)
        self._card_title.setStyleSheet(title_css)
        self._live_title.setStyleSheet(
            f"font-size: {tok.TYPE_TITLE}px; font-weight: 700; color: {tok.TEXT}; background: transparent;"
        )
        self._paint_live_clock()
        dim = f"color: {tok.TEXT_SEC}; font-size: {tok.TYPE_FOOTNOTE}px; background: transparent;"
        for label in (self._list_summary, self._detail_meta, self._card_hint, self._live_caps,
                      self._card_notes, self._empty_label, self._template_label, self._content_hint,
                      self._notes_empty, self._live_speaker_hint, self._live_speaker_feedback,
                      self._materials_agenda, self._opt_loopback_hint, self._live_mic_label,
                      self._live_sys_label, self._live_find_status, self._detail_find_status):
            label.setStyleSheet(dim)
        self._opt_save_audio.setStyleSheet(
            f"QCheckBox {{ color: {tok.TEXT}; font-size: {tok.TYPE_BODY_SM}px; background: transparent; spacing: 8px; }}"
        )
        for label in (self._marks_title, self._notes_title, self._materials_notes_label, self._materials_marks_label):
            label.setStyleSheet(
                f"color: {tok.TEXT}; font-size: {tok.TYPE_FOOTNOTE}px; font-weight: 700; background: transparent;"
            )
        status_css = (
            f"color: {tok.TEXT}; font-size: {tok.TYPE_FOOTNOTE}px; background: {tok.SURFACE_PEARL};"
            f" border: 1px solid {tok.HAIRLINE}; border-radius: {tok.RADIUS_MD}px; padding: 6px 10px;"
        )
        self._status_label.setStyleSheet(status_css)
        self._import_progress.setStyleSheet(status_css)
        self._live_status.setStyleSheet(status_css)
        self._import_progress_bar.setStyleSheet(
            f"QProgressBar {{ background: {tok.SURFACE_PEARL}; border: 1px solid {tok.HAIRLINE};"
            f" border-radius: 5px; }}"
            f"QProgressBar::chunk {{ background: {tok.ACCENT}; border-radius: 4px; }}"
        )
        is_dark = QColor(tok.BG).lightness() < 128
        warn_bg = "#3A2E12" if is_dark else "#FFFBEB"
        warn_fg = "#FDE68A" if is_dark else "#78350F"
        warn_border = "#D97706"
        self._system_silent_banner.setStyleSheet(
            f"QFrame#meetingSilentBanner {{ background: {warn_bg}; border: 1px solid {warn_border};"
            f" border-radius: {tok.RADIUS_MD}px; }}"
        )
        self._system_silent_label.setStyleSheet(
            f"color: {warn_fg}; font-size: {tok.TYPE_FOOTNOTE}px; font-weight: 600; background: transparent;"
        )
        self._audio_player.reapply_theme()
        edit_css = f"""
            QLineEdit {{
                background: {tok.INPUT_BG}; color: {tok.TEXT};
                border: 1px solid {tok.CONTROL_BORDER}; border-radius: {tok.RADIUS_MD}px;
                padding: 6px 12px; min-height: {tok.CONTROL_FIELD_HEIGHT - 14}px; font-size: {tok.TYPE_BODY_SM}px;
            }}
            QLineEdit:focus {{ border: 2px solid {tok.ACCENT}; padding: 5px 11px; }}
        """
        self._search_edit.setStyleSheet(ss.build_search_field_css())
        for edit in (self._opt_title, self._opt_participants, self._mark_note, self._live_find_edit, self._detail_find_edit):
            edit.setStyleSheet(edit_css)
        # Reads as a heading; the frame only appears once the user goes to edit it.
        self._detail_title.setStyleSheet(f"""
            QLineEdit {{
                background: transparent; color: {tok.TEXT};
                border: 1px solid transparent; border-radius: {tok.RADIUS_MD}px;
                padding: 4px 4px; min-height: 32px;
                font-size: {tok.TYPE_HERO}px; font-weight: 700;
            }}
            QLineEdit:hover {{ border: 1px solid {tok.HAIRLINE}; }}
            QLineEdit:focus {{ background: {tok.SURFACE_PEARL}; border: 2px solid {tok.ACCENT}; padding: 3px 3px; }}
        """)
        self._paint_notes_state()
        self._template_combo.setMinimumHeight(tok.CONTROL_BTN_SM_HEIGHT)
        from sayink.ui.tray_icon import _menu_font, _menu_stylesheet

        for menu in (self._more_menu, self._speaker_menu, self._export_menu, self._live_speaker_menu):
            menu.setFont(_menu_font())
            menu.setStyleSheet(_menu_stylesheet())
        text_css = f"""
            QTextBrowser, QPlainTextEdit {{
                background: {tok.SURFACE_PEARL}; color: {tok.TEXT};
                border: 1px solid {tok.HAIRLINE}; border-radius: {tok.RADIUS_MD}px;
                padding: 8px; font-size: {tok.TYPE_BODY_SM}px;
                selection-background-color: {tok.ACCENT_SOFT}; selection-color: {tok.TEXT};
            }}
            QPlainTextEdit:focus {{ border: 2px solid {tok.ACCENT}; }}
        """
        for view in (self._notes_edit, self._live_transcript, self._marks_view, self._user_notes,
                     self._opt_agenda, self._record_notes, self._record_marks):
            view.setStyleSheet(text_css)
        self._notes_edit.setStyleSheet(text_css + f"QPlainTextEdit {{ font-size: {tok.READING_FONT_PX}px; }}")
        # Reading a saved meeting matches 历史: text sits on the page, and only
        # the editor gets a field border, so "can I type here" is visible.
        reading_css = f"""
            QTextBrowser {{
                background: transparent; color: {tok.TEXT}; border: none; padding: 4px 0;
                font-size: {tok.READING_FONT_PX}px;
                selection-background-color: {tok.ACCENT_SOFT}; selection-color: {tok.TEXT};
            }}
        """
        for view in (self._notes_view, self._transcript_view):
            view.setStyleSheet(reading_css)
            view.document().setDocumentMargin(0)
        self._list.setStyleSheet(f"""
            QListWidget {{ background: transparent; border: 2px solid transparent; outline: none;
                           color: {tok.TEXT}; font-size: {tok.TYPE_BODY_SM}px; }}
            {session_list_item_css()}
            QLabel#meetingRowTitle {{ color: {tok.TEXT}; font-size: {tok.TYPE_BODY_SM}px; background: transparent; }}
            QLabel#meetingRowMeta {{ color: {tok.TEXT_SEC}; font-size: {tok.TYPE_CAPTION}px;
                                     background: transparent; }}
        """)
        for combo in (self._opt_source, self._opt_model, self._opt_language, self._template_combo):
            combo.setStyleSheet("")  # Inherit the shared form and popup styles.
        # Match the small action buttons so 纪要 and 文字稿 share one tool-row height.
        self._template_combo.setStyleSheet(ss.build_toolbar_combo_css())
        for btn in (self._start_btn, self._card_start, self._pause_btn, self._resume_btn, self._live_new_msg_btn):
            btn.setStyleSheet(ss.BTN_ACCENT_SM)
        for btn in (self._generate_btn, self._edit_btn, self._cancel_notes_edit_btn, self._copy_btn,
                    self._transcript_copy_btn, self._export_btn, self._card_cancel,
                    self._cancel_btn, self._correct_speaker_btn, self._return_notes_btn, self._clear_search_btn,
                    self._mark_btn, self._live_find_prev_btn, self._live_find_next_btn, self._live_find_close_btn,
                    self._detail_find_prev_btn, self._detail_find_next_btn, self._detail_find_close_btn,
                    self._system_silent_dismiss_btn,
                    self._manage_templates_btn, self._live_correct_speaker_btn,
                    self._card_dl_btn, self._live_dl_btn, self._load_more_btn, self._materials_toggle,
                    self._selection_mode_btn, self._batch_all_btn, self._batch_export_btn, self._batch_generate_btn,
                    self._import_btn, self._media_btn, self._media_cancel_btn, self._media_retry_btn):
            btn.setStyleSheet(ss.BTN_GHOST_SM)
        self._batch_delete_btn.setStyleSheet(ss.BTN_DANGER_SM)
        for btn in (
            self._selection_mode_btn, self._batch_all_btn, self._batch_export_btn,
            self._batch_generate_btn, self._batch_delete_btn,
        ):
            fit_action_button(btn)
        # Match 导出's real box (style min-height plus its border) so the tab
        # capsule and the button share one line. Both labels use the same weight.
        tab_h = max(self._export_btn.sizeHint().height(), tok.CONTROL_BTN_SM_HEIGHT)
        chip_h = max(20, tab_h - 8)
        self._tab_bar.setStyleSheet(f"""
            QWidget#meetingTabs {{ background: {tok.SURFACE_PEARL}; border: 1px solid {tok.HAIRLINE};
                                   border-radius: {tok.RADIUS_MD}px; }}
            QPushButton#meetingTab {{ background: transparent; color: {tok.TEXT_SEC};
                                      border: 1px solid transparent; border-radius: {tok.RADIUS_SM}px;
                                      padding: 0 16px; min-height: {chip_h}px; max-height: {chip_h}px;
                                      font-size: {tok.TYPE_BODY_SM}px; font-weight: 400; }}
            QPushButton#meetingTab:hover:!checked {{ color: {tok.TEXT}; background: {tok.ROW_HOVER}; }}
            QPushButton#meetingTab:checked {{ background: {tok.SECONDARY_CONTAINER}; color: {tok.TEXT};
                                              border-color: transparent; font-weight: 400; }}
            QPushButton#meetingTab:focus {{ border-color: {tok.ACCENT_FOCUS}; }}
        """)
        self._tab_bar.setFixedHeight(tab_h)
        no_indicator = "QPushButton::menu-indicator { image: none; width: 0px; }"
        self._more_btn.setStyleSheet(ss.BTN_GHOST_SM + no_indicator)
        self._template_menu_btn.setStyleSheet(ss.BTN_GHOST_SM + no_indicator)
        self._speaker_btn.setStyleSheet(ss.BTN_GHOST_SM + no_indicator)
        self._live_speaker_btn.setStyleSheet(ss.BTN_GHOST_SM + no_indicator)
        self._end_btn.setStyleSheet(ss.BTN_DANGER_SOLID_SM)
        for splitter in (self._splitter, self._live_split):
            splitter.setStyleSheet(
                "QSplitter::handle { background: transparent; }"
                f"QSplitter::handle:hover {{ background: {tok.ACCENT_FOCUS}; }}"
            )
        if self._detail_id:
            self._render_detail()
        if hasattr(self, "_list"):
            self._sync_batch_checks()
        self._sync_media_buttons()

    # ── list / browse ─────────────────────────────────────────────

    def _custom_templates(self) -> list[dict]:
        custom = self._config.get("meeting.custom_templates", []) or []
        return [t for t in custom if isinstance(t, dict)]

    def _reload_templates(self) -> None:
        from sayink.ui.minutes_templates import populate_template_combo

        populate_template_combo(self._template_combo, self._config, self._template_combo.currentData())
        self._rebuild_template_menu()

    def _rebuild_template_menu(self) -> None:
        menu = self._template_menu
        menu.clear()
        current = self._template_combo.currentData()
        for index in range(self._template_combo.count()):
            action = menu.addAction(self._template_combo.itemText(index))
            action.setCheckable(True)
            action.setChecked(self._template_combo.itemData(index) == current)
            tip = self._template_combo.itemData(index, Qt.ItemDataRole.ToolTipRole)
            if tip:
                action.setToolTip(str(tip))
            action.triggered.connect(lambda _checked=False, idx=index: self._select_template(idx))
        menu.addSeparator()
        manage = menu.addAction("管理模板")
        manage.triggered.connect(self._manage_templates)
        self._sync_template_menu_tip()

    def _sync_template_menu(self) -> None:
        if not hasattr(self, "_template_menu"):
            return
        names = [self._template_combo.itemText(i) for i in range(self._template_combo.count())]
        actions = [action for action in self._template_menu.actions() if not action.isSeparator() and action.text() != "管理模板"]
        if [action.text() for action in actions] != names:
            self._rebuild_template_menu()
            return
        current = self._template_combo.currentData()
        for index, action in enumerate(actions):
            action.setChecked(self._template_combo.itemData(index) == current)
        self._sync_template_menu_tip()

    def _sync_template_menu_tip(self) -> None:
        text = self._template_combo.currentText()
        self._template_menu_btn.setToolTip(f"当前模板：{text}" if text else "选择纪要模板")

    def _select_template(self, index: int) -> None:
        if self._template_combo.currentIndex() != index:
            self._template_combo.setCurrentIndex(index)
        else:
            self._sync_template_menu()

    def _fit_template_cluster(self) -> None:
        """Keep post-meeting actions on one row when the window is narrow.

        The label, combo and 管理模板 stay inline when they fit. Otherwise they
        collapse into the 模板 menu so the notes body does not lose a row.
        """
        if self._fitting_templates or not hasattr(self, "_notes_bar"):
            return
        bar = self._notes_bar
        width = bar.width()
        if width <= 0:
            return
        full = (self._template_label, self._template_combo, self._manage_templates_btn)
        actions = [
            widget
            for widget in (self._generate_btn, self._edit_btn, self._cancel_notes_edit_btn, self._copy_btn)
            if not widget.isHidden()
        ]
        gap = bar._spacing if actions else 0
        compact = bar._span(list(full)) + gap + bar._span(actions) > width
        if compact == self._template_menu_btn.isVisible() and all(widget.isVisible() != compact for widget in full):
            return
        self._fitting_templates = True
        try:
            for widget in full:
                widget.setVisible(not compact)
            self._template_menu_btn.setVisible(compact)
        finally:
            self._fitting_templates = False

    def _manage_templates(self) -> None:
        from sayink.ui.minutes_templates import MinutesTemplatesDialog

        dialog = MinutesTemplatesDialog(self._config, self._template_combo.currentData(), self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._reload_templates()
            self._template_combo.setCurrentIndex(self._template_combo.findData(dialog.selected_template_id))
            self._on_template_changed(self._template_combo.currentIndex())

    def refresh(self) -> None:
        self._reload_templates()
        if self._stack.currentIndex() == PAGE_START:
            self._sync_follow_source_label()
        if self._store.disabled:
            self._list_summary.setText("历史数据库不可用，会议记录无法保存")
            return
        keep = self._detail_id
        batch_selected = set(self._selected_meeting_ids()) if self._batch_mode else set()
        has_more_search = False
        if self._search_query:
            matched = self._store.search_meetings(self._search_query, limit=self._list_limit + 1)
            has_more_search = len(matched) > self._list_limit
            records = matched[:self._list_limit]
        else:
            records = self._store.list_meetings(limit=self._list_limit)
        if (keep and not self._search_query and records
                and all(record.id != keep for record in records)
                and self._store.get_meeting(keep) is not None):
            # A meeting opened past the first page stays selected when newer ones arrive.
            total = self._store.count_meetings()
            if total > self._list_limit:
                self._list_limit = total
                records = self._store.list_meetings(limit=self._list_limit)
        records = self._drafts.apply_optimistic_titles(records)
        self._records = {r.id: r for r in records}
        minutes = self._minutes_markers()
        rows_key = (
            tuple((record.id, record.title, record.status, record.source, record.started_at, record.ended_at) for record in records),
            tok.FONT, tok.TEXT, tok.TEXT_SEC, tok.TEXT_DIM,
        )
        self._list.blockSignals(True)
        try:
            if getattr(self, "_meeting_rows_key", None) != rows_key:
                self._release_row_widgets()
                for record in records:
                    item = QListWidgetItem()
                    item.setData(Qt.ItemDataRole.UserRole, record.id)
                    item.setData(Qt.ItemDataRole.AccessibleTextRole, _meeting_row_text(record, minutes.get(record.id, "")))
                    row = self._build_list_row(record, minutes.get(record.id, ""))
                    row.setToolTip(record.display_title)
                    self._list.addItem(item)
                    self._list.setItemWidget(item, row)
                    row.ensurePolished()
                    for label in row.findChildren(QLabel):
                        label.ensurePolished()
                    item.setSizeHint(row.sizeHint())
                self._meeting_rows_key = rows_key
            self._sync_row_minutes(records, minutes)
            if self._batch_mode:
                for index in range(self._list.count()):
                    item = self._list.item(index)
                    item.setSelected(str(item.data(Qt.ItemDataRole.UserRole) or "") in batch_selected)
            elif keep in self._records:
                for index in range(self._list.count()):
                    item = self._list.item(index)
                    if item.data(Qt.ItemDataRole.UserRole) == keep:
                        item.setSelected(True)
                        self._list.setCurrentItem(item)
                        break
            else:
                self._list.setCurrentItem(None)
        finally:
            self._list.blockSignals(False)
        count = len(records)
        if self._search_query:
            self._list_summary.setText(f"找到 {count}{'+' if has_more_search else ''} 场会议")
            self._load_more_btn.setVisible(has_more_search)
        else:
            total = self._store.count_meetings()
            if count < total:
                self._list_summary.setText(f"最近 {count} 场，共 {total} 场会议")
            else:
                self._list_summary.setText(f"共 {total} 场会议")
            self._load_more_btn.setVisible(count < total and count > 0)
        if self._batch_mode:
            self._sync_batch_summary()
        self._empty_label.setText(
            "未找到匹配的会议。\n试试其他关键词，或清除搜索查看全部会议。" if self._search_query else
            _EMPTY_MEETINGS
        )
        self._empty_label.setVisible(count == 0)
        self._list.setVisible(count > 0)
        self._selection_mode_btn.setEnabled(count > 0 or self._batch_mode)
        self._clear_search_btn.setVisible(count == 0 and bool(self._search_query))
        self._sync_batch_checks()
        if self._batch_mode:
            if not records:
                if not self._flush_bound_title(sync=True) or not self._save_notes_draft() or not self._save_record_notes():
                    return
                self._detail_id = ""
                self._clear_detail()
            elif keep and keep in self._records:
                self._render_detail()
            elif records:
                self._detail_id = records[0].id
                self._render_detail()
        elif keep and keep in self._records:
            self._render_detail()
        elif records:
            self._list.setCurrentRow(0)
        elif not records and not getattr(self._ctrl, "importing_media", False):
            if not self._flush_bound_title(sync=True) or not self._save_notes_draft() or not self._save_record_notes():
                return
            self._detail_id = ""
            self._clear_detail()
        importing = bool(getattr(self._ctrl, "importing_media", False))
        if importing:
            self._show_import_detail()
        self._detail_pane.setVisible(
            bool(records) or self._editing_notes or importing or self._status_label.isVisible()
        )

    def _release_row_widgets(self) -> None:
        """Drop row widgets before ``clear()``.

        A visible row widget that loses its parent becomes a top-level window
        for one frame and shows up as a flickering application icon.
        """
        release_list_item_widgets(self._list, self)

    def _sync_row_minutes(self, records: list[MeetingRecord], minutes: dict[str, str]) -> None:
        """Queue and generating labels change often. Update them without rebuilding rows."""
        by_id = {record.id: record for record in records}
        for index in range(self._list.count()):
            item = self._list.item(index)
            record = by_id.get(str(item.data(Qt.ItemDataRole.UserRole) or ""))
            widget = self._list.itemWidget(item)
            if record is None or widget is None:
                continue
            marker = minutes.get(record.id, "")
            text = _meeting_row_meta(record, marker)
            meta = widget.findChild(_ElidedLabel, "meetingRowMeta")
            if meta is not None and meta._full_text != " ".join(text.split()):
                meta.set_full_text(text)
            accessible = _meeting_row_text(record, marker)
            if item.data(Qt.ItemDataRole.AccessibleTextRole) != accessible:
                item.setData(Qt.ItemDataRole.AccessibleTextRole, accessible)

    def _minutes_markers(self) -> dict[str, str]:
        """Meeting id → minutes job state shown in its list row."""
        markers = {job.meeting_id: "纪要生成失败" if job.failed else "纪要排队中"
                   for job in self._store.generation_queue()}
        self._listed_generating = self._ctrl.generating_for
        if self._listed_generating:
            markers[self._listed_generating] = "纪要生成中"
        return markers

    def _build_list_row(self, record: MeetingRecord, minutes: str = "") -> QWidget:
        row = _MeetingStreamRow()
        row.setObjectName("meetingStreamRow")
        row.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        outer = QHBoxLayout(row)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(8)
        box = _SessionCheckBox()
        box.setFixedSize(24, 32)
        box.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        box.setObjectName("meetingBatchCheck")
        box.setAccessibleName(f"选择会议：{record.display_title}")
        box.setVisible(self._batch_mode)
        box.clicked.connect(lambda checked, meeting_id=record.id: self._toggle_meeting_selection(meeting_id, checked))
        outer.addWidget(box, 0, Qt.AlignmentFlag.AlignVCenter)
        lay = QVBoxLayout()
        lay.setSpacing(2)
        title = _ElidedLabel(record.display_title)
        title.setObjectName("meetingRowTitle")
        lay.addWidget(title)
        meta = _ElidedLabel(_meeting_row_meta(record, minutes))
        meta.setObjectName("meetingRowMeta")
        lay.addWidget(meta)
        outer.addLayout(lay, 1)
        _paint_meeting_row(row, selected=False)
        return row

    def _toggle_meeting_selection(self, meeting_id: str, checked: bool) -> None:
        if not self._batch_mode:
            self._set_batch_mode(True)
        for index in range(self._list.count()):
            item = self._list.item(index)
            if str(item.data(Qt.ItemDataRole.UserRole) or "") == meeting_id:
                item.setSelected(bool(checked))
                break

    def _perform_search(self) -> None:
        if not self._flush_bound_title(sync=True) or not self._save_notes_draft() or not self._save_record_notes():
            self._search_edit.blockSignals(True)
            self._search_edit.setText(self._search_query)
            self._search_edit.blockSignals(False)
            return
        self._search_query = self._search_edit.text().strip()
        self.refresh()

    def _active_detail_search_view(self):
        if self._content_stack.currentIndex() == 1:
            return self._transcript_view
        return self._notes_edit if self._editing_notes else self._notes_view

    def open_detail_find(self) -> None:
        if not self._detail_id or self._batch_mode or getattr(self._ctrl, "importing_media", False):
            return
        self._detail_find_bar.show()
        self._detail_find_edit.setFocus()
        self._detail_find_edit.selectAll()
        if self._detail_find_edit.text().strip():
            self._find_detail_text(restart=True)

    def _close_detail_find(self) -> None:
        self._detail_find_bar.hide()
        for view in (self._notes_view, self._notes_edit, self._transcript_view):
            clear_reading_search(view)
        target = self._active_detail_search_view()
        if target is not None:
            target.setFocus()

    def _find_detail_text(self, *, backward: bool = False, restart: bool = False) -> None:
        query = self._detail_find_edit.text()
        view = self._active_detail_search_view()
        for other in (self._notes_view, self._notes_edit, self._transcript_view):
            if other is not view:
                clear_reading_search(other)
        if not query.strip():
            clear_reading_search(view)
            self._detail_find_status.setText("")
            return
        if restart and view is not None:
            cur = view.textCursor()
            cur.movePosition(QTextCursor.MoveOperation.Start)
            view.setTextCursor(cur)
        idx, total = find_in_reading_view(view, query, backward=backward, step=not restart)
        self._detail_find_status.setText("未找到" if total == 0 else f"{idx} / {total}")

    def focus_search(self) -> None:
        if self._stack.currentIndex() == PAGE_LIVE:
            self._live_find_bar.show()
            self._live_find_edit.setFocus()
            self._live_find_edit.selectAll()
        elif self._stack.currentIndex() == PAGE_START:
            self._opt_title.setFocus()
        elif (
            self._detail_id
            and not self._batch_mode
            and not getattr(self._ctrl, "importing_media", False)
            and not self._search_edit.hasFocus()
        ):
            self.open_detail_find()
        else:
            self._search_edit.setFocus()
            self._search_edit.selectAll()

    def _close_live_find(self) -> None:
        self._live_find_bar.hide()
        clear_reading_search(self._live_transcript)
        self._live_transcript.setFocus()

    def _find_live_text(self, *, backward: bool = False, restart: bool = False) -> None:
        query = self._live_find_edit.text()
        view = self._live_transcript
        if not query.strip():
            clear_reading_search(view)
            self._live_find_status.setText("")
            return
        if restart:
            cursor = view.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            view.setTextCursor(cursor)
        idx, total = find_in_reading_view(view, query, backward=backward, step=not restart)
        self._live_find_status.setText("未找到" if total == 0 else f"{idx} / {total}")

    def meeting_count(self) -> int:
        return self._list.count()

    def _on_selection_changed(self) -> None:
        if getattr(self._ctrl, "importing_media", False):
            return
        ids = self._selected_meeting_ids()
        if len(ids) > 1 and not self._batch_mode:
            self._set_batch_mode(True)
            return
        self._sync_batch_checks()
        if self._batch_mode:
            self._sync_batch_checks()
            self._sync_batch_summary()
            self._sync_batch_actions()
            return
        items = self._list.selectedItems()
        if not items:
            return
        meeting_id = str(items[0].data(Qt.ItemDataRole.UserRole) or "")
        if meeting_id != self._detail_id:
            if not self._flush_bound_title(sync=True):
                self._list.blockSignals(True)
                for index in range(self._list.count()):
                    item = self._list.item(index)
                    if item.data(Qt.ItemDataRole.UserRole) == self._detail_id:
                        self._list.setCurrentItem(item)
                        break
                self._list.blockSignals(False)
                return
            if not self._save_notes_draft():
                self._list.blockSignals(True)
                for index in range(self._list.count()):
                    item = self._list.item(index)
                    if item.data(Qt.ItemDataRole.UserRole) == self._detail_id:
                        self._list.setCurrentItem(item)
                        break
                self._list.blockSignals(False)
                return
            if not self._save_record_notes():
                self._list.blockSignals(True)
                for index in range(self._list.count()):
                    item = self._list.item(index)
                    if item.data(Qt.ItemDataRole.UserRole) == self._detail_id:
                        self._list.setCurrentItem(item)
                        break
                self._list.blockSignals(False)
                return
            self._audio_player.stop()
            self._materials_toggle.setChecked(False)
            self._transcript_highlight = -1
            self._citation_return = None
            self._correcting_speakers = False
            self._status_label.hide()
            self._detail_id = meeting_id
            self._notes_view.restore_reading_position(0)
            self._transcript_view.restore_reading_position(0)
            self._render_detail()
            if self._detail_find_bar.isVisible() and self._detail_find_edit.text().strip():
                self._find_detail_text(restart=True)

    def _selected_meeting_ids(self) -> list[str]:
        selected = {str(item.data(Qt.ItemDataRole.UserRole) or "") for item in self._list.selectedItems()}
        ordered = []
        for index in range(self._list.count()):
            meeting_id = str(self._list.item(index).data(Qt.ItemDataRole.UserRole) or "")
            if meeting_id and meeting_id in selected:
                ordered.append(meeting_id)
        return ordered

    def _set_batch_mode(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled and not self._batch_mode:
            if not self._flush_bound_title(sync=True) or not self._save_notes_draft() or not self._save_record_notes():
                self._selection_mode_btn.blockSignals(True)
                self._selection_mode_btn.setChecked(False)
                self._selection_mode_btn.blockSignals(False)
                return
            self._audio_player.stop()
        if enabled == self._batch_mode and self._list.batch_mode == enabled:
            self._sync_batch_actions()
            return
        self._batch_mode = enabled
        self._list.batch_mode = enabled
        # A status line belongs to the view that produced it: single-meeting
        # notices make no sense over the batch tools, and vice versa.
        self._set_status("")
        self._selection_mode_btn.blockSignals(True)
        self._selection_mode_btn.setChecked(enabled)
        self._selection_mode_btn.blockSignals(False)
        self._selection_mode_btn.setText("完成" if enabled else "选择")
        self._selection_mode_btn.setAccessibleName("完成选择" if enabled else "选择会议")
        fit_action_button(self._selection_mode_btn)
        if not enabled:
            self._list.blockSignals(True)
            try:
                self._list.clearSelection()
                for index in range(self._list.count()):
                    item = self._list.item(index)
                    if item.data(Qt.ItemDataRole.UserRole) == self._detail_id:
                        item.setSelected(True)
                        self._list.setCurrentItem(item)
                        break
            finally:
                self._list.blockSignals(False)
        self.refresh()

    def _sync_batch_checks(self) -> None:
        selected = set(self._selected_meeting_ids())
        for index in range(self._list.count()):
            item = self._list.item(index)
            widget = self._list.itemWidget(item)
            box = widget.findChild(_SessionCheckBox, "meetingBatchCheck") if widget is not None else None
            if box is not None:
                meeting_id = str(item.data(Qt.ItemDataRole.UserRole) or "")
                box.blockSignals(True)
                box.setVisible(self._batch_mode)
                box.setChecked(meeting_id in selected)
                box.blockSignals(False)
                box.update()
            if widget is not None and widget.objectName() == "meetingStreamRow":
                _paint_meeting_row(widget, selected=item.isSelected())

    def _sync_batch_summary(self) -> None:
        count = len(self._selected_meeting_ids())
        if self._batch_mode:
            self._list_summary.setText(f"已选 {count} 场")
        self._list_summary.setToolTip("全选和多选都只包含当前已经加载的会议")

    def _sync_batch_actions(self) -> None:
        count = len(self._selected_meeting_ids())
        self._batch_export_btn.setText(f"导出 {count} 场" if count else "导出")
        self._batch_generate_btn.setText(f"生成 {count} 场" if count else "生成纪要")
        self._batch_delete_btn.setText(f"删除 {count} 场" if count else "删除")
        for button in (self._batch_export_btn, self._batch_generate_btn, self._batch_delete_btn):
            button.setEnabled(count > 0)
            fit_action_button(button)
        fit_action_button(self._batch_all_btn)
        fit_action_button(self._selection_mode_btn)
        self._batch_tools.setVisible(self._batch_mode)
        self._batch_notice_scroll.setVisible(self._batch_mode)
        self._export_btn.setVisible(not self._batch_mode)
        self._copy_btn.setVisible(not self._batch_mode)
        if not self._batch_mode:
            return
        titles = []
        for meeting_id in self._selected_meeting_ids():
            record = self._records.get(meeting_id) or self._store.get_meeting(meeting_id)
            if record is not None:
                titles.append(record.display_title)
        listed = "\n".join(titles[:12])
        if len(titles) > 12:
            listed += "\n…"
        self._batch_notice.setText(
            f"已选 {count} 场已加载的会议。\n"
            "单击勾选或取消，Shift 连选，Ctrl 加选。\n"
            "全选只包含当前列表里已经加载的会议。\n"
            "导出会写成一个 Markdown。生成纪要只排队还没有纪要的会议。删除无法撤销。\n\n"
            + listed
        )
        self._batch_notice_scroll.updateGeometry()

    def _set_single_detail_visible(self, visible: bool) -> None:
        for widget in (
            self._detail_title, self._more_btn, self._detail_meta, self._state_row,
            self._materials, self._tab_bar, self._tab_tools, self._content_stack,
            self._copy_btn, self._export_btn,
        ):
            widget.setVisible(visible)
        if visible:
            self._batch_tools.hide()
            self._batch_notice_scroll.hide()
        else:
            self._audio_player.stop()
            self._audio_player.hide()
            self._detail_find_bar.hide()
            self._content_hint.hide()

    def _select_loaded_meetings(self) -> None:
        if not self._batch_mode:
            self._set_batch_mode(True)
        self._list.blockSignals(True)
        try:
            for index in range(self._list.count()):
                self._list.item(index).setSelected(True)
        finally:
            self._list.blockSignals(False)
        self._sync_batch_checks()
        self._sync_batch_summary()
        self._sync_batch_actions()

    def _delete_selected_meetings(self) -> None:
        ids = self._selected_meeting_ids()
        records = [self._store.get_meeting(meeting_id) for meeting_id in ids]
        live = [record for record in records if record is not None and record.status == STATUS_LIVE]
        ended = [record.id for record in records if record is not None and record.status != STATUS_LIVE]
        if not ended:
            self._set_status("进行中的会议要先结束后再删除。")
            return
        if not confirm_batch_delete(self, len(ended), len(live)):
            return
        if not self._flush_bound_title(sync=True) or not self._save_notes_draft() or not self._save_record_notes():
            return
        if self._detail_id in ended:
            self._audio_player.stop()
            self._editing_notes = False
            self._notes_edit.clear()
            self._detail_id = ""
            self._drafts.title_meeting_id = ""
        if not self._ctrl.delete_meetings(ended):
            self._set_status("会议删除失败，请重试。")
        else:
            skipped = f"，{len(live)} 场进行中的未删除" if live else ""
            self._set_status(f"已删除 {len(ended)} 场会议{skipped}")
        self.refresh()

    def _confirm_export_overwrite_if_adjusted(self, raw_path: str, final_path: str) -> bool:
        raw_p = Path(raw_path)
        final_p = Path(final_path)
        if raw_p != final_p and final_p.exists():
            from sayink.ui.prompts import confirm_box

            box, overwrite = confirm_box(
                self,
                "确认覆盖已有文件",
                f"文件「{final_p.name}」已存在，是否覆盖？",
                accept="覆盖",
                cancel="取消",
                destructive=True,
            )
            _style_dialog_box(box, danger_btn=overwrite)
            box.exec()
            return box.clickedButton() == overwrite
        return True

    def _export_selected_meetings(self) -> None:
        ids = self._selected_meeting_ids()
        if not ids:
            return
        default = export_save_path(f"会议导出-{time.strftime('%Y%m%d')}.md")
        raw_path, _selected = QFileDialog.getSaveFileName(self, "导出选中的会议", default, "Markdown (*.md)")
        if not raw_path:
            return
        path = _with_export_suffix(raw_path, "md")
        if not self._confirm_export_overwrite_if_adjusted(raw_path, path):
            return
        try:
            Path(path).write_text(self._ctrl.export_meetings_markdown(ids), encoding="utf-8")
        except OSError as exc:
            box = QMessageBox(self)
            box.setWindowTitle("导出失败")
            box.setIcon(QMessageBox.Icon.Critical)
            box.setText(f"无法写入文件：{exc}")
            box.addButton("确定", QMessageBox.ButtonRole.AcceptRole)
            _style_dialog_box(box)
            box.exec()
            return
        self._set_status(f"已导出 {len(ids)} 场会议")

    def _generate_selected_meetings(self) -> None:
        ids = self._selected_meeting_ids()
        if not ids:
            return
        if not minutes_settings_complete(self._config):
            self.configure_requested.emit()
            return
        host = self._ctrl.remote_host_needing_confirmation()
        if host:
            box = QMessageBox(self)
            box.setWindowTitle("发送到远端模型")
            box.setIcon(QMessageBox.Icon.Question)
            box.setText(f"所选会议里还没有纪要的文字稿将发送到 {host}。")
            box.setInformativeText("不会发送音频。确认后本机记住该地址，这次批量生成只问这一次。")
            ok = box.addButton("发送并生成", QMessageBox.ButtonRole.AcceptRole)
            box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(ok)
            _style_dialog_box(box)
            box.exec()
            if box.clickedButton() != ok:
                return
            self._ctrl.confirm_remote_host(host)
        template_id = str(self._config.get("meeting.template_id", "general") or "general")
        queued, skipped = self._ctrl.enqueue_minutes(ids, template_id=template_id)
        if queued:
            tail = f"，跳过 {skipped} 场" if skipped else ""
            self._set_status(f"已排队生成 {queued} 场纪要{tail}")
        else:
            self._set_status("没有要生成的会议。已有纪要、正在进行或没有文字稿的会跳过。")

    def _load_more(self) -> None:
        self._list_limit += 100
        self.refresh()

    def show_meeting(self, meeting_id: str) -> None:
        """Open a meeting in the browse view (after it ended, or from a notice)."""
        if (
            self._stack.currentIndex() == PAGE_LIVE
            and not self._ctrl.is_active
            and meeting_id
            and meeting_id == self._record_notes_meeting_id
        ):
            self._show_ended_meeting(meeting_id, unsaved_live=self._has_unsaved_record_notes())
            return
        if not self._flush_bound_title(sync=True) or not self._save_notes_draft() or not self._save_record_notes():
            return
        if meeting_id != self._detail_id:
            self._audio_player.stop()
            self._materials_toggle.setChecked(False)
        self._search_timer.stop()
        self._search_query = ""
        self._search_edit.blockSignals(True)
        self._search_edit.clear()
        self._search_edit.blockSignals(False)
        if meeting_id and self._store.get_meeting(meeting_id) is not None:
            visible = self._store.list_meetings(limit=self._list_limit)
            if all(item.id != meeting_id for item in visible):
                self._list_limit = max(self._list_limit, self._store.count_meetings())
        self._transcript_highlight = -1
        self._citation_return = None
        self._correcting_speakers = False
        self._status_label.hide()
        self._detail_id = meeting_id
        self._notes_view.restore_reading_position(0)
        self._transcript_view.restore_reading_position(0)
        self._stack.setCurrentIndex(PAGE_BROWSE)
        self._start_btn.setVisible(True)
        if self._batch_mode:
            # Opening a specific meeting (import, notice) leaves batch selection,
            # otherwise the batch view keeps covering the meeting just opened.
            self._set_batch_mode(False)
        self.refresh()
        self._sync_media_buttons()

    def _ask_import(self) -> tuple[str, str, bool] | None:
        from sayink.ui.transcript_import_dialog import TranscriptImportDialog

        dialog = TranscriptImportDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.transcript_text(), dialog.meeting_title(), dialog.detect_structure()

    def _import_transcript(self) -> None:
        if self._ctrl.is_active:
            self._set_status("会议进行中，请结束后再导入文字稿")
            return
        asked = self._ask_import()
        if not asked:
            return
        text, title, detect = asked
        record = self._ctrl.import_transcript(text, title=title, detect_structure=detect)
        if record is None:
            self._set_status(getattr(self._ctrl, "transcript_import_notice", "") or "没有可导入的文字")
            self._detail_pane.setVisible(True)
            return
        self.show_meeting(record.id)
        self._set_status("已导入文字稿。确认内容后，再点击「生成纪要」。")

    def _sync_media_buttons(self) -> None:
        busy = bool(getattr(self._ctrl, "importing_media", False))
        browsing = self._stack.currentIndex() == PAGE_BROWSE
        self._import_btn.setVisible(browsing)
        self._media_btn.setVisible(browsing)
        self._media_btn.setEnabled(not busy)
        self._import_btn.setEnabled(not busy)
        self._start_btn.setEnabled(not busy)
        self._media_cancel_btn.setVisible(busy and browsing)
        self._media_retry_btn.setVisible(
            browsing and not busy and bool(getattr(self._ctrl, "_unsaved_media", None))
        )

    def _import_media(self) -> None:
        reason = self._ctrl.media_block_reason()
        if reason:
            self._set_status(reason)
            return
        path, _selected = QFileDialog.getOpenFileName(self, "导入音视频", "", file_dialog_filter())
        if not path:
            return
        message = self._ctrl.start_media_import(path, title=Path(path).stem)
        if message:
            self._set_status(message)
            return
        self._pending_media_import_path = path
        self._sync_media_buttons()
        self._media_import_title = Path(path).stem
        self._media_progress_text = "正在读取音频…"
        self._show_import_detail()

    def _show_import_detail(self) -> None:
        """The open meeting stays put until recognition finishes."""
        self._set_single_detail_visible(False)
        self._batch_tools.hide()
        self._batch_notice_scroll.hide()
        self._status_label.hide()
        self._detail_pane.setVisible(True)
        self._detail_title.setVisible(True)
        self._detail_title.setReadOnly(True)
        self._detail_title.blockSignals(True)
        self._detail_title.setText(getattr(self, "_media_import_title", "") or "正在导入音视频")
        self._detail_title.setCursorPosition(0)
        self._detail_title.blockSignals(False)
        self._detail_meta.setVisible(True)
        self._detail_meta.setText("识别完成后会打开这场新会议，当前这场先留在列表里。")
        raw_text = getattr(self, "_media_progress_text", "") or "正在读取音频…"
        match = re.search(r"(\d+)\s*/\s*(\d+)", raw_text)
        if match and int(match.group(2)) > 0:
            done, total = int(match.group(1)), int(match.group(2))
            pct = max(0, min(100, int(round(done * 100.0 / total))))
            display_text = raw_text if "%" in raw_text else f"{raw_text}（{pct}%）"
            self._import_progress_bar.setRange(0, 100)
            self._import_progress_bar.setValue(pct)
        else:
            pct_match = re.search(r"(\d+)\s*%", raw_text)
            if pct_match:
                pct = max(0, min(100, int(pct_match.group(1))))
                self._import_progress_bar.setRange(0, 100)
                self._import_progress_bar.setValue(pct)
            else:
                self._import_progress_bar.setRange(0, 0)
            display_text = raw_text
        self._import_progress.setVisible(True)
        self._import_progress.setText(display_text)
        self._import_progress_bar.setVisible(True)

    def _cancel_media_import(self) -> None:
        self._ctrl.cancel_media_import()
        self._set_status("正在取消。当前这段识别完后，已识别的部分会保留。")

    def _on_media_progress(self, text: str) -> None:
        if not text:
            return
        self._media_progress_text = text
        if getattr(self._ctrl, "importing_media", False):
            self._show_import_detail()

    def _on_media_finished(self, meeting_id: str) -> None:
        imported_path = self._pending_media_import_path
        self._pending_media_import_path = ""
        self._sync_media_buttons()
        if not meeting_id:
            # Leave the import placeholder: show the open meeting or the batch
            # tools again before the notice explains what happened.
            self._render_detail()
            self._set_status(getattr(self._ctrl, "media_import_notice", "") or "没有识别到文字")
            self._detail_pane.setVisible(True)
            self._import_progress.hide()
            self._import_progress_bar.hide()
            return
        if imported_path:
            self._meeting_audio_paths[meeting_id] = imported_path
        self.show_meeting(meeting_id)
        notice = getattr(self._ctrl, "media_import_notice", "") or ""
        if notice:
            self._set_status(notice)
            return
        record = self._store.get_meeting(meeting_id)
        if record is not None and record.status == STATUS_INTERRUPTED:
            self._set_status("已取消导入。已识别的部分保留为已中断的会议。")
        else:
            self._set_status("已导入音视频。确认文字稿后，再点击「生成纪要」。")

    def _retry_media_save(self) -> None:
        message = self._ctrl.retry_media_save()
        self._sync_media_buttons()
        if message:
            self._set_status(message)
            self._detail_pane.setVisible(True)

    def _on_media_failed(self, message: str) -> None:
        self._pending_media_import_path = ""
        self._sync_media_buttons()
        self._import_progress.hide()
        self._import_progress_bar.hide()
        self._render_detail()
        self._set_status(message or "导入音视频失败")

    def _show_ended_meeting(self, meeting_id: str, *, unsaved_live: bool = False) -> None:
        """Leave the live page once recording ends, even when a note save failed."""
        self._live_silence_timer.stop()
        self.clear_system_silent_warning()
        self._live_new_msg_btn.hide()
        title_saved = self._flush_bound_title(sync=True)
        notes_saved = self._save_notes_draft()
        other_record_unsaved = (
            bool(self._record_notes_meeting_id)
            and self._record_notes_meeting_id != meeting_id
            and not self._save_record_notes()
        )
        if not title_saved or not notes_saved or other_record_unsaved:
            self._stack.setCurrentIndex(PAGE_BROWSE)
            self._start_btn.setVisible(True)
            self.refresh()
            if other_record_unsaved or unsaved_live:
                self._set_status("笔记尚未保存，输入已保留。")
            return
        if not unsaved_live and not self._save_record_notes():
            unsaved_live = True
        self._materials_toggle.setChecked(bool(unsaved_live))
        self._search_timer.stop()
        self._search_query = ""
        self._search_edit.blockSignals(True)
        self._search_edit.clear()
        self._search_edit.blockSignals(False)
        if meeting_id and self._store.get_meeting(meeting_id) is not None:
            visible = self._store.list_meetings(limit=self._list_limit)
            if all(item.id != meeting_id for item in visible):
                self._list_limit = max(self._list_limit, self._store.count_meetings())
        self._transcript_highlight = -1
        self._citation_return = None
        self._correcting_speakers = False
        if not unsaved_live:
            self._status_label.hide()
        self._detail_id = meeting_id
        self._notes_view.restore_reading_position(0)
        self._transcript_view.restore_reading_position(0)
        self._stack.setCurrentIndex(PAGE_BROWSE)
        self._start_btn.setVisible(True)
        if self._batch_mode:
            self._set_batch_mode(False)
        self.refresh()
        if unsaved_live or self._has_unsaved_record_notes():
            self._set_status("笔记尚未保存，输入已保留。")

    @property
    def current_meeting_id(self) -> str:
        return self._detail_id

    def set_meeting_audio_path(self, meeting_id: str, path: str | Path | None) -> None:
        """Associate a local audio file or recovery directory with ``meeting_id``."""
        if not meeting_id:
            return
        resolved = str(path or "").strip()
        if resolved:
            self._meeting_audio_paths[meeting_id] = resolved
        else:
            self._meeting_audio_paths.pop(meeting_id, None)
        if self._detail_id == meeting_id and not self._batch_mode:
            self._render_detail()

    register_meeting_audio = set_meeting_audio_path

    def _resolve_meeting_audio_path(self, record: MeetingRecord | None) -> str:
        if record is None:
            return ""
        candidates: list[str] = []
        rec_audio = str(getattr(record, "audio_path", "") or "").strip()
        if rec_audio:
            candidates.append(rec_audio)
        mapped = self._meeting_audio_paths.get(record.id, "")
        if mapped:
            candidates.append(mapped)
        cfg_dir = getattr(self._config, "config_dir", None)
        if cfg_dir is not None:
            base = Path(cfg_dir)
            for rel in (
                base / "meeting-audio" / f"{record.id}.wav",
                base / "meetings" / f"{record.id}.wav",
                base / "audio-recovery" / f"{record.id}.wav",
            ):
                candidates.append(str(rel))
            rec_root = base / "audio-recovery"
            if rec_root.is_dir():
                try:
                    from sayink.audio_recovery import recordings

                    for item in recordings(rec_root):
                        if str(item.metadata.get("meeting_id") or "") == record.id:
                            candidates.append(str(item.path))
                            break
                except Exception:
                    pass
        for cand in candidates:
            if not cand:
                continue
            p = Path(cand)
            if p.is_file() or (p.is_dir() and (p / "audio.f32").is_file()):
                return str(p)
        return ""

    def _clear_detail(self) -> None:
        if not self._flush_bound_title(sync=True) or not self._save_notes_draft() or not self._save_record_notes():
            return
        self._audio_player.stop()
        self._audio_player.hide()
        self._detail_find_bar.hide()
        self._materials.hide()
        self._record_notes.blockSignals(True)
        self._record_notes.clear()
        self._record_notes.blockSignals(False)
        self._record_marks.clear()
        self._drafts.reset_record_notes()
        self._drafts.title_meeting_id = ""
        self._detail_title.clear()
        self._detail_title.setPlaceholderText("会议标题")
        self._detail_meta.clear()
        self._notes_state.clear()
        self._paint_notes_state()
        self._content_hint.hide()
        self._return_notes_btn.hide()
        self._cancel_notes_edit_btn.hide()
        self._notes_view.clear()
        self._notes_empty.clear()
        self._notes_edit.clear()
        self._transcript_view.clear()
        self._speaker_menu.clear()
        self._status_label.hide()
        for widget in (self._generate_btn, self._edit_btn, self._copy_btn, self._transcript_copy_btn, self._export_btn,
                       self._more_btn, self._speaker_btn, self._correct_speaker_btn, self._template_combo, self._detail_title):
            widget.setEnabled(False)
        from sayink.ui import settings_styles as ss
        self._start_btn.setStyleSheet(ss.BTN_ACCENT_SM)

    def _render_detail(self) -> None:
        if getattr(self._ctrl, "importing_media", False):
            self._show_import_detail()
            return
        self._import_progress.hide()
        self._import_progress_bar.hide()
        self._detail_title.setReadOnly(False)
        if self._batch_mode:
            self._set_single_detail_visible(False)
            self._sync_batch_actions()
            return
        self._set_single_detail_visible(True)
        record = self._store.get_meeting(self._detail_id) if self._detail_id else None
        if record is None:
            self._clear_detail()
            return
        for widget in (self._generate_btn, self._edit_btn, self._copy_btn, self._transcript_copy_btn, self._export_btn,
                       self._more_btn, self._speaker_btn, self._template_combo, self._detail_title):
            widget.setEnabled(True)
        if self._drafts.title_meeting_id and self._drafts.title_meeting_id != record.id:
            self._flush_bound_title(sync=True)
        self._drafts.title_meeting_id = record.id
        pending = self._drafts.pending_title
        if pending and pending[0] == self._detail_id and pending[1] != record.title:
            if self._detail_title.text() != pending[1]:
                self._detail_title.setText(pending[1])
                if not self._detail_title.hasFocus():
                    self._detail_title.setCursorPosition(0)
        elif not self._detail_title.hasFocus():
            self._detail_title.setText(record.title)
            self._detail_title.setCursorPosition(0)
            self._drafts.clear_pending_title_for(self._detail_id)
        self._detail_title.setPlaceholderText(record.display_title)
        meta = [_format_dt(record.started_at)]
        if record.ended_at:
            meta.append(_duration_label(record.duration_ms))
        if record.source == IMPORT_SOURCE:
            meta.append("导入文字稿")
        elif record.source == MEDIA_SOURCE:
            meta.append("导入音视频")
            if record.status == STATUS_INTERRUPTED:
                meta.append("已中断")
        elif record.status in _STATUS_TEXT:
            meta.append(_STATUS_TEXT[record.status])
        self._detail_meta.setText(" · ".join(meta))
        self._show_materials(record)
        audio_path = self._resolve_meeting_audio_path(record) if record.status != STATUS_LIVE else ""
        has_audio = self._audio_player.load_audio(audio_path, fallback_duration_ms=record.duration_ms)
        self._audio_player.setVisible(has_audio)

        notes = self._store.get_notes(self._detail_id)
        if notes is not None and notes.user_edited and notes.markdown:
            self._drafts.user_markdown_backups[self._detail_id] = notes.markdown
        generating = self._ctrl.generating_for == self._detail_id
        job = None if generating else self._ctrl.queued_generation(self._detail_id)
        waiting = job is not None and not job.failed
        retry_now = waiting and job.not_before > now_ms()
        blocked = generating or (waiting and not retry_now)
        from sayink.ui import settings_styles as ss

        has_notes = notes is not None and bool(notes.markdown)
        self._start_btn.setStyleSheet(ss.BTN_GHOST_SM)
        configured = minutes_settings_complete(self._config)
        if not configured:
            self._generate_btn.setText("配置 AI 接口")
            self._generate_btn.setAccessibleName("配置 AI 接口")
            self._generate_btn.setToolTip("打开 设置 → AI 服务，填写接口地址、密钥与模型")
        else:
            self._generate_btn.setText("重新生成" if has_notes else "生成纪要")
            self._generate_btn.setAccessibleName(self._generate_btn.text())
            self._generate_btn.setToolTip(
                "另一场会议正在生成；点击后排队，它完成后自动开始"
                if self._ctrl.generating_for and not generating and not waiting else ""
            )
        self._generate_btn.setStyleSheet(ss.BTN_GHOST_SM if has_notes and configured else ss.BTN_ACCENT_SM)
        self._generate_btn.setEnabled(not blocked and record.status != STATUS_LIVE)
        self._restore_action.setEnabled(not generating and notes is not None and bool(notes.previous_markdown))
        self._restore_action.setToolTip("" if self._restore_action.isEnabled() else "还没有上一版可恢复")
        user_backup = self._available_user_backup(self._detail_id, notes)
        self._restore_user_action.setEnabled(not generating and bool(user_backup))
        self._restore_user_action.setToolTip(
            "找回重新生成前的手动修改版本" if self._restore_user_action.isEnabled() else "尚无被覆盖的手动修改版"
        )
        self._cancel_btn.setVisible(generating or waiting)
        self._cancel_btn.setText("取消" if generating else "取消排队")
        default_template = notes.template_id if notes is not None and notes.template_id else self._config.get("meeting.template_id", "general")
        template_id = self._drafts.effective_template_id(self._detail_id, default_template)
        index = self._template_combo.findData(template_id)
        self._template_combo.blockSignals(True)
        if index >= 0:
            self._template_combo.setCurrentIndex(index)
        self._template_combo.blockSignals(False)
        self._template_combo.setEnabled(not blocked)
        self._manage_templates_btn.setEnabled(not self._ctrl.generating_for)
        self._sync_result_actions(notes)
        if (generating or waiting) and configured:
            if generating:
                busy = "整理中…" if self._is_refinement() else "生成中…"
            else:
                busy = "立即重试" if retry_now else "排队中…"
            self._generate_btn.setText(busy)
            self._generate_btn.setAccessibleName(busy)
        markdown = notes.markdown if notes is not None else ""
        self._cancel_notes_edit_btn.setVisible(self._editing_notes)
        if self._editing_notes:
            self._notes_stack.setCurrentWidget(self._notes_edit)
            self._edit_btn.setText("保存修改")
        else:
            self._notes_stack.setCurrentWidget(self._notes_view)
            self._edit_btn.setText("编辑正文")
            if generating and self._ctrl.generation_preview_text:
                self._notes_view.set_reading_plain(self._ctrl.generation_preview_text)
            elif markdown and notes is not None and self._is_refinement(notes.template_id):
                self._notes_view.set_reading_html("".join(
                    "<p>" + html.escape(p).replace("\n", "<br>") + "</p>"
                    for p in markdown.strip().split("\n\n") if p.strip()
                ))
            elif markdown:
                self._notes_view.set_reading_markdown(
                    link_citations(notes_for_display(markdown, record.display_title))
                )
            else:
                self._notes_view.clear()
                if record.status == STATUS_LIVE:
                    self._notes_view.setPlaceholderText("会议进行中，结束后可生成纪要或整理原文。")
                elif not configured:
                    self._notes_view.setPlaceholderText(
                        f"还没有{self._result_label()}。点击「配置 AI 接口」填写接口地址、密钥和模型，无需开启文字润色。"
                    )
                else:
                    self._notes_view.setPlaceholderText(
                        "融合多人发言，保留细节与个人语气，整理成通顺的书面文章。点击「整理原文」开始。"
                        if self._is_refinement() else "还没有纪要。选择模板后点击「生成纪要」。"
                    )
                self._notes_empty.setText(self._notes_view.placeholderText())
                self._notes_stack.setCurrentWidget(self._notes_empty)
        self._edit_btn.setEnabled(not generating and (bool(markdown) or self._editing_notes))
        if not self._edit_btn.isEnabled():
            self._edit_btn.setToolTip(f"还没有{self._result_label()}可编辑")
        self._sync_notes_state(notes)

        segments = self._store.get_segments(self._detail_id)
        names = self._ctrl.speaker_names(self._detail_id)
        lines = transcript_lines(segments, record.started_at)
        self._transcript_has_speakers, self._transcript_has_times = self._ctrl.transcript_shape(record, lines)
        self._transcript_offsets = [line.offset_ms for line in lines]
        self._transcript_time_known = [line.time_known for line in lines]
        self._transcript_sequences = [s.seq for s in segments if (s.corrected_text or s.polished_text or s.raw_text).strip()]
        self._corrected_sequences = {s.seq for s in segments if s.corrected_text}
        self._speaker_locked_sequences = {s.seq for s in segments if s.speaker_locked}
        can_correct = record.status in (STATUS_ENDED, STATUS_INTERRUPTED) and not generating and bool(lines)
        self._transcript_editable = can_correct
        self._correct_speaker_btn.setEnabled(can_correct)
        self._correcting_speakers = self._correcting_speakers and can_correct
        self._correct_speaker_btn.blockSignals(True)
        self._correct_speaker_btn.setChecked(self._correcting_speakers)
        self._correct_speaker_btn.blockSignals(False)
        self._correct_speaker_btn.setText("完成纠正" if self._correcting_speakers else "纠正发言人")
        if self._transcript_highlight >= len(lines):
            self._transcript_highlight = -1
        self._transcript_view.set_reading_html(
            self._transcript_html(
                lines, names, links=True, highlight=self._transcript_highlight,
                show_speakers=self._transcript_has_speakers, show_times=self._transcript_has_times,
                label_unknown=record.source != IMPORT_SOURCE,
                playable=has_audio,
            )
        )
        speaker_ids = sorted({int(s.speaker_id) for s in segments if int(s.speaker_id) > 0})
        self._speaker_menu.clear()
        for speaker_id in speaker_ids:
            label = names.get(speaker_id) or f"说话人{speaker_id}"
            action = self._speaker_menu.addAction(label)
            action.triggered.connect(lambda _=False, sid=speaker_id: self._prompt_rename_speaker(sid))
        self._speaker_btn.setVisible(bool(speaker_ids))
        self._speaker_btn.setEnabled(not generating)
        self._sync_copy_action()
        self._sync_content_hint()

    def _on_template_changed(self, index: int) -> None:
        self._template_combo.setToolTip(str(self._template_combo.itemData(index, Qt.ItemDataRole.ToolTipRole) or ""))
        if self._detail_id:
            self._drafts.set_pending_template(self._detail_id, str(self._template_combo.currentData() or "general"))
            self._render_detail()
        self._sync_template_menu()

    def _is_refinement(self, template_id: str | None = None) -> bool:
        return get_template(template_id or str(self._template_combo.currentData() or "general"), self._custom_templates()).is_refinement

    def _result_label(self) -> str:
        notes = self._store.get_notes(self._detail_id) if self._detail_id else None
        template_id = notes.template_id if notes is not None and notes.markdown else None
        return "精整正文" if self._is_refinement(template_id) else "纪要"

    def _sync_result_actions(self, notes) -> None:
        label = self._result_label()
        has_notes = notes is not None and bool(notes.markdown)
        if minutes_settings_complete(self._config):
            if self._is_refinement():
                action = "重新整理" if has_notes and self._is_refinement(notes.template_id) else "整理原文"
            else:
                action = "重新生成" if has_notes else "生成纪要"
            self._generate_btn.setText(action)
            self._generate_btn.setAccessibleName(action)
        self._template_combo.setToolTip(str(self._template_combo.currentData(Qt.ItemDataRole.ToolTipRole) or ""))
        self._tab_notes.setText(label)
        self._notes_view.setAccessibleName("会议" + label)
        self._notes_edit.setAccessibleName("编辑会议" + label)
        self._edit_btn.setToolTip("直接修改" + label + "文本")
        self._export_actions["notes"].setText("仅导出" + label)
        previous_label = label
        if notes is not None and notes.previous_metadata:
            try:
                metadata = json.loads(notes.previous_metadata)
                if isinstance(metadata, dict):
                    previous_label = "精整正文" if self._is_refinement(metadata.get("template_id", "general")) else "纪要"
            except (ValueError, TypeError):
                pass
        self._restore_action.setText("恢复上一版" + previous_label)

    def _sync_notes_state(self, notes) -> None:
        if self._ctrl.generating_for == self._detail_id:
            progress = self._status_label.text().strip()
            if not progress:
                progress = "正在整理原文…" if self._is_refinement() else "正在生成纪要…"
            self._notes_state.setText(progress)
            running = "整理" if self._is_refinement() else "生成"
            self._notes_state.setToolTip(
                f"{running}进行中。这里显示的是尚未保存的预览；复制和导出仍用上一版，点「取消」可停下。"
            )
            self._status_label.hide()
            self._paint_notes_state()
            return
        if self._editing_notes:
            state = "正在编辑 · 尚未保存"
        elif notes is None or not notes.markdown:
            state = "尚未整理原文" if self._is_refinement() else "尚未生成纪要"
        else:
            state = "已手动编辑" if notes.user_edited else "AI 生成"
        if notes is not None and notes.transcript_changed:
            state += f" · 文字稿已更正，{self._result_label()}需核对，建议重新生成"
        elif notes is not None and "（待核对）" in notes.markdown:
            state += " · 含待核对内容"
        job = self._ctrl.queued_generation(self._detail_id) if self._detail_id else None
        if job is not None:
            state += " · " + self._queue_state(job)
        self._notes_state.setText(state)
        if job is not None:
            self._notes_state.setToolTip(
                "自动生成没有成功，不会再自动重试；修正问题后可手动生成。" if job.failed else
                "多场会议逐场生成，手动点的排在会后自动生成的前面；点「取消排队」可移出队列。"
            )
        else:
            self._notes_state.setToolTip(
                "保留细节与语气的 AI 整理稿，可切换到文字稿核对；手动编辑不等于内容已经核实。"
                if self._result_label() == "精整正文" else
                "纪要根据文字稿生成；点击引用可核对来源。手动编辑不等于内容已经核实。"
            )
        self._paint_notes_state()

    def _queue_state(self, job: QueuedGeneration) -> str:
        reason = job.last_error.rstrip("。")
        if job.failed:
            return f"自动生成失败：{reason}"
        if job.not_before > now_ms():
            at = time.strftime("%H:%M", time.localtime(job.not_before / 1000))
            return f"自动生成暂时失败（{reason}），{at} 自动重试"
        if job.automatic and self._ctrl.defers_automatic_jobs():
            return "会议进行中，本机模型空闲后自动生成"
        position = self._ctrl.queue_position(job.meeting_id)
        return f"排队中 · 第 {position} 位，前面的完成后自动开始" if position else "排队中"

    def _paint_notes_state(self) -> None:
        from sayink.ui import design_tokens as tok

        generating = bool(self._detail_id) and self._ctrl.generating_for == self._detail_id
        row = self._state_row.layout()
        if generating:
            self._state_row.setStyleSheet(
                f"QWidget#meetingState {{ background: {tok.SURFACE_PEARL}; border: 1px solid {tok.HAIRLINE};"
                f" border-radius: {tok.RADIUS_MD}px; }}"
            )
            self._notes_state.setStyleSheet(
                f"color: {tok.TEXT}; font-size: {tok.TYPE_BODY_SM}px; font-weight: 700;"
                f" background: transparent; border: none; padding: 0;"
            )
            row.setContentsMargins(10, 4, 6, 4)
            return
        self._state_row.setStyleSheet("QWidget#meetingState { background: transparent; border: none; }")
        self._notes_state.setStyleSheet(
            f"color: {tok.TEXT_SEC}; font-size: {tok.TYPE_FOOTNOTE}px; background: transparent; border: none; padding: 0;"
        )
        row.setContentsMargins(0, 0, 0, 0)

    def _sync_content_hint(self) -> None:
        if self._batch_mode:
            # The hint describes the open meeting, which batch mode hides.
            self._content_hint.hide()
            return
        transcript = self._content_stack.currentIndex() == 1
        self._return_notes_btn.setVisible(transcript and bool(self._citation_return and self._citation_return[0] == self._detail_id))
        if transcript:
            hint = ("点击发言旁的「更正」修改这一段的归属。" if self._correcting_speakers else
                    "移到段落上或用 Tab 聚焦可编辑、查看详情；点击姓名可命名。"
                    if self._transcript_editable else
                    "正在生成，完成前不能修改文字稿。" if self._ctrl.generating_for == self._detail_id else
                    "有可编辑的段落后，可以把鼠标移到段落上修改。")
        elif self._editing_notes:
            hint = "复制与导出使用当前草稿。切换会议或离开本页时自动保存。"
        elif self._ctrl.generating_for == self._detail_id:
            hint = "生成预览尚未保存；复制和导出仍使用上一版。点「取消」可停下。"
        else:
            notes = self._store.get_notes(self._detail_id) if self._detail_id else None
            if notes is not None and notes.markdown and self._template_combo.currentData() != notes.template_id:
                hint = f"下次生成将使用「{self._template_combo.currentText()}」；当前{self._result_label()}保持原模板。"
            elif self._is_refinement():
                hint = "融合成文，保留细节与口吻；重要内容请核对文字稿。"
            else:
                hint = ""
        self._content_hint.setText(hint)
        self._content_hint.setVisible(bool(hint))

    def _current_notes_text(self) -> str:
        if self._editing_notes:
            return self._notes_edit.toPlainText()
        notes = self._store.get_notes(self._detail_id) if self._detail_id else None
        return notes.markdown if notes is not None else ""

    def _sync_copy_action(self) -> None:
        if not hasattr(self, "_transcript_view"):
            return
        from sayink.ui import settings_styles as ss

        label = "复制草稿" if self._editing_notes else "复制" + self._result_label()
        self._copy_btn.setText(label)
        self._copy_btn.setAccessibleName(label)
        shortcut = "" if self._editing_notes else " · Ctrl+C"
        self._copy_btn.setToolTip(label + ("" if self._result_label() == "精整正文" else " Markdown") + shortcut)
        has_notes = bool(self._current_notes_text().strip())
        has_transcript = bool(self._detail_id and self._transcript_offsets)
        self._copy_btn.setEnabled(has_notes)
        if not has_notes:
            self._copy_btn.setToolTip(f"还没有{self._result_label()}可复制")
        self._transcript_copy_btn.setEnabled(has_transcript)
        # Same filled action as 历史's copy button. The large primary style is
        # taller than the shared small-button box and made the two toolbars disagree.
        self._copy_btn.setStyleSheet(ss.BTN_ACCENT_SM)
        self._transcript_copy_btn.setStyleSheet(ss.BTN_ACCENT_SM)
        self._fit_template_cluster()
        self._export_actions["notes"].setEnabled(has_notes)
        self._export_actions["transcript"].setEnabled(has_transcript)
        self._export_actions["all"].setEnabled(bool(self._detail_id))

    def _transcript_html(
        self, lines, names: dict[int, str], *, links: bool = False, highlight: int = -1, live: bool = False,
        show_speakers: bool = True, show_times: bool = True, label_unknown: bool = True, playable: bool = False,
    ) -> str:
        meeting_id = self._ctrl.current_id if live else self._detail_id
        sequences = self._live_transcript_sequences if live else self._transcript_sequences
        locked = self._live_locked_sequences if live else self._speaker_locked_sequences
        correcting = self._live_correcting_speakers if live else self._correcting_speakers
        auto_names = {
            s.speaker_id for s in self._store.get_speakers(meeting_id)
            if s.suggested_name and not s.display_name and not s.name_locked
        } if links and meeting_id else set()
        return build_transcript_html(
            lines,
            names,
            links=links,
            highlight=highlight,
            live=live,
            sequences=sequences,
            corrected_sequences=self._corrected_sequences,
            locked_sequences=locked,
            correcting=correcting,
            editable=self._transcript_editable,
            live_editable=self._ctrl.state in (MeetingState.LIVE, MeetingState.PAUSED),
            auto_names=auto_names,
            show_speakers=show_speakers,
            show_times=show_times,
            label_unknown=label_unknown,
            playable=playable,
        )

    def _on_notes_link(self, url) -> None:
        if url.scheme() == "ts" and url.path().isdigit():
            self._jump_to_transcript(int(url.path()) * 1000)
        elif url.scheme() in ("http", "https"):
            QDesktopServices.openUrl(url)

    def _jump_to_transcript(self, offset_ms: int) -> None:
        """Show the transcript line a minutes citation or mark points at."""
        if not self._transcript_has_times:
            self._set_status("这场文字稿没有时间戳，无法跳转", transient=True)
            return
        if not self._transcript_offsets:
            return
        index = bisect.bisect_right(self._transcript_offsets, offset_ms + 999) - 1
        if index < 0:
            index = 0
        if index < len(self._transcript_time_known) and not self._transcript_time_known[index]:
            self._set_status("这一段没有时间戳，无法跳转", transient=True)
            return
        if self._content_stack.currentIndex() == 0:
            self._citation_return = (self._detail_id, self._notes_view.verticalScrollBar().value())
        self._transcript_highlight = index
        self._switch_tab(1)
        self._render_detail()
        self._transcript_view._reading_timer.stop()
        self._transcript_view.scrollToAnchor(f"seg{index}")

    def _return_to_notes(self) -> None:
        if self._citation_return is None or self._citation_return[0] != self._detail_id:
            return
        _, value = self._citation_return
        self._switch_tab(0)
        self._notes_view.restore_reading_position(value)

    def _set_status(self, text: str, *, transient: bool = False) -> None:
        self._status_timer.stop()
        self._status_label.setText(text)
        self._status_label.setVisible(bool(text))
        if text and transient:
            self._status_timer.start(4000)

    # ── detail actions ────────────────────────────────────────────

    def _flush_bound_title(self, *, sync: bool = False) -> bool:
        """Persist ``_detail_title`` to the meeting it was loaded for.

        Prevents switching meetings or leaving the page from dropping typed
        title text or writing one meeting's title onto another.
        """
        if not hasattr(self, "_detail_title") or self._detail_title.isReadOnly():
            return True
        meeting_id = self._drafts.title_meeting_id
        if not meeting_id:
            return True
        record = self._store.get_meeting(meeting_id) or self._records.get(meeting_id)
        if record is None:
            return True
        title = self._detail_title.text().strip()
        if not self._detail_title.hasFocus():
            self._detail_title.setCursorPosition(0)
        pending = self._drafts.pending_title
        if title == record.title:
            if not (pending and pending[0] == meeting_id and pending[1] != title):
                self._drafts.clear_pending_title_for(meeting_id)
                return True
        if sync:
            if pending == (meeting_id, title):
                if not self._store.flush():
                    self._set_status("标题尚未保存，输入已保留。")
                    return False
                saved = self._store.get_meeting(meeting_id)
                if saved is None or saved.title != title:
                    self._set_status("标题尚未保存，输入已保留。")
                    return False
                self._drafts.clear_pending_title_for(meeting_id)
                return True
            self._ctrl.update_meeting(meeting_id, title=title)
            saved = self._store.get_meeting(meeting_id) if self._store.flush() else None
            if saved is None or saved.title != title:
                self._set_status("标题尚未保存，输入已保留。")
                return False
            self._drafts.clear_pending_title_for(meeting_id)
            if meeting_id in self._records:
                self._records[meeting_id] = saved
            return True

        if pending == (meeting_id, title):
            return True
        token = self._drafts.begin_title_save(meeting_id, title)
        history = getattr(self._store, "_history", None)
        use_history = history is not None and not getattr(self._store, "disabled", False)
        enqueued = self._drafts.enqueue_tracked(
            history if use_history else None,
            lambda: self._ctrl.update_meeting(meeting_id, title=title),
        )
        if not enqueued:
            saved = self._store.get_meeting(meeting_id)
            if saved is None or saved.title != title:
                if self._detail_id == meeting_id:
                    self._render_detail()
                self._set_status("标题尚未保存，输入已保留。")
                return False
            self._drafts.pending_title = None
            self.refresh()
            return True
        weak_self = weakref.ref(self)
        history.enqueue_write(
            lambda _conn: (page := weak_self()) is not None and page._title_write_settled.emit(meeting_id, title, token)
        )
        self.refresh()
        return True

    def _save_title(self) -> None:
        self._flush_bound_title(sync=False)

    def _on_title_write_settled(self, meeting_id: str, title: str, token: int) -> None:
        if not self._drafts.matches_pending_title(meeting_id, title, token):
            return
        saved = self._store.get_meeting(meeting_id)
        if saved is None or saved.title != title:
            if self._detail_id == meeting_id:
                self._render_detail()
            self._set_status("标题尚未保存，输入已保留。")
            return
        self._drafts.pending_title = None
        self.refresh()

    def _on_history_write_failed(self) -> None:
        pending = self._drafts.pending_title
        if not pending:
            return
        meeting_id, title = pending
        saved = self._store.get_meeting(meeting_id)
        if saved is None or saved.title != title:
            if self._detail_id == meeting_id:
                self._render_detail()
            self._set_status("标题尚未保存，输入已保留。")

    def _generate(self) -> None:
        if not self._detail_id or self._ctrl.generating_for == self._detail_id:
            return
        job = self._ctrl.queued_generation(self._detail_id)
        if job is not None and not job.failed and job.not_before <= now_ms():
            return
        if not minutes_settings_complete(self._config):
            self.configure_requested.emit()
            return
        if self._editing_notes:
            self._toggle_edit()
            if self._editing_notes:
                return
        if not self._confirm_regenerate():
            return
        notes_before = self._store.get_notes(self._detail_id)
        if notes_before is not None and notes_before.user_edited and notes_before.markdown:
            self._drafts.user_markdown_backups[self._detail_id] = notes_before.markdown
        host = self._ctrl.remote_host_needing_confirmation()
        if host:
            box = QMessageBox(self)
            box.setWindowTitle("发送到远端模型")
            box.setIcon(QMessageBox.Icon.Question)
            purpose = "整理原文" if self._is_refinement() else "生成纪要"
            box.setText(f"这场会议的完整文字稿将发送到 {host} {purpose}。")
            box.setInformativeText("不会发送音频。确认后本机记住该地址，之后不再询问。")
            ok = box.addButton("发送并生成", QMessageBox.ButtonRole.AcceptRole)
            box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(ok)
            _style_dialog_box(box)
            box.exec()
            if box.clickedButton() != ok:
                return
            self._ctrl.confirm_remote_host(host)
        template_id = str(self._template_combo.currentData() or "general")
        self._ctrl.generate(self._detail_id, template_id=template_id)

    def _confirm_regenerate(self) -> bool:
        notes = self._store.get_notes(self._detail_id)
        if notes is None or not notes.markdown:
            return True
        box = QMessageBox(self)
        label = self._result_label()
        box.setWindowTitle("重新整理原文" if self._is_refinement() else "重新生成纪要")
        box.setIcon(QMessageBox.Icon.Question)
        if notes.user_edited:
            box.setText(f"当前{label}包含你的手动修改，重新生成会替换它。")
        else:
            box.setText(f"重新生成会替换当前{label}。")
        box.setInformativeText(
            f"将使用「{self._template_combo.currentText()}」重新生成。"
            f"替换前的版本可在「更多 → 恢复上一版{label}」或「恢复手动修改版」找回。"
        )
        go = box.addButton("重新生成", QMessageBox.ButtonRole.AcceptRole)
        back = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(back if notes.user_edited else go)
        _style_dialog_box(box)
        box.exec()
        return box.clickedButton() == go

    def _available_user_backup(self, meeting_id: str, notes=None) -> str:
        if not meeting_id:
            return ""
        if notes is None:
            notes = self._store.get_notes(meeting_id)
        current = notes.markdown if notes is not None else ""
        persisted = str(getattr(notes, "user_markdown", "") or "").strip() if notes is not None else ""
        if persisted and persisted != current.strip():
            return str(getattr(notes, "user_markdown", ""))
        cached = self._drafts.user_markdown_backups.get(meeting_id, "")
        if cached and cached.strip() and cached != current:
            return cached
        return ""

    def _restore_user_backup(self) -> None:
        if not self._detail_id or self._ctrl.generating_for == self._detail_id:
            return
        notes = self._store.get_notes(self._detail_id)
        backup = self._available_user_backup(self._detail_id, notes)
        if not backup:
            return
        if self._editing_notes:
            self._editing_notes = False
        restore_fn = (
            getattr(self._ctrl, "restore_user_markdown", None)
            or getattr(self._ctrl, "restore_user_notes", None)
            or getattr(self._store, "restore_user_markdown", None)
        )
        if callable(restore_fn) and getattr(notes, "user_markdown", ""):
            ok = bool(restore_fn(self._detail_id))
        else:
            ok = bool(self._ctrl.save_user_markdown(self._detail_id, backup))
        self._switch_tab(0)
        self._render_detail()
        if not ok:
            self._set_status("尚未恢复手动修改版，请重试。")
            return
        self._set_status(f"已恢复手动修改版{self._result_label()}。")

    def _restore_previous(self) -> None:
        if self._ctrl.generating_for == self._detail_id:
            return
        notes = self._store.get_notes(self._detail_id) if self._detail_id else None
        if notes is None or not notes.previous_markdown:
            return
        if self._editing_notes:
            self._toggle_edit()
            if self._editing_notes:
                return
        if notes.user_edited and notes.markdown:
            self._drafts.user_markdown_backups[self._detail_id] = notes.markdown
        self._drafts.pop_pending_template(self._detail_id)
        restored = self._ctrl.restore_previous_notes(self._detail_id)
        self._switch_tab(0)
        self._render_detail()
        if not restored:
            self._set_status("尚未恢复上一版，当前内容保持不变。")
            return
        self._set_status(f"已恢复上一版{self._result_label()}；再次恢复可切回刚才的版本。")

    def _switch_tab(self, index: int) -> None:
        self._tab_group.button(index).setChecked(True)
        self._content_stack.setCurrentIndex(index)
        self._tab_tools.setCurrentIndex(index)
        self._sync_copy_action()
        self._sync_content_hint()
        if self._detail_find_bar.isVisible():
            self._find_detail_text(restart=True)

    def _cancel_job(self) -> None:
        if self._detail_id:
            self._ctrl.remove_from_queue(self._detail_id)

    def _keep_open_draft(self, meeting_id: str) -> None:
        """A draft the user is still typing must not be replaced by the model."""
        text = self._notes_edit.toPlainText()
        notes = self._store.get_notes(meeting_id)
        if notes is not None and text == notes.markdown:
            self._editing_notes = False
            return
        saved = self._save_notes_draft()
        self._ctrl.remove_from_queue(meeting_id)
        if meeting_id != self._detail_id:
            return
        # Cancellation announces itself first. Say what actually happened to
        # this draft, or the status bar keeps claiming generation is running.
        if saved:
            self._set_status(f"已保留正在编辑的{self._result_label()}，未用生成结果覆盖。")
        else:
            self._set_status("修改尚未保存，请重试；可先复制草稿保留内容。")

    def _shows_meeting(self, meeting_id: str) -> bool:
        """True when this meeting's detail is on screen, not hidden by batch mode."""
        return bool(meeting_id) and meeting_id == self._detail_id and not self._batch_mode

    def _on_generation_progress(self, meeting_id: str, text: str) -> None:
        if meeting_id == self._detail_id and self._editing_notes:
            self._keep_open_draft(meeting_id)
        if self._ctrl.generating_for != meeting_id:
            if meeting_id != self._listed_generating:
                self.refresh()
            return
        if self._shows_meeting(meeting_id):
            self._set_status(text)
        if meeting_id != self._listed_generating:
            # A queued job just started: its list row changes too.
            self.refresh()
        elif meeting_id == self._detail_id:
            self._render_detail()

    def _on_generation_preview(self, meeting_id: str, text: str) -> None:
        if meeting_id != self._detail_id or self._editing_notes:
            return
        if not text:
            self._render_detail()
            return
        self._notes_stack.setCurrentWidget(self._notes_view)
        bar = self._notes_view.verticalScrollBar()
        position = bar.value()
        at_end = position >= bar.maximum() - 24
        self._notes_view.set_reading_plain(text)
        self._notes_view.restore_reading_position(bar.maximum() if at_end else position)

    def _on_generation_finished(self, meeting_id: str) -> None:
        self._drafts.pop_pending_template(meeting_id)
        if meeting_id == self._detail_id:
            if self._editing_notes and not self._save_notes_draft():
                self.refresh()
                return
            self._editing_notes = False
            self._switch_tab(0)
            self._render_detail()
            if not self._batch_mode:
                self._set_status(
                    "原文已精整，可切换到文字稿核对重要内容。" if self._result_label() == "精整正文" else
                    "纪要已生成，可点击时间戳核对文字稿。"
                )
        self.refresh()

    def _on_generation_failed(self, meeting_id: str, message: str) -> None:
        self.refresh()
        if self._shows_meeting(meeting_id):
            self._set_status(f"{'原文整理' if self._is_refinement() else '纪要生成'}失败：{message}")
        elif self._batch_mode:
            # Batch view has no open meeting; name the one that failed.
            record = self._records.get(meeting_id) or self._store.get_meeting(meeting_id)
            title = record.display_title if record is not None else "一场会议"
            self._set_status(f"「{title}」生成失败：{message}")

    def _on_generation_cancelled(self, meeting_id: str) -> None:
        self.refresh()
        if self._shows_meeting(meeting_id):
            label = self._result_label()
            action = "原文整理" if label == "精整正文" else "纪要生成"
            self._set_status(f"已取消{action}，上一版{label}保持不变。")

    def _on_generation_queued(self, meeting_id: str) -> None:
        self.refresh()

    def _save_notes_draft(self) -> bool:
        if not self._editing_notes or not self._detail_id:
            return True
        text = self._notes_edit.toPlainText()
        notes = self._store.get_notes(self._detail_id)
        if notes is None or text != notes.markdown:
            if not self._ctrl.save_user_markdown(self._detail_id, text):
                self._set_status("修改尚未保存，请重试；可先复制草稿保留内容。")
                return False
            if text.strip():
                self._drafts.user_markdown_backups[self._detail_id] = text
        self._editing_notes = False
        return True

    def _cancel_edit_notes(self) -> None:
        if not self._editing_notes:
            return
        notes = self._store.get_notes(self._detail_id) if self._detail_id else None
        original = notes.markdown if notes is not None else ""
        draft = self._notes_edit.toPlainText()
        if draft != original:
            from sayink.ui.prompts import confirm_box

            label = self._result_label()
            box, discard = confirm_box(
                self,
                "放弃未保存修改",
                f"放弃本次对{label}的未保存修改？",
                accept="放弃修改",
                cancel="继续编辑",
                destructive=True,
            )
            box.setInformativeText("放弃后将恢复为编辑前的已保存内容。")
            _style_dialog_box(box, danger_btn=discard)
            box.exec()
            if box.clickedButton() != discard:
                return
        self._notes_edit.blockSignals(True)
        self._notes_edit.setPlainText(original)
        self._notes_edit.blockSignals(False)
        self._editing_notes = False
        self._render_detail()
        self._set_status("已取消编辑，恢复修改前内容。", transient=True)

    def try_leave(self) -> bool:
        """Save open drafts before another page covers this one.

        False keeps the editor, the draft and the visible hint on this page.
        """
        if not hasattr(self, "_notes_edit"):
            return True
        self._audio_player.stop()
        if not self._flush_bound_title(sync=True):
            return False
        if not self.flush_pending_user_notes():
            self._set_status("笔记尚未保存，输入已保留。")
            return False
        if not self._save_record_notes():
            return False
        if not self._flush_stashed_live_notes():
            return False
        return self._save_notes_draft()

    def hideEvent(self, event) -> None:
        if hasattr(self, "_notes_edit"):
            self._audio_player.stop()
            editing = self._editing_notes
            self._flush_bound_title(sync=True)
            self._save_record_notes()
            self._flush_stashed_live_notes()
            if self._save_notes_draft() and editing:
                self._render_detail()
        super().hideEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "_materials_scroll"):
            cap = max(108, min(200, int(self.height() * 0.28)))
            self._materials_scroll.setMaximumHeight(cap)
        self._position_live_new_msg_btn()

    def keyPressEvent(self, event) -> None:
        if event.matches(QKeySequence.StandardKey.Find):
            self.focus_search()
            event.accept()
            return
        super().keyPressEvent(event)

    def eventFilter(self, obj, event) -> bool:
        if obj is getattr(self, "_notes_bar", None) and event.type() == QEvent.Type.Resize:
            self._fit_template_cluster()
        elif (
            hasattr(self, "_live_transcript")
            and obj is self._live_transcript.viewport()
            and event.type() == QEvent.Type.Resize
        ):
            self._position_live_new_msg_btn()
        elif event.type() == QEvent.Type.KeyPress:
            if obj is getattr(self, "_notes_edit", None) and event.matches(QKeySequence.StandardKey.Save):
                self._save_open_minutes()
                return True
            if obj is getattr(self, "_detail_find_edit", None):
                if event.key() == Qt.Key.Key_Escape:
                    self._close_detail_find()
                    return True
                if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and (
                    event.modifiers() & Qt.KeyboardModifier.ShiftModifier
                ):
                    self._find_detail_text(backward=True)
                    return True
            if obj is getattr(self, "_live_find_edit", None):
                if event.key() == Qt.Key.Key_Escape:
                    self._close_live_find()
                    return True
                if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and (
                    event.modifiers() & Qt.KeyboardModifier.ShiftModifier
                ):
                    self._find_live_text(backward=True)
                    return True
            if obj in (
                getattr(self, "_notes_view", None),
                getattr(self, "_notes_edit", None),
                getattr(self, "_transcript_view", None),
            ) and event.matches(QKeySequence.StandardKey.Find):
                self.open_detail_find()
                return True
        return super().eventFilter(obj, event)

    def _save_open_minutes(self) -> None:
        if not self._editing_notes:
            return
        if not self._save_notes_draft():
            return
        self._set_status("修改已保存")
        self._render_detail()

    def unsaved_exit_draft(self) -> str | None:
        """Save what we can. Return text that did not land, so exit can ask first."""
        if not self._flush_bound_title(sync=True):
            return (self._detail_title.text() if hasattr(self, "_detail_title") else "") or " "
        if not self.flush_pending_user_notes():
            return (self._user_notes.toPlainText() if hasattr(self, "_user_notes") else "") or " "
        if not self._save_record_notes():
            return self._record_notes.toPlainText() or " "
        if not self._flush_stashed_live_notes():
            return next(iter(self._unsaved_live_notes.values()), " ") or " "
        if not self._editing_notes or not self._detail_id:
            return None
        text = self._notes_edit.toPlainText()
        if self._save_notes_draft():
            return None
        return text

    def _on_record_notes_changed(self) -> None:
        self._drafts.mark_record_notes_edited()
        self._sync_record_notes_height()
        self._record_notes_timer.start()

    def _sync_record_notes_height(self) -> None:
        text = self._record_notes.toPlainText()
        line_count = max(1, text.count("\n") + 1) if text.strip() else 1
        line_h = max(18, self._record_notes.fontMetrics().lineSpacing())
        desired = min(84, max(38, line_count * line_h + 18))
        self._record_notes.setMinimumHeight(38)
        self._record_notes.setMaximumHeight(desired)

    def _on_materials_toggled(self, checked: bool) -> None:
        self._materials_scroll.setVisible(bool(checked))
        summary = getattr(self, "_materials_summary_text", "会议资料") or "会议资料"
        self._materials_toggle.setText(f"收起{summary}" if checked else f"展开{summary}")

    def _has_unsaved_record_notes(self) -> bool:
        if not hasattr(self, "_record_notes"):
            return False
        return self._drafts.has_unsaved_record_notes(
            getattr(self, "_store", None),
            self._record_notes.toPlainText(),
        )

    def _flush_stashed_live_notes(self) -> bool:
        ok = self._drafts.flush_stashed_live_notes(
            getattr(self, "_store", None),
            self._ctrl.update_meeting,
        )
        if not ok:
            self._set_status("笔记尚未保存，输入已保留。")
        return ok

    def _show_materials(self, record) -> None:
        stashed = self._drafts.pop_promotable_live_note(
            self._store, record.id, self._record_notes.toPlainText()
        )
        if stashed is not None:
            self._record_notes.blockSignals(True)
            self._record_notes.setPlainText(stashed)
            self._record_notes.blockSignals(False)
            self._record_notes_timer.start()
        marks = self._store.get_marks(record.id)
        agenda = (record.agenda or "").strip()
        # A refresh must not copy one meeting's notes into the next, or wipe
        # keystrokes that have not reached the database yet.
        same_meeting = self._drafts.record_notes_meeting_id == record.id
        unsaved = (
            same_meeting
            and self._drafts.record_notes_dirty
            and self._record_notes.toPlainText() != (record.notes_user or "")
        )
        pending = same_meeting and (self._record_notes_timer.isActive() or unsaved)
        if not pending and (not same_meeting or not self._record_notes.hasFocus()):
            self._record_notes.blockSignals(True)
            self._record_notes.setPlainText(record.notes_user or "")
            self._record_notes.blockSignals(False)
            self._drafts.record_notes_dirty = False
        self._drafts.record_notes_meeting_id = record.id
        notes_text = self._record_notes.toPlainText().strip()
        has = bool(
            agenda
            or (record.notes_user or "").strip()
            or (same_meeting and notes_text)
            or unsaved
            or marks
        )
        summary_bits: list[str] = []
        if agenda:
            summary_bits.append("议程")
        if notes_text or (record.notes_user or "").strip():
            summary_bits.append("我的笔记")
        if marks:
            summary_bits.append(f"{len(marks)} 处标记")
        self._materials_summary_text = (
            f"会议资料（{' · '.join(summary_bits)}）" if summary_bits else "会议资料"
        )
        self._materials.setVisible(has)
        if unsaved and not self._materials_toggle.isChecked():
            self._materials_toggle.setChecked(True)
        self._on_materials_toggled(self._materials_toggle.isChecked())
        self._sync_record_notes_height()
        self._materials_agenda.setVisible(bool(agenda))
        self._materials_agenda.setText(("议程：" + agenda) if agenda else "")
        marks_html = "".join(
            f"<p style='margin:0 0 4px 0'>"
            f"<a href='ts:{max(0, int(mark.offset_ms)) // 1000}' "
            f"style='color:{tok.ACCENT}; text-decoration:none;'>[{format_timestamp(mark.offset_ms)}]</a> "
            f"{html.escape(mark.note) or '标记'}</p>"
            for mark in marks
        )
        self._materials_marks_label.setVisible(bool(marks))
        self._record_marks.setVisible(bool(marks))
        if marks:
            mark_h = min(76, max(32, len(marks) * 22 + 12))
            self._record_marks.setMaximumHeight(mark_h)
        if marks_html != self._drafts.record_marks_html:
            self._drafts.record_marks_html = marks_html
            self._record_marks.setHtml(marks_html)

    def _autosave_record_notes(self) -> None:
        """Persist debounced post-meeting notes asynchronously without blocking the UI thread."""
        meeting_id = self._drafts.record_notes_meeting_id
        if not self._drafts.record_notes_dirty or not meeting_id or not hasattr(self, "_record_notes"):
            return
        text = self._record_notes.toPlainText()
        record = self._store.get_meeting(meeting_id)
        if record is None or (record.notes_user or "") == text:
            self._drafts.record_notes_dirty = False
            return
        token = self._drafts.begin_record_notes_autosave()
        history = getattr(self._store, "_history", None)
        use_history = history is not None and not getattr(self._store, "disabled", False)
        enqueued = self._drafts.enqueue_tracked(
            history if use_history else None,
            lambda: self._ctrl.update_meeting(meeting_id, notes_user=text),
        )
        if not enqueued:
            if not self._drafts.settle_record_notes_write(
                self._store, meeting_id, text, token, self._record_notes.toPlainText()
            ):
                self._set_status("笔记尚未保存，输入已保留。")
            return
        weak_self = weakref.ref(self)
        history.enqueue_write(
            lambda _conn: (page := weak_self()) is not None
            and page._record_notes_write_settled.emit(meeting_id, text, token)
        )

    def _on_record_notes_write_settled(self, meeting_id: str, text: str, token: int) -> None:
        current = self._record_notes.toPlainText() if hasattr(self, "_record_notes") else ""
        if not self._drafts.settle_record_notes_write(self._store, meeting_id, text, token, current):
            self._set_status("笔记尚未保存，输入已保留。")

    def _save_record_notes(self) -> bool:
        """Write the post-meeting notes at a navigation/export/exit boundary."""
        timer = getattr(self, "_record_notes_timer", None)
        if timer is not None:
            timer.stop()
        meeting_id = self._drafts.record_notes_meeting_id
        if not self._drafts.record_notes_dirty or not meeting_id or not hasattr(self, "_record_notes"):
            return self._flush_stashed_live_notes()
        text = self._record_notes.toPlainText()
        record = self._store.get_meeting(meeting_id)
        if record is None or (record.notes_user or "") == text:
            self._drafts.record_notes_dirty = False
            return self._flush_stashed_live_notes()
        # Keep the debounce running across the write. A refresh that sneaks in
        # while the writer is behind must not replace these keystrokes.
        if timer is not None:
            timer.start()
        self._ctrl.update_meeting(meeting_id, notes_user=text)
        saved = self._store.get_meeting(meeting_id) if self._store.flush() else None
        if saved is None or saved.notes_user != text:
            self._set_status("笔记尚未保存，输入已保留。")
            return False
        self._drafts.record_notes_dirty = False
        if timer is not None:
            timer.stop()
        return self._flush_stashed_live_notes()

    def _toggle_edit(self) -> None:
        if not self._detail_id:
            return
        if self._editing_notes:
            if not self._save_notes_draft():
                return
            self._set_status("修改已保存")
        else:
            notes = self._store.get_notes(self._detail_id)
            self._notes_edit.setPlainText(notes.markdown if notes is not None else "")
            self._editing_notes = True
            self._notes_edit.setFocus()
        self._render_detail()

    def _copy_notes(self) -> None:
        text = self._current_notes_text()
        if not text:
            return
        done = "已复制当前草稿" if self._editing_notes else (
            "已复制精整正文" if self._result_label() == "精整正文" else "已复制纪要 Markdown"
        )
        self._set_status(done if copy_text(text) else "复制失败，请重试")

    def _copy_current(self) -> None:
        if self._content_stack.currentIndex() == 0:
            self._copy_notes()
        else:
            self._copy_transcript()

    def _copy_transcript(self) -> None:
        if self._detail_id:
            text = self._ctrl.transcript_text(self._detail_id)
            if text:
                self._set_status("已复制文字稿" if copy_text(text) else "复制失败，请重试")

    def _export(self, scope: str = "all") -> None:
        if not self._flush_bound_title(sync=True) or not self._save_record_notes():
            return
        record = self._store.get_meeting(self._detail_id) if self._detail_id else None
        if record is None:
            return
        label = {"notes": self._result_label(), "transcript": "文字稿", "all": "完整会议记录"}[scope]
        default = export_save_path(
            f"{safe_filename(record.display_title)}-{label}-{time.strftime('%Y%m%d', time.localtime(record.started_at / 1000))}.md"
        )
        filters = "Markdown (*.md);;纯文本 (*.txt)"
        if self._transcript_has_times:
            filters += ";;SRT 字幕 (*.srt)"
        raw_path, selected = QFileDialog.getSaveFileName(
            self, "导出" + label, default, filters,
        )
        if not raw_path:
            return
        kind = _export_kind(raw_path, selected)
        if kind == "srt" and not self._transcript_has_times:
            self._set_status("这场文字稿没有时间戳，无法导出字幕")
            return
        path = _with_export_suffix(raw_path, kind)
        if not self._confirm_export_overwrite_if_adjusted(raw_path, path):
            return
        try:
            if kind == "srt":
                text = self._ctrl.export_transcript_srt(record.id)
                note = getattr(self._ctrl, "srt_skip_note", "") or ""
                if note:
                    self._set_status(note)
            elif kind == "txt":
                if scope == "notes":
                    text = self._current_notes_text()
                elif scope == "transcript":
                    text = self._ctrl.transcript_text(record.id)
                else:
                    notes = self._current_notes_text().rstrip()
                    supplement = self._ctrl.supplements_markdown(record.id).strip()
                    chunks = [part for part in (notes, supplement, self._ctrl.transcript_text(record.id).rstrip()) if part]
                    text = "\n\n".join(chunks)
                    if text and not text.endswith("\n"):
                        text += "\n"
            elif scope == "transcript":
                text = self._ctrl.export_transcript_markdown(record.id)
            elif scope == "notes":
                text = self._current_notes_text()
            elif self._editing_notes:
                supplement = self._ctrl.supplements_markdown(record.id)
                text = self._current_notes_text().rstrip()
                if supplement:
                    text += "\n\n" + supplement
                text += "\n\n## 文字稿\n\n" + self._ctrl.transcript_text(record.id) + "\n"
            else:
                text = self._ctrl.export_markdown(record.id)
            Path(path).write_text(text, encoding="utf-8")
        except OSError as exc:
            box = QMessageBox(self)
            box.setWindowTitle("导出失败")
            box.setIcon(QMessageBox.Icon.Critical)
            box.setText(f"无法写入文件：{exc}")
            box.addButton("确定", QMessageBox.ButtonRole.AcceptRole)
            _style_dialog_box(box)
            box.exec()
            return
        if kind == "srt":
            self._set_status("已导出文字稿字幕")
        else:
            self._set_status("已导出" + label + ("（包含当前草稿）" if self._editing_notes and scope != "transcript" else ""))

    def _delete(self) -> None:
        record = self._store.get_meeting(self._detail_id) if self._detail_id else None
        if record is None:
            return
        if record.status == STATUS_LIVE and self._ctrl.current_id == record.id:
            box = QMessageBox(self)
            box.setWindowTitle("会议进行中")
            box.setIcon(QMessageBox.Icon.Information)
            box.setText("请先结束会议，再删除记录。")
            box.addButton("确定", QMessageBox.ButtonRole.AcceptRole)
            _style_dialog_box(box)
            box.exec()
            return
        from sayink.ui.prompts import confirm_box

        box, delete = confirm_box(
            self,
            "删除会议",
            f"删除「{record.display_title}」？",
            accept="删除",
            cancel="取消",
            destructive=True,
        )
        box.setInformativeText("文字稿、笔记、标记与纪要会一起删除，无法撤销。")
        _style_dialog_box(box, danger_btn=delete)
        box.exec()
        if box.clickedButton() != delete:
            return
        self._audio_player.stop()
        if self._ctrl.delete_meeting(record.id) is False:
            self._set_status("会议删除失败，请重试。")
            self.refresh()
            return
        self._editing_notes = False
        self._detail_id = ""
        self._drafts.title_meeting_id = ""
        self.refresh()

    def _on_transcript_link(self, url) -> None:
        if url.scheme() == "seek-ms" and url.path().isdigit():
            offset_ms = int(url.path())
            if self._audio_player.audio_path:
                self._audio_player.show()
                self._audio_player.seek_ms(offset_ms, autoplay=True)
        elif url.scheme() == "speaker" and url.path().isdigit():
            self._prompt_rename_speaker(int(url.path()))
        elif url.scheme() == "segment" and url.path().isdigit() and self._correcting_speakers:
            self._prompt_segment_speaker(int(url.path()))
        elif url.scheme() == "edit-segment" and url.path().isdigit():
            self._prompt_segment_text(int(url.path()))
        elif url.scheme() in ("edited-segment", "details-segment") and url.path().isdigit():
            self._prompt_segment_text(int(url.path()), details=True)

    def _prompt_segment_text(self, seq: int, *, details: bool = False, meeting_id: str | None = None) -> None:
        meeting_id = meeting_id or self._detail_id
        record = self._store.get_meeting(meeting_id) if meeting_id else None
        if record is None:
            return
        generating = self._ctrl.generating_for == record.id
        live_session = record.status == STATUS_LIVE and self._ctrl.current_id == record.id
        draining = live_session and self._ctrl.state is MeetingState.DRAINING
        live_editable = live_session and self._ctrl.state in (MeetingState.LIVE, MeetingState.PAUSED) and not generating
        read_only = generating or draining or (record.status == STATUS_LIVE and (details or not live_editable))
        if read_only and not details:
            return
        if not read_only and not self._save_notes_draft():
            return
        get_seg = getattr(self._store, "get_segment", None)
        segment = (
            get_seg(record.id, seq)
            if callable(get_seg)
            else next((s for s in self._store.get_segments(record.id) if s.seq == seq), None)
        )
        if segment is None:
            return
        names = self._ctrl.speaker_names(record.id)
        line = transcript_lines([segment], record.started_at)
        if not line:
            return
        speaker = ((names.get(line[0].speaker_id) or f"说话人{line[0].speaker_id}")
                   if line[0].speaker_id > 0 else "说话人待确认")
        position = _position_label(line[0].offset_ms)
        if record.source == IMPORT_SOURCE and record.ended_at is None:
            position = ""
            if line[0].speaker_id <= 0:
                speaker = "本段"
        detail_text = ""
        if details:
            auto_names = {s.speaker_id for s in self._store.get_speakers(record.id)
                          if s.suggested_name and not s.display_name and not s.name_locked}
            detail_text = "姓名来源：" + _speaker_source(segment.speaker_id, names, auto_names)
            if segment.speaker_locked:
                detail_text += "\n发言归属：已手动更正，仅作用于本段。"
            if segment.corrected_text:
                detail_text += "\n正文：已编辑，修改前的原文保留如下。"
        dialog = _SegmentTextDialog(
            segment, position=position, speaker=speaker,
            save_text=lambda text: self._ctrl.save_segment_text(record.id, seq, text), parent=self,
            details=detail_text, read_only=read_only,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            action = "已恢复这一段的原文" if dialog.corrected_text is None else "已保存这一段的修改"
            if self._stack.currentIndex() == PAGE_LIVE and self._ctrl.current_id == record.id:
                self._render_live()
                self._live_transcript.place_cursor_at_segment(seq)
                self._live_feedback_timer.stop()
                self._live_speaker_feedback.setText(action + "。")
                self._live_speaker_feedback.show()
                self._live_feedback_timer.start(4000)
                return
            self._render_detail()
            self._transcript_view.place_cursor_at_segment(seq)
            notes = self._store.get_notes(record.id)
            hint = f"；已有{self._result_label()}保留，建议重新生成。" if notes is not None and notes.markdown else "。"
            self._set_status(action + hint, transient=True)

    def _toggle_speaker_correction(self, enabled: bool) -> None:
        self._correcting_speakers = enabled
        self._render_detail()

    def _prompt_segment_speaker(self, seq: int, *, meeting_id: str | None = None) -> None:
        meeting_id = meeting_id or self._detail_id
        record = self._store.get_meeting(meeting_id) if meeting_id else None
        if record is None or self._ctrl.generating_for == record.id:
            return
        live = record.status == STATUS_LIVE
        if live and (self._ctrl.current_id != record.id or self._ctrl.state is MeetingState.DRAINING):
            return
        get_seg = getattr(self._store, "get_segment", None)
        speaker_ids_fn = getattr(self._store, "speaker_ids_in_segments", None)
        if callable(get_seg) and callable(speaker_ids_fn):
            segment = get_seg(record.id, seq)
            if segment is None:
                return
            raw_ids = speaker_ids_fn(record.id)
        else:
            segments = self._store.get_segments(record.id)
            segment = next((s for s in segments if s.seq == seq), None)
            if segment is None:
                return
            raw_ids = {s.speaker_id for s in segments}
        names = self._ctrl.speaker_names(record.id)
        ids = sorted({int(sid) for sid in raw_ids if sid and int(sid) > 0})
        choices = [(sid, f"{names[sid]} · 说话人{sid}" if sid in names else f"说话人{sid}") for sid in ids]
        choices.append((None, "新增说话人" if live else "新增说话人（仅本段）"))

        def save(speaker_id):
            if live and (self._ctrl.current_id != record.id or self._ctrl.state is MeetingState.DRAINING):
                return False
            return self._ctrl.correct_segment_speaker(record.id, seq, speaker_id)

        dialog = _SegmentSpeakerDialog(segment, choices, self, save_speaker=save, live=live)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._speaker_edit_status(record.id, "已更正这一段的发言人，人工更正会保留。" if live else
                                      "已更正这一段的发言人；请核对纪要中的相关归属。", transient=True)

    def _prompt_rename_speaker(self, speaker_id: int, *, meeting_id: str | None = None) -> None:
        meeting_id = meeting_id or self._detail_id
        if not meeting_id:
            return
        record = self._store.get_meeting(meeting_id)
        if record is None:
            return
        live = record.status == STATUS_LIVE
        if self._ctrl.generating_for == meeting_id:
            self._speaker_edit_status(meeting_id, "纪要生成中，请在完成后为说话人命名。")
            return
        if live and (self._ctrl.current_id != meeting_id or self._ctrl.state is MeetingState.DRAINING):
            return
        current = self._ctrl.speaker_names(meeting_id).get(speaker_id, "")
        scope = ("这组已有发言和后续归入本组的发言都会使用该姓名；留空恢复编号。" if live else
                 "会更新这一组发言的名称和纪要中的称呼；留空恢复编号。")
        from sayink.ui.prompts import prompt_text

        name = prompt_text(
            self, "为整组说话人命名",
            f"「说话人{speaker_id}」的显示名称\n{scope}",
            current,
        )
        if name is not None:
            if live and (self._ctrl.current_id != meeting_id or self._ctrl.state is MeetingState.DRAINING):
                self._speaker_edit_status(meeting_id, "会议已结束或正在整理，姓名尚未保存，请在会后重新命名。")
                return
            self._rename_speaker(speaker_id, name, meeting_id=meeting_id)

    def _rename_speaker(self, speaker_id: int, name: str, *, meeting_id: str | None = None) -> None:
        meeting_id = meeting_id or self._detail_id
        if not meeting_id:
            return
        if meeting_id == self._detail_id and not self._save_notes_draft():
            return
        name = name.strip()
        if name:
            existing_names = self._ctrl.speaker_names(meeting_id)
            duplicate_sids = sorted(
                sid for sid, existing in existing_names.items()
                if sid != speaker_id and (existing or "").strip() == name
            )
            if duplicate_sids:
                target_sid = duplicate_sids[0]
                merge_fn = getattr(self._ctrl, "merge_speakers", None)
                if callable(merge_fn):
                    merged = merge_fn(meeting_id, speaker_id, target_sid)
                else:
                    segments = [
                        s for s in self._store.get_segments(meeting_id)
                        if int(s.speaker_id) == int(speaker_id)
                    ]
                    merged = True
                    for seg in segments:
                        if not self._ctrl.correct_segment_speaker(meeting_id, seg.seq, target_sid):
                            merged = False
                            break
                    if merged:
                        self._ctrl.rename_speaker(meeting_id, speaker_id, "")
                        self._ctrl.rename_speaker(meeting_id, target_sid, name)
                if not merged:
                    self._speaker_edit_status(meeting_id, "姓名尚未保存，请在整理完成后重试。")
                    return
                self._speaker_edit_status(
                    meeting_id,
                    f"已将说话人{speaker_id}并入「{name}」",
                    transient=True,
                )
                return
        if self._ctrl.rename_speaker(meeting_id, speaker_id, name) is None:
            self._speaker_edit_status(meeting_id, "姓名尚未保存，请在整理完成后重试。")
            return
        self._speaker_edit_status(meeting_id, f"已将说话人{speaker_id}改为「{name}」" if name else
                                  f"已恢复「说话人{speaker_id}」", transient=True)

    def _speaker_edit_status(self, meeting_id: str, text: str, *, transient: bool = False) -> None:
        if self._stack.currentIndex() == PAGE_LIVE and self._drafts.live_meeting_id == meeting_id:
            self._live_feedback_timer.stop()
            self._render_live()
            self._live_speaker_feedback.setText(text)
            self._live_speaker_feedback.show()
            if transient:
                self._live_feedback_timer.start(4000)
        elif self._detail_id == meeting_id:
            self._render_detail()
            self._set_status(text, transient=transient)

    def _populate_live_speaker_menu(self) -> None:
        meeting_id = self._ctrl.current_id
        self._live_speaker_menu.clear()
        if not meeting_id or self._ctrl.state is MeetingState.DRAINING:
            return
        names = self._ctrl.speaker_names(meeting_id)
        speaker_ids_fn = getattr(self._store, "speaker_ids_in_segments", None)
        raw_ids = (
            speaker_ids_fn(meeting_id)
            if callable(speaker_ids_fn)
            else {s.speaker_id for s in self._store.get_segments(meeting_id)}
        )
        for sid in sorted({int(sid) for sid in raw_ids if sid and int(sid) > 0}):
            action = self._live_speaker_menu.addAction(names.get(sid) or f"说话人{sid}")
            action.triggered.connect(lambda _=False, sid=sid, mid=meeting_id:
                                     self._prompt_rename_speaker(sid, meeting_id=mid))

    def _toggle_live_speaker_correction(self, enabled: bool) -> None:
        self._live_correcting_speakers = enabled
        self._render_live()

    def _on_live_transcript_link(self, url) -> None:
        meeting_id = self._ctrl.current_id
        if not meeting_id or self._ctrl.state is MeetingState.DRAINING or not url.path().isdigit():
            return
        if url.scheme() == "live-speaker":
            self._prompt_rename_speaker(int(url.path()), meeting_id=meeting_id)
        elif url.scheme() == "live-segment" and self._live_correcting_speakers:
            self._prompt_segment_speaker(int(url.path()), meeting_id=meeting_id)
        elif url.scheme() == "live-edit":
            self._prompt_segment_text(int(url.path()), meeting_id=meeting_id)
        elif url.scheme() == "live-details":
            self._prompt_segment_text(int(url.path()), details=True, meeting_id=meeting_id)

    # ── start card ────────────────────────────────────────────────

    def _on_save_audio_toggled(self, checked: bool) -> None:
        self._config.set("meeting.save_audio", bool(checked))
        if hasattr(self._config, "save"):
            try:
                self._config.save()
            except Exception:
                pass
        self._update_card_notes()

    def show_start_card(self) -> None:
        if not self._flush_bound_title(sync=True) or not self._save_notes_draft() or not self._save_record_notes():
            return
        self._audio_player.stop()
        if self._ctrl.is_active:
            self._enter_live()
            return
        defaults = MeetingOptions.from_config(self._config)
        self._opt_title.clear()
        self._opt_participants.clear()
        self._opt_agenda.clear()
        self._opt_save_audio.blockSignals(True)
        self._opt_save_audio.setChecked(bool(self._config.get("meeting.save_audio", True)))
        self._opt_save_audio.blockSignals(False)
        self._sync_follow_source_label()
        index = self._opt_source.findData(self._config.get("meeting.input_source", "") or "")
        self._opt_source.setCurrentIndex(max(0, index))
        self._opt_model.blockSignals(True)
        self._opt_model.clear()
        self._opt_model.addItem(self._follow_model_label(), "")
        for model in MODEL_REGISTRY:
            if is_model_downloaded(model["id"]):
                self._opt_model.addItem(model["name"], model["id"])
        index = self._opt_model.findData(defaults.model_id)
        self._opt_model.setCurrentIndex(max(0, index))
        self._opt_model.blockSignals(False)
        self._opt_model.setToolTip(self._opt_model.currentText())
        index = self._opt_language.findData(defaults.language)
        self._opt_language.setCurrentIndex(max(0, index))
        self._update_card_notes()
        self._start_btn.setVisible(False)
        self._stack.setCurrentIndex(PAGE_START)
        self._sync_media_buttons()
        self._opt_title.setFocus()
        self._start_scroll.verticalScrollBar().setValue(0)

    def _follow_model_label(self) -> str:
        from sayink.speech_recognizer import get_model_info

        info = get_model_info(self._config.get("stt.model_id", ""))
        name = info["name"] if info else "尚未选择"
        return f"跟随转写（{name}）"

    def _sync_follow_source_label(self) -> None:
        """The follow option names the dictation source; a card choice stays put."""
        self._opt_source.setItemText(
            0, f"跟随转写（当前：{input_source_label(audio_source(self._config))}）"
        )

    def _update_card_notes(self) -> None:
        from sayink import audio_devices

        options = self._options_from_card()
        if options.input_source in ("mixed", "system"):
            loopback_fn = getattr(audio_devices, "describe_active_loopback_device", None)
            dev_desc = str(loopback_fn() or "").strip() if callable(loopback_fn) else ""
            if dev_desc:
                prefix = "" if ("回环" in dev_desc or "输出" in dev_desc) else "当前电脑声回环输出："
                self._opt_loopback_hint.setText(f"{prefix}{dev_desc}")
                self._opt_loopback_hint.show()
            else:
                self._opt_loopback_hint.clear()
                self._opt_loopback_hint.hide()
        else:
            self._opt_loopback_hint.clear()
            self._opt_loopback_hint.hide()
        dictation = str(self._config.get("stt.model_id", DEFAULT_MODEL_ID) or DEFAULT_MODEL_ID)
        caps = resolve_capabilities(self._config, options, dictation_model_id=dictation)
        notes = list(caps.notes)
        if self._store.disabled:
            notes.insert(0, "历史数据库不可用：会议文字稿无法保存，现在无法开始会议。请重启应用或更换历史位置。")
        if not minutes_settings_complete(self._config):
            notes.append("尚未配置纪要模型（设置 → AI 服务）；会议结束后仍可先保存文字稿。")
        self._card_start.setEnabled(not self._store.disabled)
        self._card_notes.setText("\n".join(f"· {n}" for n in notes))
        self._card_notes.setVisible(bool(notes))
        self._sync_aux_buttons()

    # ── aux models (speaker embedding / Silero VAD) ───────────────

    def _sync_aux_buttons(self) -> None:
        """Show 「下载辅助模型」 wherever a helper model is still missing."""
        missing = missing_aux_models()
        if self._aux_dl.running:
            return
        label = f"下载辅助模型（约 {aux_models_size_mb(missing)} MB）" if missing else ""
        what = describe_aux_models(missing)
        self._card_dl_btn.setToolTip(
            f"{what}：下载后在会议开始时自动加载；实际状态可在 {ADVANCED_RECOGNITION_PATH} 查看"
        )
        self._live_dl_btn.setToolTip(f"{what}：本场会议已按当前方式进行，下载后从下一场开始生效")
        for button in (self._card_dl_btn, self._live_dl_btn):
            button.setText(label)
            button.setEnabled(True)
            button.setVisible(bool(missing))

    def _download_aux_models(self) -> None:
        if self._aux_dl.running:
            return
        if not self._aux_dl.start():
            self._sync_aux_buttons()
            return
        for button in (self._card_dl_btn, self._live_dl_btn):
            button.setEnabled(False)
            button.setText("下载中 0%")

    def cancel_downloads(self) -> None:
        """Cancel any in-progress helper-model download on app exit."""
        self._aux_dl.cancel()

    @property
    def running_workers(self):
        return self._aux_dl.running_workers

    def _on_aux_progress(self, pct: int) -> None:
        for button in (self._card_dl_btn, self._live_dl_btn):
            button.setText(f"下载中 {int(pct)}%")

    def _on_aux_finished(self, ok: bool, message: str) -> None:
        if ok or self._aux_dl.downloaded_any:
            self.aux_models_changed.emit()
        if ok:
            if self._stack.currentIndex() == PAGE_START:
                self._update_card_notes()
                self._card_notes.setText(
                    "· 辅助模型已下载，将在会议开始时自动加载。\n" + self._card_notes.text()
                )
                self._card_notes.show()
            elif self._stack.currentIndex() == PAGE_LIVE:
                self._live_caps.setText(f"{self._live_caps.text()} · 辅助模型已下载，下一场会议生效")
        else:
            if self._stack.currentIndex() == PAGE_START:
                self._card_notes.setText(f"· {message}；可稍后重试，或在 引擎 页下载。")
                self._card_notes.show()
            elif self._stack.currentIndex() == PAGE_LIVE:
                self._live_status.setText(f"{message}；可稍后重试，或在 引擎 页下载。")
                self._live_status.show()
        self._sync_aux_buttons()

    def _options_from_card(self) -> MeetingOptions:
        defaults = MeetingOptions.from_config(self._config)
        save_audio = bool(self._opt_save_audio.isChecked())
        kwargs: dict = dict(
            title=self._opt_title.text().strip(),
            participants=self._opt_participants.text().strip(),
            agenda=self._opt_agenda.toPlainText().strip(),
            input_source=str(self._opt_source.currentData() or audio_source(self._config)),
            model_id=str(self._opt_model.currentData() or ""),
            glossary=defaults.glossary,
            vad=defaults.vad,
            language=str(self._opt_language.currentData() or "auto"),
        )
        if "save_audio" in getattr(MeetingOptions, "__dataclass_fields__", {}):
            kwargs["save_audio"] = save_audio
        options = MeetingOptions(**kwargs)
        if not hasattr(options, "save_audio"):
            try:
                object.__setattr__(options, "save_audio", save_audio)
            except Exception:
                pass
        return options

    def _leave_start_card(self) -> None:
        self._start_btn.setVisible(True)
        self._stack.setCurrentIndex(PAGE_BROWSE)
        self._sync_media_buttons()

    def _start_from_card(self) -> None:
        options = self._options_from_card()
        # These are session overrides. Only Settings changes saved defaults.
        self.start_requested.emit(options)

    # ── live view & dual-lane audio meters ────────────────────────

    LIVE_REMOTE_SILENCE_THRESHOLD = 0.008
    LIVE_REMOTE_SILENCE_SECONDS = 15.0
    _DEFAULT_SYSTEM_SILENT_MSG = (
        "电脑声（对方）已连续 15 秒无声音输入：若正在使用蓝牙耳机（Hands-Free 通信设备）"
        "或外接声卡，请检查 Windows 默认播放设备与会议软件扬声器输出是否一致。"
    )

    def _live_input_source(self) -> str:
        record = self._ctrl.current
        if record is not None and getattr(record, "source", ""):
            return str(record.source)
        return str(audio_source(self._config, meeting=True) or "mixed")

    def _live_captures_mic_audio(self) -> bool:
        return self._live_input_source() in ("mixed", "microphone")

    def _live_captures_system_audio(self) -> bool:
        return self._live_input_source() in ("mixed", "system")

    def _sync_live_meter_lanes(self) -> None:
        has_mic = self._live_captures_mic_audio()
        has_sys = self._live_captures_system_audio()
        self._live_mic_label.setText("麦克风" if has_mic else "麦克风（未启用）")
        self._live_sys_label.setText("电脑声（对方）" if has_sys else "电脑声（未启用）")
        self._live_mic_meter.set_active(has_mic)
        self._live_sys_meter.set_active(has_sys)

    def set_lane_levels(self, mic_level: float = 0.0, system_level: float = 0.0) -> None:
        """Feed dual-lane real-time levels [0..1] from the active meeting audio pipeline."""
        self._live_mic_meter.set_level(mic_level)
        self._live_sys_meter.set_level(system_level)
        try:
            sys_val = float(system_level)
        except (TypeError, ValueError):
            sys_val = 0.0
        if sys_val >= self.LIVE_REMOTE_SILENCE_THRESHOLD:
            self._live_remote_active_ts = time.monotonic()
            self._system_silent_dismissed = False
            if self._system_silent_banner.isVisible():
                self._system_silent_banner.hide()
        else:
            self._check_live_remote_silence()

    update_lane_levels = set_lane_levels
    set_live_levels = set_lane_levels

    def show_system_silent_warning(self, message: str = "") -> None:
        """Show non-blocking yellow warning banner when remote computer audio stays silent."""
        text = (message or "").strip() or self._DEFAULT_SYSTEM_SILENT_MSG
        self._system_silent_label.setText(text)
        self._system_silent_banner.show()

    def clear_system_silent_warning(self) -> None:
        self._system_silent_dismissed = False
        self._live_remote_active_ts = time.monotonic()
        self._system_silent_banner.hide()

    def _dismiss_system_silent_warning(self) -> None:
        self._system_silent_dismissed = True
        self._system_silent_banner.hide()

    def _check_live_remote_silence(self) -> None:
        if (
            self._stack.currentIndex() != PAGE_LIVE
            or self._ctrl.state is not MeetingState.LIVE
            or not self._live_captures_system_audio()
        ):
            return
        if self._system_silent_dismissed:
            return
        if (time.monotonic() - self._live_remote_active_ts) >= self.LIVE_REMOTE_SILENCE_SECONDS:
            if not self._system_silent_banner.isVisible():
                self.show_system_silent_warning()

    def _on_live_scroll_changed(self, value: int) -> None:
        bar = self._live_transcript.verticalScrollBar()
        if value >= max(0, bar.maximum() - 24):
            self._live_new_msg_btn.hide()

    def _scroll_live_to_bottom(self) -> None:
        bar = self._live_transcript.verticalScrollBar()
        bar.setValue(bar.maximum())
        self._live_new_msg_btn.hide()

    def _position_live_new_msg_btn(self) -> None:
        if not hasattr(self, "_live_new_msg_btn") or not hasattr(self, "_live_transcript"):
            return
        vp = self._live_transcript.viewport()
        self._live_new_msg_btn.adjustSize()
        bw = max(136, self._live_new_msg_btn.sizeHint().width() + 16)
        bh = max(28, self._live_new_msg_btn.sizeHint().height())
        self._live_new_msg_btn.resize(bw, bh)
        x = max(8, (vp.width() - bw) // 2)
        y = max(8, vp.height() - bh - 10)
        self._live_new_msg_btn.move(x, y)

    def _enter_live(self) -> None:
        record = self._ctrl.current
        meeting_id = record.id if record is not None else ""
        if self._drafts.enter_live_session(meeting_id):
            self._live_transcript.clear()
            self._live_find_edit.clear()
            self._live_find_bar.hide()
            self._live_correcting_speakers = False
            self._live_speaker_menu.clear()
            self._live_speaker_feedback.clear()
            self._live_speaker_feedback.hide()
            self._live_feedback_timer.stop()
            self._live_new_msg_btn.hide()
            self._system_silent_dismissed = False
            self._system_silent_banner.hide()
        self._live_remote_active_ts = time.monotonic()
        self._sync_live_meter_lanes()
        self._live_mic_meter.set_level(0.0)
        self._live_sys_meter.set_level(0.0)
        self._live_title.setText(record.display_title if record is not None else "会议进行中")
        self._live_clock.setText(self._ctrl.elapsed_text())
        caps = self._ctrl.capabilities
        if caps is not None:
            vad_label = "Silero VAD 已加载" if caps.vad == "silero" else "能量断句"
            if caps.vad_error:
                vad_label += "（Silero 加载失败，已回退）"
            elif caps.vad_requested == "silero" and caps.vad == "energy":
                vad_label += "（Silero 未下载）"
            bits = [f"模型 {caps.model_id}", vad_label]
            bits.append("说话人模型" if caps.speaker_model_available else "说话人（粗略）")
            if caps.speaker_model_available:
                bits.append("会内换人检测（实验性）；短促插话可能仍需确认")
            if caps.hotwords and caps.hotwords_supported:
                bits.append("热词已启用")
            self._live_caps.setToolTip(" · ".join(bits))
            self._live_caps.setText("说话人区分已增强" if caps.speaker_model_available else "说话人区分较粗略")
        self._sync_aux_buttons()
        self._user_notes.blockSignals(True)
        self._user_notes.setPlainText(record.notes_user if record is not None else "")
        self._user_notes.blockSignals(False)
        self._mark_note.clear()
        self._start_btn.setVisible(False)
        self._stack.setCurrentIndex(PAGE_LIVE)
        self._sync_media_buttons()
        self._sync_live_state()
        self._render_live()

    def _sync_live_state(self) -> None:
        state = self._ctrl.state
        self._sync_live_meter_lanes()
        if state is MeetingState.PAUSED:
            self._live_silence_timer.stop()
            self._live_remote_active_ts = time.monotonic()
            self._live_status.setText(f"采集已暂停：{self._ctrl.paused_detail or '等待继续'}")
            self._live_status.show()
            self._resume_btn.show()
            self._pause_btn.hide()
            self._end_btn.setEnabled(True)
            self._mark_btn.setEnabled(False)
            self._paint_live_clock()
        elif state is MeetingState.DRAINING:
            self._live_silence_timer.stop()
            self._live_status.setText("正在整理文字稿，请稍候…")
            self._live_status.show()
            self._resume_btn.hide()
            self._pause_btn.hide()
            self._end_btn.setEnabled(False)
            self._mark_btn.setEnabled(False)
            self._paint_live_clock()
        else:
            self._live_remote_active_ts = time.monotonic()
            if self._live_captures_system_audio():
                self._live_silence_timer.start()
            else:
                self._live_silence_timer.stop()
            self._live_status.hide()
            self._resume_btn.hide()
            self._pause_btn.show()
            self._end_btn.setEnabled(True)
            self._mark_btn.setEnabled(True)
            self._paint_live_clock()

    def _paint_live_clock(self) -> None:
        from sayink.ui import design_tokens as tok

        paused = self._ctrl.state is MeetingState.PAUSED
        color = tok.TEXT_DIM if paused else tok.STATE_RECORD
        self._live_clock.setStyleSheet(
            f"font-size: {tok.TYPE_TITLE_LG}px; font-weight: 700; color: {color}; background: transparent;"
        )
        self._live_rec_dot.setStyleSheet(
            f"background: {color}; border-radius: 4px;"
        )
        self._live_rec_dot.setAccessibleName("采集已暂停" if paused else "正在记录")

    def _on_state_changed(self, state: MeetingState) -> None:
        unsaved_live = False
        if state in (MeetingState.DRAINING, MeetingState.ENDED):
            self._live_silence_timer.stop()
            # current_id is still this meeting. The debounce timer must not
            # fire later, after the controller has cleared it.
            if not self.flush_pending_user_notes():
                unsaved_live = True
                self._keep_unsaved_live_notes()
        if state is MeetingState.LIVE and self._stack.currentIndex() != PAGE_LIVE:
            self._enter_live()
        elif state in (MeetingState.PAUSED, MeetingState.DRAINING, MeetingState.LIVE):
            self._sync_live_state()
            self._render_live()
        elif state is MeetingState.ENDED:
            self._show_ended_meeting(self._ctrl.current_id, unsaved_live=unsaved_live)

    def _on_meeting_changed(self, meeting_id: str) -> None:
        self._pending_refresh_id = meeting_id
        self._refresh_timer.start()

    def _refresh_now(self) -> None:
        if self._stack.currentIndex() == PAGE_LIVE and self._ctrl.is_active:
            self._render_live()
            return
        if self._pending_refresh_id == self._detail_id or self._pending_refresh_id not in self._records:
            self.refresh()
        else:
            self._render_detail()

    def _render_live(self) -> None:
        meeting_id = self._ctrl.current_id
        if not meeting_id:
            return
        record = self._ctrl.current
        started_at = record.started_at if record is not None else 0
        segments = self._store.get_segments(meeting_id)
        names = self._ctrl.speaker_names(meeting_id)
        prev_seg_count = self._drafts.live_segment_count
        self._live_transcript_sequences = [s.seq for s in segments if (s.corrected_text or s.polished_text or s.raw_text).strip()]
        cur_seg_count = len(self._live_transcript_sequences)
        self._live_locked_sequences = {s.seq for s in segments if s.speaker_locked}
        editable = bool(self._live_transcript_sequences) and self._ctrl.state in (MeetingState.LIVE, MeetingState.PAUSED)
        self._live_speaker_btn.setEnabled(editable)
        self._live_correct_speaker_btn.setEnabled(editable)
        self._live_correcting_speakers = self._live_correcting_speakers and editable
        self._live_correct_speaker_btn.blockSignals(True)
        self._live_correct_speaker_btn.setChecked(self._live_correcting_speakers)
        self._live_correct_speaker_btn.blockSignals(False)
        self._live_correct_speaker_btn.setText("完成纠正" if self._live_correcting_speakers else "纠正发言人")
        self._live_speaker_hint.setText(
            "点击目标段落旁的「更正」修改归属，人工更正会保留。" if self._live_correcting_speakers else
            "移到已出现的段落上可改文字；点击姓名可命名。还在识别的这一句要等落段后才能改。"
        )
        bar = self._live_transcript.verticalScrollBar()
        cursor = self._live_transcript.textCursor()
        position, anchor = cursor.position(), cursor.anchor()
        scroll = bar.value()
        at_end = bar.value() >= max(0, bar.maximum() - 24)
        rendered = self._transcript_html(transcript_lines(segments, started_at), names, links=True, live=True)
        if rendered != self._drafts.live_html:
            previous = self._drafts.live_html
            if previous and self._live_transcript.retarget_speaker_labels(previous, rendered):
                self._drafts.live_html = rendered
            elif previous and rendered.startswith(previous):
                self._drafts.live_html = rendered
                # Normal live transcription only adds paragraphs. Keep the
                # existing document instead of parsing and laying it out again.
                tail = QTextCursor(self._live_transcript.document())
                tail.movePosition(QTextCursor.MoveOperation.End)
                tail.insertBlock()
                tail.insertHtml(rendered[len(previous):])
                cursor = self._live_transcript.textCursor()
                last = self._live_transcript.document().characterCount() - 1
                anchor = self._live_transcript.position_before_action(min(anchor, last))
                position = self._live_transcript.position_before_action(min(position, last))
                cursor.setPosition(anchor)
                cursor.setPosition(position, QTextCursor.MoveMode.KeepAnchor)
                self._live_transcript.setTextCursor(cursor)
                bar.setValue(bar.maximum() if at_end and position == anchor else scroll)
            elif previous and self._live_transcript.retarget_paragraphs(previous, rendered, polish_line_height=False):
                self._drafts.live_html = rendered
                bar.setValue(bar.maximum() if at_end and position == anchor else scroll)
            else:
                # Corrections that split/merge paragraphs or toggle every row rebuild.
                self._drafts.live_html = rendered
                self._live_transcript.setHtml(rendered)
                if at_end and position == anchor:
                    end = self._live_transcript.textCursor()
                    end.movePosition(QTextCursor.MoveOperation.End)
                    self._live_transcript.setTextCursor(end)
                    bar.setValue(bar.maximum())
                else:
                    bar.setValue(scroll)
            if self._live_find_bar.isVisible() and self._live_find_edit.text().strip():
                idx, total = find_in_reading_view(
                    self._live_transcript, self._live_find_edit.text(), step=False
                )
                self._live_find_status.setText("未找到" if total == 0 else f"{idx} / {total}")
        self._drafts.live_segment_count = cur_seg_count
        now_at_end = bar.value() >= max(0, bar.maximum() - 24)
        if now_at_end:
            self._live_new_msg_btn.hide()
        elif cur_seg_count > prev_seg_count:
            self._position_live_new_msg_btn()
            self._live_new_msg_btn.show()
            self._live_new_msg_btn.raise_()
        marks = self._store.get_marks(meeting_id)
        if marks:
            marks_html = "".join(
                f"<p style='margin:0 0 4px 0'>[{format_timestamp(m.offset_ms)}] {html.escape(m.note) or '标记'}</p>"
                for m in marks
            )
        else:
            marks_html = (
                "<p style='margin:0 0 6px 0'>重要时刻按一下「标记此刻」。</p>"
                "<p style='margin:0'>纪要会优先覆盖这些点。</p>"
            )
        if marks_html != self._drafts.live_marks_html:
            self._drafts.live_marks_html = marks_html
            self._marks_view.setHtml(marks_html)

    def _add_mark(self) -> None:
        if self._ctrl.state is MeetingState.PAUSED:
            return
        offset = self._ctrl.add_mark(self._mark_note.text())
        if offset is not None:
            self._mark_note.clear()
            self._render_live()

    def flush_pending_user_notes(self) -> bool:
        """Write the live note and read it back. False leaves the typed text."""
        if hasattr(self, "_user_notes_timer"):
            self._user_notes_timer.stop()
        meeting_id = getattr(self._ctrl, "current_id", None)
        if not meeting_id or not hasattr(self, "_user_notes"):
            return True
        text = self._user_notes.toPlainText()
        store = getattr(self, "_store", None)
        if store is None:
            self._save_user_notes()
            return True
        if self._drafts.flush_live_user_notes(store, meeting_id, text, self._ctrl.update_meeting):
            return True
        self._set_status("笔记尚未保存，输入已保留。")
        return False

    def _keep_unsaved_live_notes(self) -> None:
        """Hold the live note in the post-meeting editor when the write failed."""
        meeting_id = getattr(self._ctrl, "current_id", None) or self._drafts.record_notes_meeting_id
        if not meeting_id or not hasattr(self, "_user_notes"):
            return
        text = self._user_notes.toPlainText()
        if (
            self._drafts.record_notes_meeting_id
            and self._drafts.record_notes_meeting_id != meeting_id
            and self._has_unsaved_record_notes()
            and not self._save_record_notes()
        ):
            self._drafts.stash_live_note_for_other_meeting(
                getattr(self, "_store", None), meeting_id, text
            )
            self._set_status("笔记尚未保存，输入已保留。")
            return
        self._drafts.adopt_live_note(meeting_id)
        self._record_notes.blockSignals(True)
        self._record_notes.setPlainText(text)
        self._record_notes.blockSignals(False)
        if hasattr(self, "_record_notes_timer"):
            self._record_notes_timer.start()
        self._set_status("笔记尚未保存，输入已保留。")

    def _save_user_notes(self) -> None:
        meeting_id = self._ctrl.current_id
        if not meeting_id:
            return
        text = self._user_notes.toPlainText()
        record = self._ctrl.current
        if record is not None and record.notes_user == text:
            return
        self._ctrl.update_meeting(meeting_id, notes_user=text)

    def _confirm_end(self) -> None:
        self._user_notes_timer.stop()
        self._save_user_notes()
        box = QMessageBox(self)
        box.setWindowTitle("结束会议")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText("结束这场会议？")
        box.setInformativeText("会先把剩余语音识别完，然后可以生成纪要。")
        end = box.addButton("结束会议", QMessageBox.ButtonRole.AcceptRole)
        back = box.addButton("继续记录", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(end)
        box.setEscapeButton(back)
        _style_dialog_box(box, danger_btn=end)
        box.exec()
        if box.clickedButton() == end:
            self.end_requested.emit()


__all__ = ["MeetingPage", "PAGE_BROWSE", "PAGE_LIVE", "PAGE_START"]

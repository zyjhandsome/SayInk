"""Embedded meeting audio player bar, dual-lane level meter, and playback helpers."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import wave
import weakref

from PyQt6.QtCore import Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QPainter
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QWidget,
)

from sayink.meeting_store import STATUS_LIVE, MeetingRecord
from sayink.transcript_import import IMPORT_SOURCE


class _CleanComboBox(QComboBox):
    """ComboBox that elides overflowing closed-box text cleanly instead of mid-glyph clipping."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.currentTextChanged.connect(self._sync_overflow_tooltip)

    def _sync_overflow_tooltip(self, *_args) -> None:
        """Repeat the value only when the closed box cannot show all of it."""
        from PyQt6.QtWidgets import QStyle, QStyleOptionComboBox

        text = self.currentText()
        opt = QStyleOptionComboBox()
        self.initStyleOption(opt)
        edit_rect = self.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox, opt, QStyle.SubControl.SC_ComboBoxEditField, self,
        )
        avail = edit_rect.width() - 4
        if avail <= 0 or not text:
            tip = ""
        else:
            elided = self.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, avail)
            tip = text if elided != text else ""
        if self.toolTip() != tip:
            self.setToolTip(tip)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._sync_overflow_tooltip()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._sync_overflow_tooltip()

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
        track_color = tok.qcolor_from_token(tok.HAIRLINE)
        if not self._active and track_color.isValid():
            base_alpha = track_color.alpha()
            track_color.setAlpha(90 if base_alpha > 150 else max(8, int(round(base_alpha * 0.6))))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(track_color)
        painter.drawRoundedRect(rect, 4.0, 4.0)
        if self._active and self._level > 0.005:
            fill_w = max(4, int(round(rect.width() * self._level)))
            bar_color = tok.qcolor_from_token(tok.STATE_WARN if self._level > 0.85 else tok.ACCENT)
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


_STALE_PLAYBACK_WAV_PRUNED = False
_PRUNED_PLAYBACK_DIRS: set[str] = set()


def _prune_stale_playback_wavs(
    out_dir: Path,
    *,
    max_age_s: float = 48 * 3600,
    exclude: set[Path] | None = None,
    force: bool = False,
) -> None:
    global _STALE_PLAYBACK_WAV_PRUNED
    mp_mod = sys.modules.get("sayink.ui.meeting_page")
    mp_flag = getattr(mp_mod, "_STALE_PLAYBACK_WAV_PRUNED", None) if mp_mod is not None else None
    pruned_flag = bool(_STALE_PLAYBACK_WAV_PRUNED and (mp_flag is None or mp_flag))
    if not pruned_flag:
        _PRUNED_PLAYBACK_DIRS.clear()
    dir_key = str(out_dir).casefold()
    if not force and dir_key in _PRUNED_PLAYBACK_DIRS:
        return
    _STALE_PLAYBACK_WAV_PRUNED = True
    if mp_mod is not None:
        mp_mod._STALE_PLAYBACK_WAV_PRUNED = True
    _PRUNED_PLAYBACK_DIRS.add(dir_key)
    excluded: set[str] = set()
    for item in exclude or ():
        try:
            excluded.add(str(Path(item).resolve()).casefold())
        except OSError:
            excluded.add(str(item).casefold())
    try:
        if not out_dir.is_dir():
            return
        cutoff = time.time() - max_age_s
        for wav_file in out_dir.glob("*.wav"):
            try:
                if excluded and str(wav_file.resolve()).casefold() in excluded:
                    continue
                if wav_file.stat().st_mtime < cutoff:
                    wav_file.unlink(missing_ok=True)
            except OSError:
                pass
    except OSError:
        pass


class _AudioPlayerBar(QFrame):
    """Lightweight embedded audio playback bar for local meeting / imported / recovery audio."""

    playback_error = pyqtSignal(str)
    _async_wav_ready = pyqtSignal(int, str, str, str)
    _ASYNC_F32_BYTES = 2 * 1024 * 1024

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("meetingAudioPlayer")
        self._source_path = ""
        self._playable_path = ""
        self._duration_ms = 0
        self._position_ms = 0
        self._playing = False
        self._preparing_async = False
        self._prepare_token = 0
        self._rate = 1.0
        self._dragging_slider = False
        self._allow_headless_fallback: bool = False
        self._pending_seek_ms: int | None = None
        self._backend_error_seen: bool = False
        self._player = None
        self._audio_out = None
        self._session_temp_wavs: set[Path] = set()
        self._temp_wavs_by_source: dict[str, set[Path]] = {}
        self._async_wav_ready.connect(self._on_async_wav_ready)
        session_temp_wavs = self._session_temp_wavs
        prune_thread = threading.Thread(
            target=_prune_stale_playback_wavs,
            args=(Path(tempfile.gettempdir()) / "sayink-playback",),
            kwargs={"exclude": set(session_temp_wavs)},
            daemon=True,
            name="sayink-playback-prune",
        )
        prune_thread.start()
        prune_thread.join(timeout=0.05)

        def _cleanup_on_destroy(*_args, _paths=session_temp_wavs) -> None:
            for wav_path in list(_paths):
                try:
                    wav_path.unlink(missing_ok=True)
                except OSError:
                    pass
            _paths.clear()

        self.destroyed.connect(_cleanup_on_destroy)
        weakref.finalize(self, _cleanup_on_destroy)
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

        self._speed_combo = _CleanComboBox()
        self._speed_combo.setAccessibleName("倍速播放")
        self._speed_combo.setToolTip("调整音频回放倍速")
        for rate in (0.75, 1.0, 1.25, 1.5, 2.0):
            label = f"{rate:.1f}x" if rate in (1.0, 2.0) else f"{rate:g}x"
            self._speed_combo.addItem(label, rate)
        self._speed_combo.setCurrentIndex(1)
        self._speed_combo.currentIndexChanged.connect(self._on_speed_changed)
        self._speed_combo.setFixedWidth(114)
        lay.addWidget(self._speed_combo, 0)

    @property
    def audio_path(self) -> str:
        return self._source_path

    @property
    def duration_ms(self) -> int:
        return self._duration_ms

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
        self._speed_combo.setStyleSheet(
            ss.build_toolbar_combo_css()
            + "QComboBox { padding: 0px 4px 0px 8px; }"
            + "QComboBox:focus, QComboBox:on { padding: 0px 3px 0px 7px; }"
            + "QComboBox::drop-down { width: 22px; }"
        )
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
            self._player.mediaStatusChanged.connect(self._on_media_status_changed)
            self._player.errorOccurred.connect(self._on_backend_error)
            self._player.setPlaybackRate(self._rate)
            return True
        except Exception:
            self._player = None
            self._audio_out = None
            return False

    def _is_media_seekable(self) -> bool:
        if self._player is None:
            return False
        try:
            from PyQt6.QtMultimedia import QMediaPlayer

            return self._player.mediaStatus() in (
                QMediaPlayer.MediaStatus.LoadedMedia,
                QMediaPlayer.MediaStatus.BufferedMedia,
                QMediaPlayer.MediaStatus.BufferingMedia,
                QMediaPlayer.MediaStatus.EndOfMedia,
            )
        except Exception:
            return False

    def _fail_playback(self, message: str) -> None:
        self._playing = False
        self._fallback_timer.stop()
        self._pending_seek_ms = None
        self._sync_ui()
        if message:
            self.playback_error.emit(message)

    def _start_headless_fallback(self) -> None:
        self._playing = True
        self._fallback_last_ts = time.monotonic()
        self._fallback_timer.start()
        self._sync_ui()

    @staticmethod
    def _norm_audio_key(path: str | Path | None) -> str:
        raw = str(path or "").strip()
        if not raw:
            return ""
        try:
            return str(Path(raw).resolve()).casefold()
        except OSError:
            return raw.casefold()

    @staticmethod
    def _temp_wav_path_for_raw_f32(raw_f32: Path) -> Path:
        resolved_raw = raw_f32.resolve()
        digest = hashlib.sha256(str(resolved_raw).encode("utf-8")).hexdigest()[:12]
        out_dir = Path(tempfile.gettempdir()) / "sayink-playback"
        _prune_stale_playback_wavs(out_dir)
        tag = f"{raw_f32.parent.name}-{digest}-{raw_f32.stat().st_size}"
        return out_dir / f"{tag}.wav"

    def _register_temp_wav(self, source_path: str | Path, raw_f32: Path, wav_path: Path) -> None:
        self._session_temp_wavs.add(wav_path)
        for key in (self._norm_audio_key(source_path), self._norm_audio_key(raw_f32)):
            if key:
                self._temp_wavs_by_source.setdefault(key, set()).add(wav_path)

    def unload_source(self) -> None:
        self._pending_seek_ms = None
        self.stop()
        if self._player is not None:
            try:
                self._player.setSource(QUrl())
            except Exception:
                pass
        if self._source_path:
            from sayink.audio_recovery import release_recovery_path

            release_recovery_path(Path(self._source_path))
        self._source_path = ""
        self._playable_path = ""
        self._duration_ms = 0
        self._position_ms = 0
        self._sync_ui()

    def collect_temp_wavs_for_audio_paths(self, audio_paths) -> set[Path]:
        targets: set[Path] = set()
        match_keys: set[str] = set()
        for raw_item in audio_paths or ():
            text = str(raw_item or "").strip()
            if not text:
                continue
            p = Path(text)
            raw_f32 = p / "audio.f32" if p.is_dir() else p
            for candidate in (p, raw_f32):
                key = self._norm_audio_key(candidate)
                if key:
                    match_keys.add(key)
                    targets.update(self._temp_wavs_by_source.get(key, ()))
            try:
                if raw_f32.is_file() and raw_f32.suffix.lower() == ".f32":
                    targets.add(self._temp_wav_path_for_raw_f32(raw_f32))
            except OSError:
                pass
        current_key = self._norm_audio_key(self._source_path)
        playable_path = Path(self._playable_path) if self._playable_path else None
        if (current_key and current_key in match_keys) or (
            playable_path is not None and playable_path in targets
        ):
            self.unload_source()
        return targets

    def remove_temp_wavs(self, targets: set[Path] | list[Path] | tuple[Path, ...]) -> None:
        target_set = set(targets)
        if not target_set:
            return
        playable_path = Path(self._playable_path) if self._playable_path else None
        if playable_path is not None and playable_path in target_set:
            self.unload_source()
        for wav_path in target_set:
            try:
                wav_path.unlink(missing_ok=True)
            except OSError:
                pass
            self._session_temp_wavs.discard(wav_path)
        for key in list(self._temp_wavs_by_source):
            remaining = self._temp_wavs_by_source[key] - target_set
            if remaining:
                self._temp_wavs_by_source[key] = remaining
            else:
                self._temp_wavs_by_source.pop(key, None)

    def cleanup_temp_files(self) -> None:
        self.unload_source()
        for wav_path in list(self._session_temp_wavs):
            try:
                wav_path.unlink(missing_ok=True)
            except OSError:
                pass
        self._session_temp_wavs.clear()
        self._temp_wavs_by_source.clear()
        _prune_stale_playback_wavs(
            Path(tempfile.gettempdir()) / "sayink-playback",
            force=True,
        )

    @staticmethod
    def _convert_raw_f32_to_wav(raw_f32: Path, wav_path: Path) -> None:
        import numpy as np

        out_dir = wav_path.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        if wav_path.is_file():
            return
        tmp_path = out_dir / f"{wav_path.name}.{threading.get_ident()}.tmp"
        chunk_bytes = 262_144 * 4
        try:
            with raw_f32.open("rb") as src_fp, wave.open(str(tmp_path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                while raw_chunk := src_fp.read(chunk_bytes):
                    usable = len(raw_chunk) - (len(raw_chunk) % 4)
                    if usable <= 0:
                        break
                    samples = np.frombuffer(memoryview(raw_chunk)[:usable], dtype="<f4")
                    pcm = (np.clip(np.nan_to_num(samples), -1.0, 1.0) * 32767.0).astype("<i2")
                    wf.writeframes(pcm.tobytes())
            tmp_path.replace(wav_path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    def _materialize_playable_path(self) -> str:
        if self._playable_path and Path(self._playable_path).is_file():
            return self._playable_path
        if not self._source_path:
            return ""
        src = Path(self._source_path)
        if src.is_file() and src.suffix.lower() != ".f32":
            self._playable_path = str(src)
            return self._playable_path
        try:
            raw_f32 = src / "audio.f32" if src.is_dir() else src
            if not raw_f32.is_file():
                return ""
            wav_path = self._temp_wav_path_for_raw_f32(raw_f32)
            self._convert_raw_f32_to_wav(raw_f32, wav_path)
            self._register_temp_wav(self._source_path, raw_f32, wav_path)
            self._playable_path = str(wav_path)
            return self._playable_path
        except Exception:
            return ""

    def _maybe_prepare_playable_async(self) -> bool:
        if self._playable_path and Path(self._playable_path).is_file():
            return False
        if not self._source_path or self._allow_headless_fallback or not self.isVisible():
            return False
        src = Path(self._source_path)
        if src.is_file() and src.suffix.lower() != ".f32":
            return False
        raw_f32 = src / "audio.f32" if src.is_dir() else src
        try:
            if not raw_f32.is_file() or raw_f32.stat().st_size < self._ASYNC_F32_BYTES:
                return False
            wav_path = self._temp_wav_path_for_raw_f32(raw_f32)
            if wav_path.is_file():
                return False
        except OSError:
            return False
        self._prepare_token += 1
        token = self._prepare_token
        source_snapshot = self._source_path
        self._preparing_async = True
        self._sync_ui()

        def _bg() -> None:
            try:
                self._convert_raw_f32_to_wav(raw_f32, wav_path)
                self._async_wav_ready.emit(token, source_snapshot, str(raw_f32), str(wav_path))
            except Exception as exc:
                self._async_wav_ready.emit(token, source_snapshot, "", f"无法播放该音频文件：{exc}")

        threading.Thread(target=_bg, daemon=True, name="sayink-f32-wav").start()
        return True

    def _on_async_wav_ready(self, token: int, source_path: str, raw_f32_str: str, result_or_err: str) -> None:
        if token != self._prepare_token or source_path != self._source_path:
            return
        self._preparing_async = False
        if not raw_f32_str:
            self._fail_playback(result_or_err)
            return
        wav_path = Path(result_or_err)
        self._register_temp_wav(source_path, Path(raw_f32_str), wav_path)
        self._playable_path = str(wav_path)
        self._sync_ui()
        self.play()

    def load_audio(self, path: str | Path | None, *, fallback_duration_ms: int = 0) -> bool:
        resolved = str(path or "").strip()
        if not resolved:
            self.unload_source()
            return False
        p = Path(resolved)
        valid = p.is_file() or (p.is_dir() and (p / "audio.f32").is_file())
        if not valid:
            self.unload_source()
            return False
        if resolved == self._source_path:
            if self._duration_ms <= 0 and fallback_duration_ms > 0:
                self._duration_ms = int(fallback_duration_ms)
                self._sync_ui()
            return True
        self.stop()
        self._pending_seek_ms = None
        from sayink.audio_recovery import hold_recovery_path, release_recovery_path

        if self._source_path:
            release_recovery_path(Path(self._source_path))
        self._source_path = resolved
        if p.is_dir():
            hold_recovery_path(p)
        self._playable_path = str(p) if p.is_file() and p.suffix.lower() != ".f32" else ""
        probed = _probe_wav_duration_ms(p)
        self._duration_ms = max(probed, max(0, int(fallback_duration_ms)))
        self._position_ms = 0
        if self._player is not None:
            try:
                self._player.setSource(
                    QUrl.fromLocalFile(self._playable_path) if self._playable_path else QUrl()
                )
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
        if not self._source_path or self._preparing_async:
            return
        if self._duration_ms > 0 and self._position_ms >= self._duration_ms:
            self._position_ms = 0
            if self._pending_seek_ms is not None and self._pending_seek_ms >= self._duration_ms:
                self._pending_seek_ms = 0
        if self._maybe_prepare_playable_async():
            return
        playable = self._materialize_playable_path()
        if not playable:
            if self._allow_headless_fallback:
                self._start_headless_fallback()
            else:
                self._fail_playback("无法播放该音频文件：未找到可回放的本地音频。")
            return
        if not self._ensure_backend() or self._player is None:
            if self._allow_headless_fallback:
                self._start_headless_fallback()
            else:
                self._fail_playback("未检测到可用的音频输出设备，无法回放录音。")
            return
        self._backend_error_seen = False
        desired_pos = self._pending_seek_ms if self._pending_seek_ms is not None else self._position_ms
        try:
            target_url = QUrl.fromLocalFile(playable)
            if self._player.source() != target_url:
                self._pending_seek_ms = desired_pos
                self._player.setSource(target_url)
            self._player.setPlaybackRate(self._rate)
            if self._is_media_seekable():
                self._player.setPosition(desired_pos)
                self._pending_seek_ms = None
            else:
                self._pending_seek_ms = desired_pos
            self._player.play()
        except Exception as exc:
            if self._allow_headless_fallback:
                self._start_headless_fallback()
            else:
                self._fail_playback(f"无法播放该音频文件：{exc}")
            return
        if self._backend_error_seen:
            if self._allow_headless_fallback:
                self._start_headless_fallback()
            return
        self._fallback_timer.stop()
        self._playing = True
        self._sync_ui()

    def pause(self) -> None:
        self._prepare_token += 1
        self._preparing_async = False
        self._playing = False
        self._fallback_timer.stop()
        if self._player is not None:
            try:
                self._player.pause()
            except Exception:
                pass
        self._sync_ui()

    def stop(self) -> None:
        self._prepare_token += 1
        self._preparing_async = False
        self._playing = False
        self._fallback_timer.stop()
        self._pending_seek_ms = None
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
        self._pending_seek_ms = target
        if self._player is not None and self._playable_path:
            try:
                if self._player.source() == QUrl.fromLocalFile(self._playable_path) and self._is_media_seekable():
                    self._player.setPosition(target)
                    self._pending_seek_ms = None
            except Exception:
                pass
        if autoplay:
            self.play()
        else:
            self._sync_ui()

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
        if not self._dragging_slider and self._pending_seek_ms is None:
            self._position_ms = max(0, int(pos))
            self._sync_ui()

    def _on_backend_duration(self, dur: int) -> None:
        if dur > 0:
            self._duration_ms = int(dur)
            if self._pending_seek_ms is not None and self._pending_seek_ms > self._duration_ms:
                self._pending_seek_ms = self._duration_ms
                self._position_ms = self._duration_ms
            self._sync_ui()

    def _on_media_status_changed(self, status) -> None:
        try:
            from PyQt6.QtMultimedia import QMediaPlayer

            if status in (
                QMediaPlayer.MediaStatus.LoadedMedia,
                QMediaPlayer.MediaStatus.BufferedMedia,
            ) and self._pending_seek_ms is not None:
                target = max(0, int(self._pending_seek_ms))
                if self._duration_ms > 0:
                    target = min(target, self._duration_ms)
                self._position_ms = target
                if self._player is not None:
                    self._player.setPosition(target)
                self._pending_seek_ms = None
                self._sync_ui()
            elif status == QMediaPlayer.MediaStatus.EndOfMedia:
                self._pending_seek_ms = None
                self._playing = False
                self._fallback_timer.stop()
                self._sync_ui()
            elif (
                status == QMediaPlayer.MediaStatus.InvalidMedia
                and not self._backend_error_seen
                and not self._allow_headless_fallback
            ):
                self._backend_error_seen = True
                self._fail_playback("无法播放该音频文件：音频格式不受支持或文件已损坏。")
        except Exception:
            pass

    def _on_backend_error(self, error, error_string: str = "") -> None:
        try:
            from PyQt6.QtMultimedia import QMediaPlayer

            if error == QMediaPlayer.Error.NoError:
                return
        except Exception:
            pass
        self._backend_error_seen = True
        self._pending_seek_ms = None
        if self._allow_headless_fallback:
            return
        detail = str(error_string or "").strip()
        if not detail and self._player is not None:
            try:
                detail = str(self._player.errorString() or "").strip()
            except Exception:
                detail = ""
        msg = (
            f"无法播放该音频文件：{detail}"
            if detail
            else "无法播放该音频文件：音频格式不受支持或文件已损坏。"
        )
        self._fail_playback(msg)

    def _on_backend_state(self, state) -> None:
        try:
            from PyQt6.QtMultimedia import QMediaPlayer

            if state == QMediaPlayer.PlaybackState.PlayingState:
                self._fallback_timer.stop()
                self._playing = True
                self._sync_ui()
            elif state == QMediaPlayer.PlaybackState.PausedState:
                if not self._fallback_timer.isActive():
                    self._playing = False
                    self._sync_ui()
            elif state == QMediaPlayer.PlaybackState.StoppedState and not self._fallback_timer.isActive():
                loading = (
                    self._player is not None
                    and self._player.mediaStatus() == QMediaPlayer.MediaStatus.LoadingMedia
                )
                if not loading:
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
        if getattr(self, "_preparing_async", False):
            self._play_btn.setText("准备中…")
            self._play_btn.setEnabled(False)
        else:
            self._play_btn.setText("暂停" if self._playing else "播放")
            self._play_btn.setEnabled(bool(self._source_path))
        upper = max(self._duration_ms, self._position_ms, 0)
        self._slider.blockSignals(True)
        self._slider.setRange(0, upper)
        if not self._dragging_slider:
            self._slider.setValue(min(self._position_ms, upper))
        self._slider.blockSignals(False)
        self._time_label.setText(
            f"{_format_audio_clock(self._position_ms)} / {_format_audio_clock(self._duration_ms)}"
        )


def resolve_meeting_audio_path(
    record: MeetingRecord | None,
    *,
    meeting_audio_paths: dict[str, str],
    no_audio_ids: set[str],
    recovery_audio_cache: dict[str, tuple[int, str]],
    config,
) -> str:
    """Locate a playable local audio file or recovery directory for ``record``."""
    if record is None:
        return ""
    direct_candidates: list[str] = []
    rec_audio = str(getattr(record, "audio_path", "") or "").strip()
    if rec_audio:
        direct_candidates.append(rec_audio)
    mapped = meeting_audio_paths.get(record.id, "")
    if mapped:
        direct_candidates.append(mapped)
    for cand in direct_candidates:
        p = Path(cand)
        if p.is_file() or (p.is_dir() and (p / "audio.f32").is_file()):
            return str(p)
    if record.source == IMPORT_SOURCE:
        return ""
    if record.status != STATUS_LIVE and record.id in no_audio_ids:
        return ""
    cfg_dir = getattr(config, "config_dir", None)
    if cfg_dir is not None:
        base = Path(cfg_dir)
        for rel in (
            base / "meeting-audio" / f"{record.id}.wav",
            base / "meetings" / f"{record.id}.wav",
            base / "audio-recovery" / f"{record.id}.wav",
        ):
            if rel.is_file() or (rel.is_dir() and (rel / "audio.f32").is_file()):
                return str(rel)
        rec_root = base / "audio-recovery"
        if rec_root.is_dir():
            try:
                sig = hash(
                    tuple(
                        sorted(
                            (entry.name, entry.stat().st_mtime_ns)
                            for entry in os.scandir(rec_root)
                        )
                    )
                )
                cached = recovery_audio_cache.get(record.id)
                if cached is not None and cached[0] == sig:
                    rec_hit = cached[1]
                else:
                    from sayink.audio_recovery import recordings

                    rec_hit = ""
                    for item in recordings(rec_root):
                        if str(item.metadata.get("meeting_id") or "") == record.id:
                            rec_hit = str(item.path)
                            break
                    recovery_audio_cache[record.id] = (sig, rec_hit)
                if rec_hit:
                    p = Path(rec_hit)
                    if p.is_file() or (p.is_dir() and (p / "audio.f32").is_file()):
                        return str(p)
            except Exception:
                pass
    if record.status != STATUS_LIVE and not rec_audio:
        no_audio_ids.add(record.id)
    return ""


__all__ = [
    "_AudioPlayerBar",
    "_CleanComboBox",
    "_LevelMeterBar",
    "_PRUNED_PLAYBACK_DIRS",
    "_STALE_PLAYBACK_WAV_PRUNED",
    "_format_audio_clock",
    "_probe_wav_duration_ms",
    "_prune_stale_playback_wavs",
    "resolve_meeting_audio_path",
]

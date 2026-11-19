"""Inspect, export or re-transcribe opt-in local recording journals."""
from pathlib import Path
from dataclasses import replace
import time

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont, QPalette
from PyQt6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QListWidget,
    QPlainTextEdit, QPushButton, QVBoxLayout,
)

from sayink.asr_correction import apply_glossary_variants
from sayink.atomic_io import atomic_write_text
from sayink.audio_recovery import recordings, delete_recording
from sayink.audio_utils import TARGET_SAMPLE_RATE
from sayink.speech_recognizer import (
    decode_asr_slice,
    normalize_asr_output,
    restore_glossary_case,
)
from sayink.vad_segmenter import SpeechSegmenter, make_speech_gate
from sayink.ui import design_tokens as tok
from sayink.ui import settings_styles
from sayink.ui.native_dialog import native_file_dialog
from sayink.ui.prompts import confirm
from sayink.ui.settings_components import ElidedComboBox, _qcolor
from sayink.ui.theme import apply_window_dark_titlebar


class RecoveryWorker(QThread):
    text_ready = pyqtSignal(str)
    progress = pyqtSignal(int, int)
    failed = pyqtSignal(str)

    def __init__(
        self,
        recording,
        *,
        target=None,
        recognizer=None,
        vad="energy",
        vad_path=None,
        quiet=False,
        glossary=None,
        decode_lock=None,
        recognizer_getter=None,
    ):
        super().__init__()
        self.recording = recording
        self.target = target
        self.recognizer = recognizer
        self.vad = vad
        self.vad_path = vad_path
        self.quiet = quiet
        self.glossary = list(glossary) if glossary else []
        self.decode_lock = decode_lock
        self.recognizer_getter = recognizer_getter
        self.cancelled = False
        self._active_quiet = False
        self._last_pct = -1

    def _report_progress(self, done: int, total: int) -> None:
        if total <= 0:
            return
        pct = min(100, int(done * 100 // total))
        if pct != self._last_pct:
            self._last_pct = pct
            self.progress.emit(min(done, total), total)

    def run(self):
        try:
            total = max(1, int(self.recording.samples))
            if self.target is not None:
                self.recording.export_wav(
                    self.target,
                    cancelled=lambda: self.cancelled,
                    on_progress=self._report_progress,
                )
                return
            seg = SpeechSegmenter()
            seg.set_gate(make_speech_gate(self.vad, seg, model_path=self.vad_path))
            if self.quiet is None:
                self._active_quiet = None if seg.gate_name == "silero" else False
                seg.quiet_speech = self._active_quiet
            else:
                seg.quiet_speech = bool(self.quiet) and seg.gate_name == "silero"
                self._active_quiet = seg.quiet_speech
                if seg.gate_name == "silero":
                    seg.gate.input_gain = 4.0 if self.quiet else 1.0
            done = 0
            # Same 100 ms blocks as live capture; bounded to one ASR slice.
            for block in self.recording.blocks(TARGET_SAMPLE_RATE // 10):
                if self.cancelled:
                    return
                done += len(block)
                audio = seg.feed(block)
                if audio is not None:
                    self._decode(audio)
                self._report_progress(done, total)
            if not self.cancelled and (audio := seg.flush()) is not None:
                self._decode(audio)
            if not self.cancelled:
                self._report_progress(total, total)
        except Exception as exc:
            self.failed.emit(str(exc))

    def _decode(self, audio):
        if self.decode_lock is not None:
            with self.decode_lock:
                active = (
                    self.recognizer_getter()
                    if callable(self.recognizer_getter)
                    else self.recognizer
                ) or self.recognizer
                text = decode_asr_slice(
                    active,
                    audio,
                    retry_empty=True,
                    cancelled=lambda: self.cancelled,
                    quiet_speech=self._active_quiet,
                )
        else:
            active = (
                self.recognizer_getter()
                if callable(self.recognizer_getter)
                else self.recognizer
            ) or self.recognizer
            text = decode_asr_slice(
                active,
                audio,
                retry_empty=True,
                cancelled=lambda: self.cancelled,
                quiet_speech=self._active_quiet,
            )
        if self.cancelled or not text:
            return
        raw_text = normalize_asr_output(text)
        if self.glossary:
            final_text = apply_glossary_variants(
                restore_glossary_case(raw_text, self.glossary),
                self.glossary,
            )
        else:
            final_text = raw_text
        if final_text:
            self.text_ready.emit(final_text)


class AudioRecoveryDialog(QDialog):
    def __init__(
        self,
        root,
        *,
        recognizer=None,
        vad="energy",
        vad_path=None,
        quiet=False,
        glossary=None,
        decode_lock=None,
        recognizer_getter=None,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("本地录音恢复")
        font = QFont(tok.UI_FONT_FAMILY)
        font.setPixelSize(tok.TYPE_BODY)
        self.setFont(font)
        self.root = root
        self.recognizer = recognizer
        self.vad = vad
        self.vad_path = vad_path
        self.quiet = quiet
        self.glossary = list(glossary) if glossary else []
        self.decode_lock = decode_lock
        self.recognizer_getter = recognizer_getter
        self.worker = None
        self._worker_failed = False
        self._busy = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(tok.SPACE_MD, tok.SPACE_MD, tok.SPACE_MD, tok.SPACE_MD)
        layout.setSpacing(10)

        self._note_label = QLabel(
            "录音仅保存在本机。正常结束的缓存保留 7 天，总量上限 5 GB，超出时自动清理最早的旧录音。\n"
            "可导出录音或重新识别。恢复文字显示在这里，可另存为文本。"
        )
        self._note_label.setWordWrap(True)
        layout.addWidget(self._note_label)

        self._empty_banner = QFrame()
        self._empty_banner.setObjectName("recoveryEmptyBanner")
        empty_banner_lay = QVBoxLayout(self._empty_banner)
        empty_banner_lay.setContentsMargins(12, 10, 12, 10)
        empty_banner_lay.setSpacing(2)
        self._empty_banner_title = QLabel("当前没有可恢复的本地录音缓存")
        self._empty_banner_title.setObjectName("recoveryEmptyBannerTitle")
        self._empty_banner_desc = QLabel(
            "设置里打开「保存会议录音」后，会议录音会保存在此，可导出或重新识别。单次转写和持续转写不保存录音。"
        )
        self._empty_banner_desc.setObjectName("recoveryEmptyBannerDesc")
        self._empty_banner_desc.setWordWrap(True)
        empty_banner_lay.addWidget(self._empty_banner_title)
        empty_banner_lay.addWidget(self._empty_banner_desc)
        self._empty_banner.hide()
        layout.addWidget(self._empty_banner)

        self.items = QListWidget()
        self.items.setAccessibleName("可恢复录音列表")
        self._empty_list_hint = QLabel("暂无可恢复的录音记录")
        self._empty_list_hint.setObjectName("recoveryEmptyListHint")
        self._empty_list_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_list_hint.setWordWrap(True)
        self._empty_list_hint.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        items_overlay = QVBoxLayout(self.items)
        items_overlay.setContentsMargins(16, 16, 16, 16)
        items_overlay.addWidget(self._empty_list_hint, 1, Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.items, 1)

        track_row = QHBoxLayout()
        track_row.setContentsMargins(0, 0, 0, 0)
        track_row.setSpacing(tok.SPACE_SM)
        self._track_label = QLabel("恢复音轨")
        track_row.addWidget(self._track_label)
        self.track = ElidedComboBox()
        self.track.setAccessibleName("恢复音轨")
        track_row.addWidget(self.track, 1)
        layout.addLayout(track_row)
        self.items.currentRowChanged.connect(self._update_tracks)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(tok.SPACE_SM)
        self.transcribe_btn = QPushButton("重新识别")
        self.transcribe_btn.setProperty("viBtn", "primarySm")
        self.export_btn = QPushButton("导出 WAV")
        self.export_btn.setProperty("viBtn", "ghostSm")
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setProperty("viBtn", "ghostSm")
        self.delete_btn = QPushButton("删除所选录音")
        self.delete_btn.setProperty("viBtn", "dangerSm")
        actions.addWidget(self.transcribe_btn)
        actions.addWidget(self.export_btn)
        actions.addWidget(self.stop_btn)
        actions.addStretch(1)
        actions.addWidget(self.delete_btn)
        layout.addLayout(actions)

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setAccessibleName("恢复的识别文字")
        self.output.setPlaceholderText("恢复的识别文字将显示在这里")
        layout.addWidget(self.output, 1)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(tok.SPACE_SM)
        footer.addStretch(1)
        self.save_btn = QPushButton("另存文字")
        self.save_btn.setProperty("viBtn", "ghostSm")
        self.close_btn = QPushButton("关闭")
        self.close_btn.setProperty("viBtn", "ghostSm")
        footer.addWidget(self.save_btn)
        footer.addWidget(self.close_btn)
        layout.addLayout(footer)

        for button in (
            self.transcribe_btn,
            self.export_btn,
            self.stop_btn,
            self.delete_btn,
            self.save_btn,
            self.close_btn,
        ):
            button.setCursor(Qt.CursorShape.PointingHandCursor)

        self.output.textChanged.connect(self._sync_save_enabled)
        self.export_btn.clicked.connect(self._export)
        self.transcribe_btn.clicked.connect(self._transcribe)
        self.stop_btn.clicked.connect(self.cancel_processing)
        self.delete_btn.clicked.connect(self._delete)
        self.save_btn.clicked.connect(self._save_text)
        self.close_btn.clicked.connect(self.close)
        self.reapply_theme()
        self._fit_to_available_screen()
        self._refresh()

    def _fit_to_available_screen(self) -> None:
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            self.resize(720, 580)
            return
        avail = screen.availableGeometry()
        if avail.isEmpty():
            self.resize(720, 580)
            return
        max_w = max(240, avail.width() - 24)
        max_h = max(220, avail.height() - 24)
        self.setMinimumSize(min(480, avail.width()), min(360, avail.height()))
        self.resize(min(720, max_w), min(580, max_h))

    def showEvent(self, event):
        super().showEvent(event)
        self._fit_to_available_screen()

    def reapply_theme(self) -> None:
        primary_sm = getattr(settings_styles, "BTN_PRIMARY_SM", settings_styles.BTN_ACCENT_SM)
        self.setStyleSheet(
            settings_styles.WINDOW_CSS
            + settings_styles.build_combo_css()
            + f"\nQDialog {{ background: {tok.BG}; color: {tok.TEXT}; }}"
            + "\nQFrame#recoveryEmptyBanner {"
            + f" background: {tok.SURFACE_PEARL}; border: 1px solid {tok.HAIRLINE};"
            + f" border-radius: {tok.RADIUS_MD}px; }}"
            + "\nQLabel#recoveryEmptyBannerTitle {"
            + f" color: {tok.TEXT}; font-size: {tok.TYPE_BODY_SM}px; font-weight: 700; background: transparent; }}"
            + "\nQLabel#recoveryEmptyBannerDesc {"
            + f" color: {tok.TEXT_SEC}; font-size: {tok.TYPE_FOOTNOTE}px; background: transparent; }}"
            + "\nQLabel#recoveryEmptyListHint {"
            + f" color: {tok.TEXT_DIM}; font-size: {tok.TYPE_FOOTNOTE}px; background: transparent; }}"
            + "\nQListWidget {"
            + f" background: {tok.INPUT_BG}; color: {tok.TEXT};"
            + f" border: 1px solid {tok.CONTROL_BORDER}; border-radius: {tok.RADIUS_MD}px;"
            + " padding: 4px; outline: none; }"
            + "\nQListWidget:disabled {"
            + f" background: {tok.SURFACE_PEARL}; color: {tok.TEXT_DIM}; border-color: {tok.HAIRLINE}; }}"
            + "\nQListWidget::item {"
            + f" color: {tok.TEXT}; border-radius: {tok.RADIUS_SM}px;"
            + " padding: 7px 10px; margin: 1px 0; }"
            + f"\nQListWidget::item:hover:!selected {{ background: {tok.ROW_HOVER}; color: {tok.TEXT}; }}"
            + "\nQListWidget::item:selected, QListWidget::item:selected:active, QListWidget::item:selected:!active {"
            + f" background: {tok.ACCENT_SOFT}; color: {tok.TEXT}; }}"
            + "\nQPlainTextEdit {"
            + f" background: {tok.INPUT_BG}; color: {tok.TEXT};"
            + f" border: 1px solid {tok.CONTROL_BORDER}; border-radius: {tok.RADIUS_MD}px; padding: 8px 10px; }}"
            + "\nQPlainTextEdit:focus {"
            + f" border: 2px solid {tok.ACCENT_FOCUS}; padding: 7px 9px; }}"
        )
        self._note_label.setStyleSheet(
            f"color: {tok.TEXT_SEC}; font-size: {tok.TYPE_FOOTNOTE}px; background: transparent;"
        )
        self._track_label.setStyleSheet(
            f"color: {tok.TEXT_SEC}; font-size: {tok.TYPE_BODY_SM}px; background: transparent;"
        )
        self.status.setStyleSheet(
            f"color: {tok.TEXT_SEC}; font-size: {tok.TYPE_FOOTNOTE}px; background: transparent;"
        )
        self.transcribe_btn.setStyleSheet(primary_sm)
        for button in (self.export_btn, self.stop_btn, self.save_btn, self.close_btn):
            button.setStyleSheet(settings_styles.BTN_GHOST_SM)
        self.delete_btn.setStyleSheet(settings_styles.BTN_DANGER_SM)
        palette = self.output.palette()
        palette.setColor(QPalette.ColorRole.PlaceholderText, _qcolor(tok.TEXT_DIM))
        self.output.setPalette(palette)
        apply_window_dark_titlebar(self, getattr(tok, "ACTIVE_THEME", "light") == "dark")

    def _sync_save_enabled(self) -> None:
        has_text = bool(self.output.toPlainText().strip())
        self.save_btn.setEnabled((not self._busy) and has_text)

    @property
    def running_workers(self):
        return (self.worker,) if self.worker is not None else ()

    def _current_recognizer(self):
        if callable(self.recognizer_getter):
            live = self.recognizer_getter()
            if live is not None:
                self.recognizer = live
            return live
        return self.recognizer

    def cancel_processing(self):
        if self.worker is not None:
            self.worker.cancelled = True
            self.stop_btn.setEnabled(False)
            if self.worker.isRunning():
                self.status.setText("正在停止，请等待当前识别片段完成…")

    def _refresh(self):
        self.recordings = recordings(self.root)
        self.items.clear()
        for item in self.recordings:
            try:
                stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(item.metadata.get("started_at", 0)))
            except (OSError, OverflowError, ValueError, TypeError):
                stamp = "未知时间"
            state = "已结束" if item.metadata.get("complete") else "异常中断 / 缓存失败"
            self.items.addItem(f"{stamp} · {item.seconds / 60:.1f} 分钟 · {state}")
        has_recordings = bool(self.recordings)
        self._empty_banner.setVisible(not has_recordings)
        self._empty_list_hint.setVisible(not has_recordings)
        if has_recordings:
            self.items.setCurrentRow(0)
            if self._current_recognizer() is None:
                self.status.setText("模型尚未就绪，可先导出录音；模型载入后可重新识别。")
            else:
                self.status.setText("")
        else:
            self.track.clear()
            self.status.setText("没有录音缓存。会议录音在设置里打开「保存会议录音」后才会留下。")
        self._set_busy(False)

    def _update_tracks(self, row):
        self.track.clear()
        if 0 <= row < len(self.recordings):
            titles = {"audio": "混合录音", "microphone": "麦克风原始音轨", "system": "电脑声音原始音轨"}
            for track in self.recordings[row].available_tracks:
                self.track.addItem(titles.get(track, track), track)

    def _selected(self):
        row = self.items.currentRow()
        return (replace(self.recordings[row], track=self.track.currentData() or "audio")
                if 0 <= row < len(self.recordings) else None)

    def _set_busy(self, busy):
        self._busy = bool(busy)
        for button in (self.export_btn, self.delete_btn):
            button.setEnabled(not busy and bool(self.recordings))
        self.transcribe_btn.setEnabled(
            not busy and bool(self.recordings) and self._current_recognizer() is not None
        )
        self.stop_btn.setEnabled(bool(busy))
        self.items.setEnabled(not busy)
        self.track.setEnabled(not busy)
        self._sync_save_enabled()

    def _on_progress(self, done: int, total: int) -> None:
        if self.worker is None or self.worker.cancelled or self._worker_failed:
            return
        pct = min(100, int(done * 100 // max(1, total)))
        done_min = done / TARGET_SAMPLE_RATE / 60.0
        total_min = total / TARGET_SAMPLE_RATE / 60.0
        self.status.setText(f"正在本机处理… {pct}%（{done_min:.1f} / {total_min:.1f} 分钟）")

    def _on_failed(self, message: str) -> None:
        self._worker_failed = True
        self.status.setText(f"处理失败：{message}")

    def _start(self, worker):
        self.worker = worker
        self._worker_failed = False
        self._set_busy(True)
        self.status.setText("正在本机处理…")
        worker.text_ready.connect(self.output.appendPlainText)
        worker.progress.connect(self._on_progress)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(self._finished)
        worker.start()

    def _finished(self):
        worker = self.worker
        if not self._worker_failed and worker is not None:
            self.status.setText("处理完成" if not worker.cancelled else "已停止，已有恢复文字保留")
        self._set_busy(False)
        self.worker = None
        if worker is not None:
            worker.deleteLater()

    def _export(self):
        if item := self._selected():
            with native_file_dialog(self):
                target, _ = QFileDialog.getSaveFileName(self, "导出录音", "恢复录音.wav", "WAV (*.wav)")
            if target:
                self._start(RecoveryWorker(item, target=Path(target)))

    def _transcribe(self):
        rec = self._current_recognizer()
        if (item := self._selected()) and rec is not None:
            self.output.clear()
            self._start(
                RecoveryWorker(
                    item,
                    recognizer=rec,
                    vad=self.vad,
                    vad_path=self.vad_path,
                    quiet=self.quiet,
                    glossary=self.glossary,
                    decode_lock=self.decode_lock,
                    recognizer_getter=self.recognizer_getter,
                )
            )

    def _delete(self):
        if (item := self._selected()) and confirm(
                self, "删除录音", "永久删除所选录音缓存？已导出的文件和识别文字会保留。",
                accept="删除", destructive=True,
        ):
            try:
                delete_recording(self.root, item)
                self._refresh()
            except OSError as exc:
                self.status.setText(str(exc))

    def _save_text(self):
        with native_file_dialog(self):
            target, _ = QFileDialog.getSaveFileName(self, "另存恢复文字", "恢复文字.txt", "Text (*.txt)")
        if target:
            try:
                atomic_write_text(target, self.output.toPlainText(), encoding="utf-8")
            except PermissionError as exc:
                self.status.setText(f"目标文件可能正被占用，无法写入：{exc}")
            except OSError as exc:
                self.status.setText(str(exc))

    def reject(self):
        if self.worker is not None and self.worker.isRunning():
            self.worker.cancelled = True
            self.stop_btn.setEnabled(False)
            self.status.setText("正在停止，请等待当前识别片段完成…")
            return
        super().reject()

    def closeEvent(self, event):
        if self.worker is not None and self.worker.isRunning():
            self.reject()
            event.ignore()
        else:
            super().closeEvent(event)

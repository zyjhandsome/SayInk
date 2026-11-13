"""Inspect, export or re-transcribe opt-in local recording journals."""
from pathlib import Path
from dataclasses import replace
import time

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtGui import QFont, QColor, QPalette
from PyQt6.QtWidgets import (QApplication, QDialog, QVBoxLayout, QHBoxLayout, QLabel, QListWidget,
                            QPushButton, QPlainTextEdit, QFileDialog, QComboBox)

from sayink.asr_correction import apply_glossary_variants
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
from sayink.ui.prompts import confirm


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
        note = QLabel("录音仅保存在本机。正常结束的缓存保留 7 天，异常中断的缓存需手动删除；录音上限 2 GB。\n"
                      "可导出录音或重新识别。恢复文字显示在这里，可另存为文本。")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.items = QListWidget()
        layout.addWidget(self.items)
        self.track = QComboBox()
        self.track.setAccessibleName("恢复音轨")
        layout.addWidget(self.track)
        self.items.currentRowChanged.connect(self._update_tracks)
        actions = QHBoxLayout()
        self.export_btn = QPushButton("导出 WAV")
        self.transcribe_btn = QPushButton("重新识别")
        self.stop_btn = QPushButton("停止")
        self.delete_btn = QPushButton("删除所选录音")
        for button in (self.export_btn, self.transcribe_btn, self.stop_btn, self.delete_btn):
            actions.addWidget(button)
        layout.addLayout(actions)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setPlaceholderText("恢复的识别文字将显示在这里")
        layout.addWidget(self.output)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.save_btn = QPushButton("另存文字")
        layout.addWidget(self.save_btn)
        self.output.textChanged.connect(self._sync_save_enabled)
        self.export_btn.clicked.connect(self._export)
        self.transcribe_btn.clicked.connect(self._transcribe)
        self.stop_btn.clicked.connect(self.cancel_processing)
        self.delete_btn.clicked.connect(self._delete)
        self.save_btn.clicked.connect(self._save_text)
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
        self.setStyleSheet(
            settings_styles.WINDOW_CSS
            + f"\nQDialog {{ background: {tok.BG}; }}"
            + f"\nQListWidget, QPlainTextEdit {{ background: {tok.INPUT_BG}; color: {tok.TEXT};"
            + f" border: 1px solid {tok.CONTROL_BORDER}; border-radius: {tok.RADIUS_MD}px; padding: 6px; }}"
        )
        for button in (self.export_btn, self.transcribe_btn, self.stop_btn, self.save_btn):
            button.setStyleSheet(settings_styles.BTN_GHOST_SM)
        self.delete_btn.setStyleSheet(settings_styles.BTN_DANGER_SM)
        palette = self.output.palette()
        palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(tok.TEXT_DIM))
        self.output.setPalette(palette)

    def _sync_save_enabled(self) -> None:
        has_text = bool(self.output.toPlainText().strip())
        self.save_btn.setEnabled((not self._busy) and has_text)

    @property
    def running_workers(self):
        return (self.worker,) if self.worker is not None else ()

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
            stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(item.metadata.get("started_at", 0)))
            state = "已结束" if item.metadata.get("complete") else "异常中断 / 缓存失败"
            self.items.addItem(f"{stamp} · {item.seconds / 60:.1f} 分钟 · {state}")
        if self.recordings:
            self.items.setCurrentRow(0)
        else:
            self.status.setText("没有录音缓存。开启「录音恢复」后，从下一次录音开始保存。")
        if self.recordings and self.recognizer is None:
            self.status.setText("模型尚未就绪，可先导出录音；模型载入后可重新识别。")
        self._set_busy(False)

    def _update_tracks(self, row):
        self.track.clear()
        if 0 <= row < len(self.recordings):
            titles = {"audio": "混合录音", "microphone": "麦克风原始音轨", "system": "电脑声音原始音轨"}
            for track in self.recordings[row].available_tracks:
                self.track.addItem(titles[track], track)

    def _selected(self):
        row = self.items.currentRow()
        return (replace(self.recordings[row], track=self.track.currentData() or "audio")
                if 0 <= row < len(self.recordings) else None)

    def _set_busy(self, busy):
        self._busy = bool(busy)
        for button in (self.export_btn, self.delete_btn):
            button.setEnabled(not busy and bool(self.recordings))
        self.transcribe_btn.setEnabled(not busy and bool(self.recordings) and self.recognizer is not None)
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
        if not self._worker_failed and self.worker is not None:
            self.status.setText("处理完成" if not self.worker.cancelled else "已停止，已有恢复文字保留")
        self._set_busy(False)
        self.worker = None

    def _export(self):
        if item := self._selected():
            target, _ = QFileDialog.getSaveFileName(self, "导出录音", "恢复录音.wav", "WAV (*.wav)")
            if target:
                self._start(RecoveryWorker(item, target=Path(target)))

    def _transcribe(self):
        if (item := self._selected()) and self.recognizer is not None:
            self.output.clear()
            self._start(
                RecoveryWorker(
                    item,
                    recognizer=self.recognizer,
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
        target, _ = QFileDialog.getSaveFileName(self, "另存恢复文字", "恢复文字.txt", "Text (*.txt)")
        if target:
            try:
                Path(target).write_text(self.output.toPlainText(), encoding="utf-8")
            except OSError as exc:
                self.status.setText(str(exc))

    def reject(self):
        if self.worker is not None and self.worker.isRunning():
            self.worker.cancelled = True
            self.status.setText("正在停止，请等待当前识别片段完成…")
            return
        super().reject()

    def closeEvent(self, event):
        if self.worker is not None and self.worker.isRunning():
            self.reject()
            event.ignore()
        else:
            super().closeEvent(event)

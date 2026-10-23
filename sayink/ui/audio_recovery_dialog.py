"""Inspect, export or re-transcribe opt-in local recording journals."""
from pathlib import Path
from dataclasses import replace
import time

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtGui import QFont, QColor, QPalette
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QListWidget,
                            QPushButton, QPlainTextEdit, QFileDialog, QMessageBox, QComboBox)

from sayink.audio_recovery import recordings, delete_recording
from sayink.audio_utils import TARGET_SAMPLE_RATE
from sayink.speech_recognizer import decode_asr_slice
from sayink.vad_segmenter import SpeechSegmenter, make_speech_gate
from sayink.ui import design_tokens as tok
from sayink.ui import settings_styles


class RecoveryWorker(QThread):
    text_ready = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, recording, *, target=None, recognizer=None, vad="energy", vad_path=None, quiet=False):
        super().__init__()
        self.recording = recording
        self.target = target
        self.recognizer = recognizer
        self.vad = vad
        self.vad_path = vad_path
        self.quiet = quiet
        self.cancelled = False
        self._active_quiet = False

    def run(self):
        try:
            if self.target is not None:
                self.recording.export_wav(self.target, cancelled=lambda: self.cancelled)
                return
            seg = SpeechSegmenter()
            seg.set_gate(make_speech_gate(self.vad, seg, model_path=self.vad_path))
            seg.quiet_speech = self.quiet and seg.gate_name == "silero"
            self._active_quiet = seg.quiet_speech
            if seg.gate_name == "silero":
                seg.gate.input_gain = 4.0 if self.quiet else 1.0
            # Same 100 ms blocks as live capture; bounded to one ASR slice.
            for block in self.recording.blocks(TARGET_SAMPLE_RATE // 10):
                if self.cancelled:
                    return
                audio = seg.feed(block)
                if audio is not None:
                    self._decode(audio)
            if not self.cancelled and (audio := seg.flush()) is not None:
                self._decode(audio)
        except Exception as exc:
            self.failed.emit(str(exc))

    def _decode(self, audio):
        text = decode_asr_slice(self.recognizer, audio, retry_empty=True,
                                cancelled=lambda: self.cancelled, quiet_speech=self._active_quiet)
        if not self.cancelled and text:
            self.text_ready.emit(text)


class AudioRecoveryDialog(QDialog):
    def __init__(self, root, *, recognizer=None, vad="energy", vad_path=None, quiet=False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("本地录音恢复")
        font = QFont(tok.UI_FONT_FAMILY)
        font.setPixelSize(tok.TYPE_BODY)
        self.setFont(font)
        self.resize(720, 580)
        self.root = root
        self.recognizer = recognizer
        self.vad = vad
        self.vad_path = vad_path
        self.quiet = quiet
        self.worker = None
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
        self.delete_btn = QPushButton("删除所选录音")
        for button in (self.export_btn, self.transcribe_btn, self.delete_btn):
            button.setMinimumHeight(36)
            button.setStyleSheet(settings_styles.BTN_GHOST_SM)
            actions.addWidget(button)
        layout.addLayout(actions)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setPlaceholderText("恢复的识别文字将显示在这里")
        palette = self.output.palette()
        palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(tok.TEXT_DIM))
        self.output.setPalette(palette)
        layout.addWidget(self.output)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.save_btn = QPushButton("另存文字")
        self.save_btn.setMinimumHeight(36)
        self.save_btn.setStyleSheet(settings_styles.BTN_GHOST_SM)
        layout.addWidget(self.save_btn)
        self.export_btn.clicked.connect(self._export)
        self.transcribe_btn.clicked.connect(self._transcribe)
        self.delete_btn.clicked.connect(self._delete)
        self.save_btn.clicked.connect(self._save_text)
        self._refresh()

    @property
    def running_workers(self):
        return (self.worker,) if self.worker is not None else ()

    def cancel_processing(self):
        if self.worker is not None:
            self.worker.cancelled = True

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
        for button in (self.export_btn, self.delete_btn):
            button.setEnabled(not busy and bool(self.recordings))
        self.transcribe_btn.setEnabled(not busy and bool(self.recordings) and self.recognizer is not None)
        self.items.setEnabled(not busy)
        self.track.setEnabled(not busy)
        self.save_btn.setEnabled(not busy)

    def _start(self, worker):
        self.worker = worker
        self._set_busy(True)
        self.status.setText("正在本机处理…")
        worker.text_ready.connect(self.output.appendPlainText)
        worker.failed.connect(lambda message: self.status.setText(f"处理失败：{message}"))
        worker.finished.connect(self._finished)
        worker.start()

    def _finished(self):
        if self.status.text() == "正在本机处理…":
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
            self._start(RecoveryWorker(item, recognizer=self.recognizer, vad=self.vad,
                                       vad_path=self.vad_path, quiet=self.quiet))

    def _delete(self):
        if (item := self._selected()) and QMessageBox.question(
                self, "删除录音", "永久删除所选录音缓存？已导出的文件和识别文字会保留。"
        ) == QMessageBox.StandardButton.Yes:
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

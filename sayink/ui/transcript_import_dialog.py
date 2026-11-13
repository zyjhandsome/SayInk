"""Paste or open a transcript before it becomes a meeting."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QEvent, QTimer, Qt
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from sayink.transcript_import import (
    TranscriptDecodeError,
    describe_import,
    parse_transcript,
    read_transcript_file,
)
from sayink.ui import design_tokens as tok
from sayink.ui import settings_styles as ss

_MAX_CHARS = 2_000_000
_MAX_FILE_BYTES = _MAX_CHARS * 4
_DEBOUNCE_THRESHOLD_CHARS = 8_000
_DEBOUNCE_DELAY_MS = 180
_DROP_SUFFIXES = {".txt", ".md", ".srt", ".vtt", ".json"}


class TranscriptImportDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("导入文字稿")
        self.setObjectName("transcriptImport")
        self.setAcceptDrops(True)
        self._parsed_key = None
        self._parsed_value = None
        self._file_error = ""
        self._loading_file = False

        self._parse_timer = QTimer(self)
        self._parse_timer.setSingleShot(True)
        self._parse_timer.setInterval(_DEBOUNCE_DELAY_MS)
        self._parse_timer.timeout.connect(self._refresh)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)

        self._hint = QLabel("粘贴文字稿，或打开 / 拖入 .txt、.md、.srt、.vtt、.json。导入后需要你自己点击「生成纪要」。")
        self._hint.setWordWrap(True)
        self._hint.setObjectName("transcriptImportHint")
        layout.addWidget(self._hint)

        self._title = QLineEdit()
        self._title.setPlaceholderText("会议标题（可空）")
        self._title.setAccessibleName("导入会议标题")
        layout.addWidget(self._title)

        tools = QHBoxLayout()
        self._file_btn = QPushButton("从文件打开…")
        self._file_btn.setAccessibleName("从文件打开文字稿")
        self._file_btn.clicked.connect(self._open_file)
        tools.addWidget(self._file_btn)
        tools.addStretch(1)
        layout.addLayout(tools)

        self._edit = QPlainTextEdit()
        self._edit.setPlaceholderText("在这里粘贴文字稿，或直接拖入文字稿 / 字幕文件。")
        self._edit.setAccessibleName("文字稿内容")
        self._edit.installEventFilter(self)
        self._edit.viewport().installEventFilter(self)
        self._edit.textChanged.connect(self._on_text_changed)
        layout.addWidget(self._edit, 1)

        self._detect = QCheckBox("自动识别说话人与时间")
        self._detect.setAccessibleName("自动识别说话人与时间")
        self._detect.setChecked(True)
        self._detect.toggled.connect(self._on_detect_toggled)
        layout.addWidget(self._detect)

        self._preview = QLabel("还没有可导入的内容")
        self._preview.setWordWrap(True)
        self._preview.setObjectName("transcriptImportPreview")
        layout.addWidget(self._preview)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch(1)
        self._cancel_btn = QPushButton("取消")
        self._cancel_btn.clicked.connect(self.reject)
        self._ok = QPushButton("导入")
        self._ok.setAccessibleName("导入")
        self._ok.setDefault(True)
        self._ok.clicked.connect(self._accept)
        buttons.addWidget(self._cancel_btn)
        buttons.addWidget(self._ok)
        layout.addLayout(buttons)

        for button in (self._file_btn, self._cancel_btn, self._ok):
            button.setCursor(Qt.CursorShape.PointingHandCursor)

        self._fit_to_screen(640, 560)
        self.reapply_theme()
        self._refresh()

    def _fit_to_screen(self, target_w: int, target_h: int) -> None:
        parent = self.parentWidget()
        screen = (
            self.screen()
            or (parent.screen() if parent is not None else None)
            or QApplication.primaryScreen()
        )
        if screen is None:
            self.resize(target_w, target_h)
            return
        avail = screen.availableGeometry()
        max_w = max(360, avail.width() - 24)
        max_h = max(320, avail.height() - 24)
        self.setMaximumSize(max_w, max_h)
        self.resize(min(target_w, max_w), min(target_h, max_h))

    def showEvent(self, event) -> None:
        self._fit_to_screen(self.width() or 640, self.height() or 560)
        super().showEvent(event)

    def reapply_theme(self) -> None:
        self.setStyleSheet(
            ss.WINDOW_CSS
            + f"""
            QDialog#transcriptImport {{
                background: {tok.BG};
                color: {tok.TEXT};
            }}
            QLabel#transcriptImportHint {{
                color: {tok.TEXT_SEC};
                font-size: {tok.TYPE_BODY_SM}px;
                background: transparent;
            }}
            QPlainTextEdit {{
                background: {tok.INPUT_BG};
                color: {tok.TEXT};
                border: 1px solid {tok.CONTROL_BORDER};
                border-radius: {tok.RADIUS_MD}px;
                padding: 8px 10px;
                font-size: {tok.TYPE_BODY_SM}px;
                selection-background-color: {tok.ACCENT_SOFT};
                selection-color: {tok.TEXT};
            }}
            QPlainTextEdit:focus {{
                border: 2px solid {tok.ACCENT_FOCUS};
                padding: 7px 9px;
            }}
            QCheckBox {{
                color: {tok.TEXT};
                font-size: {tok.TYPE_BODY_SM}px;
                spacing: 8px;
                padding: 2px 0;
                background: transparent;
            }}
            QCheckBox::indicator {{
                width: 16px;
                height: 16px;
                border-radius: 4px;
                border: 1px solid {tok.CONTROL_BORDER};
                background: {tok.INPUT_BG};
            }}
            QCheckBox::indicator:checked {{
                background: {tok.ACCENT};
                border-color: {tok.ACCENT};
            }}
            """
        )
        self._file_btn.setStyleSheet(ss.BTN_GHOST_SM)
        self._cancel_btn.setStyleSheet(ss.BTN_GHOST_SM)
        self._ok.setStyleSheet(ss.BTN_PRIMARY)
        self._style_preview(error=bool(self._file_error) or len(self.transcript_text()) > _MAX_CHARS)

    def _style_preview(self, *, error: bool) -> None:
        color = tok.RED if error else tok.TEXT_SEC
        weight = "700" if error else "400"
        self._preview.setStyleSheet(
            f"color: {color}; font-size: {tok.TYPE_FOOTNOTE}px; font-weight: {weight}; background: transparent;"
        )

    def transcript_text(self) -> str:
        return self._edit.toPlainText()

    def meeting_title(self) -> str:
        return self._title.text().strip()

    def detect_structure(self) -> bool:
        return self._detect.isChecked()

    def parsed(self):
        if self._parse_timer.isActive():
            self._parse_timer.stop()
        text = self.transcript_text()
        detect = self.detect_structure()
        key = (text, detect)
        if key != self._parsed_key:
            self._parsed_value = parse_transcript(text, detect_structure=detect)
            self._parsed_key = key
        return self._parsed_value

    def _on_text_changed(self) -> None:
        if not self._loading_file and self._file_error:
            self._file_error = ""
        text_len = self._edit.document().characterCount()
        if text_len > _MAX_CHARS:
            self._parse_timer.stop()
            self._refresh()
            return
        if text_len > _DEBOUNCE_THRESHOLD_CHARS:
            self._parse_timer.start()
            return
        self._parse_timer.stop()
        self._refresh()

    def _on_detect_toggled(self, _checked: bool) -> None:
        if self._file_error:
            return
        if self._edit.document().characterCount() > _DEBOUNCE_THRESHOLD_CHARS:
            self._parse_timer.start()
        else:
            self._refresh()

    def _refresh(self) -> None:
        if self._file_error:
            self._preview.setText(self._file_error)
            self._style_preview(error=True)
            self._ok.setEnabled(False)
            return
        text = self.transcript_text()
        if len(text) > _MAX_CHARS:
            self._preview.setText("文字稿过长，请拆开后再导入。")
            self._style_preview(error=True)
            self._ok.setEnabled(False)
            return
        parsed = self.parsed()
        self._preview.setText(describe_import(parsed))
        self._style_preview(error=False)
        self._ok.setEnabled(bool(parsed.turns))

    def _set_file_error(self, message: str) -> None:
        self._file_error = message
        self._preview.setText(message)
        self._style_preview(error=True)
        self._ok.setEnabled(False)

    def _load_file_path(self, path: str | Path) -> bool:
        target = Path(path)
        try:
            if target.stat().st_size > _MAX_FILE_BYTES:
                self._set_file_error("文件超过可导入的大小，已保留当前输入。请拆开后再导入。")
                return False
            text = read_transcript_file(target)
        except TranscriptDecodeError as exc:
            self._set_file_error(str(exc))
            return False
        except OSError as exc:
            self._set_file_error(f"无法读取文件：{exc}")
            return False
        self._file_error = ""
        self._loading_file = True
        try:
            self._parse_timer.stop()
            self._edit.setPlainText(text)
        finally:
            self._loading_file = False
        self._refresh()
        if not self._title.text().strip():
            self._title.setText(self.parsed().title or target.stem)
        return True

    def _open_file(self) -> None:
        path, _selected = QFileDialog.getOpenFileName(
            self,
            "打开文字稿",
            "",
            "文字稿 (*.txt *.md *.srt *.vtt *.json);;所有文件 (*.*)",
        )
        if not path:
            return
        self._load_file_path(path)

    @staticmethod
    def _dropped_local_files(mime_data) -> list[Path]:
        if mime_data is None or not mime_data.hasUrls():
            return []
        files: list[Path] = []
        for url in mime_data.urls():
            if url.isLocalFile():
                local = Path(url.toLocalFile())
                if local.exists() and local.is_file():
                    files.append(local)
        return files

    def _handle_drag_enter(self, event) -> bool:
        files = self._dropped_local_files(event.mimeData())
        if not files:
            return False
        event.acceptProposedAction()
        return True

    def _handle_drop(self, event) -> bool:
        files = self._dropped_local_files(event.mimeData())
        if not files:
            return False
        event.acceptProposedAction()
        chosen = files[0]
        if chosen.suffix.lower() not in _DROP_SUFFIXES:
            self._set_file_error(
                f"不支持直接导入 {chosen.suffix or '该类型'} 文件；请选择 .txt、.md、.srt、.vtt 或 .json。"
            )
            return True
        self._load_file_path(chosen)
        return True

    def dragEnterEvent(self, event) -> None:
        if not self._handle_drag_enter(event):
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        if not self._handle_drag_enter(event):
            super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:
        if not self._handle_drop(event):
            super().dropEvent(event)

    def eventFilter(self, watched, event) -> bool:
        if watched in (self._edit, self._edit.viewport()):
            if event.type() in (QEvent.Type.DragEnter, QEvent.Type.DragMove):
                if self._handle_drag_enter(event):
                    return True
            elif event.type() == QEvent.Type.Drop:
                if self._handle_drop(event):
                    return True
        return super().eventFilter(watched, event)

    def _accept(self) -> None:
        if self._file_error:
            return
        if not self.parsed().turns:
            return
        self.accept()

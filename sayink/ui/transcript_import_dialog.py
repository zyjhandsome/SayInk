"""Paste or open a transcript before it becomes a meeting."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtWidgets import (
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
from sayink.ui import settings_styles as ss

_MAX_CHARS = 2_000_000
_MAX_FILE_BYTES = _MAX_CHARS * 4


class TranscriptImportDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("导入文字稿")
        self.setObjectName("transcriptImport")
        self.resize(640, 560)
        self.setStyleSheet(ss.WINDOW_CSS)
        self._parsed_key = None
        self._parsed_value = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)

        hint = QLabel("粘贴文字稿，或打开 .txt、.md、.srt、.vtt。导入后需要你自己点击「生成纪要」。")
        hint.setWordWrap(True)
        layout.addWidget(hint)

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
        self._edit.setPlaceholderText("在这里粘贴文字稿。")
        self._edit.setAccessibleName("文字稿内容")
        self._edit.textChanged.connect(self._refresh)
        layout.addWidget(self._edit, 1)

        self._detect = QCheckBox("自动识别说话人与时间")
        self._detect.setAccessibleName("自动识别说话人与时间")
        self._detect.setChecked(True)
        self._detect.toggled.connect(self._refresh)
        layout.addWidget(self._detect)

        self._preview = QLabel("还没有可导入的内容")
        self._preview.setWordWrap(True)
        self._preview.setObjectName("transcriptImportPreview")
        layout.addWidget(self._preview)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        self._ok = QPushButton("导入")
        self._ok.setAccessibleName("导入")
        self._ok.setDefault(True)
        self._ok.clicked.connect(self._accept)
        buttons.addWidget(cancel)
        buttons.addWidget(self._ok)
        layout.addLayout(buttons)
        self._refresh()

    def transcript_text(self) -> str:
        return self._edit.toPlainText()

    def meeting_title(self) -> str:
        return self._title.text().strip()

    def detect_structure(self) -> bool:
        return self._detect.isChecked()

    def parsed(self):
        text = self.transcript_text()
        detect = self.detect_structure()
        key = (text, detect)
        if key != self._parsed_key:
            self._parsed_value = parse_transcript(text, detect_structure=detect)
            self._parsed_key = key
        return self._parsed_value

    def _refresh(self) -> None:
        text = self.transcript_text()
        if len(text) > _MAX_CHARS:
            self._preview.setText("文字稿过长，请拆开后再导入。")
            self._ok.setEnabled(False)
            return
        parsed = self.parsed()
        self._preview.setText(describe_import(parsed))
        self._ok.setEnabled(bool(parsed.turns))

    def _open_file(self) -> None:
        path, _selected = QFileDialog.getOpenFileName(
            self, "打开文字稿", "", "文字稿 (*.txt *.md *.srt *.vtt);;所有文件 (*.*)",
        )
        if not path:
            return
        try:
            if Path(path).stat().st_size > _MAX_FILE_BYTES:
                self._preview.setText("文件超过可导入的大小，已保留当前输入。请拆开后再导入。")
                return
            text = read_transcript_file(path)
        except TranscriptDecodeError as exc:
            self._preview.setText(str(exc))
            return
        except OSError as exc:
            self._preview.setText(f"无法读取文件：{exc}")
            return
        self._edit.setPlainText(text)
        if not self._title.text().strip():
            self._title.setText(self.parsed().title)

    def _accept(self) -> None:
        if not self.parsed().turns:
            return
        self.accept()

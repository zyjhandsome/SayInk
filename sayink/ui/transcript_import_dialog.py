"""Paste or open a transcript before it becomes a meeting."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QDateTime, QEvent, QSignalBlocker, QThread, QTimer, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QDateTimeEdit,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
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
from sayink.ui.native_dialog import native_file_dialog
from sayink.ui import settings_styles as ss
from sayink.ui.settings_components import ThemedCheckBox

_MAX_CHARS = 2_000_000
_MAX_FILE_BYTES = _MAX_CHARS * 4
_DEBOUNCE_THRESHOLD_CHARS = 8_000
_ASYNC_PARSE_THRESHOLD_CHARS = 50_000
_ASYNC_FILE_BYTES = 256 * 1024
_DEBOUNCE_DELAY_MS = 180
_DROP_SUFFIXES = {".txt", ".md", ".srt", ".vtt", ".json"}


class _PreviewParseWorker(QThread):
    finished_parse = pyqtSignal(int, object)

    def __init__(self, seq: int, text: str, detect: bool, parent=None) -> None:
        super().__init__(parent)
        self._seq = seq
        self._text = text
        self._detect = detect

    def run(self) -> None:
        parsed = parse_transcript(self._text, detect_structure=self._detect)
        self.finished_parse.emit(self._seq, parsed)


class _AsyncFileLoader(QThread):
    finished_load = pyqtSignal(int, str, str, object, str)

    def __init__(self, seq: int, path: Path, detect: bool, parent=None) -> None:
        super().__init__(parent)
        self._seq = seq
        self._path = path
        self._detect = detect

    def run(self) -> None:
        try:
            text = read_transcript_file(self._path, max_bytes=_MAX_FILE_BYTES)
            if len(text) > _MAX_CHARS:
                self.finished_load.emit(
                    self._seq,
                    str(self._path),
                    "",
                    None,
                    "文件超过可导入的大小，已保留当前输入。请拆开后再导入。",
                )
                return
            parsed = parse_transcript(text, detect_structure=self._detect)
            self.finished_load.emit(self._seq, str(self._path), text, parsed, "")
        except TranscriptDecodeError as exc:
            self.finished_load.emit(self._seq, str(self._path), "", None, str(exc))
        except OSError as exc:
            self.finished_load.emit(self._seq, str(self._path), "", None, f"无法读取文件：{exc}")


class TranscriptImportDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("导入文字稿")
        self.setObjectName("transcriptImport")
        self.setAcceptDrops(True)
        self._parsed_key = None
        self._parsed_value = None
        self._file_error = ""
        self._extra_note = ""
        self._loading_file = False
        self._text_dirty = False
        self._title_autofill = ""
        self._when_autofill = True
        self._applying_clock = False
        self._loaded_file_mtime_ms: int | None = None
        self._exact_started_ms: int | None = None
        self._drag_over = False
        self._drag_url_cache: tuple[tuple[str, ...], list[Path]] | None = None
        self._close_confirmed = False
        self._parse_seq = 0
        self._parse_worker: _PreviewParseWorker | None = None
        self._file_seq = 0
        self._file_worker: _AsyncFileLoader | None = None

        self._parse_timer = QTimer(self)
        self._parse_timer.setSingleShot(True)
        self._parse_timer.setInterval(_DEBOUNCE_DELAY_MS)
        self._parse_timer.timeout.connect(self._refresh)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)

        self._hint = QLabel(
            "粘贴文字稿，或打开 / 拖入 .txt、.md、.srt、.vtt、.json。"
            "最多约 200 万字。导入后可生成会议纪要（开启自动生成时将自动排队）。"
        )
        self._hint.setWordWrap(True)
        self._hint.setObjectName("transcriptImportHint")
        layout.addWidget(self._hint)

        identity = QHBoxLayout()
        identity.setSpacing(8)
        title_col = QVBoxLayout()
        title_col.setSpacing(4)
        self._title_label = QLabel("会议标题")
        self._title_label.setObjectName("transcriptImportHint")
        title_col.addWidget(self._title_label)
        self._title = QLineEdit()
        self._title.setPlaceholderText("可留空，打开文件时会用文件名")
        self._title.setAccessibleName("导入会议标题")
        title_col.addWidget(self._title)
        identity.addLayout(title_col, 1)
        when_col = QVBoxLayout()
        when_col.setSpacing(4)
        self._when_label = QLabel("会议时间")
        self._when_label.setObjectName("transcriptImportHint")
        when_col.addWidget(self._when_label)
        self._when = QDateTimeEdit(QDateTime.currentDateTime())
        self._when.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self._when.setCalendarPopup(True)
        self._when.setAccessibleName("导入会议时间")
        self._when.dateTimeChanged.connect(self._on_when_edited)
        when_col.addWidget(self._when)
        identity.addLayout(when_col, 0)
        layout.addLayout(identity)

        tools = QHBoxLayout()
        self._file_btn = QPushButton("从文件打开…")
        self._file_btn.setAccessibleName("从文件打开文字稿")
        self._file_btn.clicked.connect(self._open_file)
        tools.addWidget(self._file_btn)
        self._use_file_time_btn = QPushButton("使用文件修改时间")
        self._use_file_time_btn.setAccessibleName("使用文件修改时间")
        self._use_file_time_btn.setToolTip("将该文件的修改时间填入「会议时间」（不影响会议在列表中的最新置顶位置）")
        self._use_file_time_btn.clicked.connect(self._use_loaded_file_clock)
        self._use_file_time_btn.hide()
        tools.addWidget(self._use_file_time_btn)
        tools.addStretch(1)
        layout.addLayout(tools)

        self._body_label = QLabel("文字稿")
        self._body_label.setObjectName("transcriptImportHint")
        layout.addWidget(self._body_label)
        self._edit = QPlainTextEdit()
        self._edit.setPlaceholderText("在这里粘贴文字稿，或直接拖入文字稿 / 字幕文件。")
        self._edit.setAccessibleName("文字稿内容")
        self._edit.installEventFilter(self)
        self._edit.viewport().installEventFilter(self)
        self._edit.textChanged.connect(self._on_text_changed)
        layout.addWidget(self._edit, 1)

        self._detect = ThemedCheckBox("自动识别说话人与时间")
        self._detect.setAccessibleName("自动识别说话人与时间")
        self._detect.setToolTip("关闭后不拆发言人和时间，整篇按纯文本导入。")
        self._detect.setChecked(True)
        self._detect.toggled.connect(self._on_detect_toggled)
        layout.addWidget(self._detect)

        self._preview = QLabel("还没有可导入的内容")
        self._preview.setTextFormat(Qt.TextFormat.PlainText)
        self._preview.setWordWrap(True)
        self._preview.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._preview.setObjectName("transcriptImportPreview")
        layout.addWidget(self._preview)
        self._keep_current_btn = QPushButton("仍导入当前文字")
        self._keep_current_btn.setAccessibleName("仍导入当前文字")
        self._keep_current_btn.setToolTip("刚才的文件没有读入。框里原来的文字还可以导入。")
        self._keep_current_btn.clicked.connect(self._keep_current_text)
        self._keep_current_btn.hide()
        layout.addWidget(self._keep_current_btn, 0, Qt.AlignmentFlag.AlignLeft)

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

        for button in (
            self._file_btn,
            self._use_file_time_btn,
            self._keep_current_btn,
            self._cancel_btn,
            self._ok,
        ):
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
        QTimer.singleShot(0, self._edit.setFocus)

    def _cleanup_workers(self) -> None:
        self._parse_timer.stop()
        self._parse_seq += 1
        self._file_seq += 1
        for attr in ("_parse_worker", "_file_worker"):
            worker = getattr(self, attr, None)
            setattr(self, attr, None)
            if worker is not None and worker.isRunning():
                worker.wait(500)

    def done(self, result: int) -> None:
        self._cleanup_workers()
        super().done(result)

    def reject(self) -> None:
        if not self._allow_close():
            return
        self._cleanup_workers()
        super().reject()

    def closeEvent(self, event) -> None:
        if not self._allow_close():
            event.ignore()
            return
        self._cleanup_workers()
        super().closeEvent(event)

    def _has_draft(self) -> bool:
        return bool(self._text_dirty and self.transcript_text().strip())

    def _allow_close(self) -> bool:
        if self._close_confirmed or not self._has_draft():
            return True
        if not self._confirm_discard():
            return False
        self._close_confirmed = True
        return True

    def _confirm_discard(self) -> bool:
        box = QMessageBox(self)
        box.setWindowTitle("放弃导入")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText("文字稿还没有导入")
        box.setInformativeText("关闭后，框里的文字不会保存。")
        stay = box.addButton("继续编辑", QMessageBox.ButtonRole.RejectRole)
        discard = box.addButton("放弃", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(stay)
        box.exec()
        return box.clickedButton() is discard

    def _confirm_replace(self) -> bool:
        box = QMessageBox(self)
        box.setWindowTitle("替换文字稿")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText("框里已有文字，打开文件会替换它")
        box.setInformativeText("替换后不能用撤销找回现在的内容。")
        stay = box.addButton("保留当前文字", QMessageBox.ButtonRole.RejectRole)
        replace = box.addButton("替换", QMessageBox.ButtonRole.AcceptRole)
        box.setDefaultButton(stay)
        box.exec()
        return box.clickedButton() is replace

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
            """
        )
        self._file_btn.setStyleSheet(ss.BTN_GHOST_SM)
        self._use_file_time_btn.setStyleSheet(ss.BTN_GHOST_SM)
        self._keep_current_btn.setStyleSheet(ss.BTN_GHOST_SM)
        self._cancel_btn.setStyleSheet(ss.BTN_GHOST_SM)
        self._ok.setStyleSheet(ss.BTN_PRIMARY)
        text_len = max(0, self._edit.document().characterCount() - 1)
        self._style_preview(error=bool(self._file_error) or text_len > _MAX_CHARS)
        if self._drag_over:
            self._paint_drag_border()

    def _style_preview(self, *, error: bool) -> None:
        color = tok.RED if error else tok.TEXT
        weight = "700" if error else "400"
        self._preview.setStyleSheet(
            f"color: {color}; font-size: {tok.TYPE_BODY_SM}px; font-weight: {weight};"
            f" background: {tok.INPUT_BG}; border: 1px solid {tok.HAIRLINE};"
            f" border-radius: {tok.RADIUS_MD}px; padding: 8px 10px;"
        )

    def _paint_drag_border(self) -> None:
        self._edit.setStyleSheet(
            f"QPlainTextEdit {{ background: {tok.INPUT_BG}; color: {tok.TEXT};"
            f" border: 2px solid {tok.ACCENT}; border-radius: {tok.RADIUS_MD}px;"
            f" padding: 7px 9px; font-size: {tok.TYPE_BODY_SM}px; }}"
        )

    def transcript_text(self) -> str:
        return self._edit.toPlainText()

    def meeting_title(self) -> str:
        return self._title.text().strip()

    def detect_structure(self) -> bool:
        return self._detect.isChecked()

    def meeting_started_ms(self) -> int:
        if self._exact_started_ms is not None:
            return int(self._exact_started_ms)
        return int(self._when.dateTime().toMSecsSinceEpoch())

    def _on_when_edited(self, _value) -> None:
        if not self._applying_clock:
            self._when_autofill = False
            self._exact_started_ms = None

    def _keep_current_text(self) -> None:
        self._file_error = ""
        self._refresh()

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

    def _schedule_async_parse(self, text: str, detect: bool) -> None:
        self._parse_seq += 1
        seq = self._parse_seq
        worker = _PreviewParseWorker(seq, text, detect, self)
        self._parse_worker = worker
        worker.finished_parse.connect(
            lambda done_seq, parsed_obj, expected_key=(text, detect): self._on_async_parse_done(
                done_seq, expected_key, parsed_obj
            )
        )
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _on_async_parse_done(self, seq: int, key: tuple[str, bool], parsed_obj) -> None:
        if seq != self._parse_seq:
            return
        self._parsed_key = key
        self._parsed_value = parsed_obj
        self._parse_worker = None
        self._refresh()

    def _on_text_changed(self) -> None:
        text_len = max(0, self._edit.document().characterCount() - 1)
        if not self._loading_file:
            self._text_dirty = text_len > 0
            self._extra_note = ""
            if self._file_error:
                self._file_error = ""
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
        if max(0, self._edit.document().characterCount() - 1) > _DEBOUNCE_THRESHOLD_CHARS:
            self._parse_timer.start()
        else:
            self._refresh()

    def _refresh(self, *, force_sync: bool = False) -> None:
        doc_len = max(0, self._edit.document().characterCount() - 1)
        if doc_len > _MAX_CHARS:
            parts = []
            if self._file_error:
                parts.append(self._file_error)
            parts.append("文字稿过长，请拆开后再导入。")
            self._preview.setText("\n".join(parts))
            self._style_preview(error=True)
            self._ok.setEnabled(False)
            self._keep_current_btn.hide()
            return
        text = self.transcript_text()
        too_long = len(text) > _MAX_CHARS
        detect = self.detect_structure()
        key = (text, detect)
        if (
            not force_sync
            and self.isVisible()
            and text
            and not too_long
            and len(text) >= _ASYNC_PARSE_THRESHOLD_CHARS
            and key != self._parsed_key
        ):
            self._preview.setText("正在解析文字稿预览…")
            self._style_preview(error=bool(self._file_error))
            self._ok.setEnabled(False)
            self._schedule_async_parse(text, detect)
            return
        parsed = self.parsed() if text and not too_long else None
        parts: list[str] = []
        if self._file_error:
            parts.append(self._file_error)
        if too_long:
            parts.append("文字稿过长，请拆开后再导入。")
        if self._extra_note:
            parts.append(self._extra_note)
        if parsed is not None:
            parts.append(describe_import(parsed))
            parts.append(f"{len(text)} 字")
        elif not parts:
            parts.append("还没有可导入的内容")
        self._preview.setText("\n".join(parts))
        self._style_preview(error=bool(self._file_error) or too_long)
        self._ok.setEnabled(not self._file_error and not too_long and bool(parsed and parsed.turns))
        self._keep_current_btn.setVisible(
            bool(self._file_error) and not too_long and bool(parsed and parsed.turns)
        )

    def _set_file_error(self, message: str) -> None:
        self._file_error = message
        self._refresh(force_sync=True)

    def _apply_loaded_content(
        self,
        target: Path,
        text: str,
        *,
        preparsed=None,
        detect: bool | None = None,
    ) -> bool:
        self._file_error = ""
        self._loading_file = True
        try:
            self._parse_timer.stop()
            self._parse_seq += 1
            if preparsed is not None:
                eff_detect = self.detect_structure() if detect is None else bool(detect)
                if eff_detect == self.detect_structure():
                    self._parsed_key = (text, eff_detect)
                    self._parsed_value = preparsed
            with QSignalBlocker(self._edit):
                self._edit.setPlainText(text)
            self._text_dirty = False
        finally:
            self._loading_file = False
        self._refresh(force_sync=True)
        self._apply_file_title(self.parsed().title or target.stem)
        self._apply_file_clock(target)
        return True

    def _load_file_path(self, path: str | Path, *, pending_note: str = "") -> bool:
        target = Path(path)
        current_has_draft = bool(self._edit.document().characterCount() > 1 and self._edit.toPlainText().strip())
        try:
            file_size = target.stat().st_size
        except OSError as exc:
            self._set_file_error(f"无法读取文件：{exc}")
            return False
        if file_size > _MAX_FILE_BYTES:
            self._set_file_error("文件超过可导入的大小，已保留当前输入。请拆开后再导入。")
            return False
        if self.isVisible() and file_size >= _ASYNC_FILE_BYTES:
            if current_has_draft and not self._confirm_replace():
                return False
            self._file_seq += 1
            seq = self._file_seq
            self._file_btn.setEnabled(False)
            self._ok.setEnabled(False)
            self._preview.setText(f"正在读取「{target.name}」…")
            self._style_preview(error=False)
            detect_now = self.detect_structure()
            loader = _AsyncFileLoader(seq, target, detect_now, self)
            self._file_worker = loader
            loader.finished_load.connect(
                lambda done_seq, p_str, txt, parsed_obj, err, note=pending_note, d=detect_now: self._on_async_file_loaded(
                    done_seq, Path(p_str), txt, parsed_obj, err, note, d
                )
            )
            loader.finished.connect(loader.deleteLater)
            loader.start()
            return True
        try:
            text = read_transcript_file(target, max_bytes=_MAX_FILE_BYTES)
            if len(text) > _MAX_CHARS:
                self._set_file_error("文件超过可导入的大小，已保留当前输入。请拆开后再导入。")
                return False
        except TranscriptDecodeError as exc:
            self._set_file_error(str(exc))
            return False
        except OSError as exc:
            self._set_file_error(f"无法读取文件：{exc}")
            return False
        current = self._edit.toPlainText()
        if current.strip() and current != text and not self._confirm_replace():
            return False
        ok = self._apply_loaded_content(target, text)
        if ok and pending_note:
            self._extra_note = pending_note
            self._refresh(force_sync=True)
        return ok

    def _on_async_file_loaded(
        self,
        seq: int,
        target: Path,
        text: str,
        parsed_obj,
        error: str,
        pending_note: str,
        detect: bool,
    ) -> None:
        if seq != self._file_seq:
            return
        self._file_worker = None
        self._file_btn.setEnabled(True)
        if error:
            self._set_file_error(error)
            return
        self._extra_note = pending_note
        self._apply_loaded_content(target, text, preparsed=parsed_obj, detect=detect)

    def _apply_file_title(self, stem: str) -> None:
        current = self._title.text().strip()
        if current and current != self._title_autofill:
            return
        self._title.setText(stem)
        self._title_autofill = stem

    def _apply_file_clock(self, path: Path) -> None:
        self._loaded_file_mtime_ms = None
        try:
            millis = int(path.stat().st_mtime * 1000)
        except OSError:
            self._use_file_time_btn.hide()
            return
        if millis <= 0:
            self._use_file_time_btn.hide()
            return
        self._loaded_file_mtime_ms = millis
        self._use_file_time_btn.setVisible(True)

    def _use_loaded_file_clock(self) -> None:
        millis = self._loaded_file_mtime_ms
        if millis is None or millis <= 0:
            return
        self._applying_clock = True
        try:
            self._when.setDateTime(QDateTime.fromMSecsSinceEpoch(millis))
            self._exact_started_ms = millis
        finally:
            self._applying_clock = False

    def _open_file(self) -> None:
        with native_file_dialog(self):
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

    def _cached_dropped_files(self, mime_data, *, refresh: bool = False) -> list[Path]:
        if mime_data is None or not mime_data.hasUrls():
            return []
        raw_urls = tuple(url.toString() for url in mime_data.urls())
        if not refresh and self._drag_url_cache is not None and self._drag_url_cache[0] == raw_urls:
            return list(self._drag_url_cache[1])
        files = self._dropped_local_files(mime_data)
        self._drag_url_cache = (raw_urls, list(files))
        return files

    def _handle_drag_enter(self, event) -> bool:
        is_enter = event.type() == QEvent.Type.DragEnter
        files = self._cached_dropped_files(event.mimeData(), refresh=is_enter)
        if not files:
            return False
        event.acceptProposedAction()
        if not self._drag_over:
            self._drag_over = True
            self._paint_drag_border()
        return True

    def _handle_drop(self, event) -> bool:
        self._drag_url_cache = None
        files = self._dropped_local_files(event.mimeData())
        if not files:
            return False
        event.acceptProposedAction()
        if self._drag_over:
            self._drag_over = False
            self.reapply_theme()
        supported = [path for path in files if path.suffix.lower() in _DROP_SUFFIXES]
        if not supported:
            chosen = files[0]
            self._set_file_error(
                f"不支持直接导入 {chosen.suffix or '该类型'} 文件；请选择 .txt、.md、.srt、.vtt 或 .json。"
            )
            return True
        note = ""
        if len(files) > 1:
            note = f"一次只能导入一个文件，已打开「{supported[0].name}」，已忽略其余 {len(files) - 1} 个。"
        self._load_file_path(supported[0], pending_note=note)
        return True

    def dragLeaveEvent(self, event) -> None:
        self._drag_url_cache = None
        if self._drag_over:
            self._drag_over = False
            self.reapply_theme()
        super().dragLeaveEvent(event)

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
        self._refresh(force_sync=True)
        if not self._ok.isEnabled():
            return
        self.accept()

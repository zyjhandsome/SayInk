"""Lightweight pre-import confirmation dialog for single or batch audio/video files."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Sequence

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from sayink.audio_file_decoder import SUPPORTED_EXTENSIONS
from sayink.config import Config
from sayink.media_import import file_dialog_filter
from sayink.meeting_store import now_ms
from sayink.speech_recognizer import MODEL_REGISTRY, is_model_downloaded
from sayink.ui import design_tokens as tok
from sayink.ui import settings_styles as ss
from sayink.ui.native_dialog import native_file_dialog
from sayink.ui.settings_components import ElidedComboBox
from sayink.ui.theme import apply_window_dark_titlebar

_NATURAL_SPLIT_RE = re.compile(r"(\d+)")


def natural_media_sort_key(path: Path | str) -> tuple:
    """Sort filenames naturally so ``part_2.mp3`` precedes ``part_10.mp3``."""
    p = path if isinstance(path, Path) else Path(path)
    parts = _NATURAL_SPLIT_RE.split(p.name.casefold())
    return tuple(int(part) if part.isdigit() else part for part in parts)


def _normalize_media_path(path: Path | str) -> str:
    p = path if isinstance(path, Path) else Path(path)
    try:
        return str(p.resolve()).casefold()
    except OSError:
        return str(p).casefold()


def _file_mtime_ms(path: Path) -> int | None:
    try:
        millis = int(path.stat().st_mtime * 1000)
    except OSError:
        return None
    return millis if millis > 0 else None


class MediaImportDialog(QDialog):
    """Confirm file order, recognition language, ASR model, and meeting timestamp mode."""

    def __init__(
        self,
        paths: Sequence[Path | str] = (),
        *,
        config: Config | None = None,
        existing_audio_paths: set[str] | None = None,
        queueing: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._queueing = bool(queueing)
        self._existing_norm_paths: set[str] = {
            _normalize_media_path(p) for p in (existing_audio_paths or ()) if str(p or "").strip()
        }
        self._paths: list[Path] = []
        self.setWindowTitle("加入音视频识别队列" if self._queueing else "导入音视频")
        self.setObjectName("mediaImportDialog")
        self.setAcceptDrops(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)

        self._hint = QLabel(
            "确认待识别的音视频文件顺序与识别参数（可继续拖入文件，支持上下调整顺序）。"
        )
        self._hint.setWordWrap(True)
        self._hint.setObjectName("mediaImportHint")
        layout.addWidget(self._hint)

        list_tools = QHBoxLayout()
        list_tools.setSpacing(6)
        self._count_label = QLabel("待导入文件（0）")
        self._count_label.setObjectName("mediaImportHint")
        list_tools.addWidget(self._count_label)
        list_tools.addStretch(1)
        self._add_btn = QPushButton("继续添加…")
        self._add_btn.setAccessibleName("继续添加音视频文件")
        self._add_btn.clicked.connect(self._on_add_files)
        list_tools.addWidget(self._add_btn)
        self._sort_btn = QPushButton("按文件名排序")
        self._sort_btn.setAccessibleName("按文件名排序")
        self._sort_btn.clicked.connect(self._sort_naturally)
        list_tools.addWidget(self._sort_btn)
        self._up_btn = QPushButton("上移")
        self._up_btn.setAccessibleName("上移所选文件")
        self._up_btn.clicked.connect(lambda: self._move_current(-1))
        list_tools.addWidget(self._up_btn)
        self._down_btn = QPushButton("下移")
        self._down_btn.setAccessibleName("下移所选文件")
        self._down_btn.clicked.connect(lambda: self._move_current(1))
        list_tools.addWidget(self._down_btn)
        self._remove_btn = QPushButton("移除")
        self._remove_btn.setAccessibleName("移除所选文件")
        self._remove_btn.clicked.connect(self._remove_current)
        list_tools.addWidget(self._remove_btn)
        layout.addLayout(list_tools)

        self._list = QListWidget()
        self._list.setAccessibleName("待导入音视频列表")
        self._list.currentRowChanged.connect(lambda _row: self._sync_controls())
        self._empty_list_hint = QLabel(
            "还没有添加音视频文件\n点击右上角「继续添加…」选择文件，或直接将音视频拖入此窗口"
        )
        self._empty_list_hint.setObjectName("mediaImportEmptyHint")
        self._empty_list_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_list_hint.setWordWrap(True)
        self._empty_list_hint.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        list_overlay = QVBoxLayout(self._list)
        list_overlay.setContentsMargins(16, 16, 16, 16)
        list_overlay.addWidget(self._empty_list_hint, 1, Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._list, 1)

        self._dup_hint = QLabel("")
        self._dup_hint.setWordWrap(True)
        self._dup_hint.setObjectName("mediaImportDupHint")
        self._dup_hint.hide()
        layout.addWidget(self._dup_hint)

        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        self._title_label = QLabel("会议标题")
        self._title_label.setObjectName("mediaImportHint")
        title_row.addWidget(self._title_label)
        self._title_edit = QLineEdit()
        self._title_edit.setAccessibleName("导入音视频会议标题")
        title_row.addWidget(self._title_edit, 1)
        layout.addLayout(title_row)

        opts_row = QHBoxLayout()
        opts_row.setSpacing(10)

        lang_col = QVBoxLayout()
        lang_col.setSpacing(4)
        self._lang_label = QLabel("识别语言")
        self._lang_label.setObjectName("mediaImportHint")
        lang_col.addWidget(self._lang_label)
        self._lang_combo = ElidedComboBox()
        self._lang_combo.setAccessibleName("导入识别语言")
        for label, code in (
            ("跟随设置 / 自动检测", ""),
            ("中文", "zh"),
            ("English", "en"),
            ("日本語", "ja"),
        ):
            self._lang_combo.addItem(label, code)
        lang_col.addWidget(self._lang_combo)
        opts_row.addLayout(lang_col, 1)

        model_col = QVBoxLayout()
        model_col.setSpacing(4)
        self._model_label = QLabel("语音模型")
        self._model_label.setObjectName("mediaImportHint")
        model_col.addWidget(self._model_label)
        self._model_combo = ElidedComboBox()
        self._model_combo.setAccessibleName("导入语音模型")
        self._model_combo.addItem("跟随会议 / 默认模型", "")
        for info in MODEL_REGISTRY:
            mid = str(info.get("id", "") or "")
            if mid and is_model_downloaded(mid):
                self._model_combo.addItem(str(info.get("name", mid)), mid)
        model_col.addWidget(self._model_combo)
        opts_row.addLayout(model_col, 1)

        time_col = QVBoxLayout()
        time_col.setSpacing(4)
        self._time_label = QLabel("会议时间")
        self._time_label.setObjectName("mediaImportHint")
        time_col.addWidget(self._time_label)
        self._time_combo = ElidedComboBox()
        self._time_combo.setAccessibleName("导入会议时间策略")
        self._time_combo.addItem("当前时间（推荐）", "now")
        self._time_combo.addItem("使用文件修改时间", "mtime")
        time_col.addWidget(self._time_combo)
        opts_row.addLayout(time_col, 1)

        layout.addLayout(opts_row)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self._cancel_btn = QPushButton("取消")
        self._cancel_btn.clicked.connect(self.reject)
        buttons.addWidget(self._cancel_btn)
        self._ok_btn = QPushButton("加入识别队列" if self._queueing else "开始导入")
        self._ok_btn.setAccessibleName("确认导入音视频")
        self._ok_btn.setDefault(True)
        self._ok_btn.clicked.connect(self.accept)
        buttons.addWidget(self._ok_btn)
        layout.addLayout(buttons)

        self._fit_to_screen(580, 460)
        self.add_paths(paths, sort_added=True)
        self.reapply_theme()

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
        if avail.isEmpty():
            self.resize(target_w, target_h)
            return
        max_w = max(360, avail.width() - 24)
        max_h = max(320, avail.height() - 24)
        self.setMinimumSize(min(460, max_w), min(360, max_h))
        self.setMaximumSize(max_w, max_h)
        self.resize(min(target_w, max_w), min(target_h, max_h))

    def showEvent(self, event) -> None:
        self._fit_to_screen(self.width() or 580, self.height() or 460)
        super().showEvent(event)

    # -- public API -----------------------------------------------------------

    def paths(self) -> list[Path]:
        return list(self._paths)

    def selected_language(self) -> str:
        return str(self._lang_combo.currentData() or "")

    def selected_model_id(self) -> str:
        return str(self._model_combo.currentData() or "")

    def time_mode(self) -> str:
        return str(self._time_combo.currentData() or "now")

    def import_requests(self) -> list[tuple[Path, str, dict[str, Any]]]:
        """Return ``(path, title, options_dict)`` in the user-confirmed queue order."""
        lang = self.selected_language()
        model_id = self.selected_model_id()
        mode = self.time_mode()
        base_now = now_ms()
        custom_single_title = (
            self._title_edit.text().strip() if len(self._paths) == 1 else ""
        )
        requests: list[tuple[Path, str, dict[str, Any]]] = []
        for idx, path in enumerate(self._paths):
            title = custom_single_title or path.stem
            opts: dict[str, Any] = {}
            if lang:
                opts["language"] = lang
            if model_id:
                opts["model_id"] = model_id
            if mode == "mtime":
                mtime = _file_mtime_ms(path)
                opts["started_at"] = mtime if mtime is not None else (base_now + idx)
            else:
                opts["started_at"] = base_now + idx
            requests.append((path, title, opts))
        return requests

    def add_paths(self, new_paths: Sequence[Path | str], *, sort_added: bool = True) -> int:
        seen = {_normalize_media_path(p) for p in self._paths}
        candidates: list[Path] = []
        for raw in new_paths:
            p = raw if isinstance(raw, Path) else Path(str(raw))
            if p.suffix.lower() not in SUPPORTED_EXTENSIONS or not p.is_file():
                continue
            norm = _normalize_media_path(p)
            if norm in seen:
                continue
            seen.add(norm)
            candidates.append(p)
        if sort_added and len(candidates) > 1:
            candidates.sort(key=natural_media_sort_key)
        if candidates:
            self._paths.extend(candidates)
            self._rebuild_list(select_row=max(0, len(self._paths) - len(candidates)))
        else:
            self._sync_controls()
        return len(candidates)

    # -- list manipulation ----------------------------------------------------

    def _rebuild_list(self, *, select_row: int = 0) -> None:
        self._list.blockSignals(True)
        self._list.clear()
        dup_count = 0
        for idx, path in enumerate(self._paths, start=1):
            is_dup = _normalize_media_path(path) in self._existing_norm_paths
            if is_dup:
                dup_count += 1
            label = f"{idx}. {path.name}" + ("  · 已导入过" if is_dup else "")
            item = QListWidgetItem(label)
            item.setToolTip(str(path))
            self._list.addItem(item)
        if self._paths:
            clamped = max(0, min(select_row, len(self._paths) - 1))
            self._list.setCurrentRow(clamped)
        self._list.blockSignals(False)
        if dup_count > 0:
            self._dup_hint.setText(
                f"提示：列表中有 {dup_count} 个文件此前已导入过。若无需用新参数重新识别，可将其选中后点「移除」。"
            )
            self._dup_hint.setVisible(True)
        else:
            self._dup_hint.hide()
        self._sync_controls()

    def _sync_controls(self) -> None:
        total = len(self._paths)
        row = self._list.currentRow()
        self._count_label.setText(f"待导入文件（{total}）")
        if hasattr(self, "_empty_list_hint"):
            self._empty_list_hint.setVisible(total == 0)
        self._sort_btn.setEnabled(total > 1)
        self._up_btn.setEnabled(total > 1 and row > 0)
        self._down_btn.setEnabled(total > 1 and 0 <= row < total - 1)
        self._remove_btn.setEnabled(total > 0 and 0 <= row < total)
        self._ok_btn.setEnabled(total > 0)
        if total == 1:
            self._title_edit.setEnabled(True)
            self._title_edit.setPlaceholderText(f"留空默认使用：{self._paths[0].stem}")
        elif total > 1:
            self._title_edit.setEnabled(False)
            self._title_edit.clear()
            self._title_edit.setPlaceholderText("批量导入时自动使用各自文件名作为会议标题")
        else:
            self._title_edit.setEnabled(False)
            self._title_edit.clear()
            self._title_edit.setPlaceholderText("请先添加音视频文件")

    def _on_add_files(self) -> None:
        with native_file_dialog(self):
            chosen, _ = QFileDialog.getOpenFileNames(
                self, "添加音视频文件", "", file_dialog_filter()
            )
        if chosen:
            self.add_paths([Path(p) for p in chosen], sort_added=True)

    def _sort_naturally(self) -> None:
        if len(self._paths) <= 1:
            return
        self._paths.sort(key=natural_media_sort_key)
        self._rebuild_list(select_row=0)

    def _move_current(self, delta: int) -> None:
        row = self._list.currentRow()
        target = row + delta
        if not (0 <= row < len(self._paths) and 0 <= target < len(self._paths)):
            return
        self._paths[row], self._paths[target] = self._paths[target], self._paths[row]
        self._rebuild_list(select_row=target)

    def _remove_current(self) -> None:
        row = self._list.currentRow()
        if not (0 <= row < len(self._paths)):
            return
        del self._paths[row]
        self._rebuild_list(select_row=min(row, len(self._paths) - 1))

    # -- drag & drop ----------------------------------------------------------

    def dragEnterEvent(self, event) -> None:
        if self._extract_dropped_media(event):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        mime = event.mimeData()
        if mime is not None and mime.hasUrls():
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:
        media = self._extract_dropped_media(event)
        if media:
            event.acceptProposedAction()
            self.add_paths(media, sort_added=True)
            return
        super().dropEvent(event)

    @staticmethod
    def _extract_dropped_media(event) -> list[Path]:
        mime = event.mimeData()
        if mime is None or not mime.hasUrls():
            return []
        found: list[Path] = []
        for url in mime.urls():
            if not url.isLocalFile():
                continue
            p = Path(url.toLocalFile())
            if p.suffix.lower() in SUPPORTED_EXTENSIONS and p.is_file():
                found.append(p)
        return found

    # -- styling --------------------------------------------------------------

    def reapply_theme(self) -> None:
        self.setStyleSheet(
            ss.WINDOW_CSS
            + ss.build_combo_css()
            + f"""
            QDialog#mediaImportDialog {{
                background: {tok.BG};
                color: {tok.TEXT};
            }}
            QLabel#mediaImportHint {{
                color: {tok.TEXT_SEC};
                font-size: {tok.TYPE_FOOTNOTE}px;
                background: transparent;
            }}
            QLabel#mediaImportEmptyHint {{
                color: {tok.TEXT_DIM};
                font-size: {tok.TYPE_FOOTNOTE}px;
                background: transparent;
            }}
            QLabel#mediaImportDupHint {{
                color: {tok.AMBER_TEXT};
                background: {tok.AMBER_SOFT};
                border: 1px solid {tok.CALLOUT_BORDER};
                border-radius: {tok.RADIUS_MD}px;
                padding: 6px 10px;
                font-size: {tok.TYPE_FOOTNOTE}px;
            }}
            QListWidget {{
                background: {tok.INPUT_BG};
                color: {tok.TEXT};
                border: 1px solid {tok.CONTROL_BORDER};
                border-radius: {tok.RADIUS_MD}px;
                padding: 4px;
                font-size: {tok.TYPE_BODY_SM}px;
                outline: none;
            }}
            QListWidget::item {{
                padding: 6px 8px;
                border-radius: {tok.RADIUS_SM}px;
            }}
            QListWidget::item:hover:!selected {{
                background: {tok.ROW_HOVER};
                color: {tok.TEXT};
            }}
            QListWidget::item:selected {{
                background: {tok.ACCENT_SOFT};
                color: {tok.TEXT};
            }}
            QLineEdit:disabled {{
                color: {tok.TEXT_DIM};
                background: {tok.SURFACE_PEARL};
                border-color: {tok.HAIRLINE};
            }}
            """
        )
        for btn in (self._add_btn, self._sort_btn, self._up_btn, self._down_btn, self._remove_btn, self._cancel_btn):
            btn.setStyleSheet(ss.BTN_GHOST_SM)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._ok_btn.setStyleSheet(ss.BTN_ACCENT_SM)
        self._ok_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        apply_window_dark_titlebar(self, getattr(tok, "ACTIVE_THEME", "light") == "dark")

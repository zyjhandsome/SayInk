"""Model selection card for the settings model page."""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout,
)

from sayink.ui import design_tokens as tok
from sayink.ui import settings_styles as ss

_ACCURACY_LABELS = {5: "高精度", 4: "较高", 3: "均衡", 2: "一般", 1: "基础"}
_SPEED_LABELS = {5: "很快", 4: "较快", 3: "均衡", 2: "较慢", 1: "较慢"}
RATING_TOOLTIP = "准确性和速度为 SayInk 模型目录内的相对评级。"


def format_model_ratings(accuracy: int, speed: int) -> str:
    return (
        f"{_ACCURACY_LABELS.get(int(accuracy), '未知')} · "
        f"{_SPEED_LABELS.get(int(speed), '未知')}"
    )


class ModelCard(QFrame):
    action_clicked = pyqtSignal(str, str)

    def __init__(
        self,
        model_info: dict,
        is_downloaded: bool,
        is_active: bool,
        parent=None,
    ):
        super().__init__(parent)
        self._model_id = model_info["id"]
        self._info = model_info
        self._is_downloaded = is_downloaded
        self._is_active = is_active
        self._progress_bar = None
        self._action_btn = None
        self._select_btn = None
        self._delete_btn = None
        self._badge = None
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(0)

        head = QHBoxLayout()
        head.setSpacing(10)
        self._name_lbl = QLabel(self._info["name"])
        self._name_lbl.setProperty("viRole", "modelName")
        head.addWidget(self._name_lbl)
        from sayink.speech_recognizer import DEFAULT_MODEL_ID
        if self._is_active or self._model_id == DEFAULT_MODEL_ID:
            self._badge = QLabel("当前" if self._is_active else "推荐")
            self._badge.setProperty("viRole", "modelBadge")
            head.addWidget(self._badge)
        head.addStretch()
        self._size_lbl = QLabel(f"{self._info['size_mb']} MB")
        self._size_lbl.setProperty("viRole", "modelSize")
        head.addWidget(self._size_lbl)
        layout.addLayout(head)

        layout.addSpacing(8)

        self._desc_lbl = QLabel(self._info["description"])
        self._desc_lbl.setProperty("viRole", "modelDesc")
        self._desc_lbl.setWordWrap(True)
        layout.addWidget(self._desc_lbl)

        layout.addSpacing(6)

        self._meta_lbl = QLabel(
            f"{self._info['languages']} · "
            f"{format_model_ratings(self._info['accuracy'], self._info['speed'])}"
        )
        self._meta_lbl.setProperty("viRole", "modelMeta")
        self._meta_lbl.setWordWrap(True)
        self._meta_lbl.setToolTip(RATING_TOOLTIP)
        layout.addWidget(self._meta_lbl)

        layout.addSpacing(12)

        self._progress_bar = QProgressBar()
        self._progress_bar.setFixedHeight(4)
        self._progress_bar.setTextVisible(False)
        self._progress_bar.setVisible(False)
        layout.addWidget(self._progress_bar)

        actions = QHBoxLayout()
        actions.setSpacing(10)
        actions.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        btn_h = tok.CONTROL_BTN_SM_HEIGHT
        if self._is_downloaded:
            if not self._is_active:
                self._select_btn = QPushButton("使用此模型")
                self._select_btn.setFixedHeight(btn_h)
                self._select_btn.clicked.connect(
                    lambda: self.action_clicked.emit(self._model_id, "select")
                )
                actions.addWidget(self._select_btn)
            self._delete_btn = QPushButton("删除")
            self._delete_btn.setFixedHeight(btn_h)
            self._delete_btn.clicked.connect(
                lambda: self.action_clicked.emit(self._model_id, "delete")
            )
            actions.addWidget(self._delete_btn)
        else:
            self._action_btn = QPushButton("下载")
            self._action_btn.setFixedHeight(btn_h)
            self._action_btn.setMinimumWidth(96)
            self._action_btn.clicked.connect(
                lambda: self.action_clicked.emit(self._model_id, "download")
            )
            actions.addWidget(self._action_btn)
        actions.addStretch()
        layout.addLayout(actions)
        for button in self.findChildren(QPushButton):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setAccessibleName(f"{button.text()}：{self._info['name']}")
        self._error_label = QLabel()
        self._error_label.setWordWrap(True)
        self._error_label.hide()
        layout.addWidget(self._error_label)
        self.reapply_styles()

    def reapply_styles(self) -> None:
        """Paint from the currently activated token axis (never stale imports)."""
        ring = (
            f"1px solid {tok.ACCENT}"
            if self._is_active
            else f"1px solid {tok.HAIRLINE}"
        )
        self.setStyleSheet(
            f"ModelCard {{ background: {tok.SURFACE}; border: {ring};"
            f" border-radius: {tok.RADIUS_MD}px; }}"
        )
        self._name_lbl.setStyleSheet(
            f"color: {tok.TEXT}; font-size: {tok.TYPE_TITLE_SM}px; font-weight: 700;"
            f" background: transparent;"
        )
        if self._badge is not None:
            self._badge.setStyleSheet(
                f"background: {tok.SURFACE_PEARL}; color: {tok.TEXT_SEC};"
                f" border-radius: {tok.RADIUS_PILL}px;"
                f"padding: 3px 9px; font-size: {tok.TYPE_CAPTION}px; font-weight: 700;"
            )
        self._size_lbl.setStyleSheet(
            f"color: {tok.TEXT_DIM}; font-size: {tok.TYPE_FOOTNOTE}px; background: transparent;"
        )
        self._desc_lbl.setStyleSheet(
            f"color: {tok.TEXT_SEC}; font-size: {tok.TYPE_BODY_SM}px; line-height: 1.45;"
            f" background: transparent;"
        )
        self._meta_lbl.setStyleSheet(
            f"color: {tok.TEXT_DIM}; font-size: {tok.TYPE_FOOTNOTE}px; background: transparent;"
        )
        if self._progress_bar is not None:
            self._progress_bar.setStyleSheet(f"""
                QProgressBar {{
                    background: {tok.BAR_OFF}; border-radius: 2px; border: none;
                }}
                QProgressBar::chunk {{
                    background: {tok.GREEN}; border-radius: 2px;
                }}
            """)
        if self._select_btn is not None:
            self._select_btn.setStyleSheet(ss.BTN_ACCENT_SM)
        if self._delete_btn is not None:
            # Secondary ghost — same family as「更改存储…»; confirm dialog keeps safety.
            self._delete_btn.setStyleSheet(ss.BTN_DANGER_SM)
        if self._action_btn is not None:
            self._action_btn.setStyleSheet(ss.BTN_ACCENT_SM)
        self._error_label.setStyleSheet(f"color: {tok.RED}; font-size: {tok.TYPE_FOOTNOTE}px; padding-top: 8px;")

    def set_download_progress(self, pct: int):
        self._error_label.hide()
        if self._progress_bar:
            self._progress_bar.setVisible(True)
            self._progress_bar.setValue(pct)
        if self._action_btn:
            self._action_btn.setEnabled(False)
            self._action_btn.setText(f"{pct}%")

    def set_download_error(self, msg: str):
        self._error_label.setText(f"下载失败：{msg}")
        self._error_label.show()
        if self._progress_bar:
            self._progress_bar.setVisible(False)
        if self._action_btn:
            self._action_btn.setEnabled(True)
            self._action_btn.setText("重试")

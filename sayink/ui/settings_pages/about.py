"""About settings subpage: application updates, configuration summary, and local paths."""

from __future__ import annotations

from datetime import datetime

from PyQt6.QtCore import QSize, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices, QIcon, QPainter
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from sayink.config import Config, DEFAULT_HOTKEY, VERSION, format_hotkey
from sayink.ui import design_tokens as tok
from sayink.ui import settings_styles
from sayink.ui.dictation_hints import dictation_hints
from sayink.ui.settings_components import (
    SettingsPage,
    ToggleOptionRow,
    footnote,
    group_divider,
    page_header,
    settings_group,
    settings_section,
)
from sayink.ui.settings_pages.general import set_section_visible, without_scroll_jump
from sayink.updater import ISSUES_URL

FEEDBACK_HINT = "遇到问题或有建议？到 GitHub 提 issue，附上「文件位置」里日志目录的最新日志会更快定位。"
SHOW_FEEDBACK_ENTRY = False


def open_feedback_page() -> bool:
    return bool(QDesktopServices.openUrl(QUrl(ISSUES_URL)))


class AboutSummaryRow(QWidget):
    """A readable, selectable value beside its label, with an explicit action."""

    def __init__(self, key: str, value: str = "", *, action: str = "", callback=None):
        super().__init__()
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 6, 16, 6)
        lay.setSpacing(12)
        self.key_label = QLabel(key)
        self.key_label.setTextFormat(Qt.TextFormat.PlainText)
        lay.addWidget(self.key_label, 0, Qt.AlignmentFlag.AlignTop)
        self.value_label = QLabel(value)
        self.value_label.setTextFormat(Qt.TextFormat.PlainText)
        self.value_label.setWordWrap(True)
        self.value_label.setMinimumWidth(0)
        self.value_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.value_label.setAccessibleName(key)
        lay.addWidget(self.value_label, 1, Qt.AlignmentFlag.AlignVCenter)
        self.action_button: QPushButton | None = None
        if action:
            self.action_button = QPushButton(action)
            self.action_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.action_button.setAccessibleName(f"{action}：{key}")
            self.action_button.setMinimumHeight(28)
            if callback is not None:
                self.action_button.clicked.connect(callback)
            lay.addWidget(self.action_button, 0, Qt.AlignmentFlag.AlignVCenter)
        self.reapply_styles()

    def reapply_styles(self) -> None:
        self.key_label.setStyleSheet(
            f"color: {tok.TEXT_DIM}; font-size: {tok.TYPE_BODY_SM}px;"
            " font-weight: 400; background: transparent;"
        )
        self.key_label.ensurePolished()
        self.key_label.setFixedWidth(max(96, self.key_label.fontMetrics().horizontalAdvance("语音识别模型") + 8))
        value_color = tok.RED if self.property("outputWarning") else tok.TEXT_SEC
        self.value_label.setStyleSheet(
            f"color: {value_color}; font-size: {tok.TYPE_BODY_SM}px;"
            " font-weight: 400; background: transparent;"
        )
        if self.action_button is not None:
            self.action_button.setStyleSheet(
                f"QPushButton {{ color: {tok.ACCENT_TEXT}; background: transparent;"
                f" border: 2px solid transparent; border-radius: {tok.RADIUS_SM}px;"
                f" font-size: {tok.TYPE_FOOTNOTE}px; min-height: 20px; padding: 2px 6px; }}"
                f"QPushButton:hover {{ background: {tok.ROW_HOVER}; }}"
                f"QPushButton:pressed {{ background: {tok.NAV_SELECTED_BG}; }}"
                f"QPushButton:focus {{ border-color: {tok.ACCENT_FOCUS}; }}"
            )


class AboutSettingsPage(SettingsPage):
    """Strongly-typed About settings subpage owning update status, runtime summary, and file locations."""

    update_check_requested = pyqtSignal()
    update_install_requested = pyqtSignal()
    auto_update_toggled = pyqtSignal(bool)
    setting_navigation_requested = pyqtSignal(str, str)

    # Explicitly typed widget/control fields (eliminating 16 dynamic win._* injections)
    about_info_group: QFrame
    about_info_lay: QVBoxLayout
    about_version_label: QLabel
    about_update_status: QLabel
    about_update_checked_at: QLabel
    about_update_btn: QPushButton
    about_auto_update_row: ToggleOptionRow
    about_runtime_wrap: QFrame
    about_runtime_lay: QVBoxLayout
    about_summary_rows: dict[str, AboutSummaryRow]
    about_paths_toggle: QPushButton
    about_paths_wrap: QWidget
    about_paths_lay: QVBoxLayout
    about_feedback_group: QFrame
    about_feedback_hint: QLabel
    about_feedback_btn: QPushButton

    def __init__(
        self,
        config: Config | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.add(page_header("关于", "SayInk · 让语音成为文字"))
        self.about_info_group = settings_group()
        self.about_info_lay = QVBoxLayout(self.about_info_group)
        self.about_info_lay.setContentsMargins(0, 0, 0, 0)
        self.about_info_lay.setSpacing(0)

        self.about_version_label = QLabel(f"当前版本 {VERSION}")

        update_row = QWidget()
        update_lay = QHBoxLayout(update_row)
        update_lay.setContentsMargins(16, 12, 16, 12)
        update_lay.setSpacing(12)
        status_lay = QVBoxLayout()
        status_lay.setSpacing(4)
        status_lay.addWidget(self.about_version_label)
        self.about_update_status = QLabel("本次启动尚未检查。自动检查至少隔 24 小时，不自动下载。")
        self.about_update_status.setObjectName("aboutUpdateStatus")
        self.about_update_status.setWordWrap(True)
        self.about_update_status.setTextFormat(Qt.TextFormat.PlainText)
        self.about_update_checked_at = QLabel()
        self.about_update_checked_at.setWordWrap(True)
        self.refresh_update_check_time(config)
        status_lay.addWidget(self.about_update_status)
        status_lay.addWidget(self.about_update_checked_at)
        update_lay.addLayout(status_lay, 1)
        self.about_update_btn = QPushButton("检查更新")
        self.about_update_btn.setObjectName("aboutUpdateButton")
        self.about_update_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.about_update_btn.clicked.connect(self._handle_update_button)
        update_lay.addWidget(self.about_update_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        self.about_info_lay.addWidget(update_row)
        self.about_info_lay.addWidget(group_divider())
        self.about_auto_update_row = ToggleOptionRow(
            "自动检查更新", "启动时检查，距上次检查至少 24 小时；不自动下载。",
        )
        self.about_auto_update_row.toggled.connect(self.auto_update_toggled.emit)
        self.about_info_lay.addWidget(self.about_auto_update_row)
        self.add(settings_section("应用与更新", self.about_info_group))

        self.about_runtime_wrap = settings_group()
        self.about_runtime_lay = QVBoxLayout(self.about_runtime_wrap)
        self.about_runtime_lay.setContentsMargins(0, 0, 0, 0)
        self.about_runtime_lay.setSpacing(0)
        self.about_summary_rows = {}
        entries = (
            ("语音识别模型", "管理模型", "engine", ""),
            ("录音快捷键", "修改快捷键", "general", "hotkey"),
            ("音频来源", "调整", "general", "source"),
            ("录制方式", "调整", "general", "mode"),
            ("输出去向", "调整", "general", "output"),
            ("文字润色", "配置润色", "polish", "polish"),
        )
        for index, (key, action, target, focus) in enumerate(entries):
            if index:
                self.about_runtime_lay.addWidget(group_divider())
            row = AboutSummaryRow(
                key,
                action=action,
                callback=(
                    (lambda _checked=False, p=target, f=focus: self.setting_navigation_requested.emit(p, f))
                    if action else None
                ),
            )
            self.about_summary_rows[key] = row
            self.about_runtime_lay.addWidget(row)
        self.add(settings_section("当前配置", self.about_runtime_wrap))

        paths_group = settings_group()
        paths_lay = QVBoxLayout(paths_group)
        paths_lay.setContentsMargins(0, 0, 0, 0)
        paths_lay.setSpacing(0)
        self.about_paths_toggle = QPushButton("文件位置")
        self.about_paths_toggle.setObjectName("aboutPathsToggle")
        self.about_paths_toggle.setCheckable(True)
        self.about_paths_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.about_paths_toggle.setMinimumHeight(44)
        self.about_paths_wrap = QWidget()
        self.about_paths_wrap.setObjectName("aboutPaths")
        self.about_paths_lay = QVBoxLayout(self.about_paths_wrap)
        self.about_paths_lay.setContentsMargins(0, 0, 0, 0)
        self.about_paths_lay.setSpacing(0)

        self.about_paths_toggle.toggled.connect(self.set_paths_expanded)
        self.set_paths_expanded(False)
        paths_lay.addWidget(self.about_paths_toggle)
        paths_lay.addWidget(self.about_paths_wrap)
        self.add(paths_group)

        # Preserve the existing, deliberately hidden feedback entry.
        self.about_feedback_group = settings_group()
        feedback_layout = QHBoxLayout(self.about_feedback_group)
        feedback_layout.setContentsMargins(16, 12, 16, 12)
        feedback_layout.setSpacing(12)
        self.about_feedback_hint = footnote(FEEDBACK_HINT)
        feedback_layout.addWidget(self.about_feedback_hint, 1)
        self.about_feedback_btn = QPushButton("GitHub 反馈")
        self.about_feedback_btn.setObjectName("aboutFeedbackButton")
        self.about_feedback_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.about_feedback_btn.setAccessibleName("在 GitHub 提交反馈")
        self.about_feedback_btn.setToolTip(ISSUES_URL)
        self.about_feedback_btn.clicked.connect(open_feedback_page)
        feedback_layout.addWidget(self.about_feedback_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        if SHOW_FEEDBACK_ENTRY:
            self.add(self.about_feedback_group)
        else:
            self.about_feedback_group.setParent(self)
            self.about_feedback_group.hide()
        self.paint_update_row()
        self.set_compact()
        self.set_spacing(16)

    def _handle_update_button(self) -> None:
        if self.about_update_btn.text() == "下载并安装":
            self.update_install_requested.emit()
            return
        self.update_check_requested.emit()

    def set_paths_expanded(self, expanded: bool) -> None:
        def change() -> None:
            set_section_visible(self.about_paths_wrap, expanded)
            self.about_paths_toggle.setAccessibleName("收起文件位置" if expanded else "展开文件位置")
            self.paint_paths_toggle()

        without_scroll_jump(self.about_paths_toggle, change)

    def refresh_update_check_time(self, config: Config | None) -> None:
        stamp = config.get("update.last_check_at", 0) if config is not None else 0
        try:
            when = datetime.fromtimestamp(float(stamp)) if float(stamp or 0) > 0 else None
        except (TypeError, ValueError, OverflowError, OSError):
            when = None
        self.about_update_checked_at.setText(
            f"上次检查：{when:%Y-%m-%d %H:%M}" if when else "尚无检查记录"
        )
        self.about_update_status.setToolTip(
            "启动时若距上次成功检查已满 24 小时才会自动检查，本次启动未检查不代表没有检查记录。"
        )
        self.about_update_checked_at.setVisible(when is not None)

    def paint_paths_toggle(self) -> None:
        btn = self.about_paths_toggle
        btn.setStyleSheet(
            f"QPushButton#aboutPathsToggle {{ color: {tok.TEXT_SEC}; background: transparent;"
            f" border: 2px solid transparent; border-radius: {tok.RADIUS_MD}px;"
            f" font-size: {tok.TYPE_BODY_SM}px; text-align: left; padding: 10px 12px; }}"
            f"QPushButton#aboutPathsToggle:hover {{ background: {tok.ROW_HOVER}; color: {tok.TEXT}; }}"
            f"QPushButton#aboutPathsToggle:pressed {{ background: {tok.NAV_SELECTED_BG}; }}"
            f"QPushButton#aboutPathsToggle:focus {{ border-color: {tok.ACCENT_FOCUS}; }}"
        )
        arrow = QStyle.StandardPixmap.SP_ArrowDown if btn.isChecked() else QStyle.StandardPixmap.SP_ArrowRight
        pixmap = btn.style().standardIcon(arrow).pixmap(QSize(16, 16), btn.devicePixelRatioF())
        painter = QPainter(pixmap)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(pixmap.rect(), QColor(tok.TEXT_SEC))
        painter.end()
        btn.setIcon(QIcon(pixmap))
        btn.setIconSize(QSize(16, 16))

    def paint_update_row(self) -> None:
        self.about_version_label.setStyleSheet(
            f"color: {tok.TEXT_SEC}; font-size: {tok.TYPE_BODY_SM}px; font-weight: 400;"
            " background: transparent; border: none; padding: 0;"
        )
        self.about_update_status.setStyleSheet(
            f"color: {tok.TEXT_SEC}; font-size: {tok.TYPE_BODY_SM}px; background: transparent;"
        )
        self.about_update_checked_at.setStyleSheet(
            f"color: {tok.TEXT_DIM}; font-size: {tok.TYPE_CAPTION}px; background: transparent;"
        )
        btn = self.about_update_btn
        btn.setStyleSheet(settings_styles.BTN_GHOST_SM)
        btn.ensurePolished()
        btn.setFixedWidth(max(btn.fontMetrics().horizontalAdvance(t) for t in ("检查更新", "下载并安装")) + 36)
        self.about_feedback_btn.setStyleSheet(settings_styles.BTN_GHOST_SM)
        self.paint_paths_toggle()

    def apply_theme(self) -> None:
        self.paint_update_row()

    def set_update_status(
        self,
        config: Config | None,
        text: str,
        *,
        action: str = "check",
    ) -> None:
        self.about_update_status.setText(text)
        self.refresh_update_check_time(config)
        if action == "install":
            self.about_update_btn.setText("下载并安装")
            self.about_update_btn.setEnabled(True)
        elif action == "busy":
            self.about_update_btn.setEnabled(False)
        else:
            self.about_update_btn.setText("检查更新")
            self.about_update_btn.setEnabled(True)

    def refresh_runtime_rows(self, config: Config) -> None:
        """Update source / mode / output on About without rescanning models."""
        hints = dictation_hints(config)
        for key, value in (
            ("录音快捷键", format_hotkey(config.get("hotkey", DEFAULT_HOTKEY))),
            ("音频来源", hints.source),
            ("录制方式", hints.mode),
            ("输出去向", hints.output),
        ):
            row = self.about_summary_rows.get(key)
            if row is not None:
                row.value_label.setText(value)
        output_row = self.about_summary_rows.get("输出去向")
        if output_row is not None:
            output_row.setProperty("outputWarning", hints.output_warning)
            output_row.reapply_styles()

    def refresh_info(self, config: Config) -> None:
        from sayink.speech_recognizer import MODEL_REGISTRY, get_model_info, is_model_downloaded
        from sayink.text_polisher import polish_settings_complete

        active_id = config.get("stt.model_id", "")
        ai = get_model_info(active_id)
        active_name = ai["name"] if ai else "未选择"

        downloaded = [m for m in MODEL_REGISTRY if is_model_downloaded(m["id"])]
        total_mb = sum(m["size_mb"] for m in downloaded)

        llm_enabled = config.get("llm.enabled", False)
        llm_configured = polish_settings_complete(
            config.get("llm.api_url", ""),
            config.get("llm.api_key", ""),
            config.get("llm.model_name", ""),
        )
        hints = dictation_hints(config)
        runtime_items = [
            ("语音识别模型", active_name),
            ("录音快捷键", format_hotkey(config.get("hotkey", DEFAULT_HOTKEY))),
            ("音频来源", hints.source),
            ("录制方式", hints.mode),
            ("输出去向", hints.output),
            (
                "文字润色",
                "已开启 · 待验证"
                if llm_enabled and llm_configured
                else "已开启 · 未配置"
                if llm_enabled
                else "已关闭",
            ),
        ]
        for key, val in runtime_items:
            self.about_summary_rows[key].value_label.setText(val)
        output_row = self.about_summary_rows["输出去向"]
        output_row.setProperty("outputWarning", hints.output_warning)
        output_row.reapply_styles()

        while self.about_paths_lay.count():
            item = self.about_paths_lay.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()

        path_items = [
            ("已下载", f"{len(downloaded)} 个 · 约 {total_mb} MB"),
            ("模型目录", str(config.models_dir)),
            ("配置文件", str(config.config_dir / "config.json")),
            ("日志目录", str(config.config_dir / "logs")),
        ]
        for key, val in path_items:
            self.about_paths_lay.addWidget(group_divider())
            row = AboutSummaryRow(key, val)
            row.value_label.setToolTip(val)
            self.about_paths_lay.addWidget(row)

    def load_config(self, config: Config) -> None:
        self.about_auto_update_row.setChecked(bool(config.get("update.auto_check", True)))
        self.refresh_update_check_time(config)


def build_about_page(
    _owner: object = None,
    *,
    config: Config | None = None,
    parent: QWidget | None = None,
) -> AboutSettingsPage:
    """Construct a typed ``AboutSettingsPage`` without mutating external window state."""
    resolved_cfg = _owner if isinstance(_owner, Config) else config
    return AboutSettingsPage(config=resolved_cfg, parent=parent)

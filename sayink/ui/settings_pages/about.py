"""About page: application updates, configuration summary and local paths."""

from datetime import datetime

from PyQt6.QtCore import QSize, Qt, QUrl
from PyQt6.QtGui import QColor, QDesktopServices, QIcon, QPainter
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSizePolicy, QStyle, QVBoxLayout, QWidget

from sayink.config import VERSION
from sayink.ui import design_tokens as tok
from sayink.ui import settings_styles
from sayink.ui.settings_components import (
    SettingsPage, ToggleOptionRow, footnote, group_divider, page_header,
    settings_group, settings_section,
)
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
        self.action_button = None
        if action:
            self.action_button = QPushButton(action)
            self.action_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.action_button.setAccessibleName(f"{action}：{key}")
            self.action_button.setFixedHeight(28)
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
                f" font-size: {tok.TYPE_FOOTNOTE}px; min-height: 0; padding: 2px 6px; }}"
                f"QPushButton:hover {{ background: {tok.ROW_HOVER}; }}"
                f"QPushButton:pressed {{ background: {tok.NAV_SELECTED_BG}; }}"
                f"QPushButton:focus {{ border-color: {tok.ACCENT_FOCUS}; }}"
            )


def refresh_update_check_time(win) -> None:
    stamp = win._config.get("update.last_check_at", 0)
    try:
        when = datetime.fromtimestamp(float(stamp)) if float(stamp or 0) > 0 else None
    except (TypeError, ValueError, OverflowError, OSError):
        when = None
    win._about_update_checked_at.setText(
        f"上次检查：{when:%Y-%m-%d %H:%M}" if when else "尚无检查记录"
    )
    win._about_update_checked_at.setVisible(when is not None)


def paint_about_paths_toggle(win) -> None:
    btn = win._about_paths_toggle
    btn.setStyleSheet(
        f"QPushButton#aboutPathsToggle {{ color: {tok.TEXT_SEC}; background: transparent;"
        f" border: 2px solid transparent; border-radius: {tok.RADIUS_MD}px;"
        f" font-size: {tok.TYPE_BODY_SM}px; text-align: left; padding: 10px 12px; }}"
        f"QPushButton#aboutPathsToggle:hover {{ background: {tok.ROW_HOVER}; color: {tok.TEXT}; }}"
        f"QPushButton#aboutPathsToggle:pressed {{ background: {tok.NAV_SELECTED_BG}; }}"
        f"QPushButton#aboutPathsToggle:focus {{ border-color: {tok.ACCENT_FOCUS}; }}"
    )
    arrow = QStyle.StandardPixmap.SP_ArrowDown if btn.isChecked() else QStyle.StandardPixmap.SP_ArrowRight
    # Native arrows otherwise inherit the OS light palette inside a dark app.
    pixmap = btn.style().standardIcon(arrow).pixmap(QSize(16, 16), btn.devicePixelRatioF())
    painter = QPainter(pixmap)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    painter.fillRect(pixmap.rect(), QColor(tok.TEXT_SEC))
    painter.end()
    btn.setIcon(QIcon(pixmap))
    btn.setIconSize(QSize(16, 16))


def paint_about_update_row(win) -> None:
    win._about_version_label.setStyleSheet(
        f"color: {tok.TEXT_SEC}; font-size: {tok.TYPE_BODY_SM}px; font-weight: 400;"
        " background: transparent; border: none; padding: 0;"
    )
    win._about_update_status.setStyleSheet(
        f"color: {tok.TEXT_SEC}; font-size: {tok.TYPE_BODY_SM}px; background: transparent;"
    )
    win._about_update_checked_at.setStyleSheet(
        f"color: {tok.TEXT_DIM}; font-size: {tok.TYPE_CAPTION}px; background: transparent;"
    )
    btn = win._about_update_btn
    btn.setStyleSheet(settings_styles.BTN_GHOST_SM)
    btn.ensurePolished()
    btn.setFixedWidth(max(btn.fontMetrics().horizontalAdvance(t) for t in ("检查更新", "下载并安装")) + 36)
    feedback_btn = getattr(win, "_about_feedback_btn", None)
    if feedback_btn is not None:
        feedback_btn.setStyleSheet(settings_styles.BTN_GHOST_SM)
    if hasattr(win, "_about_paths_toggle"):
        paint_about_paths_toggle(win)


def build_about_page(win) -> QWidget:
    page = SettingsPage()
    # About is a short document; keep values readable even in a maximized window.
    page.widget().setMaximumWidth(760)
    page.add(page_header("关于", "SayInk · 让语音成为文字"))
    win._about_info_group = settings_group()
    win._about_info_lay = QVBoxLayout(win._about_info_group)
    win._about_info_lay.setContentsMargins(0, 0, 0, 0)
    win._about_info_lay.setSpacing(0)

    win._about_version_label = QLabel(f"当前版本 {VERSION}")

    update_row = QWidget()
    update_lay = QHBoxLayout(update_row)
    update_lay.setContentsMargins(16, 12, 16, 12)
    update_lay.setSpacing(12)
    status_lay = QVBoxLayout()
    status_lay.setSpacing(4)
    status_lay.addWidget(win._about_version_label)
    win._about_update_status = QLabel("本次启动尚未检查更新")
    win._about_update_status.setObjectName("aboutUpdateStatus")
    win._about_update_status.setWordWrap(True)
    win._about_update_status.setTextFormat(Qt.TextFormat.PlainText)
    win._about_update_checked_at = QLabel()
    win._about_update_checked_at.setWordWrap(True)
    refresh_update_check_time(win)
    status_lay.addWidget(win._about_update_status)
    status_lay.addWidget(win._about_update_checked_at)
    update_lay.addLayout(status_lay, 1)
    win._about_update_btn = QPushButton("检查更新")
    win._about_update_btn.setObjectName("aboutUpdateButton")
    win._about_update_btn.setCursor(Qt.CursorShape.PointingHandCursor)
    win._about_update_btn.clicked.connect(win._on_about_update_button)
    update_lay.addWidget(win._about_update_btn, 0, Qt.AlignmentFlag.AlignVCenter)
    win._about_info_lay.addWidget(update_row)
    win._about_info_lay.addWidget(group_divider())
    win._about_auto_update_row = ToggleOptionRow(
        "自动检查更新", "启动时检查，距上次检查至少 24 小时；不自动下载。",
    )
    win._about_auto_update_row.toggled.connect(win._on_about_auto_update_toggled)
    win._about_info_lay.addWidget(win._about_auto_update_row)
    page.add(settings_section("应用与更新", win._about_info_group))

    win._about_runtime_wrap = settings_group()
    win._about_runtime_lay = QVBoxLayout(win._about_runtime_wrap)
    win._about_runtime_lay.setContentsMargins(0, 0, 0, 0)
    win._about_runtime_lay.setSpacing(0)
    win._about_summary_rows = {}
    entries = (
        ("语音识别模型", "管理模型", "engine", ""),
        ("录音快捷键", "修改快捷键", "general", "hotkey"),
        ("音频来源", "", "", ""),
        ("录制方式", "", "", ""),
        ("输出去向", "", "", ""),
        ("文字润色", "配置润色", "polish", "polish"),
    )
    for index, (key, action, target, focus) in enumerate(entries):
        if index:
            win._about_runtime_lay.addWidget(group_divider())
        row = AboutSummaryRow(
            key, action=action,
            callback=lambda _checked=False, p=target, f=focus: win._open_about_setting(p, f),
        )
        win._about_summary_rows[key] = row
        win._about_runtime_lay.addWidget(row)
    page.add(settings_section("当前配置", win._about_runtime_wrap))

    paths_group = settings_group()
    paths_lay = QVBoxLayout(paths_group)
    paths_lay.setContentsMargins(0, 0, 0, 0)
    paths_lay.setSpacing(0)
    win._about_paths_toggle = QPushButton("文件位置")
    win._about_paths_toggle.setObjectName("aboutPathsToggle")
    win._about_paths_toggle.setCheckable(True)
    win._about_paths_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
    win._about_paths_toggle.setMinimumHeight(44)
    win._about_paths_wrap = QWidget()
    win._about_paths_wrap.setObjectName("aboutPaths")
    win._about_paths_lay = QVBoxLayout(win._about_paths_wrap)
    win._about_paths_lay.setContentsMargins(0, 0, 0, 0)
    win._about_paths_lay.setSpacing(0)

    def _set_paths_expanded(expanded: bool) -> None:
        def change() -> None:
            win._set_section_visible(win._about_paths_wrap, expanded)
            win._about_paths_toggle.setAccessibleName("收起文件位置" if expanded else "展开文件位置")
            paint_about_paths_toggle(win)

        win._without_scroll_jump(win._about_paths_toggle, change)

    win._about_paths_toggle.toggled.connect(_set_paths_expanded)
    _set_paths_expanded(False)
    paths_lay.addWidget(win._about_paths_toggle)
    paths_lay.addWidget(win._about_paths_wrap)
    page.add(paths_group)

    # Preserve the existing, deliberately hidden feedback entry.
    win._about_feedback_group = settings_group()
    feedback_layout = QHBoxLayout(win._about_feedback_group)
    feedback_layout.setContentsMargins(16, 12, 16, 12)
    feedback_layout.setSpacing(12)
    win._about_feedback_hint = footnote(FEEDBACK_HINT)
    feedback_layout.addWidget(win._about_feedback_hint, 1)
    win._about_feedback_btn = QPushButton("GitHub 反馈")
    win._about_feedback_btn.setObjectName("aboutFeedbackButton")
    win._about_feedback_btn.setCursor(Qt.CursorShape.PointingHandCursor)
    win._about_feedback_btn.setAccessibleName("在 GitHub 提交反馈")
    win._about_feedback_btn.setToolTip(ISSUES_URL)
    win._about_feedback_btn.clicked.connect(open_feedback_page)
    feedback_layout.addWidget(win._about_feedback_btn, 0, Qt.AlignmentFlag.AlignVCenter)
    if SHOW_FEEDBACK_ENTRY:
        page.add(win._about_feedback_group)
    else:
        win._about_feedback_group.setParent(page)
        win._about_feedback_group.hide()
    paint_about_update_row(win)
    page.set_compact()
    page.set_spacing(16)
    return page

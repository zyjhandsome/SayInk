"""About settings page."""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QStyle, QVBoxLayout, QWidget

from sayink.config import VERSION
from sayink.ui import design_tokens as tok
from sayink.ui.settings_components import (
    SettingsPage,
    ToggleOptionRow,
    footnote,
    group_divider,
    page_header,
    settings_group,
    settings_section,
)


def paint_about_update_row(win) -> None:
    win._about_update_status.setStyleSheet(
        f"color: {tok.TEXT_SEC}; font-size: {tok.TYPE_BODY_SM}px; background: transparent;"
    )
    win._about_update_btn.setStyleSheet(
        f"QPushButton#aboutUpdateButton {{"
        f" color: {tok.TEXT}; background: {tok.SURFACE_PEARL};"
        f" border: 1px solid {tok.HAIRLINE}; border-radius: {tok.RADIUS_PILL}px;"
        f" font-size: {tok.TYPE_CAPTION}px; font-weight: 700; padding: 6px 12px;"
        f"}}"
        f"QPushButton#aboutUpdateButton:hover {{ background: {tok.CHIP_BG_HOVER}; }}"
        f"QPushButton#aboutUpdateButton:disabled {{ color: {tok.TEXT_DIM}; }}"
    )
    sync_about_pill_widths(win)


_UPDATE_BUTTON_TEXTS = ("检查更新", "下载并安装")


def sync_about_pill_widths(win) -> None:
    label = getattr(win, "_about_version_label", None)
    btn = getattr(win, "_about_update_btn", None)
    if label is None or btn is None:
        return
    label.ensurePolished()
    btn.ensurePolished()
    btn_text_w = max(btn.fontMetrics().horizontalAdvance(t) for t in _UPDATE_BUTTON_TEXTS)
    label_text_w = label.fontMetrics().horizontalAdvance(label.text())
    # Stylesheet horizontal padding (12px button / 10px label) plus 1px borders.
    width = max(btn_text_w + 26, label_text_w + 22) + 8
    label.setFixedWidth(width)
    btn.setFixedWidth(width)


def build_about_page(win) -> QWidget:
    page = SettingsPage()
    page.add(page_header("关于", "SayInk · 让语音成为文字"))
    win._about_info_group = settings_group()
    win._about_info_lay = QVBoxLayout(win._about_info_group)
    win._about_info_lay.setContentsMargins(0, 0, 0, 0)
    win._about_info_lay.setSpacing(0)

    brand_row = QWidget()
    brand_lay = QHBoxLayout(brand_row)
    brand_lay.setContentsMargins(16, 8, 16, 8)
    brand_lay.setSpacing(12)
    brand_name = QLabel("SayInk")
    brand_name.setProperty("viRole", "kvKey")
    brand_name.setStyleSheet(
        f"color: {tok.TEXT}; font-size: {tok.TYPE_BODY_SM}px; font-weight: 700;"
        f" background: transparent;"
    )
    brand_lay.addWidget(brand_name)
    brand_lay.addStretch(1)
    win._about_version_label = QLabel(f"版本 {VERSION}")
    win._about_version_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    win._about_version_label.setStyleSheet(
        f"color: {tok.TEXT_SEC}; font-size: {tok.TYPE_CAPTION}px; font-weight: 700;"
        f" background: {tok.SURFACE_PEARL}; border: 1px solid {tok.HAIRLINE};"
        f" border-radius: {tok.RADIUS_PILL}px; padding: 3px 10px;"
    )
    brand_lay.addWidget(win._about_version_label)
    win._about_info_lay.addWidget(brand_row)

    win._about_info_lay.addWidget(group_divider())
    update_row = QWidget()
    update_lay = QHBoxLayout(update_row)
    update_lay.setContentsMargins(16, 10, 16, 10)
    update_lay.setSpacing(12)
    win._about_update_status = QLabel("点击检查是否有新版本")
    win._about_update_status.setObjectName("aboutUpdateStatus")
    win._about_update_status.setWordWrap(True)
    update_lay.addWidget(win._about_update_status, 1)
    win._about_update_btn = QPushButton("检查更新")
    win._about_update_btn.setObjectName("aboutUpdateButton")
    win._about_update_btn.setCursor(Qt.CursorShape.PointingHandCursor)
    paint_about_update_row(win)
    win._about_update_btn.clicked.connect(win._on_about_update_button)
    update_lay.addWidget(win._about_update_btn, 0, Qt.AlignmentFlag.AlignVCenter)
    win._about_info_lay.addWidget(update_row)

    win._about_auto_update_row = ToggleOptionRow(
        "自动检查更新",
        "每天启动后检查一次，不自动下载",
    )
    win._about_auto_update_row.toggled.connect(win._on_about_auto_update_toggled)
    win._about_info_lay.addWidget(win._about_auto_update_row)

    win._about_runtime_wrap = QWidget()
    win._about_runtime_lay = QVBoxLayout(win._about_runtime_wrap)
    win._about_runtime_lay.setContentsMargins(0, 0, 0, 0)
    win._about_runtime_lay.setSpacing(0)
    win._about_info_lay.addWidget(win._about_runtime_wrap)

    win._about_paths_toggle = QPushButton("文件位置")
    win._about_paths_toggle.setObjectName("aboutPathsToggle")
    win._about_paths_toggle.setCheckable(True)
    win._about_paths_toggle.setChecked(False)
    win._about_paths_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
    win._about_paths_toggle.setAccessibleName("展开文件位置")
    win._about_paths_toggle.setStyleSheet(
        f"QPushButton#aboutPathsToggle {{"
        f" color: {tok.TEXT_SEC}; background: transparent; border: 2px solid transparent;"
        f" font-size: {tok.TYPE_BODY_SM}px; font-weight: 400;"
        f" text-align: left; padding: 8px 14px;"
        f"}}"
        f"QPushButton#aboutPathsToggle:hover {{ color: {tok.TEXT}; }}"
        f"QPushButton#aboutPathsToggle:focus {{ border: {tok.FOCUS_RING}; }}"
    )
    win._about_paths_wrap = QWidget()
    win._about_paths_wrap.setObjectName("aboutPaths")
    win._about_paths_lay = QVBoxLayout(win._about_paths_wrap)
    win._about_paths_lay.setContentsMargins(0, 0, 0, 0)
    win._about_paths_lay.setSpacing(0)
    win._about_paths_wrap.setVisible(False)

    def _set_paths_expanded(expanded: bool) -> None:
        win._about_paths_wrap.setVisible(expanded)
        arrow = (
            QStyle.StandardPixmap.SP_ArrowDown
            if expanded
            else QStyle.StandardPixmap.SP_ArrowRight
        )
        win._about_paths_toggle.setIcon(win.style().standardIcon(arrow))
        win._about_paths_toggle.setAccessibleName(
            "收起文件位置" if expanded else "展开文件位置"
        )

    win._about_paths_toggle.toggled.connect(_set_paths_expanded)
    _set_paths_expanded(False)
    win._about_info_lay.addWidget(win._about_paths_toggle)
    win._about_info_lay.addWidget(win._about_paths_wrap)

    page.add(settings_section("", win._about_info_group))

    win._about_usage_tip = settings_group()
    win._about_usage_tip.setObjectName("settingsGroup")
    tip_layout = QVBoxLayout(win._about_usage_tip)
    tip_layout.setContentsMargins(16, 12, 16, 12)
    tip_layout.addWidget(footnote(""))
    page.add(win._about_usage_tip)
    page.set_compact()
    page.set_spacing(12)
    return page

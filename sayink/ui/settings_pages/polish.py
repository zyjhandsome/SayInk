"""AI service settings subpage: shared LLM connection, dictation polish, and meeting minutes."""

from __future__ import annotations

from PyQt6.QtCore import QSignalBlocker, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QFont, QFontMetrics
from PyQt6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from sayink.config import Config
from sayink.text_polisher import (
    LLM_MODE_CORRECT,
    LLM_MODE_POLISH,
    api_key_required,
    custom_prompt_or_empty,
    normalize_llm_mode,
    parse_proxy_bypass_suffixes,
    prompt_config_key,
)
from sayink.ui import design_tokens as tok
from sayink.ui import settings_styles
from sayink.ui.minutes_templates import MinutesTemplatesDialog, populate_template_combo
from sayink.ui.settings_components import (
    SettingsPage,
    ToggleOptionRow,
    footnote,
    group_divider,
    inline_action_row,
    labeled_row,
    page_header,
    polish_preview_content,
    settings_group,
    settings_section,
    stacked_field_row,
)
from sayink.ui.settings_pages.general import (
    _ghost_button,
    set_combo_by_data,
    set_section_visible,
    without_scroll_jump,
)

LLM_ACTION_LABELS = ("显示", "隐藏", "测试连接", "测试中…", "恢复默认", "管理模板")

LLM_MODE_HINT = (
    "润色：去掉口语赘词、补标点、理顺语序。"
    "纠错：只改同音错别字和「自定义词汇」里的术语写法，其余一字不动，改动过大时回退原文"
)


def llm_action_button_width() -> int:
    font = QFont(tok.UI_FONT_FAMILY)
    font.setPixelSize(tok.TYPE_BODY_SM)
    widest = max(QFontMetrics(font).horizontalAdvance(label) for label in LLM_ACTION_LABELS)
    return widest + tok.CONTROL_BTN_SM_PAD_H * 2 + 8


def _status_label() -> QLabel:
    label = QLabel("")
    label.setWordWrap(True)
    label.setStyleSheet(
        f"color: {tok.TEXT_SEC}; font-size: {tok.TYPE_FOOTNOTE}px;"
        f" background: transparent; padding: 0 16px 12px 16px;"
    )
    return label


class PolishSettingsPage(SettingsPage):
    """Strongly-typed AI Service / Polish settings subpage owning connection, polish, and minutes controls."""

    llm_enable_toggled = pyqtSignal(bool)
    llm_mode_changed = pyqtSignal(int)
    llm_fields_edited = pyqtSignal()
    prompt_reset_requested = pyqtSignal()
    llm_test_requested = pyqtSignal()
    minutes_fields_edited = pyqtSignal()
    minutes_test_requested = pyqtSignal()
    minutes_auto_toggled = pyqtSignal(bool)
    minutes_auto_reset_requested = pyqtSignal()
    manage_templates_requested = pyqtSignal()

    # Explicitly typed widget/control fields (eliminating 23 dynamic win._* injections)
    llm_url_edit: QLineEdit
    llm_key_edit: QLineEdit
    llm_key_toggle: QPushButton
    llm_model_edit: QLineEdit
    llm_test_btn: QPushButton
    llm_test_status: QLabel
    llm_connection_section: QWidget
    llm_enable_row: ToggleOptionRow
    llm_preview_divider: QWidget
    llm_preview_card: QWidget
    llm_prompt_container: QWidget
    llm_mode_combo: QComboBox
    prompt_reset_btn: QPushButton
    llm_prompt_edit: QTextEdit
    llm_prompt_timer: QTimer
    llm_prompt_mode: str
    minutes_model_edit: QLineEdit
    minutes_test_btn: QPushButton
    minutes_test_status: QLabel
    minutes_template_combo: QComboBox
    minutes_manage_btn: QPushButton
    minutes_language_combo: QComboBox
    minutes_auto_row: ToggleOptionRow
    minutes_auto_reset_btn: QPushButton

    def __init__(
        self,
        config: Config | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.llm_prompt_mode = LLM_MODE_POLISH

        self.add(page_header(
            "AI 服务",
            "接入一个大模型接口，用于转写润色和会议纪要。两项功能共用下方的接口配置，可分别开启。",
        ))
        self.add(self._build_connection_section())
        self.add(footnote(
            "支持 OpenAI、DeepSeek、通义千问、Ollama 等 OpenAI 兼容接口。"
            "地址以 http:// 或 https:// 开头即可。只有地址本身是 http:// 时，密钥和转写内容才会明文传输。"
        ))
        self.add(self._build_polish_section())
        self.add(self._build_minutes_section(config))

        self.llm_url_edit.editingFinished.connect(self.llm_fields_edited.emit)
        self.llm_key_edit.editingFinished.connect(self.llm_fields_edited.emit)
        self.llm_model_edit.editingFinished.connect(self.llm_fields_edited.emit)
        self.llm_prompt_timer = QTimer(self)
        self.llm_prompt_timer.setSingleShot(True)
        self.llm_prompt_timer.timeout.connect(self.llm_fields_edited.emit)
        self.llm_prompt_edit.textChanged.connect(
            lambda: self.llm_prompt_timer.start(600)
        )

    def _build_connection_section(self) -> QWidget:
        conn_group = settings_group()
        conn_lay = QVBoxLayout(conn_group)
        conn_lay.setContentsMargins(0, 0, 0, 0)
        conn_lay.setSpacing(0)
        self.llm_url_edit = QLineEdit()
        self.llm_url_edit.setAccessibleName("接口地址")
        self.llm_url_edit.setPlaceholderText("例如：https://api.deepseek.com/v1")
        self.llm_url_edit.setMinimumHeight(36)
        conn_lay.addWidget(stacked_field_row("接口地址", self.llm_url_edit))

        key_wrap = QWidget()
        key_row = QHBoxLayout(key_wrap)
        key_row.setContentsMargins(0, 0, 0, 0)
        key_row.setSpacing(8)
        self.llm_key_edit = QLineEdit()
        self.llm_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.llm_key_edit.setAccessibleName("API 密钥")
        self.llm_key_edit.setPlaceholderText("例如：sk-…（本机或局域网服务可留空）")
        self.llm_key_edit.setMinimumHeight(36)
        self.llm_key_toggle = QPushButton("显示")
        self.style_action_button(self.llm_key_toggle)
        self.llm_key_toggle.setCheckable(True)
        self.llm_key_toggle.toggled.connect(self.toggle_key_visibility)
        self.paint_key_toggle()
        key_row.addWidget(self.llm_key_edit, 1)
        key_row.addWidget(self.llm_key_toggle, 0, Qt.AlignmentFlag.AlignVCenter)
        conn_lay.addWidget(group_divider())
        conn_lay.addWidget(stacked_field_row("API 密钥", key_wrap))

        model_wrap = QWidget()
        model_row = QHBoxLayout(model_wrap)
        model_row.setContentsMargins(0, 0, 0, 0)
        model_row.setSpacing(8)
        self.llm_model_edit = QLineEdit()
        self.llm_model_edit.setAccessibleName("模型名称")
        self.llm_model_edit.setPlaceholderText("例如：deepseek-flash")
        self.llm_model_edit.setMinimumHeight(36)
        self.llm_test_btn = QPushButton("测试连接")
        self.style_action_button(self.llm_test_btn)
        self.llm_test_btn.setProperty("viBtn", "accentSm")
        self.llm_test_btn.setStyleSheet(settings_styles.BTN_ACCENT_SM)
        self.llm_test_btn.clicked.connect(self.llm_test_requested.emit)
        model_row.addWidget(self.llm_model_edit, 1)
        model_row.addWidget(self.llm_test_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        conn_lay.addWidget(group_divider())
        conn_lay.addWidget(stacked_field_row("模型名称", model_wrap))
        self.llm_test_status = _status_label()
        conn_lay.addWidget(self.llm_test_status)
        self.llm_connection_section = settings_section("接口配置", conn_group)
        return self.llm_connection_section

    def _build_polish_section(self) -> QWidget:
        wrap = QWidget()
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        polish_card = settings_group()
        polish_card_lay = QVBoxLayout(polish_card)
        polish_card_lay.setContentsMargins(0, 0, 0, 0)
        polish_card_lay.setSpacing(0)
        self.llm_enable_row = ToggleOptionRow(
            "启用文字润色",
            "关闭时直接输出原文；开启后，转写文字会发送至上方配置的服务",
        )
        self.llm_enable_row.toggled.connect(self._handle_enable_toggled)
        polish_card_lay.addWidget(self.llm_enable_row)
        self.llm_preview_divider = group_divider()
        polish_card_lay.addWidget(self.llm_preview_divider)
        self.llm_preview_card = polish_preview_content()
        polish_card_lay.addWidget(self.llm_preview_card)
        lay.addWidget(settings_section("文字润色", polish_card))

        self.llm_prompt_container = QWidget()
        p_lay = QVBoxLayout(self.llm_prompt_container)
        p_lay.setContentsMargins(0, 0, 0, 0)
        p_lay.setSpacing(10)
        mode_group = settings_group()
        mode_lay = QVBoxLayout(mode_group)
        mode_lay.setContentsMargins(0, 0, 0, 0)
        mode_lay.setSpacing(0)
        self.llm_mode_combo = QComboBox()
        self.llm_mode_combo.setAccessibleName("处理方式")
        self.llm_mode_combo.addItem("润色", LLM_MODE_POLISH)
        self.llm_mode_combo.addItem("纠错", LLM_MODE_CORRECT)
        self.llm_mode_combo.currentIndexChanged.connect(self._handle_mode_changed)
        mode_lay.addWidget(labeled_row("处理方式", self.llm_mode_combo, LLM_MODE_HINT))
        p_lay.addWidget(settings_section("", mode_group))
        prompt_group = settings_group()
        prompt_lay = QVBoxLayout(prompt_group)
        prompt_lay.setContentsMargins(16, 12, 16, 12)
        prompt_lay.setSpacing(8)
        prompt_head = QHBoxLayout()
        prompt_head.setContentsMargins(0, 0, 0, 0)
        prompt_head.setSpacing(8)
        prompt_head.addStretch(1)
        self.prompt_reset_btn = QPushButton("恢复默认")
        self.style_action_button(self.prompt_reset_btn)
        self.prompt_reset_btn.clicked.connect(self.prompt_reset_requested.emit)
        prompt_head.addWidget(self.prompt_reset_btn)
        prompt_lay.addLayout(prompt_head)
        self.llm_prompt_edit = QTextEdit()
        self.llm_prompt_edit.setAccessibleName("润色提示词")
        self.llm_prompt_edit.setFixedHeight(128)
        self.paint_prompt_edit()
        self.llm_prompt_edit.setPlaceholderText("留空则使用所选处理方式的内置提示词")
        prompt_lay.addWidget(self.llm_prompt_edit)
        p_lay.addWidget(settings_section("提示词", prompt_group))
        lay.addWidget(self.llm_prompt_container)
        return wrap

    def _build_minutes_section(self, config: Config | None) -> QWidget:
        group = settings_group()
        lay = QVBoxLayout(group)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        model_wrap = QWidget()
        model_row = QHBoxLayout(model_wrap)
        model_row.setContentsMargins(0, 0, 0, 0)
        model_row.setSpacing(8)
        self.minutes_model_edit = QLineEdit()
        self.minutes_model_edit.setAccessibleName("纪要模型名称")
        self.minutes_model_edit.setPlaceholderText("留空则与上方模型相同；长会议建议用更大的模型")
        self.minutes_model_edit.setMinimumHeight(36)
        self.minutes_model_edit.editingFinished.connect(self.minutes_fields_edited.emit)
        self.minutes_test_btn = QPushButton("测试连接")
        self.minutes_test_btn.setAccessibleName("测试纪要模型")
        self.minutes_test_btn.setToolTip("模型名称留空时，用上方的接口模型测试。文字润色关闭也可以测。")
        self.style_action_button(self.minutes_test_btn)
        self.minutes_test_btn.clicked.connect(self.minutes_test_requested.emit)
        model_row.addWidget(self.minutes_model_edit, 1)
        model_row.addWidget(self.minutes_test_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(stacked_field_row(
            "纪要模型名称",
            model_wrap,
            "接口地址与密钥用上方「接口配置」；生成纪要不需要开启「文字润色」",
        ))
        self.minutes_test_status = _status_label()
        self.minutes_test_status.hide()
        lay.addWidget(self.minutes_test_status)
        lay.addWidget(group_divider())
        self.minutes_template_combo = QComboBox()
        self.minutes_template_combo.setAccessibleName("默认会议模板")
        if config is not None:
            populate_template_combo(self.minutes_template_combo, config)
        self.minutes_template_combo.currentIndexChanged.connect(
            lambda _idx=0: self.minutes_fields_edited.emit()
        )
        self.minutes_manage_btn = QPushButton("管理模板")
        self.style_action_button(self.minutes_manage_btn)
        self.minutes_manage_btn.clicked.connect(self.manage_templates_requested.emit)
        template_wrap = QWidget()
        template_row = QHBoxLayout(template_wrap)
        template_row.setContentsMargins(0, 0, 0, 0)
        template_row.setSpacing(8)
        template_row.addWidget(self.minutes_template_combo, 1)
        template_row.addWidget(self.minutes_manage_btn)
        lay.addWidget(stacked_field_row("默认模板", template_wrap, "生成时可在会议页临时更换；管理模板可新建或复制后修改"))
        lay.addWidget(group_divider())
        self.minutes_language_combo = QComboBox()
        self.minutes_language_combo.setAccessibleName("纪要输出语言")
        self.minutes_language_combo.setToolTip("适用于会议纪要；原文精整始终保留原文语言和中外文混用。")
        for key, label in (("auto", "与会议语言相同"), ("zh", "中文"), ("en", "English"), ("ja", "日本語")):
            self.minutes_language_combo.addItem(label, key)
        self.minutes_language_combo.currentIndexChanged.connect(
            lambda _idx=0: self.minutes_fields_edited.emit()
        )
        lay.addWidget(labeled_row("纪要语言", self.minutes_language_combo))
        lay.addWidget(group_divider())
        self.minutes_auto_row = ToggleOptionRow(
            "会议结束后自动生成纪要",
            "用默认模板在后台生成，连续多场按顺序排队；文字稿会发送到上方配置的服务",
        )
        self.minutes_auto_row.toggled.connect(self.minutes_auto_toggled.emit)
        lay.addWidget(self.minutes_auto_row)
        self.minutes_auto_reset_btn = _ghost_button(
            "自动纪要恢复默认",
            self.minutes_auto_reset_requested.emit,
            tooltip="仅关闭会后自动生成纪要，不改变接口、密钥、纪要模板和已确认的远程地址",
        )
        lay.addWidget(inline_action_row(self.minutes_auto_reset_btn))
        return settings_section("会议纪要", group)

    # ── Page-owned UI & state helpers ──────────────────────

    def _handle_enable_toggled(self, enabled: bool) -> None:
        self.set_prompt_section_visible(enabled)
        self.llm_enable_toggled.emit(enabled)

    def _handle_mode_changed(self, index: int) -> None:
        self.sync_mode_preview()
        self.llm_mode_changed.emit(index)

    def style_action_button(self, btn: QPushButton) -> None:
        """Shared metrics so polish actions share one right-edge column."""
        btn.setProperty("viBtn", "ghostSm")
        btn.setAutoDefault(False)
        btn.setDefault(False)
        btn.setFixedWidth(llm_action_button_width())
        btn.setMinimumHeight(32)
        btn.setStyleSheet(settings_styles.BTN_GHOST_SM)

    def toggle_key_visibility(self, visible: bool) -> None:
        self.llm_key_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
        )
        self.llm_key_toggle.setText("隐藏" if visible else "显示")
        self.llm_key_toggle.setProperty("viBtn", "accentSm" if visible else "ghostSm")
        self.paint_key_toggle()

    def paint_key_toggle(self) -> None:
        visible = self.llm_key_toggle.isChecked()
        self.llm_key_toggle.setToolTip("当前正在显示密钥，点击隐藏" if visible else "点击显示密钥")
        self.llm_key_toggle.setAccessibleName("隐藏 API 密钥" if visible else "显示 API 密钥")
        self.llm_key_toggle.setStyleSheet(
            settings_styles.BTN_ACCENT_SM if visible else settings_styles.BTN_GHOST_SM
        )

    def paint_prompt_edit(self) -> None:
        self.llm_prompt_edit.setStyleSheet(f"""
            QTextEdit {{
                background: {tok.INPUT_BG}; color: {tok.TEXT};
                border: 1px solid {tok.HAIRLINE}; border-radius: {tok.RADIUS_MD}px;
                padding: 10px 12px; font-size: {tok.TYPE_BODY_SM}px; font-family: {tok.FONT};
            }}
            QTextEdit:focus {{
                border: 2px solid {tok.ACCENT_FOCUS};
                padding: 9px 11px;
            }}
        """)

    def paint_status_label(self, label: QLabel) -> None:
        tone = label.property("viTone") or "idle"
        if tone == "ok":
            color, bg, weight, pad = tok.GREEN_TEXT, tok.GREEN_BG, 600, "8px 16px"
        elif tone == "error":
            color, bg, weight, pad = tok.RED, tok.RED_BG, 600, "8px 16px"
        elif tone == "busy":
            color, bg, weight, pad = tok.TEXT, tok.SURFACE_PEARL, 600, "8px 16px"
        else:
            color, bg, weight, pad = tok.TEXT_SEC, "transparent", 400, "0 16px 12px 16px"
        label.setStyleSheet(
            f"color: {color}; background: {bg}; font-size: {tok.TYPE_FOOTNOTE}px;"
            f" font-weight: {weight}; border-radius: 6px; padding: {pad};"
        )

    def set_status(
        self,
        label: QLabel,
        text: str,
        tone: str,
        *,
        proxy_suffix_raw: str = "",
    ) -> None:
        if label is self.llm_test_status and proxy_suffix_raw:
            _valid, ignored = parse_proxy_bypass_suffixes(proxy_suffix_raw)
            if ignored:
                note = "这些直连后缀不会生效：" + "、".join(ignored)
                if note not in text:
                    text = f"{text} {note}".strip()
        label.setProperty("viTone", tone)
        label.setText(text)
        self.paint_status_label(label)

    def apply_theme(self) -> None:
        self.paint_prompt_edit()
        self.paint_status_label(self.llm_test_status)
        self.paint_status_label(self.minutes_test_status)
        self.paint_key_toggle()

    def hide_revealed_key(self) -> None:
        if self.llm_key_toggle.isChecked():
            self.llm_key_toggle.setChecked(False)

    def set_prompt_section_visible(self, enabled: bool) -> None:
        without_scroll_jump(
            self.llm_enable_row,
            lambda: set_section_visible(self.llm_prompt_container, enabled),
        )

    def selected_mode(self) -> str:
        return normalize_llm_mode(self.llm_mode_combo.currentData())

    def sync_mode_preview(self) -> str:
        mode = self.selected_mode()
        show_mode = getattr(self.llm_preview_card, "show_mode", None)
        if callable(show_mode):
            show_mode(mode)
        return mode

    def load_prompt_for_mode(self, config: Config, mode: str) -> None:
        self.llm_prompt_mode = mode
        blocker = QSignalBlocker(self.llm_prompt_edit)
        self.llm_prompt_edit.setPlainText(
            custom_prompt_or_empty(config.get(prompt_config_key(mode), ""))
        )
        del blocker

    def missing_fields(self) -> list[str]:
        url = self.llm_url_edit.text().strip()
        missing = []
        if not url:
            missing.append("接口地址")
        if not self.llm_key_edit.text().strip() and api_key_required(url):
            missing.append("API 密钥")
        if not self.llm_model_edit.text().strip():
            missing.append("模型名称")
        return missing

    def refresh_templates(self, config: Config, selected_id: str | None = None) -> None:
        if selected_id is None:
            populate_template_combo(self.minutes_template_combo, config)
        else:
            populate_template_combo(self.minutes_template_combo, config, selected_id)

    def open_templates_dialog(self, config: Config, parent: QWidget | None = None) -> bool:
        dialog = MinutesTemplatesDialog(
            config,
            self.minutes_template_combo.currentData(),
            parent or self.window(),
        )
        if dialog.exec() == dialog.DialogCode.Accepted:
            populate_template_combo(self.minutes_template_combo, config, dialog.selected_template_id)
            return True
        return False

    def load_config(self, config: Config) -> None:
        llm_on = bool(config.get("llm.enabled", False))
        self.llm_enable_row.setChecked(llm_on)
        set_section_visible(self.llm_prompt_container, llm_on)
        self.llm_url_edit.setText(config.get("llm.api_url", ""))
        self.llm_key_edit.setText(config.get("llm.api_key", ""))
        self.llm_model_edit.setText(config.get("llm.model_name", ""))
        set_combo_by_data(self.llm_mode_combo, config.get("llm.mode", "polish"))
        mode = self.sync_mode_preview()
        self.load_prompt_for_mode(config, mode)

    def load_minutes_config(self, config: Config) -> None:
        self.minutes_model_edit.setText(str(config.get("meeting.model_name", "") or ""))
        populate_template_combo(self.minutes_template_combo, config)
        set_combo_by_data(self.minutes_language_combo, config.get("meeting.output_language", "auto"))
        self.minutes_auto_row.setChecked(bool(config.get("meeting.auto_generate", False)))

    def focus_connection(self) -> None:
        self.ensureWidgetVisible(self.llm_connection_section)
        for edit in (self.llm_url_edit, self.llm_key_edit, self.llm_model_edit):
            if not edit.text().strip():
                edit.setFocus()
                return
        self.llm_url_edit.setFocus()


def build_polish_page(
    _owner: object = None,
    *,
    config: Config | None = None,
    parent: QWidget | None = None,
) -> PolishSettingsPage:
    """Construct a typed ``PolishSettingsPage`` without mutating external window state."""
    resolved_cfg = _owner if isinstance(_owner, Config) else config
    return PolishSettingsPage(config=resolved_cfg, parent=parent)

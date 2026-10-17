"""AI service settings page: shared LLM connection, dictation polish, meeting minutes."""

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from sayink.ui import design_tokens as tok

from sayink.ui.settings_components import (
    SettingsPage,
    ToggleOptionRow,
    footnote,
    group_divider,
    page_header,
    polish_preview_content,
    settings_group,
    settings_section,
    stacked_field_row,
)


def _status_label() -> QLabel:
    label = QLabel("")
    label.setWordWrap(True)
    label.setStyleSheet(
        f"color: {tok.TEXT_SEC}; font-size: {tok.TYPE_FOOTNOTE}px;"
        f" background: transparent; padding: 0 16px 12px 16px;"
    )
    return label


def _build_connection_section(win) -> QWidget:
    """「接口配置」: one endpoint shared by polish and minutes, visible whether or not polish is on."""
    from sayink.ui import settings_styles

    conn_group = settings_group()
    conn_lay = QVBoxLayout(conn_group)
    conn_lay.setContentsMargins(0, 0, 0, 0)
    conn_lay.setSpacing(0)
    win._llm_url_edit = QLineEdit()
    win._llm_url_edit.setAccessibleName("接口地址")
    win._llm_url_edit.setPlaceholderText("例如：https://api.deepseek.com/v1")
    win._llm_url_edit.setMinimumHeight(36)
    conn_lay.addWidget(stacked_field_row("接口地址", win._llm_url_edit))

    key_wrap = QWidget()
    key_row = QHBoxLayout(key_wrap)
    key_row.setContentsMargins(0, 0, 0, 0)
    key_row.setSpacing(8)
    win._llm_key_edit = QLineEdit()
    win._llm_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
    win._llm_key_edit.setAccessibleName("API 密钥")
    win._llm_key_edit.setPlaceholderText("例如：sk-…（本机或局域网服务可留空）")
    win._llm_key_edit.setMinimumHeight(36)
    win._llm_key_toggle = QPushButton("显示")
    win._style_llm_action_btn(win._llm_key_toggle)
    win._llm_key_toggle.setCheckable(True)
    win._llm_key_toggle.toggled.connect(win._toggle_llm_key_visibility)
    key_row.addWidget(win._llm_key_edit, 1)
    key_row.addWidget(win._llm_key_toggle, 0, Qt.AlignmentFlag.AlignVCenter)
    conn_lay.addWidget(group_divider())
    conn_lay.addWidget(stacked_field_row("API 密钥", key_wrap))

    model_wrap = QWidget()
    model_row = QHBoxLayout(model_wrap)
    model_row.setContentsMargins(0, 0, 0, 0)
    model_row.setSpacing(8)
    win._llm_model_edit = QLineEdit()
    win._llm_model_edit.setAccessibleName("模型名称")
    win._llm_model_edit.setPlaceholderText("例如：deepseek-chat")
    win._llm_model_edit.setMinimumHeight(36)
    win._llm_test_btn = QPushButton("测试连接")
    win._style_llm_action_btn(win._llm_test_btn)
    win._llm_test_btn.setProperty("viBtn", "accentSm")
    win._llm_test_btn.setStyleSheet(settings_styles.BTN_ACCENT_SM)
    win._llm_test_btn.clicked.connect(win._test_llm)
    model_row.addWidget(win._llm_model_edit, 1)
    model_row.addWidget(win._llm_test_btn, 0, Qt.AlignmentFlag.AlignVCenter)
    conn_lay.addWidget(group_divider())
    conn_lay.addWidget(stacked_field_row("模型名称", model_wrap))
    win._llm_test_status = _status_label()
    conn_lay.addWidget(win._llm_test_status)
    win._llm_connection_section = settings_section("接口配置", conn_group)
    return win._llm_connection_section


def _build_polish_section(win) -> QWidget:
    """「文字润色」: the switch and example always; the prompt only while it is on."""
    wrap = QWidget()
    lay = QVBoxLayout(wrap)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(10)

    polish_card = settings_group()
    polish_card_lay = QVBoxLayout(polish_card)
    polish_card_lay.setContentsMargins(0, 0, 0, 0)
    polish_card_lay.setSpacing(0)
    win._llm_enable_row = ToggleOptionRow(
        "启用文字润色",
        "关闭时直接输出原文；开启后，听写文字会发送至上方配置的服务",
    )
    win._llm_enable_row.toggled.connect(win._on_llm_enable_toggled)
    polish_card_lay.addWidget(win._llm_enable_row)
    win._llm_preview_divider = group_divider()
    polish_card_lay.addWidget(win._llm_preview_divider)
    win._llm_preview_card = polish_preview_content()
    polish_card_lay.addWidget(win._llm_preview_card)
    lay.addWidget(settings_section("文字润色", polish_card))

    win._llm_prompt_container = QWidget()
    p_lay = QVBoxLayout(win._llm_prompt_container)
    p_lay.setContentsMargins(0, 0, 0, 0)
    p_lay.setSpacing(0)
    prompt_group = settings_group()
    prompt_lay = QVBoxLayout(prompt_group)
    prompt_lay.setContentsMargins(16, 12, 16, 12)
    prompt_lay.setSpacing(8)
    prompt_head = QHBoxLayout()
    prompt_head.setContentsMargins(0, 0, 0, 0)
    prompt_head.setSpacing(8)
    prompt_head.addStretch(1)
    win._prompt_reset_btn = QPushButton("恢复默认")
    win._style_llm_action_btn(win._prompt_reset_btn)
    win._prompt_reset_btn.clicked.connect(win._reset_prompt)
    prompt_head.addWidget(win._prompt_reset_btn)
    prompt_lay.addLayout(prompt_head)
    win._llm_prompt_edit = QTextEdit()
    win._llm_prompt_edit.setAccessibleName("润色提示词")
    win._llm_prompt_edit.setFixedHeight(128)
    win._paint_llm_prompt_edit()
    win._llm_prompt_edit.setPlaceholderText("留空则使用内置默认提示词")
    prompt_lay.addWidget(win._llm_prompt_edit)
    p_lay.addWidget(settings_section("润色提示词", prompt_group))
    lay.addWidget(win._llm_prompt_container)
    return wrap


def _build_minutes_section(win) -> QWidget:
    """「会议纪要」: same endpoint and key as polish; its own model / template / language."""
    from PyQt6.QtWidgets import QComboBox

    from sayink.minutes_prompts import describe_template, list_templates
    from sayink.ui.settings_components import labeled_row

    group = settings_group()
    lay = QVBoxLayout(group)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)
    model_wrap = QWidget()
    model_row = QHBoxLayout(model_wrap)
    model_row.setContentsMargins(0, 0, 0, 0)
    model_row.setSpacing(8)
    win._minutes_model_edit = QLineEdit()
    win._minutes_model_edit.setAccessibleName("纪要模型名称")
    win._minutes_model_edit.setPlaceholderText("留空则与上方模型相同；长会议建议用更大的模型")
    win._minutes_model_edit.setMinimumHeight(36)
    win._minutes_model_edit.editingFinished.connect(win._flush_minutes_fields)
    win._minutes_test_btn = QPushButton("测试连接")
    win._minutes_test_btn.setAccessibleName("测试纪要模型")
    win._style_llm_action_btn(win._minutes_test_btn)
    win._minutes_test_btn.clicked.connect(win._test_minutes_model)
    model_row.addWidget(win._minutes_model_edit, 1)
    model_row.addWidget(win._minutes_test_btn, 0, Qt.AlignmentFlag.AlignVCenter)
    lay.addWidget(stacked_field_row("纪要模型名称", model_wrap,
                                    "接口地址与密钥用上方「接口配置」；生成纪要不需要开启「文字润色」"))
    win._minutes_test_status = _status_label()
    win._minutes_test_status.hide()
    lay.addWidget(win._minutes_test_status)
    lay.addWidget(group_divider())
    win._minutes_template_combo = QComboBox()
    win._minutes_template_combo.setAccessibleName("默认会议模板")
    for template in list_templates([]):
        win._minutes_template_combo.addItem(template.name, template.id)
        win._minutes_template_combo.setItemData(
            win._minutes_template_combo.count() - 1, describe_template(template), Qt.ItemDataRole.ToolTipRole
        )
    win._minutes_template_combo.currentIndexChanged.connect(win._flush_minutes_fields)
    lay.addWidget(labeled_row("默认模板", win._minutes_template_combo, "生成时可在会议页临时更换"))
    lay.addWidget(group_divider())
    win._minutes_language_combo = QComboBox()
    win._minutes_language_combo.setAccessibleName("纪要输出语言")
    win._minutes_language_combo.setToolTip("适用于会议纪要；原文精整始终保留原文语言和中外文混用。")
    for key, label in (("auto", "跟随会议语言"), ("zh", "中文"), ("en", "English"), ("ja", "日本語")):
        win._minutes_language_combo.addItem(label, key)
    win._minutes_language_combo.currentIndexChanged.connect(win._flush_minutes_fields)
    lay.addWidget(labeled_row("纪要语言", win._minutes_language_combo))
    return settings_section("会议纪要", group)


def build_polish_page(win) -> QWidget:
    page = SettingsPage()
    page.add(page_header(
        "AI 服务",
        "接入一个大模型接口，用于听写润色和会议纪要。两项功能共用下方的接口配置，可分别开启。",
    ))
    page.add(_build_connection_section(win))
    page.add(footnote(
        "支持 OpenAI、DeepSeek、通义千问、Ollama 等 OpenAI 兼容接口。"
        "远程接口须使用 HTTPS；本机或局域网地址可用 HTTP。"
    ))
    page.add(_build_polish_section(win))
    page.add(_build_minutes_section(win))

    win._llm_url_edit.editingFinished.connect(win._flush_llm_fields)
    win._llm_key_edit.editingFinished.connect(win._flush_llm_fields)
    win._llm_model_edit.editingFinished.connect(win._flush_llm_fields)
    win._llm_prompt_timer = QTimer(win)
    win._llm_prompt_timer.setSingleShot(True)
    win._llm_prompt_timer.timeout.connect(win._flush_llm_fields)
    win._llm_prompt_edit.textChanged.connect(
        lambda: win._llm_prompt_timer.start(600)
    )

    page.set_compact()
    page.set_spacing(10)
    return page

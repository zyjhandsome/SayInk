"""Polish (LLM) settings page."""

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


def build_polish_page(win) -> QWidget:
    page = SettingsPage()
    page.add(page_header("润色", "可选的文字整理：去除口头语、调整标点，保留你的原意。"))
    polish_card = settings_group()
    polish_card_lay = QVBoxLayout(polish_card)
    polish_card_lay.setContentsMargins(0, 0, 0, 0)
    polish_card_lay.setSpacing(0)
    win._llm_enable_row = ToggleOptionRow(
        "启用文字润色",
        "关闭时直接输出原文；开启后，文本会发送至你配置的服务",
    )
    win._llm_enable_row.toggled.connect(win._on_llm_enable_toggled)
    polish_card_lay.addWidget(win._llm_enable_row)

    win._llm_preview_divider = group_divider()
    polish_card_lay.addWidget(win._llm_preview_divider)
    win._llm_preview_card = polish_preview_content()
    polish_card_lay.addWidget(win._llm_preview_card)
    page.add(settings_section("", polish_card))

    win._llm_container = QWidget()
    c_lay = QVBoxLayout(win._llm_container)
    c_lay.setContentsMargins(0, 0, 0, 0)
    c_lay.setSpacing(16)

    conn_group = settings_group()
    conn_lay = QVBoxLayout(conn_group)
    conn_lay.setContentsMargins(0, 0, 0, 0)
    conn_lay.setSpacing(0)
    win._llm_url_edit = QLineEdit()
    win._llm_url_edit.setPlaceholderText("例如：https://api.deepseek.com/v1")
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
    win._llm_model_edit.setAccessibleName("润色模型名称")
    win._llm_model_edit.setPlaceholderText("例如：deepseek-chat")
    win._llm_model_edit.setMinimumHeight(36)
    win._llm_url_edit.setMinimumHeight(36)
    win._llm_test_btn = QPushButton("测试连接")
    win._style_llm_action_btn(win._llm_test_btn)
    from sayink.ui import settings_styles
    win._llm_test_btn.setProperty("viBtn", "accentSm")
    win._llm_test_btn.setStyleSheet(settings_styles.BTN_ACCENT_SM)
    win._llm_test_btn.clicked.connect(win._test_llm)
    model_row.addWidget(win._llm_model_edit, 1)
    model_row.addWidget(win._llm_test_btn, 0, Qt.AlignmentFlag.AlignVCenter)
    conn_lay.addWidget(group_divider())
    conn_lay.addWidget(stacked_field_row("模型名称", model_wrap))
    win._llm_test_status = QLabel("")
    win._llm_test_status.setWordWrap(True)
    win._llm_test_status.setStyleSheet(
        f"color: {tok.TEXT_SEC}; font-size: {tok.TYPE_FOOTNOTE}px;"
        f" background: transparent; padding: 0 16px 12px 16px;"
    )
    conn_lay.addWidget(win._llm_test_status)
    c_lay.addWidget(settings_section("接口配置", conn_group))

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
    c_lay.addWidget(settings_section("提示词", prompt_group))
    c_lay.addWidget(footnote(
        "支持 OpenAI、DeepSeek、通义千问、Ollama 等 OpenAI 兼容接口。"
    ))
    page.add(win._llm_container)

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

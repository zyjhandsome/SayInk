"""Task-oriented settings: recording, recognition, meetings and app/history.

Each section has its own builder; ``build_general_page`` only stacks them and
fixes the keyboard order. The builders attach the controls to ``win`` because
SettingsWindow reads/writes them when settings load or change.
"""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QProgressBar,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from sayink.ui import design_tokens as tok
from sayink.ui import settings_styles
from sayink.ui.hotkey_edit import HotkeyEdit
from sayink.ui.settings_components import (
    AudioSourcePicker,
    SettingsPage,
    ThemeModeSegment,
    ToggleOptionRow,
    TriggerModePicker,
    chain_tab_order,
    device_selection_link,
    footnote,
    group_divider,
    inline_action_row,
    labeled_row,
    page_header,
    settings_group,
    settings_section,
    stacked_field_row,
)


MIXED_AUDIO_NOTE = (
    "音频来源：麦克风 + 电脑声音。线上会议可同时记录双方；外放时建议佩戴耳机，避免重复收音。"
)
SYSTEM_AUDIO_NOTE = (
    "音频来源：电脑声音。只录制系统播放的音频，不包含你的麦克风；开始录音前不会采集声音。"
)


def _card() -> tuple[QWidget, QVBoxLayout]:
    card = settings_group()
    lay = QVBoxLayout(card)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)
    return card, lay


def _ghost_button(text: str, slot, *, height: int = 32, tooltip: str = "") -> QPushButton:
    btn = QPushButton(text)
    btn.setProperty("viBtn", "ghostSm")
    btn.setMinimumHeight(height)
    btn.setStyleSheet(settings_styles.BTN_GHOST_SM)
    if tooltip:
        btn.setToolTip(tooltip)
    btn.clicked.connect(slot)
    return btn


# ── 录音 ───────────────────────────────────────────────

def build_recording_section(win) -> QWidget:
    card, lay = _card()
    win._trigger_group = QButtonGroup(win)
    win._trigger_continuous_rb = QRadioButton()
    win._trigger_hotkey_rb = QRadioButton()
    for rb in (win._trigger_hotkey_rb, win._trigger_continuous_rb):
        win._trigger_group.addButton(rb)
        rb.toggled.connect(win._on_trigger_mode_radio_toggled)
    lay.addWidget(TriggerModePicker(win._trigger_continuous_rb, win._trigger_hotkey_rb))

    win._hotkey_edit = HotkeyEdit()
    win._hotkey_edit.setObjectName("HotkeyEdit")
    win._hotkey_edit.setMinimumHeight(40)
    win._hotkey_edit.capture_started.connect(win.hotkey_capture_started.emit)
    win._hotkey_edit.capture_ended.connect(win.hotkey_capture_ended.emit)
    win._hotkey_edit.hotkey_changed.connect(win._apply_hotkey_setting)
    win._hotkey_hint = QLabel()
    win._hotkey_hint.setWordWrap(True)
    win._hotkey_hint.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
    win._hotkey_hint.setStyleSheet(
        f"color: {tok.TEXT_SEC}; font-size: {tok.TYPE_FOOTNOTE}px; line-height: 1.4;"
        f" background: transparent; padding: 0 16px 12px 16px;"
    )
    lay.addWidget(stacked_field_row("录音快捷键", win._hotkey_edit))
    lay.addWidget(win._hotkey_hint)
    win._esc_stop_divider = group_divider()
    lay.addWidget(win._esc_stop_divider)
    win._esc_stop_row = ToggleOptionRow(
        "允许 Esc 结束持续转写",
        "结束后继续识别已录内容；关闭时只能点转写条「结束」",
    )
    win._esc_stop_row.toggled.connect(win._on_esc_stop_toggled)
    lay.addWidget(win._esc_stop_row)
    lay.addWidget(group_divider())
    source_title = footnote("音频来源")
    source_title.setProperty("viRole", "importantHint")
    lay.addWidget(inline_action_row(source_title, margins=(16, 12, 16, 0)))
    _build_source_picker(win, lay)
    _build_output_controls(win, lay)
    lay.addWidget(group_divider())
    lay.addWidget(_build_mic_test_row(win))
    win._audio_device_buttons = _build_advanced_audio(win, lay)
    section = settings_section("录音与输入", card)
    section.setObjectName("recordingInputSection")
    return section


# ── 音频 ───────────────────────────────────────────────

def _build_source_picker(win, lay: QVBoxLayout) -> None:
    win._source_group = QButtonGroup(win)
    win._src_mic_rb = QRadioButton()
    win._src_sys_rb = QRadioButton()
    win._src_mixed_rb = QRadioButton()
    for rb in (win._src_mic_rb, win._src_sys_rb, win._src_mixed_rb):
        win._source_group.addButton(rb)
        rb.toggled.connect(win._on_input_source_radio_toggled)
    lay.addWidget(AudioSourcePicker(win._src_mic_rb, win._src_sys_rb, win._src_mixed_rb))

    win._mixed_audio_callout = footnote(MIXED_AUDIO_NOTE)
    win._mixed_audio_callout.setProperty("viRole", "importantHint")
    win._mixed_audio_callout_wrap = QWidget()
    callout_lay = QHBoxLayout(win._mixed_audio_callout_wrap)
    callout_lay.setContentsMargins(16, 0, 16, 8)
    callout_lay.addWidget(win._mixed_audio_callout)
    lay.addWidget(win._mixed_audio_callout_wrap)


def _build_output_controls(win, lay: QVBoxLayout) -> None:
    from sayink.config import OUTPUT_MODE_HISTORY, OUTPUT_MODE_PASTE

    win._output_mode_combo = QComboBox()
    win._output_mode_combo.setMinimumWidth(220)
    win._output_mode_combo.setAccessibleName("识别结果去向")
    win._output_mode_combo.addItem("自动（随音源切换）", "auto")
    win._output_mode_combo.addItem("输入当前光标", OUTPUT_MODE_PASTE)
    win._output_mode_combo.addItem("仅保存到历史", OUTPUT_MODE_HISTORY)
    win._output_mode_combo.currentIndexChanged.connect(win._on_output_mode_changed)
    lay.addWidget(labeled_row("结果去向", win._output_mode_combo))
    win._output_mode_hint = footnote("")
    win._output_mode_hint.setAccessibleName("当前结果去向说明")
    lay.addWidget(inline_action_row(win._output_mode_hint, margins=(16, 0, 16, 10)))
    win._enable_history_btn = _ghost_button("开启转写历史", lambda: win._history_enabled_row.setChecked(True))
    win._enable_history_wrap = inline_action_row(win._enable_history_btn, margins=(16, 0, 16, 12))
    win._enable_history_wrap.layout().setAlignment(win._enable_history_btn, Qt.AlignmentFlag.AlignLeft)
    lay.addWidget(win._enable_history_wrap)


def _build_mic_test_row(win) -> QWidget:
    row = QWidget()
    row.setMinimumHeight(52)
    row_lay = QVBoxLayout(row)
    row_lay.setContentsMargins(16, 12, 16, 12)
    row_lay.setSpacing(6)
    win._mic_test_btn = QPushButton("测试声音（约 2 秒）")
    win._mic_test_btn.setProperty("viBtn", "ghostSm")
    win._mic_test_btn.setStyleSheet(settings_styles.BTN_GHOST_SM)
    win._mic_test_btn.setCursor(Qt.CursorShape.PointingHandCursor)
    win._mic_test_btn.setFixedHeight(36)
    win._mic_test_btn.clicked.connect(win._run_mic_probe)
    win._audio_device_summary = footnote("")
    win._audio_device_summary.setProperty("viRole", "importantHint")
    win._audio_device_summary.setAccessibleName("录音设备")
    row_lay.addWidget(win._audio_device_summary)
    win._mic_reset_btn = _ghost_button("设备恢复自动选择", win._reset_audio_devices_to_auto, height=36,
                                      tooltip="仅将麦克风和电脑声音设备恢复为自动选择，保留音频来源和结果去向")
    actions = QHBoxLayout()
    actions.setContentsMargins(0, 0, 0, 0)
    actions.setSpacing(8)
    actions.addWidget(win._mic_test_btn)
    actions.addWidget(win._mic_reset_btn)
    actions.addStretch(1)
    row_lay.addLayout(actions)
    win._mic_level_bar = QProgressBar()
    win._mic_level_bar.setRange(0, 100)
    win._mic_level_bar.setTextVisible(False)
    win._mic_level_bar.setFixedHeight(6)
    win._mic_level_bar.setAccessibleName("测试输入音量")
    win._mic_level_bar.hide()
    row_lay.addWidget(win._mic_level_bar)
    win._mic_test_status = QLabel("测试会临时采集约 2 秒声音，不保存音频或转写文本。")
    win._mic_test_status.setProperty("viRole", "importantHint")
    win._mic_test_status.setStyleSheet(
        f"color: {tok.TEXT_SEC}; font-size: {tok.TYPE_FOOTNOTE}px; background: transparent;"
    )
    win._mic_test_status.setWordWrap(True)
    row_lay.addWidget(win._mic_test_status)
    return row


def _build_advanced_audio(win, lay: QVBoxLayout) -> tuple[QPushButton, QPushButton]:
    """Collapsible device pickers; returns the two buttons for the Tab chain."""
    link_row = QWidget()
    link_row.setMinimumHeight(36)
    link_lay = QHBoxLayout(link_row)
    link_lay.setContentsMargins(16, 0, 16, 12)
    link_lay.setSpacing(0)
    win._advanced_audio_btn = device_selection_link("手动选择音频设备")
    win._advanced_audio_btn.toggled.connect(win._toggle_advanced_audio)
    link_lay.addWidget(win._advanced_audio_btn, 0, Qt.AlignmentFlag.AlignVCenter)
    link_lay.addStretch(1)
    lay.addWidget(link_row)

    win._advanced_audio_panel = QWidget()
    win._advanced_audio_panel.setVisible(False)
    win._advanced_audio_panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
    adv_lay = QVBoxLayout(win._advanced_audio_panel)
    adv_lay.setContentsMargins(0, 0, 0, 0)
    adv_lay.setSpacing(0)
    win._mic_device_combo = QComboBox()
    win._system_device_combo = QComboBox()
    win._mic_device_combo.setAccessibleName("麦克风设备")
    win._system_device_combo.setAccessibleName("电脑声音设备")
    for combo in (win._mic_device_combo, win._system_device_combo):
        combo.setFixedWidth(tok.CONTROL_DEVICE_COMBO_WIDTH)
        combo.currentIndexChanged.connect(win._on_audio_device_changed)
    adv_lay.addWidget(labeled_row("麦克风", win._mic_device_combo))
    adv_lay.addWidget(group_divider())
    adv_lay.addWidget(labeled_row("电脑声音", win._system_device_combo))

    btn_row = QHBoxLayout()
    btn_row.setContentsMargins(16, 8, 16, 12)
    btn_row.setSpacing(8)
    refresh_btn = _ghost_button("刷新列表", win._refresh_audio_device_lists)
    reset_btn = _ghost_button(
        "恢复自动选择",
        win._reset_audio_devices_to_auto,
        tooltip="让程序自动挑选设备，避免选到打不开的声卡",
    )
    btn_row.addWidget(refresh_btn)
    btn_row.addWidget(reset_btn)
    btn_row.addStretch()
    adv_lay.addLayout(btn_row)
    lay.addWidget(win._advanced_audio_panel)
    return refresh_btn, reset_btn


# ── 偏好 ───────────────────────────────────────────────

def _build_theme_row(win) -> QWidget:
    win._theme_combo = ThemeModeSegment()
    win._theme_combo.currentIndexChanged.connect(win._on_theme_mode_changed)
    row = QWidget()
    row_lay = QHBoxLayout(row)
    row_lay.setContentsMargins(16, 10, 16, 10)
    row_lay.setSpacing(12)
    text = QWidget()
    text_lay = QVBoxLayout(text)
    text_lay.setContentsMargins(0, 0, 0, 0)
    text_lay.setSpacing(2)
    win._theme_title_label = QLabel("主题")
    win._theme_title_label.setProperty("viRole", "rowTitle")
    win._theme_title_label.setStyleSheet(
        f"color: {tok.TEXT}; font-size: {tok.TYPE_BODY_SM}px; font-weight: 400; background: transparent;"
    )
    win._theme_desc_label = QLabel("选择浅色、暗色，或跟随 Windows 外观")
    win._theme_desc_label.setProperty("viRole", "rowSubtitle")
    win._theme_desc_label.setStyleSheet(
        f"color: {tok.TEXT_DIM}; font-size: {tok.TYPE_FOOTNOTE}px; line-height: 1.4;"
        f" background: transparent;"
    )
    text_lay.addWidget(win._theme_title_label)
    text_lay.addWidget(win._theme_desc_label)
    row_lay.addWidget(text, 1)
    row_lay.addWidget(win._theme_combo, 0, Qt.AlignmentFlag.AlignVCenter)
    return row


def _build_toggle_rows(win, lay: QVBoxLayout) -> None:
    win._auto_start_row = ToggleOptionRow("开机时自动启动")
    win._sound_row = ToggleOptionRow("录音提示音")
    win._restore_clipboard_row = ToggleOptionRow(
        "粘贴后恢复剪贴板",
        "贴进光标后换回你原来复制的文字；关闭则转写留在剪贴板",
    )
    win._auto_start_row.toggled.connect(win._on_auto_start_toggled)
    win._sound_row.toggled.connect(win._on_sound_toggled)
    win._restore_clipboard_row.toggled.connect(win._on_restore_clipboard_toggled)
    for row in (
        win._auto_start_row,
        win._sound_row,
        win._restore_clipboard_row,
    ):
        lay.addWidget(row)
        lay.addWidget(group_divider())


def _build_history_rows(win, lay: QVBoxLayout) -> None:
    win._history_enabled_row = ToggleOptionRow(
        "保存转写历史", "仅在本机保存转写文本，不保存音频；关闭后停止新增转写"
    )
    win._history_retention_days_spin = QSpinBox()
    win._history_retention_days_spin.setRange(1, 3650)
    win._history_retention_days_spin.setSuffix(" 天")
    win._configure_numeric_spin(win._history_retention_days_spin)
    win._history_retention_days_spin.setAccessibleName("历史保留天数")
    win._history_max_entries_spin = QSpinBox()
    win._history_max_entries_spin.setRange(1, 100000)
    win._history_max_entries_spin.setSingleStep(100)
    win._history_max_entries_spin.setSuffix(" 场")
    win._configure_numeric_spin(win._history_max_entries_spin)
    win._history_max_entries_spin.setAccessibleName("最多保留会话数")
    win._history_enabled_row.toggled.connect(win._on_history_enabled_toggled)
    win._history_retention_days_spin.valueChanged.connect(win._on_history_limits_changed)
    win._history_max_entries_spin.valueChanged.connect(win._on_history_limits_changed)
    lay.addWidget(win._history_enabled_row)
    win._history_retention_divider = group_divider()
    lay.addWidget(win._history_retention_divider)
    win._history_retention_row = labeled_row("保留天数", win._history_retention_days_spin, "超过天数的转写会自动清理")
    win._history_max_entries_row = labeled_row("最大会话数", win._history_max_entries_spin, "超过上限时先清理最早的转写")
    lay.addWidget(win._history_retention_row)
    win._history_max_entries_divider = group_divider()
    lay.addWidget(win._history_max_entries_divider)
    lay.addWidget(win._history_max_entries_row)
    win._history_cleanup_hint = footnote("保留规则同样适用于已有转写；保存新转写及启动时自动清理，无法撤销。会议记录不受影响。")
    win._history_cleanup_hint.setProperty("viRole", "importantHint")
    win._history_cleanup_hint_wrap = inline_action_row(win._history_cleanup_hint, margins=(16, 0, 16, 12))
    lay.addWidget(win._history_cleanup_hint_wrap)


def build_preferences_section(win) -> QWidget:
    card, lay = _card()
    lay.addWidget(_build_theme_row(win))
    lay.addWidget(group_divider())
    _build_toggle_rows(win, lay)
    _build_history_rows(win, lay)
    bottom = QWidget()
    bottom.setFixedHeight(4)
    lay.addWidget(bottom)
    return settings_section("应用与历史", card)


def build_recognition_section(win) -> QWidget:
    """Common preferences, with actual runtime feedback supplied by App."""
    from PyQt6.QtWidgets import QPlainTextEdit

    card, lay = _card()
    intro = footnote("录音与会议共用。识别模型可在「引擎」中更换。")
    intro.setProperty("viRole", "importantHint")
    lay.addWidget(inline_action_row(intro, margins=(16, 12, 16, 0)))

    win._meeting_language_combo = QComboBox()
    win._meeting_language_combo.setAccessibleName("识别语言")
    for key, label in (("auto", "自动（中文优先）"), ("zh", "中文"), ("en", "English"), ("ja", "日本語")):
        win._meeting_language_combo.addItem(label, key)
    win._meeting_language_combo.currentIndexChanged.connect(win._on_recognition_setting_changed)
    lay.addWidget(labeled_row("识别语言", win._meeting_language_combo, "支持的模型会按此语言识别"))
    lay.addWidget(group_divider())

    win._meeting_glossary_edit = QPlainTextEdit()
    win._meeting_glossary_edit.setAccessibleName("自定义词汇")
    win._meeting_glossary_edit.setPlaceholderText("每行一个词，例如：\nSayInk\n张鑫=张新、张欣\n产品名、人名或专业缩写")
    win._meeting_glossary_edit.setFixedHeight(96)
    win._meeting_glossary_edit.setTabChangesFocus(True)
    win._paint_meeting_glossary_edit()
    win._meeting_glossary_edit.textChanged.connect(win._on_meeting_glossary_changed)
    lay.addWidget(stacked_field_row(
        "自定义词汇", win._meeting_glossary_edit,
        "帮助识别专有名词；最多 50 条，每条不超过 20 字。「词=误写1、误写2」会把常被听错的写法直接改回来",
    ))
    win._recognition_capability_label = footnote("")
    win._recognition_capability_row = inline_action_row(win._recognition_capability_label, margins=(16, 0, 16, 10))
    lay.addWidget(win._recognition_capability_row)
    win._recognition_apply_label = footnote("")
    lay.addWidget(inline_action_row(win._recognition_apply_label, margins=(16, 0, 16, 10)))

    win._advanced_recognition_btn = device_selection_link("高级识别设置")
    win._advanced_recognition_btn.setAccessibleName("展开高级识别设置")
    win._advanced_recognition_btn.toggled.connect(win._toggle_advanced_recognition)
    lay.addWidget(inline_action_row(win._advanced_recognition_btn, margins=(16, 0, 16, 12)))
    win._advanced_recognition_panel = QWidget()
    win._advanced_recognition_panel.setVisible(False)
    advanced = QVBoxLayout(win._advanced_recognition_panel)
    advanced.setContentsMargins(0, 0, 0, 0)
    advanced.setSpacing(0)
    advanced.addWidget(group_divider())
    win._meeting_vad_combo = QComboBox()
    win._meeting_vad_combo.setAccessibleName("语音分段方式")
    win._meeting_vad_combo.addItem("自动检测语音（推荐）", "silero")
    win._meeting_vad_combo.addItem("按音量检测停顿", "energy")
    win._meeting_vad_combo.currentIndexChanged.connect(win._on_recognition_setting_changed)
    advanced.addWidget(labeled_row("语音分段", win._meeting_vad_combo, "检测语音和停顿，不决定标点；按住录音仍在松开后统一输出"))
    win._vad_runtime_label = footnote("尚未获取语音分段运行状态")
    win._vad_runtime_label.setAccessibleName("语音分段实际运行状态")
    advanced.addWidget(inline_action_row(win._vad_runtime_label, margins=(16, 0, 16, 8)))
    win._vad_download_status = footnote("")
    win._vad_download_status.setProperty("viRole", "importantHint")
    win._vad_download_status_row = inline_action_row(win._vad_download_status, margins=(16, 0, 16, 10))
    advanced.addWidget(win._vad_download_status_row)
    win._vad_download_btn = _ghost_button("下载语音检测模型（约 2 MB）", win._download_shared_vad)
    win._vad_download_row = inline_action_row(win._vad_download_btn, margins=(16, 0, 16, 12))
    win._vad_download_row.layout().setAlignment(win._vad_download_btn, Qt.AlignmentFlag.AlignLeft)
    advanced.addWidget(win._vad_download_row)
    advanced.addWidget(group_divider())
    win._speaker_enabled_row = ToggleOptionRow(
        "增强说话人区分", "使用 CAM++ 帮助区分不同声音；用于持续转写与会议，不验证人物身份",
    )
    win._speaker_enabled_row.toggled.connect(win._on_recognition_setting_changed)
    advanced.addWidget(win._speaker_enabled_row)
    win._speaker_runtime_label = footnote("尚未获取说话人增强运行状态")
    advanced.addWidget(inline_action_row(win._speaker_runtime_label, margins=(16, 0, 16, 12)))
    advanced.addWidget(group_divider())
    win._speaker_activity_row = ToggleOptionRow(
        "多人发言检测（实验性）", "辅助分段，疑似重叠部分待确认；可能增加耗时，需在引擎页下载检测模型并启用 CAM++",
    )
    win._speaker_activity_row.toggled.connect(win._on_recognition_setting_changed)
    advanced.addWidget(win._speaker_activity_row)
    lay.addWidget(win._advanced_recognition_panel)
    section = settings_section("识别", card)
    section.setObjectName("sharedRecognitionSection")
    return section


def build_meeting_section(win) -> QWidget:
    from sayink.speech_recognizer import MODEL_REGISTRY

    card, lay = _card()
    win._meeting_source_combo = QComboBox()
    win._meeting_source_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    win._meeting_source_combo.setMinimumContentsLength(16)
    win._meeting_source_combo.setAccessibleName("会议音频来源")
    for key, label in (("", "跟随转写设置"), ("mixed", "混合（麦克风 + 电脑声音）"),
                       ("microphone", "仅麦克风"), ("system", "仅电脑声音")):
        win._meeting_source_combo.addItem(label, key)
    win._meeting_source_combo.currentIndexChanged.connect(win._on_meeting_setting_changed)
    lay.addWidget(labeled_row("默认音频来源", win._meeting_source_combo, "跟随录音设置，或为会议固定选择一个音源"))
    lay.addWidget(group_divider())
    win._meeting_model_combo = QComboBox()
    win._meeting_model_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    win._meeting_model_combo.setMinimumContentsLength(16)
    win._meeting_model_combo.setAccessibleName("会议识别模型")
    win._meeting_model_combo.addItem("与转写相同", "")
    for model in MODEL_REGISTRY:
        win._meeting_model_combo.addItem(model["name"], model["id"])
    win._meeting_model_combo.currentIndexChanged.connect(win._on_meeting_setting_changed)
    lay.addWidget(labeled_row("会议识别模型", win._meeting_model_combo, "可为会议单独指定；开始卡片的修改仅用于本场"))
    win._meeting_defaults_hint = footnote("")
    win._meeting_defaults_hint.setProperty("viRole", "importantHint")
    lay.addWidget(inline_action_row(win._meeting_defaults_hint, margins=(16, 0, 16, 10)))
    lay.addWidget(group_divider())
    win._meeting_start_card_row = ToggleOptionRow(
        "开始前显示会议信息卡", "填写标题、参会人与议程；关闭后从托盘直接开始",
    )
    win._meeting_start_card_row.toggled.connect(win._on_meeting_setting_changed)
    lay.addWidget(win._meeting_start_card_row)
    section = settings_section("会议默认设置", card)
    section.setObjectName("meetingDefaultsSection")
    return section


# ── Page ───────────────────────────────────────────────

def build_general_page(win) -> QWidget:
    """Keep common decisions visible and low-frequency controls expandable."""
    page = SettingsPage()
    page.add(page_header("通用", "设置录音、识别和历史记录。更改自动保存，生效时间在对应设置旁显示。"))
    page.add(build_recording_section(win))
    refresh_btn, reset_btn = win._audio_device_buttons
    page.add(build_recognition_section(win))
    page.add(build_meeting_section(win))
    page.add(build_preferences_section(win))

    win._general_footer_note = footnote(
        "快捷键与输入法冲突时，可尝试其他组合；修改录音快捷键后自动保存。"
    )
    win._general_footer_note.setObjectName("generalFooterNote")
    win._general_footer_note.setAccessibleName("设置保存与快捷键提示")
    page.add(win._general_footer_note)

    # U-07: keyboard order follows the visual order top to bottom, regardless
    # of how the widgets above were constructed. Radio groups count as one
    # stop (only the checked one keeps TabFocus, see sync_group_tab_stop).
    win._general_tab_chain = [
        # The pick cards (focus proxy → their radio), not the bare radios.
        win._trigger_hotkey_rb.parentWidget(),
        win._trigger_continuous_rb.parentWidget(),
        win._hotkey_edit,
        win._esc_stop_row,
        win._src_mic_rb.parentWidget(),
        win._src_sys_rb.parentWidget(),
        win._src_mixed_rb.parentWidget(),
        win._output_mode_combo,
        win._enable_history_btn,
        win._mic_test_btn,
        win._mic_reset_btn,
        win._advanced_audio_btn,
        win._mic_device_combo,
        win._system_device_combo,
        refresh_btn,
        reset_btn,
        win._meeting_language_combo,
        win._meeting_glossary_edit,
        win._advanced_recognition_btn,
        win._meeting_vad_combo,
        win._vad_download_btn,
        win._speaker_enabled_row,
        win._speaker_activity_row,
        win._meeting_source_combo,
        win._meeting_model_combo,
        win._meeting_start_card_row,
        win._theme_combo,
        win._auto_start_row,
        win._sound_row,
        win._restore_clipboard_row,
        win._history_enabled_row,
        win._history_retention_days_spin,
        win._history_max_entries_spin,
    ]
    chain_tab_order(win._general_tab_chain)
    return page

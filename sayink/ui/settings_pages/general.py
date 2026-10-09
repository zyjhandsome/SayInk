"""General settings page: 录音 → 音频 → 偏好."""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
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
    device_selection_link,
    footnote,
    group_divider,
    info_callout,
    labeled_row,
    page_header,
    settings_group,
    settings_section,
    stacked_field_row,
)


MIXED_AUDIO_NOTE = (
    "混合模式可能混入背景音导致识别杂乱。日常口述建议「仅麦克风」。"
    "选「仅电脑播放」或「混合」后，结果会自动改为只记录到历史、不粘贴；可在下方「偏好」里改回。"
)
SYSTEM_AUDIO_NOTE = (
    "正在听电脑播放的声音：结果会自动改为只记录到历史、不粘贴，"
    "不会输入到当前窗口；可在下方「偏好」里改回。"
)


def build_general_page(win) -> QWidget:
    """Prototype v3 layout: stacked 录音 → 音频 → 偏好 cards (top to bottom)."""
    page = SettingsPage()
    page.add(page_header("通用", "按你的习惯设置听写。更改会自动保存。"))

    # ── 录音 ──
    record_card = settings_group()
    record_lay = QVBoxLayout(record_card)
    record_lay.setContentsMargins(0, 0, 0, 0)
    record_lay.setSpacing(0)
    win._trigger_group = QButtonGroup(win)
    win._trigger_continuous_rb = QRadioButton()
    win._trigger_hotkey_rb = QRadioButton()
    for rb in (win._trigger_continuous_rb, win._trigger_hotkey_rb):
        win._trigger_group.addButton(rb)
    record_lay.addWidget(TriggerModePicker(
        win._trigger_continuous_rb,
        win._trigger_hotkey_rb,
    ))
    win._hotkey_edit = HotkeyEdit()
    win._hotkey_edit.setObjectName("HotkeyEdit")
    win._hotkey_edit.setMinimumHeight(40)
    win._hotkey_edit.capture_started.connect(win.hotkey_capture_started.emit)
    win._hotkey_edit.capture_ended.connect(win.hotkey_capture_ended.emit)
    win._hotkey_edit.hotkey_changed.connect(win._apply_hotkey_setting)
    win._hotkey_hint = QLabel()
    win._hotkey_hint.setWordWrap(True)
    win._hotkey_hint.setSizePolicy(
        QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum
    )
    win._hotkey_hint.setStyleSheet(
        f"color: {tok.TEXT_DIM}; font-size: {tok.TYPE_FOOTNOTE}px; line-height: 1.4;"
        f" background: transparent; padding: 0 16px 12px 16px;"
    )
    record_lay.addWidget(stacked_field_row("录音快捷键", win._hotkey_edit))
    record_lay.addWidget(win._hotkey_hint)
    record_lay.addWidget(group_divider())
    win._esc_stop_row = ToggleOptionRow(
        "Esc 结束持续转写",
        "关闭后，在其他软件里按 Esc 不会中断监听；用听写条「结束」停止",
    )
    win._esc_stop_row.toggled.connect(win._on_esc_stop_toggled)
    record_lay.addWidget(win._esc_stop_row)
    page.add(settings_section("录音", record_card))

    # ── 音频 ──
    audio_card = settings_group()
    audio_lay = QVBoxLayout(audio_card)
    audio_lay.setContentsMargins(0, 0, 0, 0)
    audio_lay.setSpacing(0)
    win._source_group = QButtonGroup(win)
    win._src_mic_rb = QRadioButton()
    win._src_sys_rb = QRadioButton()
    win._src_mixed_rb = QRadioButton()
    for rb in (win._src_mic_rb, win._src_sys_rb, win._src_mixed_rb):
        win._source_group.addButton(rb)
    audio_lay.addWidget(AudioSourcePicker(
        win._src_mic_rb, win._src_sys_rb, win._src_mixed_rb,
    ))
    win._mixed_audio_callout = info_callout(MIXED_AUDIO_NOTE)
    win._mixed_audio_callout_wrap = QWidget()
    callout_lay = QHBoxLayout(win._mixed_audio_callout_wrap)
    callout_lay.setContentsMargins(12, 0, 12, 12)
    callout_lay.addWidget(win._mixed_audio_callout)
    audio_lay.addWidget(win._mixed_audio_callout_wrap)
    win._src_mic_rb.toggled.connect(win._sync_source_device_widgets)
    win._src_sys_rb.toggled.connect(win._sync_source_device_widgets)
    win._src_mixed_rb.toggled.connect(win._sync_source_device_widgets)

    audio_lay.addWidget(group_divider())
    test_row = QWidget()
    test_row.setMinimumHeight(52)
    test_row_lay = QVBoxLayout(test_row)
    test_row_lay.setContentsMargins(16, 12, 16, 12)
    test_row_lay.setSpacing(6)
    win._mic_test_btn = QPushButton("测试声音（约 2 秒）")
    win._mic_test_btn.setProperty("viBtn", "primary")
    win._mic_test_btn.setStyleSheet(settings_styles.BTN_PRIMARY)
    win._mic_test_btn.setCursor(Qt.CursorShape.PointingHandCursor)
    win._mic_test_btn.setFixedHeight(36)
    win._mic_test_btn.clicked.connect(win._run_mic_probe)
    test_actions = QHBoxLayout()
    test_actions.setContentsMargins(0, 0, 0, 0)
    test_actions.setSpacing(8)
    test_actions.addWidget(win._mic_test_btn)
    win._mic_reset_btn = QPushButton("恢复自动选择")
    win._mic_reset_btn.setProperty("viBtn", "ghostSm")
    win._mic_reset_btn.setStyleSheet(settings_styles.BTN_GHOST_SM)
    win._mic_reset_btn.setMinimumHeight(36)
    win._mic_reset_btn.clicked.connect(win._reset_audio_devices_to_auto)
    test_actions.addWidget(win._mic_reset_btn)
    test_actions.addStretch(1)
    test_row_lay.addLayout(test_actions)
    win._mic_test_status = QLabel("")
    win._mic_test_status.setProperty("viRole", "hint")
    win._mic_test_status.setStyleSheet(
        f"color: {tok.TEXT_SEC}; font-size: {tok.TYPE_FOOTNOTE}px; background: transparent;"
    )
    win._mic_test_status.setWordWrap(True)
    test_row_lay.addWidget(win._mic_test_status)
    audio_lay.addWidget(test_row)

    audio_lay.addWidget(group_divider())
    link_row = QWidget()
    link_row.setMinimumHeight(52)
    link_row_lay = QHBoxLayout(link_row)
    link_row_lay.setContentsMargins(16, 12, 16, 12)
    link_row_lay.setSpacing(0)
    win._advanced_audio_btn = device_selection_link("手动选择音频设备")
    win._advanced_audio_btn.toggled.connect(win._toggle_advanced_audio)
    link_row_lay.addWidget(win._advanced_audio_btn, 0, Qt.AlignmentFlag.AlignVCenter)
    link_row_lay.addStretch(1)
    audio_lay.addWidget(link_row)

    win._advanced_audio_panel = QWidget()
    win._advanced_audio_panel.setVisible(False)
    win._advanced_audio_panel.setSizePolicy(
        QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum
    )
    adv_lay = QVBoxLayout(win._advanced_audio_panel)
    adv_lay.setContentsMargins(0, 0, 0, 0)
    adv_lay.setSpacing(0)
    win._mic_device_combo = QComboBox()
    win._system_device_combo = QComboBox()
    win._mic_device_combo.setFixedWidth(tok.CONTROL_DEVICE_COMBO_WIDTH)
    win._system_device_combo.setFixedWidth(tok.CONTROL_DEVICE_COMBO_WIDTH)
    adv_lay.addWidget(labeled_row("麦克风", win._mic_device_combo))
    adv_lay.addWidget(group_divider())
    adv_lay.addWidget(labeled_row("电脑播放", win._system_device_combo))

    dev_btn_row = QHBoxLayout()
    dev_btn_row.setContentsMargins(16, 8, 16, 12)
    dev_btn_row.setSpacing(8)
    refresh_btn = QPushButton("刷新列表")
    refresh_btn.setProperty("viBtn", "ghostSm")
    refresh_btn.setMinimumHeight(32)
    refresh_btn.setStyleSheet(settings_styles.BTN_GHOST_SM)
    refresh_btn.clicked.connect(win._refresh_audio_device_lists)
    reset_btn = QPushButton("恢复自动选择")
    reset_btn.setProperty("viBtn", "ghostSm")
    reset_btn.setMinimumHeight(32)
    reset_btn.setStyleSheet(settings_styles.BTN_GHOST_SM)
    reset_btn.setToolTip("让程序自动挑选设备，避免选到打不开的声卡")
    reset_btn.clicked.connect(win._reset_audio_devices_to_auto)
    dev_btn_row.addWidget(refresh_btn)
    dev_btn_row.addWidget(reset_btn)
    dev_btn_row.addStretch()
    adv_lay.addLayout(dev_btn_row)
    audio_lay.addWidget(win._advanced_audio_panel)
    page.add(settings_section("音频", audio_card))

    # ── 偏好 ──
    prefs_card = settings_group()
    prefs_lay = QVBoxLayout(prefs_card)
    prefs_lay.setContentsMargins(0, 0, 0, 0)
    prefs_lay.setSpacing(0)

    win._theme_combo = ThemeModeSegment()
    win._theme_combo.currentIndexChanged.connect(win._on_theme_mode_changed)
    theme_row = QWidget()
    theme_row_lay = QHBoxLayout(theme_row)
    theme_row_lay.setContentsMargins(16, 10, 16, 10)
    theme_row_lay.setSpacing(12)
    theme_text = QWidget()
    theme_text_lay = QVBoxLayout(theme_text)
    theme_text_lay.setContentsMargins(0, 0, 0, 0)
    theme_text_lay.setSpacing(2)
    win._theme_title_label = QLabel("主题")
    win._theme_title_label.setProperty("viRole", "rowTitle")
    win._theme_title_label.setStyleSheet(
        f"color: {tok.TEXT}; font-size: {tok.TYPE_BODY_SM}px; font-weight: 400; background: transparent;"
    )
    win._theme_desc_label = QLabel("跟随系统时按 Windows 外观显示")
    win._theme_desc_label.setProperty("viRole", "rowSubtitle")
    win._theme_desc_label.setStyleSheet(
        f"color: {tok.TEXT_DIM}; font-size: {tok.TYPE_FOOTNOTE}px; line-height: 1.4;"
        f" background: transparent;"
    )
    theme_text_lay.addWidget(win._theme_title_label)
    theme_text_lay.addWidget(win._theme_desc_label)
    theme_row_lay.addWidget(theme_text, 1)
    theme_row_lay.addWidget(win._theme_combo, 0, Qt.AlignmentFlag.AlignVCenter)
    prefs_lay.addWidget(theme_row)
    prefs_lay.addWidget(group_divider())

    win._auto_start_row = ToggleOptionRow("开机时自动启动")
    win._sound_row = ToggleOptionRow("录音提示音")
    win._restore_clipboard_row = ToggleOptionRow("粘贴后恢复剪贴板")
    win._history_only_output_row = ToggleOptionRow(
        "只记录到历史，不粘贴",
        "开会或听电脑播放声时用：识别结果只写入历史，不会输入到当前窗口",
    )
    win._auto_start_row.toggled.connect(win._on_auto_start_toggled)
    win._sound_row.toggled.connect(win._on_sound_toggled)
    win._restore_clipboard_row.toggled.connect(win._on_restore_clipboard_toggled)
    win._history_only_output_row.toggled.connect(win._on_history_only_output_toggled)
    prefs_lay.addWidget(win._auto_start_row)
    prefs_lay.addWidget(group_divider())
    prefs_lay.addWidget(win._sound_row)
    prefs_lay.addWidget(group_divider())
    prefs_lay.addWidget(win._history_only_output_row)
    prefs_lay.addWidget(group_divider())
    prefs_lay.addWidget(win._restore_clipboard_row)
    prefs_lay.addWidget(group_divider())

    win._history_enabled_row = ToggleOptionRow("保存语音历史", "在本机保存转写文本，便于搜索和复制；不保存音频")
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
    prefs_lay.addWidget(win._history_enabled_row)
    prefs_lay.addWidget(group_divider())
    win._history_retention_row = labeled_row(
        "保留天数", win._history_retention_days_spin
    )
    win._history_max_entries_row = labeled_row(
        "最大会话数", win._history_max_entries_spin
    )
    prefs_lay.addWidget(win._history_retention_row)
    prefs_lay.addWidget(group_divider())
    prefs_lay.addWidget(win._history_max_entries_row)
    prefs_bottom = QWidget()
    prefs_bottom.setFixedHeight(4)
    prefs_lay.addWidget(prefs_bottom)
    page.add(settings_section("偏好", prefs_card))

    for rb in (win._src_mic_rb, win._src_sys_rb, win._src_mixed_rb):
        rb.toggled.connect(win._on_input_source_radio_toggled)
    win._trigger_continuous_rb.toggled.connect(win._on_trigger_mode_radio_toggled)
    win._trigger_hotkey_rb.toggled.connect(win._on_trigger_mode_radio_toggled)
    win._mic_device_combo.currentIndexChanged.connect(win._on_audio_device_changed)
    win._system_device_combo.currentIndexChanged.connect(win._on_audio_device_changed)

    win._general_footer_note = footnote(
        "更改将自动保存并立即生效；若录音快捷键与输入法冲突，"
        "可改用 Alt + Space。"
    )
    win._general_footer_note.setObjectName("generalFooterNote")
    win._general_footer_note.setAccessibleName("设置保存与快捷键提示")
    page.add(win._general_footer_note)
    page._layout.setContentsMargins(2, 20, 2, 12)
    page.set_spacing(18)
    return page

"""Task-oriented General settings subpage: recording, recognition, meetings, and app/history."""

from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtCore import QEvent, QPoint, QSignalBlocker, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QButtonGroup,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWIDGETSIZE_MAX,
    QWidget,
)

from sayink.audio_devices import (
    INPUT_SOURCE_MICROPHONE,
    INPUT_SOURCE_MIXED,
    INPUT_SOURCE_SYSTEM,
    input_source_label,
)
from sayink.config import (
    Config,
    DEFAULT_HOTKEY,
    OUTPUT_MODE_HISTORY,
    OUTPUT_MODE_PASTE,
    TRIGGER_MODE_CONTINUOUS,
    TRIGGER_MODE_HOTKEY,
)
from sayink.recognition_settings import RecognitionSettings
from sayink.runtime_status import SegmentationStatus
from sayink.ui import design_tokens as tok
from sayink.ui import settings_styles
from sayink.ui.dictation_hints import dictation_hints
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
    paint_device_selection_link,
    settings_group,
    settings_section,
    stacked_field_row,
    sync_group_tab_stop,
)
from sayink.ui.theme import normalize_theme_mode

MIXED_AUDIO_NOTE = (
    "音频来源：麦克风 + 电脑声音。线上会议可同时记录双方；外放时建议佩戴耳机，避免重复收音。"
)
SYSTEM_AUDIO_NOTE = (
    "音频来源：电脑声音。只录制系统播放的音频，不包含你的麦克风；开始录音前不会采集声音。"
)


def device_index(data: object) -> int:
    """Combo item data -> device index; 0 is a real device, only None means automatic."""
    return -1 if data is None else int(data)


def int_or(value: object, default: int) -> int:
    """A hand-edited config may hold ``null`` or text where a number is expected."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def configure_numeric_spin(spin: QSpinBox) -> None:
    """Shared metrics for flat themed QSpinBox steppers."""
    spin.setFixedWidth(tok.CONTROL_NUMERIC_WIDTH)
    spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.UpDownArrows)
    spin.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)


def set_combo_by_data(combo: QComboBox, value: object) -> bool:
    idx = combo.findData(value)
    if idx >= 0:
        combo.setCurrentIndex(idx)
        return True
    if combo.count() > 0:
        combo.setCurrentIndex(0)
    return False


def set_checked_link(button: QPushButton, checked: bool) -> None:
    if button.isChecked() == checked:
        return
    blocker = QSignalBlocker(button)
    button.setChecked(checked)
    del blocker


def scroll_page_of(widget: QWidget) -> SettingsPage | None:
    w: QWidget | None = widget
    while w is not None:
        if isinstance(w, SettingsPage):
            return w
        w = w.parentWidget()
    return None


def set_section_visible(panel: QWidget, visible: bool) -> None:
    """Show or hide a collapsed block without a one-frame layout jump."""
    if panel.isVisible() == visible and (visible or panel.maximumHeight() == 0):
        return
    if visible:
        panel.setMaximumHeight(QWIDGETSIZE_MAX)
        saved = panel.property("_sectionMinHeight")
        if isinstance(saved, int) and saved > 0:
            panel.setMinimumHeight(saved)
        panel.setVisible(True)
        return
    if panel.property("_sectionMinHeight") is None:
        panel.setProperty("_sectionMinHeight", int(panel.minimumHeight()))
    panel.setMaximumHeight(0)
    panel.setVisible(False)
    panel.updateGeometry()


def settle_scroll_anchor(
    page: SettingsPage,
    anchor: QWidget,
    anchor_y: int,
    scroll_before: int,
) -> None:
    """Finish the show/hide layout before anything is painted."""
    host = page.widget()
    if host is None:
        return
    layout = host.layout()
    bar = page.verticalScrollBar()
    for _ in range(3):
        if layout is not None:
            layout.invalidate()
            layout.activate()
        QApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest)
        hint_h = host.sizeHint().height()
        if hint_h > 0 and host.height() != hint_h:
            host.resize(host.width(), hint_h)
        if layout is not None:
            layout.activate()
        QApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest)
        new_y = anchor.mapTo(host, anchor.rect().topLeft()).y()
        bar.setValue(0 if scroll_before == 0 else max(0, scroll_before + (new_y - anchor_y)))
        page._sync_scroll_gutter(bar.minimum(), bar.maximum())


def without_scroll_jump(anchor: QWidget, change: Callable[[], None]) -> None:
    """Run ``change`` without the scroll area shoving ``anchor`` around."""
    page = scroll_page_of(anchor)
    if page is None or not page.isVisible():
        change()
        return
    host = page.widget()
    anchor_y = anchor.mapTo(host, anchor.rect().topLeft()).y() if host is not None else 0
    scroll_before = page.verticalScrollBar().value()
    was_enabled = page.updatesEnabled()
    if was_enabled:
        page.setUpdatesEnabled(False)
    try:
        change()
        settle_scroll_anchor(page, anchor, anchor_y, scroll_before)
    finally:
        if was_enabled:
            page.setUpdatesEnabled(True)


def _card() -> tuple[QWidget, QVBoxLayout]:
    card = settings_group()
    lay = QVBoxLayout(card)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)
    return card, lay


def _ghost_button(
    text: str,
    slot: Callable[..., object],
    *,
    height: int = 32,
    tooltip: str = "",
) -> QPushButton:
    btn = QPushButton(text)
    btn.setProperty("viBtn", "ghostSm")
    btn.setMinimumHeight(height)
    btn.setStyleSheet(settings_styles.BTN_GHOST_SM)
    if tooltip:
        btn.setToolTip(tooltip)
    btn.clicked.connect(slot)
    return btn


def _reveal_section(page: SettingsPage, target: QWidget) -> None:
    """Put the group title at the top of the viewport."""
    content = page.widget()
    if content is None:
        return
    top = target.mapTo(content, QPoint(0, 0)).y()
    bar = page.verticalScrollBar()
    bar.setValue(max(bar.minimum(), min(top - 8, bar.maximum())))


class SectionJumpBar(QWidget):
    """Typed quick-jump strip reaching any section on the general settings page."""

    jump_buttons: list[QPushButton]

    def __init__(
        self,
        page: SettingsPage,
        sections: list[tuple[str, QWidget]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("generalSectionNav")
        self.jump_buttons = []
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        for title, section in sections:
            button = QPushButton(title)
            button.setAccessibleName(title)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setStyleSheet(settings_styles.BTN_GHOST_SM)
            button.clicked.connect(
                lambda _checked=False, target=section, scroller=page: _reveal_section(scroller, target)
            )
            row.addWidget(button, 0)
            self.jump_buttons.append(button)
        row.addStretch(1)


class GlossaryPlainTextEdit(QPlainTextEdit):
    """Multi-line glossary input that commits only when editing ends.

    Typing, newlines, IME composition, and temporary context-menu popups must
    not broadcast ``recognition_changed`` mid-edit; ``editing_finished`` fires
    only on a real (non-popup) focus loss.
    """

    editing_finished = pyqtSignal()

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        if event is not None and event.reason() == Qt.FocusReason.PopupFocusReason:
            return
        self.editing_finished.emit()


class GeneralSettingsPage(SettingsPage):
    """Strongly-typed General settings subpage owning all recording, recognition, meeting, and preference controls."""

    # Signals upward to SettingsWindow
    trigger_mode_toggled = pyqtSignal(bool)
    hotkey_capture_started = pyqtSignal()
    hotkey_capture_ended = pyqtSignal()
    hotkey_changed = pyqtSignal(str)
    hotkey_reset_requested = pyqtSignal()
    esc_stop_toggled = pyqtSignal(bool)
    input_source_toggled = pyqtSignal(bool)
    output_mode_changed = pyqtSignal(int)
    mic_probe_requested = pyqtSignal()
    audio_devices_reset_requested = pyqtSignal()
    advanced_audio_toggled = pyqtSignal(bool)
    audio_device_changed = pyqtSignal(int)
    audio_devices_refresh_requested = pyqtSignal()
    separate_lanes_toggled = pyqtSignal(bool)
    recognition_setting_changed = pyqtSignal()
    glossary_text_changed = pyqtSignal()
    glossary_editing_finished = pyqtSignal()
    vad_download_requested = pyqtSignal()
    meeting_setting_changed = pyqtSignal()
    meeting_start_card_reset_requested = pyqtSignal()
    llm_proxy_changed = pyqtSignal(int)
    llm_proxy_suffix_changed = pyqtSignal(str)
    theme_mode_changed = pyqtSignal(int)
    listen_bar_placement_changed = pyqtSignal(int)
    listen_bar_autohide_toggled = pyqtSignal(bool)
    auto_start_toggled = pyqtSignal(bool)
    sound_toggled = pyqtSignal(bool)
    restore_clipboard_toggled = pyqtSignal(bool)
    history_enabled_toggled = pyqtSignal(bool)
    history_limits_changed = pyqtSignal(int)
    recovery_toggled = pyqtSignal(bool)
    audio_recovery_requested = pyqtSignal()

    # Explicitly typed widget/control fields (eliminating 77 dynamic win._* injections)
    trigger_group: QButtonGroup
    trigger_continuous_rb: QRadioButton
    trigger_hotkey_rb: QRadioButton
    hotkey_edit: HotkeyEdit
    hotkey_hint: QLabel
    hotkey_reset_btn: QPushButton
    esc_stop_divider: QWidget
    esc_stop_row: ToggleOptionRow
    audio_device_buttons: tuple[QPushButton, QPushButton]
    separate_lanes_row: ToggleOptionRow
    source_group: QButtonGroup
    src_mic_rb: QRadioButton
    src_sys_rb: QRadioButton
    src_mixed_rb: QRadioButton
    mixed_audio_callout: QLabel
    mixed_audio_callout_wrap: QWidget
    output_mode_combo: QComboBox
    output_mode_hint: QLabel
    enable_history_btn: QPushButton
    enable_history_wrap: QWidget
    mic_test_btn: QPushButton
    audio_device_summary: QLabel
    mic_reset_btn: QPushButton
    mic_level_bar: QProgressBar
    mic_test_status: QLabel
    advanced_audio_btn: QPushButton
    advanced_audio_panel: QWidget
    mic_device_combo: QComboBox
    system_device_combo: QComboBox
    theme_combo: ThemeModeSegment
    theme_title_label: QLabel
    theme_desc_label: QLabel
    auto_start_row: ToggleOptionRow
    sound_row: ToggleOptionRow
    restore_clipboard_row: ToggleOptionRow
    history_enabled_row: ToggleOptionRow
    history_retention_days_spin: QSpinBox
    history_max_entries_spin: QSpinBox
    history_retention_divider: QWidget
    history_retention_row: QWidget
    history_max_entries_row: QWidget
    history_max_entries_divider: QWidget
    history_cleanup_hint: QLabel
    history_cleanup_hint_wrap: QWidget
    listen_bar_combo: QComboBox
    llm_proxy_combo: QComboBox
    llm_proxy_suffix_edit: QLineEdit
    listen_bar_autohide_row: ToggleOptionRow
    recovery_row: ToggleOptionRow
    recovery_btn: QPushButton
    meeting_language_combo: QComboBox
    meeting_glossary_edit: GlossaryPlainTextEdit
    recognition_capability_label: QLabel
    recognition_capability_row: QWidget
    recognition_apply_label: QLabel
    advanced_recognition_btn: QPushButton
    advanced_recognition_panel: QWidget
    vad_runtime_label: QLabel
    vad_download_status: QLabel
    vad_download_status_row: QWidget
    vad_download_btn: QPushButton
    vad_download_row: QWidget
    quiet_speech_row: ToggleOptionRow
    boundary_context_row: ToggleOptionRow
    speaker_enabled_row: ToggleOptionRow
    speaker_runtime_label: QLabel
    speaker_activity_row: ToggleOptionRow
    meeting_source_combo: QComboBox
    meeting_model_combo: QComboBox
    meeting_defaults_hint: QLabel
    meeting_start_card_row: ToggleOptionRow
    meeting_start_card_reset_btn: QPushButton
    section_jump_bar: SectionJumpBar
    general_section_jumps: list[QPushButton]
    general_footer_note: QLabel
    general_tab_chain: list[QWidget]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.add(page_header("通用", "设置录音、识别和历史记录。更改自动保存，生效时间在对应设置旁显示。"))
        recording = self._build_recording_section()
        refresh_btn, reset_btn = self.audio_device_buttons
        recognition = self._build_recognition_section()
        meeting = self._build_meeting_section()
        preferences = self._build_preferences_section()
        self.section_jump_bar = SectionJumpBar(self, [
            ("录音与输入", recording),
            ("识别", recognition),
            ("会议默认", meeting),
            ("应用与历史", preferences),
        ])
        self.general_section_jumps = self.section_jump_bar.jump_buttons
        self.add(self.section_jump_bar)
        self.add(recording)
        self.add(recognition)
        self.add(meeting)
        self.add(preferences)

        self.general_footer_note = footnote(
            "快捷键与输入法冲突时，可尝试其他组合；修改录音快捷键后自动保存。"
        )
        self.general_footer_note.setObjectName("generalFooterNote")
        self.general_footer_note.setAccessibleName("设置保存与快捷键提示")
        self.add(self.general_footer_note)

        # U-07: keyboard order follows the visual order top to bottom.
        self.general_tab_chain = [
            *self.general_section_jumps,
            self.trigger_hotkey_rb.parentWidget(),
            self.trigger_continuous_rb.parentWidget(),
            self.hotkey_edit,
            self.hotkey_reset_btn,
            self.esc_stop_row,
            self.src_mic_rb.parentWidget(),
            self.src_sys_rb.parentWidget(),
            self.src_mixed_rb.parentWidget(),
            self.output_mode_combo,
            self.enable_history_btn,
            self.mic_test_btn,
            self.mic_reset_btn,
            self.advanced_audio_btn,
            self.mic_device_combo,
            self.system_device_combo,
            refresh_btn,
            reset_btn,
            self.separate_lanes_row,
            self.meeting_language_combo,
            self.meeting_glossary_edit,
            self.advanced_recognition_btn,
            self.vad_download_btn,
            self.meeting_source_combo,
            self.meeting_model_combo,
            self.meeting_start_card_row,
            self.meeting_start_card_reset_btn,
            self.llm_proxy_combo,
            self.llm_proxy_suffix_edit,
            self.theme_combo,
            self.listen_bar_combo,
            self.listen_bar_autohide_row,
            self.auto_start_row,
            self.sound_row,
            self.restore_clipboard_row,
            self.history_enabled_row,
            self.history_retention_days_spin,
            self.history_max_entries_spin,
            self.recovery_row,
            self.recovery_btn,
        ]
        chain_tab_order(self.general_tab_chain)

    # ── Section builders ──────────────────────────────────

    def _build_recording_section(self) -> QWidget:
        card, lay = _card()
        self.trigger_group = QButtonGroup(self)
        self.trigger_continuous_rb = QRadioButton()
        self.trigger_hotkey_rb = QRadioButton()
        for rb in (self.trigger_hotkey_rb, self.trigger_continuous_rb):
            self.trigger_group.addButton(rb)
            rb.toggled.connect(self.trigger_mode_toggled.emit)
        lay.addWidget(TriggerModePicker(self.trigger_continuous_rb, self.trigger_hotkey_rb))

        self.hotkey_edit = HotkeyEdit()
        self.hotkey_edit.setObjectName("HotkeyEdit")
        self.hotkey_edit.setMinimumHeight(40)
        self.hotkey_edit.capture_started.connect(self.hotkey_capture_started.emit)
        self.hotkey_edit.capture_ended.connect(self.hotkey_capture_ended.emit)
        self.hotkey_edit.hotkey_changed.connect(self.hotkey_changed.emit)
        self.hotkey_hint = QLabel()
        self.hotkey_hint.setWordWrap(True)
        self.hotkey_hint.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.hotkey_hint.setStyleSheet(
            f"color: {tok.TEXT_SEC}; font-size: {tok.TYPE_FOOTNOTE}px; line-height: 1.4;"
            f" background: transparent; padding: 0 16px 12px 16px;"
        )
        lay.addWidget(stacked_field_row("录音快捷键", self.hotkey_edit))
        lay.addWidget(self.hotkey_hint)
        self.hotkey_reset_btn = _ghost_button(
            "快捷键恢复默认",
            self.hotkey_reset_requested.emit,
            tooltip="仅将录音快捷键恢复为 Alt+X，不改变转写模式、Esc、音频来源和结果去向",
        )
        lay.addWidget(inline_action_row(self.hotkey_reset_btn))
        self.esc_stop_divider = group_divider()
        lay.addWidget(self.esc_stop_divider)
        self.esc_stop_row = ToggleOptionRow(
            "允许 Esc 结束持续转写",
            "结束后继续识别已录内容；关闭时只能点转写条「结束」",
        )
        self.esc_stop_row.toggled.connect(self.esc_stop_toggled.emit)
        lay.addWidget(self.esc_stop_row)
        lay.addWidget(group_divider())
        source_title = footnote("音频来源")
        source_title.setProperty("viRole", "importantHint")
        lay.addWidget(inline_action_row(source_title, margins=(16, 12, 16, 0)))
        self._build_source_picker(lay)
        self._build_output_controls(lay)
        lay.addWidget(group_divider())
        lay.addWidget(self._build_mic_test_row())
        self.audio_device_buttons = self._build_advanced_audio(lay)
        lay.addWidget(group_divider())
        self.separate_lanes_row = ToggleOptionRow(
            "混合音源分别识别（实验性）",
            "分别识别麦克风和电脑声音，减少同时说话时的互相遮盖；可能增加处理耗时。下次录音生效。",
        )
        self.separate_lanes_row.toggled.connect(self.separate_lanes_toggled.emit)
        lay.addWidget(self.separate_lanes_row)
        section = settings_section("录音与输入", card)
        section.setObjectName("recordingInputSection")
        return section

    def _build_source_picker(self, lay: QVBoxLayout) -> None:
        self.source_group = QButtonGroup(self)
        self.src_mic_rb = QRadioButton()
        self.src_sys_rb = QRadioButton()
        self.src_mixed_rb = QRadioButton()
        for rb in (self.src_mic_rb, self.src_sys_rb, self.src_mixed_rb):
            self.source_group.addButton(rb)
            rb.toggled.connect(self.input_source_toggled.emit)
        lay.addWidget(AudioSourcePicker(self.src_mic_rb, self.src_sys_rb, self.src_mixed_rb))

        self.mixed_audio_callout = footnote(MIXED_AUDIO_NOTE)
        self.mixed_audio_callout.setProperty("viRole", "importantHint")
        self.mixed_audio_callout_wrap = QWidget()
        callout_lay = QHBoxLayout(self.mixed_audio_callout_wrap)
        callout_lay.setContentsMargins(16, 0, 16, 8)
        callout_lay.addWidget(self.mixed_audio_callout)
        lay.addWidget(self.mixed_audio_callout_wrap)

    def _build_output_controls(self, lay: QVBoxLayout) -> None:
        self.output_mode_combo = QComboBox()
        self.output_mode_combo.setAccessibleName("识别结果去向")
        self.output_mode_combo.addItem("自动（随音源切换）", "auto")
        self.output_mode_combo.addItem("输入当前光标", OUTPUT_MODE_PASTE)
        self.output_mode_combo.addItem("仅保存到历史", OUTPUT_MODE_HISTORY)
        self.output_mode_combo.currentIndexChanged.connect(self.output_mode_changed.emit)
        lay.addWidget(labeled_row("结果去向", self.output_mode_combo))
        self.output_mode_hint = footnote("")
        self.output_mode_hint.setAccessibleName("当前结果去向说明")
        lay.addWidget(inline_action_row(self.output_mode_hint, margins=(16, 0, 16, 10)))
        self.enable_history_btn = _ghost_button(
            "开启转写历史",
            lambda: self.history_enabled_row.setChecked(True),
        )
        self.enable_history_wrap = inline_action_row(self.enable_history_btn, margins=(16, 0, 16, 12))
        self.enable_history_wrap.layout().setAlignment(self.enable_history_btn, Qt.AlignmentFlag.AlignLeft)
        lay.addWidget(self.enable_history_wrap)

    def _build_mic_test_row(self) -> QWidget:
        row = QWidget()
        row.setMinimumHeight(52)
        row_lay = QVBoxLayout(row)
        row_lay.setContentsMargins(16, 12, 16, 12)
        row_lay.setSpacing(6)
        self.mic_test_btn = QPushButton("测试声音（约 2 秒）")
        self.mic_test_btn.setProperty("viBtn", "ghostSm")
        self.mic_test_btn.setStyleSheet(settings_styles.BTN_GHOST_SM)
        self.mic_test_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mic_test_btn.setFixedHeight(36)
        self.mic_test_btn.clicked.connect(self.mic_probe_requested.emit)
        self.audio_device_summary = footnote("")
        self.audio_device_summary.setProperty("viRole", "importantHint")
        self.audio_device_summary.setAccessibleName("录音设备")
        row_lay.addWidget(self.audio_device_summary)
        self.mic_reset_btn = _ghost_button(
            "设备恢复自动选择",
            self.audio_devices_reset_requested.emit,
            height=36,
            tooltip="仅将麦克风和电脑声音设备恢复为自动选择，保留音频来源和结果去向",
        )
        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(8)
        actions.addWidget(self.mic_test_btn)
        actions.addWidget(self.mic_reset_btn)
        actions.addStretch(1)
        row_lay.addLayout(actions)
        self.mic_level_bar = QProgressBar()
        self.mic_level_bar.setRange(0, 100)
        self.mic_level_bar.setTextVisible(False)
        self.mic_level_bar.setFixedHeight(6)
        self.mic_level_bar.setAccessibleName("测试输入音量")
        self.mic_level_bar.hide()
        row_lay.addWidget(self.mic_level_bar)
        self.mic_test_status = QLabel("测试会临时采集约 2 秒声音，不保存音频或转写文本。")
        self.mic_test_status.setProperty("viRole", "importantHint")
        self.mic_test_status.setStyleSheet(
            f"color: {tok.TEXT_SEC}; font-size: {tok.TYPE_FOOTNOTE}px; background: transparent;"
        )
        self.mic_test_status.setWordWrap(True)
        row_lay.addWidget(self.mic_test_status)
        return row

    def _build_advanced_audio(self, lay: QVBoxLayout) -> tuple[QPushButton, QPushButton]:
        """Collapsible device pickers; returns the two buttons for the Tab chain."""
        link_row = QWidget()
        link_row.setMinimumHeight(36)
        link_lay = QHBoxLayout(link_row)
        link_lay.setContentsMargins(16, 0, 16, 12)
        link_lay.setSpacing(0)
        self.advanced_audio_btn = device_selection_link("手动选择音频设备")
        self.advanced_audio_btn.toggled.connect(self.advanced_audio_toggled.emit)
        link_lay.addWidget(self.advanced_audio_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        link_lay.addStretch(1)
        lay.addWidget(link_row)

        self.advanced_audio_panel = QWidget()
        self.advanced_audio_panel.setVisible(False)
        self.advanced_audio_panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        adv_lay = QVBoxLayout(self.advanced_audio_panel)
        adv_lay.setContentsMargins(0, 0, 0, 0)
        adv_lay.setSpacing(0)
        self.mic_device_combo = QComboBox()
        self.system_device_combo = QComboBox()
        self.mic_device_combo.setAccessibleName("麦克风设备")
        self.system_device_combo.setAccessibleName("电脑声音设备")
        for combo in (self.mic_device_combo, self.system_device_combo):
            combo.currentIndexChanged.connect(self.audio_device_changed.emit)
        adv_lay.addWidget(labeled_row("麦克风", self.mic_device_combo))
        adv_lay.addWidget(group_divider())
        adv_lay.addWidget(labeled_row("电脑声音", self.system_device_combo))

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(16, 8, 16, 12)
        btn_row.setSpacing(8)
        refresh_btn = _ghost_button("刷新列表", self.audio_devices_refresh_requested.emit)
        reset_btn = _ghost_button(
            "恢复自动选择",
            self.audio_devices_reset_requested.emit,
            tooltip="让程序自动挑选设备，避免选到打不开的声卡",
        )
        btn_row.addWidget(refresh_btn)
        btn_row.addWidget(reset_btn)
        btn_row.addStretch()
        adv_lay.addLayout(btn_row)
        lay.addWidget(self.advanced_audio_panel)
        return refresh_btn, reset_btn

    def _build_theme_row(self) -> QWidget:
        self.theme_combo = ThemeModeSegment()
        self.theme_combo.currentIndexChanged.connect(self.theme_mode_changed.emit)
        row = QWidget()
        row_lay = QHBoxLayout(row)
        row_lay.setContentsMargins(16, 10, 16, 10)
        row_lay.setSpacing(12)
        text = QWidget()
        text_lay = QVBoxLayout(text)
        text_lay.setContentsMargins(0, 0, 0, 0)
        text_lay.setSpacing(2)
        self.theme_title_label = QLabel("主题")
        self.theme_title_label.setProperty("viRole", "rowTitle")
        self.theme_title_label.setStyleSheet(
            f"color: {tok.TEXT}; font-size: {tok.TYPE_BODY_SM}px; font-weight: 400; background: transparent;"
        )
        self.theme_desc_label = QLabel("选择浅色、暗色，或跟随 Windows 外观")
        self.theme_desc_label.setProperty("viRole", "rowSubtitle")
        self.theme_desc_label.setStyleSheet(
            f"color: {tok.TEXT_DIM}; font-size: {tok.TYPE_FOOTNOTE}px; line-height: 1.4;"
            f" background: transparent;"
        )
        text_lay.addWidget(self.theme_title_label)
        text_lay.addWidget(self.theme_desc_label)
        row_lay.addWidget(text, 1)
        row_lay.addWidget(self.theme_combo, 0, Qt.AlignmentFlag.AlignVCenter)
        return row

    def _build_toggle_rows(self, lay: QVBoxLayout) -> None:
        self.auto_start_row = ToggleOptionRow("开机时自动启动")
        self.sound_row = ToggleOptionRow("录音提示音")
        self.restore_clipboard_row = ToggleOptionRow(
            "粘贴后恢复剪贴板",
            "贴进光标后换回你原来复制的文字；关闭则转写留在剪贴板",
        )
        self.auto_start_row.toggled.connect(self.auto_start_toggled.emit)
        self.sound_row.toggled.connect(self.sound_toggled.emit)
        self.restore_clipboard_row.toggled.connect(self.restore_clipboard_toggled.emit)
        for row in (
            self.auto_start_row,
            self.sound_row,
            self.restore_clipboard_row,
        ):
            lay.addWidget(row)
            lay.addWidget(group_divider())

    def _build_history_rows(self, lay: QVBoxLayout) -> None:
        self.history_enabled_row = ToggleOptionRow(
            "保存转写历史", "历史仅保存转写文本；关闭后停止新增转写，录音恢复有独立开关"
        )
        self.history_retention_days_spin = QSpinBox()
        self.history_retention_days_spin.setRange(1, 3650)
        self.history_retention_days_spin.setSuffix(" 天")
        configure_numeric_spin(self.history_retention_days_spin)
        self.history_retention_days_spin.setAccessibleName("历史保留天数")
        self.history_max_entries_spin = QSpinBox()
        self.history_max_entries_spin.setRange(1, 100000)
        self.history_max_entries_spin.setSingleStep(100)
        self.history_max_entries_spin.setSuffix(" 场")
        configure_numeric_spin(self.history_max_entries_spin)
        self.history_max_entries_spin.setAccessibleName("最多保留会话数")
        self.history_enabled_row.toggled.connect(self.history_enabled_toggled.emit)
        self.history_retention_days_spin.valueChanged.connect(self.history_limits_changed.emit)
        self.history_max_entries_spin.valueChanged.connect(self.history_limits_changed.emit)
        lay.addWidget(self.history_enabled_row)
        self.history_retention_divider = group_divider()
        lay.addWidget(self.history_retention_divider)
        self.history_retention_row = labeled_row("保留天数", self.history_retention_days_spin, "超过天数的转写会自动清理")
        self.history_max_entries_row = labeled_row("最大会话数", self.history_max_entries_spin, "超过上限时先清理最早的转写")
        lay.addWidget(self.history_retention_row)
        self.history_max_entries_divider = group_divider()
        lay.addWidget(self.history_max_entries_divider)
        lay.addWidget(self.history_max_entries_row)
        self.history_cleanup_hint = footnote("保留规则同样适用于已有转写；保存新转写及启动时自动清理，无法撤销。会议记录不受影响。")
        self.history_cleanup_hint.setProperty("viRole", "importantHint")
        self.history_cleanup_hint_wrap = inline_action_row(self.history_cleanup_hint, margins=(16, 0, 16, 12))
        lay.addWidget(self.history_cleanup_hint_wrap)

    def _build_listen_bar_row(self) -> QWidget:
        from sayink.ui.island_chrome import LISTEN_BAR_PLACEMENT_LABELS

        self.listen_bar_combo = QComboBox()
        self.listen_bar_combo.setAccessibleName("转写条位置")
        for key, label in LISTEN_BAR_PLACEMENT_LABELS:
            self.listen_bar_combo.addItem(label, key)
        self.listen_bar_combo.currentIndexChanged.connect(self.listen_bar_placement_changed.emit)
        return labeled_row(
            "转写条位置",
            self.listen_bar_combo,
            "单次转写短暂出现在上方；持续转写和会议默认在下方。拖动后会记住。",
        )

    def _build_preferences_section(self) -> QWidget:
        card, lay = _card()
        self.llm_proxy_combo = QComboBox()
        self.llm_proxy_combo.setAccessibleName("AI 服务代理模式")
        self.llm_proxy_combo.addItem("使用系统 / 环境代理", "system")
        self.llm_proxy_combo.addItem("直连（不使用代理）", "direct")
        self.llm_proxy_combo.currentIndexChanged.connect(self.llm_proxy_changed.emit)
        lay.addWidget(labeled_row(
            "AI 服务代理", self.llm_proxy_combo,
            "适用于连接测试、文字处理和会议纪要；内网接口超时可尝试直连。更新不受此设置影响。",
        ))
        self.llm_proxy_suffix_edit = QLineEdit()
        self.llm_proxy_suffix_edit.setAccessibleName("额外直连域名")
        self.llm_proxy_suffix_edit.setPlaceholderText(".example.internal")
        self.llm_proxy_suffix_edit.setClearButtonEnabled(True)
        self.llm_proxy_suffix_edit.textChanged.connect(self.llm_proxy_suffix_changed.emit)
        lay.addWidget(labeled_row(
            "额外直连域名",
            self.llm_proxy_suffix_edit,
            "仅在「使用系统 / 环境代理」时生效。每项是域名后缀，用逗号或空格分隔。本地和私网地址不用填写。",
        ))
        lay.addWidget(group_divider())
        lay.addWidget(self._build_theme_row())
        lay.addWidget(group_divider())
        lay.addWidget(self._build_listen_bar_row())
        lay.addWidget(group_divider())
        self.listen_bar_autohide_row = ToggleOptionRow(
            "一段时间后收起转写文字",
            "持续转写和会议中，约 12 秒没有新文字就收起，鼠标移上去再展开。",
        )
        self.listen_bar_autohide_row.toggled.connect(self.listen_bar_autohide_toggled.emit)
        lay.addWidget(self.listen_bar_autohide_row)
        lay.addWidget(group_divider())
        self._build_toggle_rows(lay)
        self._build_history_rows(lay)
        lay.addWidget(group_divider())
        self.recovery_row = ToggleOptionRow(
            "录音恢复（本机缓存）",
            "默认关闭；开启后保存完整录音，正常结束保留 7 天，总量上限 2 GB。下次录音生效。",
        )
        self.recovery_row.toggled.connect(self.recovery_toggled.emit)
        lay.addWidget(self.recovery_row)
        self.recovery_btn = _ghost_button("查看 / 恢复录音", self.audio_recovery_requested.emit)
        lay.addWidget(inline_action_row(self.recovery_btn))
        bottom = QWidget()
        bottom.setFixedHeight(4)
        lay.addWidget(bottom)
        return settings_section("应用与历史", card)

    def _build_recognition_section(self) -> QWidget:
        """Common preferences, with actual runtime feedback supplied by App."""
        card, lay = _card()

        self.meeting_language_combo = QComboBox()
        self.meeting_language_combo.setAccessibleName("识别语言")
        for key, label in (("auto", "自动（中文优先）"), ("zh", "中文"), ("en", "English"), ("ja", "日本語")):
            self.meeting_language_combo.addItem(label, key)
        self.meeting_language_combo.currentIndexChanged.connect(
            lambda _idx=0: self.recognition_setting_changed.emit()
        )
        lay.addWidget(labeled_row("识别语言", self.meeting_language_combo, "支持的模型会按此语言识别"))
        lay.addWidget(group_divider())

        self.meeting_glossary_edit = GlossaryPlainTextEdit()
        self.meeting_glossary_edit.setAccessibleName("自定义词汇")
        self.meeting_glossary_edit.setPlaceholderText("SayInk\n张鑫=张新、张欣")
        self.meeting_glossary_edit.setFixedHeight(96)
        self.meeting_glossary_edit.setTabChangesFocus(True)
        self.paint_glossary_edit()
        self.meeting_glossary_edit.textChanged.connect(self.glossary_text_changed.emit)
        self.meeting_glossary_edit.editing_finished.connect(self.glossary_editing_finished.emit)
        lay.addWidget(stacked_field_row(
            "自定义词汇", self.meeting_glossary_edit,
            "每行填写一个产品名、人名或专业术语，单次转写、持续转写和会议共用。"
            "支持热词的模型会在识别时参考这些词，词汇过多可能影响识别效果。\n"
            "填写「正确词=误写1、误写2」可在识别后直接替换匹配的文字，适用于所有模型；请避免使用容易误替换的词。\n"
            "最多 50 个词；正确词和每个误写各不超过 20 字，每个词最多 5 个误写。离开输入框、切换页面或关闭设置时保存。",
        ))
        self.glossary_count_label = footnote("")
        self.meeting_glossary_edit.textChanged.connect(self.update_glossary_count)
        lay.addWidget(inline_action_row(self.glossary_count_label, margins=(16, 0, 16, 8)))
        self.update_glossary_count()
        self.recognition_capability_label = footnote("")
        self.recognition_capability_row = inline_action_row(self.recognition_capability_label, margins=(16, 0, 16, 10))
        lay.addWidget(self.recognition_capability_row)
        self.recognition_apply_label = footnote("")
        lay.addWidget(inline_action_row(self.recognition_apply_label, margins=(16, 0, 16, 10)))

        self.advanced_recognition_btn = device_selection_link("高级识别设置")
        self.advanced_recognition_btn.setAccessibleName("展开高级识别设置")
        self.advanced_recognition_btn.toggled.connect(self.set_advanced_recognition_expanded)
        lay.addWidget(inline_action_row(self.advanced_recognition_btn, margins=(16, 0, 16, 12)))
        self.advanced_recognition_panel = QWidget()
        self.advanced_recognition_panel.setVisible(False)
        advanced = QVBoxLayout(self.advanced_recognition_panel)
        advanced.setContentsMargins(0, 0, 0, 0)
        advanced.setSpacing(0)
        advanced.addWidget(group_divider())
        segmentation_hint = footnote("自动按语音和停顿分段；检测模型不可用时按音量分段。分段不决定标点，单次转写在松开快捷键后统一输出。")
        advanced.addWidget(inline_action_row(segmentation_hint, margins=(16, 10, 16, 8)))
        self.vad_runtime_label = footnote("尚未获取语音分段运行状态")
        self.vad_runtime_label.setAccessibleName("语音分段实际运行状态")
        advanced.addWidget(inline_action_row(self.vad_runtime_label, margins=(16, 0, 16, 8)))
        self.vad_download_status = footnote("")
        self.vad_download_status.setProperty("viRole", "importantHint")
        self.vad_download_status_row = inline_action_row(self.vad_download_status, margins=(16, 0, 16, 10))
        advanced.addWidget(self.vad_download_status_row)
        self.vad_download_btn = _ghost_button("下载语音检测模型（约 2 MB）", self.vad_download_requested.emit)
        self.vad_download_row = inline_action_row(self.vad_download_btn, margins=(16, 0, 16, 12))
        self.vad_download_row.layout().setAlignment(self.vad_download_btn, Qt.AlignmentFlag.AlignLeft)
        advanced.addWidget(self.vad_download_row)

        # Retired manual switches kept off-layout as hidden compatibility stubs
        # so typed-page legacy attribute delegations never raise AttributeError.
        self.quiet_speech_row = ToggleOptionRow("")
        self.quiet_speech_row.hide()
        self.boundary_context_row = ToggleOptionRow(
            "跨段音频上下文（实验性）",
            "默认关闭；仅支持带时间戳的 CTC 模型，强制分段最多多等 0.3 秒。尚未验证稳定精度收益。",
        )
        self.boundary_context_row.toggled.connect(
            lambda _checked=False: self.recognition_setting_changed.emit()
        )
        self.boundary_context_row.hide()
        self.speaker_enabled_row = ToggleOptionRow("")
        self.speaker_enabled_row.hide()
        self.speaker_activity_row = ToggleOptionRow("")
        self.speaker_activity_row.hide()

        advanced.addWidget(group_divider())
        speaker_diag_title = footnote("说话人区分（会议及使用电脑声音的持续转写中自动启用）")
        speaker_diag_title.setProperty("viRole", "importantHint")
        advanced.addWidget(inline_action_row(speaker_diag_title, margins=(16, 10, 16, 4)))
        self.speaker_runtime_label = footnote("尚未获取说话人区分运行状态")
        advanced.addWidget(inline_action_row(self.speaker_runtime_label, margins=(16, 0, 16, 8)))
        lay.addWidget(self.advanced_recognition_panel)
        section = settings_section("识别", card)
        section.setObjectName("sharedRecognitionSection")
        return section

    def _build_meeting_section(self) -> QWidget:
        from sayink.speech_recognizer import MODEL_REGISTRY

        card, lay = _card()
        self.meeting_source_combo = QComboBox()
        self.meeting_source_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.meeting_source_combo.setMinimumContentsLength(16)
        self.meeting_source_combo.setAccessibleName("会议音频来源")
        self.meeting_source_combo.addItem("跟随转写", "")
        for key in ("mixed", "microphone", "system"):
            self.meeting_source_combo.addItem(input_source_label(key), key)
        self.meeting_source_combo.currentIndexChanged.connect(
            lambda _idx=0: self.meeting_setting_changed.emit()
        )
        lay.addWidget(labeled_row("默认音频来源", self.meeting_source_combo, "跟随转写设置，或为会议固定选择一个音源"))
        lay.addWidget(group_divider())
        self.meeting_model_combo = QComboBox()
        self.meeting_model_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.meeting_model_combo.setMinimumContentsLength(16)
        self.meeting_model_combo.setAccessibleName("会议识别模型")
        self.meeting_model_combo.addItem("与转写相同", "")
        for model in MODEL_REGISTRY:
            self.meeting_model_combo.addItem(model["name"], model["id"])
        self.meeting_model_combo.currentIndexChanged.connect(
            lambda _idx=0: self.meeting_setting_changed.emit()
        )
        lay.addWidget(labeled_row("会议识别模型", self.meeting_model_combo, "可为会议单独指定；开始卡片的修改仅用于本场"))
        self.meeting_defaults_hint = footnote("")
        self.meeting_defaults_hint.setProperty("viRole", "importantHint")
        lay.addWidget(inline_action_row(self.meeting_defaults_hint, margins=(16, 0, 16, 10)))
        lay.addWidget(group_divider())
        self.meeting_start_card_row = ToggleOptionRow(
            "开始前显示会议信息卡", "填写标题、参会人与议程；关闭后从托盘直接开始",
        )
        self.meeting_start_card_row.toggled.connect(
            lambda _checked=False: self.meeting_setting_changed.emit()
        )
        lay.addWidget(self.meeting_start_card_row)
        self.meeting_start_card_reset_btn = _ghost_button(
            "开始卡片恢复默认",
            self.meeting_start_card_reset_requested.emit,
            tooltip="仅重新开启开始前的会议信息卡，不改变会议音源、会议模型和纪要模板",
        )
        lay.addWidget(inline_action_row(self.meeting_start_card_reset_btn))
        section = settings_section("会议默认设置", card)
        section.setObjectName("meetingDefaultsSection")
        return section

    # ── Page-owned UI & state helpers ──────────────────────

    def paint_glossary_edit(self) -> None:
        self.meeting_glossary_edit.setStyleSheet(f"""
            QPlainTextEdit {{
                background: {tok.INPUT_BG}; color: {tok.TEXT};
                border: 1px solid {tok.HAIRLINE}; border-radius: {tok.RADIUS_MD}px;
                padding: 8px 12px; font-size: {tok.TYPE_BODY_SM}px; font-family: {tok.FONT};
            }}
            QPlainTextEdit:focus {{
                border: 2px solid {tok.ACCENT_FOCUS};
                padding: 7px 11px;
            }}
        """)

    def apply_theme(self) -> None:
        self.hotkey_hint.setStyleSheet(
            f"color: {tok.TEXT_SEC}; font-size: {tok.TYPE_FOOTNOTE}px; line-height: 1.4;"
            f" background: transparent; padding: 0 16px 12px 16px;"
        )
        for button in self.general_section_jumps:
            button.setStyleSheet(settings_styles.BTN_GHOST_SM)
        paint_device_selection_link(self.advanced_audio_btn)
        paint_device_selection_link(self.advanced_recognition_btn)
        self.paint_glossary_edit()
        self.mic_level_bar.setStyleSheet(
            f"QProgressBar {{ border: none; background: {tok.SURFACE_PEARL}; border-radius: 3px; }}"
            f"QProgressBar::chunk {{ background: {tok.ACCENT}; border-radius: 3px; }}"
        )

    def selected_input_source(self) -> str:
        if self.src_sys_rb.isChecked():
            return INPUT_SOURCE_SYSTEM
        if self.src_mixed_rb.isChecked():
            return INPUT_SOURCE_MIXED
        return INPUT_SOURCE_MICROPHONE

    def selected_trigger_mode(self) -> str:
        if self.trigger_hotkey_rb.isChecked():
            return TRIGGER_MODE_HOTKEY
        return TRIGGER_MODE_CONTINUOUS

    def sync_source_device_controls(self) -> None:
        src = self.selected_input_source()
        mic_on = src in (INPUT_SOURCE_MICROPHONE, INPUT_SOURCE_MIXED)
        sys_on = src in (INPUT_SOURCE_SYSTEM, INPUT_SOURCE_MIXED)
        self.mic_device_combo.setEnabled(mic_on)
        self.system_device_combo.setEnabled(sys_on)
        mixed = self.src_mixed_rb.isChecked()
        show_note = mixed or src == INPUT_SOURCE_SYSTEM
        self.mixed_audio_callout.setText(MIXED_AUDIO_NOTE if mixed else SYSTEM_AUDIO_NOTE)
        self.mixed_audio_callout.setVisible(show_note)
        set_section_visible(self.mixed_audio_callout_wrap, show_note)

    def apply_input_source_radios(self, source: str) -> None:
        if source == INPUT_SOURCE_SYSTEM:
            self.src_sys_rb.setChecked(True)
        elif source == INPUT_SOURCE_MIXED:
            self.src_mixed_rb.setChecked(True)
        else:
            self.src_mic_rb.setChecked(True)
        self.sync_source_device_controls()
        sync_group_tab_stop(self.source_group)

    def apply_trigger_mode_radios(self, mode: str) -> None:
        if mode == TRIGGER_MODE_HOTKEY:
            self.trigger_hotkey_rb.setChecked(True)
        else:
            self.trigger_continuous_rb.setChecked(True)
        sync_group_tab_stop(self.trigger_group)

    def ensure_automatic_device_items(self) -> None:
        for combo in (self.mic_device_combo, self.system_device_combo):
            blocker = QSignalBlocker(combo)
            if combo.findData(-1) < 0:
                combo.insertItem(0, "自动选择", -1)
            del blocker

    def saved_devices_are_listed(self, mic: int, system: int) -> bool:
        return (
            self.mic_device_combo.findData(mic) >= 0
            and self.system_device_combo.findData(system) >= 0
        )

    def populate_audio_device_combos(
        self,
        mic_devices_fn: Callable[[], list],
        system_devices_fn: Callable[[], list],
    ) -> None:
        mic_sel = self.mic_device_combo.currentData() if self.mic_device_combo.count() else -1
        sys_sel = self.system_device_combo.currentData() if self.system_device_combo.count() else -1

        mic_blocker = QSignalBlocker(self.mic_device_combo)
        sys_blocker = QSignalBlocker(self.system_device_combo)

        self.mic_device_combo.clear()
        self.mic_device_combo.addItem("自动选择", -1)
        try:
            for dev in mic_devices_fn():
                self.mic_device_combo.addItem(dev.label, dev.index)
        except Exception as exc:
            self.mic_device_combo.addItem(f"枚举失败: {exc}", -1)

        self.system_device_combo.clear()
        self.system_device_combo.addItem("自动选择", -1)
        try:
            for dev in system_devices_fn():
                self.system_device_combo.addItem(dev.label, dev.index)
        except Exception as exc:
            self.system_device_combo.addItem(f"枚举失败: {exc}", -1)

        for combo, selected in ((self.mic_device_combo, mic_sel), (self.system_device_combo, sys_sel)):
            if selected is None:
                continue
            if int(selected) >= 0 and combo.findData(int(selected)) < 0:
                combo.addItem("原设备已不可用，请重新选择", int(selected))
            set_combo_by_data(combo, int(selected))
        del mic_blocker, sys_blocker

    def current_audio_probe_config(self) -> tuple[str, int, int]:
        return (
            self.selected_input_source(),
            int(self.mic_device_combo.currentData() if self.mic_device_combo.currentData() is not None else -1),
            int(self.system_device_combo.currentData() if self.system_device_combo.currentData() is not None else -1),
        )

    def update_audio_device_summary(self) -> None:
        source = self.selected_input_source()
        lines = []
        for key, title, combo in (
            (INPUT_SOURCE_MICROPHONE, "麦克风", self.mic_device_combo),
            (INPUT_SOURCE_SYSTEM, "电脑声音", self.system_device_combo),
        ):
            if source not in (key, INPUT_SOURCE_MIXED):
                continue
            automatic = device_index(combo.currentData()) < 0
            device = "自动选择（测试或录音时确定设备）" if automatic else combo.currentText()
            lines.append(f"{title}：{device}")
        self.audio_device_summary.setText("\n".join(lines))
        summary_height = self.audio_device_summary.fontMetrics().lineSpacing() * 2 + 2
        if self.audio_device_summary.minimumHeight() < summary_height:
            self.audio_device_summary.setMinimumHeight(summary_height)

    def set_probe_controls_enabled(self, enabled: bool) -> None:
        for widget in (
            self.mic_test_btn,
            self.mic_reset_btn,
            self.advanced_audio_btn,
            self.advanced_audio_panel,
            self.src_mic_rb.parentWidget(),
            self.src_sys_rb.parentWidget(),
            self.src_mixed_rb.parentWidget(),
        ):
            if widget is not None:
                widget.setEnabled(enabled)

    def set_advanced_audio_expanded(
        self,
        visible: bool,
        *,
        before_show: Callable[[], None] | None = None,
    ) -> None:
        def change() -> None:
            if visible and before_show is not None:
                before_show()
            set_section_visible(self.advanced_audio_panel, visible)
            self.advanced_audio_btn.setText(
                "收起音频设备" if visible else "手动选择音频设备"
            )
            self.advanced_audio_btn.setAccessibleName(
                "收起手动选择音频设备" if visible else "展开手动选择音频设备"
            )
            set_checked_link(self.advanced_audio_btn, visible)

        without_scroll_jump(self.advanced_audio_btn, change)

    def set_advanced_recognition_expanded(self, visible: bool) -> None:
        def change() -> None:
            set_section_visible(self.advanced_recognition_panel, visible)
            self.advanced_recognition_btn.setText(
                "收起高级识别设置" if visible else "高级识别设置"
            )
            self.advanced_recognition_btn.setAccessibleName(
                "收起高级识别设置" if visible else "展开高级识别设置"
            )
            set_checked_link(self.advanced_recognition_btn, visible)

        without_scroll_jump(self.advanced_recognition_btn, change)

    def refresh_history_controls(self) -> None:
        prefix = "" if self.history_enabled_row.isChecked() else "已停止新增转写；"
        self.history_cleanup_hint.setText(
            prefix + "已有转写仍按以上规则保留，超限清理无法撤销。保存新转写及启动时执行；会议记录不受影响。"
        )

    def refresh_hotkey_hint(self, config: Config) -> None:
        self.hotkey_hint.setText(dictation_hints(config).detail)
        self.hotkey_hint.setToolTip("启动阈值：单次转写按住约 0.18 秒；持续转写按住约 0.30 秒，避免误触。")
        is_default = config.get("hotkey", DEFAULT_HOTKEY) == DEFAULT_HOTKEY
        self.hotkey_reset_btn.setEnabled(True)
        self.hotkey_reset_btn.setToolTip(
            "当前已是 Alt+X。点此只会再次确认，不改变转写模式、Esc、音频来源和结果去向。"
            if is_default else
            "恢复为 Alt+X，不改变转写模式、Esc、音频来源和结果去向。"
        )
        continuous = self.selected_trigger_mode() == TRIGGER_MODE_CONTINUOUS
        set_section_visible(self.esc_stop_row, continuous)
        set_section_visible(self.esc_stop_divider, continuous)

    def refresh_output_controls(self, config: Config) -> None:
        hints = dictation_hints(config)
        automatic = config.output_follows_input_source()
        choice = "auto" if automatic else config.output_mode()
        was_blocked = self.output_mode_combo.blockSignals(True)
        set_combo_by_data(self.output_mode_combo, choice)
        self.output_mode_combo.blockSignals(was_blocked)
        history_only = config.history_only_output()
        destination = "仅保存到历史，不输入其他应用" if history_only else "输入当前光标"
        rule = (
            "自动规则：麦克风输入当前光标，电脑声音和混合音源仅保存到历史"
            if automatic else "固定选择，切换音源后保持不变"
        )
        mode_name = "持续转写" if self.selected_trigger_mode() == TRIGGER_MODE_CONTINUOUS else "单次转写"
        other_name = "单次转写" if mode_name == "持续转写" else "持续转写"
        text = f"当前是{mode_name}：{destination}。{rule}。{other_name}沿用自己的选择。"
        color = tok.TEXT_SEC
        if hints.output_warning:
            text = "历史已关闭：结果不会保存或输入其他应用。请开启转写历史，或改为输入当前光标。"
            color = tok.RED
        elif not history_only and self.selected_input_source() != INPUT_SOURCE_MICROPHONE:
            text += " 电脑声音的识别结果也会输入光标处，请确认目标窗口。"
        self.output_mode_hint.setText(text)
        self.output_mode_hint.setStyleSheet(
            f"color: {color}; font-size: {tok.TYPE_FOOTNOTE}px; background: transparent;"
        )
        set_section_visible(self.enable_history_wrap, hints.output_warning)
        self.restore_clipboard_row.setEnabled(not history_only)
        self.restore_clipboard_row.setToolTip("仅在结果输入当前光标时使用" if history_only else "")

    def refresh_meeting_defaults(self, config: Config) -> None:
        from sayink.speech_recognizer import get_model_info, is_model_downloaded

        model_id = config.get("stt.model_id", "")
        info = get_model_info(model_id)
        model_name = info["name"] if info else model_id or "尚未选择"
        if self.meeting_model_combo.count():
            self.meeting_model_combo.setItemText(0, f"与转写相同（当前：{model_name}）")
        selected = self.meeting_model_combo.currentData()
        selected_info = get_model_info(selected) if selected else None
        effective_name = selected_info["name"] if selected_info else model_name
        model_missing = bool(selected and not is_model_downloaded(selected))
        if model_missing:
            effective_name = f"{effective_name} 未下载，请到「引擎」下载或改选「与转写相同」"
        effective_source = self.meeting_source_combo.currentText()
        if self.meeting_source_combo.currentData() == "":
            effective_source = f"{effective_source}（当前：{dictation_hints(config).source}）"
        self.meeting_defaults_hint.setText(
            f"新会议：{effective_source} · {effective_name}。仅影响之后开始的会议。"
        )
        self.meeting_defaults_hint.setStyleSheet(
            f"color: {tok.AMBER_TEXT if model_missing else tok.TEXT_SEC};"
            f" font-size: {tok.TYPE_FOOTNOTE}px; background: transparent;"
        )

    def refresh_recognition_feedback(
        self,
        config: Config,
        *,
        segmentation_status: SegmentationStatus | None,
        speaker_runtime_text: str,
        recognition_pending: bool,
        glossary_skip_note: str = "",
        vad_downloading: bool = False,
    ) -> None:
        from sayink.speech_recognizer import AUX_MODEL_SILERO_VAD, is_model_downloaded

        shared = RecognitionSettings.from_config(config)
        downloaded = is_model_downloaded(AUX_MODEL_SILERO_VAD)
        failed = bool(segmentation_status is not None and segmentation_status.error)
        if segmentation_status is not None:
            scope = "当前会议" if segmentation_status.meeting else "当前转写"
            text = f"{scope}：{segmentation_status.label}"
            if (
                recognition_pending
                and downloaded
                and segmentation_status.requested == "silero"
                and not segmentation_status.model_available
            ):
                text = f"{scope}：Silero VAD 已下载，待本场结束后加载 · 当前使用能量断句"
            self.vad_runtime_label.setToolTip(segmentation_status.error)
        elif downloaded:
            text = "Silero VAD 已下载 · 尚未确认加载状态"
        else:
            text = "Silero VAD 未下载 · 自动使用能量断句"
        self.vad_runtime_label.setText(text)
        fg = tok.TEXT_SEC
        if segmentation_status is not None:
            if segmentation_status.error:
                fg = tok.RED
            elif segmentation_status.effective == "silero":
                fg = tok.GREEN_TEXT
            elif segmentation_status.requested == "silero":
                fg = tok.AMBER_TEXT
        self.vad_runtime_label.setStyleSheet(
            f"color: {fg}; font-size: {tok.TYPE_FOOTNOTE}px; background: transparent;"
        )
        self.recognition_apply_label.setStyleSheet(
            f"color: {tok.AMBER_TEXT if recognition_pending else tok.TEXT_SEC};"
            f" font-size: {tok.TYPE_FOOTNOTE}px; background: transparent;"
        )
        self.speaker_runtime_label.setText(speaker_runtime_text)
        self.recognition_capability_label.setText("\n".join(shared.notes))
        self.recognition_capability_label.setVisible(bool(shared.notes))
        set_section_visible(self.recognition_capability_row, bool(shared.notes))
        apply_parts: list[str] = []
        if recognition_pending:
            apply_parts.append("已保存 · 待本场结束且已录语音处理完成后生效。")
        if glossary_skip_note:
            apply_parts.append(glossary_skip_note)
        if failed:
            apply_parts.append("语音检测模型加载失败，当前按音量分段。可展开高级设置修复。")
        apply_text = " ".join(apply_parts)
        self.recognition_apply_label.setText(apply_text)
        show_apply = bool(apply_text)
        self.recognition_apply_label.setVisible(show_apply)
        apply_row = self.recognition_apply_label.parentWidget()
        if apply_row is not None:
            set_section_visible(apply_row, show_apply)
        if not vad_downloading:
            set_section_visible(self.vad_download_row, not downloaded or failed)
            set_section_visible(self.vad_download_status_row, False)
            self.vad_download_btn.setEnabled(True)
            self.vad_download_btn.setText("修复语音检测模型" if downloaded and failed else "下载语音检测模型（约 2 MB）")
            self.vad_download_btn.setToolTip(
                "校验或重新下载模型，并在空闲时重新加载。" if downloaded and failed else ""
            )

    def update_vad_download_progress(self, pct: int) -> None:
        self.vad_download_row.show()
        self.vad_download_status_row.hide()
        self.vad_download_btn.setEnabled(False)
        self.vad_download_btn.setText(f"下载中 {int(pct)}%")

    def load_config(self, config: Config) -> None:
        from sayink.ui.island_chrome import normalize_listen_bar_placement

        proxy_mode = config.get("llm.proxy_mode", "system")
        set_combo_by_data(self.llm_proxy_combo, proxy_mode if proxy_mode in ("system", "direct") else "system")
        self.llm_proxy_suffix_edit.setText(str(config.get("llm.proxy_bypass_suffixes", "") or ""))
        self.hotkey_edit.set_value(config.get("hotkey", DEFAULT_HOTKEY))
        self.auto_start_row.setChecked(config.get("auto_start", False))
        self.sound_row.setChecked(config.get("sound_enabled", True))
        self.restore_clipboard_row.setChecked(config.get("output.restore_clipboard", False))
        output_choice = "auto" if config.output_follows_input_source() else config.output_mode()
        set_combo_by_data(self.output_mode_combo, output_choice)
        self.esc_stop_row.setChecked(bool(config.get("audio.esc_stops_continuous", True)))
        self.history_enabled_row.setChecked(config.get("history.enabled", True))
        self.recovery_row.setChecked(bool(config.get("audio.recovery_enabled", False)))
        self.separate_lanes_row.setChecked(bool(config.get("audio.separate_lanes", False)))
        self.history_retention_days_spin.setValue(
            int_or(config.get("history.retention_days", 90), 90)
        )
        self.history_max_entries_spin.setValue(
            int_or(config.get("history.max_entries", 5000), 5000)
        )
        self.refresh_history_controls()

        theme_mode = normalize_theme_mode(config.get("appearance.theme_mode", "dark"))
        idx = self.theme_combo.findData(theme_mode)
        if idx < 0:
            idx = self.theme_combo.findData("system")
        self.theme_combo.blockSignals(True)
        self.theme_combo.setCurrentIndex(max(0, idx))
        self.theme_combo.blockSignals(False)

        set_combo_by_data(
            self.listen_bar_combo,
            normalize_listen_bar_placement(config.get("appearance.listen_bar_placement", "auto")),
        )
        self.listen_bar_autohide_row.setChecked(bool(config.get("appearance.listen_bar_autohide", True)))

        self.apply_input_source_radios(config.get("audio.input_source", INPUT_SOURCE_MICROPHONE))
        self.apply_trigger_mode_radios(config.get("audio.trigger_mode", TRIGGER_MODE_HOTKEY))

    def load_recognition_and_meeting(self, config: Config) -> None:
        from sayink.asr_correction import format_glossary_line

        set_combo_by_data(self.meeting_source_combo, config.get("meeting.input_source", "") or "")
        set_combo_by_data(self.meeting_model_combo, config.get("meeting.model_id", "") or "")
        set_combo_by_data(self.meeting_language_combo, config.get("stt.language", "auto"))
        self.meeting_start_card_row.setChecked(bool(config.get("meeting.show_start_card", True)))
        self.boundary_context_row.setChecked(bool(config.get("stt.boundary_context_enabled", False)))

        glossary = config.get("stt.glossary", []) or []
        lines = (format_glossary_line(g) for g in glossary)
        text = "\n".join(line for line in lines if line)
        if self.meeting_glossary_edit.toPlainText() != text:
            blocker = QSignalBlocker(self.meeting_glossary_edit)
            self.meeting_glossary_edit.setPlainText(text)
            del blocker
        self.update_glossary_count()

    def update_glossary_count(self) -> None:
        count = sum(bool(line.strip()) for line in self.meeting_glossary_edit.toPlainText().splitlines())
        self.glossary_count_label.setText(f"已填写 {count} 行 / 最多 50 个词")

    def parse_glossary_editor(self) -> tuple[list[dict], str]:
        from sayink.asr_correction import parse_glossary_line_with_warnings
        from sayink.speech_recognizer import MAX_HOTWORD_CHARS, MAX_HOTWORDS

        terms: list[dict] = []
        seen: set[str] = set()
        skipped: list[str] = []
        for line_number, raw in enumerate(self.meeting_glossary_edit.toPlainText().splitlines(), 1):
            entry, warnings = parse_glossary_line_with_warnings(raw)
            if warnings:
                skipped.append(f"第 {line_number} 行：{'、'.join(warnings)}，相关内容未保存")
            if entry is None:
                continue
            term = entry["term"]
            if len(term) > MAX_HOTWORD_CHARS:
                skipped.append(f"「{term[:8]}…」超过 {MAX_HOTWORD_CHARS} 字，未写入")
                continue
            if term in seen:
                skipped.append(f"第 {line_number} 行：正确词重复，未保存")
                continue
            if len(terms) >= MAX_HOTWORDS:
                skipped.append(f"超过 {MAX_HOTWORDS} 个词，其余未保存")
                break
            seen.add(term)
            entry["term"] = term
            terms.append(entry)
        return terms, " ".join(skipped)


def build_general_page(_owner: object = None, *, parent: QWidget | None = None) -> GeneralSettingsPage:
    """Construct a typed ``GeneralSettingsPage`` without mutating external window state."""
    return GeneralSettingsPage(parent=parent)

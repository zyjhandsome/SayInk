"""Composed start-card, helper-model download, and live meeting workspace controller for ``MeetingPage``.

Replaces inheritance-based ``_MeetingLiveMixin`` with explicit composition.
"""

from __future__ import annotations

import html
import time
from typing import Any

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from sayink.audio_devices import input_source_label
from sayink.aux_models import aux_models_size_mb, describe_aux_models
from sayink.meeting_session import (
    MeetingOptions,
    MeetingState,
    minutes_settings_complete,
    resolve_capabilities,
)
from sayink.minutes_prompts import format_timestamp, transcript_lines
from sayink.recognition_settings import ADVANCED_RECOGNITION_PATH, audio_source
from sayink.speech_recognizer import DEFAULT_MODEL_ID, MODEL_REGISTRY
from sayink.ui.flow_layout import PairedToolBar
from sayink.ui.meeting_dialogs import _quiet_tooltips, _style_dialog_box
from sayink.ui.meeting_player import _CleanComboBox, _LevelMeterBar
from sayink.ui.meeting_views import _CompactTextBrowser, _TranscriptView, find_in_reading_view
_SOURCE_LABELS = tuple((key, input_source_label(key)) for key in ("mixed", "microphone", "system"))
_LANGUAGE_LABELS = (("auto", "自动（中文优先）"), ("zh", "中文"), ("en", "English"), ("ja", "日本語"))

LIVE_REMOTE_SILENCE_THRESHOLD = 0.008
LIVE_REMOTE_SILENCE_SECONDS = 15.0
DEFAULT_SYSTEM_SILENT_MSG = (
    "电脑声（对方）已连续 15 秒无声音输入：线下会议可忽略。线上会议若正在使用蓝牙耳机"
    "（Hands-Free 通信设备）或外接声卡，请检查 Windows 默认播放设备与会议软件扬声器输出是否一致。"
)


def _format_live_capabilities(caps: Any) -> tuple[str, str]:
    vad_label = "Silero VAD 已加载" if caps.vad == "silero" else "能量断句"
    if caps.vad_error:
        vad_label += "（Silero 加载失败，已回退）"
    elif caps.vad_requested == "silero" and caps.vad == "energy":
        vad_label += "（Silero 未下载）"
    bits = [f"模型 {caps.model_id}", vad_label]
    bits.append("说话人模型" if caps.speaker_model_available else "说话人（粗略）")
    if caps.speaker_model_available:
        bits.append("会内换人检测（实验性）；短促插话可能仍需确认")
    if caps.hotwords and caps.hotwords_supported:
        bits.append("热词已启用")
    summary = "说话人区分已增强" if caps.speaker_model_available else "说话人区分较粗略"
    return summary, " · ".join(bits)


class MeetingLiveController:
    """Composed start-card, helper-model downloads, and live session controller."""

    def __init__(self, host: Any) -> None:
        self._host = host

    def build_start_card(self) -> QWidget:
        h = self._host
        page = QWidget()
        page_lay = QVBoxLayout(page)
        page_lay.setContentsMargins(0, 0, 0, 0)
        page_lay.setSpacing(8)
        h._start_scroll = QScrollArea()
        h._start_scroll.setFrameShape(QFrame.Shape.NoFrame)
        h._start_scroll.setWidgetResizable(True)
        h._start_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        h._start_scroll.setWidget(content)
        page_lay.addWidget(h._start_scroll, 1)
        lay = QVBoxLayout(content)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        h._card_title = QLabel("开始会议")
        h._card_title.setObjectName("meetingStartCardTitle")
        lay.addWidget(h._card_title)
        h._card_hint = QLabel(
            "音源、模型和语言仅作用于本场会议；下方标题、参会人与议程均为选填，写进纪要并帮助模型理解上下文。"
        )
        h._card_hint.setWordWrap(True)
        lay.addWidget(h._card_hint)

        row = QHBoxLayout()
        row.setSpacing(10)
        h._opt_source = _CleanComboBox()
        h._opt_source.setAccessibleName("会议音频来源")
        for key, label in _SOURCE_LABELS:
            h._opt_source.addItem(label, key)
        h._opt_model = _CleanComboBox()
        h._opt_model.setAccessibleName("会议识别模型")
        h._opt_language = _CleanComboBox()
        h._opt_language.setAccessibleName("会议语言")
        for key, label in _LANGUAGE_LABELS:
            h._opt_language.addItem(label, key)
        h._card_field_labels = []
        for caption, field, stretch in (
            ("音频来源", h._opt_source, 3),
            ("转写模型", h._opt_model, 4),
            ("识别语言", h._opt_language, 3),
        ):
            field.setMinimumWidth(0)
            field.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            column = QVBoxLayout()
            column.setSpacing(4)
            label = QLabel(caption)
            label.setBuddy(field)
            h._card_field_labels.append(label)
            column.addWidget(label)
            column.addWidget(field)
            row.addLayout(column, stretch)
        lay.addLayout(row)

        h._opt_loopback_hint = QLabel("")
        h._opt_loopback_hint.setWordWrap(True)
        h._opt_loopback_hint.setAccessibleName("电脑声回环输出设备")
        h._opt_loopback_hint.hide()
        lay.addWidget(h._opt_loopback_hint)

        meta_row = QHBoxLayout()
        meta_row.setSpacing(10)
        h._opt_title = QLineEdit()
        h._opt_title.setPlaceholderText("会议标题（选填，留空按时间命名）")
        h._opt_title.setAccessibleName("会议标题")
        meta_row.addWidget(h._opt_title, 1)
        h._opt_participants = QLineEdit()
        h._opt_participants.setPlaceholderText("参会人（选填，逗号分隔）")
        h._opt_participants.setAccessibleName("参会人")
        meta_row.addWidget(h._opt_participants, 1)
        lay.addLayout(meta_row)

        h._opt_agenda = QPlainTextEdit()
        h._opt_agenda.setPlaceholderText("议程 / 背景（选填，每行一条）")
        h._opt_agenda.setAccessibleName("议程")
        h._opt_agenda.setFixedHeight(60)
        lay.addWidget(h._opt_agenda)

        h._card_notes = QLabel("")
        h._card_notes.setWordWrap(True)
        h._card_notes.hide()
        lay.addWidget(h._card_notes)
        dl_row = QHBoxLayout()
        h._card_dl_btn = QPushButton("下载辅助模型")
        h._card_dl_btn.setAccessibleName("下载会议辅助模型")
        h._card_dl_btn.clicked.connect(h._download_aux_models)
        h._card_dl_btn.hide()
        dl_row.addWidget(h._card_dl_btn, 0)
        dl_row.addStretch(1)
        lay.addLayout(dl_row)
        lay.addStretch(1)

        actions = QHBoxLayout()
        actions.addStretch(1)
        h._card_cancel = QPushButton("取消")
        h._card_cancel.clicked.connect(h._leave_start_card)
        actions.addWidget(h._card_cancel)
        h._card_start = QPushButton("开始")
        h._card_start.setAccessibleName("开始会议记录")
        h._card_start.setDefault(True)
        h._card_start.clicked.connect(h._start_from_card)
        actions.addWidget(h._card_start)
        page_lay.addLayout(actions)
        h._opt_source.currentIndexChanged.connect(h._update_card_notes)
        h._opt_model.currentIndexChanged.connect(h._update_card_notes)
        return page

    def _build_live_header_and_tools(self, lay: QVBoxLayout) -> None:
        h = self._host
        head = QHBoxLayout()
        head.setSpacing(12)
        h._live_title = QLabel("会议进行中")
        h._live_title.setTextFormat(Qt.TextFormat.PlainText)
        h._live_title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        head.addWidget(h._live_title, 1)
        h._live_rec_dot = QLabel()
        h._live_rec_dot.setFixedSize(8, 8)
        h._live_rec_dot.setAccessibleName("正在记录")
        head.addWidget(h._live_rec_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        h._live_clock = QLabel("00:00:00")
        h._live_clock.setObjectName("meetingLiveClock")
        h._live_clock.setAccessibleName("会议已进行时间")
        head.addWidget(h._live_clock, 0)
        h._pause_btn = QPushButton("暂停采集")
        h._pause_btn.setAccessibleName("暂停会议采集")
        h._pause_btn.setToolTip("暂停期间不采集新音频；已录内容继续识别，会议记录保留。")
        h._pause_btn.clicked.connect(h.pause_requested.emit)
        head.addWidget(h._pause_btn, 0)
        h._resume_btn = QPushButton("继续采集")
        h._resume_btn.setAccessibleName("继续会议采集")
        h._resume_btn.setToolTip("继续采集新的会议音频。")
        h._resume_btn.clicked.connect(h.resume_requested.emit)
        h._resume_btn.hide()
        head.addWidget(h._resume_btn, 0)
        h._end_btn = QPushButton("结束会议")
        h._end_btn.setAccessibleName("结束会议")
        h._end_btn.clicked.connect(h._confirm_end)
        head.addWidget(h._end_btn, 0)
        lay.addLayout(head)

        h._system_silent_banner = QFrame()
        h._system_silent_banner.setObjectName("meetingSilentBanner")
        banner_lay = QHBoxLayout(h._system_silent_banner)
        banner_lay.setContentsMargins(10, 6, 8, 6)
        banner_lay.setSpacing(8)
        h._system_silent_label = QLabel()
        h._system_silent_label.setTextFormat(Qt.TextFormat.PlainText)
        h._system_silent_label.setWordWrap(True)
        h._system_silent_label.setAccessibleName("电脑声静音提醒")
        banner_lay.addWidget(h._system_silent_label, 1)
        h._system_silent_dismiss_btn = QPushButton("知道了")
        h._system_silent_dismiss_btn.setAccessibleName("关闭电脑声静音提醒")
        h._system_silent_dismiss_btn.clicked.connect(h._dismiss_system_silent_warning)
        banner_lay.addWidget(h._system_silent_dismiss_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        h._system_silent_banner.hide()
        lay.addWidget(h._system_silent_banner)

        h._live_meters_bar = QWidget()
        h._live_meters_bar.setObjectName("meetingLiveMeters")
        meters_lay = QHBoxLayout(h._live_meters_bar)
        meters_lay.setContentsMargins(0, 0, 0, 0)
        meters_lay.setSpacing(8)
        h._live_mic_label = QLabel("麦克风")
        meters_lay.addWidget(h._live_mic_label, 0)
        h._live_mic_meter = _LevelMeterBar("麦克风实时电平")
        meters_lay.addWidget(h._live_mic_meter, 1)
        meters_lay.addSpacing(8)
        h._live_sys_label = QLabel("电脑声（对方）")
        meters_lay.addWidget(h._live_sys_label, 0)
        h._live_sys_meter = _LevelMeterBar("电脑声（对方）实时电平")
        meters_lay.addWidget(h._live_sys_meter, 1)
        lay.addWidget(h._live_meters_bar)

        h._live_status = QLabel("")
        h._live_status.setTextFormat(Qt.TextFormat.PlainText)
        h._live_status.setWordWrap(True)
        h._live_status.hide()
        lay.addWidget(h._live_status)

        speaker_tools = PairedToolBar(spacing=8)
        h._live_speaker_hint = QLabel("点击姓名可命名；纠正发言人只影响选中的段落，人工修改会保留。")
        h._live_speaker_hint.setWordWrap(True)
        h._live_speaker_btn = QPushButton("命名说话人")
        h._live_speaker_btn.setAccessibleName("会议记录中命名说话人")
        h._live_speaker_menu = QMenu(h._live_speaker_btn)
        h._live_speaker_menu.aboutToShow.connect(h._populate_live_speaker_menu)
        h._live_speaker_btn.setMenu(h._live_speaker_menu)
        speaker_tools.add_right(h._live_speaker_btn)
        h._live_correct_speaker_btn = QPushButton("纠正发言人")
        h._live_correct_speaker_btn.setCheckable(True)
        h._live_correct_speaker_btn.toggled.connect(h._toggle_live_speaker_correction)
        speaker_tools.add_right(h._live_correct_speaker_btn)
        lay.addWidget(speaker_tools)
        lay.addWidget(h._live_speaker_hint)
        h._live_speaker_feedback = QLabel()
        h._live_speaker_feedback.setTextFormat(Qt.TextFormat.PlainText)
        h._live_speaker_feedback.setWordWrap(True)
        h._live_speaker_feedback.hide()
        lay.addWidget(h._live_speaker_feedback)

    def build_live(self) -> QWidget:
        h = self._host
        page = QWidget()
        lay = QVBoxLayout(page)
        h._live_layout = lay
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        self._build_live_header_and_tools(lay)

        h._live_find_bar = QWidget()
        find_lay = QHBoxLayout(h._live_find_bar)
        find_lay.setContentsMargins(0, 0, 0, 0)
        find_lay.setSpacing(6)
        h._live_find_edit = QLineEdit()
        h._live_find_edit.setPlaceholderText("查找本场文字稿；Enter 下一处，Shift+Enter 上一处")
        h._live_find_edit.setAccessibleName("查找本场文字稿")
        h._live_find_edit.setClearButtonEnabled(True)
        h._live_find_edit.textChanged.connect(lambda: h._find_live_text(restart=True))
        h._live_find_edit.returnPressed.connect(h._find_live_text)
        h._live_find_edit.installEventFilter(h)
        find_lay.addWidget(h._live_find_edit, 1)
        h._live_find_status = QLabel()
        find_lay.addWidget(h._live_find_status)
        h._live_find_prev_btn = QPushButton("上一处")
        h._live_find_prev_btn.setAccessibleName("查找上一处")
        h._live_find_prev_btn.clicked.connect(lambda: h._find_live_text(backward=True))
        find_lay.addWidget(h._live_find_prev_btn)
        h._live_find_next_btn = QPushButton("下一处")
        h._live_find_next_btn.setAccessibleName("查找下一处")
        h._live_find_next_btn.clicked.connect(lambda: h._find_live_text(backward=False))
        find_lay.addWidget(h._live_find_next_btn)
        h._live_find_close_btn = QPushButton("关闭查找")
        h._live_find_close_btn.clicked.connect(h._close_live_find)
        find_lay.addWidget(h._live_find_close_btn)
        h._live_find_bar.hide()
        lay.addWidget(h._live_find_bar)

        h._live_split = QSplitter(Qt.Orientation.Horizontal)
        h._live_split.setChildrenCollapsible(False)
        h._live_transcript = _TranscriptView()
        h._live_transcript.setOpenLinks(False)
        h._live_transcript.anchorClicked.connect(h._on_live_transcript_link)
        h._live_transcript.setAccessibleName("实时文字稿")
        h._live_transcript.setPlaceholderText("开始说话后，这里会按说话人逐条出现文字…")
        h._live_transcript.viewport().installEventFilter(h)
        h._live_follow_bottom = True
        h._live_scroll_guard = 0
        h._live_transcript.verticalScrollBar().valueChanged.connect(h._on_live_scroll_changed)
        h._live_transcript.verticalScrollBar().rangeChanged.connect(h._on_live_range_changed)
        h._live_new_msg_btn = QPushButton("↓ 底部有新发言", h._live_transcript.viewport())
        h._live_new_msg_btn.setAccessibleName("跳到底部查看新发言")
        h._live_new_msg_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        h._live_new_msg_btn.clicked.connect(h._scroll_live_to_bottom)
        h._live_new_msg_btn.hide()
        h._live_split.addWidget(h._live_transcript)
        side = QWidget()
        side_lay = QVBoxLayout(side)
        h._live_side_layout = side_lay
        side_lay.setContentsMargins(12, 0, 0, 0)
        side_lay.setSpacing(8)
        h._marks_title = QLabel("标记")
        side_lay.addWidget(h._marks_title)
        mark_row = QHBoxLayout()
        mark_row.setSpacing(6)
        h._mark_note = QLineEdit()
        h._mark_note.setPlaceholderText("备注（可空）")
        h._mark_note.setAccessibleName("标记备注")
        h._mark_note.returnPressed.connect(h._add_mark)
        mark_row.addWidget(h._mark_note, 1)
        h._mark_btn = QPushButton("标记此刻")
        h._mark_btn.setAccessibleName("标记此刻")
        h._mark_btn.clicked.connect(h._add_mark)
        mark_row.addWidget(h._mark_btn)
        side_lay.addLayout(mark_row)
        h._marks_view = _CompactTextBrowser()
        h._marks_view.setAccessibleName("标记列表")
        h._marks_view.setPlaceholderText("")
        h._marks_view.document().setDocumentMargin(1)
        h._marks_view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        h._marks_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        h._marks_view.setMinimumHeight(36)
        h._marks_view.setMaximumHeight(88)
        side_lay.addWidget(h._marks_view)
        h._notes_title = QLabel("我的笔记")
        side_lay.addWidget(h._notes_title)
        h._user_notes = QPlainTextEdit()
        h._user_notes.setAccessibleName("我的笔记")
        h._user_notes.setPlaceholderText("随手记下提纲或重点；生成纪要时将作为核心主干骨架优先融合。")
        h._user_notes.setMinimumHeight(64)
        h._user_notes_timer = QTimer(h)
        h._user_notes_timer.setSingleShot(True)
        h._user_notes_timer.setInterval(800)
        h._user_notes_timer.timeout.connect(h._save_user_notes)
        h._user_notes.textChanged.connect(lambda: h._user_notes_timer.start())
        side_lay.addWidget(h._user_notes, 1)
        h._live_side_scroll = QScrollArea()
        h._live_side_scroll.setFrameShape(QFrame.Shape.NoFrame)
        h._live_side_scroll.setWidgetResizable(True)
        h._live_side_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        h._live_side_scroll.setMinimumWidth(240)
        h._live_side_scroll.setWidget(side)
        h._live_split.addWidget(h._live_side_scroll)
        h._live_split.setStretchFactor(0, 3)
        h._live_split.setStretchFactor(1, 2)
        lay.addWidget(h._live_split, 1)

        actions = QHBoxLayout()
        h._live_caps = QLabel("")
        h._live_caps.setWordWrap(True)
        actions.addWidget(h._live_caps, 1)
        h._live_dl_btn = QPushButton("下载辅助模型")
        h._live_dl_btn.setAccessibleName("下载会议辅助模型（进行中）")
        h._live_dl_btn.clicked.connect(h._download_aux_models)
        h._live_dl_btn.hide()
        actions.addWidget(h._live_dl_btn)
        lay.addLayout(actions)
        return page

    def show_start_card(self) -> None:
        from sayink.ui import meeting_page as mp

        h = self._host
        if not h._flush_bound_title(sync=True) or not h._save_notes_draft() or not h._save_record_notes():
            return
        h._audio_player.stop()
        if h._ctrl.is_active:
            self.enter_live()
            return
        defaults = MeetingOptions.from_config(h._config)
        h._opt_title.clear()
        h._opt_participants.clear()
        h._opt_agenda.clear()
        index = h._opt_source.findData(defaults.input_source)
        h._opt_source.setCurrentIndex(max(0, index))
        h._opt_model.blockSignals(True)
        h._opt_model.clear()
        h._opt_model.addItem(h._follow_model_label(), "")
        for model in MODEL_REGISTRY:
            if mp.is_model_downloaded(model["id"]):
                h._opt_model.addItem(model["name"], model["id"])
        index = h._opt_model.findData(defaults.model_id)
        h._opt_model.setCurrentIndex(max(0, index))
        h._opt_model.blockSignals(False)
        h._opt_model._sync_overflow_tooltip()
        index = h._opt_language.findData(defaults.language)
        h._opt_language.setCurrentIndex(max(0, index))
        h._update_card_notes()
        h._start_btn.setVisible(False)
        h._stack.setCurrentIndex(mp.PAGE_START)
        h._sync_media_buttons()
        h._opt_title.setFocus()
        h._start_scroll.verticalScrollBar().setValue(0)

    def follow_model_label(self) -> str:
        from sayink.speech_recognizer import get_model_info

        info = get_model_info(self._host._config.get("stt.model_id", ""))
        name = info["name"] if info else "未选择"
        return f"与转写相同（当前：{name}）"

    def _sync_card_loopback_hint(self, input_source: str) -> None:
        from sayink import audio_devices

        h = self._host
        if input_source not in ("mixed", "system"):
            h._opt_loopback_hint.clear()
            h._opt_loopback_hint.hide()
            return
        loopback_fn = getattr(audio_devices, "describe_active_loopback_device", None)
        dev_desc = str(loopback_fn() or "").strip() if callable(loopback_fn) else ""
        if dev_desc:
            prefix = "" if ("回环" in dev_desc or "输出" in dev_desc) else "当前电脑声回环输出："
            h._opt_loopback_hint.setText(f"{prefix}{dev_desc}")
            h._opt_loopback_hint.show()
        else:
            h._opt_loopback_hint.clear()
            h._opt_loopback_hint.hide()

    def update_card_notes(self) -> None:
        h = self._host
        options = self.options_from_card()
        self._sync_card_loopback_hint(options.input_source)
        dictation = str(h._config.get("stt.model_id", DEFAULT_MODEL_ID) or DEFAULT_MODEL_ID)
        caps = resolve_capabilities(h._config, options, dictation_model_id=dictation)
        notes = list(caps.notes)
        if h._store.disabled:
            notes.insert(0, "历史数据库不可用：会议文字稿无法保存，现在无法开始会议。请重启应用或更换历史位置。")
        if not minutes_settings_complete(h._config):
            notes.append("尚未配置纪要模型（设置 → AI 服务）；会议结束后仍可先保存文字稿。")
        h._card_start.setEnabled(not h._store.disabled)
        h._card_notes.setText("\n".join(f"· {n}" for n in notes))
        h._card_notes.setVisible(bool(notes))
        self.sync_aux_buttons()

    def sync_aux_buttons(self) -> None:
        from sayink.ui import meeting_page as mp

        h = self._host
        missing = mp.missing_aux_models()
        if h._aux_dl.running:
            return
        label = f"下载辅助模型（约 {aux_models_size_mb(missing)} MB）" if missing else ""
        what = describe_aux_models(missing)
        h._card_dl_btn.setToolTip(
            f"{what}：下载后在会议开始时自动加载；实际状态可在 {ADVANCED_RECOGNITION_PATH} 查看"
        )
        h._live_dl_btn.setToolTip(f"{what}：本场会议已按当前方式进行，下载后从下一场开始生效")
        for button in (h._card_dl_btn, h._live_dl_btn):
            button.setText(label)
            button.setEnabled(True)
            button.setVisible(bool(missing))

    def download_aux_models(self) -> None:
        h = self._host
        if h._aux_dl.running:
            return
        if not h._aux_dl.start():
            self.sync_aux_buttons()
            return
        for button in (h._card_dl_btn, h._live_dl_btn):
            button.setEnabled(False)
            button.setText("下载中 0%")

    def on_aux_progress(self, pct: int) -> None:
        h = self._host
        for button in (h._card_dl_btn, h._live_dl_btn):
            button.setText(f"下载中 {int(pct)}%")

    def on_aux_finished(self, ok: bool, message: str) -> None:
        from sayink.ui import meeting_page as mp

        h = self._host
        if ok or h._aux_dl.downloaded_any:
            h.aux_models_changed.emit()
        idx = h._stack.currentIndex()
        if ok and idx == mp.PAGE_START:
            self.update_card_notes()
            h._card_notes.setText("· 辅助模型已下载，将在会议开始时自动加载。\n" + h._card_notes.text())
            h._card_notes.show()
        elif ok and idx == mp.PAGE_LIVE:
            h._live_caps.setText(f"{h._live_caps.text()} · 辅助模型已下载，下一场会议生效")
        elif not ok and idx == mp.PAGE_START:
            h._card_notes.setText(f"· {message}；可稍后重试，或在 引擎 页下载。")
            h._card_notes.show()
        elif not ok and idx == mp.PAGE_LIVE:
            h._live_status.setText(f"{message}；可稍后重试，或在 引擎 页下载。")
            h._live_status.show()
        self.sync_aux_buttons()

    def options_from_card(self) -> MeetingOptions:
        h = self._host
        defaults = MeetingOptions.from_config(h._config)
        return MeetingOptions(
            title=h._opt_title.text().strip(),
            participants=h._opt_participants.text().strip(),
            agenda=h._opt_agenda.toPlainText().strip(),
            input_source=str(h._opt_source.currentData() or defaults.input_source),
            model_id=str(h._opt_model.currentData() or ""),
            glossary=defaults.glossary,
            vad=defaults.vad,
            language=str(h._opt_language.currentData() or "auto"),
            save_audio=bool(h._config.get("meeting.save_audio", True)),
        )

    def leave_start_card(self) -> None:
        from sayink.ui import meeting_page as mp

        h = self._host
        h._start_btn.setVisible(True)
        h._stack.setCurrentIndex(mp.PAGE_BROWSE)
        h._sync_media_buttons()

    def start_from_card(self) -> None:
        self._host.start_requested.emit(self.options_from_card())

    def live_input_source(self) -> str:
        h = self._host
        record = h._ctrl.current
        if record is not None and getattr(record, "source", ""):
            return str(record.source)
        return str(audio_source(h._config, meeting=True) or "mixed")

    def live_captures_mic_audio(self) -> bool:
        return self.live_input_source() in ("mixed", "microphone")

    def live_captures_system_audio(self) -> bool:
        return self.live_input_source() in ("mixed", "system")

    def sync_live_meter_lanes(self) -> None:
        h = self._host
        has_mic = self.live_captures_mic_audio()
        has_sys = self.live_captures_system_audio()
        h._live_mic_label.setText("麦克风" if has_mic else "麦克风（未启用）")
        h._live_sys_label.setText("电脑声（对方）" if has_sys else "电脑声（未启用）")
        h._live_mic_meter.set_active(has_mic)
        h._live_sys_meter.set_active(has_sys)

    def set_lane_levels(self, mic_level: float = 0.0, system_level: float = 0.0) -> None:
        h = self._host
        h._live_mic_meter.set_level(mic_level)
        h._live_sys_meter.set_level(system_level)
        try:
            sys_val = float(system_level)
        except (TypeError, ValueError):
            sys_val = 0.0
        if sys_val >= LIVE_REMOTE_SILENCE_THRESHOLD:
            h._live_remote_active_ts = time.monotonic()
            h._system_silent_dismissed = False
            if h._system_silent_banner.isVisible():
                h._system_silent_banner.hide()
        else:
            self.check_live_remote_silence()

    def show_system_silent_warning(self, message: str = "") -> None:
        h = self._host
        text = (message or "").strip() or DEFAULT_SYSTEM_SILENT_MSG
        h._system_silent_label.setText(text)
        h._system_silent_banner.show()

    def clear_system_silent_warning(self) -> None:
        h = self._host
        h._system_silent_dismissed = False
        h._live_remote_active_ts = time.monotonic()
        h._system_silent_banner.hide()

    def dismiss_system_silent_warning(self) -> None:
        h = self._host
        h._system_silent_dismissed = True
        h._system_silent_banner.hide()

    def check_live_remote_silence(self) -> None:
        from sayink.ui import meeting_page as mp

        h = self._host
        if (
            h._stack.currentIndex() != mp.PAGE_LIVE
            or h._ctrl.state is not MeetingState.LIVE
            or not self.live_captures_system_audio()
            or h._system_silent_dismissed
        ):
            return
        if (time.monotonic() - h._live_remote_active_ts) >= LIVE_REMOTE_SILENCE_SECONDS:
            if not h._system_silent_banner.isVisible():
                self.show_system_silent_warning()

    def set_live_scroll(self, value: int) -> None:
        h = self._host
        view = h._live_transcript
        bar = view.verticalScrollBar()
        h._live_scroll_guard += 1
        view._scroll_guard += 1
        try:
            bar.setValue(value)
        finally:
            view._scroll_guard -= 1
            h._live_scroll_guard -= 1

    def should_follow_live(self, position: int, anchor: int) -> bool:
        h = self._host
        if not h._live_follow_bottom or position != anchor:
            return False
        return not bool(QApplication.mouseButtons() & Qt.MouseButton.LeftButton)

    def stick_live_bottom(self) -> None:
        h = self._host
        view = h._live_transcript
        h._live_scroll_guard += 1
        view._scroll_guard += 1
        try:
            cursor = view.textCursor()
            if not cursor.hasSelection():
                cursor.movePosition(QTextCursor.MoveOperation.End)
                view.setTextCursor(cursor)
            view.verticalScrollBar().setValue(view.verticalScrollBar().maximum())
        finally:
            view._scroll_guard -= 1
            h._live_scroll_guard -= 1

    def on_live_range_changed(self, _minimum: int, maximum: int) -> None:
        h = self._host
        if not h._live_follow_bottom or (QApplication.mouseButtons() & Qt.MouseButton.LeftButton):
            return
        bar = h._live_transcript.verticalScrollBar()
        if bar.value() < maximum:
            self.set_live_scroll(maximum)

    def on_live_scroll_changed(self, value: int) -> None:
        h = self._host
        if h._live_scroll_guard:
            return
        bar = h._live_transcript.verticalScrollBar()
        if value >= max(0, bar.maximum() - 24):
            h._live_follow_bottom = True
            h._live_new_msg_btn.hide()
        else:
            h._live_follow_bottom = False

    def scroll_live_to_bottom(self) -> None:
        h = self._host
        h._live_follow_bottom = True
        h._live_new_msg_btn.hide()
        view = h._live_transcript
        if hasattr(view, "flush_layout"):
            view.flush_layout()
        self.stick_live_bottom()

    def position_live_new_msg_btn(self) -> None:
        h = self._host
        if not hasattr(h, "_live_new_msg_btn") or not hasattr(h, "_live_transcript"):
            return
        vp = h._live_transcript.viewport()
        h._live_new_msg_btn.adjustSize()
        bw = max(136, h._live_new_msg_btn.sizeHint().width() + 16)
        bh = max(28, h._live_new_msg_btn.sizeHint().height())
        h._live_new_msg_btn.resize(bw, bh)
        x = max(8, (vp.width() - bw) // 2)
        y = max(8, vp.height() - bh - 10)
        h._live_new_msg_btn.move(x, y)

    def _reset_live_session_ui(self) -> None:
        h = self._host
        h._live_transcript.clear()
        h._live_find_edit.clear()
        h._live_find_bar.hide()
        h._live_correcting_speakers = False
        h._live_follow_bottom = True
        h._live_speaker_menu.clear()
        h._live_speaker_feedback.clear()
        h._live_speaker_feedback.hide()
        h._live_feedback_timer.stop()
        h._live_new_msg_btn.hide()
        h._system_silent_dismissed = False
        h._system_silent_banner.hide()

    def enter_live(self) -> None:
        from sayink.ui import meeting_page as mp

        h = self._host
        record = h._ctrl.current
        meeting_id = record.id if record is not None else ""
        if h._drafts.enter_live_session(meeting_id):
            self._reset_live_session_ui()
        h._live_remote_active_ts = time.monotonic()
        self.sync_live_meter_lanes()
        h._live_mic_meter.set_level(0.0)
        h._live_sys_meter.set_level(0.0)
        h._live_title.setText(record.display_title if record is not None else "会议进行中")
        h._live_clock.setText(h._ctrl.elapsed_text())
        caps = h._ctrl.capabilities
        if caps is not None:
            summary, tip = _format_live_capabilities(caps)
            h._live_caps.setToolTip(tip)
            h._live_caps.setText(summary)
        self.sync_aux_buttons()
        h._user_notes.blockSignals(True)
        h._user_notes.setPlainText(record.notes_user if record is not None else "")
        h._user_notes.blockSignals(False)
        h._mark_note.clear()
        h._start_btn.setVisible(False)
        h._stack.setCurrentIndex(mp.PAGE_LIVE)
        h._sync_media_buttons()
        self.sync_live_state()
        h._sync_responsive_layout()
        self.render_live()

    def sync_live_state(self) -> None:
        from sayink.ui import settings_styles as ss

        h = self._host
        state = h._ctrl.state
        self.sync_live_meter_lanes()
        if state is MeetingState.PAUSED:
            h._live_silence_timer.stop()
            h._live_remote_active_ts = time.monotonic()
            h._live_status.setText(f"采集已暂停：{h._ctrl.paused_detail or '等待继续'}")
            h._live_status.show()
            h._resume_btn.show()
            h._pause_btn.hide()
            h._end_btn.setEnabled(True)
            h._mark_btn.setEnabled(False)
            h._mark_btn.setStyleSheet(ss.BTN_ACCENT_SM + h._disabled_btn_sm_override())
        elif state is MeetingState.DRAINING:
            h._live_silence_timer.stop()
            h._live_status.setText("正在整理文字稿，请稍候…")
            h._live_status.show()
            h._resume_btn.hide()
            h._pause_btn.hide()
            h._end_btn.setEnabled(False)
            h._mark_btn.setEnabled(False)
            h._mark_btn.setStyleSheet(ss.BTN_ACCENT_SM + h._disabled_btn_sm_override())
        else:
            h._live_remote_active_ts = time.monotonic()
            if self.live_captures_system_audio():
                h._live_silence_timer.start()
            else:
                h._live_silence_timer.stop()
            h._live_status.hide()
            h._resume_btn.hide()
            h._pause_btn.show()
            h._end_btn.setEnabled(True)
            h._mark_btn.setEnabled(True)
            h._mark_btn.setStyleSheet(ss.BTN_ACCENT_SM)
        self.paint_live_clock()

    def paint_live_clock(self) -> None:
        from sayink.ui import design_tokens as tok

        h = self._host
        paused = h._ctrl.state is MeetingState.PAUSED
        color = tok.TEXT_DIM if paused else tok.STATE_RECORD
        h._live_clock.setStyleSheet(
            f"font-size: {tok.TYPE_TITLE_LG}px; font-weight: 700; color: {color}; background: transparent;"
        )
        h._live_rec_dot.setStyleSheet(f"background: {color}; border-radius: 4px;")
        h._live_rec_dot.setAccessibleName("采集已暂停" if paused else "正在记录")

    def on_state_changed(self, state: MeetingState) -> None:
        from sayink.ui import meeting_page as mp

        h = self._host
        unsaved_live = False
        if state in (MeetingState.DRAINING, MeetingState.ENDED):
            h._live_silence_timer.stop()
            if not self.flush_pending_user_notes():
                unsaved_live = True
                self.keep_unsaved_live_notes()
        if state is MeetingState.LIVE and h._stack.currentIndex() != mp.PAGE_LIVE:
            self.enter_live()
        elif state in (MeetingState.PAUSED, MeetingState.DRAINING, MeetingState.LIVE):
            self.sync_live_state()
            self.render_live()
        elif state is MeetingState.ENDED:
            h._show_ended_meeting(h._ctrl.current_id, unsaved_live=unsaved_live)
        elif state is MeetingState.IDLE and h._stack.currentIndex() == mp.PAGE_LIVE:
            fallback_id = str(h._drafts.live_meeting_id or h._detail_id or "")
            h._show_ended_meeting(fallback_id, unsaved_live=unsaved_live)
            h._sync_media_buttons()

    def on_meeting_changed(self, meeting_id: str) -> None:
        h = self._host
        h._pending_refresh_id = meeting_id
        h._refresh_timer.start()

    def refresh_now(self) -> None:
        from sayink.ui import meeting_page as mp

        h = self._host
        if h._stack.currentIndex() == mp.PAGE_LIVE and h._ctrl.is_active:
            self.render_live()
            return
        if h._pending_refresh_id == h._detail_id or h._pending_refresh_id not in h._records:
            h.refresh()
        else:
            h._render_detail()

    def _append_live_html_delta(
        self,
        view: _TranscriptView,
        delta_html: str,
        *,
        follow: bool,
        position: int,
        anchor: int,
        scroll: int,
    ) -> None:
        tail = QTextCursor(view.document())
        tail.movePosition(QTextCursor.MoveOperation.End)
        tail.insertBlock()
        tail.insertHtml(delta_html)
        if follow:
            self.stick_live_bottom()
            return
        cursor = view.textCursor()
        last = view.document().characterCount() - 1
        adj_anchor = view.position_before_action(min(anchor, last))
        adj_pos = view.position_before_action(min(position, last))
        cursor.setPosition(adj_anchor)
        cursor.setPosition(adj_pos, QTextCursor.MoveMode.KeepAnchor)
        view.setTextCursor(cursor)
        view.verticalScrollBar().setValue(scroll)

    def _apply_live_transcript_html(
        self,
        rendered: str,
        *,
        follow: bool,
        position: int,
        anchor: int,
        scroll: int,
    ) -> None:
        h = self._host
        view = h._live_transcript
        bar = view.verticalScrollBar()
        previous = h._drafts.live_html
        if rendered == previous:
            return
        h._drafts.live_html = rendered
        if previous and rendered.startswith(previous):
            self._append_live_html_delta(
                view,
                rendered[len(previous) :],
                follow=follow,
                position=position,
                anchor=anchor,
                scroll=scroll,
            )
        elif previous and (
            view.retarget_speaker_labels(previous, rendered)
            or view.retarget_action_links(previous, rendered)
        ):
            if follow:
                self.stick_live_bottom()
        elif previous and view.retarget_paragraphs(previous, rendered, polish_line_height=False):
            if follow:
                self.stick_live_bottom()
            else:
                bar.setValue(scroll)
        else:
            view.setHtml(rendered)
            if follow:
                view.pin_scroll(follow_bottom=True)
                self.stick_live_bottom()
            else:
                view.pin_scroll(scroll)
        if h._live_find_bar.isVisible() and h._live_find_edit.text().strip():
            idx, total = find_in_reading_view(view, h._live_find_edit.text(), step=False)
            h._live_find_status.setText("未找到" if total == 0 else f"{idx} / {total}")

    def _render_live_marks(self, meeting_id: str) -> None:
        from sayink.ui import design_tokens as tok

        h = self._host
        marks = h._store.get_marks(meeting_id)
        h._sync_live_marks_height(has_marks=bool(marks))
        if marks:
            marks_html = "".join(
                f"<p style='margin:0 0 4px 0'>[{format_timestamp(m.offset_ms)}] {html.escape(m.note) or '标记'}</p>"
                for m in marks
            )
        else:
            marks_html = (
                f"<p style='margin:0 0 2px 0; color:{tok.TEXT_DIM}'>重要时刻按一下「标记此刻」。</p>"
                f"<p style='margin:0; color:{tok.TEXT_DIM}'>纪要会优先覆盖这些点。</p>"
            )
        if marks_html != h._drafts.live_marks_html:
            h._drafts.live_marks_html = marks_html
            h._marks_view.setHtml(marks_html)

    def render_live(self) -> None:
        h = self._host
        meeting_id = h._ctrl.current_id
        if not meeting_id:
            return
        record = h._ctrl.current
        started_at = record.started_at if record is not None else 0
        segments = h._store.get_segments(meeting_id)
        names = h._ctrl.speaker_names(meeting_id)
        prev_seg_count = h._drafts.live_segment_count
        h._live_transcript_sequences = [
            s.seq for s in segments if (s.corrected_text or s.polished_text or s.raw_text).strip()
        ]
        cur_seg_count = len(h._live_transcript_sequences)
        h._live_locked_sequences = {s.seq for s in segments if s.speaker_locked}
        editable = bool(h._live_transcript_sequences) and h._ctrl.state in (
            MeetingState.LIVE,
            MeetingState.PAUSED,
        )
        h._live_speaker_btn.setEnabled(editable)
        h._live_correct_speaker_btn.setEnabled(editable)
        h._live_correcting_speakers = h._live_correcting_speakers and editable
        h._live_correct_speaker_btn.blockSignals(True)
        h._live_correct_speaker_btn.setChecked(h._live_correcting_speakers)
        h._live_correct_speaker_btn.blockSignals(False)
        h._live_correct_speaker_btn.setText("完成纠正" if h._live_correcting_speakers else "纠正发言人")
        h._live_speaker_hint.setText(
            "点击目标段落旁的「更正」修改归属，人工更正会保留。"
            if h._live_correcting_speakers
            else "移到已出现的段落上可改文字；点击姓名可命名。还在识别的这一句要等落段后才能改。"
        )
        bar = h._live_transcript.verticalScrollBar()
        cursor = h._live_transcript.textCursor()
        position, anchor = cursor.position(), cursor.anchor()
        scroll = bar.value()
        follow = self.should_follow_live(position, anchor)
        rendered = h._transcript_html(transcript_lines(segments, started_at), names, links=True, live=True)
        h._live_scroll_guard += 1
        try:
            self._apply_live_transcript_html(
                rendered,
                follow=follow,
                position=position,
                anchor=anchor,
                scroll=scroll,
            )
        finally:
            h._live_scroll_guard -= 1
        h._drafts.live_segment_count = cur_seg_count
        if h._live_follow_bottom:
            h._live_new_msg_btn.hide()
        elif cur_seg_count > prev_seg_count:
            self.position_live_new_msg_btn()
            h._live_new_msg_btn.show()
            h._live_new_msg_btn.raise_()
        self._render_live_marks(meeting_id)

    def add_mark(self) -> None:
        h = self._host
        if h._ctrl.state is MeetingState.PAUSED:
            return
        offset = h._ctrl.add_mark(h._mark_note.text())
        if offset is not None:
            h._mark_note.clear()
            self.render_live()
            return
        h._live_feedback_timer.stop()
        h._live_speaker_feedback.setText("标记未能写入数据库，输入已保留。")
        h._live_speaker_feedback.show()
        h._live_feedback_timer.start(4000)

    def flush_pending_user_notes(self) -> bool:
        h = self._host
        if hasattr(h, "_user_notes_timer"):
            h._user_notes_timer.stop()
        meeting_id = getattr(h._ctrl, "current_id", None)
        if not meeting_id or not hasattr(h, "_user_notes"):
            return True
        text = h._user_notes.toPlainText()
        store = getattr(h, "_store", None)
        if store is None:
            self.save_user_notes()
            return True
        if h._drafts.flush_live_user_notes(store, meeting_id, text, h._ctrl.update_meeting):
            return True
        h._set_status("笔记尚未保存，输入已保留。")
        return False

    def keep_unsaved_live_notes(self) -> None:
        h = self._host
        meeting_id = getattr(h._ctrl, "current_id", None) or h._drafts.record_notes_meeting_id
        if not meeting_id or not hasattr(h, "_user_notes"):
            return
        text = h._user_notes.toPlainText()
        if (
            h._drafts.record_notes_meeting_id
            and h._drafts.record_notes_meeting_id != meeting_id
            and h._has_unsaved_record_notes()
            and not h._save_record_notes()
        ):
            h._drafts.stash_live_note_for_other_meeting(getattr(h, "_store", None), meeting_id, text)
            h._set_status("笔记尚未保存，输入已保留。")
            return
        h._drafts.adopt_live_note(meeting_id)
        h._record_notes.blockSignals(True)
        h._record_notes.setPlainText(text)
        h._record_notes.blockSignals(False)
        if hasattr(h, "_record_notes_timer"):
            h._record_notes_timer.start()
        h._set_status("笔记尚未保存，输入已保留。")

    def save_user_notes(self) -> None:
        h = self._host
        meeting_id = h._ctrl.current_id
        if not meeting_id:
            return
        text = h._user_notes.toPlainText()
        record = h._ctrl.current
        if record is not None and record.notes_user == text:
            return
        h._ctrl.update_meeting(meeting_id, notes_user=text)

    def confirm_end(self) -> None:
        from sayink.ui import meeting_page as mp

        h = self._host
        h._user_notes_timer.stop()
        self.save_user_notes()
        box = mp.QMessageBox(h)
        box.setWindowTitle("结束会议")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText("结束这场会议？")
        box.setInformativeText("会先把剩余语音识别完，然后可以生成纪要。")
        end = box.addButton("结束会议", QMessageBox.ButtonRole.AcceptRole)
        back = box.addButton("继续记录", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(end)
        box.setEscapeButton(back)
        _style_dialog_box(box, danger_btn=end)
        with _quiet_tooltips(h):
            box.exec()
        if box.clickedButton() == end:
            h.end_requested.emit()


__all__ = [
    "DEFAULT_SYSTEM_SILENT_MSG",
    "LIVE_REMOTE_SILENCE_SECONDS",
    "LIVE_REMOTE_SILENCE_THRESHOLD",
    "MeetingLiveController",
    "_LANGUAGE_LABELS",
    "_SOURCE_LABELS",
]

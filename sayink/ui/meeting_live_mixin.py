"""Start card, aux model download, and live meeting view mixin for MeetingPage."""

from __future__ import annotations

import html
import time

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
from sayink.ui.settings_components import ThemedCheckBox

_SOURCE_LABELS = tuple((key, input_source_label(key)) for key in ("mixed", "microphone", "system"))
_LANGUAGE_LABELS = (("auto", "自动（中文优先）"), ("zh", "中文"), ("en", "English"), ("ja", "日本語"))


class _MeetingLiveMixin:
    """Start-meeting card, helper-model downloads, and live session workspace."""

    LIVE_REMOTE_SILENCE_THRESHOLD = 0.008
    LIVE_REMOTE_SILENCE_SECONDS = 15.0
    _DEFAULT_SYSTEM_SILENT_MSG = (
        "电脑声（对方）已连续 15 秒无声音输入：线下会议可忽略。线上会议若正在使用蓝牙耳机"
        "（Hands-Free 通信设备）或外接声卡，请检查 Windows 默认播放设备与会议软件扬声器输出是否一致。"
    )

    def _build_start_card(self) -> QWidget:
        page = QWidget()
        page_lay = QVBoxLayout(page)
        page_lay.setContentsMargins(0, 0, 0, 0)
        page_lay.setSpacing(8)
        self._start_scroll = QScrollArea()
        self._start_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._start_scroll.setWidgetResizable(True)
        self._start_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        self._start_scroll.setWidget(content)
        page_lay.addWidget(self._start_scroll, 1)
        lay = QVBoxLayout(content)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        self._card_title = QLabel("开始会议")
        self._card_title.setObjectName("meetingStartCardTitle")
        lay.addWidget(self._card_title)
        self._card_hint = QLabel(
            "音源、模型和语言仅作用于本场会议；下方标题、参会人与议程均为选填，写进纪要并帮助模型理解上下文。"
        )
        self._card_hint.setWordWrap(True)
        lay.addWidget(self._card_hint)

        row = QHBoxLayout()
        row.setSpacing(10)
        self._opt_source = _CleanComboBox()
        self._opt_source.setAccessibleName("会议音频来源")
        for key, label in _SOURCE_LABELS:
            self._opt_source.addItem(label, key)
        self._opt_model = _CleanComboBox()
        self._opt_model.setAccessibleName("会议识别模型")
        self._opt_language = _CleanComboBox()
        self._opt_language.setAccessibleName("会议语言")
        for key, label in _LANGUAGE_LABELS:
            self._opt_language.addItem(label, key)
        self._card_field_labels: list[QLabel] = []
        for caption, field, stretch in (
            ("音频来源", self._opt_source, 3),
            ("转写模型", self._opt_model, 4),
            ("识别语言", self._opt_language, 3),
        ):
            field.setMinimumWidth(0)
            field.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            column = QVBoxLayout()
            column.setSpacing(4)
            label = QLabel(caption)
            label.setBuddy(field)
            self._card_field_labels.append(label)
            column.addWidget(label)
            column.addWidget(field)
            row.addLayout(column, stretch)
        lay.addLayout(row)

        self._opt_loopback_hint = QLabel("")
        self._opt_loopback_hint.setWordWrap(True)
        self._opt_loopback_hint.setAccessibleName("电脑声回环输出设备")
        self._opt_loopback_hint.hide()
        lay.addWidget(self._opt_loopback_hint)

        self._opt_save_audio = ThemedCheckBox(
            "本机保存会议录音（支持点击文字回放核对与断电恢复，纯离线存储）"
        )
        self._opt_save_audio.setAccessibleName("本机保存会议录音")
        self._opt_save_audio.setChecked(True)
        self._opt_save_audio.toggled.connect(self._on_save_audio_toggled)
        lay.addWidget(self._opt_save_audio)

        meta_row = QHBoxLayout()
        meta_row.setSpacing(10)
        self._opt_title = QLineEdit()
        self._opt_title.setPlaceholderText("会议标题（选填，留空按时间命名）")
        self._opt_title.setAccessibleName("会议标题")
        meta_row.addWidget(self._opt_title, 1)
        self._opt_participants = QLineEdit()
        self._opt_participants.setPlaceholderText("参会人（选填，逗号分隔）")
        self._opt_participants.setAccessibleName("参会人")
        meta_row.addWidget(self._opt_participants, 1)
        lay.addLayout(meta_row)

        self._opt_agenda = QPlainTextEdit()
        self._opt_agenda.setPlaceholderText("议程 / 背景（选填，每行一条）")
        self._opt_agenda.setAccessibleName("议程")
        self._opt_agenda.setFixedHeight(60)
        lay.addWidget(self._opt_agenda)

        self._card_notes = QLabel("")
        self._card_notes.setWordWrap(True)
        self._card_notes.hide()
        lay.addWidget(self._card_notes)
        dl_row = QHBoxLayout()
        self._card_dl_btn = QPushButton("下载辅助模型")
        self._card_dl_btn.setAccessibleName("下载会议辅助模型")
        self._card_dl_btn.clicked.connect(self._download_aux_models)
        self._card_dl_btn.hide()
        dl_row.addWidget(self._card_dl_btn, 0)
        dl_row.addStretch(1)
        lay.addLayout(dl_row)
        lay.addStretch(1)

        actions = QHBoxLayout()
        actions.addStretch(1)
        self._card_cancel = QPushButton("取消")
        self._card_cancel.clicked.connect(self._leave_start_card)
        actions.addWidget(self._card_cancel)
        self._card_start = QPushButton("开始")
        self._card_start.setAccessibleName("开始会议记录")
        self._card_start.setDefault(True)
        self._card_start.clicked.connect(self._start_from_card)
        actions.addWidget(self._card_start)
        page_lay.addLayout(actions)
        self._opt_source.currentIndexChanged.connect(self._update_card_notes)
        self._opt_model.currentIndexChanged.connect(self._update_card_notes)
        return page

    def _build_live(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        self._live_layout = lay
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        head = QHBoxLayout()
        head.setSpacing(12)
        self._live_title = QLabel("会议进行中")
        self._live_title.setTextFormat(Qt.TextFormat.PlainText)
        self._live_title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        head.addWidget(self._live_title, 1)
        self._live_rec_dot = QLabel()
        self._live_rec_dot.setFixedSize(8, 8)
        self._live_rec_dot.setAccessibleName("正在记录")
        head.addWidget(self._live_rec_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        self._live_clock = QLabel("00:00:00")
        self._live_clock.setObjectName("meetingLiveClock")
        self._live_clock.setAccessibleName("会议已进行时间")
        head.addWidget(self._live_clock, 0)
        self._pause_btn = QPushButton("暂停采集")
        self._pause_btn.setAccessibleName("暂停会议采集")
        self._pause_btn.setToolTip("暂停期间不采集新音频；已录内容继续识别，会议记录保留。")
        self._pause_btn.clicked.connect(self.pause_requested.emit)
        head.addWidget(self._pause_btn, 0)
        self._resume_btn = QPushButton("继续采集")
        self._resume_btn.setAccessibleName("继续会议采集")
        self._resume_btn.setToolTip("继续采集新的会议音频。")
        self._resume_btn.clicked.connect(self.resume_requested.emit)
        self._resume_btn.hide()
        head.addWidget(self._resume_btn, 0)
        self._end_btn = QPushButton("结束会议")
        self._end_btn.setAccessibleName("结束会议")
        self._end_btn.clicked.connect(self._confirm_end)
        head.addWidget(self._end_btn, 0)
        lay.addLayout(head)

        self._system_silent_banner = QFrame()
        self._system_silent_banner.setObjectName("meetingSilentBanner")
        banner_lay = QHBoxLayout(self._system_silent_banner)
        banner_lay.setContentsMargins(10, 6, 8, 6)
        banner_lay.setSpacing(8)
        self._system_silent_label = QLabel()
        self._system_silent_label.setTextFormat(Qt.TextFormat.PlainText)
        self._system_silent_label.setWordWrap(True)
        self._system_silent_label.setAccessibleName("电脑声静音提醒")
        banner_lay.addWidget(self._system_silent_label, 1)
        self._system_silent_dismiss_btn = QPushButton("知道了")
        self._system_silent_dismiss_btn.setAccessibleName("关闭电脑声静音提醒")
        self._system_silent_dismiss_btn.clicked.connect(self._dismiss_system_silent_warning)
        banner_lay.addWidget(self._system_silent_dismiss_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        self._system_silent_banner.hide()
        lay.addWidget(self._system_silent_banner)

        self._live_meters_bar = QWidget()
        self._live_meters_bar.setObjectName("meetingLiveMeters")
        meters_lay = QHBoxLayout(self._live_meters_bar)
        meters_lay.setContentsMargins(0, 0, 0, 0)
        meters_lay.setSpacing(8)
        self._live_mic_label = QLabel("麦克风")
        meters_lay.addWidget(self._live_mic_label, 0)
        self._live_mic_meter = _LevelMeterBar("麦克风实时电平")
        meters_lay.addWidget(self._live_mic_meter, 1)
        meters_lay.addSpacing(8)
        self._live_sys_label = QLabel("电脑声（对方）")
        meters_lay.addWidget(self._live_sys_label, 0)
        self._live_sys_meter = _LevelMeterBar("电脑声（对方）实时电平")
        meters_lay.addWidget(self._live_sys_meter, 1)
        lay.addWidget(self._live_meters_bar)

        self._live_status = QLabel("")
        self._live_status.setTextFormat(Qt.TextFormat.PlainText)
        self._live_status.setWordWrap(True)
        self._live_status.hide()
        lay.addWidget(self._live_status)

        speaker_tools = PairedToolBar(spacing=8)
        self._live_speaker_hint = QLabel("点击姓名可命名；纠正发言人只影响选中的段落，人工修改会保留。")
        self._live_speaker_hint.setWordWrap(True)
        self._live_speaker_btn = QPushButton("命名说话人")
        self._live_speaker_btn.setAccessibleName("会议记录中命名说话人")
        self._live_speaker_menu = QMenu(self._live_speaker_btn)
        self._live_speaker_menu.aboutToShow.connect(self._populate_live_speaker_menu)
        self._live_speaker_btn.setMenu(self._live_speaker_menu)
        speaker_tools.add_right(self._live_speaker_btn)
        self._live_correct_speaker_btn = QPushButton("纠正发言人")
        self._live_correct_speaker_btn.setCheckable(True)
        self._live_correct_speaker_btn.toggled.connect(self._toggle_live_speaker_correction)
        speaker_tools.add_right(self._live_correct_speaker_btn)
        lay.addWidget(speaker_tools)
        lay.addWidget(self._live_speaker_hint)
        self._live_speaker_feedback = QLabel()
        self._live_speaker_feedback.setTextFormat(Qt.TextFormat.PlainText)
        self._live_speaker_feedback.setWordWrap(True)
        self._live_speaker_feedback.hide()
        lay.addWidget(self._live_speaker_feedback)

        self._live_find_bar = QWidget()
        find_lay = QHBoxLayout(self._live_find_bar)
        find_lay.setContentsMargins(0, 0, 0, 0)
        find_lay.setSpacing(6)
        self._live_find_edit = QLineEdit()
        self._live_find_edit.setPlaceholderText("查找本场文字稿；Enter 下一处，Shift+Enter 上一处")
        self._live_find_edit.setAccessibleName("查找本场文字稿")
        self._live_find_edit.setClearButtonEnabled(True)
        self._live_find_edit.textChanged.connect(lambda: self._find_live_text(restart=True))
        self._live_find_edit.returnPressed.connect(self._find_live_text)
        self._live_find_edit.installEventFilter(self)
        find_lay.addWidget(self._live_find_edit, 1)
        self._live_find_status = QLabel()
        find_lay.addWidget(self._live_find_status)
        self._live_find_prev_btn = QPushButton("上一处")
        self._live_find_prev_btn.setAccessibleName("查找上一处")
        self._live_find_prev_btn.clicked.connect(lambda: self._find_live_text(backward=True))
        find_lay.addWidget(self._live_find_prev_btn)
        self._live_find_next_btn = QPushButton("下一处")
        self._live_find_next_btn.setAccessibleName("查找下一处")
        self._live_find_next_btn.clicked.connect(lambda: self._find_live_text(backward=False))
        find_lay.addWidget(self._live_find_next_btn)
        self._live_find_close_btn = QPushButton("关闭查找")
        self._live_find_close_btn.clicked.connect(self._close_live_find)
        find_lay.addWidget(self._live_find_close_btn)
        self._live_find_bar.hide()
        lay.addWidget(self._live_find_bar)

        self._live_split = QSplitter(Qt.Orientation.Horizontal)
        self._live_split.setChildrenCollapsible(False)
        self._live_transcript = _TranscriptView()
        self._live_transcript.setOpenLinks(False)
        self._live_transcript.anchorClicked.connect(self._on_live_transcript_link)
        self._live_transcript.setAccessibleName("实时文字稿")
        self._live_transcript.setPlaceholderText("开始说话后，这里会按说话人逐条出现文字…")
        self._live_transcript.viewport().installEventFilter(self)
        self._live_follow_bottom = True
        self._live_scroll_guard = 0
        self._live_transcript.verticalScrollBar().valueChanged.connect(self._on_live_scroll_changed)
        self._live_transcript.verticalScrollBar().rangeChanged.connect(self._on_live_range_changed)
        self._live_new_msg_btn = QPushButton("↓ 底部有新发言", self._live_transcript.viewport())
        self._live_new_msg_btn.setAccessibleName("跳到底部查看新发言")
        self._live_new_msg_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._live_new_msg_btn.clicked.connect(self._scroll_live_to_bottom)
        self._live_new_msg_btn.hide()
        self._live_split.addWidget(self._live_transcript)
        side = QWidget()
        side_lay = QVBoxLayout(side)
        self._live_side_layout = side_lay
        side_lay.setContentsMargins(12, 0, 0, 0)
        side_lay.setSpacing(8)
        self._marks_title = QLabel("标记")
        side_lay.addWidget(self._marks_title)
        mark_row = QHBoxLayout()
        mark_row.setSpacing(6)
        self._mark_note = QLineEdit()
        self._mark_note.setPlaceholderText("备注（可空）")
        self._mark_note.setAccessibleName("标记备注")
        self._mark_note.returnPressed.connect(self._add_mark)
        mark_row.addWidget(self._mark_note, 1)
        self._mark_btn = QPushButton("标记此刻")
        self._mark_btn.setAccessibleName("标记此刻")
        self._mark_btn.clicked.connect(self._add_mark)
        mark_row.addWidget(self._mark_btn)
        side_lay.addLayout(mark_row)
        self._marks_view = _CompactTextBrowser()
        self._marks_view.setAccessibleName("标记列表")
        self._marks_view.setPlaceholderText("")
        self._marks_view.document().setDocumentMargin(1)
        self._marks_view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._marks_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._marks_view.setMinimumHeight(36)
        self._marks_view.setMaximumHeight(88)
        side_lay.addWidget(self._marks_view)
        self._notes_title = QLabel("我的笔记")
        side_lay.addWidget(self._notes_title)
        self._user_notes = QPlainTextEdit()
        self._user_notes.setAccessibleName("我的笔记")
        self._user_notes.setPlaceholderText("随手记；生成纪要时会一并参考。")
        self._user_notes.setMinimumHeight(64)
        self._user_notes_timer = QTimer(self)
        self._user_notes_timer.setSingleShot(True)
        self._user_notes_timer.setInterval(800)
        self._user_notes_timer.timeout.connect(self._save_user_notes)
        self._user_notes.textChanged.connect(lambda: self._user_notes_timer.start())
        side_lay.addWidget(self._user_notes, 1)
        self._live_side_scroll = QScrollArea()
        self._live_side_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._live_side_scroll.setWidgetResizable(True)
        self._live_side_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._live_side_scroll.setMinimumWidth(240)
        self._live_side_scroll.setWidget(side)
        self._live_split.addWidget(self._live_side_scroll)
        self._live_split.setStretchFactor(0, 3)
        self._live_split.setStretchFactor(1, 2)
        lay.addWidget(self._live_split, 1)

        actions = QHBoxLayout()
        self._live_caps = QLabel("")
        self._live_caps.setWordWrap(True)
        actions.addWidget(self._live_caps, 1)
        self._live_dl_btn = QPushButton("下载辅助模型")
        self._live_dl_btn.setAccessibleName("下载会议辅助模型（进行中）")
        self._live_dl_btn.clicked.connect(self._download_aux_models)
        self._live_dl_btn.hide()
        actions.addWidget(self._live_dl_btn)
        lay.addLayout(actions)
        return page

    def _on_save_audio_toggled(self, checked: bool) -> None:
        self._config.set("meeting.save_audio", bool(checked))
        if hasattr(self._config, "save"):
            try:
                self._config.save()
            except Exception:
                pass
        self._update_card_notes()

    def show_start_card(self) -> None:
        from sayink.ui import meeting_page as mp

        if not self._flush_bound_title(sync=True) or not self._save_notes_draft() or not self._save_record_notes():
            return
        self._audio_player.stop()
        if self._ctrl.is_active:
            self._enter_live()
            return
        defaults = MeetingOptions.from_config(self._config)
        self._opt_title.clear()
        self._opt_participants.clear()
        self._opt_agenda.clear()
        self._opt_save_audio.blockSignals(True)
        self._opt_save_audio.setChecked(bool(self._config.get("meeting.save_audio", True)))
        self._opt_save_audio.blockSignals(False)
        index = self._opt_source.findData(defaults.input_source)
        self._opt_source.setCurrentIndex(max(0, index))
        self._opt_model.blockSignals(True)
        self._opt_model.clear()
        self._opt_model.addItem(self._follow_model_label(), "")
        for model in MODEL_REGISTRY:
            if mp.is_model_downloaded(model["id"]):
                self._opt_model.addItem(model["name"], model["id"])
        index = self._opt_model.findData(defaults.model_id)
        self._opt_model.setCurrentIndex(max(0, index))
        self._opt_model.blockSignals(False)
        self._opt_model._sync_overflow_tooltip()
        index = self._opt_language.findData(defaults.language)
        self._opt_language.setCurrentIndex(max(0, index))
        self._update_card_notes()
        self._start_btn.setVisible(False)
        self._stack.setCurrentIndex(mp.PAGE_START)
        self._sync_media_buttons()
        self._opt_title.setFocus()
        self._start_scroll.verticalScrollBar().setValue(0)

    def _follow_model_label(self) -> str:
        from sayink.speech_recognizer import get_model_info

        info = get_model_info(self._config.get("stt.model_id", ""))
        name = info["name"] if info else "未选择"
        return f"与转写相同（当前：{name}）"

    def _update_card_notes(self) -> None:
        from sayink import audio_devices

        options = self._options_from_card()
        if options.input_source in ("mixed", "system"):
            loopback_fn = getattr(audio_devices, "describe_active_loopback_device", None)
            dev_desc = str(loopback_fn() or "").strip() if callable(loopback_fn) else ""
            if dev_desc:
                prefix = "" if ("回环" in dev_desc or "输出" in dev_desc) else "当前电脑声回环输出："
                self._opt_loopback_hint.setText(f"{prefix}{dev_desc}")
                self._opt_loopback_hint.show()
            else:
                self._opt_loopback_hint.clear()
                self._opt_loopback_hint.hide()
        else:
            self._opt_loopback_hint.clear()
            self._opt_loopback_hint.hide()
        dictation = str(self._config.get("stt.model_id", DEFAULT_MODEL_ID) or DEFAULT_MODEL_ID)
        caps = resolve_capabilities(self._config, options, dictation_model_id=dictation)
        notes = list(caps.notes)
        if self._store.disabled:
            notes.insert(0, "历史数据库不可用：会议文字稿无法保存，现在无法开始会议。请重启应用或更换历史位置。")
        if not minutes_settings_complete(self._config):
            notes.append("尚未配置纪要模型（设置 → AI 服务）；会议结束后仍可先保存文字稿。")
        self._card_start.setEnabled(not self._store.disabled)
        self._card_notes.setText("\n".join(f"· {n}" for n in notes))
        self._card_notes.setVisible(bool(notes))
        self._sync_aux_buttons()

    def _sync_aux_buttons(self) -> None:
        """Show 「下载辅助模型」 wherever a helper model is still missing."""
        from sayink.ui import meeting_page as mp

        missing = mp.missing_aux_models()
        if self._aux_dl.running:
            return
        label = f"下载辅助模型（约 {aux_models_size_mb(missing)} MB）" if missing else ""
        what = describe_aux_models(missing)
        self._card_dl_btn.setToolTip(
            f"{what}：下载后在会议开始时自动加载；实际状态可在 {ADVANCED_RECOGNITION_PATH} 查看"
        )
        self._live_dl_btn.setToolTip(f"{what}：本场会议已按当前方式进行，下载后从下一场开始生效")
        for button in (self._card_dl_btn, self._live_dl_btn):
            button.setText(label)
            button.setEnabled(True)
            button.setVisible(bool(missing))

    def _download_aux_models(self) -> None:
        if self._aux_dl.running:
            return
        if not self._aux_dl.start():
            self._sync_aux_buttons()
            return
        for button in (self._card_dl_btn, self._live_dl_btn):
            button.setEnabled(False)
            button.setText("下载中 0%")

    def cancel_downloads(self) -> None:
        """Cancel any in-progress helper-model download on app exit."""
        self._aux_dl.cancel()

    @property
    def running_workers(self):
        return self._aux_dl.running_workers

    def _on_aux_progress(self, pct: int) -> None:
        for button in (self._card_dl_btn, self._live_dl_btn):
            button.setText(f"下载中 {int(pct)}%")

    def _on_aux_finished(self, ok: bool, message: str) -> None:
        from sayink.ui import meeting_page as mp

        if ok or self._aux_dl.downloaded_any:
            self.aux_models_changed.emit()
        if ok:
            if self._stack.currentIndex() == mp.PAGE_START:
                self._update_card_notes()
                self._card_notes.setText(
                    "· 辅助模型已下载，将在会议开始时自动加载。\n" + self._card_notes.text()
                )
                self._card_notes.show()
            elif self._stack.currentIndex() == mp.PAGE_LIVE:
                self._live_caps.setText(f"{self._live_caps.text()} · 辅助模型已下载，下一场会议生效")
        else:
            if self._stack.currentIndex() == mp.PAGE_START:
                self._card_notes.setText(f"· {message}；可稍后重试，或在 引擎 页下载。")
                self._card_notes.show()
            elif self._stack.currentIndex() == mp.PAGE_LIVE:
                self._live_status.setText(f"{message}；可稍后重试，或在 引擎 页下载。")
                self._live_status.show()
        self._sync_aux_buttons()

    def _options_from_card(self) -> MeetingOptions:
        defaults = MeetingOptions.from_config(self._config)
        return MeetingOptions(
            title=self._opt_title.text().strip(),
            participants=self._opt_participants.text().strip(),
            agenda=self._opt_agenda.toPlainText().strip(),
            input_source=str(self._opt_source.currentData() or defaults.input_source),
            model_id=str(self._opt_model.currentData() or ""),
            glossary=defaults.glossary,
            vad=defaults.vad,
            language=str(self._opt_language.currentData() or "auto"),
            save_audio=bool(self._opt_save_audio.isChecked()),
        )

    def _leave_start_card(self) -> None:
        from sayink.ui import meeting_page as mp

        self._start_btn.setVisible(True)
        self._stack.setCurrentIndex(mp.PAGE_BROWSE)
        self._sync_media_buttons()

    def _start_from_card(self) -> None:
        options = self._options_from_card()
        self.start_requested.emit(options)

    def _live_input_source(self) -> str:
        record = self._ctrl.current
        if record is not None and getattr(record, "source", ""):
            return str(record.source)
        return str(audio_source(self._config, meeting=True) or "mixed")

    def _live_captures_mic_audio(self) -> bool:
        return self._live_input_source() in ("mixed", "microphone")

    def _live_captures_system_audio(self) -> bool:
        return self._live_input_source() in ("mixed", "system")

    def _sync_live_meter_lanes(self) -> None:
        has_mic = self._live_captures_mic_audio()
        has_sys = self._live_captures_system_audio()
        self._live_mic_label.setText("麦克风" if has_mic else "麦克风（未启用）")
        self._live_sys_label.setText("电脑声（对方）" if has_sys else "电脑声（未启用）")
        self._live_mic_meter.set_active(has_mic)
        self._live_sys_meter.set_active(has_sys)

    def set_lane_levels(self, mic_level: float = 0.0, system_level: float = 0.0) -> None:
        """Feed dual-lane real-time levels [0..1] from the active meeting audio pipeline."""
        self._live_mic_meter.set_level(mic_level)
        self._live_sys_meter.set_level(system_level)
        try:
            sys_val = float(system_level)
        except (TypeError, ValueError):
            sys_val = 0.0
        if sys_val >= self.LIVE_REMOTE_SILENCE_THRESHOLD:
            self._live_remote_active_ts = time.monotonic()
            self._system_silent_dismissed = False
            if self._system_silent_banner.isVisible():
                self._system_silent_banner.hide()
        else:
            self._check_live_remote_silence()

    def show_system_silent_warning(self, message: str = "") -> None:
        """Show non-blocking yellow warning banner when remote computer audio stays silent."""
        text = (message or "").strip() or self._DEFAULT_SYSTEM_SILENT_MSG
        self._system_silent_label.setText(text)
        self._system_silent_banner.show()

    def clear_system_silent_warning(self) -> None:
        self._system_silent_dismissed = False
        self._live_remote_active_ts = time.monotonic()
        self._system_silent_banner.hide()

    def _dismiss_system_silent_warning(self) -> None:
        self._system_silent_dismissed = True
        self._system_silent_banner.hide()

    def _check_live_remote_silence(self) -> None:
        from sayink.ui import meeting_page as mp

        if (
            self._stack.currentIndex() != mp.PAGE_LIVE
            or self._ctrl.state is not MeetingState.LIVE
            or not self._live_captures_system_audio()
        ):
            return
        if self._system_silent_dismissed:
            return
        if (time.monotonic() - self._live_remote_active_ts) >= self.LIVE_REMOTE_SILENCE_SECONDS:
            if not self._system_silent_banner.isVisible():
                self.show_system_silent_warning()

    def _set_live_scroll(self, value: int) -> None:
        view = self._live_transcript
        bar = view.verticalScrollBar()
        self._live_scroll_guard += 1
        view._scroll_guard += 1
        try:
            bar.setValue(value)
        finally:
            view._scroll_guard -= 1
            self._live_scroll_guard -= 1

    def _should_follow_live(self, position: int, anchor: int) -> bool:
        if not self._live_follow_bottom or position != anchor:
            return False
        return not bool(QApplication.mouseButtons() & Qt.MouseButton.LeftButton)

    def _stick_live_bottom(self) -> None:
        view = self._live_transcript
        self._live_scroll_guard += 1
        view._scroll_guard += 1
        try:
            cursor = view.textCursor()
            if not cursor.hasSelection():
                cursor.movePosition(QTextCursor.MoveOperation.End)
                view.setTextCursor(cursor)
            view.verticalScrollBar().setValue(view.verticalScrollBar().maximum())
        finally:
            view._scroll_guard -= 1
            self._live_scroll_guard -= 1

    def _on_live_range_changed(self, _minimum: int, maximum: int) -> None:
        if not self._live_follow_bottom:
            return
        if QApplication.mouseButtons() & Qt.MouseButton.LeftButton:
            return
        bar = self._live_transcript.verticalScrollBar()
        if bar.value() >= maximum:
            return
        self._set_live_scroll(maximum)

    def _on_live_scroll_changed(self, value: int) -> None:
        if self._live_scroll_guard:
            return
        bar = self._live_transcript.verticalScrollBar()
        if value >= max(0, bar.maximum() - 24):
            self._live_follow_bottom = True
            self._live_new_msg_btn.hide()
        else:
            self._live_follow_bottom = False

    def _scroll_live_to_bottom(self) -> None:
        self._live_follow_bottom = True
        self._live_new_msg_btn.hide()
        view = self._live_transcript
        if hasattr(view, "flush_layout"):
            view.flush_layout()
        self._stick_live_bottom()

    def _position_live_new_msg_btn(self) -> None:
        if not hasattr(self, "_live_new_msg_btn") or not hasattr(self, "_live_transcript"):
            return
        vp = self._live_transcript.viewport()
        self._live_new_msg_btn.adjustSize()
        bw = max(136, self._live_new_msg_btn.sizeHint().width() + 16)
        bh = max(28, self._live_new_msg_btn.sizeHint().height())
        self._live_new_msg_btn.resize(bw, bh)
        x = max(8, (vp.width() - bw) // 2)
        y = max(8, vp.height() - bh - 10)
        self._live_new_msg_btn.move(x, y)

    def _enter_live(self) -> None:
        from sayink.ui import meeting_page as mp

        record = self._ctrl.current
        meeting_id = record.id if record is not None else ""
        if self._drafts.enter_live_session(meeting_id):
            self._live_transcript.clear()
            self._live_find_edit.clear()
            self._live_find_bar.hide()
            self._live_correcting_speakers = False
            self._live_follow_bottom = True
            self._live_speaker_menu.clear()
            self._live_speaker_feedback.clear()
            self._live_speaker_feedback.hide()
            self._live_feedback_timer.stop()
            self._live_new_msg_btn.hide()
            self._system_silent_dismissed = False
            self._system_silent_banner.hide()
        self._live_remote_active_ts = time.monotonic()
        self._sync_live_meter_lanes()
        self._live_mic_meter.set_level(0.0)
        self._live_sys_meter.set_level(0.0)
        self._live_title.setText(record.display_title if record is not None else "会议进行中")
        self._live_clock.setText(self._ctrl.elapsed_text())
        caps = self._ctrl.capabilities
        if caps is not None:
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
            self._live_caps.setToolTip(" · ".join(bits))
            self._live_caps.setText("说话人区分已增强" if caps.speaker_model_available else "说话人区分较粗略")
        self._sync_aux_buttons()
        self._user_notes.blockSignals(True)
        self._user_notes.setPlainText(record.notes_user if record is not None else "")
        self._user_notes.blockSignals(False)
        self._mark_note.clear()
        self._start_btn.setVisible(False)
        self._stack.setCurrentIndex(mp.PAGE_LIVE)
        self._sync_media_buttons()
        self._sync_live_state()
        self._sync_responsive_layout()
        self._render_live()

    def _sync_live_state(self) -> None:
        from sayink.ui import settings_styles as ss

        state = self._ctrl.state
        self._sync_live_meter_lanes()
        if state is MeetingState.PAUSED:
            self._live_silence_timer.stop()
            self._live_remote_active_ts = time.monotonic()
            self._live_status.setText(f"采集已暂停：{self._ctrl.paused_detail or '等待继续'}")
            self._live_status.show()
            self._resume_btn.show()
            self._pause_btn.hide()
            self._end_btn.setEnabled(True)
            self._mark_btn.setEnabled(False)
            self._mark_btn.setStyleSheet(ss.BTN_ACCENT_SM + self._disabled_btn_sm_override())
            self._paint_live_clock()
        elif state is MeetingState.DRAINING:
            self._live_silence_timer.stop()
            self._live_status.setText("正在整理文字稿，请稍候…")
            self._live_status.show()
            self._resume_btn.hide()
            self._pause_btn.hide()
            self._end_btn.setEnabled(False)
            self._mark_btn.setEnabled(False)
            self._mark_btn.setStyleSheet(ss.BTN_ACCENT_SM + self._disabled_btn_sm_override())
            self._paint_live_clock()
        else:
            self._live_remote_active_ts = time.monotonic()
            if self._live_captures_system_audio():
                self._live_silence_timer.start()
            else:
                self._live_silence_timer.stop()
            self._live_status.hide()
            self._resume_btn.hide()
            self._pause_btn.show()
            self._end_btn.setEnabled(True)
            self._mark_btn.setEnabled(True)
            self._mark_btn.setStyleSheet(ss.BTN_ACCENT_SM)
            self._paint_live_clock()

    def _paint_live_clock(self) -> None:
        from sayink.ui import design_tokens as tok

        paused = self._ctrl.state is MeetingState.PAUSED
        color = tok.TEXT_DIM if paused else tok.STATE_RECORD
        self._live_clock.setStyleSheet(
            f"font-size: {tok.TYPE_TITLE_LG}px; font-weight: 700; color: {color}; background: transparent;"
        )
        self._live_rec_dot.setStyleSheet(
            f"background: {color}; border-radius: 4px;"
        )
        self._live_rec_dot.setAccessibleName("采集已暂停" if paused else "正在记录")

    def _on_state_changed(self, state: MeetingState) -> None:
        from sayink.ui import meeting_page as mp

        unsaved_live = False
        if state in (MeetingState.DRAINING, MeetingState.ENDED):
            self._live_silence_timer.stop()
            if not self.flush_pending_user_notes():
                unsaved_live = True
                self._keep_unsaved_live_notes()
        if state is MeetingState.LIVE and self._stack.currentIndex() != mp.PAGE_LIVE:
            self._enter_live()
        elif state in (MeetingState.PAUSED, MeetingState.DRAINING, MeetingState.LIVE):
            self._sync_live_state()
            self._render_live()
        elif state is MeetingState.ENDED:
            self._show_ended_meeting(self._ctrl.current_id, unsaved_live=unsaved_live)

    def _on_meeting_changed(self, meeting_id: str) -> None:
        self._pending_refresh_id = meeting_id
        self._refresh_timer.start()

    def _refresh_now(self) -> None:
        from sayink.ui import meeting_page as mp

        if self._stack.currentIndex() == mp.PAGE_LIVE and self._ctrl.is_active:
            self._render_live()
            return
        if self._pending_refresh_id == self._detail_id or self._pending_refresh_id not in self._records:
            self.refresh()
        else:
            self._render_detail()

    def _render_live(self) -> None:
        from sayink.ui import design_tokens as tok

        meeting_id = self._ctrl.current_id
        if not meeting_id:
            return
        record = self._ctrl.current
        started_at = record.started_at if record is not None else 0
        segments = self._store.get_segments(meeting_id)
        names = self._ctrl.speaker_names(meeting_id)
        prev_seg_count = self._drafts.live_segment_count
        self._live_transcript_sequences = [s.seq for s in segments if (s.corrected_text or s.polished_text or s.raw_text).strip()]
        cur_seg_count = len(self._live_transcript_sequences)
        self._live_locked_sequences = {s.seq for s in segments if s.speaker_locked}
        editable = bool(self._live_transcript_sequences) and self._ctrl.state in (MeetingState.LIVE, MeetingState.PAUSED)
        self._live_speaker_btn.setEnabled(editable)
        self._live_correct_speaker_btn.setEnabled(editable)
        self._live_correcting_speakers = self._live_correcting_speakers and editable
        self._live_correct_speaker_btn.blockSignals(True)
        self._live_correct_speaker_btn.setChecked(self._live_correcting_speakers)
        self._live_correct_speaker_btn.blockSignals(False)
        self._live_correct_speaker_btn.setText("完成纠正" if self._live_correcting_speakers else "纠正发言人")
        self._live_speaker_hint.setText(
            "点击目标段落旁的「更正」修改归属，人工更正会保留。" if self._live_correcting_speakers else
            "移到已出现的段落上可改文字；点击姓名可命名。还在识别的这一句要等落段后才能改。"
        )
        bar = self._live_transcript.verticalScrollBar()
        cursor = self._live_transcript.textCursor()
        position, anchor = cursor.position(), cursor.anchor()
        scroll = bar.value()
        follow = self._should_follow_live(position, anchor)
        rendered = self._transcript_html(transcript_lines(segments, started_at), names, links=True, live=True)
        self._live_scroll_guard += 1
        try:
            if rendered != self._drafts.live_html:
                previous = self._drafts.live_html
                if previous and rendered.startswith(previous):
                    self._drafts.live_html = rendered
                    tail = QTextCursor(self._live_transcript.document())
                    tail.movePosition(QTextCursor.MoveOperation.End)
                    tail.insertBlock()
                    tail.insertHtml(rendered[len(previous):])
                    if follow:
                        self._stick_live_bottom()
                    else:
                        cursor = self._live_transcript.textCursor()
                        last = self._live_transcript.document().characterCount() - 1
                        anchor = self._live_transcript.position_before_action(min(anchor, last))
                        position = self._live_transcript.position_before_action(min(position, last))
                        cursor.setPosition(anchor)
                        cursor.setPosition(position, QTextCursor.MoveMode.KeepAnchor)
                        self._live_transcript.setTextCursor(cursor)
                        bar.setValue(scroll)
                elif previous and self._live_transcript.retarget_speaker_labels(previous, rendered):
                    self._drafts.live_html = rendered
                    if follow:
                        self._stick_live_bottom()
                elif previous and self._live_transcript.retarget_action_links(previous, rendered):
                    self._drafts.live_html = rendered
                    if follow:
                        self._stick_live_bottom()
                elif previous and self._live_transcript.retarget_paragraphs(previous, rendered, polish_line_height=False):
                    self._drafts.live_html = rendered
                    if follow:
                        self._stick_live_bottom()
                    else:
                        bar.setValue(scroll)
                else:
                    self._drafts.live_html = rendered
                    self._live_transcript.setHtml(rendered)
                    if follow:
                        self._live_transcript.pin_scroll(follow_bottom=True)
                        self._stick_live_bottom()
                    else:
                        self._live_transcript.pin_scroll(scroll)
                if self._live_find_bar.isVisible() and self._live_find_edit.text().strip():
                    idx, total = find_in_reading_view(
                        self._live_transcript, self._live_find_edit.text(), step=False
                    )
                    self._live_find_status.setText("未找到" if total == 0 else f"{idx} / {total}")
        finally:
            self._live_scroll_guard -= 1
        self._drafts.live_segment_count = cur_seg_count
        if self._live_follow_bottom:
            self._live_new_msg_btn.hide()
        elif cur_seg_count > prev_seg_count:
            self._position_live_new_msg_btn()
            self._live_new_msg_btn.show()
            self._live_new_msg_btn.raise_()
        marks = self._store.get_marks(meeting_id)
        self._sync_live_marks_height(has_marks=bool(marks))
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
        if marks_html != self._drafts.live_marks_html:
            self._drafts.live_marks_html = marks_html
            self._marks_view.setHtml(marks_html)

    def _add_mark(self) -> None:
        if self._ctrl.state is MeetingState.PAUSED:
            return
        offset = self._ctrl.add_mark(self._mark_note.text())
        if offset is not None:
            self._mark_note.clear()
            self._render_live()

    def flush_pending_user_notes(self) -> bool:
        """Write the live note and read it back. False leaves the typed text."""
        if hasattr(self, "_user_notes_timer"):
            self._user_notes_timer.stop()
        meeting_id = getattr(self._ctrl, "current_id", None)
        if not meeting_id or not hasattr(self, "_user_notes"):
            return True
        text = self._user_notes.toPlainText()
        store = getattr(self, "_store", None)
        if store is None:
            self._save_user_notes()
            return True
        if self._drafts.flush_live_user_notes(store, meeting_id, text, self._ctrl.update_meeting):
            return True
        self._set_status("笔记尚未保存，输入已保留。")
        return False

    def _keep_unsaved_live_notes(self) -> None:
        """Hold the live note in the post-meeting editor when the write failed."""
        meeting_id = getattr(self._ctrl, "current_id", None) or self._drafts.record_notes_meeting_id
        if not meeting_id or not hasattr(self, "_user_notes"):
            return
        text = self._user_notes.toPlainText()
        if (
            self._drafts.record_notes_meeting_id
            and self._drafts.record_notes_meeting_id != meeting_id
            and self._has_unsaved_record_notes()
            and not self._save_record_notes()
        ):
            self._drafts.stash_live_note_for_other_meeting(
                getattr(self, "_store", None), meeting_id, text
            )
            self._set_status("笔记尚未保存，输入已保留。")
            return
        self._drafts.adopt_live_note(meeting_id)
        self._record_notes.blockSignals(True)
        self._record_notes.setPlainText(text)
        self._record_notes.blockSignals(False)
        if hasattr(self, "_record_notes_timer"):
            self._record_notes_timer.start()
        self._set_status("笔记尚未保存，输入已保留。")

    def _save_user_notes(self) -> None:
        meeting_id = self._ctrl.current_id
        if not meeting_id:
            return
        text = self._user_notes.toPlainText()
        record = self._ctrl.current
        if record is not None and record.notes_user == text:
            return
        self._ctrl.update_meeting(meeting_id, notes_user=text)

    def _confirm_end(self) -> None:
        from sayink.ui import meeting_page as mp

        self._user_notes_timer.stop()
        self._save_user_notes()
        box = mp.QMessageBox(self)
        box.setWindowTitle("结束会议")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText("结束这场会议？")
        box.setInformativeText("会先把剩余语音识别完，然后可以生成纪要。")
        end = box.addButton("结束会议", QMessageBox.ButtonRole.AcceptRole)
        back = box.addButton("继续记录", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(end)
        box.setEscapeButton(back)
        _style_dialog_box(box, danger_btn=end)
        with _quiet_tooltips(self):
            box.exec()
        if box.clickedButton() == end:
            self.end_requested.emit()


__all__ = ["_LANGUAGE_LABELS", "_MeetingLiveMixin", "_SOURCE_LABELS"]

"""The 「会议」 page: meeting list + detail, start card, and the live view.

Three states (docs/meeting-notes-design.md §4.3):

* **browse** — list of past meetings on the left, the selected meeting's
  minutes / transcript on the right;
* **start card** — title / participants / agenda / source / model before a
  meeting starts (skipped when ``meeting.show_start_card`` is off);
* **live** — the running transcript, a mark button and a notes box while a
  meeting is recording.

The page never touches audio or the recognizer: it asks the
:class:`~sayink.app.App` through ``start_requested`` / ``end_requested`` /
``resume_requested`` and reads everything else from the
:class:`~sayink.meeting_session.MeetingController`.
"""

from __future__ import annotations

import bisect
import copy
import html
import json
import logging
from pathlib import Path
import tempfile as tempfile
import time
import weakref

from PyQt6.QtCore import QEvent, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QKeySequence, QShortcut, QTextCursor
from PyQt6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QComboBox, QDialog, QFileDialog as QFileDialog,
    QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu,
    QMessageBox as QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
    QSizePolicy, QSplitter, QStackedWidget, QVBoxLayout, QWidget,
)

from sayink.atomic_io import atomic_write_text as atomic_write_text
from sayink.aux_models import (
    AuxModelDownloads,
    missing_aux_models as missing_aux_models,
)
from sayink.meeting_session import (
    MeetingController,
    MeetingState,
    minutes_settings_complete,
    safe_filename as safe_filename,
)
from sayink.meeting_store import (
    SORT_RECENT,
    SORT_STARTED_AT,
    STATUS_ENDED,
    STATUS_INTERRUPTED,
    STATUS_LIVE,
    MeetingRecord,
    QueuedGeneration,
    now_ms,
)
from sayink.minutes_prompts import describe_template, format_timestamp, get_template, list_templates, transcript_lines
from sayink.media_import import MEDIA_SOURCE
from sayink.transcript_import import IMPORT_SOURCE
from sayink.ui.history_export import _safe_dt, export_save_path as export_save_path
from sayink.speech_recognizer import is_model_downloaded as is_model_downloaded
from sayink.ui import design_tokens as tok
from sayink.ui.clipboard import COPY_FAILED_TEXT, copy_text as copy_text
from sayink.ui.flow_layout import PairedToolBar, WrappingToolBar, defer_while_live_resize
from sayink.ui.history_window import (
    _ElidedLabel, _SessionCheckBox, _SessionList, release_list_item_widgets,
)
from sayink.ui.meeting_batch import confirm_batch_delete as confirm_batch_delete, fit_action_button
from sayink.ui.meeting_dialogs import (
    _MeetingTimeDialog as _MeetingTimeDialog,
    _SegmentSpeakerDialog as _SegmentSpeakerDialog,
    _SegmentTextDialog as _SegmentTextDialog,
    _quiet_tooltips,
    _style_dialog_box,
    build_meeting_datetime_edit_css as build_meeting_datetime_edit_css,
)
from sayink.ui.meeting_drafts import MeetingDraftBuffer
from sayink.ui.meeting_import_controller import MeetingImportController
from sayink.ui.meeting_live_controller import (
    DEFAULT_SYSTEM_SILENT_MSG,
    LIVE_REMOTE_SILENCE_SECONDS,
    LIVE_REMOTE_SILENCE_THRESHOLD,
    MeetingLiveController,
)
from sayink.ui.session_stream_view import build_stream_row_wash_css
from sayink.ui.meeting_player import (
    _AudioPlayerBar as _AudioPlayerBar,
    _CleanComboBox as _CleanComboBox,
    _LevelMeterBar as _LevelMeterBar,
    _PRUNED_PLAYBACK_DIRS as _PRUNED_PLAYBACK_DIRS,
    _STALE_PLAYBACK_WAV_PRUNED as _STALE_PLAYBACK_WAV_PRUNED,
    _format_audio_clock as _format_audio_clock,
    _probe_wav_duration_ms as _probe_wav_duration_ms,
    _prune_stale_playback_wavs as _prune_stale_playback_wavs,
    resolve_meeting_audio_path,
)
from sayink.ui.meeting_views import (
    _CompactTextBrowser,
    _STATUS_TEXT,
    _CurrentPageStack as _CurrentPageStack,
    _duration_label,
    _export_kind as _export_kind,
    _format_dt,
    _meeting_row_meta,
    _meeting_row_text,
    _position_label,
    _ReadingView as _ReadingView,
    _speaker_source,
    _TranscriptView as _TranscriptView,
    _with_export_suffix as _with_export_suffix,
    build_transcript_html,
    clear_reading_search,
    find_in_reading_view,
    link_citations as link_citations,
    notes_for_display as notes_for_display,
    speaker_color as speaker_color,
)
from sayink.ui.native_dialog import native_file_dialog as native_file_dialog
from sayink.ui.prompts import alert_error as alert_error

log = logging.getLogger("SayInk")

_ORIG_GET_OPEN_FILE_NAMES = QFileDialog.getOpenFileNames

PAGE_BROWSE = 0
PAGE_START = 1
PAGE_LIVE = 2

_EMPTY_MEETINGS = "还没有会议记录。\n点击右上角「导入文字稿」「导入音视频」或「开始会议」，也可以从托盘菜单开始。"


class _MeetingStreamRow(QWidget):
    """Row chrome matches 历史: the wash is painted here, clicks fall through."""

    def mousePressEvent(self, event):
        event.ignore()


def _paint_meeting_row(row: QWidget, *, selected: bool) -> None:
    css = build_stream_row_wash_css(
        "meetingStreamRow",
        selected=selected,
        title_object_name="meetingRowTitle",
        meta_object_name="meetingRowMeta",
    )
    if row.styleSheet() != css:
        row.setStyleSheet(css)


class MeetingPage(QWidget):
    LIVE_REMOTE_SILENCE_THRESHOLD = LIVE_REMOTE_SILENCE_THRESHOLD
    LIVE_REMOTE_SILENCE_SECONDS = LIVE_REMOTE_SILENCE_SECONDS
    _DEFAULT_SYSTEM_SILENT_MSG = DEFAULT_SYSTEM_SILENT_MSG

    start_requested = pyqtSignal(object)   # MeetingOptions
    end_requested = pyqtSignal()
    pause_requested = pyqtSignal()
    resume_requested = pyqtSignal()
    configure_requested = pyqtSignal()     # minutes need the AI service connection
    audio_settings_requested = pyqtSignal()  # open audio device troubleshooting in Settings
    view_mode_changed = pyqtSignal(str)    # "browse" | "start" | "live"
    aux_models_changed = pyqtSignal()      # a helper model was downloaded here
    glossary_updated = pyqtSignal()        # user accepted a learned glossary correction
    _title_write_settled = pyqtSignal(str, str, int)
    _record_notes_write_settled = pyqtSignal(str, str, int)
    _history_write_failed = pyqtSignal()

    def __init__(self, config, controller: MeetingController, parent=None) -> None:
        super().__init__(parent)
        self._config = config
        self._ctrl = controller
        self._store = controller.store
        self._live_ui = MeetingLiveController(self)
        self._import_ui = MeetingImportController(self)
        self._compact_single_pane = False
        self._compact_showing_detail = False
        self._host_viewport_width: int | None = None
        self._aux_dl = AuxModelDownloads(str(config.get("stt.download_source", "auto") or "auto"), self)
        self._aux_dl.progress.connect(self._on_aux_progress)
        self._aux_dl.finished.connect(self._on_aux_finished)
        self._drafts = MeetingDraftBuffer()
        self._records: dict[str, MeetingRecord] = {}
        self._detail_id = ""
        self._search_query = ""
        self._meeting_audio_paths: dict[str, str] = {}
        self._recovery_audio_cache: dict[str, tuple[int | None, str]] = {}
        self._templates_snapshot: object = None
        self._pending_media_import_path = ""
        self._failed_media_path = ""
        self._import_owns_detail = False
        self._media_cancel_pending = False
        self._media_import_started = 0.0
        self._status_tone = "info"
        self._live_remote_active_ts = time.monotonic()
        self._system_silent_dismissed = False
        self._transcript_offsets: list[int] = []
        self._transcript_time_known: list[bool] = []
        self._transcript_has_speakers = True
        self._transcript_has_times = True
        self._transcript_highlight = -1
        self._transcript_sequences: list[int] = []
        self._corrected_sequences: set[int] = set()
        self._speaker_locked_sequences: set[int] = set()
        self._transcript_editable = False
        self._citation_return: tuple[str, int] | None = None
        self._correcting_speakers = False
        self._pending_glossary_suggestion: tuple[str, str] | None = None
        self._live_correcting_speakers = False
        self._live_transcript_sequences: list[int] = []
        self._live_locked_sequences: set[int] = set()
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(400)
        self._refresh_timer.timeout.connect(self._refresh_now)
        self._pending_refresh_id = ""
        self._listed_generating = ""
        self._list_limit = 100
        self._batch_mode = False
        self._setup_ui()
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(self._hide_status_banner)
        self._live_feedback_timer = QTimer(self)
        self._live_feedback_timer.setSingleShot(True)
        self._live_feedback_timer.timeout.connect(self._live_speaker_feedback.hide)
        self._live_silence_timer = QTimer(self)
        self._live_silence_timer.setInterval(1000)
        self._live_silence_timer.timeout.connect(self._check_live_remote_silence)
        self._title_write_settled.connect(self._on_title_write_settled)
        self._record_notes_write_settled.connect(self._on_record_notes_write_settled)
        self._history_write_failed.connect(self._on_history_write_failed)
        history = getattr(self._store, "_history", None)
        if history is not None and hasattr(history, "add_failed_callback"):
            weak_self = weakref.ref(self)
            history.add_failed_callback(
                lambda _msg: (page := weak_self()) is not None and page._history_write_failed.emit()
            )
        self._ctrl.state_changed.connect(self._on_state_changed)
        self._ctrl.meeting_changed.connect(self._on_meeting_changed)
        self._ctrl.clock_tick.connect(self._live_clock.setText)
        self._ctrl.generation_progress.connect(self._on_generation_progress)
        self._ctrl.generation_preview.connect(self._on_generation_preview)
        self._ctrl.generation_finished.connect(self._on_generation_finished)
        self._ctrl.generation_failed.connect(self._on_generation_failed)
        self._ctrl.generation_cancelled.connect(self._on_generation_cancelled)
        self._ctrl.generation_queued.connect(self._on_generation_queued)
        started = getattr(self._ctrl, "media_import_started", None)
        if hasattr(started, "connect"):
            started.connect(self._on_media_started)
        self._ctrl.media_import_progress.connect(self._on_media_progress)
        preview = getattr(self._ctrl, "media_import_preview", None)
        if hasattr(preview, "connect"):
            preview.connect(self._on_media_preview)
        self._ctrl.media_import_finished.connect(self._on_media_finished)
        self._ctrl.media_import_failed.connect(self._on_media_failed)
        fin_failed = getattr(self._ctrl, "finalize_save_failed", None)
        if hasattr(fin_failed, "connect"):
            fin_failed.connect(self._on_finalize_save_failed)
        self._install_import_drop_targets()
        self.reapply_theme()
        self.refresh()
        if self._ctrl.is_active:
            self._enter_live()
        player_temp_wavs = self._audio_player._session_temp_wavs

        def _cleanup_page_temp_wavs(*_args, _paths=player_temp_wavs) -> None:
            for wav_path in list(_paths):
                try:
                    wav_path.unlink(missing_ok=True)
                except OSError:
                    pass
            _paths.clear()

        self.destroyed.connect(_cleanup_page_temp_wavs)
        weakref.finalize(self, _cleanup_page_temp_wavs)

    def cleanup(self) -> None:
        if hasattr(self, "_audio_player"):
            self._audio_player.cleanup_temp_files()

    def closeEvent(self, event) -> None:
        self.cleanup()
        super().closeEvent(event)

    # ── draft state compatibility delegates ───────────────────────

    @property
    def _editing_notes(self) -> bool:
        return self._drafts.editing_notes

    @_editing_notes.setter
    def _editing_notes(self, value: bool) -> None:
        self._drafts.editing_notes = bool(value)

    @property
    def _pending_title(self) -> tuple[str, str] | None:
        return self._drafts.pending_title

    @_pending_title.setter
    def _pending_title(self, value: tuple[str, str] | None) -> None:
        self._drafts.pending_title = value

    @property
    def _record_notes_meeting_id(self) -> str:
        return self._drafts.record_notes_meeting_id

    @_record_notes_meeting_id.setter
    def _record_notes_meeting_id(self, value: str) -> None:
        self._drafts.record_notes_meeting_id = value

    @property
    def _unsaved_live_notes(self) -> dict[str, str]:
        return self._drafts.unsaved_live_notes

    # ── construction ──────────────────────────────────────────────

    def _setup_ui(self) -> None:
        outer = QVBoxLayout(self)
        self._outer_layout = outer
        outer.setContentsMargins(*tok.WORKSPACE_MARGINS)
        outer.setSpacing(14)

        top = QHBoxLayout()
        top.setSpacing(12)
        from sayink.ui.settings_components import page_title_label

        self._title_label = page_title_label("会议")
        top.addWidget(self._title_label, 1, Qt.AlignmentFlag.AlignVCenter)
        self._import_btn = QPushButton("导入文字稿")
        self._import_btn.setAccessibleName("导入文字稿")
        self._import_btn.setToolTip("粘贴或打开已有文字稿，导入为一场会议")
        self._import_tip = self._import_btn.toolTip()
        self._import_btn.clicked.connect(self._import_transcript)
        from sayink.ui import settings_styles as ss
        self._import_btn.setStyleSheet(ss.BTN_GHOST_SM)
        top.addWidget(self._import_btn, 0)
        self._media_btn = QPushButton("导入音视频")
        self._media_btn.setAccessibleName("导入音视频")
        self._media_btn.setToolTip("打开或拖入本机音视频，识别成一场会议。不复制原文件；移走原文件后将无法播放。")
        self._media_tip = self._media_btn.toolTip()
        self._media_btn.clicked.connect(self._import_media)
        self._media_btn.setStyleSheet(ss.BTN_GHOST_SM)
        top.addWidget(self._media_btn, 0)
        self._media_cancel_btn = QPushButton("取消导入")
        self._media_cancel_btn.setAccessibleName("取消导入音视频")
        self._media_cancel_btn.setToolTip("停在当前这段之后，已经识别的文字会留下")
        self._media_cancel_btn.clicked.connect(self._cancel_media_import)
        self._media_cancel_btn.setStyleSheet(ss.BTN_DANGER_SM)
        self._media_cancel_btn.hide()
        self._media_retry_btn = QPushButton("重试保存")
        self._media_retry_btn.setAccessibleName("重试保存导入结果")
        self._media_retry_btn.setToolTip("上次识别的文字还在，再写入一次数据库")
        self._media_retry_btn.clicked.connect(self._retry_media_save)
        self._media_retry_btn.setStyleSheet(ss.BTN_GHOST_SM)
        self._media_retry_btn.hide()
        top.addWidget(self._media_retry_btn, 0)
        self._media_discard_btn = QPushButton("放弃未保存结果")
        self._media_discard_btn.setAccessibleName("放弃未保存的导入结果")
        self._media_discard_btn.setToolTip("放弃当前尚未写入数据库的导入识别结果")
        self._media_discard_btn.clicked.connect(self._discard_unsaved_media)
        self._media_discard_btn.setStyleSheet(ss.BTN_DANGER_SM)
        self._media_discard_btn.hide()
        top.addWidget(self._media_discard_btn, 0)
        self._finalize_retry_btn = QPushButton("重试保存结束状态")
        self._finalize_retry_btn.setAccessibleName("重试保存会议结束状态")
        self._finalize_retry_btn.setToolTip("刚才结束的会议尚未保存终态，点击重试写入数据库")
        self._finalize_retry_btn.clicked.connect(self._retry_finalize_save)
        self._finalize_retry_btn.setStyleSheet(ss.BTN_GHOST_SM)
        self._finalize_retry_btn.hide()
        top.addWidget(self._finalize_retry_btn, 0)
        self._finalize_discard_btn = QPushButton("放弃")
        self._finalize_discard_btn.setAccessibleName("放弃未保存的会议结束状态")
        self._finalize_discard_btn.setToolTip("放弃未保存的会议结束状态更新")
        self._finalize_discard_btn.clicked.connect(self._discard_unsaved_finalize)
        self._finalize_discard_btn.setStyleSheet(ss.BTN_DANGER_SM)
        self._finalize_discard_btn.hide()
        top.addWidget(self._finalize_discard_btn, 0)
        self._media_again_btn = QPushButton("重新导入")
        self._media_again_btn.setAccessibleName("重新导入刚才的音视频")
        self._media_again_btn.setToolTip("再识别一次刚才失败的文件")
        self._media_again_btn.clicked.connect(self._retry_failed_media)
        self._media_again_btn.setStyleSheet(ss.BTN_GHOST_SM)
        self._media_again_btn.hide()
        top.addWidget(self._media_again_btn, 0)
        self._start_btn = QPushButton("开始会议")
        self._start_btn.setAccessibleName("开始会议")
        self._start_btn.setToolTip("开始一场新的会议记录")
        self._start_tip = self._start_btn.toolTip()
        self._start_btn.clicked.connect(self.show_start_card)
        top.addWidget(self._start_btn, 0)
        self._start_block = QLabel("")
        self._start_block.setObjectName("meetingImportBlock")
        self._start_block.hide()
        top.addWidget(self._start_block, 0)
        outer.addLayout(top)
        self.setAcceptDrops(True)
        self.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips, True)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_browse())
        self._stack.addWidget(self._build_start_card())
        self._stack.addWidget(self._build_live())
        self._stack.currentChanged.connect(lambda _idx: self.view_mode_changed.emit(self.current_view_mode()))
        outer.addWidget(self._stack, 1)
        for button in self.findChildren(QPushButton):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._sync_media_buttons()
        self._sync_responsive_layout()

    def _build_browse(self) -> QWidget:
        browse_page = QWidget()
        browse_lay = QVBoxLayout(browse_page)
        browse_lay.setContentsMargins(0, 0, 0, 0)
        browse_lay.setSpacing(8)
        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(8)
        self._status_label = QLabel("")
        self._status_label.setTextFormat(Qt.TextFormat.PlainText)
        self._status_label.setWordWrap(True)
        self._status_label.hide()
        status_row.addWidget(self._status_label, 1)
        self._glossary_learn_btn = QPushButton("加入词典")
        self._glossary_learn_btn.setAccessibleName("将改错词条加入自定义词汇")
        self._glossary_learn_btn.clicked.connect(self.accept_glossary_suggestion)
        self._glossary_learn_btn.hide()
        status_row.addWidget(self._glossary_learn_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        browse_lay.addLayout(status_row)

        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.setChildrenCollapsible(False)
        self._splitter.setHandleWidth(5)

        left = QWidget()
        self._browse_left = left
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(6)
        left.setMinimumWidth(tok.WORKSPACE_LIST_MIN_WIDTH)
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("搜索标题、参会人或文字稿")
        self._search_edit.setAccessibleName("搜索会议")
        self._search_edit.setClearButtonEnabled(True)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(200)
        self._search_edit.textChanged.connect(lambda: self._search_timer.start())
        self._search_timer.timeout.connect(self._perform_search)
        left_lay.addWidget(self._search_edit)
        left_lay.addSpacing(4)
        self._summary_row = WrappingToolBar(spacing=8)
        self._list_summary = QLabel()
        self._list_summary.setContentsMargins(4, 0, 0, 0)
        self._summary_row.add(self._list_summary, role="flex")
        self._sort_btn = QPushButton()
        self._sort_btn.setAccessibleName("切换会议排序")
        self._sort_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._sort_btn.clicked.connect(self._toggle_list_sort)
        self._sync_sort_button()
        self._summary_row.add(self._sort_btn, role="right")
        self._selection_mode_btn = QPushButton("选择")
        self._selection_mode_btn.setCheckable(True)
        self._selection_mode_btn.setAccessibleName("选择会议")
        self._selection_mode_btn.setToolTip("勾选多场会议，一起导出、生成或删除；也可使用 Ctrl / Shift 多选，Delete 删除")
        self._selection_mode_btn.toggled.connect(self._set_batch_mode)
        self._summary_row.add(self._selection_mode_btn, role="right")
        left_lay.addWidget(self._summary_row)
        self._list = _SessionList()
        self._list.setObjectName("meetingList")
        self._list.setAccessibleName("会议列表")
        self._list.setFrameShape(QFrame.Shape.NoFrame)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self._list.setWordWrap(True)
        self._list.itemSelectionChanged.connect(self._on_selection_changed)
        self._list.itemClicked.connect(self._on_compact_meeting_clicked)
        self._delete_shortcut = QShortcut(QKeySequence.StandardKey.Delete, self._list)
        self._delete_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._delete_shortcut.activated.connect(self._delete_selected_meetings)
        left_lay.addWidget(self._list, 1)
        self._load_more_btn = QPushButton("加载更早的会议")
        self._load_more_btn.setAccessibleName("加载更早的会议")
        self._load_more_btn.clicked.connect(self._load_more)
        self._load_more_btn.hide()
        left_lay.addWidget(self._load_more_btn, 0, Qt.AlignmentFlag.AlignHCenter)
        self._empty_label = QLabel(_EMPTY_MEETINGS)
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setWordWrap(True)
        left_lay.addWidget(self._empty_label, 1)
        self._clear_search_btn = QPushButton("清除搜索")
        self._clear_search_btn.setMaximumWidth(160)
        self._clear_search_btn.clicked.connect(self._search_edit.clear)
        self._clear_search_btn.hide()
        left_lay.addWidget(self._clear_search_btn, 0, Qt.AlignmentFlag.AlignHCenter)
        self._splitter.addWidget(left)

        right = QWidget()
        right_lay = QVBoxLayout(right)
        self._detail_layout = right_lay
        right_lay.setContentsMargins(20, 0, 0, 0)
        right_lay.setSpacing(8)

        # Identity first (title + meta), then the content tabs; each tab's tools
        # sit in the tab row so they read as acting on that tab only.
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        self._back_to_list_btn = QPushButton("← 返回列表")
        self._back_to_list_btn.setObjectName("meetingBackToListBtn")
        self._back_to_list_btn.setAccessibleName("返回会议列表")
        self._back_to_list_btn.clicked.connect(self.show_compact_list)
        self._back_to_list_btn.hide()
        title_row.addWidget(self._back_to_list_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        self._detail_title = QLineEdit()
        self._detail_title.setPlaceholderText("会议标题")
        self._detail_title.setAccessibleName("会议标题")
        self._detail_title.setToolTip("点击修改会议标题")
        self._detail_title.editingFinished.connect(self._save_title)
        title_row.addWidget(self._detail_title, 1)
        self._copy_btn = QPushButton("复制")
        self._copy_btn.setToolTip("复制纪要 Markdown")
        self._copy_btn.clicked.connect(self._copy_current)
        self._export_btn = QPushButton("导出")
        self._export_btn.setToolTip("选择要导出的会议内容")
        self._export_menu = QMenu(self._export_btn)
        self._export_actions = {}
        for scope, label in (
            ("notes", "仅导出纪要"),
            ("transcript", "仅导出文字稿"),
            ("srt", "导出文字稿字幕 (.srt)"),
            ("all", "导出完整会议记录"),
        ):
            action = self._export_menu.addAction(label)
            action.triggered.connect(lambda _checked=False, kind=scope: self._export(kind))
            self._export_actions[scope] = action
        self._export_btn.setMenu(self._export_menu)
        self._more_btn = QPushButton("更多")
        self._more_btn.setAccessibleName("更多操作")
        self._more_btn.setToolTip("更多操作")
        self._more_menu = QMenu(self._more_btn)
        self._restore_action = self._more_menu.addAction("恢复上一版纪要")
        self._restore_action.triggered.connect(self._restore_previous)
        self._restore_user_action = self._more_menu.addAction("恢复手动修改版")
        self._restore_user_action.triggered.connect(self._restore_user_backup)
        self._more_menu.addSeparator()
        self._time_action = self._more_menu.addAction("修改会议时间")
        self._time_action.triggered.connect(self._edit_meeting_time)
        self._delete_action = self._more_menu.addAction("删除会议")
        self._delete_action.triggered.connect(self._delete)
        self._more_btn.setMenu(self._more_menu)
        title_row.addWidget(self._more_btn)
        right_lay.addLayout(title_row)

        self._batch_tools = WrappingToolBar(spacing=8)
        self._batch_all_btn = QPushButton("全选已加载")
        self._batch_all_btn.setAccessibleName("全选已加载的会议")
        self._batch_all_btn.setToolTip("只选择当前列表里已经加载的会议。更早的要先点「加载更早的会议」")
        self._batch_all_btn.clicked.connect(self._select_loaded_meetings)
        self._batch_export_btn = QPushButton("导出")
        self._batch_export_btn.setAccessibleName("导出选中的会议")
        self._batch_export_btn.setToolTip("把所选会议写入一个 Markdown 文件")
        self._batch_export_btn.clicked.connect(self._export_selected_meetings)
        self._batch_generate_btn = QPushButton("生成纪要")
        self._batch_generate_btn.setAccessibleName("为选中的会议生成纪要")
        self._batch_generate_btn.setToolTip("只排队还没有纪要的会议。已有纪要的会跳过")
        self._batch_generate_btn.clicked.connect(self._generate_selected_meetings)
        self._batch_delete_btn = QPushButton("删除")
        self._batch_delete_btn.setAccessibleName("删除选中的会议")
        self._batch_delete_btn.setToolTip("删除勾选的已结束会议，无法撤销")
        self._batch_delete_btn.clicked.connect(self._delete_selected_meetings)
        for button in (self._batch_all_btn, self._batch_export_btn, self._batch_generate_btn, self._batch_delete_btn):
            self._batch_tools.add(button)
        self._batch_tools.hide()
        right_lay.addWidget(self._batch_tools)
        self._batch_notice = QLabel("选择要一起处理的会议。")
        self._batch_notice.setTextFormat(Qt.TextFormat.PlainText)
        self._batch_notice.setWordWrap(True)
        self._batch_notice.setAccessibleName("已选会议说明")
        self._batch_notice.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._batch_notice_scroll = QScrollArea()
        self._batch_notice_scroll.setWidgetResizable(True)
        self._batch_notice_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._batch_notice_scroll.setWidget(self._batch_notice)
        self._batch_notice_scroll.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding
        )
        self._batch_notice_scroll.hide()
        right_lay.addWidget(self._batch_notice_scroll, 1)
        self._import_kicker = QLabel("正在导入")
        self._import_kicker.setObjectName("meetingImportKicker")
        self._import_kicker.setToolTip("导入期间可以查看其他会议。听写和开始会议要等这次导入结束。")
        self._import_kicker.hide()
        right_lay.insertWidget(0, self._import_kicker)
        self._import_filename = _ElidedLabel("")
        self._import_filename.setObjectName("meetingImportFile")
        self._import_filename.hide()
        right_lay.insertWidget(1, self._import_filename)
        import_row = QHBoxLayout()
        import_row.setSpacing(8)
        self._import_progress = QLabel("")
        self._import_progress.setTextFormat(Qt.TextFormat.PlainText)
        self._import_progress.setWordWrap(True)
        self._import_progress.setAccessibleName("导入进度")
        self._import_progress.hide()
        import_row.addWidget(self._import_progress, 1)
        import_row.addWidget(self._media_cancel_btn, 0, Qt.AlignmentFlag.AlignTop)
        right_lay.addLayout(import_row)
        self._import_progress_bar = QProgressBar()
        self._import_progress_bar.setAccessibleName("导入进度条")
        self._import_progress_bar.setRange(0, 100)
        self._import_progress_bar.setValue(0)
        self._import_progress_bar.setFixedHeight(10)
        self._import_progress_bar.setTextVisible(False)
        self._import_progress_bar.hide()
        right_lay.addWidget(self._import_progress_bar)
        self._import_queue = QWidget()
        self._import_queue_lay = QVBoxLayout(self._import_queue)
        self._import_queue_lay.setContentsMargins(0, 0, 0, 0)
        self._import_queue_lay.setSpacing(4)
        self._import_queue.hide()
        right_lay.addWidget(self._import_queue)
        self._import_live_caption = QLabel("已识别的文字，完成后还可以修改。")
        self._import_live_caption.setObjectName("meetingImportLiveCaption")
        self._import_live_caption.hide()
        right_lay.addWidget(self._import_live_caption)
        self._import_live = QPlainTextEdit()
        self._import_live.setReadOnly(True)
        self._import_live.setAccessibleName("已识别的文字")
        self._import_live.setPlaceholderText("识别出的文字会显示在这里。")
        self._import_live.setMaximumHeight(140)
        self._import_live.hide()
        right_lay.addWidget(self._import_live)

        self._detail_meta = QLabel("")
        self._detail_meta.setObjectName("meetingDetailMeta")
        self._detail_meta.setWordWrap(True)
        self._detail_meta.setContentsMargins(4, 0, 0, 0)
        self._notes_state = QLabel()
        self._notes_state.setTextFormat(Qt.TextFormat.PlainText)
        self._notes_state.setWordWrap(True)
        self._notes_state.setAccessibleName("纪要来源与保存状态")
        self._state_row = QWidget()
        self._state_row.setObjectName("meetingState")
        state_lay = QHBoxLayout(self._state_row)
        state_lay.setContentsMargins(0, 0, 0, 0)
        state_lay.setSpacing(8)
        state_lay.addWidget(self._notes_state, 1)
        self._cancel_btn = QPushButton("取消")
        self._cancel_btn.clicked.connect(self._cancel_job)
        self._cancel_btn.hide()
        state_lay.addWidget(self._cancel_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        right_lay.addWidget(self._detail_meta)
        right_lay.addWidget(self._state_row)
        self._integrity_banner = QFrame()
        self._integrity_banner.setObjectName("meetingIntegrityBanner")
        integrity_lay = QHBoxLayout(self._integrity_banner)
        integrity_lay.setContentsMargins(10, 6, 8, 6)
        integrity_lay.setSpacing(8)
        self._integrity_label = QLabel("")
        self._integrity_label.setTextFormat(Qt.TextFormat.PlainText)
        self._integrity_label.setWordWrap(True)
        self._integrity_label.setAccessibleName("会议片段完整性提示")
        integrity_lay.addWidget(self._integrity_label, 1)
        self._segment_retry_btn = QPushButton("重试保存缺失片段")
        self._segment_retry_btn.setAccessibleName("重试保存缺失片段")
        self._segment_retry_btn.clicked.connect(lambda: self.retry_unsaved_segments())
        self._segment_retry_btn.hide()
        integrity_lay.addWidget(self._segment_retry_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        self._integrity_banner.hide()
        right_lay.addWidget(self._integrity_banner)
        self._materials = QWidget()
        self._materials.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        materials_lay = QVBoxLayout(self._materials)
        materials_lay.setContentsMargins(0, 0, 0, 0)
        materials_lay.setSpacing(4)
        self._materials_summary_text = "会议资料"
        self._materials_toggle = QPushButton("▸ 会议资料")
        self._materials_toggle.setCheckable(True)
        self._materials_toggle.setChecked(False)
        self._materials_toggle.setAccessibleName("会议资料")
        self._materials_toggle.setToolTip("点击展开或收起议程、随手笔记和标记。笔记在离开时自动保存。")
        materials_lay.addWidget(self._materials_toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self._materials_body = QWidget()
        body_lay = QVBoxLayout(self._materials_body)
        body_lay.setContentsMargins(0, 2, 4, 2)
        body_lay.setSpacing(4)
        self._materials_agenda = QLabel()
        self._materials_agenda.setWordWrap(True)
        self._materials_agenda.setTextFormat(Qt.TextFormat.PlainText)
        body_lay.addWidget(self._materials_agenda)
        self._materials_notes_label = QLabel("我的笔记")
        body_lay.addWidget(self._materials_notes_label)
        self._record_notes = QPlainTextEdit()
        self._record_notes.setAccessibleName("我的笔记")
        self._record_notes.setPlaceholderText("会中记下的内容；离开时自动保存。")
        self._record_notes.setMinimumHeight(38)
        self._record_notes.setMaximumHeight(84)
        self._record_notes_timer = QTimer(self)
        self._record_notes_timer.setSingleShot(True)
        self._record_notes_timer.setInterval(800)
        self._record_notes_timer.timeout.connect(self._autosave_record_notes)
        self._record_notes.textChanged.connect(self._on_record_notes_changed)
        body_lay.addWidget(self._record_notes)
        self._materials_marks_label = QLabel("标记")
        body_lay.addWidget(self._materials_marks_label)
        self._record_marks = _CompactTextBrowser()
        self._record_marks.setAccessibleName("标记")
        self._record_marks.setOpenLinks(False)
        self._record_marks.setMinimumHeight(32)
        self._record_marks.setMaximumHeight(76)
        self._record_marks.anchorClicked.connect(self._on_notes_link)
        body_lay.addWidget(self._record_marks)
        self._materials_scroll = QScrollArea()
        self._materials_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._materials_scroll.setWidgetResizable(True)
        self._materials_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._materials_scroll.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self._materials_scroll.setMaximumHeight(168)
        self._materials_scroll.setWidget(self._materials_body)
        self._materials_scroll.hide()
        self._materials_toggle.toggled.connect(self._on_materials_toggled)
        materials_lay.addWidget(self._materials_scroll)
        self._materials.hide()
        right_lay.addWidget(self._materials)

        self._audio_player = _AudioPlayerBar()
        self._audio_player.playback_error.connect(self._set_status)
        self._audio_player.hide()
        right_lay.addWidget(self._audio_player)
        self._audio_missing = QLabel("找不到原来的音视频文件，无法播放。导入时没有复制这份文件。")
        self._audio_missing.setWordWrap(True)
        self._audio_missing.setAccessibleName("原文件缺失")
        self._audio_missing.hide()
        right_lay.addWidget(self._audio_missing)

        tab_row = QHBoxLayout()
        tab_row.setSpacing(8)
        tab_row.setContentsMargins(0, 6, 0, 0)
        self._tab_bar = QWidget()
        self._tab_bar.setObjectName("meetingTabs")
        tabs_lay = QHBoxLayout(self._tab_bar)
        tabs_lay.setContentsMargins(3, 2, 3, 2)
        tabs_lay.setSpacing(2)
        self._tab_group = QButtonGroup(self)
        self._tab_group.setExclusive(True)
        self._tab_notes = QPushButton("纪要")
        self._tab_transcript = QPushButton("文字稿")
        for index, button in enumerate((self._tab_notes, self._tab_transcript)):
            button.setCheckable(True)
            button.setObjectName("meetingTab")
            button.setMinimumWidth(88)
            button.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
            self._tab_group.addButton(button, index)
            tabs_lay.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)
        self._tab_notes.setChecked(True)
        self._tab_group.idClicked.connect(self._switch_tab)
        tab_row.addWidget(self._tab_bar, 0, Qt.AlignmentFlag.AlignVCenter)
        tab_row.addStretch(1)
        tab_row.addWidget(self._export_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        right_lay.addLayout(tab_row)

        self._tab_tools = _CurrentPageStack()
        notes_tools = QWidget()
        notes_outer = QVBoxLayout(notes_tools)
        notes_outer.setContentsMargins(0, 0, 0, 0)
        notes_outer.setSpacing(8)
        self._template_label = QLabel("模板")
        self._template_combo = QComboBox()
        self._template_combo.setAccessibleName("会议模板")
        self._template_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        for template in list_templates(self._custom_templates()):
            self._template_combo.addItem(template.name, template.id)
            self._template_combo.setItemData(
                self._template_combo.count() - 1, describe_template(template), Qt.ItemDataRole.ToolTipRole
            )
        self._template_combo.currentIndexChanged.connect(self._on_template_changed)
        self._template_combo.setMinimumWidth(96)
        self._template_combo.setMaximumWidth(168)
        self._template_combo.setToolTip(str(self._template_combo.itemData(0, Qt.ItemDataRole.ToolTipRole) or ""))
        self._template_label.setBuddy(self._template_combo)
        self._manage_templates_btn = QPushButton("管理模板")
        self._manage_templates_btn.setToolTip("新建自定义模板，或复制内置模板后修改")
        self._manage_templates_btn.clicked.connect(self._manage_templates)
        self._template_menu_btn = QPushButton("模板")
        self._template_menu_btn.setAccessibleName("选择会议模板")
        self._template_menu_btn.setToolTip("选择纪要模板")
        self._template_menu = QMenu(self._template_menu_btn)
        self._template_menu_btn.setMenu(self._template_menu)
        self._template_menu_btn.hide()
        self._fitting_templates = False
        self._generate_btn = QPushButton("生成纪要")
        self._generate_btn.setAccessibleName("生成纪要")
        self._generate_btn.clicked.connect(self._generate)
        self._edit_btn = QPushButton("编辑正文")
        self._edit_btn.setToolTip("直接修改纪要文本")
        self._edit_btn.clicked.connect(self._toggle_edit)
        self._cancel_notes_edit_btn = QPushButton("取消")
        self._cancel_notes_edit_btn.setAccessibleName("取消编辑纪要")
        self._cancel_notes_edit_btn.setToolTip("放弃本次未保存的修改并恢复修改前内容")
        self._cancel_notes_edit_btn.clicked.connect(self._cancel_edit_notes)
        self._cancel_notes_edit_btn.hide()
        self._notes_bar = PairedToolBar(spacing=8)
        self._notes_bar.add_left(self._template_label)
        self._notes_bar.add_left(self._template_combo)
        self._notes_bar.add_left(self._manage_templates_btn)
        self._notes_bar.add_left(self._template_menu_btn)
        self._notes_bar.installEventFilter(self)
        self._notes_bar.add_right(self._generate_btn)
        self._notes_bar.add_right(self._edit_btn)
        self._notes_bar.add_right(self._cancel_notes_edit_btn)
        self._notes_bar.add_right(self._copy_btn)
        notes_outer.addWidget(self._notes_bar)
        self._tab_tools.addWidget(notes_tools)
        tools_policy = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        tools_policy.setHeightForWidth(True)
        self._tab_tools.setSizePolicy(tools_policy)

        transcript_tools = QWidget()
        transcript_lay = QVBoxLayout(transcript_tools)
        transcript_lay.setContentsMargins(0, 0, 0, 0)
        transcript_lay.setSpacing(8)
        self._transcript_bar = PairedToolBar(spacing=8)
        self._speaker_btn = QPushButton("命名说话人")
        self._speaker_btn.setAccessibleName("为整组说话人命名")
        self._speaker_btn.setToolTip("为同一组发言设置显示名称；名称来自手动设置，不表示已验证身份")
        self._speaker_menu = QMenu(self._speaker_btn)
        self._speaker_btn.setMenu(self._speaker_menu)
        self._transcript_bar.add_right(self._speaker_btn)
        self._correct_speaker_btn = QPushButton("纠正发言人")
        self._correct_speaker_btn.setCheckable(True)
        self._correct_speaker_btn.toggled.connect(self._toggle_speaker_correction)
        self._transcript_bar.add_right(self._correct_speaker_btn)
        self._return_notes_btn = QPushButton("返回纪要")
        self._return_notes_btn.setAccessibleName("返回纪要引用处")
        self._return_notes_btn.setToolTip("回到刚才阅读的纪要引用位置")
        self._return_notes_btn.clicked.connect(self._return_to_notes)
        self._return_notes_btn.hide()
        self._transcript_bar.add_right(self._return_notes_btn)
        self._transcript_copy_btn = QPushButton("复制文字稿")
        self._transcript_copy_btn.setAccessibleName("复制文字稿")
        self._transcript_copy_btn.setToolTip("复制完整文字稿（含时间与说话人名称） · Ctrl+C")
        self._transcript_copy_btn.clicked.connect(self._copy_transcript)
        self._transcript_bar.add_right(self._transcript_copy_btn)
        transcript_lay.addWidget(self._transcript_bar)
        self._tab_tools.addWidget(transcript_tools)
        right_lay.addWidget(self._tab_tools)
        self._content_hint = QLabel()
        self._content_hint.setWordWrap(True)
        self._content_hint.hide()
        right_lay.addWidget(self._content_hint)

        self._detail_find_bar = QWidget()
        detail_find_lay = QHBoxLayout(self._detail_find_bar)
        detail_find_lay.setContentsMargins(0, 0, 0, 0)
        detail_find_lay.setSpacing(6)
        self._detail_find_edit = QLineEdit()
        self._detail_find_edit.setPlaceholderText("查找当前纪要或文字稿；Enter 下一处，Shift+Enter 上一处")
        self._detail_find_edit.setAccessibleName("查找当前纪要或文字稿")
        self._detail_find_edit.setClearButtonEnabled(True)
        self._detail_find_edit.textChanged.connect(lambda: self._find_detail_text(restart=True))
        self._detail_find_edit.returnPressed.connect(self._find_detail_text)
        self._detail_find_edit.installEventFilter(self)
        detail_find_lay.addWidget(self._detail_find_edit, 1)
        self._detail_find_status = QLabel()
        detail_find_lay.addWidget(self._detail_find_status, 0)
        self._detail_find_prev_btn = QPushButton("上一处")
        self._detail_find_prev_btn.setAccessibleName("查找上一处")
        self._detail_find_prev_btn.clicked.connect(lambda: self._find_detail_text(backward=True))
        detail_find_lay.addWidget(self._detail_find_prev_btn, 0)
        self._detail_find_next_btn = QPushButton("下一处")
        self._detail_find_next_btn.setAccessibleName("查找下一处")
        self._detail_find_next_btn.clicked.connect(lambda: self._find_detail_text(backward=False))
        detail_find_lay.addWidget(self._detail_find_next_btn, 0)
        self._detail_find_close_btn = QPushButton("关闭查找")
        self._detail_find_close_btn.clicked.connect(self._close_detail_find)
        detail_find_lay.addWidget(self._detail_find_close_btn, 0)
        self._detail_find_bar.hide()
        right_lay.addWidget(self._detail_find_bar)

        self._empty_speech_bar = QWidget()
        empty_bar_lay = QHBoxLayout(self._empty_speech_bar)
        empty_bar_lay.setContentsMargins(0, 2, 0, 2)
        empty_bar_lay.setSpacing(8)
        self._empty_play_btn = QPushButton("回放本场录音")
        self._empty_play_btn.setAccessibleName("回放本场录音")
        self._empty_play_btn.clicked.connect(self._play_empty_meeting_audio)
        empty_bar_lay.addWidget(self._empty_play_btn, 0)
        self._empty_device_btn = QPushButton("检查音频设备")
        self._empty_device_btn.setAccessibleName("检查音频设备")
        self._empty_device_btn.clicked.connect(self.audio_settings_requested.emit)
        empty_bar_lay.addWidget(self._empty_device_btn, 0)
        empty_bar_lay.addStretch(1)
        self._empty_speech_bar.hide()
        right_lay.addWidget(self._empty_speech_bar)

        self._content_stack = QStackedWidget()
        self._content_stack.setMinimumHeight(140)
        self._notes_stack = QStackedWidget()
        self._notes_view = _ReadingView()
        self._notes_view.setOpenLinks(False)
        self._notes_view.copy_document_requested.connect(self._copy_current)
        self._notes_view.anchorClicked.connect(self._on_notes_link)
        self._notes_view.setAccessibleName("会议纪要")
        self._notes_view.setPlaceholderText("还没有纪要。选择模板后点击「生成纪要」。")
        self._notes_view.installEventFilter(self)
        self._notes_edit = QPlainTextEdit()
        self._notes_edit.setAccessibleName("编辑会议纪要")
        self._notes_edit.textChanged.connect(self._sync_copy_action)
        self._notes_edit.installEventFilter(self)
        self._notes_stack.addWidget(self._notes_view)
        self._notes_stack.addWidget(self._notes_edit)
        # QTextEdit placeholders are drawn on one line by Qt. Use a real label
        # for an empty document so instructions wrap with the detail pane.
        self._notes_empty = QLabel()
        self._notes_empty.setWordWrap(True)
        self._notes_empty.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._notes_empty.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._notes_stack.addWidget(self._notes_empty)
        self._content_stack.addWidget(self._notes_stack)
        self._transcript_view = _TranscriptView()
        self._transcript_view.setAccessibleName("会议文字稿")
        self._transcript_view.setPlaceholderText("文字稿为空")
        self._transcript_view.setOpenLinks(False)
        self._transcript_view.copy_document_requested.connect(self._copy_current)
        self._transcript_view.anchorClicked.connect(self._on_transcript_link)
        self._transcript_view.installEventFilter(self)
        self._content_stack.addWidget(self._transcript_view)
        right_lay.addWidget(self._content_stack, 1)
        # Takes the spare height only when the content stack is hidden
        # (batch selection, media import), so those views stay top-aligned.
        right_lay.addStretch(0)

        self._detail_pane = right
        right.setMinimumWidth(tok.WORKSPACE_DETAIL_MIN_WIDTH)
        self._splitter.addWidget(right)
        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 2)
        self._splitter.setSizes(list(tok.WORKSPACE_SPLIT_SIZES))
        browse_lay.addWidget(self._splitter, 1)
        return browse_page

    # ── theme ─────────────────────────────────────────────────────

    @staticmethod
    def _disabled_btn_sm_override() -> str:
        from sayink.ui import design_tokens as tok

        return (
            f"\nQPushButton:disabled {{ color: {tok.TEXT_DIM}; background: {tok.SURFACE_PEARL};"
            f" border: 1px solid {tok.HAIRLINE}; font-weight: 400; }}"
        )

    def reapply_theme(self) -> None:
        from sayink.ui.meeting_workspace_chrome import reapply_theme as _reapply_theme

        _reapply_theme(self)

    # ── list / browse ─────────────────────────────────────────────

    def _custom_templates(self) -> list[dict]:
        custom = self._config.get("meeting.custom_templates", []) or []
        return [t for t in custom if isinstance(t, dict)]

    def _reload_templates(self) -> None:
        from sayink.ui.minutes_templates import populate_template_combo

        selected = self._template_combo.currentData() or self._config.get("meeting.template_id", "general")
        snapshot = (copy.deepcopy(self._custom_templates()), selected)
        if (
            self._template_combo.count() > 0
            and bool(self._template_menu.actions())
            and snapshot == self._templates_snapshot
        ):
            return
        populate_template_combo(self._template_combo, self._config, self._template_combo.currentData())
        self._rebuild_template_menu()
        self._templates_snapshot = (
            copy.deepcopy(self._custom_templates()),
            self._template_combo.currentData() or self._config.get("meeting.template_id", "general"),
        )

    def _rebuild_template_menu(self) -> None:
        menu = self._template_menu
        menu.clear()
        current = self._template_combo.currentData()
        for index in range(self._template_combo.count()):
            action = menu.addAction(self._template_combo.itemText(index))
            action.setCheckable(True)
            action.setChecked(self._template_combo.itemData(index) == current)
            tip = self._template_combo.itemData(index, Qt.ItemDataRole.ToolTipRole)
            if tip:
                action.setToolTip(str(tip))
            action.triggered.connect(lambda _checked=False, idx=index: self._select_template(idx))
        menu.addSeparator()
        manage = menu.addAction("管理模板")
        manage.triggered.connect(self._manage_templates)
        self._sync_template_menu_tip()

    def _sync_template_menu(self) -> None:
        if not hasattr(self, "_template_menu"):
            return
        names = [self._template_combo.itemText(i) for i in range(self._template_combo.count())]
        actions = [action for action in self._template_menu.actions() if not action.isSeparator() and action.text() != "管理模板"]
        if [action.text() for action in actions] != names:
            self._rebuild_template_menu()
            return
        current = self._template_combo.currentData()
        for index, action in enumerate(actions):
            action.setChecked(self._template_combo.itemData(index) == current)
        self._sync_template_menu_tip()

    def _sync_template_menu_tip(self) -> None:
        text = self._template_combo.currentText()
        self._template_menu_btn.setToolTip(f"当前模板：{text}" if text else "选择纪要模板")

    def _select_template(self, index: int) -> None:
        if self._template_combo.currentIndex() != index:
            self._template_combo.setCurrentIndex(index)
        else:
            self._sync_template_menu()

    def _fit_template_cluster(self) -> None:
        """Keep post-meeting actions on one row when the window is narrow.

        The label, combo and 管理模板 stay inline when they fit. Otherwise they
        collapse into the 模板 menu so the notes body does not lose a row.
        """
        if self._fitting_templates or not hasattr(self, "_notes_bar"):
            return
        bar = self._notes_bar
        width = bar.width()
        if width <= 0:
            return
        full = (self._template_label, self._template_combo, self._manage_templates_btn)
        actions = [
            widget
            for widget in (self._generate_btn, self._edit_btn, self._cancel_notes_edit_btn, self._copy_btn)
            if not widget.isHidden()
        ]
        gap = bar._spacing if actions else 0
        compact = bar._span(list(full)) + gap + bar._span(actions) > width
        if compact == self._template_menu_btn.isVisible() and all(widget.isVisible() != compact for widget in full):
            return
        self._fitting_templates = True
        try:
            for widget in full:
                widget.setVisible(not compact)
            self._template_menu_btn.setVisible(compact)
        finally:
            self._fitting_templates = False

    def _manage_templates(self) -> None:
        from sayink.ui.minutes_templates import MinutesTemplatesDialog

        dialog = MinutesTemplatesDialog(self._config, self._template_combo.currentData(), self)
        with _quiet_tooltips(self):
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
        if accepted:
            self._reload_templates()
            self._template_combo.setCurrentIndex(self._template_combo.findData(dialog.selected_template_id))
            self._on_template_changed(self._template_combo.currentIndex())

    def _list_sort_mode(self) -> str:
        raw = str(self._config.get("meeting.list_sort", SORT_RECENT) or SORT_RECENT)
        return SORT_STARTED_AT if raw == SORT_STARTED_AT else SORT_RECENT

    def _sync_sort_button(self) -> None:
        if not hasattr(self, "_sort_btn"):
            return
        mode = self._list_sort_mode()
        if mode == SORT_STARTED_AT:
            self._sort_btn.setText("按会议时间")
            self._sort_btn.setToolTip("当前按会议发生时间排序；点击切换为按最近添加排序")
        else:
            self._sort_btn.setText("最近添加")
            self._sort_btn.setToolTip("当前按最近添加/开会排序（新导入的会议始终在最上方）；点击切换为按会议时间排序")
        fit_action_button(self._sort_btn)

    def flush_all_drafts(self, *, sync: bool = True) -> bool:
        """Public contract flushing live user notes, bound title, notes draft, and record notes."""
        self.flush_pending_user_notes()
        return bool(
            self._flush_bound_title(sync=sync)
            and self._save_notes_draft()
            and self._save_record_notes()
        )

    def _toggle_list_sort(self) -> None:
        if not self._flush_bound_title(sync=True) or not self._save_notes_draft() or not self._save_record_notes():
            return
        next_mode = SORT_STARTED_AT if self._list_sort_mode() == SORT_RECENT else SORT_RECENT
        self._config.set("meeting.list_sort", next_mode)
        self._sync_sort_button()
        self.refresh()

    def _query_list_meetings(self, *, limit: int) -> list[MeetingRecord]:
        sort_by = self._list_sort_mode()
        try:
            return self._store.list_meetings(limit=limit, sort_by=sort_by)
        except TypeError:
            return self._store.list_meetings(limit=limit)

    def _query_search_meetings(self, query: str, *, limit: int) -> list[MeetingRecord]:
        sort_by = self._list_sort_mode()
        try:
            return self._store.search_meetings(query, limit=limit, sort_by=sort_by)
        except TypeError:
            return self._store.search_meetings(query, limit=limit)

    def refresh(self) -> None:
        self._reload_templates()
        self._sync_sort_button()
        if self._store.disabled:
            self._list_summary.setText("历史数据库不可用，会议记录无法保存")
            return
        keep = self._detail_id
        batch_selected = set(self._selected_meeting_ids()) if self._batch_mode else set()
        has_more_search = False
        if self._search_query:
            matched = self._query_search_meetings(self._search_query, limit=self._list_limit + 1)
            has_more_search = len(matched) > self._list_limit
            records = matched[:self._list_limit]
        else:
            records = self._query_list_meetings(limit=self._list_limit)
        if (keep and not self._search_query and records
                and all(record.id != keep for record in records)
                and self._store.get_meeting(keep) is not None):
            # A meeting opened past the first page stays selected when newer ones arrive.
            total = self._store.count_meetings()
            if total > self._list_limit:
                self._list_limit = total
                records = self._query_list_meetings(limit=self._list_limit)
        records = self._drafts.apply_optimistic_titles(records)
        self._records = {r.id: r for r in records}
        minutes = self._minutes_markers()
        rows_key = (
            self._list_sort_mode(),
            tuple((record.id, record.title, record.status, record.source, record.started_at, record.ended_at) for record in records),
            tok.FONT, tok.TEXT, tok.TEXT_SEC, tok.TEXT_DIM,
        )
        self._list.blockSignals(True)
        try:
            if getattr(self, "_meeting_rows_key", None) != rows_key:
                self._release_row_widgets()
                for record in records:
                    item = QListWidgetItem()
                    item.setData(Qt.ItemDataRole.UserRole, record.id)
                    item.setData(Qt.ItemDataRole.AccessibleTextRole, _meeting_row_text(record, minutes.get(record.id, "")))
                    row = self._build_list_row(record, minutes.get(record.id, ""))
                    self._list.addItem(item)
                    self._list.setItemWidget(item, row)
                    row.ensurePolished()
                    for label in row.findChildren(QLabel):
                        label.ensurePolished()
                    item.setSizeHint(row.sizeHint())
                self._meeting_rows_key = rows_key
            self._sync_row_minutes(records, minutes)
            if self._batch_mode:
                for index in range(self._list.count()):
                    item = self._list.item(index)
                    item.setSelected(str(item.data(Qt.ItemDataRole.UserRole) or "") in batch_selected)
            elif keep in self._records and not (
                getattr(self._ctrl, "importing_media", False) and self._import_owns_detail
            ):
                self._list.clearSelection()
                for index in range(self._list.count()):
                    item = self._list.item(index)
                    if item.data(Qt.ItemDataRole.UserRole) == keep:
                        item.setSelected(True)
                        self._list.setCurrentItem(item)
                        self._list.scrollToItem(item)
                        break
            elif getattr(self._ctrl, "importing_media", False) and self._import_owns_detail:
                self._list.clearSelection()
            else:
                self._list.clearSelection()
                self._list.setCurrentItem(None)
        finally:
            self._list.blockSignals(False)
        count = len(records)
        if self._search_query:
            self._list_summary.setText(f"找到 {count}{'+' if has_more_search else ''} 场会议")
            self._load_more_btn.setVisible(has_more_search)
        else:
            total = self._store.count_meetings()
            if count < total:
                self._list_summary.setText(f"最近 {count} 场，共 {total} 场会议")
            else:
                self._list_summary.setText(f"共 {total} 场会议")
            self._load_more_btn.setVisible(count < total and count > 0)
        if self._batch_mode:
            self._sync_batch_summary()
        self._empty_label.setText(
            "未找到匹配的会议。\n试试其他关键词，或清除搜索查看全部会议。" if self._search_query else
            _EMPTY_MEETINGS
        )
        self._empty_label.setVisible(count == 0)
        self._list.setVisible(count > 0)
        self._selection_mode_btn.setEnabled(count > 0 or self._batch_mode)
        self._clear_search_btn.setVisible(count == 0 and bool(self._search_query))
        self._sync_batch_checks()
        had_detail = bool(keep)
        if self._batch_mode:
            if not records:
                if not self._flush_bound_title(sync=True) or not self._save_notes_draft() or not self._save_record_notes():
                    return
                self._detail_id = ""
                self._clear_detail(clear_status=had_detail)
            elif keep and keep in self._records:
                self._render_detail()
            elif records:
                self._detail_id = records[0].id
                self._render_detail()
        elif keep and keep in self._records:
            self._render_detail()
        elif records:
            self._list.setCurrentRow(0)
            first_item = self._list.item(0)
            if first_item is not None:
                self._list.scrollToItem(first_item)
        elif not records and not getattr(self._ctrl, "importing_media", False):
            if not self._flush_bound_title(sync=True) or not self._save_notes_draft() or not self._save_record_notes():
                return
            self._detail_id = ""
            self._clear_detail(clear_status=had_detail)
        importing = bool(getattr(self._ctrl, "importing_media", False))
        show_import = importing and (
            self._import_owns_detail or not self._detail_id or self._detail_id not in self._records
        )
        if show_import:
            self._show_import_detail()
        elif importing:
            self._paint_import_banner()
        from sayink.ui import meeting_workspace_chrome as mwc
        mwc.finish_page_refresh(self, has_records=bool(records), importing=importing)

    def _release_row_widgets(self) -> None:
        """Drop row widgets before ``clear()``.

        A visible row widget that loses its parent becomes a top-level window
        for one frame and shows up as a flickering application icon.
        """
        release_list_item_widgets(self._list, self)

    def _sync_row_minutes(self, records: list[MeetingRecord], minutes: dict[str, str]) -> None:
        """Queue and generating labels change often. Update them without rebuilding rows."""
        by_id = {record.id: record for record in records}
        for index in range(self._list.count()):
            item = self._list.item(index)
            record = by_id.get(str(item.data(Qt.ItemDataRole.UserRole) or ""))
            widget = self._list.itemWidget(item)
            if record is None or widget is None:
                continue
            marker = minutes.get(record.id, "")
            text = _meeting_row_meta(record, marker)
            meta = widget.findChild(_ElidedLabel, "meetingRowMeta")
            if meta is not None and meta._full_text != " ".join(text.split()):
                meta.set_full_text(text)
            accessible = _meeting_row_text(record, marker)
            if item.data(Qt.ItemDataRole.AccessibleTextRole) != accessible:
                item.setData(Qt.ItemDataRole.AccessibleTextRole, accessible)

    def _minutes_markers(self) -> dict[str, str]:
        """Meeting id → minutes job state shown in its list row."""
        markers = {job.meeting_id: "纪要生成失败" if job.failed else "纪要排队中"
                   for job in self._store.generation_queue()}
        self._listed_generating = self._ctrl.generating_for
        if self._listed_generating:
            markers[self._listed_generating] = "纪要生成中"
        return markers

    def _build_list_row(self, record: MeetingRecord, minutes: str = "") -> QWidget:
        row = _MeetingStreamRow()
        row.setObjectName("meetingStreamRow")
        row.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        outer = QHBoxLayout(row)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(8)
        box = _SessionCheckBox()
        box.setFixedSize(24, 32)
        box.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        box.setObjectName("meetingBatchCheck")
        box.setAccessibleName(f"选择会议：{record.display_title}")
        box.setVisible(self._batch_mode)
        box.clicked.connect(lambda checked, meeting_id=record.id: self._toggle_meeting_selection(meeting_id, checked))
        outer.addWidget(box, 0, Qt.AlignmentFlag.AlignVCenter)
        lay = QVBoxLayout()
        lay.setSpacing(2)
        title = _ElidedLabel(record.display_title, show_full_tooltip=False)
        title.setObjectName("meetingRowTitle")
        lay.addWidget(title)
        meta = _ElidedLabel(_meeting_row_meta(record, minutes), show_full_tooltip=False)
        meta.setObjectName("meetingRowMeta")
        lay.addWidget(meta)
        outer.addLayout(lay, 1)
        _paint_meeting_row(row, selected=False)
        return row

    def _toggle_meeting_selection(self, meeting_id: str, checked: bool) -> None:
        if not self._batch_mode:
            self._set_batch_mode(True)
        for index in range(self._list.count()):
            item = self._list.item(index)
            if str(item.data(Qt.ItemDataRole.UserRole) or "") == meeting_id:
                item.setSelected(bool(checked))
                break

    def _perform_search(self) -> None:
        if not self._flush_bound_title(sync=True) or not self._save_notes_draft() or not self._save_record_notes():
            self._search_edit.blockSignals(True)
            self._search_edit.setText(self._search_query)
            self._search_edit.blockSignals(False)
            return
        self._search_query = self._search_edit.text().strip()
        self.refresh()

    def _active_detail_search_view(self):
        if self._content_stack.currentIndex() == 1:
            return self._transcript_view
        return self._notes_edit if self._editing_notes else self._notes_view

    def open_detail_find(self) -> None:
        if not self._detail_id or self._batch_mode or getattr(self._ctrl, "importing_media", False):
            return
        self._detail_find_bar.show()
        self._detail_find_edit.setFocus()
        self._detail_find_edit.selectAll()
        if self._detail_find_edit.text().strip():
            self._find_detail_text(restart=True)

    def _close_detail_find(self) -> None:
        self._detail_find_bar.hide()
        for view in (self._notes_view, self._notes_edit, self._transcript_view):
            clear_reading_search(view)
        target = self._active_detail_search_view()
        if target is not None:
            target.setFocus()

    def _find_detail_text(self, *, backward: bool = False, restart: bool = False) -> None:
        query = self._detail_find_edit.text()
        view = self._active_detail_search_view()
        for other in (self._notes_view, self._notes_edit, self._transcript_view):
            if other is not view:
                clear_reading_search(other)
        if not query.strip():
            clear_reading_search(view)
            self._detail_find_status.setText("")
            return
        if restart and view is not None:
            cur = view.textCursor()
            cur.movePosition(QTextCursor.MoveOperation.Start)
            view.setTextCursor(cur)
        idx, total = find_in_reading_view(view, query, backward=backward, step=not restart)
        self._detail_find_status.setText("未找到" if total == 0 else f"{idx} / {total}")

    def focus_search(self) -> None:
        if self._stack.currentIndex() == PAGE_LIVE:
            self._live_find_bar.show()
            self._live_find_edit.setFocus()
            self._live_find_edit.selectAll()
        elif self._stack.currentIndex() == PAGE_START:
            self._opt_title.setFocus()
        elif (
            self._detail_id
            and not self._batch_mode
            and not getattr(self._ctrl, "importing_media", False)
            and not self._search_edit.hasFocus()
        ):
            self.open_detail_find()
        else:
            self._search_edit.setFocus()
            self._search_edit.selectAll()

    def _close_live_find(self) -> None:
        self._live_find_bar.hide()
        clear_reading_search(self._live_transcript)
        self._live_transcript.setFocus()

    def _find_live_text(self, *, backward: bool = False, restart: bool = False) -> None:
        query = self._live_find_edit.text()
        view = self._live_transcript
        if not query.strip():
            clear_reading_search(view)
            self._live_find_status.setText("")
            return
        if restart:
            cursor = view.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            view.setTextCursor(cursor)
        idx, total = find_in_reading_view(view, query, backward=backward, step=not restart)
        self._live_find_status.setText("未找到" if total == 0 else f"{idx} / {total}")

    def meeting_count(self) -> int:
        return self._list.count()

    def _on_selection_changed(self) -> None:
        if getattr(self._ctrl, "importing_media", False) and self._list.selectedItems():
            self._import_owns_detail = False
        ids = self._selected_meeting_ids()
        if len(ids) > 1 and not self._batch_mode:
            self._set_batch_mode(True)
            return
        self._sync_batch_checks(selected_ids=ids)
        if self._batch_mode:
            self._sync_batch_summary(selected_ids=ids)
            self._sync_batch_actions(selected_ids=ids)
            return
        items = self._list.selectedItems()
        if not items:
            return
        meeting_id = str(items[0].data(Qt.ItemDataRole.UserRole) or "")
        if meeting_id != self._detail_id:
            if not self._flush_bound_title(sync=True):
                self._list.blockSignals(True)
                for index in range(self._list.count()):
                    item = self._list.item(index)
                    if item.data(Qt.ItemDataRole.UserRole) == self._detail_id:
                        self._list.setCurrentItem(item)
                        break
                self._list.blockSignals(False)
                return
            if not self._save_notes_draft():
                self._list.blockSignals(True)
                for index in range(self._list.count()):
                    item = self._list.item(index)
                    if item.data(Qt.ItemDataRole.UserRole) == self._detail_id:
                        self._list.setCurrentItem(item)
                        break
                self._list.blockSignals(False)
                return
            if not self._save_record_notes():
                self._list.blockSignals(True)
                for index in range(self._list.count()):
                    item = self._list.item(index)
                    if item.data(Qt.ItemDataRole.UserRole) == self._detail_id:
                        self._list.setCurrentItem(item)
                        break
                self._list.blockSignals(False)
                return
            self._audio_player.stop()
            self._materials_toggle.setChecked(False)
            self._transcript_highlight = -1
            self._citation_return = None
            self._correcting_speakers = False
            self._status_label.hide()
            self._detail_id = meeting_id
            self._notes_view.restore_reading_position(0)
            self._transcript_view.restore_reading_position(0)
            self._apply_landing_tab_for_meeting(meeting_id, from_list=True)
            self._render_detail()
            if self._detail_find_bar.isVisible() and self._detail_find_edit.text().strip():
                self._find_detail_text(restart=True)

    def _selected_meeting_ids(self) -> list[str]:
        selected = {str(item.data(Qt.ItemDataRole.UserRole) or "") for item in self._list.selectedItems()}
        ordered = []
        for index in range(self._list.count()):
            meeting_id = str(self._list.item(index).data(Qt.ItemDataRole.UserRole) or "")
            if meeting_id and meeting_id in selected:
                ordered.append(meeting_id)
        return ordered

    def _set_batch_mode(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled and not self._batch_mode:
            if not self._flush_bound_title(sync=True) or not self._save_notes_draft() or not self._save_record_notes():
                self._selection_mode_btn.blockSignals(True)
                self._selection_mode_btn.setChecked(False)
                self._selection_mode_btn.blockSignals(False)
                return
            self._audio_player.stop()
        if enabled == self._batch_mode and self._list.batch_mode == enabled:
            self._sync_batch_actions()
            return
        self._batch_mode = enabled
        self._list.batch_mode = enabled
        # A status line belongs to the view that produced it: single-meeting
        # notices make no sense over the batch tools, and vice versa.
        self._set_status("")
        self._selection_mode_btn.blockSignals(True)
        self._selection_mode_btn.setChecked(enabled)
        self._selection_mode_btn.blockSignals(False)
        self._selection_mode_btn.setText("完成" if enabled else "选择")
        self._selection_mode_btn.setAccessibleName("完成选择" if enabled else "选择会议")
        fit_action_button(self._selection_mode_btn)
        if not enabled:
            self._list.blockSignals(True)
            try:
                self._list.clearSelection()
                for index in range(self._list.count()):
                    item = self._list.item(index)
                    if item.data(Qt.ItemDataRole.UserRole) == self._detail_id:
                        item.setSelected(True)
                        self._list.setCurrentItem(item)
                        self._list.scrollToItem(item)
                        break
            finally:
                self._list.blockSignals(False)
        self.refresh()

    def _sync_batch_checks(self, selected_ids: list[str] | None = None) -> None:
        selected = set(self._selected_meeting_ids() if selected_ids is None else selected_ids)
        for index in range(self._list.count()):
            item = self._list.item(index)
            widget = self._list.itemWidget(item)
            box = widget.findChild(_SessionCheckBox, "meetingBatchCheck") if widget is not None else None
            if box is not None:
                meeting_id = str(item.data(Qt.ItemDataRole.UserRole) or "")
                box.blockSignals(True)
                box.setVisible(self._batch_mode)
                box.setChecked(meeting_id in selected)
                box.blockSignals(False)
                box.update()
            if widget is not None and widget.objectName() == "meetingStreamRow":
                _paint_meeting_row(widget, selected=item.isSelected())

    def _sync_batch_summary(self, selected_ids: list[str] | None = None) -> None:
        ids = self._selected_meeting_ids() if selected_ids is None else selected_ids
        count = len(ids)
        if self._batch_mode:
            self._list_summary.setText(f"已选 {count} 场")
        self._list_summary.setToolTip("全选和多选都只包含当前已经加载的会议")

    def _sync_batch_actions(self, selected_ids: list[str] | None = None) -> None:
        ids = self._selected_meeting_ids() if selected_ids is None else selected_ids
        count = len(ids)
        self._batch_export_btn.setText(f"导出 {count} 场" if count else "导出")
        self._batch_generate_btn.setText(f"生成 {count} 场" if count else "生成纪要")
        self._batch_delete_btn.setText(f"删除 {count} 场" if count else "删除")
        for button in (self._batch_export_btn, self._batch_generate_btn, self._batch_delete_btn):
            button.setEnabled(count > 0)
            fit_action_button(button)
        fit_action_button(self._batch_all_btn)
        fit_action_button(self._selection_mode_btn)
        self._batch_tools.setVisible(self._batch_mode)
        self._batch_notice_scroll.setVisible(self._batch_mode)
        self._export_btn.setVisible(not self._batch_mode)
        self._copy_btn.setVisible(not self._batch_mode)
        if not self._batch_mode:
            return
        self._batch_notice.setText(
            f"已选 {count} 场已加载的会议。\n"
            "单击勾选或取消，Shift 连选，Ctrl 加选。\n"
            "全选只包含当前列表里已经加载的会议。\n"
            "导出会写成一个 Markdown。生成纪要只排队还没有纪要的会议。删除无法撤销。"
        )
        self._batch_notice_scroll.updateGeometry()

    def _set_single_detail_visible(self, visible: bool) -> None:
        for widget in (
            self._detail_title, self._more_btn, self._detail_meta, self._state_row,
            self._materials, self._tab_bar, self._tab_tools, self._content_stack,
            self._copy_btn, self._export_btn,
        ):
            widget.setVisible(visible)
        if visible:
            self._batch_tools.hide()
            self._batch_notice_scroll.hide()
        else:
            self._audio_player.stop()
            self._audio_player.hide()
            self._detail_find_bar.hide()
            self._content_hint.hide()
            self._integrity_banner.hide()
            self._empty_speech_bar.hide()

    def _select_loaded_meetings(self) -> None:
        if not self._batch_mode:
            self._set_batch_mode(True)
        self._list.blockSignals(True)
        try:
            for index in range(self._list.count()):
                self._list.item(index).setSelected(True)
        finally:
            self._list.blockSignals(False)
        self._sync_batch_checks()
        self._sync_batch_summary()
        self._sync_batch_actions()

    def _delete_selected_meetings(self) -> None:
        ids = self._selected_meeting_ids()
        records = [self._store.get_meeting(meeting_id) for meeting_id in ids]
        live = [record for record in records if record is not None and record.status == STATUS_LIVE]
        ended_records = [record for record in records if record is not None and record.status != STATUS_LIVE]
        ended = [record.id for record in ended_records]
        if not ended:
            self._set_status("进行中的会议要先结束后再删除。")
            return
        with _quiet_tooltips(self):
            confirmed = confirm_batch_delete(self, len(ended), len(live))
        if not confirmed:
            return
        if not self._flush_bound_title(sync=True) or not self._save_notes_draft() or not self._save_record_notes():
            return
        ended_audio_paths = [record.audio_path for record in ended_records if record.audio_path]
        temp_wav_targets = self._audio_player.collect_temp_wavs_for_audio_paths(ended_audio_paths)
        if self._detail_id in ended:
            self._audio_player.unload_source()
            self._editing_notes = False
            self._notes_edit.clear()
            self._detail_id = ""
            self._drafts.title_meeting_id = ""
        if not self._ctrl.delete_meetings(ended):
            self._set_status("会议删除失败，请重试。")
        else:
            self._audio_player.remove_temp_wavs(temp_wav_targets)
            skipped = f"，{len(live)} 场进行中的未删除" if live else ""
            self._set_status(f"已删除 {len(ended)} 场会议{skipped}")
        self.refresh()

    def _confirm_export_overwrite_if_adjusted(self, raw_path: str, final_path: str) -> bool:
        from sayink.ui.meeting_workspace_chrome import confirm_export_overwrite_if_adjusted

        return confirm_export_overwrite_if_adjusted(self, raw_path, final_path)

    def _export_selected_meetings(self) -> None:
        from sayink.ui.meeting_workspace_chrome import export_selected_meetings

        export_selected_meetings(self)

    def _generate_selected_meetings(self) -> None:
        from sayink.ui.meeting_workspace_chrome import confirm_remote_ai_consent

        ids = self._selected_meeting_ids()
        if not ids:
            return
        if not minutes_settings_complete(self._config):
            self.configure_requested.emit()
            return
        template_id = str(self._config.get("meeting.template_id", "general") or "general")
        include_notes: bool | None = None
        host = self._ctrl.remote_host_needing_confirmation()
        if host:
            ok, include_notes = confirm_remote_ai_consent(
                self, host, is_refinement=self._is_refinement(template_id), batch=True,
            )
            if not ok:
                return
            self._ctrl.confirm_remote_host(host)
        kw = {"include_user_notes": include_notes} if include_notes is not None else {}
        queued, skipped = self._ctrl.enqueue_minutes(ids, template_id=template_id, **kw)
        if queued:
            tail = f"，跳过 {skipped} 场" if skipped else ""
            self._set_status(f"已排队生成 {queued} 场纪要{tail}")
        else:
            self._set_status("没有要生成的会议。已有纪要、正在进行或没有文字稿的会跳过。")

    def _load_more(self) -> None:
        self._list_limit += 100
        self.refresh()

    def show_meeting(self, meeting_id: str) -> None:
        """Open a meeting in the browse view (after it ended, or from a notice)."""
        if (
            self._stack.currentIndex() == PAGE_LIVE
            and not self._ctrl.is_active
            and meeting_id
            and meeting_id == self._record_notes_meeting_id
        ):
            self._show_ended_meeting(meeting_id, unsaved_live=self._has_unsaved_record_notes())
            return
        if not self._flush_bound_title(sync=True) or not self._save_notes_draft() or not self._save_record_notes():
            return
        if meeting_id != self._detail_id:
            self._audio_player.stop()
            self._materials_toggle.setChecked(False)
        self._search_timer.stop()
        self._search_query = ""
        self._search_edit.blockSignals(True)
        self._search_edit.clear()
        self._search_edit.blockSignals(False)
        if meeting_id and self._store.get_meeting(meeting_id) is not None:
            visible = self._query_list_meetings(limit=self._list_limit)
            if all(item.id != meeting_id for item in visible):
                self._list_limit = max(self._list_limit, self._store.count_meetings())
        self._transcript_highlight = -1
        self._citation_return = None
        self._correcting_speakers = False
        self._status_label.hide()
        self._detail_id = meeting_id
        self._notes_view.restore_reading_position(0)
        self._transcript_view.restore_reading_position(0)
        self._apply_landing_tab_for_meeting(meeting_id, from_list=False)
        self._stack.setCurrentIndex(PAGE_BROWSE)
        self._start_btn.setVisible(True)
        if self._batch_mode:
            # Opening a specific meeting (import, notice) leaves batch selection,
            # otherwise the batch view keeps covering the meeting just opened.
            self._set_batch_mode(False)
        if self.is_compact_single_pane and meeting_id:
            self._compact_showing_detail = True
        self.refresh()
        self._sync_media_buttons()

    def show_start_refusal(self, reason: str) -> None:
        """Show why starting a meeting was refused (inline on PAGE_START, else browse banner)."""
        text = (reason or "").strip()
        if not text:
            return
        if self._stack.currentIndex() == PAGE_START:
            self.show_start_error_inline(text)
            return
        self._set_status(
            text if text.startswith("无法开始会议") else f"无法开始会议：{text}",
            tone="error",
        )

    def show_start_error_inline(self, reason: str) -> None:
        """Keep the user on PAGE_START with an inline ``尚未开始录音：{reason}`` banner."""
        self._live_ui.show_start_error_inline(reason)

    def _preferred_tab_for_meeting(self, meeting_id: str) -> int | None:
        if not meeting_id:
            return None
        record = self._store.get_meeting(meeting_id)
        if record is None or record.status == STATUS_LIVE:
            return None
        notes = self._store.get_notes(meeting_id)
        if notes is not None and notes.markdown:
            return 0
        if self._ctrl.generating_for == meeting_id:
            return 0
        job = self._ctrl.queued_generation(meeting_id)
        if job is not None and not job.failed:
            return 0
        if record.source in (IMPORT_SOURCE, MEDIA_SOURCE):
            return 1
        asr_fn = getattr(self._ctrl, "asr_failures", None)
        has_asr_gaps = bool(callable(asr_fn) and asr_fn(meeting_id))
        return 1 if (self._store.get_segments(meeting_id) or has_asr_gaps) else 0

    def _apply_landing_tab_for_meeting(self, meeting_id: str, *, from_list: bool) -> None:
        if self._editing_notes:
            return
        pref = self._preferred_tab_for_meeting(meeting_id)
        if pref is not None:
            if not from_list or pref == 1 or getattr(self, "_tab_auto_switched", False):
                self._switch_tab(pref, _auto=(pref == 1))
        elif getattr(self, "_tab_auto_switched", False):
            self._switch_tab(0, _auto=False)

    def _show_ended_meeting(self, meeting_id: str, *, unsaved_live: bool = False) -> None:
        """Leave the live page once recording ends, even when a note save failed."""
        self._live_silence_timer.stop()
        self.clear_system_silent_warning()
        self._live_new_msg_btn.hide()
        title_saved = self._flush_bound_title(sync=True)
        notes_saved = self._save_notes_draft()
        other_record_unsaved = (
            bool(self._record_notes_meeting_id)
            and self._record_notes_meeting_id != meeting_id
            and not self._save_record_notes()
        )
        if not title_saved or not notes_saved or other_record_unsaved:
            self._stack.setCurrentIndex(PAGE_BROWSE)
            self._start_btn.setVisible(True)
            self.refresh()
            if other_record_unsaved or unsaved_live:
                self._set_status("笔记尚未保存，输入已保留。")
            return
        if not unsaved_live and not self._save_record_notes():
            unsaved_live = True
        self._materials_toggle.setChecked(bool(unsaved_live))
        self._search_timer.stop()
        self._search_query = ""
        self._search_edit.blockSignals(True)
        self._search_edit.clear()
        self._search_edit.blockSignals(False)
        if meeting_id and self._store.get_meeting(meeting_id) is not None:
            visible = self._query_list_meetings(limit=self._list_limit)
            if all(item.id != meeting_id for item in visible):
                self._list_limit = max(self._list_limit, self._store.count_meetings())
        self._transcript_highlight = -1
        self._citation_return = None
        self._correcting_speakers = False
        if not unsaved_live:
            self._status_label.hide()
        self._detail_id = meeting_id
        self._notes_view.restore_reading_position(0)
        self._transcript_view.restore_reading_position(0)
        self._apply_landing_tab_for_meeting(meeting_id, from_list=False)
        self._stack.setCurrentIndex(PAGE_BROWSE)
        self._start_btn.setVisible(True)
        if self._batch_mode:
            self._set_batch_mode(False)
        self.refresh()
        if unsaved_live or self._has_unsaved_record_notes():
            self._set_status("笔记尚未保存，输入已保留。")

    @property
    def current_meeting_id(self) -> str:
        return self._detail_id

    def set_meeting_audio_path(self, meeting_id: str, path: str | Path | None) -> None:
        """Associate a local audio file or recovery directory with ``meeting_id``."""
        if not meeting_id:
            return
        resolved = str(path or "").strip()
        no_audio: set[str] = getattr(self, "_no_audio_meeting_ids", set())
        no_audio.discard(meeting_id)
        if resolved:
            self._meeting_audio_paths[meeting_id] = resolved
        else:
            self._meeting_audio_paths.pop(meeting_id, None)
        if self._detail_id == meeting_id and not self._batch_mode:
            self._render_detail()

    register_meeting_audio = set_meeting_audio_path

    def _resolve_meeting_audio_path(self, record: MeetingRecord | None) -> str:
        no_audio = getattr(self, "_no_audio_meeting_ids", None)
        if no_audio is None:
            no_audio = set()
            self._no_audio_meeting_ids = no_audio
        return resolve_meeting_audio_path(
            record,
            meeting_audio_paths=self._meeting_audio_paths,
            no_audio_ids=no_audio,
            recovery_audio_cache=self._recovery_audio_cache,
            config=self._config,
        )

    def _play_empty_meeting_audio(self) -> None:
        if self._audio_player.audio_path:
            self._audio_player.show()
            self._audio_player.play()

    def _clear_detail(self, *, clear_status: bool = True) -> None:
        if not self._flush_bound_title(sync=True) or not self._save_notes_draft() or not self._save_record_notes():
            return
        self._audio_player.unload_source()
        self._audio_player.hide()
        self._detail_find_bar.hide()
        self._integrity_banner.hide()
        self._empty_speech_bar.hide()
        self._materials.hide()
        self._record_notes.blockSignals(True)
        self._record_notes.clear()
        self._record_notes.blockSignals(False)
        self._record_marks.clear()
        self._drafts.reset_record_notes()
        self._drafts.title_meeting_id = ""
        self._detail_title.clear()
        self._detail_title.setPlaceholderText("会议标题")
        self._audio_missing.hide()
        if hasattr(self, "_time_action"):
            self._time_action.setEnabled(False)
        self._detail_meta.clear()
        self._notes_state.clear()
        self._paint_notes_state()
        self._content_hint.hide()
        self._return_notes_btn.hide()
        self._cancel_notes_edit_btn.hide()
        self._notes_view.clear()
        self._notes_empty.clear()
        self._notes_edit.clear()
        self._transcript_view.clear()
        self._speaker_menu.clear()
        if clear_status:
            self._status_label.hide()
        for widget in (self._generate_btn, self._edit_btn, self._copy_btn, self._transcript_copy_btn, self._export_btn,
                       self._more_btn, self._speaker_btn, self._correct_speaker_btn, self._template_combo, self._detail_title):
            widget.setEnabled(False)
        from sayink.ui import settings_styles as ss
        self._start_btn.setStyleSheet(ss.BTN_ACCENT_SM)

    def _render_detail(self) -> None:
        importing = bool(getattr(self._ctrl, "importing_media", False))
        if importing and self._import_owns_detail:
            self._show_import_detail()
            return
        if importing:
            self._paint_import_banner()
        else:
            self._hide_import_chrome()
        self._detail_title.setReadOnly(False)
        if self._batch_mode:
            self._set_single_detail_visible(False)
            self._sync_batch_actions()
            return
        self._set_single_detail_visible(True)
        record = self._store.get_meeting(self._detail_id) if self._detail_id else None
        if record is None:
            self._clear_detail()
            return
        for widget in (self._generate_btn, self._edit_btn, self._copy_btn, self._transcript_copy_btn, self._export_btn,
                       self._more_btn, self._speaker_btn, self._template_combo, self._detail_title):
            widget.setEnabled(True)
        if self._drafts.title_meeting_id and self._drafts.title_meeting_id != record.id:
            self._flush_bound_title(sync=True)
        self._drafts.title_meeting_id = record.id
        pending = self._drafts.pending_title
        if pending and pending[0] == self._detail_id and pending[1] != record.title:
            if self._detail_title.text() != pending[1]:
                self._detail_title.setText(pending[1])
                if not self._detail_title.hasFocus():
                    self._detail_title.setCursorPosition(0)
        elif not self._detail_title.hasFocus():
            self._detail_title.setText(record.title)
            self._detail_title.setCursorPosition(0)
            self._drafts.clear_pending_title_for(self._detail_id)
        self._detail_title.setPlaceholderText(record.display_title)
        meta = [_format_dt(record.started_at)]
        if record.ended_at:
            meta.append(_duration_label(record.duration_ms))
        if record.source == IMPORT_SOURCE:
            meta.append("导入文字稿")
        elif record.source == MEDIA_SOURCE:
            meta.append("导入音视频")
            if record.status == STATUS_INTERRUPTED:
                meta.append("已中断")
        elif record.status in _STATUS_TEXT:
            meta.append(_STATUS_TEXT[record.status])
        self._detail_meta.setText(" · ".join(meta))
        self._show_materials(record)
        audio_path = self._resolve_meeting_audio_path(record) if record.status != STATUS_LIVE else ""
        has_audio = self._audio_player.load_audio(audio_path, fallback_duration_ms=record.duration_ms)
        self._audio_player.setVisible(has_audio)
        stored_audio = str(getattr(record, "audio_path", "") or "").strip()
        self._audio_missing.setVisible(record.source == MEDIA_SOURCE and bool(stored_audio) and not has_audio)
        self._time_action.setEnabled(record.status != STATUS_LIVE)

        notes = self._store.get_notes(self._detail_id)
        if notes is not None and notes.user_edited and notes.markdown:
            self._drafts.user_markdown_backups[self._detail_id] = notes.markdown
        generating = self._ctrl.generating_for == self._detail_id
        job = None if generating else self._ctrl.queued_generation(self._detail_id)
        waiting = job is not None and not job.failed
        retry_now = waiting and job.not_before > now_ms()
        blocked = generating or (waiting and not retry_now)
        from sayink.ui import settings_styles as ss

        has_notes = notes is not None and bool(notes.markdown)
        self._start_btn.setStyleSheet(ss.BTN_GHOST_SM)
        configured = minutes_settings_complete(self._config)
        if not configured:
            self._generate_btn.setText("配置 AI 接口")
            self._generate_btn.setAccessibleName("配置 AI 接口")
            self._generate_btn.setToolTip("打开 设置 → AI 服务，填写接口地址、密钥与模型")
        else:
            self._generate_btn.setText("重新生成" if has_notes else "生成纪要")
            self._generate_btn.setAccessibleName(self._generate_btn.text())
            self._generate_btn.setToolTip(
                "另一场会议正在生成；点击后排队，它完成后自动开始"
                if self._ctrl.generating_for and not generating and not waiting else ""
            )
        self._generate_btn.setStyleSheet(ss.BTN_GHOST_SM if has_notes and configured else ss.BTN_ACCENT_SM)
        self._generate_btn.setEnabled(not blocked and record.status != STATUS_LIVE)
        self._restore_action.setEnabled(not generating and notes is not None and bool(notes.previous_markdown))
        self._restore_action.setToolTip("" if self._restore_action.isEnabled() else "还没有上一版可恢复")
        user_backup = self._available_user_backup(self._detail_id, notes)
        self._restore_user_action.setEnabled(not generating and bool(user_backup))
        self._restore_user_action.setToolTip(
            "找回重新生成前的手动修改版本" if self._restore_user_action.isEnabled() else "尚无被覆盖的手动修改版"
        )
        self._cancel_btn.setVisible(generating or waiting)
        self._cancel_btn.setText("取消" if generating else "取消排队")
        default_template = notes.template_id if notes is not None and notes.template_id else self._config.get("meeting.template_id", "general")
        template_id = self._drafts.effective_template_id(self._detail_id, default_template)
        index = self._template_combo.findData(template_id)
        self._template_combo.blockSignals(True)
        if index >= 0:
            self._template_combo.setCurrentIndex(index)
        self._template_combo.blockSignals(False)
        self._template_combo.setEnabled(not blocked)
        self._manage_templates_btn.setEnabled(not self._ctrl.generating_for)
        self._sync_result_actions(notes)
        if (generating or waiting) and configured:
            if generating:
                busy = "整理中…" if self._is_refinement() else "生成中…"
            else:
                busy = "立即重试" if retry_now else "排队中…"
            self._generate_btn.setText(busy)
            self._generate_btn.setAccessibleName(busy)
        markdown = notes.markdown if notes is not None else ""
        self._cancel_notes_edit_btn.setVisible(self._editing_notes)
        if self._editing_notes:
            self._notes_stack.setCurrentWidget(self._notes_edit)
            self._edit_btn.setText("保存修改")
        else:
            self._notes_stack.setCurrentWidget(self._notes_view)
            self._edit_btn.setText("编辑正文")
            if generating and self._ctrl.generation_preview_text:
                self._notes_view.set_reading_plain(self._ctrl.generation_preview_text)
            elif markdown and notes is not None and self._is_refinement(notes.template_id):
                self._notes_view.set_reading_html("".join(
                    "<p>" + html.escape(p).replace("\n", "<br>") + "</p>"
                    for p in markdown.strip().split("\n\n") if p.strip()
                ))
            elif markdown:
                self._notes_view.set_reading_markdown(
                    link_citations(notes_for_display(markdown, record.display_title))
                )
            else:
                self._notes_view.clear()
                if record.status == STATUS_LIVE:
                    self._notes_view.setPlaceholderText("会议进行中，结束后可生成纪要或整理原文。")
                elif not configured:
                    self._notes_view.setPlaceholderText(
                        f"还没有{self._result_label(notes)}。点击「配置 AI 接口」填写接口地址、密钥和模型，无需开启文字润色。"
                    )
                else:
                    self._notes_view.setPlaceholderText(
                        "融合多人发言，保留细节与个人语气，整理成通顺的书面文章。点击「整理原文」开始。"
                        if self._is_refinement() else "还没有纪要。选择模板后点击「生成纪要」。"
                    )
                self._notes_empty.setText(self._notes_view.placeholderText())
                self._notes_stack.setCurrentWidget(self._notes_empty)
        self._edit_btn.setEnabled(not generating and (bool(markdown) or self._editing_notes))
        if not self._edit_btn.isEnabled():
            self._edit_btn.setToolTip(f"还没有{self._result_label(notes)}可编辑")
        self._sync_notes_state(notes)

        segments = self._store.get_segments(self._detail_id)
        names = self._ctrl.speaker_names(self._detail_id)
        lines = transcript_lines(segments, record.started_at)
        self._transcript_has_speakers, self._transcript_has_times = self._ctrl.transcript_shape(record, lines)
        self._transcript_offsets = [line.offset_ms for line in lines]
        self._transcript_time_known = [line.time_known for line in lines]
        self._transcript_sequences = [s.seq for s in segments if (s.corrected_text or s.polished_text or s.raw_text).strip()]
        self._corrected_sequences = {s.seq for s in segments if s.corrected_text}
        self._speaker_locked_sequences = {s.seq for s in segments if s.speaker_locked}
        can_correct = record.status in (STATUS_ENDED, STATUS_INTERRUPTED) and not generating and bool(lines)
        self._transcript_editable = can_correct
        self._correct_speaker_btn.setEnabled(can_correct)
        self._correcting_speakers = self._correcting_speakers and can_correct
        self._correct_speaker_btn.blockSignals(True)
        self._correct_speaker_btn.setChecked(self._correcting_speakers)
        self._correct_speaker_btn.blockSignals(False)
        self._correct_speaker_btn.setText("完成纠正" if self._correcting_speakers else "纠正发言人")
        if self._transcript_highlight >= len(lines):
            self._transcript_highlight = -1
        self._transcript_view.set_reading_html(
            self._transcript_html(
                lines, names, links=True, highlight=self._transcript_highlight,
                show_speakers=self._transcript_has_speakers, show_times=self._transcript_has_times,
                label_unknown=record.source != IMPORT_SOURCE,
                playable=has_audio,
            )
        )
        speaker_ids = sorted({int(s.speaker_id) for s in segments if int(s.speaker_id) > 0})
        self._speaker_menu.clear()
        for speaker_id in speaker_ids:
            label = names.get(speaker_id) or f"说话人{speaker_id}"
            action = self._speaker_menu.addAction(label)
            action.triggered.connect(lambda _=False, sid=speaker_id: self._prompt_rename_speaker(sid))
        self._speaker_btn.setVisible(bool(speaker_ids))
        self._speaker_btn.setEnabled(not generating)
        from sayink.ui.meeting_workspace_chrome import sync_detail_integrity_and_empty_state

        sync_detail_integrity_and_empty_state(
            self, record, segments,
            has_audio=has_audio, configured=configured, markdown=markdown, generating=generating,
        )
        self._sync_copy_action(notes)
        self._sync_content_hint(notes)

    def _on_template_changed(self, index: int) -> None:
        self._template_combo.setToolTip(str(self._template_combo.itemData(index, Qt.ItemDataRole.ToolTipRole) or ""))
        if self._detail_id:
            self._drafts.set_pending_template(self._detail_id, str(self._template_combo.currentData() or "general"))
            self._render_detail()
        self._sync_template_menu()

    def _is_refinement(self, template_id: str | None = None) -> bool:
        return get_template(template_id or str(self._template_combo.currentData() or "general"), self._custom_templates()).is_refinement

    _NOTES_UNSET = object()

    def _resolve_notes(self, notes: object = _NOTES_UNSET):
        if notes is self._NOTES_UNSET:
            return self._store.get_notes(self._detail_id) if self._detail_id else None
        return notes

    def _result_label(self, notes: object = _NOTES_UNSET) -> str:
        resolved = self._resolve_notes(notes)
        template_id = resolved.template_id if resolved is not None and resolved.markdown else None
        return "精整正文" if self._is_refinement(template_id) else "纪要"

    def _sync_result_actions(self, notes) -> None:
        label = self._result_label(notes)
        has_notes = notes is not None and bool(notes.markdown)
        if minutes_settings_complete(self._config):
            if self._is_refinement():
                action = "重新整理" if has_notes and self._is_refinement(notes.template_id) else "整理原文"
            else:
                action = "重新生成" if has_notes else "生成纪要"
            self._generate_btn.setText(action)
            self._generate_btn.setAccessibleName(action)
        self._template_combo.setToolTip(str(self._template_combo.currentData(Qt.ItemDataRole.ToolTipRole) or ""))
        self._tab_notes.setText(label)
        self._notes_view.setAccessibleName("会议" + label)
        self._notes_edit.setAccessibleName("编辑会议" + label)
        self._edit_btn.setToolTip("直接修改" + label + "文本")
        self._export_actions["notes"].setText("仅导出" + label)
        previous_label = label
        if notes is not None and notes.previous_metadata:
            try:
                metadata = json.loads(notes.previous_metadata)
                if isinstance(metadata, dict):
                    previous_label = "精整正文" if self._is_refinement(metadata.get("template_id", "general")) else "纪要"
            except (ValueError, TypeError):
                pass
        self._restore_action.setText("恢复上一版" + previous_label)

    def _sync_notes_state(self, notes) -> None:
        if self._ctrl.generating_for == self._detail_id:
            progress = self._status_label.text().strip()
            if not progress:
                progress = "正在整理原文…" if self._is_refinement() else "正在生成纪要…"
            self._notes_state.setText(progress)
            running = "整理" if self._is_refinement() else "生成"
            self._notes_state.setToolTip(
                f"{running}进行中。这里显示的是尚未保存的预览；复制和导出仍用上一版，点「取消」可停下。"
            )
            self._status_label.hide()
            self._paint_notes_state()
            return
        result_label = self._result_label(notes)
        if self._editing_notes:
            state = "正在编辑 · 尚未保存"
        elif notes is None or not notes.markdown:
            state = "尚未整理原文" if self._is_refinement() else "尚未生成纪要"
        else:
            state = "已手动编辑" if notes.user_edited else "AI 生成"
        if notes is not None and notes.transcript_changed:
            state += f" · 文字稿已更正，{result_label}需核对，建议重新生成"
        elif notes is not None and "（待核对）" in notes.markdown:
            state += " · 含待核对内容"
        job = self._ctrl.queued_generation(self._detail_id) if self._detail_id else None
        if job is not None:
            state += " · " + self._queue_state(job)
        self._notes_state.setText(state)
        if job is not None:
            self._notes_state.setToolTip(
                "自动生成没有成功，不会再自动重试；修正问题后可手动生成。" if job.failed else
                "多场会议逐场生成，手动点的排在会后自动生成的前面；点「取消排队」可移出队列。"
            )
        else:
            self._notes_state.setToolTip(
                "保留细节与语气的 AI 整理稿，可切换到文字稿核对；手动编辑不等于内容已经核实。"
                if result_label == "精整正文" else
                "纪要根据文字稿生成；点击引用可核对来源。手动编辑不等于内容已经核实。"
            )
        self._paint_notes_state()

    def _queue_state(self, job: QueuedGeneration) -> str:
        reason = job.last_error.rstrip("。")
        if job.failed:
            return f"自动生成失败：{reason}"
        if job.not_before > now_ms():
            at = _safe_dt(job.not_before).strftime("%H:%M")
            return f"自动生成暂时失败（{reason}），{at} 自动重试"
        if job.automatic and self._ctrl.defers_automatic_jobs():
            return "会议进行中，本机模型空闲后自动生成"
        position = self._ctrl.queue_position(job.meeting_id)
        return f"排队中 · 第 {position} 位，前面的完成后自动开始" if position else "排队中"

    def _paint_notes_state(self) -> None:
        from sayink.ui import design_tokens as tok

        generating = bool(self._detail_id) and self._ctrl.generating_for == self._detail_id
        row = self._state_row.layout()
        if generating:
            self._state_row.setStyleSheet(
                f"QWidget#meetingState {{ background: {tok.SURFACE_PEARL}; border: 1px solid {tok.HAIRLINE};"
                f" border-radius: {tok.RADIUS_MD}px; }}"
            )
            self._notes_state.setStyleSheet(
                f"color: {tok.TEXT}; font-size: {tok.TYPE_BODY_SM}px; font-weight: 700;"
                f" background: transparent; border: none; padding: 0;"
            )
            row.setContentsMargins(10, 4, 6, 4)
            return
        self._state_row.setStyleSheet("QWidget#meetingState { background: transparent; border: none; }")
        self._notes_state.setStyleSheet(
            f"color: {tok.TEXT_SEC}; font-size: {tok.TYPE_FOOTNOTE}px; background: transparent; border: none; padding: 0;"
        )
        row.setContentsMargins(0, 0, 0, 0)

    def _sync_content_hint(self, notes: object = _NOTES_UNSET) -> None:
        if self._batch_mode:
            # The hint describes the open meeting, which batch mode hides.
            self._content_hint.hide()
            return
        transcript = self._content_stack.currentIndex() == 1
        self._return_notes_btn.setVisible(transcript and bool(self._citation_return and self._citation_return[0] == self._detail_id))
        if transcript:
            hint = ("点击发言旁的「更正」修改这一段的归属。" if self._correcting_speakers else
                    "移到段落上，或用方向键移到该段按 Enter 可编辑、查看详情；点击姓名可命名。"
                    if self._transcript_editable else
                    "正在生成，完成前不能修改文字稿。" if self._ctrl.generating_for == self._detail_id else
                    "有可编辑的段落后，可以把鼠标移到段落上修改。")
        elif self._editing_notes:
            hint = "复制与导出使用当前草稿。切换会议或离开本页时自动保存。"
        elif self._ctrl.generating_for == self._detail_id:
            hint = "生成预览尚未保存；复制和导出仍使用上一版。点「取消」可停下。"
        else:
            resolved = self._resolve_notes(notes)
            if resolved is not None and resolved.markdown and self._template_combo.currentData() != resolved.template_id:
                hint = f"下次生成将使用「{self._template_combo.currentText()}」；当前{self._result_label(resolved)}保持原模板。"
            elif self._is_refinement():
                hint = "融合成文，保留细节与口吻；重要内容请核对文字稿。"
            else:
                hint = ""
        self._content_hint.setText(hint)
        self._content_hint.setVisible(bool(hint))

    def _current_notes_text(self, notes: object = _NOTES_UNSET) -> str:
        if self._editing_notes:
            return self._notes_edit.toPlainText()
        resolved = self._resolve_notes(notes)
        return resolved.markdown if resolved is not None else ""

    def _sync_copy_action(self, notes: object = _NOTES_UNSET) -> None:
        if not hasattr(self, "_transcript_view"):
            return
        from sayink.ui import settings_styles as ss

        resolved = self._resolve_notes(notes)
        result_label = self._result_label(resolved)
        label = "复制草稿" if self._editing_notes else "复制" + result_label
        self._copy_btn.setText(label)
        self._copy_btn.setAccessibleName(label)
        shortcut = "" if self._editing_notes else " · Ctrl+C"
        self._copy_btn.setToolTip(label + ("" if result_label == "精整正文" else " Markdown") + shortcut)
        has_notes = bool(self._current_notes_text(resolved).strip())
        has_transcript = bool(self._detail_id and self._transcript_offsets)
        self._copy_btn.setEnabled(has_notes)
        if not has_notes:
            self._copy_btn.setToolTip(f"还没有{result_label}可复制")
        self._transcript_copy_btn.setEnabled(has_transcript)
        # Same filled action as 历史's copy button. When disabled, keep a muted
        # bordered pill instead of collapsing into floating plain text.
        disabled_sm = self._disabled_btn_sm_override()
        self._copy_btn.setStyleSheet(
            ss.BTN_ACCENT_SM if has_notes else ss.BTN_ACCENT_SM + disabled_sm
        )
        self._transcript_copy_btn.setStyleSheet(
            ss.BTN_ACCENT_SM if has_transcript else ss.BTN_ACCENT_SM + disabled_sm
        )
        self._fit_template_cluster()
        self._export_actions["notes"].setEnabled(has_notes)
        self._export_actions["transcript"].setEnabled(has_transcript)
        if "srt" in self._export_actions:
            self._export_actions["srt"].setEnabled(has_transcript and self._transcript_has_times)
        self._export_actions["all"].setEnabled(bool(self._detail_id))

    def _transcript_html(
        self, lines, names: dict[int, str], *, links: bool = False, highlight: int = -1, live: bool = False,
        show_speakers: bool = True, show_times: bool = True, label_unknown: bool = True, playable: bool = False,
    ) -> str:
        from sayink.ui.meeting_workspace_chrome import splice_asr_gaps_into_html

        meeting_id = self._ctrl.current_id if live else self._detail_id
        sequences = self._live_transcript_sequences if live else self._transcript_sequences
        locked = self._live_locked_sequences if live else self._speaker_locked_sequences
        correcting = self._live_correcting_speakers if live else self._correcting_speakers
        auto_names = {
            s.speaker_id for s in self._store.get_speakers(meeting_id)
            if s.suggested_name and not s.display_name and not s.name_locked
        } if links and meeting_id else set()
        base_html = build_transcript_html(
            lines,
            names,
            links=links,
            highlight=highlight,
            live=live,
            sequences=sequences,
            corrected_sequences=self._corrected_sequences,
            locked_sequences=locked,
            correcting=correcting,
            editable=self._transcript_editable,
            live_editable=self._ctrl.state in (MeetingState.LIVE, MeetingState.PAUSED),
            auto_names=auto_names,
            show_speakers=show_speakers,
            show_times=show_times,
            label_unknown=label_unknown,
            playable=playable,
        )
        rec = self._ctrl.current if live else (self._store.get_meeting(meeting_id) if meeting_id else None)
        gap_fn = getattr(self._ctrl, "asr_gap_lines", None)
        gaps = gap_fn(meeting_id, rec.started_at if rec is not None else 0) if (callable(gap_fn) and meeting_id and rec is not None) else []
        return splice_asr_gaps_into_html(base_html, lines, gaps)

    def _on_notes_link(self, url) -> None:
        if url.scheme() == "ts" and url.path().isdigit():
            self._jump_to_transcript(int(url.path()) * 1000)
        elif url.scheme() in ("http", "https"):
            QDesktopServices.openUrl(url)

    def _jump_to_transcript(self, offset_ms: int) -> None:
        """Show the transcript line a minutes citation or mark points at."""
        if not self._transcript_has_times:
            self._set_status("这场文字稿没有时间戳，无法跳转", transient=True)
            return
        if not self._transcript_offsets:
            return
        timed_indices = [
            idx for idx in range(len(self._transcript_offsets))
            if idx >= len(self._transcript_time_known) or self._transcript_time_known[idx]
        ]
        if not timed_indices:
            self._set_status("这一段没有时间戳，无法跳转", transient=True)
            return
        timed_offsets = [self._transcript_offsets[idx] for idx in timed_indices]
        is_monotonic = all(
            timed_offsets[i] >= timed_offsets[i - 1] for i in range(1, len(timed_offsets))
        )
        target_limit = offset_ms + 999
        if is_monotonic:
            pos = bisect.bisect_right(timed_offsets, target_limit) - 1
            index = timed_indices[pos] if pos >= 0 else timed_indices[0]
        else:
            le_candidates = [
                (abs(offset_ms - self._transcript_offsets[idx]), idx)
                for idx in timed_indices
                if self._transcript_offsets[idx] <= target_limit
            ]
            if le_candidates:
                _, index = min(le_candidates)
            else:
                _, index = min(
                    (abs(self._transcript_offsets[idx] - offset_ms), idx)
                    for idx in timed_indices
                )
        if self._content_stack.currentIndex() == 0:
            self._citation_return = (self._detail_id, self._notes_view.verticalScrollBar().value())
        self._transcript_highlight = index
        self._switch_tab(1)
        self._render_detail()
        self._transcript_view._reading_timer.stop()
        self._transcript_view.scrollToAnchor(f"seg{index}")

    def _return_to_notes(self) -> None:
        if self._citation_return is None or self._citation_return[0] != self._detail_id:
            return
        _, value = self._citation_return
        self._switch_tab(0)
        self._notes_view.restore_reading_position(value)

    def _hide_status_banner(self) -> None:
        self._status_label.hide()
        self._pending_glossary_suggestion = None
        if hasattr(self, "_glossary_learn_btn"):
            self._glossary_learn_btn.hide()

    def _set_status(
        self,
        text: str,
        *,
        transient: bool = False,
        tone: str = "info",
        learn_glossary: bool = False,
        duration_ms: int = 4000,
    ) -> None:
        from sayink.ui import settings_styles as ss

        self._status_timer.stop()
        if not learn_glossary:
            self._pending_glossary_suggestion = None
        self._status_tone = tone if text else "info"
        self._status_label.setText(text)
        self._status_label.setVisible(bool(text))
        if hasattr(self, "_glossary_learn_btn"):
            self._glossary_learn_btn.setStyleSheet(ss.BTN_ACCENT_SM)
            self._glossary_learn_btn.setVisible(bool(text and learn_glossary))
        self._paint_status_label()
        if text and transient:
            self._status_timer.start(duration_ms)

    @property
    def pending_glossary_suggestion(self) -> tuple[str, str] | None:
        """Latest ``(term, variant)`` candidate detected from a manual segment correction."""
        return self._pending_glossary_suggestion

    def accept_glossary_suggestion(self) -> bool:
        """Persist the pending ``(term, variant)`` suggestion into ``stt.glossary``."""
        suggestion = self._pending_glossary_suggestion
        self._pending_glossary_suggestion = None
        if hasattr(self, "_glossary_learn_btn"):
            self._glossary_learn_btn.hide()
        if suggestion is None or self._config is None:
            return False
        from sayink.asr_correction import (
            format_glossary_suggestion_label,
            persist_glossary_learning,
        )

        term, variant = suggestion
        if not persist_glossary_learning(self._config, term, variant):
            return False
        self.glossary_updated.emit()
        label = format_glossary_suggestion_label(term, variant)
        if self._stack.currentIndex() == PAGE_LIVE:
            self._live_feedback_timer.stop()
            self._live_speaker_feedback.setText(f"已加入自定义词汇：{label}")
            self._live_speaker_feedback.show()
            self._live_feedback_timer.start(4000)
        else:
            self._set_status(f"已加入自定义词汇：{label}", transient=True, tone="ok")
        return True

    def apply_segment_correction(
        self,
        meeting_id: str,
        seq: int,
        corrected_text: str | None,
    ) -> bool:
        """Public contract method to save a segment text edit and compute glossary suggestions."""
        record = self._store.get_meeting(meeting_id) if meeting_id else None
        if record is None:
            return False
        get_seg = getattr(self._store, "get_segment", None)
        segment = (
            get_seg(record.id, seq)
            if callable(get_seg)
            else next((s for s in self._store.get_segments(record.id) if s.seq == seq), None)
        )
        if segment is None:
            return False
        before_text = (segment.corrected_text or segment.polished_text or segment.raw_text or "").strip()
        if not self._ctrl.save_segment_text(record.id, seq, corrected_text):
            return False
        self._post_segment_text_saved(record, seq, before_text=before_text, corrected_text=corrected_text)
        return True

    def _post_segment_text_saved(
        self,
        record: MeetingRecord,
        seq: int,
        *,
        before_text: str,
        corrected_text: str | None,
    ) -> None:
        action = "已恢复这一段的原文" if corrected_text is None else "已保存这一段的修改"
        suggestion: tuple[str, str] | None = None
        if corrected_text is not None and self._config is not None:
            from sayink.asr_correction import extract_learned_correction_pair

            suggestion = extract_learned_correction_pair(
                before_text,
                corrected_text,
                self._config.get("stt.glossary", []) or [],
            )
        self._pending_glossary_suggestion = suggestion
        if self._stack.currentIndex() == PAGE_LIVE and self._ctrl.current_id == record.id:
            self._render_live()
            self._live_transcript.place_cursor_at_segment(seq)
            self._live_feedback_timer.stop()
            msg = action + "。"
            if suggestion is not None:
                from sayink.asr_correction import format_glossary_suggestion_label

                msg = f"{action} · 可将「{format_glossary_suggestion_label(*suggestion)}」加入词典"
            self._live_speaker_feedback.setText(msg)
            self._live_speaker_feedback.show()
            self._live_feedback_timer.start(5000)
            return
        self._render_detail()
        self._transcript_view.place_cursor_at_segment(seq)
        notes = self._store.get_notes(record.id)
        hint = f"；已有{self._result_label()}保留，建议重新生成。" if notes is not None and notes.markdown else "。"
        if suggestion is not None:
            from sayink.asr_correction import format_glossary_suggestion_label

            label = format_glossary_suggestion_label(*suggestion)
            self._set_status(
                f"{action}{hint} 识别出改词「{label}」",
                transient=True,
                learn_glossary=True,
                duration_ms=6000,
            )
        else:
            self._set_status(action + hint, transient=True)

    def _paint_status_label(self) -> None:
        tone = getattr(self, "_status_tone", "info")
        if tone == "error":
            color, bg, border = tok.RED, tok.RED_BG, tok.RED
        elif tone == "ok":
            color, bg, border = tok.GREEN_TEXT, tok.GREEN_BG, tok.GREEN_BORDER
        else:
            color, bg, border = tok.TEXT, tok.SURFACE_PEARL, tok.HAIRLINE
        self._status_label.setStyleSheet(
            f"color: {color}; font-size: {tok.TYPE_FOOTNOTE}px; background: {bg};"
            f" border: 1px solid {border}; border-radius: {tok.RADIUS_MD}px; padding: 6px 10px;"
        )

    def _edit_meeting_time(self) -> None:
        record = self._store.get_meeting(self._detail_id) if self._detail_id else None
        if record is None or record.status == STATUS_LIVE:
            self._set_status("进行中的会议要结束后才能改时间。")
            return
        dialog = _MeetingTimeDialog(int(record.started_at), self)
        with _quiet_tooltips(self):
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
        if not accepted:
            return
        self._store.shift_meeting_start(record.id, dialog.started_ms)
        saved = self._store.get_meeting(record.id) if self._store.flush() else None
        if saved is None or int(saved.started_at) != int(dialog.started_ms):
            self._set_status("会议时间没能保存。", tone="error")
            return
        self.refresh()
        self._set_status("已修改会议时间。", tone="ok")

    # ── detail actions ────────────────────────────────────────────

    def _flush_bound_title(self, *, sync: bool = False) -> bool:
        """Persist ``_detail_title`` to the meeting it was loaded for.

        Prevents switching meetings or leaving the page from dropping typed
        title text or writing one meeting's title onto another.
        """
        if not hasattr(self, "_detail_title") or self._detail_title.isReadOnly():
            return True
        meeting_id = self._drafts.title_meeting_id
        if not meeting_id:
            return True
        record = self._store.get_meeting(meeting_id) or self._records.get(meeting_id)
        if record is None:
            return True
        title = self._detail_title.text().strip()
        if not self._detail_title.hasFocus():
            self._detail_title.setCursorPosition(0)
        pending = self._drafts.pending_title
        if title == record.title:
            if not (pending and pending[0] == meeting_id and pending[1] != title):
                self._drafts.clear_pending_title_for(meeting_id)
                return True
        if sync:
            if pending == (meeting_id, title):
                if not self._store.flush():
                    self._set_status("标题尚未保存，输入已保留。")
                    return False
                saved = self._store.get_meeting(meeting_id)
                if saved is None or saved.title != title:
                    self._set_status("标题尚未保存，输入已保留。")
                    return False
                self._drafts.clear_pending_title_for(meeting_id)
                return True
            self._ctrl.update_meeting(meeting_id, title=title)
            saved = self._store.get_meeting(meeting_id) if self._store.flush() else None
            if saved is None or saved.title != title:
                self._set_status("标题尚未保存，输入已保留。")
                return False
            self._drafts.clear_pending_title_for(meeting_id)
            if meeting_id in self._records:
                self._records[meeting_id] = saved
            return True

        if pending == (meeting_id, title):
            return True
        token = self._drafts.begin_title_save(meeting_id, title)
        history = getattr(self._store, "_history", None)
        use_history = history is not None and not getattr(self._store, "disabled", False)
        enqueued = self._drafts.enqueue_tracked(
            history if use_history else None,
            lambda: self._ctrl.update_meeting(meeting_id, title=title),
        )
        if not enqueued:
            saved = self._store.get_meeting(meeting_id)
            if saved is None or saved.title != title:
                if self._detail_id == meeting_id:
                    self._render_detail()
                self._set_status("标题尚未保存，输入已保留。")
                return False
            self._drafts.pending_title = None
            self.refresh()
            return True
        weak_self = weakref.ref(self)
        history.enqueue_write(
            lambda _conn: (page := weak_self()) is not None and page._title_write_settled.emit(meeting_id, title, token)
        )
        self.refresh()
        return True

    def _save_title(self) -> None:
        self._flush_bound_title(sync=False)

    def _on_title_write_settled(self, meeting_id: str, title: str, token: int) -> None:
        if not self._drafts.matches_pending_title(meeting_id, title, token):
            return
        saved = self._store.get_meeting(meeting_id)
        if saved is None or saved.title != title:
            if self._detail_id == meeting_id:
                self._render_detail()
            self._set_status("标题尚未保存，输入已保留。")
            return
        self._drafts.pending_title = None
        self.refresh()

    def _on_history_write_failed(self) -> None:
        pending = self._drafts.pending_title
        if not pending:
            return
        meeting_id, title = pending
        saved = self._store.get_meeting(meeting_id)
        if saved is None or saved.title != title:
            if self._detail_id == meeting_id:
                self._render_detail()
            self._set_status("标题尚未保存，输入已保留。")

    def _generate(self) -> None:
        if not self._detail_id or self._ctrl.generating_for == self._detail_id:
            return
        job = self._ctrl.queued_generation(self._detail_id)
        if job is not None and not job.failed and job.not_before <= now_ms():
            return
        if not minutes_settings_complete(self._config):
            self.configure_requested.emit()
            return
        if self._editing_notes:
            self._toggle_edit()
            if self._editing_notes:
                return
        if not self._confirm_regenerate():
            return
        notes_before = self._store.get_notes(self._detail_id)
        if notes_before is not None and notes_before.user_edited and notes_before.markdown:
            self._drafts.user_markdown_backups[self._detail_id] = notes_before.markdown
        from sayink.ui.meeting_workspace_chrome import confirm_remote_ai_consent

        include_notes: bool | None = None
        host = self._ctrl.remote_host_needing_confirmation()
        if host:
            ok, include_notes = confirm_remote_ai_consent(
                self, host, is_refinement=self._is_refinement(), batch=False,
            )
            if not ok:
                return
            self._ctrl.confirm_remote_host(host)
        template_id = str(self._template_combo.currentData() or "general")
        kw = {"include_user_notes": include_notes} if include_notes is not None else {}
        self._ctrl.generate(self._detail_id, template_id=template_id, **kw)

    def _confirm_regenerate(self) -> bool:
        notes = self._store.get_notes(self._detail_id)
        if notes is None or not notes.markdown:
            return True
        box = QMessageBox(self)
        label = self._result_label()
        box.setWindowTitle("重新整理原文" if self._is_refinement() else "重新生成纪要")
        box.setIcon(QMessageBox.Icon.Question)
        if notes.user_edited:
            box.setText(f"当前{label}包含你的手动修改，重新生成会替换它。")
        else:
            box.setText(f"重新生成会替换当前{label}。")
        box.setInformativeText(
            f"将使用「{self._template_combo.currentText()}」重新生成。"
            f"替换前的版本可在「更多 → 恢复上一版{label}」或「恢复手动修改版」找回。"
        )
        go = box.addButton("重新生成", QMessageBox.ButtonRole.AcceptRole)
        back = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(back if notes.user_edited else go)
        _style_dialog_box(box)
        with _quiet_tooltips(self):
            box.exec()
        return box.clickedButton() == go

    def _available_user_backup(self, meeting_id: str, notes=None) -> str:
        if not meeting_id:
            return ""
        if notes is None:
            notes = self._store.get_notes(meeting_id)
        current = notes.markdown if notes is not None else ""
        persisted = str(getattr(notes, "user_markdown", "") or "").strip() if notes is not None else ""
        if persisted and persisted != current.strip():
            return str(getattr(notes, "user_markdown", ""))
        cached = self._drafts.user_markdown_backups.get(meeting_id, "")
        if cached and cached.strip() and cached != current:
            return cached
        return ""

    def _restore_user_backup(self) -> None:
        if not self._detail_id or self._ctrl.generating_for == self._detail_id:
            return
        notes = self._store.get_notes(self._detail_id)
        backup = self._available_user_backup(self._detail_id, notes)
        if not backup:
            return
        if self._editing_notes:
            self._editing_notes = False
        restore_fn = (
            getattr(self._ctrl, "restore_user_markdown", None)
            or getattr(self._store, "restore_user_markdown", None)
        )
        if callable(restore_fn) and getattr(notes, "user_markdown", ""):
            ok = bool(restore_fn(self._detail_id))
        else:
            ok = bool(self._ctrl.save_user_markdown(self._detail_id, backup))
        self._switch_tab(0)
        self._render_detail()
        if not ok:
            self._set_status("尚未恢复手动修改版，请重试。")
            return
        self._set_status(f"已恢复手动修改版{self._result_label()}。")

    def _restore_previous(self) -> None:
        if self._ctrl.generating_for == self._detail_id:
            return
        notes = self._store.get_notes(self._detail_id) if self._detail_id else None
        if notes is None or not notes.previous_markdown:
            return
        if self._editing_notes:
            self._toggle_edit()
            if self._editing_notes:
                return
        if notes.user_edited and notes.markdown:
            self._drafts.user_markdown_backups[self._detail_id] = notes.markdown
        self._drafts.pop_pending_template(self._detail_id)
        restored = self._ctrl.restore_previous_notes(self._detail_id)
        self._switch_tab(0)
        self._render_detail()
        if not restored:
            self._set_status("尚未恢复上一版，当前内容保持不变。")
            return
        self._set_status(f"已恢复上一版{self._result_label()}；再次恢复可切回刚才的版本。")

    def _switch_tab(self, index: int, *, _auto: bool = False) -> None:
        self._tab_auto_switched = bool(_auto)
        self._tab_group.button(index).setChecked(True)
        self._content_stack.setCurrentIndex(index)
        self._tab_tools.setCurrentIndex(index)
        self._sync_copy_action()
        self._sync_content_hint()
        if self._detail_find_bar.isVisible():
            self._find_detail_text(restart=True)

    def _cancel_job(self) -> None:
        if self._detail_id:
            self._ctrl.remove_from_queue(self._detail_id)

    def _keep_open_draft(self, meeting_id: str) -> None:
        """A draft the user is still typing must not be replaced by the model."""
        text = self._notes_edit.toPlainText()
        notes = self._store.get_notes(meeting_id)
        if notes is not None and text == notes.markdown:
            self._editing_notes = False
            return
        saved = self._save_notes_draft()
        self._ctrl.remove_from_queue(meeting_id)
        if meeting_id != self._detail_id:
            return
        # Cancellation announces itself first. Say what actually happened to
        # this draft, or the status bar keeps claiming generation is running.
        if saved:
            self._set_status(f"已保留正在编辑的{self._result_label()}，未用生成结果覆盖。")
        else:
            self._set_status("修改尚未保存，请重试；可先复制草稿保留内容。")

    def _shows_meeting(self, meeting_id: str) -> bool:
        """True when this meeting's detail is on screen, not hidden by batch mode."""
        return bool(meeting_id) and meeting_id == self._detail_id and not self._batch_mode

    def _on_generation_progress(self, meeting_id: str, text: str) -> None:
        if meeting_id == self._detail_id and self._editing_notes:
            self._keep_open_draft(meeting_id)
        if self._ctrl.generating_for != meeting_id:
            if meeting_id != self._listed_generating:
                self.refresh()
            return
        if self._shows_meeting(meeting_id):
            self._set_status(text)
        if meeting_id != self._listed_generating:
            # A queued job just started: its list row changes too.
            self.refresh()
        elif meeting_id == self._detail_id:
            self._render_detail()

    def _on_generation_preview(self, meeting_id: str, text: str) -> None:
        if meeting_id != self._detail_id or self._editing_notes:
            return
        if not text:
            self._render_detail()
            return
        self._notes_stack.setCurrentWidget(self._notes_view)
        bar = self._notes_view.verticalScrollBar()
        position = bar.value()
        at_end = position >= bar.maximum() - 24
        self._notes_view.set_reading_plain(text)
        self._notes_view.restore_reading_position(bar.maximum() if at_end else position)

    def _on_generation_finished(self, meeting_id: str) -> None:
        self._drafts.pop_pending_template(meeting_id)
        if meeting_id == self._detail_id:
            if self._editing_notes and not self._save_notes_draft():
                self.refresh()
                return
            self._editing_notes = False
            self._switch_tab(0)
            self._render_detail()
            if not self._batch_mode:
                self._set_status(
                    "原文已精整，可切换到文字稿核对重要内容。" if self._result_label() == "精整正文" else
                    "纪要已生成，可点击时间戳核对文字稿。"
                )
        self.refresh()

    def _on_generation_failed(self, meeting_id: str, message: str) -> None:
        self.refresh()
        if self._shows_meeting(meeting_id):
            self._set_status(f"{'原文整理' if self._is_refinement() else '纪要生成'}失败：{message}")
        elif self._batch_mode:
            # Batch view has no open meeting; name the one that failed.
            record = self._records.get(meeting_id) or self._store.get_meeting(meeting_id)
            title = record.display_title if record is not None else "一场会议"
            self._set_status(f"「{title}」生成失败：{message}")

    def _on_generation_cancelled(self, meeting_id: str) -> None:
        self.refresh()
        if self._shows_meeting(meeting_id):
            label = self._result_label()
            action = "原文整理" if label == "精整正文" else "纪要生成"
            self._set_status(f"已取消{action}，上一版{label}保持不变。")

    def _on_generation_queued(self, meeting_id: str) -> None:
        self.refresh()

    def _save_notes_draft(self) -> bool:
        if not self._editing_notes or not self._detail_id:
            return True
        text = self._notes_edit.toPlainText()
        notes = self._store.get_notes(self._detail_id)
        if notes is None or text != notes.markdown:
            if not self._ctrl.save_user_markdown(self._detail_id, text):
                self._set_status("修改尚未保存，请重试；可先复制草稿保留内容。")
                return False
            if text.strip():
                self._drafts.user_markdown_backups[self._detail_id] = text
        self._editing_notes = False
        return True

    def _cancel_edit_notes(self) -> None:
        if not self._editing_notes:
            return
        notes = self._store.get_notes(self._detail_id) if self._detail_id else None
        original = notes.markdown if notes is not None else ""
        draft = self._notes_edit.toPlainText()
        if draft != original:
            from sayink.ui.prompts import confirm_box

            label = self._result_label()
            box, discard = confirm_box(
                self,
                "放弃未保存修改",
                f"放弃本次对{label}的未保存修改？",
                accept="放弃修改",
                cancel="继续编辑",
                destructive=True,
            )
            box.setInformativeText("放弃后将恢复为编辑前的已保存内容。")
            _style_dialog_box(box, danger_btn=discard)
            with _quiet_tooltips(self):
                box.exec()
            if box.clickedButton() != discard:
                return
        self._notes_edit.blockSignals(True)
        self._notes_edit.setPlainText(original)
        self._notes_edit.blockSignals(False)
        self._editing_notes = False
        self._render_detail()
        self._set_status("已取消编辑，恢复修改前内容。", transient=True)

    def try_leave(self) -> bool:
        """Save open drafts before another page covers this one.

        False keeps the editor, the draft and the visible hint on this page.
        """
        if not hasattr(self, "_notes_edit"):
            return True
        self._audio_player.stop()
        if not self._flush_bound_title(sync=True):
            return False
        if not self.flush_pending_user_notes():
            self._set_status("笔记尚未保存，输入已保留。")
            return False
        if not self._save_record_notes():
            return False
        if not self._flush_stashed_live_notes():
            return False
        return self._save_notes_draft()

    def hideEvent(self, event) -> None:
        if hasattr(self, "_notes_edit"):
            self._audio_player.stop()
            editing = self._editing_notes
            self._flush_bound_title(sync=True)
            self._save_record_notes()
            self._flush_stashed_live_notes()
            if self._save_notes_draft() and editing:
                self._render_detail()
        super().hideEvent(event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not defer_while_live_resize(self, self._follow_live_resize):
            self._sync_responsive_layout()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if not defer_while_live_resize(self, self._follow_live_resize):
            self._follow_live_resize()

    def _sync_live_marks_height(self, has_marks: bool | None = None) -> None:
        from sayink.ui import meeting_workspace_chrome as mwc
        mwc._sync_live_marks_height(self, has_marks)

    @property
    def is_compact_single_pane(self) -> bool:
        """True when narrow split-screen mode (<680px window / <580px workspace) shows one pane at a time."""
        return bool(getattr(self, "_compact_single_pane", False))

    def show_compact_list(self) -> None:
        from sayink.ui import meeting_workspace_chrome as mwc
        mwc.show_compact_list(self)

    def show_compact_detail(self) -> None:
        from sayink.ui import meeting_workspace_chrome as mwc
        mwc.show_compact_detail(self)

    def _on_compact_meeting_clicked(self, item: QListWidgetItem) -> None:
        from sayink.ui import meeting_workspace_chrome as mwc
        mwc._on_compact_meeting_clicked(self, item)

    def _apply_compact_pane_visibility(self) -> None:
        from sayink.ui import meeting_workspace_chrome as mwc
        mwc._apply_compact_pane_visibility(self)

    def sync_viewport_width(self, host_width: int | None = None) -> None:
        from sayink.ui import meeting_workspace_chrome as mwc
        mwc.sync_viewport_width(self, host_width)

    def _sync_responsive_layout(self) -> None:
        from sayink.ui import meeting_workspace_chrome as mwc
        mwc._sync_responsive_layout(self)

    def _follow_live_resize(self) -> None:
        from sayink.ui import meeting_workspace_chrome as mwc
        mwc.follow_live_resize(self)

    def keyPressEvent(self, event) -> None:
        if event.matches(QKeySequence.StandardKey.Find):
            self.focus_search()
            event.accept()
            return
        super().keyPressEvent(event)

    def eventFilter(self, obj, event) -> bool:
        if event.type() in (QEvent.Type.DragEnter, QEvent.Type.DragMove, QEvent.Type.Drop):
            if obj in getattr(self, "_import_drop_widgets", ()) and self._handle_import_drag(
                event, dropping=event.type() == QEvent.Type.Drop,
            ):
                return True
        if obj is getattr(self, "_notes_bar", None) and event.type() == QEvent.Type.Resize:
            if not defer_while_live_resize(self._notes_bar, self._fit_template_cluster):
                self._fit_template_cluster()
        elif (
            hasattr(self, "_live_transcript")
            and obj is self._live_transcript.viewport()
            and event.type() == QEvent.Type.Resize
        ):
            self._position_live_new_msg_btn()
        elif event.type() == QEvent.Type.KeyPress:
            if obj is getattr(self, "_notes_edit", None) and event.matches(QKeySequence.StandardKey.Save):
                self._save_open_minutes()
                return True
            if obj is getattr(self, "_detail_find_edit", None):
                if event.key() == Qt.Key.Key_Escape:
                    self._close_detail_find()
                    return True
                if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and (
                    event.modifiers() & Qt.KeyboardModifier.ShiftModifier
                ):
                    self._find_detail_text(backward=True)
                    return True
            if obj is getattr(self, "_live_find_edit", None):
                if event.key() == Qt.Key.Key_Escape:
                    self._close_live_find()
                    return True
                if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and (
                    event.modifiers() & Qt.KeyboardModifier.ShiftModifier
                ):
                    self._find_live_text(backward=True)
                    return True
            if obj in (
                getattr(self, "_notes_view", None),
                getattr(self, "_notes_edit", None),
                getattr(self, "_transcript_view", None),
            ) and event.matches(QKeySequence.StandardKey.Find):
                self.open_detail_find()
                return True
        return super().eventFilter(obj, event)

    def _save_open_minutes(self) -> None:
        if not self._editing_notes:
            return
        if not self._save_notes_draft():
            return
        self._set_status("修改已保存")
        self._render_detail()

    def unsaved_exit_draft(self) -> str | None:
        """Save what we can. Return text that did not land, so exit can ask first."""
        if not self._flush_bound_title(sync=True):
            return (self._detail_title.text() if hasattr(self, "_detail_title") else "") or " "
        if not self.flush_pending_user_notes():
            return (self._user_notes.toPlainText() if hasattr(self, "_user_notes") else "") or " "
        if not self._save_record_notes():
            return self._record_notes.toPlainText() or " "
        if not self._flush_stashed_live_notes():
            return next(iter(self._unsaved_live_notes.values()), " ") or " "
        if not self._editing_notes or not self._detail_id:
            return None
        text = self._notes_edit.toPlainText()
        if self._save_notes_draft():
            return None
        return text

    def _on_record_notes_changed(self) -> None:
        self._drafts.mark_record_notes_edited()
        self._sync_record_notes_height()
        self._record_notes_timer.start()

    def _sync_record_notes_height(self) -> None:
        text = self._record_notes.toPlainText()
        line_count = max(1, text.count("\n") + 1) if text.strip() else 1
        line_h = max(18, self._record_notes.fontMetrics().lineSpacing())
        desired = min(84, max(38, line_count * line_h + 18))
        self._record_notes.setMinimumHeight(38)
        self._record_notes.setMaximumHeight(desired)

    def _on_materials_toggled(self, checked: bool) -> None:
        self._materials_scroll.setVisible(bool(checked))
        summary = getattr(self, "_materials_summary_text", "会议资料") or "会议资料"
        self._materials_toggle.setText(f"收起{summary}" if checked else f"展开{summary}")

    def _has_unsaved_record_notes(self) -> bool:
        if not hasattr(self, "_record_notes"):
            return False
        return self._drafts.has_unsaved_record_notes(
            getattr(self, "_store", None),
            self._record_notes.toPlainText(),
        )

    def _flush_stashed_live_notes(self) -> bool:
        ok = self._drafts.flush_stashed_live_notes(
            getattr(self, "_store", None),
            self._ctrl.update_meeting,
        )
        if not ok:
            self._set_status("笔记尚未保存，输入已保留。")
        return ok

    def _show_materials(self, record) -> None:
        stashed = self._drafts.pop_promotable_live_note(
            self._store, record.id, self._record_notes.toPlainText()
        )
        if stashed is not None:
            self._record_notes.blockSignals(True)
            self._record_notes.setPlainText(stashed)
            self._record_notes.blockSignals(False)
            self._record_notes_timer.start()
        marks = self._store.get_marks(record.id)
        agenda = (record.agenda or "").strip()
        # A refresh must not copy one meeting's notes into the next, or wipe
        # keystrokes that have not reached the database yet.
        same_meeting = self._drafts.record_notes_meeting_id == record.id
        unsaved = (
            same_meeting
            and self._drafts.record_notes_dirty
            and self._record_notes.toPlainText() != (record.notes_user or "")
        )
        pending = same_meeting and (self._record_notes_timer.isActive() or unsaved)
        if not pending and (not same_meeting or not self._record_notes.hasFocus()):
            self._record_notes.blockSignals(True)
            self._record_notes.setPlainText(record.notes_user or "")
            self._record_notes.blockSignals(False)
            self._drafts.record_notes_dirty = False
        self._drafts.record_notes_meeting_id = record.id
        notes_text = self._record_notes.toPlainText().strip()
        has = bool(
            agenda
            or (record.notes_user or "").strip()
            or (same_meeting and notes_text)
            or unsaved
            or marks
        )
        summary_bits: list[str] = []
        if agenda:
            summary_bits.append("议程")
        if notes_text or (record.notes_user or "").strip():
            summary_bits.append("我的笔记")
        if marks:
            summary_bits.append(f"{len(marks)} 处标记")
        self._materials_summary_text = (
            f"会议资料（{' · '.join(summary_bits)}）" if summary_bits else "会议资料"
        )
        self._materials.setVisible(has)
        if unsaved and not self._materials_toggle.isChecked():
            self._materials_toggle.setChecked(True)
        self._on_materials_toggled(self._materials_toggle.isChecked())
        self._sync_record_notes_height()
        self._materials_agenda.setVisible(bool(agenda))
        self._materials_agenda.setText(("议程：" + agenda) if agenda else "")
        marks_html = "".join(
            f"<p style='margin:0 0 4px 0'>"
            f"<a href='ts:{max(0, int(mark.offset_ms)) // 1000}' "
            f"style='color:{tok.ACCENT}; text-decoration:none;'>[{format_timestamp(mark.offset_ms)}]</a> "
            f"{html.escape(mark.note) or '标记'}</p>"
            for mark in marks
        )
        self._materials_marks_label.setVisible(bool(marks))
        self._record_marks.setVisible(bool(marks))
        if marks:
            mark_h = min(76, max(32, len(marks) * 22 + 12))
            self._record_marks.setMaximumHeight(mark_h)
        if marks_html != self._drafts.record_marks_html:
            self._drafts.record_marks_html = marks_html
            self._record_marks.setHtml(marks_html)

    def _autosave_record_notes(self) -> None:
        """Persist debounced post-meeting notes asynchronously without blocking the UI thread."""
        meeting_id = self._drafts.record_notes_meeting_id
        if not self._drafts.record_notes_dirty or not meeting_id or not hasattr(self, "_record_notes"):
            return
        text = self._record_notes.toPlainText()
        record = self._store.get_meeting(meeting_id)
        if record is None or (record.notes_user or "") == text:
            self._drafts.record_notes_dirty = False
            return
        token = self._drafts.begin_record_notes_autosave()
        history = getattr(self._store, "_history", None)
        use_history = history is not None and not getattr(self._store, "disabled", False)
        enqueued = self._drafts.enqueue_tracked(
            history if use_history else None,
            lambda: self._ctrl.update_meeting(meeting_id, notes_user=text),
        )
        if not enqueued:
            if not self._drafts.settle_record_notes_write(
                self._store, meeting_id, text, token, self._record_notes.toPlainText()
            ):
                self._set_status("笔记尚未保存，输入已保留。")
            return
        weak_self = weakref.ref(self)
        history.enqueue_write(
            lambda _conn: (page := weak_self()) is not None
            and page._record_notes_write_settled.emit(meeting_id, text, token)
        )

    def _on_record_notes_write_settled(self, meeting_id: str, text: str, token: int) -> None:
        current = self._record_notes.toPlainText() if hasattr(self, "_record_notes") else ""
        if not self._drafts.settle_record_notes_write(self._store, meeting_id, text, token, current):
            self._set_status("笔记尚未保存，输入已保留。")

    def _save_record_notes(self) -> bool:
        """Write the post-meeting notes at a navigation/export/exit boundary."""
        timer = getattr(self, "_record_notes_timer", None)
        if timer is not None:
            timer.stop()
        meeting_id = self._drafts.record_notes_meeting_id
        if not self._drafts.record_notes_dirty or not meeting_id or not hasattr(self, "_record_notes"):
            return self._flush_stashed_live_notes()
        text = self._record_notes.toPlainText()
        record = self._store.get_meeting(meeting_id)
        if record is None or (record.notes_user or "") == text:
            self._drafts.record_notes_dirty = False
            return self._flush_stashed_live_notes()
        # Keep the debounce running across the write. A refresh that sneaks in
        # while the writer is behind must not replace these keystrokes.
        if timer is not None:
            timer.start()
        self._ctrl.update_meeting(meeting_id, notes_user=text)
        saved = self._store.get_meeting(meeting_id) if self._store.flush() else None
        if saved is None or saved.notes_user != text:
            self._set_status("笔记尚未保存，输入已保留。")
            return False
        self._drafts.record_notes_dirty = False
        if timer is not None:
            timer.stop()
        return self._flush_stashed_live_notes()

    def _toggle_edit(self) -> None:
        if not self._detail_id:
            return
        if self._editing_notes:
            if not self._save_notes_draft():
                return
            self._set_status("修改已保存")
        else:
            notes = self._store.get_notes(self._detail_id)
            self._notes_edit.setPlainText(notes.markdown if notes is not None else "")
            self._editing_notes = True
            self._notes_edit.setFocus()
        self._render_detail()

    def _copy_notes(self) -> None:
        text = self._current_notes_text()
        if not text:
            return
        done = "已复制当前草稿" if self._editing_notes else (
            "已复制精整正文" if self._result_label() == "精整正文" else "已复制纪要 Markdown"
        )
        self._set_status(done if copy_text(text) else COPY_FAILED_TEXT)

    def _copy_current(self) -> None:
        if self._content_stack.currentIndex() == 0:
            self._copy_notes()
        else:
            self._copy_transcript()

    def _copy_transcript(self) -> None:
        if self._detail_id:
            text = self._ctrl.transcript_text(self._detail_id)
            if text:
                self._set_status("已复制文字稿" if copy_text(text) else COPY_FAILED_TEXT)

    def _markdown_for_export_scope(self, record: MeetingRecord, scope: str) -> str:
        if scope == "transcript":
            return self._ctrl.export_transcript_markdown(record.id)
        if scope == "notes":
            return self._current_notes_text()
        if self._editing_notes:
            supplement = self._ctrl.supplements_markdown(record.id)
            text = self._current_notes_text().rstrip()
            if supplement:
                text += "\n\n" + supplement
            text += "\n\n## 文字稿\n\n" + self._ctrl.transcript_text(record.id) + "\n"
            return text
        return self._ctrl.export_markdown(record.id)

    def _docx_metadata_rows(self, record: MeetingRecord) -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = [("时间", _format_dt(record.started_at))]
        if record.ended_at:
            rows.append(("时长", _duration_label(record.duration_ms)))
        if (record.participants or "").strip():
            rows.append(("参会人", record.participants.strip()))
        return rows

    def export_meeting_to_path(self, meeting_id: str, path: str | Path, *, scope: str = "all") -> bool:
        """Public contract writing ``meeting_id`` to ``path`` (.md, .docx, .txt, or .srt)."""
        from sayink.ui import meeting_workspace_chrome as mwc
        return mwc.export_meeting_to_path(self, meeting_id, path, scope=scope)

    def _export(self, scope: str = "all") -> None:
        from sayink.ui import meeting_workspace_chrome as mwc
        mwc.export_single_meeting(self, scope=scope)

    def _delete(self) -> None:
        record = self._store.get_meeting(self._detail_id) if self._detail_id else None
        if record is None:
            return
        if record.status == STATUS_LIVE and self._ctrl.current_id == record.id:
            box = QMessageBox(self)
            box.setWindowTitle("会议进行中")
            box.setIcon(QMessageBox.Icon.Information)
            box.setText("请先结束会议，再删除记录。")
            box.addButton("确定", QMessageBox.ButtonRole.AcceptRole)
            _style_dialog_box(box)
            with _quiet_tooltips(self):
                box.exec()
            return
        from sayink.ui.prompts import confirm_box

        box, delete = confirm_box(
            self,
            "删除会议",
            f"删除「{record.display_title}」？",
            accept="删除",
            cancel="取消",
            destructive=True,
        )
        box.setInformativeText("文字稿、笔记、标记与纪要会一起删除，无法撤销。")
        _style_dialog_box(box, danger_btn=delete)
        with _quiet_tooltips(self):
            box.exec()
        if box.clickedButton() != delete:
            return
        temp_wav_targets = self._audio_player.collect_temp_wavs_for_audio_paths(
            [record.audio_path] if record.audio_path else ()
        )
        self._audio_player.unload_source()
        if self._ctrl.delete_meeting(record.id) is False:
            self._set_status("会议删除失败，请重试。")
            self.refresh()
            return
        self._audio_player.remove_temp_wavs(temp_wav_targets)
        self._editing_notes = False
        self._detail_id = ""
        self._drafts.title_meeting_id = ""
        self.refresh()

    def _on_transcript_link(self, url) -> None:
        if url.scheme() == "seek-ms" and url.path().isdigit():
            offset_ms = int(url.path())
            if self._audio_player.audio_path:
                self._audio_player.show()
                self._audio_player.seek_ms(offset_ms, autoplay=True)
        elif url.scheme() == "speaker" and url.path().isdigit():
            self._prompt_rename_speaker(int(url.path()))
        elif url.scheme() == "segment" and url.path().isdigit() and self._correcting_speakers:
            self._prompt_segment_speaker(int(url.path()))
        elif url.scheme() == "edit-segment" and url.path().isdigit():
            self._prompt_segment_text(int(url.path()))
        elif url.scheme() in ("edited-segment", "details-segment") and url.path().isdigit():
            self._prompt_segment_text(int(url.path()), details=True)

    def _prompt_segment_text(self, seq: int, *, details: bool = False, meeting_id: str | None = None) -> None:
        meeting_id = meeting_id or self._detail_id
        record = self._store.get_meeting(meeting_id) if meeting_id else None
        if record is None:
            return
        generating = self._ctrl.generating_for == record.id
        live_session = record.status == STATUS_LIVE and self._ctrl.current_id == record.id
        draining = live_session and self._ctrl.state is MeetingState.DRAINING
        live_editable = live_session and self._ctrl.state in (MeetingState.LIVE, MeetingState.PAUSED) and not generating
        read_only = generating or draining or (record.status == STATUS_LIVE and (details or not live_editable))
        if read_only and not details:
            return
        if not read_only and not self._save_notes_draft():
            return
        get_seg = getattr(self._store, "get_segment", None)
        segment = (
            get_seg(record.id, seq)
            if callable(get_seg)
            else next((s for s in self._store.get_segments(record.id) if s.seq == seq), None)
        )
        if segment is None:
            return
        names = self._ctrl.speaker_names(record.id)
        line = transcript_lines([segment], record.started_at)
        if not line:
            return
        speaker = ((names.get(line[0].speaker_id) or f"说话人{line[0].speaker_id}")
                   if line[0].speaker_id > 0 else "说话人待确认")
        position = _position_label(line[0].offset_ms)
        if record.source == IMPORT_SOURCE and record.ended_at is None:
            position = ""
            if line[0].speaker_id <= 0:
                speaker = "本段"
        detail_text = ""
        if details:
            auto_names = {s.speaker_id for s in self._store.get_speakers(record.id)
                          if s.suggested_name and not s.display_name and not s.name_locked}
            detail_text = "姓名来源：" + _speaker_source(segment.speaker_id, names, auto_names)
            if segment.speaker_locked:
                detail_text += "\n发言归属：已手动更正，仅作用于本段。"
            if segment.corrected_text:
                detail_text += "\n正文：已编辑，修改前的原文保留如下。"
        before_text = (segment.corrected_text or segment.polished_text or segment.raw_text or "").strip()
        dialog = _SegmentTextDialog(
            segment, position=position, speaker=speaker,
            save_text=lambda text: self._ctrl.save_segment_text(record.id, seq, text), parent=self,
            details=detail_text, read_only=read_only,
        )
        with _quiet_tooltips(self):
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
        if accepted:
            self._post_segment_text_saved(
                record,
                seq,
                before_text=before_text,
                corrected_text=dialog.corrected_text,
            )

    def _toggle_speaker_correction(self, enabled: bool) -> None:
        self._correcting_speakers = enabled
        self._render_detail()

    def _prompt_segment_speaker(self, seq: int, *, meeting_id: str | None = None) -> None:
        meeting_id = meeting_id or self._detail_id
        record = self._store.get_meeting(meeting_id) if meeting_id else None
        if record is None or self._ctrl.generating_for == record.id:
            return
        live = record.status == STATUS_LIVE
        if live and (self._ctrl.current_id != record.id or self._ctrl.state is MeetingState.DRAINING):
            return
        get_seg = getattr(self._store, "get_segment", None)
        speaker_ids_fn = getattr(self._store, "speaker_ids_in_segments", None)
        if callable(get_seg) and callable(speaker_ids_fn):
            segment = get_seg(record.id, seq)
            if segment is None:
                return
            raw_ids = speaker_ids_fn(record.id)
        else:
            segments = self._store.get_segments(record.id)
            segment = next((s for s in segments if s.seq == seq), None)
            if segment is None:
                return
            raw_ids = {s.speaker_id for s in segments}
        names = self._ctrl.speaker_names(record.id)
        ids = sorted({int(sid) for sid in raw_ids if sid and int(sid) > 0})
        choices = [(sid, f"{names[sid]} · 说话人{sid}" if sid in names else f"说话人{sid}") for sid in ids]
        choices.append((None, "新增说话人" if live else "新增说话人（仅本段）"))

        def save(speaker_id):
            if live and (self._ctrl.current_id != record.id or self._ctrl.state is MeetingState.DRAINING):
                return False
            return self._ctrl.correct_segment_speaker(record.id, seq, speaker_id)

        dialog = _SegmentSpeakerDialog(segment, choices, self, save_speaker=save, live=live)
        with _quiet_tooltips(self):
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
        if accepted:
            self._speaker_edit_status(record.id, "已更正这一段的发言人，人工更正会保留。" if live else
                                      "已更正这一段的发言人；请核对纪要中的相关归属。", transient=True)

    def _prompt_rename_speaker(self, speaker_id: int, *, meeting_id: str | None = None) -> None:
        meeting_id = meeting_id or self._detail_id
        if not meeting_id:
            return
        record = self._store.get_meeting(meeting_id)
        if record is None:
            return
        live = record.status == STATUS_LIVE
        if self._ctrl.generating_for == meeting_id:
            self._speaker_edit_status(meeting_id, "纪要生成中，请在完成后为说话人命名。")
            return
        if live and (self._ctrl.current_id != meeting_id or self._ctrl.state is MeetingState.DRAINING):
            return
        current = self._ctrl.speaker_names(meeting_id).get(speaker_id, "")
        scope = ("这组已有发言和后续归入本组的发言都会使用该姓名；留空恢复编号。" if live else
                 "会更新这一组发言的名称和纪要中的称呼；留空恢复编号。")
        from sayink.ui.prompts import prompt_text

        name = prompt_text(
            self, "为整组说话人命名",
            f"「说话人{speaker_id}」的显示名称\n{scope}",
            current,
        )
        if name is not None:
            if live and (self._ctrl.current_id != meeting_id or self._ctrl.state is MeetingState.DRAINING):
                self._speaker_edit_status(meeting_id, "会议已结束或正在整理，姓名尚未保存，请在会后重新命名。")
                return
            self._rename_speaker(speaker_id, name, meeting_id=meeting_id)

    def _rename_speaker(self, speaker_id: int, name: str, *, meeting_id: str | None = None) -> None:
        meeting_id = meeting_id or self._detail_id
        if not meeting_id:
            return
        if meeting_id == self._detail_id and not self._save_notes_draft():
            return
        name = name.strip()
        if name:
            existing_names = self._ctrl.speaker_names(meeting_id)
            duplicate_sids = sorted(
                sid for sid, existing in existing_names.items()
                if sid != speaker_id and (existing or "").strip() == name
            )
            if duplicate_sids:
                target_sid = duplicate_sids[0]
                merge_fn = getattr(self._ctrl, "merge_speakers", None)
                if callable(merge_fn):
                    merged = bool(merge_fn(meeting_id, speaker_id, target_sid))
                else:
                    segments = [
                        s for s in self._store.get_segments(meeting_id)
                        if int(s.speaker_id) == int(speaker_id)
                    ]
                    merged = True
                    for seg in segments:
                        if not self._ctrl.correct_segment_speaker(meeting_id, seg.seq, target_sid):
                            merged = False
                            break
                    if merged:
                        cleared = self._ctrl.rename_speaker(meeting_id, speaker_id, "")
                        renamed = self._ctrl.rename_speaker(meeting_id, target_sid, name)
                        merged = cleared is not None and renamed is not None
                if not merged:
                    self._speaker_edit_status(meeting_id, "姓名尚未保存，请在整理完成后重试。")
                    return
                self._speaker_edit_status(
                    meeting_id,
                    f"已将说话人{speaker_id}并入「{name}」",
                    transient=True,
                )
                return
        if self._ctrl.rename_speaker(meeting_id, speaker_id, name) is None:
            self._speaker_edit_status(meeting_id, "姓名尚未保存，请在整理完成后重试。")
            return
        self._speaker_edit_status(meeting_id, f"已将说话人{speaker_id}改为「{name}」" if name else
                                  f"已恢复「说话人{speaker_id}」", transient=True)

    def _speaker_edit_status(self, meeting_id: str, text: str, *, transient: bool = False) -> None:
        if self._stack.currentIndex() == PAGE_LIVE and self._drafts.live_meeting_id == meeting_id:
            self._live_feedback_timer.stop()
            self._render_live()
            self._live_speaker_feedback.setText(text)
            self._live_speaker_feedback.show()
            if transient:
                self._live_feedback_timer.start(4000)
        elif self._detail_id == meeting_id:
            self._render_detail()
            self._set_status(text, transient=transient)

    def _populate_live_speaker_menu(self) -> None:
        meeting_id = self._ctrl.current_id
        self._live_speaker_menu.clear()
        if not meeting_id or self._ctrl.state is MeetingState.DRAINING:
            return
        names = self._ctrl.speaker_names(meeting_id)
        speaker_ids_fn = getattr(self._store, "speaker_ids_in_segments", None)
        raw_ids = (
            speaker_ids_fn(meeting_id)
            if callable(speaker_ids_fn)
            else {s.speaker_id for s in self._store.get_segments(meeting_id)}
        )
        for sid in sorted({int(sid) for sid in raw_ids if sid and int(sid) > 0}):
            action = self._live_speaker_menu.addAction(names.get(sid) or f"说话人{sid}")
            action.triggered.connect(lambda _=False, sid=sid, mid=meeting_id:
                                     self._prompt_rename_speaker(sid, meeting_id=mid))

    def _toggle_live_speaker_correction(self, enabled: bool) -> None:
        self._live_correcting_speakers = enabled
        self._render_live()

    def _on_live_transcript_link(self, url) -> None:
        meeting_id = self._ctrl.current_id
        if not meeting_id or self._ctrl.state is MeetingState.DRAINING or not url.path().isdigit():
            return
        if url.scheme() == "live-speaker":
            self._prompt_rename_speaker(int(url.path()), meeting_id=meeting_id)
        elif url.scheme() == "live-segment" and self._live_correcting_speakers:
            self._prompt_segment_speaker(int(url.path()), meeting_id=meeting_id)
        elif url.scheme() == "live-edit":
            self._prompt_segment_text(int(url.path()), meeting_id=meeting_id)
        elif url.scheme() == "live-details":
            self._prompt_segment_text(int(url.path()), details=True, meeting_id=meeting_id)

    # ── Composed live & import controller delegations ─────────────

    def _build_start_card(self) -> QWidget:
        return self._live_ui.build_start_card()

    def _build_live(self) -> QWidget:
        return self._live_ui.build_live()

    def show_start_card(self, *, reset: bool = False) -> None:
        self._live_ui.show_start_card(reset=reset)

    def reset_start_card(self) -> None:
        self._live_ui.reset_start_card()

    def current_view_mode(self) -> str:
        return {PAGE_START: "start", PAGE_LIVE: "live"}.get(self._stack.currentIndex(), "browse")

    def is_on_start_card(self) -> bool:
        return self._stack.currentIndex() == PAGE_START

    def start_card_values(self):
        return self._live_ui.options_from_card()

    def set_start_card_fields(self, **kwargs) -> None:
        from sayink.ui import meeting_workspace_chrome as mwc
        mwc.set_start_card_fields_ui(self, **kwargs)

    def retry_unsaved_segments(self, meeting_id: str | None = None) -> tuple[bool, str]:
        from sayink.ui import meeting_workspace_chrome as mwc
        return mwc.retry_unsaved_segments_ui(self, meeting_id)

    def select_meetings_for_batch(self, meeting_ids: list[str] | tuple[str, ...]) -> None:
        from sayink.ui import meeting_workspace_chrome as mwc
        mwc.select_meetings_for_batch_ui(self, meeting_ids)

    def ui_state_snapshot(self) -> dict[str, object]:
        from sayink.ui import meeting_workspace_chrome as mwc
        return mwc.page_ui_snapshot(self)

    trigger_generate = _generate
    trigger_batch_generate = _generate_selected_meetings
    trigger_export = _export

    def cancel_downloads(self) -> None:
        self._aux_dl.cancel()

    @property
    def running_workers(self):
        return self._aux_dl.running_workers

    def set_lane_levels(self, mic_level: float = 0.0, system_level: float = 0.0) -> None:
        self._live_ui.set_lane_levels(mic_level, system_level)

    def show_system_silent_warning(self, message: str = "") -> None:
        self._live_ui.show_system_silent_warning(message)

    def clear_system_silent_warning(self) -> None:
        self._live_ui.clear_system_silent_warning()

    def flush_pending_user_notes(self) -> bool:
        return self._live_ui.flush_pending_user_notes()

    def _follow_model_label(self) -> str:
        return self._live_ui.follow_model_label()

    def _update_card_notes(self) -> None:
        self._live_ui.update_card_notes()

    def _download_aux_models(self) -> None:
        self._live_ui.download_aux_models()

    def _on_aux_progress(self, pct: int) -> None:
        self._live_ui.on_aux_progress(pct)

    def _on_aux_finished(self, ok: bool, message: str) -> None:
        self._live_ui.on_aux_finished(ok, message)

    def _options_from_card(self):
        return self._live_ui.options_from_card()

    def _leave_start_card(self) -> None:
        self._live_ui.leave_start_card()

    def _start_from_card(self) -> None:
        self._live_ui.start_from_card()

    def _dismiss_system_silent_warning(self) -> None:
        self._live_ui.dismiss_system_silent_warning()

    def _check_live_remote_silence(self) -> None:
        self._live_ui.check_live_remote_silence()

    def _on_live_range_changed(self, minimum: int, maximum: int) -> None:
        self._live_ui.on_live_range_changed(minimum, maximum)

    def _on_live_scroll_changed(self, value: int) -> None:
        self._live_ui.on_live_scroll_changed(value)

    def _scroll_live_to_bottom(self) -> None:
        self._live_ui.scroll_live_to_bottom()

    def _position_live_new_msg_btn(self) -> None:
        self._live_ui.position_live_new_msg_btn()

    def _enter_live(self) -> None:
        self._live_ui.enter_live()

    def _sync_live_state(self) -> None:
        self._live_ui.sync_live_state()

    def _paint_live_clock(self) -> None:
        self._live_ui.paint_live_clock()

    def _on_state_changed(self, state: MeetingState) -> None:
        self._live_ui.on_state_changed(state)

    def _on_meeting_changed(self, meeting_id: str) -> None:
        self._live_ui.on_meeting_changed(meeting_id)

    def _refresh_now(self) -> None:
        self._live_ui.refresh_now()

    def _render_live(self) -> None:
        self._live_ui.render_live()

    def _add_mark(self) -> None:
        self._live_ui.add_mark()

    def _save_user_notes(self) -> None:
        self._live_ui.save_user_notes()

    def _confirm_end(self) -> None:
        self._live_ui.confirm_end()

    def _ask_import(self):
        return self._import_ui.ask_import()

    def _import_transcript(self) -> None:
        self._import_ui.import_transcript()

    def _remove_queued_media(self, index: int) -> None:
        self._import_ui.remove_queued_media(index)

    def _sync_media_buttons(self) -> None:
        self._import_ui.sync_media_buttons()

    def _import_media(self) -> None:
        self._import_ui.import_media()

    def _confirm_and_begin_media_paths(self, paths: list[Path]) -> tuple[bool, str]:
        return self._import_ui.confirm_and_begin_media_paths(paths)

    def _begin_media_paths(self, paths: list[Path]) -> str:
        return self._import_ui.begin_media_paths(paths)

    def _show_import_detail(self) -> None:
        self._import_ui.show_import_detail()

    def _paint_import_banner(self) -> None:
        self._import_ui.paint_import_banner()

    def _hide_import_chrome(self) -> None:
        self._import_ui.hide_import_chrome()

    def _cancel_media_import(self) -> None:
        self._import_ui.cancel_media_import()

    def _confirm_cancel_import(self) -> bool:
        return self._import_ui.confirm_cancel_import()

    def _confirm_cancel_queued_import(self, queued_count: int) -> str:
        return self._import_ui.confirm_cancel_queued_import(queued_count)

    def _on_media_started(self, path: str, title: str) -> None:
        self._import_ui.on_media_started(path, title)

    def _on_media_progress(self, text: str) -> None:
        self._import_ui.on_media_progress(text)

    def _on_media_preview(self, text: str) -> None:
        self._import_ui.on_media_preview(text)

    def _on_media_finished(self, meeting_id: str) -> None:
        self._import_ui.on_media_finished(meeting_id)

    def _retry_media_save(self) -> None:
        self._import_ui.retry_media_save()

    def _discard_unsaved_media(self) -> None:
        self._import_ui.discard_unsaved_media()

    def _on_finalize_save_failed(self, meeting_id: str, message: str) -> None:
        self._import_ui.on_finalize_save_failed(meeting_id, message)

    def _retry_finalize_save(self, meeting_id: str | None = None) -> str | None:
        return self._import_ui.retry_finalize_save(meeting_id)

    def _discard_unsaved_finalize(self, meeting_id: str | None = None) -> bool:
        return self._import_ui.discard_unsaved_finalize(meeting_id)

    retry_finalize_save = _retry_finalize_save
    discard_unsaved_finalize = _discard_unsaved_finalize

    def _retry_failed_media(self) -> None:
        self._import_ui.retry_failed_media()

    def _on_media_failed(self, message: str) -> None:
        self._import_ui.on_media_failed(message)

    def _install_import_drop_targets(self) -> None:
        self._import_ui.install_import_drop_targets()

    def _handle_import_drag(self, event, *, dropping: bool) -> bool:
        return self._import_ui.handle_import_drag(event, dropping=dropping)

    def _accept_dropped_files(self, paths: list[Path]) -> None:
        self._import_ui.accept_dropped_files(paths)

    def _import_dropped_transcript(self, path: Path, *, ignored: int) -> None:
        self._import_ui.import_dropped_transcript(path, ignored=ignored)

    def dragLeaveEvent(self, event) -> None:
        self._drag_url_cache = None
        super().dragLeaveEvent(event)

    def dragEnterEvent(self, event) -> None:
        if not self._handle_import_drag(event, dropping=False):
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        if not self._handle_import_drag(event, dropping=False):
            super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:
        if not self._handle_import_drag(event, dropping=True):
            super().dropEvent(event)


__all__ = ["MeetingPage", "PAGE_BROWSE", "PAGE_LIVE", "PAGE_START"]

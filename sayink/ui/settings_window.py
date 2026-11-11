from __future__ import annotations

import logging
import shutil
from pathlib import Path

from PyQt6.QtCore import QSignalBlocker, QThread, QTimer, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from sayink.audio_devices import (
    INPUT_SOURCE_MICROPHONE,
    INPUT_SOURCE_MIXED,
    INPUT_SOURCE_SYSTEM,
    list_microphone_devices,
    list_system_capture_devices_for_settings,
    sanitize_system_device_index,
)
from sayink.audio_recorder import AudioRecorder
from sayink.config import (
    Config,
    DEFAULT_HOTKEY,
    TRIGGER_MODE_HOTKEY,
    format_hotkey,
    is_reserved_hotkey,
)
from sayink.recognition_settings import ADVANCED_RECOGNITION_PATH
from sayink.runtime_status import RuntimeState, SegmentationStatus, coerce_runtime_status
from sayink.ui import settings_styles as _settings_styles
from sayink.ui.settings_pages import (
    AboutSettingsPage,
    GeneralSettingsPage,
    ModelSettingsPage,
    PolishSettingsPage,
)
from sayink.ui.settings_pages.general import (
    device_index as _device_index,
    set_combo_by_data,
    without_scroll_jump,
)
from sayink.ui.theme import normalize_theme_mode

log = logging.getLogger("SayInk")

# Explicit legacy private-attribute delegation map preserving 100% compatibility
# for callers/tests inspecting ``win._*`` while keeping 0 dynamic injections in
# ``sayink/ui/settings_pages/*.py``.
_GENERAL_LEGACY_NAMES = (
    "_trigger_group",
    "_trigger_continuous_rb",
    "_trigger_hotkey_rb",
    "_hotkey_edit",
    "_hotkey_hint",
    "_hotkey_reset_btn",
    "_esc_stop_divider",
    "_esc_stop_row",
    "_audio_device_buttons",
    "_separate_lanes_row",
    "_source_group",
    "_src_mic_rb",
    "_src_sys_rb",
    "_src_mixed_rb",
    "_mixed_audio_callout",
    "_mixed_audio_callout_wrap",
    "_output_mode_combo",
    "_output_mode_hint",
    "_enable_history_btn",
    "_enable_history_wrap",
    "_mic_test_btn",
    "_audio_device_summary",
    "_mic_reset_btn",
    "_mic_level_bar",
    "_mic_test_status",
    "_advanced_audio_btn",
    "_advanced_audio_panel",
    "_mic_device_combo",
    "_system_device_combo",
    "_theme_combo",
    "_theme_title_label",
    "_theme_desc_label",
    "_auto_start_row",
    "_sound_row",
    "_restore_clipboard_row",
    "_history_enabled_row",
    "_history_retention_days_spin",
    "_history_max_entries_spin",
    "_history_retention_divider",
    "_history_retention_row",
    "_history_max_entries_row",
    "_history_max_entries_divider",
    "_history_cleanup_hint",
    "_history_cleanup_hint_wrap",
    "_listen_bar_combo",
    "_llm_proxy_combo",
    "_llm_proxy_suffix_edit",
    "_listen_bar_autohide_row",
    "_recovery_row",
    "_recovery_btn",
    "_meeting_language_combo",
    "_meeting_glossary_edit",
    "_recognition_capability_label",
    "_recognition_capability_row",
    "_recognition_apply_label",
    "_advanced_recognition_btn",
    "_advanced_recognition_panel",
    "_meeting_vad_combo",
    "_vad_runtime_label",
    "_vad_download_status",
    "_vad_download_status_row",
    "_vad_download_btn",
    "_vad_download_row",
    "_quiet_speech_row",
    "_boundary_context_row",
    "_speaker_enabled_row",
    "_speaker_runtime_label",
    "_speaker_activity_row",
    "_recognition_reset_btn",
    "_meeting_source_combo",
    "_meeting_model_combo",
    "_meeting_defaults_hint",
    "_meeting_start_card_row",
    "_meeting_start_card_reset_btn",
    "_general_section_jumps",
    "_general_footer_note",
    "_general_tab_chain",
)

_MODEL_LEGACY_NAMES = (
    "_model_hero_host",
    "_model_hero_layout",
    "_model_hero_status",
    "_storage_summary_label",
    "_download_source_combo",
    "_cards_container",
    "_cards_layout",
    "_model_cards",
    "_model_cards_for_id",
)

_POLISH_LEGACY_NAMES = (
    "_llm_url_edit",
    "_llm_key_edit",
    "_llm_key_toggle",
    "_llm_model_edit",
    "_llm_test_btn",
    "_llm_test_status",
    "_llm_connection_section",
    "_llm_enable_row",
    "_llm_preview_divider",
    "_llm_preview_card",
    "_llm_prompt_container",
    "_llm_mode_combo",
    "_prompt_reset_btn",
    "_llm_prompt_edit",
    "_minutes_model_edit",
    "_minutes_test_btn",
    "_minutes_test_status",
    "_minutes_template_combo",
    "_minutes_manage_btn",
    "_minutes_language_combo",
    "_minutes_auto_row",
    "_minutes_auto_reset_btn",
    "_llm_prompt_timer",
    "_llm_prompt_mode",
)

_ABOUT_LEGACY_NAMES = (
    "_about_info_group",
    "_about_info_lay",
    "_about_version_label",
    "_about_update_status",
    "_about_update_checked_at",
    "_about_update_btn",
    "_about_auto_update_row",
    "_about_runtime_wrap",
    "_about_runtime_lay",
    "_about_summary_rows",
    "_about_paths_toggle",
    "_about_paths_wrap",
    "_about_paths_lay",
    "_about_feedback_group",
    "_about_feedback_hint",
    "_about_feedback_btn",
)

_LEGACY_PAGE_ATTRS: dict[str, tuple[str, str]] = {
    **{name: ("general_page", name[1:]) for name in _GENERAL_LEGACY_NAMES},
    **{name: ("model_page", name[1:]) for name in _MODEL_LEGACY_NAMES},
    **{name: ("polish_page", name[1:]) for name in _POLISH_LEGACY_NAMES},
    **{name: ("about_page", name[1:]) for name in _ABOUT_LEGACY_NAMES},
}


class SettingsWindow(QWidget):
    hotkey_updated = pyqtSignal(str)
    settings_changed = pyqtSignal()
    audio_options_changed = pyqtSignal()
    audio_recovery_requested = pyqtSignal()
    auto_start_changed = pyqtSignal(bool)
    sound_enabled_changed = pyqtSignal(bool)
    restore_clipboard_changed = pyqtSignal(bool)
    models_changed = pyqtSignal()
    recognition_changed = pyqtSignal()
    theme_changed = pyqtSignal(str)
    listen_bar_prefs_changed = pyqtSignal()
    hotkey_capture_started = pyqtSignal()
    hotkey_capture_ended = pyqtSignal()
    update_check_requested = pyqtSignal()
    update_install_requested = pyqtSignal()
    runtime_status_changed = pyqtSignal(object)  # RuntimeStatus
    navigation_requested = pyqtSignal(str)
    usage_hint_changed = pyqtSignal()
    closed = pyqtSignal()
    finished = pyqtSignal(int)

    # Strongly-typed subpage references
    general_page: GeneralSettingsPage
    model_page: ModelSettingsPage
    polish_page: PolishSettingsPage
    about_page: AboutSettingsPage

    def __init__(self, config: Config, parent: QWidget | None = None, pending_segment_count=None):
        super().__init__(parent)
        self._config = config
        self._pending_segment_count = pending_segment_count
        self._recording_busy = None
        self._dl_workers: dict[str, object] = {}
        self._download_threads: list[QThread] = []
        self._test_workers: list[QThread] = []
        self._llm_test_worker: QThread | None = None
        self._minutes_test_worker: QThread | None = None
        self._llm_test_stamp: tuple | None = None
        self._mic_test_recorder = AudioRecorder(self)
        self._mic_probe_active = False
        self._mic_probe_max = 0.0
        self._mic_probe_warning = ""
        self._last_audio_test_config: tuple[str, int, int] | None = None
        self._mic_probe_timer = QTimer(self)
        self._mic_probe_timer.setSingleShot(True)
        self._mic_probe_timer.timeout.connect(self._finish_mic_probe)
        self._loading = False
        self._runtime_status = coerce_runtime_status(RuntimeState.LOADING)
        self._segmentation_status: SegmentationStatus | None = None
        self._recognition_pending = False
        self._meeting_glossary_dirty = False
        self._glossary_skip_note = ""
        self._speaker_runtime_text = "尚未获取说话人增强运行状态"
        self._setup_window()
        self._setup_ui()
        self._connect_page_signals()
        self._load_settings()
        self.reapply_theme()

    def __getattr__(self, name: str):
        target = _LEGACY_PAGE_ATTRS.get(name)
        if target is not None:
            page = self.__dict__.get(target[0])
            if page is not None:
                return getattr(page, target[1])
        raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")

    def __setattr__(self, name: str, value: object) -> None:
        target = _LEGACY_PAGE_ATTRS.get(name)
        if target is not None:
            page = self.__dict__.get(target[0])
            if page is not None:
                setattr(page, target[1], value)
                return
        super().__setattr__(name, value)

    def _setup_window(self) -> None:
        self.setWindowTitle("设置")
        self.setStyleSheet(_settings_styles.WINDOW_CSS)

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        content_wrap = QWidget()
        self._content_wrap = content_wrap
        content_wrap.setObjectName("settingsContent")
        content_wrap.setStyleSheet("QWidget#settingsContent { background: transparent; }")
        content_lay = QVBoxLayout(content_wrap)
        content_lay.setContentsMargins(0, 0, 0, 0)
        content_lay.setSpacing(0)

        pages_host = QWidget()
        self._pages_host = pages_host
        pages_host.setObjectName("settingsPagesHost")
        pages_host.setStyleSheet("QWidget#settingsPagesHost { background: transparent; }")
        pages_lay = QHBoxLayout(pages_host)
        pages_lay.setContentsMargins(6, 8, 6, 12)
        pages_lay.setSpacing(0)

        self._pages = QStackedWidget()
        self._pages.setObjectName("settingsPages")
        self._pages.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._pages.setStyleSheet("QStackedWidget#settingsPages { background: transparent; }")

        self.general_page = GeneralSettingsPage()
        self.model_page = ModelSettingsPage()
        self.polish_page = PolishSettingsPage(self._config)
        self.about_page = AboutSettingsPage(self._config)

        self._pages.addWidget(self.general_page)
        self._pages.addWidget(self.model_page)
        self._pages.addWidget(self.polish_page)
        self._pages.addWidget(self.about_page)
        pages_lay.addWidget(self._pages, 1)
        content_lay.addWidget(pages_host, 1)
        root.addWidget(content_wrap, 1)

    def _connect_page_signals(self) -> None:
        gp = self.general_page
        gp.trigger_mode_toggled.connect(self._on_trigger_mode_radio_toggled)
        gp.hotkey_capture_started.connect(self.hotkey_capture_started.emit)
        gp.hotkey_capture_ended.connect(self.hotkey_capture_ended.emit)
        gp.hotkey_changed.connect(self._apply_hotkey_setting)
        gp.hotkey_reset_requested.connect(self._restore_default_hotkey)
        gp.esc_stop_toggled.connect(self._on_esc_stop_toggled)
        gp.input_source_toggled.connect(self._on_input_source_radio_toggled)
        gp.output_mode_changed.connect(self._on_output_mode_changed)
        gp.mic_probe_requested.connect(self._run_mic_probe)
        gp.audio_devices_reset_requested.connect(self._reset_audio_devices_to_auto)
        gp.advanced_audio_toggled.connect(self._toggle_advanced_audio)
        gp.audio_device_changed.connect(self._on_audio_device_changed)
        gp.audio_devices_refresh_requested.connect(self._refresh_audio_device_lists)
        gp.separate_lanes_toggled.connect(self._on_separate_lanes_toggled)
        gp.recognition_setting_changed.connect(self._on_recognition_setting_changed)
        gp.glossary_text_changed.connect(self._on_meeting_glossary_changed)
        gp.glossary_editing_finished.connect(self._flush_meeting_glossary)
        gp.vad_download_requested.connect(self._download_shared_vad)
        gp.recognition_reset_requested.connect(self._restore_default_recognition)
        gp.meeting_setting_changed.connect(self._on_meeting_setting_changed)
        gp.meeting_start_card_reset_requested.connect(self._restore_default_meeting_start_card)
        gp.llm_proxy_changed.connect(self._on_llm_proxy_changed)
        gp.llm_proxy_suffix_changed.connect(self._on_llm_proxy_suffix_changed)
        gp.theme_mode_changed.connect(self._on_theme_mode_changed)
        gp.listen_bar_placement_changed.connect(self._on_listen_bar_placement_changed)
        gp.listen_bar_autohide_toggled.connect(self._on_listen_bar_autohide_toggled)
        gp.auto_start_toggled.connect(self._on_auto_start_toggled)
        gp.sound_toggled.connect(self._on_sound_toggled)
        gp.restore_clipboard_toggled.connect(self._on_restore_clipboard_toggled)
        gp.history_enabled_toggled.connect(self._on_history_enabled_toggled)
        gp.history_limits_changed.connect(self._on_history_limits_changed)
        gp.recovery_toggled.connect(self._on_recovery_toggled)
        gp.audio_recovery_requested.connect(self.audio_recovery_requested.emit)

        mp = self.model_page
        mp.change_model_dir_requested.connect(self._change_model_dir)
        mp.download_source_changed.connect(self._on_download_source_changed)
        mp.card_action_requested.connect(self._on_card_action)

        pp = self.polish_page
        pp.llm_enable_toggled.connect(self._on_llm_enable_toggled)
        pp.llm_mode_changed.connect(self._on_llm_mode_changed)
        pp.llm_fields_edited.connect(self._flush_llm_fields)
        pp.prompt_reset_requested.connect(self._reset_prompt)
        pp.llm_test_requested.connect(self._test_llm)
        pp.minutes_fields_edited.connect(self._flush_minutes_fields)
        pp.minutes_test_requested.connect(self._test_minutes_model)
        pp.minutes_auto_toggled.connect(self._on_minutes_auto_toggled)
        pp.minutes_auto_reset_requested.connect(self._restore_default_auto_minutes)
        pp.manage_templates_requested.connect(self._manage_minutes_templates)

        ap = self.about_page
        ap.update_check_requested.connect(self.update_check_requested.emit)
        ap.update_install_requested.connect(self.update_install_requested.emit)
        ap.auto_update_toggled.connect(self._on_about_auto_update_toggled)
        ap.setting_navigation_requested.connect(self._open_about_setting)

    def reapply_theme(self) -> None:
        """Refresh dialog chrome after design tokens were activated."""
        from sayink.ui import design_tokens as tok
        from sayink.ui.settings_components import reapply_subtree as _reapply_subtree

        self.setStyleSheet(_settings_styles.WINDOW_CSS)
        self._paint_model_hero_status()
        if isinstance(self, SettingsWindow):
            self._content_wrap.setStyleSheet("QWidget#settingsContent { background: transparent; }")
            self._pages_host.setStyleSheet("QWidget#settingsPagesHost { background: transparent; }")
            self._pages.setStyleSheet("QStackedWidget#settingsPages { background: transparent; }")
        _reapply_subtree(self)
        if isinstance(self, SettingsWindow):
            self.general_page.apply_theme()
            self.model_page.apply_theme()
            self.polish_page.apply_theme()
            self.about_page.apply_theme()
            self._refresh_active_model_hero()
        else:
            self._mic_level_bar.setStyleSheet(
                f"QProgressBar {{ border: none; background: {tok.SURFACE_PEARL}; border-radius: 3px; }}"
                f"QProgressBar::chunk {{ background: {tok.ACCENT}; border-radius: 3px; }}"
            )
        self._refresh_recognition_feedback()
        self._refresh_output_controls()
        self._refresh_meeting_defaults()

    def show_page(self, index: int) -> None:
        self._flush_meeting_glossary()
        self._pages.setCurrentIndex(index)
        if index == 2:
            self.polish_page.refresh_templates(self._config)
        if index == 3:
            self._refresh_about_info()
            self.about_page.refresh_update_check_time(self._config)

    def _open_about_setting(self, page: str, focus: str = "") -> None:
        self.show_page({"general": 0, "engine": 1, "polish": 2}[page])
        self.navigation_requested.emit(page)
        if focus == "hotkey":
            self.general_page.ensureWidgetVisible(self.general_page.hotkey_edit)
            self.general_page.hotkey_edit.setFocus(Qt.FocusReason.TabFocusReason)
        elif focus == "polish":
            self.polish_page.ensureWidgetVisible(self.polish_page.llm_enable_row)
            self.polish_page.llm_enable_row.setFocus(Qt.FocusReason.TabFocusReason)

    def hideEvent(self, event) -> None:
        self._flush_meeting_glossary()
        super().hideEvent(event)
        if self.parent() is None:
            self.closed.emit()
            self.finished.emit(0)

    # ── Runtime & Recognition Status ───────────────────────

    def set_runtime_status(
        self,
        state_or_label: RuntimeState | str,
        label: str | None = None,
    ) -> None:
        self._runtime_status = coerce_runtime_status(state_or_label, label)
        self._paint_model_hero_status()
        self.runtime_status_changed.emit(self._runtime_status)

    def _paint_model_hero_status(self) -> None:
        self.model_page.paint_hero_status(
            self._runtime_status,
            recognition_pending=self._recognition_pending,
        )

    def set_recognition_runtime_status(
        self,
        status: SegmentationStatus,
        *,
        speaker_status: str,
        pending: bool = False,
    ) -> None:
        self._segmentation_status = status
        self._speaker_runtime_text = speaker_status
        self._recognition_pending = pending
        self._paint_model_hero_status()
        self._refresh_recognition_feedback()

    def _refresh_recognition_feedback(self) -> None:
        from sayink.speech_recognizer import AUX_MODEL_SILERO_VAD

        self.general_page.refresh_recognition_feedback(
            self._config,
            segmentation_status=self._segmentation_status,
            speaker_runtime_text=self._speaker_runtime_text,
            recognition_pending=self._recognition_pending,
            glossary_skip_note=self._glossary_skip_note,
            vad_downloading=AUX_MODEL_SILERO_VAD in self._dl_workers,
        )

    # ── Engine / Model Management ──────────────────────────

    def _download_shared_vad(self) -> None:
        from sayink.speech_recognizer import AUX_MODEL_SILERO_VAD

        self._start_download(AUX_MODEL_SILERO_VAD)

    def _on_shared_download_progress(self, model_id: str, pct: int) -> None:
        from sayink.speech_recognizer import AUX_MODEL_SILERO_VAD

        if model_id == AUX_MODEL_SILERO_VAD:
            self.general_page.update_vad_download_progress(pct)

    def _refresh_dir_label(self) -> None:
        self.model_page.refresh_storage_summary(self._config)

    def _change_model_dir(self) -> None:
        cur = str(self._config.models_dir)
        d = QFileDialog.getExistingDirectory(self, "选择模型存储目录", cur)
        if not d:
            return
        new_p = Path(d)
        old_p = self._config.models_dir
        if new_p == old_p:
            return
        from sayink.speech_recognizer import is_dir_writable, model_dir_names, set_models_dir

        if not is_dir_writable(new_p):
            QMessageBox.warning(self, "无法使用该目录", f"没有写入权限或无法创建：\n{new_p}")
            return
        movable = [old_p / name for name in model_dir_names() if (old_p / name).is_dir()]
        if movable:
            box = QMessageBox(self)
            box.setWindowTitle("迁移已下载的模型")
            box.setIcon(QMessageBox.Icon.Question)
            box.setText(f"要把 {len(movable)} 个已下载的模型文件夹移动到新目录吗？")
            box.setInformativeText("只移动 SayInk 自己的模型文件夹，原目录中的其他内容不会被改动。选择「不移动」则需要重新下载。")
            move_btn = box.addButton("移动", QMessageBox.ButtonRole.AcceptRole)
            box.addButton("不移动", QMessageBox.ButtonRole.DestructiveRole)
            cancel_btn = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(move_btn)
            box.exec()
            if box.clickedButton() == cancel_btn:
                return
            if box.clickedButton() != move_btn:
                movable = []
        moved = 0
        failed: list[str] = []
        for item in movable:
            target = new_p / item.name
            if target.exists():
                continue
            try:
                shutil.move(str(item), str(target))
                moved += 1
            except OSError as exc:
                log.warning("模型目录迁移失败 %s: %s", item.name, exc)
                failed.append(item.name)
        self._config.set("stt.models_dir", str(new_p))
        set_models_dir(new_p)
        self._refresh_dir_label()
        self._rebuild_model_cards()
        if moved > 0:
            QMessageBox.information(self, "完成", f"已将 {moved} 个模型迁移到新目录。")
        if failed:
            QMessageBox.warning(
                self,
                "部分迁移失败",
                "以下模型未能迁移，请手动复制后重试：\n" + "\n".join(failed),
            )

    def _refresh_active_model_hero(self) -> None:
        self._refresh_meeting_defaults()
        self.model_page.refresh_active_hero(
            self._config,
            self._runtime_status,
            recognition_pending=self._recognition_pending,
        )

    def _rebuild_model_cards(self) -> None:
        self._refresh_meeting_defaults()
        self.model_page.rebuild_cards(
            self._config,
            self._runtime_status,
            recognition_pending=self._recognition_pending,
        )

    def _on_card_action(self, model_id: str, action: str) -> None:
        if action == "select":
            self._config.set("stt.model_id", model_id)
            self._rebuild_model_cards()
            self.models_changed.emit()
        elif action == "download":
            self._start_download(model_id)
        elif action == "cancel":
            worker = self._dl_workers.get(model_id)
            if worker is not None:
                worker.cancel()
        elif action == "delete":
            self._delete_model(model_id)

    def _start_download(self, model_id: str) -> None:
        if model_id in self._dl_workers:
            return
        from sayink.speech_recognizer import ModelDownloadWorker

        worker = ModelDownloadWorker(
            model_id,
            source=self._config.get("stt.download_source", "auto"),
        )
        self._dl_workers[model_id] = worker
        if isinstance(worker, QThread):
            self._download_threads.append(worker)
            worker.finished.connect(lambda w=worker: self._reap_download_thread(w))
        self._on_shared_download_progress(model_id, 0)
        self.model_page.update_download_progress(model_id, 0)
        worker.progress.connect(lambda pct, mid=model_id: self.model_page.update_download_progress(mid, pct))
        worker.progress.connect(lambda pct, mid=model_id: self._on_shared_download_progress(mid, pct))
        worker.finished_ok.connect(lambda mid: self._on_dl_done(mid))
        worker.error.connect(
            lambda msg, mid=model_id: self._on_dl_error(msg, self.model_page.model_cards.get(mid), mid)
        )
        worker.start()

    def _reap_download_thread(self, worker) -> None:
        if worker.isRunning():
            return
        if worker in self._download_threads:
            self._download_threads.remove(worker)
        for model_id, active in list(self._dl_workers.items()):
            if active is worker:
                self._dl_workers.pop(model_id, None)

    def _on_dl_done(self, model_id: str) -> None:
        worker = self._dl_workers.pop(model_id, None)
        if worker is not None and getattr(worker, "_cancelled", False) is True:
            return
        from sayink.speech_recognizer import get_downloaded_models, get_model_info, is_aux_model

        downloaded = get_downloaded_models()
        if len(downloaded) == 1 and downloaded[0] == model_id:
            self._config.set("stt.model_id", model_id)
        self._rebuild_model_cards()
        self.models_changed.emit()
        self._refresh_recognition_feedback()
        info = get_model_info(model_id)
        name = info["name"] if info else model_id
        if is_aux_model(model_id):
            text = (
                f"{name} 已下载。按识别设置启用；正在录音或处理语音时，"
                f"待本场结束后生效。可在 {ADVANCED_RECOGNITION_PATH} 查看实际加载状态。"
            )
        elif self._config.get("stt.model_id", "") == model_id:
            if self._recognition_pending:
                text = f"{name} 已下载，待本场结束且已录语音处理完成后载入。可在通用 → 识别查看生效状态。"
            else:
                text = f"{name} 已下载，正在载入内存（约需数十秒）。载入完成前请勿开始录音。"
        else:
            text = f"{name} 已下载。当前仍使用原来的模型；点击该模型卡片上的「使用此模型」即可切换。"
        QMessageBox.information(self, "完成", text)

    def _on_dl_error(self, msg: str, card, model_id: str = "") -> None:
        worker = self._dl_workers.pop(model_id or (card._model_id if card else ""), None)
        cancelled = worker is not None and getattr(worker, "_cancelled", False) is True
        if card:
            if cancelled:
                card.set_download_cancelled()
            else:
                card.set_download_error(msg)
        elif not cancelled:
            QMessageBox.warning(self, "下载失败", msg)
        self._refresh_recognition_feedback()

    def _delete_model(self, model_id: str) -> None:
        from sayink.speech_recognizer import delete_model, get_downloaded_models, get_model_info, is_aux_model

        info = get_model_info(model_id)
        name = info["name"] if info else model_id
        if not self._confirm(
            "删除模型",
            f"确定删除「{name}」吗？删除后需重新下载。",
            accept="删除",
            destructive=True,
        ):
            return
        active_id = "" if is_aux_model(model_id) else self._config.get("stt.model_id", "")
        if not delete_model(model_id):
            QMessageBox.warning(
                self,
                "删除模型",
                f"未能删除「{name}」。它可能是安装包自带的模型、正被占用，"
                "或所在目录没有写入权限。",
            )
            self._rebuild_model_cards()
            return
        if active_id == model_id:
            remaining = get_downloaded_models()
            self._config.set("stt.model_id", remaining[0] if remaining else "")
        self._rebuild_model_cards()
        self.models_changed.emit()

    # ── Polish / AI Service ────────────────────────────────

    def _on_llm_enable_toggled(self, enabled: bool) -> None:
        self.polish_page.set_prompt_section_visible(enabled)
        if self._loading:
            return
        self._config.set("llm.enabled", enabled)
        self._flush_llm_fields()

    def _llm_missing_fields(self) -> list[str]:
        return self.polish_page.missing_fields()

    def _set_llm_status(self, label: QLabel, text: str, tone: str) -> None:
        raw_suffixes = self.general_page.llm_proxy_suffix_edit.text()
        self.polish_page.set_status(label, text, tone, proxy_suffix_raw=raw_suffixes)

    def _update_llm_configuration_status(self) -> None:
        status_lbl = self.polish_page.llm_test_status
        if getattr(self._config, "secret_persist_failed", False) is True:
            self._set_llm_status(
                status_lbl,
                "API 密钥未能存入 Windows 凭据管理器，仅本次运行有效，"
                "也没有写入配置文件；重启后需重新填写。",
                "error",
            )
            return
        if getattr(self._config, "secret_read_failed", False) is True:
            self._set_llm_status(
                status_lbl,
                "无法从 Windows 凭据管理器读取已保存的 API 密钥，这里暂时显示为空；"
                "重启后仍为空请重新填写。",
                "error",
            )
            return
        missing = self._llm_missing_fields()
        if missing and self.polish_page.llm_enable_row.isChecked():
            self._set_llm_status(
                status_lbl,
                f"未配置：缺少{'、'.join(missing)}。转写会直接输出原文。",
                "idle",
            )
        elif missing:
            self._set_llm_status(
                status_lbl,
                f"尚未填写：{'、'.join(missing)}。文字润色和会议纪要都要用到这组配置。",
                "idle",
            )
        else:
            self._show_completed_llm_status()

    def _reset_prompt(self) -> None:
        self.polish_page.llm_prompt_edit.clear()
        self._flush_llm_fields()

    def _on_llm_mode_changed(self, _index: int = 0) -> None:
        mode = self.polish_page.sync_mode_preview()
        if self._loading:
            return
        previous = self.polish_page.llm_prompt_mode
        if previous != mode:
            from sayink.text_polisher import prompt_config_key

            self._config.set(
                prompt_config_key(previous),
                self.polish_page.llm_prompt_edit.toPlainText().strip(),
            )
            self._loading = True
            try:
                self.polish_page.load_prompt_for_mode(self._config, mode)
            finally:
                self._loading = False
        self._flush_llm_fields()

    def _manage_minutes_templates(self) -> None:
        if self.polish_page.open_templates_dialog(self._config, self):
            self._flush_minutes_fields()

    # ── About Page Coordination ────────────────────────────

    def _refresh_about_runtime_rows(self) -> None:
        self.about_page.refresh_runtime_rows(self._config)

    def _refresh_about_info(self) -> None:
        self.about_page.refresh_info(self._config)

    def _on_about_auto_update_toggled(self, checked: bool) -> None:
        if self._loading:
            return
        self._config.set("update.auto_check", bool(checked))

    def set_update_status(self, text: str, *, action: str = "check") -> None:
        self.about_page.set_update_status(self._config, text, action=action)

    # ── Audio Devices & Mic Probe ──────────────────────────

    def _selected_input_source(self) -> str:
        return self.general_page.selected_input_source()

    def _selected_trigger_mode(self) -> str:
        return self.general_page.selected_trigger_mode()

    def _set_mic_test_status(self, text: str) -> None:
        self.general_page.mic_test_status.setText(text)

    def _sync_source_device_widgets(self) -> None:
        self.general_page.sync_source_device_controls()
        self._refresh_audio_device_summary()

    def _saved_device_indices(self) -> tuple[int, int]:
        return (
            int(self._config.get("audio.mic_device_index", -1)),
            int(self._config.get("audio.system_device_index", -1)),
        )

    def _sync_saved_audio_devices(self, *, refresh_devices: bool) -> None:
        mic, system = self._saved_device_indices()
        automatic = mic < 0 and system < 0
        missing = not self.general_page.saved_devices_are_listed(mic, system)
        if refresh_devices and not automatic and missing:
            self._refresh_audio_device_lists()
        else:
            self.general_page.ensure_automatic_device_items()
        mic_ok = set_combo_by_data(self.general_page.mic_device_combo, mic)
        sys_ok = set_combo_by_data(self.general_page.system_device_combo, system)
        if refresh_devices and (not mic_ok or not sys_ok):
            self._reset_audio_devices_to_auto()

    def _reset_audio_devices_to_auto(self) -> None:
        self._cancel_mic_probe_if_active()
        if not self._loading and not self._confirm_discard_pending():
            return
        was_loading = self._loading
        self._loading = True
        set_combo_by_data(self.general_page.mic_device_combo, -1)
        set_combo_by_data(self.general_page.system_device_combo, -1)
        self._loading = was_loading
        if not was_loading:
            self._persist_runtime_settings()
        self._refresh_audio_device_summary()
        self._set_mic_test_status("已恢复为「自动选择」，请再点「测试声音」。")

    def _refresh_audio_device_lists(self) -> None:
        self.general_page.populate_audio_device_combos(
            list_microphone_devices,
            list_system_capture_devices_for_settings,
        )
        self._refresh_audio_device_summary()

    def _toggle_advanced_audio(self, visible: bool) -> None:
        def ensure_listed() -> None:
            if self.general_page.mic_device_combo.count() <= 1:
                self._refresh_audio_device_lists()

        self.general_page.set_advanced_audio_expanded(visible, before_show=ensure_listed)

    def _refresh_audio_device_summary(self) -> None:
        current = self.general_page.current_audio_probe_config()
        if current != self._last_audio_test_config:
            self._cancel_mic_probe_if_active()
            self._last_audio_test_config = current
            self._set_mic_test_status("测试会临时采集约 2 秒声音，不保存音频或转写文本。")
        self.general_page.update_audio_device_summary()

    def _disconnect_mic_probe_signals(self) -> None:
        for sig, slot in (
            (self._mic_test_recorder.volume_changed, self._on_mic_probe_volume),
            (self._mic_test_recorder.error, self._on_mic_probe_error),
            (self._mic_test_recorder.warning, self._on_mic_probe_warning),
        ):
            try:
                sig.disconnect(slot)
            except TypeError:
                pass

    def set_recording_busy_checker(self, checker) -> None:
        self._recording_busy = checker

    def _run_mic_probe(self) -> None:
        if self._mic_probe_active or self._mic_test_recorder.is_recording:
            return
        busy = self._recording_busy
        if callable(busy) and busy():
            self._set_mic_test_status("请先暂停或结束当前录音或会议，再测试声音。")
            return
        gp = self.general_page
        if gp.mic_device_combo.count() <= 1 or gp.system_device_combo.count() <= 1:
            self._refresh_audio_device_lists()
        src, mic_idx, sys_idx = gp.current_audio_probe_config()
        if mic_idx >= 0 and gp.mic_device_combo.findData(mic_idx) < 0:
            mic_idx = -1
        if sys_idx >= 0 and gp.system_device_combo.findData(sys_idx) < 0:
            sys_idx = -1
        sys_idx = sanitize_system_device_index(sys_idx)
        if sys_idx < 0:
            set_combo_by_data(gp.system_device_combo, -1)
        self._mic_test_recorder.configure(
            input_source=src,
            mic_device_index=mic_idx,
            system_device_index=sys_idx,
        )
        self._mic_probe_active = True
        self._mic_probe_max = 0.0
        self._mic_probe_warning = ""
        gp.set_probe_controls_enabled(False)
        gp.mic_level_bar.setValue(0)
        gp.mic_level_bar.show()
        instruction = {
            INPUT_SOURCE_MICROPHONE: "请对麦克风说话",
            INPUT_SOURCE_SYSTEM: "请播放一段电脑声音",
            INPUT_SOURCE_MIXED: "请说话并播放一段电脑声音",
        }.get(src, "请对麦克风说话")
        self._set_mic_test_status(f"正在测试…{instruction}")
        self._mic_test_recorder.volume_changed.connect(self._on_mic_probe_volume)
        self._mic_test_recorder.error.connect(self._on_mic_probe_error)
        self._mic_test_recorder.warning.connect(self._on_mic_probe_warning)
        self._mic_test_recorder.start()
        if self._mic_probe_active:
            devices = self._mic_test_recorder.capture_device_labels
            if devices:
                gp.audio_device_summary.setText("本次测试设备：\n" + "\n".join(devices))
            self._mic_probe_timer.start(2000)

    def _on_mic_probe_volume(self, volume: float) -> None:
        if not self._mic_probe_active:
            return
        self._mic_probe_max = max(self._mic_probe_max, float(volume))
        self.general_page.mic_level_bar.setValue(
            round(min(1.0, max(0.0, float(volume)) ** 0.5 * 2) * 100)
        )

    def _on_mic_probe_error(self, msg: str) -> None:
        if not self._mic_probe_active:
            return
        self._mic_probe_active = False
        self._mic_probe_timer.stop()
        self._disconnect_mic_probe_signals()
        if self._mic_test_recorder.is_recording:
            self._mic_test_recorder.cancel()
        self.general_page.set_probe_controls_enabled(True)
        self.general_page.mic_level_bar.hide()
        self._set_mic_test_status(f"测试失败：{msg}。可展开「手动选择音频设备」后重试。")

    def _on_mic_probe_warning(self, msg: str) -> None:
        if not self._mic_probe_active:
            return
        self._mic_probe_warning = msg
        self._set_mic_test_status(msg)

    def _finish_mic_probe(self) -> None:
        if not self._mic_probe_active:
            return
        self._mic_probe_active = False
        self._mic_probe_timer.stop()
        self._disconnect_mic_probe_signals()
        if self._mic_test_recorder.is_recording:
            self._mic_test_recorder.stop()
        self.general_page.set_probe_controls_enabled(True)
        self.general_page.mic_level_bar.hide()
        peak = self._mic_probe_max
        warn = self._mic_probe_warning or self._mic_test_recorder.last_start_warning
        if peak >= 0.0015:
            base = "已检测到声音。"
            if peak < 0.005:
                base += "输入音量偏低，可靠近麦克风或提高来源音量。"
            elif peak > 0.8:
                base += "输入音量偏高，建议降低来源音量后重测。"
            if self._selected_input_source() == INPUT_SOURCE_MIXED:
                base += "混合输入请分别说话、播放声音，确认两路都能采集。"
            self._set_mic_test_status(f"{base} {warn}".strip() if warn else base)
        else:
            self._set_mic_test_status(
                "未检测到明显声音。请先说话或播放声音，再测试；也可恢复自动选择或手动选择设备。"
            )

    def _cancel_mic_probe_if_active(self) -> None:
        if not self._mic_probe_active and not self._mic_test_recorder.is_recording:
            return
        self._mic_probe_active = False
        self._mic_probe_timer.stop()
        self._disconnect_mic_probe_signals()
        if self._mic_test_recorder.is_recording:
            self._mic_test_recorder.cancel()
        self.general_page.set_probe_controls_enabled(True)
        self.general_page.mic_level_bar.hide()

    # ── Load / Save & Instant Apply ────────────────────────

    def _load_settings_fields(self, *, refresh_devices: bool, heavy: bool) -> None:
        self.general_page.load_config(self._config)
        self._refresh_audio_device_summary()
        self._refresh_hotkey_hint()
        self._sync_saved_audio_devices(refresh_devices=refresh_devices)

        self.model_page.load_config(self._config)
        cards_changed = not self.model_page.cards_match_config(self._config)
        if cards_changed:
            self._rebuild_model_cards()
        self.about_page.load_config(self._config)
        if heavy or cards_changed:
            self._refresh_about_info()

        self.polish_page.load_config(self._config)
        self._load_meeting_settings()

    def _load_settings(self, *, refresh_devices: bool = True, heavy: bool = True) -> None:
        self._loading = True
        try:
            self._load_settings_fields(refresh_devices=refresh_devices, heavy=heavy)
        finally:
            self._loading = False
        self._refresh_output_controls()
        self._refresh_audio_device_summary()
        self._refresh_meeting_defaults()
        self._update_llm_configuration_status()
        self.usage_hint_changed.emit()

    def _load_meeting_settings(self) -> None:
        self.general_page.load_recognition_and_meeting(self._config)
        self._meeting_glossary_dirty = False
        self._glossary_skip_note = ""
        self.polish_page.load_minutes_config(self._config)
        self._refresh_recognition_feedback()

    def set_pending_segment_counter(self, counter) -> None:
        self._pending_segment_count = counter

    def _confirm_discard_pending(self) -> bool:
        if self._pending_segment_count is None:
            return True
        pending = int(self._pending_segment_count())
        if pending <= 0:
            return True
        return self._confirm(
            "待识别语音",
            f"仍有 {pending} 段语音等待识别，应用此更改将丢弃这些片段。\n是否继续？",
            accept="丢弃并应用",
            destructive=True,
        )

    def _confirm(
        self,
        title: str,
        text: str,
        *,
        accept: str,
        cancel: str = "取消",
        destructive: bool = False,
    ) -> bool:
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setText(text)
        box.setIcon(QMessageBox.Icon.Question)
        role = QMessageBox.ButtonRole.DestructiveRole if destructive else QMessageBox.ButtonRole.AcceptRole
        accept_btn = box.addButton(accept, role)
        cancel_btn = box.addButton(cancel, QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel_btn)
        box.setEscapeButton(cancel_btn)
        box.exec()
        return box.clickedButton() is accept_btn

    def _revert_input_source_radios(self) -> None:
        self._loading = True
        try:
            self.general_page.apply_input_source_radios(
                self._config.get("audio.input_source", INPUT_SOURCE_MICROPHONE)
            )
            self._refresh_audio_device_summary()
        finally:
            self._loading = False

    def _revert_trigger_mode_radios(self) -> None:
        self._loading = True
        try:
            self.general_page.apply_trigger_mode_radios(
                self._config.get("audio.trigger_mode", TRIGGER_MODE_HOTKEY)
            )
        finally:
            self._loading = False

    def _persist_runtime_settings(self) -> None:
        gp = self.general_page
        source = gp.selected_input_source()
        if source != self._config.get("audio.input_source", INPUT_SOURCE_MICROPHONE):
            self._config.follow_input_source_for_output(source)
        self._config.set("audio.input_source", source)
        self._config.set("audio.trigger_mode", gp.selected_trigger_mode())
        self._config.set("audio.mic_device_index", _device_index(gp.mic_device_combo.currentData()))
        sys_idx = _device_index(gp.system_device_combo.currentData())
        if sys_idx != _device_index(self._config.get("audio.system_device_index", -1)):
            sys_idx = sanitize_system_device_index(sys_idx)
        self._config.set("audio.system_device_index", sys_idx)
        if sys_idx < 0:
            set_combo_by_data(gp.system_device_combo, -1)
        self._config.save_immediate()
        self.settings_changed.emit()
        self._refresh_hotkey_hint()
        self._refresh_about_runtime_rows()

    def _refresh_hotkey_hint(self) -> None:
        self.general_page.refresh_hotkey_hint(self._config)
        self._refresh_output_controls()
        self._refresh_meeting_defaults()

    def _refresh_output_controls(self) -> None:
        self.general_page.refresh_output_controls(self._config)

    def _refresh_meeting_defaults(self) -> None:
        self.general_page.refresh_meeting_defaults(self._config)

    def _on_llm_proxy_changed(self, _index: int = 0) -> None:
        if not self._loading:
            self._config.set("llm.proxy_mode", self.general_page.llm_proxy_combo.currentData())
            self._llm_test_stamp = None
            self._update_llm_configuration_status()
            self._set_llm_status(
                self.polish_page.minutes_test_status,
                "代理模式已更改，请重新测试连接。",
                "idle",
            )

    def _on_llm_proxy_suffix_changed(self, _text: str = "") -> None:
        if not self._loading:
            self._config.set("llm.proxy_bypass_suffixes", self.general_page.llm_proxy_suffix_edit.text())
            self._llm_test_stamp = None
            self._update_llm_configuration_status()

    def _on_theme_mode_changed(self, _index: int = 0) -> None:
        if self._loading:
            return
        mode = normalize_theme_mode(self.general_page.theme_combo.currentData())
        self._config.set("appearance.theme_mode", mode)
        self._config.save_immediate()
        self.theme_changed.emit(mode)

    def _on_listen_bar_placement_changed(self, _index: int = 0) -> None:
        if self._loading:
            return
        from sayink.ui.island_chrome import normalize_listen_bar_placement

        placement = normalize_listen_bar_placement(self.general_page.listen_bar_combo.currentData())
        self._config.set("appearance.listen_bar_placement", placement)
        self.listen_bar_prefs_changed.emit()

    def _on_listen_bar_autohide_toggled(self, checked: bool) -> None:
        if self._loading:
            return
        self._config.set("appearance.listen_bar_autohide", bool(checked))
        self.listen_bar_prefs_changed.emit()

    def set_listen_bar_placement(self, placement: str) -> None:
        from sayink.ui.island_chrome import normalize_listen_bar_placement

        combo = self.general_page.listen_bar_combo
        idx = combo.findData(normalize_listen_bar_placement(placement))
        if idx < 0 or combo.currentIndex() == idx:
            return
        was_loading = self._loading
        self._loading = True
        try:
            combo.setCurrentIndex(idx)
        finally:
            self._loading = was_loading

    def _on_auto_start_toggled(self, checked: bool) -> None:
        if self._loading:
            return
        self._config.set("auto_start", checked)
        self.auto_start_changed.emit(checked)

    def set_auto_start(self, enabled: bool) -> None:
        row = self.general_page.auto_start_row
        if row.isChecked() == enabled:
            return
        was_loading = self._loading
        self._loading = True
        try:
            row.setChecked(enabled)
        finally:
            self._loading = was_loading

    def _on_sound_toggled(self, checked: bool) -> None:
        if self._loading:
            return
        self._config.set("sound_enabled", checked)
        self.sound_enabled_changed.emit(checked)

    def _on_download_source_changed(self, _arg: object = None) -> None:
        if self._loading:
            return
        value = self.model_page.download_source_combo.currentData() or "auto"
        self._config.set("stt.download_source", value)

    def _on_esc_stop_toggled(self, checked: bool) -> None:
        if self._loading:
            return
        self._config.set("audio.esc_stops_continuous", bool(checked))
        self._refresh_hotkey_hint()
        self.usage_hint_changed.emit()

    def _on_restore_clipboard_toggled(self, checked: bool) -> None:
        if self._loading:
            return
        self._config.set("output.restore_clipboard", checked)
        self.restore_clipboard_changed.emit(checked)

    def _on_output_mode_changed(self, _index: int = 0) -> None:
        if self._loading:
            return
        gp = self.general_page

        def change() -> None:
            choice = gp.output_mode_combo.currentData()
            if choice == "auto":
                self._config.set("output.follow_input_source", True)
                self._config.follow_input_source_for_output(gp.selected_input_source())
            else:
                self._config.set_output_mode(choice, auto=False)
            self._config.save_immediate()
            self._refresh_hotkey_hint()
            self._refresh_about_runtime_rows()
            self.usage_hint_changed.emit()

        without_scroll_jump(gp.output_mode_combo, change)

    def _on_history_enabled_toggled(self, checked: bool) -> None:
        gp = self.general_page

        def change() -> None:
            gp.refresh_history_controls()
            if self._loading:
                return
            self._config.set("history.enabled", checked)
            self._refresh_hotkey_hint()
            self._refresh_about_runtime_rows()
            self.usage_hint_changed.emit()

        without_scroll_jump(gp.history_enabled_row, change)

    def _on_recovery_toggled(self, checked: bool) -> None:
        if not self._loading:
            self._config.set("audio.recovery_enabled", bool(checked))
            self.audio_options_changed.emit()

    def _on_separate_lanes_toggled(self, checked: bool) -> None:
        if not self._loading:
            self._config.set("audio.separate_lanes", bool(checked))
            self.audio_options_changed.emit()

    def _on_history_limits_changed(self, _value: int = 0) -> None:
        if self._loading:
            return
        gp = self.general_page
        self._config.set("history.retention_days", gp.history_retention_days_spin.value())
        self._config.set("history.max_entries", gp.history_max_entries_spin.value())

    def _on_meeting_setting_changed(self, *_args) -> None:
        if self._loading:
            return
        gp = self.general_page
        self._config.set("meeting.input_source", str(gp.meeting_source_combo.currentData() or ""))
        self._config.set("meeting.model_id", str(gp.meeting_model_combo.currentData() or ""))
        self._config.set("meeting.show_start_card", bool(gp.meeting_start_card_row.isChecked()))
        self._refresh_meeting_defaults()

    def _on_recognition_setting_changed(self, *_args) -> None:
        if self._loading:
            return
        gp = self.general_page
        vad = str(gp.meeting_vad_combo.currentData() or "silero")
        language = str(gp.meeting_language_combo.currentData() or "auto")
        speaker = gp.speaker_enabled_row.isChecked()
        activity = gp.speaker_activity_row.isChecked()
        quiet = gp.quiet_speech_row.isChecked()
        boundary = gp.boundary_context_row.isChecked()
        changed = (
            vad != self._config.get("stt.vad", "silero")
            or language != self._config.get("stt.language", "auto")
            or speaker != self._config.get("stt.speaker_model_enabled", True)
            or activity != self._config.get("stt.speaker_activity_enabled", False)
            or quiet != self._config.get("stt.quiet_speech_enabled", False)
            or boundary != self._config.get("stt.boundary_context_enabled", False)
        )
        self._config.set("stt.vad", vad)
        self._config.set("stt.language", language)
        self._config.set("stt.speaker_model_enabled", speaker)
        self._config.set("stt.speaker_activity_enabled", activity)
        self._config.set("stt.quiet_speech_enabled", quiet)
        self._config.set("stt.boundary_context_enabled", boundary)
        if changed:
            self.recognition_changed.emit()
        self._refresh_recognition_feedback()

    def _on_meeting_glossary_changed(self) -> None:
        if not self._loading:
            self._meeting_glossary_dirty = True

    def _flush_meeting_glossary(self) -> None:
        if self._loading or not self._meeting_glossary_dirty:
            return
        self._meeting_glossary_dirty = False
        terms, skip_note = self.general_page.parse_glossary_editor()
        prev_skip = self._glossary_skip_note
        self._glossary_skip_note = skip_note
        if terms != (self._config.get("stt.glossary", []) or []):
            self._config.set("stt.glossary", terms)
            save_immediate = getattr(self._config, "save_immediate", None)
            if callable(save_immediate):
                save_immediate()
            self.recognition_changed.emit()
            self._refresh_recognition_feedback()
        elif self._glossary_skip_note != prev_skip:
            self._refresh_recognition_feedback()

    def _flush_minutes_fields(self, *_args) -> None:
        if self._loading:
            return
        pp = self.polish_page
        self._config.set("meeting.model_name", pp.minutes_model_edit.text().strip())
        self._config.set("meeting.template_id", str(pp.minutes_template_combo.currentData() or "general"))
        self._config.set("meeting.output_language", str(pp.minutes_language_combo.currentData() or "auto"))

    def _on_minutes_auto_toggled(self, enabled: bool) -> None:
        if self._loading:
            return
        if enabled:
            from sayink.meeting_session import confirm_remote_host, remote_host_needing_confirmation

            host = remote_host_needing_confirmation(self._config)
            if host:
                if not self._confirm(
                    "会后自动生成纪要",
                    f"每场会议结束后，完整文字稿会自动发送到 {host} 生成纪要（不发送音频）。"
                    "确认后本机记住该地址，之后不再询问。",
                    accept="确认并开启",
                ):
                    self._loading = True
                    try:
                        self.polish_page.minutes_auto_row.setChecked(False)
                    finally:
                        self._loading = False
                    return
                confirm_remote_host(self._config, host)
        self._config.set("meeting.auto_generate", enabled)

    def _apply_hotkey_setting(self, hotkey: str) -> None:
        if self._loading or not hotkey:
            return
        parts = hotkey.lower().split("+")
        has_modifier = any(p.strip() in ("ctrl", "alt", "shift", "win", "cmd") for p in parts)
        problem = ""
        if not has_modifier:
            problem = "快捷键必须包含至少一个修饰键（Ctrl/Alt/Shift）。"
        elif is_reserved_hotkey(hotkey):
            problem = (
                f"{format_hotkey(hotkey)} 是系统或常用软件的快捷键，"
                "占用后它在其他软件里会失效。请换一个组合，例如 Alt + X。"
            )
        if problem:
            QMessageBox.warning(self, "提示", problem)
            self._loading = True
            try:
                self.general_page.hotkey_edit.set_value(self._config.get("hotkey", DEFAULT_HOTKEY))
            finally:
                self._loading = False
            return
        old = self._config.get("hotkey")
        self._config.set("hotkey", hotkey)
        if hotkey != old:
            self.hotkey_updated.emit(hotkey)
        self._refresh_about_runtime_rows()

    def _restore_default_hotkey(self) -> None:
        if not self._confirm(
            "恢复录音快捷键",
            "录音快捷键将恢复为 Alt+X。转写模式、Esc、音频来源和结果去向保持不变。",
            accept="恢复",
        ):
            return
        self.general_page.hotkey_edit.set_value(DEFAULT_HOTKEY)
        self._apply_hotkey_setting(DEFAULT_HOTKEY)

    def _restore_default_recognition(self) -> None:
        if not self._confirm(
            "恢复高级识别",
            "将恢复这四项：语音分段改为自动检测语音；增强说话人区分改为开启；"
            "多人发言检测改为关闭；轻声增强改为关闭。"
            "识别语言、自定义词汇和当前引擎保持不变。",
            accept="恢复",
        ):
            return
        gp = self.general_page
        self._loading = True
        try:
            set_combo_by_data(gp.meeting_vad_combo, "silero")
            gp.speaker_enabled_row.setChecked(True)
            gp.speaker_activity_row.setChecked(False)
            gp.quiet_speech_row.setChecked(False)
        finally:
            self._loading = False
        self._on_recognition_setting_changed()

    def _restore_default_meeting_start_card(self) -> None:
        if not self._confirm(
            "恢复会议信息卡",
            "「开始前显示会议信息卡」将恢复为开启。会议音源、会议模型和纪要模板保持不变。",
            accept="恢复",
        ):
            return
        self._loading = True
        try:
            self.general_page.meeting_start_card_row.setChecked(True)
        finally:
            self._loading = False
        self._on_meeting_setting_changed()

    def _restore_default_auto_minutes(self) -> None:
        if not self._confirm(
            "恢复自动纪要",
            "「会议结束后自动生成纪要」将恢复为关闭。"
            "接口、密钥、纪要模板和已确认的远程地址保持不变。",
            accept="恢复",
        ):
            return
        if self.polish_page.minutes_auto_row.isChecked():
            self.polish_page.minutes_auto_row.setChecked(False)
        else:
            self._config.set("meeting.auto_generate", False)

    def _on_input_source_radio_toggled(self, checked: bool) -> None:
        if not checked or self._loading:
            return
        anchor = self.general_page.src_mic_rb.parentWidget()
        if anchor is not None:
            without_scroll_jump(anchor, self._sync_source_device_widgets)
        else:
            self._sync_source_device_widgets()
        if not self._confirm_discard_pending():
            self._revert_input_source_radios()
            return
        if anchor is not None:
            without_scroll_jump(anchor, self._persist_runtime_settings)
        else:
            self._persist_runtime_settings()

    def _on_trigger_mode_radio_toggled(self, checked: bool) -> None:
        if not checked or self._loading:
            return
        if not self._confirm_discard_pending():
            self._revert_trigger_mode_radios()
            return
        anchor = self.general_page.trigger_hotkey_rb.parentWidget()
        if anchor is not None:
            without_scroll_jump(anchor, self._persist_runtime_settings)
        else:
            self._persist_runtime_settings()

    def _on_audio_device_changed(self, _index: int = 0) -> None:
        if self._loading:
            return
        gp = self.general_page
        if not self._confirm_discard_pending():
            self._loading = True
            try:
                set_combo_by_data(gp.mic_device_combo, int(self._config.get("audio.mic_device_index", -1)))
                set_combo_by_data(gp.system_device_combo, int(self._config.get("audio.system_device_index", -1)))
            finally:
                self._loading = False
            return
        self._persist_runtime_settings()

    def _llm_connection_identity(self, model: str) -> tuple[str, str, str]:
        from sayink.text_polisher import (
            api_url_error,
            normalize_api_key,
            normalize_model_name,
            redact_url_userinfo,
        )

        pp = self.polish_page
        url = pp.llm_url_edit.text().strip()
        suffixes = str(self._config.get("llm.proxy_bypass_suffixes", "") or "")
        if url and api_url_error(url, proxy_bypass_suffixes=suffixes) is None:
            url, _removed = redact_url_userinfo(url)
        return url, normalize_api_key(pp.llm_key_edit.text()), normalize_model_name(model)

    def _show_completed_llm_status(self) -> None:
        from sayink.text_polisher import public_cleartext_note

        pp = self.polish_page
        current = self._llm_connection_identity(pp.llm_model_edit.text())
        note = public_cleartext_note(current[0])
        stamp = self._llm_test_stamp
        if stamp is not None and stamp[:3] == current:
            ok, message = stamp[3], stamp[4]
            if ok and note and note not in message:
                message = f"{message}{note}"
            self._set_llm_status(pp.llm_test_status, message, "ok" if ok else "error")
            return
        text = "配置已填写，尚未测试连接。"
        if note:
            text += note
        self._set_llm_status(pp.llm_test_status, text, "idle")

    def _replace_llm_edit(self, edit: QLineEdit, text: str) -> None:
        if edit.text() == text:
            return
        blocker = QSignalBlocker(edit)
        edit.setText(text)
        del blocker

    def _flush_llm_fields(self) -> None:
        if self._loading:
            return
        from sayink.text_polisher import (
            URL_USERINFO_ERROR,
            api_url_error,
            normalize_api_key,
            normalize_model_name,
            prompt_config_key,
            redact_url_userinfo,
        )

        pp = self.polish_page
        llm_enabled = pp.llm_enable_row.isChecked()
        self._config.set("llm.enabled", llm_enabled)
        llm_url = pp.llm_url_edit.text().strip()
        suffixes = str(self._config.get("llm.proxy_bypass_suffixes", "") or "")
        problem = api_url_error(llm_url, proxy_bypass_suffixes=suffixes) if llm_url else None
        if problem:
            QMessageBox.warning(self, "提示", problem)
            self._replace_llm_edit(pp.llm_url_edit, self._config.get("llm.api_url", ""))
        else:
            if llm_url:
                llm_url, removed = redact_url_userinfo(llm_url)
                if removed:
                    QMessageBox.warning(self, "提示", URL_USERINFO_ERROR)
                    self._replace_llm_edit(pp.llm_url_edit, llm_url)
            self._config.set("llm.api_url", llm_url)
        key = normalize_api_key(pp.llm_key_edit.text())
        model = normalize_model_name(pp.llm_model_edit.text())
        self._replace_llm_edit(pp.llm_key_edit, key)
        self._replace_llm_edit(pp.llm_model_edit, model)
        self._config.set("llm.api_key", key)
        self._config.set("llm.model_name", model)
        mode = pp.selected_mode()
        self._config.set(prompt_config_key(mode), pp.llm_prompt_edit.toPlainText().strip())
        pp.llm_prompt_mode = mode
        self._config.set("llm.mode", mode)
        self._update_llm_configuration_status()

    def _on_done(self) -> None:
        self._cancel_mic_probe_if_active()
        self._flush_meeting_glossary()
        self._flush_llm_fields()
        self.close()

    # ── LLM Connection Test ────────────────────────────────

    def _release_button_without_shifting_focus(self, btn: QPushButton) -> None:
        if QApplication.focusWidget() is btn:
            btn.clearFocus()
        btn.setEnabled(False)
        moved = QApplication.focusWidget()
        if moved is not None and moved is not btn:
            moved.clearFocus()

    def _test_llm(self) -> None:
        pp = self.polish_page
        self._llm_test_worker = self._start_llm_test(
            pp.llm_model_edit.text().strip(),
            pp.llm_test_btn,
            pp.llm_test_status,
        )

    def _test_minutes_model(self) -> None:
        pp = self.polish_page
        model = pp.minutes_model_edit.text().strip() or pp.llm_model_edit.text().strip()
        pp.minutes_test_status.show()
        self._minutes_test_worker = self._start_llm_test(
            model,
            pp.minutes_test_btn,
            pp.minutes_test_status,
            ok_text=f"连接正常，纪要将使用 {model}。",
        )

    def _start_llm_test(
        self,
        model: str,
        btn: QPushButton,
        status: QLabel,
        *,
        ok_text: str = "连接正常，可以使用。",
    ):
        from sayink.text_polisher import polish_settings_complete

        url, key, model = self._llm_connection_identity(model)
        if not polish_settings_complete(url, key, model):
            self._set_llm_status(status, "请填写完整的接口信息。", "error")
            return None

        class _W(QThread):
            def __init__(self, u, k, m, proxy_mode, suffixes, parent=None):
                super().__init__(parent)
                self.u, self.k, self.m = u, k, m
                self.proxy_mode = proxy_mode
                self.suffixes = suffixes
                self.ok, self.msg = False, ""

            def run(self):
                from sayink.text_polisher import TextPolisher

                self.ok, self.msg = TextPolisher.test_connection(
                    self.u,
                    self.k,
                    self.m,
                    proxy_mode=self.proxy_mode,
                    proxy_bypass_suffixes=self.suffixes,
                )

        worker = _W(
            url,
            key,
            model,
            self._config.get("llm.proxy_mode", "system"),
            str(self._config.get("llm.proxy_bypass_suffixes", "") or ""),
            self,
        )
        self._test_workers.append(worker)
        worker.finished.connect(lambda w=worker: self._on_test_done(w, btn, status, ok_text))
        self._release_button_without_shifting_focus(btn)
        btn.setText("测试中…")
        self._set_llm_status(status, "正在测试连接…", "busy")
        worker.start()
        return worker

    def _on_test_done(self, w, btn: QPushButton, status: QLabel, ok_text: str) -> None:
        try:
            self._test_workers.remove(w)
        except ValueError:
            pass
        btn.setEnabled(True)
        btn.setText("测试连接")
        suffixes_now = str(self._config.get("llm.proxy_bypass_suffixes", "") or "")
        if w.proxy_mode != self._config.get("llm.proxy_mode", "system") or w.suffixes != suffixes_now:
            if status is self.polish_page.llm_test_status:
                self._update_llm_configuration_status()
            else:
                self._set_llm_status(status, "代理模式已更改，请重新测试连接。", "idle")
            return
        if status is self.polish_page.llm_test_status:
            self._llm_test_stamp = (w.u, w.k, w.m, w.ok, ok_text if w.ok else w.msg)
            self._update_llm_configuration_status()
            return
        if w.ok:
            self._set_llm_status(status, ok_text, "ok")
        else:
            self._set_llm_status(status, w.msg, "error")

    def focus_llm_connection(self) -> None:
        self.show_page(2)
        self.polish_page.focus_connection()

    # ── Cleanup & Lifecycle ────────────────────────────────

    def reload_settings(self, *, refresh_devices: bool = True, heavy: bool = True) -> None:
        self._load_settings(refresh_devices=refresh_devices, heavy=heavy)

    def cancel_hotkey_capture(self) -> None:
        self.general_page.hotkey_edit.cancel_capture_if_active()

    @property
    def running_workers(self):
        workers = [*self._download_threads, *self.findChildren(QThread)]
        return tuple(worker for worker in dict.fromkeys(workers) if worker.isRunning())

    def cancel_all_downloads(self) -> None:
        workers = [*self._dl_workers.values(), *self._download_threads]
        for worker in dict.fromkeys(workers):
            if hasattr(worker, "cancel"):
                worker.cancel()

    def on_window_hidden(self) -> None:
        self._cancel_mic_probe_if_active()
        self.cancel_hotkey_capture()
        self._flush_meeting_glossary()
        self._flush_llm_fields()
        self.polish_page.hide_revealed_key()

    def closeEvent(self, event) -> None:
        self.on_window_hidden()
        self.cancel_all_downloads()
        super().closeEvent(event)

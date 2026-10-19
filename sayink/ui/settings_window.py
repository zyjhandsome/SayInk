import logging
import shutil
from pathlib import Path

from PyQt6.QtGui import QFont, QFontMetrics
from PyQt6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QVBoxLayout, QHBoxLayout, QWidget,
    QLabel, QLineEdit, QPushButton, QComboBox,
    QMessageBox, QFrame,
    QStackedWidget,
    QFileDialog,
    QSizePolicy, QSpinBox,
    QWIDGETSIZE_MAX,
)
from PyQt6.QtCore import Qt, pyqtSignal, QThread, QTimer, QSignalBlocker, QEvent

log = logging.getLogger("SayInk")

from sayink.config import (
    Config,
    DEFAULT_HOTKEY,
    format_hotkey,
    is_reserved_hotkey,
    TRIGGER_MODE_CONTINUOUS,
    TRIGGER_MODE_HOTKEY,
)
from sayink.audio_recorder import AudioRecorder
from sayink.audio_devices import (
    INPUT_SOURCE_MICROPHONE,
    INPUT_SOURCE_MIXED,
    INPUT_SOURCE_SYSTEM,
    list_microphone_devices,
    list_system_capture_devices_for_settings,
    sanitize_system_device_index,
)

from sayink.ui.settings_components import (
    SettingsPage,
    elide_middle,
    empty_state,
    footnote,
    group_divider,
    section_header,
    settings_group,
    sync_group_tab_stop,
)
from sayink.ui.settings_pages import (
    build_about_page,
    build_general_page,
    build_model_page,
    build_polish_page,
)
from sayink.ui.settings_pages.about import (
    AboutSummaryRow, paint_about_update_row, refresh_update_check_time,
)
from sayink.ui.dictation_hints import dictation_hints
from sayink.ui.settings_pages.general import MIXED_AUDIO_NOTE, SYSTEM_AUDIO_NOTE
from sayink.ui.model_card import ModelCard, RATING_TOOLTIP, format_model_ratings
from sayink.ui import design_tokens as _tok
from sayink.ui import settings_styles as _settings_styles
from sayink.ui.theme import normalize_theme_mode
from sayink.runtime_status import RuntimeState, SegmentationStatus, coerce_runtime_status
from sayink.recognition_settings import RecognitionSettings

# Non-color layout constants (theme-independent).
_CONTROL_NUMERIC_WIDTH = _tok.CONTROL_NUMERIC_WIDTH
# Shared width so 显示 / 隐藏 / 测试连接 / 测试中… / 恢复默认 / 管理模板
# share one right edge and none of those labels clip.
_LLM_ACTION_LABELS = ("显示", "隐藏", "测试连接", "测试中…", "恢复默认", "管理模板")


def _llm_action_button_width() -> int:
    font = QFont(_tok.UI_FONT_FAMILY)
    font.setPixelSize(_tok.TYPE_BODY_SM)
    widest = max(QFontMetrics(font).horizontalAdvance(label) for label in _LLM_ACTION_LABELS)
    return widest + _tok.CONTROL_BTN_SM_PAD_H * 2 + 8


def _device_index(data) -> int:
    """Combo item data → device index; 0 is a real device, only None means automatic."""
    return -1 if data is None else int(data)

# ── Settings Window ──────────────────────────────────────────────


class SettingsWindow(QWidget):
    hotkey_updated = pyqtSignal(str)
    settings_changed = pyqtSignal()
    auto_start_changed = pyqtSignal(bool)
    sound_enabled_changed = pyqtSignal(bool)
    restore_clipboard_changed = pyqtSignal(bool)
    models_changed = pyqtSignal()
    # Glossary / language / VAD (stt.*): shared by dictation and meetings.
    recognition_changed = pyqtSignal()
    theme_changed = pyqtSignal(str)
    hotkey_capture_started = pyqtSignal()
    hotkey_capture_ended = pyqtSignal()
    update_check_requested = pyqtSignal()
    update_install_requested = pyqtSignal()
    runtime_status_changed = pyqtSignal(str)
    navigation_requested = pyqtSignal(str)
    usage_hint_changed = pyqtSignal()
    closed = pyqtSignal()
    finished = pyqtSignal(int)

    def __init__(self, config: Config, parent=None, pending_segment_count=None):
        super().__init__(parent)
        self._config = config
        self._pending_segment_count = pending_segment_count
        self._model_cards: dict[str, ModelCard] = {}
        self._dl_workers: dict[str, object] = {}
        self._test_workers: list[QThread] = []
        self._llm_test_worker: QThread | None = None
        self._minutes_test_worker: QThread | None = None
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
        self._speaker_runtime_text = "尚未获取说话人增强运行状态"
        self._setup_window()
        self._setup_ui()
        self._load_settings()
        # Paint from the active axis (construct-time helpers may use cached fragments).
        self.reapply_theme()

    def _setup_window(self):
        self.setWindowTitle("设置")
        self.setStyleSheet(_settings_styles.WINDOW_CSS)

    def reapply_theme(self) -> None:
        """Refresh dialog chrome after design tokens were activated."""
        from sayink.ui import design_tokens as tok
        from sayink.ui.settings_components import (
            paint_device_selection_link,
            reapply_subtree,
        )

        self.setStyleSheet(_settings_styles.WINDOW_CSS)
        self._paint_model_hero_status()
        if hasattr(self, "_content_wrap"):
            self._content_wrap.setStyleSheet("QWidget#settingsContent { background: transparent; }")
        if hasattr(self, "_pages_host"):
            self._pages_host.setStyleSheet("QWidget#settingsPagesHost { background: transparent; }")
        if hasattr(self, "_pages"):
            self._pages.setStyleSheet("QStackedWidget#settingsPages { background: transparent; }")
        if hasattr(self, "_hotkey_hint"):
            self._hotkey_hint.setStyleSheet(
                f"color: {tok.TEXT_SEC}; font-size: {_tok.TYPE_FOOTNOTE}px; line-height: 1.4;"
                f" background: transparent; padding: 0 16px 12px 16px;"
            )
        if hasattr(self, "_advanced_audio_btn"):
            paint_device_selection_link(self._advanced_audio_btn)
            paint_device_selection_link(self._advanced_recognition_btn)
        if hasattr(self, "_storage_summary_label"):
            self._storage_summary_label.setStyleSheet(
                f"color: {tok.TEXT_SEC}; font-size: {_tok.TYPE_BODY_SM}px; background: transparent;"
            )

        reapply_subtree(self)
        if hasattr(self, "_model_hero_layout"):
            self._refresh_active_model_hero()
        if hasattr(self, "_llm_prompt_edit"):
            self._paint_llm_prompt_edit()
        if hasattr(self, "_meeting_glossary_edit"):
            self._paint_meeting_glossary_edit()
        if hasattr(self, "_about_update_btn"):
            paint_about_update_row(self)
        for name in ("_llm_test_status", "_minutes_test_status"):
            if hasattr(self, name):
                self._paint_llm_status(getattr(self, name))
        if hasattr(self, "_llm_key_toggle"):
            self._paint_llm_key_toggle()
        self._refresh_recognition_feedback()
        self._refresh_output_controls()
        self._refresh_meeting_defaults()
        self._mic_level_bar.setStyleSheet(
            f"QProgressBar {{ border: none; background: {tok.SURFACE_PEARL}; border-radius: 3px; }}"
            f"QProgressBar::chunk {{ background: {tok.ACCENT}; border-radius: 3px; }}"
        )

    def show_page(self, index: int) -> None:
        self._pages.setCurrentIndex(index)
        if index == 2:
            from sayink.ui.minutes_templates import populate_template_combo

            populate_template_combo(self._minutes_template_combo, self._config)
        if index == 3:
            self._refresh_about_info()
            refresh_update_check_time(self)

    def _open_about_setting(self, page: str, focus: str = "") -> None:
        self.show_page({"general": 0, "engine": 1, "polish": 2}[page])
        self.navigation_requested.emit(page)
        if focus == "hotkey":
            self._pages.widget(0).ensureWidgetVisible(self._hotkey_edit)
            self._hotkey_edit.setFocus(Qt.FocusReason.TabFocusReason)
        elif focus == "polish":
            self._pages.widget(2).ensureWidgetVisible(self._llm_enable_row)
            self._llm_enable_row.setFocus(Qt.FocusReason.TabFocusReason)

    def _on_nav_changed(self, row: int):
        self.show_page(row)

    def _open_about_from_general(self) -> None:
        self.show_page(3)

    def hideEvent(self, event):
        super().hideEvent(event)
        if self.parent() is None:
            self.closed.emit()
            self.finished.emit(0)

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # The content column starts directly with the active page. Settings are
        # auto-saved, so persistent action chrome would only consume space.
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
        # Top/bottom inset so section titles (e.g. 偏好) are not flush-clipped
        # against the content column edge when scrolled.
        # Host + document margins match the 24px workspace inset. The page
        # reserves its own scrollbar gutter so switching pages never shifts it.
        pages_lay.setContentsMargins(6, 8, 6, 12)
        pages_lay.setSpacing(0)

        self._pages = QStackedWidget()
        self._pages.setObjectName("settingsPages")
        self._pages.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding,
        )
        self._pages.setStyleSheet("QStackedWidget#settingsPages { background: transparent; }")
        self._pages.addWidget(build_general_page(self))
        self._pages.addWidget(build_model_page(self))
        self._pages.addWidget(build_polish_page(self))
        self._pages.addWidget(build_about_page(self))
        pages_lay.addWidget(self._pages, 1)
        content_lay.addWidget(pages_host, 1)
        root.addWidget(content_wrap, 1)

    # ── Page: General ──────────────────────────────────

    def set_runtime_status(
        self,
        state_or_label: RuntimeState | str,
        label: str | None = None,
    ) -> None:
        self._runtime_status = coerce_runtime_status(state_or_label, label)
        self._paint_model_hero_status()
        self.runtime_status_changed.emit(self._runtime_status.label)

    def _paint_model_hero_status(self) -> None:
        """「已下载 ≠ 已载入」— the engine page must say whether the model is usable."""
        label = getattr(self, "_model_hero_status", None)
        if label is None:
            return
        status = self._runtime_status
        if self._recognition_pending:
            fg, bg, text = _tok.AMBER_TEXT, _tok.AMBER_SOFT, "设置待生效 · 本场继续使用原配置"
        elif status.state is RuntimeState.READY:
            fg, bg, text = _tok.GREEN_TEXT, _tok.GREEN_BG, "已载入 · 可用"
        elif status.state is RuntimeState.UNAVAILABLE:
            fg, bg, text = _tok.RED, _tok.RED_BG, status.label
        else:
            fg, bg, text = _tok.AMBER_TEXT, _tok.AMBER_SOFT, status.label
        label.setText(text)
        label.setStyleSheet(
            f"background: {bg}; color: {fg}; border-radius: {_tok.RADIUS_PILL}px;"
            f" padding: 3px 10px; font-size: {_tok.TYPE_CAPTION}px; font-weight: 700;"
        )

    def set_recognition_runtime_status(
        self, status: SegmentationStatus, *, speaker_status: str, pending: bool = False,
    ) -> None:
        self._segmentation_status = status
        self._speaker_runtime_text = speaker_status
        self._recognition_pending = pending
        self._paint_model_hero_status()
        self._refresh_recognition_feedback()

    def _refresh_recognition_feedback(self) -> None:
        from sayink.speech_recognizer import AUX_MODEL_SILERO_VAD, is_model_downloaded

        shared = RecognitionSettings.from_config(self._config)
        downloaded = is_model_downloaded(AUX_MODEL_SILERO_VAD)
        status = self._segmentation_status
        if status is not None:
            scope = "当前会议" if status.meeting else "当前听写"
            text = f"{scope}：{status.label}"
            if self._recognition_pending and downloaded and status.requested == "silero" and not status.model_available:
                text = f"{scope}：Silero VAD 已下载，待本场结束后加载 · 当前使用能量断句"
            self._vad_runtime_label.setToolTip(status.error)
        elif shared.vad == "energy":
            text = "已选择能量断句 · 尚未获取实际运行状态"
        elif downloaded:
            text = "Silero VAD 已下载 · 尚未确认加载状态"
        else:
            text = "Silero VAD 未下载 · 自动使用能量断句"
        self._vad_runtime_label.setText(text)
        fg = _tok.TEXT_SEC
        if status is not None:
            if status.error:
                fg = _tok.RED
            elif status.effective == "silero":
                fg = _tok.GREEN_TEXT
            elif status.requested == "silero":
                fg = _tok.AMBER_TEXT
        self._vad_runtime_label.setStyleSheet(
            f"color: {fg}; font-size: {_tok.TYPE_FOOTNOTE}px; background: transparent;"
        )
        self._recognition_apply_label.setStyleSheet(
            f"color: {_tok.AMBER_TEXT if self._recognition_pending else _tok.TEXT_SEC};"
            f" font-size: {_tok.TYPE_FOOTNOTE}px; background: transparent;"
        )
        self._speaker_runtime_label.setText(self._speaker_runtime_text)
        self._recognition_capability_label.setText("\n".join(shared.notes))
        self._recognition_capability_label.setVisible(bool(shared.notes))
        self._set_section_visible(self._recognition_capability_row, bool(shared.notes))
        apply_text = (
            "已保存 · 待本场结束且已录语音处理完成后生效。"
            if self._recognition_pending else
            "更改自动保存；空闲时应用，录音或处理期间的更改在本场结束后生效。"
        )
        if status is not None and status.error:
            apply_text += " 语音检测模型加载失败，当前按音量分段；可展开高级设置查看。"
        self._recognition_apply_label.setText(apply_text)
        if AUX_MODEL_SILERO_VAD not in self._dl_workers:
            self._set_section_visible(self._vad_download_row, not downloaded)
            self._set_section_visible(self._vad_download_status_row, bool(downloaded))
            self._vad_download_status.setText("语音检测模型（Silero VAD）：文件已下载；运行状态见上方。")
            self._vad_download_btn.setEnabled(True)
            self._vad_download_btn.setText("下载语音检测模型（约 2 MB）")

    def _download_shared_vad(self) -> None:
        from sayink.speech_recognizer import AUX_MODEL_SILERO_VAD

        self._start_download(AUX_MODEL_SILERO_VAD)

    def _on_shared_download_progress(self, model_id: str, pct: int) -> None:
        from sayink.speech_recognizer import AUX_MODEL_SILERO_VAD

        if model_id == AUX_MODEL_SILERO_VAD:
            self._vad_download_row.show()
            self._vad_download_status_row.hide()
            self._vad_download_btn.setEnabled(False)
            self._vad_download_btn.setText(f"下载中 {int(pct)}%")

    def _configure_numeric_spin(self, spin: QSpinBox) -> None:
        """Shared metrics for flat themed QSpinBox steppers."""
        spin.setFixedWidth(_CONTROL_NUMERIC_WIDTH)
        spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.UpDownArrows)
        spin.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )

    def _set_mic_test_status(self, text: str) -> None:
        if hasattr(self, "_mic_test_status"):
            self._mic_test_status.setText(text)

    def _refresh_dir_label(self):
        p = str(self._config.models_dir)
        self._dir_path_label.setText(p)
        self._dir_path_label.setToolTip(p)
        self._refresh_storage_summary()

    def _change_model_dir(self):
        cur = str(self._config.models_dir)
        d = QFileDialog.getExistingDirectory(self, "选择模型存储目录", cur)
        if not d:
            return
        new_p = Path(d)
        old_p = self._config.models_dir
        if new_p == old_p:
            return
        new_p.mkdir(parents=True, exist_ok=True)
        moved = 0
        failed: list[str] = []
        if old_p.exists():
            for item in old_p.iterdir():
                if item.is_dir():
                    target = new_p / item.name
                    if not target.exists():
                        try:
                            shutil.move(str(item), str(target))
                            moved += 1
                        except OSError as e:
                            log.warning("模型目录迁移失败 %s: %s", item.name, e)
                            failed.append(item.name)
        self._config.set("stt.models_dir", str(new_p))
        from sayink.speech_recognizer import set_models_dir
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

    def _refresh_storage_summary(self) -> None:
        if not hasattr(self, "_storage_summary_label"):
            return
        from sayink.speech_recognizer import MODEL_REGISTRY, is_model_downloaded

        downloaded = [m for m in MODEL_REGISTRY if is_model_downloaded(m["id"])]
        total_mb = sum(m["size_mb"] for m in downloaded)
        path = elide_middle(str(self._config.models_dir), 36)
        count = len(downloaded)
        self._storage_summary_label.setText(
            f"已下载 {count} 个 · 约 {total_mb} MB · {path}"
        )
        self._storage_summary_label.setToolTip(str(self._config.models_dir))

    def _refresh_active_model_hero(self) -> None:
        self._refresh_meeting_defaults()
        if not hasattr(self, "_model_hero_layout"):
            return
        while self._model_hero_layout.count():
            item = self._model_hero_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._model_hero_status = None

        from sayink.speech_recognizer import DEFAULT_MODEL_ID, get_model_info, is_model_downloaded

        active_id = self._config.get("stt.model_id", DEFAULT_MODEL_ID)
        info = get_model_info(active_id) if active_id else None
        if not info or not is_model_downloaded(active_id):
            empty_wrap = QWidget()
            empty_lay = QVBoxLayout(empty_wrap)
            empty_lay.setContentsMargins(0, 8, 0, 8)
            empty_lay.addWidget(empty_state("尚未选择已下载的模型，请从下方下载并启用。"))
            self._model_hero_layout.addWidget(empty_wrap)
            return

        card = QWidget()
        lay = QVBoxLayout(card)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(8)

        head = QHBoxLayout()
        title = QLabel(info["name"])
        title.setProperty("viRole", "engineHeroTitle")
        title.setStyleSheet(
            f"color: {_tok.TEXT}; font-family: {_tok.FONT_DISPLAY}; font-size: {_tok.TYPE_HERO}px;"
            f" font-weight: 700; background: transparent;"
        )
        head.addWidget(title)
        badge = QLabel("当前")
        badge.setProperty("viRole", "engineHeroBadge")
        badge.setStyleSheet(
            f"background: {_tok.SURFACE_PEARL}; color: {_tok.TEXT_SEC};"
            f" border-radius: {_tok.RADIUS_PILL}px; padding: 3px 10px;"
            f" font-size: {_tok.TYPE_CAPTION}px; font-weight: 700;"
        )
        head.addWidget(badge)
        self._model_hero_status = QLabel()
        self._model_hero_status.setAccessibleName("当前模型载入状态")
        self._model_hero_status.setToolTip("下载完成后仍需载入内存；载入完成前快捷键不会响应。")
        head.addWidget(self._model_hero_status)
        self._paint_model_hero_status()
        head.addStretch()
        size = QLabel(f"{info['size_mb']} MB")
        size.setProperty("viRole", "engineHeroSize")
        size.setStyleSheet(
            f"color: {_tok.TEXT_DIM}; font-size: {_tok.TYPE_FOOTNOTE}px; background: transparent;"
        )
        head.addWidget(size)
        lay.addLayout(head)

        desc = QLabel(info["description"])
        desc.setProperty("viRole", "engineHeroDesc")
        desc.setWordWrap(True)
        desc.setStyleSheet(
            f"color: {_tok.TEXT_SEC}; font-size: {_tok.TYPE_BODY_SM}px; background: transparent;"
        )
        lay.addWidget(desc)

        meta = QLabel(
            f"{info['languages']} · "
            f"{format_model_ratings(info['accuracy'], info['speed'])}"
        )
        meta.setProperty("viRole", "engineHeroMeta")
        meta.setStyleSheet(
            f"color: {_tok.TEXT_DIM}; font-size: {_tok.TYPE_FOOTNOTE}px; background: transparent;"
        )
        meta.setToolTip(RATING_TOOLTIP)
        lay.addWidget(meta)

        self._model_hero_layout.addWidget(card)

    def _rebuild_model_cards(self):
        from sayink.speech_recognizer import MODEL_REGISTRY, DEFAULT_MODEL_ID, is_model_downloaded
        self._refresh_active_model_hero()
        self._refresh_storage_summary()

        while self._cards_layout.count():
            item = self._cards_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._model_cards.clear()
        active_id = self._config.get("stt.model_id", DEFAULT_MODEL_ID)

        def _add_section(title: str, models: list, downloaded: bool) -> None:
            section = QWidget()
            section_lay = QVBoxLayout(section)
            section_lay.setContentsMargins(0, 0, 0, 0)
            section_lay.setSpacing(10)

            if title:
                section_lay.addWidget(section_header(title))

            if not models:
                msg = "暂无其他已下载模型" if downloaded else "所有模型均已下载"
                empty_group = settings_group()
                empty_lay = QVBoxLayout(empty_group)
                empty_lay.setContentsMargins(0, 0, 0, 0)
                empty_lay.addWidget(empty_state(msg))
                section_lay.addWidget(empty_group)
            else:
                list_host = QWidget()
                list_lay = QVBoxLayout(list_host)
                list_lay.setContentsMargins(0, 0, 0, 0)
                list_lay.setSpacing(12)
                for m_info in models:
                    card = ModelCard(
                        m_info, downloaded, downloaded and m_info["id"] == active_id,
                    )
                    card.action_clicked.connect(self._on_card_action)
                    self._model_cards[m_info["id"]] = card
                    list_lay.addWidget(card)
                section_lay.addWidget(list_host)

            self._cards_layout.addWidget(section)

        downloaded_models = [
            m for m in MODEL_REGISTRY
            if is_model_downloaded(m["id"]) and m["id"] != active_id
        ]
        available_models = [m for m in MODEL_REGISTRY if not is_model_downloaded(m["id"])]
        available_models.sort(key=lambda model: model["id"] != DEFAULT_MODEL_ID)
        if downloaded_models:
            _add_section("其他已下载", downloaded_models, True)
        _add_section("可下载", available_models, False)
        self._add_aux_model_section()
        self._model_cards_for_id = active_id

    def _add_aux_model_section(self) -> None:
        """Shared helpers: cards show file availability, not runtime activation."""
        from sayink.speech_recognizer import AUX_MODEL_REGISTRY, is_model_downloaded

        section = QWidget()
        section_lay = QVBoxLayout(section)
        section_lay.setContentsMargins(0, 0, 0, 0)
        section_lay.setSpacing(10)
        section_lay.addWidget(section_header("公共识别辅助模型"))
        hint = footnote("语音检测用于三种录音模式；说话人增强用于持续转写与会议。加载状态见 通用 → 识别 → 高级识别设置。")
        section_lay.addWidget(hint)
        list_host = QWidget()
        list_lay = QVBoxLayout(list_host)
        list_lay.setContentsMargins(0, 0, 0, 0)
        list_lay.setSpacing(12)
        for m_info in AUX_MODEL_REGISTRY:
            downloaded = is_model_downloaded(m_info["id"])
            card = ModelCard(m_info, downloaded, False)
            card.action_clicked.connect(self._on_card_action)
            self._model_cards[m_info["id"]] = card
            list_lay.addWidget(card)
        section_lay.addWidget(list_host)
        self._cards_layout.addWidget(section)

    def _on_card_action(self, model_id: str, action: str):
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

    def _start_download(self, model_id: str):
        if model_id in self._dl_workers:
            return
        from sayink.speech_recognizer import ModelDownloadWorker
        worker = ModelDownloadWorker(
            model_id,
            source=self._config.get("stt.download_source", "auto"),
        )
        self._dl_workers[model_id] = worker
        self._on_shared_download_progress(model_id, 0)
        card = self._model_cards.get(model_id)
        if card:
            card.set_download_progress(0)
        worker.progress.connect(lambda pct, c=card: c.set_download_progress(pct) if c else None)
        worker.progress.connect(lambda pct, mid=model_id: self._on_shared_download_progress(mid, pct))
        worker.finished_ok.connect(lambda mid: self._on_dl_done(mid))
        worker.error.connect(lambda msg, c=card, mid=model_id: self._on_dl_error(msg, c, mid))
        worker.start()

    def _on_dl_done(self, model_id: str):
        self._dl_workers.pop(model_id, None)
        from sayink.speech_recognizer import get_downloaded_models
        downloaded = get_downloaded_models()
        if len(downloaded) == 1 and downloaded[0] == model_id:
            self._config.set("stt.model_id", model_id)
        self._rebuild_model_cards()
        self.models_changed.emit()
        self._refresh_recognition_feedback()
        from sayink.speech_recognizer import get_model_info, is_aux_model
        info = get_model_info(model_id)
        name = info["name"] if info else model_id
        if is_aux_model(model_id):
            text = (
                f"{name} 已下载。按识别设置启用；正在录音或处理语音时，"
                "待本场结束后生效。可在 通用 → 识别 → 高级识别设置 查看实际加载状态。"
            )
        elif self._config.get("stt.model_id", "") == model_id:
            if self._recognition_pending:
                text = f"{name} 已下载，待本场结束且已录语音处理完成后载入。可在通用 → 识别查看生效状态。"
            else:
                text = f"{name} 已下载，正在载入内存（约需数十秒）。载入完成前请勿开始录音。"
        else:
            # Only the active model is loaded; a second download just sits
            # on disk until the user enables it, so do not claim otherwise.
            text = f"{name} 已下载。当前仍使用原来的模型；点击该模型卡片上的「使用此模型」即可切换。"
        QMessageBox.information(self, "完成", text)

    def _on_dl_error(self, msg: str, card, model_id: str = ""):
        if model_id and card is None:
            self._dl_workers.pop(model_id, None)
        if card:
            worker = self._dl_workers.pop(card._model_id, None)
            if worker is not None and getattr(worker, "_cancelled", False) is True:
                card.set_download_cancelled()
            else:
                card.set_download_error(msg)
        else:
            QMessageBox.warning(self, "下载失败", msg)
        self._refresh_recognition_feedback()

    def _delete_model(self, model_id: str):
        from sayink.speech_recognizer import get_model_info, delete_model
        info = get_model_info(model_id)
        name = info["name"] if info else model_id
        if not self._confirm(
            "删除模型",
            f"确定删除「{name}」吗？删除后需重新下载。",
            accept="删除",
            destructive=True,
        ):
            return
        active_id = self._config.get("stt.model_id", "")
        from sayink.speech_recognizer import is_aux_model
        if is_aux_model(model_id):
            active_id = ""  # never the dictation model
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
            from sayink.speech_recognizer import get_downloaded_models
            remaining = get_downloaded_models()
            self._config.set("stt.model_id", remaining[0] if remaining else "")
        self._rebuild_model_cards()
        self.models_changed.emit()

    # ── Page: Polish (LLM) ─────────────────────────────

    def _on_llm_enable_toggled(self, enabled: bool):
        # The before/after example stays visible in both states: it is the
        # information a user needs *before* deciding to turn polishing on.
        # The connection stays too: meeting minutes use it with polish off.
        self._without_scroll_jump(
            self._llm_enable_row,
            lambda: self._set_section_visible(self._llm_prompt_container, enabled),
        )

        if self._loading:
            return
        self._config.set("llm.enabled", enabled)
        self._flush_llm_fields()

    def _llm_missing_fields(self) -> list[str]:
        from sayink.text_polisher import api_key_required

        url = self._llm_url_edit.text().strip()
        missing = []
        if not url:
            missing.append("接口地址")
        if not self._llm_key_edit.text().strip() and api_key_required(url):
            missing.append("API 密钥")
        if not self._llm_model_edit.text().strip():
            missing.append("模型名称")
        return missing

    def _update_llm_configuration_status(self) -> None:
        if getattr(self._config, "secret_persist_failed", False) is True:
            self._set_llm_status(
                self._llm_test_status,
                "API 密钥未能存入 Windows 凭据管理器，仅本次运行有效，"
                "也没有写入配置文件；重启后需重新填写。",
                "error",
            )
            return
        if getattr(self._config, "secret_read_failed", False) is True:
            self._set_llm_status(
                self._llm_test_status,
                "无法从 Windows 凭据管理器读取已保存的 API 密钥，这里暂时显示为空；"
                "重启后仍为空请重新填写。",
                "error",
            )
            return
        missing = self._llm_missing_fields()
        if missing and self._llm_enable_row.isChecked():
            self._set_llm_status(
                self._llm_test_status,
                f"未配置：缺少{'、'.join(missing)}。转写会直接输出原文。",
                "idle",
            )
        elif missing:
            self._set_llm_status(
                self._llm_test_status,
                f"尚未填写：{'、'.join(missing)}。文字润色和会议纪要都要用到这组配置。",
                "idle",
            )
        else:
            self._set_llm_status(self._llm_test_status, "配置已填写，尚未测试连接。", "idle")

    def _scroll_page_of(self, widget: QWidget) -> SettingsPage | None:
        w = widget
        while w is not None:
            if isinstance(w, SettingsPage):
                return w
            w = w.parentWidget()
        return None

    def _polish_scroll_page(self):
        return self._scroll_page_of(self._llm_enable_row)

    def _set_section_visible(self, panel: QWidget, visible: bool) -> None:
        """Show or hide a collapsed block without a one-frame layout jump.

        Hiding alone leaves the scroll document at the expanded height until a
        later polish, and the first layout pass slides the link. Collapsing
        the maximum height makes that pass agree with the final size.
        """
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

    def _settle_scroll_anchor(
        self,
        page: SettingsPage,
        anchor: QWidget,
        anchor_y: int,
        scroll_before: int,
    ) -> None:
        """Finish the show/hide layout before anything is painted.

        The first pass often keeps the old document height, so the link moves
        and the scrollbar range is stale. A second pass, after resizing to the
        real size hint, puts the link back. Callers keep updates disabled
        across this, so the intermediate position is never shown.
        """
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
            bar.setValue(max(0, scroll_before + (new_y - anchor_y)))
            page._sync_scroll_gutter(bar.minimum(), bar.maximum())

    def _without_scroll_jump(self, anchor: QWidget, change) -> None:
        """Run ``change`` without the scroll area shoving ``anchor`` around."""
        page = self._scroll_page_of(anchor)
        if page is None or not page.isVisible():
            change()
            return
        host = page.widget()
        anchor_y = (
            anchor.mapTo(host, anchor.rect().topLeft()).y() if host is not None else 0
        )
        scroll_before = page.verticalScrollBar().value()
        was_enabled = page.updatesEnabled()
        if was_enabled:
            page.setUpdatesEnabled(False)
        try:
            change()
            self._settle_scroll_anchor(page, anchor, anchor_y, scroll_before)
        finally:
            if was_enabled:
                page.setUpdatesEnabled(True)

    def _toggle_llm_key_visibility(self, visible: bool) -> None:
        self._llm_key_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
        )
        self._llm_key_toggle.setText("隐藏" if visible else "显示")
        self._llm_key_toggle.setProperty("viBtn", "accentSm" if visible else "ghostSm")
        self._paint_llm_key_toggle()

    def _paint_llm_key_toggle(self) -> None:
        """Plain outline while the key is masked; filled accent while it is visible."""
        visible = self._llm_key_toggle.isChecked()
        self._llm_key_toggle.setToolTip("当前正在显示密钥，点击隐藏" if visible else "点击显示密钥")
        self._llm_key_toggle.setAccessibleName("隐藏 API 密钥" if visible else "显示 API 密钥")
        self._llm_key_toggle.setStyleSheet(
            _settings_styles.BTN_ACCENT_SM if visible else _settings_styles.BTN_GHOST_SM
        )

    def _set_llm_status(self, label: QLabel, text: str, tone: str) -> None:
        label.setProperty("viTone", tone)
        label.setText(text)
        self._paint_llm_status(label)

    def _paint_llm_status(self, label: QLabel) -> None:
        """Idle hints stay quiet. A test in progress, a success, or a failure is a colored bar."""
        tone = label.property("viTone") or "idle"
        if tone == "ok":
            color, bg, weight, pad = _tok.GREEN_TEXT, _tok.GREEN_BG, 600, "8px 16px"
        elif tone == "error":
            color, bg, weight, pad = _tok.RED, _tok.RED_BG, 600, "8px 16px"
        elif tone == "busy":
            color, bg, weight, pad = _tok.TEXT, _tok.SURFACE_PEARL, 600, "8px 16px"
        else:
            color, bg, weight, pad = _tok.TEXT_SEC, "transparent", 400, "0 16px 12px 16px"
        label.setStyleSheet(
            f"color: {color}; background: {bg}; font-size: {_tok.TYPE_FOOTNOTE}px;"
            f" font-weight: {weight}; border-radius: 6px; padding: {pad};"
        )

    def _release_button_without_shifting_focus(self, btn: QPushButton) -> None:
        """Disable a button that the click just focused.

        Disabling the focused widget makes Qt move focus to the next control,
        which lit up 「启用文字润色」. Giving that focus to the model field
        instead selected its text. Drop focus altogether so neither responds.
        """
        if QApplication.focusWidget() is btn:
            btn.clearFocus()
        btn.setEnabled(False)
        moved = QApplication.focusWidget()
        if moved is not None and moved is not btn:
            moved.clearFocus()

    def _style_llm_action_btn(self, btn: QPushButton) -> None:
        """Shared metrics so polish actions share one right-edge column."""
        btn.setProperty("viBtn", "ghostSm")
        btn.setAutoDefault(False)
        btn.setDefault(False)
        btn.setFixedWidth(_llm_action_button_width())
        btn.setMinimumHeight(32)
        btn.setStyleSheet(_settings_styles.BTN_GHOST_SM)

    def _paint_llm_prompt_edit(self) -> None:
        self._llm_prompt_edit.setStyleSheet(f"""
            QTextEdit {{
                background: {_tok.INPUT_BG}; color: {_tok.TEXT};
                border: 1px solid {_tok.HAIRLINE}; border-radius: {_tok.RADIUS_MD}px;
                padding: 10px 12px; font-size: {_tok.TYPE_BODY_SM}px; font-family: {_tok.FONT};
            }}
            QTextEdit:focus {{
                border: 2px solid {_tok.ACCENT_FOCUS};
                padding: 9px 11px;
            }}
        """)

    def _paint_meeting_glossary_edit(self) -> None:
        self._meeting_glossary_edit.setStyleSheet(f"""
            QPlainTextEdit {{
                background: {_tok.INPUT_BG}; color: {_tok.TEXT};
                border: 1px solid {_tok.HAIRLINE}; border-radius: {_tok.RADIUS_MD}px;
                padding: 8px 12px; font-size: {_tok.TYPE_BODY_SM}px; font-family: {_tok.FONT};
            }}
            QPlainTextEdit:focus {{
                border: 2px solid {_tok.ACCENT_FOCUS};
                padding: 7px 11px;
            }}
        """)

    def _reset_prompt(self):
        from sayink.text_polisher import POLISH_PROMPT
        self._llm_prompt_edit.setPlainText(POLISH_PROMPT)
        self._flush_llm_fields()

    # ── Page: About ────────────────────────────────────

    def _refresh_about_runtime_rows(self) -> None:
        """Update source / mode / output on About without rescanning models."""
        if not hasattr(self, "_about_summary_rows"):
            return
        hints = dictation_hints(self._config)
        rows = self._about_summary_rows
        for key, value in (
            ("录音快捷键", format_hotkey(self._config.get("hotkey", DEFAULT_HOTKEY))),
            ("音频来源", hints.source),
            ("录制方式", hints.mode),
            ("输出去向", hints.output),
        ):
            row = rows.get(key)
            if row is not None:
                row.value_label.setText(value)
        output_row = rows.get("输出去向")
        if output_row is not None:
            output_row.setProperty("outputWarning", hints.output_warning)
            if callable(getattr(output_row, "reapply_styles", None)):
                output_row.reapply_styles()

    def _refresh_about_info(self):
        if not hasattr(self, "_about_runtime_lay"):
            return

        from sayink.speech_recognizer import MODEL_REGISTRY, is_model_downloaded, get_model_info

        active_id = self._config.get("stt.model_id", "")
        ai = get_model_info(active_id)
        active_name = ai["name"] if ai else "未选择"

        downloaded = [m for m in MODEL_REGISTRY if is_model_downloaded(m["id"])]
        total_mb = sum(m["size_mb"] for m in downloaded)

        from sayink.text_polisher import polish_settings_complete

        llm_enabled = self._config.get("llm.enabled", False)
        llm_configured = polish_settings_complete(
            self._config.get("llm.api_url", ""),
            self._config.get("llm.api_key", ""),
            self._config.get("llm.model_name", ""),
        )
        hints = dictation_hints(self._config)
        runtime_items = [
            ("语音识别模型", active_name),
            ("录音快捷键", format_hotkey(self._config.get("hotkey", DEFAULT_HOTKEY))),
            ("音频来源", hints.source),
            ("录制方式", hints.mode),
            ("输出去向", hints.output),
            (
                "文字润色",
                "已开启 · 待验证"
                if llm_enabled and llm_configured
                else "已开启 · 未配置"
                if llm_enabled
                else "已关闭",
            ),
        ]
        for key, val in runtime_items:
            self._about_summary_rows[key].value_label.setText(val)
        output_row = self._about_summary_rows["输出去向"]
        output_row.setProperty("outputWarning", hints.output_warning)
        output_row.reapply_styles()

        while self._about_paths_lay.count():
            item = self._about_paths_lay.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()

        path_items = [
            ("已下载", f"{len(downloaded)} 个 · 约 {total_mb} MB"),
            ("模型目录", str(self._config.models_dir)),
            ("配置文件", str(self._config.config_dir / "config.json")),
            ("日志目录", str(self._config.config_dir / "logs")),
        ]
        for key, val in path_items:
            self._about_paths_lay.addWidget(group_divider())
            row = AboutSummaryRow(key, val)
            row.value_label.setToolTip(val)
            self._about_paths_lay.addWidget(row)

    def _on_about_update_button(self) -> None:
        if self._about_update_btn.text() == "下载并安装":
            self.update_install_requested.emit()
            return
        self.update_check_requested.emit()

    def _on_about_auto_update_toggled(self, checked: bool) -> None:
        if self._loading:
            return
        self._config.set("update.auto_check", bool(checked))

    def set_update_status(self, text: str, *, action: str = "check") -> None:
        if not hasattr(self, "_about_update_status"):
            return
        self._about_update_status.setText(text)
        refresh_update_check_time(self)
        if action == "install":
            self._about_update_btn.setText("下载并安装")
            self._about_update_btn.setEnabled(True)
        elif action == "busy":
            self._about_update_btn.setEnabled(False)
        else:
            self._about_update_btn.setText("检查更新")
            self._about_update_btn.setEnabled(True)

    def _config_source_label(self, source: str | None = None) -> str:
        src = source or self._config.get("audio.input_source", INPUT_SOURCE_MICROPHONE)
        return {
            INPUT_SOURCE_MICROPHONE: "仅麦克风",
            INPUT_SOURCE_SYSTEM: "仅电脑播放",
            INPUT_SOURCE_MIXED: "混合音频",
        }.get(src, "仅麦克风")

    def _config_trigger_label(self, mode: str | None = None) -> str:
        m = mode or self._config.get("audio.trigger_mode", TRIGGER_MODE_HOTKEY)
        return "持续转写" if m == TRIGGER_MODE_CONTINUOUS else "单次转写"

    # ── Shared ─────────────────────────────────────────

    @staticmethod
    def _add_sep(layout: QVBoxLayout):
        s = QFrame()
        s.setFixedHeight(1)
        s.setStyleSheet(f"background: {_tok.BAR_OFF};")
        layout.addWidget(s)

    # ── Load / Save ────────────────────────────────────

    def _selected_input_source(self) -> str:
        if self._src_sys_rb.isChecked():
            return INPUT_SOURCE_SYSTEM
        if self._src_mixed_rb.isChecked():
            return INPUT_SOURCE_MIXED
        return INPUT_SOURCE_MICROPHONE

    def _selected_trigger_mode(self) -> str:
        if self._trigger_hotkey_rb.isChecked():
            return TRIGGER_MODE_HOTKEY
        return TRIGGER_MODE_CONTINUOUS

    def _sync_source_device_widgets(self):
        src = self._selected_input_source()
        mic_on = src in (INPUT_SOURCE_MICROPHONE, INPUT_SOURCE_MIXED)
        sys_on = src in (INPUT_SOURCE_SYSTEM, INPUT_SOURCE_MIXED)
        self._mic_device_combo.setEnabled(mic_on)
        self._system_device_combo.setEnabled(sys_on)
        mixed = self._src_mixed_rb.isChecked()
        show_note = mixed or src == INPUT_SOURCE_SYSTEM
        self._mixed_audio_callout.setText(MIXED_AUDIO_NOTE if mixed else SYSTEM_AUDIO_NOTE)
        self._mixed_audio_callout.setVisible(show_note)
        if hasattr(self, "_mixed_audio_callout_wrap"):
            self._set_section_visible(self._mixed_audio_callout_wrap, show_note)
        self._refresh_audio_device_summary()

    def _refresh_history_controls(self) -> None:
        # Retention still applies to existing dictation even when new history
        # is off, so its controls remain visible and usable in both states.
        prefix = "" if self._history_enabled_row.isChecked() else "已停止新增听写；"
        self._history_cleanup_hint.setText(
            prefix + "已有听写仍按以上规则保留，超限清理无法撤销。保存新听写及启动时执行；会议记录不受影响。"
        )

    def _apply_input_source_radios(self, source: str):
        if source == INPUT_SOURCE_SYSTEM:
            self._src_sys_rb.setChecked(True)
        elif source == INPUT_SOURCE_MIXED:
            self._src_mixed_rb.setChecked(True)
        else:
            self._src_mic_rb.setChecked(True)
        self._sync_source_device_widgets()
        sync_group_tab_stop(self._source_group)

    def _apply_trigger_mode_radios(self, mode: str):
        if mode == TRIGGER_MODE_HOTKEY:
            self._trigger_hotkey_rb.setChecked(True)
        else:
            self._trigger_continuous_rb.setChecked(True)
        sync_group_tab_stop(self._trigger_group)

    def _saved_device_indices(self) -> tuple[int, int]:
        return (
            int(self._config.get("audio.mic_device_index", -1)),
            int(self._config.get("audio.system_device_index", -1)),
        )

    def _ensure_automatic_device_items(self) -> None:
        for combo in (self._mic_device_combo, self._system_device_combo):
            blocker = QSignalBlocker(combo)
            if combo.findData(-1) < 0:
                combo.insertItem(0, "自动选择", -1)
            del blocker

    def _saved_devices_are_listed(self) -> bool:
        mic, system = self._saved_device_indices()
        return (
            self._mic_device_combo.findData(mic) >= 0
            and self._system_device_combo.findData(system) >= 0
        )

    def _sync_saved_audio_devices(self, *, refresh_devices: bool) -> None:
        """Fill the device combos. Automatic (-1) does not scan PortAudio.

        Scanning on every window open and every mode click blocked the UI
        for as long as Windows took to enumerate endpoints.
        """
        mic, system = self._saved_device_indices()
        automatic = mic < 0 and system < 0
        missing = not self._saved_devices_are_listed()
        if refresh_devices and not automatic and missing:
            self._refresh_audio_device_lists()
        else:
            self._ensure_automatic_device_items()
        mic_ok = self._set_combo_by_data(self._mic_device_combo, mic)
        sys_ok = self._set_combo_by_data(self._system_device_combo, system)
        if refresh_devices and (not mic_ok or not sys_ok):
            self._reset_audio_devices_to_auto()

    def _model_cards_match_config(self) -> bool:
        if not self._model_cards:
            return False
        from sayink.speech_recognizer import DEFAULT_MODEL_ID

        active = self._config.get("stt.model_id", DEFAULT_MODEL_ID)
        return getattr(self, "_model_cards_for_id", None) == active

    def _load_settings(self, *, refresh_devices: bool = True, heavy: bool = True):
        self._loading = True
        self._hotkey_edit.set_value(self._config.get("hotkey", DEFAULT_HOTKEY))
        self._auto_start_row.setChecked(self._config.get("auto_start", False))
        self._sound_row.setChecked(self._config.get("sound_enabled", True))
        self._restore_clipboard_row.setChecked(
            self._config.get("output.restore_clipboard", False)
        )
        output_choice = "auto" if self._config.output_follows_input_source() else self._config.output_mode()
        self._set_combo_by_data(self._output_mode_combo, output_choice)
        self._esc_stop_row.setChecked(
            bool(self._config.get("audio.esc_stops_continuous", True))
        )
        self._set_combo_by_data(
            self._download_source_combo,
            self._config.get("stt.download_source", "auto"),
        )
        self._history_enabled_row.setChecked(self._config.get("history.enabled", True))
        if hasattr(self, "_about_auto_update_row"):
            self._about_auto_update_row.setChecked(
                bool(self._config.get("update.auto_check", True))
            )
        self._history_retention_days_spin.setValue(
            int(self._config.get("history.retention_days", 90))
        )
        self._history_max_entries_spin.setValue(
            int(self._config.get("history.max_entries", 5000))
        )
        self._refresh_history_controls()

        theme_mode = normalize_theme_mode(
            self._config.get("appearance.theme_mode", "dark")
        )
        idx = self._theme_combo.findData(theme_mode)
        if idx < 0:
            idx = self._theme_combo.findData("system")
        self._theme_combo.blockSignals(True)
        self._theme_combo.setCurrentIndex(max(0, idx))
        self._theme_combo.blockSignals(False)

        self._apply_input_source_radios(
            self._config.get("audio.input_source", INPUT_SOURCE_MICROPHONE)
        )
        self._apply_trigger_mode_radios(
            self._config.get("audio.trigger_mode", TRIGGER_MODE_HOTKEY)
        )
        # After the radios: the hint describes the loaded mode, not whichever
        # radio happened to be checked before.
        self._refresh_hotkey_hint()

        self._sync_saved_audio_devices(refresh_devices=refresh_devices)

        self._refresh_dir_label()
        cards_changed = not self._model_cards_match_config()
        if cards_changed:
            self._rebuild_model_cards()
        if heavy or cards_changed:
            self._refresh_about_info()

        llm_on = self._config.get("llm.enabled", False)
        self._llm_enable_row.setChecked(llm_on)
        self._llm_prompt_container.setVisible(llm_on)
        self._llm_url_edit.setText(self._config.get("llm.api_url", ""))
        self._llm_key_edit.setText(self._config.get("llm.api_key", ""))
        self._llm_model_edit.setText(self._config.get("llm.model_name", ""))
        self._llm_prompt_edit.setPlainText(self._config.get("llm.prompt", ""))
        self._llm_prompt_edit.setEnabled(True)
        self._load_meeting_settings()

        self._loading = False
        self._refresh_output_controls()
        self._refresh_audio_device_summary()
        self._refresh_meeting_defaults()
        self._update_llm_configuration_status()
        self.usage_hint_changed.emit()

    def _set_combo_by_data(self, combo: QComboBox, value: int) -> bool:
        idx = combo.findData(value)
        if idx >= 0:
            combo.setCurrentIndex(idx)
            return True
        if combo.count() > 0:
            combo.setCurrentIndex(0)
        return False

    def _reset_audio_devices_to_auto(self):
        self._cancel_mic_probe_if_active()
        if not self._loading and not self._confirm_discard_pending():
            return
        was_loading = self._loading
        self._loading = True
        self._set_combo_by_data(self._mic_device_combo, -1)
        self._set_combo_by_data(self._system_device_combo, -1)
        self._loading = was_loading
        if not was_loading:
            self._persist_runtime_settings()
        self._set_mic_test_status("已恢复为「自动选择」，请再点「测试声音」。")
        self._refresh_audio_device_summary()

    def _refresh_audio_device_lists(self):
        mic_sel = self._mic_device_combo.currentData() if self._mic_device_combo.count() else -1
        sys_sel = self._system_device_combo.currentData() if self._system_device_combo.count() else -1

        # Enumeration must not apply the transient empty / automatic entries
        # or emit a setting change before the previous selection is restored.
        mic_blocker = QSignalBlocker(self._mic_device_combo)
        sys_blocker = QSignalBlocker(self._system_device_combo)

        self._mic_device_combo.clear()
        self._mic_device_combo.addItem("自动选择", -1)
        try:
            for dev in list_microphone_devices():
                self._mic_device_combo.addItem(dev.label, dev.index)
        except Exception as e:
            self._mic_device_combo.addItem(f"枚举失败: {e}", -1)

        self._system_device_combo.clear()
        self._system_device_combo.addItem("自动选择", -1)
        try:
            for dev in list_system_capture_devices_for_settings():
                self._system_device_combo.addItem(dev.label, dev.index)
        except Exception as e:
            self._system_device_combo.addItem(f"枚举失败: {e}", -1)

        for combo, selected in ((self._mic_device_combo, mic_sel), (self._system_device_combo, sys_sel)):
            if selected is None:
                continue
            if int(selected) >= 0 and combo.findData(int(selected)) < 0:
                combo.addItem("原设备已不可用，请重新选择", int(selected))
            self._set_combo_by_data(combo, int(selected))
        del mic_blocker, sys_blocker
        self._refresh_audio_device_summary()

    def _set_checked_link(self, button, checked: bool) -> None:
        if button.isChecked() == checked:
            return
        blocker = QSignalBlocker(button)
        button.setChecked(checked)
        del blocker

    def _toggle_advanced_audio(self, visible: bool):
        def change() -> None:
            # Fill the combos while the panel is still hidden. Clearing them
            # after it is on screen flashes an empty dropdown.
            if visible and self._mic_device_combo.count() <= 1:
                self._refresh_audio_device_lists()
            self._set_section_visible(self._advanced_audio_panel, visible)
            self._advanced_audio_btn.setText(
                "收起手动设备" if visible else "手动选择音频设备"
            )
            self._set_checked_link(self._advanced_audio_btn, visible)

        self._without_scroll_jump(self._advanced_audio_btn, change)

    def _current_audio_probe_config(self) -> tuple[str, int, int]:
        return (
            self._selected_input_source(),
            int(self._mic_device_combo.currentData() if self._mic_device_combo.currentData() is not None else -1),
            int(self._system_device_combo.currentData() if self._system_device_combo.currentData() is not None else -1),
        )

    def _toggle_advanced_recognition(self, visible: bool) -> None:
        def change() -> None:
            self._set_section_visible(self._advanced_recognition_panel, visible)
            self._advanced_recognition_btn.setText(
                "收起高级识别设置" if visible else "高级识别设置"
            )
            self._advanced_recognition_btn.setAccessibleName(
                "收起高级识别设置" if visible else "展开高级识别设置"
            )
            self._set_checked_link(self._advanced_recognition_btn, visible)

        self._without_scroll_jump(self._advanced_recognition_btn, change)

    def _refresh_audio_device_summary(self) -> None:
        if not hasattr(self, "_audio_device_summary"):
            return
        source = self._selected_input_source()
        current = self._current_audio_probe_config()
        if current != self._last_audio_test_config:
            self._cancel_mic_probe_if_active()
            self._last_audio_test_config = current
            self._set_mic_test_status("测试会临时采集约 2 秒声音，不保存音频或转写文本。")
        lines = []
        for key, title, combo in (
            (INPUT_SOURCE_MICROPHONE, "麦克风", self._mic_device_combo),
            (INPUT_SOURCE_SYSTEM, "电脑声音", self._system_device_combo),
        ):
            if source not in (key, INPUT_SOURCE_MIXED):
                continue
            automatic = _device_index(combo.currentData()) < 0
            device = "自动选择（测试或录音时确定设备）" if automatic else combo.currentText()
            lines.append(f"{title}：{device}")
        self._audio_device_summary.setText("\n".join(lines))
        # Mixed lists two devices and a single source lists one. Keep the
        # two-line slot so switching 混合音源 / 电脑声音 does not pull the
        # rows underneath upward.
        summary_height = self._audio_device_summary.fontMetrics().lineSpacing() * 2 + 2
        if self._audio_device_summary.minimumHeight() < summary_height:
            self._audio_device_summary.setMinimumHeight(summary_height)

    def _set_probe_controls_enabled(self, enabled: bool) -> None:
        for widget in (
            self._mic_test_btn, self._mic_reset_btn, self._advanced_audio_btn,
            self._advanced_audio_panel, self._src_mic_rb.parentWidget(),
            self._src_sys_rb.parentWidget(), self._src_mixed_rb.parentWidget(),
        ):
            widget.setEnabled(enabled)

    def _disconnect_mic_probe_signals(self):
        try:
            self._mic_test_recorder.volume_changed.disconnect(self._on_mic_probe_volume)
        except TypeError:
            pass
        try:
            self._mic_test_recorder.error.disconnect(self._on_mic_probe_error)
        except TypeError:
            pass
        try:
            self._mic_test_recorder.warning.disconnect(self._on_mic_probe_warning)
        except TypeError:
            pass

    def _run_mic_probe(self):
        if self._mic_probe_active or self._mic_test_recorder.is_recording:
            return
        if self._mic_device_combo.count() <= 1 or self._system_device_combo.count() <= 1:
            self._refresh_audio_device_lists()
        src, mic_idx, sys_idx = self._current_audio_probe_config()
        if mic_idx >= 0 and self._mic_device_combo.findData(mic_idx) < 0:
            mic_idx = -1
        if sys_idx >= 0 and self._system_device_combo.findData(sys_idx) < 0:
            sys_idx = -1
        sys_idx = sanitize_system_device_index(sys_idx)
        if sys_idx < 0:
            self._set_combo_by_data(self._system_device_combo, -1)
        self._mic_test_recorder.configure(
            input_source=src,
            mic_device_index=mic_idx,
            system_device_index=sys_idx,
        )
        self._mic_probe_active = True
        self._mic_probe_max = 0.0
        self._mic_probe_warning = ""
        self._set_probe_controls_enabled(False)
        self._mic_level_bar.setValue(0)
        self._mic_level_bar.show()
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
                self._audio_device_summary.setText("本次测试设备：\n" + "\n".join(devices))
            self._mic_probe_timer.start(2000)

    def _on_mic_probe_volume(self, volume: float):
        if not self._mic_probe_active:
            return
        self._mic_probe_max = max(self._mic_probe_max, float(volume))
        self._mic_level_bar.setValue(round(min(1.0, max(0.0, float(volume)) ** 0.5 * 2) * 100))

    def _on_mic_probe_error(self, msg: str):
        if not self._mic_probe_active:
            return
        self._mic_probe_active = False
        self._mic_probe_timer.stop()
        self._disconnect_mic_probe_signals()
        if self._mic_test_recorder.is_recording:
            self._mic_test_recorder.cancel()
        self._set_probe_controls_enabled(True)
        self._mic_level_bar.hide()
        self._set_mic_test_status(f"测试失败：{msg}。可展开「手动选择音频设备」后重试。")

    def _on_mic_probe_warning(self, msg: str):
        if not self._mic_probe_active:
            return
        self._mic_probe_warning = msg
        self._set_mic_test_status(msg)

    def _finish_mic_probe(self):
        if not self._mic_probe_active:
            return
        self._mic_probe_active = False
        self._mic_probe_timer.stop()
        self._disconnect_mic_probe_signals()
        if self._mic_test_recorder.is_recording:
            self._mic_test_recorder.stop()
        self._set_probe_controls_enabled(True)
        self._mic_level_bar.hide()
        threshold = 0.0015
        peak = self._mic_probe_max
        warn = self._mic_probe_warning or self._mic_test_recorder.last_start_warning
        if peak >= threshold:
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

    def _cancel_mic_probe_if_active(self):
        if not self._mic_probe_active and not self._mic_test_recorder.is_recording:
            return
        self._mic_probe_active = False
        self._mic_probe_timer.stop()
        self._disconnect_mic_probe_signals()
        if self._mic_test_recorder.is_recording:
            self._mic_test_recorder.cancel()
        self._set_probe_controls_enabled(True)
        self._mic_level_bar.hide()

    # ── Instant apply ──────────────────────────────────

    def set_pending_segment_counter(self, counter) -> None:
        """``counter()`` returns how many captured segments are still waiting
        for recognition; applying an engine/audio change asks before dropping them."""
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

    def _confirm(self, title: str, text: str, *, accept: str, cancel: str = "取消",
                 destructive: bool = False) -> bool:
        """Yes/No question with Chinese buttons (Qt's standard buttons stay
        English because the app ships no QTranslator). Cancel is the default."""
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

    def _revert_input_source_radios(self):
        self._loading = True
        self._apply_input_source_radios(
            self._config.get("audio.input_source", INPUT_SOURCE_MICROPHONE)
        )
        self._loading = False

    def _revert_trigger_mode_radios(self):
        self._loading = True
        self._apply_trigger_mode_radios(
            self._config.get("audio.trigger_mode", TRIGGER_MODE_HOTKEY)
        )
        self._loading = False

    def _persist_runtime_settings(self):
        source = self._selected_input_source()
        if source != self._config.get("audio.input_source", INPUT_SOURCE_MICROPHONE):
            self._sync_output_mode_with_source(source)
        self._config.set("audio.input_source", source)
        self._config.set("audio.trigger_mode", self._selected_trigger_mode())
        self._config.set(
            "audio.mic_device_index",
            _device_index(self._mic_device_combo.currentData()),
        )
        sys_idx = sanitize_system_device_index(
            _device_index(self._system_device_combo.currentData())
        )
        self._config.set("audio.system_device_index", sys_idx)
        if sys_idx < 0:
            self._set_combo_by_data(self._system_device_combo, -1)
        self._config.save_immediate()
        self.settings_changed.emit()
        self._refresh_hotkey_hint()
        # The full About refresh rescans every model file and rebuilds rows.
        # A mode or source click only changes the runtime summary.
        self._refresh_about_runtime_rows()

    def _refresh_hotkey_hint(self) -> None:
        if not hasattr(self, "_hotkey_hint"):
            return
        self._hotkey_hint.setText(dictation_hints(self._config).detail)
        self._hotkey_hint.setToolTip("启动阈值：按住录音约 0.18 秒；持续转写约 0.30 秒，避免误触。")
        continuous = self._selected_trigger_mode() == TRIGGER_MODE_CONTINUOUS
        self._set_section_visible(self._esc_stop_row, continuous)
        self._set_section_visible(self._esc_stop_divider, continuous)
        self._trigger_hotkey_rb.parentWidget().set_copy("单次转写", "按住录音 · 松开后识别")
        self._refresh_output_controls()
        self._refresh_meeting_defaults()

    def _on_theme_mode_changed(self, _index: int = 0):
        if self._loading:
            return
        mode = normalize_theme_mode(self._theme_combo.currentData())
        self._config.set("appearance.theme_mode", mode)
        self._config.save_immediate()
        self.theme_changed.emit(mode)

    def _on_auto_start_toggled(self, checked: bool):
        if self._loading:
            return
        self._config.set("auto_start", checked)
        self.auto_start_changed.emit(checked)

    def _on_sound_toggled(self, checked: bool):
        if self._loading:
            return
        self._config.set("sound_enabled", checked)
        self.sound_enabled_changed.emit(checked)

    def _on_download_source_changed(self, _index: int = 0):
        if self._loading:
            return
        value = self._download_source_combo.currentData() or "auto"
        self._config.set("stt.download_source", value)

    def _on_esc_stop_toggled(self, checked: bool):
        if self._loading:
            return
        self._config.set("audio.esc_stops_continuous", bool(checked))
        self._refresh_hotkey_hint()
        self.usage_hint_changed.emit()

    def _on_restore_clipboard_toggled(self, checked: bool):
        if self._loading:
            return
        self._config.set("output.restore_clipboard", checked)
        self.restore_clipboard_changed.emit(checked)

    def _on_output_mode_changed(self, _index: int = 0):
        if self._loading:
            return

        def change() -> None:
            choice = self._output_mode_combo.currentData()
            if choice == "auto":
                self._config.set("output.follow_input_source", True)
                self._config.follow_input_source_for_output(self._selected_input_source())
            else:
                self._config.set_output_mode(choice, auto=False)
            self._config.save_immediate()
            self._refresh_hotkey_hint()
            self._refresh_about_runtime_rows()
            self.usage_hint_changed.emit()

        self._without_scroll_jump(self._output_mode_combo, change)

    def _sync_output_mode_with_source(self, source: str) -> None:
        self._config.follow_input_source_for_output(source)

    def _refresh_output_controls(self) -> None:
        hints = dictation_hints(self._config)
        automatic = self._config.output_follows_input_source()
        choice = "auto" if automatic else self._config.output_mode()
        was_blocked = self._output_mode_combo.blockSignals(True)
        self._set_combo_by_data(self._output_mode_combo, choice)
        self._output_mode_combo.blockSignals(was_blocked)
        history_only = self._config.history_only_output()
        destination = "仅保存到历史，不输入其他应用" if history_only else "输入当前光标"
        rule = "自动模式随音源切换" if automatic else "固定选择，切换音源后保持不变"
        text = f"当前：{destination}。{rule}。"
        color = _tok.TEXT_SEC
        if hints.output_warning:
            text = "历史已关闭：结果不会保存或输入其他应用。请开启转写历史，或改为输入当前光标。"
            color = _tok.RED
        elif not history_only and self._selected_input_source() != INPUT_SOURCE_MICROPHONE:
            text += " 电脑声音的识别结果也会输入光标处，请确认目标窗口。"
        self._output_mode_hint.setText(text)
        self._output_mode_hint.setStyleSheet(
            f"color: {color}; font-size: {_tok.TYPE_FOOTNOTE}px; background: transparent;"
        )
        self._set_section_visible(self._enable_history_wrap, hints.output_warning)
        self._restore_clipboard_row.setEnabled(not history_only)
        self._restore_clipboard_row.setToolTip("仅在结果输入当前光标时使用" if history_only else "")

    def _refresh_meeting_defaults(self) -> None:
        from sayink.speech_recognizer import get_model_info, is_model_downloaded

        if not hasattr(self, "_meeting_defaults_hint"):
            return
        source = dictation_hints(self._config).source
        model_id = self._config.get("stt.model_id", "")
        info = get_model_info(model_id)
        model_name = info["name"] if info else model_id or "尚未选择"
        follow = f"跟随录音（{source}）"
        if self._meeting_source_combo.itemText(0) != follow:
            self._meeting_source_combo.setItemText(0, follow)
        longest = self._meeting_source_combo.fontMetrics().horizontalAdvance(
            "跟随录音（麦克风 + 电脑声音）"
        ) + 40
        if self._meeting_source_combo.minimumWidth() < longest:
            self._meeting_source_combo.setMinimumWidth(longest)
        if self._meeting_model_combo.itemText(0) != "跟随录音模型":
            self._meeting_model_combo.setItemText(0, "跟随录音模型")
        selected = self._meeting_model_combo.currentData()
        selected_info = get_model_info(selected) if selected else None
        effective_name = selected_info["name"] if selected_info else model_name
        model_missing = bool(selected and not is_model_downloaded(selected))
        if model_missing:
            effective_name = f"{effective_name} 未下载，请到「引擎」下载或选择跟随录音模型"
        effective_source = self._meeting_source_combo.currentText()
        self._meeting_defaults_hint.setText(
            f"新会议：{effective_source} · {effective_name}。仅影响之后开始的会议。"
        )
        self._meeting_defaults_hint.setStyleSheet(
            f"color: {_tok.AMBER_TEXT if model_missing else _tok.TEXT_SEC};"
            f" font-size: {_tok.TYPE_FOOTNOTE}px; background: transparent;"
        )

    def _on_history_enabled_toggled(self, checked: bool):
        def change() -> None:
            self._refresh_history_controls()
            if self._loading:
                return
            self._config.set("history.enabled", checked)
            self._refresh_hotkey_hint()
            self._refresh_about_runtime_rows()
            self.usage_hint_changed.emit()

        self._without_scroll_jump(self._history_enabled_row, change)

    def _on_history_limits_changed(self, _value: int):
        if self._loading:
            return
        self._config.set("history.retention_days", self._history_retention_days_spin.value())
        self._config.set("history.max_entries", self._history_max_entries_spin.value())

    # ── Page: General → 会议 ───────────────────────────

    def _on_meeting_setting_changed(self, *_args):
        if self._loading:
            return
        self._config.set("meeting.input_source", str(self._meeting_source_combo.currentData() or ""))
        self._config.set("meeting.model_id", str(self._meeting_model_combo.currentData() or ""))
        self._config.set("meeting.show_start_card", bool(self._meeting_start_card_row.isChecked()))
        self._refresh_meeting_defaults()

    def _on_recognition_setting_changed(self, *_args):
        if self._loading:
            return
        vad = str(self._meeting_vad_combo.currentData() or "silero")
        language = str(self._meeting_language_combo.currentData() or "auto")
        speaker = self._speaker_enabled_row.isChecked()
        changed = (
            vad != self._config.get("stt.vad", "silero")
            or language != self._config.get("stt.language", "auto")
            or speaker != self._config.get("stt.speaker_model_enabled", True)
        )
        self._config.set("stt.vad", vad)
        self._config.set("stt.language", language)
        self._config.set("stt.speaker_model_enabled", speaker)
        if changed:
            self.recognition_changed.emit()
        self._refresh_recognition_feedback()

    def _on_meeting_glossary_changed(self):
        if self._loading:
            return
        if not hasattr(self, "_meeting_glossary_timer"):
            self._meeting_glossary_timer = QTimer(self)
            self._meeting_glossary_timer.setSingleShot(True)
            self._meeting_glossary_timer.timeout.connect(self._flush_meeting_glossary)
        self._meeting_glossary_timer.start(500)

    def _flush_meeting_glossary(self):
        from sayink.speech_recognizer import MAX_HOTWORD_CHARS, MAX_HOTWORDS

        terms: list[dict] = []
        seen: set[str] = set()
        for raw in self._meeting_glossary_edit.toPlainText().splitlines():
            term = raw.strip()[:MAX_HOTWORD_CHARS]
            if not term or term in seen:
                continue
            seen.add(term)
            terms.append({"term": term})
            if len(terms) >= MAX_HOTWORDS:
                break
        if terms != (self._config.get("stt.glossary", []) or []):
            self._config.set("stt.glossary", terms)
            self.recognition_changed.emit()
            self._refresh_recognition_feedback()

    def _load_meeting_settings(self) -> None:
        self._set_combo_by_data(self._meeting_source_combo, self._config.get("meeting.input_source", "") or "")
        self._set_combo_by_data(self._meeting_model_combo, self._config.get("meeting.model_id", "") or "")
        self._set_combo_by_data(self._meeting_vad_combo, self._config.get("stt.vad", "silero"))
        self._set_combo_by_data(self._meeting_language_combo, self._config.get("stt.language", "auto"))
        self._meeting_start_card_row.setChecked(bool(self._config.get("meeting.show_start_card", True)))
        self._speaker_enabled_row.setChecked(bool(self._config.get("stt.speaker_model_enabled", True)))
        glossary = self._config.get("stt.glossary", []) or []
        text = "\n".join(str(g.get("term", "")) for g in glossary if isinstance(g, dict) and g.get("term"))
        if self._meeting_glossary_edit.toPlainText() != text:
            self._meeting_glossary_edit.setPlainText(text)
        self._minutes_model_edit.setText(str(self._config.get("meeting.model_name", "") or ""))
        from sayink.ui.minutes_templates import populate_template_combo

        populate_template_combo(self._minutes_template_combo, self._config)
        self._set_combo_by_data(self._minutes_language_combo, self._config.get("meeting.output_language", "auto"))
        self._refresh_recognition_feedback()

    def _flush_minutes_fields(self, *_args):
        if self._loading:
            return
        self._config.set("meeting.model_name", self._minutes_model_edit.text().strip())
        self._config.set("meeting.template_id", str(self._minutes_template_combo.currentData() or "general"))
        self._config.set("meeting.output_language", str(self._minutes_language_combo.currentData() or "auto"))

    def _apply_hotkey_setting(self, hotkey: str):
        if self._loading or not hotkey:
            return
        parts = hotkey.lower().split("+")
        has_modifier = any(
            p.strip() in ("ctrl", "alt", "shift", "win", "cmd") for p in parts
        )
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
            self._hotkey_edit.set_value(self._config.get("hotkey", DEFAULT_HOTKEY))
            self._loading = False
            return
        old = self._config.get("hotkey")
        self._config.set("hotkey", hotkey)
        if hotkey != old:
            self.hotkey_updated.emit(hotkey)
        self._refresh_about_runtime_rows()

    def _on_input_source_radio_toggled(self, checked: bool):
        if not checked or self._loading:
            return
        anchor = self._src_mic_rb.parentWidget()
        self._without_scroll_jump(anchor, self._sync_source_device_widgets)
        if not self._confirm_discard_pending():
            self._revert_input_source_radios()
            return
        self._without_scroll_jump(anchor, self._persist_runtime_settings)

    def _on_trigger_mode_radio_toggled(self, checked: bool):
        if not checked or self._loading:
            return
        if not self._confirm_discard_pending():
            self._revert_trigger_mode_radios()
            return
        self._without_scroll_jump(
            self._trigger_hotkey_rb.parentWidget(), self._persist_runtime_settings
        )

    def _on_audio_device_changed(self, _index: int):
        if self._loading:
            return
        if not self._confirm_discard_pending():
            self._loading = True
            self._set_combo_by_data(
                self._mic_device_combo,
                int(self._config.get("audio.mic_device_index", -1)),
            )
            self._set_combo_by_data(
                self._system_device_combo,
                int(self._config.get("audio.system_device_index", -1)),
            )
            self._loading = False
            return
        self._persist_runtime_settings()

    def _flush_llm_fields(self):
        if self._loading:
            return
        from sayink.text_polisher import is_secure_or_local_url

        llm_enabled = self._llm_enable_row.isChecked()
        self._config.set("llm.enabled", llm_enabled)
        llm_url = self._llm_url_edit.text().strip()
        # Checked with polish off too: meeting minutes send to the same endpoint.
        if llm_url and not is_secure_or_local_url(llm_url):
            QMessageBox.warning(
                self,
                "提示",
                "接口地址须以 http:// 或 https:// 开头。",
            )
            self._loading = True
            self._llm_url_edit.setText(self._config.get("llm.api_url", ""))
            self._loading = False
            return
        self._config.set("llm.api_url", llm_url)
        self._config.set("llm.api_key", self._llm_key_edit.text().strip())
        self._config.set("llm.model_name", self._llm_model_edit.text().strip())
        self._config.set("llm.prompt", self._llm_prompt_edit.toPlainText().strip())
        self._config.set("llm.mode", "polish")
        self._update_llm_configuration_status()

    def _on_done(self):
        self._cancel_mic_probe_if_active()
        self._flush_llm_fields()
        self.close()

    # ── LLM Test ───────────────────────────────────────

    def _test_llm(self):
        self._llm_test_worker = self._start_llm_test(
            self._llm_model_edit.text().strip(), self._llm_test_btn, self._llm_test_status
        )

    def _test_minutes_model(self):
        model = self._minutes_model_edit.text().strip() or self._llm_model_edit.text().strip()
        self._minutes_test_status.show()
        self._minutes_test_worker = self._start_llm_test(
            model, self._minutes_test_btn, self._minutes_test_status, ok_text=f"连接正常，纪要将使用 {model}。"
        )

    def _start_llm_test(self, model: str, btn: QPushButton, status: QLabel, *, ok_text: str = "连接正常，可以使用。"):
        url = self._llm_url_edit.text().strip()
        key = self._llm_key_edit.text().strip()
        from sayink.text_polisher import polish_settings_complete

        if not polish_settings_complete(url, key, model):
            self._set_llm_status(status, "请填写完整的接口信息。", "error")
            return None

        class _W(QThread):
            def __init__(self, u, k, m, parent=None):
                super().__init__(parent)
                self.u, self.k, self.m = u, k, m
                self.ok, self.msg = False, ""

            def run(self):
                from sayink.text_polisher import TextPolisher
                self.ok, self.msg = TextPolisher.test_connection(self.u, self.k, self.m)

        worker = _W(url, key, model, self)
        self._test_workers.append(worker)
        worker.finished.connect(lambda w=worker: self._on_test_done(w, btn, status, ok_text))
        self._release_button_without_shifting_focus(btn)
        btn.setText("测试中…")
        self._set_llm_status(status, "正在测试连接…", "busy")
        worker.start()
        return worker

    def _on_test_done(self, w, btn, status: QLabel, ok_text: str):
        try:
            self._test_workers.remove(w)
        except ValueError:
            pass
        btn.setEnabled(True)
        btn.setText("测试连接")
        if w.ok:
            self._set_llm_status(status, ok_text, "ok")
        else:
            self._set_llm_status(status, w.msg, "error")

    def focus_llm_connection(self) -> None:
        """Bring 「接口配置」 into view with the first empty field focused."""
        self.show_page(2)
        page = self._polish_scroll_page()
        if page is not None:
            page.ensureWidgetVisible(self._llm_connection_section)
        for edit in (self._llm_url_edit, self._llm_key_edit, self._llm_model_edit):
            if not edit.text().strip():
                edit.setFocus()
                return
        self._llm_url_edit.setFocus()

    # ── Cleanup ────────────────────────────────────────

    def reload_settings(self, *, refresh_devices: bool = True, heavy: bool = True) -> None:
        """Reload form fields from the current config.

        ``refresh_devices=False`` keeps the open path off PortAudio.
        ``heavy=False`` skips rebuilding model cards and the About summary
        when they already match the config.
        """
        self._load_settings(refresh_devices=refresh_devices, heavy=heavy)

    def cancel_hotkey_capture(self) -> None:
        """Stop in-progress shortcut capture and resume global hotkey listening."""
        self._hotkey_edit.cancel_capture_if_active()

    def cancel_all_downloads(self):
        """Cancel and wait for any in-progress download workers to finish."""
        for worker in list(self._dl_workers.values()):
            if hasattr(worker, "cancel"):
                worker.cancel()
            if hasattr(worker, "isRunning") and worker.isRunning():
                worker.wait(3000)
        self._dl_workers.clear()
        for worker in list(self._test_workers):
            if hasattr(worker, "isRunning") and worker.isRunning():
                worker.wait(1000)
        self._test_workers.clear()

    def on_window_hidden(self) -> None:
        """Stop transient probes/captures and flush edits when the host window hides."""
        self._cancel_mic_probe_if_active()
        self.cancel_hotkey_capture()
        self._flush_llm_fields()

    def closeEvent(self, event):
        self.on_window_hidden()
        self.cancel_all_downloads()
        super().closeEvent(event)

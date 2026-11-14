"""Model settings subpage: current engine hero, storage summary, and downloadable cards."""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sayink.config import Config
from sayink.recognition_settings import ADVANCED_RECOGNITION_PATH
from sayink.runtime_status import RuntimeState, RuntimeStatus
from sayink.speech_recognizer import DOWNLOAD_SOURCE_LABELS
from sayink.ui import design_tokens as tok
from sayink.ui import settings_styles
from sayink.ui.model_card import ModelCard, RATING_TOOLTIP, format_model_ratings
from sayink.ui.settings_components import (
    SettingsPage,
    chain_tab_order,
    elide_middle,
    empty_state,
    footnote,
    labeled_row,
    page_header,
    section_header,
    settings_group,
    settings_section,
)
from sayink.ui.settings_pages.general import set_combo_by_data


class ModelSettingsPage(SettingsPage):
    """Strongly-typed Engine/Model settings subpage owning hero status, storage, and model cards."""

    change_model_dir_requested = pyqtSignal()
    download_source_changed = pyqtSignal(str)
    card_action_requested = pyqtSignal(str, str)

    # Explicitly typed widget/control fields (eliminating 6 dynamic win._* injections)
    model_hero_host: QFrame
    model_hero_layout: QVBoxLayout
    model_hero_status: QLabel | None
    storage_summary_label: QLabel
    change_dir_btn: QPushButton
    download_source_combo: QComboBox
    model_notice_label: QLabel
    cards_container: QWidget
    cards_layout: QVBoxLayout
    model_cards: dict[str, ModelCard]
    model_cards_for_id: str | None
    model_cards_downloaded: bool | None

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.model_hero_status = None
        self.model_cards = {}
        self.model_cards_for_id = None
        self.model_cards_downloaded = None

        self.add(page_header("引擎", "语音在本机识别。下载模型后，需等待载入完成才能转写。"))
        self.model_hero_host = settings_group()
        self.model_hero_layout = QVBoxLayout(self.model_hero_host)
        self.model_hero_layout.setContentsMargins(0, 0, 0, 0)
        self.model_hero_layout.setSpacing(0)
        self.add(settings_section("当前引擎", self.model_hero_host))

        storage_group = settings_group()
        storage_lay = QHBoxLayout(storage_group)
        storage_lay.setContentsMargins(16, 12, 12, 12)
        storage_lay.setSpacing(10)
        self.storage_summary_label = QLabel()
        self.storage_summary_label.setStyleSheet(
            f"color: {tok.TEXT_SEC}; font-size: {tok.TYPE_BODY_SM}px; background: transparent;"
        )
        self.storage_summary_label.setWordWrap(True)
        storage_lay.addWidget(self.storage_summary_label, 1)
        self.change_dir_btn = QPushButton("更改存储…")
        self.change_dir_btn.setProperty("viBtn", "ghostSm")
        self.change_dir_btn.setMinimumHeight(32)
        self.change_dir_btn.setStyleSheet(settings_styles.BTN_GHOST_SM)
        self.change_dir_btn.clicked.connect(self.change_model_dir_requested.emit)
        storage_lay.addWidget(self.change_dir_btn)
        self.add(settings_section("存储", storage_group))

        source_group = settings_group()
        source_lay = QVBoxLayout(source_group)
        source_lay.setContentsMargins(0, 0, 0, 0)
        self.download_source_combo = QComboBox()
        self.download_source_combo.setAccessibleName("模型下载源")
        for value, label in DOWNLOAD_SOURCE_LABELS:
            self.download_source_combo.addItem(label, value)
        self.download_source_combo.currentIndexChanged.connect(self._emit_download_source_changed)
        source_lay.addWidget(labeled_row("下载源", self.download_source_combo))
        self.add(settings_section("下载", source_group))

        self.model_notice_label = QLabel("")
        self.model_notice_label.setObjectName("modelNoticeLabel")
        self.model_notice_label.setWordWrap(True)
        self.model_notice_label.setProperty("viTone", "idle")
        self.model_notice_label.hide()
        self._paint_notice_label()
        self.add(self.model_notice_label)

        self.cards_container = QWidget()
        self.cards_layout = QVBoxLayout(self.cards_container)
        self.cards_layout.setContentsMargins(0, 0, 0, 0)
        self.cards_layout.setSpacing(24)
        self.add(self.cards_container)
        chain_tab_order([self.change_dir_btn, self.download_source_combo])

    def _emit_download_source_changed(self, _index: int = 0) -> None:
        self.download_source_changed.emit(str(self.download_source_combo.currentData() or "auto"))

    def _paint_notice_label(self) -> None:
        tone = self.model_notice_label.property("viTone") or "idle"
        if tone == "ok":
            color, bg = tok.GREEN_TEXT, tok.GREEN_BG
        elif tone == "error":
            color, bg = tok.RED_TEXT, tok.RED_BG
        elif tone in ("busy", "warn"):
            color, bg = tok.AMBER_TEXT, tok.AMBER_SOFT
        else:
            color, bg = tok.TEXT_SEC, tok.SURFACE_PEARL
        self.model_notice_label.setStyleSheet(
            f"color: {color}; background: {bg}; border-radius: {tok.RADIUS_MD}px;"
            f" padding: 10px 14px; font-size: {tok.TYPE_FOOTNOTE}px; font-weight: 700;"
        )

    def show_notice(self, text: str, tone: str = "ok") -> None:
        cleaned = (text or "").strip()
        self.model_notice_label.setProperty("viTone", tone)
        self.model_notice_label.setText(cleaned)
        self._paint_notice_label()
        self.model_notice_label.setVisible(bool(cleaned))

    def apply_theme(self) -> None:
        self.storage_summary_label.setStyleSheet(
            f"color: {tok.TEXT_SEC}; font-size: {tok.TYPE_BODY_SM}px; background: transparent;"
        )
        self._paint_notice_label()

    def paint_hero_status(
        self,
        runtime_status: RuntimeStatus,
        *,
        recognition_pending: bool = False,
    ) -> None:
        """「已下载 ≠ 已载入」— the engine page must say whether the model is usable."""
        label = self.model_hero_status
        if label is None:
            return
        if recognition_pending:
            fg, bg, text = tok.AMBER_TEXT, tok.AMBER_SOFT, "设置待生效 · 本场继续使用原配置"
        elif runtime_status.state is RuntimeState.READY:
            fg, bg, text = tok.GREEN_TEXT, tok.GREEN_BG, "已载入 · 可用"
        elif runtime_status.state is RuntimeState.UNAVAILABLE:
            fg, bg, text = tok.RED_TEXT, tok.RED_BG, runtime_status.label
        else:
            fg, bg, text = tok.AMBER_TEXT, tok.AMBER_SOFT, runtime_status.label
        label.setText(text)
        label.setStyleSheet(
            f"background: {bg}; color: {fg}; border-radius: {tok.RADIUS_PILL}px;"
            f" padding: 3px 10px; font-size: {tok.TYPE_CAPTION}px; font-weight: 700;"
        )

    def refresh_storage_summary(self, config: Config) -> None:
        from sayink.speech_recognizer import AUX_MODEL_REGISTRY, MODEL_REGISTRY, is_model_downloaded

        seen_ids: set[str] = set()
        downloaded = []
        for m in (*MODEL_REGISTRY, *AUX_MODEL_REGISTRY):
            mid = m["id"]
            if mid in seen_ids:
                continue
            seen_ids.add(mid)
            if is_model_downloaded(mid):
                downloaded.append(m)
        total_mb = sum(int(m.get("size_mb", 0) or 0) for m in downloaded)
        path = elide_middle(str(config.models_dir), 36)
        count = len(downloaded)
        self.storage_summary_label.setText(f"已下载 {count} 个 · 约 {total_mb} MB · {path}")
        self.storage_summary_label.setToolTip(str(config.models_dir))

    def refresh_active_hero(
        self,
        config: Config,
        runtime_status: RuntimeStatus,
        *,
        recognition_pending: bool = False,
    ) -> None:
        from sayink.speech_recognizer import DEFAULT_MODEL_ID, get_model_info, is_model_downloaded

        while self.model_hero_layout.count():
            item = self.model_hero_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.model_hero_status = None

        active_id = config.get("stt.model_id", DEFAULT_MODEL_ID)
        info = get_model_info(active_id) if active_id else None
        if not info or not is_model_downloaded(active_id):
            empty_wrap = QWidget()
            empty_lay = QVBoxLayout(empty_wrap)
            empty_lay.setContentsMargins(0, 8, 0, 8)
            empty_lay.addWidget(empty_state("尚未选择已下载的模型，请从下方下载并启用。"))
            self.model_hero_layout.addWidget(empty_wrap)
            return

        card = QWidget()
        lay = QVBoxLayout(card)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(8)

        head = QHBoxLayout()
        title = QLabel(info["name"])
        title.setProperty("viRole", "engineHeroTitle")
        title.setStyleSheet(
            f"color: {tok.TEXT}; font-family: {tok.FONT_DISPLAY}; font-size: {tok.TYPE_HERO}px;"
            f" font-weight: 700; background: transparent;"
        )
        head.addWidget(title)
        badge = QLabel("当前")
        badge.setProperty("viRole", "engineHeroBadge")
        badge.setStyleSheet(
            f"background: {tok.SURFACE_PEARL}; color: {tok.TEXT_SEC};"
            f" border: 1px solid {tok.HAIRLINE};"
            f" border-radius: {tok.RADIUS_PILL}px; padding: 3px 10px;"
            f" font-size: {tok.TYPE_CAPTION}px; font-weight: 700;"
        )
        head.addWidget(badge)
        self.model_hero_status = QLabel()
        self.model_hero_status.setAccessibleName("当前模型载入状态")
        self.model_hero_status.setToolTip("下载完成后仍需载入内存；载入完成前快捷键不会响应。")
        head.addWidget(self.model_hero_status)
        self.paint_hero_status(runtime_status, recognition_pending=recognition_pending)
        head.addStretch()
        size = QLabel(f"{info['size_mb']} MB")
        size.setProperty("viRole", "engineHeroSize")
        size.setStyleSheet(
            f"color: {tok.TEXT_DIM}; font-size: {tok.TYPE_FOOTNOTE}px; background: transparent;"
        )
        head.addWidget(size)
        lay.addLayout(head)

        desc = QLabel(info["description"])
        desc.setProperty("viRole", "engineHeroDesc")
        desc.setWordWrap(True)
        desc.setStyleSheet(
            f"color: {tok.TEXT_SEC}; font-size: {tok.TYPE_BODY_SM}px; background: transparent;"
        )
        lay.addWidget(desc)

        meta = QLabel(
            f"{info['languages']} · "
            f"{format_model_ratings(info['accuracy'], info['speed'])}"
        )
        meta.setProperty("viRole", "engineHeroMeta")
        meta.setStyleSheet(
            f"color: {tok.TEXT_DIM}; font-size: {tok.TYPE_FOOTNOTE}px; background: transparent;"
        )
        meta.setToolTip(RATING_TOOLTIP)
        lay.addWidget(meta)

        self.model_hero_layout.addWidget(card)

    def cards_match_config(self, config: Config) -> bool:
        if not self.model_cards:
            return False
        from sayink.speech_recognizer import DEFAULT_MODEL_ID, is_model_downloaded

        active = config.get("stt.model_id", DEFAULT_MODEL_ID)
        downloaded = bool(active and is_model_downloaded(active))
        return (
            self.model_cards_for_id == active
            and self.model_cards_downloaded == downloaded
        )

    def rebuild_cards(
        self,
        config: Config,
        runtime_status: RuntimeStatus,
        *,
        recognition_pending: bool = False,
    ) -> None:
        from sayink.speech_recognizer import DEFAULT_MODEL_ID, MODEL_REGISTRY, is_model_downloaded

        self.refresh_active_hero(config, runtime_status, recognition_pending=recognition_pending)
        self.refresh_storage_summary(config)

        while self.cards_layout.count():
            item = self.cards_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.model_cards.clear()
        active_id = config.get("stt.model_id", DEFAULT_MODEL_ID)

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
                    card.action_clicked.connect(self.card_action_requested.emit)
                    self.model_cards[m_info["id"]] = card
                    list_lay.addWidget(card)
                section_lay.addWidget(list_host)

            self.cards_layout.addWidget(section)

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
        self.model_cards_for_id = active_id
        self.model_cards_downloaded = bool(active_id and is_model_downloaded(active_id))
        card_buttons = [
            btn
            for card in self.model_cards.values()
            for btn in card.findChildren(QPushButton)
        ]
        chain_tab_order([self.change_dir_btn, self.download_source_combo, *card_buttons])

    def _add_aux_model_section(self) -> None:
        """Shared helpers: cards show file availability, not runtime activation."""
        from sayink.speech_recognizer import AUX_MODEL_REGISTRY, is_model_downloaded

        section = QWidget()
        section_lay = QVBoxLayout(section)
        section_lay.setContentsMargins(0, 0, 0, 0)
        section_lay.setSpacing(10)
        section_lay.addWidget(section_header("公共识别辅助模型"))
        hint = footnote(f"语音检测用于三种模式；说话人区分按模式与音源自动启用。加载状态见 {ADVANCED_RECOGNITION_PATH}。")
        section_lay.addWidget(hint)
        list_host = QWidget()
        list_lay = QVBoxLayout(list_host)
        list_lay.setContentsMargins(0, 0, 0, 0)
        list_lay.setSpacing(12)
        for m_info in AUX_MODEL_REGISTRY:
            downloaded = is_model_downloaded(m_info["id"])
            card = ModelCard(m_info, downloaded, False)
            card.action_clicked.connect(self.card_action_requested.emit)
            self.model_cards[m_info["id"]] = card
            list_lay.addWidget(card)
        section_lay.addWidget(list_host)
        self.cards_layout.addWidget(section)

    def update_download_progress(self, model_id: str, pct: int) -> None:
        card = self.model_cards.get(model_id)
        if card is not None:
            card.set_download_progress(pct)

    def load_config(self, config: Config) -> None:
        set_combo_by_data(
            self.download_source_combo,
            config.get("stt.download_source", "auto"),
        )
        self.refresh_storage_summary(config)


def build_model_page(_owner: object = None, *, parent: QWidget | None = None) -> ModelSettingsPage:
    """Construct a typed ``ModelSettingsPage`` without mutating external window state."""
    return ModelSettingsPage(parent=parent)

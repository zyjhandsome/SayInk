"""Smoke tests for static UI style modules."""

from __future__ import annotations

from PyQt6.QtWidgets import QLabel


def _rgb(hex_color: str) -> tuple[float, float, float]:
    value = hex_color.lstrip("#")
    return tuple(int(value[index:index + 2], 16) / 255 for index in (0, 2, 4))


def _relative_luminance(hex_color: str) -> float:
    channels = [
        channel / 12.92
        if channel <= 0.04045
        else ((channel + 0.055) / 1.055) ** 2.4
        for channel in _rgb(hex_color)
    ]
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _contrast(foreground: str, background: str) -> float:
    first = _relative_luminance(foreground)
    second = _relative_luminance(background)
    return (max(first, second) + 0.05) / (min(first, second) + 0.05)


def _blend(foreground: str, background: str, alpha: float) -> str:
    front = _rgb(foreground)
    back = _rgb(background)
    channels = [
        round((front[index] * alpha + back[index] * (1 - alpha)) * 255)
        for index in range(3)
    ]
    return "#" + "".join(f"{channel:02X}" for channel in channels)


class TestAppStyles:
    def test_global_stylesheet_is_nonempty_string(self):
        from sayink.ui.app_styles import GLOBAL_APP_STYLESHEET

        assert isinstance(GLOBAL_APP_STYLESHEET, str)
        assert len(GLOBAL_APP_STYLESHEET.strip()) > 0


class TestColorContrastContracts:
    def test_small_text_and_semantic_text_meet_aa_on_light_surfaces(self):
        from sayink.ui.design_tokens import tokens_for

        light = tokens_for("light")
        for foreground, background in (
            (light["TEXT_DIM"], light["BG"]),
            (light["ACCENT_TEXT"], light["BG"]),
            (light["RED"], light["BG"]),
            (light["GREEN"], light["BG"]),
            (light["PRIMARY_ON"], light["PRIMARY_CONTAINER"]),
        ):
            assert _contrast(foreground, background) >= 4.5

    def test_dark_selected_text_and_primary_buttons_meet_aa(self):
        from sayink.ui.design_tokens import tokens_for

        dark = tokens_for("dark")
        selected_background = _blend(
            "#FFFFFF", dark["SETTINGS_SIDEBAR_BG"], 0.06
        )
        assert _contrast(dark["TEXT"], selected_background) >= 4.5
        for background in (
            dark["PRIMARY_CONTAINER"],
            dark["PRIMARY_CONTAINER_HOVER"],
            dark["PRIMARY_CONTAINER_PRESSED"],
        ):
            assert _contrast(dark["PRIMARY_ON"], background) >= 4.5


class TestSettingsStyles:
    def test_window_css_available(self):
        import sayink.ui.settings_styles as st

        assert hasattr(st, "WINDOW_CSS")
        assert isinstance(st.WINDOW_CSS, str)

    def test_interactive_styles_provide_visible_focus_rings(self):
        import sayink.ui.settings_styles as st
        from sayink.ui.settings_components import NAV_BTN_STYLE, ROW_RADIO_STYLE

        for style in (
            st.BTN_PRIMARY,
            st.BTN_GHOST_SM,
            st.BTN_DANGER_SM,
            NAV_BTN_STYLE,
            ROW_RADIO_STYLE,
        ):
            assert ":focus" in style
            assert "2px solid" in style

    def test_model_rating_labels_and_download_use_brand_accent(self):
        from sayink.ui.model_card import ModelCard, format_model_ratings

        card = ModelCard(
            {
                "id": "demo",
                "name": "示例模型",
                "size_mb": 12,
                "description": "测试",
                "languages": "中文",
                "accuracy": 5,
                "speed": 4,
            },
            is_downloaded=False,
            is_active=False,
        )

        assert format_model_ratings(5, 4) == "高精度 · 较快"
        assert any("高精度" in label.text() for label in card.findChildren(QLabel))
        assert "#2563EB" in card._action_btn.styleSheet()

    def test_active_model_card_uses_subtle_current_state(self):
        from sayink.ui.design_tokens import ACCENT, ACCENT_SOFT, SURFACE_PEARL, TEXT_SEC
        from sayink.ui.model_card import ModelCard

        card = ModelCard(
            {
                "id": "demo",
                "name": "示例模型",
                "size_mb": 12,
                "description": "测试",
                "languages": "中文",
                "accuracy": 5,
                "speed": 4,
            },
            is_downloaded=True,
            is_active=True,
        )
        try:
            sheet = card.styleSheet()
            assert f"border: 1px solid {ACCENT}" in sheet
            assert f"border: 2px solid {ACCENT}" not in sheet

            current_badge = next(label for label in card.findChildren(QLabel) if label.text() == "当前")
            badge_sheet = current_badge.styleSheet()
            assert f"background: {SURFACE_PEARL}" in badge_sheet
            assert f"color: {TEXT_SEC}" in badge_sheet
            assert ACCENT_SOFT not in badge_sheet

            import sayink.ui.settings_styles as st

            assert card._delete_btn is not None
            assert card._delete_btn.styleSheet() == st.BTN_DANGER_SM
        finally:
            card.close()


class TestSidebarVisualContracts:
    """Sidebar spacing / type / surface polish (Stitch alignment follow-up)."""

    def test_nav_bg_matches_cool_app_background(self):
        from sayink.ui import design_tokens as t

        t.activate("light")
        assert t.NAV_BG.upper() == "#F6F7F8"
        assert t.BG.upper() == "#FFFFFF"

    def test_nav_btn_style_uses_single_left_bar_and_soft_wash(self):
        from sayink.ui.design_tokens import ACCENT, NAV_SELECTED_BG
        from sayink.ui.settings_components import NAV_BTN_STYLE

        assert "font-size: 14px" in NAV_BTN_STYLE
        # Prototype v3: left bar + soft wash + accent label.
        checked_block = NAV_BTN_STYLE.split(":checked")[1].split("}")[0]
        assert f"background: {NAV_SELECTED_BG}" in checked_block
        assert f"color: {ACCENT}" in checked_block
        assert f"border-left: 3px solid {ACCENT}" in checked_block
        assert "border: 1px solid" not in checked_block
        assert "border: 2px solid" not in checked_block

    def test_page_title_avoids_negative_tracking(self):
        from sayink.ui.design_tokens import TEXT_SEC
        from sayink.ui.settings_components import PAGE_TITLE, SECTION_LABEL

        assert "font-size: 20px" in PAGE_TITLE
        assert "font-weight: 700" in PAGE_TITLE
        assert "letter-spacing: -" not in PAGE_TITLE
        assert "letter-spacing: 0" in PAGE_TITLE
        assert TEXT_SEC.lower() in SECTION_LABEL.lower()
        assert "font-size: 14px" in SECTION_LABEL

    def test_group_and_hero_surfaces_are_bordered_cards(self):
        from sayink.ui.design_tokens import BORDER, RADIUS_LG, SURFACE
        from sayink.ui.settings_components import GROUP_STYLE, HERO_CARD_STYLE

        for style in (GROUP_STYLE, HERO_CARD_STYLE):
            assert f"background: {SURFACE}" in style
            assert f"border-radius: {RADIUS_LG}px" in style
            assert f"border: 1px solid {BORDER}" in style
        assert HERO_CARD_STYLE == GROUP_STYLE.replace("settingsGroup", "settingsHeroCard")

    def test_usage_tip_bar_is_neutral(self):
        from sayink.ui.design_tokens import HAIRLINE, SURFACE_PEARL, TEXT_SEC
        from sayink.ui.settings_components import usage_tip_bar

        tip = usage_tip_bar("提示")
        try:
            assert f"background: {SURFACE_PEARL}" in tip.styleSheet()
            assert f"border: 1px solid {HAIRLINE}" in tip.styleSheet()
            label = next(child for child in tip.findChildren(QLabel) if child.text() == "提示")
            assert f"color: {TEXT_SEC}" in label.styleSheet()
        finally:
            tip.close()

    def test_vertical_choice_selected_uses_single_emphasis(self):
        from PyQt6.QtWidgets import QRadioButton

        from sayink.ui import design_tokens as tok
        from sayink.ui.settings_components import ChoiceCard, VerticalChoiceCard

        tok.activate("light")
        radio = QRadioButton()
        radio.setChecked(True)
        card = VerticalChoiceCard("仅麦克风", "收录你的说话声", "mic", radio)
        try:
            sheet = card.styleSheet()
            assert tok.NAV_SELECTED_BG in sheet
            assert f"border: 1px solid {tok.HAIRLINE}" in sheet
            assert "border: 2px solid" not in sheet
            assert "border-left:" not in sheet or "border-left: 0" in sheet
            title = next(label for label in card.findChildren(QLabel) if label.text() == "仅麦克风")
            assert f"color: {tok.TEXT}" in title.styleSheet()
            assert f"color: {tok.ACCENT}" not in title.styleSheet()
        finally:
            card.close()
            radio.close()

        radio2 = QRadioButton()
        radio2.setChecked(True)
        grid = ChoiceCard("仅麦克风", "收录你的说话声", "mic", radio2)
        try:
            sheet = grid.styleSheet()
            assert tok.NAV_SELECTED_BG in sheet
            assert f"border: 1px solid {tok.HAIRLINE}" in sheet
            assert "border: 2px solid" not in sheet
            assert "border-left:" not in sheet or "border-left: 0" in sheet
            title = next(label for label in grid.findChildren(QLabel) if label.text() == "仅麦克风")
            assert f"color: {tok.TEXT}" in title.styleSheet()
            assert f"color: {tok.ACCENT}" not in title.styleSheet()
        finally:
            grid.close()
            radio2.close()

    def test_general_page_header_class_removed(self):
        import sayink.ui.settings_components as components

        assert not hasattr(components, "GeneralPageHeader")

    def test_settings_island_nav_has_no_sidebar(self, tmp_path, monkeypatch):
        import sys
        from PyQt6.QtWidgets import QApplication
        from sayink.config import Config
        from sayink.ui.settings_window import SettingsWindow

        QApplication.instance() or QApplication(sys.argv)
        monkeypatch.setattr(SettingsWindow, "_rebuild_model_cards", lambda self: None)
        monkeypatch.setattr(SettingsWindow, "_refresh_about_info", lambda self: None)
        monkeypatch.setattr(SettingsWindow, "_refresh_audio_device_lists", lambda self: None)
        win = SettingsWindow(Config(config_dir=tmp_path))
        try:
            assert not hasattr(win, "_sidebar")
            assert not hasattr(win, "_island_nav")
            assert win._pages.count() == 4
        finally:
            win.close()


class TestClassicDesktopTokens:
    def test_accent_and_surfaces(self):
        from sayink.ui import design_tokens as t

        t.activate("light")
        light = t.tokens_for("light")
        assert t.ACCENT.upper() == light["ACCENT"].upper() == "#2563EB"
        assert "Inter" not in t.FONT
        assert t.RADIUS_MD == 8
        assert t.STATE_RECORD.upper() in {"#DC2626", "#E5484D", "#EF4444", "#F87171"}
        assert t.STATE_RECOGNIZE in (t.FLOAT_TEXT, t.FLOAT_TEXT_SEC)
        assert t.STATE_POLISH in (t.FLOAT_TEXT, t.FLOAT_TEXT_SEC)
        assert t.SETTINGS_SIDEBAR_BG.upper() == t.SURFACE.upper()
        assert "#" not in t.NAV_SELECTED_BG.lower() or t.NAV_SELECTED_BG.startswith("rgba")

    def test_ink_surfaces_and_primary(self):
        from sayink.ui import design_tokens as t
        from sayink.ui.design_tokens import tokens_for

        light = tokens_for("light")
        dark = tokens_for("dark")
        assert light["BG"] == light["SURFACE"] == "#FFFFFF"
        assert dark["BG"] == dark["SURFACE"] == "#181818"
        assert light["NAV_BG"] == "#F6F7F8"
        assert dark["NAV_BG"] == "#141618"
        assert light["PRIMARY_CONTAINER"] == "#0D0D0D"
        assert light["PRIMARY_ON"] == "#FFFFFF"
        assert dark["PRIMARY_CONTAINER"] == "#FFFFFF"
        assert dark["PRIMARY_ON"] == "#0D0D0D"
        assert t.SIDEBAR_WIDTH == 184
        t.activate("dark")
        assert t.STATE_LISTEN == t.GREEN
        assert t.STATE_WARN == t.AMBER
        assert t.STATE_MUTED == t.FLOAT_TEXT_SEC
        t.activate("light")
        assert t.STATE_LISTEN == t.GREEN
        # U-05: warnings are amber, not the same grey as stopped/cancelled.
        assert t.STATE_WARN == t.AMBER != t.STATE_MUTED

    def test_primary_buttons_use_primary_on(self):
        import sayink.ui.settings_styles as st
        from sayink.ui import design_tokens as t

        t.activate("light")
        st.reload_styles()
        primary = st.build_btn_primary()
        accent_sm = st.build_btn_accent_sm()
        assert f"color: {t.PRIMARY_ON}" in primary
        assert f"color: {t.PRIMARY_ON}" in accent_sm
        assert "color: white" not in primary
        assert "color: white" not in accent_sm

    def test_dark_tokens_differ_from_light(self):
        from sayink.ui.design_tokens import tokens_for

        assert tokens_for("light")["BG"] != tokens_for("dark")["BG"]
        assert tokens_for("dark")["TEXT"].upper() == "#F9FAFB"

    def test_toggle_on_uses_semantic_green(self):
        from sayink.ui import design_tokens as t

        assert t.TOGGLE_ON.upper() == t.GREEN.upper()
        assert t.TOGGLE_ON_HOVER.upper() != t.TOGGLE_ON.upper()
        assert t.TOGGLE_ON_HOVER.upper().startswith("#")


class TestCursorInspiredSettingsPolish:
    def test_switch_on_track_uses_toggle_green(self):
        import sys

        from PyQt6.QtWidgets import QApplication

        from sayink.ui.design_tokens import TOGGLE_ON, TOGGLE_ON_HOVER
        from sayink.ui.settings_components import SwitchControl

        QApplication.instance() or QApplication(sys.argv)
        sw = SwitchControl()
        try:
            sw.setChecked(True, animate=False)
            color = sw._track_color()
            assert color.name().upper() == TOGGLE_ON.upper()
            sw.set_hovered(True)
            hover = sw._track_color()
            assert hover.name().upper() == TOGGLE_ON_HOVER.upper()
        finally:
            sw.close()

    def test_switch_track_is_compact(self):
        from sayink.ui.settings_components import SwitchControl

        assert SwitchControl._TRACK_W == 36
        assert SwitchControl._TRACK_H == 20

    def test_ghost_sm_button_reserves_cjk_label_space(self):
        import sayink.ui.settings_styles as st
        from sayink.ui.design_tokens import (
            CONTROL_BTN_SM_FONT_PX,
            CONTROL_BTN_SM_HEIGHT,
        )

        assert f"min-height: {CONTROL_BTN_SM_HEIGHT}px" in st.BTN_GHOST_SM
        assert f"max-height: {CONTROL_BTN_SM_HEIGHT}px" in st.BTN_GHOST_SM
        assert f"font-size: {CONTROL_BTN_SM_FONT_PX}px" in st.BTN_GHOST_SM
        assert "QPushButton:checked" in st.BTN_GHOST_SM

    def test_accent_and_ghost_sm_share_size_box(self):
        import sayink.ui.settings_styles as st
        from sayink.ui.design_tokens import (
            CONTROL_BTN_SM_FONT_PX,
            CONTROL_BTN_SM_HEIGHT,
            CONTROL_BTN_SM_PAD_H,
        )

        for sheet in (st.BTN_ACCENT_SM, st.BTN_GHOST_SM):
            assert f"min-height: {CONTROL_BTN_SM_HEIGHT}px" in sheet
            assert f"max-height: {CONTROL_BTN_SM_HEIGHT}px" in sheet
            assert f"font-size: {CONTROL_BTN_SM_FONT_PX}px" in sheet
            assert f"padding: 0px {CONTROL_BTN_SM_PAD_H}px" in sheet
            assert "border: 1px solid" in sheet

    def test_model_card_select_and_delete_match_height(self):
        from sayink.ui.design_tokens import CONTROL_BTN_SM_HEIGHT
        from sayink.ui.model_card import ModelCard

        card = ModelCard(
            {
                "id": "demo",
                "name": "示例模型",
                "size_mb": 12,
                "description": "测试",
                "languages": "中文",
                "accuracy": 5,
                "speed": 4,
            },
            is_downloaded=True,
            is_active=False,
        )
        try:
            assert card._select_btn is not None
            assert card._delete_btn is not None
            assert card._select_btn.height() == CONTROL_BTN_SM_HEIGHT
            assert card._delete_btn.height() == CONTROL_BTN_SM_HEIGHT
            assert card._select_btn.height() == card._delete_btn.height()
        finally:
            card.close()

    def test_labeled_row_puts_control_on_the_right(self):
        import sys

        from PyQt6.QtWidgets import QApplication, QComboBox, QLabel

        from sayink.ui.settings_components import labeled_row

        QApplication.instance() or QApplication(sys.argv)
        combo = QComboBox()
        row = labeled_row("麦克风", combo, "自动选择可用设备")
        try:
            labels = [lbl.text() for lbl in row.findChildren(QLabel)]
            assert "麦克风" in labels
            assert "自动选择可用设备" in labels
            lay = row.layout()
            assert lay.count() >= 2
            assert lay.itemAt(lay.count() - 1).widget() is combo
        finally:
            row.close()
            combo.close()

    def test_nav_icon_active_stays_neutral_not_accent(self):
        from pathlib import Path

        from sayink.ui import nav_icons as mod
        from sayink.ui.nav_icons import nav_icon

        idle = nav_icon("general", active=False)
        active = nav_icon("general", active=True)
        assert not idle.isNull() and not active.isNull()

        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "TEXT if active else TEXT_SEC" in src
        assert "ACCENT if active" not in src


class TestSettingsControlAlignment:
    """History numeric spins + scrollbar policy (settings-control-alignment)."""

    def test_window_css_styles_spinbox_like_inputs(self):
        import sayink.ui.settings_styles as st
        from sayink.ui.design_tokens import ACCENT_FOCUS, CONTROL_BORDER, RADIUS_MD, SURFACE

        assert "QSpinBox {" in st.WINDOW_CSS or "QSpinBox {{" in st.WINDOW_CSS
        assert f"border: 1px solid {CONTROL_BORDER}" in st.WINDOW_CSS
        assert f"background: {SURFACE}" in st.WINDOW_CSS
        assert f"border-radius: {RADIUS_MD}px" in st.WINDOW_CSS
        assert "QSpinBox:focus" in st.WINDOW_CSS
        assert f"2px solid {ACCENT_FOCUS}" in st.WINDOW_CSS
        # Flat steppers: buttons + explicit chevron images (bare buttons hide arrows).
        assert "QSpinBox::up-button" in st.WINDOW_CSS
        assert "QSpinBox::down-button" in st.WINDOW_CSS
        assert "QSpinBox::up-arrow" in st.WINDOW_CSS
        assert "QSpinBox::down-arrow" in st.WINDOW_CSS
        assert "spin_chevron_up.png" in st.WINDOW_CSS
        assert "min-height: 32px" in st.build_spinbox_css()
        from pathlib import Path

        assert Path(st._spin_arrow_urls()[0]).is_file()
        assert Path(st._spin_arrow_urls()[1]).is_file()
        # Same pitfall for combos: ::drop-down without ::down-arrow hides the chevron.
        assert "QComboBox::drop-down" not in st.WINDOW_CSS
        assert "QComboBox {" in st.WINDOW_CSS
        assert f"border: 1px solid {CONTROL_BORDER}" in st.WINDOW_CSS

    def test_numeric_control_width_token(self):
        from sayink.ui.design_tokens import CONTROL_NUMERIC_WIDTH

        assert CONTROL_NUMERIC_WIDTH == 120

    def test_device_combo_width_token(self):
        from sayink.ui.design_tokens import CONTROL_DEVICE_COMBO_WIDTH

        assert CONTROL_DEVICE_COMBO_WIDTH == 320

    def test_history_spins_share_fixed_width(self, config, monkeypatch):
        import sys

        from PyQt6.QtWidgets import QApplication

        from sayink.ui.design_tokens import CONTROL_NUMERIC_WIDTH
        from sayink.ui.settings_window import SettingsWindow

        QApplication.instance() or QApplication(sys.argv)
        monkeypatch.setattr(SettingsWindow, "_rebuild_model_cards", lambda self: None)
        monkeypatch.setattr(SettingsWindow, "_refresh_about_info", lambda self: None)
        monkeypatch.setattr(SettingsWindow, "_refresh_audio_device_lists", lambda self: None)

        win = SettingsWindow(config)
        try:
            assert win._history_retention_days_spin.minimumWidth() == CONTROL_NUMERIC_WIDTH
            assert win._history_max_entries_spin.minimumWidth() == CONTROL_NUMERIC_WIDTH
            assert win._history_retention_days_spin.maximumWidth() == CONTROL_NUMERIC_WIDTH
            assert win._history_max_entries_spin.maximumWidth() == CONTROL_NUMERIC_WIDTH
        finally:
            win.close()

    def test_device_combos_share_fixed_width(self, config, monkeypatch):
        import sys

        from PyQt6.QtWidgets import QApplication

        from sayink.ui.design_tokens import CONTROL_DEVICE_COMBO_WIDTH
        from sayink.ui.settings_window import SettingsWindow

        QApplication.instance() or QApplication(sys.argv)
        monkeypatch.setattr(SettingsWindow, "_rebuild_model_cards", lambda self: None)
        monkeypatch.setattr(SettingsWindow, "_refresh_about_info", lambda self: None)
        monkeypatch.setattr(SettingsWindow, "_refresh_audio_device_lists", lambda self: None)

        win = SettingsWindow(config)
        try:
            assert win._mic_device_combo.minimumWidth() == CONTROL_DEVICE_COMBO_WIDTH
            assert win._system_device_combo.minimumWidth() == CONTROL_DEVICE_COMBO_WIDTH
            assert win._mic_device_combo.maximumWidth() == CONTROL_DEVICE_COMBO_WIDTH
            assert win._system_device_combo.maximumWidth() == CONTROL_DEVICE_COMBO_WIDTH
        finally:
            win.close()

    def test_history_switch_and_spin_right_edges_align_under_dark(
        self, config, monkeypatch
    ):
        import sys

        from PyQt6.QtWidgets import QApplication

        from sayink.ui.settings_window import SettingsWindow
        from sayink.ui.theme import apply_theme

        qapp = QApplication.instance() or QApplication(sys.argv)
        monkeypatch.setattr(SettingsWindow, "_rebuild_model_cards", lambda self: None)
        monkeypatch.setattr(SettingsWindow, "_refresh_about_info", lambda self: None)
        monkeypatch.setattr(SettingsWindow, "_refresh_audio_device_lists", lambda self: None)

        apply_theme(mode="dark")
        win = SettingsWindow(config)
        try:
            win.resize(960, 620)
            win.show()
            qapp.processEvents()
            switch = win._history_enabled_row._switch
            spin = win._history_retention_days_spin
            host = win._pages.widget(0)
            switch_right = switch.mapTo(host, switch.rect().topRight()).x()
            spin_right = spin.mapTo(host, spin.rect().topRight()).x()
            assert abs(switch_right - spin_right) <= 1
        finally:
            win.close()
            apply_theme(mode="light")

    def test_settings_page_scrollbar_as_needed(self):
        import sys

        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QApplication, QSizePolicy

        from sayink.ui.settings_components import SettingsPage, footnote

        QApplication.instance() or QApplication(sys.argv)
        page = SettingsPage()
        try:
            assert (
                page.verticalScrollBarPolicy()
                == Qt.ScrollBarPolicy.ScrollBarAsNeeded
            )
            # No overflow → reserve gutter so later tall content won't widen/narrow.
            page._sync_scroll_gutter(0, 0)
            assert page.viewportMargins().right() > 0
            page._sync_scroll_gutter(0, 100)
            assert page.viewportMargins().right() == 0
            body = page.widget()
            assert body is not None
            assert body.sizePolicy().verticalPolicy() == QSizePolicy.Policy.Maximum
            note = footnote("测试脚注换行高度不应虚高")
            assert note.sizePolicy().verticalPolicy() == QSizePolicy.Policy.Maximum
        finally:
            page.close()

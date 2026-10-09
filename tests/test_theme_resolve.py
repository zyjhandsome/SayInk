"""Theme mode resolution, defaults, and config persistence."""

from __future__ import annotations

from pathlib import Path

import pytest


class TestResolveEffectiveTheme:
    def test_explicit_light(self):
        from sayink.ui.theme import resolve_effective_theme

        assert resolve_effective_theme("light") == "light"

    def test_explicit_dark(self):
        from sayink.ui.theme import resolve_effective_theme

        assert resolve_effective_theme("dark") == "dark"

    def test_system_follows_probe(self):
        from sayink.ui.theme import resolve_effective_theme

        assert resolve_effective_theme("system", system_is_light=True) == "light"
        assert resolve_effective_theme("system", system_is_light=False) == "dark"

    def test_unknown_mode_falls_back_to_system(self):
        from sayink.ui.theme import resolve_effective_theme

        assert resolve_effective_theme("neon", system_is_light=False) == "dark"
        assert resolve_effective_theme("", system_is_light=True) == "light"


class TestThemeConfigDefault:
    def test_default_theme_mode_is_dark(self, tmp_path: Path):
        from sayink.config import Config

        cfg = Config(config_dir=tmp_path)
        assert cfg.get("appearance.theme_mode") == "dark"

    def test_theme_mode_persists_across_reload(self, tmp_path: Path):
        from sayink.config import Config

        cfg = Config(config_dir=tmp_path)
        cfg.set("appearance.theme_mode", "dark")
        cfg.save_immediate()

        cfg2 = Config(config_dir=tmp_path)
        assert cfg2.get("appearance.theme_mode") == "dark"


class TestTokensFor:
    def test_light_and_dark_differ_on_background(self):
        from sayink.ui.design_tokens import tokens_for

        light = tokens_for("light")
        dark = tokens_for("dark")
        assert light["BG"] != dark["BG"]
        assert light["TEXT"] != dark["TEXT"]
        assert dark["BG"].startswith("#") or dark["BG"].startswith("rgb")

    def test_activate_updates_module_level_bg(self):
        from sayink.ui import design_tokens as dt

        dt.activate("dark")
        assert dt.BG == dt.tokens_for("dark")["BG"]
        dt.activate("light")
        assert dt.BG == dt.tokens_for("light")["BG"]


class TestSettingsAppearanceEntry:
    def test_appearance_combo_exists_and_persists(self, tmp_path: Path, monkeypatch):
        import sys

        from PyQt6.QtWidgets import QApplication

        from sayink.config import Config
        from sayink.ui.settings_window import SettingsWindow
        from sayink.ui.theme import apply_theme

        QApplication.instance() or QApplication(sys.argv)
        monkeypatch.setattr(SettingsWindow, "_rebuild_model_cards", lambda self: None)
        monkeypatch.setattr(SettingsWindow, "_refresh_about_info", lambda self: None)
        monkeypatch.setattr(SettingsWindow, "_refresh_audio_device_lists", lambda self: None)

        cfg = Config(config_dir=tmp_path)
        win = SettingsWindow(cfg)

        assert win._theme_combo.findData("system") >= 0
        assert win._theme_combo.findData("light") >= 0
        assert win._theme_combo.findData("dark") >= 0

        dark_index = win._theme_combo.findData("dark")
        win._theme_combo.setCurrentIndex(dark_index)
        assert cfg.get("appearance.theme_mode") == "dark"

        apply_theme(mode="dark", surfaces=(win,))
        from sayink.ui import design_tokens as tok

        css = win.styleSheet()
        assert tok.tokens_for("dark")["TEXT"] in css
        assert "background: transparent" in css
        dialog_block = css.split("QDialog {", 1)[1].split("}", 1)[0]
        assert "background: transparent" in dialog_block
        assert f"background: {tok.tokens_for('dark')['BG']}" not in dialog_block

    def test_settings_island_header_restyles_on_dark_theme(
        self, tmp_path: Path, monkeypatch
    ):
        """Embedded settings host has no island title/close; pages stay theme-aware."""
        import sys

        from PyQt6.QtWidgets import QApplication

        from sayink.config import Config
        from sayink.ui import design_tokens as tok
        from sayink.ui.settings_window import SettingsWindow
        from sayink.ui.theme import apply_theme

        QApplication.instance() or QApplication(sys.argv)
        monkeypatch.setattr(SettingsWindow, "_rebuild_model_cards", lambda self: None)
        monkeypatch.setattr(SettingsWindow, "_refresh_about_info", lambda self: None)
        monkeypatch.setattr(SettingsWindow, "_refresh_audio_device_lists", lambda self: None)

        win = SettingsWindow(Config(config_dir=tmp_path))
        try:
            apply_theme(mode="dark", surfaces=(win,))
            assert tok.TEXT.upper() == "#F9FAFB"
            assert not hasattr(win, "_island_title")
            assert not hasattr(win, "_close_btn")
            assert not hasattr(win, "_sheet")
            assert "TRANSPARENT" in win._pages_host.styleSheet().upper()
        finally:
            win.close()
            apply_theme(mode="light")

    def test_general_labels_follow_dark_text_tokens(self, tmp_path: Path, monkeypatch):
        """Regression: inline styles must not stay locked to light TEXT on dark BG."""
        import sys

        from PyQt6.QtWidgets import QApplication, QLabel

        from sayink.config import Config
        from sayink.ui import design_tokens as tok
        from sayink.ui.settings_components import CompactPickCard, ToggleOptionRow
        from sayink.ui.settings_window import SettingsWindow
        from sayink.ui.theme import apply_theme

        QApplication.instance() or QApplication(sys.argv)
        monkeypatch.setattr(SettingsWindow, "_rebuild_model_cards", lambda self: None)
        monkeypatch.setattr(SettingsWindow, "_refresh_about_info", lambda self: None)
        monkeypatch.setattr(SettingsWindow, "_refresh_audio_device_lists", lambda self: None)

        win = SettingsWindow(Config(config_dir=tmp_path))
        try:
            apply_theme(mode="dark", surfaces=(win,))
            assert tok.TEXT.upper() == "#F9FAFB"

            title = win._theme_title_label.styleSheet().upper()
            assert "#F9FAFB" in title

            row = win._auto_start_row
            assert isinstance(row, ToggleOptionRow)
            row_title = next(
                lb for lb in row.findChildren(QLabel) if lb.property("viRole") == "rowTitle"
            )
            assert tok.TEXT.upper() in row_title.styleSheet().upper()
            assert "#111827" not in row_title.styleSheet().upper()

            picks = win.findChildren(CompactPickCard)
            assert picks
            pick_title = next(
                lb for lb in picks[0].findChildren(QLabel)
                if lb.property("viRole") == "pickTitle"
            )
            assert tok.TEXT.upper() in pick_title.styleSheet().upper()

            assert tok.TEXT_DIM.upper() in win._theme_desc_label.styleSheet().upper()
        finally:
            win.close()
            apply_theme(mode="light")

    def test_light_reapply_clears_dark_settings_chrome(self, tmp_path: Path, monkeypatch):
        """Regression: dark→light must refresh pages host, ghost buttons, prompt edit."""
        import sys

        from PyQt6.QtWidgets import QApplication, QLabel

        from sayink.config import Config
        from sayink.ui import design_tokens as tok
        from sayink.ui.model_card import ModelCard
        from sayink.ui.settings_window import SettingsWindow
        from sayink.ui.theme import apply_theme

        QApplication.instance() or QApplication(sys.argv)
        monkeypatch.setattr(SettingsWindow, "_rebuild_model_cards", lambda self: None)
        monkeypatch.setattr(SettingsWindow, "_refresh_about_info", lambda self: None)
        monkeypatch.setattr(SettingsWindow, "_refresh_audio_device_lists", lambda self: None)

        win = SettingsWindow(Config(config_dir=tmp_path))
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
            parent=win,
        )
        try:
            apply_theme(mode="dark", surfaces=(win,))
            card.reapply_styles()
            assert "#181818" in card.styleSheet().upper()

            apply_theme(mode="light", surfaces=(win,))
            card.reapply_styles()
            assert tok.BG.upper() == "#FFFFFF"
            assert "TRANSPARENT" in win._pages_host.styleSheet().upper()
            assert "#111827" not in win._pages_host.styleSheet().upper()
            assert not hasattr(win, "_sheet")

            ghost = win._llm_key_toggle.styleSheet().upper()
            assert tok.SURFACE_PEARL.upper() in ghost
            assert "#374151" not in ghost

            prompt = win._llm_prompt_edit.styleSheet().upper()
            assert tok.INPUT_BG.upper() in prompt
            assert "#1F2937" not in prompt

            assert tok.SURFACE.upper() in card.styleSheet().upper()
            assert "#1F2937" not in card.styleSheet().upper()

            # Current-engine hero must repaint with light TEXT (not pale-on-white).
            win._rebuild_model_cards = lambda: None  # type: ignore[method-assign]
            # Force hero rebuild under light tokens with a stubbed active model.
            from sayink.speech_recognizer import DEFAULT_MODEL_ID, MODEL_REGISTRY

            info = next(m for m in MODEL_REGISTRY if m["id"] == DEFAULT_MODEL_ID)
            monkeypatch.setattr(
                "sayink.speech_recognizer.get_model_info", lambda _id: info
            )
            monkeypatch.setattr(
                "sayink.speech_recognizer.is_model_downloaded", lambda _id: True
            )
            win._config.set("stt.model_id", DEFAULT_MODEL_ID)
            win._refresh_active_model_hero()
            title = next(
                lb for lb in win._model_hero_host.findChildren(QLabel)
                if lb.property("viRole") == "engineHeroTitle"
            )
            assert tok.TEXT.upper() in title.styleSheet().upper()
            assert "#F9FAFB" not in title.styleSheet().upper()
            badge = next(
                lb for lb in win._model_hero_host.findChildren(QLabel)
                if lb.property("viRole") == "engineHeroBadge"
            )
            assert badge.text() == "当前"
            assert tok.SURFACE_PEARL.upper() in badge.styleSheet().upper()
            assert tok.TEXT_SEC.upper() in badge.styleSheet().upper()
            assert tok.ACCENT_SOFT.upper() not in badge.styleSheet().upper()
            assert tok.ACCENT_TEXT.upper() not in badge.styleSheet().upper()

            # Polish action buttons share one right-edge column width.
            assert win._llm_key_toggle.width() == win._llm_test_btn.width() == win._prompt_reset_btn.width()
        finally:
            card.close()
            win.close()
            apply_theme(mode="light")


class TestSurfaceThemeReapply:
    def test_system_mode_updates_application_palette_for_both_axes(self):
        import sys

        from PyQt6.QtGui import QColor, QPalette
        from PyQt6.QtWidgets import QApplication

        from sayink.ui import design_tokens as tok
        from sayink.ui.theme import apply_theme

        app = QApplication.instance() or QApplication(sys.argv)
        try:
            assert apply_theme(app, mode="system", system_is_light=False) == "dark"
            assert (
                app.palette().color(QPalette.ColorRole.Window).name().upper()
                == QColor(tok.BG).name().upper()
            )

            assert apply_theme(app, mode="system", system_is_light=True) == "light"
            assert (
                app.palette().color(QPalette.ColorRole.Window).name().upper()
                == QColor(tok.BG).name().upper()
            )
        finally:
            apply_theme(app, mode="light")

    def test_float_light_not_locked_to_legacy_dark(self):
        import sys

        from PyQt6.QtWidgets import QApplication

        from sayink.ui.floating_window import FloatingWindow
        from sayink.ui.theme import apply_theme

        QApplication.instance() or QApplication(sys.argv)
        win = FloatingWindow()
        apply_theme(mode="light", surfaces=(win,))
        sheet = win._container.styleSheet()
        assert (
            "255, 255, 255" in sheet
            or "243, 244, 246" in sheet
            or "#F3F4F6" in sheet.upper()
            or "#FFFFFF" in sheet.upper()
        )
        assert "39, 39, 41" not in sheet
        assert "islandContainer" in sheet

    def test_tray_menu_stylesheet_follows_dark(self):
        from sayink.ui.theme import apply_theme
        from sayink.ui.tray_icon import _menu_stylesheet

        apply_theme(mode="dark")
        css = _menu_stylesheet()
        assert "#181818" in css.upper()
        assert "#F9FAFB" in css.upper()

    def test_history_spinbox_widths_equal_under_dark(self, tmp_path: Path, monkeypatch):
        import sys

        from PyQt6.QtWidgets import QApplication

        from sayink.config import Config
        from sayink.ui.settings_window import SettingsWindow
        from sayink.ui.theme import apply_theme

        QApplication.instance() or QApplication(sys.argv)
        monkeypatch.setattr(SettingsWindow, "_rebuild_model_cards", lambda self: None)
        monkeypatch.setattr(SettingsWindow, "_refresh_about_info", lambda self: None)
        monkeypatch.setattr(SettingsWindow, "_refresh_audio_device_lists", lambda self: None)

        win = SettingsWindow(Config(config_dir=tmp_path))
        apply_theme(mode="dark", surfaces=(win,))
        assert (
            win._history_retention_days_spin.width()
            == win._history_max_entries_spin.width()
        )

    def test_history_reapply_keeps_construct_visual_language(self, tmp_path: Path):
        """Theme reapply must not regress History construct QSS (focus/hover/accent)."""
        import sys

        from PyQt6.QtWidgets import QApplication

        from sayink.history_store import HistoryStore
        from sayink.ui.history_window import HistoryWindow
        from sayink.ui.theme import apply_theme

        QApplication.instance() or QApplication(sys.argv)
        store = HistoryStore(tmp_path / "history.db")
        win = HistoryWindow(store)
        apply_theme(mode="dark", surfaces=(win,))

        search = win._search_edit.styleSheet()
        assert "QLineEdit:focus" in search
        assert "#222528" in search.upper()  # dark SURFACE_PEARL

        list_css = win._session_list.styleSheet()
        assert "border-left" in list_css
        assert "item:hover" in list_css
        assert "background: transparent" in list_css

        details = win._details.styleSheet()
        assert "background: transparent" in details
        assert "border: none" in details

        assert hasattr(win, "_title_label")
        title_css = win._title_label.styleSheet().upper()
        assert "#F9FAFB" in title_css  # dark TEXT

        undo_css = win._undo_bar.styleSheet().upper()
        assert "#222528" in undo_css  # dark SURFACE_PEARL

        feedback_css = win._feedback_label.styleSheet().upper()
        assert "#F9FAFB" in feedback_css

        store.close()

    def test_history_stream_row_labels_follow_theme_reapply(self):
        """Time-stream row labels must restyle when the theme axis flips."""
        import sys

        from PyQt6.QtWidgets import QApplication, QLabel

        from tests.test_history_window import FakeHistoryStore
        from sayink.ui import design_tokens as tok
        from sayink.ui.history_window import HistoryWindow
        from sayink.ui.theme import apply_theme

        QApplication.instance() or QApplication(sys.argv)
        apply_theme(mode="light")
        light_text = tok.tokens_for("light")["TEXT"].upper()
        dark_text = tok.tokens_for("dark")["TEXT"].upper()

        win = HistoryWindow(FakeHistoryStore())
        try:
            row = win._session_list.itemWidget(win.session_items()[0])
            assert row is not None
            preview = row.findChild(QLabel, "streamPreview")
            assert preview is not None
            light_css = preview.styleSheet().upper()
            assert light_text in light_css

            apply_theme(mode="dark", surfaces=(win,))
            dark_css = preview.styleSheet().upper()
            assert dark_text in dark_css
            assert light_text not in dark_css
        finally:
            win.close()
            apply_theme(mode="light")

    def test_info_callout_uses_token_border_not_emoji(self):
        import sys

        from PyQt6.QtWidgets import QApplication, QLabel

        from sayink.ui.settings_components import info_callout
        from sayink.ui.theme import apply_theme

        QApplication.instance() or QApplication(sys.argv)
        apply_theme(mode="light")
        frame = info_callout("混合采集提示")
        css = frame.styleSheet().upper()
        assert "#F5E6B8" in css or "CALLOUT" in css
        labels = [c.text() for c in frame.findChildren(QLabel)]
        assert "混合采集提示" in labels
        # Prototype v3 callout is text-only (no leading glyph / emoji).
        assert "!" not in labels
        assert "⚠" not in labels
        assert "ℹ" not in labels

    def test_tray_icons_use_semantic_tokens_and_rebuild_on_reapply(self):
        import inspect
        import sys

        from PyQt6.QtWidgets import QApplication

        from sayink.ui.theme import apply_theme
        from sayink.ui import tray_icon as tray_mod
        from sayink.ui.tray_icon import TrayIcon

        src = inspect.getsource(tray_mod._microphone_pixmap)
        assert "#FF6961" not in src
        assert "#D64545" not in src
        assert "STATE_RECORD" in src

        QApplication.instance() or QApplication(sys.argv)
        tray = TrayIcon()
        before = tray._recording_icon
        apply_theme(mode="light", surfaces=(tray,))
        assert tray._recording_icon is not before
        assert not tray._recording_icon.isNull()
        assert not tray._attention_icon.isNull()

    def test_float_close_hover_is_theme_aware(self):
        import sys

        from PyQt6.QtWidgets import QApplication

        from sayink.ui import design_tokens as tok
        from sayink.ui.floating_window import FloatingWindow
        from sayink.ui.theme import apply_theme

        QApplication.instance() or QApplication(sys.argv)
        win = FloatingWindow()
        assert not hasattr(win, "_close_btn")
        assert win._end_btn.text() == "结束"
        apply_theme(mode="light", surfaces=(win,))
        sheet = win._end_btn.styleSheet()
        assert tok.PRIMARY_CONTAINER in sheet
        assert tok.PRIMARY_CONTAINER_HOVER in sheet
        apply_theme(mode="dark", surfaces=(win,))
        dark_sheet = win._end_btn.styleSheet()
        assert tok.PRIMARY_CONTAINER in dark_sheet
        assert tok.PRIMARY_CONTAINER_HOVER in dark_sheet
        apply_theme(mode="light")


class TestSettingsThemeAwareBroadcast:
    def test_settings_reapply_theme_has_no_virole_walk(self):
        import inspect

        from sayink.ui.settings_window import SettingsWindow

        src = inspect.getsource(SettingsWindow.reapply_theme)
        assert "viRole" not in src
        assert "reapply_subtree" in src

    def test_settings_callout_follows_dark_broadcast(self, tmp_path: Path, monkeypatch):
        import sys

        from PyQt6.QtWidgets import QApplication

        from sayink.config import Config
        from sayink.ui import design_tokens as tok
        from sayink.ui.settings_window import SettingsWindow
        from sayink.ui.theme import apply_theme

        QApplication.instance() or QApplication(sys.argv)
        monkeypatch.setattr(SettingsWindow, "_rebuild_model_cards", lambda self: None)
        monkeypatch.setattr(SettingsWindow, "_refresh_about_info", lambda self: None)
        monkeypatch.setattr(SettingsWindow, "_refresh_audio_device_lists", lambda self: None)

        apply_theme(mode="light")
        win = SettingsWindow(Config(config_dir=tmp_path))
        light_border = tok.tokens_for("light")["CALLOUT_BORDER"].upper()
        apply_theme(mode="dark", surfaces=(win,))
        try:
            css = win._mixed_audio_callout.styleSheet().upper()
            assert tok.tokens_for("dark")["CALLOUT_BORDER"].upper() in css
            assert light_border not in css
        finally:
            win.close()
            apply_theme(mode="light")

    def test_llm_status_and_about_toggle_follow_dark(self, tmp_path: Path, monkeypatch):
        import sys

        from PyQt6.QtWidgets import QApplication

        from sayink.config import Config
        from sayink.ui import design_tokens as tok
        from sayink.ui.settings_window import SettingsWindow
        from sayink.ui.theme import apply_theme

        QApplication.instance() or QApplication(sys.argv)
        monkeypatch.setattr(SettingsWindow, "_rebuild_model_cards", lambda self: None)
        monkeypatch.setattr(SettingsWindow, "_refresh_about_info", lambda self: None)
        monkeypatch.setattr(SettingsWindow, "_refresh_audio_device_lists", lambda self: None)

        apply_theme(mode="light")
        win = SettingsWindow(Config(config_dir=tmp_path))
        light_sec = tok.tokens_for("light")["TEXT_SEC"].upper()
        dark_sec = tok.tokens_for("dark")["TEXT_SEC"].upper()
        try:
            apply_theme(mode="dark", surfaces=(win,))
            status_css = win._llm_test_status.styleSheet().upper()
            toggle_css = win._about_paths_toggle.styleSheet().upper()
            assert dark_sec in status_css
            assert light_sec not in status_css
            assert dark_sec in toggle_css
            assert light_sec not in toggle_css
        finally:
            win.close()
            apply_theme(mode="light")


class TestFourSurfaceThemeAwareProtocol:
    def test_four_surfaces_are_theme_aware(self, tmp_path: Path, monkeypatch):
        import sys

        from PyQt6.QtWidgets import QApplication

        from sayink.config import Config
        from sayink.history_store import HistoryStore
        from sayink.ui.floating_window import FloatingWindow
        from sayink.ui.history_window import HistoryWindow
        from sayink.ui.settings_window import SettingsWindow
        from sayink.ui.theme import ThemeAware
        from sayink.ui.tray_icon import TrayIcon

        QApplication.instance() or QApplication(sys.argv)
        monkeypatch.setattr(SettingsWindow, "_rebuild_model_cards", lambda self: None)
        monkeypatch.setattr(SettingsWindow, "_refresh_about_info", lambda self: None)
        monkeypatch.setattr(SettingsWindow, "_refresh_audio_device_lists", lambda self: None)

        settings = SettingsWindow(Config(config_dir=tmp_path))
        history = HistoryWindow(HistoryStore(tmp_path / "history.db"))
        floating = FloatingWindow()
        tray = TrayIcon()
        try:
            for surface in (settings, history, floating, tray):
                assert isinstance(surface, ThemeAware)
        finally:
            settings.close()
            history.close()
            floating.close()
            tray.hide()
            history._store.close()

    def test_apply_theme_dark_refreshes_all_four_surfaces(
        self, tmp_path: Path, monkeypatch
    ):
        import sys

        from PyQt6.QtWidgets import QApplication

        from sayink.config import Config
        from sayink.history_store import HistoryStore
        from sayink.ui import design_tokens as tok
        from sayink.ui.floating_window import FloatingWindow
        from sayink.ui.history_window import HistoryWindow
        from sayink.ui.settings_window import SettingsWindow
        from sayink.ui.theme import apply_theme
        from sayink.ui.tray_icon import TrayIcon

        QApplication.instance() or QApplication(sys.argv)
        monkeypatch.setattr(SettingsWindow, "_rebuild_model_cards", lambda self: None)
        monkeypatch.setattr(SettingsWindow, "_refresh_about_info", lambda self: None)
        monkeypatch.setattr(SettingsWindow, "_refresh_audio_device_lists", lambda self: None)

        apply_theme(mode="light")
        settings = SettingsWindow(Config(config_dir=tmp_path))
        history = HistoryWindow(HistoryStore(tmp_path / "history.db"))
        floating = FloatingWindow()
        tray = TrayIcon()
        before_icon = tray._normal_icon
        apply_theme(mode="dark", surfaces=(settings, history, floating, tray))
        try:
            dark_text = tok.tokens_for("dark")["TEXT"].upper()
            dark_bg = tok.tokens_for("dark")["BG"]
            settings_css = settings.styleSheet()
            history_css = history.styleSheet()
            assert dark_text in settings_css.upper()
            assert dark_text in history_css.upper()
            assert "background: transparent" in settings_css
            assert "background: transparent" in history_css
            settings_dialog = settings_css.split("QDialog {", 1)[1].split("}", 1)[0]
            history_dialog = history_css.split("QDialog {", 1)[1].split("}", 1)[0]
            assert "background: transparent" in settings_dialog
            assert "background: transparent" in history_dialog
            assert f"background: {dark_bg}" not in settings_dialog
            assert f"background: {dark_bg}" not in history_dialog
            float_sheet = floating._container.styleSheet().upper()
            assert tok.tokens_for("dark")["FLOAT_BG"].upper() in float_sheet
            assert tray._normal_icon is not before_icon
            assert tok.TEXT.upper() in history._title_label.styleSheet().upper()
        finally:
            settings.close()
            history.close()
            floating.close()
            tray.hide()
            apply_theme(mode="light")
            history._store.close()

    def test_history_and_settings_cold_start_on_active_dark_axis(
        self, tmp_path: Path, monkeypatch
    ):
        import sys

        from PyQt6.QtWidgets import QApplication

        from sayink.config import Config
        from sayink.history_store import HistoryStore
        from sayink.ui import design_tokens as tok
        from sayink.ui.history_window import HistoryWindow
        from sayink.ui.settings_window import SettingsWindow
        from sayink.ui.theme import apply_theme

        QApplication.instance() or QApplication(sys.argv)
        monkeypatch.setattr(SettingsWindow, "_rebuild_model_cards", lambda self: None)
        monkeypatch.setattr(SettingsWindow, "_refresh_about_info", lambda self: None)
        monkeypatch.setattr(SettingsWindow, "_refresh_audio_device_lists", lambda self: None)

        apply_theme(mode="dark")
        settings = SettingsWindow(Config(config_dir=tmp_path))
        history = HistoryWindow(HistoryStore(tmp_path / "history.db"))
        try:
            dark_text = tok.TEXT.upper()
            settings_css = settings.styleSheet()
            history_css = history.styleSheet()
            assert dark_text in settings_css.upper()
            assert dark_text in history_css.upper()
            assert "background: transparent" in settings_css
            assert "background: transparent" in history_css
            settings_dialog = settings_css.split("QDialog {", 1)[1].split("}", 1)[0]
            history_dialog = history_css.split("QDialog {", 1)[1].split("}", 1)[0]
            assert "background: transparent" in settings_dialog
            assert "background: transparent" in history_dialog
            assert f"background: {tok.BG}" not in settings_dialog
            assert f"background: {tok.BG}" not in history_dialog
            assert tok.TEXT.upper() in history._title_label.styleSheet().upper()
        finally:
            settings.close()
            history.close()
            apply_theme(mode="light")
            history._store.close()

    def test_apply_theme_continues_after_one_surface_fails(self, caplog):
        from sayink.ui.theme import apply_theme

        class Boom:
            def reapply_theme(self) -> None:
                raise RuntimeError("boom")

        class Tracker:
            def __init__(self) -> None:
                self.called = False

            def reapply_theme(self) -> None:
                self.called = True

        boom = Boom()
        tracker = Tracker()
        apply_theme(mode="light", surfaces=(boom, tracker))
        assert tracker.called
        assert any("表面换肤失败" in record.message for record in caplog.records)


@pytest.mark.parametrize("start, target", [("dark", "light"), ("light", "dark")])
def test_runtime_switch_leaves_no_widget_with_old_theme_colors(
    tmp_path: Path, monkeypatch, start, target
):
    """Regression: the engine page's model-list headings and the About page's
    update row were styled once at build time and kept the old theme."""
    import re
    import sys

    from PyQt6.QtCore import QEvent
    from PyQt6.QtWidgets import QApplication, QWidget

    from sayink.config import Config
    from sayink.history_store import HistoryStore
    from sayink.ui import design_tokens as tok
    from sayink.ui.floating_window import FloatingWindow
    from sayink.ui.main_window import PAGE_KEYS, MainWindow
    from sayink.ui.settings_window import SettingsWindow
    from sayink.ui.theme import apply_theme

    def hex_colors(theme):
        found = set()
        for value in tok.tokens_for(theme).values():
            if isinstance(value, str):
                found.update(c.lower() for c in re.findall(r"#[0-9a-fA-F]{6}\b", value))
        return found

    app = QApplication.instance() or QApplication(sys.argv)
    monkeypatch.setattr(SettingsWindow, "_refresh_audio_device_lists", lambda self: None)
    apply_theme(mode=start)
    win = MainWindow(Config(config_dir=tmp_path), HistoryStore(tmp_path / "h.db"))
    floating = FloatingWindow()
    try:
        for key in PAGE_KEYS:
            win.show_page(key)
        apply_theme(
            mode=target,
            surfaces=(win, floating, win._settings, win._history),
        )
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)

        stale_colors = hex_colors(start) - hex_colors(target)
        stale = [
            (type(w).__name__, w.objectName(), w.property("viRole"))
            for w in [win, floating, *win.findChildren(QWidget), *floating.findChildren(QWidget)]
            if any(c in w.styleSheet().lower() for c in stale_colors)
        ]
        assert stale == []
    finally:
        win.close()
        floating.close()
        apply_theme(mode="light")


def test_watch_system_color_scheme_connects_to_style_hints():
    from PyQt6.QtGui import QGuiApplication
    from sayink.ui.theme import watch_system_color_scheme

    assert hasattr(QGuiApplication.styleHints(), "colorSchemeChanged")
    calls = []
    assert watch_system_color_scheme(lambda: calls.append(1)) is True


def test_system_scheme_change_reapplies_only_in_system_mode():
    from unittest.mock import patch
    from tests.helpers.app_harness import app_harness

    for mode, expected in (("system", 1), ("dark", 0), ("light", 0)):
        with app_harness({"appearance.theme_mode": mode}) as h:
            app = h["app"]
            with patch.object(app, "apply_appearance_theme") as apply:
                app._on_system_color_scheme_changed()
            assert apply.call_count == expected

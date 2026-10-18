"""Opening the window and switching mode or audio source must stay on the UI thread
only for work the click actually needs."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from sayink.config import Config
from sayink.ui.settings_window import SettingsWindow
from tests.helpers.app_harness import app_harness


def test_trigger_mode_switch_does_not_reconfigure_audio_model_or_theme():
    with app_harness({"audio.trigger_mode": "hotkey", "audio.input_source": "microphone"}) as h:
        app = h["app"]
        h["recorder"].configure.reset_mock()
        h["recorder"].set_segmenter_profile.reset_mock()
        h["recognizer"].configure.reset_mock()
        h["hotkey"].set_continuous_trigger_mode.reset_mock()
        theme = MagicMock()
        app.apply_appearance_theme = theme
        app._config.set("audio.trigger_mode", "continuous")

        app._on_settings_changed()

        h["recorder"].configure.assert_not_called()
        h["recorder"].set_segmenter_profile.assert_not_called()
        h["recognizer"].configure.assert_not_called()
        theme.assert_not_called()
        h["hotkey"].set_continuous_trigger_mode.assert_called()


def test_audio_source_switch_does_not_enumerate_devices_or_reload_the_model():
    with app_harness({"audio.input_source": "microphone"}) as h:
        app = h["app"]
        h["recorder"].configure.reset_mock()
        h["recognizer"].configure.reset_mock()
        theme = MagicMock()
        app.apply_appearance_theme = theme

        def slow(*_args, **_kwargs):
            time.sleep(1.5)
            raise AssertionError("device plan ran on the settings-change path")

        with patch("sayink.audio_devices.build_recording_plan", slow):
            app._config.set("audio.input_source", "system")
            started = time.perf_counter()
            app._on_settings_changed()
            elapsed = time.perf_counter() - started

        assert elapsed < 0.4
        h["recorder"].configure.assert_called()
        h["recognizer"].configure.assert_not_called()
        theme.assert_not_called()


def test_reopen_paints_before_reload_and_skips_theme_and_device_scan():
    from sayink.app import App

    app = App.__new__(App)
    order = []
    main = MagicMock()
    main.current_page.return_value = "general"
    main.show.side_effect = lambda: order.append("show")
    main.settings_panel.reload_settings.side_effect = lambda *args, **kwargs: order.append(kwargs)
    app._main = main
    app.apply_appearance_theme = MagicMock(side_effect=lambda *args, **kwargs: order.append("theme"))
    app._sync_settings_runtime_status = lambda: order.append("status")
    app._updates = MagicMock()

    app._show_main_window(None)

    assert order[0] == "show"
    assert "theme" not in order
    assert {"refresh_devices": False, "heavy": False} in order


def test_automatic_devices_are_not_enumerated_until_the_panel_opens(tmp_path, monkeypatch):
    calls = []

    def fake_refresh(self):
        calls.append("enum")
        # The real refresh adds rows; without one, the re-entrant toggle
        # (setChecked emits toggled) would scan a second time.
        if self._mic_device_combo.count() <= 1:
            self._mic_device_combo.addItem("麦克风", 0)

    monkeypatch.setattr(SettingsWindow, "_refresh_audio_device_lists", fake_refresh)
    monkeypatch.setattr(SettingsWindow, "_rebuild_model_cards", lambda self: None)
    monkeypatch.setattr(SettingsWindow, "_refresh_about_info", lambda self: None)
    win = SettingsWindow(Config(config_dir=tmp_path))
    try:
        assert calls == []
        win._toggle_advanced_audio(True)
        assert calls == ["enum"]
    finally:
        win.close()


def test_reloading_an_open_window_does_not_rebuild_model_cards(tmp_path, monkeypatch):
    builds = []

    def fake_rebuild(self):
        builds.append(1)
        self._model_cards = {"kept": object()}
        self._model_cards_for_id = self._config.get("stt.model_id", "")

    monkeypatch.setattr(SettingsWindow, "_refresh_audio_device_lists", lambda self: None)
    monkeypatch.setattr(SettingsWindow, "_rebuild_model_cards", fake_rebuild)
    monkeypatch.setattr(SettingsWindow, "_refresh_about_info", lambda self: None)
    win = SettingsWindow(Config(config_dir=tmp_path))
    try:
        assert builds == [1]
        win.reload_settings(refresh_devices=False, heavy=False)
        assert builds == [1]
    finally:
        win.close()


def _quiet_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(SettingsWindow, "_refresh_audio_device_lists", lambda self: None)
    monkeypatch.setattr(SettingsWindow, "_rebuild_model_cards", lambda self: None)
    monkeypatch.setattr(SettingsWindow, "_refresh_about_info", lambda self: None)
    return SettingsWindow(Config(config_dir=tmp_path))


def test_audio_devices_are_listed_before_the_panel_is_shown(tmp_path, monkeypatch):
    win = _quiet_settings(monkeypatch, tmp_path)
    try:
        def refresh(self):
            self._mic_device_combo.addItem("麦克风", 2)
            self._system_device_combo.addItem("扬声器", 3)

        monkeypatch.setattr(SettingsWindow, "_refresh_audio_device_lists", refresh)
        seen = []
        panel = win._advanced_audio_panel
        original = panel.setVisible

        def track(visible):
            if visible:
                seen.append(win._mic_device_combo.count())
            original(visible)

        panel.setVisible = track
        win._toggle_advanced_audio(True)
        assert seen == [2]
        assert not panel.isHidden()
    finally:
        win.close()


def test_expanding_sections_keeps_the_link_in_place(tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QApplication

    qapp = QApplication.instance()
    assert qapp is not None
    win = _quiet_settings(monkeypatch, tmp_path)
    try:
        win.resize(880, 640)
        win.show()
        page = win._pages.widget(0)
        qapp.processEvents()
        for button, panel in (
            (win._advanced_audio_btn, win._advanced_audio_panel),
            (win._advanced_recognition_btn, win._advanced_recognition_panel),
        ):
            page.ensureWidgetVisible(button)
            qapp.processEvents()
            before = button.mapTo(page.viewport(), button.rect().topLeft()).y()
            range_before = page.verticalScrollBar().maximum()
            button.click()
            qapp.processEvents()
            after = button.mapTo(page.viewport(), button.rect().topLeft()).y()
            assert panel.isVisible()
            assert page.updatesEnabled()
            assert abs(after - before) <= 4
            button.click()
            qapp.processEvents()
            closed = button.mapTo(page.viewport(), button.rect().topLeft()).y()
            assert not panel.isVisible()
            assert abs(closed - before) <= 4
            assert abs(page.verticalScrollBar().maximum() - range_before) <= 2
    finally:
        win.close()


def test_switching_mixed_to_system_audio_keeps_the_source_card_in_place(tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QApplication

    qapp = QApplication.instance()
    assert qapp is not None
    win = _quiet_settings(monkeypatch, tmp_path)
    try:
        win.resize(960, 640)
        win.show()
        page = win._pages.widget(0)
        qapp.processEvents()
        win._src_mixed_rb.setChecked(True)
        qapp.processEvents()
        card = win._src_mixed_rb.parentWidget()
        page.ensureWidgetVisible(card, 0, 0)
        qapp.processEvents()
        before = card.mapTo(page.viewport(), card.rect().topLeft()).y()
        note_h = win._mixed_audio_callout_wrap.height()
        below = win._mic_test_btn.mapTo(page.viewport(), win._mic_test_btn.rect().topLeft()).y()
        win._src_sys_rb.setChecked(True)
        qapp.processEvents()
        after = card.mapTo(page.viewport(), card.rect().topLeft()).y()
        below_after = win._mic_test_btn.mapTo(page.viewport(), win._mic_test_btn.rect().topLeft()).y()
        assert "电脑声音" in win._mixed_audio_callout.text()
        assert not win._mixed_audio_callout_wrap.isHidden()
        assert page.updatesEnabled()
        assert abs(after - before) <= 4
        assert abs(below_after - below) <= 4
        assert abs(win._mixed_audio_callout_wrap.height() - note_h) <= 4
    finally:
        win.close()

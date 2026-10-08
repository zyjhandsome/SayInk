"""User-facing regressions for navigation, mode feedback and history reflow."""
from __future__ import annotations

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest

from tests.test_history_window import FakeHistoryStore
from tests.test_main_window import _make_main_window
from sayink.ui.history_window import HistoryWindow
from sayink.ui.settings_components import CompactPickCard


@pytest.fixture
def main_window(_qapp_session, tmp_path, monkeypatch):
    win, store = _make_main_window(tmp_path, monkeypatch)
    win.show()
    _qapp_session.processEvents()
    yield win
    win.hide()
    win.deleteLater()
    store.close()


def test_keyboard_can_reach_navigation_and_search(main_window, _qapp_session):
    win = main_window
    win._nav_buttons[2].setFocus()
    QTest.keyClick(win._nav_buttons[2], Qt.Key.Key_Space)
    assert win.current_page() == "engine"
    win._focus_history_search()
    _qapp_session.processEvents()
    assert win.current_page() == "history"
    assert win._history._search_edit.hasFocus()


def test_navigation_shortcuts_pause_during_hotkey_capture(main_window):
    win = main_window
    win._settings.hotkey_capture_started.emit()
    assert all(not key.isEnabled() for key in win._shortcuts)
    win._settings.hotkey_capture_ended.emit()
    assert all(key.isEnabled() for key in win._shortcuts)


def test_runtime_and_trigger_instructions_follow_actual_state(main_window):
    win = main_window
    win._settings.set_runtime_status("模型载入中")
    assert win._runtime_label.text() == "模型载入中"
    win._settings._trigger_hotkey_rb.setChecked(True)
    assert "0.18" in win._settings._hotkey_hint.text()
    assert "松开" in win._settings._hotkey_hint.text()
    assert "按住说话" in win._mode_label.text()
    win._settings._trigger_continuous_rb.setChecked(True)
    hint = win._settings._hotkey_hint.text()
    assert "0.30" in hint
    assert "Esc" in hint
    assert "松开后继续" in hint
    assert "停顿" in hint
    assert "不用" in hint


def test_sidebar_status_is_not_a_card_and_nav_has_no_stale_focus_ring(main_window):
    win = main_window
    # No nav button is focused just by opening the window.
    assert not any(btn.hasFocus() for btn in win._nav_buttons)
    assert all(btn.focusPolicy() == Qt.FocusPolicy.TabFocus for btn in win._nav_buttons)
    assert not hasattr(win, "_nav_heading")
    assert "border-radius: 8px" not in win._runtime_label.styleSheet()
    win._settings.set_runtime_status("模型载入中…")
    assert "AMBER" in win._status_tone(win._runtime_label.text())
    win._settings.set_runtime_status("就绪")
    assert win._status_tone(win._runtime_label.text()) == "GREEN"


def test_engine_hero_shows_model_load_state(main_window):
    from PyQt6.QtWidgets import QLabel
    settings = main_window._settings
    settings._model_hero_status = QLabel()
    settings.set_runtime_status("模型载入中…")
    assert settings._model_hero_status.text() == "模型载入中…"
    settings.set_runtime_status("就绪")
    assert "已载入" in settings._model_hero_status.text()
    settings.set_runtime_status("模型未就绪")
    assert settings._model_hero_status.text() == "模型未就绪"


def test_main_window_edge_hit_test(main_window):
    win = main_window
    w, h = win.width(), win.height()
    assert win._hit_test_edge(2, h // 2) is not None
    assert win._hit_test_edge(w - 2, h - 2) is not None
    assert win._hit_test_edge(w // 2, h // 2) is None


def test_trigger_choice_uses_same_name_as_runtime_mode(main_window):
    cards = main_window._settings.findChildren(CompactPickCard)
    titles = [card._title_label.text() for card in cards]
    assert "持续转写" in titles
    assert "连续口述" not in titles
    assert "持续转写" in main_window._mode_label.text()


def test_task_choices_have_visible_native_radios(main_window):
    cards = main_window._settings.findChildren(CompactPickCard)
    assert len(cards) == 5
    for card in cards:
        assert not card._radio.isHidden()
        assert card._radio.accessibleName()
        assert card.focusProxy() is card._radio


def test_radio_choices_support_arrow_keys(main_window, _qapp_session):
    settings = main_window._settings
    radio = settings._trigger_continuous_rb
    radio.setChecked(True)
    radio.setFocus()
    _qapp_session.processEvents()
    QTest.keyClick(radio, Qt.Key.Key_Right)
    assert settings._trigger_hotkey_rb.isChecked()


def test_polish_disabled_removes_configuration_but_keeps_example(main_window):
    win = main_window._settings
    win._llm_enable_row.setChecked(True)
    assert not win._llm_container.isHidden()
    win._llm_enable_row.setChecked(False)
    assert win._llm_container.isHidden()
    # The before/after example is what helps a user decide to enable it,
    # so it must be visible while the feature is still off.
    assert not win._llm_preview_card.isHidden()


def test_audio_error_remains_inline_with_recovery(main_window, monkeypatch):
    win = main_window._settings
    monkeypatch.setattr("sayink.ui.settings_window.QMessageBox.warning",
                        lambda *args: pytest.fail("Audio feedback should remain in context"))
    win._mic_probe_active = True
    win._on_mic_probe_error("设备不可用")
    assert "设备不可用" in win._mic_test_status.text()
    assert "手动选择" in win._mic_test_status.text()
    assert win._mic_test_btn.isEnabled()


def test_download_is_disabled_immediately_and_error_can_retry(main_window, monkeypatch):
    from unittest.mock import MagicMock
    from sayink.ui.model_card import ModelCard
    win = main_window._settings
    info = {"id": "preview", "name": "示例", "size_mb": 10, "description": "示例",
            "languages": "中文", "accuracy": 3, "speed": 3}
    card = ModelCard(info, False, False)
    worker = MagicMock()
    monkeypatch.setattr("sayink.speech_recognizer.ModelDownloadWorker", lambda _mid, **_kw: worker)
    monkeypatch.setattr("sayink.ui.settings_window.QMessageBox.warning",
                        lambda *args: pytest.fail("Download error should stay on the card"))
    win._model_cards["preview"] = card
    try:
        win._start_download("preview")
        win._start_download("preview")
        worker.start.assert_called_once()
        assert not card._action_btn.isEnabled()
        win._on_dl_error("网络不可用", card)
        assert card._action_btn.isEnabled()
        assert card._action_btn.text() == "重试"
        assert "网络不可用" in card._error_label.text()
        assert not card._error_label.isHidden()
    finally:
        card.close()


def test_history_rows_reflow_to_splitter_width(_qapp_session):
    win = HistoryWindow(FakeHistoryStore())
    try:
        win.resize(780, 580)
        win.show()
        _qapp_session.processEvents()
        win._splitter.setSizes([220, 500])
        _qapp_session.processEvents()
        row = win._session_list.itemWidget(win.session_items()[0])
        assert row.width() <= win._session_list.viewport().width()
    finally:
        win.close()


def test_history_can_load_older_sessions(_qapp_session):
    from dataclasses import replace
    store = FakeHistoryStore()
    original = store.sessions[0]
    store.sessions = [replace(original, session_id=f"session-{i}") for i in range(55)]
    store.segments = {s.session_id: [] for s in store.sessions}
    win = HistoryWindow(store)
    try:
        assert win.session_count() == 50
        assert not win._more_btn.isHidden()
        # The button belongs to the list it extends, not a page footer.
        assert win._more_btn.parentWidget() is win._left_pane
        win._load_more()
        assert win.session_count() == 55
        assert win._more_btn.isHidden()
    finally:
        win.close()


def test_disabled_empty_history_links_to_preference(main_window):
    history = main_window._history
    history.set_history_enabled(False)
    assert not history._history_preferences_btn.isHidden()
    assert "未开启" in history._details.toPlainText()
    history._history_preferences_btn.click()
    assert main_window.current_page() == "general"


def test_refresh_keeps_search_and_current_selection(_qapp_session):
    store = FakeHistoryStore()
    win = HistoryWindow(store)
    try:
        win._session_list.setCurrentItem(win.session_items()[1])
        win.refresh()
        assert win._selected_session_ids() == ["older"]
        win._search_edit.setText("older")
        win._perform_search()
        win.refresh()
        assert store.search_terms[-1] == "older"
        assert win.session_count() == 1
    finally:
        win.close()


def test_repeated_delete_and_refresh_preserve_single_undo_batch(_qapp_session):
    store = FakeHistoryStore()
    win = HistoryWindow(store)
    try:
        win._delete_selected_sessions()
        win.refresh()
        assert win.session_count() == 1
        assert win._selected_session_ids() == ["older"]
        win._delete_selected_sessions()
        win.refresh()
        assert win.session_count() == 0
        assert len(win._pending_delete) == 2
        assert "2 项" in win._feedback_label.text()
        win._undo_pending_delete()
        assert win.session_count() == 2
        assert store.deleted_sessions == []
    finally:
        win.close()


def test_copy_feedback_does_not_swallow_pending_undo(_qapp_session):
    store = FakeHistoryStore()
    win = HistoryWindow(store)
    try:
        win._delete_selected_sessions()
        assert not win._undo_btn.isHidden()
        win._copy_selected_raw()
        assert "已复制" in win._feedback_label.text()
        assert win._undo_btn.isHidden()
        win._feedback_timer.stop()
        win._end_transient_feedback()
        assert "撤销" in win._feedback_label.text()
        assert not win._undo_btn.isHidden()
        assert win._undo_timer.isActive()
    finally:
        win.close()


def test_day_group_headers_split_sessions_by_day(_qapp_session):
    from dataclasses import replace
    store = FakeHistoryStore()
    newer, older = store.sessions
    store.sessions = [newer, replace(older, created_at=older.created_at - 86_400_000 * 3)]
    win = HistoryWindow(store)
    try:
        headers = [
            win._session_list.item(i)
            for i in range(win._session_list.count())
            if not win._session_list.item(i).data(Qt.ItemDataRole.UserRole)
        ]
        assert len(headers) == 2
        assert win.session_count() == 2
        assert all(not (h.flags() & Qt.ItemFlag.ItemIsSelectable) for h in headers)
    finally:
        win.close()


def test_listen_bar_stop_action_matches_session_state(_qapp_session):
    from sayink.ui.floating_window import FloatingWindow
    bar = FloatingWindow()
    try:
        bar.show_model_loading()
        assert bar._end_btn.isHidden()
        bar.show_listening()
        assert not bar._end_btn.isHidden()
        assert "停顿" in bar._status_label.text()
        from sayink.ui.theme import apply_theme
        from sayink.ui import design_tokens as tok
        apply_theme(mode="dark", surfaces=[bar])
        assert tok.STATE_LISTEN in bar._status_label.styleSheet()
        assert bar._dot._color.name().lower() == tok.STATE_LISTEN.lower()
        bar.show_continuous_stopped()
        assert bar._end_btn.isHidden()
        bar.show_recording()
        assert bar._end_btn.isHidden()
    finally:
        bar.dismiss_if_idle()


def test_clear_all_cancels_pending_undo(_qapp_session, monkeypatch):
    from PyQt6.QtWidgets import QDialog
    store = FakeHistoryStore()
    win = HistoryWindow(store)
    # Clearing everything is irreversible, so it keeps a confirmation.
    monkeypatch.setattr(
        "sayink.ui.history_window._ClearHistoryDialog.exec",
        lambda _dialog: QDialog.DialogCode.Accepted,
    )
    try:
        win._delete_selected_sessions()
        win._clear_all_history()
        assert store.deleted_all
        assert win._pending_delete == []
        assert not win._undo_timer.isActive()
        assert win._undo_bar.isHidden()
    finally:
        win.close()

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
    monkeypatch.setattr("sayink.speech_recognizer.is_model_downloaded", lambda _mid: False)
    win, store = _make_main_window(tmp_path, monkeypatch)
    win.show()
    _qapp_session.processEvents()
    yield win
    win.hide()
    win.deleteLater()
    store.close()


def test_keyboard_can_reach_navigation_and_search(main_window, _qapp_session):
    win = main_window
    win._nav_buttons[3].setFocus()
    QTest.keyClick(win._nav_buttons[3], Qt.Key.Key_Space)
    assert win.current_page() == "engine"
    win._focus_history_search()
    _qapp_session.processEvents()
    assert win.current_page() == "history"
    assert win._history._search_edit.hasFocus()


def _tab_stops(win, app, count):
    names = []
    for _ in range(count):
        QTest.keyClick(win, Qt.Key.Key_Tab)
        app.processEvents()
        fw = app.focusWidget()
        names.append(fw.accessibleName() or fw.text())
    return names


def test_tab_walks_the_general_page_top_to_bottom_then_back_to_the_current_nav(main_window, _qapp_session):
    """U-07: Tab enters the sidebar on the current page, then follows the
    visual order; each radio group is one stop that lands on the checked card."""
    win = main_window
    win.resize(1100, 900)
    win.show_page("general")
    win.setFocus()
    _qapp_session.processEvents()

    stops = _tab_stops(win, _qapp_session, 31)
    assert stops == [
        "通用", "录音与输入", "识别", "会议默认", "应用与历史",
        "单次转写", "录音快捷键", "快捷键恢复默认", "仅麦克风", "识别结果去向",
        "测试声音（约 2 秒）", "设备恢复自动选择", "手动选择音频设备",
        "混合音源分别识别（实验性）",
        "识别语言", "自定义词汇", "展开高级识别设置",
        "会议音频来源", "会议识别模型", "开始前显示会议信息卡", "开始卡片恢复默认",
        "主题模式：暗色",
        "开机时自动启动", "录音提示音", "粘贴后恢复剪贴板", "保存转写历史",
        "历史保留天数", "最多保留会话数", "录音恢复（本机缓存）", "查看 / 恢复录音", "通用",
    ]


def test_tab_enters_radio_groups_on_the_checked_option(main_window, _qapp_session):
    win = main_window
    win._settings._trigger_continuous_rb.setChecked(True)
    win._settings._src_mixed_rb.setChecked(True)
    win._settings.reload_settings()
    win.setFocus()
    _qapp_session.processEvents()
    stops = _tab_stops(win, _qapp_session, 10)
    assert stops[:5] == ["通用", "录音与输入", "识别", "会议默认", "应用与历史"]
    assert stops[5] == "持续转写"
    assert stops[6:] == ["录音快捷键", "快捷键恢复默认", "允许 Esc 结束持续转写", "混合音源"]


def test_theme_segment_uses_arrows_and_keeps_only_current_option_tabbable(main_window, _qapp_session):
    theme = main_window._settings._theme_combo
    theme.setCurrentIndex(theme.findData("light"))
    button = theme.focusProxy()
    button.setFocus()
    QTest.keyClick(button, Qt.Key.Key_Right)
    _qapp_session.processEvents()
    assert theme.currentData() == "dark"
    assert main_window._config.get("appearance.theme_mode") == "dark"
    assert theme.focusProxy().hasFocus()
    assert sum(bool(b.focusPolicy() & Qt.FocusPolicy.TabFocus) for b in theme._buttons) == 1


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
    assert "0.18" in win._settings._hotkey_hint.toolTip()
    assert "松开" in win._settings._hotkey_hint.text()
    assert "单次转写" in win._mode_label.text()
    win._settings._trigger_continuous_rb.setChecked(True)
    hint = win._settings._hotkey_hint.text()
    assert "已录内容继续识别" in hint
    assert "Esc" in hint
    assert "松开后继续" in hint
    assert "停顿" in hint
    assert "不用" in hint


def test_sidebar_status_is_not_a_card_and_nav_has_no_stale_focus_ring(main_window):
    win = main_window
    # No nav button is focused just by opening the window.
    assert not any(btn.hasFocus() for btn in win._nav_buttons)
    # Clicking never focuses a nav button; Tab reaches only the current page's button.
    assert not any(btn.focusPolicy() & Qt.FocusPolicy.ClickFocus for btn in win._nav_buttons)
    tabbable = [btn for btn in win._nav_buttons if btn.focusPolicy() & Qt.FocusPolicy.TabFocus]
    assert tabbable == [btn for btn in win._nav_buttons if btn.isChecked()]
    assert not hasattr(win, "_nav_heading")
    assert "border-radius: 8px" not in win._runtime_label.styleSheet()
    win._settings.set_runtime_status("模型载入中…")
    assert win._status_tone() == "AMBER"
    win._settings.set_runtime_status("就绪")
    assert win._status_tone() == "GREEN"
    # The dot follows the typed state, not words in the label: 「模型未就绪」 is
    # red on the engine page and must be red here too.
    from sayink.runtime_status import RuntimeState
    win._settings.set_runtime_status(RuntimeState.UNAVAILABLE, "模型未就绪")
    assert win._status_tone() == "RED"
    assert win._runtime_label.text() == "模型未就绪"


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
    assert "单次转写" in titles
    assert "按住录制" not in titles
    assert titles.count("持续转写") == 1
    # P-01: hold-to-talk is the default; the sidebar names the mode like the card.
    assert "单次转写" in main_window._mode_label.text()
    main_window._settings._trigger_continuous_rb.setChecked(True)
    assert "持续转写" in main_window._mode_label.text()
    # U-09: each clause on its own line so 「停止」 never wraps alone.
    lines = main_window._shortcut_label.text().split("\n")
    assert lines[-1] == "Esc 结束并保留"
    assert all(len(line) <= 12 for line in lines)


def test_task_choices_have_visible_native_radios(main_window):
    cards = main_window._settings.findChildren(CompactPickCard)
    assert len(cards) == 5
    for card in cards:
        assert not card._radio.isHidden()
        assert card._radio.accessibleName()
        assert card.focusProxy() is card._radio


def test_radio_choices_support_arrow_keys(main_window, _qapp_session):
    settings = main_window._settings
    radio = settings._trigger_hotkey_rb
    radio.setChecked(True)
    radio.setFocus()
    _qapp_session.processEvents()
    QTest.keyClick(radio, Qt.Key.Key_Right)
    assert settings._trigger_continuous_rb.isChecked()


def test_polish_disabled_hides_prompt_but_keeps_connection_and_example(main_window):
    win = main_window._settings
    win._llm_enable_row.setChecked(True)
    assert not win._llm_prompt_container.isHidden()
    win._llm_enable_row.setChecked(False)
    assert win._llm_prompt_container.isHidden()
    # Meeting minutes use the same endpoint with polish off.
    assert not win._llm_connection_section.isHidden()
    # The before/after example is what helps a user decide to enable it,
    # so it must be visible while the feature is still off.
    assert not win._llm_preview_card.isHidden()


def test_meeting_configure_button_opens_ai_connection(main_window):
    main_window.show_page("meeting")
    settings = main_window._settings
    settings._llm_url_edit.setText("https://api.deepseek.com/v1")
    settings._llm_key_edit.clear()
    main_window._open_ai_connection()
    assert main_window.current_page() == "polish"
    assert settings._pages.currentIndex() == 2
    assert main_window._nav_buttons[4].isChecked()


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


def test_download_can_be_cancelled_from_card(main_window, monkeypatch):
    from unittest.mock import MagicMock
    from sayink.ui.model_card import ModelCard
    win = main_window._settings
    info = {"id": "preview", "name": "示例", "size_mb": 2400, "description": "示例",
            "languages": "中文", "accuracy": 3, "speed": 3}
    card = ModelCard(info, False, False)
    worker = MagicMock()
    worker._cancelled = False
    worker.cancel.side_effect = lambda: setattr(worker, "_cancelled", True)
    monkeypatch.setattr("sayink.speech_recognizer.ModelDownloadWorker", lambda _mid, **_kw: worker)
    card.action_clicked.connect(win._on_card_action)
    win._model_cards["preview"] = card
    try:
        win._start_download("preview")
        card.set_download_progress(50)
        assert not card._cancel_btn.isHidden()
        assert "1200 / 2400 MB" in card._progress_lbl.text()
        card._cancel_btn.click()
        worker.cancel.assert_called_once()
        win._on_dl_error("下载已取消", card)
        assert card._action_btn.text() == "下载"
        assert card._action_btn.isEnabled()
        assert card._error_label.isHidden()
        assert card._cancel_btn.isHidden()
        assert card._progress_row.isHidden()
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


def test_history_search_results_are_paged_too(_qapp_session):
    """A common word must not render thousands of row widgets in one go."""
    from dataclasses import replace
    store = FakeHistoryStore()
    original = store.sessions[0]
    store.sessions = [replace(original, session_id=f"session-{i}") for i in range(120)]
    store.segments = {s.session_id: [] for s in store.sessions}
    store.search_sessions = lambda q, limit=None: store.sessions[:limit]
    win = HistoryWindow(store)
    try:
        win._search_edit.setText("preview")
        win._perform_search()
        assert win.session_count() == 50
        assert "找到 50+ 条" in win._list_summary_text
        assert not win._more_btn.isHidden()
        assert win._more_btn.text() == "加载更多结果"
        win._load_more()
        assert win.session_count() == 100
        win._load_more()
        assert win.session_count() == 120
        assert "找到 120 条" in win._list_summary_text
        assert win._more_btn.isHidden()
        win._search_edit.clear()
        win._perform_search()
        assert win.session_count() == 50
        assert win._more_btn.text() == "加载更早的会话"
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


def test_meeting_bar_leaves_draining_state_when_meeting_ends(_qapp_session):
    from sayink.ui.floating_window import FloatingWindow
    bar = FloatingWindow()
    try:
        bar.show_meeting("00:27:37")
        bar.show_meeting_paused("正在整理文字稿", title="会议结束中")
        assert bar._status_label.text() == "会议结束中"
        assert not bar._end_btn.isHidden()
        bar.end_meeting_state()
        assert bar._status_label.text() == "会议已结束"
        assert "正在整理" not in bar._text_label.text()
        assert bar._end_btn.isHidden() and bar._mark_btn.isHidden()
        assert bar._hide_timer.isActive()
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

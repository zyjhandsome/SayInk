"""Tests for commit-on-finish behavior of the custom glossary editor."""

from __future__ import annotations

import json
import sys

import pytest
from PyQt6.QtCore import QEvent, QEventLoop, QTimer, Qt
from PyQt6.QtGui import QFocusEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from sayink.config import Config
from sayink.ui.settings_window import SettingsWindow

_INERT_WINDOW_METHODS = (
    "_rebuild_model_cards",
    "_refresh_about_info",
    "_refresh_audio_device_lists",
)


def _spin_event_loop(ms: int) -> None:
    """Run the real Qt event loop for ``ms`` milliseconds so any timers fire."""
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def _reset_config(cfg: Config) -> None:
    cfg._save_timer.stop()
    cfg._config_file.unlink(missing_ok=True)
    cfg._secret_value = None
    cfg._secret_persist_failed = False
    cfg._secret_read_failed = False
    cfg._load()


def _disk_glossary(cfg: Config) -> list:
    path = cfg.config_dir / "config.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("stt", {}).get("glossary", [])


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture(scope="module")
def _shared_settings(qapp, tmp_path_factory):
    with pytest.MonkeyPatch.context() as mp:
        for name in _INERT_WINDOW_METHODS:
            mp.setattr(SettingsWindow, name, lambda self: None)
        if sys.platform == "win32":
            import winreg

            def _no_entry(*_args, **_kwargs):
                raise FileNotFoundError

            mp.setattr(winreg, "QueryValueEx", _no_entry)
        home = tmp_path_factory.mktemp("glossary_commit") / ".sayink"
        home.mkdir(parents=True)
        cfg = Config(config_dir=home)
        win = SettingsWindow(cfg)
    yield win, cfg
    win.close()


@pytest.fixture
def settings_env(_shared_settings, qapp):
    win, cfg = _shared_settings
    win.hide()
    _reset_config(cfg)
    win.show_page(0)
    win.reload_settings(refresh_devices=False, heavy=False)
    qapp.processEvents()
    yield win, cfg
    win.hide()
    qapp.processEvents()


def _activate_window(win, qapp) -> None:
    win.show()
    win.raise_()
    win.activateWindow()
    QTest.qWaitForWindowActive(win, 300)
    qapp.processEvents()


def test_typing_and_pausing_over_600ms_does_not_save_or_emit(settings_env, qapp):
    """Typing, newlines, and pauses >600ms while focused must never commit."""
    win, cfg = settings_env
    fired: list[bool] = []
    cb = lambda: fired.append(True)
    win.recognition_changed.connect(cb)

    _activate_window(win, qapp)
    try:
        edit = win._meeting_glossary_edit
        edit.setFocus(Qt.FocusReason.MouseFocusReason)
        qapp.processEvents()
        assert not hasattr(win, "_meeting_glossary_timer")

        edit.insertPlainText("Say")
        _spin_event_loop(650)
        assert cfg.get("stt.glossary") == []
        assert _disk_glossary(cfg) == []
        assert fired == []

        edit.insertPlainText("Ink\n张鑫=张新")
        _spin_event_loop(650)
        assert cfg.get("stt.glossary") == []
        assert _disk_glossary(cfg) == []
        assert fired == []
        assert not hasattr(win, "_meeting_glossary_timer")
    finally:
        win.recognition_changed.disconnect(cb)


def test_popup_focus_out_does_not_commit_until_real_blur(settings_env, qapp):
    """Right-click context menu / IME popup focus loss must not commit mid-edit."""
    win, cfg = settings_env
    fired: list[bool] = []
    cb = lambda: fired.append(True)
    win.recognition_changed.connect(cb)
    try:
        edit = win._meeting_glossary_edit
        edit.setPlainText("SayInk")
        QApplication.sendEvent(
            edit,
            QFocusEvent(QEvent.Type.FocusOut, Qt.FocusReason.PopupFocusReason),
        )
        _spin_event_loop(650)
        assert cfg.get("stt.glossary") == []
        assert _disk_glossary(cfg) == []
        assert fired == []

        # User pastes another entry from the context menu and continues editing.
        QApplication.sendEvent(
            edit,
            QFocusEvent(QEvent.Type.FocusIn, Qt.FocusReason.PopupFocusReason),
        )
        edit.appendPlainText("五证=五中、无证")
        assert fired == []

        # Leaving the field for real commits everything once.
        QApplication.sendEvent(
            edit,
            QFocusEvent(QEvent.Type.FocusOut, Qt.FocusReason.MouseFocusReason),
        )
        qapp.processEvents()
        expected = [
            {"term": "SayInk"},
            {"term": "五证", "variants": ["五中", "无证"]},
        ]
        assert cfg.get("stt.glossary") == expected
        assert _disk_glossary(cfg) == expected
        assert len(fired) == 1
    finally:
        win.recognition_changed.disconnect(cb)


@pytest.mark.parametrize(
    "reason",
    [
        Qt.FocusReason.OtherFocusReason,
        Qt.FocusReason.TabFocusReason,
        Qt.FocusReason.BacktabFocusReason,
        Qt.FocusReason.ActiveWindowFocusReason,
        Qt.FocusReason.MouseFocusReason,
    ],
)
def test_normal_focus_out_reasons_persist_and_emit_once(settings_env, qapp, reason):
    win, cfg = settings_env
    fired: list[bool] = []
    cb = lambda: fired.append(True)
    win.recognition_changed.connect(cb)
    try:
        edit = win._meeting_glossary_edit
        edit.setPlainText("Fun-ASR\n张鑫=张新")
        QApplication.sendEvent(edit, QFocusEvent(QEvent.Type.FocusOut, reason))
        qapp.processEvents()

        expected = [
            {"term": "Fun-ASR"},
            {"term": "张鑫", "variants": ["张新"]},
        ]
        assert cfg.get("stt.glossary") == expected
        assert _disk_glossary(cfg) == expected
        assert len(fired) == 1
    finally:
        win.recognition_changed.disconnect(cb)


def test_real_widget_focus_transfer_commits_once(qapp, tmp_path, monkeypatch):
    for name in _INERT_WINDOW_METHODS:
        monkeypatch.setattr(SettingsWindow, name, lambda self: None)
    cfg = Config(config_dir=tmp_path / ".sayink")
    win = SettingsWindow(cfg)
    fired: list[bool] = []
    cb = lambda: fired.append(True)
    win.recognition_changed.connect(cb)
    _activate_window(win, qapp)
    try:
        edit = win._meeting_glossary_edit
        edit.setFocus(Qt.FocusReason.MouseFocusReason)
        qapp.processEvents()
        edit.insertPlainText("SayInk")
        assert fired == []

        win._meeting_language_combo.setFocus(Qt.FocusReason.TabFocusReason)
        if not win.isActiveWindow():
            # Windows ForegroundLockTimeout may keep a background test runner's
            # window inactive; deliver the same Tab FocusOut directly in that case.
            QApplication.sendEvent(
                edit,
                QFocusEvent(QEvent.Type.FocusOut, Qt.FocusReason.TabFocusReason),
            )
        qapp.processEvents()

        assert cfg.get("stt.glossary") == [{"term": "SayInk"}]
        assert _disk_glossary(cfg) == [{"term": "SayInk"}]
        assert len(fired) == 1
    finally:
        win.recognition_changed.disconnect(cb)
        win.close()


def test_show_page_commits_pending_glossary_once(settings_env, qapp):
    win, cfg = settings_env
    fired: list[bool] = []
    cb = lambda: fired.append(True)
    win.recognition_changed.connect(cb)
    win.show()
    try:
        edit = win._meeting_glossary_edit
        edit.setFocus(Qt.FocusReason.MouseFocusReason)
        qapp.processEvents()
        edit.insertPlainText("SayInk\nQwen3")
        assert cfg.get("stt.glossary") == []
        assert fired == []

        win.show_page(1)
        qapp.processEvents()

        expected = [{"term": "SayInk"}, {"term": "Qwen3"}]
        assert cfg.get("stt.glossary") == expected
        assert _disk_glossary(cfg) == expected
        assert len(fired) == 1
    finally:
        win.recognition_changed.disconnect(cb)


def test_on_window_hidden_and_hide_event_commit_immediately_once(settings_env, qapp):
    """Closing/hiding right after typing must not drop glossary edits or double-emit."""
    win, cfg = settings_env
    fired: list[bool] = []
    cb = lambda: fired.append(True)
    win.recognition_changed.connect(cb)
    try:
        # 1) Direct on_window_hidden() immediately after typing (0ms wait)
        win._meeting_glossary_edit.setPlainText("SayInk")
        win.on_window_hidden()
        assert cfg.get("stt.glossary") == [{"term": "SayInk"}]
        assert _disk_glossary(cfg) == [{"term": "SayInk"}]
        assert len(fired) == 1

        # 2) MainWindow._hide_window() sequence: hide() -> hideEvent -> on_window_hidden()
        win.show()
        qapp.processEvents()
        win._meeting_glossary_edit.setFocus(Qt.FocusReason.MouseFocusReason)
        qapp.processEvents()
        win._meeting_glossary_edit.setPlainText("SayInk\nFireRedASR2")
        assert len(fired) == 1

        win.hide()
        win.on_window_hidden()
        qapp.processEvents()

        expected = [{"term": "SayInk"}, {"term": "FireRedASR2"}]
        assert cfg.get("stt.glossary") == expected
        assert _disk_glossary(cfg) == expected
        assert len(fired) == 2
    finally:
        win.recognition_changed.disconnect(cb)


def test_close_event_flushes_pending_glossary(qapp, tmp_path, monkeypatch):
    for name in _INERT_WINDOW_METHODS:
        monkeypatch.setattr(SettingsWindow, name, lambda self: None)
    cfg = Config(config_dir=tmp_path / ".sayink")
    win = SettingsWindow(cfg)
    fired: list[bool] = []
    win.recognition_changed.connect(lambda: fired.append(True))

    win._meeting_glossary_edit.setPlainText("SenseVoice")
    win.close()
    qapp.processEvents()

    assert cfg.get("stt.glossary") == [{"term": "SenseVoice"}]
    assert _disk_glossary(cfg) == [{"term": "SenseVoice"}]
    assert len(fired) == 1


def test_unchanged_terms_do_not_re_emit_on_repeated_blur_or_hide(settings_env, qapp):
    win, cfg = settings_env
    fired: list[bool] = []
    cb = lambda: fired.append(True)
    win.recognition_changed.connect(cb)
    try:
        edit = win._meeting_glossary_edit
        edit.setPlainText("SayInk\n张鑫=张新")
        QApplication.sendEvent(
            edit,
            QFocusEvent(QEvent.Type.FocusOut, Qt.FocusReason.OtherFocusReason),
        )
        assert len(fired) == 1

        # Repeated blur, page switch, and window hide without edits -> 0 extra signals.
        QApplication.sendEvent(
            edit,
            QFocusEvent(QEvent.Type.FocusOut, Qt.FocusReason.TabFocusReason),
        )
        win.show_page(2)
        win.show_page(0)
        win.on_window_hidden()
        assert len(fired) == 1

        # Whitespace, blank lines, duplicate terms, or full-width '=' normalize
        # to the exact same parsed glossary -> still 0 extra signals.
        edit.setPlainText("  SayInk  \n\nSayInk\n张鑫 ＝ 张新\n")
        QApplication.sendEvent(
            edit,
            QFocusEvent(QEvent.Type.FocusOut, Qt.FocusReason.ActiveWindowFocusReason),
        )
        win.on_window_hidden()
        assert len(fired) == 1
        assert cfg.get("stt.glossary") == [
            {"term": "SayInk"},
            {"term": "张鑫", "variants": ["张新"]},
        ]
    finally:
        win.recognition_changed.disconnect(cb)


def test_unmodified_hand_edited_glossary_not_rewritten_on_navigation(settings_env):
    """Navigating pages without touching the glossary leaves hand-edited config intact."""
    win, cfg = settings_env
    cfg.set("stt.glossary", ["SayInk", {"term": "GitHub", "variants": []}])
    win.reload_settings(refresh_devices=False, heavy=False)

    fired: list[bool] = []
    cb = lambda: fired.append(True)
    win.recognition_changed.connect(cb)
    try:
        win.show_page(1)
        win.show_page(0)
        win.on_window_hidden()
        assert fired == []
    finally:
        win.recognition_changed.disconnect(cb)

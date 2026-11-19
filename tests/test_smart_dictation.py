"""Contract tests for Track A: Paste Last Transcript, Per-App Polish Presets,
Select-and-Speak Text Rewrite (UIA + terminal safety + remote confirmation),
and HotKeyManager secondary shortcuts.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
from pynput import keyboard
from PyQt6.QtCore import QObject, pyqtSignal

from sayink.app import App, AppModules
from sayink.config import Config
from sayink.hotkey_manager import HotKeyManager
from sayink.output_pipeline import _PendingHistoryRecord
from sayink.text_paster import PasteResult
from sayink.text_polisher import (
    format_app_presets_editor,
    parse_app_presets_editor,
    resolve_app_style_rule,
)
from sayink.ui.floating_window import FloatingWindow
from sayink.ui.settings_window import SettingsWindow
from tests.fakes import FakeWindowInputBackend


def test_paste_last_transcript_re_pastes_last_output_without_duplicate_history(tmp_path: Path):
    cfg = Config(config_dir=tmp_path)
    paster = MagicMock()
    paster.paste_async.side_effect = lambda text, cb, **_kw: cb(
        PasteResult("sent", target_app="WINWORD.EXE")
    )
    history = MagicMock()
    history.disabled = False
    history.list_sessions.return_value = []
    floating = MagicMock()

    app = App.create_headless(
        cfg,
        modules=AppModules(paster=paster, history=history, floating=floating),
    )
    try:
        # Empty before any dictation
        assert app.paste_last_transcript() is False
        floating.show_info.assert_called_with("暂无上一句可重贴", "请先完成一次语音转写")

        # Deliver one normal dictation sentence
        app.output_pipeline.output_text("请明天上午十点同步评审结论。")
        assert paster.paste_async.call_count == 1
        history.reset_mock()

        # Re-paste last transcript into current foreground window -> no duplicate history write!
        assert app.paste_last_transcript() is True
        assert paster.paste_async.call_count == 2
        assert paster.paste_async.call_args.args[0] == "请明天上午十点同步评审结论。"
        history.enqueue.assert_not_called()
    finally:
        app.deleteLater()


def test_explicit_per_app_presets_never_guess_unconfigured_apps_and_show_on_bar(
    qapp, tmp_path: Path
):
    # Unconfigured apps never match automatically
    assert resolve_app_style_rule("WeChat.exe", []) is None
    rules, warn = parse_app_presets_editor(
        "WeChat.exe=聊天口语\nCode.exe=代码终端\nwt.exe=直出原文"
    )
    assert warn == ""
    assert resolve_app_style_rule(r"C:\Program Files\Tencent\WeChat\WeChat.exe", rules) is not None
    assert resolve_app_style_rule("notepad.exe", rules) is None
    assert "WeChat.exe=聊天口语" in format_app_presets_editor(rules)

    cfg = Config(config_dir=tmp_path)
    cfg.set("llm.enabled", True)
    cfg.set("llm.api_url", "http://localhost:11434/v1")
    cfg.set("llm.model_name", "qwen2.5")
    cfg.set("llm.app_presets", rules)

    polisher = MagicMock()
    paster = MagicMock()
    paster.paste_async.side_effect = lambda text, cb, **_kw: cb(PasteResult("sent"))
    bar = FloatingWindow()
    app = App.create_headless(
        cfg,
        modules=AppModules(polisher=polisher, paster=paster, floating=bar, skip_theme_init=True),
    )
    try:
        # 1. Bound app (WeChat.exe -> 聊天口语): shows preset on FloatingWindow & injects style_instruction
        app.output_pipeline.pending_record = _PendingHistoryRecord(
            session_id="s1",
            seq=0,
            raw_text="嗯今晚吃啥",
            polished_text="",
            source="mic",
            duration_ms=1000,
            trigger_mode="hotkey",
            model="funasr-nano",
            target_hwnd=101,
            target_app="WeChat.exe",
        )
        app.output_pipeline.deliver_recognized_text("嗯今晚吃啥")
        assert "润色中 · 聊天口语" in bar.status_text
        assert "聊天口语" in polisher.polish.call_args.kwargs["style_instruction"]
        app.output_pipeline.on_polish_complete("今晚吃什么？")

        # 2. Bound app with 'raw' preset (wt.exe -> 直出原文): bypasses LLM polish completely!
        polisher.reset_mock()
        app.output_pipeline.pending_record = _PendingHistoryRecord(
            session_id="s2",
            seq=0,
            raw_text="git status",
            polished_text="",
            source="mic",
            duration_ms=900,
            trigger_mode="hotkey",
            model="funasr-nano",
            target_hwnd=202,
            target_app="wt.exe",
        )
        app.output_pipeline.deliver_recognized_text("git status")
        polisher.polish.assert_not_called()
        assert paster.paste_async.call_args.args[0] == "git status"

        # 3. Unbound app (WINWORD.EXE): zero unrequested auto-guessing!
        polisher.reset_mock()
        app.output_pipeline.pending_record = _PendingHistoryRecord(
            session_id="s3",
            seq=0,
            raw_text="周报草稿",
            polished_text="",
            source="mic",
            duration_ms=900,
            trigger_mode="hotkey",
            model="funasr-nano",
            target_hwnd=303,
            target_app="WINWORD.EXE",
        )
        app.output_pipeline.deliver_recognized_text("周报草稿")
        assert polisher.polish.call_args.kwargs.get("style_instruction", "") == ""
        assert polisher.polish.call_args.kwargs.get("target_app", "") == ""
        assert bar.status_text == "润色中"
    finally:
        bar.close()
        app.deleteLater()


def test_select_and_speak_rewrite_uia_terminal_safety_and_remote_confirmation(tmp_path: Path):
    cfg = Config(config_dir=tmp_path)
    cfg.set("llm.api_url", "https://api.deepseek.com/v1")
    cfg.set("llm.api_key", "sk-test-secret")
    cfg.set("llm.model_name", "deepseek-chat")

    fake_win = FakeWindowInputBackend(
        foreground=(505, "PowerShell", 4242),
        process_name="powershell.exe",
        selected_text="",
    )
    confirmed_origins: list[str] = []
    allow_remote = False

    def _confirm_fn(origin: str) -> bool:
        confirmed_origins.append(origin)
        return allow_remote

    recorder = MagicMock()
    recorder.is_recording = False
    recorder.is_continuous = False
    recorder.start.side_effect = lambda **_kw: setattr(recorder, "is_recording", True)
    recorder.stop.side_effect = lambda: setattr(recorder, "is_recording", False)

    class _FakeRecognizer(QObject):
        final_result = pyqtSignal(str)
        meeting_result = pyqtSignal(object)
        partial_result = pyqtSignal(str)
        error = pyqtSignal(str)
        ready = pyqtSignal()
        model_load_progress = pyqtSignal(str)
        is_ready = True
        is_loading = False
        is_silent_loading = False
        current_model_id = "funasr-nano"
        running_workers = ()
        running_transcribe_workers = ()
        configure = MagicMock()
        set_boundary_context = MagicMock()

    class _FakePolisher(QObject):
        polish_complete = pyqtSignal(str)
        polish_error = pyqtSignal(str)
        rewrite_complete = pyqtSignal(str)
        rewrite_error = pyqtSignal(str)
        polish = MagicMock()
        rewrite = MagicMock()
        abort = MagicMock()

    recognizer = _FakeRecognizer()
    polisher = _FakePolisher()
    paster = MagicMock()
    paster.paste_async.side_effect = lambda text, cb, **_kw: cb(
        PasteResult("sent", target_app="Code.exe")
    )
    floating = MagicMock()

    app = App.create_headless(
        cfg,
        modules=AppModules(
            recorder=recorder,
            recognizer=recognizer,
            polisher=polisher,
            paster=paster,
            floating=floating,
            window_input=fake_win,
            confirm_rewrite_remote_fn=_confirm_fn,
        ),
    )
    try:
        # 1. Terminal without UIA selection -> refuses cleanly without ever sending Ctrl+C!
        assert app.start_select_and_rewrite() is False
        assert fake_win.sent_chords == []
        assert "终端内不模拟复制键" in floating.show_warning.call_args.args[1]

        # 2. Editor with UIA selection, but user declines first-time remote confirmation -> aborts safely!
        fake_win.process_name = "Code.exe"
        fake_win.selected_text = "需要将这段中文翻译成英文。"
        assert app.start_select_and_rewrite() is True
        app.stop_select_and_rewrite()
        # Simulate ASR returning spoken instruction via real Qt signal
        recognizer.final_result.emit("翻译成英文")
        assert confirmed_origins == ["https://api.deepseek.com:443"]
        polisher.rewrite.assert_not_called()
        paster.paste_async.assert_not_called()

        # 3. User accepts remote origin -> remembers origin, calls polisher.rewrite, and pastes result!
        allow_remote = True
        assert app.start_select_and_rewrite() is True
        app.stop_select_and_rewrite()
        recognizer.final_result.emit("翻译成简洁英文")
        assert cfg.get("llm.rewrite_confirmed_origins") == ["https://api.deepseek.com:443"]
        polisher.rewrite.assert_called_once()
        assert polisher.rewrite.call_args.args[0] == "需要将这段中文翻译成英文。"
        assert polisher.rewrite.call_args.args[1] == "翻译成简洁英文"

        polisher.rewrite_complete.emit("Translate this Chinese paragraph into English.")
        paster.paste_async.assert_called_once()
        assert (
            paster.paste_async.call_args.args[0]
            == "Translate this Chinese paragraph into English."
        )
        assert paster.paste_async.call_args.kwargs["target_hwnd"] == 505

        # 4. Subsequent rewrite to the same remote origin does NOT prompt again!
        confirmed_origins.clear()
        assert app.start_select_and_rewrite() is True
        app.stop_select_and_rewrite()
        recognizer.final_result.emit("改成三点列表")
        assert confirmed_origins == []
    finally:
        app.deleteLater()


def test_hotkey_manager_secondary_shortcuts_and_settings_validation(qapp, tmp_path: Path):
    mgr = HotKeyManager(
        "alt+x",
        paste_last_hotkey="alt+v",
        rewrite_hotkey="alt+r",
    )
    paste_hits: list[int] = []
    rewrite_starts: list[int] = []
    mgr.paste_last_triggered.connect(lambda: paste_hits.append(1))
    mgr.rewrite_start.connect(lambda: rewrite_starts.append(1))
    try:
        mgr._on_press(keyboard.Key.alt_l)
        mgr._on_press(keyboard.KeyCode.from_char("v"))
        mgr._on_release(keyboard.KeyCode.from_char("v"))
        mgr._on_release(keyboard.Key.alt_l)
        assert paste_hits == [1]

        mgr._on_press(keyboard.Key.alt_l)
        mgr._on_press(keyboard.KeyCode.from_char("r"))
        mgr._on_hold_timeout()
        assert rewrite_starts == [1]
        mgr._on_release(keyboard.KeyCode.from_char("r"))
        mgr._on_release(keyboard.Key.alt_l)
    finally:
        mgr.stop()

    # Settings UI prevents colliding optional hotkeys with reserved combos or primary hotkey
    cfg = Config(config_dir=tmp_path)
    settings = SettingsWindow(cfg)
    try:
        settings.general_page.paste_last_hotkey_changed.emit("ctrl+c")
        assert cfg.get("audio.paste_last_hotkey") == ""
        settings.general_page.paste_last_hotkey_changed.emit("alt+x")
        assert cfg.get("audio.paste_last_hotkey") == ""
        settings.general_page.paste_last_hotkey_changed.emit("alt+v")
        assert cfg.get("audio.paste_last_hotkey") == "alt+v"
    finally:
        settings.close()
        _ = np.zeros(1)


def test_win32_window_input_backend_foreground_target_resolves_external_and_filters_own_pid(
    monkeypatch,
):
    import os
    import sayink.text_paster as tp
    from sayink.platform.window_input import Win32WindowInputBackend

    backend = Win32WindowInputBackend()

    monkeypatch.setattr(tp, "get_foreground_window_info", lambda: (4321, "Report.docx", 99999))
    monkeypatch.setattr(tp, "get_foreground_process_name", lambda: "WINWORD.EXE")
    monkeypatch.setattr(tp.TextPaster, "is_own_window", classmethod(lambda cls, info: False))
    assert backend.foreground_target() == (4321, "WINWORD.EXE")

    monkeypatch.setattr(tp, "get_foreground_window_info", lambda: (8888, "SayInk", os.getpid()))
    monkeypatch.setattr(tp, "get_foreground_process_name", lambda: "SayInk.exe")
    monkeypatch.setattr(tp.TextPaster, "is_own_window", classmethod(lambda cls, info: True))
    assert backend.foreground_target() == (0, "")


def test_paste_last_and_rewrite_arm_watchdog_and_report_honest_failure_statuses(tmp_path: Path):
    cfg = Config(config_dir=tmp_path)
    cfg.set("audio.paste_last_hotkey", "alt+v")
    cfg.set("llm.api_url", "http://localhost:11434/v1")
    cfg.set("llm.model_name", "qwen2.5")

    fake_win = FakeWindowInputBackend(
        foreground=(606, "Editor", 5150),
        process_name="Code.exe",
        selected_text="原始选区内容",
    )
    recorder = MagicMock()
    recorder.is_recording = False
    recorder.is_continuous = False
    recorder.start.side_effect = lambda **_kw: setattr(recorder, "is_recording", True)
    recorder.stop.side_effect = lambda: setattr(recorder, "is_recording", False)

    class _FakeRecognizer(QObject):
        final_result = pyqtSignal(str)
        meeting_result = pyqtSignal(object)
        partial_result = pyqtSignal(str)
        error = pyqtSignal(str)
        ready = pyqtSignal()
        model_load_progress = pyqtSignal(str)
        is_ready = True
        is_loading = False
        is_silent_loading = False
        current_model_id = "funasr-nano"
        running_workers = ()
        running_transcribe_workers = ()
        configure = MagicMock()
        set_boundary_context = MagicMock()

    class _FakePolisher(QObject):
        polish_complete = pyqtSignal(str)
        polish_error = pyqtSignal(str)
        rewrite_complete = pyqtSignal(str)
        rewrite_error = pyqtSignal(str)
        polish = MagicMock()
        rewrite = MagicMock()
        cancel = MagicMock()
        cancel_rewrite = MagicMock()

    recognizer = _FakeRecognizer()
    polisher = _FakePolisher()
    paster = MagicMock()
    floating = MagicMock()

    app = App.create_headless(
        cfg,
        modules=AppModules(
            recorder=recorder,
            recognizer=recognizer,
            polisher=polisher,
            paster=paster,
            floating=floating,
            window_input=fake_win,
        ),
    )
    try:
        # 1. Seed previous dictation raw_text + last_output_text, then test paste_last error status
        app.output_pipeline.raw_text = "绝不能在改写超时后误贴的旧口述原文"
        app.output_pipeline.last_output_text = "上一句重要结论"
        paster.paste_async.side_effect = lambda _t, cb, **_kw: cb(
            PasteResult("error", target_app="Code.exe", detail="clipboard locked")
        )
        assert app.paste_last_transcript() is True
        floating.show_error.assert_called()
        assert "无法写入剪贴板" in floating.show_error.call_args.args[0]
        assert app.output_pipeline.last_output_text == "上一句重要结论"

        # 2. Rewrite hangs in POLISH -> watchdog cancels rewrite WITHOUT pasting stale raw_text!
        floating.reset_mock()
        paster.reset_mock()
        paster.paste_async.side_effect = None
        assert app.start_select_and_rewrite() is True
        app.stop_select_and_rewrite()
        recognizer.final_result.emit("精简成一句话")
        assert app.output_pipeline.busy is True
        stuck_token = app.output_pipeline.token
        app.output_pipeline.release_stuck_output(stuck_token)
        assert app.output_pipeline.busy is False
        polisher.cancel_rewrite.assert_called_once()
        paster.paste_async.assert_not_called()
        floating.show_error.assert_called_with("改写超时，原选区未改动")
        # Late rewrite completion after watchdog is ignored
        polisher.rewrite_complete.emit("迟到的改写结果")
        paster.paste_async.assert_not_called()

        # 3. Rewrite paste skipped -> stashes rewritten text in last_output_text & never claims copied
        floating.reset_mock()
        paster.paste_async.side_effect = lambda _t, cb, **_kw: cb(
            PasteResult("skipped", target_app="Code.exe", detail="clipboard_changed")
        )
        assert app.start_select_and_rewrite() is True
        app.stop_select_and_rewrite()
        recognizer.final_result.emit("翻译成英文")
        polisher.rewrite_complete.emit("Concise summary.")
        assert app.output_pipeline.last_output_text == "Concise summary."
        floating.show_warning.assert_called_once()
        assert "Alt + V" in floating.show_warning.call_args.args[1]
        floating.show_info.assert_not_called()

        # 4. Explicit "copied" status reports clipboard copy accurately
        floating.reset_mock()
        paster.paste_async.side_effect = lambda _t, cb, **_kw: cb(
            PasteResult("copied", target_app="Code.exe")
        )
        assert app.start_select_and_rewrite() is True
        app.stop_select_and_rewrite()
        recognizer.final_result.emit("改成正式语气")
        polisher.rewrite_complete.emit("Formal summary.")
        floating.show_info.assert_called_with("改写结果已复制", "焦点已变化或受限，可按 Ctrl+V 粘贴")
    finally:
        app.deleteLater()

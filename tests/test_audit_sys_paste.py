"""Comprehensive regression tests for SayInk system/installer/paste/config/hotkey/onboarding audit fixes.

Covers C-01, C-02, C-03, C-04, C-07, H-03, H-06, H-07, H-08, H-11,
M-03, M-04, M-05, M-06, M-07, M-08, M-17, Sound thread safety/shutdown, L-04, and L-05.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import subprocess
import sys
import threading
import time
import types
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest
from pynput import keyboard
from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtWidgets import QApplication, QDialog

import installer.build_installer as build_installer_mod
import run as run_launcher
import sayink.config as config_mod
import sayink.hotkey_manager as hm_mod
import sayink.main as main_mod
import sayink.onboarding as onboarding_mod
import sayink.platform.windows_identity as wi_mod
import sayink.text_paster as tp_mod
import sayink.updater as updater_mod
from sayink.config import Config, DEFAULT_CONFIG
from sayink.hotkey_manager import HotKeyManager
from sayink.onboarding import OnboardingController
from sayink.secret_store import decode_credential_blob
from sayink.sound_manager import SoundManager
from sayink.text_paster import (
    ClipboardSnapshot,
    MAX_CLIPBOARD_SNAPSHOT_BYTES,
    PasteTargetSnapshot,
    TextPaster,
    detect_session_change,
    detect_terminal_paste_mode,
    is_terminal_window,
    remember_paste_target,
)
from sayink.update_controller import UpdateController
from sayink.updater import (
    ReleaseInfo,
    UpdateDownloadWorker,
    build_update_urlopen,
    cleanup_stale_installers,
    is_newer,
    launch_installer,
    version_key,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 1. C-01, H-06, H-07: SayInk-Setup.iss, build_installer.py, build.py
# ---------------------------------------------------------------------------


class TestInstallerSafetyAndStaging:
    def _iss_text(self) -> str:
        return (REPO_ROOT / "installer" / "SayInk-Setup.iss").read_text(encoding="utf-8")

    def test_uninstall_delete_never_uses_app_wildcard(self):
        text = self._iss_text()
        uninstall_section = text.split("[UninstallDelete]", 1)[1].split("\n[", 1)[0]
        for raw_line in uninstall_section.splitlines():
            line = raw_line.split(";", 1)[0].strip()
            if not line:
                continue
            assert not (
                "filesandordirs" in line.lower()
                and 'name: "{app}"' in line.lower()
            ), f"Unsafe wildcard delete remained in [UninstallDelete]: {line}"
        assert r"{app}\__pycache__" in uninstall_section
        assert "dirifempty" in uninstall_section.lower()

    def test_dir_exists_warning_and_sayink_subfolder_guard(self):
        text = self._iss_text()
        assert "DirExistsWarning=auto" in text
        assert "function NextButtonClick(CurPageID: Integer): Boolean;" in text
        assert "CurPageID = wpSelectDir" in text
        assert "DirectoryHasEntries" in text
        assert r"AddBackslash(SelectedDir) + 'SayInk'" in text

    def test_silent_safe_uninstall_prompt_real_user_profiles_and_cred_cleanup(self):
        text = self._iss_text()
        assert "SuppressibleMsgBox(" in text
        assert "MB_YESNO or MB_DEFBUTTON2" in text
        assert "IDNO" in text
        assert "ProfileImagePath" in text
        assert "CredDeleteW" in text
        assert "SayInk/llm.api_key" in text
        assert "VoiceInk/llm.api_key" in text

    def test_staging_dir_macro_plumbed_end_to_end(self, monkeypatch, tmp_path):
        iss = self._iss_text()
        assert '#define SayInkStagingDir "..\\dist\\SayInk"' in iss
        assert 'Source: "{#SayInkStagingDir}\\SayInk.exe"' in iss
        assert 'AppUserModelID: "SayInk.SayInkApp"' in iss

        marker = tmp_path / "dist" / "SAYINK_STAGING_DIR.txt"
        alt_dir = tmp_path / "dist" / "SayInk-20261113"
        alt_dir.mkdir(parents=True)
        marker.write_text("SayInk-20261113\n", encoding="utf-8")
        monkeypatch.setattr(build_installer_mod, "PROJECT_ROOT", tmp_path)
        assert build_installer_mod.resolve_staging_dir() == alt_dir

        build_src = (REPO_ROOT / "build.py").read_text(encoding="utf-8")
        assert "alt_exe.replace(exe_path)" in build_src

    def test_iscc_compiles_iss_with_custom_staging_dir_if_available(self, tmp_path):
        iscc = build_installer_mod.find_inno_setup()
        if iscc is None:
            pytest.skip("Inno Setup 6 ISCC.exe not installed on host")
        staging = tmp_path / "custom_staging_tree"
        (staging / "_internal").mkdir(parents=True)
        (staging / "models" / "dummy").mkdir(parents=True)
        (staging / "SayInk.exe").write_bytes(b"MZ")
        (staging / "_internal" / "runtime.dll").write_bytes(b"dll")
        (staging / "models" / "dummy" / "model.onnx").write_bytes(b"onnx")
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        cmd = [
            str(iscc),
            "/Q",
            f"/O{out_dir}",
            "/DAppVersionStr=2.2.34",
            "/DAppVersionQuad=2.2.34.0",
            f"/DSayInkStagingDir={staging}",
            str(REPO_ROOT / "installer" / "SayInk-Setup.iss"),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        assert proc.returncode == 0, f"ISCC failed:\n{proc.stdout}\n{proc.stderr}"
        assert (out_dir / "SayInk-Setup-2.2.34.exe").is_file()


# ---------------------------------------------------------------------------
# 2. C-02 & M-08: Updater & UpdateController safe handoff, proxy, semver, etc.
# ---------------------------------------------------------------------------


class TestUpdaterAndHandoff:
    def test_on_downloaded_flushes_stores_and_config_before_launching_installer(self, monkeypatch):
        _qapp = QApplication.instance() or QApplication(sys.argv)
        order: list[str] = []

        parent_app = QObject()
        parent_app._main_window = types.SimpleNamespace(
            flush_pending_user_notes=lambda: order.append("flush_notes"),
            _history_page=types.SimpleNamespace(
                flush_pending_delete=lambda: order.append("flush_deletes"),
            ),
        )
        parent_app._history = types.SimpleNamespace(
            _conn=types.SimpleNamespace(commit=lambda: order.append("sqlite_commit")),
        )

        fake_cfg = MagicMock()
        fake_cfg.get.side_effect = lambda k, d=None: d
        fake_cfg.save_immediate.side_effect = lambda: order.append("config_save") or True

        monkeypatch.setattr(
            updater_mod,
            "launch_installer",
            lambda p: order.append(f"launch:{p}"),
        )
        ctl = UpdateController(
            fake_cfg,
            parent=parent_app,
            status_sink=lambda: None,
            tray=MagicMock(),
            show_about=lambda: None,
            confirm_exit=lambda: order.append("confirm") or True,
            finish_quit=lambda: order.append("quit"),
            exit_started=lambda: False,
        )
        ctl.on_downloaded("C:/Temp/SayInk-Setup.exe")
        assert order == [
            "confirm",
            "flush_notes",
            "flush_deletes",
            "sqlite_commit",
            "config_save",
            "launch:C:/Temp/SayInk-Setup.exe",
            "quit",
        ]

    def test_launch_installer_passes_wait_pid_handshake(self, monkeypatch):
        captured = {}

        def fake_popen(args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return MagicMock()

        monkeypatch.setattr(updater_mod.subprocess, "Popen", fake_popen)
        launch_installer("C:/Temp/SayInk-Setup-2.3.0.exe", wait_pid=43210)
        assert captured["args"] == ["C:/Temp/SayInk-Setup-2.3.0.exe"]
        assert captured["kwargs"]["env"]["SAYINK_UPDATE_WAIT_PID"] == "43210"
        iss = (REPO_ROOT / "installer" / "SayInk-Setup.iss").read_text(encoding="utf-8")
        assert "SAYINK_UPDATE_WAIT_PID" in iss
        assert "WaitForGracefulAppExit" in iss

    def test_prerelease_version_ordering(self):
        ordered = [
            "2.2.99",
            "2.3.0-alpha1",
            "2.3.0-beta1",
            "2.3.0-beta2",
            "2.3.0-rc1",
            "2.3.0-rc2",
            "2.3.0",
            "2.3.1-rc1",
            "2.3.1",
        ]
        for earlier, later in zip(ordered, ordered[1:], strict=False):
            assert version_key(earlier) < version_key(later), f"{earlier} !< {later}"
            assert is_newer(later, earlier)
            assert not is_newer(earlier, later)

    def test_build_update_urlopen_honors_proxy_mode(self, monkeypatch):
        import urllib.request

        calls = []

        class FakeOpener:
            def open(self, req, timeout=None):
                calls.append((req.full_url, timeout))
                return "ok_direct"

        built_handlers = []

        def fake_build_opener(*handlers):
            built_handlers.append(handlers)
            return FakeOpener()

        monkeypatch.setattr(updater_mod, "build_opener", fake_build_opener)
        monkeypatch.setattr(
            urllib.request,
            "urlopen",
            lambda req, timeout=None: "ok_system",
        )

        direct_open = build_update_urlopen("direct")
        req = updater_mod.Request("https://github.com/zyjhandsome/SayInk/releases")
        assert direct_open(req, timeout=5) == "ok_direct"
        assert len(built_handlers[-1]) == 1
        assert built_handlers[-1][0].proxies == {}

        sys_open = build_update_urlopen("system")
        assert sys_open(req, timeout=5) == "ok_system"

    def test_install_forwards_signature_url_and_proxy_mode(self, monkeypatch):
        _qapp = QApplication.instance() or QApplication(sys.argv)
        captured = {}

        class FakeDownloadWorker(QObject):
            progress = pyqtSignal(int, int)
            finished_path = pyqtSignal(str)
            failed = pyqtSignal(str)

            def __init__(self, url, dest, parent=None, **kwargs):
                super().__init__(parent)
                captured["url"] = url
                captured["dest"] = dest
                captured["kwargs"] = kwargs

            def isRunning(self):
                return False

            def start(self):
                captured["started"] = True

        monkeypatch.setattr(updater_mod, "UpdateDownloadWorker", FakeDownloadWorker)
        cfg = MagicMock()
        cfg.get.side_effect = lambda k, d=None: {
            "llm.proxy_mode": "direct",
        }.get(k, d)
        ctl = UpdateController(
            cfg,
            parent=None,
            status_sink=lambda: None,
            tray=MagicMock(),
            show_about=lambda: None,
            confirm_exit=lambda: True,
            finish_quit=lambda: None,
            exit_started=lambda: False,
        )
        rel = ReleaseInfo(
            version="2.4.0",
            asset_name="SayInk-Setup-2.4.0.exe",
            asset_url="https://github.com/zyjhandsome/SayInk/releases/download/v2.4.0/SayInk-Setup-2.4.0.exe",
            size=123456,
            sha256="a" * 64,
            signature_url="https://github.com/zyjhandsome/SayInk/releases/download/v2.4.0/SayInk-Setup-2.4.0.exe.sig",
        )
        ctl.pending_release = rel
        ctl.install()
        assert captured["started"] is True
        assert captured["kwargs"]["signature_url"] == rel.signature_url
        assert captured["kwargs"]["proxy_mode"] == "direct"

    def test_download_progress_throttling_and_stale_cache_cleanup(self, tmp_path):
        _qapp = QApplication.instance() or QApplication(sys.argv)
        total = 10 * 1024 * 1024
        worker = UpdateDownloadWorker(
            "https://github.com/zyjhandsome/SayInk/releases/download/v2.4.0/SayInk-Setup-2.4.0.exe",
            tmp_path / "SayInk-Setup-2.4.0.exe",
            expected_size=total,
            sha256="b" * 64,
        )
        emitted = []
        worker.progress.connect(lambda d, t: emitted.append((d, t)))
        for step in range(1, 9):
            worker._on_progress(step * 64 * 1024, total)
        worker._on_progress(total, total)
        # First chunk and final completion must emit, while intermediate micro-chunks are throttled.
        assert emitted[0] == (64 * 1024, total)
        assert emitted[-1] == (total, total)
        assert len(emitted) < 5

        (tmp_path / "SayInk-Setup-2.2.0.exe").write_bytes(b"old")
        (tmp_path / "SayInk-Setup-2.2.0.exe.sig").write_bytes(b"sig")
        (tmp_path / "SayInk-Setup-2.3.0.exe.part").write_bytes(b"part")
        (tmp_path / "SayInk-Setup-2.4.0.exe").write_bytes(b"keep")
        (tmp_path / "unrelated.txt").write_text("keep", encoding="utf-8")
        removed = cleanup_stale_installers(tmp_path, keep_asset_name="SayInk-Setup-2.4.0.exe")
        assert removed == 3
        assert (tmp_path / "SayInk-Setup-2.4.0.exe").exists()
        assert (tmp_path / "unrelated.txt").exists()
        assert not (tmp_path / "SayInk-Setup-2.2.0.exe").exists()


# ---------------------------------------------------------------------------
# 3. C-03, C-07, M-17: OnboardingController & _SetupDialog
# ---------------------------------------------------------------------------


class _FakeCfg:
    def __init__(self, **initial):
        self.data = dict(initial)

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


class TestOnboardingFixes:
    def _make_controller(self, cfg=None, *, model_installed=True, ready=True, hotkey_mgr=None):
        _qapp = QApplication.instance() or QApplication(sys.argv)
        cfg = cfg or _FakeCfg(first_run_welcome_seen=False)
        decode_lock = threading.Lock()
        speech = types.SimpleNamespace(
            _is_ready=ready,
            is_ready=ready,
            is_loading=False,
            _recognizer=object(),
            _decode_lock=decode_lock,
            running_workers=(),
        )
        parent_app = QObject()
        parent_app._recognizer = speech
        parent_app._hotkey_mgr = hotkey_mgr
        ctl = OnboardingController(
            cfg,
            parent=parent_app,
            dialog_parent=lambda: None,
            is_continuous_mode=lambda: False,
            show_main_window=MagicMock(),
            model_installed=lambda: model_installed,
        )
        ctl._parent_ref = parent_app  # keep alive during test
        return ctl, speech, cfg

    def test_trial_worker_uses_native_decode_lock_running_workers_and_hotkey_pause(self, monkeypatch):
        qapp = QApplication.instance() or QApplication(sys.argv)
        hk = MagicMock()
        hk._paused = False
        ctl, speech, _ = self._make_controller(model_installed=True, ready=True, hotkey_mgr=hk)
        dialog = onboarding_mod._SetupDialog(ctl)
        ctl._setup_dialog = dialog

        created_workers = []

        class FakeTranscribeWorker(QObject):
            result_ready = pyqtSignal(str)
            error = pyqtSignal(str)
            finished = pyqtSignal()

            def __init__(self, recognizer, audio, retry_empty=False, parent=None):
                super().__init__(parent)
                self.recognizer = recognizer
                self.audio = audio
                self.native_lock = None
                self._running = False
                created_workers.append(self)

            def isRunning(self):
                return self._running

            def start(self):
                self._running = True

            def cancel(self):
                self._running = False

            def wait(self, ms=0):
                return True

        monkeypatch.setattr("sayink.speech_recognizer.TranscribeWorker", FakeTranscribeWorker)
        try:
            ctl._pause_global_hotkey()
            hk.pause.assert_called_once()
            ctl._transcribe_setup_trial(np.ones(3200, dtype=np.float32))

            assert len(created_workers) == 1
            worker = created_workers[0]
            assert worker.native_lock is speech._decode_lock
            assert worker in ctl.running_workers

            worker._running = False
            worker.result_ready.emit("测试识别成功")
            worker.finished.emit()
            qapp.processEvents()
            assert list(ctl.running_workers) == []
            hk.resume.assert_called()
            assert "测试识别成功" in dialog.trial_result.text()
        finally:
            dialog.close()

    def test_setup_ready_state_never_deadlocks_without_mic_test_and_chains_welcome(self, monkeypatch):
        _qapp = QApplication.instance() or QApplication(sys.argv)
        cfg = _FakeCfg(first_run_welcome_seen=False)
        cfg.set("history.onboarded", True)
        ctl, speech, _ = self._make_controller(cfg, model_installed=True, ready=False)

        welcomed = []
        monkeypatch.setattr(ctl, "show_welcome", lambda: welcomed.append(True))
        monkeypatch.setattr(onboarding_mod.QTimer, "singleShot", lambda _ms, fn: fn())

        def fake_exec(dialog):
            # Initially model is installed on disk, loading into memory:
            assert "正在载入" in dialog.download_btn.text()
            # Step 1 lists ~950 MB download size & cancel button exists
            assert "950" in dialog.download_step.text()
            assert hasattr(dialog, "cancel_download_btn")
            # Download error writes to download_status, NEVER mic_status!
            orig_mic = dialog.mic_status.text()
            ctl._on_setup_download_error("网络连接中断")
            assert "网络连接中断" in dialog.download_status.text()
            assert dialog.mic_status.text() == orig_mic

            # Now recognizer finishes loading while _mic_heard is still False!
            speech._is_ready = True
            speech.is_ready = True
            ctl._refresh_setup_steps()
            assert dialog.download_btn.text() == "模型已就绪"
            assert dialog.trial_btn.isEnabled() is True
            assert dialog.finish_btn.isEnabled() is True
            assert "Alt" in dialog.usage_hint.text() or "光标" in dialog.usage_hint.text()

            # Dynamic theme refresh must not raise and updates stylesheets
            ctl.refresh_theme()
            dialog.finish_btn.click()
            return QDialog.DialogCode.Accepted

        monkeypatch.setattr(QDialog, "exec", fake_exec)
        ctl.show_setup()
        assert welcomed == [True]

    def test_capture_generation_guard_and_early_finish_trial(self, monkeypatch):
        _qapp = QApplication.instance() or QApplication(sys.argv)
        ctl, _speech, _cfg = self._make_controller(model_installed=True, ready=True)
        fake_rec = MagicMock()
        fake_rec.is_recording = True
        monkeypatch.setattr(ctl, "_configure_setup_recorder", lambda: fake_rec)

        timers = []
        monkeypatch.setattr(onboarding_mod.QTimer, "singleShot", lambda ms, fn: timers.append((ms, fn)))

        dialog = onboarding_mod._SetupDialog(ctl)
        ctl._setup_dialog = dialog
        try:
            ctl._start_setup_trial()
            assert ctl._setup_mode == "trial"
            assert dialog.trial_btn.text() == "提前完成"
            stale_callback = timers[-1][1]

            # User finishes early and starts a fresh mic probe with a newer _capture_gen
            ctl._capture_gen += 1
            ctl._setup_mode = "probe"
            stale_callback()
            # Stale timer from previous generation must NOT stop the new probe!
            assert ctl._setup_mode == "probe"

            # Reopen non-modal sets WA_ShowWithoutActivating so focus isn't stolen
            dialog.hide()
            ctl._reopen_setup_nonmodal()
            assert dialog.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating) is True
        finally:
            ctl._stop_countdown()
            dialog.close()


# ---------------------------------------------------------------------------
# 4. C-04 & H-11: Config defaults + Multi-format ClipboardSnapshot + Paste guards
# ---------------------------------------------------------------------------


class TestClipboardSnapshotAndPasteGuards:
    def test_config_defaults_for_restore_clipboard_and_meeting_save_audio(self):
        assert DEFAULT_CONFIG["output"]["restore_clipboard"] is True
        assert DEFAULT_CONFIG["meeting"]["save_audio"] is True

    def test_clipboard_snapshot_supports_multiformat_and_enforces_32mb_cap(self):
        snap = ClipboardSnapshot(
            win32_entries=[
                (13, "", "hello".encode("utf-16-le") + b"\x00\x00"),
                (8, "", b"DIB_HEADER_BYTES"),
                (15, "", b"CF_HDROP_FILE_LIST"),
                (0xC001, "PNG", b"\x89PNG\r\n\x1a\n"),
                (0xC002, "HTML Format", b"Version:0.9\r\n<html></html>"),
                (0xC003, "Rich Text Format", b"{\\rtf1\\ansi test}"),
            ],
            use_win32=True,
            oversized=False,
        )
        assert bool(snap) is True
        assert len(snap.win32_entries) == 6
        assert MAX_CLIPBOARD_SNAPSHOT_BYTES == 32 * 1024 * 1024

        # Simulate Win32 clipboard has CF_DIB (8) exceeding 32 MB -> oversized skips safely
        big_payload = ctypes.create_string_buffer(16)
        enum_seq = {0: 8, 8: 0}
        fake_u32 = types.SimpleNamespace(
            OpenClipboard=lambda _h: True,
            CloseClipboard=lambda: True,
            EnumClipboardFormats=lambda prev: enum_seq.get(prev, 0),
            GetClipboardFormatNameW=lambda _f, _b, _n: 0,
            GetClipboardData=lambda fmt: 999 if fmt == 8 else None,
        )
        fake_k32 = types.SimpleNamespace(
            GlobalSize=lambda _h: MAX_CLIPBOARD_SNAPSHOT_BYTES + 1024,
            GlobalLock=lambda _h: ctypes.addressof(big_payload),
            GlobalUnlock=lambda _h: True,
        )
        orig_apis = tp_mod._win32_clipboard_apis
        tp_mod._win32_clipboard_apis = lambda: (ctypes, fake_u32, fake_k32)
        try:
            oversize_snap = tp_mod.capture_win32_clipboard_snapshot()
            assert oversize_snap.oversized is True
            assert oversize_snap.win32_entries == []
        finally:
            tp_mod._win32_clipboard_apis = orig_apis

    def test_sequence_number_guard_never_clobbers_newer_user_ctrl_c(self, monkeypatch):
        _qapp = QApplication.instance() or QApplication(sys.argv)
        paster = TextPaster(restore_clipboard=True)
        restored = []
        seq = {"val": 100}

        monkeypatch.setattr(paster, "_use_native_win32_clipboard", lambda: True)
        monkeypatch.setattr(
            paster,
            "_capture_clipboard_snapshot",
            lambda: ClipboardSnapshot(
                win32_entries=[(13, "", b"o\x00l\x00d\x00\x00\x00")],
                use_win32=True,
            ),
        )
        monkeypatch.setattr(paster, "_current_clipboard_seq", lambda: seq["val"])
        monkeypatch.setattr(
            tp_mod,
            "restore_win32_clipboard_snapshot",
            lambda snap: restored.append(snap) or True,
        )
        monkeypatch.setattr(tp_mod.pyperclip, "copy", lambda _t: seq.__setitem__("val", seq["val"] + 1))
        monkeypatch.setattr(tp_mod, "get_foreground_window_info", lambda: (101, "Untitled - Notepad", 99999))
        monkeypatch.setattr(tp_mod, "target_rejects_synthetic_input", lambda _info: False)
        monkeypatch.setattr(tp_mod, "_is_ime_composing", lambda *_a: False)
        monkeypatch.setattr(tp_mod, "_paste_shortcut", lambda *_a, **_kw: None)
        monkeypatch.setattr(tp_mod, "_verify_paste_target", lambda _h: True)

        timers = []
        monkeypatch.setattr(tp_mod.QTimer, "singleShot", lambda ms, fn: timers.append((ms, fn)))

        done = []
        remember_paste_target(PasteTargetSnapshot(hwnd=101, focus_hwnd=0, title_digest=""))
        paster.paste_async("识别结果", done.append, target_hwnd=101, target_app="notepad.exe")
        # Step 1: run _do_paste (PASTE_DELAY_MS)
        timers.pop(0)[1]()
        # Step 2: run _finish (VERIFY_AFTER_PASTE_MS) -> emits sent & schedules restore timer
        timers.pop(0)[1]()
        assert done[-1].status == "sent"
        assert len(timers) == 1
        restore_cb = timers.pop(0)[1]

        # User presses Ctrl+C before 500ms restore fires -> clipboard sequence increments!
        seq["val"] += 1
        restore_cb()
        assert restored == []
        assert paster._restore_pending is False

    def test_cancel_resets_restore_pending_and_skipped_paste_preserves_transcript(self, monkeypatch):
        _qapp = QApplication.instance() or QApplication(sys.argv)
        clipboard = {"text": "用户原本复制的截图说明"}
        monkeypatch.setattr(tp_mod.pyperclip, "paste", lambda: clipboard["text"])
        monkeypatch.setattr(tp_mod.pyperclip, "copy", lambda t: clipboard.__setitem__("text", t))

        timers = []
        monkeypatch.setattr(tp_mod.QTimer, "singleShot", lambda ms, fn: timers.append((ms, fn)))

        paster = TextPaster(restore_clipboard=True)
        paster._restore_pending = True
        paster._saved_clipboard = "stale"
        paster.cancel()
        assert paster._restore_pending is False
        assert paster._saved_clipboard is None

        # Case A: skipped due to elevated window -> transcript MUST stay on clipboard, no restore scheduled!
        monkeypatch.setattr(tp_mod, "get_foreground_window_info", lambda: (500, "Admin CMD", 88888))
        monkeypatch.setattr(tp_mod, "target_rejects_synthetic_input", lambda _info: True)
        results = []
        paster.paste_async("留给手动粘贴的文字", results.append, target_hwnd=500, target_app="cmd.exe")
        while timers:
            timers.pop(0)[1]()
        assert results[-1].status == "clipboard"
        assert results[-1].detail == "elevated"
        assert clipboard["text"] == "留给手动粘贴的文字"
        assert paster._restore_pending is False

        # Case B: skipped due to IME composing -> detail == "ime_active", transcript stays on clipboard!
        clipboard["text"] = "旧文本"
        monkeypatch.setattr(tp_mod, "target_rejects_synthetic_input", lambda _info: False)
        monkeypatch.setattr(tp_mod, "_is_ime_composing", lambda *_a: True)
        remember_paste_target(PasteTargetSnapshot(hwnd=500, focus_hwnd=0, title_digest=""))
        paster.paste_async("拼音组词时不乱贴", results.append, target_hwnd=500, target_app="wechat.exe")
        while timers:
            timers.pop(0)[1]()
        assert results[-1].status == "clipboard"
        assert results[-1].detail == "ime_active"
        assert clipboard["text"] == "拼音组词时不乱贴"
        assert paster._restore_pending is False

        # Case C: skipped due to same-HWND session/chat switch -> detail == "session_changed"!
        clipboard["text"] = "旧文本2"
        monkeypatch.setattr(tp_mod, "_is_ime_composing", lambda *_a: False)
        snap_before = PasteTargetSnapshot(
            hwnd=777,
            focus_hwnd=1001,
            title_digest="aaaa1111bbbb2222",
        )
        remember_paste_target(snap_before)
        monkeypatch.setattr(tp_mod, "get_foreground_window_info", lambda: (777, "微信 - 李四", 88888))
        monkeypatch.setattr(
            tp_mod,
            "build_paste_target_snapshot",
            lambda _info: PasteTargetSnapshot(
                hwnd=777,
                focus_hwnd=1001,
                title_digest="cccc3333dddd4444",  # User switched chat contact in same WeChat window!
            ),
        )
        paster.paste_async("切走聊天框不误发", results.append, target_hwnd=777, target_app="weixin.exe")
        while timers:
            timers.pop(0)[1]()
        assert results[-1].status == "clipboard"
        assert results[-1].detail == "session_changed"
        assert clipboard["text"] == "切走聊天框不误发"
        assert paster._restore_pending is False

    def test_detect_session_change_and_terminal_paste_modes(self, monkeypatch):
        before = PasteTargetSnapshot(hwnd=10, focus_hwnd=20, title_digest="abcd")
        same = PasteTargetSnapshot(hwnd=10, focus_hwnd=20, title_digest="abcd")
        diff_focus = PasteTargetSnapshot(hwnd=10, focus_hwnd=21, title_digest="abcd")
        diff_title = PasteTargetSnapshot(hwnd=10, focus_hwnd=20, title_digest="ef01")
        assert detect_session_change(before, same) is False
        assert detect_session_change(before, diff_focus) is True
        assert detect_session_change(before, diff_title) is True

        monkeypatch.setattr(tp_mod, "_get_window_class_name", lambda _h: "CASCADIA_HOSTING_WINDOW_CLASS")
        assert detect_terminal_paste_mode(1, "WindowsTerminal.exe") == "ctrl_shift_v"
        assert is_terminal_window(1, "WindowsTerminal.exe") is True

        monkeypatch.setattr(tp_mod, "_get_window_class_name", lambda _h: "mintty")
        assert detect_terminal_paste_mode(2, "mintty.exe") == "shift_insert"

        monkeypatch.setattr(tp_mod, "_get_window_class_name", lambda _h: "ConsoleWindowClass")
        assert detect_terminal_paste_mode(3, "cmd.exe") == "ctrl_v"
        assert is_terminal_window(3, "cmd.exe") is True

    @pytest.mark.skipif(sys.platform != "win32", reason="Win32 keybd_event test")
    def test_win32_shortcut_does_not_release_physically_held_ctrl(self, monkeypatch):
        events = []
        monkeypatch.setattr(tp_mod, "_held_stray_modifiers", lambda _u32: [])
        monkeypatch.setattr(tp_mod, "_is_ctrl_physically_held", lambda _u32: True)

        fake_user32 = types.SimpleNamespace(
            MapVirtualKeyW=lambda _vk, _m: 0,
            GetAsyncKeyState=lambda _vk: 0x8000,
            keybd_event=lambda vk, _scan, flags, _extra: events.append((int(vk), int(flags))),
        )
        monkeypatch.setattr(
            ctypes,
            "windll",
            types.SimpleNamespace(user32=fake_user32),
            raising=False,
        )
        tp_mod._paste_shortcut_win32("ctrl_v")
        # User is already holding Ctrl physically: only 'V' down + 'V' up should be sent, never VK_CONTROL KEYUP!
        assert events == [(0x56, 0), (0x56, 0x0002)]


# ---------------------------------------------------------------------------
# 5. H-03, M-05, M-06, M-07: Config & SecretStore safety
# ---------------------------------------------------------------------------


class TestConfigAndSecretStoreSafety:
    def test_config_preserves_plaintext_api_key_when_store_none_or_write_fails(self, monkeypatch, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps({"llm": {"api_key": "sk-keep-me"}}, ensure_ascii=False),
            encoding="utf-8",
        )
        monkeypatch.setattr(config_mod, "user_data_dir", lambda: tmp_path)

        # 1) secret_store is None -> file must STILL retain sk-keep-me on save!
        monkeypatch.setattr(config_mod, "default_secret_store", lambda: None)
        cfg = Config()
        assert cfg.get("llm.api_key") == "sk-keep-me"
        assert cfg.save_immediate() is True
        saved_disk = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert saved_disk["llm"]["api_key"] == "sk-keep-me"

        # 2) secret_store exists, read returns "", write returns False -> STILL retain on disk!
        failing_store = types.SimpleNamespace(
            read=lambda: "",
            write=lambda _v: False,
        )
        cfg2 = Config(secret_store=failing_store)
        assert cfg2.get("llm.api_key") == "sk-keep-me"
        assert cfg2.secret_persist_failed is True
        cfg2.set("llm.model_name", "deepseek-chat")
        cfg2.save_immediate()
        saved_disk2 = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert saved_disk2["llm"]["api_key"] == "sk-keep-me"

        # 3) secret_store write succeeds -> now plaintext key is scrubbed from config.json!
        vault = {}
        working_store = types.SimpleNamespace(
            read=lambda: vault.get("k", ""),
            write=lambda v: vault.__setitem__("k", v) or True,
        )
        Config(secret_store=working_store)
        assert vault["k"] == "sk-keep-me"
        saved_disk3 = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert saved_disk3["llm"]["api_key"] == ""

    def test_os_replace_permission_retry_corrupt_recovery_deepcopy_and_thread_set(
        self, monkeypatch, tmp_path
    ):
        _qapp = QApplication.instance() or QApplication(sys.argv)
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text("{corrupt json!!!", encoding="utf-8")
        monkeypatch.setattr(config_mod, "user_data_dir", lambda: tmp_path)

        cfg = Config(secret_store=types.SimpleNamespace(read=lambda: "", write=lambda _v: True))
        assert cfg.corrupt_recovered is True

        # Deep copy check on get_all()
        snapshot = cfg.get_all()
        snapshot["llm"]["model_name"] = "MUTATED_OUTSIDE"
        assert cfg.get("llm.model_name") != "MUTATED_OUTSIDE"

        # Transient PermissionError on first two os.replace calls -> succeeds on 3rd attempt!
        real_replace = config_mod.os.replace
        attempts = {"n": 0}

        def flaky_replace(src, dst):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise PermissionError("antivirus lock")
            return real_replace(src, dst)

        monkeypatch.setattr(config_mod.os, "replace", flaky_replace)
        monkeypatch.setattr(config_mod.time, "sleep", lambda _s: None)
        assert cfg.save_immediate() is True
        assert attempts["n"] == 3
        assert cfg.last_save_failed is False

        # Persistent PermissionError -> returns False and sets last_save_failed = True
        monkeypatch.setattr(
            config_mod.os,
            "replace",
            lambda _s, _d: (_ for _ in ()).throw(PermissionError("locked forever")),
        )
        assert cfg.save_immediate() is False
        assert cfg.last_save_failed is True

        # Restore os.replace and test Config.set() from a non-Qt background thread
        monkeypatch.setattr(config_mod.os, "replace", real_replace)
        worker_err = []

        def bg_setter():
            try:
                cfg.set("output.restore_clipboard", False)
            except Exception as exc:
                worker_err.append(exc)

        t = threading.Thread(target=bg_setter)
        t.start()
        t.join(timeout=2.0)
        assert worker_err == []
        assert cfg.save_immediate() is True
        assert cfg.last_save_failed is False

    def test_auto_start_registry_repairs_stale_exe_and_honors_startup_approved(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(config_mod, "user_data_dir", lambda: tmp_path)
        monkeypatch.setattr(config_mod.sys, "platform", "win32")

        run_key = r"Software\Microsoft\Windows\CurrentVersion\Run"
        reg_values = {
            (run_key, "SayInk"): (r'"D:\OldFolder\SayInk.exe"', 1),
            # Odd first byte (0x03) means disabled by user in Windows Task Manager!
            (config_mod.STARTUP_APPROVED_RUN_KEY, "SayInk"): (b"\x03" + b"\x00" * 11, 3),
        }
        written = []

        class FakeKey:
            def __init__(self, path):
                self.path = path

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        fake_winreg = types.SimpleNamespace(
            HKEY_CURRENT_USER=1,
            KEY_READ=1,
            KEY_SET_VALUE=2,
            REG_SZ=1,
            OpenKey=lambda _root, subkey, _res=0, _acc=1: FakeKey(subkey),
            CloseKey=lambda _k: None,
            QueryValueEx=lambda key, name: reg_values[(key.path, name)]
            if (key.path, name) in reg_values
            else (_ for _ in ()).throw(FileNotFoundError(name)),
            SetValueEx=lambda key, name, _r, _t, val: written.append((key.path, name, val)),
            DeleteValue=lambda key, name: (_ for _ in ()).throw(FileNotFoundError(name)),
        )
        monkeypatch.setitem(sys.modules, "winreg", fake_winreg)
        monkeypatch.setattr("sayink.app.auto_start_command", lambda: r'"C:\NewFolder\SayInk.exe"')
        (tmp_path / "config.json").write_text(json.dumps({"auto_start": True}), encoding="utf-8")

        cfg = Config(secret_store=types.SimpleNamespace(read=lambda: "", write=lambda _v: True))
        # Disabled in Task Manager -> must NOT re-enable, and config aligns to False!
        assert cfg.get("auto_start") is False
        assert written == []

        # Now enabled in Task Manager (0x02) with auto_start=True -> stale path gets repaired!
        reg_values[(config_mod.STARTUP_APPROVED_RUN_KEY, "SayInk")] = (b"\x02" + b"\x00" * 11, 3)
        (tmp_path / "config.json").write_text(json.dumps({"auto_start": True}), encoding="utf-8")
        cfg2 = Config(secret_store=types.SimpleNamespace(read=lambda: "", write=lambda _v: True))
        assert cfg2.get("auto_start") is True
        assert written == [(run_key, "SayInk", r'"C:\NewFolder\SayInk.exe"')]

    def test_decode_credential_blob_handles_utf16le_ascii_utf8_and_bom(self):
        key = "sk-proj-AbCdEf1234567890_XYZ"
        # Standard UTF-16LE written by SayInk
        assert decode_credential_blob(key.encode("utf-16-le")) == key
        # Raw ASCII written by external CLI / cmdkey / PowerShell (even byte count!)
        assert len(key) % 2 == 0
        assert decode_credential_blob(key.encode("ascii")) == key
        # Null-terminated ASCII C-string
        assert decode_credential_blob(key.encode("ascii") + b"\x00") == key
        # UTF-8 with BOM
        assert decode_credential_blob(b"\xef\xbb\xbf" + key.encode("utf-8")) == key
        # UTF-16LE with BOM + trailing NUL
        assert decode_credential_blob(b"\xff\xfe" + key.encode("utf-16-le") + b"\x00\x00") == key


# ---------------------------------------------------------------------------
# 6. H-08 & M-04: HotKeyManager self-healing, UIPI hold poll, F8 & ASCII VK
# ---------------------------------------------------------------------------


class TestHotkeyResilienceAndLayout:
    def test_update_hotkey_starts_when_listener_is_none_unless_explicitly_stopped(self, monkeypatch):
        qapp = QApplication.instance() or QApplication(sys.argv)
        mgr = HotKeyManager("alt+x", parent=qapp)
        started = []
        monkeypatch.setattr(mgr, "start", lambda: started.append(True))

        assert mgr._listener is None
        mgr.update_hotkey("ctrl+space")
        assert started == [True]

        mgr._stopped = True
        mgr.update_hotkey("alt+z")
        assert started == [True]  # Explicitly stopped manager stays stopped

    def test_health_tick_restarts_dead_listener_and_sleep_wake_jump(self, monkeypatch):
        qapp = QApplication.instance() or QApplication(sys.argv)
        mgr = HotKeyManager("alt+x", parent=qapp)
        restarts = []
        monkeypatch.setattr(mgr, "_restart_listener", lambda: restarts.append(True))

        # Dead listener thread triggers restart
        mgr._listener = types.SimpleNamespace(is_alive=lambda: False)
        mgr._last_health_wall = time.time()
        mgr._on_health_tick()
        assert len(restarts) == 1

        # Alive listener after 30s sleep/wake wall-clock jump triggers hook refresh
        mgr._listener = types.SimpleNamespace(is_alive=lambda: True)
        mgr._last_health_wall = time.time() - 30.0
        mgr._on_health_tick()
        assert len(restarts) == 2

    def test_uipi_async_poll_stops_recording_when_key_released_in_elevated_window(self, monkeypatch):
        qapp = QApplication.instance() or QApplication(sys.argv)
        mgr = HotKeyManager("alt+x", parent=qapp)
        stopped = []
        mgr.recording_stop.connect(lambda: stopped.append(True))

        # Simulate active hold-to-talk recording where Alt+X was pressed, then user clicked an admin window
        # and physically released Alt (so WM_KEYUP was swallowed by UIPI, but GetAsyncKeyState sees Alt is up).
        mgr._verify_async_state = True
        mgr._pressed_keys = {keyboard.Key.alt_l, keyboard.KeyCode.from_char("x")}
        mgr._is_recording = True
        monkeypatch.setattr(mgr, "_async_key_down", lambda _vk: False)

        mgr._poll_recording_keys_held()
        assert mgr._is_recording is False
        assert stopped == [True]

    def test_single_function_key_suppression_and_layout_independent_ascii_vk(self, monkeypatch):
        # Single function key F8 must populate _main_keys_by_vk even with zero modifiers!
        qapp = QApplication.instance() or QApplication(sys.argv)
        mgr_f8 = HotKeyManager("f8", parent=qapp)
        assert mgr_f8._main_keys_by_vk == {0x77: keyboard.Key.f8}
        assert mgr_f8._hotkey_modifiers == ()

        # Non-Latin keyboard layout where VkKeyScanW returns -1 must STILL resolve 'x' -> 0x58 and '1' -> 0x31!
        fake_u32 = types.SimpleNamespace(VkKeyScanW=lambda _ch: -1)
        monkeypatch.setattr(
            ctypes,
            "windll",
            types.SimpleNamespace(user32=fake_u32),
            raising=False,
        )
        assert hm_mod._main_key_vk(keyboard.KeyCode.from_char("x")) == 0x58
        assert hm_mod._main_key_vk(keyboard.KeyCode.from_char("1")) == 0x31

        # KeyCode emitted with non-Latin char but VK 0x58 normalizes to 'x'
        mgr_alt_x = HotKeyManager("alt+x", parent=qapp)
        cyrillic_x = keyboard.KeyCode(vk=0x58, char="ч")
        assert mgr_alt_x._normalize_key(cyrillic_x) == keyboard.KeyCode.from_char("x")


# ---------------------------------------------------------------------------
# 7. M-03, Sound, L-04, L-05: main.py, sound_manager.py, windows_identity.py, dev scripts
# ---------------------------------------------------------------------------


class TestSystemIdentitySoundAndDevTooling:
    def test_main_64bit_mutex_signatures_activation_retry_and_user_notice(self, monkeypatch):
        class FakeFunc:
            def __init__(self):
                self.argtypes = None
                self.restype = None

        fake_dll = types.SimpleNamespace(
            CreateMutexW=FakeFunc(),
            OpenMutexW=FakeFunc(),
            CloseHandle=FakeFunc(),
        )
        main_mod._configure_kernel32_signatures(fake_dll)
        assert fake_dll.CreateMutexW.restype == wintypes.HANDLE
        assert fake_dll.OpenMutexW.restype == wintypes.HANDLE
        assert fake_dll.CloseHandle.argtypes == [wintypes.HANDLE]

        # Cold-start activation retry succeeds on 3rd attempt
        attempts = {"count": 0}

        class FakeSocket:
            def connectToServer(self, _name):
                attempts["count"] += 1

            def waitForConnected(self, _ms):
                return attempts["count"] >= 3

            def write(self, _data):
                return 5

            def flush(self):
                return True

            def waitForBytesWritten(self, _ms):
                return True

            def disconnectFromServer(self):
                pass

        monkeypatch.setattr("PyQt6.QtNetwork.QLocalSocket", FakeSocket)
        assert main_mod._activate_running_instance(retries=4, retry_delay_s=0.001) is True
        assert attempts["count"] == 3

        # Legacy VoiceInk block skips pipe activation and notifies user clearly
        notices = []
        monkeypatch.setattr(main_mod, "_last_block_reason", "legacy_voiceink")
        assert main_mod._handle_existing_instance(retries=2, retry_delay_s=0.0, message_box=notices.append) is False
        assert len(notices) == 1 and "VoiceInk" in notices[0]

    def test_sound_manager_serializes_playback_and_supports_clean_shutdown(self, monkeypatch):
        active = {"cur": 0, "max": 0}
        guard = threading.Lock()
        done = threading.Event()
        calls = {"n": 0}

        def fake_play(_audio, _sr, blocking=True):
            with guard:
                active["cur"] += 1
                active["max"] = max(active["max"], active["cur"])
            time.sleep(0.03)
            with guard:
                active["cur"] -= 1
                calls["n"] += 1
                if calls["n"] >= 2:
                    done.set()

        monkeypatch.setitem(sys.modules, "sounddevice", types.SimpleNamespace(play=fake_play))
        mgr = SoundManager(enabled=True)
        mgr.play_start()
        mgr.play_stop()
        assert done.wait(timeout=2.0)
        assert active["max"] == 1  # Never concurrent inside PortAudio!

        mgr.shutdown(wait=True)
        # Re-instantiating SoundManager after shutdown lazily recreates a live executor
        mgr2 = SoundManager(enabled=True)
        assert mgr2._executor is not None
        assert SoundManager._executor_shutdown is False

    def test_windows_identity_shortcut_avoids_duplicates_clobbering_and_cold_rewrites(
        self, monkeypatch, tmp_path
    ):
        appdata = tmp_path / "AppData" / "Roaming"
        progdata = tmp_path / "ProgramData"
        user_programs = appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        all_group_lnk = progdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "SayInk" / "SayInk.lnk"
        all_group_lnk.parent.mkdir(parents=True)
        all_group_lnk.write_bytes(b"installer-lnk")

        monkeypatch.setenv("APPDATA", str(appdata))
        monkeypatch.setenv("PROGRAMDATA", str(progdata))

        saves = []
        commits = []

        class FakeShortcut:
            def __init__(self, path):
                self.path = path
                self.Targetpath = ""
                self.Arguments = ""
                self.WorkingDirectory = ""
                self.Description = ""
                self.IconLocation = ""

            def save(self):
                saves.append(self.path)
                Path(self.path).write_bytes(b"saved-lnk")

        shortcuts = {}

        class FakeShell:
            def CreateShortCut(self, path):
                return shortcuts.setdefault(path, FakeShortcut(path))

        class FakeStore:
            def __init__(self, app_id="SayInk.SayInkApp"):
                self.app_id = app_id

            def GetValue(self, _key):
                return types.SimpleNamespace(GetValue=lambda: self.app_id)

            def SetValue(self, _key, val):
                self.app_id = val

            def Commit(self):
                commits.append(True)

        store_obj = FakeStore()
        fake_win32com_client = types.SimpleNamespace(Dispatch=lambda _name: FakeShell())
        fake_propsys_mod = types.SimpleNamespace(
            GPS_DEFAULT=0,
            GPS_READWRITE=2,
            IID_IPropertyStore=object(),
            SHGetPropertyStoreFromParsingName=lambda *_a, **_kw: store_obj,
            PROPVARIANTType=lambda val, _vt: val,
        )
        fake_pscon_mod = types.SimpleNamespace(PKEY_AppUserModel_ID="pkey")
        monkeypatch.setitem(sys.modules, "pythoncom", types.SimpleNamespace(VT_LPWSTR=31))
        monkeypatch.setitem(sys.modules, "win32com", types.SimpleNamespace(client=fake_win32com_client))
        monkeypatch.setitem(sys.modules, "win32com.client", fake_win32com_client)
        monkeypatch.setitem(
            sys.modules,
            "win32com.propsys",
            types.SimpleNamespace(propsys=fake_propsys_mod, pscon=fake_pscon_mod),
        )

        # 1) Source mode when Setup's {group}\SayInk.lnk exists -> skips writing any duplicate shortcut!
        monkeypatch.setattr(wi_mod.sys, "frozen", False, raising=False)
        wi_mod._ensure_start_menu_shortcut()
        assert saves == []
        assert not (user_programs / "SayInk.lnk").exists()

        # 2) Frozen mode when Setup's {group}\SayInk.lnk already has AppUserModelID -> removes duplicate & zero writes!
        dup_top_lnk = user_programs / "SayInk.lnk"
        dup_top_lnk.parent.mkdir(parents=True, exist_ok=True)
        dup_top_lnk.write_bytes(b"legacy-dup")
        monkeypatch.setattr(wi_mod.sys, "frozen", True, raising=False)
        monkeypatch.setattr(wi_mod.sys, "executable", r"C:\Program Files\SayInk\SayInk.exe")
        wi_mod._ensure_start_menu_shortcut()
        assert not dup_top_lnk.exists()
        assert saves == []
        assert commits == []

        # 3) Portable frozen mode (no {group}\SayInk.lnk): first run writes once, second run is a no-op!
        all_group_lnk.unlink()
        wi_mod._ensure_start_menu_shortcut()
        assert len(saves) == 1
        assert len(commits) == 1
        wi_mod._ensure_start_menu_shortcut()
        assert len(saves) == 1  # Idempotent on cold start!
        assert len(commits) == 1

    def test_constraints_run_dev_run_py_and_retired_meeting_keys(self, monkeypatch, tmp_path):
        constraints_text = (REPO_ROOT / "constraints-windows-py310.txt").read_text(encoding="utf-8")
        for required_pin in (
            "onnxruntime==1.23.2",
            "coloredlogs==",
            "flatbuffers==",
            "protobuf==",
            "sympy==",
        ):
            assert required_pin in constraints_text

        run_dev = (REPO_ROOT / "run-dev.bat").read_text(encoding="utf-8")
        assert 'start "" "dist\\SayInk\\SayInk.exe"' not in run_dev
        assert "run.py" in run_dev

        # run.py _report_startup_error works safely when stderr is None
        monkeypatch.setattr(run_launcher.sys, "stderr", None)
        monkeypatch.setattr(run_launcher.sys, "stdout", None)
        run_launcher._report_startup_error("模拟缺少 PyQt6")

        # Retired dead meeting keys are absent from DEFAULT_CONFIG and stripped on load
        assert "hotkey" not in DEFAULT_CONFIG["meeting"]
        assert "separate_lanes" not in DEFAULT_CONFIG["meeting"]
        monkeypatch.setattr(config_mod, "user_data_dir", lambda: tmp_path)
        (tmp_path / "config.json").write_text(
            json.dumps({"meeting": {"hotkey": "ctrl+m", "separate_lanes": True, "save_audio": False}}),
            encoding="utf-8",
        )
        cfg = Config(secret_store=types.SimpleNamespace(read=lambda: "", write=lambda _v: True))
        meeting_cfg = cfg.get("meeting")
        assert "hotkey" not in meeting_cfg
        assert "separate_lanes" not in meeting_cfg
        assert meeting_cfg["save_audio"] is False

"""Regression tests for runtime lifecycle, port-aware remote origins, build PATH, and CI gates."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

from PyQt6.QtCore import QThread
from PyQt6.QtWidgets import QDialog

import build
import sayink.app as app_mod
from sayink.config import Config
from sayink.meeting_session import (
    _confirmed_remote_origins,
    _format_remote_origin,
    confirm_remote_host,
    remote_host_needing_confirmation,
)
from tests.helpers.app_harness import app_harness

REPO_ROOT = Path(__file__).resolve().parents[1]


class _BlockedWorker(QThread):
    def __init__(self, gate: threading.Event):
        super().__init__()
        self._gate = gate

    def run(self) -> None:
        self._gate.wait(5.0)


class TestPortAwareRemoteOriginConfirmation:
    """UI-08 + FC-06 (QA-12): Web Origin ``(scheme, host, effective_port)`` scoping."""

    def test_format_remote_origin_omits_default_ports_and_keeps_non_default_ports(self):
        assert _format_remote_origin("https", "api.example.com", 443) == "https://api.example.com"
        assert _format_remote_origin("https", "api.example.com") == "https://api.example.com"
        assert _format_remote_origin("http", "api.example.com", 80) == "http://api.example.com"
        assert _format_remote_origin("https", "gateway.example.com", 8443) == "https://gateway.example.com:8443"
        assert _format_remote_origin("http", "gateway.example.com", 8080) == "http://gateway.example.com:8080"
        assert _format_remote_origin("https", "api.example.com", 80) == "https://api.example.com:80"
        assert _format_remote_origin("http", "api.example.com", 443) == "http://api.example.com:443"
        assert _format_remote_origin("https", "2001:db8::1", 443) == "https://[2001:db8::1]"
        assert _format_remote_origin("https", "2001:db8::1", 8443) == "https://[2001:db8::1]:8443"

    def test_confirmed_origins_parse_legacy_bare_default_and_non_default_ports(self):
        origins = _confirmed_remote_origins([
            "api.example.com",
            "https://gateway.example.com:8443/v1",
            "http://intranet.example.internal:8080",
            "https://[2001:db8::2]:9443/v1",
        ])
        assert origins == {
            ("https", "api.example.com", 443),
            ("https", "gateway.example.com", 8443),
            ("http", "intranet.example.internal", 8080),
            ("https", "2001:db8::2", 9443),
        }

    def test_port_switch_on_same_host_requires_fresh_confirmation(self, tmp_path):
        cfg = Config(config_dir=tmp_path)
        cfg.set("llm.api_url", "https://example.invalid:9443/v1")
        assert remote_host_needing_confirmation(cfg) == "https://example.invalid:9443"
        confirm_remote_host(cfg, "https://example.invalid:9443/v1")
        assert remote_host_needing_confirmation(cfg) == ""
        assert cfg.get("meeting.confirmed_remote_hosts") == ["https://example.invalid:9443"]

        # Changing :9443 -> :8443 on the same host must NOT inherit authorization.
        cfg.set("llm.api_url", "https://example.invalid:8443/v1")
        assert remote_host_needing_confirmation(cfg) == "https://example.invalid:8443"

        # Bare hostname confirmation picks up the configured endpoint's non-default port.
        confirm_remote_host(cfg, "example.invalid")
        assert remote_host_needing_confirmation(cfg) == ""
        assert cfg.get("meeting.confirmed_remote_hosts") == [
            "https://example.invalid:9443",
            "https://example.invalid:8443",
        ]

    def test_ipv6_non_default_port_confirmation_round_trips(self, tmp_path):
        cfg = Config(config_dir=tmp_path)
        cfg.set("llm.api_url", "https://[2606:4700:4700::1111]:8443/v1")
        assert remote_host_needing_confirmation(cfg) == "https://[2606:4700:4700::1111]:8443"
        confirm_remote_host(cfg, "https://[2606:4700:4700::1111]:8443")
        assert remote_host_needing_confirmation(cfg) == ""
        cfg.set("llm.api_url", "https://[2606:4700:4700::1111]:9443/v1")
        assert remote_host_needing_confirmation(cfg) == "https://[2606:4700:4700::1111]:9443"

    def test_readme_documents_port_scoped_remote_confirmation(self):
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        assert "有效端口" in readme or "切换端口" in readme


class TestFinishQuitMeetingTempCleanup:
    """FC-04 part 2: ``App._finish_quit()`` cleans meeting temp playback WAVs."""

    def test_finish_quit_calls_meeting_panel_cleanup(self):
        with app_harness() as h:
            app = h["app"]
            events: list[str] = []
            panel = MagicMock()
            panel.cleanup.side_effect = lambda: events.append("panel.cleanup")
            main_stub = MagicMock()
            main_stub.meeting_panel = panel
            app._main = main_stub

            with patch("sayink.app.QApplication.quit"):
                app._finish_quit()

            assert events == ["panel.cleanup"]

    def test_finish_quit_deletes_real_audio_player_session_temp_wavs(self, _qapp_session, tmp_path):
        from sayink.ui.meeting_page import _AudioPlayerBar

        bar = _AudioPlayerBar()
        temp_wav = tmp_path / "playback_session.wav"
        temp_wav.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
        bar._session_temp_wavs.add(temp_wav)
        bar._temp_wavs_by_source["src"] = {temp_wav}

        panel_stub = types.SimpleNamespace(
            _audio_player=bar,
            flush_pending_user_notes=lambda: None,
            _flush_bound_title=lambda sync=True: None,
            _save_record_notes=lambda: None,
            cancel_downloads=lambda: None,
            running_workers=(),
        )
        main_stub = types.SimpleNamespace(
            meeting_panel=panel_stub,
            history_panel=types.SimpleNamespace(flush_pending_delete=lambda: None, running_workers=()),
            hide=lambda: None,
        )
        try:
            with app_harness() as h:
                app = h["app"]
                app._main = main_stub
                with patch("sayink.app.QApplication.quit"):
                    app._finish_quit()
            assert not temp_wav.exists()
            assert bar._session_temp_wavs == set()
        finally:
            bar.deleteLater()


class TestBoundedStorageBusyAndInstallerHandoffMargin:
    """FC-07: ``storage_busy`` shares bounded ``_quit_drain_deadline`` and installer margin."""

    def test_quit_timeout_and_inno_setup_wait_have_safe_handoff_margin(self):
        assert app_mod.QUIT_NATIVE_DRAIN_TIMEOUT_S == 8.0
        iss_text = (REPO_ROOT / "installer" / "SayInk-Setup.iss").read_text(encoding="utf-8")
        match = re.search(r"WaitForSingleObject\(\s*ProcHandle\s*,\s*(\d+)\s*\)", iss_text)
        assert match is not None
        wait_ms = int(match.group(1))
        assert wait_ms == 18000
        assert wait_ms >= int(app_mod.QUIT_NATIVE_DRAIN_TIMEOUT_S * 1000) + 5000

    def test_wedged_storage_busy_hits_bounded_deadline_and_exits(self, caplog, tmp_path):
        real_cfg = Config(config_dir=str(tmp_path))
        with app_harness() as h:
            app = h["app"]
            app._config = real_cfg
            app._quit_drain_timeout_s = 0.05
            released: list[str] = []
            app._on_release_instance_lock = lambda: released.append("released")
            h["recorder"].storage_busy = True

            with (
                caplog.at_level(logging.WARNING, logger="SayInk"),
                patch("sayink.app.QTimer.singleShot") as later,
                patch("sayink.app.QApplication.quit") as quit_app,
                patch("sayink.app._force_process_exit") as force_exit,
            ):
                app._finish_quit()
                assert released == []
                quit_app.assert_not_called()
                force_exit.assert_not_called()
                assert app._quit_drain_deadline is not None
                assert later.called

                # Simulate a late debounced config edit before final forced exit.
                real_cfg.set("update.last_check_at", "2026-11-15T00:00:00Z")
                time.sleep(0.07)
                later.call_args.args[1]()

                assert released == ["released"]
                quit_app.assert_called_once()
                force_exit.assert_called_once_with(0)
                assert any("录音缓存落盘超时" in rec.message for rec in caplog.records)
                saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
                assert saved["update"]["last_check_at"] == "2026-11-15T00:00:00Z"

    def test_storage_busy_and_pending_workers_share_one_drain_deadline(self, caplog):
        gate = threading.Event()
        worker = _BlockedWorker(gate)
        worker.start()
        try:
            with app_harness() as h:
                app = h["app"]
                app._quit_drain_timeout_s = 0.06
                h["recorder"].storage_busy = True
                h["recognizer"].running_workers = (worker,)

                with (
                    caplog.at_level(logging.WARNING, logger="SayInk"),
                    patch("sayink.app.QTimer.singleShot") as later,
                    patch("sayink.app.QApplication.quit") as quit_app,
                    patch("sayink.app._force_process_exit") as force_exit,
                ):
                    app._finish_quit()
                    first_deadline = app._quit_drain_deadline
                    assert first_deadline is not None

                    # Budget elapses while storage_busy was True; then storage_busy clears,
                    # leaving the wedged worker on the already-expired shared deadline.
                    time.sleep(0.08)
                    h["recorder"].storage_busy = False
                    later.call_args.args[1]()

                    assert app._quit_drain_deadline == first_deadline
                    quit_app.assert_called_once()
                    force_exit.assert_called_once_with(0)
        finally:
            gate.set()
            assert worker.wait(2000)


class TestModalDialogThemeRefresh:
    """UI-13 part 1: Open top-level ``QDialog``s receive ``reapply_theme`` on theme switch."""

    def test_apply_appearance_theme_refreshes_top_level_dialogs_once(self, _qapp_session):
        from sayink.ui.transcript_import_dialog import TranscriptImportDialog

        class _CustomModalDialog(QDialog):
            def __init__(self):
                super().__init__()
                self.reapply_calls = 0

            def reapply_theme(self) -> None:
                self.reapply_calls += 1

        custom_dlg = _CustomModalDialog()
        import_dlg = TranscriptImportDialog()
        custom_dlg.show()
        import_dlg.show()
        import_reapply_calls: list[str] = []
        orig_reapply = import_dlg.reapply_theme
        import_dlg.reapply_theme = lambda: (import_reapply_calls.append("reapplied"), orig_reapply())
        try:
            with app_harness() as h:
                app = h["app"]
                # Also verify _recovery_dialog is not called twice when also a top-level widget.
                recovery_dlg = _CustomModalDialog()
                recovery_dlg.show()
                try:
                    app._recovery_dialog = recovery_dlg
                    custom_dlg.reapply_calls = 0
                    import_reapply_calls.clear()
                    recovery_dlg.reapply_calls = 0

                    app.apply_appearance_theme("light")
                    assert custom_dlg.reapply_calls == 1
                    assert len(import_reapply_calls) == 1
                    assert recovery_dlg.reapply_calls == 1

                    app.apply_appearance_theme("dark")
                    assert custom_dlg.reapply_calls == 2
                    assert len(import_reapply_calls) == 2
                    assert recovery_dlg.reapply_calls == 2
                finally:
                    app._recovery_dialog = None
                    recovery_dlg.close()
                    recovery_dlg.deleteLater()
        finally:
            custom_dlg.close()
            custom_dlg.deleteLater()
            import_dlg.close()
            import_dlg.deleteLater()


class TestBuildEnvironmentAndStaticGates:
    """FC-08, FC-09, FC-10: ``build._build_environment()``, CI ruff gate, and QA header."""

    def test_build_environment_includes_venv_and_conda_dll_dirs_without_external_path_leak(
        self, monkeypatch, tmp_path
    ):
        win_dir = tmp_path / "Windows"
        (win_dir / "System32").mkdir(parents=True)
        venv_dir = tmp_path / "venv"
        (venv_dir / "Scripts").mkdir(parents=True)
        (venv_dir / "Library" / "bin").mkdir(parents=True)
        base_dir = tmp_path / "BasePython"
        (base_dir / "Library" / "bin").mkdir(parents=True)
        (base_dir / "DLLs").mkdir(parents=True)
        rogue_dir = tmp_path / "RogueQtApp" / "bin"
        rogue_dir.mkdir(parents=True)

        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("SystemRoot", str(win_dir))
        monkeypatch.setenv("PATH", str(rogue_dir))
        monkeypatch.setattr(sys, "prefix", str(venv_dir))
        monkeypatch.setattr(sys, "base_prefix", str(base_dir))

        env = build._build_environment()
        entries = env["PATH"].split(os.pathsep)
        assert entries == [
            str(win_dir / "System32"),
            str(win_dir),
            str(venv_dir),
            str(venv_dir / "Scripts"),
            str(venv_dir / "Library" / "bin"),
            str(base_dir),
            str(base_dir / "Library" / "bin"),
            str(base_dir / "DLLs"),
        ]
        assert str(rogue_dir) not in entries

    def test_ci_workflow_runs_ruff_check_and_ruff_toml_caps_complexity(self):
        workflow = (REPO_ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        assert "- name: Run static lint checks" in workflow
        assert "python -m ruff check ." in workflow
        assert workflow.index("python -m ruff check .") < workflow.index("python -m pytest")

        ruff_toml = (REPO_ROOT / "ruff.toml").read_text(encoding="utf-8")
        match = re.search(r"max-complexity\s*=\s*(\d+)", ruff_toml)
        assert match is not None
        assert int(match.group(1)) <= 30

    def test_qa_redblue_doc_has_archival_closure_note_above_preserved_body(self):
        doc = (REPO_ROOT / "docs" / "qa-redblue-20261112.md").read_text(encoding="utf-8")
        lines = doc.splitlines()
        assert lines[0] == "# SayInk 红蓝对抗与全项目压力验证 · 2026-11-12"
        header_block = "\n".join(lines[1:6])
        assert header_block.lstrip().startswith(">")
        assert "v3.0.2+" in header_block
        assert "QA-01" in header_block and "QA-18" in header_block
        for item in ("QA-07", "QA-11", "QA-12"):
            assert item in header_block
        # Historical 2026-11-12 audit body remains intact below the header.
        assert "**验收建议：暂缓发布当前工作区。**" in doc
        assert "| QA-18 | P3 · UI 证据工具失效 |" in doc

"""Swarm regression suite for Core Lifecycle, Exit Drain Safety, Import Privacy Contract,
Deferred-Settings UI Notice, Updater/Release-Gate Diagnostics, and README Truth-Source Sync."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QThread
from PyQt6.QtWidgets import QApplication

import sayink.updater as updater_mod
from sayink.audio_recovery import (
    INCOMPLETE_QUOTA_GRACE_SECONDS,
    INCOMPLETE_RETENTION_SECONDS,
    MAX_RECOVERY_BYTES,
    MIN_FREE_BYTES,
    RETENTION_SECONDS,
)
from sayink.config import Config
from sayink.history_store import HistoryStore
from sayink.media_import import MediaTurn, RecognitionResult
from sayink.meeting_session import MeetingController, MeetingOptions
from sayink.meeting_store import (
    QUEUE_PRIORITY_AUTO,
    STATUS_ENDED,
    STATUS_INTERRUPTED,
    MeetingStore,
)
from sayink.reload_coordinator import DEFERRED_APPLY_NOTICE
from sayink.ui.settings_window import SettingsWindow
from sayink.updater import InstallerVerificationError, UpdateError, download_installer
from sayink_build import release_gate
from sayink_build.release_signing import ed25519_public_key_from_seed
from tests.helpers.app_harness import app_harness


# ── 1. Exit Drain Data-Loss Safety ────────────────────────────────────────────


def test_exit_drain_commits_active_media_import_turns_as_interrupted_without_starting_queue_or_auto_minutes(
    tmp_path: Path,
):
    """P1 Exit Drain: Quitting during an active _MediaImportJob keeps HistoryStore open until
    the active segment finishes, commits already-recognized turns as STATUS_INTERRUPTED, and
    never starts the next queued media file or auto-minutes."""
    db_path = tmp_path / "history.db"
    first_wav = tmp_path / "first.wav"
    second_wav = tmp_path / "second.wav"
    first_wav.write_bytes(b"RIFF0000WAVE")
    second_wav.write_bytes(b"RIFF0000WAVE")

    real_cfg = Config(config_dir=str(tmp_path))
    real_cfg.set("meeting.auto_generate", True)
    real_cfg.set("llm.api_url", "https://api.example.com/v1")
    real_cfg.set("llm.api_key", "sk-test")
    real_cfg.set("llm.model_name", "gpt-test")

    real_history = HistoryStore(db_path=db_path)
    in_segment = threading.Event()
    imported_files: list[str] = []

    def _fake_media_runner(path, cancel, _progress, *_extra, **_kwargs):
        imported_files.append(Path(path).name)
        in_segment.set()
        # Simulate finishing the active segment right after cancel is signaled on shutdown.
        deadline = time.monotonic() + 3.0
        while cancel is not None and not cancel() and time.monotonic() < deadline:
            time.sleep(0.01)
        turns = (
            MediaTurn(1, 0, 1500, "第一段已经识别完毕的会议发言内容。" * 8),
            MediaTurn(2, 1800, 1500, "退出前当前这一段识别完成的发言内容。" * 8),
        )
        return RecognitionResult(turns, cancelled=bool(cancel and cancel()), audio_ms=3300)

    try:
        with app_harness() as h:
            app = h["app"]
            old_meeting = app._meeting
            old_meeting.shutdown(wait_ms=100)
            app._config = real_cfg
            app._history = real_history
            controller = MeetingController(
                real_cfg, real_history, pending_segments=lambda: 0, parent=app
            )
            controller.set_media_runner(_fake_media_runner)
            controller.confirm_remote_host("https://api.example.com")
            app._meeting = controller

            released_lock: list[str] = []
            app._on_release_instance_lock = lambda: released_lock.append("released")
            real_cfg.set("update.last_check_at", "2026-11-16T16:30:00Z")

            assert controller.start_media_import(str(first_wav), title="客户评审录音") == ""
            assert controller.start_media_import(str(second_wav), title="第二份排队录音") == ""
            assert len(controller.queued_media()) == 1
            assert in_segment.wait(2.0), "Media import worker did not enter runner"

            scheduled: list = []
            with (
                patch.object(app, "_later", side_effect=lambda _ms, cb: scheduled.append(cb)),
                patch("sayink.app.QApplication.quit") as quit_app,
            ):
                app._finish_quit()
                # Worker is still draining its active segment; HistoryStore MUST stay open
                # and instance lock must stay held while meeting worker writes to SQLite!
                assert real_history._closed is False
                assert released_lock == []
                quit_app.assert_not_called()

                qapp = QApplication.instance()
                deadline = time.monotonic() + 3.0
                while not quit_app.called and time.monotonic() < deadline:
                    if qapp is not None:
                        qapp.processEvents()
                    if scheduled:
                        cb = scheduled.pop(0)
                        cb()
                    else:
                        time.sleep(0.01)

                quit_app.assert_called_once()

            assert real_history._closed is True
            assert released_lock == ["released"]
            assert imported_files == ["first.wav"]
    finally:
        if not real_history._closed:
            real_history.close(timeout=2.0)

    saved_cfg = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert saved_cfg["update"]["last_check_at"] == "2026-11-16T16:30:00Z"

    # Reopen SQLite database and verify 0% data loss + STATUS_INTERRUPTED + no auto-minutes!
    verify_history = HistoryStore(db_path=db_path)
    try:
        verify_store = MeetingStore(verify_history)
        meetings = verify_store.list_meetings()
        assert len(meetings) == 1
        rec = meetings[0]
        assert rec.title == "客户评审录音"
        assert rec.status == STATUS_INTERRUPTED
        segs = verify_store.get_segments(rec.id)
        assert len(segs) == 2
        assert "第一段已经识别完毕" in segs[0].raw_text
        assert "当前这一段识别完成" in segs[1].raw_text
        assert verify_store.generation_queue() == []
    finally:
        verify_history.close(timeout=2.0)


class _CheckpointingMinutesWorker(QThread):
    def __init__(
        self,
        started_evt: threading.Event,
        cancel_evt: threading.Event,
        history: HistoryStore,
        save_fn,
    ):
        super().__init__()
        self._started_evt = started_evt
        self._cancel_evt = cancel_evt
        self._history = history
        self._save_fn = save_fn
        self.history_was_open_at_save: bool = False

    def cancel(self) -> None:
        self._cancel_evt.set()

    def run(self) -> None:
        self._started_evt.set()
        self._cancel_evt.wait(3.0)
        self.history_was_open_at_save = not self._history._closed
        self._save_fn("chunk:0", '{"summary":"已完成首段摘要"}')


def test_exit_drain_preserves_minutes_checkpoint_saved_during_worker_drain(tmp_path: Path):
    """MinutesWorker saving a chunk checkpoint during shutdown drain succeeds before HistoryStore closes."""
    db_path = tmp_path / "history.db"
    real_cfg = Config(config_dir=str(tmp_path))
    real_history = HistoryStore(db_path=db_path)
    started_evt = threading.Event()
    cancel_evt = threading.Event()

    try:
        store = MeetingStore(real_history)
        meeting = store.create_meeting(title="长会议纪要断点保护")
        assert real_history.flush(timeout=2.0)

        worker = _CheckpointingMinutesWorker(
            started_evt,
            cancel_evt,
            real_history,
            lambda key, raw: store.save_generation_checkpoint(meeting.id, "fp-drain", key, raw),
        )
        worker.start()
        assert started_evt.wait(2.0)

        with app_harness() as h:
            app = h["app"]
            old_meeting = app._meeting
            old_meeting.shutdown(wait_ms=100)
            app._config = real_cfg
            app._history = real_history
            controller = MeetingController(
                real_cfg, real_history, pending_segments=lambda: 0, parent=app
            )
            controller._generator = SimpleNamespace(
                running_workers=(worker,),
                shutdown=lambda wait_ms=0: worker.cancel(),
                is_running=True,
                cancel=worker.cancel,
            )
            app._meeting = controller

            scheduled: list = []
            with (
                patch.object(app, "_later", side_effect=lambda _ms, cb: scheduled.append(cb)),
                patch("sayink.app.QApplication.quit") as quit_app,
            ):
                app._finish_quit()
                deadline = time.monotonic() + 3.0
                while not quit_app.called and time.monotonic() < deadline:
                    if scheduled:
                        scheduled.pop(0)()
                    else:
                        time.sleep(0.01)
                quit_app.assert_called_once()

            assert worker.history_was_open_at_save is True
            assert real_history._closed is True
    finally:
        cancel_evt.set()
        worker.wait(2000)
        if not real_history._closed:
            real_history.close(timeout=2.0)

    verify_history = HistoryStore(db_path=db_path)
    try:
        verify_store = MeetingStore(verify_history)
        checkpoints = verify_store.generation_checkpoints(meeting.id, "fp-drain")
        assert checkpoints == {"chunk:0": '{"summary":"已完成首段摘要"}'}
    finally:
        verify_history.close(timeout=2.0)


def test_exit_drain_timeout_closes_history_and_saves_config_before_force_exit(tmp_path: Path):
    """If a wedged native worker hits _quit_drain_timeout_s, persistence finalizes before _force_process_exit."""
    gate = threading.Event()

    class _WedgedWorker(QThread):
        def run(self) -> None:
            # Stay wedged until the test releases the gate. A short timeout
            # expires while App construction is slow and the drain never starts.
            gate.wait()

    worker = _WedgedWorker()
    worker.start()
    try:
        real_cfg = Config(config_dir=str(tmp_path))
        with app_harness() as h:
            app = h["app"]
            app._config = real_cfg
            app._quit_drain_timeout_s = 0.03
            h["recognizer"].running_workers = (worker,)
            real_cfg.set("update.last_check_at", "2026-11-16T17:00:00Z")

            call_order: list[str] = []
            h["history"].close.side_effect = lambda **_: call_order.append("history.close")

            with (
                patch("sayink.app.QTimer.singleShot") as later,
                patch("sayink.app.QApplication.quit") as quit_app,
                patch("sayink.app._force_process_exit", side_effect=lambda c: call_order.append(f"exit:{c}")),
            ):
                app._finish_quit()
                assert call_order == []
                time.sleep(0.05)
                later.call_args.args[1]()
                quit_app.assert_called_once()
                assert call_order == ["history.close", "exit:0"]
                saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
                assert saved["update"]["last_check_at"] == "2026-11-16T17:00:00Z"
    finally:
        gate.set()
        worker.wait(2000)


# ── 2. Import Auto-Generate Privacy & Billing Contract ────────────────────────


def test_import_transcript_and_media_never_auto_generate_by_default_and_resume_deferred_queue(
    tmp_path: Path, qapp, monkeypatch: pytest.MonkeyPatch
):
    """P1 Privacy/Billing Contract: Imported transcripts and media files NEVER auto-enqueue AI minutes
    by default even when meeting.auto_generate=True; unrelated deferred queue jobs still resume."""
    cfg = Config(config_dir=str(tmp_path))
    cfg.set("meeting.auto_generate", True)
    cfg.set("llm.api_url", "http://localhost:11434/v1")
    cfg.set("llm.api_key", "")
    cfg.set("llm.model_name", "qwen2.5:7b")

    history = HistoryStore(db_path=tmp_path / "history.db")
    controller = MeetingController(cfg, history, pending_segments=lambda: 0)
    started_ids: list[str] = []
    monkeypatch.setattr(controller, "_start", lambda mid, *_a, **_kw: started_ids.append(mid) or "")

    try:
        # 1. Default transcript import (>200 chars) does NOT enqueue auto-generation.
        long_text = "\n".join(
            f"产品经理：这是第 {idx} 条详细的项目里程碑复盘与下阶段排期计划。" for idx in range(20)
        )
        assert len(long_text) >= 200
        text_rec = controller.import_transcript(long_text, title="外部长文字稿")
        assert text_rec is not None
        assert controller.queued_generation(text_rec.id) is None
        assert started_ids == []

        # 2. Preexisting live meeting queued while _importing_media=True is paused (local LLM),
        #    then resumes once commit_media_import finishes, WITHOUT enqueueing the imported media!
        live_rec = controller.import_transcript("张三：这是一场已经结束的会议文字稿。", title="刚结束的实时会议")
        assert live_rec is not None
        controller._store.enqueue_generation(live_rec.id, "general", priority=QUEUE_PRIORITY_AUTO)
        assert history.flush(timeout=2.0)

        controller._importing_media = True
        controller._pump_queue()
        assert started_ids == []  # Paused while media import is active on local LLM

        media_turns = tuple(
            MediaTurn(1, i * 2000, 1500, f"这是第 {i} 段足够长的导入音视频转写内容不会自动上传远端。")
            for i in range(15)
        )
        media_rec = controller.commit_media_import(
            RecognitionResult(media_turns, cancelled=False, audio_ms=30_000),
            "外部视频导入",
        )
        assert media_rec is not None
        assert media_rec.status == STATUS_ENDED
        assert controller.queued_generation(media_rec.id) is None
        assert media_rec.id not in started_ids
        # Preexisting live meeting queue resumed cleanly!
        assert started_ids == [live_rec.id]
        controller._store.dequeue_generation(live_rec.id)
        controller._store.flush()
        started_ids.clear()

        # 3. start_media_import background job also defaults to auto_generate=False!
        sample_wav = tmp_path / "batch.wav"
        sample_wav.write_bytes(b"RIFF0000WAVE")
        controller.set_media_runner(
            lambda *_a, **_kw: RecognitionResult(media_turns, cancelled=False, audio_ms=30_000)
        )
        finished_ids: list[str] = []
        controller.media_import_finished.connect(finished_ids.append)
        assert controller.start_media_import(str(sample_wav), title="后台导入") == ""
        deadline = time.monotonic() + 3.0
        while not finished_ids and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        assert len(finished_ids) == 1 and finished_ids[0]
        assert controller.queued_generation(finished_ids[0]) is None
        assert started_ids == []
    finally:
        controller.shutdown()
        history.close(timeout=2.0)


# ── 3. Consistent Deferred-Settings UI Notice ─────────────────────────────────


def test_deferred_settings_notice_shown_on_settings_panel_during_recording_and_meeting(
    config, qapp, monkeypatch: pytest.MonkeyPatch
):
    """Editing audio source, mic/system device, recognition, or model settings during active
    recording or meeting surfaces DEFERRED_APPLY_NOTICE on settings_panel and clears on session end."""
    monkeypatch.setattr(SettingsWindow, "_refresh_audio_device_lists", lambda self: None)
    panel = SettingsWindow(config)
    with app_harness() as h:
        app = h["app"]
        app._config = config
        app._reload_coord._config = config
        app._main = SimpleNamespace(
            settings_panel=panel,
            meeting_panel=MagicMock(),
            history_panel=MagicMock(),
            refresh_shell_status=MagicMock(),
        )
        panel.settings_changed.connect(app._on_settings_changed)
        panel.models_changed.connect(app._on_models_changed)
        panel.recognition_changed.connect(app._on_recognition_settings_changed)
        try:
            # Part A: Active dictation recording -> edit audio source / device & model
            h["recorder"].is_recording = True
            config.set("audio.input_source", "mixed")
            config.set("audio.mic_device_index", 2)
            panel.settings_changed.emit()
            qapp.processEvents()

            assert app.reload_snapshot.pending_apply_notice == DEFERRED_APPLY_NOTICE
            assert panel.general_page.mic_test_status.text() == DEFERRED_APPLY_NOTICE
            assert DEFERRED_APPLY_NOTICE in panel.general_page.recognition_apply_label.text()

            config.set("stt.model_id", "sensevoice")
            panel.models_changed.emit()
            qapp.processEvents()
            assert panel.model_page.model_notice_label.text() == DEFERRED_APPLY_NOTICE
            assert not panel.model_page.model_notice_label.isHidden()

            # Recording finishes -> deferred audio & model reload apply and clear the notice!
            h["recorder"].is_recording = False
            app._apply_pending_recognition_settings()
            qapp.processEvents()
            assert app.reload_snapshot.pending_apply_notice == ""
            assert panel.general_page.mic_test_status.text() != DEFERRED_APPLY_NOTICE
            assert panel.general_page.recognition_apply_label.text() == ""
            assert panel.model_page.model_notice_label.text() == ""

            # Part B: Active meeting -> edit audio device & recognition language
            h["recorder"].start_continuous.side_effect = lambda: setattr(h["recorder"], "is_continuous", True)
            with patch("sayink.meeting_session.aux_model_path", return_value=None):
                assert app.start_meeting(MeetingOptions())

            config.set("audio.system_device_index", 5)
            panel.settings_changed.emit()
            idx = panel.general_page.meeting_language_combo.findData("en")
            assert idx >= 0
            panel.general_page.meeting_language_combo.setCurrentIndex(idx)
            qapp.processEvents()

            assert app.reload_snapshot.deferred_meeting_audio_settings is True
            assert panel.general_page.mic_test_status.text() == DEFERRED_APPLY_NOTICE
            assert DEFERRED_APPLY_NOTICE in panel.general_page.recognition_apply_label.text()

            # Ending the meeting resets deferred meeting audio & applies pending recognition settings!
            h["recorder"].is_continuous = False
            app._end_meeting()
            qapp.processEvents()
            assert app.reload_snapshot.deferred_meeting_audio_settings is False
            assert app.reload_snapshot.pending_apply_notice == ""
            assert panel.general_page.mic_test_status.text() != DEFERRED_APPLY_NOTICE
        finally:
            for sig, slot in (
                (panel.settings_changed, app._on_settings_changed),
                (panel.models_changed, app._on_models_changed),
                (panel.recognition_changed, app._on_recognition_settings_changed),
            ):
                try:
                    sig.disconnect(slot)
                except TypeError:
                    pass
            panel.hide()
            panel.deleteLater()


# ── 4. Updater & Release Gate Supply-Chain Clarity ────────────────────────────


class _FakeHTTPResponse:
    def __init__(self, data: bytes):
        self._buf = io.BytesIO(data)
        self.headers = {"Content-Length": str(len(data))}

    def read(self, size: int = -1) -> bytes:
        return self._buf.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def test_updater_logs_unconfigured_signing_key_warning_and_enforces_host_and_sha256(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """When RELEASE_SIGNING_PUBLIC_KEY and SAYINK_UPDATE_PUBLIC_KEY are empty, download_installer
    logs a clear diagnostic warning while strictly enforcing GitHub host allowlist and SHA-256."""
    monkeypatch.setattr(updater_mod, "RELEASE_SIGNING_PUBLIC_KEY", "")
    monkeypatch.delenv(updater_mod.PUBLIC_KEY_ENV_VAR, raising=False)

    payload = b"MZ-fake-lite-installer-payload"
    digest = hashlib.sha256(payload).hexdigest()
    exe_url = "https://github.com/zyjhandsome/SayInk/releases/download/v3.0.7/SayInk-Setup-3.0.7.exe"

    def fake_urlopen(req, timeout=60):
        del timeout
        assert req.full_url == exe_url
        return _FakeHTTPResponse(payload)

    dest = tmp_path / "SayInk-Setup-3.0.7.exe"
    with caplog.at_level(logging.WARNING, logger="SayInk"):
        download_installer(
            exe_url,
            dest,
            fake_urlopen,
            expected_size=len(payload),
            sha256=digest,
        )
    assert dest.exists() and dest.read_bytes() == payload
    assert any(
        "RELEASE_SIGNING_PUBLIC_KEY" in rec.message
        and "SHA-256" in rec.message
        and ".sig" in rec.message
        for rec in caplog.records
    )

    # Untrusted host is still strictly rejected!
    with pytest.raises((UpdateError, ValueError), match="不受信任|HTTPS|域名|地址"):
        download_installer(
            "https://evil.example.com/SayInk-Setup-3.0.7.exe",
            tmp_path / "untrusted.exe",
            fake_urlopen,
            expected_size=len(payload),
            sha256=digest,
        )

    # Corrupted SHA-256 is still strictly rejected!
    with pytest.raises(InstallerVerificationError, match="校验值不符"):
        download_installer(
            exe_url,
            tmp_path / "badhash.exe",
            fake_urlopen,
            expected_size=len(payload),
            sha256="0" * 64,
        )


def test_release_gate_advisories_and_signing_report_non_blocking_for_unsigned_builds(
    monkeypatch: pytest.MonkeyPatch,
):
    """Unsigned open-source test builds pass release_problems while release_advisories and
    release_signing_report surface signing key status and catch malformed/mismatched keys."""
    head = "a" * 40
    responses = {
        "git status": (0, ""),
        "git rev-parse": (0, f"{head}\n"),
        "git ls-remote": (0, f"{head}\trefs/tags/v3.0.6\n"),
        "gh run": (0, json.dumps([{"status": "completed", "conclusion": "success"}])),
        "ruff check": (0, ""),
        "pytest": (0, ""),
    }

    def run(cmd):
        if "ruff" in cmd:
            key = "ruff check"
        elif "pytest" in cmd:
            key = "pytest"
        else:
            key = " ".join(cmd[:2])
        code, out = responses[key]
        return subprocess.CompletedProcess(cmd, code, stdout=out, stderr="")

    # 1. Empty pinned public key: non-blocking advisory, zero blocking problems!
    monkeypatch.setattr(updater_mod, "RELEASE_SIGNING_PUBLIC_KEY", "")
    assert release_gate.release_problems("3.0.6", run=run) == []
    adv_unsigned = release_gate.release_advisories(env={}, public_key_hex="")
    assert len(adv_unsigned) == 3
    report_unsigned = release_gate.release_signing_report(env={}, public_key_hex="")
    assert "unconfigured" in report_unsigned
    assert "SHA-256" in report_unsigned

    # 2. Matching Ed25519 seed + public key + signtool: zero advisories!
    seed = bytes(range(32))
    pub_hex = ed25519_public_key_from_seed(seed).hex()
    env_signed = {
        "SAYINK_RELEASE_PRIVATE_KEY": seed.hex(),
        "SAYINK_SIGNTOOL_CMD": 'signtool sign /fd SHA256 "{file}"',
    }
    assert release_gate.release_advisories(env=env_signed, public_key_hex=pub_hex) == []
    report_signed = release_gate.release_signing_report(env=env_signed, public_key_hex=pub_hex)
    assert "configured" in report_signed

    # 3. Mismatched private seed vs pinned public key -> flagged in advisory!
    other_pub_hex = ed25519_public_key_from_seed(bytes([7] * 32)).hex()
    adv_mismatch = release_gate.release_advisories(env=env_signed, public_key_hex=other_pub_hex)
    assert any("does not match" in item for item in adv_mismatch)

    # 4. Malformed non-empty pinned RELEASE_SIGNING_PUBLIC_KEY -> blocks release_problems!
    monkeypatch.setattr(updater_mod, "RELEASE_SIGNING_PUBLIC_KEY", "not-a-valid-hex-key")
    problems = release_gate.release_problems("3.0.6", run=run)
    assert len(problems) == 1 and "RELEASE_SIGNING_PUBLIC_KEY" in problems[0]


# ── 5. README & Terminology Truth-Source Contract ─────────────────────────────


def test_readme_and_terminology_truth_source_contract():
    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")
    terminology = (root / "docs" / "terminology.md").read_text(encoding="utf-8")

    assert MAX_RECOVERY_BYTES == 5 * 1024**3
    assert INCOMPLETE_QUOTA_GRACE_SECONDS == 24 * 3600
    assert INCOMPLETE_RETENTION_SECONDS == 30 * 86400
    assert RETENTION_SECONDS == 7 * 86400
    assert MIN_FREE_BYTES == 128 * 1024**2

    assert "总量上限 5 GB" in readme
    assert "24 小时保护期" in readme
    assert "超 30 天自动清理" in readme
    assert readme.count("约 89 MB") >= 2
    assert "约 81 MB" not in readme
    assert DEFERRED_APPLY_NOTICE in readme
    assert DEFERRED_APPLY_NOTICE in terminology

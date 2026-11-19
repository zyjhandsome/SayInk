"""Durability, bounded memory and explicit opt-in for local recording recovery."""
import json
import time
import wave
import subprocess
import sys
from unittest.mock import patch

import numpy as np
import pytest

from sayink.audio_recovery import (
    RecordingJournal, recordings, delete_recording, store_pending, StoredAudio,
    hold_recovery_path, release_recovery_path, delete_sessions_recovery_files,
)
from sayink.audio_recorder import AudioRecorder
from sayink.asr_context import with_context
from tests.helpers.app_harness import app_harness

RATE = 16000


def test_journal_survives_partial_final_sample_and_exports_every_whole_sample(tmp_path):
    root = tmp_path / "recordings"
    journal = RecordingJournal(root)
    expected = np.linspace(-0.2, 0.2, 16003, dtype=np.float32)
    for part in np.array_split(expected, 7):
        assert journal.append(part)
    assert journal.close()
    item, = recordings(root)
    with (item.path / "audio.f32").open("ab") as handle:
        handle.write(b"\x00\x01\x02")
    restored = np.concatenate(list(item.blocks(1600)))
    np.testing.assert_array_equal(restored, expected)
    assert item.metadata["complete"] is True
    item.export_wav(tmp_path / "recovered.wav")
    with wave.open(str(tmp_path / "recovered.wav"), "rb") as out:
        assert out.getnframes() == len(expected)
        assert out.getframerate() == RATE
        np.testing.assert_array_equal(np.frombuffer(out.readframes(len(expected)), "<i2"),
                                      (expected * 32767).astype("<i2"))


def test_interrupted_journal_is_visible_without_trusting_metadata_sample_count(tmp_path):
    session = tmp_path / ("a" * 32)
    session.mkdir()
    (session / "session.json").write_text(json.dumps({"sample_rate": RATE, "complete": False}), encoding="utf-8")
    np.ones(RATE, "<f4").tofile(session / "audio.f32")
    (tmp_path / "unrelated").mkdir()
    (tmp_path / ("b" * 32)).mkdir()
    item, = recordings(tmp_path)
    assert item.seconds == 1 and not item.metadata["complete"]
    delete_recording(tmp_path, item)
    assert not session.exists()
    assert (tmp_path / "unrelated").exists()


def test_quota_failure_does_not_overwrite_earlier_recording(tmp_path):
    errors = []
    journal = RecordingJournal(tmp_path, max_bytes=128, on_error=errors.append)
    assert journal.append(np.ones(16, np.float32))
    assert journal.append(np.ones(32, np.float32))
    assert journal.close()
    assert errors and not journal.healthy
    item, = recordings(tmp_path)
    assert item.samples == 16
    assert item.metadata["complete"] is False
    assert item.metadata["closed"] is True
    np.testing.assert_array_equal(np.concatenate(list(item.blocks())), np.ones(16, np.float32))
    # A quota stop is closed, so the next meeting may remove it. It must not
    # have been truncated in place by the chunk that did not fit.
    follow = RecordingJournal(tmp_path, max_bytes=64)
    try:
        assert not item.path.exists()
        assert follow.close()
    finally:
        if follow._thread.is_alive():
            follow.close()


@pytest.mark.parametrize("complete", ["false", "0", 1, [False]])
def test_expiry_ignores_a_complete_flag_that_is_not_boolean_true(tmp_path, complete):
    folder = tmp_path / ("c" * 32)
    folder.mkdir()
    (folder / "session.json").write_text(json.dumps({
        "sample_rate": RATE, "complete": complete, "started_at": time.time() - 8 * 86400,
    }))
    np.ones(10, "<f4").tofile(folder / "audio.f32")
    journal = RecordingJournal(tmp_path)
    assert journal.close()
    assert folder.exists()


def test_expiry_only_removes_completed_old_recordings(tmp_path):
    for name, complete in (("a", True), ("b", False)):
        folder = tmp_path / (name * 32)
        folder.mkdir()
        (folder / "session.json").write_text(json.dumps({"sample_rate": RATE, "complete": complete,
                                                          "started_at": time.time() - 8 * 86400}))
        np.ones(10, "<f4").tofile(folder / "audio.f32")
    journal = RecordingJournal(tmp_path)
    assert journal.close()
    assert not (tmp_path / ("a" * 32)).exists()
    assert (tmp_path / ("b" * 32)).exists()


def test_pending_audio_roundtrips_context_and_rejects_truncation(tmp_path):
    source = with_context(np.arange(10), np.arange(3), np.arange(2))
    stored = store_pending(tmp_path, source)
    assert isinstance(stored, StoredAudio)
    np.testing.assert_array_equal(stored.load(), source)
    np.testing.assert_array_equal(stored.load().prefix, source.prefix)
    stored.path.write_bytes(b"\x00" * 8)
    with pytest.raises(OSError, match="不完整"):
        stored.load()
    stored.delete()
    assert not stored.path.exists()


def test_recording_does_not_create_audio_files_without_opt_in(tmp_path, mock_recording_hardware):
    recorder = AudioRecorder()
    recorder.start()
    assert recorder._journal is None
    assert not recorder.recovery_available
    recorder.stop()
    assert not list(tmp_path.iterdir())


def test_recorder_journals_audio_before_vad_rejects_it(tmp_path, mock_recording_hardware):
    recorder = AudioRecorder()
    recorder.set_recovery_directory(tmp_path)
    recorder.start()
    audio = np.full(1600, 0.00001, np.float32)
    assert recorder._feed_segmenter(audio) is None
    recorder.stop()
    item, = recordings(tmp_path)
    np.testing.assert_array_equal(np.concatenate(list(item.blocks())), audio)


def test_backlog_spills_to_disk_without_pausing_and_preserves_order():
    with app_harness({"audio.trigger_mode": "continuous", "audio.recovery_enabled": True}) as h:
        app = h["app"]
        h["recorder"].recovery_available = True
        h["recorder"].is_continuous = True
        for index in range(100):
            app._enqueue_audio(np.full(15 * RATE, index, np.float32), route=str(index))
        assert len(app._segment_queue) == 100
        ram = sum(item.audio.size for item in app._segment_queue if isinstance(item.audio, np.ndarray))
        assert ram <= 135 * RATE
        h["recorder"].stop_continuous.assert_not_called()
        for index in range(100):
            audio, route, _ctx = app._pop_queued_audio()
            assert route == str(index) and np.all(audio == index)
        assert not list((h["config"].config_dir / "audio-recovery" / "pending").iterdir())


def test_failed_spill_preserves_memory_and_uses_original_backlog_protection():
    with app_harness({"audio.trigger_mode": "continuous"}) as h:
        app = h["app"]
        h["recorder"].recovery_available = True
        h["recorder"].is_continuous = True
        with patch("sayink.audio_recovery.store_pending", side_effect=OSError("disk full")):
            for index in range(14):
                app._enqueue_audio(np.full(15 * RATE, index, np.float32))
        assert app._segment_queue[-1].audio[0] == 13
        assert h["recorder"].stop_continuous.called


def test_clear_queue_deletes_only_its_own_spill_files():
    with app_harness() as h:
        app = h["app"]
        h["recorder"].recovery_available = True
        for _ in range(10):
            app._enqueue_audio(np.ones(15 * RATE, np.float32))
        path = h["config"].config_dir / "audio-recovery" / "pending"
        unrelated = path / "keep.txt"
        unrelated.write_text("keep")
        app._clear_queued_audio()
        assert list(path.iterdir()) == [unrelated]


def test_recovery_modal_blocks_new_recording_and_model_reconfigure():
    with app_harness() as h:
        app = h["app"]
        app._recovery_busy = True
        app._on_recording_start()
        app._on_continuous_hotkey_start()
        h["recorder"].start.assert_not_called()
        h["recorder"].start_continuous.assert_not_called()
        assert app._pipeline_busy()


def test_corrupt_cached_segment_does_not_block_later_audio_and_keeps_evidence():
    with app_harness({"audio.trigger_mode": "continuous"}) as h:
        app = h["app"]
        h["recorder"].recovery_available = True
        for _ in range(10):
            app._enqueue_audio(np.ones(15 * RATE, np.float32))
        app._segment_queue = app._segment_queue[8:]
        failed = app._segment_queue[0]
        failed.audio.path.write_bytes(b"broken")
        with patch("sayink.app.QTimer.singleShot"):
            app._pump_segment_queue()
        assert len(app._segment_queue) == 1
        assert app._failed_cached_audio == [failed]
        assert failed.audio.path.exists()
        h["floating"].show_error.assert_called()
        app._clear_queued_audio()


def test_real_process_crash_leaves_recoverable_raw_audio_and_unfinished_metadata(tmp_path):
    script = """
import os, sys, time
from pathlib import Path
import numpy as np
from sayink.audio_recovery import RecordingJournal
j = RecordingJournal(Path(sys.argv[1]))
assert j.append(np.full(16000, 0.25, np.float32))
deadline = time.monotonic() + 5
while time.monotonic() < deadline:
    if (j.path / 'audio.f32').exists() and (j.path / 'audio.f32').stat().st_size == 64000:
        os._exit(0)
    time.sleep(0.01)
os._exit(2)
"""
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], timeout=10, check=False)
    assert result.returncode == 0
    item, = recordings(tmp_path)
    assert not item.metadata["complete"] and item.samples == 16000
    np.testing.assert_array_equal(np.concatenate(list(item.blocks())), np.full(16000, 0.25, np.float32))


def _completed_journal(root, *, samples: int, started_at: float, name: str = "a" * 32):
    folder = root / name
    folder.mkdir()
    (folder / "session.json").write_text(json.dumps({
        "sample_rate": RATE, "complete": True, "closed": True, "started_at": started_at,
    }), encoding="utf-8")
    np.ones(samples, "<f4").tofile(folder / "audio.f32")
    return folder


def test_active_write_deletes_the_oldest_completed_journal_and_keeps_going(tmp_path):
    old = _completed_journal(tmp_path, samples=10, started_at=time.time() - 3600)
    fresh_crash = tmp_path / ("b" * 32)
    fresh_crash.mkdir()
    (fresh_crash / "session.json").write_text(json.dumps({
        "sample_rate": RATE, "complete": False, "started_at": time.time() - 60,
    }), encoding="utf-8")
    np.ones(2, "<f4").tofile(fresh_crash / "audio.f32")
    errors = []
    # Completed 40 bytes + crash 8 bytes fit under the 100 byte cap, so startup
    # must not delete anything. The next 60 byte chunk crosses the cap.
    journal = RecordingJournal(tmp_path, max_bytes=100, on_error=errors.append)
    assert old.exists() and fresh_crash.exists()
    assert journal.append(np.ones(15, np.float32))
    deadline = time.monotonic() + 2
    while not (journal.path / "audio.f32").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    deadline = time.monotonic() + 2
    while (journal.path / "audio.f32").stat().st_size < 60 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not errors
    assert journal.healthy
    assert not old.exists()
    assert fresh_crash.exists()
    assert journal.close()
    assert (journal.path / "audio.f32").stat().st_size == 60


def test_quota_closed_journal_can_be_replaced_by_the_next_meeting(tmp_path):
    folder = tmp_path / ("c" * 32)
    folder.mkdir()
    (folder / "session.json").write_text(json.dumps({
        "sample_rate": RATE, "complete": False, "closed": True,
        "started_at": time.time() - 30, "error": "录音缓存已达到上限",
    }), encoding="utf-8")
    np.ones(40, "<f4").tofile(folder / "audio.f32")
    journal = RecordingJournal(tmp_path, max_bytes=80)
    assert journal.path != folder
    assert not folder.exists()
    assert journal.close()
    assert journal.path.exists()


def test_playback_hold_keeps_a_completed_journal_when_the_cache_is_over_cap(tmp_path):
    playing = _completed_journal(tmp_path, samples=20, started_at=time.time() - 7200, name="d" * 32)
    older = _completed_journal(tmp_path, samples=40, started_at=time.time() - 10_000, name="e" * 32)
    hold_recovery_path(playing)
    try:
        journal = RecordingJournal(tmp_path, max_bytes=200)
        assert playing.exists()
        assert not older.exists()
        journal.close()
    finally:
        release_recovery_path(playing)


@pytest.mark.parametrize("metadata", [None, "broken json", "[]"])
def test_damaged_metadata_does_not_hide_intact_recording(tmp_path, metadata):
    directory = tmp_path / ("d" * 32)
    directory.mkdir()
    if metadata is not None:
        (directory / "session.json").write_text(metadata)
    np.ones(16000, "<f4").tofile(directory / "audio.f32")
    item, = recordings(tmp_path)
    assert item.metadata["metadata_damaged"] and item.seconds == 1


def test_close_timeout_keeps_journal_incomplete_until_writer_finishes_sync(tmp_path):
    import threading

    entered_get = threading.Event()
    unpause_writer = threading.Event()
    real_queue_get = None

    journal = RecordingJournal(tmp_path)
    real_queue_get = journal._queue.get

    def gated_get(*args, **kwargs):
        item = real_queue_get(*args, **kwargs)
        entered_get.set()
        unpause_writer.wait(timeout=5.0)
        return item

    journal._queue.get = gated_get  # type: ignore[method-assign]
    assert journal.append(np.full(RATE, 0.5, dtype=np.float32))
    assert entered_get.wait(timeout=2.0)

    # Writer is paused before writing the 64,000B chunk: close() times out and returns False.
    assert journal.close(timeout=0.05) is False
    assert (journal.path / "audio.f32").stat().st_size == 0
    on_disk_during_stall = json.loads((journal.path / "session.json").read_text(encoding="utf-8"))
    assert on_disk_during_stall["complete"] is False

    # Startup interrupted-recording scan sees complete=False during this window.
    interrupted = [item for item in recordings(tmp_path) if not item.metadata.get("complete")]
    assert len(interrupted) == 1

    # Once writer resumes, it writes all 64,000B, fsyncs, and marks complete=True.
    unpause_writer.set()
    journal._thread.join(timeout=3.0)
    assert not journal._thread.is_alive()
    assert (journal.path / "audio.f32").stat().st_size == RATE * 4
    final_meta = json.loads((journal.path / "session.json").read_text(encoding="utf-8"))
    assert final_meta["complete"] is True
    assert final_meta["closed"] is True
    assert final_meta["samples"] == RATE
    assert list(journal.path.glob("*.tmp")) == []


def test_empty_journal_close_marks_complete_with_zero_samples(tmp_path):
    journal = RecordingJournal(tmp_path)
    assert journal.close(timeout=2.0) is True
    item, = recordings(tmp_path)
    assert item.samples == 0
    assert item.metadata["complete"] is True
    assert item.metadata["closed"] is True


def test_keep_completed_exempts_saved_meeting_audio_from_expiry_and_quota(tmp_path):
    aged_kept = tmp_path / ("a" * 32)
    aged_kept.mkdir()
    (aged_kept / "session.json").write_text(
        json.dumps(
            {
                "sample_rate": RATE,
                "complete": True,
                "closed": True,
                "keep_completed": True,
                "meeting_id": "m-keep",
                "started_at": time.time() - 30 * 86400,
            }
        ),
        encoding="utf-8",
    )
    np.ones(40, "<f4").tofile(aged_kept / "audio.f32")  # 160 bytes

    old_unkept = tmp_path / ("b" * 32)
    old_unkept.mkdir()
    (old_unkept / "session.json").write_text(
        json.dumps(
            {
                "sample_rate": RATE,
                "complete": True,
                "closed": True,
                "started_at": time.time() - 3600,
            }
        ),
        encoding="utf-8",
    )
    np.ones(20, "<f4").tofile(old_unkept / "audio.f32")  # 80 bytes

    # New journal with tiny 120B cap: aged_kept (160B, 30 days old) survives both
    # 7-day expiry and quota eviction, old_unkept is evicted, and new meeting still records!
    journal = RecordingJournal(tmp_path, max_bytes=120, keep_completed=True, metadata={"meeting_id": "m-new"})
    assert aged_kept.exists()
    assert not old_unkept.exists()
    assert journal.append(np.ones(10, dtype=np.float32))
    assert journal.close(timeout=2.0) is True
    new_meta = json.loads((journal.path / "session.json").read_text(encoding="utf-8"))
    assert new_meta["keep_completed"] is True
    assert new_meta["complete"] is True

    # Explicit meeting deletion still removes kept journals in a single batch pass.
    removed = delete_sessions_recovery_files(["m-keep", "m-new"], base_dir=tmp_path)
    assert removed == 2
    assert recordings(tmp_path) == []


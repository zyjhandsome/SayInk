"""Import a local audio or video file into one meeting without copying it."""

from __future__ import annotations

import dataclasses
import threading
import time
from pathlib import Path

import pytest

import numpy as np
from PyQt6.QtCore import QCoreApplication
from PyQt6.QtWidgets import QFileDialog, QPushButton

from sayink.audio_utils import TARGET_SAMPLE_RATE
from sayink.history_store import HistoryStore, SegmentRecord
from sayink.media_import import (
    MEDIA_SOURCE,
    AudioChunk,
    MediaTurn,
    RecognitionResult,
    file_dialog_filter,
    recognize_file,
    recognize_with_session,
    segment_pcm,
)
from sayink.meeting_session import MeetingCapabilities, MeetingController, MeetingOptions
from sayink.meeting_store import STATUS_ENDED, STATUS_INTERRUPTED, MeetingStore
from sayink.speaker_turns import RecognizedTurn
from sayink.ui.meeting_page import MeetingPage
from tests.helpers.app_harness import app_harness


def _turn(text, start, stop, speaker=1):
    return RecognizedTurn(start, stop, text, np.ones(4, dtype=np.float32), True)


def test_cancel_keeps_only_segments_finished_before_the_request():
    chunks = [
        AudioChunk(0, 16_000, np.ones(16_000, dtype=np.float32)),
        AudioChunk(16_000, 32_000, np.ones(16_000, dtype=np.float32)),
        AudioChunk(32_000, 48_000, np.ones(16_000, dtype=np.float32)),
    ]
    seen = {"n": 0}

    def decode(chunk):
        seen["n"] += 1
        return [_turn(f"第{seen['n']}段", 0, len(chunk.audio))]

    def cancel():
        return seen["n"] >= 1

    result = recognize_with_session(
        chunks, decode, session=object(), threshold=0.5, cancel=cancel,
        finalize=lambda _session, _threshold: {0: 2},
    )
    assert result.cancelled
    assert [(turn.speaker_id, turn.offset_ms, turn.duration_ms, turn.text) for turn in result.turns] == [
        (2, 0, 1000, "第1段"),
    ]
    assert "embedding" not in {field.name for field in dataclasses.fields(result.turns[0])}


def test_energy_segmentation_keeps_sample_offsets():
    audio = np.zeros(TARGET_SAMPLE_RATE * 2, dtype=np.float32)
    audio[8_000:24_000] = 0.2
    chunks = segment_pcm(audio, vad="energy", vad_path=None)
    assert chunks and chunks[0].stop > chunks[0].start
    assert np.array_equal(chunks[0].audio, audio[chunks[0].start:chunks[0].stop])


def test_recognize_file_keeps_a_partial_result_and_does_not_copy_the_source(tmp_path, monkeypatch):
    source = tmp_path / "talk.mp4"
    source.write_bytes(b"original-bytes")
    chunks = [
        AudioChunk(0, 16_000, np.ones(16_000, dtype=np.float32)),
        AudioChunk(16_000, 32_000, np.ones(16_000, dtype=np.float32)),
    ]
    monkeypatch.setattr(
        "sayink.media_import.iter_media_pcm_timed",
        lambda path, *, cancel=None, progress_cb=None: iter([(np.ones(32_000, dtype=np.float32), 0, 0)]),
    )
    monkeypatch.setattr("sayink.media_import.segment_timed_blocks", lambda *args, **kwargs: (chunks, 32_000))
    seen = {"n": 0}

    class Recognizer:
        def decode_meeting_blocking(self, audio, *, turn_analyzer, cancelled):
            seen["n"] += 1
            return [_turn(f"第{seen['n']}段", 0, len(audio))], ""

    result = recognize_file(
        source, recognizer=Recognizer(), speaker_path=None, vad="energy", vad_path=None,
        cancel=lambda: seen["n"] >= 1, progress=lambda _text: None,
    )
    assert result.cancelled and [turn.text for turn in result.turns] == ["第1段"]
    assert source.read_bytes() == b"original-bytes"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["talk.mp4"]
    root = Path(__file__).resolve().parents[1]
    text = (root / "sayink" / "media_import.py").read_text(encoding="utf-8").lower()
    foundation = (root / "sayink" / "media_foundation.py").read_text(encoding="utf-8").lower()
    assert "ffmpeg" not in text and "shutil" not in text
    assert "ffmpeg" not in foundation


def test_decode_meeting_blocking_stays_on_the_caller_thread():
    from sayink.speaker_turns import SpeakerTurn
    from sayink.speech_recognizer import SpeechRecognizer

    class Stream:
        def accept_waveform(self, rate, audio):
            self.rate = rate

        class result:
            text = "你好"

        result = result()

    class Native:
        def create_stream(self):
            return Stream()

        def decode_stream(self, stream):
            return None

    recognizer = SpeechRecognizer()
    recognizer._recognizer = Native()
    recognizer._is_ready = True
    audio = np.ones(TARGET_SAMPLE_RATE, dtype=np.float32)

    def analyzer(clip, cancelled=None):
        return [SpeakerTurn(0, len(clip), np.zeros(1, dtype=np.float32), False)]

    reported: list[tuple[int, int]] = []
    turns, error = recognizer.decode_meeting_blocking(
        audio,
        turn_analyzer=analyzer,
        cancelled=lambda: False,
        on_progress=lambda done, total: reported.append((done, total)),
    )
    assert error == "" and turns and turns[0].text == "你好"
    assert reported and reported[0] == (0, len(audio))
    assert reported[-1] == (len(audio), len(audio))
    recognizer.deleteLater()


def test_file_dialog_lists_the_decodable_extensions():
    filters = file_dialog_filter()
    for ext in (".wav", ".mp3", ".m4a", ".mp4", ".mkv", ".flac"):
        assert f"*{ext}" in filters


def test_store_writes_media_times_and_keeps_them_after_history_cleanup(tmp_path):
    history = HistoryStore(db_path=tmp_path / "history.db")
    meetings = MeetingStore(history)
    try:
        turns = (
            MediaTurn(1, 300, 4_000, "内地的评论"),
            MediaTurn(2, 4_500, 1_500, "我没有想过"),
        )
        record = meetings.create_meeting_from_media(
            turns, title="访谈", status=STATUS_INTERRUPTED, audio_ms=20_000,
        )
        assert record is not None and meetings.flush()
        saved = meetings.get_meeting(record.id)
        assert saved.source == MEDIA_SOURCE and saved.status == STATUS_INTERRUPTED
        assert saved.ended_at == saved.started_at + 6_000
        segments = meetings.get_segments(record.id)
        assert [segment.raw_text for segment in segments] == ["内地的评论", "我没有想过"]
        assert segments[0].captured_at == saved.started_at + 300
        assert segments[0].duration_ms == 4_000 and segments[0].speaker_id == 1
        assert all(segment.source == MEDIA_SOURCE and segment.trigger_mode == "meeting" for segment in segments)
        assert meetings.get_speakers(record.id) == []
        complete = meetings.create_meeting_from_media(
            (MediaTurn(1, 0, 1_000, "完整"),), title="完整", status=STATUS_ENDED, audio_ms=5_000,
        )
        assert complete is not None and meetings.flush()
        assert meetings.get_meeting(complete.id).duration_ms == 5_000
        history.enqueue_delete_all()
        assert history.flush()
        assert [segment.raw_text for segment in meetings.get_segments(record.id)] == ["内地的评论", "我没有想过"]
    finally:
        history.close(timeout=2)


def test_recognition_percent_ignores_leading_silence():
    from sayink.media_import import _recognition_percent

    assert _recognition_percent(1, 0.0, 4) == 30
    assert _recognition_percent(1, 0.0, 4) < 50
    assert _recognition_percent(1, 1.0, 4) < _recognition_percent(2, 0.0, 4) or (
        _recognition_percent(1, 1.0, 4) == _recognition_percent(2, 0.0, 4)
    )
    assert _recognition_percent(4, 1.0, 4) == 99


def _controller(config, tmp_path):
    config.set("llm.api_url", "http://localhost:11434/v1")
    config.set("llm.model_name", "local-model")
    config.set("meeting.auto_generate", True)
    history = HistoryStore(db_path=tmp_path / "history.db")
    return MeetingController(config, history, pending_segments=lambda: 0), history


def test_controller_saves_a_cancelled_import_without_minutes(config, tmp_path):
    controller, history = _controller(config, tmp_path)
    try:
        assert "不支持" in controller.start_media_import(str(tmp_path / "notes.docx"))
        assert not controller.importing_media
        controller.set_external_block(lambda: "正在转写，请完成后再导入音视频")
        assert controller.begin_media_import() == "正在转写，请完成后再导入音视频"
        assert not controller.importing_media
        controller.set_external_block(lambda: "")
        assert controller.begin_media_import() == ""
        assert controller.importing_media
        assert "正在导入音视频" in controller.begin_media_import()
        controller.cancel_media_import()
        saved = controller.commit_media_import(RecognitionResult(
            (MediaTurn(1, 0, 800, "已经听到"),), cancelled=True,
        ), title="访谈")
        assert saved is not None and saved.status == STATUS_INTERRUPTED
        assert not controller.importing_media
        assert controller.store.queued_generation(saved.id) is None
        assert controller.commit_media_import(RecognitionResult((), cancelled=True), title="空") is None
    finally:
        controller.shutdown()
        history.close(timeout=2)


def test_a_live_meeting_refuses_media_import(config, tmp_path):
    controller, history = _controller(config, tmp_path)
    try:
        controller.begin(MeetingOptions(title="现场"), MeetingCapabilities(
            model_id="funasr-nano", hotwords="", hotwords_supported=False, language="zh",
            language_supported=True, vad="energy", speaker_model_available=False,
        ))
        assert controller.media_block_reason() == "已有会议在进行中"
    finally:
        controller.shutdown()
        history.close(timeout=2)


def _wait_until(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        QCoreApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_meeting_page_imports_a_file_and_cancel_keeps_the_partial_transcript(config, tmp_path, monkeypatch):
    controller, history = _controller(config, tmp_path)
    page = MeetingPage(config, controller)
    try:
        source = tmp_path / "clip.mp4"
        source.write_bytes(b"video")

        def runner(path, cancel, progress):
            assert Path(path) == source
            progress("正在识别 1/3 段")
            while not cancel():
                time.sleep(0.02)
            return RecognitionResult((MediaTurn(1, 0, 800, "已经听到"),), cancelled=True)

        controller.set_media_runner(runner)
        monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args, **kwargs: (str(source), ""))
        previous = controller.store.create_meeting(title="上一场")
        controller.store.end_meeting(previous.id, ended_at=previous.started_at + 1000)
        history.enqueue(SegmentRecord(
            previous.id, 1, previous.started_at, raw_text="上一场的发言", trigger_mode="meeting",
            captured_at=previous.started_at,
        ))
        assert controller.store.flush()
        page.show_meeting(previous.id)
        assert "上一场的发言" in page._transcript_view.toPlainText()
        button = next(item for item in page.findChildren(QPushButton) if item.accessibleName() == "导入音视频")
        button.click()
        cancel = next(item for item in page.findChildren(QPushButton) if item.accessibleName() == "取消导入音视频")
        assert not cancel.isHidden()
        assert not page._start_btn.isEnabled()
        assert page._import_filename.text() == "clip" or page._import_filename.toolTip() == "clip"
        assert page._content_stack.isHidden()
        assert _wait_until(lambda: "正在识别" in page._import_progress.text() and "段" not in page._import_progress.text())
        assert not page._import_progress.isHidden()
        cancel.click()
        assert _wait_until(lambda: not controller.importing_media)
        record = controller.store.get_meeting(page._detail_id)
        assert record.source == MEDIA_SOURCE and record.status == STATUS_INTERRUPTED
        assert "已经听到" in page._transcript_view.toPlainText()
        assert "导入音视频" in page._detail_meta.text() and "已中断" in page._detail_meta.text()
        assert controller.store.queued_generation(record.id) is None
        assert cancel.isHidden() and page._start_btn.isEnabled()
    finally:
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_explicit_job_percent_wins_over_segment_fraction(config, tmp_path):
    """「1/6 段（45%）」里的 45 是整段导入进度，不能再按 1/6 画成 17%。"""
    controller, history = _controller(config, tmp_path)
    page = MeetingPage(config, controller)
    try:
        controller._importing_media = True
        page._on_media_progress("正在识别 1/6 段（45%）")
        assert page._import_progress_bar.value() == 45
        assert "45%" in page._import_progress.text()
        assert "段" not in page._import_progress.text()
        # 没有显式百分比时，仍按段数比例显示。
        page._on_media_progress("正在识别第 3 / 10 段")
        assert page._import_progress_bar.value() == 30
        assert "30%" in page._import_progress.text()
    finally:
        controller._importing_media = False
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_finished_read_does_not_stay_on_thirty_percent_while_recognizing(tmp_path, monkeypatch):
    """文件读完后不能停在「已读取 …（30%）」；识别一开始就要换成识别进度，并随音频往前走。"""
    source = tmp_path / "talk.mp4"
    source.write_bytes(b"original")
    chunks = [
        AudioChunk(0, 16_000, np.ones(16_000, dtype=np.float32)),
        AudioChunk(16_000, 32_000, np.ones(16_000, dtype=np.float32)),
    ]
    monkeypatch.setattr(
        "sayink.media_import.iter_media_pcm_timed",
        lambda path, *, cancel=None, progress_cb=None: iter([(np.ones(32_000, dtype=np.float32), 0, 0)]),
    )
    monkeypatch.setattr(
        "sayink.media_import.segment_timed_blocks",
        lambda *args, **kwargs: (chunks, 32_000),
    )
    seen: list[tuple[int, str]] = []

    class Recognizer:
        def decode_meeting_blocking(self, audio, *, turn_analyzer, cancelled, on_progress=None):
            recog = [(pct, msg) for pct, msg in seen if "正在识别" in msg]
            assert recog, seen
            assert not any(msg.startswith("已读取") for _, msg in seen), seen
            assert recog[0][0] >= 30
            if on_progress is not None:
                before = len(seen)
                on_progress(max(1, len(audio) // 2), len(audio))
                moved = [pct for pct, msg in seen[before:] if "正在识别" in msg and pct > recog[0][0]]
                assert moved, seen
            return [_turn("一段", 0, len(audio))], ""

    result = recognize_file(
        source,
        recognizer=Recognizer(),
        speaker_path=None,
        vad="energy",
        vad_path=None,
        cancel=lambda: False,
        progress_cb=lambda pct, msg: seen.append((int(pct), msg)),
    )
    assert [turn.text for turn in result.turns] == ["一段", "一段"]
    percents = [pct for pct, _ in seen]
    assert percents[0] == 0 and percents[-1] == 100
    assert all(earlier <= later for earlier, later in zip(percents, percents[1:], strict=False))
    assert any(pct > 30 for pct, msg in seen if "正在识别" in msg)


def test_empty_library_keeps_import_progress_visible(config, tmp_path):
    controller, history = _controller(config, tmp_path)
    page = MeetingPage(config, controller)
    try:
        page.refresh()
        assert page._list.count() == 0
        assert page._detail_pane.isHidden()
        controller._importing_media = True
        page._media_import_title = "demo.wav"
        page._media_progress_text = "正在识别 1/10 段"
        page.refresh()
        assert not page._detail_pane.isHidden()
        assert not page._import_progress.isHidden()
        assert "正在识别" in page._import_progress.text()
        assert "段" not in page._import_progress.text()
        assert "demo.wav" in page._import_filename.toolTip()
    finally:
        controller._importing_media = False
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_cancelling_before_any_text_does_not_say_the_file_was_empty(config, tmp_path):
    controller, history = _controller(config, tmp_path)
    page = MeetingPage(config, controller)
    try:
        assert controller.begin_media_import() == ""
        controller._on_media_job_done(RecognitionResult((), cancelled=True))
        assert page._status_label.text() == "已取消导入。"
        assert "没有识别到文字" not in page._status_label.text()
    finally:
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_imported_media_applies_glossary_corrections(monkeypatch):
    from sayink.media_import import recognize_file

    monkeypatch.setattr(
        "sayink.media_import.iter_media_pcm_timed",
        lambda path, *, cancel=None, progress_cb=None: iter([(np.ones(1600, dtype=np.float32), 0, 0)]),
    )
    monkeypatch.setattr(
        "sayink.media_import.segment_timed_blocks",
        lambda *args, **kwargs: ([AudioChunk(0, 1600, np.ones(1600, dtype=np.float32))], 1600),
    )

    class Recognizer:
        def decode_meeting_blocking(self, audio, *, turn_analyzer, cancelled):
            return [_turn("五中齐全", 0, len(audio))], ""

    result = recognize_file(
        "clip.wav", recognizer=Recognizer(), speaker_path=None, vad="energy", vad_path=None,
        cancel=lambda: False, progress=lambda _text: None,
        glossary=[{"term": "五证", "variants": ["五中"]}],
    )
    assert result.turns[0].text == "五证齐全"


def test_cancel_before_reading_does_not_open_the_file(monkeypatch):
    from sayink.media_import import recognize_file

    def opened(*_args, **_kwargs):
        raise AssertionError("取消后不应再读取文件")

    monkeypatch.setattr("sayink.media_import.iter_media_pcm_timed", opened)
    result = recognize_file(
        "clip.wav", recognizer=object(), speaker_path=None, vad="energy", vad_path=None,
        cancel=lambda: True, progress=lambda _text: None,
    )
    assert result.cancelled and result.turns == ()


def test_media_import_blocks_dictation_meeting_and_recovery():
    """M-UX-05: Live meetings, mic test, and audio recovery stay blocked during media import,
    while hotkey/continuous dictation and recognition reload deferral coexist cleanly."""
    with app_harness({"audio.trigger_mode": "hotkey"}) as harness:
        app = harness["app"]
        assert app.meeting.begin_media_import() == ""
        assert app.start_meeting(MeetingOptions()) is False
        assert "导入音视频" in harness["floating"].show_error.call_args[0][0]
        harness["recorder"].start.reset_mock()
        harness["floating"].show_error.reset_mock()
        app._on_recording_start()
        harness["recorder"].start.assert_called_once_with(continuous=False)
        assert not app._pipeline_busy()
        assert app._reload_coordinator().pipeline_busy() is True

    with app_harness({"audio.trigger_mode": "continuous"}) as harness:
        app = harness["app"]
        app.meeting.begin_media_import()
        from unittest.mock import patch
        with patch("sayink.ui.audio_recovery_dialog.AudioRecoveryDialog") as dialog:
            app._show_audio_recovery()
            dialog.assert_not_called()
        cleared = []
        app._clear_live_transcript = lambda: cleared.append(True)
        app._on_continuous_hotkey_start()
        assert cleared == [True]
        assert app._continuous_start_pending is True
        app._stop_continuous_listening()
        harness["floating"].show_error.reset_mock()
        opened = []
        app._show_main_window = lambda *_args, **_kwargs: opened.append(True)
        app._start_meeting_from_tray()
        assert opened == []
        assert "导入音视频" in harness["floating"].show_error.call_args[0][0]
        assert app._recording_blocks_mic_test() is True

    with app_harness({"audio.trigger_mode": "hotkey"}) as harness:
        app = harness["app"]
        harness["recorder"].is_recording = True
        assert "转写" in app._media_import_conflict()
        assert app.meeting.begin_media_import() != ""
        assert app.meeting.importing_media is False
        assert not hasattr(type(app), "start_file_transcription")
        assert not hasattr(type(app), "cancel_file_transcription")


def test_a_later_chunk_failure_keeps_the_earlier_text():
    from types import SimpleNamespace

    chunks = [
        AudioChunk(0, 1600, np.zeros(1600, dtype=np.float32)),
        AudioChunk(1600, 3200, np.zeros(1600, dtype=np.float32)),
    ]

    def decode(chunk):
        if chunk.start:
            raise RuntimeError("第二段失败")
        return [SimpleNamespace(text="第一段", start=0, stop=1600, embedding=None, learnable=False)]

    result = recognize_with_session(
        chunks, decode, SimpleNamespace(assign=None), threshold=0.5, cancel=lambda: False,
    )
    assert result.cancelled is False
    assert result.error == "第二段失败"
    assert [turn.text for turn in result.turns] == ["第一段"]


def test_wav_read_stops_when_cancel_is_set(tmp_path):
    import wave

    from sayink.audio_file_decoder import AudioDecodeError, decode_audio_file

    path = tmp_path / "long.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 16000 * 3)
    calls = {"n": 0}

    def cancel():
        calls["n"] += 1
        return calls["n"] >= 2

    with pytest.raises(AudioDecodeError, match="已取消"):
        decode_audio_file(path, cancel=cancel)
    assert calls["n"] == 2


def test_write_failure_is_not_called_an_empty_transcript(config, tmp_path):
    controller, history = _controller(config, tmp_path)
    try:
        result = RecognitionResult((MediaTurn(1, 0, 1000, "有文字"),), False, audio_ms=1000)
        controller._media_title = "导入"
        controller._store.flush = lambda timeout=10: False
        controller._on_media_job_done(result)
        assert controller.media_import_notice != "没有识别到文字"
        assert "数据库" in controller.media_import_notice
        assert controller._unsaved_media is not None
    finally:
        controller.shutdown()
        history.close(timeout=2)


def test_destroying_the_controller_stops_the_import_thread(config, tmp_path):
    import threading

    from PyQt6.QtCore import QEvent
    from PyQt6.QtWidgets import QApplication

    release = threading.Event()
    controller, history = _controller(config, tmp_path)
    controller.set_media_runner(lambda *_args: release.wait(30))
    try:
        assert controller.start_media_import("synthetic.wav") == ""
        deadline = time.perf_counter() + 2
        app = QApplication.instance()
        while not (controller._media_job and controller._media_job.isRunning()) and time.perf_counter() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert controller._media_job in controller.running_workers
        controller.shutdown(wait_ms=0)
        assert controller._media_job is not None and controller._media_job.isRunning()
        job = controller._media_job
        controller.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        try:
            job.previewed.disconnect()
        except TypeError:
            pass
        else:
            raise AssertionError("import preview stayed connected after the controller was destroyed")
    finally:
        release.set()
        from sayink.meeting_session import _ORPHAN_MEDIA_JOBS
        for job in list(_ORPHAN_MEDIA_JOBS):
            job.wait(2000)
        history.close(timeout=2)


def test_recognize_file_streams_non_wav_with_delayed_origin_and_pts_gap(tmp_path, monkeypatch):
    from sayink.audio_file_decoder import TimedAudioBlock

    source = tmp_path / "delayed_gap.mp4"
    source.write_bytes(b"mp4-stub")

    def _forbid_full_decode(*_args, **_kwargs):
        raise AssertionError("recognize_file must stream blocks without calling decode_audio_file")

    monkeypatch.setattr("sayink.audio_file_decoder.decode_audio_file", _forbid_full_decode)

    closed: list[bool] = []
    tone_samples = int(TARGET_SAMPLE_RATE * 1.5)  # 24_000 samples (1.5s)
    lead_samples = int(TARGET_SAMPLE_RATE * 600.0)  # 600s leading audio track delay!
    gap_samples = int(TARGET_SAMPLE_RATE * 12.0)  # 12s mid-stream PTS silence jump!
    second_start = lead_samples + tone_samples + gap_samples  # 613.5s
    tone = (0.25 * np.sin(2.0 * np.pi * 440.0 * np.arange(tone_samples) / TARGET_SAMPLE_RATE)).astype(
        np.float32
    )

    def _fake_timed_stream(path, *, cancel=None, progress_cb=None):
        assert Path(path) == source
        try:
            if progress_cb is not None:
                progress_cb(50, "正在解码音频（2 秒）")
            yield TimedAudioBlock(tone, lead_samples, lead_samples * 1000 // TARGET_SAMPLE_RATE)
            yield TimedAudioBlock(tone, second_start, second_start * 1000 // TARGET_SAMPLE_RATE)
            if progress_cb is not None:
                progress_cb(100, "已完成解码（3 秒）")
        finally:
            closed.append(True)

    monkeypatch.setattr("sayink.media_import.iter_media_pcm_timed", _fake_timed_stream)

    seen_idx = {"n": 0}

    class _Recognizer:
        def decode_meeting_blocking(self, audio, *, turn_analyzer, cancelled):
            seen_idx["n"] += 1
            return [_turn(f"片段{seen_idx['n']}", 0, len(audio))], ""

    pcts: list[int] = []
    result = recognize_file(
        source,
        recognizer=_Recognizer(),
        speaker_path=None,
        vad="energy",
        vad_path=None,
        cancel=lambda: False,
        progress_cb=lambda pct, _msg: pcts.append(pct),
    )
    assert closed == [True]
    assert not result.cancelled
    assert [t.text for t in result.turns] == ["片段1", "片段2"]
    assert result.turns[0].offset_ms == 600_000
    assert result.turns[0].duration_ms == 1_500
    assert result.turns[1].offset_ms == 613_500
    assert result.turns[1].duration_ms == 1_500
    assert result.audio_ms == 615_000
    assert pcts[0] == 0 and 30 in pcts and pcts[-1] == 100


def test_media_import_dialog_natural_sort_reorder_and_duplicate_hint(config, tmp_path, qapp):
    """M-UX-02 & M-ORD-03: MediaImportDialog naturally sorts filenames, flags duplicates, and supports reordering."""
    from sayink.ui.media_import_dialog import MediaImportDialog

    p10 = tmp_path / "part_10.mp3"
    p2 = tmp_path / "part_2.mp3"
    p1 = tmp_path / "part_1.mp3"
    for p in (p10, p2, p1):
        p.write_bytes(b"audio")

    dlg = MediaImportDialog(
        [p10, p2, p1],
        config=config,
        existing_audio_paths={str(p2)},
    )
    try:
        assert [p.name for p in dlg.paths()] == ["part_1.mp3", "part_2.mp3", "part_10.mp3"]
        assert not dlg._dup_hint.isHidden()
        assert "1 个文件此前已导入过" in dlg._dup_hint.text()
        # Move part_10 up above part_2
        dlg._list.setCurrentRow(2)
        dlg._move_current(-1)
        assert [p.name for p in dlg.paths()] == ["part_1.mp3", "part_10.mp3", "part_2.mp3"]
        # Remove part_2 -> duplicate hint disappears
        dlg._list.setCurrentRow(2)
        dlg._remove_current()
        assert [p.name for p in dlg.paths()] == ["part_1.mp3", "part_10.mp3"]
        assert dlg._dup_hint.isHidden()

        dlg._lang_combo.setCurrentIndex(dlg._lang_combo.findData("en"))
        reqs = dlg.import_requests()
        assert [r[1] for r in reqs] == ["part_1", "part_10"]
        assert reqs[0][2]["language"] == "en"
        assert reqs[1][2]["started_at"] > reqs[0][2]["started_at"]
    finally:
        dlg.deleteLater()


def test_batch_queue_preserves_audio_paths_detail_watching_and_skip_vs_cancel_all(config, tmp_path, qapp):
    """M-ORD-02 & M-UX-07: Queued batch jobs bind each file's own audio_path, keep watching detail until the end,
    and support skip-current vs cancel-all."""
    from PyQt6.QtWidgets import QPushButton
    from unittest.mock import patch
    from sayink.ui.meeting_page import MeetingPage

    controller, history = _controller(config, tmp_path)
    page = MeetingPage(config, controller)
    try:
        gates = {name: threading.Event() for name in ("part_1.wav", "part_2.wav", "part_3.wav")}
        started_events = {name: threading.Event() for name in ("part_1.wav", "part_2.wav", "part_3.wav")}

        def runner(path, cancel, progress):
            name = Path(path).name
            started_events[name].set()
            progress("正在识别 1/2 段（60%）")
            gates[name].wait(3.0)
            if cancel():
                return RecognitionResult((), cancelled=True, audio_path=str(path))
            return RecognitionResult(
                (MediaTurn(1, 0, 1000, f"内容-{Path(path).stem}"),),
                cancelled=False,
                audio_path=str(path),
            )

        controller.set_media_runner(runner)
        f1 = tmp_path / "part_1.wav"
        f2 = tmp_path / "part_2.wav"
        f3 = tmp_path / "part_3.wav"
        for f in (f1, f2, f3):
            f.write_bytes(b"RIFF")

        # Natural sort check via _begin_media_paths([f3, f1, f2]) -> part_1, part_2, part_3
        assert page._begin_media_paths([f3, f1, f2]) == ""
        assert _wait_until(lambda: started_events["part_1.wav"].is_set())
        qapp.processEvents()
        assert page._import_owns_detail is True

        # Finish part_1 -> part_2 starts automatically; page stays watching import detail!
        gates["part_1.wav"].set()
        assert _wait_until(lambda: started_events["part_2.wav"].is_set())
        assert _wait_until(lambda: page._media_import_title == "part_2")
        assert page._import_owns_detail is True

        # Skip only current file (part_2) while part_3 remains queued!
        cancel_btn = next(b for b in page.findChildren(QPushButton) if b.accessibleName() == "取消导入音视频")
        with patch("sayink.ui.meeting_page.QMessageBox") as box_cls:
            stay_btn, skip_btn, stop_all_btn = object(), object(), object()
            box_cls.return_value.addButton.side_effect = [stay_btn, skip_btn, stop_all_btn]
            box_cls.return_value.clickedButton.return_value = skip_btn
            cancel_btn.click()
        gates["part_2.wav"].set()
        assert _wait_until(lambda: started_events["part_3.wav"].is_set())

        # Finish part_3 -> queue drains and page opens part_3 with exact per-meeting audio_path bindings!
        gates["part_3.wav"].set()
        assert _wait_until(lambda: not controller.importing_media and not controller.queued_media())
        qapp.processEvents()
        records = {r.title: r for r in controller.store.list_meetings()}
        assert set(records.keys()) == {"part_1", "part_3"}
        assert page._meeting_audio_paths[records["part_1"].id] == str(f1)
        assert page._meeting_audio_paths[records["part_3"].id] == str(f3)
        assert page.current_meeting_id == records["part_3"].id
    finally:
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_batch_media_import_reuses_dedicated_recognizer_until_queue_drains(tmp_path, monkeypatch):
    """M-PERF-02: Dedicated import recognizer loads once across a multi-file queue and releases on drain."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from sayink.speech_recognizer import SpeechRecognizer

    f1 = tmp_path / "a.wav"
    f2 = tmp_path / "b.wav"
    f1.write_bytes(b"RIFF")
    f2.write_bytes(b"RIFF")

    with app_harness() as harness:
        app = harness["app"]
        monkeypatch.setattr("sayink.app.SpeechRecognizer", SpeechRecognizer)

        class _Resident(SpeechRecognizer):
            current_model_id = "sense-voice"
            is_ready = True

            def __init__(self) -> None:
                super().__init__()
                self._recognizer = object()
                self._decode_lock = MagicMock()

        resident = _Resident()
        try:
            app._recognizer = resident
            app._resolve_media_import_model_id = lambda: "funasr-nano"
            monkeypatch.setattr("sayink.speech_recognizer.has_ram_for_warm_swap", lambda _m: True)
            created: list[str] = []
            monkeypatch.setattr(
                "sayink.speech_recognizer._create_recognizer",
                lambda mid, *_a, **_kw: created.append(mid) or object(),
            )
            monkeypatch.setattr(
                "sayink.media_import.recognize_file",
                lambda _p, **_kw: SimpleNamespace(segments=[], speaker_count=0, error=""),
            )

            # Simulate file 1 running while file 2 is still waiting in _media_queue
            app._meeting._media_queue.append((str(f2), "b"))
            app._run_media_import(f1, lambda: False, None)
            assert created == ["funasr-nano"]
            assert app._cached_media_import_recognizer is not None

            # Simulate file 2 popped and running as the last item in queue
            app._meeting._media_queue.clear()
            app._run_media_import(f2, lambda: False, None)
            assert created == ["funasr-nano"]  # Reused without a 2nd cold model load!
            assert app._cached_media_import_recognizer is None  # Released immediately when queue drained!
        finally:
            resident.deleteLater()

"""Protect original content and overlap recognition with ordered output."""
from unittest.mock import patch

import numpy as np
import pytest

from sayink.speech_recognizer import SpeechRecognizer, TranscribeWorker, normalize_asr_output
from sayink.vad_segmenter import SpeechGate, SpeechSegmenter, make_meeting_segmenter
from tests.helpers.app_harness import app_harness
from tests.test_speech_recognizer import _FakeRecognizer

RATE = 16000
LLM = {"audio.trigger_mode": "continuous", "llm.enabled": True,
       "llm.api_url": "https://example.test/v1", "llm.api_key": "test", "llm.model_name": "test"}


class SequenceRecognizer:
    def __init__(self, texts):
        self.texts = iter(texts)
        self.audio = []

    def create_stream(self):
        from types import SimpleNamespace
        return SimpleNamespace(result=SimpleNamespace(text=next(self.texts)),
                               accept_waveform=lambda _rate, audio: self.audio.append(audio))

    def decode_stream(self, stream):
        if isinstance(stream.result.text, Exception):
            raise stream.result.text


def test_overflow_retry_uses_all_audio_and_an_acoustic_candidate():
    from sayink.speech_recognizer import decode_asr_slice

    audio = np.linspace(0.02, 0.1, 2 * RATE, dtype=np.float32)
    rec = SequenceRecognizer(["那个车，" * 40, "今天那个车", "那个车已经卖了"])
    assert decode_asr_slice(rec, audio) == "今天那个车已经卖了"
    assert len(rec.audio) == 3
    np.testing.assert_array_equal(rec.audio[1], audio[:19200])
    np.testing.assert_array_equal(rec.audio[2], audio[12800:])


@pytest.mark.parametrize("text", ["请注意。" * 8, "对，" * 16, " ".join(["8888"] * 100)])
def test_realistic_repeats_and_numbers_do_not_trigger_extra_decodes(text):
    from sayink.speech_recognizer import decode_asr_slice

    rec = SequenceRecognizer([text])
    assert decode_asr_slice(rec, np.ones(15 * RATE, np.float32)) == text
    assert len(rec.audio) == 1


@pytest.mark.parametrize("retry", [["", ""], [RuntimeError("native failure")],
                                   ["很多字" * 80, "很多字" * 80]])
def test_unsuccessful_overflow_retry_preserves_first_result(retry):
    from sayink.speech_recognizer import decode_asr_slice

    original = "那个车，" * 40
    rec = SequenceRecognizer([original, *retry])
    assert decode_asr_slice(rec, np.ones(2 * RATE, np.float32)) == original
    assert len(rec.audio) <= 3


def test_cancelled_overflow_does_not_start_another_native_decode():
    from sayink.speech_recognizer import decode_asr_slice

    original = "那个车，" * 40
    rec = SequenceRecognizer([original])
    assert decode_asr_slice(rec, np.ones(2 * RATE, np.float32), cancelled=lambda: True) == original
    assert len(rec.audio) == 1


@pytest.mark.parametrize("mode", ["single", "continuous", "meeting"])
@pytest.mark.parametrize("seconds", [15.0, 15.01, 15.1, 15.2, 30.01, 1800.1, 7200.1])
def test_continuous_speech_has_no_missing_or_duplicated_samples(mode, seconds):
    seg = make_meeting_segmenter() if mode == "meeting" else SpeechSegmenter()
    seg._adaptive = False  # Isolate splitting from the synthetic signal's noise floor.
    rng = np.random.default_rng(10)
    total = round(RATE * seconds)
    cursor = 0

    def check(out):
        nonlocal cursor
        if out is not None:
            assert seg.last_bounds == (cursor, cursor + len(out))
            assert len(out) > 0
            assert len(out) <= 15 * RATE
            cursor += len(out)

    for start in range(0, total, 1600):
        block = rng.uniform(0.04, 0.06, min(1600, total - start)).astype(np.float32)
        check(seg.feed(block))
    check(seg.flush())
    assert cursor == total


def test_immediate_tail_is_kept_even_if_neural_gate_has_not_reopened():
    class Gate(SpeechGate):
        calls = 0

        def is_speech(self, *_args, **_kwargs):
            self.calls += 1
            return self.calls <= 150

    seg = SpeechSegmenter(adaptive=False, gate=Gate())
    for _ in range(150):
        out = seg.feed(np.full(1600, 0.1, np.float32))
    assert out.size == 15 * RATE
    tail = np.full(160, 0.1, np.float32)
    assert seg.feed(tail) is None
    np.testing.assert_array_equal(seg.flush(), tail)
    assert seg.last_bounds == (15 * RATE, 15 * RATE + 160)
    assert seg.flush() is None


def test_continuation_protection_expires_after_a_real_pause():
    seg = SpeechSegmenter(adaptive=False)
    for _ in range(150):
        seg.feed(np.full(1600, 0.1, np.float32))
    for _ in range(20):
        seg.feed(np.zeros(1600, np.float32))
    seg.feed(np.full(1600, 0.1, np.float32))
    assert seg.flush() is None  # Independent 100 ms click still rejected.


@pytest.mark.parametrize("mode", ["hotkey", "continuous"])
def test_app_passes_validated_sub_100ms_tail_to_recognizer(mode):
    with app_harness({"audio.trigger_mode": mode}) as h:
        tail = np.full(160, 0.1, np.float32)
        h["app"]._on_segment_ready(tail)
        assert h["recognizer"].transcribe_final.call_args.args[0] is tail


@pytest.mark.parametrize("spoken", [
    "请注意。" * 8 + "下一项。", "哈" * 30, "对，" * 10 + "就这样。",
    "然后你这个月又有点长了，" * 12, "我们出发吧" + "神の" * 20,
    "我来说中文。こんにちは。继续中文。", "中文。안녕하세요 만나서 반갑습니다.再继续。",
    "مرحبا بالعالم", "Привет, мир!", "नमस्ते दुनिया",
])
def test_normalization_preserves_repetition_and_mixed_languages(spoken):
    assert normalize_asr_output(spoken) == spoken
    assert normalize_asr_output("<asr_text>" + spoken + "</asr_text>") == spoken
    assert normalize_asr_output(normalize_asr_output(spoken)) == spoken


def _two_segments(h):
    app = h["app"]
    h["recognizer"].supports_prefetch = True
    h["recorder"].is_continuous = True
    audio = np.full(4800, 0.1, np.float32)
    app._on_segment_ready(audio)
    app._on_segment_ready(audio.copy())
    app._on_final_result("第一句")
    app._pump_segment_queue()
    return app, h["recognizer"].prefetch_final.call_args.kwargs


def _finish_first_output(h):
    h["app"]._on_polish_complete("第一句")
    h["paster"].paste_async.call_args.args[1]("sent")
    h["app"]._pump_segment_queue()


@pytest.mark.parametrize("arrival", ["during-asr", "during-polish"])
def test_event_loop_starts_prefetch_before_polish_completes(arrival):
    from PyQt6.QtCore import QEventLoop, QTimer

    with app_harness(LLM) as h:
        app = h["app"]
        h["recognizer"].supports_prefetch = True
        h["recorder"].is_continuous = True
        audio = np.full(4800, 0.1, np.float32)
        loop = QEventLoop()
        h["recognizer"].prefetch_final.side_effect = lambda *_args, **_kwargs: loop.quit()
        app._on_segment_ready(audio)
        if arrival == "during-asr":
            app._on_segment_ready(audio.copy())
        app._on_final_result("第一句")
        if arrival == "during-polish":
            app._on_segment_ready(audio.copy())
        timeout = QTimer()
        timeout.setSingleShot(True)
        timeout.timeout.connect(loop.quit)
        timeout.start(1000)
        loop.exec()
        timeout.stop()
        h["recognizer"].prefetch_final.assert_called_once()
        h["paster"].paste_async.assert_not_called()
        assert app._pending_record.raw_text == "第一句"
        app._clear_queued_audio()


def test_prefetch_finishes_during_polish_without_changing_current_output():
    with app_harness(LLM) as h, patch("sayink.app.QTimer.singleShot"):
        app, callbacks = _two_segments(h)
        session = app._pending_record.session_id
        callbacks["on_text"]("第二句")
        assert app._pending_record.raw_text == "第一句"
        assert app._output_raw_text == "第一句"
        assert len(app._segment_queue) == 1
        h["paster"].paste_async.assert_not_called()
        _finish_first_output(h)
        assert h["recognizer"].transcribe_final.call_count == 1
        assert h["polisher"].polish.call_args.args[0] == "第二句"
        app._on_polish_error("offline")
        h["paster"].paste_async.call_args.args[1]("sent")
        records = [c.args[0] for c in h["history"].enqueue.call_args_list]
        assert [r.raw_text for r in records] == ["第一句", "第二句"]
        assert [r.seq for r in records] == [0, 1]
        assert {r.session_id for r in records} == {session}
        assert [c.args[0] for c in h["paster"].paste_async.call_args_list] == ["第一句", "第二句"]


def test_output_finishing_first_waits_for_prefetch_and_keeps_one_decode():
    with app_harness(LLM) as h, patch("sayink.app.QTimer.singleShot"):
        app, callbacks = _two_segments(h)
        _finish_first_output(h)
        assert app._pipeline_busy()
        assert len(app._segment_queue) == 1
        assert h["recognizer"].transcribe_final.call_count == 1
        callbacks["on_text"]("第二句")
        app._pump_segment_queue()
        assert h["polisher"].polish.call_args.args[0] == "第二句"
        assert h["recognizer"].transcribe_final.call_count == 1


def test_failed_prefetch_retries_its_original_audio_once_in_normal_path():
    with app_harness(LLM) as h, patch("sayink.app.QTimer.singleShot"):
        app, callbacks = _two_segments(h)
        original = app._segment_queue[0].audio
        callbacks["on_error"]("decode failed")
        app._pump_segment_queue()
        assert h["recognizer"].prefetch_final.call_count == 1
        assert app._segment_queue[0].audio is original
        _finish_first_output(h)
        assert h["recognizer"].transcribe_final.call_count == 2
        assert h["recognizer"].transcribe_final.call_args.args[0] is original


def test_cancelled_prefetch_cannot_restore_discarded_queue():
    with app_harness(LLM) as h, patch("sayink.app.QTimer.singleShot"):
        app, callbacks = _two_segments(h)
        app._clear_queued_audio()
        h["recognizer"].cancel_current.assert_called_once()
        callbacks["on_text"]("已取消")
        assert app._prefetch_segment is None
        assert app._segment_queue == []
        assert app._pending_record.raw_text == "第一句"


def test_prefetched_old_session_keeps_its_identity_after_restart():
    with app_harness(LLM) as h, patch("sayink.app.QTimer.singleShot"):
        app, callbacks = _two_segments(h)
        old_id = app._segment_queue[0].context.session_id
        app._current_session_id, app._current_seq = "new-session", 0
        h["recorder"].is_continuous = False
        callbacks["on_text"]("旧场尾句")
        _finish_first_output(h)
        assert app._pending_record.session_id == old_id
        assert app._current_session_id == "new-session" and app._current_seq == 0
        assert h["polisher"].polish.call_args.args[0] == "旧场尾句"


def test_prefetched_meeting_turns_drain_in_order_with_original_timestamps():
    from tests.test_app_meeting_turns import batch, make_context
    from tests.test_app_meeting_wiring import _start

    with app_harness({**LLM, "meeting.polish_segments": True}) as h, patch("sayink.app.QTimer.singleShot"):
        app = h["app"]
        h["recognizer"].supports_prefetch = True
        meeting_id = _start(app, h)
        audio = np.full(48000, 0.1, np.float32)
        app._begin_transcription(audio, context=make_context(app, 10_000))
        app._on_speaker_turn_result(batch()[:1])
        context = make_context(app, 20_000)
        app._enqueue_audio(audio, "mic", context=context)
        app._pump_segment_queue()
        callbacks = h["recognizer"].prefetch_final.call_args.kwargs
        assert callbacks["turn_analyzer"] is not None
        callbacks["on_turns"](batch())
        app._end_meeting()
        for _ in range(4):
            app._on_polish_error("offline")
            app._pump_segment_queue()
        h["paster"].paste_async.assert_not_called()
        records = [c.args[0] for c in h["history"].enqueue.call_args_list]
        assert [r.raw_text for r in records] == ["甲说方案可行", "甲说方案可行", "乙说预算不足", "甲继续说明"]
        assert [r.seq for r in records] == [0, 1, 2, 3]
        assert [r.captured_at for r in records] == [10_000, 20_000, 21_000, 22_000]
        assert all(r.session_id == meeting_id for r in records)
        assert h["recognizer"].transcribe_final.call_count == 1


def test_output_watchdog_keeps_prefetch_while_falling_back_to_original():
    with app_harness(LLM) as h, patch("sayink.app.QTimer.singleShot"):
        app, callbacks = _two_segments(h)
        app._release_stuck_output(app._output_token)
        assert h["paster"].paste_async.call_args.args[0] == "第一句"
        callbacks["on_text"]("第二句")
        h["paster"].paste_async.call_args.args[1]("sent")
        app._pump_segment_queue()
        assert h["polisher"].polish.call_args.args[0] == "第二句"


@pytest.mark.parametrize("mode", ["hotkey", "continuous"])
def test_history_keeps_original_before_glossary_replacement(mode):
    glossary = [{"term": "五证", "variants": ["五中"]}, {"term": "GitHub"}]
    with app_harness({"audio.trigger_mode": mode, "stt.glossary": glossary}) as h:
        app = h["app"]
        app._begin_transcription(np.full(4800, 0.1, np.float32))
        app._on_final_result("五中资料放在GITHUB")
        h["paster"].paste_async.call_args.args[1]("sent")
        record = h["history"].enqueue.call_args.args[0]
        assert record.raw_text == "五中资料放在GITHUB"
        assert record.polished_text == "五证资料放在GitHub"


def test_native_prefetch_routes_only_to_cache_and_ignores_cancelled_results(monkeypatch):
    monkeypatch.setattr(TranscribeWorker, "start", lambda self: self.run())
    rec = SpeechRecognizer()
    rec._recognizer, rec._is_ready = _FakeRecognizer("<asr_text>保留原文</asr_text>"), True
    normal, prepared, errors = [], [], []
    rec.final_result.connect(normal.append)
    rec.prefetch_final(np.ones(RATE, np.float32), on_text=prepared.append,
                       on_turns=prepared.append, on_error=errors.append)
    assert normal == [] and prepared == ["保留原文"] and errors == []
    rec.cancel_current()
    rec._current_worker.result_ready.emit("late")
    assert prepared == ["保留原文"]

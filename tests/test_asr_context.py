from types import SimpleNamespace
import threading

import numpy as np
import pytest

from sayink.asr_context import BoundaryContextBuffer, with_context, decode_owned_context
from sayink.speech_recognizer import normalize_asr_output, TranscribeWorker, prepare_quiet_asr_audio
from tests.test_asr_short_retry import Recognizer

RATE = 16000


def test_boundary_lookahead_preserves_owned_samples_and_exact_context():
    audio = np.arange(30 * RATE + 1000, dtype=np.float32)
    buf = BoundaryContextBuffer()
    buf.feed(audio[:15 * RATE])
    buf.capture(audio[:15 * RATE], (0, 15 * RATE), True, ("first",))
    assert buf.release() == []
    buf.feed(audio[15 * RATE:15 * RATE + 4800])
    first, = buf.release()
    np.testing.assert_array_equal(first[0], audio[:15 * RATE])
    np.testing.assert_array_equal(first[0].suffix, audio[15 * RATE:15 * RATE + 4800])
    buf.feed(audio[15 * RATE + 4800:])
    buf.capture(audio[15 * RATE:], (15 * RATE, len(audio)), False, ("last",))
    last, = buf.release(ending=True)
    np.testing.assert_array_equal(last[0].prefix, audio[15 * RATE - 4800:15 * RATE])
    np.testing.assert_array_equal(last[0], audio[15 * RATE:])
    assert len(first[0]) + len(last[0]) == len(audio)


def test_context_does_not_cross_real_pause_and_stop_never_discards_held_audio():
    buf = BoundaryContextBuffer()
    buf.feed(np.ones(16000, np.float32))
    buf.capture(np.ones(16000), (0, 16000), True, ())
    ready, = buf.release(ending=True)
    assert ready[0].suffix.size == 0 and len(ready[0]) == 16000
    buf.feed(np.ones(24000, np.float32))
    buf.capture(np.ones(10000), (30000, 40000), False, ())
    ready, = buf.release()
    assert ready[0].prefix.size == 0


class TimedRecognizer:
    def __init__(self, tokens=("前", "重", "重", "后"), times=(0.1, 0.3, 0.6, 1.4), text="前重重后"):
        self.result = SimpleNamespace(text=text, tokens=tokens, timestamps=times)
        self.seen = []

    def create_stream(self):
        return SimpleNamespace(result=self.result, accept_waveform=lambda _rate, audio: self.seen.append(audio))

    def decode_stream(self, _stream):
        pass


def test_time_ownership_preserves_genuine_repetition_without_text_matching():
    audio = with_context(np.ones(RATE), np.ones(4800), np.ones(4800))
    rec = TimedRecognizer()
    assert decode_owned_context(rec, audio, normalize_asr_output) == "重重"
    assert len(rec.seen) == 1 and len(rec.seen[0]) == RATE + 9600


@pytest.mark.parametrize("options", [{"times": ()}, {"times": (0.1, float("nan"), 0.5, 1.1)},
                                    {"times": (0.4, 0.3, 0.6, 1.4)}, {"text": "不匹配"},
                                    {"times": (0.1, 0.3, 0.6, 9.0)}])
def test_invalid_timestamps_and_incomplete_token_coverage_do_not_trim(options):
    audio = with_context(np.ones(RATE), np.ones(4800))
    assert decode_owned_context(TimedRecognizer(**options), audio, normalize_asr_output) is None


def test_context_alignment_cannot_discard_decimal_points_or_minus_signs():
    audio = with_context(np.ones(RATE), np.ones(4800))
    for text in ("3.5", "-35", "3/5"):
        rec = TimedRecognizer(tokens=("3", "5"), times=(0.4, 0.6), text=text)
        assert decode_owned_context(rec, audio, normalize_asr_output) is None


def test_worker_uses_context_but_keeps_original_fallback_and_speaker_audio_separate():
    audio = with_context(np.ones(RATE), np.ones(4800), np.ones(4800))
    worker = TranscribeWorker(TimedRecognizer(), audio)
    result = []
    worker.result_ready.connect(result.append)
    worker.run()
    assert result == ["重重"]
    rec = Recognizer(["invalid timestamps result", "original"])
    worker = TranscribeWorker(rec, audio)
    result = []
    worker.result_ready.connect(result.append)
    worker.run()
    assert result == ["original"]
    assert len(rec.seen[0]) == RATE + 9600 and len(rec.seen[1]) == RATE


def test_quiet_gain_is_single_pass_bounded_and_never_amplifies_silence_or_steady_hum():
    time = np.arange(RATE) / RATE
    soft = (0.004 * np.sin(2 * np.pi * 200 * time)).astype(np.float32)
    soft[:4000] = soft[-4000:] = 0
    normalized = prepare_quiet_asr_audio(soft)
    assert normalized.max() > soft.max() and normalized.max() <= 0.9
    assert len(normalized) == len(soft)
    for source in (np.zeros(RATE), np.ones(RATE) * 0.001, soft * 0.01, soft * 50):
        np.testing.assert_array_equal(prepare_quiet_asr_audio(source), source.astype(np.float32))


def test_cancelled_native_worker_finishes_before_next_worker_enters_same_decoder():
    entered = threading.Event()
    finish_first = threading.Event()
    second_entered = threading.Event()
    lock = threading.Lock()

    class Native:
        calls = 0

        def create_stream(self):
            return SimpleNamespace(result=SimpleNamespace(text="文字"), accept_waveform=lambda *_args: None)

        def decode_stream(self, _stream):
            self.calls += 1
            if self.calls == 1:
                entered.set()
                assert finish_first.wait(3)
            else:
                second_entered.set()

    native = Native()
    first = TranscribeWorker(native, np.ones(RATE))
    second = TranscribeWorker(native, np.ones(RATE))
    first.native_lock = second.native_lock = lock
    thread1 = threading.Thread(target=first.run)
    thread2 = threading.Thread(target=second.run)
    thread1.start()
    assert entered.wait(2)
    first.cancel()
    thread2.start()
    assert not second_entered.wait(0.15)
    finish_first.set()
    thread1.join(3)
    thread2.join(3)
    assert second_entered.is_set() and not thread1.is_alive() and not thread2.is_alive()


def test_opted_in_quiet_gain_does_not_add_decode_or_modify_original_audio():
    samples = np.ones(RATE, np.float32) * 0.0002
    original = samples.copy()
    rec = Recognizer(["轻声文字"])
    worker = TranscribeWorker(rec, samples, retry_empty=True)
    worker.quiet_speech = True
    worker.run()
    assert len(rec.seen) == 1
    assert np.max(rec.seen[0]) <= np.max(samples) * 32.001
    assert np.max(rec.seen[0]) > np.max(samples) * 8
    np.testing.assert_array_equal(samples, original)

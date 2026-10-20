"""A short empty decode may be retried once without rewriting valid text."""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from sayink.speech_recognizer import TranscribeWorker, decode_asr_slice, short_retry_audio


def audio(seconds=0.35):
    t = np.arange(int(seconds * 16000), dtype=np.float32) / 16000
    return (0.2 * np.sin(2 * np.pi * 200 * t)).astype(np.float32)


class Recognizer:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.seen = []

    def create_stream(self):
        stream = SimpleNamespace(result=SimpleNamespace(text=""))
        stream.accept_waveform = lambda _rate, samples: self.seen.append(samples.copy())
        return stream

    def decode_stream(self, stream):
        output = next(self.outputs)
        if isinstance(output, Exception):
            raise output
        stream.result.text = output


def test_empty_short_utterance_is_retried_with_context_padding_once():
    rec = Recognizer(["<sil>", "<asr_text>好的</asr_text>"])
    assert decode_asr_slice(rec, audio(), retry_empty=True) == "好的"
    assert len(rec.seen) == 2
    assert rec.seen[1].size >= 16000
    assert np.all(rec.seen[1][:2000] == 0)


def test_successful_first_decode_is_never_rewritten_or_retried():
    rec = Recognizer(["预算不是三万元"])
    assert decode_asr_slice(rec, audio(), retry_empty=True) == "预算不是三万元"
    assert len(rec.seen) == 1


def test_silence_quiet_and_long_clips_do_not_trigger_retry():
    for samples in (np.zeros(16000), audio() * 0.001, audio(8.1)):
        rec = Recognizer([""])
        assert decode_asr_slice(rec, samples, retry_empty=True) == ""
        assert len(rec.seen) == 1


def test_retry_failure_keeps_first_result_and_does_not_drop_the_pipeline():
    rec = Recognizer(["", RuntimeError("retry failed")])
    assert decode_asr_slice(rec, audio(), retry_empty=True) == ""
    assert len(rec.seen) == 2


def test_retry_is_bounded_and_can_be_disabled_or_cancelled():
    rec = Recognizer(["", ""])
    assert decode_asr_slice(rec, audio(), retry_empty=True) == ""
    assert len(rec.seen) == 2
    for options in ({}, {"retry_empty": True, "cancelled": lambda: True}):
        rec = Recognizer([""])
        assert decode_asr_slice(rec, audio(), **options) == ""
        assert len(rec.seen) == 1


def test_retry_trims_quiet_edges_without_changing_the_speech_samples():
    speech = audio()
    clip = np.concatenate([np.zeros(32000), speech, np.zeros(32000)]).astype(np.float32)
    retry = short_retry_audio(clip)
    assert retry is not None and 16000 <= retry.size < clip.size
    assert np.count_nonzero(retry) == np.count_nonzero(speech)
    assert np.max(retry) == np.max(speech)


def test_worker_emits_only_recovered_text_and_normalized_partial():
    worker = TranscribeWorker(Recognizer(["", "<asr_text>同意</asr_text>"]), audio(), retry_empty=True)
    results, partials, errors = [], [], []
    worker.result_ready.connect(results.append)
    worker.partial_ready.connect(partials.append)
    worker.error.connect(errors.append)
    worker.run()
    assert results == partials == ["同意"] and errors == []

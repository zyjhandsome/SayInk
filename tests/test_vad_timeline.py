"""Emitted audio must point to the exact original input, including silent gaps."""
import numpy as np

from sayink.vad_segmenter import SpeechSegmenter
from tools.asr_bench import segment_meeting_timed

RATE = 16000


def test_original_offsets_survive_pre_roll_trim_and_irregular_blocks():
    rng = np.random.default_rng(42)
    audio = np.r_[np.zeros(3680), rng.normal(0, 0.05, 27200), np.zeros(9600),
                  rng.normal(0, 0.05, 12800), np.zeros(11200)].astype(np.float32)
    seg = SpeechSegmenter(adaptive=False, speech_threshold=0.002, silence_hold_sec=0.2,
                          min_speech_sec=0.1, max_speech_sec=1.0, pre_roll_sec=0.15,
                          short_speech_sec=0, trailing_silence_keep_sec=0.05)
    emitted = []
    at, sizes = 0, (333, 1700, 800, 2900)
    while at < len(audio):
        block = audio[at:at + sizes[len(emitted) % len(sizes)]]
        at += len(block)
        out = seg.feed(block)
        if out is not None:
            a, b = seg.last_bounds
            assert np.array_equal(out, audio[a:b])
            emitted.append((a, b))
    tail = seg.flush()
    if tail is not None:
        a, b = seg.last_bounds
        assert np.array_equal(tail, audio[a:b])
        emitted.append((a, b))
    assert len(emitted) >= 3
    assert all(a[1] <= b[0] for a, b in zip(emitted, emitted[1:], strict=False))
    assert any(a[1] < b[0] for a, b in zip(emitted, emitted[1:], strict=False))


def test_dropped_noise_and_empty_flush_do_not_reset_the_stream_clock():
    seg = SpeechSegmenter(adaptive=False, min_speech_sec=0.2, min_loud_sec=0.2, pre_roll_sec=0,
                          silence_hold_sec=0.1, short_speech_sec=0)
    seg.feed(np.full(800, 0.05, np.float32))
    assert seg.feed(np.zeros(3200, np.float32)) is None
    assert seg.flush() is None
    seg.feed(np.full(6400, 0.05, np.float32))
    assert seg.flush() is not None
    assert seg.last_bounds == (4000, 10400)
    seg.reset()
    seg.feed(np.full(6400, 0.05, np.float32))
    seg.flush()
    assert seg.last_bounds == (0, 6400)


def test_carried_speech_tail_has_contiguous_original_bounds():
    seg = SpeechSegmenter(adaptive=False, min_speech_sec=0.1, max_speech_sec=1)
    first = seg.feed(np.full(24000, 0.05, np.float32))
    assert len(first) == RATE and seg.last_bounds == (0, RATE)
    assert len(seg.flush()) == 8000 and seg.last_bounds == (RATE, 24000)


def test_timed_benchmark_does_not_collapse_the_gap_between_utterances():
    audio = np.r_[np.full(RATE, 0.1), np.zeros(4 * RATE), np.full(RATE, 0.1)].astype(np.float32)
    chunks, used = segment_meeting_timed(audio, RATE, vad="energy")
    assert used == "energy" and len(chunks) == 2
    assert chunks[1].start >= 4 * RATE
    assert all(np.array_equal(t.audio, audio[t.start:t.stop]) for t in chunks)


def test_recorder_timestamp_includes_pre_roll_and_keeps_the_carried_tail_clock(monkeypatch):
    from sayink.audio_recorder import AudioRecorder

    recorder = AudioRecorder()
    recorder._segmenter = SpeechSegmenter(adaptive=False, min_speech_sec=0.05, max_speech_sec=1,
                                          pre_roll_sec=0.1)
    clock = iter((1000.0, 1000.7, 1001.0))
    monkeypatch.setattr("sayink.audio_recorder.time.time", lambda: next(clock))
    assert recorder._feed_segmenter(np.zeros(4800, np.float32)) is None
    assert recorder._feed_segmenter(np.full(11200, 0.05, np.float32)) is None
    first = recorder._feed_segmenter(np.full(4800, 0.05, np.float32))
    monkeypatch.setattr("sayink.audio_recorder.time.time", lambda: 1001.0)
    recorder._emit_segment(first)
    assert recorder._segmenter.last_bounds == (3200, 19200)
    stamp = recorder.consume_segment_captured_at()
    assert stamp == 999900
    tail = recorder._segmenter.flush()
    recorder._emit_segment(tail)
    assert recorder.consume_segment_captured_at() == stamp + len(first) * 1000 // RATE

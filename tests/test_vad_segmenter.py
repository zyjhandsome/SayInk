"""Tests for README「自动持续转写」的 VAD 分段逻辑。"""

import numpy as np
import pytest

from sayink.vad_segmenter import SpeechSegmenter, SPEECH_RMS_THRESHOLD


def _tone(duration_sec: float, amplitude: float = 0.5, rate: int = 16000) -> np.ndarray:
    n = int(rate * duration_sec)
    return np.full(n, amplitude, dtype=np.float32)


def _silence(duration_sec: float, rate: int = 16000) -> np.ndarray:
    return _tone(duration_sec, 0.0, rate)


class TestSpeechSegmenterBasics:
    def test_silence_only_returns_none(self):
        seg = SpeechSegmenter(speech_threshold=0.002)
        assert seg.feed(_silence(0.5)) is None

    def test_speech_then_silence_emits_segment(self):
        seg = SpeechSegmenter(
            speech_threshold=0.002,
            silence_hold_sec=0.2,
            min_speech_sec=0.1,
        )
        assert seg.feed(_tone(0.3)) is None
        out = seg.feed(_silence(0.3))
        assert out is not None
        assert out.size >= int(16000 * 0.1)

    def test_too_short_speech_discarded(self):
        seg = SpeechSegmenter(
            speech_threshold=0.002,
            silence_hold_sec=0.1,
            min_speech_sec=0.5,
        )
        seg.feed(_tone(0.1))
        assert seg.feed(_silence(0.2)) is None

    def test_default_max_cuts_a_long_utterance_into_a_live_slice(self):
        seg = SpeechSegmenter(speech_threshold=0.002, min_speech_sec=0.1)
        out = seg.feed(_tone(16))
        assert out is not None
        assert out.size <= int(16000 * 15) + 100

    def test_max_length_forces_cut(self):
        seg = SpeechSegmenter(
            speech_threshold=0.002,
            max_speech_sec=0.3,
            min_speech_sec=0.1,
        )
        out = seg.feed(_tone(0.35))
        assert out is not None
        assert out.size <= int(16000 * 0.35) + 100

    def test_reset_clears_state(self):
        seg = SpeechSegmenter()
        seg.feed(_tone(0.2))
        seg.reset()
        assert seg.feed(_silence(0.5)) is None

    def test_flush_emits_incomplete_speech(self):
        seg = SpeechSegmenter(
            speech_threshold=0.002,
            silence_hold_sec=0.85,
            min_speech_sec=0.1,
        )
        seg.feed(_tone(0.3))
        out = seg.flush()
        assert out is not None
        assert out.size >= int(16000 * 0.1)

    def test_flush_discards_too_short_speech(self):
        seg = SpeechSegmenter(
            speech_threshold=0.002,
            min_speech_sec=0.5,
        )
        seg.feed(_tone(0.1))
        assert seg.flush() is None

    def test_lower_threshold_for_system_audio(self):
        """System loopback is quieter; recorder uses 0.0006 threshold."""
        seg = SpeechSegmenter(speech_threshold=0.0006)
        quiet = np.full(1600, 0.001, dtype=np.float32)
        assert seg.feed(quiet) is None
        louder = np.full(1600, 0.0015, dtype=np.float32)
        assert seg.feed(louder) is None  # still accumulating, not finished


def _noisy(duration_sec: float, noise: float, speech: float = 0.0, rate: int = 16000) -> np.ndarray:
    """Steady noise with optional speech on top (RMS ≈ hypot(noise, speech))."""
    n = int(rate * duration_sec)
    rng = np.random.default_rng(7)
    block = rng.normal(0.0, noise, n).astype(np.float32)
    if speech:
        block += np.float32(speech)
    return block


class TestAdaptiveNoiseFloor:
    """A noise floor sitting above the fixed gate used to turn into endless
    MAX_SPEECH_SEC slices; the gate must follow the room instead."""

    def test_gate_starts_at_the_configured_threshold(self):
        seg = SpeechSegmenter(speech_threshold=0.002)
        assert seg.effective_threshold == 0.002

    def test_gate_lifts_above_a_steady_noise_floor(self):
        seg = SpeechSegmenter(speech_threshold=0.002)
        seg.feed(_noisy(1.0, 0.0025))
        assert seg.effective_threshold == pytest.approx(0.0075, rel=0.1)

    def test_gate_never_lifts_past_the_cap(self):
        from sayink.vad_segmenter import ADAPTIVE_THRESHOLD_CAP

        seg = SpeechSegmenter(speech_threshold=0.002)
        seg.feed(_tone(1.0, 0.05))
        assert seg.effective_threshold == ADAPTIVE_THRESHOLD_CAP

    def test_speech_over_noise_still_segments_on_pause(self):
        seg = SpeechSegmenter(speech_threshold=0.002, silence_hold_sec=0.3, min_speech_sec=0.1)
        for _ in range(10):
            assert seg.feed(_noisy(0.1, 0.0025)) is None
        for _ in range(5):
            assert seg.feed(_noisy(0.1, 0.0025, speech=0.05)) is None
        out = None
        for _ in range(6):
            out = out if out is not None else seg.feed(_noisy(0.1, 0.0025))
        assert out is not None
        assert out.size < int(16000 * 1.5)

    def test_noise_alone_does_not_emit_back_to_back_max_slices(self):
        seg = SpeechSegmenter(speech_threshold=0.002, max_speech_sec=2.0, min_speech_sec=0.1)
        emitted = [seg.feed(_noisy(0.1, 0.0025)) for _ in range(60)]
        assert all(out is None for out in emitted)

    def test_adaptive_can_be_switched_off(self):
        seg = SpeechSegmenter(speech_threshold=0.002, adaptive=False)
        seg.feed(_noisy(1.0, 0.0025))
        assert seg.effective_threshold == 0.002

    def test_noise_floor_survives_reset(self):
        seg = SpeechSegmenter(speech_threshold=0.002)
        seg.feed(_noisy(1.0, 0.0025))
        lifted = seg.effective_threshold
        seg.reset()
        assert seg.effective_threshold == lifted


class TestTransientRejection:
    """A 50 ms key click used to become a 1 s segment (click + silence hold)."""

    def test_single_click_is_discarded(self):
        seg = SpeechSegmenter(speech_threshold=0.002, silence_hold_sec=0.85, min_speech_sec=0.25)
        assert seg.feed(_tone(0.05, 0.3)) is None
        assert seg.feed(_silence(1.0)) is None

    def test_click_is_discarded_on_flush_too(self):
        seg = SpeechSegmenter(speech_threshold=0.002, min_speech_sec=0.25)
        seg.feed(_tone(0.05, 0.3))
        seg.feed(_silence(0.3))
        assert seg.flush() is None

    def test_short_real_speech_still_passes(self):
        seg = SpeechSegmenter(speech_threshold=0.002, silence_hold_sec=0.3, min_speech_sec=0.25)
        seg.feed(_tone(0.3, 0.3))
        out = seg.feed(_silence(0.4))
        assert out is not None

    def test_min_loud_never_exceeds_min_speech(self):
        seg = SpeechSegmenter(speech_threshold=0.002, silence_hold_sec=0.1, min_speech_sec=0.05)
        seg.feed(_tone(0.06, 0.3))
        assert seg.feed(_silence(0.2)) is not None

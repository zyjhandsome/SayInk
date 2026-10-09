"""Tests for README「自动持续转写」的 VAD 分段逻辑。"""

import numpy as np
import pytest

from sayink.vad_segmenter import ADAPTIVE_THRESHOLD_CAP, SHORT_SILENCE_HOLD_SEC, SpeechSegmenter


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
            short_speech_sec=0.0,
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
        seg = SpeechSegmenter(speech_threshold=0.002, min_speech_sec=0.1, adaptive=False)
        out = seg.feed(_tone(16))
        assert out is not None
        assert out.size <= int(16000 * 15) + 100

    def test_long_utterance_is_cut_at_the_quietest_recent_block(self):
        """README: the 15 s slice ends in a breath, not in the middle of a word."""
        seg = SpeechSegmenter(speech_threshold=0.002, min_speech_sec=0.1, adaptive=False)
        block = int(16000 * 0.1)
        emitted = []
        for i in range(0, 160):  # 16 s in 0.1 s blocks
            amp = 0.01 if i == 130 else 0.5  # a breath at 13.0–13.1 s, still above the gate
            out = seg.feed(np.full(block, amp, dtype=np.float32))
            if out is not None:
                emitted.append(out)
        assert len(emitted) == 1
        assert emitted[0].size == 131 * block
        # The remainder carries on as the next utterance.
        tail = seg.flush()
        assert tail is not None
        assert tail.size == (160 - 131) * block

    def test_flat_long_utterance_still_cuts_at_the_limit(self):
        seg = SpeechSegmenter(speech_threshold=0.002, min_speech_sec=0.1, adaptive=False)
        block = int(16000 * 0.1)
        emitted = [seg.feed(np.full(block, 0.5, dtype=np.float32)) for _ in range(160)]
        emitted = [e for e in emitted if e is not None]
        assert len(emitted) == 1
        assert emitted[0].size == int(16000 * 15)

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
        seg.feed(_tone(1.0, 0.005))  # 3× floor would be 0.015
        assert seg.effective_threshold == ADAPTIVE_THRESHOLD_CAP

    def test_gate_never_sits_below_a_floor_louder_than_the_cap(self):
        seg = SpeechSegmenter(speech_threshold=0.002)
        _feed_blocks(seg, _noisy(4.0, 0.03))
        assert seg.effective_threshold > 0.03 * 1.2
        assert seg.hold_threshold > 0.03 * 1.2

    def test_floor_is_not_trusted_from_the_first_words(self):
        """Talking right after start: the quietest block so far is the voice itself."""
        seg = SpeechSegmenter(speech_threshold=0.002)
        _feed_blocks(seg, _tone(1.0, 0.1))
        assert seg.effective_threshold == ADAPTIVE_THRESHOLD_CAP

    def test_long_monologue_with_word_gaps_is_not_split_early(self):
        seg = SpeechSegmenter(speech_threshold=0.002, min_speech_sec=0.1)
        block = int(16000 * 0.1)
        emitted = []
        for i in range(140):  # 14 s, a short dip between every few words
            amp = 0.02 if i % 6 == 5 else (0.15 if i % 2 else 0.08)
            out = seg.feed(np.full(block, amp, dtype=np.float32))
            if out is not None:
                emitted.append(out)
        assert emitted == []
        out = seg.flush()
        assert out is not None and out.size >= 140 * block - block

    def test_loud_noise_still_lets_pauses_end_segments(self):
        """A fan / café floor above the cap used to merge everything into 15 s slices."""
        seg = SpeechSegmenter(speech_threshold=0.002, short_speech_sec=0.0)
        audio = np.concatenate([
            _noisy(5.0, 0.03),
            _noisy(2.0, 0.03, speech=0.12), _noisy(1.5, 0.03),
            _noisy(2.0, 0.03, speech=0.12), _noisy(2.0, 0.03),
        ])
        segments = [s for s in (seg.feed(audio[i:i + 1600]) for i in range(0, audio.size, 1600)) if s is not None]
        assert len(segments) == 2
        assert all(s.size < 16000 * 3.5 for s in segments)

    def test_loud_noise_alone_never_becomes_a_segment(self):
        seg = SpeechSegmenter(speech_threshold=0.002)
        audio = _noisy(20.0, 0.03)
        emitted = [seg.feed(audio[i:i + 1600]) for i in range(0, audio.size, 1600)]
        assert all(out is None for out in emitted)
        assert seg.flush() is None

    def test_speech_over_noise_still_segments_on_pause(self):
        seg = SpeechSegmenter(
            speech_threshold=0.002, silence_hold_sec=0.3, min_speech_sec=0.1, short_speech_sec=0.0
        )
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
        out = seg.feed(_silence(SHORT_SILENCE_HOLD_SEC + 0.1))
        assert out is not None

    def test_min_loud_never_exceeds_min_speech(self):
        seg = SpeechSegmenter(
            speech_threshold=0.002, silence_hold_sec=0.1, min_speech_sec=0.05, short_speech_sec=0.0
        )
        seg.feed(_tone(0.06, 0.3))
        assert seg.feed(_silence(0.2)) is not None


RATE = 16000
BLOCK = int(RATE * 0.1)


def _feed_blocks(seg: SpeechSegmenter, audio: np.ndarray) -> list[np.ndarray]:
    """Feed in 100 ms ticks like the recorder does; return emitted segments."""
    out = []
    for start in range(0, audio.size, BLOCK):
        piece = seg.feed(audio[start : start + BLOCK])
        if piece is not None:
            out.append(piece)
    return out


class TestPreRoll:
    """The soft start of the first syllable sits below the gate."""

    def test_quiet_onset_before_the_gate_opens_is_kept(self):
        seg = SpeechSegmenter(speech_threshold=0.002, short_speech_sec=0.0, adaptive=False)
        onset = np.full(BLOCK * 2, 0.001, dtype=np.float32)  # below the gate
        audio = np.concatenate([_silence(1.0), onset, _tone(1.0, 0.3), _silence(1.5)])
        (segment,) = _feed_blocks(seg, audio)
        assert np.count_nonzero(segment[: BLOCK * 3] == np.float32(0.001)) == BLOCK * 2

    def test_pre_roll_is_at_most_its_configured_length(self):
        seg = SpeechSegmenter(speech_threshold=0.002, short_speech_sec=0.0, adaptive=False)
        audio = np.concatenate([_silence(2.0), _tone(1.0, 0.3), _silence(1.5)])
        (segment,) = _feed_blocks(seg, audio)
        lead = int(np.argmax(segment != 0))
        assert lead == int(RATE * 0.3)

    def test_pre_roll_does_not_make_a_click_long_enough(self):
        seg = SpeechSegmenter(speech_threshold=0.002, min_speech_sec=0.25)
        audio = np.concatenate([_silence(1.0), _tone(0.05, 0.3), _silence(2.5)])
        assert _feed_blocks(seg, audio) == []


class TestShortClipsWaitForMore:
    """「嗯」「一个」alone give the model no context; they join the next words."""

    def test_short_clip_followed_soon_by_speech_becomes_one_segment(self):
        seg = SpeechSegmenter(speech_threshold=0.002)
        audio = np.concatenate([
            _tone(0.4, 0.3), _silence(1.4), _tone(1.5, 0.3), _silence(2.0),
        ])
        segments = _feed_blocks(seg, audio)
        assert len(segments) == 1
        assert segments[0].size > int(RATE * (0.4 + 1.4 + 1.5))

    def test_long_sentence_still_ends_on_the_normal_pause(self):
        seg = SpeechSegmenter(speech_threshold=0.002)
        audio = np.concatenate([_tone(1.5, 0.3), _silence(1.4), _tone(1.5, 0.3), _silence(2.0)])
        assert len(_feed_blocks(seg, audio)) == 2

    def test_a_lone_short_reply_is_still_emitted(self):
        seg = SpeechSegmenter(speech_threshold=0.002)
        audio = np.concatenate([_tone(0.4, 0.3), _silence(SHORT_SILENCE_HOLD_SEC + 0.2)])
        assert len(_feed_blocks(seg, audio)) == 1


class TestHoldGate:
    """Once speech started, a softer syllable must not count as a pause."""

    def test_soft_syllable_mid_sentence_does_not_split(self):
        seg = SpeechSegmenter(speech_threshold=0.002, short_speech_sec=0.0)
        soft = np.full(int(RATE * 1.5), 0.0015, dtype=np.float32)  # under 0.002, over half
        audio = np.concatenate([
            _silence(1.0), _tone(1.0, 0.3), soft, _tone(1.0, 0.3), _silence(2.0),
        ])
        assert len(_feed_blocks(seg, audio)) == 1
    def test_hold_gate_stays_above_the_noise_floor(self):
        seg = SpeechSegmenter(speech_threshold=0.002)
        seg.feed(_noisy(1.0, 0.0025))
        assert seg.hold_threshold > 0.0025

    def test_hold_gate_never_exceeds_the_opening_gate(self):
        seg = SpeechSegmenter(speech_threshold=0.002)
        seg.feed(_tone(1.0, 0.05))  # floor well above the cap
        assert seg.hold_threshold <= seg.effective_threshold


class TestTrailingSilence:
    def test_segment_keeps_only_a_short_silent_tail(self):
        from sayink.vad_segmenter import TRAILING_SILENCE_KEEP_SEC

        seg = SpeechSegmenter(speech_threshold=0.002, short_speech_sec=0.0, pre_roll_sec=0.0)
        (segment,) = _feed_blocks(seg, np.concatenate([_tone(1.0, 0.3), _silence(2.0)]))
        assert segment.size == int(RATE * (1.0 + TRAILING_SILENCE_KEEP_SEC))

    def test_trimmed_silence_feeds_the_next_pre_roll(self):
        seg = SpeechSegmenter(speech_threshold=0.002, short_speech_sec=0.0, adaptive=False)
        onset = np.full(BLOCK, 0.001, dtype=np.float32)
        audio = np.concatenate([_tone(1.0, 0.3), _silence(1.3), onset, _tone(1.0, 0.3), _silence(2.0)])
        first, second = _feed_blocks(seg, audio)
        assert np.count_nonzero(second[:BLOCK * 3] == np.float32(0.001)) == BLOCK

    def test_flush_trims_the_silent_tail_too(self):
        from sayink.vad_segmenter import TRAILING_SILENCE_KEEP_SEC

        seg = SpeechSegmenter(speech_threshold=0.002, pre_roll_sec=0.0)
        _feed_blocks(seg, np.concatenate([_tone(1.0, 0.3), _silence(1.0)]))
        out = seg.flush()
        assert out is not None
        assert out.size == int(RATE * (1.0 + TRAILING_SILENCE_KEEP_SEC))

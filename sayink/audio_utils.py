"""Audio resampling and mixing helpers for the recorder."""

from __future__ import annotations

from functools import lru_cache

import numpy as np

TARGET_SAMPLE_RATE = 16000
# Longest piece of audio any live stage handles at once. The segmenter cuts
# utterances here (inside the Fun-ASR-Nano / Qwen3-ASR context window), and
# the speaker-turn detector, overlap detector and recognizer slicer all size
# their buffers from the same number: raising the cut alone would make the
# detectors return "uncertain" for every segment without doing any work.
MAX_SEGMENT_SEC = 15.0
# Downsampling 48 kHz → 16 kHz without a low-pass folds 8–24 kHz (the hiss of
# s / x / sh / c, fan noise) back into the speech band at ~85 % level. The
# filter passes up to this fraction of the target rate and stops at Nyquist.
LOWPASS_CUTOFF_RATIO = 0.45
LOWPASS_TAPS = 127


@lru_cache(maxsize=8)
def _lowpass_taps(source_rate: int, target_rate: int) -> np.ndarray:
    """Hann-windowed sinc low-pass at the source rate, unity DC gain."""
    cutoff = LOWPASS_CUTOFF_RATIO * target_rate / source_rate
    n = np.arange(LOWPASS_TAPS) - (LOWPASS_TAPS - 1) / 2
    taps = 2 * cutoff * np.sinc(2 * cutoff * n) * np.hanning(LOWPASS_TAPS)
    return (taps / taps.sum()).astype(np.float32)


def to_mono(audio: np.ndarray) -> np.ndarray:
    """Collapse multi-channel float audio to mono."""
    if audio.size == 0:
        return audio.astype(np.float32, copy=False)
    arr = np.asarray(audio, dtype=np.float32)
    if arr.ndim == 1:
        return arr
    if arr.ndim == 2:
        if arr.shape[1] == 1:
            return arr[:, 0]
        return np.mean(arr, axis=1, dtype=np.float32)
    return arr.reshape(-1).astype(np.float32)


def resample_mono(audio: np.ndarray, source_rate: int, target_rate: int = TARGET_SAMPLE_RATE) -> np.ndarray:
    """Resample a whole mono clip to target_rate (low-pass first when shrinking)."""
    mono = to_mono(audio)
    if mono.size == 0 or source_rate <= 0:
        return mono
    if source_rate == target_rate:
        return mono
    if source_rate > target_rate:
        mono = np.convolve(mono, _lowpass_taps(source_rate, target_rate), mode="same")
    duration = mono.shape[0] / float(source_rate)
    target_len = max(1, int(round(duration * target_rate)))
    src_idx = np.linspace(0, mono.shape[0] - 1, target_len, dtype=np.float64)
    return np.interp(src_idx, np.arange(mono.shape[0], dtype=np.float64), mono).astype(np.float32)


class StreamResampler:
    """Resample a capture stream that arrives in blocks of any size.

    Filter history and the fractional read position carry over between
    blocks, so 100 ms ticks join without a seam or drift. Output lags the
    input by half the filter length (~1.3 ms at 48 kHz).
    """

    def __init__(self, source_rate: int, target_rate: int = TARGET_SAMPLE_RATE):
        self.source_rate = int(source_rate)
        self.target_rate = int(target_rate)
        self._step = self.source_rate / self.target_rate
        self._taps = (
            _lowpass_taps(self.source_rate, self.target_rate)
            if self.source_rate > self.target_rate
            else None
        )
        self._history = (
            np.zeros(LOWPASS_TAPS - 1, dtype=np.float32) if self._taps is not None else None
        )
        # Last filtered sample of the previous block, for interpolating across the seam.
        self._last: np.float32 | None = None
        # Next output position, in samples after self._last (or the block start).
        self._pos = 0.0

    def process(self, audio: np.ndarray) -> np.ndarray:
        block = to_mono(audio).astype(np.float32, copy=False)
        if block.size == 0 or self.source_rate <= 0 or self.source_rate == self.target_rate:
            return block
        if self._taps is not None:
            padded = np.concatenate([self._history, block])
            self._history = padded[-(LOWPASS_TAPS - 1):]
            filtered = np.convolve(padded, self._taps, mode="valid").astype(np.float32)
        else:
            filtered = block
        line = filtered if self._last is None else np.concatenate([[self._last], filtered])
        end = line.size - 1
        if self._pos > end:
            self._pos -= end
            self._last = filtered[-1]
            return np.zeros(0, dtype=np.float32)
        positions = np.arange(self._pos, end + 1e-9, self._step)
        out = np.interp(positions, np.arange(line.size, dtype=np.float64), line).astype(np.float32)
        self._pos = positions[-1] + self._step - end
        self._last = filtered[-1]
        return out


def rms_volume(audio: np.ndarray) -> float:
    mono = to_mono(audio)
    if mono.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(mono ** 2)))


def mix_to_mono(
    tracks: list[np.ndarray],
    sample_rate: int = TARGET_SAMPLE_RATE,
    *,
    gains: list[float] | None = None,
) -> np.ndarray:
    """Resample each track to sample_rate, align length, and mix with headroom.

    Without ``gains`` the tracks are averaged (dictation behaviour). With
    ``gains`` (one per track, from :class:`LaneBalancer`) each track is scaled
    and the tracks are *summed*: a lane that is quiet because nobody on it is
    talking must not halve the lane that is, which is what averaging did to
    mixed-capture meetings.
    """
    if not tracks:
        return np.array([], dtype=np.float32)
    pairs = [
        (resample_mono(t, sample_rate, sample_rate), (gains[i] if gains and i < len(gains) else 1.0))
        for i, t in enumerate(tracks)
        if t is not None and t.size > 0
    ]
    if not pairs:
        return np.array([], dtype=np.float32)
    max_len = max(t.shape[0] for t, _g in pairs)
    mixed = np.zeros(max_len, dtype=np.float32)
    scale = 1.0 if gains else 1.0 / len(pairs)
    for t, gain in pairs:
        if t.shape[0] < max_len:
            t = np.concatenate([t, np.zeros(max_len - t.shape[0], dtype=np.float32)])
        else:
            t = t[:max_len]
        mixed += t * (scale * float(gain))
    # The recorder calls this per 100 ms block. Rescaling only the blocks
    # that happen to sum above full scale used to step the level up and
    # down at block boundaries; clipping the rare overshoot keeps the gain
    # continuous (the balancer already targets RMS 0.05, far below 1.0).
    if mixed.size and float(np.max(np.abs(mixed))) > 1.0:
        mixed = np.clip(mixed, -1.0, 1.0).astype(np.float32)
    return mixed


# Speech below this RMS is treated as "nobody talking on this lane": its
# level is not learned, so the gain never chases room noise upward.
BALANCE_SPEECH_RMS = 0.004
# Lanes are pulled toward this RMS (a comfortable speech level); the gain is
# clamped so a bad level estimate cannot blow up or mute a lane.
BALANCE_TARGET_RMS = 0.05
BALANCE_MAX_GAIN = 4.0
BALANCE_MIN_GAIN = 0.25
# How fast a lane's learned level follows new loud blocks (0..1 per block).
BALANCE_LEVEL_ALPHA = 0.1


class LaneBalancer:
    """Slow per-lane loudness tracker for mixed capture (mic + loopback).

    A remote caller through the speakers and the user at the mic rarely come
    in at the same level; the quieter one got half the headroom and worse ASR.
    Each lane's speech RMS is tracked with an exponential average over blocks
    that actually contain speech, and the gain that would bring that level to
    ``BALANCE_TARGET_RMS`` is applied, clamped to [1/4, 4].
    """

    def __init__(
        self,
        *,
        target_rms: float = BALANCE_TARGET_RMS,
        max_gain: float = BALANCE_MAX_GAIN,
        min_gain: float = BALANCE_MIN_GAIN,
        speech_rms: float = BALANCE_SPEECH_RMS,
        alpha: float = BALANCE_LEVEL_ALPHA,
    ):
        self._target = float(target_rms)
        self._max = float(max_gain)
        self._min = float(min_gain)
        self._speech = float(speech_rms)
        self._alpha = float(alpha)
        self._levels: dict[str, float] = {}

    def reset(self) -> None:
        self._levels.clear()

    def level(self, lane: str) -> float:
        return self._levels.get(lane, 0.0)

    def observe(self, lane: str, block: np.ndarray) -> float:
        """Learn from one block and return the gain to apply to it."""
        rms = rms_volume(block)
        if rms >= self._speech:
            previous = self._levels.get(lane)
            if previous is None:
                self._levels[lane] = rms
            else:
                self._levels[lane] = previous + self._alpha * (rms - previous)
        return self.gain(lane)

    def gain(self, lane: str) -> float:
        level = self._levels.get(lane, 0.0)
        if level <= 0.0:
            return 1.0
        return float(min(self._max, max(self._min, self._target / level)))

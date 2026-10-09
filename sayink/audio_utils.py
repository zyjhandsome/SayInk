"""Audio resampling and mixing helpers for the recorder."""

from __future__ import annotations

from functools import lru_cache

import numpy as np

TARGET_SAMPLE_RATE = 16000
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


def mix_to_mono(tracks: list[np.ndarray], sample_rate: int = TARGET_SAMPLE_RATE) -> np.ndarray:
    """Resample each track to sample_rate, align length, and mix with headroom."""
    if not tracks:
        return np.array([], dtype=np.float32)
    resampled = [resample_mono(t, sample_rate, sample_rate) for t in tracks if t is not None and t.size > 0]
    if not resampled:
        return np.array([], dtype=np.float32)
    max_len = max(t.shape[0] for t in resampled)
    padded = []
    for t in resampled:
        if t.shape[0] < max_len:
            pad = np.zeros(max_len - t.shape[0], dtype=np.float32)
            padded.append(np.concatenate([t, pad]))
        else:
            padded.append(t[:max_len])
    mixed = np.zeros(max_len, dtype=np.float32)
    scale = 1.0 / len(padded)
    for t in padded:
        mixed += t * scale
    peak = float(np.max(np.abs(mixed))) if mixed.size else 0.0
    if peak > 1.0:
        mixed = (mixed / peak).astype(np.float32)
    return mixed

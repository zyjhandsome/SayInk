"""Audio resampling and mixing helpers for the recorder."""

from __future__ import annotations

import numpy as np

TARGET_SAMPLE_RATE = 16000


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
    """Linear resample mono audio to target_rate."""
    mono = to_mono(audio)
    if mono.size == 0 or source_rate <= 0:
        return mono
    if source_rate == target_rate:
        return mono
    duration = mono.shape[0] / float(source_rate)
    target_len = max(1, int(round(duration * target_rate)))
    src_idx = np.linspace(0, mono.shape[0] - 1, target_len, dtype=np.float64)
    return np.interp(src_idx, np.arange(mono.shape[0], dtype=np.float64), mono).astype(np.float32)


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

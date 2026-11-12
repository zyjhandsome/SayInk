"""Decode a local audio or video file to 16 kHz mono PCM.

WAV uses the standard library. Other formats go through Windows Media
Foundation. The source file is not copied, and no external transcoder is launched.
"""

from __future__ import annotations

import sys
import wave
from pathlib import Path

import numpy as np

from sayink.audio_utils import TARGET_SAMPLE_RATE, resample_mono

SUPPORTED_EXTENSIONS = frozenset({".wav", ".mp3", ".m4a", ".aac", ".mp4", ".mov", ".mkv", ".wma", ".flac"})


class AudioDecodeError(ValueError):
    """The file cannot be turned into meeting audio on this machine."""


def decode_audio_file(path: str | Path, *, cancel=None) -> np.ndarray:
    """Return 16 kHz mono float32 samples in ``[-1, 1]``."""
    if cancel is not None and cancel():
        raise AudioDecodeError("已取消")
    source = Path(path)
    suffix = source.suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise AudioDecodeError(f"不支持的文件类型：{suffix or '无扩展名'}")
    if not source.is_file():
        raise AudioDecodeError(f"找不到文件：{source}")
    if suffix == ".wav":
        try:
            samples, rate = _read_wav(source, cancel=cancel)
        except AudioDecodeError as exc:
            if sys.platform != "win32" or "已取消" in str(exc):
                raise
            samples, rate = _read_media_foundation(source, cancel=cancel)
    else:
        samples, rate = _read_media_foundation(source, cancel=cancel)
    if cancel is not None and cancel():
        raise AudioDecodeError("已取消")
    if samples.size == 0:
        raise AudioDecodeError("文件里没有可识别的声音")
    return resample_mono(samples, rate, TARGET_SAMPLE_RATE)


def _iter_wav_native(path: Path, cancel=None):
    """Yield ``(mono float32 at the file rate, rate)`` about one second at a time."""
    try:
        with wave.open(str(path), "rb") as handle:
            yield from _iter_open_wav(handle, cancel)
    except wave.Error as exc:
        raise AudioDecodeError(f"无法读取 WAV：{exc}") from exc


def _iter_open_wav(handle, cancel):
    rate = handle.getframerate()
    channels = handle.getnchannels()
    width = handle.getsampwidth()
    if width != 2:
        raise AudioDecodeError("WAV 需要 16 位 PCM")
    if rate <= 0:
        raise AudioDecodeError("WAV 里没有音频数据")
    step = max(int(rate), 1)
    produced = False
    while True:
        if cancel is not None and cancel():
            raise AudioDecodeError("已取消")
        frames = handle.readframes(step)
        if not frames:
            break
        produced = True
        pcm = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        if channels > 1:
            usable = (pcm.size // channels) * channels
            pcm = pcm[:usable].reshape(-1, channels).mean(axis=1)
        block = np.ascontiguousarray(pcm, dtype=np.float32)
        if block.size:
            yield block, int(rate)
    if not produced:
        raise AudioDecodeError("WAV 里没有音频数据")


def iter_wav_pcm(path: Path, cancel=None):
    """Yield about one second of 16 kHz mono float32, without keeping the whole file."""
    for block, rate in _iter_wav_native(path, cancel=cancel):
        resampled = resample_mono(block, rate, TARGET_SAMPLE_RATE)
        if resampled.size:
            yield resampled


def _read_wav(path: Path, cancel=None) -> tuple[np.ndarray, int]:
    blocks = []
    rate = TARGET_SAMPLE_RATE
    for block, file_rate in _iter_wav_native(path, cancel=cancel):
        blocks.append(block)
        rate = file_rate
    if not blocks:
        raise AudioDecodeError("WAV 里没有音频数据")
    return np.concatenate(blocks), rate


def _read_media_foundation(path: Path, *, cancel=None) -> tuple[np.ndarray, int]:
    if sys.platform != "win32":
        raise AudioDecodeError("这个格式需要 Windows Media Foundation")
    from sayink.media_foundation import AudioFoundationError, read_pcm16

    try:
        pcm, rate, channels = read_pcm16(
            path, cancel=cancel, sample_rate=TARGET_SAMPLE_RATE, channels=1,
        )
    except AudioFoundationError as exc:
        if cancel is not None and cancel():
            raise AudioDecodeError("已取消") from exc
        pcm, rate, channels = read_pcm16(path, cancel=cancel)
    samples = pcm.astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return np.ascontiguousarray(samples, dtype=np.float32), rate

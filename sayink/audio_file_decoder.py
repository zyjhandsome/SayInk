"""Decode a local audio or video file to 16 kHz mono PCM.

WAV uses the standard library. Other formats go through Windows Media
Foundation. The source file is not copied, and no external transcoder is launched.
"""

from __future__ import annotations

import struct
import sys
import wave
from pathlib import Path
from typing import Callable

import numpy as np

from sayink.audio_utils import TARGET_SAMPLE_RATE, StreamResampler, resample_mono, to_mono

SUPPORTED_EXTENSIONS = frozenset({".wav", ".mp3", ".m4a", ".aac", ".mp4", ".mov", ".mkv", ".wma", ".flac"})

_KSDATAFORMAT_SUBTYPE_TAIL = b"\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71"


class AudioDecodeError(ValueError):
    """The file cannot be turned into meeting audio on this machine."""


def decode_audio_file(
    path: str | Path,
    *,
    cancel=None,
    progress_cb: Callable[[int, str], None] | None = None,
) -> np.ndarray:
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
        chunks = list(iter_wav_pcm(source, cancel=cancel, progress_cb=progress_cb))
        samples = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
        rate = TARGET_SAMPLE_RATE
    else:
        samples, rate = _read_media_foundation(source, cancel=cancel, progress_cb=progress_cb)
    if cancel is not None and cancel():
        raise AudioDecodeError("已取消")
    if samples.size == 0:
        raise AudioDecodeError("文件里没有可识别的声音")
    return resample_mono(samples, rate, TARGET_SAMPLE_RATE)


def _decode_pcm_bytes(raw: bytes, *, fmt_tag: int, bits: int) -> np.ndarray:
    """Convert raw PCM (8/16/24/32-bit) or IEEE Float (32/64-bit) bytes to float32 [-1, 1]."""
    if fmt_tag == 1:
        if bits == 8:
            return (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
        if bits == 16:
            usable = (len(raw) // 2) * 2
            return np.frombuffer(raw[:usable], dtype="<i2").astype(np.float32) / 32768.0
        if bits == 24:
            usable = (len(raw) // 3) * 3
            if usable <= 0:
                return np.zeros(0, dtype=np.float32)
            triplets = np.frombuffer(raw[:usable], dtype=np.uint8).reshape(-1, 3)
            signed = (
                triplets[:, 0].astype(np.int32)
                | (triplets[:, 1].astype(np.int32) << 8)
                | (triplets[:, 2].astype(np.int8).astype(np.int32) << 16)
            )
            return (signed.astype(np.float32)) / 8388608.0
        if bits == 32:
            usable = (len(raw) // 4) * 4
            return (np.frombuffer(raw[:usable], dtype="<i4").astype(np.float64) / 2147483648.0).astype(np.float32)
        raise AudioDecodeError(f"不支持的 PCM 位深：{bits} 位")
    if fmt_tag == 3:
        if bits == 32:
            usable = (len(raw) // 4) * 4
            arr = np.frombuffer(raw[:usable], dtype="<f4").copy()
            return np.clip(np.nan_to_num(arr, nan=0.0, posinf=1.0, neginf=-1.0), -1.0, 1.0)
        if bits == 64:
            usable = (len(raw) // 8) * 8
            arr = np.frombuffer(raw[:usable], dtype="<f8").astype(np.float32)
            return np.clip(np.nan_to_num(arr, nan=0.0, posinf=1.0, neginf=-1.0), -1.0, 1.0)
        raise AudioDecodeError(f"不支持的浮点 WAV 位深：{bits} 位")
    raise AudioDecodeError(f"不支持的 WAV 编码格式：{fmt_tag}")


def _collapse_wav_channels(samples: np.ndarray, channels: int) -> np.ndarray:
    if channels > 1:
        usable = (samples.size // channels) * channels
        if usable <= 0:
            return np.zeros(0, dtype=np.float32)
        return np.ascontiguousarray(to_mono(samples[:usable].reshape(-1, channels)), dtype=np.float32)
    return np.ascontiguousarray(samples, dtype=np.float32)


def _iter_riff_extended_wav(
    path: Path,
    cancel=None,
    progress_cb: Callable[[int, str], None] | None = None,
):
    """Stream RIFF/WAVE files with 24/32-bit PCM, IEEE float, or WAVE_FORMAT_EXTENSIBLE headers."""
    with path.open("rb") as fh:
        header = fh.read(12)
        if len(header) < 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
            raise AudioDecodeError("不是有效的 WAV 文件")
        fmt_tag = 0
        channels = 0
        rate = 0
        bits = 0
        block_align = 0
        data_offset = -1
        data_size = 0

        while True:
            chunk_hdr = fh.read(8)
            if len(chunk_hdr) < 8:
                break
            chunk_id = chunk_hdr[:4]
            chunk_len = struct.unpack("<I", chunk_hdr[4:8])[0]
            chunk_pos = fh.tell()
            if chunk_id == b"fmt ":
                fmt_bytes = fh.read(min(chunk_len, 64))
                if len(fmt_bytes) < 16:
                    raise AudioDecodeError("WAV 格式头不完整")
                fmt_tag, channels, rate, _byte_rate, block_align, bits = struct.unpack(
                    "<HHIIHH", fmt_bytes[:16]
                )
                if fmt_tag == 0xFFFE:
                    if len(fmt_bytes) < 40 or fmt_bytes[28:40] != _KSDATAFORMAT_SUBTYPE_TAIL:
                        raise AudioDecodeError("不支持的扩展 WAV 子格式")
                    fmt_tag = struct.unpack("<I", fmt_bytes[24:28])[0]
                if data_offset >= 0:
                    break
            elif chunk_id == b"data":
                data_offset = chunk_pos
                data_size = chunk_len
                if fmt_tag:
                    break
            next_pos = chunk_pos + chunk_len + (chunk_len & 1)
            fh.seek(next_pos)

        valid_bits = (fmt_tag == 1 and bits in (8, 16, 24, 32)) or (
            fmt_tag == 3 and bits in (32, 64)
        )
        if not valid_bits or rate <= 0 or channels <= 0 or data_offset < 0:
            raise AudioDecodeError("WAV 格式需要 Media Foundation 解码")

        if data_size == 0:
            raise AudioDecodeError("WAV 里没有音频数据")
        bytes_per_sample = bits // 8
        sample_frame_bytes = channels * bytes_per_sample
        frame_bytes = max(block_align, sample_frame_bytes)
        step_frames = max(int(rate), 1)
        step_bytes = step_frames * frame_bytes
        fh.seek(data_offset)
        remaining = None if data_size == 0xFFFFFFFF else data_size
        produced = False
        read_total = 0
        last_pct = -1

        while remaining is None or remaining > 0:
            if cancel is not None and cancel():
                raise AudioDecodeError("已取消")
            to_read = step_bytes if remaining is None else min(step_bytes, remaining)
            # Align to whole frames
            to_read = (to_read // frame_bytes) * frame_bytes
            if to_read <= 0:
                break
            raw = fh.read(to_read)
            if not raw:
                break
            if remaining is not None:
                remaining -= len(raw)
            read_total += len(raw)
            usable_bytes = (len(raw) // frame_bytes) * frame_bytes
            if usable_bytes <= 0:
                break
            produced = True
            chunk_raw = raw[:usable_bytes]
            if frame_bytes > sample_frame_bytes:
                chunk_raw = (
                    np.frombuffer(chunk_raw, dtype=np.uint8)
                    .reshape(-1, frame_bytes)[:, :sample_frame_bytes]
                    .tobytes()
                )
            samples = _decode_pcm_bytes(chunk_raw, fmt_tag=fmt_tag, bits=bits)
            block = _collapse_wav_channels(samples, channels)
            if progress_cb is not None and remaining is not None and data_size > 0:
                pct = min(99, max(0, int(read_total * 100 // max(1, data_size))))
                if pct != last_pct:
                    last_pct = pct
                    sec = (read_total // frame_bytes) / float(rate)
                    progress_cb(pct, f"正在解码 WAV（{sec:.0f} 秒）")
            if block.size:
                yield block, int(rate)

        if not produced:
            raise AudioDecodeError("WAV 里没有音频数据")


def _iter_wav_native(
    path: Path,
    cancel=None,
    progress_cb: Callable[[int, str], None] | None = None,
):
    """Yield ``(mono float32 at the file rate, rate)`` about one second at a time."""
    try:
        with wave.open(str(path), "rb") as handle:
            yield from _iter_open_wav(handle, cancel, progress_cb=progress_cb)
            return
    except wave.Error:
        pass
    except AudioDecodeError as exc:
        if "已取消" in str(exc) or "没有音频数据" in str(exc):
            raise
    yield from _iter_riff_extended_wav(path, cancel=cancel, progress_cb=progress_cb)


def _iter_open_wav(
    handle,
    cancel,
    *,
    progress_cb: Callable[[int, str], None] | None = None,
):
    rate = handle.getframerate()
    channels = handle.getnchannels()
    width = handle.getsampwidth()
    if width not in (1, 2, 3, 4):
        raise AudioDecodeError(f"不支持的 WAV 采样字节宽度：{width}")
    if rate <= 0 or channels <= 0:
        raise AudioDecodeError("WAV 里没有音频数据")
    total_frames = max(0, int(handle.getnframes() or 0))
    step = max(int(rate), 1)
    produced = False
    frames_done = 0
    last_pct = -1
    while True:
        if cancel is not None and cancel():
            raise AudioDecodeError("已取消")
        frames = handle.readframes(step)
        if not frames:
            break
        produced = True
        pcm = _decode_pcm_bytes(frames, fmt_tag=1, bits=width * 8)
        block = _collapse_wav_channels(pcm, channels)
        frames_done += int(block.size)
        if progress_cb is not None and total_frames > 0:
            pct = min(99, max(0, int(frames_done * 100 // max(1, total_frames))))
            if pct != last_pct:
                last_pct = pct
                progress_cb(pct, f"正在解码 WAV（{frames_done / float(rate):.0f} 秒）")
        if block.size:
            yield block, int(rate)
    if not produced:
        raise AudioDecodeError("WAV 里没有音频数据")


def iter_wav_pcm(
    path: Path,
    cancel=None,
    *,
    progress_cb: Callable[[int, str], None] | None = None,
):
    """Yield about one second of 16 kHz mono float32 with seamless stateful resampling."""
    resampler: StreamResampler | None = None
    total_samples = 0
    try:
        for block, rate in _iter_wav_native(path, cancel=cancel, progress_cb=progress_cb):
            if rate == TARGET_SAMPLE_RATE:
                total_samples += int(block.size)
                yield block
                continue
            if resampler is None or resampler.source_rate != rate:
                if resampler is not None:
                    flushed = resampler.flush()
                    if flushed.size:
                        total_samples += int(flushed.size)
                        yield flushed
                resampler = StreamResampler(rate, TARGET_SAMPLE_RATE, trim_delay=True)
            resampled = resampler.process(block)
            if resampled.size:
                total_samples += int(resampled.size)
                yield resampled
        if resampler is not None:
            tail = resampler.flush()
            if tail.size:
                total_samples += int(tail.size)
                yield tail
        if progress_cb is not None and total_samples > 0:
            progress_cb(100, f"已完成解码（{total_samples / TARGET_SAMPLE_RATE:.0f} 秒）")
    except AudioDecodeError as exc:
        msg = str(exc)
        if sys.platform != "win32" or total_samples > 0 or "已取消" in msg or "没有音频数据" in msg:
            raise
        # Compressed WAV (ADPCM / A-law / MP3-in-WAV): fall back to Media Foundation.
        samples, rate = _read_media_foundation(path, cancel=cancel, progress_cb=progress_cb)
        mono = resample_mono(samples, rate, TARGET_SAMPLE_RATE)
        if mono.size == 0:
            raise AudioDecodeError("WAV 里没有音频数据") from exc
        step = TARGET_SAMPLE_RATE
        for offset in range(0, int(mono.size), step):
            if cancel is not None and cancel():
                raise AudioDecodeError("已取消") from exc
            yield mono[offset : offset + step]


def _read_media_foundation(
    path: Path,
    *,
    cancel=None,
    progress_cb: Callable[[int, str], None] | None = None,
) -> tuple[np.ndarray, int]:
    if sys.platform != "win32":
        raise AudioDecodeError("这个格式需要 Windows Media Foundation")
    from sayink.media_foundation import AudioFoundationError, read_pcm16

    try:
        pcm, rate, channels = read_pcm16(
            path,
            cancel=cancel,
            sample_rate=TARGET_SAMPLE_RATE,
            channels=1,
            progress_cb=progress_cb,
        )
    except AudioFoundationError as exc:
        if cancel is not None and cancel():
            raise AudioDecodeError("已取消") from exc
        try:
            pcm, rate, channels = read_pcm16(path, cancel=cancel, progress_cb=progress_cb)
        except AudioFoundationError as inner_exc:
            if cancel is not None and cancel():
                raise AudioDecodeError("已取消") from inner_exc
            raise AudioDecodeError(str(inner_exc)) from inner_exc
    samples = pcm.astype(np.float32)
    del pcm
    samples *= np.float32(1.0 / 32768.0)
    if channels > 1:
        usable = (samples.size // channels) * channels
        samples = to_mono(samples[:usable].reshape(-1, channels))
    return np.ascontiguousarray(samples, dtype=np.float32), rate

"""Pull 16-bit PCM out of a media file with Windows Media Foundation.

The source reader converts the first audio stream. Callers resample and
downmix. COM objects are released before the function returns.
"""

from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import (
    POINTER,
    Structure,
    byref,
    c_byte,
    c_long,
    c_longlong,
    c_ubyte,
    c_ulong,
    c_ushort,
    c_void_p,
    cast,
)
from pathlib import Path
from typing import Callable

import numpy as np

from sayink.audio_utils import resample_mono, to_mono

log = logging.getLogger("SayInk")

HRESULT = c_long
COINIT_MULTITHREADED = 0
MF_VERSION = 0x00020070
MF_SOURCE_READER_FIRST_AUDIO_STREAM = 0xFFFFFFFD
MF_SOURCE_READER_MEDIASOURCE = 0xFFFFFFFF
MF_SOURCE_READERF_ERROR = 0x00000001
MF_SOURCE_READERF_ENDOFSTREAM = 0x00000002
MF_SOURCE_READERF_CURRENTMEDIATYPECHANGED = 0x00000020
MF_SOURCE_READERF_STREAMTICK = 0x00000100

# Cap PTS discontinuities so non-zero stream start times or edited containers
# never allocate gigabytes of zero-padding in memory.
MAX_INITIAL_PTS_PAD_SEC = 2.0
MAX_PTS_GAP_SEC = 5.0

_PROTO_LOCK = threading.Lock()
_PROTOS_READY = False


class AudioFoundationError(RuntimeError):
    """Media Foundation could not produce PCM for this file."""


class GUID(Structure):
    _fields_ = (
        ("Data1", c_ulong),
        ("Data2", c_ushort),
        ("Data3", c_ushort),
        ("Data4", c_ubyte * 8),
    )


def _guid(data1: int, data2: int, data3: int, data4: bytes) -> GUID:
    return GUID(data1, data2, data3, (c_ubyte * 8).from_buffer_copy(data4))


_MEDIA_TAIL = bytes.fromhex("800000aa00389b71")
MF_MT_MAJOR_TYPE = _guid(0x48EBA18E, 0xF8C9, 0x4687, bytes.fromhex("bf110a74c9f96a8f"))
MF_MT_SUBTYPE = _guid(0xF7E34C9A, 0x42E8, 0x4714, bytes.fromhex("b74bcb29d72c35e5"))
MF_MT_AUDIO_NUM_CHANNELS = _guid(0x37E48BF5, 0x645E, 0x4C5B, bytes.fromhex("89deada9e29b696a"))
MF_MT_AUDIO_SAMPLES_PER_SECOND = _guid(0x5FAEEAE7, 0x0290, 0x4C31, bytes.fromhex("9e8ac534f68d9dba"))
MF_MT_AUDIO_BITS_PER_SAMPLE = _guid(0xF2DEB57F, 0x40FA, 0x4764, bytes.fromhex("aa33ed4f2d1ff669"))
MF_PD_DURATION = _guid(0x6C990D33, 0xBB8E, 0x477A, bytes.fromhex("85980d5d96fcd88a"))
MFMediaType_Audio = _guid(0x73647561, 0x0000, 0x0010, _MEDIA_TAIL)
MFAudioFormat_PCM = _guid(0x00000001, 0x0000, 0x0010, _MEDIA_TAIL)

_FRIENDLY_HRESULT_HINTS: dict[int, str] = {
    0xC00D36C4: "文件格式不受支持或内容已损坏",
    0xC00D36B4: "系统缺少解码该音频编码所需的组件",
    0xC00D3E85: "媒体文件中未找到可用的音轨",
    0xC00D5212: "系统缺少该媒体格式的解码器",
    0x80070002: "找不到指定的媒体文件",
    0x80070003: "找不到指定的媒体文件路径",
    0x80070005: "没有读取该媒体文件的权限",
    0x80070020: "媒体文件正被其他程序占用，请关闭后重试",
}


def _check(hr: int, action: str) -> None:
    if hr < 0:
        code = hr & 0xFFFFFFFF
        log.debug("Media Foundation %s 失败: 0x%08X", action, code)
        detail = _FRIENDLY_HRESULT_HINTS.get(code)
        if detail:
            raise AudioFoundationError(f"{action}失败：{detail}")
        raise AudioFoundationError(f"{action}失败，请检查音视频文件是否完整且包含有效音轨")


def _ensure_prototypes(mfplat, mfreadwrite) -> None:
    global _PROTOS_READY
    if _PROTOS_READY:
        return
    with _PROTO_LOCK:
        if _PROTOS_READY:
            return
        mfplat.MFStartup.argtypes = [c_ulong, c_ulong]
        mfplat.MFStartup.restype = HRESULT
        mfplat.MFShutdown.argtypes = []
        mfplat.MFShutdown.restype = HRESULT
        mfplat.MFCreateMediaType.argtypes = [POINTER(c_void_p)]
        mfplat.MFCreateMediaType.restype = HRESULT
        mfreadwrite.MFCreateSourceReaderFromURL.argtypes = [
            ctypes.c_wchar_p,
            c_void_p,
            POINTER(c_void_p),
        ]
        mfreadwrite.MFCreateSourceReaderFromURL.restype = HRESULT
        _PROTOS_READY = True


def _method(obj, index: int, restype, argtypes):
    table = cast(obj, POINTER(POINTER(c_void_p)))[0]
    prototype = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)
    return prototype(table[index])


def _release(obj) -> None:
    if obj:
        _method(obj, 2, c_ulong, ())(obj)


def _set_guid(media_type, key: GUID, value: GUID) -> None:
    hr = _method(media_type, 24, HRESULT, (POINTER(GUID), POINTER(GUID)))(media_type, byref(key), byref(value))
    _check(hr, "设置媒体类型")


def _set_uint32(media_type, key: GUID, value: int) -> None:
    hr = _method(media_type, 21, HRESULT, (POINTER(GUID), c_ulong))(media_type, byref(key), c_ulong(value))
    _check(hr, "设置音频参数")


def _format_of(media_type) -> tuple[int, int]:
    return (
        _get_uint32(media_type, MF_MT_AUDIO_SAMPLES_PER_SECOND),
        _get_uint32(media_type, MF_MT_AUDIO_NUM_CHANNELS),
    )


def _get_uint32(media_type, key: GUID) -> int:
    value = c_ulong()
    hr = _method(media_type, 7, HRESULT, (POINTER(GUID), POINTER(c_ulong)))(media_type, byref(key), byref(value))
    _check(hr, "读取音频参数")
    return int(value.value)


def _adapt_pcm_chunk(
    pcm: np.ndarray,
    cur_rate: int,
    cur_channels: int,
    out_rate: int,
    out_channels: int,
) -> tuple[np.ndarray, int]:
    """Adapt a decoded PCM16 chunk if mid-stream sample rate or channel count changed."""
    cur_ch = max(int(cur_channels), 1)
    out_ch = max(int(out_channels), 1)
    usable = (int(pcm.size) // cur_ch) * cur_ch
    if usable <= 0:
        return np.zeros(0, dtype=np.int16), 0
    pcm = pcm[:usable]
    if cur_rate == out_rate and cur_ch == out_ch:
        return pcm, usable // cur_ch

    flt = pcm.astype(np.float32) / 32768.0
    if cur_ch > 1:
        flt = flt.reshape(-1, cur_ch)
    mono = to_mono(flt)
    if cur_rate > 0 and out_rate > 0 and cur_rate != out_rate:
        mono = resample_mono(mono, cur_rate, out_rate)
    frames = int(mono.size)
    if frames <= 0:
        return np.zeros(0, dtype=np.int16), 0
    if out_ch > 1:
        mono = np.repeat(mono[:, None], out_ch, axis=1).reshape(-1)
    converted = np.clip(np.round(mono * 32767.0), -32768, 32767).astype(np.int16)
    return converted, frames


class _PROPVARIANT_U64(Structure):
    _fields_ = (
        ("vt", c_ushort),
        ("wReserved1", c_ushort),
        ("wReserved2", c_ushort),
        ("wReserved3", c_ushort),
        ("uhVal", ctypes.c_uint64),
        ("pad", c_ubyte * 8),
    )


def _probe_duration_100ns(reader, ole32) -> int:
    """Best-effort query of media duration in 100-ns units for progress reporting."""
    var = _PROPVARIANT_U64()
    try:
        # IMFSourceReader::GetPresentationAttribute is index 11.
        hr = _method(
            reader,
            11,
            HRESULT,
            (c_ulong, POINTER(GUID), POINTER(_PROPVARIANT_U64)),
        )(reader, c_ulong(MF_SOURCE_READER_MEDIASOURCE), byref(MF_PD_DURATION), byref(var))
        if hr >= 0 and var.vt == 21:  # VT_UI8
            return int(var.uhVal)
    except Exception:
        pass
    finally:
        try:
            ole32.PropVariantClear(byref(var))
        except Exception:
            pass
    return 0


def read_pcm16(
    path: Path,
    *,
    cancel=None,
    sample_rate: int = 0,
    channels: int = 0,
    progress_cb: Callable[[int, str], None] | None = None,
) -> tuple[np.ndarray, int, int]:
    """Return int16 samples, sample rate and channel count.

    ``sample_rate`` and ``channels`` ask Media Foundation to convert while it
    reads, so a long file is not first expanded at its original rate. ``cancel``
    is checked between samples.
    """
    ole32 = ctypes.windll.ole32
    mfplat = ctypes.windll.mfplat
    mfreadwrite = ctypes.windll.mfreadwrite
    _ensure_prototypes(mfplat, mfreadwrite)

    started = ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
    if started < 0 and (started & 0xFFFFFFFF) != 0x80010106:
        _check(started, "初始化音频组件")
    own_com = started >= 0
    mf_started = False
    reader = c_void_p()
    media_type = c_void_p()
    try:
        _check(mfplat.MFStartup(MF_VERSION, 0), "启动媒体解码服务")
        mf_started = True
        hr = mfreadwrite.MFCreateSourceReaderFromURL(str(path.resolve()), None, byref(reader))
        _check(hr, "打开媒体文件")
        hr = mfplat.MFCreateMediaType(byref(media_type))
        _check(hr, "创建媒体类型")
        _set_guid(media_type, MF_MT_MAJOR_TYPE, MFMediaType_Audio)
        _set_guid(media_type, MF_MT_SUBTYPE, MFAudioFormat_PCM)
        _set_uint32(media_type, MF_MT_AUDIO_BITS_PER_SAMPLE, 16)
        if sample_rate:
            _set_uint32(media_type, MF_MT_AUDIO_SAMPLES_PER_SECOND, int(sample_rate))
        if channels:
            _set_uint32(media_type, MF_MT_AUDIO_NUM_CHANNELS, int(channels))
        hr = _method(reader, 7, HRESULT, (c_ulong, POINTER(c_ulong), c_void_p))(
            reader, c_ulong(MF_SOURCE_READER_FIRST_AUDIO_STREAM), None, media_type,
        )
        _check(hr, "转换音频格式")
        current = c_void_p()
        hr = _method(reader, 6, HRESULT, (c_ulong, POINTER(c_void_p)))(
            reader, c_ulong(MF_SOURCE_READER_FIRST_AUDIO_STREAM), byref(current),
        )
        _check(hr, "读取当前音频格式")
        try:
            rate, channels = _format_of(current)
        finally:
            _release(current)
        cur_rate, cur_channels = int(rate), max(int(channels), 1)
        duration_100ns = _probe_duration_100ns(reader, ole32) if progress_cb is not None else 0
        last_pct = -1

        chunks: list[np.ndarray] = []
        written = 0
        origin_stamp: int | None = None
        while True:
            flags = c_ulong()
            sample = c_void_p()
            timestamp = c_longlong(-1)
            hr = _method(
                reader, 9, HRESULT,
                (c_ulong, c_ulong, POINTER(c_ulong), POINTER(c_ulong), POINTER(c_longlong), POINTER(c_void_p)),
            )(
                reader, c_ulong(MF_SOURCE_READER_FIRST_AUDIO_STREAM), 0,
                None, byref(flags), byref(timestamp), byref(sample),
            )
            _check(hr, "读取音频数据")
            if cancel is not None and cancel():
                _release(sample)
                raise AudioFoundationError("已取消")
            if flags.value & MF_SOURCE_READERF_CURRENTMEDIATYPECHANGED:
                current = c_void_p()
                type_hr = _method(reader, 6, HRESULT, (c_ulong, POINTER(c_void_p)))(
                    reader, c_ulong(MF_SOURCE_READER_FIRST_AUDIO_STREAM), byref(current),
                )
                try:
                    _check(type_hr, "读取变化后的音频格式")
                    new_rate, new_channels = _format_of(current)
                    cur_rate, cur_channels = int(new_rate), max(int(new_channels), 1)
                    if not chunks:
                        rate, channels = cur_rate, cur_channels
                finally:
                    _release(current)
            if flags.value & MF_SOURCE_READERF_ERROR:
                _release(sample)
                raise AudioFoundationError("读取音频流失败")
            usable = bool(sample) and not (flags.value & MF_SOURCE_READERF_STREAMTICK)
            if usable:
                raw_pcm = _copy_sample(sample)
                pcm, frames = _adapt_pcm_chunk(
                    raw_pcm, cur_rate, cur_channels, int(rate), max(int(channels), 1)
                )
                width = max(int(channels), 1)
                stamp = int(timestamp.value)
                if stamp >= 0 and rate > 0 and frames > 0:
                    if origin_stamp is None:
                        # Anchor positive stream start timestamps > 2s (e.g. OBS/live recordings)
                        # so we never allocate multi-GB zero arrays for non-zero PTS origins.
                        max_init_100ns = int(MAX_INITIAL_PTS_PAD_SEC * 10_000_000)
                        origin_stamp = 0 if stamp <= max_init_100ns else stamp
                    rel_stamp = max(0, stamp - origin_stamp)
                    start = rel_stamp * int(rate) // 10_000_000
                    if start > written:
                        gap = start - written
                        max_gap = max(1, int(rate * MAX_PTS_GAP_SEC))
                        if gap > max_gap:
                            # Absorb container PTS jumps above 5s into origin_stamp.
                            excess = gap - max_gap
                            origin_stamp += excess * 10_000_000 // int(rate)
                            gap = max_gap
                        chunks.append(np.zeros(gap * width, dtype=np.int16))
                        written += gap
                if frames > 0:
                    chunks.append(pcm)
                    written += frames
                if progress_cb is not None and rate > 0:
                    decoded_sec = written / float(rate)
                    if duration_100ns > 0 and stamp >= 0:
                        rel_100ns = max(0, stamp - (origin_stamp or 0))
                        pct = min(99, max(0, int(rel_100ns * 100 // max(1, duration_100ns))))
                    else:
                        pct = min(95, int(decoded_sec) % 96)
                    if pct != last_pct:
                        last_pct = pct
                        progress_cb(pct, f"正在解码音频（{decoded_sec:.0f} 秒）")
            _release(sample)
            if flags.value & MF_SOURCE_READERF_ENDOFSTREAM:
                break
        if not chunks:
            raise AudioFoundationError("媒体文件里没有音频采样")
        if progress_cb is not None and rate > 0:
            progress_cb(100, f"已完成解码（{written / float(rate):.0f} 秒）")
        return np.concatenate(chunks), rate, channels
    finally:
        _release(media_type)
        _release(reader)
        if mf_started:
            mfplat.MFShutdown()
        if own_com:
            ole32.CoUninitialize()


def _copy_sample(sample) -> np.ndarray:
    buffer = c_void_p()
    # IMFSample::ConvertToContiguousBuffer is index 41.
    hr = _method(sample, 41, HRESULT, (POINTER(c_void_p),))(sample, byref(buffer))
    _check(hr, "取出音频缓冲")
    try:
        data = POINTER(c_byte)()
        current = c_ulong()
        hr = _method(buffer, 3, HRESULT, (POINTER(POINTER(c_byte)), POINTER(c_ulong), POINTER(c_ulong)))(
            buffer, byref(data), None, byref(current),
        )
        _check(hr, "锁定音频缓冲")
        try:
            raw = ctypes.string_at(data, int(current.value))
        finally:
            _method(buffer, 4, HRESULT, ())(buffer)
        if len(raw) % 2:
            raise AudioFoundationError("PCM 字节数不是 16 位对齐")
        return np.frombuffer(raw, dtype=np.int16).copy()
    finally:
        _release(buffer)

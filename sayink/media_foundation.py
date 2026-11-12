"""Pull 16-bit PCM out of a media file with Windows Media Foundation.

The source reader converts the first audio stream. Callers resample and
downmix. COM objects are released before the function returns.
"""

from __future__ import annotations

import ctypes
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

import numpy as np

HRESULT = c_long
COINIT_MULTITHREADED = 0
MF_VERSION = 0x00020070
MF_SOURCE_READER_FIRST_AUDIO_STREAM = 0xFFFFFFFD
MF_SOURCE_READERF_ERROR = 0x00000001
MF_SOURCE_READERF_ENDOFSTREAM = 0x00000002
MF_SOURCE_READERF_CURRENTMEDIATYPECHANGED = 0x00000020
MF_SOURCE_READERF_STREAMTICK = 0x00000100


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
MFMediaType_Audio = _guid(0x73647561, 0x0000, 0x0010, _MEDIA_TAIL)
MFAudioFormat_PCM = _guid(0x00000001, 0x0000, 0x0010, _MEDIA_TAIL)


def _check(hr: int, action: str) -> None:
    if hr < 0:
        raise AudioFoundationError(f"{action}失败：0x{hr & 0xFFFFFFFF:08X}")


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


def read_pcm16(
    path: Path,
    *,
    cancel=None,
    sample_rate: int = 0,
    channels: int = 0,
) -> tuple[np.ndarray, int, int]:
    """Return int16 samples, sample rate and channel count.

    ``sample_rate`` and ``channels`` ask Media Foundation to convert while it
    reads, so a long file is not first expanded at its original rate. ``cancel``
    is checked between samples.
    """
    ole32 = ctypes.windll.ole32
    mfplat = ctypes.windll.mfplat
    mfreadwrite = ctypes.windll.mfreadwrite
    started = ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
    if started < 0 and (started & 0xFFFFFFFF) != 0x80010106:
        _check(started, "初始化 COM")
    own_com = started >= 0
    _check(mfplat.MFStartup(MF_VERSION, 0), "启动 Media Foundation")
    reader = c_void_p()
    media_type = c_void_p()
    try:
        mfreadwrite.MFCreateSourceReaderFromURL.argtypes = [ctypes.c_wchar_p, c_void_p, POINTER(c_void_p)]
        mfreadwrite.MFCreateSourceReaderFromURL.restype = HRESULT
        hr = mfreadwrite.MFCreateSourceReaderFromURL(str(path.resolve()), None, byref(reader))
        _check(hr, "打开媒体文件")
        mfplat.MFCreateMediaType.argtypes = [POINTER(c_void_p)]
        mfplat.MFCreateMediaType.restype = HRESULT
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
        _check(hr, "转换成 PCM")
        current = c_void_p()
        hr = _method(reader, 6, HRESULT, (c_ulong, POINTER(c_void_p)))(
            reader, c_ulong(MF_SOURCE_READER_FIRST_AUDIO_STREAM), byref(current),
        )
        _check(hr, "读取当前音频格式")
        try:
            rate, channels = _format_of(current)
        finally:
            _release(current)
        chunks: list[np.ndarray] = []
        written = 0
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
            _check(hr, "读取音频")
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
                    if not chunks:
                        rate, channels = _format_of(current)
                finally:
                    _release(current)
            if flags.value & MF_SOURCE_READERF_ERROR:
                _release(sample)
                raise AudioFoundationError("读取音频流失败")
            usable = bool(sample) and not (flags.value & MF_SOURCE_READERF_STREAMTICK)
            if usable:
                pcm = _copy_sample(sample)
                width = max(int(channels), 1)
                frames = int(pcm.size) // width
                stamp = int(timestamp.value)
                if stamp >= 0 and rate > 0:
                    start = int(stamp) * int(rate) // 10_000_000
                    if start > written:
                        gap = start - written
                        chunks.append(np.zeros(gap * width, dtype=np.int16))
                        written += gap
                chunks.append(pcm)
                written += frames
            _release(sample)
            if flags.value & MF_SOURCE_READERF_ENDOFSTREAM:
                break
        if not chunks:
            raise AudioFoundationError("媒体文件里没有音频采样")
        return np.concatenate(chunks), rate, channels
    finally:
        _release(media_type)
        _release(reader)
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

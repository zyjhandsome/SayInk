"""Pull 16-bit PCM out of a media file with Windows Media Foundation.

The source reader converts the first audio stream. Callers resample and
downmix. COM objects are released before the function returns.
"""

from __future__ import annotations

import ctypes
import logging
import struct
import threading
from contextlib import closing
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
from typing import Callable, Iterator

import numpy as np

from sayink.audio_utils import StreamResampler, resample_mono, to_mono

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

# Cap PTS discontinuities in flat read_pcm16() so non-zero stream start times
# or edited containers never allocate gigabytes of zero-padding in memory.
MAX_INITIAL_PTS_PAD_SEC = 2.0
MAX_PTS_GAP_SEC = 5.0
_LIVE_ORIGIN_PROGRESS_100NS = 18_000_000_000  # 1800s (30 min)
_PTS_DISCONTINUITY_100NS = 200_000  # 20 ms
_MAX_MOOV_BYTES = 64 * 1024 * 1024
_BMFF_TOP_TYPES = frozenset({b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"styp"})

_PROTO_LOCK = threading.Lock()
_PROTOS_SIG: tuple[int, int] | None = None
_OLE32 = None
_WINFUNC_CACHE: dict[tuple, object] = {}


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


def _get_ole32():
    global _OLE32
    windll_ole32 = getattr(getattr(ctypes, "windll", None), "ole32", None)
    win_dll_cls = getattr(ctypes, "WinDLL", None)
    if win_dll_cls is None or not isinstance(windll_ole32, win_dll_cls):
        return windll_ole32
    if _OLE32 is not None:
        return _OLE32
    with _PROTO_LOCK:
        if _OLE32 is None:
            dll = win_dll_cls("ole32")
            dll.CoInitializeEx.argtypes = [c_void_p, c_ulong]
            dll.CoInitializeEx.restype = HRESULT
            dll.PropVariantClear.argtypes = [POINTER(_PROPVARIANT_U64)]
            dll.PropVariantClear.restype = HRESULT
            dll.CoUninitialize.argtypes = []
            dll.CoUninitialize.restype = None
            _OLE32 = dll
    return _OLE32


def _ensure_prototypes(mfplat, mfreadwrite) -> None:
    global _PROTOS_SIG
    sig = (id(mfplat), id(mfreadwrite))
    if _PROTOS_SIG == sig:
        return
    with _PROTO_LOCK:
        if _PROTOS_SIG == sig:
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
        _PROTOS_SIG = sig


def _method(obj, index: int, restype, argtypes):
    table = cast(obj, POINTER(POINTER(c_void_p)))[0]
    key = (restype, argtypes)
    prototype = _WINFUNC_CACHE.get(key)
    if prototype is None:
        prototype = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)
        _WINFUNC_CACHE[key] = prototype
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


class _PcmStreamAdapter:
    """Adapt decoded PCM16 chunks with stateful resampling across packet boundaries."""

    def __init__(self, out_rate: int, out_channels: int) -> None:
        self.out_rate = int(out_rate)
        self.out_channels = max(int(out_channels), 1)
        self._resampler: StreamResampler | None = None

    def _encode_mono(self, mono: np.ndarray) -> tuple[np.ndarray, int]:
        frames = int(mono.size)
        if frames <= 0:
            return np.zeros(0, dtype=np.int16), 0
        out = np.repeat(mono[:, None], self.out_channels, axis=1).reshape(-1) if self.out_channels > 1 else mono
        converted = np.clip(np.round(out * 32767.0), -32768, 32767).astype(np.int16)
        return converted, frames

    def flush(self) -> tuple[np.ndarray, int]:
        if self._resampler is None:
            return np.zeros(0, dtype=np.int16), 0
        tail = self._resampler.flush()
        self._resampler = None
        return self._encode_mono(tail)

    def adapt(
        self,
        pcm: np.ndarray,
        cur_rate: int,
        cur_channels: int,
        *,
        discontinuity: bool = False,
    ) -> tuple[np.ndarray, int, np.ndarray, int]:
        cur_ch = max(int(cur_channels), 1)
        usable = (int(pcm.size) // cur_ch) * cur_ch
        pre_pcm = np.zeros(0, dtype=np.int16)
        pre_frames = 0
        need_flush = discontinuity or (
            self._resampler is not None
            and (cur_rate == self.out_rate or self._resampler.source_rate != cur_rate)
        )
        if need_flush:
            pre_pcm, pre_frames = self.flush()
        if usable <= 0:
            return pre_pcm, pre_frames, np.zeros(0, dtype=np.int16), 0
        pcm = pcm[:usable]
        if cur_rate == self.out_rate and cur_ch == self.out_channels:
            return pre_pcm, pre_frames, pcm, usable // cur_ch
        flt = pcm.astype(np.float32) * np.float32(1.0 / 32768.0)
        if cur_ch > 1:
            flt = flt.reshape(-1, cur_ch)
        mono = to_mono(flt)
        if cur_rate > 0 and self.out_rate > 0 and cur_rate != self.out_rate:
            if self._resampler is None:
                self._resampler = StreamResampler(cur_rate, self.out_rate, trim_delay=True)
            mono = self._resampler.process(mono)
        cur_pcm, cur_frames = self._encode_mono(mono)
        return pre_pcm, pre_frames, cur_pcm, cur_frames


def _iter_bmff_boxes(buf: bytes) -> Iterator[tuple[bytes, bytes]]:
    pos = 0
    total = len(buf)
    while pos + 8 <= total:
        size, box_type = struct.unpack_from(">I4s", buf, pos)
        hdr = 8
        if size == 1:
            if pos + 16 > total:
                break
            size = struct.unpack_from(">Q", buf, pos + 8)[0]
            hdr = 16
        elif size == 0:
            size = total - pos
        if size < hdr or pos + size > total:
            break
        yield box_type, buf[pos + hdr : pos + size]
        pos += size


def _parse_mvhd_timescale(body: bytes) -> int:
    if len(body) < 16:
        return 0
    ver = body[0]
    if ver == 0:
        return int(struct.unpack_from(">I", body, 12)[0])
    if ver == 1 and len(body) >= 24:
        return int(struct.unpack_from(">I", body, 20)[0])
    return 0


def _parse_elst_leading_empty(body: bytes) -> int:
    if len(body) < 8:
        return 0
    ver = body[0]
    count = int(struct.unpack_from(">I", body, 4)[0])
    pos = 8
    empty_dur = 0
    for _ in range(count):
        if ver == 1:
            if pos + 20 > len(body):
                break
            seg_dur, media_time = struct.unpack_from(">Qq", body, pos)
            pos += 20
        else:
            if pos + 12 > len(body):
                break
            seg_dur, media_time = struct.unpack_from(">Ii", body, pos)
            pos += 12
        if media_time == -1:
            empty_dur += int(seg_dur)
        else:
            break
    return empty_dur


def _parse_trak_audio_delay(trak_body: bytes) -> tuple[bool, bool, int]:
    is_audio = False
    is_enabled = True
    empty_dur = 0
    for box_type, body in _iter_bmff_boxes(trak_body):
        if box_type == b"tkhd" and len(body) >= 4:
            flags = int.from_bytes(body[1:4], "big")
            is_enabled = bool(flags & 0x1)
        elif box_type == b"mdia":
            for sub_type, sub_body in _iter_bmff_boxes(body):
                if sub_type == b"hdlr" and len(sub_body) >= 12:
                    is_audio = sub_body[8:12] == b"soun"
        elif box_type == b"edts":
            for sub_type, sub_body in _iter_bmff_boxes(body):
                if sub_type == b"elst":
                    empty_dur = _parse_elst_leading_empty(sub_body)
    return is_audio, is_enabled, empty_dur


def _parse_moov_audio_delay_100ns(moov: bytes) -> int:
    movie_timescale = 0
    fallback_delay: int | None = None
    for box_type, body in _iter_bmff_boxes(moov):
        if box_type == b"mvhd":
            movie_timescale = _parse_mvhd_timescale(body)
        elif box_type == b"trak":
            is_audio, is_enabled, empty_dur = _parse_trak_audio_delay(body)
            if is_audio:
                if is_enabled:
                    fallback_delay = empty_dur
                    break
                if fallback_delay is None:
                    fallback_delay = empty_dur
    if movie_timescale <= 0 or not fallback_delay or fallback_delay <= 0:
        return 0
    return int(round(fallback_delay * 10_000_000 / movie_timescale))


def _probe_iso_bmff_audio_delay_100ns(path: Path) -> int:
    """Read leading audio edit-list delay (elst media_time == -1) in 100-ns units.

    Windows Media Foundation's MPEG-4 source ignores audio track empty edits
    and reports ReadSample timestamps starting at 0; inspecting moov without
    reading mdat restores true container presentation timestamps.
    """
    try:
        file_size = path.stat().st_size
        if file_size < 16:
            return 0
        with path.open("rb") as fh:
            pos = 0
            while pos + 8 <= file_size:
                fh.seek(pos)
                hdr = fh.read(8)
                if len(hdr) < 8:
                    break
                size, box_type = struct.unpack(">I4s", hdr)
                hdr_len = 8
                if size == 1:
                    ext = fh.read(8)
                    if len(ext) < 8:
                        break
                    size = struct.unpack(">Q", ext)[0]
                    hdr_len = 16
                elif size == 0:
                    size = file_size - pos
                if size < hdr_len or pos + size > file_size:
                    break
                if pos == 0 and box_type not in _BMFF_TOP_TYPES:
                    return 0
                if box_type == b"moov":
                    payload_len = size - hdr_len
                    if 0 < payload_len <= _MAX_MOOV_BYTES:
                        return _parse_moov_audio_delay_100ns(fh.read(payload_len))
                    return 0
                pos += size
    except Exception:
        return 0
    return 0


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
        # IMFSourceReader::GetPresentationAttribute is COM vtable index 12
        # (IUnknown 0..2, IMFSourceReader 3..12; index 11 is 5-arg GetServiceForStream).
        hr = _method(
            reader,
            12,
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


def _parse_mvhd_duration_ms(body: bytes) -> int:
    if len(body) < 20:
        return 0
    ver = body[0]
    if ver == 0:
        timescale, duration = struct.unpack_from(">II", body, 12)
        if timescale <= 0 or duration in (0, 0xFFFFFFFF):
            return 0
        return int(duration * 1000 // timescale)
    if ver == 1 and len(body) >= 32:
        timescale, duration = struct.unpack_from(">IQ", body, 20)
        if timescale <= 0 or duration in (0, 0xFFFFFFFFFFFFFFFF):
            return 0
        return int(duration * 1000 // timescale)
    return 0


def _probe_iso_bmff_duration_ms(path: Path) -> int:
    """Best-effort header-only duration probe for MP4/M4A/MOV containers."""
    try:
        file_size = path.stat().st_size
        if file_size < 16:
            return 0
        with path.open("rb") as fh:
            pos = 0
            while pos + 8 <= file_size:
                fh.seek(pos)
                hdr = fh.read(8)
                if len(hdr) < 8:
                    break
                size, box_type = struct.unpack(">I4s", hdr)
                hdr_len = 8
                if size == 1:
                    ext = fh.read(8)
                    if len(ext) < 8:
                        break
                    size = struct.unpack(">Q", ext)[0]
                    hdr_len = 16
                elif size == 0:
                    size = file_size - pos
                if size < hdr_len or pos + size > file_size:
                    break
                if pos == 0 and box_type not in _BMFF_TOP_TYPES:
                    return 0
                if box_type == b"moov":
                    payload_len = size - hdr_len
                    if 0 < payload_len <= _MAX_MOOV_BYTES:
                        for sub_type, sub_body in _iter_bmff_boxes(fh.read(payload_len)):
                            if sub_type == b"mvhd":
                                return _parse_mvhd_duration_ms(sub_body)
                    return 0
                pos += size
    except Exception:
        return 0
    return 0


def probe_duration_ms(path: Path) -> int:
    """Best-effort media duration in milliseconds without decoding audio frames."""
    bmff_ms = _probe_iso_bmff_duration_ms(path)
    if bmff_ms > 0:
        return bmff_ms
    try:
        ole32 = _get_ole32()
        mfplat = getattr(getattr(ctypes, "windll", None), "mfplat", None)
        mfreadwrite = getattr(getattr(ctypes, "windll", None), "mfreadwrite", None)
        if ole32 is None or mfplat is None or mfreadwrite is None:
            return 0
        _ensure_prototypes(mfplat, mfreadwrite)
        started = ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
        if started < 0 and (started & 0xFFFFFFFF) != 0x80010106:
            return 0
        own_com = started >= 0
        mf_started = False
        reader = c_void_p()
        try:
            if mfplat.MFStartup(MF_VERSION, 0) < 0:
                return 0
            mf_started = True
            hr = mfreadwrite.MFCreateSourceReaderFromURL(str(path.resolve()), None, byref(reader))
            if hr < 0:
                return 0
            dur_100ns = _probe_duration_100ns(reader, ole32)
            return max(0, int(dur_100ns // 10_000)) if dur_100ns > 0 else 0
        finally:
            _release(reader)
            if mf_started:
                mfplat.MFShutdown()
            if own_com:
                ole32.CoUninitialize()
    except Exception:
        return 0


def _read_current_format(reader, action: str) -> tuple[int, int]:
    current = c_void_p()
    hr = _method(reader, 6, HRESULT, (c_ulong, POINTER(c_void_p)))(
        reader, c_ulong(MF_SOURCE_READER_FIRST_AUDIO_STREAM), byref(current),
    )
    try:
        _check(hr, action)
        rate, ch = _format_of(current)
        return int(rate), max(int(ch), 1)
    finally:
        _release(current)


def _configure_pcm_reader(mfplat, reader, sample_rate: int, channels: int) -> tuple[int, int]:
    media_type = c_void_p()
    try:
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
    finally:
        _release(media_type)
    return _read_current_format(reader, "读取当前音频格式")


def _emit_mf_progress(
    progress_cb: Callable[[int, str], None],
    *,
    last_pct: int,
    decoded_frames: int,
    out_rate: int,
    pts_100ns: int,
    first_pts_100ns: int | None,
    duration_100ns: int,
) -> int:
    if out_rate <= 0:
        return last_pct
    decoded_sec = decoded_frames / float(out_rate)
    if duration_100ns > 0 and pts_100ns >= 0:
        anchor = (
            first_pts_100ns
            if first_pts_100ns is not None
            and (first_pts_100ns >= _LIVE_ORIGIN_PROGRESS_100NS or first_pts_100ns > duration_100ns)
            else 0
        )
        rel_100ns = max(0, pts_100ns - anchor)
        pct = min(99, max(0, int(rel_100ns * 100 // max(1, duration_100ns))))
    else:
        pct = min(95, int(decoded_sec) % 96)
    if pct != last_pct:
        progress_cb(pct, f"正在解码音频（{decoded_sec:.0f} 秒）")
        return pct
    return last_pct


def iter_pcm16_timed(
    path: Path,
    *,
    cancel=None,
    sample_rate: int = 0,
    channels: int = 0,
    progress_cb: Callable[[int, str], None] | None = None,
) -> Iterator[tuple[np.ndarray, int, int, int]]:
    """Stream ``(pcm_int16_chunk, rate, channels, pts_100ns)`` from Media Foundation.

    Yields decoded packets incrementally without concatenating the full file or
    materializing zero-padding arrays. Mid-stream format changes use stateful
    resampling across packet boundaries, and ISO-BMFF edit-list audio delays
    are reflected in ``pts_100ns``.
    """
    ole32 = _get_ole32()
    mfplat = ctypes.windll.mfplat
    mfreadwrite = ctypes.windll.mfreadwrite
    _ensure_prototypes(mfplat, mfreadwrite)

    started = ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
    if started < 0 and (started & 0xFFFFFFFF) != 0x80010106:
        _check(started, "初始化音频组件")
    own_com = started >= 0
    mf_started = False
    reader = c_void_p()
    try:
        _check(mfplat.MFStartup(MF_VERSION, 0), "启动媒体解码服务")
        mf_started = True
        resolved = path.resolve()
        hr = mfreadwrite.MFCreateSourceReaderFromURL(str(resolved), None, byref(reader))
        _check(hr, "打开媒体文件")
        out_rate, out_channels = _configure_pcm_reader(mfplat, reader, sample_rate, channels)
        cur_rate, cur_channels = out_rate, out_channels
        adapter = _PcmStreamAdapter(out_rate, out_channels)
        duration_100ns = _probe_duration_100ns(reader, ole32) if progress_cb is not None else 0
        elst_delay_100ns = _probe_iso_bmff_audio_delay_100ns(resolved)
        pts_offset_100ns: int | None = None
        first_pts_100ns: int | None = None
        next_expected_pts_100ns = 0
        decoded_frames = 0
        produced = False
        last_pct = -1

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
            chunk_to_yield: tuple[np.ndarray, int, int, int] | None = None
            pre_to_yield: tuple[np.ndarray, int, int, int] | None = None
            try:
                if cancel is not None and cancel():
                    raise AudioFoundationError("已取消")
                if flags.value & MF_SOURCE_READERF_CURRENTMEDIATYPECHANGED:
                    cur_rate, cur_channels = _read_current_format(reader, "读取变化后的音频格式")
                    if not produced:
                        out_rate, out_channels = cur_rate, cur_channels
                        adapter = _PcmStreamAdapter(out_rate, out_channels)
                if flags.value & MF_SOURCE_READERF_ERROR:
                    raise AudioFoundationError("读取音频流失败")
                if bool(sample) and not (flags.value & MF_SOURCE_READERF_STREAMTICK):
                    raw_pcm = _copy_sample(sample)
                    raw_stamp = int(timestamp.value)
                    if raw_stamp >= 0:
                        if pts_offset_100ns is None:
                            pts_offset_100ns = (
                                elst_delay_100ns
                                if elst_delay_100ns > 0 and raw_stamp < (elst_delay_100ns // 2)
                                else 0
                            )
                        pts_100ns = raw_stamp + pts_offset_100ns
                    else:
                        pts_100ns = next_expected_pts_100ns if produced else (elst_delay_100ns or 0)
                    discontinuity = produced and abs(pts_100ns - next_expected_pts_100ns) > _PTS_DISCONTINUITY_100NS
                    pre_pcm, pre_frames, pcm, frames = adapter.adapt(
                        raw_pcm, cur_rate, cur_channels, discontinuity=discontinuity,
                    )
                    if pre_frames > 0:
                        pre_pts = next_expected_pts_100ns
                        pre_to_yield = (pre_pcm, out_rate, out_channels, pre_pts)
                        decoded_frames += pre_frames
                        produced = True
                        if out_rate > 0:
                            next_expected_pts_100ns = pre_pts + int(round(pre_frames * 10_000_000 / out_rate))
                    if frames > 0:
                        if first_pts_100ns is None:
                            first_pts_100ns = pts_100ns
                        chunk_to_yield = (pcm, out_rate, out_channels, pts_100ns)
                        decoded_frames += frames
                        produced = True
                        if out_rate > 0:
                            next_expected_pts_100ns = pts_100ns + int(round(frames * 10_000_000 / out_rate))
                        if progress_cb is not None:
                            last_pct = _emit_mf_progress(
                                progress_cb,
                                last_pct=last_pct,
                                decoded_frames=decoded_frames,
                                out_rate=out_rate,
                                pts_100ns=pts_100ns,
                                first_pts_100ns=first_pts_100ns,
                                duration_100ns=duration_100ns,
                            )
            finally:
                _release(sample)
            if pre_to_yield is not None:
                yield pre_to_yield
            if chunk_to_yield is not None:
                yield chunk_to_yield
            if flags.value & MF_SOURCE_READERF_ENDOFSTREAM:
                break

        tail_pcm, tail_frames = adapter.flush()
        if tail_frames > 0:
            decoded_frames += tail_frames
            produced = True
            yield (tail_pcm, out_rate, out_channels, next_expected_pts_100ns)
        if not produced:
            raise AudioFoundationError("媒体文件里没有音频采样")
        if progress_cb is not None and out_rate > 0:
            progress_cb(100, f"已完成解码（{decoded_frames / float(out_rate):.0f} 秒）")
    finally:
        _release(reader)
        if mf_started:
            mfplat.MFShutdown()
        if own_com:
            ole32.CoUninitialize()


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
    chunks: list[np.ndarray] = []
    written = 0
    origin_stamp: int | None = None
    out_rate = 0
    out_channels = 1
    with closing(
        iter_pcm16_timed(
            path,
            cancel=cancel,
            sample_rate=sample_rate,
            channels=channels,
            progress_cb=progress_cb,
        )
    ) as stream:
        for pcm, rate, ch, stamp in stream:
            out_rate = int(rate)
            out_channels = max(int(ch), 1)
            frames = int(pcm.size) // out_channels
            if stamp >= 0 and out_rate > 0 and frames > 0:
                if origin_stamp is None:
                    # Anchor positive stream start timestamps > 2s (e.g. OBS/live recordings)
                    # so flat-array callers never allocate multi-GB zero arrays.
                    max_init_100ns = int(MAX_INITIAL_PTS_PAD_SEC * 10_000_000)
                    origin_stamp = 0 if stamp <= max_init_100ns else stamp
                rel_stamp = max(0, stamp - origin_stamp)
                start = rel_stamp * out_rate // 10_000_000
                if start > written:
                    gap = start - written
                    max_gap = max(1, int(out_rate * MAX_PTS_GAP_SEC))
                    if gap > max_gap:
                        excess = gap - max_gap
                        origin_stamp += excess * 10_000_000 // out_rate
                        gap = max_gap
                    chunks.append(np.zeros(gap * out_channels, dtype=np.int16))
                    written += gap
            if frames > 0:
                chunks.append(pcm)
                written += frames
    if not chunks:
        raise AudioFoundationError("媒体文件里没有音频采样")
    return np.concatenate(chunks), out_rate, out_channels


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

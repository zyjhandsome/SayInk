"""Streaming Media Foundation decoding & container PTS timeline alignment tests (FC-03, FC-05)."""

from __future__ import annotations

import ctypes
import shutil
import struct
import subprocess
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from sayink.audio_file_decoder import (
    LIVE_PTS_ORIGIN_SEC,
    TimedAudioBlock,
    iter_media_pcm_timed,
)
from sayink.audio_utils import TARGET_SAMPLE_RATE, StreamResampler
from sayink.media_foundation import (
    MF_SOURCE_READERF_CURRENTMEDIATYPECHANGED,
    MF_SOURCE_READERF_ENDOFSTREAM,
    AudioFoundationError,
    _PcmStreamAdapter,
    _probe_iso_bmff_audio_delay_100ns,
    iter_pcm16_timed,
)
from sayink.media_import import recognize_file, segment_timed_blocks
from sayink.speaker_turns import RecognizedTurn


def _box(box_type: bytes, payload: bytes, *, large: bool = False) -> bytes:
    if large:
        return struct.pack(">I4sQ", 1, box_type, 16 + len(payload)) + payload
    return struct.pack(">I4s", 8 + len(payload), box_type) + payload


def _make_mvhd(timescale: int, *, version: int = 0) -> bytes:
    if version == 1:
        body = struct.pack(">B3sQQIQ", 1, b"\x00\x00\x00", 0, 0, timescale, timescale * 10)
    else:
        body = struct.pack(">B3sIIII", 0, b"\x00\x00\x00", 0, 0, timescale, timescale * 10)
    return _box(b"mvhd", body)


def _make_trak(
    handler: bytes,
    elst_entries: list[tuple[int, int]],
    *,
    enabled: bool = True,
    elst_version: int = 0,
) -> bytes:
    flags = b"\x00\x00\x01" if enabled else b"\x00\x00\x00"
    tkhd = _box(b"tkhd", struct.pack(">B3sIIII", 0, flags, 0, 0, 1, 0))
    hdlr = _box(b"hdlr", struct.pack(">B3sI4s12s", 0, b"\x00\x00\x00", 0, handler, b"\x00" * 12))
    mdia = _box(b"mdia", hdlr)
    parts = [tkhd, mdia]
    if elst_entries:
        rows = [struct.pack(">B3sI", elst_version, b"\x00\x00\x00", len(elst_entries))]
        for seg_dur, media_time in elst_entries:
            if elst_version == 1:
                rows.append(struct.pack(">Qqhh", seg_dur, media_time, 1, 0))
            else:
                rows.append(struct.pack(">Iihh", seg_dur, media_time, 1, 0))
        parts.append(_box(b"edts", _box(b"elst", b"".join(rows))))
    return _box(b"trak", b"".join(parts))


def test_iso_bmff_elst_audio_delay_probe_handles_v0_v1_and_mdat_after_or_before_moov(tmp_path):
    # 1) v0 mvhd/elst with moov AFTER a 64-bit largesize mdat, video at 0 + audio delayed 2.95356s
    mp4_v0 = tmp_path / "delayed_v0.mp4"
    ftyp = _box(b"ftyp", b"isom\x00\x00\x02\x00isomiso2")
    mdat = _box(b"mdat", b"\x00" * 64, large=True)
    vide_trak = _make_trak(b"vide", [(264_600, 2048)])
    disabled_soun = _make_trak(b"soun", [(44_100 * 99, -1)], enabled=False)
    enabled_soun = _make_trak(b"soun", [(130_252, -1), (89_224, 0)], enabled=True)
    moov_v0 = _box(b"moov", _make_mvhd(44_100, version=0) + vide_trak + disabled_soun + enabled_soun)
    mp4_v0.write_bytes(ftyp + mdat + moov_v0)

    delay_100ns = _probe_iso_bmff_audio_delay_100ns(mp4_v0)
    assert delay_100ns == pytest.approx(29_535_601, abs=2)

    # 2) v1 64-bit mvhd/elst with 600s leading empty edit
    mp4_v1 = tmp_path / "delayed_v1.mov"
    soun_v1 = _make_trak(b"soun", [(48_000 * 600, -1), (48_000 * 5, 1024)], elst_version=1)
    moov_v1 = _box(b"moov", _make_mvhd(48_000, version=1) + soun_v1)
    mp4_v1.write_bytes(ftyp + moov_v1 + mdat)
    assert _probe_iso_bmff_audio_delay_100ns(mp4_v1) == 6_000_000_000

    # 3) Normal AAC priming elst (media_time == 1024, no -1 empty edit) -> 0 delay
    mp4_priming = tmp_path / "priming_only.m4a"
    soun_norm = _make_trak(b"soun", [(48_000 * 3, 1024)])
    mp4_priming.write_bytes(ftyp + _box(b"moov", _make_mvhd(48_000) + soun_norm))
    assert _probe_iso_bmff_audio_delay_100ns(mp4_priming) == 0

    # 4) Non-BMFF or truncated files safely return 0
    bad = tmp_path / "corrupt.mp4"
    bad.write_bytes(b"ftyp\x00\x00\x00")
    assert _probe_iso_bmff_audio_delay_100ns(bad) == 0


def test_pcm_stream_adapter_stateful_resampling_eliminates_boundary_reset_artifacts():
    n48 = 4800
    sine48 = np.sin(2.0 * np.pi * 440.0 * np.arange(n48) / 48000.0).astype(np.float32) * 0.4
    pcm48_stereo = np.column_stack([sine48, sine48]).reshape(-1)
    pcm48_i16 = np.clip(np.round(pcm48_stereo * 32767.0), -32768, 32767).astype(np.int16)

    adapter = _PcmStreamAdapter(16000, 1)
    pieces: list[np.ndarray] = []
    # Feed tiny 240-frame (5 ms = 480 stereo samples) packets across boundaries
    for offset in range(0, pcm48_i16.size, 480):
        pre, pre_f, cur, cur_f = adapter.adapt(pcm48_i16[offset : offset + 480], 48000, 2)
        if pre_f:
            pieces.append(pre)
        if cur_f:
            pieces.append(cur)
    tail, tail_f = adapter.flush()
    if tail_f:
        pieces.append(tail)
    streamed = np.concatenate(pieces).astype(np.float32) / 32767.0

    ref = StreamResampler(48000, 16000, trim_delay=True)
    expected = np.concatenate([ref.process(pcm48_i16[::2].astype(np.float32) / 32768.0), ref.flush()])
    assert streamed.size == expected.size
    assert float(np.max(np.abs(streamed - expected))) < 2e-4


def test_iter_pcm16_timed_lifecycle_early_close_and_midstream_format_change(monkeypatch, tmp_path):
    import sayink.media_foundation as mf_mod

    events: list[str] = []
    formats = [(16000, 1), (48000, 2)]
    fmt_idx = {"i": 0}
    step = {"n": 0}

    class _FakeOle32:
        def CoInitializeEx(self, *_a):
            events.append("coinit")
            return 0

        def CoUninitialize(self):
            events.append("couninit")

        def PropVariantClear(self, *_a):
            return 0

    class _FakeMfplat:
        def MFStartup(self, *_a):
            events.append("mfstart")
            return 0

        def MFShutdown(self):
            events.append("mfstop")
            return 0

        def MFCreateMediaType(self, mt_ptr):
            ctypes.cast(mt_ptr, ctypes.POINTER(ctypes.c_void_p))[0] = 0x101
            return 0

    class _FakeMfReadWrite:
        def MFCreateSourceReaderFromURL(self, _url, _attr, rdr_ptr):
            ctypes.cast(rdr_ptr, ctypes.POINTER(ctypes.c_void_p))[0] = 0x202
            return 0

    def _fake_method(_obj, index, _restype, _argtypes):
        if index == 2:
            return lambda _o: 0
        if index in (21, 24, 7):
            return lambda *_a: 0
        if index == 6:
            def _get_mt(_o, _s, out_ptr):
                ctypes.cast(out_ptr, ctypes.POINTER(ctypes.c_void_p))[0] = 0x303
                return 0
            return _get_mt
        if index == 9:
            def _read_sample(_o, _s, _c, _a, flags_p, ts_p, sample_p):
                step["n"] += 1
                n = step["n"]
                if n == 1:
                    ctypes.cast(flags_p, ctypes.POINTER(ctypes.c_ulong))[0] = 0
                    ctypes.cast(ts_p, ctypes.POINTER(ctypes.c_longlong))[0] = 35_000_000
                    ctypes.cast(sample_p, ctypes.POINTER(ctypes.c_void_p))[0] = 1
                elif n == 2:
                    fmt_idx["i"] = 1
                    ctypes.cast(flags_p, ctypes.POINTER(ctypes.c_ulong))[0] = (
                        MF_SOURCE_READERF_CURRENTMEDIATYPECHANGED
                    )
                    ctypes.cast(ts_p, ctypes.POINTER(ctypes.c_longlong))[0] = 36_000_000
                    ctypes.cast(sample_p, ctypes.POINTER(ctypes.c_void_p))[0] = 2
                else:
                    ctypes.cast(flags_p, ctypes.POINTER(ctypes.c_ulong))[0] = MF_SOURCE_READERF_ENDOFSTREAM
                    ctypes.cast(ts_p, ctypes.POINTER(ctypes.c_longlong))[0] = 37_000_000
                    ctypes.cast(sample_p, ctypes.POINTER(ctypes.c_void_p))[0] = 0
                return 0
            return _read_sample
        raise AssertionError(f"Unexpected vtable index {index}")

    def _fake_copy(sample_id):
        val = int(ctypes.cast(sample_id, ctypes.c_void_p).value or 0)
        if val == 1:
            return np.full(1600, 4000, dtype=np.int16)
        return np.full(4800 * 2, 4000, dtype=np.int16)

    monkeypatch.setattr(mf_mod, "_ensure_prototypes", lambda *_a: None)
    monkeypatch.setattr(mf_mod, "_method", _fake_method)
    monkeypatch.setattr(mf_mod, "_format_of", lambda _mt: formats[fmt_idx["i"]])
    monkeypatch.setattr(mf_mod, "_copy_sample", _fake_copy)
    monkeypatch.setattr(
        mf_mod.ctypes,
        "windll",
        SimpleNamespace(ole32=_FakeOle32(), mfplat=_FakeMfplat(), mfreadwrite=_FakeMfReadWrite()),
    )

    # Full consumption with midstream 48kHz stereo -> 16kHz mono stateful adaptation
    out = list(iter_pcm16_timed(tmp_path / "stream.mkv", sample_rate=16000, channels=1))
    assert out[0][1:] == (16000, 1, 35_000_000)
    assert out[0][0].size == 1600
    total_adapted = sum(chunk.size for chunk, *_ in out[1:])
    assert total_adapted == pytest.approx(1600, abs=4)
    assert events == ["coinit", "mfstart", "mfstop", "couninit"]

    # Early generator close deterministically shuts down MF and COM on the caller thread
    events.clear()
    step["n"] = 0
    fmt_idx["i"] = 0
    gen = iter_pcm16_timed(tmp_path / "early.mkv", sample_rate=16000, channels=1)
    first_chunk = next(gen)
    assert first_chunk[3] == 35_000_000
    gen.close()
    assert events == ["coinit", "mfstart", "mfstop", "couninit"]


def test_iter_media_pcm_timed_fallback_stateful_resampling_and_live_origin_anchor(monkeypatch, tmp_path):
    media_file = tmp_path / "live_obs.mkv"
    media_file.write_bytes(b"stub")

    n44 = 44100 * 2
    sine44 = (0.35 * np.sin(2.0 * np.pi * 330.0 * np.arange(n44) / 44100.0)).astype(np.float32)
    stereo_i16 = np.clip(
        np.round(np.column_stack([sine44, sine44]).reshape(-1) * 32767.0), -32768, 32767
    ).astype(np.int16)

    calls: list[tuple[int, int]] = []
    # 1 hour live stream wallclock PTS (>= LIVE_PTS_ORIGIN_SEC = 1800s) -> must anchor to 0ms!
    base_pts_100ns = int((LIVE_PTS_ORIGIN_SEC + 1800.0) * 10_000_000)

    def _fake_iter_pcm16_timed(path, *, cancel=None, sample_rate=0, channels=0, progress_cb=None):
        calls.append((sample_rate, channels))
        if sample_rate == TARGET_SAMPLE_RATE:
            raise AudioFoundationError("模拟解码器不支持直接重采样")
        pkt_frames = 777  # Deliberately odd frame count to stress cross-packet stateful resampling
        pos = 0
        while pos < n44:
            end = min(n44, pos + pkt_frames)
            pts = base_pts_100ns + int(round(pos * 10_000_000 / 44100))
            yield stereo_i16[pos * 2 : end * 2], 44100, 2, pts
            pos = end

    monkeypatch.setattr("sayink.audio_file_decoder.sys.platform", "win32")
    monkeypatch.setattr("sayink.media_foundation.iter_pcm16_timed", _fake_iter_pcm16_timed)

    blocks = list(iter_media_pcm_timed(media_file))
    assert calls == [(TARGET_SAMPLE_RATE, 1), (0, 0)]
    assert blocks and blocks[0].start_sample == 0 and blocks[0].start_ms == 0

    joined = np.concatenate([b.samples for b in blocks])
    ref = StreamResampler(44100, TARGET_SAMPLE_RATE, trim_delay=True)
    expected = np.concatenate([ref.process(stereo_i16[::2].astype(np.float32) / 32768.0), ref.flush()])
    assert joined.size == expected.size
    assert float(np.max(np.abs(joined - expected))) < 2e-4


def test_segment_timed_blocks_preserves_delays_and_pts_gaps_without_zero_padding():
    tone_len = int(TARGET_SAMPLE_RATE * 1.2)
    tone = (0.25 * np.sin(2.0 * np.pi * 400.0 * np.arange(tone_len) / TARGET_SAMPLE_RATE)).astype(np.float32)

    # Leading 45s delay -> 1.2s speech -> 0.4s inline pause -> 1.0s speech -> 25s PTS jump -> 1.2s speech
    lead = 45 * TARGET_SAMPLE_RATE
    inline_pause = int(0.4 * TARGET_SAMPLE_RATE)
    short_tone = tone[: TARGET_SAMPLE_RATE]
    after_inline = lead + tone_len + inline_pause
    big_jump_start = after_inline + TARGET_SAMPLE_RATE + 25 * TARGET_SAMPLE_RATE

    streamed = [
        TimedAudioBlock(tone, lead, lead * 1000 // TARGET_SAMPLE_RATE),
        TimedAudioBlock(short_tone, after_inline, after_inline * 1000 // TARGET_SAMPLE_RATE),
        TimedAudioBlock(tone, big_jump_start, big_jump_start * 1000 // TARGET_SAMPLE_RATE),
    ]
    chunks, end_samples = segment_timed_blocks(streamed, vad="energy", vad_path=None)
    assert len(chunks) == 2
    # Utterance 1 bridges the 0.4s inline pause into a single ~2.6s chunk starting at 45.0s!
    assert chunks[0].start == lead
    assert chunks[0].stop == pytest.approx(after_inline + TARGET_SAMPLE_RATE, abs=1600)
    # Utterance 2 starts right after the 25s PTS silence jump (~72.6s) without allocating 25s of zeros!
    assert chunks[1].start == big_jump_start
    assert chunks[1].stop == big_jump_start + tone_len
    assert end_samples == big_jump_start + tone_len


def test_streaming_pipeline_consumes_lazily_and_stops_immediately_on_cancel(monkeypatch, tmp_path):
    media_file = tmp_path / "long_recording.mp4"
    media_file.write_bytes(b"stub")
    produced_packets = {"count": 0}

    def _infinite_like_packets(*_a, **_kw):
        tone_i16 = (np.sin(np.linspace(0, 80 * np.pi, 8000)) * 10000).astype(np.int16)
        for idx in range(500):  # 250 seconds if fully materialized
            produced_packets["count"] += 1
            yield tone_i16, TARGET_SAMPLE_RATE, 1, idx * 5_000_000

    monkeypatch.setattr("sayink.audio_file_decoder.sys.platform", "win32")
    monkeypatch.setattr("sayink.media_foundation.iter_pcm16_timed", _infinite_like_packets)

    stream = iter_media_pcm_timed(media_file)
    try:
        first_block = next(stream)
        assert isinstance(first_block, TimedAudioBlock)
        # Lazy streaming: reading the first 0.5s block must NOT decode all 500 packets upfront!
        assert produced_packets["count"] <= 3
    finally:
        stream.close()

    produced_packets["count"] = 0
    res = recognize_file(
        media_file,
        recognizer=object(),
        speaker_path=None,
        vad="energy",
        vad_path=None,
        cancel=lambda: produced_packets["count"] >= 4,
    )
    assert res.cancelled
    assert produced_packets["count"] <= 6


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Media Foundation integration test")
def test_real_mp4_elst_delay_and_mkv_midstream_pts_gap_in_recognize_file(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg not installed to mux container fixtures")

    # 1) QA-07 reproduction: MP4 where video starts at 0.0s and audio track is delayed by 3.5s via elst
    mp4_delayed = tmp_path / "qa07_delayed_audio.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:r=10:d=6.0",
            "-itsoffset",
            "3.5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=1.6",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            str(mp4_delayed),
        ],
        check=True,
    )

    class _StubRecognizer:
        def __init__(self) -> None:
            self.n = 0

        def decode_meeting_blocking(self, audio, *, turn_analyzer=None, cancelled=None):
            self.n += 1
            return [RecognizedTurn(0, len(audio), f"第{self.n}句", np.ones(4, np.float32), True)], ""

    res_mp4 = recognize_file(
        mp4_delayed,
        recognizer=_StubRecognizer(),
        speaker_path=None,
        vad="energy",
        vad_path=None,
        cancel=lambda: False,
    )
    assert not res_mp4.cancelled
    assert len(res_mp4.turns) == 1
    assert 3350 <= res_mp4.turns[0].offset_ms <= 3650
    assert 4900 <= res_mp4.audio_ms <= 5300

    # 2) Real container with >5.0s (12.0s) mid-stream PTS silence jump without zero-array bloat
    mkv_gap = tmp_path / "pts_jump_12s.mkv"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=2.4",
            "-af",
            "asetpts=if(lt(T\\,1.2)\\,PTS\\,PTS+12/TB)",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            str(mkv_gap),
        ],
        check=True,
    )

    timed_blocks = list(iter_media_pcm_timed(mkv_gap))
    total_decoded_samples = sum(b.samples.size for b in timed_blocks)
    # Only ~2.4s of audio samples materialized in RAM (NOT 14.4s with 12s of zeros!)
    assert total_decoded_samples < int(TARGET_SAMPLE_RATE * 3.2)

    res_gap = recognize_file(
        mkv_gap,
        recognizer=_StubRecognizer(),
        speaker_path=None,
        vad="energy",
        vad_path=None,
        cancel=lambda: False,
    )
    assert not res_gap.cancelled
    assert len(res_gap.turns) == 2
    assert res_gap.turns[0].offset_ms <= 200
    assert 13_000 <= res_gap.turns[1].offset_ms <= 13_500
    assert 14_200 <= res_gap.audio_ms <= 14_700

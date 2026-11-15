"""Local audio decoding stays inside the process and never calls ffmpeg."""
from __future__ import annotations

import ctypes
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

from sayink.audio_file_decoder import SUPPORTED_EXTENSIONS, AudioDecodeError, decode_audio_file
from sayink.audio_utils import TARGET_SAMPLE_RATE


def _write_wav(path: Path, samples: np.ndarray, rate: int, *, channels: int = 1) -> None:
    pcm = np.clip(samples, -1, 1)
    frames = (pcm * 32767).astype(np.int16).tobytes()
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(frames)


def test_wav_decodes_to_16k_mono_without_copying_the_source(tmp_path):
    source = tmp_path / "clip.wav"
    tone = np.sin(2 * np.pi * 440 * np.arange(16000) / 16000).astype(np.float32) * 0.2
    _write_wav(source, tone, 16000)
    before = source.read_bytes()
    decoded = decode_audio_file(source)
    assert decoded.dtype == np.float32
    assert decoded.shape == (TARGET_SAMPLE_RATE,)
    assert source.read_bytes() == before
    assert np.max(np.abs(decoded)) > 0.05


def test_stereo_wav_is_downmixed(tmp_path):
    left = np.full(8000, 0.5, dtype=np.float32)
    right = np.full(8000, -0.5, dtype=np.float32)
    interleaved = np.column_stack((left, right)).reshape(-1)
    path = tmp_path / "stereo.wav"
    _write_wav(path, interleaved, 16000, channels=2)
    decoded = decode_audio_file(path)
    assert decoded.shape == (8000,)
    assert np.max(np.abs(decoded)) < 0.02


def test_unknown_extension_is_rejected(tmp_path):
    path = tmp_path / "notes.docx"
    path.write_bytes(b"not audio")
    with pytest.raises(AudioDecodeError, match="不支持"):
        decode_audio_file(path)


def test_decoder_does_not_mention_ffmpeg():
    root = Path(__file__).resolve().parents[1]
    text = (root / "sayink" / "audio_file_decoder.py").read_text(encoding="utf-8")
    foundation = (root / "sayink" / "media_foundation.py").read_text(encoding="utf-8")
    assert "ffmpeg" not in text.lower()
    assert "ffmpeg" not in foundation.lower()
    assert ".mp3" in SUPPORTED_EXTENSIONS and ".m4a" in SUPPORTED_EXTENSIONS


@pytest.mark.skipif(sys.platform != "win32", reason="Media Foundation is the Windows decoder")
def test_media_foundation_reads_pcm_from_a_wav(tmp_path):
    from sayink.media_foundation import read_pcm16

    source = tmp_path / "clip.wav"
    tone = np.sin(2 * np.pi * 220 * np.arange(8000) / 8000).astype(np.float32) * 0.25
    _write_wav(source, tone, 8000)
    pcm, rate, channels = read_pcm16(source)
    assert rate == 8000
    assert channels == 1
    assert pcm.dtype == np.int16
    assert pcm.size == 8000
    assert np.max(np.abs(pcm)) > 1000
    converted, converted_rate, converted_channels = read_pcm16(source, sample_rate=16000, channels=1)
    assert converted_rate == 16000 and converted_channels == 1
    assert abs(converted.size - 16000) <= 8


def test_probe_duration_uses_com_vtable_index_12(monkeypatch):
    import sayink.media_foundation as mf

    seen_indices: list[int] = []
    cleared: list[int] = []

    def _fake_method(_obj, index: int, _restype, _argtypes):
        seen_indices.append(index)
        assert index != 11, "vtable index 11 is 5-arg GetServiceForStream and corrupts the x64 stack"

        def _get_presentation_attr(_reader, stream_idx, _guid_ptr, var_ptr):
            assert int(stream_idx.value) == mf.MF_SOURCE_READER_MEDIASOURCE
            prop = ctypes.cast(var_ptr, ctypes.POINTER(mf._PROPVARIANT_U64))[0]
            prop.vt = 21  # VT_UI8
            prop.uhVal = 25_000_000
            return 0

        return _get_presentation_attr

    class _FakeOle32:
        def PropVariantClear(self, _var_ptr):
            cleared.append(1)
            return 0

    monkeypatch.setattr(mf, "_method", _fake_method)
    duration = mf._probe_duration_100ns(ctypes.c_void_p(0x1234), _FakeOle32())
    assert seen_indices == [12]
    assert duration == 25_000_000
    assert cleared == [1]


@pytest.mark.skipif(sys.platform != "win32", reason="Media Foundation is the Windows decoder")
def test_media_foundation_progress_cb_uses_vtable_12_on_real_wav_and_mp4(tmp_path, monkeypatch):
    import sayink.media_foundation as mf
    from sayink.media_foundation import read_pcm16

    orig_method = mf._method
    called_indices: list[int] = []

    def _spy_method(obj, index: int, restype, argtypes):
        called_indices.append(index)
        assert index != 11, "Must never call vtable index 11 (GetServiceForStream)"
        return orig_method(obj, index, restype, argtypes)

    monkeypatch.setattr(mf, "_method", _spy_method)

    wav_path = tmp_path / "progress_clip.wav"
    n_samples = int(16000 * 1.5)
    tone = (np.sin(2 * np.pi * 330 * np.arange(n_samples) / 16000) * 0.3).astype(np.float32)
    _write_wav(wav_path, tone, 16000)

    wav_progress: list[tuple[int, str]] = []
    pcm, rate, ch = read_pcm16(
        wav_path,
        sample_rate=16000,
        channels=1,
        progress_cb=lambda pct, msg: wav_progress.append((pct, msg)),
    )
    assert 12 in called_indices
    assert rate == 16000 and ch == 1 and abs(pcm.size - n_samples) <= 16
    assert wav_progress and wav_progress[-1][0] == 100
    assert any(0 <= pct < 100 for pct, _ in wav_progress)

    wav_dec_progress: list[tuple[int, str]] = []
    decoded_wav = decode_audio_file(
        wav_path,
        progress_cb=lambda pct, msg: wav_dec_progress.append((pct, msg)),
    )
    assert decoded_wav.size == n_samples
    assert wav_dec_progress and wav_dec_progress[-1][0] == 100

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        mp4_path = tmp_path / "progress_clip.mp4"
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=64x64:r=10:d=2.0",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000:duration=2.0",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                str(mp4_path),
            ],
            check=True,
        )
        called_indices.clear()
        mp4_progress: list[tuple[int, str]] = []
        decoded_mp4 = decode_audio_file(
            mp4_path,
            progress_cb=lambda pct, msg: mp4_progress.append((pct, msg)),
        )
        assert 12 in called_indices
        assert 11 not in called_indices
        assert mp4_progress and mp4_progress[-1][0] == 100
        assert abs(decoded_mp4.size - 32000) <= 2048
        assert float(np.max(np.abs(decoded_mp4))) > 0.1

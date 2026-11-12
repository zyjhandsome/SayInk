"""Local audio decoding stays inside the process and never calls ffmpeg."""
from __future__ import annotations

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

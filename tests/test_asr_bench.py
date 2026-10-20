"""tools/asr_bench.py scores what the app would output, case errors included."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from tools.asr_bench import (
    build_parser,
    hotword_hits,
    recognize,
    segment_meeting,
    transcribe,
    split_speaker_turns,
)
from sayink.speaker_turns import SpeakerTurn


class _FakeRecognizer:
    def __init__(self, text: str):
        self._text = text

    def create_stream(self):
        return SimpleNamespace(accept_waveform=lambda rate, audio: None, result=SimpleNamespace(text=self._text))

    def decode_stream(self, stream):
        pass


def test_hit_rate_ignores_case_but_spelling_rate_does_not():
    reference, hypothesis = "代码在 GitHub 上", "代码在GITHUB上"
    assert hotword_hits(["GitHub"], reference, hypothesis) == (1, 1)
    assert hotword_hits(["GitHub"], reference, hypothesis, exact_case=True) == (0, 1)
    assert hotword_hits(["GitHub"], reference, "代码在GitHub上", exact_case=True) == (1, 1)


def test_recognize_applies_the_glossary_spelling_like_the_app():
    audio = np.zeros(1600, dtype=np.float32)
    assert recognize(_FakeRecognizer("代码在GITHUB上"), audio, 16000, terms=["GitHub"]) == "代码在GitHub上"
    assert recognize(_FakeRecognizer("代码在GITHUB上"), audio, 16000) == "代码在GITHUB上"


def _tone(seconds: float, amplitude: float = 0.2) -> np.ndarray:
    n = int(16000 * seconds)
    t = np.arange(n) / 16000
    return (amplitude * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def _silence(seconds: float) -> np.ndarray:
    return np.zeros(int(16000 * seconds), dtype=np.float32)


def test_energy_vad_splits_a_meeting_pause_into_two_segments():
    audio = np.concatenate([
        _silence(0.5), _tone(1.0), _silence(2.0), _tone(1.0), _silence(2.0),
    ])
    segments, used = segment_meeting(audio, 16000, vad="energy")
    assert used == "energy"
    assert len(segments) == 2


def test_off_decodes_the_whole_clip_as_one_segment():
    audio = np.concatenate([_tone(0.5), _silence(2.0), _tone(0.5)])
    segments, used = segment_meeting(audio, 16000, vad="off")
    assert used == "off"
    assert len(segments) == 1
    assert segments[0].shape[0] == audio.shape[0]


def test_non_16k_audio_is_resampled_before_segmentation():
    audio = np.zeros(22050, dtype=np.float32)
    segments, used = segment_meeting(audio, 22050, vad="off")
    assert used == "off"
    assert segments[0].shape[0] == 16000


def test_missing_silero_falls_back_to_energy(monkeypatch):
    monkeypatch.setattr("tools.asr_bench.aux_model_path", lambda _model_id: None)
    audio = np.concatenate([_silence(0.3), _tone(1.0), _silence(2.0)])
    segments, used = segment_meeting(audio, 16000, vad="silero")
    assert used == "energy"
    assert len(segments) == 1


def test_transcribe_decodes_each_segment_and_joins_text():
    audio = np.concatenate([
        _silence(0.5), _tone(1.0), _silence(2.0), _tone(1.0), _silence(2.0),
    ])

    class _Counting:
        def __init__(self):
            self.calls = 0

        def create_stream(self):
            self.calls += 1
            n = self.calls
            return SimpleNamespace(
                accept_waveform=lambda rate, block: None,
                result=SimpleNamespace(text=f"第{n}段"),
            )

        def decode_stream(self, stream):
            pass

    text, used, count = transcribe(_Counting(), audio, 16000, vad="energy")
    assert used == "energy"
    assert count == 2
    assert text == "第1段第2段"


def test_vad_flag_defaults_to_the_meeting_segmenter():
    args = build_parser().parse_args(["--data", "bench"])
    assert args.vad == "silero"


def test_unknown_vad_is_rejected():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--data", "bench", "--vad", "webrtc"])


def test_turn_detection_flag_defaults_to_available_model_and_can_be_disabled():
    assert build_parser().parse_args(["--data", "bench"]).speaker_turns == "auto"
    assert build_parser().parse_args(["--data", "bench", "--speaker-turns", "off"]).speaker_turns == "off"


def test_experimental_activity_requires_an_explicit_benchmark_option():
    assert build_parser().parse_args(["--data", "bench"]).speaker_activity == "off"
    assert build_parser().parse_args(["--data", "bench", "--speaker-activity", "auto"]).speaker_activity == "auto"


def test_asr_benchmark_decodes_each_detected_turn_like_the_app():
    audio = np.concatenate([_tone(1), _tone(1, 0.3)])

    def split(samples, **kwargs):
        return [SpeakerTurn(0, 16000, np.array([1, 0]), True),
                SpeakerTurn(16000, len(samples), np.array([0, 1]), True)]

    text, used, count = transcribe(_FakeRecognizer("独立发言"), audio, 16000, vad="off", turn_analyzer=split)
    assert count == 2 and used == "off"
    assert text == "独立发言独立发言"


def test_failed_turn_analysis_keeps_every_sample_in_benchmark_too():
    audio = _tone(2)

    def broken(samples, **kwargs):
        raise RuntimeError("analysis failed")

    parts = split_speaker_turns([audio], broken)
    assert len(parts) == 1 and np.array_equal(parts[0], audio)

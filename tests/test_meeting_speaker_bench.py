"""Replay real-clip manifests without touching app state or downloading models."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from tools.meeting_speaker_bench import load_manifest, run_comparison


class Embedder:
    name = "fake"
    threshold = 0.8
    sticky_threshold = 0.65

    def __call__(self, audio):
        return np.array([1, 0], dtype=np.float32) if len(audio) >= 16000 else np.zeros(2, dtype=np.float32)


def test_replay_preserves_short_unknowns_and_reports_coverage_and_actual_backend(monkeypatch):
    def load(path):
        n = 16000 if path.name == "long.wav" else 4000
        t = np.arange(n, dtype=np.float32) / 16000
        return 0.2 * np.sin(2 * np.pi * 180 * t), 16000

    monkeypatch.setattr("tools.meeting_speaker_bench.load_wav", load)
    clips = [dict(audio=Path("long.wav"), speaker="A", start_ms=0, route="", case="returning"),
             dict(audio=Path("short.wav"), speaker="B", start_ms=2000, route="", case="short")]
    report = run_comparison(clips, Embedder())
    assert report["backend"] == "fake"
    assert report["policies"]["stable"]["coverage"] == 0.5
    assert report["policies"]["legacy"]["merge_rate"] == 1
    assert report["clips"][1]["stable"] == 0 and report["clips"][1]["legacy"] == 1
    assert report["policies"]["stable"]["cases"]["short"]["unknown_segments"] == 1


def test_manifest_resolves_relative_clips_and_preserves_large_gaps(tmp_path):
    path = tmp_path / "meeting.json"
    path.write_text(json.dumps({"clips": [
        dict(audio="one.wav", speaker="A", start_ms=0),
        dict(audio="two.wav", speaker="A", start_ms=1_800_000),
    ]}), encoding="utf-8")
    clips = load_manifest(path)
    assert clips[0]["audio"] == tmp_path / "one.wav"
    assert clips[1]["start_ms"] == 1_800_000


@pytest.mark.parametrize("clips", [[], [{"audio": "a.wav", "speaker": "A", "start_ms": -1}],
                                   [{"audio": "a.wav", "start_ms": 1}],
                                   [{"audio": "a.wav", "speaker": "A", "start_ms": 1, "route": "bad"}],
                                   [{"audio": "a.wav", "speaker": "A", "start_ms": 20},
                                    {"audio": "b.wav", "speaker": "B", "start_ms": 10}]])
def test_invalid_manifest_is_rejected_instead_of_silently_changing_test_conditions(tmp_path, clips):
    path = tmp_path / "meeting.json"
    path.write_text(json.dumps({"clips": clips}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_manifest(path)

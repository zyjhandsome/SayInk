"""Change-boundary metrics count false cuts as well as missed turns."""
from __future__ import annotations

import json

import numpy as np
import pytest

from tools.meeting_turn_bench import evaluate_recordings, score_boundaries
from tests.test_speaker_turns import Embedder, voice


def test_exact_and_tolerant_boundaries_match_one_to_one():
    result = score_boundaries([3000, 6000], [3100, 6100, 6200], tolerance_ms=200)
    assert result["matched"] == 2 and result["extra"] == 1 and result["missed"] == 0
    assert result["precision"] == pytest.approx(2 / 3) and result["recall"] == 1
    assert result["mean_error_ms"] == 100


def test_never_splitting_is_not_reported_as_success_for_a_multi_speaker_clip():
    result = score_boundaries([3000], [])
    assert result["recall"] == result["f1"] == 0 and result["missed"] == 1
    assert score_boundaries([], [3000])["extra"] == 1
    assert score_boundaries([], [3000])["f1"] == 0
    assert score_boundaries([], [])["f1"] == 1


@pytest.mark.parametrize("reference,predicted,tolerance", [([np.nan], [], 500), ([0], [], 500),
                                                          ([], [], -1), ([], [], np.inf)])
def test_invalid_metric_inputs_are_rejected(reference, predicted, tolerance):
    with pytest.raises(ValueError):
        score_boundaries(reference, predicted, tolerance_ms=tolerance)


def test_replay_reports_raw_boundaries_and_actual_backend(tmp_path, monkeypatch):
    model = Embedder()
    model.name = "fake"
    audio = np.concatenate([voice(1), voice(2), voice(1)])
    monkeypatch.setattr("tools.meeting_turn_bench.load_wav", lambda path: (audio, 16000))
    manifest = tmp_path / "meeting.json"
    manifest.write_text(json.dumps({"recordings": [
        {"audio": "a-b-a.wav", "boundaries_ms": [3200, 6400], "case": "rapid"},
    ]}), encoding="utf-8")
    report = evaluate_recordings(manifest, model)
    assert report["backend"] == "fake" and report["matched"] == 2
    assert report["missed"] == report["extra"] == 0
    assert len(report["files"][0]["predicted_ms"]) == 2
    assert report["files"][0]["case"] == "rapid"

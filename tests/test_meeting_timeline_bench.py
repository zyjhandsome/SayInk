"""Timeline scoring keeps abstention, speaker errors and overlap distinct."""
import json

import numpy as np
import pytest

from tools import meeting_timeline_bench as bench


def test_permuted_speaker_ids_are_scored_as_the_same_people():
    gold = [(0, 1000, "A"), (1000, 2000, "B"), (2000, 3000, "A")]
    pred = [(0, 1000, "9"), (1000, 2000, "7"), (2000, 3000, "9")]
    report = bench.score_timeline(gold, pred, duration_ms=3000)
    assert report["single_correct_rate"] == 1
    assert report["short_turns_recovered"] == 3
    assert report["mapping"] == {"7": "B", "9": "A"}


def test_split_groups_cannot_both_map_to_one_person():
    report = bench.score_timeline([(0, 1000, "A"), (1000, 2000, "A")],
                                  [(0, 1000, "1"), (1000, 2000, "2")], duration_ms=2000)
    assert report["correct_single_ms"] == 1000 and report["wrong_single_ms"] == 1000
    assert report["single_coverage"] == 1 and report["split_people"] == 1


def test_all_unknown_cannot_improve_recall_or_hide_missed_short_turns():
    report = bench.score_timeline([(0, 800, "A"), (800, 1600, "B")],
                                  [(0, 1600, "0")], duration_ms=1600)
    assert report["single_correct_rate"] == 0 and report["single_precision"] is None
    assert report["unknown_single_ms"] == 1600 and report["short_turns_missed"] == 2


def test_overlap_and_non_speech_are_reported_outside_single_speaker_precision():
    report = bench.score_timeline([(0, 2000, "A"), (1000, 3000, "B")],
                                  [(0, 4000, "1")], duration_ms=4000)
    assert report["single_speaker_ms"] == 2000 and report["overlap_ms"] == 1000
    assert report["overlap_assigned_ms"] == 1000 and report["non_speech_assigned_ms"] == 1000
    assert report["correct_single_ms"] == report["wrong_single_ms"] == 1000
    assert report["merged_groups"] == 1


def test_missing_prediction_audio_counts_as_unknown_not_as_silence():
    report = bench.score_timeline([(0, 2000, "A")], [(0, 1000, "1")], duration_ms=2000)
    assert report["single_coverage"] == 0.5 and report["unknown_single_ms"] == 1000


@pytest.mark.parametrize("pred", [[(0, 1200, "1"), (1000, 2000, "1")],
                                  [(0, 2100, "1")], [(0, float("nan"), "1")]])
def test_invalid_or_overlapping_predictions_are_rejected(pred):
    with pytest.raises(ValueError):
        bench.score_timeline([(0, 2000, "A")], pred, duration_ms=2000)


def test_der_is_zero_for_a_permuted_perfect_timeline_and_counts_unknown_as_miss():
    gold = [(0, 1000, "A"), (1000, 2000, "B"), (2000, 3000, "A")]
    perfect = bench.score_der(gold, [(0, 1000, "9"), (1000, 2000, "7"), (2000, 3000, "9")], duration_ms=3000)
    assert perfect["der"] == 0 and perfect["mapping"] == {"7": "B", "9": "A"}
    assert perfect["turns"][">=3s"] == {"turns": 0, "correct": 0, "wrong": 0, "missed": 0}
    assert perfect["turns"]["<1.6s"] == {"turns": 3, "correct": 3, "wrong": 0, "missed": 0}
    # Unknown over the second sentence: missed speech, not confusion, and that turn is "missed".
    partial = bench.score_der(gold, [(0, 1000, "1"), (1000, 2000, "0"), (2000, 3000, "1")], duration_ms=3000)
    assert partial["confusion"] == 0 and partial["false_alarm"] == 0
    assert 0.2 < partial["miss"] == partial["der"] < 0.4   # one of three seconds, less the collar
    assert partial["turns"]["<1.6s"] == {"turns": 3, "correct": 2, "wrong": 0, "missed": 1}


def test_der_collar_ignores_boundary_jitter_but_not_a_wrong_speaker():
    gold = [(0, 2000, "A"), (2000, 4000, "B")]
    jitter = bench.score_der(gold, [(0, 2200, "1"), (2200, 4000, "2")], duration_ms=4000)
    assert jitter["der"] == 0
    wrong = bench.score_der(gold, [(0, 2000, "1"), (2000, 2800, "1"), (2800, 4000, "2")], duration_ms=4000)
    assert 0.1 < wrong["confusion"] == wrong["der"] < 0.2 and wrong["miss"] == 0  # 800 ms less the collar
    assert wrong["turns"][">=3s"] == {"turns": 0, "correct": 0, "wrong": 0, "missed": 0}
    assert wrong["turns"]["<3s"] == {"turns": 2, "correct": 2, "wrong": 0, "missed": 0}  # majority still right


def test_der_overlap_needs_two_labels_and_a_split_person_is_wrong_for_one_group():
    overlap = bench.score_der([(0, 2000, "A"), (1000, 3000, "B")], [(0, 3000, "1")], duration_ms=3000)
    # One of the two overlapping speakers is always missing; nothing is a false alarm.
    assert overlap["false_alarm"] == 0 and overlap["miss"] > 0
    split = bench.score_der([(0, 1000, "A"), (1000, 2000, "A")], [(0, 1000, "1"), (1000, 2000, "2")], duration_ms=2000)
    assert split["mapping"] in ({"1": "A", "2": None}, {"1": None, "2": "A"})
    assert abs(split["confusion"] - 0.5) < 0.2 and split["predicted_speakers"] == 2


def test_reference_turns_merge_close_segments_of_one_speaker():
    turns = bench.reference_turns([(0, 500, "A"), (700, 1200, "A"), (1600, 2000, "A"), (2000, 2500, "B")])
    assert turns == [(0, 1200, "A"), (1600, 2000, "A"), (2000, 2500, "B")]


def test_rttm_requires_explicit_recording_selection_when_multiple_uris(tmp_path):
    path = tmp_path / "gold.rttm"
    path.write_text("SPEAKER a 1 0.000 0.800 <NA> <NA> A <NA> <NA>\n"
                    "SPEAKER b 1 0.800 0.600 <NA> <NA> B <NA> <NA>\n")
    with pytest.raises(ValueError):
        bench.read_rttm(path)
    assert bench.read_rttm(path, uri="a") == [(0, 800, "A")]


def test_audio_pipeline_report_uses_real_sample_times_without_vad_reconstruction(tmp_path, monkeypatch):
    from tests.test_speaker_turns import Embedder, voice

    audio = np.concatenate((voice(1), voice(2), voice(1)))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"recordings": [{"audio": "x.wav", "rttm": "gold.rttm"}]}))
    (tmp_path / "gold.rttm").write_text("SPEAKER x 1 0.000 3.200 <NA> <NA> A <NA> <NA>\n"
                                      "SPEAKER x 1 3.200 3.200 <NA> <NA> B <NA> <NA>\n"
                                      "SPEAKER x 1 6.400 3.200 <NA> <NA> A <NA> <NA>\n")
    monkeypatch.setattr(bench, "load_wav", lambda path: (audio, 16000))
    model = Embedder()
    model.name, model.sticky_threshold, model.cluster_threshold = "campplus", 0.45, 0.6
    result = bench.evaluate_recordings(manifest, model)
    assert result["files"][0]["single_correct_rate"] > 0.95
    assert result["files"][0]["predicted"][-1][1] == 9600
    assert result["files"][0]["finalized"]["single_correct_rate"] > 0.95
    assert result["files"][0]["finalized"]["relabelled"] == 0
    assert result["files"][0]["der"]["der"] < 0.05 and result["files"][0]["finalized"]["der"]["der"] < 0.05


def test_finalize_keeps_live_labels_for_a_one_speaker_recording(tmp_path, monkeypatch):
    """The app only re-clusters with two or more speakers; the bench used to
    fold every turn of a single talker to unknown and score ~100 % miss."""
    from tests.test_speaker_turns import Embedder, voice

    audio = np.concatenate((voice(1), voice(1)))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"recordings": [{"audio": "x.wav", "rttm": "gold.rttm"}]}))
    (tmp_path / "gold.rttm").write_text("SPEAKER x 1 0.000 6.400 <NA> <NA> A <NA> <NA>\n")
    monkeypatch.setattr(bench, "load_wav", lambda path: (audio, 16000))
    model = Embedder()
    model.name, model.sticky_threshold, model.cluster_threshold = "campplus", 0.45, 0.6
    entry = bench.evaluate_recordings(manifest, model)["files"][0]
    live = [label for _s, _e, label in entry["predicted"]]
    final = [label for _s, _e, label in entry["finalized"]["predicted"]]
    assert live and all(label != "0" for label in live)
    assert final == live and entry["finalized"]["relabelled"] == 0


def test_bench_sessions_are_built_the_way_the_app_builds_them():
    from sayink.meeting_speakers import MeetingSpeakerSession
    from sayink.speaker_embedding import STICKY_GAP_MS
    from tests.test_speaker_turns import Embedder

    model = Embedder()
    model.name, model.sticky_threshold = "campplus", 0.45
    session = MeetingSpeakerSession.for_embedder(model, keep_history=False)
    assert session._sticky_gap_ms == STICKY_GAP_MS
    assert session._sticky_threshold == pytest.approx(0.45)
    assert session.threshold == pytest.approx(model.threshold)
    assert session._cross_route_matching is True and session._keep_history is False
    model.name = "mel"
    assert MeetingSpeakerSession.for_embedder(model)._cross_route_matching is False


def test_silero_segmenter_keeps_file_offsets_and_finalize_rescores_the_timeline(tmp_path, monkeypatch):
    """The real meeting VAD may drop silence and merge pieces; every emitted
    segment is still placed at its true file offset, and the end-of-session
    pass is scored next to the live labels."""
    from tests.test_speaker_turns import Embedder, voice
    from sayink import vad_segmenter
    from sayink.vad_segmenter import EnergyGate

    quiet = np.zeros(2 * 16000, dtype=np.float32)
    audio = np.concatenate((quiet, voice(1), quiet, voice(2), quiet, voice(1)))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"recordings": [{"audio": "x.wav", "rttm": "gold.rttm"}]}))
    (tmp_path / "gold.rttm").write_text("SPEAKER x 1 2.000 3.200 <NA> <NA> A <NA> <NA>\n"
                                      "SPEAKER x 1 7.200 3.200 <NA> <NA> B <NA> <NA>\n"
                                      "SPEAKER x 1 12.400 3.200 <NA> <NA> A <NA> <NA>\n")
    monkeypatch.setattr(bench, "load_wav", lambda path: (audio, 16000))

    class SileroLikeGate(EnergyGate):
        name = "silero"

    # The bench builds its segmenter through the shared factory, so the gate
    # is patched where that factory looks it up.
    monkeypatch.setattr(vad_segmenter, "make_speech_gate", lambda kind, segmenter, **kw: SileroLikeGate(segmenter))
    model = Embedder()
    model.name, model.sticky_threshold, model.cluster_threshold = "campplus", 0.45, 0.6
    result = bench.evaluate_recordings(manifest, model, segmenter="silero")
    entry = result["files"][0]
    assert result["segmenter"] == "silero" and result["chunk_s"] is None
    assert entry["single_correct_rate"] > 0.9 and entry["finalized"]["single_correct_rate"] > 0.9
    # Pre-roll and the silence hold are kept around each utterance, nothing more.
    assert entry["non_speech_assigned_ms"] < 3 * 1000
    starts = sorted(start for start, _stop, _label in entry["predicted"])
    assert len(starts) == 3 and all(abs(start - gold) <= 500 for start, gold in zip(starts, (2000, 7200, 12400), strict=True))


def test_finalize_recovers_a_single_known_voice_like_the_app(tmp_path, monkeypatch):
    from sayink.speaker_turns import SpeakerTurn
    from tests.test_speaker_turns import Embedder, RATE, voice

    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"recordings": [{"audio": "x.wav", "rttm": "gold.rttm"}]}))
    (tmp_path / "gold.rttm").write_text("SPEAKER x 1 0.000 6.000 <NA> <NA> A <NA> <NA>\n"
                                      "SPEAKER x 1 6.000 3.000 <NA> <NA> B <NA> <NA>\n")
    monkeypatch.setattr(bench, "load_wav", lambda path: (voice(1, 9), RATE))
    a, b = np.eye(3, dtype=np.float32)[:2]
    monkeypatch.setattr(bench, "analyze_turns", lambda clip, detector: [
        SpeakerTurn(0, 3 * RATE, a, False), SpeakerTurn(3 * RATE, 6 * RATE, a, True),
        SpeakerTurn(6 * RATE, 9 * RATE, b, False),
    ])
    model = Embedder()
    model.name, model.sticky_threshold, model.cluster_threshold, model.recluster_threshold = "campplus", 0.45, 0.6, 0.5
    entry = bench.evaluate_recordings(manifest, model)["files"][0]
    assert [sid for _, _, sid in entry["predicted"]] == ["0", "1", "0"]
    assert [sid for _, _, sid in entry["finalized"]["predicted"]] == ["1", "1", "0"]

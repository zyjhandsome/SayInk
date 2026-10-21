"""Replay predictions are review material, never their own reference labels."""
import json
from types import SimpleNamespace

import numpy as np
import pytest

from tools import recognition_replay as replay
from sayink.speaker_turns import RecognizedTurn
from tools.asr_bench import TimedAudioChunk
from tests.test_speaker_turns import Embedder, voice


def test_replay_keeps_real_input_gaps_and_never_emits_accuracy(monkeypatch):
    audio = np.r_[voice(1, 3.2), np.zeros(16000), voice(1, 3.2)]
    chunks = [TimedAudioChunk(np.int64(0), np.int64(51200), audio[:51200]),
              TimedAudioChunk(np.int64(67200), np.int64(len(audio)), audio[67200:])]
    monkeypatch.setattr(replay, "segment_meeting_timed", lambda *a, **kw: (chunks, "energy"))

    class Worker:
        def __init__(self, recognizer, audio, **kwargs):
            self.audio, self.analyze = audio, kwargs["turn_analyzer"]
            self.meeting_ready = SimpleNamespace(connect=lambda callback: setattr(self, "ready", callback))
            self.error = SimpleNamespace(connect=lambda callback: None)

        def run(self):
            self.ready([RecognizedTurn(t.start, t.stop, "待核对文字", t.embedding, t.learnable)
                        for t in self.analyze(self.audio)])

    monkeypatch.setattr(replay, "TranscribeWorker", Worker)
    model = Embedder()
    model.name, model.sticky_threshold, model.cluster_threshold = "campplus", 0.45, 0.6
    report = replay.replay(audio, 16000, object(), model, mode="speaker")
    assert report["segments"][1]["start_sample"] == 67200
    assert report["speaker_groups"] == 1 and report["turns"] == 2
    assert report["final_speaker_groups"] == 1 and report["final_unknown_turns"] == 0
    assert [row["final_speaker"] for row in report["segments"]] == [1, 1]
    assert "accuracy" not in report and "cer" not in report
    json.dumps(report)  # Activity-model boundaries may be numpy integers.


def test_worker_audio_loss_is_rejected(monkeypatch):
    monkeypatch.setattr(replay, "segment_meeting_timed", lambda *a, **kw: ([TimedAudioChunk(0, 16000, voice(1, 1))], "energy"))

    class Worker:
        def __init__(self, *args, **kwargs):
            self.meeting_ready = SimpleNamespace(connect=lambda callback: setattr(self, "ready", callback))
            self.error = SimpleNamespace(connect=lambda callback: None)

        def run(self):
            self.ready([RecognizedTurn(0, 8000, "半段", np.ones(3), True)])

    monkeypatch.setattr(replay, "TranscribeWorker", Worker)
    model = Embedder()
    model.name, model.sticky_threshold = "campplus", 0.45
    with pytest.raises(ValueError, match="full VAD chunk"):
        replay.replay(voice(1, 1), 16000, object(), model, mode="speaker")


def test_html_model_text_cannot_escape_into_script_and_notes_stay_unreviewed(tmp_path):
    payload = {"input": {"audio": str(tmp_path / 'a.wav')}, "runs": [
        {"mode": "speaker", "turns": 1, "speaker_groups": 1, "unknown_turns": 0,
         "segments": [{"start_s": 0, "stop_s": 1, "speaker": 1, "text": '</script><script>alert("bad")</script>'}]}]}
    destination = tmp_path / "review.html"
    replay.write_review(payload, destination)
    html = destination.read_text(encoding="utf-8")
    embedded = html.split('<script id="data" type="application/json">')[1].split('</script>')[0]
    assert '</script>' not in embedded
    assert json.loads(embedded) == payload
    assert "reference_status:'unreviewed'" in html
    assert 'text.textContent=s.text' in html

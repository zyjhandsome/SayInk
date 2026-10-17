"""Meeting diarization: parameterised SpeakerSession, post-meeting recluster
and the CAM++ / mel embedder switch."""

from __future__ import annotations

import sys
import types

import numpy as np
import pytest

from sayink.speaker_embedding import (
    MEL_MATCH_THRESHOLD,
    MODEL_MATCH_THRESHOLD,
    MelEmbedder,
    ModelEmbedder,
    make_embedder,
)
from sayink.speaker_session import MATCH_THRESHOLD, SpeakerSession, recluster


def _unit(*values: float) -> np.ndarray:
    v = np.asarray(values, dtype=np.float32)
    return v / np.linalg.norm(v)


class TestSpeakerSessionParams:
    def test_default_threshold_is_unchanged(self):
        assert SpeakerSession().threshold == MATCH_THRESHOLD

    def test_lower_threshold_merges_more(self):
        a = _unit(1, 0.3, 0)
        b = _unit(1, 0, 0.3)
        strict = SpeakerSession(threshold=0.95)
        assert strict.assign(a) == 1
        assert strict.assign(b) == 2
        loose = SpeakerSession(threshold=0.5)
        assert loose.assign(a) == 1
        assert loose.assign(b) == 1

    def test_history_is_kept_only_when_asked(self):
        s = SpeakerSession()
        s.assign(_unit(1, 0, 0))
        assert s.history == []
        s = SpeakerSession(keep_history=True)
        s.assign(_unit(1, 0, 0))
        s.assign(_unit(0, 1, 0))
        assert [sid for sid, _ in s.history] == [1, 2]
        assert s.speaker_count == 2
        s.reset()
        assert s.history == [] and s.speaker_count == 0

    def test_max_speakers_param(self):
        s = SpeakerSession(threshold=0.99, max_speakers=2)
        assert s.assign(_unit(1, 0, 0)) == 1
        assert s.assign(_unit(0, 1, 0)) == 2
        # Third voice folds into the closest existing one.
        assert s.assign(_unit(0.9, 0.1, 0.4)) == 1


class TestRecluster:
    def test_identity_when_nothing_merges(self):
        history = [(1, _unit(1, 0, 0)), (2, _unit(0, 1, 0)), (1, _unit(1, 0.05, 0))]
        assert recluster(history, threshold=0.8) == {1: 1, 2: 2}

    def test_split_speaker_is_merged_and_renumbered(self):
        history = [
            (1, _unit(1, 0, 0)),
            (2, _unit(0, 1, 0)),
            (3, _unit(1, 0.1, 0)),   # really speaker 1
            (4, _unit(0, 0, 1)),
        ]
        mapping = recluster(history, threshold=0.9)
        assert mapping == {1: 1, 2: 2, 3: 1, 4: 3}

    def test_zero_vectors_are_ignored(self):
        history = [(1, np.zeros(3, dtype=np.float32)), (2, _unit(0, 1, 0))]
        assert recluster(history, threshold=0.5) == {2: 1}
        assert recluster([], threshold=0.5) == {}

    def test_max_speakers_folds_smallest_group(self):
        history = [
            (1, _unit(1, 0, 0)), (1, _unit(1, 0, 0)),
            (2, _unit(0, 1, 0)), (2, _unit(0, 1, 0)),
            (3, _unit(0.7, 0, 0.7)),
        ]
        mapping = recluster(history, threshold=0.99, max_speakers=2)
        assert mapping[1] == 1 and mapping[2] == 2
        assert mapping[3] == 1  # nearest to speaker 1


@pytest.fixture
def fake_sherpa(monkeypatch):
    fake = types.ModuleType("sherpa_onnx")

    class Config:
        def __init__(self, model="", num_threads=1, debug=False, provider="cpu"):
            self.model = model

        def validate(self):
            return self.model.endswith(".onnx")

    class Stream:
        def __init__(self):
            self.samples = None

        def accept_waveform(self, rate, samples):
            self.samples = np.asarray(samples)

        def input_finished(self):
            pass

    class Extractor:
        dim = 4

        def __init__(self, config):
            self.config = config

        def create_stream(self):
            return Stream()

        def is_ready(self, stream):
            return True

        def compute(self, stream):
            # Deterministic "embedding": energy split across halves.
            s = stream.samples
            half = s.size // 2
            return [float(np.abs(s[:half]).mean()), float(np.abs(s[half:]).mean()), 1.0, 0.0]

    fake.SpeakerEmbeddingExtractorConfig = Config
    fake.SpeakerEmbeddingExtractor = Extractor
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake)
    return fake


class TestEmbedders:
    def test_mel_embedder_matches_voice_embedding(self):
        audio = np.sin(np.arange(16000) * 0.1).astype(np.float32) * 0.3
        e = MelEmbedder()
        assert e.name == "mel"
        assert e.threshold == MEL_MATCH_THRESHOLD
        assert e(audio).shape == (48,)

    def test_model_embedder_normalises_and_skips_short_or_quiet(self, fake_sherpa):
        e = ModelEmbedder("x/campplus.onnx")
        assert e.name == "campplus"
        assert e.threshold == MODEL_MATCH_THRESHOLD
        audio = np.sin(np.arange(16000) * 0.1).astype(np.float32) * 0.3
        vec = e(audio)
        assert vec.shape == (4,)
        assert np.linalg.norm(vec) == pytest.approx(1.0)
        assert not np.any(e(audio[:1000]))          # too short
        assert not np.any(e(audio * 0.001))          # too quiet

    def test_make_embedder_fallbacks(self, fake_sherpa, tmp_path):
        assert isinstance(make_embedder(None), MelEmbedder)
        assert isinstance(make_embedder(tmp_path / "missing.onnx"), MelEmbedder)
        model = tmp_path / "campplus.onnx"
        model.write_bytes(b"x")
        assert isinstance(make_embedder(model), ModelEmbedder)
        assert isinstance(make_embedder(model, enabled=False), MelEmbedder)
        bad = tmp_path / "campplus.bin"
        bad.write_bytes(b"x")
        assert isinstance(make_embedder(bad), MelEmbedder)  # validate() fails → fallback


class TestContinuityPrior:
    """A segment that follows the previous one closely stays with that
    speaker when it is at least vaguely similar (meeting fallback path)."""

    def _session(self, **kw):
        return SpeakerSession(threshold=0.86, sticky_threshold=0.70, sticky_gap_ms=3000, **kw)

    def test_sticky_keeps_previous_speaker_within_gap(self):
        a = _unit(1, 0, 0)
        b = _unit(1, 0.6, 0)        # cos ≈ 0.86 > 0.70 but < 0.86 threshold? → 0.857
        assert float(a @ b) < 0.86 and float(a @ b) >= 0.70
        s = self._session()
        assert s.assign(a, start_ms=1000, end_ms=3000) == 1
        assert s.assign(b, start_ms=4000, end_ms=6000) == 1     # gap 1 s → sticks

    def test_sticky_needs_timestamps_and_short_gap(self):
        a = _unit(1, 0, 0)
        b = _unit(1, 0.6, 0)
        s = self._session()
        assert s.assign(a) == 1
        assert s.assign(b) == 2                                  # no timestamps → no prior
        s = self._session()
        assert s.assign(a, start_ms=1000, end_ms=3000) == 1
        assert s.assign(b, start_ms=9000, end_ms=11000) == 2    # 6 s silence → new speaker

    def test_sticky_does_not_merge_clearly_different_voices(self):
        a = _unit(1, 0, 0)
        c = _unit(0, 1, 0)
        s = self._session()
        assert s.assign(a, start_ms=1000, end_ms=3000) == 1
        assert s.assign(c, start_ms=3500, end_ms=5000) == 2

    def test_sticky_respects_capture_route(self):
        a = _unit(1, 0, 0)
        b = _unit(1, 0.6, 0)
        s = self._session()
        assert s.assign(a, "mic", start_ms=1000, end_ms=3000) == 1
        assert s.assign(b, "system", start_ms=3500, end_ms=5000) == 2

    def test_zero_vector_continues_previous_and_keeps_chain(self):
        a = _unit(1, 0, 0)
        b = _unit(1, 0.6, 0)
        s = self._session()
        assert s.assign(a, start_ms=1000, end_ms=3000) == 1
        assert s.assign(np.zeros(3, dtype=np.float32), start_ms=3500, end_ms=4000) == 1
        assert s.assign(b, start_ms=4500, end_ms=6000) == 1     # measured from the short one

    def test_default_session_has_no_prior(self):
        a = _unit(1, 0, 0)
        b = _unit(1, 0.6, 0)
        s = SpeakerSession()
        assert s.assign(a, start_ms=1000, end_ms=3000) == 1
        assert s.assign(b, start_ms=3500, end_ms=5000) == 2


class TestEmbedderThresholds:
    def test_backends_expose_sticky_and_recluster_thresholds(self):
        from sayink.speaker_embedding import (
            MEL_RECLUSTER_THRESHOLD,
            MEL_STICKY_THRESHOLD,
            MODEL_RECLUSTER_THRESHOLD,
            MODEL_STICKY_THRESHOLD,
            STICKY_GAP_MS,
        )

        mel = MelEmbedder()
        assert mel.sticky_threshold == MEL_STICKY_THRESHOLD < mel.threshold
        assert mel.recluster_threshold == MEL_RECLUSTER_THRESHOLD < mel.threshold
        assert MODEL_STICKY_THRESHOLD < MODEL_MATCH_THRESHOLD
        assert MODEL_RECLUSTER_THRESHOLD < MODEL_MATCH_THRESHOLD
        assert ModelEmbedder.sticky_threshold == MODEL_STICKY_THRESHOLD
        assert ModelEmbedder.recluster_threshold == MODEL_RECLUSTER_THRESHOLD
        assert STICKY_GAP_MS >= 1000

    def test_mel_embedder_skips_short_clips(self):
        from sayink.speaker_embedding import MEL_MIN_SEC

        e = MelEmbedder()
        short = np.sin(np.arange(int(16000 * MEL_MIN_SEC) - 1600) * 0.1).astype(np.float32) * 0.3
        assert not np.any(e(short))
        long = np.sin(np.arange(int(16000 * MEL_MIN_SEC)) * 0.1).astype(np.float32) * 0.3
        assert np.any(e(long))
        # Short follow-ups continue the previous speaker rather than guessing.
        s = SpeakerSession(threshold=0.86)
        assert s.assign(e(long)) == 1
        assert s.assign(e(short)) == 1

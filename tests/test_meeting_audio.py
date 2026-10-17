"""Meeting-mode audio chain: pluggable VAD gate, Silero wrapper, per-lane
balancing and the captured_at timestamp handed out with each segment."""

from __future__ import annotations

import sys
import types

import numpy as np
import pytest

from sayink.audio_utils import LaneBalancer, mix_to_mono
from sayink.vad_segmenter import (
    VAD_ENERGY,
    VAD_SILERO,
    EnergyGate,
    SileroGate,
    SpeechGate,
    SpeechSegmenter,
    make_speech_gate,
)

RATE = 16000


def _block(level: float, sec: float = 0.1) -> np.ndarray:
    n = int(RATE * sec)
    return (np.ones(n, dtype=np.float32) * level) * np.sign(np.sin(np.arange(n) * 0.3)).astype(np.float32)


class _ScriptedGate(SpeechGate):
    name = "scripted"

    def __init__(self, verdicts):
        self._verdicts = list(verdicts)
        self.resets = 0

    def is_speech(self, block, rms, *, in_speech):
        if self._verdicts:
            return self._verdicts.pop(0)
        return False

    def reset(self):
        self.resets += 1


class TestGateInterface:
    def test_default_gate_is_energy(self):
        seg = SpeechSegmenter()
        assert isinstance(seg.gate, EnergyGate)
        assert seg.gate_name == VAD_ENERGY

    def test_scripted_gate_decides_speech(self):
        # Loud enough for the floor, but the gate says "not speech" → nothing buffered.
        seg = SpeechSegmenter(adaptive=False, gate=_ScriptedGate([False, False, False]))
        for _ in range(3):
            assert seg.feed(_block(0.1)) is None
        assert not seg.in_speech

        gate = _ScriptedGate([True] * 5 + [False] * 30)
        seg = SpeechSegmenter(adaptive=False, gate=gate)
        out = None
        for _ in range(35):
            out = seg.feed(_block(0.1))
            if out is not None:
                break
        assert out is not None and out.size > 0
        # reset() after the segment resets the neural gate's state too.
        assert gate.resets >= 1

    def test_neural_gate_cannot_open_on_digital_silence(self):
        seg = SpeechSegmenter(adaptive=False, gate=_ScriptedGate([True] * 10))
        for _ in range(10):
            assert seg.feed(np.zeros(1600, dtype=np.float32)) is None
        assert not seg.in_speech

    def test_make_gate_falls_back_to_energy_without_model(self):
        seg = SpeechSegmenter()
        gate = make_speech_gate(VAD_SILERO, seg, model_path=None)
        assert isinstance(gate, EnergyGate)
        gate = make_speech_gate(VAD_SILERO, seg, model_path="C:/nonexistent/silero.onnx")
        assert isinstance(gate, EnergyGate)


class _FakeVadModel:
    created = []

    def __init__(self):
        self.window_size = 512
        self.calls = 0
        self.resets = 0
        self.speech_windows: set[int] = set()

    def is_speech(self, samples):
        assert len(samples) == 512
        self.calls += 1
        return (self.calls - 1) in self.speech_windows

    def reset(self):
        self.resets += 1


@pytest.fixture
def fake_sherpa(monkeypatch):
    fake = types.ModuleType("sherpa_onnx")

    class SileroVadModelConfig:
        def __init__(self, model="", threshold=0.5, window_size=512, **_):
            self.model = model
            self.threshold = threshold
            self.window_size = window_size

    class VadModelConfig:
        def __init__(self):
            self.silero_vad = None
            self.sample_rate = 0
            self.num_threads = 0

    class VadModel:
        last = None

        @staticmethod
        def create(config):
            assert config.silero_vad.model.endswith("silero_vad.onnx")
            assert config.sample_rate == 16000
            model = _FakeVadModel()
            VadModel.last = model
            return model

    fake.SileroVadModelConfig = SileroVadModelConfig
    fake.VadModelConfig = VadModelConfig
    fake.VadModel = VadModel
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake)
    return fake


class TestSileroGate:
    def test_scores_in_512_windows_and_carries_remainder(self, fake_sherpa):
        gate = SileroGate("models/silero_vad.onnx")
        model = fake_sherpa.VadModel.last
        model.speech_windows = {1}
        # 1600 samples = 3 windows + 64 carried
        assert gate.is_speech(np.zeros(1600, dtype=np.float32), 0.01, in_speech=False) is True
        assert model.calls == 3
        assert gate._carry.size == 64
        # Next 1600: 64 + 1600 = 1664 → 3 more windows, 128 carried; no speech.
        assert gate.is_speech(np.zeros(1600, dtype=np.float32), 0.01, in_speech=True) is False
        assert model.calls == 6
        assert gate._carry.size == 128

    def test_short_block_keeps_previous_verdict(self, fake_sherpa):
        gate = SileroGate("x/silero_vad.onnx")
        model = fake_sherpa.VadModel.last
        model.speech_windows = {0}
        assert gate.is_speech(np.zeros(512, dtype=np.float32), 0.01, in_speech=False) is True
        assert gate.is_speech(np.zeros(100, dtype=np.float32), 0.01, in_speech=True) is True
        gate.reset()
        assert model.resets == 1
        assert gate.is_speech(np.zeros(100, dtype=np.float32), 0.01, in_speech=True) is False

    def test_window_size_as_method_like_sherpa_1_13(self, fake_sherpa, monkeypatch):
        monkeypatch.setattr(_FakeVadModel, "window_size", lambda self: 512, raising=False)
        original_init = _FakeVadModel.__init__

        def init_without_attribute(self):
            original_init(self)
            del self.__dict__["window_size"]

        monkeypatch.setattr(_FakeVadModel, "__init__", init_without_attribute)
        gate = SileroGate("x/silero_vad.onnx")
        assert gate._window == 512
        assert gate.is_speech(np.zeros(1024, dtype=np.float32), 0.01, in_speech=False) is False

    def test_real_silero_model_loads_when_installed(self):
        from sayink.meeting_session import AUX_MODEL_SILERO_VAD, aux_model_path

        path = aux_model_path(AUX_MODEL_SILERO_VAD)
        if path is None:
            pytest.skip("Silero VAD model not downloaded on this machine")
        pytest.importorskip("sherpa_onnx")
        gate = SileroGate(path)
        assert gate._window > 0
        assert gate.is_speech(np.zeros(1600, dtype=np.float32), 0.0, in_speech=False) is False

    def test_make_gate_uses_silero_when_model_present(self, fake_sherpa):
        seg = SpeechSegmenter()
        gate = make_speech_gate(VAD_SILERO, seg, model_path="x/silero_vad.onnx")
        assert isinstance(gate, SileroGate)
        assert gate.name == VAD_SILERO
        seg.set_gate(gate)
        assert seg.gate_name == VAD_SILERO


class TestLaneBalancer:
    def test_gain_is_unity_until_speech_is_heard(self):
        b = LaneBalancer()
        assert b.observe("mic", np.zeros(1600, dtype=np.float32)) == 1.0
        assert b.level("mic") == 0.0

    def test_quiet_lane_is_lifted_and_loud_lane_is_tamed(self):
        b = LaneBalancer()
        quiet = b.observe("system", _block(0.01))
        loud = b.observe("mic", _block(0.2))
        assert quiet > 1.0
        assert loud < 1.0
        assert quiet <= 4.0 and loud >= 0.25

    def test_level_tracks_slowly(self):
        b = LaneBalancer(alpha=0.5)
        b.observe("mic", _block(0.1))
        b.observe("mic", _block(0.2))
        assert 0.1 < b.level("mic") < 0.2
        b.reset()
        assert b.level("mic") == 0.0

    def test_mix_with_gains_sums_instead_of_averaging(self):
        a = np.ones(1600, dtype=np.float32) * 0.2
        s = np.zeros(1600, dtype=np.float32)
        averaged = mix_to_mono([a, s], RATE)
        summed = mix_to_mono([a, s], RATE, gains=[1.0, 1.0])
        assert averaged[0] == pytest.approx(0.1)
        assert summed[0] == pytest.approx(0.2)
        boosted = mix_to_mono([a, s], RATE, gains=[2.0, 1.0])
        assert boosted[0] == pytest.approx(0.4)
        # Still peak-limited.
        clipped = mix_to_mono([a, a], RATE, gains=[4.0, 4.0])
        assert float(np.max(np.abs(clipped))) <= 1.0


class TestRecorderProfile:
    def test_meeting_profile_rebuilds_segmenter_and_balancer(self):
        from sayink.audio_recorder import AudioRecorder

        rec = AudioRecorder()
        assert rec.vad_name == VAD_ENERGY
        # Mixed-lane balancing is shared by dictation and meetings.
        assert isinstance(rec._balancer, LaneBalancer)
        rec.set_segmenter_profile(vad=VAD_SILERO, vad_model_path=None, meeting=True)
        # No model → energy fallback, but the meeting hold is on.
        assert rec.vad_name == VAD_ENERGY
        assert isinstance(rec._balancer, LaneBalancer)
        assert rec._segmenter._silence_hold_samples == int(RATE * 1.5)
        rec.set_segmenter_profile(vad=VAD_ENERGY, meeting=False)
        assert isinstance(rec._balancer, LaneBalancer)
        assert rec._segmenter._silence_hold_samples == int(RATE * 1.2)
        # configure() keeps the chosen profile.
        rec.set_segmenter_profile(vad=VAD_ENERGY, meeting=True)
        rec.configure("mixed", -1, -1)
        assert rec._segmenter._silence_hold_samples == int(RATE * 1.5)

    def test_segment_captured_at_is_stamped(self, monkeypatch):
        from sayink import audio_recorder as ar

        rec = ar.AudioRecorder()
        now = {"t": 1_000_000.0}
        monkeypatch.setattr(ar.time, "time", lambda: now["t"])
        rec._segmenter = SpeechSegmenter(adaptive=False, gate=_ScriptedGate([True] * 5 + [False] * 40))
        got = []
        rec.segment_ready.connect(lambda seg: got.append(rec.consume_segment_captured_at()))
        for _ in range(45):
            now["t"] += 0.1
            seg = rec._feed_segmenter(_block(0.1))
            if seg is not None:
                rec._emit_segment(seg)
                break
        assert got
        # Opened on the first fed block (t = 1_000_000.1 s) minus one block.
        assert got[0] == int((1_000_000.1 - 0.1) * 1000)
        assert rec.consume_segment_captured_at() == 0

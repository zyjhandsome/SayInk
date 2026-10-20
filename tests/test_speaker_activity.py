"""Powerset activity checks protect profiles without discarding ASR audio."""
from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import ClassVar

import numpy as np
import pytest

from sayink.speaker_activity import SpeakerActivityGuard

RATE = 16000
FRAMES = 589
META = {
    "model_type": "pyannote-segmentation-3.0", "sample_rate": "16000",
    "num_classes": "7", "num_speakers": "3", "powerset_max_classes": "2",
    "window_size": "160000", "receptive_field_shift": "270", "receptive_field_size": "991",
}


def scores(labels):
    probabilities = np.full((1, FRAMES, 7), 0.001, dtype=np.float32)
    for i, label in enumerate(labels):
        probabilities[0, i, label] = 0.994
    return np.log(probabilities)


@pytest.fixture
def runtime(monkeypatch):
    class Session:
        metadata = META.copy()
        output = scores(np.ones(FRAMES, dtype=int))
        inputs: ClassVar[list] = []

        def __init__(self, *_args, **_kwargs):
            self.__class__.instance = self

        def get_modelmeta(self):
            return SimpleNamespace(custom_metadata_map=self.metadata)

        def get_inputs(self):
            return [SimpleNamespace(name="x")]

        def run(self, _outputs, inputs):
            self.inputs.append(inputs["x"].copy())
            return [self.output]

    monkeypatch.setitem(sys.modules, "onnxruntime", SimpleNamespace(
        SessionOptions=SimpleNamespace, InferenceSession=Session))
    return Session


def test_single_voice_and_padding_do_not_create_extra_people(runtime):
    labels = np.ones(FRAMES, dtype=int)
    labels[300:] = 3  # These frames are outside a 4-second real waveform.
    runtime.output = scores(labels)
    assert not SpeakerActivityGuard("model.onnx").is_unsafe(np.full(4 * RATE, 0.1))
    assert runtime.inputs[0].shape == (1, 1, 10 * RATE)


@pytest.mark.parametrize("overlap_class", [4, 5, 6])
def test_sustained_two_voice_overlap_is_unsafe(runtime, overlap_class):
    labels = np.ones(FRAMES, dtype=int)
    labels[30:60] = overlap_class
    runtime.output = scores(labels)
    assert SpeakerActivityGuard("model.onnx").is_unsafe(np.full(4 * RATE, 0.1))


def test_unresolved_sequential_speakers_are_unsafe_without_needing_overlap(runtime):
    labels = np.ones(FRAMES, dtype=int)
    labels[80:130] = 2
    runtime.output = scores(labels)
    assert SpeakerActivityGuard("model.onnx").is_unsafe(np.full(4 * RATE, 0.1))


def test_a_brief_probability_spike_does_not_reject_a_whole_turn(runtime):
    labels = np.ones(FRAMES, dtype=int)
    labels[30:33] = 4
    labels[80:83] = 2
    runtime.output = scores(labels)
    assert not SpeakerActivityGuard("model.onnx").is_unsafe(np.full(4 * RATE, 0.1))


def test_overlap_includes_the_uncertain_transition_but_not_clear_flanks(runtime):
    labels = np.ones(FRAMES, dtype=int)
    labels[80:120] = 4
    runtime.output = scores(labels)
    # Between overlap and the next sustained clear voice lies ambiguity.
    runtime.output[0, 120:140, :] = np.log(np.full((20, 7), 1 / 7))
    evidence = SpeakerActivityGuard("model.onnx").analyze(np.full(4 * RATE, 0.1))
    assert len(evidence.overlaps) == 1
    begin, end = evidence.overlaps[0]
    assert 0 < begin < 80 * 270
    assert end > 140 * 270 and end < 4 * RATE


def test_sequential_cut_offsets_never_export_window_local_speaker_labels(runtime):
    labels = np.ones(FRAMES, dtype=int)
    labels[100:150] = 2
    runtime.output = scores(labels)
    evidence = SpeakerActivityGuard("model.onnx").analyze(np.full(4 * RATE, 0.1))
    assert len(evidence.cuts) == 2 and not evidence.overlaps
    assert all(0 < cut < 4 * RATE for cut in evidence.cuts)


def test_long_clip_is_covered_by_bounded_overlapping_windows(runtime):
    guard = SpeakerActivityGuard("model.onnx")
    assert not guard.is_unsafe(np.full(15 * RATE, 0.1))
    assert len(runtime.inputs) == 2
    with pytest.raises(ValueError):
        guard.is_unsafe(np.full(16 * RATE, 0.1))


def test_local_label_permutation_between_windows_does_not_invent_a_change(runtime, monkeypatch):
    outputs = iter((scores(np.ones(FRAMES, dtype=int)), scores(np.full(FRAMES, 2))))
    monkeypatch.setattr(runtime, "run", lambda *_args: [next(outputs)])
    evidence = SpeakerActivityGuard("model.onnx").analyze(np.full(15 * RATE, 0.1))
    assert not evidence.unsafe


def test_cancellation_stops_before_starting_inference(runtime):
    guard = SpeakerActivityGuard("model.onnx")
    with pytest.raises(InterruptedError):
        guard.is_unsafe(np.ones(RATE), cancelled=lambda: True)
    assert runtime.inputs == []


@pytest.mark.parametrize("field,value", [("model_type", "other"), ("sample_rate", "8000"),
                                        ("num_classes", "3"), ("window_size", "320000")])
def test_incompatible_models_are_rejected(runtime, field, value):
    runtime.metadata[field] = value
    with pytest.raises(ValueError):
        SpeakerActivityGuard("model.onnx")


@pytest.mark.parametrize("bad", [np.zeros((1, 589, 3)), np.zeros((1, 100, 7)),
                                np.full((1, 589, 7), np.nan)])
def test_failed_or_malformed_model_output_cannot_approve_a_voice(runtime, bad):
    runtime.output = bad
    guard = SpeakerActivityGuard("model.onnx")
    with pytest.raises(ValueError):
        guard.is_unsafe(np.ones(RATE))

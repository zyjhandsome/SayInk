"""A leading suffix must not teach the next talker's profile."""
import numpy as np
import pytest

from sayink.meeting_speakers import MeetingSpeakerSession
from sayink.speaker_turns import SpeakerTurn, SpeakerTurnContext, SpeakerTurnDetector
from tests.test_speaker_turns import Embedder, RATE, voice


def run(context, embed, audio, start_ms, route="system"):
    return SpeakerTurnDetector(embed, context=context, start_ms=start_ms, route=route)(audio)


def test_context_can_recover_a_leading_short_turn_without_repeating_old_audio():
    context, model = SpeakerTurnContext(), Embedder()
    run(context, model, voice(1, 3.2), 1000)
    current = np.r_[voice(1, 0.8), voice(2, 4)]
    result = run(context, model, current, 4200)
    assert len(result) == 2
    assert result[0].start == 0 and result[-1].stop == len(current)
    assert result[0].stop == result[1].start
    assert np.array_equal(np.concatenate([current[t.start:t.stop] for t in result]), current)
    assert [int(np.argmax(t.embedding)) for t in result] == [0, 1]
    assert not any(t.learnable for t in result)


def test_tiny_leading_suffix_keeps_asr_bounds_and_labels_first_turn_by_its_dominant_voice(monkeypatch):
    context, model = SpeakerTurnContext(), Embedder()
    run(context, model, voice(1, 3.2), 1000)
    current = voice(2, 6)
    vector_a, vector_b = np.eye(3, dtype=np.float32)[:2]
    detector = SpeakerTurnDetector(model, context=context, start_ms=4200, route="system")

    def analyze(samples, **kwargs):
        if len(samples) == len(current):
            return [SpeakerTurn(0, 3 * RATE, vector_b, True), SpeakerTurn(3 * RATE, len(current), vector_b, True)]
        cut = int(2.2 * RATE)  # 200 ms from the new chunk; too short for a new ASR slice.
        return [SpeakerTurn(0, cut, vector_a, True), SpeakerTurn(cut, len(samples), vector_b, True)]

    monkeypatch.setattr(detector, "_analyze", analyze)
    result = detector(current)
    assert detector.context_action == "dominant"
    assert [(t.start, t.stop) for t in result] == [(0, 3 * RATE), (3 * RATE, 6 * RATE)]
    assert int(np.argmax(result[0].embedding)) == 1 and not result[0].learnable
    assert result[1].learnable


def test_tiny_leading_suffix_still_labels_the_turn_when_a_live_session_has_its_voice(monkeypatch):
    context, model = SpeakerTurnContext(), Embedder()
    session = MeetingSpeakerSession(threshold=0.55)
    for turn in run(context, model, voice(1, 3.2), 1000):
        session.assign(turn.embedding, learnable=turn.learnable)
    later = SpeakerTurnDetector(model)(voice(2, 4))[0]
    assert session.assign(later.embedding, learnable=later.learnable) == 2
    current = voice(2, 6)
    detector = SpeakerTurnDetector(model, context=context, start_ms=4200, route="system")
    a, b = np.eye(3, dtype=np.float32)[:2]

    def analyze(samples, **kwargs):
        if len(samples) == len(current):
            return [SpeakerTurn(0, 3 * RATE, b, True), SpeakerTurn(3 * RATE, len(current), b, True)]
        cut = int(2.2 * RATE)
        return [SpeakerTurn(0, cut, a, True), SpeakerTurn(cut, len(samples), b, True)]

    monkeypatch.setattr(detector, "_analyze", analyze)
    first = detector(current)[0]
    assert session.assign(first.embedding, learnable=first.learnable) == 2


@pytest.mark.parametrize("start_ms,route,reset", [(5500, "system", False), (4200, "mic", False), (4200, "system", True)])
def test_pause_source_change_or_reset_cannot_reuse_the_previous_audio(start_ms, route, reset):
    context, model = SpeakerTurnContext(), Embedder()
    run(context, model, voice(1, 3.2), 1000)
    if reset:
        context.reset()
    current = np.r_[voice(1, 0.8), voice(2, 4)]
    expected = SpeakerTurnDetector(model)(current)
    actual = run(context, model, current, start_ms, route)
    assert [(t.start, t.stop, t.learnable) for t in actual] == [(t.start, t.stop, t.learnable) for t in expected]


def test_same_person_keeps_original_learning_and_bounded_context():
    context, model = SpeakerTurnContext(), Embedder()
    run(context, model, voice(1, 4), 1000)
    result = run(context, model, voice(1, 7), 5000)
    assert len(result) == 1 and result[0].learnable
    assert len(context._tail) == 2 * RATE


def test_cancelled_or_reset_inference_cannot_restore_the_old_context(monkeypatch):
    context, model = SpeakerTurnContext(), Embedder()
    detector = SpeakerTurnDetector(model, context=context, start_ms=1000)
    original = detector._analyze

    def analyze(samples, **kwargs):
        context.reset()
        return original(samples, **kwargs)

    monkeypatch.setattr(detector, "_analyze", analyze)
    detector(voice(1, 3.2))
    assert context._tail.size == 0
    assert detector(voice(1), cancelled=lambda: True) == []
    assert context._tail.size == 0


def test_session_reset_also_drops_the_audio_context():
    session, model = MeetingSpeakerSession(), Embedder()
    run(session.turn_context, model, voice(1, 3.2), 1000)
    session.reset()
    assert session.turn_context._tail.size == 0


def test_context_does_not_make_an_oversized_input_trigger_model_work():
    context, model = SpeakerTurnContext(), Embedder()
    run(context, model, voice(1, 4), 1000)
    before = model.calls
    result = run(context, model, voice(1, 16), 5000)
    assert model.calls == before and len(result) == 1
    assert not result[0].learnable and context._tail.size == 2 * RATE


def test_tiny_leading_context_cannot_turn_a_whole_long_utterance_unknown(monkeypatch):
    context, model = SpeakerTurnContext(), Embedder()
    run(context, model, voice(1, 3.2), 1000)
    current = voice(2, 12)
    detector = SpeakerTurnDetector(model, context=context, start_ms=4200, route="system")
    a, b = np.eye(3, dtype=np.float32)[:2]

    def analyze(samples, **kwargs):
        if len(samples) == len(current):
            return [SpeakerTurn(0, len(samples), b, True)]
        return [SpeakerTurn(0, int(2.2 * RATE), a, True), SpeakerTurn(int(2.2 * RATE), len(samples), b, True)]

    monkeypatch.setattr(detector, "_analyze", analyze)
    result = detector(current)
    assert len(result) == 1 and result[0].learnable and detector.context_action == "none"

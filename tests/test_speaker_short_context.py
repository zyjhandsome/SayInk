"""Short continuations need stable session context and cannot teach identities."""
import numpy as np
import pytest

from sayink.meeting_speakers import MeetingSpeakerSession
from sayink.speaker_turns import SpeakerTurnContext, SpeakerTurnDetector
from tests.test_speaker_turns import Embedder, RATE, voice


class AmbiguousWindows(Embedder):
    """Simulate a model whose usual rolling windows yield no stable proposal."""

    def __call__(self, audio):
        if len(audio) == int(1.2 * RATE):
            self.calls += 1
            return np.array([0, 0, 1], dtype=np.float32)
        return super().__call__(audio)


def seeded_context(model):
    context = SpeakerTurnContext()
    SpeakerTurnDetector(model, context=context, start_ms=1000, route="system")(voice(1, 3.2))
    return context


def test_short_continuation_recovers_without_stable_window_proposals_or_profile_updates():
    model = AmbiguousWindows()
    context = seeded_context(model)
    current = np.r_[voice(1, .8), voice(2, 4)]
    detector = SpeakerTurnDetector(model, context=context, start_ms=4200, route="system")
    turns = detector(current)
    assert detector.context_action == "short_split"
    assert len(turns) == 2 and abs(turns[0].stop - .8 * RATE) <= .1 * RATE
    assert [int(np.argmax(t.embedding)) for t in turns] == [0, 1]
    assert np.array_equal(np.concatenate([current[t.start:t.stop] for t in turns]), current)
    assert not any(t.learnable for t in turns)
    session = MeetingSpeakerSession(threshold=model.threshold, keep_history=True)
    session.assign(model(voice(1)), sample_key=-2)
    session.assign(model(voice(2)), sample_key=-1)
    assert [session.assign(t.embedding, sample_key=i, learnable=t.learnable) for i, t in enumerate(turns)] == [1, 2]
    assert len(session.history) == 2
    fresh = MeetingSpeakerSession(threshold=model.threshold)
    assert [fresh.assign(t.embedding, learnable=t.learnable) for t in turns] == [0, 0]
    assert fresh.speaker_count == 0


@pytest.mark.parametrize("current", [voice(1, 4.8), voice(2, 4.8), np.r_[voice(1, .25), voice(2, 4)]])
def test_same_person_immediate_new_person_or_unsupported_brief_suffix_is_not_split(current):
    model = AmbiguousWindows()
    detector = SpeakerTurnDetector(model, context=seeded_context(model), start_ms=4200, route="system")
    turns = detector(current)
    assert len(turns) == 1 and detector.context_action == "none"
    assert turns[0].start == 0 and turns[0].stop == len(current)


@pytest.mark.parametrize("start,route", [(4500, "system"), (4200, "mic")])
def test_short_fallback_cannot_use_stale_or_other_input_context(start, route):
    model = AmbiguousWindows()
    detector = SpeakerTurnDetector(model, context=seeded_context(model), start_ms=start, route=route)
    turns = detector(np.r_[voice(1, .8), voice(2, 4)])
    assert len(turns) == 1 and detector.context_action == "none"


def test_local_activity_rejection_keeps_words_together_and_prevents_profile_learning():
    class Guard:
        def is_unsafe(self, audio, **kwargs):
            return len(audio) < RATE

    model = AmbiguousWindows()
    model.activity_guard = Guard()
    detector = SpeakerTurnDetector(model, context=seeded_context(model), start_ms=4200, route="system")
    current = np.r_[voice(1, .8), voice(2, 4)]
    turns = detector(current)
    assert detector.context_action == "short_abstain"
    assert len(turns) == 1 and turns[0].stop == len(current)
    assert not turns[0].learnable and not np.any(turns[0].embedding)


def test_unknown_original_turn_cannot_be_approved_by_short_context():
    class Guard:
        def is_unsafe(self, audio, **kwargs):
            return True

    model = AmbiguousWindows()
    model.activity_guard = Guard()
    detector = SpeakerTurnDetector(model, context=seeded_context(model), start_ms=4200, route="system")
    turns = detector(np.r_[voice(1, .8), voice(2, 4)])
    assert len(turns) == 1 and not np.any(turns[0].embedding) and not turns[0].learnable
    assert detector.context_action == "none"


def test_cancel_during_short_search_stops_model_work_and_does_not_emit_or_cache(monkeypatch):
    model = AmbiguousWindows()
    context = seeded_context(model)
    detector = SpeakerTurnDetector(model, context=context, start_ms=4200, route="system")
    original = detector._embed_or_none
    stopped = False

    def embed(audio):
        nonlocal stopped
        assert not stopped
        result = original(audio)
        if len(audio) == int(.7 * RATE):
            stopped = True
        return result

    monkeypatch.setattr(detector, "_embed_or_none", embed)
    result = detector(np.r_[voice(1, .8), voice(2, 4)], cancelled=lambda: stopped)
    assert stopped and result == [] and context._stop_ms == 4200


def test_one_isolated_supported_short_boundary_cannot_fragment_speech():
    class OneCandidate(AmbiguousWindows):
        def __call__(self, audio):
            if len(audio) < int(1.6 * RATE) and len(audio) != int(.8 * RATE):
                self.calls += 1
                return np.zeros(3, dtype=np.float32)
            return super().__call__(audio)

    model = OneCandidate()
    detector = SpeakerTurnDetector(model, context=seeded_context(model), start_ms=4200, route="system")
    turns = detector(np.r_[voice(1, .8), voice(2, 4)])
    assert len(turns) == 1 and detector.context_action == "none"


def test_unsafe_previous_audio_cannot_support_a_new_short_boundary():
    class Guard:
        def is_unsafe(self, audio, **kwargs):
            return len(audio) == 2 * RATE

    model = AmbiguousWindows()
    model.activity_guard = Guard()
    detector = SpeakerTurnDetector(model, context=seeded_context(model), start_ms=4200, route="system")
    result = detector(np.r_[voice(1, .8), voice(2, 4)])
    assert len(result) == 1 and detector.context_action == "none"


def test_short_recovery_preserves_already_separated_later_turns(monkeypatch):
    model = AmbiguousWindows()
    context = seeded_context(model)
    detector = SpeakerTurnDetector(model, context=context, start_ms=4200, route="system")
    current = np.r_[voice(1, .8), voice(2, 4), voice(3, 3.2)]
    original = detector._analyze

    def analyze(audio, **kwargs):
        if len(audio) == len(current):
            from sayink.speaker_turns import SpeakerTurn
            return [SpeakerTurn(0, int(4.8 * RATE), np.eye(3)[1], True),
                    SpeakerTurn(int(4.8 * RATE), len(audio), np.eye(3)[2], True)]
        return original(audio, **kwargs)

    monkeypatch.setattr(detector, "_analyze", analyze)
    result = detector(current)
    assert detector.context_action == "short_split" and len(result) == 3
    assert [int(np.argmax(t.embedding)) for t in result] == [0, 1, 2]
    assert result[2].start == int(4.8 * RATE) and result[2].learnable
    assert np.array_equal(np.concatenate([current[t.start:t.stop] for t in result]), current)

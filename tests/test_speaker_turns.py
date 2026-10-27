"""Sustained speaker changes partition audio without treating silence as a person."""
from __future__ import annotations

import numpy as np
import pytest

from sayink.speaker_turns import MAX_ANALYSIS_SEC, SpeakerTurnDetector
from sayink.speaker_activity import ActivityEvidence

RATE = 16000


def voice(person, seconds=3.2):
    # Distinguishable artificial channels for testing orchestration only;
    # these are not real voices or an accuracy benchmark.
    return np.full(int(seconds * RATE), person * 0.1, dtype=np.float32)


class Embedder:
    threshold = 0.55
    supports_turn_detection = True

    def __init__(self):
        self.calls = 0

    def __call__(self, audio):
        self.calls += 1
        parts = [np.mean(np.isclose(audio, marker * 0.1, atol=0.001)) for marker in (1, 2, 3)]
        vector = np.array(parts, dtype=np.float32)
        norm = np.linalg.norm(vector)
        return vector / norm if norm > 1e-6 else vector


def test_embed_failure_logs_one_warning_until_the_throttle_window_passes(caplog):
    import logging

    class Boom:
        threshold = 0.55

        def __call__(self, _audio):
            raise RuntimeError("onnx exploded")

    detector = SpeakerTurnDetector(Boom())
    audio = voice(1, 0.2)
    with caplog.at_level(logging.WARNING, logger="SayInk"):
        assert detector._embed_or_none(audio) is None
        assert detector._embed_or_none(audio) is None
    assert [r.message for r in caplog.records if "onnx exploded" in r.message] == [
        "说话人声纹计算失败，本段暂不判断说话人: onnx exploded"
    ]


def test_one_person_without_pauses_stays_one_turn():
    audio = voice(1, 8)
    result = SpeakerTurnDetector(Embedder())(audio)
    assert len(result) == 1 and result[0].learnable
    assert result[0].start == 0 and result[0].stop == len(audio)


def _bursty(seconds=4.8):
    """Speech shorter than a 1.2 s window can prove, with enough of it overall.

    Each burst is 0.35 s and the gap is 0.95 s, so no analysis window contains
    the 1.0 s of loud frames ``voice_sample_is_clear`` requires. The whole clip
    still does.
    """
    speech = int(0.35 * RATE)
    gap = int(0.95 * RATE)
    total = int(seconds * RATE)
    cycle = np.concatenate((
        np.full(speech, 0.1, dtype=np.float32),
        np.zeros(gap, dtype=np.float32),
    ))
    repeats = int(np.ceil(total / len(cycle)))
    return np.tile(cycle, repeats)[:total]


def test_a_clear_opening_without_a_stable_window_can_seed_a_speaker():
    """The first utterance is often longer than 2.8 s and full of short pauses.

    Missing window anchors must not force the whole clear clip to stay unknown,
    or the meeting cannot open speaker 1.
    """
    from sayink.meeting_speakers import MeetingSpeakerSession, voice_sample_is_clear
    from sayink.speaker_turns import HOP_SEC, WINDOW_SEC

    audio = _bursty()
    window = int(WINDOW_SEC * RATE)
    hop = int(HOP_SEC * RATE)
    assert audio.size >= 2 * window + hop
    assert voice_sample_is_clear(audio)
    assert not any(
        voice_sample_is_clear(audio[start:start + window])
        for start in range(0, audio.size - window + 1, hop)
    )
    result = SpeakerTurnDetector(Embedder())(audio)
    assert len(result) == 1 and result[0].learnable
    assert result[0].start == 0 and result[0].stop == len(audio)
    assert np.any(result[0].embedding)

    session = MeetingSpeakerSession(threshold=0.55, margin=0.08, keep_history=True)
    assert session.assign(result[0].embedding, sample_key=1, learnable=result[0].learnable) == 1


def test_overlap_still_blocks_seeding_when_no_window_is_stable():
    class Guard:
        def analyze(self, audio, **kwargs):
            return ActivityEvidence(overlaps=((0, len(audio)),))

    model = Embedder()
    model.activity_guard = Guard()
    result = SpeakerTurnDetector(model)(_bursty())
    assert len(result) == 1 and not result[0].learnable
    assert np.any(result[0].embedding)


def test_a_safe_activity_check_can_seed_a_clear_clip_with_no_stable_window():
    class Guard:
        def analyze(self, audio, **kwargs):
            return ActivityEvidence()

    model = Embedder()
    model.activity_guard = Guard()
    result = SpeakerTurnDetector(model)(_bursty())
    assert len(result) == 1 and result[0].learnable
    assert np.any(result[0].embedding)


def test_activity_cuts_do_not_split_a_clip_that_never_formed_an_anchor():
    class Guard:
        def analyze(self, audio, **kwargs):
            return ActivityEvidence(cuts=(2 * RATE,))

    model = Embedder()
    model.activity_guard = Guard()
    audio = _bursty()
    result = SpeakerTurnDetector(model)(audio)
    assert [(t.start, t.stop) for t in result] == [(0, len(audio))]
    assert not result[0].learnable
    assert np.any(result[0].embedding)


def test_two_people_with_no_silence_are_split_without_audio_loss():
    audio = np.concatenate([voice(1), voice(2)])
    result = SpeakerTurnDetector(Embedder())(audio)
    assert len(result) == 2
    assert abs(result[0].stop / RATE - 3.2) <= 0.45
    assert result[0].stop == result[1].start
    assert np.array_equal(np.concatenate([audio[t.start:t.stop] for t in result]), audio)
    assert np.argmax(result[0].embedding) == 0
    assert np.argmax(result[1].embedding) == 1


def test_a_b_a_without_silence_has_three_ordered_turns():
    audio = np.concatenate([voice(1), voice(2), voice(1)])
    result = SpeakerTurnDetector(Embedder())(audio)
    assert len(result) == 3
    assert [int(np.argmax(t.embedding)) for t in result] == [0, 1, 0]
    assert sum(t.stop - t.start for t in result) == len(audio)


def test_repeated_isolated_incompatible_windows_do_not_create_a_mixed_profile():
    # Each burst supplies just one clear analysis window. The ordinary
    # consecutive-window rule cannot locate a safe cut even though two
    # different voices recur. Keep all speech for ASR but do not label or
    # learn the mixed whole-clip embedding as a single person.
    gap = np.zeros(int(1.2 * RATE), dtype=np.float32)
    audio = np.concatenate([voice(1, 1.2), gap, voice(2, 1.2), gap,
                            voice(1, 1.2), gap, voice(2, 1.2)])
    result = SpeakerTurnDetector(Embedder())(audio)
    assert len(result) == 1
    assert (result[0].start, result[0].stop) == (0, len(audio))
    assert not result[0].learnable
    assert not np.any(result[0].embedding)
    from sayink.meeting_speakers import MeetingSpeakerSession

    session = MeetingSpeakerSession(threshold=0.55, margin=0.08)
    assert session.assign(result[0].embedding, learnable=result[0].learnable) == 0
    assert session.speaker_count == 0


def test_one_isolated_different_window_does_not_block_a_clear_voice():
    # A single short feature change does not establish a second recurring
    # voice; preserve the established behavior for normal speaker variation.
    gap = np.zeros(int(1.2 * RATE), dtype=np.float32)
    audio = np.concatenate([voice(1, 1.2), gap, voice(2, 1.2), gap,
                            voice(1, 1.2), gap, voice(1, 1.2)])
    result = SpeakerTurnDetector(Embedder())(audio)
    assert len(result) == 1 and result[0].learnable


def test_short_interjection_does_not_create_an_unstable_extra_turn():
    audio = np.concatenate([voice(1, 3.2), voice(2, 0.25), voice(1, 3.2)])
    result = SpeakerTurnDetector(Embedder())(audio)
    assert len(result) == 1


def test_detected_multiple_voices_keep_all_samples_but_cannot_train_a_profile():
    class Guard:
        def is_unsafe(self, audio, **kwargs):
            return True

    model = Embedder()
    model.activity_guard = Guard()
    audio = voice(1, 1.8)  # Even the short path must run the activity check.
    result = SpeakerTurnDetector(model)(audio)
    assert len(result) == 1 and not result[0].learnable
    assert not np.any(result[0].embedding)
    assert result[0].start == 0 and result[0].stop == len(audio)


def test_a_failed_activity_check_cannot_silently_approve_profile_learning():
    class BrokenGuard:
        def is_unsafe(self, audio, **kwargs):
            raise RuntimeError("inference failed")

    model = Embedder()
    model.activity_guard = BrokenGuard()
    result = SpeakerTurnDetector(model)(voice(1, 5))
    assert len(result) == 1 and not result[0].learnable
    assert not np.any(result[0].embedding)


def test_activity_is_checked_for_each_final_turn_including_returning_voices():
    class Guard:
        def __init__(self):
            self.seen = []

        def is_unsafe(self, audio, **kwargs):
            self.seen.append(audio.copy())
            return len(self.seen) == 2

    model = Embedder()
    guard = model.activity_guard = Guard()
    audio = np.concatenate([voice(1), voice(2), voice(1)])
    result = SpeakerTurnDetector(model)(audio)
    assert len(result) == 3 and len(guard.seen) == 3
    assert [t.learnable for t in result] == [True, False, True]
    assert not np.any(result[1].embedding)
    assert np.array_equal(np.concatenate(guard.seen), audio)


def test_unstable_short_windows_do_not_force_a_speaker_change():
    class Unstable(Embedder):
        def __call__(self, audio):
            self.calls += 1
            return np.array([self.calls % 2, (self.calls + 1) % 2], dtype=np.float32)

    result = SpeakerTurnDetector(Unstable())(voice(1, 6.4))
    assert len(result) == 1


def test_longer_voice_context_rejects_false_short_window_changes():
    class SamePersonWithVariableShortFeatures(Embedder):
        def __call__(self, audio):
            if len(audio) > int(1.3 * RATE):
                return np.array([1, 0, 0], dtype=np.float32)
            # Short phonetic/laughter windows look different, although longer
            # observations consistently describe the same person.
            return super().__call__(audio)

    clip = np.concatenate([voice(1), voice(2), voice(1)])
    result = SpeakerTurnDetector(SamePersonWithVariableShortFeatures())(clip)
    assert len(result) == 1 and result[0].learnable
    assert result[0].stop == len(clip)


def test_quiet_windows_do_not_open_a_new_voice():
    audio = np.concatenate([voice(1), np.zeros(RATE * 2), voice(1)])
    result = SpeakerTurnDetector(Embedder())(audio)
    assert len(result) == 1


def test_neural_proposals_recover_short_turns_but_only_clear_long_turns_can_train():
    class Guard:
        def analyze(self, audio, **kwargs):
            return ActivityEvidence(cuts=(int(2.4 * RATE), int(3.2 * RATE)))

    model = Embedder()
    model.activity_guard = Guard()
    clip = np.concatenate([voice(1, 2.4), voice(2, 0.8), voice(1, 2.4)])
    result = SpeakerTurnDetector(model)(clip)
    assert [int(np.argmax(t.embedding)) for t in result] == [0, 1, 0]
    assert [t.learnable for t in result] == [True, False, True]
    assert np.array_equal(np.concatenate([clip[t.start:t.stop] for t in result]), clip)


def test_overlap_only_abstains_locally_and_cannot_create_a_mixed_profile():
    class Guard:
        def analyze(self, audio, **kwargs):
            return ActivityEvidence(overlaps=((2 * RATE, 3 * RATE),))

    model = Embedder()
    model.activity_guard = Guard()
    clip = voice(1, 5)
    result = SpeakerTurnDetector(model)(clip)
    assert [(t.start, t.stop) for t in result] == [(0, 2 * RATE), (2 * RATE, 3 * RATE), (3 * RATE, 5 * RATE)]
    assert [t.learnable for t in result] == [False, False, False]
    assert np.any(result[1].embedding)
    from sayink.meeting_speakers import MeetingSpeakerSession

    session = MeetingSpeakerSession(threshold=0.55, keep_history=True)
    session.assign(model(voice(1)), sample_key=0)
    ids = [session.assign(t.embedding, sample_key=i + 1, learnable=t.learnable) for i, t in enumerate(result)]
    assert ids == [1, 1, 1] and len(session.history) == 1


def test_unresolved_single_voice_offers_its_embedding_but_cannot_train():
    """An inconclusive check leaves the clip unlearnable, yet its whole-clip
    embedding is still offered so a strong match can label it instead of
    leaving a whole sentence 待确认."""
    class Guard:
        def analyze(self, audio, **kwargs):
            return ActivityEvidence(unresolved=True)

    model = Embedder()
    model.activity_guard = Guard()
    result = SpeakerTurnDetector(model)(voice(1, 5))
    assert len(result) == 1 and not result[0].learnable
    assert np.argmax(result[0].embedding) == 0 and np.any(result[0].embedding)


@pytest.mark.parametrize("unresolved,cuts", [(True, ()), (False, (RATE,))])
def test_unresolved_or_rejected_change_keeps_overlap_local_and_out_of_clustering(unresolved, cuts):
    from sayink.meeting_speakers import MeetingSpeakerSession
    from sayink.speaker_clustering import reassign_turns

    class Guard:
        def analyze(self, audio, **kwargs):
            return ActivityEvidence(overlaps=((2 * RATE, 3 * RATE),), cuts=cuts, unresolved=unresolved)

    model = Embedder()
    model.activity_guard = Guard()
    audio = voice(1, 5)
    turns = SpeakerTurnDetector(model)(audio)
    assert [(t.start, t.stop) for t in turns] == [(0, 2 * RATE), (2 * RATE, 3 * RATE), (3 * RATE, 5 * RATE)]
    assert np.array_equal(np.concatenate([audio[t.start:t.stop] for t in turns]), audio)
    assert not any(t.learnable for t in turns)
    assert np.any(turns[1].embedding)
    session = MeetingSpeakerSession(threshold=0.55, keep_history=True)
    for key, turn in enumerate(turns):
        assert session.assign(turn.embedding, sample_key=key, learnable=turn.learnable) == 0
    session.assign(model(voice(1)), sample_key=3)
    final = reassign_turns(session.turns, threshold=0.6)
    assert final == {0: 1, 1: 1, 2: 1, 3: 1}


def test_an_already_uncertain_turn_still_preserves_clear_sides_of_overlap():
    class Guard:
        def analyze(self, audio, **kwargs):
            return ActivityEvidence(overlaps=((2 * RATE, 3 * RATE),), cuts=(RATE,))

    model = Embedder()
    model.activity_guard = Guard()
    turns = SpeakerTurnDetector(model)._single(voice(1, 5), uncertain=True)
    assert [(t.start, t.stop) for t in turns] == [(0, 2 * RATE), (2 * RATE, 3 * RATE), (3 * RATE, 5 * RATE)]
    assert [bool(np.any(t.embedding)) for t in turns] == [True, True, True]
    assert not any(t.learnable for t in turns)


def test_a_long_overlap_does_not_slice_the_sentence_or_train_a_profile():
    class Guard:
        def analyze(self, audio, **kwargs):
            return ActivityEvidence(overlaps=((2 * RATE, 6 * RATE),))

    model = Embedder()
    model.activity_guard = Guard()
    turns = SpeakerTurnDetector(model)(voice(1, 8))
    assert [(t.start, t.stop) for t in turns] == [(0, 8 * RATE)]
    assert np.any(turns[0].embedding) and not turns[0].learnable


def test_too_many_overlap_regions_cannot_fall_back_to_a_mixed_embedding():
    from sayink.speaker_turns import MAX_TURNS

    detector = SpeakerTurnDetector(Embedder())
    audio = voice(1, 15)
    overlaps = tuple((i * RATE + RATE // 2, (i + 1) * RATE) for i in range(MAX_TURNS))
    turns = detector._activity_turns(audio, ActivityEvidence(overlaps=overlaps, unresolved=True))
    assert len(turns) == 1 and turns[0].stop == len(audio)
    assert not np.any(turns[0].embedding) and not turns[0].learnable


def test_fallback_embedding_labels_only_a_strong_known_match_and_never_opens_a_speaker():
    from sayink.meeting_speakers import MeetingSpeakerSession

    model = Embedder()
    session = MeetingSpeakerSession(threshold=0.55, margin=0.08, keep_history=True)
    assert session.assign(model(voice(1)), sample_key=0) == 1
    # Same voice, unresolved: labelled through the strong match, not learned.
    assert session.assign(model(voice(1)), sample_key=1, learnable=False) == 1
    # A new voice that is only unresolved evidence stays unknown rather than
    # becoming speaker 2 on the strength of a single uncertain clip.
    assert session.assign(model(voice(2)), sample_key=2, learnable=False) == 0
    assert session.speaker_count == 1 and len(session.history) == 1
    # Both uncertain turns are still remembered for the end-of-session pass.
    assert [t.key for t in session.turns] == [0, 1, 2]
    assert session.turns[2].vector is not None


def test_local_labels_without_a_cam_voice_change_cannot_split_one_person():
    class Guard:
        def analyze(self, audio, **kwargs):
            return ActivityEvidence(cuts=(2 * RATE,))

    model = Embedder()
    model.activity_guard = Guard()
    result = SpeakerTurnDetector(model)(voice(1, 5))
    assert len(result) == 1 and not result[0].learnable


def test_tiny_unknown_edge_expands_abstention_without_dropping_the_edge_audio():
    class Guard:
        def analyze(self, audio, **kwargs):
            return ActivityEvidence(overlaps=((int(0.1 * RATE), RATE),))

    model = Embedder()
    model.activity_guard = Guard()
    result = SpeakerTurnDetector(model)(voice(1, 4))
    assert [(t.start, t.stop) for t in result] == [(0, RATE), (RATE, 4 * RATE)]
    assert np.any(result[0].embedding) and np.any(result[1].embedding)
    assert not result[0].learnable and not result[1].learnable


def test_failure_to_embed_still_returns_original_audio_unknown():
    class Broken(Embedder):
        def __call__(self, audio):
            raise RuntimeError("model unavailable")

    audio = voice(1, 4)
    result = SpeakerTurnDetector(Broken())(audio)
    assert len(result) == 1 and not result[0].learnable
    assert result[0].stop == len(audio) and not np.any(result[0].embedding)


def test_cancellation_stops_between_neural_windows():
    model = Embedder()
    result = SpeakerTurnDetector(model)(voice(1, 12), cancelled=lambda: model.calls >= 3)
    assert result == [] and model.calls == 3


def test_long_unbounded_audio_does_not_trigger_unbounded_model_work():
    model = Embedder()
    audio = voice(1, MAX_ANALYSIS_SEC + 1)
    result = SpeakerTurnDetector(model)(audio)
    assert model.calls == 0
    assert len(result) == 1 and not result[0].learnable


@pytest.mark.parametrize("audio", [np.zeros(0), np.array([np.nan]), np.array([np.inf])])
def test_invalid_audio_is_rejected(audio):
    with pytest.raises(ValueError):
        SpeakerTurnDetector(Embedder())(audio)

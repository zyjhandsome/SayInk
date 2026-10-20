"""Behavioral regressions for uncertain turns and corrected voice evidence."""
from __future__ import annotations

import numpy as np
import pytest

from sayink.meeting_speakers import (
    MAX_PROFILE_SAMPLES, MAX_RECENT_OBSERVATIONS, MeetingSpeakerSession, voice_sample_is_clear,
)


def unit(*values):
    vector = np.asarray(values, dtype=np.float32)
    return vector / np.linalg.norm(vector)


def session(**kwargs):
    return MeetingSpeakerSession(threshold=0.8, keep_history=True, sticky_threshold=0.65,
                                 sticky_gap_ms=3000, **kwargs)


def test_zero_short_turn_does_not_inherit_previous_person_or_train_a_profile():
    s = session()
    assert s.assign(unit(1, 0), sample_key=2) == 1
    assert s.assign(np.zeros(1), sample_key=7, learnable=False) == 0
    assert len(s.history) == 1 and s.speaker_count == 1
    assert s.assign(np.zeros(1), sample_key=11) == 0
    assert not s.confirm(7, 1)


def test_clear_returning_voice_matches_after_a_long_gap_and_intervening_speakers():
    s = session()
    assert s.assign(unit(1, 0, 0), start_ms=1000, end_ms=3000) == 1
    assert s.assign(unit(0, 1, 0), start_ms=4000, end_ms=6000) == 2
    assert s.assign(unit(1, 0.1, 0), start_ms=1_800_000, end_ms=1_802_000) == 1


def test_similar_candidates_are_unknown_and_do_not_contaminate_either_person():
    s = session()
    assert s.assign(unit(1, 0), sample_key=0) == 1
    assert s.assign(unit(0, 1), sample_key=1) == 2
    before = s.history
    assert s.assign(unit(1, 1), sample_key=2) == 0
    assert s.speaker_count == 2
    assert all(np.array_equal(a[1], b[1]) for a, b in zip(before, s.history, strict=True))


def test_low_quality_evidence_can_match_strongly_but_never_open_or_update_profile():
    s = session()
    assert s.assign(unit(1, 0), learnable=False) == 0
    assert s.speaker_count == 0
    assert s.assign(unit(1, 0)) == 1
    assert s.assign(unit(1, 0.1), learnable=False) == 1
    assert s.speaker_count == 1 and len(s.history) == 1
    assert s.assign(unit(0, 1), learnable=False) == 0


def test_continuity_guess_is_not_learned_and_unknown_breaks_the_chain():
    s = session()
    a, shifted = unit(1, 0), unit(1, 0.85)
    s.assign(a, start_ms=1000, end_ms=2000)
    s.assign(unit(1, 0.01), start_ms=2100, end_ms=3000)
    assert s.assign(shifted, start_ms=3100, end_ms=4000) == 1
    assert len(s.history) == 2
    assert s.assign(np.zeros(1), start_ms=4100, end_ms=4200) == 0
    assert s.assign(shifted, start_ms=4300, end_ms=5000) == 2


def test_multiple_voice_states_remain_matchable_after_the_profile_moves():
    s = session()
    first, second, third = unit(1, 0), unit(1, 0.5), unit(1, 1)
    assert s.assign(first) == 1
    assert s.assign(second) == 1
    assert s.assign(third) == 1
    for _ in range(30):
        assert s.assign(third) == 1
    assert s.assign(first, start_ms=1_800_000) == 1
    assert len(s.history) <= MAX_PROFILE_SAMPLES


def test_capacity_does_not_force_a_new_voice_into_an_existing_person():
    s = session(max_speakers=2)
    assert s.assign(unit(1, 0, 0)) == 1
    assert s.assign(unit(0, 1, 0)) == 2
    assert s.assign(unit(0, 0, 1)) == 0
    assert len(s.history) == 2


@pytest.mark.parametrize("bad", [[], [0, 0], [np.nan, 0], [np.inf, 1]])
def test_invalid_evidence_does_not_crash_or_get_a_person(bad):
    s = session()
    s.assign(unit(1, 0))
    assert s.assign(np.asarray(bad, dtype=np.float32)) == 0
    assert len(s.history) == 1


def test_feature_dimensions_cannot_mix_after_model_failure():
    s = session()
    s.assign(unit(1, 0, 0))
    assert s.assign(unit(1, 0)) == 0
    assert s.assign(unit(1, 0, 0)) == 1


def test_manual_correction_teaches_future_turns_and_removes_old_voice_evidence():
    s = session()
    a, b = unit(1, 0, 0), unit(0, 1, 0)
    assert s.assign(a, sample_key=2) == 1
    assert s.assign(b, sample_key=7) == 2
    manual = s.reserve_speaker_id()
    assert s.confirm(7, manual)
    assert s.assign(b, sample_key=11) == manual
    assert s.assign(a, sample_key=15) == 1
    assert {sid for sid, _vec in s.history} == {1, manual}
    # Editing the same paragraph again removes the earlier teaching sample.
    assert s.confirm(7, 1)
    assert sum(np.array_equal(vec, b) and sid == 1 for sid, vec in s.history) == 1
    assert not any(np.array_equal(vec, b) and sid == 2 for sid, vec in s.history)


def test_manual_confirmation_can_teach_an_ambiguous_but_clear_sample():
    s = session()
    s.assign(unit(1, 0), sample_key=2)
    s.assign(unit(0, 1), sample_key=7)
    middle = unit(1, 1)
    assert s.assign(middle, sample_key=11) == 0
    assert s.confirm(11, 2)
    assert s.assign(middle, sample_key=15) == 2


def test_manual_evidence_wins_over_duplicate_automatic_samples_but_conflicts_abstain():
    s = session()
    a = unit(1, 0)
    s.assign(a, sample_key=2)
    s.assign(a, sample_key=7)
    manual = s.reserve_speaker_id()
    assert s.confirm(7, manual)
    assert s.assign(a, sample_key=11) == manual
    # Two manually confirmed identities with identical evidence cannot be
    # disambiguated acoustically. They must not collapse to one identity.
    assert s.confirm(2, 1)
    assert s.assign(a, sample_key=15) == 0


def test_recent_observations_and_profiles_are_bounded_and_reset_for_next_meeting():
    s = session()
    for i in range(MAX_RECENT_OBSERVATIONS + 10):
        s.assign(unit(1, 0), sample_key=i)
    assert not s.confirm(0, 2)
    assert len(s._observations) == MAX_RECENT_OBSERVATIONS
    assert len(s.history) <= MAX_PROFILE_SAMPLES
    s.reset()
    assert s.history == [] and s.speaker_count == 0
    assert not s.confirm(10, 1)
    assert s.assign(unit(0, 1)) == 1


def test_capture_routes_stay_distinct_until_explicitly_linked():
    s = session()
    a = unit(1, 0)
    assert s.assign(a, "mic", sample_key=2) == 1
    assert s.assign(a, "system", sample_key=7) == 2
    assert s.confirm(7, 1)
    assert s.assign(a, "system", sample_key=11) == 1


def test_profile_quality_gate_rejects_short_quiet_nonfinite_and_clipped_audio():
    t = np.arange(16000, dtype=np.float32) / 16000
    voice = 0.2 * np.sin(2 * np.pi * 180 * t)
    assert voice_sample_is_clear(voice)
    assert not voice_sample_is_clear(voice[:8000])
    assert not voice_sample_is_clear(voice * 0.001)
    assert not voice_sample_is_clear(np.ones(16000, dtype=np.float32))
    voice[0] = np.nan
    assert not voice_sample_is_clear(voice)

"""End-of-session re-assignment: every turn clustered, numbers stable for manual identities."""
from __future__ import annotations

import numpy as np
import pytest

from sayink.meeting_speakers import MeetingSpeakerSession
from sayink.speaker_clustering import TurnObservation, average_linkage_labels, reassign_turns


def _unit(*values):
    vector = np.array(values, dtype=np.float32)
    return vector / np.linalg.norm(vector)


A = _unit(1, 0, 0, 0)
A2 = _unit(1, 0.25, 0, 0)       # cos(A, A2) ≈ 0.97
B = _unit(0, 1, 0, 0)
B2 = _unit(0, 1, 0.25, 0)
C = _unit(0, 0, 1, 0)


def _turn(key, speaker_id, vector, seconds=4.0):
    return TurnObservation(key, speaker_id, vector, int(seconds * 1000))


class TestAverageLinkage:
    def test_trivial_sizes(self):
        assert average_linkage_labels(np.zeros((0, 3)), 0.5).size == 0
        assert average_linkage_labels(np.array([[1.0, 0.0]]), 0.5).tolist() == [0]

    def test_groups_by_cosine_distance_in_order_of_first_appearance(self):
        labels = average_linkage_labels(np.stack([B, A, B2, A2, C]), 0.4)
        assert labels.tolist() == [0, 1, 0, 1, 2]

    def test_average_linkage_does_not_let_a_bridge_merge_distant_voices(self):
        # x and z are far apart; y sits between them. Average linkage keeps
        # the pair whose *mean* distance exceeds the cut separate.
        x, y, z = _unit(1, 0, 0), _unit(1, 1, 0), _unit(0, 1, 0)
        labels = average_linkage_labels(np.stack([x, y, z]), 0.35)
        assert len(set(labels.tolist())) == 2

    def test_everything_merges_under_a_loose_cut(self):
        labels = average_linkage_labels(np.stack([A, B, C]), 1.5)
        assert labels.tolist() == [0, 0, 0]


class TestReassignTurns:
    def test_a_person_split_across_numbers_is_folded_back_and_every_turn_moves(self):
        turns = [_turn(0, 1, A), _turn(1, 2, B), _turn(2, 3, A2), _turn(3, 1, A), _turn(4, 3, A2),
                 _turn(5, 2, B2), _turn(6, 2, B)]
        result = reassign_turns(turns, threshold=0.6)
        assert result == {0: 1, 1: 2, 2: 1, 3: 1, 4: 1, 5: 2, 6: 2}

    def test_a_turn_stuck_in_the_wrong_group_moves_to_its_voice(self):
        turns = [_turn(k, 1, A) for k in range(3)] + [_turn(k, 2, B) for k in range(3, 6)] + [_turn(6, 1, B2)]
        result = reassign_turns(turns, threshold=0.6)
        assert result[6] == 2 and {result[k] for k in range(3)} == {1}

    def test_tiny_clusters_fold_into_the_nearest_substantial_voice(self):
        odd = _unit(1, 0.9, 0, 0)  # closer to A than to B, too distinct to cluster with either
        turns = [_turn(k, 1, A) for k in range(4)] + [_turn(k, 2, B) for k in range(4, 8)] + [_turn(8, 3, odd, 2.0)]
        result = reassign_turns(turns, threshold=0.6)
        assert result[8] == 1 and len(set(result.values())) == 2

    def test_a_voice_with_enough_turns_keeps_its_own_number(self):
        turns = ([_turn(k, 1, A) for k in range(4)] + [_turn(k, 2, B) for k in range(4, 8)]
                 + [_turn(k, 3, C, 3.0) for k in range(8, 11)])
        result = reassign_turns(turns, threshold=0.6)
        assert len(set(result.values())) == 3

    def test_turns_without_a_vector_are_left_unknown(self):
        turns = [_turn(0, 1, A), _turn(1, 0, None), _turn(2, 0, np.zeros(4, dtype=np.float32)), _turn(3, 1, A2)]
        assert reassign_turns(turns, threshold=0.6) == {0: 1, 3: 1}

    def test_mismatched_dimensions_abstain(self):
        assert reassign_turns([_turn(0, 1, A), _turn(1, 2, _unit(1, 0))], threshold=0.6) == {}
        assert reassign_turns([], threshold=0.6) == {}

    def test_a_single_short_remark_is_folded_into_the_nearest_voice(self):
        # Fewer than three turns and under eight seconds cannot hold a number of
        # their own: on real meetings such clusters were noise or mixed chunks.
        turns = [_turn(k, 1, A) for k in range(4)] + [_turn(k, 2, B) for k in range(4, 8)] + [_turn(8, 3, C, 2.0)]
        result = reassign_turns(turns, threshold=0.6)
        assert result[8] in (1, 2) and len(set(result.values())) == 2

    def test_protected_speaker_keeps_its_number_and_lends_it_to_its_cluster(self):
        # Number 3 was named by the user while 1 is an earlier automatic split of the same voice.
        turns = ([_turn(0, 1, A), _turn(1, 2, B), _turn(2, 3, A2), _turn(3, 3, A), _turn(4, 1, A2)]
                 + [_turn(k, 2, B2) for k in range(5, 8)])
        result = reassign_turns(turns, threshold=0.6, protected_ids={3})
        assert result == {0: 3, 1: 1, 2: 3, 3: 3, 4: 3, 5: 1, 6: 1, 7: 1}

    def test_two_named_people_never_collapse_even_when_their_voices_cluster(self):
        turns = [_turn(0, 1, A), _turn(1, 2, A2), _turn(2, 1, A), _turn(3, 2, A2)]
        result = reassign_turns(turns, threshold=0.6, protected_ids={1, 2})
        assert result == {0: 1, 1: 2, 2: 1, 3: 2}

    def test_locked_turn_never_moves_and_anchors_its_number(self):
        turns = ([_turn(0, 1, A), _turn(1, 1, A2), _turn(2, 5, A)]
                 + [_turn(k, 2, B if k % 2 else B2) for k in range(3, 7)])
        result = reassign_turns(turns, threshold=0.6, protected_ids={5}, locked_keys={2})
        assert result[2] == 5
        assert result[0] == result[1] == 5  # the voice around the correction adopts the user's number
        assert {result[k] for k in range(3, 7)} == {1}

    def test_automatic_numbers_skip_those_taken_by_manual_identities(self):
        turns = [_turn(k, 4, A if k % 2 else A2) for k in range(0, 6, 2)] + [_turn(k, 1, B if k % 2 else B2) for k in range(1, 7, 2)]
        result = reassign_turns(turns, threshold=0.6, protected_ids={1})
        assert {result[k] for k in (1, 3, 5)} == {1}
        assert {result[k] for k in (0, 2, 4)} == {2}

    def test_speaker_cap_folds_the_smallest_automatic_groups(self):
        basis = np.eye(6, dtype=np.float32)
        turns = []
        key = 0
        for voice in range(6):
            for _ in range(3):
                turns.append(_turn(key, voice + 1, basis[voice], 4.0))
                key += 1
        assert len(set(reassign_turns(turns, threshold=0.6).values())) == 6
        result = reassign_turns(turns, threshold=0.6, max_speakers=3)
        assert len(set(result.values())) == 3


class TestSessionTurns:
    def test_session_records_every_keyed_turn_with_duration_and_manual_updates(self):
        session = MeetingSpeakerSession(threshold=0.55, keep_history=True)
        assert session.assign(A, "", start_ms=1000, end_ms=4000, sample_key=0) == 1
        assert session.assign(np.zeros(1, dtype=np.float32), "", start_ms=4000, end_ms=5000, sample_key=1) == 0
        assert session.assign(B, "", start_ms=5000, end_ms=9000, sample_key=2) == 2
        turns = session.turns
        assert [(t.key, t.speaker_id, t.duration_ms) for t in turns] == [(0, 1, 3000), (1, 0, 1000), (2, 2, 4000)]
        assert turns[1].vector is None and np.allclose(turns[0].vector, A)
        assert session.confirm(2, 7)
        assert [t.speaker_id for t in session.turns] == [1, 0, 7]
        session.reset()
        assert session.turns == []

    def test_turns_are_not_kept_without_history(self):
        session = MeetingSpeakerSession(threshold=0.55)
        session.assign(A, "", sample_key=0)
        assert session.turns == []


@pytest.mark.parametrize("n", [2, 7, 40])
def test_linkage_matches_brute_force_on_random_points(n):
    rng = np.random.default_rng(n)
    vectors = rng.normal(size=(n, 5)).astype(np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    cut = 0.7
    labels = average_linkage_labels(vectors, cut)
    # Reference: naive average-linkage agglomeration.
    clusters = [[i] for i in range(n)]
    distance = 1.0 - vectors @ vectors.T
    while len(clusters) > 1:
        best = None
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                d = float(np.mean([distance[a, b] for a in clusters[i] for b in clusters[j]]))
                if best is None or d < best[0]:
                    best = (d, i, j)
        if best[0] > cut:
            break
        _, i, j = best
        clusters[i].extend(clusters.pop(j))
    expected = np.zeros(n, dtype=int)
    for rank, members in enumerate(sorted(clusters, key=min)):
        expected[members] = rank
    assert labels.tolist() == expected.tolist()

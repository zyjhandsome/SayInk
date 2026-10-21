"""End-of-session speaker re-assignment over every observed turn.

Online matching is greedy: the first noisy sample decides a number, a mixed
chunk can pollute a profile, and a merge-only pass over per-speaker centroids
can neither split a wrongly merged number nor move individual turns. This
pass clusters *all* turn embeddings (average-linkage, cosine) and re-assigns
each turn, so one person split across several numbers is folded back and a
turn stuck in the wrong group moves to the right one.

Manual identities keep their numbers; locked turns never move. Nothing here
touches audio or disk.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import numpy as np

from sayink.speaker_session import MAX_SPEAKERS

# Repeated long turns make a reference voice for resolving small groups.
MIN_CLUSTER_TURNS = 3
MIN_CLUSTER_SECONDS = 8.0
# Centroid similarity below which a group is plainly not the nearest voice.
# CAM++ groups of one person score far above it and different people mostly
# below; the mel fallback rarely scores this low, so it keeps folding.
DISTINCT_VOICE_SIMILARITY = 0.30


@dataclass(frozen=True)
class TurnObservation:
    key: int
    speaker_id: int
    vector: np.ndarray | None
    duration_ms: int = 0
    learnable: bool = True


def average_linkage_labels(vectors: np.ndarray, max_distance: float) -> np.ndarray:
    """Agglomerative clustering (average linkage) on cosine distance.

    ``vectors`` must be unit length. Clusters merge while their average
    pairwise distance is at most ``max_distance``. Returns one label per row,
    numbered by first appearance.
    """
    n = int(len(vectors))
    if n == 0:
        return np.zeros(0, dtype=np.int64)
    if n == 1:
        return np.zeros(1, dtype=np.int64)
    matrix = 1.0 - vectors.astype(np.float64) @ vectors.astype(np.float64).T
    np.fill_diagonal(matrix, np.inf)
    sizes = np.ones(n, dtype=np.float64)
    members: dict[int, list[int]] = {i: [i] for i in range(n)}
    row_min = matrix.min(axis=1)
    row_arg = matrix.argmin(axis=1)
    while len(members) > 1:
        i = int(np.argmin(row_min))
        if not np.isfinite(row_min[i]) or row_min[i] > max_distance:
            break
        j = int(row_arg[i])
        if i > j:
            i, j = j, i
        merged = (sizes[i] * matrix[i] + sizes[j] * matrix[j]) / (sizes[i] + sizes[j])
        matrix[i, :] = merged
        matrix[:, i] = merged
        matrix[i, i] = np.inf
        matrix[j, :] = np.inf
        matrix[:, j] = np.inf
        sizes[i] += sizes[j]
        members[i].extend(members.pop(j))
        row_min[j] = np.inf
        row_min[i] = matrix[i].min()
        row_arg[i] = int(matrix[i].argmin())
        # Every other row's distance to i changed; its minimum can only move
        # if i (or the vanished j) was its nearest, or i just became nearer.
        stale = np.flatnonzero((row_arg == i) | (row_arg == j))
        for k in stale:
            if k != i and np.isfinite(row_min[k]):
                row_min[k] = matrix[k].min()
                row_arg[k] = int(matrix[k].argmin())
        closer = np.flatnonzero(matrix[:, i] < row_min)
        row_min[closer] = matrix[closer, i]
        row_arg[closer] = i
    labels = np.zeros(n, dtype=np.int64)
    for rank, (_root, rows) in enumerate(sorted(members.items(), key=lambda item: min(item[1]))):
        labels[rows] = rank
    return labels


def reassign_turns(
    turns: list[TurnObservation],
    *,
    threshold: float,
    max_speakers: int = MAX_SPEAKERS,
    protected_ids: set[int] | None = None,
    locked_keys: set[int] | None = None,
    min_turns: int = MIN_CLUSTER_TURNS,
    min_seconds: float = MIN_CLUSTER_SECONDS,
) -> dict[int, int]:
    """Cluster every turn embedding and return ``{turn_key: speaker_id}``.

    ``turns`` are in time order. ``threshold`` is the cosine similarity at
    which turns still count as one voice. Groups with at least ``min_turns``
    turns and ``min_seconds`` of speech are reference voices. A smaller group
    that is plainly different from all of them (``DISTINCT_VOICE_SIMILARITY``)
    keeps a number of its own when it recurs or talks long; any other small
    group joins the nearest voice, or the speaker on both sides of it when it
    resembles no one. Turns without a usable vector are left out, and evidence
    made only of non-learnable turns may join a similar voice but never founds one.
    ``max_speakers`` folds the smallest groups into a similar voice, or leaves
    them unknown. ``protected_ids`` (named or manually corrected speakers) keep their
    numbers and their turns stay put; the cluster holding most of such a
    speaker's time lends that number to the automatic turns in it.
    ``locked_keys`` are never moved and anchor their speaker.
    Other clusters are numbered by first appearance, skipping taken numbers.
    Turns keyed below zero carry no transcript sequence; callers should fall
    back to the centroid merge when none of the turns is keyed.
    """
    protected = {int(x) for x in (protected_ids or ()) if int(x) > 0}
    locked = {int(k) for k in (locked_keys or ())}
    manual = {int(t.key): int(t.speaker_id) for t in turns
              if t.key in locked or t.speaker_id in protected}
    usable: list[TurnObservation] = []
    for turn in turns:
        if turn.vector is None:
            continue
        vec = np.asarray(turn.vector, dtype=np.float32).reshape(-1)
        if vec.size < 2 or not np.all(np.isfinite(vec)):
            continue
        norm = float(np.linalg.norm(vec))
        if norm < 1e-6:
            continue
        normalized = TurnObservation(int(turn.key), int(turn.speaker_id), vec / norm,
                                     max(0, int(turn.duration_ms)), bool(turn.learnable))
        usable.append(normalized)
    if not usable:
        return manual
    dims = {t.vector.size for t in usable}
    if len(dims) != 1:
        return manual
    vectors = np.stack([t.vector for t in usable])
    labels = average_linkage_labels(vectors, max(0.0, 1.0 - float(threshold)))
    durations = np.array([t.duration_ms / 1000.0 for t in usable], dtype=np.float64)

    def centroid(label: int) -> np.ndarray:
        total = vectors[labels == label].sum(axis=0)
        norm = float(np.linalg.norm(total))
        return total / norm if norm >= 1e-6 else total

    def nearest(vector: np.ndarray, references: dict[int, np.ndarray]) -> tuple[float, int]:
        return max((float(vector @ reference), label) for label, reference in references.items())

    manual_rows = [t.key in locked or t.speaker_id in protected for t in usable]

    def established() -> set[int]:
        # A manual correction vouches for its voice even on uncertain audio.
        return {int(labels[i]) for i, t in enumerate(usable) if t.learnable or manual_rows[i]}

    counts = {int(l): int(np.sum(labels == l)) for l in np.unique(labels)}
    seconds = {l: float(durations[labels == l].sum()) for l in counts}
    anchored = {int(labels[i]) for i in range(len(usable)) if manual_rows[i]}
    founded = established()
    big = [l for l in counts if l in anchored
           or (l in founded and counts[l] >= min_turns and seconds[l] >= min_seconds)]
    if big:
        references = {l: centroid(l) for l in big}
        small = sorted(set(counts) - set(big))
        # A recurring or long voice unlike every reference is someone else
        # (an interviewer's few long questions), not a fragment to fold away.
        kept = [l for l in small if l in founded and (counts[l] >= min_turns or seconds[l] >= min_seconds)
                and nearest(centroid(l), references)[0] < DISTINCT_VOICE_SIMILARITY]
        references.update({l: centroid(l) for l in kept})
        unlike: list[tuple[int, int]] = []
        for l in small:
            if l in kept:
                continue
            similarity, target = nearest(centroid(l), references)
            if similarity >= DISTINCT_VOICE_SIMILARITY:
                labels[labels == l] = target
            elif l in founded:
                unlike.append((l, target))
        # A short clear group unlike every voice (laughter, a cough) says little
        # about who made it; between turns of one speaker it stays with them.
        for l, target in unlike:
            rows = set(np.flatnonzero(labels == l).tolist())
            around = {int(labels[n]) for r in rows for n in (r - 1, r + 1)
                      if 0 <= n < len(usable) and n not in rows}
            labels[labels == l] = around.pop() if len(around) == 1 and around <= set(references) else target

    # Uncertain observations may reinforce a similar voice, but an uncertain
    # group unlike every voice (even a long one) is not a new identity.
    for label in set(labels.tolist()) - established():
        labels[labels == label] = -1

    # The cap is not evidence of identity. An excess group unlike every other
    # stays unknown; manual identities are never removed.
    def remaining() -> list[int]:
        return [int(l) for l in np.unique(labels) if l >= 0]

    while len(remaining()) > max(1, int(max_speakers)):
        current = remaining()
        anchored = {int(labels[i]) for i in range(len(usable)) if manual_rows[i]}
        candidates = [l for l in current if l not in anchored]
        if not candidates or len(current) < 2:
            break
        smallest = min(candidates, key=lambda l: (float(durations[labels == l].sum()), int(np.sum(labels == l))))
        similarity, target = nearest(centroid(smallest), {o: centroid(o) for o in current if o != smallest})
        labels[labels == smallest] = target if similarity >= DISTINCT_VOICE_SIMILARITY else -1

    # Number the clusters. Protected speakers claim the cluster holding most
    # of their time; locked turns keep their own number regardless.
    claim: dict[int, int] = {}
    if protected:
        share: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
        for i, t in enumerate(usable):
            if t.speaker_id in protected:
                share[t.speaker_id][int(labels[i])] += max(durations[i], 1e-3)
        for sid in sorted(protected, key=lambda s: -sum(share[s].values()) if s in share else 0.0):
            if sid not in share:
                continue
            for label, _weight in sorted(share[sid].items(), key=lambda item: -item[1]):
                if label not in claim:
                    claim[label] = sid
                    break
    taken = set(protected) | set(claim.values()) | {sid for sid in manual.values() if sid > 0}
    next_id = 1
    assignment: dict[int, int] = dict(manual)
    numbered: dict[int, int] = {}
    for i, t in enumerate(usable):
        label = int(labels[i])
        if t.key in locked or t.speaker_id in protected:
            # Manual evidence stays where the user put it; two named people
            # never collapse into one even when their voices cluster together.
            assignment[t.key] = t.speaker_id
            continue
        if label < 0:
            assignment[t.key] = 0
            continue
        if label not in numbered:
            if label in claim:
                numbered[label] = claim[label]
            else:
                while next_id in taken:
                    next_id += 1
                numbered[label] = next_id
                taken.add(next_id)
        assignment[t.key] = numbered[label]
    return assignment


def final_assignments(
    session,
    threshold: float,
    *,
    protected_ids: set[int] | None = None,
    locked_keys: set[int] | None = None,
) -> dict[int, int]:
    """End-of-session ``reassign_turns`` over a speaker session's keyed turns.

    Meetings, continuous dictation and the benchmark tools all finish here. A
    session that only ever learned one voice may place its unknown turns with
    that voice, but the end pass cannot discover new people in it.
    """
    turns = [t for t in (getattr(session, "turns", None) or []) if t.key >= 0]
    if not turns:
        return {}
    limit = MAX_SPEAKERS if int(getattr(session, "speaker_count", 0)) >= 2 else 1
    return reassign_turns(turns, threshold=threshold, max_speakers=limit,
                          protected_ids=protected_ids, locked_keys=locked_keys)

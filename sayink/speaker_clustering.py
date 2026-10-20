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

# Clustering slightly finer than the match bar and then folding every tiny
# cluster (fewer turns *or* less speech than this) into the nearest substantial
# one gave the right speaker count on real meetings; leaving tiny clusters
# alone only produced spare numbers that the cap then folded blindly. A voice
# that spoke for three turns or eight seconds keeps a number of its own.
MIN_CLUSTER_TURNS = 3
MIN_CLUSTER_SECONDS = 8.0


@dataclass(frozen=True)
class TurnObservation:
    key: int
    speaker_id: int
    vector: np.ndarray | None
    duration_ms: int = 0


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

    ``threshold`` is the cosine similarity at which turns still count as one
    voice. Turns without a usable vector are left out (they stay unknown).
    ``protected_ids`` (named or manually corrected speakers) keep their
    numbers and their turns stay put; the cluster holding most of such a
    speaker's time lends that number to the automatic turns in it.
    ``locked_keys`` are never moved and anchor their speaker.
    Other clusters are numbered by first appearance, skipping taken numbers.
    Turns keyed below zero carry no transcript sequence; callers should fall
    back to the centroid merge when none of the turns is keyed.
    """
    protected = {int(x) for x in (protected_ids or ()) if int(x) > 0}
    locked = {int(k) for k in (locked_keys or ())}
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
        usable.append(TurnObservation(int(turn.key), int(turn.speaker_id), vec / norm, max(0, int(turn.duration_ms))))
    if not usable:
        return {}
    dims = {t.vector.size for t in usable}
    if len(dims) != 1:
        return {}
    vectors = np.stack([t.vector for t in usable])
    labels = average_linkage_labels(vectors, max(0.0, 1.0 - float(threshold)))
    durations = np.array([t.duration_ms / 1000.0 for t in usable], dtype=np.float64)

    def centroid(mask: np.ndarray) -> np.ndarray:
        total = vectors[mask].sum(axis=0)
        norm = float(np.linalg.norm(total))
        return total / norm if norm >= 1e-6 else total

    # Fold tiny clusters into the nearest substantial one.
    counts = {int(l): int(np.sum(labels == l)) for l in np.unique(labels)}
    seconds = {l: float(durations[labels == l].sum()) for l in counts}
    anchored = {int(labels[i]) for i, t in enumerate(usable) if t.key in locked or t.speaker_id in protected}
    big = [l for l in counts if (counts[l] >= min_turns and seconds[l] >= min_seconds) or l in anchored]
    if big:
        centroids = {l: centroid(labels == l) for l in big}
        for l in sorted(set(counts) - set(big)):
            mask = labels == l
            c = centroid(mask)
            labels[mask] = max(big, key=lambda b: float(c @ centroids[b]))

    # Cap the number of automatic speakers: fold the smallest unanchored
    # clusters into their nearest neighbour.
    def remaining() -> list[int]:
        return [int(l) for l in np.unique(labels)]

    while len(remaining()) > max(1, int(max_speakers)):
        current = remaining()
        anchored = {int(labels[i]) for i, t in enumerate(usable) if t.key in locked or t.speaker_id in protected}
        candidates = [l for l in current if l not in anchored]
        if not candidates or len(current) < 2:
            break
        smallest = min(candidates, key=lambda l: (float(durations[labels == l].sum()), int(np.sum(labels == l))))
        others = [l for l in current if l != smallest]
        c = centroid(labels == smallest)
        nearest = max(others, key=lambda o: float(c @ centroid(labels == o)))
        labels[labels == smallest] = nearest

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
    taken = set(protected) | set(claim.values()) | {t.speaker_id for t in usable if t.key in locked and t.speaker_id > 0}
    next_id = 1
    assignment: dict[int, int] = {}
    numbered: dict[int, int] = {}
    for i, t in enumerate(usable):
        label = int(labels[i])
        if t.key in locked or t.speaker_id in protected:
            # Manual evidence stays where the user put it; two named people
            # never collapse into one even when their voices cluster together.
            assignment[t.key] = t.speaker_id
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

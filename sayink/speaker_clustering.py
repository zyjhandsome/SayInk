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
# Safety bound matching MeetingController: above 600 keyed turns, cluster a
# representative <=512-turn sample and project the rest onto its centroids.
MAX_UNBOUNDED_TURNS = 600
MAX_SAMPLED_CLUSTER_TURNS = 512


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
    v64 = vectors.astype(np.float64)
    matrix = 1.0 - v64 @ v64.T
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
        stale = stale[(stale != i) & np.isfinite(row_min[stale])]
        if stale.size:
            sub = matrix[stale]
            args = np.argmin(sub, axis=1)
            row_arg[stale] = args
            row_min[stale] = sub[np.arange(stale.size), args]
        closer = np.flatnonzero(matrix[:, i] < row_min)
        row_min[closer] = matrix[closer, i]
        row_arg[closer] = i
    labels = np.zeros(n, dtype=np.int64)
    for rank, (_root, rows) in enumerate(sorted(members.items(), key=lambda item: min(item[1]))):
        labels[rows] = rank
    return labels


def _normalize_usable_turns(turns: list[TurnObservation]) -> list[TurnObservation]:
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
        usable.append(
            TurnObservation(
                int(turn.key),
                int(turn.speaker_id),
                vec / norm,
                max(0, int(turn.duration_ms)),
                bool(turn.learnable),
            )
        )
    return usable


def _cluster_centroid(vectors: np.ndarray, labels: np.ndarray, label: int) -> np.ndarray:
    total = vectors[labels == label].sum(axis=0)
    norm = float(np.linalg.norm(total))
    return total / norm if norm >= 1e-6 else total


def _nearest_reference(vector: np.ndarray, references: dict[int, np.ndarray]) -> tuple[float, int]:
    return max((float(vector @ reference), label) for label, reference in references.items())


def _recurring_subsecond_labels(
    usable: list[TurnObservation],
    vectors: np.ndarray,
    labels: np.ndarray,
    counts: dict[int, int],
    base_founded: set[int],
    min_turns: int,
) -> set[int]:
    # Short (<1s) interjections ("好"/"对"/"没问题") are marked learnable=False
    # individually so a stray cough cannot seed an online profile, yet a real
    # second participant who repeatedly answers in <1s clips forms a tight,
    # distinct cluster over the session and must not stay speaker 0 forever.
    promoted: set[int] = set()
    base_refs = {l: _cluster_centroid(vectors, labels, l) for l in base_founded} if base_founded else {}
    for label in counts:
        if label in base_founded:
            continue
        sub_rows = [
            i for i, t in enumerate(usable)
            if int(labels[i]) == label and 0 < t.duration_ms < 1000 and t.speaker_id == 0
        ]
        if len(sub_rows) < min_turns:
            continue
        center = _cluster_centroid(vectors, labels, label)
        cohesion = float(np.mean(vectors[sub_rows] @ center))
        if cohesion < 0.80:
            continue
        if base_refs and _nearest_reference(center, base_refs)[0] >= DISTINCT_VOICE_SIMILARITY:
            continue
        promoted.add(label)
    return promoted


def _established_labels(
    usable: list[TurnObservation],
    vectors: np.ndarray,
    labels: np.ndarray,
    counts: dict[int, int],
    manual_rows: list[bool],
    min_turns: int,
) -> set[int]:
    # A manual correction vouches for its voice even on uncertain audio.
    base = {int(labels[i]) for i, t in enumerate(usable) if t.learnable or manual_rows[i]}
    return base | _recurring_subsecond_labels(usable, vectors, labels, counts, base, min_turns)


def _fold_small_clusters(
    usable: list[TurnObservation],
    vectors: np.ndarray,
    labels: np.ndarray,
    counts: dict[int, int],
    seconds: dict[int, float],
    *,
    anchored: set[int],
    founded: set[int],
    threshold: float,
    min_turns: int,
    min_seconds: float,
) -> None:
    fallback_seed = False
    big = [
        l for l in counts
        if l in anchored or (l in founded and counts[l] >= min_turns and seconds[l] >= min_seconds)
    ]
    if not big and founded:
        # Short meetings where no single cluster reached both min_turns and
        # min_seconds still need small-cluster folding so one speaker split
        # across noisy fragments merges instead of proliferating fake speakers.
        big = [l for l in counts if l in founded and (counts[l] >= min_turns or seconds[l] >= min_seconds)]
        if not big:
            big = [max(founded, key=lambda l: (seconds[l], counts[l], -l))]
            fallback_seed = True
    if not big:
        return
    references = {l: _cluster_centroid(vectors, labels, l) for l in big}
    small = sorted(set(counts) - set(big))
    merge_floor = (
        max(DISTINCT_VOICE_SIMILARITY, float(threshold) - 0.15)
        if fallback_seed
        else DISTINCT_VOICE_SIMILARITY
    )
    # A recurring or long voice unlike every reference is someone else
    # (an interviewer's few long questions), not a fragment to fold away.
    # In fallback_seed short meetings (where no cluster has reached min_turns
    # or min_seconds yet), any clear founded voice below merge_floor is kept
    # as an independent speaker rather than collapsed into the single seed.
    kept = [
        l for l in small
        if l in founded
        and (fallback_seed or counts[l] >= min_turns or seconds[l] >= min_seconds)
        and _nearest_reference(_cluster_centroid(vectors, labels, l), references)[0] < merge_floor
    ]
    references.update({l: _cluster_centroid(vectors, labels, l) for l in kept})
    unlike: list[tuple[int, int]] = []
    for l in small:
        if l in kept:
            continue
        similarity, target = _nearest_reference(_cluster_centroid(vectors, labels, l), references)
        if similarity >= merge_floor:
            labels[labels == l] = target
        elif l in founded and not fallback_seed:
            unlike.append((l, target))
    # A short clear group unlike every voice (laughter, a cough) says little
    # about who made it; between turns of one speaker it stays with them.
    for l, target in unlike:
        rows = set(np.flatnonzero(labels == l).tolist())
        around = {
            int(labels[n])
            for r in rows
            for n in (r - 1, r + 1)
            if 0 <= n < len(usable) and n not in rows
        }
        labels[labels == l] = around.pop() if len(around) == 1 and around <= set(references) else target


def _enforce_speaker_cap(
    vectors: np.ndarray,
    labels: np.ndarray,
    durations: np.ndarray,
    manual_rows: list[bool],
    max_speakers: int,
) -> None:
    limit = max(1, int(max_speakers))
    while True:
        current = [int(l) for l in np.unique(labels) if l >= 0]
        if len(current) <= limit:
            break
        anchored = {int(labels[i]) for i in range(len(manual_rows)) if manual_rows[i]}
        candidates = [l for l in current if l not in anchored]
        if not candidates or len(current) < 2:
            break
        smallest = min(
            candidates,
            key=lambda l: (float(durations[labels == l].sum()), int(np.sum(labels == l))),
        )
        others = {o: _cluster_centroid(vectors, labels, o) for o in current if o != smallest}
        similarity, target = _nearest_reference(_cluster_centroid(vectors, labels, smallest), others)
        labels[labels == smallest] = target if similarity >= DISTINCT_VOICE_SIMILARITY else -1


def _claim_protected_clusters(
    usable: list[TurnObservation],
    labels: np.ndarray,
    durations: np.ndarray,
    protected: set[int],
) -> dict[int, int]:
    claim: dict[int, int] = {}
    if not protected:
        return claim
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
    return claim


def _number_clusters(
    usable: list[TurnObservation],
    labels: np.ndarray,
    durations: np.ndarray,
    *,
    protected: set[int],
    locked: set[int],
    manual: dict[int, int],
) -> dict[int, int]:
    claim = _claim_protected_clusters(usable, labels, durations, protected)
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
    manual = {
        int(t.key): int(t.speaker_id)
        for t in turns
        if t.key in locked or t.speaker_id in protected
    }
    usable = _normalize_usable_turns(turns)
    if not usable:
        return manual
    dims = {t.vector.size for t in usable if t.vector is not None}
    if len(dims) != 1:
        return manual
    vectors = np.stack([t.vector for t in usable if t.vector is not None])
    labels = average_linkage_labels(vectors, max(0.0, 1.0 - float(threshold)))
    durations = np.array([t.duration_ms / 1000.0 for t in usable], dtype=np.float64)

    manual_rows = [t.key in locked or t.speaker_id in protected for t in usable]
    counts = {int(l): int(np.sum(labels == l)) for l in np.unique(labels)}
    seconds = {l: float(durations[labels == l].sum()) for l in counts}
    anchored = {int(labels[i]) for i in range(len(usable)) if manual_rows[i]}
    founded = _established_labels(usable, vectors, labels, counts, manual_rows, min_turns)
    _fold_small_clusters(
        usable,
        vectors,
        labels,
        counts,
        seconds,
        anchored=anchored,
        founded=founded,
        threshold=threshold,
        min_turns=min_turns,
        min_seconds=min_seconds,
    )

    # Uncertain observations may reinforce a similar voice, but an uncertain
    # group unlike every voice (even a long one) is not a new identity.
    established_after = _established_labels(usable, vectors, labels, counts, manual_rows, min_turns)
    for label in set(labels.tolist()) - established_after:
        labels[labels == label] = -1

    # The cap is not evidence of identity. An excess group unlike every other
    # stays unknown; manual identities are never removed.
    _enforce_speaker_cap(vectors, labels, durations, manual_rows, max_speakers)
    return _number_clusters(
        usable, labels, durations, protected=protected, locked=locked, manual=manual
    )


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
    that voice, unless a recurring sub-second responder spoke only in ``<1s``
    clips that could not seed an online profile individually.
    """
    turns = [t for t in (getattr(session, "turns", None) or []) if t.key >= 0]
    if not turns:
        return {}
    subsecond_unknowns = sum(
        1 for t in turns if 0 < t.duration_ms < 1000 and t.speaker_id == 0 and t.vector is not None
    )
    multi_voice_candidates = (
        int(getattr(session, "speaker_count", 0)) >= 2
        or len(protected_ids or ()) >= 2
        or subsecond_unknowns >= MIN_CLUSTER_TURNS
    )
    limit = MAX_SPEAKERS if multi_voice_candidates else 1
    return reassign_turns(turns, threshold=threshold, max_speakers=limit,
                          protected_ids=protected_ids, locked_keys=locked_keys)


class _SampledSpeakerView:
    """Bounded turn view passed into ``final_assignments`` on multi-hour sessions."""

    def __init__(self, turns: list[TurnObservation], confirmed: dict[int, int], speaker_count: int) -> None:
        self.turns = tuple(turns)
        self.confirmed = dict(confirmed)
        self.speaker_count = int(speaker_count)


def _subsample_evenly(indices: list[int], limit: int) -> list[int]:
    if limit <= 0 or not indices:
        return []
    if len(indices) <= limit:
        return list(indices)
    step = len(indices) / limit
    return [indices[min(len(indices) - 1, int(i * step))] for i in range(limit)]


def _select_bounded_turn_indices(
    turns: list[TurnObservation],
    *,
    confirmed: dict[int, int],
    protected: set[int],
    locked: set[int],
    sample_budget: int,
    max_turns: int = MAX_UNBOUNDED_TURNS,
) -> list[int]:
    hard_cap = max(1, int(max_turns))
    target = min(hard_cap, max(1, int(sample_budget)))
    anchors: list[int] = []
    pinned: list[int] = []
    protected_pool: list[int] = []
    optional: list[int] = []
    seen_first_for_speaker: set[int] = set()
    for idx, turn in enumerate(turns):
        is_anchor = turn.speaker_id > 0 and turn.speaker_id not in seen_first_for_speaker
        if is_anchor:
            seen_first_for_speaker.add(turn.speaker_id)
            anchors.append(idx)
        elif turn.key in locked or turn.key in confirmed:
            pinned.append(idx)
        elif turn.speaker_id in protected:
            protected_pool.append(idx)
        else:
            optional.append(idx)

    if len(anchors) >= hard_cap:
        return sorted(_subsample_evenly(anchors, hard_cap))

    # Unsampled confirmed/locked/protected turns still keep their exact speaker
    # during projection, so never let a huge pinned/protected pool starve
    # optional turns from other speakers out of the clustering sample.
    budget_after_anchors = max(0, target - len(anchors))
    if len(pinned) + len(protected_pool) + len(optional) <= budget_after_anchors:
        return sorted(anchors + pinned + protected_pool + optional)[:hard_cap]

    opt_quota = min(len(optional), max(MIN_CLUSTER_TURNS * 2, budget_after_anchors // 3)) if optional else 0
    rem_for_kept = max(0, budget_after_anchors - opt_quota)
    pin_quota = min(len(pinned), rem_for_kept)
    prot_quota = min(len(protected_pool), max(0, rem_for_kept - pin_quota))
    spare = max(0, budget_after_anchors - opt_quota - pin_quota - prot_quota)
    if spare > 0 and len(optional) > opt_quota:
        add_opt = min(spare, len(optional) - opt_quota)
        opt_quota += add_opt
        spare -= add_opt
    if spare > 0 and len(protected_pool) > prot_quota:
        prot_quota += min(spare, len(protected_pool) - prot_quota)

    chosen = set(anchors)
    chosen.update(_subsample_evenly(pinned, pin_quota))
    chosen.update(_subsample_evenly(protected_pool, prot_quota))
    chosen.update(_subsample_evenly(optional, opt_quota))
    return sorted(chosen)[:hard_cap]


def _build_cluster_projection(
    sampled_turns: list[TurnObservation],
    core: dict[int, int],
) -> tuple[dict[int, np.ndarray], dict[int, int]]:
    cluster_vecs: dict[int, list[np.ndarray]] = {}
    speaker_vote: dict[int, dict[int, int]] = {}
    for turn in sampled_turns:
        sid = core.get(turn.key, turn.speaker_id)
        if sid > 0 and turn.learnable and turn.vector is not None and len(turn.vector) > 0:
            cluster_vecs.setdefault(sid, []).append(np.asarray(turn.vector, dtype=np.float32))
        if turn.speaker_id > 0 and sid > 0:
            votes = speaker_vote.setdefault(turn.speaker_id, {})
            votes[sid] = votes.get(sid, 0) + 1
    centroids: dict[int, np.ndarray] = {}
    for sid, vecs in cluster_vecs.items():
        mean = np.mean(np.stack(vecs, axis=0), axis=0)
        norm = float(np.linalg.norm(mean))
        if norm > 1e-8:
            centroids[sid] = (mean / norm).astype(np.float32)
    fallback_map = {
        old_sid: max(votes.items(), key=lambda kv: kv[1])[0]
        for old_sid, votes in speaker_vote.items()
        if votes
    }
    return centroids, fallback_map


def _project_turn_speaker(
    turn: TurnObservation,
    *,
    threshold: float,
    centroids: dict[int, np.ndarray],
    fallback_map: dict[int, int],
) -> int:
    best_sid = 0
    best_sim = -1.0
    if centroids and turn.vector is not None and len(turn.vector) > 0:
        vec = np.asarray(turn.vector, dtype=np.float32)
        vnorm = float(np.linalg.norm(vec))
        if vnorm > 1e-8:
            uvec = vec / vnorm
            for sid, cvec in centroids.items():
                if cvec.shape == uvec.shape:
                    sim = float(np.dot(uvec, cvec))
                    if sim > best_sim:
                        best_sim = sim
                        best_sid = sid
    if best_sid > 0 and best_sim >= threshold:
        return best_sid
    if turn.speaker_id in fallback_map:
        return fallback_map[turn.speaker_id]
    return turn.speaker_id


def bounded_final_assignments(
    session,
    threshold: float,
    *,
    protected_ids: set[int] | None = None,
    locked_keys: set[int] | None = None,
    max_turns: int = MAX_UNBOUNDED_TURNS,
    sample_budget: int = MAX_SAMPLED_CLUSTER_TURNS,
    cancel=None,
) -> dict[int, int]:
    """Bounded end-of-session speaker assignment for multi-hour recordings/imports.

    Matches ``final_assignments`` directly when keyed turns do not exceed
    ``max_turns`` (default 600). Above that ceiling, clusters a representative
    ``<= sample_budget`` subset (preserving speaker anchors, confirmed samples,
    and manual protections) and projects remaining turns onto cluster centroids
    in O(N) time. Returns ``{}`` immediately when ``cancel()`` is true.
    """
    if cancel is not None and cancel():
        return {}
    raw_turns = list(getattr(session, "turns", None) or [])
    keyed = [t for t in raw_turns if t.key >= 0]
    if not keyed:
        return {}
    protected = {int(x) for x in (protected_ids or ()) if int(x) > 0}
    locked = {int(k) for k in (locked_keys or ())}
    if len(keyed) <= max(1, int(max_turns)):
        return final_assignments(
            session, threshold, protected_ids=protected, locked_keys=locked
        )
    confirmed = dict(getattr(session, "confirmed", None) or {})
    chosen_indices = _select_bounded_turn_indices(
        keyed,
        confirmed=confirmed,
        protected=protected,
        locked=locked,
        sample_budget=sample_budget,
        max_turns=max_turns,
    )
    sampled_turns = [keyed[i] for i in chosen_indices]
    sampled_keys = {t.key for t in sampled_turns}
    sampled_confirmed = {k: v for k, v in confirmed.items() if k in sampled_keys}
    core = final_assignments(
        _SampledSpeakerView(
            sampled_turns,
            sampled_confirmed,
            int(getattr(session, "speaker_count", 0)),
        ),
        threshold,
        protected_ids=protected,
        locked_keys=locked & sampled_keys,
    )
    if cancel is not None and cancel():
        return {}
    centroids, fallback_map = _build_cluster_projection(sampled_turns, core)
    full: dict[int, int] = dict(core)
    for idx, turn in enumerate(keyed):
        if cancel is not None and (idx & 63) == 0 and cancel():
            return {}
        if turn.key in full:
            continue
        if turn.key in confirmed:
            full[turn.key] = confirmed[turn.key]
            continue
        if turn.key in locked or turn.speaker_id in protected:
            full[turn.key] = turn.speaker_id
            continue
        full[turn.key] = _project_turn_speaker(
            turn, threshold=threshold, centroids=centroids, fallback_map=fallback_map
        )
    return full

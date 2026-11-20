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
# Minimum cosine similarity required to merge or fold observations across
# distinct hardware capture routes ("mic" vs "system").
CROSS_ROUTE_MIN_SIMILARITY = 0.80
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
    route: str = ""


def _routes_conflict(routes_a: set[str], routes_b: set[str]) -> bool:
    """True when two non-empty hardware route sets differ (e.g. ``mic`` vs ``system``)."""
    return bool(routes_a and routes_b and routes_a != routes_b)


def _cluster_route_map(usable: list[TurnObservation], labels: np.ndarray) -> dict[int, set[str]]:
    route_map: dict[int, set[str]] = defaultdict(set)
    for idx, turn in enumerate(usable):
        lbl = int(labels[idx])
        if lbl >= 0 and turn.route:
            route_map[lbl].add(turn.route)
    return route_map


def _best_reference_match(
    vector: np.ndarray,
    source_routes: set[str],
    references: dict[int, np.ndarray],
    ref_routes: dict[int, set[str]],
    base_floor: float,
) -> tuple[float, int | None]:
    best_sim = -2.0
    best_label: int | None = None
    for label, ref_vec in references.items():
        sim = float(vector @ ref_vec)
        req = (
            max(base_floor, CROSS_ROUTE_MIN_SIMILARITY)
            if _routes_conflict(source_routes, ref_routes.get(label, set()))
            else base_floor
        )
        if sim >= req and (best_label is None or (sim, label) > (best_sim, best_label)):
            best_sim = sim
            best_label = label
    return best_sim, best_label


def average_linkage_labels(
    vectors: np.ndarray,
    max_distance: float,
    *,
    routes: list[str] | tuple[str, ...] | None = None,
) -> np.ndarray:
    """Agglomerative clustering (average linkage) on cosine distance.

    ``vectors`` must be unit length. Clusters merge while their average
    pairwise distance is at most ``max_distance`` (and at most
    ``1 - CROSS_ROUTE_MIN_SIMILARITY`` across conflicting non-empty ``routes``).
    Returns one label per row, numbered by first appearance.
    """
    n = int(len(vectors))
    if n == 0:
        return np.zeros(0, dtype=np.int64)
    if n == 1:
        return np.zeros(1, dtype=np.int64)
    v64 = vectors.astype(np.float64)
    raw_matrix = 1.0 - v64 @ v64.T
    np.fill_diagonal(raw_matrix, np.inf)
    has_routes = bool(routes is not None and len(routes) == n and any(routes))
    if not has_routes:
        matrix = raw_matrix
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

    assert routes is not None
    cross_max_distance = min(max_distance, max(0.0, 1.0 - CROSS_ROUTE_MIN_SIMILARITY))
    cluster_routes: dict[int, set[str]] = {
        i: ({str(routes[i]).strip()} if str(routes[i]).strip() else set())
        for i in range(n)
    }
    eff_matrix = raw_matrix.copy()
    for a in range(n):
        r_a = cluster_routes[a]
        if not r_a:
            continue
        for b in range(a + 1, n):
            r_b = cluster_routes[b]
            if r_b and r_a != r_b and eff_matrix[a, b] > cross_max_distance:
                eff_matrix[a, b] = np.inf
                eff_matrix[b, a] = np.inf
    sizes = np.ones(n, dtype=np.float64)
    members = {i: [i] for i in range(n)}
    row_min = eff_matrix.min(axis=1)
    row_arg = eff_matrix.argmin(axis=1)
    while len(members) > 1:
        i = int(np.argmin(row_min))
        if not np.isfinite(row_min[i]) or row_min[i] > max_distance:
            break
        j = int(row_arg[i])
        if i > j:
            i, j = j, i
        merged = (sizes[i] * raw_matrix[i] + sizes[j] * raw_matrix[j]) / (sizes[i] + sizes[j])
        raw_matrix[i, :] = merged
        raw_matrix[:, i] = merged
        raw_matrix[i, i] = np.inf
        raw_matrix[j, :] = np.inf
        raw_matrix[:, j] = np.inf
        sizes[i] += sizes[j]
        members[i].extend(members.pop(j))
        cluster_routes[i].update(cluster_routes.pop(j))

        eff_row = np.full(n, np.inf, dtype=np.float64)
        r_i = cluster_routes[i]
        for k, r_k in cluster_routes.items():
            if k == i:
                continue
            d = raw_matrix[i, k]
            if _routes_conflict(r_i, r_k) and d > cross_max_distance:
                d = np.inf
            eff_row[k] = d
        eff_matrix[i, :] = eff_row
        eff_matrix[:, i] = eff_row
        eff_matrix[j, :] = np.inf
        eff_matrix[:, j] = np.inf

        row_min[j] = np.inf
        row_min[i] = eff_matrix[i].min()
        row_arg[i] = int(eff_matrix[i].argmin())
        stale = np.flatnonzero((row_arg == i) | (row_arg == j))
        stale = stale[(stale != i) & np.isfinite(row_min[stale])]
        if stale.size:
            sub = eff_matrix[stale]
            args = np.argmin(sub, axis=1)
            row_arg[stale] = args
            row_min[stale] = sub[np.arange(stale.size), args]
        closer = np.flatnonzero(eff_matrix[:, i] < row_min)
        row_min[closer] = eff_matrix[closer, i]
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
                str(getattr(turn, "route", "") or "").strip(),
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
    route_map = _cluster_route_map(usable, labels)
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
        if (
            base_refs
            and _best_reference_match(
                center, route_map.get(label, set()), base_refs, route_map, DISTINCT_VOICE_SIMILARITY
            )[1]
            is not None
        ):
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
    route_map = _cluster_route_map(usable, labels)
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
    kept: list[int] = []
    for l in small:
        if (
            l in founded
            and (fallback_seed or counts[l] >= min_turns or seconds[l] >= min_seconds)
            and _best_reference_match(
                _cluster_centroid(vectors, labels, l),
                route_map.get(l, set()),
                references,
                route_map,
                merge_floor,
            )[1]
            is None
        ):
            kept.append(l)
            references[l] = _cluster_centroid(vectors, labels, l)

    # Never collapse a founded speaker on an isolated hardware route (e.g. a
    # headphone "mic" user who spoke <3 turns / <8s) into an incompatible
    # "system" speaker unless cross-route similarity reaches 0.80.
    kept_set = set(kept)
    for l in sorted(
        (c for c in small if c in founded and c not in kept_set and route_map.get(c)),
        key=lambda c: (-seconds[c], -counts[c], c),
    ):
        r_l = route_map.get(l, set())
        has_compat_ref = any(not _routes_conflict(r_l, route_map.get(ref, set())) for ref in references)
        if has_compat_ref:
            continue
        center = _cluster_centroid(vectors, labels, l)
        if _best_reference_match(center, r_l, references, route_map, merge_floor)[1] is None:
            kept.append(l)
            kept_set.add(l)
            references[l] = center

    unlike: list[tuple[int, int]] = []
    for l in small:
        if l in kept_set:
            continue
        r_l = route_map.get(l, set())
        center = _cluster_centroid(vectors, labels, l)
        _sim, target = _best_reference_match(center, r_l, references, route_map, merge_floor)
        if target is not None:
            labels[labels == l] = target
            route_map[target].update(r_l)
        elif l in founded and not fallback_seed:
            compat_refs = {
                ref: ref_vec
                for ref, ref_vec in references.items()
                if not _routes_conflict(r_l, route_map.get(ref, set()))
            }
            if compat_refs:
                unlike.append((l, _nearest_reference(center, compat_refs)[1]))
    # A short clear group unlike every voice (laughter, a cough) says little
    # about who made it; between turns of one speaker on a compatible route it stays with them.
    for l, target in unlike:
        r_l = route_map.get(l, set())
        compat_set = {
            ref for ref in references if not _routes_conflict(r_l, route_map.get(ref, set()))
        }
        rows = set(np.flatnonzero(labels == l).tolist())
        around = {
            int(labels[n])
            for r in rows
            for n in (r - 1, r + 1)
            if 0 <= n < len(usable) and n not in rows
        }
        chosen = around.pop() if len(around) == 1 and around <= compat_set else target
        labels[labels == l] = chosen
        route_map[chosen].update(r_l)


def _enforce_speaker_cap(
    usable: list[TurnObservation],
    vectors: np.ndarray,
    labels: np.ndarray,
    durations: np.ndarray,
    manual_rows: list[bool],
    max_speakers: int,
) -> None:
    limit = max(1, int(max_speakers))
    route_map = _cluster_route_map(usable, labels)
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
        r_small = route_map.get(smallest, set())
        _sim, target = _best_reference_match(
            _cluster_centroid(vectors, labels, smallest),
            r_small,
            others,
            route_map,
            DISTINCT_VOICE_SIMILARITY,
        )
        if target is not None:
            labels[labels == smallest] = target
            route_map[target].update(r_small)
        else:
            labels[labels == smallest] = -1


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
    turn_routes = [t.route for t in usable]
    labels = average_linkage_labels(
        vectors,
        max(0.0, 1.0 - float(threshold)),
        routes=turn_routes,
    )
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
    _enforce_speaker_cap(usable, vectors, labels, durations, manual_rows, max_speakers)
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
    distinct_routes = {
        str(getattr(t, "route", "") or "").strip()
        for t in turns
        if getattr(t, "route", "") and t.learnable and t.vector is not None
    }
    multi_voice_candidates = (
        int(getattr(session, "speaker_count", 0)) >= 2
        or len(protected_ids or ()) >= 2
        or subsecond_unknowns >= MIN_CLUSTER_TURNS
        or len(distinct_routes) >= 2
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
) -> tuple[dict[int, np.ndarray], dict[int, int], dict[int, set[str]]]:
    cluster_vecs: dict[int, list[np.ndarray]] = {}
    cluster_routes: dict[int, set[str]] = defaultdict(set)
    speaker_vote: dict[int, dict[int, int]] = {}
    for turn in sampled_turns:
        sid = core.get(turn.key, turn.speaker_id)
        if sid > 0 and turn.learnable and turn.vector is not None and len(turn.vector) > 0:
            cluster_vecs.setdefault(sid, []).append(np.asarray(turn.vector, dtype=np.float32))
            t_route = str(getattr(turn, "route", "") or "").strip()
            if t_route:
                cluster_routes[sid].add(t_route)
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
    return centroids, fallback_map, cluster_routes


def _project_turn_speaker(
    turn: TurnObservation,
    *,
    threshold: float,
    centroids: dict[int, np.ndarray],
    fallback_map: dict[int, int],
    cluster_routes: dict[int, set[str]] | None = None,
) -> int:
    best_sid = 0
    best_sim = -1.0
    t_route = str(getattr(turn, "route", "") or "").strip()
    t_routes = {t_route} if t_route else set()
    if centroids and turn.vector is not None and len(turn.vector) > 0:
        vec = np.asarray(turn.vector, dtype=np.float32)
        vnorm = float(np.linalg.norm(vec))
        if vnorm > 1e-8:
            uvec = vec / vnorm
            for sid, cvec in centroids.items():
                if cvec.shape == uvec.shape:
                    sim = float(np.dot(uvec, cvec))
                    req = (
                        max(threshold, CROSS_ROUTE_MIN_SIMILARITY)
                        if cluster_routes is not None
                        and _routes_conflict(t_routes, cluster_routes.get(sid, set()))
                        else threshold
                    )
                    if sim >= req and sim > best_sim:
                        best_sim = sim
                        best_sid = sid
    if best_sid > 0:
        return best_sid
    if turn.speaker_id in fallback_map:
        candidate = fallback_map[turn.speaker_id]
        if cluster_routes is None or not _routes_conflict(t_routes, cluster_routes.get(candidate, set())):
            return candidate
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
    centroids, fallback_map, cluster_routes = _build_cluster_projection(sampled_turns, core)
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
            turn,
            threshold=threshold,
            centroids=centroids,
            fallback_map=fallback_map,
            cluster_routes=cluster_routes,
        )
    return full

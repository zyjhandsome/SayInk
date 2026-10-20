"""Session-scoped speaker labels for one continuous transcription.

Numbers restart every session. At most eight people are kept; a further
voice is merged into the closest existing one. Nothing is stored on disk.
"""

from __future__ import annotations

import threading

import numpy as np

from sayink.audio_utils import TARGET_SAMPLE_RATE

MAX_SPEAKERS = 8
MATCH_THRESHOLD = 0.86
_QUIET_RMS = 0.01
_N_MELS = 24
_N_FFT = 512
_EMBED_DIM = _N_MELS * 2


def dominant_route(mic_energy: float, system_energy: float, *, ratio: float = 3.0) -> str:
    """Return mic or system when one capture lane clearly leads this segment."""
    mic = max(0.0, float(mic_energy))
    system = max(0.0, float(system_energy))
    if mic <= 0.0 and system <= 0.0:
        return ""
    if system <= 0.0 or mic >= system * ratio:
        return "mic"
    if mic <= 0.0 or system >= mic * ratio:
        return "system"
    return ""


def _hz_to_mel(hz: np.ndarray) -> np.ndarray:
    return 2595.0 * np.log10(1.0 + hz / 700.0)


def _mel_to_hz(mel: np.ndarray) -> np.ndarray:
    return 700.0 * (10.0 ** (mel / 2595.0) - 1.0)


def _mel_filterbank(sample_rate: int) -> np.ndarray:
    n_freqs = _N_FFT // 2 + 1
    points = np.linspace(_hz_to_mel(np.array(0.0)), _hz_to_mel(np.array(sample_rate / 2)), _N_MELS + 2)
    bins = np.floor((_N_FFT + 1) * _mel_to_hz(points) / sample_rate).astype(int)
    bank = np.zeros((_N_MELS, n_freqs), dtype=np.float32)
    for index in range(_N_MELS):
        left, center, right = int(bins[index]), int(bins[index + 1]), int(bins[index + 2])
        if center <= left:
            center = left + 1
        if right <= center:
            right = center + 1
        for bin_index in range(left, center):
            if 0 <= bin_index < n_freqs:
                bank[index, bin_index] = (bin_index - left) / (center - left)
        for bin_index in range(center, right):
            if 0 <= bin_index < n_freqs:
                bank[index, bin_index] = (right - bin_index) / (right - center)
    return bank


_MEL_BANK = _mel_filterbank(TARGET_SAMPLE_RATE)


def voice_embedding(audio: np.ndarray, sample_rate: int = TARGET_SAMPLE_RATE) -> np.ndarray:
    """Unit log-mel summary. Quiet or tiny clips return a zero vector."""
    samples = np.asarray(audio, dtype=np.float32).reshape(-1)
    empty = np.zeros(_EMBED_DIM, dtype=np.float32)
    if samples.size < sample_rate // 10:
        return empty
    rms = float(np.sqrt(np.mean(samples * samples)))
    if rms < _QUIET_RMS:
        return empty
    centered = samples - float(np.mean(samples))
    framed = np.append(centered[:1], centered[1:] - 0.97 * centered[:-1])
    frame_len = int(sample_rate * 0.025)
    hop = int(sample_rate * 0.010)
    if framed.size < frame_len:
        return empty
    window = np.hamming(frame_len).astype(np.float32)
    n_frames = 1 + (framed.size - frame_len) // hop
    bank = _MEL_BANK if sample_rate == TARGET_SAMPLE_RATE else _mel_filterbank(sample_rate)
    mels = np.empty((n_frames, _N_MELS), dtype=np.float32)
    for index in range(n_frames):
        start = index * hop
        spectrum = np.abs(np.fft.rfft(framed[start : start + frame_len] * window, n=_N_FFT)) ** 2
        mels[index] = np.log(bank @ spectrum[: bank.shape[1]] + 1e-6)
    mean = mels.mean(axis=0)
    centered = (mean - float(np.median(mean))).astype(np.float32)
    peak = np.zeros(_N_MELS, dtype=np.float32)
    peak[int(np.argmax(mean))] = 3.0
    features = np.concatenate([centered, peak]).astype(np.float32)
    norm = float(np.linalg.norm(features))
    if norm < 1e-6:
        return empty
    return features / norm


class _Cluster:
    def __init__(self, speaker_id: int, route: str, centroid: np.ndarray):
        self.speaker_id = speaker_id
        self.route = route
        self.centroid = centroid
        self.count = 1


class SpeakerSession:
    """Match each segment to a speaker number that lives only for this session.

    ``threshold`` is the cosine similarity above which a segment joins an
    existing speaker; the log-mel summary needs a high one (0.86), a real
    speaker-embedding model (CAM++) a lower one (see speaker_embedding.py).
    ``keep_history`` retains every (speaker_id, embedding) pair so the
    meeting page can re-cluster once the whole meeting has been heard.

    ``sticky_threshold`` adds a continuity prior for meetings: a segment
    that starts within ``sticky_gap_ms`` of the previous one and is at least
    this similar to the previous speaker stays with that speaker instead of
    opening a new number. One person finishing a thought is far more common
    than two people alternating every sentence, and the heuristic embedding
    drifts with *what* is said more than with *who* says it.
    """

    def __init__(
        self,
        *,
        threshold: float = MATCH_THRESHOLD,
        max_speakers: int = MAX_SPEAKERS,
        keep_history: bool = False,
        sticky_threshold: float | None = None,
        sticky_gap_ms: int = 0,
    ) -> None:
        self._clusters: list[_Cluster] = []
        self._lock = threading.RLock()
        self._next_id = 1
        self._last_id = 0
        self._last_end_ms = 0
        self._threshold = float(threshold)
        self._max_speakers = int(max_speakers)
        self._keep_history = bool(keep_history)
        self._history: list[tuple[int, np.ndarray]] = []
        self._sticky_threshold = float(sticky_threshold) if sticky_threshold is not None else None
        self._sticky_gap_ms = max(0, int(sticky_gap_ms))

    @property
    def threshold(self) -> float:
        return self._threshold

    @property
    def speaker_count(self) -> int:
        return len(self._clusters)

    @property
    def history(self) -> list[tuple[int, np.ndarray]]:
        """(speaker_id, embedding) for every assigned segment, in order."""
        with self._lock:
            return list(self._history)

    def reserve_speaker_id(self, minimum: int = 1) -> int:
        """Reserve a manual identity without teaching it to the voice matcher."""
        with self._lock:
            speaker_id = max(self._next_id, int(minimum))
            self._next_id = speaker_id + 1
            return speaker_id

    def reset(self) -> None:
        with self._lock:
            self._clusters.clear()
            self._next_id = 1
            self._last_id = 0
            self._last_end_ms = 0
            self._history.clear()

    def assign(
        self,
        embedding: np.ndarray,
        route: str = "",
        *,
        start_ms: int = 0,
        end_ms: int = 0,
    ) -> int:
        """Speaker number for this segment. ``start_ms``/``end_ms`` (wall clock)
        feed the continuity prior; leave them 0 to disable it for this call."""
        with self._lock:
            speaker_id = self._assign(embedding, route, int(start_ms or 0))
            if end_ms:
                self._last_end_ms = int(end_ms)
            elif start_ms:
                self._last_end_ms = int(start_ms)
            if self._keep_history:
                vector = np.asarray(embedding, dtype=np.float32).reshape(-1)
                self._history.append((speaker_id, vector.copy()))
            return speaker_id

    def _assign(self, embedding: np.ndarray, route: str, start_ms: int) -> int:
        route = route if route in ("mic", "system") else ""
        vector = np.asarray(embedding, dtype=np.float32).reshape(-1)
        if vector.size == 0 or float(np.linalg.norm(vector)) < 1e-6:
            if self._last_id:
                return self._last_id
            return 1

        match = self._closest(vector, route=route)
        if match is not None and match[1] >= self._threshold:
            self._absorb(match[0], vector, route)
            return match[0].speaker_id

        sticky = self._sticky_match(vector, route, start_ms)
        if sticky is not None:
            self._absorb(sticky, vector, route)
            return sticky.speaker_id

        if len(self._clusters) >= self._max_speakers:
            fallback = self._closest(vector, route=None)
            if fallback is None:
                return self._last_id or 1
            self._absorb(fallback[0], vector, route)
            return fallback[0].speaker_id

        speaker_id = self.reserve_speaker_id()
        self._clusters.append(_Cluster(speaker_id, route, vector.copy()))
        self._last_id = speaker_id
        return speaker_id

    def _sticky_match(self, vector: np.ndarray, route: str, start_ms: int) -> _Cluster | None:
        """The previous speaker, when this segment follows closely and sounds
        at least vaguely like them (see class docstring)."""
        if self._sticky_threshold is None or not self._last_id:
            return None
        if not start_ms or not self._last_end_ms:
            return None
        if start_ms - self._last_end_ms > self._sticky_gap_ms:
            return None
        last = next((c for c in self._clusters if c.speaker_id == self._last_id), None)
        if last is None:
            return None
        if route and last.route and last.route != route:
            return None
        if float(np.dot(last.centroid, vector)) < self._sticky_threshold:
            return None
        return last

    def _closest(
        self,
        vector: np.ndarray,
        *,
        route: str | None,
    ) -> tuple[_Cluster, float] | None:
        best: _Cluster | None = None
        best_sim = -2.0
        for cluster in self._clusters:
            if route is not None and cluster.route and route and cluster.route != route:
                continue
            similarity = float(np.dot(cluster.centroid, vector))
            if similarity > best_sim:
                best = cluster
                best_sim = similarity
        if best is None:
            return None
        return best, best_sim

    def _absorb(self, cluster: _Cluster, vector: np.ndarray, route: str) -> None:
        mixed = cluster.centroid * cluster.count + vector
        norm = float(np.linalg.norm(mixed))
        cluster.centroid = mixed / norm if norm >= 1e-6 else vector
        cluster.count += 1
        if route and not cluster.route:
            cluster.route = route
        self._last_id = cluster.speaker_id


def recluster(
    history: list[tuple[int, np.ndarray]],
    *,
    threshold: float,
    max_speakers: int = MAX_SPEAKERS,
    protected_ids: set[int] | None = None,
) -> dict[int, int]:
    """Post-meeting pass over the kept embeddings: agglomerative merge of
    speaker centroids whose cosine similarity exceeds ``threshold``.

    Online assignment is greedy — the same person can be split in two when
    the first few segments were noisy. Returns ``{old_id: new_id}`` with ids
    renumbered 1..n in order of first appearance; identity mapping when
    nothing merges. With protected_ids, distinct manual identities never
    merge and existing numbers remain stable, including reserved manual ids.
    """
    protected = set(protected_ids or ())
    centroids: dict[int, np.ndarray] = {}
    counts: dict[int, int] = {}
    order: list[int] = []
    for speaker_id, vector in history:
        vec = np.asarray(vector, dtype=np.float32).reshape(-1)
        if vec.size == 0 or float(np.linalg.norm(vec)) < 1e-6:
            continue
        if speaker_id not in centroids:
            centroids[speaker_id] = vec.copy()
            counts[speaker_id] = 1
            order.append(speaker_id)
        else:
            centroids[speaker_id] = centroids[speaker_id] + vec
            counts[speaker_id] += 1
    if not centroids:
        return {}
    for speaker_id, total in centroids.items():
        norm = float(np.linalg.norm(total))
        centroids[speaker_id] = total / norm if norm >= 1e-6 else total

    # Union-find over pairs above the threshold (closest pairs first).
    parent = {sid: sid for sid in centroids}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    pairs = []
    ids = list(order)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            pairs.append((float(np.dot(centroids[a], centroids[b])), a, b))
    pairs.sort(reverse=True)
    for sim, a, b in pairs:
        if sim < threshold:
            break
        ra, rb = find(a), find(b)
        if ra != rb:
            if ra in protected and rb in protected:
                continue
            # Manual identities keep their number and cannot swallow each other.
            if ra in protected or (rb not in protected and order.index(ra) < order.index(rb)):
                parent[rb] = ra
            else:
                parent[ra] = rb

    roots_in_order: list[int] = []
    for sid in order:
        root = find(sid)
        if root not in roots_in_order:
            roots_in_order.append(root)
    # More roots than allowed: fold the smallest groups into their nearest root.
    while len(roots_in_order) > max_speakers:
        sizes = {r: sum(counts[s] for s in order if find(s) == r) for r in roots_in_order}
        candidates = [r for r in roots_in_order if r not in protected]
        if not candidates:
            break
        smallest = min(candidates, key=lambda r: sizes[r])
        others = [r for r in roots_in_order if r != smallest]
        nearest = max(others, key=lambda r: float(np.dot(centroids[smallest], centroids[r])))
        parent[smallest] = nearest
        roots_in_order.remove(smallest)

    renumber = {root: root if protected else index + 1 for index, root in enumerate(roots_in_order)}
    return {sid: renumber[find(sid)] for sid in order}

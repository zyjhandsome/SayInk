"""Session-scoped speaker labels for one continuous transcription.

Numbers restart every session. At most eight people are kept; a further
voice is merged into the closest existing one. Nothing is stored on disk.
"""

from __future__ import annotations

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
    """Match each segment to a speaker number that lives only for this session."""

    def __init__(self) -> None:
        self._clusters: list[_Cluster] = []
        self._last_id = 0

    def reset(self) -> None:
        self._clusters.clear()
        self._last_id = 0

    def assign(self, embedding: np.ndarray, route: str = "") -> int:
        route = route if route in ("mic", "system") else ""
        vector = np.asarray(embedding, dtype=np.float32).reshape(-1)
        if vector.size == 0 or float(np.linalg.norm(vector)) < 1e-6:
            if self._last_id:
                return self._last_id
            return 1

        match = self._closest(vector, route=route)
        if match is not None and match[1] >= MATCH_THRESHOLD:
            self._absorb(match[0], vector, route)
            return match[0].speaker_id

        if len(self._clusters) >= MAX_SPEAKERS:
            fallback = self._closest(vector, route=None)
            if fallback is None:
                return self._last_id or 1
            self._absorb(fallback[0], vector, route)
            return fallback[0].speaker_id

        speaker_id = len(self._clusters) + 1
        self._clusters.append(_Cluster(speaker_id, route, vector.copy()))
        self._last_id = speaker_id
        return speaker_id

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

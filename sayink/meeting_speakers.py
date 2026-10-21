"""Conservative, session-only speaker matching for meetings and continuous dictation.

Unknown (0) is intentional: a short or ambiguous observation must not teach
the matcher a wrong voice. No audio or voice profiles are written to disk.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

import numpy as np

from sayink.audio_utils import TARGET_SAMPLE_RATE
from sayink.speaker_clustering import TurnObservation
from sayink.speaker_session import SpeakerSession

MAX_PROFILE_SAMPLES = 12
MAX_RECENT_OBSERVATIONS = 512
MATCH_MARGIN = 0.08
CROSS_ROUTE_MIN_SIMILARITY = 0.80


def voice_sample_is_clear(audio: np.ndarray) -> bool:
    """Gate profile learning by duration, finite samples, level and clipping.

    This does not detect overlapping speech or prove that a clip is one person.
    At least one second of non-quiet frames is needed to update a profile.
    """
    samples = np.asarray(audio, dtype=np.float32).reshape(-1)
    frame = TARGET_SAMPLE_RATE // 50
    if samples.size < TARGET_SAMPLE_RATE or not np.all(np.isfinite(samples)):
        return False
    if np.mean(np.abs(samples) >= 0.99) > 0.02:
        return False
    frames = samples[:samples.size // frame * frame].reshape(-1, frame)
    levels = np.sqrt(np.mean(frames * frames, axis=1))
    return int(np.count_nonzero(levels >= 0.005)) >= 50


def _unit(vector: np.ndarray) -> np.ndarray | None:
    vec = np.asarray(vector, dtype=np.float32).reshape(-1)
    if not vec.size or not np.all(np.isfinite(vec)):
        return None
    norm = float(np.linalg.norm(vec))
    return vec / norm if np.isfinite(norm) and norm >= 1e-6 else None


@dataclass
class _Observation:
    speaker_id: int
    vector: np.ndarray | None
    route: str
    learnable: bool


class _Profile:
    def __init__(self, speaker_id: int, route: str):
        self.speaker_id = speaker_id
        self.route = route
        self.samples: OrderedDict[int, np.ndarray] = OrderedDict()
        self.confirmed: set[int] = set()

    def score(self, vector: np.ndarray) -> float:
        # Retain different clear voice states instead of averaging all of them
        # into a centroid which may resemble none of the original states.
        return max((float(sample @ vector) for sample in self.samples.values()), default=-1.0)

    def confirmed_score(self, vector: np.ndarray) -> float:
        return max((float(self.samples[key] @ vector) for key in self.confirmed), default=-1.0)

    def add(self, key: int, vector: np.ndarray, *, confirmed: bool = False) -> None:
        self.samples[key] = vector.copy()
        if confirmed:
            self.confirmed.add(key)
        if len(self.samples) <= MAX_PROFILE_SAMPLES:
            return
        # Preserve the first anchor and diverse voice states. Evict the most
        # redundant older sample; the newest confirmed observation stays.
        keys = list(self.samples)
        removable = [k for k in keys[1:-1] if k not in self.confirmed] or keys[1:-1]
        redundant = max(removable, key=lambda k: max(
            float(self.samples[k] @ self.samples[other]) for other in keys if other != k
        ))
        del self.samples[redundant]
        self.confirmed.discard(redundant)


class MeetingSpeakerSession(SpeakerSession):
    """Match reliable evidence; keep ambiguous and very short turns unknown.

    Meetings and continuous dictation share this policy: bounded multiple
    samples per speaker, with no learning from continuity guesses.
    Manual corrections can relabel recent clear observations by their real
    transcript sequence, including sparse sequences and repeated corrections.
    """

    def __init__(self, *, margin: float = MATCH_MARGIN, cross_route_matching: bool = True, **kwargs):
        super().__init__(**kwargs)
        from sayink.speaker_turns import SpeakerTurnContext

        self.turn_context = SpeakerTurnContext()
        self._margin = max(0.0, float(margin))
        self._cross_route_matching = bool(cross_route_matching)
        self._profiles: dict[int, _Profile] = {}
        self._observations: OrderedDict[int, _Observation] = OrderedDict()
        self._turns: list[TurnObservation] = []
        self._serial = 0
        self._dimension = 0

    @classmethod
    def for_embedder(cls, embedder, *, keep_history: bool = True) -> "MeetingSpeakerSession":
        """The session the app runs for ``embedder``: its thresholds, the
        shared continuity gap and cross-route matching only for CAM++.

        Meetings, continuous dictation and every benchmark tool build their
        session here, so a tool cannot quietly score a different policy than
        the one users get.
        """
        from sayink.speaker_embedding import STICKY_GAP_MS

        return cls(
            threshold=embedder.threshold,
            keep_history=keep_history,
            cross_route_matching=embedder.name == "campplus",
            sticky_threshold=embedder.sticky_threshold,
            sticky_gap_ms=STICKY_GAP_MS,
        )

    @property
    def speaker_count(self) -> int:
        with self._lock:
            return len(self._profiles)

    @property
    def history(self) -> list[tuple[int, np.ndarray]]:
        with self._lock:
            return [(p.speaker_id, vector.copy()) for p in self._profiles.values()
                    for vector in p.samples.values()] if self._keep_history else []

    @property
    def turns(self) -> list[TurnObservation]:
        """Every assigned turn with its key, current number, vector and duration.

        Unlike ``history`` this is not limited to profile samples: unknown and
        non-learnable turns are included so the end-of-session pass can place
        them. Empty unless ``keep_history`` is on.
        """
        with self._lock:
            return list(self._turns)

    def reset(self) -> None:
        with self._lock:
            super().reset()
            self.turn_context.reset()
            self._profiles.clear()
            self._observations.clear()
            self._turns.clear()
            self._serial = 0
            self._dimension = 0

    def assign(self, embedding: np.ndarray, route: str = "", *, start_ms: int = 0,
               end_ms: int = 0, sample_key: int | None = None, learnable: bool = True) -> int:
        with self._lock:
            vector = _unit(embedding)
            if vector is not None and self._dimension and vector.size != self._dimension:
                vector = None  # Never compare a fallback feature with a model embedding.
            if vector is not None and not self._dimension:
                self._dimension = vector.size
            route = route if route in ("mic", "system") else ""
            self._serial += 1
            key = int(sample_key) if sample_key is not None else -self._serial
            speaker_id, train = self._choose(vector, route, start_ms, learnable)
            if train and vector is not None:
                profile = self._profiles.setdefault(speaker_id, _Profile(speaker_id, route))
                profile.add(key, vector)
            self._observations[key] = _Observation(speaker_id, vector, route, bool(learnable))
            while len(self._observations) > MAX_RECENT_OBSERVATIONS:
                self._observations.popitem(last=False)
            if self._keep_history:
                duration = int(end_ms - start_ms) if end_ms and end_ms >= start_ms >= 0 else 0
                self._turns.append(TurnObservation(key, speaker_id, None if vector is None else vector.copy(),
                                                   duration, bool(learnable)))
            # Uncertain turns break the continuity chain.
            self._last_id = speaker_id
            self._last_end_ms = int(end_ms or start_ms or 0)
            return speaker_id

    def _choose(self, vector, route, start_ms, learnable) -> tuple[int, bool]:
        if vector is None:
            return 0, False
        # A capture lane is evidence about the recording, not an identity.
        # Consider all profiles so switching inputs cannot force an identical
        # voice into a new group, and conflicting lanes still compete.
        ranked = sorted(((p.score(vector), p) for p in self._profiles.values()
                         if self._cross_route_matching or not self._cross_route(p, route)),
                        key=lambda item: item[0], reverse=True)
        best, profile = ranked[0] if ranked else (-1.0, None)
        confirmed = sorted(((p.confirmed_score(vector), p) for _score, p in ranked),
                           key=lambda item: item[0], reverse=True)
        if confirmed:
            confirmed_best, confirmed_profile = confirmed[0]
            confirmed_next = confirmed[1][0] if len(confirmed) > 1 else -1.0
            required = max(self._threshold + self._margin,
                           self._route_threshold(confirmed_profile, route))
            if (confirmed_best >= required
                    and confirmed_best - confirmed_next >= self._margin
                    and confirmed_best >= best - self._margin / 2):
                # Clear manual evidence takes precedence over a duplicate
                # automatic group. Conflicting manual samples still abstain.
                return confirmed_profile.speaker_id, bool(learnable and not self._cross_route(confirmed_profile, route))
        runner_up = ranked[1][0] if len(ranked) > 1 else -1.0
        margin = best - runner_up
        required = max(self._threshold + (0.0 if learnable else self._margin),
                       self._route_threshold(profile, route))
        if profile is not None and best >= required:
            if margin < self._margin:
                return 0, False
            # Cross-lane recognition alone does not teach a new recording
            # condition. A saved manual correction can explicitly link it.
            return profile.speaker_id, bool(learnable and not self._cross_route(profile, route)
                                            and best >= self._threshold + self._margin / 2)
        if not learnable:
            return 0, False
        uncertain_floor = self._sticky_threshold if self._sticky_threshold is not None else self._threshold - self._margin
        if self._cross_route(profile, route) and best >= uncertain_floor:
            return 0, False
        if runner_up >= uncertain_floor and margin < self._margin:
            return 0, False
        # Here ``best`` is below the threshold: a same-route match above it
        # returned earlier, and a cross-route one was caught by the floor.
        if (profile is not None and profile.speaker_id == self._last_id
                and not self._cross_route(profile, route)
                and self._sticky_threshold is not None and start_ms and self._last_end_ms
                and 0 <= start_ms - self._last_end_ms <= self._sticky_gap_ms
                and best >= self._sticky_threshold and margin >= self._margin
                and len(profile.samples) >= 2):
            return profile.speaker_id, False
        if len(self._profiles) >= self._max_speakers:
            return 0, False
        return self.reserve_speaker_id(), True

    @staticmethod
    def _cross_route(profile, route) -> bool:
        return bool(profile is not None and route and profile.route and route != profile.route)

    def _route_threshold(self, profile, route) -> float:
        if self._cross_route(profile, route):
            return max(CROSS_ROUTE_MIN_SIMILARITY, self._threshold + 2 * self._margin)
        return self._threshold

    def confirm(self, sample_key: int, speaker_id: int) -> bool:
        """Teach a successfully saved manual correction when evidence permits.

        Rebuild the original profile without this sample, so an incorrect
        automatic assignment does not keep influencing subsequent turns.
        """
        with self._lock:
            observation = self._observations.get(int(sample_key))
            if speaker_id <= 0 or observation is None:
                return False
            for sid, profile in list(self._profiles.items()):
                profile.samples.pop(int(sample_key), None)
                profile.confirmed.discard(int(sample_key))
                if not profile.samples:
                    del self._profiles[sid]
            observation.speaker_id = speaker_id
            self._turns = [TurnObservation(t.key, speaker_id, t.vector, t.duration_ms, t.learnable) if t.key == int(sample_key) else t
                           for t in self._turns]
            if next(reversed(self._observations)) == int(sample_key):
                self._last_id = speaker_id
            if not observation.learnable or observation.vector is None:
                return False
            profile = self._profiles.setdefault(speaker_id, _Profile(speaker_id, observation.route))
            if profile.route and observation.route and profile.route != observation.route:
                profile.route = ""  # An explicit correction can link capture lanes.
            profile.add(int(sample_key), observation.vector, confirmed=True)
            self._next_id = max(self._next_id, speaker_id + 1)
            return True

"""Bounded, experimental speaker-change analysis inside a captured utterance.

This uses real speaker embeddings, never the spectral fallback. It finds
sustained changes. An optional neural guard rejects detected overlapping or
unresolved multiple voices; it does not separate speech or align words.
All sample boundaries partition the original audio without loss or overlap.
Adjacent chunks may use two seconds of session-local analysis context;
previous audio is never included in the current ASR slices.
Short continuations require matching old-voice evidence and a distinct,
stable following voice. Recovered pieces cannot seed or update profiles.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import threading

import numpy as np

from sayink.audio_utils import MAX_SEGMENT_SEC, TARGET_SAMPLE_RATE
from sayink.meeting_speakers import voice_sample_is_clear

WINDOW_SEC = 1.2
HOP_SEC = 0.4
MIN_TURN_SEC = 0.8
# Segments longer than this are not analysed; the segmenter never produces
# them because both numbers come from audio_utils.MAX_SEGMENT_SEC.
MAX_ANALYSIS_SEC = MAX_SEGMENT_SEC
MAX_TURNS = 12
CHANGE_SIMILARITY = 0.35
STABLE_SIMILARITY = 0.65
VERIFY_CONTEXT_SEC = 3.0
MIN_VERIFY_SEC = 1.6
MIN_ACTIVITY_VERIFY_SEC = 0.5
CONTEXT_SEC = 2.0
CONTEXT_HEAD_SEC = 6.0
CONTEXT_GAP_MS = 50
SHORT_CONTEXT_STEP_SEC = 0.1
log = logging.getLogger("SayInk")


@dataclass(frozen=True)
class SpeakerTurn:
    start: int
    stop: int
    embedding: np.ndarray
    learnable: bool


@dataclass(frozen=True)
class RecognizedTurn:
    start: int
    stop: int
    text: str
    embedding: np.ndarray
    learnable: bool


def analyze_turns(audio: np.ndarray, analyzer, *, cancelled=None) -> list[SpeakerTurn]:
    """Shared app/benchmark guard: preserve the entire audio on analysis failure.

    Applied once, at the boundary that owns the audio (``TranscribeWorker``
    and the bench tools). The detector and its context call ``_analyze``
    directly, so a failure anywhere in the context path surfaces here as one
    whole, unlearnable turn instead of being validated twice.
    """
    try:
        turns = analyzer(audio, cancelled=cancelled)
        if cancelled is not None and cancelled():
            return []
        valid = isinstance(turns, list) and 0 < len(turns) <= MAX_TURNS
        valid = valid and all(isinstance(t, SpeakerTurn) and isinstance(t.start, (int, np.integer))
                              and isinstance(t.stop, (int, np.integer)) for t in turns)
        valid = valid and turns[0].start == 0 and turns[-1].stop == len(audio)
        valid = valid and all(0 <= t.start < t.stop <= len(audio) for t in turns)
        valid = valid and all(a.stop == b.start for a, b in zip(turns, turns[1:], strict=False))
        if valid:
            return turns
        log.warning("换人分段未完整覆盖原音频，退回完整片段")
    except Exception as exc:
        log.warning("换人检测失败，保留完整语音并暂不判断说话人: %s", exc)
    return [SpeakerTurn(0, len(audio), np.zeros(1, dtype=np.float32), False)]


def _vector(value) -> np.ndarray | None:
    vec = np.asarray(value, dtype=np.float32).reshape(-1)
    if not vec.size or not np.all(np.isfinite(vec)):
        return None
    norm = float(np.linalg.norm(vec))
    return vec / norm if np.isfinite(norm) and norm > 1e-6 else None


class SpeakerTurnContext:
    """Two seconds of session-only audio, used only for adjacent input chunks."""

    def __init__(self):
        self._lock = threading.Lock()
        self._generation = 0
        self._tail = np.zeros(0, dtype=np.float32)
        self._stop_ms = 0
        self._key = None

    def reset(self):
        with self._lock:
            self._generation += 1
            self._tail = np.zeros(0, dtype=np.float32)
            self._stop_ms = 0
            self._key = None

    def analyze(self, audio, detector, *, start_ms, route, cancelled=None):
        samples = np.asarray(audio, dtype=np.float32).reshape(-1)
        if samples.size == 0 or not np.all(np.isfinite(samples)):
            raise ValueError("invalid audio for speaker-change analysis")
        key = (detector._embed, route)
        with self._lock:
            generation = self._generation
            tail = self._tail if (start_ms > 0 and self._key is not None
                                  and self._key[0] is detector._embed and self._key[1] == route
                                  and abs(start_ms - self._stop_ms) <= CONTEXT_GAP_MS) else None
        turns = detector._analyze(samples, cancelled=cancelled)
        if cancelled is not None and cancelled():
            return []
        if tail is not None and len(tail) >= int(MIN_VERIFY_SEC * TARGET_SAMPLE_RATE):
            turns = detector._leading_context(samples, turns, tail, cancelled=cancelled)
        if cancelled is not None and cancelled():
            return []
        with self._lock:
            stop_ms = int(start_ms + len(samples) * 1000 / TARGET_SAMPLE_RATE)
            if generation == self._generation and stop_ms >= self._stop_ms:
                self._tail = samples[-int(CONTEXT_SEC * TARGET_SAMPLE_RATE):].copy()
                self._stop_ms, self._key = stop_ms, key
        return turns


class SpeakerTurnDetector:
    """Require sustained, clearly separated voice states before inserting a cut.

    Short interjections may remain unresolved. Detected mixed or inconsistent evidence
    which cannot support a stable cut stays unknown and does not train a
    speaker profile. Thresholds require calibration on real meeting recordings.
    """

    def __init__(self, embedder, *, context: SpeakerTurnContext | None = None, start_ms=0, route=""):
        self._embed = embedder
        self._match = float(embedder.threshold)
        self._activity_guard = getattr(embedder, "activity_guard", None)
        self._context, self._start_ms, self._route = context, int(start_ms or 0), route
        self.context_action = "none"

    @property
    def activity_backend(self) -> str:
        return self._activity_guard.name if self._activity_guard is not None else "off"

    def _embed_or_none(self, audio):
        try:
            return _vector(self._embed(audio))
        except Exception:
            return None

    def _activity_unsafe(self, audio, *, cancelled=None):
        if self._activity_guard is None:
            return False
        try:
            return self._activity_guard.is_unsafe(audio, cancelled=cancelled)
        except Exception as exc:
            # A failed configured check must not silently approve training.
            log.warning("多人发言检测失败，本段保留文字识别并暂不判断说话人: %s", exc)
            return True

    def _plain_single(self, audio, *, uncertain=False, mixed=False):
        """One turn over the whole clip.

        ``uncertain``: the analysis could not establish a stable single voice,
        so the clip must not teach a profile; its whole-clip embedding is still
        offered so a strong match to an already known voice can label it.
        ``mixed``: several voices were detected, the embedding is meaningless
        and the turn stays unknown.
        """
        vector = None if mixed else self._embed_or_none(audio)
        return [SpeakerTurn(0, len(audio), vector if vector is not None else np.zeros(1, dtype=np.float32),
                            vector is not None and not uncertain and voice_sample_is_clear(audio))]

    def _single(self, audio, *, uncertain=False, cancelled=None, allow_activity_splits=True):
        analyze = getattr(self._activity_guard, "analyze", None)
        if callable(analyze):
            try:
                evidence = analyze(audio, cancelled=cancelled)
                if evidence.unsafe and not uncertain and allow_activity_splits:
                    return self._activity_turns(audio, evidence, cancelled=cancelled)
                return self._plain_single(audio, uncertain=uncertain or evidence.unsafe,
                                          mixed=bool(evidence.overlaps))
            except Exception as exc:
                if cancelled is not None and cancelled():
                    return []
                log.warning("局部发言检测失败，本段说话人待确认: %s", exc)
                return self._plain_single(audio, uncertain=True, mixed=True)
        if self._activity_guard is not None and self._activity_unsafe(audio, cancelled=cancelled):
            return self._plain_single(audio, uncertain=True, mixed=True)
        return self._plain_single(audio, uncertain=uncertain)

    def _activity_turns(self, audio, evidence, *, cancelled=None):
        if evidence.unresolved:
            return self._plain_single(audio, uncertain=True)
        minimum = int(MIN_ACTIVITY_VERIFY_SEC * TARGET_SAMPLE_RATE)
        proposed = [0, *evidence.cuts, len(audio)]
        for index, cut in enumerate(evidence.cuts, 1):
            if cancelled is not None and cancelled():
                return []
            similarity = self._change_similarity(audio, cut, proposed[index - 1], proposed[index + 1],
                                                 minimum_sec=MIN_ACTIVITY_VERIFY_SEC)
            if similarity is None or similarity > CHANGE_SIMILARITY:
                # Local model labels alone cannot create a new speaker turn.
                return self._plain_single(audio, uncertain=True)
        bounds = sorted({0, len(audio), *evidence.cuts,
                         *(at for interval in evidence.overlaps for at in interval)})
        # Do not create tiny ASR slices around uncertain edges. Removing a
        # boundary expands the unknown region instead of deleting any audio.
        while len(bounds) > 2:
            tiny = next((i for i, (a, b) in enumerate(zip(bounds, bounds[1:], strict=False))
                         if b - a < minimum), None)
            if tiny is None:
                break
            del bounds[1 if tiny == 0 else tiny]
        if len(bounds) - 1 > MAX_TURNS:
            return self._plain_single(audio, uncertain=True)
        turns = []
        for start, stop in zip(bounds, bounds[1:], strict=False):
            if cancelled is not None and cancelled():
                return []
            unsafe = any(start < end and stop > begin for begin, end in evidence.overlaps)
            turn = self._plain_single(audio[start:stop], uncertain=unsafe, mixed=unsafe)[0]
            # A clear flank beside suspected overlap may match an established
            # voice, but must not seed or change a profile from this evidence.
            turns.append(SpeakerTurn(start, stop, turn.embedding, turn.learnable and not evidence.overlaps))
        return turns

    def __call__(self, audio: np.ndarray, *, cancelled=None) -> list[SpeakerTurn]:
        if self._context is not None:
            return self._context.analyze(audio, self, start_ms=self._start_ms, route=self._route, cancelled=cancelled)
        return self._analyze(audio, cancelled=cancelled)

    def _leading_context(self, samples, turns, prefix, *, cancelled=None):
        if not turns or len(turns) >= MAX_TURNS or len(samples) > int(MAX_ANALYSIS_SEC * TARGET_SAMPLE_RATE):
            return turns
        first = turns[0]
        minimum = int(MIN_ACTIVITY_VERIFY_SEC * TARGET_SAMPLE_RATE)
        head = min(len(samples), int(CONTEXT_HEAD_SEC * TARGET_SAMPLE_RATE), first.stop + minimum)
        joined = np.concatenate((prefix, samples[:head]))
        evidence = analyze_turns(joined, self._analyze, cancelled=cancelled)
        for left, right in zip(evidence, evidence[1:], strict=False):
            cut = left.stop - len(prefix)
            if not (left.start < len(prefix) < left.stop and 0 < cut < first.stop - minimum):
                continue
            a, b = _vector(left.embedding), _vector(right.embedding)
            if (not left.learnable or not right.learnable or a is None or b is None
                    or a.size != b.size or float(a @ b) > CHANGE_SIMILARITY):
                continue
            # A tiny suffix cannot be decoded reliably by itself. Preserve
            # its words in the original ASR slice and abstain for this mixed
            # leading turn, without contaminating either speaker profile.
            if cut < minimum:
                if len(turns) < 2 or first.stop > int(CONTEXT_HEAD_SEC * TARGET_SAMPLE_RATE):
                    return turns  # A tiny suffix must not reject a whole long utterance.
                self.context_action = "abstain"
                return [SpeakerTurn(0, first.stop, np.zeros(1, dtype=np.float32), False), *turns[1:]]
            pieces = []
            for start, stop in ((0, cut), (cut, first.stop)):
                if cancelled is not None and cancelled():
                    return []
                pieces.append(self._single(samples[start:stop], cancelled=cancelled))
            if cancelled is not None and cancelled():
                return []
            # Context proposes only one leading boundary. Extra unstable
            # boundaries must not recursively fragment the current slice.
            if any(len(part) != 1 for part in pieces):
                return turns
            self.context_action = "split"
            return [SpeakerTurn(0, cut, pieces[0][0].embedding, False),
                    SpeakerTurn(cut, first.stop, pieces[1][0].embedding, False), *turns[1:]]
        return self._short_leading_context(samples, turns, prefix, cancelled=cancelled)

    def _short_leading_context(self, samples, turns, prefix, *, cancelled=None):
        """Recover a short continuation only with an old voice and a stable new voice.

        This bounded fallback cannot create identities or teach profiles. It
        requires a strong match to the adjacent old voice, a distinct longer
        following voice, and more than one supported candidate boundary.
        """
        first = turns[0]
        rate = TARGET_SAMPLE_RATE
        minimum = int(MIN_VERIFY_SEC * rate)
        probe_size = int(MIN_TURN_SEC * rate)
        if (not first.learnable or first.stop < 2 * minimum
                or first.stop > int(CONTEXT_HEAD_SEC * rate)
                or not voice_sample_is_clear(prefix)):
            return turns

        def vector(clip):
            if cancelled is not None and cancelled():
                return None
            return self._embed_or_none(clip)

        old, probe = vector(prefix), vector(samples[:probe_size])
        if old is None or probe is None or old.size != probe.size or float(old @ probe) < STABLE_SIMILARITY:
            return turns
        following = vector(samples[minimum:first.stop])
        if (following is None or following.size != old.size or float(old @ following) > CHANGE_SIMILARITY
                or float(probe @ following) > CHANGE_SIMILARITY):
            return turns
        tail = vector(samples[first.stop - minimum:first.stop])
        if tail is None or tail.size != old.size or float(tail @ following) < STABLE_SIMILARITY:
            return turns
        if self._activity_unsafe(prefix, cancelled=cancelled):
            return turns
        step = int(SHORT_CONTEXT_STEP_SEC * rate)
        candidates = []
        previous = None
        # Leave at least 1.6 seconds to verify the following voice. Avoid
        # model-minimum 500 ms slices; these candidates start at 600 ms.
        for cut in range(int((MIN_ACTIVITY_VERIFY_SEC + SHORT_CONTEXT_STEP_SEC) * rate), minimum + 1, step):
            left = probe if cut == probe_size else vector(samples[:cut])
            if left is None or left.size != old.size or float(left @ old) < STABLE_SIMILARITY:
                previous = None
                continue
            right = vector(samples[cut:first.stop])
            if (right is None or right.size != old.size or float(left @ right) > CHANGE_SIMILARITY
                    or float(right @ old) > CHANGE_SIMILARITY or float(right @ following) < STABLE_SIMILARITY):
                previous = None
                continue
            candidate = (float(left @ old) - float(left @ right), cut, left, right)
            if (previous is not None and float(previous[2] @ left) >= STABLE_SIMILARITY
                    and float(previous[3] @ right) >= STABLE_SIMILARITY):
                candidates.extend((previous, candidate))
            previous = candidate
        if cancelled is not None and cancelled():
            return []
        if not candidates:
            return turns
        _, cut, _, _ = max(candidates, key=lambda item: (item[0], -item[1]))
        pieces = []
        for start, stop in ((0, cut), (cut, first.stop)):
            if cancelled is not None and cancelled():
                return []
            pieces.append(self._single(samples[start:stop], cancelled=cancelled))
        if cancelled is not None and cancelled():
            return []
        if any(len(part) != 1 for part in pieces):
            self.context_action = "short_abstain"
            return [SpeakerTurn(0, first.stop, np.zeros(1, dtype=np.float32), False), *turns[1:]]
        left, right = (_vector(part[0].embedding) for part in pieces)
        if (left is None or right is None or left.size != old.size or right.size != old.size
                or float(left @ old) < STABLE_SIMILARITY or float(left @ right) > CHANGE_SIMILARITY
                or float(right @ following) < STABLE_SIMILARITY):
            self.context_action = "short_abstain"
            return [SpeakerTurn(0, first.stop, np.zeros(1, dtype=np.float32), False), *turns[1:]]
        self.context_action = "short_split"
        return [SpeakerTurn(0, cut, left, False), SpeakerTurn(cut, first.stop, right, False), *turns[1:]]

    def _analyze(self, audio: np.ndarray, *, cancelled=None) -> list[SpeakerTurn]:
        samples = np.asarray(audio, dtype=np.float32).reshape(-1)
        if samples.size == 0 or not np.all(np.isfinite(samples)):
            raise ValueError("invalid audio for speaker-change analysis")
        window = int(WINDOW_SEC * TARGET_SAMPLE_RATE)
        hop = int(HOP_SEC * TARGET_SAMPLE_RATE)
        minimum = int(MIN_TURN_SEC * TARGET_SAMPLE_RATE)
        if samples.size > int(MAX_ANALYSIS_SEC * TARGET_SAMPLE_RATE):
            # Unbounded input gets no model work at all; it stays unknown.
            return self._plain_single(samples, uncertain=True, mixed=True)
        if samples.size < 2 * window + hop:
            return self._single(samples, cancelled=cancelled)
        starts = list(range(0, samples.size - window + 1, hop))
        if starts[-1] + window < samples.size:
            starts.append(samples.size - window)
        vectors = []
        anchors = []
        for start in starts:
            if cancelled is not None and cancelled():
                return []
            piece = samples[start:start + window]
            vector = self._embed_or_none(piece) if voice_sample_is_clear(piece) else None
            if vector is not None and anchors and vector.size != anchors[0].size:
                vector = None
            vectors.append(vector)
            if vector is not None and (not anchors or max(float(a @ vector) for a in anchors) < self._match):
                # Local labels are disposable; they never allocate meeting ids.
                if len(anchors) < MAX_TURNS:
                    anchors.append(vector)
        if not anchors:
            # No 1.2 s window held a full second of clear speech, so nothing
            # proposed a speaker change. Clips under that analysis length
            # already seed a profile when the whole utterance is clear; forcing
            # uncertain here left the opening of a meeting unnamed. A configured
            # activity check can still abstain, but it must not invent splits
            # the windows never supported.
            return self._single(samples, cancelled=cancelled, allow_activity_splits=False)
        # A new voice may first appear in a window straddling the change.
        # Replace that mixed anchor with a repeated, better-separated state.
        # Otherwise the mixed anchor can hide a real A→B boundary.
        refined = []
        for index, anchor in enumerate(anchors):
            candidates = [v for v in vectors if v is not None and float(v @ anchor) >= STABLE_SIMILARITY
                          and index == int(np.argmax([float(v @ a) for a in anchors]))]
            supported = [v for v in candidates
                         if sum(float(v @ other) >= 0.9 for other in candidates) >= 2]
            if len(anchors) > 1 and supported:
                anchor = min(supported, key=lambda v: max(float(v @ a) for j, a in enumerate(anchors) if j != index))
            refined.append(anchor)
        anchors = refined
        labels = []
        for vector in vectors:
            if vector is None:
                labels.append(-1)
                continue
            scores = sorted(((float(a @ vector), i) for i, a in enumerate(anchors)), reverse=True)
            best, label = scores[0]
            runner_up = scores[1][0] if len(scores) > 1 else -1.0
            labels.append(label if best >= STABLE_SIMILARITY and best - runner_up >= 0.08 else -1)
        # Two consecutive, mutually consistent windows are required for each
        # stable run. An isolated different window cannot create a new turn.
        runs = []
        begin = 0
        for end in range(1, len(labels) + 1):
            if end < len(labels) and labels[end] == labels[begin]:
                continue
            if labels[begin] >= 0 and end - begin >= 2:
                run_vectors = [v for v in vectors[begin:end] if v is not None]
                stable = all(float(a @ b) >= STABLE_SIMILARITY
                             for a, b in zip(run_vectors, run_vectors[1:], strict=False))
                if stable:
                    runs.append((labels[begin], begin, end - 1))
            begin = end
        cuts = []
        previous = runs[0] if runs else None
        for run in runs[1:]:
            if cancelled is not None and cancelled():
                return []
            if previous[0] != run[0] and float(anchors[previous[0]] @ anchors[run[0]]) <= CHANGE_SIMILARITY:
                center = (starts[previous[2]] + starts[run[1]] + window) // 2
                cut = self._quiet_cut(samples, center, (cuts[-1] if cuts else 0) + minimum,
                                      samples.size - minimum)
                if cut is not None:
                    cuts.append(cut)
            previous = run
        # Short CAM++ windows can disagree merely because of laughter or
        # phonetic content. A proposal must also separate longer voice clips.
        proposed = cuts
        cuts = []
        unresolved = []
        for index, cut in enumerate(proposed):
            if cancelled is not None and cancelled():
                return []
            lower = proposed[index - 1] if index else 0
            upper = proposed[index + 1] if index + 1 < len(proposed) else len(samples)
            confirmed = self._verify_change(samples, cut, lower, upper)
            if confirmed is True:
                cuts.append(cut)
            elif confirmed is None:
                unresolved.append(cut)
        if not cuts:
            return self._single(samples, uncertain=bool(unresolved), cancelled=cancelled)
        bounds = [0, *cuts[:MAX_TURNS - 1], samples.size]
        turns = []
        for start, stop in zip(bounds, bounds[1:], strict=False):
            if cancelled is not None and cancelled():
                return []
            piece = samples[start:stop]
            local = self._single(piece, uncertain=any(start < boundary < stop for boundary in unresolved),
                                 cancelled=cancelled)
            turns.extend(SpeakerTurn(start + t.start, start + t.stop, t.embedding, t.learnable) for t in local)
        if len(turns) > MAX_TURNS:
            return self._plain_single(samples, uncertain=True)
        return turns

    def _verify_change(self, samples, cut, lower, upper):
        similarity = self._change_similarity(samples, cut, lower, upper)
        return None if similarity is None else similarity <= CHANGE_SIMILARITY

    def _change_similarity(self, samples, cut, lower, upper, *, minimum_sec=MIN_VERIFY_SEC):
        context = int(VERIFY_CONTEXT_SEC * TARGET_SAMPLE_RATE)
        left_start, right_stop = max(lower, cut - context), min(upper, cut + context)
        minimum = int(minimum_sec * TARGET_SAMPLE_RATE)
        if min(cut - left_start, right_stop - cut) < minimum:
            return None
        left = self._embed_or_none(samples[left_start:cut])
        right = self._embed_or_none(samples[cut:right_stop])
        if left is None or right is None or left.size != right.size:
            return None
        return float(left @ right)

    @staticmethod
    def _quiet_cut(samples, center, lower, upper):
        if lower > upper:
            return None
        center = min(upper, max(lower, center))
        step = int(0.02 * TARGET_SAMPLE_RATE)
        radius = int(0.12 * TARGET_SAMPLE_RATE)
        options = [at for at in range(center - radius, center + radius + 1, step) if lower <= at <= upper]
        if not options:
            return center
        # Prefer the center on flat energy, rather than drifting to an edge.
        return min(options, key=lambda at: (float(np.mean(samples[max(0, at - step // 2):at + step // 2] ** 2)),
                                            abs(at - center)))

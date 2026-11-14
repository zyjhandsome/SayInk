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
import math
import threading
import time

import numpy as np

from sayink.audio_utils import MAX_SEGMENT_SEC, TARGET_SAMPLE_RATE
from sayink.meeting_speakers import voice_sample_is_clear
from sayink.speaker_session import normalize_far_field_voice
from sayink.vad_segmenter import attach_segment_acoustics


def _clear_voice(audio: np.ndarray) -> bool:
    return voice_sample_is_clear(normalize_far_field_voice(audio))

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
# Require strong within-speaker agreement (0.80) and non-trivial window share
# so stray laughter/phonetic windows inside a monologue do not trigger a false
# multi-voice conflict, while genuine alternating voices still abstain.
REPEATED_WINDOW_SIMILARITY = 0.80
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
    except InterruptedError:
        return []
    except Exception as exc:
        if cancelled is not None and cancelled():
            return []
        log.warning("换人检测失败，保留完整语音并暂不判断说话人: %s", exc)
    return [SpeakerTurn(0, len(audio), np.zeros(1, dtype=np.float32), False)]


def _vector(value) -> np.ndarray | None:
    vec = np.asarray(value, dtype=np.float32).reshape(-1)
    if not vec.size or not np.all(np.isfinite(vec)):
        return None
    norm = float(np.linalg.norm(vec))
    return vec / norm if np.isfinite(norm) and norm > 1e-6 else None


def _conflicting_repeated_windows(vectors) -> bool:
    """Detect two recurring, distinct voices without inventing a cut point.

    Windows already passed the clear-speech gate. Each candidate needs a
    second consistent window and must differ strongly from both windows of
    the other candidate. This only signals uncertainty, not identities.
    """
    valid = [v for v in vectors if v is not None]
    if len(valid) < 4:
        return False
    min_share = max(2, math.ceil(len(valid) * 0.2))
    stacked = np.stack(valid)
    similarities = stacked @ stacked.T
    count = len(valid)
    for left in range(count):
        for right in range(left + 1, count):
            if similarities[left, right] > CHANGE_SIMILARITY:
                continue
            left_support = [i for i in range(count) if i not in (left, right)
                            and similarities[left, i] >= REPEATED_WINDOW_SIMILARITY
                            and similarities[right, i] <= CHANGE_SIMILARITY]
            right_support = [i for i in range(count) if i not in (left, right)
                             and similarities[right, i] >= REPEATED_WINDOW_SIMILARITY
                             and similarities[left, i] <= CHANGE_SIMILARITY]
            if (1 + len(left_support) >= min_share
                    and 1 + len(right_support) >= min_share
                    and any(a != b for a in left_support for b in right_support)):
                return True
    return False


class SpeakerTurnContext:
    """Two seconds of session-only audio and lightweight voice prototypes."""

    def __init__(self):
        self._lock = threading.Lock()
        self._generation = 0
        self._tail = np.zeros(0, dtype=np.float32)
        self._stop_ms = 0
        self._key = None
        self._voices_embed = None
        self._voices: list[np.ndarray] = []

    def reset(self):
        with self._lock:
            self._generation += 1
            self._tail = np.zeros(0, dtype=np.float32)
            self._stop_ms = 0
            self._key = None
            self._voices_embed = None
            self._voices = []

    def session_voice_conflict(self, embedder, vector: np.ndarray | None) -> bool:
        """True when ``vector`` sits in a low-margin or borderline zone vs session voices."""
        if vector is None:
            return False
        with self._lock:
            if self._voices_embed is not embedder or not self._voices:
                return False
            voices = [v for v in self._voices if v.size == vector.size]
        if not voices:
            return False
        scores = sorted((float(v @ vector) for v in voices), reverse=True)
        best = scores[0]
        runner_up = scores[1] if len(scores) > 1 else -1.0
        match = float(getattr(embedder, "threshold", 0.55))
        sticky = float(getattr(embedder, "sticky_threshold", match - 0.10))
        margin = 0.08
        if len(scores) >= 2 and best >= sticky and best - runner_up < margin * 1.5:
            return True
        return (sticky - 0.05) <= best < (match + margin / 2)

    def _record_voices_locked(self, embedder, turns: list[SpeakerTurn]) -> None:
        if self._voices_embed is not embedder:
            self._voices_embed = embedder
            self._voices = []
        match = float(getattr(embedder, "threshold", 0.55))
        for turn in turns:
            if not turn.learnable:
                continue
            vec = _vector(turn.embedding)
            if vec is None:
                continue
            same_dim = [(idx, float(proto @ vec)) for idx, proto in enumerate(self._voices) if proto.size == vec.size]
            if same_dim:
                best_idx, best_sim = max(same_dim, key=lambda item: item[1])
                if best_sim >= match:
                    updated = _vector(0.75 * self._voices[best_idx] + 0.25 * vec)
                    if updated is not None:
                        self._voices[best_idx] = updated
                    continue
            if len(self._voices) < MAX_TURNS:
                self._voices.append(vec)

    def analyze(self, audio, detector, *, start_ms, route, cancelled=None):
        raw_floor = getattr(audio, "noise_floor", None)
        samples = np.asarray(audio, dtype=np.float32).reshape(-1)
        if samples.size == 0 or not np.all(np.isfinite(samples)):
            raise ValueError("invalid audio for speaker-change analysis")
        if raw_floor is not None:
            samples = attach_segment_acoustics(samples, noise_floor=raw_floor)
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
            if generation == self._generation:
                self._record_voices_locked(detector._embed, turns)
                if stop_ms >= self._stop_ms:
                    self._tail = samples[-int(CONTEXT_SEC * TARGET_SAMPLE_RATE):].copy()
                    self._stop_ms, self._key = stop_ms, key
        return turns


class SpeakerTurnDetector:
    """Require sustained, clearly separated voice states before inserting a cut.

    Short interjections may remain unresolved. By default, ``activity_guard``
    runs on demand as a voiceprint guardrail only when primary CAM++ evidence
    shows boundary ambiguity, conflict, or low session margin: unsafe clips
    are marked ``learnable=False`` without vetoing primary CAM++ attribution.
    Pass ``activity_mode="strict"`` to run full legacy overlap checks/splits.
    """

    def __init__(
        self,
        embedder,
        *,
        context: SpeakerTurnContext | None = None,
        start_ms=0,
        route="",
        activity_mode: str | None = None,
    ):
        self._embed = embedder
        self._match = float(embedder.threshold)
        mode = str(activity_mode or getattr(embedder, "activity_mode", "on_demand") or "on_demand").strip().lower()
        self._activity_mode = mode
        self._strict_activity = mode in ("strict", "full")
        self._activity_guard = None if mode == "off" else getattr(embedder, "activity_guard", None)
        self._context, self._start_ms, self._route = context, int(start_ms or 0), route
        self.context_action = "none"
        self.activity_checks = 0
        self._embed_warned_at = 0.0

    @property
    def activity_backend(self) -> str:
        return self._activity_guard.name if self._activity_guard is not None else "off"

    def _embed_or_none(self, audio):
        try:
            return _vector(self._embed(audio))
        except InterruptedError:
            raise
        except Exception as exc:
            now = time.monotonic()
            if now - self._embed_warned_at >= 30:
                self._embed_warned_at = now
                log.warning("说话人声纹计算失败，本段暂不判断说话人: %s", exc)
            return None

    def _activity_unsafe(self, audio, *, cancelled=None):
        if self._activity_guard is None:
            return False
        self.activity_checks += 1
        try:
            return self._activity_guard.is_unsafe(audio, cancelled=cancelled)
        except InterruptedError:
            raise
        except Exception as exc:
            if cancelled is not None and cancelled():
                raise InterruptedError("speaker activity check cancelled") from exc
            # A failed configured check must not silently approve training.
            log.warning("多人发言检测失败，本段保留文字识别并阻止声纹入库: %s", exc)
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
                            vector is not None and not uncertain and _clear_voice(audio))]

    def _verified_sequential_turns(self, audio, cuts, *, cancelled=None) -> list[SpeakerTurn] | None:
        minimum = int(MIN_ACTIVITY_VERIFY_SEC * TARGET_SAMPLE_RATE)
        proposed = [0, *cuts, len(audio)]
        if len(proposed) - 1 > MAX_TURNS or any(b - a < minimum for a, b in zip(proposed, proposed[1:], strict=False)):
            return None
        for index, cut in enumerate(cuts, 1):
            if cancelled is not None and cancelled():
                return []
            similarity = self._change_similarity(
                audio, cut, proposed[index - 1], proposed[index + 1], minimum_sec=MIN_ACTIVITY_VERIFY_SEC
            )
            if similarity is None or similarity > CHANGE_SIMILARITY:
                return None
        turns = []
        for start, stop in zip(proposed, proposed[1:], strict=False):
            if cancelled is not None and cancelled():
                return []
            turn = self._plain_single(audio[start:stop], uncertain=False, mixed=False)[0]
            turns.append(SpeakerTurn(start, stop, turn.embedding, bool(turn.learnable)))
        return turns

    def _single(
        self,
        audio,
        *,
        uncertain=False,
        cancelled=None,
        allow_activity_splits=True,
        need_guard=False,
        anchors=None,
    ):
        if not self._strict_activity:
            vector = self._embed_or_none(audio)
            clear = vector is not None and _clear_voice(audio)
            if not clear:
                return [
                    SpeakerTurn(
                        0,
                        len(audio),
                        vector if vector is not None else np.zeros(1, dtype=np.float32),
                        False,
                    )
                ]
            same_dim_anchors = [a for a in (anchors or ()) if a.size == vector.size]
            anchor_mismatch = bool(same_dim_anchors) and max(float(a @ vector) for a in same_dim_anchors) < STABLE_SIMILARITY
            should_guard = (
                need_guard
                or anchor_mismatch
                or (self._context is not None and self._context.session_voice_conflict(self._embed, vector))
            )
            if self._activity_guard is None or not should_guard:
                return [SpeakerTurn(0, len(audio), vector, not uncertain)]
            analyze = getattr(self._activity_guard, "analyze", None)
            if callable(analyze):
                self.activity_checks += 1
                try:
                    evidence = analyze(audio, cancelled=cancelled)
                except InterruptedError:
                    return []
                except Exception as exc:
                    if cancelled is not None and cancelled():
                        return []
                    log.warning("局部发言检测失败，保留主声纹并阻止声纹入库: %s", exc)
                    return [SpeakerTurn(0, len(audio), vector, False)]
                if (
                    allow_activity_splits
                    and not uncertain
                    and evidence.cuts
                    and not evidence.overlaps
                    and not evidence.unresolved
                ):
                    verified = self._verified_sequential_turns(audio, evidence.cuts, cancelled=cancelled)
                    if verified is not None:
                        return verified
                return [SpeakerTurn(0, len(audio), vector, not (uncertain or evidence.unsafe))]
            try:
                unsafe = self._activity_unsafe(audio, cancelled=cancelled)
            except InterruptedError:
                return []
            return [SpeakerTurn(0, len(audio), vector, not (uncertain or unsafe))]

        analyze = getattr(self._activity_guard, "analyze", None)
        if callable(analyze):
            self.activity_checks += 1
            try:
                evidence = analyze(audio, cancelled=cancelled)
                allow_cuts = not uncertain and allow_activity_splits
                if evidence.overlaps or (evidence.unsafe and allow_cuts):
                    return self._activity_turns(audio, evidence, cancelled=cancelled, allow_cuts=allow_cuts)
                return self._plain_single(audio, uncertain=uncertain or evidence.unsafe,
                                          mixed=bool(evidence.overlaps))
            except InterruptedError:
                return []
            except Exception as exc:
                if cancelled is not None and cancelled():
                    return []
                log.warning("局部发言检测失败，本段说话人待确认: %s", exc)
                return self._plain_single(audio, uncertain=True, mixed=True)
        try:
            if self._activity_guard is not None and self._activity_unsafe(audio, cancelled=cancelled):
                return self._plain_single(audio, uncertain=True, mixed=True)
        except InterruptedError:
            return []
        return self._plain_single(audio, uncertain=uncertain)

    def _activity_turns(self, audio, evidence, *, cancelled=None, allow_cuts=True):
        uncertain = evidence.unresolved or not allow_cuts
        if uncertain and not evidence.overlaps:
            return self._plain_single(audio, uncertain=True)
        minimum = int(MIN_ACTIVITY_VERIFY_SEC * TARGET_SAMPLE_RATE)
        proposed = [0, *evidence.cuts, len(audio)]
        cuts = list(evidence.cuts) if not uncertain else []
        for index, cut in enumerate(cuts, 1):
            if cancelled is not None and cancelled():
                return []
            similarity = self._change_similarity(audio, cut, proposed[index - 1], proposed[index + 1],
                                                 minimum_sec=MIN_ACTIVITY_VERIFY_SEC)
            if similarity is None or similarity > CHANGE_SIMILARITY:
                # A rejected sequential boundary must not discard the overlap
                # mask. Keep clear flanks available for strong matches only.
                cuts = []
                uncertain = True
                break
        if uncertain and not evidence.overlaps:
            return self._plain_single(audio, uncertain=True)
        # A long overlap is not a place to slice the sentence. A short one
        # still marks a possible interjection. Either way the overlapped
        # audio keeps its voice vector so a known speaker can match it, and
        # that vector is never allowed to train a profile.
        short_limit = int(1.5 * TARGET_SAMPLE_RATE)
        bounds = sorted({0, len(audio), *cuts,
                         *(at for begin, end in evidence.overlaps
                           if end - begin <= short_limit for at in (begin, end))})
        # Do not create tiny ASR slices around uncertain edges. Removing a
        # boundary expands the unknown region instead of deleting any audio.
        while len(bounds) > 2:
            tiny = next((i for i, (a, b) in enumerate(zip(bounds, bounds[1:], strict=False))
                         if b - a < minimum), None)
            if tiny is None:
                break
            del bounds[1 if tiny == 0 else tiny]
        if len(bounds) - 1 > MAX_TURNS:
            return self._plain_single(audio, uncertain=True, mixed=bool(evidence.overlaps))
        turns = []
        for start, stop in zip(bounds, bounds[1:], strict=False):
            if cancelled is not None and cancelled():
                return []
            unsafe = any(start < end and stop > begin for begin, end in evidence.overlaps)
            turn = self._plain_single(audio[start:stop], uncertain=unsafe or uncertain, mixed=False)[0]
            # A clear flank beside suspected overlap may match an established
            # voice, but must not seed or change a profile from this evidence.
            # Overlapped audio keeps the same rule: match, never learn.
            learnable = bool(turn.learnable) and not unsafe and not evidence.overlaps
            turns.append(SpeakerTurn(start, stop, turn.embedding, learnable))
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
            # its words in the original ASR slice and describe the turn by the
            # voice filling the rest of it: a strong match may label it, but
            # this mixed evidence must not teach either speaker profile.
            if cut < minimum:
                if len(turns) < 2 or first.stop > int(CONTEXT_HEAD_SEC * TARGET_SAMPLE_RATE):
                    return turns  # A tiny suffix must not reject a whole long utterance.
                if cancelled is not None and cancelled():
                    return []
                # The first cut can land inside a short reply. Look for a
                # longer boundary that still matches the previous voice before
                # folding the opening into whoever fills the rest of the slice.
                recovered = self._short_leading_context(samples, turns, prefix, cancelled=cancelled)
                if self.context_action == "short_split":
                    return recovered
                self.context_action = "none"
                if cancelled is not None and cancelled():
                    return []
                dominant = self._embed_or_none(samples[cut:first.stop])
                self.context_action = "dominant"
                vector = dominant if dominant is not None else np.zeros(1, dtype=np.float32)
                return [SpeakerTurn(0, first.stop, vector, False), *turns[1:]]
            pieces = []
            for start, stop in ((0, cut), (cut, first.stop)):
                if cancelled is not None and cancelled():
                    return []
                pieces.append(self._single(samples[start:stop], cancelled=cancelled, need_guard=True))
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
                or not _clear_voice(prefix)):
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
        try:
            if self._activity_unsafe(prefix, cancelled=cancelled):
                return turns
        except InterruptedError:
            return []
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
        fallback_vec = np.zeros(1, dtype=np.float32) if self._strict_activity else first.embedding
        pieces = []
        for start, stop in ((0, cut), (cut, first.stop)):
            if cancelled is not None and cancelled():
                return []
            if not self._strict_activity:
                if self._activity_guard is not None:
                    analyze = getattr(self._activity_guard, "analyze", None)
                    if callable(analyze):
                        self.activity_checks += 1
                        try:
                            unsafe = analyze(samples[start:stop], cancelled=cancelled).unsafe
                        except InterruptedError:
                            return []
                        except Exception:
                            if cancelled is not None and cancelled():
                                return []
                            unsafe = True
                    else:
                        try:
                            unsafe = self._activity_unsafe(samples[start:stop], cancelled=cancelled)
                        except InterruptedError:
                            return []
                    if unsafe:
                        self.context_action = "short_abstain"
                        return [SpeakerTurn(0, first.stop, fallback_vec, False), *turns[1:]]
                pieces.append(self._plain_single(samples[start:stop], uncertain=True, mixed=False))
            else:
                pieces.append(self._single(samples[start:stop], cancelled=cancelled))
        if cancelled is not None and cancelled():
            return []
        if any(len(part) != 1 for part in pieces):
            self.context_action = "short_abstain"
            return [SpeakerTurn(0, first.stop, fallback_vec, False), *turns[1:]]
        left, right = (_vector(part[0].embedding) for part in pieces)
        if (left is None or right is None or left.size != old.size or right.size != old.size
                or float(left @ old) < STABLE_SIMILARITY or float(left @ right) > CHANGE_SIMILARITY
                or float(right @ following) < STABLE_SIMILARITY):
            self.context_action = "short_abstain"
            return [SpeakerTurn(0, first.stop, fallback_vec, False), *turns[1:]]
        self.context_action = "short_split"
        return [SpeakerTurn(0, cut, left, False), SpeakerTurn(cut, first.stop, right, False), *turns[1:]]

    def _analyze(self, audio: np.ndarray, *, cancelled=None) -> list[SpeakerTurn]:
        raw_floor = getattr(audio, "noise_floor", None)
        samples = np.asarray(audio, dtype=np.float32).reshape(-1)
        if samples.size == 0 or not np.all(np.isfinite(samples)):
            raise ValueError("invalid audio for speaker-change analysis")
        if raw_floor is not None:
            samples = attach_segment_acoustics(samples, noise_floor=raw_floor)
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
            vector = self._embed_or_none(piece) if _clear_voice(piece) else None
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
            # activity check can still abstain on demand, without inventing splits.
            return self._single(samples, cancelled=cancelled, allow_activity_splits=False, need_guard=True)
        # A new voice may first appear in a window straddling the change.
        # Replace that mixed anchor with a repeated, better-separated state.
        # Otherwise the mixed anchor can hide a real A→B boundary.
        if len(anchors) > 1:
            refined = []
            for index, anchor in enumerate(anchors):
                candidates = [v for v in vectors if v is not None and float(v @ anchor) >= STABLE_SIMILARITY
                              and index == int(np.argmax([float(v @ a) for a in anchors]))]
                supported = [v for v in candidates
                             if sum(float(v @ other) >= 0.9 for other in candidates) >= 2]
                if supported:
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
            # Distinct voices may recur only in isolated windows, leaving no
            # stable consecutive run and no reliable cut. Their whole-clip
            # embedding would blend voices and could poison a speaker profile.
            repeated_conflict = not proposed and _conflicting_repeated_windows(vectors)
            if repeated_conflict and self._activity_guard is None:
                return self._plain_single(samples, uncertain=True, mixed=True)
            dubious = bool(proposed) or bool(unresolved) or repeated_conflict
            turns = self._single(
                samples,
                uncertain=bool(unresolved) or repeated_conflict,
                cancelled=cancelled,
                need_guard=dubious,
                anchors=anchors,
            )
            if repeated_conflict and len(turns) == 1 and self._strict_activity:
                return self._plain_single(samples, uncertain=True, mixed=True)
            return turns
        bounds = [0, *cuts[:MAX_TURNS - 1], samples.size]
        turns = []
        for start, stop in zip(bounds, bounds[1:], strict=False):
            if cancelled is not None and cancelled():
                return []
            piece = samples[start:stop]
            has_unresolved = any(start < boundary < stop for boundary in unresolved)
            short_piece = (stop - start) < int(MIN_VERIFY_SEC * TARGET_SAMPLE_RATE)
            local = self._single(
                piece,
                uncertain=has_unresolved,
                cancelled=cancelled,
                need_guard=has_unresolved or short_piece,
            )
            turns.extend(SpeakerTurn(start + t.start, start + t.stop, t.embedding, t.learnable) for t in local)
        if len(turns) > MAX_TURNS:
            return self._plain_single(samples, uncertain=True, mixed=True)
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

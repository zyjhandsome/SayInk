"""Bounded neural checks for overlapping or unresolved multiple voices.

Local speaker labels are disposable. They never allocate session identities.
Only the fixed pyannote powerset layout is accepted; it includes three local
speakers and the three possible two-speaker overlaps.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from sayink.audio_utils import MAX_SEGMENT_SEC, TARGET_SAMPLE_RATE

MAX_ACTIVITY_SEC = MAX_SEGMENT_SEC
OVERLAP_PROBABILITY = 0.65
MIN_OVERLAP_SEC = 0.20
SINGLE_PROBABILITY = 0.60
MIN_LOCAL_VOICE_SEC = 0.30
ACTIVITY_EDGE_SEC = 0.12


@dataclass(frozen=True)
class ActivityEvidence:
    # Sample offsets only: window-local speaker numbers never leave inference.
    overlaps: tuple[tuple[int, int], ...] = ()
    cuts: tuple[int, ...] = ()
    unresolved: bool = False

    @property
    def unsafe(self) -> bool:
        return bool(self.overlaps or self.cuts or self.unresolved)


def _runs(mask):
    edges = np.diff(np.r_[False, mask, False].astype(np.int8))
    return zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1), strict=True)


def _sustained(mask: np.ndarray, frames: int) -> bool:
    return any(stop - start >= frames for start, stop in _runs(mask))


class SpeakerActivityGuard:
    """Reject evidence of overlap or multiple local voices in a proposed turn.

    Passing this check is not a guarantee of one real speaker. Thresholds
    require calibration, especially for quiet secondary voices and laughter.
    """

    name = "pyannote-segmentation-3.0"

    def __init__(self, model_path: str | Path):
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        self._session = ort.InferenceSession(str(model_path), sess_options=options,
                                             providers=["CPUExecutionProvider"])
        meta = self._session.get_modelmeta().custom_metadata_map
        if (meta.get("model_type") != "pyannote-segmentation-3.0"
                or int(meta.get("sample_rate", 0)) != TARGET_SAMPLE_RATE
                or int(meta.get("num_classes", 0)) != 7
                or int(meta.get("num_speakers", 0)) != 3
                or int(meta.get("powerset_max_classes", 0)) != 2):
            raise ValueError("unsupported speaker activity model")
        self._window = int(meta["window_size"])
        self._shift = int(meta["receptive_field_shift"])
        self._field = int(meta["receptive_field_size"])
        if (self._window != 10 * TARGET_SAMPLE_RATE
                or not 0 < self._shift <= self._field <= self._window):
            raise ValueError("invalid speaker activity timing metadata")
        self._input = self._session.get_inputs()[0].name

    def is_unsafe(self, audio: np.ndarray, *, cancelled=None) -> bool:
        return self.analyze(audio, cancelled=cancelled).unsafe

    def _probability_windows(self, audio, *, cancelled=None):
        samples = np.asarray(audio, dtype=np.float32).reshape(-1)
        if (not samples.size or samples.size > MAX_ACTIVITY_SEC * TARGET_SAMPLE_RATE
                or not np.all(np.isfinite(samples))):
            raise ValueError("invalid or unbounded speaker activity input")
        starts = list(range(0, max(1, len(samples) - self._window + 1), self._window // 2))
        if starts[-1] + self._window < len(samples):
            starts.append(len(samples) - self._window)
        for start in starts:
            if cancelled is not None and cancelled():
                raise InterruptedError("speaker activity analysis cancelled")
            piece = samples[start:start + self._window]
            padded = np.pad(piece, (0, self._window - len(piece)))
            scores = np.asarray(self._session.run(None, {self._input: padded[None, None, :]})[0])
            frames = (self._window - self._field) // self._shift + 1
            if scores.shape != (1, frames, 7):
                raise ValueError("invalid speaker activity output")
            probabilities = np.exp(scores[0])
            if (not np.all(np.isfinite(probabilities))
                    or not np.allclose(probabilities.sum(axis=1), 1.0, atol=0.01)):
                raise ValueError("invalid speaker activity probabilities")
            # Padding supplies model context; frames centered beyond the real
            # waveform must not create an imaginary second voice.
            centers = (self._field - 1) / 2 + np.arange(len(probabilities)) * self._shift
            probabilities = probabilities[centers < len(piece)]
            yield start, probabilities

    def analyze(self, audio: np.ndarray, *, cancelled=None) -> ActivityEvidence:
        """Locate overlap and propose sequential changes for CAM++ verification.

        Different 10-second windows can permute their local labels. Compare
        labels only inside each window, then combine sample offsets, never ids.
        """
        overlaps, cuts = [], []
        unresolved = False
        overlap_frames = math.ceil(MIN_OVERLAP_SEC * TARGET_SAMPLE_RATE / self._shift)
        local_frames = math.ceil(MIN_LOCAL_VOICE_SEC * TARGET_SAMPLE_RATE / self._shift)
        edge = int(ACTIVITY_EDGE_SEC * TARGET_SAMPLE_RATE)
        for offset, probabilities in self._probability_windows(audio, cancelled=cancelled):
            origin = offset + (self._field - self._shift) // 2
            clear = [(begin, end) for begin, end in _runs(probabilities[:, 1:4].max(axis=1) >= SINGLE_PROBABILITY)
                     if end - begin >= local_frames]
            for begin, end in _runs(probabilities[:, 4:].sum(axis=1) >= OVERLAP_PROBABILITY):
                if end - begin >= overlap_frames:
                    # Include the uncertain transition up to sustained clear
                    # speech. A fading overlap score is not a safe voice edge.
                    begin = max((stop for _start, stop in clear if stop <= begin), default=0)
                    end = min((start for start, _stop in clear if start >= end), default=len(probabilities))
                    overlaps.append((max(0, origin + begin * self._shift - edge),
                                     min(len(audio), origin + end * self._shift + edge)))
            stable = []
            for label in (1, 2, 3):
                for begin, end in _runs(probabilities[:, label] >= SINGLE_PROBABILITY):
                    if end - begin >= local_frames:
                        stable.append((origin + begin * self._shift, origin + end * self._shift, label))
            stable.sort()
            for left, right in zip(stable, stable[1:], strict=False):
                if left[2] == right[2]:
                    continue
                # A long ambiguous gap cannot supply a precise turn boundary.
                if right[0] - left[1] > int(0.6 * TARGET_SAMPLE_RATE):
                    unresolved = True
                else:
                    cuts.append((left[1] + right[0]) // 2)
        merged = []
        for begin, end in sorted(overlaps):
            if merged and begin <= merged[-1][1] + edge:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((begin, end))
        unique = []
        for cut in sorted(cuts):
            if any(begin <= cut <= end for begin, end in merged):
                continue  # Overlap stays unknown; it is not a sequential cut.
            if not unique or cut - unique[-1] > edge:
                unique.append(cut)
        return ActivityEvidence(tuple(merged), tuple(unique), unresolved)

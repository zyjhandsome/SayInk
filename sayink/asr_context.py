"""Bounded acoustic context with explicit ownership on the input sample clock."""
from collections import deque
from dataclasses import dataclass

import numpy as np

from sayink.audio_utils import TARGET_SAMPLE_RATE

CONTEXT_SAMPLES = int(0.3 * TARGET_SAMPLE_RATE)


class ContextAudio(np.ndarray):
    """The array owns only its original samples; padding is separate metadata."""
    def __array_finalize__(self, source):
        self.prefix = np.zeros(0, np.float32)
        self.suffix = np.zeros(0, np.float32)


def with_context(audio, prefix=None, suffix=None):
    result = np.asarray(audio, dtype=np.float32).view(ContextAudio)
    result.prefix = np.asarray(prefix, dtype=np.float32) if prefix is not None else np.zeros(0, np.float32)
    result.suffix = np.asarray(suffix, dtype=np.float32) if suffix is not None else np.zeros(0, np.float32)
    return result


@dataclass
class _Pending:
    audio: np.ndarray
    bounds: tuple[int, int]
    forced: bool
    metadata: tuple
    prefix: np.ndarray


class BoundaryContextBuffer:
    """At most 300 ms lookahead, only at forced cuts, plus 300 ms left context.

    Original sample bounds never move. No text matching or global deduplication
    is involved. The ASR engine must supply independently validated timestamps.
    """
    def __init__(self):
        self._ring = np.zeros(0, np.float32)
        self._stop = 0
        self._previous_end = -1
        self._previous_tail = np.zeros(0, np.float32)
        self._pending = deque()

    def feed(self, block):
        self._stop += len(block)
        # Quiet-cut search can choose a point 3 seconds behind the clock.
        self._ring = np.concatenate([self._ring, np.asarray(block)])[-4 * TARGET_SAMPLE_RATE:]

    @property
    def pending_start(self):
        return self._pending[0].bounds[0] if self._pending else None

    def capture(self, audio, bounds, forced, metadata):
        prefix = self._previous_tail if bounds[0] == self._previous_end else np.zeros(0, np.float32)
        self._pending.append(_Pending(audio, bounds, forced, metadata, prefix))
        self._previous_end = bounds[1]
        self._previous_tail = np.asarray(audio[-CONTEXT_SAMPLES:]).copy() if forced else np.zeros(0, np.float32)

    def release(self, *, ending=False):
        ready = []
        while self._pending:
            item = self._pending[0]
            if item.forced and not ending and self._stop < item.bounds[1] + CONTEXT_SAMPLES:
                break
            self._pending.popleft()
            ring_start = self._stop - len(self._ring)
            start = item.bounds[1] - ring_start
            suffix = (self._ring[start:start + CONTEXT_SAMPLES].copy()
                      if item.forced and 0 <= start <= len(self._ring) else np.zeros(0, np.float32))
            ready.append((with_context(item.audio, item.prefix, suffix), item.metadata))
        return ready


def decode_owned_context(recognizer, audio, normalize, *, sample_rate=TARGET_SAMPLE_RATE, prepare=None):
    """Return None on unsupported/invalid alignment, so original audio wins.

    Timestamped CTC emissions are assigned to half-open original sample spans.
    Tokens, timestamp count, ordering, bounds and full-text coverage must all
    agree before any contextual text is accepted.
    """
    prefix = getattr(audio, "prefix", np.zeros(0, np.float32))
    suffix = getattr(audio, "suffix", np.zeros(0, np.float32))
    if not len(prefix) and not len(suffix):
        return None
    window = np.concatenate([prefix, np.asarray(audio), suffix])
    if prepare is not None:
        window = prepare(window)
    stream = recognizer.create_stream()
    stream.accept_waveform(sample_rate, window)
    recognizer.decode_stream(stream)
    result = stream.result
    try:
        tokens = list(getattr(result, "tokens", ()) or ())
        times = np.asarray(getattr(result, "timestamps", ()), dtype=float)
    except (TypeError, ValueError):
        return None
    if (not tokens or times.ndim != 1 or len(tokens) != len(times) or not np.all(np.isfinite(times))
            or np.any(np.diff(times) < 0) or times[0] < 0 or times[-1] > len(window) / sample_rate):
        return None
    clean_tokens = [str(token).replace("▁", " ").replace("Ġ", " ") for token in tokens]
    # Decimal points, minus signs and punctuation must also be covered.
    # An alphanumeric-only comparison could turn "3.5" into "35".
    core = lambda text: "".join(ch.lower() for ch in normalize(text) if not ch.isspace())
    if core("".join(clean_tokens)) != core(result.text):
        return None
    start, stop = len(prefix) / sample_rate, (len(prefix) + len(audio)) / sample_rate
    owned = [token for token, timestamp in zip(clean_tokens, times, strict=True) if start <= timestamp < stop]
    return normalize("".join(owned))

"""Bounded acoustic context with explicit ownership on the input sample clock."""
from collections import deque
from dataclasses import dataclass

import numpy as np

from sayink.audio_utils import TARGET_SAMPLE_RATE

CONTEXT_SAMPLES = int(0.3 * TARGET_SAMPLE_RATE)
CTC_BOUNDARY_EPSILON_SEC = 1e-4
CTC_END_SLACK_SEC = 0.03


class ContextAudio(np.ndarray):
    """The array owns only its original samples; padding is separate metadata."""

    def __array_finalize__(self, source):
        if source is None:
            self.prefix = np.zeros(0, np.float32)
            self.suffix = np.zeros(0, np.float32)
            self.noise_floor = None
            self.quiet_level = 0.0
            return
        self.noise_floor = getattr(source, "noise_floor", None)
        self.quiet_level = float(getattr(source, "quiet_level", 0.0) or 0.0)
        if isinstance(source, ContextAudio) and self.shape == source.shape:
            self.prefix = np.asarray(getattr(source, "prefix", ()), dtype=np.float32)
            self.suffix = np.asarray(getattr(source, "suffix", ()), dtype=np.float32)
        else:
            self.prefix = np.zeros(0, np.float32)
            self.suffix = np.zeros(0, np.float32)

    def __getitem__(self, key):
        item = super().__getitem__(key)
        if isinstance(item, ContextAudio) and isinstance(key, slice) and self.ndim == 1:
            start, stop, step = key.indices(len(self))
            if step == 1:
                item.prefix = self.prefix if start == 0 else np.zeros(0, np.float32)
                item.suffix = self.suffix if stop == len(self) else np.zeros(0, np.float32)
            else:
                item.prefix = np.zeros(0, np.float32)
                item.suffix = np.zeros(0, np.float32)
        return item


def with_context(audio, prefix=None, suffix=None):
    noise_floor = getattr(audio, "noise_floor", None)
    quiet_level = float(getattr(audio, "quiet_level", 0.0) or 0.0)
    result = np.asarray(audio, dtype=np.float32).view(ContextAudio)
    result.prefix = np.asarray(prefix, dtype=np.float32) if prefix is not None else np.zeros(0, np.float32)
    result.suffix = np.asarray(suffix, dtype=np.float32) if suffix is not None else np.zeros(0, np.float32)
    result.noise_floor = noise_floor
    result.quiet_level = quiet_level
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


def _is_ascii_word_continuation(prev_token: str, token: str) -> bool:
    """True when ``token`` continues an ASCII word started in ``prev_token``."""
    if not prev_token or not token or token[0].isspace():
        return False
    prev_tail = prev_token.rstrip()
    if not prev_tail or prev_tail != prev_token:
        return False
    return prev_tail[-1].isascii() and prev_tail[-1].isalnum() and token[0].isascii() and token[0].isalnum()


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
    window_dur = len(window) / sample_rate
    if (not tokens or times.ndim != 1 or len(tokens) != len(times) or not np.all(np.isfinite(times))
            or np.any(np.diff(times) < -1e-6) or times[0] < -CTC_BOUNDARY_EPSILON_SEC
            or times[-1] > window_dur + CTC_END_SLACK_SEC):
        return None
    clean_tokens = [str(token).replace("▁", " ").replace("Ġ", " ") for token in tokens]
    # Decimal points, minus signs and punctuation must also be covered.
    # An alphanumeric-only comparison could turn "3.5" into "35".
    core = lambda text: "".join(ch.lower() for ch in normalize(text) if not ch.isspace())
    if core("".join(clean_tokens)) != core(result.text):
        return None
    # Group ASCII BPE/SentencePiece continuation pieces with their word onset so
    # a single Latin word straddling a forced-cut boundary is neither split nor duplicated.
    effective_times = times.copy()
    for idx in range(1, len(clean_tokens)):
        if _is_ascii_word_continuation(clean_tokens[idx - 1], clean_tokens[idx]):
            effective_times[idx] = effective_times[idx - 1]
    start = len(prefix) / sample_rate
    stop = (len(prefix) + len(audio)) / sample_rate
    lo = start - CTC_BOUNDARY_EPSILON_SEC if len(prefix) else -CTC_END_SLACK_SEC
    hi = stop - CTC_BOUNDARY_EPSILON_SEC if len(suffix) else stop + CTC_END_SLACK_SEC
    if len(suffix):
        owned = [token for token, ts in zip(clean_tokens, effective_times, strict=True) if lo <= ts < hi]
    else:
        owned = [token for token, ts in zip(clean_tokens, effective_times, strict=True) if lo <= ts <= hi]
    return normalize("".join(owned))

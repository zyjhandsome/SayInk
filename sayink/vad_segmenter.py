"""Simple RMS-based speech segmentation for continuous listening mode."""

from __future__ import annotations

import numpy as np

from sayink.audio_utils import TARGET_SAMPLE_RATE, rms_volume
from sayink.speaker_session import dominant_route

SPEECH_RMS_THRESHOLD = 0.002
SILENCE_HOLD_SEC = 0.85
MIN_SPEECH_SEC = 0.25
# Stay inside the Fun-ASR-Nano / Qwen3-ASR context window so a long
# monologue emits a slice while the user is still talking.
MAX_SPEECH_SEC = 15.0
# A fixed RMS gate never sees silence once the room / mic noise floor sits
# above it, so a steady hiss turns into back-to-back MAX_SPEECH_SEC slices.
# Track the quietest recent block instead and lift the gate above it.
NOISE_FLOOR_WINDOW_SEC = 8.0
NOISE_FLOOR_RATIO = 3.0
# Never lift the gate past this, or quiet speech (a soft talker, a far mic,
# RMS around 0.01–0.02) would be treated as silence.
ADAPTIVE_THRESHOLD_CAP = 0.01
# A transient (key click, cough) is loud for one block only; speech keeps the
# gate open for at least this long before a segment is worth transcribing.
MIN_LOUD_SEC = 0.2


class SpeechSegmenter:
    """Accumulates 16 kHz mono audio; returns a segment when speech ends."""

    def __init__(
        self,
        sample_rate: int = TARGET_SAMPLE_RATE,
        speech_threshold: float = SPEECH_RMS_THRESHOLD,
        silence_hold_sec: float = SILENCE_HOLD_SEC,
        min_speech_sec: float = MIN_SPEECH_SEC,
        max_speech_sec: float = MAX_SPEECH_SEC,
        *,
        adaptive: bool = True,
        min_loud_sec: float = MIN_LOUD_SEC,
    ):
        self._rate = sample_rate
        self._speech_threshold = speech_threshold
        self._silence_hold_samples = int(sample_rate * silence_hold_sec)
        self._min_samples = int(sample_rate * min_speech_sec)
        self._max_samples = int(sample_rate * max_speech_sec)
        self._adaptive = adaptive
        self._min_loud_samples = int(sample_rate * min(min_loud_sec, min_speech_sec))
        self._floor_window_samples = int(sample_rate * NOISE_FLOOR_WINDOW_SEC)
        self._floor_blocks: list[tuple[int, float]] = []
        self._floor_total = 0
        self.reset()

    @property
    def speech_threshold(self) -> float:
        """Configured base gate (system loopback uses a lower one)."""
        return self._speech_threshold

    @property
    def effective_threshold(self) -> float:
        """Gate in use right now: the base gate lifted above the recent noise floor."""
        if not self._adaptive or not self._floor_blocks:
            return self._speech_threshold
        floor = min(rms for _count, rms in self._floor_blocks)
        lifted = min(floor * NOISE_FLOOR_RATIO, ADAPTIVE_THRESHOLD_CAP)
        return max(self._speech_threshold, lifted)

    def _track_noise_floor(self, block_size: int, rms: float) -> None:
        self._floor_blocks.append((block_size, rms))
        self._floor_total += block_size
        while self._floor_total > self._floor_window_samples and len(self._floor_blocks) > 1:
            count, _ = self._floor_blocks.pop(0)
            self._floor_total -= count

    def reset(self) -> None:
        # The noise-floor window deliberately survives reset(): it describes
        # the room, not the utterance.
        self._buffer: list[np.ndarray] = []
        self._energy: list[tuple[int, float, float]] = []
        self._total_samples = 0
        self._loud_samples = 0
        self._silence_run = 0
        self._in_speech = False
        self._last_route = ""

    @property
    def last_route(self) -> str:
        """mic, system, or empty for the segment most recently emitted."""
        return self._last_route

    def feed(
        self,
        mono_block: np.ndarray,
        *,
        mic_energy: float = 0.0,
        system_energy: float = 0.0,
    ) -> np.ndarray | None:
        block = np.asarray(mono_block, dtype=np.float32).reshape(-1)
        if block.size == 0:
            return None

        rms = rms_volume(block)
        self._track_noise_floor(int(block.size), float(rms))
        loud = rms >= self.effective_threshold
        if loud:
            self._in_speech = True
            self._silence_run = 0
            self._remember_block(block, mic_energy, system_energy)
            self._loud_samples += int(block.size)
            if self._total_samples >= self._max_samples:
                return self._take_segment(limit=self._max_samples)
            return None

        if not self._in_speech:
            return None

        self._remember_block(block, mic_energy, system_energy)
        self._silence_run += block.size
        if self._total_samples >= self._max_samples:
            return self._take_segment(limit=self._max_samples)
        if self._silence_run >= self._silence_hold_samples:
            return self._take_segment()
        return None

    def _too_short(self) -> bool:
        return self._total_samples < self._min_samples or self._loud_samples < self._min_loud_samples

    def flush(self) -> np.ndarray | None:
        """Emit buffered speech that has not yet reached the silence threshold."""
        if not self._in_speech or self._too_short():
            self.reset()
            return None
        if not self._buffer:
            self.reset()
            return None
        out = np.concatenate(self._buffer).astype(np.float32, copy=False)
        mic, system, _tail = self._split_energy(out.size)
        self.reset()
        self._last_route = dominant_route(mic, system)
        return out

    def _remember_block(self, block: np.ndarray, mic_energy: float, system_energy: float) -> None:
        self._buffer.append(block)
        self._energy.append((int(block.size), float(mic_energy), float(system_energy)))
        self._total_samples += int(block.size)

    def _split_energy(self, sample_count: int) -> tuple[float, float, list[tuple[int, float, float]]]:
        mic = 0.0
        system = 0.0
        remaining = int(sample_count)
        tail: list[tuple[int, float, float]] = []
        for count, mic_part, system_part in self._energy:
            count = int(count)
            if count <= 0:
                continue
            if remaining <= 0:
                tail.append((count, mic_part, system_part))
                continue
            if count <= remaining:
                mic += mic_part
                system += system_part
                remaining -= count
                continue
            fraction = remaining / count
            mic += mic_part * fraction
            system += system_part * fraction
            tail.append((count - remaining, mic_part * (1.0 - fraction), system_part * (1.0 - fraction)))
            remaining = 0
        return mic, system, tail

    def _take_segment(self, limit: int | None = None) -> np.ndarray | None:
        if self._too_short():
            self.reset()
            return None
        if not self._buffer:
            self.reset()
            return None
        audio = np.concatenate(self._buffer).astype(np.float32, copy=False)
        take = audio.size if limit is None else min(int(limit), int(audio.size))
        mic, system, tail_energy = self._split_energy(take)
        tail = audio[take:] if take < audio.size else None
        self.reset()
        self._last_route = dominant_route(mic, system)
        if tail is not None and tail.size:
            self._in_speech = True
            self._buffer = [tail]
            self._total_samples = int(tail.size)
            # The carried tail came from the loud run that forced this cut.
            self._loud_samples = int(tail.size)
            self._energy = tail_energy
        return audio[:take]

"""Simple RMS-based speech segmentation for continuous listening mode."""

from __future__ import annotations

import numpy as np

from sayink.audio_utils import TARGET_SAMPLE_RATE, rms_volume
from sayink.speaker_session import dominant_route

SPEECH_RMS_THRESHOLD = 0.002
SILENCE_HOLD_SEC = 1.2
MIN_SPEECH_SEC = 0.25
# The first syllable starts below the gate (soft consonant, breath), so the
# quiet audio just before the gate opened is kept and prepended.
PRE_ROLL_SEC = 0.3
# A clip with less voiced audio than this ("嗯", "一个") gives the ASR model no
# context to pick homophones or even the language; wait longer for the talker
# to go on so it can join the next words instead of being recognized alone.
SHORT_SPEECH_SEC = 0.8
SHORT_SILENCE_HOLD_SEC = 1.8
# Silence after the last word adds nothing to recognize and invites the model
# to hallucinate; at most this much of it stays on the segment.
TRAILING_SILENCE_KEEP_SEC = 0.5
# Once open, the gate closes only below this fraction of the opening level
# (but still above the noise floor), so the soft end of a word or a quiet
# syllable mid-sentence is not taken for a pause.
HOLD_GATE_RATIO = 0.5
HOLD_GATE_FLOOR_RATIO = 1.5
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
# ...except that the gate never sits below the noise itself: with a floor
# above the cap (fan, café, music under a meeting) every block counted as
# speech, pauses vanished, and 15 s slices cut words in half.
NOISE_FLOOR_MIN_RATIO = 1.5
# Only once this much audio was measured: before that the quietest block may
# be the talker's own voice (first words right after start), not the room.
NOISE_FLOOR_TRUST_SEC = 3.0
# A transient (key click, cough) is loud for one block only; speech keeps the
# gate open for at least this long before a segment is worth transcribing.
MIN_LOUD_SEC = 0.2
# When an utterance runs into MAX_SPEECH_SEC the cut lands on the quietest
# block inside this window before the limit (a breath or syllable gap) rather
# than exactly at the limit, so words are not sliced in half.
CUT_SEARCH_SEC = 3.0
# The quietest block only counts as a gap when its RMS is below this fraction
# of the window's median; otherwise the cut stays at the limit.
QUIET_CUT_RATIO = 0.6


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
        pre_roll_sec: float = PRE_ROLL_SEC,
        short_speech_sec: float = SHORT_SPEECH_SEC,
        short_silence_hold_sec: float = SHORT_SILENCE_HOLD_SEC,
        trailing_silence_keep_sec: float = TRAILING_SILENCE_KEEP_SEC,
    ):
        self._rate = sample_rate
        self._speech_threshold = speech_threshold
        self._silence_hold_samples = int(sample_rate * silence_hold_sec)
        self._pre_roll_samples = int(sample_rate * max(0.0, pre_roll_sec))
        self._short_speech_samples = int(sample_rate * max(0.0, short_speech_sec))
        self._short_hold_samples = max(
            self._silence_hold_samples, int(sample_rate * short_silence_hold_sec)
        )
        self._trailing_keep_samples = int(sample_rate * max(0.0, trailing_silence_keep_sec))
        self._min_samples = int(sample_rate * min_speech_sec)
        self._max_samples = int(sample_rate * max_speech_sec)
        self._adaptive = adaptive
        self._min_loud_samples = int(sample_rate * min(min_loud_sec, min_speech_sec))
        self._cut_search_samples = int(sample_rate * min(CUT_SEARCH_SEC, max_speech_sec / 2))
        self._floor_window_samples = int(sample_rate * NOISE_FLOOR_WINDOW_SEC)
        self._floor_trust_samples = int(sample_rate * NOISE_FLOOR_TRUST_SEC)
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
        floor = self._noise_floor()
        lifted = min(floor * NOISE_FLOOR_RATIO, ADAPTIVE_THRESHOLD_CAP)
        if self._floor_total >= self._floor_trust_samples:
            lifted = max(lifted, floor * NOISE_FLOOR_MIN_RATIO)
        return max(self._speech_threshold, lifted)

    @property
    def hold_threshold(self) -> float:
        """Lower gate that keeps an utterance going once it has started."""
        opening = self.effective_threshold
        hold = opening * HOLD_GATE_RATIO
        if self._adaptive and self._floor_blocks:
            hold = max(hold, self._noise_floor() * HOLD_GATE_FLOOR_RATIO)
        return min(hold, opening)

    def _noise_floor(self) -> float:
        return min(rms for _count, rms in self._floor_blocks)

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
        # (sample count, mic energy, system energy, block RMS) per buffered block.
        self._energy: list[tuple[int, float, float, float]] = []
        self._total_samples = 0
        self._loud_samples = 0
        self._silence_run = 0
        self._in_speech = False
        self._last_route = ""
        # Quiet blocks heard before the gate opened, newest last.
        self._pre_roll: list[tuple[np.ndarray, float, float, float]] = []
        self._pre_roll_total = 0
        # How much of the buffer came from the pre-roll (not the utterance).
        self._lead_samples = 0

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
        gate = self.hold_threshold if self._in_speech else self.effective_threshold
        loud = rms >= gate
        if loud:
            if not self._in_speech:
                self._in_speech = True
                self._take_pre_roll()
            self._silence_run = 0
            self._remember_block(block, mic_energy, system_energy, float(rms))
            self._loud_samples += int(block.size)
            if self._total_samples >= self._max_samples:
                return self._take_segment(limit=self._quiet_cut_point())
            return None

        if not self._in_speech:
            self._keep_pre_roll(block, mic_energy, system_energy, float(rms))
            return None

        self._remember_block(block, mic_energy, system_energy, float(rms))
        self._silence_run += block.size
        if self._total_samples >= self._max_samples:
            return self._take_segment(limit=self._quiet_cut_point())
        hold = (
            self._short_hold_samples
            if self._loud_samples < self._short_speech_samples
            else self._silence_hold_samples
        )
        if self._silence_run >= hold:
            trim = max(0, self._silence_run - self._trailing_keep_samples)
            return self._take_segment(limit=self._total_samples - trim, tail_is_speech=False)
        return None

    def _keep_pre_roll(
        self, block: np.ndarray, mic_energy: float, system_energy: float, rms: float
    ) -> None:
        if self._pre_roll_samples <= 0:
            return
        self._pre_roll.append((block, mic_energy, system_energy, rms))
        self._pre_roll_total += int(block.size)
        while self._pre_roll and self._pre_roll_total > self._pre_roll_samples:
            oldest, mic, system, old_rms = self._pre_roll[0]
            excess = self._pre_roll_total - self._pre_roll_samples
            if oldest.size <= excess:
                self._pre_roll.pop(0)
                self._pre_roll_total -= int(oldest.size)
                continue
            kept = (oldest.size - excess) / oldest.size
            self._pre_roll[0] = (oldest[excess:], mic * kept, system * kept, old_rms)
            self._pre_roll_total -= excess

    def _take_pre_roll(self) -> None:
        for block, mic, system, rms in self._pre_roll:
            self._remember_block(block, mic, system, rms)
        self._lead_samples = self._pre_roll_total
        self._pre_roll = []
        self._pre_roll_total = 0

    def _quiet_cut_point(self) -> int:
        """Sample offset to cut a too-long utterance: the end of the quietest
        block within CUT_SEARCH_SEC before the limit, else the limit itself."""
        limit = self._max_samples
        window_start = limit - self._cut_search_samples
        candidates: list[tuple[int, float]] = []
        end = 0
        for count, _mic, _system, rms in self._energy:
            end += count
            if end > limit:
                break
            if end >= window_start:
                candidates.append((end, rms))
        if not candidates:
            return limit
        best_end, best_rms = min(candidates, key=lambda c: c[1])
        typical = float(np.median([rms for _end, rms in candidates]))
        # A "breath" must be clearly quieter than the rest of the window;
        # on a flat signal every block ties and the limit itself is the cut.
        if best_rms > typical * QUIET_CUT_RATIO or best_end < self._min_samples:
            return limit
        return best_end

    def _should_drop(self) -> bool:
        spoken = self._total_samples - self._lead_samples
        if spoken < self._min_samples or self._loud_samples < self._min_loud_samples:
            return True
        return self._noise_only()

    def _noise_only(self) -> bool:
        # A segment opened by loud noise before the floor was trusted: once it
        # is, nothing in it rises above the room, and the ASR would only
        # hallucinate words into it.
        if not self._adaptive or self._floor_total < self._floor_trust_samples:
            return False
        peak = max((rms for _count, _mic, _system, rms in self._energy), default=0.0)
        return peak < self.effective_threshold

    def flush(self) -> np.ndarray | None:
        """Emit buffered speech that has not yet reached the silence threshold."""
        if not self._in_speech or self._should_drop():
            self.reset()
            return None
        if not self._buffer:
            self.reset()
            return None
        out = np.concatenate(self._buffer).astype(np.float32, copy=False)
        trim = max(0, self._silence_run - self._trailing_keep_samples)
        if trim:
            out = out[: out.size - trim]
        mic, system, _tail = self._split_energy(out.size)
        self.reset()
        self._last_route = dominant_route(mic, system)
        return out

    def _remember_block(
        self, block: np.ndarray, mic_energy: float, system_energy: float, rms: float = 0.0
    ) -> None:
        self._buffer.append(block)
        self._energy.append((int(block.size), float(mic_energy), float(system_energy), float(rms)))
        self._total_samples += int(block.size)

    def _split_energy(
        self, sample_count: int
    ) -> tuple[float, float, list[tuple[int, float, float, float]]]:
        mic = 0.0
        system = 0.0
        remaining = int(sample_count)
        tail: list[tuple[int, float, float, float]] = []
        for count, mic_part, system_part, rms in self._energy:
            count = int(count)
            if count <= 0:
                continue
            if remaining <= 0:
                tail.append((count, mic_part, system_part, rms))
                continue
            if count <= remaining:
                mic += mic_part
                system += system_part
                remaining -= count
                continue
            fraction = remaining / count
            mic += mic_part * fraction
            system += system_part * fraction
            tail.append((
                count - remaining,
                mic_part * (1.0 - fraction),
                system_part * (1.0 - fraction),
                rms,
            ))
            remaining = 0
        return mic, system, tail

    def _take_segment(
        self, limit: int | None = None, *, tail_is_speech: bool = True
    ) -> np.ndarray | None:
        if self._should_drop():
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
        if tail is not None and tail.size and not tail_is_speech:
            # Trimmed trailing silence: the next utterance's pre-roll.
            self._keep_pre_roll(
                tail,
                sum(part[1] for part in tail_energy),
                sum(part[2] for part in tail_energy),
                rms_volume(tail),
            )
        elif tail is not None and tail.size:
            self._in_speech = True
            self._buffer = [tail]
            self._total_samples = int(tail.size)
            # The carried tail came from the loud run that forced this cut.
            self._loud_samples = int(tail.size)
            self._energy = tail_energy
        return audio[:take]

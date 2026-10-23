"""Shared speech segmentation for hold-to-talk, continuous listening and meetings.

The segmenter decides *where* an utterance starts and ends from a per-block
"is this speech?" verdict. All modes can use :class:`SileroGate`; the adaptive
RMS :class:`EnergyGate` remains the fallback when its model is unavailable.
Pause durations are mode-specific while the detection algorithm is shared.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

import numpy as np

from sayink.audio_utils import MAX_SEGMENT_SEC, TARGET_SAMPLE_RATE, rms_volume
from sayink.speaker_session import dominant_route

log = logging.getLogger("SayInk")

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
# monologue emits a slice while the user is still talking (shared with the
# speaker detectors, see audio_utils.MAX_SEGMENT_SEC).
MAX_SPEECH_SEC = MAX_SEGMENT_SEC
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

VAD_ENERGY = "energy"
VAD_SILERO = "silero"
# Silero's exported model takes exactly 512 samples (32 ms) at 16 kHz.
SILERO_WINDOW = 512
# Silero's default 0.5 drops quiet or distant talkers wholesale: on real
# far-field meetings 9–14 % of labelled speech never left the segmenter,
# concentrated on the softest speakers. 0.3 halved that; 0.25 halves it
# again (8.3 % → 5 % / 5.8 % → 3 % of labelled speech on two AISHELL-4
# meetings) for about half a minute more non-speech per 40 minutes, and the
# recovered sentences are long enough to embed, so the end-of-meeting DER
# drops too (32.0 % → 27.4 %, 7.7 % → 5.9 %). 0.2 keeps opening on room noise
# instead: more false alarms, one extra speaker, no DER gain. The RMS floor
# still has to agree, so digital silence never opens the gate.
SILERO_THRESHOLD = 0.25
# A meeting pause is longer than a dictation pause: speakers think, look at
# slides, wait for someone else. Holding longer keeps a sentence together.
MEETING_SILENCE_HOLD_SEC = 1.5
# A talker who goes on for a long time: cut at a breath, not mid-word, but
# well inside the ASR context window.
MEETING_MAX_SPEECH_SEC = MAX_SEGMENT_SEC


class SpeechGate:
    """Per-block speech verdict used by :class:`SpeechSegmenter`."""

    name = "gate"
    fallback_reason = ""

    def is_speech(self, block: np.ndarray, rms: float, *, in_speech: bool) -> bool:
        raise NotImplementedError

    def reset(self) -> None:
        """Forget state between utterances (Silero keeps an LSTM state)."""


class EnergyGate(SpeechGate):
    """The adaptive RMS gate: speech is anything louder than the room."""

    name = VAD_ENERGY

    def __init__(self, segmenter: "SpeechSegmenter"):
        self._segmenter = segmenter

    def is_speech(self, block: np.ndarray, rms: float, *, in_speech: bool) -> bool:
        seg = self._segmenter
        gate = seg.hold_threshold if in_speech else seg.effective_threshold
        return rms >= gate


class SileroGate(SpeechGate):
    """Silero VAD (via sherpa-onnx ``VadModel``) with the energy gate as a floor.

    Each block is scored in 512-sample windows; any speech window makes the
    block speech. A static RMS floor still has to agree that *something* is
    there (``rms >= min(hold threshold, speech threshold)`` — the fixed
    0.002 / 0.0006, not the adaptive room floor), so Silero cannot open on
    digital silence; the segmenter's noise-floor tracking keeps running
    underneath for the energy fallback.
    """

    name = VAD_SILERO

    def __init__(self, model_path: str | Path, *, threshold: float = SILERO_THRESHOLD,
                 num_threads: int = 1):
        import sherpa_onnx

        silero = sherpa_onnx.SileroVadModelConfig(
            model=str(model_path),
            threshold=float(threshold),
            window_size=SILERO_WINDOW,
        )
        config = sherpa_onnx.VadModelConfig()
        config.silero_vad = silero
        config.sample_rate = TARGET_SAMPLE_RATE
        config.num_threads = int(num_threads)
        self._model = sherpa_onnx.VadModel.create(config)
        if self._model is None:
            raise RuntimeError("Silero VAD 模型未能载入")
        # A property in older sherpa-onnx builds, a method in 1.13.x.
        window = getattr(self._model, "window_size", SILERO_WINDOW)
        if callable(window):
            window = window()
        self._window = int(window or SILERO_WINDOW)
        self._carry = np.zeros(0, dtype=np.float32)
        self._last_verdict = False
        self.input_gain = 1.0

    def is_speech(self, block: np.ndarray, rms: float, *, in_speech: bool) -> bool:
        source = np.asarray(block, dtype=np.float32).reshape(-1)
        gain = min(self.input_gain, max(1.0, 0.9 / max(float(np.max(np.abs(source), initial=0)), 1e-8)))
        samples = np.concatenate([self._carry, source * gain])
        verdict = False
        offset = 0
        scored = False
        while offset + self._window <= samples.size:
            window = samples[offset:offset + self._window]
            scored = True
            if self._model.is_speech(window):
                verdict = True
            offset += self._window
        self._carry = samples[offset:]
        if not scored:
            # Block shorter than one window (a drained remainder): keep the
            # previous verdict so a trailing word is not cut off.
            return self._last_verdict
        self._last_verdict = verdict
        return verdict

    def reset(self) -> None:
        self._model.reset()
        self._carry = np.zeros(0, dtype=np.float32)
        self._last_verdict = False


def make_speech_gate(kind: str, segmenter: "SpeechSegmenter", *, model_path: str | Path | None = None) -> SpeechGate:
    """Build the requested gate; falls back to energy when Silero is unavailable."""
    if kind == VAD_SILERO and model_path:
        try:
            gate = SileroGate(model_path)
            log.info("公共 VAD: Silero (%s)", model_path)
            return gate
        except Exception as exc:  # sherpa_onnx missing, model file bad, …
            log.warning("Silero VAD 不可用，改用能量门限: %s", exc)
            gate = EnergyGate(segmenter)
            gate.fallback_reason = str(exc)
            return gate
    return EnergyGate(segmenter)


def make_meeting_segmenter(
    *,
    speech_threshold: float = SPEECH_RMS_THRESHOLD,
    vad: str = VAD_ENERGY,
    model_path: str | Path | None = None,
    gate_factory: Callable[["SpeechSegmenter"], SpeechGate] | None = None,
) -> "SpeechSegmenter":
    """The meeting-profile segmenter exactly as the recorder runs it.

    One constructor for the app, ``tools/asr_bench.py`` and
    ``tools/meeting_timeline_bench.py``: the pause length, the cut length and
    the gate choice (with its energy fallback) can no longer drift apart
    between what users hear and what the benches measure. The recorder passes
    ``gate_factory`` so its cached Silero session is reused instead of loaded
    again on the UI thread.
    """
    segmenter = SpeechSegmenter(
        speech_threshold=speech_threshold,
        silence_hold_sec=MEETING_SILENCE_HOLD_SEC,
        max_speech_sec=MEETING_MAX_SPEECH_SEC,
    )
    if gate_factory is not None:
        segmenter.set_gate(gate_factory(segmenter))
    else:
        segmenter.set_gate(make_speech_gate(vad, segmenter, model_path=model_path))
    return segmenter


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
        gate: SpeechGate | None = None,
    ):
        self._rate = sample_rate
        self._gate: SpeechGate = gate if gate is not None else EnergyGate(self)
        self.quiet_speech = False
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
    def gate(self) -> SpeechGate:
        return self._gate

    def set_gate(self, gate: SpeechGate | None) -> None:
        self._gate = gate if gate is not None else EnergyGate(self)
        self._gate.reset()

    @property
    def gate_name(self) -> str:
        return self._gate.name

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
        """Start a new input timeline as well as clearing utterance state."""
        self._input_samples = 0
        self._reset_buffer()

    def _reset_buffer(self) -> None:
        # The noise-floor window deliberately survives reset(): it describes
        # the room, not the utterance.
        gate = getattr(self, "_gate", None)
        if gate is not None and not isinstance(gate, EnergyGate):
            gate.reset()
        self._buffer: list[np.ndarray] = []
        # (sample count, mic energy, system energy, block RMS) per buffered block.
        self._energy: list[tuple[int, float, float, float]] = []
        self._total_samples = 0
        self._loud_samples = 0
        self._silence_run = 0
        self._in_speech = False
        self._continuation = False
        self._continuation_wait = 0
        self._last_route = ""
        self._last_bounds: tuple[int, int] | None = None
        self.last_forced = False
        # Quiet blocks heard before the gate opened, newest last.
        self._pre_roll: list[tuple[np.ndarray, float, float, float]] = []
        self._pre_roll_total = 0
        # How much of the buffer came from the pre-roll (not the utterance).
        self._lead_samples = 0

    @property
    def last_route(self) -> str:
        """mic, system, or empty for the segment most recently emitted."""
        return self._last_route

    @property
    def last_bounds(self) -> tuple[int, int] | None:
        """Original input sample bounds of the last emitted segment.

        Silence, dropped noise and pre-roll retain their place in this clock.
        Only an explicit reset starts the input timeline over.
        """
        return self._last_bounds

    @property
    def in_speech(self) -> bool:
        """True while an utterance is being buffered (gate has opened)."""
        return self._in_speech

    @property
    def pending_start(self) -> int:
        """Earliest input sample that can still belong to a future segment."""
        return self._input_samples - (self._total_samples if self._in_speech else self._pre_roll_total)

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

        self._input_samples += int(block.size)

        rms = rms_volume(block)
        self._track_noise_floor(int(block.size), float(rms))
        loud = self._gate.is_speech(block, float(rms), in_speech=self._in_speech)
        if loud and not isinstance(self._gate, EnergyGate):
            # A neural gate still needs *something* above the static RMS
            # floor (not the adaptive room floor, which would mute quiet
            # talkers Silero can hear); a hum scored as speech must not open
            # the gate on digital silence.
            floor = min(self.hold_threshold, self._speech_threshold)
            loud = rms >= (floor * 0.1 if self.quiet_speech else floor)
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
            self._continuation_wait += int(block.size)
            if self._continuation_wait >= self._silence_hold_samples:
                self._continuation = False
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
            # This is the tail of speech we already accepted, not an isolated
            # click. Do not discard it merely because the time limit split it.
            return not (self._continuation and self._loud_samples > 0)
        return self._noise_only()

    def _noise_only(self) -> bool:
        # A segment opened by loud noise before the floor was trusted: once it
        # is, nothing in it rises above the room, and the ASR would only
        # hallucinate words into it.
        if (self.quiet_speech and not isinstance(self._gate, EnergyGate)):
            return False  # Neural speech evidence is required in quiet mode.
        if not self._adaptive or self._floor_total < self._floor_trust_samples:
            return False
        peak = max((rms for _count, _mic, _system, rms in self._energy), default=0.0)
        return peak < self.effective_threshold

    def flush(self) -> np.ndarray | None:
        """Emit buffered speech that has not yet reached the silence threshold."""
        # A neural gate may need more than the final few milliseconds to open
        # again after a forced cut. Recover only the immediate audible tail,
        # never a later independent click or an arbitrary silent pre-roll.
        if (not self._in_speech and self._continuation and self._pre_roll
                and self._continuation_wait <= self._pre_roll_samples
                and any(rms >= self._speech_threshold * (0.05 if self.quiet_speech else 1)
                        for _b, _m, _s, rms in self._pre_roll)):
            self._take_pre_roll()
            self._lead_samples = 0
            self._loud_samples = self._total_samples
            self._in_speech = True
        if not self._in_speech or self._should_drop():
            self._reset_buffer()
            return None
        if not self._buffer:
            self._reset_buffer()
            return None
        start = self._input_samples - self._total_samples
        out = np.concatenate(self._buffer).astype(np.float32, copy=False)
        trim = max(0, self._silence_run - self._trailing_keep_samples)
        if trim:
            out = out[: out.size - trim]
        mic, system, _tail = self._split_energy(out.size)
        self._reset_buffer()
        self._last_route = dominant_route(mic, system)
        self._last_bounds = (start, start + int(out.size))
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
            self._reset_buffer()
            return None
        if not self._buffer:
            self._reset_buffer()
            return None
        start = self._input_samples - self._total_samples
        audio = np.concatenate(self._buffer).astype(np.float32, copy=False)
        take = audio.size if limit is None else min(int(limit), int(audio.size))
        mic, system, tail_energy = self._split_energy(take)
        tail = audio[take:] if take < audio.size else None
        self._reset_buffer()
        self._continuation = tail_is_speech
        self._last_route = dominant_route(mic, system)
        self._last_bounds = (start, start + take)
        self.last_forced = tail_is_speech
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

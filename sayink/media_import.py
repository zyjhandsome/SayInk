"""Turn a local audio or video file into meeting turns.

The source file is only read. Nothing is copied into the application data
directory, and speaker embeddings are not returned to the caller.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

import numpy as np

from sayink.audio_file_decoder import SUPPORTED_EXTENSIONS, iter_media_pcm_timed
from sayink.audio_utils import TARGET_SAMPLE_RATE

MEDIA_SOURCE = "import_media"
_SAMPLES_PER_MS = TARGET_SAMPLE_RATE // 1000


@dataclass(frozen=True)
class MediaTurn:
    speaker_id: int
    offset_ms: int
    duration_ms: int
    text: str


@dataclass(frozen=True)
class AudioChunk:
    start: int
    stop: int
    audio: np.ndarray


@dataclass(frozen=True)
class RecognitionResult:
    turns: tuple[MediaTurn, ...]
    cancelled: bool
    audio_ms: int = 0
    error: str = ""
    audio_path: str = ""


def file_dialog_filter() -> str:
    names = " ".join(f"*{ext}" for ext in sorted(SUPPORTED_EXTENSIONS))
    return f"音视频 ({names})"


def segment_pcm(audio: np.ndarray, *, vad: str, vad_path, cancel=None) -> list[AudioChunk]:
    """Cut 16 kHz mono audio with the same meeting segmenter the recorder uses."""
    clip = np.ascontiguousarray(np.asarray(audio, dtype=np.float32).reshape(-1))
    step = int(TARGET_SAMPLE_RATE * 0.1) or 1

    def blocks():
        for offset in range(0, int(clip.size), step):
            yield clip[offset:offset + step]

    return segment_blocks(blocks(), vad=vad, vad_path=vad_path, cancel=cancel)


def segment_blocks(blocks, *, vad: str, vad_path, cancel=None) -> list[AudioChunk]:
    """Cut a stream of 16 kHz mono blocks. Only a fraction of a second is buffered."""
    from sayink.vad_segmenter import VAD_ENERGY, attach_segment_acoustics, make_meeting_segmenter

    segmenter = make_meeting_segmenter(vad=vad or VAD_ENERGY, model_path=vad_path)
    chunks: list[AudioChunk] = []
    step = int(TARGET_SAMPLE_RATE * 0.1) or 1
    pending = np.zeros(0, dtype=np.float32)

    def keep(piece) -> None:
        bounds = segmenter.last_bounds
        if bounds is None or piece is None or not len(piece):
            return
        start, stop = bounds
        arr = attach_segment_acoustics(
            np.ascontiguousarray(piece, dtype=np.float32),
            noise_floor=getattr(piece, "noise_floor", None),
            quiet_level=float(getattr(piece, "quiet_level", 0.0) or 0.0),
        )
        chunks.append(AudioChunk(int(start), int(stop), arr))

    def feed_ready() -> bool:
        nonlocal pending
        while pending.size >= step:
            if cancel is not None and cancel():
                return False
            piece = segmenter.feed(pending[:step])
            pending = pending[step:]
            if piece is not None and getattr(piece, "size", 0):
                keep(piece)
        return True

    for block in blocks:
        if cancel is not None and cancel():
            return chunks
        incoming = np.ascontiguousarray(np.asarray(block, dtype=np.float32).reshape(-1))
        pending = np.concatenate((pending, incoming)) if pending.size else incoming
        if not feed_ready():
            return chunks
    if pending.size and (cancel is None or not cancel()):
        piece = segmenter.feed(pending)
        if piece is not None and getattr(piece, "size", 0):
            keep(piece)
    tail = segmenter.flush()
    if tail is not None and getattr(tail, "size", 0):
        keep(tail)
    return chunks


def _unpack_timed_block(item) -> tuple[np.ndarray, int]:
    if hasattr(item, "samples") and hasattr(item, "start_sample"):
        raw_audio = item.samples
        start_sample = int(item.start_sample)
    else:
        raw_audio = item[0]
        start_sample = int(item[1])
    arr = np.ascontiguousarray(np.asarray(raw_audio, dtype=np.float32).reshape(-1))
    return arr, max(0, start_sample)


class _TimedSegmentRunner:
    """Map streamed VAD segments onto true container sample positions without zero-array bloat."""

    def __init__(self, *, vad: str, vad_path, cancel) -> None:
        from sayink.vad_segmenter import VAD_ENERGY, attach_segment_acoustics, make_meeting_segmenter

        self._attach = attach_segment_acoustics
        self._segmenter = make_meeting_segmenter(vad=vad or VAD_ENERGY, model_path=vad_path)
        self._cancel = cancel
        self._step = int(TARGET_SAMPLE_RATE * 0.1) or 1
        self._jitter_limit = int(TARGET_SAMPLE_RATE * 0.02)
        self._inline_gap_limit = int(TARGET_SAMPLE_RATE * 2.0)
        self._zero_step = np.zeros(self._step, dtype=np.float32)
        self._pending = np.zeros(0, dtype=np.float32)
        self._seg_fed = 0
        self._seg_region_base = 0
        self._container_region_base: int | None = None
        self.timeline_end_samples = 0
        self.chunks: list[AudioChunk] = []

    def _cancelled(self) -> bool:
        return self._cancel is not None and bool(self._cancel())

    def _keep(self, piece) -> None:
        bounds = self._segmenter.last_bounds
        if bounds is None or piece is None or not len(piece):
            return
        raw_start, raw_stop = bounds
        base_c = self._container_region_base or 0
        true_start = max(0, base_c + (int(raw_start) - self._seg_region_base))
        true_stop = max(true_start, base_c + (int(raw_stop) - self._seg_region_base))
        arr = self._attach(
            np.ascontiguousarray(piece, dtype=np.float32),
            noise_floor=getattr(piece, "noise_floor", None),
            quiet_level=float(getattr(piece, "quiet_level", 0.0) or 0.0),
        )
        self.chunks.append(AudioChunk(int(true_start), int(true_stop), arr))

    def _feed_slice(self, block: np.ndarray) -> None:
        if block.size <= 0:
            return
        piece = self._segmenter.feed(block)
        self._seg_fed += int(block.size)
        if piece is not None and getattr(piece, "size", 0):
            self._keep(piece)

    def _drain_ready_steps(self) -> bool:
        while self._pending.size >= self._step:
            if self._cancelled():
                return False
            step_slice = self._pending[: self._step]
            self._pending = self._pending[self._step :]
            self._feed_slice(step_slice)
        return True

    def _flush_region(self) -> None:
        if self._pending.size and not self._cancelled():
            rem = self._pending
            self._pending = np.zeros(0, dtype=np.float32)
            self._feed_slice(rem)
        tail = self._segmenter.flush()
        if tail is not None and getattr(tail, "size", 0):
            self._keep(tail)

    def _feed_inline_silence(self, gap_samples: int) -> bool:
        remaining = int(gap_samples)
        while remaining > 0:
            if self._cancelled():
                return False
            take = min(self._step, remaining)
            silent = self._zero_step if take == self._step else self._zero_step[:take]
            self._pending = np.concatenate((self._pending, silent)) if self._pending.size else silent.copy()
            remaining -= take
            self.timeline_end_samples += take
            if not self._drain_ready_steps():
                return False
        return True

    def _mark_silence_gap_for_noise_floor(self) -> None:
        # Record a single zero-energy step so adaptive VAD knows silence elapsed
        # across a skipped container PTS gap without allocating padding arrays
        # or prepending pre-roll onto the post-gap utterance.
        self._feed_slice(self._zero_step)
        self._segmenter.flush()

    def feed_timed(self, incoming: np.ndarray, start_sample: int) -> bool:
        if incoming.size <= 0:
            return not self._cancelled()
        if self._container_region_base is None:
            if start_sample > self._jitter_limit:
                self._mark_silence_gap_for_noise_floor()
            self._container_region_base = start_sample
            self._seg_region_base = self._seg_fed
            self.timeline_end_samples = start_sample
        else:
            gap = start_sample - self.timeline_end_samples
            if gap > self._inline_gap_limit:
                self._flush_region()
                if self._cancelled():
                    return False
                self._mark_silence_gap_for_noise_floor()
                self._seg_region_base = self._seg_fed
                self._container_region_base = start_sample
                self.timeline_end_samples = start_sample
            elif gap > self._jitter_limit:
                if not self._feed_inline_silence(gap):
                    return False
        self._pending = np.concatenate((self._pending, incoming)) if self._pending.size else incoming
        self.timeline_end_samples += int(incoming.size)
        return self._drain_ready_steps()

    def finish(self) -> tuple[list[AudioChunk], int]:
        self._flush_region()
        if self.chunks:
            self.timeline_end_samples = max(
                self.timeline_end_samples,
                max(chunk.stop for chunk in self.chunks),
            )
        return self.chunks, int(self.timeline_end_samples)


def segment_timed_blocks(
    timed_blocks,
    *,
    vad: str,
    vad_path,
    cancel=None,
) -> tuple[list[AudioChunk], int]:
    """Cut streamed 16 kHz timed blocks and map chunks back to container sample offsets."""
    runner = _TimedSegmentRunner(vad=vad, vad_path=vad_path, cancel=cancel)
    for item in timed_blocks:
        if runner._cancelled():
            return runner.chunks, int(runner.timeline_end_samples)
        incoming, start_sample = _unpack_timed_block(item)
        if not runner.feed_timed(incoming, start_sample):
            return runner.chunks, int(runner.timeline_end_samples)
    return runner.finish()


def _apply_final_speakers(turns: list[MediaTurn], mapping: dict) -> tuple[MediaTurn, ...]:
    revised = []
    for index, turn in enumerate(turns):
        speaker = mapping.get(index, turn.speaker_id) if mapping else turn.speaker_id
        revised.append(turn if int(speaker) == turn.speaker_id else replace(turn, speaker_id=int(speaker)))
    return tuple(revised)


def recognize_with_session(
    chunks,
    decode_chunk,
    session,
    *,
    threshold: float,
    cancel,
    finalize=None,
    progress=None,
) -> RecognitionResult:
    """Recognize ready-cut chunks. ``cancel`` is checked before each new chunk.

    A chunk that already finished is kept. The returned turns carry speaker
    numbers and text only.
    """
    from sayink.speaker_clustering import final_assignments

    rows: list[MediaTurn] = []
    total = len(chunks)
    for index, chunk in enumerate(chunks):
        if cancel():
            mapping = _mapping(session, threshold, rows, finalize, final_assignments)
            return RecognitionResult(_apply_final_speakers(rows, mapping), True)
        try:
            decoded = decode_chunk(chunk) or []
        except Exception as exc:
            mapping = _mapping(session, threshold, rows, finalize, final_assignments)
            return RecognitionResult(
                _apply_final_speakers(rows, mapping), False, error=str(exc) or "识别中断",
            )
        for turn in decoded:
            text = str(getattr(turn, "text", "") or "").strip()
            if not text:
                continue
            start = int(chunk.start) + int(turn.start)
            stop = int(chunk.start) + int(turn.stop)
            speaker = 0
            assign = getattr(session, "assign", None)
            if assign is not None:
                speaker = int(assign(
                    turn.embedding,
                    start_ms=start // _SAMPLES_PER_MS,
                    end_ms=max(start, stop) // _SAMPLES_PER_MS,
                    sample_key=len(rows),
                    learnable=bool(getattr(turn, "learnable", False)),
                ))
            rows.append(MediaTurn(
                speaker,
                start // _SAMPLES_PER_MS,
                max(0, (stop - start) // _SAMPLES_PER_MS),
                text,
            ))
        if progress is not None:
            progress(index + 1, total)
        if cancel():
            mapping = _mapping(session, threshold, rows, finalize, final_assignments)
            return RecognitionResult(_apply_final_speakers(rows, mapping), True)
    mapping = _mapping(session, threshold, rows, finalize, final_assignments)
    return RecognitionResult(_apply_final_speakers(rows, mapping), False)


def _mapping(session, threshold, rows, finalize, default):
    if not rows:
        return {}
    choose = finalize or default
    return choose(session, threshold) or {}


def _chunk_analyzer(embedder, session, chunk):
    from sayink.speaker_turns import SpeakerTurn, SpeakerTurnDetector

    if embedder.supports_turn_detection:
        return SpeakerTurnDetector(
            embedder,
            context=session.turn_context,
            start_ms=int(chunk.start * 1000 / TARGET_SAMPLE_RATE),
            route="import",
        )

    def analyzer(clip, cancelled=None):
        if cancelled is not None and cancelled():
            return []
        vector = embedder(clip) if len(clip) else np.zeros(1, dtype=np.float32)
        return [SpeakerTurn(0, len(clip), vector, False)]

    return analyzer


_UNSET = object()


def _segment_source_media(
    source: Path,
    *,
    vad: str,
    vad_path,
    cancel,
    progress,
    progress_cb: Callable[[int, str], None] | None,
    decode_progress_cb: Callable[[int, str], None] | None,
    resolved_audio_path: str,
) -> tuple[list[AudioChunk] | None, int, RecognitionResult | None]:
    from sayink.audio_file_decoder import AudioDecodeError

    if source.suffix.lower() == ".wav" and source.is_file():
        from sayink.audio_file_decoder import iter_wav_pcm

        total = 0

        def wav_blocks():
            nonlocal total
            for block in iter_wav_pcm(source, cancel=cancel, progress_cb=decode_progress_cb):
                total += int(block.size)
                yield block

        blocks = wav_blocks()
        try:
            try:
                chunks = segment_blocks(blocks, vad=vad, vad_path=vad_path, cancel=cancel)
            except AudioDecodeError as exc:
                if "已取消" in str(exc):
                    return None, 0, RecognitionResult((), True, audio_path=resolved_audio_path)
                raise
        finally:
            blocks.close()
        timeline_samples = total
    else:
        try:
            timed_stream = iter_media_pcm_timed(
                source,
                cancel=cancel,
                progress_cb=decode_progress_cb,
            )
        except AudioDecodeError as exc:
            if "已取消" in str(exc):
                return None, 0, RecognitionResult((), True, audio_path=resolved_audio_path)
            raise
        close_stream = getattr(timed_stream, "close", None)
        try:
            try:
                chunks, timeline_samples = segment_timed_blocks(
                    timed_stream,
                    vad=vad,
                    vad_path=vad_path,
                    cancel=cancel,
                )
            except AudioDecodeError as exc:
                if "已取消" in str(exc):
                    return None, 0, RecognitionResult((), True, audio_path=resolved_audio_path)
                raise
        finally:
            if callable(close_stream):
                close_stream()

    audio_ms = int(timeline_samples * 1000 / TARGET_SAMPLE_RATE)
    read_msg = f"已读取 {audio_ms / 1000:.0f} 秒音频"
    if progress is not None and progress_cb is None:
        progress(read_msg)
    if progress_cb is not None:
        progress_cb(30, read_msg)
    if cancel is not None and cancel():
        return None, audio_ms, RecognitionResult((), True, audio_ms=audio_ms, audio_path=resolved_audio_path)
    return chunks, audio_ms, None


def recognize_file(
    path,
    *,
    recognizer,
    speaker_path,
    vad: str,
    vad_path,
    cancel,
    progress=None,
    glossary=(),
    progress_cb: Callable[[int, str], None] | None = None,
    speaker_model_enabled: bool = True,
    speaker_activity_enabled: bool = False,
    activity_path: str | Path | None | object = _UNSET,
) -> RecognitionResult:
    """Read ``path`` in place and recognize it with the meeting segmenter."""
    from sayink.audio_file_decoder import AudioDecodeError
    from sayink.meeting_speakers import MeetingSpeakerSession
    from sayink.speaker_embedding import make_embedder

    def _emit_decode_progress(pct: int, msg: str) -> None:
        if progress_cb is not None:
            progress_cb(min(30, max(0, int(pct * 30 // 100))), msg)

    if cancel is not None and cancel():
        return RecognitionResult((), True)
    source = Path(path)
    try:
        resolved_audio_path = str(source.resolve())
    except OSError:
        resolved_audio_path = str(source)
    if progress_cb is not None:
        progress_cb(0, "正在解码音频...")
    chunks, audio_ms, early = _segment_source_media(
        source,
        vad=vad,
        vad_path=vad_path,
        cancel=cancel,
        progress=progress,
        progress_cb=progress_cb,
        decode_progress_cb=_emit_decode_progress if progress_cb is not None else None,
        resolved_audio_path=resolved_audio_path,
    )
    if early is not None:
        return early
    assert chunks is not None
    effective_speaker_path = speaker_path if speaker_model_enabled else None
    available = bool(effective_speaker_path and Path(effective_speaker_path).is_file())
    embedder_kwargs: dict = {
        "enabled": bool(speaker_model_enabled) and available,
        "activity_enabled": bool(speaker_activity_enabled),
    }
    if activity_path is not _UNSET:
        embedder_kwargs["activity_path"] = activity_path
    embedder = make_embedder(effective_speaker_path, **embedder_kwargs)
    session = MeetingSpeakerSession.for_embedder(embedder)

    def decode_chunk(chunk):
        turns, error = recognizer.decode_meeting_blocking(
            chunk.audio,
            turn_analyzer=_chunk_analyzer(embedder, session, chunk),
            cancelled=cancel,
        )
        if error:
            raise AudioDecodeError(error)
        return turns

    def _on_chunk_progress(done: int, total_chunks: int) -> None:
        msg = f"正在识别 {done}/{total_chunks} 段"
        if progress is not None and progress_cb is None:
            progress(msg)
        if progress_cb is not None:
            pct = 30 + int(70 * done // max(1, total_chunks))
            progress_cb(min(100, max(30, pct)), msg)

    result = recognize_with_session(
        chunks,
        decode_chunk,
        session,
        threshold=float(embedder.cluster_threshold),
        cancel=cancel,
        progress=_on_chunk_progress if (progress is not None or progress_cb is not None) else None,
    )
    if result.error and not result.turns:
        raise AudioDecodeError(result.error)
    result = replace(result, audio_ms=audio_ms, audio_path=resolved_audio_path)
    if glossary:
        from sayink.asr_correction import apply_glossary_variants
        from sayink.speech_recognizer import restore_glossary_case

        result = replace(result, turns=tuple(
            replace(turn, text=apply_glossary_variants(restore_glossary_case(turn.text, glossary), glossary))
            for turn in result.turns
        ))
    if progress_cb is not None and not result.cancelled:
        progress_cb(100, "识别完成")
    return result

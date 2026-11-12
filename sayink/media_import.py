"""Turn a local audio or video file into meeting turns.

The source file is only read. Nothing is copied into the application data
directory, and speaker embeddings are not returned to the caller.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

import numpy as np

from sayink.audio_file_decoder import SUPPORTED_EXTENSIONS, decode_audio_file
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
    from sayink.vad_segmenter import VAD_ENERGY, make_meeting_segmenter

    segmenter = make_meeting_segmenter(vad=vad or VAD_ENERGY, model_path=vad_path)
    chunks: list[AudioChunk] = []
    step = int(TARGET_SAMPLE_RATE * 0.1) or 1
    pending = np.zeros(0, dtype=np.float32)

    def keep(piece) -> None:
        bounds = segmenter.last_bounds
        if bounds is None or piece is None or not len(piece):
            return
        start, stop = bounds
        chunks.append(AudioChunk(int(start), int(stop), np.ascontiguousarray(piece, dtype=np.float32)))

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
    audio_ms = 0
    if progress_cb is not None:
        progress_cb(0, "正在解码音频...")
    if source.suffix.lower() == ".wav" and source.is_file():
        from sayink.audio_file_decoder import iter_wav_pcm

        total = 0

        def timed_blocks():
            nonlocal total
            cb = _emit_decode_progress if progress_cb is not None else None
            for block in iter_wav_pcm(source, cancel=cancel, progress_cb=cb):
                total += int(block.size)
                yield block

        blocks = timed_blocks()
        try:
            try:
                chunks = segment_blocks(blocks, vad=vad, vad_path=vad_path, cancel=cancel)
            except AudioDecodeError as exc:
                if "已取消" in str(exc):
                    return RecognitionResult((), True)
                raise
        finally:
            blocks.close()
        audio_ms = int(total * 1000 / TARGET_SAMPLE_RATE)
        read_msg = f"已读取 {audio_ms / 1000:.0f} 秒音频"
        if progress is not None:
            progress(read_msg)
        if progress_cb is not None:
            progress_cb(30, read_msg)
        if cancel is not None and cancel():
            return RecognitionResult((), True, audio_ms=audio_ms)
    else:
        try:
            if progress_cb is not None:
                try:
                    audio = decode_audio_file(path, cancel=cancel, progress_cb=_emit_decode_progress)
                except TypeError:
                    # Support 2-arg monkeypatched decode_audio_file stubs in unit tests.
                    audio = decode_audio_file(path, cancel=cancel)
            else:
                audio = decode_audio_file(path, cancel=cancel)
        except AudioDecodeError as exc:
            if "已取消" in str(exc):
                return RecognitionResult((), True)
            raise
        read_msg = f"已读取 {audio.size / TARGET_SAMPLE_RATE:.0f} 秒音频"
        if progress is not None:
            progress(read_msg)
        if progress_cb is not None:
            progress_cb(30, read_msg)
        chunks = segment_pcm(audio, vad=vad, vad_path=vad_path, cancel=cancel)
        audio_ms = int(audio.size * 1000 / TARGET_SAMPLE_RATE)
    available = bool(speaker_path and Path(speaker_path).is_file())
    embedder = make_embedder(speaker_path, enabled=available)
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
        if progress is not None:
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
    result = replace(result, audio_ms=audio_ms)
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

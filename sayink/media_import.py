"""Turn a local audio or video file into meeting turns.

The source file is only read. Nothing is copied into the application data
directory, and speaker embeddings are not returned to the caller.
"""

from __future__ import annotations

from collections import deque
from contextlib import closing
from dataclasses import dataclass, replace
import inspect
from pathlib import Path
from typing import Callable, Iterator

import numpy as np

from sayink.audio_file_decoder import (
    SUPPORTED_EXTENSIONS,
    iter_media_pcm_timed,
    probe_media_duration_ms,
)
from sayink.audio_utils import TARGET_SAMPLE_RATE

MEDIA_SOURCE = "import_media"
_SAMPLES_PER_MS = TARGET_SAMPLE_RATE // 1000
# Reading the file is the short part of an import. The bar reserves the first
# 30 points for decode and the rest for recognition, so a finished read is 30,
# not 100. Callers must not leave the UI on that 30 with a "已读取" label.
_READ_PROGRESS_BUDGET = 30
_RECOGNITION_PROGRESS_BUDGET = 70
# Maximum ready VAD chunks held ahead of ASR at any time (~8 * <=15s < 8 MB RAM).
_STREAM_CHUNK_WINDOW = 8


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
            yield clip[offset : offset + step]

    return segment_blocks(blocks(), vad=vad, vad_path=vad_path, cancel=cancel)


def iter_segment_blocks(blocks, *, vad: str, vad_path, cancel=None) -> Iterator[AudioChunk]:
    """Lazily cut a stream of 16 kHz mono blocks with bounded memory."""
    from sayink.vad_segmenter import VAD_ENERGY, attach_segment_acoustics, make_meeting_segmenter

    segmenter = make_meeting_segmenter(vad=vad or VAD_ENERGY, model_path=vad_path)
    step = int(TARGET_SAMPLE_RATE * 0.1) or 1
    pending = np.zeros(0, dtype=np.float32)

    def make_chunk(piece) -> AudioChunk | None:
        bounds = segmenter.last_bounds
        if bounds is None or piece is None or not len(piece):
            return None
        start, stop = bounds
        arr = attach_segment_acoustics(
            np.ascontiguousarray(piece, dtype=np.float32),
            noise_floor=getattr(piece, "noise_floor", None),
            quiet_level=float(getattr(piece, "quiet_level", 0.0) or 0.0),
        )
        return AudioChunk(int(start), int(stop), arr)

    for block in blocks:
        if cancel is not None and cancel():
            return
        incoming = np.ascontiguousarray(np.asarray(block, dtype=np.float32).reshape(-1))
        pending = np.concatenate((pending, incoming)) if pending.size else incoming
        while pending.size >= step:
            if cancel is not None and cancel():
                return
            piece = segmenter.feed(pending[:step])
            pending = pending[step:]
            if piece is not None and getattr(piece, "size", 0):
                chunk = make_chunk(piece)
                if chunk is not None:
                    yield chunk
    if pending.size and (cancel is None or not cancel()):
        piece = segmenter.feed(pending)
        if piece is not None and getattr(piece, "size", 0):
            chunk = make_chunk(piece)
            if chunk is not None:
                yield chunk
    tail = segmenter.flush()
    if tail is not None and getattr(tail, "size", 0):
        chunk = make_chunk(tail)
        if chunk is not None:
            yield chunk


def segment_blocks(blocks, *, vad: str, vad_path, cancel=None) -> list[AudioChunk]:
    """Cut a stream of 16 kHz mono blocks. Only a fraction of a second is buffered."""
    return list(iter_segment_blocks(blocks, vad=vad, vad_path=vad_path, cancel=cancel))


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
        self._max_chunk_stop = 0
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
        self._max_chunk_stop = max(self._max_chunk_stop, int(true_stop))
        self.chunks.append(AudioChunk(int(true_start), int(true_stop), arr))

    def pop_ready_chunks(self) -> list[AudioChunk]:
        if not self.chunks:
            return []
        ready = list(self.chunks)
        self.chunks.clear()
        return ready

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
        self.timeline_end_samples = max(self.timeline_end_samples, self._max_chunk_stop)
        return self.chunks, int(self.timeline_end_samples)


def iter_segment_timed_blocks(
    timed_blocks,
    *,
    vad: str,
    vad_path,
    cancel=None,
    on_timeline: Callable[[int], None] | None = None,
) -> Iterator[AudioChunk]:
    """Lazily cut streamed 16 kHz timed blocks without retaining completed chunks."""
    runner = _TimedSegmentRunner(vad=vad, vad_path=vad_path, cancel=cancel)
    try:
        for item in timed_blocks:
            if runner._cancelled():
                return
            incoming, start_sample = _unpack_timed_block(item)
            ok = runner.feed_timed(incoming, start_sample)
            if on_timeline is not None:
                on_timeline(int(runner.timeline_end_samples))
            for chunk in runner.pop_ready_chunks():
                yield chunk
            if not ok:
                return
        runner.finish()
        if on_timeline is not None:
            on_timeline(int(runner.timeline_end_samples))
        for chunk in runner.pop_ready_chunks():
            yield chunk
    finally:
        if on_timeline is not None:
            on_timeline(int(max(runner.timeline_end_samples, runner._max_chunk_stop)))


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


_DEFAULT_ITER_MEDIA_PCM_TIMED = iter_media_pcm_timed
_DEFAULT_SEGMENT_TIMED_BLOCKS = segment_timed_blocks


def _apply_final_speakers(turns: list[MediaTurn], mapping: dict) -> tuple[MediaTurn, ...]:
    revised = []
    for index, turn in enumerate(turns):
        speaker = mapping.get(index, turn.speaker_id) if mapping else turn.speaker_id
        revised.append(turn if int(speaker) == turn.speaker_id else replace(turn, speaker_id=int(speaker)))
    return tuple(revised)


def _preview_lines(rows, *, limit: int = 8) -> str:
    lines: list[str] = []
    for turn in rows[-limit:]:
        text = " ".join(str(getattr(turn, "text", "") or "").split())
        if text:
            lines.append(text[:100])
    return "\n".join(lines)


def _append_decoded_turns(
    rows: list[MediaTurn],
    decoded,
    chunk: AudioChunk,
    session,
) -> None:
    assign = getattr(session, "assign", None)
    for turn in decoded:
        text = str(getattr(turn, "text", "") or "").strip()
        if not text:
            continue
        start = int(chunk.start) + int(turn.start)
        stop = int(chunk.start) + int(turn.stop)
        speaker = 0
        if assign is not None:
            speaker = int(
                assign(
                    turn.embedding,
                    start_ms=start // _SAMPLES_PER_MS,
                    end_ms=max(start, stop) // _SAMPLES_PER_MS,
                    sample_key=len(rows),
                    learnable=bool(getattr(turn, "learnable", False)),
                )
            )
        rows.append(
            MediaTurn(
                speaker,
                start // _SAMPLES_PER_MS,
                max(0, (stop - start) // _SAMPLES_PER_MS),
                text,
            )
        )


def recognize_with_session(
    chunks,
    decode_chunk,
    session,
    *,
    threshold: float,
    cancel,
    finalize=None,
    progress=None,
    preview=None,
) -> RecognitionResult:
    """Recognize ready-cut or streamed chunks. ``cancel`` is checked before each chunk.

    A chunk that already finished is kept. The returned turns carry speaker
    numbers and text only, and default finalization uses bounded speaker
    reclustering so multi-hour files never stall on O(N^2) clustering.
    """
    from sayink.speaker_clustering import bounded_final_assignments

    def default_finalize(sess, thresh):
        return bounded_final_assignments(sess, thresh, cancel=cancel)

    rows: list[MediaTurn] = []
    total = len(chunks) if hasattr(chunks, "__len__") else None
    chunk_iter = iter(chunks)
    index = 0
    while True:
        if cancel():
            mapping = _mapping(session, threshold, rows, finalize, default_finalize)
            return RecognitionResult(_apply_final_speakers(rows, mapping), True)
        try:
            chunk = next(chunk_iter)
        except StopIteration:
            break
        except Exception as exc:
            if cancel() and "已取消" in str(exc):
                mapping = _mapping(session, threshold, rows, finalize, default_finalize)
                return RecognitionResult(_apply_final_speakers(rows, mapping), True)
            mapping = _mapping(session, threshold, rows, finalize, default_finalize)
            return RecognitionResult(
                _apply_final_speakers(rows, mapping), False, error=str(exc) or "识别中断"
            )
        try:
            decoded = decode_chunk(chunk) or []
        except Exception as exc:
            mapping = _mapping(session, threshold, rows, finalize, default_finalize)
            return RecognitionResult(
                _apply_final_speakers(rows, mapping), False, error=str(exc) or "识别中断"
            )
        _append_decoded_turns(rows, decoded, chunk, session)
        index += 1
        if progress is not None:
            progress(index, total if total is not None else index)
        if preview is not None and rows:
            preview(_preview_lines(rows))
        if cancel():
            mapping = _mapping(session, threshold, rows, finalize, default_finalize)
            return RecognitionResult(_apply_final_speakers(rows, mapping), True)
    mapping = _mapping(session, threshold, rows, finalize, default_finalize)
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


class _StreamingChunkSource:
    """Bounded sliding-window stream of ``AudioChunk`` items from a media file."""

    def __init__(
        self,
        source: Path,
        *,
        vad: str,
        vad_path,
        cancel,
        decode_progress_cb: Callable[[int, str], None] | None,
        window_size: int = _STREAM_CHUNK_WINDOW,
    ) -> None:
        self._cancel = cancel
        self._window_size = max(1, int(window_size))
        self._window: deque[AudioChunk] = deque()
        self._chunk_iter: Iterator[AudioChunk] | None = None
        self._closers: list[Callable[[], None]] = []
        self.timeline_samples = 0
        self.total_produced = 0
        self.eof = False
        self.cancelled_during_open = False
        self._open(source, vad=vad, vad_path=vad_path, decode_progress_cb=decode_progress_cb)

    def _cancelled(self) -> bool:
        return self._cancel is not None and bool(self._cancel())

    def _set_timeline(self, samples: int) -> None:
        self.timeline_samples = max(self.timeline_samples, int(samples))

    def _open(
        self,
        source: Path,
        *,
        vad: str,
        vad_path,
        decode_progress_cb: Callable[[int, str], None] | None,
    ) -> None:
        from sayink.audio_file_decoder import AudioDecodeError

        if source.suffix.lower() == ".wav" and source.is_file():
            from sayink.audio_file_decoder import iter_wav_pcm

            def wav_blocks():
                for block in iter_wav_pcm(source, cancel=self._cancel, progress_cb=decode_progress_cb):
                    self.timeline_samples += int(block.size)
                    yield block

            blocks = wav_blocks()
            self._closers.append(blocks.close)
            gen = iter_segment_blocks(blocks, vad=vad, vad_path=vad_path, cancel=self._cancel)
            self._closers.append(gen.close)
            self._chunk_iter = gen
            self._prefill(AudioDecodeError)
            return

        try:
            timed_stream = iter_media_pcm_timed(
                source,
                cancel=self._cancel,
                progress_cb=decode_progress_cb,
            )
        except AudioDecodeError as exc:
            if "已取消" in str(exc):
                self.cancelled_during_open = True
                self.eof = True
                return
            raise
        close_stream = getattr(timed_stream, "close", None)
        if callable(close_stream):
            self._closers.append(close_stream)

        if segment_timed_blocks is not _DEFAULT_SEGMENT_TIMED_BLOCKS:
            try:
                chunks, timeline = segment_timed_blocks(
                    timed_stream, vad=vad, vad_path=vad_path, cancel=self._cancel
                )
            except AudioDecodeError as exc:
                self.close()
                if "已取消" in str(exc):
                    self.cancelled_during_open = True
                    self.eof = True
                    return
                raise
            finally:
                self.close()
            self._window.extend(chunks)
            self.total_produced = len(chunks)
            self.timeline_samples = int(timeline)
            self.eof = True
            if self._cancelled():
                self.cancelled_during_open = True
            return

        gen = iter_segment_timed_blocks(
            timed_stream,
            vad=vad,
            vad_path=vad_path,
            cancel=self._cancel,
            on_timeline=self._set_timeline,
        )
        self._closers.append(gen.close)
        self._chunk_iter = gen
        self._prefill(AudioDecodeError)

    def _prefill(self, decode_err_cls) -> None:
        try:
            self._fill_window()
        except decode_err_cls as exc:
            self.close()
            if "已取消" in str(exc):
                self.cancelled_during_open = True
                self.eof = True
                return
            raise
        except Exception:
            self.close()
            raise
        if self._cancelled():
            self.cancelled_during_open = True
            self.close()

    def _fill_window(self) -> None:
        if self.eof or self._chunk_iter is None:
            return
        while len(self._window) < self._window_size:
            if self._cancelled():
                return
            try:
                chunk = next(self._chunk_iter)
            except StopIteration:
                self.eof = True
                self.close()
                return
            self._window.append(chunk)
            self.total_produced += 1
            self.timeline_samples = max(self.timeline_samples, int(chunk.stop))

    @property
    def audio_ms(self) -> int:
        return int(self.timeline_samples * 1000 / TARGET_SAMPLE_RATE)

    def peek(self) -> AudioChunk | None:
        return self._window[0] if self._window else None

    def __iter__(self) -> Iterator[AudioChunk]:
        first = True
        while True:
            if not first and not self.eof and not self._cancelled():
                self._fill_window()
            first = False
            if not self._window:
                if not self.eof and not self._cancelled():
                    self._fill_window()
                if not self._window:
                    return
            yield self._window.popleft()

    def close(self) -> None:
        closers = list(reversed(self._closers))
        self._closers.clear()
        self._chunk_iter = None
        for closer in closers:
            try:
                closer()
            except Exception:
                pass


def _recognition_percent(index: int, fraction: float, count: int) -> int:
    """Map chunk progress onto the 30–99 band. 100 is completion.

    ``index`` is 1-based and ``fraction`` is how far the current chunk has
    been decoded. Leading silence is not treated as work already done.
    """
    total = max(1, int(count))
    done = (max(1, int(index)) - 1) + max(0.0, min(1.0, float(fraction)))
    return min(99, _READ_PROGRESS_BUDGET + int(_RECOGNITION_PROGRESS_BUDGET * done / total))


def _streaming_percent(
    index: int,
    fraction: float,
    chunk: AudioChunk | None,
    *,
    first_chunk_start: int,
    probed_samples: int,
    produced_count: int,
) -> int:
    frac = max(0.0, min(1.0, float(fraction)))
    if probed_samples > 0 and chunk is not None:
        span = max(1, probed_samples - first_chunk_start)
        pos = int(chunk.start) + frac * max(0, int(chunk.stop) - int(chunk.start))
        ratio = max(0.0, min(0.99, (pos - first_chunk_start) / span))
        return min(99, _READ_PROGRESS_BUDGET + int(_RECOGNITION_PROGRESS_BUDGET * ratio))
    done = (max(1, int(index)) - 1) + frac
    horizon = max(float(produced_count + 2), done + float(_STREAM_CHUNK_WINDOW))
    return min(99, _READ_PROGRESS_BUDGET + int(_RECOGNITION_PROGRESS_BUDGET * (done / horizon)))


def _decode_accepts_on_progress(fn) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    if "on_progress" in params:
        return True
    return any(param.kind is inspect.Parameter.VAR_KEYWORD for param in params.values())


def _probe_import_samples(source: Path) -> int:
    if not source.is_file():
        return 0
    if source.suffix.lower() != ".wav" and iter_media_pcm_timed is not _DEFAULT_ITER_MEDIA_PCM_TIMED:
        return 0
    duration_ms = probe_media_duration_ms(source)
    return int(duration_ms * _SAMPLES_PER_MS) if duration_ms > 0 else 0


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
    preview_cb: Callable[[str], None] | None = None,
    speaker_model_enabled: bool = True,
    speaker_activity_enabled: bool = False,
    activity_path: str | Path | None | object = _UNSET,
) -> RecognitionResult:
    """Stream ``path`` in place and recognize it with bounded chunk memory."""
    from sayink.audio_file_decoder import AudioDecodeError
    from sayink.meeting_speakers import MeetingSpeakerSession
    from sayink.speaker_embedding import make_embedder

    if cancel is not None and cancel():
        return RecognitionResult((), True)
    source = Path(path)
    try:
        resolved_audio_path = str(source.resolve())
    except OSError:
        resolved_audio_path = str(source)

    state = {"pct": -1, "recognizing": False}

    def _emit_decode_progress(pct: int, msg: str) -> None:
        if progress_cb is None or state["recognizing"]:
            return
        scaled = min(_READ_PROGRESS_BUDGET, max(0, int(pct) * _READ_PROGRESS_BUDGET // 100))
        if scaled < state["pct"]:
            scaled = state["pct"]
        else:
            state["pct"] = scaled
        progress_cb(scaled, msg)

    if progress_cb is not None:
        state["pct"] = 0
        progress_cb(0, "正在解码音频...")
    probed_samples = _probe_import_samples(source)

    source_stream = _StreamingChunkSource(
        source,
        vad=vad,
        vad_path=vad_path,
        cancel=cancel,
        decode_progress_cb=_emit_decode_progress if progress_cb is not None else None,
    )
    with closing(source_stream):
        if source_stream.cancelled_during_open:
            return RecognitionResult(
                (),
                True,
                audio_ms=source_stream.audio_ms,
                audio_path=resolved_audio_path,
            )
        if source_stream.eof and progress is not None and progress_cb is None:
            progress(f"已读取 {source_stream.audio_ms / 1000:.0f} 秒音频")
        first_chunk = source_stream.peek()
        first_chunk_start = int(first_chunk.start) if first_chunk is not None else 0
        state["recognizing"] = True

        def emit_recognition(fraction: float, index: int, chunk: AudioChunk | None = None) -> None:
            if first_chunk is None or (progress is None and progress_cb is None):
                return
            if source_stream.eof:
                total = max(1, source_stream.total_produced)
                pct = _recognition_percent(index, fraction, total)
                msg = f"正在识别 {index}/{total} 段"
            else:
                pct = _streaming_percent(
                    index,
                    fraction,
                    chunk,
                    first_chunk_start=first_chunk_start,
                    probed_samples=max(probed_samples, source_stream.timeline_samples),
                    produced_count=source_stream.total_produced,
                )
                msg = f"正在识别第 {index} 段"
            if pct < state["pct"]:
                pct = state["pct"]
            else:
                state["pct"] = pct
            if progress_cb is not None:
                progress_cb(pct, msg)
            elif progress is not None:
                progress(msg if source_stream.eof else f"{msg}（{pct}%）")

        if first_chunk is not None:
            # Publish this before the speaker model loads. Otherwise the last line
            # the UI has is "已读取（30%）" for the whole first segment.
            emit_recognition(0.0, 1, first_chunk)
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
        decode_with_progress = _decode_accepts_on_progress(recognizer.decode_meeting_blocking)
        index_box = {"n": 0}

        def decode_chunk(chunk: AudioChunk):
            index_box["n"] += 1
            index = index_box["n"]

            def on_progress(done: int, total: int) -> None:
                total_samples = max(1, int(total))
                done_samples = max(0, min(total_samples, int(done)))
                emit_recognition(done_samples / total_samples, index, chunk)

            emit_recognition(0.0, index, chunk)
            kwargs = {
                "turn_analyzer": _chunk_analyzer(embedder, session, chunk),
                "cancelled": cancel,
            }
            if decode_with_progress:
                kwargs["on_progress"] = on_progress
            turns, error = recognizer.decode_meeting_blocking(chunk.audio, **kwargs)
            if error:
                raise AudioDecodeError(error)
            emit_recognition(1.0, index, chunk)
            return turns

        result = recognize_with_session(
            source_stream,
            decode_chunk,
            session,
            threshold=float(embedder.cluster_threshold),
            cancel=cancel,
            preview=preview_cb,
        )
        final_audio_ms = source_stream.audio_ms

    if result.error and not result.turns:
        raise AudioDecodeError(result.error)
    result = replace(result, audio_ms=final_audio_ms, audio_path=resolved_audio_path)
    if glossary:
        from sayink.asr_correction import apply_glossary_variants
        from sayink.speech_recognizer import restore_glossary_case

        result = replace(
            result,
            turns=tuple(
                replace(
                    turn,
                    text=apply_glossary_variants(restore_glossary_case(turn.text, glossary), glossary),
                )
                for turn in result.turns
            ),
        )
    if progress_cb is not None and not result.cancelled:
        progress_cb(100, "识别完成")
    return result

"""Offline ASR benchmark: CER, hot-word hit rate and real-time factor per model.

Usage (from the repo root, models downloaded through the app)::

    py -3.10 tools/asr_bench.py --data bench/ --models funasr-nano --vad silero
    py -3.10 tools/asr_bench.py --data bench/ --hotwords "SayInk,CAM++" --language zh
    py -3.10 tools/asr_bench.py --data bench/ --vad energy

Layout of ``--data``: for every ``name.wav`` (mono PCM; resampled to 16 kHz) a
``name.txt`` with the reference transcript. Optional ``glossary.txt`` (one
term per line) is used both as recognizer hot words (when the model supports
them) and as the term list for the hit-rate column. Like the app, output then
takes the glossary spelling of English terms; the spelling column counts only
hits written exactly as the glossary does (CER and hit rate ignore case).

``--vad`` defaults to ``silero`` and cuts the clip with the meeting
:class:`sayink.vad_segmenter.SpeechSegmenter` (1.5 s pause, 15 s max) before
each slice is decoded. That is the product path. ``energy`` is the RMS
fallback. ``off`` decodes the whole file at once and must not be pasted into
the baseline table. A missing Silero model falls back to energy and says so.

The script never touches the app's config or history; it only loads models
from the same models directory the app uses. Results print as a Markdown
table so they can be pasted into ``docs/asr-benchmark.md``.

Only text and timings are kept in memory; nothing is written unless
``--out`` is given.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sayink.audio_utils import TARGET_SAMPLE_RATE, resample_mono
from sayink.recognition_settings import aux_model_path
from sayink.speech_recognizer import (
    AUX_MODEL_SILERO_VAD,
    MODEL_REGISTRY,
    SUPPORTS_HOTWORDS,
    _create_recognizer,
    format_hotwords,
    is_model_downloaded,
    language_argument,
    model_supports,
    normalize_asr_output,
    restore_glossary_case,
)
from sayink.vad_segmenter import (
    MEETING_MAX_SPEECH_SEC,
    MEETING_SILENCE_HOLD_SEC,
    SPEECH_RMS_THRESHOLD,
    VAD_ENERGY,
    VAD_SILERO,
    SpeechSegmenter,
    make_speech_gate,
)

VAD_OFF = "off"
VAD_CHOICES = (VAD_SILERO, VAD_ENERGY, VAD_OFF)
# Same tick as AudioRecorder.CHUNK_DURATION. The segmenter decides pauses
# from these blocks, so a different size would not match a live meeting.
_BLOCK_SAMPLES = int(TARGET_SAMPLE_RATE * 0.1)


def load_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wf:
        rate = wf.getframerate()
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        frames = wf.readframes(wf.getnframes())
    if width != 2:
        raise ValueError(f"{path.name}: only 16-bit PCM is supported")
    samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples, rate


def normalize_for_cer(text: str, *, keep_case: bool = False) -> str:
    """Drop whitespace and punctuation so CER measures words, not layout."""
    keep = []
    for ch in text:
        if ch.isspace():
            continue
        if ch in "，。！？、；：“”‘’（）《》〈〉【】,.!?;:\"'()[]{}<>-—…·":
            continue
        keep.append(ch if keep_case else ch.lower())
    return "".join(keep)


def edit_distance(a: str, b: str) -> int:
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def cer(reference: str, hypothesis: str) -> float:
    ref = normalize_for_cer(reference)
    hyp = normalize_for_cer(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    return edit_distance(ref, hyp) / len(ref)


def hotword_hits(terms: list[str], reference: str, hypothesis: str, *, exact_case: bool = False) -> tuple[int, int]:
    """(hits, expected): a term counts once per reference occurrence.

    ``exact_case`` only counts a hit spelled exactly as the glossary does,
    so ``GITHUB`` for ``GitHub`` is a miss.
    """
    ref = normalize_for_cer(reference)
    hyp = normalize_for_cer(hypothesis, keep_case=exact_case)
    expected = hits = 0
    for term in terms:
        key = normalize_for_cer(term)
        if not key:
            continue
        n_ref = ref.count(key)
        if n_ref == 0:
            continue
        expected += n_ref
        hits += min(n_ref, hyp.count(normalize_for_cer(term, keep_case=exact_case)))
    return hits, expected


def recognize(recognizer, audio: np.ndarray, rate: int, *, terms: list[str] = ()) -> str:
    stream = recognizer.create_stream()
    stream.accept_waveform(rate, audio)
    recognizer.decode_stream(stream)
    return restore_glossary_case(normalize_asr_output(stream.result.text), terms)


def segment_meeting(audio: np.ndarray, rate: int, *, vad: str) -> tuple[list[np.ndarray], str]:
    """Cut one clip the way a meeting does, and report which gate actually ran.

    ``off`` returns the whole 16 kHz clip. Silero with no downloaded model
    uses the energy gate; ``used`` is then ``energy``, not the request.
    """
    if vad not in VAD_CHOICES:
        raise ValueError(f"unknown vad: {vad}")
    clip = resample_mono(np.asarray(audio, dtype=np.float32), int(rate))
    if vad == VAD_OFF:
        return ([clip] if clip.size else []), VAD_OFF

    segmenter = SpeechSegmenter(
        speech_threshold=SPEECH_RMS_THRESHOLD,
        silence_hold_sec=MEETING_SILENCE_HOLD_SEC,
        max_speech_sec=MEETING_MAX_SPEECH_SEC,
    )
    model_path = aux_model_path(AUX_MODEL_SILERO_VAD) if vad == VAD_SILERO else None
    segmenter.set_gate(make_speech_gate(vad, segmenter, model_path=model_path))
    used = segmenter.gate_name if segmenter.gate_name in (VAD_SILERO, VAD_ENERGY) else VAD_ENERGY

    segments: list[np.ndarray] = []
    for start in range(0, clip.size, _BLOCK_SAMPLES):
        piece = segmenter.feed(clip[start:start + _BLOCK_SAMPLES])
        if piece is not None and piece.size:
            segments.append(piece)
    tail = segmenter.flush()
    if tail is not None and tail.size:
        segments.append(tail)
    return segments, used


def transcribe(recognizer, audio: np.ndarray, rate: int, *, vad: str,
               terms: list[str] | tuple[str, ...] = ()) -> tuple[str, str, int]:
    """Decode each meeting slice and join the text. Returns (text, vad_used, n)."""
    segments, used = segment_meeting(audio, rate, vad=vad)
    term_list = list(terms)
    parts = [
        recognize(recognizer, seg, TARGET_SAMPLE_RATE, terms=term_list)
        for seg in segments
    ]
    return "".join(parts), used, len(segments)


def run_model(model_id: str, items: list[tuple[Path, str]], *, terms: list[str], language: str,
              num_threads: int, vad: str = VAD_SILERO) -> dict:
    hotwords = format_hotwords([{"term": t} for t in terms]) if model_supports(model_id, SUPPORTS_HOTWORDS) else ""
    t0 = time.perf_counter()
    recognizer = _create_recognizer(model_id, num_threads, hotwords=hotwords,
                                    language=language_argument(model_id, language))
    load_s = time.perf_counter() - t0
    total_audio = total_decode = 0.0
    cers: list[float] = []
    hits = spelled = expected = 0
    segment_count = 0
    vad_used = vad
    per_file = []
    for wav_path, reference in items:
        audio, rate = load_wav(wav_path)
        segments, vad_used = segment_meeting(audio, rate, vad=vad)
        t1 = time.perf_counter()
        hypothesis = "".join(
            recognize(recognizer, seg, TARGET_SAMPLE_RATE, terms=terms) for seg in segments
        )
        decode_s = time.perf_counter() - t1
        audio_s = len(audio) / rate
        total_audio += audio_s
        total_decode += decode_s
        segment_count += len(segments)
        c = cer(reference, hypothesis)
        cers.append(c)
        h, e = hotword_hits(terms, reference, hypothesis)
        hits += h
        expected += e
        spelled += hotword_hits(terms, reference, hypothesis, exact_case=True)[0]
        per_file.append({
            "file": wav_path.name,
            "cer": round(c, 4),
            "rtf": round(decode_s / max(audio_s, 1e-6), 3),
            "segments": len(segments),
            "hypothesis": hypothesis,
        })
    return {
        "model": model_id,
        "vad_requested": vad,
        "vad_used": vad_used,
        "load_s": round(load_s, 1),
        "cer": round(float(np.mean(cers)) if cers else 0.0, 4),
        "hotword_hit_rate": round(hits / expected, 3) if expected else None,
        "term_spelling_rate": round(spelled / expected, 3) if expected else None,
        "rtf": round(total_decode / max(total_audio, 1e-6), 3),
        "audio_s": round(total_audio, 1),
        "segments": segment_count,
        "files": per_file,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", type=Path, required=True, help="directory with name.wav + name.txt pairs")
    parser.add_argument("--models", nargs="*", default=None, help="model ids (default: all downloaded)")
    parser.add_argument("--hotwords", default="", help="comma-separated terms (overrides glossary.txt)")
    parser.add_argument("--language", default="zh", help="auto / zh / en / ja (models that take a language)")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument(
        "--vad",
        default=VAD_SILERO,
        choices=VAD_CHOICES,
        help="silero (default, meeting pauses) | energy | off (whole file; not a baseline)",
    )
    parser.add_argument("--out", type=Path, default=None, help="write full JSON results here")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    items = []
    for wav_path in sorted(args.data.glob("*.wav")):
        txt = wav_path.with_suffix(".txt")
        if txt.exists():
            items.append((wav_path, txt.read_text(encoding="utf-8").strip()))
    if not items:
        print(f"no wav/txt pairs under {args.data}", file=sys.stderr)
        return 2
    if args.hotwords:
        terms = [t.strip() for t in args.hotwords.split(",") if t.strip()]
    else:
        glossary = args.data / "glossary.txt"
        terms = [t.strip() for t in glossary.read_text(encoding="utf-8").splitlines() if t.strip()] if glossary.exists() else []

    model_ids = args.models or [m["id"] for m in MODEL_REGISTRY if is_model_downloaded(m["id"])]
    missing = [m for m in model_ids if not is_model_downloaded(m)]
    if missing:
        print(f"not downloaded: {', '.join(missing)}", file=sys.stderr)
        model_ids = [m for m in model_ids if m not in missing]
    if not model_ids:
        return 2

    results = [
        run_model(m, items, terms=terms, language=args.language, num_threads=args.threads, vad=args.vad)
        for m in model_ids
    ]
    used = results[0]["vad_used"]
    if used != args.vad:
        print(f"vad {args.vad} unavailable, used {used}", file=sys.stderr)
    print(
        f"\n{len(items)} files · {results[0]['audio_s']} s audio · {len(terms)} terms · "
        f"language={args.language} · vad={args.vad} (used {used}) · "
        f"{results[0]['segments']} segments\n"
    )
    print("| 模型 | CER | 热词命中率 | 术语写法正确率 | RTF | 载入 (s) |")
    print("|---|---|---|---|---|---|")
    for r in results:
        hit = "—" if r["hotword_hit_rate"] is None else f"{r['hotword_hit_rate']:.1%}"
        spelling = "—" if r["term_spelling_rate"] is None else f"{r['term_spelling_rate']:.1%}"
        print(f"| {r['model']} | {r['cer']:.2%} | {hit} | {spelling} | {r['rtf']:.2f} | {r['load_s']} |")
    if args.out:
        args.out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

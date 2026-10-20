"""Offline ASR benchmark: CER, hot-word hit rate and real-time factor per model.

Usage (from the repo root, models downloaded through the app)::

    py -3.10 tools/asr_bench.py --data bench/ --models funasr-nano --vad silero
    py -3.10 tools/asr_bench.py --data bench/ --hotwords "SayInk,CAM++" --language zh
    py -3.10 tools/asr_bench.py --data bench/ --vad energy

Layout of ``--data``: for every ``name.wav`` (mono PCM; resampled to 16 kHz) a
``name.txt`` with the reference transcript. Optional ``glossary.txt`` (one
term per line; ``术语=误写1、误写2`` lists mis-hearings) is used both as
recognizer hot words (when the model supports them) and as the term list for
the hit-rate column. Like the app, output then takes the glossary spelling of
English terms and replaces the listed mis-hearings; the spelling column counts
only hits written exactly as the glossary does (CER and hit rate ignore case).

``--llm-correct`` additionally runs the app's LLM 纠错 mode over every decoded
segment (same prompt, same acceptance guard, previous segment as context) and
prints a second CER column. The key is read from ``SAYINK_LLM_API_KEY`` so it
never appears in a command line or a log.

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
import os
import sys
import time
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sayink.asr_correction import (
    apply_glossary_variants,
    correction_rejection_reason,
    correction_user_prompt,
    parse_glossary_line,
)
from sayink.audio_utils import TARGET_SAMPLE_RATE, resample_mono
from sayink.recognition_settings import aux_model_path
from sayink.text_polisher import polish_settings_complete
from sayink.speaker_embedding import make_embedder
from sayink.speaker_turns import SpeakerTurnDetector, analyze_turns
from sayink.speech_recognizer import (
    AUX_MODEL_SILERO_VAD,
    AUX_MODEL_SPEAKER,
    MODEL_REGISTRY,
    SUPPORTS_HOTWORDS,
    _create_recognizer,
    format_hotwords,
    is_model_downloaded,
    language_argument,
    model_supports,
    decode_asr_slice,
    restore_glossary_case,
)
from sayink.vad_segmenter import VAD_ENERGY, VAD_SILERO, make_meeting_segmenter

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


def recognize(recognizer, audio: np.ndarray, rate: int, *, terms: list[str] = (),
              retry_empty: bool = True, glossary=()) -> str:
    """Decode one slice and post-process it like the app: glossary spelling for
    English terms, then the glossary's listed mis-hearings replaced."""
    text = decode_asr_slice(recognizer, audio, sample_rate=rate, retry_empty=retry_empty)
    return apply_glossary_variants(restore_glossary_case(text, terms), glossary)


def parse_glossary_file(path: Path) -> list[dict]:
    """``glossary.txt`` lines; ``术语=误写1、误写2`` keeps the mis-hearings."""
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = parse_glossary_line(line)
        if entry is not None:
            entries.append(entry)
    return entries


def llm_correct_segments(texts: list[str], *, api_url: str, api_key: str, model_name: str,
                         glossary=(), timeout_sec: float = 30.0) -> tuple[list[str], dict]:
    """Run the app's correction mode over decoded segments, one request each,
    with the previous accepted segment as context and the same acceptance
    guard the app applies. Returns the corrected texts and a small summary."""
    import httpx

    from sayink.text_polisher import LLM_MODE_CORRECT, build_system_prompt, chat_completion, thinking_payload_extra

    system = build_system_prompt(LLM_MODE_CORRECT)
    extra = thinking_payload_extra(api_url, model_name, "disabled")
    corrected: list[str] = []
    changed = errors = 0
    rejected: dict[str, int] = {}
    latencies: list[float] = []
    previous = ""
    with httpx.Client(timeout=timeout_sec) as client:
        for raw in texts:
            if not raw.strip():
                corrected.append(raw)
                continue
            t0 = time.perf_counter()
            try:
                reply = chat_completion(
                    client, httpx, api_url=api_url, api_key=api_key, model_name=model_name,
                    messages=[{"role": "system", "content": system},
                              {"role": "user", "content": correction_user_prompt(raw, glossary, previous)}],
                    temperature=0.0, max_tokens=2048, label="纠错", extra_payload=extra,
                )
            except Exception as exc:  # network / provider errors degrade to the raw text, like the app
                errors += 1
                print(f"llm-correct error: {type(exc).__name__}: {str(exc)[:80]}", file=sys.stderr)
                reply = raw
            latencies.append(time.perf_counter() - t0)
            reason = correction_rejection_reason(raw, reply, glossary)
            if reason:
                rejected[reason] = rejected.get(reason, 0) + 1
                reply = raw
            elif reply != raw:
                changed += 1
            corrected.append(reply)
            previous = reply
    summary = {
        "model": model_name, "changed": changed, "errors": errors, "rejected": rejected,
        "median_latency_s": round(float(np.median(latencies)), 2) if latencies else None,
    }
    return corrected, summary


@dataclass(frozen=True)
class TimedAudioChunk:
    start: int
    stop: int
    audio: np.ndarray


def segment_meeting(audio: np.ndarray, rate: int, *, vad: str) -> tuple[list[np.ndarray], str]:
    """Cut one clip the way a meeting does, and report which gate actually ran.

    ``off`` returns the whole 16 kHz clip. Silero with no downloaded model
    uses the energy gate; ``used`` is then ``energy``, not the request.
    """
    chunks, used = segment_meeting_timed(audio, rate, vad=vad)
    return [chunk.audio for chunk in chunks], used


def segment_meeting_timed(audio: np.ndarray, rate: int, *, vad: str) -> tuple[list[TimedAudioChunk], str]:
    """Keep original sample offsets through silence, pre-roll and carried tails."""
    if vad not in VAD_CHOICES:
        raise ValueError(f"unknown vad: {vad}")
    clip = resample_mono(np.asarray(audio, dtype=np.float32), int(rate))
    if vad == VAD_OFF:
        return ([TimedAudioChunk(0, len(clip), clip)] if clip.size else []), VAD_OFF

    model_path = aux_model_path(AUX_MODEL_SILERO_VAD) if vad == VAD_SILERO else None
    segmenter = make_meeting_segmenter(vad=vad, model_path=model_path)
    used = segmenter.gate_name if segmenter.gate_name in (VAD_SILERO, VAD_ENERGY) else VAD_ENERGY

    segments: list[TimedAudioChunk] = []

    def keep(piece):
        start, stop = segmenter.last_bounds
        if not np.array_equal(piece, clip[start:stop]):
            raise ValueError("VAD output does not match its original sample bounds")
        segments.append(TimedAudioChunk(start, stop, piece))

    for start in range(0, clip.size, _BLOCK_SAMPLES):
        piece = segmenter.feed(clip[start:start + _BLOCK_SAMPLES])
        if piece is not None and piece.size:
            keep(piece)
    tail = segmenter.flush()
    if tail is not None and tail.size:
        keep(tail)
    return segments, used


def decode_segments(recognizer, segments, *, terms: list[str] | tuple[str, ...] = (),
                    retry_empty: bool = True, turn_analyzer=None, glossary=()) -> list[str]:
    """Split each VAD slice at speaker turns and decode every piece.

    The one decode path for ``transcribe`` (what the tests check) and
    ``run_model`` (what the benchmark measures), so the two cannot drift.
    """
    term_list = list(terms)
    return [
        recognize(recognizer, seg, TARGET_SAMPLE_RATE, terms=term_list, retry_empty=retry_empty, glossary=glossary)
        for seg in split_speaker_turns(segments, turn_analyzer)
    ]


def transcribe(recognizer, audio: np.ndarray, rate: int, *, vad: str,
               terms: list[str] | tuple[str, ...] = (), retry_empty: bool = True,
               turn_analyzer=None, glossary=()) -> tuple[str, str, int]:
    """Decode each meeting slice and join the text. Returns (text, vad_used, n)."""
    segments, used = segment_meeting(audio, rate, vad=vad)
    parts = decode_segments(recognizer, segments, terms=terms, retry_empty=retry_empty,
                            turn_analyzer=turn_analyzer, glossary=glossary)
    return "".join(parts), used, len(parts)


def split_speaker_turns(segments, turn_analyzer):
    if turn_analyzer is None:
        return segments
    return [audio[turn.start:turn.stop] for audio in segments
            for turn in analyze_turns(audio, turn_analyzer)]


def run_model(model_id: str, items: list[tuple[Path, str]], *, terms: list[str], language: str,
              num_threads: int, vad: str = VAD_SILERO, retry_empty: bool = True,
              turn_analyzer=None, turn_backend: str = "off", glossary=(),
              llm_correct: dict | None = None) -> dict:
    hotwords = format_hotwords([{"term": t} for t in terms]) if model_supports(model_id, SUPPORTS_HOTWORDS) else ""
    t0 = time.perf_counter()
    recognizer = _create_recognizer(model_id, num_threads, hotwords=hotwords,
                                    language=language_argument(model_id, language))
    load_s = time.perf_counter() - t0
    total_audio = total_decode = 0.0
    cers: list[float] = []
    hits = spelled = expected = 0
    segment_count = 0
    empty_count = 0
    vad_used = vad
    per_file = []
    corrected_cers: list[float] = []
    correction_summaries = []
    for wav_path, reference in items:
        audio, rate = load_wav(wav_path)
        segments, vad_used = segment_meeting(audio, rate, vad=vad)
        t1 = time.perf_counter()
        # RTF counts turn splitting and decoding, not the VAD pass above.
        outputs = decode_segments(recognizer, segments, terms=terms, retry_empty=retry_empty,
                                  turn_analyzer=turn_analyzer, glossary=glossary)
        hypothesis = "".join(outputs)
        decode_s = time.perf_counter() - t1
        corrected_text = None
        if llm_correct:
            fixed, summary = llm_correct_segments(outputs, glossary=glossary, **llm_correct)
            corrected_text = "".join(fixed)
            corrected_cers.append(cer(reference, corrected_text))
            correction_summaries.append({"file": wav_path.name, **summary})
        audio_s = len(audio) / rate
        total_audio += audio_s
        total_decode += decode_s
        segment_count += len(outputs)
        empty_count += sum(not text for text in outputs)
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
            "segments": len(outputs),
            "empty_segments": sum(not text for text in outputs),
            "hypothesis": hypothesis,
            **({"corrected_cer": round(corrected_cers[-1], 4), "corrected_hypothesis": corrected_text}
               if corrected_text is not None else {}),
        })
    return {
        "model": model_id,
        "retry_empty": retry_empty,
        "speaker_turns": turn_backend,
        "speaker_activity": getattr(turn_analyzer, "activity_backend", "off"),
        "vad_requested": vad,
        "vad_used": vad_used,
        "load_s": round(load_s, 1),
        "cer": round(float(np.mean(cers)) if cers else 0.0, 4),
        "hotword_hit_rate": round(hits / expected, 3) if expected else None,
        "term_spelling_rate": round(spelled / expected, 3) if expected else None,
        "rtf": round(total_decode / max(total_audio, 1e-6), 3),
        "audio_s": round(total_audio, 1),
        "segments": segment_count,
        "empty_segments": empty_count,
        "empty_segment_rate": empty_count / segment_count if segment_count else None,
        "corrected_cer": round(float(np.mean(corrected_cers)), 4) if corrected_cers else None,
        "llm_correction": correction_summaries,
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
    parser.add_argument("--no-short-retry", action="store_true", help="compare without the meeting short-utterance retry")
    parser.add_argument("--speaker-turns", choices=("auto", "off", "campplus"), default="auto",
                        help="auto enables the loaded CAM++ turn detector like a meeting; off compares without it")
    parser.add_argument("--speaker-activity", choices=("off", "auto"), default="off",
                        help="optional experimental multi-voice guard; off matches the application default")
    parser.add_argument("--llm-correct", action="store_true",
                        help="also run the app's LLM 纠错 pass per segment and add a corrected-CER column "
                             "(needs --llm-url, --llm-model and SAYINK_LLM_API_KEY in the environment)")
    parser.add_argument("--llm-url", default="", help="OpenAI-compatible endpoint for --llm-correct")
    parser.add_argument("--llm-model", default="", help="model name for --llm-correct")
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
        glossary = [{"term": t.strip()} for t in args.hotwords.split(",") if t.strip()]
    else:
        glossary = parse_glossary_file(args.data / "glossary.txt")
    terms = [g["term"] for g in glossary]

    llm_correct = None
    if args.llm_correct:
        api_key = os.environ.get("SAYINK_LLM_API_KEY", "")
        if not polish_settings_complete(args.llm_url, api_key, args.llm_model):
            print("--llm-correct needs --llm-url, --llm-model and SAYINK_LLM_API_KEY (key optional for local servers)",
                  file=sys.stderr)
            return 2
        llm_correct = {"api_url": args.llm_url, "api_key": api_key, "model_name": args.llm_model}

    model_ids = args.models or [m["id"] for m in MODEL_REGISTRY if is_model_downloaded(m["id"])]
    missing = [m for m in model_ids if not is_model_downloaded(m)]
    if missing:
        print(f"not downloaded: {', '.join(missing)}", file=sys.stderr)
        model_ids = [m for m in model_ids if m not in missing]
    if not model_ids:
        return 2

    analyzer = None
    turn_backend = "off"
    if args.speaker_turns != "off":
        embedder = make_embedder(aux_model_path(AUX_MODEL_SPEAKER), activity_enabled=args.speaker_activity == "auto")
        if embedder.supports_turn_detection:
            analyzer = SpeakerTurnDetector(embedder)
            turn_backend = embedder.name
        elif args.speaker_turns == "campplus":
            print("CAM++ unavailable for speaker-turn analysis", file=sys.stderr)
            return 2
    results = [
        run_model(m, items, terms=terms, language=args.language, num_threads=args.threads,
                  vad=args.vad, retry_empty=not args.no_short_retry, turn_analyzer=analyzer,
                  turn_backend=turn_backend, glossary=glossary, llm_correct=llm_correct)
        for m in model_ids
    ]
    used = results[0]["vad_used"]
    if used != args.vad:
        print(f"vad {args.vad} unavailable, used {used}", file=sys.stderr)
    print(
        f"\n{len(items)} files · {results[0]['audio_s']} s audio · {len(terms)} terms · "
        f"language={args.language} · vad={args.vad} (used {used}) · speaker_turns={turn_backend} · "
        f"{results[0]['segments']} segments\n"
    )
    corrected_col = " LLM 纠错后 CER |" if llm_correct else ""
    print(f"| 模型 | CER |{corrected_col} 热词命中率 | 术语写法正确率 | RTF | 载入 (s) |")
    print("|---|---|" + ("---|" if llm_correct else "") + "---|---|---|---|")
    for r in results:
        hit = "—" if r["hotword_hit_rate"] is None else f"{r['hotword_hit_rate']:.1%}"
        spelling = "—" if r["term_spelling_rate"] is None else f"{r['term_spelling_rate']:.1%}"
        corrected = f" {r['corrected_cer']:.2%} |" if llm_correct and r["corrected_cer"] is not None else (
            " — |" if llm_correct else "")
        print(f"| {r['model']} | {r['cer']:.2%} |{corrected} {hit} | {spelling} | {r['rtf']:.2f} | {r['load_s']} |")
    if llm_correct:
        for r in results:
            for s in r["llm_correction"]:
                print(f"  {r['model']} · {s['file']}: {s['changed']} 段已改, 拒绝 {s['rejected'] or '无'}, "
                      f"错误 {s['errors']}, 中位延迟 {s['median_latency_s']} s")
    if args.out:
        args.out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Offline ASR benchmark: CER, hot-word hit rate and real-time factor per model.

Usage (from the repo root, models downloaded through the app)::

    py -3.10 tools/asr_bench.py --data bench/ --models funasr-nano qwen3-asr-0.6b
    py -3.10 tools/asr_bench.py --data bench/ --hotwords "SayInk,CAM++" --language zh

Layout of ``--data``: for every ``name.wav`` (16 kHz mono, any length) a
``name.txt`` with the reference transcript. Optional ``glossary.txt`` (one
term per line) is used both as recognizer hot words (when the model supports
them) and as the term list for the hit-rate column.

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

from sayink.speech_recognizer import (  # noqa: E402
    MODEL_REGISTRY,
    SUPPORTS_HOTWORDS,
    _create_recognizer,
    format_hotwords,
    is_model_downloaded,
    language_argument,
    model_supports,
    normalize_asr_output,
)


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


def normalize_for_cer(text: str) -> str:
    """Drop whitespace and punctuation so CER measures words, not layout."""
    keep = []
    for ch in text:
        if ch.isspace():
            continue
        if ch in "，。！？、；：“”‘’（）《》〈〉【】,.!?;:\"'()[]{}<>-—…·":
            continue
        keep.append(ch.lower())
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


def hotword_hits(terms: list[str], reference: str, hypothesis: str) -> tuple[int, int]:
    """(hits, expected): a term counts once per reference occurrence."""
    ref = normalize_for_cer(reference)
    hyp = normalize_for_cer(hypothesis)
    expected = hits = 0
    for term in terms:
        key = normalize_for_cer(term)
        if not key:
            continue
        n_ref = ref.count(key)
        if n_ref == 0:
            continue
        expected += n_ref
        hits += min(n_ref, hyp.count(key))
    return hits, expected


def recognize(recognizer, audio: np.ndarray, rate: int) -> str:
    stream = recognizer.create_stream()
    stream.accept_waveform(rate, audio)
    recognizer.decode_stream(stream)
    return normalize_asr_output(stream.result.text)


def run_model(model_id: str, items: list[tuple[Path, str]], *, terms: list[str], language: str,
              num_threads: int) -> dict:
    hotwords = format_hotwords([{"term": t} for t in terms]) if model_supports(model_id, SUPPORTS_HOTWORDS) else ""
    t0 = time.perf_counter()
    recognizer = _create_recognizer(model_id, num_threads, hotwords=hotwords,
                                    language=language_argument(model_id, language))
    load_s = time.perf_counter() - t0
    total_audio = total_decode = 0.0
    cers: list[float] = []
    hits = expected = 0
    per_file = []
    for wav_path, reference in items:
        audio, rate = load_wav(wav_path)
        t1 = time.perf_counter()
        hypothesis = recognize(recognizer, audio, rate)
        decode_s = time.perf_counter() - t1
        audio_s = len(audio) / rate
        total_audio += audio_s
        total_decode += decode_s
        c = cer(reference, hypothesis)
        cers.append(c)
        h, e = hotword_hits(terms, reference, hypothesis)
        hits += h
        expected += e
        per_file.append({"file": wav_path.name, "cer": round(c, 4), "rtf": round(decode_s / max(audio_s, 1e-6), 3),
                         "hypothesis": hypothesis})
    return {
        "model": model_id,
        "load_s": round(load_s, 1),
        "cer": round(float(np.mean(cers)) if cers else 0.0, 4),
        "hotword_hit_rate": round(hits / expected, 3) if expected else None,
        "rtf": round(total_decode / max(total_audio, 1e-6), 3),
        "audio_s": round(total_audio, 1),
        "files": per_file,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", type=Path, required=True, help="directory with name.wav + name.txt pairs")
    parser.add_argument("--models", nargs="*", default=None, help="model ids (default: all downloaded)")
    parser.add_argument("--hotwords", default="", help="comma-separated terms (overrides glossary.txt)")
    parser.add_argument("--language", default="zh", help="auto / zh / en / ja (models that take a language)")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--out", type=Path, default=None, help="write full JSON results here")
    args = parser.parse_args(argv)

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

    results = [run_model(m, items, terms=terms, language=args.language, num_threads=args.threads) for m in model_ids]
    print(f"\n{len(items)} files · {results[0]['audio_s']} s audio · {len(terms)} terms · language={args.language}\n")
    print("| 模型 | CER | 热词命中率 | RTF | 载入 (s) |")
    print("|---|---|---|---|---|")
    for r in results:
        hit = "—" if r["hotword_hit_rate"] is None else f"{r['hotword_hit_rate']:.1%}"
        print(f"| {r['model']} | {r['cer']:.2%} | {hit} | {r['rtf']:.2f} | {r['load_s']} |")
    if args.out:
        args.out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

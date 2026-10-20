"""Measure within-chunk speaker-change boundaries on labeled real audio.

Manifest: {"recordings": [{"audio": "a-b-a.wav", "boundaries_ms": [3200,
6400], "case": "rapid_turn"}]}. Each clip must be no longer than 15 seconds,
matching a captured meeting chunk. This evaluates change detection, not DER,
overlap separation or transcription accuracy. No audio is saved by this tool.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sayink.audio_utils import TARGET_SAMPLE_RATE, resample_mono
from sayink.recognition_settings import aux_model_path
from sayink.speaker_embedding import make_embedder
from sayink.speaker_turns import MAX_ANALYSIS_SEC, SpeakerTurnDetector
from sayink.speech_recognizer import AUX_MODEL_SPEAKER
from tools.asr_bench import load_wav


def score_boundaries(reference, predicted, *, tolerance_ms=500) -> dict:
    """Chronological one-to-one matching, with missed/extra cuts explicit."""
    if tolerance_ms < 0 or not math.isfinite(tolerance_ms):
        raise ValueError("tolerance must be finite and nonnegative")
    gold, guess = sorted(reference), sorted(predicted)
    if any(not math.isfinite(v) or v <= 0 for v in [*gold, *guess]):
        raise ValueError("boundaries must be finite positive milliseconds")
    i = j = 0
    errors = []
    while i < len(gold) and j < len(guess):
        delta = guess[j] - gold[i]
        if abs(delta) <= tolerance_ms:
            errors.append(abs(delta))
            i += 1
            j += 1
        elif delta < 0:
            j += 1
        else:
            i += 1
    hits = len(errors)
    precision = hits / len(guess) if guess else (1.0 if not gold else 0.0)
    recall = hits / len(gold) if gold else 1.0
    return {"reference_cuts": len(gold), "predicted_cuts": len(guess), "matched": hits,
            "missed": len(gold) - hits, "extra": len(guess) - hits,
            "precision": precision, "recall": recall,
            "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
            "mean_error_ms": float(np.mean(errors)) if errors else None}


def evaluate_recordings(manifest: Path, embedder, *, tolerance_ms=500):
    payload = json.loads(manifest.read_text(encoding="utf-8-sig"))
    items = payload.get("recordings") if isinstance(payload, dict) else None
    if not isinstance(items, list) or not items:
        raise ValueError("manifest needs a nonempty recordings list")
    detector = SpeakerTurnDetector(embedder)
    files = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("boundaries_ms"), list):
            raise ValueError("each recording needs audio and boundaries_ms")
        path = manifest.parent / item["audio"]
        audio, rate = load_wav(path)
        audio = resample_mono(audio, rate)
        seconds = len(audio) / TARGET_SAMPLE_RATE
        if not 0 < seconds <= MAX_ANALYSIS_SEC:
            raise ValueError(f"{path.name}: clips must be 0–{MAX_ANALYSIS_SEC:g} seconds")
        reference = [float(value) for value in item["boundaries_ms"]]
        if len(set(reference)) != len(reference) or any(v >= seconds * 1000 for v in reference):
            raise ValueError(f"{path.name}: reference cuts must be unique and inside the clip")
        start = time.perf_counter()
        turns = detector(audio)
        elapsed = time.perf_counter() - start
        predicted = [t.start * 1000 / TARGET_SAMPLE_RATE for t in turns[1:]]
        files.append({"file": str(path), "case": str(item.get("case", "general")), "audio_s": seconds,
                      "analysis_s": elapsed, "rtf": elapsed / seconds,
                      "reference_ms": reference, "predicted_ms": predicted,
                      "unknown_turns": sum(not np.any(t.embedding) for t in turns),
                      **score_boundaries(reference, predicted, tolerance_ms=tolerance_ms)})
    total_audio = sum(f["audio_s"] for f in files)
    total_analysis = sum(f["analysis_s"] for f in files)
    return {"backend": embedder.name, "speaker_activity": detector.activity_backend, "tolerance_ms": tolerance_ms,
            "notes": "Boundary detection only, not overlap separation, DER or ASR accuracy.",
            "audio_s": total_audio, "analysis_s": total_analysis, "rtf": total_analysis / total_audio,
            "matched": sum(f["matched"] for f in files), "missed": sum(f["missed"] for f in files),
            "extra": sum(f["extra"] for f in files), "files": files}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--tolerance-ms", type=float, default=500)
    parser.add_argument("--speaker-activity", choices=("off", "auto"), default="off")
    args = parser.parse_args(argv)
    try:
        embedder = make_embedder(aux_model_path(AUX_MODEL_SPEAKER), activity_enabled=args.speaker_activity == "auto")
        if not embedder.supports_turn_detection:
            raise ValueError("CAM++ must be downloaded, enabled and successfully loaded for this benchmark")
        report = evaluate_recordings(args.manifest, embedder, tolerance_ms=args.tolerance_ms)
        output = json.dumps(report, ensure_ascii=False, indent=2)
        print(output)
        if args.out:
            args.out.write_text(output, encoding="utf-8")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

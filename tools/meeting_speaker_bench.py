"""Replay labeled real utterances against legacy and stable speaker policies.

The JSON manifest contains chronological, single-speaker clips from one
meeting: {"clips": [{"audio": "a.wav", "speaker": "A", "start_ms": 0,
"route": "", "case": "returning"}]}. Paths are relative to the manifest.
Original gaps are preserved. Overlapping or mixed-speaker clips must be
reported separately; this is not a time-aligned diarization/DER benchmark.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sayink.audio_utils import TARGET_SAMPLE_RATE, resample_mono
from sayink.meeting_speakers import MeetingSpeakerSession, voice_sample_is_clear
from sayink.recognition_settings import aux_model_path
from sayink.speaker_embedding import STICKY_GAP_MS, make_embedder
from sayink.speaker_session import SpeakerSession
from sayink.speech_recognizer import AUX_MODEL_SPEAKER
from tools.asr_bench import load_wav
from tools.speaker_eval import score_speakers


def load_manifest(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    clips = payload.get("clips") if isinstance(payload, dict) else None
    if not isinstance(clips, list) or not clips:
        raise ValueError("manifest needs a nonempty clips list")
    previous = -1
    result = []
    for index, item in enumerate(clips):
        if not isinstance(item, dict) or not str(item.get("speaker", "")).strip() or not item.get("audio"):
            raise ValueError(f"clip {index}: audio and speaker are required")
        start = int(item.get("start_ms", -1))
        if start < 0 or start < previous:
            raise ValueError(f"clip {index}: start_ms must be nonnegative and chronological")
        route = str(item.get("route", ""))
        if route not in ("", "mic", "system"):
            raise ValueError(f"clip {index}: invalid route")
        audio_path = path.parent / item["audio"]
        result.append({**item, "audio": audio_path, "start_ms": start, "route": route,
                       "speaker": str(item["speaker"]).strip()})
        previous = start
    return result


def run_comparison(clips: list[dict], embedder) -> dict:
    params = dict(threshold=embedder.threshold, keep_history=True,
                  sticky_threshold=embedder.sticky_threshold, sticky_gap_ms=STICKY_GAP_MS)
    policies = {"legacy": SpeakerSession(**params),
                "stable": MeetingSpeakerSession.for_embedder(embedder)}
    rows = {name: [] for name in policies}
    per_clip = []
    embedding_s = audio_s = 0.0
    matching_s = dict.fromkeys(policies, 0.0)
    for seq, clip in enumerate(clips):
        samples, rate = load_wav(clip["audio"])
        audio = resample_mono(samples, rate)
        duration = len(audio) / TARGET_SAMPLE_RATE
        if duration <= 0:
            raise ValueError(f"empty audio: {clip['audio']}")
        audio_s += duration
        t0 = time.perf_counter()
        vector = embedder(audio)
        clear = voice_sample_is_clear(audio)
        embedding_s += time.perf_counter() - t0
        predicted = {}
        for name, policy in policies.items():
            t0 = time.perf_counter()
            kwargs = dict(start_ms=clip["start_ms"] + 1,
                          end_ms=clip["start_ms"] + 1 + int(duration * 1000))
            if name == "stable":
                kwargs.update(sample_key=seq, learnable=clear)
            sid = policy.assign(vector, clip["route"], **kwargs)
            matching_s[name] += time.perf_counter() - t0
            rows[name].append((clip["speaker"], str(sid)))
            predicted[name] = sid
        per_clip.append({"seq": seq, "file": str(clip["audio"]), "speaker": clip["speaker"],
                         "start_ms": clip["start_ms"], "duration_s": duration,
                         "case": str(clip.get("case", "general")), "learnable": clear, **predicted})
    return {
        "backend": embedder.name, "audio_s": audio_s, "embedding_s": embedding_s,
        "notes": "Single-speaker clips only; inspect coverage together with split/merge. Not DER.",
        "policies": {
            name: {**score_speakers(label_rows),
                   "rtf": (embedding_s + matching_s[name]) / audio_s,
                   "cases": {case: score_speakers([
                       (c["speaker"], str(c[name])) for c in per_clip if c["case"] == case
                   ]) for case in sorted({c["case"] for c in per_clip})}}
            for name, label_rows in rows.items()
        },
        "clips": per_clip,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--backend", choices=("auto", "campplus", "mel"), default="auto")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    try:
        clips = load_manifest(args.manifest)
        model = aux_model_path(AUX_MODEL_SPEAKER) if args.backend != "mel" else None
        embedder = make_embedder(model, enabled=args.backend != "mel")
        if args.backend == "campplus" and embedder.name != "campplus":
            raise ValueError("CAM++ unavailable; download/load it before comparing this backend")
        result = run_comparison(clips, embedder)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.out:
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

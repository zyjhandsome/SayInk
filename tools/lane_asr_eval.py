"""Controlled overlap of two locally cached, human-labelled speech tracks.

Select clear turns from two different meetings; recognize their mono mix and
their independent lanes. This is an offline overlap experiment, not a real
microphone/loopback device measurement. Only the requested JSON is written.
"""
import argparse
import json
from pathlib import Path
import sys
import time

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sayink.recognition_settings import aux_model_path
from sayink.speech_recognizer import _create_recognizer, decode_asr_slice, AUX_MODEL_SILERO_VAD
from sayink.vad_segmenter import make_meeting_segmenter
from tools.asr_bench import load_wav, cer, edit_distance, normalize_for_cer
from tools.textgrid_reference import read_textgrid


def clear_turns(labels):
    return [u for u in labels if 4 <= (u.end_ms - u.start_ms) / 1000 <= 8
            and u.end_ms < 600000 and len(normalize_for_cer(u.text)) >= 15
            and not any(v is not u and v.speaker != u.speaker and v.start_ms < u.end_ms
                        and v.end_ms > u.start_ms for v in labels)]


def prepare(source, turn):
    clip = source[int(turn.start_ms * 16):int(turn.end_ms * 16)]
    rms = float(np.sqrt(np.mean(clip**2)))
    gain = min(0.04 / max(rms, 1e-8), 0.9 / max(float(np.max(np.abs(clip))), 1e-8))
    return clip * gain


def decode(recognizer, samples, *, threshold=0.002):
    seg = make_meeting_segmenter(vad="silero", speech_threshold=threshold,
                                  model_path=aux_model_path(AUX_MODEL_SILERO_VAD))
    if seg.gate_name != "silero":
        raise RuntimeError("Silero is required for this evaluation")
    text = []
    start = time.perf_counter()
    for begin in range(0, len(samples), 1600):
        if (audio := seg.feed(samples[begin:begin + 1600])) is not None:
            text.append(decode_asr_slice(recognizer, audio, retry_empty=True))
    if (audio := seg.flush()) is not None:
        text.append(decode_asr_slice(recognizer, audio, retry_empty=True))
    return "".join(text), time.perf_counter() - start


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--model", default="funasr-nano")
    args = parser.parse_args()
    files = sorted(args.data.glob("*.ch0.wav"))[:2]
    if len(files) < 2:
        raise ValueError("Two labelled meetings are required")
    sources, turns = [], []
    for file in files:
        source, rate = load_wav(file)
        if rate != 16000:
            raise ValueError("16 kHz mono WAV is required")
        sources.append(source)
        labels = read_textgrid(file.with_name(file.name.removesuffix(".ch0.wav") + ".TextGrid"))
        turns.append(clear_turns(labels)[:2])
    rec = _create_recognizer(args.model, 4, language="中文")
    report = {"model": args.model, "threads": 4, "controlled_overlap": True, "cases": []}
    for index, (left, right) in enumerate(zip(*turns, strict=True)):
        mic, system = prepare(sources[0], left), prepare(sources[1], right)
        length = max(len(mic), len(system))
        mic, system = np.pad(mic, (0, length - len(mic))), np.pad(system, (0, length - len(system)))
        mix = (mic + system) / 2
        mixed, mixed_seconds = decode(rec, mix)
        a, a_seconds = decode(rec, mic)
        b, b_seconds = decode(rec, system, threshold=0.0006)
        refs = [normalize_for_cer(turn.text) for turn in (left, right)]
        normalized = [normalize_for_cer(text) for text in (a, b)]
        total_chars = sum(map(len, refs))
        case = {"index": index, "seconds": length / 16000,
                "sources": [{"file": file.name, "start_ms": turn.start_ms, "end_ms": turn.end_ms,
                              "reference": turn.text} for file, turn in zip(files, (left, right), strict=True)],
                "mixed": mixed, "separate": [a, b],
                "mixed_cer_best_order": min(cer(left.text + right.text, mixed), cer(right.text + left.text, mixed)),
                "separate_weighted_cer": sum(edit_distance(ref, text) for ref, text in zip(refs, normalized, strict=True)) / total_chars,
                "mixed_rtf": mixed_seconds / (length / 16000),
                "separate_rtf": (a_seconds + b_seconds) / (length / 16000)}
        report["cases"].append(case)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({k: v for k, v in case.items() if k not in {"sources", "mixed", "separate"}}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

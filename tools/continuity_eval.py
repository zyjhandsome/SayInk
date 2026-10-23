"""Compare a Git baseline with current ASR on local human-labelled recordings.

No network, settings, history or credentials are accessed. Each *.ch0.wav in
--data needs a matching *.TextGrid. --out is the only output written. Quiet
audio is an explicit synthetic attenuation of real speech, not a new speaker
or microphone. Time-stamped boundary context is evaluated only on CTC models.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time
import types

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sayink import speech_recognizer as asr, vad_segmenter as vad
from sayink.asr_context import BoundaryContextBuffer, decode_owned_context
from sayink.recognition_settings import aux_model_path
from tools.asr_bench import load_wav, cer, normalize_for_cer
from tools.textgrid_reference import read_textgrid, reference_text


def baseline_module(ref, filename):
    name = "continuity_baseline_" + Path(filename).stem
    module = types.ModuleType(name)
    module.__spec__ = importlib.util.spec_from_loader(name, loader=None)
    module.__file__ = str(ROOT / filename)
    sys.modules[name] = module
    source = subprocess.check_output(["git", "show", ref + ":" + filename], cwd=ROOT).decode("utf-8")
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module


def chunks(audio, module, *, context=False, quiet=False):
    seg = module.make_meeting_segmenter(vad="silero", model_path=aux_model_path(asr.AUX_MODEL_SILERO_VAD))
    if quiet:
        seg.quiet_speech = seg.gate_name == "silero"
        seg.gate.input_gain = 4.0
    buffer = BoundaryContextBuffer() if context else None
    output = []
    bounds = []

    def keep(part):
        if part is None:
            return
        bounds.append(seg.last_bounds)
        if buffer:
            buffer.capture(part, seg.last_bounds, seg.last_forced, ())
        else:
            output.append(part)

    for start in range(0, len(audio), 1600):
        block = audio[start:start + 1600]
        if buffer:
            buffer.feed(block)
            output.extend(part for part, _meta in buffer.release())
        keep(seg.feed(block))
        if buffer:
            output.extend(part for part, _meta in buffer.release())
    keep(seg.flush())
    if buffer:
        output.extend(part for part, _meta in buffer.release(ending=True))
    return output, bounds


def run(rec, audio, reference, module, vad_module, *, context=False, quiet=False):
    start = time.perf_counter()
    pieces, bounds = chunks(audio, vad_module, context=context, quiet=quiet)
    vad_seconds = time.perf_counter() - start
    start = time.perf_counter()
    texts = []
    aligned = fallbacks = 0
    for piece in pieces:
        candidate = None
        has_context = len(getattr(piece, "prefix", ())) or len(getattr(piece, "suffix", ()))
        if context and has_context:
            candidate = decode_owned_context(rec, piece, module.normalize_asr_output)
            if candidate:
                aligned += 1
            else:
                fallbacks += 1
        options = {"quiet_speech": True} if quiet else {}
        texts.append(candidate or module.decode_asr_slice(rec, piece, retry_empty=True, **options))
    elapsed = time.perf_counter() - start
    hypothesis = "".join(texts)
    return {"cer": cer(reference, hypothesis), "decode_seconds": elapsed,
            "rtf": elapsed / (len(audio) / 16000), "vad_seconds": vad_seconds,
            "segments": len(pieces), "emitted_seconds": sum(len(p) for p in pieces) / 16000,
            "aligned_segments": aligned, "alignment_fallbacks": fallbacks,
            "reference_chars": len(normalize_for_cer(reference)), "bounds": bounds,
            "hypothesis": hypothesis}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--baseline-ref", default="HEAD")
    parser.add_argument("--models", nargs="+", default=["funasr-nano", "fireredasr2-ctc"])
    parser.add_argument("--seconds", type=float, default=600)
    parser.add_argument("--quiet-seconds", type=float, default=180)
    parser.add_argument("--quiet-scale", type=float, default=0.05)
    parser.add_argument("--resume", action="store_true", help="skip cases already recorded in --out")
    parser.add_argument("--only-quiet-enhancement", action="store_true")
    args = parser.parse_args()
    old_asr = baseline_module(args.baseline_ref, "sayink/speech_recognizer.py")
    old_vad = baseline_module(args.baseline_ref, "sayink/vad_segmenter.py")
    report = {"baseline_ref": subprocess.check_output(["git", "rev-parse", args.baseline_ref], cwd=ROOT).decode().strip(),
              "threads": 4, "llm": False, "speaker_turns": False, "runs": []}
    if args.resume and args.out.exists():
        previous = json.loads(args.out.read_text(encoding="utf-8"))
        if previous["baseline_ref"] != report["baseline_ref"]:
            raise ValueError("Cannot resume results from a different baseline")
        report = previous
    for model in args.models:
        rec = asr._create_recognizer(model, 4, language="中文")
        for path in sorted(args.data.glob("*.ch0.wav")):
            key = path.name.removesuffix(".ch0.wav")
            source, rate = load_wav(path)
            if rate != 16000:
                raise ValueError("Evaluation requires 16 kHz channel 0 WAV")
            labels = read_textgrid(args.data / (key + ".TextGrid"))
            for scale, seconds in ((1.0, args.seconds), (args.quiet_scale, args.quiet_seconds)):
                if args.only_quiet_enhancement and scale >= 1:
                    continue
                audio = source[:int(seconds * rate)] * scale
                reference = reference_text(labels, until_ms=len(audio) * 1000 / rate)
                asr.decode_asr_slice(rec, audio[:rate])
                cases = [("baseline", old_asr, old_vad, False), ("optimized", asr, vad, False)]
                if model == "fireredasr2-ctc":
                    cases.append(("timestamp_context", asr, vad, True))
                if scale < 1:
                    cases.append(("quiet_enhancement", asr, vad, False))
                if args.only_quiet_enhancement:
                    cases = [case for case in cases if case[0] == "quiet_enhancement"]
                for variant, decoder, segmenter, context in cases:
                    if any(old["model"] == model and old["file"] == key and old["scale"] == scale
                           and old["seconds"] == len(audio) / rate and old["variant"] == variant for old in report["runs"]):
                        continue
                    result = run(rec, audio, reference, decoder, segmenter, context=context,
                                 quiet=variant == "quiet_enhancement")
                    result.update(model=model, file=key, scale=scale, seconds=len(audio) / rate,
                                  variant=variant, speakers=len({u.speaker for u in labels if u.start_ms < len(audio) * 1000 / rate}))
                    report["runs"].append(result)
                    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                    print(json.dumps({k: v for k, v in result.items() if k not in {"hypothesis", "bounds"}}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

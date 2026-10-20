"""Score speaker timelines against independently labeled RTTM reference audio.

Manifest: {"recordings": [{"audio": "sample.wav", "rttm": "sample.rttm"}]}.
``--segmenter fixed`` (default) feeds 15-second chunks and keeps exact file
times, evaluating the speaker pipeline alone. ``--segmenter silero`` runs the
meeting VAD the app uses, so segment edges and the end-of-session re-assignment
(``finalized``) are scored exactly as a meeting would be. No ASR.

Two scores per timeline. ``score_timeline`` splits single-speaker time into
correct / wrong / unknown and reports overlap separately, never as a correct
attribution. ``score_der`` is the standard diarization error rate (10 ms
frames, 250 ms collar around reference boundaries, one-to-one speaker
mapping) with turn attribution by reference-turn length; unknown counts as
missed speech there too.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import sys
import time

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sayink.audio_utils import TARGET_SAMPLE_RATE, resample_mono
from sayink.meeting_speakers import MeetingSpeakerSession
from sayink.recognition_settings import aux_model_path
from sayink.speaker_clustering import reassign_turns
from sayink.speaker_embedding import make_embedder
from sayink.speaker_turns import MAX_ANALYSIS_SEC, SpeakerTurnDetector, analyze_turns
from sayink.speech_recognizer import AUX_MODEL_SILERO_VAD, AUX_MODEL_SPEAKER
from sayink.vad_segmenter import VAD_SILERO, make_meeting_segmenter
from tools.asr_bench import load_wav

SEGMENTER_FIXED = "fixed"
SEGMENTER_SILERO = "silero"
FRAME_MS = 10
DER_COLLAR_MS = 250
# Reference segments of one speaker closer than this form one turn.
TURN_GAP_MS = 300
TURN_BUCKETS = ((800, "<0.8s"), (1600, "<1.6s"), (3000, "<3s"), (math.inf, ">=3s"))
UNKNOWN_LABELS = ("0", "", "unknown")


def read_rttm(path: Path, *, uri=None):
    rows, names = [], set()
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        cells = line.split()
        if len(cells) != 10 or cells[0] != "SPEAKER":
            raise ValueError("RTTM requires ten-column SPEAKER records")
        names.add(cells[1])
        if uri is not None and cells[1] != uri:
            continue
        start, duration = float(cells[3]) * 1000, float(cells[4]) * 1000
        if not math.isfinite(start) or not math.isfinite(duration) or start < 0 or duration <= 0:
            raise ValueError("invalid RTTM time")
        rows.append((start, start + duration, cells[7]))
    if not rows or (uri is None and len(names) != 1):
        raise ValueError("RTTM must contain one recording, or select its uri explicitly")
    return rows


def _mapping(weights):
    predictions = sorted(weights)
    people = sorted({g for row in weights.values() for g in row})
    if max(len(predictions), len(people)) > 12:
        raise ValueError("timeline scoring supports at most twelve speaker groups")
    # Maximum-duration one-to-one identity permutation. Unmatched groups
    # remain wrong instead of being mapped onto an already matched person.
    states = {0: (0.0, ())}
    for pred in predictions:
        following = {}
        for mask, (score, chosen) in states.items():
            options = [(mask, score, (*chosen, None))]
            for index, person in enumerate(people):
                if not mask & (1 << index):
                    options.append((mask | (1 << index), score + weights[pred].get(person, 0), (*chosen, person)))
            for new_mask, total, labels in options:
                if new_mask not in following or total > following[new_mask][0]:
                    following[new_mask] = (total, labels)
        states = following
    _total, labels = max(states.values(), key=lambda item: item[0])
    return dict(zip(predictions, labels, strict=True))


def score_timeline(reference, predicted, *, duration_ms):
    if not math.isfinite(duration_ms) or duration_ms <= 0:
        raise ValueError("duration must be finite and positive")
    events = defaultdict(list)
    for kind, rows in ((0, reference), (1, predicted)):
        for start, stop, label in rows:
            if not all(math.isfinite(v) for v in (start, stop)) or not 0 <= start < stop <= duration_ms:
                raise ValueError("timeline intervals must be inside the audio")
            events[start].append((kind, str(label), 1))
            events[stop].append((kind, str(label), -1))
    edges = sorted({0, duration_ms, *events})
    active = [Counter(), Counter()]
    spans = []
    weights = defaultdict(lambda: defaultdict(float))
    single = assigned = overlap = overlap_assigned = false_alarm = 0.0
    for start, stop in zip(edges, edges[1:], strict=False):
        for kind, label, delta in events[start]:
            active[kind][label] += delta
            if active[kind][label] == 0:
                del active[kind][label]
        if sum(active[1].values()) > 1:
            raise ValueError("predicted single-speaker intervals cannot overlap")
        gold = set(active[0])
        pred = next(iter(active[1]), "0")
        known = pred not in ("0", "", "unknown")
        elapsed = stop - start
        spans.append((start, stop, gold, pred if known else None))
        if len(gold) == 1:
            single += elapsed
            if known:
                assigned += elapsed
                weights[pred][next(iter(gold))] += elapsed
        elif len(gold) > 1:
            overlap += elapsed
            overlap_assigned += elapsed if known else 0
        elif known:
            false_alarm += elapsed
    mapping = _mapping(weights)
    correct = sum(row.get(mapping[pred], 0) for pred, row in weights.items())
    short = recovered = 0
    for begin, end, person in reference:
        if end - begin > 1500:
            continue
        solo = right = 0.0
        for a, b, gold, pred in spans:
            amount = max(0, min(b, end) - max(a, begin))
            if gold == {str(person)}:
                solo += amount
                if pred is not None and mapping.get(pred) == str(person):
                    right += amount
        if solo > 0:
            short += 1
            recovered += right >= solo / 2
    return {
        "single_speaker_ms": single, "overlap_ms": overlap,
        "assigned_single_ms": assigned, "correct_single_ms": correct,
        "wrong_single_ms": assigned - correct, "unknown_single_ms": single - assigned,
        "single_coverage": assigned / single if single else None,
        "single_correct_rate": correct / single if single else None,
        "single_precision": correct / assigned if assigned else None,
        "overlap_assigned_ms": overlap_assigned, "overlap_unknown_ms": overlap - overlap_assigned,
        "non_speech_assigned_ms": false_alarm,
        "short_turns": short, "short_turns_recovered": recovered,
        "short_turns_missed": short - recovered,
        "merged_groups": sum(len(row) > 1 for row in weights.values()),
        "split_people": sum(sum(person in row for row in weights.values()) > 1
                            for person in {g for row in weights.values() for g in row}),
        "mapping": mapping,
    }


def _frame_activity(rows, labels, frames):
    index = {label: i for i, label in enumerate(labels)}
    active = np.zeros((frames, len(labels)), dtype=bool)
    for start, stop, label in rows:
        a, b = int(round(start / FRAME_MS)), int(round(stop / FRAME_MS))
        active[max(0, a):min(frames, b), index[str(label)]] = True
    return active


def reference_turns(reference, *, gap_ms=TURN_GAP_MS):
    turns = []
    for start, stop, label in sorted((s, e, str(l)) for s, e, l in reference):
        if turns and turns[-1][2] == label and start - turns[-1][1] < gap_ms:
            turns[-1] = (turns[-1][0], max(turns[-1][1], stop), label)
        else:
            turns.append((start, stop, label))
    return turns


def score_der(reference, predicted, *, duration_ms, collar_ms=DER_COLLAR_MS):
    """Diarization error rate plus turn attribution.

    Frames of ``FRAME_MS`` within ``collar_ms`` of a reference boundary are
    not scored. Predicted ids map one-to-one onto reference speakers by
    maximal co-activity; a predicted id left without a person is wrong
    everywhere. Unknown predictions are missed speech. ``turns`` counts, per
    reference-turn length, whether the majority predicted id over the turn
    maps to its speaker (``missed`` when no frame carries an id at all).
    """
    if not math.isfinite(duration_ms) or duration_ms <= 0:
        raise ValueError("duration must be finite and positive")
    for rows in (reference, predicted):
        for start, stop, _label in rows:
            if not all(math.isfinite(v) for v in (start, stop)) or not 0 <= start < stop <= duration_ms:
                raise ValueError("timeline intervals must be inside the audio")
    frames = int(math.ceil(duration_ms / FRAME_MS)) + 1
    people = sorted({str(l) for _, _, l in reference})
    known = [(s, e, str(l)) for s, e, l in predicted if str(l) not in UNKNOWN_LABELS]
    ids = sorted({l for _, _, l in known}, key=lambda x: (not x.isdigit(), int(x) if x.isdigit() else x))
    gold = _frame_activity(reference, people, frames)
    hyp = _frame_activity(known, ids, frames)
    scored = np.ones(frames, dtype=bool)
    collar = int(round(collar_ms / FRAME_MS))
    for start, stop, _label in reference:
        for edge in (start, stop):
            f = int(round(edge / FRAME_MS))
            scored[max(0, f - collar):min(frames, f + collar)] = False
    g, h = gold[scored], hyp[scored]
    co = g.astype(np.int64).T @ h.astype(np.int64) if people and ids else np.zeros((len(people), len(ids)), dtype=np.int64)
    weights = {pred: {person: float(co[i, j]) for i, person in enumerate(people) if co[i, j] > 0}
               for j, pred in enumerate(ids)}
    mapping = _mapping(weights) if weights else {}
    ref_count, hyp_count = g.sum(axis=1), h.sum(axis=1)
    correct = np.zeros(g.shape[0], dtype=np.int64)
    for j, pred in enumerate(ids):
        person = mapping.get(pred)
        if person is not None:
            correct += (g[:, people.index(person)] & h[:, j]).astype(np.int64)
    total = float(ref_count.sum())
    miss = float(np.maximum(0, ref_count - hyp_count).sum())
    false_alarm = float(np.maximum(0, hyp_count - ref_count).sum())
    confusion = float((np.minimum(ref_count, hyp_count) - correct).sum())
    buckets = {name: {"turns": 0, "correct": 0, "wrong": 0, "missed": 0} for _limit, name in TURN_BUCKETS}
    for start, stop, person in reference_turns(reference):
        name = next(name for limit, name in TURN_BUCKETS if stop - start < limit)
        window = hyp[int(round(start / FRAME_MS)):int(round(stop / FRAME_MS))]
        buckets[name]["turns"] += 1
        if window.size == 0 or not window.any():
            buckets[name]["missed"] += 1
            continue
        majority = ids[int(np.argmax(window.sum(axis=0)))]
        buckets[name]["correct" if mapping.get(majority) == person else "wrong"] += 1
    return {
        "der": (miss + false_alarm + confusion) / total if total else None,
        "miss": miss / total if total else None,
        "false_alarm": false_alarm / total if total else None,
        "confusion": confusion / total if total else None,
        "collar_ms": collar_ms, "frame_ms": FRAME_MS,
        "reference_speakers": len(people), "predicted_speakers": len(ids),
        "mapping": mapping,
        "turns": buckets,
    }


def fixed_segments(audio):
    """15-second chunks at exact file offsets (no VAD)."""
    chunk = int(MAX_ANALYSIS_SEC * TARGET_SAMPLE_RATE)
    for offset in range(0, len(audio), chunk):
        yield offset, audio[offset:offset + chunk]


def silero_segments(audio, *, model_path=None):
    """The meeting VAD exactly as the recorder runs it, with file offsets."""
    segmenter = make_meeting_segmenter(vad=VAD_SILERO, model_path=model_path or aux_model_path(AUX_MODEL_SILERO_VAD))
    if segmenter.gate_name != VAD_SILERO:
        raise ValueError("Silero VAD model is required for --segmenter silero")
    block = TARGET_SAMPLE_RATE // 10
    for start in range(0, len(audio), block):
        piece = segmenter.feed(audio[start:start + block])
        if piece is not None and piece.size:
            yield segmenter.last_bounds[0], piece
    tail = segmenter.flush()
    if tail is not None and tail.size:
        yield segmenter.last_bounds[0], tail


def evaluate_recordings(manifest: Path, embedder, *, contextual=True, segmenter=SEGMENTER_FIXED, finalize=True):
    payload = json.loads(manifest.read_text(encoding="utf-8-sig"))
    items = payload.get("recordings") if isinstance(payload, dict) else None
    if not isinstance(items, list) or not items:
        raise ValueError("manifest needs nonempty recordings")
    if segmenter not in (SEGMENTER_FIXED, SEGMENTER_SILERO):
        raise ValueError("segmenter must be fixed or silero")
    activity_backend = "off"
    files = []
    for item in items:
        path = manifest.parent / item["audio"]
        audio, rate = load_wav(path)
        audio = resample_mono(audio, rate)
        duration = len(audio) * 1000 / TARGET_SAMPLE_RATE
        reference = read_rttm(manifest.parent / item["rttm"], uri=item.get("uri"))
        session = MeetingSpeakerSession.for_embedder(embedder, keep_history=finalize)
        predicted, seq = [], 0
        tick = time.perf_counter()
        pieces = fixed_segments(audio) if segmenter == SEGMENTER_FIXED else silero_segments(audio)
        for offset, clip in pieces:
            detector = (SpeakerTurnDetector(embedder, context=session.turn_context,
                                            start_ms=int(offset * 1000 / TARGET_SAMPLE_RATE), route="timeline")
                        if contextual else SpeakerTurnDetector(embedder))
            activity_backend = detector.activity_backend
            for turn in analyze_turns(clip, detector):
                start, stop = (offset + turn.start) * 1000 / TARGET_SAMPLE_RATE, (offset + turn.stop) * 1000 / TARGET_SAMPLE_RATE
                speaker = session.assign(turn.embedding, start_ms=int(start), end_ms=int(stop),
                                         sample_key=seq, learnable=turn.learnable)
                predicted.append((start, stop, str(speaker)))
                seq += 1
        elapsed = time.perf_counter() - tick
        entry = {"audio": str(path), "audio_ms": duration, "analysis_s": elapsed,
                 "predicted": predicted, **score_timeline(reference, predicted, duration_ms=duration),
                 "der": score_der(reference, predicted, duration_ms=duration)}
        if finalize:
            # What the app does when the meeting ends (meeting_session
            # _apply_recluster): with two or more speakers, cluster every turn
            # and re-assign; a turn the clustering abstains on, and every turn
            # of a one-speaker recording, keeps its live label.
            turns = [t for t in session.turns if t.key >= 0] if session.speaker_count >= 2 else []
            assignments = reassign_turns(turns, threshold=embedder.cluster_threshold) if turns else {}
            final = [(start, stop, str(assignments.get(seq, live))) for seq, (start, stop, live) in enumerate(predicted)]
            # ``relabelled`` counts every number that changed, renumbering included.
            entry["finalized"] = {"predicted": final,
                                  "relabelled": sum(a[2] != b[2] for a, b in zip(predicted, final, strict=True)),
                                  **score_timeline(reference, final, duration_ms=duration),
                                  "der": score_der(reference, final, duration_ms=duration)}
        files.append(entry)
    scope = ("Fixed chunks, speaker pipeline only; no VAD or ASR. "
             "Timeline score has no collar and lists overlap separately; `der` is the collared diarization error rate."
             if segmenter == SEGMENTER_FIXED else
             "Meeting VAD segments and speaker pipeline; no ASR. "
             "Timeline score has no collar and lists overlap separately; `der` is the collared diarization error rate.")
    return {"speaker_backend": embedder.name, "activity_backend": activity_backend, "contextual": contextual,
            "segmenter": segmenter, "chunk_s": MAX_ANALYSIS_SEC if segmenter == SEGMENTER_FIXED else None,
            "finalize": finalize, "scope": scope, "files": files}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--speaker-activity", choices=("off", "auto"), default="off")
    parser.add_argument("--context", choices=("on", "off"), default="on")
    parser.add_argument("--segmenter", choices=(SEGMENTER_FIXED, SEGMENTER_SILERO), default=SEGMENTER_FIXED)
    parser.add_argument("--finalize", choices=("on", "off"), default="on",
                        help="also score the end-of-session re-assignment the app applies when a meeting ends")
    args = parser.parse_args(argv)
    try:
        embedder = make_embedder(aux_model_path(AUX_MODEL_SPEAKER), activity_enabled=args.speaker_activity == "auto")
        if not embedder.supports_turn_detection:
            raise ValueError("CAM++ must be loaded for timeline evaluation")
        output = json.dumps(evaluate_recordings(args.manifest, embedder, contextual=args.context == "on",
                                                segmenter=args.segmenter, finalize=args.finalize == "on"),
                            ensure_ascii=False, indent=2)
        print(output)
        if args.out:
            args.out.write_text(output, encoding="utf-8")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

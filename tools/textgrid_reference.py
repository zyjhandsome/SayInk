"""Turn a Praat TextGrid transcript into the bench inputs this repo scores.

Public meeting corpora (AISHELL-4 and others) ship one IntervalTier per
speaker with the words of each utterance. ``asr_bench.py`` wants one
``name.txt`` reference next to ``name.wav``; ``meeting_timeline_bench.py``
wants an RTTM. This writes either from the same TextGrid, so a corpus can be
scored without hand-made files.

Reference text is every non-empty interval in time order across all tiers
with ``<...>`` markers (``<sil>``, noise tags) removed. During overlap the
words of every speaker are included, so far-field CER is an upper bound on
the error of any one voice. ``--until`` keeps only utterances starting before
that many seconds, for scoring an excerpt cut from the start of the file.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import re
import sys

_TAG = re.compile(r"<[^>]*>")
_NAME = re.compile(r'^\s*name\s*=\s*"(.*)"\s*$')
_XMIN = re.compile(r"^\s*xmin\s*=\s*([-+0-9.eE]+)\s*$")
_XMAX = re.compile(r"^\s*xmax\s*=\s*([-+0-9.eE]+)\s*$")
_TEXT = re.compile(r'^\s*text\s*=\s*"(.*)"\s*$')


@dataclass(frozen=True)
class Utterance:
    start_ms: float
    end_ms: float
    speaker: str
    text: str


def read_textgrid(path: Path) -> list[Utterance]:
    """Non-empty intervals of every IntervalTier, sorted by start then tier."""
    tier = None
    start = end = None
    out: list[Utterance] = []
    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if (m := _NAME.match(raw)):
            tier = m.group(1)
            start = end = None
            continue
        if (m := _XMIN.match(raw)):
            start = float(m.group(1))
            continue
        if (m := _XMAX.match(raw)):
            end = float(m.group(1))
            continue
        if (m := _TEXT.match(raw)) is None:
            continue
        text = _TAG.sub("", m.group(1).replace('""', '"')).strip()
        if tier is None or start is None or end is None:
            raise ValueError(f"{path.name}: interval text before its tier or bounds")
        if text and end > start:
            out.append(Utterance(start * 1000, end * 1000, tier, text))
        start = end = None
    if not out:
        raise ValueError(f"{path.name}: no transcribed intervals found")
    return sorted(out, key=lambda u: (u.start_ms, u.speaker))


def reference_text(utterances: list[Utterance], *, until_ms: float | None = None) -> str:
    return "".join(u.text for u in utterances if until_ms is None or u.start_ms < until_ms)


def rttm_lines(utterances: list[Utterance], uri: str, *, until_ms: float | None = None) -> list[str]:
    lines = []
    for u in utterances:
        if until_ms is not None and u.start_ms >= until_ms:
            continue
        stop = u.end_ms if until_ms is None else min(u.end_ms, until_ms)
        lines.append(f"SPEAKER {uri} 1 {u.start_ms / 1000:.3f} {(stop - u.start_ms) / 1000:.3f} "
                     f"<NA> <NA> {u.speaker} <NA> <NA>")
    return lines


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("textgrid", type=Path)
    parser.add_argument("--until", type=float, default=None, help="seconds; keep utterances starting before this")
    parser.add_argument("--out", type=Path, default=None, help="reference text file (default: <stem>.txt beside the TextGrid)")
    parser.add_argument("--rttm", type=Path, default=None, help="also write an RTTM with the tier names as speakers")
    parser.add_argument("--uri", default=None, help="recording id for the RTTM (default: TextGrid stem)")
    args = parser.parse_args(argv)
    try:
        utterances = read_textgrid(args.textgrid)
        until_ms = args.until * 1000 if args.until is not None else None
        out = args.out or args.textgrid.with_suffix(".txt")
        out.write_text(reference_text(utterances, until_ms=until_ms), encoding="utf-8")
        print(f"{out}: {sum(1 for u in utterances if until_ms is None or u.start_ms < until_ms)} utterances, "
              f"{len({u.speaker for u in utterances})} tiers")
        if args.rttm:
            lines = rttm_lines(utterances, args.uri or args.textgrid.stem, until_ms=until_ms)
            args.rttm.write_text("\n".join(lines) + "\n", encoding="utf-8")
            print(f"{args.rttm}: {len(lines)} segments")
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

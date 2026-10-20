"""Score speaker split / merge from a hand-labeled meeting transcript.

Each row is one recognized segment. Two columns (``真人``, ``预测``) or three
(``序号``, ``真人``, ``预测``), tab- or comma-separated. Lines starting with
``#`` are comments.

    py -3.10 tools/speaker_eval.py labels.tsv

A person is split when their segments carry more than one predicted id.
A predicted id is a merge when it contains more than one person. Rates are
those counts divided by the number of people and predicted ids. An empty
file has no rate. This does not load audio or the speaker model.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

_HEADER_CELLS = frozenset({
    "gold", "pred", "speaker", "id",
    "真人", "预测", "说话人", "编号", "序号", "segment", "index",
})
_UNKNOWN_LABELS = frozenset({"0", "unknown", "待确认", "说话人待确认"})


def score_speakers(rows: list[tuple[str, str]]) -> dict:
    """``rows`` are ``(gold_speaker, predicted_id)`` for one meeting."""
    by_gold: dict[str, set[str]] = defaultdict(set)
    by_pred: dict[str, set[str]] = defaultdict(set)
    kept = 0
    unknown = 0
    people: set[str] = set()
    for gold, pred in rows:
        person, label = str(gold).strip(), str(pred).strip()
        if not person or not label:
            continue
        kept += 1
        people.add(person)
        if label.lower() in _UNKNOWN_LABELS:
            unknown += 1
            continue
        by_gold[person].add(label)
        by_pred[label].add(person)
    split_names = sorted(person for person, labels in by_gold.items() if len(labels) > 1)
    merged_ids = sorted(label for label, people in by_pred.items() if len(people) > 1)
    n_gold, n_pred = len(people), len(by_pred)
    return {
        "segments": kept,
        "unknown_segments": unknown,
        "unknown_rate": unknown / kept if kept else None,
        "coverage": (kept - unknown) / kept if kept else None,
        "gold_speakers": n_gold,
        "pred_ids": n_pred,
        "split_speakers": len(split_names),
        "split_rate": (len(split_names) / n_gold) if n_gold else None,
        "split_names": split_names,
        "merge_ids": len(merged_ids),
        "merge_rate": (len(merged_ids) / n_pred) if n_pred else None,
        "merged_pred_ids": merged_ids,
    }


def _cells(line: str) -> list[str]:
    if "\t" in line:
        return [part.strip() for part in line.split("\t")]
    return [part.strip() for part in line.split(",")]


def parse_label_text(text: str) -> list[tuple[str, str]]:
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    start = 0
    if lines and any(cell.lower() in _HEADER_CELLS for cell in _cells(lines[0])):
        start = 1
    rows: list[tuple[str, str]] = []
    for line in lines[start:]:
        cells = _cells(line)
        if len(cells) == 2:
            gold, pred = cells
        elif len(cells) == 3:
            _index, gold, pred = cells
        else:
            raise ValueError(f"需要 2 列（真人、预测）或 3 列（序号、真人、预测）: {line}")
        if gold and pred:
            rows.append((gold, pred))
    return rows


def format_report(scored: dict) -> str:
    def _pct(rate: float | None, numerator: int, denominator: int) -> str:
        if rate is None:
            return "—"
        return f"{numerator}/{denominator} ({rate:.0%})"

    return (
        f"片段 {scored['segments']} · 真人 {scored['gold_speakers']} · "
        f"预测编号 {scored['pred_ids']} · "
        f"拆人 {_pct(scored['split_rate'], scored['split_speakers'], scored['gold_speakers'])} · "
        f"并人 {_pct(scored['merge_rate'], scored['merge_ids'], scored['pred_ids'])} · "
        f"待确认 {_pct(scored['unknown_rate'], scored['unknown_segments'], scored['segments'])}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("labels", type=Path, help="TSV/CSV：真人与预测编号，可带序号列")
    args = parser.parse_args(argv)
    try:
        rows = parse_label_text(args.labels.read_text(encoding="utf-8-sig"))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(format_report(score_speakers(rows)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

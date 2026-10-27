"""Parse the model's JSON and render it as Markdown. Pure functions.

Guardrails (docs/meeting-notes-design.md §4.5):
* a ``ts`` outside the meeting's duration is dropped, the text kept;
* dates / numbers / names in action items and decisions that do not occur in
  the transcript are flagged 「待核对」 rather than silently removed;
* a missing section renders as 「本场未识别到」 instead of failing.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Mapping, Sequence

from sayink.minutes_prompts import (
    ALL_SECTIONS,
    SECTION_ACTION_ITEMS,
    SECTION_CHAPTERS,
    SECTION_DECISIONS,
    SECTION_OPEN_QUESTIONS,
    SECTION_SPEAKERS,
    SECTION_SUMMARY,
    SECTION_TITLES,
    SECTION_TOPICS,
    MinutesTemplate,
    parse_timestamp,
    speaker_label,
)

EMPTY_SECTION_TEXT = "本场未识别到"
UNVERIFIED_MARK = "（待核对）"
_SPEAKER_LABEL_RE = re.compile(r"说话人\s*(\d+)")
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


def extract_json(text: str) -> dict[str, Any] | None:
    """``json.loads`` first; else the first balanced ``{…}`` in the text."""
    if not text:
        return None
    candidate = _FENCE_RE.sub("", text.strip())
    # RecursionError: absurdly nested brackets from a broken endpoint must not
    # take the minutes worker down with them; they are simply "not JSON".
    try:
        data = json.loads(candidate)
        return data if isinstance(data, dict) else None
    except (ValueError, RecursionError):
        pass
    if "}" not in candidate:
        return None
    start = candidate.find("{")
    tries = 0
    while start != -1 and tries < 32:
        tries += 1
        depth = 0
        in_string = False
        escape = False
        for index in range(start, len(candidate)):
            ch = candidate[index]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(candidate[start:index + 1])
                    except (ValueError, RecursionError):
                        break
                    return data if isinstance(data, dict) else None
        start = candidate.find("{", start + 1)
    return None


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _ts(value: Any, duration_ms: int | None) -> str:
    """Render a timestamp as ``[hh:mm:ss]`` when valid and inside the meeting."""
    text = _as_text(value)
    ms = parse_timestamp(text)
    if ms is None:
        return ""
    if duration_ms is not None and duration_ms > 0 and ms > duration_ms + 60_000:
        return ""
    return f"[{text}]"


_CHECKABLE_RE = re.compile(
    r"\d{4}[-/年]\d{1,2}([-/月]\d{1,2}日?)?"       # 2026-03-01 / 2026年3月1日
    r"|\d{1,2}月\d{1,2}日"                         # 3月1日
    r"|(?:周|星期|礼拜)[一二三四五六日天]"           # 周三
    r"|\d+(?:\.\d+)?\s*(?:%|万|亿|千|百|人|天|周|个月|小时|分钟|元|块|美元|k|K|w|W)"  # amounts
)
_CJK_NUMERALS = "零一二两三四五六七八九十百千万亿"


def _normalize_for_lookup(text: str) -> str:
    return re.sub(r"[\s,，。、；;:：]", "", text or "")


def flag_unverified(text: str, transcript_text: str) -> str:
    """Append 「待核对」 when a date / amount in ``text`` is absent from the transcript."""
    if not text or not transcript_text:
        return text
    haystack = _normalize_for_lookup(transcript_text)
    for match in _CHECKABLE_RE.finditer(text):
        needle = _normalize_for_lookup(match.group(0))
        if needle and needle not in haystack:
            return text + UNVERIFIED_MARK
    return text


def apply_speaker_names(text: str, names: Mapping[int, str] | None) -> str:
    """Replace 「说话人N」 with a manual or source-validated display name."""
    if not names or not text:
        return text

    def _swap(match: re.Match) -> str:
        sid = int(match.group(1))
        name = (names.get(sid) or "").strip()
        return name or match.group(0)

    return _SPEAKER_LABEL_RE.sub(_swap, text)


def _canonical_speaker_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Keep generated literal names connected to stable speaker numbers.

    The worker records its own name snapshot, never a model's identity guess.
    Canonicalise before rendering so later corrections and even name swaps do
    not get undone by old literal names embedded in the stored JSON.
    """
    snapshot = payload.get("_speaker_names")
    if not isinstance(snapshot, dict):
        return payload
    owners: dict[str, set[int]] = {}
    for sid, name in snapshot.items():
        if str(sid).isdigit() and int(sid) > 0 and isinstance(name, str) and name.strip():
            owners.setdefault(name.strip(), set()).add(int(sid))
    aliases = {name: speaker_label(next(iter(ids))) for name, ids in owners.items() if len(ids) == 1}
    if not aliases:
        return payload
    patterns = []
    for name in sorted(aliases, key=len, reverse=True):
        head, tail = _name_bounds(name)
        patterns.append(head + re.escape(name) + tail)
    pattern = re.compile("|".join(patterns))

    def canonical(value):
        if isinstance(value, str):
            return pattern.sub(lambda match: aliases[match.group()], value)
        if isinstance(value, list):
            return [canonical(item) for item in value]
        if isinstance(value, dict):
            return {key: canonical(item) for key, item in value.items() if key != "_speaker_names"}
        return value

    return canonical(dict(payload))


def render_markdown(
    payload: Mapping[str, Any] | None,
    *,
    template: MinutesTemplate,
    title_fallback: str = "会议纪要",
    duration_ms: int | None = None,
    transcript_text: str = "",
    speaker_names: Mapping[int, str] | None = None,
    meta_lines: Sequence[str] = (),
) -> str:
    """Render the structured minutes; missing sections become placeholders."""
    if template.is_refinement:
        from sayink.transcript_refiner import render_refinement

        # Refined prose is near-verbatim speech: a name in it is content
        # (「张强这个月回款才38万」), not a speaker label, so it must not be
        # rewritten when a speaker who happens to share the name is renamed.
        return apply_speaker_names(render_refinement(payload or {}), speaker_names)
    payload = _canonical_speaker_payload(payload or {})
    title = _as_text(payload.get("title")) or title_fallback
    out: list[str] = [f"# {apply_speaker_names(title, speaker_names)}"]
    if meta_lines:
        out.append("")
        out.extend(f"- {line}" for line in meta_lines if line)

    def section(key: str) -> None:
        out.append("")
        out.append(f"## {SECTION_TITLES[key]}")
        body = _render_section(key, payload, duration_ms, transcript_text, speaker_names)
        out.append(body if body else EMPTY_SECTION_TEXT)

    for key in template.sections:
        if key in ALL_SECTIONS:
            section(key)
    return "\n".join(out).rstrip() + "\n"


def _render_topics_section(
    payload: Mapping[str, Any],
    duration_ms: int | None,
    fix: Callable[[str], str],
) -> str:
    lines: list[str] = []
    for topic in _as_list(payload.get("topics")):
        if not isinstance(topic, dict):
            continue
        heading = fix(_as_text(topic.get("title")))
        points = [p for p in _as_list(topic.get("points")) if isinstance(p, dict) and _as_text(p.get("text"))]
        if not heading and not points:
            continue
        if heading:
            lines.append(f"### {heading}")
        for point in points:
            ts = _ts(point.get("ts"), duration_ms)
            lines.append(f"- {fix(_as_text(point.get('text')))}{(' ' + ts) if ts else ''}")
    return "\n".join(lines)


def _render_bullet_section(
    key: str,
    payload: Mapping[str, Any],
    duration_ms: int | None,
    transcript_text: str,
    fix: Callable[[str], str],
) -> str:
    lines = []
    for item in _as_list(payload.get(key)):
        text = _as_text(item.get("text")) if isinstance(item, dict) else _as_text(item)
        if not text:
            continue
        if key == SECTION_DECISIONS:
            text = flag_unverified(text, transcript_text)
        ts = _ts(item.get("ts"), duration_ms) if isinstance(item, dict) else ""
        lines.append(f"- {fix(text)}{(' ' + ts) if ts else ''}")
    return "\n".join(lines)


def _render_action_items_section(
    payload: Mapping[str, Any],
    duration_ms: int | None,
    transcript_text: str,
    fix: Callable[[str], str],
) -> str:
    lines = []
    for item in _as_list(payload.get("action_items")):
        if not isinstance(item, dict):
            continue
        task = _as_text(item.get("task"))
        if not task:
            continue
        task = flag_unverified(task, transcript_text)
        owner = fix(_as_text(item.get("owner"))) or "未指定"
        due = _as_text(item.get("due")) or "未提及"
        if due != "未提及":
            due = flag_unverified(due, transcript_text)
        ts = _ts(item.get("ts"), duration_ms)
        lines.append(f"- [ ] {fix(task)} — @{owner} · {due}{(' ' + ts) if ts else ''}")
    return "\n".join(lines)


def _render_chapters_section(
    payload: Mapping[str, Any],
    duration_ms: int | None,
    fix: Callable[[str], str],
) -> str:
    lines = []
    for chapter in _as_list(payload.get("chapters")):
        if not isinstance(chapter, dict):
            continue
        heading = fix(_as_text(chapter.get("title")))
        if not heading:
            continue
        start = _ts(chapter.get("start"), duration_ms)
        end = _ts(chapter.get("end"), duration_ms)
        span = f"{start} – {end} " if (start and end) else (f"{start} " if start else "")
        summary = fix(_as_text(chapter.get("summary")))
        lines.append(f"- {span}**{heading}**{('：' + summary) if summary else ''}")
    return "\n".join(lines)


def _render_speakers_section(
    payload: Mapping[str, Any],
    duration_ms: int | None,
    names: Mapping[int, str] | None,
    fix: Callable[[str], str],
) -> str:
    lines = []
    for speaker in _as_list(payload.get("speakers")):
        if not isinstance(speaker, dict):
            continue
        label = _as_text(speaker.get("label"))
        summary = fix(_as_text(speaker.get("summary")))
        if not label and not summary:
            continue
        match = _SPEAKER_LABEL_RE.search(label)
        if match:
            label = speaker_label(int(match.group(1)), dict(names) if names else None)
        suggested = _as_text(speaker.get("suggested_name"))
        if suggested.lower() in ("null", "none"):
            suggested = ""
        hint = ""
        if suggested and suggested != label and not (match and names and int(match.group(1)) in names):
            ts = _ts(speaker.get("evidence_ts"), duration_ms)
            hint = f"（可能是 {suggested}{(' ' + ts) if ts else ''}）"
        lines.append(f"- **{label}**{hint}：{summary or EMPTY_SECTION_TEXT}")
    return "\n".join(lines)


def _render_section(
    key: str,
    payload: Mapping[str, Any],
    duration_ms: int | None,
    transcript_text: str,
    names: Mapping[int, str] | None,
) -> str:
    def fix(text: str) -> str:
        return apply_speaker_names(text, names)

    if key == SECTION_SUMMARY:
        return fix(_as_text(payload.get("summary")))
    if key == SECTION_TOPICS:
        return _render_topics_section(payload, duration_ms, fix)
    if key in (SECTION_DECISIONS, SECTION_OPEN_QUESTIONS):
        return _render_bullet_section(key, payload, duration_ms, transcript_text, fix)
    if key == SECTION_ACTION_ITEMS:
        return _render_action_items_section(payload, duration_ms, transcript_text, fix)
    if key == SECTION_CHAPTERS:
        return _render_chapters_section(payload, duration_ms, fix)
    if key == SECTION_SPEAKERS:
        return _render_speakers_section(payload, duration_ms, names, fix)
    return ""


def _name_bounds(label: str) -> tuple[str, str]:
    """Don't let a short label rewrite a longer token.

    ``说话人2`` stops before ``说话人21``. A Chinese name may be followed by
    a verb (负责、审核) but not by another character that lengthens the name
    (``王总`` must not hit ``王总监``).
    """
    head = r"(?<![0-9A-Za-z])" if re.match(r"[0-9A-Za-z]", label) else ""
    tail = r"(?![0-9A-Za-z])" if re.search(r"[0-9A-Za-z]$", label) else ""
    if re.search(r"[\u4e00-\u9fff]", label):
        head = head or r"(?<![\u4e00-\u9fff])"
        following = (
            r"(?:(?![\u4e00-\u9fff])|(?=[负责审核补充确认说明汇报参加记录整理总结"
            r"提出建议决定安排介绍解释指出表示问道说讲的了着过和与及并而把被在从对向给]))"
        )
        tail = f"(?![0-9A-Za-z]){following}" if tail else following
    return head, tail


def replace_names_in_markdown(markdown: str, old_label: str, new_label: str) -> tuple[str, int]:
    """Whole-label replacement for user-edited (free text) notes; returns the
    new text and how many places changed."""
    if not old_label or not new_label or old_label == new_label:
        return markdown, 0
    head, tail = _name_bounds(old_label)
    pattern = re.compile(head + re.escape(old_label) + tail)
    new_text, count = pattern.subn(new_label, markdown)
    return new_text, count

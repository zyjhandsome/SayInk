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
    SECTION_TOPICS,
    MinutesTemplate,
    section_title,
    parse_timestamp,
    speaker_label,
)
from sayink.text_polisher import strip_model_thinking

EMPTY_SECTION_TEXT = "本场未识别到"
UNVERIFIED_MARK = "（待核对）"
_SPEAKER_LABEL_RE = re.compile(r"说话人\s*(\d+)")
_SPEAKER_LABEL_FULL_RE = re.compile(r"^说话人\s*(\d+)$")
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


def extract_json(text: str) -> dict[str, Any] | None:
    """``json.loads`` first; else the first balanced ``{…}`` in the text."""
    if not text:
        return None
    stripped = strip_model_thinking(text).strip()
    if not stripped:
        return None
    candidate = _FENCE_RE.sub("", stripped)
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


def clear_timeline(payload: dict) -> dict:
    """Blank clocks a model invented for a transcript that has none."""
    for key in ("topics", "decisions", "action_items", "open_questions", "speakers"):
        for item in payload.get(key) or []:
            if not isinstance(item, dict):
                continue
            for field in ("ts", "evidence_ts"):
                if field in item:
                    item[field] = ""
            for point in item.get("points") or []:
                if isinstance(point, dict) and "ts" in point:
                    point["ts"] = ""
    for chapter in payload.get("chapters") or []:
        if isinstance(chapter, dict):
            chapter["start"] = ""
            chapter["end"] = ""
    return payload


_CLOCK_CITATION = re.compile(r"\[\d{1,2}:\d{2}:\d{2}\]")


def strip_clock_citations(text: str) -> str:
    return _CLOCK_CITATION.sub("", text)


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
    patterns: list[tuple[re.Pattern[str], str]] = []
    for name in sorted(aliases, key=len, reverse=True):
        patterns.append((_label_pattern(name), aliases[name]))

    def _replace_text(text: str) -> str:
        # Simultaneous single-pass match so swapping 张三 <-> 李四 never collides.
        combined = re.compile(
            "|".join(f"(?P<_g{i}>{pat.pattern})" for i, (pat, _) in enumerate(patterns))
        )

        def _sub(match: re.Match[str]) -> str:
            group_name = match.lastgroup
            if group_name and group_name.startswith("_g"):
                return patterns[int(group_name[2:])][1]
            return aliases.get(match.group(), match.group())

        return combined.sub(_sub, text)

    def canonical(value):
        if isinstance(value, str):
            return _replace_text(value)
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
    payload = _canonical_speaker_payload(payload or {})
    if template.is_refinement:
        from sayink.transcript_refiner import render_refinement

        return apply_speaker_names(render_refinement(dict(payload)), speaker_names)
    title = _as_text(payload.get("title")) or title_fallback
    out: list[str] = [f"# {apply_speaker_names(title, speaker_names)}"]
    if meta_lines:
        out.append("")
        out.extend(f"- {line}" for line in meta_lines if line)

    def section(key: str) -> None:
        out.append("")
        out.append(f"## {section_title(template, key)}")
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


_CJK_ALLOWED_BEFORE = (
    "由请和与及跟同向给对让叫找被把从替为据按听等派约邀帮陪拉连除在是有或但而并"
    "因待经如若当即含属系将就才都也还又再先后问看见归使令转交推选定点知醒排托助合持谢询复馈接"
)
_CJK_ALLOWED_AFTER_SINGLE = (
    "负责审核补充确认说明汇报参加记录整理总结提出建议决定安排介绍解释指出表示问道说讲"
    "的了着过和与及并而把被在从对向给跟同推落完交沟协评复准牵主配支处排修验测核拍签"
    "反更调优拆合发移承运转联通邀出列认觉希预计回答追询展演判担关需已将会要应可能先再后也都还又未没不于就等"
)
_CJK_ALLOWED_AFTER_PHRASES = (
    "强调|跟进|同步|推进|落实|提交|完成|交付|对接|沟通|协调|评审|复盘|准备|牵头|主导|"
    "协助|配合|支持|处理|解决|排查|修复|验证|测试|核对|核实|审批|拍板|签字|反馈|更新|"
    "修改|调整|优化|拆分|合并|提供|发送|移交|承接|承担|认领|转交|联系|通知|提醒|邀请|"
    "主持|出席|列席|发言|提到|认为|觉得|同意|反对|质疑|要求|希望|预计|计划|承诺|回应|"
    "回答|答复|回复|追问|询问|提问|分享|展示|演示|分析|评估|判断|担忧|担心|关注|关心|"
    "这边|那边|已经"
)
_TITLE_EXTENSION_GUARD = r"(?!(?:监|经理|裁|工|管|助|编|师|长))"


def _name_bounds(label: str) -> tuple[str, str]:
    """Don't let a short label rewrite a longer token.

    ``说话人2`` stops before ``说话人21``. A Chinese name may be preceded by
    common prepositions/conjunctions (由/请/和/与/让/给/跟/找/向…) and followed
    by verbs/particles (确认/跟进/负责/同步/汇报/提出…), while blocking title
    extensions (``王总`` must not hit ``王总监`` / ``王总经理``).
    """
    head = r"(?<![0-9A-Za-z])" if re.match(r"[0-9A-Za-z]", label) else ""
    tail = r"(?![0-9A-Za-z0-9])" if re.search(r"[0-9A-Za-z]$", label) else ""
    if re.search(r"[\u4e00-\u9fff]", label):
        cjk_head = rf"(?:(?<![\u4e00-\u9fff])|(?<=[{_CJK_ALLOWED_BEFORE}]))"
        head = head or cjk_head
        title_guard = _TITLE_EXTENSION_GUARD if label.endswith(("总", "工", "师", "长", "导")) else ""
        following = (
            rf"{title_guard}(?:(?![\u4e00-\u9fff])|(?=(?:{_CJK_ALLOWED_AFTER_PHRASES}|[{_CJK_ALLOWED_AFTER_SINGLE}])))"
        )
        tail = f"(?![0-9A-Za-z]){following}" if tail else following
    return head, tail


def _label_pattern(label: str) -> re.Pattern[str]:
    generic = _SPEAKER_LABEL_FULL_RE.match(label.strip())
    head, tail = _name_bounds(label.strip())
    if generic is not None:
        num = int(generic.group(1))
        return re.compile(rf"{head}说话人\s*0*{num}(?!\d)")
    return re.compile(head + re.escape(label) + tail)


def replace_names_in_markdown(markdown: str, old_label: str, new_label: str) -> tuple[str, int]:
    """Whole-label replacement for user-edited (free text) notes; returns the
    new text and how many places changed."""
    if not old_label or not new_label or old_label == new_label:
        return markdown, 0
    return _label_pattern(old_label).subn(lambda _: new_label, markdown)

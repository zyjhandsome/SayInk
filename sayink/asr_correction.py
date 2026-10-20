"""Correcting recognized text after the decoder: glossary mis-hearings and
the LLM 纠错 (correction) mode.

Two layers, both optional:

* **Glossary variants** — a glossary line ``五证=五中、无证`` means "when the
  recognizer writes 五中 or 无证 it meant 五证". The variants are replaced
  deterministically on every final result (no network, no latency). Hotwords
  only ever receive the canonical term (see ``format_hotwords``), so listing
  the mis-hearings here is the only place they are useful.
* **LLM correction** — a stricter sibling of polish: fix homophone typos and
  glossary spellings, change nothing else. The reply is accepted only when it
  passes the polish fidelity guard *and* stays within a small edit budget, so
  a chatty model falls back to the raw text instead of rewriting it.

The guard matters more than the prompt. Measured on a far-field meeting
excerpt the correction pass moves CER by a few tenths of a percent either
way; its value is the glossary-aware fixes (赠 ← 增, 新盘 ← 训盘), not the
headline number.

This module is pure Python so the benchmark can import it without Qt.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

from sayink.polish_fidelity import polish_rejection_reason

# ``术语=误写1、误写2``; full-width ＝ is what a Chinese IME produces.
_GLOSSARY_SEPARATOR_RE = re.compile(r"\s*[=＝]\s*")
_VARIANT_SPLIT_RE = re.compile(r"\s*[,，、;；]\s*")
MAX_GLOSSARY_VARIANTS = 5
MAX_GLOSSARY_VARIANT_CHARS = 20
# Entries sent to the model; matches the hotword cap so both lists agree.
MAX_PROMPT_GLOSSARY_ENTRIES = 50
# Edit budget for an accepted correction, on compacted text.
CORRECTION_MAX_EDIT_RATIO = 0.2
CORRECTION_MIN_LENGTH_RATIO = 0.85
CORRECTION_MAX_LENGTH_RATIO = 1.15
CORRECTION_LENGTH_SLACK = 4
CORRECTION_CONTEXT_CHARS = 120

CORRECTION_PROMPT = (
    "你是语音识别结果的校对工具，不是对话助手。\n"
    "任务：只修正语音识别产生的错误——同音字、近音字造成的错别字，被切错的词，"
    "以及术语表中术语的写法。其他内容一律原样保留。\n"
    "规则：\n"
    "1. 不回答、不评论内容；不润色、不删口语赘词、不改语序、不增删标点、不补全残句。\n"
    "2. 没有把握就保留原文；拿不准的字宁可不改。原文没有错误时原样输出。\n"
    "3. 数字、英文、人名保持原写法，除非术语表明确给出正确写法。\n"
    "4. 术语表格式为「正确写法 ← 可能被听成的写法」；只有原文出现误识别写法且上下文相符时才替换。\n"
    "5. 只输出校对后的全文，不加前缀、引号或任何解释。"
)


# ── glossary lines ───────────────────────────────────────────────────────────


def _entry_parts(entry) -> tuple[str, list[str]]:
    if isinstance(entry, dict):
        term = str(entry.get("term", "") or "").strip()
        raw_variants = entry.get("variants") or ()
    else:
        term, raw_variants = str(entry or "").strip(), ()
    variants: list[str] = []
    for variant in raw_variants:
        variant = str(variant or "").strip()
        if variant and variant != term and variant not in variants:
            variants.append(variant)
    return term, variants


def parse_glossary_line(line: str) -> dict | None:
    """One editor line → ``{"term": ...}`` or ``{"term": ..., "variants": [...]}``.

    ``五证=五中、无证`` lists what the recognizer tends to write instead of
    五证. Variants are split on ``,`` ``，`` ``、`` ``;`` ``；``; empty ones, a
    variant equal to the term and anything beyond the caps are dropped.
    Returns ``None`` for a blank line or one with no term before the ``=``.
    """
    text = (line or "").strip()
    if not text:
        return None
    term, sep, rest = _split_once(text)
    if not term:
        return None
    if not sep:
        return {"term": term}
    variants: list[str] = []
    for variant in _VARIANT_SPLIT_RE.split(rest):
        variant = variant.strip()
        if (
            not variant
            or variant == term
            or variant in variants
            or len(variant) > MAX_GLOSSARY_VARIANT_CHARS
        ):
            continue
        variants.append(variant)
        if len(variants) >= MAX_GLOSSARY_VARIANTS:
            break
    return {"term": term, "variants": variants} if variants else {"term": term}


def _split_once(text: str) -> tuple[str, str, str]:
    match = _GLOSSARY_SEPARATOR_RE.search(text)
    if match is None:
        return text, "", ""
    return text[: match.start()].strip(), match.group(0), text[match.end():].strip()


def format_glossary_line(entry) -> str:
    """Inverse of :func:`parse_glossary_line` for the settings editor."""
    term, variants = _entry_parts(entry)
    if not term:
        return ""
    return f"{term}={'、'.join(variants)}" if variants else term


# ── deterministic replacement ────────────────────────────────────────────────


def glossary_variant_map(glossary: Iterable) -> list[tuple[str, str]]:
    """``(variant, term)`` pairs, longest variant first.

    A variant that is itself another entry's term is skipped: 「新盘=训盘」
    plus a separate 「训盘」 term would otherwise fight over the same word.
    So is a variant contained in any term (「盘点=盘」 next to 「新盘」):
    applying it to already-corrected text would turn 新盘 into 新盘点, and
    the correction guard relies on the replacement being idempotent.
    """
    entries = [_entry_parts(e) for e in glossary or ()]
    terms = [term.casefold() for term, _ in entries if term]
    term_set = set(terms)
    pairs: dict[str, str] = {}
    for term, variants in entries:
        if not term:
            continue
        for variant in variants:
            key = variant.casefold()
            if key in term_set or key in pairs:
                continue
            if any(key in other for other in terms):
                continue
            pairs[key] = term
    ordered = sorted(pairs.items(), key=lambda kv: (-len(kv[0]), kv[0]))
    return [(variant, term) for variant, term in ordered]


def _variant_pattern(variant: str) -> str:
    escaped = re.escape(variant)
    if re.search(r"[A-Za-z0-9]", variant):
        # Latin variants only match whole words, like restore_glossary_case:
        # ``git hub`` should not fire inside ``digit hubris``.
        return rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])"
    return escaped


def apply_glossary_variants(text: str, glossary: Iterable) -> str:
    """Replace every listed mis-hearing with its glossary term.

    One left-to-right pass, longest variant first, case-insensitive for Latin
    text. The result is not re-scanned, so a term that happens to contain
    another variant is left alone.
    """
    if not text:
        return text
    pairs = glossary_variant_map(glossary)
    if not pairs:
        return text
    lookup = {variant.casefold(): term for variant, term in pairs}
    pattern = re.compile("|".join(_variant_pattern(v) for v, _ in pairs), re.IGNORECASE)
    return pattern.sub(lambda m: lookup.get(m.group(0).casefold(), m.group(0)), text)


# ── LLM prompt ───────────────────────────────────────────────────────────────


def glossary_prompt_lines(glossary: Iterable) -> list[str]:
    """``正确写法 ← 误写1、误写2`` per entry (plain terms list just the term)."""
    lines: list[str] = []
    seen: set[str] = set()
    for entry in glossary or ():
        term, variants = _entry_parts(entry)
        if not term or term in seen:
            continue
        seen.add(term)
        lines.append(f"{term} ← {'、'.join(variants)}" if variants else term)
        if len(lines) >= MAX_PROMPT_GLOSSARY_ENTRIES:
            break
    return lines


def correction_user_prompt(text: str, glossary: Iterable = (), context: str = "") -> str:
    """The user turn: optional glossary, optional preceding text, then the text.

    The context is the previous output (its tail), marked as read-only, so a
    sentence split across two recordings still gets its first half's words.
    """
    parts: list[str] = []
    lines = glossary_prompt_lines(glossary)
    if lines:
        parts.append("术语表：\n" + "\n".join(f"- {line}" for line in lines))
    tail = (context or "").strip()[-CORRECTION_CONTEXT_CHARS:]
    if tail:
        parts.append(f"上文（仅供参考，不要输出）：{tail}")
    parts.append(f"待校对：\n{text}")
    return "\n".join(parts)


# ── acceptance guard ─────────────────────────────────────────────────────────


def _compact(text: str) -> str:
    """Letters, digits and CJK only, case-folded: the edit budget should count
    changed words, not punctuation the model added or dropped."""
    out = []
    for ch in unicodedata.normalize("NFKC", text or ""):
        if ch.isalnum():
            out.append(ch.casefold())
    return "".join(out)


def _edit_distance(a: str, b: str, limit: int) -> int:
    """Levenshtein distance, or any value above ``limit`` once it is exceeded."""
    # Corrections are local, so strip the shared ends before the O(n·m) part.
    start = 0
    while start < len(a) and start < len(b) and a[start] == b[start]:
        start += 1
    end_a, end_b = len(a), len(b)
    while end_a > start and end_b > start and a[end_a - 1] == b[end_b - 1]:
        end_a -= 1
        end_b -= 1
    a, b = a[start:end_a], b[start:end_b]
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    if not a or not b:
        return max(len(a), len(b))
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        best = i
        for j, cb in enumerate(b, 1):
            cost = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb))
            current.append(cost)
            best = min(best, cost)
        if best > limit:
            return limit + 1
        previous = current
    return previous[-1]


def correction_rejection_reason(raw: str, corrected: str, glossary: Iterable = ()) -> str:
    """Why a correction must not replace ``raw``; empty when it may.

    The baseline is ``raw`` with glossary variants already applied, so a model
    that performs the same replacement is not charged for it. On top of the
    polish guard (numbers, negations, English names) the reply must keep
    roughly the same length and change at most a fifth of the characters.
    """
    expected = apply_glossary_variants(raw or "", glossary)
    reason = polish_rejection_reason(expected, corrected)
    if reason:
        return reason
    a, b = _compact(expected), _compact(corrected)
    if not a:
        return "" if not b else "空文本被扩写"
    if (
        len(b) < len(a) * CORRECTION_MIN_LENGTH_RATIO
        or len(b) > len(a) * CORRECTION_MAX_LENGTH_RATIO + CORRECTION_LENGTH_SLACK
    ):
        return "长度变化过大"
    limit = max(2, int(len(a) * CORRECTION_MAX_EDIT_RATIO))
    if _edit_distance(a, b, limit) > limit:
        return "改动过多"
    return ""


def correction_acceptable(raw: str, corrected: str, glossary: Iterable = ()) -> bool:
    return not correction_rejection_reason(raw, corrected, glossary)

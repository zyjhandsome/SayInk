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

import functools
import logging
import re
import threading
import unicodedata
from collections.abc import Iterable

from typing import Any

from sayink.polish_fidelity import polish_rejection_reason

log = logging.getLogger("SayInk")

# ``术语=误写1、误写2``; full-width ＝ is what a Chinese IME produces.
_GLOSSARY_SEPARATOR_RE = re.compile(r"\s*[=＝]\s*")
_VARIANT_SPLIT_RE = re.compile(r"\s*[,，、;；]\s*")
MAX_GLOSSARY_ENTRIES = 500
MAX_GLOSSARY_VARIANTS = 5
MAX_GLOSSARY_VARIANT_CHARS = 20
# Entries sent to the LLM correction prompt (keeps token budget bounded even with 500 local entries).
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
    return parse_glossary_line_with_warnings(line)[0]


def parse_glossary_line_with_warnings(line: str) -> tuple[dict | None, list[str]]:
    """Parse an editor line and report values omitted by the limits."""
    text = (line or "").strip()
    if not text:
        return None, []
    term, sep, rest = _split_once(text)
    if not term:
        return None, ["缺少正确词"]
    if not sep:
        return {"term": term}, []
    variants: list[str] = []
    warnings: list[str] = []
    for variant in _VARIANT_SPLIT_RE.split(rest):
        variant = variant.strip()
        if not variant or variant == term or variant in variants:
            continue
        if len(variant) > MAX_GLOSSARY_VARIANT_CHARS:
            if "误写超过 20 字" not in warnings:
                warnings.append("误写超过 20 字")
            continue
        if len(variants) >= MAX_GLOSSARY_VARIANTS:
            if "误写超过 5 个" not in warnings:
                warnings.append("误写超过 5 个")
            continue
        variants.append(variant)
    entry = {"term": term, "variants": variants} if variants else {"term": term}
    return entry, warnings


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

# High-frequency everyday 2-character Chinese predicates/modifiers: when one of
# these is listed as a homophone variant (e.g. ``通义=同意``), protect everyday
# grammatical collocations like ``不同意`` / ``没同意`` / ``很同意`` / ``同意了`` / ``同意吗``.
_COMMON_DAILY_CJK_WORDS = frozenset({
    "同意", "明白", "清楚", "确定", "方便", "意思", "知道", "可能", "需要", "应该",
    "可以", "问题", "关系", "希望", "理想", "自然", "正式", "正常", "显示", "消息",
    "发现", "完全", "绝对", "一定", "一般", "一直", "一起", "一下", "一些", "一点",
    "已经", "以为", "以后", "以前", "以上", "以下", "以外", "以内", "其实", "其他",
    "其中", "非常", "特别", "比较", "觉得", "感觉", "认为", "表示", "进行", "通过",
    "开始", "结束", "继续", "准备", "完成", "支持", "反对", "参加", "参与", "处理",
    "解决", "讨论", "确认", "通知", "联系", "沟通", "安排", "计划", "方案", "结果",
})
_LOGGED_GLOSSARY_CONFLICTS: set[str] = set()
_GLOSSARY_CONFLICT_LOCK = threading.Lock()


def _append_unique_conflict(conflicts: list[str], message: str) -> None:
    if message and message not in conflicts:
        conflicts.append(message)


def _classify_variant_entry(
    term: str,
    term_cf: str,
    variant: str,
    *,
    terms_orig: list[str],
    terms_cf: list[str],
    term_by_cf: dict[str, str],
    term_set: set[str],
    pairs: dict[str, tuple[str, str]],
) -> tuple[tuple[str, str] | None, str]:
    """Return ``((variant, term), "")`` when usable, or ``(None, conflict_msg)``."""
    key = variant.casefold()
    if len(variant) == 1 and re.fullmatch(r"[\u4e00-\u9fff]", variant):
        return None, f"单字误写「{variant}→{term}」已忽略（易误伤常用字）"
    if key == term_cf:
        other_terms = [
            orig for orig, cf in zip(terms_orig, terms_cf, strict=True)
            if cf == key and orig != term
        ]
        if other_terms:
            return None, f"误写「{variant}→{term}」与术语「{other_terms[0]}」同名，已跳过"
        return ((variant, term), "") if key not in pairs else (None, "")
    if key in term_set:
        other = term_by_cf.get(key, variant)
        return None, f"误写「{variant}→{term}」与术语「{other}」同名，已跳过"
    if key in pairs:
        kept_term = pairs[key][1]
        return None, f"误写「{variant}」同时指向「{kept_term}」与「{term}」，保留「{kept_term}」"
    containing = [orig for orig, cf in zip(terms_orig, terms_cf, strict=True) if key in cf]
    if containing:
        return None, f"误写「{variant}→{term}」被术语「{containing[0]}」包含，已跳过以防重复替换"
    return (variant, term), ""


def _log_new_glossary_conflicts(conflicts: list[str]) -> None:
    if not conflicts:
        return
    with _GLOSSARY_CONFLICT_LOCK:
        for msg in conflicts:
            if msg not in _LOGGED_GLOSSARY_CONFLICTS:
                if len(_LOGGED_GLOSSARY_CONFLICTS) > 512:
                    _LOGGED_GLOSSARY_CONFLICTS.clear()
                _LOGGED_GLOSSARY_CONFLICTS.add(msg)
                log.warning("词库冲突提示: %s", msg)


def glossary_variant_map_with_conflicts(
    glossary: Iterable,
) -> tuple[list[tuple[str, str]], list[str]]:
    """Return ``(pairs, conflicts)`` where ``conflicts`` records shadowed rules."""
    entries = [_entry_parts(e) for e in glossary or ()]
    terms_orig = [term for term, _ in entries if term]
    terms_cf = [term.casefold() for term in terms_orig]
    term_by_cf = {term.casefold(): term for term in terms_orig}
    term_set = set(terms_cf)
    pairs: dict[str, tuple[str, str]] = {}
    conflicts: list[str] = []
    for term, variants in entries:
        if not term:
            continue
        term_cf = term.casefold()
        for variant in variants:
            pair, conflict = _classify_variant_entry(
                term,
                term_cf,
                variant,
                terms_orig=terms_orig,
                terms_cf=terms_cf,
                term_by_cf=term_by_cf,
                term_set=term_set,
                pairs=pairs,
            )
            if conflict:
                _append_unique_conflict(conflicts, conflict)
            elif pair is not None:
                pairs[variant.casefold()] = pair
    _log_new_glossary_conflicts(conflicts)
    ordered = sorted(pairs.items(), key=lambda kv: (-len(kv[0]), kv[0]))
    return [pair for _, pair in ordered], conflicts


def glossary_variant_map(glossary: Iterable) -> list[tuple[str, str]]:
    """``(variant, term)`` pairs, longest variant first.

    A variant that is itself another entry's term is skipped: 「新盘=训盘」
    plus a separate 「训盘」 term would otherwise fight over the same word.
    So is a variant contained in any term (「盘点=盘」 next to 「新盘」):
    applying it to already-corrected text would turn 新盘 into 新盘点, and
    the correction guard relies on the replacement being idempotent.
    """
    return glossary_variant_map_with_conflicts(glossary)[0]


def _variant_pattern(variant: str) -> str:
    escaped = re.escape(variant)
    if re.search(r"[A-Za-z0-9]", variant):
        # Protect code identifiers (api_key, user-api, my.net) and version strings
        # (v1.0.5) in addition to ASCII word boundaries.
        return rf"(?<![A-Za-z0-9])(?<![A-Za-z0-9][._-]){escaped}(?![A-Za-z0-9])(?![._-][A-Za-z0-9])"
    if len(variant) == 2 and variant in _COMMON_DAILY_CJK_WORDS:
        return rf"(?<![不没未别莫很太更最无非]){escaped}(?![吗呢吧啊呀了着过])"
    return escaped


@functools.lru_cache(maxsize=16)
def _compiled_variant_rules(
    pairs_tuple: tuple[tuple[str, str], ...],
) -> tuple[re.Pattern[str], dict[str, str]]:
    lookup = {variant.casefold(): term for variant, term in pairs_tuple}
    pattern = re.compile("|".join(_variant_pattern(v) for v, _ in pairs_tuple), re.IGNORECASE)
    return pattern, lookup


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
    pattern, lookup = _compiled_variant_rules(tuple(pairs))
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


# ── self-learning glossary flywheel ──────────────────────────────────────────

_ASCII_WORD_CHAR_RE = re.compile(r"[A-Za-z0-9._+-]")
_CJK_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")
_PUNCT_STRIP_CHARS = " \t\r\n,，.。!！?？;；:：'\"“”‘’()（）[]【】<>《》-—·、"
_INTERNAL_PUNCT_RE = re.compile(r"[\r\n,，.。!！?？;；:：()（）\[\]【】<>《》]")
_NUMERIC_ONLY_RE = re.compile(r"^[\d\s.,:%+\-/]+$")
# Function particles / pronouns that should never be glued onto a 1-char CJK typo
# when expanding a single-character diff into a 2-character lexical term.
_CJK_BOUNDARY_STOP_CHARS = frozenset(
    "的我你他她它们这那在是了着过吗呢吧啊呀哦嗯哈和与及或而但又也都很更最"
    "不没未别莫已正将再才就还只刚先去来把被让给向从对跟比按用为因由到说看想"
)


def _is_cjk_content_char(ch: str) -> bool:
    return bool(ch and _CJK_CHAR_RE.fullmatch(ch) and ch not in _CJK_BOUNDARY_STOP_CHARS)


def _compact_case_sensitive(text: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKC", text or "") if ch.isalnum())


def _is_ascii_word(ch: str) -> bool:
    return bool(ch and _ASCII_WORD_CHAR_RE.fullmatch(ch))


def _expand_ascii_bounds(
    before: str,
    after: str,
    start: int,
    end_b: int,
    end_a: int,
) -> tuple[int, int, int]:
    while start > 0 and _is_ascii_word(before[start - 1]):
        in_word = (start < len(before) and _is_ascii_word(before[start])) or (
            start < len(after) and _is_ascii_word(after[start])
        )
        if not in_word:
            break
        start -= 1
    while end_b < len(before) and end_a < len(after) and _is_ascii_word(before[end_b]):
        in_word = (end_b > 0 and _is_ascii_word(before[end_b - 1])) or (
            end_a > 0 and _is_ascii_word(after[end_a - 1])
        )
        if not in_word:
            break
        end_b += 1
        end_a += 1
    return start, end_b, end_a


def _expand_single_cjk_neighbor(
    before: str,
    after: str,
    start: int,
    end_b: int,
    end_a: int,
) -> tuple[int, int, int]:
    raw_b, raw_a = before[start:end_b], after[start:end_a]
    if not (
        len(raw_b) == 1
        and len(raw_a) == 1
        and _CJK_CHAR_RE.fullmatch(raw_b)
        and _CJK_CHAR_RE.fullmatch(raw_a)
    ):
        return start, end_b, end_a
    if start > 0 and _is_cjk_content_char(before[start - 1]):
        return start - 1, end_b, end_a
    if end_b < len(before) and _is_cjk_content_char(before[end_b]):
        return start, end_b + 1, end_a + 1
    return start, end_b, end_a


def _isolate_diff_span(before: str, after: str) -> tuple[str, str]:
    """Isolate the minimal word-aligned changed span between ``before`` and ``after``."""
    start = 0
    limit = min(len(before), len(after))
    while start < limit and before[start] == after[start]:
        start += 1
    end_b, end_a = len(before), len(after)
    while end_b > start and end_a > start and before[end_b - 1] == after[end_a - 1]:
        end_b -= 1
        end_a -= 1
    start, end_b, end_a = _expand_ascii_bounds(before, after, start, end_b, end_a)
    start, end_b, end_a = _expand_single_cjk_neighbor(before, after, start, end_b, end_a)
    return before[start:end_b].strip(_PUNCT_STRIP_CHARS), after[start:end_a].strip(_PUNCT_STRIP_CHARS)


def _passes_lexical_pair_shape(term: str, variant: str) -> bool:
    if not term or not variant or term == variant:
        return False
    if not (2 <= len(term) <= MAX_GLOSSARY_VARIANT_CHARS):
        return False
    if not (2 <= len(variant) <= MAX_GLOSSARY_VARIANT_CHARS):
        return False
    if abs(len(term) - len(variant)) > 8 or variant in _COMMON_DAILY_CJK_WORDS:
        return False
    if _INTERNAL_PUNCT_RE.search(term) or _INTERNAL_PUNCT_RE.search(variant):
        return False
    return not (_NUMERIC_ONLY_RE.fullmatch(term) or _NUMERIC_ONLY_RE.fullmatch(variant))


def _is_valid_learned_pair(term: str, variant: str, existing_glossary: Iterable) -> bool:
    if not _passes_lexical_pair_shape(term, variant):
        return False
    entries = [_entry_parts(e) for e in existing_glossary or ()]
    if term.casefold() == variant.casefold():
        return not any(t == term for t, _ in entries)
    mapped = {v.casefold(): t for v, t in glossary_variant_map(existing_glossary)}
    if mapped.get(variant.casefold()) == term:
        return False
    updated, changed = merge_glossary_learning(existing_glossary, term, variant)
    if not changed:
        return False
    new_map = {v.casefold(): t for v, t in glossary_variant_map(updated)}
    return new_map.get(variant.casefold()) == term


def _single_changed_clause(orig: str, dest: str) -> tuple[str, str] | None:
    orig_clauses = [c.strip() for c in _INTERNAL_PUNCT_RE.split(orig) if c.strip()]
    dest_clauses = [c.strip() for c in _INTERNAL_PUNCT_RE.split(dest) if c.strip()]
    if len(orig_clauses) != len(dest_clauses):
        return None
    changed = [
        (o, d)
        for o, d in zip(orig_clauses, dest_clauses, strict=True)
        if _compact_case_sensitive(o) != _compact_case_sensitive(d)
    ]
    return changed[0] if len(changed) == 1 else None


def extract_learned_correction_pair(
    original: str,
    edited: str,
    existing_glossary: Iterable = (),
) -> tuple[str, str] | None:
    """Extract a ``(term, variant)`` glossary candidate from a user text edit."""
    orig = (original or "").strip()
    dest = (edited or "").strip()
    if not orig or not dest or _compact_case_sensitive(orig) == _compact_case_sensitive(dest):
        return None
    variant, term = _isolate_diff_span(orig, dest)
    if _is_valid_learned_pair(term, variant, existing_glossary):
        return (term, variant)
    clause_pair = _single_changed_clause(orig, dest)
    if clause_pair is not None:
        v2, t2 = _isolate_diff_span(clause_pair[0], clause_pair[1])
        if _is_valid_learned_pair(t2, v2, existing_glossary):
            return (t2, v2)
    return None


def format_glossary_suggestion_label(term: str, variant: str = "") -> str:
    """Format ``term`` and ``variant`` as shown in UI feedback (``SayInk`` or ``张鑫=张新``)."""
    clean_term = (term or "").strip()
    clean_var = (variant or "").strip()
    if not clean_var or clean_var.casefold() == clean_term.casefold():
        return clean_term
    return f"{clean_term}={clean_var}"


def _normalize_glossary_for_merge(
    glossary: Iterable,
    clean_term_cf: str,
) -> tuple[list[dict[str, Any]], int | None]:
    normalized: list[dict[str, Any]] = []
    target_idx: int | None = None
    for item in glossary or ():
        t, vars_list = _entry_parts(item)
        if not t:
            continue
        entry: dict[str, Any] = {"term": t, "variants": list(vars_list)} if vars_list else {"term": t}
        if t.casefold() == clean_term_cf and target_idx is None:
            target_idx = len(normalized)
        normalized.append(entry)
    return normalized, target_idx


def _merge_into_existing_entry(
    existing: dict[str, Any],
    clean_term: str,
    clean_var: str,
) -> tuple[dict[str, Any], bool]:
    variants = list(existing.get("variants") or [])
    if not clean_var:
        if existing["term"] == clean_term:
            return existing, False
        updated = {"term": clean_term, "variants": variants} if variants else {"term": clean_term}
        return updated, True
    if clean_var in variants or len(variants) >= MAX_GLOSSARY_VARIANTS:
        return existing, False
    variants.append(clean_var)
    return {"term": existing["term"], "variants": variants}, True


def merge_glossary_learning(
    glossary: Iterable,
    term: str,
    variant: str = "",
) -> tuple[list[dict[str, Any]], bool]:
    """Merge a learned ``(term, variant)`` rule into ``glossary`` without displacing top hotwords."""
    clean_term = (term or "").strip()
    clean_var = (variant or "").strip()
    if not clean_term or len(clean_term) > MAX_GLOSSARY_VARIANT_CHARS:
        return [], False
    if len(clean_var) > MAX_GLOSSARY_VARIANT_CHARS or clean_var.casefold() == clean_term.casefold():
        clean_var = ""

    normalized, target_idx = _normalize_glossary_for_merge(glossary, clean_term.casefold())
    if target_idx is not None:
        merged, changed = _merge_into_existing_entry(normalized[target_idx], clean_term, clean_var)
        if changed:
            normalized[target_idx] = merged
        return normalized, changed

    if len(normalized) >= MAX_GLOSSARY_ENTRIES:
        return normalized, False
    new_entry: dict[str, Any] = (
        {"term": clean_term, "variants": [clean_var]} if clean_var else {"term": clean_term}
    )
    normalized.append(new_entry)
    return normalized, True


def persist_glossary_learning(config: Any, term: str, variant: str = "") -> bool:
    """Persist a learned glossary entry into ``config`` (``stt.glossary``) and return True if changed."""
    if config is None or not callable(getattr(config, "get", None)):
        return False
    current = config.get("stt.glossary", []) or []
    updated, changed = merge_glossary_learning(current, term, variant)
    if not changed:
        return False
    config.set("stt.glossary", updated)
    save_immediate = getattr(config, "save_immediate", None)
    if callable(save_immediate):
        save_immediate()
    return True

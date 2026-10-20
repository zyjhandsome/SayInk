"""Guards that decide whether an LLM edit may replace the recognized text.

Shared by dictation polish, the ASR correction mode and the benchmark. Only
clear-cut changes are caught (a lost number, a dropped or moved negation, a
missing English name, a runaway length); these are guards, not a proof of
semantic fidelity. Nothing here touches Qt or the network.
"""
from __future__ import annotations

import re
import unicodedata

_THOUSANDS_SEP_RE = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
# Words containing a negation character without negating anything.
_NON_NEGATING_WORDS = (
    "非常", "无论", "不过", "不仅", "不但", "未来", "无线",
    "特别", "区别", "分别", "类别", "级别", "告别", "识别", "性别", "别人", "别的",
)
_NEGATION_CHARS = frozenset("不没别未无非勿莫")
_EN_NEGATION_RE = re.compile(
    r"\b(?:not|no|never|none|cannot)\b|n't\b", re.IGNORECASE | re.ASCII
)
_LATIN_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")
# Chinese numerals: FireRedASR2 writes 两千五百元 / 十月十五日, so the number
# guard has to read them too. A numeral starts with a digit or 十; a lone
# 百/千/万 is a word (千万别, 百分之) and stays text.
_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}
_CN_BIG_UNITS = {"万": 10_000, "亿": 100_000_000}
_CN_NUMERAL_RE = re.compile(r"[一二两三四五六七八九十][零〇一二两三四五六七八九十百千万亿]*")
_REPEATED_NEGATION_RE = re.compile(r"([不没别未无非勿莫])\1+")
# Spoken filler a polish is allowed to drop; the compression guard measures
# the raw text without it so a cleaned-up ramble is not mistaken for a summary.
_FILLER_RE = re.compile(r"嗯+|啊+|呃+|额+|唔+|哦+|那个|这个|就是说|就是|然后|的话|一下|对对+|呢|吧|嘛|呀")
# How a chat model opens an *answer* rather than an edit. Only decisive when
# the raw text did not open the same way and the reply grew well past it.
_ANSWER_PREFIX_RE = re.compile(r"^(?:好的|好啊|以下是|当然可以|当然|没问题|明白了|收到|请问|作为一名|作为)")
_WHITESPACE_RE = re.compile(r"\s+")


def _cn_numeral_to_arabic(numeral: str) -> str:
    """两千五百 → 2500; 一三八零零 → 13800; 三四 → "3 4" (an approximate range)."""
    if all(ch in _CN_DIGITS for ch in numeral):
        digits = [_CN_DIGITS[ch] for ch in numeral]
        if len(digits) == 2 and 0 not in digits and abs(digits[0] - digits[1]) == 1:
            return f"{digits[0]} {digits[1]}"
        return "".join(str(d) for d in digits)
    total = 0
    section = 0
    number = 0
    for ch in numeral:
        if ch in _CN_DIGITS:
            number = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            section += (number or 1) * _CN_UNITS[ch]
            number = 0
        else:
            total = (total + section + number) * _CN_BIG_UNITS[ch]
            section = 0
            number = 0
    if number and len(numeral) > 1:
        # Colloquial 三万五 / 两千五: the trailing digit takes the next unit down.
        before = numeral[-2]
        unit = _CN_UNITS.get(before) or _CN_BIG_UNITS.get(before) or 0
        if unit >= 100:
            number *= unit // 10
    return str(total + section + number)


def _normalize_cn_numerals(text: str) -> str:
    def _replace(match: re.Match) -> str:
        numeral = match.group(0)
        if numeral == "一":
            # A lone 一 is mostly an article or idiom (一段话, 优化一下, 一起);
            # dropping or adding it does not change the meaning.
            return numeral
        return f" {_cn_numeral_to_arabic(numeral)} "

    return _CN_NUMERAL_RE.sub(_replace, text)


def _number_sequence(text: str) -> list[str]:
    """Numbers in the order they appear, after Chinese-numeral normalization."""
    values = []
    text = _normalize_cn_numerals(_THOUSANDS_SEP_RE.sub("", text))
    for match in _NUMBER_RE.findall(text):
        whole, _, frac = match.partition(".")
        value = (whole.lstrip("0") or "0") + (f".{frac.rstrip('0')}" if frac.rstrip("0") else "")
        values.append(value)
    return values


def _numbers(text: str) -> set[str]:
    return set(_number_sequence(text))


def _number_order_kept(raw: list[str], polished: list[str]) -> bool:
    """Polished text may insert formatting zeros (3点 → 3:00) but not reorder."""
    index = 0
    for value in polished:
        if index < len(raw) and value == raw[index]:
            index += 1
        elif value == "0":
            continue
        else:
            return False
    return index == len(raw)


_CLAUSE_SPLIT_RE = re.compile(r"[，。！？；、,.!?;]+")


def _clause_negation_counts(text: str) -> list[int]:
    parts = [part.strip() for part in _CLAUSE_SPLIT_RE.split(text) if part.strip()]
    return [_negation_count(part) for part in parts]


def _negation_count(text: str) -> int:
    for word in _NON_NEGATING_WORDS:
        text = text.replace(word, "")
    text = _REPEATED_NEGATION_RE.sub(r"\1", text)
    return sum(1 for ch in text if ch in _NEGATION_CHARS) + len(_EN_NEGATION_RE.findall(text))


def _has_negation(text: str) -> bool:
    return _negation_count(text) > 0


def _proper_nouns(text: str) -> set[str]:
    """Latin names worth keeping verbatim: GitHub, iPhone, API, AI, Python,
    GPT4. All-lowercase words are left out so dropped English filler is not
    mistaken for a lost name."""
    names = set()
    for token in _LATIN_TOKEN_RE.findall(text):
        if len(token) >= 2 and any(c.isupper() or c.isdigit() for c in token):
            names.add(token.casefold())
    return names


def _keeps_name(name: str, folded: str) -> bool:
    # Whole word only: API inside "rapid" is not the name. Spacing may change
    # either way (iPhone15 → iPhone 15), so try the text with and without it.
    # A digit may precede the name (2024Q3, 3GPP): the tokenizer starts a name
    # at its first letter, so the same text must pass here or an unchanged
    # sentence would be "missing" its own name.
    pattern = re.compile(rf"(?<![a-z]){re.escape(name)}(?![a-z0-9])")
    return any(pattern.search(t) for t in (folded, re.sub(r"\s+", "", folded)))


def polish_rejection_reason(raw: str, polished: str) -> str:
    """Why a polish reply must not replace the raw text; empty when it may.

    Only clear-cut changes are caught: converting 一百 → 100 or rewording
    Chinese names passes, so this is a guard, not a fidelity proof.
    """
    raw = unicodedata.normalize("NFKC", raw or "").strip()
    polished = unicodedata.normalize("NFKC", polished or "").strip()
    if not raw:
        return ""
    # Growth budget: twice the sentence plus a little, where "a little" scales
    # with the sentence. A flat +40 let any reply to a five-character line pass
    # (「你好」→ a whole greeting), which is where models answer most readily.
    if len(polished) > len(raw) * 2 + max(8, min(40, len(raw) // 2)):
        return "长度异常"
    raw_compact = _WHITESPACE_RE.sub("", raw)
    polished_compact = _WHITESPACE_RE.sub("", polished)
    core = _FILLER_RE.sub("", raw_compact)
    if len(core) >= 12 and len(polished_compact) < len(core) * 0.4:
        # A polish may drop filler and repetition, not most of the content.
        return "内容被大幅压缩"
    answer = _ANSWER_PREFIX_RE.match(polished_compact)
    if (
        answer
        and not raw_compact.startswith(answer.group(0))
        and len(polished) > len(raw) * 1.3 + 2
    ):
        return "疑似应答而非编辑"
    raw_sequence = _number_sequence(raw)
    polished_sequence = _number_sequence(polished)
    raw_numbers = set(raw_sequence)
    polished_numbers = set(polished_sequence)
    # Keep every original number, including zero, in the same order. An added
    # zero can be formatting (3点 → 3:00); swapping 100 and 10 is not.
    if (
        raw_numbers - polished_numbers
        or (polished_numbers - raw_numbers) - {"0"}
        or not _number_order_kept(raw_sequence, polished_sequence)
    ):
        return "数字被改动"
    # Count, not presence: 我不同意，也没准备好 → 我不同意，也准备好了 drops one.
    if _negation_count(polished) < _negation_count(raw):
        return "否定词丢失"
    raw_clauses = _clause_negation_counts(raw)
    polished_clauses = _clause_negation_counts(polished)
    # Same number of clauses: a negation that moved to another clause flipped
    # which claim it applies to (不是 A，而是 B → 是 A，不是 B).
    if raw_clauses and len(raw_clauses) == len(polished_clauses) and raw_clauses != polished_clauses:
        return "否定词错位"
    folded = polished.casefold()
    if any(not _keeps_name(name, folded) for name in _proper_nouns(raw)):
        return "英文专名丢失"
    return ""


def polish_looks_plausible(raw: str, polished: str) -> bool:
    """Reject replies that answer or change the text instead of lightly editing it."""
    return not polish_rejection_reason(raw, polished)

"""Turn pasted or saved meeting text into ordered turns.

A format is used only when its cue repeats. A memo that happens to contain
one colon stays plain text, and the caller decides whether missing speakers
or timestamps may be shown to the model.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Sequence

IMPORT_SOURCE = "import_text"

_STOP_LABELS = frozenset({
    "背景", "注意", "摘要", "议程", "时间", "参会人", "标题", "说明", "备注", "总结", "结论", "主题",
})
_NOT_NAMES = _STOP_LABELS | {"http", "https", "ftp", "file", "mailto"}
_NUMBERED = re.compile(r"^(?:说话人|发言人|speaker)\s*(\d+)$", re.IGNORECASE)
_CLOCK_IN_LABEL = re.compile(r"\d{1,2}:\d{2}")
_SENTENCE_PUNCT = "。！？!?，,"
_CUE_TIME = re.compile(
    r"(?:(?P<sh>\d{1,}):)?(?P<sm>\d{1,2}):(?P<ss>\d{2})[.,](?P<sms>\d{1,3})\s*-->\s*"
    r"(?:(?P<eh>\d{1,}):)?(?P<em>\d{1,2}):(?P<es>\d{2})[.,](?P<ems>\d{1,3})"
)
_LOOSE_CUE_ARROW = re.compile(r"\d+:\d{1,2}(?::\d{1,2})?(?:[.,]\d+)?\s*-->\s*\d+:\d{1,2}")
_VTT_META_BLOCK = re.compile(r"^(?:NOTE|STYLE|REGION)(?:$|[ \t])", re.IGNORECASE)
_VTT_INLINE_TIME = re.compile(r"<(?:\d{1,}:)?\d{1,2}:\d{2}[.,]\d{1,3}>")
_VTT_INLINE_TAG = re.compile(
    r"</?(?:c|v|i|b|u|s|ruby|rt|rp|lang|font)(?:\.[^\s/>]+)*(?:\s+[^>]*)?>",
    re.IGNORECASE,
)
_DATE_TOKEN = r"\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?"
_MERIDIEM_TOKEN = r"(?:上午|下午|早上|清晨|中午|傍晚|晚上|凌晨|AM|PM|am|pm)"
_CLOCK = r"(?P<a>\d{1,3}):(?P<b>\d{2})(?::(?P<c>\d{2}))?"
_TRAILING_DATE = re.compile(
    rf"(?:\s+{_DATE_TOKEN})?(?:\s*{_MERIDIEM_TOKEN})?\s*$"
)
_HAS_WALL_HINT = re.compile(rf"(?:{_DATE_TOKEN}|{_MERIDIEM_TOKEN})")
_PM_HINT = re.compile(r"(?:下午|傍晚|晚上|\bPM\b|\bpm\b)")
_AM_MIDNIGHT_HINT = re.compile(r"(?:凌晨|\bAM\b|\bam\b)")
_BLOCK_HEADER = re.compile(
    rf"^(?P<who>.{{1,48}}?)\s*[\(（]?\s*{_CLOCK}\s*(?P<ampm>AM|PM|am|pm)?\s*[\)）]?\s*$"
)
_DATE_FIRST_HEADER = re.compile(
    rf"^(?P<date>{_DATE_TOKEN})\s*(?P<mer>{_MERIDIEM_TOKEN})?\s*{_CLOCK}\s+(?P<who>.{{1,24}}?)\s*$"
)
_TIMED_COLON = re.compile(
    rf"^(?P<who>.{{1,48}}?)\s*[\(（]?\s*{_CLOCK}\s*(?P<ampm>AM|PM|am|pm)?\s*[\)）]?\s*[：:]\s*(?P<body>\S.*)$"
)
_BRACKET = re.compile(
    rf"^\[(?:(?P<date>{_DATE_TOKEN})\s*)?(?:(?P<mer>{_MERIDIEM_TOKEN})\s*)?"
    rf"(?P<a>\d{{1,3}}):(?P<b>\d{{2}})(?::(?P<c>\d{{2}}))?(?:\s*(?P<ampm>AM|PM|am|pm))?\]\s*"
    r"(?:(?P<who>[^：:\]]{1,24})[：:])?\s*(?P<body>\S.*)$"
)
_DATE_FIRST_COLON = re.compile(
    rf"^(?P<date>{_DATE_TOKEN})\s*(?P<mer>{_MERIDIEM_TOKEN})?\s*{_CLOCK}\s+"
    r"(?P<who>[^：:\n]{1,24})[：:]\s*(?P<body>\S.*)$"
)
_DIALOGUE = re.compile(r"^(?P<who>[^：:\n]{1,12})[：:]\s*(?P<body>\S.*)$")
_SPEAKER_PREFIX = re.compile(r"^(?P<who>[^：:\n]{1,16})[：:]\s*(?P<body>\S.*)$", re.DOTALL)
_VTT_VOICE = re.compile(r"^<v(?:\.[^\s>]+)*[ \t]+([^>]+)>(.*?)(?:</v>)?\s*$", re.DOTALL | re.IGNORECASE)
_H1 = re.compile(r"^#\s+(.+?)\s*$")
_SUPPRESSED_POLISHED_MARKER = "\u200b"


@dataclass(frozen=True)
class ImportedTurn:
    speaker_label: str
    offset_ms: int | None
    duration_ms: int
    text: str


class TranscriptDecodeError(ValueError):
    """The bytes are not a transcript the user can import. Keep the current text."""


@dataclass(frozen=True)
class ImportedTranscript:
    title: str
    turns: tuple[ImportedTurn, ...]
    format_label: str
    discarded: int = 0
    skipped: tuple[str, ...] = ()

    @property
    def has_timestamps(self) -> bool:
        return any(turn.offset_ms is not None for turn in self.turns)

    @property
    def has_speakers(self) -> bool:
        return any(turn.speaker_label.strip() for turn in self.turns)


def _detect_bomless_utf_wide(data: bytes) -> str | None:
    """Detect unmistakable BOM-less UTF-32/UTF-16 byte layouts before 8-bit codecs."""
    n = len(data)
    if n >= 4 and n % 4 == 0:
        units = n // 4
        if (
            data[3::4].count(0) == units
            and data[2::4].count(0) == units
            and (data[0::4].count(0) < units or data[1::4].count(0) < units)
        ):
            return "utf-32-le"
        if (
            data[0::4].count(0) == units
            and data[1::4].count(0) == units
            and (data[2::4].count(0) < units or data[3::4].count(0) < units)
        ):
            return "utf-32-be"
    if n >= 2:
        pairs = max(1, n // 2)
        even_nuls = data[0::2].count(0)
        odd_nuls = data[1::2].count(0)
        threshold = 1 if pairs <= 2 else max(2, pairs // 4)
        if odd_nuls >= threshold and odd_nuls > even_nuls * 3:
            return "utf-16-le"
        if even_nuls >= threshold and even_nuls > odd_nuls * 3:
            return "utf-16-be"
    return None


def _score_utf16_text(decoded: str) -> float:
    if not decoded:
        return -1e9
    score = 0.0
    for ch in decoded:
        cp = ord(ch)
        if cp in (0x09, 0x0A, 0x0D) or 0x20 <= cp <= 0x7E:
            score += 3.0
        elif (
            0x4E00 <= cp <= 0x9FFF
            or 0x3400 <= cp <= 0x4DBF
            or 0x3000 <= cp <= 0x303F
            or 0xFF01 <= cp <= 0xFF60
            or 0x2010 <= cp <= 0x203B
            or 0x00A0 <= cp <= 0x024F
        ):
            score += 3.0
        elif (
            cp < 0x20
            or 0x7F <= cp <= 0x9F
            or 0xE000 <= cp <= 0xF8FF
            or cp in (0x0900, 0x0A00, 0x0D00, 0x2000, 0xFFFE, 0xFFFF)
        ):
            score -= 8.0
        else:
            score -= 1.5
    return score


def _try_bomless_utf16(data: bytes, *, min_avg: float = 1.0) -> str | None:
    if len(data) < 2 or len(data) % 2 != 0:
        return None
    best_text: str | None = None
    best_score = -1e9
    for enc in ("utf-16-le", "utf-16-be"):
        try:
            candidate = data.decode(enc)
        except UnicodeDecodeError:
            continue
        if not candidate:
            continue
        score = _score_utf16_text(candidate)
        if score >= len(candidate) * min_avg and score > best_score:
            best_score = score
            best_text = candidate
    return best_text


def decode_transcript(data: bytes) -> str:
    """UTF-32/UTF-16 BOM first, then UTF-8 / BOM-less UTF-16 / GB18030.

    Replacement characters are not used: a damaged file must not become a meeting.
    """
    if not data or not data.strip(b"\x00 \t\r\n"):
        raise TranscriptDecodeError("文件是空的，没有可导入的文字。")
    if data.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        try:
            text = data.decode("utf-32")
        except UnicodeDecodeError as exc:
            raise TranscriptDecodeError("文字稿的 UTF-32 内容不完整，文件可能被截断。") from exc
    elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            text = data.decode("utf-16")
        except UnicodeDecodeError as exc:
            raise TranscriptDecodeError("文字稿的 UTF-16 内容不完整，文件可能被截断。") from exc
    else:
        text = None
        wide = _detect_bomless_utf_wide(data) if b"\x00" in data else None
        if wide is not None:
            if wide.startswith("utf-16") and len(data) % 2 != 0:
                raise TranscriptDecodeError("文字稿的 UTF-16 内容不完整，文件可能被截断。")
            try:
                text = data.decode(wide)
            except UnicodeDecodeError as exc:
                label = "UTF-32" if wide.startswith("utf-32") else "UTF-16"
                raise TranscriptDecodeError(f"文字稿的 {label} 内容不完整，文件可能被截断。") from exc
        if text is None:
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = None
        if text is None and b"\x00" in data:
            if len(data) % 2 != 0:
                raise TranscriptDecodeError("文字稿的 UTF-16 内容不完整，文件可能被截断。")
            text = _try_bomless_utf16(data, min_avg=1.0)
        if text is None:
            try:
                text = data.decode("gb18030")
            except UnicodeDecodeError as exc:
                text = _try_bomless_utf16(data, min_avg=2.0)
                if text is None:
                    raise TranscriptDecodeError("文件不是有效的 UTF-8 或 GB18030 文本。") from exc
    text = text.replace("\x00", "")
    if not text.strip():
        raise TranscriptDecodeError("文件里没有可导入的文字。")
    return text


def read_transcript_file(path: str | Path) -> str:
    return decode_transcript(Path(path).read_bytes())


def parse_transcript(text: str, *, detect_structure: bool = True) -> ImportedTranscript:
    normalized = (text or "").replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return ImportedTranscript("", (), "纯文本")
    if not detect_structure:
        title, turns = _plain_turns(normalized, extract_title=False)
        return ImportedTranscript(title, turns, "纯文本")
    json_parsed = _parse_json_transcript(normalized)
    if json_parsed is not None:
        return json_parsed
    if normalized.lstrip().upper().startswith("WEBVTT"):
        return _parse_cues(normalized, vtt=True)
    if "-->" in normalized:
        cues = _parse_cues(normalized, vtt=False)
        if cues.turns or (cues.discarded and _LOOSE_CUE_ARROW.search(normalized)):
            return cues
    for parser in (_parse_blocks, _parse_timed_dialogue, _parse_dialogue):
        parsed = parser(normalized)
        if parsed is not None:
            return parsed
    title, turns = _plain_turns(normalized, extract_title=True)
    return ImportedTranscript(title, turns, "纯文本")


def assign_speakers(turns: Sequence[ImportedTurn]) -> tuple[list[tuple[int, ImportedTurn]], dict[int, str]]:
    """Numbered labels keep their number. Real names get the next free id.

    The returned map is only the real names. Numbered speakers stay
    「说话人 N」 and are not stored as a display name.
    """
    used: set[int] = set()
    for turn in turns:
        match = _NUMBERED.match(turn.speaker_label.strip())
        if match and int(match.group(1)) > 0:
            used.add(int(match.group(1)))
    names: dict[str, int] = {}
    display: dict[int, str] = {}
    next_id = 1
    assigned: list[tuple[int, ImportedTurn]] = []
    for turn in turns:
        label = turn.speaker_label.strip()
        match = _NUMBERED.match(label) if label else None
        if not label:
            speaker_id = 0
        elif match:
            speaker_id = int(match.group(1))
            if speaker_id <= 0:
                speaker_id = 0
        else:
            speaker_id = names.get(label, 0)
            if speaker_id <= 0:
                while next_id in used:
                    next_id += 1
                speaker_id = next_id
                names[label] = speaker_id
                display[speaker_id] = label
                used.add(speaker_id)
                next_id += 1
        assigned.append((speaker_id, turn))
    return assigned, display


def _preview_clock(offset_ms: int | None) -> str:
    if offset_ms is None:
        return ""
    total = max(0, int(offset_ms)) // 1000
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"[{hours:02d}:{minutes:02d}:{seconds:02d}] "
    return f"[{minutes:02d}:{seconds:02d}] "


def describe_import(parsed: ImportedTranscript) -> str:
    lines: list[str] = []
    if not parsed.turns:
        lines.append("没有可导入的字幕。" if parsed.discarded else "还没有可导入的内容")
    else:
        speakers = {turn.speaker_label.strip() for turn in parsed.turns if turn.speaker_label.strip()}
        who = f"{len(speakers)} 位发言人" if speakers else "未区分说话人"
        when = "含时间戳" if parsed.has_timestamps else "无时间戳"
        lines.append(f"{parsed.format_label} · {len(parsed.turns)} 段 · {who} · {when}")
        for turn in parsed.turns[:3]:
            speaker = turn.speaker_label.strip() or "未标发言人"
            body = turn.text.splitlines()[0][:40]
            lines.append(f"{_preview_clock(turn.offset_ms)}{speaker}：{body}")
    if parsed.discarded:
        lines.append(f"有 {parsed.discarded} 条字幕没有导入。")
        if parsed.skipped:
            lines.append("未导入：" + "；".join(parsed.skipped[:2]))
    return "\n".join(lines)


def _clock_ms(
    first: str,
    second: str,
    third: str | None,
    *,
    wall_clock: bool = False,
    hint: str = "",
) -> int | None:
    """Two fields are minutes:seconds unless a calendar date/meridiem marks HH:MM."""
    left, right = int(first), int(second)
    if third is None:
        if wall_clock:
            hours, minutes, seconds = left, right, 0
            if hours > 23 or minutes >= 60:
                return None
        else:
            if left > 999 or right >= 60:
                return None
            hours, minutes = divmod(left, 60)
            seconds = right
    else:
        hours, minutes, seconds = left, right, int(third)
        if hours > 999 or minutes >= 60 or seconds >= 60:
            return None
    if hint:
        if _PM_HINT.search(hint) and 1 <= hours <= 11:
            hours += 12
        elif _AM_MIDNIGHT_HINT.search(hint) and hours == 12:
            hours = 0
    return ((hours * 60 + minutes) * 60 + seconds) * 1000


def _cue_ms(hours: str | None, minutes: str, seconds: str, millis: str) -> int | None:
    h = int(hours) if hours else 0
    m, s = int(minutes), int(seconds)
    ms = int(millis.ljust(3, "0"))
    if h > 999 or m >= 60 or s >= 60 or ms >= 1000:
        return None
    return ((h * 60 + m) * 60 + s) * 1000 + ms


def _clean_speaker(who: str) -> str:
    """Drop a trailing calendar date/meridiem and the parentheses around a clock."""
    cleaned = _TRAILING_DATE.sub("", who).strip().strip("()（）").strip()
    cleaned = _TRAILING_DATE.sub("", cleaned).strip().strip("()（）").strip()
    return cleaned


def _strip_cue_markup(text: str) -> str:
    cleaned = _VTT_INLINE_TIME.sub("", text)
    cleaned = _VTT_INLINE_TAG.sub("", cleaned)
    cleaned = html.unescape(cleaned).replace("\xa0", " ")
    return "\n".join(line.strip() for line in cleaned.split("\n") if line.strip()).strip()


def _plausible_label(who: str, *, limit: int) -> bool:
    label = who.strip()
    if not label or len(label) > limit:
        return False
    if any(mark in label for mark in _SENTENCE_PUNCT):
        return False
    folded = label.casefold()
    if folded in _NOT_NAMES or folded in {item.casefold() for item in _NOT_NAMES}:
        return False
    if "://" in label or label.startswith("//") or "/" in label:
        return False
    if label in _STOP_LABELS or _CLOCK_IN_LABEL.search(label):
        return False
    return True


def _split_speaker(body: str) -> tuple[str, str]:
    text = body.strip()
    voice = _VTT_VOICE.match(text)
    if voice:
        who = _strip_cue_markup(voice.group(1))
        rest_raw = voice.group(2).strip()
        if rest_raw.lower().endswith("</v>"):
            rest_raw = rest_raw[: -len("</v>")].strip()
        rest = _strip_cue_markup(rest_raw)
        if rest:
            return who, rest
    cleaned = _strip_cue_markup(text)
    match = _SPEAKER_PREFIX.match(cleaned)
    if match and (_plausible_label(match.group("who"), limit=16) or _NUMBERED.match(match.group("who").strip())):
        return match.group("who").strip(), match.group("body").strip()
    return "", cleaned


class _LaterClock:
    """Smallest timestamp strictly after one already seen, in logarithmic time."""

    def __init__(self, values: Sequence[int]) -> None:
        self.values = list(values)
        self.n = 1
        while self.n < max(len(self.values), 1):
            self.n *= 2
        self._has = [False] * (self.n * 2)

    def add(self, rank: int) -> None:
        index = rank + self.n
        self._has[index] = True
        index //= 2
        while index:
            self._has[index] = self._has[index * 2] or self._has[index * 2 + 1]
            index //= 2

    def minimum_above(self, rank: int) -> int | None:
        found = self._leftmost(1, 0, self.n, rank + 1)
        if found is None or found >= len(self.values):
            return None
        return found

    def _leftmost(self, node: int, start: int, end: int, target: int) -> int | None:
        if end <= target or not self._has[node]:
            return None
        if end - start == 1:
            return start
        mid = (start + end) // 2
        left = self._leftmost(node * 2, start, mid, target)
        if left is not None:
            return left
        return self._leftmost(node * 2 + 1, mid, end, target)


def _fill_durations(turns: Sequence[ImportedTurn]) -> tuple[ImportedTurn, ...]:
    """Duration runs until the next later clock, including clocks that are not adjacent.

    Equal and reverse-ordered clocks stay O(n log n) instead of scanning every
    later line for every row.
    """
    known = sorted({turn.offset_ms for turn in turns if turn.offset_ms is not None})
    rank = {value: index for index, value in enumerate(known)}
    later = _LaterClock(known)
    nxt: list[int | None] = [None] * len(turns)
    for index in range(len(turns) - 1, -1, -1):
        offset = turns[index].offset_ms
        if offset is None:
            continue
        found = later.minimum_above(rank[offset])
        if found is not None:
            nxt[index] = known[found]
        later.add(rank[offset])
    filled: list[ImportedTurn] = []
    for turn, duration_end in zip(turns, nxt, strict=True):
        if turn.offset_ms is None or turn.duration_ms or duration_end is None:
            filled.append(turn)
            continue
        filled.append(replace(turn, duration_ms=duration_end - turn.offset_ms))
    return tuple(filled)


def _parse_cues(text: str, *, vtt: bool) -> ImportedTranscript:
    lines = text.split("\n")
    index = 1 if vtt else 0
    turns: list[ImportedTurn] = []
    skipped: list[str] = []
    while index < len(lines):
        line = lines[index].strip()
        if not line:
            index += 1
            continue
        if vtt and _VTT_META_BLOCK.match(line):
            index += 1
            while index < len(lines) and lines[index].strip():
                index += 1
            continue
        timing = _CUE_TIME.search(line)
        header_line = line
        if timing is None and index + 1 < len(lines):
            next_line = lines[index + 1].strip()
            nxt = _CUE_TIME.search(next_line)
            if nxt is not None:
                timing = nxt
                header_line = next_line
                index += 1
            elif _LOOSE_CUE_ARROW.search(next_line):
                header_line = next_line
                index += 2
                bad_body: list[str] = []
                while index < len(lines) and lines[index].strip():
                    bad_body.append(lines[index].strip())
                    index += 1
                skipped.append(("\n".join(bad_body).strip() or header_line).replace("\n", " ")[:80])
                continue
        if timing is None:
            if _LOOSE_CUE_ARROW.search(line):
                index += 1
                bad_body = []
                while index < len(lines) and lines[index].strip():
                    bad_body.append(lines[index].strip())
                    index += 1
                skipped.append(("\n".join(bad_body).strip() or line).replace("\n", " ")[:80])
                continue
            index += 1
            continue
        start = _cue_ms(timing.group("sh"), timing.group("sm"), timing.group("ss"), timing.group("sms"))
        end = _cue_ms(timing.group("eh"), timing.group("em"), timing.group("es"), timing.group("ems"))
        index += 1
        body: list[str] = []
        while index < len(lines) and lines[index].strip():
            body.append(lines[index].strip())
            index += 1
        content_block = "\n".join(body).strip()
        if start is None or end is None or not content_block:
            skipped.append((content_block or header_line).replace("\n", " ")[:80])
            continue
        speaker, content = _split_speaker(content_block)
        if not content:
            skipped.append(content_block.replace("\n", " ")[:80])
            continue
        duration = end - start
        turns.append(ImportedTurn(speaker, start, duration if duration > 0 else 1000, content))
    return ImportedTranscript(
        "", tuple(turns), "WebVTT 字幕" if vtt else "SRT 字幕",
        discarded=len(skipped), skipped=tuple(skipped),
    )


def _match_block_header(raw: str) -> tuple[str, int] | None:
    stripped = raw.strip()
    date_first = _DATE_FIRST_HEADER.match(stripped)
    if date_first is not None:
        who = _clean_speaker(date_first.group("who"))
        if _plausible_label(who, limit=24) or _NUMBERED.match(who):
            hint = f"{date_first.group('date') or ''} {date_first.group('mer') or ''}"
            offset = _clock_ms(
                date_first.group("a"), date_first.group("b"), date_first.group("c"),
                wall_clock=True, hint=hint,
            )
            if offset is not None:
                return who, offset
    match = _BLOCK_HEADER.match(stripped)
    if match is None:
        return None
    raw_who = match.group("who")
    ampm = match.group("ampm") or ""
    wall_hint = f"{raw_who} {ampm}".strip()
    has_wall = bool(_HAS_WALL_HINT.search(raw_who) or ampm)
    who = _clean_speaker(raw_who)
    if not _plausible_label(who, limit=24) and not _NUMBERED.match(who):
        return None
    offset = _clock_ms(
        match.group("a"), match.group("b"), match.group("c"),
        wall_clock=has_wall, hint=wall_hint if has_wall else "",
    )
    if offset is None:
        return None
    return who, offset


def _parse_blocks(text: str) -> ImportedTranscript | None:
    lines = text.split("\n")
    headers: list[tuple[int, str, int]] = []
    for index, raw in enumerate(lines):
        parsed_header = _match_block_header(raw)
        if parsed_header is None:
            continue
        who, offset = parsed_header
        headers.append((index, who, offset))
    if len(headers) < 2:
        return None
    turns: list[ImportedTurn] = []
    preamble: list[str] = []
    for position, (index, who, offset) in enumerate(headers):
        if position == 0:
            preamble = [lines[i].strip() for i in range(index) if lines[i].strip()]
        end = headers[position + 1][0] if position + 1 < len(headers) else len(lines)
        body = [lines[i].strip() for i in range(index + 1, end) if lines[i].strip()]
        if body:
            turns.append(ImportedTurn(who, offset, 0, "\n".join(body)))
    if len(turns) < 2:
        return None
    title = ""
    if len(preamble) == 1 and len(preamble[0]) <= 40:
        title = preamble[0]
    elif preamble:
        turns.insert(0, ImportedTurn("", None, 0, "\n".join(preamble)))
    return ImportedTranscript(title, _fill_durations(turns), "带时间的发言记录")


def _parse_timed_line(line: str) -> ImportedTurn | None:
    bracket = _BRACKET.match(line)
    if bracket is not None:
        b_date = bracket.group("date") or ""
        b_mer = bracket.group("mer") or ""
        b_ampm = bracket.group("ampm") or ""
        has_wall = bool(b_date or b_mer or b_ampm)
        offset = _clock_ms(
            bracket.group("a"), bracket.group("b"), bracket.group("c"),
            wall_clock=has_wall, hint=f"{b_date} {b_mer} {b_ampm}".strip() if has_wall else "",
        )
        if offset is None:
            return None
        who = _clean_speaker(bracket.group("who") or "")
        body = bracket.group("body").strip()
        if who and not _plausible_label(who, limit=24) and not _NUMBERED.match(who):
            body = f"{who}：{body}"
            who = ""
        return ImportedTurn(who, offset, 0, body)
    date_colon = _DATE_FIRST_COLON.match(line)
    if date_colon is not None:
        who = _clean_speaker(date_colon.group("who"))
        if _plausible_label(who, limit=24) or _NUMBERED.match(who):
            offset = _clock_ms(
                date_colon.group("a"), date_colon.group("b"), date_colon.group("c"),
                wall_clock=True,
                hint=f"{date_colon.group('date') or ''} {date_colon.group('mer') or ''}".strip(),
            )
            if offset is not None:
                return ImportedTurn(who, offset, 0, date_colon.group("body").strip())
    timed = _TIMED_COLON.match(line)
    if timed is None:
        return None
    raw_who = timed.group("who")
    ampm = timed.group("ampm") or ""
    has_wall = bool(_HAS_WALL_HINT.search(raw_who) or ampm)
    who = _clean_speaker(raw_who)
    if not _plausible_label(who, limit=24) and not _NUMBERED.match(who):
        return None
    offset = _clock_ms(
        timed.group("a"), timed.group("b"), timed.group("c"),
        wall_clock=has_wall, hint=f"{raw_who} {ampm}".strip() if has_wall else "",
    )
    if offset is None:
        return None
    return ImportedTurn(who, offset, 0, timed.group("body").strip())


def _json_speaker_lookup(raw_speakers: Any) -> dict[int, str]:
    mapping: dict[int, str] = {}
    if isinstance(raw_speakers, dict):
        for key, val in raw_speakers.items():
            try:
                sid = int(key)
            except (TypeError, ValueError):
                continue
            if sid <= 0:
                continue
            if isinstance(val, str) and val.strip():
                mapping[sid] = val.strip()
            elif isinstance(val, dict):
                name = str(val.get("display_name") or val.get("name") or val.get("label") or "").strip()
                if name:
                    mapping[sid] = name
    elif isinstance(raw_speakers, list):
        for item in raw_speakers:
            if not isinstance(item, dict):
                continue
            try:
                sid = int(item.get("speaker_id") or item.get("id") or 0)
            except (TypeError, ValueError):
                continue
            name = str(item.get("display_name") or item.get("name") or item.get("label") or "").strip()
            if sid > 0 and name:
                mapping[sid] = name
    return mapping


def _json_item_text(item: dict[str, Any]) -> str:
    polished = item.get("polished_text")
    if polished == _SUPPRESSED_POLISHED_MARKER:
        return ""
    for key in ("corrected_text", "polished_text", "text", "content", "body", "raw_text"):
        val = item.get(key)
        if isinstance(val, str):
            cleaned = val.replace("\x00", "").replace(_SUPPRESSED_POLISHED_MARKER, "").strip()
            if cleaned:
                return cleaned
    return ""


def _json_item_speaker(item: dict[str, Any], speaker_map: dict[int, str]) -> str:
    for key in ("speaker_label", "speaker_name", "speaker"):
        val = item.get(key)
        if isinstance(val, int) and val > 0:
            return speaker_map.get(val, f"说话人{val}")
        if isinstance(val, str) and val.strip():
            raw = val.strip()
            if raw.isdigit() and int(raw) > 0:
                sid = int(raw)
                return speaker_map.get(sid, f"说话人{sid}")
            if raw == "说话人待确认":
                return ""
            return raw
    raw_sid = item.get("speaker_id")
    if isinstance(raw_sid, (int, float)) and not isinstance(raw_sid, bool):
        sid = int(raw_sid)
        if sid > 0:
            return speaker_map.get(sid, f"说话人{sid}")
    elif isinstance(raw_sid, str) and raw_sid.strip().isdigit():
        sid = int(raw_sid.strip())
        if sid > 0:
            return speaker_map.get(sid, f"说话人{sid}")
    return ""


def _json_int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(round(float(value)))
    return None


def _parse_json_transcript(text: str) -> ImportedTranscript | None:
    stripped = text.strip()
    if len(stripped) < 2:
        return None
    if not (
        (stripped.startswith("{") and stripped.endswith("}"))
        or (stripped.startswith("[") and stripped.endswith("]"))
    ):
        return None
    try:
        payload = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return None

    title = ""
    speaker_map: dict[int, str] = {}
    root_started_at: int | None = None
    items: list[dict[str, Any]] = []

    if isinstance(payload, list):
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            nested_segs = entry.get("segments")
            if isinstance(nested_segs, list) and nested_segs:
                for seg in nested_segs:
                    if isinstance(seg, dict):
                        items.append(seg)
            else:
                items.append(entry)
    elif isinstance(payload, dict):
        raw_title = payload.get("title")
        if isinstance(raw_title, str) and raw_title.strip():
            title = raw_title.strip()[:80]
        speaker_map = _json_speaker_lookup(payload.get("speakers"))
        root_started_at = _json_int_or_none(payload.get("started_at"))
        if root_started_at is None and isinstance(payload.get("meeting"), dict):
            meeting_meta = payload["meeting"]
            if not title and isinstance(meeting_meta.get("title"), str):
                title = meeting_meta["title"].strip()[:80]
            root_started_at = _json_int_or_none(meeting_meta.get("started_at"))
            if not speaker_map:
                speaker_map = _json_speaker_lookup(meeting_meta.get("speakers"))
        if isinstance(payload.get("sessions"), list):
            sessions = payload["sessions"]
            if not title and len(sessions) == 1 and isinstance(sessions[0], dict):
                preview = sessions[0].get("title") or sessions[0].get("preview")
                if isinstance(preview, str) and preview.strip():
                    title = preview.strip()[:80]
            for session_obj in sessions:
                if not isinstance(session_obj, dict):
                    continue
                segs = session_obj.get("segments")
                if isinstance(segs, list) and segs:
                    for seg in segs:
                        if isinstance(seg, dict):
                            items.append(seg)
                elif _json_item_text(session_obj):
                    items.append(session_obj)
        else:
            for list_key in ("segments", "turns", "utterances", "paragraphs", "items", "messages", "results"):
                candidate = payload.get(list_key)
                if isinstance(candidate, list):
                    items = [entry for entry in candidate if isinstance(entry, dict)]
                    break
            if not items and _json_item_text(payload):
                items = [payload]
    else:
        return None

    if not items:
        return None

    # Determine base epoch timestamp when rows carry SayInk epoch-ms clocks (captured_at / created_at).
    epoch_clocks: list[int] = []
    for item in items:
        if any(k in item for k in ("offset_ms", "start_ms", "start")):
            continue
        cap = _json_int_or_none(item.get("captured_at"))
        cre = _json_int_or_none(item.get("created_at"))
        clock = cap if (cap is not None and cap > 0) else (cre if (cre is not None and cre > 0) else None)
        if clock is not None:
            epoch_clocks.append(clock)
    base_epoch: int | None = None
    if epoch_clocks:
        earliest = min(epoch_clocks)
        if root_started_at is not None and 0 < root_started_at <= earliest:
            base_epoch = root_started_at
        else:
            base_epoch = earliest

    turns: list[ImportedTurn] = []
    for item in items:
        content = _json_item_text(item)
        if not content:
            continue
        speaker = _json_item_speaker(item, speaker_map)
        duration = max(0, _json_int_or_none(item.get("duration_ms")) or 0)
        offset: int | None = None
        if "offset_ms" in item and item["offset_ms"] is not None:
            raw_off = _json_int_or_none(item.get("offset_ms"))
            if raw_off is not None and raw_off >= 0:
                offset = raw_off
        elif "start_ms" in item and item["start_ms"] is not None:
            raw_off = _json_int_or_none(item.get("start_ms"))
            if raw_off is not None and raw_off >= 0:
                offset = raw_off
            end_ms = _json_int_or_none(item.get("end_ms"))
            if not duration and offset is not None and end_ms is not None and end_ms > offset:
                duration = end_ms - offset
        elif isinstance(item.get("start"), (int, float)) and not isinstance(item.get("start"), bool):
            start_s = float(item["start"])
            if start_s >= 0:
                offset = int(round(start_s * 1000))
                if not duration and isinstance(item.get("end"), (int, float)) and not isinstance(item.get("end"), bool):
                    end_ms = int(round(float(item["end"]) * 1000))
                    if end_ms > offset:
                        duration = end_ms - offset
        elif base_epoch is not None:
            cap = _json_int_or_none(item.get("captured_at"))
            cre = _json_int_or_none(item.get("created_at"))
            if cap is not None and cap < 0:
                offset = None
            else:
                clock = cap if (cap is not None and cap > 0) else (cre if (cre is not None and cre > 0) else None)
                if clock is not None and (
                    len(epoch_clocks) >= 2
                    or duration > 0
                    or (cap is not None and cap > 0)
                    or root_started_at is not None
                ):
                    offset = max(0, clock - base_epoch)
        turns.append(ImportedTurn(speaker, offset, duration, content))

    if not turns:
        return None
    stored = _fill_durations(turns) if any(t.offset_ms is not None for t in turns) else tuple(turns)
    return ImportedTranscript(title, stored, "JSON 记录")


def _parse_dialogue_line(line: str) -> ImportedTurn | None:
    if "://" in line:
        return None
    found = _DIALOGUE.match(line)
    if found is None:
        return None
    who = found.group("who").strip()
    if not _plausible_label(who, limit=12) and not _NUMBERED.match(who):
        return None
    return ImportedTurn(who, None, 0, found.group("body").strip())


def _parse_timed_dialogue(text: str) -> ImportedTranscript | None:
    return _parse_line_format(text, _parse_timed_line, "带时间的对话")


def _parse_dialogue(text: str) -> ImportedTranscript | None:
    return _parse_line_format(text, _parse_dialogue_line, "对话记录")


def _parse_line_format(
    text: str,
    match: Callable[[str], ImportedTurn | None],
    label: str,
) -> ImportedTranscript | None:
    """Use a line format only when it covers at least half of the non-empty lines."""
    nonempty = [(index, line.strip()) for index, line in enumerate(text.split("\n")) if line.strip()]
    if len(nonempty) < 2:
        return None
    matched: dict[int, ImportedTurn] = {}
    for index, line in nonempty:
        turn = match(line)
        if turn is not None:
            matched[index] = turn
    if len(matched) < 2 or len(matched) * 2 < len(nonempty):
        return None
    turns: list[ImportedTurn] = []
    for index, line in nonempty:
        if index in matched:
            turns.append(matched[index])
        elif turns:
            previous = turns[-1]
            turns[-1] = replace(previous, text=previous.text + "\n" + line)
        else:
            turns.append(ImportedTurn("", None, 0, line))
    title = ""
    if (
        turns and not turns[0].speaker_label and turns[0].offset_ms is None and len(turns) > 1
        and "\n" not in turns[0].text and len(turns[0].text) <= 40
    ):
        title = turns[0].text
        turns = turns[1:]
    if sum(1 for turn in turns if turn.speaker_label or turn.offset_ms is not None) < 2:
        return None
    stored = _fill_durations(turns) if any(turn.offset_ms is not None for turn in turns) else tuple(turns)
    return ImportedTranscript(title, stored, label)


def _plain_turns(text: str, *, extract_title: bool) -> tuple[str, tuple[ImportedTurn, ...]]:
    lines = text.split("\n")
    title = ""
    if extract_title:
        for index, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                continue
            heading = _H1.match(stripped)
            if heading:
                title = heading.group(1).strip()
                lines = lines[:index] + lines[index + 1:]
            break
    # One source line is one segment. Joining lines around a blank line hid
    # the edit action: it only sat after the last line of the paragraph.
    chunks = [line.strip() for line in lines if line.strip()]
    return title, tuple(ImportedTurn("", None, 0, chunk) for chunk in chunks if chunk)

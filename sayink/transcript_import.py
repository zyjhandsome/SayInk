"""Turn pasted or saved meeting text into ordered turns.

A format is used only when its cue repeats. A memo that happens to contain
one colon stays plain text, and the caller decides whether missing speakers
or timestamps may be shown to the model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Sequence

IMPORT_SOURCE = "import_text"

_STOP_LABELS = frozenset({
    "背景", "注意", "摘要", "议程", "时间", "参会人", "标题", "说明", "备注", "总结", "结论", "主题",
})
_NOT_NAMES = _STOP_LABELS | {"http", "https", "ftp", "file", "mailto"}
_NUMBERED = re.compile(r"^(?:说话人|发言人|speaker)\s*(\d+)$", re.IGNORECASE)
_CLOCK_IN_LABEL = re.compile(r"\d{1,2}:\d{2}")
_SENTENCE_PUNCT = "。！？!?，,"
_CUE_TIME = re.compile(
    r"(?:(?P<sh>\d{2,}):)?(?P<sm>\d{2}):(?P<ss>\d{2})[.,](?P<sms>\d{3})\s*-->\s*"
    r"(?:(?P<eh>\d{2,}):)?(?P<em>\d{2}):(?P<es>\d{2})[.,](?P<ems>\d{3})"
)
_CLOCK = r"(?P<a>\d{1,2}):(?P<b>\d{2})(?::(?P<c>\d{2}))?"
_TRAILING_DATE = re.compile(r"\s+\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?$")
_BLOCK_HEADER = re.compile(rf"^(?P<who>.{{1,40}}?)\s*[\(（]?\s*{_CLOCK}[\)）]?\s*$")
_TIMED_COLON = re.compile(
    rf"^(?P<who>.{{1,40}}?)\s*[\(（]?\s*{_CLOCK}[\)）]?\s*[：:]\s*(?P<body>\S.*)$"
)
_BRACKET = re.compile(
    r"^\[(?P<a>\d{1,2}):(?P<b>\d{2})(?::(?P<c>\d{2}))?\]\s*(?:(?P<who>[^：:\]]{1,24})[：:])?\s*(?P<body>\S.*)$"
)
_DIALOGUE = re.compile(r"^(?P<who>[^：:\n]{1,12})[：:]\s*(?P<body>\S.*)$")
_SPEAKER_PREFIX = re.compile(r"^(?P<who>[^：:\n]{1,16})[：:]\s*(?P<body>\S.*)$", re.DOTALL)
_VTT_VOICE = re.compile(r"^<v(?:\.[^ >]+)?\s+([^>]+)>(.*?)(?:</v>)?\s*$", re.DOTALL | re.IGNORECASE)
_H1 = re.compile(r"^#\s+(.+?)\s*$")


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


def decode_transcript(data: bytes) -> str:
    """UTF-8 (with BOM) first, then GB18030. Truncated or unknown bytes are an error.

    Replacement characters are not used: a damaged file must not become a meeting.
    """
    if not data or not data.strip(b"\x00 \t\r\n"):
        raise TranscriptDecodeError("文件是空的，没有可导入的文字。")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            text = data.decode("utf-16")
        except UnicodeDecodeError as exc:
            raise TranscriptDecodeError("文字稿的 UTF-16 内容不完整，文件可能被截断。") from exc
    else:
        text = None
        for encoding in ("utf-8-sig", "gb18030"):
            try:
                text = data.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            raise TranscriptDecodeError("文件不是有效的 UTF-8 或 GB18030 文本。")
    if not text.strip():
        raise TranscriptDecodeError("文件里没有可导入的文字。")
    return text


def read_transcript_file(path: str | Path) -> str:
    return decode_transcript(Path(path).read_bytes())


def parse_transcript(text: str, *, detect_structure: bool = True) -> ImportedTranscript:
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return ImportedTranscript("", (), "纯文本")
    if not detect_structure:
        title, turns = _plain_turns(normalized, extract_title=False)
        return ImportedTranscript(title, turns, "纯文本")
    if normalized.lstrip().upper().startswith("WEBVTT"):
        return _parse_cues(normalized, vtt=True)
    if "-->" in normalized:
        cues = _parse_cues(normalized, vtt=False)
        if cues.turns:
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


def _clock_ms(first: str, second: str, third: str | None) -> int | None:
    """Two fields are minutes:seconds. Three fields are hours:minutes:seconds."""
    left, right = int(first), int(second)
    if third is None:
        hours, minutes, seconds = 0, left, right
    else:
        hours, minutes, seconds = left, right, int(third)
    if hours > 99 or minutes >= 60 or seconds >= 60:
        return None
    return ((hours * 60 + minutes) * 60 + seconds) * 1000


def _cue_ms(hours: str | None, minutes: str, seconds: str, millis: str) -> int | None:
    h = int(hours) if hours else 0
    m, s, ms = int(minutes), int(seconds), int(millis)
    if h > 99 or m >= 60 or s >= 60 or ms >= 1000:
        return None
    return ((h * 60 + m) * 60 + s) * 1000 + ms


def _clean_speaker(who: str) -> str:
    """Drop a trailing calendar date and the parentheses around a clock."""
    return _TRAILING_DATE.sub("", who).strip().strip("()（）")


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
        who, rest = voice.group(1).strip(), voice.group(2).strip()
        if rest.lower().endswith("</v>"):
            rest = rest[: -len("</v>")].strip()
        if rest:
            return who, rest
    match = _SPEAKER_PREFIX.match(text)
    if match and (_plausible_label(match.group("who"), limit=16) or _NUMBERED.match(match.group("who").strip())):
        return match.group("who").strip(), match.group("body").strip()
    return "", text


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
        upper = line.upper()
        if vtt and upper.startswith(("NOTE", "STYLE")):
            index += 1
            while index < len(lines) and lines[index].strip():
                index += 1
            continue
        timing = _CUE_TIME.search(line)
        if timing is None and index + 1 < len(lines):
            nxt = _CUE_TIME.search(lines[index + 1].strip())
            if nxt is not None:
                timing = nxt
                index += 1
        if timing is None:
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
            skipped.append((content_block or line).replace("\n", " ")[:80])
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


def _parse_blocks(text: str) -> ImportedTranscript | None:
    lines = text.split("\n")
    headers: list[tuple[int, str, int]] = []
    for index, raw in enumerate(lines):
        match = _BLOCK_HEADER.match(raw.strip())
        if match is None:
            continue
        who = _clean_speaker(match.group("who"))
        if not _plausible_label(who, limit=24) and not _NUMBERED.match(who):
            continue
        offset = _clock_ms(match.group("a"), match.group("b"), match.group("c"))
        if offset is None:
            continue
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
        offset = _clock_ms(bracket.group("a"), bracket.group("b"), bracket.group("c"))
        if offset is None:
            return None
        who = _clean_speaker(bracket.group("who") or "")
        body = bracket.group("body").strip()
        if who and not _plausible_label(who, limit=24) and not _NUMBERED.match(who):
            body = f"{who}：{body}"
            who = ""
        return ImportedTurn(who, offset, 0, body)
    timed = _TIMED_COLON.match(line)
    if timed is None:
        return None
    who = _clean_speaker(timed.group("who"))
    if not _plausible_label(who, limit=24) and not _NUMBERED.match(who):
        return None
    offset = _clock_ms(timed.group("a"), timed.group("b"), timed.group("c"))
    if offset is None:
        return None
    return ImportedTurn(who, offset, 0, timed.group("body").strip())


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

"""Conservative, local speaker labels backed by explicit source introductions.

Voice clustering identifies groups, not legal identities. Neither a participant
list, a passing mention nor a model's suggested name establishes who spoke.
"""
from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping, Sequence

from sayink.minutes_prompts import TranscriptLine

_SURNAMES = frozenset(
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
    "戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳鲍史唐"
    "费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于傅皮卞齐康伍余元卜顾孟平黄"
    "和穆萧尹姚邵汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁"
    "杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌"
    "霍虞万支柯管卢莫经房裘缪干解应宗丁宣邓郁单杭洪包诸左石崔吉龚程"
    "邢裴陆荣翁荀羊甄曲封芮储靳段富巫乌焦巴牧山谷车侯全班仰秋仲伊宫"
    "宁仇栾甘厉戎祖武符刘景詹束龙叶幸司韶黎乔苍双闻莘党翟谭贡劳姬申"
    "扶堵冉宰郦雍璩桑桂濮牛寿通边燕尚农温庄晏柴瞿阎连茹习艾鱼容向古"
    "易廖庾终暨居衡步都耿满弘匡文寇广禄阙东欧殳沃利蔚越夔隆师巩聂晁"
    "勾敖冷辛阚简饶曾关蒯查竺权逯盖益桓公肖"
)
_COMPOUND_SURNAMES = ("欧阳", "司马", "上官", "诸葛", "东方", "皇甫", "尉迟", "公孙", "慕容", "司徒", "司空", "夏侯")
_NAME = r"[\u4e00-\u9fff]{2,4}|[A-Z][a-z]+(?:[ -][A-Z][a-z]+){0,2}"
_INTRO = re.compile(
    r"(?:^|[，,。.!！；;、\n])\s*(?:(?:大家好|各位好|你好|您好|嗯|啊|好的)[，,、！!。\s]*)*"
    r"(?:我叫|我的名字(?:是|叫)|我是|我这边是)[\s：:]*"
    rf"(?P<name>{_NAME})(?=$|[，,。.!！；;、\s])"
)
_EN_INTRO = re.compile(r"(?:^|[,.!;\n])\s*(?:Hello[,! ]+|Hi[,! ]+)?My name is (?P<name>[A-Z][a-z]+(?:[ -][A-Z][a-z]+){0,2})(?=$|[,.!\s])")
_DENIAL = re.compile(rf"(?:我不叫|我不是)\s*(?P<name>{_NAME})(?=$|[，,。.!！；;、\s])")
_UNSAFE = re.compile(r"[\"“”‘’「」『』《》？?]|假设|假如|假定|如果|可能|也许|好像|比如|举例|扮演|台词|原话|转述|自称|他说|她说|他说过|她说过|不对|更正|纠正")
_REPORTED = re.compile(r"(?:说|说过|讲过|表示|提到)[，,:：]|\b(?:said|says|quoted|example|pretend|imagine)\b", re.IGNORECASE)
_CLAUSES = re.compile(r"[，,。.!！；;、\n]+")
_BARE_NAME = re.compile(rf"(?:{_NAME})")
_SPELLING = re.compile(r"^(?P<char>[\u4e00-\u9fff])(?:是|就是)(?P<word>[\u4e00-\u9fff]{2,6})的(?P=char)$")
_SHORT_SPELLING = re.compile(r"^(?P<word>[\u4e00-\u9fff]{2,6})的(?P<char>[\u4e00-\u9fff])$")
_SPELLING_GAP_MS = 30_000
_NON_NAMES = re.compile(r"负责|经理|老师|总|助理|主任|同事|先生|女士|用户|客户|系统|公司|项目|测试|王者|主人|中国|北京|上海|开发|工程|管理员|明天|今天|高兴|高管|主任")


# A self-introduction that merely starts with a surname character is still a
# role or origin (程序员、江西人). Auto-naming would rather miss a name.
_ROLE_ENDINGS = ("人", "员", "生", "师", "工", "头", "党", "医", "兵", "贩", "栈", "手", "官")
_NOT_PERSONAL_NAMES = frozenset({"司机", "房东", "黄牛", "路人", "全栈"})


def _plausible_name(name: str) -> bool:
    if name.isascii():
        return name not in {"DeepSeek", "SayInk", "Windows", "Python", "Assistant", "Unknown", "None", "Null"}
    if _NON_NAMES.search(name) or name in _NOT_PERSONAL_NAMES or name.endswith(_ROLE_ENDINGS):
        return False
    if name.startswith(_COMPOUND_SURNAMES):
        return len(name) in (3, 4)
    return len(name) in (2, 3) and name[0] in _SURNAMES


def _one_character_change(old: str, new: str) -> str:
    if old.isascii() or new.isascii() or len(old) != len(new):
        return ""
    changes = [after for before, after in zip(old, new, strict=True) if before != after]
    return changes[0] if len(changes) == 1 else ""


def _spelled_names(text: str, introduced: set[str], context: set[str]) -> tuple[set[str], dict[str, str], bool]:
    """A repeated full name plus explicit character spelling can resolve ASR.

    Never guess homophones: all but one character must agree with the recent
    self-introduction, and the changed character must be explicitly explained.
    A repeated, unexplained variant remains a conflict rather than a correction.
    """
    repeated = set(introduced)
    bare_names = set()
    chars = set()
    for clause in _CLAUSES.split(text):
        clause = clause.strip()
        if _BARE_NAME.fullmatch(clause) and _plausible_name(clause):
            repeated.add(clause)
            bare_names.add(clause)
        spelling = _SPELLING.fullmatch(clause) or _SHORT_SPELLING.fullmatch(clause)
        if spelling and spelling.group("char") in spelling.group("word"):
            chars.add(spelling.group("char"))
    related = {name for name in repeated if any(name == old or _one_character_change(old, name) for old in context)}
    targets = {name for name in related if any(_one_character_change(old, name) in chars for old in context)}
    # More than one possible corrected full name is still ambiguous.
    corrections = {}
    ambiguous = len(related & bare_names) > 1 or bool(chars and related and not any(chars <= set(name) for name in related))
    if len(targets) == 1 and not ambiguous:
        target = next(iter(targets))
        corrections = {old: target for old in context if _one_character_change(old, target) in chars}
    return related, corrections, ambiguous


def infer_speaker_names(
    lines: Sequence[TranscriptLine], *, manual_names: Mapping[int, str] | None = None,
) -> dict[int, str]:
    """Return only unique, uncontradicted introductions from original ASR text.

    A named addressee followed by 'yes' is deliberately insufficient. Multiple
    names within one voice group, or one name in multiple groups, are ambiguous;
    do not merge voice groups or choose a winner. Manual labels always win.
    """
    candidates: dict[int, set[str]] = defaultdict(set)
    denied: dict[int, set[str]] = defaultdict(set)
    ambiguous_speakers: set[int] = set()
    previous_line: TranscriptLine | None = None
    previous_names: set[str] = set()
    for line in lines:
        if line.speaker_id <= 0:
            previous_line, previous_names = None, set()
            continue
        text = line.text.strip()
        denied[line.speaker_id].update(m.group("name") for m in _DENIAL.finditer(text))
        if _UNSAFE.search(text) or _REPORTED.search(text):
            previous_line, previous_names = None, set()
            continue
        introduced = {
            match.group("name") for pattern in (_INTRO, _EN_INTRO) for match in pattern.finditer(text)
            if _plausible_name(match.group("name"))
        }
        context = set(introduced)
        if (previous_line is not None and previous_line.speaker_id == line.speaker_id
                and 0 <= line.offset_ms - previous_line.offset_ms <= _SPELLING_GAP_MS):
            context.update(previous_names)
        known = candidates[line.speaker_id] | introduced
        related, corrections, ambiguous = _spelled_names(text, introduced, known) if known else (set(), {}, False)
        # Stale or interrupted evidence may expose a conflict, but cannot
        # silently correct a name across an unrelated intervening turn.
        corrections = {old: name for old, name in corrections.items() if old in context}
        if ambiguous:
            ambiguous_speakers.add(line.speaker_id)
        names = candidates[line.speaker_id]
        names.update(introduced | related)
        for old, corrected in corrections.items():
            names.discard(old)
            names.add(corrected)
        previous_names = {corrections.get(name, name) for name in context | related} if related and context else set()
        previous_line = line

    owners: dict[str, set[int]] = defaultdict(set)
    for sid, names in candidates.items():
        for name in names:
            owners[name.casefold()].add(sid)
    for sid, name in (manual_names or {}).items():
        if name:
            owners[name.strip().casefold()].add(sid)
    return {
        sid: next(iter(names)) for sid, names in candidates.items()
        if len(names) == 1 and not names.intersection(denied[sid])
        and sid not in ambiguous_speakers
        and len(owners[next(iter(names)).casefold()]) == 1
        and sid not in (manual_names or {})
    }

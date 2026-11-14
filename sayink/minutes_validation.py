"""Validate provider output before it can become a saved meeting document."""
from __future__ import annotations

import re

from sayink.text_polisher import LLMResponseError, strip_model_thinking

LIST_FIELDS = ("topics", "decisions", "action_items", "open_questions", "chapters", "speakers")
# The text an item cannot do without; items missing it are dropped, not fatal.
# A speaker entry only needs its label: the renderer shows one without a summary.
ITEM_FIELDS = {
    "decisions": ("text",), "action_items": ("task",), "open_questions": ("text",),
    "chapters": ("title",), "speakers": ("label",),
}
_STRING_ITEM_PRIMARY_FIELD = {
    "topics": "title",
    "decisions": "text",
    "action_items": "task",
    "open_questions": "text",
    "chapters": "title",
    "speakers": "label",
}
_STRICT_TEXT_KEYS = ("title", "label", "text", "task", "summary")
_KNOWN_OPTIONAL_KEYS = ("owner", "due", "ts", "evidence_ts", "start", "end", "suggested_name")


def _coerce_item(raw_item, key: str) -> dict:
    """Normalise string list entries commonly returned by local/3rd-party LLMs."""
    if isinstance(raw_item, str):
        primary = _STRING_ITEM_PRIMARY_FIELD[key]
        coerced: dict = {primary: raw_item.strip()}
        if key == "topics":
            coerced["points"] = []
        return coerced
    if isinstance(raw_item, dict):
        return dict(raw_item)
    raise LLMResponseError(f"纪要字段 {key} 的条目格式错误")


def _coerce_text_fields(item: dict, key: str) -> None:
    """``null`` where the prompt asked for an empty string is a format the
    renderer already tolerates; only genuinely wrong types are errors."""
    for name, value in list(item.items()):
        if name == "points":
            continue
        if value is None:
            item[name] = ""
        elif isinstance(value, str):
            continue
        elif name not in ITEM_FIELDS.get(key, ()) and name not in _STRICT_TEXT_KEYS:
            if name in _KNOWN_OPTIONAL_KEYS:
                if isinstance(value, list) and all(isinstance(v, str) for v in value):
                    item[name] = "、".join(v.strip() for v in value if v.strip())
                elif isinstance(value, (int, float)) and not isinstance(value, bool):
                    item[name] = str(value)
                else:
                    item.pop(name, None)
            else:
                item.pop(name, None)
        else:
            raise LLMResponseError(f"纪要条目字段 {name} 格式错误")


def _clean_points(points) -> list:
    if points is None:
        return []
    if isinstance(points, str):
        points = [points] if points.strip() else []
    if not isinstance(points, list):
        raise LLMResponseError("议题要点格式错误")
    kept = []
    for raw_point in points:
        if isinstance(raw_point, str):
            point = {"text": raw_point.strip()}
        elif isinstance(raw_point, dict):
            point = dict(raw_point)
        else:
            raise LLMResponseError("议题要点格式错误")
        for name, value in list(point.items()):
            if value is None:
                point[name] = ""
            elif not isinstance(value, str):
                raise LLMResponseError("议题要点格式错误")
        if point.get("text", "").strip():
            kept.append(point)
    return kept


def validate_minutes(payload):
    """Accept omitted optional sections, but reject malformed or empty documents.

    Models routinely answer ``null`` for fields the prompt told them to leave
    empty, or emit bare strings inside list sections; those are normalised and
    items without their required text are dropped. Wrong types (a list where
    text belongs, a string where a list belongs) still reject the document.
    """
    if not isinstance(payload, dict) or not payload:
        raise LLMResponseError("未返回有效的纪要内容")
    for key in ("title", "summary"):
        if key in payload:
            if payload[key] is None:
                payload[key] = ""
            elif not isinstance(payload[key], str):
                raise LLMResponseError(f"纪要字段 {key} 格式错误")
    meaningful = bool(payload.get("summary", "").strip())
    for key in LIST_FIELDS:
        if key not in payload:
            continue
        items = payload[key]
        if items is None:
            payload[key] = []
            continue
        if not isinstance(items, list):
            raise LLMResponseError(f"纪要字段 {key} 应为列表")
        kept = []
        for raw_item in items:
            was_str = isinstance(raw_item, str)
            item = _coerce_item(raw_item, key)
            _coerce_text_fields(item, key)
            if key == "topics":
                item.setdefault("title", "")
                item["points"] = _clean_points(item.get("points"))
                if not isinstance(item.get("title"), str):
                    raise LLMResponseError("议题缺少标题或要点")
                if not item["title"].strip() and not item["points"]:
                    continue
                meaningful |= bool(item["points"]) or (was_str and bool(item["title"].strip()))
            else:
                if any(not isinstance(item.get(name), str) or not item[name].strip() for name in ITEM_FIELDS[key]):
                    continue
                meaningful = True
            kept.append(item)
        payload[key] = kept
    if not meaningful:
        raise LLMResponseError("纪要内容为空，本次结果未保存")
    return payload


def validate_markdown(text: str) -> str:
    """A format fallback still needs actual document content."""
    text = strip_model_thinking(text or "").strip()
    body = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith(("#", "```"))]
    if text.startswith(("{", "[")) or not re.search(r"(?m)^#{1,3}\s+\S", text) or not body:
        raise LLMResponseError("模型未返回有效的 Markdown 正文，本次结果未保存")
    return text

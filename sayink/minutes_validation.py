"""Validate provider output before it can become a saved meeting document."""
from __future__ import annotations

import re

from sayink.text_polisher import LLMResponseError

LIST_FIELDS = ("topics", "decisions", "action_items", "open_questions", "chapters", "speakers")
# The text an item cannot do without; items missing it are dropped, not fatal.
# A speaker entry only needs its label: the renderer shows one without a summary.
ITEM_FIELDS = {
    "decisions": ("text",), "action_items": ("task",), "open_questions": ("text",),
    "chapters": ("title",), "speakers": ("label",),
}


def _coerce_text_fields(item: dict, key: str) -> None:
    """``null`` where the prompt asked for an empty string is a format the
    renderer already tolerates; only genuinely wrong types are errors."""
    for name, value in list(item.items()):
        if name == "points":
            continue
        if value is None:
            item[name] = ""
        elif not isinstance(value, str):
            raise LLMResponseError(f"纪要条目字段 {name} 格式错误")


def _clean_points(points) -> list:
    if points is None:
        return []
    if not isinstance(points, list):
        raise LLMResponseError("议题要点格式错误")
    kept = []
    for point in points:
        if not isinstance(point, dict):
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
    empty; those are normalised to ``""`` / ``[]`` and items without their
    required text are dropped. Wrong types (a list where text belongs, a
    string where a list belongs) still reject the document.
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
        for item in items:
            if not isinstance(item, dict):
                raise LLMResponseError(f"纪要字段 {key} 的条目格式错误")
            _coerce_text_fields(item, key)
            if key == "topics":
                item["points"] = _clean_points(item.get("points"))
                if not isinstance(item.get("title"), str):
                    raise LLMResponseError("议题缺少标题或要点")
                if not item["title"].strip() and not item["points"]:
                    continue
                meaningful |= bool(item["points"])
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
    text = text.strip()
    body = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith(("#", "```"))]
    if text.startswith(("{", "[")) or not re.search(r"(?m)^#{1,3}\s+\S", text) or not body:
        raise LLMResponseError("模型未返回有效的 Markdown 正文，本次结果未保存")
    return text

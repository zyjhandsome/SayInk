"""Validate provider output before it can become a saved meeting document."""
from __future__ import annotations

import re

from sayink.text_polisher import LLMResponseError

LIST_FIELDS = ("topics", "decisions", "action_items", "open_questions", "chapters", "speakers")
ITEM_FIELDS = {
    "decisions": ("text",), "action_items": ("task",), "open_questions": ("text",),
    "chapters": ("title",), "speakers": ("label", "summary"),
}


def validate_minutes(payload):
    """Accept omitted optional sections, but reject malformed or empty documents."""
    if not isinstance(payload, dict) or not payload:
        raise LLMResponseError("未返回有效的纪要内容")
    for key in ("title", "summary"):
        if key in payload and not isinstance(payload[key], str):
            raise LLMResponseError(f"纪要字段 {key} 格式错误")
    meaningful = bool(payload.get("summary", "").strip())
    for key in LIST_FIELDS:
        if key not in payload:
            continue
        items = payload[key]
        if not isinstance(items, list):
            raise LLMResponseError(f"纪要字段 {key} 应为列表")
        for item in items:
            if not isinstance(item, dict):
                raise LLMResponseError(f"纪要字段 {key} 的条目格式错误")
            for name, value in item.items():
                if name == "points":
                    if not isinstance(value, list) or any(
                        not isinstance(p, dict) or not isinstance(p.get("text"), str) or not p["text"].strip()
                        or any(not isinstance(v, str) for v in p.values()) for p in value
                    ):
                        raise LLMResponseError("议题要点格式错误")
                elif value is not None and not isinstance(value, str):
                    raise LLMResponseError(f"纪要条目字段 {name} 格式错误")
            if key == "topics":
                if not isinstance(item.get("title"), str) or not isinstance(item.get("points"), list):
                    raise LLMResponseError("议题缺少标题或要点")
                meaningful |= bool(item["points"])
            else:
                if any(not isinstance(item.get(name), str) or not item[name].strip() for name in ITEM_FIELDS[key]):
                    raise LLMResponseError(f"纪要字段 {key} 缺少正文")
                meaningful = True
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

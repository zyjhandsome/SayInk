"""Stateless session formatting and Markdown/YAML/plain-text/CSV/JSON export builders for HistoryWindow."""

from __future__ import annotations

import csv
from datetime import datetime
import html
import io
import json
from pathlib import Path
import re
import time
from typing import Iterable

from sayink.history_store import SegmentRecord, SessionSummary

_SUPPRESSED_POLISHED_MARKER = "\u200b"
_YAML_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
_YAML_KEYWORDS = frozenset({"true", "false", "null", "yes", "no", "on", "off", "~"})
_YAML_SPECIAL_CHARS = frozenset(':#"\'\\[]{}&*!|>%@`')
_CSV_FORMULA_LEADERS = ("=", "+", "-", "@", "\t", "\r")


def _safe_dt(ms: int) -> datetime:
    try:
        numeric = int(ms)
    except (TypeError, ValueError, OverflowError):
        return datetime(1970, 1, 1, 0, 0, 0)
    try:
        return datetime.fromtimestamp(numeric / 1000)
    except (OSError, OverflowError, ValueError):
        if numeric < 0:
            try:
                return datetime.fromtimestamp(0)
            except (OSError, OverflowError, ValueError):
                return datetime(1970, 1, 1, 0, 0, 0)
        return datetime(9999, 12, 31, 23, 59, 59)


def _format_dt(ms: int) -> str:
    return _safe_dt(ms).strftime("%Y-%m-%d %H:%M:%S")


def _day_label(ms: int) -> str:
    day = _safe_dt(ms).date()
    today = datetime.now().date()
    delta = (today - day).days
    if delta == 0:
        return "今天"
    if delta == 1:
        return "昨天"
    return day.strftime("%Y-%m-%d")


def _filename_dt(ms: int) -> str:
    return _safe_dt(ms).strftime("%Y%m%d-%H%M%S")


def _effective_text(segment: SegmentRecord, *, prefer_raw: bool = False) -> str:
    if prefer_raw:
        return segment.raw_text or (
            "" if segment.polished_text == _SUPPRESSED_POLISHED_MARKER else segment.polished_text
        )
    if segment.polished_text == _SUPPRESSED_POLISHED_MARKER:
        return ""
    return segment.polished_text or segment.raw_text


def _one_line(value: object) -> str:
    if value is None:
        return ""
    return re.sub(r"[\r\n\t]+", " ", str(value)).strip()


def _yaml_value(value: object) -> str:
    cleaned = _one_line(value)
    if not cleaned:
        return ""
    if _YAML_DATETIME_RE.fullmatch(cleaned):
        return cleaned
    needs_quotes = (
        cleaned.casefold() in _YAML_KEYWORDS
        or cleaned[0] in "-?,"
        or any(ch in _YAML_SPECIAL_CHARS for ch in cleaned)
    )
    if needs_quotes:
        escaped = cleaned.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return cleaned


def _segments_in_order(segments: list[SegmentRecord]) -> list[SegmentRecord]:
    return sorted(segments, key=lambda s: s.seq)


def _session_duration_ms(segments: list[SegmentRecord]) -> int:
    return sum(max(0, int(s.duration_ms)) for s in segments)


def _session_model(segments: list[SegmentRecord]) -> str:
    return next((s.model for s in segments if s.model), "")


def history_shows_speakers(segments: list[SegmentRecord]) -> bool:
    """Label multiple voices, or uncertainty alongside an identified voice."""
    ids = {int(segment.speaker_id) for segment in segments if int(segment.speaker_id) > 0}
    return len(ids) >= 2 or bool(ids) and any(
        segment.trigger_mode == "continuous" and int(segment.speaker_id) == 0 for segment in segments
    )


def _segment_clock_ms(segment: SegmentRecord) -> int:
    """When the person spoke, or when the line was saved if that is unknown."""
    captured = int(getattr(segment, "captured_at", 0) or 0)
    if captured > 0:
        return captured
    return int(segment.created_at)


def _route_caption(route: str) -> str:
    return {"mic": "麦克风", "system": "电脑声音"}.get((route or "").strip(), "")


def _speaker_heading(segment: SegmentRecord) -> str:
    heading = f"说话人 {int(segment.speaker_id)}" if int(segment.speaker_id) > 0 else "说话人待确认"
    route = _route_caption(segment.speaker_route)
    if route:
        return f"{heading} · {route}"
    return heading


def _labeled_text(segment: SegmentRecord, text: str, *, show_speakers: bool) -> str:
    body = text.strip()
    if not body:
        return ""
    if show_speakers:
        return f"{_speaker_heading(segment)}\n{body}"
    return body


def _session_body(segments: list[SegmentRecord], *, prefer_raw: bool = False) -> str:
    ordered = _segments_in_order(segments)
    show_speakers = history_shows_speakers(ordered)
    chunks = [
        _labeled_text(
            segment,
            _effective_text(segment, prefer_raw=prefer_raw),
            show_speakers=show_speakers,
        )
        for segment in ordered
    ]
    return "\n\n".join(chunk for chunk in chunks if chunk)


def _format_duration(ms: int) -> str:
    seconds = max(0, int(ms)) // 1000
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {seconds:02d} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 时 {minutes:02d} 分"


def _session_title(created_at: int) -> str:
    return f"{_day_label(created_at)} {_safe_dt(created_at).strftime('%H:%M')}"


def _highlight_html(text: str, query: str, mark_css: str) -> str:
    escaped = html.escape(text)
    q = (query or "").strip()
    if not q:
        return escaped
    pattern = re.compile(re.escape(html.escape(q)), re.IGNORECASE)
    return pattern.sub(lambda m: f'<span style="{mark_css}">{m.group(0)}</span>', escaped)


def export_directory() -> str:
    """A SayInk folder under Documents, so export does not open in the app's working directory."""
    home = Path.home()
    documents = home / "Documents"
    base = documents if documents.is_dir() else home
    target = base / "SayInk"
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        return str(base)
    return str(target)


def export_save_path(filename: str) -> str:
    return str(Path(export_directory()) / filename)


def suggest_single_export_filename(session: SessionSummary) -> str:
    return f"sayink-{_filename_dt(session.created_at)}.md"


def suggest_batch_export_filename(*, now_ms: int | None = None) -> str:
    stamp = int(time.time() * 1000) if now_ms is None else now_ms
    return f"sayink-export-{_filename_dt(stamp)}.md"


def build_single_session_plain(
    session: SessionSummary,
    segments: list[SegmentRecord],
    *,
    prefer_raw: bool = False,
) -> str:
    del session
    return _session_body(segments, prefer_raw=prefer_raw).rstrip() + "\n"


def build_single_session_markdown(
    session: SessionSummary,
    segments: list[SegmentRecord],
    *,
    prefer_raw: bool = False,
) -> str:
    frontmatter = [
        "---",
        f"created_at: {_yaml_value(_format_dt(session.created_at))}",
        f"source: {_yaml_value(session.source)}",
        f"target_app: {_yaml_value(session.target_app)}",
        f"duration_ms: {_session_duration_ms(segments)}",
        f"model: {_yaml_value(_session_model(segments))}",
        f"segment_count: {session.segment_count}",
        "---",
        "",
    ]
    return "\n".join(frontmatter) + _session_body(segments, prefer_raw=prefer_raw).rstrip() + "\n"


def build_batch_export_markdown(
    sessions: list[SessionSummary],
    segments_by_session: dict[str, list[SegmentRecord]],
    *,
    prefer_raw: bool = False,
) -> str:
    lines = [
        "---",
        f"exported_at: {_yaml_value(_format_dt(int(time.time() * 1000)))}",
        f"session_count: {len(sessions)}",
        "---",
        "",
    ]
    for session in sorted(sessions, key=lambda s: s.created_at):
        segments = segments_by_session.get(session.session_id, [])
        preview = _one_line(session.preview)
        heading = (
            f"## {_format_dt(session.created_at)} · {preview}"
            if preview
            else f"## {_format_dt(session.created_at)}"
        )
        lines.extend(
            [
                heading,
                "",
                f"- source: {_one_line(session.source)}",
                f"- target_app: {_one_line(session.target_app)}",
                f"- duration_ms: {_session_duration_ms(segments)}",
                f"- model: {_one_line(_session_model(segments))}",
                f"- segment_count: {session.segment_count}",
                "",
                _session_body(segments, prefer_raw=prefer_raw).rstrip(),
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _csv_safe_cell(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return value
    text = str(value)
    if not text:
        return ""
    stripped = text.lstrip(" \n")
    if text.startswith(_CSV_FORMULA_LEADERS) or (
        stripped != text and stripped.startswith(_CSV_FORMULA_LEADERS)
    ):
        return f"'{text}"
    return text


def _normalize_export_inputs(
    sessions_or_session: SessionSummary | Iterable[SessionSummary] | Iterable[SegmentRecord] | None,
    segments_or_map: Iterable[SegmentRecord] | dict[str, list[SegmentRecord]] | None,
) -> tuple[list[SessionSummary], dict[str, list[SegmentRecord]]]:
    if sessions_or_session is None:
        return [], {}
    if isinstance(sessions_or_session, SessionSummary):
        if isinstance(segments_or_map, dict):
            seg_map = {k: list(v) for k, v in segments_or_map.items()}
        else:
            seg_map = {sessions_or_session.session_id: list(segments_or_map or [])}
        return [sessions_or_session], seg_map

    items = list(sessions_or_session)
    if items and isinstance(items[0], SegmentRecord):
        seg_items = [seg for seg in items if isinstance(seg, SegmentRecord)]
        grouped: dict[str, list[SegmentRecord]] = {}
        for seg in seg_items:
            grouped.setdefault(seg.session_id, []).append(seg)
        synthesized = [
            SessionSummary(
                session_id=sid,
                created_at=min(s.created_at for s in segs),
                segment_count=len(segs),
                preview=(segs[0].polished_text or segs[0].raw_text)[:80],
                source=segs[0].source,
                target_app=segs[0].target_app,
            )
            for sid, segs in grouped.items()
        ]
        return synthesized, grouped

    session_list = [s for s in items if isinstance(s, SessionSummary)]
    if isinstance(segments_or_map, dict):
        seg_map = {k: list(v) for k, v in segments_or_map.items()}
    elif segments_or_map is not None and len(session_list) == 1:
        seg_map = {session_list[0].session_id: list(segments_or_map)}
    else:
        seg_map = {}
    return session_list, seg_map


def _build_csv_without_bom(
    sessions: list[SessionSummary],
    segments_by_session: dict[str, list[SegmentRecord]],
    *,
    prefer_raw: bool = False,
) -> str:
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(
        [
            "session_id",
            "created_at",
            "source",
            "target_app",
            "duration_ms",
            "model",
            "segment_count",
            "text",
        ]
    )
    for session in sorted(sessions, key=lambda s: s.created_at):
        segments = segments_by_session.get(session.session_id, [])
        body = _session_body(segments, prefer_raw=prefer_raw).strip()
        if not body and session.preview:
            body = session.preview
        writer.writerow(
            [
                _csv_safe_cell(session.session_id),
                _csv_safe_cell(_format_dt(session.created_at)),
                _csv_safe_cell(session.source),
                _csv_safe_cell(session.target_app),
                _session_duration_ms(segments),
                _csv_safe_cell(_session_model(segments)),
                int(session.segment_count),
                _csv_safe_cell(body),
            ]
        )
    return buf.getvalue()


def build_single_session_csv(
    session: SessionSummary,
    segments: list[SegmentRecord],
    *,
    prefer_raw: bool = False,
) -> str:
    return "\ufeff" + _build_csv_without_bom(
        [session], {session.session_id: segments}, prefer_raw=prefer_raw,
    )


def build_batch_export_csv(
    sessions: list[SessionSummary],
    segments_by_session: dict[str, list[SegmentRecord]],
    *,
    prefer_raw: bool = False,
) -> str:
    return "\ufeff" + _build_csv_without_bom(
        sessions, segments_by_session, prefer_raw=prefer_raw,
    )


def export_csv(
    first: str | Path | SessionSummary | Iterable[SessionSummary] | Iterable[SegmentRecord],
    second: SessionSummary | Iterable[SessionSummary] | Iterable[SegmentRecord] | dict[str, list[SegmentRecord]] | None = None,
    third: Iterable[SegmentRecord] | dict[str, list[SegmentRecord]] | None = None,
    *,
    path: str | Path | None = None,
    prefer_raw: bool = False,
) -> str:
    """Build UTF-8 BOM (``utf-8-sig``) CSV with formula-injection protection.

    Supports both ``export_csv(path, sessions, segments_by_session, prefer_raw=...)``
    and ``export_csv(sessions, segments_by_session, path=..., prefer_raw=...)``
    (as well as single-session ``(session, segments)``).
    """
    if isinstance(first, (str, Path)):
        target_path: Path | None = Path(first)
        sessions, seg_map = _normalize_export_inputs(second, third)  # type: ignore[arg-type]
    else:
        target_path = Path(path) if path is not None else None
        sessions, seg_map = _normalize_export_inputs(first, second)  # type: ignore[arg-type]

    csv_no_bom = _build_csv_without_bom(sessions, seg_map, prefer_raw=prefer_raw)
    if target_path is not None:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(csv_no_bom, encoding="utf-8-sig", newline="")
    return "\ufeff" + csv_no_bom


def _segment_to_export_dict(segment: SegmentRecord, *, prefer_raw: bool = False) -> dict[str, object]:
    return {
        "session_id": segment.session_id,
        "seq": int(segment.seq),
        "created_at": int(segment.created_at),
        "captured_at": int(getattr(segment, "captured_at", 0) or 0),
        "duration_ms": int(segment.duration_ms),
        "raw_text": segment.raw_text,
        "polished_text": segment.polished_text,
        "text": _effective_text(segment, prefer_raw=prefer_raw),
        "source": segment.source,
        "target_app": segment.target_app,
        "model": segment.model,
        "speaker_id": int(segment.speaker_id),
        "speaker_route": segment.speaker_route,
    }


def build_single_session_json(
    session: SessionSummary,
    segments: list[SegmentRecord],
    *,
    prefer_raw: bool = False,
) -> str:
    ordered = _segments_in_order(segments)
    payload = {
        "session_id": session.session_id,
        "created_at": int(session.created_at),
        "created_at_iso": _format_dt(session.created_at),
        "source": session.source,
        "target_app": session.target_app,
        "duration_ms": _session_duration_ms(ordered),
        "model": _session_model(ordered),
        "segment_count": int(session.segment_count),
        "prefer_raw": bool(prefer_raw),
        "text": _session_body(ordered, prefer_raw=prefer_raw),
        "segments": [_segment_to_export_dict(seg, prefer_raw=prefer_raw) for seg in ordered],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def build_batch_export_json(
    sessions: list[SessionSummary],
    segments_by_session: dict[str, list[SegmentRecord]],
    *,
    prefer_raw: bool = False,
) -> str:
    session_items = []
    for session in sorted(sessions, key=lambda s: s.created_at):
        ordered = _segments_in_order(segments_by_session.get(session.session_id, []))
        session_items.append(
            {
                "session_id": session.session_id,
                "created_at": int(session.created_at),
                "created_at_iso": _format_dt(session.created_at),
                "preview": session.preview,
                "source": session.source,
                "target_app": session.target_app,
                "duration_ms": _session_duration_ms(ordered),
                "model": _session_model(ordered),
                "segment_count": int(session.segment_count),
                "text": _session_body(ordered, prefer_raw=prefer_raw),
                "segments": [_segment_to_export_dict(seg, prefer_raw=prefer_raw) for seg in ordered],
            }
        )
    payload = {
        "exported_at": _format_dt(int(time.time() * 1000)),
        "session_count": len(session_items),
        "prefer_raw": bool(prefer_raw),
        "sessions": session_items,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


__all__ = [
    "_SUPPRESSED_POLISHED_MARKER",
    "_csv_safe_cell",
    "_day_label",
    "_effective_text",
    "_filename_dt",
    "_format_dt",
    "_format_duration",
    "_highlight_html",
    "_labeled_text",
    "_route_caption",
    "_safe_dt",
    "_segment_clock_ms",
    "_segments_in_order",
    "_session_body",
    "_session_duration_ms",
    "_session_model",
    "_session_title",
    "_speaker_heading",
    "_yaml_value",
    "build_batch_export_csv",
    "build_batch_export_json",
    "build_batch_export_markdown",
    "build_single_session_csv",
    "build_single_session_json",
    "build_single_session_markdown",
    "build_single_session_plain",
    "export_csv",
    "export_directory",
    "export_save_path",
    "history_shows_speakers",
    "suggest_batch_export_filename",
    "suggest_single_export_filename",
]

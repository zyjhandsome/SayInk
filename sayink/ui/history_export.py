"""Stateless session formatting and Markdown/YAML/plain-text export builders for HistoryWindow."""

from __future__ import annotations

from datetime import datetime
import html
from pathlib import Path
import re
import time

from sayink.history_store import SegmentRecord, SessionSummary

_SUPPRESSED_POLISHED_MARKER = "\u200b"


def _format_dt(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


def _day_label(ms: int) -> str:
    day = datetime.fromtimestamp(ms / 1000).date()
    today = datetime.now().date()
    delta = (today - day).days
    if delta == 0:
        return "今天"
    if delta == 1:
        return "昨天"
    return day.strftime("%Y-%m-%d")


def _filename_dt(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%Y%m%d-%H%M%S")


def _effective_text(segment: SegmentRecord) -> str:
    if segment.polished_text == _SUPPRESSED_POLISHED_MARKER:
        return ""
    return segment.polished_text or segment.raw_text


def _yaml_value(value: object) -> str:
    return str(value).replace("\n", " ").strip()


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


def _session_body(segments: list[SegmentRecord]) -> str:
    ordered = _segments_in_order(segments)
    show_speakers = history_shows_speakers(ordered)
    chunks = [
        _labeled_text(segment, _effective_text(segment), show_speakers=show_speakers)
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
    return f"{_day_label(created_at)} {datetime.fromtimestamp(created_at / 1000).strftime('%H:%M')}"


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
) -> str:
    del session
    return _session_body(segments).rstrip() + "\n"


def build_single_session_markdown(
    session: SessionSummary,
    segments: list[SegmentRecord],
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
    return "\n".join(frontmatter) + _session_body(segments).rstrip() + "\n"


def build_batch_export_markdown(
    sessions: list[SessionSummary],
    segments_by_session: dict[str, list[SegmentRecord]],
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
        lines.extend(
            [
                f"## {_format_dt(session.created_at)} · {session.preview}",
                "",
                f"- source: {session.source}",
                f"- target_app: {session.target_app}",
                f"- duration_ms: {_session_duration_ms(segments)}",
                f"- model: {_session_model(segments)}",
                f"- segment_count: {session.segment_count}",
                "",
                _session_body(segments).rstrip(),
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "_SUPPRESSED_POLISHED_MARKER",
    "_day_label",
    "_effective_text",
    "_filename_dt",
    "_format_dt",
    "_format_duration",
    "_highlight_html",
    "_labeled_text",
    "_route_caption",
    "_segment_clock_ms",
    "_segments_in_order",
    "_session_body",
    "_session_duration_ms",
    "_session_model",
    "_session_title",
    "_speaker_heading",
    "_yaml_value",
    "build_batch_export_markdown",
    "build_single_session_markdown",
    "build_single_session_plain",
    "history_shows_speakers",
    "suggest_batch_export_filename",
    "suggest_single_export_filename",
]

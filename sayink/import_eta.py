"""Estimate how long the current media import still needs.

The clock starts when recognition actually moves forward. Time spent loading a
model or decoding the file does not count as recognition speed. The displayed
value counts down between progress reports and only moves up after a slower
estimate has stayed that way.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math


_READ_BAR = 2
_RECOGNIZE_BAR_END = 95
_FINAL_BAR_END = 99
_WINDOW_S = 45.0
_WARMUP_S = 10.0
_WARMUP_FRACTION = 0.03
_INFLATE_RATIO = 1.25
_INFLATE_HOLD_S = 20.0
_FINALIZE_RATIO = 0.02
_FINALIZE_MIN_S = 2.0
_FINALIZE_MAX_S = 180.0
_RECENT_WEIGHT = 0.7


def _half_up(value: float) -> int:
    return int(math.floor(value + 0.5))


def format_coarse_remaining(seconds: float) -> str:
    """Coarse, stable label for a remaining-seconds estimate."""
    if seconds < 15:
        return "即将完成"
    if seconds < 60:
        return "不到 1 分钟"
    if seconds < 600:
        minutes = max(1, _half_up(seconds / 60.0))
        return f"约 {minutes} 分钟"
    total_minutes = (_half_up(seconds / 300.0) * 300) // 60
    hours, minutes = divmod(int(total_minutes), 60)
    if hours and minutes:
        return f"约 {hours} 小时 {minutes} 分钟"
    if hours:
        return f"约 {hours} 小时"
    return f"约 {int(total_minutes)} 分钟"


def format_audio_position(position_ms: int) -> str:
    total_s = max(0, int(position_ms) // 1000)
    hours, rem = divmod(total_s, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        clock = f"{hours}:{minutes:02d}:{seconds:02d}"
    else:
        clock = f"{minutes}:{seconds:02d}"
    return f"已识别 {clock} 音频"


@dataclass(frozen=True)
class ImportProgressSample:
    """One progress report from the importer. ``wall_s`` is a monotonic clock."""

    phase: str
    wall_s: float
    audio_position_ms: int = 0
    audio_total_ms: int = 0
    duration_known: bool = False
    read_percent: int = 0


@dataclass(frozen=True)
class ImportEtaView:
    phase_text: str
    remain_text: str
    percent: int | None
    position_text: str
    remaining_s: float | None


class ImportEtaEstimator:
    """Turn audio-position samples into a countdown and a matching progress percent."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._origin: tuple[float, int] | None = None
        self._last_wall: float | None = None
        self._last_pos: int | None = None
        self._points: deque[tuple[float, int]] = deque()
        self._anchor_remaining: float | None = None
        self._anchor_wall: float | None = None
        self._inflate_since: float | None = None
        self._percent_floor: int | None = None
        self._finalize_wall: float | None = None
        self._last: ImportProgressSample | None = None

    def observe(self, sample: ImportProgressSample) -> ImportEtaView:
        if sample.phase == "recognizing":
            self._track(sample.wall_s, int(sample.audio_position_ms))
        if sample.phase == "finalizing" and self._finalize_wall is None:
            self._finalize_wall = sample.wall_s
        self._last = sample
        if sample.phase == "recognizing" and self._duration_known(sample):
            estimate = self._recognition_remaining(sample.wall_s, sample)
            if estimate is not None:
                self._consider(estimate, sample.wall_s)
        elif sample.phase == "finalizing" and self._duration_known(sample):
            self._consider(self._finalize_remaining(sample.wall_s, sample), sample.wall_s)
        return self._render(sample, sample.wall_s)

    def tick(self, wall_s: float) -> ImportEtaView:
        if self._last is None:
            return ImportEtaView("正在读取", "", None, "", None)
        return self._render(self._last, wall_s)

    def _track(self, wall: float, position: int) -> None:
        if self._last_pos is None or self._last_wall is None:
            self._last_wall = wall
            self._last_pos = position
            return
        if position <= self._last_pos:
            if self._origin is None:
                self._last_wall = wall
            return
        if self._origin is None:
            self._origin = (self._last_wall, self._last_pos)
        self._points.append((wall, position))
        self._last_wall = wall
        self._last_pos = position

    def _rate_ms_per_s(self) -> float | None:
        if self._origin is None or not self._points:
            return None
        origin_wall, origin_pos = self._origin
        end_wall, end_pos = self._points[-1]
        overall_dt = end_wall - origin_wall
        overall_dp = end_pos - origin_pos
        if overall_dt <= 0 or overall_dp <= 0:
            return None
        overall = overall_dp / overall_dt
        cutoff = end_wall - _WINDOW_S
        left_wall, left_pos = self._origin
        for wall, pos in self._points:
            if wall <= cutoff:
                left_wall, left_pos = wall, pos
            else:
                break
        window_dt = end_wall - left_wall
        window_dp = end_pos - left_pos
        if window_dt >= 5 and window_dp > 0 and window_dt < overall_dt * 0.9:
            recent = window_dp / window_dt
            return _RECENT_WEIGHT * recent + (1.0 - _RECENT_WEIGHT) * overall
        return overall

    def _warm(self, now: float, sample: ImportProgressSample) -> bool:
        if self._origin is None:
            return True
        if now - self._origin[0] < _WARMUP_S:
            return True
        total = sample.audio_total_ms
        if total <= 0:
            return True
        return sample.audio_position_ms / total < _WARMUP_FRACTION

    def _finalize_allowance(self, total_ms: int) -> float:
        raw = _FINALIZE_RATIO * max(0, int(total_ms)) / 1000.0
        return min(_FINALIZE_MAX_S, max(_FINALIZE_MIN_S, raw))

    def _recognition_remaining(self, now: float, sample: ImportProgressSample) -> float | None:
        if self._warm(now, sample):
            return None
        rate = self._rate_ms_per_s()
        if rate is None or rate <= 0:
            return None
        remain_ms = max(0, int(sample.audio_total_ms) - int(sample.audio_position_ms))
        return remain_ms / rate + self._finalize_allowance(sample.audio_total_ms)

    def _finalize_remaining(self, now: float, sample: ImportProgressSample) -> float:
        allowance = self._finalize_allowance(sample.audio_total_ms)
        start = self._finalize_wall if self._finalize_wall is not None else now
        return max(0.0, allowance - max(0.0, now - start))

    def _shown(self, now: float) -> float | None:
        if self._anchor_remaining is None or self._anchor_wall is None:
            return None
        return max(0.0, self._anchor_remaining - (now - self._anchor_wall))

    def _set_anchor(self, remaining: float, now: float) -> None:
        self._anchor_remaining = max(0.0, remaining)
        self._anchor_wall = now
        self._inflate_since = None

    def _consider(self, estimate: float, now: float) -> None:
        shown = self._shown(now)
        if shown is None or estimate <= shown + 0.5:
            self._set_anchor(estimate, now)
            return
        significant = estimate >= 15 if shown <= 0 else estimate > shown * _INFLATE_RATIO
        if not significant:
            self._inflate_since = None
            return
        if self._inflate_since is None:
            self._inflate_since = now
            return
        if now - self._inflate_since >= _INFLATE_HOLD_S:
            self._set_anchor(estimate, now)

    @staticmethod
    def _duration_known(sample: ImportProgressSample) -> bool:
        return bool(sample.duration_known) and int(sample.audio_total_ms) > 0

    def _percent(self, sample: ImportProgressSample, now: float) -> int | None:
        if sample.phase == "finalizing" and self._duration_known(sample):
            allowance = self._finalize_allowance(sample.audio_total_ms)
            start = self._finalize_wall if self._finalize_wall is not None else now
            elapsed = max(0.0, now - start)
            fraction = min(1.0, elapsed / allowance) if allowance > 0 else 1.0
            percent: int | None = _RECOGNIZE_BAR_END + int((_FINAL_BAR_END - _RECOGNIZE_BAR_END) * fraction)
        elif not self._duration_known(sample):
            percent = None
        elif sample.phase == "reading":
            read = max(0, min(100, int(sample.read_percent)))
            percent = int(_READ_BAR * read / 100)
        else:
            ratio = min(1.0, max(0, int(sample.audio_position_ms)) / int(sample.audio_total_ms))
            percent = _READ_BAR + int((_RECOGNIZE_BAR_END - _READ_BAR) * ratio)
        if percent is None:
            return None
        if self._percent_floor is not None:
            percent = max(self._percent_floor, percent)
        self._percent_floor = percent
        return percent

    def _phase_text(self, phase: str) -> str:
        if phase == "recognizing":
            return "正在识别"
        if phase == "finalizing":
            return "正在整理说话人"
        return "正在读取"

    def _render(self, sample: ImportProgressSample, now: float) -> ImportEtaView:
        percent = self._percent(sample, now)
        position = ""
        if not self._duration_known(sample) and sample.phase != "reading":
            position = format_audio_position(sample.audio_position_ms)
        remaining = self._shown(now)
        if not self._duration_known(sample) or sample.phase == "reading":
            remain_text = ""
            remaining = None
        elif remaining is None:
            remain_text = "正在估算剩余时间…"
        else:
            remain_text = format_coarse_remaining(remaining)
        return ImportEtaView(self._phase_text(sample.phase), remain_text, percent, position, remaining)

"""Typed runtime status shared by the main window, engine page, and tray."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RuntimeState(str, Enum):
    READY = "ready"
    LOADING = "loading"
    UNAVAILABLE = "unavailable"


class ModelLoadPhase(str, Enum):
    """What SpeechRecognizer.model_load_progress is reporting."""

    LOADING = "loading"
    READY = "ready"
    FAILED = "failed"


class OutputStage(str, Enum):
    """Who currently owns the sentence being output (App watchdog bookkeeping)."""

    IDLE = "idle"
    POLISH = "polish"
    PASTE = "paste"


@dataclass(frozen=True)
class RuntimeStatus:
    state: RuntimeState
    label: str


# One wording per meeting capture state for the tray tooltip, the sidebar and
# the bar (which only has room for the short form before the clock).
MEETING_STATE_LABELS = {
    "live": "会议记录中",
    "live_short": "会议中",
    "paused": "会议已暂停",
    "draining": "正在整理会议",
    "active": "会议进行中",
}


@dataclass(frozen=True)
class SegmentationStatus:
    """Actual recorder gate, distinct from a preference or a file on disk."""

    requested: str
    effective: str
    model_available: bool = False
    error: str = ""
    meeting: bool = False

    @property
    def label(self) -> str:
        if self.requested != "silero":
            return "能量断句 · 当前生效"
        if not self.model_available:
            return "Silero VAD 未下载 · 当前使用能量断句"
        if not self.effective:
            return "Silero VAD 已下载 · 正在加载…"
        if self.effective == "silero":
            return "Silero VAD 已下载 · 已加载 · 当前生效"
        return "Silero VAD 已下载 · 加载失败 · 已回退能量断句"


def runtime_status_from_flags(*, is_loading: bool, is_ready: bool) -> RuntimeStatus:
    if is_loading:
        return RuntimeStatus(RuntimeState.LOADING, "模型载入中…")
    if is_ready:
        return RuntimeStatus(RuntimeState.READY, "就绪")
    return RuntimeStatus(RuntimeState.UNAVAILABLE, "模型未就绪")


def coerce_runtime_status(
    state_or_label: RuntimeStatus | RuntimeState | str,
    label: str | None = None,
) -> RuntimeStatus:
    """Normalize legacy label-only calls without substring-based state guessing."""
    if isinstance(state_or_label, RuntimeStatus):
        return state_or_label if label is None else RuntimeStatus(state_or_label.state, label)
    if isinstance(state_or_label, RuntimeState):
        state = state_or_label
    else:
        raw = str(state_or_label).strip()
        try:
            state = RuntimeState(raw)
        except ValueError:
            if raw == "就绪":
                state = RuntimeState.READY
            elif raw in {"模型载入中", "模型载入中…", "模型加载中", "模型加载中…", "正在启动…"}:
                state = RuntimeState.LOADING
            else:
                state = RuntimeState.UNAVAILABLE
        if label is None:
            label = raw

    fallback = {
        RuntimeState.READY: "就绪",
        RuntimeState.LOADING: "模型载入中…",
        RuntimeState.UNAVAILABLE: "模型未就绪",
    }[state]
    return RuntimeStatus(state, (label or "").strip() or fallback)

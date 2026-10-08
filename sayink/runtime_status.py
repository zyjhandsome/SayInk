"""Typed runtime status shared by the main window, engine page, and tray."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RuntimeState(str, Enum):
    READY = "ready"
    LOADING = "loading"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class RuntimeStatus:
    state: RuntimeState
    label: str


def runtime_status_from_flags(*, is_loading: bool, is_ready: bool) -> RuntimeStatus:
    if is_loading:
        return RuntimeStatus(RuntimeState.LOADING, "模型载入中…")
    if is_ready:
        return RuntimeStatus(RuntimeState.READY, "就绪")
    return RuntimeStatus(RuntimeState.UNAVAILABLE, "模型未就绪")


def coerce_runtime_status(
    state_or_label: RuntimeState | str,
    label: str | None = None,
) -> RuntimeStatus:
    """Normalize legacy label-only calls without substring-based state guessing."""
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

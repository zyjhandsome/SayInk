"""Typed architectural contracts and immutable snapshots across SayInk layers.

Phase 0 contract foundation: UI panels, orchestrator services, and test doubles
communicate through these explicit Protocols and frozen snapshots instead of
cross-module private ``._*`` attribute probing or duck-typed ``getattr`` shims.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from sayink.runtime_status import OutputStage


@dataclass(frozen=True)
class OutputPipelineSnapshot:
    """Read-only state snapshot of ``OutputPipeline`` for orchestrator & tests."""

    busy: bool
    stage: OutputStage
    stage_text: str
    last_output_text: str
    last_output_at: float
    dropped_slices: int


@dataclass(frozen=True)
class ReloadStateSnapshot:
    """Read-only state snapshot of ``ReloadCoordinator`` for UI & tests."""

    recognition_change_pending: bool
    models_reload_pending: bool
    deferred_audio_settings: bool
    loading_model_id: str
    models_dir_notice: str
    deferred_meeting_audio_settings: bool = False
    pending_apply_notice: str = ""


@runtime_checkable
class ReloadHostProtocol(Protocol):
    """Typed orchestrator boundary consumed by ``ReloadCoordinator`` (replaces 23 loose lambdas)."""

    def is_meeting_active(self) -> bool:
        ...

    def meeting_save_audio_override(self) -> bool | None:
        ...

    def is_reload_pipeline_busy(self) -> bool:
        ...

    def queued_audio_segment_count(self) -> int:
        ...

    def is_recovery_busy(self) -> bool:
        ...

    def is_exit_started(self) -> bool:
        ...

    def is_onboarding_active(self) -> bool:
        ...

    def is_main_window_visible(self) -> bool:
        ...

    def clear_queued_audio(self) -> None:
        ...

    def stop_continuous_listening(self) -> None:
        ...

    def reset_continuous_stop_flags(self) -> None:
        ...

    def rebuild_speaker_embedder(self) -> Any:
        ...

    def sync_hotkey_trigger_mode(self) -> None:
        ...

    def sync_settings_runtime_status(self, status: Any = None) -> None:
        ...

    def sync_recognition_runtime_status(self) -> None:
        ...

    def sync_deferred_settings_notice(self, notice: str = "") -> None:
        ...

    def sync_tray_status_summary(self, status: Any = None) -> None:
        ...


@dataclass(frozen=True)
class AppStyleRule:
    """User-configured explicit per-application LLM style preset binding."""

    process_name: str
    preset_id: str
    custom_instruction: str = ""


@runtime_checkable
class SecretStoreProtocol(Protocol):
    """Credential storage boundary (Windows Credential Manager in prod, in-memory in tests)."""

    def read(self) -> str | None:
        ...

    def write(self, value: str) -> bool:
        ...


@runtime_checkable
class MeetingPanelContract(Protocol):
    """Public lifecycle & query contract exposed by ``MeetingPage`` to ``App`` and tests."""

    @property
    def running_workers(self) -> tuple[Any, ...]:
        ...

    def flush_pending_user_notes(self) -> bool:
        ...

    def flush_all_drafts(self, *, sync: bool = True) -> bool:
        ...

    def cancel_downloads(self) -> None:
        ...

    def cleanup(self) -> None:
        ...

    def show_meeting(self, meeting_id: str) -> None:
        ...

    def refresh(self) -> None:
        ...


@runtime_checkable
class HistoryPanelContract(Protocol):
    """Public lifecycle & query contract exposed by ``HistoryWindow`` to ``App`` and tests."""

    @property
    def running_workers(self) -> tuple[Any, ...]:
        ...

    def flush_pending_delete(self) -> None:
        ...

    def refresh(self) -> None:
        ...


__all__ = [
    "AppStyleRule",
    "HistoryPanelContract",
    "MeetingPanelContract",
    "OutputPipelineSnapshot",
    "ReloadHostProtocol",
    "ReloadStateSnapshot",
    "SecretStoreProtocol",
]

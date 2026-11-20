"""Reload and runtime-configuration coordinator for audio, VAD, speaker, and ASR.

Owns:
- ``RecognitionSettings`` diffing across ``asr_key``, ``segmenter_key``, and ``speaker_key``
- Deferred recognition / audio reload state while capture, meeting, recovery, or output is busy
- Audio device, dictation VAD segmenter profile, and STT configuration (silent vs visible HUD)
- Per-model load-duration ETA persistence and runtime status formatting
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import time
from typing import Any, Callable

from PyQt6.QtWidgets import QSystemTrayIcon

from sayink.audio_devices import INPUT_SOURCES, sanitize_system_device_index
from sayink.contracts import ReloadHostProtocol, ReloadStateSnapshot
from sayink.recognition_settings import RecognitionSettings, aux_model_path
from sayink.runtime_status import RuntimeStatus, SegmentationStatus, runtime_status_from_flags
from sayink.speech_recognizer import (
    AUX_MODEL_SILERO_VAD,
    AUX_MODEL_SPEAKER,
    AUX_MODEL_SPEAKER_ACTIVITY,
    DEFAULT_MODEL_ID,
    get_model_info,
    set_models_dir,
)
from sayink.vad_segmenter import VAD_SILERO

log = logging.getLogger("SayInk")

# Top-level extra key persisted in Config._extra_keys for cross-session model load ETA.
LOAD_SECONDS_KEY = "stt_load_seconds"
DEFERRED_APPLY_NOTICE = "已保存，待本场结束后生效"


def _noop(*_args: Any, **_kwargs: Any) -> None:
    return None


def _false() -> bool:
    return False


def _none() -> None:
    return None


_HOST_CALLBACK_MAP: tuple[tuple[str, str, Callable[..., Any]], ...] = (
    ("meeting_active", "is_meeting_active", _false),
    ("meeting_save_audio", "meeting_save_audio_override", _none),
    ("pipeline_busy", "is_reload_pipeline_busy", _false),
    ("recovery_busy", "is_recovery_busy", _false),
    ("exit_started", "is_exit_started", _false),
    ("onboarding_active", "is_onboarding_active", _false),
    ("main_window_visible", "is_main_window_visible", _false),
    ("clear_queued_audio", "clear_queued_audio", _noop),
    ("stop_continuous_listening", "stop_continuous_listening", _noop),
    ("reset_continuous_stop_flags", "reset_continuous_stop_flags", _noop),
    ("rebuild_speaker_embedder", "rebuild_speaker_embedder", _noop),
    ("sync_hotkey_trigger_mode", "sync_hotkey_trigger_mode", _noop),
    ("sync_settings_runtime_status", "sync_settings_runtime_status", _noop),
    ("sync_recognition_runtime_status", "sync_recognition_runtime_status", _noop),
    ("sync_deferred_settings_notice", "sync_deferred_settings_notice", _noop),
    ("sync_tray_status_summary", "sync_tray_status_summary", _noop),
)


class ReloadCoordinator:
    """Coordinates runtime diffing and safe deferred reloads for audio, VAD, speaker, and ASR."""

    def __init__(
        self,
        *,
        host: ReloadHostProtocol | None = None,
        config: Any = None,
        recorder: Any = None,
        recognizer: Any = None,
        floating: Any = None,
        tray: Any = None,
        sound: Any = None,
        paster: Any = None,
        aux_model_path_fn: Callable[[str], Any] | None = None,
        set_models_dir_fn: Callable[[str | None], bool] | None = None,
        is_model_downloaded_fn: Callable[[str], bool] | None = None,
        resolve_startup_model_id_fn: Callable[[str], str] | None = None,
        model_registry_fn: Callable[[], list[dict]] | None = None,
        now_monotonic: Callable[[], float] | None = None,
        **callbacks: Callable[..., Any] | None,
    ) -> None:
        self.host = host
        self.config = config
        self.recorder = recorder
        self.recognizer = recognizer
        self.floating = floating
        self.tray = tray
        self.sound = sound
        self.paster = paster

        self.aux_model_path_fn = aux_model_path_fn or aux_model_path
        self.set_models_dir_fn = set_models_dir_fn or set_models_dir
        self.is_model_downloaded_fn = is_model_downloaded_fn
        self.resolve_startup_model_id_fn = resolve_startup_model_id_fn
        self.model_registry_fn = model_registry_fn
        self.now_monotonic = now_monotonic or time.monotonic
        self._bind_host_callbacks(host, callbacks)

        self.recognition_change_pending: bool = False
        self.models_reload_pending: bool = False
        self.deferred_audio_settings: bool = False
        self.deferred_meeting_audio_settings: bool = False
        self.settings_after_recovery: bool = False
        self.cold_boot_load_quiet: bool = False
        self.user_awaited_model_ready: bool = False
        self.applied_audio_key: tuple[str, int, int] | None = None
        self.applied_recognition_key: tuple | None = None
        self.applied_speaker_aux: tuple = ()
        self.applied_vad_aux: Any = None
        self.load_started_at: float | None = None
        self.loading_model_id: str = ""
        self.models_dir_notice: str = ""

        if config is not None and callable(getattr(config, "get", None)):
            self._applied_recognition_settings = RecognitionSettings.from_config(config)
            self._initialized_from_config = True
        else:
            self._applied_recognition_settings = RecognitionSettings(model_id=DEFAULT_MODEL_ID)
            self._initialized_from_config = False

    def _bind_host_callbacks(
        self,
        host: ReloadHostProtocol | None,
        callbacks: dict[str, Callable[..., Any] | None],
    ) -> None:
        for attr_name, host_method, fallback in _HOST_CALLBACK_MAP:
            override = callbacks.pop(attr_name, None)
            if override is not None:
                setattr(self, attr_name, override)
            elif host is not None and hasattr(host, host_method):
                setattr(self, attr_name, getattr(host, host_method))
            else:
                setattr(self, attr_name, fallback)

        has_queued = callbacks.pop("has_queued_segments", None)
        queued_count = callbacks.pop("queued_segment_count", None)
        if queued_count is None and host is not None and hasattr(host, "queued_audio_segment_count"):
            queued_count = host.queued_audio_segment_count
        if has_queued is None:
            has_queued = (lambda: bool(queued_count())) if queued_count is not None else _false
        if queued_count is None:
            queued_count = lambda: 1 if has_queued() else 0
        self.has_queued_segments = has_queued
        self.queued_segment_count = queued_count
        if callbacks:
            unknown = ", ".join(sorted(callbacks))
            raise TypeError(f"Unexpected ReloadCoordinator callback argument(s): {unknown}")

    @property
    def applied_recognition_settings(self) -> RecognitionSettings:
        return self._applied_recognition_settings

    @applied_recognition_settings.setter
    def applied_recognition_settings(self, value: RecognitionSettings) -> None:
        self._applied_recognition_settings = value
        self._initialized_from_config = True

    def snapshot(self) -> ReloadStateSnapshot:
        return ReloadStateSnapshot(
            recognition_change_pending=bool(self.recognition_change_pending),
            models_reload_pending=bool(self.models_reload_pending),
            deferred_audio_settings=bool(self.deferred_audio_settings),
            loading_model_id=str(self.loading_model_id or ""),
            models_dir_notice=str(self.models_dir_notice or ""),
            deferred_meeting_audio_settings=bool(self.deferred_meeting_audio_settings),
            pending_apply_notice=self.pending_apply_notice,
        )

    def sync_config_if_needed(self, config: Any) -> None:
        """Populate initial recognition snapshot if ``config`` was injected lazily."""
        if self.config is not config:
            self.config = config
        if (
            not self._initialized_from_config
            and config is not None
            and callable(getattr(config, "get", None))
        ):
            self._applied_recognition_settings = RecognitionSettings.from_config(config)
            self._initialized_from_config = True

    def _is_model_downloaded(self, model_id: str) -> bool:
        from sayink.speech_recognizer import is_asr_model_downloaded, is_aux_model

        if is_aux_model(model_id):
            return False
        if self.is_model_downloaded_fn is not None:
            return bool(self.is_model_downloaded_fn(model_id))
        return bool(is_asr_model_downloaded(model_id))

    def _resolve_startup_model_id(self, configured: str) -> str:
        if self.resolve_startup_model_id_fn is not None:
            return self.resolve_startup_model_id_fn(configured)
        from sayink.speech_recognizer import resolve_startup_model_id

        return resolve_startup_model_id(configured)

    def _model_registry(self) -> list[dict]:
        if self.model_registry_fn is not None:
            return self.model_registry_fn()
        from sayink.speech_recognizer import MODEL_REGISTRY

        return MODEL_REGISTRY

    def sync_models_dir(self) -> bool:
        """Apply only a folder the user actually chose.

        The default location still has to see the model shipped beside the
        program. Passing that default path in as a custom directory would
        hide the installed copy and tell the user to download it again.
        """
        chosen = str(self.config.get("stt.models_dir", "") or "").strip()
        return self.set_models_dir_fn(chosen or None)

    def init_models_dir_notice(self) -> str:
        """Sync configured models directory on startup and record fallback notice if unusable."""
        self.models_dir_notice = ""
        if not self.sync_models_dir():
            self.models_dir_notice = (
                f"模型目录不可用，已改用默认目录：{self.config.get('stt.models_dir', '')}"
            )
        return self.models_dir_notice

    def audio_runtime_key(self) -> tuple[str, int, int]:
        """Device choice only. Trigger mode must not look like an audio change."""
        return (
            str(self.config.get("audio.input_source", "microphone")),
            int(self.config.get("audio.mic_device_index", -1)),
            int(self.config.get("audio.system_device_index", -1)),
        )

    def recognition_runtime_key(self, settings: RecognitionSettings | None = None) -> tuple:
        shared = settings or RecognitionSettings.from_config(self.config)
        return (
            *shared.asr_key(int(self.config.get("stt.num_threads", 4) or 4)),
            str(self.config.get("stt.models_dir", "") or "").strip(),
        )

    def speaker_aux_paths(self, settings: RecognitionSettings) -> tuple:
        return (
            self.aux_model_path_fn(AUX_MODEL_SPEAKER) if settings.speaker_model_enabled else None,
            self.aux_model_path_fn(AUX_MODEL_SPEAKER_ACTIVITY) if settings.speaker_activity_enabled else None,
        )

    def vad_aux_path(self, vad: str) -> Any:
        return self.aux_model_path_fn(AUX_MODEL_SILERO_VAD) if vad == VAD_SILERO else None

    def sync_recognizer_quiet_speech(self, shared: RecognitionSettings | None = None) -> None:
        recognizer = self.recognizer
        if recognizer is None:
            return
        settings = shared or self.applied_recognition_settings
        seg_status = getattr(self.recorder, "segmentation_status", None)
        if getattr(seg_status, "effective", "") != VAD_SILERO:
            recognizer.quiet_speech = False
        else:
            recognizer.quiet_speech = settings.quiet_speech_mode

    def apply_audio_config(self) -> None:
        source = self.config.get("audio.input_source", "microphone")
        if source not in INPUT_SOURCES:
            source = "microphone"
            self.config.set("audio.input_source", source)
        sys_idx = sanitize_system_device_index(
            int(self.config.get("audio.system_device_index", -1))
        )
        if sys_idx != int(self.config.get("audio.system_device_index", -1)):
            self.config.set("audio.system_device_index", sys_idx)
        self.recorder.configure(
            input_source=source,
            mic_device_index=int(self.config.get("audio.mic_device_index", -1)),
            system_device_index=sys_idx,
        )
        self.apply_dictation_segmenter_profile()
        floating = self.floating
        if floating is not None:
            setter = getattr(floating, "set_input_source", None)
            if callable(setter):
                setter(source)
        # The device is opened on the next recording, not here. Enumerating
        # PortAudio on this click froze the window for the whole query.
        self.applied_audio_key = self.audio_runtime_key()

    def apply_audio_options(self) -> None:
        # Dictation never keeps audio. A meeting writes a local journal only
        # when 设置 → 会议默认设置 → 保存会议录音 is on for that meeting.
        meeting_on = bool(self.meeting_active())
        per_meeting = self.meeting_save_audio() if meeting_on else None
        meeting_wants_audio = (
            bool(self.config.get("meeting.save_audio", True))
            if per_meeting is None
            else bool(per_meeting)
        )
        save_audio = bool(meeting_on and meeting_wants_audio)
        cfg_dir = getattr(self.config, "config_dir", None)
        root = (
            Path(cfg_dir) / "audio-recovery"
            if save_audio and isinstance(cfg_dir, (str, os.PathLike))
            else None
        )
        set_keep_fn = getattr(self.recorder, "set_recovery_keep_completed", None)
        if callable(set_keep_fn):
            set_keep_fn(save_audio)
        self.recorder.set_recovery_directory(root)
        self.recorder.set_separate_lanes(bool(self.config.get("audio.separate_lanes", False)))

    def apply_dictation_segmenter_profile(self) -> None:
        """Dictation keeps its short pauses but uses the same VAD choice as
        meetings (Silero when downloaded, else energy)."""
        self.applied_recognition_settings = RecognitionSettings.from_config(self.config)
        vad = self.applied_recognition_settings.vad
        self.recorder.set_quiet_speech(self.applied_recognition_settings.quiet_speech_mode)
        self.set_boundary_context_for_model(self.applied_recognition_settings.model_id)
        model_path = self.vad_aux_path(vad)
        self.applied_vad_aux = model_path
        self.recorder.set_segmenter_profile(vad=vad, vad_model_path=model_path, meeting=False)
        self.sync_recognizer_quiet_speech(self.applied_recognition_settings)

    def set_boundary_context_for_model(self, model_id: str) -> None:
        loader = (get_model_info(model_id) or {}).get("loader", "")
        self.recorder.set_boundary_context(
            self.applied_recognition_settings.boundary_context_enabled
            and loader in {"fire_red_asr_ctc", "zipformer_ctc"}
        )

    def _configure_downloaded_stt(
        self,
        model_id: str,
        num_threads: int,
        *,
        hotwords: str,
        language: str,
        boot: bool,
    ) -> None:
        info = get_model_info(model_id)
        name = info["name"] if info else model_id
        needs_load = not (
            self.recognizer.current_model_id == model_id and self.recognizer.is_ready
        )
        if needs_load and not self.recognizer.is_loading:
            quiet_surface = bool(boot or self.onboarding_active() or self.main_window_visible())
            self.cold_boot_load_quiet = quiet_surface
            if not quiet_surface:
                self.user_awaited_model_ready = True
                self.floating.show_model_loading(
                    f"正在将 {name} 载入内存，请稍候（{self.load_eta_text(model_id)}）…"
                )
            else:
                self.user_awaited_model_ready = False
            self.tray.set_activity_tooltip("loading")
            self.sync_settings_runtime_status()
        self.recognizer.configure(model_id, num_threads, hotwords=hotwords, language=language)

    def _notify_missing_stt_model(self, model_id: str, *, boot: bool) -> None:
        self.cold_boot_load_quiet = False
        self.user_awaited_model_ready = False
        info = get_model_info(model_id) if model_id else None
        name = info["name"] if info else (model_id or DEFAULT_MODEL_ID)
        log.warning("语音模型 %s 未下载，请在设置中下载模型", name)
        hint = (
            f"请下载语音模型「{name}」。"
            "Windows 可点击托盘图标打开主窗口，再到设置 → 引擎；或右键托盘 → 打开 SayInk。"
        )
        first_run_setup = boot and (
            not bool(self.config.get("first_run_welcome_seen", True)) or self.onboarding_active()
        )
        if first_run_setup:
            return
        if not boot and not self.main_window_visible():
            self.floating.show_error(hint)
        self.tray.showMessage(
            "SayInk",
            hint,
            QSystemTrayIcon.MessageIcon.Warning,
            6000,
        )

    def configure_stt(self, *, boot: bool = False) -> None:
        configured = self.config.get("stt.model_id", DEFAULT_MODEL_ID)
        model_id = self._resolve_startup_model_id(configured)
        if model_id and model_id != configured and self._is_model_downloaded(model_id):
            log.info("语音模型配置已改为实际使用的 %s（原 %s 未下载）", model_id, configured)
            self.config.set("stt.model_id", model_id)
            if not self.recognition_change_pending:
                self.applied_recognition_settings = RecognitionSettings.from_config(self.config)
        num_threads = self.config.get("stt.num_threads", 4)
        shared = RecognitionSettings.from_config(self.config)
        self.sync_recognizer_quiet_speech(shared)

        if model_id and self._is_model_downloaded(model_id):
            self._configure_downloaded_stt(
                model_id,
                num_threads,
                hotwords=shared.hotwords,
                language=shared.effective_language,
                boot=boot,
            )
        else:
            self._notify_missing_stt_model(model_id, boot=boot)
        self.applied_recognition_key = self.recognition_runtime_key()

    def load_eta_text(self, model_id: str) -> str:
        seconds = (self.config.get(LOAD_SECONDS_KEY, {}) or {}).get(model_id)
        if isinstance(seconds, (int, float)) and seconds > 0:
            return f"上次用时约 {max(1, round(seconds))} 秒"
        return "约 10–40 秒"

    def begin_tracked_model_load(self, msg: str) -> str:
        """Record load start time for ``current_model_id`` and append ETA suffix."""
        model_id = getattr(self.recognizer, "current_model_id", "")
        if isinstance(model_id, str) and model_id:
            self.load_started_at = self.now_monotonic()
            self.loading_model_id = model_id
            return f"{msg}（{self.load_eta_text(model_id)}）"
        return msg

    def remember_load_duration(self) -> None:
        started = self.load_started_at
        model_id = self.loading_model_id
        self.load_started_at = None
        if started is None or not model_id:
            return
        durations = dict(self.config.get(LOAD_SECONDS_KEY, {}) or {})
        durations[model_id] = round(self.now_monotonic() - started, 1)
        self.config.set(LOAD_SECONDS_KEY, durations)

    def runtime_status(self) -> RuntimeStatus:
        return runtime_status_from_flags(
            is_loading=bool(self.recognizer.is_loading),
            is_ready=bool(self.recognizer.is_ready),
        )

    def dictation_model_id(self) -> str:
        current = self.recognizer.current_model_id
        if isinstance(current, str) and current:
            return current
        if self.recognition_change_pending:
            return self.applied_recognition_settings.model_id
        return str(self.config.get("stt.model_id", DEFAULT_MODEL_ID) or DEFAULT_MODEL_ID)

    def _loaded_campplus_status_text(self, embedder: Any, *, activity_requested: bool) -> str:
        speaker = "CAM++ 已下载 · 已加载；用于持续转写与会议"
        if getattr(embedder, "activity_guard", None) is not None:
            return speaker + "；多人发言检测（按需护档）已加载"
        if getattr(embedder, "activity_guard_error", ""):
            return speaker + "；多人发言检测加载失败，本场未启用"
        if activity_requested:
            return speaker + "；多人发言检测未加载，请在引擎页检查检测模型"
        return speaker

    def _disabled_campplus_status_text(self) -> str:
        if self.config.get("stt.debug_force_speaker_model", None) is False:
            return "CAM++ 增强已关闭 · 使用频谱粗略区分"
        if self.aux_model_path_fn(AUX_MODEL_SPEAKER) is not None:
            return "CAM++ 已下载 · 按需加载（会议及使用电脑声音的持续转写中自动启用）"
        return "CAM++ 未下载 · 会议或含电脑声音时使用频谱粗略区分；可在引擎页下载"

    def speaker_runtime_status_text(
        self,
        *,
        active_meeting: Any = None,
        dictation_embedder: Any = None,
    ) -> str:
        active = active_meeting is not None and bool(getattr(active_meeting, "is_active", False))
        caps = getattr(active_meeting, "capabilities", None) if active else None
        embedder = getattr(active_meeting, "embedder", None) if active else dictation_embedder
        applied = self.applied_recognition_settings
        enabled = caps.speaker_enabled if caps is not None else applied.speaker_model_enabled
        activity_req = (
            caps.speaker_activity_enabled if caps is not None else applied.speaker_activity_enabled
        )
        if embedder is not None and getattr(embedder, "name", "") == "campplus":
            return self._loaded_campplus_status_text(embedder, activity_requested=activity_req)
        if not enabled:
            return self._disabled_campplus_status_text()
        if embedder is not None and getattr(embedder, "model_available", False):
            return "CAM++ 已下载 · 加载失败 · 已回退频谱粗略区分"
        if self.aux_model_path_fn(AUX_MODEL_SPEAKER) is not None:
            return "CAM++ 已下载 · 待加载 · 本场使用频谱粗略区分"
        return "CAM++ 未下载 · 使用频谱粗略区分；可在引擎页下载"

    def recognition_diff(
        self,
        old: RecognitionSettings,
        new: RecognitionSettings,
        *,
        models_changed: bool = False,
    ) -> tuple[bool, bool, bool]:
        speaker_changed = new.speaker_key != old.speaker_key or (
            models_changed
            and self.speaker_aux_paths(new) != self.applied_speaker_aux
        )
        segmenter_changed = new.segmenter_key != old.segmenter_key
        if models_changed:
            vad_path = self.vad_aux_path(new.vad)
            status = getattr(self.recorder, "segmentation_status", None)
            # Repair can replace the file in place, leaving the path unchanged.
            retry_failed_vad = isinstance(status, SegmentationStatus) and bool(status.error and vad_path)
            segmenter_changed = segmenter_changed or vad_path != self.applied_vad_aux or retry_failed_vad
        new_asr_key = self.recognition_runtime_key(new)
        old_asr_key = self.applied_recognition_key
        if old_asr_key is None:
            old_asr_key = (
                *old.asr_key(int(self.config.get("stt.num_threads", 4) or 4)),
                "",
            )
        asr_changed = new_asr_key != old_asr_key or models_changed
        return speaker_changed, segmenter_changed, asr_changed

    @property
    def has_deferred_settings(self) -> bool:
        return bool(
            self.recognition_change_pending
            or self.models_reload_pending
            or self.deferred_audio_settings
            or self.deferred_meeting_audio_settings
            or self.settings_after_recovery
        )

    @property
    def pending_apply_notice(self) -> str:
        return DEFERRED_APPLY_NOTICE if self.has_deferred_settings else ""

    def _publish_deferred_notice(self) -> None:
        self.sync_deferred_settings_notice(self.pending_apply_notice)

    def on_models_changed(self) -> None:
        """Keep current speech intact; apply the latest settings when idle."""
        if self.exit_started():
            return
        self.models_reload_pending = True
        self.recognition_change_pending = True
        self.apply_pending_recognition_settings()
        self.sync_recognition_runtime_status()
        self._publish_deferred_notice()
        self.update_tray_models()

    def _is_recognition_apply_blocked(self) -> bool:
        queue_blocks = bool(self.has_queued_segments()) and bool(
            self.recognizer.is_ready or self.recognizer.is_loading
        )
        return bool(
            self.meeting_active()
            or getattr(self.recorder, "is_recording", False)
            or self.pipeline_busy()
            or queue_blocks
        )

    def _apply_changed_subsystems(
        self,
        new: RecognitionSettings,
        *,
        models_changed: bool,
        speaker_changed: bool,
        segmenter_changed: bool,
        asr_changed: bool,
    ) -> None:
        self.recognition_change_pending = False
        self.models_reload_pending = False
        if models_changed or asr_changed:
            self.sync_models_dir()
        self.applied_recognition_settings = new
        if segmenter_changed:
            self.apply_dictation_segmenter_profile()
        if speaker_changed:
            self.rebuild_speaker_embedder()
        if asr_changed:
            self.configure_stt()
        self.sync_recognition_runtime_status()

    def apply_pending_recognition_settings(self) -> None:
        if self.exit_started():
            return
        was_deferred = self.has_deferred_settings
        if self.recognition_change_pending:
            old = self.applied_recognition_settings
            new = RecognitionSettings.from_config(self.config)
            models_changed = bool(self.models_reload_pending)
            speaker_changed, segmenter_changed, asr_changed = self.recognition_diff(
                old,
                new,
                models_changed=models_changed,
            )
            if not (speaker_changed or segmenter_changed or asr_changed) and not self.meeting_active():
                self.recognition_change_pending = False
                self.models_reload_pending = False
                self.applied_recognition_settings = new
                self.sync_recognition_runtime_status()
            elif not self._is_recognition_apply_blocked():
                self._apply_changed_subsystems(
                    new,
                    models_changed=models_changed,
                    speaker_changed=speaker_changed,
                    segmenter_changed=segmenter_changed,
                    asr_changed=asr_changed,
                )
        self.apply_deferred_audio_settings()
        if was_deferred and not self.has_deferred_settings:
            self._publish_deferred_notice()

    def apply_deferred_audio_settings(self) -> None:
        """Finish a settings save that arrived while hold-to-talk was open.

        The utterance already being recorded keeps the segmenter it started
        with. The new device / model applies once that utterance has left
        the queue, and it is not discarded.
        """
        if not self.deferred_audio_settings:
            return
        if (
            self.meeting_active()
            or bool(getattr(self.recorder, "is_recording", False))
            or self.pipeline_busy()
            or self.has_queued_segments()
        ):
            return
        self.deferred_audio_settings = False
        self.commit_audio_settings(clear_queue=False)

    def on_recognition_settings_changed(self) -> None:
        """Glossary / language / VAD / speaker settings changed: apply only the
        affected subsystem so glossary edits do not rebuild VAD or CAM++."""
        if self.exit_started():
            return
        old_model_id = self.applied_recognition_settings.model_id
        self.recognition_change_pending = True
        self.apply_pending_recognition_settings()
        self.sync_recognition_runtime_status()
        self._publish_deferred_notice()
        if str(self.config.get("stt.model_id", "") or "") != old_model_id:
            self.update_tray_models()

    def apply_immediate_preferences(self) -> None:
        self.sound.enabled = self.config.get("sound_enabled", True)
        self.tray.set_auto_start(self.config.get("auto_start", False))
        self.paster.restore_clipboard = self.config.get("output.restore_clipboard", True)

    def on_settings_changed(self) -> None:
        if self.exit_started():
            return
        if self.recovery_busy():
            self.settings_after_recovery = True
            self.sync_recognition_runtime_status()
            self._publish_deferred_notice()
            return
        # Drop only what the confirmation counted; the sentence cut off by
        # stopping below is still the user's speech and gets transcribed.
        if self.meeting_active():
            # The meeting keeps its device, model and queue; audio / engine
            # changes apply when it ends (_on_meeting_ended reconfigures).
            log.info("会议进行中，音频与引擎设置将在会议结束后生效")
            self.deferred_meeting_audio_settings = True
            self.apply_immediate_preferences()
            self.sync_models_dir()
            self.update_tray_models()
            self.sync_recognition_runtime_status()
            self._publish_deferred_notice()
            return
        if getattr(self.recorder, "is_recording", False) and not getattr(
            self.recorder, "is_continuous", False
        ):
            # Rebuilding the segmenter under an open single-shot hold resets the
            # audio anchor mid-sentence. Keep this utterance, apply once it is done.
            log.info("单次录音进行中，音频与引擎设置将在本次录音结束后生效")
            self._defer_audio_settings()
            return
        self.commit_audio_settings(clear_queue=True)

    def _defer_audio_settings(self) -> None:
        self.deferred_audio_settings = True
        self.apply_immediate_preferences()
        self.sync_recognition_runtime_status()
        self._publish_deferred_notice()

    def commit_audio_settings(self, *, clear_queue: bool) -> None:
        if clear_queue:
            queued = self.queued_segment_count()
            if queued:
                log.warning("设置已保存，丢弃 %d 段待识别语音", queued)
            self.clear_queued_audio()
            if getattr(self.recorder, "is_continuous", False):
                self.stop_continuous_listening()
            if not self.pipeline_busy():
                self.reset_continuous_stop_flags()
        self.apply_immediate_preferences()
        # A trigger-mode click used to re-open the recognizer, rescan every
        # audio device and restyle the window. Only the pieces that changed
        # are applied; theme has its own signal.
        old_settings = self.applied_recognition_settings
        new_settings = RecognitionSettings.from_config(self.config)
        models_changed = bool(self.models_reload_pending)
        speaker_changed, segmenter_changed, asr_changed = self.recognition_diff(
            old_settings,
            new_settings,
            models_changed=models_changed,
        )
        if models_changed or asr_changed:
            self.sync_models_dir()
        if self.audio_runtime_key() != self.applied_audio_key:
            self.apply_audio_config()
        elif segmenter_changed:
            self.apply_dictation_segmenter_profile()
        else:
            self.applied_recognition_settings = new_settings
            self.sync_recognizer_quiet_speech(new_settings)
        if speaker_changed:
            self.rebuild_speaker_embedder()
        if asr_changed:
            self.configure_stt()
            self.update_tray_models()
        self.recognition_change_pending = False
        self.models_reload_pending = False
        self.deferred_audio_settings = False
        self.deferred_meeting_audio_settings = False
        self.sync_recognition_runtime_status()
        self._publish_deferred_notice()
        self.sync_hotkey_trigger_mode()

    def on_tray_model_switch(self, model_id: str) -> None:
        current = self.config.get("stt.model_id", "")
        if model_id == current:
            return
        self.config.set("stt.model_id", model_id)
        self.on_models_changed()

    def update_tray_models(self) -> None:
        downloaded = [m for m in self._model_registry() if self._is_model_downloaded(m["id"])]
        active = self.config.get("stt.model_id", "")
        self.tray.update_models(downloaded, active)
        self.sync_tray_status_summary()

    def resume_after_recovery(self) -> None:
        if self.settings_after_recovery:
            self.settings_after_recovery = False
            self.on_settings_changed()
        self.apply_pending_recognition_settings()
        if not self.has_deferred_settings:
            self._publish_deferred_notice()

    def reset_for_meeting_end(self) -> None:
        """Hand the pipeline back to dictation once a meeting finishes."""
        self.recognition_change_pending = False
        self.models_reload_pending = False
        self.deferred_audio_settings = False
        self.deferred_meeting_audio_settings = False
        self.apply_audio_options()
        self.apply_audio_config()
        self.configure_stt()
        self._publish_deferred_notice()

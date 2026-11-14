"""Standalone unit and constructor-DI tests for ReloadCoordinator."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

from sayink.app import App
from sayink.config import DEFAULT_CONFIG, Config
from sayink.reload_coordinator import LOAD_SECONDS_KEY, ReloadCoordinator
from sayink.runtime_status import RuntimeState, SegmentationStatus
from sayink.speech_recognizer import (
    AUX_MODEL_SILERO_VAD,
    AUX_MODEL_SPEAKER,
    AUX_MODEL_SPEAKER_ACTIVITY,
)
from sayink.vad_segmenter import VAD_ENERGY, VAD_SILERO


def _dot_get(store: dict, key: str, default=None):
    node = store
    for part in key.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return default
    return node


def _dot_set(store: dict, key: str, value) -> None:
    node = store
    parts = key.split(".")
    for part in parts[:-1]:
        if not isinstance(node.get(part), dict):
            node[part] = {}
        node = node[part]
    node[parts[-1]] = value


class _FakeConfig:
    def __init__(self, overrides: dict | None = None, *, config_dir: Path | None = None) -> None:
        self.store = deepcopy(DEFAULT_CONFIG)
        self.config_dir = config_dir or Path(".")
        if overrides:
            for key, value in overrides.items():
                self.set(key, value)

    def get(self, key: str, default=None):
        return _dot_get(self.store, key, default)

    def set(self, key: str, value) -> None:
        _dot_set(self.store, key, value)


@dataclass
class _FakeRecorder:
    is_recording: bool = False
    is_continuous: bool = False
    segmentation_status: SegmentationStatus = field(
        default_factory=lambda: SegmentationStatus(VAD_SILERO, VAD_SILERO)
    )
    configured: list[dict] = field(default_factory=list)
    segmenter_profiles: list[dict] = field(default_factory=list)
    quiet_speech_calls: list[bool | None] = field(default_factory=list)
    boundary_calls: list[bool] = field(default_factory=list)
    recovery_dirs: list[Path | None] = field(default_factory=list)
    separate_lanes_calls: list[bool] = field(default_factory=list)

    def configure(self, **kwargs) -> None:
        self.configured.append(kwargs)

    def set_segmenter_profile(self, **kwargs) -> None:
        self.segmenter_profiles.append(kwargs)

    def set_quiet_speech(self, enabled: bool | None) -> None:
        self.quiet_speech_calls.append(enabled)

    def set_boundary_context(self, enabled: bool) -> None:
        self.boundary_calls.append(enabled)

    def set_recovery_directory(self, path: Path | None) -> None:
        self.recovery_dirs.append(path)

    def set_separate_lanes(self, enabled: bool) -> None:
        self.separate_lanes_calls.append(enabled)


@dataclass
class _FakeRecognizer:
    is_ready: bool = True
    is_loading: bool = False
    current_model_id: str = "funasr-nano"
    quiet_speech: bool | None = False
    configure_calls: list[dict] = field(default_factory=list)

    def configure(
        self,
        model_id: str,
        num_threads: int = 4,
        *,
        hotwords: str = "",
        language: str = "auto",
    ) -> None:
        self.configure_calls.append(
            {
                "model_id": model_id,
                "num_threads": num_threads,
                "hotwords": hotwords,
                "language": language,
            }
        )


@dataclass
class _FakeFloating:
    loading_messages: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    input_sources: list[str] = field(default_factory=list)

    def show_model_loading(self, msg: str) -> None:
        self.loading_messages.append(msg)

    def show_error(self, msg: str) -> None:
        self.errors.append(msg)

    def set_input_source(self, source: str) -> None:
        self.input_sources.append(source)


@dataclass
class _FakeTray:
    tooltips: list[str | None] = field(default_factory=list)
    messages: list[tuple] = field(default_factory=list)
    auto_starts: list[bool] = field(default_factory=list)
    updated_models: list[tuple[list[dict], str]] = field(default_factory=list)

    def set_activity_tooltip(self, value: str | None) -> None:
        self.tooltips.append(value)

    def showMessage(self, title: str, message: str, icon=None, msecs: int = 0) -> None:
        self.messages.append((title, message, icon, msecs))

    def set_auto_start(self, enabled: bool) -> None:
        self.auto_starts.append(enabled)

    def update_models(self, downloaded: list[dict], active: str) -> None:
        self.updated_models.append((downloaded, active))


@dataclass
class _FakeSound:
    enabled: bool = True


@dataclass
class _FakePaster:
    restore_clipboard: bool = False


def _build_coordinator(
    overrides: dict | None = None,
    *,
    downloaded: set[str] | None = None,
    aux_paths: dict[str, str | None] | None = None,
    **kwargs,
) -> tuple[ReloadCoordinator, dict]:
    cfg = _FakeConfig(overrides)
    recorder = _FakeRecorder()
    recognizer = _FakeRecognizer(
        current_model_id=str(cfg.get("stt.model_id", "funasr-nano") or "funasr-nano")
    )
    floating = _FakeFloating()
    tray = _FakeTray()
    sound = _FakeSound()
    paster = _FakePaster()
    downloaded_models = downloaded if downloaded is not None else {"funasr-nano", "paraformer-zh", "sensevoice"}
    aux_map = aux_paths if aux_paths is not None else {
        AUX_MODEL_SILERO_VAD: "/models/silero_vad.onnx",
        AUX_MODEL_SPEAKER: "/models/campplus.onnx",
        AUX_MODEL_SPEAKER_ACTIVITY: None,
    }
    clock = [500.0]
    speaker_rebuilds = [0]
    cleared_queues = [0]
    stopped_continuous = [0]
    reset_flags = [0]
    synced_hotkeys = [0]

    coord = ReloadCoordinator(
        config=cfg,
        recorder=recorder,
        recognizer=recognizer,
        floating=floating,
        tray=tray,
        sound=sound,
        paster=paster,
        aux_model_path_fn=lambda mid: aux_map.get(mid),
        set_models_dir_fn=lambda _p: True,
        is_model_downloaded_fn=lambda mid: mid in downloaded_models,
        resolve_startup_model_id_fn=lambda mid: mid,
        now_monotonic=lambda: clock[0],
        clear_queued_audio=lambda: cleared_queues.__setitem__(0, cleared_queues[0] + 1),
        stop_continuous_listening=lambda: stopped_continuous.__setitem__(0, stopped_continuous[0] + 1),
        reset_continuous_stop_flags=lambda: reset_flags.__setitem__(0, reset_flags[0] + 1),
        rebuild_speaker_embedder=lambda: speaker_rebuilds.__setitem__(0, speaker_rebuilds[0] + 1),
        sync_hotkey_trigger_mode=lambda: synced_hotkeys.__setitem__(0, synced_hotkeys[0] + 1),
        **kwargs,
    )
    ctx = {
        "config": cfg,
        "recorder": recorder,
        "recognizer": recognizer,
        "floating": floating,
        "tray": tray,
        "sound": sound,
        "paster": paster,
        "downloaded": downloaded_models,
        "aux_paths": aux_map,
        "clock": clock,
        "speaker_rebuilds": speaker_rebuilds,
        "cleared_queues": cleared_queues,
        "stopped_continuous": stopped_continuous,
        "reset_flags": reset_flags,
        "synced_hotkeys": synced_hotkeys,
    }
    return coord, ctx


class TestReloadCoordinatorDiffAndSubsystemIsolation:
    def test_post_decode_glossary_variants_or_non_hotword_model_skip_all_reloads(self) -> None:
        # sensevoice does not support decoder hotwords -> effective_hotwords stays ""
        coord, ctx = _build_coordinator({"stt.model_id": "sensevoice"})
        coord.apply_audio_config()
        coord.configure_stt()
        ctx["recorder"].segmenter_profiles.clear()
        ctx["recognizer"].configure_calls.clear()

        ctx["config"].set("stt.glossary", [{"term": "SayInk", "variants": ["say ink"]}])
        coord.on_recognition_settings_changed()

        assert coord.recognition_change_pending is False
        assert coord.applied_recognition_settings.glossary[0]["term"] == "SayInk"
        assert ctx["recognizer"].configure_calls == []
        assert ctx["recorder"].segmenter_profiles == []
        assert ctx["speaker_rebuilds"][0] == 0

    def test_hotword_edit_on_funasr_reloads_only_asr_silently_without_hud_or_vad_rebuild(self) -> None:
        coord, ctx = _build_coordinator({"stt.model_id": "funasr-nano"})
        coord.apply_audio_config()
        coord.configure_stt()
        ctx["recorder"].segmenter_profiles.clear()
        ctx["recognizer"].configure_calls.clear()
        ctx["floating"].loading_messages.clear()

        # Model is already ready on funasr-nano -> hotword change reconfigures STT without loading HUD
        ctx["config"].set("stt.glossary", [{"term": "热词测试"}])
        coord.on_recognition_settings_changed()

        assert len(ctx["recognizer"].configure_calls) == 1
        assert ctx["recognizer"].configure_calls[0]["hotwords"] == "热词测试"
        assert ctx["floating"].loading_messages == []
        assert ctx["recorder"].segmenter_profiles == []
        assert ctx["speaker_rebuilds"][0] == 0

    def test_vad_and_speaker_changes_rebuild_only_their_respective_subsystems(self) -> None:
        coord, ctx = _build_coordinator({"stt.model_id": "funasr-nano"})
        coord.apply_audio_config()
        coord.configure_stt()
        ctx["recorder"].segmenter_profiles.clear()
        ctx["recognizer"].configure_calls.clear()

        ctx["config"].set("stt.debug_force_quiet_speech", False)
        coord.on_recognition_settings_changed()
        assert len(ctx["recorder"].segmenter_profiles) == 1
        assert ctx["recorder"].segmenter_profiles[0]["vad"] == VAD_SILERO
        assert ctx["recognizer"].configure_calls == []
        assert ctx["speaker_rebuilds"][0] == 0

        ctx["recorder"].segmenter_profiles.clear()
        ctx["config"].set("stt.debug_force_speaker_model", True)
        coord.on_recognition_settings_changed()
        assert ctx["speaker_rebuilds"][0] == 1
        assert ctx["recorder"].segmenter_profiles == []
        assert ctx["recognizer"].configure_calls == []

    def test_repaired_silero_at_same_path_is_reloaded_when_capture_finishes(self) -> None:
        coord, ctx = _build_coordinator()
        coord.apply_audio_config()
        recorder = ctx["recorder"]
        recorder.segmenter_profiles.clear()
        recorder.segmentation_status = SegmentationStatus(VAD_SILERO, VAD_ENERGY, True, "bad model")
        recorder.is_recording = True

        coord.on_models_changed()
        assert coord.recognition_change_pending
        assert not recorder.segmenter_profiles

        recorder.is_recording = False
        coord.apply_pending_recognition_settings()
        assert not coord.recognition_change_pending
        assert recorder.segmenter_profiles == [{
            "vad": VAD_SILERO,
            "vad_model_path": ctx["aux_paths"][AUX_MODEL_SILERO_VAD],
            "meeting": False,
        }]

    def test_mode_and_source_hot_switches_apply_policy_without_asr_reload(self) -> None:
        coord, ctx = _build_coordinator(
            {"audio.trigger_mode": "hotkey", "audio.input_source": "microphone"}
        )
        coord.apply_audio_config()
        coord.configure_stt()
        # Adaptive quiet speech passes None under Silero VAD.
        assert ctx["recorder"].quiet_speech_calls[-1] is None
        assert ctx["recognizer"].quiet_speech is None

        ctx["recognizer"].configure_calls.clear()
        ctx["speaker_rebuilds"][0] = 0

        # 1) hotkey(mic) -> continuous(mic): speaker stays off, zero ASR reload, zero embedder rebuild.
        ctx["config"].set("audio.trigger_mode", "continuous")
        coord.on_settings_changed()
        assert coord.applied_recognition_settings.speaker_model_enabled is False
        assert coord.applied_recognition_settings.speaker_activity_enabled is False
        assert ctx["speaker_rebuilds"][0] == 0
        assert ctx["recognizer"].configure_calls == []

        # 2) continuous(mic) -> continuous(mixed): auto-enables CAM++ & activity guard, zero ASR reload.
        ctx["config"].set("audio.input_source", "mixed")
        coord.on_settings_changed()
        assert coord.applied_recognition_settings.speaker_model_enabled is True
        assert coord.applied_recognition_settings.speaker_activity_enabled is True
        assert ctx["speaker_rebuilds"][0] == 1
        assert ctx["recognizer"].configure_calls == []

        # 3) continuous(mixed) -> continuous(system): speaker stays enabled, zero embedder rebuild, zero ASR reload.
        ctx["config"].set("audio.input_source", "system")
        coord.on_settings_changed()
        assert coord.applied_recognition_settings.speaker_model_enabled is True
        assert ctx["speaker_rebuilds"][0] == 1
        assert ctx["recognizer"].configure_calls == []

        # 4) continuous(system) -> hotkey(system): single-shot disables speaker separation, zero ASR reload.
        ctx["config"].set("audio.trigger_mode", "hotkey")
        coord.on_settings_changed()
        assert coord.applied_recognition_settings.speaker_model_enabled is False
        assert coord.applied_recognition_settings.speaker_activity_enabled is False
        assert ctx["speaker_rebuilds"][0] == 2
        assert ctx["recognizer"].configure_calls == []

        # 5) Energy VAD fallback disables recognizer quiet_speech adaptive gain.
        ctx["recorder"].segmentation_status = SegmentationStatus(VAD_ENERGY, VAD_ENERGY)
        coord.sync_recognizer_quiet_speech()
        assert ctx["recognizer"].quiet_speech is False


class TestReloadCoordinatorDeferralAndUnblocking:
    def test_recognition_change_defers_while_busy_and_unblocks_failed_model_queue(self) -> None:
        queued = [True]
        busy = [True]
        coord, ctx = _build_coordinator(
            {"stt.model_id": "funasr-nano"},
            pipeline_busy=lambda: busy[0],
            has_queued_segments=lambda: queued[0],
        )
        coord.configure_stt()
        ctx["recognizer"].configure_calls.clear()

        # Switch to paraformer-zh while pipeline is busy -> deferred
        ctx["config"].set("stt.model_id", "paraformer-zh")
        coord.on_recognition_settings_changed()
        assert coord.recognition_change_pending is True
        assert ctx["recognizer"].configure_calls == []
        assert coord.dictation_model_id() == "funasr-nano"

        # Pipeline idle, but queued audio is draining on a healthy ready model -> still deferred
        busy[0] = False
        coord.apply_pending_recognition_settings()
        assert coord.recognition_change_pending is True
        assert ctx["recognizer"].configure_calls == []

        # Current model failed (is_ready=False, is_loading=False): queued audio must NOT deadlock
        # the new model switch because only the new model can drain the queue!
        ctx["recognizer"].is_ready = False
        ctx["recognizer"].is_loading = False
        ctx["recognizer"].current_model_id = ""
        coord.apply_pending_recognition_settings()

        assert coord.recognition_change_pending is False
        assert len(ctx["recognizer"].configure_calls) == 1
        assert ctx["recognizer"].configure_calls[0]["model_id"] == "paraformer-zh"
        assert ctx["floating"].loading_messages  # Cold switch shows loading HUD

    def test_settings_defer_during_open_hold_and_recovery_and_exit_guards_short_circuit(self) -> None:
        recovery = [False]
        exiting = [False]
        coord, ctx = _build_coordinator(
            recovery_busy=lambda: recovery[0],
            exit_started=lambda: exiting[0],
        )
        coord.apply_audio_config()
        coord.configure_stt()
        ctx["recorder"].configured.clear()

        # Settings saved during active single-shot hold -> immediate prefs apply, audio deferred
        ctx["recorder"].is_recording = True
        ctx["config"].set("sound_enabled", False)
        ctx["config"].set("audio.input_source", "system")
        coord.on_settings_changed()
        assert ctx["sound"].enabled is False
        assert coord.deferred_audio_settings is True
        assert ctx["recorder"].configured == []

        # Hold finishes -> deferred audio settings commit without clearing queue
        ctx["recorder"].is_recording = False
        coord.apply_deferred_audio_settings()
        assert coord.deferred_audio_settings is False
        assert ctx["cleared_queues"][0] == 0
        assert ctx["recorder"].configured[-1]["input_source"] == "system"

        # Settings saved during audio recovery -> applied on resume_after_recovery
        recovery[0] = True
        ctx["config"].set("audio.input_source", "mixed")
        coord.on_settings_changed()
        assert coord.settings_after_recovery is True
        recovery[0] = False
        coord.resume_after_recovery()
        assert coord.settings_after_recovery is False
        assert ctx["recorder"].configured[-1]["input_source"] == "mixed"

        # Exit guard prevents any further reloads
        exiting[0] = True
        ctx["recorder"].configured.clear()
        ctx["config"].set("audio.input_source", "microphone")
        coord.on_settings_changed()
        coord.on_models_changed()
        coord.on_recognition_settings_changed()
        assert ctx["recorder"].configured == []


class TestReloadCoordinatorLoadEtaAndRuntimeStatus:
    def test_tracked_model_load_persists_duration_and_formats_eta_and_speaker_status(self) -> None:
        coord, ctx = _build_coordinator()
        assert coord.load_eta_text("funasr-nano") == "约 10–40 秒"

        msg = coord.begin_tracked_model_load("正在加载 Fun-ASR-Nano")
        assert "约 10–40 秒" in msg
        ctx["clock"][0] = 512.4
        coord.remember_load_duration()

        saved = ctx["config"].get(LOAD_SECONDS_KEY, {})
        assert saved["funasr-nano"] == 12.4
        assert coord.load_eta_text("funasr-nano") == "上次用时约 12 秒"

        ctx["recognizer"].is_loading = False
        ctx["recognizer"].is_ready = True
        assert coord.runtime_status().state is RuntimeState.READY
        assert "CAM++ 已下载 · 按需加载" in coord.speaker_runtime_status_text()


class TestAppHeadlessReloadDI:
    def test_create_headless_coordinates_deferred_reload_end_to_end(self, tmp_path: Path) -> None:
        cfg = Config(config_dir=tmp_path / "profile")
        app = App.create_headless(cfg)
        try:
            app._recognizer.configure.reset_mock()
            app._recorder.is_recording = True

            # Change model while recording -> deferred via ReloadCoordinator
            cfg.set("stt.model_id", "paraformer-zh")
            app._on_recognition_settings_changed()
            assert app._recognition_change_pending is True
            app._recognizer.configure.assert_not_called()

            # Recording stops -> pending model reload fires automatically
            app._recorder.is_recording = False
            app._apply_pending_recognition_settings()
            assert app._recognition_change_pending is False
            app._recognizer.configure.assert_called_once()
            assert app._recognizer.configure.call_args.args[0] == "paraformer-zh"
        finally:
            app.deleteLater()

import logging
import os
import sys
import time
from dataclasses import dataclass, replace
from typing import Callable, ClassVar
from uuid import uuid4

import numpy as np
import pyperclip
from PyQt6.QtCore import QEvent, QObject, Qt, QThread, QTimer, pyqtSignal

# Tests replace ``QTimer.singleShot`` to run or swallow delayed work. The
# child-timer path below must keep honoring that replacement.
_QT_SINGLE_SHOT = QTimer.singleShot
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon, QMessageBox

from sayink.ui.prompts import confirm

from sayink.config import (
    Config,
    # Tests replace ``Config`` with a mock constructor. Destination rules stay
    # on the real class, which only reads ``get`` / ``set``.
    Config as _ConfigRules,
    DEFAULT_HOTKEY,
    format_hotkey,
    TRIGGER_MODE_CONTINUOUS,
    TRIGGER_MODE_HOTKEY,
    TRIGGER_MODE_MEETING,
)
from sayink.hotkey_manager import (
    HotKeyManager,
    MIN_HOLD_MS,
    MIN_HOLD_CONTINUOUS_MS,
)
from sayink.audio_devices import (
    INPUT_SOURCE_MICROPHONE,
    INPUT_SOURCE_SYSTEM,
    INPUT_SOURCE_MIXED,
    input_source_label,
)
from sayink.audio_recorder import AudioRecorder
from sayink.audio_utils import TARGET_SAMPLE_RATE
from sayink.speaker_embedding import SpeakerEmbedder, make_embedder
from sayink.speaker_clustering import final_assignments
from sayink.speaker_session import SpeakerSession, recluster, voice_embedding
from sayink.meeting_speakers import MeetingSpeakerSession, voice_sample_is_clear
from sayink.speaker_turns import SpeakerTurnDetector
from sayink.speech_recognizer import (
    AUX_MODEL_SILERO_VAD,
    AUX_MODEL_SPEAKER,
    AUX_MODEL_SPEAKER_ACTIVITY,
    DEFAULT_MODEL_ID,
    SpeechRecognizer,
    set_models_dir,
    normalize_asr_output,
    join_segment_texts,
    restore_glossary_case,
)
from sayink.recognition_settings import RecognitionSettings, aux_model_path
from sayink.vad_segmenter import SILENCE_HOLD_SEC, VAD_SILERO
from sayink.history_store import HistoryStore, SegmentRecord
from sayink.meeting_session import (
    MeetingController,
    MeetingOptions,
    MeetingState,
    resolve_capabilities,
)
from sayink.text_polisher import TextPolisher
from sayink.asr_correction import apply_glossary_variants
from sayink.text_paster import PasteResult, TextPaster
from sayink.sound_manager import SoundManager
from sayink.ui.floating_window import FloatingWindow
from sayink.ui.tray_icon import TrayIcon
from sayink.ui.main_window import MainWindow
from sayink.runtime_status import (  # noqa: F401  (re-exported for callers and tests)
    ModelLoadPhase,
    OutputStage,
    RuntimeStatus,
    RuntimeState,
    SegmentationStatus,
    runtime_status_from_flags,
)
from sayink.update_controller import UpdateController
from sayink.onboarding import OnboardingController
from sayink.output_pipeline import (  # noqa: F401  (re-exported for callers and tests)
    OUTPUT_WATCHDOG_MS,
    RECENT_OUTPUT_CONTEXT_S,
    RESULT_TOAST_MS,
    RESULT_TOAST_WITH_HINT_MS,
    SEGMENT_PUMP_DELAY_MS,
    OutputPipeline,
    _PendingHistoryRecord,
    _QueuedSegment,
    _SegmentContext,
)
from sayink.reload_coordinator import (  # noqa: F401  (re-exported for callers and tests)
    LOAD_SECONDS_KEY,
    ReloadCoordinator,
)

log = logging.getLogger("SayInk")

MIN_AUDIO_SAMPLES = 1600  # 0.1s at 16kHz — ignore recordings shorter than this
# A hold at least this long that produced no audio was heard, not tapped: the
# speech gate never opened, so the hint is "no speech", not "too short".
HOLD_NO_SPEECH_SEC = 1.0
SHORT_TAP_TRAY_COOLDOWN_S = 300  # 托盘「按过短」提示最少间隔，避免输入法反复弹窗
# Queued speech beyond this pauses continuous listening (≈11 MB of float32);
# captured audio is never dropped, only further capture stops.
MAX_BACKLOG_AUDIO_SECONDS = 180

HISTORY_DISABLED_MEETING_REFUSAL = "历史数据库不可用，会议文字稿无法保存；请重启应用或在 设置 → 通用 更换历史位置后再开始会议"

BAR_REFRESH_AFTER_STOP_MS = 1200
MEETING_BAR_REFRESH_MS = 1500
MEETING_IDLE_REMINDER_SEC = 15 * 60
# One event-loop turn after the bar painted 「录音中」 before opening the device.
CONTINUOUS_START_DELAY_MS = 50
QUIT_POLL_MS = 80
QUIT_NATIVE_DRAIN_TIMEOUT_S = 12.0
UPDATE_CHECK_STARTUP_DELAY_MS = 5000


def _force_process_exit(code: int = 0) -> None:
    """Bypass interpreter teardown when a native C++ worker thread is wedged."""
    for handler in logging.getLogger().handlers:
        try:
            handler.flush()
        except Exception:
            pass
    os._exit(code)


from sayink.polish_fidelity import (  # noqa: F401  (re-exported for callers and tests)
    polish_looks_plausible,
    polish_rejection_reason,
)


def auto_start_command() -> str:
    """Command line for the Run registry key.

    A frozen build is its own executable; ``sys.executable`` is absolute there
    (``sys.argv[0]`` may be relative to wherever the shortcut started us). From
    a source checkout the interpreter alone would just open Python, so pass
    ``run.py`` as well.
    """
    exe = os.path.abspath(sys.executable)
    if getattr(sys, "frozen", False) or hasattr(sys, "_MEIPASS"):
        return f'"{exe}"'
    run_py = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "run.py")
    return f'"{exe}" "{run_py}"'


def _activity_state(embedder, requested: bool) -> str:
    """Multi-speaker detection as it actually runs this session, for the log."""
    if getattr(embedder, "activity_guard", None) is not None:
        return "已加载"
    if getattr(embedder, "activity_guard_error", ""):
        return "加载失败"
    return "未加载" if requested else "关"


def _delegate_attr(owner_getter: str, attr_name: str):
    """Forward legacy ``App._*`` state reads/writes to an extracted collaborator."""
    return property(
        lambda self: getattr(getattr(self, owner_getter)(), attr_name),
        lambda self, value: setattr(getattr(self, owner_getter)(), attr_name, value),
    )


@dataclass(frozen=True)
class AppModules:
    """Optional pre-built collaborators for DI and headless testing without global patches."""

    hotkey_mgr: HotKeyManager | None = None
    recorder: AudioRecorder | None = None
    recognizer: SpeechRecognizer | None = None
    polisher: TextPolisher | None = None
    paster: TextPaster | None = None
    sound: SoundManager | None = None
    history: HistoryStore | None = None
    meeting: MeetingController | None = None
    floating: FloatingWindow | None = None
    tray: TrayIcon | None = None
    updates: UpdateController | None = None
    onboarding: OnboardingController | None = None
    aux_model_path_fn: Callable[[str], object] | None = None
    is_model_downloaded_fn: Callable[[str], bool] | None = None
    set_models_dir_fn: Callable[[str | None], bool] | None = None
    skip_theme_init: bool = False


class App(QObject):
    """Central orchestrator that connects capture, recognition, meeting, UI, and lifecycle."""

    _history_committed = pyqtSignal()
    _history_failed = pyqtSignal(str)

    # Delegated output-pipeline state (keeps legacy App._* access intact)
    _output_busy = _delegate_attr("_output_pipeline", "busy")
    _output_token = _delegate_attr("_output_pipeline", "token")
    _output_raw_text = _delegate_attr("_output_pipeline", "raw_text")
    _output_stage = _delegate_attr("_output_pipeline", "stage")
    _output_stage_text = _delegate_attr("_output_pipeline", "stage_text")
    _output_stage_record = _delegate_attr("_output_pipeline", "stage_record")
    _output_pending_record = _delegate_attr("_output_pipeline", "output_pending_record")
    _pending_record = _delegate_attr("_output_pipeline", "pending_record")
    _output_llm_mode = _delegate_attr("_output_pipeline", "llm_mode")
    _output_glossary = _delegate_attr("_output_pipeline", "glossary")
    _last_output_text = _delegate_attr("_output_pipeline", "last_output_text")
    _last_output_at = _delegate_attr("_output_pipeline", "last_output_at")
    _output_dropped_slices = _delegate_attr("_output_pipeline", "dropped_slices")
    _keep_unsaved_continuous_text = _delegate_attr("_output_pipeline", "keep_unsaved_continuous_text")
    _history_failure_notified = _delegate_attr("_output_pipeline", "history_failure_notified")

    # Delegated reload-coordinator state (keeps legacy App._* access intact)
    _applied_recognition_settings = _delegate_attr("_reload_coordinator", "applied_recognition_settings")
    _recognition_change_pending = _delegate_attr("_reload_coordinator", "recognition_change_pending")
    _models_reload_pending = _delegate_attr("_reload_coordinator", "models_reload_pending")
    _deferred_audio_settings = _delegate_attr("_reload_coordinator", "deferred_audio_settings")
    _settings_after_recovery = _delegate_attr("_reload_coordinator", "settings_after_recovery")
    _applied_audio_key = _delegate_attr("_reload_coordinator", "applied_audio_key")
    _applied_recognition_key = _delegate_attr("_reload_coordinator", "applied_recognition_key")
    _applied_speaker_aux = _delegate_attr("_reload_coordinator", "applied_speaker_aux")
    _applied_vad_aux = _delegate_attr("_reload_coordinator", "applied_vad_aux")
    _load_started_at = _delegate_attr("_reload_coordinator", "load_started_at")
    _loading_model_id = _delegate_attr("_reload_coordinator", "loading_model_id")
    _models_dir_notice = _delegate_attr("_reload_coordinator", "models_dir_notice")

    # 友好化错误信息映射
    ERROR_HINTS: ClassVar[dict[str, str]] = {
        "已断开": "音频设备已断开\n请重新连接设备，或在设置 → 通用 → 录音与输入 中刷新并选择其他设备",
        "麦克风": "无法访问麦克风\n请检查：1) 麦克风是否已连接\n2) 是否被其他应用占用\n3) 系统隐私设置",
        "模型未就绪": "语音模型未就绪\n请打开 SayInk（双击托盘图标）→ 引擎 → 下载模型",
        "模型未下载": "语音模型未下载\n请打开 SayInk（双击托盘图标）→ 引擎 → 下载模型",
        "录音过短": "录音过短\n请按住快捷键说完一句再松开",
        "未识别": "未识别到语音内容\n请确保音频来源与设备正确，并靠近麦克风或播放电脑声音",
        "音频设备": "无法打开音频设备\n请在设置 → 通用 → 录音与输入 中刷新并选择设备",
        "系统声音": "无法采集系统声音\nWindows 可启用立体声混音或安装 PyAudioWPatch；混合模式需配置电脑播放设备",
        "输出失败": "输出失败\n请确保目标窗口可接收文本输入",
    }

    # 先匹配更具体的来源。``ERROR_HINTS`` 里「麦克风」写在「音频设备」和
    # 「系统声音」前面，按字典顺序会把混音失败说成麦克风问题。
    _ERROR_HINT_ORDER: ClassVar[tuple[str, ...]] = (
        "系统声音",
        "已断开",
        "音频设备",
        "麦克风",
        "模型未就绪",
        "模型未下载",
        "录音过短",
        "未识别",
        "输出失败",
    )

    @classmethod
    def _friendly_error(cls, original_msg: str) -> str:
        """将技术性错误信息转换为友好提示"""
        for keyword in cls._ERROR_HINT_ORDER:
            if keyword in original_msg:
                return cls.ERROR_HINTS[keyword]
        return original_msg

    @classmethod
    def create_headless(
        cls,
        config: Config | None = None,
        *,
        modules: AppModules | None = None,
        on_release_instance_lock: Callable[[], None] | None = None,
        **overrides,
    ) -> "App":
        """Construct a real ``App`` with lightweight mock collaborators and no OS/UI hooks."""
        from unittest.mock import MagicMock

        base = modules or AppModules()
        recorder = overrides.pop("recorder", base.recorder)
        if recorder is None:
            recorder = MagicMock()
            recorder.is_recording = False
            recorder.is_continuous = False
            recorder.storage_busy = False
            recorder.recovery_available = False
            recorder.input_source = (
                str(config.get("audio.input_source", "microphone"))
                if config is not None
                else "microphone"
            )
            recorder.input_source_display = "麦克风"
        recognizer = overrides.pop("recognizer", base.recognizer)
        if recognizer is None:
            recognizer = MagicMock()
            recognizer.is_ready = True
            recognizer.is_loading = False
            recognizer.is_silent_loading = False
            recognizer.current_model_id = (
                str(config.get("stt.model_id", DEFAULT_MODEL_ID) or DEFAULT_MODEL_ID)
                if config is not None
                else DEFAULT_MODEL_ID
            )
            recognizer.running_workers = ()
            recognizer.running_transcribe_workers = ()
        floating = overrides.pop("floating", base.floating)
        if floating is None:
            floating = MagicMock()
            floating.call_when_painted.side_effect = lambda fn: fn()
        fields: dict[str, object] = {
            "hotkey_mgr": overrides.pop("hotkey_mgr", base.hotkey_mgr) or MagicMock(),
            "recorder": recorder,
            "recognizer": recognizer,
            "polisher": overrides.pop("polisher", base.polisher) or MagicMock(),
            "paster": overrides.pop("paster", base.paster) or MagicMock(),
            "sound": overrides.pop("sound", base.sound) or MagicMock(),
            "floating": floating,
            "tray": overrides.pop("tray", base.tray) or MagicMock(),
            "meeting": overrides.pop("meeting", base.meeting),
            "updates": overrides.pop("updates", base.updates),
            "onboarding": overrides.pop("onboarding", base.onboarding),
            "aux_model_path_fn": overrides.pop(
                "aux_model_path_fn", base.aux_model_path_fn or (lambda _mid: None)
            ),
            "is_model_downloaded_fn": overrides.pop(
                "is_model_downloaded_fn", base.is_model_downloaded_fn or (lambda _mid: True)
            ),
            "set_models_dir_fn": overrides.pop(
                "set_models_dir_fn", base.set_models_dir_fn or (lambda _p: True)
            ),
            "skip_theme_init": overrides.pop("skip_theme_init", True),
        }
        history = overrides.pop("history", base.history)
        if history is None:
            history = MagicMock()
            history.disabled = False
        fields["history"] = history
        if overrides:
            unknown = ", ".join(sorted(overrides))
            raise TypeError(f"Unexpected create_headless collaborator(s): {unknown}")
        app = cls(
            config=config,
            modules=AppModules(**fields),  # type: ignore[arg-type]
            on_release_instance_lock=on_release_instance_lock,
        )
        app._onboarding.show_welcome = lambda: None
        app._onboarding.show_setup = lambda: None
        app._onboarding.ask_history_enabled = lambda: False
        app._updates.maybe_auto_check = lambda: None
        return app

    def __init__(
        self,
        config: Config | None = None,
        *,
        modules: AppModules | None = None,
        on_release_instance_lock: Callable[[], None] | None = None,
    ):
        super().__init__()

        self._modules = modules
        self._config = config if config is not None else Config()
        self._current_transcription = ""
        self._live_committed = ""
        self._live_raw_committed = ""
        self._live_inflight = ""
        self._hold_paste_sent = False
        self._hold_target = (0, "")
        self._hold_duration_ms = 0
        self._hold_failed_slices = 0
        self._hold_open_pending = False
        self._hold_opened_at = 0.0
        self._hold_request = 0
        self._is_transcribing = False
        self._segment_queue: list[_QueuedSegment] = []
        self._prefetch_segment: _QueuedSegment | None = None
        self._failed_cached_audio = []
        self._embedder: SpeakerEmbedder | None = None
        self._embedder_policy_key: tuple[bool, bool] | None = None
        self._reload_coord = self._build_reload_coordinator()
        self._output_pipe = self._build_output_pipeline()
        self._reload_coord.init_models_dir_notice()

        self._speakers = self._new_speaker_session()
        self._reclustered_session_id: str | None = None
        self._continuous_user_stopped = False
        self._current_session_id: str | None = None
        self._current_seq = 0
        self._pending_turn_context: _SegmentContext | None = None
        self._pending_turn_audio: np.ndarray | None = None
        self._short_tap_tray_last_at = 0.0
        self._hotkey_conflict_warned = False
        self._exit_dialog_open = False
        self._exit_started = False
        self._on_release_instance_lock = on_release_instance_lock
        self._instance_lock_released = False
        self._quit_drain_deadline: float | None = None
        self._quit_drain_timeout_s = QUIT_NATIVE_DRAIN_TIMEOUT_S
        self._recovery_busy = False
        self._meeting_capture_pending = False
        self._meeting_user_paused = False
        self._restore_main_after_menu = False
        self._hotkey_mgr: HotKeyManager
        self._recorder: AudioRecorder
        self._recognizer: SpeechRecognizer
        self._polisher: TextPolisher
        self._paster: TextPaster
        self._sound: SoundManager
        self._history: HistoryStore
        self._meeting: MeetingController
        self._floating: FloatingWindow
        self._tray: TrayIcon
        self._main: MainWindow | None = None

        log.info("正在初始化各模块...")
        self._init_modules()
        log.info("正在初始化界面...")
        self._init_ui()
        self._updates = (
            modules.updates
            if modules is not None and modules.updates is not None
            else UpdateController(
                self._config,
                parent=self,
                status_sink=self._settings_widget,
                tray=self._tray,
                show_about=lambda: self._show_main_window("about"),
                confirm_exit=lambda: self._confirm_exit(install_update=True),
                finish_quit=self._finish_quit,
                exit_started=lambda: self._exit_started,
            )
        )
        self._onboarding = (
            modules.onboarding
            if modules is not None and modules.onboarding is not None
            else OnboardingController(
                self._config,
                parent=self,
                dialog_parent=lambda: self._main,
                is_continuous_mode=self._is_continuous_mode,
                show_main_window=self._show_main_window,
            )
        )
        self._connect_signals()
        self._sync_hotkey_trigger_mode()
        self._configure_stt()
        self._update_tray_models()

    def _safe_attr(self, name: str, default=None):
        """Read an instance attribute without triggering ``QObject.__getattr__`` on ``__new__`` stubs."""
        return self.__dict__.get(name, default)

    def _aux_model_path(self, model_id: str):
        modules = self._safe_attr("_modules")
        if modules is not None and modules.aux_model_path_fn is not None:
            return modules.aux_model_path_fn(model_id)
        return aux_model_path(model_id)

    def _is_model_downloaded(self, model_id: str) -> bool:
        modules = self._safe_attr("_modules")
        if modules is not None and modules.is_model_downloaded_fn is not None:
            return bool(modules.is_model_downloaded_fn(model_id))
        from sayink.speech_recognizer import is_model_downloaded

        return bool(is_model_downloaded(model_id))

    def _build_output_pipeline(self) -> OutputPipeline:
        from sayink.asr_correction import correction_rejection_reason

        pipe = OutputPipeline(
            schedule_later=lambda ms, fn: self._later(ms, fn),
            pump_queue=lambda: self._pump_segment_queue(),
            schedule_queue_pump=lambda: self._schedule_queue_pump(),
            finish_empty_recognition=lambda: self._finish_empty_recognition(),
            refresh_continuous_ui=lambda: self._refresh_continuous_ui_after_output(),
            maybe_finish_dictation_speakers=lambda: self._maybe_finish_dictation_speakers(),
            stop_continuous_user_session=lambda: self._stop_continuous_user_session(),
            ask_history_output_conflict=lambda **kw: self._ask_history_output_conflict(**kw),
            glossary_for_pending_segment=lambda: self._glossary_for_pending_segment(),
            continuous_session_active=lambda: self._continuous_session_active(),
            continuous_user_stopped=lambda: bool(self._safe_attr("_continuous_user_stopped", False)),
            has_queued_segments=lambda: bool(self._safe_attr("_segment_queue")),
            meeting_active=lambda: self._meeting_active(),
            exit_started=lambda: bool(self._safe_attr("_exit_started", False)),
            hold_target=lambda: self._safe_attr("_hold_target", (0, "")),
            clear_transcribing=lambda: setattr(self, "_is_transcribing", False),
            friendly_error=self._friendly_error,
            output_text_dispatch=lambda text, **kw: self._output_text(text, **kw),
            handle_paste_result_dispatch=lambda result, **kw: self._handle_paste_result(result, **kw),
            copy_to_clipboard=lambda text: pyperclip.copy(text),
            now_monotonic=lambda: time.monotonic(),
            now_wall=lambda: time.time(),
            platform_name=lambda: sys.platform,
            watchdog_ms=lambda: OUTPUT_WATCHDOG_MS,
            recent_context_window_s=lambda: RECENT_OUTPUT_CONTEXT_S,
            polish_reject_fn=lambda raw, pol: polish_rejection_reason(raw, pol),
            correction_reject_fn=lambda raw, pol, g: correction_rejection_reason(raw, pol, g),
        )
        self._sync_output_pipeline_refs(pipe)
        return pipe

    def _sync_output_pipeline_refs(self, pipe: OutputPipeline) -> None:
        d = self.__dict__
        pipe.config = d.get("_config")
        pipe.polisher = d.get("_polisher")
        pipe.paster = d.get("_paster")
        pipe.history = d.get("_history")
        pipe.floating = d.get("_floating")
        pipe.tray = d.get("_tray")
        pipe.recorder = d.get("_recorder")

    def _output_pipeline(self) -> OutputPipeline:
        d = self.__dict__
        pipe = d.get("_output_pipe")
        if pipe is None:
            pipe = self._build_output_pipeline()
            d["_output_pipe"] = pipe
        else:
            self._sync_output_pipeline_refs(pipe)
        return pipe

    def _reset_continuous_stop_flags(self) -> None:
        self._continuous_user_stopped = False
        self._keep_unsaved_continuous_text = False

    def _rebuild_speaker_embedder(self) -> None:
        self._embedder = None
        self._embedder_policy_key = None
        self._speaker_embedder()
        if not self._meeting_active() and self._safe_attr("_current_session_id") is None:
            self._speakers = self._new_speaker_session()

    def _build_reload_coordinator(self) -> ReloadCoordinator:
        modules = self._safe_attr("_modules")
        set_dir_fn = (
            modules.set_models_dir_fn
            if modules is not None and modules.set_models_dir_fn is not None
            else (lambda p: set_models_dir(p))
        )
        coord = ReloadCoordinator(
            config=self._safe_attr("_config"),
            aux_model_path_fn=lambda mid: self._aux_model_path(mid),
            set_models_dir_fn=set_dir_fn,
            is_model_downloaded_fn=(
                modules.is_model_downloaded_fn
                if modules is not None and modules.is_model_downloaded_fn is not None
                else None
            ),
            now_monotonic=lambda: time.monotonic(),
            meeting_active=lambda: self._meeting_active(),
            hold_open_pending=lambda: bool(self._safe_attr("_hold_open_pending", False)),
            pipeline_busy=lambda: self._pipeline_busy(),
            has_queued_segments=lambda: bool(self._safe_attr("_segment_queue")),
            queued_segment_count=lambda: len(self._safe_attr("_segment_queue") or ()),
            recovery_busy=lambda: bool(self._safe_attr("_recovery_busy", False)),
            exit_started=lambda: bool(self._safe_attr("_exit_started", False)),
            clear_queued_audio=lambda: self._clear_queued_audio(),
            stop_continuous_listening=lambda: self._stop_continuous_listening(),
            reset_continuous_stop_flags=self._reset_continuous_stop_flags,
            rebuild_speaker_embedder=self._rebuild_speaker_embedder,
            sync_hotkey_trigger_mode=lambda: self._sync_hotkey_trigger_mode(),
            sync_settings_runtime_status=lambda status=None: self._sync_settings_runtime_status(status),
            sync_recognition_runtime_status=lambda: self._sync_recognition_runtime_status(),
            sync_tray_status_summary=lambda status=None: self._sync_tray_status_summary(status),
        )
        self._sync_reload_coordinator_refs(coord)
        return coord

    def _sync_reload_coordinator_refs(self, coord: ReloadCoordinator) -> None:
        d = self.__dict__
        coord.sync_config_if_needed(d.get("_config"))
        coord.recorder = d.get("_recorder")
        coord.recognizer = d.get("_recognizer")
        coord.floating = d.get("_floating")
        coord.tray = d.get("_tray")
        coord.sound = d.get("_sound")
        coord.paster = d.get("_paster")

    def _reload_coordinator(self) -> ReloadCoordinator:
        d = self.__dict__
        coord = d.get("_reload_coord")
        if coord is None:
            coord = self._build_reload_coordinator()
            d["_reload_coord"] = coord
        else:
            self._sync_reload_coordinator_refs(coord)
        return coord

    def _sync_models_dir(self) -> bool:
        return self._reload_coordinator().sync_models_dir()

    def _init_modules(self):
        m = self._modules
        self._hotkey_mgr = (
            m.hotkey_mgr
            if m is not None and m.hotkey_mgr is not None
            else HotKeyManager(self._config.get("hotkey", DEFAULT_HOTKEY))
        )
        self._recorder = m.recorder if m is not None and m.recorder is not None else AudioRecorder()
        cfg_dir = getattr(self._config, "config_dir", None)
        if isinstance(cfg_dir, (str, os.PathLike)):
            from pathlib import Path
            from sayink.audio_recovery import sweep_orphan_pending

            sweep_orphan_pending(Path(cfg_dir) / "audio-recovery")
        self._apply_audio_options()
        self._apply_audio_config()
        self._recognizer = (
            m.recognizer if m is not None and m.recognizer is not None else SpeechRecognizer()
        )
        self._polisher = m.polisher if m is not None and m.polisher is not None else TextPolisher()
        self._paster = (
            m.paster
            if m is not None and m.paster is not None
            else TextPaster(restore_clipboard=self._config.get("output.restore_clipboard", False))
        )
        self._sound = (
            m.sound
            if m is not None and m.sound is not None
            else SoundManager(enabled=self._config.get("sound_enabled", True))
        )
        self._history = (
            m.history
            if m is not None and m.history is not None
            else HistoryStore(db_path=self._config.config_dir / "history.db")
        )
        self._meeting = (
            m.meeting
            if m is not None and m.meeting is not None
            else MeetingController(
                self._config,
                self._history,
                pending_segments=self._pending_segment_count,
                parent=self,
            )
        )
        self._deferred_audio_settings = False
        self._meeting_idle_warned = False
        self._unrecognized_meeting_notice = ""
        self._meeting_idle_timer = QTimer(self)
        self._meeting_idle_timer.setInterval(30_000)
        self._meeting_idle_timer.timeout.connect(self._on_meeting_idle_tick)

    def _init_ui(self):
        m = self._modules
        self._floating = m.floating if m is not None and m.floating is not None else FloatingWindow()
        self._tray = m.tray if m is not None and m.tray is not None else TrayIcon()

        self._tray.set_auto_start(self._config.get("auto_start", False))
        self._tray.show()
        self._floating.set_input_source(
            self._config.get("audio.input_source", "microphone")
        )
        self._floating.set_esc_stops_continuous(self._config.get("audio.esc_stops_continuous", True))
        self._floating.position_committed.connect(self._on_listen_bar_position_committed)
        self._floating.placement_reset.connect(self._on_listen_bar_placement_reset)
        self._apply_listen_bar_preferences()
        if m is None or not m.skip_theme_init:
            # Re-apply after surfaces exist so cold-start dark/system is not stuck
            # on import-time light snapshots for inline-styled widgets.
            self.apply_appearance_theme()
            from sayink.ui.theme import watch_system_color_scheme

            watch_system_color_scheme(self._on_system_color_scheme_changed)

    def _on_system_color_scheme_changed(self) -> None:
        if self._config.get("appearance.theme_mode", "dark") == "system":
            log.info("系统浅/深色外观已变化，重新换肤")
            self.apply_appearance_theme()

    def _meeting_or_none(self) -> MeetingController | None:
        return self._safe_attr("_meeting")

    def _meeting_active(self) -> bool:
        """A meeting owns the audio pipeline (live, paused or draining)."""
        meeting = self._meeting_or_none()
        return meeting is not None and meeting.is_active

    def _capture_open(self) -> bool:
        """A hold or continuous listen still owns the device."""
        if self._safe_attr("_hold_open_pending", False):
            return True
        recorder = self._safe_attr("_recorder")
        return bool(recorder is not None and getattr(recorder, "is_recording", False))

    def _is_continuous_mode(self) -> bool:
        # A meeting is a continuous session whatever the dictation trigger
        # mode is: segments flow through the same VAD → queue → history path.
        if self._meeting_active():
            return True
        # The saved trigger mode can change while this capture is still open.
        # Stop, paste and segment identity stay on the mode that started it.
        if self._capture_open():
            locked = self._safe_attr("_locked_trigger_mode")
            if locked in (TRIGGER_MODE_HOTKEY, TRIGGER_MODE_CONTINUOUS):
                return locked == TRIGGER_MODE_CONTINUOUS
        return self._config.get("audio.trigger_mode", TRIGGER_MODE_HOTKEY) == TRIGGER_MODE_CONTINUOUS

    def _continuous_session_active(self) -> bool:
        """True while continuous listen session is running (hotkey started, not yet stopped)."""
        return self._is_continuous_mode() and self._recorder.is_continuous

    def _continuous_hotkey_label(self) -> str:
        return format_hotkey(self._config.get("hotkey", DEFAULT_HOTKEY))

    def _refresh_continuous_ui_after_output(self) -> None:
        if self._continuous_session_active():
            self._floating.show_listening()
        elif self._continuous_user_stopped and self._pending_segment_count() > 0:
            pending = self._pending_segment_count()
            self._floating.show_continuous_stopped(f"正在处理剩余内容（{pending} 段）")
        else:
            self._floating.dismiss_if_idle()

    def _sync_hotkey_trigger_mode(self) -> None:
        self._hotkey_mgr.set_continuous_trigger_mode(self._is_continuous_mode())

    def _connect_signals(self):
        self._hotkey_mgr.recording_start.connect(self._on_recording_start)
        self._hotkey_mgr.recording_stop.connect(self._on_recording_stop)
        self._hotkey_mgr.recording_cancel.connect(self._on_recording_cancel)
        self._hotkey_mgr.continuous_listen_start.connect(self._on_continuous_hotkey_start)
        self._hotkey_mgr.hotkey_tap_too_short.connect(self._on_hotkey_tap_too_short)
        self._hotkey_mgr.esc_pressed.connect(self._on_esc_pressed)
        self._history.add_committed_callback(self._history_committed.emit)
        self._history_committed.connect(self._refresh_open_history_ui)
        # Writer-thread failures hop to the GUI thread through the signal.
        self._history.add_failed_callback(self._history_failed.emit)
        self._history_failed.connect(self._on_history_write_failed)
        self._hotkey_mgr.listener_status.connect(self._on_hotkey_listener_status)

        self._recorder.volume_changed.connect(self._floating.update_volume)
        self._recorder.recording_finished.connect(self._on_recording_finished)
        self._recorder.segment_ready.connect(self._on_segment_ready)
        opened = getattr(self._recorder, "utterance_opened", None)
        if opened is not None and hasattr(opened, "connect"):
            opened.connect(self._on_utterance_opened)
        self._recorder.segmentation_changed.connect(self._on_segmentation_changed)
        self._recorder.error.connect(self._on_recorder_error)
        self._recorder.warning.connect(self._on_recorder_warning)
        self._recorder.no_speech_warning.connect(self._on_no_speech_warning)

        self._recognizer.final_result.connect(self._on_final_result)
        self._recognizer.meeting_result.connect(self._on_speaker_turn_result)
        self._recognizer.partial_result.connect(self._on_partial_result)
        self._recognizer.error.connect(self._on_recognizer_error)
        self._recognizer.ready.connect(self._on_stt_ready)
        self._recognizer.model_load_progress.connect(self._on_model_load_progress)

        self._polisher.polish_complete.connect(self._on_polish_complete)
        self._polisher.polish_error.connect(self._on_polish_error)

        self._floating.continuous_stop_requested.connect(self._stop_continuous_user_session)
        self._floating.settings_requested.connect(self._show_settings)
        self._floating.history_requested.connect(self._show_history_window)
        self._floating.meeting_mark_requested.connect(lambda: self._meeting.add_mark())
        self._floating.meeting_open_requested.connect(self._show_meeting_window)

        self._meeting.clock_tick.connect(self._floating.update_meeting_clock)
        self._meeting.notice.connect(self._on_meeting_notice)
        self._meeting.state_changed.connect(self._on_meeting_state_changed)
        self._meeting.generation_finished.connect(self._on_minutes_generated)
        self._meeting.generation_failed.connect(self._on_minutes_failed)

        self._tray.open_settings.connect(self._show_settings)
        self._tray.wake_island.connect(self._show_main_window)
        self._tray.history_requested.connect(self._show_history_window)
        self._tray.meeting_start_requested.connect(self._start_meeting_from_tray)
        self._tray.meeting_end_requested.connect(self._end_meeting)
        self._tray.meetings_requested.connect(self._show_meeting_window)
        self._tray.quit_app.connect(self._quit)
        self._tray.auto_start_toggled.connect(self._on_auto_start_toggled)
        self._tray.model_switched.connect(self._on_tray_model_switch)
        self._tray.menu_about_to_show.connect(self._note_main_before_tray_menu)
        self._tray.menu_closed.connect(self._restore_main_after_tray_menu)
        self._tray.check_update_requested.connect(self._updates.check_from_tray)
        self._tray.messageClicked.connect(self._updates.on_tray_message_clicked)

    def _audio_runtime_key(self) -> tuple[str, int, int]:
        return self._reload_coordinator().audio_runtime_key()

    def _recognition_runtime_key(self, settings: RecognitionSettings | None = None) -> tuple:
        return self._reload_coordinator().recognition_runtime_key(settings)

    def _speaker_aux_paths(self, settings: RecognitionSettings) -> tuple:
        return self._reload_coordinator().speaker_aux_paths(settings)

    def _vad_aux_path(self, vad: str):
        return self._reload_coordinator().vad_aux_path(vad)

    def _sync_recognizer_quiet_speech(self, shared: RecognitionSettings | None = None) -> None:
        self._reload_coordinator().sync_recognizer_quiet_speech(shared)

    def _apply_audio_config(self):
        self._reload_coordinator().apply_audio_config()

    def _apply_audio_options(self):
        self._reload_coordinator().apply_audio_options()

    def _apply_dictation_segmenter_profile(self) -> None:
        self._reload_coordinator().apply_dictation_segmenter_profile()

    def _set_boundary_context_for_model(self, model_id):
        self._reload_coordinator().set_boundary_context_for_model(model_id)

    def _configure_stt(self):
        self._reload_coordinator().configure_stt()

    # ── Recording flow ────────────────────────────────

    def _model_not_ready_message(self) -> str:
        if self._recognizer.is_loading:
            return "模型载入中，请稍候"
        return self._friendly_error("模型未就绪")

    def _show_model_not_ready(self) -> None:
        if self._recognizer.is_loading:
            self._floating.show_model_loading("正在加载语音模型，请稍候...")
            self._tray.set_activity_tooltip("loading")
            return
        hint = self._friendly_error("模型未就绪")
        self._floating.show_error(hint)
        self._tray.showMessage(
            "SayInk",
            hint,
            QSystemTrayIcon.MessageIcon.Warning,
            6000,
        )

    def _pending_segment_count(self) -> int:
        in_flight = bool(self._safe_attr("_is_transcribing", False)) or self._output_busy
        return len(self._safe_attr("_segment_queue") or ()) + (1 if in_flight else 0)

    def _discardable_segment_count(self) -> int:
        """Segments a settings change will actually drop.

        A live meeting keeps its queue. A sentence already in polish or paste
        is not cancelled by ``_clear_queued_audio``, so it must not be counted
        as something the confirmation dialog is about to discard.
        """
        if self._meeting_active():
            return 0
        pending = len(self._segment_queue)
        if self._prefetch_segment is not None:
            pending += 1
        return pending

    def _recording_blocks_mic_test(self) -> bool:
        recorder = self._safe_attr("_recorder")
        if recorder is None:
            return False
        return bool(
            recorder.is_recording
            or recorder.is_continuous
            or self._safe_attr("_hold_open_pending", False)
            or self._meeting_active()
        )

    def _drop_unrecognized_meeting_audio(self, reason: str) -> int:
        """Tell the user when draining drops audio because no model can take it."""
        count = len(self._segment_queue) + (1 if self._prefetch_segment is not None else 0)
        if count <= 0:
            return 0
        log.warning("%s，丢弃 %d 段未识别的会议语音", reason, count)
        self._clear_queued_audio()
        detail = (
            f"语音模型未就绪，会议末尾 {count} 段语音未能识别。"
            "若已开启录音恢复，可从原始录音找回。"
        )
        self._unrecognized_meeting_notice = detail
        meeting = self._meeting_or_none()
        if meeting is not None:
            meeting.notice.emit("会议语音未识别", detail)
        tray = self._safe_attr("_tray")
        show = getattr(tray, "showMessage", None)
        if callable(show):
            show("SayInk", detail, QSystemTrayIcon.MessageIcon.Warning, 8000)
        return count

    def _on_meeting_idle_tick(self) -> None:
        if not self._meeting_active() or self._meeting.state is not MeetingState.LIVE:
            return
        elapsed = self._recorder.seconds_since_last_speech()
        if not isinstance(elapsed, (int, float)) or elapsed < 60:
            self._meeting_idle_warned = False
            return
        if elapsed < MEETING_IDLE_REMINDER_SEC or self._meeting_idle_warned:
            return
        self._meeting_idle_warned = True
        minutes = max(1, int(elapsed // 60))
        detail = f"已连续 {minutes} 分钟没有检测到发言。如果会议已经结束，可以点结束。"
        self._meeting.notice.emit("会议可能已结束", detail)
        self._tray.showMessage("SayInk", detail, QSystemTrayIcon.MessageIcon.Information, 8000)

    def _pipeline_busy(self) -> bool:
        return bool(
            self._safe_attr("_is_transcribing", False)
            or self._output_busy
            or self._safe_attr("_prefetch_segment") is not None
            or self._safe_attr("_recovery_busy", False)
        )

    def _on_esc_pressed(self):
        if self._meeting_active():
            # Esc never ends a meeting: an hour of notes must not vanish on a
            # stray key. The bar's end button / tray menu do that.
            return
        if self._continuous_session_active():
            if self._config.get("audio.esc_stops_continuous", True):
                self._stop_continuous_user_session()
        # Hold-to-talk: the hotkey manager sends recording_cancel itself; a
        # second cancel from here would find the recorder stopped and hide
        # 「已取消」 at once.

    def _on_hotkey_listener_status(self, ok: bool, message: str):
        if ok:
            return
        self._tray.showMessage(
            "SayInk",
            message or "快捷键监听未能启动，请在设置中更换快捷键",
            QSystemTrayIcon.MessageIcon.Warning,
            8000,
        )
        self._floating.show_error(message or "快捷键监听未能启动")

    def _load_eta_text(self, model_id: str) -> str:
        return self._reload_coordinator().load_eta_text(model_id)

    def _remember_load_duration(self) -> None:
        self._reload_coordinator().remember_load_duration()

    def _on_model_load_progress(self, phase: ModelLoadPhase, msg: str):
        if phase is ModelLoadPhase.LOADING and getattr(self._recognizer, "is_silent_loading", False) is True:
            return
        if phase is ModelLoadPhase.READY:
            self._remember_load_duration()
            self._floating.clear_model_loading_lock()
            self._sync_settings_runtime_status()
            return
        if phase is ModelLoadPhase.FAILED:
            self._is_transcribing = False
            self._floating.clear_model_loading_lock()
            self._tray.set_activity_tooltip(None)
            self._sync_settings_runtime_status(
                RuntimeStatus(RuntimeState.UNAVAILABLE, "模型载入失败")
            )
            if self._meeting_active():
                self._meeting_capture_pending = False
                if self._recorder.is_continuous or self._recorder.is_recording:
                    self._recorder.stop_continuous()
                if self._meeting.state is MeetingState.DRAINING and not self._recognizer.is_ready:
                    self._drop_unrecognized_meeting_audio("模型载入失败")
                    self._refresh_meeting_bar()
                elif not self._meeting_user_paused:
                    self._meeting.pause("语音模型载入失败 · 请检查模型后在会议页继续或结束")
                    self._floating.show_meeting_paused(self._meeting.paused_detail)
            else:
                self._floating.show_error(self._friendly_error(msg))
            # Outside a meeting, queued audio stays until a model loads.
            if not self._recognizer.is_loading:
                self._pump_segment_queue()
            return
        if self._recorder.is_continuous:
            if self._meeting_active() and self._meeting.state is MeetingState.LIVE:
                log.info("模型重新加载，会议采集暂停")
                self._recorder.stop_continuous()
                self._meeting.pause("语音模型载入中 · 载入完成后自动继续")
                self._meeting_capture_pending = True
                self._floating.show_meeting_paused(self._meeting.paused_detail)
            else:
                log.info("模型重新加载，暂停持续监听")
                self._stop_continuous_listening()
        msg = self._reload_coordinator().begin_tracked_model_load(msg)
        self._floating.show_model_loading(msg)
        self._tray.set_activity_tooltip("loading")
        self._sync_settings_runtime_status()

    def _capture_source_name(self) -> str:
        source = getattr(self._recorder, "input_source", "")
        if source == INPUT_SOURCE_SYSTEM:
            return "电脑声音"
        if source == INPUT_SOURCE_MIXED:
            return "麦克风和电脑声音"
        return "麦克风"

    def _on_no_speech_warning(self):
        if self._meeting_active():
            return
        log.warning("持续监听长时间未检测到有效语音")
        source = self._capture_source_name()
        self._floating.show_warning(
            "似乎没采集到声音",
            f"请检查{source}设备与系统隐私权限",
        )
        self._tray.showMessage(
            "SayInk",
            f"持续监听中长时间未检测到语音，请检查{source}与设备设置。",
            QSystemTrayIcon.MessageIcon.Warning,
            6000,
        )

    def _meeting_blocks_hotkey(self) -> bool:
        """Dictation hotkeys are parked while a meeting owns the microphone."""
        if not self._meeting_active():
            return False
        # The bar title must match the meeting. A live capture is not paused;
        # only the dictation hotkey is parked. Refresh restores the real bar.
        if self._meeting.state is MeetingState.LIVE:
            self._floating.show_meeting(
                self._meeting.elapsed_text(),
                detail="转写快捷键在会议中不可用",
            )
        elif self._meeting.state is MeetingState.DRAINING:
            self._floating.show_meeting_paused(
                "转写快捷键在会议中不可用",
                title="会议结束中",
            )
        else:
            self._floating.show_meeting_paused("转写快捷键在会议中不可用")
        self._later(MEETING_BAR_REFRESH_MS, self._refresh_meeting_bar)
        return True

    def _refresh_meeting_bar(self) -> None:
        if not self._meeting_active():
            return
        if self._meeting.state is MeetingState.PAUSED:
            self._floating.show_meeting_paused(self._meeting.paused_detail)
        elif self._meeting.state is MeetingState.DRAINING:
            pending = self._pending_segment_count()
            self._floating.show_meeting_paused(
                f"正在整理文字稿（剩余 {pending} 段）" if pending else "正在整理文字稿",
                title="会议结束中",
            )
        else:
            self._floating.show_meeting(self._meeting.elapsed_text())

    def _on_recording_start(self):
        if self._recovery_busy:
            return
        if self._safe_attr("_exit_started", False):
            return
        if self._meeting_blocks_hotkey():
            return
        self._apply_pending_recognition_settings()
        if self._is_continuous_mode():
            return
        if not self._recognizer.is_ready:
            self._show_model_not_ready()
            return

        if self._is_transcribing or self._segment_queue:
            # A previous hold is still being recognized or waits in the queue
            # behind an output in flight. Starting a new hold now would merge
            # that utterance into this one, and Esc would discard it.
            log.warning("上一轮语音尚未识别完，忽略新的录音请求")
            self._floating.show_busy_transcribing()
            return

        self._hold_request += 1
        request = self._hold_request
        if not self._ensure_history_output_ready():
            return
        if request != self._hold_request:
            log.info("确认框期间快捷键已松开，不开始录音")
            self._reset_recording_ui_after_abort()
            return
        log.info("开始录音（来源: %s）...", self._recorder.input_source_display)
        self._locked_trigger_mode = TRIGGER_MODE_HOTKEY
        self._hold_target = self._read_paste_target()
        self._current_transcription = ""
        self._hold_paste_sent = False
        self._hold_duration_ms = 0
        self._hold_failed_slices = 0
        self._clear_live_transcript()
        self._sound.play_start()
        self._tray.set_recording(True)
        self._tray.set_activity_tooltip("recording")
        self._floating.show_recording()
        # Opening the device blocks the UI thread for ~0.5 s; until the bar
        # has painted 「录音中」 it would keep showing the previous 「已发送」.
        self._hold_open_pending = True
        self._floating.call_when_painted(self._open_hold_recorder)

    def _open_hold_recorder(self):
        if not self._hold_open_pending:
            return
        self._hold_open_pending = False
        self._hold_opened_at = time.monotonic()
        self._recorder.start(continuous=False)

    def _abort_pending_hold(self) -> bool:
        """The key went up / Esc before the device opened: nothing was recorded."""
        if not self._hold_open_pending:
            return False
        self._hold_open_pending = False
        self._reset_recording_ui_after_abort()
        self._later(0, self._apply_pending_recognition_settings)
        return True

    def _note_hold_abandoned_before_open(self) -> None:
        """A key-up or Esc before the device opens cancels the hold being confirmed."""
        if self._hold_open_pending or getattr(self._recorder, "is_recording", False):
            return
        self._hold_request += 1

    def _on_recording_stop(self):
        if self._meeting_active() or self._is_continuous_mode():
            return
        self._note_hold_abandoned_before_open()
        if self._abort_pending_hold():
            log.warning("录音设备尚未打开就已松开，忽略")
            self._floating.show_error(self._friendly_error("录音过短"))
            return
        if not self._recorder.is_recording:
            self._release_unstarted_hold()
            return

        log.info("停止录音，开始识别...")
        self._sound.play_stop()
        self._tray.set_recording(False)
        self._tray.set_activity_tooltip("recognizing")
        # The key is up: the bar must stop saying 「松开结束，Esc 取消」.
        self._floating.end_capture()
        self._recorder.stop()
        self._later(0, self._apply_pending_recognition_settings)

    def _reset_recording_ui_after_abort(self):
        """录音未真正开始或已结束时，收回托盘/浮窗状态（防止短按后浮窗常驻）。"""
        self._tray.set_recording(False)
        self._tray.set_activity_tooltip(None)
        if not self._is_continuous_mode():
            self._floating.dismiss_if_idle()

    def _release_unstarted_hold(self) -> None:
        """The key went up on a hold the app refused or failed to start. The
        bar is showing why (模型载入中 / 请稍候 / an error) and dismisses that
        itself; the tray keeps describing work still in flight."""
        self._tray.set_recording(False)
        if not (self._pipeline_busy() or self._recognizer.is_loading):
            self._tray.set_activity_tooltip(None)

    def _on_recording_cancel(self):
        if self._meeting_active() or self._is_continuous_mode():
            return
        self._note_hold_abandoned_before_open()
        if self._abort_pending_hold():
            self._floating.show_cancelled()
            return
        if not self._recorder.is_recording:
            # This hold never started (refused while the previous utterance
            # was still in the pipeline, or aborted early), so Esc has nothing
            # to cancel. The utterance already in flight was not asked to go.
            log.info("Esc：当前没有进行中的录音，忽略取消")
            self._release_unstarted_hold()
            return
        self._hold_paste_sent = True
        self._hold_duration_ms = 0
        # A hold cannot start while an earlier utterance is still queued
        # (_on_recording_start refuses), so everything here is this hold's:
        # the queue, the slice being recognised right now, and its record.
        self._clear_queued_audio()
        cancel_current = getattr(self._recognizer, "cancel_current", None)
        if callable(cancel_current) and cancel_current():
            log.info("Esc：放弃正在识别的切片")
        self._is_transcribing = False
        # Only this hold's row. A sentence already in polish lives in
        # `_output_pending_record` and keeps its original window.
        self._pending_record = None
        self._pending_turn_context = None
        self._pending_turn_audio = None
        self._clear_live_transcript()
        self._reset_recording_ui_after_abort()
        self._recorder.cancel()
        self._floating.show_cancelled()
        self._later(0, self._apply_pending_recognition_settings)

    def _on_continuous_hotkey_start(self):
        if self._recovery_busy:
            return
        if self._meeting_blocks_hotkey():
            return
        self._apply_pending_recognition_settings()
        if not self._is_continuous_mode():
            return
        if not self._recognizer.is_ready:
            log.warning("持续转写：模型未就绪，无法开始监听")
            self._show_model_not_ready()
            if not self._recognizer.is_loading:
                self._tray.showMessage(
                    "SayInk",
                    "语音模型未就绪。请在设置 → 引擎 中下载默认模型并等待加载完成。",
                    QSystemTrayIcon.MessageIcon.Warning,
                    6000,
                )
            return
        if self._recorder.is_continuous:
            log.debug("持续转写：已在监听中，忽略重复快捷键")
            return
        log.info("快捷键触发：开始持续转写")
        self._continuous_user_stopped = False
        self._keep_unsaved_continuous_text = False
        self._current_session_id = None
        self._current_seq = 0
        # Segments still queued from the previous session keep its speaker numbering.
        self._speakers = self._new_speaker_session()
        self._clear_live_transcript()
        self._publish_open_history_session()
        self._locked_trigger_mode = TRIGGER_MODE_CONTINUOUS
        self._start_continuous_listening()

    def _on_hotkey_tap_too_short(self):
        if self._meeting_active():
            return
        if self._continuous_session_active():
            log.debug("已在持续监听中，忽略快捷键短按提示")
            return

        hotkey = self._continuous_hotkey_label()
        hold_ms = (
            MIN_HOLD_CONTINUOUS_MS
            if self._is_continuous_mode()
            else MIN_HOLD_MS
        )
        hint = f"请按住 {hotkey} 约 {hold_ms / 1000:.2f} 秒以上"
        hotkey_raw = self._config.get("hotkey", "").lower()
        if "ctrl+space" in hotkey_raw.replace(" ", ""):
            hint += "。若与输入法冲突，可在设置中改为 Alt+Space"
        log.info("快捷键按过短: %s", hint)

        now = time.monotonic()
        cooldown = 0.0 if not self._hotkey_conflict_warned else SHORT_TAP_TRAY_COOLDOWN_S
        if now - self._short_tap_tray_last_at >= cooldown:
            self._hotkey_conflict_warned = True
            self._short_tap_tray_last_at = now
            self._tray.showMessage(
                "SayInk", hint, QSystemTrayIcon.MessageIcon.Information, 4000
            )
        else:
            # During cooldown, still give a light tray cue so short taps don't feel dead.
            self._tray.flash_attention()

        if not self._is_continuous_mode():
            self._floating.show_error("录音过短\n" + hint.split("。")[0])

    def _stop_continuous_user_session(self):
        """End the whole continuous listen session; in-flight transcription still completes."""
        if self._meeting_active():
            self._end_meeting()
            return
        if not self._is_continuous_mode() or not self._recorder.is_continuous:
            if not self._pending_segment_count():
                self._floating.dismiss_if_idle()
            return
        self._continuous_user_stopped = True
        log.info("用户停止持续转写会话（进行中的识别会继续完成）")
        if self._recorder.is_continuous or self._recorder.is_recording:
            self._recorder.stop_continuous()
        # Keep _current_session_id/_current_seq for late/queued segments (ADR-0001/0009).
        # Cleared on the next user start in _on_continuous_hotkey_start.
        self._enqueue_history_cleanup()
        self._tray.set_activity_tooltip(None)
        pending = self._pending_segment_count()
        detail = f"正在处理剩余内容（{pending} 段）" if pending else ""
        self._floating.show_continuous_stopped(detail)
        self._publish_open_history_session()
        if not pending:
            self._maybe_finish_dictation_speakers()
            self._apply_pending_recognition_settings()
            # Re-checks state: a session restarted within the delay keeps its bar.
            self._later(BAR_REFRESH_AFTER_STOP_MS, self._refresh_continuous_ui_after_output)

    def _on_recorder_error(self, error_msg: str):
        if self._safe_attr("_exit_started", False):
            return
        if self._meeting_active():
            # The meeting stays open. A dictation-style error toast would hide
            # the meeting bar a few seconds later.
            if self._meeting.state is MeetingState.DRAINING:
                log.warning("会议结束过程中采集报错: %s", error_msg)
                return
            if self._recorder.is_continuous or self._recorder.is_recording:
                self._recorder.stop_continuous()
            if self._meeting_user_paused:
                return
            self._meeting_capture_pending = False
            self._meeting.pause(f"采集中断：{self._friendly_error(error_msg)}")
            self._sound.play_error()
            self._floating.show_meeting_paused(self._meeting.paused_detail)
            return
        if self._recorder.is_continuous:
            self._stop_continuous_listening()
        self._tray.set_recording(False)
        self._tray.set_activity_tooltip(None)
        self._sound.play_error()
        self._floating.show_error(self._friendly_error(error_msg))

    def _on_recorder_warning(self, warning_msg: str):
        if self._safe_attr("_exit_started", False):
            return
        log.warning("%s", warning_msg)
        self._floating.show_warning("音频采集受限", warning_msg)
        self._tray.showMessage(
            "SayInk",
            warning_msg,
            QSystemTrayIcon.MessageIcon.Warning,
            10000,
        )
        self._pause_if_backlog_too_long()

    def _start_continuous_listening(self):
        if self._recovery_busy:
            return
        if self._safe_attr("_exit_started", False):
            return
        if not self._is_continuous_mode():
            return
        if not self._recognizer.is_ready:
            return
        if self._recorder.is_continuous:
            return
        if not self._ensure_history_output_ready():
            return
        self._locked_trigger_mode = TRIGGER_MODE_CONTINUOUS
        log.info("开启持续转写（来源: %s）", self._recorder.input_source_display)
        self._sound.play_start()

        def _begin():
            if not self._recognizer.is_ready or self._recognizer.is_loading:
                log.warning("延迟开启持续监听时模型仍未就绪，已取消")
                self._tray.set_activity_tooltip(None)
                self._show_model_not_ready()
                return
            self._recorder.start_continuous()
            if not self._recorder.is_continuous:
                log.warning("持续监听启动失败")
                self._tray.set_activity_tooltip(None)
                self._floating.show_error("无法开启持续监听\n请检查音频设备设置")
                return
            self._tray.set_activity_tooltip("listening")
            self._floating.show_listening(self._listening_target(""))
            if (
                self._config.get("audio.input_source") == "system"
                and self._recorder.is_continuous
            ):
                self._tray.showMessage(
                    "SayInk",
                    "正在采集系统播放声。请确认视频/会议声音走 Windows 默认扬声器（与环回设备一致）。",
                    QSystemTrayIcon.MessageIcon.Information,
                    5000,
                )

        self._later(CONTINUOUS_START_DELAY_MS, _begin)

    def _stop_continuous_listening(self):
        if self._recorder.is_continuous or self._recorder.is_recording:
            self._recorder.stop_continuous()
        self._tray.set_activity_tooltip(None)
        self._floating.dismiss_if_idle()

    def _hold_audio_until_ready(
        self,
        audio: np.ndarray,
        route: str = "",
        *,
        front: bool,
        context: _SegmentContext | None = None,
    ) -> None:
        """Keep captured audio until the model can transcribe it."""
        self._enqueue_audio(audio, route, front=front, context=context)
        if self._recognizer.is_loading:
            log.debug("模型加载中，语音段已排队（队列 %d）", len(self._segment_queue))
            self._floating.show_model_loading(
                "模型载入中，已录制的语音将排队等待识别…"
            )
            return
        log.warning("模型未就绪，语音段已保留（队列 %d）", len(self._segment_queue))
        self._show_model_not_ready()

    def _segment_route_from_recorder(self) -> str:
        consume = getattr(self._recorder, "consume_segment_route", None)
        if not callable(consume):
            return ""
        route = consume()
        return route if isinstance(route, str) else ""

    def _enqueue_audio(
        self,
        audio: np.ndarray,
        route: str = "",
        *,
        front: bool = False,
        context: _SegmentContext | None = None,
    ) -> None:
        if self._safe_attr("_exit_started", False):
            return
        segment = _QueuedSegment(audio=audio, route=route, context=context)
        # Only opt-in recording sessions may spill audio onto disk. Existing
        # queued items keep their own identity and original sample count.
        if (getattr(self._recorder, "recovery_available", False) is True
                and self._queued_audio_seconds() >= 120):
            from sayink.audio_recovery import store_pending
            from pathlib import Path
            try:
                pending_dir = getattr(self._recorder, "recovery_pending_dir", None)
                if not isinstance(pending_dir, Path):
                    pending_dir = self._config.config_dir / "audio-recovery" / "pending"
                segment.audio = store_pending(pending_dir, audio)
            except OSError as exc:
                log.warning("排队录音无法写入磁盘: %s", exc)
                self._tray.showMessage("SayInk", str(exc), QSystemTrayIcon.MessageIcon.Warning, 8000)
        if front:
            self._segment_queue.insert(0, segment)
            return
        self._segment_queue.append(segment)
        self._pause_if_backlog_too_long()

    def _pop_queued_audio(self) -> tuple[np.ndarray, str, _SegmentContext | None]:
        from sayink.audio_recovery import StoredAudio
        segment = self._segment_queue[0]
        audio = segment.load_audio()
        self._segment_queue.pop(0)
        if isinstance(segment.audio, StoredAudio):
            segment.audio.delete()
        return audio, segment.route, segment.context

    def _clear_queued_audio(self) -> None:
        from sayink.audio_recovery import StoredAudio
        if self._prefetch_segment is not None:
            self._prefetch_segment = None
            self._recognizer.cancel_current()
        for segment in self._segment_queue:
            if isinstance(segment.audio, StoredAudio):
                segment.audio.delete()
        self._segment_queue.clear()

    def _queued_audio(self) -> list[np.ndarray]:
        """Audio arrays waiting in the queue, oldest first (tests and logging)."""
        return [segment.load_audio() for segment in self._segment_queue]

    def _queued_audio_seconds(self) -> float:
        return sum(int(segment.audio.size) for segment in self._segment_queue) / TARGET_SAMPLE_RATE

    def _pause_if_backlog_too_long(self) -> None:
        if (getattr(self._recorder, "recovery_available", False) is True
                and sum(item.audio.size for item in self._segment_queue
                        if isinstance(item.audio, np.ndarray)) <= 135 * TARGET_SAMPLE_RATE):
            return
        if self._meeting_active() and self._meeting_user_paused:
            return
        if not self._continuous_session_active():
            return
        if self._queued_audio_seconds() <= MAX_BACKLOG_AUDIO_SECONDS:
            return
        log.warning(
            "识别积压 %d 段（约 %.0f 秒），暂停持续监听",
            len(self._segment_queue), self._queued_audio_seconds(),
        )
        if self._meeting_active():
            # Capture pauses, the meeting does not end; it resumes once the
            # backlog has drained (see _pump_segment_queue).
            self._recorder.stop_continuous()
            self._meeting.pause("识别跟不上，已暂停采集 · 处理完会自动继续")
            self._meeting_capture_pending = True
            self._floating.show_meeting_paused(self._meeting.paused_detail)
            self._tray.showMessage(
                "SayInk",
                "识别跟不上说话速度，会议采集已暂停；已录下的内容会继续处理，处理完自动继续。",
                QSystemTrayIcon.MessageIcon.Warning,
                8000,
            )
            return
        self._stop_continuous_user_session()
        pending = self._pending_segment_count()
        self._floating.show_continuous_stopped(
            f"识别跟不上，已暂停监听 · 剩余 {pending} 段处理中"
        )
        self._tray.showMessage(
            "SayInk",
            "识别或润色跟不上说话速度，已暂停监听。已录下的内容会继续处理，"
            "处理完后可再次按快捷键开始。",
            QSystemTrayIcon.MessageIcon.Warning,
            8000,
        )

    def _capture_segment_context(self) -> _SegmentContext | None:
        if not self._is_continuous_mode():
            return None
        if self._current_session_id is None:
            self._current_session_id = uuid4().hex
        context = _SegmentContext(
            session_id=self._current_session_id,
            seq=self._current_seq,
            source=self._history_source(),
            speakers=self._speakers,
        )
        if self._meeting_active() and self._meeting.current_id == self._current_session_id:
            context.trigger_mode = TRIGGER_MODE_MEETING
            context.model = self._meeting.capabilities.model_id
            # SpeakerEmbedder instances are callables (audio → vector).
            context.embed = self._meeting.embedder
        else:
            context.embed = self._speaker_embedder()
            context.model = self._dictation_model_id()
        context.target_hwnd, context.target_app = self._segment_target_from_recorder()
        self._current_seq += 1
        self._publish_open_history_session()
        return context

    def _history_output_blocked(self) -> bool:
        return self._output_pipeline().history_output_blocked()

    def _ensure_history_output_ready(self) -> bool:
        return self._output_pipeline().ensure_history_output_ready()

    def _copy_clipboard(self, text: str) -> bool:
        return self._output_pipeline().copy_clipboard(text)

    def _release_unsaved_hold(self) -> None:
        self._output_pipeline().release_unsaved_hold()

    def _ask_history_output_conflict(self, *, listening: bool = False) -> str:
        from PyQt6.QtWidgets import QWidget

        main = self._safe_attr("_main")
        parent = main if isinstance(main, QWidget) else None
        box = QMessageBox(parent)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("结果没有去处")
        if listening:
            box.setText("正在录音，但历史已关闭，这句话只会进历史，所以现在没有去处。")
            box.setInformativeText("可以开启历史，或只把当前模式改为输入光标。取消会把已经识别出的文字复制出来并停止。")
        else:
            box.setText("历史已关闭，当前结果只会进历史，所以不会开始录音。")
            box.setInformativeText("可以开启历史，或只把当前模式改为输入光标。")
        enable = box.addButton("开启历史", QMessageBox.ButtonRole.AcceptRole)
        paste = box.addButton("改为输入光标", QMessageBox.ButtonRole.ActionRole)
        cancel = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        enable.setAccessibleName("开启历史")
        paste.setAccessibleName("改为输入光标")
        cancel.setAccessibleName("取消")
        box.setDefaultButton(enable)
        box.exec()
        clicked = box.clickedButton()
        if clicked is enable:
            return "history"
        if clicked is paste:
            return "paste"
        return "cancel"

    def _read_paste_target(self) -> tuple[int, str]:
        """Foreground window at the moment a single-shot hold is accepted."""
        from sayink.text_paster import (
            TextPaster,
            get_foreground_process_name,
            get_foreground_window_info,
            target_rejects_synthetic_input,
        )

        info = get_foreground_window_info()
        hwnd = int(info[0] or 0)
        name = get_foreground_process_name() if hwnd else ""
        if hwnd and (
            TextPaster._is_own_window(TextPaster, info)
            or target_rejects_synthetic_input(info)
        ):
            hwnd = 0
        return hwnd, name

    def _segment_target_from_recorder(self) -> tuple[int, str]:
        consume = getattr(self._recorder, "consume_segment_target", None)
        if not callable(consume):
            return 0, ""
        value = consume()
        if isinstance(value, tuple) and len(value) == 2:
            return int(value[0] or 0), str(value[1] or "")
        return 0, ""

    def _paste_target_for_output(self) -> tuple[int, str]:
        return self._output_pipeline().paste_target_for_output()

    def _listening_target(self, name: str) -> str:
        """What the bar names after 「正在听」; never a window that gets no paste."""
        if _ConfigRules.history_only_for_capture(
            self._config, self._history_source(), trigger_mode=TRIGGER_MODE_CONTINUOUS,
        ):
            return "仅保存到历史"
        return name

    def _on_utterance_opened(self, hwnd: int, name: str) -> None:
        if self._meeting_active() or not self._is_continuous_mode():
            return
        show = getattr(self._floating, "show_listening", None)
        if callable(show):
            show(target_app=self._listening_target(str(name or "")))

    # ── Speaker numbering (shared with meetings) ──────

    def _speaker_embedder(self) -> SpeakerEmbedder:
        """CAM++ when downloaded and enabled by mode/source policy, else the mel
        summary; built once and dropped when the policy or aux models change."""
        shared = RecognitionSettings.from_config(self._config)
        policy_key = shared.speaker_key
        embedder = self._embedder
        if embedder is None or self._safe_attr("_embedder_policy_key") != policy_key:
            enabled = shared.speaker_model_enabled
            activity_enabled = shared.speaker_activity_enabled
            speaker_path = self._aux_model_path(AUX_MODEL_SPEAKER) if enabled else None
            activity_path = self._aux_model_path(AUX_MODEL_SPEAKER_ACTIVITY) if activity_enabled else None
            embedder = make_embedder(
                speaker_path,
                enabled=enabled,
                activity_enabled=activity_enabled,
            )
            self._embedder = embedder
            self._embedder_policy_key = policy_key
            self._applied_speaker_aux = (speaker_path, activity_path)
        return embedder

    def _new_speaker_session(self) -> MeetingSpeakerSession:
        return MeetingSpeakerSession.for_embedder(self._speaker_embedder())

    def _finish_dictation_speakers(self) -> None:
        """A continuous session has fully drained: re-cluster its speakers the
        way a meeting does and renumber the rows already in history."""
        session_id = self._current_session_id
        speakers = self._speakers
        if not session_id or speakers is None or session_id == self._reclustered_session_id:
            return
        if self._meeting_active() and self._meeting.current_id == session_id:
            return
        self._reclustered_session_id = session_id
        turns = [t for t in (getattr(speakers, "turns", None) or []) if getattr(t, "key", -1) >= 0]
        if turns:
            assignments = final_assignments(speakers, self._speaker_embedder().cluster_threshold)
            changed = {seq: sid for seq, sid in assignments.items() if seq >= 0}
            if changed:
                log.info("持续转写 %s 说话人重新归属 %d 段", session_id[:8], len(changed))
                self._history.relabel_session_segments(session_id, changed)
            return
        if speakers.speaker_count < 2:
            return
        mapping = recluster(speakers.history, threshold=self._speaker_embedder().recluster_threshold)
        if mapping and any(old != new for old, new in mapping.items()):
            log.info("持续转写 %s 说话人重聚类: %s", session_id[:8], mapping)
            self._history.relabel_session_speakers(session_id, mapping)

    def _maybe_finish_dictation_speakers(self) -> None:
        self._publish_open_history_session()
        if not self._continuous_user_stopped or self._recorder.is_continuous:
            return
        if self._pending_segment_count() > 0 or self._is_transcribing:
            return
        self._finish_dictation_speakers()

    def _segment_route_reliable_from_recorder(self) -> bool:
        consume = getattr(self._recorder, "consume_segment_route_reliable", None)
        if not callable(consume):
            return False
        return bool(consume())

    def _segment_captured_at_from_recorder(self) -> int:
        consume = getattr(self._recorder, "consume_segment_captured_at", None)
        if not callable(consume):
            return 0
        value = consume()
        return int(value) if isinstance(value, (int, float)) else 0

    def _on_segment_ready(self, audio: np.ndarray):
        route = self._segment_route_from_recorder()
        route_reliable = self._segment_route_reliable_from_recorder()
        captured_at = self._segment_captured_at_from_recorder()
        # SpeechSegmenter has already rejected independent short noises. A
        # legitimate continuation after its time limit may be shorter than
        # 100 ms; imposing another duration gate here would drop that tail.
        if audio.size == 0:
            if not self._is_continuous_mode():
                # The recorder counts this flush as delivered and sends no
                # recording_finished; finish the hold once stop() has returned.
                self._later(0, self._finish_hold_after_dropped_segment)
            return
        context = self._capture_segment_context()
        if context is not None:
            context.captured_at = captured_at
            context.route_reliable = route_reliable
        if not self._recognizer.is_ready:
            self._hold_audio_until_ready(audio, route, front=False, context=context)
            return
        if self._pipeline_busy() or self._segment_queue:
            self._enqueue_audio(audio, route, context=context)
            log.debug("转写排队，队列长度 %d", len(self._segment_queue))
            self._schedule_queue_pump()
            return
        self._begin_transcription(audio, route=route, context=context)

    def _finish_hold_after_dropped_segment(self) -> None:
        """A hold whose last flushed slice was too short to recognise still
        has to paste what the earlier slices produced (or say it heard nothing)."""
        if self._is_continuous_mode() or self._hold_paste_sent or self._recorder.is_recording:
            return
        if self._emit_hold_output_if_ready():
            return
        if self._is_transcribing or self._segment_queue:
            return  # an earlier slice is still being recognised; its result finishes the hold
        if not self._live_committed.strip():
            self._reset_recording_ui_after_abort()
            self._floating.show_error(self._friendly_error("未识别"))

    def _schedule_queue_pump(self) -> None:
        # Output may still be busy: the pump can use that time to prefetch.
        # Never schedule another native decode while one already owns ASR.
        if not self._is_transcribing and self._prefetch_segment is None:
            self._later(SEGMENT_PUMP_DELAY_MS, self._pump_segment_queue)

    # ── Recognition ───────────────────────────────────

    def _on_recording_finished(self, full_audio: np.ndarray):
        if self._safe_attr("_exit_started", False):
            return
        if full_audio.size < MIN_AUDIO_SAMPLES:
            if self._emit_hold_output_if_ready():
                return
            if (
                not self._is_continuous_mode()
                and (self._is_transcribing or self._segment_queue or self._live_committed.strip())
            ):
                return
            held_sec = time.monotonic() - self._hold_opened_at if self._hold_opened_at else 0.0
            self._hold_opened_at = 0.0
            self._reset_recording_ui_after_abort()
            if held_sec >= HOLD_NO_SPEECH_SEC:
                # The key was down long enough; the gate simply never heard
                # speech. "Too short" would send the user chasing the wrong fix.
                log.warning("按住 %.1f 秒但未检测到语音，忽略", held_sec)
                self._floating.show_error(self._friendly_error("未识别"))
            else:
                log.warning("录音过短 (%d 采样点)，忽略", full_audio.size)
                self._floating.show_error(self._friendly_error("录音过短"))
            return
        if self._pipeline_busy() or self._segment_queue:
            self._enqueue_audio(full_audio)
            self._schedule_queue_pump()
            return
        self._begin_transcription(full_audio)

    def _continuous_diarization_unused(self, context: _SegmentContext | None) -> bool:
        """True only when continuous dictation neither stores nor displays speaker IDs."""
        if context is None or context.trigger_mode != TRIGGER_MODE_CONTINUOUS:
            return False
        if bool(self._config.get("history.enabled", True)):
            return False
        return not _ConfigRules.history_only_for_capture(
            self._config, context.source, trigger_mode=TRIGGER_MODE_CONTINUOUS,
        )

    def _begin_transcription(
        self,
        audio: np.ndarray,
        route: str = "",
        context: _SegmentContext | None = None,
        recognized_turns: list | None = None,
    ):
        if not self._recognizer.is_ready:
            self._hold_audio_until_ready(audio, route, front=True, context=context)
            # Turn analysis may already have set this. Leaving it set blocks
            # `_on_stt_ready` from ever draining the queue.
            self._is_transcribing = False
            return
        if context is None:
            context = self._capture_segment_context()
        self._pending_record = self._build_pending_history_record(audio, context)
        skip_diarization = self._continuous_diarization_unused(context)
        analyze_turns = (
            not skip_diarization
            and context is not None
            and context.trigger_mode in (TRIGGER_MODE_CONTINUOUS, TRIGGER_MODE_MEETING)
            and context.recognized_text is None
            and getattr(context.embed, "supports_turn_detection", False) is True
        )
        self._pending_turn_context = context if analyze_turns else None
        self._pending_turn_audio = audio if analyze_turns else None
        speaker_id, speaker_route = (
            (0, route)
            if (analyze_turns or skip_diarization)
            else self._label_speaker(audio, route, context)
        )
        self._pending_record.speaker_id = speaker_id
        self._pending_record.speaker_route = speaker_route
        if not self._is_continuous_mode():
            self._hold_duration_ms += self._pending_record.duration_ms
        self._is_transcribing = True
        self._tray.set_activity_tooltip("recognizing")
        if self._continuous_user_stopped:
            self._floating.show_continuous_stopped("正在处理剩余内容 · 正在识别")
        else:
            self._floating.show_recognizing()
        if recognized_turns is not None:
            self._on_speaker_turn_result(recognized_turns)
        elif context is not None and context.recognized_text is not None:
            self._on_final_result(context.recognized_text)
        elif analyze_turns:
            analyzer = SpeakerTurnDetector(context.embed, context=getattr(context.speakers, "turn_context", None),
                                           start_ms=context.captured_at, route=f"{context.source}:{route}")
            self._recognizer.transcribe_final(audio, retry_empty=True, turn_analyzer=analyzer)
        else:
            # All three modes use the same bounded empty-result retry. Hold
            # output still waits for release and all captured pieces to finish.
            self._recognizer.transcribe_final(audio, retry_empty=True)

    def _on_speaker_turn_result(self, turns) -> None:
        """Insert recognized turns ahead of later captures, preserving their order.

        Only unsaved queue contexts move. Already persisted transcript sequences
        and manual edits remain stable. Normal output handles each turn once.
        """
        context = self._pending_turn_context
        audio = self._pending_turn_audio
        if self._safe_attr("_exit_started", False) or context is None or audio is None or not turns:
            if not self._safe_attr("_exit_started", False):
                self._is_transcribing = False
                self._pump_segment_queue()
            return
        if context.trigger_mode == TRIGGER_MODE_MEETING:
            if not self._meeting_active() or self._meeting.current_id != context.session_id:
                self._is_transcribing = False
                self._pump_segment_queue()
                return
        elif context.trigger_mode != TRIGGER_MODE_CONTINUOUS:
            return
        # Continuous dictation can already have restarted while this older
        # capture finishes. Its frozen context retains the original session
        # and matcher; only its own unsaved queue entries are renumbered.
        self._pending_turn_context = None
        self._pending_turn_audio = None
        extra = len(turns) - 1
        if extra:
            for queued in self._segment_queue:
                if (queued.context is not None and queued.context.session_id == context.session_id
                        and queued.context.seq > context.seq):
                    queued.context.seq += extra
            if self._current_session_id == context.session_id:
                self._current_seq += extra
        prepared = []
        route = self._pending_record.speaker_route if self._pending_record is not None else ""
        if len(turns) > 1 and context.source == "mixed" and not context.route_reliable:
            # A dominant lane for the whole chunk does not identify each
            # individual turn. A lane that was captured on its own keeps
            # its route when the turn detector splits that lane.
            route = ""
        for index, turn in enumerate(turns):
            turn_context = replace(
                context, seq=context.seq + index,
                captured_at=context.captured_at + turn.start * 1000 // TARGET_SAMPLE_RATE if context.captured_at else 0,
                recognized_text=turn.text, speaker_vector=turn.embedding, speaker_learnable=turn.learnable,
            )
            prepared.append(_QueuedSegment(audio[turn.start:turn.stop], route, turn_context))
        self._segment_queue[0:0] = prepared[1:]
        first = prepared[0]
        self._begin_transcription(first.audio, route=first.route, context=first.context)

    def _utterance_history_only(self, record) -> bool:
        return self._output_pipeline().utterance_history_only(record)

    def _history_source(self) -> str:
        return {
            INPUT_SOURCE_MICROPHONE: "mic",
            INPUT_SOURCE_SYSTEM: "system",
            INPUT_SOURCE_MIXED: "mixed",
        }.get(getattr(self._recorder, "input_source", INPUT_SOURCE_MICROPHONE), "mic")

    def _build_pending_history_record(
        self,
        audio: np.ndarray,
        context: _SegmentContext | None = None,
    ) -> _PendingHistoryRecord:
        captured_at = 0
        if context is not None:
            trigger_mode = context.trigger_mode or TRIGGER_MODE_CONTINUOUS
            session_id, seq, source = context.session_id, context.seq, context.source
            captured_at = context.captured_at
        else:
            trigger_mode = TRIGGER_MODE_HOTKEY
            session_id, seq, source = uuid4().hex, 0, self._history_source()

        model = (context.model if context is not None else "") or self._dictation_model_id()
        if context is not None:
            target_hwnd = int(context.target_hwnd or 0)
            target_app = context.target_app or ""
        elif not self._is_continuous_mode():
            target_hwnd, target_app = self._hold_target
        else:
            target_hwnd, target_app = 0, ""
        return _PendingHistoryRecord(
            session_id=session_id,
            seq=seq,
            raw_text="",
            polished_text="",
            source=source,
            duration_ms=int(len(audio) / TARGET_SAMPLE_RATE * 1000),
            trigger_mode=trigger_mode,
            model=model,
            captured_at=captured_at,
            target_hwnd=int(target_hwnd or 0),
            target_app=str(target_app or ""),
        )

    def _clear_live_transcript(self) -> None:
        self._live_committed = ""
        self._live_raw_committed = ""
        self._live_inflight = ""
        self._floating.clear_live_transcript()

    def _show_live_transcript(self) -> None:
        parts = [self._live_committed, self._live_inflight]
        text = join_segment_texts(parts)
        if text:
            self._floating.show_live_transcript(text)

    def _on_partial_result(self, text: str) -> None:
        if self._safe_attr("_exit_started", False):
            return
        piece = (text or "").strip()
        if not piece:
            return
        self._live_inflight = piece
        self._show_live_transcript()

    def _commit_live_transcript(self, text: str, *, raw_text: str | None = None) -> None:
        piece = (text or "").strip()
        if not piece:
            return
        if self._live_committed:
            self._live_committed = join_segment_texts([self._live_committed, piece])
        else:
            self._live_committed = piece
        self._live_raw_committed = join_segment_texts([
            self._live_raw_committed, piece if raw_text is None else raw_text,
        ])
        self._live_inflight = ""
        self._show_live_transcript()

    def _defer_hold_output(self) -> bool:
        """Hold-to-talk keeps slices on the bar and pastes once the key is up."""
        if self._is_continuous_mode() or self._hold_paste_sent:
            return False
        return bool(self._recorder.is_recording or self._segment_queue)

    def _emit_hold_output_if_ready(self) -> bool:
        """Paste the whole hold utterance once nothing is left to recognize."""
        if self._is_continuous_mode() or self._hold_paste_sent:
            return False
        if self._recorder.is_recording or self._is_transcribing or self._segment_queue:
            return False
        text = (self._live_committed or "").strip()
        if not text:
            return False
        self._hold_paste_sent = True
        self._output_dropped_slices = self._hold_failed_slices
        self._current_transcription = text
        if self._pending_record is not None:
            self._pending_record.raw_text = self._live_raw_committed or text
            if self._hold_duration_ms:
                self._pending_record.duration_ms = self._hold_duration_ms
        self._deliver_recognized_text(text)
        return True

    def _record_for_output(self):
        return self._output_pipeline().record_for_output()

    def _claim_pending_record_for_output(self) -> None:
        self._output_pipeline().claim_pending_record_for_output()

    def _glossary_for_pending_segment(self) -> list:
        """The meeting's own terms for a meeting row, else the applied dictation glossary."""
        record = self._record_for_output()
        meeting = self._meeting_or_none()
        if (
            record is not None
            and record.trigger_mode == TRIGGER_MODE_MEETING
            and meeting is not None
            and meeting.current is not None
        ):
            return meeting.current.glossary
        return self._applied_recognition_settings.glossary

    def _on_final_result(self, text: str):
        if self._safe_attr("_exit_started", False):
            return
        glossary = self._glossary_for_pending_segment()
        raw_text = normalize_asr_output(text)
        text = apply_glossary_variants(
            restore_glossary_case(raw_text, glossary), glossary,
        )
        if self._hold_paste_sent and not self._is_continuous_mode():
            self._is_transcribing = False
            self._pump_segment_queue()
            return
        if text.strip():
            log.debug("识别结果长度: %d 字符", len(text))
            self._current_transcription = text
            self._commit_live_transcript(text, raw_text=raw_text)
            if self._pending_record is not None:
                self._pending_record.raw_text = raw_text
        else:
            log.warning("未识别到语音内容")

        self._is_transcribing = False
        if self._defer_hold_output():
            if self._recognizer.is_loading:
                self._floating.show_model_loading("模型载入中，请稍候…")
                self._tray.set_activity_tooltip("loading")
            elif self._recorder.is_recording:
                self._tray.set_activity_tooltip("recording")
                self._floating.show_recording()
            self._pump_segment_queue()
            return

        if not self._is_continuous_mode():
            if self._emit_hold_output_if_ready():
                return
            if not text.strip():
                self._finish_empty_recognition()
            return

        if not text.strip():
            self._finish_empty_recognition()
            return
        self._deliver_recognized_text(text)

    def _finish_empty_recognition(self) -> None:
        if self._recognizer.is_loading:
            self._floating.show_model_loading("模型载入中，请稍候…")
            self._tray.set_activity_tooltip("loading")
            self._pump_segment_queue()
            return
        self._tray.set_activity_tooltip(
            "listening" if self._continuous_session_active() else None
        )
        if self._continuous_session_active():
            self._floating.show_listening()
        elif self._is_continuous_mode():
            self._refresh_continuous_ui_after_output()
        elif self._recorder.is_recording:
            self._floating.show_recording()
        else:
            self._floating.show_error(self._friendly_error("未识别"))
        self._pump_segment_queue()

    def _later(self, msec: int, slot) -> None:
        """One-shot timer owned by this app.

        ``QTimer.singleShot`` is parented to the process, so it still fired
        after the window was gone and could copy text into a later task.
        A child timer stops on exit with the other ``QTimer``s.
        """
        if QTimer.singleShot is not _QT_SINGLE_SHOT:
            QTimer.singleShot(msec, slot)
            return
        try:
            timer = QTimer(self)
        except RuntimeError:
            _QT_SINGLE_SHOT(msec, slot)
            return
        timer.setSingleShot(True)

        def fire() -> None:
            timer.deleteLater()
            slot()

        timer.timeout.connect(fire)
        timer.start(msec)

    def _mark_output_busy(self) -> None:
        self._output_pipeline().mark_busy()

    def _release_stuck_output(self, token: int) -> None:
        self._output_pipeline().release_stuck_output(token)

    def _deliver_recognized_text(self, text: str) -> None:
        self._output_pipeline().deliver_recognized_text(text)

    def _recent_output_context(self) -> str:
        return self._output_pipeline().recent_output_context()

    def _on_recognizer_error(self, error_msg: str):
        self._pending_turn_context = None
        self._pending_turn_audio = None
        self._is_transcribing = False
        if self._recognizer.is_loading:
            return
        if self._meeting_active():
            log.warning("会议识别失败: %s", error_msg)
            if not self._recognizer.is_ready and not self._recognizer.is_loading:
                if self._meeting.state is MeetingState.DRAINING:
                    self._drop_unrecognized_meeting_audio("会议识别失败")
                elif not self._meeting_user_paused:
                    self._meeting_capture_pending = False
                    if self._recorder.is_continuous or self._recorder.is_recording:
                        self._recorder.stop_continuous()
                    self._meeting.pause(f"识别中断：{self._friendly_error(error_msg)}")
                    self._sound.play_error()
                    self._floating.show_meeting_paused(self._meeting.paused_detail)
            else:
                self._refresh_meeting_bar()
            self._pump_segment_queue()
            return
        if not self._is_continuous_mode():
            if not self._hold_paste_sent:
                self._hold_failed_slices += 1
            if self._emit_hold_output_if_ready():
                return
            if self._defer_hold_output():
                if self._recorder.is_recording:
                    self._tray.set_activity_tooltip("recording")
                    self._floating.show_recording()
                self._pump_segment_queue()
                return
        self._floating.clear_model_loading_lock()
        self._tray.set_activity_tooltip("listening" if self._is_continuous_mode() else None)
        self._sound.play_error()
        self._floating.show_error(self._friendly_error(error_msg))
        self._pump_segment_queue()

    def _pump_segment_queue(self):
        if self._prefetch_segment is not None:
            return
        if self._output_busy and not self._is_transcribing:
            self._prefetch_next_segment()
            return
        if self._pipeline_busy() or not self._segment_queue:
            if not self._pipeline_busy():
                # Empty or failed final turns have no paste callback. They
                # still complete a stopped session before settings change.
                self._maybe_finish_dictation_speakers()
                self._apply_pending_recognition_settings()
                self._resume_meeting_capture_if_pending()
            if self._continuous_session_active() and not self._pipeline_busy():
                self._floating.show_listening()
            return
        if not self._recognizer.is_ready:
            return
        prepared_turns = self._segment_queue[0].recognized_turns
        try:
            next_audio, next_route, next_context = self._pop_queued_audio()
        except OSError as exc:
            # Preserve the corrupt file for inspection and the raw journal for
            # manual recovery. A failed disk read must not wedge every later
            # segment behind it or keep recording an ever-growing backlog.
            self._failed_cached_audio.append(self._segment_queue.pop(0))
            if self._recorder.is_recording:
                if self._recorder.is_continuous:
                    self._continuous_user_stopped = True
                    self._recorder.stop_continuous()
                else:
                    self._recorder.stop()
            if self._meeting_active():
                self._meeting_capture_pending = False
                self._meeting.pause("排队录音读取失败，已暂停采集；请从录音恢复核对原声。")
                self._floating.show_meeting_paused(self._meeting.paused_detail)
            else:
                self._floating.show_error(str(exc))
            self._tray.showMessage("SayInk", str(exc), QSystemTrayIcon.MessageIcon.Warning, 8000)
            self._schedule_queue_pump()
            return
        self._begin_transcription(next_audio, route=next_route, context=next_context,
                                  recognized_turns=prepared_turns)

    def _prefetch_next_segment(self) -> None:
        """Overlap one ASR job with output, keeping its original queue identity.

        Pending history, speaker assignment and paste state still belong to
        the current output. Cache only the next segment; failures leave its
        audio in place for the normal recognition path to retry.
        """
        if (not self._segment_queue or not self._recognizer.is_ready
                or getattr(self._recognizer, "supports_prefetch", False) is not True):
            return
        segment = self._segment_queue[0]
        context = segment.context
        if (context is None or context.trigger_mode not in (TRIGGER_MODE_CONTINUOUS, TRIGGER_MODE_MEETING)
                or context.recognized_text is not None or segment.prefetch_attempted):
            return
        analyzer = None
        if (
            not self._continuous_diarization_unused(context)
            and getattr(context.embed, "supports_turn_detection", False) is True
        ):
            analyzer = SpeakerTurnDetector(context.embed, context=getattr(context.speakers, "turn_context", None),
                                            start_ms=context.captured_at, route=f"{context.source}:{segment.route}")
        segment.prefetch_attempted = True
        self._prefetch_segment = segment

        def complete(*, text=None, turns=None, error=None):
            if self._prefetch_segment is not segment:
                return
            self._prefetch_segment = None
            if self._exit_started or not any(item is segment for item in self._segment_queue):
                return
            if error is not None or turns == []:
                log.warning("预识别失败，保留音频等待正常识别")
            elif turns is not None:
                segment.recognized_turns = turns
            else:
                context.recognized_text = text
            self._schedule_queue_pump()

        try:
            self._recognizer.prefetch_final(
                segment.load_audio(), turn_analyzer=analyzer,
                on_text=lambda text: complete(text=text),
                on_turns=lambda turns: complete(turns=turns),
                on_error=lambda error: complete(error=error),
            )
        except Exception:
            log.exception("无法启动预识别，保留音频")
            complete(error=True)

    def _on_stt_ready(self):
        if getattr(self._recognizer, "last_ready_silent", False) is True:
            self._sync_settings_runtime_status()
            if self._segment_queue and not self._is_transcribing:
                self._pump_segment_queue()
            return
        self._floating.clear_model_loading_lock()
        self._tray.set_activity_tooltip(None)
        self._sync_settings_runtime_status()
        if self._segment_queue and not self._is_transcribing:
            log.info("模型就绪，处理排队语音 %d 段", len(self._segment_queue))
            self._pump_segment_queue()
        if self._meeting_active():
            self._resume_meeting_capture_if_pending()
            self._refresh_meeting_bar()
            return
        hotkey = self._continuous_hotkey_label()
        if self._is_continuous_mode():
            # Loading HUD stays visible until replaced/dismissed; continuous mode
            # no longer shows idle float, so dismiss explicitly after ready.
            self._floating.dismiss_if_idle()
            stop = (
                "按 Esc 或转写条「结束」停止整场。"
                if self._config.get("audio.esc_stops_continuous", True)
                else "用转写条「结束」停止整场。"
            )
            tray_msg = (
                f"持续转写已就绪。按住 {hotkey} 开始监听，说话停顿约 {SILENCE_HOLD_SEC:g} 秒后自动输入，不用点结束；"
                + stop
            )
        else:
            log.info("✓ 语音识别模型已就绪，按 %s 开始语音输入", hotkey)
            self._floating.show_success("已就绪", f"按 {hotkey} 开始语音输入")
            tray_msg = f"已就绪，按 {hotkey} 开始语音输入"
        self._tray.showMessage(
            "SayInk",
            tray_msg,
            QSystemTrayIcon.MessageIcon.Information,
            6000 if self._is_continuous_mode() else 3000,
        )

    # ── Polishing & Output ────────────────────────────

    def _on_polish_complete(self, polished_text: str):
        self._output_pipeline().on_polish_complete(polished_text)

    def _on_polish_error(self, error_msg: str):
        self._output_pipeline().on_polish_error(error_msg)

    def _output_text(self, text: str, *, degraded_from_polish: bool = False):
        self._output_pipeline().output_text(text, degraded_from_polish=degraded_from_polish)

    def _freeze_pending_history_record(
        self,
        output_text: str,
        degraded_from_polish: bool,
    ) -> SegmentRecord | None:
        return self._output_pipeline().freeze_pending_history_record(
            output_text, degraded_from_polish
        )

    def _label_speaker(
        self,
        audio: np.ndarray,
        route: str,
        context: _SegmentContext | None,
    ) -> tuple[int, str]:
        """Number this segment inside its own continuous session only."""
        if context is None:
            return 0, ""
        stored_route = ""
        if context.source == "mixed" and route in ("mic", "system"):
            stored_route = route
        embedding = context.speaker_vector
        if embedding is None:
            embed = context.embed if callable(context.embed) else voice_embedding
            try:
                embedding = embed(audio)
            except Exception as e:  # a broken speaker model must not drop the words
                log.warning("说话人特征提取失败，本段说话人待确认: %s", e)
                embedding = np.zeros(1, dtype=np.float32)
        # Wall-clock bounds let the session apply its continuity prior.
        start_ms = int(context.captured_at or 0)
        end_ms = start_ms + int(audio.size * 1000 / TARGET_SAMPLE_RATE) if start_ms else 0
        if isinstance(context.speakers, MeetingSpeakerSession):
            speaker_id = context.speakers.assign(
                embedding, stored_route, start_ms=start_ms, end_ms=end_ms,
                sample_key=context.seq, learnable=(voice_sample_is_clear(audio) if context.speaker_learnable is None
                                                  else context.speaker_learnable),
            )
        else:
            speaker_id = context.speakers.assign(embedding, stored_route, start_ms=start_ms, end_ms=end_ms)
        return speaker_id, stored_route

    def _show_post_output_floating(
        self,
        title: str,
        hint: str,
        *,
        stopped_text: str,
        use_info: bool,
        refresh_ms: int,
    ) -> None:
        self._output_pipeline().show_post_output_floating(
            title,
            hint,
            stopped_text=stopped_text,
            use_info=use_info,
            refresh_ms=refresh_ms,
        )

    def _handle_paste_result(
        self,
        result: PasteResult | str,
        *,
        degraded_from_polish: bool = False,
        record: SegmentRecord | None = None,
    ):
        self._output_pipeline().handle_paste_result(
            result,
            degraded_from_polish=degraded_from_polish,
            record=record,
        )

    def _refresh_open_history_ui(self) -> None:
        if self._main is None:
            return
        self._main.history_panel.refresh()

    def _show_history_startup_notice(self) -> None:
        """Tell the user once if history.db was rebuilt or could not be opened,
        or the configured models folder could not be used."""
        if self._models_dir_notice:
            log.warning(self._models_dir_notice)
            self._tray.showMessage(
                "SayInk", self._models_dir_notice, QSystemTrayIcon.MessageIcon.Warning, 10000
            )
        notice = getattr(self._history, "startup_notice", "")
        if not isinstance(notice, str) or not notice:
            return
        log.warning("历史记录状态: %s", notice)
        self._tray.showMessage(
            "SayInk", notice, QSystemTrayIcon.MessageIcon.Warning, 10000
        )

    def _on_history_write_failed(self, message: str) -> None:
        self._output_pipeline().on_history_write_failed(message)

    def _publish_open_history_session(self) -> None:
        """Keep history from deleting or merging a session that is still being written."""
        session_id = None
        if (
            not self._meeting_active()
            and self._current_session_id
            and (getattr(self._recorder, "is_continuous", False) or self._pending_segment_count() > 0)
        ):
            session_id = self._current_session_id
        main = self._safe_attr("_main")
        panel = getattr(main, "history_panel", None) if main is not None else None
        setter = getattr(panel, "set_open_session", None) if panel is not None else None
        if callable(setter):
            setter(session_id)

    def _enqueue_history_record(
        self,
        record: SegmentRecord | None,
        *,
        target_app: str = "",
    ) -> None:
        self._output_pipeline().enqueue_history_record(record, target_app=target_app)

    def _enqueue_history_cleanup(self) -> None:
        self._history.enqueue_cleanup(
            retention_days=int(self._config.get("history.retention_days", 90)),
            max_entries=int(self._config.get("history.max_entries", 5000)),
            active_session_id=self._current_session_id,
        )

    # ── Meeting ───────────────────────────────────────

    @property
    def meeting(self) -> MeetingController:
        return self._meeting

    def _on_meeting_page_start(self, options: object) -> None:
        if isinstance(options, MeetingOptions):
            self._request_meeting_start(options)

    def _start_meeting_from_tray(self) -> None:
        if self._meeting_active():
            self._show_meeting_window()
            return
        if self._config.get("meeting.show_start_card", True):
            self._show_main_window("meeting")
            main = self._main
            if main is not None:
                main.meeting_panel.show_start_card()
            return
        self._request_meeting_start(MeetingOptions.from_config(self._config))

    # ── aux models: first-meeting prompt ──────────────────────────

    def _request_meeting_start(self, options: MeetingOptions) -> None:
        """User asked for a meeting: offer the helper models once, then start."""
        decision = self._offer_aux_download()
        if decision == "cancel":
            return
        if decision == "download":
            self._download_aux_then_start(options)
            return
        self._start_meeting_or_show_browse(options)

    def _start_meeting_or_show_browse(self, options: MeetingOptions) -> None:
        if not self.start_meeting(options):
            main = self._main
            if main is not None:
                main.meeting_panel.show_meeting("")

    def _offer_aux_download(self) -> str:
        """「download」/「start」/「cancel」. Asks only once per install, and only
        while a helper model is missing (docs/meeting-notes-design.md §4.6)."""
        from sayink.aux_models import missing_aux_models

        if self._config.get("meeting.aux_prompted", False):
            return "start"
        missing = missing_aux_models()
        if not missing:
            return "start"
        decision = self._ask_aux_download(missing)
        if decision != "cancel":
            self._config.set("meeting.aux_prompted", True)
        return decision

    def _ask_aux_download(self, missing: list[dict]) -> str:
        from sayink.aux_models import describe_aux_models

        box = QMessageBox(self._main)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("会议辅助模型")
        box.setText("区分说话人和更稳的断句需要两个小模型，现在下载吗？")
        box.setInformativeText(
            f"{describe_aux_models(missing)}。不下载也能开会，但同一个人可能被分成多位说话人。"
            "之后随时可在 会议 页或 引擎 页下载；此提示只出现一次。"
        )
        download = box.addButton("下载后开始", QMessageBox.ButtonRole.AcceptRole)
        start = box.addButton("直接开始", QMessageBox.ButtonRole.ActionRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(download)
        box.exec()
        clicked = box.clickedButton()
        if clicked is download:
            return "download"
        if clicked is start:
            return "start"
        return "cancel"

    def _download_aux_then_start(self, options: MeetingOptions) -> None:
        from PyQt6.QtWidgets import QProgressDialog

        from sayink.aux_models import AuxModelDownloads

        downloads = AuxModelDownloads(str(self._config.get("stt.download_source", "auto") or "auto"), self)
        dialog = QProgressDialog("正在下载会议辅助模型…", "取消", 0, 100, self._main)
        dialog.setWindowTitle("会议辅助模型")
        dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.setValue(0)
        downloads.progress.connect(dialog.setValue)
        dialog.canceled.connect(downloads.cancel)

        def finished(ok: bool, message: str) -> None:
            dialog.close()
            dialog.deleteLater()
            downloads.deleteLater()
            if ok:
                log.info("会议辅助模型已就绪，开始会议")
                self._start_meeting_or_show_browse(options)
                return
            if message == "已取消":
                self._tray.showMessage("SayInk", "已取消下载，会议未开始。", QSystemTrayIcon.MessageIcon.Information, 4000)
                return
            if confirm(
                self._main, "下载失败",
                f"{message}\n\n仍以粗略的说话人区分开始会议吗？（可稍后在 引擎 页重试下载）",
                accept="仍然开始", cancel="返回", default_accept=True,
            ):
                self._start_meeting_or_show_browse(options)

        downloads.finished.connect(finished)
        if not downloads.start():
            # Someone downloaded them meanwhile: nothing to wait for.
            dialog.close()
            dialog.deleteLater()
            downloads.deleteLater()
            self._start_meeting_or_show_browse(options)
            return
        dialog.show()

    def _meeting_refusal(self, options: MeetingOptions) -> str:
        """Why a meeting cannot start right now ("" when it can)."""
        if self._recovery_busy:
            return "正在恢复录音，请完成后再开始会议"

        if self._meeting_active():
            return "已有会议在进行中"
        if self._history.disabled:
            # Every transcript row goes through the history DB; without it
            # the meeting would run with a blank transcript and save nothing.
            return HISTORY_DISABLED_MEETING_REFUSAL
        if self._recorder.is_recording and not self._recorder.is_continuous:
            return "正在进行单次转写，请松开快捷键后再开始会议"
        if (
            not self._is_continuous_mode()
            and not self._hold_paste_sent
            and (
                self._is_transcribing
                or bool(self._segment_queue)
                or self._output_busy
                or bool((self._live_committed or "").strip())
            )
        ):
            return "上一句还在处理，请等它完成后再开始会议"
        configured = options.model_id or str(self._config.get("stt.model_id", DEFAULT_MODEL_ID))
        if not configured or not self._is_model_downloaded(configured):
            return "语音模型未下载：请在 设置 → 引擎 下载后再开始会议"
        return ""

    def start_meeting(self, options: MeetingOptions) -> bool:
        """Switch the audio pipeline to a meeting; False when refused."""
        refusal = self._meeting_refusal(options)
        if refusal:
            log.warning("无法开始会议：%s", refusal)
            self._floating.show_error(refusal)
            self._tray.showMessage("SayInk", refusal, QSystemTrayIcon.MessageIcon.Warning, 6000)
            return False
        if self._continuous_session_active():
            # One microphone, one owner: the dictation session yields.
            self._continuous_user_stopped = True
            self._recorder.stop_continuous()
            self._enqueue_history_cleanup()
        dictation_model = str(self._config.get("stt.model_id", DEFAULT_MODEL_ID) or DEFAULT_MODEL_ID)
        meeting_settings = RecognitionSettings.from_config(
            self._config, meeting=True, input_source=options.input_source,
        )
        caps = resolve_capabilities(self._config, options, dictation_model_id=dictation_model)
        # A file on disk can still fail to load. Publish a live meeting only
        # after resolving the actual recorder gate.
        self._recorder.configure(
            input_source=options.input_source,
            mic_device_index=int(self._config.get("audio.mic_device_index", -1)),
            system_device_index=int(self._config.get("audio.system_device_index", -1)),
        )
        self._recorder.set_quiet_speech(meeting_settings.quiet_speech_mode)
        self._recorder.set_segmenter_profile(
            vad=options.vad,
            vad_model_path=self._aux_model_path(AUX_MODEL_SILERO_VAD) if options.vad == VAD_SILERO else None,
            meeting=True,
        )
        actual = self._recorder.segmentation_status
        if isinstance(actual, SegmentationStatus):
            caps = replace(caps, vad=actual.effective, vad_error=actual.error)
        record = self._meeting.begin(options, caps)
        caps = self._meeting.capabilities or caps
        log.info(
            "开始会议 %s（来源 %s，模型 %s，VAD %s，热词 %d 字，说话人模型 %s，多人发言检测 %s）",
            record.id[:8], options.input_source, caps.model_id, caps.vad,
            len(caps.hotwords), "是" if caps.speaker_model_available else "否",
            _activity_state(self._meeting.embedder, caps.speaker_activity_enabled),
        )
        # Session identity for every segment captured from here on.
        self._current_session_id = record.id
        self._current_seq = 0
        self._speakers = self._meeting.speakers or SpeakerSession()
        self._continuous_user_stopped = False
        self._keep_unsaved_continuous_text = False
        self._meeting_capture_pending = False
        self._clear_live_transcript()

        self._recognizer.configure(
            caps.model_id,
            int(self._config.get("stt.num_threads", 4)),
            hotwords=caps.hotwords,
            language=caps.language,
        )
        self._recognizer.quiet_speech = (
            meeting_settings.quiet_speech_mode if caps.vad == VAD_SILERO else False
        )
        self._tray.set_meeting_in_progress(True)
        self._tray.set_activity_tooltip("meeting")
        self._sound.play_start()
        self._floating.set_meeting_source(input_source_label(options.input_source))
        self._floating.show_meeting(self._meeting.elapsed_text())
        for note in caps.notes:
            log.info("会议提示：%s", note)
        self._start_meeting_capture()
        self._sync_recognition_runtime_status()
        self._publish_open_history_session()
        return True

    def _start_meeting_capture(self) -> None:
        """Open the device; segments queue while the model is still loading."""
        if self._recovery_busy:
            return
        if not self._meeting_active() or self._meeting_user_paused or self._recorder.is_continuous:
            return
        self._set_boundary_context_for_model(self._meeting.capabilities.model_id)
        self._recorder.start_continuous()
        if not self._recorder.is_continuous:
            self._meeting.pause("音频设备打开失败 · 请检查设备后在会议页「继续采集」")
            self._floating.show_meeting_paused(self._meeting.paused_detail)
            self._tray.showMessage(
                "SayInk", "会议采集无法开启，请检查音频设备设置。",
                QSystemTrayIcon.MessageIcon.Warning, 6000,
            )
            return
        self._meeting.resume()
        self._tray.set_activity_tooltip("meeting")
        self._floating.show_meeting(self._meeting.elapsed_text())

    def pause_meeting_capture(self) -> None:
        if not self._meeting_active() or self._meeting.state is not MeetingState.LIVE:
            return
        self._meeting_user_paused = True
        self._meeting_capture_pending = False
        self._meeting.pause("你已暂停采集；暂停期间不采集新音频，已录内容继续识别。")
        self._recorder.stop_continuous()
        self._floating.show_meeting_paused(self._meeting.paused_detail)
        self._tray.set_activity_tooltip("meeting_paused")

    def resume_meeting_capture(self) -> None:
        """User-driven resume after a device error / backlog pause."""
        if not self._meeting_active() or self._meeting.state is not MeetingState.PAUSED:
            return
        self._meeting_user_paused = False
        self._meeting_capture_pending = False
        self._start_meeting_capture()

    def _resume_meeting_capture_if_pending(self) -> None:
        if self._meeting_user_paused:
            return
        if not self._meeting_capture_pending or not self._meeting_active():
            return
        if self._meeting.state is not MeetingState.PAUSED:
            self._meeting_capture_pending = False
            return
        if not self._recognizer.is_ready or self._segment_queue:
            return
        self._meeting_capture_pending = False
        log.info("积压处理完毕，会议采集继续")
        self._start_meeting_capture()

    def _end_meeting(self) -> None:
        if not self._meeting_active():
            return
        if self._meeting.state is MeetingState.DRAINING:
            return
        log.info("结束会议，等待剩余 %d 段识别完成", self._pending_segment_count())
        self._meeting_capture_pending = False
        if self._recorder.is_continuous or self._recorder.is_recording:
            self._recorder.stop_continuous()
        if not self._recognizer.is_ready and not self._recognizer.is_loading:
            # No model will ever come for these; do not wait on them forever.
            self._drop_unrecognized_meeting_audio("模型不可用")
        self._sound.play_stop()
        self._tray.set_activity_tooltip("recognizing" if self._pending_segment_count() else None)
        self._refresh_meeting_bar()
        self._meeting.request_end()

    def _on_meeting_state_changed(self, state: MeetingState) -> None:
        timer = getattr(self, "_meeting_idle_timer", None)
        if state is MeetingState.LIVE and timer is not None:
            self._meeting_idle_warned = False
            timer.start()
        elif timer is not None:
            timer.stop()
        if state is MeetingState.DRAINING:
            self._refresh_meeting_bar()
        elif state is MeetingState.ENDED:
            self._on_meeting_ended()

    def _on_meeting_ended(self) -> None:
        self._meeting_user_paused = False
        meeting_id = self._meeting.current_id
        log.info("会议 %s 已结束", meeting_id[:8])
        self._tray.set_meeting_in_progress(False)
        self._tray.set_activity_tooltip(None)
        dropped = self._unrecognized_meeting_notice
        self._unrecognized_meeting_notice = ""
        self._floating.end_meeting_state(dropped)
        # Hand the pipeline back to dictation (segmenter profile included).
        self._embedder = None
        self._embedder_policy_key = None
        self._reload_coordinator().reset_for_meeting_end()
        self._current_session_id = None
        self._current_seq = 0
        self._speakers = self._new_speaker_session()
        self._publish_open_history_session()
        self._sync_recognition_runtime_status()
        self._sync_hotkey_trigger_mode()
        main = self._main
        if main is not None and main.isVisible():
            main.meeting_panel.show_meeting(meeting_id)
        else:
            job = self._meeting.queued_generation(meeting_id)
            host = self._meeting.remote_host_needing_confirmation() if job is not None and job.automatic else ""
            if dropped:
                # The generic "meeting ended" balloon would replace this, and
                # it is the only notice that the last audio was not transcribed.
                self._tray.showMessage(
                    "SayInk", dropped, QSystemTrayIcon.MessageIcon.Warning, 8000,
                )
            elif not host:
                # An unconfirmed host is refused as soon as the queue runs,
                # and that failure already tells the user. Don't claim the
                # minutes have started.
                queued = job is not None and not job.failed
                self._tray.showMessage(
                    "SayInk",
                    "会议已结束，纪要正在后台生成，完成后会通知你。" if queued else "会议已结束，可到「会议」页生成纪要。",
                    QSystemTrayIcon.MessageIcon.Information, 5000,
                )

    def _on_meeting_notice(self, title: str, detail: str) -> None:
        if not self._meeting_active():
            return
        self._floating.show_info(title, detail)
        self._later(BAR_REFRESH_AFTER_STOP_MS, self._refresh_meeting_bar)

    def _on_minutes_generated(self, meeting_id: str) -> None:
        main = self._main
        if main is None or not main.isVisible():
            from sayink.minutes_prompts import get_template

            notes = self._meeting.store.get_notes(meeting_id)
            message = "原文已精整，可在会议页查看。" if notes is not None and get_template(notes.template_id).is_refinement else "会议纪要已生成。"
            self._tray.showMessage(
                "SayInk", message, QSystemTrayIcon.MessageIcon.Information, 5000
            )

    def _on_minutes_failed(self, meeting_id: str, message: str) -> None:
        log.warning("纪要生成失败: %s", message)
        main = self._main
        if main is None or not main.isVisible():
            job = self._meeting.queued_generation(meeting_id)
            text = (f"纪要自动生成失败：{message.rstrip('。')}。可到「会议」页重试。" if job is not None and job.automatic
                    else f"会议内容处理失败：{message}")
            self._tray.showMessage("SayInk", text, QSystemTrayIcon.MessageIcon.Warning, 8000)

    # ── Settings ──────────────────────────────────────

    def _runtime_status(self) -> RuntimeStatus:
        return self._reload_coordinator().runtime_status()

    def _runtime_status_label(self) -> str:
        return self._runtime_status().label

    def _active_model_display_name(self) -> str:
        return self._reload_coordinator().active_model_display_name()

    def _dictation_model_id(self) -> str:
        return self._reload_coordinator().dictation_model_id()

    def _sync_tray_status_summary(self, status: RuntimeStatus | None = None) -> None:
        status = status or self._runtime_status()
        if status.state is RuntimeState.READY:
            summary = f"{status.label} · {self._active_model_display_name()}"
        else:
            summary = status.label
        self._tray.set_status_summary(summary)

    def _sync_settings_runtime_status(self, status: RuntimeStatus | None = None) -> None:
        status = status or self._runtime_status()
        self._sync_tray_status_summary(status)
        settings = self._settings_widget()
        if settings is None:
            return
        settings.set_runtime_status(status.state, status.label)
        self._sync_recognition_runtime_status()

    def _on_segmentation_changed(self, _status: SegmentationStatus) -> None:
        self._sync_recognition_runtime_status()

    def _sync_recognition_runtime_status(self) -> None:
        settings = self._settings_widget()
        status = self._recorder.segmentation_status
        if settings is None or not isinstance(status, SegmentationStatus):
            return
        coord = self._reload_coordinator()
        speaker = coord.speaker_runtime_status_text(
            active_meeting=self._meeting_or_none(),
            dictation_embedder=self._embedder,
        )
        settings.set_recognition_runtime_status(
            status, speaker_status=speaker, pending=coord.recognition_change_pending,
        )

    def _show_main_window(self, page: str | None = None):
        if self._safe_attr("_exit_started", False):
            return
        if self._main is None:
            try:
                self._main = MainWindow(self._config, self._history, meeting=self._meeting_or_none())
            except Exception:
                log.exception("主窗口创建失败")
                self._tray.showMessage(
                    "SayInk",
                    "主窗口打开失败，错误已写入日志 sayink.log。转写不受影响，可稍后重试。",
                    QSystemTrayIcon.MessageIcon.Warning,
                    10000,
                )
                return
            meeting_page = getattr(self._main, "meeting_panel", None)
            if meeting_page is not None and hasattr(meeting_page, "start_requested"):
                meeting_page.start_requested.connect(self._on_meeting_page_start)
                meeting_page.end_requested.connect(self._end_meeting)
                meeting_page.pause_requested.connect(self.pause_meeting_capture)
                meeting_page.resume_requested.connect(self.resume_meeting_capture)
                meeting_page.aux_models_changed.connect(self._on_models_changed)
            settings = self._main.settings_panel
            settings.set_pending_segment_counter(self._discardable_segment_count)
            settings.set_recording_busy_checker(self._recording_blocks_mic_test)
            settings.hotkey_updated.connect(self._on_hotkey_updated)
            settings.settings_changed.connect(self._on_settings_changed)
            settings.audio_options_changed.connect(self._apply_audio_options)
            settings.audio_recovery_requested.connect(self._show_audio_recovery)
            settings.usage_hint_changed.connect(
                lambda: self._floating.set_esc_stops_continuous(
                    self._config.get("audio.esc_stops_continuous", True)
                )
            )
            settings.auto_start_changed.connect(self._on_auto_start_toggled)
            settings.sound_enabled_changed.connect(self._on_sound_enabled_changed)
            settings.restore_clipboard_changed.connect(self._on_restore_clipboard_changed)
            settings.models_changed.connect(self._on_models_changed)
            settings.recognition_changed.connect(self._on_recognition_settings_changed)
            settings.theme_changed.connect(self._on_theme_changed)
            settings.listen_bar_prefs_changed.connect(self._on_listen_bar_prefs_changed)
            settings.finished.connect(self._on_settings_closed)
            settings.hotkey_capture_started.connect(self._hotkey_mgr.pause)
            settings.hotkey_capture_ended.connect(self._hotkey_mgr.resume)
            settings.update_check_requested.connect(
                lambda: self._updates.check(interactive=True)
            )
            settings.update_install_requested.connect(self._updates.install)
            self._main.installEventFilter(self)
        if page:
            self._main.show_page(page)
        # Show before the form refresh. The window is already themed from
        # construction; applying it again walked every settings widget.
        self._main.show()
        self._main.raise_()
        self._main.activateWindow()
        if page == "history" or (not page and self._main.current_page() == "history"):
            self._main.history_panel.refresh()
        panel = self._main.settings_panel
        reload = getattr(panel, "reload_settings", None)
        if callable(reload):
            try:
                reload(refresh_devices=False, heavy=False)
            except TypeError:
                reload()
        self._sync_settings_runtime_status()
        self._updates.sync_status()

    def _note_main_before_tray_menu(self) -> None:
        main = self._main
        active = QApplication.activeWindow()
        self._restore_main_after_menu = bool(
            main is not None and main.isVisible() and active is main
        )

    def _restore_main_after_tray_menu(self) -> None:
        if not self._restore_main_after_menu:
            return
        self._restore_main_after_menu = False
        self._later(0, self._reactivate_main_window)

    def _reactivate_main_window(self) -> None:
        main = self._main
        if main is None or not main.isVisible():
            return
        main.raise_()
        main.activateWindow()
        app = QApplication.instance()
        if app is not None and app.overrideCursor() is not None:
            app.restoreOverrideCursor()

    def show_main_window(self) -> None:
        self._show_main_window(None)

    def _show_settings(self):
        self._show_main_window(None)

    def _show_history_window(self):
        self._show_main_window("history")

    def _show_meeting_window(self):
        self._show_main_window("meeting")

    def eventFilter(self, obj, event):
        if obj is getattr(self, "_main", None) and event.type() == QEvent.Type.Hide:
            self._on_settings_closed()
        return super().eventFilter(obj, event)

    def _on_settings_closed(self):
        settings = self._settings_widget()
        if settings is not None:
            settings.cancel_hotkey_capture()
        self._hotkey_mgr.resume()
        self._update_tray_models()

    def _on_hotkey_updated(self, new_hotkey: str):
        self._hotkey_mgr.update_hotkey(new_hotkey)

    def _recognition_diff(
        self,
        old: RecognitionSettings,
        new: RecognitionSettings,
        *,
        models_changed: bool = False,
    ) -> tuple[bool, bool, bool]:
        return self._reload_coordinator().recognition_diff(old, new, models_changed=models_changed)

    def _on_models_changed(self):
        """Keep current speech intact; apply the latest settings when idle."""
        self._reload_coordinator().on_models_changed()

    def _apply_pending_recognition_settings(self) -> None:
        self._reload_coordinator().apply_pending_recognition_settings()

    def _apply_deferred_audio_settings(self) -> None:
        self._reload_coordinator().apply_deferred_audio_settings()

    def _on_recognition_settings_changed(self):
        """Glossary / language / VAD / speaker settings changed: apply only the
        affected subsystem so glossary edits do not rebuild VAD or CAM++."""
        self._reload_coordinator().on_recognition_settings_changed()

    def _on_theme_changed(self, mode: str):
        """Apply appearance without reconfiguring STT / audio."""
        self.apply_appearance_theme(mode)

    def _apply_listen_bar_preferences(self) -> None:
        floating = getattr(self, "_floating", None)
        config = getattr(self, "_config", None)
        if floating is None or config is None:
            return
        floating.set_placement_preference(
            config.get("appearance.listen_bar_placement", "auto")
        )
        saved = config.get("appearance.listen_bar_position", {})
        floating.set_saved_positions(saved if isinstance(saved, dict) else {})
        floating.set_autohide(bool(config.get("appearance.listen_bar_autohide", True)))

    def _on_listen_bar_prefs_changed(self) -> None:
        self._apply_listen_bar_preferences()
        floating = getattr(self, "_floating", None)
        reposition = getattr(floating, "reposition_for_preference", None)
        if callable(reposition):
            reposition()

    def _on_listen_bar_position_committed(self, payload: dict) -> None:
        if not isinstance(payload, dict):
            return
        screen = str(payload.get("screen") or "")
        if not screen:
            return
        try:
            entry = {
                "rel_x": float(payload["rel_x"]),
                "rel_y": float(payload["rel_y"]),
                "anchor": payload.get("anchor") if payload.get("anchor") in ("top", "bottom") else "top",
            }
        except (KeyError, TypeError, ValueError):
            return
        current = self._config.get("appearance.listen_bar_position", {})
        positions = dict(current) if isinstance(current, dict) else {}
        positions[screen] = entry
        self._config.set("appearance.listen_bar_position", positions)
        self._config.set("appearance.listen_bar_placement", "last")
        self._floating.set_saved_positions(positions)
        self._floating.set_placement_preference("last")
        main = getattr(self, "_main", None)
        panel = getattr(main, "settings_panel", None) if main is not None else None
        if panel is not None and hasattr(panel, "set_listen_bar_placement"):
            panel.set_listen_bar_placement("last")

    def _on_listen_bar_placement_reset(self) -> None:
        self._config.set("appearance.listen_bar_placement", "auto")
        self._floating.set_placement_preference("auto")
        main = getattr(self, "_main", None)
        panel = getattr(main, "settings_panel", None) if main is not None else None
        if panel is not None and hasattr(panel, "set_listen_bar_placement"):
            panel.set_listen_bar_placement("auto")

    def apply_appearance_theme(self, mode: str | None = None) -> None:
        from PyQt6.QtWidgets import QApplication
        from sayink.ui.theme import apply_theme

        config = self._safe_attr("_config")
        theme_mode = "dark"
        if mode is not None:
            theme_mode = mode
        elif config is not None:
            theme_mode = config.get("appearance.theme_mode", "dark")

        surfaces = [
            surface
            for attr in ("_main", "_floating", "_tray")
            if (surface := self._safe_attr(attr)) is not None
        ]
        main = self._safe_attr("_main")
        if main is not None:
            for child_attr in ("_settings", "_history", "_meeting"):
                child = getattr(main, child_attr, None)
                if child is not None and callable(getattr(child, "reapply_theme", None)):
                    surfaces.append(child)
        apply_theme(QApplication.instance(), mode=theme_mode, surfaces=surfaces)

    def _on_settings_changed(self):
        self._reload_coordinator().on_settings_changed()

    def _apply_immediate_preferences(self) -> None:
        self._reload_coordinator().apply_immediate_preferences()

    def _commit_audio_settings(self, *, clear_queue: bool) -> None:
        self._reload_coordinator().commit_audio_settings(clear_queue=clear_queue)

    def _has_active_transcribe_workers(self) -> bool:
        transcribe_workers = getattr(self._recognizer, "running_transcribe_workers", None)
        if isinstance(transcribe_workers, (list, tuple)):
            return any(w.isRunning() for w in transcribe_workers)
        if getattr(self._recognizer, "is_silent_loading", False) is True:
            return False
        return any(w.isRunning() for w in getattr(self._recognizer, "running_workers", ()))

    def _show_audio_recovery(self):
        silent_unready = (
            getattr(self._recognizer, "is_silent_loading", False) is True
            and not self._recognizer.is_ready
        )
        if (self._recorder.is_recording or self._hold_open_pending or self._pipeline_busy() or self._segment_queue or self._meeting_active()
                or getattr(self._recorder, "storage_busy", False) is True
                or self._recognizer.is_loading or silent_unready or self._has_active_transcribe_workers()):
            self._tray.showMessage("SayInk", "请先结束当前录音，并等待已录内容处理完成。",
                                   QSystemTrayIcon.MessageIcon.Information, 5000)
            return
        from sayink.ui.audio_recovery_dialog import AudioRecoveryDialog
        # The modal dialog temporarily owns the same resident native decoder.
        # Hotkeys cannot start a second decode while recovery is running.
        self._recovery_busy = True
        try:
            dialog = AudioRecoveryDialog(
                self._config.config_dir / "audio-recovery",
                recognizer=self._recognizer._recognizer if self._recognizer.is_ready else None,
                vad=self._applied_recognition_settings.vad,
                vad_path=self._aux_model_path(AUX_MODEL_SILERO_VAD),
                quiet=self._applied_recognition_settings.quiet_speech_enabled,
                glossary=self._applied_recognition_settings.glossary,
                decode_lock=getattr(self._recognizer, "_decode_lock", None),
                recognizer_getter=lambda: (
                    self._recognizer._recognizer if self._recognizer.is_ready else None
                ),
                parent=self._settings_widget(),
            )
            self._recovery_dialog = dialog
            dialog.exec()
        finally:
            self._recovery_dialog = None
            self._recovery_busy = False
            if not self._exit_started:
                self._reload_coordinator().resume_after_recovery()

    def _on_tray_model_switch(self, model_id: str):
        self._reload_coordinator().on_tray_model_switch(model_id)

    def _update_tray_models(self):
        self._reload_coordinator().update_tray_models()

    def _on_auto_start_toggled(self, enabled: bool):
        self._config.set("auto_start", enabled)
        self._tray.set_auto_start(enabled)
        # The tray toggle and the settings row are the same switch; keep the
        # visible row in step instead of waiting for the next window show.
        settings = self._settings_widget()
        if settings is not None and hasattr(settings, "set_auto_start"):
            settings.set_auto_start(enabled)
        self._setup_auto_start(enabled)

    def _on_sound_enabled_changed(self, enabled: bool):
        self._config.set("sound_enabled", enabled)
        self._sound.enabled = enabled

    def _on_restore_clipboard_changed(self, enabled: bool):
        self._paster.restore_clipboard = enabled

    def _setup_auto_start(self, enabled: bool):
        if sys.platform != "win32":
            return
        try:
            import winreg
            key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
            app_name = "SayInk"

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
                if enabled:
                    winreg.SetValueEx(
                        key, app_name, 0, winreg.REG_SZ, auto_start_command()
                    )
                else:
                    try:
                        winreg.DeleteValue(key, app_name)
                    except FileNotFoundError:
                        pass
        except Exception as e:
            log.warning("设置开机自启失败: %s", e)

    # ── Lifecycle ─────────────────────────────────────

    def _quit(self):
        if self._exit_started or not self._confirm_exit():
            return
        self._finish_quit()

    def _unsaved_meeting_draft(self) -> str | None:
        panel = getattr(self._main, "meeting_panel", None) if self._main is not None else None
        report = getattr(panel, "unsaved_exit_draft", None)
        if not callable(report):
            return None
        draft = report()
        # Only a real leftover string blocks exit. A missing panel must not.
        return draft if isinstance(draft, str) and draft else None

    def _confirm_exit(self, *, install_update: bool = False) -> bool:
        """Never silently discard captured or in-flight speech on exit."""
        if self._exit_dialog_open:
            return False
        draft = self._unsaved_meeting_draft()
        if draft:
            self._exit_dialog_open = True
            try:
                box = QMessageBox(self._main)
                box.setWindowTitle("退出 SayInk")
                box.setIcon(QMessageBox.Icon.Warning)
                box.setText("有修改尚未保存")
                box.setInformativeText("纪要或会中笔记没能写入。可以返回继续编辑，或放弃这次修改并退出。")
                back = box.addButton("返回编辑", QMessageBox.ButtonRole.RejectRole)
                discard = box.addButton("放弃修改并退出", QMessageBox.ButtonRole.DestructiveRole)
                box.setDefaultButton(back)
                box.setEscapeButton(back)
                box.exec()
                if box.clickedButton() != discard:
                    return False
            finally:
                self._exit_dialog_open = False
        meeting_live = self._meeting_active()
        pending = (
            meeting_live
            or self._recorder.is_recording
            or self._recorder.is_continuous
            or self._pipeline_busy()
            or self._segment_queue
        )
        if not pending:
            return True
        self._exit_dialog_open = True
        try:
            box = QMessageBox(self._main)
            box.setWindowTitle("安装更新" if install_update else "退出 SayInk")
            box.setIcon(QMessageBox.Icon.Question)
            if meeting_live:
                box.setText("会议记录仍在进行")
                box.setInformativeText(
                    "已识别的文字稿已保存；立即退出会丢弃尚未识别的语音，"
                    "这场会议将标记为「已中断」，之后仍可生成纪要。\n"
                    "建议返回应用，先结束会议再退出。"
                )
            else:
                box.setText("仍在录音或有转写尚未完成")
                box.setInformativeText(
                    "立即退出会丢弃尚未识别、润色或输出的内容。\n"
                    "可以返回应用，结束监听并等待处理完成后再退出。"
                )
            back = box.addButton("返回继续处理", QMessageBox.ButtonRole.RejectRole)
            discard = box.addButton("放弃并退出", QMessageBox.ButtonRole.DestructiveRole)
            box.setDefaultButton(back)
            box.setEscapeButton(back)
            box.exec()
            return box.clickedButton() == discard
        finally:
            self._exit_dialog_open = False

    def set_instance_lock_releaser(self, callback: Callable[[], None] | None) -> None:
        self._on_release_instance_lock = callback

    def _release_instance_lock_once(self) -> None:
        if self._safe_attr("_instance_lock_released", False):
            return
        self._instance_lock_released = True
        releaser = self._safe_attr("_on_release_instance_lock")
        if callable(releaser):
            try:
                releaser()
            except Exception:
                log.exception("提前释放单实例锁失败")

    def _finish_quit(self):
        """Release resources after the user has accepted any pending loss."""
        if self._exit_started:
            return
        self._exit_started = True
        self._quit_drain_deadline = None
        from sayink.ui.theme import unwatch_system_color_scheme

        unwatch_system_color_scheme()
        recovery = getattr(self, "_recovery_dialog", None)
        if recovery is not None:
            recovery.cancel_processing()
        self._clear_queued_audio()
        self._output_stage = OutputStage.IDLE
        log.info("SayInk 正在退出...")
        for timer in self.findChildren(QTimer):
            timer.stop()
        self._stop_continuous_listening()
        panel = getattr(self._main, "meeting_panel", None) if self._main is not None else None
        flush_notes = getattr(panel, "flush_pending_user_notes", None)
        if callable(flush_notes):
            flush_notes()
        self._meeting.shutdown(wait_ms=0)
        self._hotkey_mgr.stop()
        self._hold_open_pending = False
        if self._recorder.is_recording:
            self._recorder.cancel()

        settings = self._settings_widget()
        if settings is not None:
            settings.cancel_all_downloads()
        if self._main is not None:
            cancel_meeting_dl = getattr(self._main.meeting_panel, "cancel_downloads", None)
            if callable(cancel_meeting_dl):
                cancel_meeting_dl()
            self._main.hide()

        if self._main is not None:
            self._main.history_panel.flush_pending_delete()
        self._recognizer.shutdown(wait_ms=0)
        self._polisher.cancel()
        # Paste and clipboard-restore timers belong to TextPaster, not this
        # window, so stopping child QTimers does not drop a queued Ctrl+V.
        self._paster.cancel()
        self._updates.shutdown()
        self._tray.hide()
        # Queued history writes and debounced config edits finish immediately,
        # before any external installer (taskkill /F) or native worker drain.
        self._history.close()
        self._config.save_immediate()
        self._complete_quit_when_idle()

    def _complete_quit_when_idle(self):
        if getattr(self._recorder, "storage_busy", False) is True:
            self._later(QUIT_POLL_MS, self._complete_quit_when_idle)
            return
        save_timer = getattr(self._config, "_save_timer", None)
        if getattr(save_timer, "isActive", lambda: False)() is True:
            self._config.save_immediate()
        self._release_instance_lock_once()
        owners = [self._recognizer, self._polisher, self._meeting, self._updates, self._settings_widget()]
        owners.append(getattr(self, "_recovery_dialog", None))
        if self._main is not None:
            owners.append(self._main.meeting_panel)
        # Native model loading cannot safely be force-terminated. Keep the Qt
        # event loop and each owner's worker references alive until it exits,
        # up to a bounded drain deadline so a wedged C++ call cannot linger forever.
        pending = [worker for owner in owners if owner is not None
                   for worker in getattr(owner, "running_workers", ())
                   if isinstance(worker, QThread) and worker.isRunning()]
        if pending:
            now = time.monotonic()
            timeout_s = float(
                self._safe_attr("_quit_drain_timeout_s", QUIT_NATIVE_DRAIN_TIMEOUT_S)
            )
            deadline = self._safe_attr("_quit_drain_deadline")
            if deadline is None:
                deadline = now + max(0.0, timeout_s)
                self._quit_drain_deadline = deadline
            if now < deadline:
                self._later(QUIT_POLL_MS, self._complete_quit_when_idle)
                return
            log.warning(
                "退出等待后台工作线程超时（%.1fs，仍有 %d 个线程运行中），强制结束进程",
                timeout_s,
                len(pending),
            )
            QApplication.quit()
            _force_process_exit(0)
            return
        QApplication.quit()

    def start(self):
        self._hotkey_mgr.start()
        from sayink.audio_recovery import recordings
        interrupted = [item for item in recordings(self._config.config_dir / "audio-recovery")
                       if not item.metadata.get("complete")]
        if interrupted:
            self._tray.showMessage("SayInk", f"发现 {len(interrupted)} 段可恢复录音。请到通用 → 应用与历史 → 查看 / 恢复录音。",
                                   QSystemTrayIcon.MessageIcon.Information, 10000)
        self._show_history_startup_notice()
        self._meeting.mark_interrupted()
        self._meeting.resume_generation_queue()
        self._enqueue_history_cleanup()
        self._later(UPDATE_CHECK_STARTUP_DELAY_MS, self._updates.maybe_auto_check)
        self._onboarding.schedule(self._recognizer.ready)

    def _settings_widget(self):
        if self._main is not None:
            return self._main.settings_panel
        return None

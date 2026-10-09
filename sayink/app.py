import logging
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from uuid import uuid4

import numpy as np
import pyperclip
from PyQt6.QtCore import QEvent, QObject, QTimer, pyqtSignal
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon, QMessageBox

from sayink.config import (
    Config,
    DEFAULT_HOTKEY,
    format_hotkey,
    TRIGGER_MODE_CONTINUOUS,
    TRIGGER_MODE_HOTKEY,
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
)
from sayink.audio_recorder import AudioRecorder
from sayink.audio_utils import TARGET_SAMPLE_RATE
from sayink.speaker_session import SpeakerSession, voice_embedding
from sayink.speech_recognizer import (
    DEFAULT_MODEL_ID,
    SpeechRecognizer,
    set_models_dir,
    normalize_asr_output,
    get_model_info,
    join_segment_texts,
)
from sayink.history_store import HistoryStore, SegmentRecord
from sayink.text_polisher import (
    LLM_MODE_POLISH,
    TextPolisher,
    polish_settings_complete,
)
from sayink.text_paster import PasteResult, TextPaster
from sayink.sound_manager import SoundManager
from sayink.ui.floating_window import FloatingWindow
from sayink.ui.tray_icon import TrayIcon
from sayink.ui.main_window import MainWindow
from sayink.runtime_status import RuntimeStatus, RuntimeState, runtime_status_from_flags
from sayink.update_controller import UpdateController
from sayink.onboarding import OnboardingController

log = logging.getLogger("SayInk")

MIN_AUDIO_SAMPLES = 1600  # 0.1s at 16kHz — ignore recordings shorter than this
SHORT_TAP_TRAY_COOLDOWN_S = 300  # 托盘「按过短」提示最少间隔，避免输入法反复弹窗
OUTPUT_WATCHDOG_MS = 45_000  # 润色超时 15s + 粘贴校验，远小于此值
# Queued speech beyond this pauses continuous listening (≈11 MB of float32);
# captured audio is never dropped, only further capture stops.
MAX_BACKLOG_AUDIO_SECONDS = 180
# Top-level key: Config drops unknown keys nested under defaults like "stt".
LOAD_SECONDS_KEY = "stt_load_seconds"


_THOUSANDS_SEP_RE = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
# Words containing a negation character without negating anything.
_NON_NEGATING_WORDS = (
    "非常", "无论", "不过", "不仅", "不但", "未来", "无线",
    "特别", "区别", "分别", "类别", "级别", "告别", "识别", "性别", "别人", "别的",
)
_NEGATION_CHARS = frozenset("不没别未无非勿莫")
_EN_NEGATION_RE = re.compile(
    r"\b(?:not|no|never|none|cannot)\b|n't\b", re.IGNORECASE | re.ASCII
)
_LATIN_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")
# Chinese numerals: FireRedASR2 writes 两千五百元 / 十月十五日, so the number
# guard has to read them too. A numeral starts with a digit or 十; a lone
# 百/千/万 is a word (千万别, 百分之) and stays text.
_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}
_CN_BIG_UNITS = {"万": 10_000, "亿": 100_000_000}
_CN_NUMERAL_RE = re.compile(r"[一二两三四五六七八九十][零〇一二两三四五六七八九十百千万亿]*")
_REPEATED_NEGATION_RE = re.compile(r"([不没别未无非勿莫])\1+")


def _cn_numeral_to_arabic(numeral: str) -> str:
    """两千五百 → 2500; 一三八零零 → 13800; 三四 → "3 4" (an approximate range)."""
    if all(ch in _CN_DIGITS for ch in numeral):
        digits = [_CN_DIGITS[ch] for ch in numeral]
        if len(digits) == 2 and 0 not in digits and abs(digits[0] - digits[1]) == 1:
            return f"{digits[0]} {digits[1]}"
        return "".join(str(d) for d in digits)
    total = 0
    section = 0
    number = 0
    for ch in numeral:
        if ch in _CN_DIGITS:
            number = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            section += (number or 1) * _CN_UNITS[ch]
            number = 0
        else:
            total = (total + section + number) * _CN_BIG_UNITS[ch]
            section = 0
            number = 0
    if number and len(numeral) > 1:
        # Colloquial 三万五 / 两千五: the trailing digit takes the next unit down.
        before = numeral[-2]
        unit = _CN_UNITS.get(before) or _CN_BIG_UNITS.get(before) or 0
        if unit >= 100:
            number *= unit // 10
    return str(total + section + number)


def _normalize_cn_numerals(text: str) -> str:
    def _replace(match: re.Match) -> str:
        numeral = match.group(0)
        if numeral == "一":
            # A lone 一 is mostly an article or idiom (一段话, 优化一下, 一起);
            # dropping or adding it does not change the meaning.
            return numeral
        return f" {_cn_numeral_to_arabic(numeral)} "

    return _CN_NUMERAL_RE.sub(_replace, text)


def _numbers(text: str) -> set[str]:
    values = set()
    text = _normalize_cn_numerals(_THOUSANDS_SEP_RE.sub("", text))
    for match in _NUMBER_RE.findall(text):
        whole, _, frac = match.partition(".")
        value = (whole.lstrip("0") or "0") + (f".{frac.rstrip('0')}" if frac.rstrip("0") else "")
        values.add(value)
    return values


def _negation_count(text: str) -> int:
    for word in _NON_NEGATING_WORDS:
        text = text.replace(word, "")
    text = _REPEATED_NEGATION_RE.sub(r"\1", text)
    return sum(1 for ch in text if ch in _NEGATION_CHARS) + len(_EN_NEGATION_RE.findall(text))


def _has_negation(text: str) -> bool:
    return _negation_count(text) > 0


def _proper_nouns(text: str) -> set[str]:
    """Latin names worth keeping verbatim: GitHub, iPhone, API, AI, Python,
    GPT4. All-lowercase words are left out so dropped English filler is not
    mistaken for a lost name."""
    names = set()
    for token in _LATIN_TOKEN_RE.findall(text):
        if len(token) >= 2 and any(c.isupper() or c.isdigit() for c in token):
            names.add(token.casefold())
    return names


def _keeps_name(name: str, folded: str) -> bool:
    # Whole word only: API inside "rapid" is not the name. Spacing may change
    # either way (iPhone15 → iPhone 15), so try the text with and without it.
    pattern = re.compile(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])")
    return any(pattern.search(t) for t in (folded, re.sub(r"\s+", "", folded)))


def polish_rejection_reason(raw: str, polished: str) -> str:
    """Why a polish reply must not replace the raw text; empty when it may.

    Only clear-cut changes are caught: converting 一百 → 100 or rewording
    Chinese names passes, so this is a guard, not a fidelity proof.
    """
    raw = unicodedata.normalize("NFKC", raw or "").strip()
    polished = unicodedata.normalize("NFKC", polished or "").strip()
    if not raw:
        return ""
    if len(polished) > len(raw) * 2 + 40:
        return "长度异常"
    raw_numbers = _numbers(raw)
    polished_numbers = _numbers(polished)
    # Keep every original number, including zero. An added zero can be
    # formatting (3点 → 3:00); other introduced numbers change the content.
    if raw_numbers and (
        raw_numbers - polished_numbers
        or (polished_numbers - raw_numbers) - {"0"}
    ):
        return "数字被改动"
    # Count, not presence: 我不同意，也没准备好 → 我不同意，也准备好了 drops one.
    if _negation_count(polished) < _negation_count(raw):
        return "否定词丢失"
    folded = polished.casefold()
    if any(not _keeps_name(name, folded) for name in _proper_nouns(raw)):
        return "英文专名丢失"
    return ""


def polish_looks_plausible(raw: str, polished: str) -> bool:
    """Reject replies that answer or change the text instead of lightly editing it."""
    return not polish_rejection_reason(raw, polished)


def auto_start_command() -> str:
    """Command line for the Run registry key.

    A frozen build is its own executable; ``sys.executable`` is absolute there
    (``sys.argv[0]`` may be relative to wherever the shortcut started us). From
    a source checkout the interpreter alone would just open Python, so pass
    ``run.py`` as well.
    """
    import os

    exe = os.path.abspath(sys.executable)
    if getattr(sys, "frozen", False) or hasattr(sys, "_MEIPASS"):
        return f'"{exe}"'
    run_py = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "run.py")
    return f'"{exe}" "{run_py}"'

@dataclass
class _PendingHistoryRecord:
    session_id: str
    seq: int
    raw_text: str
    polished_text: str
    source: str
    duration_ms: int
    trigger_mode: str
    model: str
    speaker_id: int = 0
    speaker_route: str = ""


@dataclass
class _SegmentContext:
    """Continuous-session identity fixed when a segment is captured, not when it is recognized."""

    session_id: str
    seq: int
    source: str
    speakers: SpeakerSession


@dataclass
class _QueuedSegment:
    """One captured utterance waiting for the recognizer.

    Everything that belongs to the audio travels with it, so the queue cannot
    fall out of step with a parallel list of routes or contexts.
    """

    audio: np.ndarray
    route: str = ""
    context: _SegmentContext | None = None


class App(QObject):
    """Central orchestrator that connects all modules."""

    _history_committed = pyqtSignal()
    _history_failed = pyqtSignal(str)

    # 友好化错误信息映射
    ERROR_HINTS = {
        "已断开": "音频设备已断开\n请重新连接设备，或在设置 → 通用 → 声音收录 中刷新并选择其他设备",
        "麦克风": "无法访问麦克风\n请检查：1) 麦克风是否已连接\n2) 是否被其他应用占用\n3) 系统隐私设置",
        "模型未就绪": "语音模型未就绪\n请打开 SayInk（双击托盘图标）→ 引擎 → 下载模型",
        "模型未下载": "语音模型未下载\n请打开 SayInk（双击托盘图标）→ 引擎 → 下载模型",
        "录音过短": "录音过短\n请按住快捷键说话，时长至少 0.1 秒",
        "未识别": "未识别到语音内容\n请确保音频来源与设备正确，并靠近麦克风或播放电脑声音",
        "音频设备": "无法打开音频设备\n请在设置 → 通用 → 声音收录 中刷新并选择设备",
        "系统声音": "无法采集系统声音\nWindows 可启用立体声混音或安装 PyAudioWPatch；混合模式需配置电脑播放设备",
        "润色失败": "润色失败，已输出原文\n请检查 API 配置是否正确",
        "输出失败": "输出失败\n请确保目标窗口可接收文本输入",
    }

    def _friendly_error(self, original_msg: str) -> str:
        """将技术性错误信息转换为友好提示"""
        for keyword, hint in self.ERROR_HINTS.items():
            if keyword in original_msg:
                return hint
        return original_msg

    def __init__(self, config: Config | None = None):
        super().__init__()

        self._config = config if config is not None else Config()
        self._current_transcription = ""
        self._live_committed = ""
        self._live_inflight = ""
        self._hold_paste_sent = False
        self._hold_duration_ms = 0
        self._is_transcribing = False
        # One segment at a time from ASR through paste: the pending history
        # record and the polisher each hold a single in-flight segment.
        self._output_busy = False
        self._output_token = 0
        # Raw text of the segment currently being output. A new hold recording
        # may start meanwhile and reset _current_transcription.
        self._output_raw_text = ""
        # "polish" while the polisher owns the sentence, "paste" once it was
        # handed to the paster; the watchdog decides from this what to salvage.
        self._output_stage = ""
        self._output_stage_text = ""
        self._segment_queue: list[_QueuedSegment] = []
        self._speakers = SpeakerSession()
        self._continuous_user_stopped = False
        self._current_session_id: str | None = None
        self._current_seq = 0
        self._pending_record: _PendingHistoryRecord | None = None
        self._load_started_at: float | None = None
        self._loading_model_id = ""
        self._short_tap_tray_last_at = 0.0
        self._hotkey_conflict_warned = False
        self._exit_dialog_open = False
        self._exit_started = False

        log.info("正在初始化各模块...")
        set_models_dir(self._config.models_dir)
        self._init_modules()
        log.info("正在初始化界面...")
        self._init_ui()
        self._updates = UpdateController(
            self._config,
            parent=self,
            status_sink=self._settings_widget,
            tray=self._tray,
            show_about=lambda: self._show_main_window("about"),
            confirm_exit=lambda: self._confirm_exit(install_update=True),
            finish_quit=self._finish_quit,
            exit_started=lambda: self._exit_started,
        )
        self._onboarding = OnboardingController(
            self._config,
            parent=self,
            dialog_parent=lambda: self._main,
            is_continuous_mode=self._is_continuous_mode,
            show_main_window=self._show_main_window,
        )
        self._connect_signals()
        self._sync_hotkey_trigger_mode()
        self._configure_stt()
        self._update_tray_models()

    def _init_modules(self):
        self._hotkey_mgr = HotKeyManager(
            self._config.get("hotkey", DEFAULT_HOTKEY)
        )
        self._recorder = AudioRecorder()
        self._apply_audio_config()
        self._recognizer = SpeechRecognizer()
        self._polisher = TextPolisher()
        self._paster = TextPaster(
            restore_clipboard=self._config.get("output.restore_clipboard", False)
        )
        self._sound = SoundManager(
            enabled=self._config.get("sound_enabled", True)
        )
        self._history = HistoryStore(db_path=self._config.config_dir / "history.db")

    def _init_ui(self):
        self._floating = FloatingWindow()
        self._tray = TrayIcon()
        self._main: MainWindow | None = None
        self._restore_main_after_menu = False

        self._tray.set_auto_start(self._config.get("auto_start", False))
        self._tray.show()
        self._floating.set_input_source(
            self._config.get("audio.input_source", "microphone")
        )
        # Re-apply after surfaces exist so cold-start dark/system is not stuck
        # on import-time light snapshots for inline-styled widgets.
        self.apply_appearance_theme()
        from sayink.ui.theme import watch_system_color_scheme

        watch_system_color_scheme(self._on_system_color_scheme_changed)

    def _on_system_color_scheme_changed(self) -> None:
        if self._config.get("appearance.theme_mode", "dark") == "system":
            log.info("系统浅/深色外观已变化，重新换肤")
            self.apply_appearance_theme()

    def _is_continuous_mode(self) -> bool:
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
        self._history_failure_notified = False
        self._hotkey_mgr.listener_status.connect(self._on_hotkey_listener_status)

        self._recorder.volume_changed.connect(self._floating.update_volume)
        self._recorder.recording_finished.connect(self._on_recording_finished)
        self._recorder.segment_ready.connect(self._on_segment_ready)
        self._recorder.error.connect(self._on_recorder_error)
        self._recorder.warning.connect(self._on_recorder_warning)
        self._recorder.no_speech_warning.connect(self._on_no_speech_warning)

        self._recognizer.final_result.connect(self._on_final_result)
        self._recognizer.partial_result.connect(self._on_partial_result)
        self._recognizer.error.connect(self._on_recognizer_error)
        self._recognizer.ready.connect(self._on_stt_ready)
        self._recognizer.model_load_progress.connect(self._on_model_load_progress)

        self._polisher.polish_complete.connect(self._on_polish_complete)
        self._polisher.polish_error.connect(self._on_polish_error)

        self._floating.continuous_stop_requested.connect(self._stop_continuous_user_session)
        self._floating.settings_requested.connect(self._show_settings)
        self._floating.history_requested.connect(self._show_history_window)

        self._tray.open_settings.connect(self._show_settings)
        self._tray.wake_island.connect(self._show_main_window)
        self._tray.history_requested.connect(self._show_history_window)
        self._tray.quit_app.connect(self._quit)
        self._tray.auto_start_toggled.connect(self._on_auto_start_toggled)
        self._tray.model_switched.connect(self._on_tray_model_switch)
        self._tray.menu_about_to_show.connect(self._note_main_before_tray_menu)
        self._tray.menu_closed.connect(self._restore_main_after_tray_menu)
        self._tray.check_update_requested.connect(self._updates.check_from_tray)
        self._tray.messageClicked.connect(self._updates.on_tray_message_clicked)

    def _apply_audio_config(self):
        from sayink.audio_devices import (
            INPUT_SOURCES,
            build_recording_plan,
            plan_includes_system_capture,
            sanitize_system_device_index,
        )

        source = self._config.get("audio.input_source", "microphone")
        if source not in INPUT_SOURCES:
            source = "microphone"
            self._config.set("audio.input_source", source)
        sys_idx = sanitize_system_device_index(
            int(self._config.get("audio.system_device_index", -1))
        )
        if sys_idx != int(self._config.get("audio.system_device_index", -1)):
            self._config.set("audio.system_device_index", sys_idx)
        self._recorder.configure(
            input_source=source,
            mic_device_index=int(self._config.get("audio.mic_device_index", -1)),
            system_device_index=sys_idx,
        )
        floating = getattr(self, "_floating", None)
        if floating is not None:
            setter = getattr(floating, "set_input_source", None)
            if callable(setter):
                setter(source)
        try:
            plan = build_recording_plan(
                source,
                int(self._config.get("audio.mic_device_index", -1)),
                sys_idx,
            )
            needs_system = source in ("system", "mixed")
            if needs_system and not plan_includes_system_capture(plan):
                log.warning(
                    "当前来源需要电脑播放声道但未配置成功；看视频/开会远端可能无法转写。"
                )
        except RuntimeError as e:
            log.warning("音频计划不可用: %s", e)

    def _configure_stt(self):
        from sayink.speech_recognizer import is_model_downloaded, resolve_startup_model_id

        configured = self._config.get("stt.model_id", DEFAULT_MODEL_ID)
        model_id = resolve_startup_model_id(configured)
        num_threads = self._config.get("stt.num_threads", 4)

        if model_id and is_model_downloaded(model_id):
            info = get_model_info(model_id)
            name = info["name"] if info else model_id
            needs_load = not (
                self._recognizer.current_model_id == model_id
                and self._recognizer.is_ready
            )
            if needs_load and not self._recognizer.is_loading:
                self._floating.show_model_loading(
                    f"正在将 {name} 载入内存，请稍候（{self._load_eta_text(model_id)}）…"
                )
                self._tray.set_activity_tooltip("loading")
                self._sync_settings_runtime_status()
            self._recognizer.configure(model_id, num_threads)
        else:
            info = get_model_info(model_id) if model_id else None
            name = info["name"] if info else (model_id or DEFAULT_MODEL_ID)
            log.warning("语音模型 %s 未下载，请在设置中下载模型", name)
            hint = (
                f"请下载语音模型「{name}」。"
                "Windows 可双击托盘打开主窗口，再到设置 → 引擎；或右键托盘 → 打开 SayInk。"
            )
            self._floating.show_error(hint)
            self._tray.showMessage(
                "SayInk",
                hint,
                QSystemTrayIcon.MessageIcon.Warning,
                6000,
            )

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
        in_flight = self._is_transcribing or self._output_busy
        return len(self._segment_queue) + (1 if in_flight else 0)

    def _pipeline_busy(self) -> bool:
        return self._is_transcribing or self._output_busy

    def _on_esc_pressed(self):
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
        seconds = (self._config.get(LOAD_SECONDS_KEY, {}) or {}).get(model_id)
        if isinstance(seconds, (int, float)) and seconds > 0:
            return f"上次用时约 {max(1, round(seconds))} 秒"
        return "约 10–40 秒"

    def _remember_load_duration(self) -> None:
        started = self._load_started_at
        model_id = self._loading_model_id
        self._load_started_at = None
        if started is None or not model_id:
            return
        durations = dict(self._config.get(LOAD_SECONDS_KEY, {}) or {})
        durations[model_id] = round(time.monotonic() - started, 1)
        self._config.set(LOAD_SECONDS_KEY, durations)

    def _on_model_load_progress(self, msg: str):
        if "就绪" in msg:
            self._remember_load_duration()
            self._floating.clear_model_loading_lock()
            self._sync_settings_runtime_status()
            return
        if "失败" in msg:
            self._floating.clear_model_loading_lock()
            self._tray.set_activity_tooltip(None)
            self._floating.show_error(self._friendly_error(msg))
            self._sync_settings_runtime_status(
                RuntimeStatus(RuntimeState.UNAVAILABLE, "模型载入失败")
            )
            return
        if self._recorder.is_continuous:
            log.info("模型重新加载，暂停持续监听")
            self._stop_continuous_listening()
        model_id = self._recognizer.current_model_id
        if isinstance(model_id, str) and model_id:
            self._load_started_at = time.monotonic()
            self._loading_model_id = model_id
            msg = f"{msg}（{self._load_eta_text(model_id)}）"
        self._floating.show_model_loading(msg)
        self._tray.set_activity_tooltip("loading")
        self._sync_settings_runtime_status()

    def _on_no_speech_warning(self):
        log.warning("持续监听长时间未检测到有效语音")
        self._floating.show_warning(
            "似乎没采集到声音",
            "请检查麦克风设备与系统隐私权限",
        )
        self._tray.showMessage(
            "SayInk",
            "持续监听中长时间未检测到语音，请检查麦克风与设备设置。",
            QSystemTrayIcon.MessageIcon.Warning,
            6000,
        )

    def _on_recording_start(self):
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

        log.info("开始录音（来源: %s）...", self._recorder.input_source_display)
        self._current_transcription = ""
        self._hold_paste_sent = False
        self._hold_duration_ms = 0
        self._clear_live_transcript()
        self._sound.play_start()
        self._tray.set_recording(True)
        self._tray.set_activity_tooltip("recording")
        self._floating.show_recording()
        self._recorder.start(continuous=False)

    def _on_recording_stop(self):
        if self._is_continuous_mode():
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
        if self._is_continuous_mode():
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
        # (_on_recording_start refuses), so everything here is this hold's.
        self._clear_queued_audio()
        self._clear_live_transcript()
        self._reset_recording_ui_after_abort()
        self._recorder.cancel()
        self._floating.show_cancelled()

    def _on_continuous_hotkey_start(self):
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
        self._current_session_id = None
        self._current_seq = 0
        # Segments still queued from the previous session keep its speaker numbering.
        self._speakers = SpeakerSession()
        self._clear_live_transcript()
        self._start_continuous_listening()

    def _on_hotkey_tap_too_short(self):
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
        if not self._is_continuous_mode() or not self._recorder.is_continuous:
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
        if not pending:
            # Re-checks state: a session restarted within the delay keeps its bar.
            QTimer.singleShot(1200, self._refresh_continuous_ui_after_output)

    def _on_recorder_error(self, error_msg: str):
        if self._recorder.is_continuous:
            self._stop_continuous_listening()
        self._tray.set_recording(False)
        self._tray.set_activity_tooltip(None)
        self._sound.play_error()
        self._floating.show_error(self._friendly_error(error_msg))

    def _on_recorder_warning(self, warning_msg: str):
        log.warning("%s", warning_msg)
        self._floating.show_warning("音频采集受限", warning_msg)
        self._tray.showMessage(
            "SayInk",
            warning_msg,
            QSystemTrayIcon.MessageIcon.Warning,
            10000,
        )

    def _start_continuous_listening(self):
        if not self._is_continuous_mode():
            return
        if not self._recognizer.is_ready:
            return
        if self._recorder.is_continuous:
            return
        log.info("开启自动持续转写（来源: %s）", self._recorder.input_source_display)
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
            self._floating.show_listening()
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

        QTimer.singleShot(50, _begin)

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
        segment = _QueuedSegment(audio=audio, route=route, context=context)
        if front:
            self._segment_queue.insert(0, segment)
            return
        self._segment_queue.append(segment)
        self._pause_if_backlog_too_long()

    def _pop_queued_audio(self) -> tuple[np.ndarray, str, _SegmentContext | None]:
        segment = self._segment_queue.pop(0)
        return segment.audio, segment.route, segment.context

    def _clear_queued_audio(self) -> None:
        self._segment_queue.clear()

    def _queued_audio(self) -> list[np.ndarray]:
        """Audio arrays waiting in the queue, oldest first (tests and logging)."""
        return [segment.audio for segment in self._segment_queue]

    def _queued_audio_seconds(self) -> float:
        return sum(int(segment.audio.size) for segment in self._segment_queue) / TARGET_SAMPLE_RATE

    def _pause_if_backlog_too_long(self) -> None:
        if not self._continuous_session_active():
            return
        if self._queued_audio_seconds() <= MAX_BACKLOG_AUDIO_SECONDS:
            return
        log.warning(
            "识别积压 %d 段（约 %.0f 秒），暂停持续监听",
            len(self._segment_queue), self._queued_audio_seconds(),
        )
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
        self._current_seq += 1
        return context

    def _on_segment_ready(self, audio: np.ndarray):
        route = self._segment_route_from_recorder()
        if audio.size < MIN_AUDIO_SAMPLES:
            return
        context = self._capture_segment_context()
        if not self._recognizer.is_ready:
            self._hold_audio_until_ready(audio, route, front=False, context=context)
            return
        if self._pipeline_busy() or self._segment_queue:
            self._enqueue_audio(audio, route, context=context)
            log.debug("转写排队，队列长度 %d", len(self._segment_queue))
            self._schedule_queue_pump()
            return
        self._begin_transcription(audio, route=route, context=context)

    def _schedule_queue_pump(self) -> None:
        # Older segments may be waiting out the post-paste pause; joining the
        # queue instead of starting right away keeps them in spoken order.
        if not self._pipeline_busy():
            QTimer.singleShot(300, self._pump_segment_queue)

    # ── Recognition ───────────────────────────────────

    def _on_recording_finished(self, full_audio: np.ndarray):
        if full_audio.size < MIN_AUDIO_SAMPLES:
            if self._emit_hold_output_if_ready():
                return
            if (
                not self._is_continuous_mode()
                and (self._is_transcribing or self._segment_queue or self._live_committed.strip())
            ):
                return
            log.warning("录音过短 (%d 采样点)，忽略", full_audio.size)
            self._reset_recording_ui_after_abort()
            self._floating.show_error(self._friendly_error("录音过短"))
            return
        if self._pipeline_busy() or self._segment_queue:
            self._enqueue_audio(full_audio)
            self._schedule_queue_pump()
            return
        self._begin_transcription(full_audio)

    def _begin_transcription(
        self,
        audio: np.ndarray,
        route: str = "",
        context: _SegmentContext | None = None,
    ):
        if not self._recognizer.is_ready:
            self._hold_audio_until_ready(audio, route, front=True, context=context)
            return
        if context is None:
            context = self._capture_segment_context()
        self._pending_record = self._build_pending_history_record(audio, context)
        speaker_id, speaker_route = self._label_speaker(audio, route, context)
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
        self._recognizer.transcribe_final(audio)

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
        if context is not None:
            trigger_mode = TRIGGER_MODE_CONTINUOUS
            session_id, seq, source = context.session_id, context.seq, context.source
        else:
            trigger_mode = TRIGGER_MODE_HOTKEY
            session_id, seq, source = uuid4().hex, 0, self._history_source()

        return _PendingHistoryRecord(
            session_id=session_id,
            seq=seq,
            raw_text="",
            polished_text="",
            source=source,
            duration_ms=int(len(audio) / TARGET_SAMPLE_RATE * 1000),
            trigger_mode=trigger_mode,
            model=self._config.get("stt.model_id", DEFAULT_MODEL_ID),
        )

    def _clear_live_transcript(self) -> None:
        self._live_committed = ""
        self._live_inflight = ""
        self._floating.clear_live_transcript()

    def _show_live_transcript(self) -> None:
        parts = [self._live_committed, self._live_inflight]
        text = join_segment_texts(parts)
        if text:
            self._floating.show_live_transcript(text)

    def _on_partial_result(self, text: str) -> None:
        piece = (text or "").strip()
        if not piece:
            return
        self._live_inflight = piece
        self._show_live_transcript()

    def _commit_live_transcript(self, text: str) -> None:
        piece = (text or "").strip()
        if not piece:
            return
        if self._live_committed:
            self._live_committed = join_segment_texts([self._live_committed, piece])
        else:
            self._live_committed = piece
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
        self._current_transcription = text
        if self._pending_record is not None:
            self._pending_record.raw_text = text
            if self._hold_duration_ms:
                self._pending_record.duration_ms = self._hold_duration_ms
        self._deliver_recognized_text(text)
        return True

    def _on_final_result(self, text: str):
        text = normalize_asr_output(text)
        if self._hold_paste_sent and not self._is_continuous_mode():
            self._is_transcribing = False
            self._pump_segment_queue()
            return
        if text.strip():
            log.debug("识别结果长度: %d 字符", len(text))
            self._current_transcription = text
            self._commit_live_transcript(text)
            if self._pending_record is not None:
                self._pending_record.raw_text = text
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

    def _mark_output_busy(self) -> None:
        self._output_busy = True
        self._output_token += 1
        token = self._output_token
        QTimer.singleShot(OUTPUT_WATCHDOG_MS, lambda: self._release_stuck_output(token))

    def _release_stuck_output(self, token: int) -> None:
        if not self._output_busy or token != self._output_token:
            return
        raw = self._output_raw_text
        if self._output_stage == "polish" and raw.strip():
            # The polisher never answered. Its result would have been this
            # sentence anyway, so output the raw words instead of losing them.
            log.error("润色超时未回调，改为输出原文")
            self._polisher.cancel()
            self._output_stage = "paste"
            self._output_text(raw, degraded_from_polish=True)
            return
        log.error("输出流程超时未回调，释放队列以免后续语音卡住")
        self._output_busy = False
        self._output_stage = ""
        if self._output_stage_text.strip():
            # Ctrl+V may or may not have gone out; leave the words on the
            # clipboard rather than risk pasting them twice.
            try:
                pyperclip.copy(self._output_stage_text)
            except Exception as exc:
                log.warning("复制超时句子到剪贴板失败: %s", exc)
            else:
                self._floating.show_info("输出超时 · 已复制", "可按 Ctrl+V 粘贴")
        self._pump_segment_queue()

    def _deliver_recognized_text(self, text: str) -> None:
        self._mark_output_busy()
        self._output_raw_text = text
        self._output_stage = "polish"
        self._output_stage_text = ""
        llm_enabled = self._config.get("llm.enabled", False)
        api_url = self._config.get("llm.api_url", "")
        api_key = self._config.get("llm.api_key", "")
        model_name = self._config.get("llm.model_name", "")
        prompt = self._config.get("llm.prompt", "")
        mode = (self._config.get("llm.mode", LLM_MODE_POLISH) or LLM_MODE_POLISH).strip().lower()

        if (
            llm_enabled
            and mode == LLM_MODE_POLISH
            and polish_settings_complete(api_url, api_key, model_name)
        ):
            self._tray.set_activity_tooltip("polishing")
            if self._continuous_user_stopped:
                self._floating.show_continuous_stopped("正在处理剩余内容 · 正在润色")
            else:
                self._floating.show_polishing(text)
            self._polisher.polish(
                text,
                api_url,
                api_key,
                model_name,
                prompt,
                mode=LLM_MODE_POLISH,
            )
        elif llm_enabled and mode == LLM_MODE_POLISH:
            log.warning("文字润色已开启但配置不完整，直接输出原文")
            self._output_text(text, degraded_from_polish=True)
        else:
            self._output_text(text)

    def _on_recognizer_error(self, error_msg: str):
        self._is_transcribing = False
        if self._recognizer.is_loading:
            return
        if "加载失败" in error_msg:
            # Load progress already showed this failure. Pump must not drop audio.
            self._pump_segment_queue()
            return
        if not self._is_continuous_mode():
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
        if self._pipeline_busy() or not self._segment_queue:
            if self._continuous_session_active() and not self._pipeline_busy():
                self._floating.show_listening()
            return
        if not self._recognizer.is_ready:
            return
        next_audio, next_route, next_context = self._pop_queued_audio()
        self._begin_transcription(next_audio, route=next_route, context=next_context)

    def _on_stt_ready(self):
        self._floating.clear_model_loading_lock()
        self._tray.set_activity_tooltip(None)
        self._sync_settings_runtime_status()
        if self._segment_queue and not self._is_transcribing:
            log.info("模型就绪，处理排队语音 %d 段", len(self._segment_queue))
            self._pump_segment_queue()
        hotkey = self._continuous_hotkey_label()
        if self._is_continuous_mode():
            # Loading HUD stays visible until replaced/dismissed; continuous mode
            # no longer shows idle float, so dismiss explicitly after ready.
            self._floating.dismiss_if_idle()
            stop = (
                "按 Esc 或听写条「结束」停止整场。"
                if self._config.get("audio.esc_stops_continuous", True)
                else "用听写条「结束」停止整场。"
            )
            tray_msg = (
                f"持续转写已就绪。按住 {hotkey} 开始监听，说话停顿约 1 秒后自动输入，不用点结束；"
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

    # ── Polishing ─────────────────────────────────────

    def _on_polish_complete(self, polished_text: str):
        if self._output_stage != "polish":
            log.warning("润色结果晚于超时到达，已输出原文，忽略")
            return
        raw = self._output_raw_text
        reason = polish_rejection_reason(raw, polished_text) if raw else ""
        if reason:
            log.warning(
                "润色结果未通过保真检查（%s；原文 %d 字，结果 %d 字），改为输出原文",
                reason, len(raw), len(polished_text),
            )
            self._output_text(raw, degraded_from_polish=True)
            return
        self._output_text(polished_text)

    def _on_polish_error(self, error_msg: str):
        if self._output_stage != "polish":
            return
        log.warning("后处理失败，降级输出原文: %s", error_msg)
        self._output_text(
            self._output_raw_text,
            degraded_from_polish=True,
        )

    # ── Output ────────────────────────────────────────

    def _output_text(self, text: str, *, degraded_from_polish: bool = False):
        self._is_transcribing = False
        text = normalize_asr_output(text)
        if not text.strip():
            self._output_busy = False
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
            return

        record = self._freeze_pending_history_record(text, degraded_from_polish)
        if self._config.history_only_output():
            # Meeting / playback capture: the words go to history, never to
            # whatever window happens to be in front.
            saved = bool(self._config.get("history.enabled", True))
            log.info("只记录不粘贴：%d 字%s", len(text), "" if saved else "（历史已关闭，未保存）")
            self._handle_paste_result(
                PasteResult("recorded", detail="" if saved else "history_disabled"),
                degraded_from_polish=degraded_from_polish,
                record=record,
            )
            return
        self._output_stage = "paste"
        self._output_stage_text = text
        self._paster.paste_async(text, lambda result, record=record: self._handle_paste_result(
            result,
            degraded_from_polish=degraded_from_polish,
            record=record,
        ))

    def _freeze_pending_history_record(
        self,
        output_text: str,
        degraded_from_polish: bool,
    ) -> SegmentRecord | None:
        pending = self._pending_record
        if pending is None:
            return None
        raw_text = pending.raw_text or output_text
        if degraded_from_polish:
            polished_text = raw_text
        elif pending.raw_text and output_text != pending.raw_text:
            polished_text = output_text
        else:
            polished_text = pending.polished_text
        self._pending_record = None
        return SegmentRecord(
            session_id=pending.session_id,
            seq=pending.seq,
            created_at=int(time.time() * 1000),
            raw_text=raw_text,
            polished_text=polished_text,
            source=pending.source,
            duration_ms=pending.duration_ms,
            target_app="",
            trigger_mode=pending.trigger_mode,
            model=pending.model,
            speaker_id=int(pending.speaker_id),
            speaker_route=pending.speaker_route or "",
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
        return context.speakers.assign(voice_embedding(audio), stored_route), stored_route

    def _handle_paste_result(
        self,
        result: PasteResult | str,
        *,
        degraded_from_polish: bool = False,
        record: SegmentRecord | None = None,
    ):
        self._output_busy = False
        self._output_stage = ""
        self._output_stage_text = ""
        if isinstance(result, PasteResult):
            status = result.status
            target_app = result.target_app
            error_detail = result.detail
        else:
            # Compatibility for tests and third-party integrations using the old callback.
            status = result.split(":", 1)[0]
            target_app = ""
            error_detail = result.partition(":")[2]
        paste_hint = "可按 Cmd+V 粘贴" if sys.platform == "darwin" else "可按 Ctrl+V 粘贴"
        success_msg = "已发送（原文）" if degraded_from_polish else "已发送"
        target_hint = f"发送到 {target_app}" if target_app else "请确认目标应用已接收"

        if status in {"sent", "pasted"}:
            log.info("已向目标窗口发送粘贴快捷键%s", f": {target_app}" if target_app else "")
            self._tray.set_activity_tooltip(
                "listening" if self._continuous_session_active() else None
            )
            if self._continuous_session_active():
                if degraded_from_polish:
                    self._floating.show_info(success_msg, target_hint)
                else:
                    self._floating.show_success(success_msg, target_hint)
                QTimer.singleShot(1700, self._refresh_continuous_ui_after_output)
            elif self._continuous_user_stopped:
                self._floating.show_continuous_stopped(
                    f"剩余内容：{success_msg} · {target_hint}"
                )
                QTimer.singleShot(2200, self._refresh_continuous_ui_after_output)
            elif self._recorder.is_recording:
                self._floating.show_recording()
            else:
                if degraded_from_polish:
                    self._floating.show_info(success_msg, target_hint)
                else:
                    self._floating.show_success(success_msg, target_hint)
        elif status == "recorded":
            saved = error_detail != "history_disabled"
            title = ("已记录" if saved else "未保存 · 历史已关闭") + ("（原文）" if degraded_from_polish else "")
            hint = "" if saved else "当前为「只记录到历史」，但历史已关闭；请在设置中打开历史或改回粘贴"
            self._tray.set_activity_tooltip(
                "listening" if self._continuous_session_active() else None
            )
            if self._continuous_session_active():
                if saved and not degraded_from_polish:
                    self._floating.show_success(title, hint)
                else:
                    self._floating.show_info(title, hint)
                QTimer.singleShot(1700, self._refresh_continuous_ui_after_output)
            elif self._continuous_user_stopped:
                self._floating.show_continuous_stopped(f"剩余内容：{title}")
                QTimer.singleShot(2200, self._refresh_continuous_ui_after_output)
            elif self._recorder.is_recording:
                self._floating.show_recording()
            elif saved and not degraded_from_polish:
                self._floating.show_success(title, hint)
            else:
                self._floating.show_info(title, hint)
        elif status == "unverified":
            # Ctrl+V already went out, then the foreground changed. Saying
            # 「已复制」 here invites a second manual paste of the same words.
            log.info("已发送粘贴快捷键，但焦点随后切换，无法确认是否已插入")
            self._tray.set_activity_tooltip(
                "listening" if self._continuous_session_active() else None
            )
            unverified_hint = f"焦点已切换，若 {target_app} 未出字{paste_hint}" if target_app else f"焦点已切换，若未出字{paste_hint}"
            if self._continuous_session_active():
                self._floating.show_info("已发送 · 未确认", unverified_hint)
                QTimer.singleShot(2200, self._refresh_continuous_ui_after_output)
            elif self._continuous_user_stopped:
                self._floating.show_continuous_stopped(f"剩余内容：已发送 · 未确认 · {unverified_hint}")
                QTimer.singleShot(2200, self._refresh_continuous_ui_after_output)
            elif self._recorder.is_recording:
                self._floating.show_recording()
            else:
                self._floating.show_info("已发送 · 未确认", unverified_hint)
        elif status == "clipboard":
            log.info("已复制到剪贴板（粘贴未确认成功）")
            self._tray.set_activity_tooltip(
                "listening" if self._continuous_session_active() else None
            )
            if self._continuous_session_active():
                self._floating.show_success("已复制", paste_hint)
                QTimer.singleShot(2200, self._refresh_continuous_ui_after_output)
            elif self._continuous_user_stopped:
                self._floating.show_continuous_stopped(f"剩余内容：已复制 · {paste_hint}")
                QTimer.singleShot(2200, self._refresh_continuous_ui_after_output)
            elif self._recorder.is_recording:
                self._floating.show_recording()
            else:
                self._floating.show_success("已复制到剪贴板", paste_hint)
        else:
            log.error("输出失败: %s", error_detail)
            self._tray.set_activity_tooltip(
                "listening" if self._continuous_session_active() else None
            )
            self._floating.show_error(self._friendly_error("输出失败"))

        self._enqueue_history_record(record, target_app=target_app)
        QTimer.singleShot(300, self._pump_segment_queue)

    def _refresh_open_history_ui(self) -> None:
        if self._main is None:
            return
        self._main._history.refresh()

    def _show_history_startup_notice(self) -> None:
        """Tell the user once if history.db was rebuilt or could not be opened."""
        notice = getattr(self._history, "startup_notice", "")
        if not isinstance(notice, str) or not notice:
            return
        log.warning("历史记录状态: %s", notice)
        self._tray.showMessage(
            "SayInk", notice, QSystemTrayIcon.MessageIcon.Warning, 10000
        )

    def _on_history_write_failed(self, message: str) -> None:
        """One tray warning per run; the log keeps every failure."""
        if self._history_failure_notified:
            return
        self._history_failure_notified = True
        self._tray.showMessage(
            "SayInk", message, QSystemTrayIcon.MessageIcon.Warning, 8000
        )

    def _enqueue_history_record(
        self,
        record: SegmentRecord | None,
        *,
        target_app: str = "",
    ) -> None:
        if record is None:
            return
        if not self._config.get("history.enabled", True):
            return
        if not (record.raw_text.strip() or record.polished_text.strip()):
            return
        self._history.enqueue(
            SegmentRecord(
                session_id=record.session_id,
                seq=record.seq,
                created_at=record.created_at,
                raw_text=record.raw_text,
                polished_text=record.polished_text,
                source=record.source,
                duration_ms=record.duration_ms,
                target_app=target_app or "",
                trigger_mode=record.trigger_mode,
                model=record.model,
                speaker_id=int(record.speaker_id),
                speaker_route=record.speaker_route or "",
            )
        )

    def _enqueue_history_cleanup(self) -> None:
        self._history.enqueue_cleanup(
            retention_days=int(self._config.get("history.retention_days", 90)),
            max_entries=int(self._config.get("history.max_entries", 5000)),
            active_session_id=self._current_session_id,
        )

    # ── Settings ──────────────────────────────────────

    def _runtime_status(self) -> RuntimeStatus:
        return runtime_status_from_flags(
            is_loading=bool(self._recognizer.is_loading),
            is_ready=bool(self._recognizer.is_ready),
        )

    def _runtime_status_label(self) -> str:
        return self._runtime_status().label

    def _active_model_display_name(self) -> str:
        from sayink.speech_recognizer import MODEL_REGISTRY

        active_id = self._config.get("stt.model_id", "")
        for model in MODEL_REGISTRY:
            if model["id"] == active_id:
                return model["name"]
        return active_id or "未选择模型"

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

    def _show_main_window(self, page: str | None = None):
        if self._main is None:
            self._main = MainWindow(self._config, self._history)
            settings = self._main._settings
            settings._pending_segment_count = self._pending_segment_count
            settings.hotkey_updated.connect(self._on_hotkey_updated)
            settings.settings_changed.connect(self._on_settings_changed)
            settings.auto_start_changed.connect(self._on_auto_start_toggled)
            settings.sound_enabled_changed.connect(self._on_sound_enabled_changed)
            settings.restore_clipboard_changed.connect(self._on_restore_clipboard_changed)
            settings.models_changed.connect(self._on_models_changed)
            settings.theme_changed.connect(self._on_theme_changed)
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
        if page == "history" or (not page and self._main.current_page() == "history"):
            self._main._history.refresh()
        self.apply_appearance_theme()
        self._main._settings.reload_settings()
        self._sync_settings_runtime_status()
        self._main.show()
        self._main.raise_()
        self._main.activateWindow()
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
        QTimer.singleShot(0, self._reactivate_main_window)

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

    def _on_models_changed(self):
        """Reload STT after model download/select without requiring a full settings save."""
        if self._recorder.is_continuous:
            self._stop_continuous_listening()
        self._configure_stt()
        self._update_tray_models()

    def _on_theme_changed(self, mode: str):
        """Apply appearance without reconfiguring STT / audio."""
        self.apply_appearance_theme(mode)

    def apply_appearance_theme(self, mode: str | None = None) -> None:
        from PyQt6.QtWidgets import QApplication
        from sayink.ui.theme import apply_theme

        # Tests may construct App via __new__ without QObject __init__;
        # use object.__getattribute__ to avoid Qt "super-class __init__" errors.
        def _attr(name, default=None):
            try:
                return object.__getattribute__(self, name)
            except (AttributeError, RuntimeError):
                return default

        config = _attr("_config")
        theme_mode = "dark"
        if mode is not None:
            theme_mode = mode
        elif config is not None:
            theme_mode = config.get("appearance.theme_mode", "dark")

        surfaces = [
            surface
            for attr in ("_main", "_floating", "_tray")
            if (surface := _attr(attr)) is not None
        ]
        main = _attr("_main")
        if main is not None:
            for child_attr in ("_settings", "_history"):
                child = getattr(main, child_attr, None)
                if child is not None:
                    surfaces.append(child)
        apply_theme(QApplication.instance(), mode=theme_mode, surfaces=surfaces)

    def _on_settings_changed(self):
        # Drop only what the confirmation counted; the sentence cut off by
        # stopping below is still the user's speech and gets transcribed.
        if self._segment_queue:
            log.warning("设置已保存，丢弃 %d 段待识别语音", len(self._segment_queue))
        self._clear_queued_audio()
        was_continuous = self._recorder.is_continuous
        if was_continuous:
            self._stop_continuous_listening()

        self._continuous_user_stopped = False
        self._sound.enabled = self._config.get("sound_enabled", True)
        self._tray.set_auto_start(self._config.get("auto_start", False))
        self._apply_audio_config()
        set_models_dir(self._config.models_dir)
        self._configure_stt()
        self._update_tray_models()
        self._paster.restore_clipboard = self._config.get(
            "output.restore_clipboard", False
        )
        self._sync_hotkey_trigger_mode()
        self.apply_appearance_theme()

    def _on_tray_model_switch(self, model_id: str):
        current = self._config.get("stt.model_id", "")
        if model_id == current:
            return
        if self._recorder.is_continuous:
            self._stop_continuous_listening()
        self._config.set("stt.model_id", model_id)
        self._configure_stt()
        self._update_tray_models()

    def _update_tray_models(self):
        from sayink.speech_recognizer import MODEL_REGISTRY, is_model_downloaded
        downloaded = [m for m in MODEL_REGISTRY if is_model_downloaded(m["id"])]
        active = self._config.get("stt.model_id", "")
        self._tray.update_models(downloaded, active)
        self._sync_tray_status_summary()

    def _on_auto_start_toggled(self, enabled: bool):
        self._config.set("auto_start", enabled)
        self._tray.set_auto_start(enabled)
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

    def _confirm_exit(self, *, install_update: bool = False) -> bool:
        """Never silently discard captured or in-flight speech on exit."""
        if self._exit_dialog_open:
            return False
        pending = (
            self._recorder.is_recording
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

    def _finish_quit(self):
        """Release resources after the user has accepted any pending loss."""
        if self._exit_started:
            return
        self._exit_started = True
        log.info("SayInk 正在退出...")
        self._stop_continuous_listening()
        self._hotkey_mgr.stop()
        if self._recorder.is_recording:
            self._recorder.cancel()

        settings = self._settings_widget()
        if settings is not None:
            settings.cancel_all_downloads()
        if self._main is not None:
            self._main.hide()

        if self._main is not None:
            self._main._history.flush_pending_delete()
        self._recognizer.shutdown()
        self._polisher.cancel()
        self._tray.hide()
        # Queued history writes finish before the process goes away; the
        # writer is a daemon thread, so this wait is their only chance.
        self._history.close()
        self._config.save_immediate()
        QApplication.quit()

    def start(self):
        self._hotkey_mgr.start()
        self._show_history_startup_notice()
        self._enqueue_history_cleanup()
        QTimer.singleShot(5000, self._updates.maybe_auto_check)
        self._onboarding.schedule(self._recognizer.ready)

    def _settings_widget(self):
        if self._main is not None:
            return getattr(self._main, "_settings", None)
        return None

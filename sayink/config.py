import copy
import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any
from PyQt6.QtCore import QTimer

from sayink.secret_store import default_secret_store
from sayink.speech_recognizer import (
    DEFAULT_MODEL_ID,
    LEGACY_DEFAULT_MODEL_IDS,
    default_models_dir,
)
from sayink.user_data import user_data_dir
from sayink.version import __version__ as VERSION  # noqa: F401  (re-exported for the About page)

log = logging.getLogger("SayInk")

# Bump when adding one-time STT default migrations for existing installs.
STT_MODEL_MIGRATION_VERSION = 2

# Keys left in user configs by older test runs (stripped on load).
_TEST_POLLUTION_KEYS = frozenset({"test_key", "atomic_test"})


def _get_default_models_dir() -> Path:
    return default_models_dir()


def format_hotkey(hotkey: str) -> str:
    if not hotkey:
        return ""
    parts = hotkey.split("+")
    out = []
    for p in parts:
        p = p.strip()
        out.append(p.capitalize() if len(p) > 1 else p.upper())
    return " + ".join(out)

# The hotkey's main key is swallowed while held, so these would stop working.
RESERVED_HOTKEYS = frozenset(
    frozenset(combo.split("+"))
    for combo in (
        "alt+tab", "alt+f4", "alt+esc",
        "ctrl+a", "ctrl+c", "ctrl+v", "ctrl+x", "ctrl+z", "ctrl+y", "ctrl+s",
        "ctrl+esc", "ctrl+tab", "win+l", "win+d", "win+tab", "win+v",
    )
)


def is_reserved_hotkey(hotkey: str) -> bool:
    parts = frozenset(p.strip().lower() for p in (hotkey or "").split("+") if p.strip())
    parts = frozenset("win" if p == "cmd" else p for p in parts)
    return parts in RESERVED_HOTKEYS


TRIGGER_MODE_HOTKEY = "hotkey"
TRIGGER_MODE_CONTINUOUS = "continuous"
# Not a user-selectable trigger mode: the value written to history rows that
# belong to a meeting started from the 「会议」 page / tray.
TRIGGER_MODE_MEETING = "meeting"

DEFAULT_HOTKEY = "alt+x"

# Where a recognized sentence goes: pasted at the cursor (dictation, the
# product's main job) or written to history only (listening to a meeting
# through 「仅电脑播放 / 混合」, where pasting into whatever window is in
# front would be an accident).
OUTPUT_MODE_PASTE = "paste"
OUTPUT_MODE_HISTORY = "history"
OUTPUT_MODES = (OUTPUT_MODE_PASTE, OUTPUT_MODE_HISTORY)
# Input sources for which the app switches to history-only output by itself.
HISTORY_ONLY_INPUT_SOURCES = frozenset({"system", "mixed"})

DEFAULT_CONFIG = {
    "hotkey": DEFAULT_HOTKEY,
    "first_run_welcome_seen": True,
    "auto_start": False,
    "sound_enabled": True,
    "output": {
        "restore_clipboard": False,
        "mode": OUTPUT_MODE_PASTE,
        # True while the mode was picked by the app (input source changed),
        # so returning to the microphone can undo it; a manual choice sticks.
        "mode_auto": False,
        # None preserves legacy behavior: automatic unless history was chosen
        # manually. Explicit choices persist True / False independently.
        "follow_input_source": None,
    },
    "audio": {
        "input_source": "microphone",
        "trigger_mode": TRIGGER_MODE_HOTKEY,
        "mic_device_index": -1,
        "system_device_index": -1,
        "esc_stops_continuous": True,
        "recovery_enabled": False,
        "separate_lanes": False,
    },
    "stt": {
        "model_id": DEFAULT_MODEL_ID,
        "num_threads": 4,
        "models_dir": "",
        "download_source": "auto",
        "migration_version": 0,
        # 识别增强——转写与会议共用（曾是 meeting.* 键，启动时自动迁移）。
        "glossary": [],            # [{"term": "张鑫", "variants": ["张新"]}] → 热词
        "language": "auto",        # 语种锁定；auto → zh（支持 language 的模型）
        "vad": "silero",           # silero / energy；Silero 未下载时自动退回 energy
        "speaker_model_enabled": True,
        "speaker_activity_enabled": False,
        "quiet_speech_enabled": False,
        "boundary_context_enabled": False,
    },
    "llm": {
        "enabled": False,
        "api_url": "",
        "api_key": "",
        "model_name": "",
        "prompt": "",          # custom prompt for 润色 (legacy key)
        "prompt_correct": "",  # custom prompt for 纠错
        "mode": "polish",
    },
    "history": {
        "enabled": True,
        "onboarded": False,
        "retention_days": 90,
        "max_entries": 5000,
    },
    "appearance": {
        "theme_mode": "dark",
        # auto: one-shot near the top, continuous / meeting along the bottom.
        "listen_bar_placement": "auto",
        # screen name → {rel_x, rel_y, anchor}; used when placement is "last".
        "listen_bar_position": {},
        "listen_bar_autohide": True,
    },
    # 会议记录 / 会议纪要（docs/meeting-notes-design.md §4.8）。
    "meeting": {
        "input_source": "mixed",   # 新会议默认麦克风 + 电脑声音；空 = 跟随转写音源
        "model_id": "",            # 空 = 沿用 stt.model_id
        "hotkey": "",              # 空 = 未设置
        "show_start_card": True,
        "polish_segments": False,
        "model_name": "",          # 纪要模型；空 = 沿用 llm.model_name
        "template_id": "general",
        # 会议结束后按默认模板在后台排队生成纪要；会把文字稿发给 AI 服务，故默认关闭。
        "auto_generate": False,
        "custom_templates": [],
        "output_language": "auto",
        "aux_prompted": False,     # 首次开会缺辅助模型时问过一次「现在下载？」
        "confirmed_remote_hosts": [],
        # Per-request transcript cap (characters). 8000 is also the hard
        # ceiling the generator enforces; this can only lower it.
        "single_request_char_limit": 8000,
        "context_window_tokens": 32768,
        "max_output_tokens": 4096,
        "separate_lanes": False,
    },
    "update": {
        "auto_check": True,
        "last_check_at": 0,
    },
}


SECRET_KEY = "llm.api_key"

_BOOL_TRUE = frozenset({"1", "true", "yes", "on"})
_BOOL_FALSE = frozenset({"0", "false", "no", "off"})


def _parse_bool(value):
    """``True`` / ``False``, ``0`` / ``1``, or the words true/false/yes/no/on/off."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        word = value.strip().lower()
        if word in _BOOL_TRUE:
            return True
        if word in _BOOL_FALSE:
            return False
    return None


def _parse_unix_time(value):
    """Whole seconds since the epoch. A fractional ``time.time()`` is kept, truncated.

    Other integer settings must not do this: ``1.5`` in a count is a bad edit
    and still falls through to the default. Only the update timestamp is written
    as a float by this app.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value == value and abs(value) != float("inf"):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text and text.lstrip("+-").replace(".", "", 1).isdigit():
            try:
                number = float(text)
            except ValueError:
                return None
            if number == number and abs(number) != float("inf"):
                return int(number)
    return None


def _parse_int(value):
    """An integer, an integral float, or a base-10 string. Booleans are not numbers."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer() and abs(value) <= 2**53:
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text and text.lstrip("+-").isdigit():
            try:
                return int(text)
            except ValueError:
                return None
    return None


def _coerce_known_value(default_value, value):
    """Fit one saved scalar to the type of its default.

    Returns ``(stored, repaired)``. A value that cannot be read becomes the
    default. Callers must not log ``value``: a misplaced secret could land
    in a string field.
    """
    if isinstance(default_value, bool):
        parsed = _parse_bool(value)
        if parsed is None:
            return default_value, True
        return parsed, parsed is not value
    if isinstance(default_value, int):
        parsed = _parse_int(value)
        if parsed is None:
            return default_value, True
        return parsed, type(parsed) is not type(value) or parsed != value
    if isinstance(default_value, str):
        if isinstance(value, str):
            return value, False
        return default_value, True
    if isinstance(default_value, list):
        if isinstance(value, list):
            return value, False
        return copy.deepcopy(default_value), True
    return value, False
# Keys that moved from the meeting section to stt (shared by dictation).
SHARED_RECOGNITION_KEYS = ("glossary", "language", "vad", "speaker_model_enabled")
# Run-key value name written by VoiceInk ≤ 2.1.0; removed once on first start.
LEGACY_AUTO_START_VALUE = "VoiceInk"


class Config:
    def __init__(
        self,
        config_dir: Path | str | None = None,
        secret_store=None,
        *,
        file_secrets: bool | None = None,
    ):
        explicit_dir = config_dir is not None
        if config_dir is not None:
            self._config_dir = Path(config_dir)
        else:
            self._config_dir = user_data_dir()
        # The real profile (~/.sayink) uses Credential Manager. If that store
        # is missing, the key stays in memory and is never written to
        # config.json. An explicit directory (tests, a portable copy) is its
        # own profile and may keep the key in that directory's config.json.
        if secret_store is None and not explicit_dir:
            secret_store = default_secret_store()
        self._secrets = secret_store
        if file_secrets is None:
            file_secrets = self._secrets is None and explicit_dir
        self._file_secrets = bool(file_secrets) and self._secrets is None
        self._secret_value: str | None = None
        self._secret_persist_failed = False
        self._secret_read_failed = False
        # Unreadable config whose backup also failed. Saving would replace it
        # with defaults and drop hotkeys, glossary and templates.
        self._persist_blocked = False
        self._config_file = self._config_dir / "config.json"
        self._models_dir = _get_default_models_dir()
        self._config: dict = {}
        self._extra_keys: dict = {}
        # 延迟保存机制：避免频繁写入文件
        self._save_timer = QTimer()
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._do_save)
        self._ensure_dirs()
        self._load()
        self._sync_registry_auto_start()

    def _ensure_dirs(self):
        self._config_dir.mkdir(parents=True, exist_ok=True)
        # models_dir may be install dir (already exists) or user dir
        try:
            self._models_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass  # Permission denied (install dir), models already there

    def _load(self):
        config_existed = self._config_file.exists()
        raw: dict = {}
        if config_existed:
            try:
                with open(self._config_file, "r", encoding="utf-8-sig") as f:
                    raw = json.load(f)
                if not isinstance(raw, dict):
                    raise ValueError("顶层不是对象")
            except (json.JSONDecodeError, ValueError, OSError, RecursionError) as e:
                backup = self._backup_unreadable_config()
                if backup is None:
                    self._persist_blocked = True
                log.warning(
                    "配置文件读取失败，使用默认配置（原文件已备份到 %s）: %s", backup, e
                )
                raw = {}
        self._extra_keys = {k: v for k, v in raw.items() if k not in DEFAULT_CONFIG}
        moved = self._migrate_recognition_settings(raw)
        self._type_repairs = False
        self._config = self._merge_defaults(DEFAULT_CONFIG, raw)
        # True = 不写欢迎弹窗（升级用户或未配置时用默认 True）；首次本无配置文件时单独打开欢迎。
        # 必须先于下面的迁移落盘，否则新文件会把欢迎写成已看过。
        if not config_existed:
            self._config["first_run_welcome_seen"] = False
        # Before any migration save, so a plaintext key is lifted into memory
        # and stripped instead of being written back out.
        self._load_secret()
        output_migrated = self._migrate_output_by_mode()
        if (
            not self._persist_blocked
            and (self._strip_test_pollution() or moved or output_migrated or self._type_repairs)
        ):
            self.save_immediate()
        self._migrate_stt_model()

    def _load_secret(self) -> None:
        store = getattr(self, "_secrets", None)
        in_file = str(self._config.get("llm", {}).get("api_key", "") or "")
        if store is None:
            if in_file and not self._file_secrets:
                self._secret_value = in_file
                self._scrub_plaintext_key(
                    "没有可用的凭据存储，配置文件中的 API Key 已移除，仅本次运行有效"
                )
            return
        stored = store.read()
        if stored is None:
            # Credential Manager unreadable: an older plaintext key still works
            # this run, but it may not stay on disk (README P0: never written
            # in plaintext). The settings page tells the user to re-enter it.
            self._secret_value = in_file
            self._secret_read_failed = not in_file
            if not in_file:
                log.warning("凭据管理器不可读，已保存的 API Key 本次无法读取")
            elif store.write(in_file):
                # Reading can fail where writing works (e.g. a missing helper
                # module); then the key is safe and the plaintext copy can go.
                self._config.setdefault("llm", {})["api_key"] = ""
                self.save_immediate()
                log.info("凭据管理器读取失败但写入成功，API Key 已迁移")
            else:
                self._scrub_plaintext_key(
                    "凭据管理器不可读，配置文件中的 API Key 已移除，仅本次运行有效"
                )
            return
        if in_file and store.write(in_file):
            log.info("已将 API Key 从配置文件迁移到 Windows 凭据管理器")
            stored = in_file
            self._config.setdefault("llm", {})["api_key"] = ""
            self.save_immediate()
        elif in_file:
            stored = in_file
            self._scrub_plaintext_key(
                "API Key 未能迁移到凭据管理器，已从配置文件移除，仅本次运行有效"
            )
        self._secret_value = stored

    def _scrub_plaintext_key(self, reason: str) -> None:
        """Drop a legacy plaintext key from config.json; keep it in memory only."""
        self._config.setdefault("llm", {})["api_key"] = ""
        self._secret_persist_failed = True
        self.save_immediate()
        log.warning(reason)

    @property
    def secret_persist_failed(self) -> bool:
        """True when the last API key change is held in memory only."""
        return self._secret_persist_failed

    @property
    def secret_read_failed(self) -> bool:
        """True when a saved API key may exist but could not be read this run."""
        return self._secret_read_failed

    def _backup_unreadable_config(self) -> Path | None:
        """Keep the unreadable file so the next save cannot erase API keys etc."""
        import time

        backup = self._config_dir / f"config.corrupt-{time.strftime('%Y%m%d-%H%M%S')}.json"
        try:
            os.replace(self._config_file, backup)
            return backup
        except OSError as e:
            log.error("无法备份损坏的配置文件: %s", e)
            return None

    def _strip_test_pollution(self) -> bool:
        """Remove keys accidentally written by un-isolated tests."""
        dirty = [k for k in self._extra_keys if k in _TEST_POLLUTION_KEYS]
        for k in dirty:
            del self._extra_keys[k]
        return bool(dirty)

    @staticmethod
    def _migrate_recognition_settings(raw: dict) -> bool:
        """Glossary / language / VAD / speaker model used to be meeting-only
        (``meeting.*``); dictation shares them now under ``stt.*``. Move the
        values once so nobody loses their term list. Returns True on change."""
        meeting = raw.get("meeting")
        if not isinstance(meeting, dict):
            return False
        moved = False
        stt = raw.get("stt")
        if not isinstance(stt, dict):
            stt = {}
        for key in SHARED_RECOGNITION_KEYS:
            if key not in meeting:
                continue
            value = meeting.pop(key)
            if key not in stt:
                stt[key] = value
                log.info("升级迁移：meeting.%s → stt.%s", key, key)
            moved = True
        if moved:
            raw["stt"] = stt
        return moved

    def _migrate_stt_model(self) -> None:
        """One-time upgrade: former app defaults → Fun-ASR-Nano."""
        stt = self._config.setdefault("stt", {})
        if stt.get("migration_version", 0) >= STT_MODEL_MIGRATION_VERSION:
            return
        cur = stt.get("model_id", DEFAULT_MODEL_ID)
        if cur in LEGACY_DEFAULT_MODEL_IDS and cur != DEFAULT_MODEL_ID:
            log.info("升级迁移：语音模型 %s → %s", cur, DEFAULT_MODEL_ID)
            stt["model_id"] = DEFAULT_MODEL_ID
        stt["migration_version"] = STT_MODEL_MIGRATION_VERSION
        self.save_immediate()

    def _sync_registry_auto_start(self):
        """Sync auto_start setting with Windows registry (installer may have set it)."""
        if sys.platform != "win32":
            return
        try:
            import winreg
            key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ)
            registered = False
            try:
                winreg.QueryValueEx(key, "SayInk")
                registered = True
            except FileNotFoundError:
                # Registry entry doesn't exist - nothing to sync
                pass
            winreg.CloseKey(key)
            # VoiceInk ≤ 2.1.0 wrote its own Run value. Its uninstaller does
            # not remove it, so it would keep pointing at a deleted EXE (or
            # start the app twice). Carry the intent over and drop the value.
            if self._drop_legacy_auto_start_value(winreg, key_path):
                if not registered:
                    self._write_auto_start_value(winreg, key_path)
                registered = True
            if registered and not self._config.get("auto_start", False):
                self._config["auto_start"] = True
                self.save_immediate()
                log.info("同步注册表开机自启状态到配置文件")
        except Exception as e:
            log.warning("读取注册表开机自启状态失败: %s", e)

    @staticmethod
    def _write_auto_start_value(winreg, key_path: str) -> None:
        from sayink.app import auto_start_command

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, "SayInk", 0, winreg.REG_SZ, auto_start_command())
        except Exception as e:
            log.warning("写入开机自启项失败: %s", e)

    @staticmethod
    def _drop_legacy_auto_start_value(winreg, key_path: str) -> bool:
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE
            ) as key:
                try:
                    winreg.QueryValueEx(key, LEGACY_AUTO_START_VALUE)
                except FileNotFoundError:
                    return False
                winreg.DeleteValue(key, LEGACY_AUTO_START_VALUE)
                log.info("已移除旧版 VoiceInk 的开机自启项，改由 SayInk 接管")
                return True
        except Exception as e:
            log.warning("清理旧版开机自启项失败: %s", e)
            return False

    def _merge_defaults(self, defaults: dict, current: dict, _path: str = "") -> dict:
        result = {}
        for key, default_value in defaults.items():
            dotted = f"{_path}.{key}" if _path else key
            if key in current:
                if isinstance(default_value, dict):
                    if isinstance(current[key], dict):
                        result[key] = self._merge_defaults(default_value, current[key], dotted)
                    else:
                        # A section written as a scalar (e.g. "history": "on") would
                        # crash later code that calls .get() on it; fall back to defaults.
                        log.warning(
                            "配置节 %s 应为对象，实际为 %s，已恢复为默认值",
                            dotted, type(current[key]).__name__,
                        )
                        result[key] = copy.deepcopy(default_value)
                        self._type_repairs = True
                else:
                    value = current[key]
                    # ``None`` is a real sentinel (output.follow_input_source), not
                    # "missing". Only a boolean may replace it.
                    if default_value is None:
                        if value is None or isinstance(value, bool):
                            result[key] = value
                        else:
                            parsed = _parse_bool(value)
                            log.warning(
                                "配置项 %s 应为布尔或空，实际为 %s，已改为可用的值",
                                dotted, type(value).__name__,
                            )
                            result[key] = parsed
                            self._type_repairs = True
                    elif dotted == "update.last_check_at":
                        parsed = _parse_unix_time(value)
                        if parsed is None:
                            log.warning(
                                "配置项 %s 的类型与默认值不符（实际为 %s），已改为可用的值",
                                dotted, type(value).__name__,
                            )
                            result[key] = default_value
                            self._type_repairs = True
                        else:
                            result[key] = parsed
                            if type(value) is not int:
                                log.warning(
                                    "配置项 %s 的类型与默认值不符（实际为 %s），已改为可用的值",
                                    dotted, type(value).__name__,
                                )
                                self._type_repairs = True
                    else:
                        coerced, repaired = _coerce_known_value(default_value, value)
                        if repaired:
                            log.warning(
                                "配置项 %s 的类型与默认值不符（实际为 %s），已改为可用的值",
                                dotted, type(value).__name__,
                            )
                            self._type_repairs = True
                        result[key] = coerced
            else:
                result[key] = (
                    copy.deepcopy(default_value)
                    if isinstance(default_value, (dict, list))
                    else default_value
                )
        if _path:
            # Keys this build does not know inside a known section (written by
            # a newer release, or hand-added) ride along instead of being
            # erased on the next save. Top-level extras live in _extra_keys.
            for key, value in current.items():
                if key not in defaults:
                    result[key] = copy.deepcopy(value)
        return result

    def _do_save(self):
        """实际执行保存操作"""
        self.save()

    def save_immediate(self):
        """立即保存配置，用于应用退出前。失败时返回 False。"""
        self._save_timer.stop()
        return self.save()

    def save(self):
        """Atomic write: write to temp file then rename to prevent corruption.

        Returns False when the file on disk was left unchanged.
        """
        if self._persist_blocked:
            log.error("配置未能读出且无法备份，已停止写回以免覆盖原文件")
            return False
        llm = self._config.get("llm")
        if isinstance(llm, dict) and llm.get("api_key") and not self._file_secrets:
            leaked = str(llm.get("api_key") or "")
            if leaked and not (self._secret_value or ""):
                self._secret_value = leaked
                self._secret_persist_failed = True
            llm["api_key"] = ""
        data = {**self._config, **self._extra_keys}
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self._config_dir), suffix=".tmp", prefix="config_"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, str(self._config_file))
        except OSError as e:
            log.error("配置文件保存失败: %s", e)
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            return False
        return True

    def get(self, key: str, default: Any = None) -> Any:
        if key == SECRET_KEY and (
            getattr(self, "_secrets", None) is not None or self._secret_value is not None
        ):
            return self._secret_value or ""
        keys = key.split(".")
        value = self._config
        for i, k in enumerate(keys):
            if isinstance(value, dict) and k in value:
                value = value[k]
            elif i == 0 and len(keys) == 1 and k in self._extra_keys:
                return self._extra_keys[k]
            else:
                return default
        # Only the known modes come back: "correct" stays, the retired
        # light-translation mode and typos fall back to polish silently.
        if key == "llm.mode":
            mode = str(value or "").strip().lower()
            return mode if mode in ("polish", "correct") else "polish"
        return value

    def set(self, key: str, value: Any):
        store = getattr(self, "_secrets", None)
        if key == SECRET_KEY and store is None and not self._file_secrets:
            secret = str(value or "")
            llm = self._config.setdefault("llm", {})
            leaked = bool(llm.get("api_key"))
            if not secret:
                self._secret_value = ""
                self._secret_persist_failed = False
                if leaked:
                    llm["api_key"] = ""
                    self._save_timer.start(500)
                return
            if secret == (self._secret_value or "") and self._secret_persist_failed and not leaked:
                return
            self._secret_value = secret
            self._secret_persist_failed = True
            if leaked:
                llm["api_key"] = ""
                self._save_timer.start(500)
            log.warning("没有可用的凭据存储，API Key 仅在本次运行中有效，未写入配置文件")
            return
        if key == SECRET_KEY and store is not None:
            secret = str(value or "")
            if secret == (self._secret_value or "") and not self._secret_persist_failed:
                return
            self._secret_value = secret
            self._secret_persist_failed = not store.write(secret)
            if secret and not self._secret_persist_failed:
                self._secret_read_failed = False
            if self._secret_persist_failed:
                log.warning("API Key 未能存入凭据管理器，仅在本次运行中有效，未写入配置文件")
            llm = self._config.setdefault("llm", {})
            if llm.get("api_key"):
                # A replaced legacy plaintext key must not outlive the change.
                llm["api_key"] = ""
                self._save_timer.start(500)
            return
        if "." not in key and key not in DEFAULT_CONFIG:
            self._extra_keys[key] = value
            self._save_timer.start(500)
            return
        keys = key.split(".")
        config = self._config
        for k in keys[:-1]:
            if k not in config or not isinstance(config[k], dict):
                config[k] = {}
            config = config[k]
        config[keys[-1]] = value
        if key == "audio.trigger_mode":
            self._realign_active_output()
        elif key in ("output.mode", "output.follow_input_source"):
            self._sync_active_output_slot(key, value)
        # 延迟保存：500ms 后写入，多次 set 只触发一次写入
        self._save_timer.start(500)

    def get_all(self) -> dict:
        return self._config.copy()

    # ── Output mode ───────────────────────────────────

    def _active_output_trigger(self) -> str:
        mode = self.get("audio.trigger_mode", TRIGGER_MODE_HOTKEY)
        if mode not in (TRIGGER_MODE_HOTKEY, TRIGGER_MODE_CONTINUOUS):
            return TRIGGER_MODE_HOTKEY
        return mode

    def _output_section(self) -> dict:
        output = self._config.get("output")
        if not isinstance(output, dict):
            output = {}
            self._config["output"] = output
        return output

    def _output_by_mode(self) -> dict:
        output = self._output_section()
        by_mode = output.get("by_mode")
        if not isinstance(by_mode, dict):
            by_mode = {}
            output["by_mode"] = by_mode
        return by_mode

    def _output_slot(self, trigger: str | None = None) -> dict:
        trigger = trigger or self._active_output_trigger()
        slot = self._output_by_mode().get(trigger)
        if not isinstance(slot, dict):
            slot = {"mode": OUTPUT_MODE_PASTE, "follow_input_source": True}
            self._output_by_mode()[trigger] = slot
        return slot

    @staticmethod
    def _legacy_output_follows(output: dict) -> bool:
        follow = output.get("follow_input_source", None)
        if isinstance(follow, bool):
            return follow
        mode = output.get("mode", OUTPUT_MODE_PASTE)
        return mode == OUTPUT_MODE_PASTE or bool(output.get("mode_auto", False))

    def _automatic_output_mode(self, source: str) -> str:
        if source in HISTORY_ONLY_INPUT_SOURCES:
            return OUTPUT_MODE_HISTORY
        return OUTPUT_MODE_PASTE

    def _active_slot_view(self) -> dict | None:
        """Read the active slot through ``get`` so test doubles keep working."""
        by_mode = self.get("output.by_mode")
        if not isinstance(by_mode, dict):
            return None
        slot = by_mode.get(self._active_output_trigger())
        return slot if isinstance(slot, dict) else None

    def _store_active_slot(self, mode: str, follow) -> None:
        if mode not in OUTPUT_MODES:
            mode = OUTPUT_MODE_PASTE
        trigger = Config._active_output_trigger(self)
        current = self.get("output.by_mode")
        by_mode: dict = {}
        if isinstance(current, dict):
            for key, value in current.items():
                by_mode[key] = dict(value) if isinstance(value, dict) else value
        by_mode[trigger] = {"mode": mode, "follow_input_source": follow}
        self.set("output.by_mode", by_mode)
        self.set("output.mode", mode)
        self.set("output.follow_input_source", follow)

    def _mirror_active_output(self) -> None:
        slot = self._output_slot()
        output = self._output_section()
        output["mode"] = slot.get("mode", OUTPUT_MODE_PASTE)
        output["follow_input_source"] = slot.get("follow_input_source", None)

    def _migrate_output_by_mode(self) -> bool:
        """Copy a shared destination into per-mode slots once.

        Returns True when the file should be rewritten. A later launch that
        already has both slots must not run this again.
        """
        output = self._config.get("output")
        if not isinstance(output, dict):
            return False
        by_mode = output.get("by_mode")
        if (
            isinstance(by_mode, dict)
            and TRIGGER_MODE_HOTKEY in by_mode
            and TRIGGER_MODE_CONTINUOUS in by_mode
        ):
            before = (output.get("mode"), output.get("follow_input_source", None))
            self._mirror_active_output()
            after = (output.get("mode"), output.get("follow_input_source", None))
            return before != after
        follows = self._legacy_output_follows(output)
        mode = output.get("mode", OUTPUT_MODE_PASTE)
        if mode not in OUTPUT_MODES:
            mode = OUTPUT_MODE_PASTE
        source = self.get("audio.input_source", "microphone")
        automatic = {
            "mode": self._automatic_output_mode(source),
            "follow_input_source": True,
        }
        if follows:
            slots = {
                TRIGGER_MODE_HOTKEY: dict(automatic),
                TRIGGER_MODE_CONTINUOUS: dict(automatic),
            }
        else:
            # One destination used to cover every mode. Keep that choice on
            # both, so the mode that was not selected does not start pasting.
            explicit = {"mode": mode, "follow_input_source": False}
            slots = {
                TRIGGER_MODE_HOTKEY: dict(explicit),
                TRIGGER_MODE_CONTINUOUS: dict(explicit),
            }
        output["by_mode"] = slots
        self._mirror_active_output()
        return True

    def _realign_active_output(self) -> None:
        """When the active mode still follows the source, apply that source now."""
        if not Config.output_follows_input_source(self):
            self._mirror_active_output()
            return
        Config.follow_input_source_for_output(self, self.get("audio.input_source", "microphone"))

    def _sync_active_output_slot(self, key: str, value) -> None:
        """Keep direct writes of the legacy mirror on the active mode only."""
        trigger = self._active_output_trigger()
        slot = dict(self._output_slot(trigger))
        if key == "output.mode":
            slot["mode"] = value if value in OUTPUT_MODES else OUTPUT_MODE_PASTE
        else:
            slot["follow_input_source"] = value
        self._output_by_mode()[trigger] = slot

    def output_mode(self) -> str:
        slot = Config._active_slot_view(self)
        mode = slot.get("mode") if slot else self.get("output.mode", OUTPUT_MODE_PASTE)
        return mode if mode in OUTPUT_MODES else OUTPUT_MODE_PASTE

    def history_only_output(self) -> bool:
        return Config.output_mode(self) == OUTPUT_MODE_HISTORY

    def set_output_mode(self, mode: str, *, auto: bool = False) -> None:
        """Pick where results go. ``auto`` marks a choice made by the app."""
        if mode not in OUTPUT_MODES:
            mode = OUTPUT_MODE_PASTE
        Config._store_active_slot(self, mode, bool(auto))
        self.set("output.mode_auto", bool(auto))

    def output_follows_input_source(self) -> bool:
        slot = Config._active_slot_view(self)
        if slot is None:
            follow = self.get("output.follow_input_source", None)
            mode = self.get("output.mode", OUTPUT_MODE_PASTE)
        else:
            follow = slot.get("follow_input_source", None)
            mode = slot.get("mode", OUTPUT_MODE_PASTE)
        if isinstance(follow, bool):
            return follow
        return mode == OUTPUT_MODE_PASTE or bool(self.get("output.mode_auto", False))

    def follow_input_source_for_output(self, source: str) -> str | None:
        """Keep the active mode in step with the audio source.

        In automatic mode, system / mixed capture goes to history and the
        microphone goes to the cursor. Either explicit destination sticks
        across later source changes, and the other trigger mode is left alone.
        Returns the new effective mode only when it changed.
        """
        if not Config.output_follows_input_source(self):
            return None
        mode = Config._automatic_output_mode(self, source)
        changed = mode != Config.output_mode(self)
        Config._store_active_slot(self, mode, True)
        self.set("output.mode_auto", mode == OUTPUT_MODE_HISTORY)
        return mode if changed else None

    @property
    def models_dir(self) -> Path:
        custom = self.get("stt.models_dir", "")
        if custom:
            return Path(custom)
        return self._models_dir

    @property
    def config_dir(self) -> Path:
        return self._config_dir

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
    },
    "audio": {
        "input_source": "microphone",
        "trigger_mode": TRIGGER_MODE_HOTKEY,
        "mic_device_index": -1,
        "system_device_index": -1,
        "esc_stops_continuous": True,
    },
    "stt": {
        "model_id": DEFAULT_MODEL_ID,
        "num_threads": 4,
        "models_dir": "",
        "download_source": "auto",
        "migration_version": 0,
    },
    "llm": {
        "enabled": False,
        "api_url": "",
        "api_key": "",
        "model_name": "",
        "prompt": "",
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
    },
    "update": {
        "auto_check": True,
        "last_check_at": 0,
    },
}


SECRET_KEY = "llm.api_key"
# Run-key value name written by VoiceInk ≤ 2.1.0; removed once on first start.
LEGACY_AUTO_START_VALUE = "VoiceInk"


class Config:
    def __init__(self, config_dir: Path | str | None = None, secret_store=None):
        if config_dir is not None:
            self._config_dir = Path(config_dir)
        else:
            self._config_dir = user_data_dir()
        # Only the real profile uses Credential Manager; isolated dirs (tests,
        # portable copies) keep everything in their own config.json.
        if secret_store is None and config_dir is None:
            secret_store = default_secret_store()
        self._secrets = secret_store
        self._secret_value: str | None = None
        self._secret_persist_failed = False
        self._secret_read_failed = False
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
                with open(self._config_file, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if not isinstance(raw, dict):
                    raise ValueError("顶层不是对象")
            except (json.JSONDecodeError, ValueError, OSError) as e:
                backup = self._backup_unreadable_config()
                log.warning(
                    "配置文件读取失败，使用默认配置（原文件已备份到 %s）: %s", backup, e
                )
                raw = {}
        self._extra_keys = {k: v for k, v in raw.items() if k not in DEFAULT_CONFIG}
        self._config = self._merge_defaults(DEFAULT_CONFIG, raw)
        if self._strip_test_pollution():
            self.save_immediate()
        # True = 不写欢迎弹窗（升级用户或未配置时用默认 True）；首次本无配置文件时单独打开欢迎
        if not config_existed:
            self._config["first_run_welcome_seen"] = False
        self._migrate_stt_model()
        self._load_secret()

    def _load_secret(self) -> None:
        store = getattr(self, "_secrets", None)
        if store is None:
            return
        in_file = str(self._config.get("llm", {}).get("api_key", "") or "")
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
                else:
                    result[key] = current[key]
            else:
                result[key] = (
                    copy.deepcopy(default_value)
                    if isinstance(default_value, dict)
                    else default_value
                )
        return result

    def _do_save(self):
        """实际执行保存操作"""
        self.save()

    def save_immediate(self):
        """立即保存配置，用于应用退出前"""
        self._save_timer.stop()
        self.save()

    def save(self):
        """Atomic write: write to temp file then rename to prevent corruption."""
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

    def get(self, key: str, default: Any = None) -> Any:
        if key == SECRET_KEY and getattr(self, "_secrets", None) is not None:
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
        # Legacy light-translation mode: treat as polish (silent fallback).
        if key == "llm.mode" and isinstance(value, str) and value.strip().lower() == "translate":
            return "polish"
        return value

    def set(self, key: str, value: Any):
        store = getattr(self, "_secrets", None)
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
        # 延迟保存：500ms 后写入，多次 set 只触发一次写入
        self._save_timer.start(500)

    def get_all(self) -> dict:
        return self._config.copy()

    # ── Output mode ───────────────────────────────────

    def output_mode(self) -> str:
        mode = self.get("output.mode", OUTPUT_MODE_PASTE)
        return mode if mode in OUTPUT_MODES else OUTPUT_MODE_PASTE

    def history_only_output(self) -> bool:
        return self.output_mode() == OUTPUT_MODE_HISTORY

    def set_output_mode(self, mode: str, *, auto: bool = False) -> None:
        """Pick where results go. ``auto`` marks a choice made by the app."""
        if mode not in OUTPUT_MODES:
            mode = OUTPUT_MODE_PASTE
        self.set("output.mode", mode)
        self.set("output.mode_auto", bool(auto))

    def follow_input_source_for_output(self, source: str) -> str | None:
        """Keep output mode in step with the audio source.

        Picking 「仅电脑播放」 or 「混合」 switches to history-only output so a
        meeting is not typed into the front window; going back to the
        microphone undoes that, but only when the app made the choice. A
        history-only mode the user set by hand is left alone, and the user can
        flip the switch back at any time. Returns the new mode if changed.
        """
        current = self.output_mode()
        auto = bool(self.get("output.mode_auto", False))
        if source in HISTORY_ONLY_INPUT_SOURCES:
            if current == OUTPUT_MODE_PASTE:
                self.set_output_mode(OUTPUT_MODE_HISTORY, auto=True)
                return OUTPUT_MODE_HISTORY
            return None
        if current == OUTPUT_MODE_HISTORY and auto:
            self.set_output_mode(OUTPUT_MODE_PASTE, auto=False)
            return OUTPUT_MODE_PASTE
        return None

    @property
    def models_dir(self) -> Path:
        custom = self.get("stt.models_dir", "")
        if custom:
            return Path(custom)
        return self._models_dir

    @property
    def config_dir(self) -> Path:
        return self._config_dir

"""Tests for README「声音收录 / 触发方式」相关配置项。"""

import json
import shutil
import tempfile
from pathlib import Path

import pytest

from sayink.config import (
    DEFAULT_CONFIG,
    TRIGGER_MODE_CONTINUOUS,
    TRIGGER_MODE_HOTKEY,
    Config,
)


class TestAudioConfigDefaults:
    """README: 多种音频来源 + 按住快捷键 / 自动持续转写。"""

    def test_default_input_source_is_microphone(self):
        assert DEFAULT_CONFIG["audio"]["input_source"] == "microphone"

    def test_default_trigger_mode_is_continuous(self):
        assert DEFAULT_CONFIG["audio"]["trigger_mode"] == TRIGGER_MODE_CONTINUOUS

    def test_default_device_indices_auto(self):
        assert DEFAULT_CONFIG["audio"]["mic_device_index"] == -1
        assert DEFAULT_CONFIG["audio"]["system_device_index"] == -1

    def test_trigger_mode_constants(self):
        assert TRIGGER_MODE_HOTKEY == "hotkey"
        assert TRIGGER_MODE_CONTINUOUS == "continuous"


class TestAudioConfigPersistence:
    def test_save_and_load_system_source_hotkey_mode(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Config, "_ensure_dirs", lambda self: None)
        cfg_dir = tmp_path / ".sayink"
        cfg_dir.mkdir()
        cfg_file = cfg_dir / "config.json"
        cfg_file.write_text(
            json.dumps(
                {
                    "audio": {
                        "input_source": "system",
                        "trigger_mode": "hotkey",
                        "mic_device_index": -1,
                        "system_device_index": 17,
                    }
                }
            ),
            encoding="utf-8",
        )

        config = Config()
        config._config_dir = cfg_dir
        config._config_file = cfg_file
        config._load()

        assert config.get("audio.input_source") == "system"
        assert config.get("audio.trigger_mode") == "hotkey"
        assert config.get("audio.system_device_index") == 17

    def test_set_mixed_source_persists(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Config, "_ensure_dirs", lambda self: None)
        cfg_dir = tmp_path / ".sayink"
        cfg_dir.mkdir()
        config = Config()
        config._config_dir = cfg_dir
        config._config_file = cfg_dir / "config.json"
        config._config = {
            "hotkey": DEFAULT_CONFIG["hotkey"],
            "audio": dict(DEFAULT_CONFIG["audio"]),
            "stt": dict(DEFAULT_CONFIG["stt"]),
            "llm": dict(DEFAULT_CONFIG["llm"]),
        }

        config.set("audio.input_source", "mixed")
        config.set("audio.trigger_mode", TRIGGER_MODE_HOTKEY)
        config.save_immediate()

        reloaded = json.loads((cfg_dir / "config.json").read_text(encoding="utf-8"))
        assert reloaded["audio"]["input_source"] == "mixed"
        assert reloaded["audio"]["trigger_mode"] == TRIGGER_MODE_HOTKEY

    def test_merge_fills_missing_audio_keys(self):
        from sayink.config import Config

        cfg = Config.__new__(Config)
        fresh_defaults = {
            "hotkey": "ctrl+space",
            "audio": {
                "input_source": "microphone",
                "trigger_mode": TRIGGER_MODE_CONTINUOUS,
                "mic_device_index": -1,
                "system_device_index": -1,
            },
        }
        merged = cfg._merge_defaults(fresh_defaults, {"hotkey": "alt+space"})
        assert merged["hotkey"] == "alt+space"
        assert merged["audio"]["input_source"] == "microphone"
        assert merged["audio"]["trigger_mode"] == TRIGGER_MODE_CONTINUOUS

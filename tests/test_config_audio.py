"""Tests for README「声音收录 / 触发方式」相关配置项。"""

import json
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

    def test_default_trigger_mode_is_hold_to_talk(self):
        # P-01: dictation first; continuous transcription is the opt-in mode.
        assert DEFAULT_CONFIG["audio"]["trigger_mode"] == TRIGGER_MODE_HOTKEY

    def test_default_device_indices_auto(self):
        assert DEFAULT_CONFIG["audio"]["mic_device_index"] == -1
        assert DEFAULT_CONFIG["audio"]["system_device_index"] == -1

    def test_trigger_mode_constants(self):
        assert TRIGGER_MODE_HOTKEY == "hotkey"
        assert TRIGGER_MODE_CONTINUOUS == "continuous"


@pytest.mark.parametrize("mode", ["paste", "history"])
def test_fixed_output_choice_survives_every_source_and_config_reload(config, config_home, mode):
    config.set_output_mode(mode)
    for source in ("system", "microphone", "mixed", "microphone"):
        assert config.follow_input_source_for_output(source) is None
        assert config.output_mode() == mode
    config.save_immediate()
    reloaded = Config(config_dir=config_home)
    assert not reloaded.output_follows_input_source()
    reloaded.follow_input_source_for_output("mixed")
    assert reloaded.output_mode() == mode


def test_automatic_output_keeps_following_after_returning_to_microphone(config):
    for source, expected in (("system", "history"), ("microphone", "paste"), ("mixed", "history")):
        config.follow_input_source_for_output(source)
        assert config.output_mode() == expected
        assert config.output_follows_input_source()


def test_legacy_manual_history_and_automatic_history_keep_their_intent(config):
    config.set("output.mode", "history")
    config.set("output.follow_input_source", None)
    config.set("output.mode_auto", False)
    config.follow_input_source_for_output("microphone")
    assert config.history_only_output()
    config.set("output.mode_auto", True)
    config.follow_input_source_for_output("microphone")
    assert not config.history_only_output()


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

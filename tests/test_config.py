import json


from sayink.config import Config, format_hotkey, DEFAULT_CONFIG
from sayink.speech_recognizer import DEFAULT_MODEL_ID


class TestFormatHotkey:
    def test_empty_hotkey(self):
        assert format_hotkey("") == ""

    def test_single_key(self):
        assert format_hotkey("space") == "Space"

    def test_modifier_combination(self):
        result = format_hotkey("ctrl+space")
        assert "Ctrl" in result
        assert "Space" in result
        assert " + " in result

    def test_multiple_modifiers(self):
        result = format_hotkey("ctrl+shift+space")
        parts = result.split(" + ")
        assert len(parts) == 3
        assert "Ctrl" in parts
        assert "Shift" in parts
        assert "Space" in parts

    def test_lowercase_input(self):
        result = format_hotkey("ALT+TAB")
        assert result == "Alt + Tab"

    def test_single_character_uppercase(self):
        result = format_hotkey("a")
        assert result == "A"


class TestConfigDefaults:
    def test_default_hotkey(self):
        assert DEFAULT_CONFIG["hotkey"] == "alt+x"

    def test_default_sound_enabled(self):
        assert DEFAULT_CONFIG["sound_enabled"] is True

    def test_default_auto_start(self):
        assert DEFAULT_CONFIG["auto_start"] is False

    def test_default_first_run_welcome_seen(self):
        assert DEFAULT_CONFIG["first_run_welcome_seen"] is True

    def test_default_model_id(self):
        assert DEFAULT_CONFIG["stt"]["model_id"] == DEFAULT_MODEL_ID
        assert DEFAULT_MODEL_ID == "funasr-nano"

    def test_default_num_threads(self):
        assert DEFAULT_CONFIG["stt"]["num_threads"] == 4

    def test_default_llm_enabled(self):
        assert DEFAULT_CONFIG["llm"]["enabled"] is False

    def test_default_llm_api_fields_empty(self):
        assert DEFAULT_CONFIG["llm"]["api_url"] == ""
        assert DEFAULT_CONFIG["llm"]["api_key"] == ""
        assert DEFAULT_CONFIG["llm"]["model_name"] == ""

    def test_default_llm_mode_is_polish(self):
        assert DEFAULT_CONFIG["llm"]["mode"] == "polish"


class TestConfigLlmModeFallback:
    def test_get_translate_mode_falls_back_to_polish(self, config_home):
        with open(config_home / "config.json", "w", encoding="utf-8") as f:
            json.dump({"llm": {"mode": "translate"}}, f)
        config = Config(config_dir=config_home)
        assert config.get("llm.mode") == "polish"

    def test_get_translate_mode_case_insensitive(self, config_home):
        with open(config_home / "config.json", "w", encoding="utf-8") as f:
            json.dump({"llm": {"mode": "Translate"}}, f)
        config = Config(config_dir=config_home)
        assert config.get("llm.mode") == "polish"


class TestConfigInit:
    def test_config_loads_defaults(self, config_home):
        with open(config_home / "config.json", "w", encoding="utf-8") as f:
            json.dump({}, f)
        config = Config(config_dir=config_home)
        assert config.get("hotkey") == "alt+x"
        assert config.get("sound_enabled") is True

    def test_config_loads_existing(self, config_home):
        config_data = {
            "hotkey": "alt+space",
            "sound_enabled": False,
            "stt": {"model_id": "sensevoice"},
        }
        with open(config_home / "config.json", "w", encoding="utf-8") as f:
            json.dump(config_data, f)
        config = Config(config_dir=config_home)
        assert config.get("hotkey") == "alt+space"
        assert config.get("sound_enabled") is False
        assert config.get("stt.model_id") == "sensevoice"

    def test_model_chosen_after_migration_survives_restart(self, config_home):
        """Regression: migration_version was dropped on load, so the one-time
        migration reran every start and reset Qwen3 / FireRed to Fun-ASR-Nano."""
        config = Config(config_dir=config_home)
        config.set("stt.model_id", "qwen3-asr-0.6b")
        config.save_immediate()
        assert Config(config_dir=config_home).get("stt.model_id") == "qwen3-asr-0.6b"


class TestSharedRecognitionSettings:
    """Glossary / language / VAD / speaker model are shared by dictation and
    meetings, so they live under ``stt.*``; an older ``meeting.*`` layout is
    moved there once on load."""

    def test_defaults_live_under_stt(self):
        stt = DEFAULT_CONFIG["stt"]
        assert stt["glossary"] == []
        assert stt["language"] == "auto"
        assert stt["vad"] == "silero"
        assert stt["speaker_model_enabled"] is True
        for key in ("glossary", "language", "vad", "speaker_model_enabled"):
            assert key not in DEFAULT_CONFIG["meeting"]

    def test_legacy_meeting_keys_are_migrated_and_saved(self, config_home):
        legacy = {
            "meeting": {
                "input_source": "microphone",
                "glossary": [{"term": "SayInk"}],
                "language": "en",
                "vad": "energy",
                "speaker_model_enabled": False,
            }
        }
        with open(config_home / "config.json", "w", encoding="utf-8") as f:
            json.dump(legacy, f)
        config = Config(config_dir=config_home)
        assert config.get("stt.glossary") == [{"term": "SayInk"}]
        assert config.get("stt.language") == "en"
        assert config.get("stt.vad") == "energy"
        assert config.get("stt.speaker_model_enabled") is False
        assert config.get("meeting.input_source") == "microphone"
        assert config.get("meeting.glossary") is None
        with open(config_home / "config.json", encoding="utf-8") as f:
            on_disk = json.load(f)
        assert on_disk["stt"]["language"] == "en"
        assert "language" not in on_disk["meeting"]

    def test_existing_stt_value_wins_over_legacy(self, config_home):
        data = {"stt": {"language": "zh"}, "meeting": {"language": "en"}}
        with open(config_home / "config.json", "w", encoding="utf-8") as f:
            json.dump(data, f)
        config = Config(config_dir=config_home)
        assert config.get("stt.language") == "zh"
        assert config.get("meeting.language") is None


class TestConfigGetSet:
    def test_get_nested_key(self, config):
        assert config.get("stt.model_id") == DEFAULT_MODEL_ID
        assert config.get("llm.enabled") is False

    def test_get_default_value(self, config):
        assert config.get("nonexistent_key", "default") == "default"
        assert config.get("stt.nonexistent", 123) == 123

    def test_set_simple_value(self, config):
        config.set("hotkey", "ctrl+a")
        assert config.get("hotkey") == "ctrl+a"

    def test_set_nested_value(self, config):
        config.set("llm.enabled", True)
        assert config.get("llm.enabled") is True
        config.set("stt.num_threads", 8)
        assert config.get("stt.num_threads") == 8

    def test_get_all(self, config):
        all_config = config.get_all()
        assert isinstance(all_config, dict)
        assert "hotkey" in all_config
        assert "stt" in all_config
        assert "llm" in all_config


class TestConfigPersistence:
    def test_config_saves_to_file(self, config):
        config.set("test_key", "test_value")
        config.save()

        config_path = config._config_file
        assert config_path.exists()

        with open(config_path, "r", encoding="utf-8") as f:
            saved_data = json.load(f)
        assert "test_key" in saved_data or "test_key" in config.get_all()

    def test_config_loads_saved_values(self, config_home):
        config = Config(config_dir=config_home)
        config.set("persistence_test", "saved_value")
        config.save_immediate()

        config2 = Config(config_dir=config_home)
        assert config2.get("persistence_test") == "saved_value"


class TestConfigAtomicWrite:
    def test_atomic_write_creates_temp_file(self, config_home):
        config = Config(config_dir=config_home)
        config.set("atomic_test", True)
        config.save()

        temp_files = list(config_home.glob("config_*.tmp"))
        assert len(temp_files) == 0


class TestConfigModelsDir:
    def test_models_dir_property(self, config):
        from pathlib import Path

        models_dir = config.models_dir
        assert isinstance(models_dir, Path)

    def test_custom_models_dir(self, config, tmp_path):
        custom_path = tmp_path / "custom_models"
        config.set("stt.models_dir", str(custom_path))
        assert config.models_dir == custom_path


class TestConfigRegistrySync:
    def test_registry_sync_handles_errors(self, config):
        assert config.get("auto_start") is not None


def test_reserved_hotkeys_are_detected_regardless_of_order():
    from sayink.config import is_reserved_hotkey

    assert is_reserved_hotkey("ctrl+c")
    assert is_reserved_hotkey("C+Ctrl")
    assert is_reserved_hotkey("cmd+l")
    assert not is_reserved_hotkey("alt+z")
    assert not is_reserved_hotkey("shift+x")
    assert not is_reserved_hotkey("alt+x")
    assert not is_reserved_hotkey("alt+space")


def test_esc_stops_continuous_defaults_on(config):
    assert config.get("audio.esc_stops_continuous") is True


def test_unreadable_config_is_backed_up_before_defaults_are_saved(config_home):
    from sayink.config import Config

    broken = '{"llm": {"api_key": "sk-keep-me"'
    (config_home / "config.json").write_text(broken, encoding="utf-8")

    cfg = Config(config_dir=config_home)
    cfg.save_immediate()

    backups = list(config_home.glob("config.corrupt-*.json"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == broken
    assert cfg.get("llm.api_key") == ""


def test_non_object_config_is_treated_as_unreadable(config_home):
    from sayink.config import Config

    (config_home / "config.json").write_text("[1, 2]", encoding="utf-8")
    cfg = Config(config_dir=config_home)
    assert cfg.get("hotkey") == "alt+x"
    assert list(config_home.glob("config.corrupt-*.json"))


def test_scalar_in_place_of_section_falls_back_to_section_defaults(config_home):
    """F-24: ``"stt": "abc"`` used to survive the merge and crash ``_migrate_stt_model``."""
    import json

    from sayink.config import DEFAULT_CONFIG, Config

    (config_home / "config.json").write_text(
        json.dumps({"stt": "abc", "history": "on", "hotkey": "alt+space"}),
        encoding="utf-8",
    )
    cfg = Config(config_dir=config_home)  # must not raise
    assert cfg.get("hotkey") == "alt+space"
    assert cfg.get("stt.model_id") == DEFAULT_CONFIG["stt"]["model_id"]
    assert cfg.get("history.enabled") == DEFAULT_CONFIG["history"]["enabled"]
    assert isinstance(cfg.get_all()["history"], dict)
    # Valid sections and scalars are untouched; the bad file is not quarantined.
    assert not list(config_home.glob("config.corrupt-*.json"))


def test_mistyped_scalars_do_not_crash_startup_and_false_stays_false(config_home, monkeypatch):
    """A hand-edited config may store ``"false"`` or ``"auto"`` where a bool or
    int belongs. ``"false"`` must not read as on, and ``int()`` at startup must
    not raise."""
    import json

    from sayink.config import Config

    monkeypatch.setattr(Config, "_sync_registry_auto_start", lambda self: None)
    (config_home / "config.json").write_text(json.dumps({
        "auto_start": "false",
        "audio": {"mic_device_index": "auto", "system_device_index": None, "esc_stops_continuous": "no"},
        "history": {"enabled": "false", "retention_days": "ninety", "max_entries": 1.5},
        "stt": {"num_threads": "8", "glossary": "SayInk"},
        "meeting": {"auto_generate": 1, "single_request_char_limit": "lots", "context_window_tokens": None},
        "update": {"auto_check": "false", "last_check_at": "yesterday"},
        "output": {"follow_input_source": "false", "mode": "paste"},
    }), encoding="utf-8")
    cfg = Config(config_dir=config_home)
    assert cfg.get("auto_start") is False
    assert cfg.get("history.enabled") is False
    assert cfg.get("update.auto_check") is False
    assert cfg.get("audio.esc_stops_continuous") is False
    assert cfg.get("meeting.auto_generate") is True
    assert cfg.get("stt.num_threads") == 8
    assert cfg.get("stt.glossary") == []
    assert cfg.get("audio.mic_device_index") == -1
    assert cfg.get("audio.system_device_index") == -1
    assert cfg.get("history.retention_days") == 90
    assert cfg.get("history.max_entries") == 5000
    assert cfg.get("meeting.single_request_char_limit") == 8000
    assert cfg.get("meeting.context_window_tokens") == 32768
    assert cfg.get("update.last_check_at") == 0
    assert cfg.get("output.follow_input_source") is False
    for key, default in (
        ("audio.mic_device_index", -1),
        ("stt.num_threads", 4),
        ("history.retention_days", 90),
        ("meeting.context_window_tokens", 32768),
    ):
        int(cfg.get(key, default))
    saved = json.loads((config_home / "config.json").read_text(encoding="utf-8"))
    assert saved["history"]["enabled"] is False
    assert saved["stt"]["num_threads"] == 8
    assert saved["audio"]["mic_device_index"] == -1


def test_unknown_nested_keys_survive_a_save_by_an_older_build(config_home):
    """A newer release's ``stt.foo`` (or a hand-added key) must not be erased
    the first time this build saves; only top-level extras were kept before."""
    import json

    from sayink.config import Config

    (config_home / "config.json").write_text(
        json.dumps({"stt": {"model_id": "sensevoice", "future_knob": {"a": 1}}, "top_extra": 7}),
        encoding="utf-8",
    )
    cfg = Config(config_dir=config_home)
    cfg.set("hotkey", "ctrl+space")
    cfg.save_immediate()
    saved = json.loads((config_home / "config.json").read_text(encoding="utf-8"))
    assert saved["stt"]["future_knob"] == {"a": 1}
    assert saved["stt"]["model_id"] == "sensevoice"
    assert saved["top_extra"] == 7


def test_save_survives_mkstemp_failure(config, monkeypatch):
    import tempfile

    def _fail(**_kw):
        raise OSError("disk full")

    monkeypatch.setattr(tempfile, "mkstemp", _fail)
    config.save()


class _FakeSecrets:
    def __init__(self, value="", fail_write=False, unreadable=False):
        self.value = value
        self.fail_write = fail_write
        self.unreadable = unreadable
        self.writes = []

    def read(self):
        return None if self.unreadable else self.value

    def write(self, value):
        self.writes.append(value)
        if self.fail_write:
            return False
        self.value = value
        return True


def _write_config(config_home, data):
    import json

    (config_home / "config.json").write_text(json.dumps(data), encoding="utf-8")


def test_api_key_in_file_is_migrated_to_credential_store(config_home):
    import json
    from sayink.config import Config

    _write_config(config_home, {"llm": {"api_key": "sk-old"}})
    secrets = _FakeSecrets()
    cfg = Config(config_dir=config_home, secret_store=secrets)

    assert cfg.get("llm.api_key") == "sk-old"
    assert secrets.value == "sk-old"
    on_disk = json.loads((config_home / "config.json").read_text(encoding="utf-8"))
    assert on_disk["llm"]["api_key"] == ""


def test_setting_api_key_writes_store_not_file(config_home):
    import json
    from sayink.config import Config

    secrets = _FakeSecrets()
    cfg = Config(config_dir=config_home, secret_store=secrets)
    cfg.set("llm.api_key", "sk-new")
    cfg.save_immediate()

    assert cfg.get("llm.api_key") == "sk-new"
    on_disk = json.loads((config_home / "config.json").read_text(encoding="utf-8"))
    assert on_disk["llm"]["api_key"] == ""


def _key_on_disk(config_home) -> str:
    import json

    return json.loads((config_home / "config.json").read_text(encoding="utf-8"))["llm"]["api_key"]


def test_store_write_failure_keeps_key_in_memory_only(config_home):
    from sayink.config import Config

    secrets = _FakeSecrets(fail_write=True)
    cfg = Config(config_dir=config_home, secret_store=secrets)
    cfg.set("llm.api_key", "sk-memory-only")
    cfg.save_immediate()

    assert cfg.get("llm.api_key") == "sk-memory-only"
    assert cfg.secret_persist_failed is True
    assert _key_on_disk(config_home) == ""
    assert "sk-memory-only" not in (config_home / "config.json").read_text(encoding="utf-8")


def test_store_write_retried_after_failure_clears_warning(config_home):
    from sayink.config import Config

    secrets = _FakeSecrets(fail_write=True)
    cfg = Config(config_dir=config_home, secret_store=secrets)
    cfg.set("llm.api_key", "sk-retry")
    secrets.fail_write = False
    cfg.set("llm.api_key", "sk-retry")

    assert cfg.secret_persist_failed is False
    assert secrets.value == "sk-retry"


def test_unreadable_store_keeps_file_key(config_home):
    from sayink.config import Config

    _write_config(config_home, {"llm": {"api_key": "sk-file"}})
    cfg = Config(config_dir=config_home, secret_store=_FakeSecrets(unreadable=True))
    assert cfg.get("llm.api_key") == "sk-file"


def test_unreadable_store_scrubs_plaintext_key_from_file(config_home):
    """README P0: the key is never left in config.json, even when migration fails."""
    from sayink.config import Config

    _write_config(config_home, {"llm": {"api_key": "sk-file"}})
    cfg = Config(
        config_dir=config_home,
        secret_store=_FakeSecrets(unreadable=True, fail_write=True),
    )

    assert cfg.get("llm.api_key") == "sk-file"
    assert cfg.secret_persist_failed is True
    assert _key_on_disk(config_home) == ""
    cfg.set("sound_enabled", False)
    cfg.save_immediate()
    assert "sk-file" not in (config_home / "config.json").read_text(encoding="utf-8")


def test_unreadable_but_writable_store_still_takes_the_file_key(config_home):
    """Reading can fail where writing works; the key must not be lost then."""
    from sayink.config import Config

    _write_config(config_home, {"llm": {"api_key": "sk-file"}})
    secrets = _FakeSecrets(unreadable=True)
    cfg = Config(config_dir=config_home, secret_store=secrets)

    assert secrets.writes == ["sk-file"]
    assert cfg.get("llm.api_key") == "sk-file"
    assert cfg.secret_persist_failed is False
    assert _key_on_disk(config_home) == ""


def test_failed_migration_scrubs_plaintext_key_from_file(config_home):
    from sayink.config import Config

    _write_config(config_home, {"llm": {"api_key": "sk-legacy"}})
    cfg = Config(config_dir=config_home, secret_store=_FakeSecrets(fail_write=True))

    assert cfg.get("llm.api_key") == "sk-legacy"
    assert cfg.secret_persist_failed is True
    assert _key_on_disk(config_home) == ""


def test_unreadable_store_never_writes_new_key_to_file(config_home):
    from sayink.config import Config

    secrets = _FakeSecrets(unreadable=True, fail_write=True)
    cfg = Config(config_dir=config_home, secret_store=secrets)
    cfg.set("llm.api_key", "sk-new")
    cfg.save_immediate()

    assert cfg.get("llm.api_key") == "sk-new"
    assert _key_on_disk(config_home) == ""


def test_replacing_legacy_file_key_removes_plaintext_even_if_store_fails(config_home):
    from sayink.config import Config

    _write_config(config_home, {"llm": {"api_key": "sk-legacy"}})
    cfg = Config(config_dir=config_home, secret_store=_FakeSecrets(fail_write=True))
    assert cfg.get("llm.api_key") == "sk-legacy"

    cfg.set("llm.api_key", "sk-replacement")
    cfg.save_immediate()

    assert cfg.get("llm.api_key") == "sk-replacement"
    assert _key_on_disk(config_home) == ""


def test_missing_secret_store_keeps_a_new_key_in_memory_only(config_home):
    from sayink.config import Config

    cfg = Config(config_dir=config_home, file_secrets=False)
    cfg.set("llm.api_key", "sk-plaintext")
    cfg.save_immediate()

    assert cfg.get("llm.api_key") == "sk-plaintext"
    assert cfg.secret_persist_failed is True
    assert _key_on_disk(config_home) == ""
    assert "sk-plaintext" not in (config_home / "config.json").read_text(encoding="utf-8")


def test_missing_secret_store_scrubs_a_legacy_file_key(config_home):
    from sayink.config import Config

    _write_config(config_home, {"llm": {"api_key": "sk-file"}})
    cfg = Config(config_dir=config_home, file_secrets=False)

    assert cfg.get("llm.api_key") == "sk-file"
    assert cfg.secret_persist_failed is True
    assert _key_on_disk(config_home) == ""
    assert "sk-file" not in (config_home / "config.json").read_text(encoding="utf-8")


def test_isolated_config_dir_does_not_use_credential_store(config_home, monkeypatch):
    from sayink.config import Config

    monkeypatch.setattr("sayink.config.default_secret_store", lambda: _FakeSecrets("sk-real"))
    cfg = Config(config_dir=config_home)
    assert cfg.get("llm.api_key") == ""


def test_fractional_last_check_at_survives_restart(config_home):
    """Update checks store time.time(). A fractional stamp must not reset to 0."""
    import json
    import time

    from sayink.config import Config
    from sayink.updater import should_auto_check

    stamp = time.time()
    cfg = Config(config_dir=config_home)
    cfg.set("update.last_check_at", stamp)
    cfg.save_immediate()
    loaded = Config(config_dir=config_home).get("update.last_check_at")
    assert loaded == int(stamp)
    assert json.loads((config_home / "config.json").read_text(encoding="utf-8"))["update"]["last_check_at"] == int(stamp)
    assert should_auto_check(enabled=True, last_check_at=float(loaded), now=stamp + 60) is False


def test_config_with_bom_is_not_treated_as_corrupt(config_home):
    from sayink.config import Config

    (config_home / "config.json").write_text(
        '{"history": {"enabled": false}}', encoding="utf-8-sig",
    )
    cfg = Config(config_dir=config_home)
    assert cfg.get("history.enabled") is False
    assert not list(config_home.glob("config.corrupt-*.json"))


def test_deeply_nested_config_falls_back_instead_of_crashing(config_home):
    from sayink.config import Config

    (config_home / "config.json").write_text('{"unknown": ' + ("[" * 2000) + "0" + ("]" * 2000) + "}", encoding="utf-8")
    cfg = Config(config_dir=config_home)
    assert cfg.get("history.enabled") is True
    assert list(config_home.glob("config.corrupt-*.json"))


def test_save_failure_is_visible_to_the_caller(config_home, monkeypatch):
    from sayink.config import Config

    cfg = Config(config_dir=config_home)
    cfg.set("history.enabled", False)

    def deny(_src, _dst):
        raise PermissionError("read-only")

    monkeypatch.setattr("sayink.config.os.replace", deny)
    assert cfg.save_immediate() is False

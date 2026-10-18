"""TDD tests for history config defaults (T2)."""

from __future__ import annotations

import json

from sayink.config import Config, DEFAULT_CONFIG


class TestHistoryDefaults:
    def test_default_config_has_history_block(self):
        assert DEFAULT_CONFIG["history"] == {
            "enabled": True,
            "onboarded": False,
            "retention_days": 90,
            "max_entries": 5000,
        }

    def test_legacy_config_without_history_gets_defaults(self, config_home):
        with open(config_home / "config.json", "w", encoding="utf-8") as f:
            json.dump({"hotkey": "ctrl+space"}, f)
        config = Config(config_dir=config_home)
        assert config.get("history.enabled") is True
        assert config.get("history.onboarded") is False
        assert config.get("history.retention_days") == 90
        assert config.get("history.max_entries") == 5000

    def test_set_history_enabled_persists_after_debounce(self, config):
        from PyQt6.QtTest import QTest

        config.set("history.enabled", False)
        assert config.get("history.enabled") is False
        # Wait for the observable write, not a 100ms scheduling margin: the
        # full Qt suite can spend longer than that processing queued paints.
        for _ in range(40):
            QTest.qWait(50)
            try:
                saved = json.loads((config.config_dir / "config.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if saved.get("history", {}).get("enabled") is False:
                break
        reloaded = Config(config_dir=config.config_dir)
        assert reloaded.get("history.enabled") is False

"""TDD tests for foreground process name capture (T3 / D4)."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

import sayink.text_paster as tp


class TestGetForegroundProcessName:
    def test_returns_basename_from_pid(self, monkeypatch):
        monkeypatch.setattr(
            tp,
            "get_foreground_window_info",
            lambda: (1, "Google Chrome", 4242),
        )

        if sys.platform == "win32":
            mock_win32api = MagicMock()
            mock_win32process = MagicMock()
            mock_win32process.GetModuleFileNameEx.return_value = (
                r"C:\Program Files\Google\Chrome\Application\chrome.exe"
            )
            monkeypatch.setitem(sys.modules, "win32api", mock_win32api)
            monkeypatch.setitem(sys.modules, "win32process", mock_win32process)
            monkeypatch.setitem(sys.modules, "win32con", MagicMock())
        else:
            # Non-Windows path returns "" in Phase 1.
            assert tp.get_foreground_process_name() == ""
            return

        assert tp.get_foreground_process_name() == "chrome.exe"
        mock_win32api.OpenProcess.assert_called()
        mock_win32process.GetModuleFileNameEx.assert_called()

    def test_returns_empty_string_on_failure(self, monkeypatch):
        monkeypatch.setattr(
            tp,
            "get_foreground_window_info",
            lambda: (0, "", 0),
        )
        assert tp.get_foreground_process_name() == ""

    def test_returns_empty_when_module_lookup_raises(self, monkeypatch):
        if sys.platform != "win32":
            pytest.skip("Windows-specific failure path")
        monkeypatch.setattr(
            tp,
            "get_foreground_window_info",
            lambda: (1, "X", 99),
        )
        mock_win32api = MagicMock()
        mock_win32api.OpenProcess.side_effect = OSError("denied")
        monkeypatch.setitem(sys.modules, "win32api", mock_win32api)
        monkeypatch.setitem(sys.modules, "win32process", MagicMock())
        monkeypatch.setitem(sys.modules, "win32con", MagicMock())
        assert tp.get_foreground_process_name() == ""

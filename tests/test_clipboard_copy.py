"""copy_text retries when a clipboard manager grabs the clipboard mid-write."""

from __future__ import annotations

from unittest.mock import patch

import sayink.ui.clipboard as clip


class _FlakyClipboard:
    """Fails the first ``failures`` writes the way Windows does: silently."""

    def __init__(self, failures: int):
        self._failures = failures
        self.value = ""
        self.writes = 0

    def setText(self, text):
        self.writes += 1
        if self._failures > 0:
            self._failures -= 1
            return
        self.value = text

    def text(self):
        return self.value


def _run(failures: int) -> tuple[bool, _FlakyClipboard]:
    board = _FlakyClipboard(failures)
    with patch.object(clip.QApplication, "clipboard", staticmethod(lambda: board)), patch.object(
        clip, "_pump", lambda _ms: None
    ):
        ok = clip.copy_text("hello")
    return ok, board


def test_first_write_succeeds_without_retry():
    ok, board = _run(0)
    assert ok and board.value == "hello" and board.writes == 1


def test_transient_failures_are_retried():
    ok, board = _run(2)
    assert ok and board.value == "hello" and board.writes == 3


def test_persistent_failure_is_reported():
    ok, board = _run(99)
    assert ok is False and board.value == "" and board.writes == clip._ATTEMPTS


def test_history_window_tells_the_user_when_copy_fails(_qapp_session, monkeypatch):
    from sayink.ui.history_window import COPY_FAILED_TEXT, HistoryWindow
    from tests.test_history_window import FakeHistoryStore, _select_polished_session

    monkeypatch.setattr("sayink.ui.history_window.copy_text", lambda _text: False)
    window = HistoryWindow(FakeHistoryStore())
    try:
        _select_polished_session(window)
        window._copy_selected_effective()
        assert window._feedback_label.text() == COPY_FAILED_TEXT
    finally:
        window.close()

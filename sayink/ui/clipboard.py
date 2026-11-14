"""Clipboard writes that survive clipboard managers.

On Windows, Qt publishes clipboard data as an OLE object owned by this
process. Tools such as Ditto, Listary or the built-in clipboard history read
it right after every change, and that read is a cross-process COM call which
only completes while our event loop is running. If the loop is blocked, the
manager keeps the clipboard open until the COM timeout (~30 s) and every
write in between fails silently. So: pump events after a write, verify the
text actually landed, and retry briefly — 「已复制」 is only shown when true.
"""

from __future__ import annotations

import logging
import threading

from PyQt6.QtCore import QEventLoop
from PyQt6.QtWidgets import QApplication

log = logging.getLogger("SayInk")

COPY_FAILED_TEXT = "复制失败，请再试一次"

_ATTEMPTS = 6
_SETTLE_MS = 30
_lock = threading.Lock()
_busy = False
_queued_text: str | None = None


def _pump(ms: int) -> None:
    """Serve cross-process OLE clipboard reads for ``ms`` without dispatching
    mouse/keyboard events that could re-enter UI slots."""
    QApplication.processEvents(
        QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents
        | QEventLoop.ProcessEventsFlag.WaitForMoreEvents,
        ms,
    )


def _write_once(clipboard, text: str) -> bool:
    for attempt in range(_ATTEMPTS):
        clipboard.setText(text)
        _pump(_SETTLE_MS)
        if clipboard.text() == text:
            if attempt:
                log.debug("剪贴板写入在第 %d 次重试后成功", attempt)
            return True
    log.warning("剪贴板写入失败（可能被剪贴板管理器占用）")
    return False


def copy_text(text: str) -> bool:
    """Put ``text`` on the clipboard; return True once it is really there.

    If a queued cross-thread signal slot re-enters ``copy_text`` while ``_pump``
    is serving OLE requests, record the newer text instead of nesting
    ``processEvents`` and apply it immediately once the current write finishes.
    """
    global _busy, _queued_text
    with _lock:
        if _busy:
            _queued_text = text
            return True
        _busy = True
        _queued_text = None
    try:
        clipboard = QApplication.clipboard()
        if clipboard is None:
            return False
        ok = _write_once(clipboard, text)
        while True:
            with _lock:
                next_text = _queued_text
                _queued_text = None
            if next_text is None:
                break
            ok = _write_once(clipboard, next_text)
        return ok
    finally:
        with _lock:
            _busy = False
            _queued_text = None

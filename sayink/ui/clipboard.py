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

from PyQt6.QtCore import QEventLoop
from PyQt6.QtWidgets import QApplication

log = logging.getLogger("SayInk")

_ATTEMPTS = 6
_SETTLE_MS = 30
_busy = False


def _pump(ms: int) -> None:
    """Serve cross-process OLE clipboard reads for ``ms`` without dispatching
    mouse/keyboard events that could re-enter UI slots."""
    QApplication.processEvents(
        QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents
        | QEventLoop.ProcessEventsFlag.WaitForMoreEvents,
        ms,
    )


def copy_text(text: str) -> bool:
    """Put ``text`` on the clipboard; return True once it is really there."""
    global _busy
    if _busy:
        return False
    _busy = True
    try:
        clipboard = QApplication.clipboard()
        for attempt in range(_ATTEMPTS):
            clipboard.setText(text)
            _pump(_SETTLE_MS)
            if clipboard.text() == text:
                if attempt:
                    log.debug("剪贴板写入在第 %d 次重试后成功", attempt)
                return True
        log.warning("剪贴板写入失败（可能被剪贴板管理器占用）")
        return False
    finally:
        _busy = False

"""Keep the frameless main window up while a native file dialog is open.

A system file dialog owns the top-level window. On a frameless window that
ownership shows Qt's small system caption and can deliver a close, which this
app treats as "hide to the tray". The dialog is still modal; only that stray
close is ignored, including one already queued when the dialog returns.
"""

from __future__ import annotations

from contextlib import contextmanager


def _owning_main_window(widget):
    current = widget
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if callable(getattr(current, "begin_native_dialog", None)) and callable(
            getattr(current, "end_native_dialog", None)
        ):
            return current
        parent = getattr(current, "parentWidget", None)
        current = parent() if callable(parent) else None
    return None


@contextmanager
def native_file_dialog(parent):
    window = _owning_main_window(parent)
    if window is not None:
        window.begin_native_dialog()
    try:
        yield
    finally:
        if window is not None:
            window.end_native_dialog()

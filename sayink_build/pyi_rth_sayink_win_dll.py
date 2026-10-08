"""
PyInstaller runtime hook (must run before bundled hooks such as multiprocessing).

On Windows, a system Python install on PATH can cause the loader to bind
python3xx.dll from that install instead of the bundled copy, leading to:

    ImportError: Module use of python310.dll conflicts with version of Python.

This hook clears redirect env vars and prioritizes sys._MEIPASS for DLL search.
Only applies when frozen (sys.frozen).
"""

from __future__ import annotations

import os
import sys


def _prioritize_bundled_runtime() -> None:
    if not getattr(sys, "frozen", False):
        return
    if sys.platform != "win32":
        return

    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return

    # Stop the embedded runtime from resolving stdlib/site against another install.
    os.environ.pop("PYTHONHOME", None)
    os.environ.pop("PYTHONPATH", None)

    if hasattr(os, "add_dll_directory"):
        try:
            os.add_dll_directory(meipass)
        except (OSError, FileNotFoundError):
            pass

    sep = os.pathsep
    path = os.environ.get("PATH", "")
    prefix = meipass + sep
    if path == meipass or path.startswith(prefix):
        return
    os.environ["PATH"] = prefix + path


_prioritize_bundled_runtime()

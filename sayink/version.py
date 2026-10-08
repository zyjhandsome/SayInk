"""
Application release version — single source of truth.

Update __version__ when you ship a new build; packaging scripts and the
installer read this module so EXE / Setup stay in sync.

Format: semantic version (MAJOR.MINOR.PATCH). Optional suffixes (e.g. -beta1)
are not used in Windows file-version quads; only the numeric prefix counts.
"""

from __future__ import annotations

import re

__version__ = "2.1.0"


def file_version_quad() -> str:
    """Four-part version for Inno Setup / Win32 FILEVERSION, e.g. ``1.3.0.0``."""
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", __version__.strip())
    if not m:
        return "0.0.0.0"
    return f"{m.group(1)}.{m.group(2)}.{m.group(3)}.0"


def file_version_tuple() -> tuple[int, int, int, int]:
    """Tuple for PyInstaller ``FixedFileInfo``."""
    a, b, c, d = file_version_quad().split(".")
    return int(a), int(b), int(c), int(d)

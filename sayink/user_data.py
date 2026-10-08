"""Per-user data directory (``~/.sayink``) with a one-time move from ``~/.voiceink``.

The app shipped as VoiceInk up to 2.1.0. Its settings, history, logs and
downloaded models live in ``~/.voiceink``; the first SayInk start renames
that folder so nothing is lost or downloaded twice. The rename is atomic on
the same volume and skipped whenever ``~/.sayink`` already exists.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("SayInk")

DATA_DIR_NAME = ".sayink"
LEGACY_DATA_DIR_NAME = ".voiceink"


def migrate_legacy_data_dir(home: Path | None = None) -> bool:
    """Move ``~/.voiceink`` to ``~/.sayink`` once. Returns True when a move happened."""
    home = Path(home) if home is not None else Path.home()
    new_dir = home / DATA_DIR_NAME
    old_dir = home / LEGACY_DATA_DIR_NAME
    if new_dir.exists() or not old_dir.is_dir():
        return False
    try:
        os.rename(old_dir, new_dir)
    except OSError as exc:
        log.warning("无法把旧数据目录 %s 迁移到 %s: %s", old_dir, new_dir, exc)
        return False
    log.info("已把旧数据目录 %s 迁移到 %s", old_dir, new_dir)
    return True


def user_data_dir(home: Path | None = None) -> Path:
    """``~/.sayink`` (after migrating a legacy ``~/.voiceink`` when present)."""
    home = Path(home) if home is not None else Path.home()
    migrate_legacy_data_dir(home)
    return home / DATA_DIR_NAME

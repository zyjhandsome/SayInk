"""Crash-safe and overwrite-safe atomic file writers for Windows and POSIX."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import time

_REPLACE_BACKOFF_SECONDS: tuple[float, ...] = (0.02, 0.05, 0.1, 0.2)


def _replace_with_retry(src: Path, dst: Path) -> None:
    """Replace ``dst`` with ``src``, retrying brief Windows sharing violations."""
    for delay in _REPLACE_BACKOFF_SECONDS:
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            time.sleep(delay)
    os.replace(src, dst)


def atomic_write_bytes(path: Path | str, data: bytes) -> None:
    """Atomically write ``data`` to ``path`` via a same-directory temporary file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd: int | None = None
    tmp_path: Path | None = None
    try:
        raw_fd, raw_tmp = tempfile.mkstemp(
            dir=str(target.parent),
            prefix=f".{target.name}.",
            suffix=".tmp",
        )
        fd = raw_fd
        tmp_path = Path(raw_tmp)
        with os.fdopen(raw_fd, "wb") as handle:
            fd = None
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(tmp_path, target)
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


def atomic_write_text(
    path: Path | str,
    text: str,
    encoding: str = "utf-8",
    *,
    newline: str | None = None,
) -> None:
    """Atomically write ``text`` to ``path`` via a same-directory temporary file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd: int | None = None
    tmp_path: Path | None = None
    try:
        raw_fd, raw_tmp = tempfile.mkstemp(
            dir=str(target.parent),
            prefix=f".{target.name}.",
            suffix=".tmp",
        )
        fd = raw_fd
        tmp_path = Path(raw_tmp)
        os.close(raw_fd)
        fd = None
        tmp_path.write_text(text, encoding=encoding, newline=newline)
        with tmp_path.open("r+b") as fh:
            os.fsync(fh.fileno())
        _replace_with_retry(tmp_path, target)
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


__all__ = [
    "atomic_write_bytes",
    "atomic_write_text",
]

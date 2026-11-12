"""Opt-in local float32 recording journal, independent of VAD and ASR.

The capture thread never waits for storage. A bounded writer queue reports a
failure instead of silently discarding blocks. Files have no mutable WAV header,
so even an interrupted final write can be exported up to the last whole sample.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import threading
import time
import wave
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import numpy as np

from sayink.audio_utils import TARGET_SAMPLE_RATE

MAX_RECOVERY_BYTES = 2 * 1024**3
MIN_FREE_BYTES = 128 * 1024**2
RETENTION_SECONDS = 7 * 86400
# Super-aged incomplete crash fragments are cleaned up after 30 days, and
# fragments older than 24h are eligible for rolling 2 GB quota eviction so
# abandoned fragments can never permanently lock out future recordings while
# fresh (<24h) crash recordings remain untouched.
INCOMPLETE_RETENTION_SECONDS = 30 * 86400
INCOMPLETE_QUOTA_GRACE_SECONDS = 24 * 3600

_PENDING_BYTES_LOCK = threading.Lock()
_PENDING_BYTES_CACHE: dict[Path, tuple[float, int]] = {}
_PENDING_CACHE_TTL_SEC = 5.0


def _invalidate_pending_bytes_cache(root: Path | None = None) -> None:
    with _PENDING_BYTES_LOCK:
        if root is None:
            _PENDING_BYTES_CACHE.clear()
        else:
            try:
                _PENDING_BYTES_CACHE.pop(root.resolve(), None)
            except OSError:
                _PENDING_BYTES_CACHE.clear()


def _adjust_pending_bytes_cache(root: Path, delta: int) -> None:
    try:
        key = root.resolve()
    except OSError:
        return
    with _PENDING_BYTES_LOCK:
        cached = _PENDING_BYTES_CACHE.get(key)
        if cached is not None:
            _PENDING_BYTES_CACHE[key] = (cached[0], max(0, cached[1] + int(delta)))


def _dir_f32_bytes(folder: Path) -> int:
    if not folder.is_dir() or folder.is_symlink():
        return 0
    total = 0
    for item in folder.glob("*.f32"):
        try:
            if not item.is_symlink():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def _journal_f32_bytes(folder: Path) -> int:
    """Fast non-recursive size of a journal directory (tracks + pending/*.f32)."""
    return _dir_f32_bytes(folder) + _dir_f32_bytes(folder / "pending")


def _tree_f32_bytes(folder: Path) -> int:
    if not folder.is_dir() or folder.is_symlink():
        return 0
    try:
        key = folder.resolve()
    except OSError:
        key = folder
    now = time.monotonic()
    with _PENDING_BYTES_LOCK:
        cached = _PENDING_BYTES_CACHE.get(key)
        if cached is not None and (now - cached[0]) < _PENDING_CACHE_TTL_SEC:
            return cached[1]
    total = _dir_f32_bytes(folder) + _dir_f32_bytes(folder / "pending")
    try:
        for child in folder.iterdir():
            if child.is_dir() and not child.is_symlink() and len(child.name) == 32:
                total += _journal_f32_bytes(child)
    except OSError:
        pass
    with _PENDING_BYTES_LOCK:
        _PENDING_BYTES_CACHE[key] = (now, total)
    return total


def sweep_orphan_pending(root: Path, *, exclude_journal: Path | None = None) -> int:
    """Remove stale queued-segment spill files (``pending/*.f32``) left by a dead session.

    Leaves non-``.f32`` files untouched and never touches raw journal tracks
    (``audio.f32`` / ``microphone.f32`` / ``system.f32``).
    """
    if not root.is_dir() or root.is_symlink():
        return 0
    removed = 0
    excluded = exclude_journal.resolve() if exclude_journal is not None else None
    candidates: list[Path] = [root / "pending"]
    try:
        for child in root.iterdir():
            if not child.is_dir() or child.is_symlink() or len(child.name) != 32:
                continue
            try:
                int(child.name, 16)
            except ValueError:
                continue
            if excluded is not None and child.resolve() == excluded:
                continue
            candidates.append(child / "pending")
    except OSError:
        pass
    for pending in candidates:
        if not pending.is_dir() or pending.is_symlink():
            continue
        for item in list(pending.glob("*.f32")):
            try:
                if not item.is_symlink():
                    item.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                continue
        try:
            if not any(pending.iterdir()):
                pending.rmdir()
        except OSError:
            pass
    return removed


@dataclass(frozen=True)
class RecoveryRecording:
    path: Path
    metadata: dict
    track: str = "audio"

    def __post_init__(self):
        if self.track not in {"audio", "microphone", "system"}:
            raise ValueError("未知录音音轨")

    @property
    def cache_bytes(self):
        return _journal_f32_bytes(self.path)

    @property
    def available_tracks(self):
        return [track for track in ("audio", "microphone", "system")
                if (self.path / (track + ".f32")).is_file() and not (self.path / (track + ".f32")).is_symlink()]

    @property
    def samples(self) -> int:
        return (self.path / (self.track + ".f32")).stat().st_size // 4

    @property
    def seconds(self) -> float:
        return self.samples / TARGET_SAMPLE_RATE

    def blocks(self, samples: int = TARGET_SAMPLE_RATE):
        with (self.path / (self.track + ".f32")).open("rb") as handle:
            while data := handle.read(samples * 4):
                usable = len(data) // 4 * 4
                if usable:
                    yield np.frombuffer(data[:usable], dtype="<f4").copy()

    def export_wav(self, target: Path, *, cancelled=None, on_progress=None) -> None:
        if target.resolve().is_relative_to(self.path.parent.resolve()):
            raise ValueError("请选择缓存目录之外的导出位置")
        temporary = target.with_name(f"{target.name}.{uuid4().hex[:8]}.partial")
        total = max(1, self.samples)
        done = 0
        try:
            with wave.open(str(temporary), "wb") as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(TARGET_SAMPLE_RATE)
                for block in self.blocks():
                    if cancelled and cancelled():
                        raise InterruptedError("已取消导出")
                    pcm = (np.clip(np.nan_to_num(block), -1, 1) * 32767).astype("<i2")
                    out.writeframesraw(pcm.tobytes())
                    done += len(block)
                    if on_progress is not None:
                        on_progress(min(done, total), total)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)


def recordings(root: Path) -> list[RecoveryRecording]:
    found = []
    if not root.exists():
        return found
    for path in root.iterdir():
        if not path.is_dir() or path.is_symlink() or len(path.name) != 32:
            continue
        try:
            int(path.name, 16)
            if (path / "session.json").is_symlink():
                continue
            try:
                meta = json.loads((path / "session.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                meta = None
            # The UUID directory + fixed .f32 filename also identifies our
            # format if metadata was interrupted or damaged. Never rewrite it.
            if not isinstance(meta, dict):
                meta = {"sample_rate": TARGET_SAMPLE_RATE, "complete": False,
                        "started_at": path.stat().st_mtime, "metadata_damaged": True}
            if (isinstance(meta.get("started_at", 0), (int, float))
                    and np.isfinite(meta.get("started_at", 0)) and not (path / "session.json").is_symlink()
                    and meta.get("sample_rate") == TARGET_SAMPLE_RATE and (path / "audio.f32").is_file()
                    and not (path / "audio.f32").is_symlink()):
                found.append(RecoveryRecording(path, meta))
        except (OSError, ValueError, TypeError):
            continue
    return sorted(found, key=lambda item: item.metadata.get("started_at", 0), reverse=True)


def delete_recording(root: Path, recording: RecoveryRecording) -> None:
    path = recording.path.resolve()
    if path.parent != root.resolve() or recording.path.is_symlink():
        raise ValueError("缓存路径不属于录音目录")
    shutil.rmtree(path)
    _invalidate_pending_bytes_cache(root)


def _is_hex32_dir(path: Path) -> bool:
    if not path.is_dir() or path.is_symlink() or len(path.name) != 32:
        return False
    try:
        int(path.name, 16)
        return True
    except ValueError:
        return False


def delete_session_recovery_files(session_id: str, base_dir: Path | None = None) -> int:
    """Delete plaintext ``.f32`` recovery journals associated with ``session_id``.

    Matches either the 32-hex journal directory name directly or ``session_id`` /
    ``meeting_id`` / ``history_id`` / ``id`` stored in ``session.json``.
    Returns the count of deleted ``.f32`` audio files (or ``>= 1`` per removed
    matching journal directory).
    """
    target_id = str(session_id or "").strip()
    if not target_id:
        return 0
    root = Path(base_dir) if base_dir is not None else (Path.home() / ".sayink" / "audio-recovery")
    if not root.exists() or not root.is_dir() or root.is_symlink():
        return 0
    resolved_root = root.resolve()
    removed_files = 0

    for child in list(root.iterdir()):
        if not _is_hex32_dir(child):
            continue
        try:
            if child.resolve().parent != resolved_root:
                continue
        except OSError:
            continue
        matched = child.name.lower() == target_id.lower()
        if not matched:
            meta_file = child / "session.json"
            if meta_file.is_file() and not meta_file.is_symlink():
                try:
                    meta = json.loads(meta_file.read_text(encoding="utf-8"))
                    if isinstance(meta, dict):
                        for key in ("session_id", "meeting_id", "history_id", "id"):
                            val = meta.get(key)
                            if val is not None and str(val).strip() == target_id:
                                matched = True
                                break
                except (OSError, ValueError):
                    pass
        if matched:
            f32_count = sum(
                1
                for f in list(child.glob("*.f32")) + list((child / "pending").glob("*.f32"))
                if not f.is_symlink()
            )
            try:
                shutil.rmtree(child)
                removed_files += max(1, f32_count)
            except OSError:
                continue

    if removed_files:
        _invalidate_pending_bytes_cache(root)
    return removed_files


class RecordingJournal:
    def __init__(self, root: Path, *, metadata: dict | None = None,
                 max_bytes: int = MAX_RECOVERY_BYTES, on_error=None):
        self.root = root
        self.path = root / uuid4().hex
        self._max_bytes = max_bytes
        self._on_error = on_error
        self._error = ""
        self._closed = False
        self._queue: queue.Queue = queue.Queue(maxsize=20)
        self._stop = threading.Event()
        now = time.time()
        self._meta = {**(metadata or {}), "sample_rate": TARGET_SAMPLE_RATE,
                      "started_at": now, "complete": False}
        self._written_bytes = 0
        root.mkdir(parents=True, exist_ok=True)
        sweep_orphan_pending(root)
        _invalidate_pending_bytes_cache(root)
        # Expire completed recordings past 7 days AND super-aged (>30d) incomplete fragments.
        existing = recordings(root)
        kept: list[RecoveryRecording] = []
        for item in existing:
            age = now - float(item.metadata.get("started_at", 0) or 0)
            is_complete = item.metadata.get("complete") is True
            expired = (
                (is_complete and age > RETENTION_SECONDS)
                or (not is_complete and age > INCOMPLETE_RETENTION_SECONDS)
            )
            if expired:
                delete_recording(root, item)
            else:
                kept.append(item)

        self._existing_bytes = sum(item.cache_bytes for item in kept)
        # Rolling quota eviction when total cache hits max_bytes:
        # 1) evict oldest completed recordings first;
        # 2) then evict stale incomplete fragments older than INCOMPLETE_QUOTA_GRACE_SECONDS (24h),
        #    never overwriting fresh (<24h) crash recordings.
        if self._existing_bytes >= max_bytes:
            evictable = sorted(
                (
                    item
                    for item in kept
                    if item.metadata.get("complete") is True
                    or (now - float(item.metadata.get("started_at", 0) or 0)) > INCOMPLETE_QUOTA_GRACE_SECONDS
                ),
                key=lambda item: (
                    0 if item.metadata.get("complete") is True else 1,
                    float(item.metadata.get("started_at", 0) or 0),
                ),
            )
            evicted_paths: set[Path] = set()
            for candidate in evictable:
                if self._existing_bytes < max_bytes:
                    break
                size_bytes = candidate.cache_bytes
                try:
                    delete_recording(root, candidate)
                    evicted_paths.add(candidate.path)
                    self._existing_bytes = max(0, self._existing_bytes - size_bytes)
                except OSError:
                    continue
            if evicted_paths:
                kept = [item for item in kept if item.path not in evicted_paths]

        if self._existing_bytes >= max_bytes or shutil.disk_usage(root).free < MIN_FREE_BYTES:
            raise OSError("录音缓存空间不足，请导出或删除旧录音后重试")
        self.path.mkdir()
        self._save_metadata(sync=False)
        self._thread = threading.Thread(target=self._write, name="SayInkAudioJournal", daemon=True)
        self._thread.start()

    def _save_metadata(self, *, sync: bool = True):
        temp = self.path / "session.tmp"
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(self._meta, handle, ensure_ascii=False)
            handle.flush()
            if sync:
                os.fsync(handle.fileno())
        os.replace(temp, self.path / "session.json")

    @property
    def healthy(self) -> bool:
        return not self._error and not self._closed and self._thread.is_alive()

    @property
    def error(self) -> str:
        return self._error

    @property
    def is_writing(self) -> bool:
        return self._thread.is_alive()

    @property
    def total_recovery_bytes(self) -> int:
        pending = _dir_f32_bytes(self.path / "pending") + _dir_f32_bytes(self.root / "pending")
        return self._existing_bytes + self._written_bytes + pending

    def _fail(self, message: str):
        if not self._error:
            self._error = message
            if self._on_error:
                self._on_error(message)
        self._stop.set()

    def append(self, audio: np.ndarray, *, tracks=None) -> bool:
        if not self.healthy:
            return False
        try:
            data = {"audio": np.asarray(audio, dtype="<f4").reshape(-1).tobytes()}
            for role, track in (tracks or {}).items():
                if role in {"microphone", "system"} and len(track) == len(audio):
                    data[role] = np.asarray(track, dtype="<f4").reshape(-1).tobytes()
            self._queue.put_nowait(data)
            return True
        except queue.Full:
            self._fail("录音缓存写入跟不上采集；已停止缓存，内存中的语音仍继续处理")
            return False

    def _write(self):
        size = 0
        try:
            with ExitStack() as stack:
                handles = {"audio": stack.enter_context((self.path / "audio.f32").open("wb", buffering=0))}
                last_sync = time.monotonic()
                while not self._stop.is_set() or not self._queue.empty():
                    try:
                        data = self._queue.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    chunk_bytes = sum(map(len, data.values()))
                    if self.total_recovery_bytes + chunk_bytes > self._max_bytes:
                        self._fail("录音缓存已达到 2 GB 上限；请导出或删除旧录音")
                        break
                    if time.monotonic() - last_sync >= 1:
                        for handle in handles.values():
                            os.fsync(handle.fileno())
                        last_sync = time.monotonic()
                        if shutil.disk_usage(self.root).free < MIN_FREE_BYTES:
                            self._fail("磁盘剩余空间不足，已停止录音缓存")
                            break
                    for role, samples in data.items():
                        if role not in handles:
                            handles[role] = stack.enter_context((self.path / (role + ".f32")).open("wb", buffering=0))
                        remaining = memoryview(samples)
                        while remaining:
                            written = handles[role].write(remaining)
                            if not written:
                                raise OSError("录音缓存写入不完整")
                            remaining = remaining[written:]
                    size += chunk_bytes
                    self._written_bytes = size
                for handle in handles.values():
                    os.fsync(handle.fileno())
            self._meta.update(complete=self._closed and not self._error,
                              samples=(self.path / "audio.f32").stat().st_size // 4,
                              error=self._error, stopped_at=time.time())
            self._save_metadata()
        except OSError as exc:
            self._fail(f"录音缓存写入失败：{exc}")

    def close(self, timeout: float = 3) -> bool:
        self._closed = True
        self._stop.set()
        self._thread.join(timeout=timeout)
        return not self._thread.is_alive()


@dataclass(frozen=True)
class StoredAudio:
    path: Path
    size: int
    prefix: object = None
    suffix: object = None
    noise_floor: float | None = None
    quiet_level: float = 0.0

    def load(self) -> np.ndarray:
        data = np.fromfile(self.path, dtype="<f4")
        if data.size != self.size:
            raise OSError("排队录音文件不完整；可从录音缓存恢复")
        from sayink.vad_segmenter import attach_segment_acoustics
        from sayink.asr_context import with_context

        if self.noise_floor is not None or self.quiet_level > 0.0:
            data = attach_segment_acoustics(
                data,
                noise_floor=self.noise_floor,
                quiet_level=self.quiet_level,
            )
        return with_context(data, self.prefix, self.suffix)

    def delete(self):
        try:
            byte_len = self.path.stat().st_size if self.path.is_file() else 0
            self.path.unlink(missing_ok=True)
            if byte_len:
                quota_root = _infer_recovery_root_for_pending(self.path.parent)
                if quota_root is not None:
                    _adjust_pending_bytes_cache(quota_root, -byte_len)
        except OSError:
            pass  # The journal still owns this cache; it remains removable there.


def _infer_recovery_root_for_pending(pending_dir: Path) -> Path | None:
    if pending_dir.name != "pending":
        return None
    parent = pending_dir.parent
    if len(parent.name) == 32:
        try:
            int(parent.name, 16)
            if parent.parent.is_dir():
                return parent.parent
        except ValueError:
            pass
    if parent.is_dir():
        return parent
    return None


def store_pending(
    root: Path,
    audio: np.ndarray,
    *,
    max_bytes: int = MAX_RECOVERY_BYTES,
    recovery_root: Path | None = None,
) -> StoredAudio:
    root.mkdir(parents=True, exist_ok=True)
    raw_bytes = np.asarray(audio, dtype="<f4").tobytes()
    if shutil.disk_usage(root).free < MIN_FREE_BYTES + len(raw_bytes):
        raise OSError("磁盘空间不足，无法继续缓存排队语音")
    quota_root = recovery_root if recovery_root is not None else _infer_recovery_root_for_pending(root)
    used_bytes = _tree_f32_bytes(quota_root) if quota_root is not None else _dir_f32_bytes(root)
    if used_bytes + len(raw_bytes) > max_bytes:
        raise OSError("待识别录音已达到 2 GB 上限，请等待识别完成")
    path = root / (uuid4().hex + ".f32")
    noise_floor = getattr(audio, "noise_floor", None)
    quiet_level = float(getattr(audio, "quiet_level", 0.0) or 0.0)
    try:
        with path.open("xb") as handle:
            handle.write(raw_bytes)
            handle.flush()
        if quota_root is not None:
            _adjust_pending_bytes_cache(quota_root, len(raw_bytes))
        return StoredAudio(
            path,
            audio.size,
            getattr(audio, "prefix", None),
            getattr(audio, "suffix", None),
            noise_floor=float(noise_floor) if noise_floor is not None else None,
            quiet_level=quiet_level,
        )
    except OSError:
        path.unlink(missing_ok=True)
        raise

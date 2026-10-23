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
        return sum(file.stat().st_size for file in self.path.rglob("*.f32") if not file.is_symlink())

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

    def export_wav(self, target: Path, *, cancelled=None) -> None:
        if target.resolve().is_relative_to(self.path.parent.resolve()):
            raise ValueError("请选择缓存目录之外的导出位置")
        temporary = target.with_name(target.name + ".partial")
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
        self._meta = {**(metadata or {}), "sample_rate": TARGET_SAMPLE_RATE,
                      "started_at": time.time(), "complete": False}
        root.mkdir(parents=True, exist_ok=True)
        # Only completed old recordings expire automatically. Interrupted
        # recordings require an explicit export/delete, even under quota pressure.
        for item in recordings(root):
            if item.metadata.get("complete") and time.time() - item.metadata.get("started_at", 0) > RETENTION_SECONDS:
                delete_recording(root, item)
        self._existing_bytes = sum(item.cache_bytes for item in recordings(root))
        if self._existing_bytes >= max_bytes or shutil.disk_usage(root).free < MIN_FREE_BYTES:
            raise OSError("录音缓存空间不足，请导出或删除旧录音后重试")
        self.path.mkdir()
        self._save_metadata()
        self._thread = threading.Thread(target=self._write, name="SayInkAudioJournal", daemon=True)
        self._thread.start()

    def _save_metadata(self):
        temp = self.path / "session.tmp"
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(self._meta, handle, ensure_ascii=False)
            handle.flush()
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
                    if self._existing_bytes + size + sum(map(len, data.values())) > self._max_bytes:
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
                    size += sum(map(len, data.values()))
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

    def load(self) -> np.ndarray:
        data = np.fromfile(self.path, dtype="<f4")
        if data.size != self.size:
            raise OSError("排队录音文件不完整；可从录音缓存恢复")
        from sayink.asr_context import with_context
        return with_context(data, self.prefix, self.suffix)

    def delete(self):
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass  # The journal still owns this cache; it remains removable there.


def store_pending(root: Path, audio: np.ndarray) -> StoredAudio:
    root.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(root).free < MIN_FREE_BYTES + audio.nbytes:
        raise OSError("磁盘空间不足，无法继续缓存排队语音")
    if sum(path.stat().st_size for path in root.glob("*.f32")) + audio.nbytes > MAX_RECOVERY_BYTES:
        raise OSError("待识别录音已达到 2 GB 上限，请等待识别完成")
    path = root / (uuid4().hex + ".f32")
    try:
        with path.open("xb") as handle:
            handle.write(np.asarray(audio, dtype="<f4").tobytes())
            handle.flush()
        return StoredAudio(path, audio.size, getattr(audio, "prefix", None), getattr(audio, "suffix", None))
    except OSError:
        path.unlink(missing_ok=True)
        raise

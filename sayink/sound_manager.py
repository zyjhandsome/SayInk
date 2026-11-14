import atexit
import logging
import threading
import numpy as np
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("SayInk")


class SoundManager:
    """Generates and plays simple beep sounds for recording start/stop feedback."""

    SAMPLE_RATE = 44100
    _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sound")
    _executor_lock = threading.Lock()
    _play_lock = threading.Lock()
    _executor_shutdown = False

    def __init__(self, enabled: bool = True):
        self._enabled = enabled
        self._closed = False
        self._cue_cache: dict[tuple[int, str], np.ndarray] = {}
        self._ensure_executor()

    @classmethod
    def _ensure_executor(cls) -> ThreadPoolExecutor:
        with cls._executor_lock:
            if cls._executor_shutdown or cls._executor is None:
                cls._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sound")
                cls._executor_shutdown = False
            return cls._executor

    @classmethod
    def shutdown_executor(cls, wait: bool = False) -> None:
        """Shut down the shared audio cue worker pool idempotently on exit."""
        with cls._executor_lock:
            if cls._executor_shutdown:
                return
            cls._executor_shutdown = True
            executor = cls._executor
        try:
            executor.shutdown(wait=wait, cancel_futures=True)
        except Exception:
            pass

    def shutdown(self, wait: bool = False) -> None:
        """Stop accepting cues for this manager and release the background pool."""
        self._closed = True
        self.shutdown_executor(wait=wait)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool):
        self._enabled = value

    def _generate_tone(self, frequency: float, duration: float, volume: float = 0.3) -> np.ndarray:
        t = np.linspace(0, duration, int(self.SAMPLE_RATE * duration), endpoint=False)
        envelope = np.ones_like(t)
        fade = int(self.SAMPLE_RATE * 0.01)
        if fade > 0 and len(t) > 2 * fade:
            envelope[:fade] = np.linspace(0, 1, fade)
            envelope[-fade:] = np.linspace(1, 0, fade)
        tone = np.sin(2 * np.pi * frequency * t) * volume * envelope
        return tone.astype(np.float32)

    def _play_async(self, audio_data: np.ndarray):
        if self._closed:
            return

        def _play():
            if self._closed or self._executor_shutdown:
                return
            with self._play_lock:
                if self._closed or self._executor_shutdown:
                    return
                try:
                    import sounddevice as sd
                    sd.play(audio_data, self.SAMPLE_RATE, blocking=True)
                except Exception as e:
                    log.warning("播放声音失败: %s", e)

        # 使用线程池串行化提交，避免并发调用 PortAudio 全局流及解释器退出时报 RuntimeError
        executor = self._ensure_executor()
        try:
            executor.submit(_play)
        except RuntimeError:
            log.debug("提示音线程池已关闭，跳过播放")

    def _get_cue(self, name: str) -> np.ndarray:
        key = (self.SAMPLE_RATE, name)
        cached = self._cue_cache.get(key)
        if cached is not None:
            return cached
        if name == "start":
            tone = self._generate_tone(880, 0.1, 0.25)
        elif name == "stop":
            tone = self._generate_tone(660, 0.12, 0.25)
        elif name == "error":
            t1 = self._generate_tone(400, 0.1, 0.2)
            silence = np.zeros(int(self.SAMPLE_RATE * 0.05), dtype=np.float32)
            t2 = self._generate_tone(300, 0.15, 0.2)
            tone = np.concatenate([t1, silence, t2])
        else:
            raise ValueError(f"Unknown sound cue: {name}")
        self._cue_cache[key] = tone
        return tone

    def play_start(self):
        if not self._enabled or self._closed:
            return
        self._play_async(self._get_cue("start"))

    def play_stop(self):
        if not self._enabled or self._closed:
            return
        self._play_async(self._get_cue("stop"))

    def play_error(self):
        if not self._enabled or self._closed:
            return
        self._play_async(self._get_cue("error"))


atexit.register(SoundManager.shutdown_executor)

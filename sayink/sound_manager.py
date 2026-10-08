import logging
import numpy as np
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("SayInk")


class SoundManager:
    """Generates and plays simple beep sounds for recording start/stop feedback."""

    SAMPLE_RATE = 44100
    _executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="sound")

    def __init__(self, enabled: bool = True):
        self._enabled = enabled

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
        def _play():
            try:
                import sounddevice as sd
                sd.play(audio_data, self.SAMPLE_RATE, blocking=True)
            except Exception as e:
                log.warning("播放声音失败: %s", e)

        # 使用线程池，避免频繁创建线程
        self._executor.submit(_play)

    def play_start(self):
        if not self._enabled:
            return
        tone = self._generate_tone(880, 0.1, 0.25)
        self._play_async(tone)

    def play_stop(self):
        if not self._enabled:
            return
        tone = self._generate_tone(660, 0.12, 0.25)
        self._play_async(tone)

    def play_error(self):
        if not self._enabled:
            return
        t1 = self._generate_tone(400, 0.1, 0.2)
        silence = np.zeros(int(self.SAMPLE_RATE * 0.05), dtype=np.float32)
        t2 = self._generate_tone(300, 0.15, 0.2)
        tone = np.concatenate([t1, silence, t2])
        self._play_async(tone)

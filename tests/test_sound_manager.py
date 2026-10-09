import numpy as np
from sayink.sound_manager import SoundManager


class TestSoundManagerConstants:
    def test_sample_rate(self):
        assert SoundManager.SAMPLE_RATE == 44100


class TestSoundManagerInit:
    def test_init_enabled(self):
        manager = SoundManager()
        assert manager._enabled is True

    def test_init_disabled(self):
        manager = SoundManager(enabled=False)
        assert manager._enabled is False

    def test_init_with_default(self):
        manager = SoundManager(True)
        assert manager._enabled is True


class TestSoundManagerEnabled:
    def test_enabled_property(self):
        manager = SoundManager()
        assert manager.enabled is True

    def test_enabled_setter(self):
        manager = SoundManager()
        manager.enabled = False
        assert manager._enabled is False
        manager.enabled = True
        assert manager._enabled is True


class TestSoundManagerGenerateTone:
    def test_generate_tone_returns_array(self):
        manager = SoundManager()
        tone = manager._generate_tone(440, 0.1)
        assert isinstance(tone, np.ndarray)

    def test_generate_tone_correct_length(self):
        manager = SoundManager()
        duration = 0.1
        expected_samples = int(SoundManager.SAMPLE_RATE * duration)
        tone = manager._generate_tone(440, duration)
        assert len(tone) == expected_samples

    def test_generate_tone_dtype(self):
        manager = SoundManager()
        tone = manager._generate_tone(440, 0.1)
        assert tone.dtype == np.float32

    def test_generate_tone_has_envelope(self):
        manager = SoundManager()
        tone = manager._generate_tone(440, 0.1, volume=0.5)
        assert tone.max() <= 0.5
        assert tone.min() >= -0.5

    def test_generate_tone_different_frequencies(self):
        manager = SoundManager()
        tone_440 = manager._generate_tone(440, 0.1)
        tone_880 = manager._generate_tone(880, 0.1)
        assert len(tone_440) == len(tone_880)

    def test_generate_tone_volume_parameter(self):
        manager = SoundManager()
        tone_low = manager._generate_tone(440, 0.1, volume=0.1)
        tone_high = manager._generate_tone(440, 0.1, volume=0.9)
        assert tone_high.max() > tone_low.max()


class TestSoundManagerPlayMethods:
    def test_play_start_exists(self):
        manager = SoundManager()
        assert hasattr(manager, "play_start")
        assert callable(manager.play_start)

    def test_play_stop_exists(self):
        manager = SoundManager()
        assert hasattr(manager, "play_stop")
        assert callable(manager.play_stop)

    def test_play_error_exists(self):
        manager = SoundManager()
        assert hasattr(manager, "play_error")
        assert callable(manager.play_error)


class TestSoundManagerDisabled:
    def test_play_start_when_disabled(self):
        manager = SoundManager(enabled=False)
        manager.play_start()

    def test_play_stop_when_disabled(self):
        manager = SoundManager(enabled=False)
        manager.play_stop()

    def test_play_error_when_disabled(self):
        manager = SoundManager(enabled=False)
        manager.play_error()


class TestSoundManagerPlayAsync:
    def test_play_async_exists(self):
        manager = SoundManager()
        assert hasattr(manager, "_play_async")
        assert callable(manager._play_async)

    def test_play_async_accepts_array(self):
        manager = SoundManager()
        tone = manager._generate_tone(440, 0.1)
        manager._play_async(tone)


class TestSoundManagerThreadPool:
    def test_executor_exists(self):
        assert hasattr(SoundManager, "_executor")
        from concurrent.futures import ThreadPoolExecutor
        assert isinstance(SoundManager._executor, ThreadPoolExecutor)


class TestToneGeneration:
    def test_silence_tone(self):
        manager = SoundManager()
        tone = manager._generate_tone(0, 0.1)
        assert isinstance(tone, np.ndarray)

    def test_high_frequency_tone(self):
        manager = SoundManager()
        tone = manager._generate_tone(20000, 0.1)
        assert isinstance(tone, np.ndarray)

    def test_long_duration_tone(self):
        manager = SoundManager()
        tone = manager._generate_tone(440, 1.0)
        expected_samples = int(SoundManager.SAMPLE_RATE * 1.0)
        assert len(tone) == expected_samples

    def test_short_duration_tone(self):
        manager = SoundManager()
        tone = manager._generate_tone(440, 0.01)
        expected_samples = int(SoundManager.SAMPLE_RATE * 0.01)
        assert len(tone) == expected_samples

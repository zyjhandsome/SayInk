import numpy as np
from sayink.audio_utils import mix_to_mono, resample_mono, rms_volume, to_mono


class TestToMono:
    def test_1d_unchanged(self):
        a = np.array([0.1, -0.2], dtype=np.float32)
        assert to_mono(a).shape == (2,)

    def test_stereo_mean(self):
        a = np.array([[1.0, -1.0], [1.0, -1.0]], dtype=np.float32)
        mono = to_mono(a)
        assert mono.shape == (2,)
        assert abs(mono[0]) < 1e-6


class TestResample:
    def test_same_rate(self):
        a = np.ones(1600, dtype=np.float32)
        out = resample_mono(a, 16000, 16000)
        assert out.shape == a.shape

    def test_double_rate(self):
        a = np.ones(3200, dtype=np.float32)
        out = resample_mono(a, 32000, 16000)
        assert out.shape[0] == 1600


def _sine(freq: float, rate: int, seconds: float = 1.0) -> np.ndarray:
    t = np.arange(int(rate * seconds)) / rate
    return (0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x[400:-400] ** 2)))


class TestAntiAliasing:
    """48 kHz → 16 kHz used to fold sibilant hiss (8–24 kHz) into the speech band."""

    def test_content_above_the_new_nyquist_is_removed(self):
        for freq in (10000, 12000, 20000):
            out = resample_mono(_sine(freq, 48000), 48000, 16000)
            assert _rms(out) < 0.01 * 0.3

    def test_speech_band_passes_unchanged(self):
        for freq, tolerance in ((300, 0.03), (1000, 0.03), (4000, 0.03), (6000, 0.10)):
            out = resample_mono(_sine(freq, 48000), 48000, 16000)
            assert abs(_rms(out) / (0.3 / np.sqrt(2)) - 1) < tolerance

    def test_44k1_input_is_filtered_too(self):
        out = resample_mono(_sine(11000, 44100), 44100, 16000)
        assert _rms(out) < 0.01 * 0.3


class TestStreamResampler:
    """The recorder resamples each 100 ms tick; blocks must join seamlessly."""

    def test_blockwise_output_matches_one_pass(self):
        from sayink.audio_utils import StreamResampler

        sig = _sine(440, 48000, 2.0) + _sine(5000, 48000, 2.0)
        whole = StreamResampler(48000).process(sig)
        chunked = StreamResampler(48000)
        sizes = [4800, 4801, 333, 9600, 1, 7]
        parts, start = [], 0
        while start < sig.size:
            size = sizes[len(parts) % len(sizes)]
            parts.append(chunked.process(sig[start:start + size]))
            start += size
        joined = np.concatenate(parts)
        assert joined.size == whole.size
        assert np.allclose(joined, whole, atol=1e-5)

    def test_length_tracks_the_rate_ratio_without_drift(self):
        from sayink.audio_utils import StreamResampler

        r = StreamResampler(44100)
        total = sum(r.process(np.zeros(4410, dtype=np.float32)).size for _ in range(600))
        assert abs(total - 16000 * 60) <= 1

    def test_stream_filters_aliasing_as_well(self):
        from sayink.audio_utils import StreamResampler

        r = StreamResampler(48000)
        sig = _sine(12000, 48000)
        out = np.concatenate([r.process(sig[i:i + 4800]) for i in range(0, sig.size, 4800)])
        assert _rms(out) < 0.01 * 0.3

    def test_same_rate_is_passthrough(self):
        from sayink.audio_utils import StreamResampler

        sig = _sine(440, 16000)
        assert np.array_equal(StreamResampler(16000).process(sig), sig)


class TestMix:
    def test_mix_two_equal(self):
        a = np.ones(1600, dtype=np.float32) * 0.5
        b = np.ones(1600, dtype=np.float32) * 0.5
        m = mix_to_mono([a, b], 16000)
        assert m.shape[0] == 1600
        assert m[0] > 0.4

    def test_mix_empty(self):
        assert mix_to_mono([], 16000).size == 0


class TestRms:
    def test_silent(self):
        assert rms_volume(np.zeros(100, dtype=np.float32)) == 0.0

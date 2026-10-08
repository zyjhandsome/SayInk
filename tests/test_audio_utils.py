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

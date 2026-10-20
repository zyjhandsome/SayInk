"""Conservative duplicate playback removal before microphone/loopback mixing.

This only handles an almost identical, delayed waveform. It does not claim
acoustic echo cancellation for reverberation or separation during double talk.
Uncorrelated microphone audio is returned unchanged.
"""
from __future__ import annotations

import numpy as np

from sayink.audio_utils import TARGET_SAMPLE_RATE


class DuplicateLaneSuppressor:
    MAX_DELAY = TARGET_SAMPLE_RATE // 4
    PROBE = int(0.4 * TARGET_SAMPLE_RATE)
    MIN_PROBE = TARGET_SAMPLE_RATE // 4
    BLOCK = TARGET_SAMPLE_RATE // 10
    MIN_CORRELATION = 0.985

    def __init__(self):
        self.reset()

    def reset(self):
        self._mic = np.zeros(0, dtype=np.float32)
        self._system = np.zeros(0, dtype=np.float32)
        self.suppressed_samples = 0

    @staticmethod
    def _fit(reference, target):
        x = reference.astype(np.float64) - np.mean(reference)
        y = target.astype(np.float64) - np.mean(target)
        xx, yy = float(x @ x), float(y @ y)
        if min(xx, yy) <= len(x) * 0.004 ** 2:
            return None
        xy = float(x @ y)
        return xy / np.sqrt(xx * yy), xy / xx

    def _estimate(self):
        size = min(self.PROBE, len(self._mic))
        if size < self.MIN_PROBE:
            return None
        delay = min(self.MAX_DELAY, len(self._system) - size)
        # FFT correlation keeps the exact capture sample grid (decimation
        # would miss odd-sample delays). Normalize each candidate's variance.
        target = self._mic[-size:].astype(np.float64)
        target -= target.mean()
        energy = float(target @ target)
        if energy < len(target) * 0.004 ** 2:
            return None
        reference = self._system[-size - delay:].astype(np.float64)
        count = len(target)
        sums = np.r_[0., np.cumsum(reference)]
        squares = np.r_[0., np.cumsum(reference * reference)]
        variance = squares[count:] - squares[:-count] - (sums[count:] - sums[:-count]) ** 2 / count
        fft_size = 1 << (len(reference) + count - 2).bit_length()
        convolution = np.fft.irfft(np.fft.rfft(reference, fft_size) * np.fft.rfft(target[::-1], fft_size), fft_size)
        dots = convolution[count - 1:len(reference)]
        correlations = np.abs(dots) / np.sqrt(np.maximum(variance * energy, 1e-20))
        correlations[variance < count * 0.004 ** 2] = 0.0
        best = int(np.argmax(correlations))
        if correlations[best] < self.MIN_CORRELATION:
            return None
        lag = len(dots) - 1 - best
        stop = len(self._system) - lag
        fit = self._fit(self._system[stop - size:stop], self._mic[-size:])
        if fit is None:
            return None
        correlation, gain = fit
        if abs(correlation) < self.MIN_CORRELATION or not 0.02 <= abs(gain) <= 4.0:
            return None
        return lag, gain

    def _process_block(self, microphone, system):
        bound = self.PROBE + self.MAX_DELAY
        self._mic = np.concatenate((self._mic, microphone))[-bound:]
        self._system = np.concatenate((self._system, system))[-bound:]
        estimate = self._estimate()
        if estimate is None:
            return microphone
        lag, gain = estimate
        stop = len(self._system) - lag
        reference = self._system[stop - len(microphone):stop]
        fit = self._fit(reference, microphone)
        # Recheck the newest audio: a local speaker can enter after a long
        # stretch of echo. Historical correlation must not approve this block.
        if fit is None or abs(fit[0]) < self.MIN_CORRELATION or abs(fit[1] - gain) > 0.15 * abs(gain):
            return microphone
        self.suppressed_samples += len(microphone)
        return (microphone - gain * reference).astype(np.float32)

    def process(self, microphone, system):
        mic = np.asarray(microphone, dtype=np.float32).reshape(-1)
        playback = np.asarray(system, dtype=np.float32).reshape(-1)
        if len(mic) != len(playback) or not np.all(np.isfinite(mic)) or not np.all(np.isfinite(playback)):
            self.reset()
            return mic
        result = []
        for start in range(0, len(mic), self.BLOCK):
            result.append(self._process_block(mic[start:start + self.BLOCK], playback[start:start + self.BLOCK]))
        return np.concatenate(result) if result else mic

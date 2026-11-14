"""Conservative duplicate & speakerphone playback removal before mic/loopback mixing.

Handles both pure delayed digital bleed and short-path acoustic speakerphone
coloration/reflections across extended Bluetooth buffer delays (up to 600 ms),
while immediately passing through uncorrelated local speech or double-talk
bit-exact without harming user voice.
"""
from __future__ import annotations

import numpy as np

from sayink.audio_utils import TARGET_SAMPLE_RATE


class DuplicateLaneSuppressor:
    # Up to 600 ms covers Bluetooth A2DP/HFP + WASAPI loopback + USB mic chains.
    MAX_DELAY = int(0.60 * TARGET_SAMPLE_RATE)
    PROBE = int(0.4 * TARGET_SAMPLE_RATE)
    MIN_PROBE = TARGET_SAMPLE_RATE // 4
    BLOCK = TARGET_SAMPLE_RATE // 10
    MIN_CORRELATION = 0.985
    # Short multi-tap FIR fallback for laptop speakerphone acoustic echo
    # (speaker EQ + desk reflection lowers 1-tap |r| below 0.985).
    FIR_TAPS = 28
    FIR_PRE_SAMPLES = 4
    ACOUSTIC_PEAK_CORRELATION = 0.52
    ACOUSTIC_MIN_CORRELATION = 0.88
    ACOUSTIC_BLOCK_CORRELATION = 0.88

    _BOUND = PROBE + MAX_DELAY

    def __init__(self):
        self.reset()

    def reset(self):
        self._mic_buf = np.zeros(self._BOUND, dtype=np.float32)
        self._mic_len = 0
        self._sys_buf = np.zeros(self._BOUND, dtype=np.float32)
        self._sys_len = 0
        self.suppressed_samples = 0

    @property
    def _mic(self) -> np.ndarray:
        return self._mic_buf[self._BOUND - self._mic_len :]

    @property
    def _system(self) -> np.ndarray:
        return self._sys_buf[self._BOUND - self._sys_len :]

    @staticmethod
    def _append_ring(buf: np.ndarray, cur_len: int, chunk: np.ndarray) -> int:
        cap = len(buf)
        n = len(chunk)
        if n <= 0:
            return cur_len
        if n >= cap:
            buf[:] = chunk[-cap:]
            return cap
        keep = min(cur_len, cap - n)
        if keep > 0:
            buf[cap - n - keep : cap - n] = buf[cap - keep : cap]
        buf[cap - n : cap] = chunk
        return keep + n

    @staticmethod
    def _fit(reference, target):
        x = reference.astype(np.float64) - np.mean(reference)
        y = target.astype(np.float64) - np.mean(target)
        xx, yy = float(x @ x), float(y @ y)
        if min(xx, yy) <= len(x) * 0.004 ** 2:
            return None
        xy = float(x @ y)
        return xy / np.sqrt(xx * yy), xy / xx

    @classmethod
    def _build_fir_matrix(cls, system_buf: np.ndarray, start: int, length: int) -> np.ndarray | None:
        taps = cls.FIR_TAPS
        # Row i predicts mic sample at (start + i) using system[start + i - k].
        if length <= 0 or start - (taps - 1) < 0 or start + length > len(system_buf):
            return None
        window_src = system_buf[start - (taps - 1) : start + length].astype(np.float64)
        return np.lib.stride_tricks.sliding_window_view(window_src, taps)[:, ::-1]

    def _fit_acoustic_fir(self, base_lag: int, size: int) -> tuple[int, np.ndarray, float] | None:
        # Center the causal FIR slightly ahead of the peak lag so direct path +
        # 1.5 ms of speaker coloration / desk bounce are captured together.
        anchor_lag = max(0, base_lag - self.FIR_PRE_SAMPLES)
        stop = len(self._system) - anchor_lag
        start = stop - size
        X = self._build_fir_matrix(self._system, start, size)
        if X is None:
            return None
        y = self._mic[-size:].astype(np.float64)
        y_centered = y - np.mean(y)
        yy = float(y_centered @ y_centered)
        if yy <= size * 0.004 ** 2:
            return None
        X_mean = np.mean(X, axis=0)
        Xc = X - X_mean
        gram = Xc.T @ Xc
        energy_diag = float(np.trace(gram)) / max(1, self.FIR_TAPS)
        if energy_diag <= size * 0.004 ** 2:
            return None
        reg = max(1e-8, 1e-3 * energy_diag)
        weights = np.linalg.solve(gram + reg * np.eye(self.FIR_TAPS), Xc.T @ y_centered)
        gain_norm = float(np.linalg.norm(weights))
        if not 0.02 <= gain_norm <= 4.5:
            return None
        pred = Xc @ weights
        fit = self._fit(pred, y_centered)
        if fit is None or fit[0] < self.ACOUSTIC_MIN_CORRELATION:
            return None
        bias = float(np.mean(y) - X_mean @ weights)
        return anchor_lag, weights, bias

    def _estimate(self):
        size = min(self.PROBE, len(self._mic))
        if size < self.MIN_PROBE or len(self._system) < size:
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
        peak_corr = float(correlations[best])
        lag = len(dots) - 1 - best

        if peak_corr >= self.MIN_CORRELATION:
            stop = len(self._system) - lag
            fit = self._fit(self._system[stop - size:stop], self._mic[-size:])
            if fit is not None:
                correlation, gain = fit
                if abs(correlation) >= self.MIN_CORRELATION and 0.02 <= abs(gain) <= 4.0:
                    return ("single", lag, gain)

        if peak_corr >= self.ACOUSTIC_PEAK_CORRELATION:
            fir = self._fit_acoustic_fir(lag, size)
            if fir is not None:
                anchor_lag, weights, bias = fir
                return ("fir", anchor_lag, weights, bias)
        return None

    def _process_block(self, microphone, system):
        if len(microphone) == 0:
            return microphone
        self._mic_len = self._append_ring(self._mic_buf, self._mic_len, microphone)
        self._sys_len = self._append_ring(self._sys_buf, self._sys_len, system)
        estimate = self._estimate()
        if estimate is None:
            return microphone
        if estimate[0] == "single":
            _, lag, gain = estimate
            stop = len(self._system) - lag
            reference = self._system[stop - len(microphone):stop]
            fit = self._fit(reference, microphone)
            # Recheck the newest audio: a local speaker can enter after a long
            # stretch of echo. Historical correlation must not approve this block.
            if fit is None or abs(fit[0]) < self.MIN_CORRELATION or abs(fit[1] - gain) > 0.15 * abs(gain):
                return microphone
            self.suppressed_samples += len(microphone)
            return (microphone - gain * reference).astype(np.float32)

        _, anchor_lag, weights, bias = estimate
        if len(microphone) < 64:
            return microphone
        stop = len(self._system) - anchor_lag
        start = stop - len(microphone)
        X = self._build_fir_matrix(self._system, start, len(microphone))
        if X is None:
            return microphone
        predicted = X @ weights + bias
        fit = self._fit(predicted, microphone)
        if fit is None or fit[0] < self.ACOUSTIC_BLOCK_CORRELATION or abs(fit[1] - 1.0) > 0.22:
            return microphone
        self.suppressed_samples += len(microphone)
        return (microphone.astype(np.float64) - predicted).astype(np.float32)

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

"""Do not trade local speech for duplicate playback suppression."""
import numpy as np
import pytest

from sayink.lane_echo import DuplicateLaneSuppressor

RATE = 16000


def noise(seed, seconds=2):
    return np.random.default_rng(seed).normal(0, 0.05, int(RATE * seconds)).astype(np.float32)


@pytest.mark.parametrize("delay,gain", [(0, 0.5), (799, 0.3), (4000, -0.8)])
def test_delayed_duplicate_is_removed_without_changing_length(delay, gain):
    system = noise(1)
    mic = np.pad(system, (delay, 0))[:len(system)] * gain
    suppressor = DuplicateLaneSuppressor()
    cleaned = suppressor.process(mic, system)
    assert len(cleaned) == len(mic)
    assert np.max(np.abs(cleaned[RATE:])) < 1e-6
    assert suppressor.suppressed_samples >= RATE
    assert len(suppressor._system) <= suppressor.PROBE + suppressor.MAX_DELAY


def test_independent_local_speech_is_bit_exact():
    mic, system = noise(2), noise(1)
    suppressor = DuplicateLaneSuppressor()
    assert np.array_equal(suppressor.process(mic, system), mic)
    assert suppressor.suppressed_samples == 0


def test_local_speaker_entering_during_echo_is_preserved_immediately():
    system, local = noise(1), noise(2)
    mic = system * 0.4
    mic[RATE:] += local[RATE:]
    cleaned = DuplicateLaneSuppressor().process(mic, system)
    assert np.max(np.abs(cleaned[RATE // 2:RATE])) < 1e-6
    assert np.array_equal(cleaned[RATE:], mic[RATE:])


def test_weak_local_voice_is_retained_in_the_residual():
    system, local = noise(1), noise(2) * 0.04
    mic = system * 0.4 + local
    cleaned = DuplicateLaneSuppressor().process(mic, system)
    assert np.corrcoef(cleaned[RATE:], local[RATE:])[0, 1] > 0.99
    assert np.std(cleaned[RATE:]) >= 0.95 * np.std(local[RATE:])


def test_silent_or_constant_tracks_do_not_establish_an_echo_path():
    suppressor = DuplicateLaneSuppressor()
    mic = np.full(RATE, 0.1, dtype=np.float32)
    assert np.array_equal(suppressor.process(mic, np.full(RATE, 0.2)), mic)
    assert np.array_equal(suppressor.process(mic, np.zeros(RATE)), mic)


def test_reset_drops_the_previous_session_and_irregular_chunks_are_supported():
    system = noise(1)
    mic = system * 0.4
    suppressor = DuplicateLaneSuppressor()
    pieces = [suppressor.process(mic[a:b], system[a:b])
              for a, b in [(0, 3199), (3199, 10123), (10123, len(mic))]]
    assert np.max(np.abs(np.concatenate(pieces)[RATE:])) < 1e-6
    suppressor.reset()
    assert len(suppressor._system) == 0 and suppressor.suppressed_samples == 0
    assert np.array_equal(suppressor.process(mic[:1600], system[:1600]), mic[:1600])

"""Speaker-change analysis stays inside the recognizer's cancellable worker."""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from sayink.speaker_turns import SpeakerTurn
from sayink.speech_recognizer import SpeechRecognizer, TranscribeWorker


class Decoder:
    def __init__(self):
        self.seen = []

    def create_stream(self):
        stream = SimpleNamespace(result=SimpleNamespace(text=""))
        stream.accept_waveform = lambda _rate, samples: self.seen.append(samples.copy())
        return stream

    def decode_stream(self, stream):
        stream.result.text = ["第一位发言", "第二位接话", "第一位继续"][len(self.seen) - 1]


def turns(audio, **kwargs):
    return [SpeakerTurn(i * 16000, (i + 1) * 16000, np.eye(3, dtype=np.float32)[i % 2], True)
            for i in range(3)]


def run(worker):
    final, batches, errors = [], [], []
    worker.result_ready.connect(final.append)
    worker.meeting_ready.connect(batches.append)
    worker.error.connect(errors.append)
    worker.run()
    return final, batches, errors


def test_each_turn_is_decoded_separately_and_returned_once_in_order():
    audio = np.linspace(0.01, 0.3, 48000, dtype=np.float32)
    decoder = Decoder()
    worker = TranscribeWorker(decoder, audio, turn_analyzer=turns)
    final, batches, errors = run(worker)
    assert final == [] and errors == [] and len(batches) == 1
    assert [t.text for t in batches[0]] == ["第一位发言", "第二位接话", "第一位继续"]
    assert [(t.start, t.stop) for t in batches[0]] == [(0, 16000), (16000, 32000), (32000, 48000)]
    assert np.array_equal(np.concatenate(decoder.seen), audio)


def test_speaker_failure_does_not_discard_or_shorten_audio_for_asr():
    def broken(audio, **kwargs):
        raise RuntimeError("model failed")

    decoder = Decoder()
    audio = np.ones(48000, dtype=np.float32) * 0.1
    final, batches, errors = run(TranscribeWorker(decoder, audio, turn_analyzer=broken))
    assert final == errors == [] and len(batches[0]) == 1
    assert batches[0][0].text == "第一位发言"
    assert not batches[0][0].learnable and not np.any(batches[0][0].embedding)
    assert np.array_equal(decoder.seen[0], audio)


def test_gaps_and_overlaps_from_analyzer_fall_back_to_full_audio():
    for invalid in ([SpeakerTurn(100, 48000, np.ones(2), True)],
                    [SpeakerTurn(0, 32000, np.ones(2), True), SpeakerTurn(30000, 48000, np.ones(2), True)],
                    [], ["malformed"], None):
        decoder = Decoder()
        _, batches, errors = run(TranscribeWorker(
            decoder, np.ones(48000, dtype=np.float32) * 0.1,
            turn_analyzer=lambda audio, invalid=invalid, **kwargs: invalid,
        ))
        assert errors == [] and len(batches[0]) == 1
        assert len(decoder.seen[0]) == 48000 and not batches[0][0].learnable


def test_cancellation_during_analysis_emits_nothing_and_does_not_start_asr():
    decoder = Decoder()
    worker = TranscribeWorker(decoder, np.ones(48000, dtype=np.float32))

    def cancel(audio, **kwargs):
        worker.cancel()
        return []

    worker._turn_analyzer = cancel
    assert run(worker) == ([], [], [])
    assert decoder.seen == []


def test_cancellation_between_turns_does_not_emit_a_partial_saved_batch():
    decoder = Decoder()
    worker = TranscribeWorker(decoder, np.full(48000, 0.1, dtype=np.float32), turn_analyzer=turns)
    original = decoder.decode_stream

    def cancel_after_first(stream):
        original(stream)
        worker.cancel()

    decoder.decode_stream = cancel_after_first
    assert run(worker) == ([], [], [])
    assert len(decoder.seen) == 1


def test_late_batch_is_ignored_after_superseding_or_shutdown():
    recognizer = SpeechRecognizer()
    batches = []
    recognizer.meeting_result.connect(batches.append)
    recognizer._active_seq = 5
    recognizer._on_meeting_result(["old"], 4)
    assert batches == []
    recognizer._on_meeting_result(["current"], 5)
    assert batches == [["current"]]
    recognizer.shutdown(wait_ms=0)
    recognizer._on_meeting_result(["late"], 5)
    assert batches == [["current"]]


def test_analysis_runs_off_gui_thread_and_batch_is_delivered_on_gui_thread():
    import threading
    from PyQt6.QtCore import QEventLoop, QTimer

    gui_thread = threading.get_ident()
    analyzed_on, received_on, batches = [], [], []
    recognizer = SpeechRecognizer()
    recognizer._recognizer = Decoder()
    recognizer._model_id = "sensevoice"
    recognizer._is_ready = True
    loop = QEventLoop()

    def analyze(audio, **kwargs):
        analyzed_on.append(threading.get_ident())
        return turns(audio)

    def receive(result):
        received_on.append(threading.get_ident())
        batches.append(result)
        loop.quit()

    recognizer.meeting_result.connect(receive)
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    timer.start(5000)
    recognizer.transcribe_final(np.full(48000, 0.1, dtype=np.float32), turn_analyzer=analyze)
    loop.exec()
    timer.stop()
    recognizer.shutdown(wait_ms=2000)
    assert analyzed_on and analyzed_on[0] != gui_thread
    assert received_on == [gui_thread]
    assert len(batches) == 1 and len(batches[0]) == 3

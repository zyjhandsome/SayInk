"""Comprehensive regression suite for SayInk audio, Bluetooth loopback, VAD/ASR,
speaker diarization, and LLM polish/refiner/transport audit fixes.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import struct
import threading
import time
import wave
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from sayink import audio_devices
from sayink.asr_context import ContextAudio, decode_owned_context, with_context
from sayink.asr_correction import (
    apply_glossary_variants,
    glossary_conflicts,
    parse_glossary_line,
)
from sayink.audio_devices import (
    INPUT_SOURCE_MIXED,
    AudioDeviceInfo,
    StreamEndpoint,
    WindowsRenderEndpoints,
    build_recording_plan,
    describe_active_loopback_device,
    invalidate_device_cache,
    mark_portaudio_stream_active,
    preferred_windows_loopback_names,
    refresh_audio_devices,
)
from sayink.audio_file_decoder import iter_wav_pcm
from sayink.audio_recorder import _WORKING_STREAM_PARAMS, AudioRecorder, _CaptureLane
from sayink.audio_recovery import (
    INCOMPLETE_QUOTA_GRACE_SECONDS,
    INCOMPLETE_RETENTION_SECONDS,
    RecordingJournal,
    delete_session_recovery_files,
    recordings,
    store_pending,
)
from sayink.audio_utils import TARGET_SAMPLE_RATE, StreamResampler, rms_volume, to_mono
from sayink.aux_models import AuxModelDownloads, missing_aux_models
from sayink.lane_echo import DuplicateLaneSuppressor
from sayink.llm_transport import _stream_text
from sayink.media_foundation import (
    MF_SOURCE_READERF_ENDOFSTREAM,
    AudioFoundationError,
    _adapt_pcm_chunk,
    read_pcm16,
)
from sayink.media_import import recognize_file
from sayink.minutes_prompts import TranscriptLine
from sayink.pawp_capture import encode_pawp_device_index
from sayink.polish_fidelity import polish_rejection_reason
from sayink.speaker_clustering import TurnObservation, reassign_turns
from sayink.speaker_embedding import ModelEmbedder
from sayink.speaker_naming import infer_speaker_names
from sayink.speaker_session import SpeakerSession, normalize_far_field_voice, voice_embedding
from sayink.speaker_turns import SpeakerTurnDetector, analyze_turns
from sayink.speech_recognizer import (
    AUX_MODEL_SPEAKER_ACTIVITY,
    ModelDownloadWorker,
    TranscribeWorker,
    merge_slice_texts,
    normalize_asr_output,
    restore_glossary_case,
)
from sayink.text_polisher import (
    LLM_MODE_POLISH,
    PolishWorker,
    _collect_stream_text,
    is_local_endpoint,
    strip_model_thinking,
    strip_transcript_tags,
)
from sayink.transcript_refiner import (
    RefinementUnit,
    RefinementValidationError,
    validate_refinement,
)
from sayink.vad_segmenter import (
    SILERO_WINDOW,
    SileroGate,
    SpeechGate,
    SpeechSegmenter,
    attach_segment_acoustics,
)


class _DummyStream:
    def __init__(self, *, active: bool = True, stopped: bool = False):
        self.active = active
        self.stopped = stopped
        self.closed = False

    def start(self):
        self.active = True

    def stop(self):
        self.active = False
        self.stopped = True

    def close(self):
        self.closed = True


# ── 1. C-08 & H-30: Bluetooth Loopback, Hot-Plug, Lane Levels & Echo/Drift ───


class TestAudit01LoopbackBluetoothAndMix:
    def test_portaudio_refresh_respects_active_streams_and_reinitializes_when_idle(
        self, monkeypatch
    ):
        calls: list[str] = []
        monkeypatch.setattr(audio_devices.sd, "_terminate", lambda: calls.append("term"))
        monkeypatch.setattr(audio_devices.sd, "_initialize", lambda: calls.append("init"))
        monkeypatch.setattr(audio_devices, "_active_portaudio_streams", 0)

        mark_portaudio_stream_active(+1)
        try:
            assert refresh_audio_devices() is False
            assert calls == []
        finally:
            mark_portaudio_stream_active(-1)

        assert refresh_audio_devices(force=True) is True
        assert calls == ["term", "init"]

    def test_bluetooth_communications_endpoint_and_describe_active_loopback(self, monkeypatch):
        invalidate_device_cache()
        bt_endpoints = WindowsRenderEndpoints(
            console_name="耳机 (AirPods Pro Stereo)",
            console_peak=0.0,
            communications_name="耳机 (AirPods Pro Hands-Free AG Audio)",
            communications_peak=0.08,
            active_render_name="耳机 (AirPods Pro Hands-Free AG Audio)",
            active_render_peak=0.08,
        )
        monkeypatch.setattr(audio_devices, "_cached_windows_render_endpoints", lambda: bt_endpoints)
        names = preferred_windows_loopback_names(
            active_mic_name="麦克风 (AirPods Pro Hands-Free AG Audio)"
        )
        assert names[0] == "耳机 (AirPods Pro Hands-Free AG Audio)"

        pawp_stereo = AudioDeviceInfo(
            encode_pawp_device_index(1),
            "耳机 (AirPods Pro Stereo) [Loopback]",
            "PyAudioWPatch (WASAPI 环回)",
            48000,
            2,
            True,
            False,
        )
        pawp_hfp = AudioDeviceInfo(
            encode_pawp_device_index(2),
            "耳机 (AirPods Pro Hands-Free AG Audio) [Loopback]",
            "PyAudioWPatch (WASAPI 环回)",
            16000,
            1,
            True,
            False,
        )
        mic_hfp = AudioDeviceInfo(
            0,
            "麦克风 (AirPods Pro Hands-Free AG Audio)",
            "Windows WASAPI",
            16000,
            1,
            False,
            False,
        )
        monkeypatch.setattr(audio_devices, "list_microphone_devices", lambda: [mic_hfp])
        monkeypatch.setattr(audio_devices, "pick_default_microphone", lambda: mic_hfp)
        monkeypatch.setattr(
            audio_devices, "list_system_capture_devices", lambda: [pawp_stereo, pawp_hfp]
        )
        monkeypatch.setattr(audio_devices, "_cached_pawp_loopback_devices", lambda: [pawp_stereo, pawp_hfp])
        monkeypatch.setattr(audio_devices, "_supports_wasapi_loopback_flag", lambda: False)
        monkeypatch.setattr(audio_devices.sys, "platform", "win32")

        resolved_label = describe_active_loopback_device(-1, -1)
        assert resolved_label == "耳机 (AirPods Pro Hands-Free AG Audio)"

    def test_system_lane_lost_detection_and_self_healing(self, monkeypatch):
        rec = AudioRecorder()
        old_sys = AudioDeviceInfo(10, "耳机 (BT Stereo) [Loopback]", "WASAPI", 16000, 1, True, False)
        new_sys = AudioDeviceInfo(11, "耳机 (BT Hands-Free AG Audio) [Loopback]", "WASAPI", 16000, 1, True, False)
        dead_lane = _CaptureLane(StreamEndpoint("system", old_sys, False))
        dead_lane.stream_failed = True

        rec._lanes = [dead_lane]
        rec._is_recording = True
        rec._continuous_mode = True
        rec._route_watch_enabled = True

        assert rec._lost_lane() is dead_lane

        def _fake_open(lane: _CaptureLane):
            lane.stream = _DummyStream(active=True)
            lane.sample_rate = 16000
            lane.stream_failed = False

        monkeypatch.setattr(rec, "_open_lane_stream", _fake_open)
        monkeypatch.setattr(
            "sayink.audio_recorder.pick_default_system_capture",
            lambda **_kw: new_sys,
        )
        monkeypatch.setattr("sayink.audio_recorder.ordered_system_devices", lambda _idx=-1: [new_sys])

        warnings: list[str] = []
        rec.warning.connect(warnings.append)
        rec._handle_lost_device(dead_lane)

        assert rec.is_recording is True
        assert len(rec._lanes) == 1
        assert rec._lanes[0].endpoint.device.name == new_sys.name
        assert any("Hands-Free AG Audio" in w for w in warnings)

    def test_mixed_mode_lane_levels_and_system_silent_warning_not_masked_by_mic(self):
        rec = AudioRecorder()
        rec.configure(input_source=INPUT_SOURCE_MIXED)
        mic_dev = AudioDeviceInfo(0, "USB Mic", "WASAPI", 16000, 1, False, False)
        sys_dev = AudioDeviceInfo(1, "BT Stereo [Loopback]", "WASAPI", 16000, 1, True, False)
        mic_lane = _CaptureLane(StreamEndpoint("microphone", mic_dev, False))
        sys_lane = _CaptureLane(StreamEndpoint("system", sys_dev, False))
        # User speaks loudly on mic, while system channel delivers near-zero silence.
        t = np.arange(1600, dtype=np.float32) / 16000.0
        mic_lane.chunks = [0.15 * np.sin(2.0 * np.pi * 220.0 * t).astype(np.float32)]
        sys_lane.chunks = [np.zeros(1600, dtype=np.float32)]

        rec._lanes = [mic_lane, sys_lane]
        rec._is_recording = True
        rec._continuous_mode = True
        rec._mixed_unpaused_since = time.monotonic() - 16.0

        level_events: list[tuple[float, float]] = []
        silent_warnings: list[str] = []
        rec.lane_levels_changed.connect(lambda m, s: level_events.append((m, s)))
        rec.system_silent_warning.connect(silent_warnings.append)

        # While paused, 15s silent warning must not fire.
        rec.set_paused(True)
        rec._mixed_elapsed_before_pause = 5.0
        rec._poll_system_route_and_silence()
        assert silent_warnings == []

        # Unpause past 15s total elapsed: mic speech must NOT mask dead system audio.
        rec.set_paused(False)
        rec._mixed_elapsed_before_pause = 15.5
        rec._on_continuous_tick()

        mic_lvl, sys_lvl = rec.lane_levels
        assert mic_lvl > 0.05
        assert sys_lvl == pytest.approx(0.0, abs=1e-6)
        assert level_events and level_events[-1][0] > 0.05
        assert len(silent_warnings) == 1
        assert "BT Stereo" in silent_warnings[0]

    def test_7_1_multichannel_to_mono_does_not_attenuate_12db(self):
        frames = 1600
        ch8 = np.zeros((frames, 8), dtype=np.float32)
        t = np.arange(frames, dtype=np.float32) / 16000.0
        voice = (0.2 * np.sin(2.0 * np.pi * 300.0 * t)).astype(np.float32)
        # Front L + Front R carry stereo speech; 6 surround channels are silent.
        ch8[:, 0] = voice
        ch8[:, 1] = voice
        mono = to_mono(ch8)
        assert rms_volume(mono) == pytest.approx(rms_volume(voice), rel=0.05)

    def test_clock_drift_compensation_absorbs_sustained_excess_without_jitter_distortion(self):
        rec = AudioRecorder()
        mic_dev = AudioDeviceInfo(0, "Mic", "WASAPI", 16000, 1, False, False)
        sys_dev = AudioDeviceInfo(1, "Sys", "WASAPI", 16000, 1, True, False)
        mic_lane = _CaptureLane(StreamEndpoint("microphone", mic_dev, False))
        sys_lane = _CaptureLane(StreamEndpoint("system", sys_dev, False))
        rec._lanes = [mic_lane, sys_lane]

        # Single-tick 1-block phase jitter stays bit-exact without warping.
        mic_lane.chunks = [np.ones(3200, dtype=np.float32) * 0.1]
        sys_lane.chunks = [np.ones(1600, dtype=np.float32) * 0.1]
        now = time.monotonic()
        mic_lane.last_chunk_at = now
        sys_lane.last_chunk_at = now
        out1 = rec._drain_mixed_mono()
        assert out1 is not None and out1.size == 1600
        assert mic_lane.pending.size == 1600

        # Sustained drift (+3 samples/tick) over >= DRIFT_SUSTAIN_TICKS gently absorbs
        # excess samples back down toward DRIFT_TOLERANCE_SAMPLES (~320).
        for _ in range(25):
            now = time.monotonic()
            mic_lane.chunks.append(np.ones(1603, dtype=np.float32) * 0.1)
            sys_lane.chunks.append(np.ones(1600, dtype=np.float32) * 0.1)
            mic_lane.last_chunk_at = now
            sys_lane.last_chunk_at = now
            mixed = rec._drain_mixed_mono()
            assert mixed is not None and mixed.size == 1600

        # Without drift absorption pending would be 1600 + 25 * 3 = 1675; with warp it shrank!
        assert mic_lane.pending.size < 1560

    def test_duplicate_lane_suppressor_catches_bluetooth_delayed_acoustic_echo_only(self):
        sup = DuplicateLaneSuppressor()
        rng = np.random.default_rng(42)
        # Broadband modulated speech-like signal on system playback
        t = np.arange(24000, dtype=np.float32) / 16000.0
        env = (0.55 + 0.45 * np.sin(2.0 * np.pi * 3.5 * t)).astype(np.float32)
        broadband = rng.standard_normal(24000).astype(np.float32)
        sys_sig = (0.12 * env * broadband).astype(np.float32)

        # Multi-path speakerphone acoustic echo delayed by 380 ms (> old 250 ms limit!)
        # with speaker coloration + desk bounce so 1-tap |r| < 0.85 (< 0.985)
        lag = int(0.38 * 16000)
        mic_echo = np.zeros_like(sys_sig)
        mic_echo[lag:] += 0.36 * sys_sig[:-lag]
        mic_echo[lag + 6 :] -= 0.24 * sys_sig[: -(lag + 6)]
        mic_echo[lag + 14 :] += 0.15 * sys_sig[: -(lag + 14)]

        cleaned_blocks = []
        for idx in range(0, 24000, 1600):
            cleaned_blocks.append(sup.process(mic_echo[idx : idx + 1600], sys_sig[idx : idx + 1600]))
        cleaned = np.concatenate(cleaned_blocks)
        assert rms_volume(cleaned[16000:]) < rms_volume(mic_echo[16000:]) * 0.05

        # Uncorrelated human speech on mic must NOT be suppressed.
        sup.reset()
        human_mic = (0.12 * rng.standard_normal(3200)).astype(np.float32)
        kept = sup.process(human_mic, sys_sig[:3200])
        assert np.allclose(kept, human_mic)


# ── 2. H-14: cancel() Continuous Flush Guard & PAWP Lock Serialization ────────


class TestAudit02CancelAndPawpClose:
    def test_cancel_in_continuous_mode_never_flushes_buffered_audio(self):
        rec = AudioRecorder()
        dev = AudioDeviceInfo(0, "Mic", "WASAPI", 16000, 1, False, False)
        lane = _CaptureLane(StreamEndpoint("microphone", dev, False))
        t = np.arange(8000, dtype=np.float32) / 16000.0
        lane.chunks = [(0.2 * np.sin(2.0 * np.pi * 300.0 * t)).astype(np.float32)]
        rec._lanes = [lane]
        rec._is_recording = True
        rec._continuous_mode = True

        emitted: list[np.ndarray] = []
        rec.segment_ready.connect(emitted.append)
        rec.cancel()

        assert emitted == []
        assert rec.is_recording is False
        assert rec.is_continuous is False

    def test_close_lane_serializes_pawp_stream_with_pawp_lock(self):
        rec = AudioRecorder()
        dev = AudioDeviceInfo(encode_pawp_device_index(0), "Loopback", "PAWP", 48000, 2, True, False)
        lane = _CaptureLane(StreamEndpoint("system", dev, False))
        events: list[str] = []

        class _FakePawpStream:
            def stop_stream(self):
                assert lane.pawp_lock.locked()
                events.append("stop")

            def close(self):
                assert lane.pawp_lock.locked()
                events.append("close")

        stop_evt = threading.Event()
        lane.pawp_stop = stop_evt
        lane.pawp_stream = _FakePawpStream()
        done_thread = threading.Thread(target=lambda: None)
        done_thread.start()
        done_thread.join()
        lane.pawp_thread = done_thread

        rec._close_lane(lane)
        assert stop_evt.is_set()
        assert lane.pawp_stop is None
        assert events == ["stop", "close"]
        assert lane.pawp_stream is None


# ── 3. H-15, H-16 & H-56: Media Foundation, 24-bit/Float WAV & Progress ───────


class TestAudit03MediaFoundationWavAndImport:
    def test_mfstartup_failure_still_calls_couninitialize(self, monkeypatch, tmp_path):
        import sayink.media_foundation as mf_mod

        uninit_calls: list[int] = []

        class _FakeOle32:
            def CoInitializeEx(self, _reserved, _coinit):
                return 0  # S_OK -> own_com = True

            def CoUninitialize(self):
                uninit_calls.append(1)

        class _FakeMfplat:
            def MFStartup(self, _ver, _flags):
                return -0x7FFFBFFB  # E_FAIL (< 0)

            def MFShutdown(self):
                raise AssertionError("MFShutdown must not be called when MFStartup failed")

        monkeypatch.setattr(mf_mod, "_ensure_prototypes", lambda *_a: None)
        monkeypatch.setattr(
            mf_mod,
            "ctypes",
            SimpleNamespace(
                windll=SimpleNamespace(
                    ole32=_FakeOle32(),
                    mfplat=_FakeMfplat(),
                    mfreadwrite=SimpleNamespace(),
                ),
                byref=ctypes.byref,
                c_void_p=ctypes.c_void_p,
                c_ulong=ctypes.c_ulong,
                c_longlong=ctypes.c_longlong,
            ),
        )

        with pytest.raises(AudioFoundationError, match="启动媒体解码服务失败"):
            read_pcm16(tmp_path / "broken.mp4")
        assert uninit_calls == [1]

    def test_positive_initial_pts_and_midstream_format_adaptation_avoid_oom(self, monkeypatch, tmp_path):
        import sayink.media_foundation as mf_mod

        # 1. Verify _adapt_pcm_chunk dynamically converts stereo 48kHz -> mono 16kHz
        stereo_48k = np.tile(np.array([16000, -16000], dtype=np.int16), 480)
        adapted, frames = _adapt_pcm_chunk(stereo_48k, 48000, 2, 16000, 1)
        assert frames == pytest.approx(160, abs=2)
        assert adapted.size == frames

        # 2. Verify huge initial PTS (3600s = 3.6e10 100ns units) is anchored, not zero-padded to GBs!
        uninit_calls: list[int] = []

        class _FakeOle32:
            def CoInitializeEx(self, *_a):
                return 0

            def CoUninitialize(self):
                uninit_calls.append(1)

            def PropVariantClear(self, *_a):
                return 0

        class _FakeMfplat:
            def MFStartup(self, *_a):
                return 0

            def MFShutdown(self):
                return 0

            def MFCreateMediaType(self, mt_ptr):
                ctypes.cast(mt_ptr, ctypes.POINTER(ctypes.c_void_p))[0] = 0x1001
                return 0

        class _FakeMfReadWrite:
            def MFCreateSourceReaderFromURL(self, _url, _attr, rdr_ptr):
                ctypes.cast(rdr_ptr, ctypes.POINTER(ctypes.c_void_p))[0] = 0x2002
                return 0

        read_step = {"n": 0}

        def _fake_method(_obj, index, _restype, _argtypes):
            if index == 2:  # Release
                return lambda _o: 0
            if index in (21, 24, 7):  # SetUINT32 / SetGUID / SetCurrentMediaType
                return lambda *_a: 0
            if index == 6:  # GetCurrentMediaType
                def _get_mt(_o, _stream, out_ptr):
                    ctypes.cast(out_ptr, ctypes.POINTER(ctypes.c_void_p))[0] = 0x3003
                    return 0
                return _get_mt
            if index == 9:  # ReadSample
                def _read_sample(_o, _s, _ctrl, _actual, flags_p, ts_p, sample_p):
                    read_step["n"] += 1
                    if read_step["n"] == 1:
                        # 1 hour positive initial PTS!
                        ctypes.cast(flags_p, ctypes.POINTER(ctypes.c_ulong))[0] = 0
                        ctypes.cast(ts_p, ctypes.POINTER(ctypes.c_longlong))[0] = 36_000_000_000
                        ctypes.cast(sample_p, ctypes.POINTER(ctypes.c_void_p))[0] = 0x4004
                    else:
                        ctypes.cast(flags_p, ctypes.POINTER(ctypes.c_ulong))[0] = MF_SOURCE_READERF_ENDOFSTREAM
                        ctypes.cast(ts_p, ctypes.POINTER(ctypes.c_longlong))[0] = 36_001_000_000
                        ctypes.cast(sample_p, ctypes.POINTER(ctypes.c_void_p))[0] = 0
                    return 0
                return _read_sample
            raise AssertionError(f"Unexpected vtable index {index}")

        monkeypatch.setattr(mf_mod, "_ensure_prototypes", lambda *_a: None)
        monkeypatch.setattr(mf_mod, "_method", _fake_method)
        monkeypatch.setattr(mf_mod, "_format_of", lambda _mt: (16000, 1))
        monkeypatch.setattr(mf_mod, "_probe_duration_100ns", lambda *_a: 10_000_000)
        monkeypatch.setattr(mf_mod, "_copy_sample", lambda _s: np.ones(1600, dtype=np.int16) * 1000)
        monkeypatch.setattr(
            mf_mod.ctypes,
            "windll",
            SimpleNamespace(ole32=_FakeOle32(), mfplat=_FakeMfplat(), mfreadwrite=_FakeMfReadWrite()),
        )

        progress_log: list[tuple[int, str]] = []
        pcm, rate, ch = read_pcm16(
            tmp_path / "obs.mkv",
            progress_cb=lambda p, m: progress_log.append((p, m)),
        )
        assert rate == 16000 and ch == 1
        assert pcm.size == 1600  # Anchored! Zero GB padding!
        assert progress_log and progress_log[-1][0] == 100
        assert uninit_calls == [1]

    def test_ieee_float_wav_fallback_stateful_resampling_and_import_progress(
        self, monkeypatch, tmp_path
    ):
        # 1a) IEEE Float WAV (format tag = 3, rejected by stdlib wave.open) decodes cleanly
        float_wav = tmp_path / "float32.wav"
        floats = (0.25 * np.sin(2.0 * np.pi * 440.0 * np.arange(16000) / 16000.0)).astype("<f4")
        fmt_chunk = struct.pack("<HHIIHH", 3, 1, 16000, 64000, 4, 32)
        data_bytes = floats.tobytes()
        riff = (
            b"RIFF"
            + struct.pack("<I", 4 + (8 + len(fmt_chunk)) + (8 + len(data_bytes)))
            + b"WAVEfmt "
            + struct.pack("<I", len(fmt_chunk))
            + fmt_chunk
            + b"data"
            + struct.pack("<I", len(data_bytes))
            + data_bytes
        )
        float_wav.write_bytes(riff)
        decoded_float = np.concatenate(list(iter_wav_pcm(float_wav)))
        assert np.allclose(decoded_float, floats, atol=1e-5)

        # 1b) Compressed WAV (ADPCM format tag = 2) falls back to Media Foundation on Windows
        adpcm_wav = tmp_path / "adpcm.wav"
        adpcm_fmt = struct.pack("<HHIIHH", 2, 1, 16000, 8000, 256, 4)
        adpcm_riff = (
            b"RIFF"
            + struct.pack("<I", 4 + (8 + len(adpcm_fmt)) + (8 + 256))
            + b"WAVEfmt "
            + struct.pack("<I", len(adpcm_fmt))
            + adpcm_fmt
            + b"data"
            + struct.pack("<I", 256)
            + (b"\x00" * 256)
        )
        adpcm_wav.write_bytes(adpcm_riff)

        mf_called: list[Path] = []
        monkeypatch.setattr("sayink.audio_file_decoder.sys.platform", "win32")
        monkeypatch.setattr(
            "sayink.audio_file_decoder._read_media_foundation",
            lambda p, cancel=None, progress_cb=None: (mf_called.append(Path(p)) or floats, 16000),
        )
        decoded_blocks = list(iter_wav_pcm(adpcm_wav))
        assert mf_called == [adpcm_wav]
        assert np.allclose(np.concatenate(decoded_blocks), floats, atol=1e-4)

        # Stateful resampling across 1s WAV blocks at 44100 Hz matches continuous StreamResampler
        wav_44k = tmp_path / "sine44k.wav"
        n44 = int(44100 * 2.5)
        sine44 = (0.3 * np.sin(2.0 * np.pi * 330.0 * np.arange(n44) / 44100.0)).astype(np.float32)
        pcm16 = (sine44 * 32767.0).astype("<i2")
        with wave.open(str(wav_44k), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(44100)
            wf.writeframes(pcm16.tobytes())

        streamed = np.concatenate(list(iter_wav_pcm(wav_44k)))
        ref_rs = StreamResampler(44100, TARGET_SAMPLE_RATE, trim_delay=True)
        expected = np.concatenate(
            [ref_rs.process((pcm16.astype(np.float32) / 32768.0)), ref_rs.flush()]
        )
        assert streamed.size == expected.size
        assert np.max(np.abs(streamed - expected)) < 1e-5

        # media_import.recognize_file progress_cb contract
        class _StubRecognizer:
            def decode_meeting_blocking(self, audio, *, turn_analyzer=None, cancelled=None):
                from sayink.speaker_turns import RecognizedTurn

                return [RecognizedTurn(0, len(audio), "测试进度回调", np.zeros(1, np.float32), False)], ""

        progress_steps: list[tuple[int, str]] = []
        res = recognize_file(
            wav_44k,
            recognizer=_StubRecognizer(),
            speaker_path=None,
            vad="energy",
            vad_path=None,
            cancel=lambda: False,
            progress_cb=lambda pct, msg: progress_steps.append((pct, msg)),
        )
        assert not res.cancelled
        assert res.turns and res.turns[0].text == "测试进度回调"
        assert progress_steps[0][0] == 0
        assert progress_steps[-1][0] == 100


# ── 4. H-17: Audio Recovery Quota Eviction, Session Cleanup & Metadata ────────


class TestAudit04AudioRecovery:
    def test_stale_incomplete_eviction_and_delete_session_recovery_files(self, tmp_path):
        now = time.time()

        def _make_journal_dir(hex_name: str, *, complete: bool, age_sec: float, session_id: str, nbytes: int):
            d = tmp_path / hex_name
            d.mkdir()
            (d / "audio.f32").write_bytes(b"\x00" * nbytes)
            meta = {
                "sample_rate": TARGET_SAMPLE_RATE,
                "started_at": now - age_sec,
                "complete": complete,
                "session_id": session_id,
            }
            (d / "session.json").write_text(json.dumps(meta), encoding="utf-8")
            return d

        # 1) >30 days incomplete fragment -> expired immediately
        super_old = _make_journal_dir(
            "a" * 32,
            complete=False,
            age_sec=INCOMPLETE_RETENTION_SECONDS + 3600,
            session_id="old-crash",
            nbytes=4096,
        )
        # 2) 48h old incomplete fragment -> evicted when quota is full
        stale_crash = _make_journal_dir(
            "b" * 32,
            complete=False,
            age_sec=INCOMPLETE_QUOTA_GRACE_SECONDS + 3600,
            session_id="stale-crash",
            nbytes=8000,
        )
        # 3) Fresh (<24h) incomplete fragment -> protected!
        fresh_crash = _make_journal_dir(
            "c" * 32,
            complete=False,
            age_sec=60.0,
            session_id="keep-session",
            nbytes=2000,
        )

        journal = RecordingJournal(
            tmp_path,
            metadata={"session_id": "target-session"},
            max_bytes=6000,
        )
        journal.append(np.zeros(400, dtype=np.float32))
        assert journal.close()

        assert not super_old.exists()
        assert not stale_crash.exists()
        assert fresh_crash.exists()

        # delete_session_recovery_files deletes only target-session .f32 journals
        deleted = delete_session_recovery_files("target-session", base_dir=tmp_path)
        assert deleted >= 1
        assert not journal.path.exists()
        assert fresh_crash.exists()
        assert len(recordings(tmp_path)) == 1

    def test_store_pending_preserves_segmented_acoustics_and_avoids_rglob(
        self, monkeypatch, tmp_path
    ):
        raw = attach_segment_acoustics(
            np.linspace(-0.1, 0.1, 1600, dtype=np.float32),
            noise_floor=0.00042,
            quiet_level=0.75,
        )
        ctx_audio = with_context(raw, prefix=np.zeros(320, dtype=np.float32), suffix=None)
        pending_dir = tmp_path / "pending"
        spilled = store_pending(pending_dir, ctx_audio, recovery_root=tmp_path)

        # Second spill must use cached tree bytes rather than scanning Path.rglob on main thread
        monkeypatch.setattr(
            Path,
            "rglob",
            lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("rglob called on hot path")),
        )
        spilled2 = store_pending(pending_dir, raw, recovery_root=tmp_path)

        restored = spilled.load()
        assert getattr(restored, "noise_floor", None) == pytest.approx(0.00042)
        assert getattr(restored, "quiet_level", 0.0) == pytest.approx(0.75)
        assert isinstance(restored, ContextAudio)
        spilled.delete()
        spilled2.delete()


# ── 5. H-18: Cached Working Sample Rate & Snapshot Dedup on Hotkey Open ───────


class TestAudit05FastMicOpen:
    def test_working_stream_params_cache_skips_failed_rate_probes(self, monkeypatch):
        _WORKING_STREAM_PARAMS.clear()
        rec = AudioRecorder()
        dev = AudioDeviceInfo(3, "WASAPI Speaker", "Windows WASAPI", 48000, 2, True, True)
        monkeypatch.setattr(
            "sayink.audio_recorder._device_info_by_index",
            lambda _idx: {
                "name": "WASAPI Speaker",
                "max_input_channels": 0,
                "max_output_channels": 2,
                "default_samplerate": 44100,
            },
        )
        monkeypatch.setattr("sayink.audio_recorder.sd.WasapiSettings", lambda loopback=False: ("wasapi", loopback))

        tried_rates: list[int] = []

        def _fake_input_stream(*, samplerate, **_kwargs):
            tried_rates.append(int(samplerate))
            if int(samplerate) != 48000:
                raise RuntimeError("Invalid sample rate")
            return _DummyStream()

        monkeypatch.setattr("sayink.audio_recorder.sd.InputStream", _fake_input_stream)

        lane1 = _CaptureLane(StreamEndpoint("system", dev, True))
        rec._open_lane_stream(lane1)
        rec._close_lane(lane1)
        assert tried_rates == [44100, 48000]

        tried_rates.clear()
        lane2 = _CaptureLane(StreamEndpoint("system", dev, True))
        rec._open_lane_stream(lane2)
        rec._close_lane(lane2)
        # Cached 48000 Hz opens immediately on the very first probe!
        assert tried_rates == [48000]

    def test_build_recording_plan_queries_portaudio_devices_at_most_once(self, monkeypatch):
        invalidate_device_cache()
        query_count = {"n": 0}

        def _fake_query(index=None):
            query_count["n"] += 1
            devs = [
                {
                    "name": "USB Mic",
                    "hostapi": 0,
                    "max_input_channels": 1,
                    "max_output_channels": 0,
                    "default_samplerate": 48000,
                },
                {
                    "name": "Realtek Speakers",
                    "hostapi": 0,
                    "max_input_channels": 0,
                    "max_output_channels": 2,
                    "default_samplerate": 48000,
                },
            ]
            return devs[int(index)] if index is not None else devs

        monkeypatch.setattr(audio_devices.sd, "query_devices", _fake_query)
        monkeypatch.setattr(
            audio_devices.sd,
            "query_hostapis",
            lambda: [{"name": "Windows WASAPI"}],
        )
        monkeypatch.setattr(audio_devices.sd, "default", SimpleNamespace(device=[0, 1]))
        monkeypatch.setattr(audio_devices, "_supports_wasapi_loopback_flag", lambda: True)
        monkeypatch.setattr(
            audio_devices,
            "_cached_windows_render_endpoints",
            lambda: WindowsRenderEndpoints(console_name="Realtek Speakers", console_peak=0.05),
        )

        plan = build_recording_plan(INPUT_SOURCE_MIXED, -1, -1)
        assert len(plan.endpoints) == 2
        assert query_count["n"] == 1


# ── 6. H-19: VAD 15s Forced Cut Preserves LSTM State & Smooth Carry Gain ─────


class TestAudit06VadSegmenter:
    def test_forced_15s_cut_preserves_gate_state_while_silence_end_resets_gate(self):
        class _TrackingGate(SpeechGate):
            name = "silero"

            def __init__(self):
                self.verdict = True
                self.resets = 0

            def is_speech(self, block: np.ndarray, rms: float, *, in_speech: bool) -> bool:
                return self.verdict

            def reset(self) -> None:
                self.resets += 1

        gate = _TrackingGate()
        seg = SpeechSegmenter(
            max_speech_sec=1.0,
            silence_hold_sec=0.3,
            short_silence_hold_sec=0.3,
            min_speech_sec=0.1,
        )
        seg.set_gate(gate)
        gate.resets = 0

        speech_block = np.full(1600, 0.05, dtype=np.float32)
        emitted = None
        for _ in range(12):
            out = seg.feed(speech_block)
            if out is not None:
                emitted = out
                break
        assert emitted is not None
        assert seg.last_forced is True
        # Forced mid-speech cut MUST keep Silero LSTM hidden state alive!
        assert gate.resets == 0

        # Continuation speech followed by natural silence MUST reset gate state.
        seg.feed(speech_block)
        seg.feed(speech_block)
        gate.verdict = False
        silence = np.zeros(1600, dtype=np.float32)
        for _ in range(10):
            seg.feed(silence)
        assert gate.resets >= 1

    def test_silero_gate_keeps_unscaled_carry_and_ramps_gain_smoothly(self):
        gate = SileroGate.__new__(SileroGate)
        fed_chunks: list[np.ndarray] = []

        class _FakeVadModel:
            def is_speech(self, window_samples):
                fed_chunks.append(np.asarray(window_samples, dtype=np.float32).copy())
                return True

            def reset(self):
                pass

        gate._model = _FakeVadModel()
        gate._window = SILERO_WINDOW
        gate._carry = np.zeros(0, dtype=np.float32)
        gate._last_verdict = False
        gate.input_gain = 10.0
        gate._prev_gain = 1.0

        # Feed 700 constant samples (512 scored + 188 unscaled carry)
        b1 = np.full(700, 0.02, dtype=np.float32)
        assert gate.is_speech(b1, 0.02, in_speech=False) is True
        assert gate._carry.size == 700 - SILERO_WINDOW
        # Carry must remain unscaled (0.02, NOT 0.2)!
        assert np.allclose(gate._carry, 0.02)

        # First window was smoothly ramped from _prev_gain=1.0 toward target_gain=10.0
        w0 = fed_chunks[0]
        assert w0[0] == pytest.approx(0.02, rel=0.05)
        assert w0[-1] > 0.18


# ── 7. H-20: ContextAudio Slicing, CTC Slack & Empty Attribution Guard ────────


class TestAudit07AsrContext:
    def test_context_audio_slicing_adjusts_prefix_suffix_and_preserves_noise_floor(self):
        base = attach_segment_acoustics(
            np.arange(100, dtype=np.float32),
            noise_floor=0.0003,
            quiet_level=0.6,
        )
        wrapped = with_context(
            base,
            prefix=np.ones(20, dtype=np.float32),
            suffix=np.ones(15, dtype=np.float32),
        )
        assert wrapped.prefix.size == 20
        assert wrapped.suffix.size == 15

        head = wrapped[:40]
        assert head.prefix.size == 20
        assert head.suffix.size == 0
        assert getattr(head, "noise_floor", None) == pytest.approx(0.0003)

        tail = wrapped[50:]
        assert tail.prefix.size == 0
        assert tail.suffix.size == 15

        mid = wrapped[10:80]
        assert mid.prefix.size == 0
        assert mid.suffix.size == 0

    def test_ctc_boundary_slack_and_empty_owned_context_skips_double_decode(
        self, monkeypatch
    ):
        # ASCII subword pieces ("▁Py" + "thon") straddling owned_start=0.20s stay together,
        # and a trailing token slightly past window_dur within CTC_END_SLACK_SEC=0.03s is kept.
        base = np.zeros(16000, dtype=np.float32)
        ctx = with_context(
            base,
            prefix=np.zeros(3200, dtype=np.float32),  # owned_start = 0.20s
            suffix=None,  # owned_end = 1.20s, window_dur = 1.20s
        )

        class _FakeResult:
            text = "前 Python 好"
            tokens = ("前", "▁Py", "thon", "好")
            timestamps = (0.05, 0.20, 0.25, 1.22)  # 1.22s <= 1.20 + 0.03s slack!

        class _FakeRec:
            def create_stream(self):
                return SimpleNamespace(
                    accept_waveform=lambda *_a: None,
                    result=_FakeResult(),
                )

            def decode_stream(self, _s):
                pass

        kept = decode_owned_context(_FakeRec(), ctx, normalize_asr_output)
        assert kept == "Python好"

        # Also verify subword started in prefix (0.18s < 0.20s) keeps "thon" (0.23s) out of owned
        # so the word is NOT split into "thon" on the second segment!
        class _PrefixWordResult:
            text = "Python 好"
            tokens = ("▁Py", "thon", "好")
            timestamps = (0.18, 0.23, 0.50)

        class _PrefixWordRec:
            def create_stream(self):
                return SimpleNamespace(
                    accept_waveform=lambda *_a: None,
                    result=_PrefixWordResult(),
                )

            def decode_stream(self, _s):
                pass

        assert decode_owned_context(_PrefixWordRec(), ctx, normalize_asr_output) == "好"

        # When decode_owned_context returns "" (all speech belonged to neighbour flanks),
        # TranscribeWorker._decode_slice MUST NOT run a second full-audio decode!
        calls = {"count": 0}

        class _FlankOnlyRec:
            def create_stream(self):
                return SimpleNamespace(
                    accept_waveform=lambda *_a: None,
                    result=SimpleNamespace(text="邻居", tokens=["邻居"], timestamps=[0.05]),
                )

            def decode_stream(self, _s):
                pass

        monkeypatch.setattr(
            "sayink.speech_recognizer.decode_asr_slice",
            lambda *_a, **_kw: calls.__setitem__("count", calls["count"] + 1) or "邻居",
        )
        worker = TranscribeWorker(_FlankOnlyRec(), ctx)
        assert worker._decode_slice(ctx) == ""
        assert calls["count"] == 0


# ── 8. H-21: Glossary Identifier/Version Protection & Conflict Reporting ─────


class TestAudit08GlossaryBoundariesAndConflicts:
    def test_glossary_protects_code_identifiers_versions_and_everyday_phrases(self):
        glossary = [
            parse_glossary_line("API=api"),
            parse_glossary_line(".NET=net"),
            parse_glossary_line("V1=v1"),
            parse_glossary_line("通义=同意"),
        ]
        src = "请检查 api_key、user-api、my.net 和 v1.0.5；我不同意这个方案，但同意千问很强，单独调 api 可以。"
        step1 = restore_glossary_case(src, glossary)
        out = apply_glossary_variants(step1, glossary)

        assert "api_key" in out
        assert "user-api" in out
        assert "my.net" in out
        assert "v1.0.5" in out
        assert "我不同意这个方案" in out
        assert "通义千问很强" in out
        assert "单独调 API 可以" in out

    def test_glossary_conflicts_reports_and_logs_shadowed_variants(self, caplog):
        import sayink.asr_correction as corr_mod

        corr_mod._LOGGED_GLOSSARY_CONFLICTS.clear()
        glossary = [
            parse_glossary_line("新盘=训盘"),
            parse_glossary_line("训盘=巡盘"),  # '训盘' collides with term
            parse_glossary_line("甲词=同音"),
            parse_glossary_line("乙词=同音"),  # duplicate variant
            parse_glossary_line("测试=测"),    # 1-char CJK variant
        ]
        with caplog.at_level(logging.WARNING, logger="SayInk"):
            conflicts = glossary_conflicts(glossary)

        assert len(conflicts) == 3
        assert any("同名" in c for c in conflicts)
        assert any("同时指向" in c for c in conflicts)
        assert any("单字误写" in c for c in conflicts)
        assert any("词库冲突提示" in rec.message for rec in caplog.records)


# ── 9. H-22, H-23, M-11, M-12: Diarization, Route Isolation & Naming ─────────


class TestAudit09SpeakerDiarizationAndNaming:
    def test_far_field_quiet_voice_normalization_enables_embeddings_without_boosting_hum(self):
        t = np.arange(16000, dtype=np.float32) / 16000.0
        # Syllabic modulated quiet speech (peak ~0.0012, below _QUIET_RMS=0.003 before normalization)
        syllable_env = np.where((np.sin(2.0 * np.pi * 3.0 * t) > 0.0), 1.0, 0.08).astype(np.float32)
        quiet_speech = (0.0012 * syllable_env * np.sin(2.0 * np.pi * 210.0 * t)).astype(np.float32)

        lifted = normalize_far_field_voice(quiet_speech)
        assert rms_volume(lifted) >= 0.004
        mel_vec = voice_embedding(quiet_speech)
        assert mel_vec.size > 1 and float(np.linalg.norm(mel_vec)) == pytest.approx(1.0, abs=1e-4)

        # ModelEmbedder also lifts modulated quiet speech before checking _QUIET_RMS
        emb = ModelEmbedder.__new__(ModelEmbedder)
        emb.dim = 4

        class _FakeExtractor:
            dim = 4

            def create_stream(self):
                return SimpleNamespace(accept_waveform=lambda *_a: None, input_finished=lambda: None)

            def is_ready(self, _s):
                return True

            def compute(self, _s):
                return [1.0, 2.0, 3.0, 4.0]

        emb._extractor = _FakeExtractor()
        model_vec = emb(quiet_speech)
        assert float(np.linalg.norm(model_vec)) == pytest.approx(1.0, abs=1e-5)

        # Unmodulated steady hum stays untouched and yields zero vector
        steady_hum = (0.001 * np.sin(2.0 * np.pi * 60.0 * t)).astype(np.float32)
        assert float(np.linalg.norm(emb(steady_hum))) == 0.0

    def test_reassign_turns_promotes_recurring_subsecond_responder_and_merges_small_only(self):
        v_host = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        v_short = np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32)
        turns = [
            TurnObservation(0, 1, v_host, 2500, True),
            TurnObservation(1, 0, v_short + np.array([0.01, 0, 0, 0], np.float32), 650, False),
            TurnObservation(2, 1, v_host, 2400, True),
            TurnObservation(3, 0, v_short + np.array([0.0, 0.01, 0.01, 0], np.float32), 700, False),
            TurnObservation(4, 1, v_host, 2200, True),
            TurnObservation(5, 0, v_short + np.array([0.01, 0.0, -0.01, 0], np.float32), 680, False),
        ]
        mapping = reassign_turns(turns, threshold=0.65)
        assert mapping[0] == mapping[2] == mapping[4] == 1
        assert mapping[1] == mapping[3] == mapping[5] == 2

        # Small-only meeting (`not big`) merges same-voice splinter turns into one speaker
        small_only = [
            TurnObservation(10, 1, np.array([0.99, 0.14, 0.0, 0.0], np.float32), 900, True),
            TurnObservation(11, 2, np.array([0.97, 0.24, 0.0, 0.0], np.float32), 900, True),
        ]
        merged_small = reassign_turns(small_only, threshold=0.80)
        assert merged_small[10] == merged_small[11]

    def test_speaker_session_zero_vector_respects_route_and_turn_cancel_is_quiet(self, caplog):
        session = SpeakerSession(threshold=0.7)
        mic_vec = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        spk_mic = session.assign(mic_vec, route="mic", start_ms=100, end_ms=1500)
        assert spk_mic == 1

        # Zero-vector on 'system' route MUST NOT steal 'mic' speaker 1!
        spk_sys_zero = session.assign(
            np.zeros(3, dtype=np.float32), route="system", start_ms=1600, end_ms=2100
        )
        assert spk_sys_zero == 0

        # InterruptedError during SpeakerTurnDetector embedding is treated as clean cancel without warning
        class _InterruptingEmbedder:
            supports_turn_detection = True
            threshold = 0.5
            activity_guard = None
            activity_mode = "off"

            def __call__(self, _audio):
                raise InterruptedError("cancelled")

        detector = SpeakerTurnDetector(_InterruptingEmbedder())
        with caplog.at_level(logging.WARNING, logger="SayInk"):
            res = analyze_turns(np.ones(16000, dtype=np.float32), detector)
        assert res == []
        assert all("失败" not in r.message for r in caplog.records)

    def test_speaker_naming_rejects_predicates_and_accepts_uncommon_surnames(self):
        lines = [
            TranscriptLine(offset_ms=0, speaker_id=1, text="大家好，我是明白的，这个方案我是全职跟进。"),
            TranscriptLine(offset_ms=1000, speaker_id=2, text="大家好，我是涂磊。"),
            TranscriptLine(offset_ms=2000, speaker_id=3, text="你好，我叫覃海洋。"),
            TranscriptLine(offset_ms=3000, speaker_id=4, text="我是邝美云。"),
            TranscriptLine(offset_ms=4000, speaker_id=5, text="我叫冼星海。"),
        ]
        names = infer_speaker_names(lines)
        assert 1 not in names
        assert names[2] == "涂磊"
        assert names[3] == "覃海洋"
        assert names[4] == "邝美云"
        assert names[5] == "冼星海"


# ── 10. H-24, H-25, H-26, H-27: Polish XML, Idioms, Refiner & Transport ──────


class TestAudit10PolishFidelityRefinerAndTransport:
    def test_polish_worker_xml_tags_thinking_disabled_and_orphan_think_stripping(
        self, monkeypatch
    ):
        captured: dict = {}

        def _fake_stream_chat(client, httpx_mod, **kwargs):
            captured.update(kwargs)
            return "<transcript>这个问题怎么解决？</transcript>"

        monkeypatch.setattr("sayink.text_polisher.stream_chat_completion", _fake_stream_chat)
        worker = PolishWorker(
            api_url="https://api.deepseek.com/v1",
            api_key="sk-test",
            model_name="deepseek-flash",
            text="嗯这个问题怎么解决",
            mode=LLM_MODE_POLISH,
        )
        results: list[str] = []
        worker.result_ready.connect(results.append)
        worker.run()

        user_msg = captured["messages"][-1]["content"]
        assert user_msg.startswith("<transcript>\n") and user_msg.endswith("\n</transcript>")
        assert captured["extra_payload"].get("thinking") == {"type": "disabled"}
        assert results == ["这个问题怎么解决？"]

        # Orphan leading ...</think> without opening <think> is stripped cleanly
        assert strip_model_thinking("先分析一下用户意图</think>\n保留的正文。") == "保留的正文。"
        assert strip_transcript_tags("<transcript>\n干净文本\n</transcript>") == "干净文本"

    def test_polish_fidelity_catches_short_answers_and_allows_idioms_and_a_not_a(self):
        # Short chatbot answer to spoken question -> rejected!
        assert polish_rejection_reason("这个问题怎么解决", "可以先重启试试") == "疑似应答而非编辑"
        assert polish_rejection_reason("预算批了吗？", "根据您的情况建议稍后确认。") == "疑似应答而非编辑"

        # Lexicalized Chinese number idioms / vague quantifiers -> accepted!
        assert polish_rejection_reason("十分感谢大家的支持", "非常感谢大家的支持。") == ""
        assert polish_rejection_reason("现场搞得乱七八糟的", "现场搞得很乱。") == ""
        assert polish_rejection_reason("我简单说一两句吧", "我简单说几句。") == ""
        assert polish_rejection_reason("十之八九是网络问题", "大概率是网络问题。") == ""

        # A-not-A questions & polite phrases -> accepted!
        assert polish_rejection_reason("你今天是不是要去开会", "你今天要去开会吗？") == ""
        assert polish_rejection_reason("能不能帮我看一下", "可以帮我看一下吗？") == ""
        assert polish_rejection_reason("没事，我们继续讨论", "我们继续讨论。") == ""

        # Repeated real negation ("不不不") is NOT treated as A-not-A!
        assert polish_rejection_reason("不不不，我不去", "不，我不去。") == ""
        assert polish_rejection_reason("不不不，我不去", "我去。") == "否定词丢失"

    def test_transcript_refiner_allows_stutter_dedup_while_blocking_distinct_number_loss(self):
        def _check_single(raw_text: str, edited_text: str):
            units = [RefinementUnit(source_id=1, speaker_id=1, text=raw_text)]
            payload = {"paragraphs": [{"source_ids": [1], "text": edited_text}]}
            return validate_refinement(payload, units)

        assert _check_single("预算大概50万，50万左右", "预算大概50万左右。")
        assert _check_single("这个方案可能，可能还要改", "这个方案可能还要改。")
        assert _check_single("现场乱七八糟的，十分感谢配合", "现场比较杂乱，非常感谢配合。")
        # Distinct numbers for two entities must still be protected!
        with pytest.raises(RefinementValidationError, match="数字或单位被改动"):
            _check_single("甲组83万元，乙组83万元", "合计83万元。")

    def test_sse_done_without_stop_and_tailscale_local_endpoint(self):
        import asyncio

        # Tailscale CGNAT 100.64.0.0/10 is local; outside 100.64.0.0/10 is not.
        assert is_local_endpoint("http://100.100.10.20:11434/v1") is True
        assert is_local_endpoint("http://100.64.0.1:8000/v1") is True
        assert is_local_endpoint("http://100.127.255.254:8000/v1") is True
        assert is_local_endpoint("http://100.128.0.1:8000/v1") is False
        assert is_local_endpoint("http://100.63.255.254:8000/v1") is False

        # Both sync text_polisher._collect_stream_text and async llm_transport._stream_text
        # accept [DONE] with non-empty text even when finish_reason=stop was omitted.
        sse_lines = [
            'data: {"choices": [{"delta": {"content": "润色后的"}, "finish_reason": null}]}',
            "",
            'data: {"choices": [{"delta": {"content": "完整句子。"}, "finish_reason": null}]}',
            "",
            "data: [DONE]",
            "",
        ]
        sync_out = _collect_stream_text(
            sse_lines, is_cancelled=lambda: False, total_timeout=5.0, reject_truncated=True
        )
        assert sync_out == "润色后的完整句子。"

        class _AsyncResp:
            async def aiter_lines(self):
                for ln in sse_lines:
                    yield ln

        async_out = asyncio.run(
            _stream_text(_AsyncResp(), first_timeout=5.0, idle_timeout=5.0, on_delta=None)
        )
        assert async_out == "润色后的完整句子。"


# ── 11. H-47, H-48, M-09, M-10: Downloader Resume, Error Copy & Tag Guard ────


class TestAudit11DownloaderErrorCopyAndTags:
    def test_model_download_worker_disk_precheck_and_http_range_resume(
        self, monkeypatch, tmp_path
    ):
        import sayink.speech_recognizer as sr_mod

        payload = b"SayInk-Pinned-Model-Bytes-" * 200
        sha = hashlib.sha256(payload).hexdigest()
        fake_model = {
            "id": "test-resume-model",
            "name": "测试断点续传模型",
            "dir_name": "test-resume-model",
            "hf_repo": "test/repo",
            "revision": "main",
            "size_mb": 10,
            "files": ["model.onnx"],
            "sha256": {"model.onnx": sha},
        }
        monkeypatch.setattr(sr_mod, "get_model_info", lambda mid: fake_model if mid == "test-resume-model" else None)
        monkeypatch.setattr(sr_mod, "_get_models_dir", lambda: tmp_path)

        # 1) Disk space pre-check blocks before network when free space is tiny
        DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])
        monkeypatch.setattr(sr_mod.shutil, "disk_usage", lambda _p: DiskUsage(1000, 990, 1024 * 1024))
        w_space = ModelDownloadWorker("test-resume-model")
        space_errors: list[str] = []
        w_space.error.connect(space_errors.append)
        w_space.run()
        assert space_errors and "磁盘剩余空间不足" in space_errors[0]
        assert "下载失败：下载失败" not in space_errors[0]

        # 2) HTTP Range resume from existing .tmp file
        monkeypatch.setattr(
            sr_mod.shutil,
            "disk_usage",
            lambda _p: DiskUsage(100 * 1024**3, 10 * 1024**3, 90 * 1024**3),
        )
        model_dir = tmp_path / "test-resume-model"
        model_dir.mkdir(parents=True, exist_ok=True)
        cut = len(payload) // 2
        (model_dir / "model.onnx.tmp").write_bytes(payload[:cut])

        seen_headers: list[dict] = []

        class _RangeResp:
            def __init__(self):
                self.status_code = 206
                self.headers = {
                    "content-range": f"bytes {cut}-{len(payload) - 1}/{len(payload)}",
                    "content-length": str(len(payload) - cut),
                }

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

            def raise_for_status(self):
                return None

            def iter_bytes(self, chunk_size=65536):
                yield payload[cut:]

        def _fake_stream(_method, _url, **kwargs):
            seen_headers.append(dict(kwargs.get("headers") or {}))
            return _RangeResp()

        monkeypatch.setattr("httpx.stream", _fake_stream)
        w_resume = ModelDownloadWorker("test-resume-model", source="huggingface")
        done_ids: list[str] = []
        errs: list[str] = []
        w_resume.finished_ok.connect(done_ids.append)
        w_resume.error.connect(errs.append)
        w_resume.run()

        assert errs == []
        assert done_ids == ["test-resume-model"]
        assert seen_headers == [{"Range": f"bytes={cut}-"}]
        assert (model_dir / "model.onnx").read_bytes() == payload
        assert not (model_dir / "model.onnx.tmp").exists()

    def test_aux_models_includes_speaker_activity_and_avoids_double_error_prefix(
        self, monkeypatch
    ):
        monkeypatch.setattr("sayink.aux_models.is_model_downloaded", lambda _mid: False)
        ids = [m["id"] for m in missing_aux_models()]
        assert AUX_MODEL_SPEAKER_ACTIVITY in ids

        from PyQt6.QtCore import QObject, pyqtSignal

        class _StubDlWorker(QObject):
            progress = pyqtSignal(int)
            finished_ok = pyqtSignal(str)
            error = pyqtSignal(str)

            def start(self):
                self.error.emit("下载失败：网络连接超时，请检查网络")

        batch = AuxModelDownloads(worker_factory=lambda _mid, _src: _StubDlWorker())
        outcomes: list[tuple[bool, str]] = []
        batch.finished.connect(lambda ok, msg: outcomes.append((ok, msg)))
        assert batch.start([AUX_MODEL_SPEAKER_ACTIVITY]) is True
        assert outcomes and outcomes[0][0] is False
        assert "下载失败：下载失败" not in outcomes[0][1]

    def test_normalize_asr_output_and_merge_slice_texts_and_worker_ref_release(self):
        # Preserve HTML/XML tags while stripping ASR meta markers
        raw = "<zh><sil>请保留 <tr><th>标题</th></tr>、<s>删除线</s> 和 <id>42</id><|tr|><tr-TR>"
        cleaned = normalize_asr_output(raw)
        assert cleaned == "请保留 <tr><th>标题</th></tr>、<s>删除线</s> 和 <id>42</id>"

        # Parallel month phrases must NOT be swallowed by fuzzy slice overlap
        merged = merge_slice_texts(["我们在2026年一月完成架构升级，2026年一月", "2026年二月启动全量回归测试"])
        assert "2026年一月" in merged and "2026年二月启动全量回归测试" in merged

        # TranscribeWorker releases _recognizer reference in finally after run()
        fake_rec = SimpleNamespace()
        tw = TranscribeWorker(fake_rec, np.zeros(0, dtype=np.float32))
        tw.run()
        assert tw._recognizer is None

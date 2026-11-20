"""Regression tests for Stage-1 audio engines optimization track (opt/audio-engines).

Covers:
1. Preserve root device/permission error in AudioRecorder (_open_lane_stream & _friendly_open_error).
2. Stop overwriting single-shot snapshot & capture isolated per-segment open snapshot in continuous mode.
3. Live speech_active_changed Signal(bool) for 「放弃本句」 visibility.
4. 15s forced VAD split 240ms overlap without breaking quiet cuts or double-counting duration.
5. separate_lanes=True bounded cross-lane watermark hold (SPLIT_MAX_HOLD_SEC = 2.5s).
6. Speaker reclustering hardware route isolation (CROSS_ROUTE_MIN_SIMILARITY = 0.80) & bounded _turns.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from sayink import text_paster as tp
from sayink.audio_devices import AudioDeviceInfo, RecordingPlan, StreamEndpoint
from sayink.audio_recorder import AudioRecorder
from sayink.meeting_speakers import (
    CROSS_ROUTE_MIN_SIMILARITY,
    MAX_RETAINED_TURNS,
    MeetingSpeakerSession,
)
from sayink.speaker_clustering import (
    TurnObservation,
    average_linkage_labels,
    bounded_final_assignments,
    final_assignments,
    reassign_turns,
)
from sayink.vad_segmenter import FORCED_CUT_OVERLAP_SEC, SpeechSegmenter

RATE = 16000
BLOCK = 1600


def _unit(*values: float) -> np.ndarray:
    vec = np.asarray(values, dtype=np.float32)
    return vec / np.linalg.norm(vec)


# ── 1. Preserve Root Device / Permission Error (Continuous #P2-6) ────────────


class TestPreserveRootDevicePermissionError:
    @pytest.mark.parametrize(
        "exc,expected_sub",
        [
            (RuntimeError("Error opening InputStream: Access is denied. [PaErrorCode -9999]"), "系统隐私与麦克风权限"),
            (PermissionError("Permission denied"), "系统隐私与麦克风权限"),
            (RuntimeError("AUDCLNT_E_ACCESS_DENIED (0x80070005)"), "系统隐私与麦克风权限"),
            (RuntimeError("Invalid device [PaErrorCode -9996]"), "恢复自动选择"),
        ],
    )
    def test_open_lane_stream_preserves_root_error_for_friendly_guidance(
        self, monkeypatch, exc, expected_sub
    ):
        rec = AudioRecorder()
        mic_dev = AudioDeviceInfo(0, "Realtek 麦克风阵列", "Windows WASAPI", 48000, 1, False, False)
        plan = RecordingPlan("microphone", (StreamEndpoint("microphone", mic_dev, False),))
        monkeypatch.setattr("sayink.audio_recorder.build_recording_plan", lambda *_a, **_kw: plan)
        monkeypatch.setattr(
            "sayink.audio_recorder._device_info_by_index",
            lambda _idx: {"name": mic_dev.name, "max_input_channels": 1, "default_samplerate": 48000},
        )

        def _fail_stream(**_kwargs):
            raise exc

        monkeypatch.setattr("sayink.audio_recorder.sd.InputStream", _fail_stream)
        errors: list[str] = []
        rec.error.connect(errors.append)

        rec.start_continuous()

        assert rec.is_recording is False
        assert rec.is_continuous is False
        assert len(errors) == 1
        assert expected_sub in errors[0]

    def test_friendly_open_error_walks_exception_cause_chain(self):
        rec = AudioRecorder()
        try:
            try:
                raise PermissionError("Access is denied")
            except PermissionError as root:
                raise RuntimeError("无法打开音频设备：USB 麦克风") from root
        except RuntimeError as wrapped:
            friendly = rec._friendly_open_error(str(wrapped), wrapped)
        assert "系统隐私与麦克风权限" in friendly


# ── 2. Per-Segment Open Snapshot & Single-Shot Isolation (Single #1, P1-1a) ──


class TestPerSegmentOpenSnapshotIsolation:
    def test_single_shot_hold_never_calls_remember_open_target_on_vad_openings(self, monkeypatch):
        tp._REMEMBERED_TARGET_SNAPSHOTS.clear()
        baseline = tp.remember_paste_target((500, "Box A - Editor", 123))
        assert tp._REMEMBERED_TARGET_SNAPSHOTS[500].title_digest == baseline.title_digest

        # Simulate user moving focus inside hwnd 500 to Box B during a multi-segment hold
        monkeypatch.setattr(
            tp, "get_foreground_window_info", lambda: (500, "Box B - Editor", 123)
        )
        monkeypatch.setattr(tp, "_get_gui_focus_hwnd", lambda _hwnd: 9999)

        rec = AudioRecorder()
        rec._is_recording = True
        rec._continuous_mode = False
        rec._segment_live = True
        rec._segmenter = SpeechSegmenter(
            adaptive=False, silence_hold_sec=0.2, short_silence_hold_sec=0.2, min_speech_sec=0.1
        )

        opened_signals: list[tuple[int, str]] = []
        rec.utterance_opened.connect(lambda h, n: opened_signals.append((h, n)))

        # Utterance 1 opens and closes
        for _ in range(3):
            rec._feed_segmenter(np.full(BLOCK, 0.1, dtype=np.float32))
        for _ in range(3):
            seg1 = rec._feed_segmenter(np.zeros(BLOCK, dtype=np.float32))
            if seg1 is not None:
                rec._emit_segment(seg1)
        # Utterance 2 opens during the same hold
        for _ in range(3):
            rec._feed_segmenter(np.full(BLOCK, 0.1, dtype=np.float32))

        assert opened_signals == []
        assert tp._REMEMBERED_TARGET_SNAPSHOTS[500] == baseline

    def test_continuous_mode_captures_per_segment_snapshots_without_mutating_global_dict(
        self, monkeypatch
    ):
        tp._REMEMBERED_TARGET_SNAPSHOTS.clear()
        windows = [
            ((101, "Doc A", 10), "code.exe", 1001),
            ((202, "Chat B", 20), "Slack.exe", 2002),
        ]
        current = {"idx": 0}

        monkeypatch.setattr(
            tp, "get_foreground_window_info", lambda: windows[current["idx"]][0]
        )
        monkeypatch.setattr(
            tp, "_process_name_from_window_info", lambda _info: windows[current["idx"]][1]
        )
        monkeypatch.setattr(
            tp, "_get_gui_focus_hwnd", lambda _hwnd: windows[current["idx"]][2]
        )
        monkeypatch.setattr(tp, "target_rejects_synthetic_input", lambda _info: False)

        rec = AudioRecorder()
        rec._is_recording = True
        rec._continuous_mode = True
        rec._segmenter = SpeechSegmenter(
            adaptive=False,
            silence_hold_sec=0.2,
            short_silence_hold_sec=0.2,
            min_speech_sec=0.1,
            pre_roll_sec=0.0,
        )

        emitted: list[np.ndarray] = []
        rec.segment_ready.connect(emitted.append)

        # Segment 1 spoken in Window 101 / Focus 1001
        current["idx"] = 0
        for _ in range(3):
            rec._feed_segmenter(np.full(BLOCK, 0.1, dtype=np.float32))
        for _ in range(3):
            s = rec._feed_segmenter(np.zeros(BLOCK, dtype=np.float32))
            if s is not None:
                rec._emit_segment(s)

        # Segment 2 spoken in Window 202 / Focus 2002 BEFORE Segment 1's target was popped
        current["idx"] = 1
        for _ in range(3):
            rec._feed_segmenter(np.full(BLOCK, 0.1, dtype=np.float32))
        for _ in range(3):
            s = rec._feed_segmenter(np.zeros(BLOCK, dtype=np.float32))
            if s is not None:
                rec._emit_segment(s)

        assert len(emitted) == 2
        # Global _REMEMBERED_TARGET_SNAPSHOTS must remain completely untouched!
        assert tp._REMEMBERED_TARGET_SNAPSHOTS == {}

        hwnd1, app1, snap1 = rec.pop_open_target_details()
        assert (hwnd1, app1) == (101, "code.exe")
        assert snap1 is not None and snap1.hwnd == 101 and snap1.focus_hwnd == 1001

        # Backward-compatible pop_open_target() returns 2-tuple (hwnd, app_name)
        hwnd2, app2 = rec.pop_open_target()
        assert (hwnd2, app2) == (202, "Slack.exe")

        assert rec.pop_open_target_details() == (0, "", None)


# ── 3. Live Speech Active Signal for 「放弃本句」 (Continuous #P1-3) ───────────


class TestSpeechActiveSignal:
    def test_speech_active_changed_tracks_sentence_open_close_discard_and_stop(self):
        rec = AudioRecorder()
        rec._is_recording = True
        rec._continuous_mode = True
        rec._segmenter = SpeechSegmenter(
            adaptive=False,
            silence_hold_sec=0.2,
            short_silence_hold_sec=0.2,
            min_speech_sec=0.1,
            pre_roll_sec=0.0,
        )
        transitions: list[bool] = []
        rec.speech_active_changed.connect(transitions.append)

        with patch.object(rec, "_remember_open_target"):
            # Silence before sentence 1 -> still False, no signal
            rec._feed_segmenter(np.zeros(BLOCK, dtype=np.float32))
            assert transitions == []
            assert rec.is_speech_active is False

            # First speech block of sentence 1 -> immediately fires True!
            rec._feed_segmenter(np.full(BLOCK, 0.1, dtype=np.float32))
            assert transitions == [True]
            assert rec.is_speech_active is True

            # Continued speech -> no duplicate True
            rec._feed_segmenter(np.full(BLOCK, 0.1, dtype=np.float32))
            assert transitions == [True]

            # VAD closes after silence -> fires False
            for _ in range(3):
                out = rec._feed_segmenter(np.zeros(BLOCK, dtype=np.float32))
                if out is not None:
                    rec._emit_segment(out)
            assert transitions == [True, False]
            assert rec.is_speech_active is False

            # Sentence 2 opens -> True, then user clicks 「放弃本句」 -> immediately False!
            rec._feed_segmenter(np.full(BLOCK, 0.1, dtype=np.float32))
            assert transitions == [True, False, True]
            rec.discard_current_utterance()
            assert transitions == [True, False, True, False]

            # Sentence 3 opens -> True, then stop_continuous -> False
            rec._feed_segmenter(np.full(BLOCK, 0.1, dtype=np.float32))
            assert transitions[-1] is True
            rec.stop_continuous(flush=False)
            assert transitions[-1] is False
            assert rec.is_speech_active is False


# ── 4. 15s Forced VAD Split 240ms Overlap (Omission #9) ──────────────────────


class TestForcedVadSplitOverlap:
    def test_forced_15s_cut_without_breath_carries_240ms_overlap_and_accurate_bounds(self):
        seg = SpeechSegmenter(adaptive=False, pre_roll_sec=0.0)
        overlap_expected = int(RATE * FORCED_CUT_OVERLAP_SEC)
        assert overlap_expected == 3840

        # Deterministic ramp with steady RMS so no quiet breath exists
        total = int(16.5 * RATE)
        t = np.arange(total, dtype=np.float32)
        stream = (0.1 * np.where((t % 40) < 20, 1.0, -1.0)).astype(np.float32)
        # Stamp unique marker values in the 240ms window right before 15.0s while keeping RMS ~0.1
        boundary_window = slice(15 * RATE - overlap_expected, 15 * RATE)
        stream[boundary_window] = np.linspace(0.08, 0.12, overlap_expected, dtype=np.float32)

        emitted: list[tuple[np.ndarray, tuple[int, int]]] = []
        for pos in range(0, total, BLOCK):
            out = seg.feed(stream[pos : pos + BLOCK])
            if out is not None:
                assert seg.last_bounds is not None
                emitted.append((out, seg.last_bounds))
        tail = seg.flush()
        if tail is not None:
            assert seg.last_bounds is not None
            emitted.append((tail, seg.last_bounds))

        assert len(emitted) == 2
        seg1, bounds1 = emitted[0]
        seg2, bounds2 = emitted[1]

        assert bounds1 == (0, 15 * RATE)
        assert getattr(seg1, "overlap_samples", 0) == 0
        assert bounds2 == (15 * RATE - overlap_expected, total)
        assert getattr(seg2, "overlap_samples", 0) == overlap_expected
        # Trailing 240ms of seg1 matches leading 240ms of seg2 sample-for-sample
        np.testing.assert_array_equal(seg1[-overlap_expected:], seg2[:overlap_expected])
        np.testing.assert_array_equal(seg2, stream[bounds2[0] : bounds2[1]])
        # Net non-overlapping duration equals exact stream length (no double-counting)
        net_samples = sum(len(a) - getattr(a, "overlap_samples", 0) for a, _ in emitted)
        assert net_samples == total

    def test_exact_15s_followed_by_silence_or_flush_emits_no_phantom_overlap_duplicate(self):
        seg = SpeechSegmenter(adaptive=False, pre_roll_sec=0.0)
        emitted = []
        for _ in range(150):
            out = seg.feed(np.full(BLOCK, 0.1, dtype=np.float32))
            if out is not None:
                emitted.append(out)
        assert len(emitted) == 1 and emitted[0].size == 15 * RATE
        for _ in range(20):
            assert seg.feed(np.zeros(BLOCK, dtype=np.float32)) is None
        assert seg.flush() is None

    def test_quiet_breath_cut_still_has_zero_overlap(self):
        seg = SpeechSegmenter(adaptive=False, pre_roll_sec=0.0)
        emitted = []
        for i in range(160):
            amp = 0.01 if i == 135 else 0.3
            out = seg.feed(np.full(BLOCK, amp, dtype=np.float32))
            if out is not None:
                emitted.append((out, seg.last_bounds))
        tail = seg.flush()
        assert tail is not None and seg.last_bounds is not None
        emitted.append((tail, seg.last_bounds))
        assert len(emitted) == 2
        assert emitted[0][1] == (0, 136 * BLOCK)
        assert emitted[1][1] == (136 * BLOCK, 160 * BLOCK)
        assert getattr(emitted[1][0], "overlap_samples", 0) == 0


# ── 5. Split-Lane Watermark Starvation Bound (Continuous Omission #4) ────────


class TestSplitLaneWatermarkBound:
    def test_long_system_monologue_does_not_starve_short_mic_utterance_for_15s(self):
        recorder = AudioRecorder()
        recorder.set_separate_lanes(True)
        recorder.set_segmenter_profile(meeting=False)
        recorder._lanes = [
            SimpleNamespace(endpoint=SimpleNamespace(role=role))
            for role in ("microphone", "system")
        ]
        recorder._prepare_split_segmenters()
        recorder._stream_anchor_ms = 10_000
        for seg in recorder._split_segmenters.values():
            seg._adaptive = False

        events: list[tuple[str, float]] = []
        current_sec = {"t": 0.0}

        def _on_seg(_audio):
            events.append((recorder.consume_segment_route(), current_sec["t"]))

        recorder.segment_ready.connect(_on_seg)

        # System talks continuously for 15s (0..15s); mic speaks 1.0..2.0s (VAD closes ~3.2s)
        with patch.object(recorder, "_remember_open_target"):
            for step in range(150):
                t_sec = (step + 1) * 0.1
                current_sec["t"] = round(t_sec, 2)
                sys_blk = np.full(BLOCK, 0.02, dtype=np.float32)
                mic_blk = (
                    np.full(BLOCK, 0.08, dtype=np.float32)
                    if 10 <= step < 20
                    else np.zeros(BLOCK, dtype=np.float32)
                )
                recorder._block_tracks = {"microphone": mic_blk, "system": sys_blk}
                recorder._feed_split_tracks((mic_blk + sys_blk) * 0.5)

        mic_releases = [t for route, t in events if route == "mic"]
        sys_releases = [t for route, t in events if route == "system"]
        assert len(mic_releases) == 1
        # Mic VAD closed around 3.2s (or 3.8s short-hold) + <= 2.5s hold -> released well before 7.0s, NOT 15.0s!
        assert mic_releases[0] <= 6.5
        assert sys_releases and mic_releases[0] < sys_releases[0]


# ── 6. Speaker Route Isolation & Bounded Turns (Omission #8, Cont Omission #7)


class TestSpeakerRouteIsolationAndBoundedTurns:
    def test_cross_route_small_mic_cluster_never_folds_into_system_speaker_below_080(self):
        # System speaker has 6 long turns; headphone mic user has 2 short turns with cos = 0.55
        v_sys = _unit(1.0, 0.0, 0.0, 0.0)
        v_mic = _unit(0.55, float(np.sqrt(1.0 - 0.55**2)), 0.0, 0.0)
        assert 0.30 < float(v_sys @ v_mic) < CROSS_ROUTE_MIN_SIMILARITY

        turns = [
            TurnObservation(k, 1, v_sys, 3500, True, route="system")
            for k in range(6)
        ] + [
            TurnObservation(6, 2, v_mic, 2000, True, route="mic"),
            TurnObservation(7, 2, v_mic, 2200, True, route="mic"),
        ]
        assigned = reassign_turns(turns, threshold=0.65)
        sys_ids = {assigned[k] for k in range(6)}
        mic_ids = {assigned[6], assigned[7]}
        assert len(sys_ids) == 1
        assert len(mic_ids) == 1
        assert sys_ids != mic_ids

    def test_single_short_mic_remark_still_stays_separate_from_system_speaker(self):
        v_sys = _unit(1.0, 0.0, 0.0, 0.0)
        v_mic = _unit(0.45, float(np.sqrt(1.0 - 0.45**2)), 0.0, 0.0)
        turns = [
            TurnObservation(k, 1, v_sys, 4000, True, route="system")
            for k in range(5)
        ] + [
            TurnObservation(5, 2, v_mic, 1800, True, route="mic"),
        ]
        assigned = reassign_turns(turns, threshold=0.65)
        assert assigned[5] != assigned[0]
        assert assigned[5] > 0

    def test_average_linkage_blocks_cross_route_merge_below_080_even_with_loose_threshold(self):
        v_sys = _unit(1.0, 0.0, 0.0)
        v_mic = _unit(0.65, float(np.sqrt(1.0 - 0.65**2)), 0.0)  # cos = 0.65 < 0.80
        labels = average_linkage_labels(
            np.stack([v_sys, v_mic]),
            max_distance=0.50,  # threshold = 0.50 would merge same-route 0.65!
            routes=["system", "mic"],
        )
        assert labels.tolist() == [0, 1]

        # Identical voice across routes (cos >= 0.80) CAN merge when threshold allows
        v_same = _unit(0.92, float(np.sqrt(1.0 - 0.92**2)), 0.0)
        labels_same = average_linkage_labels(
            np.stack([v_sys, v_same]),
            max_distance=0.35,
            routes=["system", "mic"],
        )
        assert labels_same.tolist() == [0, 0]

    def test_meeting_speaker_session_populates_route_and_bounds_multi_hour_turns(self):
        session = MeetingSpeakerSession(
            threshold=0.70,
            keep_history=True,
            max_turns=256,
        )
        v_mic = _unit(1.0, 0.0, 0.0, 0.0)
        v_sys = _unit(0.50, float(np.sqrt(1.0 - 0.50**2)), 0.0, 0.0)

        for seq in range(1200):
            if seq % 5 == 0:
                session.assign(v_mic, "mic", start_ms=seq * 2000, end_ms=seq * 2000 + 1800, sample_key=seq)
            else:
                session.assign(v_sys, "system", start_ms=seq * 2000, end_ms=seq * 2000 + 1800, sample_key=seq)

        # Confirm one recent turn and verify route is preserved
        last_turn = session.turns[-1]
        assert last_turn.route in ("mic", "system")
        session.confirm(last_turn.key, last_turn.speaker_id)
        assert session.turns[-1].route == last_turn.route

        # Memory strictly bounded to max_turns across both _turns and _turn_index!
        assert len(session.turns) <= 256
        assert len(session._turns) <= 256
        assert len(session._turn_index) <= 256
        assert MeetingSpeakerSession(keep_history=True).max_turns == MAX_RETAINED_TURNS

        reassigned = bounded_final_assignments(session, 0.65)
        mic_sids = {reassigned[t.key] for t in session.turns if t.route == "mic"}
        sys_sids = {reassigned[t.key] for t in session.turns if t.route == "system"}
        assert len(mic_sids) == 1 and len(sys_sids) == 1
        assert mic_sids != sys_sids

        # final_assignments also separates routes even if speaker_count was mocked to 1
        one_spk_view = SimpleNamespace(turns=session.turns[:20], speaker_count=1)
        fa = final_assignments(one_spk_view, 0.65)
        assert {fa[t.key] for t in one_spk_view.turns if t.route == "mic"} != {
            fa[t.key] for t in one_spk_view.turns if t.route == "system"
        }

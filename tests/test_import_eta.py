"""剩余时间按已识别的音频进度估算，而不是按进度条百分比反推。"""

from __future__ import annotations

import pytest

from sayink.import_eta import (
    ImportEtaEstimator,
    ImportProgressSample,
    format_audio_position,
    format_coarse_remaining,
)


def _recognizing(wall: float, position_ms: int, total_ms: int = 100_000) -> ImportProgressSample:
    return ImportProgressSample(
        phase="recognizing",
        wall_s=wall,
        audio_position_ms=position_ms,
        audio_total_ms=total_ms,
        duration_known=True,
    )


def test_coarse_remaining_uses_stable_buckets():
    assert format_coarse_remaining(0) == "即将完成"
    assert format_coarse_remaining(14.9) == "即将完成"
    assert format_coarse_remaining(15) == "不到 1 分钟"
    assert format_coarse_remaining(59.9) == "不到 1 分钟"
    assert format_coarse_remaining(60) == "约 1 分钟"
    assert format_coarse_remaining(89) == "约 1 分钟"
    assert format_coarse_remaining(90) == "约 2 分钟"
    assert format_coarse_remaining(599) == "约 10 分钟"
    assert format_coarse_remaining(600) == "约 10 分钟"
    assert format_coarse_remaining(750) == "约 15 分钟"
    assert format_coarse_remaining(3600) == "约 1 小时"
    assert format_coarse_remaining(4500) == "约 1 小时 15 分钟"
    assert format_coarse_remaining(7200) == "约 2 小时"


def test_audio_position_reads_as_a_clock():
    assert format_audio_position(750_000) == "已识别 12:30 音频"
    assert format_audio_position(3_661_000) == "已识别 1:01:01 音频"


def test_constant_rate_tracks_the_rest_of_the_file():
    """匀速识别时，剩余时间跟着剩下的音频走，不会在中段停在一个偏小的数上。"""
    eta = ImportEtaEstimator()
    eta.observe(_recognizing(0, 0))
    early = eta.observe(_recognizing(10, 10_000))
    assert early.remain_text == "约 2 分钟"
    assert early.remaining_s == pytest.approx(92, abs=1)

    later = eta.observe(_recognizing(40, 40_000))
    assert later.remaining_s == pytest.approx(62, abs=1)
    assert later.remain_text == "约 1 分钟"
    assert later.percent == 2 + int(93 * 0.4)
    assert later.percent < 50


def test_warmup_hides_the_countdown_until_speed_is_observed():
    eta = ImportEtaEstimator()
    eta.observe(_recognizing(0, 0))
    warming = eta.observe(_recognizing(9, 9_000))
    assert warming.remain_text == "正在估算剩余时间…"
    assert warming.remaining_s is None
    assert warming.percent == 2 + int(93 * 0.09)

    ready = eta.observe(_recognizing(10, 10_000))
    assert ready.remain_text == "约 2 分钟"
    assert ready.phase_text == "正在识别"


def test_model_load_stall_is_not_counted_as_recognition_speed():
    eta = ImportEtaEstimator()
    eta.observe(_recognizing(0, 5_000))
    eta.observe(_recognizing(30, 5_000))
    view = eta.observe(_recognizing(40, 15_000))
    assert view.remaining_s == pytest.approx(87, abs=1)
    assert view.remaining_s < 120


def test_reading_time_does_not_start_the_recognition_clock():
    eta = ImportEtaEstimator()
    eta.observe(ImportProgressSample("reading", 0, read_percent=0, audio_total_ms=100_000, duration_known=True))
    eta.observe(ImportProgressSample("reading", 25, read_percent=100, audio_total_ms=100_000, duration_known=True))
    eta.observe(_recognizing(25, 0))
    view = eta.observe(_recognizing(35, 10_000))
    assert view.remaining_s == pytest.approx(92, abs=1)
    assert view.remaining_s < 120


def test_unknown_duration_shows_position_without_a_countdown():
    eta = ImportEtaEstimator()
    view = eta.observe(ImportProgressSample("recognizing", 20, audio_position_ms=750_000, duration_known=False))
    assert view.remain_text == ""
    assert view.remaining_s is None
    assert view.position_text == "已识别 12:30 音频"
    assert view.percent is None
    assert view.phase_text == "正在识别"


def test_reading_fills_only_a_small_slice_of_the_bar():
    eta = ImportEtaEstimator()
    start = eta.observe(ImportProgressSample("reading", 0, read_percent=0, audio_total_ms=100_000, duration_known=True))
    middle = eta.observe(ImportProgressSample("reading", 1, read_percent=50, audio_total_ms=100_000, duration_known=True))
    done = eta.observe(ImportProgressSample("reading", 2, read_percent=100, audio_total_ms=100_000, duration_known=True))
    assert start.percent == 0
    assert middle.percent == 1
    assert done.percent == 2
    assert done.remain_text == ""
    assert done.phase_text == "正在读取"


def test_bar_follows_audio_position_and_never_moves_backward():
    eta = ImportEtaEstimator()
    eta.observe(_recognizing(0, 0))
    half = eta.observe(_recognizing(10, 50_000))
    assert half.percent == 2 + int(93 * 0.5)
    finished = eta.observe(_recognizing(20, 100_000))
    assert finished.percent == 95
    glitch = eta.observe(_recognizing(21, 1_000))
    assert glitch.percent == 95


def test_countdown_ticks_down_between_reports_and_stops_at_almost_done():
    eta = ImportEtaEstimator()
    eta.observe(_recognizing(0, 0))
    eta.observe(_recognizing(10, 10_000))
    ticked = eta.tick(11)
    assert ticked.remaining_s == pytest.approx(91, abs=1)
    assert ticked.remain_text == "约 2 分钟"
    almost = eta.tick(10 + 92)
    assert almost.remaining_s == 0
    assert almost.remain_text == "即将完成"
    assert eta.tick(400).remain_text == "即将完成"
    assert eta.tick(400).remaining_s == 0


def test_a_faster_new_estimate_is_accepted_immediately():
    eta = ImportEtaEstimator()
    eta.observe(_recognizing(0, 0))
    eta.observe(_recognizing(10, 10_000))
    faster = eta.observe(_recognizing(20, 40_000))
    assert faster.remaining_s == pytest.approx(32, abs=1)
    assert faster.remain_text == "不到 1 分钟"


def test_a_slower_estimate_waits_before_it_is_allowed_to_rise():
    eta = ImportEtaEstimator()
    total = 200_000
    eta.observe(_recognizing(0, 0, total))
    eta.observe(_recognizing(10, 20_000, total))
    eta.observe(_recognizing(30, 22_000, total))
    held = eta.observe(_recognizing(40, 23_000, total))
    assert held.remain_text == "约 1 分钟"
    assert held.remaining_s == pytest.approx(64, abs=2)
    accepted = eta.observe(_recognizing(50, 24_000, total))
    assert accepted.remaining_s == pytest.approx(371, abs=5)
    assert accepted.remain_text == "约 6 分钟"


def test_recent_speed_pulls_the_estimate_below_the_lifetime_average():
    eta = ImportEtaEstimator()
    total = 200_000
    eta.observe(_recognizing(0, 0, total))
    eta.observe(_recognizing(25, 10_000, total))
    view = eta.observe(_recognizing(70, 50_000, total))
    assert 165 < view.remaining_s < 200


def test_finalizing_uses_its_own_allowance_and_advances_the_last_slice():
    eta = ImportEtaEstimator()
    total = 3_600_000
    eta.observe(_recognizing(0, 0, total))
    eta.observe(_recognizing(10, total, total))
    started = eta.observe(
        ImportProgressSample(
            "finalizing",
            10,
            audio_position_ms=total,
            audio_total_ms=total,
            duration_known=True,
        )
    )
    assert started.phase_text == "正在整理说话人"
    assert started.percent == 95
    assert started.remaining_s == pytest.approx(72, abs=1)
    assert started.remain_text == "约 1 分钟"
    mid = eta.tick(10 + 36)
    assert mid.percent == 97
    done = eta.tick(10 + 72)
    assert done.percent == 99
    assert done.remain_text == "即将完成"


def test_reset_starts_a_new_file_from_zero():
    eta = ImportEtaEstimator()
    eta.observe(_recognizing(0, 0))
    eta.observe(_recognizing(20, 100_000))
    assert eta.tick(20).percent == 95
    eta.reset()
    fresh = eta.observe(ImportProgressSample("reading", 0, read_percent=0, audio_total_ms=50_000, duration_known=True))
    assert fresh.percent == 0
    assert fresh.remain_text == ""

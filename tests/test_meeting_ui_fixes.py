"""Regression tests for Meeting Page UI, token color parsing, and AudioPlayerBar fixes.

Covers:
- UI-01 (part 1): ``qcolor_from_token`` parsing for hex / rgb / rgba tokens, light-mode
  reading search highlights, and ``_LevelMeterBar`` painting.
- UI-02 + FC-02 + FC-04 (part 1): ``_AudioPlayerBar`` fail-fast error status without
  headless fake playback, async ``_pending_seek_ms`` seek on ``LoadedMedia``/``BufferedMedia``,
  ``PlayingState`` stopping ``_fallback_timer``, ``unload_source`` releasing Windows file locks
  on empty/invalid loads, and ``_prune_stale_playback_wavs`` on init/cleanup.
- UI-03: Visible ``_status_label`` feedback when 0 meetings exist without exposing blank
  ``_detail_pane`` editors.
- UI-04: ``MeetingPage.refresh()`` clearing stale list selection in ``ExtendedSelection`` mode.
- UI-05: ``_batch_notice_scroll`` vertical expansion in batch selection mode.
- UI-06 (part 1): ``_AudioPlayerBar._speed_combo`` using ``_CleanComboBox`` with unclipped labels.
- UI-07 (part 1): Compact-height (`< 540`) live sidebar layout and narrow-width (`< 880`)
  dynamic pane ``minimumWidth`` relaxation/restoration.
"""

from __future__ import annotations

import os
from pathlib import Path
import time
import wave

import numpy as np
from PyQt6.QtCore import QObject, QPoint, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QImage
from PyQt6.QtMultimedia import QMediaDevices, QMediaPlayer
from PyQt6.QtWidgets import (
    QApplication,
    QSizePolicy,
    QStyle,
    QStyleOptionComboBox,
)
import pytest

from sayink.config import Config
from sayink.history_store import HistoryStore, SegmentRecord
from sayink.meeting_session import MeetingController, MeetingOptions, resolve_capabilities
from sayink.ui import design_tokens as tok
from sayink.ui import meeting_page as mp
from sayink.ui import theme as theme_mod
from sayink.ui.main_window import MainWindow
from sayink.ui.meeting_page import (
    MeetingPage,
    _AudioPlayerBar,
    _CleanComboBox,
    _LevelMeterBar,
    _prune_stale_playback_wavs,
)
from sayink.ui.meeting_views import find_in_reading_view


def _pump(rounds: int = 4) -> None:
    app = QApplication.instance()
    if app is None:
        return
    for _ in range(rounds):
        app.processEvents()


def _write_test_wav(path: Path, *, seconds: float = 2.5, rate: int = 16000) -> Path:
    frames = int(seconds * rate)
    t = np.linspace(0.0, seconds, frames, endpoint=False, dtype=np.float32)
    pcm = (np.sin(2.0 * np.pi * 440.0 * t) * 8000.0).astype("<i2")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm.tobytes())
    return path


@pytest.fixture
def meeting_fixture(qapp, tmp_path, monkeypatch):
    cfg = Config(config_dir=tmp_path / "cfg")
    cfg.set("llm.api_url", "http://127.0.0.1:11434/v1")
    cfg.set("llm.model_name", "qwen2.5:7b")

    playback_tmp = tmp_path / "playback_tmp"
    playback_tmp.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mp.tempfile, "gettempdir", lambda: str(playback_tmp))

    history = HistoryStore(db_path=tmp_path / "history.db")
    controller = MeetingController(cfg, history, pending_segments=lambda: 0)
    theme_mod.apply_theme(qapp, mode="light")
    page = MeetingPage(cfg, controller)
    try:
        yield cfg, history, controller, page
    finally:
        page.cleanup()
        page.close()
        page.deleteLater()
        _pump()
        controller.shutdown()
        history.close(timeout=2)


# ── 1. UI-01: qcolor_from_token, search highlights & _LevelMeterBar ──────────


def test_qcolor_from_token_supports_hex_rgb_rgba_and_all_design_tokens(qapp):
    assert theme_mod.qcolor_from_token is tok.qcolor_from_token

    c_hex3 = tok.qcolor_from_token("#25E")
    assert c_hex3.isValid()
    assert (c_hex3.red(), c_hex3.green(), c_hex3.blue(), c_hex3.alpha()) == (0x22, 0x55, 0xEE, 255)

    c_hex6 = tok.qcolor_from_token("#2563EB")
    assert c_hex6.isValid()
    assert (c_hex6.red(), c_hex6.green(), c_hex6.blue(), c_hex6.alpha()) == (37, 99, 235, 255)

    c_hex8 = tok.qcolor_from_token("#802563EB")
    assert c_hex8.isValid()
    assert (c_hex8.red(), c_hex8.green(), c_hex8.blue(), c_hex8.alpha()) == (37, 99, 235, 128)

    c_rgb = tok.qcolor_from_token("rgb(37, 99, 235)")
    assert c_rgb.isValid()
    assert (c_rgb.red(), c_rgb.green(), c_rgb.blue(), c_rgb.alpha()) == (37, 99, 235, 255)

    c_rgb_pct = tok.qcolor_from_token("rgb(100%, 50%, 0%)")
    assert c_rgb_pct.isValid()
    assert (c_rgb_pct.red(), c_rgb_pct.green(), c_rgb_pct.blue(), c_rgb_pct.alpha()) == (255, 128, 0, 255)

    c_rgba_float = tok.qcolor_from_token("rgba(37, 99, 235, 0.08)")
    assert c_rgba_float.isValid()
    assert (c_rgba_float.red(), c_rgba_float.green(), c_rgba_float.blue(), c_rgba_float.alpha()) == (37, 99, 235, 20)

    c_rgba_zero = tok.qcolor_from_token("rgba(13, 13, 13, 0)")
    assert c_rgba_zero.isValid()
    assert c_rgba_zero.alpha() == 0

    c_rgba_one = tok.qcolor_from_token("rgba(13, 13, 13, 1.0)")
    assert c_rgba_one.isValid()
    assert c_rgba_one.alpha() == 255

    c_rgba_int = tok.qcolor_from_token("rgba(24, 24, 24, 236)")
    assert c_rgba_int.isValid()
    assert (c_rgba_int.red(), c_rgba_int.green(), c_rgba_int.blue(), c_rgba_int.alpha()) == (24, 24, 24, 236)

    for bad in ("", "   ", "rgb(1, 2)", "rgba(1, 2, 3)", "rgba(1, 2, 3, nope)", "not_a_valid_color_token"):
        assert not tok.qcolor_from_token(bad).isValid(), bad

    for axis in ("light", "dark"):
        tokens = tok.tokens_for(axis)
        for key, raw_val in tokens.items():
            values = raw_val if isinstance(raw_val, tuple) else (raw_val,)
            for item in values:
                parsed = tok.qcolor_from_token(item)
                assert parsed.isValid(), f"{axis}.{key}={item!r} produced invalid QColor"


def test_light_mode_search_highlight_and_level_meter_never_render_black(qapp, meeting_fixture):
    _cfg, history, controller, page = meeting_fixture
    theme_mod.apply_theme(qapp, mode="light")
    page.reapply_theme()

    rec = controller.store.create_meeting(title="色彩回归")
    controller.store.end_meeting(rec.id, ended_at=rec.started_at + 8000)
    history.enqueue(
        SegmentRecord(rec.id, 0, rec.started_at + 1000, raw_text="第一处关键词高亮检查", trigger_mode="meeting", speaker_id=1)
    )
    history.enqueue(
        SegmentRecord(rec.id, 1, rec.started_at + 3000, raw_text="第二处关键词高亮确认", trigger_mode="meeting", speaker_id=2)
    )
    history.flush()

    page.resize(960, 640)
    page.show()
    page.show_meeting(rec.id)
    page._switch_tab(1)
    _pump()

    idx, total = find_in_reading_view(page._transcript_view, "关键词")
    assert (idx, total) == (1, 2)
    selections = page._transcript_view.extraSelections()
    assert len(selections) == 2

    active_bg = selections[0].format.background().color()
    passive_bg = selections[1].format.background().color()
    assert active_bg.isValid()
    assert passive_bg.isValid()
    assert active_bg.name().lower() == "#93c5fd"
    assert active_bg.alpha() == 255
    assert active_bg.name().lower() != "#000000"
    assert passive_bg.name().lower() == "#fde68a"

    # Paint _LevelMeterBar onto a white canvas in light mode and verify no black #000000 track pixels.
    meter = _LevelMeterBar("测试电平")
    meter.resize(120, 8)
    meter.set_active(True)
    meter.set_level(0.5)

    canvas = QImage(120, 8, QImage.Format.Format_ARGB32_Premultiplied)
    canvas.fill(Qt.GlobalColor.white)
    meter.render(canvas, QPoint(0, 0))

    # Track region on the right half (x=95, y=4) must be light translucent grey over white, never black.
    track_px = canvas.pixelColor(95, 4)
    assert track_px.red() > 210 and track_px.green() > 210 and track_px.blue() > 210
    # Active fill region on the left half (x=20, y=4) must be accent blue, never black.
    fill_px = canvas.pixelColor(20, 4)
    assert fill_px.blue() > fill_px.red() + 80


# ── 2. UI-02 + FC-02 + FC-04: _AudioPlayerBar reliability & seek fixes ───────


def test_audio_player_no_output_device_stops_and_surfaces_chinese_status(meeting_fixture, tmp_path, monkeypatch):
    _cfg, history, controller, page = meeting_fixture
    wav_path = _write_test_wav(tmp_path / "no_device.wav", seconds=2.0)

    rec = controller.store.create_meeting(title="无声卡测试", audio_path=str(wav_path))
    controller.store.end_meeting(rec.id, ended_at=rec.started_at + 2000)
    history.flush()

    page.resize(960, 640)
    page.show()
    page.show_meeting(rec.id)
    _pump()

    player = page._audio_player
    assert player._allow_headless_fallback is False
    monkeypatch.setattr(player, "_ensure_backend", lambda: False)

    player.play()
    _pump()

    assert player._playing is False
    assert not player._fallback_timer.isActive()
    assert player._play_btn.text() == "播放"
    assert page._status_label.isVisible()
    assert "音频输出设备" in page._status_label.text()


def test_audio_player_backend_error_stops_and_surfaces_chinese_status(meeting_fixture, tmp_path):
    _cfg, history, controller, page = meeting_fixture
    wav_path = _write_test_wav(tmp_path / "corrupt_sim.wav", seconds=2.0)

    rec = controller.store.create_meeting(title="损坏音频测试", audio_path=str(wav_path))
    controller.store.end_meeting(rec.id, ended_at=rec.started_at + 2000)
    history.flush()

    page.resize(960, 640)
    page.show()
    page.show_meeting(rec.id)
    _pump()

    player = page._audio_player
    player._playing = True
    player._fallback_timer.start()
    player._on_backend_error(QMediaPlayer.Error.FormatError, "不支持的编码格式")
    _pump()

    assert player._playing is False
    assert not player._fallback_timer.isActive()
    assert player._play_btn.text() == "播放"
    assert page._status_label.isVisible()
    assert "无法播放该音频文件" in page._status_label.text()
    assert "不支持的编码格式" in page._status_label.text()


class _FakeAsyncMediaPlayer(QObject):
    positionChanged = pyqtSignal(int)
    durationChanged = pyqtSignal(int)
    playbackStateChanged = pyqtSignal(object)
    mediaStatusChanged = pyqtSignal(object)
    errorOccurred = pyqtSignal(object, str)

    def __init__(self) -> None:
        super().__init__()
        self._source = QUrl()
        self._status = QMediaPlayer.MediaStatus.NoMedia
        self._state = QMediaPlayer.PlaybackState.StoppedState
        self._pos = 0
        self._rate = 1.0
        self.seek_calls: list[int] = []

    def source(self) -> QUrl:
        return self._source

    def mediaStatus(self) -> QMediaPlayer.MediaStatus:
        return self._status

    def playbackState(self) -> QMediaPlayer.PlaybackState:
        return self._state

    def position(self) -> int:
        return self._pos

    def setPlaybackRate(self, rate: float) -> None:
        self._rate = rate

    def setSource(self, url: QUrl) -> None:
        self._source = url
        if url.isEmpty():
            self._status = QMediaPlayer.MediaStatus.NoMedia
            self._pos = 0
            return
        # Simulate Qt's async load resetting position to 0 while LoadingMedia.
        self._status = QMediaPlayer.MediaStatus.LoadingMedia
        self._pos = 0
        self.positionChanged.emit(0)
        self.mediaStatusChanged.emit(QMediaPlayer.MediaStatus.LoadingMedia)

    def setPosition(self, pos: int) -> None:
        self.seek_calls.append(int(pos))
        # Like QMediaPlayer on Windows, setPosition before LoadedMedia is ignored by backend.
        if self._status in (
            QMediaPlayer.MediaStatus.LoadedMedia,
            QMediaPlayer.MediaStatus.BufferedMedia,
            QMediaPlayer.MediaStatus.BufferingMedia,
            QMediaPlayer.MediaStatus.EndOfMedia,
        ):
            self._pos = int(pos)
            self.positionChanged.emit(self._pos)

    def play(self) -> None:
        self._state = QMediaPlayer.PlaybackState.PlayingState
        self.playbackStateChanged.emit(self._state)

    def pause(self) -> None:
        self._state = QMediaPlayer.PlaybackState.PausedState
        self.playbackStateChanged.emit(self._state)

    def stop(self) -> None:
        self._state = QMediaPlayer.PlaybackState.StoppedState
        self.playbackStateChanged.emit(self._state)

    def finish_async_load(self, status=QMediaPlayer.MediaStatus.LoadedMedia) -> None:
        self._status = status
        self.mediaStatusChanged.emit(status)


def test_audio_player_async_first_seek_and_dual_timer_conflict(qapp, tmp_path):
    wav_path = _write_test_wav(tmp_path / "async_seek.wav", seconds=10.0)
    bar = _AudioPlayerBar()
    fake = _FakeAsyncMediaPlayer()

    def _attach_fake() -> bool:
        if bar._player is None:
            bar._player = fake
            fake.positionChanged.connect(bar._on_backend_position)
            fake.durationChanged.connect(bar._on_backend_duration)
            fake.playbackStateChanged.connect(bar._on_backend_state)
            fake.mediaStatusChanged.connect(bar._on_media_status_changed)
            fake.errorOccurred.connect(bar._on_backend_error)
        return True

    bar._ensure_backend = _attach_fake  # type: ignore[method-assign]
    try:
        assert bar.load_audio(wav_path) is True
        # Simulate dual-timer hazard: start fallback timer before first timestamp seek+autoplay.
        bar._fallback_timer.start()

        # First seek while backend has not loaded the source yet.
        bar.seek_ms(4500, autoplay=True)
        # Position must NOT reset to 0 when setSource() emitted positionChanged(0) during LoadingMedia!
        assert bar._position_ms == 4500
        assert bar._pending_seek_ms == 4500
        # PlayingState must stop _fallback_timer immediately!
        assert not bar._fallback_timer.isActive()
        assert bar._playing is True

        # Calling seek_ms again while still LoadingMedia updates pending target to 6200ms.
        bar.seek_ms(6200, autoplay=True)
        assert bar._pending_seek_ms == 6200
        assert bar._position_ms == 6200

        # Once media reaches LoadedMedia, pending seek is applied to backend!
        fake.finish_async_load(QMediaPlayer.MediaStatus.LoadedMedia)
        assert bar._pending_seek_ms is None
        assert fake.position() == 6200
        assert bar._position_ms == 6200
        assert 6200 in fake.seek_calls
    finally:
        bar.cleanup_temp_files()
        bar.deleteLater()
        _pump()


def test_audio_player_unload_releases_windows_file_lock_and_prunes_stale_wavs(qapp, tmp_path, monkeypatch):
    temp_root = tmp_path / "temp_root"
    playback_dir = temp_root / "sayink-playback"
    playback_dir.mkdir(parents=True, exist_ok=True)
    stale_wav = _write_test_wav(playback_dir / "stale-old.wav", seconds=0.5)
    fresh_wav = _write_test_wav(playback_dir / "fresh-keep.wav", seconds=0.5)
    old_ts = time.time() - 72 * 3600
    os.utime(stale_wav, (old_ts, old_ts))

    monkeypatch.setattr(mp.tempfile, "gettempdir", lambda: str(temp_root))
    monkeypatch.setattr(mp, "_STALE_PLAYBACK_WAV_PRUNED", False)

    # Initializing _AudioPlayerBar prunes stale wavs safely while keeping fresh wavs.
    bar = _AudioPlayerBar()
    try:
        assert not stale_wav.exists()
        assert fresh_wav.exists()

        wav1 = _write_test_wav(tmp_path / "lock_test_1.wav", seconds=1.5)
        wav2 = _write_test_wav(tmp_path / "lock_test_2.wav", seconds=1.5)

        assert bar.load_audio(wav1) is True
        if QMediaDevices.audioOutputs():
            bar.play()
            _pump(8)

        # Empty path load must call unload_source() (setSource(QUrl())) so Windows file lock is released.
        assert bar.load_audio("") is False
        _pump(4)
        if bar._player is not None:
            assert bar._player.source().isEmpty()
        wav1.unlink()
        assert not wav1.exists()

        # Invalid/non-existent path load must ALSO unload source and release previous file handle.
        assert bar.load_audio(wav2) is True
        if QMediaDevices.audioOutputs():
            bar.play()
            _pump(8)
        assert bar.load_audio(tmp_path / "missing_audio.wav") is False
        _pump(4)
        if bar._player is not None:
            assert bar._player.source().isEmpty()
        wav2.unlink()
        assert not wav2.exists()

        # Also verify _prune_stale_playback_wavs runs during cleanup_temp_files().
        stale_cleanup = _write_test_wav(playback_dir / "stale-cleanup.wav", seconds=0.5)
        os.utime(stale_cleanup, (old_ts, old_ts))
        bar.cleanup_temp_files()
        assert not stale_cleanup.exists()
        assert fresh_wav.exists()
    finally:
        _prune_stale_playback_wavs(playback_dir, max_age_s=0, force=True)
        bar.deleteLater()
        _pump()


# ── 3. UI-03: Visible status when 0 meetings exist without blank editors ─────


def test_zero_meetings_status_feedback_visible_without_exposing_detail_pane(meeting_fixture, monkeypatch):
    _cfg, _history, controller, page = meeting_fixture
    page.resize(960, 640)
    page.show()
    _pump()

    assert page.meeting_count() == 0
    assert page._detail_pane.isHidden()
    assert page._status_label.isHidden()

    # 1) Pre-flight model loading block on media import with 0 records.
    monkeypatch.setattr(
        controller,
        "media_block_reason",
        lambda: "语音模型未就绪，请等待模型加载完成后再导入音视频",
    )
    page._media_btn.click()
    _pump()

    assert page._status_label.isVisible()
    assert "语音模型未就绪" in page._status_label.text()
    assert page._detail_pane.isHidden()
    assert not page._detail_title.isVisible()
    assert not page._content_stack.isVisible()

    # 2) Media import failure after import progress placeholder on an empty library.
    page._show_import_detail()
    _pump()
    assert page._detail_pane.isVisible()

    page._on_media_failed("导入音视频失败：无法解码损坏文件")
    _pump()
    assert page._status_label.isVisible()
    assert "无法解码损坏文件" in page._status_label.text()
    assert page._detail_pane.isHidden()
    assert not page._detail_title.isVisible()

    # 3) Media import finished with empty result ("") on an empty library.
    page._show_import_detail()
    controller.media_import_notice = "已取消导入。"
    page._on_media_finished("")
    _pump()
    assert page._status_label.isVisible()
    assert page._status_label.text() == "已取消导入。"
    assert page._detail_pane.isHidden()


# ── 4. UI-04: Single-selection restore clears stale ExtendedSelection rows ───


def test_refresh_and_show_meeting_never_leave_two_rows_selected(meeting_fixture):
    _cfg, history, controller, page = meeting_fixture
    records = []
    for idx in range(3):
        rec = controller.store.create_meeting(title=f"会议 {idx + 1}")
        controller.store.end_meeting(rec.id, ended_at=rec.started_at + 5000)
        records.append(rec)
    history.flush()

    page.resize(960, 640)
    page.show()
    page.refresh()
    _pump()

    page.show_meeting(records[0].id)
    _pump()
    assert [it.data(Qt.ItemDataRole.UserRole) for it in page._list.selectedItems()] == [records[0].id]

    # Simulate stale selection remaining in ExtendedSelection before switching to another meeting.
    page._list.blockSignals(True)
    page._list.item(0).setSelected(True)
    page._list.item(1).setSelected(True)
    page._list.blockSignals(False)

    page.show_meeting(records[2].id)
    _pump()
    selected_ids = [it.data(Qt.ItemDataRole.UserRole) for it in page._list.selectedItems()]
    assert selected_ids == [records[2].id]


# ── 5. UI-05: _batch_notice_scroll expands vertically in batch mode ──────────


def test_batch_notice_scroll_uses_available_height_in_batch_mode(meeting_fixture):
    _cfg, history, controller, page = meeting_fixture
    for idx in range(8):
        rec = controller.store.create_meeting(title=f"批量复盘会议 #{idx + 1}")
        controller.store.end_meeting(rec.id, ended_at=rec.started_at + 4000)
    history.flush()

    page.resize(960, 640)
    page.show()
    page.refresh()
    _pump()

    page._select_loaded_meetings()
    _pump()

    assert page._batch_mode is True
    assert page._batch_notice_scroll.isVisible()
    assert not page._content_stack.isVisible()
    assert page._batch_notice_scroll.sizePolicy().verticalPolicy() == QSizePolicy.Policy.Expanding
    assert page._batch_notice_scroll.height() >= 240


# ── 6. UI-06: _AudioPlayerBar._speed_combo uses _CleanComboBox & fits rates ──


def test_speed_combo_uses_clean_combobox_and_fits_all_rates_unclipped(meeting_fixture, tmp_path):
    _cfg, history, controller, page = meeting_fixture
    wav_path = _write_test_wav(tmp_path / "speed_test.wav", seconds=2.0)
    rec = controller.store.create_meeting(title="倍速显示检查", audio_path=str(wav_path))
    controller.store.end_meeting(rec.id, ended_at=rec.started_at + 2000)
    history.flush()

    page.resize(960, 640)
    page.show()
    page.show_meeting(rec.id)
    _pump()

    combo = page._audio_player._speed_combo
    assert isinstance(combo, _CleanComboBox)
    assert combo.width() >= 108

    expected_labels = ["0.75x", "1.0x", "1.25x", "1.5x", "2.0x"]
    actual_labels = [combo.itemText(i) for i in range(combo.count())]
    assert actual_labels == expected_labels

    for i, text in enumerate(expected_labels):
        combo.setCurrentIndex(i)
        _pump()
        opt = QStyleOptionComboBox()
        combo.initStyleOption(opt)
        edit_rect = combo.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox,
            opt,
            QStyle.SubControl.SC_ComboBoxEditField,
            combo,
        )
        avail = max(24, edit_rect.width() - 4)
        advance = opt.fontMetrics.horizontalAdvance(text)
        assert advance <= avail, f"{text!r} ({advance}px) overflows edit field ({avail}px)"


# ── 7. UI-07: Compact height (<540) live view & narrow width (<880) browse ───


def test_compact_viewport_live_view_keeps_notes_visible_without_scrolling(qapp, tmp_path, monkeypatch):
    cfg = Config(config_dir=tmp_path / "cfg_compact")
    playback_tmp = tmp_path / "playback_compact"
    playback_tmp.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mp.tempfile, "gettempdir", lambda: str(playback_tmp))

    history = HistoryStore(db_path=tmp_path / "compact.db")
    controller = MeetingController(cfg, history, pending_segments=lambda: 0)
    theme_mod.apply_theme(qapp, mode="light")

    win = MainWindow(cfg, history, meeting=controller)
    page: MeetingPage = win.meeting_panel  # type: ignore[assignment]
    try:
        # 1366x768 @ 150% DPI effective viewport: 910x472
        win.setMinimumSize(640, 400)
        win.resize(910, 472)
        win.show()
        win.show_page("meeting")
        _pump(6)

        options = MeetingOptions(title="紧凑视口实时会议", input_source="mixed")
        caps = resolve_capabilities(cfg, options, dictation_model_id="sensevoice")
        controller.begin(options, caps)
        _pump(8)

        assert page.height() < 540
        assert page._marks_view.maximumHeight() <= 52

        vp = page._live_side_scroll.viewport()
        bar = page._live_side_scroll.verticalScrollBar()
        assert bar.value() == 0

        for state_check in ("live", "paused"):
            if state_check == "paused":
                controller.pause("设备切换中")
                _pump(6)
            title_top = page._notes_title.mapTo(vp, QPoint(0, 0)).y()
            title_bottom = title_top + page._notes_title.height()
            notes_top = page._user_notes.mapTo(vp, QPoint(0, 0)).y()
            visible_notes_h = vp.height() - notes_top
            assert 0 <= title_top < title_bottom <= vp.height(), (state_check, title_top, title_bottom, vp.height())
            assert visible_notes_h >= 40, (state_check, notes_top, vp.height(), visible_notes_h)
    finally:
        if controller.is_active:
            controller.request_end()
        page.cleanup()
        win.close()
        win.deleteLater()
        _pump()
        controller.shutdown()
        history.close(timeout=2)


def test_narrow_width_relaxes_pane_minimums_and_restores_when_wide(meeting_fixture):
    cfg, history, controller, page = meeting_fixture
    rec = controller.store.create_meeting(title="窄屏工具栏防裁切测试")
    controller.store.end_meeting(rec.id, ended_at=rec.started_at + 6000)
    controller.store.save_notes(
        rec.id,
        template_id="general",
        model="qwen2.5:7b",
        json_payload="",
        markdown="## 会议结论\n\n确认右侧按钮不超出窗口边界。",
    )
    history.flush()

    # 1) Standalone narrow MeetingPage (< 880, e.g. 800px) relaxes minimumWidth, and >= 880 restores 240/380.
    page.resize(800, 620)
    page.show()
    page.show_meeting(rec.id)
    _pump(6)

    assert page._browse_left.minimumWidth() < tok.WORKSPACE_LIST_MIN_WIDTH
    assert page._detail_pane.minimumWidth() < tok.WORKSPACE_DETAIL_MIN_WIDTH
    for btn in (page._more_btn, page._export_btn, page._copy_btn):
        assert btn.isVisible()
        right_edge = btn.mapTo(page, QPoint(btn.width(), 0)).x()
        assert right_edge <= page.width(), (btn.text(), right_edge, page.width())

    page.resize(900, 620)
    _pump(6)
    assert page._browse_left.minimumWidth() == tok.WORKSPACE_LIST_MIN_WIDTH
    assert page._detail_pane.minimumWidth() == tok.WORKSPACE_DETAIL_MIN_WIDTH

    # 2) Inside an 800px MainWindow (MeetingPage ~630px wide), right-side buttons remain unclipped.
    win = MainWindow(cfg, history, meeting=controller)
    win_page: MeetingPage = win.meeting_panel  # type: ignore[assignment]
    try:
        win.setMinimumSize(640, 420)
        win.resize(800, 600)
        win.show()
        win.show_page("meeting")
        win_page.show_meeting(rec.id)
        _pump(8)

        assert win_page.width() < 880
        assert win_page._browse_left.minimumWidth() < tok.WORKSPACE_LIST_MIN_WIDTH
        assert win_page._detail_pane.minimumWidth() < tok.WORKSPACE_DETAIL_MIN_WIDTH
        for btn in (win_page._more_btn, win_page._export_btn, win_page._copy_btn):
            assert btn.isVisible()
            right_in_page = btn.mapTo(win_page, QPoint(btn.width(), 0)).x()
            right_in_win = btn.mapTo(win, QPoint(btn.width(), 0)).x()
            assert right_in_page <= win_page.width(), (btn.text(), right_in_page, win_page.width())
            assert right_in_win <= win.width(), (btn.text(), right_in_win, win.width())
    finally:
        win_page.cleanup()
        win.close()
        win.deleteLater()
        _pump()

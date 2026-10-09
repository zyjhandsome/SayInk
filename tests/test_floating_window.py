"""Tests for the floating status HUD (sayink/ui/floating_window.py)."""

from __future__ import annotations

import sys

import pytest
from PyQt6.QtWidgets import QApplication

from sayink.ui.floating_window import (
    MODEL_LOADING_TITLE,
    FloatingWindow,
    WaveformWidget,
    _DotIndicator,
)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication(sys.argv)


@pytest.fixture
def win(qapp):
    w = FloatingWindow()
    yield w
    w.close()


class TestCallWhenPainted:
    """A translucent bar reappears with its last frame (「已发送」) until it
    repaints; blocking work must wait until the new state is on screen."""

    def test_runs_after_the_bar_painted_not_before(self, win, qapp):
        import time

        win.show_success("已发送")
        for _ in range(20):
            qapp.processEvents()
            time.sleep(0.01)
        win.dismiss_if_idle()
        qapp.processEvents()
        painted = []
        win.paintEvent = (lambda orig: lambda ev: (painted.append(1), orig(ev)))(win.paintEvent)
        ran = []

        win.show_recording()
        win.call_when_painted(lambda: ran.append(bool(painted)))
        assert ran == []
        deadline = time.monotonic() + 1.0
        while not ran and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.005)
        assert ran == [True]

    def test_falls_back_when_no_paint_arrives(self, win, qapp):
        import time

        from sayink.ui.floating_window import PAINT_WAIT_MAX_MS

        win.hide()
        ran = []
        win.call_when_painted(lambda: ran.append(1))
        deadline = time.monotonic() + PAINT_WAIT_MAX_MS / 1000 + 1.0
        while not ran and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.005)
        assert ran == [1]


class TestFloatingWindowStates:
    def test_initial_flags(self, win):
        assert win._listening_active is False
        assert win._model_loading_active is False

    def test_show_listening_sets_flags(self, win):
        win.show_listening()
        assert win._listening_active is True
        assert win._model_loading_active is False
        assert "正在听" in win._status_label.text()

    def test_show_continuous_idle_sets_hint(self, win):
        win.show_continuous_idle("Alt+Space")
        assert win._listening_active is False
        assert "Alt+Space" in win._text_label.text()

    def test_show_continuous_stopped(self, win):
        win.show_listening()
        win.show_continuous_stopped("正在处理剩余内容（1 段）")
        assert win._listening_active is False
        assert "已停止" in win._status_label.text()
        assert "剩余内容" in win._text_label.text()

    def test_show_recording(self, win):
        win.show_recording()
        assert win._listening_active is False
        assert "录音中" in win._status_label.text()

    def test_show_recognizing_truncates_long_text(self, win):
        long_text = "字" * 80
        win.show_recognizing(long_text)
        assert win._status_label.text() == "正在识别"
        assert win._text_label.text().startswith("...")

    def test_show_polishing(self, win):
        win.show_polishing("润色文本")
        assert win._status_label.text() == "润色中"

    def test_processing_keeps_continuous_listening_visible(self, win):
        win.show_listening()
        win.show_recognizing()
        assert "正在听" in win._status_label.text()
        assert "识别中" in win._status_label.text()
        assert win._waveform.isVisible()
        assert win._waveform._timer.isActive()

    def test_success_does_not_hide_active_listening_bar(self, win):
        win.show_listening()
        win.show_success("已发送", "发送到 notepad.exe")
        assert win._listening_active is True
        assert "正在听" in win._status_label.text()
        assert "已发送" in win._status_label.text()
        assert not win._hide_timer.isActive()

    def test_show_success_with_subtitle(self, win):
        win.show_success("已输入", "可按 Ctrl+V")
        assert win._status_label.text() == "已输入"
        assert win._text_label.text() == "可按 Ctrl+V"

    def test_show_info(self, win):
        win.show_info("已输入（原文）")
        assert win._status_label.text() == "已输入（原文）"

    def test_show_cancelled(self, win):
        win.show_cancelled()
        assert win._status_label.text() == "已取消"

    def test_show_busy_transcribing(self, win):
        win.show_busy_transcribing()
        assert "请稍候" in win._status_label.text()

    def test_model_loading_excerpt_stays_single_line(self, win):
        win.show_model_loading("正在将 Fun-ASR-Nano 载入内存\n第二行请勿录音")
        assert "\n" not in win._text_label.text()
        assert win._text_label.isVisible()
        assert win._text_label.maximumHeight() <= 18

    def test_update_partial_text(self, win):
        win.update_partial_text("部分文本")
        assert win._text_label.text() == "部分文本"

    def test_update_volume_forwards_to_waveform(self, win):
        win.update_volume(0.5)
        assert win._waveform._volume == pytest.approx(min(0.5 * 8, 1.0))


class TestModelLoadingGuard:
    """FR-UI-04 / P0: model-loading state must not be overwritten by errors/warnings."""

    def test_error_suppressed_during_model_loading(self, win):
        win.show_model_loading("载入中")
        assert win._model_loading_active is True
        win.show_error("识别失败")
        # Still showing model-loading state, error was ignored.
        assert win._status_label.text() == MODEL_LOADING_TITLE

    def test_warning_suppressed_during_model_loading(self, win):
        win.show_model_loading()
        win.show_warning("音频受限")
        assert win._status_label.text() == MODEL_LOADING_TITLE

    def test_do_not_record_hint_is_in_the_title_not_the_elided_detail(self, win):
        """U-04: a long ETA detail used to push “请勿录音” off the end of the bar."""
        win.show_model_loading("正在加载 Fun-ASR-Nano（首次载入约 10–40 秒，上次用时约 12 秒）…")
        assert "请勿录音" in win._status_label.text()
        assert "…" in win._text_label.text() or "Fun-ASR-Nano" in win._text_label.text()

    def test_clear_lock_allows_errors_again(self, win):
        win.show_model_loading()
        win.clear_model_loading_lock()
        assert win._model_loading_active is False
        win.show_error("识别失败")
        assert win._status_label.text() == "识别失败"
        assert win.toolTip() == "识别失败"
        assert win._hide_timer.isActive()

    def test_dismiss_hides_model_loading_hud(self, win):
        win.show_model_loading("正在加载 FireRedASR2...")
        assert win.isVisible()
        win.dismiss_if_idle()
        assert not win.isVisible()
        assert win._model_loading_active is False

    def test_error_stays_capsule_height(self, win):
        win.show_error("识别失败：" + "请检查网络或模型配置。" * 12)

        from sayink.ui.floating_window import COMPACT_HEIGHT

        assert win.height() <= COMPACT_HEIGHT + 40
        assert win.toolTip()

    def test_error_splits_newline_then_colon(self, win):
        msg = "录音过短\n请按住快捷键说话，时长至少 0.1 秒"
        win.show_error(msg)

        from sayink.ui.floating_window import COMPACT_HEIGHT

        assert win._status_label.text() == "录音过短"
        assert win._text_label.text()
        assert win.height() <= COMPACT_HEIGHT + 40
        assert msg in win.toolTip()

    def test_success_clears_loading_flag(self, win):
        win.show_model_loading()
        win.show_success("已就绪")
        assert win._model_loading_active is False


class TestFloatingWindowClassicColors:
    def test_recording_uses_record_accent_others_neutral(self, win):
        from sayink.ui.design_tokens import STATE_LISTEN, STATE_RECORD

        win.show_listening()
        listen_ss = win._status_label.styleSheet().lower()
        assert STATE_RECORD.lower() not in listen_ss
        assert STATE_LISTEN.lower() in listen_ss

        win.show_recording()
        rec_ss = win._status_label.styleSheet().lower()
        assert STATE_RECORD.lower() in rec_ss

        win.dismiss_if_idle()
        win.show_recognizing("hi")
        assert STATE_RECORD.lower() not in win._status_label.styleSheet().lower()


def test_listen_bar_is_thin_without_extra_actions(win):
    from sayink.ui.floating_window import BAR_HEIGHT, BAR_WIDTH
    win.show_listening()
    assert win.height() <= BAR_HEIGHT + 8
    assert win.width() <= BAR_WIDTH + 16
    assert win._end_btn.text() == "结束"
    assert not hasattr(win, "_history_btn")
    assert not hasattr(win, "_settings_btn")


def test_hold_bar_stays_put_until_the_key_is_released(win):
    from sayink.ui.design_tokens import STATE_RECORD

    win.show_recording()
    held_height = win.height()
    assert "松开结束" in win._text_label.text()

    win.show_recognizing()
    assert win.height() == held_height
    assert win._status_label.text() == "录音中"
    assert "松开结束" in win._text_label.text()
    assert STATE_RECORD.lower() in win._status_label.styleSheet().lower()

    win.show_live_transcript("开头" + "啊" * 40 + "这句还在")
    shown = win._text_label.text()
    shown_height = win.height()
    assert "这句还在" in shown
    assert win._status_label.text() == "录音中"
    assert win._dot._timer.isActive()

    win.show_recognizing()
    win.show_recording()
    assert win._text_label.text() == shown
    assert win.height() == shown_height
    assert win._status_label.text() == "录音中"
    assert STATE_RECORD.lower() in win._status_label.styleSheet().lower()
    assert win._dot._timer.isActive()


def test_hold_bar_switches_to_recognizing_once_the_key_is_released(win):
    """After release the bar must stop saying 「录音中 / 松开结束」 even though
    the capture gate is still up until the result arrives."""
    from sayink.ui.design_tokens import STATE_RECOGNIZE, STATE_RECORD

    win.show_recording()
    win.end_capture()
    assert win._status_label.text() == "正在识别"
    assert "松开结束" not in win._text_label.text()
    assert STATE_RECOGNIZE.lower() in win._status_label.styleSheet().lower()
    assert not win._dot._timer.isActive()
    assert not win._waveform.isVisible()

    # Late state pushes from the pipeline keep the released look.
    win.show_recognizing()
    assert win._status_label.text() == "正在识别"
    win.show_polishing()
    assert win._status_label.text() == "润色中"
    assert STATE_RECORD.lower() not in win._status_label.styleSheet().lower()
    # The final transcript landing after release must not paint 「录音中」 again.
    win.show_live_transcript("今天下午三点开会")
    assert win._status_label.text() == "正在识别"
    assert STATE_RECORD.lower() not in win._status_label.styleSheet().lower()


def test_end_capture_keeps_the_live_transcript_tail(win):
    win.show_recording()
    win.show_live_transcript("开头" + "啊" * 80 + "这句还在")
    win.end_capture()
    assert "这句还在" in win._text_label.text()
    assert win._status_label.text() == "正在识别"


def test_end_capture_is_a_noop_outside_a_hold(win):
    win.show_listening()
    win.end_capture()
    assert "正在听" in win._status_label.text()
    assert win._capture_released is False

    win.show_recording()
    win.end_capture()
    win.show_recording()
    assert win._capture_released is False
    assert "松开结束" in win._text_label.text()


def test_hold_recording_shows_live_transcript(win):
    win.show_recording()
    win.show_live_transcript("开头" + "啊" * 80 + "这句还在")
    assert "这句还在" in win._text_label.text()
    assert "松开结束" not in win._text_label.text()
    assert win._waveform.isVisible()
    assert win._waveform.width() <= 40

    win.show_recognizing()
    win.show_success("已输入", "可按 Ctrl+V 粘贴")
    assert "这句还在" in win._text_label.text()


def test_live_transcript_keeps_the_tail_while_listening(win):
    win.show_listening()
    win.show_live_transcript("开头" + "啊" * 80 + "结尾还在")
    assert "结尾还在" in win._text_label.text()
    assert win._waveform.isVisible()
    assert win._waveform.width() <= 40

    win.show_listening()
    win.show_recognizing()
    win.show_success("已发送", "发送到 notepad")
    assert "结尾还在" in win._text_label.text()
    assert "正在听" in win._status_label.text()


def test_partial_text_grows_excerpt_only(win):
    from sayink.ui.floating_window import BAR_EXCERPT_HEIGHT
    win.show_listening()
    win.update_partial_text("下一步把这份纪要贴到会议群里。")
    assert win.height() <= BAR_EXCERPT_HEIGHT + 8
    assert "纪要" in win._text_label.text()
    assert win._end_btn.text() == "结束"


def test_update_partial_text_keeps_width_and_center_on_excerpt(win, qapp):
    """U-12: the excerpt line only adds height; the bar must not jump sideways."""
    from PyQt6.QtGui import QCursor
    from PyQt6.QtWidgets import QApplication

    from sayink.ui.floating_window import BAR_EXCERPT_HEIGHT, BAR_HEIGHT, BAR_WIDTH

    screen = QApplication.screenAt(QCursor.pos()) or qapp.primaryScreen()
    assert screen is not None
    geo = screen.availableGeometry()

    win.show_listening()
    qapp.processEvents()
    compact_x = win.x()
    assert win.width() == BAR_WIDTH
    assert win.height() == BAR_HEIGHT
    assert compact_x == geo.x() + (geo.width() - BAR_WIDTH) // 2

    win.update_partial_text("下一步把这份纪要贴到会议群里。")
    qapp.processEvents()
    assert win.width() == BAR_WIDTH
    assert win.height() == BAR_EXCERPT_HEIGHT
    assert win.x() == compact_x


class TestCloseButton:
    def test_end_emits_stop_when_listening(self, win):
        stops = []
        win.continuous_stop_requested.connect(lambda: stops.append(True))
        win.show_listening()
        win._end_btn.click()
        assert stops == [True]

    def test_listening_close_emits_stop_if_present(self, win):
        if not hasattr(win, "_close_btn"):
            return
        stops = []
        win.continuous_stop_requested.connect(lambda: stops.append(True))
        win.show_listening()
        win._on_close_clicked()
        assert stops == [True]

    def test_close_dismisses_when_idle(self, win):
        win.show_continuous_idle("Alt+Space")
        if hasattr(win, "_on_close_clicked"):
            win._on_close_clicked()
        else:
            win.dismiss_if_idle()
        assert win.isVisible() is False


class TestSubWidgets:
    def test_dot_indicator_pulse_lifecycle(self, qapp):
        dot = _DotIndicator()
        dot.start_pulse()
        assert dot._timer.isActive()
        dot.stop_pulse()
        assert not dot._timer.isActive()

    def test_waveform_start_stop(self, qapp):
        wf = WaveformWidget()
        wf.start()
        assert wf._timer.isActive()
        wf.set_volume(1.0)
        wf._animate()
        wf.stop()
        assert not wf._timer.isActive()
        assert all(h == 0.0 for h in wf._bar_heights)

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
        win.show_polishing("纠错文本", action="纠错")
        assert win._status_label.text() == "纠错中"

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


def test_error_breaks_at_punctuation_instead_of_twelve_characters(win):
    msg = "识别失败，模型推理时发生内部错误"
    win.show_error(msg)
    assert win._status_label.text() == "识别失败"
    assert "模型推理时发生内部错误" in win._text_label.text()
    assert msg in win.toolTip()


def test_warning_text_uses_the_readable_amber(win):
    from sayink.ui.design_tokens import AMBER, AMBER_TEXT

    win.show_warning("会议已暂停")
    css = win._status_label.styleSheet()
    assert AMBER_TEXT.lower() in css.lower()
    assert AMBER.lower() not in css.lower()
    win.show_meeting_paused()
    assert AMBER_TEXT.lower() in win._status_label.styleSheet().lower()

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


def test_end_action_respects_esc_preference_and_preserves_meeting_semantics(win):
    win.set_esc_stops_continuous(False)
    win.show_listening()
    assert "Esc" not in win._end_btn.toolTip()
    assert "继续识别" in win._end_btn.toolTip()
    win.show_meeting()
    win.set_esc_stops_continuous(True)
    assert win._end_btn.accessibleName() == "结束会议"
    assert "Esc" not in win._end_btn.toolTip()
    win.end_meeting_state()
    assert "Esc" in win._end_btn.toolTip()


def test_hold_bar_stays_put_until_the_key_is_released(win):
    from sayink.ui.design_tokens import STATE_RECORD

    win.show_recording()
    held_height = win.height()
    assert "松开识别" in win._text_label.text()

    win.show_recognizing()
    assert win.height() == held_height
    assert win._status_label.text() == "录音中"
    assert "松开识别" in win._text_label.text()
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
    assert "松开识别" not in win._text_label.text()
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
    assert "松开识别" in win._text_label.text()


def test_hold_recording_shows_live_transcript(win):
    win.show_recording()
    win.show_live_transcript("开头" + "啊" * 80 + "这句还在")
    assert "这句还在" in win._text_label.text()
    assert "松开识别" not in win._text_label.text()
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
    """U-12: the excerpt line only adds height; a long session stays bottom-centered."""
    from PyQt6.QtGui import QCursor
    from PyQt6.QtWidgets import QApplication

    from sayink.ui.floating_window import BAR_EXCERPT_HEIGHT, BAR_HEIGHT, BAR_WIDTH
    from sayink.ui.island_chrome import ISLAND_BOTTOM_MARGIN

    screen = QApplication.screenAt(QCursor.pos()) or qapp.primaryScreen()
    assert screen is not None
    geo = screen.availableGeometry()

    win.show_listening()
    qapp.processEvents()
    compact_x = win.x()
    bottom = win.y() + win.height()
    assert win.width() == BAR_WIDTH
    assert win.height() == BAR_HEIGHT
    assert compact_x == geo.x() + (geo.width() - BAR_WIDTH) // 2
    assert bottom == geo.y() + geo.height() - ISLAND_BOTTOM_MARGIN

    win.update_partial_text("下一步把这份纪要贴到会议群里。")
    qapp.processEvents()
    assert win.width() == BAR_WIDTH
    assert win.height() == BAR_EXCERPT_HEIGHT
    assert win.x() == compact_x
    assert win.y() + win.height() == bottom


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


def _mouse(widget, kind, global_pos, buttons):
    from PyQt6.QtCore import QEvent, QPointF, Qt
    from PyQt6.QtGui import QMouseEvent

    local = widget.mapFromGlobal(global_pos)
    event = QMouseEvent(
        kind,
        QPointF(local),
        QPointF(global_pos),
        Qt.MouseButton.LeftButton,
        buttons,
        Qt.KeyboardModifier.NoModifier,
    )
    if kind == QEvent.Type.MouseButtonPress:
        widget.mousePressEvent(event)
    elif kind == QEvent.Type.MouseMove:
        widget.mouseMoveEvent(event)
    elif kind == QEvent.Type.MouseButtonRelease:
        widget.mouseReleaseEvent(event)
    else:
        widget.mouseDoubleClickEvent(event)


def _drag(widget, start, end):
    from PyQt6.QtCore import QEvent, Qt

    _mouse(widget, QEvent.Type.MouseButtonPress, start, Qt.MouseButton.LeftButton)
    _mouse(widget, QEvent.Type.MouseMove, end, Qt.MouseButton.LeftButton)
    _mouse(widget, QEvent.Type.MouseButtonRelease, end, Qt.MouseButton.NoButton)


class TestListenBarPlacement:
    def test_one_shot_stays_near_the_top(self, win, qapp):
        from PyQt6.QtGui import QCursor
        from PyQt6.QtWidgets import QApplication

        win.show_recording()
        screen = QApplication.screenAt(QCursor.pos()) or qapp.primaryScreen()
        geo = screen.availableGeometry()
        assert win.y() <= geo.y() + 140
        assert win.x() == geo.x() + (geo.width() - win.width()) // 2

    def test_model_loading_then_listening_moves_to_the_bottom(self, win, qapp):
        from PyQt6.QtGui import QCursor
        from PyQt6.QtWidgets import QApplication

        from sayink.ui.island_chrome import ISLAND_BOTTOM_MARGIN

        screen = QApplication.screenAt(QCursor.pos()) or qapp.primaryScreen()
        geo = screen.availableGeometry()
        win.show_model_loading("载入中")
        assert win.y() <= geo.y() + 140
        win.show_listening()
        assert win.y() + win.height() == geo.y() + geo.height() - ISLAND_BOTTOM_MARGIN

    def test_later_states_do_not_pull_a_moved_bar_back(self, win):
        from PyQt6.QtCore import QPoint

        win.show_listening()
        start = win.pos()
        _drag(win, start + QPoint(40, 20), start + QPoint(40, -180))
        moved = win.pos()
        assert moved.y() < start.y() - 100
        win.show_success("已输入")
        win.update_partial_text("这句还在底部附近")
        assert win.x() == moved.x()
        assert win.y() < start.y() - 80

    def test_drag_remembers_position_and_a_click_still_opens_the_meeting(self, win, qapp):
        from PyQt6.QtCore import QEvent, QPoint, Qt

        opened = []
        saved = []
        win.meeting_open_requested.connect(lambda: opened.append(1))
        win.position_committed.connect(saved.append)
        win.show_meeting()
        start = win.pos()
        _drag(win, start + QPoint(30, 16), start + QPoint(90, 16))
        assert win.x() == start.x() + 60
        assert saved and "rel_x" in saved[-1] and saved[-1]["screen"]
        assert opened == []

        click = start + QPoint(30, 16)
        _mouse(win, QEvent.Type.MouseButtonPress, click, Qt.MouseButton.LeftButton)
        _mouse(win, QEvent.Type.MouseButtonRelease, click, Qt.MouseButton.NoButton)
        assert opened == []
        assert win._meeting_click_timer.isActive()
        win._meeting_click_timer.stop()
        win._open_meeting_from_click()
        assert opened == [1]

    def test_double_click_restores_the_default_and_does_not_open_the_meeting(self, win, qapp):
        from PyQt6.QtCore import QEvent, QPoint, Qt

        opened = []
        reset = []
        win.meeting_open_requested.connect(lambda: opened.append(1))
        win.placement_reset.connect(lambda: reset.append(1))
        win.show_meeting()
        start = win.pos()
        press = start + QPoint(24, 12)
        _drag(win, press, press + QPoint(0, -160))
        assert win.y() < start.y() - 100
        _mouse(win, QEvent.Type.MouseButtonPress, press, Qt.MouseButton.LeftButton)
        _mouse(win, QEvent.Type.MouseButtonRelease, press, Qt.MouseButton.NoButton)
        _mouse(win, QEvent.Type.MouseButtonDblClick, press, Qt.MouseButton.LeftButton)
        qapp.processEvents()
        assert opened == []
        assert reset == [1]
        assert abs(win.y() - start.y()) <= 2
        assert [action.text() for action in win._position_menu().actions()] == ["恢复默认位置"]

    def test_saved_position_is_reused_for_the_next_session(self, win, qapp):
        from PyQt6.QtGui import QCursor
        from PyQt6.QtWidgets import QApplication

        from sayink.ui.island_chrome import origin_from_relative, screen_storage_key

        screen = QApplication.screenAt(QCursor.pos()) or qapp.primaryScreen()
        geo = screen.availableGeometry()
        saved = {"rel_x": 0.05, "rel_y": 0.2, "anchor": "top"}
        win.set_placement_preference("last")
        win.set_saved_positions({screen_storage_key(screen): saved})
        win.show_listening()
        expected = origin_from_relative(geo, win.width(), win.height(), saved)
        assert (win.x(), win.y()) == expected

    def test_idle_excerpt_collapses_and_hover_restores_it(self, win):
        from PyQt6.QtCore import QPointF
        from PyQt6.QtGui import QEnterEvent

        win.set_autohide(True)
        win.set_autohide_idle_ms(30)
        win.show_listening()
        win.update_partial_text("这行字会先露出来")
        assert win._text_label.isVisible()
        assert win._idle_timer.isActive()
        win.underMouse = lambda: False
        win._collapse_idle_excerpt()
        assert not win._text_label.isVisible()
        assert win._end_btn.isVisible()
        win.enterEvent(QEnterEvent(QPointF(4, 4), QPointF(4, 4), QPointF(4, 4)))
        assert win._text_label.isVisible()
        assert "这行字" in win._text_label.text() or "露出来" in win._text_label.text()

    def test_one_shot_excerpt_does_not_autohide(self, win, qapp):
        import time

        win.set_autohide(True)
        win.set_autohide_idle_ms(30)
        win.show_recording()
        assert "松开识别" in win._text_label.text()
        deadline = time.monotonic() + 0.2
        while time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        assert win._text_label.isVisible()
        assert "松开识别" in win._text_label.text()


def test_app_loads_listen_bar_preferences(qapp):
    from tests.helpers.app_harness import app_harness

    with app_harness({
        "appearance.listen_bar_placement": "bottom_right",
        "appearance.listen_bar_autohide": False,
        "appearance.listen_bar_position": {"DISPLAY1": {"rel_x": 0.2, "rel_y": 0.8, "anchor": "bottom"}},
    }) as harness:
        floating = harness["floating"]
        floating.set_placement_preference.assert_any_call("bottom_right")
        floating.set_autohide.assert_any_call(False)
        floating.set_saved_positions.assert_called()

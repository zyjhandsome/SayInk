"""Comprehensive verification suite for UI shell, design system, floating island,
settings center, and central orchestration audit fixes."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from PyQt6.QtCore import QEvent, QPoint, QPointF, QRect, Qt
from PyQt6.QtGui import QColor, QMouseEvent, QWheelEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import (
    QApplication,
    QDoubleSpinBox,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from sayink.audio_devices import INPUT_SOURCE_MIXED
from sayink.config import TRIGGER_MODE_CONTINUOUS, Config
from sayink.output_pipeline import OutputPipeline
from sayink.recognition_settings import RecognitionSettings
from sayink.reload_coordinator import ReloadCoordinator
from sayink.runtime_status import OutputStage
from sayink.text_paster import PasteResult
from sayink.ui import app_styles, settings_styles
from sayink.ui import design_tokens as tok
from sayink.ui.audio_recovery_dialog import AudioRecoveryDialog
from sayink.ui.clipboard import copy_text
from sayink.ui.floating_window import FloatingWindow
from sayink.ui.flow_layout import FlowLayout
from sayink.ui.hotkey_edit import HotkeyEdit, hotkey_conflict_warning
from sayink.ui.island_chrome import ISLAND_SHADOW_BLUR
from sayink.ui.prompts import text_prompt_box
from sayink.ui.settings_components import ThemeModeSegment
from sayink.ui.settings_pages.general import configure_numeric_spin
from sayink.ui.settings_window import SettingsWindow, _ModelDirMoveWorker
from sayink.ui.theme import apply_theme
from sayink.ui.tray_icon import _PIXMAP_CACHE, _microphone_pixmap
from tests.helpers.app_harness import app_harness
from tests.test_main_window import _make_main_window


def _relative_luminance(hex_color: str) -> float:
    c = QColor(hex_color)
    channels = []
    for raw in (c.redF(), c.greenF(), c.blueF()):
        channels.append(raw / 12.92 if raw <= 0.03928 else ((raw + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _contrast_ratio(fg: str, bg: str) -> float:
    l1 = _relative_luminance(fg)
    l2 = _relative_luminance(bg)
    hi, lo = (l1, l2) if l1 >= l2 else (l2, l1)
    return (hi + 0.05) / (lo + 0.05)


@pytest.fixture
def fast_settings_window(config, monkeypatch):
    monkeypatch.setattr(SettingsWindow, "_rebuild_model_cards", lambda self: None)
    monkeypatch.setattr(SettingsWindow, "_refresh_about_info", lambda self: None)
    monkeypatch.setattr(SettingsWindow, "_refresh_audio_device_lists", lambda self: None)
    win = SettingsWindow(config)
    yield win
    win.close()


# ── 1. C-06: SpinBox Wheel Guard & History Cleanup Safeguard ──────────


class TestSpinBoxWheelAndHistoryLimitsC06:
    def test_unfocused_spinbox_wheel_forwards_to_scroll_area_without_mutation(self, _qapp_session):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.resize(300, 120)
        host = QWidget()
        lay = QVBoxLayout(host)
        for _ in range(10):
            spacer = QWidget()
            spacer.setMinimumHeight(40)
            lay.addWidget(spacer)
        spin = QSpinBox()
        spin.setRange(1, 100)
        spin.setValue(50)
        configure_numeric_spin(spin)
        app_styles.install_spinbox_wheel_guard(spin)
        dspin = QDoubleSpinBox()
        dspin.setRange(0.0, 10.0)
        dspin.setValue(2.5)
        configure_numeric_spin(dspin)
        app_styles.install_spinbox_wheel_guard(dspin)
        lay.addWidget(spin)
        lay.addWidget(dspin)
        scroll.setWidget(host)
        scroll.show()
        _qapp_session.processEvents()

        spin.clearFocus()
        dspin.clearFocus()
        wheel = QWheelEvent(
            QPointF(10, 10),
            QPointF(10, 10),
            QPoint(0, 0),
            QPoint(0, -120),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        )
        QApplication.sendEvent(spin, wheel)
        QApplication.sendEvent(dspin, wheel)
        assert spin.value() == 50
        assert dspin.value() == pytest.approx(2.5)
        scroll.close()

    def test_lowering_history_limits_prompts_when_existing_sessions_affected(
        self, fast_settings_window, config, monkeypatch
    ):
        now_ms = 1_800_000_000_000
        monkeypatch.setattr("sayink.ui.settings_window.time.time", lambda: now_ms / 1000.0)
        sessions = [
            SimpleNamespace(created_at=now_ms - i * 10 * 86_400_000)
            for i in range(6)
        ]
        store = SimpleNamespace(list_sessions=lambda limit=100_000, offset=0: sessions)
        fast_settings_window.set_history_store(store)

        # Declining reverts spinbox and keeps config untouched
        prompts = []
        monkeypatch.setattr(
            fast_settings_window,
            "_confirm",
            lambda title, text, **kw: prompts.append((title, text)) or False,
        )
        fast_settings_window.general_page.history_max_entries_spin.setValue(2)
        assert prompts
        assert config.get("history.max_entries") == 5000
        assert fast_settings_window.general_page.history_max_entries_spin.value() == 5000

        # Accepting commits the lower limit
        monkeypatch.setattr(fast_settings_window, "_confirm", lambda *_a, **_kw: True)
        fast_settings_window.general_page.history_max_entries_spin.setValue(2)
        assert config.get("history.max_entries") == 2


# ── 2. C-08: Mixed Audio Dual Meters ──────────────────────────────────


class TestMixedAudioDualMetersC08:
    def test_mixed_probe_renders_independent_mic_and_system_meters(
        self, fast_settings_window, config, monkeypatch
    ):
        monkeypatch.setattr(fast_settings_window._mic_test_recorder, "start", lambda: None)
        monkeypatch.setattr(fast_settings_window._mic_test_recorder, "stop", lambda: None)
        config.set("audio.input_source", INPUT_SOURCE_MIXED)
        fast_settings_window.reload_settings(refresh_devices=False, heavy=False)

        fast_settings_window._run_mic_probe()
        gp = fast_settings_window.general_page
        assert not gp.mic_level_wrap.isHidden()
        assert not gp.system_level_wrap.isHidden()
        assert gp.mic_level_label.text() == "麦克风"
        assert gp.system_level_label.text() == "电脑声"

        fast_settings_window._mic_test_recorder._block_tracks = {
            "microphone": np.full(1600, 0.25, dtype=np.float32),
            "system": np.zeros(1600, dtype=np.float32),
        }
        fast_settings_window._on_mic_probe_volume(0.1)
        assert gp.mic_level_bar.value() > 50
        assert gp.system_level_bar.value() == 0

        fast_settings_window._finish_mic_probe()
        assert "已检测到麦克风，未检测到电脑声" in gp.mic_test_status.text()


# ── 3. C-09, M-18, M-24: Viewport Clamping & AudioRecoveryDialog ──────


class TestViewportClampingAndRecoveryDialog:
    def test_main_window_clamps_minimum_size_on_1366x768_150pct_dpi(self, tmp_path, monkeypatch):
        from sayink.ui.main_window import MainWindow

        fake_screen = MagicMock()
        fake_screen.availableGeometry.return_value = QRect(0, 0, 910, 472)
        monkeypatch.setattr(MainWindow, "screen", lambda self: fake_screen)
        win, store = _make_main_window(tmp_path, monkeypatch)
        try:
            assert win.minimumWidth() <= 910
            assert win.minimumHeight() <= 472
            assert win.width() <= 910
            assert win.height() <= 472
        finally:
            win.close()
            store.close()

    def test_audio_recovery_dialog_clamps_height_disables_empty_export_and_reapplies_theme(
        self, tmp_path, monkeypatch, _qapp_session
    ):
        fake_screen = MagicMock()
        fake_screen.availableGeometry.return_value = QRect(0, 0, 820, 440)
        monkeypatch.setattr(AudioRecoveryDialog, "screen", lambda self: fake_screen)
        monkeypatch.setattr("sayink.ui.audio_recovery_dialog.recordings", lambda _d: [])

        dlg = AudioRecoveryDialog(tmp_path, recognizer=MagicMock())
        try:
            assert dlg.minimumWidth() <= 820
            assert dlg.minimumHeight() <= 440
            assert dlg.height() <= 440
            assert not dlg.save_btn.isEnabled()
            dlg.output.setPlainText("恢复出的文本")
            assert dlg.save_btn.isEnabled()
            apply_theme(_qapp_session, mode="dark", surfaces=[dlg])
            first_css_line = next(line for line in settings_styles.WINDOW_CSS.splitlines() if line.strip())
            assert first_css_line in dlg.styleSheet()
        finally:
            dlg.close()


# ── 4. Orchestration, OutputPipeline & ReloadCoordinator ──────────────


class TestOrchestrationAndPipelineFixes:
    def test_h01_drop_unrecognized_meeting_audio_keeps_continuous_segments(self):
        with app_harness({"audio.trigger_mode": TRIGGER_MODE_CONTINUOUS}) as h:
            app = h["app"]
            cont_ctx = SimpleNamespace(trigger_mode=TRIGGER_MODE_CONTINUOUS)
            meet_ctx = SimpleNamespace(trigger_mode="meeting")
            app._segment_queue.append((np.zeros(800, dtype=np.float32), cont_ctx))
            app._segment_queue.append((np.zeros(800, dtype=np.float32), meet_ctx))

            dropped = app._drop_unrecognized_meeting_audio()
            assert dropped == 1
            assert len(app._segment_queue) == 1
            assert app._segment_queue[0][1].trigger_mode == TRIGGER_MODE_CONTINUOUS

    def test_h01_pipeline_never_ghost_pastes_into_active_meeting(self, tmp_path):
        copied = []
        floating = MagicMock()
        paster = MagicMock()
        history = MagicMock()
        cfg = Config(config_dir=tmp_path)
        cfg.set("history.enabled", False)
        pipeline = OutputPipeline(
            config=cfg,
            polisher=MagicMock(),
            paster=paster,
            history=history,
            floating=floating,
            tray=MagicMock(),
            meeting_active=lambda: True,
            copy_to_clipboard=copied.append,
        )
        pipeline.output_text("遗留口述分片")
        paster.paste_async.assert_not_called()
        assert copied == ["遗留口述分片"]

    def test_h02_pump_segment_oserror_in_meeting_does_not_flag_continuous_stopped(self):
        with app_harness() as h:
            app = h["app"]
            app._continuous_user_stopped = False
            h["recorder"].is_recording = True
            h["recorder"].is_continuous = True
            fake_meeting = MagicMock(is_active=True, importing_media=False, paused_detail="已暂停")
            app._meeting = fake_meeting
            app._enqueue_audio(np.zeros(800, dtype=np.float32))
            with (
                patch.object(app, "_meeting_active", return_value=True),
                patch.object(app, "_pop_queued_audio", side_effect=OSError("disk read error")),
            ):
                app._pump_segment_queue()
            assert app._continuous_user_stopped is False
            fake_meeting.pause.assert_called_once()

    def test_m02_reload_coordinator_preserves_pending_segmenter_diff_on_audio_commit(self):
        cfg = MagicMock()
        state = {
            "stt.model_id": "sense-voice-small",
            "stt.language": "auto",
            "stt.vad": "silero",
            "stt.boundary_context_enabled": False,
            "stt.glossary": [],
            "audio.input_source": "microphone",
            "audio.trigger_mode": "hotkey",
            "audio.mic_device_index": -1,
            "audio.system_device_index": -1,
            "audio.separate_lanes": False,
            "audio.recovery_enabled": False,
        }
        cfg.get.side_effect = lambda k, d=None: state.get(k, d)
        coord = ReloadCoordinator(
            config=cfg,
            recorder=MagicMock(is_recording=False, is_continuous=False),
            recognizer=MagicMock(is_ready=True, is_loading=False),
            floating=MagicMock(),
            tray=MagicMock(),
            sound=MagicMock(),
            paster=MagicMock(),
            aux_model_path_fn=lambda _m: None,
            is_model_downloaded_fn=lambda _m: True,
        )
        baseline = RecognitionSettings.from_config(cfg)
        coord.applied_audio_key = coord.audio_runtime_key()
        coord.applied_recognition_settings = replace(baseline, vad="energy")
        coord.commit_audio_settings(clear_queue=False)
        # Pending vad="energy" -> "silero" diff must be applied to the segmenter, not swallowed
        coord.recorder.set_segmenter_profile.assert_called_once_with(
            vad="silero", vad_model_path=None, meeting=False
        )
        assert coord.applied_recognition_settings.vad == "silero"

    def test_h05_pipeline_renews_watchdog_on_paste_and_ignores_stale_paste_callback(self):
        paster = MagicMock()
        floating = MagicMock()
        polisher = MagicMock()
        cfg = MagicMock()
        store = {
            "llm.enabled": True,
            "llm.api_url": "https://api.example.com/v1",
            "llm.api_key": "sk-1",
            "llm.model_name": "m",
            "llm.mode": "polish",
            "llm.proxy_mode": "system",
            "llm.proxy_bypass_suffixes": "",
            "output.restore_clipboard": False,
            "history.enabled": False,
        }
        cfg.get.side_effect = lambda k, d=None: store.get(k, d)
        cfg.history_only_output.return_value = False
        watchdog_starts = []
        pipeline = OutputPipeline(
            config=cfg,
            polisher=polisher,
            paster=paster,
            history=MagicMock(),
            floating=floating,
            tray=MagicMock(),
            schedule_later=lambda ms, fn: watchdog_starts.append((ms, fn)),
        )

        pipeline.deliver_recognized_text("测试文本")
        assert pipeline.stage == OutputStage.POLISH
        first_count = len(watchdog_starts)

        pipeline.on_polish_complete("润色后文本")
        assert pipeline.stage == OutputStage.PASTE
        assert len(watchdog_starts) > first_count
        paste_cb = paster.paste_async.call_args.args[1]
        paste_token = pipeline.token

        # Simulate watchdog timeout invalidating paste_token, then start a new paste
        pipeline.release_stuck_output(paste_token)
        assert pipeline.stage == OutputStage.IDLE
        pipeline.output_text("下一句")
        assert pipeline.stage == OutputStage.PASTE

        # Late callback from the timed-out paste must be ignored completely
        paste_cb(PasteResult("sent"))
        assert pipeline.stage == OutputStage.PASTE

    def test_h12_h25_actionable_paste_refusals_and_polish_fallback_notice(self):
        paster = MagicMock()
        floating = MagicMock()
        cfg = MagicMock()
        cfg.get.side_effect = lambda k, d=None: False if k == "history.enabled" else d
        cfg.history_only_output.return_value = False

        for detail, expected in (
            ("elevated", "目标为管理员窗口，已复制，请按 Ctrl+V"),
            ("ime_active", "检测到正在拼音选词，已复制，请按 Ctrl+V"),
            ("session_changed", "桌面会话已切换，已复制，请按 Ctrl+V"),
        ):
            floating.reset_mock()
            pipeline = OutputPipeline(
                config=cfg,
                polisher=MagicMock(),
                paster=paster,
                history=MagicMock(),
                floating=floating,
                tray=MagicMock(),
            )
            paster.paste_async.side_effect = lambda _t, cb, _d=detail, **_kw: cb(
                PasteResult("clipboard", target_app="Code", detail=_d)
            )
            pipeline.output_text("你好")
            shown_args = floating.show_info.call_args.args
            assert expected in " ".join(str(a) for a in shown_args)

        # H-25: Polish error fallback must clearly report polish failure
        floating.reset_mock()
        pipeline = OutputPipeline(
            config=cfg,
            polisher=MagicMock(),
            paster=paster,
            history=MagicMock(),
            floating=floating,
            tray=MagicMock(),
        )
        pipeline.stage = OutputStage.POLISH
        pipeline.raw_text = "原始句子"
        paster.paste_async.side_effect = lambda _t, cb, **_kw: cb(
            PasteResult("sent", target_app="Notepad")
        )
        pipeline.on_polish_error("HTTP 401 Unauthorized")
        info_text = " ".join(str(a) for a in floating.show_info.call_args.args)
        assert "润色未生效（密钥/网络异常），已发送原文" in info_text

    def test_h09_h10_h13_h18_h31_app_guards(self):
        with app_harness({"audio.trigger_mode": "hotkey", "llm.enabled": False}) as h:
            app = h["app"]
            # H-09: _meeting_refusal blocks while hold-to-talk open is pending
            app._hold_open_pending = True
            assert app._meeting_refusal(MagicMock()) != ""
            app._hold_open_pending = False

            # H-18: hold-to-talk starts recorder BEFORE painting floating bar
            call_order = []
            h["recorder"].start.side_effect = lambda **_kw: call_order.append("recorder.start")
            h["floating"].show_recording.side_effect = lambda *_a, **_kw: call_order.append("floating.show")
            app._on_recording_start()
            assert call_order[:2] == ["recorder.start", "floating.show"]

            # H-31: file transcription model resolution prefers meeting.model_id
            h["store"]["stt"]["model_id"] = "sense-voice-small"
            h["store"]["meeting"]["model_id"] = "firered-asr"
            with patch.object(app, "_is_model_downloaded", return_value=True):
                assert app._resolve_media_import_model_id() == "firered-asr"


# ── 5. Floating Island Polish (H-36..H-42, H-50, M-20..M-22) ──────────


class TestFloatingIslandPolish:
    def test_h36_h37_meeting_bar_keeps_end_btn_on_model_load_and_preserves_live_text(self):
        bar = FloatingWindow()
        try:
            bar.show_meeting("00:01:12")
            bar.show_live_transcript("持久会议实时字幕")
            assert not bar._end_btn.isHidden()

            # H-36: show_model_loading during meeting never hides _end_btn
            bar.show_model_loading("正在切换模型")
            assert not bar._end_btn.isHidden()
            assert bar.meeting_active

            # H-37: transient warning/info preserves meeting title and does not clobber _live_text
            bar.clear_model_loading_lock()
            bar.show_info("已标记重点", "00:01:15")
            assert "会议中" in bar._full_status_text
            assert "已标记重点" in bar._text_label.toolTip()
            assert bar._live_text == "持久会议实时字幕"
            bar._restore_after_transient_meeting_notice()
            assert "持久会议实时字幕" in bar._text_label.toolTip()
        finally:
            bar.close()

    def test_h38_h40_context_menu_actions_and_single_click_meeting_open(self):
        bar = FloatingWindow()
        try:
            menu = bar._position_menu()
            assert menu.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
            labels = [a.text() for a in menu.actions() if not a.isSeparator()]
            assert labels == ["打开历史记录", "打开设置", "重置悬浮窗位置"]

            hist, settings_req, reset_req = [], [], []
            bar.history_requested.connect(lambda: hist.append(True))
            bar.settings_requested.connect(lambda: settings_req.append(True))
            bar.placement_reset.connect(lambda: reset_req.append(True))
            for action in menu.actions():
                if not action.isSeparator():
                    action.trigger()
            assert hist == [True]
            assert settings_req == [True]
            assert reset_req == [True]
            menu.close()

            # H-40: single click opens meeting immediately without timer delay
            bar.show_meeting("00:00:05")
            opened = []
            bar.meeting_open_requested.connect(lambda: opened.append(True))
            press = QMouseEvent(
                QEvent.Type.MouseButtonPress,
                QPointF(20, 20),
                QPointF(120, 120),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            release = QMouseEvent(
                QEvent.Type.MouseButtonRelease,
                QPointF(20, 20),
                QPointF(120, 120),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
            )
            bar.mousePressEvent(press)
            bar.mouseReleaseEvent(release)
            assert opened == [True]
        finally:
            bar.close()

    def test_h41_h42_h50_m22_dual_channel_glyphs_cancel_buttons_and_shadow_blur(self):
        assert ISLAND_SHADOW_BLUR <= 16
        bar = FloatingWindow()
        try:
            glyphs = set()
            for state in ("STATE_RECORD", "STATE_RECOGNIZE", "STATE_POLISH", "STATE_SUCCESS", "STATE_ERROR"):
                bar._set_state("状态", state)
                glyphs.add(bar._dot.state_glyph_kind())
            assert len(glyphs) == 5

            # H-42: single-shot recording shows 取消; continuous with text shows 放弃本句
            bar.show_recording()
            assert not bar._cancel_btn.isHidden()
            assert bar._cancel_btn.text() == "取消"

            bar.show_listening("Visual Studio Code")
            assert "Visual Studio Code" in bar._status_label.text()
            bar.show_live_transcript("正在说的话")
            assert not bar._cancel_btn.isHidden()
            assert bar._cancel_btn.text() == "放弃本句"

            # H-50: meeting 标记 vs 结束 distinct styles & >=14px spacing
            bar.show_meeting("00:00:10")
            assert bar._mark_btn.styleSheet() != bar._end_btn.styleSheet()
            assert tok.STATE_RECORD.lower() in bar._end_btn.styleSheet().lower()
        finally:
            bar.close()


# ── 6. Settings Center, HotkeyEdit, Contrast & Non-Modal UX ───────────


class TestSettingsAccessibilityAndSafeguards:
    def test_h43_hotkey_edit_win_modifier_backspace_clear_tab_navigation_and_conflict(self):
        host = QWidget()
        lay = QVBoxLayout(host)
        edit = HotkeyEdit("alt+x")
        other = QSpinBox()
        lay.addWidget(edit)
        lay.addWidget(other)
        host.show()
        QApplication.processEvents()
        try:
            emitted = []
            edit.hotkey_changed.connect(emitted.append)

            # Win+Shift+R capture (canonical modifier order: shift+win+r)
            edit.begin_capture()
            QTest.keyClick(
                edit,
                Qt.Key.Key_R,
                Qt.KeyboardModifier.MetaModifier | Qt.KeyboardModifier.ShiftModifier,
            )
            assert edit.value == "shift+win+r"
            assert emitted[-1] == "shift+win+r"

            # Backspace clears binding
            edit.begin_capture()
            QTest.keyClick(edit, Qt.Key.Key_Backspace)
            assert edit.value == ""
            assert emitted[-1] == ""

            # Tab exits capture cleanly without binding "tab"
            edit.set_value("alt+x")
            edit.begin_capture()
            QTest.keyClick(edit, Qt.Key.Key_Tab)
            assert not edit._capturing
            assert edit.value == "alt+x"

            # Common conflict warning & no Alt+Space recommendation
            assert hotkey_conflict_warning("ctrl+shift+v") != ""
            app_src = Path("sayink/app.py").read_text(encoding="utf-8")
            about_src = Path("sayink/ui/settings_pages/about.py").read_text(encoding="utf-8")
            assert "Alt + Space" not in app_src
            assert "Alt+Space" not in about_src
        finally:
            host.close()

    def test_h44_wcag_aa_contrast_in_light_and_dark_axes_and_allowed_weights(self):
        for axis in ("light", "dark"):
            tok.activate(axis)
            assert _contrast_ratio(tok.GREEN_TEXT, tok.GREEN_BG) >= 4.5
            assert _contrast_ratio(tok.GREEN_TEXT, tok.SURFACE) >= 4.5
            assert _contrast_ratio(tok.RED_TEXT, tok.SURFACE) >= 4.5
            assert _contrast_ratio(tok.TEXT_SEC, tok.BG) >= 4.5
            assert _contrast_ratio(tok.TEXT_SEC, tok.NAV_BG) >= 4.5
            if axis == "dark":
                assert _contrast_ratio(tok.ACCENT_TEXT, tok.SURFACE_PEARL) >= 4.5
        tok.activate("light")

        polish_src = Path("sayink/ui/settings_pages/polish.py").read_text(encoding="utf-8")
        assert "font-weight: 600" not in polish_src
        assert "font-weight: 500" not in polish_src

    def test_h45_h46_m05_collapsible_groups_proxy_jump_glossary_clear_and_save_failed(
        self, fast_settings_window, config, monkeypatch
    ):
        gp = fast_settings_window.general_page
        assert gp.advanced_mixing_panel.isHidden()
        assert gp.advanced_network_panel.isHidden()
        assert gp.advanced_recovery_panel.isHidden()

        # Proxy link on Polish page jumps to General page & expands network panel
        fast_settings_window.show_page(2)
        fast_settings_window.polish_page.proxy_jump_btn.click()
        assert fast_settings_window._pages.currentIndex() == 0
        assert not gp.advanced_network_panel.isHidden()

        # Device enumeration error never leaks raw Python exception text
        def _boom():
            raise RuntimeError("PortAudio secret internal error 0xDEAD")

        gp.populate_audio_device_combos(_boom, _boom)
        combo_items = [gp.mic_device_combo.itemText(i) for i in range(gp.mic_device_combo.count())]
        assert all("0xDEAD" not in item for item in combo_items)
        assert "读取受限" in gp.audio_enum_warning

        # Glossary clear asks confirmation first
        gp.meeting_glossary_edit.setPlainText("SayInk\nParaformer")
        monkeypatch.setattr(fast_settings_window, "_confirm", lambda *_a, **_kw: False)
        fast_settings_window._clear_meeting_glossary()
        assert "SayInk" in gp.meeting_glossary_edit.toPlainText()
        monkeypatch.setattr(fast_settings_window, "_confirm", lambda *_a, **_kw: True)
        fast_settings_window._clear_meeting_glossary()
        assert gp.meeting_glossary_edit.toPlainText() == ""

        # M-05: last_save_failed surfaces warning instead of "已保存"
        monkeypatch.setattr(config, "last_save_failed", True, raising=False)
        fast_settings_window._update_llm_configuration_status()
        assert "写入磁盘失败" in fast_settings_window.polish_page.llm_test_status.text()
        assert fast_settings_window.polish_page.llm_test_status.property("viTone") == "error"

    def test_h47_background_model_dir_move_skips_existing_without_clobbering(self, tmp_path):
        src_root = tmp_path / "old_models"
        dst_root = tmp_path / "new_models"
        (src_root / "model_a").mkdir(parents=True)
        (src_root / "model_a" / "weights.bin").write_bytes(b"123")
        (src_root / "model_b").mkdir(parents=True)
        (dst_root / "model_b").mkdir(parents=True)

        results = []
        worker = _ModelDirMoveWorker([src_root / "model_a", src_root / "model_b"], dst_root)
        worker.finished_move.connect(lambda d, m, s, f: results.append((d, m, s, f)))
        worker.run()
        assert len(results) == 1
        _dest, moved, skipped, failed = results[0]
        assert moved == 1
        assert skipped == ["model_b"]
        assert failed == []
        assert (dst_root / "model_a" / "weights.bin").is_file()
        assert (src_root / "model_b").is_dir()

    def test_h50_text_prompt_box_uses_primary_btn_and_inline_error(self):
        from PyQt6.QtWidgets import QDialogButtonBox

        dlg, edit = text_prompt_box(
            None,
            "重命名会话",
            "请输入会话名称",
            text="",
            validator=lambda s: "" if s.strip() else "名称不能为空",
        )
        try:
            box = dlg.findChild(QDialogButtonBox)
            save_btn = box.buttons()[0]
            assert save_btn.styleSheet() == settings_styles.BTN_PRIMARY
            edit.setText("")
            save_btn.click()
            assert not dlg._error_label.isHidden()
            assert "名称不能为空" in dlg._error_label.text()
            assert tok.RED_TEXT.lower() in edit.styleSheet().lower()
        finally:
            dlg.close()

    def test_m23_m27_l06_l07_flow_layout_theme_arrows_tray_cache_and_clipboard(self, monkeypatch, caplog):
        # M-23: FlowLayout honors margin & spacing
        host = QWidget()
        fl = FlowLayout(host, margin=12, spacing=10)
        m = fl.contentsMargins()
        assert (m.left(), m.top(), m.right(), m.bottom()) == (12, 12, 12, 12)
        assert fl.spacing() == 10
        host.close()

        # M-27: ThemeModeSegment Left/Right arrow keys switch active theme
        seg = ThemeModeSegment()
        seg.show()
        seg.setCurrentIndex(0)
        QTest.keyClick(seg._buttons[0], Qt.Key.Key_Right)
        assert seg.currentIndex() == 1
        QTest.keyClick(seg._buttons[1], Qt.Key.Key_Left)
        assert seg.currentIndex() == 0
        seg.close()

        # L-06: TrayIcon caches rendered pixmaps
        _PIXMAP_CACHE.clear()
        p1 = _microphone_pixmap("#2E6CE6", False, 24)
        p2 = _microphone_pixmap("#2E6CE6", False, 24)
        assert p1.cacheKey() == p2.cacheKey()
        assert len(_PIXMAP_CACHE) == 1

        # L-07: copy_text logs warning when clipboard fails
        fake_cb = SimpleNamespace(setText=lambda _t: None, text=lambda: "")
        monkeypatch.setattr("sayink.ui.clipboard._pump", lambda _ms: None)
        monkeypatch.setattr(QApplication, "clipboard", staticmethod(lambda: fake_cb))
        with caplog.at_level("WARNING", logger="SayInk"):
            ok = copy_text("hello")
        assert ok is False
        assert any("剪贴板" in r.message for r in caplog.records)

    def test_all_vi_roles_ast_tokens_and_downloaded_model_settings_theme_switch(
        self, monkeypatch, tmp_path
    ):
        import ast
        from pathlib import Path
        from sayink import config as config_mod
        from sayink.config import Config
        from sayink.ui import design_tokens as dt
        from sayink.ui import settings_styles as ss
        from sayink.ui.settings_components import stylesheet_for_role
        from sayink.ui.settings_window import SettingsWindow

        # 1) _LIGHT and _DARK token sets must match 1:1 and cover every AST tok/live/ss reference
        assert set(dt._LIGHT.keys()) == set(dt._DARK.keys())
        assert "GREEN_BORDER" in dt._LIGHT and "GREEN_BORDER" in dt._DARK

        repo_sayink = Path(__file__).resolve().parents[1] / "sayink"
        missing_refs: list[str] = []
        for py_file in repo_sayink.rglob("*.py"):
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                    if node.value.id in ("tok", "live", "design_tokens", "dt") and not hasattr(dt, node.attr):
                        missing_refs.append(f"{py_file.name}:{node.lineno}:{node.value.id}.{node.attr}")
                    elif node.value.id in ("ss", "settings_styles") and not hasattr(ss, node.attr):
                        missing_refs.append(f"{py_file.name}:{node.lineno}:{node.value.id}.{node.attr}")
        assert missing_refs == []

        # 2) Every viRole in stylesheet_for_role renders valid QSS in both light and dark themes
        all_roles = (
            "pageTitle",
            "pageSubtitle",
            "sectionLabel",
            "rowTitle",
            "rowSubtitle",
            "fieldLabel",
            "hint",
            "importantHint",
            "footnote",
            "polishPreviewHeading",
            "polishPreviewLabel",
            "polishPreviewText",
            "kvKey",
            "kvValue",
            "kvValueMono",
            "pickTitle",
            "pickSubtitle",
            "emptyState",
            "engineHeroTitle",
            "modelName",
            "engineHeroBadge",
            "modelBadge",
            "engineHeroSize",
            "engineHeroMeta",
            "modelSize",
            "modelMeta",
            "engineHeroDesc",
            "modelDesc",
        )
        try:
            for axis in ("light", "dark"):
                dt.activate(axis)
                for role in all_roles:
                    css = stylesheet_for_role(role)
                    assert isinstance(css, str) and css.strip(), f"empty css for {role} in {axis}"
                assert stylesheet_for_role(None) is None
                assert stylesheet_for_role("unknownRole") is None
        finally:
            dt.activate("light")

        # 3) SettingsWindow with is_model_downloaded=True builds engineHeroBadge & switches light/dark cleanly
        monkeypatch.setattr(config_mod, "user_data_dir", lambda: tmp_path)
        monkeypatch.setattr("sayink.speech_recognizer.is_model_downloaded", lambda _mid: True)
        cfg = Config(secret_store=SimpleNamespace(read=lambda: "", write=lambda _v: True))
        sw = SettingsWindow(cfg)
        try:
            assert any(w.property("viRole") == "engineHeroBadge" for w in sw.findChildren(QWidget))
            dt.activate("dark")
            sw.reapply_theme()
            dt.activate("light")
            sw.reapply_theme()
        finally:
            sw.close()
            sw.deleteLater()
            dt.activate("light")

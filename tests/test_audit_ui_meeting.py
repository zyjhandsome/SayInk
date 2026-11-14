"""Comprehensive regression and offscreen geometry/contrast audit for meeting UI,
history window, dialogs, and benchmark tooling (C-08/C-10/C-11, H-29/H-46/H-49..H-56,
M-16/M-28..M-31, L-03).
"""

from __future__ import annotations

import time
import wave
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QMimeData, QPoint, QRect, Qt, QUrl
from PyQt6.QtGui import QColor, QDragEnterEvent, QDropEvent
from PyQt6.QtWidgets import QApplication, QWidget

from sayink.config import Config
from sayink.history_store import HistoryStore, SegmentRecord
from sayink.meeting_session import (
    MeetingController,
    MeetingOptions,
    MeetingState,
    resolve_capabilities,
)
from sayink.ui import design_tokens as tok
from sayink.ui import settings_styles as ss
from sayink.ui.history_window import HistoryWindow
from sayink.ui.main_window import MainWindow
from sayink.ui.meeting_page import PAGE_LIVE, PAGE_START, MeetingPage
from sayink.ui.meeting_views import _SegmentTextDialog, _with_export_suffix
from sayink.ui.minutes_templates import MinutesTemplatesDialog, TemplateEditorDialog
from sayink.ui.settings_window import SettingsWindow
from sayink.ui.theme import apply_theme
from sayink.ui.transcript_import_dialog import TranscriptImportDialog
from tools.asr_bench import hotword_hits


def _rel_lum(color: QColor) -> float:
    def _chan(v: int) -> float:
        s = v / 255.0
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

    return 0.2126 * _chan(color.red()) + 0.7152 * _chan(color.green()) + 0.0722 * _chan(color.blue())


def _contrast_ratio(c1: QColor, c2: QColor) -> float:
    l1, l2 = _rel_lum(c1), _rel_lum(c2)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def _settle(rounds: int = 4) -> None:
    app = QApplication.instance()
    if app is not None:
        for _ in range(rounds):
            app.processEvents()


def _write_wav(path: Path, seconds: float = 1.5, rate: int = 16000) -> Path:
    n = int(rate * seconds)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * n)
    return path


def _global_rect(widget: QWidget) -> QRect:
    top_left = widget.mapToGlobal(QPoint(0, 0))
    return QRect(top_left.x(), top_left.y(), widget.width(), widget.height())


def _assert_no_overlap(widgets: list[QWidget], context: str) -> None:
    visible = [w for w in widgets if w.isVisible() and w.width() > 0 and w.height() > 0]
    for i in range(len(visible)):
        r1 = _global_rect(visible[i]).adjusted(1, 1, -1, -1)
        for j in range(i + 1, len(visible)):
            r2 = _global_rect(visible[j]).adjusted(1, 1, -1, -1)
            inter = r1.intersected(r2)
            assert inter.isEmpty(), (
                f"Overlap in {context}: {visible[i].objectName() or type(visible[i]).__name__} {r1} "
                f"overlaps {visible[j].objectName() or type(visible[j]).__name__} {r2} -> {inter}"
            )


@pytest.fixture
def meeting_env(tmp_path):
    cfg = Config(config_dir=tmp_path / "cfg")
    cfg.set("llm.api_url", "http://localhost:11434/v1")
    cfg.set("llm.model_name", "local-model")
    history = HistoryStore(db_path=tmp_path / "history.db")
    controller = MeetingController(cfg, history, pending_segments=lambda: 0)
    page = MeetingPage(cfg, controller)
    yield cfg, history, controller, page
    page.hide()
    page.close()
    page.deleteLater()
    cfg._save_timer.stop()
    controller.shutdown()
    history.close(timeout=2.0)


def _stub_unused_settings_builders(monkeypatch) -> None:
    monkeypatch.setattr(SettingsWindow, "_rebuild_model_cards", lambda self: None)
    monkeypatch.setattr(SettingsWindow, "_refresh_about_info", lambda self: None)
    monkeypatch.setattr(SettingsWindow, "_refresh_audio_device_lists", lambda self: None)


# ── 1. C-10: Post-meeting materials default collapse & 0px overlap ──


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("size", [(1024, 680), (880, 580)])
def test_c10_materials_default_collapsed_and_zero_overlap_across_sizes(tmp_path, monkeypatch, theme, size):
    _stub_unused_settings_builders(monkeypatch)
    cfg = Config(config_dir=tmp_path / "cfg")
    cfg.set("llm.api_url", "http://localhost:11434/v1")
    cfg.set("llm.model_name", "local-model")
    history = HistoryStore(db_path=tmp_path / "history.db")
    controller = MeetingController(cfg, history, pending_segments=lambda: 0)
    app = QApplication.instance()
    win = MainWindow(cfg, history, meeting=controller)
    try:
        rec = controller.store.create_meeting(
            title="2026-11-12 季度产品与架构联合评审会议",
            participants="张三, 李四, 王五",
            agenda="1. 音频管线可靠性\n2. 会议纪要布局与回放\n3. 历史编辑保护",
        )
        controller.store.update_meeting(rec.id, notes_user="重点关注蓝牙耳机通信端点回环。")
        controller.store.add_mark(rec.id, 12_000, "关键结论")
        for i in range(6):
            history.enqueue(
                SegmentRecord(
                    rec.id,
                    i,
                    rec.started_at + i * 5000,
                    raw_text=f"第 {i + 1} 段会议讨论正文内容，核对视口高度。",
                    trigger_mode="meeting",
                    speaker_id=(i % 2) + 1,
                )
            )
        controller.store.save_notes(
            rec.id,
            template_id="general",
            model="local",
            json_payload="",
            markdown="## 会议摘要\n\n完成了核心模块的联调与回归验证。\n\n## 行动项\n\n- 跟进小屏布局巡检",
        )
        history.flush()

        win.show_page("meeting")
        apply_theme(app, mode=theme, surfaces=[win, win._settings, win._history, win._meeting])
        win.resize(*size)
        win.show()
        p = win._meeting
        p.show_meeting(rec.id)
        _settle(6)

        # Default collapsed when viewing saved meeting
        assert p._materials.isVisible()
        assert not p._materials_toggle.isChecked()
        assert not p._materials_scroll.isVisible()
        assert "展开会议资料（" in p._materials_toggle.text()
        assert p._notes_view.height() >= 200

        # No overlap among visible vertical children in detail pane (collapsed)
        _assert_no_overlap(
            [p._detail_title, p._detail_meta, p._materials, p._tab_bar, p._tab_tools, p._content_stack],
            f"detail_collapsed_{theme}_{size}",
        )

        # Expand materials: adaptive _record_notes height (not hardcoded 96) and 0px overlap
        p._materials_toggle.click()
        _settle(6)
        assert p._materials_toggle.isChecked()
        assert p._materials_scroll.isVisible()
        assert p._record_notes.maximumHeight() <= 84
        assert p._content_stack.height() >= 130

        _assert_no_overlap(
            [
                p._materials_agenda,
                p._materials_marks_label,
                p._record_marks,
                p._materials_notes_label,
                p._record_notes,
            ],
            f"materials_expanded_{theme}_{size}",
        )
        _assert_no_overlap(
            [p._detail_title, p._detail_meta, p._materials, p._tab_bar, p._tab_tools, p._content_stack],
            f"detail_expanded_{theme}_{size}",
        )
    finally:
        win.hide()
        win.close()
        win.deleteLater()
        cfg._save_timer.stop()
        controller.shutdown()
        history.close(timeout=2.0)


# ── 2. C-11 & M-28: TranscriptImportDialog contrast/debounce/drop & HistoryWindow edit lock ──


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_c11_m28_transcript_import_dialog_contrast_height_debounce_and_drop(tmp_path, theme):
    app = QApplication.instance()
    apply_theme(app, mode=theme)

    class CompactScreen:
        def availableGeometry(self):
            return QRect(0, 0, 910, 472)

    with patch.object(TranscriptImportDialog, "screen", return_value=CompactScreen()):
        dlg = TranscriptImportDialog()
        dlg.show()
        _settle()
        try:
            assert dlg.height() <= 472
            bg = QColor(tok.BG)
            fg = QColor(tok.TEXT)
            sec = QColor(tok.TEXT_SEC)
            assert _contrast_ratio(bg, fg) >= 4.5
            assert _contrast_ratio(bg, sec) >= 4.5
            assert f"background: {tok.BG}" in dlg.styleSheet()
            assert dlg._ok.styleSheet() == ss.BTN_PRIMARY

            # Debounce on large text typing (> 8000 chars) avoids blocking UI thread
            large_text = "[00:01] 张三：测试防抖解析内容。\n" * 450
            dlg._edit.setPlainText(large_text)
            assert dlg._parse_timer.isActive()
            dlg._refresh()
            assert dlg._ok.isEnabled()

            # Failed file open must disable OK and show red error without silently importing stale text
            bad_file = tmp_path / "missing_file.txt"
            assert dlg._load_file_path(str(bad_file)) is False
            assert dlg._file_error != ""
            assert not dlg._ok.isEnabled()

            # Drag & drop .txt file reads content directly instead of inserting file:/// URL
            good_file = tmp_path / "dropped_meeting.txt"
            good_file.write_text("[00:05] 李四：拖放文件直接导入正文\n", encoding="utf-8")
            mime = QMimeData()
            mime.setUrls([QUrl.fromLocalFile(str(good_file))])
            drag_ev = QDragEnterEvent(
                QPoint(10, 10),
                Qt.DropAction.CopyAction,
                mime,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            dlg.dragEnterEvent(drag_ev)
            assert drag_ev.isAccepted()
            drop_ev = QDropEvent(
                dlg._edit.rect().center().toPointF(),
                Qt.DropAction.CopyAction,
                mime,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            dlg.dropEvent(drop_ev)
            assert "file:///" not in dlg.transcript_text()
            assert "拖放文件直接导入正文" in dlg.transcript_text()
            assert dlg._ok.isEnabled()
        finally:
            dlg.close()
            dlg.deleteLater()


def test_c11_history_inline_edit_locks_view_toggle_preventing_header_corruption(tmp_path):
    cfg = Config(config_dir=tmp_path / "cfg")
    store = HistoryStore(db_path=tmp_path / "hist.db")
    win = None
    try:
        now = int(time.time() * 1000)
        store.enqueue(
            SegmentRecord(
                "s1",
                0,
                now,
                raw_text="原始口语内容",
                polished_text="润色后的书面内容",
                duration_ms=3200,
                trigger_mode="hotkey",
            )
        )
        store.flush()
        win = HistoryWindow(store, config=cfg)
        win.resize(960, 620)
        win.show()
        win.refresh()
        _settle()

        assert win._view_bar.isVisible()
        assert win._view_polished_btn.isEnabled()
        assert win._view_raw_btn.isEnabled()

        win._start_inline_edit()
        _settle()
        assert win._editing_session is True
        assert win._editing_session_id == "s1"
        assert not win._view_bar.isEnabled()
        assert not win._view_polished_btn.isEnabled()
        assert not win._view_raw_btn.isEnabled()
        assert "保存或取消" in win._view_raw_btn.toolTip()

        # Attempting _on_view_toggled while editing is ignored; draft stays clean
        win._details.setPlainText("用户修改后的润色稿")
        win._on_view_toggled(False)
        assert win._details.toPlainText() == "用户修改后的润色稿"
        assert win._save_inline_edit()
        store.flush()
        seg = store.get_session_segments("s1")[0]
        assert seg.polished_text == "用户修改后的润色稿"
        assert "秒" not in (seg.polished_text or "")
        assert seg.raw_text == "原始口语内容"
        assert win._view_raw_btn.isEnabled()
        win.hide()
    finally:
        if win is not None:
            win._undo_timer.stop()
            win._feedback_timer.stop()
            win.close()
            win.deleteLater()
        cfg._save_timer.stop()
        store.close(timeout=2.0)


# ── 3. C-08 UI & H-49: Start Card compact viewport, save_audio, dual meters & 15s silence ──


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_c08_h49_start_card_compact_910x472_and_model_combo_1024x680(tmp_path, monkeypatch, theme):
    _stub_unused_settings_builders(monkeypatch)
    cfg = Config(config_dir=tmp_path / "cfg")
    cfg.set("llm.api_url", "http://localhost:11434/v1")
    cfg.set("llm.model_name", "local-model")
    history = HistoryStore(db_path=tmp_path / "history.db")
    controller = MeetingController(cfg, history, pending_segments=lambda: 0)
    app = QApplication.instance()
    win = MainWindow(cfg, history, meeting=controller)
    try:
        with patch("sayink.ui.meeting_page.is_model_downloaded", return_value=True), patch(
            "sayink.audio_devices.describe_active_loopback_device",
            return_value="扬声器 (Realtek Audio) [回环]",
            create=True,
        ):
            win.show_page("meeting")
            apply_theme(app, mode=theme, surfaces=[win, win._settings, win._history, win._meeting])
            p = win._meeting
            win.setMinimumSize(880, 440)
            win.resize(910, 472)
            win.show()
            p.show_start_card()
            _settle(6)

            assert p._stack.currentIndex() == PAGE_START
            assert p._opt_save_audio.isChecked() is True
            assert p._opt_loopback_hint.isVisible()
            assert "Realtek" in p._opt_loopback_hint.text()

            # Core combos and save_audio checkbox sit above the fold without scrolling on 910x472
            vp = p._start_scroll.viewport()
            vp_global = _global_rect(vp)
            for core_widget in (p._opt_source, p._opt_model, p._opt_language, p._opt_save_audio):
                w_rect = _global_rect(core_widget)
                assert vp_global.contains(w_rect), f"{core_widget.accessibleName()} clipped out of 910x472 fold: {w_rect} vs {vp_global}"
            assert p._card_start.isVisible()

            # Toggling save_audio persists to config and MeetingOptions
            p._opt_save_audio.setChecked(False)
            assert cfg.get("meeting.save_audio") is False
            opts = p._options_from_card()
            assert getattr(opts, "save_audio", None) is False
            p._opt_save_audio.setChecked(True)
            assert cfg.get("meeting.save_audio") is True

            # Switching to microphone hides loopback hint
            mic_idx = p._opt_source.findData("microphone")
            p._opt_source.setCurrentIndex(mic_idx)
            _settle()
            assert not p._opt_loopback_hint.isVisible()

            # At 1024x680 the model stays the widest column, and language
            # matches the audio source so「自动（中文优先）」is not squeezed away.
            win.resize(1024, 680)
            _settle(4)
            assert p._opt_model.width() > p._opt_source.width()
            assert abs(p._opt_language.width() - p._opt_source.width()) <= 4
    finally:
        win.hide()
        win.close()
        win.deleteLater()
        cfg._save_timer.stop()
        controller.shutdown()
        history.close(timeout=2.0)


def test_c08_live_dual_meters_and_15s_remote_silence_banner(meeting_env):
    cfg, _history, controller, page = meeting_env
    page.resize(1024, 680)
    page.show()
    options = MeetingOptions(title="双路电平测试", input_source="mixed")
    caps = resolve_capabilities(cfg, options, dictation_model_id="funasr-nano")
    controller.begin(options, replace(caps, speaker_model_available=False))
    _settle()

    assert page._stack.currentIndex() == PAGE_LIVE
    assert page._live_meters_bar.isVisible()
    assert page._live_mic_label.text() == "麦克风"
    assert page._live_sys_label.text() == "电脑声（对方）"
    assert not page._system_silent_banner.isVisible()

    page.set_lane_levels(0.65, 0.42)
    assert abs(page._live_mic_meter.level - 0.65) < 0.01
    assert abs(page._live_sys_meter.level - 0.42) < 0.01
    assert not page._system_silent_banner.isVisible()

    # Simulate 15.5s of remote computer audio silence while mic continues speaking
    page._live_remote_active_ts = time.monotonic() - 15.5
    page.set_lane_levels(0.55, 0.0)
    assert page._system_silent_banner.isVisible()
    assert "蓝牙耳机" in page._system_silent_label.text()

    # Remote audio recovery auto-clears the warning banner
    page.set_lane_levels(0.4, 0.35)
    assert not page._system_silent_banner.isVisible()

    # Manual dismiss suppresses repeated banner until audio recovers again
    page._live_remote_active_ts = time.monotonic() - 16.0
    page._check_live_remote_silence()
    assert page._system_silent_banner.isVisible()
    page._system_silent_dismiss_btn.click()
    assert not page._system_silent_banner.isVisible()
    page._check_live_remote_silence()
    assert not page._system_silent_banner.isVisible()


# ── 4. H-50, H-29 UI & H-54 templates ──


def test_h50_title_cursor_zero_paused_mark_disabled_and_templates_dialog(meeting_env):
    cfg, history, controller, page = meeting_env
    rec = controller.store.create_meeting(
        title="2026-11-12 跨部门年度预算与技术路线图超长评审会议纪要归档测试标题"
    )
    history.enqueue(SegmentRecord(rec.id, 0, rec.started_at, raw_text="测试", trigger_mode="meeting"))
    history.flush()
    page.resize(880, 580)
    page.show()
    page.show_meeting(rec.id)
    _settle()
    assert page._detail_title.cursorPosition() == 0

    # Live -> Paused disables _mark_btn
    opts = MeetingOptions(title="暂停禁用标记", input_source="mixed")
    caps = resolve_capabilities(cfg, opts, dictation_model_id="funasr-nano")
    controller.begin(opts, replace(caps, speaker_model_available=False))
    _settle()
    assert page._mark_btn.isEnabled()
    controller.pause("设备切换暂停")
    _settle()
    assert controller.state is MeetingState.PAUSED
    assert not page._mark_btn.isEnabled()
    controller.resume()
    _settle()
    assert page._mark_btn.isEnabled()
    controller.finalize()

    # TemplateEditorDialog & MinutesTemplatesDialog styling, validation error color, default template & cancel guard
    from sayink.minutes_prompts import MinutesTemplate

    editor = TemplateEditorDialog(MinutesTemplate(id="custom-arch", name="", focus=""))
    editor.show()
    _settle()
    try:
        assert editor._save_btn.styleSheet() == ss.BTN_PRIMARY
        editor._name.setText("")
        editor.accept()
        assert editor._error.isVisible()
        assert tok.RED.lower() in editor._error.styleSheet().lower()

        editor._name.setText("架构评审模板")
        editor._focus.setPlainText("重点提取架构风险与接口变更")
        editor.accept()
        assert editor.entry is not None
        staged_entry = editor.entry
    finally:
        editor.hide()
        editor.deleteLater()

    dlg = MinutesTemplatesDialog(cfg)
    dlg.show()
    _settle()
    try:
        assert dlg._save_btn.styleSheet() == ss.BTN_PRIMARY
        assert "新会议默认模板" in dlg._set_default_check.text()
        assert "设为会议默认" not in dlg._save_btn.toolTip()

        dlg._custom.append(staged_entry)
        dlg._reload(staged_entry["id"])
        _settle()
        assert dlg._has_unsaved_changes()

        # Dirty draft triggers confirmation on Cancel; choosing keep-editing stays open
        with patch("sayink.ui.minutes_templates.confirm", return_value=False):
            dlg.reject()
            assert dlg.isVisible()

        dlg._set_default_check.setChecked(True)
        dlg.accept()
        assert cfg.get("meeting.template_id") == staged_entry["id"]
    finally:
        dlg.hide()
        dlg.deleteLater()


# ── 5. H-51: Muted visible paragraph actions & inline audio playback bar ──


def test_h51_audio_player_bar_and_timestamp_seek(meeting_env, tmp_path):
    _cfg, history, controller, page = meeting_env
    wav_file = _write_wav(tmp_path / "sample_meeting.wav", seconds=2.0)

    rec_no_audio = controller.store.create_meeting(title="无音频旧会议")
    controller.store.end_meeting(rec_no_audio.id, ended_at=rec_no_audio.started_at + 5000)
    history.enqueue(
        SegmentRecord(rec_no_audio.id, 0, rec_no_audio.started_at + 2000, raw_text="第一句", trigger_mode="meeting")
    )
    rec_audio = controller.store.create_meeting(title="带录音会议")
    controller.store.end_meeting(rec_audio.id, ended_at=rec_audio.started_at + 10_000)
    history.enqueue(
        SegmentRecord(rec_audio.id, 0, rec_audio.started_at + 5000, raw_text="点击时间戳回放这一句", trigger_mode="meeting")
    )
    history.flush()

    page.resize(1024, 680)
    page.show()
    page.show_meeting(rec_no_audio.id)
    _settle()
    assert not page._audio_player.isVisible()

    page.register_meeting_audio(rec_audio.id, str(wav_file))
    page.show_meeting(rec_audio.id)
    page._switch_tab(1)
    _settle()
    assert page._audio_player.isVisible()
    assert "seek-ms:5000" in page._transcript_view.toHtml()

    with patch.object(page._audio_player, "seek_ms") as seek_mock:
        page._on_transcript_link(QUrl("seek-ms:5000"))
        seek_mock.assert_called_once_with(5000, autoplay=True)


# ── 6. H-52: Ctrl+F search ignores action anchors, handles emojis & works in PAGE_BROWSE ──


def test_h52_in_document_search_ignores_anchors_and_supports_browse_and_live(meeting_env):
    cfg, history, controller, page = meeting_env
    rec = controller.store.create_meeting(title="搜索验证")
    history.enqueue(
        SegmentRecord(rec.id, 0, rec.started_at + 1000, raw_text="🚀 第一处推进目标", trigger_mode="meeting", speaker_id=1)
    )
    history.enqueue(
        SegmentRecord(rec.id, 1, rec.started_at + 4000, raw_text="第二处推进目标确认", trigger_mode="meeting", speaker_id=2)
    )
    controller.store.save_notes(
        rec.id, template_id="general", model="m", json_payload="", markdown="## 结论\n\n稳步推进第一阶段目标，再次推进验收。"
    )
    history.flush()

    page.resize(1024, 680)
    page.show()
    page.show_meeting(rec.id)
    page._switch_tab(1)
    _settle()

    page.open_detail_find()
    assert page._detail_find_bar.isVisible()
    # Searching "编辑" must NOT match the trailing paragraph action links
    page._detail_find_edit.setText("编辑")
    _settle()
    assert page._detail_find_status.text() == "未找到"

    # Searching "推进" matches both transcript paragraphs accurately across emoji surrogate pairs
    page._detail_find_edit.setText("推进")
    _settle()
    assert page._detail_find_status.text() == "1 / 2"
    page._detail_find_next_btn.click()
    assert page._detail_find_status.text() == "2 / 2"
    page._detail_find_prev_btn.click()
    assert page._detail_find_status.text() == "1 / 2"

    # Switching to 纪要 tab updates search on minutes view
    page._switch_tab(0)
    _settle()
    assert page._detail_find_status.text() == "1 / 2"

    # Live view Ctrl+F also ignores "编辑" and counts matches
    opts = MeetingOptions(title="实时搜索", input_source="mixed")
    caps = resolve_capabilities(cfg, opts, dictation_model_id="funasr-nano")
    live = controller.begin(opts, replace(caps, speaker_model_available=False))
    history.enqueue(SegmentRecord(live.id, 0, live.started_at + 1000, raw_text="实时讨论关键事项", trigger_mode="meeting"))
    history.flush()
    page._render_live()
    _settle()
    page.focus_search()
    page._live_find_edit.setText("编辑")
    assert page._live_find_status.text() == "未找到"
    page._live_find_edit.setText("关键")
    assert page._live_find_status.text() == "1 / 1"
    controller.finalize()


# ── 7. H-53 & H-46: Bound title flush, cancel minutes edit & backup recovery ──


def test_h53_bound_title_flush_and_minutes_cancel_button(meeting_env):
    _cfg, history, controller, page = meeting_env
    m1 = controller.store.create_meeting(title="会议一原标题")
    m2 = controller.store.create_meeting(title="会议二原标题")
    controller.store.save_notes(m1.id, template_id="general", model="m", json_payload="", markdown="原始纪要正文")
    history.flush()

    page.resize(1024, 680)
    page.show()
    page.show_meeting(m1.id)
    _settle()

    # Edit m1 title without pressing Enter, then immediately switch to m2 -> m1 gets saved, m2 untouched!
    page._detail_title.setFocus()
    page._detail_title.setText("会议一新标题")
    page._detail_title.textEdited.emit("会议一新标题")
    page.show_meeting(m2.id)
    history.flush()
    assert controller.store.get_meeting(m1.id).title == "会议一新标题"
    assert controller.store.get_meeting(m2.id).title == "会议二原标题"

    # Minutes edit mode shows explicit Cancel button and discards draft when confirmed
    page.show_meeting(m1.id)
    _settle()
    page._toggle_edit()
    assert page._editing_notes
    assert page._cancel_notes_edit_btn.isVisible()
    page._notes_edit.setPlainText("不小心改乱的草稿")

    with patch("sayink.ui.prompts.confirm_box") as mock_confirm:
        fake_box = MagicMock()
        discard_btn = MagicMock()
        fake_box.buttons.return_value = [discard_btn]
        fake_box.clickedButton.return_value = discard_btn
        mock_confirm.return_value = (fake_box, discard_btn)
        page._cancel_notes_edit_btn.click()

    assert not page._editing_notes
    assert controller.store.get_notes(m1.id).markdown == "原始纪要正文"


# ── 8. H-54: Dotted title export suffix preservation ──


def test_h54_export_suffix_preserves_dotted_titles():
    assert _with_export_suffix("C:/out/11.12 SayInk v1.4 评审", "md").endswith("11.12 SayInk v1.4 评审.md")
    assert _with_export_suffix("C:/out/11.12 SayInk v1.4 评审.txt", "md").endswith("11.12 SayInk v1.4 评审.md")
    assert _with_export_suffix("C:/out/11.12 SayInk v1.4 评审.md", "md").endswith("11.12 SayInk v1.4 评审.md")


# ── 9. H-55, M-16, M-30, L-03: HistoryWindow raw copy, raw edit, undo toast, pagination & export ──


def test_h55_m16_m30_l03_history_improvements(tmp_path):
    cfg = Config(config_dir=tmp_path / "cfg")
    store = HistoryStore(db_path=tmp_path / "hist.db")
    win = None
    try:
        base = int(time.time() * 1000)
        for idx in range(55):
            sid = f"sess-{idx:03d}"
            store.enqueue(
                SegmentRecord(
                    sid,
                    0,
                    base - idx * 2000,
                    raw_text=f"未润色原话 {idx}",
                    polished_text=f"润色后文本 {idx}",
                    duration_ms=1500,
                    trigger_mode="hotkey",
                )
            )
        store.flush()

        win = HistoryWindow(store, config=cfg)
        win.resize(1024, 680)
        win.show()
        win.refresh()
        _settle()

        # 1-click 复制原文 button visible and copies raw_text even while viewing polished
        assert win._copy_raw_btn.isVisible()
        with patch("sayink.ui.history_window.copy_text", return_value=True) as copy_mock:
            win._copy_raw_btn.click()
            copy_mock.assert_called_once_with("未润色原话 0")

        # Incremental +50 pagination preserves existing QListWidgetItem instances
        assert len(win.session_items()) == 50
        first_session_item = next(
            it for it in win.session_items() if it.data(Qt.ItemDataRole.UserRole) == "sess-000"
        )
        win._load_more()
        _settle()
        assert len(win.session_items()) == 55
        same_session_item = next(
            it for it in win.session_items() if it.data(Qt.ItemDataRole.UserRole) == "sess-000"
        )
        assert same_session_item is first_session_item

        # Editing in 原文 view updates raw_text without overwriting polished_text, even with extra blank lines
        win._session_list.clearSelection()
        first_session_item.setSelected(True)
        win._session_list.setCurrentItem(first_session_item)
        _settle()
        win._on_view_toggled(False)
        win._start_inline_edit()
        win._details.setPlainText("修正后的原文第一行\n\n\n补充的第二行")
        assert win._save_inline_edit()
        store.flush()
        seg0 = store.get_session_segments("sess-000")[0]
        assert "修正后的原文第一行" in seg0.raw_text
        assert seg0.polished_text == "润色后文本 0"

        # Export passes prefer_raw=True when viewing 原文
        win._on_view_toggled(False)
        out_md = tmp_path / "2026.11.12 导出测试"
        with patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName", return_value=(str(out_md), "Markdown (*.md)")):
            win._export_selected()
        written = (tmp_path / "2026.11.12 导出测试.md").read_text(encoding="utf-8")
        assert "修正后的原文第一行" in written

        # Search reset unsticks _view_polished back to user preference when switched to polished first
        win._on_view_toggled(True)
        win._search_edit.setText("修正后的原文第一行")
        win._perform_search()
        assert win._view_polished is False
        win._search_edit.clear()
        win._perform_search()
        assert win._view_polished is True

        # Copy during 8s delete undo window preserves 撤销 action button on _toast
        item_001 = next(it for it in win.session_items() if it.data(Qt.ItemDataRole.UserRole) == "sess-001")
        win._session_list.clearSelection()
        item_001.setSelected(True)
        win._session_list.setCurrentItem(item_001)
        _settle()
        win._delete_selected_sessions()
        assert len(win._pending_delete) == 1
        assert win._undo_btn.isVisible()
        with patch("sayink.ui.history_window.copy_text", return_value=True):
            win._copy_selected_effective()
        assert win._undo_btn.isVisible()
        assert win._undo_btn.text() == "撤销"

        # running_workers property exists
        assert win.running_workers == []
        win.hide()
    finally:
        if win is not None:
            win._undo_timer.stop()
            win._feedback_timer.stop()
            win.close()
            win.deleteLater()
        cfg._save_timer.stop()
        store.close(timeout=2.0)


# ── 10. H-56, M-29, M-31: Media % progress, live new-msg pill, speaker merge, null screen & hotwords ──


def test_h56_m29_m31_media_pct_live_scroll_pill_speaker_merge_and_hotword_bench(meeting_env):
    cfg, history, controller, page = meeting_env
    page.resize(1024, 680)
    page.show()

    # Media import percentage progress bar
    with patch.object(type(controller), "importing_media", new_callable=lambda: property(lambda _s: True)):
        page._media_import_title = "demo_video"
        page._on_media_progress("正在识别第 3 / 10 段")
        assert page._import_progress_bar.isVisible()
        assert page._import_progress_bar.value() == 30
        assert "30%" in page._import_progress.text()

    # Live scroll-up floating "↓ 底部有新发言" pill
    opts = MeetingOptions(title="滚动提醒测试", input_source="mixed")
    caps = resolve_capabilities(cfg, opts, dictation_model_id="funasr-nano")
    live = controller.begin(opts, replace(caps, speaker_model_available=False))
    for i in range(30):
        history.enqueue(
            SegmentRecord(
                live.id,
                i,
                live.started_at + i * 1000,
                raw_text=f"这是第 {i + 1} 条较长的会议实时发言内容，用于撑出纵向滚动条高度。" * 2,
                trigger_mode="meeting",
                speaker_id=(i % 2) + 1,
            )
        )
    history.flush()
    page._render_live()
    _settle(4)

    bar = page._live_transcript.verticalScrollBar()
    assert bar.maximum() > 40
    bar.setValue(0)
    _settle()
    history.enqueue(
        SegmentRecord(
            live.id,
            31,
            live.started_at + 31_000,
            raw_text="用户在上方回看时新到达的一句底部发言！",
            trigger_mode="meeting",
            speaker_id=1,
        )
    )
    history.flush()
    page._render_live()
    _settle()
    assert page._live_new_msg_btn.isVisible()
    page._live_new_msg_btn.click()
    _settle()
    assert not page._live_new_msg_btn.isVisible()
    assert bar.value() >= bar.maximum() - 24

    # Renaming speaker 2 to speaker 1's exact name auto-merges segments into speaker 1
    page._rename_speaker(1, "王工", meeting_id=live.id)
    page._rename_speaker(2, "王工", meeting_id=live.id)
    history.flush()
    segs = controller.store.get_segments(live.id)
    assert {s.speaker_id for s in segs} == {1}
    controller.finalize()

    # _SegmentTextDialog null-screen guard
    seg_sample = segs[0]
    with patch.object(_SegmentTextDialog, "screen", return_value=None):
        dlg = _SegmentTextDialog(seg_sample, position="00:00", speaker="王工", save_text=lambda _t: True)
        assert dlg.width() > 0
        dlg.close()
        dlg.deleteLater()

    # tools/asr_bench.py short ASCII hotword word-boundary accuracy
    assert hotword_hits(["AI"], "We wait on main for AI", "We wait on main") == (0, 1)
    assert hotword_hits(["AI"], "使用AI助手讨论wait方案", "讨论wait方案") == (0, 1)
    assert hotword_hits(["AI"], "使用AI助手讨论wait方案", "使用ai助手讨论wait方案") == (1, 1)
    assert hotword_hits(["CAM++"], "已加载 CAM++ 声纹模型", "已加载cam++声纹模型") == (1, 1)

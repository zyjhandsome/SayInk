"""Regression tests for Workspace UI fixes (HistoryWindow & MeetingPage)."""

from __future__ import annotations

from pathlib import Path

import pytest
from PyQt6.QtCore import QDateTime, QPoint, Qt
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sayink.config import Config
from sayink.history_store import HistoryStore, SegmentRecord
from sayink.meeting_session import MeetingController, MeetingOptions, resolve_capabilities
from sayink.ui import design_tokens as tok
from sayink.ui import meeting_dialogs, meeting_page, meeting_player, meeting_views
from sayink.ui import settings_styles as ss
from sayink.ui.history_window import HistoryWindow
from sayink.ui.meeting_page import MeetingPage
from sayink.ui.settings_components import ThemedCheckBox
from sayink.ui.theme import apply_theme
from tests.test_history_window import FakeHistoryStore


def _srgb_luminance(color: QColor) -> float:
    comps = (color.redF(), color.greenF(), color.blueF())
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in comps]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _contrast_ratio(c1: QColor, c2: QColor) -> float:
    l1, l2 = sorted((_srgb_luminance(c1), _srgb_luminance(c2)))
    return (l2 + 0.05) / (l1 + 0.05)


def _pump(rounds: int = 4) -> None:
    for _ in range(rounds):
        QApplication.processEvents()


@pytest.fixture
def meeting_env(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr("sayink.config.default_secret_store", lambda: None)
    monkeypatch.setattr("sayink.config.Config._sync_registry_auto_start", lambda self: None)
    cfg = Config(config_dir=tmp_path / "cfg")
    cfg.set("llm.api_url", "http://127.0.0.1:11434/v1")
    cfg.set("llm.model_name", "qwen2.5:7b")
    cfg.set("stt.models_dir", str(tmp_path / "models"))
    history = HistoryStore(db_path=tmp_path / "meetings.db")
    ctrl = MeetingController(cfg, history, pending_segments=lambda: 0)
    page = MeetingPage(cfg, ctrl)
    try:
        yield cfg, history, ctrl, page
    finally:
        if ctrl.is_active:
            ctrl.finalize()
        page.cleanup()
        page.close()
        page.deleteLater()
        _pump(2)
        ctrl.shutdown()
        history.close(timeout=2)


# ── 1. HistoryWindow batch export version leak & styled Chinese error alerts ──


@pytest.mark.parametrize(
    ("ext", "filt", "expected_polished", "forbidden_raw"),
    [
        ("md", "Markdown (*.md)", "polished first", "raw first"),
        ("csv", "CSV 表格 (*.csv)", "polished first", "raw first"),
        ("json", "JSON (*.json)", "polished first", "raw first"),
    ],
)
def test_history_batch_export_uses_polished_text_even_after_raw_single_toggle(
    qapp, tmp_path, monkeypatch, ext, filt, expected_polished, forbidden_raw
):
    store = FakeHistoryStore()
    window = HistoryWindow(store)
    try:
        newer, older = window.session_items()
        window._session_list.setCurrentItem(newer)
        window._on_selection_changed()
        # Switch single-session toggle to raw ("原文") before entering batch selection.
        window._view_raw_btn.setChecked(True)
        assert window._view_polished is False

        # Single export while on "原文" still exports raw text.
        single_out = tmp_path / f"single.{ext}"
        monkeypatch.setattr(
            QFileDialog,
            "getSaveFileName",
            lambda *_a, **_kw: (str(single_out), filt),
        )
        window._export_selected()
        single_text = single_out.read_text(encoding="utf-8")
        assert "raw first" in single_text

        # Multi-selecting hides single-item toggle; batch export must prefer polished text.
        newer.setSelected(True)
        older.setSelected(True)
        window._on_selection_changed()
        assert window._view_bar.isHidden()

        batch_out = tmp_path / f"batch.{ext}"
        monkeypatch.setattr(
            QFileDialog,
            "getSaveFileName",
            lambda *_a, **_kw: (str(batch_out), filt),
        )
        window._export_selected()
        batch_text = batch_out.read_text(encoding="utf-8")
        assert expected_polished in batch_text
        if ext != "json":
            assert forbidden_raw not in batch_text
        else:
            import json

            payload = json.loads(batch_text)
            newer_payload = next(s for s in payload["sessions"] if s["session_id"] == "newer")
            assert newer_payload["text"].startswith("polished first")
    finally:
        window.close()


def test_history_single_and_batch_export_errors_use_styled_chinese_alert(qapp, tmp_path, monkeypatch):
    store = FakeHistoryStore()
    window = HistoryWindow(store)
    captured: list[tuple[str, list[str], str]] = []
    try:
        monkeypatch.setattr(
            QFileDialog,
            "getSaveFileName",
            lambda *_a, **_kw: (str(tmp_path / "fail.md"), "Markdown (*.md)"),
        )
        monkeypatch.setattr(
            Path,
            "write_text",
            lambda *_a, **_kw: (_ for _ in ()).throw(OSError("permission denied")),
        )
        monkeypatch.setattr(
            QMessageBox,
            "exec",
            lambda box: captured.append(
                (box.text(), [b.text() for b in box.buttons()], box.buttons()[0].styleSheet())
            )
            or 0,
        )

        # Single export failure
        window.session_items()[0].setSelected(True)
        window._export_selected()

        # Batch export failure
        for item in window.session_items():
            item.setSelected(True)
        window._on_selection_changed()
        window._export_selected()

        assert len(captured) == 2
        for msg, btn_labels, btn_css in captured:
            assert "permission denied" in msg
            assert btn_labels == ["确定"]
            assert btn_css == ss.BTN_ACCENT_SM
    finally:
        window.close()


# ── 2. Keyboard Tab focus trap fix & arrow+Enter paragraph actions ────────────


def test_reading_and_transcript_views_advance_focus_on_single_tab_without_anchor_trap(meeting_env):
    _cfg, history, ctrl, page = meeting_env
    rec = ctrl.store.create_meeting(title="键盘焦点测试")
    ctrl.store.end_meeting(rec.id, ended_at=rec.started_at + 10_000)
    for idx in range(6):
        history.enqueue(
            SegmentRecord(
                rec.id,
                idx,
                rec.started_at + idx * 1000,
                raw_text=f"第 {idx} 段发言内容",
                trigger_mode="meeting",
                speaker_id=1 + (idx % 2),
                captured_at=rec.started_at + idx * 1000,
            )
        )
    ctrl.store.save_notes(
        rec.id,
        template_id="general",
        model="qwen2.5:7b",
        json_payload="",
        markdown="## 摘要\n- 结论一 [00:00:01]\n- 结论二 [00:00:03]\n",
    )
    ctrl.store.flush()

    host = QWidget()
    lay = QVBoxLayout(host)
    before_btn = QPushButton("前置按钮")
    after_btn = QPushButton("后置按钮")
    lay.addWidget(before_btn)
    lay.addWidget(page)
    lay.addWidget(after_btn)
    host.resize(980, 660)
    host.show()
    page.show_meeting(rec.id)
    _pump(6)
    try:
        assert page._notes_view.tabChangesFocus() is True
        assert page._transcript_view.tabChangesFocus() is True
        assert page._marks_view.tabChangesFocus() is True
        assert page._record_marks.tabChangesFocus() is True

        # Tab from _notes_view (which has multiple ts: anchors) must leave _notes_view in 1 press.
        page._switch_tab(0)
        _pump(4)
        page._notes_view.setFocus(Qt.FocusReason.TabFocusReason)
        _pump(2)
        assert host.focusWidget() is page._notes_view
        QTest.keyClick(page._notes_view, Qt.Key.Key_Tab)
        _pump(2)
        assert host.focusWidget() is not page._notes_view

        # Tab from _transcript_view (which has speaker: and edit-segment: anchors) must leave in 1 press.
        page._switch_tab(1)
        _pump(4)
        page._transcript_view.setFocus(Qt.FocusReason.TabFocusReason)
        _pump(2)
        assert host.focusWidget() is page._transcript_view
        QTest.keyClick(page._transcript_view, Qt.Key.Key_Tab)
        _pump(2)
        assert host.focusWidget() is not page._transcript_view
    finally:
        host.close()
        host.deleteLater()
        _pump(2)


# ── 3. In-text Ctrl+F search highlight contrast in Light & Dark ───────────────


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_ctrl_f_search_highlights_have_wcag_aa_contrast_and_update_on_theme_switch(
    qapp, meeting_env, mode
):
    _cfg, history, ctrl, page = meeting_env
    rec = ctrl.store.create_meeting(title="搜索高亮对比度")
    ctrl.store.end_meeting(rec.id, ended_at=rec.started_at + 6000)
    history.enqueue(
        SegmentRecord(rec.id, 0, rec.started_at + 1000, raw_text="第一处预算目标确认", trigger_mode="meeting", speaker_id=1)
    )
    history.enqueue(
        SegmentRecord(rec.id, 1, rec.started_at + 2000, raw_text="第二处预算目标复核", trigger_mode="meeting", speaker_id=2)
    )
    ctrl.store.flush()

    apply_theme(qapp, mode=mode, surfaces=[page])
    page.resize(960, 640)
    page.show()
    page.show_meeting(rec.id)
    page._switch_tab(1)
    page.open_detail_find()
    page._detail_find_edit.setText("预算目标")
    _pump(4)

    sels = page._transcript_view.extraSelections()
    assert len(sels) == 2
    active_bg = sels[0].format.background().color()
    passive_bg = sels[1].format.background().color()
    text_fg = sels[0].format.foreground().color()
    assert active_bg != passive_bg
    assert _contrast_ratio(text_fg, active_bg) >= 4.5
    assert _contrast_ratio(text_fg, passive_bg) >= 4.5

    # Live theme switch while find bar is open must immediately re-tint ExtraSelections.
    other_mode = "dark" if mode == "light" else "light"
    apply_theme(qapp, mode=other_mode, surfaces=[page])
    _pump(4)
    updated_sels = page._transcript_view.extraSelections()
    assert len(updated_sels) == 2
    assert updated_sels[0].format.background().color() != active_bg


# ── 4. Styled Meeting Time Dialog & Cancel Import QMessageBox styling ─────────


def test_meeting_time_dialog_has_40px_themed_datetime_edit_and_persists(qapp, meeting_env, monkeypatch):
    _cfg, _history, ctrl, page = meeting_env
    apply_theme(qapp, mode="dark", surfaces=[page])
    rec = ctrl.store.create_meeting(title="修改时间测试", started_at=1_700_000_000_000)
    ctrl.store.end_meeting(rec.id, ended_at=1_700_000_060_000)
    ctrl.store.flush()
    page.resize(960, 640)
    page.show()
    page.show_meeting(rec.id)
    _pump(4)

    target_dt = QDateTime.fromMSecsSinceEpoch(1_700_100_000_000)

    def _inspect_and_accept(dlg: meeting_dialogs._MeetingTimeDialog):
        dlg.show()
        _pump(4)
        before_size = dlg.datetime_edit.size()
        assert dlg.datetime_edit.height() == tok.CONTROL_FIELD_HEIGHT
        dlg.datetime_edit.setFocus()
        _pump(2)
        assert dlg.datetime_edit.size() == before_size
        assert tok.INPUT_BG in dlg.styleSheet()
        assert tok.CONTROL_BORDER in dlg.styleSheet()
        assert dlg.cancel_btn.styleSheet() == ss.BTN_GHOST_SM
        assert dlg.save_btn.styleSheet() == ss.BTN_PRIMARY
        dlg.datetime_edit.setDateTime(target_dt)
        dlg.close()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(meeting_dialogs._MeetingTimeDialog, "exec", _inspect_and_accept)
    page._edit_meeting_time()
    updated = ctrl.store.get_meeting(rec.id)
    assert updated is not None
    # Display format is minute-precision ("yyyy-MM-dd HH:mm").
    expected_minute_ms = int(
        QDateTime.fromString(target_dt.toString("yyyy-MM-dd HH:mm"), "yyyy-MM-dd HH:mm").toMSecsSinceEpoch()
    )
    assert updated.started_at == expected_minute_ms


def test_cancel_import_dialogs_apply_themed_message_box_styles(qapp, meeting_env, monkeypatch):
    _cfg, _history, _ctrl, page = meeting_env
    apply_theme(qapp, mode="dark", surfaces=[page])
    styled_boxes: list[QMessageBox] = []

    def _capture_exec(box: QMessageBox):
        styled_boxes.append(box)
        return 0

    monkeypatch.setattr(QMessageBox, "exec", _capture_exec)
    assert page._confirm_cancel_import() is False
    assert page._confirm_cancel_queued_import(2) == ""
    assert len(styled_boxes) == 2
    for box in styled_boxes:
        assert tok.BG in box.styleSheet()
        styles = [btn.styleSheet() for btn in box.buttons()]
        assert ss.BTN_DANGER_SOLID_SM in styles
        assert ss.BTN_GHOST_SM in styles


# ── 5. Start Card, Live View hierarchy, Empty Marks Scrollbar & Empty Notes ──


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_start_card_checkbox_border_and_heading_hierarchy(qapp, meeting_env, mode):
    _cfg, _history, _ctrl, page = meeting_env
    apply_theme(qapp, mode=mode, surfaces=[page])
    page.resize(960, 640)
    page.show()
    page.show_start_card()
    _pump(4)

    assert not hasattr(page, "_opt_save_audio")
    assert f"{tok.TYPE_TITLE_LG}px" in page._title_label.styleSheet()
    assert f"{tok.TYPE_TITLE}px" in page._card_title.styleSheet()
    assert tok.TEXT_SEC in page._card_title.styleSheet()

    # Unchecked ThemedCheckBox must paint a visible non-background box outline in both Light & Dark.
    box = ThemedCheckBox("保存会议录音", page)
    box.setChecked(False)
    canvas = QImage(220, 32, QImage.Format.Format_ARGB32_Premultiplied)
    bg = QColor(tok.BG)
    canvas.fill(bg)
    box.resize(220, 28)
    box.render(canvas, QPoint(0, 0))
    indicator_pixels = {
        canvas.pixelColor(x, y).name()
        for x in range(1, 20)
        for y in range(4, 24)
    }
    assert len(indicator_pixels - {bg.name()}) >= 1


def test_live_view_button_hierarchy_and_empty_marks_never_show_scrollbar(qapp, meeting_env):
    cfg, _history, ctrl, page = meeting_env
    apply_theme(qapp, mode="dark", surfaces=[page])
    options = MeetingOptions(title="实时层级检查", input_source="mixed")
    caps = resolve_capabilities(cfg, options, dictation_model_id="sensevoice")
    ctrl.begin(options, caps)
    page.resize(910, 472)
    page.show()
    _pump(6)

    # Live state: Pause is quiet ghost, Mark is prominent accent, End is solid danger.
    assert page._pause_btn.styleSheet() == ss.BTN_GHOST_SM
    assert page._mark_btn.styleSheet() == ss.BTN_ACCENT_SM
    assert page._end_btn.styleSheet() == ss.BTN_DANGER_SOLID_SM

    # Empty marks placeholder must never show a vertical scrollbar (even at compact 910x472).
    assert page._marks_view.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert not page._marks_view.verticalScrollBar().isVisible()

    # Paused state: Resume takes accent prominence, disabled Mark keeps bordered pill QSS.
    ctrl.pause("暂停核对")
    _pump(4)
    assert page._resume_btn.isVisible()
    assert page._resume_btn.styleSheet() == ss.BTN_ACCENT_SM
    assert not page._mark_btn.isEnabled()
    assert f"background: {tok.SURFACE_PEARL}" in page._mark_btn.styleSheet()

    # Adding marks enables scrolling as needed.
    ctrl.resume()
    ctrl.add_mark("关键决定")
    ctrl.store.flush()
    page._render_live()
    _pump(2)
    assert page._marks_view.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAsNeeded


def test_empty_notes_action_bar_keeps_bordered_disabled_buttons_and_restores_accent_when_ready(
    qapp, meeting_env
):
    _cfg, history, ctrl, page = meeting_env
    apply_theme(qapp, mode="dark", surfaces=[page])
    rec = ctrl.store.create_meeting(title="无纪要动作栏")
    ctrl.store.end_meeting(rec.id, ended_at=rec.started_at + 4000)
    history.enqueue(
        SegmentRecord(rec.id, 0, rec.started_at + 500, raw_text="讨论内容", trigger_mode="meeting", speaker_id=1)
    )
    ctrl.store.flush()

    page.resize(960, 640)
    page.show()
    page.show_meeting(rec.id)
    page._switch_tab(0)
    _pump(4)

    assert page._copy_btn.isVisible() and not page._copy_btn.isEnabled()
    assert page._edit_btn.isVisible() and not page._edit_btn.isEnabled()
    assert f"background: {tok.SURFACE_PEARL}" in page._copy_btn.styleSheet()
    assert f"background: {tok.SURFACE_PEARL}" in page._edit_btn.styleSheet()

    ctrl.store.save_notes(
        rec.id, template_id="general", model="qwen2.5:7b", json_payload="", markdown="## 结论\n已完成。"
    )
    ctrl.store.flush()
    page._render_detail()
    _pump(2)
    assert page._copy_btn.isEnabled()
    assert page._copy_btn.styleSheet() == ss.BTN_ACCENT_SM


# ── 6. Modularization & white-box re-export contracts ─────────────────────────


def test_meeting_page_modularization_and_reexport_identity():
    mp_lines = Path(meeting_page.__file__).read_text(encoding="utf-8").splitlines()
    assert len(mp_lines) < 3800
    assert meeting_page._AudioPlayerBar is meeting_player._AudioPlayerBar
    assert meeting_page._LevelMeterBar is meeting_player._LevelMeterBar
    assert meeting_page._CleanComboBox is meeting_player._CleanComboBox
    assert meeting_page._prune_stale_playback_wavs is meeting_player._prune_stale_playback_wavs
    assert meeting_page._SegmentSpeakerDialog is meeting_dialogs._SegmentSpeakerDialog
    assert meeting_views._SegmentSpeakerDialog is meeting_dialogs._SegmentSpeakerDialog
    assert meeting_page._SegmentTextDialog is meeting_dialogs._SegmentTextDialog
    assert meeting_views._SegmentTextDialog is meeting_dialogs._SegmentTextDialog
    assert meeting_page._MeetingTimeDialog is meeting_dialogs._MeetingTimeDialog

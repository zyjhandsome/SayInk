"""Global anti-flicker & zero-transient-popup regression suite across SayInk UI.

Guards against all 5 Windows Qt6 top-level popup flash & layout jitter patterns:
- Pattern A: Unparented QWidgets calling setVisible(True) / show() before parenting.
- Pattern B: Detached layout / list / chip widgets staying visible before deleteLater().
- Pattern C: Premature winId() native HWND materialization on unshown QMessageBox / QDialog.
- Pattern D: Redundant 400ms post-delete refresh & multi-frame list rebuild jitter.
- Pattern E: Overlay / toast show() firing at (0, 0) before reposition().
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from PyQt6.QtCore import QEvent, QObject, QPoint, Qt
from PyQt6.QtWidgets import QLabel, QMessageBox, QWidget

from sayink.history_store import HistoryStore, SegmentRecord
from sayink.meeting_session import MeetingController
from sayink.onboarding import OnboardingController, _SetupDialog
from sayink.ui.history_window import HistoryWindow
from sayink.ui.meeting_page import MeetingPage
from sayink.ui.prompts import confirm_box, style_message_box, text_prompt_box
from sayink.ui.settings_pages.model import ModelSettingsPage
from sayink.ui.theme import apply_theme


class _TransientWindowWatcher(QObject):
    """Record unexpected top-level QWidget Show / WinIdChange / Polish events."""

    def __init__(self, allowed_windows: set[QWidget]) -> None:
        super().__init__()
        self._allowed = allowed_windows
        self.events: list[tuple[str, str, str, int, int]] = []
        self.child_show_positions: dict[str, QPoint] = {}

    def eventFilter(self, obj: QObject | None, event: QEvent | None) -> bool:
        if obj is None or event is None or not isinstance(obj, QWidget):
            return False
        etype = event.type()
        if etype == QEvent.Type.Show and not obj.isWindow() and obj.objectName():
            self.child_show_positions[obj.objectName()] = QPoint(obj.pos())
        if etype in (QEvent.Type.Show, QEvent.Type.WinIdChange) or (
            etype == QEvent.Type.Polish
            and (obj.isVisible() or obj.testAttribute(Qt.WidgetAttribute.WA_WState_Created))
        ):
            if obj.isWindow() and obj not in self._allowed:
                self.events.append(
                    (
                        etype.name,
                        type(obj).__name__,
                        obj.objectName(),
                        obj.width(),
                        obj.height(),
                    )
                )
        return False


def _seed_meetings(config, tmp_path: Path):
    config.set("llm.api_url", "http://localhost:11434/v1")
    config.set("llm.model_name", "local-model")
    history = HistoryStore(db_path=tmp_path / "meetings_flicker.db")
    controller = MeetingController(config, history, pending_segments=lambda: 0)
    records = []
    for idx, title in enumerate(("甲例会", "乙评审", "丙复盘", "丁规划"), start=1):
        rec = controller.store.create_meeting(title=title, started_at=1_700_000_000_000 + idx * 10_000)
        controller.store.end_meeting(rec.id, ended_at=rec.started_at + 5_000)
        history.enqueue(
            SegmentRecord(
                rec.id,
                1,
                rec.started_at,
                raw_text=f"{title}发言内容。",
                trigger_mode="meeting",
                captured_at=rec.started_at,
            )
        )
        records.append(rec)
    assert controller.store.flush()
    return history, controller, records


def test_meeting_batch_delete_search_sort_and_single_delete_emit_zero_transient_popups(
    config, tmp_path: Path, qapp, monkeypatch
) -> None:
    history, controller, records = _seed_meetings(config, tmp_path)
    page = MeetingPage(config, controller)
    page.resize(980, 640)
    watcher = _TransientWindowWatcher({page})
    qapp.installEventFilter(watcher)
    try:
        page.show()
        page.show_meeting(records[0].id)
        qapp.processEvents()
        watcher.events.clear()

        monkeypatch.setattr("sayink.ui.meeting_page.confirm_batch_delete", lambda *_a, **_kw: True)

        # Enter batch mode, select 2 of 4 meetings, and batch-delete while 2 remain.
        page.set_batch_mode(True)
        page.set_selected_meeting_ids([records[0].id, records[1].id])
        page.delete_selected_meetings()

        # Must NOT leave 400ms _refresh_timer active after synchronous refresh!
        assert page.has_pending_refresh is False
        assert page.meeting_count() == 2
        assert page.is_batch_mode is True
        qapp.processEvents()
        assert watcher.events == [], f"Unexpected top-level flashes on batch delete: {watcher.events}"

        # Search, clear search, and toggle sort while batch checkboxes are visible!
        page.search_meetings("丙")
        assert page.meeting_count() == 1
        page.search_meetings("")
        assert page.meeting_count() == 2
        page.toggle_list_sort()
        qapp.processEvents()
        assert watcher.events == [], f"Unexpected top-level flashes on batch search/sort: {watcher.events}"

        # Single-meeting delete also cancels any pending 400ms refresh timer and flashes 0 popups.
        page.set_batch_mode(False)
        page.show_meeting(records[2].id)
        monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
        monkeypatch.setattr(
            QMessageBox,
            "clickedButton",
            lambda self: next(b for b in self.buttons() if b.text() == "删除"),
        )
        # Allow the expected modal QMessageBox itself, but forbid any unparented child popup.
        orig_filter = watcher.eventFilter

        def _ignore_msgbox(obj, ev):
            if isinstance(obj, QMessageBox):
                return False
            return orig_filter(obj, ev)

        monkeypatch.setattr(watcher, "eventFilter", _ignore_msgbox)
        watcher.events.clear()
        page.delete_current_meeting()
        assert page.has_pending_refresh is False
        assert page.meeting_count() == 1
        qapp.processEvents()
        assert watcher.events == [], f"Unexpected flashes on single delete: {watcher.events}"

        # Deleting the last remaining meeting in batch mode exits batch mode cleanly.
        page.set_batch_mode(True)
        page.select_loaded_meetings()
        page.delete_selected_meetings()
        assert page.has_pending_refresh is False
        assert page.meeting_count() == 0
        assert page.is_batch_mode is False
        qapp.processEvents()
        assert watcher.events == [], f"Unexpected flashes deleting final meeting: {watcher.events}"
    finally:
        qapp.removeEventFilter(watcher)
        page.close()
        page.deleteLater()
        controller.shutdown()
        history.close(timeout=2)


def test_history_selection_delete_search_chips_and_toast_emit_zero_transient_popups(
    tmp_path: Path, qapp
) -> None:
    store = HistoryStore(db_path=tmp_path / "history_flicker.db")
    for idx, text in enumerate(("第一条转写内容", "第二条搜索目标", "第三条保留记录"), start=1):
        sid = f"sess-{idx}"
        ts = 1_700_000_000_000 + idx * 60_000
        store.enqueue(
            SegmentRecord(
                sid,
                1,
                ts,
                raw_text=text,
                polished_text=f"{text}（润色）",
                source="mic",
                target_app="notepad.exe",
                trigger_mode="hotkey",
            )
        )
    assert store.flush()

    win = HistoryWindow(store)
    win.resize(960, 620)
    watcher = _TransientWindowWatcher({win})
    qapp.installEventFilter(watcher)
    try:
        win.show()
        win.refresh()
        qapp.processEvents()
        watcher.events.clear()
        watcher.child_show_positions.clear()

        # Enter selection mode and delete 1 of 3 sessions (rebuilds remaining stream rows in selection mode).
        win.set_selection_mode(True)
        items = win.session_items()
        assert len(items) == 3
        items[0].setSelected(True)
        win.delete_selected_sessions()
        qapp.processEvents()
        assert win.session_count() == 2
        assert watcher.events == [], f"Unexpected top-level flashes in HistoryWindow delete: {watcher.events}"

        # Toast must be repositioned BEFORE show() so it never flashes at (0, 0).
        toast_pos = watcher.child_show_positions.get("historyToast")
        assert toast_pos is not None
        assert toast_pos != QPoint(0, 0), f"Toast flashed at top-left (0, 0) before reposition: {toast_pos}"

        # Search and clear search while selection checkboxes are visible.
        win.search_sessions("第二")
        assert win.session_count() == 1
        win.search_sessions("")
        assert win.session_count() == 2
        qapp.processEvents()
        assert watcher.events == [], f"Unexpected top-level flashes in HistoryWindow search: {watcher.events}"

        # Rebuilding detail chips hides & marks old chips WA_DontShowOnScreen before deleteLater.
        win.set_detail_chips(["单次转写", "麦克风", "Notepad", "1 段"])
        first_generation = [
            lbl
            for lbl in win.findChildren(QLabel, "streamChip")
            if not lbl.testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        ]
        assert len(first_generation) == 4
        win.set_detail_chips(["持续转写", "电脑声音"])
        for stale in first_generation:
            assert stale.testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
            assert stale.isHidden()
        qapp.processEvents()
        assert watcher.events == []
    finally:
        qapp.removeEventFilter(watcher)
        win.close()
        win.deleteLater()
        store.close(timeout=2)


def test_dialogs_and_dynamic_layouts_avoid_premature_hwnd_and_top_level_flashes(
    config, tmp_path: Path, qapp, monkeypatch
) -> None:
    apply_theme(qapp, mode="dark")
    host = QWidget()
    watcher = _TransientWindowWatcher({host})
    qapp.installEventFilter(watcher)
    try:
        host.resize(800, 600)
        host.show()
        qapp.processEvents()
        watcher.events.clear()

        # 1. confirm_box / style_message_box / text_prompt_box must NOT materialize native HWND before show().
        cbox, _accept = confirm_box(
            host,
            "删除会议",
            "删除 2 场已结束的会议？",
            accept="删除",
            cancel="取消",
            destructive=True,
            informative_text="文字稿、笔记、标记与纪要会一起删除，无法撤销。",
        )
        assert not cbox.testAttribute(Qt.WidgetAttribute.WA_WState_Created)
        assert cbox.informativeText().startswith("文字稿")

        raw_box = QMessageBox(host)
        raw_box.setText("提示")
        style_message_box(raw_box)
        assert not raw_box.testAttribute(Qt.WidgetAttribute.WA_WState_Created)

        prompt_dlg, _edit = text_prompt_box(host, "重命名", "请输入名称：", "测试")
        assert not prompt_dlg.testAttribute(Qt.WidgetAttribute.WA_WState_Created)

        cbox.deleteLater()
        raw_box.deleteLater()
        prompt_dlg.deleteLater()
        assert watcher.events == []

        # 2. Onboarding _SetupDialog with history.onboarded=False must NOT flash unparented ThemedCheckBox
        # or create native HWND during __init__.
        config.set("history.onboarded", False)
        ob_ctrl = OnboardingController(
            config=config,
            parent=host,
            dialog_parent=lambda: host,
            is_continuous_mode=lambda: False,
            show_main_window=lambda *_a: None,
            model_installed=lambda: False,
        )
        setup_dlg = _SetupDialog(ob_ctrl, host)
        assert not setup_dlg.testAttribute(Qt.WidgetAttribute.WA_WState_Created)
        setup_dlg.deleteLater()
        ob_ctrl.deleteLater()

        from sayink.ui.media_import_dialog import MediaImportDialog
        from sayink.ui.transcript_import_dialog import TranscriptImportDialog

        media_dlg = MediaImportDialog(config=config, parent=host)
        transcript_dlg = TranscriptImportDialog(parent=host)
        assert not media_dlg.testAttribute(Qt.WidgetAttribute.WA_WState_Created)
        assert not transcript_dlg.testAttribute(Qt.WidgetAttribute.WA_WState_Created)
        media_dlg.deleteLater()
        transcript_dlg.deleteLater()

        qapp.processEvents()
        assert watcher.events == [], f"Unexpected popup during dialog init: {watcher.events}"

        # 3. ModelSettingsPage hero & card rebuilds mark old widgets WA_DontShowOnScreen + hidden without popups.
        monkeypatch.setattr("sayink.speech_recognizer.is_model_downloaded", lambda mid: mid == "sensevoice-small")
        model_page = ModelSettingsPage(host)
        runtime_mock = MagicMock(state="ready", active_model_id="sensevoice-small", message="")
        model_page.rebuild_cards(config, runtime_mock)
        first_cards = list(model_page.model_cards.values())
        assert first_cards
        model_page.rebuild_cards(config, runtime_mock)
        for old_card in first_cards:
            assert not old_card.isVisible()
            anc = old_card.parentWidget()
            assert anc is not None and anc.parentWidget() is not None
            assert anc.parentWidget().testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
            assert anc.parentWidget().isHidden()
        qapp.processEvents()
        assert watcher.events == [], f"Unexpected popup during ModelSettingsPage rebuild: {watcher.events}"
        model_page.deleteLater()
    finally:
        qapp.removeEventFilter(watcher)
        host.close()
        host.deleteLater()
        apply_theme(qapp, mode="light")

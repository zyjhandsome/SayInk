"""Batch 3 regression tests: FloatingWindow/QScreen safety, CJK minutes budget &
incremental preview, HistoryStore/MeetingStore indexes & thread-local read
connections, and MeetingController/MeetingPage point-lookups & UX polish."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import os
import threading

from PyQt6.QtCore import QRect, QUrl
from PyQt6.QtWidgets import QMessageBox

from sayink.config import Config
from sayink.history_store import HistoryStore, SegmentRecord
from sayink.meeting_session import (
    MeetingController,
    MeetingOptions,
    resolve_capabilities,
)
from sayink.minutes_generator import (
    MinutesRequest,
    MinutesWorker,
    estimate_prompt_tokens,
)
from sayink.minutes_prompts import (
    GENERAL_TEMPLATE_ID,
    REFINE_TEMPLATE_ID,
    MeetingContext,
    TranscriptLine,
    build_system_prompt,
    get_template,
)
from sayink.transcript_refiner import REFINE_CHUNK_CHARS
from sayink.speaker_embedding import SpeakerEmbedder
from sayink.ui import design_tokens as tok
from sayink.ui.floating_window import FloatingWindow, WaveformWidget
from sayink.ui.island_chrome import (
    is_screen_alive,
    position_listen_bar,
    screen_storage_key,
)
from sayink.ui.meeting_page import MeetingPage


class _DeadScreen:
    """Simulates a PyQt6 QScreen wrapper whose underlying C++ object died."""

    def availableGeometry(self):
        raise RuntimeError("wrapped C/C++ object of type QScreen has been deleted")

    def geometry(self):
        raise RuntimeError("wrapped C/C++ object of type QScreen has been deleted")

    def name(self):
        raise RuntimeError("wrapped C/C++ object of type QScreen has been deleted")


class TestFloatingWindowPolish:
    def test_hide_and_show_suspend_and_resume_20hz_timers(self, qapp):
        win = FloatingWindow()
        try:
            win.show_listening()
            assert win._waveform._timer.isActive()

            win.hide()
            qapp.processEvents()
            assert not win._waveform._timer.isActive()
            assert not win._dot._timer.isActive()

            win.show()
            qapp.processEvents()
            assert win._waveform._timer.isActive()

            win.show_meeting("00:00:05")
            assert win._dot._timer.isActive()
            assert win._waveform._timer.isActive()
            win.hide()
            qapp.processEvents()
            assert not win._dot._timer.isActive()
            assert not win._waveform._timer.isActive()
            win.show()
            qapp.processEvents()
            assert win._dot._timer.isActive()
            assert win._waveform._timer.isActive()
        finally:
            win.close()
            win.deleteLater()

    def test_waveform_silent_auto_pause_and_wake(self, qapp):
        wf = WaveformWidget()
        try:
            wf.start()
            assert wf._timer.isActive()

            # Simulate loud speech followed by silence decaying to zero.
            wf.set_volume(0.5)
            wf._animate()
            assert wf._timer.isActive()
            assert max(wf._bar_heights) > 0.01

            wf.set_volume(0.0)
            for _ in range(30):
                wf._animate()
                if not wf._timer.isActive():
                    break
            assert not wf._timer.isActive()
            assert wf._bar_heights == [0.0] * wf.NUM_BARS

            # New audio wakes the timer immediately without waiting for state change.
            wf.set_volume(0.25)
            assert wf._timer.isActive()

            # While hidden/suspended, set_volume updates level without starting timer.
            wf.set_suspended(True)
            assert not wf._timer.isActive()
            wf.set_volume(0.4)
            assert not wf._timer.isActive()
            wf.set_suspended(False)
            assert wf._timer.isActive()
        finally:
            wf.stop()
            wf.deleteLater()

    def test_dead_or_unplugged_screen_recovers_to_primary_screen(self, qapp):
        dead = _DeadScreen()
        assert is_screen_alive(None) is False
        assert is_screen_alive(dead) is False
        assert isinstance(screen_storage_key(dead), str)

        win = FloatingWindow()
        try:
            win.show_listening()
            position_listen_bar(win)
            position_listen_bar(win, screen=dead)

            win._pin_screen = dead
            win._pin_right = 40
            win._pin_bottom = 60
            # Must not raise RuntimeError on geometry change with dead _pin_screen.
            win._hold_pinned_edges()
            assert win._pin_screen is not dead

            win._pin_screen = dead
            win._on_screen_removed(dead)
            assert win._pin_screen is not dead
            avail = qapp.primaryScreen().availableGeometry()
            assert isinstance(avail, QRect)
            assert avail.intersects(win.frameGeometry())
        finally:
            win.close()
            win.deleteLater()


class TestMinutesGeneratorCjkAndPreview:
    def test_cjk_token_estimator_avoids_utf8_3x_overcount_and_fits_6000_cjk_single_pass(self):
        cjk_text = "这是一段中文会议讨论内容，主要围绕产品架构与性能优化展开。" * 215  # >6,000 chars
        assert len(cjk_text) >= 6000
        est = estimate_prompt_tokens("system", cjk_text)
        # Old UTF-8 byte // 2 + 128 gave ~9,400+ for 6,235 CJK chars; CJK-aware gives ~7,550.
        assert 6500 <= est <= 8200

        lines = [
            TranscriptLine(
                offset_ms=i * 5000,
                speaker_id=(i % 2) + 1,
                text="我们确认本季度发布窗口与回归测试计划，确保核心指标达标。",
            )
            for i in range(150)
        ]
        template = get_template(GENERAL_TEMPLATE_ID)
        req = MinutesRequest(
            api_url="http://localhost:11434/v1",
            api_key="",
            model_name="local",
            template=template,
            context=MeetingContext(title="季度评审"),
            lines=lines,
            context_window_tokens=16384,
            max_output_tokens=4096,
        )
        worker = MinutesWorker(req)
        chunks = worker._plan_chunks(build_system_prompt(template))
        assert len(chunks) == 1

    def test_refine_chunk_cap_uses_two_thirds_of_max_output_tokens(self, monkeypatch):
        lines = [
            TranscriptLine(
                offset_ms=i * 3000,
                speaker_id=1,
                text="这是需要精整的一段会议口语内容，大约四十个汉字左右用于测试分段上限。",
            )
            for i in range(85)
        ]
        template = get_template(REFINE_TEMPLATE_ID)
        req = MinutesRequest(
            api_url="http://localhost:11434/v1",
            api_key="",
            model_name="local",
            template=template,
            context=MeetingContext(title="精整测试"),
            lines=lines,
            speaker_names={1: "主持人"},
            chunk_chars=REFINE_CHUNK_CHARS,
            context_window_tokens=32768,
            max_output_tokens=4096,
        )
        worker = MinutesWorker(req)
        group_sizes: list[int] = []

        async def fake_refine_group(_client, _httpx, chunks, index, *_args, **_kwargs):
            units = chunks[index]
            group_sizes.append(sum(len(u.text) for u in units))
            return [{"source_ids": [u.source_id for u in units], "text": "精整后的段落。"}]

        monkeypatch.setattr(worker, "_refine_group", fake_refine_group)
        # 85 * 34 = 2,890 chars:
        # - Old cap (4096 // 3 = 1365) split into 3 chunks (<=1365 chars each).
        # - New cap (4096 * 2 // 3 = 2730 -> REFINE_CHUNK_CHARS=1600) fits in 2 chunks!
        payload, chunked = asyncio.run(worker._refine(None, None))
        assert chunked is True
        assert len(group_sizes) == 2
        assert group_sizes[0] > 1365
        assert len(payload["paragraphs"]) == 2

    def test_incremental_preview_caches_completed_fields_and_strips_think_blocks(self):
        req = MinutesRequest(
            api_url="http://localhost:11434/v1",
            api_key="",
            model_name="local",
            template=get_template(GENERAL_TEMPLATE_ID),
            context=MeetingContext(title="预览测试"),
            lines=[TranscriptLine(offset_ms=0, speaker_id=1, text="测试")],
        )
        worker = MinutesWorker(req)
        previews: list[str] = []
        worker.preview.connect(previews.append)

        worker._reset_preview()
        previews.clear()
        worker._stage = "正在生成纪要…"

        part1 = '<think>先思考结构</think>{"summary": "第一阶段结论", "action_items": [{"task": "完成联调"}, {"task": "准备灰测'
        worker._preview_raw += part1
        worker._publish_preview()
        assert len(previews) == 1
        assert "先思考结构" not in previews[-1]
        assert "第一阶段结论" in previews[-1]
        assert "完成联调" in previews[-1]
        assert "准备灰测" in previews[-1]
        first_scan_pos = worker._preview_scan_pos
        assert first_scan_pos > 0
        assert worker._preview_completed == ["第一阶段结论", "完成联调"]

        part2 = '方案"}]}'
        worker._preview_raw += part2
        worker._publish_preview()
        assert worker._preview_scan_pos > first_scan_pos
        assert worker._preview_completed == ["第一阶段结论", "完成联调", "准备灰测方案"]
        assert "准备灰测方案" in previews[-1]


class TestHistoryAndMeetingStorePerf:
    def test_composite_indexes_created_and_used_by_query_planner(self, tmp_path):
        db_path = tmp_path / "idx.db"
        store = HistoryStore(db_path=db_path)
        try:
            indexes = store.read(
                lambda c: {
                    r[0]
                    for r in c.execute(
                        "SELECT name FROM sqlite_master WHERE type='index'"
                    ).fetchall()
                },
                default=set(),
            )
            assert "idx_history_session_seq" in indexes
            assert "idx_meeting_marks_meeting_offset" in indexes

            seg_plan = store.read(
                lambda c: " | ".join(
                    str(r[-1])
                    for r in c.execute(
                        "EXPLAIN QUERY PLAN SELECT seq FROM history "
                        "WHERE session_id = 'm1' AND trigger_mode = 'meeting' ORDER BY seq ASC"
                    ).fetchall()
                ),
                default="",
            )
            assert "idx_history_session_seq" in seg_plan
            assert "TEMP B-TREE" not in seg_plan.upper()

            mark_plan = store.read(
                lambda c: " | ".join(
                    str(r[-1])
                    for r in c.execute(
                        "EXPLAIN QUERY PLAN SELECT id, offset_ms, note FROM meeting_marks "
                        "WHERE meeting_id = 'm1' ORDER BY offset_ms ASC"
                    ).fetchall()
                ),
                default="",
            )
            assert "idx_meeting_marks_meeting_offset" in mark_plan
            assert "TEMP B-TREE" not in mark_plan.upper()
        finally:
            store.close(timeout=2.0)

    def test_thread_local_read_connection_reused_and_closed_cleanly_on_windows(self, tmp_path):
        db_path = tmp_path / "pool.db"
        store = HistoryStore(db_path=db_path)
        store.enqueue(
            SegmentRecord(
                session_id="s1",
                seq=0,
                created_at=1000,
                raw_text="hello",
                polished_text="hello",
                source="mic",
                duration_ms=500,
                target_app="",
                trigger_mode="toggle",
                model="funasr-nano",
            )
        )
        assert store.flush(timeout=2.0)

        conn_id_1 = store.read(lambda c: id(c), default=0)
        conn_id_2 = store.read(lambda c: id(c), default=0)
        assert conn_id_1 != 0 and conn_id_1 == conn_id_2

        worker_conn_ids: list[int] = []
        t = threading.Thread(target=lambda: worker_conn_ids.append(store.read(lambda c: id(c), default=0)))
        t.start()
        t.join()
        assert worker_conn_ids[0] != 0 and worker_conn_ids[0] != conn_id_1

        store.close(timeout=2.0)
        assert len(store._read_conns) == 0

        # Post-close reads still work via ephemeral connections without leaking handles.
        sessions = store.list_sessions()
        assert len(sessions) == 1
        assert len(store._read_conns) == 0

        # Must rename/delete cleanly on Windows with zero [WinError 32] file locks.
        renamed = tmp_path / "pool_moved.db"
        os.replace(db_path, renamed)
        renamed.unlink()

    def test_search_meetings_enforces_limit_and_meeting_page_paginates_search(self, qapp, tmp_path):
        history = HistoryStore(db_path=tmp_path / "search.db")
        config = Config(config_dir=tmp_path / "cfg")
        ctrl = MeetingController(config, history, pending_segments=lambda: 0)
        page = MeetingPage(config, ctrl)
        try:
            ids = []
            for idx in range(6):
                rec = ctrl.store.create_meeting(title=f"架构周会 {idx}")
                ctrl.store.end_meeting(rec.id, ended_at=rec.started_at + 1000 * (idx + 1))
                ids.append(rec.id)
            assert ctrl.store.flush()

            top3 = ctrl.store.search_meetings("架构周会", limit=3)
            assert len(top3) == 3
            assert ctrl.store.search_meetings("架构周会", limit=0) == []

            page._list_limit = 3
            page._search_edit.setText("架构周会")
            page._perform_search()
            assert page._list.count() == 3
            assert page._list_summary.text() == "找到 3+ 场会议"
            assert not page._load_more_btn.isHidden()
        finally:
            page.deleteLater()
            ctrl.shutdown()
            history.close(timeout=2.0)


class _FakeCampPlusEmbedder(SpeakerEmbedder):
    name = "campplus"
    model_available = True
    supports_turn_detection = True


class TestMeetingControllerAndPagePolish:
    def test_embedder_cached_across_meetings_when_config_unchanged_and_cleared_when_idle(
        self, qapp, tmp_path, monkeypatch
    ):
        history = HistoryStore(db_path=tmp_path / "emb.db")
        config = Config(config_dir=tmp_path / "cfg")
        ctrl = MeetingController(config, history, pending_segments=lambda: 0)
        fake_model = tmp_path / "campplus.onnx"
        fake_model.write_bytes(b"onnx")
        fake_act = tmp_path / "activity.onnx"
        fake_act.write_bytes(b"onnx")

        monkeypatch.setattr(
            "sayink.meeting_session.aux_model_path",
            lambda key: fake_model if "campplus" in str(key) or "speaker" in str(key) else fake_act,
        )
        calls: list[tuple] = []

        def fake_make_embedder(path, *, enabled=True, activity_enabled=False):
            calls.append((str(path), bool(enabled), bool(activity_enabled)))
            return _FakeCampPlusEmbedder()

        monkeypatch.setattr("sayink.meeting_session.make_embedder", fake_make_embedder)
        base_caps = resolve_capabilities(config, MeetingOptions(), dictation_model_id="funasr-nano")
        caps1 = replace(
            base_caps,
            speaker_model_available=True,
            speaker_activity_enabled=False,
        )
        try:
            ctrl.begin(MeetingOptions(title="第一场"), caps1)
            assert len(calls) == 1
            assert ctrl.embedder is not None
            ctrl.finalize()
            assert ctrl.embedder is None

            # Second meeting with identical speaker config reuses cached embedder.
            ctrl.begin(MeetingOptions(title="第二场"), caps1)
            assert len(calls) == 1
            assert ctrl.embedder is not None
            ctrl.finalize()
            assert ctrl.embedder is None

            # Changing speaker_activity_enabled invalidates cache and reloads.
            caps2 = replace(
                base_caps,
                speaker_model_available=True,
                speaker_activity_enabled=True,
            )
            ctrl.begin(MeetingOptions(title="第三场"), caps2)
            assert len(calls) == 2
            ctrl.finalize()
        finally:
            ctrl.shutdown()
            assert ctrl._cached_embedder is None
            history.close(timeout=2.0)

    def test_point_lookups_and_single_flush_rename_speaker(self, qapp, tmp_path, monkeypatch):
        history = HistoryStore(db_path=tmp_path / "pt.db")
        config = Config(config_dir=tmp_path / "cfg")
        ctrl = MeetingController(config, history, pending_segments=lambda: 0)
        try:
            rec = ctrl.store.create_meeting(title="点查与合并刷新")
            for seq, (sid, text) in enumerate([(1, "我叫张三，负责后端。"), (2, "收到，我跟进测试。")]):
                history.enqueue(
                    SegmentRecord(
                        rec.id,
                        seq,
                        rec.started_at + seq * 4000,
                        raw_text=text,
                        polished_text=text,
                        speaker_id=sid,
                        trigger_mode="meeting",
                        captured_at=rec.started_at + seq * 4000,
                    )
                )
            ctrl.store.end_meeting(rec.id, ended_at=rec.started_at + 10_000)
            payload = {"summary": "说话人1负责后端，说话人2跟进测试。"}
            ctrl.store.save_notes(
                rec.id,
                template_id="general",
                model="local",
                json_payload=json.dumps(payload, ensure_ascii=False),
                markdown="说话人1负责后端，说话人2跟进测试。",
            )
            assert ctrl.store.flush()

            assert ctrl.store.get_segment(rec.id, 1).raw_text == "收到，我跟进测试。"
            assert ctrl.store.get_segment(rec.id, 99) is None
            assert ctrl.store.has_speaker(rec.id, 2) is True
            assert ctrl.store.has_speaker(rec.id, 9) is False
            assert ctrl.store.speaker_ids_in_segments(rec.id) == {1, 2}

            orig_get_segments = ctrl.store.get_segments
            orig_flush = ctrl.store.flush
            seg_scans = []
            flushes = []
            changed_events = []
            monkeypatch.setattr(
                ctrl.store,
                "get_segments",
                lambda mid: seg_scans.append(mid) or orig_get_segments(mid),
            )
            monkeypatch.setattr(
                ctrl.store,
                "flush",
                lambda *a, **kw: flushes.append(1) or orig_flush(*a, **kw),
            )
            ctrl.meeting_changed.connect(changed_events.append)

            # save_segment_text uses get_segment -> 0 full get_segments scans.
            assert ctrl.save_segment_text(rec.id, 1, "收到，我今天跟进测试。") is True
            assert len(seg_scans) == 0
            assert len(flushes) == 1
            assert len(changed_events) == 1

            seg_scans.clear()
            flushes.clear()
            changed_events.clear()

            # rename_speaker coalesces speaker + notes + inferred-names into 1 flush & 1 signal.
            assert ctrl.rename_speaker(rec.id, 2, "李四") == 0
            assert len(flushes) == 1
            assert len(seg_scans) == 1
            assert len(changed_events) == 1
            assert ctrl.speaker_names(rec.id) == {1: "张三", 2: "李四"}
            notes_md = ctrl.store.get_notes(rec.id).markdown
            assert "李四" in notes_md and "张三" in notes_md
        finally:
            ctrl.shutdown()
            history.close(timeout=2.0)

    def test_record_marks_ts_link_jumps_to_transcript_segment(self, qapp, tmp_path):
        history = HistoryStore(db_path=tmp_path / "marks.db")
        config = Config(config_dir=tmp_path / "cfg")
        ctrl = MeetingController(config, history, pending_segments=lambda: 0)
        page = MeetingPage(config, ctrl)
        try:
            rec = ctrl.store.create_meeting(title="标记跳转")
            for seq, offset in enumerate((2000, 9000)):
                history.enqueue(
                    SegmentRecord(
                        rec.id,
                        seq,
                        rec.started_at + offset,
                        raw_text=f"第{seq + 1}段内容",
                        polished_text=f"第{seq + 1}段内容",
                        speaker_id=1,
                        trigger_mode="meeting",
                        captured_at=rec.started_at + offset,
                    )
                )
            ctrl.store.add_mark(rec.id, 500, "开场前标记")
            ctrl.store.add_mark(rec.id, 9200, "关键结论")
            ctrl.store.end_meeting(rec.id, ended_at=rec.started_at + 15_000)
            assert ctrl.store.flush()

            page.show_meeting(rec.id)
            marks_html = page._record_marks.toHtml()
            assert "ts:0" in marks_html
            assert "ts:9" in marks_html

            # Clicking mark at 9.2s jumps to segment 1 (offset 9000ms).
            page._record_marks.anchorClicked.emit(QUrl("ts:9"))
            assert page._content_stack.currentWidget() is page._transcript_view
            assert page._transcript_highlight == 1
            assert tok.HIGHLIGHT_BG.lower() in page._transcript_view.toHtml().lower()

            # Clicking early mark at 0.5s (before first segment at 2.0s) clamps to segment 0.
            page._switch_tab(0)
            page._record_marks.anchorClicked.emit(QUrl("ts:0"))
            assert page._content_stack.currentWidget() is page._transcript_view
            assert page._transcript_highlight == 0
        finally:
            page.deleteLater()
            ctrl.shutdown()
            history.close(timeout=2.0)

    def test_delete_meeting_verifies_readback_and_preserves_detail_on_failure(
        self, qapp, tmp_path, monkeypatch
    ):
        history = HistoryStore(db_path=tmp_path / "del.db")
        config = Config(config_dir=tmp_path / "cfg")
        ctrl = MeetingController(config, history, pending_segments=lambda: 0)
        page = MeetingPage(config, ctrl)
        try:
            rec = ctrl.store.create_meeting(title="待删除会议")
            ctrl.store.end_meeting(rec.id, ended_at=rec.started_at + 5000)
            assert ctrl.store.flush()
            page.show_meeting(rec.id)
            assert page._detail_id == rec.id

            # Auto-click the destructive "删除" button in QMessageBox.
            def accept_delete(box):
                for btn in box.buttons():
                    if box.buttonRole(btn) == QMessageBox.ButtonRole.DestructiveRole:
                        box._clicked = btn
                        break
                return 0

            monkeypatch.setattr(QMessageBox, "exec", accept_delete)
            monkeypatch.setattr(
                QMessageBox,
                "clickedButton",
                lambda box: getattr(box, "_clicked", None),
            )

            # Simulate write/flush failure: meeting stays in DB, UI keeps detail & shows error.
            orig_delete = ctrl.store.delete_meeting
            orig_flush = ctrl.store.flush
            monkeypatch.setattr(ctrl.store, "delete_meeting", lambda _mid: None)
            monkeypatch.setattr(ctrl.store, "flush", lambda *a, **kw: False)

            page._delete()
            assert page._detail_id == rec.id
            assert "删除失败" in page._status_label.text()
            assert ctrl.store.get_meeting(rec.id) is not None

            # Restore store: delete succeeds and clears _detail_id.
            monkeypatch.setattr(ctrl.store, "delete_meeting", orig_delete)
            monkeypatch.setattr(ctrl.store, "flush", orig_flush)
            page._delete()
            assert ctrl.store.get_meeting(rec.id) is None
            assert page._detail_id == ""
        finally:
            page.deleteLater()
            ctrl.shutdown()
            history.close(timeout=2.0)

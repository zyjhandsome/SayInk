"""Tests for Stage-1 meeting-ui optimizations (Deliverables #1–#8)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import time
import zipfile

import numpy as np
from PyQt6.QtWidgets import QCheckBox, QFileDialog, QMessageBox

from sayink.config import Config
from sayink.history_store import HistoryStore, SegmentRecord
from sayink.meeting_session import (
    MEETING_TRIGGER_MODE,
    MeetingCapabilities,
    MeetingController,
    MeetingOptions,
    MeetingState,
)
from sayink.meeting_store import STATUS_ENDED, STATUS_INTERRUPTED, MeetingStore
from sayink.minutes_prompts import REFINE_TEMPLATE_ID
from sayink.ui import design_tokens as tok
from sayink.ui.audio_recovery_dialog import AudioRecoveryDialog
from sayink.ui.main_window import MainWindow
from sayink.ui.meeting_page import MeetingPage
from sayink.ui.meeting_player import (
    PLAYBACK_TEMP_WAV_MAX_AGE_S,
    _AudioPlayerBar,
    _prune_stale_playback_wavs,
)
from sayink.vad_segmenter import VAD_ENERGY


def _caps(**over) -> MeetingCapabilities:
    base = {
        "model_id": "funasr-nano",
        "hotwords": "",
        "hotwords_supported": True,
        "language": "中文",
        "language_supported": True,
        "vad": VAD_ENERGY,
        "speaker_model_available": False,
    }
    base.update(over)
    return MeetingCapabilities(**base)


def _make_env(tmp_path: Path, overrides: dict | None = None):
    cfg = Config(config_dir=tmp_path / "cfg")
    cfg.set("meeting.show_start_card", True)
    cfg.set("meeting.auto_generate", False)
    cfg.set("meeting.save_audio", True)
    if overrides:
        for key, val in overrides.items():
            cfg.set(key, val)
    history = HistoryStore(db_path=tmp_path / "history.db")
    pending = {"n": 0}
    ctrl = MeetingController(cfg, history, pending_segments=lambda: pending["n"])
    captured_requests: list = []

    def _capture_generate(req, *_a, **_kw) -> None:
        captured_requests.append(req)

    ctrl._generator.generate = _capture_generate  # type: ignore[method-assign]
    return cfg, history, ctrl.store, ctrl, captured_requests, pending


def _make_segment(
    session_id: str,
    seq: int,
    text: str,
    *,
    captured_at: int,
    duration_ms: int = 1500,
    speaker_id: int = 1,
) -> SegmentRecord:
    return SegmentRecord(
        session_id=session_id,
        seq=seq,
        created_at=captured_at + duration_ms,
        raw_text=text,
        polished_text=text,
        source="mixed",
        duration_ms=duration_ms,
        target_app="",
        trigger_mode=MEETING_TRIGGER_MODE,
        model="funasr-nano",
        speaker_id=speaker_id,
        speaker_route="microphone",
        captured_at=captured_at,
    )


def _insert_segment_sync(history: HistoryStore, store: MeetingStore, seg: SegmentRecord) -> None:
    ticket = history.enqueue(seg)
    assert store.flush_ticket(ticket, timeout=5.0)


# ── Deliverable 1: Missing segments, DB retry, & ASR gap markers ─────────


def test_meeting_1_asr_gap_notice_in_live_browse_exports_and_llm_prompt(qapp, tmp_path: Path):
    cfg, history, store, ctrl, captured_requests, _pending = _make_env(
        tmp_path,
        {
            "meeting.save_audio": False,
            "llm.api_url": "http://127.0.0.1:11434/v1",
            "llm.api_key": "local",
            "llm.model_name": "qwen2.5",
        },
    )
    ui = MeetingPage(cfg, ctrl)
    try:
        rec = ctrl.begin(MeetingOptions(title="架构评审", save_audio=False), _caps())
        mid = rec.id
        t0 = rec.started_at
        _insert_segment_sync(
            history, store, _make_segment(mid, 1, "第一段正常发言", captured_at=t0 + 1_000)
        )
        ctrl.record_asr_failure(t0 + 5_000, 2_000, reason="decode timeout", meeting_id=mid)
        _insert_segment_sync(
            history, store, _make_segment(mid, 2, "第三段恢复发言", captured_at=t0 + 9_000)
        )
        ctrl.meeting_changed.emit(mid)
        ui.refresh()
        qapp.processEvents()

        live_snap = ui.ui_state_snapshot()
        assert "[00:00:05 语音片段识别失败]" in str(live_snap["live_transcript_text"])

        ctrl.request_end()
        ui.show_meeting(mid)
        qapp.processEvents()

        snap = ui.ui_state_snapshot()
        banner = str(snap["integrity_banner"])
        assert "已保存 2 段、识别失败 1 段、保存失败 0 段" in banner
        assert "未保存会议录音" in banner
        assert snap["segment_retry_visible"] is False

        browse_text = str(snap["transcript_text"])
        assert "第一段正常发言" in browse_text
        assert "[00:00:05 语音片段识别失败]" in browse_text
        assert "第三段恢复发言" in browse_text
        assert (
            browse_text.index("第一段正常发言")
            < browse_text.index("[00:00:05 语音片段识别失败]")
            < browse_text.index("第三段恢复发言")
        )

        plain_export = ctrl.transcript_text(mid)
        md_export = ctrl.export_markdown(mid)
        assert "[00:00:05 语音片段识别失败]" in plain_export
        assert "[00:00:05 语音片段识别失败]" in md_export

        assert ctrl.generate(mid)
        assert len(captured_requests) == 1
        llm_texts = [line.text for line in captured_requests[0].lines]
        assert any("[00:00:05 语音片段识别失败]" in t for t in llm_texts)
    finally:
        ui.close()
        ctrl.shutdown()
        history.close()


def test_meeting_1_unsaved_segments_banner_and_idempotent_retry(qapp, tmp_path: Path):
    cfg, history, store, ctrl, captured_requests, _pending = _make_env(
        tmp_path,
        {
            "meeting.auto_generate": True,
            "llm.api_url": "http://127.0.0.1:11434/v1",
            "llm.api_key": "local",
            "llm.model_name": "qwen2.5",
        },
    )
    ui = MeetingPage(cfg, ctrl)
    try:
        rec = ctrl.begin(MeetingOptions(title="数据库抖动复盘"), _caps())
        mid = rec.id
        t0 = rec.started_at
        _insert_segment_sync(
            history, store, _make_segment(mid, 1, "已落库第一段", captured_at=t0 + 1_000)
        )
        unsaved_seg = _make_segment(mid, 2, "写库失败的第二段", captured_at=t0 + 4_000)
        ctrl.record_unsaved_segment(mid, unsaved_seg)

        ctrl.request_end()
        ui.show_meeting(mid)
        qapp.processEvents()

        # Auto-minutes held back while segments are missing so incomplete notes aren't generated
        assert len(captured_requests) == 0

        snap = ui.ui_state_snapshot()
        assert "已保存 1 段、识别失败 0 段、保存失败 1 段" in str(snap["integrity_banner"])
        assert snap["segment_retry_visible"] is True

        ok1, _msg1 = ui.retry_unsaved_segments(mid)
        ok2, _msg2 = ui.retry_unsaved_segments(mid)
        assert ok1 is True
        assert ok2 is True

        segs = store.get_segments(mid)
        assert [(s.seq, s.raw_text) for s in segs] == [
            (1, "已落库第一段"),
            (2, "写库失败的第二段"),
        ]
        snap_after = ui.ui_state_snapshot()
        assert snap_after["integrity_banner"] == ""
        assert snap_after["segment_retry_visible"] is False
    finally:
        ui.close()
        ctrl.shutdown()
        history.close()


# ── Deliverable 2: Export scope isolation & dedicated SRT menu item ──────


def test_meeting_2_export_notes_scope_blocks_srt_and_never_leaks_verbatim_transcript(
    qapp, tmp_path: Path, monkeypatch
):
    cfg, history, store, ctrl, _reqs, _pending = _make_env(tmp_path)
    ui = MeetingPage(cfg, ctrl)
    try:
        rec = ctrl.begin(MeetingOptions(title="薪酬委员会"), _caps())
        mid = rec.id
        t0 = rec.started_at
        _insert_segment_sync(
            history,
            store,
            _make_segment(mid, 1, "绝密逐字原话不可外泄XYZ", captured_at=t0 + 2_000, duration_ms=2_000),
        )
        ctrl.request_end()
        assert ctrl.save_user_markdown(mid, "## 会议摘要\n\n仅对外发布的纪要结论ABC。\n")
        ui.show_meeting(mid)
        qapp.processEvents()

        menu_state = ui.ui_state_snapshot()["export_menu"]
        assert "srt" in menu_state
        assert menu_state["srt"] == ("导出文字稿字幕 (.srt)", True)

        # Verify file dialog filter for scope == "notes" omits *.srt and rejects forced .srt path
        seen_filters: list[str] = []
        forced_srt = tmp_path / "leaked.srt"

        def _fake_save_dialog(_parent, _caption, _default, filters):
            seen_filters.append(filters)
            return (str(forced_srt), "Markdown (*.md)")

        monkeypatch.setattr(QFileDialog, "getSaveFileName", _fake_save_dialog)
        ui.trigger_export("notes")
        assert len(seen_filters) == 1
        assert ".srt" not in seen_filters[0].lower()
        assert not forced_srt.exists()
        assert "不支持 SRT" in str(ui.ui_state_snapshot()["status_text"])

        # Direct programmatic export_meeting_to_path contract across all formats & scopes
        assert ui.export_meeting_to_path(mid, tmp_path / "notes.srt", scope="notes") is False
        assert not (tmp_path / "notes.srt").exists()

        for ext in (".md", ".txt", ".docx"):
            out = tmp_path / f"only_notes{ext}"
            assert ui.export_meeting_to_path(mid, out, scope="notes") is True
            if ext == ".docx":
                with zipfile.ZipFile(out) as zf:
                    doc_xml = zf.read("word/document.xml").decode("utf-8")
                assert "仅对外发布的纪要结论ABC" in doc_xml
                assert "绝密逐字原话不可外泄XYZ" not in doc_xml
            else:
                content = out.read_text(encoding="utf-8")
                assert "仅对外发布的纪要结论ABC" in content
                assert "绝密逐字原话不可外泄XYZ" not in content

        for ext in (".md", ".txt", ".docx", ".srt"):
            out_t = tmp_path / f"only_transcript{ext}"
            assert ui.export_meeting_to_path(mid, out_t, scope="transcript") is True
            if ext == ".docx":
                with zipfile.ZipFile(out_t) as zf:
                    doc_xml = zf.read("word/document.xml").decode("utf-8")
                assert "绝密逐字原话不可外泄XYZ" in doc_xml
                assert "仅对外发布的纪要结论ABC" not in doc_xml
            else:
                content = out_t.read_text(encoding="utf-8")
                assert "绝密逐字原话不可外泄XYZ" in content
                assert "仅对外发布的纪要结论ABC" not in content

        srt_direct = tmp_path / "captions.srt"
        assert ui.export_meeting_to_path(mid, srt_direct, scope="srt") is True
        assert "-->" in srt_direct.read_text(encoding="utf-8")
    finally:
        ui.close()
        ctrl.shutdown()
        history.close()


# ── Deliverable 3: Remote AI consent full disclosure & exclude 我的笔记 ──


def test_meeting_3_remote_consent_disclosure_exclude_notes_and_batch_copy(
    qapp, tmp_path: Path, monkeypatch
):
    cfg, history, store, ctrl, captured_requests, _pending = _make_env(
        tmp_path,
        {
            "llm.api_url": "https://api.remote-llm.example/v1",
            "llm.api_key": "sk-test",
            "llm.model_name": "gpt-4o-mini",
            "asr.custom_words": ["SayInk专有词"],
        },
    )
    ui = MeetingPage(cfg, ctrl)
    try:
        rec1 = ctrl.begin(
            MeetingOptions(title="产品规划A", participants="张三, 李四", agenda="路线图讨论"),
            _caps(),
        )
        mid1 = rec1.id
        _insert_segment_sync(
            history, store, _make_segment(mid1, 1, "第一场讨论内容", captured_at=rec1.started_at + 1_000)
        )
        ctrl.add_mark("重点里程碑")
        assert ctrl.update_meeting(mid1, notes_user="这是不希望发给云端的个人私密笔记111")
        ctrl.request_end()

        rec2 = ctrl.begin(
            MeetingOptions(title="产品规划B", participants="王五", agenda="排期确认"),
            _caps(),
        )
        mid2 = rec2.id
        _insert_segment_sync(
            history, store, _make_segment(mid2, 1, "第二场讨论内容", captured_at=rec2.started_at + 1_000)
        )
        assert ctrl.update_meeting(mid2, notes_user="第二场私密笔记222")
        ctrl.request_end()

        ui.show_meeting(mid1)
        qapp.processEvents()

        dialogs_seen: list[dict[str, object]] = []

        def _fake_exec_exclude_notes(box: QMessageBox):
            cb = box.checkBox()
            has_cb = isinstance(cb, QCheckBox)
            if has_cb:
                cb.setChecked(False)
            dialogs_seen.append(
                {
                    "text": box.text(),
                    "info": box.informativeText(),
                    "cb_text": cb.text() if has_cb else "",
                }
            )
            box._clicked_override = box.defaultButton()
            return int(QMessageBox.StandardButton.Ok)

        monkeypatch.setattr(QMessageBox, "exec", _fake_exec_exclude_notes)
        monkeypatch.setattr(
            QMessageBox,
            "clickedButton",
            lambda box: getattr(box, "_clicked_override", box.defaultButton()),
        )

        # 1) Single-meeting minutes consent: full field list + unchecking 我的笔记
        ui.trigger_generate()
        assert len(dialogs_seen) == 1
        d1 = dialogs_seen[0]
        for expected_term in (
            "完整文字稿",
            "会议标题",
            "参会人",
            "议程",
            "时间标记",
            "我的笔记",
            "自定义词汇",
            "说话人称呼",
        ):
            assert expected_term in str(d1["text"])
        assert d1["cb_text"] == "发送时包含「我的笔记」"
        assert len(captured_requests) == 1
        assert captured_requests[0].context.notes_user == ""

        # 2) Reset confirmed hosts and test 原文精整 disclosure
        cfg.set("meeting.confirmed_remote_hosts", [])
        cfg.set("meeting.template_id", REFINE_TEMPLATE_ID)
        ctrl.remove_from_queue(mid1)
        ctrl._generating_for = ""
        captured_requests.clear()
        dialogs_seen.clear()
        ui.show_meeting(mid2)
        qapp.processEvents()
        ui.trigger_generate()
        assert len(dialogs_seen) == 1
        d_refine = dialogs_seen[0]
        for term in ("文字稿", "说话人称呼", "自定义词汇"):
            assert term in str(d_refine["text"])
        assert d_refine["cb_text"] == ""

        # 3) Reset confirmed hosts and test batch generation copy & note exclusion
        cfg.set("meeting.confirmed_remote_hosts", [])
        cfg.set("meeting.template_id", "general")
        ctrl.remove_from_queue(mid2)
        ctrl._generating_for = ""
        captured_requests.clear()
        dialogs_seen.clear()

        ui.refresh()
        ui.select_meetings_for_batch([mid1, mid2])
        ui.trigger_batch_generate()
        assert len(dialogs_seen) == 1
        d_batch = dialogs_seen[0]
        assert "确认后本机记住该地址，后续生成不再重复询问" in str(d_batch["info"])
        assert "这次批量生成只问这一次" not in str(d_batch["info"])
        assert "我的笔记" in str(d_batch["text"])
        assert len(captured_requests) >= 1
        assert captured_requests[0].context.notes_user == ""
    finally:
        ui.close()
        ctrl.shutdown()
        history.close()


# ── Deliverable 4: Multi-meeting UnsavedFinalize dict & sidecar ──────────


def test_meeting_4_multi_unsaved_finalizes_sidecar_restart_and_navigation(
    qapp, tmp_path: Path, monkeypatch
):
    cfg, history, store, ctrl, _reqs, _pending = _make_env(tmp_path)
    ui = MeetingPage(cfg, ctrl)
    try:
        real_end = store.end_meeting
        monkeypatch.setattr(store, "end_meeting", lambda *_a, **_kw: False)

        rec_a = ctrl.begin(MeetingOptions(title="早会A"), _caps())
        mid_a = rec_a.id
        _insert_segment_sync(
            history, store, _make_segment(mid_a, 1, "早会A发言", captured_at=rec_a.started_at + 500)
        )
        ctrl.request_end()

        rec_b = ctrl.begin(MeetingOptions(title="午会B"), _caps())
        mid_b = rec_b.id
        _insert_segment_sync(
            history, store, _make_segment(mid_b, 1, "午会B发言", captured_at=rec_b.started_at + 500)
        )
        ctrl.request_end()

        # Both pending finalizes are preserved without overwriting each other
        assert set(ctrl.unsaved_finalize_ids()) == {mid_a, mid_b}
        assert ctrl.has_unsaved_finalize(mid_a)
        assert ctrl.has_unsaved_finalize(mid_b)

        # Simulate restart before DB recovery: mark_interrupted MUST NOT corrupt mid_a / mid_b
        ctrl.shutdown()
        monkeypatch.setattr(store, "end_meeting", real_end)
        ctrl2 = MeetingController(cfg, history, pending_segments=lambda: 0)
        try:
            ctrl2.mark_interrupted()
            assert ctrl2.store.get_meeting(mid_a).status != STATUS_INTERRUPTED
            assert ctrl2.store.get_meeting(mid_b).status != STATUS_INTERRUPTED
            assert set(ctrl2.unsaved_finalize_ids()) == {mid_a, mid_b}

            ui2 = MeetingPage(cfg, ctrl2)
            try:
                ui2.show_meeting(mid_a)
                qapp.processEvents()
                snap_a = ui2.ui_state_snapshot()
                assert snap_a["finalize_retry_visible"] is True
                assert snap_a["finalize_retry_text"] == "重试保存结束状态"
                assert "早会A" in str(snap_a["finalize_retry_tooltip"])

                err_a = ui2.retry_finalize_save()
                assert err_a is None
                assert ui2.current_meeting_id == mid_a
                assert ctrl2.store.get_meeting(mid_a).status == STATUS_ENDED
                assert ctrl2.has_unsaved_finalize(mid_a) is False
                assert ctrl2.has_unsaved_finalize(mid_b) is True

                ui2.show_meeting(mid_b)
                qapp.processEvents()
                err_b = ui2.retry_finalize_save()
                assert err_b is None
                assert ui2.current_meeting_id == mid_b
                assert ctrl2.store.get_meeting(mid_b).status == STATUS_ENDED
                assert ctrl2.has_unsaved_finalize() is False
            finally:
                ui2.close()
        finally:
            ctrl2.shutdown()
    finally:
        ui.close()
        history.close()


# ── Deliverable 5: Preserve PAGE_START inputs & inline start error ───────


def test_meeting_5_preserve_start_card_inputs_and_inline_start_error(qapp, tmp_path: Path):
    cfg, history, _store, ctrl, _reqs, _pending = _make_env(tmp_path)
    ui = MeetingPage(cfg, ctrl)
    try:
        ui.show_start_card()
        ui.set_start_card_fields(
            title="Q4 战略对齐",
            participants="Alice, Bob",
            agenda="1. 目标拆解\n2. 风险预案",
            input_source="system",
            language="en",
        )
        ui.show_start_refusal("未检测到可用电脑声回环设备")
        qapp.processEvents()

        assert ui.is_on_start_card() is True
        snap = ui.ui_state_snapshot()
        assert snap["start_error"] == "尚未开始录音：未检测到可用电脑声回环设备"
        vals = ui.start_card_values()
        assert vals.title == "Q4 战略对齐"
        assert vals.participants == "Alice, Bob"
        assert vals.agenda == "1. 目标拆解\n2. 风险预案"
        assert vals.input_source == "system"
        assert vals.language == "en"

        # Navigating away and back before begin() still preserves draft inputs
        ui.show_meeting("")
        assert ui.is_on_start_card() is False
        ui.show_start_card()
        vals_reentered = ui.start_card_values()
        assert vals_reentered.title == "Q4 战略对齐"
        assert vals_reentered.participants == "Alice, Bob"
        assert vals_reentered.agenda == "1. 目标拆解\n2. 风险预案"
        assert vals_reentered.input_source == "system"

        # Once begin() succeeds, inputs are cleared for the next future meeting
        ctrl.begin(vals_reentered, _caps())
        ctrl.request_end()
        ui.show_start_card()
        vals_after_begin = ui.start_card_values()
        assert vals_after_begin.title == ""
        assert vals_after_begin.participants == ""
        assert vals_after_begin.agenda == ""
        assert ui.ui_state_snapshot()["start_error"] == ""
    finally:
        ui.close()
        ctrl.shutdown()
        history.close()


# ── Deliverable 6: Sidebar on PAGE_START + landing tab + 0-seg empty ─────


def test_meeting_6_sidebar_start_context_landing_tab_and_zero_segment_empty_state(
    qapp, tmp_path: Path
):
    cfg, history, store, ctrl, _reqs, _pending = _make_env(tmp_path, {"meeting.save_audio": True})
    main_win = MainWindow(cfg, history, meeting=ctrl)
    try:
        meeting_ui = main_win.meeting_panel
        assert isinstance(meeting_ui, MeetingPage)

        # On PAGE_BROWSE, normal dictation hints stay visible
        main_win.show_page("meeting")
        qapp.processEvents()
        browse_sidebar = main_win.sidebar_summary_texts()
        assert "点「开始」录制会议" not in browse_sidebar["shortcut"]

        # On PAGE_START, sidebar overrides ONLY here with start guidance & save_audio status
        meeting_ui.show_start_card()
        qapp.processEvents()
        start_sidebar_on = main_win.sidebar_summary_texts()
        assert start_sidebar_on["shortcut"] == "点「开始」录制会议 · 结果保存到会议记录"
        assert start_sidebar_on["output"] == "保存会议录音：已开启"
        assert "保存会议录音：已开启" in str(meeting_ui.ui_state_snapshot()["save_audio_hint"])

        cfg.set("meeting.save_audio", False)
        meeting_ui.show_start_card()
        qapp.processEvents()
        start_sidebar_off = main_win.sidebar_summary_texts()
        assert start_sidebar_off["output"] == "保存会议录音：已关闭"
        assert "保存会议录音：已关闭" in str(meeting_ui.ui_state_snapshot()["save_audio_hint"])

        # Returning to PAGE_BROWSE restores dictation hints
        meeting_ui.show_meeting("")
        qapp.processEvents()
        assert "点「开始」录制会议" not in main_win.sidebar_summary_texts()["shortcut"]

        # Live meeting with >= 1 segment and empty minutes lands on 文字稿 tab
        rec_speech = ctrl.begin(MeetingOptions(title="有发言会议"), _caps())
        mid_with_speech = rec_speech.id
        _insert_segment_sync(
            history,
            store,
            _make_segment(mid_with_speech, 1, "大家下午好，开始同步进度", captured_at=rec_speech.started_at + 1_000),
        )
        ctrl.request_end()
        qapp.processEvents()
        assert meeting_ui.current_meeting_id == mid_with_speech
        assert meeting_ui.ui_state_snapshot()["active_tab"] == "transcript"

        # Live meeting with 0 segments renders explicit 本场未录到发言 + playback/device entry
        wav_dir = tmp_path / "silent_journal"
        wav_dir.mkdir()
        (wav_dir / "audio.f32").write_bytes(np.zeros(16000, dtype="<f4").tobytes())
        rec_silent = ctrl.begin(
            MeetingOptions(title="静音会议", audio_path=str(wav_dir)),
            _caps(),
        )
        ctrl.request_end()
        qapp.processEvents()
        assert meeting_ui.current_meeting_id == rec_silent.id

        silent_snap = meeting_ui.ui_state_snapshot()
        assert "本场未录到发言" in str(silent_snap["notes_empty_text"])
        assert silent_snap["empty_bar_visible"] is True
        assert silent_snap["empty_play_visible"] is True
        assert silent_snap["empty_device_visible"] is True

        meeting_ui.audio_settings_requested.emit()
        qapp.processEvents()
        assert "点「开始」录制会议" not in main_win.sidebar_summary_texts()["shortcut"]
    finally:
        main_win.close()
        ctrl.shutdown()
        history.close()


# ── Deliverable 7: MeetingState.DRAINING amber dot & screen-reader label ─


def test_meeting_7_draining_amber_dot_and_accessible_name_with_remaining_count(
    qapp, tmp_path: Path
):
    cfg, history, _store, ctrl, _reqs, pending = _make_env(tmp_path)
    ui = MeetingPage(cfg, ctrl)
    try:
        ctrl.begin(MeetingOptions(title="收尾测试"), _caps())
        qapp.processEvents()
        live_snap = ui.ui_state_snapshot()
        assert live_snap["live_dot_a11y"] == "正在记录"
        assert tok.STATE_RECORD in str(live_snap["live_dot_style"])

        pending["n"] = 3
        ctrl.request_end()
        qapp.processEvents()
        assert ctrl.state is MeetingState.DRAINING
        drain_snap = ui.ui_state_snapshot()
        assert drain_snap["live_dot_a11y"] == "已停止采集，正在整理文字稿（剩余 3 段）"
        assert tok.AMBER in str(drain_snap["live_dot_style"])
        assert tok.STATE_RECORD not in str(drain_snap["live_dot_style"])

        pending["n"] = 0
        ctrl.finalize()
        qapp.processEvents()
        assert ctrl.state is MeetingState.IDLE
    finally:
        ui.close()
        ctrl.shutdown()
        history.close()


# ── Deliverable 8a & 8b: Playback temp WAV hygiene & crash backfill ──────


def test_meeting_8a_playback_temp_wav_max_age_2h_and_cleanup_on_stop_and_close(
    qapp, tmp_path: Path
):
    assert PLAYBACK_TEMP_WAV_MAX_AGE_S == 2 * 3600

    prune_dir = tmp_path / "sayink-playback-test"
    prune_dir.mkdir()
    stale_3h = prune_dir / "stale_3h.wav"
    fresh_30m = prune_dir / "fresh_30m.wav"
    stale_3h.write_bytes(b"RIFF")
    fresh_30m.write_bytes(b"RIFF")
    now_s = time.time()
    os.utime(stale_3h, (now_s - 3 * 3600, now_s - 3 * 3600))
    os.utime(fresh_30m, (now_s - 1800, now_s - 1800))

    _prune_stale_playback_wavs(prune_dir, force=True)
    assert not stale_3h.exists()
    assert fresh_30m.exists()

    journal_dir = tmp_path / "rec-journal"
    journal_dir.mkdir()
    (journal_dir / "audio.f32").write_bytes(np.zeros(8000, dtype="<f4").tobytes())

    bar = _AudioPlayerBar()
    bar._allow_headless_fallback = True
    try:
        assert bar.load_audio(journal_dir, fallback_duration_ms=500)
        bar.play()
        active_during_play = bar.active_temp_wav_paths()
        assert len(active_during_play) == 1
        synthesized_wav = active_during_play[0]
        assert synthesized_wav.is_file()

        bar.stop()
        assert not synthesized_wav.exists()
        assert bar.active_temp_wav_paths() == []

        # Re-playing transparently re-materializes and closing cleans it up
        bar.play()
        active_replay = bar.active_temp_wav_paths()
        assert len(active_replay) == 1
        bar.close()
        assert not active_replay[0].exists()
    finally:
        bar.cleanup_temp_files()


def test_meeting_8b_audio_recovery_backfill_appends_only_tail_to_interrupted_meeting(
    qapp, tmp_path: Path
):
    cfg, history, store, ctrl, _reqs, _pending = _make_env(tmp_path)
    recovery_root = tmp_path / "audio-recovery"
    recovery_root.mkdir()
    try:
        rec = ctrl.begin(MeetingOptions(title="断电中断周会"), _caps())
        mid = rec.id
        t0 = rec.started_at
        _insert_segment_sync(
            history,
            store,
            _make_segment(mid, 1, "崩溃前已保存的第一段", captured_at=t0 + 1_000, duration_ms=1_500),
        )
        ctrl.shutdown()

        # Mark the live meeting as STATUS_INTERRUPTED as happens after crash
        ctrl2 = MeetingController(cfg, history, pending_segments=lambda: 0)
        ctrl2.mark_interrupted()
        assert ctrl2.store.get_meeting(mid).status == STATUS_INTERRUPTED

        # Create matching 32-hex UUID recovery journal on disk
        jdir = recovery_root / mid
        jdir.mkdir()
        (jdir / "audio.f32").write_bytes((np.ones(16000 * 8, dtype="<f4") * 0.2).tobytes())
        (jdir / "session.json").write_text(
            json.dumps(
                {
                    "meeting_id": mid,
                    "started_at": t0 / 1000.0,
                    "sample_rate": 16000,
                    "complete": False,
                }
            ),
            encoding="utf-8",
        )

        dialog = AudioRecoveryDialog(
            recovery_root,
            recognizer=object(),
            meeting_store=ctrl2.store,
            history_store=history,
        )
        try:
            assert not dialog.backfill_btn.isHidden()
            assert dialog.backfill_btn.text() == "补回原会议"
            assert "原会议：断电中断周会" in dialog.items.item(0).text()

            events: list[tuple[str, int]] = []
            dialog.meeting_backfilled.connect(lambda m, c: events.append((m, c)))

            appended = dialog.append_recovered_segments(
                [
                    (1_000, 1_500, "崩溃前已保存的第一段（重跑）"),
                    (3_200, 1_800, "崩溃前未落库的第二段"),
                    (5_500, 1_500, "崩溃前未落库的第三段"),
                ]
            )
            assert appended == 2
            assert events == [(mid, 2)]

            segs = ctrl2.store.get_segments(mid)
            assert [(s.seq, s.raw_text) for s in segs] == [
                (1, "崩溃前已保存的第一段"),
                (2, "崩溃前未落库的第二段"),
                (3, "崩溃前未落库的第三段"),
            ]

            # Idempotent second backfill appends 0 duplicates
            appended_again = dialog.append_recovered_segments(
                [
                    (1_000, 1_500, "崩溃前已保存的第一段（重跑）"),
                    (3_200, 1_800, "崩溃前未落库的第二段"),
                    (5_500, 1_500, "崩溃前未落库的第三段"),
                ]
            )
            assert appended_again == 0
            assert len(ctrl2.store.get_segments(mid)) == 3
        finally:
            dialog.close()
            ctrl2.shutdown()
    finally:
        history.close()

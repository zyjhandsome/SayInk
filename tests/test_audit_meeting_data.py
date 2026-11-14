"""Comprehensive regression suite for SayInk meeting session, minutes, SQLite stores, and import/export audit fixes.

Covers: C-05, H-23, H-27, H-28, H-29, H-31, H-32, H-33, H-34, H-35, M-13, M-14, M-15, L-01, L-02, L-03.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import threading
import time
from types import SimpleNamespace

import httpx
import numpy as np
import pytest

from sayink.config import Config
from sayink.history_store import SCHEMA_VERSION, HistoryStore, SegmentRecord, SessionSummary
from sayink.meeting_session import (
    MeetingController,
    MeetingOptions,
    MeetingState,
    resolve_capabilities,
    safe_filename,
)
from sayink.meeting_speakers import MAX_RECENT_OBSERVATIONS, MeetingSpeakerSession
from sayink.meeting_store import STATUS_ENDED, STATUS_INTERRUPTED
from sayink.minutes_generator import MinutesRequest, MinutesWorker, _preserve_facts
from sayink.minutes_prompts import (
    MeetingContext,
    TranscriptLine,
    chunk_transcript_with_spans,
    get_template,
    render_srt,
)
from sayink.minutes_render import extract_json, render_markdown, replace_names_in_markdown
from sayink.minutes_validation import LLMResponseError, validate_markdown, validate_minutes
from sayink.transcript_import import (
    TranscriptDecodeError,
    decode_transcript,
    parse_transcript,
)
from sayink.ui.history_export import (
    build_batch_export_json,
    build_batch_export_markdown,
    build_single_session_json,
    build_single_session_markdown,
    build_single_session_plain,
    export_csv,
)


def _make_controller(tmp_path):
    cfg = Config(config_dir=tmp_path / "cfg")
    cfg.set("llm.api_url", "http://localhost:11434/v1")
    cfg.set("llm.model_name", "local-model")
    cfg.set("meeting.auto_generate", False)
    history = HistoryStore(db_path=tmp_path / "history.db")
    controller = MeetingController(cfg, history, pending_segments=lambda: 0)
    return cfg, history, controller


# ---------------------------------------------------------------------------
# 1. C-05: Protect user hand-edited minutes across unlimited regenerations
# ---------------------------------------------------------------------------


def test_c05_save_user_markdown_backs_up_initial_ai_draft_and_survives_regenerations(tmp_path):
    _cfg, history, controller = _make_controller(tmp_path)
    try:
        rec = controller.store.create_meeting(title="C05 纪要防丢压测")
        controller.store.end_meeting(rec.id, ended_at=rec.started_at + 10_000)
        controller.store.save_notes(
            rec.id,
            template_id="general",
            model="m1",
            json_payload='{"title":"初稿","summary":"AI 初稿摘要","topics":[],"decisions":[],"action_items":[],"open_questions":[],"chapters":[],"speakers":[]}',
            markdown="# 初稿\n\nAI 初稿摘要\n",
            duration_ms=100,
        )
        assert controller.store.flush()
        initial = controller.store.get_notes(rec.id)
        assert initial is not None and initial.previous_markdown == ""

        # First user manual edit must back up the initial AI draft into previous_markdown.
        assert controller.save_user_markdown(rec.id, "# 人工精修版\n\n说话人1负责核心交付。\n")
        after_edit = controller.store.get_notes(rec.id)
        assert after_edit is not None
        assert after_edit.user_edited is True
        assert after_edit.previous_markdown == "# 初稿\n\nAI 初稿摘要\n"
        assert after_edit.user_markdown == "# 人工精修版\n\n说话人1负责核心交付。\n"
        assert bool(after_edit.user_markdown) is True

        # Switch templates and regenerate 3 consecutive times without touching manual edit.
        for idx, tpl in enumerate(("standup", "interview", "action_only"), start=1):
            controller.store.save_notes(
                rec.id,
                template_id=tpl,
                model="m1",
                json_payload="",
                markdown=f"# AI 第{idx}次重生成（{tpl}）\n\n内容 {idx}\n",
                duration_ms=50,
            )
            assert controller.store.flush()
            notes = controller.store.get_notes(rec.id)
            assert notes is not None
            assert notes.user_edited is False
            assert bool(notes.user_markdown) is True
            assert notes.user_markdown == "# 人工精修版\n\n说话人1负责核心交付。\n"
            assert notes.previous_markdown == "# 人工精修版\n\n说话人1负责核心交付。\n"

        # Renaming a speaker while viewing an AI regeneration also keeps user_markdown synced.
        controller.store.set_speaker(rec.id, 1, display_name="")
        history.enqueue(
            SegmentRecord(
                rec.id,
                0,
                rec.started_at,
                raw_text="核心交付",
                speaker_id=1,
                trigger_mode="meeting",
                captured_at=rec.started_at,
            )
        )
        assert controller.store.flush()
        assert controller.rename_speaker(rec.id, 1, "陈工") is not None
        synced = controller.store.get_notes(rec.id)
        assert synced is not None and "陈工负责核心交付" in synced.user_markdown

        # One-click restore recovers the hand-edited draft intact with user_edited=True.
        assert controller.restore_user_markdown(rec.id)
        restored = controller.store.get_notes(rec.id)
        assert restored is not None
        assert restored.user_edited is True
        assert "陈工负责核心交付" in restored.markdown
    finally:
        controller.shutdown()
        history.close(timeout=2)


def test_c05_legacy_schema_migrates_user_markdown_and_cancels_inflight_generation(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                raw_text TEXT NOT NULL,
                polished_text TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                duration_ms INTEGER NOT NULL DEFAULT 0,
                target_app TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE meetings (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                participants TEXT NOT NULL DEFAULT '',
                agenda TEXT NOT NULL DEFAULT '',
                notes_user TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                model_id TEXT NOT NULL DEFAULT '',
                vad TEXT NOT NULL DEFAULT '',
                glossary_json TEXT NOT NULL DEFAULT '[]',
                started_at INTEGER NOT NULL,
                ended_at INTEGER,
                status TEXT NOT NULL DEFAULT 'ended',
                summary_preview TEXT
            );
            CREATE TABLE meeting_notes (
                meeting_id TEXT PRIMARY KEY,
                template_id TEXT NOT NULL DEFAULT 'general',
                model TEXT NOT NULL DEFAULT '',
                json_payload TEXT NOT NULL DEFAULT '',
                markdown TEXT NOT NULL DEFAULT '',
                previous_markdown TEXT NOT NULL DEFAULT '',
                user_edited INTEGER NOT NULL DEFAULT 0,
                generated_at INTEGER NOT NULL DEFAULT 0,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                chunked INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO meetings (id, title, started_at, status) VALUES ('m-legacy', '旧版会议', 1000, 'ended');
            INSERT INTO meeting_notes (meeting_id, markdown, user_edited, generated_at)
            VALUES ('m-legacy', '# 旧库手改纪要', 1, 2000);
            PRAGMA user_version = 1;
            """
        )
        conn.commit()
    finally:
        conn.close()

    cfg = Config(config_dir=tmp_path / "cfg")
    history = HistoryStore(db_path=db_path)
    controller = MeetingController(cfg, history, pending_segments=lambda: 0)
    try:
        version = history.read(lambda c: c.execute("PRAGMA user_version").fetchone()[0], default=0)
        assert version == SCHEMA_VERSION
        notes = controller.store.get_notes("m-legacy")
        assert notes is not None
        assert notes.user_markdown == "# 旧库手改纪要"
        assert bool(notes.user_markdown) is True

        cancelled = []
        controller._generating_for = "m-legacy"
        monkeypatch.setattr(controller._generator, "cancel", lambda: cancelled.append(True))
        assert controller.save_user_markdown("m-legacy", "# 新手改稿")
        assert cancelled == [True]
        assert controller.generating_for == ""
    finally:
        controller.shutdown()
        history.close(timeout=2)


# ---------------------------------------------------------------------------
# 2. H-23: Speaker observation eviction protection & fast bounded finalize
# ---------------------------------------------------------------------------


def test_h23_eviction_protects_confirmed_and_anchor_observations():
    session = MeetingSpeakerSession(threshold=0.62, keep_history=True)
    v1 = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    v2 = np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32)

    assert session.assign(v1, route="mic", sample_key=0, learnable=True) == 1
    assert session.assign(v2, route="system", sample_key=1, learnable=True) == 2
    assert session.assign(v1, route="mic", sample_key=3, learnable=True) == 1
    assert session.assign(v2, route="system", sample_key=4, learnable=True) == 2
    assert session.confirm(3, 1) is True

    for key in range(10, 10 + MAX_RECENT_OBSERVATIONS + 40):
        vec = v1 if key % 2 == 0 else v2
        route = "mic" if key % 2 == 0 else "system"
        session.assign(vec, route=route, sample_key=key, learnable=True)

    kept_keys = set(session._observations.keys())
    assert len(session._observations) == MAX_RECENT_OBSERVATIONS
    # First-turn anchors (0, 1) remain preserved in speaker profiles even after >512 turns,
    # and manually confirmed turn (3) is protected in _observations so later re-confirmation works!
    assert 0 in session._profiles[1].samples
    assert 1 in session._profiles[2].samples
    assert 3 in kept_keys
    assert 4 not in kept_keys
    assert session.confirm(4, 2) is False
    # Re-assigning the protected confirmed turn (3) after >512 turns succeeds cleanly!
    assert session.confirm(3, 2) is True
    assert 3 in session._profiles[2].confirmed
    assert 3 not in session._profiles[1].confirmed
    assert any(t.key == 3 and t.speaker_id == 2 for t in session.turns)


def test_h23_finalize_with_over_600_turns_uses_bounded_clustering_quickly(tmp_path):
    cfg, history, controller = _make_controller(tmp_path)
    try:
        opts = MeetingOptions(title="超长会议结算", input_source="mixed")
        caps = resolve_capabilities(cfg, opts, dictation_model_id="funasr-nano")
        rec = controller.begin(opts, caps)
        v1 = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        v2 = np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32)
        controller._speakers = MeetingSpeakerSession(threshold=0.62, keep_history=True)

        for seq in range(640):
            sid = controller._speakers.assign(
                v1 if seq % 2 == 0 else v2,
                route="mic" if seq % 2 == 0 else "system",
                start_ms=seq * 500,
                end_ms=seq * 500 + 1200,
                sample_key=seq,
                learnable=True,
            )
            history.enqueue(
                SegmentRecord(
                    rec.id,
                    seq,
                    rec.started_at + seq * 500,
                    raw_text=f"第{seq}轮发言",
                    duration_ms=400,
                    trigger_mode="meeting",
                    speaker_id=sid,
                    captured_at=rec.started_at + seq * 500,
                )
            )
        controller.store.set_segment_speaker(rec.id, 5, 2)
        # 640 queued writes are setup. The bound under test is request_end below.
        assert controller.store.flush(timeout=30.0)

        assert len(controller._speakers._observations) <= MAX_RECENT_OBSERVATIONS

        t0 = time.perf_counter()
        controller.request_end()
        elapsed = time.perf_counter() - t0
        assert controller.state == MeetingState.IDLE
        assert elapsed < 10.0
        locked_seg = controller.store.get_segment(rec.id, 5)
        assert locked_seg is not None and locked_seg.speaker_id == 2 and locked_seg.speaker_locked
    finally:
        controller.shutdown()
        history.close(timeout=2)


# ---------------------------------------------------------------------------
# 3. H-27 & H-32: Minutes validation coercion, chunk fallback & fact merge
# ---------------------------------------------------------------------------


def test_h27_validate_minutes_coerces_string_entries_and_strips_thinking():
    raw_payload = {
        "title": "版本评审",
        "summary": "整体按计划推进。",
        "topics": [{"title": "联调进度", "points": ["完成接口联调", {"text": "压测通过", "ts": "00:01:05"}]}],
        "decisions": ["本周五晚发布 v1.4", {"text": "开启默认剪贴板恢复"}],
        "action_items": ["张三补充回归测试"],
        "open_questions": ["灰度比例是否扩至 20%"],
        "chapters": [],
        "speakers": [],
    }
    cleaned = validate_minutes(raw_payload)
    assert [d["text"] for d in cleaned["decisions"]] == ["本周五晚发布 v1.4", "开启默认剪贴板恢复"]
    assert [a["task"] for a in cleaned["action_items"]] == ["张三补充回归测试"]
    assert [q["text"] for q in cleaned["open_questions"]] == ["灰度比例是否扩至 20%"]
    assert [p["text"] for p in cleaned["topics"][0]["points"]] == ["完成接口联调", "压测通过"]

    with pytest.raises(LLMResponseError):
        validate_minutes({"summary": ["bad list"]})
    with pytest.raises(LLMResponseError):
        validate_minutes({"decisions": "not a list"})

    obj = extract_json('思考草稿...\n</think>\n```json\n{"title": "T", "summary": "S"}\n```')
    assert obj == {"title": "T", "summary": "S"}
    md = validate_markdown("<think>内部推理</think>\n## 结论\n\n正式纪要内容充足且无乱码。")
    assert "<think>" not in md and "内部推理" not in md and "## 结论" in md


def test_h27_h32_multichunk_mark_slicing_markdown_fallback_and_no_fact_resurrection(monkeypatch):
    monkeypatch.setattr("sayink.minutes_generator._RETRY_DELAY_SEC", 0.0)
    lines = [
        TranscriptLine(2_000, 1, "我们先暂时考虑方案 A。" * 6),
        TranscriptLine(8_000, 2, "部署窗口还没定。" * 6),
        TranscriptLine(65_000, 1, "经过压测，推翻方案 A，最终决定采用方案 B。" * 6),
        TranscriptLine(72_000, 2, "部署窗口已定在周五晚十点，李四负责发版。" * 6),
    ]
    spans = chunk_transcript_with_spans(lines, {1: "张三", 2: "李四"}, max_chars=160)
    assert len(spans) >= 2
    assert spans[0][1] == 0 and spans[-1][2] >= 72_000

    sent_prompts: list[str] = []
    # Chunk 1 returns Markdown twice (triggering H-27 Markdown chunk fallback!),
    # remaining chunk(s) return JSON, and merge returns consolidated JSON.
    merge_json = json.dumps(
        {
            "title": "架构评审",
            "summary": "经压测推翻早期设想，最终采用方案 B 并定于周五晚发版。",
            "topics": [{"title": "方案选型", "points": [{"text": "最终采用方案 B", "ts": "00:01:05"}]}],
            "decisions": [{"text": "最终决定采用方案 B，周五晚十点发版", "ts": "00:01:05"}],
            "action_items": [{"owner": "李四", "task": "周五晚十点执行发版", "due": "周五", "ts": "00:01:12"}],
            "open_questions": [],
            "chapters": [],
            "speakers": [],
        },
        ensure_ascii=False,
    )

    def _mock_transport(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        user_msg = body["messages"][1]["content"]
        sent_prompts.append(user_msg)
        if "第 1/" in user_msg:
            reply = "## 早期讨论\n- [00:00:02] 张三先考虑方案 A\n"
        elif "段）：" in user_msg:
            reply = json.dumps(
                {
                    "title": "",
                    "summary": "推翻方案 A，改用方案 B。",
                    "topics": [],
                    "decisions": [{"text": "最终决定采用方案 B", "ts": "00:01:05"}],
                    "action_items": [{"owner": "李四", "task": "周五晚十点执行发版", "due": "周五", "ts": "00:01:12"}],
                    "open_questions": [],
                    "chapters": [],
                    "speakers": [],
                },
                ensure_ascii=False,
            )
        else:
            reply = merge_json
        sse = (
            "data: " + json.dumps({"choices": [{"delta": {"content": reply}, "finish_reason": "stop"}]}) + "\n\n"
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse.encode("utf-8"))

    orig_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: orig_client(transport=httpx.MockTransport(_mock_transport), **kw),
    )

    req = MinutesRequest(
        api_url="http://localhost:11434/v1",
        api_key="",
        model_name="m",
        template=get_template("general"),
        context=MeetingContext(
            title="架构评审",
            notes_user="全会笔记仅在最终汇总出现",
            marks=[(3_000, "早期标记"), (70_000, "最终拍板标记")],
        ),
        lines=lines,
        speaker_names={1: "张三", 2: "李四"},
        duration_ms=80_000,
        chunk_chars=160,
    )
    worker = MinutesWorker(req)
    ok, errs = [], []
    worker.finished_ok.connect(ok.append)
    worker.error.connect(errs.append)
    worker.run()

    assert not errs and len(ok) == 1
    chunk1_prompts = [p for p in sent_prompts if "第 1/" in p]
    later_chunk_prompts = [p for p in sent_prompts if "段）：" in p and "第 1/" not in p]
    merge_prompts = [p for p in sent_prompts if "中间纪要 1/" in p]
    assert chunk1_prompts and merge_prompts
    # Mark slicing & notes_user placement:
    assert "早期标记" in chunk1_prompts[0]
    assert "最终拍板标记" not in chunk1_prompts[0]
    assert "全会笔记仅在最终汇总出现" not in chunk1_prompts[0]
    assert any("最终拍板标记" in p for p in later_chunk_prompts)
    assert all("早期标记" not in p for p in later_chunk_prompts)
    assert "全会笔记仅在最终汇总出现" in merge_prompts[0]

    # Direct unit verification of _preserve_facts: overturned early decisions and
    # resolved open questions must NOT be resurrected when merge already consolidated them!
    partials = [
        {
            "decisions": [{"text": "暂定采用方案 A", "ts": "00:00:02"}],
            "action_items": [{"owner": "李四", "task": "准备发布脚本", "due": "", "ts": "00:00:10"}],
            "open_questions": [{"text": "发布窗口未定", "ts": "00:00:08"}],
            "chapters": [],
            "speakers": [],
        },
        {
            "decisions": [{"text": "放弃方案 A，最终采用方案 B", "ts": "00:01:05"}],
            "action_items": [],
            "open_questions": [],
            "chapters": [],
            "speakers": [],
        },
    ]
    merged = {
        "decisions": [{"text": "最终采用方案 B", "ts": "00:01:05"}],
        "action_items": [],
        "open_questions": [],
        "chapters": [],
        "speakers": [],
    }
    _preserve_facts(merged, partials)
    assert [d["text"] for d in merged["decisions"]] == ["最终采用方案 B"]
    assert merged["open_questions"] == []
    # Meanwhile, action_items (completely omitted by merge) IS rescued!
    assert [a["task"] for a in merged["action_items"]] == ["准备发布脚本"]


# ---------------------------------------------------------------------------
# 4. H-28 & H-29: begin() flush check, audio cleanup, pause clock & SRT cap
# ---------------------------------------------------------------------------


def test_h28_begin_raises_on_flush_failure_and_cleans_owned_audio_on_delete(tmp_path, monkeypatch):
    cfg, history, controller = _make_controller(tmp_path)
    try:
        monkeypatch.setattr(controller.store, "flush", lambda timeout=5.0: False)
        opts = MeetingOptions(title="幽灵会议拦截", input_source="microphone")
        caps = resolve_capabilities(cfg, opts, dictation_model_id="funasr-nano")
        with pytest.raises(RuntimeError, match="无法写入本机数据库"):
            controller.begin(opts, caps)
        assert controller.state == MeetingState.IDLE
        assert controller.current is None
    finally:
        controller.shutdown()
        history.close(timeout=2)

    # Owned cached audio cleanup vs external user media safety:
    _cfg2, history2, controller2 = _make_controller(tmp_path / "clean")
    try:
        cache_dir = tmp_path / "clean" / "sayink_audio_cache"
        cache_dir.mkdir(parents=True)
        owned_wav = cache_dir / "rec1.wav"
        owned_wav.write_bytes(b"RIFF0000WAVE")

        external_dir = tmp_path / "user_desktop"
        external_dir.mkdir(parents=True)
        user_mp3 = external_dir / "important_interview.mp3"
        user_mp3.write_bytes(b"USER_ORIGINAL_AUDIO")

        m1 = controller2.store.create_meeting(title="带缓存录音", audio_path=str(owned_wav))
        m2 = controller2.store.create_meeting(title="外部导入音频", audio_path=str(user_mp3))
        assert controller2.store.flush()
        assert controller2.store.get_meeting(m1.id).audio_path == str(owned_wav)

        controller2.store.delete_meeting(m1.id)
        controller2.store.delete_meetings([m2.id])
        assert controller2.store.flush()
        assert not owned_wav.exists()
        assert user_mp3.exists()
    finally:
        controller2.shutdown()
        history2.close(timeout=2)


def test_h28_abandon_on_exit_and_mark_interrupted_use_segment_end_and_autoname(tmp_path):
    cfg, history, controller = _make_controller(tmp_path)
    try:
        opts = MeetingOptions(title="异常退出测试", input_source="microphone")
        caps = resolve_capabilities(cfg, opts, dictation_model_id="funasr-nano")
        rec = controller.begin(opts, caps)
        history.enqueue(
            SegmentRecord(
                rec.id,
                0,
                rec.started_at + 500,
                raw_text="大家好，我是林峰，今天确认发布计划。",
                duration_ms=8_500,
                trigger_mode="meeting",
                speaker_id=1,
                captured_at=rec.started_at + 2_000,
            )
        )
        assert controller.store.flush()
        controller.abandon_on_exit()

        saved = controller.store.get_meeting(rec.id)
        assert saved is not None and saved.status == STATUS_INTERRUPTED
        assert saved.ended_at == rec.started_at + 10_500
        assert controller.speaker_names(rec.id).get(1) == "林峰"

        # Startup mark_interrupted_meetings also uses MAX(captured_at + duration_ms).
        crash_rec = controller.store.create_meeting(title="断电会议", started_at=100_000)
        history.enqueue(
            SegmentRecord(
                crash_rec.id,
                0,
                101_000,
                raw_text="尾音测试",
                duration_ms=14_000,
                trigger_mode="meeting",
                speaker_id=1,
                captured_at=105_000,
            )
        )
        assert controller.store.flush()
        controller.store.mark_interrupted_meetings()
        assert controller.store.flush()
        fixed = controller.store.get_meeting(crash_rec.id)
        assert fixed is not None and fixed.status == STATUS_INTERRUPTED
        assert fixed.ended_at == 119_000
    finally:
        controller.shutdown()
        history.close(timeout=2)


def test_h29_pause_resume_deducts_tea_break_across_clock_marks_segments_and_srt(tmp_path, monkeypatch):
    now_box = [1_000_000]
    monkeypatch.setattr("sayink.meeting_session.time.time", lambda: now_box[0] / 1000.0)
    monkeypatch.setattr("sayink.meeting_store.now_ms", lambda: now_box[0])

    cfg, history, controller = _make_controller(tmp_path)
    try:
        opts = MeetingOptions(title="茶歇校准会议", input_source="mixed")
        caps = resolve_capabilities(cfg, opts, dictation_model_id="funasr-nano")
        rec = controller.begin(opts, caps)
        assert controller._clock.isActive()

        # Speak for 10s, then pause for a 50-minute (3,000,000 ms) break.
        history.enqueue(
            SegmentRecord(
                rec.id,
                0,
                1_002_000,
                raw_text="上半场结论。",
                duration_ms=3_000,
                trigger_mode="meeting",
                speaker_id=1,
                captured_at=1_002_000,
            )
        )
        now_box[0] = 1_010_000
        controller.pause("茶歇暂停")
        assert not controller._clock.isActive()
        assert controller.elapsed_ms() == 10_000

        # Advance wall clock by 50 minutes while paused: elapsed_ms & marks must NOT advance!
        now_box[0] = 4_010_000
        assert controller.elapsed_ms() == 10_000
        mark_offset = controller.add_mark("暂停态书签")
        assert mark_offset == 10_000

        controller.resume()
        assert controller._clock.isActive()

        # 5s after resume (wall=4_015_000), effective meeting offset is 15_000 ms!
        now_box[0] = 4_015_000
        assert controller.elapsed_ms() == 15_000
        history.enqueue(
            SegmentRecord(
                rec.id,
                1,
                4_016_000,
                raw_text="下半场开场。",
                duration_ms=4_000,
                trigger_mode="meeting",
                speaker_id=2,
                captured_at=4_015_000,
            )
        )
        now_box[0] = 4_020_000
        assert controller.store.flush()
        controller.request_end()

        ended = controller.store.get_meeting(rec.id)
        assert ended is not None and ended.status == STATUS_ENDED
        # Total effective duration is 20s (NOT 50m20s = 3,020,000 ms)!
        assert ended.duration_ms == 20_000
        segs = controller.store.get_segments(rec.id)
        assert segs[1].captured_at == rec.started_at + 15_000

        # Also verify render_srt caps 0-duration cues across huge silence gaps at <= 10s!
        srt = render_srt(
            [
                TranscriptLine(0, 1, "第一句无时长", duration_ms=0),
                TranscriptLine(2_400_000, 2, "四十分钟后第二句", duration_ms=2_000),
            ],
            {1: "张三", 2: "李四"},
        )
        assert "00:00:00,000 --> 00:00:10,000" in srt
        assert "00:40:00,000" in srt
    finally:
        controller.shutdown()
        history.close(timeout=2)


# ---------------------------------------------------------------------------
# 5. H-31: Media import glossary, meeting.model_id, auto_name & retry dedup
# ---------------------------------------------------------------------------


def test_h31_media_import_persists_glossary_model_autonames_and_dedups_retry(tmp_path, monkeypatch):
    cfg, history, controller = _make_controller(tmp_path)
    try:
        cfg.set("meeting.model_id", "qwen3-asr-0.6b")
        cfg.set("stt.glossary", [{"term": "SayInk", "aliases": ["赛音"]}])
        media_result = SimpleNamespace(
            turns=[
                SimpleNamespace(offset_ms=0, duration_ms=3000, speaker_id=1, text="大家好，我是赵一淳，讨论 SayInk。"),
                SimpleNamespace(offset_ms=4000, duration_ms=2000, speaker_id=2, text="收到。"),
            ],
            audio_ms=6000,
            cancelled=False,
            error="",
        )

        # Simulate first flush timing out while the write actually lands in SQLite,
        # then retry_media_save() must NOT create a duplicate second meeting!
        real_flush = controller.store.flush
        calls = [0]

        def _flaky_flush(timeout: float = 5.0) -> bool:
            calls[0] += 1
            real_flush(timeout=timeout)
            return calls[0] > 1

        monkeypatch.setattr(controller.store, "flush", _flaky_flush)
        controller._media_title = "客户访谈录音"
        controller._on_media_job_done(media_result)
        assert "重试保存" in controller.media_import_notice

        err = controller.retry_media_save()
        assert err == ""
        meetings = controller.store.list_meetings()
        assert len(meetings) == 1
        saved = meetings[0]
        assert saved.model_id == "qwen3-asr-0.6b"
        assert saved.glossary == [{"term": "SayInk", "aliases": ["赛音"]}]
        assert controller.speaker_names(saved.id).get(1) == "赵一淳"
    finally:
        controller.shutdown()
        history.close(timeout=2)


# ---------------------------------------------------------------------------
# 6. H-33: Speaker rename Chinese boundaries, spaced labels & refine sync
# ---------------------------------------------------------------------------


def test_h33_speaker_rename_boundaries_spaced_labels_and_refinement_sync():
    md = (
        "会后由张三推进接口，请张三与王总确认排期，和张三跟进联调；\n"
        "王总同步风险，王总监列席旁听；\n"
        "说话人 1 提出预案，说话人 11 补充细节。\n"
    )
    step1, count1 = replace_names_in_markdown(md, "张三", "张明")
    assert count1 == 3
    assert "由张明推进接口" in step1
    assert "请张明与王总确认排期" in step1
    assert "和张明跟进联调" in step1

    step2, count2 = replace_names_in_markdown(step1, "王总", "王伟")
    assert count2 == 2
    assert "与王伟确认排期" in step2
    assert "王伟同步风险" in step2
    assert "王总监列席旁听" in step2  # Must NEVER corrupt 王总监 -> 王伟监!

    step3, count3 = replace_names_in_markdown(step2, "说话人1", "李娜")
    assert count3 == 1
    assert "李娜 提出预案" in step3 or "李娜提出预案" in step3
    assert "说话人 11 补充细节" in step3

    # Refinement JSON document re-renders speaker headings & prose when renamed!
    refine_payload = {
        "kind": "refine",
        "_speaker_names": {"1": "说话人1", "2": "王总"},
        "paragraphs": [
            {"seq": 1, "ts": "00:00:01", "speaker": "说话人 1", "text": "请王总确认预算。"},
            {"seq": 2, "ts": "00:00:05", "speaker": "王总", "text": "由说话人 1 跟进执行。"},
        ],
    }
    rendered = render_markdown(
        refine_payload,
        template=get_template("refine"),
        speaker_names={1: "周敏", 2: "王海"},
        duration_ms=10_000,
    )
    assert "周敏" in rendered and "王海" in rendered
    assert "请王海确认预算" in rendered
    assert "由周敏跟进执行" in rendered or "由周敏 跟进执行" in rendered
    assert "说话人1" not in rendered and "说话人 1" not in rendered


# ---------------------------------------------------------------------------
# 7. H-34: HistoryStore.flush() per-barrier truth & dead-thread conn reaping
# ---------------------------------------------------------------------------


def test_h34_flush_recovers_after_single_failure_and_reaps_dead_read_conns(tmp_path):
    history = HistoryStore(db_path=tmp_path / "h34.db")
    try:
        def _boom(_conn: sqlite3.Connection) -> None:
            raise sqlite3.OperationalError("simulated transient write error")

        history.enqueue_write(_boom)
        assert history.flush(timeout=2.0) is False

        # Subsequent healthy writes MUST return True instead of being poisoned forever!
        history.enqueue(SegmentRecord("s-ok", 0, 1000, raw_text="正常入库"))
        assert history.flush(timeout=2.0) is True
        assert len(history.get_session_segments("s-ok")) == 1

        # Spawn short-lived worker threads that open thread-local read connections.
        def _short_reader() -> None:
            rows = history.get_session_segments("s-ok")
            assert len(rows) == 1

        threads = [threading.Thread(target=_short_reader) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert history.flush(timeout=2.0) is True
        with history._read_lock:
            owners = [ref() for ref in history._read_conn_threads.values()]
            assert len(history._read_conns) == 1
        assert all(t is not None and t.is_alive() for t in owners)
    finally:
        history.close(timeout=2)


# ---------------------------------------------------------------------------
# 8. H-35 & M-14: VTT/SRT parsing, BOM/UTF-32/UTF-16, 2-part clocks & JSON
# ---------------------------------------------------------------------------


def test_h35_single_digit_hours_vtt_meta_boundaries_tags_and_entities():
    vtt = (
        "WEBVTT\n\n"
        "NOTE 顶部备注块应当跳过\n\n"
        "STYLE\n::cue { color: yellow; }\n\n"
        "1:00:05.000 --> 1:00:08.500\n"
        "<v.loud.lead 张三><01:00:05.500><c.yellow>检索 &amp; 排序</c>已就绪</v>\n\n"
        "1:00:10.000 --> 1:00:12.000\n"
        "NOTEBOOK 评测全部通过\n\n"
        "1:00:13.000 --> 1:00:15.000\n"
        "STYLESHEET 令牌已对齐 &lt;4.5:1&gt;\n"
    )
    parsed = parse_transcript(vtt)
    assert parsed.format_label == "WebVTT 字幕"
    assert len(parsed.turns) == 3
    assert parsed.turns[0].speaker_label == "张三"
    assert parsed.turns[0].offset_ms == 3_605_000
    assert parsed.turns[0].duration_ms == 3_500
    assert parsed.turns[0].text == "检索 & 排序已就绪"
    assert parsed.turns[1].text == "NOTEBOOK 评测全部通过"
    assert parsed.turns[2].text == "STYLESHEET 令牌已对齐 <4.5:1>"


def test_m14_utf32_bom_bomless_utf16_two_part_wall_clocks_and_json_roundtrip():
    sample = "张三：第一条发言。\n李四：第二条发言。"
    # 4-byte UTF-32LE & UTF-32BE BOM must not be misread as UTF-16!
    assert decode_transcript(b"\xff\xfe\x00\x00" + sample.encode("utf-32-le")) == sample
    assert decode_transcript(b"\x00\x00\xfe\xff" + sample.encode("utf-32-be")) == sample
    with pytest.raises(TranscriptDecodeError, match="UTF-32"):
        decode_transcript(b"\xff\xfe\x00\x00\x61")

    # BOM-less UTF-16LE / UTF-16BE + stray NUL cleanup:
    assert decode_transcript(sample.encode("utf-16-le")) == sample
    assert decode_transcript(sample.encode("utf-16-be")) == sample
    assert "\x00" not in decode_transcript(b"Speaker 1: hello\x00\nSpeaker 2: world\n")

    # Dated 2-part wall clock HH:MM vs >59m elapsed MM:SS:
    dated_blocks = parse_transcript(
        "张三 2026-11-12 14:30\n下午评审开始。\n\n李四 2026-11-12 15:05\n确认通过。\n"
    )
    assert [(t.speaker_label, t.offset_ms) for t in dated_blocks.turns] == [
        ("张三", (14 * 3600 + 30 * 60) * 1000),
        ("李四", (15 * 3600 + 5 * 60) * 1000),
    ]
    long_elapsed = parse_transcript("[65:30] 张三：超一小时节点\n[66:00] 李四：收尾\n")
    assert [t.offset_ms for t in long_elapsed.turns] == [3_930_000, 3_960_000]

    # SayInk history single & batch JSON export round-trip:
    session = SessionSummary(
        session_id="s1",
        created_at=1_700_000_000_000,
        segment_count=2,
        source="mixed",
        target_app="Code.exe",
        preview="检索联调",
    )
    segs = [
        SegmentRecord(
            "s1", 0, 1_700_000_000_000, raw_text="初稿一", polished_text="润色一",
            duration_ms=2000, speaker_id=1, captured_at=1_700_000_001_000,
        ),
        SegmentRecord(
            "s1", 1, 1_700_000_004_000, raw_text="被合并旧句", polished_text="\u200b",
            duration_ms=1000, speaker_id=1, captured_at=1_700_000_004_000,
        ),
        SegmentRecord(
            "s1", 2, 1_700_000_006_000, raw_text="初稿二", polished_text="润色二",
            duration_ms=3000, speaker_id=2, captured_at=1_700_000_006_000,
        ),
    ]
    single_json = build_single_session_json(session, segs)
    reimported_single = parse_transcript(single_json)
    assert reimported_single.format_label == "JSON 记录"
    assert [(t.speaker_label, t.offset_ms, t.text) for t in reimported_single.turns] == [
        ("说话人1", 0, "润色一"),
        ("说话人2", 5_000, "润色二"),
    ]

    batch_json = build_batch_export_json([session], {"s1": segs})
    reimported_batch = parse_transcript(batch_json)
    assert [t.text for t in reimported_batch.turns] == ["润色一", "润色二"]


# ---------------------------------------------------------------------------
# 9. M-13, M-15, L-01, L-02, L-03: Search, Windows names, YAML, prefer_raw, CSV
# ---------------------------------------------------------------------------


def test_m13_search_meetings_and_history_escape_wildcards_and_skip_stale_raw(tmp_path):
    _cfg, history, controller = _make_controller(tmp_path)
    try:
        m = controller.store.create_meeting(title="100% 达成_复盘")
        controller.store.set_speaker(m.id, 1, display_name="欧阳修")
        history.enqueue(
            SegmentRecord(
                m.id, 0, m.started_at, raw_text="常规开场", trigger_mode="meeting",
                speaker_id=1, captured_at=m.started_at,
            )
        )
        # Non-meeting dictation row must NOT pollute search_meetings:
        history.enqueue(
            SegmentRecord("dictation-only", 0, m.started_at, raw_text="秘密口述词", trigger_mode="hold")
        )
        # History session where inline edit replaced raw_text and suppressed row 1 with \u200b:
        history.enqueue(
            SegmentRecord("hist-edit", 0, m.started_at + 100, raw_text="旧错别字版本", polished_text="初版润色")
        )
        history.enqueue(
            SegmentRecord("hist-edit", 1, m.started_at + 200, raw_text="已合并残影词", polished_text="第二句")
        )
        history.enqueue_update_polished("hist-edit", {0: "全新修订定稿", 1: "\u200b"})
        # AI-polished session (not manually edited): polished view vs raw view filtering:
        history.enqueue(
            SegmentRecord("hist-polish", 0, m.started_at + 300, raw_text="原声口误词", polished_text="AI精修文本")
        )
        assert controller.store.flush()

        # Speaker name search works:
        assert [r.id for r in controller.store.search_meetings("欧阳修")] == [m.id]
        # Wildcards % and _ are escaped literally:
        assert [r.id for r in controller.store.search_meetings("100%")] == [m.id]
        assert controller.store.search_meetings("99%") == []
        assert controller.store.search_meetings("达成X复盘") == []
        assert controller.store.search_meetings("秘密口述词") == []

        # History search skips manually overwritten raw_text and \u200b suppressed rows:
        assert history.search_sessions("旧错别字版本") == []
        assert history.search_sessions("已合并残影词") == []
        assert history.search_sessions("\u200b") == []
        assert [s.session_id for s in history.search_sessions("全新修订定稿")] == ["hist-edit"]
        # View-mode filtering (prefer_raw=False vs prefer_raw=True):
        assert history.search_sessions("原声口误词", prefer_raw=False) == []
        assert [s.session_id for s in history.search_sessions("原声口误词", prefer_raw=True)] == ["hist-polish"]
    finally:
        controller.shutdown()
        history.close(timeout=2)


def test_m15_l01_l03_safe_filename_yaml_prefer_raw_timestamps_and_csv(tmp_path):
    for reserved in ("CON", "prn", "Aux", "NUL", "COM1", "LPT9", "con.md"):
        cleaned = safe_filename(reserved)
        stem = cleaned.split(".")[0].upper()
        assert stem not in {"CON", "PRN", "AUX", "NUL", "COM1", "LPT9"}

    # Extreme negative & huge timestamps must never raise OSError on Windows CRT:
    weird_session = SessionSummary(
        session_id="s-weird",
        created_at=-99_999_999_999_999,
        segment_count=2,
        preview="第一行预览\r\n# 注入标题",
        source="mixed: #1",
        target_app="App: My#Notes\r\nPro.exe",
    )
    segs = [
        SegmentRecord(
            "s-weird",
            0,
            -99_999_999_999_999,
            raw_text="原始未润色口述",
            polished_text="=SUM(1,2) 润色后文本",
            duration_ms=1500,
            model="qwen:latest #fast",
        ),
        SegmentRecord(
            "s-weird",
            1,
            10**18,
            raw_text="被抑制段的原始句",
            polished_text="\u200b",
            duration_ms=500,
        ),
    ]

    md_polished = build_single_session_markdown(weird_session, segs, prefer_raw=False)
    md_raw = build_single_session_markdown(weird_session, segs, prefer_raw=True)
    plain_raw = build_single_session_plain(weird_session, segs, prefer_raw=True)
    batch_md = build_batch_export_markdown([weird_session], {"s-weird": segs}, prefer_raw=True)

    assert 'source: "mixed: #1"' in md_polished
    assert 'target_app: "App: My#Notes Pro.exe"' in md_polished
    assert 'model: "qwen:latest #fast"' in md_polished
    assert "=SUM(1,2) 润色后文本" in md_polished and "原始未润色口述" not in md_polished
    assert "原始未润色口述" in md_raw and "被抑制段的原始句" in md_raw
    assert "原始未润色口述" in plain_raw
    assert "\n# 注入标题" not in batch_md
    assert "第一行预览 # 注入标题" in batch_md

    # CSV export: UTF-8 BOM (utf-8-sig) + formula injection neutralization + prefer_raw:
    csv_path = tmp_path / "export.csv"
    returned = export_csv(csv_path, [weird_session], {"s-weird": segs}, prefer_raw=False)
    raw_bytes = csv_path.read_bytes()
    assert raw_bytes.startswith(b"\xef\xbb\xbf")
    assert not raw_bytes.startswith(b"\xef\xbb\xbf\xef\xbb\xbf")
    assert returned.startswith("\ufeff")

    rows = list(csv.reader(io.StringIO(raw_bytes.decode("utf-8-sig"))))
    assert rows[0][0] == "session_id"
    assert rows[1][7].startswith("'=SUM(1,2)")

    csv_raw_str = export_csv(weird_session, segs, prefer_raw=True)
    rows_raw = list(csv.reader(io.StringIO(csv_raw_str.lstrip("\ufeff"))))
    assert "原始未润色口述" in rows_raw[1][7]

    # L-01: Concurrent cross-thread reads of controller.current_id never TOCTOU-crash:
    _cfg, history, controller = _make_controller(tmp_path / "toctou")
    try:
        errors: list[Exception] = []
        stop = threading.Event()

        def _mutator() -> None:
            rec = SimpleNamespace(id="live-123")
            for _ in range(500):
                with controller._current_lock:
                    controller._current = rec  # type: ignore[assignment]
                with controller._current_lock:
                    controller._current = None
            stop.set()

        def _reader() -> None:
            while not stop.is_set():
                try:
                    cid = controller.current_id
                    assert cid in ("", "live-123")
                except Exception as exc:  # pragma: no cover
                    errors.append(exc)

        t1 = threading.Thread(target=_mutator)
        t2 = threading.Thread(target=_reader)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        assert errors == []
    finally:
        controller.shutdown()
        history.close(timeout=2)


def test_cross_backend_recovery_cleanup_and_media_audio_path(tmp_path) -> None:
    """Cross-backend seam: HistoryStore/MeetingStore recovery cleanup & media audio_path."""
    from pathlib import Path
    import wave

    from sayink.media_import import MediaTurn, RecognitionResult, recognize_file

    _cfg, history, controller = _make_controller(tmp_path / "cross")
    try:
        recovery_root = history.db_path.parent / "audio-recovery"
        recovery_root.mkdir(parents=True, exist_ok=True)

        # 1) Dictation session recovery journal cleaned on enqueue_delete_sessions,
        #    while meeting recovery journal is protected from dictation delete.
        dict_journal = recovery_root / ("a" * 32)
        dict_journal.mkdir()
        (dict_journal / "session.json").write_text(
            json.dumps({"session_id": "dict-sess-1", "complete": True}),
            encoding="utf-8",
        )
        (dict_journal / "audio.f32").write_bytes(b"\x00" * 64)

        mtg = controller.store.create_meeting(title="受保护会议")
        assert history.flush()
        mtg_journal = recovery_root / ("b" * 32)
        mtg_journal.mkdir()
        (mtg_journal / "session.json").write_text(
            json.dumps({"session_id": mtg.id, "complete": True}),
            encoding="utf-8",
        )
        (mtg_journal / "audio.f32").write_bytes(b"\x00" * 64)

        history.enqueue(
            SegmentRecord(
                session_id="dict-sess-1",
                seq=0,
                created_at=1_700_000_000_000,
                raw_text="口述内容",
                polished_text="口述内容",
                source="microphone",
                duration_ms=1000,
                target_app="notepad.exe",
                trigger_mode="hold",
                model="funasr-nano",
            )
        )
        assert history.flush()

        # Passing both dictation session and meeting ID to dictation delete:
        history.enqueue_delete_sessions(["dict-sess-1", mtg.id])
        assert history.flush()
        assert not dict_journal.exists()
        assert mtg_journal.exists()

        # Deleting the meeting via MeetingStore cleans up its recovery journal:
        controller.store.end_meeting(mtg.id, ended_at=mtg.started_at + 5000)
        controller.store.delete_meeting(mtg.id)
        assert history.flush()
        assert not mtg_journal.exists()

        # 2) recognize_file & commit_media_import persist resolved audio_path:
        wav_path = tmp_path / "sample_import.wav"
        with wave.open(str(wav_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(b"\x10\x00" * 16000)

        class _StubRecognizer:
            def decode_meeting_blocking(self, audio, *, turn_analyzer=None, cancelled=None):
                import numpy as np

                return [
                    type(
                        "_DecodedTurn",
                        (),
                        {
                            "text": "我是周敏，确认导入回放路径。",
                            "start": 0,
                            "stop": len(audio),
                            "embedding": np.ones(8, dtype=np.float32),
                            "learnable": True,
                        },
                    )()
                ], ""

        # Force non-silent chunk by feeding synthetic chunk directly via recognize_file
        import numpy as np

        tone = (0.25 * np.sin(np.linspace(0, 400 * np.pi, 16000, dtype=np.float32)) * 32767).astype("<i2")
        with wave.open(str(wav_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(tone.tobytes() + b"\x00\x00" * 12000)

        res = recognize_file(
            wav_path,
            recognizer=_StubRecognizer(),
            speaker_path=None,
            vad="energy",
            vad_path=None,
            cancel=lambda: False,
        )
        assert Path(res.audio_path) == wav_path.resolve()
        saved = controller.commit_media_import(
            res if res.turns else RecognitionResult(
                turns=(MediaTurn(1, 0, 1000, "我是周敏"),),
                cancelled=False,
                audio_ms=1000,
                audio_path=res.audio_path,
            ),
            "导入回放验证",
        )
        assert saved is not None
        assert Path(saved.audio_path) == wav_path.resolve()
        # Deleting imported media meeting never deletes user's external WAV file!
        controller.store.delete_meeting(saved.id)
        assert history.flush()
        assert wav_path.exists()
    finally:
        controller.shutdown()
        history.close(timeout=2)


def test_live_meeting_pause_resume_appends_single_journal_and_delete_cleans_playback_wav(
    tmp_path, monkeypatch
):
    import numpy as np
    import tempfile
    from sayink.audio_recovery import RecordingJournal, _tree_f32_bytes
    from sayink.audio_recorder import AudioRecorder

    fake_temp = tmp_path / "temp_root"
    fake_temp.mkdir(parents=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_temp))

    cfg, history, controller = _make_controller(tmp_path / "live_mtg")
    try:
        opts = MeetingOptions(title="暂停续写录音测试", input_source="microphone")
        caps = resolve_capabilities(cfg, opts, dictation_model_id="funasr-nano")
        rec = controller.begin(opts, caps)

        rec_root = cfg.config_dir / "audio-recovery"
        part1 = np.full(8000, 0.125, dtype=np.float32)
        part2 = np.full(8000, -0.25, dtype=np.float32)

        # Before pause: create initial meeting journal
        j1 = RecordingJournal(
            rec_root,
            metadata={"session_id": rec.id, "meeting_id": rec.id, "meeting": True},
        )
        j1_dir = j1.path
        started_t0 = j1._meta["started_at"]
        assert j1.append(part1)
        assert j1.close(timeout=3)

        # Recorder keeps _recovery_session_id & _last_session_journal across _close_journal when _meeting_profile=True
        recorder = AudioRecorder()
        recorder._meeting_profile = True
        recorder.set_recovery_session_id(rec.id)
        recorder._last_session_journal = (rec.id, j1_dir)
        recorder._close_journal()
        assert recorder._recovery_session_id == rec.id
        assert recorder._last_session_journal == (rec.id, j1_dir)

        # After resume: RecordingJournal(resume_dir=j1_dir) appends to the SAME hex32 dir!
        j2 = RecordingJournal(
            rec_root,
            metadata={"session_id": rec.id, "meeting_id": rec.id, "meeting": True},
            resume_dir=j1_dir,
        )
        assert j2.path == j1_dir
        assert j2._meta["started_at"] == started_t0
        assert j2.append(part2)
        assert j2.close(timeout=3)

        combined = np.fromfile(j1_dir / "audio.f32", dtype="<f4")
        assert combined.size == 16000
        assert np.allclose(combined[:8000], 0.125)
        assert np.allclose(combined[8000:], -0.25)

        # Bind audio_path to meeting and materialize %TEMP%/sayink-playback/<hex32>-64000.wav
        controller.set_audio_path(str(j1_dir), rec.id)
        assert history.flush()
        assert controller.store.get_meeting(rec.id).audio_path == str(j1_dir)

        playback_dir = fake_temp / "sayink-playback"
        playback_dir.mkdir(parents=True, exist_ok=True)
        playback_wav = playback_dir / f"{j1_dir.name}-64000.wav"
        playback_wav.write_bytes(b"RIFF0000WAVE")

        # Prime _PENDING_BYTES_CACHE before deleting meeting
        assert _tree_f32_bytes(rec_root) == 64000

        controller.store.delete_meeting(rec.id)
        assert history.flush()

        # Both the raw .f32 journal dir AND %TEMP%/sayink-playback WAV are wiped, and quota cache is 0!
        assert not j1_dir.exists()
        assert not playback_wav.exists()
        assert _tree_f32_bytes(rec_root) == 0
    finally:
        controller.shutdown()
        history.close(timeout=2)

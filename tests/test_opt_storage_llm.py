"""Tests for Stage-1 storage & LLM optimizations (branch ``opt/storage-llm``)."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

import sayink.main as main_mod
from sayink.asr_correction import glossary_variant_map_with_conflicts
from sayink.history_store import WRITE_FAILED_NOTICE, HistoryStore, SegmentRecord
from sayink.llm.endpoint import redact_url_query_secrets
from sayink.llm.transport import (
    async_completion,
    chat_completion,
    stream_chat_completion,
    test_endpoint_connection as probe_endpoint_connection,
)
from sayink.meeting_store import MeetingStore
from sayink.minutes_generator import (
    MinutesGenerator,
    MinutesRequest,
    MinutesResult,
    MinutesWorker,
    request_fingerprint,
)
from sayink.minutes_prompts import (
    MERGE_SYSTEM_PROMPT,
    MeetingContext,
    TranscriptLine,
    build_merge_prompt,
    build_user_prompt,
    get_template,
)
from sayink.text_polisher import (
    LLM_MODE_POLISH,
    PolishWorker,
    TextPolisher,
    polish_user_prompt,
    strip_transcript_tags,
    validate_rewrite_output,
)


def _make_segment(
    session_id: str,
    seq: int,
    *,
    raw_text: str = "测试片段",
    target_app: str = "notepad.exe",
    trigger_mode: str = "continuous",
    created_at: int = 1_000,
) -> SegmentRecord:
    return SegmentRecord(
        session_id=session_id,
        seq=seq,
        created_at=created_at,
        raw_text=raw_text,
        source="mic",
        duration_ms=800,
        target_app=target_app,
        trigger_mode=trigger_mode,
        model="funasr-nano",
    )


class TestHistoryStoreOptimizations:
    def test_ticket_settled_callback_fires_async_on_success_and_failure(self, tmp_path: Path) -> None:
        store = HistoryStore(tmp_path / "history.db")
        settled: list[tuple[int, bool, str, str]] = []
        done = threading.Event()

        def on_settled(ticket: int, ok: bool, err: str) -> None:
            settled.append((ticket, ok, err, threading.current_thread().name))
            if len(settled) >= 2:
                done.set()

        store.set_ticket_settled_callback(on_settled)
        try:
            t1 = store.enqueue(_make_segment("sess-ok", 0, raw_text="正常写入"))
            assert t1 > 0
            assert store.flush_ticket(t1, timeout=2.0) is True

            # Force an insert failure on the second segment
            real_insert = store._do_insert

            def fail_once(conn: sqlite3.Connection, rec: SegmentRecord, **kwargs: object) -> None:
                raise sqlite3.OperationalError("simulated disk full")

            store._do_insert = fail_once  # type: ignore[method-assign]
            t2 = store.enqueue(_make_segment("sess-fail", 0, raw_text="失败写入"))
            assert t2 > 0
            assert done.wait(timeout=2.0)
            store._do_insert = real_insert  # type: ignore[method-assign]

            assert settled[0] == (t1, True, "", "HistoryStoreWriter")
            assert settled[1] == (t2, False, WRITE_FAILED_NOTICE, "HistoryStoreWriter")
        finally:
            store.close(timeout=2.0)

    def test_disabled_store_enqueue_returns_zero_and_records_session_failure(self, tmp_path: Path) -> None:
        store = HistoryStore(tmp_path / "history.db")
        store.close(timeout=2.0)
        store.disabled = True

        ticket = store.enqueue(_make_segment("sess-disabled", 0, raw_text="不会入库"))
        assert ticket == 0
        assert store.get_session_write_failures("sess-disabled") == 1
        store.clear_session_write_failures("sess-disabled")
        assert store.get_session_write_failures("sess-disabled") == 0

    def test_deduplicate_session_seq_prevents_duplicate_rows_on_repeated_retry(self, tmp_path: Path) -> None:
        store = HistoryStore(tmp_path / "history.db")
        try:
            seg = _make_segment("sess-retry", 3, raw_text="重试不重复", created_at=3000)
            t1 = store.enqueue(seg, deduplicate_session_seq=True)
            t2 = store.enqueue(seg, deduplicate_session_seq=True)
            t3 = store.enqueue(seg, deduplicate_session_seq=True)
            assert store.flush_ticket(t1, timeout=2.0) is True
            assert store.flush_ticket(t2, timeout=2.0) is True
            assert store.flush_ticket(t3, timeout=2.0) is True

            rows = store.get_session_segments("sess-retry")
            assert len(rows) == 1
            assert rows[0].seq == 3
            assert rows[0].raw_text == "重试不重复"
        finally:
            store.close(timeout=2.0)

    def test_session_write_failures_survive_unscoped_flush(self, tmp_path: Path) -> None:
        store = HistoryStore(tmp_path / "history.db")
        try:
            store.enqueue(_make_segment("mtg-1", 0, raw_text="第一句", trigger_mode="meeting"))
            assert store.flush(timeout=2.0) is True

            real_insert = store._do_insert

            def fail_insert(conn: sqlite3.Connection, rec: SegmentRecord, **kwargs: object) -> None:
                raise sqlite3.OperationalError("simulated lock timeout")

            store._do_insert = fail_insert  # type: ignore[method-assign]
            failed_ticket = store.enqueue(_make_segment("mtg-1", 1, raw_text="丢失段", trigger_mode="meeting"))
            # First unscoped flush consumes internal unobserved flag and returns False
            assert store.flush(timeout=2.0) is False
            # Second unscoped flush (e.g. MeetingController.finalize) returns True on unobserved queue,
            # YET per-session and per-ticket failure visibility MUST remain intact!
            assert store.flush(timeout=2.0) is True
            assert store.get_session_write_failures("mtg-1") == 1
            assert store.is_ticket_failed(failed_ticket) is True
            assert store.get_session_write_failures("other-session") == 0

            store._do_insert = real_insert  # type: ignore[method-assign]
            store.clear_session_write_failures("mtg-1")
            assert store.get_session_write_failures("mtg-1") == 0
        finally:
            store.close(timeout=2.0)

    def test_session_summary_surfaces_first_non_empty_target_app(self, tmp_path: Path) -> None:
        store = HistoryStore(tmp_path / "history.db")
        try:
            store.enqueue(_make_segment("cont-1", 0, raw_text="无焦点首句", target_app="", created_at=1000))
            store.enqueue(_make_segment("cont-1", 1, raw_text="切回编辑器", target_app="Code.exe", created_at=2000))
            store.enqueue(_make_segment("cont-1", 2, raw_text="再切聊天", target_app="WeChat.exe", created_at=3000))
            store.enqueue(_make_segment("cont-empty", 0, raw_text="全程仅历史", target_app="", created_at=4000))
            assert store.flush(timeout=2.0) is True

            listed = {s.session_id: s for s in store.list_sessions(limit=10)}
            assert listed["cont-1"].target_app == "Code.exe"
            assert listed["cont-empty"].target_app == ""

            searched = store.search_sessions("切回编辑器")
            assert len(searched) == 1
            assert searched[0].target_app == "Code.exe"
        finally:
            store.close(timeout=2.0)

    def test_secure_delete_enabled_and_delete_all_vacuums_plaintext_residue(self, tmp_path: Path) -> None:
        db_file = tmp_path / "history.db"
        store = HistoryStore(db_file)
        secret_marker = "TOP_SECRET_TRANSCRIPT_PAYLOAD_99887766"
        try:
            # Verify writer connection enabled secure_delete (1=ON or 2=FAST)
            pragma_holder: list[int] = []
            t_check = store.enqueue_write(
                lambda conn: pragma_holder.append(
                    int(conn.execute("PRAGMA secure_delete").fetchone()[0])
                )
            )
            assert store.flush_ticket(t_check, timeout=2.0) is True
            assert pragma_holder and pragma_holder[0] in (1, 2)

            for idx in range(15):
                store.enqueue(_make_segment(f"sec-{idx}", 0, raw_text=f"{secret_marker}_{idx}", created_at=1000 + idx))
            assert store.flush(timeout=2.0) is True

            t_del = store.enqueue_delete_all()
            assert store.flush_ticket(t_del, timeout=5.0) is True
            assert store.list_sessions() == []
        finally:
            store.close(timeout=2.0)

        raw_db_bytes = db_file.read_bytes()
        wal_file = tmp_path / "history.db-wal"
        raw_wal_bytes = wal_file.read_bytes() if wal_file.exists() else b""
        assert secret_marker.encode("utf-8") not in raw_db_bytes
        assert secret_marker.encode("utf-8") not in raw_wal_bytes


class TestMeetingStoreShiftMarkdownHeader:
    def test_shift_meeting_start_updates_markdown_header_time_only(self, tmp_path: Path) -> None:
        history = HistoryStore(tmp_path / "history.db")
        meetings = MeetingStore(history)
        try:
            old_ts = int(time.mktime((2026, 5, 10, 9, 30, 0, 0, 0, -1)) * 1000)
            new_ts = int(time.mktime((2026, 5, 12, 14, 45, 0, 0, 0, -1)) * 1000)
            rec = meetings.create_meeting(title="季度复盘", started_at=old_ts)
            meetings.end_meeting(rec.id, ended_at=old_ts + 1_800_000)
            original_md = (
                "# 季度复盘\n\n"
                "- 时间：2026-05-10 09:30\n"
                "- 时长：00:30:00\n"
                "- 参会人：张三、李四\n\n"
                "## 待办\n"
                "- 时间：下周三前提交报告\n"
            )
            meetings.save_notes(
                rec.id,
                template_id="general",
                model="deepseek-chat",
                json_payload='{"summary":"ok"}',
                markdown=original_md,
            )
            meetings.save_user_markdown(rec.id, original_md.replace("季度复盘", "季度复盘（手改）"))
            assert meetings.flush() is True

            meetings.shift_meeting_start(rec.id, new_ts)
            assert meetings.flush() is True

            notes = meetings.get_notes(rec.id)
            assert notes is not None
            assert "- 时间：2026-05-12 14:45\n" in notes.markdown
            assert "- 时间：2026-05-10 09:30" not in notes.markdown
            # Body section bullet starting with `- 时间：` must NOT be altered!
            assert "- 时间：下周三前提交报告\n" in notes.markdown
            assert "- 时间：2026-05-12 14:45\n" in notes.previous_markdown
            assert "- 时间：2026-05-12 14:45\n" in notes.user_markdown
        finally:
            history.close(timeout=2.0)


class TestPolishHotwordsAndRewriteGuard:
    def test_polish_mode_includes_user_glossary_terms(self) -> None:
        glossary = [
            {"term": "SayInk", "variants": ["塞音", "声印"]},
            {"term": "FunASR-Nano"},
        ]
        worker = PolishWorker(
            "https://api.deepseek.com/v1",
            "sk-test",
            "deepseek-chat",
            "今天测试塞音和funasr",
            mode=LLM_MODE_POLISH,
            glossary=glossary,
        )
        msg = worker.user_message()
        assert "SayInk ← 塞音、声印" in msg
        assert "FunASR-Nano" in msg
        assert "<transcript>\n今天测试塞音和funasr\n</transcript>" in msg
        # Empty glossary keeps bare <transcript> wrapper
        assert polish_user_prompt("纯文本") == "<transcript>\n纯文本\n</transcript>"

    def test_strip_transcript_tags_unwraps_selection_and_transcript(self) -> None:
        assert strip_transcript_tags("<selection>\n改写后的段落\n</selection>") == "改写后的段落"
        assert strip_transcript_tags("<SELECTION>Hello world</SELECTION>") == "Hello world"
        assert strip_transcript_tags("<transcript><selection>双层包裹</selection></transcript>") == "双层包裹"

    @pytest.mark.parametrize(
        "refusal",
        [
            "抱歉，我无法处理这段涉及敏感信息的文本。",
            "对不起，我不能按照您的要求改写。",
            "作为AI语言模型，我无法执行该指令。",
            "作为 AI，我不能协助完成此请求。",
            "As an AI, I cannot rewrite this content.",
            "I cannot fulfill this request due to policy.",
        ],
    )
    def test_validate_rewrite_output_catches_meta_refusals(self, refusal: str) -> None:
        cleaned, err = validate_rewrite_output(refusal, "请把明天的产品周会推迟到下午三点。")
        assert err is not None
        assert "拒绝" in err
        assert cleaned == refusal

    def test_validate_rewrite_output_allows_legitimate_text_and_existing_phrases(self) -> None:
        cleaned, err = validate_rewrite_output(
            "<selection>\n烦请将明日产品周会顺延至下午三点。\n</selection>",
            "请把明天的产品周会推迟到下午三点。",
        )
        assert err is None
        assert cleaned == "烦请将明日产品周会顺延至下午三点。"

        # Phrase already present in original_selection must NOT be flagged as refusal
        cleaned2, err2 = validate_rewrite_output(
            "抱歉，我无法出席明天的评审会议。",
            "抱歉，我无法参加明天的评审会",
        )
        assert err2 is None
        assert cleaned2 == "抱歉，我无法出席明天的评审会议。"

        empty_clean, empty_err = validate_rewrite_output("<selection>   </selection>", "原文")
        assert empty_clean == ""
        assert empty_err is not None

    def test_text_polisher_rewrite_routes_refusal_to_rewrite_error(self) -> None:
        polisher = TextPolisher()
        fake_worker = MagicMock()
        polisher._rewrite_worker = fake_worker
        polisher._rewrite_selection = "原始客户邮件草稿"
        errors: list[str] = []
        oks: list[str] = []
        polisher.rewrite_error.connect(errors.append)
        polisher.rewrite_complete.connect(oks.append)

        # Simulate sender check via direct helper verification
        cleaned, err = validate_rewrite_output("抱歉，我无法改写该邮件。", polisher._rewrite_selection)
        assert err is not None and cleaned
        assert oks == []


class TestChunkedMinutesUserNotes:
    def test_merge_system_prompt_resolves_user_notes_contradiction(self) -> None:
        assert "<user_notes>" in MERGE_SYSTEM_PROMPT
        assert "（待核对）" in MERGE_SYSTEM_PROMPT

        ctx = MeetingContext(title="长会", notes_user="核对海外法务预算\n确认灰度节奏")
        merge_prompt = build_merge_prompt(ctx, [{"summary": "第一段"}])
        assert "<user_notes>\n核对海外法务预算\n确认灰度节奏\n</user_notes>" in merge_prompt
        assert "（待核对）" in merge_prompt

        excluded = build_user_prompt(ctx, "正文", include_user_notes=False)
        assert "核对海外法务预算" not in excluded
        assert "<user_notes>" not in excluded
        excluded_merge = build_merge_prompt(ctx, [{"summary": "第一段"}], include_user_notes=False)
        assert "核对海外法务预算" not in excluded_merge

    def test_chunked_long_meeting_preserves_user_notes_and_honors_exclusion(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # >8,000 chars across lines forces multi-chunk map-reduce
        lines = [
            TranscriptLine(i * 3000, 1 + (i % 2), "讨论架构升级与网关压测指标细节。" * 15)
            for i in range(45)
        ]
        total_chars = sum(len(line.text) for line in lines)
        assert total_chars > 8000

        sent_user_prompts: list[str] = []

        def _mock_transport(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            user_msg = body["messages"][1]["content"]
            sent_user_prompts.append(user_msg)
            if "中间纪要 1/" in user_msg:
                # Merge model omits one user note to verify deterministic preservation too
                reply = json.dumps(
                    {
                        "title": "网关长会",
                        "summary": "完成了网关压测指标评审。",
                        "topics": [{"title": "压测", "points": [{"text": "QPS 达标", "ts": "00:00:03"}]}],
                        "decisions": [],
                        "action_items": [],
                        "open_questions": [],
                        "chapters": [],
                        "speakers": [],
                    },
                    ensure_ascii=False,
                )
            else:
                reply = json.dumps({"summary": "分段压测讨论"}, ensure_ascii=False)
            sse = (
                "data: "
                + json.dumps({"choices": [{"delta": {"content": reply}, "finish_reason": "stop"}]}, ensure_ascii=False)
                + "\n\ndata: [DONE]\n\n"
            )
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse.encode("utf-8"))

        orig_client = httpx.AsyncClient
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **kw: orig_client(transport=httpx.MockTransport(_mock_transport), **kw),
        )

        ctx = MeetingContext(
            title="网关长会",
            notes_user="跟进跨区域灾备预算\n确认法务签署节点",
        )
        req_keep = MinutesRequest(
            api_url="http://localhost:11434/v1",
            api_key="",
            model_name="qwen2.5",
            template=get_template("general"),
            context=ctx,
            lines=lines,
            duration_ms=150_000,
            include_user_notes=True,
        )
        worker_keep = MinutesWorker(req_keep)
        res_keep: list[MinutesResult] = []
        err_keep: list[str] = []
        worker_keep.finished_ok.connect(res_keep.append)
        worker_keep.error.connect(err_keep.append)
        worker_keep.run()

        assert not err_keep and len(res_keep) == 1
        out_keep = res_keep[0]
        assert out_keep.chunked is True
        md_keep = out_keep.markdown
        assert "跟进跨区域灾备预算（待核对）" in md_keep
        assert "确认法务签署节点（待核对）" in md_keep
        assert any("<user_notes>" in p for p in sent_user_prompts)

        # Now run with include_user_notes=False: notes_user must NEVER appear in prompts or output!
        sent_user_prompts.clear()
        req_omit = MinutesRequest(
            api_url="http://localhost:11434/v1",
            api_key="",
            model_name="qwen2.5",
            template=get_template("general"),
            context=ctx,
            lines=lines,
            duration_ms=150_000,
            include_user_notes=False,
        )
        assert request_fingerprint(req_keep) != request_fingerprint(req_omit)
        worker_omit = MinutesWorker(req_omit)
        res_omit: list[MinutesResult] = []
        worker_omit.finished_ok.connect(res_omit.append)
        worker_omit.run()
        assert len(res_omit) == 1
        md_omit = res_omit[0].markdown
        assert "跟进跨区域灾备预算" not in md_omit
        assert "确认法务签署节点" not in md_omit
        assert all("跟进跨区域灾备预算" not in p for p in sent_user_prompts)
        assert all("<user_notes>" not in p for p in sent_user_prompts)

        # MinutesGenerator.generate(..., include_user_notes=False) also overrides cleanly
        gen = MinutesGenerator()
        captured_reqs: list[MinutesRequest] = []
        monkeypatch.setattr(
            "sayink.minutes_generator.MinutesWorker",
            lambda r: captured_reqs.append(r) or MagicMock(isRunning=lambda: False),
        )
        gen.generate(req_keep, include_user_notes=False)
        assert len(captured_reqs) == 1 and captured_reqs[0].include_user_notes is False


class TestSecretRedactionAndStartupHygiene:
    def test_setup_logging_clamps_httpx_under_debug_and_redacts_secrets(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = logging.getLogger()
        saved_handlers = root.handlers[:]
        saved_filters = root.filters[:]
        saved_level = root.level
        try:
            monkeypatch.setenv("SAYINK_LOG_LEVEL", "DEBUG")
            monkeypatch.delenv("SAYINK_LOG_HTTP_WIRE", raising=False)
            main_mod.setup_logging(str(tmp_path))

            assert root.level == logging.DEBUG
            assert logging.getLogger("httpx").level == logging.WARNING
            assert logging.getLogger("httpcore").level == logging.WARNING

            log = logging.getLogger("SayInk")
            log.debug(
                "Auth header Authorization: Bearer sk-live-super-secret-999 and "
                "url https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
                "?key=AIzaSySecretKey000&api-key=az-secret-111&token=tok-222&api_key=k-333"
            )
            for h in root.handlers:
                h.flush()

            log_text = (tmp_path / "sayink.log").read_text(encoding="utf-8")
            assert "sk-live-super-secret-999" not in log_text
            assert "AIzaSySecretKey000" not in log_text
            assert "az-secret-111" not in log_text
            assert "tok-222" not in log_text
            assert "k-333" not in log_text
            assert "Bearer [REDACTED]" in log_text
            assert "key=[REDACTED]" in log_text
            assert "api-key=[REDACTED]" in log_text

            # Opt-in wire logging un-clamps httpx/httpcore when SAYINK_LOG_HTTP_WIRE=1
            monkeypatch.setenv("SAYINK_LOG_HTTP_WIRE", "1")
            main_mod.setup_logging(str(tmp_path / "wire"))
            assert logging.getLogger("httpx").level == logging.NOTSET
            assert logging.getLogger("httpcore").level == logging.NOTSET
        finally:
            import faulthandler

            faulthandler.disable()
            for h in root.handlers:
                if h not in saved_handlers:
                    h.close()
            root.handlers[:] = saved_handlers
            root.filters[:] = saved_filters
            root.setLevel(saved_level)
            if main_mod._crash_file is not None:
                main_mod._crash_file.close()
                main_mod._crash_file = None

    def test_sweep_playback_temp_dir_removes_stale_wavs_unconditionally(self, tmp_path: Path) -> None:
        playback_dir = tmp_path / "sayink-playback"
        playback_dir.mkdir()
        wav1 = playback_dir / "meeting1-1234.wav"
        wav2 = playback_dir / "meeting2-5678.wav"
        keep_note = playback_dir / "keep.txt"
        wav1.write_bytes(b"RIFF....WAVE")
        wav2.write_bytes(b"RIFF....WAVE")
        keep_note.write_text("ok", encoding="utf-8")

        removed = main_mod.sweep_playback_temp_dir(tmp_path)
        assert removed == 2
        assert not wav1.exists()
        assert not wav2.exists()
        assert keep_note.exists()

    def test_endpoint_and_transport_redact_query_string_api_keys(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        raw_err = (
            "ConnectError: Failed for url 'https://my.openai.azure.com/openai/v1/chat/completions"
            "?api-version=2024-06-01&api-key=azure_secret_777&key=gemini_secret_888'"
        )
        redacted = redact_url_query_secrets(raw_err)
        assert "azure_secret_777" not in redacted
        assert "gemini_secret_888" not in redacted
        assert "api-version=2024-06-01" in redacted
        assert "api-key=[REDACTED]" in redacted
        assert "key=[REDACTED]" in redacted
        # Idempotent on repeated calls
        assert redact_url_query_secrets(redacted) == redacted

        def _failing_transport(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

        orig_sync = httpx.Client
        monkeypatch.setattr(
            httpx,
            "Client",
            lambda **kw: orig_sync(transport=httpx.MockTransport(_failing_transport), **kw),
        )
        ok, msg = probe_endpoint_connection(
            "https://api.example.com/v1?api_key=sk-query-secret-555",
            "sk-header-secret",
            "gpt-4o-mini",
            proxy_mode="direct",
        )
        assert ok is False
        assert "sk-query-secret-555" not in msg
        assert "api_key=[REDACTED]" in msg

        with httpx.Client() as sync_client:
            with pytest.raises(httpx.ConnectError) as sync_exc_info:
                chat_completion(
                    sync_client,
                    httpx,
                    api_url="https://api.example.com/v1?key=AIzaSecretSync999",
                    api_key="k",
                    model_name="m",
                    messages=[{"role": "user", "content": "hi"}],
                    temperature=0.0,
                    max_tokens=16,
                    retry_delay_sec=0.0,
                )
            assert "AIzaSecretSync999" not in str(sync_exc_info.value)
            assert "key=[REDACTED]" in str(sync_exc_info.value)

            with pytest.raises(httpx.ConnectError) as stream_exc_info:
                stream_chat_completion(
                    sync_client,
                    httpx,
                    api_url="https://api.example.com/v1?token=StreamSecretToken123",
                    api_key="k",
                    model_name="m",
                    messages=[{"role": "user", "content": "hi"}],
                    temperature=0.0,
                    max_tokens=16,
                    retry_delay_sec=0.0,
                )
            assert "StreamSecretToken123" not in str(stream_exc_info.value)
            assert "token=[REDACTED]" in str(stream_exc_info.value)

        async def _run_async_fail() -> str:
            async with httpx.AsyncClient(transport=httpx.MockTransport(_failing_transport)) as aclient:
                return await async_completion(
                    aclient,
                    httpx,
                    api_url="https://api.example.com/v1?api-key=AsyncQuerySecret321",
                    api_key="k",
                    model_name="m",
                    messages=[{"role": "user", "content": "hi"}],
                    max_tokens=16,
                    retry_delay_sec=0.0,
                )

        with pytest.raises(httpx.ConnectError) as async_exc_info:
            asyncio.run(_run_async_fail())
        assert "AsyncQuerySecret321" not in str(async_exc_info.value)
        assert "api-key=[REDACTED]" in str(async_exc_info.value)

    def test_glossary_conflict_warning_never_logs_plaintext_user_terms(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        sensitive_glossary = [
            {"term": "内部代号麒麟99", "variants": ["代"]},
            {"term": "机密客户王某某", "variants": ["机密并购案"]},
            {"term": "机密并购案"},
        ]
        with caplog.at_level(logging.WARNING, logger="SayInk"):
            pairs, conflicts = glossary_variant_map_with_conflicts(sensitive_glossary)

        assert pairs == []
        assert len(conflicts) == 2
        logged_messages = "\n".join(r.getMessage() for r in caplog.records)
        assert "词库冲突提示" in logged_messages
        assert "内部代号麒麟99" not in logged_messages
        assert "机密客户王某某" not in logged_messages
        assert "机密并购案" not in logged_messages

"""Minutes pipeline: prompts, chunking, JSON extraction, Markdown rendering
guardrails and the generator's single / map-reduce / fallback flows."""

from __future__ import annotations

import json

import httpx
import pytest

from sayink.history_store import SegmentRecord
from sayink.minutes_generator import MinutesRequest, MinutesWorker, _concat_partials
from sayink.minutes_prompts import (
    ALL_SECTIONS,
    BUILTIN_TEMPLATES,
    GENERAL_TEMPLATE_ID,
    MeetingContext,
    TranscriptLine,
    build_markdown_system_prompt,
    build_system_prompt,
    build_user_prompt,
    chunk_transcript,
    format_timestamp,
    get_template,
    list_templates,
    parse_timestamp,
    render_transcript,
    segment_offset_ms,
    transcript_lines,
)
from sayink.minutes_render import (
    EMPTY_SECTION_TEXT,
    UNVERIFIED_MARK,
    apply_speaker_names,
    extract_json,
    flag_unverified,
    render_markdown,
    replace_names_in_markdown,
)
from sayink.transcript_refiner import (
    REFINE_SYSTEM_PROMPT, RefinementValidationError, RefinementUnit,
    refinement_chunks, refinement_user_prompt, render_refinement, validate_refinement,
)


def _seg(seq, text, *, created_at, duration_ms=2000, captured_at=0, speaker_id=1, polished=""):
    return SegmentRecord(
        session_id="m", seq=seq, created_at=created_at, raw_text=text, polished_text=polished,
        duration_ms=duration_ms, trigger_mode="meeting", speaker_id=speaker_id, captured_at=captured_at,
    )


class TestTimestamps:
    def test_format_and_parse_round_trip(self):
        assert format_timestamp(0) == "00:00:00"
        assert format_timestamp(3_723_000) == "01:02:03"
        assert parse_timestamp("01:02:03") == 3_723_000
        assert parse_timestamp("1:02:03") == 3_723_000
        assert parse_timestamp("01:62:03") is None
        assert parse_timestamp("abc") is None
        assert parse_timestamp(None) is None

    def test_segment_offset_prefers_captured_at(self):
        assert segment_offset_ms(_seg(0, "a", created_at=10_000, captured_at=7_500), 5_000) == 2_500

    def test_segment_offset_falls_back_for_old_rows(self):
        # created_at is when recognition finished: back off by the clip length.
        assert segment_offset_ms(_seg(0, "a", created_at=10_000, duration_ms=2_000), 5_000) == 3_000
        assert segment_offset_ms(_seg(0, "a", created_at=4_000, duration_ms=2_000), 5_000) == 0


class TestTranscript:
    def test_lines_prefer_polished_and_skip_empty(self):
        segs = [
            _seg(0, "原始", created_at=1000, captured_at=500, polished="润色"),
            _seg(1, "   ", created_at=2000),
            _seg(2, "第二句", created_at=3000, captured_at=2500, speaker_id=2),
        ]
        lines = transcript_lines(segs, 0)
        assert [l.text for l in lines] == ["润色", "第二句"]
        text = render_transcript(lines, {2: "李四"})
        assert text == "[00:00:00] 说话人1：润色\n[00:00:02] 李四：第二句"
        raw = render_transcript(transcript_lines(segs, 0, prefer_polished=False))
        assert raw.startswith("[00:00:00] 说话人1：原始\n")

    def test_chunk_transcript_splits_at_lines(self):
        lines = [TranscriptLine(i * 1000, 1, "字" * 50) for i in range(10)]
        chunks = chunk_transcript(lines, None, max_chars=200)
        assert len(chunks) > 1
        assert all(len(c) <= 200 for c in chunks)
        assert "\n".join(chunks).count("\n") == 9
        pieces = chunk_transcript([TranscriptLine(0, 1, "x" * 500)], None, max_chars=100)
        assert len(pieces) > 1 and all(len(piece) <= 100 for piece in pieces)
        assert "".join(piece.split("：", 1)[1] for piece in pieces) == "x" * 500


class TestTemplatesAndPrompts:
    def test_builtin_templates(self):
        ids = [t.id for t in BUILTIN_TEMPLATES]
        assert ids[0] == GENERAL_TEMPLATE_ID
        assert len(ids) == 6
        assert get_template("nonexistent").id == GENERAL_TEMPLATE_ID
        assert get_template("standup").sections[0] == "summary"
        assert get_template(GENERAL_TEMPLATE_ID).sections == ALL_SECTIONS

    def test_custom_template(self):
        custom = [{"id": "mine", "name": "我的", "focus": "只要待办", "sections": ["action_items", "bogus"]}]
        t = get_template("mine", custom)
        assert t.name == "我的" and not t.builtin
        assert t.sections == ("action_items",)
        assert [t.id for t in list_templates(custom)][-1] == "mine"

    def test_system_prompt_mentions_rules_schema_and_focus(self):
        t = get_template(GENERAL_TEMPLATE_ID)
        prompt = build_system_prompt(t, output_language="zh")
        assert "不编造" in prompt
        assert '"action_items"' in prompt
        assert t.focus in prompt
        assert "用中文撰写" in prompt
        assert "write the minutes in English" in build_system_prompt(t, output_language="en")
        md = build_markdown_system_prompt(t)
        assert "## 待办" in md and "- [ ]" in md

    def test_user_prompt_includes_marks_notes_and_glossary(self):
        ctx = MeetingContext(
            title="周会", participants="张三, 李四", agenda="进度", notes_user="记得问预算",
            marks=[(65_000, "重点"), (120_000, "")], glossary_terms=["SayInk"],
        )
        prompt = build_user_prompt(ctx, "[00:00:01] 说话人1：你好", chunk_label="第 1/2 段")
        assert "标题：周会" in prompt
        assert "[00:01:05] 标记：重点" in prompt
        assert "[00:02:00] 标记" in prompt
        assert "记得问预算" in prompt
        assert "SayInk" in prompt
        assert "文字稿（第 1/2 段）：" in prompt
        assert prompt.endswith("[00:00:01] 说话人1：你好")
        assert "（无）" in build_user_prompt(MeetingContext(), "x")


class TestExtractJson:
    def test_plain_fenced_and_embedded(self):
        assert extract_json('{"a": 1}') == {"a": 1}
        assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
        assert extract_json('好的，这是结果：{"a": {"b": "}"}} 谢谢') == {"a": {"b": "}"}}
        assert extract_json("[1, 2]") is None
        assert extract_json("没有 JSON") is None
        assert extract_json("") is None
        assert extract_json("{broken") is None

    def test_absurdly_nested_brackets_are_treated_as_not_json(self):
        # json.loads raises RecursionError (not ValueError) here; a broken
        # endpoint must not take the minutes worker down.
        nested = '{"a":' + "[" * 5000 + "]" * 5000 + "}"
        assert extract_json(nested) is None
        assert extract_json("前言 " + nested + " 后记") is None


class TestValidateMinutes:
    def test_nulls_where_the_prompt_asked_for_empty_values_are_normalised(self):
        from sayink.minutes_validation import validate_minutes

        payload = {
            "title": None,
            "summary": "结论先行。",
            "decisions": [{"text": None}, {"text": "采用方案 B", "owner": None}],
            "action_items": None,
            "speakers": [{"label": "说话人1", "summary": ""}],
            "topics": [{"title": None, "points": None}, {"title": "进度", "points": [{"text": None}, {"text": "按期"}]}],
        }
        out = validate_minutes(payload)
        assert out["title"] == ""
        assert out["decisions"] == [{"text": "采用方案 B", "owner": ""}]
        assert out["action_items"] == []
        # A speaker without a summary is still a speaker.
        assert out["speakers"] == [{"label": "说话人1", "summary": ""}]
        assert out["topics"] == [{"title": "进度", "points": [{"text": "按期"}]}]

    def test_wrong_types_and_empty_documents_still_reject(self):
        from sayink.minutes_validation import validate_minutes
        from sayink.text_polisher import LLMResponseError

        with pytest.raises(LLMResponseError):
            validate_minutes({"summary": ["列表"]})
        with pytest.raises(LLMResponseError):
            validate_minutes({"decisions": "不是列表"})
        with pytest.raises(LLMResponseError):
            validate_minutes({"decisions": [{"text": 12}]})
        with pytest.raises(LLMResponseError, match="为空"):
            validate_minutes({"title": "x", "decisions": [{"text": None}]})


class TestSplitChunk:
    def test_mid_line_cut_keeps_the_prefix_of_the_line_being_cut(self):
        from sayink.minutes_generator import _split_chunk

        text = "[00:00:01] 说话人1：短句。\n[00:00:02] 说话人2：" + "甲" * 60 + "乙" * 60
        left, right = _split_chunk(text)
        assert left + right.replace("[00:00:02] 说话人2：", "", 1) == text
        assert right.startswith("[00:00:02] 说话人2：甲")
        assert "说话人1" not in right

    def test_newline_split_is_preferred(self):
        from sayink.minutes_generator import _split_chunk

        text = "\n".join(f"[00:00:0{i}] 说话人{i}：内容{i}" for i in range(1, 7))
        left, right = _split_chunk(text)
        assert left.endswith("内容2") and right.startswith("[00:00:03] 说话人3")
        assert left + "\n" + right == text


class TestRender:
    def _payload(self):
        return {
            "title": "产品周会",
            "summary": "讨论了上线窗口。",
            "topics": [{"title": "上线", "points": [{"text": "说话人2 建议周三上线", "ts": "00:05:00"}]}],
            "decisions": [{"text": "周三上线", "ts": "00:06:00"}, {"text": "预算加 20万", "ts": "09:00:00"}],
            "action_items": [
                {"task": "写发布说明", "owner": "说话人2", "due": "周三", "ts": "00:07:00"},
                {"task": "", "owner": "x"},
                {"task": "订会议室", "owner": "", "due": "", "ts": "bad"},
            ],
            "open_questions": [{"text": "灰度比例未定", "ts": "00:08:00"}],
            "chapters": [{"start": "00:00:00", "end": "00:10:00", "title": "开场", "summary": "寒暄"}],
            "speakers": [
                {"label": "说话人1", "summary": "主持", "suggested_name": "张三", "evidence_ts": "00:00:10"},
                {"label": "说话人2", "summary": "汇报", "suggested_name": None},
            ],
        }

    def test_full_render_with_names_and_guardrails(self):
        transcript = "[00:05:00] 说话人2：建议周三上线\n[00:06:00] 说话人1：那就周三"
        md = render_markdown(
            self._payload(),
            template=get_template(GENERAL_TEMPLATE_ID),
            duration_ms=600_000,
            transcript_text=transcript,
            speaker_names={2: "李四"},
            meta_lines=["时间：2026-10-17 10:00", "时长：00:10:00"],
        )
        assert md.startswith("# 产品周会\n\n- 时间：2026-10-17 10:00\n- 时长：00:10:00\n")
        assert "## 摘要\n讨论了上线窗口。" in md
        assert "### 上线\n- 李四 建议周三上线 [00:05:00]" in md
        # Decision timestamp beyond meeting length dropped; 20万 not in transcript → 待核对.
        assert "- 周三上线 [00:06:00]" in md
        assert f"- 预算加 20万{UNVERIFIED_MARK}\n" in md
        assert "- [ ] 写发布说明 — @李四 · 周三 [00:07:00]" in md
        assert "- [ ] 订会议室 — @未指定 · 未提及\n" in md
        assert "- 灰度比例未定 [00:08:00]" in md
        assert "- [00:00:00] – [00:10:00] **开场**：寒暄" in md
        assert "- **说话人1**（可能是 张三 [00:00:10]）：主持" in md
        assert "- **李四**：汇报" in md
        assert md.endswith("\n")

    def test_missing_sections_render_placeholder_and_template_limits_sections(self):
        md = render_markdown({}, template=get_template(GENERAL_TEMPLATE_ID), title_fallback="备用标题")
        assert md.startswith("# 备用标题\n")
        assert md.count(EMPTY_SECTION_TEXT) == len(ALL_SECTIONS)
        md = render_markdown(None, template=get_template("standup"))
        assert "## 决策" not in md and "## 发言人摘要" in md

    def test_flag_unverified(self):
        assert flag_unverified("3月1日上线", "我们 3 月 1 日 上线") == "3月1日上线"
        assert flag_unverified("3月1日上线", "周三上线") == "3月1日上线" + UNVERIFIED_MARK
        assert flag_unverified("周三上线", "") == "周三上线"
        assert flag_unverified("没有数字", "任何") == "没有数字"

    def test_apply_speaker_names_and_replace(self):
        assert apply_speaker_names("说话人1 和 说话人 2 讨论", {1: "张三"}) == "张三 和 说话人 2 讨论"
        assert apply_speaker_names("x", None) == "x"
        text, n = replace_names_in_markdown("说话人1 说，说话人1 做", "说话人1", "张三")
        assert text == "张三 说，张三 做" and n == 2
        assert replace_names_in_markdown("a", "a", "a") == ("a", 0)

    def test_concat_partials(self):
        merged = _concat_partials([
            {"title": "T", "summary": "A", "decisions": [{"text": "d1"}]},
            {"summary": "B", "decisions": [{"text": "d2"}], "speakers": "bad"},
        ])
        assert merged["title"] == "T"
        assert merged["summary"] == "A\n\nB"
        assert [d["text"] for d in merged["decisions"]] == ["d1", "d2"]
        assert merged["speakers"] == []


# ── generator with real HTTPX and an offline transport ─────────────────────


class _Timeout(httpx.ReadTimeout):
    pass


@pytest.fixture
def fake_httpx(monkeypatch):
    calls: list[dict] = []
    script: list = []

    def respond(request):
        payload = json.loads(request.content)
        calls.append({"url": str(request.url), "headers": dict(request.headers), "payload": payload})
        # Preserve the existing assertions' canonical header spelling.
        if "authorization" in request.headers:
            calls[-1]["headers"]["Authorization"] = request.headers["authorization"]
        item = fake.responder(payload) if fake.responder else script.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, httpx.Response):
            return item
        status, content, finish = 200, item, "stop"
        if isinstance(item, tuple):
            status, content = item[:2]
            finish = item[2] if len(item) > 2 else "stop"
        if status >= 400:
            return httpx.Response(status, json={"error": {"message": content}})
        events = [
            {"choices": [{"delta": {"content": content}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": finish}]},
        ]
        body = "".join("data: " + json.dumps(event, ensure_ascii=False) + "\n\n" for event in events) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    monkeypatch.setattr("sayink.minutes_generator._RETRY_DELAY_SEC", 0.0)
    from types import SimpleNamespace
    fake = SimpleNamespace(calls=calls, script=script, responder=None)
    return fake


def _request(lines=None, **overrides):
    lines = lines if lines is not None else [TranscriptLine(1000, 1, "大家好"), TranscriptLine(5000, 2, "开始吧")]
    kwargs = dict(
        api_url="https://api.example.com/v1",
        api_key="k",
        model_name="gpt-x",
        template=get_template(GENERAL_TEMPLATE_ID),
        context=MeetingContext(title="周会"),
        lines=lines,
        duration_ms=60_000,
        title_fallback="周会",
    )
    kwargs.update(overrides)
    return MinutesRequest(**kwargs)


def _run(worker):
    results, errors, progress = [], [], []
    worker.finished_ok.connect(results.append)
    worker.error.connect(errors.append)
    worker.progress.connect(progress.append)
    worker.run()
    return results, errors, progress


class TestMinutesWorker:
    def test_known_names_are_recorded_as_owned_metadata(self, fake_httpx):
        fake_httpx.script.append(json.dumps({"summary": "张三负责发布。", "_speaker_names": {"9": "猜测姓名"}}))
        results, errors, _ = _run(MinutesWorker(_request(speaker_names={1: "张三"})))
        assert errors == []
        payload = json.loads(results[0].json_payload)
        assert payload["_speaker_names"] == {"1": "张三"}
        assert "张三负责发布" in results[0].markdown

    def test_markdown_fallback_uses_the_same_known_names(self, fake_httpx):
        fake_httpx.script.extend(["not JSON", "# 周会\n\n## 摘要\n说话人1负责发布，稍后验证。"])
        results, errors, _ = _run(MinutesWorker(_request(speaker_names={1: "张三"})))
        assert errors == [] and not results[0].json_payload
        assert "张三负责发布" in results[0].markdown
        assert "说话人1" not in results[0].markdown

    def test_single_request_json(self, fake_httpx):
        fake_httpx.script.append(json.dumps({"title": "周会", "summary": "一切顺利", "decisions": [{"text": "继续", "ts": "00:00:05"}]}))
        results, errors, _ = _run(MinutesWorker(_request()))
        assert errors == []
        res = results[0]
        assert res.chunked is False
        assert json.loads(res.json_payload)["summary"] == "一切顺利"
        assert "## 摘要\n一切顺利" in res.markdown
        assert "- 继续 [00:00:05]" in res.markdown
        assert res.model == "gpt-x" and res.template_id == GENERAL_TEMPLATE_ID
        call = fake_httpx.calls[0]
        assert call["url"] == "https://api.example.com/v1/chat/completions"
        assert call["headers"]["Authorization"] == "Bearer k"
        assert call["payload"]["messages"][1]["content"].endswith("[00:00:05] 说话人2：开始吧")

    def test_non_json_falls_back_to_markdown_prompt(self, fake_httpx):
        fake_httpx.script.extend(["这不是 JSON", "# 周会\n\n## 摘要\n顺利"])
        results, errors, progress = _run(MinutesWorker(_request()))
        assert errors == []
        assert results[0].json_payload == ""
        assert results[0].markdown == "# 周会\n\n## 摘要\n顺利\n"
        assert any("Markdown" in p for p in progress)
        assert "## 摘要" in fake_httpx.calls[1]["payload"]["messages"][0]["content"]

    @staticmethod
    def _long_lines():
        lines = [TranscriptLine(i * 1000, 1 + i % 2, "内容" * 30) for i in range(40)]
        n_chunks = len(chunk_transcript(lines, {}, max_chars=1000))
        assert n_chunks >= 3
        return lines, n_chunks

    def test_map_reduce_for_long_transcripts(self, fake_httpx):
        lines, n = self._long_lines()
        part = {"summary": "部分", "decisions": [{"text": "d", "ts": "00:00:01"}]}
        fake_httpx.script.extend([json.dumps(part)] * n)
        fake_httpx.script.append(json.dumps({"title": "合并", "summary": "总", "decisions": [{"text": "d", "ts": "00:00:01"}]}))
        results, errors, progress = _run(MinutesWorker(_request(lines, chunk_chars=1000)))
        assert errors == []
        res = results[0]
        assert res.chunked is True
        assert "# 合并" in res.markdown
        assert len(fake_httpx.calls) == n + 1
        assert f"中间纪要 1/{n}" in fake_httpx.calls[-1]["payload"]["messages"][1]["content"]
        assert f"第 1/{n} 段" in fake_httpx.calls[0]["payload"]["messages"][1]["content"]
        assert any("合并" in p for p in progress)

    def test_merge_without_json_concatenates_partials(self, fake_httpx):
        lines, n = self._long_lines()
        fake_httpx.script.extend([json.dumps({"summary": chr(ord("A") + i)}) for i in range(n)])
        fake_httpx.script.append("不是 JSON")
        results, errors, _ = _run(MinutesWorker(_request(lines, chunk_chars=1000)))
        assert errors == []
        assert "A\n\nB\n\nC" in results[0].markdown

    def test_chunk_returning_non_json_is_an_error(self, fake_httpx):
        lines = [TranscriptLine(i * 1000, 1, "内容" * 30) for i in range(40)]
        fake_httpx.script.extend([json.dumps({"summary": "A"}), "坏掉了"])
        results, errors, _ = _run(MinutesWorker(_request(lines, chunk_chars=1000)))
        assert results == [] and "第 2/" in errors[0]

    def test_non_http_url_rejected_without_network(self, fake_httpx):
        results, errors, _ = _run(MinutesWorker(_request(api_url="ftp://api.example.com/v1")))
        assert results == [] and "http://" in errors[0]
        assert fake_httpx.calls == []

    def test_empty_transcript(self, fake_httpx):
        results, errors, _ = _run(MinutesWorker(_request(lines=[])))
        assert results == [] and "文字稿为空" in errors[0]

    def test_timeout_and_http_errors(self, fake_httpx):
        fake_httpx.script.extend([_Timeout("timeout"), _Timeout("timeout")])
        _, errors, _ = _run(MinutesWorker(_request()))
        assert "超时" in errors[0]
        fake_httpx.script.append((401, ""))
        _, errors, _ = _run(MinutesWorker(_request()))
        assert "HTTP 401" in errors[0]

    def test_gateway_error_is_retried_once(self, fake_httpx):
        fake_httpx.script.extend([(503, ""), json.dumps({"summary": "ok"})])
        results, errors, _ = _run(MinutesWorker(_request()))
        assert errors == [] and len(fake_httpx.calls) == 2
        assert "ok" in results[0].markdown

    @pytest.mark.parametrize("template", BUILTIN_TEMPLATES, ids=lambda template: template.id)
    def test_recovered_retry_status_is_cleared_before_preview(self, fake_httpx, template):
        lines = [TranscriptLine(0, 1, "本周已经确认完成验收。")]
        body = (_refined(refinement_chunks(lines)[0]) if template.is_refinement
                else json.dumps({"summary": "本周已经确认完成验收。"}))
        fake_httpx.script.extend([_Timeout("timeout"), body])
        worker = MinutesWorker(_request(lines, template=template))
        latest, preview_statuses = [""], []
        worker.progress.connect(lambda text: latest.__setitem__(0, text))
        worker.preview.connect(lambda text: preview_statuses.append(latest[0]) if text else None)

        results, errors, progress = _run(worker)

        assert not errors and len(results) == 1 and len(fake_httpx.calls) == 2
        assert any("重试" in text for text in progress)
        assert preview_statuses and all("重试" not in text for text in preview_statuses)
        assert "重试" not in progress[-1]

    def test_cancel_suppresses_output(self, fake_httpx):
        fake_httpx.script.append(json.dumps({"summary": "ok"}))
        worker = MinutesWorker(_request())
        worker.cancel()
        results, errors, _ = _run(worker)
        assert results == [] and errors == []


def _refined(units, text=None):
    if text is not None:
        paragraphs = [{"source_ids": [u.source_id for u in units], "text": text}]
    else:
        paragraphs = [{"source_ids": [u.source_id], "text": u.text} for u in units]
    return json.dumps({"paragraphs": paragraphs}, ensure_ascii=False)


class TestRefinement:
    def test_template_uses_dedicated_prompt_and_keeps_original_language(self):
        from sayink.minutes_prompts import describe_template

        template = get_template("refine")
        assert template.is_refinement and template.sections == ()
        assert "原文" in template.name
        assert "生成章节" not in describe_template(template)
        assert build_system_prompt(template, output_language="en") == REFINE_SYSTEM_PROMPT
        assert "不额外翻译" in REFINE_SYSTEM_PROMPT
        assert '"action_items"' not in REFINE_SYSTEM_PROMPT

    def test_single_request_renders_only_prose(self, fake_httpx):
        lines = [TranscriptLine(0, 1, "嗯，上个月回款83万元。"), TranscriptLine(1000, 2, "我不满意，可能还要核对 API。")]
        units = refinement_chunks(lines)[0]
        body = "上个月回款83万元。我不满意，可能还要核对 API。"
        fake_httpx.script.append(_refined(units, body))
        results, errors, _ = _run(MinutesWorker(_request(lines, template=get_template("refine"), output_language="en")))
        assert errors == []
        assert results[0].markdown == body + "\n"
        assert results[0].template_id == "refine" and not results[0].chunked
        assert json.loads(results[0].json_payload)["kind"] == "refine"
        assert len(fake_httpx.calls) == 1
        prompt = fake_httpx.calls[0]["payload"]["messages"][1]["content"]
        assert "用户标记与笔记" not in prompt

    def test_multiple_speakers_form_written_paragraphs_with_personal_voice(self, fake_httpx):
        lines = [
            TranscriptLine(0, 1, "嗯，张强这个月做得真不行，回款才38万。"),
            TranscriptLine(1000, 2, "上个月是83万。这个月少了两家客户，合同审批还卡着。"),
            TranscriptLine(2000, 1, "所以我现在真不想再扩招。如果审批下周还没过，就先别加人。"),
            TranscriptLine(3000, 1, "再说李芳，她这次把3个老客户都稳住了，这一点我挺认可。"),
        ]
        paragraphs = [
            {"source_ids": [1, 2], "text": "张强这个月做得真不行，回款才38万。上个月是83万，这个月少了两家客户，合同审批还卡着。"},
            {"source_ids": [3], "text": "所以我现在真不想再扩招。如果审批下周还没过，就先别加人。"},
            {"source_ids": [4], "text": "再说李芳，她这次把3个老客户都稳住了，这一点我挺认可。"},
        ]
        fake_httpx.script.append(json.dumps({"paragraphs": paragraphs}, ensure_ascii=False))
        results, errors, _ = _run(MinutesWorker(_request(lines, template=get_template("refine"))))
        assert errors == [] and len(fake_httpx.calls) == 1
        body = results[0].markdown
        assert body == "\n\n".join(p["text"] for p in paragraphs) + "\n"
        assert body.count("\n\n") == 2
        assert "真不想再扩招" in body and "我挺认可" in body
        assert "说话人" not in body and "一方认为" not in body
        assert json.loads(results[0].json_payload)["paragraphs"][0]["source_ids"] == [1, 2]

    def test_one_source_can_cover_multiple_topics_without_speaker_labels(self, fake_httpx):
        lines = [TranscriptLine(0, 1, "张强回款38万，我不满意。再说李芳，她稳住了3个老客户，这点我挺认可。")]
        body = "张强回款38万，我不满意。\n\n再说李芳，她稳住了3个老客户，这点我挺认可。"
        fake_httpx.script.append(_refined(refinement_chunks(lines)[0], body))
        results, errors, _ = _run(MinutesWorker(_request(lines, template=get_template("refine"))))
        assert errors == [] and results[0].markdown == body + "\n"

    def test_narrative_retains_disagreement_without_dialogue_script(self, fake_httpx):
        lines = [TranscriptLine(0, 1, "直接上线，我坚决不同意。"), TranscriptLine(1000, 2, "我倒觉得值得试试，但只做小规模验证。")]
        body = "直接上线，我坚决不同意。也有不同意见：“我倒觉得值得试试，但只做小规模验证。”"
        fake_httpx.script.append(_refined(refinement_chunks(lines)[0], body))
        results, errors, _ = _run(MinutesWorker(_request(lines, template=get_template("refine"))))
        assert errors == [] and results[0].markdown == body + "\n"

    @pytest.mark.parametrize("source,edited", [
        ("我不支持直接上线，先做验证。", "我不支持直接上线，不过先做验证。"),
        ("不但要修登录，还要修导出。", "除了修登录，还要修导出。"),
        ("两组的看法有差异，我不同意直接上线。", "两组有不同意见，我不同意直接上线。"),
    ])
    def test_written_transitions_do_not_trigger_negation_guard(self, source, edited):
        paragraphs = validate_refinement(
            {"paragraphs": [{"source_ids": [1], "text": edited}]}, [RefinementUnit(1, 1, source)]
        )
        assert render_refinement({"paragraphs": paragraphs}) == edited + "\n"

    def test_long_transcript_has_no_summary_merge_or_duplicated_context(self, fake_httpx):
        lines = [TranscriptLine(i * 1000, 1 + i % 2, f"第{i}项。" + "保留这个讨论的具体细节。" * 5) for i in range(60)]
        chunks = refinement_chunks(lines, max_chars=1000)
        fake_httpx.script.extend(_refined(chunk) for chunk in chunks)
        results, errors, progress = _run(MinutesWorker(_request(lines, template=get_template("refine"), chunk_chars=1000)))
        assert errors == [] and results[0].chunked
        assert len(fake_httpx.calls) == len(chunks)
        assert results[0].markdown == "\n\n".join(line.text for line in lines) + "\n"
        assert all("合并" not in p for p in progress)
        middle = json.loads(fake_httpx.calls[1]["payload"]["messages"][1]["content"].split("\n", 1)[1])
        assert middle["before_context"] and middle["after_context"]
        assert "source_id" not in middle["before_context"][0]

    def test_one_overlong_line_is_split_without_losing_characters(self):
        original = ("这段没有标点的发言必须完整保留" * 250) + "最后一句。"
        chunks = refinement_chunks([TranscriptLine(0, 2, original)])
        assert len(chunks) > 1
        assert all(sum(len(u.text) for u in chunk) <= 1600 for chunk in chunks)
        assert "".join(u.text for chunk in chunks for u in chunk) == original
        assert [u.source_id for chunk in chunks for u in chunk] == list(range(1, sum(map(len, chunks)) + 1))

    def test_hard_boundary_keeps_an_english_name_intact(self):
        original = "讨论" * 249 + "SayInk 123456 的完整行为。"
        chunks = refinement_chunks([TranscriptLine(0, 1, original)])
        units = chunks[0]
        assert units[0].text == "讨论" * 249
        assert units[1].text.startswith("SayInk 123456")
        validate_refinement({"paragraphs": [{"source_ids": [u.source_id for u in units], "text": original}]}, units)

    @pytest.mark.parametrize("ids", [[1], [1, 1, 2], [2, 1], [True, 2], [1, 3]])
    def test_missing_repeated_reordered_or_invalid_sources_are_rejected(self, ids):
        units = [RefinementUnit(1, 1, "先讨论预算。"), RefinementUnit(2, 2, "再讨论进度。")]
        with pytest.raises(RefinementValidationError):
            validate_refinement({"paragraphs": [{"source_ids": ids, "text": "先讨论预算，再讨论进度。"}]}, units)

    @pytest.mark.parametrize("source,edited,reason", [
        ("回款83万元。", "回款38万元。", "数字"),
        ("回款83万元。", "回款83亿元。", "单位"),
        ("甲组83万元，乙组83万元。", "甲组和乙组合计83万元。", "数字"),
        ("增长-10%。", "增长10%。", "数字"),
        ("不同意，现在没准备好。", "不同意，现在准备好了。", "否定"),
        ("现在准备好了。", "现在没准备好。", "否定"),
        ("如果下周通过，可能采用 API。", "下周通过，采用 API。", "条件"),
        ("采用 API。", "可能采用 API。", "条件"),
        ("采用 SayInk API。", "采用 SayInk。", "英文"),
        ("讨论预算。", "## 讨论预算。", "标题"),
        ("讨论预算。", "说话人1：讨论预算。", "对话"),
        ("讨论预算。", "", "清空"),
        ("讨论具体细节和原因。" * 20, "讨论细节。", "压缩"),
    ])
    def test_obvious_fidelity_failures_are_rejected(self, source, edited, reason):
        with pytest.raises(RefinementValidationError, match=reason):
            validate_refinement({"paragraphs": [{"source_ids": [1], "text": edited}]}, [RefinementUnit(1, 1, source)])

    def test_greetings_can_be_removed_but_not_business_details(self):
        units = [RefinementUnit(1, 1, "嗯，大家好。"), RefinementUnit(2, 2, "今天讨论预算。")]
        payload = {"paragraphs": [{"source_ids": [1], "text": ""}, {"source_ids": [2], "text": "今天讨论预算。"}]}
        assert render_refinement({"paragraphs": validate_refinement(payload, units)}) == "今天讨论预算。\n"
        with pytest.raises(RefinementValidationError):
            validate_refinement({"paragraphs": [{"source_ids": [1], "text": ""}]}, [RefinementUnit(1, 1, "你好，周五交付。")])

    def test_retry_keeps_original_sources_and_discards_bad_attempt(self, fake_httpx):
        lines = [TranscriptLine(0, 1, "本月83万元，下月可能调整。")]
        units = refinement_chunks(lines)[0]
        fake_httpx.script.extend([_refined(units, "本月38万元，下月调整。"), _refined(units)])
        results, errors, progress = _run(MinutesWorker(_request(lines, template=get_template("refine"))))
        assert errors == [] and results[0].markdown == lines[0].text + "\n"
        assert len(fake_httpx.calls) == 2 and any("重新核对" in p for p in progress)
        retry_prompt = fake_httpx.calls[1]["payload"]["messages"][1]["content"]
        assert "上次结果未通过检查" in retry_prompt and "本月83万元" in retry_prompt

    def test_truncated_reply_is_retried_even_if_it_is_valid_json(self, fake_httpx):
        lines = [TranscriptLine(0, 1, "不能改变原意。")]
        raw = _refined(refinement_chunks(lines)[0])
        fake_httpx.script.extend([(200, raw, "length"), raw])
        results, errors, _ = _run(MinutesWorker(_request(lines, template=get_template("refine"))))
        assert errors == [] and results
        assert len(fake_httpx.calls) == 2

    def test_failed_later_chunk_does_not_publish_partial_document(self, fake_httpx):
        lines = [TranscriptLine(0, 1, "第一段具体内容。" * 60), TranscriptLine(1, 1, "第二段具体内容。" * 60)]
        chunks = refinement_chunks(lines, max_chars=600)
        fake_httpx.script.extend([_refined(chunks[0])] + ['{"paragraphs": []}'] * 25)
        results, errors, _ = _run(MinutesWorker(_request(lines, template=get_template("refine"), chunk_chars=600)))
        assert results == [] and "本次结果未保存" in errors[0] and "第 2 段" in errors[0]

    def test_context_and_user_notes_are_not_treated_as_content(self):
        chunks = refinement_chunks([TranscriptLine(0, 1, "前文。" * 100), TranscriptLine(1, 2, "当前正文。" * 100)], max_chars=300)
        prompt = refinement_user_prompt(chunks, 1, {2: "手动姓名"}, ["SayInk"])
        data = json.loads(prompt.split("\n", 1)[1])
        assert data["glossary"] == ["SayInk"]
        assert set(data) == {"glossary", "before_context", "units", "after_context"}


def test_minutes_generator_retains_cancelled_worker_until_finished(monkeypatch):
    import threading
    from PyQt6.QtCore import QCoreApplication
    from sayink.minutes_generator import MinutesGenerator

    release = threading.Event()
    monkeypatch.setattr(MinutesWorker, "run", lambda self: release.wait(2.0))

    gen = MinutesGenerator()
    gen.generate(_request([TranscriptLine(0, 1, "讨论预算。")]))
    worker = gen._worker
    assert worker is not None

    gen.cancel()
    assert gen._worker is None
    assert worker in gen._workers

    release.set()
    assert worker.wait(2000)
    QCoreApplication.processEvents()
    assert worker not in gen._workers


def test_failed_job_resumes_validated_chunks(fake_httpx):
    lines, count = TestMinutesWorker._long_lines()
    checkpoints = {}
    save = lambda key, raw: checkpoints.update({key: raw})
    fake_httpx.script.extend([json.dumps({"summary": "first"}), (401, "invalid key")])
    _, errors, _ = _run(MinutesWorker(_request(lines, chunk_chars=1000, save_checkpoint=save)))
    assert errors and len(checkpoints) == 1
    initial_calls = len(fake_httpx.calls)
    fake_httpx.script.extend([json.dumps({"summary": "other"})] * (count - 1))
    fake_httpx.script.append(json.dumps({"summary": "merged"}))
    results, errors, progress = _run(MinutesWorker(_request(
        lines, chunk_chars=1000, checkpoints=dict(checkpoints), save_checkpoint=save,
    )))
    assert results and not errors
    assert len(fake_httpx.calls) - initial_calls == count  # Remaining chunks + merge.
    assert any("复用" in text for text in progress)


def test_fingerprint_covers_source_provider_model_template_and_language():
    from dataclasses import replace
    from sayink.minutes_generator import request_fingerprint
    request = _request()
    baseline = request_fingerprint(request)
    for key, value in dict(api_url="https://other.test", api_key="other", model_name="other", output_language="en",
                           template=get_template("standup"), context=MeetingContext(notes_user="changed"),
                           lines=[TranscriptLine(0, 1, "different")], max_output_tokens=2048).items():
        assert request_fingerprint(replace(request, **{key: value})) != baseline
    assert request_fingerprint(replace(request, checkpoints={"ignored": "ignored"})) == baseline


def test_empty_and_malformed_minutes_never_publish(fake_httpx):
    for payload in ({}, {"title": "only title"}, {"summary": 12}, {"summary": "x", "decisions": "wrong"}):
        fake_httpx.script.append(json.dumps(payload))
        results, errors, _ = _run(MinutesWorker(_request()))
        assert not results and errors


def test_truncated_summary_is_split_instead_of_being_saved(fake_httpx):
    text = "继续讨论预算。" * 50
    fake_httpx.script.extend([
        (200, json.dumps({"summary": "must not save"}), "length"),
        json.dumps({"summary": "left"}), json.dumps({"summary": "right"}), json.dumps({"summary": "whole"}),
    ])
    results, errors, _ = _run(MinutesWorker(_request([TranscriptLine(0, 1, text)])))
    assert not errors and results[0].chunked
    assert "must not save" not in results[0].markdown and len(fake_httpx.calls) == 4
    for call in fake_httpx.calls[1:3]:
        assert "[00:00:00] 说话人1：" in call["payload"]["messages"][1]["content"]


def test_input_plan_counts_context_and_leaves_output_room(fake_httpx):
    from sayink.minutes_generator import estimate_prompt_tokens
    request = _request([TranscriptLine(i * 1000, 1, "中文内容" * 100) for i in range(20)],
                       context_window_tokens=14000, chunk_chars=20000)
    worker = MinutesWorker(request)
    system = build_system_prompt(request.template)
    chunks = worker._plan_chunks(system)
    assert len(chunks) > 1
    assert all(estimate_prompt_tokens(system, worker._chunk_prompt(chunks, i)) <= 14000 - 4096 - 1024
               for i in range(len(chunks)))
    request.context.notes_user = "备注" * 20000
    results, errors, _ = _run(MinutesWorker(request))
    assert not results and errors and not fake_httpx.calls


def test_merge_inherits_language_template_notes_and_preserves_tasks(fake_httpx):
    from sayink.minutes_generator import _preserve_facts
    from sayink.minutes_prompts import build_merge_prompt, build_merge_system_prompt
    template = get_template("standup")
    system = build_merge_system_prompt(template, output_language="en")
    assert "write the minutes in English" in system and template.focus in system
    context = MeetingContext(notes_user="watch budget", glossary_terms=["SayInk"])
    assert "watch budget" in build_merge_prompt(context, [{"summary": "x"}])
    assert "SayInk" in build_merge_prompt(context, [{"summary": "x"}])
    task = {"task": "ship", "owner": "A", "due": "Friday", "ts": "00:00:01"}
    merged = _preserve_facts({"summary": "merged", "action_items": []}, [{"action_items": [task]}])
    assert merged["action_items"] == [task]


def test_hierarchical_merge_never_sends_oversized_requests(fake_httpx):
    from sayink.llm_transport import Cancellation
    from sayink.minutes_prompts import build_merge_prompt, build_merge_system_prompt
    request = _request(context_window_tokens=18000)
    worker = MinutesWorker(request)
    parts = [{"summary": f"section{i}:" + "细节" * 600} for i in range(9)]
    system = build_merge_system_prompt(request.template)
    assert not worker._fits(system, build_merge_prompt(request.context, parts))
    fake_httpx.script.extend([json.dumps({"summary": "merged"})] * 20)
    async def merge():
        async with httpx.AsyncClient() as client:
            return await worker._merge(client, httpx, parts)
    assert Cancellation().run(merge())["summary"] == "merged"
    assert len(fake_httpx.calls) > 1
    for call in fake_httpx.calls:
        messages = call["payload"]["messages"]
        assert worker._fits(messages[0]["content"], messages[1]["content"])


def test_queued_result_from_cancelled_worker_cannot_complete_new_job(monkeypatch):
    import threading
    from PyQt6.QtCore import QCoreApplication
    from sayink.minutes_generator import MinutesGenerator
    emitted, release = threading.Event(), threading.Event()
    old = []
    def work(self):
        if not old:
            old.append(self)
            self.finished_ok.emit("old result")
            emitted.set()
        release.wait(2)
    monkeypatch.setattr(MinutesWorker, "run", work)
    generator = MinutesGenerator()
    results = []
    generator.finished_ok.connect(results.append)
    generator.generate(_request())
    assert emitted.wait(1)
    generator.cancel()
    generator.generate(_request())
    new = generator._worker
    QCoreApplication.processEvents()
    assert not results and generator._worker is new
    release.set()
    for worker in list(generator._workers):
        assert worker.wait(2000)
    QCoreApplication.processEvents()
    generator.shutdown()


def test_idle_generation_callbacks_with_a_deleted_sender_are_ignored():
    from sayink.minutes_generator import MinutesGenerator

    generator = MinutesGenerator()
    events = []
    generator.finished_ok.connect(events.append)
    generator.error.connect(events.append)
    generator.progress.connect(events.append)
    generator.preview.connect(events.append)
    generator._on_done("stale result")
    generator._on_error("stale error")
    generator._on_progress("stale progress")
    generator._on_preview("stale preview")
    assert events == []


def test_embedded_json_preview_never_exposes_protocol(fake_httpx):
    worker = MinutesWorker(_request())
    previews = []
    worker.preview.connect(previews.append)
    worker._on_delta('Here is the result: {"summary":"readable text"}')
    assert previews and "readable text" in previews[-1]
    assert '"summary"' not in previews[-1] and "Here is the result" not in previews[-1]


@pytest.mark.parametrize("fallback", ["# Empty\n\n## Summary", "please retry later", '{"summary":"wrong format"}'])
def test_markdown_fallback_needs_actual_document_body(fake_httpx, fallback):
    fake_httpx.script.extend(["not JSON", fallback])
    results, errors, _ = _run(MinutesWorker(_request()))
    assert not results and errors and "Markdown" in errors[0]


def _echo_refinement_request(payload):
    data = json.loads(payload["messages"][1]["content"].split("\n", 1)[1])
    paragraphs = [{"source_ids": [unit["source_id"]], "text": unit["text"]} for unit in data["units"]]
    return data, json.dumps({"paragraphs": paragraphs}, ensure_ascii=False)


def test_real_transcript_recovers_from_repeated_truncation_without_losing_sources(fake_httpx):
    import re
    from pathlib import Path
    from sayink.minutes_prompts import parse_timestamp
    text = (Path(__file__).parent / "fixtures" / "refinement_20261019.txt").read_text(encoding="utf-8")
    lines = []
    for row in text.splitlines():
        match = re.fullmatch(r"\[([^]]+)\] 说话人(\d+)：(.*)", row)
        lines.append(TranscriptLine(parse_timestamp(match[1]), int(match[2]), match[3]))
    sizes = []
    def responder(payload):
        data, reply = _echo_refinement_request(payload)
        size = sum(len(unit["text"]) for unit in data["units"])
        sizes.append(size)
        return (200, reply, "length" if size > 250 else "stop")
    fake_httpx.responder = responder
    result, errors, progress = _run(MinutesWorker(_request(lines, template=get_template("refine"))))
    assert not errors and result and result[0].chunked
    original = refinement_chunks(lines, max_chars=4096 // 3)
    units = [unit for chunk in original for unit in chunk]
    paragraphs = json.loads(result[0].json_payload)["paragraphs"]
    assert [source for paragraph in paragraphs for source in paragraph["source_ids"]] == [unit.source_id for unit in units]
    assert "".join(paragraph["text"].replace("\n", "") for paragraph in paragraphs) == "".join(line.text for line in lines)
    assert sizes[0] > 250 and any(size <= 250 for size in sizes)
    assert any("自动拆小" in line for line in progress)


def test_single_source_can_be_split_and_reassembled_with_its_original_id(fake_httpx):
    lines = [TranscriptLine(0, 1, "讨论 API 的完整行为。如果下周通过，可能交付83万元的方案。" * 4)]
    count = []
    def responder(payload):
        data, reply = _echo_refinement_request(payload)
        size = sum(len(unit["text"]) for unit in data["units"])
        count.append(size)
        return (200, reply, "length" if size > 80 else "stop")
    fake_httpx.responder = responder
    results, errors, _ = _run(MinutesWorker(_request(lines, template=get_template("refine"))))
    assert not errors and results and len(count) > 2
    paragraphs = json.loads(results[0].json_payload)["paragraphs"]
    assert [p["source_ids"] for p in paragraphs] == [[1]]
    assert paragraphs[0]["text"].replace("\n", "") == lines[0].text


def test_split_children_resume_without_resending_the_truncated_parent(fake_httpx):
    checkpoints = {}
    lines = [TranscriptLine(0, 1, "先讨论预算，回款83万元。" * 12), TranscriptLine(1000, 1, "再讨论计划，下周可能交付。" * 12)]
    count = [0]
    def failing(payload):
        _data, reply = _echo_refinement_request(payload)
        count[0] += 1
        if count[0] == 1:
            return 200, reply, "length"
        if count[0] == 3:
            return 401, "auth failure"
        return reply
    fake_httpx.responder = failing
    save = lambda key, raw: checkpoints.update({key: raw})
    result, errors, _ = _run(MinutesWorker(_request(lines, template=get_template("refine"), save_checkpoint=save)))
    assert not result and errors and count[0] == 3
    initial_calls = len(fake_httpx.calls)
    fake_httpx.responder = lambda payload: _echo_refinement_request(payload)[1]
    result, errors, _ = _run(MinutesWorker(_request(lines, template=get_template("refine"),
                                                 checkpoints=dict(checkpoints), save_checkpoint=save)))
    assert result and not errors and len(fake_httpx.calls) - initial_calls == 1


def test_perpetual_truncation_is_bounded_and_does_not_publish(fake_httpx):
    count = []
    def responder(payload):
        data, reply = _echo_refinement_request(payload)
        count.append(sum(len(unit["text"]) for unit in data["units"]))
        return 200, reply, "length"
    fake_httpx.responder = responder
    results, errors, _ = _run(MinutesWorker(_request([TranscriptLine(0, 1, "不得遗漏有效信息。" * 45)], template=get_template("refine"))))
    assert not results and errors and "最小处理片段" in errors[0]
    assert len(count) <= 10


def test_repeated_fidelity_failure_is_split_without_relaxing_checks(fake_httpx):
    lines = [TranscriptLine(0, 1, "本月回款83万元。" * 6), TranscriptLine(1000, 1, "下月预算38万元。" * 6)]
    prompts = []
    def responder(payload):
        data, reply = _echo_refinement_request(payload)
        prompts.append(payload["messages"][1]["content"])
        if len(data["units"]) > 1:
            return reply.replace("83", "38")
        return reply
    fake_httpx.responder = responder
    results, errors, progress = _run(MinutesWorker(_request(lines, template=get_template("refine"))))
    assert not errors and results and results[0].chunked
    assert "必须恢复" in prompts[1] and "83万元" in prompts[1]
    assert any("未通过核对" in text for text in progress)
    assert results[0].markdown.count("83万元") == 6 and results[0].markdown.count("38万元") == 6

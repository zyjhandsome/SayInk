"""Import an existing transcript as an ended meeting, without inventing speakers or clocks."""
from __future__ import annotations

import json

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFileDialog, QPushButton

from sayink.history_store import HistoryStore, SegmentRecord
from sayink.meeting_session import MeetingController
from sayink.meeting_store import MeetingStore
from sayink.minutes_generator import MinutesRequest, MinutesWorker, request_fingerprint
from sayink.minutes_prompts import (
    MeetingContext,
    TranscriptLine,
    build_system_prompt,
    chunk_transcript_with_spans,
    get_template,
    render_transcript,
)
from sayink.minutes_render import clear_timeline
from sayink.transcript_import import (
    IMPORT_SOURCE,
    assign_speakers,
    describe_import,
    parse_transcript,
    read_transcript_file,
)
from sayink.ui.meeting_page import MeetingPage
from sayink.ui.transcript_import_dialog import TranscriptImportDialog


SRT = """1
00:00:01,000 --> 00:00:04,000
张三：今天先看检索。

2
00:00:18,000 --> 00:00:21,500
李四：下周联调。
"""

VTT = """WEBVTT

00:00:01.000 --> 00:00:04.000
<v 张三>今天先看检索。

00:00:18.000 --> 00:00:21.000
李四：下周联调。
"""

BLOCKS = """发言人1 00:00:01
今天先看检索。

李四 00:18
下周联调。
"""

TIMED_LINES = """[00:01:02] 张三：今天先看检索。
[00:01:40] 李四：下周联调。
"""

DIALOGUE = """张三：本周完成了检索。
李四：下周联调。
"""


def test_subtitles_keep_speakers_and_cues():
    srt = parse_transcript(SRT)
    assert srt.format_label == "SRT 字幕"
    assert [(turn.speaker_label, turn.offset_ms, turn.duration_ms, turn.text) for turn in srt.turns] == [
        ("张三", 1_000, 3_000, "今天先看检索。"),
        ("李四", 18_000, 3_500, "下周联调。"),
    ]
    vtt = parse_transcript(VTT)
    assert vtt.format_label == "WebVTT 字幕"
    assert vtt.turns[0].speaker_label == "张三" and vtt.turns[0].offset_ms == 1_000
    assert vtt.turns[1].speaker_label == "李四"


def test_block_and_inline_timestamps_are_not_treated_as_plain_text():
    blocks = parse_transcript(BLOCKS)
    assert blocks.format_label == "带时间的发言记录"
    assert [(turn.speaker_label, turn.offset_ms) for turn in blocks.turns] == [("发言人1", 1_000), ("李四", 18_000)]
    assert blocks.turns[0].text == "今天先看检索。"
    inline = parse_transcript(TIMED_LINES)
    assert inline.format_label == "带时间的对话"
    assert inline.turns[0].offset_ms == 62_000 and inline.turns[0].speaker_label == "张三"
    named = parse_transcript("张三 00:01:23：今天先看检索。\n李四 00:01:40：下周联调。")
    assert named.format_label == "带时间的对话"
    assert named.turns[0].offset_ms == 83_000
    dated = parse_transcript("张三 2026-11-12 14:30:05\n今天先看检索。\n\n李四(00:18)\n下周联调。\n")
    assert dated.format_label == "带时间的发言记录"
    assert [(turn.speaker_label, turn.offset_ms) for turn in dated.turns] == [
        ("张三", 52_205_000), ("李四", 18_000),
    ]


def test_dialogue_plain_text_and_conservative_labels():
    dialogue = parse_transcript(DIALOGUE)
    assert dialogue.format_label == "对话记录" and dialogue.has_speakers and not dialogue.has_timestamps
    assert [turn.speaker_label for turn in dialogue.turns] == ["张三", "李四"]
    titled = parse_transcript("# 周会\n\n本周完成了检索。\n\n下周联调。")
    assert titled.title == "周会" and titled.format_label == "纯文本"
    assert [turn.text for turn in titled.turns] == ["本周完成了检索。", "下周联调。"]
    lines = parse_transcript("第一句。\n第二句。")
    assert [turn.text for turn in lines.turns] == ["第一句。", "第二句。"] and not lines.has_speakers
    wrapped = parse_transcript("第一行\n第二行\n\n第三行")
    assert [turn.text for turn in wrapped.turns] == ["第一行", "第二行", "第三行"]
    stopped = parse_transcript("背景：这是说明\n注意：不要切分")
    assert stopped.format_label == "纯文本" and not stopped.has_speakers
    assert parse_transcript("只有张三：一句话。").format_label == "纯文本"
    forced = parse_transcript(DIALOGUE, detect_structure=False)
    assert forced.format_label == "纯文本" and not forced.has_speakers
    assert parse_transcript("   ").turns == ()


def test_speaker_ids_keep_numbers_and_assign_real_names():
    numbered, names = assign_speakers(parse_transcript(BLOCKS).turns)
    assert [speaker_id for speaker_id, _turn in numbered] == [1, 2]
    assert names == {2: "李四"}
    mixed, mixed_names = assign_speakers(parse_transcript("说话人1：甲。\n张三：乙。").turns)
    assert [speaker_id for speaker_id, _turn in mixed] == [1, 2]
    assert mixed_names == {2: "张三"}


def test_read_transcript_file_accepts_utf8_bom_and_gb18030(tmp_path):
    utf8 = tmp_path / "utf8.txt"
    utf8.write_bytes("张三：甲。\n李四：乙。".encode("utf-8-sig"))
    assert read_transcript_file(utf8).startswith("张三")
    assert "\ufeff" not in read_transcript_file(utf8)
    gb = tmp_path / "gb.txt"
    gb.write_bytes("张三：甲。\n李四：乙。".encode("gb18030"))
    parsed = parse_transcript(read_transcript_file(gb))
    assert [turn.speaker_label for turn in parsed.turns] == ["张三", "李四"]


def test_each_line_of_a_segment_keeps_its_own_edit_link():
    from PyQt6.QtGui import QTextDocument

    from sayink.minutes_prompts import TranscriptLine
    from sayink.ui.meeting_views import build_transcript_html

    html_text = build_transcript_html(
        [TranscriptLine(0, 0, "在你觉得呢家乐福都是看见你的名字\nsdnskjnxlzjnv ckj")],
        {},
        links=True,
        sequences=[0],
        editable=True,
        show_speakers=False,
        show_times=False,
    )
    document = QTextDocument()
    document.setHtml(html_text)
    seen_break = False
    edit_before_break = False
    it = document.begin().begin()
    while not it.atEnd():
        fragment = it.fragment()
        if "\u2028" in fragment.text() or fragment.text().endswith("\u2028"):
            seen_break = True
        if fragment.charFormat().anchorHref() == "edit-segment:0" and not seen_break:
            edit_before_break = True
        it += 1
    assert edit_before_break


def test_preview_describes_the_detected_shape():
    text = describe_import(parse_transcript(DIALOGUE))
    assert text.splitlines()[0] == "对话记录 · 2 段 · 2 位发言人 · 无时间戳"
    assert "张三：本周完成了检索" in text
    assert describe_import(parse_transcript("   ")) == "还没有可导入的内容"


def test_create_meeting_from_transcript_preserves_shape_and_survives_history_cleanup(tmp_path):
    history = HistoryStore(db_path=tmp_path / "history.db")
    meetings = MeetingStore(history)
    try:
        plain = meetings.create_meeting_from_transcript(parse_transcript(DIALOGUE), title="周会")
        assert plain is not None and meetings.flush()
        saved = meetings.get_meeting(plain.id)
        assert saved.source == IMPORT_SOURCE and saved.title == "周会"
        assert saved.status == "ended" and saved.ended_at is None and saved.duration_ms == 0
        segments = meetings.get_segments(plain.id)
        assert [segment.raw_text for segment in segments] == ["本周完成了检索。", "下周联调。"]
        assert [segment.speaker_id for segment in segments] == [1, 2]
        assert all(segment.captured_at == saved.started_at and segment.trigger_mode == "meeting" for segment in segments)
        assert [speaker.display_name for speaker in meetings.get_speakers(plain.id)] == ["张三", "李四"]
        assert all(speaker.name_locked for speaker in meetings.get_speakers(plain.id))
        timed = meetings.create_meeting_from_transcript(parse_transcript(SRT))
        assert timed is not None and meetings.flush()
        timed_saved = meetings.get_meeting(timed.id)
        assert timed_saved.ended_at is not None and timed_saved.duration_ms == 21_500
        first = meetings.get_segments(timed.id)[0]
        assert first.captured_at == timed_saved.started_at + 1_000 and first.duration_ms == 3_000
        assert meetings.create_meeting_from_transcript(parse_transcript("  ")) is None
        history.enqueue_delete_all()
        assert history.flush()
        assert [segment.raw_text for segment in meetings.get_segments(plain.id)] == ["本周完成了检索。", "下周联调。"]
    finally:
        history.close(timeout=2)


def _controller(config, tmp_path):
    config.set("llm.api_url", "http://localhost:11434/v1")
    config.set("llm.model_name", "local-model")
    config.set("meeting.auto_generate", True)
    history = HistoryStore(db_path=tmp_path / "history.db")
    return MeetingController(config, history, pending_segments=lambda: 0), history


def test_import_does_not_enqueue_minutes_and_plain_text_changes_the_prompt(config, tmp_path, monkeypatch):
    controller, history = _controller(config, tmp_path)
    calls = []
    generate = controller.generate
    monkeypatch.setattr(controller, "generate", lambda *args, **kwargs: calls.append(args))
    try:
        record = controller.import_transcript("本周完成了检索。\n\n下周联调。", title="导入周会")
        assert record is not None and record.source == IMPORT_SOURCE and record.ended_at is None
        assert calls == []
        assert controller.store.queued_generation(record.id) is None
        assert controller.transcript_text(record.id) == "本周完成了检索。\n下周联调。"
        monkeypatch.setattr(controller, "generate", generate)
        captured = []
        monkeypatch.setattr(controller._generator, "generate", captured.append)
        assert controller._start(record.id, "general") == ""
        request = captured[0]
        assert request.speakers_distinguished is False and request.timestamps_available is False
        prompt = build_system_prompt(
            request.template, speakers_distinguished=False, timestamps_available=False,
        )
        assert "不得将它们合成" not in prompt
        live = controller.store.create_meeting()
        history.enqueue(SegmentRecord(
            live.id, 1, live.started_at, raw_text="还没分出人。", trigger_mode="meeting", speaker_id=0,
            captured_at=live.started_at,
        ))
        controller.store.end_meeting(live.id, ended_at=live.started_at + 5_000)
        assert controller.store.flush()
        assert controller._start(live.id, "general") == ""
        assert captured[1].speakers_distinguished is True and captured[1].timestamps_available is True
        live_text = controller.transcript_text(live.id)
        assert "[00:00:00]" in live_text and "说话人待确认" in live_text
    finally:
        controller.shutdown()
        history.close(timeout=2)


def test_default_prompt_and_fingerprint_stay_stable_when_shape_flags_are_true():
    template = get_template("standup")
    assert build_system_prompt(template) == build_system_prompt(
        template, speakers_distinguished=True, timestamps_available=True,
    )
    request = MinutesRequest(
        api_url="http://localhost/v1", api_key="", model_name="m", template=template,
        context=MeetingContext(), lines=[TranscriptLine(0, 1, "讨论预算。")],
    )
    assert request_fingerprint(request) == request_fingerprint(
        MinutesRequest(
            api_url=request.api_url, api_key=request.api_key, model_name=request.model_name,
            template=request.template, context=request.context, lines=request.lines,
            speakers_distinguished=True, timestamps_available=True,
        )
    )
    changed = MinutesRequest(
        api_url=request.api_url, api_key=request.api_key, model_name=request.model_name,
        template=request.template, context=request.context, lines=request.lines,
        speakers_distinguished=False,
    )
    assert request_fingerprint(changed) != request_fingerprint(request)
    assert render_transcript([TranscriptLine(0, 1, "润色")]) == "[00:00:00] 说话人1：润色"
    assert [
        c[0]
        for c in chunk_transcript_with_spans(
            [TranscriptLine(0, 0, "本周完成了检索。")], None, max_chars=200,
            show_speakers=False, show_times=False,
        )
    ] == ["本周完成了检索。"]


def test_refinement_input_omits_the_unconfirmed_label_when_speakers_were_not_imported():
    from sayink.transcript_refiner import RefinementUnit, refinement_user_prompt

    hidden = refinement_user_prompt(
        [[RefinementUnit(1, 0, "本周完成了检索。")]], 0, {}, [], anonymous_speakers=True,
    )
    assert "说话人待确认" not in hidden
    named = refinement_user_prompt([[RefinementUnit(1, 1, "本周完成了检索。")]], 0, {}, [])
    assert "说话人1" in named


def test_clear_timeline_removes_invented_clocks():
    payload = {
        "topics": [{"title": "检索", "points": [{"text": "完成", "ts": "00:00:00"}]}],
        "decisions": [{"text": "联调", "ts": "00:00:01"}],
        "chapters": [{"start": "00:00:00", "end": "00:01:00", "title": "开始", "summary": "检索"}],
        "speakers": [{"label": "张三", "summary": "检索", "evidence_ts": "00:00:01"}],
    }
    clear_timeline(payload)
    assert payload["topics"][0]["points"][0]["ts"] == ""
    assert payload["decisions"][0]["ts"] == ""
    assert payload["chapters"][0]["start"] == "" and payload["chapters"][0]["end"] == ""
    assert payload["speakers"][0]["evidence_ts"] == ""


def test_worker_does_not_send_or_save_fake_speakers_and_clocks(monkeypatch):
    import httpx

    calls = []
    script = [json.dumps({
        "title": "周会",
        "summary": "完成检索。",
        "topics": [{"title": "检索", "points": [{"text": "完成检索", "ts": "00:00:00"}]}],
        "decisions": [], "action_items": [], "open_questions": [], "chapters": [], "speakers": [],
    })]

    original = httpx.AsyncClient

    def respond(request):
        calls.append(json.loads(request.content))
        body = "data: " + json.dumps({"choices": [{"delta": {"content": script[0]}, "finish_reason": None}]}) + "\n\n"
        body += "data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}) + "\n\n"
        body += "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())

    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs),
    )
    monkeypatch.setattr("sayink.minutes_generator._RETRY_DELAY_SEC", 0.0)
    worker = MinutesWorker(MinutesRequest(
        api_url="https://api.example.com/v1", api_key="k", model_name="m",
        template=get_template("general"), context=MeetingContext(title="周会"),
        lines=[TranscriptLine(0, 0, "本周完成了检索。"), TranscriptLine(0, 0, "下周联调。")],
        speakers_distinguished=False, timestamps_available=False, duration_ms=0, title_fallback="周会",
    ))
    results, errors = [], []
    worker.finished_ok.connect(results.append)
    worker.error.connect(errors.append)
    worker.run()
    assert not errors and results
    system = calls[0]["messages"][0]["content"]
    user = calls[0]["messages"][1]["content"]
    assert "不得将它们合成" not in system
    assert "说话人待确认" not in user and "00:00:00" not in user and "本周完成了检索" in user
    assert "00:00:00" not in results[0].markdown


def test_dialog_previews_structure_and_loads_a_file(tmp_path, monkeypatch):
    dialog = TranscriptImportDialog()
    try:
        ok = next(button for button in dialog.findChildren(QPushButton) if button.text() == "导入")
        assert not ok.isEnabled()
        dialog._edit.setPlainText(DIALOGUE)
        assert "2 位发言人" in dialog._preview.text() and "无时间戳" in dialog._preview.text()
        assert ok.isEnabled()
        dialog._detect.setChecked(False)
        assert "未区分说话人" in dialog._preview.text() and "纯文本" in dialog._preview.text()
        path = tmp_path / "notes.txt"
        path.write_bytes(DIALOGUE.encode("gb18030"))
        monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args, **kwargs: (str(path), ""))
        dialog._detect.setChecked(True)
        dialog._open_file()
        assert dialog.transcript_text().startswith("张三")
        assert dialog.parsed().has_speakers
    finally:
        dialog.deleteLater()


@pytest.fixture
def meeting(config, tmp_path):
    config.set("llm.api_url", "http://localhost:11434/v1")
    config.set("llm.model_name", "local-model")
    history = HistoryStore(db_path=tmp_path / "meetings.db")
    controller = MeetingController(config, history, pending_segments=lambda: 0)
    record = controller.store.create_meeting(title="设计评审")
    controller.store.end_meeting(record.id, ended_at=record.started_at + 85_000)
    controller.store.flush()
    page = MeetingPage(config, controller)
    page.show_meeting(record.id)
    yield page, controller, history, record
    page.deleteLater()
    controller.shutdown()
    history.close(timeout=2)


def test_meeting_page_imports_text_without_generating_or_offering_srt(meeting, monkeypatch):
    page, controller, _history, existing = meeting
    button = next(item for item in page.findChildren(QPushButton) if item.accessibleName() == "导入文字稿")
    assert button.text() == "导入文字稿"
    monkeypatch.setattr(page, "_ask_import", lambda: ("张三：本周完成了检索。\n李四：下周联调。", "导入周会", True))
    page._import_transcript()
    assert page._detail_id != existing.id
    record = controller.store.get_meeting(page._detail_id)
    assert record.source == IMPORT_SOURCE and record.ended_at is None
    assert controller.store.queued_generation(record.id) is None and not controller.is_generating
    transcript = page._transcript_view.toPlainText()
    assert "本周完成了检索" in transcript and "张三" in transcript
    assert "说话人待确认" not in transcript and "00:00" not in transcript
    meta = page._detail_meta.text()
    assert "导入文字稿" in meta and "已结束" not in meta and "0 秒" not in meta
    rows = [
        page._list.item(index).data(Qt.ItemDataRole.AccessibleTextRole) for index in range(page._list.count())
    ]
    imported = next(row for row in rows if "导入文字稿" in row)
    assert "已结束" not in imported
    page._jump_to_transcript(0)
    assert "没有时间戳" in page._status_label.text()
    seen = {}

    def _save(*args, **kwargs):
        seen["filter"] = args[3]
        return "", ""

    monkeypatch.setattr("sayink.ui.meeting_page.QFileDialog.getSaveFileName", _save)
    page._export("transcript")
    assert "SRT" not in seen["filter"]


def test_timestamped_import_keeps_times_and_srt_export(meeting, monkeypatch):
    page, _controller, _history, _existing = meeting
    monkeypatch.setattr(page, "_ask_import", lambda: (SRT, "", True))
    page._import_transcript()
    transcript = page._transcript_view.toPlainText()
    assert "张三" in transcript and "00:01" in transcript
    assert "说话人待确认" not in transcript
    seen = {}

    def _save(*args, **kwargs):
        seen["filter"] = args[3]
        return "", ""

    monkeypatch.setattr("sayink.ui.meeting_page.QFileDialog.getSaveFileName", _save)
    page._export("transcript")
    assert "SRT" in seen["filter"]


def test_plain_import_export_does_not_invent_speakers_or_clocks(config, tmp_path):
    controller, history = _controller(config, tmp_path)
    try:
        record = controller.import_transcript("本周完成了检索。\n下周联调。", title="导入周会")
        exported = controller.export_markdown(record.id)
        assert "本周完成了检索" in exported and "下周联调" in exported
        assert "00:00:00" not in exported and "说话人待确认" not in exported
        controller.store.set_segment_speaker(record.id, 0, 3)
        assert controller.store.flush()
        text = controller.transcript_text(record.id)
        full = controller.export_markdown(record.id)
        assert "说话人3" in text and "说话人待确认" not in text
        assert "说话人待确认" not in full and "00:00:00" not in full
    finally:
        controller.shutdown()
        history.close(timeout=2)


def test_utf16_transcript_round_trips():
    from sayink.transcript_import import decode_transcript

    text = decode_transcript("张三：本周完成了检索。\n李四：下周联调。".encode("utf-16"))
    parsed = parse_transcript(text)
    assert [turn.speaker_label for turn in parsed.turns] == ["张三", "李四"]


def test_truncated_utf16_is_rejected_and_keeps_the_editor(tmp_path, monkeypatch):
    from sayink.transcript_import import TranscriptDecodeError, decode_transcript

    with pytest.raises(TranscriptDecodeError, match="UTF-16"):
        decode_transcript(bytes.fromhex("FFFE61"))
    dialog = TranscriptImportDialog()
    try:
        dialog._edit.setPlainText("原来的文字")
        path = tmp_path / "cut.txt"
        path.write_bytes(bytes.fromhex("FFFE61"))
        monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args, **kwargs: (str(path), ""))
        dialog._open_file()
        assert dialog.transcript_text() == "原来的文字"
        assert "UTF-16" in dialog._preview.text()
    finally:
        dialog.deleteLater()


def test_short_webvtt_and_a_bad_srt_cue_are_not_silent():
    short = parse_transcript("WEBVTT\n\n00:01.000 --> 00:02.500\n第一条\n\n00:03.000 --> 00:04.000\n第二条\n")
    assert [turn.text for turn in short.turns] == ["第一条", "第二条"]
    assert short.turns[0].offset_ms == 1_000
    voice = parse_transcript("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<v 小李>同意</v>\n")
    assert voice.turns[0].speaker_label == "小李"
    assert voice.turns[0].text == "同意"
    broken = parse_transcript("1\n00:00:01,000 --> 00:00:02,000\n保留\n\n2\n00:00:63,000 --> 00:00:04,000\n丢失\n")
    assert [turn.text for turn in broken.turns] == ["保留"]
    assert broken.discarded == 1
    assert "丢失" in describe_import(broken)


def test_urls_and_stop_labels_keep_their_text():
    urls = parse_transcript("https://example.com/a\nhttps://example.com/b\n")
    assert urls.format_label == "纯文本"
    assert all(turn.speaker_label == "" for turn in urls.turns)
    assert "example.com/a" in urls.turns[0].text
    timed = parse_transcript("[00:01] 注意：必须保留\n[00:02] 小李：下一条\n")
    assert timed.turns[0].text == "注意：必须保留"
    assert timed.turns[0].speaker_label == ""
    assert timed.turns[1].speaker_label == "小李"


def test_same_timestamp_transcript_parses_quickly():
    import time

    text = "[00:00] 小李：测试内容\n" * 8000
    started = time.perf_counter()
    parsed = parse_transcript(text)
    assert time.perf_counter() - started < 1.5
    assert len(parsed.turns) == 8000


def test_mixed_transcript_does_not_invent_an_srt_clock(tmp_path):
    from sayink.meeting_session import MeetingController
    from sayink.minutes_prompts import render_srt, transcript_lines

    history = HistoryStore(db_path=tmp_path / "history.db")
    meetings = MeetingStore(history)
    try:
        parsed = parse_transcript("会前背景\n继续保留。\n[00:12] 小李：开始\n[00:13] 小王：继续\n")
        record = meetings.create_meeting_from_transcript(parsed, title="混合")
        assert record is not None and meetings.flush()
        segments = meetings.get_segments(record.id)
        assert segments[0].captured_at < 0
        lines = transcript_lines(segments, record.started_at)
        assert lines[0].time_known is False
        srt = render_srt(lines, {1: "小李", 2: "小王"})
        assert "会前背景" not in srt
        assert "00:00:12,000" in srt
        config_dir = tmp_path / "cfg"
        from sayink.config import Config
        controller = MeetingController(Config(config_dir=config_dir), history, pending_segments=lambda: 0)
        try:
            exported = controller.export_transcript_srt(record.id)
            assert "会前背景" not in exported
            assert "没有时间" in controller.srt_skip_note
        finally:
            controller.shutdown()
    finally:
        history.close(timeout=2)


def test_wall_clock_import_normalizes_duration_and_midnight_rollover(tmp_path):
    history = HistoryStore(db_path=tmp_path / "history.db")
    meetings = MeetingStore(history)
    try:
        day_text = (
            "张三 2026-11-12 14:30:00\n今天先看检索。\n\n"
            "李四 2026-11-12 15:05:00\n下周联调。\n"
        )
        day_parsed = parse_transcript(day_text)
        assert day_parsed.wall_clock_offsets is True
        day_rec = meetings.create_meeting_from_transcript(day_parsed, title="下午评审")
        assert day_rec is not None and meetings.flush()
        day_saved = meetings.get_meeting(day_rec.id)
        assert day_saved.duration_ms == 35 * 60 * 1000
        day_segs = meetings.get_segments(day_rec.id)
        assert day_segs[0].captured_at == day_saved.started_at
        assert day_segs[0].duration_ms == 35 * 60 * 1000
        assert day_segs[1].captured_at == day_saved.started_at + 35 * 60 * 1000

        midnight_text = (
            "[2026-11-12 23:58:00] 张三：夜班交接开始。\n"
            "[2026-11-13 00:03:00] 李四：交接完成。\n"
        )
        mid_parsed = parse_transcript(midnight_text)
        assert mid_parsed.wall_clock_offsets is True
        mid_rec = meetings.create_meeting_from_transcript(mid_parsed, title="跨零点交接")
        assert mid_rec is not None and meetings.flush()
        mid_saved = meetings.get_meeting(mid_rec.id)
        assert mid_saved.duration_ms == 5 * 60 * 1000
        mid_segs = meetings.get_segments(mid_rec.id)
        assert mid_segs[0].duration_ms == 5 * 60 * 1000
        assert mid_segs[1].captured_at == mid_saved.started_at + 5 * 60 * 1000
    finally:
        history.close(timeout=2)

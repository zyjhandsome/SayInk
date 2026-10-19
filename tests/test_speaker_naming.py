"""Source-backed names, ambiguous people, manual overrides and export parity."""
from __future__ import annotations

import json
import sqlite3

import pytest

from sayink.history_store import HistoryStore, SegmentRecord
from sayink.meeting_session import MeetingController, MeetingOptions, resolve_capabilities
from sayink.meeting_store import MeetingStore
from sayink.minutes_generator import MinutesResult
from sayink.minutes_prompts import BUILTIN_TEMPLATES, TranscriptLine, get_template
from sayink.minutes_render import render_markdown
from sayink.speaker_naming import infer_speaker_names
from sayink.ui.meeting_page import MeetingPage


def _lines(*pairs):
    return [TranscriptLine(i * 1000, sid, text) for i, (sid, text) in enumerate(pairs)]


@pytest.mark.parametrize("text,name", [
    ("大家好，我叫张三，负责研发。", "张三"),
    ("你好，我是李明。", "李明"),
    ("我的名字叫欧阳修。", "欧阳修"),
    ("我是王小明，今天汇报。", "王小明"),
    ("我叫Alice。", "Alice"),
    ("My name is Alice Smith.", "Alice Smith"),
    ("Hi, My name is Alice.", "Alice"),
    ("会议刚开始。我的名字是张三。", "张三"),
    ("刚刚是小明在讲话，我是赵一淳。", "赵一淳"),
    ("我是刚刚是那个小明在讲话，我是赵一淳。", "赵一淳"),
])
def test_explicit_self_introduction(text, name):
    assert infer_speaker_names(_lines((2, text), (1, "请开始"))) == {2: name}


@pytest.mark.parametrize("text", [
    "请张三汇报。", "张三，你说一下。", "李明做得不错。", "张三是负责人。",
    "他说我叫张三。", "张三说：我叫李四。", "我叫张三？", "假如我叫张三。",
    "比如我是李明。", "“我叫张三。”", "我叫张三，这是台词。",
    "我是负责研发的。", "我是王总的助理。", "我是王经理。", "我是高兴。",
    "我是张三的同事。", "我是张三还是李四？", "我不是张三。", "我叫张三不对。",
    "我的名字是测试。", "我是明天。",
    "张三说，我叫李四。", "Alice said, My name is Bob.",
    "可能，我叫张三。", "先假定一下，我是李明。",
    "I'm ready.", "My name is Assistant.", "我叫DeepSeek。",
])
def test_mentions_quotes_roles_and_uncertainty_do_not_name_a_speaker(text):
    assert infer_speaker_names(_lines((1, text))) == {}


def test_address_followed_by_short_reply_and_participant_list_are_not_identity():
    assert infer_speaker_names(_lines((1, "张三你在吗？"), (2, "在的，我来汇报。"))) == {}


def _reported_recording():
    return [
        TranscriptLine(0, 1, "直到前不久，我去参加了一次线下活动，体验到了一台完全不一样的笔记本。没错，就是我现在手上这台笔记本。"),
        TranscriptLine(8000, 1, "这是AIGC，像U盘Pro是Magic Spark，可以这么介绍。"),
        TranscriptLine(13000, 1, "那以往的Windows笔记本不同，它搭载了M V D R X Spark N文件超级芯片，就搭载了六千一百四十四个酷的核数的blackwear G P U."),
        TranscriptLine(38000, 2, "你好啊，我现在你能识别到我是不一样的声音吗？"),
        TranscriptLine(46000, 2, "我是刚刚是那个小明在讲话，我是赵一淳。"),
        TranscriptLine(57000, 2, "纯是纯净的纯，赵一纯。"),
    ]


def test_reported_recording_uses_explicit_character_spelling_without_naming_previous_voice():
    assert infer_speaker_names(_reported_recording()) == {2: "赵一纯"}


@pytest.mark.parametrize("intro,spelling,expected", [
    ("我是赵一淳。", "纯是纯净的纯，赵一纯。", {1: "赵一纯"}),
    ("我是赵一淳。", "纯净的纯，赵一纯。", {1: "赵一纯"}),
    ("我是赵一淳，纯是纯净的纯，赵一纯。", "", {1: "赵一纯"}),
    ("我叫赵一淳。", "赵一纯。", {}),
    ("我叫赵一淳。", "纯是纯净的纯，赵一淳。", {}),
    ("我叫赵一淳。", "纯是纯净的纯，赵一纯，赵一淳。", {}),
    ("我叫赵一淳。", "纯是纯净的纯，王小明。", {1: "赵一淳"}),
])
def test_spelling_requires_repeated_full_name_and_matching_character(intro, spelling, expected):
    assert infer_speaker_names(_lines((1, intro), (1, spelling))) == expected


def test_spelling_does_not_cross_speakers_intervening_turns_or_long_gaps():
    intro = TranscriptLine(0, 1, "我是赵一淳。")
    clarification = "纯是纯净的纯，赵一纯。"
    assert infer_speaker_names([intro, TranscriptLine(1000, 2, clarification)]) == {1: "赵一淳"}
    assert infer_speaker_names([intro, TranscriptLine(31000, 1, clarification)]) == {}
    assert infer_speaker_names([intro, TranscriptLine(1000, 2, "好的。"), TranscriptLine(2000, 1, clarification)]) == {}


def test_spelling_never_overrides_manual_name_or_hides_other_identity_conflicts():
    lines = _reported_recording()
    assert infer_speaker_names(lines, manual_names={2: "主持人"}) == {}
    assert infer_speaker_names(lines + [TranscriptLine(58000, 2, "我叫王五。")]) == {}
    assert infer_speaker_names(lines + [TranscriptLine(58000, 3, "我是赵一纯。")]) == {}


@pytest.mark.parametrize("pairs", [
    ((1, "我叫张三。"), (1, "我是李四。")),
    ((1, "我叫张三。"), (2, "我是张三。")),
    ((1, "我叫张三，我是李四。"),),
    ((1, "我叫张三。"), (1, "我不叫张三。")),
])
def test_conflicting_identity_does_not_pick_a_winner(pairs):
    assert infer_speaker_names(_lines(*pairs)) == {}


def test_manual_names_and_explicit_number_restore_take_precedence():
    lines = _lines((1, "我叫张三。"), (2, "我叫李四。"), (3, "我是王五。"))
    assert infer_speaker_names(lines, manual_names={1: "主持人", 2: ""}) == {3: "王五"}
    assert infer_speaker_names(lines, manual_names={4: "张三"}) == {2: "李四", 3: "王五"}


@pytest.fixture
def naming(config, tmp_path):
    history = HistoryStore(db_path=tmp_path / "names.db")
    controller = MeetingController(config, history, pending_segments=lambda: 0)
    yield controller, history
    controller.shutdown()
    history.close(timeout=2)


def _meeting(controller, history, *, template_id="general", edited=False, raw=None, polished=None):
    record = controller.store.create_meeting(title="评审", participants="张三、李四、王五")
    for seq, (sid, text) in enumerate(raw or [(1, "我叫张三，今天汇报。"), (2, "李四出差了。"), (3, "我是王五。")]):
        history.enqueue(SegmentRecord(
            record.id, seq, record.started_at + seq * 3000, raw_text=text,
            polished_text=polished or "", speaker_id=sid, trigger_mode="meeting",
            captured_at=record.started_at + seq * 3000,
        ))
    controller.store.end_meeting(record.id, ended_at=record.started_at + 60_000)
    payload = ({"kind": "refine", "paragraphs": [{"source_ids": [1], "text": "说话人1负责。说话人2审核。说话人3补充。"}]}
               if template_id == "refine" else {"summary": "说话人1负责。说话人2审核。说话人3补充。"})
    for markdown in ("旧版说话人1。", "说话人1负责。说话人2审核。说话人3补充。"):
        controller.store.save_notes(record.id, template_id=template_id, model="local", markdown=markdown,
                                    json_payload=json.dumps(payload, ensure_ascii=False), duration_ms=12)
    if edited:
        controller.store.save_user_markdown(record.id, "手写补充：说话人1负责。说话人2审核。说话人3补充。说话人11另有安排。")
    assert controller.store.flush()
    return record


@pytest.mark.parametrize("template", BUILTIN_TEMPLATES + (get_template("custom", [{"id": "custom", "name": "自定义"}]),), ids=lambda t: t.id)
def test_all_templates_transcript_and_export_share_validated_names(naming, template):
    controller, history = naming
    record = _meeting(controller, history, template_id=template.id)
    raw_before = [(s.raw_text, s.polished_text, s.speaker_id) for s in controller.store.get_segments(record.id)]
    assert controller.auto_name_speakers(record.id)
    assert controller.speaker_names(record.id) == {1: "张三", 3: "王五"}
    notes = controller.store.get_notes(record.id)
    assert "张三" in notes.markdown and "王五" in notes.markdown and "说话人2" in notes.markdown
    assert notes.previous_markdown == "旧版说话人1。"
    assert not notes.user_edited and notes.model == "local" and notes.duration_ms == 12
    for text in (controller.transcript_text(record.id), controller.export_transcript_markdown(record.id), controller.export_markdown(record.id)):
        assert "张三：我叫张三" in text and "王五：我是王五" in text
        assert "说话人2：李四出差了。" in text
    assert [(s.raw_text, s.polished_text, s.speaker_id) for s in controller.store.get_segments(record.id)] == raw_before
    assert controller.auto_name_speakers(record.id)  # Idempotent; preserve the genuine backup.
    assert controller.store.get_notes(record.id) == notes


def test_preserves_user_edits_backup_and_similar_speaker_numbers(naming):
    controller, history = naming
    record = _meeting(controller, history, edited=True)
    assert controller.auto_name_speakers(record.id)
    notes = controller.store.get_notes(record.id)
    assert notes.markdown == "手写补充：张三负责。说话人2审核。王五补充。说话人11另有安排。"
    assert notes.user_edited and notes.previous_markdown == "旧版说话人1。"
    controller.restore_previous_notes(record.id)
    assert controller.store.get_notes(record.id).markdown == "旧版张三。"


def test_invented_names_in_polishing_or_model_output_are_not_evidence(naming):
    controller, history = naming
    record = _meeting(controller, history, raw=[(1, "我来汇报。")], polished="我叫张三。")
    payload = {"summary": "说话人1汇报。", "speakers": [{"label": "说话人1", "suggested_name": "张三", "evidence_ts": "00:00:00"}]}
    controller._generating_for = record.id
    controller._on_generation_done(MinutesResult(json.dumps(payload), "说话人1汇报。", False, 1, "local", "general"))
    assert controller.auto_name_speakers(record.id)
    assert controller.speaker_names(record.id) == {}


def test_manual_override_and_reset_survive_later_generation_and_restart(naming, config):
    controller, history = naming
    record = _meeting(controller, history)
    assert controller.auto_name_speakers(record.id)
    controller.rename_speaker(record.id, 1, "主持人")
    controller.rename_speaker(record.id, 3, "")
    assert controller.auto_name_speakers(record.id)
    assert controller.speaker_names(record.id) == {1: "主持人"}
    assert "主持人负责" in controller.store.get_notes(record.id).markdown
    assert "说话人3补充" in controller.store.get_notes(record.id).markdown
    path = history.db_path
    controller.shutdown()
    history.close(timeout=2)
    reopened = HistoryStore(db_path=path)
    restored = MeetingController(config, reopened, pending_segments=lambda: 0)
    try:
        assert restored.auto_name_speakers(record.id)
        assert restored.speaker_names(record.id) == {1: "主持人"}
        assert next(s for s in restored.store.get_speakers(record.id) if s.speaker_id == 3).name_locked
    finally:
        restored.shutdown()
        reopened.close(timeout=2)


def test_manual_name_collision_withdraws_ambiguous_automatic_label(naming):
    controller, history = naming
    record = _meeting(controller, history)
    assert controller.auto_name_speakers(record.id)
    controller.rename_speaker(record.id, 2, "张三")
    assert controller.speaker_names(record.id) == {2: "张三", 3: "王五"}
    assert "说话人1负责" in controller.store.get_notes(record.id).markdown


def test_failed_notes_write_rolls_back_names_and_prevents_generation(naming, config, monkeypatch):
    controller, history = naming
    record = _meeting(controller, history)
    before = controller.store.get_notes(record.id)
    history.enqueue_write(lambda conn: conn.execute(
        "CREATE TRIGGER fail_names BEFORE UPDATE OF markdown ON meeting_notes "
        "BEGIN SELECT RAISE(ABORT, 'simulate disk failure'); END"
    ))
    assert history.flush()
    assert not controller.auto_name_speakers(record.id)
    assert controller.speaker_names(record.id) == {}
    assert controller.store.get_notes(record.id) == before
    config.set("llm.api_url", "http://localhost:11434/v1")
    config.set("llm.model_name", "local")
    requests, errors = [], []
    monkeypatch.setattr(controller._generator, "generate", requests.append)
    controller.generation_failed.connect(lambda _mid, error: errors.append(error))
    assert not controller.generate(record.id)
    assert not requests and "名称保存失败" in errors[-1]
    history.enqueue_write(lambda conn: conn.execute("DROP TRIGGER fail_names"))
    assert history.flush()
    assert controller.auto_name_speakers(record.id)


def test_automatic_names_are_isolated_to_one_meeting(naming):
    controller, history = naming
    first = _meeting(controller, history)
    second = _meeting(controller, history, raw=[(1, "我来汇报。")])
    assert controller.auto_name_speakers(first.id)
    assert controller.auto_name_speakers(second.id)
    assert controller.speaker_names(first.id) == {1: "张三", 3: "王五"}
    assert controller.speaker_names(second.id) == {}


def test_correcting_source_turn_recomputes_names_without_rewriting_edited_text(naming):
    controller, history = naming
    record = _meeting(controller, history, edited=True)
    assert controller.auto_name_speakers(record.id)
    # Two introductions now belong to one group: withdraw both automatic names.
    assert controller.correct_segment_speaker(record.id, 2, 1)
    assert controller.speaker_names(record.id) == {}
    notes = controller.store.get_notes(record.id)
    assert notes.user_edited and notes.transcript_changed
    assert "手写补充" in notes.markdown and "说话人1负责" in notes.markdown


@pytest.mark.parametrize("template", BUILTIN_TEMPLATES, ids=lambda t: t.id)
def test_generation_receives_names_before_any_model_call(naming, config, monkeypatch, template):
    controller, history = naming
    record = _meeting(controller, history, template_id=template.id)
    config.set("llm.api_url", "http://localhost:11434/v1")
    config.set("llm.model_name", "local")
    requests = []
    monkeypatch.setattr(controller._generator, "generate", requests.append)
    assert controller.generate(record.id, template_id=template.id)
    assert len(requests) == 1 and requests[0].speaker_names == {1: "张三", 3: "王五"}
    assert controller.speaker_names(record.id) == requests[0].speaker_names


def test_finalize_names_only_after_reclustering(naming, config, monkeypatch):
    controller, history = naming
    options = MeetingOptions()
    caps = resolve_capabilities(config, options, dictation_model_id="funasr-nano")
    record = controller.begin(options, caps)
    history.enqueue(SegmentRecord(record.id, 0, record.started_at, raw_text="我叫张三。", speaker_id=2,
                                  trigger_mode="meeting", captured_at=record.started_at))
    assert history.flush()
    assert not controller.auto_name_speakers(record.id)  # Live groups can still change.
    def recluster(mid):
        controller.store.relabel_segments(mid, {2: 1})
    monkeypatch.setattr(controller, "_apply_recluster", recluster)
    controller.finalize()
    assert controller.speaker_names(record.id) == {1: "张三"}


def test_ui_labels_source_of_name_and_copy_uses_it(naming, config):
    controller, history = naming
    record = _meeting(controller, history)
    assert controller.auto_name_speakers(record.id)
    page = MeetingPage(config, controller)
    try:
        page.show_meeting(record.id)
        plain = page._transcript_view.toPlainText()
        assert "张三" in plain and "原文自报姓名" in plain and "说话人2" in plain
        assert "张三负责" in page._notes_view.toPlainText()
        page._tab_transcript.click()
        assert "无法确定时保留编号" in page._content_hint.text()
        page._rename_speaker(1, "主持人")
        assert "主持人" in page._transcript_view.toPlainText() and "手动命名" in page._transcript_view.toPlainText()
    finally:
        page.deleteLater()


def test_reported_recording_updates_notes_transcript_and_visible_copy(naming, config, monkeypatch):
    controller, history = naming
    source = _reported_recording()
    record = controller.store.create_meeting(title="复现姓名释字")
    for seq, line in enumerate(source):
        history.enqueue(SegmentRecord(
            record.id, seq, record.started_at + line.offset_ms, raw_text=line.text,
            speaker_id=line.speaker_id, trigger_mode="meeting", captured_at=record.started_at + line.offset_ms,
        ))
    controller.store.end_meeting(record.id, ended_at=record.started_at + 61_000)
    payload = {"summary": "说话人1介绍产品，说话人2进行声音测试。",
               "speakers": [{"label": "说话人2", "summary": "进行声音测试。", "suggested_name": "赵一淳", "evidence_ts": "00:00:46"}]}
    controller.store.save_notes(record.id, template_id="general", model="offline", json_payload=json.dumps(payload), markdown="说话人2进行声音测试。")
    assert controller.store.flush()
    assert controller.auto_name_speakers(record.id)
    assert controller.speaker_names(record.id) == {2: "赵一纯"}
    notes = controller.store.get_notes(record.id)
    assert "赵一纯进行声音测试" in notes.markdown
    assert "可能是 赵一淳" not in notes.markdown
    copied = []
    monkeypatch.setattr("sayink.ui.meeting_page.copy_text", lambda text: copied.append(text) or True)
    page = MeetingPage(config, controller)
    try:
        page.show()
        page.show_meeting(record.id)
        page._tab_transcript.click()
        assert page._transcript_copy_btn.isVisible() and page._transcript_copy_btn.isEnabled()
        page._transcript_copy_btn.click()
        assert copied == [controller.transcript_text(record.id)]
        assert "[00:00:46] 赵一纯：我是刚刚是那个小明在讲话，我是赵一淳。" in copied[0]
        assert "[00:00:00] 说话人1：" in copied[0]
        assert [s.raw_text for s in controller.store.get_segments(record.id)] == [line.text for line in source]
        assert "赵一纯：" in controller.export_markdown(record.id)
    finally:
        page.deleteLater()


def test_legacy_speaker_table_migrates_without_losing_names(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE meeting_speakers (meeting_id TEXT, speaker_id INTEGER, display_name TEXT DEFAULT '', suggested_name TEXT DEFAULT '', PRIMARY KEY (meeting_id, speaker_id))")
    conn.execute("INSERT INTO meeting_speakers VALUES ('m', 1, '手工姓名', '')")
    conn.commit()
    conn.close()
    history = HistoryStore(db_path=path)
    try:
        row = MeetingStore(history).get_speakers("m")[0]
        assert row.display_name == "手工姓名" and row.name_locked
    finally:
        history.close(timeout=2)


def test_known_display_name_hides_conflicting_model_hint():
    markdown = render_markdown({"speakers": [{"label": "说话人1", "summary": "汇报", "suggested_name": "李四"}]},
                               template=get_template("standup"), speaker_names={1: "张三"})
    assert "张三" in markdown and "李四" not in markdown


@pytest.mark.parametrize("template_id", ["general", "standup", "refine"])
def test_literal_names_in_model_json_follow_later_manual_correction(naming, template_id):
    controller, history = naming
    record = _meeting(controller, history)
    assert controller.auto_name_speakers(record.id)
    payload = {"summary": "张三负责。王五审核。", "speakers": [{"label": "张三", "summary": "张三负责"}],
               "paragraphs": [{"source_ids": [1], "text": "张三负责。王五审核。"}],
               "_speaker_names": {"1": "张三", "3": "王五"}}
    controller.store.save_notes(record.id, template_id=template_id, model="m", json_payload=json.dumps(payload), markdown="张三负责。王五审核。")
    assert controller.store.flush()
    controller.rename_speaker(record.id, 1, "张晓明")
    notes = controller.store.get_notes(record.id)
    assert "张晓明负责" in notes.markdown and "张三负责" not in notes.markdown
    if template_id != "refine":
        assert "参会人：张三、李四、王五" in notes.markdown  # User-entered metadata is independent.
    controller.rename_speaker(record.id, 1, "")
    assert "说话人1负责" in controller.store.get_notes(record.id).markdown


def test_name_swap_is_simultaneous_and_render_does_not_modify_payload():
    payload = {"summary": "张三负责，李四审核。说话人1补充。",
               "_speaker_names": {"1": "张三", "2": "李四"}}
    before = json.dumps(payload)
    markdown = render_markdown(payload, template=get_template("general"), speaker_names={1: "李四", 2: "张三"})
    assert "李四负责，张三审核。李四补充。" in markdown
    assert json.dumps(payload) == before

"""Follow-up guards for the confirmed fidelity, naming and minutes issues."""
from sayink.asr_correction import apply_glossary_variants
from sayink.minutes_render import extract_json, replace_names_in_markdown
from sayink.minutes_validation import validate_minutes
from sayink.polish_fidelity import polish_rejection_reason
from sayink.speaker_naming import infer_speaker_names
from sayink.minutes_prompts import TranscriptLine
from sayink.text_polisher import chat_message_text, LLMTruncatedError, LLMResponseError


def test_ellipsis_after_a_chinese_amount_still_sees_the_number():
    assert polish_rejection_reason("大概五万…", "大概八万。") == "数字被改动"


def test_added_negation_is_rejected():
    assert polish_rejection_reason("明天去一趟。", "明天不去一趟。") == "否定词新增"


def test_sign_flip_is_rejected_but_a_range_is_not():
    assert polish_rejection_reason("温度是-10度", "温度是10度") == "数字被改动"
    assert polish_rejection_reason("净利润-5.2万元", "净利润5.2万元") == "数字被改动"
    assert polish_rejection_reason("偏移量+3.5毫米", "偏移量-3.5毫米") == "数字被改动"
    assert polish_rejection_reason("大概3-5天", "大概3到5天。") == ""


def test_roles_and_origins_are_not_personal_names():
    lines = [TranscriptLine(0, 1, "大家好，我是程序员，主要写后端。"), TranscriptLine(1, 2, "我是张伟。")]
    assert infer_speaker_names(lines) == {2: "张伟"}


def test_a_shorter_chinese_name_does_not_rewrite_a_longer_one():
    text, count = replace_names_in_markdown("王总确认；王总监补充。", "王总", "王建国")
    assert "王建国监" not in text
    assert "王总监" in text
    assert count == 1


def test_a_single_chinese_character_is_not_a_global_replacement():
    assert apply_glossary_variants("中国的中心", [{"term": "证", "variants": ["中"]}]) == "中国的中心"


def test_an_extra_numeric_field_does_not_reject_the_minutes():
    payload = validate_minutes({
        "summary": "讨论了预算。",
        "decisions": [{"text": "通过预算", "id": 1}],
    })
    assert payload["decisions"][0]["text"] == "通过预算"
    assert "id" not in payload["decisions"][0]


def test_unclosed_braces_do_not_scan_quadratically():
    import time
    started = time.perf_counter()
    assert extract_json("{" * 8000) is None
    assert time.perf_counter() - started < 0.2


def test_truncated_or_non_text_content_is_rejected_and_think_blocks_are_removed():
    try:
        chat_message_text({"choices": [{"finish_reason": "length", "message": {"content": "半句"}}]}, reject_truncated=True)
    except LLMTruncatedError:
        pass
    else:
        raise AssertionError("truncated reply was accepted")
    try:
        chat_message_text({"choices": [{"message": {"content": [1]}}]})
    except LLMResponseError:
        pass
    else:
        raise AssertionError("list repr would have been pasted")
    assert chat_message_text({"choices": [{"message": {"content": "<think>草稿</think>今天下午开会。"}}]}) == "今天下午开会。"

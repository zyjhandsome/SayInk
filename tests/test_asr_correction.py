"""Glossary mis-hearing variants and the LLM 纠错 (correction) mode."""

import json
import sys
from unittest.mock import MagicMock, patch

from sayink.asr_correction import (
    CORRECTION_PROMPT,
    apply_glossary_variants,
    correction_rejection_reason,
    correction_user_prompt,
    format_glossary_line,
    glossary_prompt_lines,
    glossary_variant_map,
    parse_glossary_line,
)
from sayink.text_polisher import (
    LLM_MODE_CORRECT,
    LLM_MODE_POLISH,
    POLISH_PROMPT,
    PolishWorker,
    build_system_prompt,
    normalize_llm_mode,
    thinking_payload_extra,
)


# ── glossary lines ───────────────────────────────────────────────────────────


class TestGlossaryLines:
    def test_plain_line_is_just_a_term(self):
        assert parse_glossary_line("SayInk") == {"term": "SayInk"}
        assert parse_glossary_line("  SayInk  ") == {"term": "SayInk"}

    def test_equals_sign_lists_mis_hearings(self):
        assert parse_glossary_line("五证=五中、无证") == {"term": "五证", "variants": ["五中", "无证"]}

    def test_fullwidth_equals_and_every_separator(self):
        entry = parse_glossary_line("张鑫 ＝ 张新, 张欣，张心; 张信； 章新")
        assert entry == {"term": "张鑫", "variants": ["张新", "张欣", "张心", "张信", "章新"]}

    def test_variants_are_capped_deduped_and_never_the_term(self):
        entry = parse_glossary_line("a=a、b、b、c、d、e、f、g")
        assert entry == {"term": "a", "variants": ["b", "c", "d", "e", "f"]}
        assert parse_glossary_line("x=" + "y" * 21 + "、z") == {"term": "x", "variants": ["z"]}

    def test_blank_or_termless_lines_are_dropped(self):
        assert parse_glossary_line("") is None
        assert parse_glossary_line("   ") is None
        assert parse_glossary_line("=误写") is None
        # An equals sign with nothing after it is a plain term.
        assert parse_glossary_line("Term=") == {"term": "Term"}

    def test_format_round_trips(self):
        for line in ("SayInk", "五证=五中、无证", "GitHub=git hub"):
            assert format_glossary_line(parse_glossary_line(line)) == line
        assert format_glossary_line({"term": "GitHub", "variants": []}) == "GitHub"
        assert format_glossary_line({"term": ""}) == ""
        assert format_glossary_line("plain") == "plain"


# ── deterministic replacement ────────────────────────────────────────────────


GLOSSARY = [
    {"term": "五证", "variants": ["五中", "无证"]},
    {"term": "GitHub", "variants": ["git hub", "get hub"]},
    {"term": "新盘", "variants": ["训盘"]},
    {"term": "SayInk"},
]


class TestApplyGlossaryVariants:
    def test_replaces_listed_mis_hearings_only(self):
        assert apply_glossary_variants("五中和无证都齐了，训盘上线", GLOSSARY) == "五证和五证都齐了，新盘上线"
        assert apply_glossary_variants("五证齐全", GLOSSARY) == "五证齐全"

    def test_latin_variants_are_whole_words_and_case_insensitive(self):
        assert apply_glossary_variants("代码在 Git Hub 上", GLOSSARY) == "代码在 GitHub 上"
        assert apply_glossary_variants("digit hubris", GLOSSARY) == "digit hubris"

    def test_longest_variant_wins(self):
        glossary = [{"term": "甲", "variants": ["乙丙"]}, {"term": "丁", "variants": ["乙丙戊"]}]
        assert apply_glossary_variants("x乙丙戊x", glossary) == "x丁x"
        assert apply_glossary_variants("x乙丙x", glossary) == "x甲x"

    def test_variant_equal_to_another_term_is_ignored(self):
        glossary = [{"term": "新盘", "variants": ["训盘"]}, {"term": "训盘"}]
        assert glossary_variant_map(glossary) == []
        assert apply_glossary_variants("训盘", glossary) == "训盘"

    def test_replacement_is_idempotent_so_the_guard_baseline_cannot_drift(self):
        # 「盘」 is inside the term 「新盘」 (and inside its own term): applying
        # it to text that already reads 新盘 would produce 新盘点.
        glossary = [{"term": "新盘", "variants": ["训盘"]}, {"term": "盘点", "variants": ["盘", "判点"]}]
        once = apply_glossary_variants("训盘的判点", glossary)
        assert once == "新盘的盘点"
        assert apply_glossary_variants(once, glossary) == once
        assert correction_rejection_reason(once, once, glossary) == ""

    def test_no_variants_or_empty_text_is_a_no_op(self):
        assert apply_glossary_variants("原文", [{"term": "SayInk"}]) == "原文"
        assert apply_glossary_variants("", GLOSSARY) == ""
        assert apply_glossary_variants("原文", None) == "原文"

    def test_plain_strings_are_accepted_as_glossary_entries(self):
        assert apply_glossary_variants("原文", ["SayInk", ""]) == "原文"


# ── prompt ───────────────────────────────────────────────────────────────────


class TestCorrectionPrompt:
    def test_system_prompt_forbids_polishing(self):
        for phrase in ("校对", "不润色", "不删口语赘词", "不回答", "正确写法 ← 可能被听成的写法"):
            assert phrase in CORRECTION_PROMPT

    def test_glossary_lines_use_the_arrow_notation(self):
        assert glossary_prompt_lines(GLOSSARY) == ["五证 ← 五中、无证", "GitHub ← git hub、get hub", "新盘 ← 训盘", "SayInk"]

    def test_user_prompt_has_glossary_context_tail_and_text(self):
        prompt = correction_user_prompt("待校对文本", GLOSSARY, "上" * 200)
        assert prompt.startswith("术语表：\n- 五证 ← 五中、无证\n")
        assert "上文（仅供参考，不要输出）：" + "上" * 120 + "\n" in prompt
        assert "上" * 121 not in prompt
        assert prompt.endswith("待校对：\n待校对文本")

    def test_user_prompt_without_glossary_or_context_is_just_the_text(self):
        assert correction_user_prompt("文本") == "待校对：\n文本"


# ── acceptance guard ─────────────────────────────────────────────────────────


class TestCorrectionGuard:
    def test_accepts_a_homophone_fix(self):
        assert correction_rejection_reason("今天增送一套", "今天赠送一套") == ""
        assert correction_rejection_reason("原文没有错", "原文没有错") == ""

    def test_rejects_a_rewrite_or_an_answer(self):
        # The shared polish guard already refuses a reply this much longer;
        # a milder expansion falls through to correction's own length budget.
        assert correction_rejection_reason("今天增送一套", "好的，今天赠送一套，还有什么需要帮忙的吗？") == "长度异常"
        assert correction_rejection_reason("今天增送一套房子给你", "今天赠送一套房子给你，请查收并确认。") == "长度变化过大"
        assert correction_rejection_reason("这个问题怎么解决", "可以先重启试试") == "改动过多"

    def test_rejects_polish_style_filler_removal(self):
        # Removing 嗯/那个 is polish, not correction: the sentence must stay.
        assert correction_rejection_reason("嗯那个这个方案还行吧", "这个方案还行吧") == "长度变化过大"

    def test_keeps_the_polish_guard_rules(self):
        assert correction_rejection_reason("我们有一百个人", "我们有十个人") == "数字被改动"
        assert correction_rejection_reason("请不要删除", "请删除") == "否定词丢失"
        assert correction_rejection_reason("用 GitHub 托管", "用 托管") == "英文专名丢失"

    def test_glossary_replacement_is_not_charged_against_the_budget(self):
        glossary = [{"term": "五证", "variants": ["五四证"]}]
        # Without the glossary 五四证 → 五证 looks like a number change.
        assert correction_rejection_reason("五四证齐全", "五证齐全") == "数字被改动"
        assert correction_rejection_reason("五四证齐全", "五证齐全", glossary) == ""

    def test_empty_raw_only_accepts_empty_reply(self):
        assert correction_rejection_reason("", "") == ""
        assert correction_rejection_reason("", "凭空出现") == "空文本被扩写"

    def test_punctuation_changes_do_not_count_as_edits(self):
        assert correction_rejection_reason("我们走吧好的", "我们走吧，好的。") == ""

    def test_edit_budget_is_a_fifth_of_the_text_with_a_floor_of_two(self):
        assert correction_rejection_reason("abcde", "abXYe") == ""          # 2 ≤ max(2, 1)
        assert correction_rejection_reason("abcde", "aXYZe") == "改动过多"  # 3 > 2
        long = "天地玄黄宇宙洪荒日月" * 3                                       # 30 chars → budget 6
        assert correction_rejection_reason(long, long[:24] + "甲乙丙丁戊己") == ""
        assert correction_rejection_reason(long, long[:23] + "甲乙丙丁戊己庚") == "改动过多"


# ── mode plumbing in text_polisher ───────────────────────────────────────────


class TestCorrectionMode:
    def test_normalize_mode(self):
        assert normalize_llm_mode("correct") == LLM_MODE_CORRECT
        assert normalize_llm_mode(" Correct ") == LLM_MODE_CORRECT
        assert normalize_llm_mode("polish") == LLM_MODE_POLISH
        for value in ("translate", "", None, "typo"):
            assert normalize_llm_mode(value) == LLM_MODE_POLISH

    def test_system_prompt_follows_the_mode(self):
        assert build_system_prompt(LLM_MODE_CORRECT) == CORRECTION_PROMPT
        assert build_system_prompt(LLM_MODE_POLISH) == POLISH_PROMPT
        assert build_system_prompt(LLM_MODE_CORRECT, custom_prompt="我的提示词") == "我的提示词"

    def test_a_verbatim_builtin_prompt_does_not_pin_the_other_mode(self):
        # 恢复默认 pastes POLISH_PROMPT into the editor; switching to 纠错
        # afterwards must still use the correction prompt.
        assert build_system_prompt(LLM_MODE_CORRECT, custom_prompt=POLISH_PROMPT) == CORRECTION_PROMPT
        assert build_system_prompt(LLM_MODE_POLISH, custom_prompt=CORRECTION_PROMPT) == POLISH_PROMPT

    def test_thinking_is_only_disabled_for_official_deepseek_dual_mode_models(self):
        assert thinking_payload_extra("https://api.deepseek.com/v1", "deepseek-flash", "disabled") == {
            "thinking": {"type": "disabled"}
        }
        assert thinking_payload_extra("https://api.deepseek.com/v1", "deepseek-chat", "disabled") == {}
        assert thinking_payload_extra("https://proxy.example.com/v1", "deepseek-flash", "disabled") == {}
        assert thinking_payload_extra("https://api.deepseek.com/v1", "deepseek-flash", None) == {}

    def test_worker_builds_the_correction_request(self):
        worker = PolishWorker(
            "https://api.deepseek.com/v1", "k", "deepseek-flash", "今天增送一套",
            mode=LLM_MODE_CORRECT, glossary=GLOSSARY, context="上一句",
        )
        assert worker.is_correction
        assert worker._prompt == CORRECTION_PROMPT
        assert worker._action_label == "纠错"
        message = worker.user_message()
        assert message.startswith("术语表：") and "上一句" in message and message.endswith("今天增送一套")
        assert worker.request_options() == {
            "temperature": 0.0, "extra_payload": {"thinking": {"type": "disabled"}},
        }

    def test_polish_request_is_unchanged(self):
        worker = PolishWorker("https://api.deepseek.com/v1", "k", "deepseek-flash", "文本")
        assert not worker.is_correction
        assert worker.user_message() == "文本"
        assert worker.request_options() == {"temperature": 0.3, "extra_payload": {}}
        assert worker._action_label == "润色"

    def test_run_posts_the_correction_payload(self):
        response = MagicMock()
        response.json.return_value = {"choices": [{"message": {"content": "今天赠送一套"}}]}
        response.raise_for_status.return_value = None
        client = MagicMock()
        client.post.return_value = response
        client.__enter__ = lambda self: client
        client.__exit__ = lambda self, *exc: False
        json_dumps = json.dumps

        def stream(method, url, headers=None, json=None, **kwargs):
            resp = client.post(url, headers=headers, json=json)
            content = resp.json()["choices"][0]["message"]["content"]
            resp.status_code = 200
            resp.iter_lines.return_value = iter((
                "data: " + json_dumps(
                    {"choices": [{"delta": {"content": content}, "finish_reason": None}]}
                ),
                "",
                "data: " + json_dumps(
                    {"choices": [{"delta": {}, "finish_reason": "stop"}]}
                ),
                "",
                "data: [DONE]",
                "",
            ))
            ctx = MagicMock()
            ctx.__enter__.return_value = resp
            ctx.__exit__.return_value = False
            return ctx

        client.stream.side_effect = stream
        fake_httpx = MagicMock()
        fake_httpx.Client.return_value = client
        fake_httpx.NetworkError = type("NetworkError", (Exception,), {})
        fake_httpx.RemoteProtocolError = type("RemoteProtocolError", (Exception,), {})
        fake_httpx.TimeoutException = type("TimeoutException", (Exception,), {})

        worker = PolishWorker("https://api.deepseek.com/v1", "k", "deepseek-flash", "今天增送一套",
                              mode=LLM_MODE_CORRECT, glossary=GLOSSARY)
        results = []
        worker.result_ready.connect(results.append)
        with patch.dict(sys.modules, {"httpx": fake_httpx}):
            worker.run()
        assert results == ["今天赠送一套"]
        payload = client.post.call_args.kwargs["json"]
        assert payload["temperature"] == 0.0
        assert payload["stream"] is True
        assert payload["thinking"] == {"type": "disabled"}
        assert payload["messages"][0]["content"] == CORRECTION_PROMPT
        assert payload["messages"][1]["content"].endswith("待校对：\n今天增送一套")


# ── config ───────────────────────────────────────────────────────────────────


class TestConfigMode:
    def test_correct_mode_round_trips_and_unknown_falls_back(self, config_home):
        from sayink.config import Config

        (config_home / "config.json").write_text('{"llm": {"mode": "correct"}}', encoding="utf-8")
        config = Config(config_dir=config_home)
        assert config.get("llm.mode") == "correct"
        config.set("llm.mode", "translate")
        assert config.get("llm.mode") == "polish"
        config.set("llm.mode", "nonsense")
        assert config.get("llm.mode") == "polish"
        config.set("llm.mode", "correct")
        assert config.get("llm.mode") == "correct"


# ── app wiring ───────────────────────────────────────────────────────────────


LLM_OVERRIDES = {
    "llm.enabled": True,
    "llm.api_url": "https://api.example.test/v1",
    "llm.api_key": "key",
    "llm.model_name": "gpt-test",
}


class TestAppCorrectionMode:
    def test_final_result_applies_glossary_variants(self):
        from tests.helpers.app_harness import app_harness

        glossary = [{"term": "五证", "variants": ["五中"]}, {"term": "GitHub"}]
        with app_harness({"stt.glossary": glossary}) as h:
            app = h["app"]
            app._on_final_result("五中资料放在GITHUB")
            assert h["paster"].paste_async.call_args[0][0] == "五证资料放在GitHub"

    def test_correct_mode_sends_glossary_and_recent_context(self):
        from tests.helpers.app_harness import app_harness

        glossary = [{"term": "五证", "variants": ["五中"]}]
        with app_harness({**LLM_OVERRIDES, "llm.mode": "correct", "stt.glossary": glossary}) as h:
            app = h["app"]
            app._deliver_recognized_text("第一句")
            app._on_polish_complete("第一句")
            app._deliver_recognized_text("第二句")
            kwargs = h["polisher"].polish.call_args.kwargs
            assert kwargs["mode"] == "correct"
            assert kwargs["glossary"] == glossary
            assert kwargs["context"] == "第一句"

    def test_stale_output_is_not_context(self):
        from tests.helpers.app_harness import app_harness

        with app_harness({**LLM_OVERRIDES, "llm.mode": "correct"}) as h:
            app = h["app"]
            app._deliver_recognized_text("很久以前")
            app._on_polish_complete("很久以前")
            app._last_output_at -= 1000.0
            app._deliver_recognized_text("现在")
            assert h["polisher"].polish.call_args.kwargs["context"] == ""

    def test_polish_mode_keeps_its_own_arguments(self):
        from tests.helpers.app_harness import app_harness

        with app_harness(LLM_OVERRIDES) as h:
            app = h["app"]
            app._deliver_recognized_text("一句话")
            kwargs = h["polisher"].polish.call_args.kwargs
            assert kwargs["mode"] == "polish"

    def test_correction_reply_that_rewrites_falls_back_to_raw(self):
        from tests.helpers.app_harness import app_harness

        with app_harness({**LLM_OVERRIDES, "llm.mode": "correct"}) as h:
            app = h["app"]
            app._deliver_recognized_text("嗯那个这个方案还行吧")
            app._on_polish_complete("这个方案还行吧")
            assert h["paster"].paste_async.call_args[0][0] == "嗯那个这个方案还行吧"

    def test_correction_reply_with_a_homophone_fix_is_used(self):
        from tests.helpers.app_harness import app_harness

        with app_harness({**LLM_OVERRIDES, "llm.mode": "correct"}) as h:
            app = h["app"]
            app._deliver_recognized_text("今天增送一套")
            app._on_polish_complete("今天赠送一套")
            assert h["paster"].paste_async.call_args[0][0] == "今天赠送一套"

    def test_polish_mode_still_accepts_filler_removal(self):
        from tests.helpers.app_harness import app_harness

        with app_harness(LLM_OVERRIDES) as h:
            app = h["app"]
            app._deliver_recognized_text("嗯那个这个方案还行吧")
            app._on_polish_complete("这个方案还行。")
            assert h["paster"].paste_async.call_args[0][0] == "这个方案还行。"

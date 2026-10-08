"""Fidelity corpus for the polish guard: meaning changes fall back to raw text,
ordinary light edits keep the polished text."""

from __future__ import annotations

import pytest

from sayink.app import polish_looks_plausible, polish_rejection_reason

MEANING_CHANGED = [
    # negation dropped or flipped
    ("请不要删除文件。", "请删除文件。", "否定词丢失"),
    ("项目不能发布。", "项目可以发布。", "否定词丢失"),
    ("我还没有提交代码", "我已经提交代码了。", "否定词丢失"),
    ("千万别告诉他", "告诉他。", "否定词丢失"),
    ("I do not agree with this plan", "I agree with this plan.", "否定词丢失"),
    ("we can't ship today", "We can ship today.", "否定词丢失"),
    # numbers changed
    ("金额是100元。", "金额是900元。", "数字被改动"),
    ("会议定在3点半", "会议定在4点半。", "数字被改动"),
    ("版本号是2.0.8", "版本号是2.0.9。", "数字被改动"),
    ("预算大概1,200块", "预算大概12000块。", "数字被改动"),
    ("金额是100元。", "金额已经确认。", "数字被改动"),
    ("预算100元，备用金200元。", "预算100元。", "数字被改动"),
    ("今天新增0个错误。", "今天新增100个错误。", "数字被改动"),
    ("今天新增0个错误。", "今天新增错误。", "数字被改动"),
    ("预算100元。", "预算100元，备用金200元。", "数字被改动"),
    # proper nouns replaced or translated away
    ("把代码推到GitHub上", "把代码推到代码仓库上。", "英文专名丢失"),
    ("这个API返回了错误", "这个接口返回了错误。", "英文专名丢失"),
    ("用iPhone15录的音", "用苹果手机录的音。", "数字被改动"),
    # answering instead of editing
    ("今天开会", "好的！以下是关于开会的建议：" + "内容" * 60, "长度异常"),
]

LIGHT_EDITS = [
    ("嗯那个我觉得这个方案还行吧就是可能需要再优化一下", "我觉得这个方案还行，可能需要再优化一下。"),
    ("今天天气怎么样啊感觉还不错", "今天天气怎么样？感觉还不错。"),
    ("嗯这个不能这么做", "这个无法这么做。"),
    ("我们非常需要这个功能", "我们很需要这个功能。"),
    ("特别是识别率那一块", "尤其是识别率那一块。"),
    ("明天三点开会", "明天3点开会。"),
    ("一共是1000元", "一共是1,000元。"),
    ("3点到5点开会", "3:00到5:00开会。"),
    ("新增0个错误", "新增0个错误。"),
    ("价格是100.00元", "价格是100元。"),
    ("１００个用户", "100个用户。"),
    ("我用python写了个脚本", "我用 Python 写了个脚本。"),
    ("把代码推到github上", "把代码推到 GitHub 上。"),
    ("用iPhone15录的音", "用 iPhone 15 录的音。"),
    ("I don't think so", "I do not think so."),
    ("uh so the API is down", "So the API is down."),
    ("", "任何内容"),
]


@pytest.mark.parametrize("raw, polished, reason", MEANING_CHANGED)
def test_meaning_changes_fall_back_to_raw(raw, polished, reason):
    assert polish_rejection_reason(raw, polished) == reason
    assert polish_looks_plausible(raw, polished) is False


@pytest.mark.parametrize("raw, polished", LIGHT_EDITS)
def test_light_edits_keep_polished_text(raw, polished):
    assert polish_rejection_reason(raw, polished) == ""
    assert polish_looks_plausible(raw, polished) is True


@pytest.mark.parametrize("raw, polished", [
    ("请不要删除文件", "请删除文件。"),
    ("预算100元，备用金200元", "预算100元。"),
])
def test_app_outputs_raw_when_polish_changes_meaning(raw, polished):
    from tests.helpers.app_harness import app_harness

    overrides = {
        "llm.enabled": True,
        "llm.api_url": "https://api.example.test/v1",
        "llm.api_key": "key",
        "llm.model_name": "gpt-test",
    }
    with app_harness(overrides) as h:
        app = h["app"]
        app._deliver_recognized_text(raw)
        app._on_polish_complete(polished)
        assert h["paster"].paste_async.call_args[0][0] == raw

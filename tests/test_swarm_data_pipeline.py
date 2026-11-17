"""Regression tests for data-pipeline fixes:
1. Bounded streaming media import & bounded speaker reclustering (>600 turns + cancel)
2. Chinese/Arabic numeral & percent normalization in minutes verification + owner checks + chunked user notes fallback
3. Zero C901 per-file-ignores in ruff.toml & 9-model benchmark matrix coverage in docs/asr-benchmark.md
"""

from __future__ import annotations

import json
from pathlib import Path
import struct
from types import SimpleNamespace
import wave

import httpx
import numpy as np
import pytest

from sayink.audio_file_decoder import TimedAudioBlock, probe_media_duration_ms
from sayink.audio_utils import TARGET_SAMPLE_RATE
from sayink.media_import import (
    AudioChunk,
    iter_segment_blocks,
    iter_segment_timed_blocks,
    recognize_file,
    recognize_with_session,
)
from sayink.meeting_speakers import MeetingSpeakerSession
from sayink.minutes_generator import MinutesRequest, MinutesWorker, _concat_partials
from sayink.minutes_prompts import GENERAL_TEMPLATE_ID, MeetingContext, TranscriptLine, get_template
from sayink.minutes_render import UNVERIFIED_MARK, flag_unverified, render_markdown
from sayink.polish_fidelity import normalize_fact_lookup_text, polish_rejection_reason
from sayink.speaker_clustering import (
    MAX_UNBOUNDED_TURNS,
    TurnObservation,
    bounded_final_assignments,
    final_assignments,
)
from sayink.speaker_turns import RecognizedTurn
from sayink.speech_recognizer import MODEL_REGISTRY


def _unit_vec(*vals: float) -> np.ndarray:
    arr = np.array(vals, dtype=np.float32)
    return arr / np.linalg.norm(arr)


def _box(box_type: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), box_type) + payload


# ── 1. Bounded Streaming Media Import & Duration Probing ─────────────────────


def test_recognize_file_streams_chunks_with_bounded_lookahead_and_monotonic_progress(
    tmp_path, monkeypatch
):
    source = tmp_path / "multi_hour_sim.mp4"
    source.write_bytes(b"mp4-placeholder")

    total_utterances = 24
    tone_samples = TARGET_SAMPLE_RATE  # 1.0s speech
    gap_samples = TARGET_SAMPLE_RATE * 3  # 3.0s gap (>2.0s flushes each utterance cleanly)
    tone = (0.25 * np.sin(2.0 * np.pi * 440.0 * np.arange(tone_samples) / TARGET_SAMPLE_RATE)).astype(
        np.float32
    )

    stream_state = {"yielded_blocks": 0, "closed": False}

    def _lazy_timed_blocks(path, *, cancel=None, progress_cb=None):
        assert Path(path) == source
        try:
            for idx in range(total_utterances):
                if cancel is not None and cancel():
                    return
                start_sample = idx * (tone_samples + gap_samples)
                stream_state["yielded_blocks"] += 1
                if progress_cb is not None:
                    pct = int((idx + 1) * 100 // total_utterances)
                    progress_cb(min(99, pct), f"正在解码音频（{idx + 1} 秒）")
                yield TimedAudioBlock(
                    tone,
                    start_sample,
                    start_sample * 1000 // TARGET_SAMPLE_RATE,
                )
        finally:
            stream_state["closed"] = True

    monkeypatch.setattr("sayink.media_import.iter_media_pcm_timed", _lazy_timed_blocks)

    decode_observations: list[tuple[int, int]] = []

    class _StreamingCheckRecognizer:
        def decode_meeting_blocking(self, audio, *, turn_analyzer=None, cancelled=None, on_progress=None):
            chunk_num = len(decode_observations) + 1
            # Bounded streaming proof: when ASR runs on chunk 1, the 24-utterance
            # stream must NOT have been decoded to completion upfront!
            decode_observations.append((chunk_num, stream_state["yielded_blocks"]))
            if on_progress is not None:
                on_progress(len(audio) // 2, len(audio))
            return [RecognizedTurn(0, len(audio), f"第{chunk_num}句", np.ones(4, np.float32), True)], ""

    progress_events: list[tuple[int, str]] = []
    res = recognize_file(
        source,
        recognizer=_StreamingCheckRecognizer(),
        speaker_path=None,
        vad="energy",
        vad_path=None,
        cancel=lambda: False,
        progress_cb=lambda pct, msg: progress_events.append((int(pct), msg)),
    )

    assert stream_state["closed"] is True
    assert not res.cancelled
    assert len(res.turns) == total_utterances
    # When chunk 1 was recognized, at most window+1 blocks (~9) had been pulled, far below 24!
    first_chunk_idx, blocks_at_first_asr = decode_observations[0]
    assert first_chunk_idx == 1
    assert blocks_at_first_asr <= 10

    pcts = [p for p, _ in progress_events]
    assert pcts[0] == 0
    assert 30 in pcts
    assert pcts[-1] == 100
    assert all(a <= b for a, b in zip(pcts, pcts[1:], strict=False))
    # Once recognition started, no decode status message ("正在解码") may interleave!
    first_recog_pos = next(i for i, (_p, m) in enumerate(progress_events) if "正在识别" in m)
    assert all("正在解码" not in m for _p, m in progress_events[first_recog_pos:])


def test_recognize_file_streaming_cancel_stops_reading_remaining_stream(tmp_path, monkeypatch):
    source = tmp_path / "cancel_mid_stream.mkv"
    source.write_bytes(b"mkv")
    tone = np.full(TARGET_SAMPLE_RATE, 0.2, dtype=np.float32)
    pulled = {"n": 0}

    def _huge_stream(_path, *, cancel=None, progress_cb=None):
        for idx in range(200):
            if cancel is not None and cancel():
                return
            pulled["n"] += 1
            start = idx * TARGET_SAMPLE_RATE * 4
            yield TimedAudioBlock(tone, start, start * 1000 // TARGET_SAMPLE_RATE)

    monkeypatch.setattr("sayink.media_import.iter_media_pcm_timed", _huge_stream)
    decoded = {"n": 0}

    class _CancelAfterTwoRecognizer:
        def decode_meeting_blocking(self, audio, *, turn_analyzer=None, cancelled=None):
            decoded["n"] += 1
            return [RecognizedTurn(0, len(audio), f"保留{decoded['n']}", np.ones(4, np.float32), True)], ""

    res = recognize_file(
        source,
        recognizer=_CancelAfterTwoRecognizer(),
        speaker_path=None,
        vad="energy",
        vad_path=None,
        cancel=lambda: decoded["n"] >= 2,
    )
    assert res.cancelled is True
    assert [t.text for t in res.turns] == ["保留1", "保留2"]
    assert pulled["n"] <= 12  # Never read anywhere near 200 utterances!


def test_probe_media_duration_ms_handles_pcm_wav_riff_float_and_iso_bmff(tmp_path):
    # 1) Standard 16-bit PCM WAV (2.5s)
    wav16 = tmp_path / "pcm16.wav"
    with wave.open(str(wav16), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x00" * 40000)
    assert probe_media_duration_ms(wav16) == 2500

    # 2) Extended IEEE float32 RIFF WAV (fmt_tag=3, 3.0s @ 8000 Hz)
    wav_flt = tmp_path / "float32.wav"
    data_bytes = np.zeros(24000, dtype="<f4").tobytes()
    fmt_chunk = struct.pack("<4sIHHIIHH", b"fmt ", 16, 3, 1, 8000, 32000, 4, 32)
    data_chunk = struct.pack("<4sI", b"data", len(data_bytes)) + data_bytes
    riff_hdr = struct.pack("<4sI4s", b"RIFF", 4 + len(fmt_chunk) + len(data_chunk), b"WAVE")
    wav_flt.write_bytes(riff_hdr + fmt_chunk + data_chunk)
    assert probe_media_duration_ms(wav_flt) == 3000

    # 3) ISO-BMFF MP4 mvhd v0 & v1 header probe
    mp4_v0 = tmp_path / "probe_v0.mp4"
    ftyp = _box(b"ftyp", b"isom\x00\x00\x02\x00isom")
    mvhd_v0 = _box(b"mvhd", struct.pack(">B3sIIII", 0, b"\x00\x00\x00", 0, 0, 1000, 125_400))
    mp4_v0.write_bytes(ftyp + _box(b"moov", mvhd_v0))
    from sayink.media_foundation import _probe_iso_bmff_duration_ms

    assert _probe_iso_bmff_duration_ms(mp4_v0) == 125_400
    mp4_v1 = tmp_path / "probe_v1.mov"
    mvhd_v1 = _box(b"mvhd", struct.pack(">B3sQQIQ", 1, b"\x00\x00\x00", 0, 0, 48000, 48000 * 3600))
    mp4_v1.write_bytes(ftyp + _box(b"moov", mvhd_v1))
    assert _probe_iso_bmff_duration_ms(mp4_v1) == 3_600_000


def test_iter_segment_blocks_and_recognize_with_session_generator_preserve_partial_on_stream_error():
    audio = np.zeros(TARGET_SAMPLE_RATE * 2, dtype=np.float32)
    audio[4000:20000] = 0.25
    streamed_chunks = list(
        iter_segment_blocks([audio[:16000], audio[16000:]], vad="energy", vad_path=None)
    )
    assert len(streamed_chunks) == 1

    timed = list(
        iter_segment_timed_blocks(
            [TimedAudioBlock(audio, 8000, 500)], vad="energy", vad_path=None
        )
    )
    assert len(timed) == 1 and timed[0].start >= 8000

    def _failing_chunk_generator():
        yield AudioChunk(0, 16000, np.ones(16000, dtype=np.float32))
        raise RuntimeError("容器尾部损坏")

    res = recognize_with_session(
        _failing_chunk_generator(),
        lambda chunk: [RecognizedTurn(0, len(chunk.audio), "首段已识别", np.ones(4, np.float32), True)],
        SimpleNamespace(assign=None),
        threshold=0.55,
        cancel=lambda: False,
    )
    assert not res.cancelled
    assert res.error == "容器尾部损坏"
    assert [t.text for t in res.turns] == ["首段已识别"]


# ── 2. Bounded Speaker Reclustering (>600 turns + cancel) ───────────────────


def test_bounded_final_assignments_scales_past_600_turns_and_respects_cancel():
    va = _unit_vec(1.0, 0.0, 0.0, 0.0)
    va_noisy = _unit_vec(0.98, 0.15, 0.0, 0.0)
    vb = _unit_vec(0.0, 1.0, 0.0, 0.0)
    vb_noisy = _unit_vec(0.12, 0.99, 0.0, 0.0)

    # Simulate 820 turns (> MAX_UNBOUNDED_TURNS = 600) where voice A was split
    # across online IDs 1 and 3, voice B has ID 2, and turn 10 is manually locked to 7.
    turns: list[TurnObservation] = []
    for k in range(820):
        if k == 10:
            turns.append(TurnObservation(k, 7, va, 3000, True))
        elif k % 3 == 0:
            turns.append(TurnObservation(k, 1, va, 3000, True))
        elif k % 3 == 1:
            turns.append(TurnObservation(k, 2, vb if k % 2 == 0 else vb_noisy, 3000, True))
        else:
            turns.append(TurnObservation(k, 3, va_noisy, 3000, True))

    session = SimpleNamespace(turns=turns, confirmed={10: 7}, speaker_count=3)
    assert len(turns) > MAX_UNBOUNDED_TURNS

    # Cancelled call returns {} immediately without clustering.
    assert bounded_final_assignments(session, 0.65, cancel=lambda: True) == {}

    mapping = bounded_final_assignments(
        session, 0.65, protected_ids={7}, locked_keys={10}, cancel=lambda: False
    )
    assert len(mapping) == 820
    assert mapping[10] == 7
    # Voice A (keys % 3 in {0, 2}, except locked key 10 if different) folds into one speaker ID,
    # distinct from Voice B (keys % 3 == 1).
    voice_a_ids = {mapping[k] for k in range(820) if k != 10 and k % 3 in (0, 2)}
    voice_b_ids = {mapping[k] for k in range(820) if k != 10 and k % 3 == 1}
    assert len(voice_a_ids) == 1
    assert len(voice_b_ids) == 1
    assert voice_a_ids != voice_b_ids

    # Below the 600-turn ceiling, bounded_final_assignments matches final_assignments identically.
    small_session = SimpleNamespace(turns=turns[:60], confirmed={10: 7}, speaker_count=3)
    assert bounded_final_assignments(
        small_session, 0.65, protected_ids={7}, locked_keys={10}
    ) == final_assignments(small_session, 0.65, protected_ids={7}, locked_keys={10})


def test_recognize_with_session_uses_bounded_reclustering_by_default():
    va = _unit_vec(1.0, 0.0, 0.0, 0.0)
    vb = _unit_vec(0.0, 1.0, 0.0, 0.0)
    session = MeetingSpeakerSession(threshold=0.60, keep_history=True)

    chunks = [AudioChunk(i * 16000, (i + 1) * 16000, np.zeros(16, dtype=np.float32)) for i in range(650)]

    def _decode(chunk):
        idx = chunk.start // 16000
        vec = va if idx % 2 == 0 else vb
        return [RecognizedTurn(0, 16000, f"turn-{idx}", vec, True)]

    res = recognize_with_session(
        chunks,
        _decode,
        session,
        threshold=0.60,
        cancel=lambda: False,
    )
    assert not res.cancelled
    assert len(res.turns) == 650
    even_speakers = {res.turns[i].speaker_id for i in range(0, 650, 2)}
    odd_speakers = {res.turns[i].speaker_id for i in range(1, 650, 2)}
    assert even_speakers == {1}
    assert odd_speakers == {2}


# ── 3. Minutes Verification (Chinese/Arabic Numerals, Owners & User Notes) ──


@pytest.mark.parametrize(
    "claim, transcript",
    [
        ("采购预算定为 2500元", "发言人：这批设备预算两千五百元整。"),
        ("采购预算定为 2,500元", "发言人：这批设备两千五百块钱搞定。"),
        ("转化率提升 30%", "发言人：预计转化率能提升百分之三十。"),
        ("手续费率控制在 3.5%", "发言人：费率控制在百分之三点五以内。"),
        ("留存率提升 1%", "发言人：哪怕提升百分之一也很可观。"),
        ("核心用例覆盖率达到 100%", "发言人：上线前核心用例必须百分之百覆盖。"),
        ("正式发版定在 3月5日", "发言人：我们定在三月五日晚上发版。"),
        ("灰度截止 3月5日", "发言人：三月五号之前必须完成灰度。"),
        ("元旦值班安排在 1月1日", "发言人：一月一日由运维组值班。"),
        ("里程碑定于 2026-03-05", "发言人：二零二六年三月五日正式验收。"),
        ("周三完成评审", "发言人：那就定在星期三下午评审。"),
        ("周日执行停机迁移", "发言人：礼拜天凌晨执行迁移。"),
        ("推广预算增加 20万", "发言人：推广这边再加二十万预算。"),
    ],
)
def test_flag_unverified_accepts_equivalent_chinese_and_arabic_facts(claim, transcript):
    assert flag_unverified(claim, transcript) == claim
    assert polish_rejection_reason("预算两千五百元", "预算2500元。") == ""
    assert normalize_fact_lookup_text("两千五百元") == normalize_fact_lookup_text("2500元")


@pytest.mark.parametrize(
    "invented_claim, transcript",
    [
        ("采购预算定为 3500元", "发言人：这批设备预算两千五百元。"),
        ("采购预算定为 500元", "发言人：这批设备预算两千五百元。"),   # Substring trap: 500 inside 2500
        ("转化率提升 30%", "发言人：去年增长了百分之一百三十。"),       # Substring trap: 30% inside 130%
        ("推广预算增加 20万", "发言人：全年总盘子是二百万。"),          # Trailing-digit trap: 200000 inside 2000000
        ("发版定在 2月5日", "发言人：去年十二月五日发过一次。"),        # Substring trap: 2月5日 inside 12月5日
        ("发版定在 3月6日", "发言人：我们定在三月五日发版。"),
        ("里程碑定于 2027-03-05", "发言人：三月五日正式验收。"),
        ("周四完成评审", "发言人：星期三下午完成评审。"),
    ],
)
def test_flag_unverified_flags_invented_and_substring_trap_numbers(invented_claim, transcript):
    assert flag_unverified(invented_claim, transcript) == invented_claim + UNVERIFIED_MARK


def test_render_markdown_flags_hallucinated_action_item_owners_while_allowing_valid_sources():
    transcript = "[00:00:10] 说话人1：请王五协助准备压测脚本。\n[00:00:20] 说话人2：收到，我来跟进。"
    payload = {
        "title": "发布准备会",
        "summary": "明确压测与发布分工。",
        "decisions": [{"text": "预算控制在 2500元，提升 30% 吞吐", "ts": "00:00:10"}],
        "action_items": [
            {"task": "跟进线上灰度", "owner": "李四", "due": "3月5日", "ts": "00:00:20"},
            {"task": "准备压测脚本", "owner": "王五", "due": "未提及", "ts": "00:00:10"},
            {"task": "法务合规签字", "owner": "赵敏", "due": "", "ts": ""},
            {"task": "整理会议录音", "owner": "说话人1", "due": "", "ts": ""},
            {"task": "全员回归验证", "owner": "全体", "due": "", "ts": ""},
            {"task": "排期确认", "owner": "待定", "due": "", "ts": ""},
            {"task": "海外渠道投放", "owner": "孙七", "due": "", "ts": ""},
            {"task": "幽灵编号跟进", "owner": "说话人9", "due": "", "ts": ""},
        ],
    }
    full_transcript = (
        transcript
        + "\n[00:00:30] 说话人1：预算两千五百元，吞吐提升百分之三十，三月五日完成。"
    )
    md = render_markdown(
        payload,
        template=get_template(GENERAL_TEMPLATE_ID),
        duration_ms=120_000,
        transcript_text=full_transcript,
        speaker_names={2: "李四"},
        meta_lines=["时间：2026-11-17 10:00", "参会人：张三、李四、赵敏"],
    )
    # Chinese/Arabic normalized decision & due date pass cleanly without 待核对:
    assert "- 预算控制在 2500元，提升 30% 吞吐 [00:00:10]\n" in md
    assert "- [ ] 跟进线上灰度 — @李四 · 3月5日 [00:00:20]" in md
    # Mentioned in transcript ("王五"), in meta_lines 参会人 ("赵敏"), valid 说话人1, and placeholders pass:
    assert "- [ ] 准备压测脚本 — @王五 · 未提及 [00:00:10]" in md
    assert "- [ ] 法务合规签字 — @赵敏 · 未提及" in md
    assert "- [ ] 整理会议录音 — @说话人1 · 未提及" in md
    assert "- [ ] 全员回归验证 — @全体 · 未提及" in md
    assert "- [ ] 排期确认 — @待定 · 未提及" in md
    # Hallucinated person ("孙七") and absent speaker label ("说话人9") are flagged 待核对:
    assert f"- [ ] 海外渠道投放 — @孙七{UNVERIFIED_MARK} · 未提及" in md
    assert f"- [ ] 幽灵编号跟进 — @说话人9{UNVERIFIED_MARK} · 未提及" in md


def test_chunked_merge_concat_fallback_preserves_uncaptured_user_notes_without_duplication(
    monkeypatch,
):
    lines = [TranscriptLine(i * 2000, 1 + (i % 2), "讨论架构改造细节。" * 8) for i in range(30)]
    calls: list[str] = []

    def _mock_transport(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        user_msg = body["messages"][1]["content"]
        calls.append(user_msg)
        if "第 1/" in user_msg:
            # Chunk 1 already captures the early mark, so fallback must NOT duplicate it!
            reply = json.dumps({"summary": "第一部分摘要", "topics": [{"title": "架构", "points": [{"text": "确认核心网关限流阈值", "ts": "00:00:04"}]}]})
        elif "段）：" in user_msg:
            reply = json.dumps({"summary": "后续分段摘要"})
        else:
            # Merge fails validation -> triggers _concat_partials fallback!
            reply = "合并服务暂时返回了非结构化文本"
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
    monkeypatch.setattr("sayink.minutes_generator._RETRY_DELAY_SEC", 0.0)

    context = MeetingContext(
        title="网关评审",
        notes_user="务必补充跨机房容灾演练结论\n跟进法务合同条款",
        marks=[(4_000, "确认核心网关限流阈值"), (50_000, "王工提示检查连接池上限")],
    )

    for tpl_id in ("general", "standup"):
        req = MinutesRequest(
            api_url="http://localhost:11434/v1",
            api_key="",
            model_name="local-llm",
            template=get_template(tpl_id),
            context=context,
            lines=lines,
            duration_ms=90_000,
            chunk_chars=500,
        )
        worker = MinutesWorker(req)
        results, errors = [], []
        worker.finished_ok.connect(results.append)
        worker.error.connect(errors.append)
        worker.run()
        assert not errors and len(results) == 1
        md = results[0].markdown
        # Uncovered user notes & uncovered mark are preserved in final output:
        assert "务必补充跨机房容灾演练结论" in md
        assert "跟进法务合同条款" in md
        assert "王工提示检查连接池上限" in md
        # Already-captured mark in Chunk 1 is NOT duplicated in the fallback block:
        if tpl_id == "general":
            assert md.count("确认核心网关限流阈值") == 1

    # Direct _concat_partials check when merge groups exceed token window (len(groups) == len(partials))
    direct = _concat_partials(
        [{"summary": "分段A"}, {"summary": "分段B"}],
        context=context,
        template=get_template("general"),
    )
    topic_titles = [t["title"] for t in direct["topics"]]
    assert "用户笔记与标记" in topic_titles


# ── 4. Ruff Complexity Ceiling & Benchmark Matrix Documentation ──────────────


def test_ruff_toml_has_zero_per_file_ignores_and_benchmark_doc_covers_all_9_models():
    repo_root = Path(__file__).resolve().parents[1]
    ruff_toml = (repo_root / "ruff.toml").read_text(encoding="utf-8")
    assert "per-file-ignores" not in ruff_toml
    assert "max-complexity = 30" in ruff_toml

    bench_doc = (repo_root / "docs" / "asr-benchmark.md").read_text(encoding="utf-8")
    assert len(MODEL_REGISTRY) == 9
    for entry in MODEL_REGISTRY:
        assert entry["id"] in bench_doc, f"Missing model id {entry['id']} in docs/asr-benchmark.md"
    assert "lane_asr_eval.py" in bench_doc
    assert "meeting_timeline_bench.py" in bench_doc
    assert "待实测" in bench_doc

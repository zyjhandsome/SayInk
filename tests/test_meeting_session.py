"""MeetingController: lifecycle, marks, recluster on finalize, speaker rename,
minutes generation gating, remote-host confirmation; plus the MeetingPage
smoke under the offscreen platform."""

from __future__ import annotations

import json
import time

import numpy as np
import pytest
from PyQt6.QtCore import QCoreApplication
from PyQt6.QtWidgets import QLabel

from sayink.config import Config
from sayink.history_store import HistoryStore, SegmentRecord
from sayink.meeting_session import (
    MeetingCapabilities,
    MeetingController,
    MeetingOptions,
    MeetingState,
    minutes_settings,
    resolve_capabilities,
    safe_filename,
)
from sayink.meeting_store import STATUS_ENDED, STATUS_INTERRUPTED
from sayink.vad_segmenter import VAD_ENERGY, VAD_SILERO


def _pump(ms: int = 50) -> None:
    deadline = time.monotonic() + ms / 1000
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.005)


@pytest.fixture
def config(tmp_path):
    return Config(config_dir=tmp_path / "cfg")


@pytest.fixture
def history(tmp_path):
    store = HistoryStore(db_path=tmp_path / "history.db")
    yield store
    store.close(timeout=2.0)


@pytest.fixture
def controller(config, history):
    pending = {"n": 0}
    ctrl = MeetingController(config, history, pending_segments=lambda: pending["n"])
    ctrl._test_pending = pending
    yield ctrl
    ctrl.shutdown()


def _caps(**over) -> MeetingCapabilities:
    base = dict(
        model_id="funasr-nano", hotwords="", hotwords_supported=True, language="中文",
        language_supported=True, vad=VAD_ENERGY, speaker_model_available=False,
    )
    base.update(over)
    return MeetingCapabilities(**base)


def _row(meeting_id: str, seq: int, text: str, *, speaker_id: int, started_at: int) -> SegmentRecord:
    return SegmentRecord(
        session_id=meeting_id, seq=seq, created_at=started_at + (seq + 1) * 3000,
        raw_text=text, source="mixed", duration_ms=2000, trigger_mode="meeting",
        model="funasr-nano", speaker_id=speaker_id, captured_at=started_at + seq * 3000,
    )


def _result(title: str, template_id: str = "general"):
    from sayink.minutes_generator import MinutesResult

    return MinutesResult(json_payload='{"summary": "ok"}', markdown=f"# {title}\n\nok", chunked=False,
                         duration_ms=1000, model="m", template_id=template_id)


LAN_URL = "http://192.168.1.10:8000/v1"
LONG_TEXT = "这一段是足够长的会议内容，用来触发会后自动生成纪要。" * 12


class TestCapabilities:
    def test_energy_fallback_when_silero_missing(self, config, monkeypatch):
        monkeypatch.setattr("sayink.meeting_session.aux_model_path", lambda _mid: None)
        caps = resolve_capabilities(config, MeetingOptions(vad=VAD_SILERO), dictation_model_id="funasr-nano")
        assert caps.vad == VAD_ENERGY
        assert caps.speaker_model_available is False
        assert any("Silero" in n for n in caps.notes)
        assert any("说话人" in n for n in caps.notes)

    def test_model_defaults_to_dictation_and_language_defaults_to_zh(self, config, monkeypatch):
        monkeypatch.setattr("sayink.meeting_session.aux_model_path", lambda _mid: None)
        caps = resolve_capabilities(config, MeetingOptions(), dictation_model_id="qwen3-asr-0.6b")
        assert caps.model_id == "qwen3-asr-0.6b"
        assert caps.language == "zh"
        assert caps.hotwords_supported is True
        assert caps.language_supported is False

    def test_hotwords_note_when_model_cannot_use_them(self, config, monkeypatch):
        monkeypatch.setattr("sayink.meeting_session.aux_model_path", lambda _mid: None)
        opts = MeetingOptions(glossary=[{"term": "SayInk"}])
        caps = resolve_capabilities(config, opts, dictation_model_id="sensevoice")
        assert caps.hotwords == "SayInk"
        assert not caps.hotwords_supported
        assert any("热词" in n for n in caps.notes)

    def test_safe_filename_strips_path_characters(self):
        assert safe_filename('Q3 复盘: a/b*c?') == "Q3 复盘 a b c"
        assert safe_filename("") == "会议纪要"

    def test_minutes_settings_prefer_meeting_model(self, config):
        config.set("llm.api_url", "http://localhost:1234/v1")
        config.set("llm.api_key", "k")
        config.set("llm.model_name", "polish-model")
        assert minutes_settings(config)[2] == "polish-model"
        config.set("meeting.model_name", "big-model")
        assert minutes_settings(config)[2] == "big-model"


class TestLifecycle:
    def test_begin_creates_live_record_and_speaker_session(self, controller):
        states = []
        controller.state_changed.connect(states.append)
        record = controller.begin(MeetingOptions(title="周会", input_source="mixed"), _caps())
        assert controller.is_active
        assert controller.state is MeetingState.LIVE
        assert controller.current_id == record.id
        assert controller.speakers is not None
        assert controller.embedder is not None and controller.embedder.name == "mel"
        stored = controller.store.get_meeting(record.id)
        assert stored is not None and stored.title == "周会" and stored.status == "live"
        assert states == [MeetingState.LIVE]

    def test_speaker_load_failure_is_distinct_from_a_missing_download(self, controller, monkeypatch):
        from sayink.speaker_embedding import MelEmbedder

        monkeypatch.setattr("sayink.meeting_session.make_embedder", lambda *_args, **_kw: MelEmbedder())
        controller.begin(MeetingOptions(), _caps(speaker_model_available=True))
        assert not controller.capabilities.speaker_model_available
        assert controller.capabilities.speaker_load_failed
        assert any("CAM++ 加载失败" in note for note in controller.capabilities.notes)

    def test_second_begin_refused_while_active(self, controller):
        controller.begin(MeetingOptions(), _caps())
        with pytest.raises(RuntimeError):
            controller.begin(MeetingOptions(), _caps())

    def test_pause_resume(self, controller):
        controller.begin(MeetingOptions(), _caps())
        controller.pause("设备中断")
        assert controller.state is MeetingState.PAUSED
        assert controller.paused_detail == "设备中断"
        controller.resume()
        assert controller.state is MeetingState.LIVE
        assert controller.paused_detail == ""

    def test_pause_updates_detail_while_already_paused(self, controller):
        controller.begin(MeetingOptions(), _caps())
        seen = []
        controller.state_changed.connect(seen.append)
        controller.pause("识别跟不上，已暂停采集 · 处理完会自动继续")
        controller.pause("音频设备打开失败 · 请检查设备后在会议页「继续采集」")
        assert controller.state is MeetingState.PAUSED
        assert controller.paused_detail == "音频设备打开失败 · 请检查设备后在会议页「继续采集」"
        assert seen[-1] is MeetingState.PAUSED

    def test_add_mark_records_offset_and_notice(self, controller):
        record = controller.begin(MeetingOptions(), _caps())
        notices = []
        controller.notice.connect(lambda t, d: notices.append((t, d)))
        offset = controller.add_mark("重点")
        assert offset is not None and offset >= 0
        controller.store.flush()
        marks = controller.store.get_marks(record.id)
        assert [m.note for m in marks] == ["重点"]
        assert notices and notices[0][0] == "已标记"

    def test_request_end_waits_for_pending_segments(self, controller):
        record = controller.begin(MeetingOptions(), _caps())
        ended = []
        controller.meeting_ended.connect(ended.append)
        controller._test_pending["n"] = 2
        controller.request_end()
        assert controller.state is MeetingState.DRAINING
        _pump(400)
        assert not ended
        controller._test_pending["n"] = 0
        _pump(500)
        assert ended == [record.id]
        assert controller.state is MeetingState.IDLE
        assert not controller.is_active
        stored = controller.store.get_meeting(record.id)
        assert stored is not None and stored.status == STATUS_ENDED and stored.ended_at

    def test_finalize_reclusters_speakers_and_drops_embeddings(self, controller, history):
        record = controller.begin(MeetingOptions(), _caps())
        speakers = controller.speakers
        base_a = np.zeros(24, dtype=np.float32)
        base_a[0] = 1.0
        base_b = np.zeros(24, dtype=np.float32)
        base_b[1] = 1.0
        # Speaker A twice, then a near-duplicate of A labelled as a new speaker
        # because online matching saw it before A's centroid settled.
        ids = [speakers.assign(base_a, ""), speakers.assign(base_b, ""), speakers.assign(base_a * 0.999, "")]
        for seq, sid in enumerate(ids):
            history.enqueue(_row(record.id, seq, f"t{seq}", speaker_id=sid, started_at=record.started_at))
        history.flush(timeout=2.0)
        controller.finalize()
        assert controller.speakers is None and controller.embedder is None
        rows = controller.store.get_segments(record.id)
        assert rows[0].speaker_id == rows[2].speaker_id
        assert rows[1].speaker_id != rows[0].speaker_id

    def test_abandon_on_exit_closes_record(self, controller):
        record = controller.begin(MeetingOptions(), _caps())
        controller.abandon_on_exit()
        assert not controller.is_active
        stored = controller.store.get_meeting(record.id)
        assert stored is not None and stored.ended_at
        assert stored.status == STATUS_INTERRUPTED

    def test_mark_interrupted_flags_stale_live_meetings(self, controller, history):
        record = controller.store.create_meeting(title="崩溃前")
        controller.store.flush()
        controller.mark_interrupted()
        controller.store.flush()
        stored = controller.store.get_meeting(record.id)
        assert stored is not None and stored.status == STATUS_INTERRUPTED

    def test_meeting_changed_emitted_on_history_commit(self, controller, history):
        record = controller.begin(MeetingOptions(), _caps())
        changed = []
        controller.meeting_changed.connect(changed.append)
        history.enqueue(_row(record.id, 0, "hi", speaker_id=1, started_at=record.started_at))
        history.flush(timeout=2.0)
        _pump(100)
        assert record.id in changed


class TestRenameAndExport:
    def _ended_meeting(self, controller, history, *, notes_json=None, markdown="", user_edited=False):
        record = controller.store.create_meeting(title="评审")
        for seq, (sid, text) in enumerate([(1, "方案 A 通过"), (2, "下周上线")]):
            history.enqueue(_row(record.id, seq, text, speaker_id=sid, started_at=record.started_at))
        controller.store.end_meeting(record.id, ended_at=record.started_at + 60_000)
        if markdown or notes_json:
            controller.store.save_notes(
                record.id, template_id="general", model="m",
                json_payload=json.dumps(notes_json, ensure_ascii=False) if notes_json else "",
                markdown=markdown, duration_ms=60_000, chunked=False,
            )
            if user_edited:
                controller.store.save_user_markdown(record.id, markdown)
        controller.store.flush()
        return record

    def test_rename_rerenders_json_notes(self, controller, history):
        payload = {
            "summary": "说话人1 说方案 A 通过。",
            "key_points": [{"text": "方案 A 通过", "ts": "00:00:00", "speaker_id": 1}],
            "decisions": [], "action_items": [], "questions": [], "timeline": [], "speaker_view": [],
        }
        record = self._ended_meeting(controller, history, notes_json=payload, markdown="# 评审\n说话人1 说方案 A 通过。")
        replaced = controller.rename_speaker(record.id, 1, "张三")
        assert replaced == 0
        notes = controller.store.get_notes(record.id)
        assert notes is not None and "张三" in notes.markdown and "说话人1" not in notes.markdown
        assert controller.speaker_names(record.id) == {1: "张三"}

    def test_rename_replaces_in_user_edited_notes(self, controller, history):
        record = self._ended_meeting(controller, history, markdown="说话人2 负责上线。\n说话人21 不是同一人。", user_edited=True)
        replaced = controller.rename_speaker(record.id, 2, "李四")
        assert replaced == 1
        notes = controller.store.get_notes(record.id)
        assert notes is not None
        assert notes.markdown.startswith("李四 负责上线。")
        assert "说话人21" in notes.markdown

    def test_rename_speaker_does_not_touch_notes_while_generating(self, controller, history):
        record = self._ended_meeting(
            controller, history, markdown="说话人2 负责上线。", user_edited=True,
        )
        controller._generating_for = record.id
        assert controller.rename_speaker(record.id, 2, "李四") is None
        assert controller.speaker_names(record.id) == {}
        notes = controller.store.get_notes(record.id)
        assert notes is not None and notes.markdown == "说话人2 负责上线。"
        assert notes.user_edited

    def test_export_includes_transcript_with_names(self, controller, history):
        record = self._ended_meeting(controller, history, markdown="# 评审\n\n## 摘要\n通过。")
        controller.rename_speaker(record.id, 1, "张三")
        text = controller.export_markdown(record.id)
        assert text.startswith("# 评审")
        assert "## 文字稿" in text
        assert "张三：方案 A 通过" in text
        assert "说话人2：下周上线" in text
        bare = controller.export_markdown(record.id, include_transcript=False)
        assert "## 文字稿" not in bare

    def test_export_without_notes_still_has_header(self, controller, history):
        record = self._ended_meeting(controller, history)
        text = controller.export_markdown(record.id)
        assert text.startswith("# 评审")
        assert "时长：00:01:00" in text

    def test_delete_meeting_removes_everything(self, controller, history):
        record = self._ended_meeting(controller, history, markdown="x")
        controller.delete_meeting(record.id)
        assert controller.store.get_meeting(record.id) is None
        assert controller.store.get_segments(record.id) == []
        assert controller.store.get_notes(record.id) is None


class TestGeneration:
    def test_generate_refuses_without_settings(self, controller, history):
        record = controller.store.create_meeting()
        history.enqueue(_row(record.id, 0, "hi", speaker_id=1, started_at=record.started_at))
        controller.store.end_meeting(record.id)
        controller.store.flush()
        failures = []
        controller.generation_failed.connect(lambda mid, msg: failures.append((mid, msg)))
        assert controller.generate(record.id) is False
        assert failures and "未配置" in failures[0][1]

    def test_generate_refuses_empty_transcript(self, controller, config):
        config.set("llm.api_url", "http://localhost:1234/v1")
        config.set("llm.api_key", "k")
        config.set("llm.model_name", "m")
        record = controller.store.create_meeting()
        controller.store.end_meeting(record.id)
        controller.store.flush()
        failures = []
        controller.generation_failed.connect(lambda mid, msg: failures.append(msg))
        assert controller.generate(record.id) is False
        assert failures == ["文字稿为空，无法生成纪要"]

    def test_generate_dispatches_request_and_saves_result(self, controller, config, history, monkeypatch):
        config.set("llm.api_url", "http://localhost:1234/v1")
        config.set("llm.api_key", "k")
        config.set("llm.model_name", "m")
        config.set("meeting.template_id", "standup")
        record = controller.store.create_meeting(title="站会")
        history.enqueue(_row(record.id, 0, "昨天修了登录", speaker_id=1, started_at=record.started_at))
        controller.store.end_meeting(record.id, ended_at=record.started_at + 30_000)
        controller.store.flush()
        seen = {}

        def fake_generate(request):
            seen["request"] = request

        monkeypatch.setattr(controller._generator, "generate", fake_generate)
        assert controller.generate(record.id) is True
        assert controller.generate(record.id) is False
        assert controller.generating_for == record.id
        req = seen["request"]
        assert req.template.id == "standup"
        assert req.model_name == "m"
        assert [line.text for line in req.lines] == ["昨天修了登录"]
        assert req.chunk_chars == 8_000
        assert controller.generating_for == record.id

        from sayink.minutes_generator import MinutesResult

        done = []
        controller.generation_finished.connect(done.append)
        controller._on_generation_done(MinutesResult(
            json_payload='{"summary": "ok"}', markdown="# 站会\n\nok", chunked=False,
            duration_ms=1200, model="m", template_id="standup",
        ))
        assert done == [record.id]
        notes = controller.store.get_notes(record.id)
        assert notes is not None and notes.markdown.startswith("# 站会") and notes.template_id == "standup"
        assert controller.generating_for == ""

    def test_second_meeting_queues_behind_the_first_and_runs_next(
        self, controller, config, history, monkeypatch,
    ):
        config.set("llm.api_url", "http://localhost:1234/v1")
        config.set("llm.api_key", "k")
        config.set("llm.model_name", "m")
        first = controller.store.create_meeting(title="第一场")
        second = controller.store.create_meeting(title="第二场")
        history.enqueue(_row(first.id, 0, "第一场内容", speaker_id=1, started_at=first.started_at))
        history.enqueue(_row(second.id, 0, "第二场内容", speaker_id=1, started_at=second.started_at))
        controller.store.end_meeting(first.id, ended_at=first.started_at + 1000)
        controller.store.end_meeting(second.id, ended_at=second.started_at + 1000)
        controller.store.flush()
        calls = []
        monkeypatch.setattr(controller._generator, "generate", calls.append)
        failures, queued = [], []
        controller.generation_failed.connect(lambda mid, msg: failures.append((mid, msg)))
        controller.generation_queued.connect(queued.append)

        assert controller.generate(first.id) is True
        assert controller.generate(second.id) is True
        assert len(calls) == 1 and controller.generating_for == first.id
        assert failures == [] and queued == [second.id]
        assert controller.queue_position(second.id) == 1

        controller._on_generation_done(_result("第一场"))
        assert len(calls) == 2 and [line.text for line in calls[1].lines] == ["第二场内容"]
        assert controller.generating_for == second.id
        assert controller.store.queued_generation(first.id) is None

    def test_generate_refuses_a_live_meeting(self, controller, config, history, monkeypatch):
        config.set("llm.api_url", "http://localhost:1234/v1")
        config.set("llm.api_key", "k")
        config.set("llm.model_name", "m")
        record = controller.begin(MeetingOptions(), _caps())
        history.enqueue(_row(record.id, 0, "还在说", speaker_id=1, started_at=record.started_at))
        history.flush(timeout=2.0)
        calls = []
        monkeypatch.setattr(controller._generator, "generate", calls.append)
        failures = []
        controller.generation_failed.connect(lambda mid, msg: failures.append((mid, msg)))

        assert controller.generate(record.id) is False
        assert calls == []
        assert controller.generating_for == ""
        assert failures and failures[0][0] == record.id
        assert "进行" in failures[0][1]

    def test_remote_host_confirmation_is_remembered(self, controller, config):
        config.set("llm.api_url", "https://api.example.com/v1")
        assert controller.remote_host_needing_confirmation() == "api.example.com"
        controller.confirm_remote_host("api.example.com")
        assert controller.remote_host_needing_confirmation() == ""
        config.set("llm.api_url", "http://127.0.0.1:11434/v1")
        assert controller.remote_host_needing_confirmation() == ""

    def test_cancel_generation_is_not_reported_as_a_failure(self, controller):
        controller._generating_for = "abc"
        failures, cancelled = [], []
        controller.generation_failed.connect(lambda mid, msg: failures.append((mid, msg)))
        controller.generation_cancelled.connect(cancelled.append)
        controller.cancel_generation()
        assert failures == []
        assert cancelled == ["abc"]
        assert controller.generating_for == ""


class TestGenerationQueue:
    """Automatic end-of-meeting minutes and the one-at-a-time queue."""

    @pytest.fixture
    def calls(self, controller, monkeypatch):
        calls = []
        monkeypatch.setattr(controller._generator, "generate", calls.append)
        return calls

    @staticmethod
    def _configure(config, *, url=LAN_URL, auto=True, template="general"):
        config.set("llm.api_url", url)
        config.set("llm.api_key", "k")
        config.set("llm.model_name", "m")
        config.set("meeting.auto_generate", auto)
        config.set("meeting.template_id", template)

    @staticmethod
    def _ended(controller, history, title, text=LONG_TEXT):
        record = controller.store.create_meeting(title=title)
        history.enqueue(_row(record.id, 0, text, speaker_id=1, started_at=record.started_at))
        controller.store.end_meeting(record.id, ended_at=record.started_at + 120_000)
        controller.store.flush()
        return record

    @staticmethod
    def _live_meeting_ends(controller, history, text=LONG_TEXT):
        record = controller.begin(MeetingOptions(), _caps())
        history.enqueue(_row(record.id, 0, text, speaker_id=1, started_at=record.started_at))
        history.flush(timeout=2.0)
        controller.finalize()
        return record

    @staticmethod
    def _enqueue_auto(controller, record, template="general"):
        from sayink.meeting_store import QUEUE_PRIORITY_AUTO

        controller.store.enqueue_generation(record.id, template, priority=QUEUE_PRIORITY_AUTO)
        controller.store.flush()

    def test_meeting_end_queues_default_template_and_starts_it(self, controller, config, history, calls):
        self._configure(config, template="standup")
        queued = []
        controller.generation_queued.connect(queued.append)
        record = self._live_meeting_ends(controller, history)
        assert queued == [record.id]
        assert len(calls) == 1 and calls[0].template.id == "standup"
        assert controller.generating_for == record.id
        job = controller.store.queued_generation(record.id)
        assert job is not None and job.automatic
        controller._on_generation_done(_result("会议", "standup"))
        assert controller.store.queued_generation(record.id) is None
        assert controller.store.get_notes(record.id).template_id == "standup"

    @pytest.mark.parametrize("auto, model, text", [
        (False, "m", LONG_TEXT),       # switch off (the default)
        (True, "", LONG_TEXT),         # AI service not configured
        (True, "m", "就一句话。"),      # too short to be worth a request
    ])
    def test_meeting_end_does_not_queue_without_switch_service_or_content(
        self, controller, config, history, calls, auto, model, text,
    ):
        self._configure(config, auto=auto)
        config.set("llm.model_name", model)
        record = self._live_meeting_ends(controller, history, text=text)
        assert calls == [] and controller.store.queued_generation(record.id) is None

    def test_auto_generation_is_off_by_default(self, config):
        assert config.get("meeting.auto_generate") is False

    def test_back_to_back_meetings_are_generated_in_order(self, controller, config, history, calls):
        self._configure(config)
        first = self._live_meeting_ends(controller, history)
        second = self._live_meeting_ends(controller, history)
        assert len(calls) == 1 and controller.generating_for == first.id
        assert controller.queue_position(second.id) == 1
        controller._on_generation_done(_result("第一场"))
        assert len(calls) == 2 and controller.generating_for == second.id
        controller._on_generation_done(_result("第二场"))
        assert controller.generating_for == "" and controller.store.generation_queue() == []
        assert controller.store.get_notes(first.id) is not None and controller.store.get_notes(second.id) is not None

    def test_user_request_runs_before_waiting_automatic_jobs(self, controller, config, history, calls):
        self._configure(config)
        running = self._ended(controller, history, "进行中的")
        auto = self._ended(controller, history, "自动排队")
        user = self._ended(controller, history, "手动请求")
        assert controller.generate(running.id) is True
        self._enqueue_auto(controller, auto)
        assert controller.generate(user.id) is True
        assert controller.queue_position(user.id) == 1 and controller.queue_position(auto.id) == 2
        controller._on_generation_done(_result("进行中的"))
        assert controller.generating_for == user.id
        controller._on_generation_done(_result("手动请求"))
        assert controller.generating_for == auto.id

    def test_transient_failure_is_retried_later_then_left_for_the_user(self, controller, config, history, calls):
        from sayink.meeting_session import AUTO_RETRY_DELAYS_MS

        self._configure(config)
        failures, queued = [], []
        controller.generation_failed.connect(lambda mid, msg: failures.append((mid, msg)))
        controller.generation_queued.connect(queued.append)
        record = self._live_meeting_ends(controller, history)
        for attempt, delay in enumerate(AUTO_RETRY_DELAYS_MS, start=1):
            before = int(time.time() * 1000)
            controller._generator.last_error_transient = True
            controller._on_generation_error("纪要生成超时，已完成的分段可在重试时继续使用")
            job = controller.store.queued_generation(record.id)
            assert job is not None and not job.failed and job.attempts == attempt
            assert job.not_before >= before + delay and "超时" in job.last_error
            assert failures == [] and queued[-1] == record.id
            assert controller.generating_for == "" and controller._retry_timer.isActive()
            controller.store.reschedule_generation(record.id, attempts=attempt, not_before=0, last_error=job.last_error)
            controller.store.flush()
            controller._pump_queue()
            assert controller.generating_for == record.id
        controller._generator.last_error_transient = True
        controller._on_generation_error("纪要生成超时，已完成的分段可在重试时继续使用")
        job = controller.store.queued_generation(record.id)
        assert job is not None and job.failed
        assert failures == [(record.id, "纪要生成超时，已完成的分段可在重试时继续使用")]
        assert len(calls) == 1 + len(AUTO_RETRY_DELAYS_MS)

    def test_permanent_failure_is_kept_and_the_queue_moves_on(self, controller, config, history, calls):
        self._configure(config)
        failures = []
        controller.generation_failed.connect(lambda mid, msg: failures.append((mid, msg)))
        first = self._live_meeting_ends(controller, history)
        second = self._live_meeting_ends(controller, history)
        controller._generator.last_error_transient = False
        controller._on_generation_error("纪要生成失败: HTTP 401；请检查接口密钥")
        job = controller.store.queued_generation(first.id)
        assert job is not None and job.failed and "401" in job.last_error
        assert failures == [(first.id, "纪要生成失败: HTTP 401；请检查接口密钥")]
        assert controller.generating_for == second.id
        assert controller.queue_position(first.id) == 0

    def test_failed_user_request_is_reported_and_not_kept(self, controller, config, history, calls):
        self._configure(config)
        record = self._ended(controller, history, "手动")
        assert controller.generate(record.id) is True
        controller._generator.last_error_transient = True
        controller._on_generation_error("纪要生成超时")
        assert controller.store.queued_generation(record.id) is None
        assert not controller._retry_timer.isActive()

    def test_unconfirmed_remote_host_is_never_sent_automatically(self, controller, config, history, calls):
        self._configure(config, url="https://api.example.com/v1")
        failures = []
        controller.generation_failed.connect(lambda mid, msg: failures.append((mid, msg)))
        record = self._live_meeting_ends(controller, history)
        assert calls == []
        job = controller.store.queued_generation(record.id)
        assert job is not None and job.failed
        assert failures and failures[0][0] == record.id and "api.example.com" in failures[0][1]
        controller.confirm_remote_host("api.example.com")
        assert controller.generate(record.id) is True and len(calls) == 1

    def test_local_model_waits_until_the_next_meeting_ends(self, controller, config, history, calls):
        self._configure(config, url="http://127.0.0.1:11434/v1")
        waiting = self._ended(controller, history, "上一场")
        self._enqueue_auto(controller, waiting)
        controller.begin(MeetingOptions(), _caps())
        controller._pump_queue()
        assert calls == []
        record = controller.current
        history.enqueue(_row(record.id, 0, LONG_TEXT, speaker_id=1, started_at=record.started_at))
        history.flush(timeout=2.0)
        controller.finalize()
        assert controller.generating_for == waiting.id
        assert controller.queue_position(record.id) == 1

    def test_new_meeting_pauses_a_local_automatic_job(self, controller, config, history, calls, monkeypatch):
        self._configure(config, url="http://localhost:11434/v1")
        cancelled, queued = [], []
        monkeypatch.setattr(controller._generator, "cancel", lambda: cancelled.append(True))
        controller.generation_cancelled.connect(lambda mid: cancelled.append(mid))
        controller.generation_queued.connect(queued.append)
        first = self._live_meeting_ends(controller, history)
        assert controller.generating_for == first.id
        controller.begin(MeetingOptions(), _caps())
        assert controller.generating_for == "" and cancelled == [True]
        assert queued[-1] == first.id
        job = controller.store.queued_generation(first.id)
        assert job is not None and not job.failed and job.attempts == 0

    def test_remote_automatic_job_keeps_running_during_a_new_meeting(self, controller, config, history, calls):
        self._configure(config)
        first = self._live_meeting_ends(controller, history)
        controller.begin(MeetingOptions(), _caps())
        assert controller.generating_for == first.id

    def test_cancelling_removes_the_job_and_starts_the_next(self, controller, config, history, calls):
        self._configure(config)
        cancelled = []
        controller.generation_cancelled.connect(cancelled.append)
        first = self._live_meeting_ends(controller, history)
        second = self._live_meeting_ends(controller, history)
        third = self._live_meeting_ends(controller, history)
        controller.remove_from_queue(third.id)
        assert controller.store.queued_generation(third.id) is None and cancelled == [third.id]
        controller.remove_from_queue(first.id)
        assert cancelled == [third.id, first.id]
        assert controller.store.queued_generation(first.id) is None
        assert controller.generating_for == second.id

    def test_deleting_the_running_meeting_starts_the_next(self, controller, config, history, calls):
        self._configure(config)
        first = self._live_meeting_ends(controller, history)
        second = self._live_meeting_ends(controller, history)
        controller.delete_meeting(first.id)
        assert controller.generating_for == second.id

    def test_turning_the_switch_off_drops_waiting_automatic_jobs(self, controller, config, history, calls):
        self._configure(config)
        running = self._ended(controller, history, "手动")
        assert controller.generate(running.id) is True
        waiting = self._live_meeting_ends(controller, history)
        config.set("meeting.auto_generate", False)
        controller._on_generation_done(_result("手动"))
        assert controller.generating_for == "" and controller.store.queued_generation(waiting.id) is None

    def test_automatic_job_never_replaces_existing_notes(self, controller, config, history, calls):
        self._configure(config)
        record = self._ended(controller, history, "已有纪要")
        controller.store.save_user_markdown(record.id, "我自己写的要点")
        self._enqueue_auto(controller, record)
        controller._pump_queue()
        assert calls == [] and controller.store.queued_generation(record.id) is None
        assert controller.store.get_notes(record.id).markdown == "我自己写的要点"

    def test_notes_written_by_hand_settle_a_waiting_or_failed_automatic_job(self, controller, config, history, calls):
        self._configure(config)
        failed = self._ended(controller, history, "失败后手写")
        waiting = self._ended(controller, history, "排队时手写")
        for record in (failed, waiting):
            self._enqueue_auto(controller, record)
        controller.store.fail_generation(failed.id, "HTTP 401")
        controller.store.flush()
        for record in (failed, waiting):
            assert controller.save_user_markdown(record.id, "手写要点")
            assert controller.store.queued_generation(record.id) is None
        assert calls == []

    def test_a_hand_written_save_drops_a_waiting_user_job(self, controller, config, history, calls):
        self._configure(config)
        running = self._ended(controller, history, "正在生成")
        assert controller.generate(running.id) is True
        waiting = self._ended(controller, history, "排队的手动纪要")
        assert controller.generate(waiting.id) is True
        assert controller.save_user_markdown(waiting.id, "我改好的纪要")
        assert controller.store.queued_generation(waiting.id) is None
        controller._on_generation_done(_result("正在生成"))
        assert controller.generating_for != waiting.id
        assert controller.store.get_notes(waiting.id).markdown == "我改好的纪要"

    def test_switching_off_clears_failed_automatic_jobs(self, controller, config, history, calls):
        self._configure(config)
        record = self._ended(controller, history, "失败")
        self._enqueue_auto(controller, record)
        controller.store.fail_generation(record.id, "HTTP 401")
        controller.store.flush()
        config.set("meeting.auto_generate", False)
        controller._pump_queue()
        assert controller.store.queued_generation(record.id) is None

    def test_queue_resumes_after_restart(self, config, history):
        self._configure(config)
        first = MeetingController(config, history, pending_segments=lambda: 0)
        record = self._ended(first, history, "上次没生成完")
        self._enqueue_auto(first, record)
        first.shutdown()
        second = MeetingController(config, history, pending_segments=lambda: 0)
        started = []
        second._generator.generate = started.append
        try:
            second.resume_generation_queue()
            assert second.generating_for == record.id and len(started) == 1
        finally:
            second.shutdown()


class TestMeetingPage:
    @pytest.fixture
    def page(self, config, controller):
        from sayink.ui.meeting_page import MeetingPage

        config.set("llm.api_url", "http://localhost:11434/v1")
        config.set("llm.model_name", "qwen2.5")
        widget = MeetingPage(config, controller)
        yield widget
        widget.deleteLater()

    def test_streaming_preview_never_replaces_saved_notes_and_reverts_on_failure(self, page, controller):
        record = controller.store.create_meeting(title="preview")
        controller.store.end_meeting(record.id)
        controller.store.save_notes(record.id, template_id="general", model="m", json_payload="",
                                    markdown="# Previous\n\nSaved result")
        assert controller.store.flush()
        page.show_meeting(record.id)
        controller._generating_for = record.id
        controller._on_generation_progress("正在生成纪要…")
        controller._on_generation_preview("生成预览 · 尚未保存\n\nDraft text")
        assert "Draft text" in page._notes_view.toPlainText()
        assert controller.store.get_notes(record.id).markdown == "# Previous\n\nSaved result"
        assert not page._edit_btn.isEnabled()
        controller._on_generation_error("network interrupted")
        assert "Draft text" not in page._notes_view.toPlainText()
        assert "Saved result" in page._notes_view.toPlainText()

    def test_empty_state_and_start_card(self, page):
        from sayink.ui.meeting_page import PAGE_BROWSE, PAGE_START

        assert page.meeting_count() == 0
        assert page._stack.currentIndex() == PAGE_BROWSE
        page.show_start_card()
        assert page._stack.currentIndex() == PAGE_START
        assert page._opt_source.currentData() == "mixed"
        page._leave_start_card()
        assert page._stack.currentIndex() == PAGE_BROWSE

    def test_open_start_card_follows_dictation_source_without_replacing_a_choice(self, page, config):
        config.set("meeting.input_source", "")
        config.set("audio.input_source", "system")
        page.show_start_card()
        assert page._opt_source.itemText(0) == "跟随转写（当前：电脑声音）"
        assert page._opt_source.currentData() == ""

        config.set("audio.input_source", "mixed")
        page.refresh()
        assert page._opt_source.itemText(0) == "跟随转写（当前：麦克风 + 电脑声音）"
        assert page._opt_source.currentData() == ""

        page._opt_source.setCurrentIndex(page._opt_source.findData("microphone"))
        config.set("audio.input_source", "system")
        page.refresh()
        assert page._opt_source.itemText(0) == "跟随转写（当前：电脑声音）"
        assert page._opt_source.currentData() == "microphone"

    def test_start_card_emits_options_without_changing_defaults(self, page, config):
        received = []
        page.start_requested.connect(received.append)
        page.show_start_card()
        page._opt_title.setText("设计评审")
        page._opt_participants.setText("甲, 乙")
        page._opt_source.setCurrentIndex(page._opt_source.findData("microphone"))
        page._start_from_card()
        assert len(received) == 1
        options = received[0]
        assert isinstance(options, MeetingOptions)
        assert options.title == "设计评审" and options.participants == "甲, 乙"
        assert options.input_source == "microphone"
        assert config.get("meeting.input_source") == "mixed"

    def test_start_card_model_and_language_are_session_overrides(self, page, config):
        config.set("stt.language", "zh")
        config.set("meeting.model_id", "funasr-nano")
        page.show_start_card()
        page._opt_model.addItem("临时模型", "sensevoice")
        page._opt_model.setCurrentIndex(page._opt_model.findData("sensevoice"))
        page._opt_language.setCurrentIndex(page._opt_language.findData("en"))
        received = []
        page.start_requested.connect(received.append)
        page._start_from_card()
        assert received[0].model_id == "sensevoice" and received[0].language == "en"
        assert config.get("meeting.model_id") == "funasr-nano"
        assert config.get("stt.language") == "zh"

    def test_live_view_follows_controller_state(self, page, controller, history):
        from sayink.ui.meeting_page import PAGE_BROWSE, PAGE_LIVE

        record = controller.begin(MeetingOptions(title="周会"), _caps())
        assert page._stack.currentIndex() == PAGE_LIVE
        assert page._live_title.text() == "周会"
        history.enqueue(_row(record.id, 0, "第一句", speaker_id=1, started_at=record.started_at))
        history.flush(timeout=2.0)
        page._render_live()
        assert "第一句" in page._live_transcript.toPlainText()
        controller.pause("设备中断")
        assert page._resume_btn.isVisibleTo(page)
        controller.resume()
        assert not page._resume_btn.isVisibleTo(page)
        controller.finalize()
        _pump(50)
        assert page._stack.currentIndex() == PAGE_BROWSE
        assert page.current_meeting_id == record.id
        assert page.meeting_count() == 1
        assert "第一句" in page._transcript_view.toPlainText()
        assert page._generate_btn.text() == "生成纪要"

    def test_detail_shows_notes_and_rename_updates_transcript(self, page, controller, history):
        record = controller.store.create_meeting(title="评审")
        history.enqueue(_row(record.id, 0, "方案通过", speaker_id=1, started_at=record.started_at))
        controller.store.end_meeting(record.id, ended_at=record.started_at + 5000)
        controller.store.save_notes(
            record.id, template_id="general", model="m", json_payload="",
            markdown="# 评审\n\n说话人1 同意。", duration_ms=10, chunked=False,
        )
        controller.store.flush()
        page.show_meeting(record.id)
        assert page._generate_btn.text() == "重新生成"
        assert "同意" in page._notes_view.toPlainText()
        assert [a.text() for a in page._speaker_menu.actions()] == ["说话人1"]
        assert "speaker:1" in page._transcript_view.toHtml()
        page._rename_speaker(1, " 王五 ")
        assert "王五" in page._transcript_view.toPlainText()
        assert "王五" in page._notes_view.toPlainText()
        assert [a.text() for a in page._speaker_menu.actions()] == ["王五"]

    def test_transcript_name_link_opens_rename(self, page, controller, history, monkeypatch):
        from PyQt6.QtCore import QUrl

        record = controller.store.create_meeting(title="评审")
        history.enqueue(_row(record.id, 0, "方案通过", speaker_id=2, started_at=record.started_at))
        controller.store.end_meeting(record.id)
        controller.store.flush()
        page.show_meeting(record.id)
        prompted = []
        monkeypatch.setattr(page, "_prompt_rename_speaker", prompted.append)
        page._on_transcript_link(QUrl("speaker:2"))
        page._on_transcript_link(QUrl("https://example.com"))
        assert prompted == [2]

    def test_current_content_copy_is_primary_while_reviewing(self, page, controller, history):
        from sayink.ui import settings_styles as ss

        record = controller.store.create_meeting(title="评审")
        controller.store.end_meeting(record.id)
        controller.store.flush()
        page.show_meeting(record.id)
        assert page._generate_btn.styleSheet() == ss.BTN_ACCENT_SM
        controller.store.save_notes(
            record.id, template_id="general", model="m", json_payload="", markdown="纪要", duration_ms=1,
        )
        controller.store.flush()
        page._render_detail()
        assert page._generate_btn.styleSheet() == ss.BTN_GHOST_SM
        assert page._start_btn.styleSheet() == ss.BTN_GHOST_SM
        assert page._copy_btn.styleSheet() == ss.BTN_ACCENT_SM

    def test_notes_display_drops_header_duplicates(self):
        from sayink.ui.meeting_page import notes_for_display

        md = "# 周会\n\n- 时间：2026-10-17 15:14\n- 时长：00:01:25\n- 参会人：甲\n\n## 摘要\n\n- 时间：保留在正文\n"
        shown = notes_for_display(md, "周会")
        assert shown.startswith("- 参会人：甲")
        assert "# 周会" not in shown and "2026-10-17" not in shown
        assert "- 时间：保留在正文" in shown
        assert notes_for_display("# 周会（评审）\n\n## 摘要\n", "周会").startswith("# 周会（评审）")

    def test_citations_become_transcript_links(self):
        from sayink.ui.meeting_page import link_citations

        out = link_citations("- 同意方案 [00:01:05]\n- 已链接 [00:00:01](x)")
        assert "(ts:65)" in out
        assert "[00:00:01](x)" in out

    def test_citation_click_highlights_transcript_line(self, page, controller, history):
        from PyQt6.QtCore import QUrl
        from sayink.ui import design_tokens as tok

        record = controller.store.create_meeting(title="评审")
        for seq, text in enumerate(("第一句", "第二句", "第三句")):
            history.enqueue(_row(record.id, seq, text, speaker_id=1, started_at=record.started_at))
        history.flush(timeout=2.0)
        controller.store.end_meeting(record.id)
        controller.store.flush()
        page.show_meeting(record.id)
        page._on_notes_link(QUrl("ts:4"))
        assert page._content_stack.currentWidget() is page._transcript_view
        assert page._transcript_highlight == 1
        assert tok.HIGHLIGHT_BG.lower() in page._transcript_view.toHtml().lower()

    def test_speakers_get_distinct_colors(self):
        from sayink.ui import design_tokens as tok
        from sayink.ui.meeting_page import speaker_color

        assert speaker_color(1) != speaker_color(2)
        assert speaker_color(1 + len(tok.SPEAKER_COLORS)) == speaker_color(1)
        assert speaker_color(0) == tok.TEXT_SEC

    def test_list_rows_split_title_and_meta(self, page, controller):
        record = controller.store.create_meeting(title="评审")
        controller.store.end_meeting(record.id, ended_at=record.started_at + 5000)
        controller.store.flush()
        page.refresh()
        row = page._list.itemWidget(page._list.item(0))
        assert row.findChild(QLabel, "meetingRowTitle").accessibleName() == "评审"
        assert "5 秒" in row.findChild(QLabel, "meetingRowMeta").accessibleName()
        assert "已结束" in row.findChild(QLabel, "meetingRowMeta").accessibleName()
        assert page._search_edit.parentWidget() is page._list.parentWidget()

    def test_unconfigured_minutes_button_leads_to_settings(self, page, controller, config):
        config.set("llm.api_url", "")
        record = controller.store.create_meeting(title="评审")
        controller.store.end_meeting(record.id)
        controller.store.flush()
        page.show_meeting(record.id)
        assert page._generate_btn.text() == "配置 AI 接口"
        asked = []
        page.configure_requested.connect(lambda: asked.append(True))
        page._generate_btn.click()
        assert asked == [True]
        assert controller.generating_for == ""

        config.set("llm.api_url", "http://localhost:11434/v1")
        config.set("llm.model_name", "qwen2.5")
        page.show_meeting(record.id)
        assert page._generate_btn.text() == "生成纪要"

    def test_list_shares_history_row_states(self, page):
        from sayink.ui.history_window import session_list_item_css

        css = page._list.styleSheet()
        assert session_list_item_css().strip() in css
        assert "border: 2px solid transparent" in css
        assert "QListWidget:focus" not in css
        assert "border-radius" not in css.split("QLabel")[0]

    def test_saved_meeting_reads_on_the_page_and_edits_in_a_field(self, page):
        assert "border: none" in page._notes_view.styleSheet()
        assert "border: none" in page._transcript_view.styleSheet()
        assert "border: 1px solid" in page._notes_edit.styleSheet()

    def test_tabs_switch_content_and_tools(self, page, controller):
        record = controller.store.create_meeting(title="评审")
        controller.store.end_meeting(record.id)
        controller.store.flush()
        page.show_meeting(record.id)
        assert page._content_stack.currentWidget() is page._notes_stack
        page._tab_transcript.click()
        assert page._content_stack.currentWidget() is page._transcript_view
        assert page._tab_tools.currentIndex() == 1
        page._tab_notes.click()
        assert page._content_stack.currentWidget() is page._notes_stack

    def test_regenerate_asks_before_replacing_notes(self, page, controller, monkeypatch):
        record = controller.store.create_meeting(title="评审")
        controller.store.end_meeting(record.id)
        controller.store.save_notes(
            record.id, template_id="general", model="m", json_payload="", markdown="旧纪要", duration_ms=1,
        )
        controller.store.flush()
        page.show_meeting(record.id)
        calls = []
        monkeypatch.setattr(page, "_confirm_regenerate", lambda: False)
        monkeypatch.setattr(controller, "generate", lambda *a, **k: calls.append(a))
        page._generate()
        assert calls == []

    def test_restore_previous_swaps_versions(self, page, controller):
        record = controller.store.create_meeting(title="评审")
        controller.store.end_meeting(record.id)
        for text in ("第一版", "第二版"):
            controller.store.save_notes(
                record.id, template_id="general", model="m", json_payload="", markdown=text, duration_ms=1,
            )
        controller.store.flush()
        page.show_meeting(record.id)
        assert page._restore_action.isEnabled()
        page._restore_previous()
        notes = controller.store.get_notes(record.id)
        assert notes.markdown == "第一版" and notes.previous_markdown == "第二版"
        # Both versions were AI output: restoring one is not a manual edit.
        assert not notes.user_edited
        assert "AI 生成" in page._notes_state.text()
        assert "第一版" in page._notes_view.toPlainText()
        page._restore_previous()
        assert controller.store.get_notes(record.id).markdown == "第二版"

    def test_restore_brings_back_the_edited_flag_of_that_version(self, page, controller):
        record = controller.store.create_meeting(title="评审")
        controller.store.end_meeting(record.id)
        controller.store.save_notes(record.id, template_id="general", model="m", json_payload="", markdown="AI 版", duration_ms=1)
        controller.store.flush()
        controller.save_user_markdown(record.id, "我改过的版本")
        controller.store.save_notes(record.id, template_id="general", model="m", json_payload="", markdown="新 AI 版", duration_ms=1)
        controller.store.flush()
        page.show_meeting(record.id)
        page._restore_previous()
        notes = controller.store.get_notes(record.id)
        assert notes.markdown == "我改过的版本" and notes.user_edited
        assert "已手动编辑" in page._notes_state.text()
        page._restore_previous()
        notes = controller.store.get_notes(record.id)
        assert notes.markdown == "新 AI 版" and not notes.user_edited

    def test_rename_after_the_custom_refine_template_was_deleted_keeps_the_prose(self, controller, history, config):
        """The delete dialog promises the saved text survives; re-rendering a
        refine payload through 「通用会议」 would replace it with empty sections."""
        record = controller.store.create_meeting(title="评审")
        history.enqueue(_row(record.id, 0, "张强这个月回款才38万", speaker_id=1, started_at=record.started_at))
        controller.store.end_meeting(record.id)
        payload = {"kind": "refine", "paragraphs": [{"text": "张强这个月回款才38万，说话人1 很不满意。", "sources": [0]}],
                   "_speaker_names": {"1": "说话人1"}}
        prose = "张强这个月回款才38万，说话人1 很不满意。\n"
        controller.store.save_notes(
            record.id, template_id="custom-refine", model="m",
            json_payload=json.dumps(payload, ensure_ascii=False), markdown=prose, duration_ms=1,
        )
        controller.store.flush()
        config.set("meeting.custom_templates", [])  # the template is gone

        controller.rename_speaker(record.id, 1, "王五")

        notes = controller.store.get_notes(record.id)
        assert "本场未识别到" not in notes.markdown
        assert "王五 很不满意" in notes.markdown
        # A name inside the prose is content, not the speaker label.
        assert "张强这个月回款才38万" in notes.markdown

    def test_rename_after_a_custom_minutes_template_was_deleted_patches_the_text(self, controller, history, config):
        record = controller.store.create_meeting(title="评审")
        history.enqueue(_row(record.id, 0, "好", speaker_id=1, started_at=record.started_at))
        controller.store.end_meeting(record.id)
        payload = {"summary": "说话人1 汇报了进度。", "_speaker_names": {"1": "说话人1"}}
        controller.store.save_notes(
            record.id, template_id="custom-x", model="m",
            json_payload=json.dumps(payload, ensure_ascii=False),
            markdown="# 评审\n\n## 摘要\n说话人1 汇报了进度。\n\n## 风险\n- 无\n", duration_ms=1,
        )
        controller.store.flush()

        controller.rename_speaker(record.id, 1, "王五")

        notes = controller.store.get_notes(record.id)
        assert "## 风险" in notes.markdown  # the custom section set is kept
        assert "王五 汇报了进度" in notes.markdown
        assert notes.json_payload  # still re-renderable once the template is back

    def test_speaker_rename_keeps_previous_version(self, controller, history):
        record = controller.store.create_meeting(title="评审")
        history.enqueue(_row(record.id, 0, "好", speaker_id=1, started_at=record.started_at))
        controller.store.end_meeting(record.id)
        for text in ("第一版", "第二版 说话人1"):
            controller.store.save_notes(
                record.id, template_id="general", model="m", json_payload="", markdown=text, duration_ms=1,
            )
        controller.store.flush()
        controller.rename_speaker(record.id, 1, "王五")
        notes = controller.store.get_notes(record.id)
        assert notes.markdown == "第二版 王五" and notes.previous_markdown == "第一版"

    def test_edit_toggle_saves_user_markdown(self, page, controller, history):
        record = controller.store.create_meeting(title="评审")
        controller.store.end_meeting(record.id)
        controller.store.save_notes(
            record.id, template_id="general", model="m", json_payload="",
            markdown="原文", duration_ms=10, chunked=False,
        )
        controller.store.flush()
        page.show_meeting(record.id)
        page._toggle_edit()
        assert page._edit_btn.text() == "保存修改"
        page._notes_edit.setPlainText("改过的纪要")
        page._toggle_edit()
        notes = controller.store.get_notes(record.id)
        assert notes is not None and notes.markdown == "改过的纪要" and notes.user_edited
        assert "手动编辑" in page._notes_state.text()
        assert "已保存" in page._status_label.text()


class TestSpeakerSessionWiring:
    def test_begin_builds_session_with_embedder_priors(self, controller):
        from sayink.speaker_embedding import MEL_STICKY_THRESHOLD, STICKY_GAP_MS

        controller.begin(MeetingOptions(), _caps())
        speakers = controller.speakers
        assert speakers._sticky_threshold == MEL_STICKY_THRESHOLD
        assert speakers._sticky_gap_ms == STICKY_GAP_MS
        assert speakers.threshold == controller.embedder.threshold

    def test_recluster_uses_embedder_recluster_threshold(self, controller, history):
        """Centroids at 0.80 similarity: below the 0.86 online bar, above the
        0.75 recluster bar for the mel backend → merged after the meeting."""
        record = controller.begin(MeetingOptions(), _caps())
        speakers = controller.speakers
        a = np.zeros(24, dtype=np.float32)
        a[0] = 1.0
        b = np.zeros(24, dtype=np.float32)
        b[0], b[1] = 0.8, 0.6                        # cos(a, b) = 0.80
        ids = [speakers.assign(a, ""), speakers.assign(b, "")]
        assert ids == [1, 2]
        for seq, sid in enumerate(ids):
            history.enqueue(_row(record.id, seq, f"t{seq}", speaker_id=sid, started_at=record.started_at))
        history.flush(timeout=2.0)
        controller.finalize()
        rows = controller.store.get_segments(record.id)
        assert rows[0].speaker_id == rows[1].speaker_id == 1

    def test_keyed_turns_are_reassigned_one_by_one_around_manual_locks(self, controller, history):
        """With per-segment keys the finalize pass clusters every turn: a
        sentence that online matching stuck in the wrong group moves on its
        own, a one-off spare number folds into the voice it belongs to, and a
        segment the user corrected (plus its number) stays put."""
        record = controller.begin(MeetingOptions(), _caps())
        speakers = controller.speakers
        a = np.zeros(24, dtype=np.float32)
        a[0] = 1.0
        a2 = a.copy()
        a2[1] = 0.2
        b = np.zeros(24, dtype=np.float32)
        b[2] = 1.0
        b2 = b.copy()
        b2[3] = 0.2
        plan = [a, b, a2, b2, a, b, a2, b2, a]
        ids = []
        for seq, vector in enumerate(plan):
            start = record.started_at + seq * 4000
            ids.append(speakers.assign(vector, "", start_ms=start, end_ms=start + 3500, sample_key=seq))
        assert ids[0] == 1 and ids[1] == 2
        rows = [_row(record.id, seq, f"t{seq}", speaker_id=sid, started_at=record.started_at) for seq, sid in enumerate(ids)]
        # Online matching got seq 4 wrong and invented a spare number for seq 8.
        rows[4] = SegmentRecord(**{**rows[4].__dict__, "speaker_id": 2})
        rows[8] = SegmentRecord(**{**rows[8].__dict__, "speaker_id": 3})
        # The user pinned seq 1 to speaker 2 before the meeting ended.
        rows[1] = SegmentRecord(**{**rows[1].__dict__, "speaker_locked": True})
        for row in rows:
            history.enqueue(row)
        history.flush(timeout=2.0)
        controller.finalize()
        saved = controller.store.get_segments(record.id)
        assert [s.speaker_id for s in saved] == [1, 2, 1, 2, 1, 2, 1, 2, 1]
        assert saved[1].speaker_locked and not saved[4].speaker_locked


class TestMeetingPageAuxDownload:
    @pytest.fixture
    def page(self, config, controller):
        from sayink.ui.meeting_page import MeetingPage

        widget = MeetingPage(config, controller)
        yield widget
        widget.deleteLater()

    def test_start_card_offers_download_when_models_missing(self, page, monkeypatch):
        missing = [{"id": "speaker-campplus", "name": "说话人嵌入 CAM++", "size_mb": 28}]
        monkeypatch.setattr("sayink.ui.meeting_page.missing_aux_models", lambda: missing)
        page.show_start_card()
        assert page._card_dl_btn.isVisibleTo(page)
        assert "28 MB" in page._card_dl_btn.text()
        monkeypatch.setattr("sayink.ui.meeting_page.missing_aux_models", list)
        page._update_card_notes()
        assert not page._card_dl_btn.isVisibleTo(page)

    def test_live_view_offers_download_and_reports_result(self, page, controller, monkeypatch):
        missing = [{"id": "silero-vad", "name": "Silero VAD", "size_mb": 2}]
        monkeypatch.setattr("sayink.ui.meeting_page.missing_aux_models", lambda: missing)
        controller.begin(MeetingOptions(title="x"), _caps())
        _pump(20)
        assert page._live_dl_btn.isVisibleTo(page)
        changed = []
        page.aux_models_changed.connect(lambda: changed.append(True))
        monkeypatch.setattr(page._aux_dl, "start", lambda ids=None: True)
        page._download_aux_models()
        assert not page._live_dl_btn.isEnabled()
        page._on_aux_progress(42)
        assert "42%" in page._live_dl_btn.text()
        monkeypatch.setattr("sayink.ui.meeting_page.missing_aux_models", list)
        page._on_aux_finished(True, "")
        assert changed == [True]
        assert "下一场会议生效" in page._live_caps.text()
        assert not page._live_dl_btn.isVisibleTo(page)

    def test_failed_download_is_explained_on_card(self, page, monkeypatch):
        missing = [{"id": "silero-vad", "name": "Silero VAD", "size_mb": 2}]
        monkeypatch.setattr("sayink.ui.meeting_page.missing_aux_models", lambda: missing)
        page.show_start_card()
        page._on_aux_finished(False, "Silero VAD 下载失败：网络超时")
        assert "网络超时" in page._card_notes.text()
        assert page._card_dl_btn.isVisibleTo(page) and page._card_dl_btn.isEnabled()

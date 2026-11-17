"""Contract tests for Track B: glossary hotword decoupling, self-learning flywheel,
and ReloadHostProtocol orchestration.
"""

from __future__ import annotations

from pathlib import Path

from sayink.app import App
from sayink.asr_correction import (
    MAX_GLOSSARY_ENTRIES,
    apply_glossary_variants,
    extract_learned_correction_pair,
    merge_glossary_learning,
    persist_glossary_learning,
)
from sayink.config import Config
from sayink.contracts import ReloadHostProtocol
from sayink.history_store import HistoryStore, SegmentRecord
from sayink.meeting_session import MeetingController, MeetingOptions, resolve_capabilities
from sayink.recognition_settings import RecognitionSettings
from sayink.reload_coordinator import ReloadCoordinator
from sayink.speech_recognizer import MAX_HOTWORDS, format_hotwords, restore_glossary_case
from sayink.ui.history_window import HistoryWindow
from sayink.ui.meeting_page import MeetingPage


def test_decoder_hotwords_cap_at_30_while_post_asr_glossary_scales_to_500():
    assert MAX_HOTWORDS == 30
    assert MAX_GLOSSARY_ENTRIES == 500

    entries = [
        {"term": f"专有名词{i:03d}", "variants": [f"误写词{i:03d}"]}
        for i in range(MAX_GLOSSARY_ENTRIES)
    ]
    entries[420] = {"term": "OpenTelemetry", "variants": ["open telemetry"]}

    hotwords = format_hotwords(entries).split(",")
    assert len(hotwords) == 30
    assert hotwords[0] == "专有名词000"
    assert hotwords[-1] == "专有名词029"

    # Entry #499 (far beyond decoder hotword cap) still applies deterministically at 0 latency.
    replaced = apply_glossary_variants("今天提到了误写词499和open telemetry", entries)
    assert replaced == "今天提到了专有名词499和OpenTelemetry"
    assert restore_glossary_case("部署在OPENTELEMETRY集群", entries) == "部署在OpenTelemetry集群"

    # Appending beyond top-30 hotwords does NOT alter RecognitionSettings.asr_key (zero model reload).
    s_before = RecognitionSettings(model_id="funasr-nano", glossary=entries[:35])
    s_after = RecognitionSettings(model_id="funasr-nano", glossary=entries[:250])
    assert s_before.asr_key(4) == s_after.asr_key(4)


def test_extract_learned_correction_pair_filters_noise_and_extracts_lexical_fixes():
    # 1. CJK 2-char name homophone fix inside a sentence (expands shared neighbor '张')
    assert extract_learned_correction_pair("今天张新主持评审会", "今天张鑫主持评审会") == ("张鑫", "张新")
    # 2. CJK noun homophone fix on the leading character (expands shared neighbor '盘')
    assert extract_learned_correction_pair("训盘的数据已经同步", "新盘的数据已经同步") == ("新盘", "训盘")
    # 3. English casing / spelling fix inside mixed text
    assert extract_learned_correction_pair("我们用 sayink 做会议记录", "我们用 SayInk 做会议记录") == (
        "SayInk",
        "sayink",
    )
    assert extract_learned_correction_pair("部署到 kubernets 集群", "部署到 Kubernetes 集群") == (
        "Kubernetes",
        "kubernets",
    )
    # 4. Rejects punctuation-only edits, pure numbers, broad rewrites, and daily words
    assert extract_learned_correction_pair("今天开会", "今天开会。") is None
    assert extract_learned_correction_pair("预算 120 万", "预算 150 万") is None
    assert extract_learned_correction_pair(
        "我觉得这个方案可能需要后面再重新讨论一下",
        "方案延后评审",
    ) is None
    assert extract_learned_correction_pair("大家都同意这个结论", "大家都通义这个结论") is None
    # 5. Skips rules already covered by existing glossary
    existing = [{"term": "张鑫", "variants": ["张新"]}]
    assert extract_learned_correction_pair("今天张新主持", "今天张鑫主持", existing) is None


def test_merge_and_persist_glossary_learning_preserves_top_hotwords(tmp_path: Path):
    cfg = Config(config_dir=tmp_path)
    cfg.set("stt.glossary", [{"term": "核心词A"}, {"term": "张鑫", "variants": ["张欣"]}])

    # Merging a new variant onto an existing term keeps ordering intact
    assert persist_glossary_learning(cfg, "张鑫", "张新") is True
    assert cfg.get("stt.glossary") == [
        {"term": "核心词A"},
        {"term": "张鑫", "variants": ["张欣", "张新"]},
    ]

    # Case-only Latin fix adds canonical term without redundant variant
    updated, changed = merge_glossary_learning(cfg.get("stt.glossary"), "SayInk", "sayink")
    assert changed is True
    assert updated[-1] == {"term": "SayInk"}


def test_history_and_meeting_edit_flywheel_end_to_end(qapp, tmp_path: Path):
    cfg = Config(config_dir=tmp_path)
    store = HistoryStore(db_path=tmp_path / "history.db")
    try:
        store.enqueue(
            SegmentRecord(
                session_id="sess-1",
                seq=0,
                created_at=1_700_000_000_000,
                raw_text="今天张新来介绍新方案",
                polished_text="",
                source="microphone",
                duration_ms=2000,
                target_app="notepad.exe",
                trigger_mode="hotkey",
                model="funasr-nano",
            )
        )
        assert store.flush(timeout=2.0)

        history_view = HistoryWindow(store, config=cfg)
        try:
            learned_events: list[bool] = []
            history_view.glossary_updated.connect(lambda: learned_events.append(True))
            assert history_view.edit_selected_session("今天张鑫来介绍新方案") is True
            assert history_view.pending_glossary_suggestion == ("张鑫", "张新")
            assert history_view.accept_glossary_suggestion() is True
            assert learned_events == [True]
            assert cfg.get("stt.glossary") == [{"term": "张鑫", "variants": ["张新"]}]
        finally:
            history_view.close()

        # Meeting transcript correction -> one-click glossary learning
        ctrl = MeetingController(cfg, store, pending_segments=lambda: 0)
        opts = MeetingOptions(title="架构评审")
        rec = ctrl.begin(opts, resolve_capabilities(cfg, opts, dictation_model_id="funasr-nano"))
        store.enqueue(
            SegmentRecord(
                session_id=rec.id,
                seq=0,
                created_at=rec.started_at + 1000,
                raw_text="请把五中材料补齐",
                polished_text="",
                source="mixed",
                duration_ms=2500,
                target_app="",
                trigger_mode="meeting",
                model="funasr-nano",
                speaker_id=1,
            )
        )
        assert store.flush(timeout=2.0)
        ctrl.finalize()

        meeting_view = MeetingPage(cfg, ctrl)
        try:
            meeting_view.show_meeting(rec.id)
            assert meeting_view.apply_segment_correction(rec.id, 0, "请把五证材料补齐") is True
            assert meeting_view.pending_glossary_suggestion == ("五证", "五中")
            assert meeting_view.accept_glossary_suggestion() is True
            assert {"term": "五证", "variants": ["五中"]} in cfg.get("stt.glossary")
        finally:
            meeting_view.cleanup()
            meeting_view.close()
    finally:
        store.close(timeout=2.0)


def test_app_satisfies_reload_host_protocol_and_coordinates_via_typed_host(tmp_path: Path):
    cfg = Config(config_dir=tmp_path)
    app = App.create_headless(cfg)
    try:
        assert isinstance(app, ReloadHostProtocol)
        assert app.reload_coordinator.host is app
        snap = app.reload_snapshot
        assert snap.recognition_change_pending is False
        assert snap.pending_apply_notice == ""

        # Standalone fake ReloadHostProtocol works cleanly without 23 lambdas
        class _FakeHost:
            def __init__(self) -> None:
                self.notices: list[str] = []
                self.busy = True

            def is_meeting_active(self) -> bool:
                return False

            def meeting_save_audio_override(self) -> bool | None:
                return None

            def is_reload_pipeline_busy(self) -> bool:
                return self.busy

            def queued_audio_segment_count(self) -> int:
                return 0

            def is_recovery_busy(self) -> bool:
                return False

            def is_exit_started(self) -> bool:
                return False

            def is_onboarding_active(self) -> bool:
                return False

            def is_main_window_visible(self) -> bool:
                return True

            def clear_queued_audio(self) -> None:
                pass

            def stop_continuous_listening(self) -> None:
                pass

            def reset_continuous_stop_flags(self) -> None:
                pass

            def rebuild_speaker_embedder(self) -> None:
                pass

            def sync_hotkey_trigger_mode(self) -> None:
                pass

            def sync_settings_runtime_status(self, status: object = None) -> None:
                pass

            def sync_recognition_runtime_status(self) -> None:
                pass

            def sync_deferred_settings_notice(self, notice: str = "") -> None:
                self.notices.append(notice)

            def sync_tray_status_summary(self, status: object = None) -> None:
                pass

        fake_host = _FakeHost()
        assert isinstance(fake_host, ReloadHostProtocol)
        coord = ReloadCoordinator(host=fake_host, config=cfg)
        cfg.set("stt.language", "en")
        coord.on_recognition_settings_changed()
        assert coord.snapshot().recognition_change_pending is True
        assert fake_host.notices[-1] != ""
    finally:
        app.deleteLater()

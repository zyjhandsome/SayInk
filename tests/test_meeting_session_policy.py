"""Backend policy tests for MeetingOptions / MeetingCapabilities / MeetingController."""

from __future__ import annotations

import json
import pytest

from sayink.config import Config
from sayink.history_store import HistoryStore
from sayink.meeting_session import (
    MeetingController,
    MeetingOptions,
    resolve_capabilities,
)
from sayink.speaker_embedding import MelEmbedder


@pytest.mark.parametrize("source", ["microphone", "system", "mixed"])
def test_meeting_auto_enables_speaker_and_activity_across_all_sources_despite_legacy_config(
    tmp_path, monkeypatch, source
):
    cfg_dir = tmp_path / f"cfg-{source}"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    legacy = {
        "stt": {
            "speaker_model_enabled": False,
            "speaker_activity_enabled": False,
            "quiet_speech_enabled": False,
        },
        "meeting": {
            "input_source": source,
            "speaker_model_enabled": False,
            "speaker_activity_enabled": False,
        },
    }
    (cfg_dir / "config.json").write_text(json.dumps(legacy), encoding="utf-8")
    config = Config(config_dir=cfg_dir)

    monkeypatch.setattr(
        "sayink.meeting_session.aux_model_path",
        lambda mid: tmp_path / f"{mid}.onnx",
    )
    opts = MeetingOptions.from_config(config)
    assert opts.input_source == source
    caps = resolve_capabilities(config, opts, dictation_model_id="funasr-nano")
    assert caps.speaker_enabled is True
    assert caps.speaker_activity_enabled is True
    assert caps.speaker_model_available is True


def test_meeting_begin_passes_auto_activity_guard_and_smoothly_falls_back_when_campplus_missing(
    tmp_path, monkeypatch
):
    config = Config(config_dir=tmp_path / "cfg")
    history = HistoryStore(db_path=tmp_path / "history.db")
    ctrl = MeetingController(config, history, pending_segments=lambda: 0)
    try:
        # Case 1: Source checkout without CAM++ downloaded -> smooth MelEmbedder fallback, UI-observable note.
        monkeypatch.setattr("sayink.meeting_session.aux_model_path", lambda _mid: None)
        opts = MeetingOptions.from_config(config)
        caps_missing = resolve_capabilities(config, opts, dictation_model_id="funasr-nano")
        assert caps_missing.speaker_enabled is True
        assert caps_missing.speaker_model_available is False
        assert any("说话人识别模型未下载" in note for note in caps_missing.notes)

        ctrl.begin(opts, caps_missing)
        assert isinstance(ctrl.embedder, MelEmbedder)
        assert ctrl.capabilities.speaker_model_available is False
        assert ctrl.capabilities.speaker_load_failed is False
        ctrl.finalize()

        # Case 2: CAM++ & activity model downloaded -> begin() enables both CAM++ and on-demand activity guard.
        monkeypatch.setattr(
            "sayink.meeting_session.aux_model_path",
            lambda mid: tmp_path / f"{mid}.onnx",
        )
        seen_calls: list[tuple] = []

        class _FakeCampPlus:
            name = "campplus"
            threshold = 0.55
            sticky_threshold = 0.45
            recluster_threshold = 0.50
            cluster_threshold = 0.52
            activity_guard_error = ""

        def fake_make_embedder(path, *, enabled=True, activity_enabled=False):
            seen_calls.append((path, bool(enabled), bool(activity_enabled)))
            return _FakeCampPlus()

        monkeypatch.setattr("sayink.meeting_session.make_embedder", fake_make_embedder)
        caps_ready = resolve_capabilities(config, opts, dictation_model_id="funasr-nano")
        ctrl.begin(opts, caps_ready)
        assert seen_calls == [(tmp_path / "speaker-campplus.onnx", True, True)]
        assert ctrl.capabilities.speaker_model_available is True
        assert ctrl.capabilities.speaker_load_failed is False
        ctrl.finalize()
    finally:
        ctrl.shutdown()
        history.close(timeout=2.0)

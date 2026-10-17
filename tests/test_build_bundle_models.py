"""Full installer bundles the ASR model and the two meeting helpers."""

from __future__ import annotations

from pathlib import Path

import build as build_mod
from sayink.speech_recognizer import AUX_MODEL_SILERO_VAD, AUX_MODEL_SPEAKER, get_model_info


def _plant(root: Path, info: dict) -> None:
    folder = root / info["dir_name"]
    for name in info["files"]:
        target = folder / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")


def test_bundle_ids_are_asr_then_meeting_helpers():
    assert build_mod.bundle_model_ids() == [
        "funasr-nano",
        AUX_MODEL_SPEAKER,
        AUX_MODEL_SILERO_VAD,
    ]


def test_find_model_sources_picks_each_required_model(tmp_path: Path):
    asr_dir = tmp_path / "asr"
    helper_dir = tmp_path / "helpers"
    for model_id, dest in (
        ("funasr-nano", asr_dir),
        (AUX_MODEL_SPEAKER, helper_dir),
        (AUX_MODEL_SILERO_VAD, helper_dir),
    ):
        _plant(dest, get_model_info(model_id))

    found = dict(build_mod._find_model_sources([asr_dir, helper_dir]))

    assert set(found) == {
        "sherpa-onnx-funasr-nano-int8-2025-12-30",
        "3dspeaker-campplus-zh-en",
        "silero-vad",
    }
    assert found["sherpa-onnx-funasr-nano-int8-2025-12-30"].parent == asr_dir
    assert found["silero-vad"].parent == helper_dir


def test_find_model_sources_skips_an_incomplete_helper(tmp_path: Path):
    root = tmp_path / "models"
    _plant(root, get_model_info("funasr-nano"))
    _plant(root, get_model_info(AUX_MODEL_SPEAKER))
    # Silero directory exists but its weight file does not.
    (root / "silero-vad").mkdir()

    found = dict(build_mod._find_model_sources([root]))

    assert "silero-vad" not in found
    assert "3dspeaker-campplus-zh-en" in found

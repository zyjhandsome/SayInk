"""Full installer bundles the ASR model and the two meeting helpers."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import build as build_mod
from sayink.speech_recognizer import AUX_MODEL_SILERO_VAD, AUX_MODEL_SPEAKER, get_model_info


def _plant(root: Path, info: dict) -> None:
    folder = root / info["dir_name"]
    for name in info["files"]:
        target = folder / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")


def test_bundle_ids_are_asr_then_meeting_helpers():
    assert build_mod.bundle_aux_model_ids() == [AUX_MODEL_SPEAKER, AUX_MODEL_SILERO_VAD]
    assert build_mod.bundle_model_ids() == [
        "funasr-nano",
        AUX_MODEL_SPEAKER,
        AUX_MODEL_SILERO_VAD,
    ]


def test_lite_bundle_copies_helpers_and_leaves_the_asr_model(tmp_path: Path):
    root = tmp_path / "models"
    for model_id in ("funasr-nano", AUX_MODEL_SPEAKER, AUX_MODEL_SILERO_VAD):
        _plant(root, get_model_info(model_id))

    found = build_mod._find_model_sources([root], build_mod.bundle_aux_model_ids())
    names = {name for name, _ in found}

    assert names == {"3dspeaker-campplus-zh-en", "silero-vad"}
    build_mod._require_bundle_model(found, build_mod.bundle_aux_model_ids())


def test_lite_bundle_refuses_to_ship_without_a_helper(tmp_path: Path):
    info = get_model_info(AUX_MODEL_SPEAKER)
    _plant(tmp_path, info)
    with pytest.raises(SystemExit) as exc:
        build_mod._require_bundle_model(
            [(info["dir_name"], tmp_path / info["dir_name"])],
            build_mod.bundle_aux_model_ids(),
        )
    assert exc.value.code == 1


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


_REVISION = "0123456789abcdef0123456789abcdef01234567"


def _tiny_info(body: bytes) -> dict:
    return {"id": "tiny", "name": "Tiny", "hf_repo": "org/tiny", "dir_name": "tiny",
            "files": ["a.bin"], "revision": _REVISION,
            "sha256": {"a.bin": hashlib.sha256(body).hexdigest()}}


def test_full_build_refuses_models_that_differ_from_the_pins(tmp_path: Path):
    info = get_model_info("funasr-nano")
    _plant(tmp_path, info)
    with pytest.raises(SystemExit) as exc:
        build_mod._require_pinned_content([(info["dir_name"], tmp_path / info["dir_name"])])
    assert exc.value.code == 1


def test_full_build_accepts_models_matching_the_pins(tmp_path: Path, monkeypatch):
    info = _tiny_info(b"x")
    _plant(tmp_path, info)
    monkeypatch.setattr("sayink.speech_recognizer.get_model_info", lambda _mid: info)
    build_mod._require_pinned_content([("tiny", tmp_path / "tiny")])


class _Stream:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        return None

    def iter_bytes(self, _size=0):
        yield self._body


def _build_download(monkeypatch, tmp_path, body: bytes):
    import httpx
    import sayink_build.download_bundle_model_for_build as dl

    monkeypatch.setattr(dl, "get_model_info", lambda _mid: _tiny_info(b"weights"))
    monkeypatch.setattr(dl, "ROOT", tmp_path)
    urls = []
    monkeypatch.setattr(httpx, "stream", lambda _m, url, **_k: urls.append(url) or _Stream(body))
    return dl, urls


def test_build_download_is_pinned_and_rejects_a_wrong_hash(monkeypatch, tmp_path):
    dl, urls = _build_download(monkeypatch, tmp_path, b"tampered")
    assert dl._download_one("tiny") == 1
    assert urls == [f"https://huggingface.co/org/tiny/resolve/{_REVISION}/a.bin"]
    assert list((tmp_path / "models" / "tiny").iterdir()) == []


def test_build_download_replaces_a_corrupt_existing_file(monkeypatch, tmp_path):
    dl, urls = _build_download(monkeypatch, tmp_path, b"weights")
    target = tmp_path / "models" / "tiny" / "a.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"corrupt")
    assert dl._download_one("tiny") == 0
    assert target.read_bytes() == b"weights"
    assert len(urls) == 1

"""Shared recognition preferences and audio-source inheritance for every mode.

Session controllers own their lifecycle; these values only describe the common
audio/ASR capabilities. Meeting start-card overrides stay in that session.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

from sayink.audio_devices import INPUT_SOURCES
from sayink.speech_recognizer import (
    DEFAULT_MODEL_ID,
    SUPPORTS_HOTWORDS,
    SUPPORTS_LANGUAGE,
    effective_language,
    format_hotwords,
    get_model_dir,
    get_model_info,
    is_model_downloaded,
    model_supports,
)
from sayink.vad_segmenter import VAD_ENERGY, VAD_SILERO

# Where the shared recognition options (VAD, CAM++, overlap detection) and
# their real load state live in the UI. Every hint that sends the user there
# spells the path from here; the section was once called 「公共识别」 and that
# name lingered in tooltips after the page moved.
ADVANCED_RECOGNITION_PATH = "通用 → 识别 → 高级识别设置"


def audio_source(config, *, meeting: bool = False) -> str:
    """Resolve an empty meeting preference against the shared dictation source."""
    source = config.get("audio.input_source", "microphone")
    if source not in INPUT_SOURCES:
        source = "microphone"
    if meeting:
        override = config.get("meeting.input_source", "")
        if override in INPUT_SOURCES:
            source = override
    return source


def aux_model_path(model_id: str) -> Path | None:
    """A downloaded helper file; availability alone never implies it loaded."""
    if not is_model_downloaded(model_id):
        return None
    info = get_model_info(model_id)
    if not info:
        return None
    try:
        return get_model_dir(model_id) / info["files"][0]
    except ValueError:
        return None


@dataclass(frozen=True)
class RecognitionSettings:
    model_id: str
    glossary: list = field(default_factory=list)
    language: str = "auto"
    vad: str = VAD_SILERO
    speaker_model_enabled: bool = True
    speaker_activity_enabled: bool = False

    @classmethod
    def from_config(cls, config) -> "RecognitionSettings":
        glossary = config.get("stt.glossary", [])
        vad = config.get("stt.vad", VAD_SILERO)
        return cls(
            model_id=str(config.get("stt.model_id", DEFAULT_MODEL_ID) or DEFAULT_MODEL_ID),
            glossary=deepcopy(glossary) if isinstance(glossary, list) else [],
            language=str(config.get("stt.language", "auto") or "auto"),
            vad=vad if vad in (VAD_SILERO, VAD_ENERGY) else VAD_SILERO,
            speaker_model_enabled=bool(config.get("stt.speaker_model_enabled", True)),
            speaker_activity_enabled=bool(config.get("stt.speaker_activity_enabled", False)),
        )

    @property
    def hotwords(self) -> str:
        return format_hotwords(self.glossary)

    @property
    def effective_language(self) -> str:
        return effective_language(self.language)

    @property
    def hotwords_supported(self) -> bool:
        return model_supports(self.model_id, SUPPORTS_HOTWORDS)

    @property
    def language_supported(self) -> bool:
        return model_supports(self.model_id, SUPPORTS_LANGUAGE)

    @property
    def notes(self) -> list[str]:
        notes = []
        if self.hotwords and not self.hotwords_supported:
            notes.append("当前模型不支持识别期热词；术语仍可用于会议纪要。")
        if not self.language_supported:
            notes.append("当前模型不支持指定识别语言，将按模型自身能力识别。")
        return notes

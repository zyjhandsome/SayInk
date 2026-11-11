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

# Audio sources containing system loopback where multi-speaker separation is
# automatically enabled in continuous dictation.
LOOPBACK_SPEAKER_SOURCES = frozenset({"system", "mixed"})

# Internal R&D troubleshooting overrides (unset / None = automatic policy).
DEBUG_FORCE_SPEAKER_MODEL_KEY = "stt.debug_force_speaker_model"
DEBUG_FORCE_SPEAKER_ACTIVITY_KEY = "stt.debug_force_speaker_activity"
DEBUG_FORCE_QUIET_SPEECH_KEY = "stt.debug_force_quiet_speech"


def _normalize_policy_mode(trigger_mode: str | None) -> str:
    mode = str(trigger_mode or "").strip().lower()
    if mode in ("meeting",):
        return "meeting"
    if mode in ("continuous",):
        return "continuous"
    return "hotkey"


def _normalize_policy_source(input_source: str | None) -> str:
    source = str(input_source or "").strip().lower()
    if source in ("system", "mixed"):
        return source
    return "microphone"


def _optional_debug_bool(config, key: str) -> bool | None:
    if config is None or not hasattr(config, "get"):
        return None
    value = config.get(key, None)
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        word = value.strip().lower()
        if word in ("1", "true", "yes", "on"):
            return True
        if word in ("0", "false", "no", "off"):
            return False
    return None


@dataclass(frozen=True)
class ModeRecognitionPolicy:
    """Automatic speaker / turn-splitting / adaptive quiet-speech policy."""

    trigger_mode: str
    input_source: str
    diarization_enabled: bool
    turn_splitting_enabled: bool
    speaker_model_enabled: bool
    speaker_activity_enabled: bool
    quiet_speech_mode: bool | None = None


def resolve_mode_recognition_policy(
    trigger_mode: str | None,
    input_source: str | None = None,
    config=None,
) -> ModeRecognitionPolicy:
    """Resolve zero-config recognition behavior from (mode, audio_source).

    - ``hotkey`` (single-shot): speaker separation, turn splitting and activity
      guard are all disabled for zero overhead.
    - ``meeting``: enhanced CAM++ speaker separation + on-demand activity guard
      are automatically enabled.
    - ``continuous``: mic-only disables CAM++ / turn splitting / activity guard
      for fastest output; loopback sources (``system`` / ``mixed``) automatically
      enable CAM++ + on-demand activity guard.
    - ``quiet_speech_mode``: ``None`` selects real-time noise-floor adaptive gain.
    """
    mode = _normalize_policy_mode(trigger_mode)
    source = _normalize_policy_source(input_source)

    if mode == "meeting":
        auto_speaker = True
    elif mode == "continuous":
        auto_speaker = source in LOOPBACK_SPEAKER_SOURCES
    else:
        auto_speaker = False

    speaker_override = _optional_debug_bool(config, DEBUG_FORCE_SPEAKER_MODEL_KEY)
    speaker_model_enabled = auto_speaker if speaker_override is None else speaker_override

    activity_override = _optional_debug_bool(config, DEBUG_FORCE_SPEAKER_ACTIVITY_KEY)
    if activity_override is not None:
        speaker_activity_enabled = activity_override
    else:
        speaker_activity_enabled = speaker_model_enabled

    diarization_enabled = speaker_model_enabled and mode in ("continuous", "meeting")
    turn_splitting_enabled = diarization_enabled
    quiet_speech_mode = _optional_debug_bool(config, DEBUG_FORCE_QUIET_SPEECH_KEY)

    return ModeRecognitionPolicy(
        trigger_mode=mode,
        input_source=source,
        diarization_enabled=diarization_enabled,
        turn_splitting_enabled=turn_splitting_enabled,
        speaker_model_enabled=speaker_model_enabled,
        speaker_activity_enabled=speaker_activity_enabled,
        quiet_speech_mode=quiet_speech_mode,
    )


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
    quiet_speech_enabled: bool = False
    quiet_speech_mode: bool | None = None
    boundary_context_enabled: bool = False
    trigger_mode: str = "hotkey"
    input_source: str = "microphone"

    @classmethod
    def from_config(
        cls,
        config,
        *,
        meeting: bool = False,
        input_source: str | None = None,
    ) -> "RecognitionSettings":
        glossary = config.get("stt.glossary", [])
        vad = config.get("stt.vad", VAD_SILERO)
        mode = "meeting" if meeting else str(config.get("audio.trigger_mode", "hotkey") or "hotkey")
        resolved_source = (
            input_source if input_source is not None else audio_source(config, meeting=meeting)
        )
        policy = resolve_mode_recognition_policy(mode, resolved_source, config)
        return cls(
            model_id=str(config.get("stt.model_id", DEFAULT_MODEL_ID) or DEFAULT_MODEL_ID),
            glossary=deepcopy(glossary) if isinstance(glossary, list) else [],
            language=str(config.get("stt.language", "auto") or "auto"),
            vad=vad if vad in (VAD_SILERO, VAD_ENERGY) else VAD_SILERO,
            speaker_model_enabled=policy.speaker_model_enabled,
            speaker_activity_enabled=policy.speaker_activity_enabled,
            quiet_speech_enabled=policy.quiet_speech_mode is not False,
            quiet_speech_mode=policy.quiet_speech_mode,
            boundary_context_enabled=bool(config.get("stt.boundary_context_enabled", False)),
            trigger_mode=policy.trigger_mode,
            input_source=policy.input_source,
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
    def effective_hotwords(self) -> str:
        """Hotword string actually consumed by the active ASR model's decoder."""
        return self.hotwords if self.hotwords_supported else ""

    @property
    def effective_asr_language(self) -> str:
        """Language code actually consumed by the active ASR model's decoder."""
        if get_model_info(self.model_id) is None:
            return self.effective_language
        return self.effective_language if self.language_supported else ""

    @property
    def effective_boundary_context(self) -> bool:
        loader = (get_model_info(self.model_id) or {}).get("loader", "")
        return self.boundary_context_enabled and loader in {"fire_red_asr_ctc", "zipformer_ctc"}

    @property
    def speaker_key(self) -> tuple[bool, bool]:
        """Fields governing the resident CAM++ / speaker-activity embedder."""
        return (self.speaker_model_enabled, self.speaker_activity_enabled)

    @property
    def segmenter_key(self) -> tuple[str, bool | None, bool, bool, bool]:
        """Fields governing the dictation VAD / speech segmenter profile."""
        return (
            self.vad,
            self.quiet_speech_mode,
            self.quiet_speech_enabled,
            self.boundary_context_enabled,
            self.effective_boundary_context,
        )

    def asr_key(self, num_threads: int = 4) -> tuple[str, int, str, str]:
        """Fields governing the resident OfflineRecognizer instance."""
        return (
            self.model_id,
            int(num_threads or 4),
            self.effective_hotwords,
            self.effective_asr_language,
        )

    @property
    def notes(self) -> list[str]:
        notes = []
        if (self.boundary_context_enabled
                and (get_model_info(self.model_id) or {}).get("loader") not in {"fire_red_asr_ctc", "zipformer_ctc"}):
            notes.append("当前模型没有可用的词元时间戳，跨段上下文不生效；仍识别完整原片段。")
        if self.quiet_speech_mode is True and self.vad != VAD_SILERO:
            notes.append("轻声增强需要实际加载 Silero；能量断句不启用此增强。")
        if self.hotwords and not self.hotwords_supported:
            notes.append("当前模型不支持识别期热词；术语仍可用于会议纪要。")
        if not self.language_supported:
            notes.append("当前模型不支持指定识别语言，将按模型自身能力识别。")
        return notes

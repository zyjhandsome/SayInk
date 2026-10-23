import hashlib
import logging
import re
import shutil
import threading
from pathlib import Path

import numpy as np
from PyQt6.QtCore import QObject, pyqtSignal, QThread

from sayink.audio_utils import MAX_SEGMENT_SEC, TARGET_SAMPLE_RATE
from sayink.runtime_status import ModelLoadPhase
from sayink.speaker_turns import RecognizedTurn, analyze_turns
from sayink.vad_segmenter import CUT_SEARCH_SEC

log = logging.getLogger("SayInk")

SAMPLE_RATE = TARGET_SAMPLE_RATE

# Default STT model for new installs, config fallback, and release EXE bundling.
DEFAULT_MODEL_ID = "funasr-nano"

# Former app-wide defaults; upgraded to DEFAULT_MODEL_ID once (see config.py migration).
LEGACY_DEFAULT_MODEL_IDS = frozenset({"qwen3-asr-0.6b", "fireredasr2-ctc"})

# Qwen3-ASR may emit XML-style delimiters; FireRedASR2 emits <sil>, <zh>, etc.
_ASR_TAG_PATTERNS = (
    re.compile(r"</?\s*asr_text\s*/?\s*>", re.IGNORECASE),
    re.compile(r"<\s*asr_text\b[^>]*>", re.IGNORECASE),
    re.compile(r"</\s*asr_text\b[^>]*>", re.IGNORECASE),
    re.compile(r"<\s*/\s*asr_text\b[^>]*>", re.IGNORECASE),
)
# FireRedASR2 / sherpa meta tokens in tokens.txt: <sil>, <zh>, <en>, <|zh|>,
# <zh-CN>, … Only known marker words and language / locale codes are removed;
# a spoken HTML tag ("请把 <div> 改成 <span>") or comparison ("a<b 并且 c>d")
# is speech and stays.
_ASR_META_TOKEN_PATTERN = re.compile(r"<\s*/?\s*(\|?)([A-Za-z_][A-Za-z0-9_\-]*)\|?\s*/?\s*>")
_ASR_MARKER_WORDS = frozenset({
    "sil", "unk", "blank", "pad", "eos", "sos", "bos", "s", "nospeech", "speech",
    "noise", "music", "laughter", "event", "asr_text", "withitn", "woitn",
})
_ASR_LANG_CODES = frozenset({
    "zh", "en", "ja", "ko", "yue", "de", "fr", "es", "it", "pt", "ru", "ar", "hi",
    "th", "vi", "id", "ms", "tr", "nl", "pl", "sv", "uk", "cs", "ro", "hu", "el",
    "he", "fa", "ur", "bn", "ta", "te", "kn", "ml", "tl", "sw", "cmn", "wuu",
    "nan", "hak", "jpn", "kor", "eng", "zho", "chi",
})
_ASR_LANG_TAG_RE = re.compile(r"^[a-z]{2,3}(?:[-_][A-Za-z]{2,4})?$")
# Qwen3-ASR prefixes every decode with "language Chinese<asr_text>". Only a
# known language name that sits right before <asr_text> or directly against
# CJK speech is the marker; "the language English is…" stays.
_QWEN_LANGUAGE_NAMES = (
    "None", "Chinese", "English", "Cantonese", "Arabic", "German", "French",
    "Spanish", "Portuguese", "Indonesian", "Italian", "Korean", "Russian",
    "Thai", "Vietnamese", "Japanese", "Turkish", "Hindi", "Malay", "Dutch",
    "Swedish", "Danish", "Finnish", "Polish", "Czech", "Filipino", "Persian",
    "Greek", "Romanian", "Hungarian", "Macedonian",
)
_QWEN_LANGUAGE_PREFIX_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])language\s*(?:" + "|".join(_QWEN_LANGUAGE_NAMES) + r")"
    r"(?:\s*(?=<\s*asr_text)|(?=[\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7af])|\s*$)",
    re.IGNORECASE,
)


def _is_asr_meta_token(match: re.Match) -> bool:
    piped, word = match.group(1), match.group(2)
    if piped:
        # <|zh|>, <|HAPPY|>, <|withitn|>: SenseVoice / sherpa always pipe-wrap.
        return True
    lowered = word.lower()
    if lowered in _ASR_MARKER_WORDS:
        return True
    if "-" in word or "_" in word:
        base = re.split(r"[-_]", word)[0].lower()
        return base in _ASR_LANG_CODES and bool(_ASR_LANG_TAG_RE.match(word))
    return lowered in _ASR_LANG_CODES


def _strip_meta_tokens(text: str) -> tuple[str, bool]:
    stripped = False

    def _replace(match: re.Match) -> str:
        nonlocal stripped
        if _is_asr_meta_token(match):
            stripped = True
            return ""
        return match.group(0)

    return _ASR_META_TOKEN_PATTERN.sub(_replace, text), stripped
# Some decoded silence markers lose their angle brackets ("/sil", "/sil>").
# ASCII word/path boundaries keep /silver, /sil.txt and URLs intact; Chinese
# speech can follow the marker directly without a space.
_ASR_SIL_FRAGMENT_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_./\\:-])/sil(?![A-Za-z0-9_./\\:-])(?:[ \t]*>)?",
    re.IGNORECASE,
)


def _is_speech_char(ch: str) -> bool:
    # Multilingual models also emit Arabic, Cyrillic, Indic scripts, etc.
    return ch.isalnum()


def normalize_asr_output(text: str) -> str:
    """Remove ASR model wrapper / meta tokens from recognition text."""
    if not text:
        return ""
    cleaned = text
    stripped_tags = False
    if _QWEN_LANGUAGE_PREFIX_PATTERN.search(cleaned):
        stripped_tags = True
        cleaned = _QWEN_LANGUAGE_PREFIX_PATTERN.sub("", cleaned)
    for pattern in _ASR_TAG_PATTERNS:
        if pattern.search(cleaned):
            stripped_tags = True
        cleaned = pattern.sub("", cleaned)
    cleaned, stripped_meta = _strip_meta_tokens(cleaned)
    stripped_tags = stripped_tags or stripped_meta
    if _ASR_SIL_FRAGMENT_PATTERN.search(cleaned):
        stripped_tags = True
        cleaned = _ASR_SIL_FRAGMENT_PATTERN.sub("", cleaned)
    # Text alone cannot distinguish decoder hallucinations from deliberate
    # repetition or code-switching. Preserve spoken content; remove only the
    # known engine markers above. This also keeps normalization idempotent.
    cleaned = cleaned.strip()
    if stripped_tags:
        log.debug("已剥离 ASR 标签，清洗后长度: %d", len(cleaned))
    if cleaned and not any(_is_speech_char(ch) for ch in cleaned):
        return ""
    return cleaned


# Fun-ASR-Nano and Qwen3-ASR keep only the start of an utterance once audio
# exceeds the exported KV window (about 20s). Every live mode (hold-to-talk
# included) is already segmented at MAX_SPEECH_SEC by vad_segmenter, so this
# is a safety net for audio handed in whole (queued clips, tools, tests).
CONTEXT_LIMITED_LOADERS = frozenset({"funasr_nano", "qwen3_asr"})
CONTEXT_SLICE_SEC = MAX_SEGMENT_SEC
CONTEXT_SLICE_OVERLAP_SEC = 0.4


def slice_window_for_loader(loader: str) -> tuple[float, float] | None:
    """Return (slice_sec, overlap_sec) for models that truncate long audio."""
    if loader in CONTEXT_LIMITED_LOADERS:
        return (CONTEXT_SLICE_SEC, CONTEXT_SLICE_OVERLAP_SEC)
    return None


# A slice boundary moves back to the quietest 100 ms frame inside this window
# (a breath or syllable gap) instead of landing mid-word at exactly slice_sec.
# Same window as the segmenter's quiet cut; that one works on its per-block
# energy log, this one on raw samples, so the search itself is not shared.
SLICE_CUT_SEARCH_SEC = CUT_SEARCH_SEC
SLICE_CUT_FRAME_SEC = 0.1


def _quiet_boundary(audio: np.ndarray, end: int, sample_rate: int) -> int:
    frame = max(1, int(sample_rate * SLICE_CUT_FRAME_SEC))
    search = int(sample_rate * SLICE_CUT_SEARCH_SEC)
    start = max(0, end - search)
    if end - start < frame * 2:
        return end
    best_end = end
    best_rms: float | None = None
    frame_end = start + frame
    while frame_end <= end:
        piece = audio[frame_end - frame:frame_end]
        rms = float(np.sqrt(np.mean(np.square(piece)))) if piece.size else 0.0
        if best_rms is None or rms <= best_rms:
            best_rms = rms
            best_end = frame_end
        frame_end += frame
    return best_end


def plan_audio_slices(
    audio: np.ndarray,
    sample_rate: int,
    slice_sec: float,
    overlap_sec: float,
    *,
    cut_at_quiet: bool = True,
) -> list[np.ndarray]:
    """Split audio into slices no longer than slice_sec, overlapping slightly.

    With ``cut_at_quiet`` each boundary is pulled back to the quietest frame of
    the last SLICE_CUT_SEARCH_SEC so the joins fall between words.
    """
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    if audio.size == 0:
        return []
    slice_len = max(1, int(round(slice_sec * sample_rate)))
    overlap = max(0, int(round(overlap_sec * sample_rate)))
    if overlap >= slice_len:
        overlap = max(0, slice_len // 5)
    if audio.size <= slice_len:
        return [audio]

    slices: list[np.ndarray] = []
    start = 0
    total = audio.size
    while start < total:
        end = min(total, start + slice_len)
        if cut_at_quiet and end < total:
            quiet = _quiet_boundary(audio, end, sample_rate)
            if quiet - start > overlap:
                end = quiet
        slices.append(audio[start:end])
        if end >= total:
            break
        next_start = end - overlap
        if next_start <= start:
            next_start = end
        start = next_start
    return slices


FUZZY_OVERLAP_MIN_CHARS = 6


def merge_slice_texts(parts: list[str], max_overlap_chars: int = 12) -> str:
    """Join slice transcripts, dropping a duplicated boundary from the overlap."""
    merged = ""
    for part in parts:
        piece = (part or "").strip()
        if not piece:
            continue
        if not merged:
            merged = piece
            continue
        limit = min(len(merged), len(piece), max_overlap_chars)
        cut = 0
        for size in range(limit, 0, -1):
            head = piece[:size]
            tail = merged[-size:]
            if head == tail:
                cut = size
                break
            # The overlap is decoded twice and the two passes rarely agree
            # letter for letter (两百个 / 两白个). Tolerate one mismatch per
            # five characters, but only for overlaps long enough that two
            # genuinely different phrases (前十五秒 / 后十五秒) cannot collide.
            if size >= FUZZY_OVERLAP_MIN_CHARS and sum(a != b for a, b in zip(head, tail, strict=True)) <= size // 5:
                cut = size
                break
        merged += piece[cut:]
    return merged


def join_segment_texts(parts: list[str]) -> str:
    """Join transcripts of separate utterances (no shared audio, so nothing to
    de-duplicate); keep a space between Latin words on either side."""
    joined = ""
    for part in parts:
        piece = (part or "").strip()
        if not piece:
            continue
        if joined and joined[-1].isascii() and joined[-1].isalnum() and piece[0].isascii() and piece[0].isalnum():
            joined += " "
        joined += piece
    return joined


HF_URL = "https://huggingface.co"
HF_MIRROR_URL = "https://hf-mirror.com"
DOWNLOAD_SOURCE_AUTO = "auto"
DOWNLOAD_SOURCES = {
    DOWNLOAD_SOURCE_AUTO: (HF_URL, HF_MIRROR_URL),
    "huggingface": (HF_URL,),
    "hf-mirror": (HF_MIRROR_URL,),
}
DOWNLOAD_SOURCE_LABELS = (
    (DOWNLOAD_SOURCE_AUTO, "自动（官方优先，连不上换镜像）"),
    ("huggingface", "Hugging Face 官方"),
    ("hf-mirror", "hf-mirror 国内镜像"),
)


def download_endpoints(source: str) -> tuple[str, ...]:
    return DOWNLOAD_SOURCES.get(source or DOWNLOAD_SOURCE_AUTO, DOWNLOAD_SOURCES[DOWNLOAD_SOURCE_AUTO])

# ── Model Registry ────────────────────────────────────────────────
#
# ``supports`` lists the per-model knobs the meeting mode can use
# (docs/meeting-notes-design.md §5.8): "hotwords" (glossary injected into the
# decoder prompt) and "language" (lock the transcription language).
#
# ``revision`` pins downloads to one commit of ``hf_repo`` and ``sha256`` is
# checked for every file, so a later push to a (third-party) repository or a
# mirror serving other bytes cannot change what users install. Moving to a new
# upload means updating both.

SUPPORTS_HOTWORDS = "hotwords"
SUPPORTS_LANGUAGE = "language"

MODEL_REGISTRY = [
    {
        "id": "funasr-nano",
        "name": "Fun-ASR-Nano",
        "description": "默认。中/英/日与方言，支持热词和识别语言",
        "accuracy": 5,
        "speed": 2,
        "languages": "中/英/日/方言",
        "size_mb": 950,
        "loader": "funasr_nano",
        "hf_repo": "csukuangfj/sherpa-onnx-funasr-nano-int8-2025-12-30",
        "dir_name": "sherpa-onnx-funasr-nano-int8-2025-12-30",
        "revision": "6f16bd378457e13f36ccf3910df9017f96c346fb",
        "sha256": {
            "encoder_adaptor.int8.onnx": "f36dea2e30fbc33b5db1d7a7265cc976c5e5586c77b042d5adb1ad27c72db422",
            "llm.int8.onnx": "dfbf9aa3be41bccc257587f151e15c63fbe1b549f2b517f5ccd5bdce3bf4322a",
            "embedding.int8.onnx": "95e61cd0c9c3b9543339a4cf973c95c116815e745ccc1e0285cbd81f76d18644",
            "Qwen3-0.6B/vocab.json": "ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910",
            "Qwen3-0.6B/merges.txt": "8831e4f1a044471340f7c0a83d7bd71306a5b867e95fd870f74d0c5308a904d5",
            "Qwen3-0.6B/tokenizer.json": "aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4",
        },
        "files": [
            "encoder_adaptor.int8.onnx",
            "llm.int8.onnx",
            "embedding.int8.onnx",
            "Qwen3-0.6B/vocab.json",
            "Qwen3-0.6B/merges.txt",
            "Qwen3-0.6B/tokenizer.json",
        ],
        "supports": (SUPPORTS_HOTWORDS, SUPPORTS_LANGUAGE),
    },
    {
        "id": "sensevoice",
        "name": "SenseVoice",
        "description": "更快。中/英/日/韩/粤，支持识别语言",
        "accuracy": 3,
        "speed": 5,
        "languages": "中/英/日/韩/粤",
        "size_mb": 230,
        "loader": "sense_voice",
        "hf_repo": "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17",
        "dir_name": "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17",
        "revision": "2365baeacb507f821a0c8120fcee3d484dba7a07",
        "sha256": {
            "model.int8.onnx": "c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51",
            "tokens.txt": "f449eb28dc567533d7fa59be34e2abca8784f771850c78a47fb731a31429a1dc",
        },
        "files": ["model.int8.onnx", "tokens.txt"],
        "supports": (SUPPORTS_LANGUAGE,),
    },
    {
        "id": "qwen3-asr-0.6b",
        "name": "Qwen3-ASR 0.6B",
        "description": "多语种，支持热词",
        "accuracy": 4,
        "speed": 2,
        "languages": "中/英/多语种",
        "size_mb": 983,
        "loader": "qwen3_asr",
        "hf_repo": "csukuangfj2/sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25",
        "dir_name": "sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25",
        "revision": "68818b2313fe77bd06f6a7c5068ff3ef59d02b8a",
        "sha256": {
            "conv_frontend.onnx": "d22dc4423e0940e49884e903d2ea2f7e5567c14fc1aed97e4e26d6b8f208ef9e",
            "encoder.int8.onnx": "60748d3e6744a57c9c91e1b17424a6c2990567e8adceb0783940c03ed98fa9d9",
            "decoder.int8.onnx": "4f6885be5959ae26af3089d38ee7972c5fafbeeb1cf8d5e76eab6d8b61ca5771",
            "tokenizer/vocab.json": "ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910",
            "tokenizer/merges.txt": "8831e4f1a044471340f7c0a83d7bd71306a5b867e95fd870f74d0c5308a904d5",
            "tokenizer/tokenizer_config.json": "4942d005604266809309cabc9f4e9cb89ce855d59b14681fdc0e1cc62ea26c4c",
        },
        "files": [
            "conv_frontend.onnx",
            "encoder.int8.onnx",
            "decoder.int8.onnx",
            "tokenizer/vocab.json",
            "tokenizer/merges.txt",
            "tokenizer/tokenizer_config.json",
        ],
        "supports": (SUPPORTS_HOTWORDS,),
    },
    {
        "id": "fireredasr2-ctc",
        "name": "FireRedASR2",
        "description": "中文和方言候选（CTC 分支）",
        "accuracy": 4,
        "speed": 3,
        "languages": "中/英/方言",
        "size_mb": 740,
        "loader": "fire_red_asr_ctc",
        "hf_repo": "csukuangfj2/sherpa-onnx-fire-red-asr2-ctc-zh_en-int8-2026-02-25",
        "dir_name": "sherpa-onnx-fire-red-asr2-ctc-zh_en-int8-2026-02-25",
        "revision": "423be1acfdc5d8aaa5a485fb6263fe4ea8570b06",
        "sha256": {
            "model.int8.onnx": "ca3dbabd82170110cc0b343c2890866d449984bc9cd92b9a18371ff80a81bb99",
            "tokens.txt": "1bc613de2112d257e61a349c3e72d1b1a9cf19c33d3ca954197ad2171e5ea07b",
        },
        "files": ["model.int8.onnx", "tokens.txt"],
    },
    {
        "id": "fireredasr2-aed",
        "name": "FireRedASR2 AED",
        "description": "中文和方言的较慢精度候选",
        "accuracy": 4,
        "speed": 2,
        "languages": "中/英/方言",
        "size_mb": 1234,
        "loader": "fire_red_asr",
        "hf_repo": "csukuangfj2/sherpa-onnx-fire-red-asr2-zh_en-int8-2026-02-26",
        "dir_name": "sherpa-onnx-fire-red-asr2-zh_en-int8-2026-02-26",
        "revision": "374cff185e952c40fcf2f6da972a3b6cf340608d",
        "sha256": {
            "encoder.int8.onnx": "54048d66b6e8f3c80ea7ce95efe794587b0fd81d7271651d0decd3803852ae82",
            "decoder.int8.onnx": "b840ce7196ae4a14d05ae84bbf56082b6b61ccec5610fda907dddbcea37354ff",
            "tokens.txt": "1bc613de2112d257e61a349c3e72d1b1a9cf19c33d3ca954197ad2171e5ea07b",
        },
        "files": ["encoder.int8.onnx", "decoder.int8.onnx", "tokens.txt"],
    },
    {
        "id": "paraformer-zh",
        "name": "Paraformer 中文",
        "description": "小体积中英识别",
        "accuracy": 4,
        "speed": 4,
        "languages": "中/英",
        "size_mb": 240,
        "loader": "paraformer",
        "hf_repo": "csukuangfj/sherpa-onnx-paraformer-zh-2024-03-09",
        "dir_name": "sherpa-onnx-paraformer-zh-2024-03-09",
        "revision": "906992d326ebf0c5171cde675aa0902be9e5bc6c",
        "sha256": {
            "model.int8.onnx": "90bc03034ae1bef9575f8cc798cd1519c8be8aa9e8b458a033e32017ff4d584c",
            "tokens.txt": "6c0e3b35cece259829e6cb5b8d90d13db88f61ea3a2953d11898e4b2bfd7a2e2",
        },
        "files": ["model.int8.onnx", "tokens.txt"],
    },
    {
        "id": "paraformer-trilingual",
        "name": "Paraformer 三语",
        "description": "中英粤；需要粤语时再选",
        "accuracy": 4,
        "speed": 4,
        "languages": "中/英/粤",
        "size_mb": 240,
        "loader": "paraformer",
        "hf_repo": "csukuangfj/sherpa-onnx-paraformer-trilingual-zh-cantonese-en",
        "dir_name": "sherpa-onnx-paraformer-trilingual-zh-cantonese-en",
        "revision": "8d90151338178bb433354c9fb677bd3acb8023cd",
        "sha256": {
            "model.int8.onnx": "eb3cdd288f535cf73258f491cdd7d68ad5a00aee135c0bba4c0884ea8d926144",
            "tokens.txt": "8e4593d7a2eb2404ff82976b5494265e9a06283ca4d5e8605bf7b4fed557a492",
        },
        "files": ["model.int8.onnx", "tokens.txt"],
    },
    {
        "id": "zipformer-ctc-zh",
        "name": "Zipformer CTC",
        "description": "纯中文，无英文和热词",
        "accuracy": 3,
        "speed": 5,
        "languages": "中",
        "size_mb": 367,
        "loader": "zipformer_ctc",
        "hf_repo": "csukuangfj/sherpa-onnx-zipformer-ctc-zh-int8-2025-07-03",
        "dir_name": "sherpa-onnx-zipformer-ctc-zh-int8-2025-07-03",
        "revision": "d5f3d5789fb467496dbef554e3fb6f177a65bc5a",
        "sha256": {
            "model.int8.onnx": "e291b9c468b651e2697caa09bc684326c3addc6a019e78eb537cfd1a8248ca07",
            "tokens.txt": "6fed8c6c248516f38e7faa19404b57413e8ce259f1cbc1fa4aebc86eac32fdfd",
        },
        "files": ["model.int8.onnx", "tokens.txt"],
    },
    {
        "id": "qwen3-asr-1.7b",
        "name": "Qwen3-ASR 1.7B",
        "description": "多语种大参数版，较慢；权重来自第三方仓库",
        "accuracy": 4,
        "speed": 1,
        "languages": "中/英/多语种",
        "size_mb": 2400,
        "loader": "qwen3_asr",
        "hf_repo": "ilmina/qwen3-asr-1.7b-sherpa-onnx",
        "dir_name": "sherpa-onnx-qwen3-asr-1.7B-int8-2026-03-25",
        "revision": "66fb5ea2d4d1682ff8a663bf7e788913604996a0",
        "sha256": {
            "conv_frontend.onnx": "fa894a4ba53da6a4238f2a6ca0b09362e505d39cecbd646051b033e2e8d7e2fb",
            "encoder.int8.onnx": "436fbd910a0c8914851e5ac1354e807be9f283d08a5da728adaa609731c41469",
            "decoder.int8.onnx": "c43c853fa6e97d08365cb8a5502b360b595cd43c00dc60e4d8ca7cc18cad460b",
            "tokenizer/vocab.json": "ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910",
            "tokenizer/merges.txt": "8831e4f1a044471340f7c0a83d7bd71306a5b867e95fd870f74d0c5308a904d5",
            "tokenizer/tokenizer_config.json": "4942d005604266809309cabc9f4e9cb89ce855d59b14681fdc0e1cc62ea26c4c",
        },
        "files": [
            "conv_frontend.onnx",
            "encoder.int8.onnx",
            "decoder.int8.onnx",
            "tokenizer/vocab.json",
            "tokenizer/merges.txt",
            "tokenizer/tokenizer_config.json",
        ],
        "supports": (SUPPORTS_HOTWORDS,),
    },
]

# Auxiliary models used by the meeting mode (not speech recognizers; never
# offered as the STT model). They share the download / storage machinery.
AUX_MODEL_SPEAKER = "speaker-campplus"
AUX_MODEL_SILERO_VAD = "silero-vad"
AUX_MODEL_SPEAKER_ACTIVITY = "speaker-activity"
AUX_MODEL_REGISTRY = [
    {
        "id": AUX_MODEL_SPEAKER,
        "name": "说话人嵌入 CAM++",
        "description": "3D-Speaker CAM++ 中英声纹模型，持续转写与会议共用的说话人增强",
        "languages": "中/英",
        "size_mb": 28,
        "loader": "speaker_embedding",
        "hf_repo": "csukuangfj/speaker-embedding-models",
        "dir_name": "3dspeaker-campplus-zh-en",
        "revision": "0743f301363dec56491a490f6d6cbc9d67f9a3bf",
        "sha256": {
            "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx": "aa3cfc16963a10586a9393f5035d6d6b57e98d358b347f80c2a30bf4f00ceba2",
        },
        "files": ["3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"],
        "aux": True,
    },
    {
        "id": AUX_MODEL_SILERO_VAD,
        "name": "Silero VAD",
        "description": "神经网络语音活动检测，单次转写、持续转写与会议共用的断句能力",
        "languages": "通用",
        "size_mb": 2,
        "loader": "silero_vad",
        "hf_repo": "csukuangfj/vad",
        "dir_name": "silero-vad",
        "revision": "fba88cd2e921609e7675c3aaf51e0b9b295da4bc",
        "sha256": {
            "silero_vad.onnx": "a35ebf52fd3ce5f1469b2a36158dba761bc47b973ea3382b3186ca15b1f5af28",
        },
        "files": ["silero_vad.onnx"],
        "aux": True,
    },
    {
        "id": AUX_MODEL_SPEAKER_ACTIVITY,
        "name": "多人发言检测（实验性）",
        "description": "检测疑似重叠与未分开的多人发言，阻止这些片段训练单人声音档案；不分离声音",
        "languages": "通用",
        "size_mb": 2,
        "loader": "speaker_activity",
        "hf_repo": "csukuangfj/sherpa-onnx-pyannote-segmentation-3-0",
        "dir_name": "pyannote-segmentation-3-0",
        "revision": "340b52f1f5cd12d45a30fa284691417eaad2ff92",
        "sha256": {
            "model.int8.onnx": "10a438c2e0d90ed5f5da545cec2244d887315f6dbbbf1d3d564d00745b01952e",
            "LICENSE": "14d7016ad68e7394d6e6b78d96cc2ae431c905287b89674cfdf021e79e62b8ba",
        },
        "files": ["model.int8.onnx", "LICENSE"],
        "aux": True,
        # Shipped in both installers. Runtime use stays off until the user
        # enables it, and a missing copy does not block a meeting.
        "optional": True,
        "bundle": True,
    },
]


def get_model_info(model_id: str) -> dict | None:
    for m in MODEL_REGISTRY:
        if m["id"] == model_id:
            return m
    for m in AUX_MODEL_REGISTRY:
        if m["id"] == model_id:
            return m
    return None


def is_aux_model(model_id: str) -> bool:
    info = get_model_info(model_id)
    return bool(info and info.get("aux"))


def model_supports(model_id: str, feature: str) -> bool:
    info = get_model_info(model_id)
    return bool(info and feature in info.get("supports", ()))


# Fun-ASR-Nano takes a language *name* that is spliced into its prompt
# (「语音转写：中文」); SenseVoice takes an ISO-ish code.
_FUNASR_NANO_LANGUAGE_NAMES = {"zh": "中文", "en": "英文", "ja": "日文", "yue": "粤语"}
_SENSE_VOICE_LANGUAGE_CODES = {"zh": "zh", "en": "en", "ja": "ja", "ko": "ko", "yue": "yue"}


def language_argument(model_id: str, language: str) -> str:
    """Translate a config language ('auto'/'zh'/'en'/…) to the loader's value;
    empty string when the model takes none or the language is 'auto'."""
    code = (language or "").strip().lower()
    if code in ("", "auto"):
        return ""
    info = get_model_info(model_id)
    if not info:
        return ""
    loader = info.get("loader", "")
    if loader == "funasr_nano":
        return _FUNASR_NANO_LANGUAGE_NAMES.get(code, "")
    if loader == "sense_voice":
        return _SENSE_VOICE_LANGUAGE_CODES.get(code, "")
    return ""


def effective_language(language: str) -> str:
    """The config language with 「auto」 resolved: Chinese first, for dictation
    and meetings alike (both must agree or a meeting start reloads the model)."""
    code = (language or "").strip().lower()
    return "zh" if code in ("", "auto") else code


def recognition_params(config) -> tuple[str, str]:
    """``(hotwords, language)`` for :meth:`SpeechRecognizer.configure` from the
    shared ``stt.glossary`` / ``stt.language`` settings."""
    from sayink.recognition_settings import RecognitionSettings

    shared = RecognitionSettings.from_config(config)
    return shared.hotwords, shared.effective_language


MAX_HOTWORDS = 50
MAX_HOTWORD_CHARS = 20


def format_hotwords(glossary) -> str:
    """Glossary entries (``{"term": ..., "variants": [...]}`` or plain strings)
    → the comma-separated hotword string Fun-ASR-Nano / Qwen3-ASR expect.

    Only the canonical term goes in: a hotword nudges the decoder towards that
    spelling, so listing the mis-heard variants would make them likelier."""
    seen: list[str] = []
    for entry in glossary or ():
        term = entry.get("term", "") if isinstance(entry, dict) else entry
        # Commas separate hotwords; any other whitespace, including a newline
        # pasted into the file, would split one term across decoder lines.
        term = str(term or "").replace(",", " ").replace("，", " ")
        term = " ".join(term.split())
        if not term or len(term) > MAX_HOTWORD_CHARS or term in seen:
            continue
        seen.append(term)
        if len(seen) >= MAX_HOTWORDS:
            break
    return ",".join(seen)


def restore_glossary_case(text: str, glossary) -> str:
    """Spell glossary terms the way the glossary does.

    Some models only know one case for English (FireRedASR2 says ``GITHUB``),
    and models without hotword support never see the glossary at all. A whole
    word that matches a term case-insensitively takes the term's spelling;
    longer terms win, so ``code`` does not re-case ``Visual Studio Code``.
    """
    spelling: dict[str, str] = {}
    for entry in glossary or ():
        term = entry.get("term", "") if isinstance(entry, dict) else entry
        term = str(term or "").strip()
        if re.search(r"[A-Za-z]", term):
            spelling.setdefault(term.lower(), term)
    if not text or not spelling:
        return text
    alternatives = "|".join(re.escape(t) for t in sorted(spelling.values(), key=len, reverse=True))
    pattern = re.compile(rf"(?<![A-Za-z])(?:{alternatives})(?![A-Za-z])", re.IGNORECASE)
    return pattern.sub(lambda m: spelling.get(m.group(0).lower(), m.group(0)), text)


_custom_models_dir: Path | None = None


def set_models_dir(path: Path | None) -> bool:
    """Use ``path`` for models; ``None`` restores the default location.

    A configured folder that cannot be created (removed drive, revoked
    permission) must not take the whole app down at startup: the default
    directory is used instead and False is returned so the caller can tell
    the user.
    """
    global _custom_models_dir
    if path:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("模型目录 %s 不可用，改用默认目录: %s", path, exc)
            _custom_models_dir = None
            return False
    _custom_models_dir = path
    return True


def model_dir_names() -> list[str]:
    """Folder names SayInk itself creates under the models directory."""
    return [info["dir_name"] for info in (*MODEL_REGISTRY, *AUX_MODEL_REGISTRY)]


def is_dir_writable(path: Path) -> bool:
    """Probe by writing a file: os.access ignores ACLs on Windows."""
    import tempfile

    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=str(path)):
            pass
        return True
    except OSError:
        return False


def user_models_dir() -> Path:
    from sayink.user_data import user_data_dir

    return user_data_dir() / "models"


def default_models_dir() -> Path:
    """Where downloads go when the user has not picked a folder.

    The installer puts the bundled model under Program Files, which a normal
    user cannot write; that copy is still found by _get_portable_model_dir.
    """
    import sys

    if hasattr(sys, '_MEIPASS'):
        install_models = Path(sys._MEIPASS).parent / "models"
        if is_dir_writable(install_models):
            return install_models
    return user_models_dir()


def _get_models_dir() -> Path:
    if _custom_models_dir:
        return _custom_models_dir
    return default_models_dir()


def _get_portable_model_dir(model_id: str) -> Path | None:
    """Look for models in portable locations: next to exe or project root."""
    import sys
    info = get_model_info(model_id)
    if not info:
        return None

    candidates = []
    if hasattr(sys, '_MEIPASS'):
        candidates.append(Path(sys._MEIPASS).parent / "models" / info["dir_name"])
    candidates.append(Path(__file__).parent.parent / "models" / info["dir_name"])

    for d in candidates:
        if d.exists() and all((d / f).exists() for f in info["files"]):
            return d
    return None


def get_model_dir(model_id: str) -> Path:
    portable = _get_portable_model_dir(model_id)
    if portable:
        return portable
    info = get_model_info(model_id)
    if not info:
        raise ValueError(f"未知模型: {model_id}")
    return _get_models_dir() / info["dir_name"]


def model_file_url(endpoint: str, info: dict, filename: str) -> str:
    return f"{endpoint}/{info['hf_repo']}/resolve/{info['revision']}/{filename}"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def mismatched_model_files(info: dict, folder: Path) -> list[str]:
    """Files of ``info`` under ``folder`` that are missing or differ from the pins."""
    return [
        name for name in info["files"]
        if not (folder / name).is_file() or file_sha256(folder / name) != info["sha256"][name]
    ]


_LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/"
_LFS_POINTER_MAX_BYTES = 1024


def _is_usable_model_file(path: Path) -> bool:
    """A real weight / token file, not a git-lfs pointer or an empty stub.

    A checkout without ``git lfs pull`` leaves ~130-byte pointer files where
    the models should be; treating those as 「已下载」 made CI pass on nothing.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size == 0:
        return False
    if size <= _LFS_POINTER_MAX_BYTES:
        try:
            with path.open("rb") as handle:
                head = handle.read(len(_LFS_POINTER_PREFIX))
        except OSError:
            return False
        if head == _LFS_POINTER_PREFIX:
            return False
    return True


def is_model_downloaded(model_id: str) -> bool:
    if _get_portable_model_dir(model_id):
        return True
    info = get_model_info(model_id)
    if not info:
        return False
    d = _get_models_dir() / info["dir_name"]
    return all(_is_usable_model_file(d / f) for f in info["files"])


def get_downloaded_models() -> list[str]:
    return [m["id"] for m in MODEL_REGISTRY if is_model_downloaded(m["id"])]


def resolve_startup_model_id(configured_id: str | None) -> str:
    """Choose which STT model to load at startup.

    Uses the saved config when that model is on disk; otherwise prefers
    DEFAULT_MODEL_ID, then any other downloaded model.
    """
    configured = (configured_id or "").strip() or DEFAULT_MODEL_ID

    if is_model_downloaded(configured):
        return configured

    if configured != DEFAULT_MODEL_ID and is_model_downloaded(DEFAULT_MODEL_ID):
        log.info("配置的模型 %s 未下载，改用默认模型 %s", configured, DEFAULT_MODEL_ID)
        return DEFAULT_MODEL_ID

    downloaded = get_downloaded_models()
    if downloaded:
        fallback = downloaded[0]
        log.warning(
            "配置的模型 %s 未下载，暂时使用已下载模型 %s（请在设置中下载 %s）",
            configured,
            fallback,
            DEFAULT_MODEL_ID,
        )
        return fallback

    return configured


def delete_model(model_id: str) -> bool:
    if _get_portable_model_dir(model_id):
        return False
    info = get_model_info(model_id)
    if not info:
        return False
    d = _get_models_dir() / info["dir_name"]
    if not d.exists():
        return False
    shutil.rmtree(d, ignore_errors=True)
    if d.exists():
        log.warning("模型目录未能完全删除（可能被占用或无权限）: %s", d)
        return False
    return True


# ── Workers ───────────────────────────────────────────────────────

class _DownloadCancelled(Exception):
    pass


class IncompleteDownloadError(IOError):
    pass


class ChecksumMismatchError(IOError):
    pass


class ModelDownloadWorker(QThread):
    """Downloads model files from HuggingFace (or a mirror)."""
    progress = pyqtSignal(int)
    finished_ok = pyqtSignal(str)  # model_id
    error = pyqtSignal(str)

    def __init__(self, model_id: str, source: str = DOWNLOAD_SOURCE_AUTO):
        super().__init__()
        self._model_id = model_id
        self._endpoints = download_endpoints(source)
        self._cancelled = False
        self._last_emit_pct = -1
        self._expected_bytes = 0
        self._done_bytes = 0

    def cancel(self):
        """Set cancellation flag to stop download."""
        self._cancelled = True

    def _emit_pct(self, pct: int) -> None:
        if pct > self._last_emit_pct:
            self._last_emit_pct = pct
            self.progress.emit(pct)

    def _emit_progress(self, index: int, total_files: int, file_fraction: float, file_bytes: int) -> None:
        # One large decoder dominates the byte count, so equal per-file weights
        # would stall the bar for most of the download. size_mb is approximate:
        # cap below 100 until every file is in place.
        if self._expected_bytes > 0:
            pct = int((self._done_bytes + file_bytes) / self._expected_bytes * 100)
            self._emit_pct(min(pct, 99))
        else:
            self._emit_pct(int((index + file_fraction) / total_files * 100))

    def _download_file(
        self, url: str, target: Path, index: int, total_files: int, expected_sha256: str
    ) -> None:
        import httpx

        tmp_path = target.with_name(target.name + ".tmp")
        try:
            digest = hashlib.sha256()
            with httpx.stream("GET", url, timeout=60.0, follow_redirects=True) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length", 0))
                downloaded = 0
                with open(tmp_path, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=1024 * 256):
                        if self._cancelled:
                            raise _DownloadCancelled()
                        f.write(chunk)
                        digest.update(chunk)
                        downloaded += len(chunk)
                        fraction = downloaded / total if total > 0 else 0.0
                        self._emit_progress(index, total_files, fraction, downloaded)
            if total > 0 and downloaded != total:
                raise IncompleteDownloadError(
                    f"{target.name} 不完整（{downloaded} / {total} 字节）"
                )
            if downloaded == 0:
                raise IncompleteDownloadError(f"{target.name} 为空文件")
            if digest.hexdigest() != expected_sha256:
                raise ChecksumMismatchError(f"{target.name} 校验失败：内容与固定版本不一致")
            tmp_path.replace(target)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    def run(self):
        try:
            info = get_model_info(self._model_id)
            if not info:
                self.error.emit(f"未知模型: {self._model_id}")
                return

            model_dir = _get_models_dir() / info["dir_name"]
            model_dir.mkdir(parents=True, exist_ok=True)

            files = info["files"]
            total_files = len(files)
            self._expected_bytes = int(info.get("size_mb") or 0) * 1024 * 1024

            for i, filename in enumerate(files):
                if self._cancelled:
                    raise _DownloadCancelled()

                target = model_dir / filename
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    # Keep a leftover only when it is byte-for-byte the pinned
                    # file. An empty stub, git-lfs pointer or truncated earlier
                    # download used to be "kept", so every retry re-failed with
                    # 「模型文件不完整」 without a single network call.
                    if (
                        _is_usable_model_file(target)
                        and file_sha256(target) == info["sha256"][filename]
                    ):
                        self._done_bytes += target.stat().st_size
                        self._emit_progress(i, total_files, 1.0, 0)
                        continue
                    log.warning("模型文件 %s 已存在但校验不通过，重新下载", target.name)
                    target.unlink()

                last_err: Exception | None = None
                for endpoint in self._endpoints:
                    url = model_file_url(endpoint, info, filename)
                    log.info("下载: %s", url)
                    try:
                        self._download_file(url, target, i, total_files, info["sha256"][filename])
                        self._done_bytes += target.stat().st_size
                        last_err = None
                        break
                    except _DownloadCancelled:
                        raise
                    except Exception as e:
                        last_err = e
                        log.warning("从 %s 下载失败: %s", endpoint, e)
                if last_err is not None:
                    raise last_err

            if self._cancelled:
                return

            if is_model_downloaded(self._model_id):
                log.info("模型下载完成: %s", info["name"])
                self._emit_pct(100)
                self.finished_ok.emit(self._model_id)
            else:
                self.error.emit("模型文件不完整，请重试")

        except _DownloadCancelled:
            log.info("模型下载已取消")
            self.error.emit("下载已取消")
        except Exception as e:
            log.error("模型下载失败: %s", e)
            hint = "" if len(self._endpoints) > 1 else "；可在「下载源」改用其他来源"
            self.error.emit(f"下载失败: {e}{hint}")


def _create_recognizer(model_id: str, num_threads: int, *, hotwords: str = "", language: str = ""):
    """Create a sherpa_onnx.OfflineRecognizer for the given model.

    ``hotwords`` / ``language`` are applied only to loaders that take them
    (see ``supports`` in the registry); ``language`` is already the loader's
    own value (see :func:`language_argument`).
    """
    import sherpa_onnx

    info = get_model_info(model_id)
    if not info or info.get("aux"):
        raise ValueError(f"未知模型: {model_id}")

    model_dir = get_model_dir(model_id)
    loader = info["loader"]

    if loader == "sense_voice":
        return sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(model_dir / "model.int8.onnx"),
            tokens=str(model_dir / "tokens.txt"),
            num_threads=num_threads,
            language=language,
            use_itn=True,
            debug=False,
        )
    elif loader == "paraformer":
        return sherpa_onnx.OfflineRecognizer.from_paraformer(
            paraformer=str(model_dir / "model.int8.onnx"),
            tokens=str(model_dir / "tokens.txt"),
            num_threads=num_threads,
            debug=False,
        )
    elif loader == "fire_red_asr_ctc":
        return sherpa_onnx.OfflineRecognizer.from_fire_red_asr_ctc(
            model=str(model_dir / "model.int8.onnx"),
            tokens=str(model_dir / "tokens.txt"),
            num_threads=num_threads,
            debug=False,
        )
    elif loader == "fire_red_asr":
        return sherpa_onnx.OfflineRecognizer.from_fire_red_asr(
            encoder=str(model_dir / "encoder.int8.onnx"),
            decoder=str(model_dir / "decoder.int8.onnx"),
            tokens=str(model_dir / "tokens.txt"),
            num_threads=num_threads,
            debug=False,
        )
    elif loader == "zipformer_ctc":
        return sherpa_onnx.OfflineRecognizer.from_zipformer_ctc(
            model=str(model_dir / "model.int8.onnx"),
            tokens=str(model_dir / "tokens.txt"),
            num_threads=num_threads,
            debug=False,
        )
    elif loader == "qwen3_asr":
        return sherpa_onnx.OfflineRecognizer.from_qwen3_asr(
            conv_frontend=str(model_dir / "conv_frontend.onnx"),
            encoder=str(model_dir / "encoder.int8.onnx"),
            decoder=str(model_dir / "decoder.int8.onnx"),
            tokenizer=str(model_dir / "tokenizer"),
            num_threads=num_threads,
            hotwords=hotwords,
            debug=False,
        )
    elif loader == "funasr_nano":
        return sherpa_onnx.OfflineRecognizer.from_funasr_nano(
            encoder_adaptor=str(model_dir / "encoder_adaptor.int8.onnx"),
            llm=str(model_dir / "llm.int8.onnx"),
            embedding=str(model_dir / "embedding.int8.onnx"),
            tokenizer=str(model_dir / "Qwen3-0.6B"),
            num_threads=num_threads,
            language=language,
            hotwords=hotwords,
            debug=False,
        )
    else:
        raise ValueError(f"不支持的 loader: {loader}")


# (model_id, num_threads, hotwords, language): what a loaded recognizer was built from.
LoadTarget = tuple[str, int, str, str]


class ModelLoadWorker(QThread):
    """Loads an ASR model in a background thread."""
    loaded = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, model_id: str, num_threads: int = 4, *, hotwords: str = "", language: str = ""):
        super().__init__()
        self._model_id = model_id
        self._num_threads = num_threads
        self._hotwords = hotwords
        self._language = language

    def run(self):
        try:
            info = get_model_info(self._model_id)
            name = info["name"] if info else self._model_id
            log.info("正在加载语音识别模型 (%s)...", name)
            recognizer = _create_recognizer(
                self._model_id, self._num_threads,
                hotwords=self._hotwords, language=self._language,
            )
            log.info("语音识别模型加载完成: %s", name)
            self.loaded.emit(recognizer)
        except Exception as e:
            log.error("模型加载失败: %s", e)
            self.error.emit(f"模型加载失败: {e}")


def short_retry_audio(audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> np.ndarray | None:
    """Prepare one bounded retry for an empty short utterance.

    Remove long quiet edges while retaining 120 ms of context and add quiet
    padding for tiny clips. Never amplify noise, retry silence or loop retries.
    """
    samples = np.asarray(audio, dtype=np.float32).reshape(-1)
    frame = max(1, int(sample_rate * 0.02))
    if not frame <= samples.size <= sample_rate * 8 or not np.all(np.isfinite(samples)):
        return None
    blocks = samples[:samples.size // frame * frame].reshape(-1, frame)
    levels = np.sqrt(np.mean(blocks * blocks, axis=1))
    threshold = max(0.005, min(0.015, float(levels.max()) * 0.1))
    voiced = np.flatnonzero(levels >= threshold)
    if voiced.size < 5:
        return None
    margin = int(sample_rate * 0.12)
    start = max(0, int(voiced[0]) * frame - margin)
    stop = min(samples.size, (int(voiced[-1]) + 1) * frame + margin)
    clip = samples[start:stop]
    padding = max(int(sample_rate * 0.2), (sample_rate - clip.size + 1) // 2)
    return np.pad(clip, (padding, padding))


_REPEATED_ASR_UNIT = re.compile(r"(.{1,24}?)\1{7,}")


def _repetition_overflow(text: str, samples: int, sample_rate: int) -> bool:
    """A retry signal, never permission to delete repetitions from a transcript.

    Ordinary emphasis and repeated instructions fit comfortably under this
    generous speech-character budget. A long loop alone is not enough.
    """
    core = "".join(ch for ch in text if ch.isalnum())
    if len(core) <= 15 * samples / sample_rate + 20:
        return False
    return any(not match.group(1).isdigit() for match in _REPEATED_ASR_UNIT.finditer(core))


def prepare_quiet_asr_audio(audio: np.ndarray, sample_rate: int = SAMPLE_RATE, *, assume_speech=False) -> np.ndarray:
    """Raise clearly modulated, quiet speech without another native decode.

    This only affects ASR input; raw recording and speaker embeddings stay
    unchanged. Near silence, steady hum and normal-volume clips are untouched.
    """
    samples = np.asarray(audio, dtype=np.float32)
    frame = max(1, sample_rate // 50)
    if samples.size < frame * 10 or not np.all(np.isfinite(samples)):
        return samples
    levels = np.sqrt(np.mean(samples[:samples.size // frame * frame].reshape(-1, frame)**2, axis=1))
    low, high = np.percentile(levels, [20, 90])
    if not (0.00005 if assume_speech else 0.0005) <= high < 0.008:
        return samples
    if not assume_speech and high < max(low * 4, 0.0005):
        return samples
    gain = min(32.0 if assume_speech else 8.0, 0.02 / high,
               0.9 / max(float(np.max(np.abs(samples))), 1e-8))
    return samples * np.float32(gain) if gain > 1.2 else samples


def decode_asr_slice(recognizer, audio: np.ndarray, *, sample_rate: int = SAMPLE_RATE,
                     retry_empty: bool = False, cancelled=None, quiet_speech=False) -> str:
    """The same normalized, bounded decode policy for the app and benchmark."""
    def decode(piece):
        stream = recognizer.create_stream()
        stream.accept_waveform(sample_rate, prepare_quiet_asr_audio(piece, sample_rate, assume_speech=quiet_speech))
        recognizer.decode_stream(stream)
        return normalize_asr_output(stream.result.text)

    text = decode(audio)
    if _repetition_overflow(text, len(audio), sample_rate) and not (cancelled and cancelled()):
        # Ask the acoustics again, with bounded shorter context, instead of
        # assuming a repeated phrase was not actually spoken. Both halves
        # cover the entire original clip, sharing only a small seam.
        middle = len(audio) // 2
        margin = min(int(sample_rate * 0.2), middle // 2)
        pieces = [audio[:middle + margin], audio[middle - margin:]]
        try:
            parts = []
            for piece in pieces:
                if cancelled and cancelled():
                    return text
                parts.append(decode(piece))
            candidate = merge_slice_texts(parts)
            size = sum(ch.isalnum() for ch in candidate)
            if candidate and size <= 15 * len(audio) / sample_rate + 20:
                text = candidate
                log.info("异常重复片段已用完整音频分两片重新识别")
        except Exception as exc:
            log.warning("异常重复重识别失败，保留首次文字: %s", exc)
    if text or not retry_empty or (cancelled is not None and cancelled()):
        return text
    retry_source = prepare_quiet_asr_audio(audio, sample_rate, assume_speech=True) if quiet_speech else audio
    retry = short_retry_audio(retry_source, sample_rate)
    if retry is None:
        return text
    try:
        log.info("短句识别为空，使用保留上下文的音频重试一次")
        return decode(retry)
    except Exception as exc:
        log.warning("短句重试失败，保留首次识别结果: %s", exc)
        return text


class TranscribeWorker(QThread):
    """Runs transcription in a background thread."""
    result_ready = pyqtSignal(str)
    partial_ready = pyqtSignal(str)
    error = pyqtSignal(str)
    meeting_ready = pyqtSignal(object)

    def __init__(
        self,
        recognizer,
        audio_data: np.ndarray,
        *,
        max_slice_sec: float | None = None,
        overlap_sec: float = CONTEXT_SLICE_OVERLAP_SEC,
        retry_empty: bool = False,
        turn_analyzer=None,
    ):
        super().__init__()
        self._recognizer = recognizer
        self._audio_data = audio_data
        self._cancelled = False
        self._max_slice_sec = max_slice_sec
        self._overlap_sec = overlap_sec
        self._retry_empty = retry_empty
        self._turn_analyzer = turn_analyzer
        self.native_lock = None
        self.quiet_speech = False

    def cancel(self):
        """Set cancellation flag."""
        self._cancelled = True

    def _decode_slice(self, audio: np.ndarray) -> str:
        from sayink.asr_context import decode_owned_context
        if not self._cancelled and (len(getattr(audio, "prefix", ())) or len(getattr(audio, "suffix", ()))):
            try:
                text = decode_owned_context(self._recognizer, audio, normalize_asr_output,
                                             prepare=lambda window: prepare_quiet_asr_audio(window, assume_speech=self.quiet_speech))
                if text:
                    return text
            except Exception as exc:
                log.warning("跨段时间对齐不可用，识别原始片段: %s", exc)
        if self._cancelled:
            return ""
        options = {"quiet_speech": True} if self.quiet_speech else {}
        return decode_asr_slice(self._recognizer, audio, retry_empty=self._retry_empty,
                                cancelled=lambda: self._cancelled, **options)

    def run(self):
        if self.native_lock is None:
            self._run_locked()
            return
        while not self._cancelled:
            if self.native_lock.acquire(timeout=0.1):
                try:
                    self._run_locked()
                finally:
                    self.native_lock.release()
                return

    def _run_locked(self):
        if self._cancelled:
            return

        try:
            from sayink.asr_context import with_context
            audio = with_context(self._audio_data, getattr(self._audio_data, "prefix", None),
                                 getattr(self._audio_data, "suffix", None))
            if audio.ndim > 1:
                audio = audio[:, 0]

            # 验证音频数据有效性
            if audio.size == 0 or np.any(np.isnan(audio)) or np.any(np.isinf(audio)):
                self.error.emit("音频数据无效")
                return

            log.info("开始识别 (%.1f 秒音频)...", len(audio) / SAMPLE_RATE)
            if self._turn_analyzer is not None:
                self._run_meeting(audio)
                return
            if self._max_slice_sec and audio.size > int(self._max_slice_sec * SAMPLE_RATE):
                pieces = plan_audio_slices(
                    audio, SAMPLE_RATE, self._max_slice_sec, self._overlap_sec
                )
            else:
                pieces = [audio]

            texts: list[str] = []
            for index, piece in enumerate(pieces, start=1):
                if self._cancelled:
                    return
                if len(pieces) > 1:
                    log.info(
                        "识别分段 %d/%d (%.1f 秒)",
                        index,
                        len(pieces),
                        len(piece) / SAMPLE_RATE,
                    )
                texts.append(self._decode_slice(piece))
                merged = merge_slice_texts(texts)
                if merged:
                    self.partial_ready.emit(merged)

            if self._cancelled:
                return

            text = merge_slice_texts(texts) if len(pieces) > 1 else (texts[0] if texts else "")
            log.debug("识别结果长度: %d 字符", len(text))
            self.result_ready.emit(text)

        except Exception as e:
            log.error("识别失败: %s", e)
            self.error.emit(f"识别失败: {e}")

    def _run_meeting(self, audio: np.ndarray) -> None:
        turns = analyze_turns(audio, self._turn_analyzer, cancelled=lambda: self._cancelled)
        if self._cancelled:
            return
        results = []
        for turn in turns:
            if self._cancelled:
                return
            part = audio[turn.start:turn.stop]
            from sayink.asr_context import with_context
            part = with_context(part, audio.prefix if turn.start == 0 else None,
                                 audio.suffix if turn.stop == len(audio) else None)
            pieces = (plan_audio_slices(part, SAMPLE_RATE, self._max_slice_sec, self._overlap_sec)
                      if self._max_slice_sec and len(part) > int(self._max_slice_sec * SAMPLE_RATE) else [part])
            texts = []
            for piece in pieces:
                if self._cancelled:
                    return
                texts.append(self._decode_slice(piece))
            text = merge_slice_texts(texts) if len(texts) > 1 else texts[0]
            results.append(RecognizedTurn(turn.start, turn.stop, text, turn.embedding, turn.learnable))
            preview = join_segment_texts([t.text for t in results])
            if preview:
                self.partial_ready.emit(preview)
        if not self._cancelled:
            self.meeting_ready.emit(results)


# ── Main Recognizer ──────────────────────────────────────────────

class SpeechRecognizer(QObject):
    """Offline speech recognizer supporting multiple sherpa-onnx models."""
    supports_prefetch = True
    final_result = pyqtSignal(str)
    meeting_result = pyqtSignal(object)
    partial_result = pyqtSignal(str)
    error = pyqtSignal(str)
    ready = pyqtSignal()
    # (ModelLoadPhase, human-readable message)
    model_load_progress = pyqtSignal(object, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._recognizer = None
        self._current_worker = None
        self._load_worker = None
        self._reload_after_current = False
        self._loaded_target: LoadTarget | None = None
        self._is_ready = False
        self._model_id = ""
        self._num_threads = 4
        self._hotwords = ""
        self._language = ""
        # Monotonic request id: results from superseded/cancelled workers are
        # ignored instead of force-terminating C++/ONNX threads (see SD-05).
        self._request_seq = 0
        self._active_seq = 0
        self._workers: list[TranscribeWorker] = []
        self._decode_lock = threading.Lock()
        self.quiet_speech = False

    def _target(self) -> LoadTarget:
        return (self._model_id, self._num_threads, self._hotwords, self._language)

    def configure(
        self,
        model_id: str,
        num_threads: int = 4,
        *,
        hotwords: str = "",
        language: str = "",
    ):
        """Load ``model_id``; ``hotwords`` / ``language`` are silently dropped
        for models that do not take them (so the same call works for all)."""
        if not model_id:
            log.warning("未选择语音模型")
            return

        if not model_supports(model_id, SUPPORTS_HOTWORDS):
            hotwords = ""
        if not model_supports(model_id, SUPPORTS_LANGUAGE):
            language = ""
        language = language_argument(model_id, language) if language else ""
        wanted: LoadTarget = (model_id, num_threads, hotwords or "", language)

        if wanted == self._target() and self._is_ready and self._recognizer is not None:
            log.debug("语音模型已驻留内存，跳过重复加载: %s", model_id)
            return

        if wanted == self._target() and self.is_loading:
            log.debug("语音模型正在加载中，跳过重复请求: %s", model_id)
            return

        self._model_id, self._num_threads, self._hotwords, self._language = wanted

        if is_model_downloaded(model_id):
            self._load_model()
        else:
            log.warning("语音模型 %s 未下载", model_id)

    def _load_model(self):
        if self._load_worker and self._load_worker.isRunning():
            # A load thread cannot be interrupted; load the newer choice after it.
            self._reload_after_current = True
            return

        info = get_model_info(self._model_id)
        name = info["name"] if info else self._model_id
        self._is_ready = False
        self._reload_after_current = False
        self.model_load_progress.emit(ModelLoadPhase.LOADING, f"正在加载 {name}…")
        worker = ModelLoadWorker(
            self._model_id, self._num_threads,
            hotwords=self._hotwords, language=self._language,
        )
        target = self._target()
        worker.loaded.connect(lambda rec, t=target: self._on_model_loaded(rec, t))
        worker.error.connect(lambda msg, t=target: self._on_model_load_error(msg, t))
        worker.finished.connect(self._on_load_worker_finished)
        self._load_worker = worker
        worker.start()

    def _is_current_target(self, target: LoadTarget | None) -> bool:
        return target is None or target == self._target()

    def _on_model_loaded(self, recognizer, target: LoadTarget | None = None):
        if not self._is_current_target(target):
            log.info("丢弃已过时的模型加载结果: %s", target[0])
            return
        self._recognizer = recognizer
        self._loaded_target = self._target()
        self._is_ready = True
        self.model_load_progress.emit(ModelLoadPhase.READY, "模型已就绪")
        self.ready.emit()

    def _on_model_load_error(self, msg: str, target: LoadTarget | None = None):
        if not self._is_current_target(target):
            log.info("忽略已过时的模型加载错误: %s", msg)
            return
        self._is_ready = False
        # The App handles a failed load from this one signal; no separate error.
        self.model_load_progress.emit(ModelLoadPhase.FAILED, msg)

    def _on_load_worker_finished(self) -> None:
        worker = self._load_worker
        if worker is not None:
            worker.wait(2000)
            self._load_worker = None
        if not self._reload_after_current:
            return
        self._reload_after_current = False
        if self._is_ready and self._loaded_target == self._target():
            return
        if is_model_downloaded(self._model_id):
            self._load_model()

    def prefetch_final(self, audio: np.ndarray, *, on_text, on_turns, on_error, turn_analyzer=None):
        """Decode the next queued segment without publishing it as current output.

        Uses the same single worker slot and cancellation sequence as normal
        recognition; no second native decoder runs concurrently.
        """
        self.transcribe_final(audio, retry_empty=True, turn_analyzer=turn_analyzer,
                              _callbacks=(on_text, on_turns, on_error))

    def transcribe_final(self, full_audio: np.ndarray, *, retry_empty: bool = False,
                         turn_analyzer=None, _callbacks=None):
        if not self._is_ready or self._recognizer is None:
            message = "语音模型未就绪，请等待模型加载完成"
            if _callbacks is None:
                self.error.emit(message)
            else:
                _callbacks[2](message)
            return

        # Supersede any in-flight worker without force-terminating it: mark it
        # cancelled and bump the active sequence so its (late) result is ignored.
        # This avoids QThread.terminate() corrupting the shared sherpa-onnx /
        # ONNX Runtime state (SD-05).
        if self._current_worker is not None and self._current_worker.isRunning():
            log.warning("上一次转写仍在进行中，标记取消并忽略其过时结果...")
            self._current_worker.cancel()

        self._request_seq += 1
        seq = self._request_seq
        self._active_seq = seq

        info = get_model_info(self._model_id)
        loader = info.get("loader", "") if info else ""
        window = slice_window_for_loader(loader)
        if window is None:
            worker = TranscribeWorker(self._recognizer, full_audio, retry_empty=retry_empty, turn_analyzer=turn_analyzer)
        else:
            slice_sec, overlap_sec = window
            worker = TranscribeWorker(
                self._recognizer,
                full_audio,
                max_slice_sec=slice_sec,
                overlap_sec=overlap_sec,
                retry_empty=retry_empty,
                turn_analyzer=turn_analyzer,
            )
        worker.native_lock = self._decode_lock
        worker.quiet_speech = self.quiet_speech
        if _callbacks is None:
            worker.result_ready.connect(lambda text, s=seq: self._on_final_result(text, s))
            worker.partial_ready.connect(lambda text, s=seq: self._on_partial_result(text, s))
            worker.meeting_ready.connect(lambda turns, s=seq: self._on_meeting_result(turns, s))
            worker.error.connect(lambda e, s=seq: self._on_worker_error(e, s))
        else:
            on_text, on_turns, on_error = _callbacks
            worker.result_ready.connect(lambda text, s=seq: on_text(text) if s == self._active_seq else None)
            worker.meeting_ready.connect(lambda turns, s=seq: on_turns(turns) if s == self._active_seq else None)
            worker.error.connect(lambda e, s=seq: on_error(e) if s == self._active_seq else None)
        worker.finished.connect(lambda w=worker: self._reap_worker(w))
        self._current_worker = worker
        self._workers.append(worker)
        worker.start()

    def cancel_current(self) -> bool:
        """Discard the in-flight transcription (the user pressed Esc).

        The worker is asked to stop cooperatively and its sequence is retired,
        so a result or error that is already on its way is ignored. Returns
        True when a worker was actually running.
        """
        worker = self._current_worker
        running = worker is not None and worker.isRunning()
        if running:
            worker.cancel()
        self._active_seq = -1
        return running

    def _reap_worker(self, worker: "TranscribeWorker") -> None:
        try:
            self._workers.remove(worker)
        except ValueError:
            pass
        if worker is self._current_worker and not worker.isRunning():
            self._current_worker = None

    def _on_partial_result(self, text: str, seq: int):
        if seq != self._active_seq:
            return
        self.partial_result.emit(text)

    def _on_final_result(self, text: str, seq: int):
        if seq != self._active_seq:
            log.debug("忽略过时识别结果 (seq=%d, active=%d)", seq, self._active_seq)
            return
        self.final_result.emit(text)

    def _on_meeting_result(self, turns, seq: int):
        if seq == self._active_seq:
            self.meeting_result.emit(turns)

    def _on_worker_error(self, error_msg: str, seq: int):
        if seq != self._active_seq:
            log.debug("忽略过时识别错误 (seq=%d, active=%d)", seq, self._active_seq)
            return
        self.error.emit(error_msg)

    @property
    def running_workers(self):
        return tuple(worker for worker in [self._load_worker, *self._workers]
                     if worker is not None and worker.isRunning())

    def shutdown(self, *, wait_ms: int = 1000):
        """Stop all running workers for clean app exit."""
        self._reload_after_current = False
        if self._load_worker:
            try:
                self._load_worker.disconnect()
            except TypeError:
                pass
            if wait_ms and self._load_worker.isRunning():
                self._load_worker.wait(wait_ms)

        # Ignore any pending results and let running workers finish cooperatively.
        self._active_seq = -1
        for worker in list(self._workers):
            if worker.isRunning():
                worker.cancel()
                if wait_ms:
                    worker.wait(wait_ms)

    @property
    def is_ready(self) -> bool:
        return self._is_ready

    @property
    def is_loading(self) -> bool:
        return self._load_worker is not None and self._load_worker.isRunning()

    @property
    def current_model_id(self) -> str:
        return self._model_id

    @property
    def current_hotwords(self) -> str:
        return self._hotwords

    @property
    def current_language(self) -> str:
        return self._language

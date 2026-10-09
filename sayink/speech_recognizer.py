import logging
import re
import shutil
from pathlib import Path

import numpy as np
from PyQt6.QtCore import QObject, pyqtSignal, QThread

from sayink.runtime_status import ModelLoadPhase

log = logging.getLogger("SayInk")

SAMPLE_RATE = 16000

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
_SENTENCE_PATTERN = re.compile(r"[^。！？!?]+[。！？!?]?")
# A short token or clause repeated this many times is a decoder loop, not speech.
# The upper bound covers a clause like "然后你这个月又有点长了，" (12 chars).
_LOOP_MIN_REPEATS = 8
_LOOP_MIN_UNIT = 2
_LOOP_MAX_UNIT = 24
# How many characters survive when a loop is one character repeated (哈 × 16).
_SINGLE_CHAR_RUN_KEEP = 3


def _script_counts(text: str) -> tuple[int, int, int]:
    kana = hangul = cjk = 0
    for ch in text:
        code = ord(ch)
        if 0x3040 <= code <= 0x30FF or 0x31F0 <= code <= 0x31FF or 0xFF66 <= code <= 0xFF9D:
            kana += 1
        elif 0xAC00 <= code <= 0xD7AF:
            hangul += 1
        elif 0x4E00 <= code <= 0x9FFF:
            cjk += 1
    return kana, hangul, cjk


def _is_speech_char(ch: str) -> bool:
    code = ord(ch)
    if ch.isascii() and ch.isalnum():
        return True
    return (
        0x3040 <= code <= 0x30FF
        or 0x31F0 <= code <= 0x31FF
        or 0xFF66 <= code <= 0xFF9D
        or 0xAC00 <= code <= 0xD7AF
        or 0x4E00 <= code <= 0x9FFF
    )


def _excise_repetition_loops(text: str) -> str:
    """Drop a short token that the decoder emitted over and over."""
    if len(text) < _LOOP_MIN_UNIT * _LOOP_MIN_REPEATS:
        return text
    kept: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        cut_at = None
        for unit_len in range(_LOOP_MIN_UNIT, _LOOP_MAX_UNIT + 1):
            end = index + unit_len * _LOOP_MIN_REPEATS
            if end > length:
                continue
            unit = text[index:index + unit_len]
            if not any(_is_speech_char(ch) for ch in unit):
                continue
            repeats = 1
            cursor = index + unit_len
            while cursor + unit_len <= length and text[cursor:cursor + unit_len] == unit:
                repeats += 1
                cursor += unit_len
            if repeats >= _LOOP_MIN_REPEATS:
                cut_at = cursor
                if len(set(unit)) == 1:
                    # 哈哈哈…… / 对对对…… is laughter or emphasis, not a decoder
                    # loop; keep a natural-sounding run instead of deleting it.
                    kept.append(unit[0] * _SINGLE_CHAR_RUN_KEEP)
                break
        if cut_at is None:
            kept.append(text[index])
            index += 1
        else:
            index = cut_at
    return "".join(kept)


def _is_unrelated_foreign_sentence(sentence: str) -> bool:
    """A whole sentence that switched into Japanese or Korean."""
    kana, hangul, cjk = _script_counts(sentence)
    if hangul >= 4 and hangul >= cjk:
        return True
    return kana >= 3 and kana > cjk


def _drop_unrelated_foreign_sentences(text: str, *, decoder_glitch: bool = False) -> str:
    sentences = [s for s in _SENTENCE_PATTERN.findall(text) if s.strip()]
    kept = [s for s in sentences if not _is_unrelated_foreign_sentence(s)]
    if len(kept) == len(sentences):
        return text
    if not decoder_glitch and not any(_script_counts(s)[2] for s in kept):
        # Nothing Chinese to drift away from: the user spoke Japanese / Korean.
        return text
    return "".join(kept)


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
    deloop = _excise_repetition_loops(cleaned)
    # A decoder loop means the model derailed, so stray Japanese / Korean
    # around it is noise. Otherwise only drop a foreign sentence when the
    # utterance is otherwise Chinese (language drift), never a whole Japanese
    # utterance: Fun-ASR-Nano is a 中/英/日 model.
    cleaned = _drop_unrelated_foreign_sentences(deloop, decoder_glitch=deloop != cleaned)
    cleaned = cleaned.strip()
    if stripped_tags:
        log.debug("已剥离 ASR 标签，清洗后长度: %d", len(cleaned))
    if cleaned and not any(_is_speech_char(ch) for ch in cleaned):
        return ""
    return cleaned


# Fun-ASR-Nano and Qwen3-ASR keep only the start of an utterance once audio
# exceeds the exported KV window (about 20s). Continuous mode already slices
# at MAX_SPEECH_SEC (15 s) in vad_segmenter; this covers hold-to-talk, where
# a single held utterance has no upper bound.
CONTEXT_LIMITED_LOADERS = frozenset({"funasr_nano", "qwen3_asr"})
CONTEXT_SLICE_SEC = 15.0
CONTEXT_SLICE_OVERLAP_SEC = 0.4


def slice_window_for_loader(loader: str) -> tuple[float, float] | None:
    """Return (slice_sec, overlap_sec) for models that truncate long audio."""
    if loader in CONTEXT_LIMITED_LOADERS:
        return (CONTEXT_SLICE_SEC, CONTEXT_SLICE_OVERLAP_SEC)
    return None


# A slice boundary moves back to the quietest 100 ms frame inside this window
# (a breath or syllable gap) instead of landing mid-word at exactly slice_sec.
SLICE_CUT_SEARCH_SEC = 3.0
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
            if size >= FUZZY_OVERLAP_MIN_CHARS and sum(a != b for a, b in zip(head, tail)) <= size // 5:
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

MODEL_REGISTRY = [
    {
        "id": "sensevoice",
        "name": "SenseVoice",
        "description": "极速推理，多语种支持",
        "accuracy": 3,
        "speed": 5,
        "languages": "中/英/日/韩/粤",
        "size_mb": 230,
        "loader": "sense_voice",
        "hf_repo": "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17",
        "dir_name": "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17",
        "files": ["model.int8.onnx", "tokens.txt"],
    },
    {
        "id": "paraformer-zh",
        "name": "Paraformer 中文",
        "description": "中英文识别，体积小",
        "accuracy": 4,
        "speed": 4,
        "languages": "中/英",
        "size_mb": 240,
        "loader": "paraformer",
        "hf_repo": "csukuangfj/sherpa-onnx-paraformer-zh-2024-03-09",
        "dir_name": "sherpa-onnx-paraformer-zh-2024-03-09",
        "files": ["model.int8.onnx", "tokens.txt"],
    },
    {
        "id": "fireredasr2-ctc",
        "name": "FireRedASR2",
        "description": "侧重中文，含方言，速度适中",
        "accuracy": 5,
        "speed": 3,
        "languages": "中/英/方言",
        "size_mb": 740,
        "loader": "fire_red_asr_ctc",
        "hf_repo": "csukuangfj2/sherpa-onnx-fire-red-asr2-ctc-zh_en-int8-2026-02-25",
        "dir_name": "sherpa-onnx-fire-red-asr2-ctc-zh_en-int8-2026-02-25",
        "files": ["model.int8.onnx", "tokens.txt"],
    },
    {
        "id": "paraformer-trilingual",
        "name": "Paraformer 三语",
        "description": "中英粤三语支持",
        "accuracy": 4,
        "speed": 4,
        "languages": "中/英/粤",
        "size_mb": 240,
        "loader": "paraformer",
        "hf_repo": "csukuangfj/sherpa-onnx-paraformer-trilingual-zh-cantonese-en",
        "dir_name": "sherpa-onnx-paraformer-trilingual-zh-cantonese-en",
        "files": ["model.int8.onnx", "tokens.txt"],
    },
    {
        "id": "fireredasr2-aed",
        "name": "FireRedASR2 AED",
        "description": "FireRedASR2 编解码版，含方言（较慢）",
        "accuracy": 5,
        "speed": 2,
        "languages": "中/英/方言",
        "size_mb": 1234,
        "loader": "fire_red_asr",
        "hf_repo": "csukuangfj2/sherpa-onnx-fire-red-asr2-zh_en-int8-2026-02-26",
        "dir_name": "sherpa-onnx-fire-red-asr2-zh_en-int8-2026-02-26",
        "files": ["encoder.int8.onnx", "decoder.int8.onnx", "tokens.txt"],
    },
    {
        "id": "zipformer-ctc-zh",
        "name": "Zipformer CTC",
        "description": "轻量快速，纯中文识别",
        "accuracy": 3,
        "speed": 5,
        "languages": "中",
        "size_mb": 367,
        "loader": "zipformer_ctc",
        "hf_repo": "csukuangfj/sherpa-onnx-zipformer-ctc-zh-int8-2025-07-03",
        "dir_name": "sherpa-onnx-zipformer-ctc-zh-int8-2025-07-03",
        "files": ["model.int8.onnx", "tokens.txt"],
    },
    {
        "id": "qwen3-asr-0.6b",
        "name": "Qwen3-ASR 0.6B",
        "description": "阿里大模型 ASR，多语种",
        "accuracy": 5,
        "speed": 2,
        "languages": "中/英/多语种",
        "size_mb": 983,
        "loader": "qwen3_asr",
        "hf_repo": "csukuangfj2/sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25",
        "dir_name": "sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25",
        "files": [
            "conv_frontend.onnx",
            "encoder.int8.onnx",
            "decoder.int8.onnx",
            "tokenizer/vocab.json",
            "tokenizer/merges.txt",
            "tokenizer/tokenizer_config.json",
        ],
    },
    {
        "id": "qwen3-asr-1.7b",
        "name": "Qwen3-ASR 1.7B",
        "description": "阿里大模型 ASR 大参数版，多语种（较慢）",
        "accuracy": 5,
        "speed": 1,
        "languages": "中/英/多语种",
        "size_mb": 2400,
        "loader": "qwen3_asr",
        "hf_repo": "ilmina/qwen3-asr-1.7b-sherpa-onnx",
        "dir_name": "sherpa-onnx-qwen3-asr-1.7B-int8-2026-03-25",
        "files": [
            "conv_frontend.onnx",
            "encoder.int8.onnx",
            "decoder.int8.onnx",
            "tokenizer/vocab.json",
            "tokenizer/merges.txt",
            "tokenizer/tokenizer_config.json",
        ],
    },
    {
        "id": "funasr-nano",
        "name": "Fun-ASR-Nano",
        "description": "FunASR 端到端模型，SenseVoice 编码 + Qwen3",
        "accuracy": 5,
        "speed": 2,
        "languages": "中/英/日/方言",
        "size_mb": 950,
        "loader": "funasr_nano",
        "hf_repo": "csukuangfj/sherpa-onnx-funasr-nano-int8-2025-12-30",
        "dir_name": "sherpa-onnx-funasr-nano-int8-2025-12-30",
        "files": [
            "encoder_adaptor.int8.onnx",
            "llm.int8.onnx",
            "embedding.int8.onnx",
            "Qwen3-0.6B/vocab.json",
            "Qwen3-0.6B/merges.txt",
            "Qwen3-0.6B/tokenizer.json",
        ],
    },
]


def get_model_info(model_id: str) -> dict | None:
    for m in MODEL_REGISTRY:
        if m["id"] == model_id:
            return m
    return None


_custom_models_dir: Path | None = None


def set_models_dir(path: Path | None):
    global _custom_models_dir
    _custom_models_dir = path
    if path:
        path.mkdir(parents=True, exist_ok=True)


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

    def _download_file(self, url: str, target: Path, index: int, total_files: int) -> None:
        import httpx

        tmp_path = target.with_name(target.name + ".tmp")
        try:
            with httpx.stream("GET", url, timeout=60.0, follow_redirects=True) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length", 0))
                downloaded = 0
                with open(tmp_path, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=1024 * 256):
                        if self._cancelled:
                            raise _DownloadCancelled()
                        f.write(chunk)
                        downloaded += len(chunk)
                        fraction = downloaded / total if total > 0 else 0.0
                        self._emit_progress(index, total_files, fraction, downloaded)
            if total > 0 and downloaded != total:
                raise IncompleteDownloadError(
                    f"{target.name} 不完整（{downloaded} / {total} 字节）"
                )
            if downloaded == 0:
                raise IncompleteDownloadError(f"{target.name} 为空文件")
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
                    self._done_bytes += target.stat().st_size
                    self._emit_progress(i, total_files, 1.0, 0)
                    continue

                last_err: Exception | None = None
                for endpoint in self._endpoints:
                    url = f"{endpoint}/{info['hf_repo']}/resolve/main/{filename}"
                    log.info("下载: %s", url)
                    try:
                        self._download_file(url, target, i, total_files)
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


def _create_recognizer(model_id: str, num_threads: int):
    """Create a sherpa_onnx.OfflineRecognizer for the given model."""
    import sherpa_onnx

    info = get_model_info(model_id)
    if not info:
        raise ValueError(f"未知模型: {model_id}")

    model_dir = get_model_dir(model_id)
    loader = info["loader"]

    if loader == "sense_voice":
        return sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(model_dir / "model.int8.onnx"),
            tokens=str(model_dir / "tokens.txt"),
            num_threads=num_threads,
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
            debug=False,
        )
    elif loader == "funasr_nano":
        return sherpa_onnx.OfflineRecognizer.from_funasr_nano(
            encoder_adaptor=str(model_dir / "encoder_adaptor.int8.onnx"),
            llm=str(model_dir / "llm.int8.onnx"),
            embedding=str(model_dir / "embedding.int8.onnx"),
            tokenizer=str(model_dir / "Qwen3-0.6B"),
            num_threads=num_threads,
            debug=False,
        )
    else:
        raise ValueError(f"不支持的 loader: {loader}")


class ModelLoadWorker(QThread):
    """Loads an ASR model in a background thread."""
    loaded = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, model_id: str, num_threads: int = 4):
        super().__init__()
        self._model_id = model_id
        self._num_threads = num_threads

    def run(self):
        try:
            info = get_model_info(self._model_id)
            name = info["name"] if info else self._model_id
            log.info("正在加载语音识别模型 (%s)...", name)
            recognizer = _create_recognizer(self._model_id, self._num_threads)
            log.info("语音识别模型加载完成: %s", name)
            self.loaded.emit(recognizer)
        except Exception as e:
            log.error("模型加载失败: %s", e)
            self.error.emit(f"模型加载失败: {e}")


class TranscribeWorker(QThread):
    """Runs transcription in a background thread."""
    result_ready = pyqtSignal(str)
    partial_ready = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(
        self,
        recognizer,
        audio_data: np.ndarray,
        *,
        max_slice_sec: float | None = None,
        overlap_sec: float = CONTEXT_SLICE_OVERLAP_SEC,
    ):
        super().__init__()
        self._recognizer = recognizer
        self._audio_data = audio_data
        self._cancelled = False
        self._max_slice_sec = max_slice_sec
        self._overlap_sec = overlap_sec

    def cancel(self):
        """Set cancellation flag."""
        self._cancelled = True

    def _decode_slice(self, audio: np.ndarray) -> str:
        stream = self._recognizer.create_stream()
        stream.accept_waveform(SAMPLE_RATE, audio)
        self._recognizer.decode_stream(stream)
        return normalize_asr_output(stream.result.text)

    def run(self):
        if self._cancelled:
            return

        try:
            audio = self._audio_data.astype(np.float32)
            if audio.ndim > 1:
                audio = audio[:, 0]

            # 验证音频数据有效性
            if audio.size == 0 or np.any(np.isnan(audio)) or np.any(np.isinf(audio)):
                self.error.emit("音频数据无效")
                return

            log.info("开始识别 (%.1f 秒音频)...", len(audio) / SAMPLE_RATE)
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


# ── Main Recognizer ──────────────────────────────────────────────

class SpeechRecognizer(QObject):
    """Offline speech recognizer supporting multiple sherpa-onnx models."""
    final_result = pyqtSignal(str)
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
        self._loaded_target: tuple[str, int] | None = None
        self._is_ready = False
        self._model_id = ""
        self._num_threads = 4
        # Monotonic request id: results from superseded/cancelled workers are
        # ignored instead of force-terminating C++/ONNX threads (see SD-05).
        self._request_seq = 0
        self._active_seq = 0
        self._workers: list[TranscribeWorker] = []

    def configure(self, model_id: str, num_threads: int = 4):
        if not model_id:
            log.warning("未选择语音模型")
            return

        if (
            model_id == self._model_id
            and num_threads == self._num_threads
            and self._is_ready
            and self._recognizer is not None
        ):
            log.debug("语音模型已驻留内存，跳过重复加载: %s", model_id)
            return

        if (
            model_id == self._model_id
            and num_threads == self._num_threads
            and self.is_loading
        ):
            log.debug("语音模型正在加载中，跳过重复请求: %s", model_id)
            return

        self._num_threads = num_threads
        self._model_id = model_id

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
        worker = ModelLoadWorker(self._model_id, self._num_threads)
        target = (self._model_id, self._num_threads)
        worker.loaded.connect(lambda rec, t=target: self._on_model_loaded(rec, t))
        worker.error.connect(lambda msg, t=target: self._on_model_load_error(msg, t))
        worker.finished.connect(self._on_load_worker_finished)
        self._load_worker = worker
        worker.start()

    def _is_current_target(self, target: tuple[str, int] | None) -> bool:
        return target is None or target == (self._model_id, self._num_threads)

    def _on_model_loaded(self, recognizer, target: tuple[str, int] | None = None):
        if not self._is_current_target(target):
            log.info("丢弃已过时的模型加载结果: %s", target[0])
            return
        self._recognizer = recognizer
        self._loaded_target = (self._model_id, self._num_threads)
        self._is_ready = True
        self.model_load_progress.emit(ModelLoadPhase.READY, "模型已就绪")
        self.ready.emit()

    def _on_model_load_error(self, msg: str, target: tuple[str, int] | None = None):
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
        current = (self._model_id, self._num_threads)
        if self._is_ready and self._loaded_target == current:
            return
        if is_model_downloaded(self._model_id):
            self._load_model()

    def transcribe_final(self, full_audio: np.ndarray):
        if not self._is_ready or self._recognizer is None:
            self.error.emit("语音模型未就绪，请等待模型加载完成")
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
            worker = TranscribeWorker(self._recognizer, full_audio)
        else:
            slice_sec, overlap_sec = window
            worker = TranscribeWorker(
                self._recognizer,
                full_audio,
                max_slice_sec=slice_sec,
                overlap_sec=overlap_sec,
            )
        worker.result_ready.connect(
            lambda text, s=seq: self._on_final_result(text, s)
        )
        worker.partial_ready.connect(
            lambda text, s=seq: self._on_partial_result(text, s)
        )
        worker.error.connect(lambda e, s=seq: self._on_worker_error(e, s))
        worker.finished.connect(lambda w=worker: self._reap_worker(w))
        self._current_worker = worker
        self._workers.append(worker)
        worker.start()

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

    def _on_worker_error(self, error_msg: str, seq: int):
        if seq != self._active_seq:
            log.debug("忽略过时识别错误 (seq=%d, active=%d)", seq, self._active_seq)
            return
        self.error.emit(error_msg)

    def shutdown(self):
        """Stop all running workers for clean app exit."""
        if self._load_worker and self._load_worker.isRunning():
            try:
                self._load_worker.disconnect()
            except TypeError:
                pass
            self._load_worker.wait(1000)

        # Ignore any pending results and let running workers finish cooperatively.
        self._active_seq = -1
        for worker in list(self._workers):
            if worker.isRunning():
                worker.cancel()
                worker.wait(1000)

    @property
    def is_ready(self) -> bool:
        return self._is_ready

    @property
    def is_loading(self) -> bool:
        return self._load_worker is not None and self._load_worker.isRunning()

    @property
    def current_model_id(self) -> str:
        return self._model_id

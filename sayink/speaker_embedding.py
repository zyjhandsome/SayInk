"""Speaker embeddings for meeting diarization.

Two backends behind one callable:

* :class:`ModelEmbedder` — sherpa-onnx ``SpeakerEmbeddingExtractor`` with the
  3D-Speaker CAM++ zh/en model (28 MB, downloaded as the ``speaker-campplus``
  aux model). Proper x-vectors; two people with similar pitch stay apart.
* :class:`MelEmbedder` — the log-mel summary dictation has always used
  (``speaker_session.voice_embedding``). No model file needed.

The embedding stays in memory for the meeting and is never written to disk;
only the resulting speaker number goes into the database
(docs/meeting-notes-design.md §4.6 / D5).
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from sayink.audio_utils import TARGET_SAMPLE_RATE
from sayink.speaker_session import MATCH_THRESHOLD, voice_embedding

log = logging.getLogger("SayInk")

# CAM++ cosine similarity between two clips of the same speaker typically sits
# above 0.6; different speakers below 0.4. 0.55 leans toward "same person"
# because a split speaker is corrected by the post-meeting recluster, while
# two people merged into one cannot be told apart afterwards.
MODEL_MATCH_THRESHOLD = 0.55
MEL_MATCH_THRESHOLD = MATCH_THRESHOLD
# Continuity prior (SpeakerSession.sticky_threshold): a segment that follows
# the previous one within STICKY_GAP_MS keeps the same speaker when at least
# this similar. The mel summary swings with the words spoken, so its bar sits
# well below the open-a-new-speaker threshold; CAM++ barely needs the help.
MODEL_STICKY_THRESHOLD = 0.45
MEL_STICKY_THRESHOLD = 0.70
STICKY_GAP_MS = 3000
# Post-meeting recluster compares per-speaker centroids. Averaging over many
# segments cancels most of the content variance, so splits of the same person
# merge at a lower bar than the online one.
MODEL_RECLUSTER_THRESHOLD = 0.50
MEL_RECLUSTER_THRESHOLD = 0.75
# Shorter clips give either backend too little to go on; a zero vector makes
# SpeakerSession continue the previous speaker instead of guessing.
MODEL_MIN_SEC = 0.5
MEL_MIN_SEC = 1.0
_QUIET_RMS = 0.005


class SpeakerEmbedder:
    name = "embedder"
    model_available = False
    load_error = ""
    threshold = MEL_MATCH_THRESHOLD
    sticky_threshold = MEL_STICKY_THRESHOLD
    recluster_threshold = MEL_RECLUSTER_THRESHOLD

    def __call__(self, audio: np.ndarray) -> np.ndarray:
        raise NotImplementedError


class MelEmbedder(SpeakerEmbedder):
    """Log-mel summary (dictation's heuristic)."""

    name = "mel"
    threshold = MEL_MATCH_THRESHOLD
    sticky_threshold = MEL_STICKY_THRESHOLD
    recluster_threshold = MEL_RECLUSTER_THRESHOLD

    def __call__(self, audio: np.ndarray) -> np.ndarray:
        samples = np.asarray(audio, dtype=np.float32).reshape(-1)
        if samples.size < int(TARGET_SAMPLE_RATE * MEL_MIN_SEC):
            return np.zeros(1, dtype=np.float32)
        return voice_embedding(samples)


class ModelEmbedder(SpeakerEmbedder):
    """sherpa-onnx speaker embedding model (CAM++)."""

    name = "campplus"
    model_available = True
    threshold = MODEL_MATCH_THRESHOLD
    sticky_threshold = MODEL_STICKY_THRESHOLD
    recluster_threshold = MODEL_RECLUSTER_THRESHOLD

    def __init__(self, model_path: str | Path, *, num_threads: int = 1):
        import sherpa_onnx

        config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(model_path),
            num_threads=int(num_threads),
            debug=False,
            provider="cpu",
        )
        if hasattr(config, "validate") and not config.validate():
            raise RuntimeError(f"说话人模型配置无效: {model_path}")
        self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
        self.dim = int(getattr(self._extractor, "dim", 0) or 0)

    def __call__(self, audio: np.ndarray) -> np.ndarray:
        samples = np.asarray(audio, dtype=np.float32).reshape(-1)
        if samples.size < int(TARGET_SAMPLE_RATE * MODEL_MIN_SEC):
            return np.zeros(self.dim or 1, dtype=np.float32)
        rms = float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0
        if rms < _QUIET_RMS:
            return np.zeros(self.dim or 1, dtype=np.float32)
        try:
            stream = self._extractor.create_stream()
            stream.accept_waveform(TARGET_SAMPLE_RATE, samples)
            stream.input_finished()
            if not self._extractor.is_ready(stream):
                return np.zeros(self.dim or 1, dtype=np.float32)
            vector = np.asarray(self._extractor.compute(stream), dtype=np.float32).reshape(-1)
        except Exception as exc:
            log.warning("说话人嵌入计算失败，本段无法判断说话人: %s", exc)
            return np.zeros(self.dim or 1, dtype=np.float32)
        norm = float(np.linalg.norm(vector))
        if norm < 1e-6:
            return np.zeros(vector.size or 1, dtype=np.float32)
        if not self.dim:
            self.dim = int(vector.size)
        return vector / norm


def make_embedder(model_path: str | Path | None, *, enabled: bool = True) -> SpeakerEmbedder:
    """CAM++ when enabled and the model file is there, else the mel summary."""
    fallback = MelEmbedder()
    fallback.model_available = bool(model_path and Path(model_path).is_file())
    if enabled and fallback.model_available:
        try:
            embedder = ModelEmbedder(model_path)
            log.info("会议说话人模型: CAM++ (%s)", model_path)
            return embedder
        except Exception as exc:
            log.warning("说话人模型加载失败，改用频谱摘要: %s", exc)
            fallback.load_error = str(exc)
    return fallback

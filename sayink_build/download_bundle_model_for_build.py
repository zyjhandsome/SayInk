"""
Download the models a full installer bundles into <project>/models/.

That is Fun-ASR-Nano plus the meeting helpers (CAM++, Silero VAD, and the
experimental speaker-activity model). Lite and full installers both copy the
helpers; only the full installer also copies Fun-ASR-Nano.
Same HuggingFace layout as the in-app downloader. Run from repo root:

  python sayink_build/download_bundle_model_for_build.py
  python sayink_build/download_bundle_model_for_build.py funasr-nano

Requires: pip install httpx (already in requirements.txt)
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("download_bundle_model")

from sayink.speech_recognizer import HF_URL, file_sha256, get_model_info, model_file_url


def _download_one(model_id: str) -> int:
    import hashlib

    import httpx

    info = get_model_info(model_id)
    if not info:
        log.error("Unknown model: %s", model_id)
        return 1

    model_dir = ROOT / "models" / info["dir_name"]
    model_dir.mkdir(parents=True, exist_ok=True)
    files = info["files"]
    timeout = httpx.Timeout(connect=60.0, read=600.0, write=60.0, pool=60.0)

    log.info("Downloading %s (%s) ...", info["name"], model_id)

    for i, filename in enumerate(files, start=1):
        target = model_dir / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        expected = info["sha256"][filename]
        if target.exists():
            if file_sha256(target) == expected:
                log.info("[%d/%d] skip (verified): %s", i, len(files), filename)
                continue
            log.warning("[%d/%d] %s differs from the pinned hash, downloading again", i, len(files), filename)

        url = model_file_url(HF_URL, info, filename.replace("\\", "/"))
        log.info("[%d/%d] downloading %s ...", i, len(files), filename)
        tmp = target.with_suffix(target.suffix + ".part")
        try:
            digest = hashlib.sha256()
            with httpx.stream("GET", url, timeout=timeout, follow_redirects=True) as resp:
                resp.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in resp.iter_bytes(256 * 1024):
                        f.write(chunk)
                        digest.update(chunk)
            if digest.hexdigest() != expected:
                log.error("%s does not match the pinned sha256; not keeping it", filename)
                tmp.unlink(missing_ok=True)
                return 1
            tmp.replace(target)
        except Exception:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            raise

    missing = [f for f in files if not (model_dir / f).exists()]
    if missing:
        log.error("Incomplete download, missing: %s", missing)
        return 1

    log.info("Complete: %s", model_dir)
    return 0


def main(model_id: str | None = None) -> int:
    """Download one model, or every model a full installer bundles."""
    if model_id:
        return _download_one(model_id)
    from build import bundle_model_ids

    rc = 0
    for mid in bundle_model_ids():
        rc = _download_one(mid) or rc
    return rc


if __name__ == "__main__":
    requested = sys.argv[1] if len(sys.argv) > 1 else None
    sys.exit(main(requested))

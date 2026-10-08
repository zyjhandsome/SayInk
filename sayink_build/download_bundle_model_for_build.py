"""
Download the default bundled ASR model into <project>/models/ for PyInstaller / Inno builds.

Same HuggingFace layout as the in-app downloader. Run from repo root:

  python sayink_build/download_bundle_model_for_build.py

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

from sayink.speech_recognizer import DEFAULT_MODEL_ID, get_model_info, HF_URL


def main(model_id: str = DEFAULT_MODEL_ID) -> int:
    import httpx

    info = get_model_info(model_id)
    if not info:
        log.error("Unknown model: %s", model_id)
        return 1

    model_dir = ROOT / "models" / info["dir_name"]
    model_dir.mkdir(parents=True, exist_ok=True)
    hf_base = f"{HF_URL}/{info['hf_repo']}/resolve/main/"
    files = info["files"]
    timeout = httpx.Timeout(connect=60.0, read=600.0, write=60.0, pool=60.0)

    log.info("Downloading %s (%s) ...", info["name"], model_id)

    for i, filename in enumerate(files, start=1):
        target = model_dir / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.stat().st_size > 0:
            log.info("[%d/%d] skip (exists): %s", i, len(files), filename)
            continue

        url = hf_base + filename.replace("\\", "/")
        log.info("[%d/%d] downloading %s ...", i, len(files), filename)
        tmp = target.with_suffix(target.suffix + ".part")
        try:
            with httpx.stream("GET", url, timeout=timeout, follow_redirects=True) as resp:
                resp.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in resp.iter_bytes(256 * 1024):
                        f.write(chunk)
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


if __name__ == "__main__":
    sys.exit(main())

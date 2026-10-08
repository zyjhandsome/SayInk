"""
Deprecated wrapper — use download_bundle_model_for_build.py instead.

Kept so older docs/scripts that reference this filename still work.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sayink_build.download_bundle_model_for_build import main

if __name__ == "__main__":
    sys.exit(main())

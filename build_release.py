"""
One-shot Windows release build: PyInstaller → Inno Setup → remove staging.

Final artifact (for distribution):
  dist/SayInk-Setup-<version>.exe  (version in sayink/version.py)

The unpacked folder dist/SayInk/ is only an intermediate step and is
removed after the installer is built (unless --keep-staging).

Usage:
  python build_release.py
  python build_release.py --keep-staging    # keep dist/SayInk for debugging
"""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.resolve()


def main() -> None:
    parser = argparse.ArgumentParser(description="Build SayInk Windows installer (single EXE output).")
    parser.add_argument(
        "--keep-staging",
        action="store_true",
        help="Do not delete dist/SayInk after the installer succeeds.",
    )
    args = parser.parse_args()

    print("Step 1/2: PyInstaller (dist/SayInk/) …")
    r = subprocess.run([sys.executable, str(ROOT / "build.py")], cwd=ROOT)
    if r.returncode != 0:
        sys.exit(r.returncode)

    print()
    print("Step 2/2: Inno Setup (dist/SayInk-Setup-<version>.exe) …")
    inst_cmd = [sys.executable, str(ROOT / "installer" / "build_installer.py")]
    if args.keep_staging:
        inst_cmd.append("--keep-staging")
    r = subprocess.run(inst_cmd, cwd=ROOT)
    sys.exit(r.returncode)


if __name__ == "__main__":
    main()

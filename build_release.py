"""
One-shot Windows release build: PyInstaller → Inno Setup → remove staging.

Final artifact (for distribution):
  dist/SayInk-Setup-<version>.exe       lite installer (default; the app
                                        downloads Fun-ASR-Nano on first start)
  dist/SayInk-Setup-<version>-full.exe  with --with-model: model bundled

The version comes from sayink/version.py. The unpacked folder dist/SayInk/
is only an intermediate step and is removed after the installer is built
(unless --keep-staging).

Usage:
  python build_release.py                 # lite installer
  python build_release.py --with-model    # full installer (needs the model locally)
  python build_release.py --keep-staging  # keep dist/SayInk for debugging
"""

import argparse
import os
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
    parser.add_argument(
        "--with-model",
        action="store_true",
        help="Bundle Fun-ASR-Nano into the installer (SayInk-Setup-<version>-full.exe).",
    )
    args = parser.parse_args()

    env = dict(os.environ)
    env["SAYINK_BUNDLE_MODEL"] = "1" if args.with_model else "0"
    flavor = "full (model bundled)" if args.with_model else "lite (model downloaded on first start)"

    print(f"Step 1/2: PyInstaller (dist/SayInk/), {flavor} …")
    r = subprocess.run([sys.executable, str(ROOT / "build.py")], cwd=ROOT, env=env)
    if r.returncode != 0:
        sys.exit(r.returncode)

    print()
    print("Step 2/2: Inno Setup (dist/SayInk-Setup-<version>[-full].exe) …")
    inst_cmd = [sys.executable, str(ROOT / "installer" / "build_installer.py")]
    if args.keep_staging:
        inst_cmd.append("--keep-staging")
    if args.with_model:
        inst_cmd.append("--with-model")
    r = subprocess.run(inst_cmd, cwd=ROOT, env=env)
    sys.exit(r.returncode)


if __name__ == "__main__":
    main()

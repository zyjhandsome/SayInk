"""
One-shot Windows release build: PyInstaller → Inno Setup → remove staging.

Final artifact (for distribution):
  dist/SayInk-Setup-<version>.exe       lite installer (default; bundles
                                        CAM++, Silero VAD and speaker-activity;
                                        the app downloads Fun-ASR-Nano on first start)
  dist/SayInk-Setup-<version>-full.exe  with --with-model: also bundles
                                        Fun-ASR-Nano

The version comes from sayink/version.py. The unpacked folder dist/SayInk/
is only an intermediate step and is removed after the installer is built
(unless --keep-staging).

Before building, sayink_build/release_gate.py checks that the working tree is
committed, tag v<version> on origin points at HEAD and CI passed for HEAD.

Usage:
  python build_release.py                 # lite installer
  python build_release.py --with-model    # full installer (needs the model locally)
  python build_release.py --keep-staging  # keep dist/SayInk for debugging
  python build_release.py --skip-release-checks  # local trial build, never publish it
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

from sayink.version import __version__
from sayink_build.release_gate import release_problems

ROOT = Path(__file__).parent.resolve()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Build SayInk Windows installer (single EXE output).")
    parser.add_argument(
        "--keep-staging",
        action="store_true",
        help="Do not delete dist/SayInk after the installer succeeds.",
    )
    parser.add_argument(
        "--with-model",
        action="store_true",
        help="Also bundle Fun-ASR-Nano (SayInk-Setup-<version>-full.exe). Lite already bundles CAM++ and Silero VAD.",
    )
    parser.add_argument(
        "--skip-release-checks",
        action="store_true",
        help="Local trial build: skip the commit / tag / CI checks. Never publish the result.",
    )
    args = parser.parse_args(argv)

    if not args.skip_release_checks:
        problems = release_problems(__version__)
        if problems:
            print(f"Not releasable as v{__version__}:")
            for problem in problems:
                print(f"  - {problem}")
            print("Fix these, or pass --skip-release-checks for a local trial build.")
            sys.exit(1)

    env = dict(os.environ)
    env["SAYINK_BUNDLE_MODEL"] = "1" if args.with_model else "0"
    flavor = (
        "full (Fun-ASR-Nano, CAM++, Silero VAD and speaker-activity)"
        if args.with_model
        else "lite (CAM++, Silero VAD and speaker-activity; Fun-ASR-Nano downloaded on first start)"
    )

    print(f"Step 1/2: PyInstaller (dist/SayInk/), {flavor} …")
    r = subprocess.run([sys.executable, str(ROOT / "build.py")], cwd=ROOT, env=env, check=False)
    if r.returncode != 0:
        sys.exit(r.returncode)

    print()
    print("Step 2/2: Inno Setup (dist/SayInk-Setup-<version>[-full].exe) …")
    inst_cmd = [sys.executable, str(ROOT / "installer" / "build_installer.py")]
    if args.keep_staging:
        inst_cmd.append("--keep-staging")
    if args.with_model:
        inst_cmd.append("--with-model")
    r = subprocess.run(inst_cmd, cwd=ROOT, env=env, check=False)
    sys.exit(r.returncode)


if __name__ == "__main__":
    main()

"""
Build script for creating SayInk Windows installer using Inno Setup.

Prerequisites:
- Inno Setup 6 must be installed (https://jrsoftware.org/isdl.php)
- SayInk must already be built with PyInstaller (run build.py first), or use
  ../build_release.py for a one-shot build.

Output: dist/SayInk-Setup-<version>.exe (version from sayink/version.py)

By default, the intermediate folder dist/SayInk/ is deleted after a
successful compile so dist/ only contains the setup EXE. Pass --keep-staging
to preserve it for debugging.
"""

import argparse
import subprocess
import shutil
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sayink.version import __version__, file_version_quad


def resolve_staging_dir() -> Path:
    """PyInstaller output folder (dist/SayInk or alternate from SAYINK_STAGING_DIR.txt)."""
    marker = PROJECT_ROOT / "dist" / "SAYINK_STAGING_DIR.txt"
    if marker.is_file():
        name = marker.read_text(encoding="utf-8").strip()
        if name:
            return PROJECT_ROOT / "dist" / name
    return PROJECT_ROOT / "dist" / "SayInk"


# Inno Setup compiler path (common installation locations)
INNO_SETUP_PATHS = [
    Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
    Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
    Path.home() / "AppData" / "Local" / "Programs" / "Inno Setup 6" / "ISCC.exe",  # winget install location
    Path(r"C:\Program Files (x86)\Inno Setup 5\ISCC.exe"),
    Path(r"C:\Program Files\Inno Setup 5\ISCC.exe"),
]


def find_inno_setup() -> Path | None:
    """Find Inno Setup compiler executable."""
    for path in INNO_SETUP_PATHS:
        if path.exists():
            return path
    return None


def check_prerequisites():
    """Check that all prerequisites are met."""
    # Check Inno Setup
    inno_path = find_inno_setup()
    if not inno_path:
        print("\n[ERROR] Inno Setup not found!")
        print("\nPlease install Inno Setup 6 from:")
        print("  https://jrsoftware.org/isdl.php")
        print("\nAfter installation, run this script again.")
        return False

    print(f"[OK] Inno Setup found: {inno_path}")

    # Check SayInk build exists
    sayink_exe = resolve_staging_dir() / "SayInk.exe"
    if not sayink_exe.exists():
        print("\n[ERROR] SayInk.exe not found!")
        print("\nPlease run build.py first to create the executable.")
        return False

    print(f"[OK] SayInk.exe found: {sayink_exe}")

    # Check installer script
    installer_script = SCRIPT_DIR / "SayInk-Setup.iss"
    if not installer_script.exists():
        print("\n[ERROR] Installer script not found!")
        print(f"  Expected: {installer_script}")
        return False

    print(f"[OK] Installer script found: {installer_script}")

    # Check icon file
    icon_file = PROJECT_ROOT / "sayink" / "icon.ico"
    if not icon_file.exists():
        print("\n[ERROR] Icon file not found!")
        print(f"  Expected: {icon_file}")
        return False

    print(f"[OK] Icon file found: {icon_file}")

    # Check models
    models_dir = resolve_staging_dir() / "models"
    if models_dir.exists():
        model_count = len(list(models_dir.iterdir()))
        print(f"[OK] Models found: {model_count} models in {models_dir}")
    else:
        print("[WARN] No models found - users will need to download after installation")

    return True


def build_installer(*, keep_staging: bool = False):
    """Build the Windows installer using Inno Setup."""
    print("=" * 60)
    print("  SayInk Installer Build Script")
    print(f"  Version {__version__}  (Inno / Win file quad {file_version_quad()})")
    print("=" * 60)
    print()

    if not check_prerequisites():
        sys.exit(1)

    print()
    print("[1/2] Building installer with Inno Setup...")
    print("     (Large tree + LZMA ultra can take many minutes; progress from ISCC appears below.)")
    print()

    inno_path = find_inno_setup()
    installer_script = SCRIPT_DIR / "SayInk-Setup.iss"

    # Stream ISCC output (do not capture) so long compress steps do not look hung.
    try:
        result = subprocess.run(
            [
                str(inno_path),
                f"/DAppVersionStr={__version__}",
                f"/DAppVersionQuad={file_version_quad()}",
                str(installer_script),
            ],
            cwd=str(SCRIPT_DIR),
            timeout=7200,  # 2 h — ~1 GB+ payloads with ultra compression
        )

        if result.returncode != 0:
            print("\n[ERROR] Inno Setup compilation failed (see messages above).")
            sys.exit(1)

    except subprocess.TimeoutExpired:
        print("\n[ERROR] Inno Setup compilation timed out after 2 hours.")
        sys.exit(1)
    except Exception as e:
        print(f"\n[ERROR] Failed to run Inno Setup: {e}")
        sys.exit(1)

    # Check output (filename includes version; see SayInk-Setup.iss OutputBaseFilename)
    output_file = PROJECT_ROOT / "dist" / f"SayInk-Setup-{__version__}.exe"
    if not output_file.exists():
        print("\n[ERROR] Installer not created!")
        sys.exit(1)

    # Get file size
    size_mb = output_file.stat().st_size / (1024 * 1024)

    print()
    print("=" * 60)
    print("  [2/2] Build Complete!")
    print("=" * 60)
    print(f"  Output file : {output_file}")
    print(f"  File size   : {size_mb:.0f} MB")
    print()
    print("  Distribution:")
    print(f"    Share {output_file.name} with users.")
    print("    Users run the installer to install SayInk.")
    print("=" * 60)

    staging_dir = resolve_staging_dir()
    if not keep_staging and staging_dir.exists():
        shutil.rmtree(staging_dir, ignore_errors=True)
        marker = PROJECT_ROOT / "dist" / "SAYINK_STAGING_DIR.txt"
        if marker.is_file():
            marker.unlink(missing_ok=True)
        print()
        print(f"  Removed staging folder (intermediate build): {staging_dir}")
        print(f"  dist/ now contains {output_file.name} only.")

    return output_file


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Build SayInk-Setup.exe (Inno Setup).")
    ap.add_argument(
        "--keep-staging",
        action="store_true",
        help="Keep dist/SayInk after success (default: delete to leave only the installer).",
    )
    ns = ap.parse_args()
    build_installer(keep_staging=ns.keep_staging)
"""
Build script for packaging VoiceInk as a standalone Windows application.

Output: dist/VoiceInk/ — VoiceInk.exe, _internal/, and optional models/.

Models are copied next to the exe (not inside it) from ~/.voiceink/models/
or ./models/ when present. **Fun-ASR-Nano must exist locally** or the build exits with an error.
Fetch it with: `python voiceink_build/download_bundle_model_for_build.py` (writes to `./models/`).

Distribution:
- **Installer (recommended):** run `python build_release.py` — produces
  `dist/VoiceInk-Setup-<version>.exe` (version from `voiceink/version.py`;
  staging folder removed after Inno Setup).
- **Portable folder:** run this script only, then zip `dist/VoiceInk/` for
  users who should not run an installer.
"""

import shutil
import subprocess
import sys
import time
from pathlib import Path

if __name__ == "__main__" and __package__ is None:
    _root = Path(__file__).resolve().parent
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))


SCRIPT_DIR = Path(__file__).parent.resolve()
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

# Always bundle this model in dist/VoiceInk/models/ for released EXE/installer.
from voiceink.speech_recognizer import DEFAULT_MODEL_ID

BUNDLE_REQUIRE_MODEL_ID = DEFAULT_MODEL_ID


def _find_model_sources() -> list[tuple[str, Path]]:
    """Return the default release model if present under project or user models dir."""
    try:
        from voiceink.speech_recognizer import get_model_info
    except ImportError:
        return []

    info = get_model_info(BUNDLE_REQUIRE_MODEL_ID)
    if not info:
        return []

    project_models = SCRIPT_DIR / "models"
    user_models = Path.home() / ".voiceink" / "models"

    for src_dir in [project_models, user_models]:
        d = src_dir / info["dir_name"]
        if d.exists() and all((d / f).exists() for f in info["files"]):
            return [(info["dir_name"], d)]
    return []


def _require_bundle_model(downloaded: list[tuple[str, Path]]) -> None:
    from voiceink.speech_recognizer import get_model_info

    info = get_model_info(BUNDLE_REQUIRE_MODEL_ID)
    if not info:
        print(f"\n[ERROR] Unknown bundle model id: {BUNDLE_REQUIRE_MODEL_ID!r}")
        sys.exit(1)
    dirname = info["dir_name"]
    if any(d == dirname for d, _ in downloaded):
        return
    print("\n[ERROR] 打包 EXE 需要已在本地就绪的 Fun-ASR-Nano 模型。")
    print("  请先在应用「设置 → 引擎」中下载该模型，或将完整目录放到:")
    print(f"    {SCRIPT_DIR / 'models' / dirname}")
    print(f"    或 {Path.home() / '.voiceink' / 'models' / dirname}")
    sys.exit(1)


def _kill_running_voiceink():
    if sys.platform != "win32":
        return
    subprocess.run(
        ["taskkill", "/IM", "VoiceInk.exe", "/F"],
        capture_output=True,
        text=True,
    )


def _prepare_dist_output_dir() -> tuple[Path, str]:
    """Return (dist_parent, pyinstaller --name) after trying to clear dist/VoiceInk."""
    dist_parent = SCRIPT_DIR / "dist"
    dist_parent.mkdir(parents=True, exist_ok=True)
    target = dist_parent / "VoiceInk"
    if not target.exists():
        return dist_parent, "VoiceInk"

    _kill_running_voiceink()
    for attempt in range(4):
        try:
            shutil.rmtree(target)
            return dist_parent, "VoiceInk"
        except OSError as exc:
            print(f"[WARN] 无法删除 {target} (尝试 {attempt + 1}/4): {exc}")
            time.sleep(2)
            _kill_running_voiceink()

    alt_name = f"VoiceInk_{int(time.time())}"
    print(f"[WARN] 将输出到备用目录 dist/{alt_name}/（请关闭占用 dist/VoiceInk 的程序后改回标准路径）")
    return dist_parent, alt_name


def build():
    """Package VoiceInk with PyInstaller."""
    from voiceink_build.dependency_check import require_release_dependencies

    # Validate before killing a running app or clearing its previous build.
    require_release_dependencies()
    print("=" * 55)
    print("  VoiceInk Build Script")
    print("=" * 55)
    print()

    print("[1/3] Building VoiceInk with PyInstaller...")

    import PyInstaller.__main__

    dist_parent, app_name = _prepare_dist_output_dir()

    main_script = str(SCRIPT_DIR / "voiceink" / "main.py")
    win_dll_rthook = SCRIPT_DIR / "voiceink_build" / "pyi_rth_voiceink_win_dll.py"
    if not win_dll_rthook.is_file():
        print(f"\n[ERROR] Missing runtime hook: {win_dll_rthook}")
        sys.exit(1)

    import os

    spin_icons = SCRIPT_DIR / "voiceink" / "ui" / "icons"
    args = [
        main_script,
        f"--name={app_name}",
        f"--distpath={dist_parent}",
        "--onedir",
        "--windowed",
        "--noconfirm",
        "--clean",
        "--noupx",
        f"--runtime-hook={win_dll_rthook}",
        f"--icon={SCRIPT_DIR / 'voiceink' / 'icon.ico'}",
        # Non-imported PNG steppers used by settings QSS (Path(__file__).parent/icons).
        f"--add-data={spin_icons}{os.pathsep}voiceink/ui/icons",
    ]

    if sys.platform == "win32":
        from voiceink_build.pyinstaller_version_info import write_version_file

        vf = SCRIPT_DIR / "build" / "file_version_info.txt"
        write_version_file(vf)
        args.append(f"--version-file={vf}")

    args.extend([
        "--hidden-import=pynput.keyboard._win32",
        "--hidden-import=pynput.mouse._win32",
        "--hidden-import=sounddevice",
        "--hidden-import=numpy",
        "--hidden-import=httpx",
        "--hidden-import=httpcore",
        "--hidden-import=pyperclip",
        # Paste uses Win32 keybd_event. pyautogui pulls in Pillow, and the
        # frozen app dies decompressing that Pillow module from the PYZ archive.
        "--exclude-module=pyautogui",
        "--exclude-module=pyscreeze",
        "--exclude-module=PIL",
        "--exclude-module=mouseinfo",
        "--exclude-module=pytweening",
        "--hidden-import=win32gui",
        "--hidden-import=win32api",
        "--hidden-import=win32con",
        "--hidden-import=win32cred",
        "--hidden-import=PyQt6.QtNetwork",
        "--hidden-import=sherpa_onnx",
    ])

    PyInstaller.__main__.run(args)

    dist_dir = dist_parent / app_name
    exe_path = dist_dir / "VoiceInk.exe"
    if app_name != "VoiceInk":
        marker = dist_parent / "VOICEINK_STAGING_DIR.txt"
        marker.write_text(app_name, encoding="utf-8")
    elif (dist_parent / "VOICEINK_STAGING_DIR.txt").exists():
        (dist_parent / "VOICEINK_STAGING_DIR.txt").unlink(missing_ok=True)

    if not exe_path.exists():
        print("\n[ERROR] Build failed — VoiceInk.exe not found.")
        sys.exit(1)

    print()
    print("[2/3] Copying models to dist/VoiceInk/models/ ...")

    models_dst = dist_dir / "models"
    downloaded = _find_model_sources()
    _require_bundle_model(downloaded)

    if downloaded:
        models_dst.mkdir(parents=True, exist_ok=True)
        for dir_name, src_path in downloaded:
            dst_path = models_dst / dir_name
            if dst_path.exists():
                shutil.rmtree(dst_path)
            shutil.copytree(src_path, dst_path)
            print(f"    + {dir_name}")
    else:
        print("    (no models found, users will need to download in settings)")

    total_size = sum(
        f.stat().st_size for f in dist_dir.rglob("*") if f.is_file()
    )
    size_mb = total_size / (1024 * 1024)

    print()
    print("=" * 55)
    print("  [3/3] Build Complete!")
    print("=" * 55)
    print(f"  Output folder : {dist_dir}")
    print(f"  Executable    : {exe_path}")
    print(f"  Total size    : {size_mb:.0f} MB")
    if downloaded:
        print(f"  Models        : {len(downloaded)} models in models/ folder")
    else:
        print("  Models        : none (download via settings)")
    print()
    print("  Distribution:")
    print("    Portable: zip this folder and share.")
    print("    Installer only: run python build_release.py — staging is removed after Setup.exe.")
    print("=" * 55)


if __name__ == "__main__":
    build()

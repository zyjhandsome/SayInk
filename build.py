"""
Build script for packaging SayInk as a standalone Windows application.

Output: dist/SayInk/ — SayInk.exe, _internal/, and models/.

The default **lite** package copies the meeting helpers (CAM++ speaker
embedding and Silero VAD). Fun-ASR-Nano is not included; the app downloads it
on first start (设置 → 引擎). ``--with-model`` (or ``SAYINK_BUNDLE_MODEL=1``)
also copies Fun-ASR-Nano. Both flavors fail when a model they must ship is
missing. Fetch the files with
`python sayink_build/download_bundle_model_for_build.py` (writes to `./models/`).

Distribution:
- **Installer (recommended):** run `python build_release.py` — produces
  `dist/SayInk-Setup-<version>.exe` (lite: CAM++ + Silero); add `--with-model`
  for `dist/SayInk-Setup-<version>-full.exe` (~740 MB, also Fun-ASR-Nano).
  Version from `sayink/version.py`; staging folder removed after Inno Setup.
- **Portable folder:** run this script only, then zip `dist/SayInk/` for
  users who should not run an installer.
"""

import os
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

# Fun-ASR-Nano is copied only by a --with-model build. Meeting helpers
# (CAM++, Silero) ship in both the lite and the full installer.
from sayink.speech_recognizer import DEFAULT_MODEL_ID

BUNDLE_REQUIRE_MODEL_ID = DEFAULT_MODEL_ID


def bundle_model_requested(argv: list[str] | None = None) -> bool:
    """``--with-model`` on the command line or SAYINK_BUNDLE_MODEL=1."""
    argv = sys.argv[1:] if argv is None else argv
    if "--with-model" in argv:
        return True
    return os.environ.get("SAYINK_BUNDLE_MODEL", "").strip().lower() in ("1", "true", "yes")


def bundle_aux_model_ids() -> list[str]:
    """Meeting helpers shipped in the lite installer as well as the full one."""
    from sayink.speech_recognizer import AUX_MODEL_REGISTRY

    return [m["id"] for m in AUX_MODEL_REGISTRY if not m.get("optional")]


def bundle_model_ids() -> list[str]:
    """Models a ``--with-model`` build must copy: ASR, then meeting helpers."""
    return [BUNDLE_REQUIRE_MODEL_ID, *bundle_aux_model_ids()]


def model_search_dirs() -> list[Path]:
    """Project tree first, then the per-user dirs the app itself downloads into."""
    return [
        SCRIPT_DIR / "models",
        Path.home() / ".sayink" / "models",
        Path.home() / ".voiceink" / "models",
    ]


def _find_model_sources(
    search_dirs: list[Path] | None = None,
    model_ids: list[str] | None = None,
) -> list[tuple[str, Path]]:
    """Return each requested bundle model found under ``search_dirs``."""
    try:
        from sayink.speech_recognizer import get_model_info
    except ImportError:
        return []

    dirs = model_search_dirs() if search_dirs is None else search_dirs
    wanted = bundle_model_ids() if model_ids is None else model_ids
    found: list[tuple[str, Path]] = []
    for model_id in wanted:
        info = get_model_info(model_id)
        if not info:
            continue
        for src_dir in dirs:
            candidate = src_dir / info["dir_name"]
            if candidate.is_dir() and all((candidate / name).is_file() for name in info["files"]):
                found.append((info["dir_name"], candidate))
                break
    return found


def _require_pinned_content(downloaded: list[tuple[str, Path]]) -> None:
    """The full installer ships exactly the model files pinned in the registry."""
    from sayink.speech_recognizer import get_model_info, mismatched_model_files

    infos = {info["dir_name"]: info for info in map(get_model_info, bundle_model_ids()) if info}
    bad = []
    for dir_name, src_path in downloaded:
        print(f"    verifying {dir_name} …")
        names = mismatched_model_files(infos[dir_name], src_path)
        if names:
            bad.append(f"{src_path}: {', '.join(names)}")
    if bad:
        print("\n[ERROR] Model files differ from the sha256 pinned in sayink/speech_recognizer.py:")
        for line in bad:
            print(f"  {line}")
        print("Delete them and run: python sayink_build/download_bundle_model_for_build.py")
        sys.exit(1)


def _require_bundle_model(
    downloaded: list[tuple[str, Path]],
    model_ids: list[str] | None = None,
) -> None:
    from sayink.speech_recognizer import get_model_info

    required = bundle_model_ids() if model_ids is None else model_ids
    present = {dirname for dirname, _ in downloaded}
    missing: list[dict] = []
    for model_id in required:
        info = get_model_info(model_id)
        if not info:
            print(f"\n[ERROR] Unknown bundle model id: {model_id!r}")
            sys.exit(1)
        if info["dir_name"] not in present:
            missing.append(info)
    if not missing:
        return
    flavor = "完整包" if BUNDLE_REQUIRE_MODEL_ID in required else "轻量包"
    print(f"\n[ERROR] {flavor}需要这些模型已在本地就绪：")
    for info in missing:
        print(f"  - {info['name']}（{info['dir_name']}）")
    print("  请在应用「设置 → 引擎」下载，或运行:")
    print("    python sayink_build/download_bundle_model_for_build.py")
    print("  目录可以是:")
    for src in model_search_dirs()[:2]:
        print(f"    {src}")
    sys.exit(1)


def _kill_running_sayink():
    if sys.platform != "win32":
        return
    subprocess.run(
        ["taskkill", "/IM", "SayInk.exe", "/F"],
        capture_output=True,
        text=True,
        check=False,
    )


def _prepare_dist_output_dir() -> tuple[Path, str]:
    """Return (dist_parent, pyinstaller --name) after trying to clear dist/SayInk."""
    dist_parent = SCRIPT_DIR / "dist"
    dist_parent.mkdir(parents=True, exist_ok=True)
    target = dist_parent / "SayInk"
    if not target.exists():
        return dist_parent, "SayInk"

    _kill_running_sayink()
    for attempt in range(4):
        try:
            shutil.rmtree(target)
            return dist_parent, "SayInk"
        except OSError as exc:
            print(f"[WARN] 无法删除 {target} (尝试 {attempt + 1}/4): {exc}")
            time.sleep(2)
            _kill_running_sayink()

    alt_name = f"SayInk_{int(time.time())}"
    print(f"[WARN] 将输出到备用目录 dist/{alt_name}/（请关闭占用 dist/SayInk 的程序后改回标准路径）")
    return dist_parent, alt_name


def build():
    """Package SayInk with PyInstaller."""
    from sayink_build.dependency_check import require_release_dependencies

    # Validate before killing a running app or clearing its previous build.
    require_release_dependencies()
    print("=" * 55)
    print("  SayInk Build Script")
    print("=" * 55)
    print()

    print("[1/3] Building SayInk with PyInstaller...")

    try:
        import PyInstaller.__main__
    except ImportError:
        print("\n[ERROR] PyInstaller 未安装（它只在打包时需要）: pip install -r requirements-build.txt")
        sys.exit(1)

    dist_parent, app_name = _prepare_dist_output_dir()

    main_script = str(SCRIPT_DIR / "sayink" / "main.py")
    win_dll_rthook = SCRIPT_DIR / "sayink_build" / "pyi_rth_sayink_win_dll.py"
    if not win_dll_rthook.is_file():
        print(f"\n[ERROR] Missing runtime hook: {win_dll_rthook}")
        sys.exit(1)

    spin_icons = SCRIPT_DIR / "sayink" / "ui" / "icons"
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
        f"--icon={SCRIPT_DIR / 'sayink' / 'icon.ico'}",
        # Non-imported PNG steppers used by settings QSS (Path(__file__).parent/icons).
        f"--add-data={spin_icons}{os.pathsep}sayink/ui/icons",
    ]

    if sys.platform == "win32":
        from sayink_build.pyinstaller_version_info import write_version_file

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
        "--hidden-import=win32timezone",
        "--hidden-import=PyQt6.QtNetwork",
        "--hidden-import=sherpa_onnx",
        "--hidden-import=onnxruntime",
    ])

    PyInstaller.__main__.run(args)

    dist_dir = dist_parent / app_name
    exe_path = dist_dir / "SayInk.exe"
    if app_name != "SayInk":
        marker = dist_parent / "SAYINK_STAGING_DIR.txt"
        marker.write_text(app_name, encoding="utf-8")
    elif (dist_parent / "SAYINK_STAGING_DIR.txt").exists():
        (dist_parent / "SAYINK_STAGING_DIR.txt").unlink(missing_ok=True)

    if not exe_path.exists():
        print("\n[ERROR] Build failed — SayInk.exe not found.")
        sys.exit(1)

    print()
    with_model = bundle_model_requested()
    models_dst = dist_dir / "models"
    model_ids = bundle_model_ids() if with_model else bundle_aux_model_ids()
    if with_model:
        print("[2/3] Copying Fun-ASR-Nano, CAM++ and Silero VAD to dist/SayInk/models/ ...")
    else:
        print("[2/3] Lite build: bundling CAM++ and Silero VAD (Fun-ASR-Nano is downloaded on first start).")
    downloaded = _find_model_sources(model_ids=model_ids)
    _require_bundle_model(downloaded, model_ids)
    _require_pinned_content(downloaded)

    models_dst.mkdir(parents=True, exist_ok=True)
    for dir_name, src_path in downloaded:
        dst_path = models_dst / dir_name
        if dst_path.exists():
            shutil.rmtree(dst_path)
        shutil.copytree(src_path, dst_path)
        print(f"    + {dir_name}")
    if not with_model:
        from sayink.speech_recognizer import get_model_info

        asr_dir = models_dst / get_model_info(BUNDLE_REQUIRE_MODEL_ID)["dir_name"]
        if asr_dir.exists():
            shutil.rmtree(asr_dir)

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

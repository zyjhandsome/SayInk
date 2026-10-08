"""Windows shell integration: taskbar identity and toast-friendly app name."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

log = logging.getLogger("SayInk")

APP_USER_MODEL_ID = "SayInk.SayInkApp"
APP_DISPLAY_NAME = "SayInk"


def set_windows_app_user_model_id() -> None:
    """Must run before QApplication / any top-level window is created."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception as exc:
        log.debug("AppUserModelID 设置失败: %s", exc)


def configure_windows_app_identity(app) -> None:
    """Qt display name + Start Menu shortcut for friendly shell labels."""
    app.setOrganizationName(APP_DISPLAY_NAME)
    app.setApplicationDisplayName(APP_DISPLAY_NAME)

    if sys.platform != "win32":
        return

    _ensure_start_menu_shortcut()


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _launcher_target() -> tuple[str, str, str]:
    """Return (target_path, arguments, working_directory) for the Start Menu shortcut."""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable)
        return str(exe), "", str(exe.parent)

    root = _project_root()
    icon_script = root / "run.py"
    python_dir = Path(sys.executable).parent
    pythonw = python_dir / "pythonw.exe"
    launcher = pythonw if pythonw.exists() else Path(sys.executable)
    return str(launcher), f'"{icon_script}"', str(root)


def _ensure_start_menu_shortcut() -> None:
    """Register Start Menu shortcut so taskbar/toast show 'SayInk' instead of 'Python'."""
    try:
        import pythoncom
        import win32com.client
        from win32com.propsys import propsys, pscon
    except ImportError:
        log.debug("pywin32 不可用，跳过开始菜单快捷方式注册")
        return

    try:
        start_menu = (
            Path(os.environ["APPDATA"])
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "Programs"
        )
        start_menu.mkdir(parents=True, exist_ok=True)
        lnk_path = start_menu / f"{APP_DISPLAY_NAME}.lnk"

        target, arguments, workdir = _launcher_target()
        shell = win32com.client.Dispatch("WScript.Shell")
        shortcut = shell.CreateShortCut(str(lnk_path))
        shortcut.Targetpath = target
        shortcut.Arguments = arguments
        shortcut.WorkingDirectory = workdir
        shortcut.Description = APP_DISPLAY_NAME

        icon_path = _project_root() / "sayink" / "icon.ico"
        if icon_path.exists():
            shortcut.IconLocation = f"{icon_path},0"

        shortcut.save()

        store = propsys.SHGetPropertyStoreFromParsingName(
            str(lnk_path),
            None,
            propsys.GPS_READWRITE,
            propsys.IID_IPropertyStore,
        )
        store.SetValue(
            pscon.PKEY_AppUserModel_ID,
            propsys.PROPVARIANTType(APP_USER_MODEL_ID, pythoncom.VT_LPWSTR),
        )
        store.Commit()
        log.info("已注册开始菜单快捷方式: %s", lnk_path)
    except Exception as exc:
        log.debug("开始菜单快捷方式注册失败: %s", exc)

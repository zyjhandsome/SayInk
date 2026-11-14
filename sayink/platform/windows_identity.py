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

        fn = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
        if hasattr(fn, "argtypes"):
            fn.argtypes = [ctypes.c_wchar_p]
            fn.restype = ctypes.HRESULT
        fn(APP_USER_MODEL_ID)
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


def _norm_win_path(value: str) -> str:
    return os.path.normcase(os.path.normpath(value.strip())) if value and value.strip() else ""


def _installed_group_shortcuts(user_programs: Path) -> list[Path]:
    """Return existing Inno Setup `{group}\\SayInk.lnk` candidates (all-users & per-user)."""
    candidates: list[Path] = []
    program_data = os.environ.get("PROGRAMDATA", "").strip()
    if program_data:
        candidates.append(
            Path(program_data)
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "Programs"
            / APP_DISPLAY_NAME
            / f"{APP_DISPLAY_NAME}.lnk"
        )
    candidates.append(user_programs / APP_DISPLAY_NAME / f"{APP_DISPLAY_NAME}.lnk")
    return [p for p in candidates if p.is_file()]


def _read_shortcut_app_id(lnk_path: Path, propsys, pscon) -> str:
    try:
        gps_default = getattr(propsys, "GPS_DEFAULT", 0)
        store = propsys.SHGetPropertyStoreFromParsingName(
            str(lnk_path),
            None,
            gps_default,
            propsys.IID_IPropertyStore,
        )
        prop = store.GetValue(pscon.PKEY_AppUserModel_ID)
        value = prop.GetValue() if hasattr(prop, "GetValue") else None
        return str(value or "")
    except Exception:
        return ""


def _stamp_shortcut_app_id(lnk_path: Path, pythoncom, propsys, pscon) -> None:
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


def _ensure_start_menu_shortcut() -> None:
    """Register Start Menu shortcut idempotently without duplicating or clobbering installs."""
    try:
        import pythoncom
        import win32com.client
        from win32com.propsys import propsys, pscon
    except ImportError:
        log.debug("pywin32 不可用，跳过开始菜单快捷方式注册")
        return

    try:
        appdata = os.environ.get("APPDATA", "").strip()
        if not appdata:
            return
        start_menu = (
            Path(appdata)
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "Programs"
        )
        start_menu.mkdir(parents=True, exist_ok=True)
        lnk_path = start_menu / f"{APP_DISPLAY_NAME}.lnk"
        is_frozen = bool(getattr(sys, "frozen", False))
        group_shortcuts = _installed_group_shortcuts(start_menu)

        # Running from source must never overwrite an installed SayInk shortcut or
        # create a duplicate second entry alongside Setup's Programs\SayInk\SayInk.lnk.
        if not is_frozen and group_shortcuts:
            log.debug("检测到已安装版开始菜单快捷方式，源码模式跳过写入: %s", group_shortcuts[0])
            return

        target, arguments, workdir = _launcher_target()

        if is_frozen and group_shortcuts:
            # Setup already created {group}\SayInk.lnk; remove any legacy duplicate
            # top-level Programs\SayInk.lnk and ensure AppUserModelID is stamped if writable.
            if lnk_path.is_file():
                try:
                    lnk_path.unlink()
                except OSError:
                    pass
            primary = group_shortcuts[0]
            if _read_shortcut_app_id(primary, propsys, pscon) != APP_USER_MODEL_ID:
                try:
                    _stamp_shortcut_app_id(primary, pythoncom, propsys, pscon)
                except Exception:
                    pass
            return

        shell = win32com.client.Dispatch("WScript.Shell")

        if lnk_path.is_file():
            existing = shell.CreateShortCut(str(lnk_path))
            existing_target = getattr(existing, "Targetpath", "") or ""
            existing_args = getattr(existing, "Arguments", "") or ""
            existing_workdir = getattr(existing, "WorkingDirectory", "") or ""
            if not is_frozen and existing_target.lower().endswith("sayink.exe"):
                log.debug("保留正式版开始菜单快捷方式，不被源码运行覆盖: %s", existing_target)
                return
            if (
                _norm_win_path(existing_target) == _norm_win_path(target)
                and existing_args.strip() == arguments.strip()
                and _norm_win_path(existing_workdir) == _norm_win_path(workdir)
                and _read_shortcut_app_id(lnk_path, propsys, pscon) == APP_USER_MODEL_ID
            ):
                return
            shortcut = existing
        else:
            shortcut = shell.CreateShortCut(str(lnk_path))

        shortcut.Targetpath = target
        shortcut.Arguments = arguments
        shortcut.WorkingDirectory = workdir
        shortcut.Description = APP_DISPLAY_NAME

        if is_frozen:
            shortcut.IconLocation = f"{target},0"
        else:
            icon_path = _project_root() / "sayink" / "icon.ico"
            if icon_path.exists():
                shortcut.IconLocation = f"{icon_path},0"

        shortcut.save()
        _stamp_shortcut_app_id(lnk_path, pythoncom, propsys, pscon)
        log.info("已注册开始菜单快捷方式: %s", lnk_path)
    except Exception as exc:
        log.debug("开始菜单快捷方式注册失败: %s", exc)

"""SayInk launcher — run from the project root directory."""
import os
import sys
from pathlib import Path

# Python 3.14+ no longer prepends the script directory to sys.path.
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Windows: AppUserModelID must be set before Qt creates any window.
if sys.platform == "win32":
    from sayink.platform.windows_identity import set_windows_app_user_model_id

    set_windows_app_user_model_id()

def _report_startup_error(message: str) -> None:
    stream = sys.stderr if sys.stderr is not None else sys.stdout
    if stream is not None:
        print(message, file=stream)
    if (
        sys.platform == "win32"
        and sys.stderr is None
        and not os.environ.get("PYTEST_CURRENT_TEST")
    ):
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.MessageBoxW.argtypes = [
                wintypes.HWND,
                wintypes.LPCWSTR,
                wintypes.LPCWSTR,
                wintypes.UINT,
            ]
            user32.MessageBoxW.restype = ctypes.c_int
            user32.MessageBoxW(None, message, "SayInk 启动失败", 0x00000010)
        except Exception:
            pass


def _check_runtime() -> None:
    try:
        import PyQt6  # noqa: F401
    except ModuleNotFoundError:
        _report_startup_error(
            f"未找到 PyQt6。当前 Python: {sys.executable}\n"
            "请先安装依赖: pip install -r requirements.txt\n"
            "若当前 Python 无 pip 或未装依赖，Windows 可改用: py -3.10 run.py"
        )
        sys.exit(1)


if __name__ == "__main__":
    _check_runtime()
    from sayink.main import main

    main()

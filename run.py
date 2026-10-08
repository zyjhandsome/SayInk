"""SayInk launcher — run from the project root directory."""
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

def _check_runtime() -> None:
    try:
        import PyQt6  # noqa: F401
    except ModuleNotFoundError:
        print(f"未找到 PyQt6。当前 Python: {sys.executable}")
        print("请先安装依赖: pip install -r requirements.txt")
        print("若当前 Python 无 pip 或未装依赖，Windows 可改用: py -3.10 run.py")
        sys.exit(1)


from sayink.main import main

if __name__ == "__main__":
    _check_runtime()
    main()

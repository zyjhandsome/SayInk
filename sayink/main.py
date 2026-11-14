import sys
import os
import time
import atexit
import logging
import threading
import ctypes
from ctypes import wintypes

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

_lock_file_path: str | None = None
_win_mutex = None
_last_block_reason: str | None = None

MUTEX_NAME = "Local\\SayInk_Single_Instance_Mutex"
# VoiceInk ≤ 2.1.0 (same program, old name) owns this one while it runs. A
# SayInk started next to it would record the same hotkey twice.
LEGACY_MUTEX_NAME = "Local\\VoiceInk_Single_Instance_Mutex"
_SYNCHRONIZE = 0x00100000
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_ACCESS_DENIED = 5
_ERROR_ALREADY_EXISTS = 183
_STILL_ACTIVE = 259
_MB_OK_INFO = 0x00000040 | 0x00010000  # MB_ICONINFORMATION | MB_SETFOREGROUND

# 在 main() 配置日志前，使用临时 logger
_log = logging.getLogger("SayInk")
_configured_kernel32 = None


def _configure_kernel32_signatures(dll):
    """Bind explicit 64-bit HANDLE/BOOL/DWORD signatures so 64-bit handles never truncate."""
    if hasattr(dll.CreateMutexW, "argtypes"):
        dll.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        dll.CreateMutexW.restype = wintypes.HANDLE
    if hasattr(dll.OpenMutexW, "argtypes"):
        dll.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        dll.OpenMutexW.restype = wintypes.HANDLE
    open_proc = getattr(dll, "OpenProcess", None)
    if open_proc is not None and hasattr(open_proc, "argtypes"):
        open_proc.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_proc.restype = wintypes.HANDLE
    get_exit = getattr(dll, "GetExitCodeProcess", None)
    if get_exit is not None and hasattr(get_exit, "argtypes"):
        get_exit.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        get_exit.restype = wintypes.BOOL
    if hasattr(dll.CloseHandle, "argtypes"):
        dll.CloseHandle.argtypes = [wintypes.HANDLE]
        dll.CloseHandle.restype = wintypes.BOOL
    return dll


def _resolve_kernel32():
    """Return `(kernel32_obj, uses_ctypes_last_error)` honoring test monkeypatches."""
    global _configured_kernel32
    windll_obj = getattr(ctypes, "windll", None)
    current = getattr(windll_obj, "kernel32", None)
    if (
        sys.platform == "win32"
        and hasattr(ctypes, "WinDLL")
        and isinstance(current, ctypes.WinDLL)
    ):
        if _configured_kernel32 is None:
            _configured_kernel32 = _configure_kernel32_signatures(
                ctypes.WinDLL("kernel32", use_last_error=True)
            )
        return _configured_kernel32, True
    if current is not None:
        _configure_kernel32_signatures(current)
    return current, False


def _close_mutex_handle(handle) -> None:
    if not handle:
        return
    k32, _ = _resolve_kernel32()
    if k32 is not None:
        k32.CloseHandle(handle)


def last_block_reason() -> str | None:
    """Return `'sayink'`, `'legacy_voiceink'`, `'lock_file'`, or `None` after `check_single_instance()`."""
    return _last_block_reason


def _legacy_instance_running(kernel32, *, use_ctypes_err: bool = False) -> bool:
    if use_ctypes_err:
        ctypes.set_last_error(0)
    handle = kernel32.OpenMutexW(_SYNCHRONIZE, False, LEGACY_MUTEX_NAME)
    if not handle:
        err = (
            ctypes.get_last_error()
            if use_ctypes_err
            else (kernel32.GetLastError() if hasattr(kernel32, "GetLastError") else 0)
        )
        return err == _ERROR_ACCESS_DENIED
    kernel32.CloseHandle(handle)
    return True


def _pid_is_running(pid: int) -> bool:
    """Non-destructively probe whether *pid* is alive (os.kill(pid, 0) terminates on Windows)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        kernel32, use_ctypes_err = _resolve_kernel32()
        open_proc = getattr(kernel32, "OpenProcess", None) if kernel32 is not None else None
        if open_proc is None and hasattr(ctypes, "WinDLL"):
            kernel32 = _configure_kernel32_signatures(
                ctypes.WinDLL("kernel32", use_last_error=True)
            )
            open_proc = kernel32.OpenProcess
            use_ctypes_err = True
        if open_proc is None:
            return False
        if use_ctypes_err:
            ctypes.set_last_error(0)
        handle = open_proc(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            err = (
                ctypes.get_last_error()
                if use_ctypes_err
                else (kernel32.GetLastError() if hasattr(kernel32, "GetLastError") else 0)
            )
            return err == _ERROR_ACCESS_DENIED
        try:
            get_exit = getattr(kernel32, "GetExitCodeProcess", None)
            if get_exit is None:
                return True
            exit_code = wintypes.DWORD()
            if not get_exit(handle, ctypes.byref(exit_code)):
                return True
            return int(exit_code.value) == _STILL_ACTIVE
        finally:
            close_fn = getattr(kernel32, "CloseHandle", None)
            if close_fn is not None:
                close_fn(handle)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (ProcessLookupError, OSError):
        return False


def check_single_instance() -> bool:
    """Ensure only one instance of SayInk runs at a time."""
    global _last_block_reason
    _last_block_reason = None

    if sys.platform == "win32":
        # 使用 ctypes 创建 Windows 互斥锁（显式声明 64 位 HANDLE + use_last_error）
        mutex = None
        kernel32 = None
        try:
            mutex_name = MUTEX_NAME
            kernel32, use_ctypes_err = _resolve_kernel32()
            if use_ctypes_err:
                ctypes.set_last_error(0)

            mutex = kernel32.CreateMutexW(None, False, mutex_name)
            last_error = ctypes.get_last_error() if use_ctypes_err else kernel32.GetLastError()

            if last_error == _ERROR_ALREADY_EXISTS or (not mutex and last_error == _ERROR_ACCESS_DENIED):
                # 互斥锁已存在（或由提权实例持有），说明已有实例运行
                _last_block_reason = "sayink"
                if mutex:
                    kernel32.CloseHandle(mutex)
                return False

            if _legacy_instance_running(kernel32, use_ctypes_err=use_ctypes_err):
                _last_block_reason = "legacy_voiceink"
                _log.warning("检测到旧版 VoiceInk 正在运行，SayInk 不再启动第二个实例")
                if mutex:
                    kernel32.CloseHandle(mutex)
                return False

            global _win_mutex
            _win_mutex = mutex
            mutex = None
            return True
        except Exception as e:
            if mutex:
                try:
                    if kernel32 is not None:
                        kernel32.CloseHandle(mutex)
                    else:
                        _close_mutex_handle(mutex)
                except Exception:
                    pass
            _log.warning("互斥锁创建失败，使用文件锁: %s", e)

    # 非 Windows 或互斥锁失败时，使用文件锁
    global _lock_file_path
    import tempfile
    lock_file = os.path.join(tempfile.gettempdir(), "sayink.lock")
    _lock_file_path = lock_file

    try:
        if os.path.exists(lock_file):
            with open(lock_file, "r") as f:
                old_pid_str = f.read().strip()
            if old_pid_str.isdigit():
                old_pid = int(old_pid_str)
                if _pid_is_running(old_pid):
                    # 进程存在，说明已有实例
                    _last_block_reason = "lock_file"
                    return False

        with open(lock_file, "w") as f:
            f.write(str(os.getpid()))
        return True
    except OSError as e:
        _log.warning("文件锁创建失败: %s", e)
        return True


def cleanup_lock():
    """清理单实例锁（幂等，可在退出排空前提前调用）"""
    global _win_mutex, _lock_file_path, _crash_file

    # Windows: 关闭互斥锁句柄
    if _win_mutex:
        try:
            _close_mutex_handle(_win_mutex)
        except Exception:
            pass
        _win_mutex = None

    # 文件锁: 删除锁文件
    if _lock_file_path:
        try:
            os.remove(_lock_file_path)
        except OSError:
            pass
        _lock_file_path = None

    if _crash_file is not None:
        try:
            import faulthandler

            faulthandler.disable()
            _crash_file.close()
        except Exception:
            pass
        _crash_file = None


def release_instance_lock(activation_server=None) -> None:
    """Close the activation pipe and release the single-instance lock idempotently."""
    if activation_server is not None:
        try:
            activation_server.close()
        except Exception:
            pass
    cleanup_lock()


def _install_exception_hooks(log: logging.Logger):
    """Install global exception handlers so crashes are logged, not silently lost."""

    def _excepthook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        log.critical("未捕获异常", exc_info=(exc_type, exc_value, exc_tb))

    def _threading_excepthook(args):
        if args.exc_type is SystemExit:
            return
        log.critical(
            "线程 %s 未捕获异常", args.thread.name if args.thread else "?",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _excepthook
    threading.excepthook = _threading_excepthook


def activation_server_name() -> str:
    import getpass

    try:
        user = getpass.getuser()
    except Exception:
        user = "default"
    # Named pipes are machine-wide; the single-instance mutex is per session.
    return f"SayInk-activate-{user}"


def _activate_running_instance(*, retries: int = 1, retry_delay_s: float = 0.25) -> bool:
    """Ask the running instance to show its window instead of exiting silently."""
    attempts = max(1, int(retries))
    for attempt in range(attempts):
        try:
            from PyQt6.QtCore import QCoreApplication
            from PyQt6.QtNetwork import QLocalSocket

            _app = QCoreApplication.instance() or QCoreApplication(sys.argv)
            sock = QLocalSocket()
            try:
                sock.connectToServer(activation_server_name())
                if sock.waitForConnected(1000):
                    sock.write(b"show\n")
                    sock.flush()
                    sock.waitForBytesWritten(1000)
                    sock.disconnectFromServer()
                    return True
            finally:
                abort_fn = getattr(sock, "abort", None)
                if callable(abort_fn):
                    abort_fn()
        except Exception:
            pass
        if attempt + 1 < attempts and retry_delay_s > 0:
            time.sleep(retry_delay_s)
    return False


def _notify_already_running(reason: str | None = None, *, message_box=None) -> str:
    """Log and surface a friendly notice when an existing instance cannot be activated."""
    if reason == "legacy_voiceink":
        msg = (
            "检测到旧版 VoiceInk 正在后台运行。\n"
            "请先在系统托盘退出旧版 VoiceInk，再启动 SayInk（避免重复监听同一快捷键）。"
        )
    else:
        msg = (
            "SayInk 已在后台运行（或正在启动中）。\n"
            "可在屏幕右下角系统托盘找到麦克风图标打开主界面。"
        )
    _log.warning(msg.replace("\n", " "))
    if message_box is not None:
        message_box(msg)
        return msg
    if (
        sys.platform == "win32"
        and not os.environ.get("PYTEST_CURRENT_TEST")
        and os.environ.get("QT_QPA_PLATFORM", "").strip().lower() != "offscreen"
    ):
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.MessageBoxW.argtypes = [
                wintypes.HWND,
                wintypes.LPCWSTR,
                wintypes.LPCWSTR,
                wintypes.UINT,
            ]
            user32.MessageBoxW.restype = ctypes.c_int
            user32.MessageBoxW(None, msg, "SayInk", _MB_OK_INFO)
        except Exception:
            pass
    return msg


def _handle_existing_instance(*, retries: int = 6, retry_delay_s: float = 0.25, message_box=None) -> bool:
    """Wake the running instance if reachable, otherwise notify the user why startup exited."""
    reason = last_block_reason()
    if reason != "legacy_voiceink":
        if _activate_running_instance(retries=retries, retry_delay_s=retry_delay_s):
            return True
    _notify_already_running(reason, message_box=message_box)
    return False


def start_activation_server(on_activate):
    from PyQt6.QtNetwork import QLocalServer, QLocalSocket

    server = QLocalServer()
    name = activation_server_name()
    QLocalServer.removeServer(name)
    if not server.listen(name):
        _log.warning("唤起服务启动失败: %s", server.errorString())
        return None

    def _on_connection():
        while server.hasPendingConnections():
            conn = server.nextPendingConnection()
            if conn is None:
                break
            if conn.bytesAvailable():
                conn.readAll()
            if conn.state() == QLocalSocket.LocalSocketState.UnconnectedState:
                conn.deleteLater()
            else:
                conn.readyRead.connect(conn.readAll)
                conn.disconnected.connect(conn.deleteLater)
            on_activate()

    server.newConnection.connect(_on_connection)
    return server


LOG_FILE_MAX_BYTES = 1_000_000
LOG_FILE_BACKUPS = 3
_crash_file = None


def log_dir() -> str:
    from sayink.user_data import user_data_dir

    return os.path.join(str(user_data_dir()), "logs")


def setup_logging(directory: str | None = None) -> str | None:
    """Log to a rotating file; the windowed build has no console to read."""
    import faulthandler
    from logging.handlers import RotatingFileHandler

    global _crash_file
    directory = directory or log_dir()
    fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)s %(threadName)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handlers: list[logging.Handler] = []
    log_path = None
    try:
        os.makedirs(directory, exist_ok=True)
        log_path = os.path.join(directory, "sayink.log")
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=LOG_FILE_MAX_BYTES,
            backupCount=LOG_FILE_BACKUPS,
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        handlers.append(file_handler)
        # Native crashes (ONNX Runtime, PortAudio) never reach excepthook.
        # A buffered text file can vanish with the process before the dump is flushed.
        if _crash_file is not None:
            try:
                faulthandler.disable()
                _crash_file.close()
            except Exception:
                pass
            _crash_file = None
        _crash_file = open(os.path.join(directory, "crash.log"), "ab", buffering=0)  # noqa: SIM115
        faulthandler.enable(file=_crash_file, all_threads=True)
    except OSError as exc:
        log_path = None
        if sys.stderr is not None:
            print(f"SayInk: 无法写入日志目录 {directory}: {exc}", file=sys.stderr)
    if sys.stderr is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt="%H:%M:%S"))
        handlers.append(console)
    logging.basicConfig(level=_configured_log_level(), handlers=handlers, force=True)
    return log_path


def _configured_log_level() -> int:
    """``SAYINK_LOG_LEVEL=DEBUG`` makes device and clipboard diagnostics visible."""
    name = os.environ.get("SAYINK_LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, name, None)
    if not isinstance(level, int):
        return logging.INFO
    return level


def main():
    setup_logging()
    log = logging.getLogger("SayInk")
    _install_exception_hooks(log)

    if not check_single_instance():
        _handle_existing_instance()
        sys.exit(0)

    atexit.register(cleanup_lock)
    log.info("SayInk 启动中...")

    if sys.platform == "win32":
        from sayink.platform.windows_identity import set_windows_app_user_model_id

        set_windows_app_user_model_id()

    from PyQt6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("SayInk")

    say_ink = None
    _early_activate_requested = False

    def _dispatch_activation() -> None:
        nonlocal _early_activate_requested
        if say_ink is not None:
            say_ink.show_main_window()
        else:
            _early_activate_requested = True

    _activation_server = start_activation_server(_dispatch_activation)

    from sayink.ui.tray_icon import create_microphone_icon
    app.setWindowIcon(create_microphone_icon(recording=False, size=64))

    # Windows: AppUserModelID + Start Menu shortcut for taskbar icon and friendly toast title
    from sayink.platform.windows_identity import configure_windows_app_identity
    configure_windows_app_identity(app)

    from PyQt6.QtCore import Qt

    from sayink.ui.app_styles import DropDownComboStyle
    app.setStyle(DropDownComboStyle("Fusion"))
    # Windows' "slide open combo boxes" rolls a snapshot of the popup, then
    # swaps in the real one; the swap reads as a flash on dark surfaces.
    app.setEffectEnabled(Qt.UIEffect.UI_AnimateCombo, False)

    from sayink.config import Config
    from sayink.ui.theme import apply_theme

    _boot_config = Config()
    apply_theme(app, mode=_boot_config.get("appearance.theme_mode", "dark"))

    from sayink.app import App

    _instance_released = False

    def _release_once() -> None:
        nonlocal _activation_server, _instance_released
        if _instance_released:
            return
        _instance_released = True
        srv, _activation_server = _activation_server, None
        release_instance_lock(srv)

    # Reuse it: a second Config would not see a plaintext API key the first one
    # already moved out of config.json when Credential Manager was unreadable.
    say_ink = App(_boot_config, on_release_instance_lock=_release_once)
    say_ink.start()
    if _early_activate_requested:
        _early_activate_requested = False
        say_ink.show_main_window()

    exit_code = app.exec()
    _release_once()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

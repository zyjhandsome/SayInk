import sys
import os
import atexit
import logging
import threading
import ctypes

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

_lock_file_path: str | None = None
_win_mutex = None

# 在 main() 配置日志前，使用临时 logger
_log = logging.getLogger("SayInk")


def check_single_instance() -> bool:
    """Ensure only one instance of SayInk runs at a time."""
    if sys.platform == "win32":
        # 使用 ctypes 创建 Windows 互斥锁（更可靠，不需要 pywin32）
        try:
            mutex_name = "Local\\SayInk_Single_Instance_Mutex"
            # CreateMutex 参数: lpMutexAttributes=NULL, bInitialOwner=False, lpName
            kernel32 = ctypes.windll.kernel32
            ERROR_ALREADY_EXISTS = 183  # Windows 错误代码

            mutex = kernel32.CreateMutexW(None, False, mutex_name)
            last_error = kernel32.GetLastError()

            if last_error == ERROR_ALREADY_EXISTS:
                # 互斥锁已存在，说明已有实例运行
                if mutex:
                    kernel32.CloseHandle(mutex)
                return False

            global _win_mutex
            _win_mutex = mutex
            return True
        except Exception as e:
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
                try:
                    os.kill(old_pid, 0)
                    # 进程存在，说明已有实例
                    return False
                except (OSError, ProcessLookupError):
                    # 进程不存在，可以启动
                    pass

        with open(lock_file, "w") as f:
            f.write(str(os.getpid()))
        return True
    except OSError as e:
        _log.warning("文件锁创建失败: %s", e)
        return True


def cleanup_lock():
    """清理单实例锁"""
    global _win_mutex

    # Windows: 关闭互斥锁句柄
    if _win_mutex:
        try:
            ctypes.windll.kernel32.CloseHandle(_win_mutex)
            _win_mutex = None
        except Exception:
            pass

    # 文件锁: 删除锁文件
    if _lock_file_path:
        try:
            os.remove(_lock_file_path)
        except OSError:
            pass


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


def _activate_running_instance() -> bool:
    """Ask the running instance to show its window instead of exiting silently."""
    try:
        from PyQt6.QtCore import QCoreApplication
        from PyQt6.QtNetwork import QLocalSocket

        _app = QCoreApplication.instance() or QCoreApplication(sys.argv)
        sock = QLocalSocket()
        sock.connectToServer(activation_server_name())
        if not sock.waitForConnected(1000):
            return False
        sock.write(b"show\n")
        sock.flush()
        sock.waitForBytesWritten(1000)
        sock.disconnectFromServer()
        return True
    except Exception:
        return False


def start_activation_server(on_activate):
    from PyQt6.QtNetwork import QLocalServer

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
        _crash_file = open(os.path.join(directory, "crash.log"), "a", encoding="utf-8")
        faulthandler.enable(file=_crash_file, all_threads=True)
    except OSError as exc:
        log_path = None
        if sys.stderr is not None:
            print(f"SayInk: 无法写入日志目录 {directory}: {exc}", file=sys.stderr)
    if sys.stderr is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt="%H:%M:%S"))
        handlers.append(console)
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    return log_path


def main():
    if not check_single_instance():
        _activate_running_instance()
        sys.exit(0)

    atexit.register(cleanup_lock)

    setup_logging()
    log = logging.getLogger("SayInk")
    _install_exception_hooks(log)
    log.info("SayInk 启动中...")

    if sys.platform == "win32":
        from sayink.platform.windows_identity import set_windows_app_user_model_id

        set_windows_app_user_model_id()

    from PyQt6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("SayInk")

    from sayink.ui.tray_icon import create_microphone_icon
    app.setWindowIcon(create_microphone_icon(recording=False, size=64))

    # Windows: AppUserModelID + Start Menu shortcut for taskbar icon and friendly toast title
    from sayink.platform.windows_identity import configure_windows_app_identity
    configure_windows_app_identity(app)

    app.setStyle("Fusion")

    from sayink.config import Config
    from sayink.ui.theme import apply_theme

    _boot_config = Config()
    apply_theme(app, mode=_boot_config.get("appearance.theme_mode", "dark"))

    from sayink.app import App
    say_ink = App()
    say_ink.start()
    _activation_server = start_activation_server(say_ink.show_main_window)

    exit_code = app.exec()
    cleanup_lock()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

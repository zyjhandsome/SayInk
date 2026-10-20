"""First-run welcome and the one-time history opt-in, kept out of the App.

Both dialogs wait for the model to be ready (or a timeout) so they never
cover the 「模型加载中」 state; the welcome always precedes the history
question.
"""

from __future__ import annotations

import logging
from typing import Callable

from PyQt6.QtCore import QObject, Qt, QTimer
from PyQt6.QtWidgets import QMessageBox, QWidget

from sayink.config import DEFAULT_HOTKEY, format_hotkey
from sayink.speech_recognizer import DEFAULT_MODEL_ID, get_model_info

log = logging.getLogger("SayInk")

WELCOME_FALLBACK_MS = 15000
WELCOME_DELAY_MS = 400
HISTORY_AFTER_WELCOME_MS = 200
HISTORY_AFTER_READY_MS = 500


def message_box(parent: QWidget | None, title: str, text: str, icon) -> QMessageBox:
    """Message box owned by the main window; a tray-only app has no other
    anchor, so without one the box stays on top instead of opening behind
    whatever the user is working in."""
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    box.setIcon(icon)
    if parent is None:
        box.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
    return box


def has_any_downloaded_model() -> bool:
    from sayink.speech_recognizer import get_downloaded_models

    try:
        return bool(get_downloaded_models())
    except Exception:
        log.debug("无法枚举已下载模型", exc_info=True)
        return False


class OnboardingController(QObject):
    def __init__(
        self,
        config,
        *,
        parent: QObject | None,
        dialog_parent: Callable[[], QWidget | None],
        is_continuous_mode: Callable[[], bool],
        show_main_window: Callable[[str], None],
        model_installed: Callable[[], bool] = has_any_downloaded_model,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._dialog_parent = dialog_parent
        self._is_continuous_mode = is_continuous_mode
        self._show_main_window = show_main_window
        self._model_installed = model_installed
        self._ready_signal = None
        self._welcome_scheduled = False

    # ── Scheduling ─────────────────────────────────────

    def schedule(self, ready_signal) -> None:
        """Called once from App.start(); ``ready_signal`` is the recognizer's ``ready``."""
        self._ready_signal = ready_signal
        if not self._config.get("first_run_welcome_seen", True):
            ready_signal.connect(self.show_welcome_once)
            QTimer.singleShot(WELCOME_FALLBACK_MS, self.show_welcome_once)
        elif not self._config.get("history.onboarded", False):
            ready_signal.connect(self.show_history_onboarding_once)

    def _disconnect_ready(self, slot) -> None:
        if self._ready_signal is None:
            return
        try:
            self._ready_signal.disconnect(slot)
        except TypeError:
            pass

    def show_welcome_once(self) -> None:
        # Both model-ready and the fallback timer land here; the seen flag is
        # only written after the modal closes, so guard the scheduling itself.
        if self._config.get("first_run_welcome_seen", True) or self._welcome_scheduled:
            return
        self._welcome_scheduled = True
        self._disconnect_ready(self.show_welcome_once)
        QTimer.singleShot(WELCOME_DELAY_MS, self.show_welcome)

    def show_history_onboarding_once(self) -> None:
        if self._config.get("history.onboarded", False):
            return
        self._disconnect_ready(self.show_history_onboarding_once)
        QTimer.singleShot(HISTORY_AFTER_READY_MS, self.show_history_onboarding)

    # ── Dialogs ────────────────────────────────────────

    def welcome_text(self, *, model_missing: bool) -> str:
        hotkey = format_hotkey(self._config.get("hotkey", DEFAULT_HOTKEY))
        if self._is_continuous_mode():
            stop = "按 Esc 或转写条「结束」停止" if self._config.get("audio.esc_stops_continuous", True) else "点转写条「结束」停止"
            mode_tip = (
                f"当前为「持续转写」：按住 {hotkey} 启动，松开后继续录制；{stop}。"
            )
        else:
            mode_tip = f"当前为「单次转写」：按住 {hotkey} 录音，松开后识别；Esc 取消本次录音。"
        if model_missing:
            info = get_model_info(DEFAULT_MODEL_ID) or {}
            model_tip = (
                f"本机还没有语音模型：请先在设置 → 引擎 中下载 {info.get('name', DEFAULT_MODEL_ID)}"
                f"（约 {info.get('size_mb', '?')} MB，仅需一次），下载前无法转写。\n\n"
            )
        else:
            model_tip = "语音模型已下载，载入完成后即可试用。\n\n"
        history_only = self._config.get("output.mode", "paste") == "history"
        result_tip = (
            "结果仅保存到「历史」，不会输入其他应用。" if history_only and self._config.get("history.enabled", False) else
            "结果当前不会保存或输入其他应用：请在通用页开启转写历史，或修改「结果去向」。" if history_only else
            "试用时，先把光标放到要输入文字的位置。"
        )
        return (
            "SayInk 在本地完成语音识别；可选的 AI 润色会使用你配置的服务。\n\n"
            + model_tip
            + f"{mode_tip}\n\n"
            + result_tip + "\n\n"
            "音源与结果去向：通用页。\n"
            "双击托盘图标可打开主窗口。"
        )

    def show_welcome(self) -> None:
        model_missing = not self._model_installed()
        box = message_box(
            self._dialog_parent(),
            "欢迎使用 SayInk",
            self.welcome_text(model_missing=model_missing),
            QMessageBox.Icon.Information,
        )
        download_btn = None
        if model_missing:
            download_btn = box.addButton("去下载模型", QMessageBox.ButtonRole.ActionRole)
        box.addButton("知道了", QMessageBox.ButtonRole.AcceptRole)
        box.exec()
        self._config.set("first_run_welcome_seen", True)
        if download_btn is not None and box.clickedButton() is download_btn:
            self._show_main_window("engine")
        # Sequence: history onboarding only after welcome is dismissed.
        if not self._config.get("history.onboarded", False):
            QTimer.singleShot(HISTORY_AFTER_WELCOME_MS, self.show_history_onboarding)

    def show_history_onboarding(self) -> None:
        if self._config.get("history.onboarded", False):
            return
        enabled = self.ask_history_enabled()
        self._config.set("history.enabled", enabled)
        self._config.set("history.onboarded", True)

    def ask_history_enabled(self) -> bool:
        text = (
            "SayInk 可以在本机保存转写历史，方便稍后搜索、复制或导出。\n\n"
            "历史只保存在本地；关闭后只会停止未来写入，不会删除已有数据。\n"
            "随时可以在设置关闭或调整保留策略。"
        )
        box = message_box(self._dialog_parent(), "开启转写历史？", text, QMessageBox.Icon.Question)
        enable_btn = box.addButton("开启", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("暂不开启", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(enable_btn)
        box.exec()
        return box.clickedButton() == enable_btn

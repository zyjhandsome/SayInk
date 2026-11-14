"""First-run welcome and the one-time history opt-in, kept out of the App.

Both dialogs wait for the model to be ready (or a timeout) so they never
cover the 「模型加载中」 state; the welcome always precedes the history
question.
"""

from __future__ import annotations

import logging
from typing import Callable

from PyQt6.QtCore import QEvent, QObject, Qt, QThread, QTimer
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sayink.config import DEFAULT_HOTKEY, format_hotkey
from sayink.speech_recognizer import DEFAULT_MODEL_ID, get_model_info

log = logging.getLogger("SayInk")

WELCOME_FALLBACK_MS = 15000
WELCOME_DELAY_MS = 400
HISTORY_AFTER_WELCOME_MS = 200
HISTORY_AFTER_READY_MS = 500
_MIC_HEARD_THRESHOLD = 0.0015
_SETUP_PROBE_MS = 2000
_SETUP_TRIAL_MS = 6000


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
        self._setup_dialog: _SetupDialog | None = None
        self._setup_closed = False
        self._setup_reopened = False
        self._setup_open_engine = False
        self._setup_mode = ""
        self._setup_recorder_obj = None
        self._setup_watch: QTimer | None = None
        self._countdown_timer: QTimer | None = None
        self._trial_remaining_sec = 0
        self._capture_gen = 0
        self._probe_peak = 0.0
        self._trial_audio = None
        self._trial_worker = None
        self._download_worker = None
        self._download_threads: list[object] = []
        self._mic_heard = False
        self._paused_hotkey_for_setup = False

    @property
    def running_workers(self):
        workers = []
        for candidate in (
            self._download_worker,
            *self._download_threads,
            self._trial_worker,
            *self.findChildren(QThread),
        ):
            if candidate is not None and candidate not in workers:
                is_running = getattr(candidate, "isRunning", None)
                if callable(is_running) and is_running():
                    workers.append(candidate)
        return tuple(workers)

    def shutdown(self) -> None:
        self._stop_setup_audio()

    def refresh_theme(self) -> None:
        if self._setup_dialog is not None:
            self._setup_dialog.apply_theme_styles()

    def _pause_global_hotkey(self) -> None:
        if self._paused_hotkey_for_setup:
            return
        hotkey_mgr = getattr(self.parent(), "_hotkey_mgr", None)
        if hotkey_mgr is None:
            return
        if getattr(hotkey_mgr, "_paused", False):
            return
        pause_fn = getattr(hotkey_mgr, "pause", None)
        if callable(pause_fn):
            pause_fn()
            self._paused_hotkey_for_setup = True

    def _resume_global_hotkey(self) -> None:
        if not self._paused_hotkey_for_setup:
            return
        self._paused_hotkey_for_setup = False
        hotkey_mgr = getattr(self.parent(), "_hotkey_mgr", None)
        resume_fn = getattr(hotkey_mgr, "resume", None) if hotkey_mgr is not None else None
        if callable(resume_fn):
            resume_fn()

    # ── Scheduling ─────────────────────────────────────

    def schedule(self, ready_signal) -> None:
        """Called once from App.start(); ``ready_signal`` is the recognizer's ``ready``."""
        self._ready_signal = ready_signal
        if not self._config.get("first_run_welcome_seen", True):
            if not self._model_installed():
                QTimer.singleShot(WELCOME_DELAY_MS, self.show_setup_once)
                return
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

    def show_setup_once(self) -> None:
        if self._config.get("first_run_welcome_seen", True) or self._welcome_scheduled:
            return
        if self._model_installed():
            self.show_welcome_once()
            return
        self._welcome_scheduled = True
        self.show_setup()

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
        history_only = (
            self._config.history_only_output()
            if callable(getattr(self._config, "history_only_output", None))
            else self._config.get("output.mode", "paste") == "history"
        )
        result_tip = (
            "结果仅保存到「历史」，不会输入其他应用。" if history_only and self._config.get("history.enabled", True) else
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

    def _dismiss_idle_floating(self) -> None:
        """Dismiss passive startup toasts on FloatingWindow before opening an
        ApplicationModal dialog so the floating window never sits behind a modal
        ignoring mouse hover/clicks."""
        floating = getattr(self.parent(), "_floating", None)
        if floating is None:
            return
        is_visible = getattr(floating, "isVisible", None)
        if not (callable(is_visible) and is_visible() is True):
            return
        if any(
            getattr(floating, flag, False) is True
            for flag in (
                "_model_loading_active",
                "_listening_active",
                "_capture_active",
                "_meeting_active",
            )
        ):
            return
        dismiss = getattr(floating, "dismiss_if_idle", None)
        if callable(dismiss):
            dismiss()

    def show_welcome(self) -> None:
        self._dismiss_idle_floating()
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
        self._dismiss_idle_floating()
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

    # ── Missing-model setup ────────────────────────────

    def show_setup(self) -> None:
        """Download, microphone check, then one trial sentence. Trial text stays here."""
        if self._setup_dialog is not None:
            self._setup_dialog.raise_()
            self._setup_dialog.activateWindow()
            return
        self._dismiss_idle_floating()
        self._setup_closed = False
        self._setup_reopened = False
        self._setup_open_engine = False
        dialog = _SetupDialog(self, parent=self._dialog_parent())
        self._setup_dialog = dialog
        self._setup_watch = QTimer(dialog)
        self._setup_watch.setInterval(WELCOME_DELAY_MS)
        self._setup_watch.timeout.connect(self._refresh_setup_steps)
        self._setup_watch.start()
        self._refresh_setup_steps()
        dialog.finished.connect(self._on_setup_finished)
        code = dialog.exec()
        if self._setup_reopened or self._setup_closed:
            return
        if code == int(QDialog.DialogCode.Rejected) and not self._model_installed():
            self._defer_setup()
            return
        self._finish_setup()

    def _recognizer_ready(self) -> bool:
        speech = getattr(self.parent(), "_recognizer", None)
        return bool(getattr(speech, "is_ready", False) or getattr(speech, "_is_ready", False))

    def _trial_ready(self) -> bool:
        return bool(self._model_installed() and self._recognizer_ready())

    def note_mic_level(self, level: float) -> None:
        dialog = self._setup_dialog
        if dialog is None:
            return
        self._mic_heard = float(level) >= _MIC_HEARD_THRESHOLD
        if self._mic_heard and self._trial_ready():
            dialog.mic_status.setText("已听到声音。可以试说一句。")
        elif self._mic_heard:
            dialog.mic_status.setText("已听到声音。模型载入完成后可以试说一句。")
        else:
            dialog.mic_status.setText("没有听到声音。可以重试，或稍后继续。")
        dialog.mic_btn.setEnabled(True)
        dialog.close_btn.setEnabled(True)
        dialog.trial_btn.setEnabled(bool(self._trial_ready() and not self._setup_mode))

    def submit_trial(self, text: str) -> None:
        self._resume_global_hotkey()
        dialog = self._setup_dialog
        if dialog is None:
            return
        dialog.trial_btn.setText("试说一句")
        dialog.trial_btn.setEnabled(bool(self._trial_ready() and not self._setup_mode))
        cleaned = (text or "").strip()
        dialog.trial_result.setText(cleaned or "没有识别到文字。可以再试说一句。")

    def _on_setup_finished(self, code: int) -> None:
        if self._setup_open_engine:
            self._setup_open_engine = False
            self._setup_reopened = True
            self._show_main_window("engine")
            QTimer.singleShot(0, self._reopen_setup_nonmodal)
            return
        if code == int(QDialog.DialogCode.Rejected) and not self._model_installed():
            self._defer_setup()
            return
        self._finish_setup()

    def _defer_setup(self) -> None:
        """「稍后继续」leaves the page for the next launch, until a model exists."""
        self._setup_closed = True
        self._stop_setup_audio()
        if self._setup_watch is not None:
            self._setup_watch.stop()
            self._setup_watch = None
        dialog = self._setup_dialog
        self._setup_dialog = None
        if dialog is not None:
            dialog.hide()
            dialog.deleteLater()
        self._welcome_scheduled = False

    def _reopen_setup_nonmodal(self) -> None:
        dialog = self._setup_dialog
        if dialog is None:
            return
        anchor = self._dialog_parent()
        if anchor is not None and dialog.parentWidget() is not anchor:
            dialog.setParent(anchor, Qt.WindowType.Dialog)
        dialog.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, False)
        dialog.setWindowModality(Qt.WindowModality.NonModal)
        dialog.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        dialog.apply_theme_styles()
        dialog.show()

    def _open_engine_from_setup(self) -> None:
        if self._setup_dialog is None:
            return
        self._setup_open_engine = True
        self._setup_dialog.accept()

    def _refresh_setup_steps(self) -> None:
        dialog = self._setup_dialog
        if dialog is None:
            return
        installed = self._model_installed()
        ready = self._recognizer_ready()
        dialog.finish_btn.setEnabled(bool(installed))
        if self._download_worker is not None:
            dialog.cancel_download_btn.setVisible(True)
            dialog.cancel_download_btn.setEnabled(True)
        else:
            dialog.cancel_download_btn.setVisible(False)
            dialog.cancel_download_btn.setEnabled(False)
            if installed and ready:
                dialog.download_btn.setText("模型已就绪")
                dialog.download_btn.setEnabled(False)
                if not dialog.download_status.text() or "正在载入" in dialog.download_status.text():
                    dialog.download_status.setText("语音模型已就绪，可以试说一句或直接完成。")
                    dialog.download_status.setVisible(True)
            elif installed:
                dialog.download_btn.setText("模型正在载入…")
                dialog.download_btn.setEnabled(False)
        if self._setup_mode:
            return
        dialog.mic_btn.setEnabled(True)
        trial_busy = self._trial_worker is not None and self._trial_worker.isRunning()
        dialog.trial_btn.setText("正在识别…" if trial_busy else "试说一句")
        dialog.trial_btn.setEnabled(bool(self._trial_ready() and not trial_busy))

    def _finish_setup(self) -> None:
        if self._setup_closed:
            return
        self._setup_closed = True
        self._stop_setup_audio()
        if self._setup_watch is not None:
            self._setup_watch.stop()
            self._setup_watch = None
        dialog = self._setup_dialog
        self._setup_dialog = None
        if dialog is not None:
            dialog.hide()
            dialog.deleteLater()
        if not self._config.get("first_run_welcome_seen", False):
            self._welcome_scheduled = False
            QTimer.singleShot(WELCOME_DELAY_MS, self.show_welcome)
        elif not self._config.get("history.onboarded", False):
            QTimer.singleShot(HISTORY_AFTER_WELCOME_MS, self.show_history_onboarding)

    def _stop_countdown(self) -> None:
        if self._countdown_timer is not None:
            self._countdown_timer.stop()
            self._countdown_timer = None

    def _tick_trial_countdown(self) -> None:
        dialog = self._setup_dialog
        if dialog is None or self._setup_mode != "trial":
            self._stop_countdown()
            return
        self._trial_remaining_sec = max(0, self._trial_remaining_sec - 1)
        if self._trial_remaining_sec > 0:
            dialog.trial_result.setText(
                f"正在听（剩余 {self._trial_remaining_sec} 秒）…请说一句短话，说完可点「提前完成」。"
            )

    def _on_trial_btn_clicked(self) -> None:
        if self._setup_mode == "trial":
            self._finish_setup_trial()
            return
        self._start_setup_trial()

    def _start_setup_mic_test(self) -> None:
        dialog = self._setup_dialog
        if dialog is None or self._setup_mode:
            return
        dialog.mic_status.setText("正在准备麦克风…")
        dialog.mic_btn.setEnabled(False)
        if not self._begin_setup_capture("probe"):
            return
        gen = self._capture_gen
        dialog.mic_status.setText("正在听…请对着麦克风说一句话。")
        QTimer.singleShot(_SETUP_PROBE_MS, lambda g=gen: self._finish_setup_mic_test(g))

    def _start_setup_trial(self) -> None:
        dialog = self._setup_dialog
        if dialog is None or self._setup_mode:
            return
        if not self._model_installed():
            dialog.trial_result.setText("请先下载模型，再试说一句。")
            return
        if not self._recognizer_ready():
            dialog.trial_result.setText("模型正在载入内存，请稍候…")
            return
        dialog.trial_result.setText("正在准备录音…")
        dialog.trial_btn.setEnabled(False)
        if not self._begin_setup_capture("trial"):
            return
        gen = self._capture_gen
        self._trial_remaining_sec = max(1, _SETUP_TRIAL_MS // 1000)
        dialog.trial_result.setText(
            f"正在听（剩余 {self._trial_remaining_sec} 秒）…请说一句短话，说完可点「提前完成」。"
        )
        dialog.trial_btn.setText("提前完成")
        dialog.trial_btn.setEnabled(True)
        self._stop_countdown()
        self._countdown_timer = QTimer(dialog)
        self._countdown_timer.setInterval(1000)
        self._countdown_timer.timeout.connect(self._tick_trial_countdown)
        self._countdown_timer.start()
        QTimer.singleShot(_SETUP_TRIAL_MS, lambda g=gen: self._finish_setup_trial(g))

    def _begin_setup_capture(self, mode: str) -> bool:
        if self._setup_mode:
            return False
        try:
            recorder = self._configure_setup_recorder()
        except Exception:
            log.debug("准备页无法配置录音", exc_info=True)
            self._on_setup_audio_error("无法准备麦克风")
            return False
        self._capture_gen += 1
        self._setup_mode = mode
        self._probe_peak = 0.0
        if mode == "trial":
            self._trial_audio = None
        self._pause_global_hotkey()
        recorder.start()
        if self._setup_mode != mode or not recorder.is_recording:
            self._resume_global_hotkey()
            return False
        return True

    def _configure_setup_recorder(self):
        from sayink.audio_devices import sanitize_system_device_index

        recorder = self._ensure_setup_recorder()
        source = self._config.get("audio.input_source", "microphone") or "microphone"
        mic = _device_index(self._config.get("audio.mic_device_index", -1))
        system = sanitize_system_device_index(_device_index(self._config.get("audio.system_device_index", -1)))
        recorder.configure(
            input_source=source,
            mic_device_index=mic,
            system_device_index=system,
        )
        return recorder

    def _ensure_setup_recorder(self):
        if self._setup_recorder_obj is None:
            from sayink.audio_recorder import AudioRecorder

            recorder = AudioRecorder(self)
            recorder.volume_changed.connect(self._on_setup_volume)
            recorder.error.connect(self._on_setup_audio_error)
            recorder.segment_ready.connect(self._on_setup_segment)
            self._setup_recorder_obj = recorder
        return self._setup_recorder_obj

    def _on_setup_volume(self, volume: float) -> None:
        if self._setup_mode == "probe":
            self._probe_peak = max(self._probe_peak, float(volume))

    def _on_setup_segment(self, audio) -> None:
        if self._setup_mode not in ("trial", "closing"):
            return
        if audio is None or getattr(audio, "size", 0) <= 0:
            return
        import numpy as np

        # The recorder can reuse its buffer, and a pause splits one sentence
        # into several segments. Keep a private copy of every piece.
        piece = np.array(audio, dtype=np.float32, copy=True)
        current = self._trial_audio
        if current is None or getattr(current, "size", 0) == 0:
            self._trial_audio = piece
        else:
            self._trial_audio = np.concatenate((current, piece))

    def _on_setup_audio_error(self, msg: str) -> None:
        mode = self._setup_mode
        self._capture_gen += 1
        self._setup_mode = ""
        self._stop_countdown()
        self._stop_setup_recorder(flush=False)
        self._resume_global_hotkey()
        dialog = self._setup_dialog
        if dialog is None:
            return
        dialog.mic_btn.setEnabled(True)
        dialog.close_btn.setEnabled(True)
        detail = f"{msg} " if msg else ""
        if mode == "trial":
            dialog.trial_btn.setText("试说一句")
            dialog.trial_btn.setEnabled(bool(self._trial_ready()))
            dialog.trial_result.setText(f"没有听清。{detail}可以重试，或稍后继续。".strip())
            return
        dialog.mic_status.setText(f"没有听到声音。{detail}可以重试，或稍后继续。".strip())

    def _finish_setup_mic_test(self, gen: int | None = None) -> None:
        if gen is not None and gen != self._capture_gen:
            return
        if self._setup_mode != "probe":
            return
        self._capture_gen += 1
        peak = self._probe_peak
        self._setup_mode = ""
        self._stop_setup_recorder(flush=False)
        self._resume_global_hotkey()
        self.note_mic_level(peak)

    def _finish_setup_trial(self, gen: int | None = None) -> None:
        if gen is not None and gen != self._capture_gen:
            return
        if self._setup_mode != "trial":
            return
        self._capture_gen += 1
        self._stop_countdown()
        self._setup_mode = "closing"
        self._stop_setup_recorder(flush=True)
        audio = self._trial_audio
        self._trial_audio = None
        self._setup_mode = ""
        dialog = self._setup_dialog
        if dialog is not None:
            dialog.trial_btn.setText("试说一句")
            dialog.trial_btn.setEnabled(bool(self._trial_ready()))
        if audio is None or getattr(audio, "size", 0) == 0:
            self._resume_global_hotkey()
            self.submit_trial("")
            return
        self._transcribe_setup_trial(audio)

    def _on_trial_worker_finished(self, worker) -> None:
        if self._trial_worker is worker:
            self._trial_worker = None
        self._resume_global_hotkey()
        self._refresh_setup_steps()

    def _transcribe_setup_trial(self, audio) -> None:
        dialog = self._setup_dialog
        if dialog is None:
            self._resume_global_hotkey()
            return
        speech = getattr(self.parent(), "_recognizer", None)
        engine = getattr(speech, "_recognizer", None)
        ready = bool(getattr(speech, "is_ready", False) or getattr(speech, "_is_ready", False))
        busy = tuple(getattr(speech, "running_workers", ()) or ())
        if engine is None or not ready:
            self._resume_global_hotkey()
            dialog.trial_result.setText("模型还没准备好。请先下载，并等到引擎可用后再试说一句。")
            return
        if busy:
            self._resume_global_hotkey()
            dialog.trial_result.setText("识别正忙。请稍后再试说一句。")
            return
        from sayink.speech_recognizer import TranscribeWorker

        worker = TranscribeWorker(engine, audio, retry_empty=True, parent=self)
        worker.native_lock = getattr(speech, "_decode_lock", None)
        worker.result_ready.connect(self.submit_trial)
        worker.error.connect(self._on_setup_trial_error)
        worker.finished.connect(lambda w=worker: self._on_trial_worker_finished(w))
        self._trial_worker = worker
        dialog.trial_btn.setText("正在识别…")
        dialog.trial_btn.setEnabled(False)
        dialog.trial_result.setText("正在识别试说语音…")
        worker.start()

    def _on_setup_trial_error(self, message: str) -> None:
        self._resume_global_hotkey()
        dialog = self._setup_dialog
        if dialog is None:
            return
        dialog.trial_btn.setText("试说一句")
        dialog.trial_btn.setEnabled(bool(self._trial_ready() and not self._setup_mode))
        dialog.trial_result.setText(f"试说没有完成。{message} 可以再试一次，或稍后继续。".strip())

    def _reap_download_worker(self, worker: object) -> None:
        self._download_threads = [
            t
            for t in self._download_threads
            if t is not worker and getattr(t, "isRunning", lambda: False)()
        ]
        if isinstance(worker, QThread):
            worker.deleteLater()

    def _start_inline_model_download(self) -> None:
        dialog = self._setup_dialog
        if dialog is None or self._download_worker is not None:
            return
        from sayink.speech_recognizer import DEFAULT_MODEL_ID, ModelDownloadWorker

        model_id = self._config.get("stt.model_id", "") or DEFAULT_MODEL_ID
        worker = ModelDownloadWorker(
            model_id,
            source=self._config.get("stt.download_source", "auto"),
        )
        self._download_worker = worker
        if worker not in self._download_threads:
            self._download_threads.append(worker)
        dialog.download_btn.setEnabled(False)
        dialog.download_btn.setText("正在下载… 0%")
        dialog.cancel_download_btn.setVisible(True)
        dialog.cancel_download_btn.setEnabled(True)
        dialog.download_status.setText("正在后台下载模型，您可以先测试下方麦克风。")
        dialog.download_status.setVisible(True)
        worker.progress.connect(
            lambda pct, w=worker: self._on_setup_download_progress(pct, worker=w)
        )
        worker.finished_ok.connect(
            lambda mid, w=worker: self._on_setup_download_done(mid, worker=w)
        )
        worker.error.connect(
            lambda msg, w=worker: self._on_setup_download_error(msg, worker=w)
        )
        finished_sig = getattr(worker, "finished", None)
        if finished_sig is not None and hasattr(finished_sig, "connect"):
            finished_sig.connect(lambda w=worker: self._reap_download_worker(w))
        worker.start()

    def _cancel_inline_model_download(self) -> None:
        worker = self._download_worker
        self._download_worker = None
        if worker is not None and hasattr(worker, "cancel"):
            worker.cancel()
        dialog = self._setup_dialog
        if dialog is None:
            return
        dialog.cancel_download_btn.setVisible(False)
        dialog.cancel_download_btn.setEnabled(False)
        dialog.download_btn.setEnabled(True)
        dialog.download_btn.setText("下载模型")
        dialog.download_status.setText("已取消模型下载。需要时可随时重新下载。")
        dialog.download_status.setVisible(True)

    def _is_stale_download_worker(self, worker: object | None) -> bool:
        return worker is not None and (
            worker is not self._download_worker
            or getattr(worker, "_cancelled", False) is True
        )

    def _on_setup_download_progress(
        self, pct: int, *, worker: object | None = None
    ) -> None:
        if self._is_stale_download_worker(worker):
            return
        dialog = self._setup_dialog
        if dialog is None or self._download_worker is None:
            return
        dialog.download_btn.setText(f"正在下载… {int(pct)}%")

    def _on_setup_download_done(
        self, model_id: str, *, worker: object | None = None
    ) -> None:
        if self._is_stale_download_worker(worker):
            return
        self._download_worker = None
        self._config.set("stt.model_id", model_id)
        dialog = self._setup_dialog
        if dialog is not None:
            dialog.cancel_download_btn.setVisible(False)
            dialog.cancel_download_btn.setEnabled(False)
            dialog.download_btn.setText("模型已下载，正在载入…")
            dialog.download_btn.setEnabled(False)
            dialog.download_status.setText("模型下载完成，正在载入内存…")
            dialog.download_status.setVisible(True)
        parent = self.parent()
        if parent is not None and hasattr(parent, "_on_models_changed"):
            parent._on_models_changed()
        self._refresh_setup_steps()

    def _on_setup_download_error(
        self, message: str, *, worker: object | None = None
    ) -> None:
        if self._is_stale_download_worker(worker):
            return
        self._download_worker = None
        dialog = self._setup_dialog
        if dialog is None:
            return
        dialog.cancel_download_btn.setVisible(False)
        dialog.cancel_download_btn.setEnabled(False)
        dialog.download_btn.setEnabled(True)
        dialog.download_btn.setText("下载模型")
        dialog.download_status.setText(f"模型下载没有完成。{message} 可以重试，或稍后继续。".strip())
        dialog.download_status.setVisible(True)

    def _stop_setup_audio(self) -> None:
        self._capture_gen += 1
        self._setup_mode = ""
        self._stop_countdown()
        download = self._download_worker
        if download is not None and hasattr(download, "cancel"):
            download.cancel()
        self._download_worker = None
        worker = self._trial_worker
        if worker is not None and worker.isRunning():
            worker.cancel()
        self._stop_setup_recorder(flush=False)
        self._resume_global_hotkey()

    def _stop_setup_recorder(self, *, flush: bool) -> None:
        recorder = self._setup_recorder_obj
        if recorder is None or not recorder.is_recording:
            return
        if flush:
            recorder.stop()
        else:
            recorder.cancel()


def _device_index(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


class _SetupDialog(QDialog):
    """First-run page when no speech model is installed. No AI fields."""

    def __init__(self, controller: OnboardingController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._controller = controller
        self.setObjectName("setupDialog")
        self.setWindowTitle("准备 SayInk")
        self.setModal(True)
        if parent is None:
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setMinimumWidth(440)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        intro = QLabel(
            "本机还没有语音模型。完成模型下载后即可开始语音输入；"
            "也可以在这里测试麦克风并试说一句（试说文字只留在本页）。"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        model_id = controller._config.get("stt.model_id", "") or DEFAULT_MODEL_ID
        info = get_model_info(model_id) or get_model_info(DEFAULT_MODEL_ID) or {}
        size_mb = info.get("size_mb", 950)
        self.download_step = QLabel(f"1. 下载语音模型（约 {size_mb} MB，仅需一次）")
        layout.addWidget(self.download_step)

        dl_row = QHBoxLayout()
        dl_row.setSpacing(8)
        self.download_btn = QPushButton("下载模型")
        self.download_btn.setAccessibleName("下载模型")
        self.download_btn.setDefault(True)
        self.download_btn.setAutoDefault(True)
        self.download_btn.clicked.connect(controller._start_inline_model_download)
        dl_row.addWidget(self.download_btn, 1)

        self.cancel_download_btn = QPushButton("取消下载")
        self.cancel_download_btn.setAccessibleName("取消下载")
        self.cancel_download_btn.setVisible(False)
        self.cancel_download_btn.setEnabled(False)
        self.cancel_download_btn.clicked.connect(controller._cancel_inline_model_download)
        dl_row.addWidget(self.cancel_download_btn)
        layout.addLayout(dl_row)

        self.download_status = QLabel("")
        self.download_status.setObjectName("setupDownloadStatus")
        self.download_status.setAccessibleName("模型下载状态")
        self.download_status.setWordWrap(True)
        self.download_status.setVisible(False)
        layout.addWidget(self.download_status)

        self.engine_btn = QPushButton("在引擎页查看其他模型")
        self.engine_btn.setAccessibleName("在引擎页查看其他模型")
        self.engine_btn.clicked.connect(controller._open_engine_from_setup)
        layout.addWidget(self.engine_btn)

        mic_step = QLabel("2. 测试麦克风（可选）")
        layout.addWidget(mic_step)
        self.mic_btn = QPushButton("测试麦克风")
        self.mic_btn.setAccessibleName("测试麦克风")
        self.mic_btn.setEnabled(True)
        self.mic_btn.clicked.connect(controller._start_setup_mic_test)
        layout.addWidget(self.mic_btn)
        self.mic_status = QLabel("可以先测试麦克风，不必等模型下载完。")
        self.mic_status.setObjectName("setupMicStatus")
        self.mic_status.setAccessibleName("麦克风测试状态")
        self.mic_status.setWordWrap(True)
        layout.addWidget(self.mic_status)

        trial_step = QLabel("3. 试说一句（可选）")
        layout.addWidget(trial_step)
        self.trial_btn = QPushButton("试说一句")
        self.trial_btn.setAccessibleName("试说一句")
        self.trial_btn.setEnabled(False)
        self.trial_btn.clicked.connect(controller._on_trial_btn_clicked)
        layout.addWidget(self.trial_btn)
        self.trial_result = QLabel("试说结果会留在这里。")
        self.trial_result.setObjectName("setupTrial")
        self.trial_result.setAccessibleName("试说结果")
        self.trial_result.setWordWrap(True)
        self.trial_result.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.trial_result)

        hotkey_label = format_hotkey(controller._config.get("hotkey", DEFAULT_HOTKEY))
        self.usage_hint = QLabel(
            f"使用提示：先把光标放到要输入文字的位置，按住 {hotkey_label} 说话、松开上屏；"
            "双击托盘图标可打开主窗口。"
        )
        self.usage_hint.setObjectName("setupUsageHint")
        self.usage_hint.setWordWrap(True)
        layout.addWidget(self.usage_hint)

        layout.addStretch(1)
        footer_row = QHBoxLayout()
        footer_row.setSpacing(8)
        self.close_btn = QPushButton("稍后继续")
        self.close_btn.setAccessibleName("稍后继续")
        self.close_btn.clicked.connect(self.reject)
        footer_row.addWidget(self.close_btn)

        self.finish_btn = QPushButton("完成并开始使用")
        self.finish_btn.setAccessibleName("完成并开始使用")
        self.finish_btn.setEnabled(bool(controller._model_installed()))
        self.finish_btn.clicked.connect(self.accept)
        footer_row.addWidget(self.finish_btn, 1)
        layout.addLayout(footer_row)

        self.apply_theme_styles()
        self.download_btn.setFocus()

    def apply_theme_styles(self) -> None:
        if getattr(self, "_applying_theme", False):
            return
        self._applying_theme = True
        try:
            from sayink.ui import design_tokens as tok
            from sayink.ui import settings_styles as ss

            dialog_css = (
                ss.WINDOW_CSS
                + f"""
                QDialog#setupDialog {{ background: {tok.SURFACE}; color: {tok.TEXT}; }}
                QLabel {{ background: transparent; color: {tok.TEXT}; font-size: {tok.TYPE_BODY_SM}px; }}
                QLabel#setupDownloadStatus, QLabel#setupMicStatus, QLabel#setupTrial, QLabel#setupUsageHint {{
                    color: {tok.TEXT_DIM};
                }}
                """
            )
            if self.styleSheet() != dialog_css:
                self.setStyleSheet(dialog_css)
            self.download_btn.setStyleSheet(ss.BTN_PRIMARY)
            self.cancel_download_btn.setStyleSheet(ss.BTN_GHOST)
            self.engine_btn.setStyleSheet(ss.BTN_GHOST)
            self.mic_btn.setStyleSheet(ss.BTN_GHOST)
            self.trial_btn.setStyleSheet(ss.BTN_GHOST)
            self.close_btn.setStyleSheet(ss.BTN_GHOST)
            self.finish_btn.setStyleSheet(ss.BTN_PRIMARY)
        finally:
            self._applying_theme = False

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if getattr(self, "_applying_theme", False):
            return
        if event.type() in (
            QEvent.Type.PaletteChange,
            QEvent.Type.ApplicationPaletteChange,
        ):
            self.apply_theme_styles()

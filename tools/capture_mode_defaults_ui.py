"""Offscreen shots for the dictation-mode default states.

No microphone, speech model, or network is opened. Python 3.10.
"""
from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PyQt6.QtCore import QCoreApplication, QEvent, QPoint
from PyQt6.QtGui import QFontDatabase
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QDialog, QLabel, QLineEdit, QMessageBox, QPushButton

from sayink.config import Config
from sayink.history_store import HistoryStore, SegmentRecord
from sayink.meeting_session import MeetingController, MeetingOptions, resolve_capabilities
from sayink.onboarding import OnboardingController
from sayink.ui.floating_window import FloatingWindow
from sayink.ui.main_window import MainWindow
from sayink.ui.theme import apply_theme

EVIDENCE = ROOT / "openspec/changes/dictation-mode-defaults/visual-evidence.md"
OUT = ROOT / "openspec/changes/dictation-mode-defaults/visual"
REQUIRED = (
    "v1-setup-pending.png",
    "v2-mic-unheard.png",
    "v3-listening-target.png",
    "v4-history-conflict.png",
    "v5-live-edit.png",
    "v5-live-edit-narrow.png",
    "v6-ended-without-notes.png",
)
STORED_LINE = "已经落下的这句话可以修改。"


def settle(app: QApplication) -> None:
    for _ in range(8):
        app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def save_grab(widget, name: str) -> None:
    settle(QApplication.instance())
    path = OUT / name
    if not widget.grab().save(str(path)):
        raise SystemExit(f"could not write {path}")


def buttons(widget) -> list[QPushButton]:
    return widget.findChildren(QPushButton)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def capture_setup(config: Config) -> None:
    controller = OnboardingController(
        config,
        parent=None,
        dialog_parent=lambda: None,
        is_continuous_mode=lambda: False,
        show_main_window=lambda _page: None,
        model_installed=lambda: False,
    )
    config.set("first_run_welcome_seen", False)
    config.set("history.onboarded", True)

    def exec_(dialog: QDialog):
        dialog.show()
        dialog.adjustSize()
        settle(QApplication.instance())
        labels = " ".join(label.text() for label in dialog.findChildren(QLabel))
        names = [button.text() for button in buttons(dialog)]
        require("下载模型" in names and "测试麦克风" in names and "试说一句" in names, "setup is missing a step")
        require(dialog.findChildren(QLineEdit) == [], "setup page has a text field")
        require("密钥" not in labels and "润色" not in labels and "API" not in labels, "setup page asks for an AI service")
        download = next(button for button in buttons(dialog) if button.text() == "下载模型")
        require(download.isDefault() and not next(button for button in buttons(dialog) if button.text() == "测试麦克风").isEnabled(),
                "setup is not waiting on the model download")
        save_grab(dialog, "v1-setup-pending.png")
        controller.note_mic_level(0)
        settle(QApplication.instance())
        status = dialog.findChild(QLabel, "setupMicStatus").text()
        require("没有听到" in status, "silent microphone is not explained")
        require(next(button for button in buttons(dialog) if button.text() == "测试麦克风").isEnabled(), "microphone retry is disabled")
        require(next(button for button in buttons(dialog) if button.text() == "稍后继续").isEnabled(), "setup cannot be closed")
        save_grab(dialog, "v2-mic-unheard.png")
        return QDialog.DialogCode.Rejected

    original = QDialog.exec
    QDialog.exec = exec_
    try:
        controller.show_setup()
    finally:
        QDialog.exec = original


def capture_listening_target() -> None:
    bar = FloatingWindow()
    bar.show_listening("notepad.exe")
    settle(QApplication.instance())
    text = bar._status_label.text()
    require("notepad.exe" in text, "listening bar does not name the process")
    require("未命名" not in text and "记事本" not in text, "listening bar shows a window title")
    require(not bar.windowTitle(), "listening bar has a window title")
    save_grab(bar, "v3-listening-target.png")
    bar.hide()


def capture_history_conflict() -> None:
    box = QMessageBox()
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle("结果没有去处")
    box.setText("历史已关闭，当前结果只会进历史，所以不会开始录音。")
    box.setInformativeText("可以开启历史，或只把当前模式改为输入光标。")
    enable = box.addButton("开启历史", QMessageBox.ButtonRole.AcceptRole)
    box.addButton("改为输入光标", QMessageBox.ButtonRole.ActionRole)
    cancel = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
    enable.setAccessibleName("开启历史")
    cancel.setAccessibleName("取消")
    box.setDefaultButton(enable)
    box.show()
    enable.setFocus()
    settle(QApplication.instance())
    labels = {button.text() for button in buttons(box)}
    require(labels == {"开启历史", "改为输入光标", "取消"}, "conflict choices changed")
    require(box.defaultButton() is enable, "enable-history is not the default")
    save_grab(box, "v4-history-conflict.png")
    box.hide()


def reveal_edit(page) -> None:
    paragraph = page._live_transcript.document().find(STORED_LINE)
    require(not paragraph.isNull(), "stored live sentence is missing")
    point = page._live_transcript.cursorRect(paragraph).center()
    QTest.mouseMove(page._live_transcript.viewport(), point if point != QPoint() else QPoint(24, 24))
    page._live_transcript._activate_actions(paragraph.block())
    settle(QApplication.instance())
    visible = page._live_transcript.toPlainText()
    require(STORED_LINE in visible and "编辑" in visible, "live edit affordance is missing")


def capture_meeting(config: Config, history: HistoryStore) -> None:
    controller = MeetingController(config, history, pending_segments=lambda: 0)
    win = MainWindow(config, history, meeting=controller)
    win.show_page("meeting")
    win.show()
    win.setMinimumWidth(880)
    win.resize(1040, 720)
    page = win._meeting
    apply_theme(QApplication.instance(), mode="light", surfaces=[win, win._settings, win._history, page])
    options = MeetingOptions(title="会议录制（示例）", input_source="mixed")
    caps = resolve_capabilities(config, options, dictation_model_id="funasr-nano")
    live = controller.begin(options, replace(caps, speaker_model_available=False))
    history.enqueue(SegmentRecord(
        live.id, 3, live.started_at, raw_text=STORED_LINE, trigger_mode="meeting",
        speaker_id=1, captured_at=live.started_at,
    ))
    history.flush()
    page._render_live()
    settle(QApplication.instance())
    reveal_edit(page)
    save_grab(page, "v5-live-edit.png")
    win.resize(880, 580)
    settle(QApplication.instance())
    reveal_edit(page)
    require(page.width() < 1000, "narrow meeting capture is still the wide layout")
    save_grab(page, "v5-live-edit-narrow.png")
    win.resize(1040, 720)
    controller.finalize()
    page._switch_tab(1)
    page._status_label.hide()
    settle(QApplication.instance())
    hint = page._content_hint.text()
    require("生成完成" not in hint and "编辑" in hint, "ended meeting still says editing waits for notes")
    require(controller.store.get_notes(live.id) is None or not controller.store.get_notes(live.id).markdown,
            "ended sample unexpectedly has notes")
    save_grab(page, "v6-ended-without-notes.png")
    win.hide()
    controller.shutdown()


def write_evidence() -> None:
    rows = (
        ("V1", "v1-setup-pending.png", "缺模型准备页，待下载", "看见下载模型、测试麦克风、试说一句；没有输入框，也没有密钥或润色配置。"),
        ("V2", "v2-mic-unheard.png", "准备页，麦克风未听到", "说明没有听到声音，测试麦克风和稍后继续都可以使用。"),
        ("V3", "v3-listening-target.png", "持续转写浮条，目标进程名", "状态是 notepad.exe，没有窗口标题。"),
        ("V4", "v4-history-conflict.png", "开录前选择", "开启历史、改为输入光标、取消都在，默认是开启历史。"),
        ("V5", "v5-live-edit.png", "会议进行中，悬停已落段，默认宽度", "已落段文字旁出现编辑，原句仍在。"),
        ("V5 窄", "v5-live-edit-narrow.png", "同一场会议，窄宽度", "窄宽度下编辑入口还在，正文没有被拆掉。"),
        ("V6", "v6-ended-without-notes.png", "已结束且没有纪要", "提示可以编辑，不再要求先生成纪要。"),
    )
    lines = [
        "# 视觉证据：分模式默认与会中改字",
        "",
        "Qt offscreen 截图，没有打开麦克风、语音模型或网络。对照规格文字，这些状态没有旧像素基线。",
        "",
        "| ID | 图片 | 状态 | 对照结论 |",
        "|---|---|---|---|",
    ]
    for label, filename, state, conclusion in rows:
        lines.append(f"| {label} | `openspec/changes/dictation-mode-defaults/visual/{filename}` | {state} | {conclusion} |")
    lines.append("")
    EVIDENCE.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    for font in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf"):
        path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / font
        if path.exists():
            QFontDatabase.addApplicationFont(str(path))
    apply_theme(app, mode="light")
    with tempfile.TemporaryDirectory(prefix="mode-defaults-") as temp, \
         patch("sayink.config.default_secret_store", return_value=None), \
         patch("sayink.config.Config._sync_registry_auto_start", return_value=None), \
         patch("winreg.QueryValueEx", side_effect=FileNotFoundError), \
         patch("sayink.ui.settings_window.list_microphone_devices", return_value=[]), \
         patch("sayink.ui.settings_window.list_system_capture_devices_for_settings", return_value=[]), \
         patch("sayink.ui.meeting_page.missing_aux_models", return_value=[]), \
         patch("sayink.speech_recognizer.is_model_downloaded", return_value=False):
        config = Config(config_dir=Path(temp) / "config")
        capture_setup(config)
        capture_listening_target()
        capture_history_conflict()
        history = HistoryStore(db_path=Path(temp) / "history.db")
        try:
            capture_meeting(config, history)
        finally:
            history.close(timeout=2)
    missing = [name for name in REQUIRED if not (OUT / name).is_file() or (OUT / name).stat().st_size < 2000]
    if missing:
        raise SystemExit("missing screenshots: " + ", ".join(missing))
    write_evidence()
    print(OUT.resolve())


if __name__ == "__main__":
    main()

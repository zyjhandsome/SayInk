"""Render native UI with isolated, explicitly fictional preview data.

Run: py -3.10 tools/capture_ui.py --output build/ux-preview
No microphone, model loading, network calls or personal history are used.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QFontDatabase
from PyQt6.QtCore import QCoreApplication, QEvent
from sayink.config import Config
from sayink.history_store import SegmentRecord, SessionSummary
from sayink.ui.main_window import MainWindow, PAGE_KEYS
from sayink.ui.floating_window import FloatingWindow
from sayink.ui.theme import apply_theme
from sayink.speech_recognizer import DEFAULT_MODEL_ID


class PreviewStore:
    """Fictional records used exclusively by this visual QA script."""
    def __init__(self):
        self.sessions = []
        self.segments = {}
        examples = [
            ("今天的讨论先整理成三个要点，下午再补充具体的执行计划。", "会议记录（示例）", "microphone"),
            ("把想法说出来，再慢慢整理成文字。", "写作草稿（示例）", "microphone"),
            ("下周三之前完成第一轮评审，并确认参与人员。", "待办整理（示例）", "mixed"),
            ("请保留原始记录，方便之后核对细节。", "阅读笔记（示例）", "system"),
        ]
        for i, (body, target, source) in enumerate(examples):
            stamp = int((datetime.now() - timedelta(hours=i * 9)).timestamp() * 1000)
            sid = f"preview-{i}"
            self.sessions.append(SessionSummary(sid, stamp, 1, source, target, body))
            self.segments[sid] = [SegmentRecord(sid, 0, stamp, body, "", source, 8000, target, "continuous", "Fun-ASR-Nano")]

    def list_sessions(self, limit=50, offset=0):
        return self.sessions[offset:offset + limit]

    def search_sessions(self, query):
        return [s for s in self.sessions if query in s.preview]

    def get_session_segments(self, sid):
        return self.segments.get(sid, [])


def settle(app):
    app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("build/ux-preview"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    # Qt's offscreen Windows plugin does not enumerate installed fonts.
    for font in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf", "seguisb.ttf", "consola.ttf"):
        path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / font
        if path.exists():
            QFontDatabase.addApplicationFont(str(path))
    with tempfile.TemporaryDirectory(prefix="sayink-ui-") as temp, \
         patch("sayink.ui.settings_window.list_microphone_devices", return_value=[]), \
         patch("sayink.ui.settings_window.list_system_capture_devices_for_settings", return_value=[]), \
         patch("sayink.speech_recognizer.is_model_downloaded", side_effect=lambda mid: mid == DEFAULT_MODEL_ID):
        config = Config(config_dir=Path(temp))
        config.set("stt.models_dir", str(Path(temp) / "models"))
        config.set("history.enabled", True)
        config.set("auto_start", False)
        store = PreviewStore()
        win = MainWindow(config, store)
        from sayink.runtime_status import RuntimeState
        win._settings.set_runtime_status(RuntimeState.READY, "就绪（示例）")
        bar = FloatingWindow()
        for mode in ("light", "dark"):
            apply_theme(app, mode=mode, surfaces=[win, win._settings, win._history, bar])
            win.show()
            for key in PAGE_KEYS:
                win.show_page(key)
                for _ in range(4):
                    settle(app)
                win.grab().save(str(args.output / f"{key}-{mode}.png"))
            win.show_page("polish")
            win._settings._llm_enable_row.setChecked(True)
            for _ in range(4):
                settle(app)
            win.grab().save(str(args.output / f"polish-enabled-{mode}.png"))
            page = win._settings._pages.widget(2)
            page.verticalScrollBar().setValue(page.verticalScrollBar().maximum())
            settle(app)
            win.grab().save(str(args.output / f"polish-bottom-{mode}.png"))
            page.verticalScrollBar().setValue(0)
            win._settings._llm_enable_row.setChecked(False)
            win.show_page("general")
            page = win._settings._pages.widget(0)
            page.verticalScrollBar().setValue(page.verticalScrollBar().maximum())
            settle(app)
            win.grab().save(str(args.output / f"general-bottom-{mode}.png"))
            page.verticalScrollBar().setValue(0)
            win.show_page("history")
            win.resize(880, 580)
            settle(app)
            win.grab().save(str(args.output / f"history-narrow-{mode}.png"))
            win.resize(960, 640)
            win._history._search_edit.setText("没有这条示例")
            win._history._perform_search()
            settle(app)
            win.grab().save(str(args.output / f"history-no-results-{mode}.png"))
            win._history._search_edit.clear()
            win._history._perform_search()
            bar.show_listening()
            bar.update_partial_text("这是一条用于检查听写状态的示例文本。")
            settle(app)
            bar.grab().save(str(args.output / f"listen-{mode}.png"))
            bar.dismiss_if_idle()
        win.hide()
        config.save_immediate()
    print(args.output.resolve())


if __name__ == "__main__":
    main()

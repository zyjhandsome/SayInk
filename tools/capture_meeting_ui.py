"""Render meeting review with fictional records and an isolated database.

No audio capture, model loading or network requests are started.
Run with Python 3.10; set QT_SCALE_FACTOR=1.5 for the high-DPI preview.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PyQt6.QtCore import QCoreApplication, QEvent, QRect
from PyQt6.QtGui import QFontDatabase
from PyQt6.QtWidgets import QApplication

from sayink.config import Config
from sayink.history_store import HistoryStore, SegmentRecord
from sayink.meeting_session import MeetingController, MeetingOptions, resolve_capabilities
from sayink.ui.main_window import MainWindow
from sayink.ui.meeting_page import _SegmentSpeakerDialog, _SegmentTextDialog
from sayink.ui.theme import apply_theme

NOTES = """# 产品方案评审（示例）

## 摘要
本次评审确认优先优化会后核对流程：复制当前查看的内容，并让每条结论都能回到文字稿。具体上线时间仍需确认。

## 议题要点
### 内容核对与分享
- 复制按钮应明确区分纪要和文字稿，导出时可以选择内容范围。[00:00:13]
- 纪要引用应能跳转到对应发言，并支持返回原来的阅读位置。[00:00:27]

### 说话人纠错
- 人名用于标注发言，不能据此判断系统已验证真实身份。[00:00:54]
- 单段归属出错时，应只修改该段，随后核对纪要中的相关归属。[00:01:04]

## 决策
- 先完成复制、导出和核对路径，再安排上线评审。[00:00:27]

## 待办
本场未识别到

## 遗留问题
- 下一次评审时间尚未明确。[00:01:04]
"""


def settle(app):
    for _ in range(8):
        app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "build/meeting-ux-review/preview")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    for font in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf", "consola.ttf"):
        path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / font
        if path.exists():
            QFontDatabase.addApplicationFont(str(path))
    with tempfile.TemporaryDirectory(prefix="sample-", dir=args.output) as temp, \
         patch("sayink.config.default_secret_store", return_value=None), \
         patch("sayink.config.Config._sync_registry_auto_start", return_value=None), \
         patch("winreg.QueryValueEx", side_effect=FileNotFoundError), \
         patch("sayink.ui.settings_window.list_microphone_devices", return_value=[]), \
         patch("sayink.ui.settings_window.list_system_capture_devices_for_settings", return_value=[]), \
         patch("sayink.ui.meeting_page.missing_aux_models", return_value=[
             {"id": "speaker-campplus", "name": "说话人增强", "size_mb": 28},
             {"id": "silero-vad", "name": "语音检测", "size_mb": 2},
         ]), \
         patch("sayink.speech_recognizer.is_model_downloaded", return_value=False):
        config = Config(config_dir=Path(temp) / "config")
        config.set("llm.api_url", "http://localhost:11434/v1")
        config.set("llm.model_name", "local-model")
        config.set("stt.models_dir", str(Path(temp) / "models"))
        history = HistoryStore(db_path=Path(temp) / "history.db")
        controller = MeetingController(config, history, pending_segments=lambda: 0)
        for index, title in enumerate(("产品方案评审（示例）", "每周进展同步（示例）", "客户需求讨论（示例）")):
            stamp = int((datetime.now() - timedelta(hours=index * 6)).timestamp() * 1000)
            record = controller.store.create_meeting(title=title, started_at=stamp, source="mixed")
            controller.store.end_meeting(record.id, ended_at=stamp + 85_000)
            if index:
                continue
            current = record
            for offset, speaker, text in (
                (1, 1, "今天先讨论会后的核对流程，减少反复切换。"),
                (13, 1, "复制按钮应该区分纪要和文字稿，导出时让用户选择范围。"),
                (27, 2, "同意。点击引用以后，也要能返回刚才阅读的位置。"),
                (54, 2, "这里显示的名字是我们手动设置的，不代表系统验证了身份。"),
                (64, 1, "如果只是一句话分错人，就只更正这一句。下次评审时间再确认。"),
            ):
                history.enqueue(SegmentRecord(record.id, offset, stamp + offset * 1000, raw_text=text,
                                              trigger_mode="meeting", speaker_id=speaker, source="mixed",
                                              captured_at=stamp + offset * 1000))
            controller.store.set_speaker(record.id, 1, display_name="小林")
            controller.store.set_speaker(record.id, 2, display_name="小周")
        controller.store.flush()
        win = MainWindow(config, history, meeting=controller)
        from sayink.runtime_status import RuntimeState
        win._settings.set_runtime_status(RuntimeState.READY, "就绪（示例）")
        win.show_page("meeting")
        win.show()
        win.setMinimumWidth(880)
        win.resize(1040, 720)
        page = win._meeting

        def capture(name):
            settle(app)
            win.grab().save(str(args.output / name))

        for theme in ("dark", "light"):
            apply_theme(app, mode=theme, surfaces=[win, win._settings, win._history, page])
            page.show_start_card()
            capture(f"start-{theme}.png")
            options = MeetingOptions(title="会议录制（示例）", input_source="mixed")
            caps = resolve_capabilities(config, options, dictation_model_id="funasr-nano")
            live = controller.begin(options, replace(caps, speaker_model_available=False))
            history.enqueue(SegmentRecord(live.id, 0, live.started_at,
                raw_text="这里是本场会议的实时文字稿，可核对、复制，或暂停采集。", trigger_mode="meeting", speaker_id=1))
            history.flush()
            page._render_live()
            capture(f"live-{theme}.png")
            page.focus_search()
            page._live_find_edit.setText("核对")
            capture(f"live-find-{theme}.png")
            page._close_live_find()
            controller.pause("你已暂停采集；暂停期间不采集新音频，已录内容继续识别。")
            capture(f"paused-{theme}.png")
            class CompactScreen:
                def availableGeometry(self):
                    return QRect(0, 0, 910, 472)

            with patch.object(MainWindow, "screen", return_value=CompactScreen()):
                win._fit_to_available_screen()
                capture(f"small-paused-{theme}.png")
                assert win.height() <= 472
            controller.finalize()
            win._fit_to_available_screen()
            win.resize(1040, 720)
            page._search_edit.setText("没有匹配的示例会议")
            page._perform_search()
            capture(f"no-results-{theme}.png")
            page._search_edit.clear()
            page._perform_search()
            controller.store.save_notes(current.id, template_id="general", model="m", json_payload="", markdown=NOTES)
            controller.store.flush()
            page.show_meeting(current.id)
            page._switch_tab(0)
            apply_theme(app, mode=theme, surfaces=[win, win._settings, win._history, page])
            page._status_label.hide()
            capture(f"notes-{theme}.png")
            page._switch_tab(1)
            capture(f"transcript-{theme}.png")
            page._correct_speaker_btn.setChecked(True)
            capture(f"correction-{theme}.png")
            segment = controller.store.get_segments(current.id)[0]
            dialog = _SegmentSpeakerDialog(segment, [(1, "小林 · 说话人1"), (2, "小周 · 说话人2")], win)
            dialog.show()
            settle(app)
            dialog.grab().save(str(args.output / f"correction-dialog-{theme}.png"))
            dialog.close()
            page._correct_speaker_btn.setChecked(False)
            page._switch_tab(0)
            page._toggle_edit()
            capture(f"draft-{theme}.png")
            page._toggle_edit()
            page._status_label.hide()
            page._template_combo.setCurrentIndex(page._template_combo.findData("standup"))
            capture(f"template-{theme}.png")
            page._pending_templates.clear()
            page._render_detail()
            page._jump_to_transcript(27_000)
            capture(f"citation-{theme}.png")
            page._return_to_notes()
            win.resize(880, 580)
            capture(f"narrow-notes-{theme}.png")
            page._switch_tab(1)
            capture(f"narrow-transcript-{theme}.png")
            win.resize(1040, 720)
            controller.store.save_notes(
                current.id, template_id="refine", model="m", json_payload="",
                markdown=(
                    "今天先讨论会后的核对流程，减少反复切换。复制按钮应该区分纪要和文字稿，导出时让用户选择范围。"
                    "这一点，我也同意。点击引用以后，也要能返回刚才阅读的位置。\n\n"
                    "这里显示的名字是我们手动设置的，不代表系统验证了身份。\n\n"
                    "如果只是一句话分错人，就只更正这一句。下次评审时间再确认。"
                ),
            )
            controller.store.flush()
            page._pending_templates.clear()
            page.show_meeting(current.id)
            page._switch_tab(0)
            capture(f"refinement-{theme}.png")
            win.resize(880, 580)
            capture(f"narrow-refinement-{theme}.png")
            win.resize(1040, 720)
            controller.save_segment_text(current.id, 13, "复制按钮应区分纪要和文字稿；导出时可选择范围，周三开始验证。")
            page._switch_tab(1)
            page._render_detail()
            capture(f"edited-transcript-{theme}.png")
            win.resize(880, 580)
            capture(f"narrow-edited-transcript-{theme}.png")
            win.resize(1040, 720)
            segment = controller.store.get_segments(current.id)[1]
            dialog = _SegmentTextDialog(
                segment, position="00:13", speaker="小林", save_text=lambda _text: True, parent=win,
            )
            dialog.show()
            dialog.original_btn.setChecked(True)
            settle(app)
            dialog.grab().save(str(args.output / f"text-edit-dialog-{theme}.png"))
            dialog.resize(360, 440)
            settle(app)
            dialog.grab().save(str(args.output / f"narrow-text-edit-dialog-{theme}.png"))
            dialog.close()
            controller.save_segment_text(current.id, 13, None)
        win.hide()
        controller.shutdown()
        history.close(timeout=2)
        config.save_immediate()
    print(args.output.resolve())


if __name__ == "__main__":
    main()

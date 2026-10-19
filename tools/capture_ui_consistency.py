"""Compare native pages at matching sizes with isolated fictional data.

No user settings, credentials, microphone, models or online services are used.
Set QT_SCALE_FACTOR=1.5 or 2 to verify display scaling.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtCore import QPoint
from PyQt6.QtGui import QColor, QFontDatabase, QPalette
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QComboBox, QLabel, QLineEdit, QPushButton, QSpinBox, QStyle, QStyleOptionButton, QWidget

from sayink.config import Config
from sayink.history_store import HistoryStore, SegmentRecord
from sayink.meeting_session import MeetingController
from sayink.runtime_status import RuntimeState
from sayink.ui import design_tokens as tok
from sayink.ui.main_window import MainWindow, PAGE_KEYS
from sayink.ui.theme import apply_theme
from tools.capture_ui import PreviewStore, settle


def contrast(foreground: str, background: str) -> float:
    def luminance(value):
        color = QColor(value)
        components = (color.redF(), color.greenF(), color.blueF())
        linear = [c / 12.92 if c <= .04045 else ((c + .055) / 1.055) ** 2.4 for c in components]
        return sum(c * weight for c, weight in zip(linear, (.2126, .7152, .0722), strict=True))

    first, second = sorted((luminance(foreground), luminance(background)))
    return round((second + .05) / (first + .05), 2)


def bounds(widget, relative):
    point = widget.mapTo(relative, QPoint())
    return [point.x(), point.y(), widget.width(), widget.height()]


def font_snapshot(widget):
    font = widget.font()
    return dict(family=font.family(), pixels=font.pixelSize(), weight=font.weight(),
                color=widget.palette().color(QPalette.ColorRole.WindowText).name())


def check_controls(root):
    issues = []
    for label in root.findChildren(QLabel):
        if label.isVisible() and label.wordWrap() and label.text():
            needed = label.heightForWidth(label.width())
            if needed > label.height() + 2:
                issues.append(dict(kind="wrapped-label", text=label.text()[:80],
                                   height=label.height(), needed=needed))
    for button in root.findChildren(QPushButton):
        if not button.isVisible() or not button.text():
            continue
        option = QStyleOptionButton()
        button.initStyleOption(option)
        area = button.style().subElementRect(QStyle.SubElement.SE_PushButtonContents, option, button)
        text = button.fontMetrics().boundingRect(button.text())
        if area.height() < text.height() or area.width() < button.fontMetrics().horizontalAdvance(button.text()):
            issues.append(dict(kind="button-text", text=button.text(),
                               area=[area.width(), area.height()], needed=[text.width(), text.height()]))
    return issues


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("build/ui-consistency/preview-100"))
    parser.add_argument("--sizes", nargs="+", default=["960x640", "1560x750", "880x580"])
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    for name in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf", "seguisb.ttf", "consola.ttf"):
        font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / name
        if font.exists():
            QFontDatabase.addApplicationFont(str(font))
    report = dict(scale=os.environ.get("QT_SCALE_FACTOR", "1"), pages={}, states={}, contrast={}, navigation={})
    with ExitStack() as stack:
        temp = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="config-", dir=args.output.resolve())))
        for target, value in (
            ("sayink.config.default_secret_store", None),
            ("sayink.config.Config._sync_registry_auto_start", None),
            ("sayink.ui.settings_window.list_microphone_devices", []),
            ("sayink.ui.settings_window.list_system_capture_devices_for_settings", []),
            ("sayink.speech_recognizer.is_model_downloaded", False),
        ):
            stack.enter_context(patch(target, return_value=value))
        config = Config(config_dir=temp)
        config.set("stt.models_dir", str(temp / "models"))
        config.set("history.enabled", True)
        history = HistoryStore(db_path=temp / "meetings.db")
        controller = MeetingController(config, history, pending_segments=lambda: 0)
        try:
            record = controller.store.create_meeting(title="一致性检查（示例）")
            controller.store.end_meeting(record.id, ended_at=record.started_at + 60000)
            history.enqueue(SegmentRecord(record.id, 0, record.started_at, raw_text="先检查布局，再确认操作与反馈。",
                                          trigger_mode="meeting", speaker_id=1))
            controller.store.flush()
            win = MainWindow(config, PreviewStore(), meeting=controller)
            win.settings_panel.set_runtime_status(RuntimeState.READY, "就绪（示例）")
            win.show()
            for theme in ("light", "dark"):
                apply_theme(app, mode=theme, surfaces=[win, win._settings, win._history, win._meeting])
                report["contrast"][theme] = {key: contrast(getattr(tok, key), tok.BG)
                                             for key in ("TEXT", "TEXT_SEC", "TEXT_DIM", "ACCENT_TEXT", "RED", "GREEN_TEXT", "AMBER_TEXT")}
                for size in args.sizes:
                    width, height = map(int, size.split("x"))
                    win.resize(width, height)
                    for page_key in PAGE_KEYS:
                        win.show_page(page_key)
                        for _ in range(3):
                            settle(app)
                        data = dict(size=[win.width(), win.height()], issues=check_controls(win),
                                    checked=[PAGE_KEYS[i] for i, b in enumerate(win._nav_buttons) if b.isChecked()])
                        if page_key in ("general", "engine", "polish", "about"):
                            page = win._settings._pages.currentWidget()
                            page.verticalScrollBar().setValue(0)
                            settle(app)
                            title = next(w for w in page.findChildren(QLabel) if w.property("viRole") == "pageTitle")
                            data.update(body=bounds(page.widget(), win), viewport=bounds(page.viewport(), win),
                                        title=bounds(title, win), title_font=font_snapshot(title),
                                        scroll_max=page.verticalScrollBar().maximum(),
                                        sections=[bounds(page._layout.itemAt(i).widget(), win)
                                                  for i in range(page._layout.count()) if page._layout.itemAt(i).widget()])
                        else:
                            page = win._history if page_key == "history" else win._meeting
                            data.update(title=bounds(page._title_label, win), title_font=font_snapshot(page._title_label),
                                        search=bounds(page._search_edit, win))
                        data["fields"] = [dict(name=field.accessibleName() or field.objectName(),
                                               kind=type(field).__name__, size=[field.width(), field.height()])
                                          for field in page.findChildren(QWidget)
                                          if isinstance(field, (QLineEdit, QComboBox, QSpinBox)) and field.isVisible()]
                        name = f"{page_key}-{size}-{theme}"
                        report["pages"][name] = data
                        win.grab().save(str(args.output / f"{name}.png"))
                win.show_page("about")
                QTest.mouseMove(win._nav_buttons[1], QPoint(12, 12))
                settle(app)
                report["navigation"][theme] = dict(hover=tok.ROW_HOVER, selected=tok.NAV_SELECTED_BG,
                    checked=[PAGE_KEYS[i] for i, b in enumerate(win._nav_buttons) if b.isChecked()])
                win.grab().save(str(args.output / f"sidebar-hover-{theme}.png"))
                win.resize(960, 640)

                def capture_state(name, theme=theme):
                    settle(app)
                    settle(app)
                    report["states"][f"{name}-{theme}"] = dict(issues=check_controls(win))
                    win.grab().save(str(args.output / f"{name}-{theme}.png"))

                settings = win.settings_panel
                settings._about_paths_toggle.setChecked(True)
                capture_state("about-paths")
                settings._about_paths_toggle.setChecked(False)
                settings.set_update_status("检查失败（示例）：当前无法连接更新服务，请稍后重试。" * 3, action="check")
                capture_state("about-long-status")
                settings.set_update_status("本次启动尚未检查更新", action="check")
                win.show_page("general")
                settings._advanced_audio_btn.setChecked(True)
                settings._advanced_recognition_btn.setChecked(True)
                capture_state("general-expanded")
                settings._advanced_audio_btn.setChecked(False)
                settings._advanced_recognition_btn.setChecked(False)
                win.show_page("meeting")
                win._meeting.show_start_card()
                capture_state("meeting-start")
                win._meeting.show_meeting(record.id)
                win._meeting._template_combo.setCurrentIndex(win._meeting._template_combo.findData("refine"))
                capture_state("meeting-refine")
                win._meeting._template_combo.setCurrentIndex(win._meeting._template_combo.findData("general"))
            win.hide()
            win.deleteLater()
            settle(app)
        finally:
            controller.shutdown()
            history.close(timeout=2)
            config.save_immediate()
    (args.output / "measurements.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(dict(output=str(args.output), pages=len(report["pages"]), states=len(report["states"]),
                          issues={key: value["issues"] for key, value in {**report["pages"], **report["states"]}.items()
                                  if value["issues"]}), ensure_ascii=False))


if __name__ == "__main__":
    main()

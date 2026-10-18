"""Render General settings with isolated config and simulated audio states.

Never reads personal history, opens audio devices, loads models or uses network.
Set QT_SCALE_FACTOR=1.5 to inspect display scaling.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import PropertyMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtGui import QColor, QFontDatabase, QPixmap
from PyQt6.QtCore import QRect
from PyQt6.QtWidgets import QApplication, QLabel

from sayink.config import Config
from sayink.runtime_status import RuntimeState, SegmentationStatus
from sayink.ui.main_window import MainWindow
from sayink.ui.theme import apply_theme
from tools.capture_ui import PreviewStore, settle


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("build/general-ux-review/preview-100"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    for font in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf", "consola.ttf"):
        path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / font
        if path.exists():
            QFontDatabase.addApplicationFont(str(path))
    metrics = {}
    with ExitStack() as stack:
        temp = stack.enter_context(tempfile.TemporaryDirectory(prefix="config-", dir=args.output.resolve()))
        for target, value in (
            ("sayink.config.default_secret_store", None),
            ("sayink.config.Config._sync_registry_auto_start", None),
            ("sayink.ui.settings_window.list_microphone_devices", []),
            ("sayink.ui.settings_window.list_system_capture_devices_for_settings", []),
            ("sayink.speech_recognizer.is_model_downloaded", True),
            ("sayink.ui.settings_window.SettingsWindow._rebuild_model_cards", None),
            ("sayink.audio_recorder.AudioRecorder.start", None),
        ):
            stack.enter_context(patch(target, return_value=value))
        stack.enter_context(patch(
            "sayink.audio_recorder.AudioRecorder.capture_device_labels", new_callable=PropertyMock,
            return_value=("麦克风：示例 USB 麦克风",),
        ))
        config = Config(config_dir=Path(temp))
        config.set("stt.models_dir", str(Path(temp) / "models"))
        win = MainWindow(config, PreviewStore())
        settings = win.settings_panel
        settings.set_runtime_status(RuntimeState.READY)
        settings.set_recognition_runtime_status(
            SegmentationStatus("silero", "silero", True),
            speaker_status="CAM++ 已加载，用于持续转写与会议。",
        )
        page = settings._pages.widget(0)
        win.show_page("general")
        win.show()

        def capture(name, *, full=False):
            for _ in range(3):
                settle(app)
            target = page.widget() if full else win
            if full:
                from sayink.ui import design_tokens as tok

                pixmap = QPixmap(target.size() * target.devicePixelRatioF())
                pixmap.setDevicePixelRatio(target.devicePixelRatioF())
                pixmap.fill(QColor(tok.BG))
                target.render(pixmap)
            else:
                pixmap = target.grab()
            pixmap.save(str(args.output / f"{name}.png"))
            clipped = []
            for label in page.findChildren(QLabel):
                if label.isVisible() and label.wordWrap() and label.text():
                    needed = label.heightForWidth(label.width())
                    if needed > label.height() + 2:
                        clipped.append(label.text())
            metrics[name] = {
                "window": [win.width(), win.height()],
                "minimum": [win.minimumWidth(), win.minimumHeight()],
                "document": [page.widget().width(), page.widget().height()],
                "clipped_wrapped_labels": clipped,
            }

        for theme in ("light", "dark"):
            config.set("appearance.theme_mode", theme)
            config.set("audio.input_source", "microphone")
            config.set("audio.trigger_mode", "hotkey")
            config.set("audio.esc_stops_continuous", True)
            config.set("history.enabled", True)
            config.set("output.follow_input_source", True)
            config.follow_input_source_for_output("microphone")
            settings.reload_settings()
            settings._advanced_recognition_btn.setChecked(False)
            settings._advanced_audio_btn.setChecked(False)
            apply_theme(app, mode=theme, surfaces=[win, settings, win.history_panel])
            win.resize(1040, 820)
            page.verticalScrollBar().setValue(0)
            capture(f"general-{theme}")
            capture(f"general-document-{theme}", full=True)
            settings._src_sys_rb.setChecked(True)
            capture(f"general-system-{theme}")
            settings._history_enabled_row.setChecked(False)
            capture(f"general-history-off-{theme}")
            settings._enable_history_btn.click()
            settings._trigger_continuous_rb.setChecked(True)
            capture(f"general-continuous-{theme}")
            settings._src_mic_rb.setChecked(True)
            settings._trigger_hotkey_rb.setChecked(True)
            settings._advanced_recognition_btn.setChecked(True)
            page.ensureWidgetVisible(settings._meeting_vad_combo, 0, 100)
            capture(f"general-advanced-{theme}")
            settings.set_recognition_runtime_status(
                SegmentationStatus("silero", "energy", True, "示例模型加载失败"),
                speaker_status="CAM++ 已加载", pending=True,
            )
            settings._advanced_recognition_btn.setChecked(False)
            page.ensureWidgetVisible(settings._recognition_apply_label, 0, 80)
            capture(f"general-pending-{theme}")
            settings.set_recognition_runtime_status(
                SegmentationStatus("silero", "silero", True), speaker_status="CAM++ 已加载",
            )
            page.verticalScrollBar().setValue(page.verticalScrollBar().maximum())
            capture(f"general-history-{theme}")
            win.resize(880, 640)
            page.verticalScrollBar().setValue(0)
            capture(f"general-narrow-{theme}")
            settings._run_mic_probe()
            settings._on_mic_probe_volume(0.03)
            page.ensureWidgetVisible(settings._mic_test_btn, 0, 80)
            capture(f"general-audio-test-{theme}")
            settings._finish_mic_probe()
            capture(f"general-audio-result-{theme}")
            config.set("audio.input_source", "mixed")
            config.set("audio.trigger_mode", "continuous")
            config.set("history.enabled", False)
            config.set_output_mode("history")
            settings.reload_settings()
            page.verticalScrollBar().setValue(0)

            class CompactScreen:
                def availableGeometry(self):
                    return QRect(0, 0, 910, 472)

            with patch.object(MainWindow, "screen", return_value=CompactScreen()):
                win._fit_to_available_screen()
                capture(f"general-small-screen-{theme}")
                assert win.height() <= 472 and win.minimumHeight() <= 472, "Sidebar exceeds the compact screen"
            win._fit_to_available_screen()
        win.hide()
        config.save_immediate()
    (args.output / "layout-check.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    print(args.output.resolve())


if __name__ == "__main__":
    main()

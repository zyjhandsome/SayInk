"""Capture About UX scenarios using isolated settings and simulated states.

No personal history, credentials, microphone, recognition models or network.
Run with QT_SCALE_FACTOR=1.5 for the scaled layout check.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtGui import QFontDatabase
from PyQt6.QtWidgets import QApplication

from sayink.config import Config, OUTPUT_MODE_HISTORY, OUTPUT_MODE_PASTE
from sayink.runtime_status import RuntimeState
from sayink.ui.main_window import MainWindow
from sayink.ui.theme import apply_theme
from tools.capture_ui import PreviewStore, settle


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("build/about-ux-review/preview-100"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    for font in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf", "consola.ttf"):
        path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / font
        if path.exists():
            QFontDatabase.addApplicationFont(str(path))
    with ExitStack() as stack:
        temp = stack.enter_context(tempfile.TemporaryDirectory(prefix="preview-config-", dir=args.output.resolve()))
        for target, value in (
            ("sayink.config.default_secret_store", None),
            ("sayink.config.Config._sync_registry_auto_start", None),
            ("sayink.ui.settings_window.list_microphone_devices", []),
            ("sayink.ui.settings_window.list_system_capture_devices_for_settings", []),
            ("sayink.speech_recognizer.is_model_downloaded", False),
            ("sayink.ui.settings_window.SettingsWindow._rebuild_model_cards", None),
        ):
            stack.enter_context(patch(target, return_value=value))
        config = Config(config_dir=Path(temp))
        config.set("stt.models_dir", str(Path(temp) / "models"))
        config.set("history.enabled", True)
        win = MainWindow(config, PreviewStore())
        settings = win.settings_panel
        settings.set_runtime_status(RuntimeState.READY)
        win.show_page("about")
        win.show()

        def capture(name):
            for _ in range(4):
                settle(app)
            win.grab().save(str(args.output / f"{name}.png"))

        for theme in ("light", "dark"):
            apply_theme(app, mode=theme, surfaces=[win, settings, win.history_panel])
            config.set("audio.input_source", "microphone")
            config.set("audio.trigger_mode", "hotkey")
            config.set("history.enabled", True)
            config.set_output_mode(OUTPUT_MODE_PASTE)
            settings.reload_settings()
            settings._about_paths_toggle.setChecked(False)
            settings.set_update_status("本次启动尚未检查更新")
            win.resize(960, 640)
            capture(f"about-{theme}")
            settings._about_paths_toggle.setChecked(True)
            capture(f"about-paths-{theme}")
            settings._about_paths_toggle.setChecked(False)
            config.set("audio.input_source", "system")
            config.set_output_mode(OUTPUT_MODE_HISTORY)
            settings.reload_settings()
            capture(f"about-system-{theme}")
            config.set("history.enabled", False)
            settings.reload_settings()
            capture(f"about-unsaved-{theme}")
            config.set("history.enabled", True)
            config.set("audio.input_source", "mixed")
            config.set("audio.trigger_mode", "continuous")
            config.set("audio.esc_stops_continuous", False)
            settings.reload_settings()
            capture(f"about-continuous-{theme}")
            settings.set_update_status("检查失败，请稍后再试", action="check")
            capture(f"about-update-error-{theme}")
            settings.set_update_status("正在下载 5 / 10 MB", action="busy")
            capture(f"about-downloading-{theme}")
            win.resize(880, 580)
            capture(f"about-narrow-{theme}")
            settings._about_paths_toggle.setChecked(True)
            page = settings._pages.widget(3)
            settle(app)
            page.verticalScrollBar().setValue(page.verticalScrollBar().maximum())
            capture(f"about-paths-narrow-{theme}")
            page.verticalScrollBar().setValue(0)
        win.hide()
        config.save_immediate()
    print(args.output.resolve())


if __name__ == "__main__":
    main()

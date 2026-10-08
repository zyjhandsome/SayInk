"""Regression checks for removed file-transcription entry points."""

from sayink.app import App
from sayink.ui.tray_icon import TrayIcon


def test_file_transcription_entry_points_are_absent():
    assert not hasattr(App, "start_file_transcription")
    assert not hasattr(App, "cancel_file_transcription")
    assert not hasattr(TrayIcon, "import_file_requested")


def test_tray_menu_has_no_import_file_action(qapp=None):
    import sys
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    tray = TrayIcon()
    labels = [action.text() for action in tray.contextMenu().actions()]
    assert "导入文件转写…" not in labels
    tray.hide()
    del app

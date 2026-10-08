"""Hotkey capture widget for settings."""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QLineEdit

from sayink.config import format_hotkey

_MODIFIER_KEYS: set[int] = set()


def _init_modifier_keys() -> None:
    global _MODIFIER_KEYS
    _MODIFIER_KEYS = {
        Qt.Key.Key_Control, Qt.Key.Key_Shift, Qt.Key.Key_Alt,
        Qt.Key.Key_AltGr, Qt.Key.Key_Meta,
    }


def _qt_key_to_name(key: int) -> str:
    mapping = {
        Qt.Key.Key_Space: "space", Qt.Key.Key_Tab: "tab",
        Qt.Key.Key_Return: "enter", Qt.Key.Key_Enter: "enter",
    }
    if key in mapping:
        return mapping[key]
    if Qt.Key.Key_A <= key <= Qt.Key.Key_Z:
        return chr(key).lower()
    if Qt.Key.Key_0 <= key <= Qt.Key.Key_9:
        return chr(key)
    if Qt.Key.Key_F1 <= key <= Qt.Key.Key_F12:
        return f"f{key - Qt.Key.Key_F1 + 1}"
    return ""


class HotkeyEdit(QLineEdit):
    hotkey_changed = pyqtSignal(str)
    capture_started = pyqtSignal()
    capture_ended = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        _init_modifier_keys()
        self.setReadOnly(True)
        self.setPlaceholderText("点击此处，然后按下快捷键...")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setAccessibleName("录音快捷键")
        self.setAccessibleDescription(
            "点击后按下组合键绑定快捷键；按住该键开始录音，松开结束。"
        )
        self._capturing = False
        self._value = ""

    @property
    def value(self) -> str:
        return self._value

    def set_value(self, hotkey: str):
        self._value = hotkey
        self.setText(format_hotkey(hotkey))

    def mousePressEvent(self, event):
        self._begin_capture()
        super().mousePressEvent(event)

    def _begin_capture(self) -> None:
        from sayink.ui import design_tokens as tok

        if self._capturing:
            self.capture_ended.emit()
        self._capturing = True
        self.setText("请按下组合键...")
        self.setStyleSheet(
            f"border: 2px solid {tok.ACCENT_FOCUS};"
            f" background: {tok.SURFACE_PEARL}; color: {tok.TEXT};"
        )
        self.capture_started.emit()

    def keyPressEvent(self, event):
        if not self._capturing:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
                self._begin_capture()
                event.accept()
                return
            super().keyPressEvent(event)
            return

        key = event.key()
        modifiers = event.modifiers()

        # Esc cancels recording and continuous listening, so it can never be
        # (part of) the hotkey; here it backs out of the capture instead.
        if key == Qt.Key.Key_Escape:
            self.cancel_capture_if_active()
            event.accept()
            return

        mod_parts = []
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            mod_parts.append("Ctrl")
        if modifiers & Qt.KeyboardModifier.AltModifier:
            mod_parts.append("Alt")
        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            mod_parts.append("Shift")

        if key in _MODIFIER_KEYS:
            if mod_parts:
                self.setText(" + ".join(mod_parts) + " + ...")
            return

        key_name = _qt_key_to_name(key)
        if not key_name:
            return

        display_key = key_name.capitalize() if len(key_name) > 1 else key_name.upper()
        all_parts = mod_parts + [display_key]

        display = " + ".join(all_parts)
        value = "+".join(p.lower() for p in all_parts)

        self.setText(display)
        self._value = value
        self._capturing = False
        self.setStyleSheet("")
        self.hotkey_changed.emit(value)
        self.capture_ended.emit()

    def focusOutEvent(self, event):
        if self._capturing:
            self._capturing = False
            self.setStyleSheet("")
            self.setText(format_hotkey(self._value))
            self.capture_ended.emit()
        super().focusOutEvent(event)

    def cancel_capture_if_active(self):
        """Release global hotkey pause if settings closes during shortcut capture."""
        if not self._capturing:
            return
        self._capturing = False
        self.setStyleSheet("")
        self.setText(format_hotkey(self._value))
        self.capture_ended.emit()

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


_PUNCTUATION_KEY_NAMES = {
    Qt.Key.Key_Minus: "-",
    Qt.Key.Key_Equal: "=",
    Qt.Key.Key_BracketLeft: "[",
    Qt.Key.Key_BracketRight: "]",
    Qt.Key.Key_Backslash: "\\",
    Qt.Key.Key_Semicolon: ";",
    Qt.Key.Key_Apostrophe: "'",
    Qt.Key.Key_Comma: ",",
    Qt.Key.Key_Period: ".",
    Qt.Key.Key_Slash: "/",
    Qt.Key.Key_QuoteLeft: "`",
}

_COMMON_CONFLICT_HINTS: dict[frozenset[str], str] = {
    frozenset({"alt", "space"}): (
        "提示：Alt + Space 常被窗口菜单或启动器（如 PowerToys Run）占用，若无响应可改用 Alt + X。"
    ),
    frozenset({"ctrl", "space"}): (
        "提示：Ctrl + Space 常用于中英文输入法切换，长按时可能触发输入法切换。"
    ),
    frozenset({"win", "space"}): (
        "提示：Win + Space 是系统切换输入法/键盘布局的快捷键，可能产生冲突。"
    ),
    frozenset({"shift", "space"}): (
        "提示：Shift + Space 常用于输入法全角/半角切换，可能产生冲突。"
    ),
    frozenset({"win", "h"}): (
        "提示：Win + H 是 Windows 自带语音输入快捷键，可能产生冲突。"
    ),
    frozenset({"win", "e"}): (
        "提示：Win + E 是系统打开资源管理器的快捷键，占用期间原功能可能受影响。"
    ),
    frozenset({"win", "r"}): (
        "提示：Win + R 是系统打开「运行」的快捷键，占用期间原功能可能受影响。"
    ),
    frozenset({"ctrl", "alt", "a"}): (
        "提示：Ctrl + Alt + A 常被微信/QQ 截图快捷键占用，请注意冲突。"
    ),
    frozenset({"ctrl", "alt", "z"}): (
        "提示：Ctrl + Alt + Z 常被即时通讯软件唤起主窗占用，请注意冲突。"
    ),
    frozenset({"ctrl", "shift", "v"}): (
        "提示：Ctrl + Shift + V 常用于浏览器或编辑器的无格式粘贴，占用期间原功能可能受影响。"
    ),
}


def hotkey_conflict_warning(hotkey: str) -> str:
    """Return a concise non-blocking warning for commonly conflicting shortcuts."""
    if not hotkey:
        return ""
    parts = frozenset(
        "win" if p == "cmd" else p
        for p in (part.strip().lower() for part in hotkey.split("+"))
        if p
    )
    return _COMMON_CONFLICT_HINTS.get(parts, "")


def _qt_key_to_name(key: int) -> str:
    mapping = {
        Qt.Key.Key_Space: "space", Qt.Key.Key_Tab: "tab",
        Qt.Key.Key_Return: "enter", Qt.Key.Key_Enter: "enter",
        **_PUNCTUATION_KEY_NAMES,
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
        self.setPlaceholderText("点击此处按下快捷键（Backspace 清空）...")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setAccessibleName("录音快捷键")
        self.setAccessibleDescription(
            "点击后按下组合键绑定快捷键；按 Backspace 或 Delete 清空；按住该键开始录音，松开结束。"
        )
        self._capturing = False
        self._value = ""

    @property
    def value(self) -> str:
        return self._value

    def set_value(self, hotkey: str):
        self._value = hotkey
        self.setText(format_hotkey(hotkey))

    def clear_hotkey(self) -> None:
        was_capturing = self._capturing
        self._capturing = False
        self._value = ""
        self.setStyleSheet("")
        self.setText("")
        self.hotkey_changed.emit("")
        if was_capturing:
            self.capture_ended.emit()

    def mousePressEvent(self, event):
        self.begin_capture()
        super().mousePressEvent(event)

    def begin_capture(self) -> None:
        """Enter capture immediately. About's 「修改快捷键」 uses this so the first click is enough."""
        self.setFocus(Qt.FocusReason.OtherFocusReason)
        self._begin_capture()

    def _begin_capture(self) -> None:
        from sayink.ui import design_tokens as tok

        if self._capturing:
            self.capture_ended.emit()
        self._capturing = True
        self.setText("请按下组合键（Backspace 清空）...")
        self.setStyleSheet(
            f"border: 2px solid {tok.ACCENT_FOCUS};"
            f" background: {tok.SURFACE_PEARL}; color: {tok.TEXT};"
        )
        self.capture_started.emit()

    def keyPressEvent(self, event):
        key = event.key()
        modifiers = event.modifiers()
        has_any_mod = bool(
            modifiers
            & (
                Qt.KeyboardModifier.ControlModifier
                | Qt.KeyboardModifier.AltModifier
                | Qt.KeyboardModifier.ShiftModifier
                | Qt.KeyboardModifier.MetaModifier
            )
        )

        if not self._capturing:
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space) and not has_any_mod:
                self.begin_capture()
                event.accept()
                return
            if key in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete) and not has_any_mod:
                self.clear_hotkey()
                event.accept()
                return
            super().keyPressEvent(event)
            return

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
        if modifiers & Qt.KeyboardModifier.MetaModifier:
            mod_parts.append("Win")

        # Bare Tab / Shift+Tab moves focus cleanly without trapping keyboard users.
        if key == Qt.Key.Key_Tab and not mod_parts:
            self.cancel_capture_if_active()
            self.focusNextPrevChild(True)
            event.accept()
            return
        if key == Qt.Key.Key_Backtab and (
            not mod_parts or mod_parts == ["Shift"]
        ):
            self.cancel_capture_if_active()
            self.focusNextPrevChild(False)
            event.accept()
            return

        if key in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete) and not mod_parts:
            self.clear_hotkey()
            event.accept()
            return

        if key in _MODIFIER_KEYS:
            if key == Qt.Key.Key_Meta and "Win" not in mod_parts:
                mod_parts.append("Win")
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

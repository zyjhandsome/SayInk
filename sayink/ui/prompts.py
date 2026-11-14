"""Chinese action buttons for questions and short text prompts.

Qt's standard Yes/No and OK/Cancel stay English because the app does not
ship a QTranslator. Callers pass the verb the button actually performs.
"""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)


def style_message_box(
    box: QMessageBox,
    *,
    primary: object | None = None,
    destructive: bool = False,
) -> None:
    """Apply active theme tokens to a QMessageBox and its buttons."""
    from sayink.ui import design_tokens as tok
    from sayink.ui import settings_styles as ss

    box.setStyleSheet(
        ss.WINDOW_CSS
        + f"\nQMessageBox {{ background: {tok.BG}; color: {tok.TEXT}; }}"
        + f"\nQMessageBox QLabel {{ color: {tok.TEXT}; background: transparent; }}"
    )
    primary_css = ss.BTN_DANGER_SOLID_SM if destructive else ss.BTN_ACCENT_SM
    for btn in box.buttons():
        btn.setStyleSheet(primary_css if btn is primary else ss.BTN_GHOST_SM)


def confirm_box(
    parent: QWidget | None,
    title: str,
    text: str,
    *,
    accept: str,
    cancel: str = "取消",
    default_accept: bool = False,
    destructive: bool = False,
) -> tuple[QMessageBox, object]:
    """Build a question. The second value is the accept button.

    Delete-style questions default to 取消. ``default_accept`` keeps Enter
    on the confirming action when that action is not destructive.
    """
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    box.setIcon(QMessageBox.Icon.Warning if destructive else QMessageBox.Icon.Question)
    role = QMessageBox.ButtonRole.DestructiveRole if destructive else QMessageBox.ButtonRole.AcceptRole
    accept_btn = box.addButton(accept, role)
    cancel_btn = box.addButton(cancel, QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(accept_btn if default_accept and not destructive else cancel_btn)
    box.setEscapeButton(cancel_btn)
    style_message_box(box, primary=accept_btn, destructive=destructive)
    return box, accept_btn


def alert_error(parent: QWidget | None, title: str, text: str) -> None:
    """Show a themed error dialog with a Chinese 确定 button."""
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    box.setIcon(QMessageBox.Icon.Critical)
    ok_btn = box.addButton("确定", QMessageBox.ButtonRole.AcceptRole)
    box.setDefaultButton(ok_btn)
    box.setEscapeButton(ok_btn)
    style_message_box(box, primary=ok_btn)
    box.exec()


def confirm(
    parent: QWidget | None,
    title: str,
    text: str,
    *,
    accept: str,
    cancel: str = "取消",
    default_accept: bool = False,
    destructive: bool = False,
) -> bool:
    box, accept_btn = confirm_box(
        parent, title, text,
        accept=accept, cancel=cancel,
        default_accept=default_accept, destructive=destructive,
    )
    box.exec()
    return box.clickedButton() is accept_btn


def text_prompt_box(
    parent: QWidget | None,
    title: str,
    label: str,
    text: str = "",
    *,
    validator=None,
) -> tuple[QDialog, QLineEdit]:
    """A one-line prompt whose buttons say 保存 and 取消."""
    from sayink.ui import design_tokens as tok
    from sayink.ui import settings_styles

    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    dialog.setMinimumWidth(420)
    dialog.setStyleSheet(
        settings_styles.WINDOW_CSS
        + f"\nQDialog {{ background: {tok.BG}; color: {tok.TEXT}; }}"
    )
    layout = QVBoxLayout(dialog)
    message = QLabel(label)
    message.setWordWrap(True)
    layout.addWidget(message)
    edit = QLineEdit(text)
    edit.setAccessibleName(title)
    layout.addWidget(edit)
    error_label = QLabel("")
    error_label.setObjectName("promptErrorLabel")
    error_label.setWordWrap(True)
    error_label.setStyleSheet(
        f"color: {tok.RED_TEXT}; font-size: {tok.TYPE_CAPTION}px;"
        " font-weight: 700; background: transparent;"
    )
    error_label.hide()
    layout.addWidget(error_label)
    dialog._error_label = error_label  # type: ignore[attr-defined]
    buttons = QDialogButtonBox()
    save = buttons.addButton("保存", QDialogButtonBox.ButtonRole.AcceptRole)
    cancel = buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
    save.setStyleSheet(settings_styles.BTN_PRIMARY)
    cancel.setStyleSheet(settings_styles.BTN_GHOST)
    save.setDefault(True)
    cancel.setAutoDefault(False)

    def _set_validation_error(err: str) -> None:
        msg = (err or "").strip()
        if msg:
            error_label.setText(msg)
            error_label.show()
            edit.setStyleSheet(
                f"QLineEdit {{ background: {tok.SURFACE}; color: {tok.TEXT};"
                f" border: 2px solid {tok.RED_TEXT}; border-radius: {tok.RADIUS_MD}px;"
                " padding: 5px 11px; }"
            )
        else:
            error_label.clear()
            error_label.hide()
            edit.setStyleSheet("")

    dialog.set_validation_error = _set_validation_error  # type: ignore[attr-defined]

    def _on_accept() -> None:
        if callable(validator):
            err = validator(edit.text())
            if err:
                _set_validation_error(str(err))
                edit.setFocus()
                return
        _set_validation_error("")
        dialog.accept()

    edit.textChanged.connect(lambda _txt: _set_validation_error("") if error_label.isVisible() else None)
    buttons.accepted.connect(_on_accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    edit.setFocus()
    return dialog, edit


def prompt_text(
    parent: QWidget | None,
    title: str,
    label: str,
    text: str = "",
    *,
    validator=None,
) -> str | None:
    """Return the entered text, including "", or None when cancelled."""
    dialog, edit = text_prompt_box(parent, title, label, text, validator=validator)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return edit.text()

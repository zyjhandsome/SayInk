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
    return box, accept_btn


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
) -> tuple[QDialog, QLineEdit]:
    """A one-line prompt whose buttons say 保存 and 取消."""
    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    dialog.setMinimumWidth(420)
    layout = QVBoxLayout(dialog)
    message = QLabel(label)
    message.setWordWrap(True)
    layout.addWidget(message)
    edit = QLineEdit(text)
    edit.setAccessibleName(title)
    layout.addWidget(edit)
    buttons = QDialogButtonBox()
    save = buttons.addButton("保存", QDialogButtonBox.ButtonRole.AcceptRole)
    cancel = buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
    save.setDefault(True)
    cancel.setAutoDefault(False)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    edit.setFocus()
    return dialog, edit


def prompt_text(
    parent: QWidget | None,
    title: str,
    label: str,
    text: str = "",
) -> str | None:
    """Return the entered text, including "", or None when cancelled."""
    dialog, edit = text_prompt_box(parent, title, label, text)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return edit.text()

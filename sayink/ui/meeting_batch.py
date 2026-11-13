"""Copy for confirming a hard delete of several meetings."""

from __future__ import annotations

from PyQt6.QtWidgets import QPushButton, QSizePolicy


def batch_delete_copy(count: int, live_count: int) -> tuple[str, str]:
    text = f"删除 {count} 场已结束的会议？"
    info = "文字稿、笔记、标记与纪要会一起删除，无法撤销。"
    if live_count:
        info += f"另有 {live_count} 场正在进行，不会删除。"
    return text, info


def confirm_batch_delete(parent, count: int, live_count: int) -> bool:
    if count <= 0:
        return False
    from sayink.ui import design_tokens as tok
    from sayink.ui import settings_styles as ss
    from sayink.ui.prompts import confirm_box

    text, info = batch_delete_copy(count, live_count)
    box, delete = confirm_box(
        parent,
        "删除会议",
        text,
        accept="删除",
        cancel="取消",
        destructive=True,
    )
    box.setInformativeText(info)
    box.setStyleSheet(
        ss.WINDOW_CSS
        + f" QMessageBox {{ background: {tok.BG}; color: {tok.TEXT}; }}"
        + f" QMessageBox QLabel {{ color: {tok.TEXT}; background: transparent; }}"
    )
    delete.setStyleSheet(ss.BTN_DANGER_SOLID_SM)
    for btn in box.buttons():
        if btn is not delete:
            btn.setStyleSheet(ss.BTN_GHOST_SM)
    box.exec()
    return box.clickedButton() == delete


def fit_action_button(button: QPushButton, *, pad: int = 28) -> None:
    """Keep the label intact. A narrow column must not squeeze it to one glyph."""
    button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    button.ensurePolished()
    button.setMinimumWidth(button.fontMetrics().horizontalAdvance(button.text()) + pad)

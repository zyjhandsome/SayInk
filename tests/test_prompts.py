"""Chinese action labels on questions and one-line prompts."""

import sys

import pytest
from PyQt6.QtWidgets import QApplication, QPushButton


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication(sys.argv)


def test_delete_confirm_defaults_to_cancel_and_avoids_english_buttons(qapp):
    from sayink.ui.prompts import confirm_box

    box, accept = confirm_box(None, "删除录音", "永久删除？", accept="删除", destructive=True)
    labels = {button.text().replace("&", "") for button in box.buttons()}
    assert labels == {"删除", "取消"}
    assert accept.text() == "删除"
    assert box.defaultButton().text() == "取消"
    assert box.escapeButton().text() == "取消"
    box.deleteLater()


def test_non_destructive_confirm_can_default_to_the_action(qapp):
    from sayink.ui.prompts import confirm_box

    box, _accept = confirm_box(
        None, "下载失败", "仍然开始？",
        accept="仍然开始", cancel="返回", default_accept=True,
    )
    assert box.defaultButton().text() == "仍然开始"
    assert [button.text() for button in box.buttons()] == ["仍然开始", "返回"]
    box.deleteLater()


def test_text_prompt_uses_save_and_cancel(qapp):
    from sayink.ui.prompts import text_prompt_box

    dialog, edit = text_prompt_box(None, "为整组说话人命名", "显示名称", "小林")
    labels = [button.text() for button in dialog.findChildren(QPushButton)]
    assert "保存" in labels
    assert "取消" in labels
    assert "OK" not in labels
    assert "Cancel" not in labels
    assert edit.text() == "小林"
    dialog.deleteLater()

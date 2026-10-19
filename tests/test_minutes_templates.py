"""Custom template editing, persistence and selection across UI entry points."""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QComboBox, QDialog, QMessageBox

from sayink.config import Config
from sayink.minutes_prompts import get_template, list_templates
from sayink.ui.minutes_templates import (
    MinutesTemplatesDialog, TemplateEditorDialog, populate_template_combo,
)


def test_editor_validates_and_preserves_selected_sections():
    editor = TemplateEditorDialog(get_template("general"))
    editor._name.clear()
    editor.accept()
    assert editor.result() != QDialog.DialogCode.Accepted
    assert "名称" in editor._error.text()
    editor._name.setText("项目进展")
    editor._focus.clear()
    editor.accept()
    assert "整理要求" in editor._error.text()
    editor._focus.setPlainText("按项目记录进展和风险，待办写明负责人。")
    for check in editor._sections.values():
        check.setChecked(False)
    editor.accept()
    assert "至少选择" in editor._error.text()
    editor._sections["action_items"].setChecked(True)
    editor.accept()
    assert editor.entry["sections"] == ["action_items"]
    assert editor.entry["focus"].startswith("按项目")


def test_copy_edit_save_and_reopen_custom_template(config, monkeypatch):
    def fill_and_save(editor):
        editor._name.setText("项目跟进")
        editor._focus.setPlainText("突出每个项目的阻塞与负责人。")
        editor.accept()
        return editor.result()

    monkeypatch.setattr(TemplateEditorDialog, "exec", fill_and_save)
    manager = MinutesTemplatesDialog(config, "standup")
    manager._copy_btn.click()
    template_id = manager.selected_template_id
    assert template_id.startswith("custom-")
    assert config.get("meeting.custom_templates") == []
    assert get_template("standup").name == "周会 / 站会"
    assert manager._edit_btn.isEnabled()
    manager.accept()
    config.save_immediate()
    reopened = Config(config_dir=config.config_dir)
    template = get_template(template_id, reopened.get("meeting.custom_templates"))
    assert template.name == "项目跟进"
    assert template.focus == "突出每个项目的阻塞与负责人。"
    assert template.sections == get_template("standup").sections
    combo = QComboBox()
    populate_template_combo(combo, reopened, template_id)
    assert combo.currentData() == template_id
    assert "负责人" in combo.currentData(Qt.ItemDataRole.ToolTipRole)


def test_cancel_discards_edits_and_refinement_copy_keeps_output_mode(config, monkeypatch):
    def save(editor):
        editor.accept()
        return editor.result()

    monkeypatch.setattr(TemplateEditorDialog, "exec", save)
    manager = MinutesTemplatesDialog(config, "refine")
    manager._copy_btn.click()
    template = get_template(manager.selected_template_id, manager._custom)
    assert template.is_refinement
    assert template.sections == ()
    manager.reject()
    assert config.get("meeting.custom_templates") == []


def test_delete_custom_default_falls_back_and_builtin_cannot_be_deleted(config, monkeypatch):
    entry = {"id": "custom-a", "name": "模板 A", "focus": "突出结论", "sections": ["summary"]}
    config.set("meeting.custom_templates", [entry])
    config.set("meeting.template_id", "custom-a")
    manager = MinutesTemplatesDialog(config, "custom-a")
    monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.StandardButton.Yes)
    manager._delete_btn.click()
    manager.accept()
    assert config.get("meeting.custom_templates") == []
    assert config.get("meeting.template_id") == "general"
    count = len(list_templates(manager._custom))
    manager._delete()
    assert len(list_templates(manager._custom)) == count
    assert not manager._delete_btn.isEnabled()


def test_combo_refresh_does_not_emit_or_lose_custom_default(config):
    config.set("meeting.custom_templates", [{"id": "custom-a", "name": "自定义", "focus": "突出结论"}])
    config.set("meeting.template_id", "custom-a")
    combo = QComboBox()
    changes = []
    combo.currentIndexChanged.connect(changes.append)
    populate_template_combo(combo, config)
    assert combo.currentData() == "custom-a"
    populate_template_combo(combo, config)
    assert changes == []

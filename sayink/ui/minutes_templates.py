"""Local template management shared by the meeting and AI settings pages."""
from __future__ import annotations

import copy
import uuid

from PyQt6.QtCore import QSignalBlocker, Qt
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QGridLayout,
    QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPlainTextEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from sayink.minutes_prompts import (
    ALL_SECTIONS, GENERAL_TEMPLATE_ID, SECTION_TITLES,
    describe_template, get_template, list_templates,
)
from sayink.ui import design_tokens as tok
from sayink.ui import settings_styles as ss
from sayink.ui.prompts import confirm


def populate_template_combo(combo, config, selected_id=None) -> None:
    """Refresh names and custom entries without persisting intermediate choices."""
    selected_id = selected_id or config.get("meeting.template_id", GENERAL_TEMPLATE_ID)
    templates = list_templates(config.get("meeting.custom_templates", []) or [])
    expected = [(t.id, t.name, describe_template(t)) for t in templates]
    actual = [(combo.itemData(i), combo.itemText(i), combo.itemData(i, Qt.ItemDataRole.ToolTipRole))
              for i in range(combo.count())]
    blocker = QSignalBlocker(combo)
    if expected != actual:
        combo.clear()
        for template_id, name, description in expected:
            combo.addItem(name, template_id)
            combo.setItemData(combo.count() - 1, description, Qt.ItemDataRole.ToolTipRole)
    index = combo.findData(selected_id)
    combo.setCurrentIndex(max(0, index))
    combo.setToolTip(str(combo.currentData(Qt.ItemDataRole.ToolTipRole) or ""))
    del blocker


def _style_dialog(dialog) -> None:
    dialog.setStyleSheet(ss.WINDOW_CSS + f"""
        QDialog {{ background: {tok.BG}; }}
        QListWidget, QPlainTextEdit {{
            background: {tok.SURFACE_PEARL}; color: {tok.TEXT};
            border: 1px solid {tok.CONTROL_BORDER}; border-radius: {tok.RADIUS_SM}px;
            padding: 8px; font-size: {tok.TYPE_BODY_SM}px;
        }}
        QListWidget::item {{ padding: 8px; }}
        QListWidget::item:selected {{ background: {tok.ROW_SELECTED}; color: {tok.TEXT}; }}
        QCheckBox {{ color: {tok.TEXT}; padding: 4px; }}
    """)
    for button in dialog.findChildren(QPushButton):
        button.setStyleSheet(ss.BTN_GHOST_SM)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
    screen = dialog.screen()
    if screen is not None:
        available = screen.availableGeometry()
        dialog.resize(min(dialog.width(), available.width() - 24),
                      min(dialog.height(), available.height() - 48))


class TemplateEditorDialog(QDialog):
    def __init__(self, template, parent=None):
        super().__init__(parent)
        self.setWindowTitle("编辑自定义模板" if not template.builtin else "新建自定义模板")
        self.resize(540, 580)
        self.template_id = template.id
        self.entry = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(4, 4, 12, 4)
        layout.setSpacing(10)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)
        self._name = QLineEdit(template.name)
        self._name.setAccessibleName("模板名称")
        self._name.setMaxLength(60)
        self._mode = QComboBox()
        self._mode.setAccessibleName("模板输出形式")
        self._mode.addItem("会议纪要（按所选章节输出）", "minutes")
        self._mode.addItem("原文精整（连续正文）", "refine")
        self._mode.setCurrentIndex(1 if template.is_refinement else 0)
        self._focus = QPlainTextEdit(template.focus)
        self._focus.setAccessibleName("模板整理要求")
        self._focus.setPlaceholderText("例如：按项目整理进展、风险和下一步；待办写明负责人和期限。")
        self._focus.setMinimumHeight(110)
        for title, field in (("模板名称", self._name), ("输出形式", self._mode), ("整理要求", self._focus)):
            label = QLabel(title)
            label.setBuddy(field)
            layout.addWidget(label)
            layout.addWidget(field, 1 if field is self._focus else 0)
        self._sections_label = QLabel("输出章节")
        layout.addWidget(self._sections_label)
        grid = QGridLayout()
        self._sections = {}
        section_order = dict.fromkeys((*template.sections, *ALL_SECTIONS))
        for index, section in enumerate(section_order):
            check = QCheckBox(SECTION_TITLES[section])
            check.setChecked(section in template.sections)
            grid.addWidget(check, index // 2, index % 2)
            self._sections[section] = check
        layout.addLayout(grid)
        self._error = QLabel()
        self._error.setWordWrap(True)
        self._error.setStyleSheet(f"color: {tok.RED};")
        layout.addWidget(self._error)
        pending = QLabel("这里只加入待保存列表。回到上一窗口后，要点「保存并使用所选模板」才会写入设置；取消则放弃。")
        pending.setWordWrap(True)
        layout.addWidget(pending)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("加入待保存列表")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        self._mode.currentIndexChanged.connect(self._sync_mode)
        self._sync_mode()
        _style_dialog(self)

    def _sync_mode(self) -> None:
        enabled = self._mode.currentData() == "minutes"
        self._sections_label.setEnabled(enabled)
        for check in self._sections.values():
            check.setEnabled(enabled)

    def accept(self) -> None:
        name, focus = self._name.text().strip(), self._focus.toPlainText().strip()
        sections = [section for section, check in self._sections.items() if check.isChecked()]
        mode = self._mode.currentData()
        if not name:
            self._error.setText("请填写模板名称。")
            self._name.setFocus()
            return
        if not focus:
            self._error.setText("请填写整理要求，说明希望如何组织内容。")
            self._focus.setFocus()
            return
        if mode == "minutes" and not sections:
            self._error.setText("请至少选择一个输出章节。")
            return
        self.entry = {"id": self.template_id, "name": name, "focus": focus,
                      "sections": sections if mode == "minutes" else [], "output_mode": mode}
        super().accept()


class MinutesTemplatesDialog(QDialog):
    """Stage changes locally; Save writes them together and Cancel discards them."""
    def __init__(self, config, selected_id=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("管理会议模板")
        self.resize(540, 570)
        self._config = config
        self._custom = copy.deepcopy(config.get("meeting.custom_templates", []) or [])
        self.selected_template_id = selected_id or GENERAL_TEMPLATE_ID
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)
        hint = QLabel("内置模板可复制后修改；自定义模板可设置名称、整理要求和输出章节。AI 接口在「AI 服务」中单独配置。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self._list = QListWidget()
        self._list.setAccessibleName("会议模板列表")
        self._list.currentItemChanged.connect(self._selection_changed)
        layout.addWidget(self._list, 1)
        self._description = QPlainTextEdit()
        self._description.setReadOnly(True)
        self._description.setAccessibleName("模板说明")
        self._description.setMinimumHeight(72)
        self._description.setMaximumHeight(110)
        layout.addWidget(self._description)
        row = QHBoxLayout()
        self._new_btn = QPushButton("新建")
        self._copy_btn = QPushButton("复制")
        self._edit_btn = QPushButton("编辑")
        self._delete_btn = QPushButton("删除")
        for button, callback in ((self._new_btn, self._new), (self._copy_btn, self._copy),
                                 (self._edit_btn, self._edit), (self._delete_btn, self._delete)):
            button.clicked.connect(callback)
            row.addWidget(button)
        layout.addLayout(row)
        self._list.itemDoubleClicked.connect(lambda _item: self._edit() if self._edit_btn.isEnabled() else self._copy())
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存并使用所选模板")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._reload(self.selected_template_id)
        _style_dialog(self)

    def _reload(self, selected_id) -> None:
        self._list.clear()
        for template in list_templates(self._custom):
            item = QListWidgetItem(template.name + (" · 内置" if template.builtin else " · 自定义"))
            item.setData(Qt.ItemDataRole.UserRole, template.id)
            item.setToolTip(describe_template(template))
            self._list.addItem(item)
            if template.id == selected_id:
                self._list.setCurrentItem(item)
        if not self._list.currentItem():
            self._list.setCurrentRow(0)

    def _selected(self):
        item = self._list.currentItem()
        return get_template(str(item.data(Qt.ItemDataRole.UserRole)) if item else GENERAL_TEMPLATE_ID, self._custom)

    def _selection_changed(self, *_args) -> None:
        template = self._selected()
        self.selected_template_id = template.id
        self._description.setPlainText(describe_template(template))
        self._edit_btn.setEnabled(not template.builtin)
        self._delete_btn.setEnabled(not template.builtin)

    def _save_editor(self, template, *, new=False) -> None:
        editor = TemplateEditorDialog(template, self)
        if new:
            editor.template_id = "custom-" + uuid.uuid4().hex
        if editor.exec() != QDialog.DialogCode.Accepted:
            return
        entry = editor.entry
        self._custom = [t for t in self._custom if t.get("id") != entry["id"]]
        self._custom.append(entry)
        self._reload(entry["id"])

    def _new(self) -> None:
        from sayink.minutes_prompts import MinutesTemplate

        self._save_editor(MinutesTemplate(id="", name="", focus=""), new=True)

    def _copy(self) -> None:
        from dataclasses import replace

        template = self._selected()
        self._save_editor(replace(template, name=template.name + "（副本）"), new=True)

    def _edit(self) -> None:
        if not self._selected().builtin:
            self._save_editor(self._selected())

    def _delete(self) -> None:
        template = self._selected()
        if template.builtin:
            return
        if not confirm(
            self, "删除模板", f"删除「{template.name}」？已保存的纪要正文会保留。",
            accept="删除", destructive=True,
        ):
            return
        self._custom = [t for t in self._custom if t.get("id") != template.id]
        self._reload(GENERAL_TEMPLATE_ID)

    def accept(self) -> None:
        self._config.set("meeting.custom_templates", self._custom)
        if self._config.get("meeting.template_id", GENERAL_TEMPLATE_ID) not in {t.id for t in list_templates(self._custom)}:
            self._config.set("meeting.template_id", GENERAL_TEMPLATE_ID)
        super().accept()

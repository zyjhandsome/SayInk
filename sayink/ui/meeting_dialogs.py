"""Dialogs and themed dialog styling helpers for MeetingPage."""

from __future__ import annotations

from contextlib import contextmanager

from PyQt6.QtCore import QDateTime, QEvent, Qt
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sayink.ui.prompts import style_message_box


@contextmanager
def _quiet_tooltips(widget: QWidget):
    """Hide tips before a dialog, and don't let them pop again while it is open."""
    from PyQt6.QtWidgets import QToolTip

    QToolTip.hideText()
    forced = widget.testAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips)
    if forced:
        widget.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips, False)
    try:
        yield
    finally:
        if forced:
            widget.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips, True)
        QToolTip.hideText()


def _style_dialog_box(box: QMessageBox, *, danger_btn: QPushButton | None = None) -> None:
    primary = danger_btn
    if primary is None:
        for btn in box.buttons():
            if box.buttonRole(btn) == QMessageBox.ButtonRole.AcceptRole:
                primary = btn
                break
    style_message_box(box, primary=primary, destructive=danger_btn is not None)


def build_meeting_datetime_edit_css() -> str:
    """Build 40px rounded themed QSS for QDateTimeEdit and its calendar popup."""
    from sayink.ui import design_tokens as tok
    from sayink.ui import settings_styles as ss

    shared_builder = getattr(ss, "build_date_time_edit_css", None)
    if callable(shared_builder):
        base_css = str(shared_builder())
    else:
        down = ss._spin_arrow_urls()[1]
        base_css = f"""
        QDateTimeEdit {{
            background: {tok.INPUT_BG};
            color: {tok.TEXT};
            border: 1px solid {tok.CONTROL_BORDER};
            border-radius: {tok.RADIUS_MD}px;
            padding: 6px 28px 6px 12px;
            font-size: {tok.TYPE_BODY_SM}px;
            min-height: {tok.CONTROL_FIELD_HEIGHT - 14}px;
            max-height: {tok.CONTROL_FIELD_HEIGHT - 14}px;
            selection-background-color: {tok.PRIMARY_CONTAINER};
            selection-color: {tok.PRIMARY_ON};
        }}
        QDateTimeEdit:hover {{
            border-color: {tok.CONTROL_BORDER_HOVER};
        }}
        QDateTimeEdit:focus, QDateTimeEdit:on {{
            border: 2px solid {tok.ACCENT_FOCUS};
            padding: 5px 27px 5px 11px;
        }}
        QDateTimeEdit:disabled {{
            color: {tok.TEXT_DIM};
            background: {tok.BAR_OFF};
            border-color: {tok.HAIRLINE};
        }}
        QDateTimeEdit::drop-down {{
            subcontrol-origin: padding;
            subcontrol-position: center right;
            width: 28px;
            border: none;
            background: transparent;
        }}
        QDateTimeEdit::down-arrow {{
            image: url("{down}");
            width: 10px;
            height: 6px;
        }}
        """
    calendar_css = f"""
        QCalendarWidget QWidget {{
            background: {tok.SURFACE_PEARL};
            color: {tok.TEXT};
            alternate-background-color: {tok.SURFACE};
        }}
        QCalendarWidget QAbstractItemView:enabled {{
            background: {tok.SURFACE};
            color: {tok.TEXT};
            selection-background-color: {tok.ACCENT};
            selection-color: #FFFFFF;
            outline: none;
        }}
        QCalendarWidget QToolButton {{
            background: transparent;
            color: {tok.TEXT};
            border: none;
            border-radius: {tok.RADIUS_SM}px;
            padding: 4px 8px;
            font-weight: 700;
        }}
        QCalendarWidget QToolButton:hover {{
            background: {tok.ROW_HOVER};
        }}
    """
    return base_css + calendar_css


class _MeetingTimeDialog(QDialog):
    """Themed dialog for shifting a completed/interrupted meeting's start timestamp."""

    def __init__(self, started_at_ms: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from sayink.ui import design_tokens as tok
        from sayink.ui import settings_styles as ss

        self.setWindowTitle("修改会议时间")
        self.setObjectName("meetingTimeDialog")
        self.setMinimumWidth(400)
        self.setStyleSheet(
            ss.WINDOW_CSS
            + build_meeting_datetime_edit_css()
            + f"""
            QDialog#meetingTimeDialog {{ background: {tok.SURFACE}; color: {tok.TEXT}; }}
            QLabel {{ background: transparent; color: {tok.TEXT_SEC}; font-size: {tok.TYPE_BODY_SM}px; }}
            """
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)
        hint = QLabel("列表按这个时间排序。修改后，每句话相对会议开始的时间不变。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.datetime_edit = QDateTimeEdit(QDateTime.fromMSecsSinceEpoch(int(started_at_ms)))
        self.datetime_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.datetime_edit.setCalendarPopup(True)
        self.datetime_edit.setAccessibleName("会议时间")
        layout.addWidget(self.datetime_edit)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setStyleSheet(ss.BTN_GHOST_SM)
        self.cancel_btn.clicked.connect(self.reject)
        self.save_btn = QPushButton("保存")
        self.save_btn.setStyleSheet(ss.BTN_PRIMARY)
        self.save_btn.setDefault(True)
        self.save_btn.clicked.connect(self.accept)
        buttons.addWidget(self.cancel_btn)
        buttons.addWidget(self.save_btn)
        layout.addLayout(buttons)
        self.datetime_edit.setFocus()

    @property
    def started_ms(self) -> int:
        return int(self.datetime_edit.dateTime().toMSecsSinceEpoch())


class _SegmentSpeakerDialog(QDialog):
    def __init__(self, segment, choices: list[tuple[int | None, str]], parent=None, *, save_speaker=None, live=False):
        super().__init__(parent)
        self._save_speaker = save_speaker
        from sayink.ui import design_tokens as tok
        from sayink.ui import settings_styles as ss

        self.setWindowTitle("修改本段说话人")
        self.setObjectName("meetingSpeakerCorrection")
        self.setFixedWidth(420)
        self.setStyleSheet(ss.WINDOW_CSS + f"""
            QDialog#meetingSpeakerCorrection {{ background: {tok.SURFACE}; color: {tok.TEXT}; }}
            QLabel {{ background: transparent; color: {tok.TEXT}; font-size: {tok.TYPE_BODY_SM}px; }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(12)
        preview = QLabel((segment.corrected_text or segment.polished_text or segment.raw_text)[:220])
        preview.setTextFormat(Qt.TextFormat.PlainText)
        preview.setWordWrap(True)
        lay.addWidget(preview)
        self.speaker_combo = QComboBox()
        self.speaker_combo.setAccessibleName("这一段的发言人")
        self.speaker_combo.setPlaceholderText("请选择这一段的发言人")
        for speaker_id, label in choices:
            self.speaker_combo.addItem(label, speaker_id)
        self.speaker_combo.setCurrentIndex(self.speaker_combo.findData(segment.speaker_id))
        lay.addWidget(self.speaker_combo)
        hint = QLabel(
            "更正这一段，人工更正会保留。清晰的声音会帮助判断后续发言；其他已有段落保持原归属。" if live else
            "仅更改这一段的归属。现有纪要中的发言人需要核对，或重新生成纪要。"
        )
        hint.setWordWrap(True)
        lay.addWidget(hint)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet(f"color: {tok.RED}; font-weight: 700; background: transparent;")
        self.error.hide()
        lay.addWidget(self.error)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setStyleSheet(ss.BTN_GHOST_SM)
        cancel.clicked.connect(self.reject)
        self.save_btn = QPushButton("应用更正")
        self.save_btn.setStyleSheet(ss.BTN_PRIMARY)
        self.save_btn.clicked.connect(self._save)
        buttons.addWidget(cancel)
        buttons.addWidget(self.save_btn)
        lay.addLayout(buttons)

    @property
    def speaker_id(self) -> int | None:
        value = self.speaker_combo.currentData()
        return int(value) if value is not None else None

    def _save(self) -> None:
        if self.speaker_combo.currentIndex() < 0:
            self.error.setText("请先选择这一段的发言人。")
            self.error.show()
            return
        if self._save_speaker is not None and not self._save_speaker(self.speaker_id):
            self.error.setText("更正尚未保存，选择已保留。请重试；会议已结束或正在整理时，请关闭后重新选择。")
            self.error.show()
            return
        self.accept()


class _SegmentTextDialog(QDialog):
    """Keep a segment draft open until its write has been verified."""

    def __init__(
        self,
        segment,
        *,
        position: str,
        speaker: str,
        save_text,
        parent=None,
        details: str = "",
        read_only: bool = False,
    ):
        super().__init__(parent)
        from sayink.ui import design_tokens as tok
        from sayink.ui import settings_styles as ss

        self._save_text = save_text
        self._original = (segment.polished_text or segment.raw_text).strip()
        self._initial = (segment.corrected_text or self._original).strip()
        self._read_only = read_only
        self.setWindowTitle("修改详情" if details else "编辑本段文字稿")
        self.setObjectName("meetingTextCorrection")
        self.setStyleSheet(ss.WINDOW_CSS + f"""
            QDialog#meetingTextCorrection {{ background: {tok.SURFACE}; color: {tok.TEXT}; }}
            QLabel {{ background: transparent; color: {tok.TEXT}; font-size: {tok.TYPE_BODY_SM}px; }}
            QPlainTextEdit {{ background: {tok.SURFACE_PEARL}; color: {tok.TEXT};
                border: 1px solid {tok.HAIRLINE}; border-radius: {tok.RADIUS_MD}px;
                padding: 8px; font-size: {tok.READING_FONT_PX}px;
                selection-background-color: {tok.ACCENT_SOFT}; selection-color: {tok.TEXT}; }}
        """)
        self.setMinimumSize(320, 300)
        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            self.resize(
                min(600, max(320, available.width() - 48)),
                min(500, max(300, available.height() - 64)),
            )
        else:
            self.resize(600, 500)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(12)
        context = QLabel(f"[{position}]  {speaker}" if position else speaker)
        context.setTextFormat(Qt.TextFormat.PlainText)
        context.setWordWrap(True)
        lay.addWidget(context)
        if details:
            self.details_label = QLabel(details)
            self.details_label.setTextFormat(Qt.TextFormat.PlainText)
            self.details_label.setWordWrap(True)
            lay.addWidget(self.details_label)
        hint = QLabel(
            "当前只能查看。生成或收尾结束后可以再改这一段。" if read_only else
            "仅修改这一段。保存后用于纪要、原文精整、复制和导出，时间与发言人保持不变。"
        )
        hint.setWordWrap(True)
        lay.addWidget(hint)
        self.editor = QPlainTextEdit()
        self.editor.setAccessibleName("本段文字稿内容")
        self.editor.setPlainText(self._initial)
        self.editor.setReadOnly(read_only)
        lay.addWidget(self.editor, 1)
        self.original_view = QPlainTextEdit()
        self.original_view.setAccessibleName("修改前的原文")
        self.original_view.setReadOnly(True)
        self.original_view.setPlainText(self._original)
        self.original_view.setMaximumHeight(110)
        self.original_view.hide()
        lay.addWidget(self.original_view)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setAccessibleName("文字稿保存状态")
        self.error.setStyleSheet(f"color: {tok.RED}; font-weight: 700; background: transparent;")
        self.error.hide()
        lay.addWidget(self.error)
        buttons = QHBoxLayout()
        self.original_btn = QPushButton("查看原文")
        self.original_btn.setCheckable(True)
        self.original_btn.setStyleSheet(ss.BTN_GHOST_SM)
        self.original_btn.toggled.connect(self.original_view.setVisible)
        self.original_btn.toggled.connect(lambda shown: self.original_btn.setText("收起原文" if shown else "查看原文"))
        buttons.addWidget(self.original_btn)
        self.restore_btn = QPushButton("恢复原文")
        self.restore_btn.setStyleSheet(ss.BTN_GHOST_SM)
        self.restore_btn.setToolTip("将本段恢复为修改前的文字，点击「保存修改」后生效")
        self.restore_btn.clicked.connect(self._restore_original)
        buttons.addWidget(self.restore_btn)
        buttons.addStretch(1)
        lay.insertLayout(lay.indexOf(self.original_view), buttons)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("取消")
        if read_only:
            cancel.setText("关闭")
        cancel.setStyleSheet(ss.BTN_GHOST_SM)
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.save_btn = QPushButton("保存修改")
        self.save_btn.setStyleSheet(ss.BTN_PRIMARY)
        self.save_btn.clicked.connect(self._save)
        self.save_btn.setDefault(True)
        buttons.addWidget(self.save_btn)
        lay.addLayout(buttons)
        self.editor.textChanged.connect(self._sync_actions)
        self._sync_actions()
        self.restore_btn.setVisible(not read_only)
        self.save_btn.setVisible(not read_only)
        if details and segment.corrected_text:
            self.original_btn.setChecked(True)
        self.editor.setFocus()
        self.editor.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:
        if obj is self.editor and event.type() == QEvent.Type.KeyPress:
            if (
                event.modifiers() == Qt.KeyboardModifier.ControlModifier
                and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
            ):
                self._save()
                return True
        return super().eventFilter(obj, event)

    def _restore_original(self) -> None:
        """One undoable edit, so Ctrl+Z brings back what was just replaced."""
        cursor = self.editor.textCursor()
        cursor.beginEditBlock()
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.insertText(self._original)
        cursor.endEditBlock()
        self.editor.setTextCursor(cursor)

    @property
    def corrected_text(self) -> str | None:
        text = self.editor.toPlainText().strip()
        return None if text == self._original else text

    def _sync_actions(self) -> None:
        text = self.editor.toPlainText().strip()
        self.save_btn.setEnabled(not self._read_only and bool(text) and text != self._initial)
        self.restore_btn.setEnabled(text != self._original)

    def _save(self) -> None:
        if not self.save_btn.isEnabled():
            return
        if not self._save_text(self.corrected_text):
            self.error.setText("尚未保存。修改内容仍保留在这里，请重试或复制备份。")
            self.error.show()
            return
        self.accept()


__all__ = [
    "_MeetingTimeDialog",
    "_SegmentSpeakerDialog",
    "_SegmentTextDialog",
    "_quiet_tooltips",
    "_style_dialog_box",
    "build_meeting_datetime_edit_css",
]

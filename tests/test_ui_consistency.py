"""Cross-page geometry, rendered controls and stable theme transitions."""
from pathlib import Path

import pytest
from PyQt6.QtCore import QPoint
from PyQt6.QtGui import QColor
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QLabel, QStyle, QStyleOptionButton

from sayink.config import Config
from sayink.history_store import HistoryStore, SegmentRecord
from sayink.meeting_session import MeetingController
from sayink.ui import design_tokens as tok
from sayink.ui.main_window import MainWindow, PAGE_KEYS
from sayink.ui.settings_components import polish_preview_content, reapply_subtree, settings_group, settings_section
from sayink.ui.settings_window import SettingsWindow
from sayink.ui.theme import apply_theme


def settle():
    for _ in range(3):
        QApplication.processEvents()


def geometry(widget, relative):
    point = widget.mapTo(relative, QPoint())
    return point.x(), point.y(), widget.width(), widget.height()


@pytest.fixture(scope="module")
def window(_qapp_session, tmp_path_factory):
    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(SettingsWindow, "_rebuild_model_cards", lambda self: None)
        patcher.setattr(SettingsWindow, "_refresh_audio_device_lists", lambda self: None)
        patcher.setattr("sayink.config.default_secret_store", lambda: None)
        patcher.setattr("sayink.config.Config._sync_registry_auto_start", lambda self: None)
        directory = Path(tmp_path_factory.mktemp("consistency"))
        config = Config(config_dir=directory)
        config.set("stt.models_dir", str(directory / "models"))
        history = HistoryStore(db_path=directory / "history.db")
        history.enqueue(SegmentRecord("dictation-example", 0, 1700000000000, raw_text="用于检查阅读行宽的示例文字。" * 100))
        controller = MeetingController(config, history, pending_segments=lambda: 0)
        record = controller.store.create_meeting(title="示例会议")
        controller.store.end_meeting(record.id)
        controller.store.save_notes(record.id, template_id="general", model="example", json_payload="",
                                    markdown="## 摘要\n" + "用于检查阅读行宽的示例文字。" * 100)
        controller.store.flush()
        win = MainWindow(config, history, meeting=controller)
        win._meeting.show_meeting(record.id)
        win.show()
        settle()
        yield win
        win.hide()
        controller.shutdown()
        history.close(timeout=2)
        config._save_timer.stop()
        win.deleteLater()


def set_theme(win, theme):
    apply_theme(mode=theme, surfaces=[win, win._settings, win._history, win._meeting])
    settle()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("size", [(960, 640), (1560, 750), (880, 580)])
def test_settings_share_column_title_and_section_edges(window, theme, size):
    set_theme(window, theme)
    window.resize(*size)
    columns, titles, typography = [], [], []
    for key in ("general", "engine", "polish", "about"):
        window.show_page(key)
        page = window._settings._pages.currentWidget()
        page.verticalScrollBar().setValue(0)
        settle()
        body = page.widget()
        title = next(w for w in page.findChildren(QLabel) if w.property("viRole") == "pageTitle")
        columns.append(geometry(body, window)[:3])
        titles.append(geometry(title, window))
        typography.append((title.font().family(), title.font().pixelSize(), title.font().weight()))
        assert page.horizontalScrollBar().maximum() == 0
        assert body.width() <= tok.CONTENT_MAX_WIDTH
    assert len(set(columns)) == len(set(titles)) == len(set(typography)) == 1


def test_workspace_search_and_split_geometry_match(window):
    set_theme(window, "dark")
    window.resize(1560, 750)
    snapshots = []
    for key, page in (("history", window._history), ("meeting", window._meeting)):
        window.show_page(key)
        settle()
        snapshots.append((geometry(page._search_edit, window), geometry(page._title_label, window)))
    assert snapshots[0][0] == snapshots[1][0]
    assert snapshots[0][1][:2] == snapshots[1][1][:2]
    assert snapshots[0][1][3] == snapshots[1][1][3]
    assert window._history._copy_btn.height() == window._meeting._copy_btn.height()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_hover_does_not_look_like_a_second_selected_page(window, theme):
    set_theme(window, theme)
    window.show_page("about")
    hovered, selected = window._nav_buttons[1], window._nav_buttons[5]
    QTest.mouseMove(hovered, QPoint(12, 12))
    settle()
    assert [PAGE_KEYS[i] for i, button in enumerate(window._nav_buttons) if button.isChecked()] == ["about"]
    hover_pixel = hovered.grab().toImage().pixelColor(8, hovered.height() // 2)
    selected_pixel = selected.grab().toImage().pixelColor(8, selected.height() // 2)
    assert hover_pixel != selected_pixel


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_transparent_hosts_preserve_input_background(window, theme):
    set_theme(window, theme)
    window.resize(960, 640)
    window.show_page("polish")
    settle()
    field = window._settings._llm_url_edit
    pixmap = field.grab()
    ratio = pixmap.devicePixelRatio()
    pixel = pixmap.toImage().pixelColor(int((field.width() - 30) * ratio), int(field.height() / 2 * ratio))
    assert pixel == QColor(tok.INPUT_BG)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_form_fields_share_height_and_keep_their_size_on_focus(window, theme):
    set_theme(window, theme)
    window.resize(960, 640)
    settings = window._settings
    for page_key, field in (("polish", settings._llm_url_edit),
                            ("general", settings._output_mode_combo),
                            ("general", settings._history_retention_days_spin)):
        window.show_page(page_key)
        page = settings._pages.currentWidget()
        page.ensureWidgetVisible(field)
        settle()
        before = field.size()
        field.setFocus()
        settle()
        assert field.height() == tok.CONTROL_FIELD_HEIGHT
        assert field.size() == before


def test_section_and_preview_typography_does_not_change_on_reapply():
    for widget in (settings_section("分组标题", settings_group()), polish_preview_content()):
        widget.show()
        settle()
        labels = widget.findChildren(QLabel)
        before = [(label.font().pixelSize(), label.font().weight(), label.sizeHint()) for label in labels]
        reapply_subtree(widget)
        settle()
        after = [(label.font().pixelSize(), label.font().weight(), label.sizeHint()) for label in labels]
        assert before == after
        widget.close()


def test_about_column_stays_put_when_expansion_adds_a_scrollbar(window):
    set_theme(window, "dark")
    window.resize(1560, 750)
    window.show_page("about")
    toggle = window._settings._about_paths_toggle
    toggle.setChecked(False)
    settle()
    page = window._settings._pages.currentWidget()
    before = geometry(page.widget(), window)[:3]
    toggle.setChecked(True)
    settle()
    assert page.verticalScrollBar().maximum() > 0
    assert geometry(page.widget(), window)[:3] == before
    toggle.setChecked(False)


def test_long_update_error_does_not_squeeze_configuration_links(window):
    set_theme(window, "dark")
    window.resize(960, 640)
    window.show_page("about")
    settings = window._settings
    settings.set_update_status("检查失败（示例）：当前无法连接更新服务，请稍后重试。" * 3, action="check")
    settle()
    for row in settings._about_summary_rows.values():
        button = row.action_button
        if button is not None:
            option = QStyleOptionButton()
            button.initStyleOption(option)
            area = button.style().subElementRect(QStyle.SubElement.SE_PushButtonContents, option, button)
            assert area.height() >= button.fontMetrics().boundingRect(button.text()).height()
    settings.set_update_status("本次启动尚未检查更新", action="check")


def test_saved_documents_share_font_and_readable_line_length(window):
    set_theme(window, "light")
    window.resize(1560, 750)
    for key, reader in (("history", window._history._details), ("meeting", window._meeting._notes_view)):
        window.show_page(key)
        settle()
        assert reader.font().pixelSize() == tok.READING_FONT_PX
        margin = reader.document().rootFrame().frameFormat().rightMargin()
        assert reader.viewport().width() - margin <= tok.READING_MAX_WIDTH


def test_resizing_an_editor_keeps_text_undo_as_the_next_action(window):
    set_theme(window, "light")
    window.show_page("history")
    history = window._history
    history._on_edit_clicked()
    reader = history._details
    original = reader.toPlainText()
    reader.insertPlainText("新增文字")
    window.resize(1400, 700)
    settle()
    reader.undo()
    assert reader.toPlainText() == original
    history._cancel_inline_edit()

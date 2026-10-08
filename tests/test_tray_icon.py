"""Tray icon activation behavior tests."""

from __future__ import annotations

import sys

import pytest
from PyQt6.QtCore import QPoint, QRect, QSize
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon

from sayink.ui.tray_icon import TrayIcon, tray_menu_top_left


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture
def tray(qapp):
    icon = TrayIcon()
    yield icon


class TestTrayMenuOpensAboveTheIcon:
    def test_bottom_tray_menu_sits_above_the_icon(self):
        menu = QSize(180, 220)
        anchor = QRect(1700, 1040, 24, 40)
        available = QRect(0, 0, 1920, 1040)

        pos = tray_menu_top_left(menu, anchor, available)

        assert pos.y() + menu.height() <= anchor.y()
        assert pos.y() >= available.top()
        assert pos.x() + menu.width() <= available.right() + 1

    def test_top_tray_menu_opens_downward(self):
        menu = QSize(180, 220)
        anchor = QRect(1700, 0, 24, 40)
        available = QRect(0, 48, 1920, 1032)

        pos = tray_menu_top_left(menu, anchor, available)

        assert pos.y() >= available.top()
        assert pos.y() + menu.height() <= available.top() + available.height()

    def test_context_menu_uses_the_upward_popup(self, tray):
        from sayink.ui.tray_icon import _UpwardContextMenu

        assert isinstance(tray.contextMenu(), _UpwardContextMenu)


class TestTrayActivation:
    def test_windows_double_click_opens_once(self, tray, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        emitted: list[object] = []
        tray.wake_island.connect(lambda: emitted.append(True))

        tray._on_activated(QSystemTrayIcon.ActivationReason.Trigger)
        tray._on_activated(QSystemTrayIcon.ActivationReason.DoubleClick)

        assert len(emitted) == 1
        assert not tray._single_click_timer.isActive()

    def test_windows_single_click_opens_after_double_click_interval(self, tray, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        emitted: list[object] = []
        tray.wake_island.connect(lambda: emitted.append(True))

        tray._on_activated(QSystemTrayIcon.ActivationReason.Trigger)
        assert emitted == []
        assert tray._single_click_timer.isActive()
        tray._single_click_timer.stop()
        tray._single_click_timer.timeout.emit()

        assert len(emitted) == 1

    def test_right_click_cancels_the_pending_single_click(self, tray, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        tray._on_activated(QSystemTrayIcon.ActivationReason.Trigger)
        assert tray._single_click_timer.isActive()

        tray._on_activated(QSystemTrayIcon.ActivationReason.Context)

        assert not tray._single_click_timer.isActive()

    def test_double_click_waits_until_the_context_menu_closes(self, tray, qapp, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        emitted: list[object] = []
        tray.wake_island.connect(lambda: emitted.append(True))
        tray._on_menu_about_to_show()

        tray._on_activated(QSystemTrayIcon.ActivationReason.DoubleClick)

        assert emitted == []
        tray._on_menu_about_to_hide()
        qapp.processEvents()
        assert emitted == [True]

    def test_non_windows_uses_single_trigger(self, tray, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        emitted: list[object] = []
        tray.wake_island.connect(lambda: emitted.append(True))

        tray._on_activated(QSystemTrayIcon.ActivationReason.Trigger)

        assert len(emitted) == 1

    def test_status_summary_is_disabled_first_menu_action_and_updates_tooltip(self, tray):
        tray.set_status_summary("就绪 · FireRedASR2")

        action = tray.contextMenu().actions()[0]
        assert action.text() == "就绪 · FireRedASR2"
        assert not action.isEnabled()
        assert "FireRedASR2" in tray.toolTip()

    def test_activity_tooltip_uses_capsule_words(self, tray):
        tray.set_status_summary("就绪")
        tray.set_activity_tooltip("listening")
        assert tray.toolTip() == "SayInk - 正在听"
        tray.set_activity_tooltip("recognizing")
        assert tray.toolTip() == "SayInk - 正在识别"
        tray.set_activity_tooltip("loading")
        assert tray.toolTip() == "SayInk - 模型载入中"
        tray.set_activity_tooltip(None)
        assert tray.toolTip() == "SayInk - 就绪"


class TestTrayMenuStyleAndGrouping:
    def test_menu_stylesheet_uses_reference_style_tokens(self, tray):
        from sayink.ui import design_tokens as t

        css = tray.contextMenu().styleSheet()
        assert t.TRAY_MENU_RADIUS == 8
        assert f"border-radius: {t.TRAY_MENU_RADIUS}px" in css
        assert "border-radius: 4px" not in css
        assert "border-radius: 12px" not in css
        assert t.TRAY_MENU_HOVER in css
        assert t.TRAY_MENU_SEPARATOR in css
        assert t.TRAY_MENU_BORDER in css
        assert f"font-size: {t.TRAY_MENU_FONT}px" in css
        assert t.TRAY_MENU_FONT == t.TYPE_FOOTNOTE
        assert f"font-size: {t.TYPE_BODY_SM}px" not in css
        assert "QMenu::item" in css
        assert f"font-size: {t.TYPE_CAPTION}px" in css
        assert "margin-left: 6px" in css
        assert t.TEXT_DIM in css
        assert "QMenu::item:disabled:selected" in css
        assert "margin: 6px 12px" in css
        assert f"padding: {t.TRAY_MENU_PAD_V}px" in css
        assert tray.contextMenu().font().pixelSize() == t.TRAY_MENU_FONT
        # No Stitch tray look leftovers
        assert "rgba(0, 80, 203" not in css

    def test_menu_groups_match_spec_order(self, tray):
        actions = tray.contextMenu().actions()
        labels = []
        for a in actions:
            if a.isSeparator():
                labels.append("---")
            else:
                labels.append(a.text())

        assert labels[0]  # status (dynamic)
        assert not actions[0].isEnabled()
        assert labels[1] == "---"
        assert labels[2] == "打开 SayInk"
        assert labels[3] == "检查更新"
        assert labels[4] == "历史"
        assert labels[5] == "切换模型"
        assert labels[6] == "开机自启"
        assert actions[6].isCheckable()
        assert labels[7] == "---"
        assert labels[8] == "退出"


def test_model_switch_keeps_the_active_name_beside_the_label(tray):
    tray.update_models(
        [
            {"id": "firered", "name": "FireRedASR2", "languages": "中/英/方言"},
            {"id": "qwen", "name": "Qwen3-ASR 0.6B", "languages": "中/英/多语种"},
            {"id": "fun", "name": "Fun-ASR-Nano", "languages": "中/英/日/方言"},
        ],
        "fun",
    )

    switch = next(a for a in tray.contextMenu().actions() if a.menu() is not None)
    assert switch.text() == "切换模型 · Fun-ASR-Nano"
    assert "  " not in switch.text()

    choices = switch.menu().actions()
    assert [a.text() for a in choices] == [
        "FireRedASR2 · 中/英/方言",
        "Qwen3-ASR 0.6B · 中/英/多语种",
        "Fun-ASR-Nano · 中/英/日/方言",
    ]
    assert [a.isChecked() for a in choices] == [False, False, True]
    assert all("  " not in a.text() for a in choices)


def test_ready_status_republishes_a_small_tray_glyph(tray, qapp):
    tray.show()
    try:
        qapp.processEvents()
        before = tray.icon().cacheKey()
        tray.set_status_summary("就绪 · Fun-ASR-Nano")
        icon = tray.icon()
        widths = {size.width() for size in icon.availableSizes()}
        assert tray.toolTip() == "SayInk - 就绪 · Fun-ASR-Nano"
        assert not icon.isNull()
        assert icon.cacheKey() != before
        assert {16, 32}.issubset(widths)
        from PyQt6.QtCore import QSize

        pixmap = icon.pixmap(QSize(16, 16), 1.0)
        assert not pixmap.isNull()
        assert pixmap.devicePixelRatio() == 1.0
        assert pixmap.width() == 16
        assert pixmap.toImage().pixelColor(8, 8).alpha() > 200
    finally:
        tray.hide()


def test_tray_glyph_matches_shell_size_and_keeps_mic_contrast(tray):
    from PyQt6.QtCore import QSize

    from sayink.ui import design_tokens as tok
    from sayink.ui.tray_icon import _windows_small_icon_px

    tok.activate("dark")
    try:
        icon = tray.icon()
        metric = _windows_small_icon_px() or 16
        pixmap = icon.pixmap(icon.actualSize(QSize(metric, metric)))
        assert pixmap.devicePixelRatio() == 1.0
        assert pixmap.width() == metric
        assert pixmap.height() == metric
        image = pixmap.toImage()
        plate = image.pixelColor(int(metric * 0.20), int(metric * 0.80))
        mic = image.pixelColor(metric // 2, int(metric * 0.28))
        assert plate.alpha() > 200
        assert mic.alpha() > 200
        assert abs(plate.lightness() - mic.lightness()) > 80
    finally:
        tok.activate("light")


def test_microphone_icon_includes_windows_small_sizes():
    from sayink.ui.tray_icon import create_microphone_icon

    icon = create_microphone_icon()
    widths = {size.width() for size in icon.availableSizes()}
    assert {16, 32}.issubset(widths)


def test_idle_mic_uses_text_token():
    import inspect

    from sayink.ui import design_tokens as tok
    from sayink.ui.tray_icon import _microphone_pixmap

    source = inspect.getsource(_microphone_pixmap)
    assert "TEXT" in source
    assert tok.ACCENT not in source.split("recording")[0]

"""Colors that are applied after startup must follow the active theme axis."""

from PyQt6.QtWidgets import QWidget

from voiceink.ui.design_tokens import activate
from voiceink.ui.hotkey_edit import HotkeyEdit
from voiceink.ui.settings_components import empty_state, reapply_subtree


def test_hotkey_capture_uses_active_dark_colors():
    activate("dark")
    edit = HotkeyEdit()
    edit._begin_capture()
    sheet = edit.styleSheet().lower()
    assert "#222528" in sheet
    assert "#f9fafb" in sheet
    assert "#f7f7f7" not in sheet
    assert "#111827" not in sheet


def test_empty_state_restyles_when_theme_changes():
    activate("light")
    root = QWidget()
    label = empty_state("尚未选择模型")
    label.setParent(root)
    assert "#667085" in label.styleSheet()

    activate("dark")
    reapply_subtree(root)
    sheet = label.styleSheet()
    assert "#9CA3AF" in sheet
    assert "#667085" not in sheet


def test_widgets_built_after_a_theme_switch_use_the_live_palette():
    """Colors bound at import time (``from design_tokens import TEXT``) go
    stale after ``activate()``; builders must read ``tok.X`` live."""
    from PyQt6.QtWidgets import QLabel

    from voiceink.ui.floating_window import _DotIndicator
    from voiceink.ui.settings_components import option_row

    activate("dark")
    dark_row = option_row("标题", "副标题")
    title = dark_row.findChildren(QLabel)[0]
    assert "#F9FAFB" in title.styleSheet()
    assert "#111827" not in title.styleSheet()
    assert _DotIndicator()._color.name().upper() == "#F87171"

    activate("light")
    light_row = option_row("标题", "副标题")
    title = light_row.findChildren(QLabel)[0]
    assert "#111827" in title.styleSheet()
    assert "#F9FAFB" not in title.styleSheet()
    assert _DotIndicator()._color.name().upper() == "#DC2626"

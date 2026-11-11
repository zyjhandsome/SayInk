"""Typed QWidget subpages for SettingsWindow."""

from sayink.ui.settings_pages.about import AboutSettingsPage, build_about_page
from sayink.ui.settings_pages.general import (
    GeneralSettingsPage,
    SectionJumpBar,
    build_general_page,
)
from sayink.ui.settings_pages.model import ModelSettingsPage, build_model_page
from sayink.ui.settings_pages.polish import PolishSettingsPage, build_polish_page

__all__ = [
    "AboutSettingsPage",
    "GeneralSettingsPage",
    "ModelSettingsPage",
    "PolishSettingsPage",
    "SectionJumpBar",
    "build_about_page",
    "build_general_page",
    "build_model_page",
    "build_polish_page",
]

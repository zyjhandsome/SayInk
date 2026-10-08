"""Settings page builders. Widgets stay attached to the SettingsWindow instance."""

from sayink.ui.settings_pages.about import build_about_page
from sayink.ui.settings_pages.general import build_general_page
from sayink.ui.settings_pages.model import build_model_page
from sayink.ui.settings_pages.polish import build_polish_page

__all__ = [
    "build_about_page",
    "build_general_page",
    "build_model_page",
    "build_polish_page",
]

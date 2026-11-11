"""Unit tests for typed QWidget settings subpages (Priority 2A architecture)."""

from __future__ import annotations

import ast
import sys
import typing
from pathlib import Path

import pytest
from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QFocusEvent
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QFrame,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from sayink.config import Config, OUTPUT_MODE_HISTORY, TRIGGER_MODE_CONTINUOUS
from sayink.ui import design_tokens as tok
from sayink.ui.settings_components import SettingsPage, ThemeModeSegment, ToggleOptionRow
from sayink.ui.settings_pages import (
    AboutSettingsPage,
    GeneralSettingsPage,
    ModelSettingsPage,
    PolishSettingsPage,
    SectionJumpBar,
    build_about_page,
    build_general_page,
    build_model_page,
    build_polish_page,
)
from sayink.ui.settings_pages.about import AboutSummaryRow
from sayink.ui.settings_window import (
    SettingsWindow,
    _ABOUT_LEGACY_NAMES,
    _GENERAL_LEGACY_NAMES,
    _LEGACY_PAGE_ATTRS,
    _MODEL_LEGACY_NAMES,
    _POLISH_LEGACY_NAMES,
)
from sayink.ui.theme import apply_theme

REPO_ROOT = Path(__file__).resolve().parents[1]
SETTINGS_PAGES_DIR = REPO_ROOT / "sayink" / "ui" / "settings_pages"
SETTINGS_WINDOW_FILE = REPO_ROOT / "sayink" / "ui" / "settings_window.py"

_INERT_WINDOW_METHODS = (
    "_rebuild_model_cards",
    "_refresh_about_info",
    "_refresh_audio_device_lists",
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


class _ReadOnlyOwnerSentinel:
    """Fails immediately if any builder attempts to inject attributes onto ``win``."""

    def __setattr__(self, name: str, value: object) -> None:
        raise AssertionError(f"Unexpected attribute injection on owner: {name}={value!r}")


def test_settings_pages_perform_zero_win_injections_or_monkeypatches():
    """AST-level guarantee: 0 ``win`` params, 0 ``win._*`` accesses, 0 ``setattr``, 0 ``_jump_buttons``."""
    py_files = sorted(SETTINGS_PAGES_DIR.glob("*.py"))
    assert {p.name for p in py_files} == {
        "__init__.py",
        "about.py",
        "general.py",
        "model.py",
        "polish.py",
    }

    for path in py_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                arg_names = [a.arg for a in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)]
                assert "win" not in arg_names, f"{path.name}:{node.lineno} still accepts untyped 'win'"
            if isinstance(node, ast.Attribute):
                if isinstance(node.value, ast.Name):
                    assert node.value.id != "win", f"{path.name}:{node.lineno} still touches win.{node.attr}"
                assert node.attr != "_jump_buttons", (
                    f"{path.name}:{node.lineno} still monkey-patches _jump_buttons"
                )
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "setattr", f"{path.name}:{node.lineno} uses dynamic setattr()"


def test_settings_window_eliminates_defensive_private_hasattr_workarounds():
    """SettingsWindow must not use ``hasattr(self, '_...')`` to probe injected widget fields."""
    tree = ast.parse(SETTINGS_WINDOW_FILE.read_text(encoding="utf-8"), filename=str(SETTINGS_WINDOW_FILE))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ("hasattr", "getattr")
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "self"
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
            and node.args[1].value.startswith("_")
        ):
            offenders.append(f"line {node.lineno}: {ast.unparse(node)}")
    assert offenders == []


def test_builders_accept_readonly_sentinel_without_mutating_it(qapp, tmp_path):
    """Compatibility ``build_*_page`` helpers return typed subpages with 0 owner mutations."""
    cfg = Config(config_dir=tmp_path / ".sayink")
    sentinel = _ReadOnlyOwnerSentinel()

    general = build_general_page(sentinel)
    model = build_model_page(sentinel)
    polish = build_polish_page(sentinel, config=cfg)
    about = build_about_page(sentinel, config=cfg)

    try:
        assert isinstance(general, GeneralSettingsPage)
        assert isinstance(model, ModelSettingsPage)
        assert isinstance(polish, PolishSettingsPage)
        assert isinstance(about, AboutSettingsPage)
        for page in (general, model, polish, about):
            assert isinstance(page, QWidget)
            assert isinstance(page, SettingsPage)

        assert isinstance(general.section_jump_bar, SectionJumpBar)
        assert isinstance(general.section_jump_bar, QWidget)
        assert not hasattr(general.section_jump_bar, "_jump_buttons")
        assert [b.text() for b in general.section_jump_bar.jump_buttons] == [
            "录音与输入",
            "识别",
            "会议默认",
            "应用与历史",
        ]
    finally:
        for page in (general, model, polish, about):
            page.deleteLater()
        cfg._save_timer.stop()


def test_all_subpage_type_annotations_carry_concrete_widget_instances(qapp, tmp_path):
    """Every declared widget/control field on all 4 subpage classes is populated on init."""
    cfg = Config(config_dir=tmp_path / ".sayink")
    general = GeneralSettingsPage()
    model = ModelSettingsPage()
    polish = PolishSettingsPage(cfg)
    about = AboutSettingsPage(cfg)

    expected_counts = (
        (GeneralSettingsPage, general, _GENERAL_LEGACY_NAMES, 77),
        (ModelSettingsPage, model, _MODEL_LEGACY_NAMES, 9),
        (PolishSettingsPage, polish, _POLISH_LEGACY_NAMES, 24),
        (AboutSettingsPage, about, _ABOUT_LEGACY_NAMES, 16),
    )
    nullable_before_card_build = {"model_hero_status", "model_cards_for_id"}
    concrete_control_types = (
        QWidget,
        QVBoxLayout,
        QButtonGroup,
        ThemeModeSegment,
        ToggleOptionRow,
        QComboBox,
        QLineEdit,
        QPlainTextEdit,
        QTextEdit,
        QPushButton,
        QRadioButton,
        QProgressBar,
        QSpinBox,
        QLabel,
        QFrame,
    )

    try:
        for cls, instance, legacy_names, expected_len in expected_counts:
            assert len(legacy_names) == expected_len
            hints = typing.get_type_hints(cls)
            for legacy in legacy_names:
                field = legacy[1:]
                assert field in cls.__annotations__, f"{cls.__name__} missing annotation for {field}"
                assert field in hints, f"{cls.__name__} unresolvable type hint for {field}"
                assert field in instance.__dict__, f"{cls.__name__}.{field} not assigned on self"
                val = getattr(instance, field)
                if field in nullable_before_card_build:
                    assert val is None
                elif isinstance(val, (list, tuple, dict, str)):
                    assert val is not None
                else:
                    assert isinstance(val, concrete_control_types) or hasattr(val, "timeout")

        assert len(about.about_summary_rows) == 6
        assert all(isinstance(r, AboutSummaryRow) for r in about.about_summary_rows.values())
    finally:
        for page in (general, model, polish, about):
            page.deleteLater()
        cfg._save_timer.stop()


def test_settings_window_holds_typed_pages_and_delegates_legacy_attrs_cleanly(
    qapp, tmp_path, monkeypatch
):
    """SettingsWindow owns typed subpages with 0 injected widget attrs in ``win.__dict__``."""
    for name in _INERT_WINDOW_METHODS:
        monkeypatch.setattr(SettingsWindow, name, lambda self: None)
    cfg = Config(config_dir=tmp_path / ".sayink")
    win = SettingsWindow(cfg)
    try:
        assert isinstance(win.general_page, GeneralSettingsPage)
        assert isinstance(win.model_page, ModelSettingsPage)
        assert isinstance(win.polish_page, PolishSettingsPage)
        assert isinstance(win.about_page, AboutSettingsPage)

        assert win._pages.widget(0) is win.general_page
        assert win._pages.widget(1) is win.model_page
        assert win._pages.widget(2) is win.polish_page
        assert win._pages.widget(3) is win.about_page

        # All 122 original injected attributes (+4 moved subpage state fields) are mapped,
        # and NONE are stored directly in SettingsWindow.__dict__.
        assert len(_LEGACY_PAGE_ATTRS) == 126
        assert set(_LEGACY_PAGE_ATTRS).isdisjoint(win.__dict__)

        for legacy_name, (page_attr, field_name) in _LEGACY_PAGE_ATTRS.items():
            subpage = getattr(win, page_attr)
            assert getattr(win, legacy_name) is getattr(subpage, field_name)

        # Legacy attribute writes delegate cleanly onto the owning subpage.
        sentinel_cards = {"test-id": object()}
        win._model_cards = sentinel_cards
        assert win.model_page.model_cards is sentinel_cards
        assert "_model_cards" not in win.__dict__
    finally:
        win.close()
        cfg._save_timer.stop()


def test_typed_subpages_config_and_theme_round_trip(qapp, tmp_path, monkeypatch):
    """Config edits through typed subpage controls persist, reload, and survive theme switches."""
    for name in _INERT_WINDOW_METHODS:
        monkeypatch.setattr(SettingsWindow, name, lambda self: None)
    monkeypatch.setattr(
        "sayink.speech_recognizer.is_model_downloaded",
        lambda _mid: False,
    )

    cfg = Config(config_dir=tmp_path / ".sayink")
    win = SettingsWindow(cfg)
    try:
        gp = win.general_page
        mp = win.model_page
        pp = win.polish_page
        ap = win.about_page

        # Mutate controls via typed subpage attributes
        gp.trigger_continuous_rb.setChecked(True)
        gp.esc_stop_row.setChecked(False)
        gp.output_mode_combo.setCurrentIndex(gp.output_mode_combo.findData(OUTPUT_MODE_HISTORY))
        gp.history_retention_days_spin.setValue(45)
        gp.history_max_entries_spin.setValue(1200)
        gp.llm_proxy_combo.setCurrentIndex(gp.llm_proxy_combo.findData("direct"))
        gp.llm_proxy_suffix_edit.setText(".corp.internal")

        gp.meeting_glossary_edit.setPlainText("SayInk\n五证=五中")
        QApplication.sendEvent(
            gp.meeting_glossary_edit,
            QFocusEvent(QEvent.Type.FocusOut, Qt.FocusReason.TabFocusReason),
        )
        qapp.processEvents()

        mp.download_source_combo.setCurrentIndex(mp.download_source_combo.findData("hf-mirror"))

        pp.llm_enable_row.setChecked(True)
        pp.llm_url_edit.setText("https://api.example.com/v1")
        pp.llm_key_edit.setText("sk-typed-test")
        pp.llm_model_edit.setText("gpt-4o-mini")
        pp.llm_prompt_edit.setPlainText("精简口语但保留数字")
        win._flush_llm_fields()

        pp.minutes_model_edit.setText("gpt-4.1")
        pp.minutes_language_combo.setCurrentIndex(pp.minutes_language_combo.findData("zh"))
        win._flush_minutes_fields()

        ap.about_auto_update_row.setChecked(False)

        assert cfg.get("audio.trigger_mode") == TRIGGER_MODE_CONTINUOUS
        assert cfg.get("audio.esc_stops_continuous") is False
        assert cfg.output_mode() == OUTPUT_MODE_HISTORY
        assert cfg.get("history.retention_days") == 45
        assert cfg.get("history.max_entries") == 1200
        assert cfg.get("llm.proxy_mode") == "direct"
        assert cfg.get("llm.proxy_bypass_suffixes") == ".corp.internal"
        assert cfg.get("stt.glossary") == [
            {"term": "SayInk"},
            {"term": "五证", "variants": ["五中"]},
        ]
        assert cfg.get("stt.download_source") == "hf-mirror"
        assert cfg.get("llm.enabled") is True
        assert cfg.get("llm.api_url") == "https://api.example.com/v1"
        assert cfg.get("llm.api_key") == "sk-typed-test"
        assert cfg.get("llm.model_name") == "gpt-4o-mini"
        assert cfg.get("llm.prompt") == "精简口语但保留数字"
        assert cfg.get("meeting.model_name") == "gpt-4.1"
        assert cfg.get("meeting.output_language") == "zh"
        assert cfg.get("update.auto_check") is False

        # Reload into a fresh SettingsWindow and verify all typed subpages reflect saved state
        win2 = SettingsWindow(cfg)
        try:
            assert win2.general_page.trigger_continuous_rb.isChecked()
            assert not win2.general_page.esc_stop_row.isChecked()
            assert win2.general_page.output_mode_combo.currentData() == OUTPUT_MODE_HISTORY
            assert win2.general_page.history_retention_days_spin.value() == 45
            assert win2.general_page.history_max_entries_spin.value() == 1200
            assert win2.general_page.llm_proxy_combo.currentData() == "direct"
            assert win2.general_page.llm_proxy_suffix_edit.text() == ".corp.internal"
            assert "五证=五中" in win2.general_page.meeting_glossary_edit.toPlainText()
            assert win2.model_page.download_source_combo.currentData() == "hf-mirror"
            assert win2.polish_page.llm_enable_row.isChecked()
            assert win2.polish_page.llm_url_edit.text() == "https://api.example.com/v1"
            assert win2.polish_page.llm_model_edit.text() == "gpt-4o-mini"
            assert win2.polish_page.llm_prompt_edit.toPlainText() == "精简口语但保留数字"
            assert win2.polish_page.minutes_model_edit.text() == "gpt-4.1"
            assert win2.polish_page.minutes_language_combo.currentData() == "zh"
            assert not win2.about_page.about_auto_update_row.isChecked()

            # Verify dark <-> light theme round-trip across all 4 subpages
            for mode in ("dark", "light"):
                apply_theme(qapp, mode=mode, surfaces=[win2])
                assert tok.INPUT_BG.lower() in win2.general_page.meeting_glossary_edit.styleSheet().lower()
                assert tok.INPUT_BG.lower() in win2.polish_page.llm_prompt_edit.styleSheet().lower()
                assert tok.ACCENT.lower() in win2.general_page.mic_level_bar.styleSheet().lower()
                assert tok.TEXT_SEC.lower() in win2.model_page.storage_summary_label.styleSheet().lower()
                assert not win2.about_page.about_paths_toggle.icon().isNull()
        finally:
            win2.close()
    finally:
        apply_theme(qapp, mode="light")
        win.close()
        cfg._save_timer.stop()

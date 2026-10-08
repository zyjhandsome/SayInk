# Island UI Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reconstruct SayInk's four UI surfaces into one Spatial Island system while keeping brand tokens, IA page order, and the transcription pipeline unchanged.

**Architecture:** Shared chrome lives in `island_chrome.py` and `design_tokens.py`. `FloatingWindow` is the capsule/expanded HUD. `SettingsWindow` and `HistoryWindow` are top-center sheets. `TrayIcon` speaks the same status words. `app.py` orchestration stays; only tooltip string maps and UI wiring change.

**Tech Stack:** Python 3.10, PyQt6, pytest, existing QSS token axis.

**Spec:** `docs/superpowers/specs/2026-09-19-island-ui-redesign-design.md`

## Global Constraints

- Do not change hotkey, recording, VAD, ASR, paste, polish, SQLite, or config key names.
- Do not restore system title-bar windows for settings/history.
- Brand stays: blue accent, cool neutrals, Segoe/YaHei resolve, light/dark.
- Radii only: control 8, card 10, sheet 28, capsule/chip/toggle pill.
- Settings page order stays 0=通用, 1=引擎, 2=润色, 3=关于.
- Capsule status words are the single vocabulary: 就绪 / 正在听 / 正在识别 / 润色中 / 模型载入中.
- Tray capture icon stays red while the listen capsule dot stays green.
- Windows: run tests with `py -3.10 -m pytest`. If `py -3.10` is missing, use `python -m pytest` on 3.10+.
- Every task's tests are part of that task. Do not skip the failing-test step.

## File map

| File | Role after this plan |
|------|----------------------|
| `sayink/ui/design_tokens.py` | `TRAY_MENU_RADIUS = 8`; float tokens remain the only color axis |
| `design-system/MASTER.md` | Float table matches `tokens_for()` |
| `sayink/ui/island_chrome.py` | Placement, flags, sheet/capsule QSS, shared close-button QSS |
| `sayink/ui/floating_window.py` | Capsule vs expanded hierarchy |
| `sayink/ui/settings_window.py` | Pill nav only; about disclosure; inline LLM test; history-limit visibility |
| `sayink/ui/settings_pages/*.py` | No `PageHero`; 通用 pill; shorter hints |
| `sayink/ui/settings_components.py` | Remove unused `SettingsSidebar` after window stops using it |
| `sayink/ui/history_window.py` | Title 历史; chips; secondary export |
| `sayink/ui/tray_icon.py` | Capsule-word activity map; radius follows token |
| `README.md` | Island status words; 设置 → 通用; 历史 window name |
| `tests/test_*.py` | Assertions updated with the visual change |

---

### Task 1: Token contract (tray radius + MASTER float)

**Files:**
- Modify: `sayink/ui/design_tokens.py` (`TRAY_MENU_RADIUS`)
- Modify: `design-system/MASTER.md` (floating overlay table)
- Modify: `tests/test_tray_icon.py` (`TRAY_MENU_RADIUS` assertion)
- Test: `tests/test_theme_resolve.py` (existing float-token test)

**Interfaces:**
- Consumes: `tokens_for(effective: str) -> dict[str, Any]`
- Produces: `TRAY_MENU_RADIUS: int = 8`. Tray QSS already interpolates this token.

- [ ] **Step 1: Write the failing assertion**

In `tests/test_tray_icon.py`, change `TestTrayMenuStyleAndGrouping.test_menu_stylesheet_uses_reference_style_tokens`:

```python
        assert t.TRAY_MENU_RADIUS == 8
        assert f"border-radius: {t.TRAY_MENU_RADIUS}px" in css
        assert "border-radius: 4px" not in css
        assert "border-radius: 12px" not in css
```

Remove the existing `assert "border-radius: 8px" not in css` line (that line forbids the new radius).

- [ ] **Step 2: Run test to verify it fails**

Run: `py -3.10 -m pytest tests/test_tray_icon.py::TestTrayMenuStyleAndGrouping::test_menu_stylesheet_uses_reference_style_tokens -q`

Expected: FAIL on `TRAY_MENU_RADIUS == 8` (still 4).

- [ ] **Step 3: Implement token + MASTER**

In `sayink/ui/design_tokens.py`:

```python
TRAY_MENU_RADIUS = 8
```

In `design-system/MASTER.md`, replace the floating overlay table so Light/Dark `Float BG` / `Float Surface` / `Float Border` / `Float Text` / `Chip BG` equal `tokens_for("light")` and `tokens_for("dark")` for `FLOAT_BG`, `FLOAT_TILE`, `FLOAT_BORDER`, `FLOAT_TEXT`, `CHIP_BG`. Copy the hex/rgba strings from `design_tokens.py` verbatim. Do not invent a third float palette.

- [ ] **Step 4: Run tests**

Run: `py -3.10 -m pytest tests/test_tray_icon.py tests/test_theme_resolve.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sayink/ui/design_tokens.py design-system/MASTER.md tests/test_tray_icon.py
git commit -m "fix(ui): align island tokens and tray menu radius"
```

---

### Task 2: Capsule and expanded island hierarchy

**Files:**
- Modify: `sayink/ui/floating_window.py`
- Modify: `sayink/ui/island_chrome.py` (optional `island_close_css()`)
- Modify: `tests/test_island_surface.py`
- Modify: `tests/test_floating_window.py` (`test_error_message_can_expand_window_height`)

**Interfaces:**
- Consumes: `island_container_css()`, `island_window_flags()`, `position_island(widget, width=None)`
- Produces: `FloatingWindow.island_mode() -> str` (`"compact"` | `"expanded"`). `_end_btn` is the only primary. `_mic_chip` / `_sys_chip` are `QLabel` (still expose `islandOn` via `setProperty`). `show_error(message: str, *, auto_dismiss_ms: int = 5000) -> None` stays; window height stays at capsule + one subtitle line.

- [ ] **Step 1: Write failing tests**

Add to `tests/test_island_surface.py` in `TestIslandMorph`:

```python
    def test_expanded_end_is_only_primary(self, win):
        from sayink.ui import design_tokens as tok

        win.show_listening()
        win.expand_live("hello")
        assert tok.ACCENT.lower() in win._end_btn.styleSheet().lower()
        assert tok.ACCENT.lower() not in win._history_btn.styleSheet().lower()
        assert tok.ACCENT.lower() not in win._settings_btn.styleSheet().lower()

    def test_source_chips_are_not_buttons(self, win):
        from PyQt6.QtWidgets import QLabel, QPushButton

        win.set_input_source("mixed")
        win.expand_live("hello")
        assert isinstance(win._mic_chip, QLabel)
        assert isinstance(win._sys_chip, QLabel)
        assert not isinstance(win._mic_chip, QPushButton)
        assert win._mic_chip.property("islandOn") is True

    def test_header_click_collapses_expanded_listen(self, win):
        win.show_listening()
        win.expand_live("hello")
        assert win.island_mode() == "expanded"
        win.collapse_live()
        assert win.island_mode() == "compact"
        assert win._listening_active is True
```

In `tests/test_floating_window.py`, replace `test_error_message_can_expand_window_height`:

```python
    def test_error_stays_capsule_height(self, win):
        win.show_error("识别失败：" + "请检查网络或模型配置。" * 12)

        from sayink.ui.floating_window import COMPACT_HEIGHT

        assert win.height() <= COMPACT_HEIGHT + 40
        assert win.toolTip()
```

- [ ] **Step 2: Run new tests to verify they fail**

Run: `py -3.10 -m pytest tests/test_island_surface.py::TestIslandMorph::test_expanded_end_is_only_primary tests/test_island_surface.py::TestIslandMorph::test_source_chips_are_not_buttons tests/test_island_surface.py::TestIslandMorph::test_header_click_collapses_expanded_listen tests/test_floating_window.py::TestModelLoadingGuard::test_error_stays_capsule_height -q`

Expected: FAIL (missing `collapse_live`, chips still `QPushButton`, error still grows).

- [ ] **Step 3: Implement floating window**

In `floating_window.py`:

1. Build `_mic_chip` / `_sys_chip` as `QLabel("麦克风")` / `QLabel("电脑播放")`. Keep `setProperty("islandOn", ...)`. Paint with chip QSS (on: `ISLAND_MINT` fill; off: border + `FLOAT_TEXT_SEC`). Do not call `setEnabled(False)` on a button.

2. In `reapply_theme`, style `_end_btn` with accent fill + `ACCENT_ON_DARK` text; style `_history_btn` and `_settings_btn` with `CHIP_BG` (same ghost as today).

3. Add:

```python
    def collapse_live(self) -> None:
        if not self._listening_active:
            return
        self._text_label.hide()
        self._set_mode("compact")
        self._present()
```

4. In `mousePressEvent`, if `_listening_active` and `_mode == "expanded"` and the click is on `_status_label` or `_dot` or `_waveform`, call `collapse_live()`. Keep existing compact-click → `expand_live`.

5. `show_error`: `_set_mode("compact")`; status = first line before `：` or the first 12 chars; subtitle = elided remainder on `_text_label`; `setToolTip(message)`; `setFixedHeight(COMPACT_HEIGHT)` (allow +24 only if subtitle is visible). Do not compute height from `len(display) // 34`.

6. `show_listening`: do not put a hidden helper sentence on `_text_label`. Keep the label hidden in compact listen.

- [ ] **Step 4: Run island + float tests**

Run: `py -3.10 -m pytest tests/test_island_surface.py tests/test_floating_window.py -q`

Expected: PASS. Existing emit tests for 结束/历史/设置 still pass.

- [ ] **Step 5: Commit**

```bash
git add sayink/ui/floating_window.py sayink/ui/island_chrome.py tests/test_island_surface.py tests/test_floating_window.py
git commit -m "feat(ui): give the expanded island a single primary action"
```

---

### Task 3: Settings chrome (pills, no sidebar, no page heroes)

**Files:**
- Modify: `sayink/ui/settings_window.py`
- Modify: `sayink/ui/settings_pages/general.py`
- Modify: `sayink/ui/settings_pages/model.py`
- Modify: `sayink/ui/settings_pages/polish.py`
- Modify: `sayink/ui/settings_pages/about.py`
- Modify: `tests/test_settings_general.py`
- Modify: `tests/test_ui_styles.py` (`test_sidebar_brand_and_nav_metrics`)

**Interfaces:**
- Consumes: `_on_island_nav(self, row: int) -> None`, page stack order 0..3
- Produces: `_island_nav[0].text() == "通用"`. No `_sidebar` attribute. No `_general_hero` / `_model_hero` / `_polish_hero` / `_about_hero` widgets on the pages. `_open_about_from_general` calls `_on_island_nav(3)`.

- [ ] **Step 1: Write failing tests**

Replace `test_header_title_matches_reference` and the hero lines in `test_general_copy_matches_prototype_v3` in `tests/test_settings_general.py`:

```python
    def test_island_nav_labels(self, settings_window):
        assert [b.text() for b in settings_window._island_nav] == [
            "通用", "引擎", "润色", "关于",
        ]
        assert not hasattr(settings_window, "_sidebar")
        assert not hasattr(settings_window, "_general_hero")
```

In `tests/test_ui_styles.py` class `TestSidebarVisualContracts`, replace `test_sidebar_brand_and_nav_metrics` with:

```python
    def test_settings_island_nav_has_no_sidebar(self, tmp_path, monkeypatch):
        import sys
        from PyQt6.QtWidgets import QApplication
        from sayink.config import Config
        from sayink.ui.settings_window import SettingsWindow

        QApplication.instance() or QApplication(sys.argv)
        monkeypatch.setattr(SettingsWindow, "_rebuild_model_cards", lambda self: None)
        monkeypatch.setattr(SettingsWindow, "_refresh_about_info", lambda self: None)
        monkeypatch.setattr(SettingsWindow, "_refresh_audio_device_lists", lambda self: None)
        win = SettingsWindow(Config(config_dir=tmp_path))
        try:
            assert not hasattr(win, "_sidebar")
            assert [b.text() for b in win._island_nav] == ["通用", "引擎", "润色", "关于"]
        finally:
            win.close()
```

If `settings_components.SettingsSidebar` is deleted in this task, also delete imports of `SettingsSidebar` / `NAV_BTN_STYLE` sidebar checks in `test_ui_styles.py`.

- [ ] **Step 2: Run to verify fail**

Run: `py -3.10 -m pytest tests/test_settings_general.py::TestGeneralPageLayout::test_island_nav_labels tests/test_ui_styles.py::TestSidebarVisualContracts::test_settings_island_nav_has_no_sidebar -q`

Expected: FAIL (`_island_nav[0]` is still `录音`, `_sidebar` still exists).

- [ ] **Step 3: Implement chrome**

1. In `settings_window.py` `_setup_ui`, change the pill tuple to `("通用", "引擎", "润色", "关于")`.
2. Delete `self._sidebar = SettingsSidebar(...)` and `self._sidebar.hide()`. Delete `from sayink.ui.settings_components import SettingsSidebar` if unused.
3. `_on_island_nav` only calls `_on_nav_changed(row)`.
4. `_open_about_from_general`: `self._on_island_nav(3)` only.
5. Remove the `for hero_name in (...)` reapply loop if heroes are gone.
6. In `general.py` / `model.py` / `polish.py` / `about.py`, remove `PageHero(...)` construction and `page.add(win._*hero)`. Leave section titles (`settings_section("录音" | "音频" | "偏好" | ...)`).
7. Stub or delete `_refresh_about_hero_status` / `_refresh_model_hero_status` / `_refresh_all_heroes` if they only update removed heroes. Keep `_refresh_about_info` (Task 4 will reshape it).
8. Delete class `SettingsSidebar` from `settings_components.py` only after no remaining imports.

- [ ] **Step 4: Run settings tests**

Run: `py -3.10 -m pytest tests/test_settings_general.py tests/test_ui_styles.py tests/test_theme_resolve.py -q`

Expected: PASS. `test_general_stacks_three_sections_top_to_bottom` still finds 录音 / 音频 / 偏好 section titles.

- [ ] **Step 5: Commit**

```bash
git add sayink/ui/settings_window.py sayink/ui/settings_pages sayink/ui/settings_components.py tests/test_settings_general.py tests/test_ui_styles.py
git commit -m "feat(ui): use island pills for settings and drop the sidebar"
```

---

### Task 4: Settings content (hints, history limits, about, polish test)

**Files:**
- Modify: `sayink/ui/settings_pages/general.py`
- Modify: `sayink/ui/settings_pages/about.py`
- Modify: `sayink/ui/settings_window.py` (`_refresh_about_info`, `_on_history_enabled_toggled`, `_sync_source_device_widgets`, `_test_llm`, `_on_test_done`)
- Test: `tests/test_settings_general.py` (extend)

**Interfaces:**
- Consumes: `win._history_enabled_row.isChecked() -> bool`, `win._src_mixed_rb.isChecked() -> bool`
- Produces: `_set_history_limit_rows_visible(self, visible: bool) -> None`. `_about_paths_wrap` collapsed by default. `_llm_test_status` `QLabel` under the model field. `_hotkey_hint` is one sentence.

- [ ] **Step 1: Write failing tests**

Add to `tests/test_settings_general.py`:

```python
    def test_history_limit_rows_follow_toggle(self, settings_window):
        settings_window._history_enabled_row.setChecked(False)
        settings_window._set_history_limit_rows_visible(
            settings_window._history_enabled_row.isChecked()
        )
        assert not settings_window._history_retention_row.isVisible()
        assert not settings_window._history_max_entries_row.isVisible()
        settings_window._history_enabled_row.setChecked(True)
        settings_window._set_history_limit_rows_visible(True)
        assert settings_window._history_retention_row.isVisible()

    def test_mixed_callout_only_when_mixed(self, settings_window):
        settings_window._src_mic_rb.setChecked(True)
        settings_window._sync_source_device_widgets()
        assert not settings_window._mixed_audio_callout.isVisible()
        settings_window._src_mixed_rb.setChecked(True)
        settings_window._sync_source_device_widgets()
        assert settings_window._mixed_audio_callout.isVisible()

    def test_hotkey_hint_is_one_sentence(self, settings_window):
        text = settings_window._hotkey_hint.text()
        assert "0.30" in text
        assert text.count("。") <= 2
        assert "浮窗" not in text
```

- [ ] **Step 2: Run to verify fail**

Run: `py -3.10 -m pytest tests/test_settings_general.py::TestGeneralPageLayout::test_history_limit_rows_follow_toggle tests/test_settings_general.py::TestGeneralPageLayout::test_mixed_callout_only_when_mixed tests/test_settings_general.py::TestGeneralPageLayout::test_hotkey_hint_is_one_sentence -q`

Expected: FAIL (`_set_history_limit_rows_visible` missing and/or callout always visible).

- [ ] **Step 3: Implement**

1. Hotkey hint text (set wherever `_hotkey_hint` is filled, including `_load_settings` / trigger-mode refresh):

```text
持续模式按住约 0.30 秒开始，松开不结束；Esc 或结束可结束整场。
```

2. In `_sync_source_device_widgets`, `self._mixed_audio_callout.setVisible(self._src_mixed_rb.isChecked())`. Also hide the callout wrapper if it has extra padding.

3. Add `_set_history_limit_rows_visible(self, visible: bool)` that sets both spin rows visible. Call it from `_load_settings` and `_on_history_enabled_toggled`.

4. About: keep SayInk + version. In `_refresh_about_info`, append only 当前模型 / 快捷键 / 润色. Put 已下载 / 模型目录 / 配置文件 inside a `QWidget` `#aboutPaths` under a checkable `QPushButton`「文件位置」(objectName `aboutPathsToggle`), default unchecked/hidden.

5. Polish: add `win._llm_test_status = QLabel("")` under the model row. `_test_llm` incomplete fields → set that label to「请填写完整的接口信息。」and return (no `QMessageBox`). `_on_test_done`: success「连接正常，可以使用。」; failure `w.msg`. Keep `QMessageBox` only if some other caller still needs it; this path must be inline.

- [ ] **Step 4: Run**

Run: `py -3.10 -m pytest tests/test_settings_general.py tests/test_theme_resolve.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sayink/ui/settings_pages/general.py sayink/ui/settings_pages/about.py sayink/ui/settings_window.py tests/test_settings_general.py
git commit -m "feat(ui): tighten settings hierarchy and inline polish test"
```

---

### Task 5: History island

**Files:**
- Modify: `sayink/ui/history_window.py`
- Modify: `tests/test_history_window.py`
- Modify: `tests/test_island_surface.py` (`TestHistoryTimeStream`)
- Modify: `tests/test_theme_resolve.py` (title still exists; do not require 22px)

**Interfaces:**
- Consumes: `FakeHistoryStore` in `tests/test_history_window.py`
- Produces: `_title_label.text() == "历史"`. Search placeholder `搜索转写内容`. `_export_btn` uses `BTN_GHOST` / `BTN_GHOST_SM` (not `BTN_PRIMARY`). `_detail_chips` is a `QHBoxLayout` host. `_details` body has no `来源：` run-on line. `_copy_polished_btn` disabled when the selected session has no polished text.

- [ ] **Step 1: Write failing tests**

Add to `tests/test_history_window.py`:

```python
def test_history_chrome_copy(qapp):
    window = HistoryWindow(FakeHistoryStore())
    try:
        assert window._title_label.text() == "历史"
        assert window._search_edit.placeholderText() == "搜索转写内容"
        from sayink.ui import settings_styles as ss
        from sayink.ui import design_tokens as tok
        assert tok.ACCENT.lower() not in window._export_btn.styleSheet().lower() or (
            ss.BTN_PRIMARY not in (window._export_btn.styleSheet(),)
        )
    finally:
        window.close()


def test_detail_uses_chips_not_runon_meta(qapp):
    window = HistoryWindow(FakeHistoryStore())
    try:
        window._session_list.setCurrentRow(0)
        window._expand_session(window._session_list.item(0))
        body = window._details.toPlainText()
        assert "来源：" not in body
        assert "polished first" in body
        chips = [c.text() for c in window._detail_chip_labels]
        assert any("混合" in t for t in chips)
        assert any("持续" in t or "持续转写" in t for t in chips)
    finally:
        window.close()
```

Update `test_double_click_expands_session_segments` and `test_legacy_file_import_history_labels_are_preserved` so metadata assertions read chips (`window._detail_chip_labels`) instead of `来源：` inside `_details`. Body still contains `file raw text` / `polished first`.

- [ ] **Step 2: Run to verify fail**

Run: `py -3.10 -m pytest tests/test_history_window.py::test_history_chrome_copy tests/test_history_window.py::test_detail_uses_chips_not_runon_meta -q`

Expected: FAIL (title still 过去的话).

- [ ] **Step 3: Implement**

1. Title `历史`. Search placeholder `搜索转写内容`.
2. Title stylesheet: `TYPE_TITLE` (16), weight 600. Not `TYPE_DISPLAY` / 700.
3. `_export_btn.setStyleSheet(ss.BTN_GHOST_SM)` in `_paint_history_styles`.
4. Row chips: three `QLabel`s — source, `target_app` or skip if empty, `{n} 段`. Do not weld source and exe into one chip.
5. Add `self._detail_chip_host` + `self._detail_chip_labels: list`. `_expand_session` clears and rebuilds chips for source / trigger / model; `_details` only gets transcript blocks. Labels: **原文** / **润色** when both differ; otherwise unlabeled body. Legacy file: chips `文件转写` and `导入文件`.
6. Empty list copy: `还没有会话。完成一次转写后会出现在这里。` Search empty: `没有匹配的转写。`
7. After selection change, disable `_copy_polished_btn` when no polished text exists on that session.

- [ ] **Step 4: Run**

Run: `py -3.10 -m pytest tests/test_history_window.py tests/test_island_surface.py::TestHistoryTimeStream tests/test_theme_resolve.py::TestSurfaceThemeReapply::test_history_reapply_keeps_construct_visual_language -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sayink/ui/history_window.py tests/test_history_window.py tests/test_island_surface.py tests/test_theme_resolve.py
git commit -m "feat(ui): rebuild history as a readable island sheet"
```

---

### Task 6: Tray words, README, regression

**Files:**
- Modify: `sayink/ui/tray_icon.py` (`set_activity_tooltip` map only)
- Modify: `README.md` (status words + 历史窗名)
- Modify: `tests/test_readme_features.py` only if it snapshots the changed sentences
- Test: `tests/test_tray_icon.py`

**Interfaces:**
- Consumes: `TrayIcon.set_activity_tooltip(self, state: str | None) -> None` keys: `recording`, `recognizing`, `polishing`, `listening`, `loading`
- Produces: tooltip strings `SayInk - 录音中` / `SayInk - 正在识别` / `SayInk - 润色中` / `SayInk - 正在听` / `SayInk - 模型载入中`. Idle tooltip unchanged (`SayInk - {status_summary}`). Do not change `app.py` call sites.

- [ ] **Step 1: Write failing test**

Add to `tests/test_tray_icon.py`:

```python
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
```

- [ ] **Step 2: Run to verify fail**

Run: `py -3.10 -m pytest tests/test_tray_icon.py::TestTrayActivation::test_activity_tooltip_uses_capsule_words -q`

Expected: FAIL (`监听中` / `识别中` / `模型加载中` still present).

- [ ] **Step 3: Implement map + README**

In `tray_icon.py`:

```python
        lines = {
            "recording": "录音中",
            "recognizing": "正在识别",
            "polishing": "润色中",
            "listening": "正在听",
            "loading": "模型载入中",
        }
```

README user-facing edits (keep behavior facts):

- 功能一览空间岛句：托盘与胶囊使用 模型载入中 / 正在听 / 正在识别 / 润色中 / 已复制，不要写「监听」当另一种状态名。
- 历史窗名写 **历史**，不要当窗名写「过去的话」。
- 设置入口保持 **设置 → 通用**。

If `tests/test_readme_features.py` asserts old phrases, update those strings to the new sentences.

- [ ] **Step 4: Full UI regression**

Run:

```
py -3.10 -m pytest tests/test_readme_features.py tests/test_theme_resolve.py tests/test_ui_styles.py tests/test_floating_window.py tests/test_island_surface.py tests/test_tray_icon.py tests/test_history_window.py tests/test_settings_general.py -q
```

Expected: PASS.

Manual smoke (do not skip when executing): start app → wait 模型载入中 → continuous listen + text → 结束 → open 设置/历史 → switch 浅色/暗色. Confirm four surfaces restyle without restart.

- [ ] **Step 5: Commit**

```bash
git add sayink/ui/tray_icon.py README.md tests/test_tray_icon.py tests/test_readme_features.py
git commit -m "fix(ui): use capsule status words in the tray and README"
```

---

## Self-review

**Spec coverage**

| Spec section | Task |
|--------------|------|
| Visual language / radii / MASTER float | 1 |
| Capsule / expanded / error height / source chips | 2 |
| Settings pills 通用, no sidebar, no PageHero | 3 |
| Hotkey hint, mixed callout, history spins, about paths, inline LLM test | 4 |
| History title, chips, export secondary, empty copy | 5 |
| Tray words, README, acceptance suite | 6 |
| Engine card chips / polish-off collapse | 4 (polish-off already hides `_llm_container`; do not reopen that unless `_on_llm_enable_toggled` broke) |
| Engine featured-card chips | Optional polish inside Task 3/4 if `ModelCard` already shows ratings; do not redesign `ModelCard` internals unless a test in Task 4 requires capability chips |

Engine "capability chips instead of dotted run-on" is satisfied if `ModelCard` already renders rating chips. Do not start a seventh task for a visual nicety on the hero subtitle. If `_refresh_active_model_hero` still writes a `·` run-on into a removed `PageHero`, delete that write in Task 3.

**Placeholders:** none.

**Type consistency:** `collapse_live()` and `_set_history_limit_rows_visible(visible: bool)` are defined in the task that first tests them. Chip widgets stay `_mic_chip` / `_sys_chip` with `islandOn`. Tray keys are unchanged strings.

# Codex-style Main Window Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace SayInk's spatial-island settings/history chrome with one Codex-quiet main window (160px flush sidebar + stacked pages) and a thin listen bar, without changing the transcription pipeline.

**Architecture:** New `MainWindow` owns sidebar navigation and a two-slot stack (history host + settings host). `SettingsWindow` and `HistoryWindow` become embeddable `QWidget`s (no island flags). `FloatingWindow` shrinks to a listen bar. `app.py` opens `MainWindow` instead of two island dialogs. Tokens switch to an ink axis (`#181818` / `#FFFFFF`); primary buttons use ink fill, not blue.

**Tech Stack:** Python 3.10, PyQt6, pytest, existing QSS token axis in `design_tokens.py`.

**Spec:** `docs/superpowers/specs/2026-09-21-codex-main-window-design.md`

## Global Constraints

- Do not change hotkey, recording, VAD, ASR, paste, polish, SQLite, or config key names.
- Do not restore system title-bar windows; MainWindow is frameless with a 36px caption. Close hides to tray.
- Almost no chromatic accent. Green / red / amber are semantic only (listen / fail / warn).
- Primary button is ink fill: dark `#FFFFFF` on `#0D0D0D` text; light `#0D0D0D` on `#FFFFFF` text.
- Sidebar width is 160px. Outer radius 12px. Flush chrome, hairline divider only.
- Nav order is 历史 / 通用 / 引擎 / 润色 / 关于.
- Status words stay: 就绪 / 正在听 / 正在识别 / 润色中 / 模型载入中.
- Tray capture icon stays red while the listen-bar dot stays green.
- Windows: run tests with `py -3.10 -m pytest`. If `py -3.10` is missing, use `python -m pytest` on 3.10+.
- Every task's tests are part of that task. Do not skip the failing-test step.
- Do not execute `docs/superpowers/plans/2026-09-19-island-ui-redesign.md`.

## File map

| File | Role after this plan |
|------|----------------------|
| `sayink/ui/design_tokens.py` | Ink surfaces; `PRIMARY_ON`; `SIDEBAR_WIDTH=160`; `STATE_LISTEN` follows `GREEN` |
| `sayink/ui/settings_styles.py` | `BTN_PRIMARY` / accent-sm use `PRIMARY_ON`, not hardcoded white |
| `design-system/MASTER.md` | Surfaces and float table match `tokens_for()` |
| `sayink/ui/main_window.py` | Frameless 960×640 shell; 160px nav; hide-on-close |
| `sayink/ui/settings_window.py` | `QWidget` pages host; no island header / flags / `position_island` |
| `sayink/ui/history_window.py` | `QWidget`; no island sheet / close-x; left/right split stays |
| `sayink/ui/floating_window.py` | Listen bar 40px / excerpt 64px; one primary 「结束」 |
| `sayink/ui/island_chrome.py` | Listen-bar placement only (or unused by settings/history) |
| `sayink/ui/tray_icon.py` | Menu 「打开 SayInk」; ready icon uses `TEXT`, not `ACCENT` |
| `sayink/app.py` | Construct `MainWindow`; tray/double-click show it; `_wake_island` removed |
| `README.md` | Main window, listen bar, 「打开 SayInk」 |
| `tests/test_*.py` | Assertions follow the new chrome |

---

### Task 1: Ink token axis

**Files:**
- Modify: `sayink/ui/design_tokens.py`
- Modify: `sayink/ui/settings_styles.py` (`build_btn_primary`, `build_btn_accent_sm`)
- Modify: `design-system/MASTER.md` (color tables)
- Modify: `tests/test_ui_styles.py` (`TestClassicDesktopTokens`, contrast tests that assume old `#111827` / `#2563EB` primary)
- Modify: `tests/test_theme_resolve.py` only if it asserts old `BG` / `SURFACE` hex

**Interfaces:**
- Consumes: `tokens_for(effective: str) -> dict[str, Any]`; `activate(effective: str) -> None`
- Produces: `_LIGHT`/`_DARK` `BG`/`NAV_BG`/`SURFACE`/`SETTINGS_SIDEBAR_BG` = `#FFFFFF` / `#181818`. `PRIMARY_CONTAINER` light `#0D0D0D`, dark `#FFFFFF`. New key `PRIMARY_ON` light `#FFFFFF`, dark `#0D0D0D`. `SIDEBAR_WIDTH = 160`. `activate()` sets `STATE_LISTEN` from `GREEN`, not `ISLAND_MINT`. `NAV_SELECTED_BG` / `ROW_SELECTED` = `rgba(ink, 0.06)`.

- [ ] **Step 1: Write the failing token tests**

In `tests/test_ui_styles.py`, replace `TestClassicDesktopTokens.test_accent_and_surfaces` surface asserts (keep `ACCENT` as `#2563EB` for the leftover focus ring):

```python
def test_ink_surfaces_and_primary(self):
    from sayink.ui import design_tokens as t
    from sayink.ui.design_tokens import tokens_for

    light = tokens_for("light")
    dark = tokens_for("dark")
    assert light["BG"] == light["SURFACE"] == light["NAV_BG"] == "#FFFFFF"
    assert dark["BG"] == dark["SURFACE"] == dark["NAV_BG"] == "#181818"
    assert light["PRIMARY_CONTAINER"] == "#0D0D0D"
    assert light["PRIMARY_ON"] == "#FFFFFF"
    assert dark["PRIMARY_CONTAINER"] == "#FFFFFF"
    assert dark["PRIMARY_ON"] == "#0D0D0D"
    assert t.SIDEBAR_WIDTH == 160
    t.activate("dark")
    assert t.STATE_LISTEN == t.GREEN
    t.activate("light")
    assert t.STATE_LISTEN == t.GREEN
```

Also change `build_btn_primary` tests that require `color: white` on a blue container: after this task, primary text is `PRIMARY_ON`.

- [ ] **Step 2: Run to verify fail**

Run: `py -3.10 -m pytest tests/test_ui_styles.py::TestClassicDesktopTokens -q`

Expected: FAIL (`PRIMARY_ON` missing and/or `BG` still `#F3F4F6` / `#111827`).

- [ ] **Step 3: Implement tokens + button text**

In `_LIGHT` set `BG`, `NAV_BG`, `SURFACE`, `SURFACE_PEARL`, `SETTINGS_SIDEBAR_BG` to `#FFFFFF` / `#F7F7F7` only for pearl rows if needed; spec says flush `#FFFFFF` — use `#FFFFFF` for `BG`/`NAV_BG`/`SURFACE`/`SETTINGS_SIDEBAR_BG`. `HAIRLINE`/`BORDER` = `rgba(13,13,13,0.08)`. `NAV_SELECTED_BG` = `rgba(13,13,13,0.06)`. `ROW_SELECTED` = `rgba(13,13,13,0.06)`. `PRIMARY_CONTAINER` = `#0D0D0D`. `PRIMARY_CONTAINER_HOVER` = `#262626`. `PRIMARY_CONTAINER_PRESSED` = `#404040`. `PRIMARY_ON` = `#FFFFFF`.

In `_DARK` set those surfaces to `#181818`. `HAIRLINE`/`BORDER` = `rgba(255,255,255,0.08)`. `NAV_SELECTED_BG` / `ROW_SELECTED` = `rgba(255,255,255,0.06)`. `PRIMARY_CONTAINER` = `#FFFFFF`. hover `#E8E8E8`. pressed `#D0D0D0`. `PRIMARY_ON` = `#0D0D0D`. `FLOAT_BG` = `rgba(24,24,24,236)`. `FLOAT_TILE` = `#181818`.

`SIDEBAR_WIDTH = 160`.

In `activate()`:

```python
g["STATE_LISTEN"] = vals["GREEN"]
```

In `settings_styles.py` `build_btn_primary` and `build_btn_accent_sm`, replace `color: white` with `color: {t.PRIMARY_ON}`.

Update `design-system/MASTER.md` light/dark tables to the same hex values.

Fix contrast tests in `test_ui_styles.py` that compute against old `#111827` selected backgrounds so they use the new surfaces (AA must still pass).

- [ ] **Step 4: Run tests**

Run: `py -3.10 -m pytest tests/test_ui_styles.py tests/test_theme_resolve.py -q`

Expected: PASS. If a leftover assert still names `#2563EB` as `PRIMARY_CONTAINER`, update that assert in this task (accent may remain `#2563EB`).

- [ ] **Step 5: Commit**

```bash
git add sayink/ui/design_tokens.py sayink/ui/settings_styles.py design-system/MASTER.md tests/test_ui_styles.py tests/test_theme_resolve.py
git commit -m "feat(ui): switch tokens to ink surfaces and ink primary"
```

---

### Task 2: MainWindow chrome

**Files:**
- Create: `sayink/ui/main_window.py`
- Create: `tests/test_main_window.py`
- Modify: `sayink/ui/__init__.py` only if the package re-exports windows

**Interfaces:**
- Consumes: `design_tokens.SIDEBAR_WIDTH`, `TYPE_BODY_SM`, `BG`, `TEXT`, `TEXT_SEC`, `NAV_SELECTED_BG`, `HAIRLINE`
- Produces:

```python
NAV_LABELS = ("历史", "通用", "引擎", "润色", "关于")

class MainWindow(QWidget):
    def __init__(self, parent=None) -> None: ...
    def show_page(self, key: str) -> None:  # key in {"history","general","engine","polish","about"}
    def current_page(self) -> str: ...
    def closeEvent(self, event) -> None:  # ignore + hide
```

Stack for this task may be empty `QStackedWidget` placeholders (five blank pages). Settings/history widgets land in Task 3.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_main_window.py
import sys
import pytest
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt

@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication(sys.argv)

def test_chrome_size_and_nav(qapp):
    from sayink.ui.main_window import NAV_LABELS, MainWindow
    from sayink.ui import design_tokens as tok
    win = MainWindow()
    try:
        assert win.width() == 960
        assert win.height() == 640
        assert [b.text() for b in win._nav_buttons] == list(NAV_LABELS)
        assert win._sidebar.width() == tok.SIDEBAR_WIDTH
        assert win.windowFlags() & Qt.WindowType.FramelessWindowHint
        assert win.current_page() == "general"
        win.show_page("history")
        assert win.current_page() == "history"
    finally:
        win.close()

def test_close_hides_does_not_quit(qapp):
    from sayink.ui.main_window import MainWindow
    win = MainWindow()
    win.show()
    win.close()
    assert win.isHidden()
    win.deleteLater()
```

- [ ] **Step 2: Run to verify fail**

Run: `py -3.10 -m pytest tests/test_main_window.py -q`

Expected: FAIL (`ModuleNotFoundError: sayink.ui.main_window`).

- [ ] **Step 3: Implement chrome**

Create `sayink/ui/main_window.py`:

- `QWidget`, frameless, translucent off, background `tok.BG`.
- Caption 36px: 8px ink dot, label `SayInk`, min / max / close. Close calls `hide()`.
- `closeEvent`: `event.ignore(); self.hide()`.
- Left `QWidget` objectName `mainSidebar`, fixed width `tok.SIDEBAR_WIDTH`, right-border `1px solid {tok.HAIRLINE}`.
- Five checkable `QPushButton`s, 29px, 12px font, exclusive. Click maps to `show_page`.
- `QStackedWidget` `_stack` with five placeholder `QWidget`s named `pageHistory` … `pageAbout` for now.
- `resize(960, 640)`.
- `reapply_theme()` paints caption, sidebar, nav checked = `NAV_SELECTED_BG`.

Do not construct `SettingsWindow` yet.

- [ ] **Step 4: Run tests**

Run: `py -3.10 -m pytest tests/test_main_window.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sayink/ui/main_window.py tests/test_main_window.py
git commit -m "feat(ui): add frameless main window chrome with flush sidebar"
```

---

### Task 3: Embed settings and history

**Files:**
- Modify: `sayink/ui/settings_window.py` (base class, `_setup_window`, `_setup_ui` header, `showEvent`)
- Modify: `sayink/ui/history_window.py` (base class, island sheet, close-x)
- Modify: `sayink/ui/main_window.py` (accept `config` + `history_store`, embed widgets)
- Modify: `tests/test_settings_general.py` (`test_island_nav_labels` → no `_island_nav`)
- Modify: `tests/test_ui_styles.py` (`test_settings_island_nav_has_no_sidebar`)
- Modify: `tests/test_island_surface.py` (settings island asserts)
- Modify: `tests/test_theme_resolve.py` (island title / `islandSheet` if asserted)
- Modify: `tests/test_main_window.py` (page switch shows embedded widgets)

**Interfaces:**
- Consumes: `SettingsWindow(config, parent=None)`, `HistoryWindow(store, parent=None)` — same constructors
- Produces: both are `QWidget`. `SettingsWindow.show_page(index: int)` with 0=通用,1=引擎,2=润色,3=关于. No `_island_nav`, no `_island_title`, no `_sheet` island. `MainWindow.__init__(self, config, history_store, parent=None)`. `show_page("general")` shows settings and `settings.show_page(0)`.

- [ ] **Step 1: Write failing embed tests**

Add to `tests/test_main_window.py`:

```python
def test_show_page_embeds_hosts(qapp, tmp_path, monkeypatch):
    from sayink.config import Config
    from sayink.history_store import HistoryStore
    from sayink.ui.main_window import MainWindow
    from sayink.ui.settings_window import SettingsWindow
    monkeypatch.setattr(SettingsWindow, "_rebuild_model_cards", lambda self: None)
    monkeypatch.setattr(SettingsWindow, "_refresh_about_info", lambda self: None)
    monkeypatch.setattr(SettingsWindow, "_refresh_audio_device_lists", lambda self: None)
    store = HistoryStore(tmp_path / "h.db")
    win = MainWindow(Config(config_dir=tmp_path), store)
    try:
        assert not hasattr(win._settings, "_island_nav")
        win.show_page("engine")
        assert win._settings._pages.currentIndex() == 1
        win.show_page("history")
        assert win._stack.currentWidget() is win._history
        assert win._history._title_label.text() == "历史"
        assert not hasattr(win._history, "_close_btn")
    finally:
        win.close()
        store.close()
```

Change `tests/test_settings_general.py` `test_island_nav_labels` to assert `SettingsWindow` is a `QWidget` and `_pages.count() == 4`. Delete asserts on `_island_nav`.

- [ ] **Step 2: Run to verify fail**

Run: `py -3.10 -m pytest tests/test_main_window.py::test_show_page_embeds_hosts tests/test_settings_general.py::TestSettingsGeneral::test_island_nav_labels -q`

Expected: FAIL (`MainWindow.__init__` does not take config/store, or `_island_nav` still exists).

- [ ] **Step 3: Implement embed**

`SettingsWindow`: change `QDialog` → `QWidget`. Remove `apply_island_sheet_flags`, `position_island` in `showEvent`, header title, `_island_nav` pills, `_close_btn`. Keep `_pages` + `build_*_page`. Add `show_page(self, index: int)`. If `app.py` still connects `finished`, add `closed = pyqtSignal()` and emit from `hideEvent` only when used standalone; MainWindow will not rely on it.

`HistoryWindow`: `QDialog` → `QWidget`. Remove island `_sheet` wrapper, `_close_btn`, `position_island`. Keep search, list, detail, export. Put list and detail in a horizontal split (`_left_pane` | `_right_pane`) if not already side by side.

`MainWindow.__init__(self, config, history_store, parent=None)`:
- `self._settings = SettingsWindow(config, self)`
- `self._history = HistoryWindow(history_store, self)`
- stack page 0 = history; page 1 = settings
- `show_page` mapping: history→stack 0; general/engine/polish/about→stack 1 + settings index 0/1/2/3

Update existing settings tests that poke `_island_nav` / `_on_island_nav` to use `show_page` / `_pages.setCurrentIndex`.

- [ ] **Step 4: Run tests**

Run: `py -3.10 -m pytest tests/test_main_window.py tests/test_settings_general.py tests/test_settings_history.py tests/test_history_window.py tests/test_island_surface.py tests/test_theme_resolve.py tests/test_ui_styles.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sayink/ui/main_window.py sayink/ui/settings_window.py sayink/ui/history_window.py tests/test_main_window.py tests/test_settings_general.py tests/test_settings_history.py tests/test_history_window.py tests/test_island_surface.py tests/test_theme_resolve.py tests/test_ui_styles.py
git commit -m "feat(ui): embed settings and history in the main window stack"
```

---

### Task 4: Listen bar

**Files:**
- Modify: `sayink/ui/floating_window.py`
- Modify: `tests/test_floating_window.py`
- Modify: `tests/test_island_surface.py` (`COMPACT_HEIGHT` / expanded-island asserts)

**Interfaces:**
- Consumes: existing `show_listening`, `update_partial_text`, `continuous_stop_requested`
- Produces: `BAR_HEIGHT = 40`, `BAR_EXCERPT_HEIGHT = 64`, `BAR_WIDTH = 360`, `BAR_EXCERPT_WIDTH = 420`. No 历史/设置 buttons. One ink `QPushButton` 「结束」 that emits `continuous_stop_requested`. `update_partial_text` shows a single-line excerpt and grows to 64px; does not add a second action row. `COMPACT_HEIGHT` may alias `BAR_HEIGHT` so old imports keep working.

- [ ] **Step 1: Write failing listen-bar tests**

```python
def test_listen_bar_is_thin_without_extra_actions(win):
    from sayink.ui.floating_window import BAR_HEIGHT, BAR_WIDTH
    win.show_listening()
    assert win.height() <= BAR_HEIGHT + 8
    assert win.width() <= BAR_WIDTH + 16
    assert win._end_btn.text() == "结束"
    assert not hasattr(win, "_history_btn")
    assert not hasattr(win, "_settings_btn")

def test_partial_text_grows_excerpt_only(win):
    from sayink.ui.floating_window import BAR_EXCERPT_HEIGHT
    win.show_listening()
    win.update_partial_text("下一步把这份纪要贴到会议群里。")
    assert win.height() <= BAR_EXCERPT_HEIGHT + 8
    assert "纪要" in win._text_label.text()
    assert win._end_btn.text() == "结束"
```

Remove or rewrite tests that require `EXPANDED_MIN_HEIGHT == 168` or three equal buttons.

- [ ] **Step 2: Run to verify fail**

Run: `py -3.10 -m pytest tests/test_floating_window.py::test_listen_bar_is_thin_without_extra_actions -q`

Expected: FAIL (`BAR_HEIGHT` missing or height still 64/168).

- [ ] **Step 3: Implement listen bar**

- Constants as above. `COMPACT_HEIGHT = BAR_HEIGHT`.
- Layout: `_dot`, `_wave`, `_status_label`, stretch, `_end_btn`. `_text_label` hidden until excerpt.
- `_end_btn` style = ink primary (use `settings_styles.BTN_PRIMARY` or inline `PRIMARY_CONTAINER` / `PRIMARY_ON`), pill radius.
- `update_partial_text`: if text strip nonempty, show `_text_label` (one line, elide), resize to excerpt; else keep 40px.
- Delete history/settings actions if present.
- Keep Tool + stay-on-top + no-focus flags. Placement: top-center of the screen under the cursor (may keep a slim helper in `island_chrome.py` named `position_listen_bar`).
- `×` is not required if 「结束」 is present; if a close control remains, listening close = `continuous_stop_requested`.

- [ ] **Step 4: Run tests**

Run: `py -3.10 -m pytest tests/test_floating_window.py tests/test_island_surface.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sayink/ui/floating_window.py sayink/ui/island_chrome.py tests/test_floating_window.py tests/test_island_surface.py
git commit -m "feat(ui): shrink floating HUD to a thin listen bar"
```

---

### Task 5: Tray copy, ready icon, app wiring

**Files:**
- Modify: `sayink/ui/tray_icon.py`
- Modify: `sayink/app.py`
- Modify: `tests/test_tray_icon.py`
- Modify: `tests/test_app.py` (`_wake_island` connection)

**Interfaces:**
- Consumes: `TrayIcon.open_settings`, `history_requested`, `wake_island` (signal names stay)
- Produces: menu label `打开 SayInk`. Ready pixmap uses `tok.TEXT`, not `tok.ACCENT` / `tok.ACCENT_FOCUS`. `SayInkApp._main: MainWindow | None`. `_show_main_window(page: str | None = None)` creates/shows/raises `MainWindow`. Close of main window does not call `QApplication.quit`. `_show_settings` → `_show_main_window("general")` if first open else last page. `_show_history_window` → `_show_main_window("history")`. `_wake_island` deleted; `wake_island` connects to `_show_main_window`.

- [ ] **Step 1: Write failing tests**

In `tests/test_tray_icon.py` `test_menu_groups_match_spec_order`:

```python
assert labels[2] == "打开 SayInk"
```

Add:

```python
def test_ready_icon_is_ink_not_accent(tray):
    from sayink.ui import design_tokens as tok
    src = open("sayink/ui/tray_icon.py", encoding="utf-8").read()
    assert "tok.TEXT" in src
    assert "tok.ACCENT_FOCUS" not in src or "recording" in src
```

Better (no source scrape): after `tray.reapply_theme()`, the normal icon painter path must use `TEXT`. Implement by changing `create_microphone_icon` default `color` to `tok.TEXT` and asserting that in a small unit test of the icon helper:

```python
def test_idle_mic_uses_text_token():
    from sayink.ui import design_tokens as tok
    from sayink.ui.tray_icon import create_microphone_icon
    # constructor default
    import inspect
    assert "TEXT" in inspect.getsource(create_microphone_icon)
    assert tok.ACCENT not in inspect.getsource(create_microphone_icon).split("recording")[0]
```

In `tests/test_app.py`, change `wake_island.connect.assert_called_with(app._wake_island)` to `app._show_main_window`.

- [ ] **Step 2: Run to verify fail**

Run: `py -3.10 -m pytest tests/test_tray_icon.py::TestTrayMenuStyleAndGrouping::test_menu_groups_match_spec_order tests/test_app.py -q`

Expected: FAIL (`打开设置` still present / `_wake_island` still the slot).

- [ ] **Step 3: Implement**

`tray_icon.py`: action text `打开 SayInk`. `create_microphone_icon` idle colors = `tok.TEXT` (both gradient stops). Keep recording = `STATE_RECORD`. Attention = `ATTENTION`.

`app.py`:
- `from sayink.ui.main_window import MainWindow`
- `self._main = None` (do not construct at startup).
- `_show_main_window(self, page: str | None = None)`:
  - if `_main is None`: `self._main = MainWindow(self._config, self._history)`; connect the same settings signals currently on `_settings_win` (`hotkey_updated`, `settings_changed`, …). Get them from `self._main._settings`.
  - if page: `self._main.show_page(page)`
  - `apply_appearance_theme`; `self._main.show(); raise_; activateWindow()`
- `_show_settings` calls `_show_main_window(None)` (last page; first time defaults to general inside MainWindow).
- `_show_history_window` calls `_show_main_window("history")`.
- Remove `_wake_island`.
- `self._tray.wake_island.connect(self._show_main_window)`
- `self._tray.open_settings.connect(self._show_settings)`
- Do not show MainWindow in `__init__`.

Theme apply must call `self._main.reapply_theme()` when `_main` exists, plus listen bar + tray as today.

- [ ] **Step 4: Run tests**

Run: `py -3.10 -m pytest tests/test_tray_icon.py tests/test_app.py tests/test_app_history_wiring.py tests/test_main.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sayink/ui/tray_icon.py sayink/app.py tests/test_tray_icon.py tests/test_app.py
git commit -m "feat(ui): open main window from tray and drop island wake"
```

---

### Task 6: Quiet rows and history actions

**Files:**
- Modify: `sayink/ui/settings_components.py` (choice-card selected style: ink wash, no `2px solid ACCENT`, no left accent bar)
- Modify: `sayink/ui/settings_pages/general.py` / `model.py` / `polish.py` / `about.py` only if they hardcode accent borders
- Modify: `sayink/ui/model_card.py` (「当前」chip = ink, not `ACCENT` border)
- Modify: `sayink/ui/history_window.py` (export ghost; copy-polished is primary when polished exists)
- Modify: `tests/test_ui_styles.py` (`test_vertical_choice_selected_uses_single_emphasis`, `test_active_model_card_uses_subtle_current_state`, `test_model_rating_labels_and_download_use_brand_accent`)
- Modify: `tests/test_history_window.py` (export not primary blue; copy-polished enabled rules stay)

**Interfaces:**
- Consumes: `NAV_SELECTED_BG`, `PRIMARY_CONTAINER`, `BTN_PRIMARY`, `BTN_GHOST_SM`
- Produces: selected choice rows use `background: {NAV_SELECTED_BG}` and `border: 1px solid {HAIRLINE}` — no `border-left: 3px solid {ACCENT}`, no `2px solid {ACCENT}`. Model 「当前」 badge uses `SURFACE_PEARL` + `TEXT_SEC`. History `_export_btn` uses `BTN_GHOST_SM`. After single-select with polished text, `_copy_polished_btn` uses `BTN_PRIMARY`; otherwise `_copy_raw_btn` uses `BTN_PRIMARY`.

- [ ] **Step 1: Write failing style asserts**

In `tests/test_ui_styles.py` `test_vertical_choice_selected_uses_single_emphasis`, replace ACCENT-bar asserts:

```python
from sayink.ui.design_tokens import HAIRLINE, NAV_SELECTED_BG
assert NAV_SELECTED_BG in sheet
assert f"border: 2px solid" not in sheet
assert "border-left:" not in sheet or "border-left: 0" in sheet
```

In history tests:

```python
def test_export_is_ghost_and_copy_primary_follows_polish(qapp):
    # use existing FakeHistoryStore
    from sayink.ui import settings_styles as ss
    window = HistoryWindow(FakeHistoryStore())
    try:
        assert ss.BTN_PRIMARY not in (window._export_btn.styleSheet(),)
        window._session_list.setCurrentRow(0)
        assert ss.BTN_PRIMARY in window._copy_polished_btn.styleSheet() or "PRIMARY_CONTAINER" in window._copy_polished_btn.styleSheet()
    finally:
        window.close()
```

Adapt to the real `FakeHistoryStore` already in `tests/test_history_window.py`.

- [ ] **Step 2: Run to verify fail**

Run: `py -3.10 -m pytest tests/test_ui_styles.py::TestSidebarVisualContracts::test_vertical_choice_selected_uses_single_emphasis tests/test_history_window.py -q`

Expected: FAIL (selected choice still has accent bar / export still primary).

- [ ] **Step 3: Implement restyle**

Update the selected-state QSS in `settings_components.py` choice pickers. Update `model_card.py` current badge. History export + copy button styles in `_paint_history_styles` / selection handler. Do not rewrite download or LLM test state machines.

- [ ] **Step 4: Run tests**

Run: `py -3.10 -m pytest tests/test_ui_styles.py tests/test_history_window.py tests/test_settings_general.py tests/test_main_window.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sayink/ui/settings_components.py sayink/ui/model_card.py sayink/ui/history_window.py sayink/ui/settings_pages tests/test_ui_styles.py tests/test_history_window.py
git commit -m "feat(ui): restyle choices and history actions as ink-quiet rows"
```

---

### Task 7: README and leftover docs

**Files:**
- Modify: `README.md` (空间岛 → 主窗口 + 听写条; 托盘「打开 SayInk」; 设置走侧栏)
- Modify: `tests/test_readme_features.py` if it snapshots island copy
- Modify: `docs/superpowers/specs/2026-09-19-island-ui-redesign-design.md` first lines: `Status: superseded by 2026-09-21-codex-main-window-design.md`

**Interfaces:**
- Consumes: user-facing strings already in the 2026-09-21 spec
- Produces: README no longer tells users the primary chrome is a spatial island or that double-click wakes an island.

- [ ] **Step 1: Write failing README contract**

If `tests/test_readme_features.py` asserts 「空间岛」as the settings shell, change the expected phrases to 主窗口 / 听写条 / 打开 SayInk. Add:

```python
def test_readme_describes_main_window_not_island():
    text = Path("README.md").read_text(encoding="utf-8")
    assert "打开 SayInk" in text
    assert "听写条" in text or "薄" in text
    assert "双击托盘会唤醒空间岛" not in text
```

- [ ] **Step 2: Run to verify fail**

Run: `py -3.10 -m pytest tests/test_readme_features.py -q`

Expected: FAIL on the new assert.

- [ ] **Step 3: Update README + superseded spec banner**

Rewrite the appearance / 空间岛 / 托盘 paragraphs to match the spec. Keep P0 paste/listen behavior unchanged.

- [ ] **Step 4: Run tests**

Run: `py -3.10 -m pytest tests/test_readme_features.py tests/test_main_window.py tests/test_floating_window.py tests/test_tray_icon.py tests/test_theme_resolve.py tests/test_ui_styles.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add README.md tests/test_readme_features.py docs/superpowers/specs/2026-09-19-island-ui-redesign-design.md
git commit -m "docs: describe the main window and listen bar"
```

---

## Self-review

1. **Spec coverage:** Architecture / lifecycle / tokens / chrome / five pages / listen-bar states / tray copy / hide-on-close / no pipeline change / tests+README each map to Tasks 1–7. Semantic empty/error copy is already in history/floating code; Task 4 keeps the state table behavior.
2. **Placeholders:** none.
3. **Types:** `MainWindow.show_page(key: str)` and `current_page() -> str` stay consistent from Task 2 through Task 5. Settings page indices remain 0=通用 … 3=关于. Tray signal names `open_settings` / `wake_island` are unchanged; only labels and slots change.

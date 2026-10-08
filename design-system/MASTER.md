# Design System Master File

> **2026-09-21 desktop redesign:** Current UX decisions and audit are in
> [`../docs/ux-redesign.md`](../docs/ux-redesign.md). This revision supersedes older
> prototype and island layouts. Runtime tokens remain in `sayink/ui/design_tokens.py`.

> **LOGIC:** When building a specific page, first check `design-system/pages/[page-name].md`.
> If that file exists, its rules **override** this Master file.
> If not, strictly follow the rules below.

---

**Project:** SayInk  
**Generated:** 2026-07-18 via ui-ux-pro-max (`--design-system --persist`)  
**Adapted for:** PyQt6 Windows desktop tray utility (light + dark)  
**Style seed:** AI-Native UI / high-end utility (voice transcription)  
**Stack notes:** No web Google Fonts runtime; use system UI fonts. No landing-page hero chrome.

---

## Global Rules

### Theme modes

| Mode | Meaning |
|------|---------|
| `light` | Forced light effective theme |
| `dark` | Forced dark effective theme |
| `system` | Effective theme follows Windows AppsUseLightTheme |

Effective theme is always `light` or `dark`.

### Color Palette — Light

| Role | Hex | Token / CSS Variable |
|------|-----|----------------------|
| Accent / focus | `#2563EB` | `ACCENT` / `--color-accent` |
| Accent text | `#2563EB` | `ACCENT_TEXT` |
| Primary container | `#0D0D0D` | `PRIMARY_CONTAINER` |
| Primary on | `#FFFFFF` | `PRIMARY_ON` |
| Primary hover | `#262626` | `PRIMARY_CONTAINER_HOVER` |
| Primary pressed | `#404040` | `PRIMARY_CONTAINER_PRESSED` |
| Accent Hover | `#1D4ED8` | `ACCENT_HV` |
| Accent Soft | `rgba(37, 99, 235, 0.08)` | `ACCENT_SOFT` |
| Background | `#FFFFFF` | `BG` / `--color-background` |
| Surface | `#FFFFFF` | `SURFACE` |
| Surface Muted | `#F7F7F7` | `SURFACE_PEARL` |
| Navigation surface | `#F6F7F8` | `NAV_BG` |
| Foreground | `#111827` | `TEXT` / `--color-foreground` |
| Foreground Secondary | `#4B5563` | `TEXT_SEC` |
| Foreground Dim | `#667085` | `TEXT_DIM` |
| Border | `rgba(13,13,13,0.08)` | `BORDER` / `HAIRLINE` |
| Control Border | `#D1D5DB` | `CONTROL_BORDER` |
| Destructive / Record | `#C81E1E` / `#DC2626` | `RED` / `STATE_RECORD` |
| Success / toggle on | `#15803D` | `GREEN` / `TOGGLE_ON` / `STATE_LISTEN` |
| Focus Ring | `#2563EB` | `ACCENT_FOCUS` |
| Nav / row selected | `rgba(13,13,13,0.06)` | `NAV_SELECTED_BG` / `ROW_SELECTED` |

**Notes:** Ink primary actions; blue for keyboard focus and links. A subtle neutral rail separates navigation from the reading surface. Recording red is semantic only.

### Color Palette — Dark

| Role | Hex | Token / CSS Variable |
|------|-----|----------------------|
| Accent / focus | `#3B82F6` | `ACCENT` |
| Accent text | `#60A5FA` | `ACCENT_TEXT` |
| Primary container | `#FFFFFF` | `PRIMARY_CONTAINER` |
| Primary on | `#0D0D0D` | `PRIMARY_ON` |
| Primary hover | `#E8E8E8` | `PRIMARY_CONTAINER_HOVER` |
| Primary pressed | `#D0D0D0` | `PRIMARY_CONTAINER_PRESSED` |
| Accent Hover | `#60A5FA` | `ACCENT_HV` |
| Accent Soft | `rgba(59, 130, 246, 0.16)` | `ACCENT_SOFT` |
| Background | `#181818` | `BG` |
| Surface | `#181818` | `SURFACE` |
| Surface Muted | `#222528` | `SURFACE_PEARL` |
| Navigation surface | `#141618` | `NAV_BG` |
| Input surface | `#202326` | `INPUT_BG` |
| Foreground | `#F9FAFB` | `TEXT` |
| Foreground Secondary | `#D1D5DB` | `TEXT_SEC` |
| Foreground Dim | `#9CA3AF` | `TEXT_DIM` |
| Border | `rgba(255,255,255,0.08)` | `BORDER` / `HAIRLINE` |
| Control Border | `#4B5563` | `CONTROL_BORDER` |
| Destructive / Record | `#F87171` | `RED` / `STATE_RECORD` |
| Success / toggle on | `#16A34A` | `GREEN` / `TOGGLE_ON` / `STATE_LISTEN` (quiet mid-green; not neon `#22C55E`) |
| Focus Ring | `#3B82F6` | `ACCENT_FOCUS` |
| Nav / row selected | `rgba(255,255,255,0.06)` | `NAV_SELECTED_BG` / `ROW_SELECTED` |

### Floating overlay (theme-aware)

| Role | Light | Dark |
|------|-------|------|
| Float BG | `rgba(255, 255, 255, 236)` | `rgba(24, 24, 24, 236)` |
| Float Surface | `#FFFFFF` | `#181818` |
| Float Border | `rgba(17, 24, 39, 0.10)` | `rgba(255, 255, 255, 0.12)` |
| Float Text | `#111827` | `#FFFFFF` |
| Float Text Sec | `#4B5563` | `rgba(235, 235, 245, 0.72)` |
| Chip BG | `rgba(17, 24, 39, 0.08)` | `rgba(210, 210, 215, 0.40)` |
| Chip BG Hover | `rgba(17, 24, 39, 0.16)` | `rgba(210, 210, 215, 0.55)` |
| Chip BG Press | `rgba(17, 24, 39, 0.12)` | `rgba(210, 210, 215, 0.48)` |

### Typography

- **UI Font resolve (runtime):** prefer `Microsoft YaHei UI` / `Microsoft YaHei` so QSS (single family) keeps CJK; skip Latin-only families such as `Segoe UI Variable` when a CJK family is present (`resolve_ui_font_family` / `FONT_STACK`)
- **UI Font stack (docs):** `"Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI Variable", "Segoe UI"`
- **Mono resolve (runtime):** `resolve_mono_font_family` picks one installed family — Cascadia Mono, else Consolas, else JetBrains Mono — and publishes that single quoted name as `FONT_MONO`. Qt stylesheets do not honor a CSS fallback list.
- **Weights:** Microsoft YaHei UI ships Regular and Bold only. UI text uses font-weight 400 (body, unselected, quiet controls) or 700 (titles, selected items, badges, primary actions). Do not use 500, 550, or 600.
- **Tracking:** letter-spacing 0 on Chinese UI text. No negative tracking.
- **Mood:** technical, precision, clean, premium desktop utility
- **Do not** ship runtime Google Fonts imports in the desktop app

| Token | Size | Typical use |
|-------|------|-------------|
| `TYPE_CAPTION` | `12px` | Badges, meta captions |
| `TYPE_FOOTNOTE` | `13px` | Footnotes and tips |
| `TYPE_BODY_SM` | `14px` | Inputs, nav, tray menu, section labels |
| `TYPE_BODY` | `14px` | Global baseline, brand, primary body |
| `TYPE_TITLE_SM` | `15px` | Model card titles |
| `TYPE_TITLE` | `16px` | Detail titles and history reading text |
| `TYPE_ICON_LG` | `17px` | Float close glyph |
| `TYPE_HERO` | `18px` | Engine hero title |
| `TYPE_TITLE_LG` | `20px` | Settings page titles |
| `TYPE_DISPLAY` | `22px` | History window title |

### Spacing

| Token | Value | Usage |
|-------|-------|-------|
| `SPACE_XS` | `8px` | Tight gaps (the web prototype called this `--space-sm`) |
| `SPACE_SM` | `12px` | Inline |
| `SPACE_MD` / `--space-md` | `16px` | Standard padding |
| `SPACE_LG` / `--space-lg` | `24px` | Section padding |
| `SPACE_XL` | `32px` | Large gaps |
| `SIDEBAR_WIDTH` | `184px` | Navigation and runtime summary |
| `CONTENT_MAX_WIDTH` | `900px` | Settings document width at larger window sizes |
| `CONTROL_NUMERIC_WIDTH` | `120px` | History spinboxes (flat stepper + suffix) |

### Shape

| Token | Value |
|-------|-------|
| `RADIUS_XS` | `4px` |
| `RADIUS_SM` | `6px` |
| `RADIUS_MD` | `8px` |
| `RADIUS_LG` | `10px` |
| `WINDOW_RADIUS` | `12px` |

### Shadows

None. Qt style sheets do not render `box-shadow`, and the desktop surfaces are
flat: depth comes from `HAIRLINE` borders and surface colour steps. Do not add
shadow tokens to `design_tokens.py` unless a surface actually paints one.

---

## Component Specs (desktop)

### Buttons

- Primary: ink fill (`PRIMARY_CONTAINER` / `PRIMARY_ON`), radius MD, visible `:focus` ring 2px accent
- Ghost / danger: semantic colors; danger uses destructive red
- Hover: darken/lighten within theme; no large translate on tray-adjacent chrome

### Cards / settings groups

- Surface fill, 1px border, radius LG
- Main navigation: ink wash (`NAV_SELECTED_BG`), semibold label, 40px row height, 2px keyboard focus ring. Focus ring appears only for keyboard navigation (TabFocus) — never as a second "selected" state on open.
- Sidebar runtime status: hairline + 8px semantic dot (`GREEN` ready / `AMBER` loading / `RED` failure) + text. Not a filled card, so it cannot read as a sixth nav item.
- Task choices: native radio indicator, title and task-specific explanation; full card is clickable.
- Settings pages: 20px page title, purpose sentence, grouped 14px section labels.
- Engine hero card shows the *load* state (「已载入 · 可用」/「模型载入中…」) beside the「当前」badge; downloaded ≠ usable.
- Polish page keeps the before/after example visible while the feature is off.
- History list: 58px rows (`HH:MM` mono + preview / app · source · N 段), non-selectable day headers (今天 / 昨天 / date), selected row = `ACCENT_SOFT` wash + 3px `ACCENT` left bar. No rounded accent capsule around the row.
- History detail: title is the session identity (day + time), stats right-aligned, metadata chips, then a「润色 / 原文」segmented view toggle (only when a polished text exists). Segments render as numbered blocks with `HH:MM:SS · duration` captions; search hits highlighted with `AMBER_SOFT`.
- History action bar is fixed (buttons disable, never disappear): primary copy on the left,「导出」「删除」on the right. Multi-select keeps「复制」enabled and joins each session's effective text, oldest first, with a blank line between sessions.「清空全部历史」is a quiet text action on the list summary row, disabled when there is nothing to clear, and confirms with an in-app card (取消 / 清空). History preferences stay in Settings; the empty state still offers「设置历史记录」when recording is off.
- History list scrollbar is a single transparent-track thumb. The splitter handle stays transparent at rest so it does not read as a second scrollbar.

### Inputs

- Surface / input bg, control border, focus ring accent
- Numeric spinboxes share `CONTROL_NUMERIC_WIDTH` (120px); flat up/down PNG chevrons

### Floating window

- Follows **effective** theme float tokens (not permanently locked to dark)
- 360×44px at rest; 420×68px with a one-line excerpt. Radius 22px / 16px respectively.
- The 64×30px stop target appears only during a continuous session; state colors survive theme changes.
- Native QFont sizes use pixels to match the QSS type scale under display scaling.

### Tray menu

- Surface, border, hover row wash from effective theme

---

## UX Guidelines

- Focus rings visible for keyboard users
- Ctrl+1…5 switches pages, Ctrl+F opens history search, Ctrl+W hides to tray; shortcuts pause during hotkey capture.
- Model and audio test errors remain readable at their controls; success does not require a modal.
- History refresh preserves selection and search; pending deletions stay hidden and share an eight-second undo batch.
- Deleting sessions has exactly one safety net: the undo toast. No confirmation dialog. Only「清空全部历史」(irreversible) confirms.
- History feedback (copied / exported / undo) is an overlay toast anchored to the bottom of the page, never a layout row — nothing shifts.
- History keyboard: `Delete` removes the selection, `Ctrl+C` copies the effective text (joined when several sessions are selected), `Ctrl/Shift` multi-select.
- Closing the main window preserves the tray process; title-bar double click maximizes/restores; every window edge resizes (native `WM_NCHITTEST`), plus the corner grip.
- Tray: single click opens the main window on every platform (Windows defers by the double-click interval so a double click opens once).
- No emoji-as-icon; use drawn/SVG icons
- Theme switch applies without restart; preference persisted as `appearance.theme_mode`
- Keep recording red rationed to recording/error semantics
- Avoid excessive decoration and web-landing patterns in settings chrome

---

## Anti-patterns

- Warm cream / terracotta desktop kits
- Purple-glow AI cliché chrome
- Hard-coding a second undocumented brand palette beside this MASTER
- Floating window permanently dark when effective theme is light

---

## Traceability

| Source | Detail |
|--------|--------|
| ui-ux-pro-max query | `desktop utility voice transcription productivity dark mode windows tray` |
| Persist output | `design-system/sayink/MASTER.md` (raw skill output) |
| This file | Canonical app MASTER with light/dark + desktop adaptations |

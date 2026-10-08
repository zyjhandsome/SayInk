# SayInk 空间岛 UI 重构

Date: 2026-09-19  
Status: superseded by 2026-09-21-codex-main-window-design.md  
Approach: A, 空间岛系统重构（保留品牌）

## Problem

SayInk 的四条 UI 表面（胶囊浮窗、设置、历史、托盘）已半迁移到顶部空间岛，但还不是同一套产品语言。设置仍藏着侧栏、展开岛三个等权按钮、历史文案和元数据挤成一团、token 与 MASTER 有漂移。用户要的是专业级全界面重构，不是换品牌色。

## Goal

在不改转写链路的前提下，把四条表面收成同一套空间岛系统：胶囊报状态，展开岛给出一个主出口，设置岛和历史岛是同一家族的大面板，托盘与胶囊共用状态词。

## Non-goals

- 不改热键、录音、VAD、识别、粘贴、润色、历史 SQLite、配置键名。
- 不把设置/历史变回系统标题栏窗口。
- 不换品牌色、不换运行时字体、不加着陆页式大标题。
- 不在胶囊里放设置项。
- 不新增第五个设置页，不保存或回放音频。

## Design read

Windows 托盘型桌面工具。刻度：`VARIANCE 5` / `MOTION 4` / `DENSITY 5`。动效只服务状态。

## Visual language

Brand stays: 蓝强调、冷灰中性、系统字体、浅暗双轴。

### Shells

Exactly two chrome families.

| Shell | Size | Radius | Role |
|-------|------|--------|------|
| 胶囊 | ~320 × 64 | pill | 只报当下 |
| 面板 | 历史 520 × 640；设置 720 × 640 | 28px | 顶栏 + 滚动内容 |

Both sit top-center of the screen under the cursor, 72px below the available-area top. Frameless, stay-on-top, translucent. Float colors come from `design_tokens.py`; `design-system/MASTER.md` must match the code axis. No third palette.

### Color roles

| Token | Use | Forbidden |
|-------|-----|-----------|
| `ACCENT` blue | 焦点、主按钮、选中描边、当前引擎 | 整页底、录音态 |
| `GREEN` / `ISLAND_MINT` | 开关开、正在听、就绪点 | 主 CTA |
| `STATE_RECORD` / `RED` | 正在录、失败、删除/清空 | 品牌色 |
| `AMBER` | 可恢复提示 | 错误 |

State is color + copy + motion, never color alone.

### Type, shape, space

- Font resolve: Segoe UI Variable → 微软雅黑 UI → Segoe UI. No web fonts.
- Size ladder stays 11–22px.
- Radii only: control 8, card 10, sheet 28, capsule/chip/toggle pill.
- Space: 8 / 12 / 16 / 24. Sheet inset 16.

### Motion

Listening: dot pulse + waveform. Capsule to sheet: transform/opacity ~200ms. Honor reduced motion (instant expand, no pulse/wave). No decorative loops.

### Shared components

1. Primary button: blue fill, white text, height 32, radius 8. One primary intent per screen.
2. Secondary: muted fill + stroke, same height.
3. Danger: red text or red stroke. Delete / clear only.
4. Choice cards: selected = soft blue + 2px accent border.
5. Chips: source, app, segment count, model. Never glue metadata into one sentence.
6. Toggle: green on, gray off; one track size everywhere.
7. Input: label above, hint/error below, 2px accent focus ring.
8. Close: 28×28 circle, drawn icon (not emoji), same on all four surfaces.
9. Settings nav: four pills in the sheet header. Delete hidden `SettingsSidebar`.
10. Tray menu radius 8.

## Capsule and expanded island

File: `sayink/ui/floating_window.py` + `island_chrome.py`.

### Capsule (default)

Left to right: status dot, waveform (capture only), status label, close. No source chips, no actions, no body.

- Click capsule only while continuous listening is active → expand.
- `×` while listening = end the session (same as Esc / 结束). `×` while idle = dismiss.

### Expanded island

Only for continuous listening after user click or after partial text arrives. Width ~420, height ~168.

1. Header: dot, waveform, status, read-only source chips, close.
2. Body: live text, max 4 lines, ellipsis + tooltip for overflow.
3. Actions: primary **结束**; secondary **历史** **设置**.

Source chips are display-only (mic / system; both on when mixed). They must not look like disabled buttons. Click the header status zone again to collapse.

Existing signals stay: `continuous_stop_requested`, `history_requested`, `settings_requested`. `update_partial_text` still auto-expands during continuous listen.

### State table

| State | Shell | Accent / wave | Auto-hide |
|-------|-------|---------------|-----------|
| 模型载入中 | capsule + one-line subtitle | muted, no wave | no |
| 正在听 | capsule; expand on click or text | green + wave | no |
| 录音中 (hold-to-talk) | capsule | red pulse + wave | no |
| 正在识别 / 润色中 | capsule; optional one-line excerpt | body color, no wave | no |
| 已输入 / 已就绪 | capsule | body color | 1.5–2.2s |
| 已复制 | capsule + amber subtitle | body color | 2.2s |
| 失败 | capsule, title + subtitle | red | 5s |
| 已取消 / 已停止 | capsule | dim | 1s |

Failure must not grow the window toward 260px. Long errors go in tooltip.

Keep: no idle「待开始」capsule after continuous-mode ready; short-press hotkey does not show the island; paste failure reports 已复制, never 已输入.

## Settings island

File: `sayink/ui/settings_window.py` + `settings_pages/` + `settings_components.py`.

### Chrome

720×640 sheet. Header: title **设置**, four pills, close. No in-page `PageHero`. Auto-save, no footer save bar. One footnote at the bottom of 通用.

Pills: **通用** / **引擎** / **润色** / **关于**. First pill is 通用 (not 录音) because the page already holds theme, autostart, and history. This matches README「设置 → 通用」.

Delete the hidden `SettingsSidebar` instance and its dual-nav sync.

### 通用

Three groups, same order: 录音 → 音频 → 偏好.

1. **录音**: two choice cards (连续口述 / 按住说话) + hotkey field. Hint is one sentence: hold ~0.30s to start continuous listen; release does not end; Esc or 结束 ends the session.
2. **音频**: three source cards. Amber mixed-mode callout only when mixed is selected. One primary: 测试声音. Device combos stay behind 手动选择; default is auto.
3. **偏好**: theme segment (跟随系统 / 浅色 / 暗色) + toggles (开机启动、提示音、恢复剪贴板、保存语音历史). Retention days and max sessions visible only when history is on.

### 引擎

- Current engine as the featured card: name, blue「当前」badge, size, capability chips (not a dotted run-on).
- Storage: one summary line + secondary「更改位置」.
- Downloadable cards share the same anatomy. One primary per card (下载 or 使用此模型). Delete is danger secondary. Progress lives inside the card.

### 润色

Toggle first. Off: only the toggle and「直接输出原文」. On: preview, API fields, prompt. Key masked by default. 显示 / 测试连接 / 恢复默认 share one action-column width. Success/failure of 测试连接 is inline under the field, not a system dialog as the primary feedback.

### 关于

Not a spec sheet. Row 1: SayInk + version chip. Three status rows: current model, hotkey, polish on/off. Model dir and config path live in a collapsed「文件位置」disclosure. Bottom amber tip follows the active trigger mode.

Config keys and auto-save behavior do not change.

## History island

File: `sayink/ui/history_window.py`.

520×640 single column (no left/right split). Title **历史** (not 过去的话). Title type is 16px semibold, not 22px display.

- Search placeholder: 搜索转写内容. 200ms debounce stays.
- **导出 Markdown** stays as the label, painted as a secondary button (not blue primary). Selected 1 = that session; multi-select = batch. Markdown field set unchanged.
- Day groups: 今天 / 昨天 / `YYYY-MM-DD`. Group header once per day.
- Row: `HH:MM` (no seconds), 2-line preview, chips for source / target app / segment count. App chip is the process name alone.
- Selected row: soft blue + 3px accent bar (keep).
- Detail: chips for source / trigger / model; body is the hero. If polished ≠ raw, two blocks labeled **原文** and **润色** (never 译文). If only one text, no label.
- Copy raw / copy polished enabled only for single select. Copy polished disabled when no polished text. Success is a light inline toast, not a system dialog.
- Empty: 「还没有会话。完成一次转写后会出现在这里。」
- Search empty: 「没有匹配的转写。」
- Delete: confirm, 8s undo (existing).
- 清空全部历史: bottom danger secondary, separate confirm.

SQLite schema, retention policy, and export fields do not change.

## Tray

File: `sayink/ui/tray_icon.py`.

Three icon kinds only:

| Kind | Color | When |
|------|-------|------|
| 就绪 | blue mic | idle, 已输入, 已停止 |
| 采集 | red mic | 正在听, 录音中 |
| 注意 | amber | existing `flash_attention`, then restore previous kind |

Tray capture-red while the capsule listen-dot is green is intentional: the tray must read as "audio is live" from the taskbar. Do not recolor the tray to mint to match the capsule.

Menu radius 8. Structure unchanged: disabled status line; 打开设置; 历史; 切换模型 (empty: 暂无已下载模型); 开机自启; 退出.

Windows double-click wakes the island, does not open settings. Tooltip and the disabled status line use the **capsule words**, not a second vocabulary: 就绪 / 正在听 / 正在识别 / 润色中 / 模型载入中. Format: `SayInk - {状态}`. Never pair 加载中 with 模型载入中, or 监听中 with 正在听.

## Cross-cutting empty / error / busy

| Scene | UI |
|-------|-----|
| Model loading | capsule subtitle + tray 模型载入中; hotkey ignored; other errors must not overwrite |
| Load failure | red two-line capsule; tray returns to 就绪 |
| Unrecognized / too short | red two-line capsule, 5s |
| Cannot paste | 已复制 + amber subtitle, never 已输入 |
| Polish failure | silent raw output, no error island |
| Mic test failure | red text under the button |
| LLM test | inline under the field |
| Reduced motion | no pulse/wave; expand is instant |

## Architecture

```
app.py (unchanged orchestration)
  ├── FloatingWindow     capsule / expanded
  ├── SettingsWindow     720 sheet, pill nav
  ├── HistoryWindow      520 sheet
  └── TrayIcon           3 icon kinds + menu
island_chrome.py         flags, placement, sheet/capsule QSS
design_tokens.py         single color axis; MASTER.md must match
```

Reuse existing settings widgets (`TriggerModePicker`, `AudioSourcePicker`, `ToggleOptionRow`, `ModelCard`, `HotkeyEdit`). Change chrome, hierarchy, and copy. Do not rewrite business dialogs.

## Testing and docs

Update, do not discard:

- `tests/test_theme_resolve.py` (four-surface theme, island chrome)
- `tests/test_ui_styles.py`
- `tests/test_floating_window.py`
- `tests/test_tray_icon.py`
- `tests/test_island_surface.py` if present
- `tests/test_readme_features.py` if user-facing copy in README changes
- `design-system/MASTER.md` float tokens
- README: 设置 → 通用; 空间岛状态词; 历史（不再写「过去的话」作为窗名）

Acceptance:

1. Light / dark / system switch updates all four surfaces without restart.
2. Settings has only the four header pills; no sidebar remnant.
3. Expanded island has one primary: 结束.
4. History title is 历史; day groups; detail metadata is chips.
5. Tray capture is red, idle is blue; Windows double-click wakes the island.
6. Theme, float, tray, README tests stay green. Manual smoke: load → continuous listen + text → end → open settings/history → switch theme.

## Risks

- Settings still references a hidden sidebar; removing it can break nav/index tests. Keep page order 通用=0, 引擎=1, 润色=2, 关于=3.
- History and settings tests assert old titles and 22px display type; update those assertions with the visual change.
- Token drift: publish float tokens once, then regenerate QSS from `activate()`.

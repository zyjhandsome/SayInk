## Why

无边框主窗口 `MainWindow` 当前只有实心底色、没有外边框。窗口贴在浅色桌面上时，用户看不到软件边界。

## What Changes

给 `MainWindow` 加上主题感知的 1px 外边框，只画在最外层窗口，不泄漏到侧栏按钮或内嵌页面。

## Capabilities

- main-window-chrome

## Impact

- `sayink/ui/main_window.py`（`MainWindow.reapply_theme`）
- `tests/test_main_window.py`

---

# 轻量契约（Quick / Debug-Low）

目标：在已打开的无边框主界面四周画一条可见外边框，让用户能辨认窗口边界。
非目标：不恢复系统标题栏；不改听写胶囊 / 浮动条；不改独立设置岛或历史岛的 `FLOAT_BORDER`；不改侧栏、导航或页面内容；不加阴影、圆角或窗口尺寸变化。
影响文件/符号：`sayink/ui/main_window.py` `MainWindow.reapply_theme`；`tests/test_main_window.py`
可观察行为：浅色与深色主题下，主窗口最外圈出现一条显性 1px 实线外框（类似系统窗口轮廓），颜色使用 `TEXT_DIM`（浅色 `#667085`，深色 `#9CA3AF`）；根布局留 1px 内缩，避免标题栏/侧栏盖住边线；子控件不因此多出边框。
最小验证：`python -m pytest tests/test_main_window.py -q`，断言外框色为 `TEXT_DIM` 且根布局四边 margin 为 1。
禁止范围：`sayink/app.py` 编排；`sayink/ui/island_chrome.py`；`sayink/ui/floating_window.py`；`sayink/ui/design_tokens.py` 色值；录音 / 热键 / ASR / 粘贴 / 润色 / SQLite。
风险/未知项：Low / Quick。无红线。工作区另有未提交改动，本变更只碰主窗口 chrome 与其测试。仓内另有 3 个已标 Complete 的 theme change，路径不包含 `main_window.py`。默认用 `CONTROL_BORDER` 而非更淡的 `HAIRLINE`，避免加了边仍看不见。
澄清完整性扫描：已检查的适用维度=入口（托盘打开主窗口）、正常态（浅/深色、默认 960×640）、最大化仍保留描边、失败/数据/权限/迁移=N/A、调用方契约不变、验收=样式断言。证据已解决项=主窗口 `FramelessWindowHint` 且 `reapply_theme` 只设 `background`。新增开放问题=无。N/A=支付/隐私/并发。是否仍阻塞=否。

### Explore 交接消费
N/A — 无 explore handoff

### 状态源与工件位置
- 后端：OpenSpec change
- 路径：openspec/changes/ui-main-window-outer-border
- 闸门记录：实施批准：已批准（批准人=用户 contract_go=start / 2026-09-21T14:54+08:00；绑定当前提案修订）

> Quick 车道无 `design.md` 属预期：`openspec status` 显示 design 未完成不代表变更不完整，
> 向用户解释时引用本行即可；不要为凑 status 写假 design。

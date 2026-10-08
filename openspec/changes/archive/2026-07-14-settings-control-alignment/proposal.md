## Why

设置页「历史」区块中，保留天数与最大会话数的 `QSpinBox` 随内容自适应宽度，右侧形成锯齿，且未纳入与 `QLineEdit`/`QComboBox` 同一套视觉样式；同类右对齐控件（开关 vs 数值）右缘也不统一。用户已确认希望统一宽度，并顺带做一版轻量对齐 polish。

## What Changes

- 历史区两个数值步进框使用相同固定宽度与统一高度
- 为 `QSpinBox` 补充与设置页输入控件一致的 QSS（边框、圆角、内边距）
- 右侧控件列（开关 / spin）右缘对齐到同一边距
- 设置内容区滚动条改为按需显示（不再常开占位）
- 不改动导航结构、主题色、触发方式/音频来源卡片布局，不缩短脚注（本轮不做）

## Capabilities

### New Capabilities

- `settings-control-alignment`: 设置页同类数值控件等宽、样式纳入设计系统、右侧控件列对齐、滚动条按需显示

### Modified Capabilities

- （无；主库 `openspec/specs/` 当前无既有能力可 delta）

## Impact

- `sayink/ui/settings_window.py` — 历史 spin 固定宽度
- `sayink/ui/settings_styles.py` — `QSpinBox` QSS
- `sayink/ui/settings_components.py` — `labeled_row` / 滚动条策略（若需）
- `sayink/ui/design_tokens.py` — 可选：控件槽宽度 token
- `tests/test_ui_styles.py` — 样式/对齐相关断言（若已有覆盖点）

## Why

设置四表面换肤与字体 polish 已落地，但 `SettingsWindow.reapply_theme` 仍是 225 行角色枚举（cyclo 55 / cognitive 106），`settings_window.py` 与 `settings_components.py` 各约 1600 行。后续任何样式改动都会继续往巡检里加分支。用户选定探索推荐方向：把设置窗换肤收成 ThemeAware 协议，并按页拆分，而不改设置 IA 或听写主路径。

## What Changes

- 四表面换肤统一为 ThemeAware 广播：壳层只通知，控件自己按活 token 重绘（设置窗删除 `viRole`/`objectName` 巡检链）
- 按设置分页拆出页面模块，`SettingsWindow` 保留接线、信号与保存；分页 IA 不变
- 既有 light/dark/system 切换与 `settings-control-alignment` 对齐验收在新结构下仍须成立
- 视觉：用户可察观感与 typography polish 后基线一致，允许非用户可察的 QSS 结构重组
- **非 BREAKING 对用户功能**：不改热键、录音、ASR、润色、历史数据模型；不新增配置键

## Capabilities

### New Capabilities

- `settings-themeaware`: 设置窗 ThemeAware 换肤协议、页面模块边界、切主题后无角色巡检残留

### Modified Capabilities

- `settings-control-alignment`: 在 ThemeAware 拆分后，历史数值控件等宽、右缘对齐、滚动条 AsNeeded 等既有要求仍须满足（light/dark 均成立）

## Impact

- 代码：`sayink/ui/settings_window.py`、`settings_components.py`、`theme.py`、`settings_styles.py`、`model_card.py`；相关 `tests/test_settings_general.py`、`test_ui_styles.py`、`test_theme_resolve.py`、`test_ui_font.py`
- 配置/API：无持久化键变更（默认）
- 依赖：无新第三方依赖
- 并行 change：`ui-theme-design-system`、`ui-typography-theme-polish` 已 Complete/verified 未归档，共享 `sayink/ui/*`（G8 警告，非阻塞）

---

# ui-settings-themeaware-split：需求与代码事实简报

## 意图

### 目标与成功标准
- 目标：四表面换肤统一为 ThemeAware 协议；设置窗按页拆分模块，使后续样式改动不再扩展角色枚举。
- 可观察的成功结果：
  1. 切换 light/dark/system 后，设置 / 历史 / 浮窗 / 托盘随有效主题刷新，无构造期残留色。
  2. `SettingsWindow.reapply_theme` 不再按 `viRole`/`objectName` 枚举刷样式；改为广播 ThemeAware / `reapply_styles`。
  3. 设置分页仍为通用 / 模型 / 润色 / 关于，入口与文案不因拆分而改。
  4. `settings-control-alignment` 在 light/dark 下仍成立；既有设置/主题/四表面自动化测试通过。
  5. 用户可察观感与当前 polish 后基线一致（允许非用户可察的 QSS 重组）。

### 边界与非目标
- 本次范围：四表面 ThemeAware 协议；设置窗按页拆分；对齐验收延续。
- 非目标：
  - 重做浅/暗色 token 或字体阶梯
  - 恢复文件转写或翻译模式
  - 替换 PyQt6；重排设置分页信息架构
  - 持续听写中间结果、系统主题热更新、API Key 钥匙串、`App` 会话状态抽出
  - 像素级截图回归基建；顺手改用户可察视觉残留
- 禁止修改路径：`sayink/speech_recognizer.py`、`sayink/audio_recorder.py`、热键核心逻辑

## 代码事实

### 现状摘要
- `theme.ThemeAware` 是仅含 `reapply_theme()` 的 Protocol；`apply_theme` 对传入 surfaces 调用该方法。
- 四表面均实现 `reapply_theme`，体量不对称：History 5 行（委托 `_paint_history_styles`）、Tray 9 行、Floating 42 行、Settings **225 行**（cyclo 55 / cognitive 106）。
- Settings 构造后立即 `reapply_theme()`，以覆盖工厂函数构造期烘焙的样式片段。
- 部分控件已有 `reapply_styles`（`PageHero`、`ToggleOptionRow`、`CompactPickCard`、`ThemeModeSegment`、`ModelCard`）；工厂函数 `info_callout` / `paint_usage_tip_bar` / `footnote` / `kv_row` 仍靠窗级巡检补刷。
- 设置页由 `SettingsWindow._create_general_page`（401–639）、`_create_model_page`、`_create_polish_page`、`_create_about_page` 建在同一文件；`settings_window.py` 1621 行、`settings_components.py` 1625 行。
- 测试：`test_settings_general` 锁定原生分页与文案；`test_theme_resolve` / `test_ui_styles` / `test_ui_font` 覆盖切主题与 token。

### 可复用 / 需扩展 / 冲突
#### 可直接复用
- `theme.apply_theme` / `ThemeAware` 广播；History 的「壳层 setStyleSheet + 委托绘制」模式
- 已有 `reapply_styles` 的控件与 `paint_info_callout` / `paint_usage_tip_bar`
- `settings-control-alignment` 与设置/主题 pytest

#### 需要扩展
- 工厂控件与无 `reapply_styles` 的标签/按钮角色，需可被广播刷新
- 页面建造逻辑从 `SettingsWindow` 迁出（若开放问题选择拆页）
- 窗级 `reapply_theme` 收缩为广播 + 少量壳层 chrome

#### 需求与现状冲突
- 目标「不再角色枚举」与当前 225 行 `viRole` 链冲突，必须删除该链而不是再加角色
- 与未归档 Complete change 共享 `sayink/ui/*`（序列风险，G8 警告）

### 挂载点候选
| 优先级 | 路径/符号 | 理由 |
|---|---|---|
| 必选 | `sayink/ui/settings_window.py` `SettingsWindow.reapply_theme` | 要删除的角色枚举 |
| 必选 | `sayink/ui/settings_components.py` 工厂函数与已有 `reapply_styles` | 换肤责任下沉处 |
| 必选 | `sayink/ui/theme.py` `ThemeAware` / `apply_theme` | 既有广播协议 |
| 必选 | `SettingsWindow._create_general_page` 等四页建造方法 | 拆页挂载 |
| 必选 | `history_window.py` / `floating_window.py` / `tray_icon.py` 的 `reapply_theme` | Q1=C：四表面同一协议 |
| 备选 | `sayink/ui/model_card.py` `ModelCard.reapply_styles` | 已自刷，保持协议一致 |
| 备选 | `tests/test_settings_general.py` / `test_theme_resolve.py` / `test_ui_styles.py` / `test_floating_window.py` / `test_history_window.py` / `test_tray_icon.py` | 回归 |

### 波及线索
- 调用方：`App.apply_appearance_theme` → `apply_theme(..., surfaces=[settings, history, floating, tray])`；设置窗打开时构造期 `reapply_theme`
- 测试：设置分页文案、控件对齐、切主题、字体/callout 刷新断言需跟结构走，不应改用户可见文案
- 无配置键、无打包依赖变更预期
- 未归档 UI change 并行存在，实施期避免同时改同一文件

### 质量画像与验收

| profile | required / not_required | 证据与验收 |
|---|---|---|
| functional | required | 切主题后设置四页控件刷新；分页入口/文案不变；对齐规格仍成立。pytest：`test_settings_general` `test_theme_resolve` `test_ui_styles` `test_ui_font` |
| visual | required | 桌面四表面（非浏览器）。baseline：当前 typography polish 后 light/dark。primary route：设置→通用 切主题。secondary：设置其余三页、历史窗、浮窗、托盘菜单。必测 state：light、dark、system 解析为二者之一。允许差异：非用户可察的 QSS/DOM 结构重组。禁止差异：分页 IA、半换肤、控件错位、用户可察色/字号/间距漂移。G9：`openspec/changes/ui-settings-themeaware-split/verification.md`（execute） |
| accessibility | not_required | 不改键盘/读屏契约；热键捕获信号保持 |
| performance | not_required | 换肤仍是用户触发的一次性刷新，非热路径；删除巡检预期不恶化 |
| compatibility | required | 无配置键变更；`settings-control-alignment` 不削弱 |

### 证据表

| 类型 | 结论 | 证据 |
|---|---|---|
| 事实 | Settings `reapply_theme` 225 行 / cyclo 55 | Memory `get_code_snippet` `SettingsWindow.reapply_theme` |
| 事实 | History `reapply_theme` 仅 5 行委托绘制 | `HistoryWindow.reapply_theme` |
| 事实 | `ThemeAware` 已是 Protocol | `sayink/ui/theme.py` L25–26 |
| 事实 | 部分控件已有 `reapply_styles` | `PageHero` / `ToggleOptionRow` / `ModelCard` 等 |
| 事实 | 设置双文件各约 1600 行 | 工作区行数：1621 / 1625 |
| 事实 | 用户选定探索推荐方向 | 会话回复「推荐方向」 |
| 推断 | 工厂控件若无自刷钩子，切主题仍会残留 | 既有 polish verification 警告 + 构造期 `setStyleSheet` |
| 决策 | Q1=C 四表面协议 + 设置拆页；Q2=A 观感一致 | 开放问题清单 |

## 消歧与闸门

### 开放问题清单

| 优先级 | 问题 | 代码事实背景 | 选项与影响（摘要） | 建议 | 状态 | 最终决策 |
|---|---|---|---|---|---|---|
| 必选 | Q1 拆分深度？ | 双文件各 ~1600 行；换肤债在 Settings；其它表面已短 | A 只收口 reapply / B 按页拆 + ThemeAware / C B + 四表面 | B | decided | **C**（2026-09-18 用户点选）— 设置按页拆 + 四表面同一 ThemeAware 协议 |
| 必选 | Q2 视觉验收口径？ | 无像素回归基建 | A 观感一致 / B 像素级 / C 顺手改残留 | A | decided | **A**（2026-09-18 用户点选）— 观感一致，允许非用户可察的 QSS 重组 |
| 可选 | Q3 未归档 Complete change 是否本变更前 archive？ | 两份 UI change Complete，G8 共享 `sayink/ui/*` | A 先 archive / B 本变更后另开 / C 用户自行 | B | deferred | 非阻塞；实施期不并行改旧 change |

### 澄清完整性扫描
- 已检查的适用维度：使用者（设置窗）；切主题正常态；构造后再切主题的残留；对齐/兼容；验证面；非目标边界
- 由证据解决的缺失事实：四表面 reapply 体量差；ThemeAware 已存在；工厂控件部分未自刷；无配置键
- 新增开放问题及处理状态：Q1/Q2 decided；Q3 deferred 非阻塞
- 明确不适用：鉴权/支付/隐私/迁移/热键/ASR；系统主题热监听；像素截图基建
- 结论：无实质阻塞项

### 风险定级与闸门建议
- 建议车道/风险：Standard / `medium`
- 命中的风险特征：跨四表面 + 共享换肤协议；影响用户可观察外观；需回归验证；与未归档 change 路径重叠（警告）
- 未命中的高风险特征：无 auth/payment/privacy/migration/public API/破坏性数据/核心 STT 路径；不改配置 schema；Q2=A 非视觉 BREAKING
- 不确定点：拆页后测试夹具内部路径；浮窗/托盘协议化是否只需对齐接口、实现已较短
- 闸门建议：规格闸门 → plan → 实现闸门；不得降为 Quick。未升 High：无红线、无 schema、目标无用户可察视觉变更
- 可用验证：设置/主题/四表面 pytest；手工四表面切 light/dark
- 缺失验证：无像素级视觉回归（Q2=A，用手工 + 样式断言替代）

Explore `risk_signal=standard-likely` 仅线索；本定级按上述代码事实重算为 Standard/medium。

### Explore 交接消费

- [x] `chosen_direction` → 已写入「意图」（用户「推荐方向」= 设置窗 ThemeAware 化与拆分）
- [x] `non_goals` → 已写入「意图」边界
- [x] `code_anchors` → 已驱动挂载点与证据表（settings_window / settings_components / theme / 测试）
- [x] `risk_signal` → 仅线索；已按代码事实重算 Standard/medium
- [x] `unknowns` → Q1/Q2 已决；macOS/Linux、API Key、中间结果非本变更；Q3 deferred

落点摘要：意图=四表面 ThemeAware + 设置拆页；挂载=四表面 reapply + pages；Risk=medium；开放问题=Q1C Q2A

### 状态源与工件位置
- 后端：OpenSpec change
- 路径：`openspec/changes/ui-settings-themeaware-split/`
- 闸门记录：
  - 规格批准状态 = **已批准**
  - 批准人：user（点选「批准范围并进入设计」）
  - 批准时间：2026-09-18T16:05:00+08:00
  - binds_to_revision（规格）：见同目录 `handoff.json` 的 `source_revision.artifact_revision`
  - accepted_warning_ids：[`W-g8-overlap-ui-theme`]
  - 实现批准状态 = **已批准**
  - 实现批准人：user（对话回复「开始实施」）
  - 实现批准时间：2026-09-18T23:59:00+08:00
  - 实现批准 binds_to_revision：`6f1a7e5c624f9d7aa1387c0020b5904a4f83e9ef55ffb5dcc5688febe67aa203`
  - accepted_warning_ids（实现）：[`W-g8-overlap-ui-theme`]
  - 下一阶段 = `delivery-execute-verify`

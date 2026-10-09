## Why

双主题与四表面换肤主路径已落地，但字体存在「QSS YaHei / 浮窗 Segoe」双轨，字号以魔法数散落，部分工厂控件与视觉 token 仍有残留不一致。用户要求在同一变更中一并收束字体体系统一（A）、换肤残留补齐（B）与视觉债收尾（C），以提升全 UI 观感一致性。

## What Changes

- 统一 UI 字体解析策略，并让全局 QSS、`QFont` 与各表面标题一致使用同一解析结果（废除浮窗硬编码字体族）
- 引入字号阶梯 token（`TYPE_*`），替换设置/历史/浮窗/托盘/模型卡中的散落 `font-size` 魔法数
- 补齐主题切换时工厂控件（如 `info_callout`、`usage_tip_bar`、footnote 等）的换肤刷新
- 收尾：浮窗 chip hover 正式 token、MASTER 与 `design_tokens` 漂移对齐（含暗色 GREEN 与字体段）、`FONT_MONO` 使用面核对
- **非 BREAKING**：不改 ASR/录音/热键语义；不重组设置分页 IA

## Capabilities

### New Capabilities
- `ui-typography`: 字体解析、字号阶梯 token、跨表面排版一致性与 MASTER 字体/字号同轴
- `theme-surface-polish`: 换肤残留补齐 + chip/callout 等视觉债 token 化与 MASTER 色轴对齐

### Modified Capabilities
- （无）主规格库尚无 `ui-theme` 主规格；本变更以新增 capability 承载可观察行为

## Impact

- 代码：`sayink/ui/design_tokens.py`、`app_styles.py`、`theme.py`、`settings_styles.py`、`settings_components.py`、`settings_window.py`、`history_window.py`、`floating_window.py`、`tray_icon.py`、`model_card.py`；`design-system/MASTER.md`；相关 `tests/test_ui_styles.py` / `test_theme_resolve.py` 等
- 配置/API：无持久化键变更（除非实现选用显式字体偏好——默认不新增）
- 依赖：无新第三方依赖

---

# ui-typography-theme-polish：需求与代码事实简报

## 意图

### 目标与成功标准
- 目标：在同一变更中完成探索方向 A+B+C——统一字体与字号体系、补齐主题切换残留、收尾视觉 token/MASTER 漂移。
- 可观察的成功结果：
  1. 切换 light/dark 后，设置/历史/浮窗/托盘文字族与字号层级一致，无「半边 Segoe、半边 YaHei」观感撕裂。
  2. 主题切换后，原先构造期写死的提示条/callout 等随有效主题刷新。
  3. MASTER 字体/关键色轴与 `design_tokens` 可对表；自动化 UI/theme 测试通过。

### 边界与非目标
- 本次范围：排版 token、字体解析、四表面样式挂载、工厂控件 reapply、chip/callout/MASTER 对齐。
- 非目标：替换 PyQt6；设置分页 IA 重组；ASR/录音/热键业务；系统主题热监听增强（沿用既有 best-effort）；从零重做双主题。
- 禁止修改路径：`sayink/speech_recognizer.py`、`sayink/audio_recorder.py`、热键核心逻辑。

## 代码事实

### 现状摘要
- `design_tokens` 提供 light/dark 色轴、`FONT`/`FONT_STACK`/`FONT_MONO`、间距与控件尺寸；`FONT_STACK` 未接入 QSS。
- `app_styles.build_global_stylesheet` 仅设 `font-family: FONT`（YaHei）与 14px 基线。
- 浮窗 `_setup_ui` 用 `QFont("Segoe UI Variable", …)`，与全局 QSS 字体不一致。
- `settings_components.reload_styles` 与 `SettingsWindow.reapply_theme` 已刷新大量控件（含 `PageHero`/`ModelCard`），但工厂函数（`info_callout` 等）仍可能在构造期烘焙 stylesheet。
- `ui-theme-design-system` change 已 Complete；本变更为独立后续 polish，不复用其未归档目录作状态源。

### 可复用 / 需扩展 / 冲突
#### 可直接复用
- `theme.apply_theme` / `ThemeAware.reapply_theme` 管道；`settings_styles.reload_styles`；现有 UI/theme pytest。
#### 需要扩展
- 字体解析 API + `TYPE_*` 字号常量；浮窗去掉硬编码族；工厂控件 reapply 钩子或角色遍历；chip hover token；MASTER 表同步。
#### 需求与现状冲突
- MASTER Typography 写 Segoe-first，运行时 QSS 为 YaHei-only；需产品选定优先级后同轴。

### 挂载点候选
| 优先级 | 路径/符号 | 理由 |
|---|---|---|
| 必选 | `sayink/ui/design_tokens.py` | 字体/字号/色 token 权威 |
| 必选 | `sayink/ui/app_styles.py` / `theme.apply_theme` | 全局字体基线 |
| 必选 | `sayink/ui/floating_window.py` | 硬编码 `QFont` 主冲突点 |
| 必选 | `sayink/ui/settings_window.py` `reapply_theme` + `settings_components` 工厂函数 | 换肤残留 |
| 备选 | `sayink/ui/history_window.py` / `tray_icon.py` / `model_card.py` | 字号魔法数收敛 |
| 备选 | `design-system/MASTER.md` | 文档同轴 |

### 波及线索
- 调用方：`App` 主题应用 → 各 surface `reapply_theme`；设置页工厂控件创建路径。
- 测试：`tests/test_ui_styles.py`、`test_theme_resolve.py`、浮窗/历史/托盘相关用例需扩展字体/字号/残留换肤断言。
- 无持久化/打包依赖变更预期。

### 证据表

| 类型 | 结论 | 证据 |
|---|---|---|
| 事实 | 全局 QSS 使用 YaHei | `app_styles.build_global_stylesheet` |
| 事实 | 浮窗硬编码 Segoe UI Variable | `FloatingWindow._setup_ui` L225/L242 |
| 事实 | `FONT_STACK` 定义未用 | `design_tokens.FONT_STACK` vs search 无 QSS 引用 |
| 事实 | 字号魔法数跨表面散落 | `reload_styles`/`history`/`model_card`/`tray` font-size |
| 事实 | reapply 已覆盖 ModelCard/PageHero 等 | `SettingsWindow.reapply_theme` |
| 推断 | `info_callout` 等工厂控件切主题可能残留旧色 | verification 警告 + 构造期 `setStyleSheet` |
| 决策 | 字体优先策略 = 运行时 Segoe Variable → YaHei | 开放问题 Q1 decided `1B` |
| 决策 | MASTER 必须同步字号阶梯并验收 | 开放问题 Q2 decided `2A` |

## 消歧与闸门

### 开放问题清单

| 优先级 | 问题 | 代码事实背景 | 选项与影响（摘要） | 建议 | 状态 | 最终决策 |
|---|---|---|---|---|---|---|
| 必选 | Q1 全局字体优先策略？ | QSS=YaHei；浮窗=Segoe；MASTER=Segoe-first | A YaHei-first（中文稳）/ B 运行时解析：优先 Segoe Variable 否则 YaHei / C 维持双轨（不达 A） | B | decided | **B**（2026-07-21 用户：`1B`）— 运行时解析优先 Segoe UI Variable，否则 Microsoft YaHei UI；全表面共用 |
| 必选 | Q2 MASTER 是否必须同步正式字号阶梯表？ | 现无 TYPE_*；MASTER 仅有字体族句 | A 必须写进 MASTER 并验收 / B 仅代码 token，MASTER 只改字体句 | A | decided | **A**（2026-07-21 用户：`2A`）— MASTER 必须含字号阶梯表并作为验收 |
| 可选 | Q3 旧 change `ui-theme-design-system` 是否本变更前 archive？ | 已 Complete 未 archive | A 本变更后另开 archive / B 用户稍后自行 archive | A | deferred | 非阻塞；本变更独立状态源 |

### 澄清完整性扫描
- 已检查的适用维度：入口/主题切换正常态；失败恢复（字体探测失败回落）；兼容（无公共 API）；可证伪验收（pytest + 手工四表面）
- 由证据解决的缺失事实：reapply 已覆盖面；浮窗字体冲突；FONT_STACK 未用
- 新增开放问题及处理状态：Q1/Q2 decided；Q3 deferred（非阻塞）
- 明确不适用：鉴权/支付/隐私/迁移/热键业务
- 结论：无实质阻塞项

### 风险定级与闸门建议
- 建议车道/风险：Standard / `medium`
- 命中的风险特征：跨多 UI 模块与共享 token；影响用户可观察外观；需回归验证
- 未命中的高风险特征：无 auth/payment/privacy/migration/public API/破坏性数据操作/核心 STT 路径
- 不确定点：字体探测在少数 Win 环境上的可用性（可用回落消解）
- 闸门建议：规格闸门 → plan → 实现闸门；独立审查建议在 execute
- 可用验证：扩展 `test_ui_styles`/`test_theme_resolve`；四表面切主题冒烟
- 缺失验证：像素级视觉回归（非本变更强制）

### Explore 交接消费

- [x] `chosen_direction` → 已写入「意图」（用户确认 A+B+C 一并优化）
- [x] `non_goals` → 已写入「意图」边界
- [x] `code_anchors` → 已驱动挂载点与证据表检查
- [x] `risk_signal` → explore 为 standard-likely；本简报按代码事实重算为 Standard/medium
- [x] `unknowns` → 已写入开放问题 Q1–Q3

落点摘要：意图=A+B+C 一并；挂载=tokens/app_styles/float/settings reapply；Risk=medium；开放问题=Q1/Q2

### 状态源与工件位置
- 后端：OpenSpec change
- 路径：`openspec/changes/ui-typography-theme-polish/`
- 闸门记录：
  - 规格批准状态 = **已批准**
  - 批准人：user（对话回复 `ok`）
  - 批准时间：2026-07-21T09:52:42+08:00
  - binds_to_revision（规格）：`2dc429bfb6f2f0c9b3bc0349bfb12e4958034c948f2f171c730bfd547ede0107`
  - 实现批准状态 = **已批准**
  - 批准人：user（对话回复 `ok`）
  - 批准时间：2026-07-21T09:56:00+08:00
  - binds_to_revision（实现）：`eed928ce3fd89304fbc961b62134c5fdf71744a97e3a256aac320ffea9f73684`
  - accepted_warning_ids：[`W-g8-overlap-ui-theme`]
- 既有 `ui-theme-design-system` 不作为本变更状态源（Complete；archive 非本变更阻塞）

### 实现就绪审查（plan）
- 结论：**就绪（条件：用户接受 G8 路径重叠警告）** / 风险 medium
- 阻塞项：无（G8 重叠按协议默认阻塞，但旧 change 已 Complete/verified 且无未完成任务 → 降记为**警告项**，须闸门显式接受）
- 警告项：
  - `W-g8-overlap-ui-theme`：active change `ui-theme-design-system`（Complete）与本变更共享 `sayink/ui/*`、`design-system/MASTER.md`；接受「旧变更已收工、本变更为续作 polish、不并行改同一文件」序列风险
- 建议项：实现后可 archive `ui-theme-design-system`
- G1：唯一状态源本 change — pass
- G2：任务含真实路径/符号 — pass
- G3：每任务有验证命令与预期 — pass
- G5：规格闸门已批；实现闸门待批 — pending
- G8：见警告项
- 后端：`openspec validate --strict` valid；artifacts 4/4
- 澄清扫描（plan）：无用户必决技术项；D1–D5 Agent 已决；无回 Frame

## Why

SayInk 四表面（设置 / 历史 / 浮窗 / 托盘）仍以单轴浅色 tokens 与大量静态 QSS 拼装为主，缺少可切换主题与统一设计系统权威源。用户已选定用 ui-ux-pro-max 重定 MASTER、替换现有 tokens，并引入暗色与系统跟随，使全应用视觉一致、可主题化。

## What Changes

- 引入 ui-ux-pro-max 生成的设计系统 MASTER，并据此**整体替换** `design_tokens` 权威色板/间距/字体轴（**BREAKING** 相对当前 cool-axis 视觉基线与硬编码色值测试）
- 新增外观主题能力：浅色 / 暗色 / 跟随系统；偏好可配置且可在运行时切换
- 一次覆盖四表面：`SettingsWindow`、`HistoryWindow`、`FloatingWindow`、`TrayIcon`（含托盘菜单样式）
- 全局与表面样式从「模块 import 时固化的单主题字符串」演进为「按当前有效主题解析的 token/QSS」
- 既有 `settings-control-alignment` 的布局/对齐/滚动条行为要求在新主题下仍须成立（结构验收不因换肤失效）

## Capabilities

### New Capabilities

- `ui-theme`: 外观主题模式（light / dark / system）、有效主题解析、偏好持久化、设置入口、运行时应用到四表面与全局 stylesheet

### Modified Capabilities

- `settings-control-alignment`: 在 light/dark（及 system 解析结果）下，历史数值控件等宽、右缘对齐、滚动条 AsNeeded 等既有要求仍须满足；必要时补充暗色场景表述（不削弱原结构验收）

## Impact

- 代码：`sayink/ui/design_tokens.py`、`app_styles.py`、`settings_styles.py`、`settings_window.py`、`settings_components.py`、`history_window.py`、`floating_window.py`、`tray_icon.py`、`model_card.py`、`nav_icons.py`、`hotkey_edit.py`；入口 `sayink/main.py`（`GLOBAL_APP_STYLESHEET`）；可能 `sayink/config.py` / `sayink/app.py`（主题偏好与应用）
- 配置：`DEFAULT_CONFIG` 今日无 theme 键（事实）；将新增主题偏好键（形态待产品决定）
- 测试：`tests/test_ui_styles.py` 含硬编码 `#2563EB` 等色值断言，需随新 tokens/主题更新
- 文档工件：ui-ux-pro-max MASTER 落盘路径待产品决定
- 非目标（默认）：不改 ASR/录音/热键业务逻辑；不换 UI 框架；不做设置分页信息架构重组

---

# ui-theme-design-system：需求与代码事实简报

## 意图

### 目标与成功标准

- 目标：用 ui-ux-pro-max 生成并落地新设计系统 MASTER，替换现有 design tokens；为 SayInk 提供浅色/暗色/跟随系统主题，并一次应用到设置、历史、浮窗、托盘四表面，使视觉与主题行为一致。
- 可观察的成功结果：
  - 默认主题模式为 `system`；用户可在设置→通用→「外观」选择 `light` / `dark` / `system`
  - 主题偏好写入 `config.json`，重启后仍生效；非法值回落 `system`
  - 切换主题后，四表面（设置/历史/浮窗/托盘菜单）与全局控件呈现与有效主题一致的配色/层级；浮窗随有效主题换肤
  - 「跟随系统」时，有效主题按 Windows 浅/深色外观解析（启动与手动切换必达；系统外观热更新交设计推荐）
  - 设置页既有对齐/等宽/滚动条 AsNeeded 行为在 light 与 dark 下仍成立
  - 仓库含 `design-system/MASTER.md`，实现 token 与之可追溯一致

### 边界与非目标

- 本次范围：设计系统重定基线 + 主题能力 + 四表面视觉/样式应用；设置页作为四表面之一纳入（含跨主题下的既有对齐验收延续）
- 非目标：
  - 不改 ASR / 录音 / 热键 / 转写业务逻辑与历史数据模型（主题偏好的最小配置键除外）
  - 不引入 Web/移动壳，不替换 PyQt6
  - 不做设置分页/导航信息架构大翻
  - 不在定框阶段写实现代码
- 禁止修改路径（预置）：`sayink/speech_recognizer.py`、`sayink/audio_recorder.py`、热键核心逻辑（除非证据证明主题应用必须触碰——默认禁止）

## 代码事实

### 现状摘要

- UI 集中在 `sayink/ui/`（12 个文件节点）；主表面为 `SettingsWindow`、`HistoryWindow`、`FloatingWindow`、`TrayIcon`。
- `design_tokens.py` 声明「classic desktop utility」单轴浅色 tokens；另有独立 `FLOAT_*` 暗色叠层常量（浮窗专用）。
- `app_styles.GLOBAL_APP_STYLESHEET` 在模块 import 时用浅色 token 拼成静态字符串；`main.py` 在 `app.setStyle("Fusion")` 后 `app.setStyleSheet(GLOBAL_APP_STYLESHEET)`。
- 各表面大量 `setStyleSheet(...)` / 组件级常量（`settings_styles.py`、`settings_components.py` 等），今日无 theme 解析层。
- `Config.DEFAULT_CONFIG` 无外观/主题字段。
- 已归档能力 `settings-control-alignment` 约束设置页数值控件等宽、右缘对齐、滚动条 AsNeeded。
- `tests/test_ui_styles.py` 对样式字符串与品牌色（如 `#2563EB`）做静态断言。

### 可复用 / 需扩展 / 冲突

#### 可直接复用

- 四表面挂载点与组件拆分（Settings / History / Floating / Tray）
- Fusion + 全局 stylesheet 入口（`main.py`）
- `settings-control-alignment` 的结构验收意图（等宽/对齐/AsNeeded）
- `test_ui_styles` 作为样式回归骨架（断言值需更新）

#### 需要扩展

- token 权威源：单文件浅色常量 → MASTER 驱动的多主题 token（至少 light/dark）
- 主题解析与应用：配置键 + 系统外观探测 + 运行时刷新全局/表面 QSS
- 浮窗：今日独立 `FLOAT_*` 轴，需与统一主题策略对齐（产品待决）
- ui-ux-pro-max MASTER 落盘与实现同步机制

#### 需求与现状冲突

- 用户要求 **3A 整体替换** tokens，与近期 cool-axis / stitch 视觉基线及硬编码色值测试冲突（预期 **BREAKING** 视觉与测试更新）
- 「跟随系统 / 暗色」与当前「仅浅色全局 + 浮窗常暗」模型冲突

### 挂载点候选

| 优先级 | 路径/符号 | 理由 |
|---|---|---|
| 必选 | `sayink/ui/design_tokens.py` | token 权威源替换 |
| 必选 | `sayink/ui/app_styles.py` / `sayink/main.py`（`GLOBAL_APP_STYLESHEET`） | 全局换肤入口 |
| 必选 | `sayink/ui/settings_window.py` + `settings_styles.py` + `settings_components.py` | 设置表面 + 既有对齐验收 |
| 必选 | `sayink/ui/history_window.py` | 历史表面 |
| 必选 | `sayink/ui/floating_window.py` | 浮窗表面；FACE 与 FLOAT_* |
| 必选 | `sayink/ui/tray_icon.py`（`_menu_stylesheet`） | 托盘菜单 |
| 必选 | `sayink/config.py`（`DEFAULT_CONFIG` / `Config.get|set`） | 主题偏好持久化 |
| 备选 | `sayink/app.py` | 设置变更广播 / 生命周期 |
| 备选 | `tests/test_ui_styles.py` | 样式/主题回归 |

### 波及线索

- 所有 import `design_tokens` 的 UI 模块与硬编码色值测试
- 运行时主题切换需刷新已打开窗口与托盘菜单（否则出现半换肤）
- 配置文件新增键：旧配置兼容（缺省合并）属轻量迁移面
- `settings-control-alignment` 主规格与增量 delta 需对齐暗色场景表述
- 打包/资源：若 MASTER 进仓库，影响文档与审查面，不必然进运行时包

### 证据表

| 类型 | 结论 | 证据 |
|---|---|---|
| 事实 | UI 四表面文件存在 | Memory `sayink/ui` file_tree；路径如上 |
| 事实 | tokens 为单轴浅色 + 独立 FLOAT_* | `sayink/ui/design_tokens.py` |
| 事实 | 全局 QSS 在 main 一次性 setStyleSheet | `sayink/main.py` L148–151；`app_styles.py` |
| 事实 | DEFAULT_CONFIG 无 theme 键 | `sayink/config.py` `DEFAULT_CONFIG` |
| 事实 | 设置对齐规格已存在 | `openspec/specs/settings-control-alignment/spec.md` |
| 事实 | 样式测试绑定旧品牌色 | `tests/test_ui_styles.py`（`#2563EB` 等） |
| 决策 | 四表面 + 主题 + 新 MASTER 替换 tokens | Explore 用户答 1A 2C 3A → 见「开放问题清单」种子决策 |
| 推断 | 运行时切换需显式刷新已创建 widgets | 大量组件构造期 `setStyleSheet`；待设计确认机制 |

## 消歧与闸门

### 开放问题清单

| 优先级 | 问题 | 代码事实背景 | 选项与影响（摘要） | 建议 | 状态 | 最终决策 |
|---|---|---|---|---|---|---|
| 必选 | 主题默认模式？ | 无既有 theme 配置 | A 跟随系统 / B 默认浅色 / C 默认暗色 | A 跟随系统（贴合 2C） | decided | A — 默认跟随系统（用户 1A） |
| 必选 | 主题偏好是否持久化？ | Config 可扩展 DEFAULT_CONFIG | A 写入 config.json / B 仅会话内 | A 持久化 | decided | A — 写入 config.json（用户 2A） |
| 必选 | 浮窗与统一主题关系？ | 已有独立 FLOAT_* 暗色轴 | A 随有效主题换肤 / B 保持常暗叠层只微调 / C 浮窗除外 | A 随有效主题（与「四表面一致」一致） | decided | A — 随有效主题换肤（用户 3A） |
| 必选 | 设置中的主题入口？ | 设置通用页已有多组选项 | A 通用页新增「外观」/ B 仅托盘菜单 / C 两处都有 | A 通用页外观组 | decided | A — 设置→通用→「外观」组（用户 4A） |
| 必选 | ui-ux-pro-max MASTER 是否入库？ | 仓库尚无 design-system/ | A 提交 `design-system/MASTER.md` / B 仅本地生成不入库 | A 入库作为权威文档 | decided | A — 提交 `design-system/MASTER.md`（用户 5A） |
| 可延后 | 系统外观变化时是否热更新（无需重启）？ | 今日无监听 | 热更新 vs 仅启动时解析 | 倾向热更新；可在设计阶段定技术方案 | deferred | 非阻塞：验收至少保证启动与手动切换正确；热更新由 design 推荐实现 |
| 可延后 | 托盘菜单是否完整跟随主题？ | `_menu_stylesheet` 独立 | 跟随 vs 系统原生 | 跟随统一主题 | deferred | 默认跟随统一主题（与四表面一致）；实现细节交 design |

### 澄清完整性扫描

- 已检查的适用维度：使用者与入口；正常主题切换与默认；失败/非法配置值；配置保存与重启；既有规格兼容；验证面；非目标边界；公共契约（配置键向后兼容缺省合并）
- 由证据解决的缺失事实：无现成 theme 配置；全局样式入口在 main；浮窗有独立暗色 tokens；样式测试绑旧色
- 新增开放问题及处理状态：五题必选均 `decided`（用户 1A–5A）；热更新与托盘细节保持 `deferred` 非阻塞；非法 theme 值 → 非阻塞默认回落 `system`（记入规格）
- 明确不适用 / 不在范围：支付/鉴权/隐私导出；ASR 业务；非 Windows 系统主题 API；设置分页 IA 重组
- 结论：无实质阻塞项

### 风险定级与闸门建议

- 建议车道/风险：**High / high**
- 命中的风险特征：
  - 跨模块多表面 + 共享样式基础设施（影响面不可局部）
  - 配置 schema 新增键（轻量迁移/兼容）
  - 核心用户可见主路径（托盘/浮窗/设置）视觉与交互感知变更
  - **BREAKING** 相对既有视觉基线与规格/测试色值契约
  - Explore `risk_signal=high-likely` 仅作线索；本定级由上述代码事实重算
- 未命中的高风险特征：鉴权、支付、权限模型、隐私导出、破坏性数据删除、公共网络 API 协议
- 不确定点：系统主题监听 API 可靠性；浮窗常暗 vs 统一主题的产品取舍（开放问题）；MASTER 与 QSS 映射工作量
- 闸门建议：规格闸门（本阶段）→ plan 强化 rollback/验证/安全（无敏感数据面则记 N/A）→ execute 独立审查；不得降为 Quick
- 可用验证：扩展 `test_ui_styles`；主题切换/config 往返单测；设置对齐场景在 dark 下复验；手工四表面截图对照
- 缺失验证：目前无主题相关测试；无系统外观变更自动化（可人工或后续补）

### Explore 交接消费

- [x] `chosen_direction` → 已写入「意图」（四表面 + 主题 + ui-ux-pro-max MASTER 替换 tokens）
- [x] `non_goals` → 已写入「意图」边界
- [x] `code_anchors` → 已驱动「代码事实」/「挂载点候选」（design_tokens、四表面、config、main、settings-control-alignment、test_ui_styles）
- [x] `risk_signal` → 仅线索；本简报按代码事实重算为 High/high
- [x] `unknowns` → 已写入「开放问题清单」或 deferred

落点摘要：意图=主题+MASTER+四表面；挂载=ui/*+config+main；Risk=High；开放问题=5 必选 decided

### 状态源与工件位置

- 后端：OpenSpec change
- 路径：`openspec/changes/ui-theme-design-system/`
- Explore handoff：`ui-ux-pro-max-theme-explore-2`（direction selected；不落盘）
- 闸门记录：
  - 规格批准状态 = **已批准**
  - 批准人 = 用户（会话回复「继续」= 批准范围并进入设计）
  - 批准时间 = 2026-07-17T15:56:49Z
  - binds_to_revision = 见同目录 `handoff.json` 的 `gate_status.binds_to_revision`（写入批准后重算）
  - accepted_warning_ids = []
  - 下一阶段 = `delivery-plan-tasks`（设计/任务已写，待实现闸门）
  - 实现就绪审查（plan 内部）：就绪；阻塞项=无；G8=无其他 active change；High 五面自检通过（见 `design.md`）
  - 实现批准状态 = **已批准**
  - 实现批准人 = 用户（会话回复「开始实施」）
  - 实现批准时间 = 2026-07-17T16:03:00Z
  - 实现批准 binds_to_revision = 见 `handoff.json`（放行时重算）
  - accepted_warning_ids = ["system-theme-hot-update-best-effort", "dark-alignment-manual-tolerance"]
  - 下一阶段 = `delivery-execute-verify`（已 verified；归档 deferred）
  - 实现结果 = **verified**（见 `verification.md` / `handoff.json`）

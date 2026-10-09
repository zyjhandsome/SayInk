# ui-typography-theme-polish：技术实施计划

**深度说明（Standard/Medium 比例化）：** 单包 UI、无红线、文件集有界 → 采用紧凑 design（决策记录 + 挂载点 + 验证矩阵），不写仪式化架构章。完整 G1–G3/G5/G8 就绪检查仍执行。

## 已批准目标与约束
- 目标：A 字体解析（Segoe UI Variable → YaHei）+ `TYPE_*` 字号；B 工厂控件换肤；C chip token 与 MASTER 同轴。
- 非目标：PyQt6 替换、设置 IA、ASR/录音/热键、系统主题热监听增强。
- 风险/闸门：Standard / medium；规格闸门已于 2026-07-21 用户 `ok` 批准。

## 已刷新代码事实
| 结论 | 证据 | 新鲜度 |
|---|---|---|
| 全局 QSS 用 `FONT=YaHei` | `app_styles.build_global_stylesheet` | HEAD `abfd7ea` |
| 浮窗 `QFont("Segoe UI Variable")` | `floating_window._setup_ui` | 同上 |
| `FONT_STACK` 未接入 | `design_tokens` + search | 同上 |
| `SettingsWindow.reapply_theme` 已 walk 多类 `viRole` / ModelCard | `settings_window.py` ~130–330 | 同上 |
| 工厂 `info_callout` 等仍构造期 `setStyleSheet` | `settings_components.info_callout` | 同上 |
| 浮窗 chip hover 用 `CHIP_BG.replace` | `FloatingWindow.reapply_theme` | 同上 |
| 无现成 `QFontDatabase` 探测工具 | Memory search 0 hits | 同上 |

## 技术决策清单
| ID | 待决事项 | 决策归属 | 实质影响 | 选项与建议 | 状态 | 最终结论与记录 |
|---|---|---|---|---|---|---|
| D1 | 字体解析挂载位置 | Agent | 低 | A 独立 `font_resolve.py` / B 放 `design_tokens`+`theme.apply_theme` 调用 | decided | **B** — 避免新模块扩散；`resolve_ui_font_family()` 在 tokens 或 theme，apply 时刷新模块级 `FONT`/`FONT_DISPLAY` |
| D2 | 字号阶梯命名 | Agent | 低 | 对齐现网魔法数 | decided | `TYPE_CAPTION=11` `TYPE_FOOTNOTE=12` `TYPE_BODY_SM=13` `TYPE_BODY=14` `TYPE_TITLE_SM=15` `TYPE_TITLE=16` `TYPE_TITLE_LG=20` `TYPE_DISPLAY=22` `TYPE_ICON_LG=17`（浮窗关闭钮） |
| D3 | 工厂控件换肤策略 | Agent | 中 | A objectName+reapply 函数 / B 全改 QSS 属性选择器 | decided | **A** — `infoCallout`/`usageTipBar` 等设稳定 objectName；抽出 `paint_*`/`reapply_*`；`SettingsWindow.reapply_theme` `findChildren` 调用；footnote 已有 `viRole` 则走现有 walk |
| D4 | chip hover/press | Agent | 低 | 正式 token | decided | 增加 `CHIP_BG_HOVER`/`CHIP_BG_PRESS`（light/dark 轴）；删除 string replace |
| D5 | MASTER 暗色 GREEN | Agent | 低 | 文档跟代码 | decided | MASTER 改为 `#16A34A` 并注「quiet mid-green」；与 runtime 一致 |
| D6 | 与 Complete change 路径重叠 | 用户可接受序列 | 警告 | 旧 change Complete 无未完成任务 | decided | 警告项：同 UI 路径；接受「旧 change 已 verified、本变更续作 polish」序列（gate 时列入 accepted_warning_ids） |

## 方案比较
### 方案 A — Token + resolve 就地扩展（选定）
- 形态：扩展现有 `design_tokens`/`theme`/`reapply` 管道
- 收益：改动面清晰、复用 apply 管线、测试可挂现有 suite
- 成本/风险：需清点散落 font-size；字体探测需 GUI fixture
- 可逆性：高（纯视觉）
- 验证：pytest UI/theme + MASTER 抽样断言

### 方案 B — 新建完整 typography 子系统
- 成本更高、与现网 QSS 字符串拼接模式冲突；不选。

## 最终决策
- 选定方案：A
- 选择理由：与已落地主题管线一致；满足 1B/2A 产品决策
- 决策来源：规格闸门批准 + Agent D1–D5

## 集成方式与数据流/控制流
1. 启动 / `apply_theme` → `resolve_ui_font_family()` → 写回 `dt.FONT`/`FONT_DISPLAY` → `build_global_stylesheet` / `reload_styles` → surfaces `reapply_theme`
2. 设置工厂控件创建时调用 paint 助手；主题切换时 `reapply_theme` 再次调用同一助手
3. 浮窗 `QFont(resolved, TYPE_*)` + chip tokens

## 接口与状态模型
- `resolve_ui_font_family(*, preferred: Sequence[str] | None = None) -> str`
- Preferred 默认：`("Segoe UI Variable", "Microsoft YaHei UI", "Segoe UI")`
- 探测：`QFontDatabase.families()` 成员检测；无 QApplication 时回落 YaHei（测试友好）
- `TYPE_*`：int px，供 f-string QSS 与 `QFont` point/pixel（浮窗保持 px 语义与现网一致：`QFont(family, px)` 在 Qt 中为 pointSize——**保持现有 `QFont(family, n)` 调用形态**，仅替换 family 与 n 的来源为 token，避免无意改变字号度量）

## 失败处理与可观测性
- 字体探测异常 → 记 warning log → YaHei；不阻断启动
- 无新增用户可见错误 UI

## 兼容、迁移与回滚
- 无配置键变更；回滚=git revert 视觉文件
- 不适用数据迁移

## 安全与性能
- 不适用鉴权；字体探测 O(families) 一次/主题应用，可模块缓存至下次 apply

## 验证策略
- 单测：resolver 优先/回落；stylesheet 含 resolved family；float 无硬编码 Segoe 字面量；chip QSS 含 `CHIP_BG_HOVER`；dark apply 后 callout 色含 dark token；MASTER 含 TYPE 阶梯与字体策略句
- 回归：`pytest tests/test_theme_resolve.py tests/test_ui_styles.py`（及现有 float/history/tray 相关）
- 手工：设置切主题 → 观察 callout/浮窗/托盘

## 需求追溯
| 需求/场景 | 设计要素 | 任务 | 验证 |
|---|---|---|---|
| ui-typography: Unified font / float match / fallback | resolve + apply 写回 FONT | T1 | test resolver + float family |
| ui-typography: TYPE scale rebuild | TYPE_* + reload_styles/model/history/tray | T2 | test_ui_styles size/token |
| ui-typography: MASTER alignment | MASTER 表 | T4 | file/assert 抽样 |
| theme-surface-polish: factory retheme | paint/reapply + findChildren | T3 | dark apply callout |
| theme-surface-polish: chip tokens | CHIP_* | T3 | reapply stylesheet |
| theme-surface-polish: MASTER green | MASTER sync | T4 | 文档对照 |

## 已知风险与非目标
- [Risk] 无头 CI 无真实 Segoe → 测 mock `families` / 回落路径 — Mitigation: 注入探测函数
- [Risk] 与 Complete `ui-theme-design-system` 路径重叠 — Mitigation: 警告 + 序列接受；不并行改同一文件
- 非目标见简报

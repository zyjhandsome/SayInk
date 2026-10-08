## Context

SayInk 为 PyQt6 托盘应用：全局 `GLOBAL_APP_STYLESHEET` 在 `main.py` 一次性注入；各表面大量在构造期 `setStyleSheet`；`design_tokens.py` 为单轴浅色 + 独立 `FLOAT_*` 常暗常量；`Config.DEFAULT_CONFIG` 无主题键。规格已批准引入 ui-ux-pro-max `design-system/MASTER.md`、替换 tokens，并支持 `light`/`dark`/`system` 覆盖四表面。

## Goals / Non-Goals

**Goals:**

- MASTER 入库并驱动 light/dark token
- 主题模式持久化与有效主题解析（含非法值回落）
- 设置→通用→外观入口；切换后即时应用到界面
- 设置 / 历史 / 浮窗 / 托盘菜单随有效主题呈现
- 双主题下保持 `settings-control-alignment` 结构验收

**Non-Goals:**

- ASR / 录音 / 热键业务逻辑
- 设置分页 IA 重组
- 非 Windows 系统主题 API
- 替换 UI 框架

---

# ui-theme-design-system：技术实施计划

## 已批准目标与约束

- 目标：见 `proposal.md`「意图」与 `specs/ui-theme`、`specs/settings-control-alignment`
- 非目标：见简报边界；禁止改 ASR/录音/热键核心
- 风险/闸门：High；规格闸门已通过（用户「继续」，`binds_to_revision` 见 `handoff.json`）

## 已刷新代码事实

| 结论 | 证据 | 新鲜度 |
|---|---|---|
| 全局 QSS 入口 `main.py` L148–151 | 源码 | HEAD `fc4a403` |
| tokens 单轴 + FLOAT_* | `design_tokens.py` | 已读 |
| 无 theme 配置键 | `config.py` `DEFAULT_CONFIG` | 已读 |
| 设置变更回调 `_on_settings_changed` 未处理外观 | `app.py` L909–927 | 已读 |
| 通用页挂载点 `_create_general_page`（pref/history 组前可插外观组） | `settings_window.py` L212+ | 已读 |
| 托盘菜单 `_menu_stylesheet` | `tray_icon.py` | Memory/search |
| 样式测试绑 `#2563EB` | `tests/test_ui_styles.py` | 已读 |
| 无其他 active change（G8） | `openspec list` 仅本 change | 2026-07-17 |

## 技术决策清单

| ID | 待决事项 | 决策归属 | 实质影响 | 选项与建议 | 状态 | 最终结论与记录 |
|---|---|---|---|---|---|---|
| D1 | 配置键名与取值 | Agent | 持久化形态 | `appearance.theme_mode` ∈ {light,dark,system} | decided | 采用嵌套键，缺省 `system`；未知值运行时回落 `system` |
| D2 | Token 结构 | Agent | 全 UI | A 双表 LIGHT/DARK + 解析函数 / B 运行时对象 | decided | A：`design_tokens` 提供 `tokens_for(effective)`；模块级兼容别名指向当前有效主题（由 apply 刷新） |
| D3 | 主题应用中枢 | Agent | 刷新面 | 新建 `sayink/ui/theme.py`：`resolve_effective_theme` / `apply_theme(app, windows...)` | decided | 单一入口，避免各表面各自读配置 |
| D4 | Windows 系统外观探测 | Agent | system 模式 | `QSettings` 读 `AppsUseLightTheme`（或等价） | decided | 失败时回落 `light` 并打日志 |
| D5 | 系统外观热更新 | Agent | 体验 | 尽力：监听/定时轻量轮询；非阻塞 | decided | 启动+手动切换必达；热更新 best-effort，失败不挡验收 |
| D6 | 浮窗换肤策略 | Agent（产品已决随主题） | 浮窗视觉 | 有效主题 light/dark 各一套 float 表面 token，写入 MASTER | decided | 废弃「仅常暗唯一轴」；保留适度对比度 |
| D7 | MASTER 生成方式 | Agent | 文档 | 实施时运行 ui-ux-pro-max `--design-system --persist -p SayInk`，再人工对齐桌面工具约束写入仓库 | decided | 查询关键词：desktop utility voice transcription productivity dark mode |

无用户必决技术题（成本/不可逆部署无实质分叉）；无回 Frame 项。

## 方案比较

### 方案 A — 主题中枢 + 双主题 token 表（推荐）

- 形态：MASTER → light/dark token 表；`theme.apply_theme` 刷新 `QApplication` stylesheet 并通知已打开表面 `reapply_theme()`
- 收益：单一真相源；表面可纵向切片验收
- 成本/风险：需梳理构造期 QSS；测试色值更新面大
- 可逆性：git revert；配置键可忽略
- 验证：单元测 resolve；样式/窗口测；手工四表面

### 方案 B — 仅替换全局 QSS，表面硬编码不动

- 形态：只改 `GLOBAL_APP_STYLESHEET`
- 收益：改动小
- 成本/风险：不满足四表面规格；半换肤
- 可逆性：高
- 验证：无法通过规格场景

### 方案 C — 每控件动态 palette 无 QSS

- 形态：Qt palette 驱动
- 收益：理论统一
- 成本/风险：与现有大量 QSS 冲突，重写成本高于 A
- 可逆性：中
- 验证：难与现测对齐

## 最终决策

- 选定方案：**A**
- 理由：满足规格、可追溯 MASTER、与现有 QSS 架构兼容、可纵向切片
- 未选：B 不达标；C 成本过高
- 决策来源：Agent 技术决策（规格边界内）

## Decisions（OpenSpec）

1. **Config**：`appearance.theme_mode` 默认 `system`；`Config._merge_defaults` 兼容旧文件。
2. **theme.py**：解析有效主题；读系统外观；`apply_theme` 设置全局 QSS 并调用注册表面的 `reapply_theme`。
3. **Tokens**：由 MASTER 映射生成 light/dark；导出 `tokens_for`；更新 `app_styles`/`settings_styles` 为函数或按当前主题重建字符串。
4. **设置 UI**：在 `_create_general_page` 中于偏好组附近新增「外观」`settings_section`，三选一（Radio/Combo）；变更 → `config.set` → `apply_theme`；并扩展 `_on_settings_changed` 或专用信号以免打断录音流程时仍能换肤（换肤不得依赖停录；Agent：主题变更走轻量路径，不走完整 `_on_settings_changed` 重配置 STT）。
5. **浮窗/历史/托盘**：实现 `reapply_theme` 或重建菜单 stylesheet。
6. **测试**：`test_theme_resolve.py`；更新 `test_ui_styles.py` 去掉写死旧蓝或改为断言 token API；暗色对齐场景补充可自动化部分。

## 集成方式与数据流/控制流

```text
启动 main
  → Config 读 appearance.theme_mode
  → resolve_effective_theme()
  → apply_theme(app)  # 全局 QSS + tokens 当前轴
  → App 创建各表面（构造时读当前 tokens）

用户改外观
  → SettingsWindow 写 config
  → apply_theme(app, settings, history, floating, tray)
  → 各表面 reapply_theme()

system 模式 + 系统外观变化（best-effort）
  → 重新 resolve → 若有效主题变化则 apply_theme
```

## 接口与状态模型

- `ThemeMode = Literal["light","dark","system"]`
- `EffectiveTheme = Literal["light","dark"]`
- `resolve_effective_theme(mode, *, system_is_light: bool | None) -> EffectiveTheme`
- `apply_theme(app: QApplication, *, surfaces: Iterable[ThemeAware] = ()) -> None`
- `ThemeAware.reapply_theme(self) -> None`（协议/duck typing）

## 失败处理与可观测性

- 系统外观读取失败 → 有效主题 `light` + warning 日志
- 非法 mode → `system` 再解析
- `apply_theme` 单表面失败 → 记录异常，尽量继续其余表面
- 不引入用户可见错误弹窗（避免打扰托盘工作流）

## 兼容、迁移与回滚

- 迁移：旧 `config.json` 缺键 → 默认 `system`（已有 merge）
- 回滚：还原 commit；删除/忽略 `appearance` 键即可回退行为；MASTER 随代码回滚
- **BREAKING**：视觉与 `test_ui_styles` 旧色值断言必须同变更更新

## 安全与性能

- 安全：无新网络/隐私面；配置仅本地主题枚举 — **N/A（无红线数据面）**，记入状态源
- 性能：换肤为同步 UI 线程样式刷新；避免在音频回调中调用；系统轮询若采用则间隔 ≥ 2s 且仅 `system` 模式

## 验证策略

- 单元：resolve / 非法值 / 默认
- 样式：stylesheet 非空；焦点环仍在；token API 与 MASTER 主色一致（抽样）
- UI：设置外观组存在；切换后设置窗 stylesheet 含暗色轴特征色
- 回归：`pytest tests/test_ui_styles.py tests/test_theme*.py`；手工四表面清单
- 对齐：暗色下等宽/右缘（可测属性 width；右缘允许手工）

## 需求追溯

| 需求/场景 | 设计要素 | 任务 | 验证 |
|---|---|---|---|
| 主题模式与有效主题解析（含默认 system/非法回落） | D1/D3/D4 | T1 | `test_theme_resolve` |
| 主题偏好持久化 | D1 + Config | T1 | config 往返测 |
| 设置页外观入口 + 即时应用 | D3 + 设置 UI | T2 | UI/样式测 + 手工 |
| 四表面随有效主题（含浮窗） | D2/D6 + reapply | T3–T5 | 样式/手工 |
| MASTER 权威文档 | D7 | T0 | 文件存在断言 |
| 结构对齐双主题 | 既有控件宽度常量 + 暗色样式 | T6 | test + 手工 |

## 已知风险与非目标

- [Risk] 构造期 QSS 漏刷新 → 半换肤 — Mitigation：ThemeAware 清单 + 最终手工四表面
- [Risk] MASTER 与 token 漂移 — Mitigation：T0 先 MASTER再映射；抽样断言
- [Risk] `_on_settings_changed` 过重 — Mitigation：主题变更独立轻量路径
- 非目标：见上

## 澄清完整性扫描（plan）

- 已检查：成本/部署（无新依赖除已有 PyQt6）；迁移（缺省键）；回滚；安全（N/A）；验证；并行重叠（无）
- 证据解决：挂载点与配置入口已确认
- 不适用：支付/鉴权/多租户
- 剩余阻塞：无

## 实现就绪审查（Agent 内部，High 五面）

### 结论

- 就绪（待用户实现闸门放行）
- 风险等级：high
- 所需批准：实现 go（单题）

### 阻塞项

- （无）

### 警告项

- 系统外观热更新为 best-effort，规格以启动+手动切换为准
- 右缘对齐 ±1px 在暗色下以手工/属性测为主

### 建议项

- 后续可为历史/浮窗增加页面级 `design-system/pages/*.md` override（非本变更必须）

### High 五面自检（内部）

- design：完整 — 通过
- tasks：纵向切片且含验证命令 — 见 `tasks.md` — 通过
- rollback：git revert + 忽略配置键 — 通过
- security：无新敏感面 — N/A 通过
- validation：可证伪命令已挂到任务 — 通过

### G8

- 其他 active changes：无 — 通过

## Open Questions

- （无阻塞）热更新具体 API 在实施中按 D5 选型并记录于 verification

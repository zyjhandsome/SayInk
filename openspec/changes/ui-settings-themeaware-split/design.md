# ui-settings-themeaware-split：技术实施计划

深度：Standard/medium 紧凑计划（决策记录 + 挂载点 + 验证矩阵）。理由：无红线、无 schema、无新依赖；文件集限定在 `sayink/ui/*` 与既有 UI 测试。

## 已批准目标与约束
- 目标：四表面 ThemeAware 广播；设置按页拆分；删除 Settings 窗级 `viRole` 巡检链。
- 非目标：token/字体重做、IA 重排、听写/润色业务、像素截图基建。
- 风险/闸门：Standard / medium；规格已批；G8 与未归档 UI change 重叠，用户已在规格闸门接受 `W-g8-overlap-ui-theme`。
- 禁止路径：`speech_recognizer.py`、`audio_recorder.py`、热键核心。

## 已刷新代码事实
| 结论 | 证据 | 新鲜度 |
|---|---|---|
| Settings `reapply_theme` 114–338 行角色枚举 | `settings_window.py` | HEAD 4bc0431 |
| History/Tray 换肤已短；Floating 自绘 42 行 | 各 `reapply_theme` | 同左 |
| `ThemeAware` 为 Protocol；`apply_theme` 调 surface.reapply_theme | `theme.py` L25–144 | 同左 |
| 四页在 `_setup_ui` 直接 `addWidget(_create_*_page())` | `settings_window.py` L385–388 | 同左 |
| 测试夹具依赖 `SettingsWindow` 实例属性 | `test_settings_general.py` | 同左 |

## 技术决策清单
| ID | 待决事项 | 决策归属 | 实质影响 | 选项与建议 | 状态 | 最终结论与记录 |
|---|---|---|---|---|---|---|
| D1 | ThemeAware 形态 | Agent | 无用户可察差异 | Protocol 保持 vs 改 ABC | decided | 保持 Protocol；duck-type |
| D2 | 拆页落点 | Agent | 仅内部模块 | `settings_pages/` 包 vs mixin | decided | `sayink/ui/settings_pages/{general,model,polish,about}.py`，`build_*_page(win)` 仍把控件挂到 `win`，避免测试大改 |
| D3 | 角色样式迁出位置 | Agent | 满足「窗级不再枚举」 | 控件自刷 vs 组件树 helper | decided | 工厂/角色控件实现 `reapply_styles`；`settings_components.reapply_subtree(root)` 仅作发现已实现钩子的子树广播，不含 viRole 长链 |
| D4 | 其它三表面改动量 | Agent | Q1=C 协议对齐 | 重写 vs 对齐接口 | decided | History/Tray/Float 已有 `reapply_theme`；补测试与构造期残留，不重写短实现 |
| D5 | 视觉证据 | Agent（Q2=A） | 无像素基建 | 截图 G9 vs 样式断言+手工 | decided | substitute：pytest 样式/主题断言 + 手工四表面清单写入 verification.md |

无用户必决技术项；无需回 Frame。

## 方案比较
### 方案 A（选定）：广播 + 拆页包 + 三表面对齐
- 形态：Settings 换肤变广播；页建造迁到 `settings_pages/`；其它表面保持短 `reapply_theme` 并加回归。
- 收益：直接删除 225 行枚举；后续加控件只实现钩子。
- 成本/风险：设置双文件仍大，但职责切开；测试需跟路径走。
- 可逆性：可 git 回退；无数据迁移。
- 验证：既有设置/主题/四表面 pytest。

### 方案 B：只搬枚举到 helper，不拆页
- 被拒：与已批 Q1=C（含拆页）不符。

### 方案 C：四表面全部重写成统一基类
- 被拒：History/Tray 已短，重写不降概念数。

## 最终决策
- 选定方案：A
- 选择理由：满足 Q1=C 与「窗级不再枚举」，且不扩大无收益重写。
- 决策来源：Agent D1–D5；规格 Q1=C / Q2=A。

## 集成方式与数据流/控制流
`App.apply_appearance_theme` → `theme.apply_theme(app, surfaces=[settings, history, floating, tray])` → 各表面 `reapply_theme`。
设置：壳层 QSS + `reapply_subtree(self)` + 模型 hero / LLM 编辑器等仍由窗拥有的少量刷新。
拆页后 `_setup_ui` 改为 `build_general_page(self)` 等，信号与 `_load_settings` 仍在 `SettingsWindow`。

## 接口与状态模型
- 不新增配置键、不改 `ThemeAware` 公开形状。
- `build_*_page(win: SettingsWindow) -> QWidget` 为内部函数，非公共 API。

## 失败处理与可观测性
- 沿用 `apply_theme` 对单表面异常 warning 且继续其余表面（规格已写）。
- 无新遥测。

## 兼容、迁移与回滚
- 兼容：`settings-control-alignment` 不削弱。
- 迁移：无。
- 回滚：还原 `sayink/ui/settings*.py` 与测试；无用户数据。

## 安全与性能
- 安全：不适用（不碰密钥/网络）。
- 性能：换肤仍为用户触发一次性刷新；删除窗级长链不恶化。

## 验证策略

### 质量画像

| profile | requirement/acceptance | task | validation |
|---|---|---|---|
| functional | 切主题四页/四表面刷新；IA 不变 | T1–T3 | pytest 下列命令 |
| visual | Q2=A substitute：色轴/字号/无半换肤 | T1, T3, T4 | pytest + 手工清单 |
| compatibility | 对齐规格 light/dark | T4 | `test_settings_general` / 对齐相关用例 |

### Visual validation matrix（substitute，Q2=A）

| ID | baseline/substitute | route | state | browser/viewport | diff policy | evidence path | required_for_verified | failure proves |
|---|---|---|---|---|---|---|---|---|
| V1 | substitute：`test_theme_resolve` / `test_ui_styles` | 设置→通用切主题 | light→dark | 桌面 960×620 | 禁止半换肤/字号漂；允许 QSS 文本重组 | verification.md | yes | 设置通用半换肤 |
| V2 | 同上 + `test_ui_font` | 设置模型/润色/关于 | light/dark | 同上 | 同上 | verification.md | yes | 其它设置页残留 |
| V3 | `test_history_window` 换肤 | 历史窗 | light/dark | 历史窗默认尺寸 | 同上 | verification.md | yes | 历史停在旧轴 |
| V4 | `test_floating_window` | 浮窗 | light/dark | 浮窗 | 同上 | verification.md | yes | 浮窗锁暗/锁浅 |
| V5 | `test_tray_icon` | 托盘菜单 | light/dark | 托盘菜单 | 同上 | verification.md | yes | 菜单不跟主题 |

无截图 SHA；G9 像素五行不适用，以本表 substitute 作为已批准替代（规格 Q2=A）。

## 需求追溯
| 需求/场景 | 设计要素 | 任务 | 验证 |
|---|---|---|---|
| 设置窗 ThemeAware 广播 / 切暗色无残留 | D3 广播 | T1 | test_theme_resolve / test_ui_styles / 源码无 viRole 链 |
| 设置换肤不再角色巡检 | D3 | T1 | 断言 `reapply_theme` 不含 viRole 枚举 |
| 分页模块化且 IA 不变 | D2 | T2 | test_settings_general |
| 四表面同一协议 / 冷启动 | D4 | T3 | 四表面 theme 测试 |
| 单表面 reapply 失败不中断 | 沿用 apply_theme try/except | T3 | 既有或补测 |
| 观感与基线一致 | D5 | T1 T3 T4 | substitute 矩阵 |
| 拆分后对齐仍成立 | 不改布局几何 | T4 | 对齐用例 light/dark |

## 已知风险与非目标
- G8：不并行改两份 Complete change。
- 非目标见简报。

## 实现就绪审查

### 结论
- 就绪（条件：实现闸门再次接受 `W-g8-overlap-ui-theme`）
- 风险等级：medium
- 所需批准：实现闸门单题放行

### 阻塞项
- 无

### 警告项
- `W-g8-overlap-ui-theme`：active Complete change `ui-theme-design-system`、`ui-typography-theme-polish` 与本变更共享 `sayink/ui/*`。规格闸门已接受；实现闸门须再次可见。

### 建议项
- 本变更后 archive 两份旧 UI change。

### 覆盖情况
| 需求/场景 | 技术计划 | 任务 | 验证 | 状态 |
|---|---|---|---|---|
| ThemeAware 广播 | D3 | T1 | pytest + 源码断言 | 已映射 |
| 拆页 IA | D2 | T2 | test_settings_general | 已映射 |
| 四表面协议 | D4 | T3 | 四表面测试 | 已映射 |
| 对齐 | 几何不动 | T4 | 对齐用例 | 已映射 |
| 视觉 | D5 substitute | T1 T3 T4 | 矩阵 V1–V5 | 已映射 |

### 技术决策就绪度
- 扫描：集成、失败（单表面）、兼容、验证、回滚；无部署/迁移/安全面
- Agent 自主：D1–D5
- 用户决定：Q1=C Q2=A（Frame）
- 回 Frame：无
- 剩余阻塞：无

### 代码事实新鲜度
- 分支/提交：main / 4bc0431
- 变基/重构检查：路径均存在
- 缺失/改名路径：无

### 并行安全
- 独立任务组：无（T1→T4 顺序，共享 settings 文件）
- 共享文件：`settings_window.py`、`settings_components.py`
- 所有权：单 Agent 顺序
- G8：见警告项；用户须在实现闸门摘要中再次接受

### 后端结构验证
- 待 `openspec validate --strict` 在写入 tasks 后重跑

### 闸门记录
- 决定：待实现闸门（上一波点选回传了 Q1/Q2，未记为实施放行）
- 规格批准：user / 2026-09-18T16:05:00+08:00
- 实现批准：待批准

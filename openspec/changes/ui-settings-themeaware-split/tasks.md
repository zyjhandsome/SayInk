# ui-settings-themeaware-split：实施任务清单

## 执行规则
- 权威状态源：`openspec/changes/ui-settings-themeaware-split/`
- 风险/闸门：Standard / medium；勾选属 Execute
- 禁止范围：`sayink/speech_recognizer.py`、`sayink/audio_recorder.py`、热键核心、token/字体重做、IA 重排、像素截图基建
- 必须执行的最终验证：
  `py -3.10 -m pytest tests/test_theme_resolve.py tests/test_ui_styles.py tests/test_ui_font.py tests/test_settings_general.py tests/test_floating_window.py tests/test_history_window.py tests/test_tray_icon.py -q`

## 任务

- [x] 任务 1：设置换肤改为 ThemeAware 广播
  - 对应需求/场景：设置窗换肤为 ThemeAware 广播；切到暗色后设置四页无残留浅色；设置换肤不再角色巡检
  - 前置依赖：无
  - 目标文件/符号：`sayink/ui/settings_window.py` `SettingsWindow.reapply_theme`；`sayink/ui/settings_components.py` `reapply_subtree` / 各 `reapply_styles` / `paint_*`；`tests/test_theme_resolve.py`；`tests/test_ui_styles.py`；`tests/test_ui_font.py`
  - 允许修改：上述文件及为断言新增的测试辅助
  - 禁止修改：ASR/录音/热键；`design_tokens` 色轴与 `TYPE_*` 数值
  - 实施步骤：
    1. 先写失败测试：`SettingsWindow.reapply_theme` 源码不含 `viRole` 枚举链；切主题后 callout/tip/hero 随轴。
    2. 为仍靠窗级巡检的工厂控件补 `reapply_styles`。
    3. 将窗级 `reapply_theme` 收成壳层 QSS + `reapply_subtree` + 窗拥有的少量刷新（模型 hero、LLM 编辑器）。
  - 失败测试或已批准替代验证：先 RED 后实现
  - 验证命令/动作：`py -3.10 -m pytest tests/test_theme_resolve.py tests/test_ui_styles.py tests/test_ui_font.py -q`
  - 预期结果：exit 0
  - 质量层/证据：functional + visual V1/V2（substitute）
  - 迁移/回滚：还原上述文件
  - 完成定义：窗级无 viRole 链；相关 pytest 绿
  - 负责人/冲突说明：顺序执行；独占 settings 换肤文件

- [x] 任务 2：按页拆出设置页面模块
  - 对应需求/场景：设置分页模块化且 IA 不变；分页入口保持四个
  - 前置依赖：任务 1
  - 目标文件/符号：`sayink/ui/settings_pages/general.py` `build_general_page`；`model.py` `build_model_page`；`polish.py` `build_polish_page`；`about.py` `build_about_page`；`sayink/ui/settings_window.py` `_setup_ui`
  - 允许修改：新建 `settings_pages/`；`settings_window.py` 改为调用 builder；`tests/test_settings_general.py` 仅在夹具 import 路径必须时改
  - 禁止修改：侧栏页名、用户可见文案、分页数量
  - 实施步骤：
    1. 确认 `test_settings_stack_contains_only_native_pages` 等失败条件仍覆盖 IA。
    2. 将 `_create_*_page` 及纯页面辅助迁到 `settings_pages/`，控件仍挂在 `win` 上。
    3. `_setup_ui` 改为四个 `build_*_page(self)`。
  - 失败测试或已批准替代验证：以既有 `test_settings_general` 为护栏（IA 已锁定）
  - 验证命令/动作：`py -3.10 -m pytest tests/test_settings_general.py -q`
  - 预期结果：exit 0
  - 质量层/证据：functional
  - 迁移/回滚：删除 `settings_pages/` 并还原 `_create_*_page`
  - 完成定义：四页入口与文案不变；pytest 绿
  - 负责人/冲突说明：顺序；与 T1 同改 `settings_window.py`

- [x] 任务 3：四表面 ThemeAware 协议对齐
  - 对应需求/场景：四表面同一 ThemeAware 协议；四表面同时跟随暗色；冷启动有效主题一致；单表面失败不中断
  - 前置依赖：任务 1
  - 目标文件/符号：`sayink/ui/theme.py` `apply_theme`；`history_window.py` `HistoryWindow.reapply_theme`；`floating_window.py` `FloatingWindow.reapply_theme`；`tray_icon.py` `TrayIcon.reapply_theme`；`tests/test_floating_window.py`；`tests/test_history_window.py`；`tests/test_tray_icon.py`；`tests/test_theme_resolve.py`
  - 允许修改：上述表面换肤与对应测试
  - 禁止修改：听写状态机、托盘业务菜单项语义
  - 实施步骤：
    1. 补/强化四表面随 `apply_theme` 换肤与冷启动断言。
    2. 清 History/Float/Tray 构造期残留（若测试暴露）。
    3. 确认 `apply_theme` 单表面异常不中断其余表面。
  - 失败测试或已批准替代验证：先补失败断言再改
  - 验证命令/动作：`py -3.10 -m pytest tests/test_theme_resolve.py tests/test_floating_window.py tests/test_history_window.py tests/test_tray_icon.py -q`
  - 预期结果：exit 0
  - 质量层/证据：functional + visual V3–V5
  - 迁移/回滚：还原三表面与测试
  - 完成定义：四表面测试绿；协议可被 `apply_theme` 广播
  - 负责人/冲突说明：可与 T2 顺序；勿并行改同一测试文件

- [x] 任务 4：对齐规格与视觉 substitute 收口
  - 对应需求/场景：ThemeAware 拆分后结构对齐仍成立；观感与现网基线一致
  - 前置依赖：任务 2、任务 3
  - 目标文件/符号：`tests/test_settings_general.py` 对齐/等宽/滚动条用例；`tests/test_ui_styles.py`；手工清单写入 `openspec/changes/ui-settings-themeaware-split/verification.md`（Execute 填写结果）
  - 允许修改：测试断言随结构更新（不得改对齐语义）；verification 模板段落
  - 禁止修改：对齐规格的 ±1px / AsNeeded 语义
  - 实施步骤：
    1. 跑对齐相关用例，修复拆页引起的查找路径。
    2. 按 visual 矩阵 V1–V5 列出手工步骤（Execute 执行）。
  - 失败测试或已批准替代验证：既有对齐测试
  - 验证命令/动作：`py -3.10 -m pytest tests/test_settings_general.py tests/test_ui_styles.py -q`
  - 预期结果：exit 0
  - 质量层/证据：compatibility + visual V1–V5 substitute
  - 迁移/回滚：还原测试
  - 完成定义：对齐用例绿；verification 含手工清单（结果属 Execute）
  - 负责人/冲突说明：收口任务，最后执行

## 集成顺序
T1 → T2 → T3 → T4（T3 在 T1 之后即可，默认仍串行以免测试文件重叠）。

## 最终验证
| 命令/动作 | 覆盖范围 | 预期结果 |
|---|---|---|
| `py -3.10 -m pytest tests/test_theme_resolve.py tests/test_ui_styles.py tests/test_ui_font.py tests/test_settings_general.py tests/test_floating_window.py tests/test_history_window.py tests/test_tray_icon.py -q` | 主题、样式、字体、设置 IA/对齐、四表面 | exit 0 |
| `openspec validate ui-settings-themeaware-split --strict` | 工件结构 | valid |
| 手工：设置/历史/浮窗/托盘切 light↔dark | visual V1–V5 | 无半换肤、无 IA 变化 |

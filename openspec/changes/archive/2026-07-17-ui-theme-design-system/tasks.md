# ui-theme-design-system：实施任务清单

## 执行规则

- 权威状态源：`openspec/changes/ui-theme-design-system/`
- 风险/闸门：High；须实现闸门放行后方可改业务代码
- 禁止范围：`sayink/speech_recognizer.py`、`sayink/audio_recorder.py`、热键核心逻辑、设置分页 IA 重组、替换 PyQt6
- 必须执行的最终验证：见文末「最终验证」表；全部通过才可声称 verified

## 任务

- [x] T0：生成并入库设计系统 MASTER
  - 对应需求/场景：设计系统 MASTER 权威文档 / MASTER 存在于版本库；Token 与 MASTER 同轴
  - 前置依赖：无
  - 目标文件/符号：`design-system/MASTER.md`；ui-ux-pro-max `scripts/search.py --design-system --persist -p SayInk`
  - 允许修改：`design-system/**`
  - 禁止修改：业务 Python（本任务仅文档）
  - 实施步骤：
    1. 运行 ui-ux-pro-max design-system 查询（desktop utility / voice transcription / productivity / dark mode）并 `--persist`
    2. 审阅 MASTER，去掉与桌面托盘工具冲突的 web-only 建议；明确 light/dark 色板
    3. 确保路径为仓库根下 `design-system/MASTER.md`
  - 失败测试或已批准替代验证：文件存在与非空检查（pytest 或脚本）
  - 验证命令/动作：`python -c "from pathlib import Path; p=Path('design-system/MASTER.md'); assert p.is_file() and p.stat().st_size>200"`
  - 预期结果：断言通过；MASTER 含颜色/字体/间距规则
  - 迁移/回滚：删除 `design-system/` 即可
  - 完成定义：MASTER 已提交工作区且验证命令通过
  - 负责人/冲突说明：串行；解锁 T1

- [x] T1：双主题 token + 主题解析与配置持久化
  - 对应需求/场景：主题模式与有效主题解析；主题偏好持久化；非法值回落；Token 与 MASTER 同轴
  - 前置依赖：T0
  - 目标文件/符号：`sayink/ui/design_tokens.py`；`sayink/ui/theme.py`（新建：`resolve_effective_theme`/`apply_theme` 骨架）；`sayink/config.py`（`DEFAULT_CONFIG["appearance"]`）；`tests/test_theme_resolve.py`（新建）
  - 允许修改：上述路径；必要时 `sayink/ui/app_styles.py` 改为可按主题重建
  - 禁止修改：各 Window 大改版（留 T2–T5）；ASR/录音
  - 实施步骤：
    1. 按 MASTER 映射 light/dark token 表与 `tokens_for(effective)`
    2. 实现 `resolve_effective_theme`（含 system 探测失败→light、非法 mode→system）
    3. 增加 `appearance.theme_mode` 默认 `system`；配置往返
    4. `apply_theme` 至少能设置 `QApplication` 全局 stylesheet（表面刷新可先留 hook）
    5. 测试优先：写失败用例再实现
  - 失败测试或已批准替代验证：`tests/test_theme_resolve.py` 覆盖默认/显式/非法/持久化
  - 验证命令/动作：`python -m pytest tests/test_theme_resolve.py -q`
  - 预期结果：全部通过
  - 迁移/回滚：缺省键兼容；git revert
  - 完成定义：测通；main 启动路径可调用 resolve+apply（可在 T1 末接线）
  - 负责人/冲突说明：串行；与 T2 共享 `theme.py`/`config.py`（T2 开始前合并）

- [x] T2：设置「外观」入口 + 即时换肤（设置窗）
  - 对应需求/场景：设置页外观入口；从设置切换到暗色；外观组可见
  - 前置依赖：T1
  - 目标文件/符号：`SettingsWindow._create_general_page`；外观控件；`_persist`/轻量主题变更路径；`App` 中主题应用调用（避免经完整 `_on_settings_changed` 重配 STT）；`tests/test_ui_styles.py` 或新建设置外观相关断言
  - 允许修改：`sayink/ui/settings_window.py`、`settings_components.py`（若需）、`settings_styles.py`、`sayink/app.py`（仅主题接线）、相关测试
  - 禁止修改：录音/STT 配置逻辑行为
  - 实施步骤：
    1. 通用页新增「外观」组（light/dark/system）
    2. 变更写入 `appearance.theme_mode` 并 `apply_theme` + `settings.reapply_theme`
    3. 设置窗样式随有效主题重建
  - 失败测试或已批准替代验证：断言外观控件存在；切换 mode 后配置值变化；设置窗 stylesheet 随 dark 变化（可测字符串特征）
  - 验证命令/动作：`python -m pytest tests/test_theme_resolve.py tests/test_ui_styles.py -q`
  - 预期结果：相关用例通过
  - 迁移/回滚：git revert
  - 完成定义：设置内可切换并持久化；设置窗即时换肤
  - 负责人/冲突说明：串行于 T1 后；与 T3 可能同改 `settings_styles` — 先 T2 再 T3

- [x] T3：历史窗口随有效主题
  - 对应需求/场景：四表面随有效主题呈现（历史）
  - 前置依赖：T1；建议 T2 完成后以复用 apply 管道
  - 目标文件/符号：`sayink/ui/history_window.py`（`HistoryWindow.reapply_theme` 或等价）
  - 允许修改：`history_window.py`；共享 styles/tokens；相关测试
  - 禁止修改：历史导出业务语义
  - 实施步骤：将硬编码色/QSS 改为当前 tokens；实现 reapply；接入 `apply_theme` 表面列表
  - 失败测试或已批准替代验证：构造 HistoryWindow 后 dark apply 时主样式含暗色轴特征（或 snapshot 关键字段）
  - 验证命令/动作：`python -m pytest tests/test_ui_styles.py -q -k history`
  - 预期结果：选定用例通过；若无现成 `-k history` 则新增最小用例并在本命令中覆盖
  - 迁移/回滚：git revert
  - 完成定义：历史窗随 light/dark 切换无大面积未换肤
  - 负责人/冲突说明：可与 T4/T5 并行若文件不重叠；默认串行降低冲突

- [x] T4：浮窗随有效主题（废除唯常暗轴）
  - 对应需求/场景：四表面…浮窗；浅色下浮窗不再锁死常暗
  - 前置依赖：T1
  - 目标文件/符号：`sayink/ui/floating_window.py`；`FLOAT_*` 主题化映射
  - 允许修改：`floating_window.py`、`design_tokens.py`（float 轴）、相关测试
  - 禁止修改：录音状态机业务语义（仅视觉）
  - 实施步骤：为 light/dark 定义 float 表面 token；`reapply_theme`；接入 apply 管道
  - 失败测试或已批准替代验证：有效主题 light 时浮窗背景/文本取自 light float token（单元或 widget 测）
  - 验证命令/动作：`python -m pytest tests/test_ui_styles.py tests/test_theme_resolve.py -q`
  - 预期结果：新增/更新用例通过
  - 迁移/回滚：git revert
  - 完成定义：规格「浅色下浮窗不再锁死常暗」可演示
  - 负责人/冲突说明：与 T3/T5 文件不重叠时可并行

- [x] T5：托盘菜单随有效主题
  - 对应需求/场景：四表面…托盘上下文菜单
  - 前置依赖：T1
  - 目标文件/符号：`sayink/ui/tray_icon.py`（`_menu_stylesheet` / 重建菜单样式）
  - 允许修改：`tray_icon.py`、tokens、相关测试
  - 禁止修改：托盘菜单业务动作语义
  - 实施步骤：stylesheet 按有效主题生成；主题变更时刷新菜单 CSS
  - 失败测试或已批准替代验证：`_menu_stylesheet`（或公开包装）在 dark 下含暗色背景/文本 token
  - 验证命令/动作：`python -m pytest tests/test_ui_styles.py -q -k tray`
  - 预期结果：用例通过（无则新增后纳入命令）
  - 迁移/回滚：git revert
  - 完成定义：托盘菜单随主题变化
  - 负责人/冲突说明：可与 T3/T4 并行

- [x] T6：双主题结构对齐 + 样式回归收束
  - 对应需求/场景：settings-control-alignment 双主题；样式回归；MASTER 同轴抽样
  - 前置依赖：T2–T5
  - 目标文件/符号：`tests/test_ui_styles.py`；必要时设置控件宽度常量；`verification.md`（execute 阶段填写，本任务准备断言）
  - 允许修改：测试与为对齐所需的最小样式/布局；禁止借机 IA 大改
  - 禁止修改：无关模块
  - 实施步骤：
    1. 更新硬编码色断言为 token/API
    2. 补充 dark 下等宽（width 相等）自动化
    3. 跑全量 UI/theme 测试并记录
  - 失败测试或已批准替代验证：暗色等宽断言失败则修控件样式
  - 验证命令/动作：`python -m pytest tests/test_theme_resolve.py tests/test_ui_styles.py -q`
  - 预期结果：全部通过
  - 迁移/回滚：n/a（测试）
  - 完成定义：自动化绿；附手工四表面检查清单结果（execute 写入 verification）
  - 负责人/冲突说明：最后串行集成

## 集成顺序

```text
T0 → T1 → T2 → (T3 ∥ T4 ∥ T5) → T6
```

默认建议串行 T3→T4→T5 以降低样式冲突；若并行，T6 前必须全量回归。

## 最终验证

| 命令/动作 | 覆盖范围 | 预期结果 |
|---|---|---|
| `python -c "from pathlib import Path; assert Path('design-system/MASTER.md').is_file()"` | MASTER | 通过 |
| `python -m pytest tests/test_theme_resolve.py tests/test_ui_styles.py -q` | 主题+样式 | 全绿 |
| 手工：设置外观切 light/dark/system；开历史/浮窗/托盘 | 四表面 | 无大面积未换肤；浮窗随有效主题 |
| 手工：暗色下历史数值等宽与右缘 | 对齐规格 | 符合 ±1px |
| `openspec validate ui-theme-design-system` | 工件 | valid |

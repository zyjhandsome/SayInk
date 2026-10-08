# ui-typography-theme-polish：实施任务清单

## 执行规则
- 权威状态源：`openspec/changes/ui-typography-theme-polish/`
- 风险/闸门：Standard / medium；须实现闸门放行后方可改业务代码
- 禁止范围：`sayink/speech_recognizer.py`、`sayink/audio_recorder.py`、热键核心逻辑、设置分页 IA 重组、替换 PyQt6
- 必须执行的最终验证：见文末「最终验证」表；全部通过才可声称 verified

## 任务

- [x] T1：字体解析 API + 全局/浮窗统一 family
  - 对应需求/场景：ui-typography / Unified UI font family resolution（float match + fallback）
  - 前置依赖：无
  - 目标文件/符号：`sayink/ui/design_tokens.py`（`resolve_ui_font_family` 或并列助手）、`sayink/ui/theme.py`（`apply_theme` 刷新 `FONT`/`FONT_DISPLAY`）、`sayink/ui/app_styles.py`、`sayink/ui/floating_window.py`（移除硬编码 `"Segoe UI Variable"`）、`tests/test_theme_resolve.py` 或新建 `tests/test_ui_font.py`
  - 允许修改：上述路径；必要时 `settings_styles.py` 仅改 `font-family: {t.FONT}` 消费已刷新值
  - 禁止修改：ASR/录音/热键；无关窗口业务逻辑
  - 实施步骤：
    1. 实现探测：优先 Segoe UI Variable → Microsoft YaHei UI → Segoe UI；可注入 families 列表便于单测
    2. `apply_theme` / 冷启动路径调用解析并写回模块级 FONT
    3. 浮窗 `QFont` 使用解析后的 family + 现有字号参数（字号 token 化可先保留数字，T2 替换）
    4. 测试：优先命中 / 缺失回落 / stylesheet 或 float 无硬编码族字符串
  - 失败测试或已批准替代验证：先写失败用例再实现
  - 验证命令/动作：`py -3.10 -m pytest tests/test_theme_resolve.py tests/test_ui_styles.py tests/test_ui_font.py -q`（若新建 font 测文件）
  - 预期结果：相关用例通过；浮窗源码无 `"Segoe UI Variable"` 字面量（除注释/测试夹具外）
  - 迁移/回滚：git revert
  - 完成定义：解析与全局/浮窗一致；测通
  - 负责人/冲突说明：串行；与 T2 共享 tokens — 先 T1 再 T2

- [x] T2：`TYPE_*` 字号阶梯接入四表面样式
  - 对应需求/场景：ui-typography / Typography size scale tokens
  - 前置依赖：T1
  - 目标文件/符号：`design_tokens.TYPE_*`；`settings_components.reload_styles`；`settings_styles.build_*`；`history_window.reapply_theme`；`tray_icon._menu_stylesheet`；`model_card.reapply_styles`；`floating_window.reapply_theme`/`_setup_ui`；相关测试
  - 允许修改：上述 UI 样式路径与测试
  - 禁止修改：业务语义；禁止借机 IA 改版
  - 实施步骤：
    1. 按 design D2 增加 TYPE_* 常量
    2. 替换设置/历史/托盘/模型卡/浮窗用户可见 font-size 魔法数为 token
    3. 断言 stylesheet 或常量引用含 token 值
  - 失败测试或已批准替代验证：扩展 `tests/test_ui_styles.py`
  - 验证命令/动作：`py -3.10 -m pytest tests/test_ui_styles.py tests/test_theme_resolve.py -q`
  - 预期结果：全部通过；抽查无新增游离魔法字号（测试断言数值除外）
  - 迁移/回滚：git revert
  - 完成定义：四表面样式构建走 TYPE_*
  - 负责人/冲突说明：串行于 T1 后；与 T3 可能同改 settings_window — 先 T2 完成字号再 T3

- [x] T3：工厂控件换肤 + 浮窗 chip token
  - 对应需求/场景：theme-surface-polish / factory retheme；float chip tokenized
  - 前置依赖：T1（建议 T2 后以减少冲突）
  - 目标文件/符号：`settings_components.info_callout`/`usage_tip_bar`/相关 paint；`SettingsWindow.reapply_theme`；`design_tokens.CHIP_BG_HOVER`/`CHIP_BG_PRESS`；`FloatingWindow.reapply_theme`；测试
  - 允许修改：上述路径与测试
  - 禁止修改：无关页业务
  - 实施步骤：
    1. 为 callout/tip 等抽出按当前 tok 重绘的函数；构造与 reapply 共用
    2. `reapply_theme` 中 `findChildren` 或 objectName 刷新
    3. 增加 chip hover/press token；删除 `CHIP_BG.replace` 字符串黑客
    4. 测试：dark apply 后 callout 样式含 dark 轴色；float QSS 含新 token 名或值
  - 失败测试或已批准替代验证：新增/扩展 `test_ui_styles` 主题切换用例
  - 验证命令/动作：`py -3.10 -m pytest tests/test_ui_styles.py tests/test_floating_window.py -q`
  - 预期结果：用例通过
  - 迁移/回滚：git revert
  - 完成定义：切主题后 callout/tip 与 chip 状态色正确
  - 负责人/冲突说明：串行；独占 settings_components 工厂段与 floating chip

- [x] T4：MASTER 同轴（字体策略 + 字号表 + 暗色 GREEN）+ 回归收束
  - 对应需求/场景：ui-typography MASTER alignment；theme-surface-polish MASTER green
  - 前置依赖：T1–T3
  - 目标文件/符号：`design-system/MASTER.md`；必要时轻量断言；`verification.md`（execute 填写）
  - 允许修改：`design-system/MASTER.md`、测试断言、本 change 验证记录
  - 禁止修改：业务 Python（本任务以文档+断言为主；若抽样发现代码漂移仅允许最小对齐）
  - 实施步骤：
    1. MASTER 写明 Segoe-first→YaHei 回落与 TYPE_* 表（数值与 tokens 一致）
    2. 暗色 GREEN/TOGGLE 文档改为 `#16A34A`（quiet mid-green）
    3. 跑全量 UI/theme 相关测试并记录
  - 失败测试或已批准替代验证：MASTER 文件含策略句与阶梯数字的断言
  - 验证命令/动作：`py -3.10 -m pytest tests/test_theme_resolve.py tests/test_ui_styles.py tests/test_floating_window.py tests/test_history_window.py tests/test_tray_icon.py -q`
  - 预期结果：全部通过；MASTER 抽样断言通过
  - 迁移/回滚：git revert
  - 完成定义：文档与代码同轴；回归绿
  - 负责人/冲突说明：收尾串行

## 集成顺序
T1 → T2 → T3 → T4（默认串行；T2/T3 勿并行改同一大文件）

## 最终验证
| 命令/动作 | 覆盖范围 | 预期结果 |
|---|---|---|
| `py -3.10 -m pytest tests/test_theme_resolve.py tests/test_ui_styles.py tests/test_floating_window.py tests/test_history_window.py tests/test_tray_icon.py -q` | 字体/字号/换肤/浮窗/历史/托盘 | exit 0 |
| MASTER 抽样（字体策略 + TYPE 表 + dark GREEN） | 文档同轴 | 断言通过 |
| 手工：设置切 light/dark → 看 callout/浮窗/托盘 | 观感冒烟 | 无残留旧色/字体撕裂 |

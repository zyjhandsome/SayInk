# ui-main-window-outer-border：实施任务清单

## 执行规则
- 权威状态源：openspec/changes/ui-main-window-outer-border
- 风险/闸门：Quick / low；实施前须用户明确批准轻量契约
- 禁止范围：`sayink/app.py`、`island_chrome.py`、`floating_window.py`、`design_tokens.py` 色值、录音/热键/ASR/粘贴/润色/SQLite
- 必须执行的最终验证：`python -m pytest tests/test_main_window.py -q`

## 任务

- [x] 任务 1：给无边框主窗口加上可见的 1px 外边框
  - 对应需求/场景：Visible main-window outer border / 基本行为
  - 前置依赖：无
  - 目标文件/符号：`sayink/ui/main_window.py` `MainWindow.reapply_theme`；`tests/test_main_window.py`
  - 允许修改：`sayink/ui/main_window.py`；`tests/test_main_window.py`
  - 禁止修改：`sayink/app.py`、`sayink/ui/island_chrome.py`、`sayink/ui/floating_window.py`、`sayink/ui/design_tokens.py`
  - 实施步骤：先写失败测试断言最外层样式含 `border: 1px solid` 且使用 `CONTROL_BORDER`；再把 `reapply_theme` 改成对象名限定的窗口描边，避免子控件继承边框。
  - 失败测试或已批准替代验证：`tests/test_main_window.py` 新增主题描边断言
  - 验证命令/动作：`python -m pytest tests/test_main_window.py -q`
  - 预期结果：exit 0，全部通过
  - 迁移/回滚：不适用（可逆小改动）
  - 完成定义：验证命令通过且主窗口四周可见 1px 外边框
  - 负责人/冲突说明：单人无冲突

## 集成顺序

单任务，无集成顺序。

## 最终验证
| 命令/动作 | 覆盖范围 | 预期结果 |
|---|---|---|
| `python -m pytest tests/test_main_window.py -q` | 主窗口 chrome 尺寸/导航/描边 + 相邻回归 | exit 0 |

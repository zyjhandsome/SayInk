# ui-main-window-outer-border：验证报告

## 范围与状态
- 状态源：openspec/changes/ui-main-window-outer-border
- 风险/闸门：Quick / low；实施批准已记录；独立审查 pass（无 CRITICAL）
- 提交/差异：仅 `sayink/ui/main_window.py` 与 `tests/test_main_window.py`；未改禁止范围文件

## 运行与静态证据
| 时间 | 命令/动作 | 退出码/结果 | 失败数 | 覆盖范围 |
|---|---|---|---|---|
| 2026-09-21T07:05:00Z | `py -3.10 -m pytest tests/test_main_window.py::test_outer_border_uses_control_border_token -q` | RED：`assert '' == 'mainWindow'` | 1 | 描边未实现 |
| 2026-09-21T07:10:00Z | `py -3.10 -m pytest tests/test_main_window.py -q` | exit 0，5 passed | 0 | 主窗口 chrome + 描边 |
| 2026-09-21T07:11:00Z | `openspec validate ui-main-window-outer-border` | valid | 0 | OpenSpec 结构 |
| 2026-09-21T07:11:00Z | `node validate_delivery_change.mjs openspec/changes/ui-main-window-outer-border` | PASS | 0 | Delivery 工件 |

### 主验证证据（机器锚点，标签稳定勿改）
- 命令：`py -3.10 -m pytest tests/test_main_window.py -q`
- 时间：2026-09-21T07:22:00Z
- 结果：pass / exit 0 / 5 passed

## 需求验证
| 需求/场景 | 实现证据 | 验证方式 | 结果 |
|---|---|---|---|
| Visible main-window outer border / 基本行为 | `MainWindow.reapply_theme` 使用 `QWidget#mainWindow` + `border: 1px solid {CONTROL_BORDER}` | `test_outer_border_uses_control_border_token` | 通过 |

## 视觉证据（`quality_profiles.visual=required` 时）
- Visual report：不适用（契约验收为样式断言替代，非五态截图）
- Source artifact revision：41e06c9a4ecbdb9e1ecb7abc2d682665571777a0f51aaad1d9f63684126c8c51
- Assessment mode：consistency_review
- Baseline / substitute：`tests/test_main_window.py::test_outer_border_uses_control_border_token`
- Capture context：PyQt6 桌面窗体；浅/深色 token `CONTROL_BORDER`
- Required states：未采图；由样式字符串断言覆盖默认主题
- Visual validator：同上 pytest
- G9 Visual Evidence：not_required
- Accepted differences / coverage gaps：未在实机桌面截图确认；Win11 圆角可能裁掉四角像素

## 规格一致性
- 工具/审查：`openspec validate` 通过；对照 `specs/main-window-chrome/spec.md`
- 完整性：一条 Requirement + 基本行为已实现
- 正确性：选择器用 objectName 限定最外层
- 一致性：未改 `FLOAT_BORDER` / 听写胶囊 / 系统标题栏

## 代码审查
- 模式：independent（subagent `f7940851-7209-4f2e-b45e-196ad2f13486`）
- 结论：pass
### 阻塞项
无
### 警告项
- 测试断言 QSS 文本而非像素绘制
- 侧栏 substring 不能证明未继承，真正护栏是 `#mainWindow` 选择器
- Win11 DWM 圆角可能裁四角像素
### 建议项
- 不必为凑 G9 扩到五态截图；保持契约内的样式断言

## 降级项与残余风险
- 跳过/降级检查：未做实机窗口截图
- 批准/原因：Quick 契约最小验证即 pytest 样式断言
- 覆盖缺口：最大化态、深色实机观感需用户打开主界面目视

## 最终闸门
- 运行/静态检查：通过
- 规格核对：通过
- 代码审查：通过
- G9 视觉证据：不适用
- 是否达到已验证：是
- OpenSpec 归档：deferred_to_openspec（本技能不执行）

## 资产回写
- 已更新：无
- 无需回写，原因：主窗口描边是局部 chrome，不改 canonical 产品规格以外的 README/ADR

# ui-settings-themeaware-split：视觉 substitute 证据

> Delivery family 自有验证记录。Q2=A 批准无像素截图基建；本文件记录一致性审查，不作为 G9 像素五行输入。

| 字段 | 取值 |
|---|---|
| schema | delivery-visual-evidence/v1 |
| producer | delivery-execute-verify |
| state_owner | openspec_change |
| implementation_authority | delivery |
| change_dir | D:\Hzhao\AI_Test\SayInk-20260918\SayInk\openspec\changes\ui-settings-themeaware-split |
| source_artifact_revision | 6f1a7e5c624f9d7aa1387c0020b5904a4f83e9ef55ffb5dcc5688febe67aa203 |
| analysis_status | complete |
| remediation_status | done |
| assessment_mode | consistency_review |
| visual_acceptance_required | yes |
| final_visual_result | pass |
| adapter / browser | PyQt6 desktop (no browser) |
| viewport / device_scale_factor | settings 960×620; history 900×640; float 400×124; tray menu |
| locale / timezone / theme | zh-CN / Asia/Shanghai / light↔dark |
| font_ready_condition | `resolve_ui_font_family` via `apply_theme` |
| animation_policy | n/a (static restyle) |
| data_fixture / dynamic_masks | pytest tmp Config / HistoryStore |

- baseline_source / substitute_standard：approved-substitute:design.md visual matrix V1–V5 + pytest theme/style/font/alignment | user | 6f1a7e5c624f9d7aa1387c0020b5904a4f83e9ef55ffb5dcc5688febe67aa203
- Implementation gate reference：user / 2026-09-18T15:59:00Z / 6f1a7e5c624f9d7aa1387c0020b5904a4f83e9ef55ffb5dcc5688febe67aa203

### Required state evidence（substitute，非截图）

| id | route | state | baseline/substitute | policy | result |
|---|---|---|---|---|---|
| V1 | 设置→通用切主题 | light→dark | `test_theme_resolve` / `test_ui_styles`；禁止半换肤/字号漂 | tolerance_bound | pass |
| V2 | 设置模型/润色/关于 | light/dark | 同上 + `test_ui_font` + `test_settings_general` IA | tolerance_bound | pass |
| V3 | 历史窗 | light/dark | `test_history_window` + `test_history_reapply_keeps_construct_visual_language` | tolerance_bound | pass |
| V4 | 浮窗 | light/dark | `test_floating_window` + float light/dark stylesheet 断言 | tolerance_bound | pass |
| V5 | 托盘菜单 | light/dark | `test_tray_icon` + tray menu/icon rebuild | tolerance_bound | pass |

### 手工清单（发布前可选冒烟）

1. 打开设置四页，light↔dark：侧栏/卡片/hero/callout/开关/主按钮无浅色残留，IA 仍为通用/模型/润色/关于。
2. 设置→通用「历史」：保留天数与最大会话数等宽；开关与数值右缘 ±1px。
3. 打开历史窗切主题：标题、搜索、列表、详情跟随当前轴。
4. 浮窗切主题：容器/关闭钮/状态字跟随 FLOAT_*，不锁死旧暗色。
5. 托盘菜单切主题：菜单 QSS 与麦克风图标重建。

## Verification

| Id | 结果 | 备注 |
|---|---|---|
| V0 | pass | 工件 revision 与实现闸门绑定一致 |
| V1 | pass | pytest substitute |
| V2 | pass | pytest substitute |
| V3 | pass | pytest substitute |
| V4 | pass | pytest substitute |
| P1 | pass | 无像素基线（Q2=A） |
| P2 | pass | 允许 QSS 结构重组 |
| P3 | pass | 未改 TYPE_* / 色轴数值 |
| P4 | pass | 对齐用例含 dark ±1px |
| P5 | pass | AsNeeded 仍成立 |
| P6 | pass | 四表面 `isinstance(..., ThemeAware)` |
| P7 | pass | 单表面失败不中断 |

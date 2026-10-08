# ui-settings-themeaware-split：验证报告

## 范围与状态
- 状态源：`openspec/changes/ui-settings-themeaware-split/`
- 风险/闸门：Standard / medium；规格与实现闸门均已批准；`W-g8-overlap-ui-theme` 已接受
- 提交/差异：相对 HEAD `4bc0431` 未提交；设置换肤广播、四页拆出、`ThemeAware` 运行时协议、历史窗构造期 live QSS

## 运行与静态证据
| 时间 | 命令/动作 | 退出码/结果 | 失败数 | 覆盖范围 |
|---|---|---|---|---|
| 2026-09-18T16:15:17Z | `py -3.10 -m pytest tests/test_theme_resolve.py tests/test_ui_styles.py tests/test_ui_font.py tests/test_settings_general.py tests/test_floating_window.py tests/test_history_window.py tests/test_tray_icon.py -q` | exit 0 | 0 | 主题、样式、字体、设置 IA/对齐、四表面 |
| 2026-09-18T16:08:33Z | `openspec validate ui-settings-themeaware-split --strict` | valid | 0 | 工件结构 |

### 主验证证据（机器锚点，标签稳定勿改）
- 命令：`py -3.10 -m pytest tests/test_theme_resolve.py tests/test_ui_styles.py tests/test_ui_font.py tests/test_settings_general.py tests/test_floating_window.py tests/test_history_window.py tests/test_tray_icon.py -q`
- 时间：2026-09-18T16:15:17Z
- 结果：exit 0 / 145 passed

## 需求验证
| 需求/场景 | 实现证据 | 验证方式 | 结果 |
|---|---|---|---|
| 设置窗 ThemeAware 广播 / 切暗色无残留 | `SettingsWindow.reapply_theme` → `reapply_subtree`；窗级源码无 `viRole` | `TestSettingsThemeAwareBroadcast` + `test_theme_resolve` / `test_ui_styles` | 通过 |
| 设置换肤不再角色巡检 | `inspect.getsource(SettingsWindow.reapply_theme)` 不含 `viRole` | 同上 | 通过 |
| 分页模块化且 IA 不变 | `settings_pages/{general,model,polish,about}.py`；`_setup_ui` 调四个 `build_*_page` | `test_settings_general` 28 passed（含四页栈、文案、区块顺序） | 通过 |
| 四表面同一协议 / 冷启动 | `@runtime_checkable ThemeAware`；History 构造后 `reapply_theme` | `TestFourSurfaceThemeAwareProtocol` | 通过 |
| 单表面 reapply 失败不中断 | `apply_theme` try/except 后续表面仍调用 | `test_apply_theme_continues_after_one_surface_fails` | 通过 |
| 拆分后浅色历史数值仍等宽 | `CONTROL_NUMERIC_WIDTH=120`；两 spin min/max 等宽 | `TestSettingsControlAlignment` | 通过 |
| 拆分后暗色右缘对齐 ±1px | 开关与保留天数 spin 映射右缘差 ≤1 | `test_history_switch_and_spin_right_edges_align_under_dark` | 通过 |
| 拆分后滚动条 AsNeeded | `SettingsPage` `ScrollBarAsNeeded` + gutter | `test_settings_page_scrollbar_as_needed` | 通过 |
| 观感与基线一致（Q2=A substitute） | 样式/字体/主题断言 + 手工清单 V1–V5 | pytest + `visual-evidence.md` | 通过（substitute） |

## 视觉证据（`quality_profiles.visual=required` 时）
- Visual report：`openspec/changes/ui-settings-themeaware-split/visual-evidence.md`
- Source artifact revision：`6f1a7e5c624f9d7aa1387c0020b5904a4f83e9ef55ffb5dcc5688febe67aa203`
- Assessment mode：consistency_review
- Baseline / substitute：规格 Q2=A 批准的 pytest + 手工四表面清单（无像素截图基建）
- Capture context：桌面设置 960×620 / 历史默认 / 浮窗 / 托盘菜单；zh-CN；light↔dark
- Required states：V1–V5 见 visual-evidence.md（substitute，非 SHA 截图）
- Visual validator：Q2=A 已批准跳过 G9 像素五行；以本变更 `design.md` 视觉矩阵 + 上表 pytest 为替代
- G9 Visual Evidence：not_required（用户 Q2=A：允许 QSS 结构重组，无像素基线）
- Accepted differences / coverage gaps：允许非用户可察 QSS 文本重组；手工四表面切主题未在本会话实机点击，由自动化换肤断言替代

## 规格一致性
- 工具/审查：`openspec validate ui-settings-themeaware-split --strict` → valid
- 完整性：delta `settings-themeaware` + `settings-control-alignment` 场景均有任务与测试映射
- 正确性：窗级不再枚举 `viRole`；四页入口/文案未改；`apply_theme` 单表面失败继续
- 一致性：未改 ASR/热键/token 数值；未同步 canonical `openspec/specs/`

## 代码审查
独立审查：`r-uisplit-20260919-a4f2`（[Review](191b68aa-bb7f-4209-a889-5bcd83027d07)）→ **warn**，无 CRITICAL。
已处理：补 live callout 暗色广播断言；去掉窗级被 subtree 覆盖的重复刷色；协议测试关闭表面。
### 阻塞项
- 无
### 警告项
- `W-g8-overlap-ui-theme`：未归档 UI change 仍共享 `sayink/ui/*`（闸门已接受）
- `W-reapply-subtree-role-map`：**已接受**。`reapply_subtree` 仍对带 `viRole`/`objectName`/`viBtn` 的工厂控件做映射刷新（`stylesheet_for_role`），不是纯钩子发现。规格禁止的是窗级 `SettingsWindow.reapply_theme` 枚举链（已删除）。后续新复合控件应实现 `reapply_styles`。
### 建议项
- 本变更 archive 后归档两份旧 UI change
- 工厂控件逐步改为自刷钩子，缩短 `reapply_subtree`

## 降级项与残余风险
- 跳过/降级检查：G9 像素五行（Q2=A 已批准 substitute）；本会话未做实机手工切主题
- 批准/原因：规格闸门 Q2=A；实现闸门再次接受 `W-g8-overlap-ui-theme`；独立审查 WARNING `W-reapply-subtree-role-map` 已显式接受
- 覆盖缺口：可选发布前手工冒烟（设置/历史/浮窗/托盘 light↔dark）；未挂 `viRole`/`reapply_styles` 的新工厂控件可能半换肤

## 最终闸门
- 运行/静态检查：通过
- 规格核对：通过
- 代码审查：通过（independent / warn accepted，无 CRITICAL）
- G9 视觉证据：不适用（Q2=A substitute）
- 是否达到已验证：是
- OpenSpec 归档：deferred_to_openspec（本技能不执行）

## 资产回写
- 已更新：无（canonical specs 待 archive）
- 无需回写，原因：实现未改用户可见 IA/配置键/README 行为说明

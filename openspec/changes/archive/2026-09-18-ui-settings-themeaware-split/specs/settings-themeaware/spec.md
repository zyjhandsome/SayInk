## ADDED Requirements

### Requirement: 设置窗换肤为 ThemeAware 广播

设置窗口 SHALL 通过 ThemeAware（或等价的 `reapply_theme` / `reapply_styles`）广播刷新已创建控件。系统 MUST NOT 再在 `SettingsWindow.reapply_theme` 中按 `viRole` / `objectName` 枚举刷内联样式。切换有效主题后，已打开的设置窗通用 / 模型 / 润色 / 关于页 SHALL 呈现与当前 token 轴一致的颜色与字号，无构造期残留色。

#### Scenario: 切到暗色后设置四页无残留浅色

- **WHEN** 设置窗已打开且用户将有效主题从 `light` 改为 `dark`
- **THEN** 侧栏、分组卡片、hero、callout、开关行、主按钮与页脚提示均使用暗色 token，MUST NOT 留下浅色构造期 stylesheet

#### Scenario: 设置换肤不再角色巡检

- **WHEN** 实现完成且运行设置窗切主题
- **THEN** 设置窗壳层只通知子控件自刷；MUST NOT 再出现按标签角色逐条 `setStyleSheet` 的窗级枚举链

#### 失败行为
- **WHEN** 某个子控件未实现自刷钩子
- **THEN** 不得 silently 跳过导致半换肤；该控件须纳入 ThemeAware / `reapply_styles`，或测试失败

### Requirement: 设置分页模块化且 IA 不变

设置窗口 SHALL 仍提供且仅提供四个原生分页：通用、模型、润色、关于。页面建造可拆到独立模块，但用户可见入口、标题与文案 MUST NOT 因拆分而改变。

#### Scenario: 分页入口保持四个

- **WHEN** 用户打开设置窗
- **THEN** 侧栏/堆叠仍只有通用、模型、润色、关于，且各页原有区块（音频、历史、外观、模型卡、润色表单、关于信息）仍在对应页

#### 失败行为
- **WHEN** 拆分误改文案或合并/删除分页
- **THEN** `test_settings_general` 等分页/文案断言失败，不得以「结构更干净」为由改 IA

### Requirement: 四表面同一 ThemeAware 协议

设置窗、历史窗、浮窗、托盘 SHALL 均通过 ThemeAware 协议响应 `apply_theme` 广播。切主题后，已创建的这四个表面 MUST 在无需重启的情况下跟随有效主题；各表面不得再依赖与协议无关的一次性构造期色。

#### Scenario: 四表面同时跟随暗色

- **WHEN** 设置、历史、浮窗、托盘均已创建，且用户将有效主题改为 `dark`
- **THEN** 四个表面均呈现暗色轴，无某一表面停留在浅色

#### Scenario: 冷启动有效主题一致

- **WHEN** 主题模式为 `dark` 或 `system` 解析为 `dark`，用户冷启动后打开设置与历史
- **THEN** 新创建表面一开始即为暗色，MUST NOT 先闪浅色再补刷作为可接受终态（构造后立即 reapply 可保留）

#### 失败行为
- **WHEN** 某一表面 `reapply_theme` 抛错
- **THEN** `apply_theme` 记录警告且其余表面仍须完成换肤，MUST NOT 中断整个主题切换

### Requirement: 观感与现网基线一致

在 light 与 dark 下，四表面的用户可察颜色、字号阶梯、间距与控件几何 SHALL 与本变更前 typography polish 基线一致。允许非用户可察的 QSS 字符串结构重组。MUST NOT 借本变更调整用户可察视觉残留。

#### Scenario: 设置通用页切主题后布局不漂

- **WHEN** 用户在设置→通用于 light 与 dark 之间切换
- **THEN** 分组卡片、开关、数值框位置与对齐保持不变，仅色轴切换

#### 失败行为
- **WHEN** 重构导致控件错位、半换肤或字号漂移
- **THEN** 视为验收失败，即使 QSS 文本「更干净」

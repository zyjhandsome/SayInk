## ADDED Requirements

### Requirement: 主题模式与有效主题解析

系统 SHALL 支持三种外观主题模式：`light`、`dark`、`system`。当模式为 `system` 时，有效主题 SHALL 解析为当前 Windows 浅色或深色外观对应的 `light` 或 `dark`。新安装或配置中缺失主题键时，主题模式 SHALL 默认为 `system`。配置中出现未知主题模式值时，系统 SHALL 回落为 `system` 且不得崩溃。

#### Scenario: 默认跟随系统

- **WHEN** 用户首次运行或配置文件中不存在主题模式键
- **THEN** 主题模式为 `system`，且界面按当前系统浅/深色外观呈现对应有效主题

#### Scenario: 显式浅色

- **WHEN** 用户将主题模式设为 `light`
- **THEN** 有效主题为 `light`，不随系统外观改变（在用户再次更改模式之前）

#### Scenario: 显式暗色

- **WHEN** 用户将主题模式设为 `dark`
- **THEN** 有效主题为 `dark`

#### Scenario: 非法值回落

- **WHEN** 配置中的主题模式值为未识别字符串
- **THEN** 系统按 `system` 解析有效主题，应用可继续正常使用

### Requirement: 主题偏好持久化

主题模式偏好 SHALL 持久化到用户 `config.json`（经既有 `Config` 读写路径）。用户更改主题模式并完成保存后，进程重启 SHALL 恢复同一主题模式。

#### Scenario: 重启后保留选择

- **WHEN** 用户将主题模式改为 `dark` 且配置已保存，随后退出并再次启动应用
- **THEN** 主题模式仍为 `dark`，有效主题为 `dark`

### Requirement: 设置页外观入口

设置窗口「通用」页 SHALL 提供「外观」设置组，允许用户在 `light` / `dark` / `system` 之间选择主题模式。更改选择后，有效主题 SHALL 在无需重启应用的情况下应用到界面（至少覆盖已打开的设置窗口与全局 stylesheet；其余已打开表面不得长时间停留在旧主题）。

#### Scenario: 从设置切换到暗色

- **WHEN** 用户打开设置 → 通用 → 外观，并将主题模式改为 `dark`
- **THEN** 设置窗口在短时间内切换为暗色有效主题，且偏好被持久化

#### Scenario: 外观组可见

- **WHEN** 用户打开设置并进入通用页
- **THEN** 可见名为「外观」的设置组及主题模式选择控件

### Requirement: 四表面随有效主题呈现

在任一有效主题（`light` 或 `dark`）下，下列表面 SHALL 使用与该有效主题一致的设计 token / 样式，不得大面积保留另一主题的背景或文本色：设置窗口、历史窗口、录音浮窗、托盘上下文菜单。浮窗 SHALL 随有效主题换肤，不得在有效主题为 `light` 时仍强制使用与浅色设置页冲突的常暗专用轴作为唯一外观。

#### Scenario: 暗色下四表面一致

- **WHEN** 有效主题为 `dark`，且用户依次打开设置、历史、触发浮窗显示、打开托盘菜单
- **THEN** 上述各表面的主背景与主文本色均符合暗色 token 轴（非未换肤的浅色全局默认）

#### Scenario: 浅色下浮窗不再锁死常暗

- **WHEN** 有效主题为 `light` 且浮窗可见
- **THEN** 浮窗主表面使用浅色有效主题 token（可保留适度对比），而非仅绑定旧版常暗 `FLOAT_*` 唯一外观

### Requirement: 设计系统 MASTER 权威文档

仓库 SHALL 包含由 ui-ux-pro-max 工作流生成的设计系统权威文档 `design-system/MASTER.md`。实现所用的 light/dark 设计 token SHALL 与该 MASTER 中的颜色/字体/间距规则可追溯一致（允许 PyQt/QSS 表达差异，但不得使用与 MASTER 冲突的第二套未文档化品牌色作为主轴）。

#### Scenario: MASTER 存在于版本库

- **WHEN** 本变更合入后检查仓库根相对路径
- **THEN** 存在文件 `design-system/MASTER.md` 且内容包含项目级设计系统规则（非空占位）

#### Scenario: Token 与 MASTER 同轴

- **WHEN** 审查实现中的主表面色与强调色
- **THEN** 其取值可在 `design-system/MASTER.md` 中找到对应规则或色板条目

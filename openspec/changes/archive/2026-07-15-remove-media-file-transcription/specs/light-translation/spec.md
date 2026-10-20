## ADDED Requirements

### Requirement: 产品不再提供轻翻译模式

系统 MUST NOT 再提供「翻译」作为用户可选的 LLM 后处理模式；设置界面 SHALL 仅保留与润色相关的后处理配置（在润色功能本身仍启用的前提下）。

#### Scenario: 设置无翻译模式

- **WHEN** 用户打开文字润色相关设置
- **THEN** 界面 MUST NOT 提供「翻译（仅文件转写）」或等价翻译模式选项

#### Scenario: 运行时不执行翻译语义

- **WHEN** 用户完成实时转写且 LLM 后处理开启
- **THEN** 系统 MUST NOT 按翻译 prompt/目标语言执行翻译；仅可按润色逻辑处理或输出原文

### Requirement: 遗留 translate 配置静默回落润色

当持久化配置中 `llm.mode` 为 `translate`（或等价已废弃翻译取值）时，系统在读取配置时 SHALL 静默将其视为 `polish`，MUST NOT 因此报错或阻断启动。

#### Scenario: 读取时回落 polish

- **WHEN** 用户配置文件仍保存 `llm.mode=translate` 并启动应用或打开设置
- **THEN** 系统按润色模式解释该配置（等效 `polish`），且 MUST NOT 进入翻译行为

## REMOVED Requirements

### Requirement: 文件转写任务可启用轻翻译

**Reason**: 文件转写与轻翻译一并撤回；翻译不再有合法作用面。
**Migration**: 使用润色模式或关闭 LLM 后处理；配置中的 `translate` 将静默回落为 `polish`。

### Requirement: 翻译与润色互斥

**Reason**: 翻译模式已移除，互斥规则不再适用。
**Migration**: 仅保留润色与关闭后处理两种路径。

### Requirement: 翻译失败回退原文

**Reason**: 不再执行翻译请求。
**Migration**: 无。

### Requirement: 翻译结果可进入历史

**Reason**: 不再产生新的译文写入。
**Migration**: 历史中既有译文槽位内容只读保留，不做清理。

### Requirement: 非同声传译边界

**Reason**: 轻翻译能力整体撤回，边界声明随能力移除。
**Migration**: 无；产品亦不得再宣传同声传译。

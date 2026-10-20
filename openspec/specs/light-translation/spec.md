## Purpose

定义轻翻译能力撤回后的可观察行为：产品不再提供翻译后处理模式；遗留 `llm.mode=translate` 配置静默回落为润色；设置仅保留润色相关后处理。

## Requirements

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

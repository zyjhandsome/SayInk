# history-content-organization Specification

## Purpose
TBD - created by archiving change history-content-organization. Update Purpose after archive.
## Requirements
### Requirement: Single-session inline editing preserves raw text

The history window SHALL allow users to edit the text of a single selected dictation session and persist the edited result into `polished_text` without modifying `raw_text`.

#### Scenario: 用户编辑单条历史会话并保存
- **WHEN** 用户在历史页选中单条会话，点击「编辑」修改文本并点击「保存」
- **THEN** 系统将修改后的文本通过 `HistoryStore` 写入该会话分段的 `polished_text`
- **AND** 该会话的 `raw_text` 保持原始识别内容不变，「润色 | 原文」切换条可见且可随时切回「原文」对照

#### Scenario: 用户取消单条历史编辑
- **WHEN** 用户在编辑态点击「取消」或切换到其他会话而不保存
- **THEN** 数据库中的 `polished_text` 与 `raw_text` 均不被修改，详情区恢复只读展示

### Requirement: On-demand AI polishing for a single history session

The history window SHALL allow users to trigger AI polishing (or re-polishing) on a single selected history session using the configured OpenAI-compatible LLM endpoint.

#### Scenario: 单条历史补润色成功
- **WHEN** 已配置完整的 LLM 服务，用户在单条历史详情点击「润色」（已有润色时为「重新润色」；「处理方式」为纠错时按钮为「纠错 / 重新纠错」）
- **THEN** 系统异步调用 `TextPolisher` 对该会话的 `raw_text` 进行处理，使用与实时输出相同的 `llm.prompt`、`llm.mode` 与术语表，完成后写回 `polished_text`，自动切换到「润色」视图并显示完成反馈

#### Scenario: 结果未通过保真检查
- **WHEN** 模型返回的文本未通过与实时输出相同的保真检查（数字、否定、英文专名；纠错模式下还包括长度与改动比例）
- **THEN** 不写回 `polished_text`，底部 Toast 说明原因，原文与已有润色文本保持不变

#### Scenario: LLM 未配置或润色请求失败
- **WHEN** 用户点击「润色」但 LLM 配置不完整，或请求超时/报错
- **THEN** 未配置时引导用户打开「AI 服务」设置页；请求失败时在底部 Toast 提示失败原因，且绝不覆盖或清空已有的 `raw_text` 与 `polished_text`

### Requirement: Batch merge of multiple history sessions with undo

The history window SHALL allow users to merge two or more selected dictation sessions into a single chronological session with an 8-second undo window.

#### Scenario: 多选会话合并并在超时后落库
- **WHEN** 用户在历史页选中 ≥2 条会话并点击「合并」
- **THEN** 列表立即预览合并后的单条会话（以最早会话的 `session_id` 为主会话，所有分段按时间从早到晚重排连续 `seq`），底部弹出 8 秒「撤销」Toast；8 秒到期后通过 `HistoryStore` 持久化合并

#### Scenario: 用户在 8 秒内撤销合并
- **WHEN** 用户在合并后的 8 秒窗口内点击 Toast 上的「撤销」按钮
- **THEN** 取消待提交的合并操作，各会话恢复合并前的独立状态与原始分段归属

### Requirement: Promote single or multiple history sessions to a meeting

The history window SHALL allow users to promote a single selected session or multiple selected sessions into a new ended meeting record (by copying segments) and navigate directly to that meeting in the Meeting workbench.

#### Scenario: 单条或多条历史转为会议并跳转
- **WHEN** 用户在单选态点击「转为会议」或在多选态点击「合并转为会议」
- **THEN** 系统在 `meetings` 表创建一条 `status = 'ended'` 的新会议记录，将所选历史会话的分段按时间顺序复制写入该 `meeting_id`（`trigger_mode = 'meeting'`），保留原日常转写历史不变
- **AND** 主窗口自动切换到「会议」页并选中打开该新建会议，用户可直接选模板生成纪要、重命名说话人或编辑导出

---

# history-content-organization：需求规格说明（中文对齐附注）


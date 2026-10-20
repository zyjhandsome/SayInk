## ADDED Requirements

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
- **WHEN** 已配置完整的 LLM 服务，用户在单条历史详情点击「润色」（已有润色时为「重新润色」）
- **THEN** 系统异步调用 `TextPolisher` 对该会话的 `raw_text` 进行润色，完成后写回 `polished_text`，自动切换到「润色」视图并显示完成反馈

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

## 目的
打通日常转写历史（`HistoryWindow`）与结构化整理之间的断层：高频轻量微调（补润色、改错字、合并断句会话）在历史页原地闭环；深度提炼（结构化纪要、待办提取、说话人改名）一键复制转入「会议」页工作台完成。

## 范围
### 本次范围
- 单条历史会话：原地编辑保存（写 `polished_text` 保 `raw_text`）、事后补润色/重新润色（`TextPolisher`）、一键转为会议。
- 多选历史会话：物理合并为一条会话（带 8 秒 Toast 撤销）、一键合并转为会议。
- 跨页联动：转为会议后主窗口自动切到「会议」页并定位到新会议详情。

### 非目标
- 不在「历史」页内置纪要模板下拉框、Markdown 双栏编辑器或说话人重命名弹窗。
- 不修改任何历史记录的 `raw_text`。

## 兼容性与外部契约
- SQLite 表结构无新增列，全部复用 `history` 与 `meetings` 现有字段。
- `HistoryWindow(store, parent=None, *, config=None, meeting_store=None)` 保持对旧单测 `HistoryWindow(store)` 的完全兼容。

## 验收矩阵
| 需求/场景 | 验证方法 | 可证伪的失败表现 |
|---|---|---|
| 单条编辑保存与取消 | `pytest tests/test_history_window.py` | 保存后 `raw_text` 被篡改，或切到原文看不到初始文本 |
| 单条事后 AI 润色与失败保护 | `pytest tests/test_history_window.py` | 润色成功后未写回 `polished_text`，或失败时清空了已有文本 |
| 多选合并会话与 8 秒撤销 | `pytest tests/test_history_store.py tests/test_history_window.py` | 合并后 `seq` 乱序、撤销后原会话未恢复、或到期未落库 |
| 单条/多条转为会议（复制分段）并跳转 | `pytest tests/test_meeting_store.py tests/test_history_window.py` | 原历史被删、新会议分段缺失、或未触发跳转信号 |

## 已确认决策
| 决策项 | 选择 | 批准人/日期 | 影响的需求 |
|---|---|---|---|
| Q1 转为会议时原历史处理方式 | A. 复制分段到新会议，保留原日常历史 | 用户 / 2026-10-18 | Promote single or multiple history sessions to a meeting |
| Q2 多选合并会话撤销机制 | A. 支持 8 秒 Toast 撤销 | 用户 / 2026-10-18 | Batch merge of multiple history sessions with undo |

## 显式未知项
只能保留不阻塞实施的未知项：
- 无阻塞未知项。

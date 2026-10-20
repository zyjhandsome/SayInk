## Why

当前「历史」页（`HistoryWindow`）只支持浏览、切换润色/原文、复制、导出 `.md` 与删除：
1. 转写时若未开自动润色、或遇到网络波动回退原文、或有少量识别错字，事后无法在历史页内补跑润色或手动修正文本。
2. 一次长转写因中途停顿被切成多条会话时，无法在历史页合并为一条会话。
3. 当单条长语音或多条零散语音需要提炼摘要、要点与待办时，虽然「会议」页（`MeetingPage`）已有完整的纪要工作台，但历史页缺少一键转入会议的通道。

## What Changes

采用探索阶段选定的「方向 A+B 分层组合」方案：
- **历史页内轻量整理（方向 B）**：
  - 单条会话支持事后 **「AI 润色 / 重新润色」**（调用 `TextPolisher` 并通过 `HistoryStore.enqueue_write` 回写 `polished_text`，保留 `raw_text`）。
  - 单条会话支持 **「编辑」**（原地修改保存至 `polished_text`，`raw_text` 永远不可变）。
  - 多选会话支持 **「合并为一条会话」**（按时间顺序将多条会话分段归并到最早一条的 `session_id` 并重排 `seq`）。
- **一键转入会议工作台深加工（方向 A）**：
  - 单条或多选历史会话支持 **「转为会议」/「合并转为会议」**，在 `meetings` 表创建已结束状态的会议记录并关联分段，随后自动切换至「会议」页定位到该场次，直接复用多模板纪要生成、说话人命名与 Markdown 编辑能力。

## Capabilities

### New Capabilities

- `history-content-organization`: 历史会话的单条补润色、内联编辑、多条会话合并及一键转为会议记录。

## Impact

- 代码：`sayink/ui/history_window.py`、`sayink/history_store.py`、`sayink/meeting_store.py`、`sayink/ui/main_window.py`、`sayink/app.py`
- 测试：`tests/test_history_window.py`、`tests/test_history_store.py`、`tests/test_meeting_store.py`
- 数据库 schema：复用现有 `history` 与 `meetings` 表，**无需新增表或列**
- 配置/依赖：无新增第三方依赖，复用现有 `llm.*` 配置

---

# history-content-organization：需求与代码事实简报

## 意图

### 目标与成功标准
- 目标：在「历史」页提供轻量原地微调（单条补润色、单条编辑保存、多条合并为一条会话），并提供一键桥接通道将单条或多条历史会话转为「会议」记录以复用结构化纪要工作台。
- 可观察的成功结果：
  1. 在历史页选中单条会话时，可触发「AI 润色」（已有润色时为「重新润色」），完成后更新 `polished_text` 并切到「润色」视图，`raw_text` 保持不变。
  2. 在历史页选中单条会话时，可进入编辑态修改文本并保存至 `polished_text`，切换「原文」仍可查看初始 `raw_text`。
  3. 在历史页多选（≥2 条）会话时，可执行「合并为一条会话」，按时间从早到晚合并分段并重排 `seq`。
  4. 在历史页单选或多选会话时，可执行「转为会议」（多选为「合并转为会议」），自动在「会议」页打开新生成的会议记录，可直接点击「生成纪要」或重命名说话人。

### 边界与非目标
- 本次范围：`HistoryWindow` 单条/多选整理操作、`HistoryStore` 分段更新与会话合并方法、`MeetingStore` 从历史会话创建会议的方法、`MainWindow` 历史页到会议页的跳转接线。
- 非目标：
  - 不在「历史」页重复建设带模板下拉框、说话人重命名、章节跳转的第二套纪要编辑器。
  - 不修改或覆盖 `history.raw_text`（所有编辑与补润色均只写 `polished_text`）。
  - 不涉及音频文件保存或外部录音文件导入。
- 禁止修改路径：`sayink/audio_recorder.py`、`sayink/speech_recognizer.py`、`sayink/vad_segmenter.py`、`sayink/hotkey_manager.py`。

## 代码事实

### 现状摘要
- `HistoryWindow`（`sayink/ui/history_window.py`）构造时仅接收 `(store, parent)`，目前不持有 `config` 或 `TextPolisher` 引用。
- 单选详情区（`_expand_session`，L1363–1411）通过 `_render_segments_html` 将分段渲染在只读 `_DetailTextEdit`（`self._details`）中；当 `_session_has_polished(segments)` 为真时显示 `self._view_bar`（「润色 | 原文」切换）。
- 多选状态（`_on_selection_changed`，L1548–1585）右侧渲染 `_render_multi_selection_html`，工具栏显示 `self._copy_btn`（复制 N 条）、`self._export_btn`（导出 N 条）、`self._delete_btn`（删除 N 条），并隐藏 `self._detail_more_btn`。
- `HistoryStore`（`sayink/history_store.py`）使用单后台写线程，提供通用写入口 `enqueue_write(fn, failure_notice=...)`（L355–369）与同步屏障 `flush()`（L399–409），并在每次提交后触发 `add_committed_callback` 刷新 UI。
- `MeetingStore`（`sayink/meeting_store.py`）共用同一个 `HistoryStore`，会议文字稿就是 `history` 表中 `session_id = meeting.id` 且 `trigger_mode = 'meeting'` 的行；`MeetingPage.show_meeting(meeting_id)`（`sayink/ui/meeting_page.py:1009`）支持直接跳转到指定会议的浏览详情页。
- `MainWindow`（`sayink/ui/main_window.py:296-306`）同时持有 `self._history`、`self._meeting`、`self._config` 与 `self._history_store`，可通过信号完成跨页导航。

### 可复用 / 需扩展 / 冲突
#### 可直接复用
- `HistoryStore.enqueue_write` 单写线程事务机制。
- `TextPolisher` / `chat_completion`（`sayink/text_polisher.py`）短文本润色请求与 `polish_settings_complete` 校验。
- `MeetingStore` 表结构与 `MeetingPage.show_meeting(meeting_id)` 页面定位接口。

#### 需要扩展
- `HistoryStore`：新增更新单条会话分段 `polished_text` 的方法、将多条 `session_id` 合并为一条会话的方法。
- `MeetingStore`：新增从一条或多条历史会话创建 `STATUS_ENDED` 会议记录并写入 `history` 分段（`trigger_mode = 'meeting'`）的方法。
- `HistoryWindow`：增加单条「润色 / 编辑 / 转为会议」与多选「合并会话 / 合并转为会议」操作入口，以及未配置 AI 服务时的跳转信号。
- `MainWindow`：连接 `HistoryWindow` 的「打开会议详情」与「去配置 AI 服务」信号。

#### 需求与现状冲突
- 无架构冲突。唯一需要注意的是单条会话可能包含多个分段（如持续转写产生的多段 `seq`），编辑或补润色时需明确如何对应到分段（见代码事实与设计约束：单段会话直接写入该段 `polished_text`；多段会话按段润色或按空行分段回写）。

### 挂载点候选
| 优先级 | 路径/符号 | 理由 |
|---|---|---|
| 必选 | `sayink/history_store.py` `HistoryStore` | 新增分段 `polished_text` 更新、多会话合并、转为会议的数据写入方法 |
| 必选 | `sayink/meeting_store.py` `MeetingStore` | 新增将历史会话转为已结束 `MeetingRecord` 的事务方法 |
| 必选 | `sayink/ui/history_window.py` `HistoryWindow` | 新增单条（润色/编辑/转为会议）与多选（合并会话/合并转为会议）UI 及交互 |
| 必选 | `sayink/ui/main_window.py` `MainWindow` | 连接历史页转会议后跳转 `show_page("meeting")` + `meeting_panel.show_meeting(id)` |
| 备选 | `tests/test_history_window.py` / `tests/test_history_store.py` | 覆盖新增整理行为的单元与离屏 UI 测试 |

### 波及线索
- 调用方：`MainWindow._setup_ui` 构造 `HistoryWindow(self._history_store, self, config=self._config)`（保持 `config=None` 可选以兼容既有单测 `HistoryWindow(store)`）。
- 共享状态：所有 SQLite 写入均走 `HistoryStore.enqueue_write`，不破坏单写线程不变量；历史页转会议不会触碰正在运行的 `MeetingController` 录音状态机（直接生成 `status = 'ended'` 的历史会议记录）。
- 相关测试：既有 `tests/test_history_window.py` 对工具栏按钮可见性与文案有断言，新增按钮需遵循现有设计 token 与模式切换规则。

### 质量画像与验收

| profile | required / not_required | 证据与验收 |
|---|---|---|
| functional | required | 单条补润色、单条编辑保存、多条合并会话、单条/多条转为会议并跳转均通过 pytest 验证（含控件可见性、样式 token 与状态切换断言） |
| visual | not_required | 本变更是在原生 PyQt6 `HistoryWindow` 工具栏/菜单上增量挂载已有样式的按钮（`settings_styles.BTN_GHOST_SM` / `BTN_PRIMARY`），非前端 UI 框架升级或跨仓视觉对齐；控件可见性、主题切换样式（light/dark）通过离屏 Qt pytest 断言覆盖 |
| accessibility | not_required | 延续既有按钮 `AccessibleName` 与 `TabFocus` 约定 |
| performance | not_required | 本地 SQLite 事务在后台单写线程执行，LLM 润色在 `QThread` 异步执行，不阻塞 UI 线程 |
| compatibility | required | `HistoryWindow(store)` 无 `config` 参数的旧调用签名保持向后兼容；不改数据库表结构 |

### 证据表

| 类型 | 结论 | 证据 |
|---|---|---|
| 事实 | `HistoryWindow` 目前只有只读 `_DetailTextEdit` 和复制/导出/删除操作 | `sayink/ui/history_window.py:1051-1089` |
| 事实 | `HistoryStore` 提供 `enqueue_write(fn)` 在唯一写线程执行任意 SQLite 事务 | `sayink/history_store.py:355-369` |
| 事实 | `MeetingStore` 与 `HistoryStore` 共用 `history.db`，会议分段即 `trigger_mode='meeting'` | `sayink/history_store.py:37-85`, `sayink/meeting_store.py:452-467` |
| 事实 | `MeetingPage.show_meeting(meeting_id)` 可直接打开指定会议详情 | `sayink/ui/meeting_page.py:1009-1022` |
| 事实 | 用户已确认采用「方向 A+B 分层组合」方案立项 | 会话回复「可以，按照推荐方案开展」 |
| 决策 | Q1 转为会议时原历史去留；Q2 多选合并会话是否支持撤销 | 见下方「开放问题清单」 |

## 消歧与闸门

### 开放问题清单

| 优先级 | 问题 | 代码事实背景 | 选项与影响（摘要） | 建议 | 状态 | 最终决策 |
|---|---|---|---|---|---|---|
| 必选 | Q1 历史会话「转为会议」时，原历史记录如何处理？ | `HistoryStore.list_sessions` 过滤了 `trigger_mode != 'meeting'`；若直接改 `trigger_mode`，原记录会从历史列表消失；若复制分段，则两边独立保留 | A. 复制分段到新会议（原历史保留） / B. 移动为会议（从日常历史列表移除） | A | decided | **A**（2026-10-18 用户点选）— 复制分段到新会议，原日常转写历史保留 |
| 必选 | Q2 多选「合并为一条会话」时，是否复用现有的 8 秒 Toast 撤销机制？ | 删除操作目前使用 8 秒 `_toast` 延迟提交（`_pending_delete`）；合并会话若支持 8 秒撤销可防止误合并 | A. 支持 8 秒 Toast 撤销（与删除一致） / B. 直接合并落库（无需撤销或弹确认框） | A | decided | **A**（2026-10-18 用户点选）— 支持 8 秒 Toast 撤销（与删除体验一致） |

### 澄清完整性扫描
- 已检查的适用维度：
  - 使用者与入口：历史页单选工具栏/菜单、多选工具栏；未配置 LLM 时点击「AI 润色」提示或跳转「AI 服务」设置页。
  - 正常状态变化：单条补润色写回 `polished_text`、单条编辑保存写回 `polished_text`、多条合并会话按时间重排 `seq`（支持 8 秒撤销）、转为会议时复制分段到新建 `MeetingRecord` 并跳转 `MeetingPage`。
  - 失败与恢复：LLM 润色失败时通过 `_show_feedback` 提示且不覆盖已有文本；数据库写失败走既有 `_notify_failed`。
  - 数据保存与隐私：`raw_text` 永远保留不改；补润色复用既有 `llm.*` 端点安全校验（`is_secure_or_local_url`）。
  - 向后兼容：`HistoryWindow(store, parent=None, *, config=None)` 保持旧测试零改动通过。
- 由证据解决的缺失事实：`MeetingPage.show_meeting` 已存在，无需在 `MeetingPage` 新写一套详情加载逻辑。
- 新增开放问题及处理状态：Q1=A（复制分段到新会议）、Q2=A（8 秒 Toast 撤销），均已 decided。
- 明确不适用 / 不在范围的维度：音频录制、ASR 识别模型切换、跨进程协议。
- 结论：无实质阻塞项

### 风险定级与闸门建议
- 建议车道/风险：Standard / `medium`
- 命中的风险特征：跨 `HistoryWindow`、`HistoryStore`、`MeetingStore`、`MainWindow` 多个模块协同；涉及用户历史数据的合并与修改；包含 UI 交互状态扩展。
- 未命中的高风险特征：不涉及 auth/payment/permission；不破坏隐私承诺（`raw_text` 保留，LLM 走既有配置）；无需数据库 schema 迁移（复用现有列）；不触碰核心录音/ASR 实时流水线。
- 不确定点：无阻塞不确定点（Q1、Q2 已由用户确认）。
- 闸门建议：进入规格闸门，确认后进入 `delivery-plan-tasks` 编写 `design.md` 与 `tasks.md`。
- 可用验证：`pytest tests/test_history_window.py tests/test_history_store.py tests/test_meeting_store.py -q`
- 缺失验证：新增整理动作的单测与离屏 UI 测试（将在实施阶段按 TDD 补齐）。

### Explore 交接消费

- [x] `chosen_direction` → 已写入「意图」（方向 A+B 分层组合：历史页内轻量闭环 + 一键转入会议工作台）
- [x] `non_goals` → 已写入「意图」边界（不在历史页造第二套纪要编辑器、不改 `raw_text`、不存音频）
- [x] `code_anchors` → 已驱动「代码事实」与「挂载点候选」检查（`HistoryWindow`、`HistoryStore`、`MeetingStore`、`MeetingPage`、`TextPolisher`、`MainWindow`）
- [x] `risk_signal` → 仅作线索；「风险定级与闸门建议」已按代码事实重算为 Standard / `medium`
- [x] `unknowns` → 已转化为「开放问题清单」中的 Q1 与 Q2

落点摘要：意图=A+B分层历史整理；挂载=HistoryWindow/HistoryStore/MeetingStore/MainWindow；Risk=medium；开放问题=Q1/Q2

### 状态源与工件位置
- 后端：OpenSpec change
- 路径：`openspec/changes/history-content-organization/`
- 闸门记录：
  - 规格批准状态 = **已批准**
  - 批准人：user（点选「建议：批准范围并进入设计」）
  - 批准时间：2026-10-18T00:25:00+08:00
  - binds_to_revision（规格）：`sha256:1885b8e49a34f17dca7e91c39ceafdc9605f408c407a28ebf6bd4cc5218db1c4`
  - accepted_warning_ids：`[]`
  - 实现批准状态 = **已批准**
  - 实现批准人：user（点选「建议：开始实施」）
  - 实现批准时间：2026-10-18T00:30:00+08:00
  - 实现批准 binds_to_revision：见同目录 `handoff.json` 的 `source_revision.artifact_revision`
  - accepted_warning_ids（实现）：`[]`
  - 下一阶段 = `delivery-execute-verify`

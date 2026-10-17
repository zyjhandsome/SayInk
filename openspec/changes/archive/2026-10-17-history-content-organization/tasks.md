# history-content-organization：实施任务清单

## 执行规则
- 权威状态源：`openspec/changes/history-content-organization/`
- 风险/闸门：Standard / `medium`；须通过实现闸门获得用户放行后方可执行代码变更。
- 禁止范围：`sayink/audio_recorder.py`、`sayink/speech_recognizer.py`、`sayink/vad_segmenter.py`、`sayink/hotkey_manager.py`；禁止修改 `history.raw_text`。
- 必须执行的最终验证：`python -m pytest tests/test_history_store.py tests/test_meeting_store.py tests/test_history_window.py tests/test_main_window.py -q`

## 任务

- [x] 任务 1：实现存储层历史分段润色回写、多会话合并与复制转会议接口
  - 对应需求/场景：
    - Single-session inline editing preserves raw text
    - Batch merge of multiple history sessions with undo
    - Promote single or multiple history sessions to a meeting
  - 前置依赖：无
  - 目标文件/符号：`sayink/history_store.py` (`HistoryStore.enqueue_update_polished`, `HistoryStore.enqueue_merge_sessions`)；`sayink/meeting_store.py` (`MeetingStore.create_meeting_from_sessions`)；`tests/test_history_store.py`；`tests/test_meeting_store.py`
  - 允许修改：`sayink/history_store.py`、`sayink/meeting_store.py`、`tests/test_history_store.py`、`tests/test_meeting_store.py`
  - 禁止修改：`sayink/audio_recorder.py`、`sayink/speech_recognizer.py`
  - 实施步骤：
    1. 在 `tests/test_history_store.py` 与 `tests/test_meeting_store.py` 中先编写失败单测：验证 `enqueue_update_polished` 只更新指定 `polished_text` 不改 `raw_text`；验证 `enqueue_merge_sessions` 将多条会话按时间正序合并到目标 `session_id` 并重排连续 `seq = 0..N-1`；验证 `create_meeting_from_sessions` 复制所选历史会话分段生成 `STATUS_ENDED` 的会议记录且原历史会话保留不变。
    2. 在 `HistoryStore` 中实现 `enqueue_update_polished` 与 `enqueue_merge_sessions`（走 `enqueue_write` 单写线程事务）。
    3. 在 `MeetingStore` 中实现 `create_meeting_from_sessions`（走 `_write` 单写线程事务，自动计算 `started_at`、`ended_at` 与默认标题，复制分段并标记 `trigger_mode = 'meeting'`）。
  - 失败测试或已批准替代验证：TDD 先跑新增单测确认紅灯（AttributeError），实现后转绿。
  - 验证命令/动作：`python -m pytest tests/test_history_store.py tests/test_meeting_store.py -q`
  - 预期结果：所有测试通过（exit code 0），原历史 `raw_text` 未被修改，合并后 `seq` 连续，转为会议后原历史与新会议分段独立存在。
  - 质量层/证据：`functional`、`compatibility`
  - 迁移/回滚：无 schema 变更，可随时通过 git revert 回滚。
  - 完成定义：`test_history_store.py` 与 `test_meeting_store.py` 新增与既有测试全部通过。
  - 负责人/冲突说明：主 Agent 顺序执行。

- [x] 任务 2：实现 HistoryWindow 单条内联编辑、事后 AI 补润色与多选 8 秒可撤销合并
  - 对应需求/场景：
    - Single-session inline editing preserves raw text
    - On-demand AI polishing for a single history session
    - Batch merge of multiple history sessions with undo
  - 前置依赖：任务 1
  - 目标文件/符号：`sayink/ui/history_window.py` (`HistoryWindow`)；`tests/test_history_window.py`
  - 允许修改：`sayink/ui/history_window.py`、`tests/test_history_window.py`
  - 禁止修改：`sayink/audio_recorder.py`、`sayink/speech_recognizer.py`
  - 实施步骤：
    1. 在 `tests/test_history_window.py` 中先编写失败测试：覆盖单条会话点击「编辑 → 修改 → 保存」写回 `polished_text` 且「原文」不变、点击「取消」不保存；覆盖单条点击「润色 / 重新润色」调用 `TextPolisher` 成功后更新视图、未配置或失败时提示且不丢原稿；覆盖多选点击「合并 N 条」立即预览合并态、8 秒内点「撤销」恢复原状、到期后调用 `enqueue_merge_sessions` 落库。
    2. 扩展 `HistoryWindow.__init__` 支持可选 `config` 与 `polisher` 参数，新增单条编辑态（`_edit_btn`、`_cancel_edit_btn`）、单条润色按钮（`_polish_btn`）及多选合并按钮（`_merge_btn`），并实现 `_pending_merge` 与 8 秒 `_undo_timer` 的协同与 `flush_pending_delete` 刷新。
  - 失败测试或已批准替代验证：TDD 先跑 `test_history_window.py` 新增用例确认红灯，实现后转绿。
  - 验证命令/动作：`python -m pytest tests/test_history_window.py -q`
  - 预期结果：所有测试通过（exit code 0），单条编辑/补润色与多选合并撤销及既有历史窗口行为全部正常。
  - 质量层/证据：`functional`、`compatibility`
  - 迁移/回滚：纯 UI 与控制器逻辑变更，git revert 即可回滚。
  - 完成定义：`tests/test_history_window.py` 全部通过。
  - 负责人/冲突说明：主 Agent 顺序执行。

- [x] 任务 3：实现历史页单条/多条一键「转为会议」及 MainWindow 自动跳转会议详情
  - 对应需求/场景：
    - Promote single or multiple history sessions to a meeting
  - 前置依赖：任务 1、任务 2
  - 目标文件/符号：`sayink/ui/history_window.py` (`HistoryWindow._promote_selected_to_meeting`, `HistoryWindow.meeting_created`)；`sayink/ui/main_window.py` (`MainWindow._setup_ui`, `MainWindow._open_promoted_meeting`)；`tests/test_history_window.py`；`tests/test_main_window.py`
  - 允许修改：`sayink/ui/history_window.py`、`sayink/ui/main_window.py`、`tests/test_history_window.py`、`tests/test_main_window.py`
  - 禁止修改：`sayink/audio_recorder.py`、`sayink/speech_recognizer.py`
  - 实施步骤：
    1. 在 `tests/test_history_window.py` 与 `tests/test_main_window.py` 中先编写失败测试：验证在 `HistoryWindow` 单选点击「转为会议」或多选点击「合并转为会议」后，创建新会议并发射 `meeting_created(meeting_id)`；验证 `MainWindow` 接收到 `meeting_created` 与 `ai_settings_requested` 后，分别切换到 `"meeting"` 页打开对应会议、或切换到 `"polish"` 页聚焦 AI 连接配置。
    2. 在 `HistoryWindow` 中添加 `_to_meeting_btn`（单选显示「转为会议」，多选 `>=2` 显示「合并转为会议」），点击后调用 `MeetingStore.create_meeting_from_sessions`，刷新并发射 `meeting_created(meeting_id)`。
    3. 在 `MainWindow._setup_ui` 中将 `self._config` 传入 `HistoryWindow`，并连接 `meeting_created` 到 `_open_promoted_meeting`、`ai_settings_requested` 到 `_open_ai_connection`。
  - 失败测试或已批准替代验证：TDD 先跑新增测试确认红灯，实现后转绿。
  - 验证命令/动作：`python -m pytest tests/test_history_window.py tests/test_main_window.py -q`
  - 预期结果：所有测试通过（exit code 0），单选/多选转为会议后主窗口自动切到会议页并打开新建会议。
  - 质量层/证据：`functional`、`compatibility`
  - 迁移/回滚：git revert 即可回滚。
  - 完成定义：`test_history_window.py` 与 `test_main_window.py` 全部通过。
  - 负责人/冲突说明：主 Agent 顺序执行。

## 集成顺序
1. 任务 1（`HistoryStore` + `MeetingStore` 数据层接口与单测）
2. 任务 2（`HistoryWindow` 单条编辑、补润色、多选合并与 8 秒撤销）
3. 任务 3（`HistoryWindow` 转为会议按钮 + `MainWindow` 跨页跳转接线）

## 最终验证
| 命令/动作 | 覆盖范围 | 预期结果 |
|---|---|---|
| `python -m pytest tests/test_history_store.py tests/test_meeting_store.py tests/test_history_window.py tests/test_main_window.py -q` | 存储层更新/合并/转会议、历史页单选/多选整理、主窗口跨页联动 | 全部 PASS（exit code 0） |
| `openspec validate history-content-organization` | OpenSpec 工件结构与 delta spec 校验 | Validation passed |

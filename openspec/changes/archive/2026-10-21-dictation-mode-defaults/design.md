## Context

单次转写和持续转写共用 `output.mode`。粘贴目标在 `TextPaster.paste_async` 里才读取前台窗口。缺模型时欢迎要等模型 `ready` 或 15 秒。会议文字编辑在 `MeetingController.save_segment_text` 和 `MeetingStore.set_segment_text` 里拒绝 `status = live`。规格闸门已通过，绑定 `sha256:9212320eb7519fb6a347bce12e4c8e69fb84b87d101be0770dcc53a6861b8119`（当时只有 proposal 与 delta spec）。

录音侧已经能知道一句话何时开口：`AudioRecorder._feed_segmenter` 在 `in_speech` 从假变真时写下 `_segment_started_at`。这段发生在 UI 线程的 `QTimer` 回调 `_on_continuous_tick` 里，不在 PortAudio 回调里。持续转写的浮条由 `FloatingWindow.show_listening` 维持。实时文字稿的悬停入口由 `_TranscriptView._CONTEXT_LINKS` 点亮，直播点击走 `_on_live_transcript_link`。

## Goals / Non-Goals

**Goals:**

- 按已批准规格实现分模式结果去向、一次迁移、开口窗口粘贴、缺模型准备页、开录前历史冲突选择、会中修改已落段文字。
- 失败时不把字粘进新的前台窗口，不丢掉会议原文。

**Non-Goals:**

- 不捆绑 Fun-ASR-Nano，不改会议的开始、暂停、结束和 Esc，不把会议文字粘贴到其他应用。
- 不改默认音源、快捷键和默认关闭润色，不在准备页配置 AI。
- 不把编辑入口改成常驻按钮。

## Decisions

### 决策 1：用 `output.by_mode` 保存两份去向，旧字段只做当前模式的镜子

- 选择：在 `output` 下增加 `by_mode.hotkey` 与 `by_mode.continuous`，每份含 `mode` 和 `follow_input_source`。`Config.output_mode`、`set_output_mode`、`output_follows_input_source`、`follow_input_source_for_output` 都只读写当前 `audio.trigger_mode`。每次写入同时把旧的 `output.mode` / `output.follow_input_source` 写成当前模式的值。
- 理由：设置页仍然只有一个组合框，它跟着「当前选中的录制方式」走。旧测试和任何仍读 `output.mode` 的代码看到的是正在使用的那一种，而不是被丢掉的另一种。
- 未选：删掉旧字段。回滚到旧版本时，当前模式的选择会丢。
- 归属：Agent。键名不改变已批准的可观察行为。

### 决策 2：开口时在 UI 线程记下窗口，粘贴时只跟这个窗口比

- 选择：单次在 `App._on_recording_start` 记下前台句柄和进程名，整次按住共用。持续在 `_feed_segmenter` 发现开口的同一次 tick 里记下，放进 `_SegmentContext`，随队列走到 `_output_text`。`paste_async` 增加已记住的句柄；当前前台不一致、句柄为空、是 SayInk 自己或目标拒绝模拟输入时，不发送粘贴键。
- 理由：开口判定已经在 UI 线程，不必从音频回调里读前台窗口。句柄不写入数据库。
- 未选：整场持续转写锁一个窗口。这与已批准的「每段开口」冲突。
- 归属：Agent。进程名沿用 `get_foreground_process_name`，不记录窗口标题。

### 决策 3：准备页只替换「没有模型」的首次欢迎

- 选择：`OnboardingController.schedule` 在欢迎未看过且 `model_installed()` 为假时，约 400 毫秒后打开准备页，不再挂 15 秒兜底。模型已在时保持现有 `ready` 加 15 秒兜底。准备页内试说只把文字显示在页上。
- 理由：完整包的路径已经可用，规格要求不动。
- 归属：Agent。麦克风测试复用现有录音音量，不新做采集栈。

### 决策 4：会中改字沿用说话人纠正的门闩

- 选择：`save_segment_text` 在会议为 live、且就是当前这场、且状态不是 `DRAINING`、且没有在生成时允许保存。`set_segment_text` 的 SQL 状态条件加入 `live`。暂停仍是 live，因此同样可改。实时文字稿增加 `live-edit:` 链接，并把它放进 `_CONTEXT_LINKS`，点击调用已有的 `_prompt_segment_text`。
- 理由：说话人纠正已经处理了收尾期间的迟到保存。正文走同一道门，避免两套并发规则。
- 未选：生成期间也允许改字。规格明确拒绝。
- 归属：Agent。

## Risks / Trade-offs

- [升级后另一种模式回到自动] → 只迁移一次；`by_mode` 已存在则不再读旧字段。当前模式的明确选择写入镜子字段，便于回滚时至少保住正在用的模式。
- [开口后几毫秒内用户已经切走窗口] → 比较失败就留剪贴板，不补粘。
- [持续转写浮条要显示目标，又不能挡住波形] → 只改 `show_listening` 的副文案，不新增一块面板。
- [会中编辑时新段落追加] → 保存的是 `corrected_text`，新段落是新的 `seq` 插入，不更新旧行。
- [准备页和模型下载进度抢窗口] → 准备页的下载动作打开现有引擎页，不自己再做一套下载器。

## Migration Plan

1. 加载配置时，若 `output.by_mode` 缺少 `hotkey` 或 `continuous`，按规格迁移一次：自动则两份都自动；明确选择则已保存的 `audio.trigger_mode` 保留，另一份自动。缺省录制方式当单次。
2. 回滚代码时不删除用户配置。旧代码只读 `output.mode`，看到的是最后一次使用的模式。另一种模式的单独选择在回滚后不可见，直到再次升级。
3. 会议库不新增列。回滚只是恢复「live 不能写 `corrected_text`」。已写成的 `corrected_text` 保留。

## Open Questions

无。产品选择已在规格闸门关闭。下面的技术选择由 Agent 记录，不再询问。

---

# 口述模式默认体验：技术实施计划

## 已批准目标与约束

- 目标：见 `specs/dictation-mode-defaults/spec.md` 的七条需求。
- 非目标：见上文 Non-Goals。
- 风险/闸门：High。规格闸门已通过。实现闸门在就绪审查之后单独询问。
- 深度：High，本文件保持完整设计，不使用中等风险的压缩形态。

## 已刷新代码事实

| 结论 | 证据 | 新鲜度 |
|---|---|---|
| 开口时刻已在 `_feed_segmenter` 计算，且该函数由 UI 线程的 `_on_continuous_tick` 调用 | `sayink/audio_recorder.py` | 与 HEAD `737e792` 一致，本轮读过源码 |
| 单次开始于 `App._on_recording_start`，持续开始于 `App._start_continuous_listening` | `sayink/app.py` | 同上 |
| 段落上下文是 `_SegmentContext`，目前没有目标窗口 | `sayink/app.py` | 同上 |
| 粘贴延迟内的焦点变化已经会留下剪贴板 | `TextPaster.paste_async` | 同上 |
| 会中文字保存被控制器和 SQL 双重拒绝 | `save_segment_text`、`set_segment_text` | 同上 |
| 悬停点亮依赖 `_CONTEXT_LINKS`；直播点击在 `_on_live_transcript_link` | `sayink/ui/meeting_page.py` | 同上 |
| 没有其他活跃 OpenSpec change | `openspec list` 早前返回空；就绪前再查一次 | 见就绪审查 |

## 技术决策清单

| ID | 待决事项 | 决策归属 | 实质影响 | 选项与建议 | 状态 | 最终结论与记录 |
|---|---|---|---|---|---|---|
| D1 | 两份去向存在哪 | Agent | 只影响配置形状和回滚可见性 | 嵌套 `by_mode` 并镜像旧字段 | decided | 决策 1 |
| D2 | 在哪一个线程读前台窗口 | Agent | 避免音频回调里读 UI | UI 线程的开口 tick | decided | 决策 2 |
| D3 | 准备页是否替换完整包欢迎 | Agent | 规格已要求不替换 | 仅缺模型 | decided | 决策 3 |
| D4 | 会中保存与收尾的并发规则 | Agent | 与说话人纠正一致 | 复用 DRAINING / generating 拒绝 | decided | 决策 4 |
| D5 | 升级映射、每段窗口、浮条、会中改已落段 | 用户 | 可观察行为 | 已在规格中决定 | decided | proposal 开放问题清单 |

没有留给用户的技术阻塞项。

## 方案比较

### 方案 A

- 方案形态：分模式配置加镜像字段；开口窗口放在内存上下文；准备页和会中编辑分别接到现有入口。
- 收益：每条失败都能落到现有测试文件，回滚保留当前模式。
- 成本/风险：`app.py` 被粘贴目标和开录前选择先后修改，必须串行。
- 可逆性：配置与会议正文都可回滚代码而不删用户数据。
- 验证方式：现有 pytest 加对应失败断言。

### 方案 B

- 方案形态：结果去向每次切换模式时弹窗询问，不持久化第二份。
- 收益：没有迁移。
- 成本/风险：和「各自记住」冲突，每次切换都打断。
- 可逆性：高，但是不合格。
- 验证方式：不适用。

## 最终决策

- 选定方案：方案 A。
- 选择理由：它满足「各自记住」和「只迁移一次」，并复用开口检测、悬停编辑和引擎下载。
- 未选方案及原因：方案 B 会在每次切换时询问，不是已批准行为。
- 决策来源 / 批准记录：规格闸门 `sha256:9212320eb7519fb6a347bce12e4c8e69fb84b87d101be0770dcc53a6861b8119`。

## 集成方式与数据流/控制流

1. 设置组合框改变时，只写当前录制方式的 `by_mode` 槽位，再跑该槽位的音源联动。
2. 单次按键：先处理历史冲突，再记下前台窗口，再打开设备。识别完成后把这个窗口交给 `paste_async`。
3. 持续开口：tick 里保存窗口；`segment_ready` 时放进 `_SegmentContext`。输出时同样交给 `paste_async`。浮条副文案显示进程名。
4. 目标不一致：`PasteResult("clipboard", detail="target_changed")`。现有浮条分支加一句原来的进程名，不新增模态框。
5. 准备页关闭后沿用现有历史提问。
6. 会中 `live-edit:` → `_prompt_segment_text` → `save_segment_text` → `set_segment_text`。新段落仍按新 `seq` 插入。

## 接口与状态模型

- `output.by_mode.hotkey` / `output.by_mode.continuous`：`mode` 为 `paste` 或 `history`，`follow_input_source` 为布尔。
- `_SegmentContext` 增加 `target_hwnd: int = 0`、`target_app: str = ""`。不进 SQLite。
- `paste_async(text, callback, *, target_hwnd: int = 0, target_app: str = "")`。缺省 0 表示沿用今天「粘贴当时的前台」只用于没有开口记录的旧调用；产品路径必须传入开口时的句柄。
- 直播链接 scheme：`live-edit`。

## 失败处理与可观测性

- 读不到前台窗口、比较失败、写入剪贴板失败：不发送粘贴键。日志只写进程名或「无目标」，不写窗口标题。
- 历史冲突对话框取消：不调用 `recorder.start`。持续转写进行中取消：结束会话，当前段留在剪贴板。
- 会中保存失败：对话框保持草稿，段落原文不变。收尾或生成中返回 false。
- 准备页麦克风没有声音：页内说明，可重试或关闭。关闭仍标记欢迎已看过。

## 兼容、迁移与回滚

见 Migration Plan。会议不新增列，不改 `history.enabled` 对会议记录的含义。

## 安全与性能

- 安全：界面和日志只用进程名。不新增个人信息收集，不碰 API Key。
- 性能：每次开口一次前台窗口查询。不改变识别和下载。缺模型准备页不得等待 15 秒。

## 验证策略

### 质量画像

| profile | requirement/acceptance | task | validation |
|---|---|---|---|
| functional | 七条需求的场景 | 任务 2–6 | 各任务的 pytest |
| visual | 准备页、冲突选择、浮条目标、会中编辑、会后提示 | 任务 1 与任务 7 | offscreen 截图 |
| accessibility | 准备页和冲突选择的主按钮有可访问名称，默认焦点在推荐动作 | 任务 4、任务 5 | pytest 读取 accessibleName 与 default button |
| performance | 不新增指标；缺模型不得等 15 秒 | 任务 4 | 测试断言没有挂 15000 毫秒兜底 |
| compatibility | 旧配置只迁移一次 | 任务 2 | 加载两次的配置测试 |

### Visual validation matrix（visual=required 时）

桌面 Qt，offscreen，不是浏览器。窄宽度沿用会议预览的窄窗口。基线替代：仓库里已有的 `docs/ux-preview/meeting/` 只作会议页对照；本次新状态没有旧像素基线，以规格文字和截图核对。

| ID | baseline/substitute | route | state | browser/viewport | diff policy | evidence path | required_for_verified | failure proves |
|---|---|---|---|---|---|---|---|---|
| V1 | 无旧图；对照规格「下载模型」 | 缺模型准备页 | 待下载 | Qt offscreen，默认宽度 | 必须看见下载、麦克风测试、试说，且没有 AI 字段 | `openspec/changes/dictation-mode-defaults/visual-evidence.md` | yes | 第一眼仍要等下载入口或出现 AI 配置 |
| V2 | 无旧图 | 准备页 | 麦克风未听到 | Qt offscreen，默认宽度 | 必须能重试或关闭 | 同上 | yes | 失败后无法离开 |
| V3 | 现有录音浮条 | 持续转写浮条 | 已有目标进程名 | Qt offscreen，录音条尺寸 | 只增加进程名，不改主题色 | 同上 | yes | 看不出字会进哪个程序 |
| V4 | 无旧图 | 开录前选择 | 历史关闭且只记历史 | Qt offscreen，默认宽度 | 两个选择加取消，默认焦点在开启历史 | 同上 | yes | 可以直接开始录音 |
| V5 | `docs/ux-preview/meeting/hover-transcript-*.png` | 会议进行中文字稿 | 悬停已落段 | Qt offscreen，默认与窄宽度 | 出现编辑，正文不重排 | 同上 | yes | 进行中没有编辑入口 |
| V6 | 现有会后文字稿 | 已结束且无纪要 | 提示 | Qt offscreen，默认宽度 | 提示不再要求先生成纪要 | 同上 | yes | 用户仍被写成必须等生成 |

## 需求追溯

| 需求/场景 | 设计要素 | 任务 | 验证 |
|---|---|---|---|
| 分模式去向与音源联动 | 决策 1 | 任务 2 | `tests/test_config_audio.py` `tests/test_settings_general.py` |
| 旧配置只迁移一次 | Migration Plan | 任务 2 | 同上，加载两次 |
| 开口窗口与目标变化浮条 | 决策 2 | 任务 3 | `tests/test_text_paster.py` `tests/test_app.py` |
| 缺模型准备页 / 模型已在的欢迎 | 决策 3 | 任务 5 | `tests/test_onboarding.py` |
| 历史关闭且只记历史 | 失败处理 | 任务 4 | `tests/test_app_history_wiring.py` |
| 会中修改已落段文字 | 决策 4 | 任务 6 | `tests/test_meeting_ux.py` |
| 视觉与可访问名称 | 验证策略 | 任务 1、任务 7 | 截图与控件查询 |

## 已知风险与非目标

真机粘贴兼容和识别速度不在自动测试里。任务 7 只证明界面状态，不证明麦克风硬件。

## 实现就绪审查

## 结论

- 就绪，条件是实现闸门获得用户批准。本审查没有阻塞项。
- 风险等级：high
- 所需批准：实现闸门。规格闸门已完成。

## 阻塞项

- 无。

## 警告项

- 无。另一种模式在代码回滚后不可见，已写进 Migration Plan，不需要用户额外接受。

## 建议项

- 实施时不要把窗口标题写进日志。

## 覆盖情况

| 需求/场景 | 技术计划 | 任务 | 验证 | 状态 |
|---|---|---|---|---|
| 分模式去向 | 决策 1 | 任务 2 | pytest | 已覆盖 |
| 迁移一次 | Migration Plan | 任务 2 | pytest | 已覆盖 |
| 开口窗口 | 决策 2 | 任务 3 | pytest | 已覆盖 |
| 浮条与不打断 | 决策 2 | 任务 3 | pytest | 已覆盖 |
| 准备页 | 决策 3 | 任务 5 | pytest | 已覆盖 |
| 模型已在的欢迎 | 决策 3 | 任务 5 | pytest | 已覆盖 |
| 开录前选择 | 失败处理 | 任务 4 | pytest | 已覆盖 |
| 会中改字 | 决策 4 | 任务 6 | pytest | 已覆盖 |
| 视觉五态以上 | Visual matrix | 任务 1、7 | 截图 | 已覆盖 |

## 技术决策就绪度

- 对齐完整性扫描：已检查迁移、并发窗口、会中收尾、准备页失败、可访问名称、回滚。证据已解决线程和现有入口。不适用支付、权限、API Key。剩余阻塞：无。
- Agent 自主决定及依据：D1–D4。
- 用户决定及记录：proposal 开放问题清单，2026-10-21。
- 回 Frame 的事项及状态：无。
- 剩余待决阻塞项：无。

## 代码事实新鲜度

- 分支/提交：main，HEAD `737e792614474e6081b2070d7a2f7d2199ff9c73`。
- 变基/重构检查：规划期间未变基。
- 缺失/改名路径：无。设计点名的符号都在当前源码中。

## 并行安全

- 独立任务组：任务 5 与任务 6 互不修改同一文件，可在任务 2 之后并行。任务 3 与任务 4 都改 `sayink/app.py`，必须串行。
- 共享文件/状态：`sayink/app.py` 归任务 3 然后任务 4。
- 所有权：见任务清单。
- 集成顺序：1 → 2 → 3 → 4，5 可与 2 并行，6 可与 5 并行，7 最后。
- **其他 active changes 路径重叠（G8）：** 无。`openspec list` 仅有本 change。

## 后端结构验证

- 命令/工具：`openspec validate dictation-mode-defaults --strict`；`node validate_delivery_change.mjs`。
- 结果：两者均已通过。`openspec list` 只有本 change，7 项任务都未勾选。G8 无重叠。High 五面 design、tasks、rollback、security、validation 均已在本文件写明，可以询问实现闸门。没有实现批准，所以仍然不能改产品代码。

## 闸门记录

- 决定：规格已批准；实现尚未批准。
- 批准人/日期：规格为用户 / 2026-10-21。实现闸门留空。
- 附加约束：无接受警告。
- High 内部五面：design 已写明数据流、失败和迁移；tasks 待文件落地后勾查；rollback 见 Migration Plan；security 只使用进程名且不碰 API Key；validation 每条任务有 pytest 或截图。五面在询问实现闸门前必须再次确认为通过。

## G1–G3、G5、G8

- G1：唯一状态源是 `openspec/changes/dictation-mode-defaults/`。仓库根没有平行 `brief.md`。
- G2 / G3：由 tasks.md 的机器字段满足，写完后用脚本复查。
- G5：规格批准已记录。没有实现批准，因此现在不得改产品代码。
- G8：无其他活跃 change。

# 音视频文件转写 + 轻翻译：需求与代码事实简报

## Why

用户需要把本地音/视频转成文字，并在转写后得到目标语言译文；当前 SayInk 只支持麦克风/系统声实时听写与同语言润色，缺少文件入口与翻译能力。先做「文件转写 + 事后轻翻译」组合 MVP，明确不做真·流式同声传译。

## What Changes

- 新增：从本地媒体文件导入音频、转写为文字，并写入历史/可查看结果
- 新增：转写完成后的轻量翻译（目标语言可配置；走现有 OpenAI 兼容 API 路径的变体）
- 修改：润色设置/提示词或后处理链路，以支持「翻译」模式（具体关系见开放问题）
- 可能修改：历史记录字段语义或 schema（若需同时保留润色文与译文）
- 非目标：真·流式同声传译、会议级低延迟双语叠字

## Capabilities

### New Capabilities

- `media-file-transcription`: 本地音/视频（或约定音频格式）文件导入、解码为 ASR 可用 PCM、整段/分段转写、进度/取消/失败提示、结果进入历史
- `light-translation`: 转写文本的事后轻翻译（目标语言、开关、失败回退原文、与润色关系）

### Modified Capabilities

- （无现有主规格；`openspec/specs/` 当前为空或已删）无 Modified Capabilities

## Impact

- 代码：新建文件导入/解码模块；`App` 编排挂载；可能扩展 `TextPolisher` / 设置页；`HistoryStore` / 历史 UI 视存储决策而定
- 依赖：可能引入 ffmpeg（或等价解码）与打包体积/CI 变化
- 验证：文件转写单测/集成；翻译 prompt 与失败回退；不回归空闲听写热键路径

---

## 意图

### 目标与成功标准

- 目标：用户可导入本地媒体文件完成转写；可在转写结果上启用轻量翻译；两者组成可交付 MVP，且不宣称同声传译。
- 可观察的成功结果：
  1. 选定格式的本地文件可被转写为可读文本并出现在历史（或等价结果面）。
  2. 开启翻译且 API 可用时，用户能看到目标语言译文；失败时回退并可见提示。
  3. 既有热键听写（麦克风/系统声）主路径行为不被破坏。

### 边界与非目标

- 本次范围：本地音/视频文件导入；安装包捆绑解码器抽音频后转写；文件转写任务上的事后轻翻译（与润色互斥）；托盘或历史窗「导入文件」入口（非阻塞默认）。
- 非目标：真·流式同声传译；实时听写路径上的翻译；同时保留润色文与译文的双槽存储；依赖用户自行安装系统 ffmpeg；改写核心听写热键语义来迁就文件流程。
- 禁止修改路径：（待设计阶段细化）无意改动的听写主路径符号应保持行为兼容；具体禁改清单在 `design.md` 锁定。

---

## 代码事实

### 现状摘要

- 实时听写：`AudioRecorder` + 设备计划 `build_recording_plan`（麦克风/系统声/混合）→ `App` 收段 → `SpeechRecognizer.transcribe_final(np.ndarray)`（sherpa-onnx **离线整段**）。
- 连续模式：VAD 分段入 `_segment_queue`，`_pump_segment_queue` 串行转写。
- 后处理：可选 `TextPolisher.polish`；`POLISH_PROMPT` **明确保持原有语言不变**；失败则 `_on_polish_error` 回退 ASR 原文粘贴。
- 历史：`HistoryStore` SQLite，字段含 `raw_text` / `polished_text` / `source` / `trigger_mode` / `model` 等；**无**独立译文列。
- 产品文档（README）描述听写与润色，**无**文件转写或翻译。
- 代码库中**无** ffmpeg/媒体解码产品路径（定点检索未发现可用解码管线）。

### 可复用 / 需扩展 / 冲突

#### 可直接复用

- `SpeechRecognizer.transcribe_final` / `TranscribeWorker`（已吃 16 kHz float PCM）
- `App._begin_transcription` 及历史 pending → freeze → enqueue 模式
- `TextPolisher` / `PolishWorker` 的 HTTP 与线程取消骨架
- `HistoryStore` 写入与历史窗口展示（若译文复用 `polished_text` 语义）

#### 需要扩展

- 新媒体导入 UI + 文件→PCM 解码
- 翻译 prompt / 设置（目标语言、开关）及与润色的产品关系
- 文件来源在 `source` / `trigger_mode` 上的标注约定
- （条件）历史 schema 或 UI，若需同时保留润色与译文

#### 需求与现状冲突

- 「轻翻译」与 `POLISH_PROMPT`「保持原有语言不变」直接冲突 → 必须新增翻译专用 prompt/模式，不能静默改润色语义。
- 「音视频文件」与「仅实时采集」冲突 → 必须新增旁路入口，不宜劫持热键听写。

### 挂载点候选

| 优先级 | 路径/符号 | 理由 |
|---|---|---|
| 必选 | `sayink/speech_recognizer.py` → `SpeechRecognizer.transcribe_final` | 文件 PCM 进入现有 ASR 的唯一稳定入口 |
| 必选 | `sayink/app.py` → `_begin_transcription` / `_on_final_result` / `_output_text` | 编排、润色/翻译分支、历史冻结 |
| 必选 | `sayink/text_polisher.py` → `TextPolisher` / `POLISH_PROMPT` | 轻翻译挂载点；需与润色语义分离 |
| 备选 | `sayink/history_store.py` → `SegmentRecord` / DDL | 仅当译文不能复用 `polished_text` |
| 备选 | `sayink/ui/history_window.py` / tray / settings | 文件入口与结果面 |

### 波及线索

- 调用方：`App` 新增文件任务状态机，避免与实时 `_is_transcribing` / segment queue 死锁或互相取消（需设计约定）。
- 共享识别器：文件长音频可能长时间占用 `TranscribeWorker`；与实时听写并发策略需明确。
- 持久化：历史 `source`/`trigger_mode`；可能 schema 迁移。
- 打包依赖：若选 ffmpeg，影响 `requirements`/PyInstaller/Inno 与体积。
- 测试：`tests/test_app.py`、`tests/test_text_polisher.py`、`tests/test_history_*`；需新增文件解码与翻译场景测试。

### 证据表

| 类型 | 结论 | 证据 |
|---|---|---|
| 事实 | ASR 为离线整段 `transcribe_final(full_audio)` | `sayink/speech_recognizer.py` `SpeechRecognizer.transcribe_final` |
| 事实 | 最终结果可触发润色再输出 | `App._on_final_result` → `TextPolisher.polish`（Memory `trace_path` outbound） |
| 事实 | 润色 prompt 禁止改语言 | `sayink/text_polisher.py` `POLISH_PROMPT` 规则 4 |
| 事实 | 历史仅有 `raw_text`/`polished_text`，无译文列 | `sayink/history_store.py` `_DDL` / `SegmentRecord` |
| 事实 | 无媒体文件解码管线 | Memory `search_code`（ffmpeg/mp3/mp4 等）无产品路径；`requirements.txt` 无 ffmpeg 绑定 |
| 事实 | 系统声/混合采集已存在（实时路径） | `audio_devices.build_recording_plan` / `pawp_capture` |
| 推断 | 文件转写可旁路 `AudioRecorder`，直接喂 PCM 给 recognizer | 基于 `transcribe_final` 签名；待设计确认分段策略 |
| 推断 | 译文短期可复用 `polished_text` 若与润色互斥 | 基于现有双字段模型；若需并存则升级为 schema 变更 |
| 决策 | 组合 MVP；音+视频；捆绑 ffmpeg；仅文件翻译；润色\|翻译互斥 | 开放问题清单第 1–3、5 行已决 |

---

## 消歧与闸门

### 开放问题清单

| 优先级 | 问题 | 代码事实背景 | 选项与影响（摘要） | 建议 | 状态 | 最终决策 |
|---|---|---|---|---|---|---|
| 必选 | 媒体输入范围（格式/解码） | 无解码管线；ASR 需 16 kHz PCM | A 仅 WAV/PCM；B 常见音频（需解码）；C 音+视频（ffmpeg） | C 对齐「音视频」表述，接受打包成本 | decided | C：音+视频（经解码抽音频） |
| 必选 | 翻译作用面 | 后处理挂在 `_on_final_result` | A 仅文件任务；B 文件+实时听写均可 | A 缩小实时路径风险 | decided | A：仅文件转写任务可翻译 |
| 必选 | 翻译与润色关系 | 单槽 `polished_text`；润色禁改语言 | A 互斥模式；B 可叠加；C 独立开关默认可并存 | A 免 schema、语义清晰 | decided | A：互斥（润色 \| 翻译）；译文复用 `polished_text` |
| 条件 | 历史如何存译文 | 无独立译文列 | （仅当非互斥时）新列 vs 拼接约定 | — | deferred | 不适用（互斥已决；不改 schema） |
| 条件 | ffmpeg 获取方式 | 打包/CI 会变 | （仅当需解码时）捆绑 vs 依赖系统 PATH | 捆绑更可预期 | decided | A：随安装包捆绑 ffmpeg（或等价解码器） |
| 非阻塞默认 | 文件入口 UI | 无现成导入 | 默认：托盘/历史旁「导入文件」+ 文件对话框 | 假定托盘/历史入口 | deferred | 默认：托盘或历史窗「导入文件」 |

### 风险定级与闸门建议

- 建议车道/风险：Standard / `medium`（已决：音+视频解码、翻译仅文件、互斥免 schema；若 ffmpeg **捆绑进安装包** 叠加发布面，仍保持 medium，设计须写清回滚/体积验收；若再叠实时翻译或 schema 迁移则升 `high`）
- 命中的风险特征：跨模块（UI/解码/App/ASR/后处理）；新增解码依赖与打包；后处理产品语义变更（翻译模式）；验证需补新路径
- 未命中的高风险特征：鉴权/支付；破坏性数据删除；公开协议变更；历史 schema 迁移（已因互斥规避）；翻译进入实时听写（已否决）
- 不确定点：无阻塞开放问题（ffmpeg 已决为捆绑）
- 闸门建议：阻塞开放问题已决 → 写 delta spec → 规格闸门 → `delivery-plan-tasks`
- 证据模式：图谱 + 定点源码（`evidence_mode: full`）
- 可用验证：现有 `tests/test_app.py`、`tests/test_text_polisher.py`、历史相关测试可作回归锚
- 缺失验证：文件解码、长文件取消、翻译 prompt/回退、与实时队列并发的专门测试（规划阶段补）

### 状态源与工件位置

- 后端：OpenSpec change
- 路径：`openspec/changes/av-transcribe-and-light-translate/`
- 能力快照：`memory: ok` / `openspec: initialized` / `superpowers: loaded`
- 闸门记录：规格批准状态 = **已批准**；批准人/时间 = 用户 / 2026-07-13；附加约束 = 音+视频+捆绑 ffmpeg；翻译仅文件；润色|翻译互斥；非真同传；方案 A
- 设计/任务：`design.md` + `tasks.md` 已完成；`openspec validate` 通过；就绪审查阻塞项 = 0
- 实现闸门：状态 = **已批准开始实施**；批准人/时间 = 用户 / 2026-07-13
- 实施进度：tasks 1.1–4.2 已勾选；验证见 `verification.md`
- 归档：准备归档；delta 规格已同步至 `openspec/specs/`；手册冒烟仍为发布前检查项


---

## 探索交接（只读摘要）

- 已选方向：组合 MVP（文件转写 + 轻翻译）；非真同传
- 来源：`delivery-explore`；`state_source` 此前为 none，本 change 为唯一状态源

# 去掉音视频文件转写：需求与代码事实简报

## Why

产品决定撤回「本地音/视频导入转写」能力，避免继续维护解码捆绑、文件任务编排与相关 UI/规格。当前主规格 `media-file-transcription` 与实现（托盘导入、`media_decoder`、App 文件任务、打包 ffmpeg）已存在，需以可验收方式从产品面移除，并明确与「轻翻译」及历史记录的边界。

## What Changes

- **BREAKING（产品能力）**：移除用户可见的「导入文件转写」入口与整条文件转写任务路径
- 移除或停用媒体解码模块与安装包 ffmpeg 捆绑（以开放问题最终决策为准）
- 主规格：`media-file-transcription` 需求撤回（delta 为 REMOVED）
- 视决策：同步撤回 `light-translation`（当前规格仅绑定文件转写任务）或保留润色-only 后处理
- 清理相关测试与错误提示；**不**改写实时热键听写主路径语义

## Capabilities

### New Capabilities

（无）

### Modified Capabilities

- `media-file-transcription`: **移除**全部文件导入/解码/文件任务转写需求；增量规格同时写明撤回后的可观察禁令与历史遗产展示
- `light-translation`: **移除**轻翻译能力；配置 `translate` 静默回落 `polish`；设置回到润色-only

## Impact

- 代码：`sayink/app.py` 文件任务编排；`sayink/media_decoder.py`；`sayink/ui/tray_icon.py` 导入菜单；`sayink/ui/history_window.py`「导入文件」元数据展示；`sayink/ui/settings_window.py` / `sayink/text_polisher.py` 翻译模式（条件）；`build.py` + `third_party/ffmpeg/`
- 配置：`llm.mode` / `llm.target_language`（条件）
- 规格：`openspec/specs/media-file-transcription/`；条件 `openspec/specs/light-translation/`
- 测试：`tests/test_media_decoder*.py`、`tests/test_app_file_transcription.py`、相关 polisher/settings 断言
- 验证：托盘无导入入口；实时听写回归；打包无强制 ffmpeg（若决移除捆绑）

---

## 意图

### 目标与成功标准

- 目标：产品不再提供「本地音/视频文件 → 转文字」能力；用户界面与主路径行为回到以实时听写（及既有润色）为中心。
- 可观察的成功结果：
  1. 托盘（及任何历史入口）不再出现「导入文件转写」或等价启动文件任务的入口。
  2. 选择本地媒体文件无法再启动本应用的文件转写任务（无隐藏快捷路径依赖该能力）。
  3. 实时热键听写主路径行为不被破坏（录音 → ASR → 可选润色 → 粘贴/历史）。
  4. 设置不再提供「翻译」模式；若配置中仍为 `llm.mode=translate`，读取时静默回落为 `polish`。
  5. 已有 `source=file` 历史记录仍可查看，并可显示「导入文件」类只读来源信息。
  6. 安装包构建不再捆绑/依赖 `third_party/ffmpeg` 以支持文件转写。

### 边界与非目标

- 本次范围：撤回文件导入转写产品面与对应规格；一并移除轻翻译模式；从打包与仓库挂钩移除 ffmpeg；清理为上述能力服务的模块/测试；保留已有 `source=file` 历史只读展示。
- 非目标：重做实时听写；新增多模态 ASR；删除或迁移历史库中的文件来源记录；引入替代第三方转写服务。
- 禁止修改路径：无意改动的热键听写互斥以外的录音/ASR 核心语义；与本次无关的 settings-control-alignment 视觉约定。

---

## 代码事实

### 现状摘要

- 主规格已存在：`openspec/specs/media-file-transcription/spec.md`（导入、捆绑 ffmpeg、历史 source、取消/互斥、失败行为）；`openspec/specs/light-translation/spec.md`（**仅**文件转写任务可翻译，与润色互斥）。
- 归档变更 `openspec/changes/archive/2026-07-14-av-transcribe-and-light-translate/` 已实现并验证过该组合 MVP。
- 运行时：托盘 `import_file_requested` → `App._on_import_file_requested` → `start_file_transcription` → `_FileDecodeWorker` / `decode_media_to_pcm` → `_begin_transcription`；`HISTORY_SOURCE_FILE="file"`、`TRIGGER_MODE_FILE_IMPORT`；结果可跳过粘贴写入历史。
- 互斥：文件任务进行中会阻断实时录音入口并提示；退出时可 `cancel_file_transcription`。
- 翻译：`llm.mode=translate` + `TextPolisher` `LLM_MODE_TRANSLATE`；设置页文案标明「仅文件转写」；测试断言实时听写忽略 translate。
- 打包：`build.py` 将 `third_party/ffmpeg` 拷入 dist；`media_decoder.resolve_ffmpeg_executable` 解析捆绑/PATH/`SAYINK_FFMPEG`。

### 可复用 / 需扩展 / 冲突

#### 可直接复用

- 实时听写编排（`AudioRecorder` → `SpeechRecognizer.transcribe_final` → 历史/粘贴）保持为回归基线
- 润色路径（`llm.mode=polish`）在去掉翻译时仍可保留

#### 需要扩展

- 从 App/托盘/历史/设置剥离文件任务与（条件）翻译 UI
- 主规格 REMOVED delta；条件 light-translation REMOVED/改写
- 测试从「证明文件能力存在」改为「证明入口与任务不存在 / 听写不回归」
- 打包去掉 ffmpeg 挂钩（若决移除）

#### 需求与现状冲突

- 现行主规格 **要求** 提供文件导入与捆绑解码；本变更目标是 **撤回** 这些 SHALL → 必须以 REMOVED/替换规格显式覆盖，不能只改代码不改规格。

### 挂载点候选

| 优先级 | 路径/符号 | 理由 |
|---|---|---|
| 必选 | `sayink/app.py`：`start_file_transcription` / `_FileDecodeWorker` / `HISTORY_SOURCE_FILE` | 文件任务编排中枢 |
| 必选 | `sayink/ui/tray_icon.py`：`import_file_requested` / 「导入文件转写…」 | 用户可见入口 |
| 必选 | `openspec/specs/media-file-transcription/spec.md` | 主规格必须撤回 |
| 备选 | `sayink/media_decoder.py`；`build.py` `_copy_ffmpeg_into_dist` | 解码与打包 |
| 备选 | `sayink/ui/settings_window.py` 润色/翻译；`sayink/text_polisher.py` `LLM_MODE_TRANSLATE` | 轻翻译绑定文件任务 |
| 备选 | `sayink/ui/history_window.py`「触发：导入文件」 | 历史元数据展示 |

### 波及线索

- 调用方：托盘信号、App 内录音/听写互斥分支、历史 source 展示、设置 llm.mode
- 共享状态：`_file_job_active` 与实时听写互斥；去掉后应简化互斥分支
- 持久化：已有 `source=file` / `trigger_mode=file_import` 历史行（是否清理为开放问题；默认倾向保留只读展示）
- 打包依赖：ffmpeg 捆绑与体积/CI
- 测试：`tests/test_app_file_transcription.py`、`tests/test_media_decoder*.py`、polisher/settings 相关

### 证据表

| 类型 | 结论 | 证据 |
|---|---|---|
| 事实 | 文件导入入口在托盘 | `tray_icon.py` `import_file_requested`；菜单「导入文件转写…」 |
| 事实 | App 有完整文件任务 API | Memory `search_graph`：`App.start_file_transcription`（约 L866+）、`cancel_file_transcription`；`_on_import_file_requested` |
| 事实 | 解码经 `media_decoder.decode_media_to_pcm` | `sayink/media_decoder.py`；`tests/test_media_decoder.py` |
| 事实 | 打包复制 ffmpeg | `build.py` `_copy_ffmpeg_into_dist` / 步骤 `[3/4] Copying bundled ffmpeg` |
| 事实 | 轻翻译规格仅绑定文件任务 | `openspec/specs/light-translation/spec.md` Purpose + Requirement |
| 事实 | 历史可标「导入文件」 | `history_window.py` meta「触发：导入文件」 |
| 事实 | 无活跃 OpenSpec change（本变更新建前） | `openspec list` → No active changes；归档含 av-transcribe |
| 推断 | （已决）轻翻译随文件能力一并移除 | 开放问题 1A |
| 决策 | 去掉翻译；保留文件历史展示；移除 ffmpeg 捆绑；translate 配置静默回落 polish | 开放问题 1–4 已决 A（用户 2026-07-15） |

---

## 消歧与闸门

### 开放问题清单

| 优先级 | 问题 | 代码事实背景 | 选项与影响（摘要） | 建议 | 状态 | 最终决策 |
|---|---|---|---|---|---|---|---|
| 必选 | 是否同步去掉轻翻译（`llm.mode=translate`）？ | `light-translation` 仅作用于文件转写；设置页与 polisher 已实现 | A 一并移除翻译模式，设置回到润色-only；B 暂留 UI/配置但无作用面（易误导）；C 其他 | A | decided | A：一并移除翻译模式，设置回到润色-only（用户 2026-07-15） |
| 必选 | 已有 `source=file` 历史记录如何处理？ | 历史已可写入文件来源元数据 | A 保留记录与「导入文件」展示（只读遗产）；B 保留记录但去掉专用展示文案；C 删除此类历史行（破坏性） | A | decided | A：保留记录与「导入文件」只读展示（用户 2026-07-15） |
| 必选 | 安装包 ffmpeg 捆绑是否物理移除？ | `build.py` + `third_party/ffmpeg` 专为文件解码 | A 从打包与仓库挂钩中移除；B 暂留二进制/拷贝逻辑但不暴露产品入口 | A | decided | A：从打包与仓库挂钩中移除（用户 2026-07-15） |
| 条件 | 若去掉翻译：已保存 `llm.mode=translate` 的配置如何处理？ | `config.py` 默认 `mode=polish`，用户可能已选 translate；第 1 题已决 A | A 读取时静默回落 `polish`；B 保留键但 UI 不可选（死键）；C 其他 | A | decided | A：读取时静默回落为 `polish`（用户 2026-07-15） |
| 非阻塞 | 是否同时改 README/对外文案 | 产品文档可能已提及文件转写 | 默认：若有提及则删掉相关句；无则不动 | 有则删 | deferred | 有提及则删除相关描述 |

### 风险定级与闸门建议

- 建议车道/风险：Standard / `medium`
- 命中的风险特征：跨模块（App/UI/解码/打包/规格）；共享编排状态与听写互斥；发布物依赖（ffmpeg）变更；主规格撤回
- 未命中的高风险特征：鉴权/支付/权限；隐私导出；公共外部 API 协议；破坏性清库（已决保留 `source=file` 历史）
- 不确定点：无阻塞开放问题（1–4 已决）
- 闸门建议：增量规格已齐 → 规格闸门单题放行 → `delivery-plan-tasks`
- 证据模式：full
- 可用验证：pytest 文件/解码相关改写或删除后的回归集；手动确认托盘无导入；热键听写冒烟
- 缺失验证：完整安装包在无系统 ffmpeg 机器上的体积/行为对比（设计阶段补任务）

### Explore 交接消费

N/A — 无 explore handoff

### 状态源与工件位置

- 后端：OpenSpec change
- 路径：`openspec/changes/remove-media-file-transcription/`
- 已确认工件：`proposal.md`；`design.md`；`specs/*`；`tasks.md`（全部完成）；`verification.md`
- 闸门记录：规格批准 = 已批准（用户 / 2026-07-15）；实现批准 = 已批准（用户 / 2026-07-15）；overall_status = **verified**；archive = deferred_to_openspec；附加约束 = 1–4 全 A / 方案 A

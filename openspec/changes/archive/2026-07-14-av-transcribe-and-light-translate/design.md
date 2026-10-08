# 音视频文件转写 + 轻翻译：技术实施计划

## Context

SayInk 已具备实时听写（麦克风/系统声）→ 离线 ASR（`SpeechRecognizer.transcribe_final`）→ 可选润色（`TextPolisher`）→ 粘贴/历史。规格已批准：捆绑 ffmpeg 的音/视频文件转写 + **仅文件任务**的事后轻翻译（与润色互斥），非同声传译。用户选定实现方案 **A**：旁路文件任务 + 整段转写 + Polisher 翻译模式。

## Goals / Non-Goals

**Goals:**

- 托盘/历史「导入文件」→ 解码抽音频 → 整段 ASR → 历史可查；`source` 区分文件来源
- 文件任务可选翻译（互斥于润色）；失败回退原文并提示
- 安装包/PyInstaller 产物捆绑 ffmpeg（或等价），无系统 PATH 亦可解码
- 文件任务与实时听写硬互斥（占用中拒绝并提示）
- 不破坏热键听写主路径

**Non-Goals:**

- 真·流式同传；实时听写翻译；历史 schema 新列；文件切片串行（方案 B）；独立 FileTranscribeService（方案 C）

## 已批准目标与约束

- 目标/非目标：见上；规格见 `specs/media-file-transcription`、`specs/light-translation`
- 风险/闸门：Standard / `medium`；规格已于 2026-07-13 用户批准
- 方案决策：A（本文件）

## 已刷新代码事实

| 结论 | 证据 | 新鲜度 |
|---|---|---|
| ASR 入口 `transcribe_final(np.ndarray)` | `sayink/speech_recognizer.py` | HEAD `bb8db26` |
| 历史 `source` 现为 mic/system/mixed | `App._build_pending_history_record` | 同上 |
| 润色 `llm.*` + `POLISH_PROMPT` 禁改语言 | `config.DEFAULT_CONFIG` / `text_polisher.py` | 同上 |
| 托盘已有设置/历史入口 | `tray_icon._setup_menu` | 同上 |
| PyInstaller 在 `build.py` 组装 args；Inno 拷贝 `dist/SayInk/_internal` | `build.py` / `installer/SayInk-Setup.iss` | 同上 |
| 无并行活跃 change 冲突 | `openspec/changes` 仅本 change | 同上 |

## 方案比较

### 方案 A（已选）

- 形态：旁路文件任务 + 整段 PCM 转写 + Polisher 翻译模式；硬互斥实时听写；捆绑 ffmpeg
- 收益：挂载清晰、改动可控
- 成本/风险：超长文件内存；打包体积
- 可逆性：高（关入口/开关）
- 验证方式：单测 mock 解码/ASR；打包冒烟检查二进制存在

### 方案 B / C

- 未选：切片队列纠缠更大；独立服务 MVP 过重（见 framing 方案比较）

## Decisions

1. **解码**：新增 `sayink/media_decoder.py`，通过 subprocess 调用捆绑 `ffmpeg`，输出 mono float32 16 kHz PCM（`numpy` 数组）。解析可执行路径：开发态可用环境变量/`third_party/ffmpeg`；冻结态优先 `_MEIPASS` / 安装目录旁 `ffmpeg`（与 models 布局同类）。
2. **文件任务状态**：在 `App` 增加 `_file_job_*` 状态（idle/decoding/transcribing/postprocess/cancelling）。进行中拒绝热键开始录音，并提示；实时录音/转写中拒绝新文件任务。
3. **历史元数据**：`source="file"`；`trigger_mode="file_import"`（或等价常量）；不改 DDL。
4. **后处理配置**：扩展 `llm`（或并列 `postprocess`）为模式 `polish` | `translate`（互斥）；翻译目标语言配置键；仅当 `_file_job_active` 且 mode=translate 时走翻译 prompt。实时路径仅在 mode=polish 且 enabled 时润色（保持现状）。
5. **翻译实现**：`TextPolisher` 增加 `TRANSLATE_PROMPT` 模板（注入目标语言）或 `polish(..., mode=...)`；复用 `PolishWorker` HTTP 骨架；失败仍走现有 `_on_polish_error` 降级语义（文件任务文案区分「翻译未成功」）。
6. **UI**：托盘菜单「导入文件」信号；历史窗同入口可选。`QFileDialog` 过滤常见音视频。浮窗复用 recognizing/polishing 状态，必要时增加「文件转写中/可取消」。
7. **打包**：`build.py` 在构建后（或 `--add-binary`）将平台 ffmpeg 打入 `dist/SayInk/`（如 `_internal/ffmpeg/` 或根目录）；文档注明二进制来源与许可证；开发者需提供/下载 ffmpeg 构建输入（任务内脚本或 README 步骤）。Inno 随 `_internal` 递归已覆盖则无需改 iss；若放在 app 根需补 `[Files]`。
8. **并发取消**：取消文件任务时 `cancel` 解码子进程 + `SpeechRecognizer` 现有 supersede/cancel + polisher.cancel；不 `QThread.terminate` ASR（遵守现有 SD-05 约束）。

## 最终决策

- 选定方案：A
- 选择理由：对齐规格与现有 ASR/Polisher 挂载；交付最快
- 未选：B/C 见上

## 集成方式与数据流/控制流

```text
[托盘/历史 导入文件]
        │
        ▼
 App.start_file_job(path)
   ├─ 若实时忙 → 提示拒绝
   ├─ media_decoder.decode_to_pcm(path)  // 可取消
   ├─ _begin_transcription(pcm) 变体（标记 file job；source=file）
   ├─ ASR final_result
   ├─ 若 file+translate → TextPolisher(mode=translate)
   │   若 file+polish → TextPolisher(mode=polish)
   │   否则直接输出
   └─ 写历史 / 展示结果
[实时热键] ──文件忙──▶ 提示拒绝
```

**文件任务输出策略（默认假设，非阻塞）：** 成功后以历史 + 浮窗/结果提示为主；**默认不自动粘贴**到任意前台窗口（与听写差异）。若实现中发现需粘贴，须保持可关且不改变听写默认。

## 接口与状态模型

- `resolve_ffmpeg_executable() -> Path`
- `decode_media_to_pcm(path: Path, *, cancel_event) -> np.ndarray`；失败抛可分类错误（missing_ffmpeg / decode_error / no_audio）
- `App.start_file_transcription(path: Path)` / `cancel_file_transcription()`
- Config：`llm.mode`: `"polish" | "translate"`；`llm.target_language`: str（如 `en` / `zh`）；`llm.enabled` 对两种模式均表示「启用后处理」
- History：`source="file"`，`trigger_mode="file_import"`

## 失败处理与可观测性

- 缺失 ffmpeg / 解码失败 / 无音轨 / ASR 空结果 / 翻译失败：浮窗或对话框短文案 + log
- 日志：`SayInk` logger，含 job id、文件 basename、阶段耗时
- 进度：至少阶段文案（解码中/识别中/翻译中）；精确百分比可选（非阻塞）

## 兼容、迁移与回滚

- **兼容**：不改历史 DDL；旧配置缺 `llm.mode` 时默认 `polish`
- **迁移**：无 DB 迁移
- **回滚**：移除托盘入口或配置关闭；卸载带回退版本；开发态不捆绑则文件功能自检失败并提示

## 安全与性能

- 安全：翻译/润色仍走既有 HTTPS/本地 URL 策略（`is_secure_or_local_url`）；文件路径仅本地用户选择；ffmpeg 参数防注入（列表 argv，禁止 shell=True）
- 性能：整段转写受内存限制；可在 UI 对超大文件警告（建议项）
- 隐私：译文请求将文本发往用户配置的 LLM（与润色相同）；文件内容不上传解码服务

## 验证策略

- 单测：decoder（mock subprocess）、polisher translate prompt、App 文件任务互斥与历史 source、配置默认
- 回归：现有 `tests/test_app.py`、`tests/test_text_polisher.py`、`tests/test_readme_features.py`
- 手动/打包：安装包内 ffmpeg 存在；选一短 mp4/mp3 转写成功；翻译开关与润色互斥

## 需求追溯

| 需求/场景 | 设计要素 | 任务 | 验证 |
|---|---|---|---|
| 导入文件转写 | `start_file_transcription` + UI | 2.1–2.2 | pytest + 手动 |
| 捆绑解码 | `media_decoder` + `build.py` | 1.1–1.2 | 单测 + 构建产物检查 |
| 历史可查/来源可区分 | `source=file` | 2.1、4.1 | pytest |
| 取消与互斥 | file job 状态机 | 2.1–2.2 | pytest |
| 文件可翻译 / 实时不翻译 | mode 门控 | 3.1–3.2 | pytest |
| 润色\|翻译互斥 | `llm.mode` | 3.1–3.2 | pytest |
| 翻译失败回退 | 既有 polish_error 路径 | 3.2 | pytest |
| 非同声传译文案 | 设置/结果文案 | 3.2 | 人工检查文案 |

## Risks / Trade-offs

- [超长媒体 OOM/耗时] → 阶段提示；后续可加切片（非本变更）
- [ffmpeg 体积/许可] → 构建文档写明二进制来源与许可；CI 需缓存
- [文件与实时互斥体验硬] → 明确提示「文件转写进行中」
- [默认不粘贴与听写不一致] → 文案说明结果在历史中

## Migration Plan

1. 开发实现与单测
2. 准备 ffmpeg 构建输入并更新 `build.py`
3. 本地 `build.py` 冒烟
4. 回滚：发布前一版本安装包；配置默认 polish 不影响旧用户

## Open Questions

- 无阻塞产品题。非阻塞默认：文件任务成功后不自动粘贴；超大文件仅警告不拦截。

## 已知风险与非目标

见 Goals/Non-Goals 与 Risks。

## 实现就绪审查（摘要）

- 结论：就绪（阻塞项 = 0）
- 警告项：超长文件内存（已接受为 medium 已知限制）；打包需准备 ffmpeg 构建输入
- 建议项：超大文件时长警告
- 后端结构验证：待 `openspec validate` 确认

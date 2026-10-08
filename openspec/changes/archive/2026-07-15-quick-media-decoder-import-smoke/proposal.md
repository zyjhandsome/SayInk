# Quick 契约：media_decoder 公开 API 导入烟雾测试

## Why

delivery 族需在 SayInk 上跑通一次 **Quick → execute → verification.md**，并验证 **Explore 交接消费（C5）** 勾选块落在 `proposal.md`。选一处已入图、边界清晰、无红线的锚点：`sayink/media_decoder.py` 公开 API 可被测试导入。

## What Changes

- 新增轻量单测：导入并断言 `resolve_ffmpeg_executable` / `decode_media_to_pcm` 与错误类型可访问
- 不改解码行为、不改 App 编排、不加 delta 规格 / design

## 轻量契约

目标：为 `media_decoder` 公开 API 增加可重复的导入烟雾测试，作为 delivery Quick 路径活体验证。

非目标：改 ffmpeg 解析逻辑；改文件转写/翻译产品行为；新增 OpenSpec delta 规格；真媒体 E2E。

影响文件/符号：
- 新建 `tests/test_media_decoder_import_smoke.py`
- 只读引用：`sayink.media_decoder`（`resolve_ffmpeg_executable`、`decode_media_to_pcm`、`MissingFFmpegError`、`DecodeError`、`NoAudioError`、`CancelledError`）

可观察行为：`pytest tests/test_media_decoder_import_smoke.py -q` 全绿；导入失败则红。

最小验证：`python -m pytest tests/test_media_decoder_import_smoke.py -q`

禁止范围：`sayink/app.py`、`sayink/media_decoder.py` 行为修改；`speech_recognizer.py`；历史 DDL；打包脚本。

风险/未知项：Low / Quick；无红线；工作区已有未提交媒体转写改动，本变更仅新增测试文件，避免碰撞。

### Explore 交接消费（C5）

- [x] `chosen_direction` → 已写入目标（或用户修订）
- [x] `non_goals` → 已写入非目标 / N/A
- [x] `code_anchors` → 已检查并引用路径或符号
- [x] `risk_signal` → 仅线索；风险已按代码事实重算
- [x] `unknowns` → 已写入开放问题或非阻塞丢弃

落点摘要：意图=media_decoder 导入烟雾；挂载=`sayink/media_decoder.py`；Risk=Quick/low（hit 无红线）；开放问题=无阻塞。

实施批准：已批准（批准人=用户「完整的跑一次测试验证」/ 2026-07-15）

## 状态源与工件位置

- 后端：OpenSpec change
- 路径：`openspec/changes/quick-media-decoder-import-smoke/`
- 车道/风险：Quick / low
- 能力快照：`memory: ok` / `openspec: initialized` / `superpowers: loaded`
- 证据模式：`full`
- 下一技能：`delivery-execute-verify`（跳过 plan）

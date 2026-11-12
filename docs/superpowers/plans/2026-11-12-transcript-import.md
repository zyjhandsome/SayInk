# 导入文字稿 Implementation Plan

> **For agentic workers:** 场景 1 已完成。场景 3、场景 2 已按后续验收继续实现，当前都待用户验收。不要提交 git，除非用户明确要求。

**Goal:** 会议页可以粘贴或打开已有文字稿，生成一场已结束的会议；保留认得出的说话人与时间，纯文本不伪造「说话人待确认」和 `00:00:00`；导入后只打开会议，不自动生成纪要。

**Architecture:** 纯函数解析器 `sayink/transcript_import.py` 不碰 Qt 和数据库。`MeetingStore.create_meeting_from_transcript` 在同一次写入里创建 `source=import_text` 的已结束会议、文字稿行和真实姓名。纪要请求用显式的 `speakers_distinguished` / `timestamps_available`（默认都为真，现场会议即使全是说话人 0 也保持原提示词和指纹）。界面只加一个次按钮和一个对话框。

**Tech Stack:** Python 3.10、PyQt6、SQLite（`HistoryStore` 单写线程）、pytest（`QT_QPA_PLATFORM=offscreen`）。

**产品决定（已锁定，来自评估方案）：**

- 粘贴，以及 UTF-8 / GB18030 的 `.txt` `.md` `.srt` `.vtt`。不做 `.docx`。
- 自动识别 SRT、WebVTT、腾讯/飞书/通义这类「姓名 + 时间」分块、行内对话；识别不到就按纯文本分段。
- 导入后打开该场会议，用户自己点「生成纪要」。不跟随 `meeting.auto_generate`。
- 无时间戳：`ended_at` 为空，不显示时长，不能跳转，不能导出 SRT。
- 无说话人：不把每段渲染成「说话人待确认」，纪要提示词不要求把这些段当成不同的声纹人。
- 不改 `sayink/ui/main_window.py` 和 `tests/test_main_window.py`（工作区里已有未提交修改）。
- 不改 `app.py`、托盘、不恢复文件转写，不打包 ffmpeg。
- 新行为写入 README「会议」和测试。不新开 OpenSpec。`docs/meeting-notes-design.md` 仍是旧快照，本轮不改。

## 文件

| 文件 | 职责 |
|---|---|
| `sayink/transcript_import.py` | 解析、编码、说话人编号、预览文案 |
| `sayink/meeting_store.py` | `create_meeting_from_transcript` |
| `sayink/minutes_prompts.py` | 默认提示词字节级不变；缺说话人或时间时换规则 |
| `sayink/minutes_generator.py` | 请求带两个标志；分块、渲染、指纹使用它们 |
| `sayink/minutes_render.py` | 无时间戳时清掉模型填的时钟 |
| `sayink/meeting_session.py` | `import_transcript`、文字稿形状、复制/SRT |
| `sayink/ui/transcript_import_dialog.py` | 粘贴、打开文件、预览 |
| `sayink/ui/meeting_page.py` | 「导入文字稿」按钮、跳转和导出降级 |
| `sayink/ui/meeting_views.py` | 文字稿 HTML、列表状态「导入文字稿」 |
| `sayink/transcript_refiner.py` | 无说话人时精整输入不写「说话人待确认」 |
| `tests/test_transcript_import.py` | 解析、存储、纪要契约、对话框、页面 |
| `README.md` | 会议节新增「导入文字稿」 |

## 场景 1 任务

- [x] 解析器：SRT / VTT / 分块时间 / 行内时间 / 对话 / 纯文本 / 停用词 / GB18030 / BOM
- [x] 存储：同一次写入会议、分段、真实姓名；无时间戳则 `ended_at` 为空且 `captured_at = started_at`；清空历史删不掉
- [x] 纪要：默认指纹与提示词不变；纯文本请求不带伪造说话人和时钟；模型返回的 `00:00:00` 不进正文
- [x] 现场会议全是说话人 0 时，两个标志仍为真
- [x] 页面：导入后打开会议、不入队、列表写「导入文字稿」、无时间不能跳转也不能选 SRT
- [x] README 与 `tests/test_readme_features.py` 同步

验证：

```powershell
$env:QT_QPA_PLATFORM='offscreen'
py -3.10 -m pytest tests/test_transcript_import.py tests/test_minutes.py tests/test_meeting_store.py tests/test_meeting_ux.py tests/test_meeting_session.py tests/test_readme_features.py tests/test_app_file_transcription.py -q --tb=short
py -3.10 -m ruff check sayink/transcript_import.py sayink/ui/transcript_import_dialog.py sayink/meeting_store.py sayink/minutes_prompts.py sayink/minutes_generator.py sayink/minutes_render.py sayink/meeting_session.py sayink/ui/meeting_page.py sayink/ui/meeting_views.py sayink/transcript_refiner.py tests/test_transcript_import.py tests/test_readme_features.py
```

## 验收后再做

**场景 3（会议列表多选）已实现，待验收：** 列表「选择」多选已加载的会议。「全选已加载」不包含还没加载的更早会议。删除是一次写入、一次纪要队列推进，确认后硬删除，无法撤销，不把 `status` 改成 `deleted`；进行中的会议不删。导出写进一个 Markdown。生成纪要只排队还没有纪要的会议，远端地址整批只确认一次。本轮不合并会议，也不做批量复制。

**场景 2（导入音视频）已接到会议页，待验收：** 会议页「导入音视频」读取本机 wav/mp3/m4a/aac/mp4/mov/mkv/wma/flac，不复制原文件，声纹不落盘。取消后已识别的部分保留为已中断的会议。导入期间不能开始转写、会议或录音恢复。应用和托盘仍然没有「导入文件转写」。导入后不自动生成纪要。真实样本 `0e892da1-….mp4` 已解码并跑通会议识别链路。

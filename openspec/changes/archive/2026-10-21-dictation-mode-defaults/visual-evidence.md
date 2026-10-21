# 视觉证据：分模式默认与会中改字

Qt offscreen 截图，没有打开麦克风、语音模型或网络。对照规格文字，这些状态没有旧像素基线。

| ID | 图片 | 状态 | 对照结论 |
|---|---|---|---|
| V1 | `openspec/changes/dictation-mode-defaults/visual/v1-setup-pending.png` | 缺模型准备页，待下载 | 看见下载模型、测试麦克风、试说一句；没有输入框，也没有密钥或润色配置。 |
| V2 | `openspec/changes/dictation-mode-defaults/visual/v2-mic-unheard.png` | 准备页，麦克风未听到 | 说明没有听到声音，测试麦克风和稍后继续都可以使用。 |
| V3 | `openspec/changes/dictation-mode-defaults/visual/v3-listening-target.png` | 持续转写浮条，目标进程名 | 状态是 notepad.exe，没有窗口标题。 |
| V4 | `openspec/changes/dictation-mode-defaults/visual/v4-history-conflict.png` | 开录前选择 | 开启历史、改为输入光标、取消都在，默认是开启历史。 |
| V5 | `openspec/changes/dictation-mode-defaults/visual/v5-live-edit.png` | 会议进行中，悬停已落段，默认宽度 | 已落段文字旁出现编辑，原句仍在。 |
| V5 窄 | `openspec/changes/dictation-mode-defaults/visual/v5-live-edit-narrow.png` | 同一场会议，窄宽度 | 窄宽度下编辑入口还在，正文没有被拆掉。 |
| V6 | `openspec/changes/dictation-mode-defaults/visual/v6-ended-without-notes.png` | 已结束且没有纪要 | 提示可以编辑，不再要求先生成纪要。 |

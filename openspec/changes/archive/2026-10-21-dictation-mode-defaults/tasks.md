# 口述模式默认体验：实施任务清单

## 执行规则

- 权威状态源：`openspec/changes/dictation-mode-defaults/`
- 风险/闸门：High。规格已批准。没有实现批准之前不得修改 `sayink/` 或 `tests/`。
- 禁止范围：不捆绑 Fun-ASR-Nano；不把会议文字粘贴到其他应用；不改会议开始、暂停、结束和 Esc；不改默认音源、快捷键和默认关闭润色；不在准备页配置 AI；不记录窗口标题；不改凭据管理器。
- 必须执行的最终验证：本文件「最终验证」表中的 pytest、Ruff 和截图脚本。

## 任务

- [x] 任务 1：留下会改动前的会议文字稿对照图
  - 对应需求/场景：视觉验收 V5、V6 的基线替代
  - 前置依赖：无
  - 目标文件/符号：`docs/ux-preview/meeting/hover-transcript-light.png`、`docs/ux-preview/meeting/transcript-light.png`
  - 允许修改：`openspec/changes/dictation-mode-defaults/visual-baseline/`
  - 禁止修改：`sayink/`、`tests/`、`docs/ux-preview/`
  - 实施步骤：把上述两张已提交的预览复制到 `visual-baseline/`，并写一张同目录说明，标明 V1–V4 没有旧像素基线。
  - 失败测试或已批准替代验证：复制后的文件必须存在且非空。
  - 验证命令/动作：`py -3.10 -c "from pathlib import Path; root=Path('openspec/changes/dictation-mode-defaults/visual-baseline'); need=['hover-transcript-light.png','transcript-light.png','README.md']; missing=[n for n in need if not (root/n).is_file() or (root/n).stat().st_size==0]; raise SystemExit(missing or 0)"`
  - 预期结果：命令退出码 0
  - 质量层/证据：visual；基线是现有会议预览；路线是会后文字稿；状态是悬停与普通阅读；Qt offscreen 的已有图，不是浏览器；差异政策是只作对照不覆盖原图；证据在 `visual-baseline/`
  - 迁移/回滚：删除 `visual-baseline/` 即可
  - 完成定义：两张对照图和说明都在，产品代码未改
  - 负责人/冲突说明：可与任务 2 并行；不碰产品代码

- [x] 任务 2：单次和持续各自记住结果去向，旧配置只迁移一次
  - 对应需求/场景：Single and continuous dictation remember output separately；Existing shared output choice migrates once
  - 前置依赖：无
  - 目标文件/符号：`sayink/config.py` `Config._load`、`Config.output_mode`、`Config.set_output_mode`、`Config.follow_input_source_for_output`；`sayink/ui/settings_window.py` `SettingsWindow._on_output_mode_changed`、`SettingsWindow._refresh_output_controls`；`sayink/ui/dictation_hints.py` `dictation_hints`
  - 允许修改：上述文件，以及 `tests/test_config_audio.py`、`tests/test_settings_general.py`、`tests/test_about_ux.py`、`tests/test_config.py`
  - 禁止修改：`sayink/app.py`、`sayink/text_paster.py`、`sayink/meeting_store.py`、安装包脚本
  - 实施步骤：先写失败测试，覆盖全新安装两份都是自动、持续的明确「仅保存历史」不改变单次、音源联动只改当前模式、旧配置迁移一次且第二次加载不重置。再实现 `output.by_mode`，并把旧字段写成当前模式的镜子。
  - 失败测试或已批准替代验证：`tests/test_config_audio.py` 中迁移与分模式用例在实现前失败。
  - 验证命令/动作：`py -3.10 -m pytest tests/test_config_audio.py tests/test_settings_general.py tests/test_about_ux.py tests/test_config.py -q --tb=line`
  - 预期结果：退出码 0
  - 质量层/证据：functional、compatibility
  - 迁移/回滚：见 design.md Migration Plan。不要删除用户已有的 `output.mode`。
  - 完成定义：切换录制方式后组合框显示该模式自己的去向；加载两次不会重置后来的手动选择
  - 负责人/冲突说明：本任务独占 `sayink/config.py` 与设置页去向控件

- [x] 任务 3：按开口时的窗口粘贴，变了就留在剪贴板并用浮条说明
  - 对应需求/场景：Paste uses the window from the start of the utterance；A changed paste target is announced without blocking the next utterance
  - 前置依赖：任务 2
  - 目标文件/符号：`sayink/audio_recorder.py` `AudioRecorder._feed_segmenter`；`sayink/app.py` `App._on_recording_start`、`App._output_text`、`_SegmentContext`；`sayink/text_paster.py` `TextPaster.paste_async`；`sayink/ui/floating_window.py` `FloatingWindow.show_listening`
  - 允许修改：上述文件，以及 `tests/test_text_paster.py`、`tests/test_app.py`、`tests/test_app_continuous_turns.py`
  - 禁止修改：`sayink/meeting_page.py`、`sayink/onboarding.py`、会议开始与 Esc
  - 实施步骤：先写失败测试：单次开口后的窗口切换不发送粘贴键；持续两段各自使用开口时的进程名；SayInk 自己的窗口不粘进后来的外部窗口；浮条提示不要求确认。再在 UI 线程开口处记录句柄和进程名，并让 `paste_async` 只跟这个句柄比较。
  - 失败测试或已批准替代验证：`tests/test_text_paster.py` 的目标变化用例在实现前失败。
  - 验证命令/动作：`py -3.10 -m pytest tests/test_text_paster.py tests/test_app.py tests/test_app_continuous_turns.py -q --tb=line`
  - 预期结果：退出码 0
  - 质量层/证据：functional。浮条外观由任务 7 截图；这里只断言副文案含进程名且不含窗口标题
  - 迁移/回滚：句柄只在内存里，回滚代码即可
  - 完成定义：识别期间换窗口不会把字粘进新窗口；下一段仍能开始
  - 负责人/冲突说明：本任务先改 `sayink/app.py`。任务 4 必须等本任务合并后再改同一文件

- [x] 任务 4：历史关闭且只会进历史时，先选择再录音
  - 对应需求/场景：History-only output with history disabled must be resolved before recording
  - 前置依赖：任务 2、任务 3
  - 目标文件/符号：`sayink/app.py` `App._on_recording_start`、`App._start_continuous_listening`、`App._output_text`
  - 允许修改：`sayink/app.py`、`tests/test_app_history_wiring.py`、`tests/test_app_continuous_turns.py`
  - 禁止修改：会议开始路径、`sayink/meeting_store.py`
  - 实施步骤：先写失败测试：该组合按下快捷键不调用录音开始；选择开启历史后才开始，且不改另一种模式；取消不录音；持续转写进行中取消会结束会话并把当前段留在剪贴板；会议开始不受影响。再在两个开始入口和输出入口接上选择。
  - 失败测试或已批准替代验证：`tests/test_app_history_wiring.py` 中开录前拦截用例在实现前失败。
  - 验证命令/动作：`py -3.10 -m pytest tests/test_app_history_wiring.py tests/test_app_continuous_turns.py -q --tb=line`
  - 预期结果：退出码 0
  - 质量层/证据：functional、accessibility。断言默认按钮是「开启历史」，取消按钮存在且有可访问名称
  - 迁移/回滚：不改配置文件格式
  - 完成定义：死路组合不能开始录音；会议仍能开始
  - 负责人/冲突说明：接管 `sayink/app.py` 的开始路径，必须在任务 3 之后

- [x] 任务 5：没有语音模型时立刻进入准备页
  - 对应需求/场景：Missing speech model opens setup immediately
  - 前置依赖：无
  - 目标文件/符号：`sayink/onboarding.py` `OnboardingController.schedule`、`OnboardingController.show_welcome`、`WELCOME_FALLBACK_MS`
  - 允许修改：`sayink/onboarding.py`、`tests/test_onboarding.py`
  - 禁止修改：`build.py`、`sayink/app.py`、模型下载器内部协议
  - 实施步骤：先写失败测试：缺模型且欢迎未看过时，准备页在 400 毫秒级出现，不注册 15 秒欢迎；试说结果不调用粘贴；麦克风无输入时可以关闭；模型已在时仍走原来的欢迎。再实现准备页，下载动作打开现有引擎页。
  - 失败测试或已批准替代验证：`tests/test_onboarding.py` 的缺模型计时用例在实现前失败。
  - 验证命令/动作：`py -3.10 -m pytest tests/test_onboarding.py -q --tb=line`
  - 预期结果：退出码 0
  - 质量层/证据：functional、accessibility、performance。准备页主按钮有可访问名称；测试证明没有挂 `WELCOME_FALLBACK_MS`
  - 迁移/回滚：不改已看过欢迎的配置
  - 完成定义：轻量安装第一眼是准备页；完整包欢迎时机不变
  - 负责人/冲突说明：只碰引导模块，可与任务 2、任务 6 并行

- [x] 任务 6：会议进行中修改已经落段的文字
  - 对应需求/场景：Finished meeting segments can be edited during a live meeting
  - 前置依赖：无
  - 目标文件/符号：`sayink/meeting_session.py` `MeetingController.save_segment_text`；`sayink/meeting_store.py` `MeetingStore.set_segment_text`；`sayink/ui/meeting_page.py` `_TranscriptView._CONTEXT_LINKS`、`MeetingPage._on_live_transcript_link`、`MeetingPage._prompt_segment_text`、`MeetingPage._sync_content_hint`
  - 允许修改：上述文件，以及 `tests/test_meeting_ux.py`
  - 禁止修改：会议开始、暂停、结束、Esc；`sayink/app.py` 的粘贴路径
  - 实施步骤：先写失败测试：live 和暂停可以保存已有段落且原文还在；新段落插入不覆盖修改；未落段没有 `live-edit`；DRAINING 和生成中保存返回 false；已结束但没有纪要时提示不要求先生成。再放开 live 保存，并接上悬停入口。
  - 失败测试或已批准替代验证：`tests/test_meeting_ux.py` 的会中正文保存用例在实现前失败。
  - 验证命令/动作：`py -3.10 -m pytest tests/test_meeting_ux.py -q --tb=line`
  - 预期结果：退出码 0
  - 质量层/证据：functional。外观由任务 7 覆盖
  - 迁移/回滚：不新增数据库列。回滚后 live 再次拒绝写入，已保存的 `corrected_text` 保留
  - 完成定义：进行中能改已出现的段落；收尾和生成中不能改；会后提示不再写「必须等生成完成」
  - 负责人/冲突说明：只碰会议文字路径，可与任务 5 并行

- [x] 任务 7：截取准备页、冲突选择、浮条目标和会中编辑
  - 对应需求/场景：视觉验收 V1–V6
  - 前置依赖：任务 3、任务 4、任务 5、任务 6
  - 目标文件/符号：`tools/capture_meeting_ui.py`、`sayink/onboarding.py` `OnboardingController`、`sayink/ui/floating_window.py` `FloatingWindow.show_listening`、`sayink/ui/meeting_page.py` `MeetingPage._render_live`
  - 允许修改：`tools/capture_mode_defaults_ui.py`、`openspec/changes/dictation-mode-defaults/visual-evidence.md`
  - 禁止修改：产品行为。截图脚本不得打开真实麦克风、模型或网络
  - 实施步骤：仿照 `tools/capture_meeting_ui.py` 的 offscreen 方式，渲染 V1–V6 并写入 `visual-evidence.md` 的路径、状态和对照结论。窄宽度至少覆盖会中编辑。
  - 失败测试或已批准替代验证：脚本缺少任一必需状态文件时退出码非 0。
  - 验证命令/动作：`py -3.10 tools/capture_mode_defaults_ui.py`
  - 预期结果：退出码 0，且 `visual-evidence.md` 列出 V1 到 V6 的图片路径
  - 质量层/证据：visual；Qt offscreen；默认宽度与会议窄宽度；证据路径 `openspec/changes/dictation-mode-defaults/visual-evidence.md`；required_for_verified 为 yes
  - 迁移/回滚：删除新截图和脚本即可
  - 完成定义：六种状态都有图，图中没有窗口标题，准备页没有 AI 字段
  - 负责人/冲突说明：最后执行，只读产品界面

## 集成顺序

任务 1 与任务 2、任务 5、任务 6 可并行。任务 3 在任务 2 之后。任务 4 在任务 3 之后。任务 7 最后。任务 5 不改 `sayink/app.py`。

## 最终验证

| 命令/动作 | 覆盖范围 | 预期结果 |
|---|---|---|
| `py -3.10 -m pytest tests/test_config_audio.py tests/test_settings_general.py tests/test_about_ux.py tests/test_config.py tests/test_text_paster.py tests/test_app.py tests/test_app_continuous_turns.py tests/test_app_history_wiring.py tests/test_onboarding.py tests/test_meeting_ux.py -q --tb=line` | 去向、迁移、粘贴目标、历史冲突、准备页、会中改字 | 退出码 0 |
| `py -3.10 -m ruff check sayink/config.py sayink/app.py sayink/text_paster.py sayink/audio_recorder.py sayink/onboarding.py sayink/meeting_session.py sayink/meeting_store.py sayink/ui/meeting_page.py sayink/ui/floating_window.py sayink/ui/settings_window.py sayink/ui/dictation_hints.py` | 本次改动文件的静态检查 | 退出码 0 |
| `py -3.10 tools/capture_mode_defaults_ui.py` | V1–V6 界面状态 | 退出码 0 |

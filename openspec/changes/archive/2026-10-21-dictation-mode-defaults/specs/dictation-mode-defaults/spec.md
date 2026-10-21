## ADDED Requirements

### Requirement: Single and continuous dictation remember output separately

The system MUST store the result destination of single-shot dictation and continuous dictation separately. Each destination MUST be one of automatic, paste-at-cursor, or history-only. Automatic MUST paste when the audio source is the microphone and MUST be history-only when the audio source is system audio or mixed. A manual choice in one mode MUST NOT change the other mode. The settings control MUST edit only the destination of the trigger mode currently selected. Fresh installs with no existing output fields MUST start both modes in automatic.

#### Scenario: 全新安装的单次转写输入光标

- **WHEN** 没有旧配置，结果去向字段不存在，录制方式为单次，音源为麦克风，用户按住默认快捷键说话后松开
- **THEN** 系统在识别完成后把文字输入开始说话时的目标窗口，并写入转写历史
- **AND** 不调用 AI 润色

#### Scenario: 持续转写的手动选择不改变单次转写

- **WHEN** 用户在持续转写中把结果去向改为「仅保存历史」，再切换到单次转写，音源仍是麦克风
- **THEN** 单次转写的结果去向仍是自动，识别结果输入光标并保存历史
- **AND** 再切回持续转写时，持续转写仍是「仅保存历史」

#### Scenario: 自动模式随音源切换且只作用于当前模式

- **WHEN** 当前模式的结果去向是自动，用户把音源改为电脑声音或混合
- **THEN** 当前模式变为只保存历史、不输入其他应用
- **AND** 另一种模式已保存的明确选择保持不变

### Requirement: Existing shared output choice migrates once

When per-mode destinations are absent and a legacy shared output choice exists, the system MUST migrate that choice once. If the legacy choice is automatic, both modes MUST become automatic. If it is an explicit paste or history-only choice, the saved trigger mode MUST keep that explicit choice and the other mode MUST become automatic. A missing trigger mode MUST be treated as single-shot. After per-mode values exist, later launches MUST NOT migrate again.

#### Scenario: 持续转写里手选的只记历史不带走单次

- **WHEN** 旧配置的录制方式是持续转写，结果去向是明确的「仅保存历史」，且还没有分模式字段
- **THEN** 首次加载后持续转写保持「仅保存历史」
- **AND** 单次转写变为自动

#### Scenario: 已经是自动的旧配置两种都保持自动

- **WHEN** 旧配置的结果去向会随音源变化，且还没有分模式字段
- **THEN** 单次和持续都变为自动
- **AND** 再次启动不会把后来手选的结果去向重置

### Requirement: Paste uses the window from the start of the utterance

For a paste destination, the system MUST remember the foreground window when the utterance starts: when a single-shot hold is accepted, or when a continuous segment's speech starts. At output, the system MUST send the paste shortcut only if that same window is still the foreground window and is an external pasteable window. The remembered label shown to the user MUST be the process name, not the window title. Meeting transcript output MUST NOT use this paste target and MUST NOT be pasted into another application.

#### Scenario: 识别期间切换窗口不再粘贴

- **WHEN** 用户在记事本按下单次转写并开始说话，识别完成前把前台切换到另一个应用
- **THEN** 系统不向新的前台窗口发送粘贴键
- **AND** 这段文字留在剪贴板

#### Scenario: 持续转写每段各自记住开口时的窗口

- **WHEN** 持续转写会话中，第一段开口时前台是应用 A，第二段开口时前台是应用 B，且两段输出时前台都没有再变化
- **THEN** 第一段输入应用 A，第二段输入应用 B
- **AND** 录音浮条在每一段开口后显示该段的目标进程名，直到该段输出结束

#### Scenario: 开始时没有可粘贴的外部窗口

- **WHEN** 开口时前台是 SayInk 自己的窗口、没有前台窗口，或目标进程拒绝模拟输入
- **THEN** 系统不向之后才出现的其他窗口发送粘贴键
- **AND** 文字留在剪贴板

#### Scenario: 会议文字稿不进入其他应用

- **WHEN** 会议正在记录，前台窗口在会议期间发生变化
- **THEN** 系统只把文字追加到会议文字稿
- **AND** 不发送粘贴键

### Requirement: A changed paste target is announced without blocking the next utterance

When paste is skipped because the foreground window changed after speech started, the system MUST keep the text on the clipboard and MUST show a non-modal floating notice naming the original target process. The notice MUST NOT require acknowledgement and MUST NOT prevent the next single-shot hold or the next continuous segment from starting.

#### Scenario: 目标变化时用浮条提示

- **WHEN** 一段话的目标窗口在输出前变了，剪贴板写入成功
- **THEN** 浮条说明没有输入到原来的进程，并提示可以粘贴
- **AND** 不出现必须点掉才能继续的对话框

#### Scenario: 浮条提示时下一段仍可开始

- **WHEN** 持续转写的上一段因为目标变化留在剪贴板，用户接着说下一段
- **THEN** 下一段按它自己开口时的窗口正常处理
- **AND** 上一段的文字仍可由用户手动粘贴

### Requirement: Missing speech model opens setup immediately

On a first launch where the welcome has not been seen and no speech model is installed, the system MUST show a setup page without waiting for the 15-second welcome fallback. The page MUST offer, in order, downloading the speech model, checking the microphone after the model is ready, and trying one spoken sentence. The trial sentence MUST appear on the setup page and MUST NOT be pasted into another application. The page MUST NOT ask for an AI service. Closing the page MUST mark the welcome as seen. A first launch that already has a speech model MUST keep the existing welcome dialog and history question timing.

#### Scenario: 轻量安装缺模型时立刻看到准备页

- **WHEN** 首次启动，欢迎尚未看过，并且本机没有已下载的语音模型
- **THEN** 准备页在主窗口出现后即可看见，而不是在约 15 秒后才第一次出现
- **AND** 页面包含下载模型、模型就绪后的麦克风测试，以及试说一句

#### Scenario: 试说一句不进入其他应用

- **WHEN** 用户在准备页试说一句并识别成功
- **THEN** 识别文字显示在准备页
- **AND** 其他应用没有收到粘贴键

#### Scenario: 模型已在时仍用原来的欢迎

- **WHEN** 首次启动时语音模型已经下载
- **THEN** 系统沿用现有欢迎对话框，并在模型就绪或现有兜底计时后显示
- **AND** 不显示缺模型准备页

#### Scenario: 麦克风测试没有听到声音

- **WHEN** 模型已就绪，用户进行麦克风测试但没有检测到输入
- **THEN** 准备页说明没有听到声音，并允许重试或稍后继续
- **AND** 不阻止关闭准备页

### Requirement: History-only output with history disabled must be resolved before recording

Before starting a single-shot hold or a continuous session, if the active mode's effective destination is history-only and transcription history is disabled, the system MUST NOT start recording. It MUST offer exactly the choices to enable history or to change the active mode's destination to paste-at-cursor. Dismissing the choice MUST leave the destination and history setting unchanged and MUST NOT start recording. If this conflict appears while a continuous session is already recording, the system MUST NOT discard the current segment; dismissing MUST end that session and leave the current segment on the clipboard without pasting it. Meeting recording and meeting storage MUST ignore this dictation conflict.

#### Scenario: 开录前必须先选择

- **WHEN** 转写历史已关闭，当前模式的有效结果去向是仅保存历史，用户按下录音快捷键
- **THEN** 录音不开始
- **AND** 用户可以选择「开启历史」或「改为输入光标」

#### Scenario: 开启历史后才开始

- **WHEN** 冲突选择出现，用户选择「开启历史」
- **THEN** 转写历史变为开启，并且这次录音开始
- **AND** 另一种模式的结果去向不变

#### Scenario: 取消选择不录音

- **WHEN** 冲突选择出现，用户关闭或取消
- **THEN** 历史仍然关闭，结果去向不变
- **AND** 没有开始录音

#### Scenario: 会议不受普通历史开关影响

- **WHEN** 转写历史已关闭，用户开始会议
- **THEN** 会议正常记录
- **AND** 不出现上述开录前选择

### Requirement: Finished meeting segments can be edited during a live meeting

While a meeting is live or paused, the system MUST let the user edit the text of a segment that has already been stored in the transcript. The edit MUST keep that segment's original text, time, and speaker, and MUST remain in place when later segments are appended. The system MUST NOT offer text editing for speech that has not yet been stored as a segment. The system MUST refuse the edit while that meeting is draining or while its notes are being generated. An existing ended or interrupted meeting MUST remain editable one segment at a time whenever notes are not being generated, including when notes have never been generated. User-facing copy MUST NOT say that text editing waits until notes generation has finished.

#### Scenario: 进行中修改已经落段的文字

- **WHEN** 会议正在录制，某一段已经显示在文字稿中，用户修改这一段并保存
- **THEN** 文字稿显示修改后的文字，该段的原文、时间和发言人仍在
- **AND** 之后新的发言继续追加，这一段的修改不被覆盖

#### Scenario: 暂停时同样可以修改已落段文字

- **WHEN** 会议处于暂停，文字稿里已有段落，用户修改其中一段并保存
- **THEN** 修改被保存
- **AND** 暂停状态和尚未恢复的采集保持不变

#### Scenario: 还没落段的当前句不能改

- **WHEN** 会议正在录制，用户正在说的内容还没有写成文字稿段落
- **THEN** 这段未落段的内容没有文字编辑入口
- **AND** 已经落段的其他段落仍可编辑

#### Scenario: 收尾或生成期间拒绝修改

- **WHEN** 会议正在结束收尾，或这场会议的纪要或原文精整正在生成，用户保存某一段的文字修改
- **THEN** 保存被拒绝，该段文字保持保存前的内容
- **AND** 不出现「必须等纪要生成完成才能编辑」作为会后常态提示

#### Scenario: 会后未生成纪要也能编辑

- **WHEN** 会议已结束或已中断，从未生成纪要，也没有正在生成
- **THEN** 用户可以按段编辑文字稿
- **AND** 提示说明现在可以编辑，而不是要求先完成纪要生成

---

# dictation-mode-defaults：需求规格

## 目的

让单次转写、持续转写和会议在全新安装与日常切换时，各自把字送到用户心里已经认定的地方：单次和持续默认对着开始说话的那个窗口，会议留在会议记录里，并且已经落段的文字能改。

## 范围

### 本次范围

- 单次与持续各自记住结果去向，初始都是自动随音源。
- 旧的一份结果去向只迁移一次。
- 粘贴目标取开口时的窗口；变了就留剪贴板并用浮条提示。
- 缺模型的首次启动直接进入准备页。
- 只记历史但历史关闭时，普通转写开始前先选择。
- 会议进行中或暂停时，修改已经落段的文字；未落段、收尾中和生成中不能改。

### 非目标

- 不改变会议的开始、暂停、结束和 Esc，也不把会议文字粘贴到其他应用。
- 不把主识别模型打进轻量安装包。
- 不在准备页配置 AI。
- 不改默认音源、默认快捷键和默认关闭润色。

## 兼容性与外部契约

- 分模式字段缺失时等于该模式自动。旧的共用字段只在分模式字段不存在时读取一次。
- 展示和提示只用进程名，不使用窗口标题。
- 会议输出不读取普通转写的结果去向，也不读取 `history.enabled` 来决定会议记录是否保存。

## 验收矩阵

| 需求/场景 | 验证方法 | 可证伪的失败表现 |
|---|---|---|
| 分模式去向与全新安装默认 | 配置与设置测试 | 持续转写的「仅保存历史」切回单次后仍然不输入光标 |
| 旧配置只迁移一次 | 用旧 `output.mode` 构造配置后加载两次 | 第二次启动把用户后来的选择重置；或两种模式被写成同一个明确选择 |
| 开口窗口与识别期间切换 | 粘贴器与应用输出测试，替换前台窗口读取 | 文字进入切换后的窗口，或开口时是 SayInk 自己却粘进了别的应用 |
| 持续转写分段目标与浮条 | 连续分段测试 | 第二段使用第一段的窗口，或浮条显示窗口标题 |
| 目标变化的浮条 | 输出反馈测试 | 出现模态对话框，或下一段无法开始 |
| 缺模型准备页 | 首装引导测试，模型枚举为空 | 仍要等约 15 秒才出现下载入口，或试说一句把文字粘进其他应用 |
| 模型已在的欢迎 | 首装引导测试，模型枚举非空 | 出现缺模型准备页，或欢迎不再等待模型就绪 |
| 历史关闭且只记历史 | 开录前拦截测试 | 录音已经开始，结束后才提示未保存 |
| 会议隔离 | 会议输出测试，历史关闭 | 会议文字被粘贴，或会议因为普通历史关闭而无法记录 |
| 会中修改已落段文字 | 会议文字稿编辑测试，进行中与暂停 | 修改被拒绝、原文丢失，或后到的新段落覆盖了修改 |
| 未落段与收尾不能改 | 会议进行中尚无该段、以及收尾或生成中的保存 | 未落段内容出现编辑入口，或收尾期间保存成功 |
| 会后提示与未生成纪要 | 已结束且没有纪要的文字稿 | 提示仍要求先生成纪要，或没有纪要时不能编辑 |

## 已确认决策

| 决策项 | 选择 | 批准人/日期 | 影响的需求 |
|---|---|---|---|
| 旧结果去向如何拆开 | 已保存的录制方式保留明确选择；另一种回到自动；当时已是自动则两种都自动 | 用户 / 2026-10-21 | Existing shared output choice migrates once |
| 持续转写的目标窗口 | 每一段开口时的前台窗口 | 用户 / 2026-10-21 | Paste uses the window from the start of the utterance |
| 目标变化如何提示 | 非模态浮条，文字留在剪贴板，不打断下一段 | 用户 / 2026-10-21 | A changed paste target is announced without blocking the next utterance |
| 模型已下载的首次启动 | 保持现有欢迎和历史提问 | 非阻塞默认 / 2026-10-21 | Missing speech model opens setup immediately |
| 会议文字何时可改 | 进行中和暂停可改已落段文字；未落段、收尾和生成期间不改 | 用户 / 2026-10-21 | Finished meeting segments can be edited during a live meeting |

## 显式未知项

- 真机上不同应用是否接受模拟粘贴、麦克风是否被系统独占、本机识别速度。这些不改变本规格的可观察行为，留在实施后的手工验收。

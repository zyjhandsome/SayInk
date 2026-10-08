# 音视频文件转写 + 轻翻译：实施任务清单

## 执行规则

- 权威状态源：`openspec/changes/av-transcribe-and-light-translate/`
- 风险/闸门：Standard / `medium`；规格已批准；实施须另获明确「开始实施」
- 禁止范围：真同传；实时听写翻译；历史 DDL 新列；方案 B/C；改热键听写语义；`QThread.terminate` ASR
- 必须执行的最终验证：`python -m pytest tests/ -q`（或项目惯用等价命令）全绿；手动：短视频/音频导入转写；翻译互斥与降级；热键听写回归

## 1. 媒体解码与捆绑

- [x] 1.1 实现捆绑 ffmpeg 路径解析与媒体→PCM 解码
  - 对应需求/场景：捆绑媒体解码能力；解码失败可见；空音频失败
  - 前置依赖：无
  - 目标文件/符号：新建 `sayink/media_decoder.py`（`resolve_ffmpeg_executable`、`decode_media_to_pcm`）；新建 `tests/test_media_decoder.py`
  - 允许修改：上述新文件；必要时极小工具函数
  - 禁止修改：`speech_recognizer.py` ASR 内核；听写热键逻辑
  - 实施步骤：列表 argv 调用 ffmpeg；输出 mono float32 16 kHz；分类错误；支持取消（杀子进程）；禁止 `shell=True`
  - 失败测试或已批准替代验证：先写 `tests/test_media_decoder.py`（mock subprocess / 假二进制行为）
  - 验证命令/动作：`python -m pytest tests/test_media_decoder.py -q`
  - 预期结果：路径解析与错误分类用例通过
  - 迁移/回滚：删除模块即可；无持久化
  - 完成定义：单测绿；无真实网络依赖
  - 负责人/冲突说明：独占 `media_decoder.py`

- [x] 1.2 将 ffmpeg 纳入 PyInstaller/发布产物
  - 对应需求/场景：无系统 ffmpeg 仍可解码
  - 前置依赖：1.1
  - 目标文件/符号：`build.py`；必要时 `installer/SayInk-Setup.iss`；构建说明中的 ffmpeg 获取步骤；约定放置目录（如 `third_party/ffmpeg/`）
  - 允许修改：构建脚本与安装拷贝规则、简短构建文档
  - 禁止修改：运行时听写逻辑
  - 实施步骤：构建时把 ffmpeg 拷入 `dist/SayInk/`（或 `--add-binary`）；确保冻结态能解析；文档写清来源/许可
  - 失败测试或已批准替代验证：脚本/断言检查产物路径存在（无输入时跳过并明确提示）
  - 验证命令/动作：有 ffmpeg 输入时跑构建或最小产物检查
  - 预期结果：冻结路径解析指向捆绑二进制
  - 迁移/回滚：去掉拷贝步骤即回退包体
  - 完成定义：路径约定与构建步骤可重复执行
  - 负责人/冲突说明：独占构建脚本相关行

## 2. 文件转写任务（无翻译）

- [x] 2.1 App 文件任务：解码→ASR→历史（含互斥）
  - 对应需求/场景：导入并转写；历史写入与来源可区分；互斥策略；文件失败行为
  - 前置依赖：1.1
  - 目标文件/符号：`sayink/app.py`（`start_file_transcription` / `cancel_file_transcription` / file job 状态）；必要时历史 pending 文件变体；`tests/test_app_file_transcription.py`（新）或扩展 `tests/test_app.py`
  - 允许修改：`app.py` 及相关测试；常量 `source=file`、`trigger_mode=file_import`
  - 禁止修改：翻译模式；DDL；无关设置页大改
  - 实施步骤：硬互斥实时录音/转写；复用 `transcribe_final`；成功写历史；默认不自动粘贴；取消协作式停止
  - 失败测试或已批准替代验证：先写互斥/历史 source/失败提示测试（mock decoder+recognizer）
  - 验证命令/动作：`python -m pytest tests/test_app_file_transcription.py tests/test_app.py -q`
  - 预期结果：新测与听写相关回归通过
  - 迁移/回滚：去掉入口调用即可
  - 完成定义：无 UI 也可经测试驱动文件任务逻辑
  - 负责人/冲突说明：独占 `app.py` 文件任务段（与 3.x 串行）

- [x] 2.2 托盘/历史「导入文件」UI与进行中取消
  - 对应需求/场景：用户可导入；取消文件转写；进行中状态
  - 前置依赖：2.1
  - 目标文件/符号：`sayink/ui/tray_icon.py`；`sayink/ui/history_window.py`（可选同入口）；`sayink/ui/floating_window.py`；`app.py` 接线；相关 UI 测试
  - 允许修改：上述 UI + app 接线
  - 禁止修改：解码实现细节；翻译设置
  - 实施步骤：菜单项 + `QFileDialog`；连接启动；展示阶段状态；提供取消
  - 失败测试或已批准替代验证：信号/槽或轻量 UI 测试；不足部分手册清单记入最终验证
  - 验证命令/动作：`python -m pytest tests/test_floating_window.py tests/test_history_window.py -q`（及新增用例）
  - 预期结果：入口可触发；取消有覆盖
  - 迁移/回滚：隐藏菜单项
  - 完成定义：用户可从托盘完成一次导入流程（开发态）
  - 负责人/冲突说明：UI 文件与 4.1 协调

## 3. 文件任务轻翻译

- [x] 3.1 TextPolisher 翻译模式与配置键
  - 对应需求/场景：润色\|翻译互斥；翻译失败回退（单元层）
  - 前置依赖：无（可与 2.x 并行，合并前需 2.1）
  - 目标文件/符号：`sayink/text_polisher.py`；`sayink/config.py`（`llm.mode`、`llm.target_language` 默认）；`tests/test_text_polisher.py`
  - 允许修改：上述文件
  - 禁止修改：`app.py` 文件任务接线（留给 3.2）；DDL
  - 实施步骤：`TRANSLATE_PROMPT`；`mode` 参数；缺省 `polish`；保持 URL 安全策略
  - 失败测试或已批准替代验证：先扩展 polisher 测试
  - 验证命令/动作：`python -m pytest tests/test_text_polisher.py -q`
  - 预期结果：翻译/润色 prompt 与互斥语义单测通过
  - 迁移/回滚：缺省 mode=polish 兼容旧配置
  - 完成定义：不依赖 UI 即可测
  - 负责人/冲突说明：独占 polisher/config；与 3.2 串行合并

- [x] 3.2 仅文件任务启用翻译 + 设置文案（非同传）
  - 对应需求/场景：文件可翻译；实时不翻译；失败回退；非同声传译边界
  - 前置依赖：2.1、3.1
  - 目标文件/符号：`app.py`（file job 后处理分支）；`sayink/ui/settings_window.py` / `settings_components.py`；测试
  - 允许修改：上述
  - 禁止修改：实时路径自动翻译；schema
  - 实施步骤：仅文件任务 + mode=translate + enabled 时翻译；实时仍只润色；降级提示；设置文案为转写后翻译
  - 失败测试或已批准替代验证：app 级测试区分 file vs live
  - 验证命令/动作：`python -m pytest tests/test_app_file_transcription.py tests/test_app.py tests/test_settings_general.py -q`
  - 预期结果：分支与文案约束满足
  - 迁移/回滚：mode 默认 polish
  - 完成定义：规格场景可测
  - 负责人/冲突说明：在 2.1 之后改 app 后处理

## 4. 历史展示与收尾

- [x] 4.1 历史来源展示与原文/译文查看
  - 对应需求/场景：历史来源可区分；翻译结果进入历史
  - 前置依赖：2.1、3.2
  - 目标文件/符号：`sayink/ui/history_window.py`；必要时测试
  - 允许修改：历史 UI 展示逻辑
  - 禁止修改：DDL
  - 实施步骤：`source=file` 显示为文件转写；保留 raw/polished（译文）查看
  - 失败测试或已批准替代验证：历史窗测试或手册检查
  - 验证命令/动作：`python -m pytest tests/test_history_window.py -q`
  - 预期结果：文件条目标注正确
  - 迁移/回滚：展示文案回退
  - 完成定义：与规格一致
  - 负责人/冲突说明：与 2.2 同文件时串行

- [x] 4.2 最终回归与打包冒烟清单
  - 对应需求/场景：全规格验收；听写不回归
  - 前置依赖：1.2、2.2、3.2、4.1
  - 目标文件/符号：无功能代码（可补验证笔记，非第二状态源）
  - 允许修改：仅测试/文档笔记
  - 禁止修改：功能范围外重构
  - 实施步骤：跑全量 pytest；手册：短 mp3/mp4、翻译开关、互斥、热键听写
  - 失败测试或已批准替代验证：全量自动测 + 手册项勾选
  - 验证命令/动作：`python -m pytest tests/ -q`
  - 预期结果：全绿；手册项通过或记缺口
  - 迁移/回滚：不适用
  - 完成定义：实现闸门前证据齐全
  - 负责人/冲突说明：收尾独占

## 集成顺序

1.1 → 1.2（打包可稍后但发布前必须）  
1.1 → 2.1 → 2.2  
3.1 ∥ 2.1 → 3.2 → 4.1 → 4.2  

并行安全：`3.1` 可与 `2.1` 并行（不同文件）；`app.py` 仅串行任务改；`history_window` 上 2.2/4.1 串行。

## 最终验证

| 命令/动作 | 覆盖范围 | 预期结果 |
|---|---|---|
| `python -m pytest tests/ -q` | 自动回归 + 新测 | 全绿 |
| 开发态导入短音频/视频 | 解码+ASR+历史 | 成功且 source 可辨 |
| 开启翻译（文件）/润色互斥 | 轻翻译 | 译文或降级提示 |
| 热键听写一轮 | 兼容 | 行为与改前一致 |
| （有构建输入时）检查 dist 内 ffmpeg | 捆绑 | 文件存在且冻结路径可解析 |

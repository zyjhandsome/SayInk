# 去掉音视频文件转写：实施任务清单

## 执行规则

- 权威状态源：`openspec/changes/remove-media-file-transcription/`
- 风险/闸门：Standard / `medium`；规格已批准；实现须用户放行后由 `delivery-execute-verify` 执行
- 禁止范围：改听写热键语义；删历史 DB 行；改无关 settings 视觉 polish；在本阶段 sync/archive 主规格
- 必须执行的最终验证：见文末「最终验证」表

## 1. 去掉轻翻译并回落配置

- [x] 1.1 Config 将 `llm.mode=translate` 静默规范为 `polish`
  - 对应需求/场景：遗留 translate 配置静默回落润色 / 读取时回落 polish
  - 前置依赖：无
  - 目标文件/符号：`sayink/config.py`（`get` 或加载路径规范化 `llm.mode`）；可选默认字典去掉或保留但忽略 `target_language`
  - 允许修改：`sayink/config.py`；`tests/test_config.py`（或新建等价用例）
  - 禁止修改：`history_store.py`；听写录音模块
  - 实施步骤：在读取 `llm.mode` 时若值为 `translate`（大小写不敏感）则返回 `polish`；单测写入 translate 后断言读出 polish
  - 失败测试或已批准替代验证：先写/改测试断言 `get("llm.mode")` 对 translate 回落，再改实现
  - 验证命令/动作：`python -m pytest tests/test_config.py -q -k "mode or translate or llm" --tb=short`（若无匹配则跑含新用例的文件全量）
  - 预期结果：新/改用例通过；translate 不导致异常
  - 迁移/回滚：读时规范化；git revert
  - 完成定义：测试绿；设置未打开时 App 读到的 mode 亦为 polish
  - 负责人/冲突说明：独享 `config.py`

- [x] 1.2 移除 TextPolisher 翻译模式与相关测试
  - 对应需求/场景：产品不再提供轻翻译模式 / 运行时不执行翻译语义
  - 前置依赖：1.1 可并行，建议同批
  - 目标文件/符号：`sayink/text_polisher.py`（`LLM_MODE_TRANSLATE`、`TRANSLATE_PROMPT_TEMPLATE`、`build_system_prompt` 分支、`PolishWorker` 翻译文案）；`tests/test_text_polisher.py` 中 translate 用例改为删除或改为「不支持」
  - 允许修改：上述文件
  - 禁止修改：润色成功路径语义
  - 实施步骤：删除 translate 常量与分支；保留 polish；更新测试只覆盖 polish
  - 失败测试或已批准替代验证：删除依赖 translate 的测试；保留 polish 测试先红后绿（若误删 polish）
  - 验证命令/动作：`python -m pytest tests/test_text_polisher.py -q --tb=short`
  - 预期结果：全部通过；源码无 `LLM_MODE_TRANSLATE` / 翻译专用 prompt
  - 迁移/回滚：git revert
  - 完成定义：polisher 仅 polish；测试绿
  - 负责人/冲突说明：独享 `text_polisher.py`；与任务 3 的 `app.py` import 衔接在 3 完成

- [x] 1.3 设置页去掉翻译 UI 与文案
  - 对应需求/场景：设置无翻译模式
  - 前置依赖：1.1
  - 目标文件/符号：`sayink/ui/settings_window.py`（模式 combo、目标语言行、hero/帮助文案「润色 / 翻译」）；相关 `tests/test_ui_styles.py` / settings 测试若断言翻译文案
  - 允许修改：上述 UI 与对应测试
  - 禁止修改：settings-control-alignment 无关控件布局大改
  - 实施步骤：去掉 translate 选项与目标语言控件；文案回到润色-only；加载时显示规范化后的 mode
  - 失败测试或已批准替代验证：更新/新增断言「无翻译（仅文件转写）」文案
  - 验证命令/动作：`python -m pytest tests/test_ui_styles.py tests/test_settings_general.py -q --tb=short`（按实际存在的 settings 测试文件裁剪）
  - 预期结果：通过；设置源码无翻译模式入口
  - 迁移/回滚：git revert
  - 完成定义：UI 无翻译；测试绿
  - 负责人/冲突说明：独享 settings 翻译相关区块

## 2. 去掉托盘导入入口

- [x] 2.1 移除托盘「导入文件转写」菜单与信号
  - 对应需求/场景：托盘无导入入口
  - 前置依赖：无（可与 1 并行）
  - 目标文件/符号：`sayink/ui/tray_icon.py`（`import_file_requested`、菜单项）
  - 允许修改：`tray_icon.py`；若有托盘菜单测试则更新
  - 禁止修改：其他托盘项（设置/历史/退出等）语义
  - 实施步骤：删除信号、菜单 action 与触发连接
  - 失败测试或已批准替代验证：新增或改测试断言菜单 action 文本集合不含「导入文件转写」；或断言无 `import_file_requested` 属性
  - 验证命令/动作：`python -m pytest tests/ -q -k "tray" --tb=short`；若无 tray 测试则：`python -c "from sayink.ui.tray_icon import TrayIcon; assert not hasattr(TrayIcon, 'import_file_requested')"`
  - 预期结果：无导入信号/菜单文案
  - 迁移/回滚：git revert
  - 完成定义：入口不可见；断言通过
  - 负责人/冲突说明：独享 `tray_icon.py`；任务 3 删除 App 侧 `connect`

## 3. 拆除 App 文件任务与解码模块

- [x] 3.1 从 App 移除文件转写编排并恢复听写路径简洁互斥
  - 对应需求/场景：无法启动新的文件转写任务；撤回后不破坏实时听写主路径
  - 前置依赖：2.1（避免悬空 connect）；1.2（去掉 translate 分支）
  - 目标文件/符号：`sayink/app.py`（`_FileDecodeWorker`、`start_file_transcription`、`cancel_file_transcription`、`_file_job_*`、`HISTORY_SOURCE_FILE` 写入、`TRIGGER_MODE_FILE_IMPORT`、文件结果跳过粘贴分支、ERROR_HINTS 中 ffmpeg/解码/翻译失败、对 `import_file_requested` 的 connect、后处理中 translate 分支）
  - 允许修改：`app.py`；删除/改写 `tests/test_app_file_transcription.py` 为「缺席/听写不受文件逻辑影响」类测试
  - 禁止修改：热键听写成功路径的可观察输出语义；`history_window` 展示映射
  - 实施步骤：删除文件任务符号与分支；去掉 media_decoder import；保留纯听写 `_begin_transcription`；将原 file 测试改为断言无 `start_file_transcription` 或删除该文件并加最小缺席测试
  - 失败测试或已批准替代验证：先让旧 `test_app_file_transcription.py` 失败/删除，再以缺席断言+`tests/test_app.py` 回归证明听写仍在
  - 验证命令/动作：`python -m pytest tests/test_app.py tests/test_app_file_transcription.py -q --tb=short`（若后者删除则只跑 `test_app.py` + 新缺席测试文件）
  - 预期结果：通过；`app.py` 无文件转写 API
  - 迁移/回滚：git revert
  - 完成定义：无文件任务；听写回归绿
  - 负责人/冲突说明：**主责 `app.py`**；勿与他人并行改同一文件

- [x] 3.2 删除 `media_decoder` 模块及其测试
  - 对应需求/场景：无法启动新的文件转写任务（解码能力随产品撤回）
  - 前置依赖：3.1（App 不再 import）
  - 目标文件/符号：删除 `sayink/media_decoder.py`；删除 `tests/test_media_decoder.py`、`tests/test_media_decoder_import_smoke.py`
  - 允许修改：删除上述文件；若包 `__init__` 导出则清理
  - 禁止修改：ASR/录音模块
  - 实施步骤：删模块与测试；全库 grep 确认无残留 import
  - 失败测试或已批准替代验证：`python -c "import sayink.media_decoder"` 应失败（ModuleNotFoundError）
  - 验证命令/动作：`python -m pytest tests/test_media_decoder.py tests/test_media_decoder_import_smoke.py -q` 应收集为 0 或文件已不存在；另跑 `python -c "import importlib; importlib.import_module('sayink.media_decoder')"` 期望失败
  - 预期结果：模块不可导入；无残留引用
  - 迁移/回滚：git revert
  - 完成定义：grep 无 `media_decoder` 产品引用
  - 负责人/冲突说明：在 3.1 之后串行

## 4. 移除打包 ffmpeg 挂钩

- [x] 4.1 从 `build.py` 移除 ffmpeg 拷贝并改写测试；清理 `third_party/ffmpeg`
  - 对应需求/场景：构建不拷贝文件转写 ffmpeg；发布物不再捆绑
  - 前置依赖：3.2（产品不再依赖解码）可并行于 1–2，但须在最终验证前完成
  - 目标文件/符号：`build.py`（`_ffmpeg_binary_name`、`_find_ffmpeg_bundle_source`、`_copy_ffmpeg_into_dist`、构建步骤打印）；`tests/test_build_ffmpeg.py`（改为断言符号不存在或不再拷贝）；`third_party/ffmpeg/README.md` 删除或改为「已废弃勿用」后删除目录；`.gitignore` 中 ffmpeg 规则可删
  - 允许修改：上述文件
  - 禁止修改：与 ffmpeg 无关的打包主流程（除步骤编号/摘要）
  - 实施步骤：删除拷贝调用与辅助函数；测试断言 `hasattr(build, '_copy_ffmpeg_into_dist') is False` 或等价；移除 third_party 说明目录
  - 失败测试或已批准替代验证：先改测试期望「无拷贝函数」，再改 `build.py`
  - 验证命令/动作：`python -m pytest tests/test_build_ffmpeg.py -q --tb=short`
  - 预期结果：通过；构建脚本无 ffmpeg 捆绑步骤
  - 迁移/回滚：git revert；勿在实施中提交大型二进制
  - 完成定义：测试绿；`build.py` grep 无 ffmpeg 捆绑逻辑
  - 负责人/冲突说明：独享 `build.py`；可与任务 1 并行

## 5. 历史遗产展示与最终回归

- [x] 5.1 保留并锁定历史「文件转写 / 导入文件」只读展示
  - 对应需求/场景：既有文件历史仍可查看
  - 前置依赖：3.1（确认未误删展示）
  - 目标文件/符号：`sayink/ui/history_window.py`（`source=="file"` →「文件转写」；`trigger_mode=="file_import"` →「触发：导入文件」）；`tests/test_history_window.py`（新增/保留构造 file 记录的断言）
  - 允许修改：仅测试加强；**原则上不删**上述映射代码
  - 禁止修改：删除 file 展示分支；清理 DB
  - 实施步骤：若映射仍在则补回归测试；若被误删则恢复映射
  - 失败测试或已批准替代验证：测试构造 `source=file`、`trigger_mode=file_import` 的摘要/详情，断言文案存在
  - 验证命令/动作：`python -m pytest tests/test_history_window.py -q --tb=short`
  - 预期结果：通过；映射代码仍在
  - 迁移/回滚：无数据迁移
  - 完成定义：遗产展示测试绿
  - 负责人/冲突说明：独享 history 测试；勿改 store schema

- [x] 5.2 最终回归套件
  - 对应需求/场景：撤回后不破坏实时听写；翻译/文件缺席
  - 前置依赖：1–5.1
  - 目标文件/符号：无新功能；跑既有测试集
  - 允许修改：仅修复本变更引起的测试断裂
  - 禁止修改：借回归扩大范围
  - 实施步骤：跑最终验证表命令；修复失败项
  - 失败测试或已批准替代验证：命令必须全绿
  - 验证命令/动作：见「最终验证」
  - 预期结果：全绿
  - 迁移/回滚：无
  - 完成定义：最终验证表全部满足
  - 负责人/冲突说明：集成所有者

## 集成顺序

1. 任务 1（配置/翻译）与任务 4（build/ffmpeg）可并行  
2. 任务 2（托盘）可与任务 1 并行  
3. 任务 3（App + media_decoder）在 1.2 与 2.1 之后串行  
4. 任务 5 在 3 之后  
5. 禁止并行双写 `app.py`

## 最终验证

| 命令/动作 | 覆盖范围 | 预期结果 |
|---|---|---|
| `python -m pytest tests/test_config.py tests/test_text_polisher.py tests/test_app.py tests/test_history_window.py tests/test_build_ffmpeg.py -q --tb=short` | 回落、润色、听写、历史遗产、打包 | 全通过 |
| `python -m pytest tests/ -q --tb=line`（若过长可改为上述+`test_ui_styles`/`test_settings_general`） | 宽回归 | 全通过；无残留 media_decoder 收集错误 |
| `rg -n "import_file_requested|start_file_transcription|LLM_MODE_TRANSLATE|media_decoder|_copy_ffmpeg_into_dist" sayink build.py`（或等价） | 产品代码残留 | 无匹配（测试/openspec/archive 除外） |
| 手动（可选）：开托盘菜单 | 无导入项 | 目视确认 |

# 去掉音视频文件转写：技术实施计划

## Context

已批准规格要求撤回本地音/视频文件转写与轻翻译；保留历史中 `source=file` / `trigger_mode=file_import` 的只读展示；打包不再捆绑 ffmpeg；`llm.mode=translate` 读取时静默回落 `polish`。实现已存在于 `App` 文件任务、`media_decoder`、托盘入口、设置翻译模式与 `build.py` 拷贝逻辑。本设计只描述如何拆除，不改变实时热键听写语义。

## Goals / Non-Goals

**Goals:**

- 产品面不再能启动文件转写；托盘无导入入口
- 删除解码模块与打包 ffmpeg 挂钩
- 去掉翻译模式与相关 prompt/UI；遗留配置回落润色
- 历史遗产展示保留；热键听写 + 润色回归通过

**Non-Goals:**

- 删除或迁移历史库行；改 SQLite DDL
- 重做实时听写 / 新增替代文件转写服务
- 在本阶段 sync/archive 主规格（属后续 archive 流程）

## 已批准目标与约束

- 目标：见 `proposal.md` 意图成功标准 1–6
- 非目标：见简报边界
- 风险/闸门：Standard / `medium`；规格已批准（用户 2026-07-15）；实现闸门待本阶段放行
- 已决：开放问题 1–4 全 A

## 已刷新代码事实

| 结论 | 证据 | 新鲜度 |
|---|---|---|
| 托盘有 `import_file_requested` 与「导入文件转写…」 | `sayink/ui/tray_icon.py` | 2026-07-15 定点读 |
| App 文件任务完整存在 | `start_file_transcription` / `_FileDecodeWorker` / `_file_job_*` | Memory + 源码 |
| 解码模块独立 | `sayink/media_decoder.py` | 磁盘存在 |
| 打包拷贝 ffmpeg | `build.py` `_copy_ffmpeg_into_dist`；`tests/test_build_ffmpeg.py` | 定点读 |
| 翻译模式在 polisher/设置/App 后处理分支 | `LLM_MODE_TRANSLATE`；`settings_window` 模式 combo；`app.py` ~L698+ | 定点读 |
| 历史展示 `file` / `file_import` | `history_window.py` L392–405 | 定点读；**须保留** |
| README 无「导入/文件转写/翻译/ffmpeg」命中 | `README.md` grep | 非阻塞文案可跳过 |
| 无其他活跃 change 冲突 | `openspec list` 仅本变更 | 2026-07-15 |

## 方案比较

### 方案 A — 物理拆除（建议）

- 方案形态：删除入口、App 文件任务、`media_decoder`、翻译模式代码与 ffmpeg 打包挂钩；测试改为证明缺席 + 听写回归；历史展示映射保留
- 收益：与已决 1A/3A 一致；无死代码与假入口
- 成本/风险：`app.py` 改动面较大；须仔细拆互斥分支以免听写回归
- 可逆性：git revert 可回滚
- 验证方式：pytest 缺席/回归 + 托盘菜单断言 + build 无拷贝符号

### 方案 B — 仅藏入口、保留解码/打包

- 方案形态：去掉菜单但仍保留 `media_decoder`/`build.py` ffmpeg
- 收益：改动更小
- 成本/风险：与已决 3A 冲突；留下无产品作用的发布体积与维护面
- 可逆性：高
- 验证方式：仅 UI 断言，规格「发布物不再捆绑」无法满足

### 方案 C — 分两阶段（先 UI 后内核）

- 方案形态：先藏入口，后续 PR 再删模块
- 收益：可分 PR
- 成本/风险：中间态仍含可调用 `start_file_transcription`；与「无法启动新任务」场景不完全一致
- 可逆性：高
- 验证方式：两阶段重复验证成本更高

## 最终决策

- 选定方案：**A — 物理拆除**
- 选择理由：与规格 ADDED/REMOVED 及已决 1–4 对齐；避免死代码与规格缺口
- 未选方案及原因：B 违反 3A；C 拉长不一致窗口且本变更体量可单次完成
- （无需再向用户征询方案：产品约束已锁定 A）

## 集成方式与数据流/控制流

撤回后控制流：

```text
热键听写（保留）
  AudioRecorder → SpeechRecognizer → [可选 TextPolisher polish] → 粘贴/历史

文件转写（删除）
  托盘导入 / start_file_transcription / media_decoder / ffmpeg  —— 全部移除

配置
  Config 读取 llm.mode==translate → 规范化为 polish（读时静默）
```

历史只读：DB 中既有 `source=file` 行不变；`history_window` 继续映射「文件转写」「触发：导入文件」。

## 接口与状态模型

- **删除**：`App.start_file_transcription` / `cancel_file_transcription` / `_FileDecodeWorker` / `_file_job_*` / `HISTORY_SOURCE_FILE` 写入路径；托盘 `import_file_requested`；整个 `sayink.media_decoder`；`LLM_MODE_TRANSLATE` 与翻译 prompt；设置页翻译 combo/目标语言行；`build._copy_ffmpeg_into_dist` 等
- **保留**：`history_window` 对 `file` / `file_import` 的展示映射；润色 `LLM_MODE_POLISH`；实时听写互斥逻辑中仅去掉文件任务相关分支
- **配置键**：可保留 `llm.target_language` 键于文件中但不暴露 UI、不参与运行时；或读时忽略。`llm.mode` 非法/`translate` → `polish`

## 失败处理与可观测性

- 配置回落：静默，不弹窗、不阻断启动
- 删除后不应再出现「ffmpeg/解码/文件转写」类 ERROR_HINTS 用户路径
- 日志：去掉文件任务 info；听写日志保持

## 兼容、迁移与回滚

- 迁移：读配置时 normalize `translate`→`polish`；不写回也可（下次保存时自然写成 polish）——**建议在 Config 加载或 `get("llm.mode")` 时规范化，并在设置打开时显示 polish**
- 历史：无迁移脚本
- 回滚：还原本 change 的 git 提交；重新引入 third_party ffmpeg 说明
- 打包：去掉 ffmpeg 步骤后构建步骤编号/摘要需同步，避免文档谎称已捆绑

## 安全与性能

- 安全：移除 subprocess 调 ffmpeg，减少攻击面；无新网络面
- 性能：减小发布体积（视原捆绑大小）；启动少一模块导入
- 不适用：鉴权/支付

## 验证策略

| 层 | 内容 |
|---|---|
| 单元 | Config translate 回落；polisher 无 translate API；build 无 ffmpeg 辅助函数或行为断言为「不拷贝」 |
| 集成 | App 无 `start_file_transcription`；托盘菜单无导入项；历史 `file_import` 展示仍在 |
| 回归 | 既有 `tests/test_app.py` / polisher polish / settings 润色路径 |
| 手动（最终可选） | 打开托盘确认无导入；设置无翻译项 |
| 缺口 | 完整安装包体积对比 —— 记为警告项可接受，用单元断言替代 |

## 需求追溯

| 需求/场景 | 设计要素 | 任务 | 验证 |
|---|---|---|---|
| 产品不再提供音视频文件导入转写 / 托盘无导入 | 删托盘信号与菜单 | 2.x | 托盘单测或静态断言菜单文案 |
| 无法启动新的文件转写任务 | 删 App 文件任务 API | 3.x | `hasattr`/`pytest.raises` 或测试改写为模块无符号 |
| 撤回后不破坏实时听写 | 保留热键路径；拆互斥 | 3.x + 5.x | `tests/test_app.py` 等回归 |
| 历史中的文件来源记录只读保留 | 不改 history_store；保留 UI 映射 | 5.x | 历史窗单测构造 file 记录 |
| 发布物不再捆绑解码器 | 删 build 拷贝与 third_party | 4.x | 改写/替换 `test_build_ffmpeg` |
| 产品不再提供轻翻译模式 | 设置+polisher 去 translate | 1.x | settings/polisher 测试 |
| 遗留 translate 配置静默回落 | Config 规范化 | 1.x | config 单测 |

## 已知风险与非目标

- [Risk] `app.py` 大块删除误伤听写 → Mitigation：纵向任务 + 听写回归命令必跑
- [Risk] 删除 `HISTORY_SOURCE_FILE` 常量时误删历史展示映射 → Mitigation：任务 5 显式保护 `history_window` 映射
- [Risk] `.gitignore` / README third_party 残留误导 → Mitigation：任务 4 清理挂钩与目录说明
- 非目标见上；不在本 change 内 archive 主规格

## Risks / Trade-offs

- 单 PR 改动文件多 vs 一致性：选一致性（方案 A）
- 配置是否写回 polish：读时规范化即可满足规格；写回为建议优化，不阻塞

## Migration Plan

1. 实施任务 1→5 顺序合入同一 change
2. 用户升级后旧 `translate` 配置自动按 polish 解释
3. 回滚：git revert；必要时恢复 ffmpeg 捆绑文档

## Open Questions

无阻塞项。非阻塞：是否从默认 config 字典删除 `target_language` 键（建议删除默认键与 UI，文件中残留键忽略）。

## 实现就绪审查（摘要）

见下方「就绪审查」；结论：**就绪**（阻塞项为零），待用户实现闸门放行。

---

# 实现就绪审查

## 结论

- 就绪
- 风险等级：`medium`
- 所需批准：实现闸门（用户单题放行）

## 阻塞项

（无）

## 警告项

- 完整安装包体积/无系统 ffmpeg 机器对比未自动化 → 接受：以 `build.py` 单元断言「不再拷贝」覆盖规格场景；手动打包可选
- OpenSpec `tasks` 机器模板偏简，本文件采用 delivery 纵向字段 + checkbox 双合规

## 建议项

- Config 规范化后可选 `save` 写回 `polish`，减少配置文件残留 `translate`

## 覆盖情况

| 需求/场景 | 技术计划 | 任务 | 验证 | 状态 |
|---|---|---|---|---|
| 无导入入口 / 无新文件任务 | 方案 A | 2–3 | pytest | 已规划 |
| 听写不破坏 | 方案 A | 3、5 | pytest 回归 | 已规划 |
| 历史只读 | 保留映射 | 5 | 历史单测 | 已规划 |
| 去 ffmpeg 捆绑 | 方案 A | 4 | build 测试改写 | 已规划 |
| 去翻译 + 回落 | Config/UI/polisher | 1 | config/polisher/settings | 已规划 |

## 代码事实新鲜度

- 分支：当前工作区含未提交 AV 功能实现
- 变基/重构检查：符号仍在磁盘与 Memory
- 缺失/改名路径：无
- 证据模式：full

## 并行安全

- 独立任务组：任务 1（翻译/配置）与任务 4（build/ffmpeg）文件重叠少，可并行；任务 2–3 共享 `app.py`/`tray` 建议串行
- 共享文件：`app.py`（任务 3 主责）、`settings_window.py`（任务 1）
- 所有权：见 tasks.md
- 集成顺序：1 → 2 → 3 → 4 → 5

## 后端结构验证

- 命令：`openspec validate remove-media-file-transcription`（design/tasks 写入后复跑）
- G1–G3/G5：唯一 change；tasks 含路径与验证；规格已批准；实现批准待用户

## 闸门记录

- 决定：设计/任务已就绪，等待实现放行
- 批准人/日期：（实现闸门待填）
- 附加约束：方案 A；开放问题 1–4 全 A

# 2026-10-08 可靠性优化：首批分析与修改

起点：`e8efbfb`（2.1.0 后的当前源码），分支 `codex/reliability-first-pass`。
范围：先处理可复现的核心链路问题，并建立可重复的 Windows 验证环境。

## 已确认的问题与处理

| 问题 | 原因与证据 | 本轮修改 |
| --- | --- | --- |
| 未完成转写可能在退出时被静默丢弃 | `voiceink/app.py` 的退出流程直接停止录音、关闭识别、取消润色；原先没有检查录音、排队、识别或输出状态 | 有未完成内容时先确认；默认返回，Esc / 关闭也返回；明确选择放弃才执行清理。返回后仍可结束监听、完成剩余片段，再正常退出 |
| 更新可能先启动安装程序，再丢弃待处理内容 | `_on_update_downloaded` 原先先调用安装程序，然后调用退出 | 先完成退出确认，再启动安装程序；拒绝退出或启动安装失败时保留应用。再次安装可复用缓存，但必须在后台重新核对整文件 SHA-256 与已知大小 |
| 润色漏检数字被删除 | `polish_rejection_reason` 原先只检查结果中新出现的数值；`_numbers` 丢弃零 | 检查原文数值是否丢失，保留零；继续允许时间补 `:00`、千位分隔符和小数末尾零等格式变化。不把这套规则当作完整语义校验 |
| macOS / Linux 粘贴命令失败仍可能显示已发送 | `text_paster._paste_shortcut` 原先忽略外部命令的非零退出码 | 失败抛出到已有降级路径，保留文字并报告已复制；Windows 仍用原有输入路径 |
| 低于发布下限的依赖环境可以继续构建 | 原本本机 sherpa-onnx 为 1.13.2，而 `requirements.txt` 要求 ≥1.13.8 | 新增 `voiceink_build/dependency_check.py`，构建在清理旧产物或停止已有程序前检查依赖；独立命令也可提前检查 |
| 测试与安装环境难以复现 | 依赖仅有下限，缺少 CI | 记录 Windows x64 / Python 3.10 的直接和传递依赖约束，增加 Windows Actions 工作流：安装固定版本 → pip check → 发布依赖检查 → 全量 pytest |

回归用例在修改前复现了 15 个失败；修复后的定向验证覆盖退出取消/放弃、更新启动顺序、数字删改、粘贴命令失败和构建检查顺序。
同时将实际向前台发送粘贴的旧测试改为隔离模拟，减少运行测试对真实剪贴板和目标应用的影响。

## 验证方式

独立虚拟环境位于系统临时目录，没有更改开发者现有 Python 环境。

```powershell
python -m pip install -r requirements-dev.txt -c constraints-windows-py310.txt
python -m pip check
python -m voiceink_build.dependency_check
$env:QT_QPA_PLATFORM = 'offscreen'
python -m pytest -p no:cacheprovider tests -q
```

环境：Python 3.10.11、PyQt6 6.11.0（Qt 6.11.2）、sherpa-onnx 1.13.8、pytest 9.1.1。
验证结果：

| 验证 | 结果 |
| --- | --- |
| 独立环境按固定约束安装 | 通过；直接与传递依赖版本与约束文件一致 |
| `pip check` / 发布依赖检查 | 通过 |
| 原生 `sherpa_onnx` 导入 | 通过，版本 1.13.8 |
| 修改后的定向回归（退出、保真、输出、更新、构建、README 流程） | 163 passed |
| 全量 `python -m pytest -p no:cacheprovider tests -q --tb=short` | **921 passed，129.03 秒** |
| 原有环境下的发布依赖检查 | 正确拒绝 sherpa-onnx 1.13.2，未进行构建或清理产物 |
| 原生退出确认窗深浅主题、默认/取消操作 | 通过；关闭对话框保留转写，默认和 Esc 均指向返回 |
| Git 差异格式检查 / CI YAML 结构检查 | 通过 |

退出确认窗的隔离原生渲染：[浅色](reliability-preview/exit-light.png)、[深色](reliability-preview/exit-dark.png)。
CI 文件已添加到本地，尚未推送或在远程运行。

工作流固定官方 Action 的提交，依据：
[checkout v7.0.1](https://github.com/actions/checkout/releases/tag/v7.0.1)、
[setup-python v7.0.0](https://github.com/actions/setup-python/releases/tag/v7.0.0)。

## 保留的不确定性与后续顺序

1. **真实音频与性能评测**：建立带人工文本的普通话、方言、中英混说、噪声及长句语料；测字符错误率、首段延迟、实时率、冷启动和内存。现有合成音频和模拟测试无法说明实际准确率。
2. **生命周期与存储**：本轮明确了用户退出时的丢弃选择；模型加载/推理无法即时中断、后台线程关闭、磁盘失败及限时历史写入仍需专项测试。原始音频目前没有持久化恢复能力。
3. **实际跨应用输入**：验证编辑器、浏览器、终端、只读控件和权限组合。“已发送”只表示发出快捷键，不能证明目标控件已经插入文本。
4. **桌面可用性**：覆盖首次使用、小屏、高缩放、多显示器、键盘和辅助技术。主工作台与听写条当前组织可沿用，先用真实任务验证瓶颈。
5. **会议方向与代码拆分**：说话人编号需要真人会议评估；中央编排、设置和历史大类适合在行为回归稳定后逐步拆分，不同时改写录音状态机与 UI。

本轮没有使用真实麦克风、调用在线润色服务、重建/安装发行包或验证多人会议效果。
固定依赖约束只针对 Windows x64 / Python 3.10，不固定模型文件与系统组件；其他平台继续使用通用依赖文件。

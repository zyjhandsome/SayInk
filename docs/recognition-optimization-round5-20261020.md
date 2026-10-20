# 第五轮：结束后逐段重新归属、待确认回退与 Silero 门限

前四轮都在在线匹配这一侧做加法（跨来源匹配、反合并、跨段上下文、短接话补充）。
本轮改的是三处结构性问题：一场结束时只按"编号质心"合并、拆不开也搬不动单句；
片段内换人分析一旦拿不准就整段待确认；Silero 0.5 门限把远场轻声整句切掉。
两个真实远场会议（AISHELL-4，人工逐句说话人标注）上，密集 6 人会议的 DER 从 62.1% 降到
32.0%，待确认时长从 33% 降到 0.9%；慢节奏 5 人会议从 19.3% 降到 7.7%，人数判对。

## 实现范围

- `sayink/speaker_clustering.py`（新）：`reassign_turns()`。对本场全部发言的声纹做平均连接层次
  聚类（numpy，无 scipy），**逐段**给出归属：不足 3 段且不足 8 秒的小簇并入最近的大簇，
  再按自动编号上限折叠；命名或更正过的编号认领各自时长最多的簇并保留编号，锁定段不移动。
  没有逐段证据（旧测试里不带 `sample_key` 的会话）时仍走原来的 `recluster()` 质心合并。
- `sayink/meeting_speakers.py`：`MeetingSpeakerSession.turns` 记录每段的键、编号、声纹和时长
  （含待确认与不可学习段），`confirm()` 同步更正；`reset()` 清空。
- `sayink/meeting_session.py::_apply_recluster`、`sayink/app.py::_finish_dictation_speakers`：
  会议 `finalize` 与持续转写收尾改为逐段写回（`MeetingStore.set_segment_speakers`、
  `HistoryStore.relabel_session_segments`），锁定行不改。
- `sayink/speaker_embedding.py`：新增 `cluster_threshold`（CAM++ 0.60、频谱 0.75），
  原 `recluster_threshold` 只给回退路径用。
- `sayink/speaker_turns.py`：换人分析得不出稳定单人结论（窗口不稳、分界未证实、局部检测
  `unresolved`）时，整段声纹仍交给会话做**强匹配**（阈值加余量、不新建编号、不更新档案）；
  检测到重叠或多人混合的部分照旧零向量待确认。
- `sayink/vad_segmenter.py`：`SILERO_THRESHOLD` 0.5 → 0.3；能量门限仍为底线。
- `tools/meeting_timeline_bench.py`：`--segmenter silero` 用会议实际断句；`--finalize on`
  另给出结束后逐段归属（`finalized`）的成绩；每个阶段附标准 DER（0.25 秒 collar、一对一映射）
  和按回合长度分组的归属正确数。`tools/textgrid_reference.py`（新）把 TextGrid 逐字稿转成
  `asr_bench.py` 的参考文本与 RTTM，公开语料可直接作为回归基线。

## 真实会议对照

数据：AISHELL-4 `M_R003S04C01`（37.6 分钟，6 人，577 个发言回合，密集抢话）和
`S_R004S03C01`（38.8 分钟，5 人，93 个回合，慢节奏），单通道远场录音，官方 RTTM 人工标注。
两段音频与标注留在仓库外的临时目录。评分：DER 按 0.25 秒 collar、匈牙利一对一映射；
"回合判对"按回合长度分组，回合内多数时长归到映射后的正确人即算对。
"之前"是本轮改动前的工作区代码（第四轮之后），同一台机器、同一模型、关闭多人检测。

| 会议 | 阶段 | 之前 | 本轮 |
| --- | --- | --- | --- |
| 密集 6 人 | 实时编号 | 8 个编号，DER 62.1%，待确认 33.0% | 8 个编号，DER 43.1%，待确认 19.8% |
| 密集 6 人 | 结束后 | 4 个编号（并掉两人），DER 63.6% | **7 个编号，DER 32.0%（错人 11.3%），待确认 0.9%，无并人无拆人** |
| 密集 6 人 | ≥3 秒回合判对 | 93 / 215 | **176 / 215** |
| 密集 6 人 | 1.6–3 秒回合判对 | 42 / 138 | 74 / 138 |
| 密集 6 人 | <0.8 秒回合判对 | 8 / 84 | 7 / 84 |
| 慢节奏 5 人 | 实时编号 | 7 个编号，DER 19.3%，待确认 8.4% | 6 个编号，DER 9.7%，待确认 2.5% |
| 慢节奏 5 人 | 结束后 | 7 个编号，DER 19.3% | **5 个编号，DER 7.7%（错人 0.1%），待确认 0.4%** |
| 慢节奏 5 人 | ≥3 秒回合判对 | 75 / 81 | 80 / 81 |

同一数据用仓库内工具复现（`--segmenter silero --finalize on`）：时长分数（无 collar、重叠单列）
密集会议单人时长判对率实时 62.2% → 结束后 74.4%，慢节奏会议 91.0% → 93.3%，结束后 5 个编号；
工具内置的 `der`（0.25 秒 collar）与上表一致，见下文"验证与复现"。

参数对照（缓存声纹上扫描）：聚类阈值 0.55 / 0.60 / 0.65 × 小簇"并入 / 保留 / 阈值外转待确认"，
0.60 + 并入在两场都最好或并列最好；保留小簇只多出几个零散编号。按真实人数强制聚类反而更差
（慢节奏会议 DER 30.5%），因此不做人数估计。

## 文字识别

Silero 0.3 只影响断句，不改模型。`M_R003S04C01` 前 10 分钟、官方逐字稿（含标注符号，绝对值偏高）：
Fun-ASR-Nano 分段 CER 35.3% → 31.3%，FireRedASR2-CTC 37.6% → 35.8%；非语音多出的时长每 40 分钟
不到两分钟。片段内换人切分对 CER 影响在 1 个百分点内（0.5 门限下 34.4% / 36.4%，0.3 门限下
`--speaker-turns auto` 31.8%）。以上数字已用仓库内 `tools/asr_bench.py` 复现并记入
[`asr-benchmark.md`](asr-benchmark.md) 的结果表。

## 没有改的

- 声音档案打分仍取 12 样本最大值。用两场会议缓存的声纹回放在线匹配，对比均值、中位数、
  前 3 均值、前半均值、最大+中位数、次大值六种打分：均值和中位数让密集场实时 DER 从 43.1%
  恶化到 56%（待确认升到 27–29%），其余在 ±1 个百分点内波动，没有一种在两场上同时更好；
  结束后逐段归属的结果与打分方式无关（全部 32.0% / 7.7%）。因此不改。在线阈值 0.55 /
  余量 0.08 / 连续性先验同样未动。
- 会议进行中不做周期性重聚类，仍只在结束时做一次；实时编号的错误要等结束后才修。
- 多人发言检测保持默认关闭：开启后两场 DER 都略差且耗时翻倍。
- 亚秒级接话（<0.8 秒）判对率没有变化（7–8 / 84）：这类回合大多被断句并进邻句，
  不是归属问题而是边界问题。第六轮量了帧级分割的上限（告知真实人数也只有 26 / 84），
  见 [第六轮记录](recognition-optimization-round6-20261020.md)。

## 验证与复现

新增测试：`tests/test_speaker_clustering.py`（层次聚类与暴力对照、小簇并入、保护编号、锁定段、
人数上限、会话 `turns`）；`test_speaker_turns.py`（待确认回退只接受强匹配、不开新编号、重叠仍为零向量）；
`test_meeting_store.py`（逐段写回跳过锁定行与他场）；`test_meeting_session.py`（有键会话逐段归属、
锁定行与编号不动）；`test_app_history_wiring.py`（持续转写收尾走逐段写回）；
`test_meeting_timeline_bench.py`（Silero 断句保留文件时间、`finalized` 评分）。
`test_textgrid_reference.py`（TextGrid 转参考文本 / RTTM）；`test_meeting_timeline_bench.py` 另覆盖
DER 的映射、collar、待确认计漏检、重叠与拆人处理。
完整回归 2100 项通过（`QT_QPA_PLATFORM=offscreen`，约 5 分钟）；Ruff 通过。

仓库内工具在同一数据上的复现与本文数字一致（DER 实时 43.1% / 9.7%，结束后 32.0% / 7.7%；
回合判对 ≥3 秒 176/215、80/81）：

```powershell
# 真实会议时间线 + DER（需 CAM++ 与 Silero 模型已下载；manifest 指向 wav + RTTM）
python tools/meeting_timeline_bench.py <dir>/manifest.json --segmenter silero --finalize on --out <dir>/timeline.json
# 参考文本：TextGrid → name.txt（前 10 分钟），再跑 ASR 基线
python tools/textgrid_reference.py <dir>/M_R003S04C01.TextGrid --until 600 --out <bench>/M_R003S04C01.txt
python tools/asr_bench.py --data <bench> --models funasr-nano fireredasr2-ctc --speaker-turns off
```

# 会议识别评测基线

`tools/asr_bench.py` 在本机离线跑完一组会议录音，给出每个模型的字错率（CER）、
热词命中率与实时率（RTF）。它是 `docs/meeting-notes-design.md` §5.8「准确率改进」里
所有模型/参数决策的依据：先有数字，再换模型。

## 怎么跑

1. 准备一个目录（不要放进仓库），每段录音一对文件：

   ```
   bench/
     standup-01.wav     # 16 kHz 单声道 16-bit PCM
     standup-01.txt     # 人工校对的参考文本
     review-02.wav
     review-02.txt
     glossary.txt       # 可选：每行一个术语，用作热词与命中率统计；`词=误写1、误写2` 同时做误写替换
   ```

   建议至少 30 分钟音频，覆盖：线下麦克风、线上混合音频、多人抢话、带英文缩写的段落。

   公开语料（如 AISHELL-4）的逐字稿是 Praat TextGrid，每个说话人一个 tier。
   `tools/textgrid_reference.py` 把它转成上面的 `name.txt`（全部 tier 按时间拼接、去掉
   `<sil>` 等标记；重叠时每个人的字都计入，所以远场 CER 是任何单一说话人错误的上界），
   `--until 600` 只保留前 10 分钟，配合从文件开头截出的 wav 用于短程对照；`--rttm` 顺带写出
   `meeting_timeline_bench.py` 需要的说话人标注：

   ```powershell
   py -3.10 tools/textgrid_reference.py D:\bench\M_R003S04C01.TextGrid --until 600 --out D:\bench\M_R003S04C01.txt
   ```

2. 在仓库根目录执行（模型需已在应用「引擎」页下载）：

   ```powershell
   py -3.10 tools/asr_bench.py --data D:\bench --language zh
   py -3.10 tools/asr_bench.py --data D:\bench --models funasr-nano --vad energy --hotwords "SayInk,CAM++"
   py -3.10 tools/asr_bench.py --data D:\bench --out D:\bench\results.json
   py -3.10 tools/asr_bench.py --data D:\bench --no-short-retry --out D:\bench\without-retry.json
   py -3.10 tools/asr_bench.py --data D:\bench --speaker-turns off --out D:\bench\without-turns.json
   ```

   `--vad` 默认 `silero`：先按**会议**停顿切段（静音 1.5 秒、最长 15 秒，与开会相同），再逐段解码。`energy` 用能量门限，用来对照 Silero 有没有收益。`off` 整段一次解码，测的不是线上链路，**不要写入下表**。Silero 模型没下载时会退回能量门限，输出里会写明实际用的门。

   wav 可以不是 16 kHz，脚本会重采样到 16 kHz。RTF 计换人分析、解码和短句重试，不计 VAD 切段。

   `--speaker-turns auto` 为默认：CAM++ 能实际加载时，像会议一样在 VAD 片段内检测换人，
   再分别转写。`off` 用于同一批音频的关闭对照，`campplus` 要求成功加载，否则报错。
   终端与 JSON 记录实际使用的检测后端；阈值尚待标定，必须检查分段前后的 CER 是否变差，
   不能仅凭说话人更清晰就判断文字更准确。

   2026-10-20 起默认共用会议的短句空结果重试：首次没有文字且音频不超过 8 秒时，
   有至少约 100 ms 非静音才保留语音边界并补静音重试一次。有效的首次文字不改写，
   不对纯静音或很轻的底噪重试；重试失败保留首次空结果。重试耗时计入 RTF。
   使用 `--no-short-retry` 跑同一组录音作对照，JSON 同时记录开关、空片段数和空片段率。
   空片段率下降仍须结合 CER 检查，不能把噪音幻觉当作收益。

   `--llm-correct --llm-url <地址> --llm-model <模型>` 在每段识别后再跑应用的 LLM 纠错
   （同一提示词、同一接受门槛、上一段作上文），表格多一列「LLM 纠错后 CER」，并打印每个文件
   改动 / 拒绝 / 错误段数与中位延迟。密钥从环境变量 `SAYINK_LLM_API_KEY` 读取，不出现在命令行
   或日志里；本机 / 局域网端点可不设。这会产生真实的 API 调用与费用（10 分钟音频约 1.4 万 token）。

3. 把输出的 Markdown 表格贴到下面的「结果」小节，注明日期、机器、音频来源，以及 `--vad` 实际用的门（`silero` 或回退后的 `energy`）。合成语音、`--vad off` 的结果不贴进这张表。

## 指标口径

| 指标 | 定义 |
|---|---|
| CER | 去掉空白与标点、统一小写后的字级编辑距离 ÷ 参考字数，按文件平均 |
| 热词命中率 | 参考文本里出现的术语次数中，识别结果也出现的比例（按出现次数计，不分大小写） |
| 术语写法正确率 | 同上，但识别结果必须和术语表写法完全一致（`GITHUB` 不算 `GitHub`）。识别结果先按术语表恢复英文大小写，与应用一致 |
| RTF | 换人分析、解码与重试耗时 ÷ 音频时长；项目目标 ≤ 0.3，为持续会议留出处理余量 |
| 载入 | 冷启动创建识别器的秒数，决定开会前要等多久 |

## 结果

| 日期 | 机器 | 音频 | 模型 | 热词 | VAD | CER | 热词命中率 | 术语写法正确率 | RTF | 载入 (s) |
|---|---|---|---|---|---|---|---|---|---|---|
| 2026-10-20 | Core Ultra 9 185H，CPU 4 线程 | AISHELL-4 `M_R003S04C01` 第 0 通道前 10 分钟（远场阵列单通道，6 人，8.9% 重叠，参考 2812 字） | funasr-nano | 无 | silero（门限 0.3，`--speaker-turns off`） | 31.33% | — | — | 0.25 | 10.7 |
| 2026-10-20 | 同上 | 同上 | fireredasr2-ctc | 无 | silero（门限 0.3，`--speaker-turns off`） | 35.81% | — | — | 0.33 | 2.3 |
| 2026-10-20 | 同上 | 同上 | funasr-nano | 无 | silero（门限 0.3，`--speaker-turns auto`=campplus） | 31.76% | — | — | 0.32 | 7.2 |
| 2026-10-21 | 同上 | 同上 | funasr-nano | 无 | silero（门限 0.25，当前默认，`--speaker-turns off`） | 30.97% | — | — | 0.28 | 8.9 |
| 2026-10-21 | 同上 | 同上 | fireredasr2-ctc | 无 | silero（门限 0.25，当前默认，`--speaker-turns off`） | 34.14% | — | — | 0.39 | 2.9 |
| 2026-10-21 | 同上 | 同上 | funasr-nano | 3 条（赠送=增送 / 新盘=训盘 / 五证=五中、五四证） | silero（门限 0.25，`--speaker-turns off`） | 31.86%（LLM 纠错后 31.79%） | 60.0% | 60.0% | 0.26 | 8.0 |
| 2026-10-21 | 同上 | 同上 | fireredasr2-ctc | 同上（不支持热词，只做误写替换） | silero（门限 0.25，`--speaker-turns off`） | 34.17%（LLM 纠错后 34.00%） | 30.0% | 30.0% | 0.30 | 2.4 |

远场、多人、重叠且参考文本计入所有人的字，这组绝对值偏高，只用于同一音频上的相对比较。
最后两行用 `--llm-correct`（DeepSeek `deepseek-flash`，关闭思考）跑应用的纠错链路：改动 4 / 8 段、
拒绝 2 / 3 段，中位延迟 0.75 / 0.90 s。注意 funasr-nano **带热词后 CER 反而高 0.9 个百分点**：热词
改变的是整段解码（多出口语词与标点变化，231 处差异），不只是术语；参考稿不含口语词，所以全算错。
热词列表要短，只放真正常错的词。细节见 [`recognition-optimization-round7-20261021.md`](recognition-optimization-round7-20261021.md)。
同一段音频在 Silero 门限 0.5 时 funasr-nano 为 35.3%、fireredasr2-ctc 为 37.6%（仓库外脚本，第五轮前）；
把门限降到 0.3 是两款模型上唯一都有效的改法，片段内换人切分对 CER 的影响在 1 个百分点内。
第六轮把门限再降到 0.25（主要为了说话人漏检），两款模型的 CER 都没有变差；0.2 时 funasr-nano
反而升到 32.9%，多吐出的房间噪声开始被识别成字。
通用降噪（GTCRN）在该音频上为负收益（35.5%），不建议用于远场。

## 模型矩阵覆盖状态与复现配方

当前仓库共在 `sayink/speech_recognizer.py`（`MODEL_REGISTRY`）中注册 **9 款离线识别模型**。上表仅录入已在真实远场会议语料上跑完核对的实测数据；尚未在同一真实语料跑完的模型保持「待实测」，**严禁填入估算值、公开榜单转抄值或 TTS 合成语音结果**。

| 模型 ID | 界面名称 | 语种范围 | 热词注入 (`hotwords`) | 语言锁定 (`language`) | 长音频滑窗 | 当前基线状态 |
|---|---|---|---|---|---|---|
| `funasr-nano` | Fun-ASR-Nano | 中/英/日/方言 | 支持 | 支持 | 15 s 滑窗 | 已实测（当前默认） |
| `sensevoice` | SenseVoice | 中/英/日/韩/粤 | 不支持（仅术语表后置替换） | 支持 | 整段解码 | 待实测 |
| `qwen3-asr-0.6b` | Qwen3-ASR 0.6B | 中/英/多语种 | 支持 | 不支持 | 15 s 滑窗 | 待实测 |
| `fireredasr2-ctc` | FireRedASR2 | 中/英/方言 | 不支持（仅术语表后置替换） | 不支持 | 整段解码 | 已实测（候选对照） |
| `fireredasr2-aed` | FireRedASR2 AED | 中/英/方言 | 不支持（仅术语表后置替换） | 不支持 | 整段解码 | 待实测 |
| `paraformer-zh` | Paraformer 中文 | 中/英 | 不支持（仅术语表后置替换） | 不支持 | 整段解码 | 待实测 |
| `paraformer-trilingual` | Paraformer 三语 | 中/英/粤 | 不支持（仅术语表后置替换） | 不支持 | 整段解码 | 待实测 |
| `zipformer-ctc-zh` | Zipformer CTC | 纯中文 | 不支持（仅术语表后置替换） | 不支持 | 整段解码 | 待实测 |
| `qwen3-asr-1.7b` | Qwen3-ASR 1.7B | 中/英/多语种 | 支持 | 不支持 | 15 s 滑窗 | 待实测 |

### 复现全部 9 款已注册模型的评测命令

在应用「引擎」页下载所需模型（及辅助模型 Silero VAD、CAM++）后，可在同一真实会议目录上一次性对比全部 9 款模型，并分别跑 `--vad silero` 与 `--vad energy` 对照：

```powershell
# 1) 默认会议链路（Silero VAD + CAM++ 换人切分 + 短句空结果重试）跑全部 9 款模型
py -3.10 tools/asr_bench.py --data D:\bench `
  --models funasr-nano sensevoice qwen3-asr-0.6b fireredasr2-ctc fireredasr2-aed paraformer-zh paraformer-trilingual zipformer-ctc-zh qwen3-asr-1.7b `
  --language zh --vad silero --speaker-turns auto --out D:\bench\matrix-silero.json

# 2) 同一批音频用能量门限对照，确认 Silero 在各模型上的真实收益
py -3.10 tools/asr_bench.py --data D:\bench `
  --models funasr-nano sensevoice qwen3-asr-0.6b fireredasr2-ctc fireredasr2-aed paraformer-zh paraformer-trilingual zipformer-ctc-zh qwen3-asr-1.7b `
  --language zh --vad energy --speaker-turns off --out D:\bench\matrix-energy.json
```

> 说明：不传 `--models` 时，`tools/asr_bench.py` 会自动遍历本机已下载的全部已注册模型；仅 `funasr-nano`、`qwen3-asr-0.6b`、`qwen3-asr-1.7b` 会向解码器注入热词提示，其余模型的「热词命中率 / 术语写法正确率」反映的是无解码器热词下配合术语表大小写恢复与误写替换（`词=误写1、误写2`）的真实表现。

### 双通道（麦克风 + 电脑系统环回）录音评测配方

上面的 AISHELL-4 基线测的是**单通道远场阵列录音**，不代表佩戴耳机/外放开线上会议时的 `microphone + system` 双路采集链路（双路音量平衡、回声重复抑制与分路断句）。双通道场景目前尚未录入实测基线，按以下可复现实验与回放工具分层验证：

1. **受控双轨重叠对比（单声道混音 vs 双路独立切段）**：准备至少两场带人工校对 `.TextGrid` 与 16 kHz 单声道 `.ch0.wav` 的录音，运行 `tools/lane_asr_eval.py` 对比混音解码（`mixed_cer_best_order`、`mixed_rtf`）与麦克风/系统声分路解码（`separate_weighted_cer`、`separate_rtf`）：

   ```powershell
   py -3.10 tools/lane_asr_eval.py --data D:\bench --model funasr-nano --out D:\bench\lane-overlap-funasr.json
   ```

   注意该脚本仅评估受控双轨语音重叠，不等同于真实声卡环回硬件延迟与扬声器串音。

2. **真实双路（麦克风 + 系统环回）会议录音评测**：
   - **有人工校对逐字稿与说话人时间轴（RTTM）时**：将真实混合录音（及分路导出的 16 kHz 单声道 `.wav`）配对人工校对 `.txt` 放入评测目录跑 `tools/asr_bench.py` 获取 CER / RTF，并用 `tools/meeting_timeline_bench.py` 评估说话人时间轴归属与 DER：

     ```powershell
     py -3.10 tools/asr_bench.py --data D:\bench\dual-lane --language zh --vad silero --out D:\bench\dual-lane-asr.json
     py -3.10 tools/meeting_timeline_bench.py --manifest D:\bench\dual-lane\manifest.json --segmenter silero --out D:\bench\dual-lane-timeline.json
     ```

   - **仅有本机真实录音、尚无人工逐字真值时**：使用 `tools/recognition_replay.py` 生成本地试听核对页（对比仅按停顿、说话人分析、多人检测三种模式），人工逐段核对前不计算 CER/DER：

     ```powershell
     py -3.10 tools/recognition_replay.py --audio D:\bench\dual-lane\sample.wav --out D:\bench\replay.json --review-html D:\bench\review.html
     ```

## 决策记录

- 默认会议模型保持 Fun-ASR-Nano，直到某个候选在同一组数据上 CER 低 ≥ 15% 且 RTF ≤ 0.3。
- FireRedASR2 CTC 为候选而非推荐（见设计文档 §5.8）；评测通过前不进入默认配置。
- 热词只在 Fun-ASR-Nano / Qwen3-ASR 上生效；SenseVoice 的「命中率」列反映的是无热词基线。
- 同一批录音要各跑一行 `--vad silero` 和一行 `--vad energy`，VAD 列写实际用的门。这样才知道 Silero 有没有收益。

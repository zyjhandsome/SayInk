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
     glossary.txt       # 可选：每行一个术语，用作热词与命中率统计
   ```

   建议至少 30 分钟音频，覆盖：线下麦克风、线上混合音频、多人抢话、带英文缩写的段落。

2. 在仓库根目录执行（模型需已在应用「引擎」页下载）：

   ```powershell
   py -3.10 tools/asr_bench.py --data D:\bench --language zh
   py -3.10 tools/asr_bench.py --data D:\bench --models funasr-nano qwen3-asr-0.6b --hotwords "SayInk,CAM++"
   py -3.10 tools/asr_bench.py --data D:\bench --out D:\bench\results.json
   ```

3. 把输出的 Markdown 表格贴到下面的「结果」小节，注明日期、机器与音频来源。

## 指标口径

| 指标 | 定义 |
|---|---|
| CER | 去掉空白与标点、统一小写后的字级编辑距离 ÷ 参考字数，按文件平均 |
| 热词命中率 | 参考文本里出现的术语次数中，识别结果也出现的比例（按出现次数计） |
| RTF | 解码耗时 ÷ 音频时长；< 0.3 才能在 90 分钟会议里不积压 |
| 载入 | 冷启动创建识别器的秒数，决定开会前要等多久 |

## 结果

> 尚无数据。首次填入时请保留本提示下方的表头。

| 日期 | 机器 | 音频 | 模型 | 热词 | CER | 热词命中率 | RTF | 载入 (s) |
|---|---|---|---|---|---|---|---|---|

## 决策记录

- 默认会议模型保持 Fun-ASR-Nano，直到某个候选在同一组数据上 CER 低 ≥ 15% 且 RTF ≤ 0.3。
- FireRedASR2 CTC 为候选而非推荐（见设计文档 §5.8）；评测通过前不进入默认配置。
- 热词只在 Fun-ASR-Nano / Qwen3-ASR 上生效；SenseVoice 的「命中率」列反映的是无热词基线。

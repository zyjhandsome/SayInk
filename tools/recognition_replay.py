"""Replay local WAV audio through the app worker without inventing reference labels.

Example: python tools/recognition_replay.py --audio clip.wav --out replay.json
  --review-html review.html --source-media original.mp4
The optional HTML uses local audio only. Predictions and review notes remain
unverified; this tool does not report CER, DER or speaker accuracy.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
import time
import wave

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sayink.audio_utils import TARGET_SAMPLE_RATE
from sayink.meeting_speakers import MeetingSpeakerSession
from sayink.recognition_settings import aux_model_path
from sayink.speaker_embedding import make_embedder
from sayink.speaker_turns import SpeakerTurn, SpeakerTurnDetector
from sayink.speech_recognizer import AUX_MODEL_SPEAKER, TranscribeWorker, _create_recognizer, language_argument
from tools.asr_bench import load_wav, segment_meeting_timed

MODES = ("pause_only", "speaker", "activity")


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def replay(audio, rate, recognizer, embedder, *, mode, vad="silero", contextual=True):
    if mode not in MODES:
        raise ValueError("unknown replay mode")
    chunks, used = segment_meeting_timed(audio, rate, vad=vad)
    session = MeetingSpeakerSession.for_embedder(embedder)
    samples_per_ms = TARGET_SAMPLE_RATE // 1000
    rows, actions = [], Counter()
    tick = time.perf_counter()
    for chunk in chunks:
        detector = SpeakerTurnDetector(embedder, context=session.turn_context if contextual else None,
                                        start_ms=int(chunk.start * 1000 / TARGET_SAMPLE_RATE), route="replay")
        analyzer = detector
        if mode == "pause_only":
            def analyzer(clip, **kwargs):
                return [SpeakerTurn(0, len(clip), np.zeros(1, dtype=np.float32), False)]
        batch, errors = [], []
        worker = TranscribeWorker(recognizer, chunk.audio, retry_empty=True, turn_analyzer=analyzer)
        worker.meeting_ready.connect(batch.extend)
        worker.error.connect(errors.append)
        worker.run()
        if errors:
            raise ValueError("; ".join(errors))
        if (not batch or batch[0].start != 0 or batch[-1].stop != len(chunk.audio)
                or any(a.stop != b.start for a, b in zip(batch, batch[1:], strict=False))):
            raise ValueError("worker did not preserve the full VAD chunk")
        actions[detector.context_action] += 1
        for turn in batch:
            start, stop = int(chunk.start + turn.start), int(chunk.start + turn.stop)
            speaker = session.assign(turn.embedding, start_ms=start // samples_per_ms, end_ms=stop // samples_per_ms,
                                     sample_key=len(rows), learnable=turn.learnable)
            rows.append({"start_sample": start, "stop_sample": stop, "start_s": start / TARGET_SAMPLE_RATE,
                         "stop_s": stop / TARGET_SAMPLE_RATE, "speaker": speaker,
                         "learnable": bool(turn.learnable), "text": turn.text})
    text = "".join(row["text"] for row in rows)
    total = sum(len(chunk.audio) for chunk in chunks)
    return {"mode": mode, "vad_used": used, "speaker_backend": embedder.name,
            "activity_backend": "off" if mode != "activity" else detector.activity_backend if chunks else "off",
            "contextual": contextual and mode != "pause_only", "context_actions": dict(actions),
            "speaker_groups": session.speaker_count, "profile_samples": len(session.history),
            "vad_chunks": len(chunks), "turns": len(rows), "unknown_turns": sum(row["speaker"] == 0 for row in rows),
            "assigned_audio_fraction": (sum(row["stop_sample"] - row["start_sample"] for row in rows if row["speaker"]) / total
                                        if total and mode != "pause_only" else None),
            "characters": len(text), "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "processing_s": time.perf_counter() - tick, "segments": rows}


def write_review(report, path):
    payload = json.dumps(report, ensure_ascii=False).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    audio_uri = Path(report["input"]["audio"]).resolve().as_uri()
    import html

    document = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>语音回放核对</title><style>
body{font:15px/1.6 system-ui,"Microsoft YaHei",sans-serif;color:#202c35;background:#f4f5f2;margin:0}
main{max-width:1180px;margin:36px auto;padding:0 24px}h1{font-size:28px;margin-bottom:8px}.hint{color:#58666b}
.player{background:white;border:1px solid #d9dfda;border-radius:12px;padding:20px;position:sticky;top:8px}
audio{width:100%;margin:10px 0}select,button,textarea{font:inherit}select,button{padding:6px 12px;border:1px solid #b5c2ba;border-radius:6px;background:white}
button{cursor:pointer}table{width:100%;border-collapse:collapse;background:white;margin:22px 0}th,td{padding:12px;text-align:left;border-bottom:1px solid #dce2dd;vertical-align:top}
th{font-size:13px;color:#5d6963}td:nth-child(1){width:145px;white-space:nowrap}td:nth-child(2){width:95px}td:nth-child(4){width:220px}
textarea{box-sizing:border-box;width:100%;min-height:80px;border:1px solid #d4ddd6;border-radius:6px;padding:8px;resize:vertical}
@media(max-width:800px){main{padding:0 12px}.player{position:static}td:nth-child(4),th:nth-child(4){display:none}td,th{padding:8px}}
</style><main><h1>语音回放核对</h1><p class="hint">模型文字和人物编号均未核对。字幕可以辅助检查，不能自动视为逐字真值。</p>
<div class="player"><label>对照模式 <select id="mode"></select></label> <button id="export">导出备注</button>
<audio id="audio" controls preload="metadata" src="AUDIO_URI"></audio><div id="summary" class="hint"></div></div>
<table><thead><tr><th>原音频时间</th><th>模型人物</th><th>模型文字（未核对）</th><th>核对备注</th></tr></thead><tbody id="rows"></tbody></table>
<p class="hint">点击时间播放该段。备注保存在当前页面内存中，关闭前可导出；导出内容仍标为未核对，程序不会据此计算准确率。</p></main>
<script id="data" type="application/json">PAYLOAD</script><script>
const report=JSON.parse(document.getElementById('data').textContent),notes={};
const mode=document.getElementById('mode'),player=document.getElementById('audio');let stopAt=null;
const labels={pause_only:'仅按停顿转写',speaker:'说话人分析',activity:'说话人分析 + 多人检测'};
report.runs.forEach((r,i)=>{let o=document.createElement('option');o.value=i;o.textContent=r.label||labels[r.mode]||r.mode;mode.append(o)});
mode.value=Math.max(0,report.runs.findIndex(r=>r.mode==='speaker'));
function render(){const r=report.runs[mode.value],body=document.getElementById('rows');body.replaceChildren();
document.getElementById('summary').textContent=`${r.turns} 段 · ${r.speaker_groups} 个模型人物组 · ${r.unknown_turns} 段待确认 · 仅供排查，不代表准确率`;
r.segments.forEach((s,i)=>{let row=document.createElement('tr'),time=document.createElement('td'),b=document.createElement('button');
b.textContent=`${s.start_s.toFixed(2)}–${s.stop_s.toFixed(2)} 秒`;b.onclick=()=>{player.currentTime=s.start_s;stopAt=s.stop_s;player.play().catch(()=>{})};time.append(b);row.append(time);
let who=document.createElement('td');who.textContent=r.mode==='pause_only'?'未启用':s.speaker?`说话人 ${s.speaker}`:'待确认';row.append(who);
let text=document.createElement('td');text.textContent=s.text;row.append(text);let cell=document.createElement('td'),input=document.createElement('textarea'),key=`${mode.value}:${i}`;
input.placeholder='记录错字、接话或人物归属问题';input.value=notes[key]||'';input.oninput=()=>notes[key]=input.value;cell.append(input);row.append(cell);body.append(row)})}
player.addEventListener('timeupdate',()=>{if(stopAt!==null&&player.currentTime>=stopAt){player.pause();stopAt=null}});mode.onchange=render;
document.getElementById('export').onclick=()=>{let blob=new Blob([JSON.stringify({...report,review_notes:notes,reference_status:'unreviewed'},null,2)],{type:'application/json'});
let a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='recognition-review-notes.json';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000)};render();
</script></html>'''
    path.write_text(document.replace("AUDIO_URI", html.escape(audio_uri, quote=True)).replace("PAYLOAD", payload), encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--source-media", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--review-html", type=Path)
    parser.add_argument("--modes", nargs="+", choices=MODES, default=list(MODES))
    parser.add_argument("--context", choices=("on", "off"), default="on")
    parser.add_argument("--vad", choices=("energy", "silero"), default="silero")
    parser.add_argument("--model", default="funasr-nano")
    parser.add_argument("--threads", type=int, default=4)
    args = parser.parse_args(argv)
    try:
        if args.threads <= 0:
            raise ValueError("threads must be positive")
        with wave.open(str(args.audio), "rb") as stream:
            seconds = stream.getnframes() / stream.getframerate()
            if not 0 < seconds <= 600:
                raise ValueError("use a nonempty WAV clip of at most ten minutes")
        audio, rate = load_wav(args.audio)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        if args.review_html:
            args.review_html.parent.mkdir(parents=True, exist_ok=True)
        report = {"schema_version": 1, "reference_status": "unreviewed", "completed": False, "recognizer": args.model,
                  "input": {"audio": str(args.audio.resolve()), "audio_sha256": file_hash(args.audio), "seconds": seconds},
                  "runs": []}
        if args.source_media:
            report["input"].update(source_media=str(args.source_media.resolve()), source_sha256=file_hash(args.source_media))
        recognizer = _create_recognizer(args.model, args.threads, language=language_argument(args.model, "zh"))
        for mode in args.modes:
            embedder = make_embedder(aux_model_path(AUX_MODEL_SPEAKER), activity_enabled=mode == "activity")
            if mode != "pause_only" and not embedder.supports_turn_detection:
                raise ValueError("CAM++ must be loaded for speaker replay")
            result = replay(audio, rate, recognizer, embedder, mode=mode, vad=args.vad, contextual=args.context == "on")
            report["runs"].append(result)
            args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({k: v for k, v in result.items() if k != "segments"}, ensure_ascii=False), flush=True)
        report["completed"] = True
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        if args.review_html:
            write_review(report, args.review_html)
    except (OSError, ValueError, TypeError, KeyError, wave.Error) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

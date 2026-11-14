"""User-facing recording instructions derived from the saved configuration."""

from dataclasses import dataclass

from sayink.audio_devices import INPUT_SOURCE_LABELS
from sayink.config import DEFAULT_HOTKEY, TRIGGER_MODE_CONTINUOUS, format_hotkey


@dataclass(frozen=True)
class DictationHints:
    source: str
    mode: str
    output: str
    shortcut: str
    detail: str
    output_warning: bool = False


def dictation_hints(config) -> DictationHints:
    source_key = config.get("audio.input_source", "microphone")
    source = INPUT_SOURCE_LABELS.get(source_key, "麦克风")
    continuous = config.get("audio.trigger_mode") == TRIGGER_MODE_CONTINUOUS
    mode = "持续转写" if continuous else "单次转写"
    history_only = config.history_only_output()
    warning = history_only and not config.get("history.enabled", True)
    # Same words as the 结果去向 control, so the sidebar and the setting do not disagree.
    output = "未保存 · 历史已关闭" if warning else "仅保存到历史" if history_only else "输入当前光标"
    destination = "保存到历史" if history_only else "输入当前光标"
    raw_hotkey = str(config.get("hotkey", DEFAULT_HOTKEY) or "").strip()
    hotkey = format_hotkey(raw_hotkey) if raw_hotkey else ""
    if continuous:
        esc_stops = config.get("audio.esc_stops_continuous", True)
        stop = (
            "Esc 或转写条「结束」结束录音，已录内容继续识别；"
            if esc_stops else "点转写条「结束」结束录音，已录内容继续识别；"
        )
        output_clause = "；历史已关闭，结果不会保存或输入" if warning else f"并{destination}"
        if not hotkey:
            detail = (
                f"尚未绑定快捷键（前往 设置 → 通用 绑定）；开启后停顿即识别{output_clause}。"
                f"{stop}说话时可点转写条「放弃本句」丢弃当前句。"
            )
            shortcut = "快捷键未绑定\n前往 设置 → 通用 绑定"
        else:
            detail = (
                f"按住快捷键启动，松开后继续录制；停顿后识别{output_clause}，不用一直按住。"
                f"{stop}说话时可点转写条「放弃本句」丢弃当前句。"
            )
            shortcut = f"按住 {hotkey}\n松开后继续录制\n" + (
                "Esc 结束并保留" if esc_stops else "点「结束」并保留"
            )
    else:
        output_clause = "；历史已关闭，结果不会保存或输入" if warning else f"并{destination}"
        if not hotkey:
            detail = f"尚未绑定快捷键（前往 设置 → 通用 绑定）；绑定后按住录音、松开识别{output_clause}。"
            shortcut = "快捷键未绑定\n前往 设置 → 通用 绑定"
        else:
            detail = f"按住快捷键录音，松开后识别{output_clause}；Esc 或转写条「取消」取消本次录音，不保留本次内容。"
            shortcut = f"按住 {hotkey}，松开识别\nEsc 取消本次录音"
    return DictationHints(source, mode, output, shortcut, detail, warning)

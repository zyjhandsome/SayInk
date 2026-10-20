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
    warning = history_only and not config.get("history.enabled", False)
    output = "未保存 · 历史已关闭" if warning else "仅记录历史" if history_only else "粘贴到光标"
    destination = "记录到历史" if history_only else "粘贴到光标处"
    hotkey = format_hotkey(config.get("hotkey", DEFAULT_HOTKEY))
    if continuous:
        esc_stops = config.get("audio.esc_stops_continuous", True)
        stop = (
            "Esc 或转写条「结束」结束录音，已录内容继续识别。"
            if esc_stops else "点转写条「结束」结束录音，已录内容继续识别。"
        )
        output_clause = "；历史已关闭，结果不会保存或粘贴" if warning else f"并{destination}"
        detail = f"按住快捷键启动，松开后继续录制；停顿后识别{output_clause}，不用一直按住。{stop}"
        shortcut = f"按住 {hotkey}\n松开后继续录制\n" + (
            "Esc 结束并保留" if esc_stops else "点「结束」并保留"
        )
    else:
        output_clause = "；历史已关闭，结果不会保存或粘贴" if warning else f"并{destination}"
        detail = f"按住快捷键录音，松开后识别{output_clause}；Esc 取消本次录音，不保留本次内容。"
        shortcut = f"按住 {hotkey}\n松开识别\nEsc 取消本次录音"
    return DictationHints(source, mode, output, shortcut, detail, warning)

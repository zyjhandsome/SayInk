"""Enumerate and resolve microphone / system (loopback) capture devices."""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from typing import Any, Optional

import sounddevice as sd

log = logging.getLogger("SayInk")

INPUT_SOURCE_MICROPHONE = "microphone"
INPUT_SOURCE_SYSTEM = "system"
INPUT_SOURCE_MIXED = "mixed"
INPUT_SOURCES = (INPUT_SOURCE_MICROPHONE, INPUT_SOURCE_SYSTEM, INPUT_SOURCE_MIXED)

_SYSTEM_NAME_HINTS = (
    "loopback",
    "stereo mix",
    "stereomix",
    "立体声混音",
    "what u hear",
    "monitor",
    "blackhole",
    "soundflower",
    "vb-audio",
    "virtual cable",
    "cable output",
    "wave link",
)

# 自动选择时降低优先级（仍可手动选）：部分 App 虚拟扬声器不支持 WASAPI 环回
_LOW_PRIORITY_AUTO_HINTS = (
    "网易",
    "netease",
    "映射器",
    "mapper",
    "steam",
    "obs virtual",
)

_HIGH_PRIORITY_AUTO_HINTS = (
    "stereo mix",
    "stereomix",
    "立体声混音",
    "cable",
    "loopback",
    "monitor",
    "blackhole",
    "vb-audio",
    "realtek",
    "speaker",
    "扬声器",
    "headphone",
    "耳机",
    "display audio",
)


@dataclass(frozen=True)
class AudioDeviceInfo:
    index: int
    name: str
    hostapi: str
    sample_rate: int
    channels: int
    is_loopback: bool
    is_output: bool

    @property
    def label(self) -> str:
        suffix = " [系统回放]" if self.is_loopback or self.is_output else ""
        return f"{self.name}{suffix}"


def _hostapi_name(hostapi_index: int) -> str:
    try:
        apis = sd.query_hostapis()
        return apis[hostapi_index]["name"]
    except Exception:
        return ""


def _wasapi_hostapi_index() -> Optional[int]:
    for i, api in enumerate(sd.query_hostapis()):
        if "wasapi" in api["name"].lower():
            return i
    return None


def _supports_wasapi_loopback_flag() -> bool:
    try:
        sd.WasapiSettings(loopback=True)
        return True
    except TypeError:
        return False


def _looks_like_system_capture(name: str) -> bool:
    lower = name.lower()
    return any(h in lower for h in _SYSTEM_NAME_HINTS)


def _device_channels(info: dict[str, Any], want_input: bool) -> int:
    key = "max_input_channels" if want_input else "max_output_channels"
    return int(info.get(key, 0) or 0)


def _make_device_info(index: int, info: dict[str, Any], *, loopback: bool, is_output: bool) -> Optional[AudioDeviceInfo]:
    in_ch = _device_channels(info, True)
    out_ch = _device_channels(info, False)
    if loopback:
        channels = out_ch or in_ch
    else:
        channels = in_ch
    if channels <= 0:
        return None
    sr = int(info.get("default_samplerate") or 48000)
    return AudioDeviceInfo(
        index=index,
        name=str(info.get("name", f"Device {index}")),
        hostapi=_hostapi_name(int(info.get("hostapi", 0))),
        sample_rate=sr,
        channels=min(channels, 2),
        is_loopback=loopback,
        is_output=is_output,
    )


def list_microphone_devices() -> list[AudioDeviceInfo]:
    """Input devices suitable for microphone capture."""
    devices: list[AudioDeviceInfo] = []
    for i, info in enumerate(sd.query_devices()):
        if _device_channels(info, True) <= 0:
            continue
        name = str(info.get("name", ""))
        if _looks_like_system_capture(name) and "microphone" not in name.lower() and "mic" not in name.lower():
            continue
        entry = _make_device_info(i, info, loopback=False, is_output=False)
        if entry:
            devices.append(entry)
    return devices


def list_system_capture_devices() -> list[AudioDeviceInfo]:
    """Devices that can capture computer playback (loopback / monitor / stereo mix)."""
    from sayink.pawp_capture import list_pawp_loopback_devices

    seen: set[int] = set()
    devices: list[AudioDeviceInfo] = []

    if sys.platform == "win32" and not _supports_wasapi_loopback_flag():
        for entry in list_pawp_loopback_devices():
            if entry.index not in seen:
                devices.append(entry)
                seen.add(entry.index)

    for i, info in enumerate(sd.query_devices()):
        name = str(info.get("name", ""))
        in_ch = _device_channels(info, True)
        out_ch = _device_channels(info, False)
        is_loopback_name = _looks_like_system_capture(name)

        if in_ch > 0 and is_loopback_name and i not in seen:
            entry = _make_device_info(i, info, loopback=True, is_output=False)
            if entry:
                devices.append(entry)
                seen.add(i)

        if out_ch > 0 and i not in seen:
            hostapi = _hostapi_name(int(info.get("hostapi", 0)))
            if "wasapi" in hostapi.lower() or is_loopback_name:
                entry = _make_device_info(i, info, loopback=True, is_output=True)
                if entry:
                    devices.append(entry)
                    seen.add(i)

    devices = _ensure_default_output_in_system_list(devices, seen)
    return devices


def pick_default_microphone() -> Optional[AudioDeviceInfo]:
    mics = list_microphone_devices()
    if not mics:
        return None
    try:
        default_in = sd.default.device[0]
        if default_in is not None and int(default_in) >= 0:
            for d in mics:
                if d.index == int(default_in):
                    return d
    except Exception:
        pass
    return mics[0]


def _default_output_device_index() -> Optional[int]:
    try:
        idx = sd.default.device[1]
        if idx is not None and int(idx) >= 0:
            return int(idx)
    except Exception:
        pass
    return None


def _same_playback_device_name(index_a: int, index_b: int) -> bool:
    try:
        name_a = str(sd.query_devices(int(index_a)).get("name", ""))
        name_b = str(sd.query_devices(int(index_b)).get("name", ""))
        return bool(name_a) and name_a == name_b
    except Exception:
        return False


def _wasapi_output_twin_for_default(default_out: int) -> Optional[AudioDeviceInfo]:
    """Find a WASAPI output entry for the same logical speaker as the default device."""
    try:
        default_name = str(sd.query_devices(int(default_out)).get("name", ""))
    except Exception:
        return None
    if not default_name:
        return None
    for i, info in enumerate(sd.query_devices()):
        if int(i) == int(default_out):
            continue
        if _device_channels(info, False) <= 0:
            continue
        if str(info.get("name", "")) != default_name:
            continue
        hostapi = _hostapi_name(int(info.get("hostapi", 0)))
        if "wasapi" not in hostapi.lower():
            continue
        return _make_device_info(int(i), info, loopback=True, is_output=True)
    return None


def _ensure_default_output_in_system_list(
    devices: list[AudioDeviceInfo], seen: set[int]
) -> list[AudioDeviceInfo]:
    """Always expose the OS default playback device for loopback / mixed mode."""
    default_out = _default_output_device_index()
    if default_out is None:
        return devices
    out = list(devices)
    to_prepend: list[AudioDeviceInfo] = []

    if int(default_out) not in seen:
        try:
            info = sd.query_devices(int(default_out))
            entry = _make_device_info(int(default_out), info, loopback=True, is_output=True)
            if entry:
                to_prepend.append(entry)
                seen.add(int(default_out))
        except Exception as e:
            log.debug("默认输出设备不可用: %s", e)

    twin = _wasapi_output_twin_for_default(int(default_out))
    if twin is not None and twin.index not in seen:
        to_prepend.append(twin)
        seen.add(twin.index)

    return to_prepend + out


def _system_device_auto_score(dev: AudioDeviceInfo, default_out_idx: Optional[int]) -> int:
    from sayink.pawp_capture import is_encoded_pawp_device_index

    if is_encoded_pawp_device_index(dev.index):
        return 500
    name = dev.name.lower()
    score = 0
    if default_out_idx is not None and dev.index == default_out_idx:
        score += 120
    if not dev.is_output:
        score += 90
    if any(h in name for h in _HIGH_PRIORITY_AUTO_HINTS):
        score += 50
    if "wasapi" in dev.hostapi.lower() and dev.is_output:
        score += 15
    if any(h in name for h in _LOW_PRIORITY_AUTO_HINTS):
        score -= 100
    if "audio device" in name and "realtek" not in name and "立体声" not in name:
        score -= 80
    if "virtual" in name or "虚拟" in name:
        if "vb-audio" not in name and "cable" not in name:
            score -= 40
    return score


def ordered_system_devices(preferred_index: int = -1) -> list[AudioDeviceInfo]:
    """System capture devices in try order (auto mode skips low-priority virtual speakers)."""
    systems = list_system_capture_devices()
    if not systems:
        return []
    default_out = _default_output_device_index()
    ranked = sorted(
        systems,
        key=lambda d: _system_device_auto_score(d, default_out),
        reverse=True,
    )
    if preferred_index is not None and int(preferred_index) >= 0:
        preferred = [d for d in ranked if d.index == int(preferred_index)]
        rest = [d for d in ranked if d.index != int(preferred_index)]
        return preferred + rest
    return ranked


def is_unreliable_loopback_output(dev: AudioDeviceInfo) -> bool:
    """WASAPI loopback often fails on app virtual speakers (NetEase, generic Audio Device)."""
    from sayink.pawp_capture import is_encoded_pawp_device_index

    if is_encoded_pawp_device_index(dev.index):
        return False
    if not dev.is_output:
        return False
    name = dev.name.lower()
    if any(h in name for h in _LOW_PRIORITY_AUTO_HINTS):
        return True
    if "audio device" in name and "realtek" not in name:
        return True
    return False


def sanitize_system_device_index(index: int) -> int:
    """Map manual picks of known-bad loopback outputs back to auto (-1)."""
    if int(index) < 0:
        return -1
    default_out = _default_output_device_index()
    for dev in list_system_capture_devices():
        if dev.index == int(index):
            if is_unreliable_loopback_output(dev):
                if default_out is not None and (
                    dev.index == int(default_out)
                    or _same_playback_device_name(dev.index, int(default_out))
                ):
                    return int(index)
                return -1
            return int(index)
    return -1


def list_system_capture_devices_for_settings() -> list[AudioDeviceInfo]:
    """Devices shown in settings; hide known-bad virtual speakers when alternatives exist."""
    devices = list_system_capture_devices()
    reliable = [d for d in devices if not is_unreliable_loopback_output(d)]
    return reliable if reliable else devices


def should_use_wasapi_loopback(dev: AudioDeviceInfo) -> bool:
    if not _supports_wasapi_loopback_flag() or sys.platform != "win32":
        return False
    return dev.is_output and "wasapi" in dev.hostapi.lower()


def pick_default_system_capture() -> Optional[AudioDeviceInfo]:
    from sayink.pawp_capture import (
        pick_default_pawp_loopback,
    )

    if sys.platform == "win32" and not _supports_wasapi_loopback_flag():
        pawp_default = pick_default_pawp_loopback()
        if pawp_default is not None:
            return pawp_default

    systems = ordered_system_devices(-1)
    if not systems:
        return None
    default_out = _default_output_device_index()
    reliable = [d for d in systems if not is_unreliable_loopback_output(d)]
    pool = reliable if reliable else systems
    if default_out is not None:
        for dev in pool:
            if dev.index == int(default_out):
                return dev
        for dev in pool:
            if _same_playback_device_name(dev.index, int(default_out)):
                return dev
    for dev in pool:
        if not is_unreliable_loopback_output(dev):
            return dev
    return pool[0]


def resolve_device(index: int, candidates: list[AudioDeviceInfo], default: Optional[AudioDeviceInfo]) -> Optional[AudioDeviceInfo]:
    if index is not None and int(index) >= 0:
        for d in candidates:
            if d.index == int(index):
                return d
    return default


@dataclass(frozen=True)
class StreamEndpoint:
    role: str
    device: AudioDeviceInfo
    use_wasapi_loopback: bool


@dataclass(frozen=True)
class RecordingPlan:
    input_source: str
    endpoints: tuple[StreamEndpoint, ...]


def build_recording_plan(
    input_source: str,
    mic_device_index: int = -1,
    system_device_index: int = -1,
) -> RecordingPlan:
    source = input_source if input_source in INPUT_SOURCES else INPUT_SOURCE_MICROPHONE
    mics = list_microphone_devices()
    systems = list_system_capture_devices()
    default_mic = pick_default_microphone()
    default_sys = pick_default_system_capture()

    mic = resolve_device(mic_device_index, mics, default_mic)
    sys_dev = resolve_device(system_device_index, systems, default_sys)

    endpoints: list[StreamEndpoint] = []

    if source == INPUT_SOURCE_MICROPHONE:
        if mic is None:
            raise RuntimeError("未找到可用的麦克风设备")
        endpoints.append(StreamEndpoint("microphone", mic, False))
    elif source == INPUT_SOURCE_SYSTEM:
        if sys_dev is None:
            raise RuntimeError(
                "未找到可用的系统声音采集设备。Windows 可尝试启用「立体声混音」或安装 VB-Audio Virtual Cable；"
                "macOS 可安装 BlackHole 并在下方选择对应设备。"
            )
        endpoints.append(
            StreamEndpoint("system", sys_dev, should_use_wasapi_loopback(sys_dev))
        )
    elif source == INPUT_SOURCE_MIXED:
        if mic is None:
            raise RuntimeError("未找到可用的麦克风设备（混合模式需要麦克风）")
        endpoints.append(StreamEndpoint("microphone", mic, False))
        if sys_dev is not None:
            endpoints.append(
                StreamEndpoint("system", sys_dev, should_use_wasapi_loopback(sys_dev))
            )
    else:
        raise RuntimeError(f"未知的音频来源: {source}")

    return RecordingPlan(source, tuple(endpoints))


def input_source_label(source: str) -> str:
    return {
        INPUT_SOURCE_MICROPHONE: "麦克风",
        INPUT_SOURCE_SYSTEM: "电脑播放",
        INPUT_SOURCE_MIXED: "麦克风 + 电脑播放",
    }.get(source, source)


def plan_includes_system_capture(plan: RecordingPlan) -> bool:
    return any(ep.role == "system" for ep in plan.endpoints)

"""Windows WASAPI loopback via PyAudioWPatch when PortAudio lacks loopback support."""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, Any, Optional, Sequence

if TYPE_CHECKING:
    from sayink.audio_devices import AudioDeviceInfo

log = logging.getLogger("SayInk")

# Avoid colliding with sounddevice device indices in config/UI.
PAWP_DEVICE_INDEX_OFFSET = 100_000


def strip_loopback_suffix(name: str) -> str:
    """Normalize away '[Loopback]' / '[系统回放]' tags for physical endpoint matching."""
    cleaned = (name or "").strip()
    for suffix in (" [Loopback]", "[Loopback]", " (Loopback)", " [系统回放]", "[系统回放]"):
        if cleaned.lower().endswith(suffix.lower()):
            cleaned = cleaned[: len(cleaned) - len(suffix)].strip()
    return cleaned


def endpoint_names_match(candidate_name: str, target_name: str) -> bool:
    """Match a loopback device name against a CoreAudio/PortAudio render endpoint name."""
    cand = strip_loopback_suffix(candidate_name).lower()
    targ = strip_loopback_suffix(target_name).lower()
    if not cand or not targ:
        return False
    if cand == targ:
        return True
    # MME / legacy APIs sometimes truncate device names to 31 characters.
    if min(len(cand), len(targ)) >= 24 and (cand.startswith(targ) or targ.startswith(cand)):
        return True
    return False


def pawp_available() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import pyaudiowpatch  # noqa: F401

        return True
    except ImportError:
        return False


def encode_pawp_device_index(pawp_index: int) -> int:
    return PAWP_DEVICE_INDEX_OFFSET + int(pawp_index)


def is_encoded_pawp_device_index(index: int) -> bool:
    return int(index) >= PAWP_DEVICE_INDEX_OFFSET


def decode_pawp_device_index(encoded: int) -> int:
    return int(encoded) - PAWP_DEVICE_INDEX_OFFSET


def _info_to_audio_device(info: dict[str, Any], default_name: str = "默认扬声器环回") -> AudioDeviceInfo:
    from sayink.audio_devices import AudioDeviceInfo

    idx = int(info["index"])
    ch = int(info.get("maxInputChannels", 1) or 1)
    sr = int(info.get("defaultSampleRate") or 48000)
    return AudioDeviceInfo(
        index=encode_pawp_device_index(idx),
        name=str(info.get("name", default_name)),
        hostapi="PyAudioWPatch (WASAPI 环回)",
        sample_rate=sr,
        channels=max(1, ch),
        is_loopback=True,
        is_output=False,
    )


def list_pawp_loopback_devices() -> list[AudioDeviceInfo]:
    if not pawp_available():
        return []
    import pyaudiowpatch as pyaudio

    p = pyaudio.PyAudio()
    devices: list[AudioDeviceInfo] = []
    try:
        for info in p.get_loopback_device_info_generator():
            idx = int(info["index"])
            devices.append(_info_to_audio_device(info, default_name=f"Loopback {idx}"))
    except Exception as e:
        log.debug("枚举 PyAudioWPatch 环回设备失败: %s", e)
    finally:
        p.terminate()
    return devices


def pick_pawp_loopback_for_names(
    preferred_names: Sequence[str],
    devices: Optional[Sequence[AudioDeviceInfo]] = None,
) -> Optional[AudioDeviceInfo]:
    """Resolve the first PyAudioWPatch loopback matching preferred render endpoint names."""
    pool = list(devices) if devices is not None else list_pawp_loopback_devices()
    if not pool:
        return None
    for target in preferred_names:
        if not target:
            continue
        for dev in pool:
            if endpoint_names_match(dev.name, target):
                return dev
    return None


def resolve_pawp_stream_device_info(
    pa_instance: Any,
    encoded_index: int,
    expected_name: str = "",
) -> dict[str, Any]:
    """Validate PAWP loopback index at stream-open time and recover if hotplug shifted indices."""
    raw_idx = decode_pawp_device_index(encoded_index)
    try:
        info = pa_instance.get_device_info_by_index(raw_idx)
        is_loopback = bool(info.get("isLoopbackDevice", True))
        if is_loopback and (
            not expected_name or endpoint_names_match(str(info.get("name", "")), expected_name)
        ):
            return info
    except Exception:
        info = None

    # Device index shifted or pointed at a non-loopback endpoint; relocate by name first.
    if expected_name:
        try:
            for candidate in pa_instance.get_loopback_device_info_generator():
                if endpoint_names_match(str(candidate.get("name", "")), expected_name):
                    return candidate
        except Exception as e:
            log.debug("按名称重定位 PyAudioWPatch 环回设备失败: %s", e)

    if info is not None and bool(info.get("isLoopbackDevice", False)):
        return info
    return pa_instance.get_default_wasapi_loopback()


def pick_default_pawp_loopback() -> Optional[AudioDeviceInfo]:
    if not pawp_available():
        return None
    import pyaudiowpatch as pyaudio

    from sayink.audio_devices import preferred_windows_loopback_names

    p = pyaudio.PyAudio()
    try:
        loopbacks = list(p.get_loopback_device_info_generator())
        preferred = preferred_windows_loopback_names()
        for target in preferred:
            if not target:
                continue
            for cand in loopbacks:
                if endpoint_names_match(str(cand.get("name", "")), target):
                    return _info_to_audio_device(cand)
        info = p.get_default_wasapi_loopback()
        return _info_to_audio_device(info)
    except Exception as e:
        log.debug("获取默认 WASAPI 环回失败: %s", e)
        return None
    finally:
        p.terminate()

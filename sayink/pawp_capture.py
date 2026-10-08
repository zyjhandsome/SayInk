"""Windows WASAPI loopback via PyAudioWPatch when PortAudio lacks loopback support."""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from sayink.audio_devices import AudioDeviceInfo

log = logging.getLogger("SayInk")

# Avoid colliding with sounddevice device indices in config/UI.
PAWP_DEVICE_INDEX_OFFSET = 100_000


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


def list_pawp_loopback_devices() -> list[AudioDeviceInfo]:
    from sayink.audio_devices import AudioDeviceInfo

    if not pawp_available():
        return []
    import pyaudiowpatch as pyaudio

    p = pyaudio.PyAudio()
    devices: list[AudioDeviceInfo] = []
    try:
        for info in p.get_loopback_device_info_generator():
            idx = int(info["index"])
            ch = int(info.get("maxInputChannels", 1) or 1)
            sr = int(info.get("defaultSampleRate") or 48000)
            name = str(info.get("name", f"Loopback {idx}"))
            devices.append(
                AudioDeviceInfo(
                    index=encode_pawp_device_index(idx),
                    name=name,
                    hostapi="PyAudioWPatch (WASAPI 环回)",
                    sample_rate=sr,
                    channels=max(1, ch),
                    is_loopback=True,
                    is_output=False,
                )
            )
    except Exception as e:
        log.debug("枚举 PyAudioWPatch 环回设备失败: %s", e)
    finally:
        p.terminate()
    return devices


def pick_default_pawp_loopback() -> Optional[AudioDeviceInfo]:
    if not pawp_available():
        return None
    import pyaudiowpatch as pyaudio

    from sayink.audio_devices import AudioDeviceInfo

    p = pyaudio.PyAudio()
    try:
        info = p.get_default_wasapi_loopback()
        idx = int(info["index"])
        ch = int(info.get("maxInputChannels", 1) or 1)
        sr = int(info.get("defaultSampleRate") or 48000)
        return AudioDeviceInfo(
            index=encode_pawp_device_index(idx),
            name=str(info.get("name", "默认扬声器环回")),
            hostapi="PyAudioWPatch (WASAPI 环回)",
            sample_rate=sr,
            channels=max(1, ch),
            is_loopback=True,
            is_output=False,
        )
    except Exception as e:
        log.debug("获取默认 WASAPI 环回失败: %s", e)
        return None
    finally:
        p.terminate()

"""Enumerate and resolve microphone / system (loopback) capture devices."""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional, Sequence

import sounddevice as sd

log = logging.getLogger("SayInk")

INPUT_SOURCE_MICROPHONE = "microphone"
INPUT_SOURCE_SYSTEM = "system"
INPUT_SOURCE_MIXED = "mixed"
INPUT_SOURCES = (INPUT_SOURCE_MICROPHONE, INPUT_SOURCE_SYSTEM, INPUT_SOURCE_MIXED)

_BLUETOOTH_COMM_HINTS = (
    "hands-free",
    "handsfree",
    "ag audio",
    "hf audio",
    "headset",
    "免提",
    "耳机通话",
)

_portaudio_lock = threading.RLock()
_active_portaudio_streams = 0
_SNAPSHOT_TTL_SEC = 1.5
_snapshot_cache: dict[str, Any] = {
    "ts": 0.0,
    "sig": None,
    "devices": None,
    "hostapis": None,
    "pawp_devices": None,
    "win_endpoints": None,
}


def mark_portaudio_stream_active(delta: int) -> None:
    """Track active PortAudio streams so refresh_audio_devices never resets live streams."""
    global _active_portaudio_streams
    with _portaudio_lock:
        _active_portaudio_streams = max(0, _active_portaudio_streams + int(delta))


def invalidate_device_cache(*, keep_win_endpoints: bool = False) -> None:
    """Drop short-lived device enumeration caches."""
    with _portaudio_lock:
        _snapshot_cache["ts"] = 0.0
        _snapshot_cache["sig"] = None
        _snapshot_cache["devices"] = None
        _snapshot_cache["hostapis"] = None
        _snapshot_cache["pawp_devices"] = None
        if not keep_win_endpoints:
            _snapshot_cache["win_endpoints"] = None


def refresh_audio_devices(*, force: bool = False) -> bool:
    """Re-initialize PortAudio device list (when idle) and clear enumeration caches.

    PortAudio caches hardware endpoints inside ``Pa_Initialize()`` and otherwise
    never notices newly connected USB mics or Bluetooth headsets until restart.
    """
    with _portaudio_lock:
        invalidate_device_cache()
        if _active_portaudio_streams > 0 and not force:
            return False
        term = getattr(sd, "_terminate", None)
        init = getattr(sd, "_initialize", None)
        if not (callable(term) and callable(init)):
            return False
        # Avoid spinning up native PortAudio when tests monkeypatch query_devices,
        # unless _terminate/_initialize themselves were patched or force=True.
        qd_mod = getattr(sd.query_devices, "__module__", "") or ""
        term_mod = getattr(term, "__module__", "") or ""
        if not force and not qd_mod.startswith("sounddevice") and term_mod.startswith("sounddevice"):
            return False
        try:
            term()
            init()
            return True
        except Exception as e:
            log.debug("刷新 PortAudio 设备列表失败: %s", e)
            try:
                init()
            except Exception:
                pass
            return False


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", ctypes.c_uint32)]


class _PROPVARIANT_UNION(ctypes.Union):
    _fields_ = [
        ("pwszVal", ctypes.c_wchar_p),
        ("uhVal", ctypes.c_uint64),
        ("pad", ctypes.c_ubyte * 16),
    ]


class _PROPVARIANT(ctypes.Structure):
    _fields_ = [
        ("vt", ctypes.c_uint16),
        ("wReserved1", ctypes.c_uint16),
        ("wReserved2", ctypes.c_uint16),
        ("wReserved3", ctypes.c_uint16),
        ("u", _PROPVARIANT_UNION),
    ]


def _make_guid(a: int, b: int, c: int, d: Sequence[int]) -> _GUID:
    arr = (ctypes.c_ubyte * 8)(*d)
    return _GUID(a, b, c, arr)


_CLSID_MMDeviceEnumerator = _make_guid(
    0xBCDE0395, 0xE52F, 0x467C, (0x8E, 0x3D, 0xC4, 0x57, 0x92, 0x91, 0x69, 0x2E)
)
_IID_IMMDeviceEnumerator = _make_guid(
    0xA95664D2, 0x9614, 0x4F35, (0xA7, 0x46, 0xDE, 0x8D, 0xB6, 0x36, 0x17, 0xE6)
)
_IID_IAudioMeterInformation = _make_guid(
    0xC02216F6, 0x8C67, 0x4B5B, (0x90, 0x00, 0xD0, 0x08, 0xE7, 0x3E, 0x00, 0x64)
)
_PKEY_Device_FriendlyName = _PROPERTYKEY(
    _make_guid(0xA45C254E, 0xDF1C, 0x4EFD, (0x80, 0x20, 0x67, 0xD1, 0x46, 0xA8, 0x50, 0xE0)),
    14,
)


@dataclass(frozen=True)
class WindowsRenderEndpoints:
    console_name: str = ""
    console_peak: float = 0.0
    communications_name: str = ""
    communications_peak: float = 0.0
    active_render_name: str = ""
    active_render_peak: float = 0.0


def _com_release(ptr: ctypes.c_void_p) -> None:
    if ptr and ptr.value:
        vtable = ctypes.cast(
            ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))[0],
            ctypes.POINTER(ctypes.c_void_p),
        )
        release_fn = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])
        release_fn(ptr)
        ptr.value = None


def _mmdevice_friendly_name_and_peak(dev_ptr: ctypes.c_void_p, ole32: Any) -> tuple[str, float]:
    name = ""
    peak = 0.0
    if not dev_ptr or not dev_ptr.value:
        return name, peak
    vtable = ctypes.cast(
        ctypes.cast(dev_ptr, ctypes.POINTER(ctypes.c_void_p))[0],
        ctypes.POINTER(ctypes.c_void_p),
    )
    # IMMDevice::OpenPropertyStore (index 4), STGM_READ = 0
    open_store = ctypes.WINFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)
    )(vtable[4])
    store_ptr = ctypes.c_void_p()
    if open_store(dev_ptr, 0, ctypes.byref(store_ptr)) >= 0 and store_ptr.value:
        try:
            svt = ctypes.cast(
                ctypes.cast(store_ptr, ctypes.POINTER(ctypes.c_void_p))[0],
                ctypes.POINTER(ctypes.c_void_p),
            )
            # IPropertyStore::GetValue (index 5)
            get_val = ctypes.WINFUNCTYPE(
                ctypes.c_long,
                ctypes.c_void_p,
                ctypes.POINTER(_PROPERTYKEY),
                ctypes.POINTER(_PROPVARIANT),
            )(svt[5])
            prop = _PROPVARIANT()
            if get_val(store_ptr, ctypes.byref(_PKEY_Device_FriendlyName), ctypes.byref(prop)) >= 0:
                try:
                    if prop.vt == 31 and prop.u.pwszVal:  # VT_LPWSTR
                        name = str(prop.u.pwszVal).strip()
                finally:
                    try:
                        ole32.PropVariantClear(ctypes.byref(prop))
                    except Exception:
                        pass
        finally:
            _com_release(store_ptr)

    # IMMDevice::Activate (index 3) -> IAudioMeterInformation
    activate = ctypes.WINFUNCTYPE(
        ctypes.c_long,
        ctypes.c_void_p,
        ctypes.POINTER(_GUID),
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    )(vtable[3])
    meter_ptr = ctypes.c_void_p()
    if (
        activate(
            dev_ptr,
            ctypes.byref(_IID_IAudioMeterInformation),
            1,  # CLSCTX_INPROC_SERVER
            None,
            ctypes.byref(meter_ptr),
        )
        >= 0
        and meter_ptr.value
    ):
        try:
            mvt = ctypes.cast(
                ctypes.cast(meter_ptr, ctypes.POINTER(ctypes.c_void_p))[0],
                ctypes.POINTER(ctypes.c_void_p),
            )
            get_peak = ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_float)
            )(mvt[3])
            val = ctypes.c_float(0.0)
            if get_peak(meter_ptr, ctypes.byref(val)) >= 0:
                peak = float(val.value)
        finally:
            _com_release(meter_ptr)
    return name, peak


_OLE32: Any = None
_ole32_lock = threading.Lock()


def _get_ole32() -> Any:
    global _OLE32
    windll_ole32 = getattr(getattr(ctypes, "windll", None), "ole32", None)
    win_dll_cls = getattr(ctypes, "WinDLL", None)
    if win_dll_cls is None or not isinstance(windll_ole32, win_dll_cls):
        return windll_ole32
    if _OLE32 is not None:
        return _OLE32
    with _ole32_lock:
        if _OLE32 is None:
            dll = win_dll_cls("ole32")
            dll.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            dll.CoInitializeEx.restype = ctypes.c_long
            dll.CoCreateInstance.argtypes = [
                ctypes.POINTER(_GUID),
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.POINTER(_GUID),
                ctypes.POINTER(ctypes.c_void_p),
            ]
            dll.CoCreateInstance.restype = ctypes.c_long
            dll.PropVariantClear.argtypes = [ctypes.POINTER(_PROPVARIANT)]
            dll.PropVariantClear.restype = ctypes.c_long
            dll.CoUninitialize.argtypes = []
            dll.CoUninitialize.restype = None
            _OLE32 = dll
    return _OLE32


def query_windows_render_endpoints() -> WindowsRenderEndpoints:
    """Query Windows CoreAudio eConsole + eCommunications render endpoints & peak meters."""
    if sys.platform != "win32":
        return WindowsRenderEndpoints()
    try:
        ole32 = _get_ole32()
    except Exception:
        return WindowsRenderEndpoints()
    if ole32 is None:
        return WindowsRenderEndpoints()

    hr_init = ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
    need_uninit = hr_init in (0, 1)  # S_OK or S_FALSE
    # RPC_E_CHANGED_MODE (0x80010106) means MTA is already initialized on this thread, which is fine.
    enum_ptr = ctypes.c_void_p()
    try:
        hr = ole32.CoCreateInstance(
            ctypes.byref(_CLSID_MMDeviceEnumerator),
            None,
            1,  # CLSCTX_INPROC_SERVER
            ctypes.byref(_IID_IMMDeviceEnumerator),
            ctypes.byref(enum_ptr),
        )
        if hr < 0 or not enum_ptr.value:
            return WindowsRenderEndpoints()
        evt = ctypes.cast(
            ctypes.cast(enum_ptr, ctypes.POINTER(ctypes.c_void_p))[0],
            ctypes.POINTER(ctypes.c_void_p),
        )
        # IMMDeviceEnumerator::GetDefaultAudioEndpoint (index 4): eRender=0, role=0(eConsole)/2(eCommunications)
        get_default = ctypes.WINFUNCTYPE(
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_void_p),
        )(evt[4])

        console_name, console_peak = "", 0.0
        comm_name, comm_peak = "", 0.0

        dev_console = ctypes.c_void_p()
        if get_default(enum_ptr, 0, 0, ctypes.byref(dev_console)) >= 0 and dev_console.value:
            try:
                console_name, console_peak = _mmdevice_friendly_name_and_peak(dev_console, ole32)
            finally:
                _com_release(dev_console)

        dev_comm = ctypes.c_void_p()
        if get_default(enum_ptr, 0, 2, ctypes.byref(dev_comm)) >= 0 and dev_comm.value:
            try:
                comm_name, comm_peak = _mmdevice_friendly_name_and_peak(dev_comm, ole32)
            finally:
                _com_release(dev_comm)

        # Also scan active render endpoints (DEVICE_STATE_ACTIVE = 1) for live audio peak
        # in case a meeting app routed explicitly to a Bluetooth Hands-Free endpoint.
        active_name, active_peak = "", 0.0
        enum_endpoints = ctypes.WINFUNCTYPE(
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
        )(evt[3])
        coll_ptr = ctypes.c_void_p()
        if enum_endpoints(enum_ptr, 0, 1, ctypes.byref(coll_ptr)) >= 0 and coll_ptr.value:
            try:
                cvt = ctypes.cast(
                    ctypes.cast(coll_ptr, ctypes.POINTER(ctypes.c_void_p))[0],
                    ctypes.POINTER(ctypes.c_void_p),
                )
                get_count = ctypes.WINFUNCTYPE(
                    ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)
                )(cvt[3])
                get_item = ctypes.WINFUNCTYPE(
                    ctypes.c_long,
                    ctypes.c_void_p,
                    ctypes.c_uint,
                    ctypes.POINTER(ctypes.c_void_p),
                )(cvt[4])
                cnt = ctypes.c_uint(0)
                if get_count(coll_ptr, ctypes.byref(cnt)) >= 0:
                    for idx in range(min(int(cnt.value), 32)):
                        item_ptr = ctypes.c_void_p()
                        if get_item(coll_ptr, idx, ctypes.byref(item_ptr)) >= 0 and item_ptr.value:
                            try:
                                nm, pk = _mmdevice_friendly_name_and_peak(item_ptr, ole32)
                                if pk > active_peak and nm and not any(
                                    low in nm.lower() for low in _LOW_PRIORITY_AUTO_HINTS
                                ):
                                    active_name, active_peak = nm, pk
                            finally:
                                _com_release(item_ptr)
            finally:
                _com_release(coll_ptr)

        return WindowsRenderEndpoints(
            console_name=console_name,
            console_peak=console_peak,
            communications_name=comm_name,
            communications_peak=comm_peak,
            active_render_name=active_name,
            active_render_peak=active_peak,
        )
    except Exception as e:
        log.debug("探测 Windows 播放端点失败: %s", e)
        return WindowsRenderEndpoints()
    finally:
        _com_release(enum_ptr)
        if need_uninit:
            try:
                ole32.CoUninitialize()
            except Exception:
                pass


def _is_bluetooth_comm_name(name: str) -> bool:
    lower = (name or "").lower()
    return any(h in lower for h in _BLUETOOTH_COMM_HINTS)


def _share_bluetooth_unit(name_a: str, name_b: str) -> bool:
    """Heuristic check whether two endpoints belong to the same Bluetooth headset."""
    a = (name_a or "").lower()
    b = (name_b or "").lower()
    if not a or not b:
        return False
    # Extract parenthetical model token if present, e.g. "(AirPods Hands-Free AG Audio)" vs "(AirPods Stereo)"
    for token in ("hands-free", "ag audio", "stereo", "headset", "headphones", "耳机", "扬声器", "(", ")"):
        a = a.replace(token, " ")
        b = b.replace(token, " ")
    words_a = {w for w in a.split() if len(w) >= 2}
    words_b = {w for w in b.split() if len(w) >= 2}
    return bool(words_a and words_b and (words_a & words_b))


def preferred_windows_loopback_names(active_mic_name: str = "") -> list[str]:
    """Return prioritized render endpoint names considering eConsole, eCommunications, and live output."""
    ep = _cached_windows_render_endpoints()
    ordered: list[str] = []

    def _add(nm: str) -> None:
        if nm and nm not in ordered:
            ordered.append(nm)

    # 1. If eCommunications has live audio while eConsole is silent (or quieter), prefer eCommunications!
    if ep.communications_name and ep.communications_peak > 1e-4 and (
        ep.communications_peak >= ep.console_peak * 0.8 or ep.console_peak <= 1e-4
    ):
        _add(ep.communications_name)

    # 2. If any active physical render endpoint is actively outputting audio while eConsole is silent
    #    (e.g. Zoom/Teams explicitly routed to Headset Hands-Free AG Audio), prefer it!
    if (
        ep.active_render_name
        and ep.active_render_peak > 1e-3
        and ep.console_peak <= 1e-4
    ):
        _add(ep.active_render_name)

    # 3. If the active microphone is a Bluetooth Hands-Free / Headset mic (or shares the BT unit
    #    with eCommunications), Windows suspends A2DP Stereo during mic capture so HFP eCommunications
    #    is the active call output even before remote speech begins.
    if ep.communications_name and ep.communications_name != ep.console_name:
        if active_mic_name and (
            _is_bluetooth_comm_name(active_mic_name)
            and (
                _is_bluetooth_comm_name(ep.communications_name)
                or _share_bluetooth_unit(active_mic_name, ep.communications_name)
            )
        ):
            _add(ep.communications_name)
        elif (
            _is_bluetooth_comm_name(ep.communications_name)
            and ep.console_peak <= 1e-4
            and _share_bluetooth_unit(ep.console_name, ep.communications_name)
            and active_mic_name
        ):
            _add(ep.communications_name)

    # 4. Default multimedia/console endpoint, then communications endpoint as fallback.
    _add(ep.console_name)
    _add(ep.communications_name)
    return ordered

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


def _query_raw_snapshot() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return cached (query_devices, query_hostapis) within a short TTL unless monkeypatched."""
    now = time.monotonic()
    sig = (id(sd.query_devices), id(sd.query_hostapis))
    with _portaudio_lock:
        if (
            _snapshot_cache["sig"] == sig
            and _snapshot_cache["devices"] is not None
            and _snapshot_cache["hostapis"] is not None
            and (now - float(_snapshot_cache["ts"])) < _SNAPSHOT_TTL_SEC
        ):
            return _snapshot_cache["devices"], _snapshot_cache["hostapis"]

    raw_devs = list(sd.query_devices())
    try:
        raw_apis = list(sd.query_hostapis())
    except Exception:
        log.debug("查询 hostapis 列表失败", exc_info=True)
        raw_apis = []

    with _portaudio_lock:
        _snapshot_cache["ts"] = now
        _snapshot_cache["sig"] = sig
        _snapshot_cache["devices"] = raw_devs
        _snapshot_cache["hostapis"] = raw_apis
    return raw_devs, raw_apis


def _cached_pawp_loopback_devices() -> list[AudioDeviceInfo]:
    from sayink import pawp_capture

    now = time.monotonic()
    fn = pawp_capture.list_pawp_loopback_devices
    with _portaudio_lock:
        cached = _snapshot_cache.get("pawp_devices")
        if (
            cached is not None
            and cached[0] == id(fn)
            and (now - float(cached[1])) < _SNAPSHOT_TTL_SEC
        ):
            return list(cached[2])
    devs = list(fn())
    with _portaudio_lock:
        _snapshot_cache["pawp_devices"] = (id(fn), now, devs)
    return list(devs)


def _cached_windows_render_endpoints() -> WindowsRenderEndpoints:
    # Avoid real COM probes when unit tests monkeypatch query_devices unless query_windows_render_endpoints itself is patched.
    qd_mod = getattr(sd.query_devices, "__module__", "") or ""
    probe_mod = getattr(query_windows_render_endpoints, "__module__", "") or ""
    if not qd_mod.startswith("sounddevice") and probe_mod == __name__:
        return WindowsRenderEndpoints()
    now = time.monotonic()
    with _portaudio_lock:
        cached = _snapshot_cache.get("win_endpoints")
        if (
            cached is not None
            and cached[0] == id(query_windows_render_endpoints)
            and (now - float(cached[1])) < 0.75
        ):
            return cached[2]
    ep = query_windows_render_endpoints()
    with _portaudio_lock:
        _snapshot_cache["win_endpoints"] = (id(query_windows_render_endpoints), now, ep)
    return ep


def _hostapi_name(hostapi_index: int, apis: Optional[Sequence[dict[str, Any]]] = None) -> str:
    try:
        pool = apis if apis is not None else _query_raw_snapshot()[1]
        return str(pool[hostapi_index]["name"])
    except Exception:
        log.debug("查询 hostapi %s 失败", hostapi_index, exc_info=True)
        return ""


def _supports_wasapi_loopback_flag() -> bool:
    try:
        sd.WasapiSettings(loopback=True)
        return True
    except TypeError:
        # Feature probe called from several places; the recorder logs once
        # when it actually falls back.
        return False


def _looks_like_system_capture(name: str) -> bool:
    lower = name.lower()
    return any(h in lower for h in _SYSTEM_NAME_HINTS)


def _device_channels(info: dict[str, Any], want_input: bool) -> int:
    key = "max_input_channels" if want_input else "max_output_channels"
    return int(info.get(key, 0) or 0)


def _make_device_info(
    index: int,
    info: dict[str, Any],
    *,
    loopback: bool,
    is_output: bool,
    apis: Optional[Sequence[dict[str, Any]]] = None,
) -> Optional[AudioDeviceInfo]:
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
        hostapi=_hostapi_name(int(info.get("hostapi", 0)), apis=apis),
        sample_rate=sr,
        channels=min(channels, 2),
        is_loopback=loopback,
        is_output=is_output,
    )


def list_microphone_devices() -> list[AudioDeviceInfo]:
    """Input devices suitable for microphone capture."""
    raw_devs, apis = _query_raw_snapshot()
    devices: list[AudioDeviceInfo] = []
    for i, info in enumerate(raw_devs):
        if _device_channels(info, True) <= 0:
            continue
        name = str(info.get("name", ""))
        if _looks_like_system_capture(name) and "microphone" not in name.lower() and "mic" not in name.lower():
            continue
        entry = _make_device_info(i, info, loopback=False, is_output=False, apis=apis)
        if entry:
            devices.append(entry)
    return devices


def list_system_capture_devices() -> list[AudioDeviceInfo]:
    """Devices that can capture computer playback (loopback / monitor / stereo mix)."""
    seen: set[int] = set()
    devices: list[AudioDeviceInfo] = []

    if sys.platform == "win32" and not _supports_wasapi_loopback_flag():
        for entry in _cached_pawp_loopback_devices():
            if entry.index not in seen:
                devices.append(entry)
                seen.add(entry.index)

    raw_devs, apis = _query_raw_snapshot()
    for i, info in enumerate(raw_devs):
        name = str(info.get("name", ""))
        in_ch = _device_channels(info, True)
        out_ch = _device_channels(info, False)
        is_loopback_name = _looks_like_system_capture(name)

        if in_ch > 0 and is_loopback_name and i not in seen:
            entry = _make_device_info(i, info, loopback=True, is_output=False, apis=apis)
            if entry:
                devices.append(entry)
                seen.add(i)

        if out_ch > 0 and i not in seen:
            hostapi = _hostapi_name(int(info.get("hostapi", 0)), apis=apis)
            if "wasapi" in hostapi.lower() or is_loopback_name:
                entry = _make_device_info(i, info, loopback=True, is_output=True, apis=apis)
                if entry:
                    devices.append(entry)
                    seen.add(i)

    devices = _ensure_default_output_in_system_list(devices, seen, raw_devs=raw_devs, apis=apis)
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
        log.debug("读取默认输入设备失败，改用第一个麦克风", exc_info=True)
    return mics[0]


def _default_output_device_index() -> Optional[int]:
    try:
        idx = sd.default.device[1]
        if idx is not None and int(idx) >= 0:
            return int(idx)
    except Exception:
        log.debug("读取默认输出设备失败", exc_info=True)
    return None


def _device_info_by_index(
    index: int,
    raw_devs: Optional[Sequence[dict[str, Any]]] = None,
) -> dict[str, Any]:
    if raw_devs is not None and 0 <= int(index) < len(raw_devs):
        return raw_devs[int(index)]
    devs, _ = _query_raw_snapshot()
    if 0 <= int(index) < len(devs):
        return devs[int(index)]
    return dict(sd.query_devices(int(index)))


def _wasapi_output_twin_for_default(
    default_out: int,
    *,
    raw_devs: Optional[Sequence[dict[str, Any]]] = None,
    apis: Optional[Sequence[dict[str, Any]]] = None,
) -> Optional[AudioDeviceInfo]:
    """Find a WASAPI output entry for the same logical speaker as the default device."""
    try:
        default_name = str(_device_info_by_index(int(default_out), raw_devs).get("name", ""))
    except Exception:
        log.debug("查询默认输出设备 %s 失败", default_out, exc_info=True)
        return None
    if not default_name:
        return None
    devs = raw_devs if raw_devs is not None else _query_raw_snapshot()[0]
    for i, info in enumerate(devs):
        if int(i) == int(default_out):
            continue
        if _device_channels(info, False) <= 0:
            continue
        if str(info.get("name", "")) != default_name:
            continue
        hostapi = _hostapi_name(int(info.get("hostapi", 0)), apis=apis)
        if "wasapi" not in hostapi.lower():
            continue
        return _make_device_info(int(i), info, loopback=True, is_output=True, apis=apis)
    return None


def _ensure_default_output_in_system_list(
    devices: list[AudioDeviceInfo],
    seen: set[int],
    *,
    raw_devs: Optional[Sequence[dict[str, Any]]] = None,
    apis: Optional[Sequence[dict[str, Any]]] = None,
) -> list[AudioDeviceInfo]:
    """Always expose the OS default playback device for loopback / mixed mode."""
    default_out = _default_output_device_index()
    if default_out is None:
        return devices
    out = list(devices)
    to_prepend: list[AudioDeviceInfo] = []

    if int(default_out) not in seen:
        try:
            info = _device_info_by_index(int(default_out), raw_devs)
            entry = _make_device_info(int(default_out), info, loopback=True, is_output=True, apis=apis)
            if entry:
                to_prepend.append(entry)
                seen.add(int(default_out))
        except Exception as e:
            log.debug("默认输出设备不可用: %s", e)

    twin = _wasapi_output_twin_for_default(int(default_out), raw_devs=raw_devs, apis=apis)
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
    from sayink.pawp_capture import endpoint_names_match

    default_out = _default_output_device_index()
    default_out_name = ""
    if default_out is not None:
        try:
            default_out_name = str(_device_info_by_index(int(default_out)).get("name", ""))
        except Exception:
            default_out_name = ""
    for dev in list_system_capture_devices():
        if dev.index == int(index):
            if is_unreliable_loopback_output(dev):
                if default_out is not None and (
                    dev.index == int(default_out)
                    or (
                        bool(default_out_name)
                        and (
                            dev.name == default_out_name
                            or endpoint_names_match(dev.name, default_out_name)
                        )
                    )
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


def _match_system_device_by_preferred_names(
    pool: Sequence[AudioDeviceInfo],
    preferred_names: Sequence[str],
) -> Optional[AudioDeviceInfo]:
    from sayink.pawp_capture import endpoint_names_match, is_encoded_pawp_device_index

    for target in preferred_names:
        if not target:
            continue
        # Prefer PAWP / native WASAPI entries first for an exact endpoint match
        pawp_matches = [
            d for d in pool if is_encoded_pawp_device_index(d.index) and endpoint_names_match(d.name, target)
        ]
        if pawp_matches:
            return pawp_matches[0]
        wasapi_matches = [
            d
            for d in pool
            if "wasapi" in d.hostapi.lower() and endpoint_names_match(d.name, target)
        ]
        if wasapi_matches:
            return wasapi_matches[0]
        any_matches = [d for d in pool if endpoint_names_match(d.name, target)]
        if any_matches:
            return any_matches[0]
    return None


def pick_default_system_capture(active_mic_name: str = "") -> Optional[AudioDeviceInfo]:
    from sayink.pawp_capture import (
        endpoint_names_match,
        pick_default_pawp_loopback,
        pick_pawp_loopback_for_names,
    )

    preferred_names = preferred_windows_loopback_names(active_mic_name=active_mic_name)

    if sys.platform == "win32" and not _supports_wasapi_loopback_flag():
        # Reuse cached PAWP list when preferred Windows endpoints resolve cleanly;
        # always honor monkeypatched pick_default_pawp_loopback in unit tests.
        pawp_mod = getattr(pick_default_pawp_loopback, "__module__", "") or ""
        if preferred_names and pawp_mod == "sayink.pawp_capture":
            matched = pick_pawp_loopback_for_names(
                preferred_names,
                devices=_cached_pawp_loopback_devices(),
            )
            if matched is not None:
                return matched
        try:
            pawp_default = pick_default_pawp_loopback(active_mic_name=active_mic_name)
        except TypeError:
            pawp_default = pick_default_pawp_loopback()
        if pawp_default is not None:
            return pawp_default

    systems = ordered_system_devices(-1)
    if not systems:
        return None
    default_out = _default_output_device_index()
    reliable = [d for d in systems if not is_unreliable_loopback_output(d)]
    pool = reliable if reliable else systems

    if preferred_names:
        matched_sys = _match_system_device_by_preferred_names(pool, preferred_names)
        if matched_sys is not None:
            return matched_sys

    if default_out is not None:
        for dev in pool:
            if dev.index == int(default_out):
                return dev
        try:
            default_out_name = str(_device_info_by_index(int(default_out)).get("name", ""))
        except Exception:
            default_out_name = ""
        if default_out_name:
            for dev in pool:
                if dev.name == default_out_name or endpoint_names_match(dev.name, default_out_name):
                    return dev
    return pool[0]


def describe_active_loopback_device(
    system_device_index: int = -1,
    mic_device_index: int = -1,
) -> str:
    """Return the human-readable name of the currently resolved physical loopback device."""
    from sayink.pawp_capture import strip_loopback_suffix

    active_mic_name = ""
    try:
        mics = list_microphone_devices()
        mic_dev = resolve_device(mic_device_index, mics, pick_default_microphone())
        if mic_dev is not None:
            active_mic_name = mic_dev.name
    except Exception:
        active_mic_name = ""

    systems = list_system_capture_devices()
    sanitized_idx = sanitize_system_device_index(system_device_index)
    try:
        default_sys = pick_default_system_capture(active_mic_name=active_mic_name)
    except TypeError:
        default_sys = pick_default_system_capture()
    resolved = resolve_device(sanitized_idx, systems, default_sys)
    if resolved is None:
        return ""
    return strip_loopback_suffix(resolved.name)


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

    mic: Optional[AudioDeviceInfo] = None
    if source in (INPUT_SOURCE_MICROPHONE, INPUT_SOURCE_MIXED):
        mics = list_microphone_devices()
        default_mic = pick_default_microphone()
        mic = resolve_device(mic_device_index, mics, default_mic)
        if mic is None and mic_device_index >= 0 and refresh_audio_devices():
            mics = list_microphone_devices()
            mic = resolve_device(mic_device_index, mics, pick_default_microphone())

    sys_dev: Optional[AudioDeviceInfo] = None
    if source in (INPUT_SOURCE_SYSTEM, INPUT_SOURCE_MIXED):
        systems = list_system_capture_devices()
        active_mic_name = mic.name if mic is not None else ""
        try:
            default_sys = pick_default_system_capture(active_mic_name=active_mic_name)
        except TypeError:
            # Support 0-arg monkeypatches in unit tests.
            default_sys = pick_default_system_capture()
        sys_dev = resolve_device(system_device_index, systems, default_sys)
        if sys_dev is None and refresh_audio_devices():
            systems = list_system_capture_devices()
            try:
                default_sys = pick_default_system_capture(active_mic_name=active_mic_name)
            except TypeError:
                default_sys = pick_default_system_capture()
            sys_dev = resolve_device(system_device_index, systems, default_sys)

    endpoints: list[StreamEndpoint] = []

    if source == INPUT_SOURCE_MICROPHONE:
        if mic is None:
            raise RuntimeError("未找到可用的麦克风设备")
        endpoints.append(StreamEndpoint("microphone", mic, False))
    elif source == INPUT_SOURCE_SYSTEM:
        if sys_dev is None:
            raise RuntimeError(
                "未找到可用的系统声音采集设备。请确认电脑扬声器或耳机处于启用状态，"
                "或在 Windows 声音设置中启用「立体声混音」。"
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


INPUT_SOURCE_LABELS = {
    INPUT_SOURCE_MICROPHONE: "麦克风",
    INPUT_SOURCE_SYSTEM: "电脑声音",
    INPUT_SOURCE_MIXED: "麦克风 + 电脑声音",
}


def input_source_label(source: str) -> str:
    """The one user-facing name per source (docs/terminology.md): the source
    picker, sidebar, history chips and meeting card all read from here."""
    return INPUT_SOURCE_LABELS.get(source, source)

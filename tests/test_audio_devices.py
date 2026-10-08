import pytest
from sayink.audio_devices import (
    INPUT_SOURCE_MICROPHONE,
    INPUT_SOURCE_MIXED,
    INPUT_SOURCE_SYSTEM,
    AudioDeviceInfo,
    build_recording_plan,
    ordered_system_devices,
    pick_default_system_capture,
    _looks_like_system_capture,
    _system_device_auto_score,
)


class TestSystemNameHints:
    def test_stereo_mix(self):
        assert _looks_like_system_capture("Stereo Mix (Realtek)")

    def test_normal_mic_false(self):
        assert not _looks_like_system_capture("Microphone Array")


class TestBuildRecordingPlan:
    def test_microphone_plan(self, monkeypatch):
        from sayink import audio_devices as ad

        fake_mic = ad.AudioDeviceInfo(0, "Mic", "WASAPI", 16000, 1, False, False)

        monkeypatch.setattr(ad, "list_microphone_devices", lambda: [fake_mic])
        monkeypatch.setattr(ad, "list_system_capture_devices", lambda: [])
        monkeypatch.setattr(ad, "pick_default_microphone", lambda: fake_mic)
        monkeypatch.setattr(ad, "pick_default_system_capture", lambda: None)

        plan = build_recording_plan(INPUT_SOURCE_MICROPHONE)
        assert plan.input_source == INPUT_SOURCE_MICROPHONE
        assert len(plan.endpoints) == 1
        assert plan.endpoints[0].role == "microphone"

    def test_mixed_requires_both(self, monkeypatch):
        from sayink import audio_devices as ad

        fake_mic = ad.AudioDeviceInfo(0, "Mic", "WASAPI", 16000, 1, False, False)
        fake_sys = ad.AudioDeviceInfo(1, "Speakers", "WASAPI", 48000, 2, True, True)

        monkeypatch.setattr(ad, "list_microphone_devices", lambda: [fake_mic])
        monkeypatch.setattr(ad, "list_system_capture_devices", lambda: [fake_sys])
        monkeypatch.setattr(ad, "pick_default_microphone", lambda: fake_mic)
        monkeypatch.setattr(ad, "pick_default_system_capture", lambda: fake_sys)
        monkeypatch.setattr(ad, "_supports_wasapi_loopback_flag", lambda: False)

        plan = build_recording_plan(INPUT_SOURCE_MIXED)
        assert len(plan.endpoints) == 2
        roles = {e.role for e in plan.endpoints}
        assert roles == {"microphone", "system"}

    def test_mixed_without_system_device_mic_only(self, monkeypatch):
        from sayink import audio_devices as ad

        fake_mic = ad.AudioDeviceInfo(0, "Mic", "WASAPI", 16000, 1, False, False)
        bad_sys = ad.AudioDeviceInfo(13, "Speakers (Audio Device)", "WASAPI", 48000, 2, True, True)

        monkeypatch.setattr(ad, "list_microphone_devices", lambda: [fake_mic])
        monkeypatch.setattr(ad, "list_system_capture_devices", lambda: [bad_sys])
        monkeypatch.setattr(ad, "pick_default_microphone", lambda: fake_mic)
        monkeypatch.setattr(ad, "pick_default_system_capture", lambda: None)

        plan = build_recording_plan(INPUT_SOURCE_MIXED)
        assert len(plan.endpoints) == 1
        assert plan.endpoints[0].role == "microphone"

    def test_mixed_includes_default_system_even_if_unreliable(self, monkeypatch):
        from sayink import audio_devices as ad

        fake_mic = ad.AudioDeviceInfo(0, "Mic", "WASAPI", 16000, 1, False, False)
        bad_sys = ad.AudioDeviceInfo(13, "Speakers (Audio Device)", "WASAPI", 48000, 2, True, True)

        monkeypatch.setattr(ad, "list_microphone_devices", lambda: [fake_mic])
        monkeypatch.setattr(ad, "list_system_capture_devices", lambda: [bad_sys])
        monkeypatch.setattr(ad, "pick_default_microphone", lambda: fake_mic)
        monkeypatch.setattr(ad, "pick_default_system_capture", lambda: bad_sys)
        monkeypatch.setattr(ad, "_supports_wasapi_loopback_flag", lambda: False)

        plan = build_recording_plan(INPUT_SOURCE_MIXED)
        assert len(plan.endpoints) == 2
        assert {e.role for e in plan.endpoints} == {"microphone", "system"}

    def test_system_missing_raises(self, monkeypatch):
        from sayink import audio_devices as ad

        monkeypatch.setattr(ad, "list_microphone_devices", lambda: [])
        monkeypatch.setattr(ad, "list_system_capture_devices", lambda: [])
        monkeypatch.setattr(ad, "pick_default_microphone", lambda: None)
        monkeypatch.setattr(ad, "pick_default_system_capture", lambda: None)

        with pytest.raises(RuntimeError, match="系统声音"):
            build_recording_plan(INPUT_SOURCE_SYSTEM)

    def test_system_only_plan(self, monkeypatch):
        """README: 仅电脑播放 — 单路系统环回。"""
        from sayink import audio_devices as ad

        fake_sys = ad.AudioDeviceInfo(17, "Speakers [Loopback]", "WASAPI", 48000, 4, True, True)
        monkeypatch.setattr(ad, "list_microphone_devices", lambda: [])
        monkeypatch.setattr(ad, "list_system_capture_devices", lambda: [fake_sys])
        monkeypatch.setattr(ad, "pick_default_microphone", lambda: None)
        monkeypatch.setattr(ad, "pick_default_system_capture", lambda: fake_sys)

        plan = build_recording_plan(INPUT_SOURCE_SYSTEM)
        assert plan.input_source == INPUT_SOURCE_SYSTEM
        assert len(plan.endpoints) == 1
        assert plan.endpoints[0].role == "system"
        assert plan.endpoints[0].device.index == 17

    def test_invalid_source_defaults_to_microphone(self, monkeypatch):
        from sayink import audio_devices as ad

        fake_mic = ad.AudioDeviceInfo(0, "Mic", "WASAPI", 16000, 1, False, False)
        monkeypatch.setattr(ad, "list_microphone_devices", lambda: [fake_mic])
        monkeypatch.setattr(ad, "list_system_capture_devices", lambda: [])
        monkeypatch.setattr(ad, "pick_default_microphone", lambda: fake_mic)
        monkeypatch.setattr(ad, "pick_default_system_capture", lambda: None)

        plan = build_recording_plan("unknown-source")
        assert plan.input_source == INPUT_SOURCE_MICROPHONE


class TestSystemDeviceRanking:
    def _dev(self, index: int, name: str, *, is_output: bool = True) -> AudioDeviceInfo:
        return AudioDeviceInfo(index, name, "Windows WASAPI", 48000, 2, True, is_output)

    def test_unreliable_default_still_picked_when_no_alternative(self, monkeypatch):
        from sayink import audio_devices as ad

        netease = self._dev(12, "扬声器 (网易虚拟音频设备)")
        generic = self._dev(13, "扬声器 (Audio Device)")
        monkeypatch.setattr(
            "sayink.pawp_capture.pick_default_pawp_loopback", lambda: None
        )
        monkeypatch.setattr(ad, "list_system_capture_devices", lambda: [netease, generic])
        monkeypatch.setattr(ad, "_default_output_device_index", lambda: 13)
        monkeypatch.setattr(ad, "_supports_wasapi_loopback_flag", lambda: False)
        picked = ad.pick_default_system_capture()
        assert picked is not None
        assert picked.index == 13

    def test_netease_deprioritized_over_realtek(self, monkeypatch):
        from sayink import audio_devices as ad

        netease = self._dev(1, "扬声器 (网易虚拟音频设备)")
        realtek = self._dev(2, "扬声器 (Realtek(R) Audio)")
        monkeypatch.setattr(ad, "list_system_capture_devices", lambda: [netease, realtek])
        monkeypatch.setattr(ad, "_default_output_device_index", lambda: 2)
        monkeypatch.setattr(
            "sayink.pawp_capture.pick_default_pawp_loopback", lambda: None
        )
        monkeypatch.setattr(ad, "_supports_wasapi_loopback_flag", lambda: True)

        ordered = ordered_system_devices(-1)
        assert ordered[0].index == 2
        assert pick_default_system_capture().index == 2

    def test_default_output_scores_highest(self):
        dev = self._dev(5, "Speakers")
        assert _system_device_auto_score(dev, 5) > _system_device_auto_score(dev, None)

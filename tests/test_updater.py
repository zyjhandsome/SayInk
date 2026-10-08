"""GitHub release check: compare versions, pick the installer, respect the daily gate."""

from io import BytesIO

from voiceink.updater import (
    ReleaseInfo,
    download_installer,
    is_newer,
    is_trusted_installer_url,
    release_from_payload,
    should_auto_check,
)


def _payload(version: str, name: str, url: str) -> dict:
    return {
        "tag_name": f"v{version}",
        "assets": [{"name": name, "browser_download_url": url}],
    }


class TestReleaseSelection:
    def test_newer_release_with_the_matching_installer_is_offered(self):
        info = release_from_payload(
            _payload(
                "2.0.6",
                "VoiceInk-Setup-2.0.6.exe",
                "https://github.com/zyjhandsome/VoiceInk/releases/download/v2.0.6/VoiceInk-Setup-2.0.6.exe",
            ),
            current="2.0.5",
        )
        assert info == ReleaseInfo(
            version="2.0.6",
            asset_name="VoiceInk-Setup-2.0.6.exe",
            asset_url="https://github.com/zyjhandsome/VoiceInk/releases/download/v2.0.6/VoiceInk-Setup-2.0.6.exe",
        )

    def test_same_or_older_release_is_not_an_update(self):
        payload = _payload(
            "2.0.5",
            "VoiceInk-Setup-2.0.5.exe",
            "https://github.com/zyjhandsome/VoiceInk/releases/download/v2.0.5/VoiceInk-Setup-2.0.5.exe",
        )
        assert release_from_payload(payload, current="2.0.5") is None
        assert not is_newer("2.0.4", "2.0.5")

    def test_untrusted_asset_url_is_ignored(self):
        info = release_from_payload(
            _payload("9.0.0", "VoiceInk-Setup-9.0.0.exe", "http://example.com/setup.exe"),
            current="2.0.5",
        )
        assert info is None
        assert not is_trusted_installer_url("http://github.com/a.exe")


class TestAutoCheckGate:
    def test_disabled_switch_never_checks(self):
        assert should_auto_check(enabled=False, last_check_at=0, now=10_000) is False

    def test_first_check_and_daily_interval(self):
        assert should_auto_check(enabled=True, last_check_at=0, now=10_000) is True
        assert should_auto_check(enabled=True, last_check_at=1_000, now=1_000 + 3600) is False
        assert should_auto_check(enabled=True, last_check_at=1_000, now=1_000 + 86_400) is True


class TestDownload:
    def test_download_writes_the_body_and_reports_size(self, tmp_path):
        body = b"installer-bytes"

        class _Response:
            headers = {"Content-Length": str(len(body))}

            def read(self, _n):
                return self._buf.read(_n)

            def __enter__(self):
                self._buf = BytesIO(body)
                return self

            def __exit__(self, *args):
                return False

        import hashlib

        seen = []
        dest = tmp_path / "VoiceInk-Setup-2.0.6.exe"
        download_installer(
            "https://github.com/zyjhandsome/VoiceInk/releases/download/v2.0.6/VoiceInk-Setup-2.0.6.exe",
            dest,
            lambda *_args, **_kwargs: _Response(),
            on_progress=lambda got, total: seen.append((got, total)),
            sha256=hashlib.sha256(body).hexdigest(),
        )
        assert dest.read_bytes() == body
        assert seen[-1] == (len(body), len(body))


def _response(body: bytes, length: int | None = None):
    class _Response:
        headers = {"Content-Length": str(len(body) if length is None else length)}

        def read(self, n):
            return self._buf.read(n)

        def __enter__(self):
            self._buf = BytesIO(body)
            return self

        def __exit__(self, *args):
            return False

    return lambda *_a, **_k: _Response()


_URL = "https://github.com/zyjhandsome/VoiceInk/releases/download/v2.0.9/VoiceInk-Setup-2.0.9.exe"


class TestInstallerVerification:
    def test_verified_existing_installer_is_reused_without_network(self, tmp_path):
        import hashlib
        from unittest.mock import Mock

        body = b"verified installer"
        dest = tmp_path / "VoiceInk-Setup-2.0.9.exe"
        dest.write_bytes(body)
        opened = Mock(side_effect=AssertionError("must reuse the verified file"))
        progress = []
        download_installer(
            _URL, dest, opened, expected_size=len(body),
            sha256=hashlib.sha256(body).hexdigest(), reuse_existing=True,
            on_progress=lambda got, total: progress.append((got, total)),
        )
        opened.assert_not_called()
        assert dest.read_bytes() == body
        assert progress == [(len(body), len(body))]

    def test_modified_existing_installer_is_rechecked_and_replaced(self, tmp_path):
        import hashlib
        from unittest.mock import Mock

        body = b"verified installer"
        dest = tmp_path / "VoiceInk-Setup-2.0.9.exe"
        dest.write_bytes(b"x" * len(body))
        opened = Mock(side_effect=_response(body))
        download_installer(
            _URL, dest, opened, expected_size=len(body),
            sha256=hashlib.sha256(body).hexdigest(), reuse_existing=True,
        )
        opened.assert_called_once()
        assert dest.read_bytes() == body

    def test_existing_installer_never_bypasses_required_digest(self, tmp_path):
        import pytest
        from unittest.mock import Mock
        from voiceink.updater import MissingDigestError

        dest = tmp_path / "VoiceInk-Setup-2.0.9.exe"
        dest.write_bytes(b"unverified installer")
        opened = Mock()
        with pytest.raises(MissingDigestError):
            download_installer(_URL, dest, opened, reuse_existing=True)
        opened.assert_not_called()

    def test_release_carries_size_and_sha256_digest(self):
        payload = _payload("2.0.9", "VoiceInk-Setup-2.0.9.exe", _URL)
        payload["assets"][0]["size"] = 1234
        payload["assets"][0]["digest"] = "sha256:" + "ab" * 32
        info = release_from_payload(payload, current="2.0.8")
        assert info.size == 1234
        assert info.sha256 == "ab" * 32

    def test_malformed_digest_is_ignored(self):
        payload = _payload("2.0.9", "VoiceInk-Setup-2.0.9.exe", _URL)
        payload["assets"][0]["digest"] = "md5:xyz"
        assert release_from_payload(payload, current="2.0.8").sha256 == ""

    def test_truncated_download_is_rejected_and_leaves_no_file(self, tmp_path):
        import pytest
        from voiceink.updater import InstallerVerificationError

        dest = tmp_path / "VoiceInk-Setup-2.0.9.exe"
        with pytest.raises(InstallerVerificationError, match="不完整"):
            download_installer(_URL, dest, _response(b"half", length=100), sha256="00" * 32)
        assert not dest.exists()
        assert not (tmp_path / "VoiceInk-Setup-2.0.9.exe.part").exists()

    def test_missing_digest_is_rejected_before_downloading(self, tmp_path):
        import pytest
        from voiceink.updater import MissingDigestError

        opened = []
        dest = tmp_path / "VoiceInk-Setup-2.0.9.exe"
        with pytest.raises(MissingDigestError):
            download_installer(_URL, dest, lambda *a, **k: opened.append(a), sha256="")
        assert opened == []
        assert not dest.exists()

    def test_worker_reports_missing_digest_distinctly(self, tmp_path):
        from voiceink.updater import MISSING_DIGEST_MESSAGE, UpdateDownloadWorker

        worker = UpdateDownloadWorker(_URL, tmp_path / "x.exe", sha256="")
        failures = []
        worker.failed.connect(failures.append)
        worker.run()
        assert failures == [MISSING_DIGEST_MESSAGE]

    def test_app_does_not_start_download_without_digest(self):
        from tests.helpers.app_harness import app_harness
        from voiceink.updater import MISSING_DIGEST_MESSAGE, ReleaseInfo
        from unittest.mock import MagicMock, patch

        with app_harness() as h:
            app = h["app"]
            settings = MagicMock()
            app._settings_widget = lambda: settings
            app._pending_release = ReleaseInfo("2.0.9", "VoiceInk-Setup-2.0.9.exe", _URL)
            with patch("voiceink.updater.UpdateDownloadWorker") as worker_cls:
                app._on_update_install_requested()
            worker_cls.assert_not_called()
            settings.set_update_status.assert_called_with(MISSING_DIGEST_MESSAGE, action="check")

    def test_hash_mismatch_is_rejected(self, tmp_path):
        import pytest
        from voiceink.updater import InstallerVerificationError

        dest = tmp_path / "VoiceInk-Setup-2.0.9.exe"
        with pytest.raises(InstallerVerificationError):
            download_installer(_URL, dest, _response(b"body"), sha256="00" * 32)
        assert not dest.exists()

    def test_matching_hash_and_size_is_accepted(self, tmp_path):
        import hashlib

        body = b"real-installer"
        dest = tmp_path / "VoiceInk-Setup-2.0.9.exe"
        download_installer(
            _URL,
            dest,
            _response(body),
            expected_size=len(body),
            sha256=hashlib.sha256(body).hexdigest(),
        )
        assert dest.read_bytes() == body

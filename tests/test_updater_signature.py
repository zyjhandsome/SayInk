"""RFC 8032 Ed25519 detached signature verification tests for ``sayink.updater``."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
from urllib.error import HTTPError

import pytest

from sayink import updater
from sayink.updater import (
    INVALID_SIGNATURE_MESSAGE,
    PUBLIC_KEY_ENV_VAR,
    InstallerSignatureError,
    UpdateDownloadWorker,
    canonical_release_manifest,
    download_installer,
    parse_detached_signature,
    release_from_payload,
    resolve_signing_public_key,
    verify_ed25519_signature,
)
from sayink_build.release_signing import (
    PRIVATE_KEY_ENV_VAR,
    SigningError,
    ed25519_public_key_from_seed,
    ed25519_sign,
    format_detached_signature,
    generate_ed25519_keypair,
    main as signing_cli_main,
    sign_release_file,
    verify_release_file,
)


class _FakeHTTPResponse:
    def __init__(self, data: bytes, headers: dict[str, str] | None = None):
        self._buf = io.BytesIO(data)
        self.headers = headers or {"Content-Length": str(len(data))}

    def read(self, size: int = -1) -> bytes:
        return self._buf.read(size)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_rfc8032_section_7_1_official_vectors():
    # RFC 8032 §7.1 TEST 1 (empty message)
    seed1 = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
    pub1 = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
    sig1 = bytes.fromhex(
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e0652249015"
        "55fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
    )
    assert ed25519_public_key_from_seed(seed1) == pub1
    assert ed25519_sign(seed1, b"") == sig1
    assert verify_ed25519_signature(pub1, b"", sig1) is True
    assert verify_ed25519_signature(pub1, b"x", sig1) is False

    # RFC 8032 §7.1 TEST 2 (1-byte 0x72)
    seed2 = bytes.fromhex("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb")
    pub2 = bytes.fromhex("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c")
    sig2 = bytes.fromhex(
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
        "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"
    )
    assert ed25519_public_key_from_seed(seed2) == pub2
    assert ed25519_sign(seed2, b"\x72") == sig2
    assert verify_ed25519_signature(pub2, b"\x72", sig2) is True


def test_cross_verifies_with_openssl_cryptography_when_available():
    ed_mod = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
    raw_mod = pytest.importorskip("cryptography.hazmat.primitives._serialization")
    del raw_mod

    priv_hex, pub_hex = generate_ed25519_keypair()
    seed = bytes.fromhex(priv_hex)
    msg = canonical_release_manifest(
        asset_name="SayInk-Setup-2.3.0.exe",
        sha256="ab" * 32,
        size=84_934_656,
    )
    our_sig = ed25519_sign(seed, msg)

    ossl_priv = ed_mod.Ed25519PrivateKey.from_private_bytes(seed)
    ossl_pub = ossl_priv.public_key()
    ossl_pub.verify(our_sig, msg)

    ossl_sig = ossl_priv.sign(msg)
    assert ossl_sig == our_sig
    assert verify_ed25519_signature(bytes.fromhex(pub_hex), msg, ossl_sig) is True


def test_rejects_malleable_scalar_and_malformed_signature_inputs():
    priv_hex, pub_hex = generate_ed25519_keypair()
    pub = bytes.fromhex(pub_hex)
    sig = ed25519_sign(bytes.fromhex(priv_hex), b"hello")
    # S + L >= L must be rejected per RFC 8032 §5.1.7.
    s_int = int.from_bytes(sig[32:], "little") + updater._ED_L
    if s_int < (1 << 256):
        malleated = sig[:32] + s_int.to_bytes(32, "little")
        assert verify_ed25519_signature(pub, b"hello", malleated) is False

    assert verify_ed25519_signature(b"short", b"hello", sig) is False
    assert verify_ed25519_signature(pub, b"hello", b"short") is False

    with pytest.raises(InstallerSignatureError):
        parse_detached_signature("# comment only\n\n")
    with pytest.raises(InstallerSignatureError):
        parse_detached_signature("ed25519:not-hex")
    with pytest.raises(InstallerSignatureError):
        resolve_signing_public_key("not-a-64-char-hex-key")


def test_sign_and_verify_release_file_blocks_tampering_and_asset_renaming(tmp_path: Path):
    priv_hex, pub_hex = generate_ed25519_keypair()
    installer = tmp_path / "SayInk-Setup-2.3.0.exe"
    installer.write_bytes(b"MZ-fake-sayink-installer-payload")

    sig_path = sign_release_file(installer, priv_hex)
    assert sig_path == tmp_path / "SayInk-Setup-2.3.0.exe.sig"
    assert verify_release_file(installer, pub_hex) is True

    # Renaming a signed 2.3.0 binary to 2.4.0 with the same bytes & SHA-256 must fail!
    renamed = tmp_path / "SayInk-Setup-2.4.0.exe"
    renamed.write_bytes(installer.read_bytes())
    renamed_sig = tmp_path / "SayInk-Setup-2.4.0.exe.sig"
    renamed_sig.write_bytes(sig_path.read_bytes())
    assert verify_release_file(renamed, pub_hex) is False

    # Flipping a single byte in the installer invalidates verification.
    installer.write_bytes(b"MZ-fake-sayink-installer-payloaX")
    assert verify_release_file(installer, pub_hex) is False


def test_release_signing_cli_keygen_sign_and_verify(tmp_path: Path, capsys, monkeypatch):
    assert signing_cli_main(["keygen"]) == 0
    keygen_lines = dict(
        line.split("=", 1)
        for line in capsys.readouterr().out.strip().splitlines()
        if "=" in line
    )
    priv_hex = keygen_lines[PRIVATE_KEY_ENV_VAR]
    pub_hex = keygen_lines["RELEASE_SIGNING_PUBLIC_KEY"]

    asset = tmp_path / "SayInk-Setup-2.3.1.exe"
    asset.write_bytes(b"binary-content-for-cli-test")
    monkeypatch.setenv(PRIVATE_KEY_ENV_VAR, priv_hex)
    assert signing_cli_main(["sign", str(asset)]) == 0
    capsys.readouterr()

    assert signing_cli_main(["verify", str(asset), "--public-key", pub_hex]) == 0
    _, wrong_pub = generate_ed25519_keypair()
    assert signing_cli_main(["verify", str(asset), "--public-key", wrong_pub]) == 1

    with pytest.raises(SigningError):
        sign_release_file(tmp_path / "missing.exe", priv_hex)


def test_release_from_payload_discovers_trusted_detached_signature_asset():
    exe_url = "https://github.com/zyjhandsome/SayInk/releases/download/v2.4.0/SayInk-Setup-2.4.0.exe"
    sig_url = f"{exe_url}.sig"
    payload = {
        "tag_name": "v2.4.0",
        "assets": [
            {
                "name": "SayInk-Setup-2.4.0.exe",
                "browser_download_url": exe_url,
                "size": 1024,
                "digest": "sha256:" + ("11" * 32),
            },
            {
                "name": "SayInk-Setup-2.4.0.exe.sig",
                "browser_download_url": sig_url,
                "size": 162,
            },
        ],
    }
    info = release_from_payload(payload, current="2.3.0")
    assert info is not None
    assert info.signature_url == sig_url
    assert updater._KNOWN_SIGNATURE_URLS[exe_url] == sig_url

    # Untrusted HTTP .sig URL must never be accepted.
    payload["assets"][1]["browser_download_url"] = "http://evil.example/SayInk-Setup-2.4.0.exe.sig"
    info_untrusted = release_from_payload(payload, current="2.3.0")
    assert info_untrusted is not None
    assert info_untrusted.signature_url == ""
    assert exe_url not in updater._KNOWN_SIGNATURE_URLS


def test_download_installer_without_pinned_key_keeps_sha256_only_behavior(tmp_path: Path, monkeypatch):
    monkeypatch.delenv(PUBLIC_KEY_ENV_VAR, raising=False)
    monkeypatch.setattr(updater, "RELEASE_SIGNING_PUBLIC_KEY", "")

    body = b"unsigned-but-sha256-pinned-installer"
    digest = hashlib.sha256(body).hexdigest()
    exe_url = "https://github.com/zyjhandsome/SayInk/releases/download/v2.4.0/SayInk-Setup-2.4.0.exe"
    requested_urls: list[str] = []

    def fake_urlopen(req, timeout=60):
        del timeout
        requested_urls.append(req.full_url)
        if req.full_url == exe_url:
            return _FakeHTTPResponse(body)
        raise AssertionError(f"unexpected URL requested: {req.full_url}")

    dest = tmp_path / "SayInk-Setup-2.4.0.exe"
    download_installer(
        exe_url,
        dest,
        fake_urlopen,
        expected_size=len(body),
        sha256=digest,
    )
    assert dest.read_bytes() == body
    assert requested_urls == [exe_url]


def test_download_installer_verifies_ed25519_signature_and_caches_sidecar(tmp_path: Path):
    priv_hex, pub_hex = generate_ed25519_keypair()
    body = b"signed-installer-payload-v2.4.0"
    digest = hashlib.sha256(body).hexdigest()
    asset_name = "SayInk-Setup-2.4.0.exe"
    exe_url = f"https://github.com/zyjhandsome/SayInk/releases/download/v2.4.0/{asset_name}"
    sig_url = f"{exe_url}.sig"

    manifest = canonical_release_manifest(asset_name=asset_name, sha256=digest, size=len(body))
    sig_bytes = ed25519_sign(bytes.fromhex(priv_hex), manifest)
    sig_text = format_detached_signature(sig_bytes, asset_name).encode("utf-8")

    calls: list[str] = []

    def fake_urlopen(req, timeout=60):
        del timeout
        calls.append(req.full_url)
        if req.full_url == exe_url:
            return _FakeHTTPResponse(body)
        if req.full_url == sig_url:
            return _FakeHTTPResponse(sig_text)
        raise AssertionError(f"unexpected URL: {req.full_url}")

    dest = tmp_path / asset_name
    download_installer(
        exe_url,
        dest,
        fake_urlopen,
        expected_size=len(body),
        sha256=digest,
        public_key_hex=pub_hex,
    )
    assert dest.read_bytes() == body
    assert (tmp_path / f"{asset_name}.sig").is_file()
    assert calls == [exe_url, sig_url]

    # Reuse with valid cached .sig sidecar requires zero network requests.
    calls.clear()
    download_installer(
        exe_url,
        dest,
        fake_urlopen,
        expected_size=len(body),
        sha256=digest,
        reuse_existing=True,
        public_key_hex=pub_hex,
    )
    assert calls == []


def test_download_installer_rejects_forged_missing_or_untrusted_signature(tmp_path: Path):
    priv_hex, pub_hex = generate_ed25519_keypair()
    other_priv_hex, _ = generate_ed25519_keypair()
    del priv_hex

    body = b"compromised-release-binary"
    digest = hashlib.sha256(body).hexdigest()
    asset_name = "SayInk-Setup-2.4.0.exe"
    exe_url = f"https://github.com/zyjhandsome/SayInk/releases/download/v2.4.0/{asset_name}"
    sig_url = f"{exe_url}.sig"

    manifest = canonical_release_manifest(asset_name=asset_name, sha256=digest, size=len(body))
    wrong_sig = format_detached_signature(
        ed25519_sign(bytes.fromhex(other_priv_hex), manifest),
        asset_name,
    ).encode("utf-8")

    def forged_urlopen(req, timeout=60):
        del timeout
        if req.full_url == exe_url:
            return _FakeHTTPResponse(body)
        if req.full_url == sig_url:
            return _FakeHTTPResponse(wrong_sig)
        raise AssertionError(req.full_url)

    dest = tmp_path / asset_name
    with pytest.raises(InstallerSignatureError):
        download_installer(
            exe_url,
            dest,
            forged_urlopen,
            expected_size=len(body),
            sha256=digest,
            public_key_hex=pub_hex,
        )
    assert not dest.exists()
    assert not (tmp_path / f"{asset_name}.part").exists()

    # Cached unsigned file must NOT bypass signature verification when 404 is returned for .sig!
    dest.write_bytes(body)

    def missing_sig_urlopen(req, timeout=60):
        del timeout
        if req.full_url == sig_url:
            raise HTTPError(sig_url, 404, "Not Found", hdrs=None, fp=None)
        return _FakeHTTPResponse(body)

    with pytest.raises(InstallerSignatureError):
        download_installer(
            exe_url,
            dest,
            missing_sig_urlopen,
            expected_size=len(body),
            sha256=digest,
            reuse_existing=True,
            public_key_hex=pub_hex,
        )

    # Untrusted signature_url fails immediately before downloading anything.
    with pytest.raises(InstallerSignatureError):
        download_installer(
            exe_url,
            tmp_path / "other.exe",
            lambda *_a, **_k: pytest.fail("must not download"),
            expected_size=len(body),
            sha256=digest,
            signature_url="https://attacker.example/SayInk-Setup-2.4.0.exe.sig",
            public_key_hex=pub_hex,
        )


def test_update_download_worker_surfaces_distinct_signature_error_message(tmp_path: Path, monkeypatch):
    _, pub_hex = generate_ed25519_keypair()
    body = b"worker-signature-failure-payload"
    digest = hashlib.sha256(body).hexdigest()
    exe_url = "https://github.com/zyjhandsome/SayInk/releases/download/v2.4.0/SayInk-Setup-2.4.0.exe"

    def fake_urlopen(req, timeout=60):
        del timeout
        if req.full_url == exe_url:
            return _FakeHTTPResponse(body)
        raise HTTPError(req.full_url, 404, "Not Found", hdrs=None, fp=None)

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    errors: list[str] = []
    finished: list[str] = []
    worker = UpdateDownloadWorker(
        exe_url,
        tmp_path / "SayInk-Setup-2.4.0.exe",
        expected_size=len(body),
        sha256=digest,
        public_key_hex=pub_hex,
    )
    worker.failed.connect(errors.append)
    worker.finished_path.connect(finished.append)
    worker.run()

    assert finished == []
    assert errors == [INVALID_SIGNATURE_MESSAGE]

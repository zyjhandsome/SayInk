"""Check GitHub releases and download the Windows installer on request.

A daily check only asks for the latest version. The installer is downloaded
when the user chooses 下载并安装.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import types
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
import urllib.request as _urllib_request
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

_STDLIB_URLOPEN = _urllib_request.urlopen

from PyQt6.QtCore import QThread, pyqtSignal

log = logging.getLogger("SayInk")

GITHUB_REPO = "zyjhandsome/SayInk"
LATEST_RELEASE_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
ISSUES_URL = f"https://github.com/{GITHUB_REPO}/issues/new/choose"
USER_AGENT = "SayInk"
CHECK_INTERVAL_SEC = 24 * 60 * 60
# Hex-encoded 32-byte RFC 8032 Ed25519 public key for detached ``.sig`` release
# verification. Empty until pinned for official releases; can also be supplied via
# ``SAYINK_UPDATE_PUBLIC_KEY`` in enterprise/test environments.
RELEASE_SIGNING_PUBLIC_KEY = ""
PUBLIC_KEY_ENV_VAR = "SAYINK_UPDATE_PUBLIC_KEY"
_MAX_SIGNATURE_BYTES = 4096
# Accept "x.y.z" / "x.y" and optional pre-release tags (-alpha/-beta/-rc/-dev).
_VERSION_RE = re.compile(
    r"(\d+)\.(\d+)(?:\.(\d+))?(?:[-._]?(alpha|a|beta|b|rc|pre|preview|dev)[.-]?(\d*))?(?![\d.])",
    re.IGNORECASE,
)
_PRERELEASE_STAGE_RANK = {
    "dev": 0,
    "alpha": 1,
    "a": 1,
    "beta": 2,
    "b": 2,
    "pre": 2,
    "preview": 2,
    "rc": 3,
}
_FINAL_STAGE_RANK = 4
_PROGRESS_THROTTLE_SEC = 0.1
_PROGRESS_THROTTLE_BYTES = 1024 * 1024
_TRUSTED_HOSTS = frozenset(
    {
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
        "github-releases.githubusercontent.com",
    }
)
_KNOWN_SIGNATURE_URLS: dict[str, str] = {}
_SIGNATURE_URLS_LOCK = threading.Lock()

# RFC 8032 Ed25519 curve parameters (GF(2^255 - 19), pure stdlib verification).
_ED_P = 2**255 - 19
_ED_L = 2**252 + 27742317777372353535851937790883648493
_ED_D = (-121665 * pow(121666, _ED_P - 2, _ED_P)) % _ED_P
_ED_I = pow(2, (_ED_P - 1) // 4, _ED_P)


def _ed_xrecover(y: int) -> int:
    xx = (y * y - 1) * pow(_ED_D * y * y + 1, _ED_P - 2, _ED_P) % _ED_P
    x = pow(xx, (_ED_P + 3) // 8, _ED_P)
    if (x * x - xx) % _ED_P != 0:
        x = (x * _ED_I) % _ED_P
    return _ED_P - x if x & 1 else x


_ED_BY = (4 * pow(5, _ED_P - 2, _ED_P)) % _ED_P
_ED_BX = _ed_xrecover(_ED_BY)
_ED_B = (_ED_BX, _ED_BY, 1, (_ED_BX * _ED_BY) % _ED_P)


def _ed_add(p1: tuple[int, int, int, int], p2: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    x1, y1, z1, t1 = p1
    x2, y2, z2, t2 = p2
    a = (y1 - x1) * (y2 - x2) % _ED_P
    b = (y1 + x1) * (y2 + x2) % _ED_P
    c = (2 * _ED_D * t1 * t2) % _ED_P
    d = (2 * z1 * z2) % _ED_P
    e = (b - a) % _ED_P
    f = (d - c) % _ED_P
    g = (d + c) % _ED_P
    h = (b + a) % _ED_P
    return (e * f % _ED_P, g * h % _ED_P, f * g % _ED_P, e * h % _ED_P)


def _ed_double(pt: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    x1, y1, z1, _ = pt
    a = (x1 * x1) % _ED_P
    b = (y1 * y1) % _ED_P
    c = (2 * z1 * z1) % _ED_P
    h = (a + b) % _ED_P
    e = (h - (x1 + y1) * (x1 + y1)) % _ED_P
    g = (a - b) % _ED_P
    f = (c + g) % _ED_P
    return (e * f % _ED_P, g * h % _ED_P, f * g % _ED_P, e * h % _ED_P)


def _ed_mul(pt: tuple[int, int, int, int], scalar: int) -> tuple[int, int, int, int]:
    res = (0, 1, 1, 0)
    addend = pt
    n = scalar
    while n > 0:
        if n & 1:
            res = _ed_add(res, addend)
        addend = _ed_double(addend)
        n >>= 1
    return res


def _ed_encode_point(pt: tuple[int, int, int, int]) -> bytes:
    x, y, z, _ = pt
    zi = pow(z, _ED_P - 2, _ED_P)
    xp = (x * zi) % _ED_P
    yp = (y * zi) % _ED_P
    out = bytearray(yp.to_bytes(32, "little"))
    out[31] |= (xp & 1) << 7
    return bytes(out)


def _ed_decode_point(encoded: bytes) -> tuple[int, int, int, int] | None:
    if len(encoded) != 32:
        return None
    y = int.from_bytes(encoded, "little") & ((1 << 255) - 1)
    sign = encoded[31] >> 7
    if y >= _ED_P:
        return None
    u = (y * y - 1) % _ED_P
    v = (_ED_D * y * y + 1) % _ED_P
    xx = (u * pow(v, _ED_P - 2, _ED_P)) % _ED_P
    x = pow(xx, (_ED_P + 3) // 8, _ED_P)
    if (x * x - xx) % _ED_P != 0:
        x = (x * _ED_I) % _ED_P
    if (x * x - xx) % _ED_P != 0:
        return None
    if x == 0 and sign == 1:
        return None
    if (x & 1) != sign:
        x = _ED_P - x
    return (x, y, 1, (x * y) % _ED_P)


def verify_ed25519_signature(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """RFC 8032 §5.1.7 cofactored Ed25519 verification using stdlib sha512."""
    import hashlib

    if len(public_key) != 32 or len(signature) != 64:
        return False
    pub_pt = _ed_decode_point(public_key)
    r_pt = _ed_decode_point(signature[:32])
    if pub_pt is None or r_pt is None:
        return False
    s_int = int.from_bytes(signature[32:], "little")
    if s_int >= _ED_L:
        return False
    k_int = int.from_bytes(hashlib.sha512(signature[:32] + public_key + message).digest(), "little") % _ED_L
    lhs = _ed_mul(_ED_B, s_int)
    rhs = _ed_add(r_pt, _ed_mul(pub_pt, k_int))
    for _ in range(3):
        lhs = _ed_double(lhs)
        rhs = _ed_double(rhs)
    return _ed_encode_point(lhs) == _ed_encode_point(rhs)


def canonical_release_manifest(*, asset_name: str, sha256: str, size: int) -> bytes:
    """Domain-separated release manifest binding filename, SHA-256, and byte size."""
    clean_name = Path(asset_name).name
    digest = (sha256 or "").strip().lower()
    if not clean_name or "\n" in clean_name or "\r" in clean_name:
        raise ValueError("安装包文件名无效")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("SHA-256 格式无效")
    if size < 0:
        raise ValueError("安装包大小无效")
    return f"sayink-release-v1\nname:{clean_name}\nsha256:{digest}\nsize:{size}\n".encode("ascii")


def parse_detached_signature(raw: str | bytes) -> bytes:
    """Parse ``ed25519:<128-hex>`` or ``<128-hex>`` (optional trailing filename)."""
    text = raw.decode("utf-8", errors="strict") if isinstance(raw, bytes) else str(raw or "")
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if not lines:
        raise InstallerSignatureError("签名文件为空")
    token = lines[0].split()[0]
    if token.lower().startswith("ed25519:"):
        token = token.split(":", 1)[1]
    if not re.fullmatch(r"[0-9a-fA-F]{128}", token):
        raise InstallerSignatureError("签名格式无效")
    return bytes.fromhex(token)


def resolve_signing_public_key(public_key_hex: str | None = None) -> bytes | None:
    """Return 32-byte Ed25519 public key when configured, or ``None`` when unpinned."""
    raw = (
        public_key_hex
        if public_key_hex is not None
        else (os.environ.get(PUBLIC_KEY_ENV_VAR, "") or RELEASE_SIGNING_PUBLIC_KEY)
    )
    cleaned = (raw or "").strip()
    if cleaned.lower().startswith("ed25519:"):
        cleaned = cleaned.split(":", 1)[1].strip()
    if not cleaned:
        return None
    if not re.fullmatch(r"[0-9a-fA-F]{64}", cleaned):
        raise InstallerSignatureError("更新签名公钥格式无效")
    key_bytes = bytes.fromhex(cleaned)
    if _ed_decode_point(key_bytes) is None:
        raise InstallerSignatureError("更新签名公钥不是有效的 Ed25519 点")
    return key_bytes


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    asset_name: str
    asset_url: str
    size: int = 0
    sha256: str = ""
    signature_url: str = ""


def asset_sha256(asset: dict) -> str:
    """GitHub publishes ``digest: "sha256:<hex>"`` for release assets."""
    digest = str(asset.get("digest") or "").strip().lower()
    if digest.startswith("sha256:"):
        value = digest.split(":", 1)[1]
        if re.fullmatch(r"[0-9a-f]{64}", value):
            return value
    return ""


def version_key(text: str) -> tuple[int, int, int, int, int]:
    raw = (text or "").strip()
    match = _VERSION_RE.search(raw)
    if not match:
        return (0, 0, 0, 0, 0)
    major = int(match.group(1))
    minor = int(match.group(2))
    patch = int(match.group(3) or 0)
    stage_raw = (match.group(4) or "").lower()
    stage_num = int(match.group(5) or 0)
    if stage_raw:
        stage_rank = _PRERELEASE_STAGE_RANK.get(stage_raw, 0)
    else:
        trailing = raw[match.end():].strip()
        # Any other "-suffix" (except build metadata "+...") is treated as pre-release
        # unless it is empty, so 2.3.0 > 2.3.0-custom.
        if trailing.startswith("-"):
            stage_rank = 0
        else:
            stage_rank = _FINAL_STAGE_RANK
    return (major, minor, patch, stage_rank, stage_num)


def is_newer(remote: str, current: str) -> bool:
    return version_key(remote) > version_key(current)


def is_trusted_installer_url(url: str) -> bool:
    parsed = urlparse((url or "").strip())
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and host in _TRUSTED_HOSTS


def should_auto_check(
    *,
    enabled: bool,
    last_check_at: float,
    now: float,
    interval_sec: float = CHECK_INTERVAL_SEC,
) -> bool:
    if not enabled:
        return False
    if last_check_at <= 0:
        return True
    return (now - last_check_at) >= interval_sec


def _version_token_in_name(name: str, version: str) -> bool:
    """True when ``version`` is a whole number in the filename.

    ``2.2.1`` must not match ``SayInk-Setup-2.2.10.exe`` or ``SayInk-Setup-12.2.1.exe``.
    """
    if not version:
        return False
    return re.search(rf"(?<!\d){re.escape(version)}(?!\d)", name) is not None


def pick_installer_asset(assets: list[dict], version: str) -> dict | None:
    """Prefer ``SayInk-Setup-<version>.exe`` — the lite installer without
    Fun-ASR-Nano. CAM++ and Silero VAD travel with it; an already installed
    speech model stays in ``{app}\\models`` or ``~/.sayink/models``. Otherwise
    fall back to a setup whose name still carries ``<version>`` (e.g.
    ``SayInk-Setup-2.2.1-x64.exe`` or the ``-full`` installer).

    A setup for a different version is never offered, so a stray old installer
    attached to a new release cannot be installed and re-prompted forever.
    """
    wanted = f"SayInk-Setup-{version}.exe"
    exact = None
    fallback = None
    for asset in assets:
        name = str(asset.get("name") or "")
        url = str(asset.get("browser_download_url") or "")
        if not name.lower().endswith(".exe") or not is_trusted_installer_url(url):
            continue
        if name == wanted:
            exact = asset
        elif (
            fallback is None
            and name.startswith("SayInk-Setup-")
            and _version_token_in_name(name, version)
        ):
            fallback = asset
    return exact or fallback


def _pick_signature_asset_url(assets: list[dict], asset_name: str) -> str:
    if not asset_name:
        return ""
    wanted = f"{asset_name}.sig"
    for item in assets:
        if str(item.get("name") or "") != wanted:
            continue
        sig_url = str(item.get("browser_download_url") or "")
        if is_trusted_installer_url(sig_url):
            return sig_url
    return ""


def release_from_payload(payload: dict, *, current: str) -> ReleaseInfo | None:
    version = str(payload.get("tag_name") or "").strip().lstrip("vV")
    if not version or not is_newer(version, current):
        return None
    assets = list(payload.get("assets") or [])
    asset = pick_installer_asset(assets, version)
    if asset is None:
        return None
    try:
        size = int(asset.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    asset_name = str(asset.get("name") or "")
    asset_url = str(asset.get("browser_download_url") or "")
    sig_url = _pick_signature_asset_url(assets, asset_name)
    with _SIGNATURE_URLS_LOCK:
        if asset_url and sig_url:
            if len(_KNOWN_SIGNATURE_URLS) >= 32 and asset_url not in _KNOWN_SIGNATURE_URLS:
                _KNOWN_SIGNATURE_URLS.pop(next(iter(_KNOWN_SIGNATURE_URLS)))
            _KNOWN_SIGNATURE_URLS[asset_url] = sig_url
        elif asset_url:
            _KNOWN_SIGNATURE_URLS.pop(asset_url, None)
    return ReleaseInfo(
        version=version,
        asset_name=asset_name,
        asset_url=asset_url,
        size=max(0, size),
        sha256=asset_sha256(asset),
        signature_url=sig_url,
    )


def _request(url: str) -> Request:
    return Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github+json",
        },
    )


class UpdateError(URLError):
    """Raised when an update check or installer download hits an untrusted redirect."""


class _ProxyHopSwitch(Exception):
    """Internal signal when a redirect target crosses a proxy-bypass boundary."""

    def __init__(self, request: Request) -> None:
        super().__init__()
        self.request = request


def _is_trusted_api_redirect_url(url: str) -> bool:
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    return parsed.scheme.lower() == "https" and (
        host in _TRUSTED_HOSTS or host == "api.github.com"
    )


class _TrustedRedirectHandler(HTTPRedirectHandler):
    """Validate every 301/302/303/307/308 redirect target before following."""

    def __init__(
        self,
        *,
        trusted_only: bool = False,
        direct: bool = False,
        force_direct: bool = False,
        bypass_suffixes: str = "",
    ) -> None:
        super().__init__()
        self._trusted_only = trusted_only
        self._direct = direct
        self._force_direct = force_direct
        self._bypass_suffixes = bypass_suffixes

    def _validate_target(self, target_url: str, fp=None) -> None:
        trusted = (
            is_trusted_installer_url(target_url)
            if self._trusted_only
            else _is_trusted_api_redirect_url(target_url)
        )
        if not trusted:
            if fp is not None:
                try:
                    fp.close()
                except Exception:
                    pass
            raise UpdateError(f"拒绝重定向到不受信任的地址: {target_url}")

    @staticmethod
    def _extract_location(headers) -> str | None:
        if headers is None:
            return None
        for key in ("location", "Location", "uri", "URI"):
            val = headers.get(key)
            if val:
                return str(val)
        if hasattr(headers, "items"):
            for k, v in headers.items():
                if str(k).lower() in ("location", "uri") and v:
                    return str(v)
        return None

    def http_error_302(self, req, fp, code, msg, headers):
        location = self._extract_location(headers)
        if location:
            resolved = urljoin(req.full_url, location.strip())
            self._validate_target(resolved, fp=fp)
            if not self._force_direct and self._bypass_suffixes:
                from sayink.text_polisher import bypasses_env_proxy

                next_direct = bypasses_env_proxy(
                    "system", resolved, self._bypass_suffixes
                )
                if next_direct != self._direct:
                    new_req = self.redirect_request(req, fp, code, msg, headers, resolved)
                    if fp is not None:
                        try:
                            fp.close()
                        except Exception:
                            pass
                    if new_req is not None:
                        raise _ProxyHopSwitch(new_req)
        return super().http_error_302(req, fp, code, msg, headers)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self._validate_target(newurl, fp=fp)
        if code == 308:
            method = req.get_method()
            if method not in ("GET", "HEAD"):
                raise HTTPError(req.full_url, code, msg, headers, fp)
            new_headers = {
                k: v
                for k, v in req.headers.items()
                if k.lower() not in ("content-length", "content-type")
            }
            return Request(
                newurl.replace(" ", "%20"),
                headers=new_headers,
                origin_req_host=req.origin_req_host,
                unverifiable=True,
                method=method,
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def build_update_urlopen(
    proxy_mode: str = "system",
    *,
    proxy_bypass_suffixes: str = "",
    trusted_only: bool = False,
):
    """Build a ``urlopen(request, timeout=...)`` callable honoring ``llm.proxy_mode``
    and ``llm.proxy_bypass_suffixes`` while validating redirect targets."""
    mode = (proxy_mode or "system").strip().lower()
    suffixes = (proxy_bypass_suffixes or "").strip()
    force_direct = mode in ("direct", "none", "off", "bypass")

    def _open(req, timeout: float = 30):
        from sayink.text_polisher import bypasses_env_proxy

        current_req = req if isinstance(req, Request) else _request(str(req))
        for _hop in range(HTTPRedirectHandler.max_repeats):
            target_url = current_req.full_url
            use_direct = force_direct or (
                bool(suffixes) and bypasses_env_proxy(mode, target_url, suffixes)
            )
            redirect_handler = _TrustedRedirectHandler(
                trusted_only=trusted_only,
                direct=use_direct,
                force_direct=force_direct,
                bypass_suffixes=suffixes,
            )
            if use_direct:
                opener = build_opener(ProxyHandler({}), redirect_handler)
            elif _urllib_request.urlopen is not _STDLIB_URLOPEN:
                return _urllib_request.urlopen(current_req, timeout=timeout)
            else:
                opener = build_opener(redirect_handler)
            try:
                return opener.open(current_req, timeout=timeout)
            except _ProxyHopSwitch as hop:
                current_req = hop.request
        raise UpdateError("更新请求重定向次数过多")

    if not trusted_only:
        _open.__dict__["_sayink_with_trusted_only"] = lambda: build_update_urlopen(
            proxy_mode,
            proxy_bypass_suffixes=proxy_bypass_suffixes,
            trusted_only=True,
        )
    return _open


def cleanup_stale_installers(cache_dir: Path, *, keep_asset_name: str = "") -> int:
    """Remove outdated ``SayInk-Setup-*`` installers/signatures/parts in ``%TEMP%\\SayInk``."""
    if not cache_dir.is_dir():
        return 0
    keep: set[str] = set()
    if keep_asset_name:
        keep.add(keep_asset_name)
        keep.add(f"{keep_asset_name}.sig")
    removed = 0
    try:
        entries = list(cache_dir.iterdir())
    except OSError:
        return 0
    for entry in entries:
        name = entry.name
        if name in keep or not name.startswith("SayInk-Setup-"):
            continue
        if not name.endswith((".exe", ".exe.sig", ".exe.part")):
            continue
        try:
            if entry.is_file():
                entry.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def fetch_latest_release(
    urlopen=None,
    *,
    current: str,
    proxy_mode: str = "system",
    proxy_bypass_suffixes: str = "",
) -> ReleaseInfo | None:
    opener_fn = urlopen or build_update_urlopen(
        proxy_mode,
        proxy_bypass_suffixes=proxy_bypass_suffixes,
    )
    with opener_fn(_request(LATEST_RELEASE_URL), timeout=15) as response:
        final_url = getattr(response, "geturl", lambda: "")()
        if isinstance(final_url, str) and final_url and not _is_trusted_api_redirect_url(final_url):
            raise UpdateError(f"拒绝不受信任的更新检查响应地址: {final_url}")
        payload = json.loads(response.read().decode("utf-8"))
    return release_from_payload(payload, current=current)


def _shell_execute_runas(executable: str, parameters: str) -> None:
    import ctypes
    from ctypes import wintypes

    current_shell = getattr(getattr(ctypes, "windll", None), "shell32", None)
    if (
        sys.platform == "win32"
        and hasattr(ctypes, "WinDLL")
        and isinstance(current_shell, ctypes.WinDLL)
    ):
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    else:
        shell32 = current_shell
    shell_exec = shell32.ShellExecuteW
    if hasattr(shell_exec, "argtypes"):
        shell_exec.argtypes = [
            wintypes.HWND,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            ctypes.c_int,
        ]
        shell_exec.restype = ctypes.c_ssize_t
    ret = int(shell_exec(None, "runas", executable, parameters, None, 1))
    if ret <= 32:
        raise OSError(f"安装包提权启动被拒绝或失败 (ShellExecuteW={ret})")


def launch_installer(path: str, *, wait_pid: int | None = None) -> None:
    """Start the setup program with PID handshake so it waits for clean exit."""
    target = str(path)
    pid = int(os.getpid() if wait_pid is None else wait_pid)
    pid_arg = f"/WAITPID={pid}"
    env = os.environ.copy()
    env["SAYINK_UPDATE_WAIT_PID"] = str(pid)
    kwargs: dict = {"env": env}
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen([target, pid_arg], **kwargs)
    except OSError as exc:
        if sys.platform == "win32" and getattr(exc, "winerror", None) == 740:
            _shell_execute_runas(target, pid_arg)
            return
        raise


class InstallerVerificationError(ValueError):
    pass


class MissingDigestError(InstallerVerificationError):
    """The release publishes no SHA-256, so the installer cannot be verified."""


class InstallerSignatureError(InstallerVerificationError):
    """The release signature is missing, malformed, or fails Ed25519 verification."""


MISSING_DIGEST_MESSAGE = "该版本未提供安装包校验值，为安全起见不自动安装，请到 GitHub 发布页手动下载"
INVALID_SIGNATURE_MESSAGE = "安装包数字签名校验失败，已停止自动安装，请到 GitHub 发布页核实"


def _ensure_trusted_installer_urlopen(urlopen):
    if isinstance(urlopen, types.FunctionType):
        upgrade = urlopen.__dict__.get("_sayink_with_trusted_only")
        if callable(upgrade):
            return upgrade()
    return urlopen


def _fetch_and_verify_signature(
    urlopen,
    sig_url: str,
    *,
    pub_key: bytes,
    asset_name: str,
    sha256: str,
    size: int,
) -> bytes:
    if not is_trusted_installer_url(sig_url):
        raise InstallerSignatureError("安装包签名地址不受信任")
    trusted_urlopen = _ensure_trusted_installer_urlopen(urlopen)
    try:
        with trusted_urlopen(_request(sig_url), timeout=30) as sig_resp:
            final_sig_url = getattr(sig_resp, "geturl", lambda: "")()
            if (
                isinstance(final_sig_url, str)
                and final_sig_url
                and not is_trusted_installer_url(final_sig_url)
            ):
                raise UpdateError(f"拒绝不受信任的安装包签名响应地址: {final_sig_url}")
            raw_sig = sig_resp.read(_MAX_SIGNATURE_BYTES + 1)
    except (InstallerSignatureError, UpdateError):
        raise
    except Exception as exc:
        raise InstallerSignatureError(f"无法下载安装包签名: {exc}") from exc
    if len(raw_sig) > _MAX_SIGNATURE_BYTES:
        raise InstallerSignatureError("安装包签名文件过大")
    try:
        sig_bytes = parse_detached_signature(raw_sig)
    except UnicodeDecodeError as exc:
        raise InstallerSignatureError("签名文件编码无效") from exc
    manifest = canonical_release_manifest(
        asset_name=asset_name,
        sha256=sha256,
        size=size,
    )
    if not verify_ed25519_signature(pub_key, manifest, sig_bytes):
        raise InstallerSignatureError("安装包 Ed25519 数字签名不匹配")
    return f"ed25519:{sig_bytes.hex()}  {Path(asset_name).name}\n".encode("ascii")


def download_installer(
    url: str,
    dest: Path,
    urlopen=None,
    on_progress=None,
    *,
    expected_size: int = 0,
    sha256: str = "",
    reuse_existing: bool = False,
    signature_url: str = "",
    public_key_hex: str | None = None,
    proxy_mode: str = "system",
    proxy_bypass_suffixes: str = "",
) -> None:
    """Download to ``<dest>.part`` and only rename after size/hash/signature checks pass.

    A published SHA-256 is mandatory: without it nothing is downloaded.
    When an Ed25519 public key is pinned (via ``RELEASE_SIGNING_PUBLIC_KEY``,
    ``SAYINK_UPDATE_PUBLIC_KEY``, or ``public_key_hex``), a valid detached
    ``.sig`` over ``canonical_release_manifest`` is also required.
    Reusing an installer always rechecks its complete contents first.
    """
    import hashlib

    if not is_trusted_installer_url(url):
        raise ValueError("安装包地址不受信任")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", sha256 or ""):
        raise MissingDigestError("发布未提供有效的 SHA-256 校验值")
    trusted_urlopen = (
        build_update_urlopen(
            proxy_mode,
            proxy_bypass_suffixes=proxy_bypass_suffixes,
            trusted_only=True,
        )
        if urlopen is None
        else _ensure_trusted_installer_urlopen(urlopen)
    )
    pub_key = resolve_signing_public_key(public_key_hex)
    resolved_sig_url = ""
    if pub_key is not None:
        with _SIGNATURE_URLS_LOCK:
            known_sig_url = _KNOWN_SIGNATURE_URLS.get(url, "")
        resolved_sig_url = (
            signature_url or known_sig_url or f"{url}.sig"
        ).strip()
        if not is_trusted_installer_url(resolved_sig_url):
            raise InstallerSignatureError("安装包签名地址不受信任")
    normalized_sha = sha256.lower()
    sig_dest = dest.with_name(dest.name + ".sig")

    def _atomic_write_sig(payload: bytes) -> None:
        sig_part = sig_dest.with_name(sig_dest.name + ".part")
        try:
            sig_part.write_bytes(payload)
            os.replace(sig_part, sig_dest)
        except BaseException:
            try:
                sig_part.unlink()
            except OSError:
                pass
            raise

    if reuse_existing and dest.is_file():
        try:
            size = dest.stat().st_size
            if not expected_size or size == expected_size:
                cached_hash = hashlib.sha256()
                with dest.open("rb") as cached:
                    for chunk in iter(lambda: cached.read(256 * 1024), b""):
                        cached_hash.update(chunk)
                if cached_hash.hexdigest() == normalized_sha:
                    if pub_key is not None:
                        manifest = canonical_release_manifest(
                            asset_name=dest.name,
                            sha256=normalized_sha,
                            size=size,
                        )
                        sidecar_ok = False
                        if sig_dest.is_file():
                            try:
                                cached_sig = parse_detached_signature(sig_dest.read_bytes())
                                sidecar_ok = verify_ed25519_signature(pub_key, manifest, cached_sig)
                            except (OSError, UnicodeDecodeError, InstallerSignatureError):
                                log.info("已缓存的签名文件无效，重新拉取远程签名")
                        if not sidecar_ok:
                            verified_sig = _fetch_and_verify_signature(
                                trusted_urlopen,
                                resolved_sig_url,
                                pub_key=pub_key,
                                asset_name=dest.name,
                                sha256=normalized_sha,
                                size=size,
                            )
                            _atomic_write_sig(verified_sig)
                    if on_progress is not None:
                        on_progress(size, size)
                    return
        except OSError:
            log.info("已下载的安装包无法读取，重新下载")
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    hasher = hashlib.sha256()
    try:
        with trusted_urlopen(_request(url), timeout=60) as response:
            final_url = getattr(response, "geturl", lambda: "")()
            if (
                isinstance(final_url, str)
                and final_url
                and not is_trusted_installer_url(final_url)
            ):
                raise UpdateError(f"拒绝不受信任的安装包响应地址: {final_url}")
            total = int(response.headers.get("Content-Length") or 0) or expected_size
            want = expected_size or total
            got = 0
            with part.open("wb") as handle:
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    got += len(chunk)
                    if want and got > want:
                        raise InstallerVerificationError(f"安装包大小超过预期（{got} / {want} 字节）")
                    handle.write(chunk)
                    hasher.update(chunk)
                    if on_progress is not None:
                        on_progress(got, total)
        if want and got != want:
            raise InstallerVerificationError(f"安装包不完整（{got} / {want} 字节）")
        if hasher.hexdigest() != normalized_sha:
            raise InstallerVerificationError("安装包校验值不符")
        if pub_key is not None:
            verified_sig = _fetch_and_verify_signature(
                trusted_urlopen,
                resolved_sig_url,
                pub_key=pub_key,
                asset_name=dest.name,
                sha256=normalized_sha,
                size=got,
            )
            _atomic_write_sig(verified_sig)
        os.replace(part, dest)
    except BaseException:
        try:
            part.unlink()
        except OSError:
            pass
        raise


class UpdateCheckWorker(QThread):
    result_ready = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(
        self,
        current: str,
        parent=None,
        *,
        proxy_mode: str = "system",
        proxy_bypass_suffixes: str = "",
    ):
        super().__init__(parent)
        self._current = current
        self._proxy_mode = proxy_mode
        self._proxy_bypass_suffixes = proxy_bypass_suffixes

    def run(self) -> None:
        urlopen = build_update_urlopen(
            self._proxy_mode,
            proxy_bypass_suffixes=self._proxy_bypass_suffixes,
        )

        try:
            self.result_ready.emit(fetch_latest_release(urlopen, current=self._current))
        except Exception as exc:
            log.info("检查更新失败: %s", exc)
            self.failed.emit("检查失败，请稍后再试")


class UpdateDownloadWorker(QThread):
    progress = pyqtSignal(int, int)
    finished_path = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(
        self,
        url: str,
        dest: Path,
        parent=None,
        *,
        expected_size: int = 0,
        sha256: str = "",
        signature_url: str = "",
        public_key_hex: str | None = None,
        proxy_mode: str = "system",
        proxy_bypass_suffixes: str = "",
    ):
        super().__init__(parent)
        self._url = url
        self._dest = dest
        self._expected_size = expected_size
        self._sha256 = sha256
        self._signature_url = signature_url
        self._public_key_hex = public_key_hex
        self._proxy_mode = proxy_mode
        self._proxy_bypass_suffixes = proxy_bypass_suffixes
        self._cancelled = False
        self._last_progress_at = 0.0
        self._last_progress_got = -1

    def cancel(self) -> None:
        self._cancelled = True

    def _on_progress(self, got: int, total: int) -> None:
        if self._cancelled:
            raise RuntimeError("更新下载已取消")
        now = time.monotonic()
        is_first = self._last_progress_got < 0
        is_done = total > 0 and got >= total
        if (
            is_first
            or is_done
            or got <= 0
            or (now - self._last_progress_at) >= _PROGRESS_THROTTLE_SEC
            or (got - self._last_progress_got) >= _PROGRESS_THROTTLE_BYTES
        ):
            self._last_progress_at = now
            self._last_progress_got = got
            self.progress.emit(got, total)

    def run(self) -> None:
        urlopen = build_update_urlopen(
            self._proxy_mode,
            proxy_bypass_suffixes=self._proxy_bypass_suffixes,
            trusted_only=True,
        )

        if self._cancelled:
            return
        try:
            download_installer(
                self._url,
                self._dest,
                urlopen,
                on_progress=self._on_progress,
                expected_size=self._expected_size,
                sha256=self._sha256,
                reuse_existing=True,
                signature_url=self._signature_url,
                public_key_hex=self._public_key_hex,
            )
            if not self._cancelled:
                self.finished_path.emit(str(self._dest))
        except MissingDigestError as exc:
            log.warning("更新包缺少校验值: %s", exc)
            self.failed.emit(MISSING_DIGEST_MESSAGE)
        except InstallerSignatureError as exc:
            log.warning("更新包数字签名校验失败: %s", exc)
            self.failed.emit(INVALID_SIGNATURE_MESSAGE)
        except InstallerVerificationError as exc:
            log.warning("更新包校验失败: %s", exc)
            self.failed.emit("下载的安装包不完整或已损坏，请重试")
        except UpdateError as exc:
            log.warning("更新包重定向校验失败: %s", exc)
            self.failed.emit("更新地址不受信任，已停止下载")
        except Exception as exc:
            if self._cancelled:
                return
            log.info("下载更新失败: %s", exc)
            self.failed.emit("下载失败，请稍后再试")

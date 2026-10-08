"""Check GitHub releases and download the Windows installer on request.

A daily check only asks for the latest version. The installer is downloaded
when the user chooses 下载并安装.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request

from PyQt6.QtCore import QThread, pyqtSignal

log = logging.getLogger("SayInk")

GITHUB_REPO = "zyjhandsome/SayInk"
LATEST_RELEASE_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
USER_AGENT = "SayInk"
CHECK_INTERVAL_SEC = 24 * 60 * 60
# Accept "x.y.z" and "x.y" (a two-part tag counts as patch 0).
_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")
_TRUSTED_HOSTS = frozenset(
    {
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
        "github-releases.githubusercontent.com",
    }
)


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    asset_name: str
    asset_url: str
    size: int = 0
    sha256: str = ""


def asset_sha256(asset: dict) -> str:
    """GitHub publishes ``digest: "sha256:<hex>"`` for release assets."""
    digest = str(asset.get("digest") or "").strip().lower()
    if digest.startswith("sha256:"):
        value = digest.split(":", 1)[1]
        if re.fullmatch(r"[0-9a-f]{64}", value):
            return value
    return ""


def version_key(text: str) -> tuple[int, int, int]:
    match = _VERSION_RE.search((text or "").strip())
    if not match:
        return (0, 0, 0)
    return int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)


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


def pick_installer_asset(assets: list[dict], version: str) -> dict | None:
    """Prefer ``SayInk-Setup-<version>.exe``; otherwise a setup whose name
    still carries ``<version>`` (e.g. ``SayInk-Setup-2.1.0-x64.exe``).

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
            and version
            and version in name
        ):
            fallback = asset
    return exact or fallback


def release_from_payload(payload: dict, *, current: str) -> ReleaseInfo | None:
    version = str(payload.get("tag_name") or "").strip().lstrip("vV")
    if not version or not is_newer(version, current):
        return None
    asset = pick_installer_asset(list(payload.get("assets") or []), version)
    if asset is None:
        return None
    try:
        size = int(asset.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    return ReleaseInfo(
        version=version,
        asset_name=str(asset.get("name") or ""),
        asset_url=str(asset.get("browser_download_url") or ""),
        size=max(0, size),
        sha256=asset_sha256(asset),
    )


def _request(url: str) -> Request:
    return Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github+json",
        },
    )


def fetch_latest_release(urlopen, *, current: str) -> ReleaseInfo | None:
    with urlopen(_request(LATEST_RELEASE_URL), timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return release_from_payload(payload, current=current)


def launch_installer(path: str) -> None:
    """Start the setup program so it outlives this process."""
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([path], **kwargs)


class InstallerVerificationError(ValueError):
    pass


class MissingDigestError(InstallerVerificationError):
    """The release publishes no SHA-256, so the installer cannot be verified."""


MISSING_DIGEST_MESSAGE = "该版本未提供安装包校验值，为安全起见不自动安装，请到 GitHub 发布页手动下载"


def download_installer(
    url: str,
    dest: Path,
    urlopen,
    on_progress=None,
    *,
    expected_size: int = 0,
    sha256: str = "",
    reuse_existing: bool = False,
) -> None:
    """Download to ``<dest>.part`` and only rename after size/hash checks pass.

    A published SHA-256 is mandatory: without it nothing is downloaded.
    Reusing an installer always rechecks its complete contents first.
    """
    import hashlib
    import os

    if not is_trusted_installer_url(url):
        raise ValueError("安装包地址不受信任")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", sha256 or ""):
        raise MissingDigestError("发布未提供有效的 SHA-256 校验值")
    if reuse_existing and dest.is_file():
        try:
            size = dest.stat().st_size
            if not expected_size or size == expected_size:
                cached_hash = hashlib.sha256()
                with dest.open("rb") as cached:
                    for chunk in iter(lambda: cached.read(256 * 1024), b""):
                        cached_hash.update(chunk)
                if cached_hash.hexdigest() == sha256.lower():
                    if on_progress is not None:
                        on_progress(size, size)
                    return
        except OSError:
            log.info("已下载的安装包无法读取，重新下载")
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    hasher = hashlib.sha256()
    try:
        with urlopen(_request(url), timeout=60) as response:
            total = int(response.headers.get("Content-Length") or 0) or expected_size
            got = 0
            with part.open("wb") as handle:
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    hasher.update(chunk)
                    got += len(chunk)
                    if on_progress is not None:
                        on_progress(got, total)
        want = expected_size or total
        if want and got != want:
            raise InstallerVerificationError(f"安装包不完整（{got} / {want} 字节）")
        if hasher.hexdigest() != sha256.lower():
            raise InstallerVerificationError("安装包校验值不符")
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

    def __init__(self, current: str, parent=None):
        super().__init__(parent)
        self._current = current

    def run(self) -> None:
        from urllib.request import urlopen

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
    ):
        super().__init__(parent)
        self._url = url
        self._dest = dest
        self._expected_size = expected_size
        self._sha256 = sha256

    def run(self) -> None:
        from urllib.request import urlopen

        try:
            download_installer(
                self._url,
                self._dest,
                urlopen,
                on_progress=lambda got, total: self.progress.emit(got, total),
                expected_size=self._expected_size,
                sha256=self._sha256,
                reuse_existing=True,
            )
            self.finished_path.emit(str(self._dest))
        except MissingDigestError as exc:
            log.warning("更新包缺少校验值: %s", exc)
            self.failed.emit(MISSING_DIGEST_MESSAGE)
        except InstallerVerificationError as exc:
            log.warning("更新包校验失败: %s", exc)
            self.failed.emit("下载的安装包不完整或已损坏，请重试")
        except Exception as exc:
            log.info("下载更新失败: %s", exc)
            self.failed.emit("下载失败，请稍后再试")

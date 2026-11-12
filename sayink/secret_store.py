"""Keep the LLM API key in Windows Credential Manager instead of config.json."""

from __future__ import annotations

import logging
import sys

log = logging.getLogger("SayInk")

CREDENTIAL_TARGET = "SayInk/llm.api_key"
# Written by VoiceInk ≤ 2.1.0; read once and carried over to the new target.
LEGACY_CREDENTIAL_TARGET = "VoiceInk/llm.api_key"


def decode_credential_blob(blob: bytes | bytearray | memoryview | str | None) -> str:
    """Decode a Windows CredentialBlob stored as UTF-16LE or raw UTF-8/ASCII."""
    if not blob:
        return ""
    if isinstance(blob, str):
        return blob
    raw = bytes(blob)
    if raw.startswith(b"\xff\xfe"):
        return raw[2:].decode("utf-16-le", errors="replace").rstrip("\x00")
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be", errors="replace").rstrip("\x00")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="replace").rstrip("\x00")

    # C-string null-terminated single-byte ASCII/UTF-8 (only trailing NULs, no interior NULs)
    stripped_trailing_nul = raw.rstrip(b"\x00")
    if stripped_trailing_nul and len(stripped_trailing_nul) < len(raw) and b"\x00" not in stripped_trailing_nul:
        if all(0x20 <= b <= 0x7E or b in (9, 10, 13) for b in stripped_trailing_nul):
            return stripped_trailing_nul.decode("ascii")
        try:
            text = stripped_trailing_nul.decode("utf-8", errors="strict")
            if all(ch.isprintable() or ch in "\t\r\n" for ch in text):
                return text
        except UnicodeDecodeError:
            pass

    if b"\x00" in raw and len(raw) % 2 == 0:
        return raw.decode("utf-16-le", errors="replace").rstrip("\x00")

    if b"\x00" not in raw:
        if all(0x20 <= b <= 0x7E or b in (9, 10, 13) for b in raw):
            return raw.decode("ascii")
        try:
            text = raw.decode("utf-8", errors="strict")
            if all(ch.isprintable() or ch in "\t\r\n" for ch in text):
                return text
        except UnicodeDecodeError:
            pass

    if len(raw) % 2 == 0:
        return raw.decode("utf-16-le", errors="replace").rstrip("\x00")
    return raw.decode("utf-8", errors="replace").rstrip("\x00")


class WindowsCredentialStore:
    """Generic credential scoped to the current Windows user."""

    def __init__(self, target: str = CREDENTIAL_TARGET, legacy_target: str | None = LEGACY_CREDENTIAL_TARGET):
        import win32cred

        self._cred = win32cred
        self._target = target
        self._legacy_target = legacy_target if legacy_target != target else None

    def _read_target(self, target: str) -> str | None:
        try:
            cred = self._cred.CredRead(target, self._cred.CRED_TYPE_GENERIC, 0)
        except Exception as exc:
            if getattr(exc, "winerror", None) == 1168:  # ERROR_NOT_FOUND
                return ""
            log.warning("读取凭据管理器失败: %s", exc)
            return None
        blob = cred.get("CredentialBlob") or b""
        return decode_credential_blob(blob)

    def read(self) -> str | None:
        value = self._read_target(self._target)
        if value or value is None or not self._legacy_target:
            return value
        legacy = self._read_target(self._legacy_target)
        if legacy is None:
            # Unreadable is not "no key": the caller must not treat it as blank.
            return None
        if not legacy:
            return value
        # Carry the key over so the old entry can go; keep it if the write fails.
        if self.write(legacy):
            try:
                self._cred.CredDelete(self._legacy_target, self._cred.CRED_TYPE_GENERIC, 0)
            except Exception as exc:
                log.debug("删除旧凭据 %s 失败: %s", self._legacy_target, exc)
            log.info("已把 API Key 从旧凭据 %s 迁移到 %s", self._legacy_target, self._target)
        return legacy

    def write(self, value: str) -> bool:
        try:
            if not value:
                try:
                    self._cred.CredDelete(self._target, self._cred.CRED_TYPE_GENERIC, 0)
                except Exception as exc:
                    if getattr(exc, "winerror", None) != 1168:
                        raise
                return True
            self._cred.CredWrite(
                {
                    "Type": self._cred.CRED_TYPE_GENERIC,
                    "TargetName": self._target,
                    "UserName": "SayInk",
                    "CredentialBlob": value,
                    "Persist": self._cred.CRED_PERSIST_LOCAL_MACHINE,
                },
                0,
            )
            return True
        except Exception as exc:
            log.warning("写入凭据管理器失败: %s", exc)
            return False


def default_secret_store():
    if sys.platform != "win32":
        return None
    try:
        return WindowsCredentialStore()
    except Exception as exc:
        log.warning("凭据管理器不可用: %s", exc)
        return None

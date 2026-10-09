"""Keep the LLM API key in Windows Credential Manager instead of config.json."""

from __future__ import annotations

import logging
import sys

log = logging.getLogger("SayInk")

CREDENTIAL_TARGET = "SayInk/llm.api_key"
# Written by VoiceInk ≤ 2.1.0; read once and carried over to the new target.
LEGACY_CREDENTIAL_TARGET = "VoiceInk/llm.api_key"


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
        if isinstance(blob, bytes):
            return blob.decode("utf-16-le", errors="ignore")
        return str(blob)

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

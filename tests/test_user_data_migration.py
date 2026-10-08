"""README: 从 VoiceInk 升级到 SayInk 不丢设置、历史和模型。"""

from __future__ import annotations

from sayink.secret_store import CREDENTIAL_TARGET, LEGACY_CREDENTIAL_TARGET, WindowsCredentialStore
from sayink.user_data import migrate_legacy_data_dir, user_data_dir


class TestDataDirMigration:
    def test_legacy_dir_is_renamed_once(self, tmp_path):
        old = tmp_path / ".voiceink"
        (old / "models").mkdir(parents=True)
        (old / "config.json").write_text("{}", encoding="utf-8")
        (old / "history.db").write_bytes(b"db")

        assert migrate_legacy_data_dir(tmp_path) is True

        new = tmp_path / ".sayink"
        assert not old.exists()
        assert (new / "config.json").read_text(encoding="utf-8") == "{}"
        assert (new / "history.db").read_bytes() == b"db"
        assert (new / "models").is_dir()
        assert migrate_legacy_data_dir(tmp_path) is False

    def test_existing_new_dir_is_never_overwritten(self, tmp_path):
        (tmp_path / ".voiceink").mkdir()
        (tmp_path / ".voiceink" / "config.json").write_text("old", encoding="utf-8")
        (tmp_path / ".sayink").mkdir()
        (tmp_path / ".sayink" / "config.json").write_text("new", encoding="utf-8")

        assert migrate_legacy_data_dir(tmp_path) is False
        assert (tmp_path / ".sayink" / "config.json").read_text(encoding="utf-8") == "new"
        assert (tmp_path / ".voiceink" / "config.json").read_text(encoding="utf-8") == "old"

    def test_fresh_profile_without_legacy_dir(self, tmp_path):
        assert migrate_legacy_data_dir(tmp_path) is False
        assert user_data_dir(tmp_path) == tmp_path / ".sayink"

    def test_user_data_dir_points_at_migrated_folder(self, tmp_path):
        (tmp_path / ".voiceink").mkdir()
        assert user_data_dir(tmp_path) == tmp_path / ".sayink"
        assert (tmp_path / ".sayink").is_dir()


class _NotFound(Exception):
    winerror = 1168  # ERROR_NOT_FOUND, as pywintypes.error exposes it


class _FakeCred:
    """Just enough of win32cred for the store: a dict of target -> value."""

    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2

    def __init__(self, entries: dict[str, str]):
        self.entries = dict(entries)
        self.deleted: list[str] = []

    def CredRead(self, target, _type, _flags):
        if target not in self.entries:
            raise _NotFound(target)
        return {"CredentialBlob": self.entries[target].encode("utf-16-le")}

    def CredWrite(self, cred, _flags):
        self.entries[cred["TargetName"]] = cred["CredentialBlob"]

    def CredDelete(self, target, _type, _flags):
        self.entries.pop(target, None)
        self.deleted.append(target)


def _store(entries: dict[str, str]) -> tuple[WindowsCredentialStore, _FakeCred]:
    store = WindowsCredentialStore.__new__(WindowsCredentialStore)
    fake = _FakeCred(entries)
    store._cred = fake
    store._target = CREDENTIAL_TARGET
    store._legacy_target = LEGACY_CREDENTIAL_TARGET
    return store, fake


class TestCredentialMigration:
    def test_legacy_key_is_carried_over_and_old_entry_removed(self):
        store, fake = _store({LEGACY_CREDENTIAL_TARGET: "sk-old"})

        assert store.read() == "sk-old"
        assert fake.entries[CREDENTIAL_TARGET] == "sk-old"
        assert LEGACY_CREDENTIAL_TARGET not in fake.entries
        assert fake.deleted == [LEGACY_CREDENTIAL_TARGET]

    def test_new_entry_wins_over_legacy(self):
        store, fake = _store({CREDENTIAL_TARGET: "sk-new", LEGACY_CREDENTIAL_TARGET: "sk-old"})

        assert store.read() == "sk-new"
        assert fake.entries[LEGACY_CREDENTIAL_TARGET] == "sk-old"
        assert fake.deleted == []

    def test_no_key_anywhere_reads_empty(self):
        store, fake = _store({})
        assert store.read() == ""
        assert fake.deleted == []

    def test_write_uses_the_new_target(self):
        store, fake = _store({})
        assert store.write("sk-1") is True
        assert fake.entries == {CREDENTIAL_TARGET: "sk-1"}

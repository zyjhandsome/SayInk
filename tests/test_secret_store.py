"""WindowsCredentialStore against a fake win32cred: every branch the app
relies on so an API key is never silently lost or written in the clear (Q-13)."""

import sys

import pytest

from sayink.secret_store import (
    CREDENTIAL_TARGET,
    LEGACY_CREDENTIAL_TARGET,
    WindowsCredentialStore,
    default_secret_store,
)


class _WinError(Exception):
    def __init__(self, winerror, msg="win32 error"):
        super().__init__(msg)
        self.winerror = winerror


class _FakeCred:
    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2

    def __init__(self, entries=None, *, fail_write=False, fail_delete=None, fail_read=None):
        self.entries = dict(entries or {})
        self.writes: list[dict] = []
        self.deleted: list[str] = []
        self.fail_write = fail_write
        self.fail_delete = fail_delete
        self.fail_read = fail_read

    def CredRead(self, target, _type, _flags):
        if self.fail_read is not None and target in self.fail_read:
            raise _WinError(5, "access denied")
        if target not in self.entries:
            raise _WinError(1168, "not found")
        return {"CredentialBlob": self.entries[target]}

    def CredWrite(self, cred, _flags):
        if self.fail_write:
            raise _WinError(1312, "no logon session")
        self.writes.append(dict(cred))
        self.entries[cred["TargetName"]] = cred["CredentialBlob"]

    def CredDelete(self, target, _type, _flags):
        if self.fail_delete is not None and target in self.fail_delete:
            raise _WinError(self.fail_delete[target])
        if target not in self.entries:
            raise _WinError(1168, "not found")
        self.entries.pop(target)
        self.deleted.append(target)


def _store(fake: _FakeCred, *, legacy=LEGACY_CREDENTIAL_TARGET) -> WindowsCredentialStore:
    store = WindowsCredentialStore.__new__(WindowsCredentialStore)
    store._cred = fake
    store._target = CREDENTIAL_TARGET
    store._legacy_target = legacy if legacy != CREDENTIAL_TARGET else None
    return store


class TestRead:
    def test_utf16_blob_is_decoded(self):
        fake = _FakeCred({CREDENTIAL_TARGET: "sk-密钥".encode("utf-16-le")})
        assert _store(fake).read() == "sk-密钥"

    def test_string_blob_is_returned_as_is(self):
        fake = _FakeCred({CREDENTIAL_TARGET: "sk-plain"})
        assert _store(fake).read() == "sk-plain"

    def test_missing_entry_reads_empty_not_none(self):
        assert _store(_FakeCred()).read() == ""

    def test_unreadable_entry_reads_none_and_is_logged(self, caplog):
        fake = _FakeCred({CREDENTIAL_TARGET: "x"}, fail_read={CREDENTIAL_TARGET})
        with caplog.at_level("WARNING", logger="SayInk"):
            assert _store(fake).read() is None
        assert "读取凭据管理器失败" in caplog.text

    def test_store_without_legacy_target_never_looks_for_one(self):
        fake = _FakeCred({LEGACY_CREDENTIAL_TARGET: "sk-old"})
        store = _store(fake, legacy=None)
        assert store.read() == ""
        assert fake.deleted == []


class TestLegacyMigration:
    def test_legacy_key_stays_in_place_when_the_new_write_fails(self, caplog):
        fake = _FakeCred({LEGACY_CREDENTIAL_TARGET: "sk-old"}, fail_write=True)
        with caplog.at_level("WARNING", logger="SayInk"):
            assert _store(fake).read() == "sk-old"
        assert fake.entries == {LEGACY_CREDENTIAL_TARGET: "sk-old"}
        assert fake.deleted == []
        assert "写入凭据管理器失败" in caplog.text

    def test_legacy_delete_failure_is_tolerated_after_a_successful_copy(self):
        fake = _FakeCred({LEGACY_CREDENTIAL_TARGET: "sk-old"}, fail_delete={LEGACY_CREDENTIAL_TARGET: 5})
        assert _store(fake).read() == "sk-old"
        assert fake.entries[CREDENTIAL_TARGET] == "sk-old"
        assert fake.entries[LEGACY_CREDENTIAL_TARGET] == "sk-old"

    def test_empty_legacy_entry_is_not_migrated(self):
        fake = _FakeCred({LEGACY_CREDENTIAL_TARGET: ""})
        assert _store(fake).read() == ""
        assert fake.writes == []
        assert fake.deleted == []


class TestWrite:
    def test_write_stores_a_generic_per_machine_credential(self):
        fake = _FakeCred()
        assert _store(fake).write("sk-1") is True
        (cred,) = fake.writes
        assert cred["Type"] == _FakeCred.CRED_TYPE_GENERIC
        assert cred["TargetName"] == CREDENTIAL_TARGET
        assert cred["CredentialBlob"] == "sk-1"
        assert cred["Persist"] == _FakeCred.CRED_PERSIST_LOCAL_MACHINE

    def test_empty_value_deletes_the_entry(self):
        fake = _FakeCred({CREDENTIAL_TARGET: "sk-1"})
        assert _store(fake).write("") is True
        assert fake.entries == {}
        assert fake.deleted == [CREDENTIAL_TARGET]

    def test_clearing_an_absent_entry_is_still_a_success(self):
        fake = _FakeCred()
        assert _store(fake).write("") is True
        assert fake.writes == []

    def test_other_delete_errors_are_reported_as_failure(self, caplog):
        fake = _FakeCred({CREDENTIAL_TARGET: "sk-1"}, fail_delete={CREDENTIAL_TARGET: 5})
        with caplog.at_level("WARNING", logger="SayInk"):
            assert _store(fake).write("") is False
        assert fake.entries == {CREDENTIAL_TARGET: "sk-1"}
        assert "写入凭据管理器失败" in caplog.text

    def test_write_failure_returns_false_instead_of_raising(self):
        fake = _FakeCred(fail_write=True)
        assert _store(fake).write("sk-1") is False
        assert fake.entries == {}


class TestConstructorAndFactory:
    def test_constructor_binds_win32cred_and_targets(self, monkeypatch):
        fake_module = _FakeCred()
        monkeypatch.setitem(sys.modules, "win32cred", fake_module)
        store = WindowsCredentialStore()
        assert store._cred is fake_module
        assert store._target == CREDENTIAL_TARGET
        assert store._legacy_target == LEGACY_CREDENTIAL_TARGET
        assert WindowsCredentialStore(target="T", legacy_target="T")._legacy_target is None

    def test_factory_is_none_off_windows(self, monkeypatch):
        import sayink.secret_store as mod

        monkeypatch.setattr(mod.sys, "platform", "linux")
        assert default_secret_store() is None

    def test_factory_is_none_when_win32cred_is_unavailable(self, monkeypatch, caplog):
        import sayink.secret_store as mod

        monkeypatch.setattr(mod.sys, "platform", "win32")
        monkeypatch.setitem(sys.modules, "win32cred", None)  # import raises ImportError
        with caplog.at_level("WARNING", logger="SayInk"):
            assert default_secret_store() is None
        assert "凭据管理器不可用" in caplog.text

    @pytest.mark.skipif(sys.platform != "win32", reason="needs the real win32cred module")
    def test_factory_returns_a_store_on_windows(self, monkeypatch):
        pytest.importorskip("win32cred")
        store = default_secret_store()
        assert isinstance(store, WindowsCredentialStore)

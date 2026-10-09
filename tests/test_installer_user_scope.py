"""Installer: per-user state belongs to the user who ran Setup, and nothing
of an old install or of the app's own per-user entries outlives it."""

from pathlib import Path

_ISS = Path(__file__).resolve().parents[1] / "installer" / "SayInk-Setup.iss"


def _text() -> str:
    return _ISS.read_text(encoding="utf-8")


def test_upgrade_clears_old_libraries_first():
    text = _text()
    section = text.split("[InstallDelete]", 1)[1].split("\n[", 1)[0]
    assert 'Name: "{app}\\_internal"' in section


def test_setup_never_touches_the_elevated_accounts_profile_or_hive():
    text = _text()
    setup_code = text.split("procedure CurUninstallStepChanged", 1)[0]
    assert "{%USERPROFILE}" not in setup_code
    assert "Root: HKCU" not in text
    assert "ExecAsOriginalUser" in text


def test_uninstall_removes_what_the_app_created_per_user():
    text = _text()
    uninstall = text.split("procedure CurUninstallStepChanged", 1)[1]
    assert "RemovePerUserLeftovers();" in uninstall
    assert "SayInk.lnk" in text
    # Only a Run value launching this install; a source checkout keeps its own.
    assert "Pos(AppExe, Lowercase(Value)) > 0" in text

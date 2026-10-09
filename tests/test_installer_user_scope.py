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


# P-04: the default installer is the lite one; the model only ships with /DBundleModel.
def test_model_files_are_only_packed_into_the_full_installer():
    text = _text()
    files = text.split("[Files]", 1)[1].split("\n[", 1)[0]
    model_block = files.split("#ifdef BundleModel", 1)[1].split("#endif", 1)[0]
    assert 'Source: "..\\dist\\SayInk\\models\\*"' in model_block
    assert "skipifsourcedoesntexist" not in model_block
    assert "OutputBaseFilename=SayInk-Setup-{#AppVersionStr}{#OutputSuffix}" in text
    assert '#define OutputSuffix ""' in text


def test_upgrade_keeps_an_installed_model_folder():
    """A lite update over a full install must leave {app}\\models alone."""
    text = _text()
    section = text.split("[InstallDelete]", 1)[1].split("\n[", 1)[0]
    assert "models" not in section


def test_uninstall_removes_what_the_app_created_per_user():
    text = _text()
    uninstall = text.split("procedure CurUninstallStepChanged", 1)[1]
    assert "RemovePerUserLeftovers();" in uninstall
    assert "SayInk.lnk" in text
    # Only a Run value launching this install; a source checkout keeps its own.
    assert "Pos(AppExe, Lowercase(Value)) > 0" in text

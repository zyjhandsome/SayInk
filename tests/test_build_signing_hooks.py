"""Tests for build-time signing hooks and zero-dependency parallel test runner."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from sayink_build.release_signing import (
    PRIVATE_KEY_ENV_VAR,
    SIGNTOOL_ENV_VAR,
    SigningError,
    build_signtool_argv,
    generate_ed25519_keypair,
    inno_signtool_command,
    sign_file_if_configured,
    verify_release_file,
    write_detached_signature_if_configured,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_parallel_runner(monkeypatch: pytest.MonkeyPatch):
    script_path = REPO_ROOT / "tools" / "run_tests_parallel.py"
    spec = importlib.util.spec_from_file_location("run_tests_parallel", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "run_tests_parallel", module)
    spec.loader.exec_module(module)
    return module


def test_signtool_hook_is_clean_noop_when_unset(tmp_path: Path):
    exe = tmp_path / "SayInk.exe"
    exe.write_bytes(b"MZ-unsigned")
    called = []
    assert sign_file_if_configured(exe, env={}, run=lambda *a, **k: called.append(a)) is False
    assert called == []


def test_build_signtool_argv_handles_spaces_and_placeholders(tmp_path: Path):
    target = tmp_path / "SayInk-Setup-2.2.33.exe"
    cmd_append = (
        '"C:\\Program Files (x86)\\Windows Kits\\10\\bin\\10.0.22621.0\\x64\\signtool.exe" '
        "sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 /a"
    )
    argv = build_signtool_argv(cmd_append, target)
    assert argv[0].endswith("signtool.exe")
    assert argv[-1] == str(target)

    argv_placeholder = build_signtool_argv("signtool sign /fd SHA256 {file} /v", target)
    assert argv_placeholder == ["signtool", "sign", "/fd", "SHA256", str(target), "/v"]

    argv_inno_var = build_signtool_argv("signtool sign $f", target)
    assert argv_inno_var == ["signtool", "sign", str(target)]


def test_sign_file_if_configured_runs_command_and_surfaces_failures(tmp_path: Path):
    exe = tmp_path / "SayInk.exe"
    exe.write_bytes(b"MZ-payload")
    seen_cmds: list[list[str]] = []

    def ok_runner(argv, **_kwargs):
        seen_cmds.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="Successfully signed", stderr="")

    signed = sign_file_if_configured(
        exe,
        env={SIGNTOOL_ENV_VAR: "signtool sign /fd SHA256"},
        run=ok_runner,
    )
    assert signed is True
    assert seen_cmds == [["signtool", "sign", "/fd", "SHA256", str(exe)]]

    def fail_runner(argv, **_kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="No certificates were found")

    with pytest.raises(SigningError, match="No certificates were found"):
        sign_file_if_configured(
            exe,
            env={SIGNTOOL_ENV_VAR: "signtool sign /fd SHA256"},
            run=fail_runner,
        )

    with pytest.raises(SigningError, match="missing file"):
        sign_file_if_configured(
            tmp_path / "missing.exe",
            env={SIGNTOOL_ENV_VAR: "signtool sign"},
            run=ok_runner,
        )


def test_inno_setup_script_and_signtool_formatter_stay_aligned():
    assert inno_signtool_command("") == ""
    assert inno_signtool_command("signtool sign /a") == "signtool sign /a $q$f$q"
    assert inno_signtool_command("signtool sign {file}") == "signtool sign $q$f$q"
    assert inno_signtool_command("signtool sign $q$f$q") == "signtool sign $q$f$q"

    iss_text = (REPO_ROOT / "installer" / "SayInk-Setup.iss").read_text(encoding="utf-8")
    assert "#ifdef UseSignTool" in iss_text
    assert "SignTool=sayinksign" in iss_text
    assert "SignedUninstaller=yes" in iss_text


def test_post_authenticode_binary_is_covered_by_detached_ed25519_signature(tmp_path: Path):
    """Authenticode embeds a PKCS#7 block into the PE file; .sig must hash the post-sign bytes."""
    priv_hex, pub_hex = generate_ed25519_keypair()
    installer = tmp_path / "SayInk-Setup-2.2.33.exe"
    installer.write_bytes(b"MZ-unsigned-installer")

    def fake_authenticode_sign(argv, **_kwargs):
        target_file = Path(argv[-1])
        target_file.write_bytes(target_file.read_bytes() + b"|AUTHENTICODE-PKCS7-BLOCK")
        return subprocess.CompletedProcess(argv, 0, stdout="Signed", stderr="")

    env = {
        SIGNTOOL_ENV_VAR: "signtool sign /fd SHA256",
        PRIVATE_KEY_ENV_VAR: priv_hex,
    }
    assert sign_file_if_configured(installer, env=env, run=fake_authenticode_sign) is True
    sig_file = write_detached_signature_if_configured(installer, env=env)
    assert sig_file is not None and sig_file.is_file()
    assert verify_release_file(installer, pub_hex) is True
    assert write_detached_signature_if_configured(installer, env={}) is None


def test_parallel_runner_lpt_sharding_and_summary_parsing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    runner = _load_parallel_runner(monkeypatch)
    assert 1 <= runner.default_worker_count(None) <= 6
    assert runner.default_worker_count(16) == 6
    assert runner.default_worker_count(2) == 2

    suite = tmp_path / "tests"
    suite.mkdir()
    # Two heavy UI files and two tiny unit files should spread heavy files across 2 shards.
    (suite / "test_settings_heavy.py").write_text("# " + ("x" * 40_000) + "\n", encoding="utf-8")
    (suite / "test_meeting_heavy.py").write_text("# " + ("y" * 38_000) + "\n", encoding="utf-8")
    (suite / "test_small_a.py").write_text("def test_a(): assert True\n", encoding="utf-8")
    (suite / "test_small_b.py").write_text("def test_b(): assert True\n", encoding="utf-8")

    targets = runner.discover_test_targets(["tests"], base_dir=tmp_path)
    assert len(targets) == 4
    shards = runner.build_shards(targets, workers=2, base_dir=tmp_path)
    assert len(shards) == 2
    assert "tests/test_settings_heavy.py" in shards[0].targets
    assert "tests/test_meeting_heavy.py" in shards[1].targets

    counts = runner.parse_pytest_counts("...\n12 passed, 2 skipped, 1 failed, 9 deselected in 1.23s\n")
    assert counts["passed"] == 12
    assert counts["skipped"] == 2
    assert counts["failed"] == 1
    assert counts["deselected"] == 9


def test_parallel_runner_executes_shards_and_propagates_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    runner = _load_parallel_runner(monkeypatch)
    suite = tmp_path / "tests"
    suite.mkdir()
    (suite / "test_one.py").write_text(
        "def test_alpha():\n    assert 1 + 1 == 2\n",
        encoding="utf-8",
    )
    (suite / "test_two.py").write_text(
        "def test_beta():\n    assert 'sayink'.upper() == 'SAYINK'\n",
        encoding="utf-8",
    )

    assert runner.main(["--dry-run", "-n", "2", str(suite)]) == 0

    targets = runner.discover_test_targets([str(suite)], base_dir=tmp_path)
    code, results, _ = runner.run_parallel_pytest(
        targets,
        workers=2,
        repo_root=tmp_path,
        quiet=True,
    )
    assert code == 0
    assert sum(r.counts["passed"] for r in results) == 2

    # Failing shard must cause non-zero exit code.
    (suite / "test_two.py").write_text(
        "def test_broken():\n    assert 1 == 2\n",
        encoding="utf-8",
    )
    fail_code, fail_results, _ = runner.run_parallel_pytest(
        targets,
        workers=2,
        repo_root=tmp_path,
        quiet=True,
    )
    assert fail_code != 0
    assert sum(r.counts["failed"] for r in fail_results) == 1


def test_parallel_runner_handles_keyword_deselection_across_shards(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
):
    runner = _load_parallel_runner(monkeypatch)
    suite = tmp_path / "tests"
    suite.mkdir()
    (suite / "test_one.py").write_text("def test_only_in_one():\n    assert True\n", encoding="utf-8")
    (suite / "test_two.py").write_text("def test_only_in_two():\n    assert True\n", encoding="utf-8")
    (suite / "test_three.py").write_text("def test_only_in_three():\n    assert True\n", encoding="utf-8")

    targets = runner.discover_test_targets([str(suite)], base_dir=tmp_path)
    # Matching only 1 shard while 2 shards exit 5 due to -k deselection must succeed (0).
    code_one, res_one, _ = runner.run_parallel_pytest(
        targets,
        workers=3,
        pytest_args=["-k", "test_only_in_two"],
        repo_root=tmp_path,
        quiet=False,
    )
    captured_one = capsys.readouterr()
    assert code_one == 0
    assert sum(r.counts["passed"] for r in res_one) == 1
    assert sum(r.counts["deselected"] for r in res_one) == 2
    assert "FAILED SHARD" not in captured_one.err

    # Matching 0 tests across ALL shards returns exit code 5 cleanly without FAILED SHARD banners.
    code_none, res_none, _ = runner.run_parallel_pytest(
        targets,
        workers=3,
        pytest_args=["-k", "no_such_test_anywhere_xyz"],
        repo_root=tmp_path,
        quiet=False,
    )
    captured_none = capsys.readouterr()
    assert code_none == 5
    assert sum(r.counts["passed"] for r in res_none) == 0
    assert sum(r.counts["deselected"] for r in res_none) == 3
    assert "FAILED SHARD" not in captured_none.err

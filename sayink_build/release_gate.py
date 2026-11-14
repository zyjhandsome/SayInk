"""Checks before building an installer meant for release.

Users get every release through the in-app updater, so the installer has to
come from a commit anyone can check out again: committed, tagged
``v<version>`` on origin, and green in CI.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = "tests.yml"
# Untracked files in source/spec/test/doc/tooling directories or at repo root
# must not slip past a release check; build/, caches, and gitignored staging
# folders stay ignored.
SOURCE_DIRS = (
    "sayink/",
    "sayink_build/",
    "installer/",
    "openspec/",
    "tests/",
    "tools/",
    "docs/",
    "design-system/",
    ".github/",
)

Runner = Callable[[list[str]], subprocess.CompletedProcess]


class _CommandFailed(Exception):
    pass


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    return subprocess.run(
        cmd, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )


def _output(run: Runner, cmd: list[str]) -> str:
    name = " ".join(cmd[:2])
    try:
        result = run(cmd)
    except OSError as exc:
        raise _CommandFailed(f"Could not run `{name}`: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise _CommandFailed(f"`{name}` failed: {detail}")
    return result.stdout


def _uncommitted(run: Runner) -> str | None:
    status = _output(run, ["git", "status", "--porcelain", "--untracked-files=all"])
    dirty = []
    for line in status.splitlines():
        code, path = line[:2], line[3:]
        is_root_stray = code == "??" and "/" not in path and not path.startswith(".")
        if code != "??" or path.startswith(SOURCE_DIRS) or is_root_stray:
            dirty.append(path)
    if not dirty:
        return None
    shown = ", ".join(dirty[:10]) + (" …" if len(dirty) > 10 else "")
    return f"Uncommitted changes would ship in the installer: {shown}"


def _tag_mismatch(run: Runner, tag: str, head: str) -> str | None:
    ref = f"refs/tags/{tag}"
    peeled_ref = f"{ref}^{{}}"
    refs = {}
    for line in _output(run, ["git", "ls-remote", "origin", ref, peeled_ref]).splitlines():
        sha, _, matched_ref = line.partition("\t")
        refs[matched_ref.strip()] = sha.strip()
    # Annotated tags list the tag object first and the commit as ``^{}``.
    remote = refs.get(peeled_ref) or refs.get(ref)
    if remote is None:
        return f"Tag {tag} is not on origin: tag the release commit and push the tag first."
    if remote != head:
        return (
            f"Tag {tag} on origin points at {remote[:12]}, not at HEAD {head[:12]}: "
            "build from the tagged commit."
        )
    return None


def _ci_not_green(run: Runner, head: str) -> str | None:
    out = _output(
        run,
        ["gh", "run", "list", "--commit", head, "--workflow", WORKFLOW,
         "--json", "status,conclusion", "--limit", "1"],
    )
    runs = json.loads(out or "[]")
    if not runs:
        return f"There is no CI run for {head[:12]}: push the commit and wait for {WORKFLOW} to pass."
    latest = runs[0]
    if latest.get("status") != "completed":
        return f"CI for {head[:12]} is still running; wait for it to pass."
    if latest.get("conclusion") != "success":
        return f"CI for {head[:12]} ended with {latest.get('conclusion')}; fix it before releasing."
    return None


def _ruff_problem(run: Runner) -> str | None:
    """Lint the tree before packaging.

    CI also enforces this via ``tests/test_release_gate.py::test_workspace_passes_ruff``
    (originally added when the push token lacked GitHub ``workflow`` scope to edit
    ``.github/workflows/tests.yml`` directly).
    """
    import sys

    cmd = [
        sys.executable, "-m", "ruff", "check", "--no-cache",
        "sayink", "sayink_build", "tools", "tests",
        "build.py", "build_release.py", "installer/build_installer.py", "run.py",
    ]
    try:
        result = run(cmd)
    except OSError as exc:
        return f"Could not run ruff: {exc}"
    if result.returncode != 0:
        detail = (result.stdout or result.stderr or "").strip().splitlines()
        tail = "; ".join(detail[:4])
        return f"ruff check failed: {tail}"
    return None


def _readme_contract_problem(run: Runner) -> str | None:
    """Verify the README truth-source contract and OpenSpec archive hygiene."""
    import sys

    cmd = [
        sys.executable, "-m", "pytest", "-p", "no:cacheprovider",
        "tests/test_readme_features.py", "-q",
    ]
    try:
        result = run(cmd)
    except OSError as exc:
        return f"Could not run README contract tests: {exc}"
    if result.returncode != 0:
        detail = (result.stdout or result.stderr or "").strip().splitlines()
        tail = "; ".join(detail[-4:])
        return f"README truth-source check failed: {tail}"
    return None


def _installer_version_problem(_run: Runner) -> str | None:
    """Ensure SayInk-Setup.iss fallback #defines stay synced with sayink/version.py."""
    from sayink.version import __version__, file_version_quad

    iss_path = ROOT / "installer" / "SayInk-Setup.iss"
    try:
        iss_text = iss_path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"Could not read installer/SayInk-Setup.iss: {exc}"
    expected_str = f'#define AppVersionStr "{__version__}"'
    expected_quad = f'#define AppVersionQuad "{file_version_quad()}"'
    if expected_str not in iss_text or expected_quad not in iss_text:
        return (
            "installer/SayInk-Setup.iss fallback AppVersionStr / AppVersionQuad "
            f"does not match sayink/version.py ({__version__} / {file_version_quad()})."
        )
    return None


def release_problems(version: str, run: Runner = _run) -> list[str]:
    """Everything that stops ``version`` from being released; empty when ready."""
    problems = []

    def check(fn, *args) -> None:
        try:
            problem = fn(run, *args)
        except _CommandFailed as exc:
            problem = str(exc)
        if problem:
            problems.append(problem)

    check(_uncommitted)
    check(_installer_version_problem)
    check(_ruff_problem)
    check(_readme_contract_problem)
    try:
        head = _output(run, ["git", "rev-parse", "HEAD"]).strip()
    except _CommandFailed as exc:
        return problems + [str(exc)]
    check(_tag_mismatch, f"v{version}", head)
    check(_ci_not_green, head)
    return problems

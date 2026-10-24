"""Checks before building an installer meant for release.

Users get every release through the in-app updater, so the installer has to
come from a commit anyone can check out again: committed, tagged
``v<version>`` on origin, and green in CI.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = "tests.yml"
# Untracked files here end up in the installer; elsewhere (build/, caches) they do not.
SOURCE_DIRS = ("sayink/", "sayink_build/", "installer/")

Runner = Callable[[list[str]], subprocess.CompletedProcess]


class _CommandFailed(Exception):
    pass


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
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
        if code != "??" or path.startswith(SOURCE_DIRS):
            dirty.append(path)
    if not dirty:
        return None
    shown = ", ".join(dirty[:10]) + (" …" if len(dirty) > 10 else "")
    return f"Uncommitted changes would ship in the installer: {shown}"


def _tag_mismatch(run: Runner, tag: str, head: str) -> str | None:
    refs = {}
    for line in _output(run, ["git", "ls-remote", "origin", f"refs/tags/{tag}"]).splitlines():
        sha, _, ref = line.partition("\t")
        refs[ref.strip()] = sha.strip()
    # Annotated tags list the tag object first and the commit as ``^{}``.
    remote = refs.get(f"refs/tags/{tag}^{{}}") or refs.get(f"refs/tags/{tag}")
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
    """Lint the tree that CI would ship. The workflow file cannot run ruff itself."""
    import sys

    cmd = [
        sys.executable, "-m", "ruff", "check",
        "sayink", "sayink_build", "tests", "build.py", "build_release.py", "run.py",
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
    check(_ruff_problem)
    try:
        head = _output(run, ["git", "rev-parse", "HEAD"]).strip()
    except _CommandFailed as exc:
        return problems + [str(exc)]
    check(_tag_mismatch, f"v{version}", head)
    check(_ci_not_green, head)
    return problems

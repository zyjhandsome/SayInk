"""A release installer is only built from a pushed, tagged, CI-green commit."""

from __future__ import annotations

import json
import subprocess

import pytest

import build_release
from sayink_build.release_gate import release_problems

HEAD = "a" * 40
OTHER = "b" * 40


def _runner(overrides=None):
    """Fake subprocess runner keyed by the first two words of a command."""
    responses = {
        "git status": (0, "?? build/app/SayInk.exe\n?? .pytest_cache/v/cache\n"),
        "git rev-parse": (0, f"{HEAD}\n"),
        "git ls-remote": (0, f"{OTHER}\trefs/tags/v2.2.5\n{HEAD}\trefs/tags/v2.2.5^{{}}\n"),
        "gh run": (0, json.dumps([{"status": "completed", "conclusion": "success"}])),
        "ruff check": (0, ""),
    }
    responses.update(overrides or {})
    calls = []

    def run(cmd):
        calls.append(cmd)
        key = "ruff check" if "ruff" in cmd else " ".join(cmd[:2])
        if key not in responses:
            raise AssertionError(f"unexpected command: {cmd}")
        result = responses[key]
        if isinstance(result, BaseException):
            raise result
        code, out = result
        return subprocess.CompletedProcess(cmd, code, stdout=out, stderr="boom" if code else "")

    run.calls = calls
    return run


def test_pushed_tagged_green_commit_is_ready():
    assert release_problems("2.2.5", run=_runner()) == []


def test_ci_is_looked_up_for_the_head_commit():
    run = _runner()
    release_problems("2.2.5", run=run)
    gh = next(cmd for cmd in run.calls if cmd[0] == "gh")
    assert gh[gh.index("--commit") + 1] == HEAD


def test_uncommitted_changes_and_untracked_sources_block_the_release():
    status = " M sayink/app.py\n?? sayink/new_module.py\n?? build/app/SayInk.exe\n"
    problems = release_problems("2.2.5", run=_runner({"git status": (0, status)}))
    assert len(problems) == 1
    assert "sayink/app.py" in problems[0]
    assert "sayink/new_module.py" in problems[0]
    assert "build/app" not in problems[0]


def test_tag_must_exist_on_the_remote():
    problems = release_problems("2.2.5", run=_runner({"git ls-remote": (0, "")}))
    assert problems == ["Tag v2.2.5 is not on origin: tag the release commit and push the tag first."]


def test_tag_must_point_at_head():
    problems = release_problems("2.2.5", run=_runner({"git ls-remote": (0, f"{OTHER}\trefs/tags/v2.2.5\n")}))
    assert len(problems) == 1
    assert "v2.2.5" in problems[0] and OTHER[:12] in problems[0] and HEAD[:12] in problems[0]


@pytest.mark.parametrize(
    "runs, expected",
    [
        ([], "no CI run"),
        ([{"status": "in_progress", "conclusion": ""}], "still running"),
        ([{"status": "completed", "conclusion": "failure"}], "failure"),
    ],
)
def test_ci_must_have_passed_for_the_commit(runs, expected):
    problems = release_problems("2.2.5", run=_runner({"gh run": (0, json.dumps(runs))}))
    assert len(problems) == 1
    assert "CI" in problems[0] and expected in problems[0]


def test_missing_gh_is_reported_instead_of_crashing():
    problems = release_problems("2.2.5", run=_runner({"gh run": FileNotFoundError("gh")}))
    assert len(problems) == 1
    assert "gh" in problems[0]


def test_ruff_findings_block_the_release():
    problems = release_problems("2.2.5", run=_runner({"ruff check": (1, "sayink/app.py:1:1 F401 unused")}))
    assert any(p.startswith("ruff check failed:") for p in problems)


def test_workspace_passes_ruff():
    from sayink_build.release_gate import _ruff_problem, _run

    assert _ruff_problem(_run) is None


def test_failed_git_command_is_reported():
    problems = release_problems("2.2.5", run=_runner({"git ls-remote": (128, "")}))
    assert len(problems) == 1
    assert "git ls-remote" in problems[0]


def test_build_release_stops_before_building_when_not_ready(monkeypatch):
    monkeypatch.setattr(build_release, "release_problems", lambda version: ["Tag missing."])
    builds = []
    monkeypatch.setattr(build_release.subprocess, "run", lambda *a, **k: builds.append(a))
    with pytest.raises(SystemExit) as exc:
        build_release.main([])
    assert exc.value.code == 1
    assert builds == []


def test_skip_flag_builds_a_local_trial_without_checks(monkeypatch):
    monkeypatch.setattr(
        build_release, "release_problems", lambda version: pytest.fail("checks must be skipped")
    )
    builds = []
    monkeypatch.setattr(
        build_release.subprocess,
        "run",
        lambda cmd, **_k: builds.append(cmd) or subprocess.CompletedProcess(cmd, 0),
    )
    with pytest.raises(SystemExit) as exc:
        build_release.main(["--skip-release-checks"])
    assert exc.value.code == 0
    assert [cmd[1].rsplit("\\", 1)[-1].rsplit("/", 1)[-1] for cmd in builds] == [
        "build.py",
        "build_installer.py",
    ]

"""Validate release dependencies before starting a destructive/expensive build."""

from __future__ import annotations

import importlib.metadata as metadata
import sys
from pathlib import Path
from typing import Callable

from packaging.requirements import Requirement

REQUIREMENTS_FILE = Path(__file__).resolve().parents[1] / "requirements.txt"


def dependency_issues(
    requirements_file: Path = REQUIREMENTS_FILE,
    *,
    version_lookup: Callable[[str], str] | None = None,
    marker_environment: dict[str, str] | None = None,
) -> list[str]:
    """Check the direct requirements and respect their platform markers."""
    lookup = version_lookup or metadata.version
    issues = []
    for line in requirements_file.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        requirement = Requirement(line)
        if requirement.marker and not requirement.marker.evaluate(marker_environment):
            continue
        try:
            installed = lookup(requirement.name)
        except metadata.PackageNotFoundError:
            issues.append(f"{requirement.name}: not installed (requires {requirement.specifier})")
            continue
        if not requirement.specifier.contains(installed, prereleases=True):
            issues.append(f"{requirement.name}: {installed} does not satisfy {requirement.specifier}")
    return issues


def require_release_dependencies() -> None:
    issues = dependency_issues()
    if issues:
        raise RuntimeError(
            "Release dependency check failed:\n  " + "\n  ".join(issues)
            + "\nInstall requirements.txt in a clean environment before building."
        )


def main() -> int:
    print(f"Python {sys.version.split()[0]} / {sys.platform}")
    try:
        require_release_dependencies()
    except RuntimeError as exc:
        print(exc)
        return 1
    print("Release dependencies satisfy requirements.txt")
    for name in ("PyQt6", "sherpa-onnx"):
        print(f"{name} {metadata.version(name)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

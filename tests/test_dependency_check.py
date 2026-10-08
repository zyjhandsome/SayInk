from importlib.metadata import PackageNotFoundError
from unittest.mock import Mock

import pytest

from voiceink_build.dependency_check import dependency_issues


def test_release_engine_below_minimum_is_rejected(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("sherpa-onnx>=1.13.8\n", encoding="utf-8")
    assert dependency_issues(requirements, version_lookup=lambda _name: "1.13.2") == [
        "sherpa-onnx: 1.13.2 does not satisfy >=1.13.8"
    ]


def test_matching_release_versions_pass(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("# release engine\nsherpa-onnx>=1.13.8\n", encoding="utf-8")
    assert dependency_issues(requirements, version_lookup=lambda _name: "1.13.8") == []


def test_missing_dependency_is_reported(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("sherpa-onnx>=1.13.8\n", encoding="utf-8")
    lookup = Mock(side_effect=PackageNotFoundError("sherpa-onnx"))
    assert "not installed" in dependency_issues(requirements, version_lookup=lookup)[0]


def test_windows_dependencies_are_skipped_on_other_platforms(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text('pywin32>=306; sys_platform == "win32"\n', encoding="utf-8")
    lookup = Mock(side_effect=PackageNotFoundError("pywin32"))
    assert dependency_issues(
        requirements, version_lookup=lookup, marker_environment={"sys_platform": "linux"}
    ) == []
    lookup.assert_not_called()


def test_windows_dependencies_are_required_on_windows(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text('pywin32>=306; sys_platform == "win32"\n', encoding="utf-8")
    lookup = Mock(side_effect=PackageNotFoundError("pywin32"))
    assert len(dependency_issues(
        requirements, version_lookup=lookup, marker_environment={"sys_platform": "win32"}
    )) == 1


def test_invalid_build_environment_never_clears_existing_output(monkeypatch):
    import build

    prepare = Mock()
    monkeypatch.setattr(build, "_prepare_dist_output_dir", prepare)
    monkeypatch.setattr(
        "voiceink_build.dependency_check.require_release_dependencies",
        Mock(side_effect=RuntimeError("invalid dependencies")),
    )
    with pytest.raises(RuntimeError, match="invalid dependencies"):
        build.build()
    prepare.assert_not_called()

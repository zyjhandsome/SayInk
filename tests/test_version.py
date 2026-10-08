import pytest
from sayink import version
from sayink.version import __version__, file_version_quad, file_version_tuple


class TestVersion:
    def test_version_exists(self):
        assert __version__ is not None

    def test_version_format(self):
        parts = __version__.split(".")
        assert len(parts) >= 2
        for part in parts:
            assert part.isdigit() or part.replace(".", "").isalnum()

    def test_version_not_empty(self):
        assert len(__version__) > 0


class TestFileVersionQuad:
    def test_quad_has_four_parts(self):
        quad = file_version_quad()
        parts = quad.split(".")
        assert len(parts) == 4
        assert all(p.isdigit() for p in parts)

    def test_quad_matches_version_prefix(self):
        quad = file_version_quad()
        assert quad.startswith(__version__.split("-")[0])
        assert quad.endswith(".0")

    def test_quad_handles_suffix(self, monkeypatch):
        monkeypatch.setattr(version, "__version__", "2.4.1-beta3")
        assert file_version_quad() == "2.4.1.0"

    def test_quad_fallback_on_bad_version(self, monkeypatch):
        monkeypatch.setattr(version, "__version__", "not-a-version")
        assert file_version_quad() == "0.0.0.0"


class TestFileVersionTuple:
    def test_tuple_is_four_ints(self):
        tup = file_version_tuple()
        assert isinstance(tup, tuple)
        assert len(tup) == 4
        assert all(isinstance(x, int) for x in tup)

    def test_tuple_matches_quad(self):
        quad = file_version_quad()
        assert ".".join(str(x) for x in file_version_tuple()) == quad

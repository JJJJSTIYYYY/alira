"""Package metadata and version comparison use the package version."""

from importlib.metadata import version
from importlib import import_module

from packaging.version import InvalidVersion, Version
import pytest

from alira import __version__
from alira.shared.application.version import compare_version

version_module = import_module("alira.shared.application.version")


def test_installed_distribution_matches_package_version():
    assert version("alira-harness") == __version__


def test_compare_version_uses_package_version():
    assert compare_version(__version__) == 0
    assert str(Version(__version__)) == __version__
    assert compare_version("0.0.0") == -1


@pytest.mark.parametrize(("current", "candidate", "expected"), [
    ("3.0.0a0", "3.0.0-alpha", 0),
    ("3.0.0a0", "3.0.0a1", 1),
    ("3.0.0b1", "3.0.0a9", -1),
    ("3.0.0rc1", "3.0.0", 1),
    ("3.0.0", "3.0", 0),
    ("3.0.0", "3.0.0.dev1", -1),
    ("3.0.0", "3.0.0.post1", 1),
    ("3.0.0", "3.0.0+local.1", 1),
    ("1!3.0.0", "3.0.0", -1),
])
def test_compare_version_supports_pep440_ordering(monkeypatch, current, candidate, expected):
    monkeypatch.setattr(version_module, "__version__", current)
    assert compare_version(candidate) == expected


def test_compare_version_rejects_invalid_versions():
    with pytest.raises(InvalidVersion):
        compare_version("not-a-version")

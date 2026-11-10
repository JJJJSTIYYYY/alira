"""Package metadata and version comparison use the package version."""

from importlib.metadata import version

from alira import __version__
from alira.shared.application.version import compare_version


def test_installed_distribution_matches_package_version():
    assert version("alira-harness") == __version__


def test_compare_version_uses_package_version():
    assert compare_version(__version__) == 0
    assert compare_version(f"{__version__}.0") == 0
    assert compare_version(f"{__version__}.1") == 1
    assert compare_version("0.0.0") == -1

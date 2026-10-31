"""Package metadata, API metadata, and runtime display share one version."""

from importlib.metadata import version

from alira import __version__
from alira.common.utils.version import compare_version, print_logo
from alira.server import create_app


def test_installed_distribution_matches_package_version():
    assert version("alira") == __version__


def test_api_and_banner_use_package_version(capsys):
    app = create_app()
    assert app.version == __version__
    assert app.openapi()["info"]["version"] == __version__
    print_logo()
    assert f"Alira Agent  |  v{__version__}" in capsys.readouterr().out


def test_compare_version_uses_package_version():
    assert compare_version(__version__) == 0
    assert compare_version(f"{__version__}.0") == 0
    assert compare_version(f"{__version__}.1") == 1
    assert compare_version("0.0.0") == -1

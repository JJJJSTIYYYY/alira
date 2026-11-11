"""Publishing checks parse wheel versions without importing the application."""

import subprocess
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from zipfile import ZipFile

import pytest
from packaging.version import Version

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_distribution.py"
spec = spec_from_file_location("distribution_check", SCRIPT)
distribution_check = module_from_spec(spec)
spec.loader.exec_module(distribution_check)


@pytest.fixture
def source_version(tmp_path, monkeypatch):
    path = tmp_path / "_version.py"
    path.write_text('__version__ = "3.0.0a0"\n')
    monkeypatch.setattr(distribution_check, "VERSION_FILE", path)
    return path


def wheel(
    tmp_path,
    *,
    name="alira_harness",
    version="3.0.0a0",
    metadata_name="alira-harness",
    metadata_version="3.0.0a0",
):
    path = tmp_path / f"{name}-{version}-py3-none-any.whl"
    with ZipFile(path, "w") as archive:
        archive.writestr(
            f"{name}-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.4\nName: {metadata_name}\nVersion: {metadata_version}\n",
        )
    return path


def test_distribution_accepts_canonical_prerelease(tmp_path, source_version, capsys):
    path = wheel(tmp_path)
    assert distribution_check.check_distribution(path) == Version("3.0.0a0")
    assert distribution_check.main([str(path)]) == 0
    assert "Verified alira-harness 3.0.0a0" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"name": "alira"}, "Expected distribution"),
        (
            {"version": "3.0.0a1", "metadata_version": "3.0.0a1"},
            "does not match source version",
        ),
        ({"metadata_name": "alira"}, "METADATA Name"),
        ({"metadata_version": "3.0.0a1"}, "METADATA Version"),
        ({"metadata_version": "3.0.0-alpha"}, "METADATA Version"),
        ({"version": "3.0.0a"}, "filename version must be canonical"),
    ],
)
def test_distribution_rejects_wrong_name_or_version(
    tmp_path, source_version, options, message
):
    with pytest.raises(ValueError, match=message):
        distribution_check.check_distribution(wheel(tmp_path, **options))


def test_distribution_rejects_noncanonical_source_version(tmp_path, source_version):
    source_version.write_text('__version__ = "3.0.0-alpha"\n')
    with pytest.raises(ValueError, match="Source version must be canonical"):
        distribution_check.check_distribution(wheel(tmp_path))


@pytest.mark.parametrize(
    "entries", [[], ["a.dist-info/METADATA", "b.dist-info/METADATA"]]
)
def test_distribution_requires_one_metadata_file(tmp_path, source_version, entries):
    path = tmp_path / "alira_harness-3.0.0a0-py3-none-any.whl"
    with ZipFile(path, "w") as archive:
        for name in entries:
            archive.writestr(name, "Name: alira-harness\nVersion: 3.0.0a0\n")
    with pytest.raises(ValueError, match="exactly one"):
        distribution_check.check_distribution(path)


def test_distribution_cli_reports_invalid_artifacts(tmp_path, source_version, capsys):
    path = wheel(tmp_path, metadata_name="wrong")
    with pytest.raises(SystemExit) as caught:
        distribution_check.main([str(path)])
    assert caught.value.code == 2
    assert "METADATA Name" in capsys.readouterr().err


def test_distribution_script_does_not_import_application(tmp_path):
    script = (
        "from importlib.util import module_from_spec, spec_from_file_location\n"
        "import sys\n"
        f"spec = spec_from_file_location('distribution_check', {str(SCRIPT)!r})\n"
        "module = module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "assert not any(name == 'alira' or name.startswith('alira.') for name in sys.modules)\n"
    )
    subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, check=True, capture_output=True
    )

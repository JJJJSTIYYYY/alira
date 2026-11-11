"""Validate an Alira wheel against its metadata and the source version.

Run before publishing: python scripts/check_distribution.py dist/<wheel>.whl
This script reads source as data and never imports the application.
"""

import argparse
import ast
from collections.abc import Sequence
from email.parser import BytesParser
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version

DISTRIBUTION = "alira-harness"
VERSION_FILE = Path(__file__).resolve().parents[1] / "src/alira/_version.py"


def _source_version() -> Version:
    for node in ast.parse(VERSION_FILE.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in node.targets
        ):
            value = ast.literal_eval(node.value)
            version = Version(value)
            if value != str(version):
                raise ValueError(f"Source version must be canonical PEP 440: {value!r}")
            return version
    raise ValueError(f"No __version__ assignment found in {VERSION_FILE}")


def check_distribution(wheel: str | Path) -> Version:
    """Check wheel filename, METADATA and source version; return its version."""
    wheel = Path(wheel)
    name, version, _, _ = parse_wheel_filename(wheel.name)
    if name != DISTRIBUTION:
        raise ValueError(f"Expected distribution {DISTRIBUTION!r}, got {name!r}")
    expected = _source_version()
    if version != expected:
        raise ValueError(
            f"Wheel version {version} does not match source version {expected}"
        )
    if wheel.name.split("-")[1] != str(version):
        raise ValueError(
            f"Wheel filename version must be canonical PEP 440: {wheel.name}"
        )
    with ZipFile(wheel) as archive:
        metadata_files = [
            name
            for name in archive.namelist()
            if name.endswith(".dist-info/METADATA") and name.count("/") == 1
        ]
        if len(metadata_files) != 1:
            raise ValueError("Wheel must contain exactly one .dist-info/METADATA file")
        metadata = BytesParser().parsebytes(archive.read(metadata_files[0]))
    metadata_name = metadata.get("Name", "")
    if canonicalize_name(metadata_name) != DISTRIBUTION:
        raise ValueError(
            f"METADATA Name must be {DISTRIBUTION!r}, got {metadata_name!r}"
        )
    metadata_version = metadata.get("Version", "")
    if metadata_version != str(version):
        raise ValueError(
            f"METADATA Version {metadata_version!r} does not match wheel version {version}"
        )
    return version


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path, help="Wheel to validate before publishing")
    args = parser.parse_args(argv)
    try:
        version = check_distribution(args.wheel)
    except (ValueError, OSError, BadZipFile) as error:
        parser.error(str(error))
    print(f"Verified {DISTRIBUTION} {version}: {args.wheel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

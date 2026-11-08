"""Skill archive inspection and collision-free destination naming."""

import hashlib
import zipfile
from pathlib import Path

import yaml


def next_available_package_path(skills_dir: Path, file_name: str) -> Path:
    """Return a free path, adding ``_<index>`` before the extension."""
    source_name = Path(file_name)
    candidate = skills_dir / source_name.name
    index = 1
    while candidate.exists():
        candidate = skills_dir / f"{source_name.stem}_{index}{source_name.suffix}"
        index += 1
    return candidate


def inspect_skill_package(package_path: Path) -> dict:
    """Validate SKILL.md and collect archive metadata."""
    with zipfile.ZipFile(package_path, "r") as zip_file:
        skill_md_path = next(
            (name for name in zip_file.namelist() if Path(name).name == "SKILL.md"),
            None,
        )
        if not skill_md_path:
            raise ValueError("SKILL.md is missing")
        skill_md = zip_file.read(skill_md_path).decode("utf-8")

    if not skill_md.startswith("---"):
        raise ValueError("SKILL.md is missing YAML frontmatter")
    parts = skill_md.split("---", 2)
    if len(parts) < 3:
        raise ValueError("Invalid SKILL.md YAML frontmatter format")
    metadata = yaml.safe_load(parts[1]) or {}
    if not isinstance(metadata, dict):
        raise TypeError("SKILL.md YAML frontmatter must be an object")

    missing_fields = [
        name for name in ("name", "description") if not metadata.get(name)
    ]
    if missing_fields:
        raise ValueError("Missing skill metadata: " + ", ".join(missing_fields))

    with package_path.open("rb") as package_file:
        package_sha256 = hashlib.file_digest(package_file, "sha256").hexdigest()
    return {
        "skill_name": str(metadata["name"]),
        "skill_description": str(metadata["description"]),
        "skill_version": str(metadata.get("version") or "0.0.1"),
        "package_size": package_path.stat().st_size,
        "package_sha256": package_sha256,
    }

import asyncio
import hashlib
import shutil
import zipfile
from pathlib import Path
from uuid import uuid4

import yaml

from apix.agent.store.core.handlers import service_handler
from apix.common.utils.logger import logger
from apix.config.base_config import APIX_BASE_DIR


class FileService:
    """Copy workspace uploads and import skill archives."""

    @service_handler
    async def save_files(self, payload: dict) -> dict:
        """Copy ``file_path`` entries into ``workspace/user_upload``.

        Return each file's name, absolute saved_path and workspace-relative
        ws_saved_path. Failures use the common service result format.
        """
        source_paths: list[str] = payload["file_path"]
        workspace = Path(payload["workspace"]).expanduser().resolve()
        upload_dir = workspace / "user_upload"
        upload_dir.mkdir(parents=True, exist_ok=True)

        file_info: list[dict] = []
        for source_path in source_paths:
            source = Path(source_path).expanduser().resolve(strict=True)
            if not source.is_file():
                raise ValueError(f"Source path is not a file: {source}")

            destination = upload_dir / source.name
            await asyncio.to_thread(shutil.copy2, source, destination)
            file_info.append(
                {
                    "file_name": source.name,
                    "saved_path": str(destination),
                    "ws_saved_path": destination.relative_to(workspace).as_posix(),
                }
            )
            logger.info(f"Copied source={source} destination={destination}")

        return {"success": True, "messages": file_info}

    @staticmethod
    def _next_available_package_path(skills_dir: Path, file_name: str) -> Path:
        """Return a free path, adding ``_<index>`` before the extension."""
        source_name = Path(file_name)
        candidate = skills_dir / source_name.name
        index = 1
        while candidate.exists():
            candidate = skills_dir / f"{source_name.stem}_{index}{source_name.suffix}"
            index += 1
        return candidate

    @staticmethod
    def _inspect_skill_package(package_path: Path) -> dict:
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

    @service_handler
    async def import_skill_packages(self, payload: dict) -> dict:
        """Move ``file_path`` ZIP archives into the user's skill directory.

        ``user_uid`` selects the directory. Return skill identity, frontmatter
        metadata, package_path, package_size and package_sha256 for each archive.
        Invalid archives are removed after moving; version defaults to 0.0.1.
        """
        user_uid: str = payload["user_uid"]
        source_paths: list[str] = payload["file_path"]
        skills_dir = (
            (Path(APIX_BASE_DIR) / user_uid / "apix_skills").expanduser().resolve()
        )
        skills_dir.mkdir(parents=True, exist_ok=True)

        skills_info: list[dict] = []
        for source_path in source_paths:
            source = Path(source_path).expanduser().resolve(strict=True)
            if not source.is_file():
                raise ValueError(f"Source path is not a file: {source}")
            if source.suffix.lower() != ".zip":
                raise ValueError(f"Skill package must be a zip file: {source.name}")

            skill_id = uuid4().hex
            package_path = self._next_available_package_path(skills_dir, source.name)
            await asyncio.to_thread(shutil.move, str(source), str(package_path))
            try:
                package_info = await asyncio.to_thread(
                    self._inspect_skill_package, package_path
                )
            except Exception as error:
                try:
                    package_path.unlink(missing_ok=True)
                except OSError as cleanup_error:
                    logger.warning(
                        f"Failed to delete invalid skill package "
                        f"path={package_path}: {cleanup_error}"
                    )
                raise ValueError(
                    f"Invalid skill package {source.name}: {error}"
                ) from error

            skills_info.append(
                {
                    "skill_id": skill_id,
                    "package_path": str(package_path),
                    **package_info,
                }
            )
            logger.info(
                f"Skill package saved user_uid={user_uid} skill_id={skill_id} "
                f"skill_name={package_info['skill_name']} package_path={package_path}"
            )

        return {"success": True, "messages": skills_info}

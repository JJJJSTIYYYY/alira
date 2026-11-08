"""Workspace uploads and skill-package storage."""

import asyncio
import shutil
from pathlib import Path
from uuid import uuid4

from alira.database.services.files.packages import (
    inspect_skill_package,
    next_available_package_path,
)
from alira.database.utils.handlers import service_handler
from alira.utils.logger import logger
from alira.shared.application.config import ALIRA_BASE_DIR


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
            (Path(ALIRA_BASE_DIR) / user_uid / "alira_skills").expanduser().resolve()
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
            package_path = next_available_package_path(skills_dir, source.name)
            await asyncio.to_thread(shutil.move, str(source), str(package_path))
            try:
                package_info = await asyncio.to_thread(
                    inspect_skill_package, package_path
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

import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from alira.modules.database.executor import StoreExecutor
from alira.modules.database.services.cache import CacheService
from alira.modules.database.services.data import DataService
from alira.modules.database.services.files import FileService


def make_executor(*, data_store, file_store):
    return StoreExecutor(
        cache_store=CacheService(),
        data_store=data_store,
        file_store=file_store,
    )


def make_skill_zip(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "demo/SKILL.md",
            "---\nname: demo\ndescription: Demo skill\nversion: 1.2.3\n---\nbody\n",
        )
    return path


@pytest.mark.asyncio
async def test_upload_files_checks_user_then_saves_file():
    payload = {
        "user_uid": "user-1",
        "file_path": ["/upload/demo.txt"],
        "workspace": "/workspace/demo",
    }
    expected = {"success": True, "messages": [{"file_name": "demo.txt"}]}
    data_store = SimpleNamespace(
        ensure_user_exists=AsyncMock(
            return_value={"success": True, "messages": "success"}
        )
    )
    file_store = SimpleNamespace(save_files=AsyncMock(return_value=expected))
    executor = make_executor(data_store=data_store, file_store=file_store)

    result = await executor.upload_files(payload)

    assert result == expected
    data_store.ensure_user_exists.assert_awaited_once_with(payload)
    file_store.save_files.assert_awaited_once_with(payload)


@pytest.mark.asyncio
async def test_upload_files_short_circuits_for_missing_user():
    failure = {"success": False, "messages": "user missing"}
    data_store = SimpleNamespace(ensure_user_exists=AsyncMock(return_value=failure))
    file_store = SimpleNamespace(save_files=AsyncMock())
    executor = make_executor(data_store=data_store, file_store=file_store)

    result = await executor.upload_files({"user_uid": "missing"})

    assert result == failure
    file_store.save_files.assert_not_awaited()


@pytest.mark.asyncio
async def test_upload_files_converts_unexpected_exception():
    data_store = SimpleNamespace(
        ensure_user_exists=AsyncMock(side_effect=RuntimeError("database down"))
    )
    executor = make_executor(
        data_store=data_store,
        file_store=SimpleNamespace(save_files=AsyncMock()),
    )

    result = await executor.upload_files({"user_uid": "user-1"})

    assert result == {"success": False, "messages": "fail: database down"}


@pytest.mark.asyncio
async def test_create_skills_returns_file_metadata():
    payload = {"user_uid": "user-1", "file_path": ["/upload/demo.zip"]}
    file_result = {
        "success": True,
        "messages": [
            {
                "skill_id": "skill-1",
                "skill_name": "demo",
                "skill_description": "Demo skill",
                "skill_version": "1.2.3",
                "package_path": "/data/user-1/alira_skills/demo.zip",
                "package_size": 128,
                "package_sha256": "abc123",
            }
        ],
    }
    data_store = SimpleNamespace(
        ensure_user_exists=AsyncMock(return_value={"success": True}),
    )
    file_store = SimpleNamespace(
        import_skill_packages=AsyncMock(return_value=file_result)
    )
    executor = make_executor(data_store=data_store, file_store=file_store)

    assert await executor.create_skills(payload) == file_result
    data_store.ensure_user_exists.assert_awaited_once_with(payload)
    file_store.import_skill_packages.assert_awaited_once_with(payload)


@pytest.mark.asyncio
async def test_create_skills_short_circuits_on_user_or_file_failure():
    user_failure = {"success": False, "messages": "user missing"}
    data_store = SimpleNamespace(
        ensure_user_exists=AsyncMock(return_value=user_failure),
    )
    file_store = SimpleNamespace(import_skill_packages=AsyncMock())
    executor = make_executor(data_store=data_store, file_store=file_store)

    assert await executor.create_skills({"user_uid": "missing"}) == user_failure
    file_store.import_skill_packages.assert_not_awaited()

    file_failure = {"success": False, "messages": "invalid package"}
    data_store.ensure_user_exists.return_value = {"success": True}
    file_store.import_skill_packages.return_value = file_failure

    assert await executor.create_skills({"user_uid": "user-1"}) == file_failure


@pytest.mark.asyncio
async def test_create_skills_converts_unexpected_exception():
    data_store = SimpleNamespace(
        ensure_user_exists=AsyncMock(return_value={"success": True}),
    )
    file_store = SimpleNamespace(
        import_skill_packages=AsyncMock(side_effect=RuntimeError("filesystem down"))
    )
    executor = make_executor(data_store=data_store, file_store=file_store)

    result = await executor.create_skills({"user_uid": "user-1"})

    assert result == {"success": False, "messages": "fail: filesystem down"}


def test_removed_metadata_actions_are_not_exported():
    executor = make_executor(data_store=SimpleNamespace(), file_store=FileService())
    handlers = executor.export_handlers()
    assert "create_skills" in handlers
    assert {
        "list_longterm_memories",
        "create_longterm_memory",
        "update_longterm_memory",
        "update_skill",
        "list_skills",
        "get_skill",
    }.isdisjoint(handlers)


@pytest.mark.asyncio
async def test_create_skills_saves_archive_without_metadata_tables(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        "alira.modules.database.services.files.service.ALIRA_BASE_DIR",
        str(tmp_path / "files"),
    )
    source = make_skill_zip(tmp_path / "demo.zip")
    data_store = DataService(tmp_path / "alira.sqlite3")
    await data_store.start()
    try:
        assert (
            await data_store.create_user(
                {"user_uid": "user-1", "username": "demo", "password": "test"}
            )
        )["success"]
        executor = make_executor(data_store=data_store, file_store=FileService())
        result = await executor.create_skills(
            {"user_uid": "user-1", "file_path": [str(source)]}
        )
        assert result["success"]
        skill = result["messages"][0]
        saved = Path(skill["package_path"])
        assert not source.exists()
        assert saved == tmp_path / "files" / "user-1" / "alira_skills" / "demo.zip"
        assert skill["skill_name"] == "demo"
        assert skill["skill_description"] == "Demo skill"
        assert skill["skill_version"] == "1.2.3"
        assert skill["package_size"] == saved.stat().st_size
        assert len(skill["package_sha256"]) == 64
        with zipfile.ZipFile(saved) as archive:
            assert archive.read("demo/SKILL.md").endswith(b"body\n")
    finally:
        await data_store.stop()

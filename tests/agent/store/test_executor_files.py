from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import zipfile

import pytest

from apix.agent.store.core import executor as execute_module
from apix.agent.store.core.executor import StoreExecutor
from apix.agent.store.core.services.cache import CacheService


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
    data_store = SimpleNamespace(
        ensure_user_exists=AsyncMock(return_value=failure)
    )
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
async def test_create_skills_persists_metadata_and_returns_visible_fields(monkeypatch):
    payload = {"user_uid": "user-1", "file_path": ["/upload/demo.zip"]}
    skill_info = {
        "skill_id": "skill-1",
        "skill_name": "demo",
        "skill_description": "Demo skill",
        "skill_version": "1.2.3",
        "package_path": "/data/user-1/apix_skills/demo.zip",
        "package_size": 128,
        "package_sha256": "abc123",
    }
    file_result = {
        "success": True,
        "messages": [skill_info],
    }
    skill_payload = {
        "user_uid": payload["user_uid"],
        "skills": file_result.get("messages", []),
    }
    data_store = SimpleNamespace(
        ensure_user_exists=AsyncMock(return_value={"success": True}),
        create_skills=AsyncMock(
            return_value={"success": True, "messages": "success"}
        ),
    )
    file_store = SimpleNamespace(
        import_skill_packages=AsyncMock(return_value=file_result)
    )
    executor = make_executor(data_store=data_store, file_store=file_store)
    monkeypatch.setattr(
        execute_module,
        "datetime",
        SimpleNamespace(now=lambda: datetime(2026, 7, 21, 20, 30, 0)),
    )

    result = await executor.create_skills(payload)

    assert result == {
        "success": True,
        "messages": [
            {
                "skill_id": "skill-1",
                "skill_name": "demo",
                "skill_description": "Demo skill",
                "skill_version": "1.2.3",
                "package_size": 128,
                "is_active": False,
                "upload_at": "2026-07-21 20:30:00",
            }
        ],
    }
    data_store.ensure_user_exists.assert_awaited_once_with(payload)
    file_store.import_skill_packages.assert_awaited_once_with(payload)
    data_store.create_skills.assert_awaited_once_with(skill_payload)


@pytest.mark.asyncio
async def test_create_skills_short_circuits_on_user_or_file_failure():
    user_failure = {"success": False, "messages": "user missing"}
    data_store = SimpleNamespace(
        ensure_user_exists=AsyncMock(return_value=user_failure),
        create_skills=AsyncMock(),
    )
    file_store = SimpleNamespace(import_skill_packages=AsyncMock())
    executor = make_executor(data_store=data_store, file_store=file_store)

    assert await executor.create_skills({"user_uid": "missing"}) == user_failure
    file_store.import_skill_packages.assert_not_awaited()
    data_store.create_skills.assert_not_awaited()

    file_failure = {"success": False, "messages": "invalid package"}
    data_store.ensure_user_exists.return_value = {"success": True}
    file_store.import_skill_packages.return_value = file_failure

    assert await executor.create_skills({"user_uid": "user-1"}) == file_failure
    data_store.create_skills.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_skills_converts_unexpected_exception():
    data_store = SimpleNamespace(
        ensure_user_exists=AsyncMock(return_value={"success": True}),
        create_skills=AsyncMock(),
    )
    file_store = SimpleNamespace(
        import_skill_packages=AsyncMock(side_effect=RuntimeError("filesystem down"))
    )
    executor = make_executor(data_store=data_store, file_store=file_store)

    result = await executor.create_skills({"user_uid": "user-1"})

    assert result == {"success": False, "messages": "fail: filesystem down"}
    data_store.create_skills.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "executor_method,store_method,payload,expected",
    [
        (
            "update_skill",
            "update_skill",
            {"user_uid": "user-1", "skill_id": "skill-1", "is_active": True},
            {"success": True, "messages": "updated"},
        ),
        (
            "list_skills",
            "list_skills",
            {"user_uid": "user-1", "limit": 10},
            {"success": True, "messages": [{"skill_id": "skill-1"}]},
        ),
        (
            "get_skill",
            "get_skill",
            {"user_uid": "user-1", "skill_id": "skill-1"},
            {"success": True, "messages": [{"skill_id": "skill-1"}]},
        ),
    ],
)
async def test_skill_handlers_forward_payload(
    executor_method, store_method, payload, expected
):
    store_handler = AsyncMock(return_value=expected)
    data_store = SimpleNamespace(**{store_method: store_handler})
    executor = make_executor(
        data_store=data_store,
        file_store=SimpleNamespace(),
    )

    result = await getattr(executor, executor_method)(payload)

    assert result == expected
    store_handler.assert_awaited_once_with(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "executor_method,store_method",
    [
        ("update_skill", "update_skill"),
        ("list_skills", "list_skills"),
        ("get_skill", "get_skill"),
    ],
)
async def test_skill_handlers_convert_store_exception(
    executor_method, store_method
):
    data_store = SimpleNamespace(
        **{store_method: AsyncMock(side_effect=RuntimeError("database down"))}
    )
    executor = make_executor(
        data_store=data_store,
        file_store=SimpleNamespace(),
    )

    result = await getattr(executor, executor_method)({"user_uid": "user-1"})

    assert result == {"success": False, "messages": "fail: database down"}

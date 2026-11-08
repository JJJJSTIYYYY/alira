import asyncio
import hashlib
import zipfile
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from alira.modules.database.services.files import FileService
from alira.modules.database.services.files import service as file_module


def make_skill_zip(path: Path, frontmatter: str | None, *, nested=True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    member = "sample-skill/SKILL.md" if nested else "SKILL.md"
    with zipfile.ZipFile(path, "w") as archive:
        if frontmatter is not None:
            archive.writestr(member, frontmatter)
        else:
            archive.writestr("README.md", "no skill metadata")
    return path


@pytest.mark.asyncio
async def test_save_files_copies_sources_and_returns_workspace_paths(tmp_path):
    source_one = tmp_path / "first.txt"
    source_two = tmp_path / "second.bin"
    source_one.write_text("hello", encoding="utf-8")
    source_two.write_bytes(b"\x00\x01")
    workspace = tmp_path / "workspace"

    result = await FileService().save_files(
        {
            "file_path": [str(source_one), str(source_two)],
            "workspace": str(workspace),
        }
    )

    assert result["success"] is True
    assert result["messages"] == [
        {
            "file_name": "first.txt",
            "saved_path": str(workspace / "user_upload" / "first.txt"),
            "ws_saved_path": "user_upload/first.txt",
        },
        {
            "file_name": "second.bin",
            "saved_path": str(workspace / "user_upload" / "second.bin"),
            "ws_saved_path": "user_upload/second.bin",
        },
    ]
    assert (workspace / "user_upload" / "first.txt").read_text() == "hello"
    assert source_one.exists(), "save_files copies rather than moves uploads"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload_factory",
    [
        lambda root: {"workspace": str(root)},
        lambda root: {"file_path": [str(root / "missing")], "workspace": str(root)},
        lambda root: {"file_path": [str(root)], "workspace": str(root / "ws")},
    ],
)
async def test_save_files_reports_invalid_payloads(tmp_path, payload_factory):
    result = await FileService().save_files(payload_factory(tmp_path))
    assert result["success"] is False
    assert isinstance(result["messages"], str) and result["code"] != 0


@pytest.mark.asyncio
async def test_import_skill_packages_moves_valid_zip_and_extracts_metadata(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(file_module, "ALIRA_BASE_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(
        "alira.modules.database.services.files.service.uuid4",
        lambda: SimpleNamespace(hex="fixedid"),
    )
    source = make_skill_zip(
        tmp_path / "skill.zip",
        "---\nname: demo\ndescription: Demo skill\nversion: 2.1.0\n---\n# Demo\n",
    )
    expected_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    expected_size = source.stat().st_size

    result = await FileService().import_skill_packages(
        {"user_uid": "user-1", "file_path": [str(source)]}
    )

    assert result["success"] is True
    assert len(result["messages"]) == 1
    info = result["messages"][0]
    assert info == {
        "skill_id": "fixedid",
        "skill_name": "demo",
        "skill_description": "Demo skill",
        "skill_version": "2.1.0",
        "package_path": str(
            tmp_path / "data" / "user-1" / "alira_skills" / "skill.zip"
        ),
        "package_size": expected_size,
        "package_sha256": expected_hash,
    }
    assert not source.exists()
    assert Path(info["package_path"]).is_file()


@pytest.mark.asyncio
async def test_import_skill_packages_uses_default_version(monkeypatch, tmp_path):
    monkeypatch.setattr(file_module, "ALIRA_BASE_DIR", str(tmp_path))
    source = make_skill_zip(
        tmp_path / "skill.zip",
        "---\nname: demo\ndescription: Demo skill\n---\nbody\n",
        nested=False,
    )

    result = await FileService().import_skill_packages(
        {"user_uid": "user-1", "file_path": [str(source)]}
    )

    assert result["success"] is True
    assert result["messages"][0]["skill_version"] == "0.0.1"


@pytest.mark.asyncio
async def test_import_skill_packages_adds_incrementing_index_on_name_conflict(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(file_module, "ALIRA_BASE_DIR", str(tmp_path / "data"))
    skills_dir = tmp_path / "data" / "user-1" / "alira_skills"
    skills_dir.mkdir(parents=True)
    existing = skills_dir / "skill.zip"
    existing.write_bytes(b"existing package must not be overwritten")
    frontmatter = "---\nname: demo\ndescription: Demo skill\n---\nbody\n"
    first = make_skill_zip(tmp_path / "first" / "skill.zip", frontmatter)
    second = make_skill_zip(tmp_path / "second" / "skill.zip", frontmatter)

    result = await FileService().import_skill_packages(
        {"user_uid": "user-1", "file_path": [str(first), str(second)]}
    )

    assert result["success"] is True
    assert [Path(item["package_path"]).name for item in result["messages"]] == [
        "skill_1.zip",
        "skill_2.zip",
    ]
    assert existing.read_bytes() == b"existing package must not be overwritten"
    assert sorted(path.name for path in skills_dir.iterdir()) == [
        "skill.zip",
        "skill_1.zip",
        "skill_2.zip",
    ]


@pytest.mark.asyncio
async def test_same_package_name_is_isolated_by_user(monkeypatch, tmp_path):
    monkeypatch.setattr(file_module, "ALIRA_BASE_DIR", str(tmp_path / "data"))
    frontmatter = "---\nname: demo\ndescription: Demo skill\n---\nbody\n"
    first = make_skill_zip(tmp_path / "first" / "skill.zip", frontmatter)
    second = make_skill_zip(tmp_path / "second" / "skill.zip", frontmatter)
    service = FileService()

    first_result = await service.import_skill_packages(
        {"user_uid": "user-1", "file_path": [str(first)]}
    )
    second_result = await service.import_skill_packages(
        {"user_uid": "user-2", "file_path": [str(second)]}
    )

    assert Path(first_result["messages"][0]["package_path"]) == (
        tmp_path / "data" / "user-1" / "alira_skills" / "skill.zip"
    )
    assert Path(second_result["messages"][0]["package_path"]) == (
        tmp_path / "data" / "user-2" / "alira_skills" / "skill.zip"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "frontmatter",
    [
        None,
        "# no frontmatter",
        "---\nname: demo\n",
        "---\n- item\n---\n",
        "---\nname: demo\n---\n",
    ],
)
async def test_invalid_skill_package_is_deleted(monkeypatch, tmp_path, frontmatter):
    monkeypatch.setattr(file_module, "ALIRA_BASE_DIR", str(tmp_path))
    skills_dir = tmp_path / "user-1" / "alira_skills"
    source = make_skill_zip(tmp_path / "invalid.zip", frontmatter)

    result = await FileService().import_skill_packages(
        {"user_uid": "user-1", "file_path": [str(source)]}
    )

    assert result["success"] is False
    assert result["code"] == 1001
    assert result["messages"] == "Invalid request payload."
    assert list(skills_dir.iterdir()) == []
    assert not source.exists(), "the selected archive was moved before validation"


@pytest.mark.asyncio
async def test_import_skill_packages_rejects_non_zip_without_moving_it(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(file_module, "ALIRA_BASE_DIR", str(tmp_path))
    source = tmp_path / "skill.txt"
    source.write_text("not a zip", encoding="utf-8")

    result = await FileService().import_skill_packages(
        {"user_uid": "user-1", "file_path": [str(source)]}
    )

    assert result["success"] is False
    assert result["code"] == 1001
    assert source.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "missing_field"),
    [({"file_path": []}, "user_uid"), ({"user_uid": "user-1"}, "file_path")],
)
async def test_import_skill_packages_normalizes_missing_fields(payload, missing_field):
    result = await FileService().import_skill_packages(payload)

    assert result == {
        "code": 1001,
        "success": False,
        "messages": "Invalid request payload.",
    }


@pytest.mark.asyncio
async def test_import_skill_packages_deletes_corrupt_zip(monkeypatch, tmp_path):
    monkeypatch.setattr(file_module, "ALIRA_BASE_DIR", str(tmp_path / "data"))
    source = tmp_path / "invalid.zip"
    source.write_bytes(b"not a ZIP archive")

    result = await FileService().import_skill_packages(
        {"user_uid": "user-1", "file_path": [str(source)]}
    )

    assert result["success"] is False
    assert result["code"] == 1001
    assert not source.exists()
    assert list((tmp_path / "data" / "user-1" / "alira_skills").iterdir()) == []


@pytest.mark.asyncio
async def test_skill_inspection_keeps_event_loop_available(monkeypatch, tmp_path):
    monkeypatch.setattr(file_module, "ALIRA_BASE_DIR", str(tmp_path / "data"))
    source = make_skill_zip(
        tmp_path / "skill.zip",
        "---\nname: demo\ndescription: Demo skill\n---\nbody\n",
    )
    service = FileService()
    inspect_package = file_module.inspect_skill_package
    started = Event()
    release = Event()

    def blocked_inspection(package_path):
        started.set()
        if not release.wait(timeout=3):
            raise TimeoutError("The event loop did not release skill inspection")
        return inspect_package(package_path)

    monkeypatch.setattr(file_module, "inspect_skill_package", blocked_inspection)
    operation = asyncio.create_task(
        service.import_skill_packages(
            {"user_uid": "user-1", "file_path": [str(source)]}
        )
    )
    try:
        assert await asyncio.wait_for(asyncio.to_thread(started.wait, 1), timeout=2), (
            "Skill inspection did not start"
        )
        assert not operation.done(), (
            "Inspection blocked the event loop until its timeout"
        )
        release.set()
        result = await asyncio.wait_for(operation, timeout=2)
        assert result["success"] is True
    finally:
        release.set()
        if not operation.done():
            await asyncio.wait_for(operation, timeout=2)

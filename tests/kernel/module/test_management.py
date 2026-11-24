"""Declaration/lock orchestration preserves user intent across command modes."""

import asyncio
import hashlib
import json
from unittest.mock import AsyncMock
from zipfile import ZipFile

import pytest

from alira.kernel.module.dependencies import DependencyError
from alira.kernel.module.dependencies.uv import EMPTY_LOCK


def declare(root, name, *, required=(), version="1"):
    directory = root / name
    (directory / "src").mkdir(parents=True)
    (directory / "main.py").write_text(
        "from alira.kernel.module import BaseModule\n"
        f"module = BaseModule({name!r}, {version!r}, required_modules={list(required)!r})\n"
    )
    return directory


@pytest.fixture
def managed(tmp_path, module_manager_factory, monkeypatch):
    root = tmp_path / "modules"
    root.mkdir()
    manager = module_manager_factory(root)
    monkeypatch.setattr(manager._dependencies, "install", AsyncMock())
    monkeypatch.setattr(manager._dependencies, "uninstall_packages", AsyncMock())
    return manager


async def test_sync_preserves_removed_and_unselected_sources(managed):
    manager = managed
    declare(manager.modules_dir, "selected")
    declare(manager.modules_dir, "unselected")
    await manager.add("selected", no_sync=True)
    assert manager.deployments == {"selected"}
    await manager.remove("selected")
    await manager.sync()
    assert manager.deployments == set()
    assert (manager.modules_dir / "selected/main.py").is_file()
    assert (manager.modules_dir / "unselected/main.py").is_file()
    manager._dependencies.install.assert_awaited()
    assert manager._dependencies.ledger.read_lock()["modules"] == {}


async def test_locked_check_and_frozen_preserve_state_on_changed_inputs(managed):
    manager = managed
    directory = declare(manager.modules_dir, "demo")
    await manager.add("demo", no_sync=True)
    ledger = manager._dependencies.ledger
    before = ledger.snapshot()
    await manager.lock(check=True)
    assert ledger.snapshot() == before
    # A changed requirements input makes the generated lock stale.
    (directory / "requirements.txt").write_text("new-package==1\n")
    for operation in (manager.lock(check=True), manager.sync(locked=True)):
        with pytest.raises(DependencyError, match="outdated"):
            await operation
        assert ledger.snapshot() == before
    # Frozen does not inspect a newer declaration; source files remain unchanged.
    (directory / "requirements.txt").unlink()
    declaration = ledger.read()
    declaration["requires-kernel"] = ">=99"
    ledger.write_declaration(declaration)
    changed = ledger.snapshot()
    await manager.sync(frozen=True)
    assert ledger.snapshot() == changed
    with pytest.raises(DependencyError, match="outdated"):
        await manager.sync(locked=True)


async def test_partial_batch_keeps_published_source_and_retries_without_extraction(
    managed, tmp_path, monkeypatch
):
    manager = managed
    archive = tmp_path / "first.zip"
    with ZipFile(archive, "w") as stream:
        stream.writestr("main.py", "module = BaseModule('first', '1')\n")
        stream.writestr("src/__init__.py", "")
    ledger = manager._dependencies.ledger
    before = ledger.snapshot()
    with pytest.raises(DependencyError):
        await manager.add(str(archive), str(tmp_path / "missing.zip"), no_sync=True)
    assert (manager.modules_dir / "first/main.py").is_file()
    assert ledger.snapshot() == before
    monkeypatch.setattr(
        "alira.kernel.module.sources.archive.prepare_module_source",
        AsyncMock(side_effect=AssertionError("Published source was extracted twice")),
    )
    await manager.add(str(archive), no_sync=True)
    assert manager.deployments == {"first"}
    assert ledger.read()["modules"]["first"]["source"]["type"] == "path"


async def test_cancelled_package_install_restores_files_but_retains_source(
    managed, monkeypatch
):
    manager = managed
    declare(manager.modules_dir, "before")
    await manager.add("before", no_sync=True)
    snapshot = manager._dependencies.ledger.snapshot()
    declare(manager.modules_dir, "after")
    monkeypatch.setattr(
        manager._dependencies, "install", AsyncMock(side_effect=asyncio.CancelledError)
    )
    with pytest.raises(asyncio.CancelledError):
        await manager.add("after")
    assert manager._dependencies.ledger.snapshot() == snapshot
    assert manager.deployments == {"before"}
    assert (manager.modules_dir / "after/main.py").is_file()


async def test_declared_version_mismatch_requires_explicit_add(managed):
    directory = declare(managed.modules_dir, "demo")
    await managed.add("demo", no_sync=True)
    snapshot = managed._dependencies.ledger.snapshot()
    (directory / "main.py").write_text("module = BaseModule('demo', '2')\n")
    with pytest.raises(DependencyError, match="declares version"):
        await managed.lock()
    assert managed._dependencies.ledger.snapshot() == snapshot
    await managed.add("demo", no_sync=True)
    assert managed._dependencies.ledger.read_lock()["modules"]["demo"]["version"] == "2"


async def test_migration_reuses_old_artifacts_without_resolving(
    managed, tmp_path, monkeypatch
):
    manager = managed
    declare(manager.modules_dir, "demo")
    home = tmp_path / "legacy"
    home.mkdir()
    manager._home = home
    (home / "pylock.toml").write_bytes(EMPTY_LOCK)
    previous = {
        "format": 1,
        "lock_hash": hashlib.sha256(EMPTY_LOCK).hexdigest(),
        "modules": {"demo": {"packages": {}}},
    }
    (home / "modules.json").write_text(json.dumps(previous))
    monkeypatch.setattr(
        manager._dependencies,
        "resolve",
        AsyncMock(side_effect=AssertionError("Migration resolved packages")),
    )
    old = {path: path.read_bytes() for path in home.iterdir()}
    await manager.migrate()
    assert manager.deployments == {"demo"}
    for path, content in old.items():
        assert path.read_bytes() == content
        assert path.with_name(path.name + ".v1.bak").read_bytes() == content
    manager._dependencies.install.assert_not_awaited()
    await manager.lock(check=True)


async def test_source_root_is_required_before_state_creation(
    tmp_path, module_manager_factory
):
    manager = module_manager_factory(tmp_path / "absent")
    for method, args in (
        ("add", ("demo",)),
        ("sync", ()),
        ("lock", ()),
        ("remove", ("demo",)),
        ("prepare_run", ()),
    ):
        with pytest.raises(DependencyError, match="source checkout"):
            await getattr(manager, method)(*args)
    assert not manager._dependencies.ledger.directory.exists()


async def test_remove_rejects_required_module_without_state_or_source_changes(managed):
    declare(managed.modules_dir, "base")
    declare(managed.modules_dir, "child", required=["base>=1"])
    await managed.add("child", no_sync=True)
    snapshot = managed._dependencies.ledger.snapshot()
    with pytest.raises(DependencyError, match="requires a removed"):
        await managed.remove("base", purge=True)
    assert managed._dependencies.ledger.snapshot() == snapshot
    assert (managed.modules_dir / "base/main.py").is_file()


async def test_locked_sync_restores_missing_source_before_loading_and_checks_selection(
    managed, tmp_path
):
    source = declare(tmp_path / "external", "demo")
    await managed.add(str(source), no_sync=True)
    import shutil

    shutil.rmtree(managed.modules_dir / "demo")
    declare(managed.modules_dir, "rogue")
    with pytest.raises(DependencyError, match="not declared"):
        await managed.prepare_run("rogue", locked=True)
    assert not (managed.modules_dir / "demo/main.py").exists()
    await managed.prepare_run("demo", locked=True)
    assert (managed.modules_dir / "demo/main.py").is_file()
    await managed.lock(check=True)


async def test_run_retries_a_concurrent_state_replacement_once(managed, monkeypatch):
    declare(managed.modules_dir, "demo")
    await managed.add("demo", no_sync=True)
    ledger = managed._dependencies.ledger
    read_lock = ledger.read_lock
    calls = 0

    def racing_read():
        nonlocal calls
        calls += 1
        if calls == 1:
            # Model an atomic writer commit after prepare_run checked journal.
            declaration = ledger.read()
            declaration["requires-kernel"] = ">=3"
            ledger.write_declaration(declaration)
            raise DependencyError("read conflicted with writer")
        return read_lock()

    monkeypatch.setattr(ledger, "read_lock", racing_read)
    monkeypatch.setattr(managed, "sync", AsyncMock())
    await managed.prepare_run("demo")
    assert calls == 2
    managed.sync.assert_awaited_once()


async def test_frozen_run_routes_incompatible_kernel_through_install_validation(
    managed, monkeypatch
):
    declare(managed.modules_dir, "demo")
    await managed.add("demo", no_sync=True)
    ledger = managed._dependencies.ledger
    lock = ledger.read_lock()
    lock["kernel"]["version"] = "0"
    ledger.write_lock(lock)
    monkeypatch.setattr(managed, "sync", AsyncMock())
    await managed.prepare_run("demo", frozen=True)
    managed.sync.assert_awaited_once_with(
        locked=False, frozen=True, background=True, prune=False
    )


@pytest.mark.parametrize("existing_backup", [False, True])
async def test_migration_failure_preserves_old_files_and_existing_backups(
    managed, tmp_path, monkeypatch, existing_backup
):
    manager = managed
    home = tmp_path / "legacy-state"
    home.mkdir()
    manager._home = home
    (home / "pylock.toml").write_bytes(EMPTY_LOCK)
    (home / "modules.json").write_text(
        json.dumps(
            {
                "format": 1,
                "lock_hash": hashlib.sha256(EMPTY_LOCK).hexdigest(),
                "modules": {},
            }
        )
    )
    if existing_backup:
        (home / "modules.json.v1.bak").write_bytes(b"a previous migration backup")
    before = {path.name: path.read_bytes() for path in home.iterdir()}
    ledger = manager._dependencies.ledger
    original_write = ledger.atomic_write

    def fail_second_backup(path, content):
        if path.name == "pylock.toml.v1.bak":
            raise OSError("backup destination unavailable")
        original_write(path, content)

    monkeypatch.setattr(type(ledger), "atomic_write", staticmethod(fail_second_backup))
    with pytest.raises((DependencyError, OSError)):
        await manager.migrate()
    assert {path.name: path.read_bytes() for path in home.iterdir()} == before
    assert not ledger.path.exists()
    assert not ledger.lockfile.exists()


def test_state_directory_is_fixed_under_home(tmp_path, monkeypatch):
    from pathlib import Path

    from alira.kernel.module import ModuleManager
    from alira.shared.application import config

    home = tmp_path / "home"
    root = tmp_path / "modules"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("ALIRA_HOME", str(tmp_path / "ignored-home"))
    monkeypatch.setattr(config, "ALIRA_BASE_DIR", str(tmp_path / "module-data"))
    manager = ModuleManager._new_for_tests()
    manager.modules_dir = root
    identity = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]
    assert manager._dependencies.ledger.directory == home / ".alira/projects" / identity
    assert manager._home == home / ".alira"
    assert not home.exists()

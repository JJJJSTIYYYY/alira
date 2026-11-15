"""Generated modules follow the documented structure and declaration contract."""

import sys
from pathlib import Path
from typing import get_type_hints
from uuid import uuid4

import pytest

from alira.kernel.module import BaseModule, DependencyError, ModuleStatus
from alira.kernel.module.catalog import ModuleSpec


@pytest.fixture
def manager(tmp_path, monkeypatch, module_manager_factory):
    package = f"modules_{uuid4().hex}"
    root = tmp_path / package
    root.mkdir()
    (root / "__init__.py").touch()
    monkeypatch.syspath_prepend(str(tmp_path))
    return module_manager_factory(root, package=package)


@pytest.mark.parametrize("name", ["demo", "sample_module", "_1", "模块"])
async def test_init_creates_importable_minimal_module_without_deployment(manager, name):
    ledger = manager._dependencies.ledger
    ledger.path.parent.mkdir(parents=True)
    ledger.path.write_text("{broken records")

    directory = await manager.init(name)

    assert directory == manager.modules_dir / name
    assert {
        path.relative_to(directory).as_posix()
        for path in directory.rglob("*") if path.is_file()
    } == {
        "__init__.py",
        "main.py",
        "requirements.txt",
        "src/__init__.py",
        "src/shared/__init__.py",
        "src/shared/events.py",
    }
    assert (directory / "requirements.txt").read_text() == ""
    assert ledger.path.read_text() == "{broken records"
    assert not ledger.lockfile.exists()
    assert manager.list() == [name]
    assert not manager.modules and not manager.loaded
    assert f"{manager.package}.{name}.main" not in sys.modules

    spec = ModuleSpec.read(directory, name=name)
    assert spec.name == name
    assert spec.required_modules == ()
    module = manager.discover()[name]
    assert isinstance(module, BaseModule)
    assert module.name == name and module.version == "0.1.0"
    assert f"{manager.package}.{name}.src" not in sys.modules
    await module.startup()
    assert "health" in type(module).__dict__
    assert get_type_hints(module.health)["return"] is ModuleStatus
    assert (await manager.check_health(name))[name] == {
        "health": False, "message": "Health check is not implemented."
    }
    await module.shutdown()


@pytest.mark.parametrize("name", ["../outside", "/absolute", "bad-name", "class", "__hidden"])
async def test_init_rejects_invalid_names_without_creating_sources(manager, name):
    with pytest.raises(DependencyError):
        await manager.init(name)
    assert list(manager.modules_dir.iterdir()) == [manager.modules_dir / "__init__.py"]


@pytest.mark.parametrize("kind", ["directory", "file", "symlink"])
async def test_init_preserves_existing_sources(manager, tmp_path, kind):
    destination = manager.modules_dir / "demo"
    if kind == "directory":
        destination.mkdir()
        (destination / "local.txt").write_text("Keep this file")
    elif kind == "file":
        destination.write_text("Keep this file")
    else:
        target = tmp_path / "external"
        target.mkdir()
        (target / "local.txt").write_text("Keep this file")
        destination.symlink_to(target, target_is_directory=True)

    with pytest.raises(DependencyError):
        await manager.init("demo")
    sentinel = destination if kind == "file" else destination / "local.txt"
    assert sentinel.read_text() == "Keep this file"
    assert not (destination / "main.py").exists()


async def test_init_write_failure_does_not_publish_partial_module(manager, monkeypatch):
    write = Path.write_text

    def fail_events(path, *args, **kwargs):
        if path.name == "events.py":
            raise OSError("No space left on device")
        return write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_events)
    with pytest.raises(OSError, match="No space left"):
        await manager.init("demo")
    assert list(manager.modules_dir.iterdir()) == [manager.modules_dir / "__init__.py"]


async def test_init_creates_a_missing_module_root(manager):
    manager.modules_dir = manager.modules_dir / "new_root"
    directory = await manager.init("demo")
    assert (directory / "src/shared/events.py").is_file()

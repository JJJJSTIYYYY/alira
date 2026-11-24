"""Dependency graph validation precedes solving, startup and state replacement."""

import importlib
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4
from zipfile import ZipFile

import pytest

from alira.cli import entrypoint
from alira.kernel.module import DependencyError


def declaration(names):
    return {
        "format": 2,
        "requires-kernel": ">=3,<4",
        "modules": {
            name: {
                "version": "1.0",
                "source": {"type": "builtin"},
                "required-modules": [],
                "requirements": None,
                "requirements-hash": None,
            }
            for name in names
        },
    }


@pytest.mark.parametrize(
    "command",
    [
        "run",
        "run-sync",
        "add",
        "add-no-sync",
        "sync",
        "lock",
        "install",
        "manifest",
        "manifest-unrelated",
        "remove",
        "uninstall",
    ],
)
@pytest.mark.parametrize(
    "problem, message",
    [
        ("missing", "not found|Unknown module|unrecorded"),
        ("cycle", "cycle"),
        ("version", "version conflict"),
        ("kernel", "requires kernel"),
        ("syntax", "Invalid module requirement"),
        ("invalid-version", "Invalid version metadata"),
        ("invalid-list", "required_modules must be a list"),
    ],
)
async def test_commands_reject_invalid_graphs_before_state_or_package_mutation(
    tmp_path, monkeypatch, module_manager_factory, command, problem, message
):
    package = f"preflight_{uuid4().hex}"
    root = tmp_path / package
    root.mkdir()
    (root / "__init__.py").touch()
    marker = tmp_path / "startup-called"
    dependencies = {
        "missing": ["absent"],
        "cycle": ["web>=1"],
        "version": ["agent>2"],
        "syntax": ["agent=>1"],
    }.get(problem, ["agent>=1"])
    if problem == "invalid-list":
        dependencies = ""
    definitions = {
        "web": (
            "invalid" if problem == "invalid-version" else "1.0",
            dependencies,
            ">=99" if problem == "kernel" else ">=3.0.0",
        ),
        "agent": ("1.0", [], ">=3.0.0"),
        "victim": ("1.0", [], ">=3.0.0"),
    }
    for name, (version, required, kernel) in definitions.items():
        folder = root / name
        (folder / "src").mkdir(parents=True)
        (folder / "__init__.py").touch()
        (folder / "main.py").write_text(
            "from pathlib import Path\nfrom alira.kernel.module import BaseModule\n"
            "class Module(BaseModule):\n"
            f"    async def startup(self): Path({str(marker)!r}).touch()\n"
            f"module=Module({name!r},{version!r},required_modules={required!r},required_kernel={kernel!r})\n"
        )
    manager = module_manager_factory(root, package=package)
    manager._dependencies.ledger.write_declaration(declaration(definitions))
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(entrypoint, "module_manager", manager)
    application = importlib.import_module("alira.cli.application")

    async def start_application(manager, modules):
        await manager.load(*modules, verify_dependencies=False)

    monkeypatch.setattr(application, "run_application", start_application)
    python_resolution = AsyncMock(
        side_effect=AssertionError("Python solver ran before preflight")
    )
    monkeypatch.setattr(
        "alira.kernel.module.dependencies.deployment.resolve_requirements",
        python_resolution,
    )
    arguments = {
        "run": ["run", "web"],
        "run-sync": ["run", "web", "--sync"],
        "add": ["add", "web"],
        "add-no-sync": ["add", "web", "--no-sync"],
        "sync": ["sync"],
        "lock": ["lock"],
        "remove": ["remove", "victim"],
        "uninstall": ["uninstall", "victim"],
    }.get(command)
    if command in {"install", "manifest", "manifest-unrelated"}:
        archive = tmp_path / "incoming.zip"
        prerequisites = [] if command == "manifest-unrelated" else ["web"]
        with ZipFile(archive, "w") as stream:
            stream.writestr(
                "main.py",
                f"from alira.kernel.module import BaseModule\nmodule=BaseModule('incoming','1',required_modules={prerequisites!r})",
            )
            stream.writestr("src/__init__.py", "")
        if command == "install":
            arguments = ["install", str(archive)]
        else:
            manifest = tmp_path / "package.yml"
            manifest.write_text(f"modules:\n  - source: {archive}\n")
            arguments = ["add", "--manifest", str(manifest)]
    before = manager._dependencies.ledger.snapshot()
    with pytest.raises(DependencyError, match=message):
        await entrypoint.run(arguments)
    assert manager._dependencies.ledger.snapshot() == before
    assert not marker.exists()
    # Successfully published sources survive later graph/dependency failures.
    assert (root / "incoming").exists() == (
        command in {"install", "manifest", "manifest-unrelated"}
    )
    assert (root / "victim/main.py").is_file()
    python_resolution.assert_not_awaited()


@pytest.mark.parametrize("command", ["remove", "uninstall"])
async def test_removing_broken_source_is_allowed_when_retained_graph_is_valid(
    tmp_path, monkeypatch, module_manager_factory, command
):
    manager = module_manager_factory(tmp_path)
    for name, text in [
        ("bad", "invalid syntax !!!"),
        ("good", "module=BaseModule('good','1.0')"),
    ]:
        folder = tmp_path / name
        folder.mkdir()
        (folder / "main.py").write_text(text)
    manager._dependencies.ledger.write_declaration(declaration(["bad", "good"]))
    monkeypatch.setattr(entrypoint, "module_manager", manager)
    monkeypatch.setattr(
        "send2trash.send2trash", lambda name: Path(name).rename(tmp_path / "trash")
    )
    await entrypoint.run([command, "bad"])
    assert manager.deployments == {"good"}
    assert (tmp_path / "good/main.py").is_file()
    assert (tmp_path / "bad").exists() == (command == "remove")


@pytest.mark.parametrize("command", ["add", "remove", "uninstall", "sync", "lock"])
async def test_damaged_declarations_require_user_repair(
    tmp_path, monkeypatch, module_manager_factory, command
):
    manager = module_manager_factory(tmp_path)
    folder = tmp_path / "good"
    folder.mkdir()
    (folder / "main.py").write_text("module=BaseModule('good','1.0')")
    ledger = manager._dependencies.ledger
    ledger.path.parent.mkdir(parents=True)
    ledger.path.write_text("{interrupted write")
    before = ledger.snapshot()
    monkeypatch.setattr(entrypoint, "module_manager", manager)
    arguments = [command] if command in {"sync", "lock"} else [command, "good"]
    with pytest.raises(DependencyError, match="repair modules.json"):
        await entrypoint.run(arguments)
    assert ledger.snapshot() == before
    assert (folder / "main.py").is_file()


@pytest.mark.parametrize("command", ["sync", "lock"])
async def test_damaged_generated_lock_can_be_rebuilt_from_valid_declarations(
    tmp_path, monkeypatch, module_manager_factory, command
):
    manager = module_manager_factory(tmp_path)
    folder = tmp_path / "good"
    folder.mkdir()
    (folder / "main.py").write_text("module=BaseModule('good','1.0')")
    ledger = manager._dependencies.ledger
    ledger.write_declaration(declaration(["good"]))
    ledger.lockfile.write_text("{broken lock")
    monkeypatch.setattr(entrypoint, "module_manager", manager)
    monkeypatch.setattr(manager._dependencies, "installed", dict)
    await entrypoint.run([command])
    assert set(ledger.read_lock()["modules"]) == {"good"}


async def test_default_run_synchronizes_before_source_import_and_releases_writer_lock(
    tmp_path, monkeypatch, module_manager_factory
):
    from filelock import FileLock

    package = f"launch_{uuid4().hex}"
    root = tmp_path / package
    folder = root / "demo"
    folder.mkdir(parents=True)
    (root / "__init__.py").touch()
    (folder / "__init__.py").touch()
    synchronized = tmp_path / "synchronized"
    (folder / "main.py").write_text(
        "from pathlib import Path\n"
        f"assert Path({str(synchronized)!r}).exists(), 'entry imported before sync'\n"
        "from alira.kernel.module import BaseModule\n"
        "module=BaseModule('demo','1.0')\n"
    )
    manager = module_manager_factory(root, package=package)
    ledger = manager._dependencies.ledger
    ledger.write_declaration(declaration(["demo"]))
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(entrypoint, "module_manager", manager)
    monkeypatch.setattr(manager._dependencies, "installed", dict)
    install = AsyncMock(side_effect=lambda *args, **kwargs: synchronized.touch())
    monkeypatch.setattr(manager._dependencies, "install", install)
    application = importlib.import_module("alira.cli.application")

    async def application_after_sync(manager, modules):
        assert "demo" in manager.modules
        assert modules == ["demo"]
        assert not ledger.journal.exists()
        with FileLock(ledger.directory / ".lock", timeout=0):
            pass

    launch = AsyncMock(side_effect=application_after_sync)
    monkeypatch.setattr(application, "run_application", launch)
    await entrypoint.run(["run", "demo"])
    install.assert_awaited_once()
    assert install.await_args.kwargs["prune"] is False
    assert ledger.read_lock()["modules"]["demo"]["version"] == "1.0"
    snapshot = ledger.snapshot()
    await entrypoint.run(["run", "demo"])
    assert install.await_count == 1
    assert launch.await_count == 2
    assert ledger.snapshot() == snapshot

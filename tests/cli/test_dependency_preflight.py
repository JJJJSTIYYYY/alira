"""Every graph-changing or loading command shares dependency preflight."""

import importlib
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4
from zipfile import ZipFile

import pytest

from alira.cli import entrypoint
from alira.kernel.module import DependencyError
from alira.kernel.module.dependencies.uv import EMPTY_LOCK


@pytest.mark.parametrize(
    "command",
    [
        "run",
        "run-install",
        "add",
        "add-no-install",
        "sync",
        "sync-no-install",
        "install",
        "package",
        "package-unrelated",
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
async def test_commands_reject_invalid_graphs_before_mutation(
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
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(entrypoint, "module_manager", manager)
    application = importlib.import_module("alira.cli.application")

    async def start_application(manager, modules, *, install_missing=False):
        await manager.load(*modules, install_missing=install_missing)

    monkeypatch.setattr(application, "run_application", start_application)
    python_resolution = AsyncMock(
        side_effect=AssertionError("Python solver ran before preflight")
    )
    monkeypatch.setattr(
        "alira.kernel.module.dependencies.deployment.resolve_requirements",
        python_resolution,
    )
    if command in {"remove", "uninstall"}:
        records = {
            name: {
                "packages": {},
                "requirements": None,
                "requirements_hash": None,
                "required_modules": [],
                "version": "1.0",
                "required_kernel": ">=3.0.0",
            }
            for name in definitions
        }
        manager._dependencies.ledger.write(
            {"format": 1, "modules": records}, EMPTY_LOCK
        )
    arguments = {
        "run": ["run", "web"],
        "run-install": ["run", "web", "-i"],
        "add": ["add", "web"],
        "add-no-install": ["add", "web", "-n"],
        "sync": ["sync"],
        "sync-no-install": ["sync", "-n"],
        "remove": ["remove", "victim"],
        "uninstall": ["uninstall", "victim"],
    }.get(command)
    if command in {"install", "package", "package-unrelated"}:
        archive = tmp_path / "incoming.zip"
        prerequisites = [] if command == "package-unrelated" else ["web"]
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
            arguments = ["install", "-r", str(manifest)]
    before = manager._dependencies.ledger.snapshot()
    with pytest.raises(DependencyError, match=message):
        await entrypoint.run(arguments)
    assert manager._dependencies.ledger.snapshot() == before
    assert not marker.exists()
    assert not (root / "incoming").exists()
    assert (root / "victim/main.py").is_file()
    python_resolution.assert_not_awaited()


@pytest.mark.parametrize("command", ["remove", "uninstall"])
async def test_removing_broken_module_is_allowed_when_retained_graph_is_valid(
    tmp_path, monkeypatch, module_manager_factory, command
):
    manager = module_manager_factory(tmp_path)
    for name, text in [
        ("bad", "invalid syntax !!!"),
        ("good", "module=BaseModule('good','1')"),
    ]:
        folder = tmp_path / name
        folder.mkdir()
        (folder / "main.py").write_text(text)
    records = {
        name: {
            "packages": {},
            "requirements": None,
            "requirements_hash": None,
            "required_modules": [],
        }
        for name in ["bad", "good"]
    }
    manager._dependencies.ledger.write({"format": 1, "modules": records}, EMPTY_LOCK)
    monkeypatch.setattr(entrypoint, "module_manager", manager)
    monkeypatch.setattr(
        "send2trash.send2trash",
        lambda name: Path(name).rename(tmp_path / "trash"),
    )
    await entrypoint.run([command, "bad"])
    assert manager.deployments == {"good"}
    assert (tmp_path / "good/main.py").is_file()
    assert (tmp_path / "bad").exists() == (command == "remove")


@pytest.mark.parametrize("command", ["add", "remove", "uninstall", "sync"])
async def test_only_sync_rebuilds_damaged_deployment_records(
    tmp_path, monkeypatch, module_manager_factory, command
):
    manager = module_manager_factory(tmp_path)
    folder = tmp_path / "good"
    folder.mkdir()
    (folder / "main.py").write_text("module=BaseModule('good','1')")
    ledger = manager._dependencies.ledger
    ledger.path.parent.mkdir(parents=True)
    ledger.path.write_text("{interrupted write")
    before = ledger.snapshot()
    monkeypatch.setattr(entrypoint, "module_manager", manager)
    if command == "sync":
        await entrypoint.run(["sync", "-n"])
        assert manager.deployments == {"good"}
    else:
        with pytest.raises(DependencyError, match="sync"):
            await entrypoint.run([command, "good"])
        assert ledger.snapshot() == before
    assert (folder / "main.py").is_file()

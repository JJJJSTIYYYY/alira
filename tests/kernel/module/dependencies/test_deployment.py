"""Source-driven records, kernel protection, environment sync and recovery."""

import asyncio
import importlib
import json
import shutil
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zipfile import ZipFile

import pytest

from alira.kernel.module import BaseModule, ModuleManager
from alira.kernel.module.catalog import ModuleCatalog
from alira.kernel.module.dependencies import DependencyError, ModuleRequirements
from alira.kernel.module.dependencies.uv import EMPTY_LOCK

code = importlib.import_module("alira.kernel.module.dependencies.deployment")


@pytest.mark.parametrize("with_uv", [False, True])
def test_kernel_protection_includes_its_installed_dependency_tool(monkeypatch, with_uv):
    versions = {"alira-harness": "1.0", "host-runtime": "4.0", "plugin-extra": "2.0"}
    if with_uv:
        versions["uv"] = "0.9.7"
    monkeypatch.setattr(
        code,
        "distributions",
        lambda: [
            SimpleNamespace(
                metadata={"Name": name},
                version=version,
                requires=["host-runtime>=4"] if name == "alira-harness" else [],
            )
            for name, version in versions.items()
        ],
    )
    expected = {"alira-harness": "1.0", "host-runtime": "4.0"}
    if with_uv:
        expected["uv"] = "0.9.7"
    assert code.kernel_packages() == expected


def lock_bytes(packages):
    return (
        b'lock-version = "1.0"\ncreated-by = "test"\n'
        + b"".join(
            f'\n[[packages]]\nname = "{name}"\nversion = "{version}"\n'.encode()
            for name, version in sorted(packages.items())
        )
        if packages
        else EMPTY_LOCK
    )


@pytest.fixture
def deployment_environment(tmp_path, monkeypatch):
    core = {"alira-harness": "1.0", "host-runtime": "4.0"}
    installed = dict(core)
    plans, commands = {}, []
    root = tmp_path / "modules"
    root.mkdir()
    state = tmp_path / "state"
    monkeypatch.setattr(code, "kernel_packages", lambda: dict(core))
    monkeypatch.setattr(
        code.ModuleDependencies, "installed", staticmethod(lambda: dict(installed))
    )

    async def resolve(sources, pins, directory, cwd):
        union, result = {}, {}
        for name, path in sources.items():
            packages = plans[name] if path is not None else {}
            for package, version in packages.items():
                expected = pins.get(package, union.get(package, version))
                if expected != version:
                    raise DependencyError(
                        f"Cannot resolve protected/conflicting {package}"
                    )
                union[package] = version
            result[name] = ModuleRequirements(None, dict(packages))
        combined = directory / "combined"
        combined.mkdir()
        (combined / "pylock.toml").write_bytes(lock_bytes(union))
        return result

    async def uv(*arguments, cwd, capture=True):
        commands.append(arguments)
        if arguments[0] == "install":
            if "--requirements" in arguments:
                lock = Path(arguments[arguments.index("--requirements") + 1])
                installed.update(
                    {
                        item["name"]: item["version"]
                        for item in tomllib.loads(lock.read_text())["packages"]
                    }
                )
            else:
                installed.update(
                    argument.split("==")
                    for argument in arguments[1:]
                    if "==" in argument
                )
        else:
            assert arguments[0] == "uninstall"
            for name in arguments[1:]:
                installed.pop(name, None)

    monkeypatch.setattr(code, "resolve_requirements", resolve)
    monkeypatch.setattr(code, "run_uv", uv)

    def manager():
        instance = ModuleManager._new_for_tests(state_dir=state)
        instance.modules_dir = root
        return instance

    def register(instance, name, packages, *, required_modules=()):
        folder = root / name
        folder.mkdir(exist_ok=True)
        (folder / "src").mkdir(exist_ok=True)
        (folder / "main.py").write_text(
            "raise AssertionError('management executed the module')\n"
            "from alira.kernel.module import BaseModule\n"
            f"module = BaseModule({name!r}, '1.0', required_modules={list(required_modules)!r})\n"
        )
        (folder / "requirements.txt").write_text(
            "".join(f"{package}=={version}\n" for package, version in packages.items())
        )
        plans[name] = dict(packages)
        return folder

    return SimpleNamespace(
        core=core,
        installed=installed,
        plans=plans,
        commands=commands,
        root=root,
        state_dir=state,
        manager=manager,
        register=register,
    )


async def test_full_sync_rebuilds_from_sources_and_reconciles_environment(
    deployment_environment,
):
    env = deployment_environment
    manager = env.manager()
    env.register(manager, "a", {"shared": "1.0", "a-runtime": "1.0"})
    env.register(manager, "b", {"shared": "1.0"})
    env.installed["orphan"] = "9.0"
    await manager.sync()
    assert env.installed == env.core | {"shared": "1.0", "a-runtime": "1.0"}
    assert manager.dependencies["shared"].ref_count == 2
    env.register(manager, "a", {"a-runtime": "2.0"})
    shutil.rmtree(env.root / "b")
    await manager.sync()
    assert env.installed == env.core | {"a-runtime": "2.0"}
    assert manager.deployments == {"a"}


@pytest.mark.parametrize(
    "damage", ["json", "lock", "missing-json", "missing-lock", "both", "ownership"]
)
async def test_sync_ignores_damaged_or_missing_records(deployment_environment, damage):
    env = deployment_environment
    manager = env.manager()
    env.register(manager, "web", {"web-runtime": "1.0"})
    await manager.sync()
    ledger = manager._dependencies.ledger
    if damage in {"json", "both"}:
        ledger.path.write_text("{broken")
    if damage in {"lock", "both"}:
        ledger.lockfile.write_text("not a lock")
    if damage == "missing-json":
        ledger.path.unlink()
    if damage == "missing-lock":
        ledger.lockfile.unlink()
    if damage == "ownership":
        state = json.loads(ledger.path.read_text())
        state["managed"] = list(env.core)
        state["modules"]["web"]["packages"] = {"host-runtime": "4.0"}
        ledger.path.write_text(json.dumps(state))
    env.register(manager, "web", {"web-runtime": "2.0"})
    restarted = env.manager()
    await restarted.sync()
    assert env.installed == env.core | {"web-runtime": "2.0"}
    assert restarted.deployments == {"web"}
    assert "managed" not in restarted._dependencies.ledger.read()


@pytest.mark.parametrize("operation", ["sync", "add"])
async def test_no_install_resolves_and_writes_both_files_without_environment_changes(
    deployment_environment, operation
):
    env = deployment_environment
    manager = env.manager()
    env.register(manager, "web", {"web-runtime": "1.0"})
    env.installed["orphan"] = "1.0"
    before = dict(env.installed)
    if operation == "sync":
        await manager.sync(no_install=True)
    else:
        await manager.add("web", no_install=True)
    assert env.installed == before
    assert env.commands == []
    assert manager.deployments == {"web"}
    assert manager._dependencies.ledger.lockfile.is_file()
    assert manager._dependencies.ledger.read()["modules"]["web"]["packages"] == {
        "web-runtime": "1.0"
    }


async def test_add_is_targeted_and_does_not_scan_unmanaged_entries(
    deployment_environment, monkeypatch
):
    env = deployment_environment
    manager = env.manager()
    env.register(manager, "base", {"shared": "1.0"})
    env.register(manager, "child", {"child-runtime": "1.0"}, required_modules=["base"])
    bad = env.root / "unrelated"
    bad.mkdir()
    (bad / "main.py").write_text("invalid syntax !!!")
    monkeypatch.setattr(
        ModuleCatalog, "names", lambda self: pytest.fail("add scanned all modules")
    )
    await manager.add("child")
    assert manager.deployments == {"base", "child"}
    assert env.installed == env.core | {"shared": "1.0", "child-runtime": "1.0"}


async def test_add_missing_prerequisite_fails_before_writes(deployment_environment):
    env = deployment_environment
    manager = env.manager()
    env.register(
        manager, "child", {"child-runtime": "1.0"}, required_modules=["missing"]
    )
    with pytest.raises(DependencyError, match="Module not found: missing"):
        await manager.add("child")
    assert not manager._dependencies.ledger.path.exists()
    assert env.commands == []


async def test_remove_preserves_shared_and_kernel_packages_and_sync_readds_sources(
    deployment_environment,
):
    env = deployment_environment
    manager = env.manager()
    env.register(manager, "left", {"shared": "1.0", "host-runtime": "4.0"})
    env.register(manager, "right", {"shared": "1.0"})
    await manager.sync()
    await manager.remove("left")
    assert env.installed == env.core | {"shared": "1.0"}
    assert (env.root / "left/main.py").exists()
    await manager.remove("right")
    assert env.installed == env.core
    assert not manager.deployments
    await manager.sync()
    assert manager.deployments == {"left", "right"}
    assert env.installed == env.core | {"shared": "1.0"}


async def test_remove_unknown_and_required_modules_fail_explicitly(
    deployment_environment,
):
    env = deployment_environment
    manager = env.manager()
    env.register(manager, "base", {})
    env.register(manager, "child", {}, required_modules=["base"])
    with pytest.raises(DependencyError, match="not in dependency management"):
        await manager.remove("base")
    await manager.sync()
    with pytest.raises(DependencyError, match="required by: child"):
        await manager.remove("base")
    await manager.remove("base", "child")
    assert env.installed == env.core


async def test_kernel_versions_are_protected_from_plugin_requirements(
    deployment_environment,
):
    env = deployment_environment
    manager = env.manager()
    env.register(manager, "bad", {"host-runtime": "9.0"})
    with pytest.raises(DependencyError, match="protected"):
        await manager.sync()
    assert env.installed == env.core
    assert not manager._dependencies.ledger.path.exists()


async def test_cancelled_install_leaves_rebuildable_records(
    deployment_environment, monkeypatch
):
    env = deployment_environment
    manager = env.manager()
    env.register(manager, "web", {"web-runtime": "1.0", "shared": "1.0"})
    entered = asyncio.Event()
    original = code.run_uv

    async def partial(*args, **kwargs):
        env.installed["shared"] = "1.0"
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr(code, "run_uv", partial)
    task = asyncio.create_task(manager.sync())
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert manager.deployments == {"web"}
    monkeypatch.setattr(code, "run_uv", original)
    await env.manager().sync()
    assert env.installed == env.core | env.plans["web"]


async def test_runtime_checks_records_without_environment_mutation(
    deployment_environment, monkeypatch
):
    env = deployment_environment
    manager = env.manager()
    env.register(manager, "web", {"web-runtime": "1.0"})
    await manager.sync()
    manager.register(BaseModule("web", "1.0"))
    snapshot = manager._dependencies.ledger.snapshot()
    monkeypatch.setattr(
        code,
        "run_uv",
        AsyncMock(side_effect=AssertionError("runtime installed packages")),
    )
    await manager.load("web")
    await manager.stop()
    assert manager._dependencies.ledger.snapshot() == snapshot
    env.installed.pop("web-runtime")
    with pytest.raises(DependencyError, match="missing dependency"):
        await manager.load("web")


async def test_default_records_stay_in_home(
    deployment_environment, tmp_path, monkeypatch
):
    env = deployment_environment
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    manager = ModuleManager._new_for_tests()
    manager.modules_dir = env.root
    env.register(manager, "web", {})
    await manager.sync(no_install=True)
    monkeypatch.chdir(tmp_path)
    restarted = ModuleManager._new_for_tests()
    assert restarted._dependencies.ledger.path == home / ".alira/modules.json"
    assert restarted.deployments == {"web"}


async def test_uninstall_uses_trash_and_preserves_other_sources(
    deployment_environment, tmp_path, monkeypatch
):
    env = deployment_environment
    manager = env.manager()
    folder = env.register(manager, "web", {"web-runtime": "1.0"})
    peer = env.register(manager, "peer", {})
    await manager.sync()
    trash = tmp_path / "system-trash"
    trash.mkdir()
    calls = []

    def send(path):
        calls.append(path)
        shutil.move(path, trash / Path(path).name)

    monkeypatch.setattr("send2trash.send2trash", send)
    await manager.uninstall("web")
    assert calls == [str(folder)]
    assert not folder.exists()
    assert (trash / "web/main.py").exists()
    assert (peer / "main.py").exists()
    assert env.installed == env.core
    assert manager.deployments == {"peer"}


async def test_trash_failure_rolls_back_dependencies_and_never_deletes_sources(
    deployment_environment, monkeypatch
):
    env = deployment_environment
    manager = env.manager()
    folder = env.register(manager, "web", {"web-runtime": "1.0"})
    await manager.sync()
    snapshot = manager._dependencies.ledger.snapshot()

    def fail(path):
        raise OSError("trash unavailable")

    monkeypatch.setattr("send2trash.send2trash", fail)
    with pytest.raises(OSError, match="trash unavailable"):
        await manager.uninstall("web")
    assert (folder / "main.py").exists()
    assert manager._dependencies.ledger.snapshot() == snapshot
    assert env.installed == env.core | {"web-runtime": "1.0"}


async def test_install_missing_dependency_rolls_back_sources_and_records(
    deployment_environment, tmp_path
):
    env = deployment_environment
    manager = env.manager()
    archive = tmp_path / "plugin.zip"
    with ZipFile(archive, "w") as package:
        package.writestr(
            "main.py",
            "raise RuntimeError('must not execute')\nfrom alira.kernel.module import BaseModule\nmodule=BaseModule('plugin','1',required_modules=['missing'])\n",
        )
        package.writestr("src/__init__.py", "")
    with pytest.raises(DependencyError, match="Module not found: missing"):
        await manager.install(str(archive))
    assert not (env.root / "plugin").exists()
    assert not manager._dependencies.ledger.path.exists()
    assert env.installed == env.core


async def test_install_partial_dependency_failure_restores_records_sources_and_environment(
    deployment_environment, tmp_path, monkeypatch
):
    env = deployment_environment
    manager = env.manager()
    env.register(manager, "existing", {"shared": "1.0"})
    await manager.add("existing")
    snapshot = manager._dependencies.ledger.snapshot()
    env.plans["plugin"] = {"plugin-runtime": "1.0"}
    archive = tmp_path / "plugin.zip"
    with ZipFile(archive, "w") as package:
        package.writestr(
            "main.py",
            "raise RuntimeError('must not execute')\nmodule=BaseModule('plugin','1')\n",
        )
        package.writestr("src/__init__.py", "")
        package.writestr("requirements.txt", "plugin-runtime==1.0\n")
    original = code.run_uv

    async def partial(*args, **kwargs):
        if args[0] == "install" and "--requirements" in args:
            env.installed["plugin-runtime"] = "1.0"
            raise DependencyError("install failed after writing a package")
        await original(*args, **kwargs)

    monkeypatch.setattr(code, "run_uv", partial)
    with pytest.raises(DependencyError, match="install failed"):
        await manager.install(str(archive))
    assert not (env.root / "plugin").exists()
    assert (env.root / "existing/main.py").exists()
    assert manager._dependencies.ledger.snapshot() == snapshot
    assert env.installed == env.core | {"shared": "1.0"}


async def test_partial_trash_failure_restores_previously_trashed_sources(
    deployment_environment, tmp_path, monkeypatch
):
    env = deployment_environment
    manager = env.manager()
    env.register(manager, "a", {"a-runtime": "1.0"})
    env.register(manager, "b", {"b-runtime": "1.0"})
    await manager.sync()
    snapshot = manager._dependencies.ledger.snapshot()
    trash = tmp_path / "system-trash"
    trash.mkdir()

    def partial(path):
        if Path(path).name == "b":
            raise OSError("trash failed on second module")
        shutil.move(path, trash / Path(path).name)

    monkeypatch.setattr("send2trash.send2trash", partial)
    with pytest.raises(OSError, match="second module"):
        await manager.uninstall("a", "b")
    assert (env.root / "a/main.py").exists()
    assert (env.root / "b/main.py").exists()
    assert manager._dependencies.ledger.snapshot() == snapshot
    assert env.installed == env.core | {"a-runtime": "1.0", "b-runtime": "1.0"}


async def test_sync_recovers_pair_write_interruption(
    deployment_environment, monkeypatch
):
    env = deployment_environment
    manager = env.manager()
    env.register(manager, "web", {"web-runtime": "1.0"})
    await manager.sync()
    env.register(manager, "web", {"web-runtime": "2.0"})
    ledger = manager._dependencies.ledger
    write = ledger.atomic_write

    def interrupted(path, content):
        if path == ledger.path:
            raise OSError("JSON write interrupted")
        write(path, content)

    monkeypatch.setattr(ledger, "atomic_write", interrupted)
    with pytest.raises(OSError, match="interrupted"):
        await manager.sync()
    with pytest.raises(DependencyError, match="need to be synchronized"):
        ledger.read()
    await env.manager().sync()
    assert env.installed == env.core | {"web-runtime": "2.0"}


async def test_kernel_packages_are_excluded_from_install_artifacts(
    deployment_environment, monkeypatch
):
    env = deployment_environment
    manager = env.manager()
    env.register(manager, "web", {"host-runtime": "4.0", "web-runtime": "1.0"})
    original = code.run_uv

    async def checked(*args, **kwargs):
        if args[0] == "install":
            lock = Path(args[args.index("--requirements") + 1])
            names = {
                record["name"] for record in tomllib.loads(lock.read_text())["packages"]
            }
            assert not names.intersection(env.core)
        await original(*args, **kwargs)

    monkeypatch.setattr(code, "run_uv", checked)
    await manager.sync()
    assert env.installed == env.core | {"web-runtime": "1.0"}


async def test_incomplete_source_restoration_keeps_original_failure_and_restores_records(
    deployment_environment, tmp_path, monkeypatch
):
    env = deployment_environment
    manager = env.manager()
    env.register(manager, "a", {"a-runtime": "1.0"})
    env.register(manager, "b", {"b-runtime": "1.0"})
    await manager.sync()
    snapshot = manager._dependencies.ledger.snapshot()
    trash = tmp_path / "system-trash"
    trash.mkdir()
    copy = shutil.copytree

    def partial_trash(path):
        if Path(path).name == "b":
            raise OSError("original trash error")
        shutil.move(path, trash / Path(path).name)

    def failed_restore(source, destination, *args, **kwargs):
        if Path(destination).parent == env.root:
            raise OSError("source restore error")
        return copy(source, destination, *args, **kwargs)

    monkeypatch.setattr("send2trash.send2trash", partial_trash)
    monkeypatch.setattr(shutil, "copytree", failed_restore)
    with pytest.raises(BaseExceptionGroup) as caught:
        await manager.uninstall("a", "b")
    messages = [str(exc) for exc in caught.value.exceptions]
    assert messages == ["original trash error", "source restore error"]
    assert manager._dependencies.ledger.snapshot() == snapshot
    assert env.installed == env.core | {"a-runtime": "1.0", "b-runtime": "1.0"}
    assert (trash / "a/main.py").exists()
    assert (env.root / "b/main.py").exists()


async def test_failed_version_restoration_still_removes_new_packages_and_restores_records(
    deployment_environment, monkeypatch
):
    env = deployment_environment
    manager = env.manager()
    env.register(manager, "existing", {"shared": "1.0"})
    await manager.add("existing")
    snapshot = manager._dependencies.ledger.snapshot()
    original = code.run_uv

    async def fail_restore(*args, **kwargs):
        if args[0] == "install":
            raise DependencyError("old artifact unavailable")
        await original(*args, **kwargs)

    monkeypatch.setattr(code, "run_uv", fail_restore)
    with pytest.raises(BaseExceptionGroup) as caught:
        async with manager._dependencies.transaction(env.root):
            env.installed.update({"shared": "2.0", "new-package": "1.0"})
            raise DependencyError("operation failed")
    assert [str(exc) for exc in caught.value.exceptions] == [
        "operation failed",
        "old artifact unavailable",
    ]
    assert env.installed == env.core | {"shared": "2.0"}
    assert manager._dependencies.ledger.snapshot() == snapshot

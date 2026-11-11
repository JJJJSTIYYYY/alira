"""Exercise the real uv resolver and installer against offline local wheels."""

import asyncio
import importlib
import os
import shutil
import subprocess
import sys
from importlib.metadata import distributions
from zipfile import ZipFile

import pytest

from alira.kernel.module import BaseModule
from alira.kernel.module.dependencies import DependencyError, run_uv

deployment_module = importlib.import_module(
    "alira.kernel.module.dependencies.deployment"
)


def wheel(directory, name, version, dependencies=(), extras=()):
    normalized = name.replace("-", "_")
    info = f"{normalized}-{version}.dist-info"
    path = directory / f"{normalized}-{version}-py3-none-any.whl"
    metadata = f"Metadata-Version: 2.3\nName: {name}\nVersion: {version}\n"
    metadata += "".join(f"Provides-Extra: {extra}\n" for extra in extras)
    metadata += "".join(f"Requires-Dist: {dependency}\n" for dependency in dependencies)
    with ZipFile(path, "w") as archive:
        archive.writestr(f"{normalized}/__init__.py", "")
        archive.writestr(f"{info}/METADATA", metadata)
        archive.writestr(
            f"{info}/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(
            f"{info}/RECORD",
            "".join(
                f"{entry},,\n" for entry in [*archive.namelist(), f"{info}/RECORD"]
            ),
        )
    return path


class Service(BaseModule):
    def __init__(self, name):
        super().__init__(name, "1.0")
        self.running = False

    async def startup(self):
        self.running = True

    async def shutdown(self):
        self.running = False


@pytest.fixture
def environment(tmp_path, monkeypatch):
    uv = shutil.which("uv")
    assert uv is not None, "uv is required for module dependency integration tests"
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("UV_OFFLINE", "1")
    virtualenv = tmp_path / "environment"
    subprocess.run(
        [uv, "venv", "--python", sys.executable, str(virtualenv)],
        check=True,
        capture_output=True,
    )
    interpreter = virtualenv / "bin" / "python"
    site = next((virtualenv / "lib").glob("python*/site-packages"))
    scripts = tmp_path / "bin"
    scripts.mkdir()
    executable = scripts / "uv"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "arguments = sys.argv[1:]\n"
        f"arguments[arguments.index('--python') + 1] = {str(interpreter)!r}\n"
        f"os.execv({uv!r}, [{uv!r}, *arguments])\n"
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(scripts) + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(
        deployment_module, "distributions", lambda: distributions(path=[str(site)])
    )
    monkeypatch.setattr(deployment_module, "kernel_packages", dict)
    packages = tmp_path / "wheels"
    packages.mkdir()
    wheel(packages, "shared-pkg", "1.0")
    wheel(packages, "shared-pkg", "2.0")
    wheel(packages, "left-pkg", "1.0", ["shared-pkg>=1,<3"])
    wheel(packages, "right-pkg", "1.0", ["shared-pkg<2"])
    wheel(packages, "leaf-pkg", "1.0")
    wheel(packages, "extra-pkg", "1.0", ['leaf-pkg; extra == "feature"'], ["feature"])
    return tmp_path, packages, site


def add_module(manager, wheelhouse, name, requirements):
    directory = manager.modules_dir / name
    directory.mkdir()
    (directory / "requirements.txt").write_text(
        f"--no-index\n--find-links {wheelhouse}\n" + requirements + "\n"
    )
    (directory / "src").mkdir()
    (directory / "main.py").write_text(
        "from alira.kernel.module import BaseModule\n"
        f"module = BaseModule({name!r}, '1.0')\n"
    )
    module = Service(name)
    manager.register(module)
    return module


def installed(site):
    return {
        distribution.metadata["Name"]: distribution.version
        for distribution in distributions(path=[str(site)])
    }


async def test_shared_transitive_packages_resolve_once_and_uninstall_at_last_reference(
    environment, module_manager_factory
):
    root, packages, site = environment
    manager = module_manager_factory(root)
    left = add_module(manager, packages, "left", "Left_Pkg>=1\nleft-pkg>=1")
    right = add_module(manager, packages, "right", "right-pkg>=1")
    await manager.sync()
    await manager.load()
    assert left.running and right.running
    # A separate solve of left would choose shared 2.0; joint solving must find 1.0.
    assert installed(site) == {
        "left-pkg": "1.0",
        "right-pkg": "1.0",
        "shared-pkg": "1.0",
    }
    assert manager.dependencies["shared-pkg"].ref_count == 2
    await manager.unload("left")
    assert installed(site) == {
        "left-pkg": "1.0",
        "right-pkg": "1.0",
        "shared-pkg": "1.0",
    }
    await manager.stop()
    await manager.remove("left")
    assert installed(site) == {"right-pkg": "1.0", "shared-pkg": "1.0"}
    assert manager.dependencies["shared-pkg"].modules == {"right"}
    await manager.stop()
    await manager.remove(*tuple(manager._dependencies.claims))
    assert installed(site) == {}
    assert not manager.dependencies


async def test_conflicting_versions_fail_before_environment_or_startup_changes(
    environment, module_manager_factory
):
    root, packages, site = environment
    manager = module_manager_factory(root)
    first = add_module(manager, packages, "a", "shared-pkg==1.0")
    second = add_module(manager, packages, "b", "shared-pkg==2.0")
    with pytest.raises(DependencyError, match="No solution found"):
        await manager.sync()
    assert not first.running and not second.running
    assert not manager.dependencies
    assert installed(site) == {}


async def test_includes_constraints_extras_markers_and_reference_counts(
    environment, module_manager_factory
):
    root, packages, site = environment
    manager = module_manager_factory(root)
    add_module(
        manager,
        packages,
        "extra",
        '-r included.txt\n-c constraints.txt\nmissing-pkg; python_version < "2"',
    )
    (root / "extra" / "included.txt").write_text("extra-pkg[feature]\n")
    (root / "extra" / "constraints.txt").write_text("extra-pkg==1.0\n")
    add_module(manager, packages, "leaf", "leaf-pkg")
    await manager.sync()
    await manager.load()
    assert installed(site) == {"extra-pkg": "1.0", "leaf-pkg": "1.0"}
    assert manager.dependencies["leaf-pkg"].ref_count == 2
    await manager.unload("extra")
    await manager.stop()
    await manager.remove("extra")
    assert installed(site) == {"leaf-pkg": "1.0"}
    await manager.stop()
    await manager.remove(*tuple(manager._dependencies.claims))
    assert installed(site) == {}


async def test_kernel_packages_are_pinned_and_never_removed(
    environment, module_manager_factory, monkeypatch
):
    root, packages, site = environment
    await run_uv(
        "install",
        "--no-index",
        "--find-links",
        str(packages),
        "shared-pkg==1.0",
        cwd=root,
    )
    monkeypatch.setattr(
        deployment_module, "kernel_packages", lambda: {"shared-pkg": "1.0"}
    )
    manager = module_manager_factory(root)
    add_module(manager, packages, "a", "left-pkg")
    await manager.sync()
    await manager.load()
    assert manager.dependencies["shared-pkg"].preinstalled
    add_module(manager, packages, "incompatible", "shared-pkg==2.0")
    await manager.stop()
    with pytest.raises(DependencyError, match="No solution found"):
        await manager.add("incompatible")
    await manager.remove("a")
    assert installed(site) == {"shared-pkg": "1.0"}


async def test_running_module_versions_cannot_be_changed_by_later_load(
    environment, module_manager_factory
):
    root, packages, site = environment
    manager = module_manager_factory(root)
    add_module(manager, packages, "left", "left-pkg")
    await manager.add("left")
    await manager.load("left")
    assert installed(site)["shared-pkg"] == "2.0"
    add_module(manager, packages, "right", "right-pkg")
    with pytest.raises(DependencyError, match="Stop running modules"):
        await manager.load("right", install_missing=True)
    assert manager.dependencies["shared-pkg"].modules == {"left"}
    assert installed(site)["shared-pkg"] == "2.0"
    await manager.stop()


async def test_uv_cancellation_reaps_process(tmp_path, monkeypatch):
    executable = tmp_path / "uv"
    pid_file = tmp_path / "pid"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import os, time\nfrom pathlib import Path\n"
        f"Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    task = asyncio.create_task(run_uv("compile", cwd=tmp_path))

    async def wait_started():
        while not pid_file.is_file():
            await asyncio.sleep(0.01)

    try:
        await asyncio.wait_for(wait_started(), 3)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


async def test_failed_startup_preserves_deployment_for_retry_or_explicit_removal(
    environment, module_manager_factory
):
    root, packages, site = environment
    manager = module_manager_factory(root)
    module = add_module(manager, packages, "broken", "left-pkg")

    async def fail():
        raise RuntimeError("business startup failed")

    module.startup = fail
    await manager.sync()
    with pytest.raises(ExceptionGroup) as caught:
        await manager.load()
    assert "business startup failed" in str(caught.value.exceptions[0])
    assert installed(site) == {"left-pkg": "1.0", "shared-pkg": "2.0"}
    await manager.remove("broken")
    assert installed(site) == {}
    assert not manager.dependencies
    assert not manager.loaded


async def test_discovery_and_unselected_modules_never_install_packages(
    environment, module_manager_factory
):
    root, packages, site = environment
    manager = module_manager_factory(root)
    selected = add_module(manager, packages, "selected", "left-pkg")
    inactive = add_module(manager, packages, "inactive", "unavailable-pkg==99")
    inactive.required_modules = ["missing"]
    assert set(manager.list()) == {"selected", "inactive"}
    assert installed(site) == {}
    assert not selected.running and not inactive.running
    await manager.add("selected")
    await manager.load("selected")
    assert selected.running and not inactive.running
    assert installed(site) == {"left-pkg": "1.0", "shared-pkg": "2.0"}
    await manager.stop()
    await manager.remove(*tuple(manager._dependencies.claims))
    assert installed(site) == {}


async def test_real_resolver_upgrades_managed_packages_after_restart(
    environment, module_manager_factory
):
    root, packages, site = environment
    first = module_manager_factory(root)
    add_module(first, packages, "a", "shared-pkg==1.0")
    await first.add("a")
    assert installed(site) == {"shared-pkg": "1.0"}
    state_dir = first._dependencies.ledger.directory

    from alira.kernel.module import ModuleManager

    second = ModuleManager._new_for_tests(state_dir=state_dir)
    second.modules_dir = root
    second.register(Service("a"))
    assert second.deployments == {"a"}
    (root / "a/requirements.txt").write_text(
        f"--no-index\n--find-links {packages}\nshared-pkg>=2.0\n"
    )
    await second.add("a")
    assert installed(site) == {"shared-pkg": "2.0"}
    await second.load("a")
    assert second.loaded == {"a"}
    await second.stop()
    await second.remove("a")
    assert installed(site) == {}


async def test_visible_uv_progress_is_available_before_command_finishes(
    tmp_path, monkeypatch, capfd
):
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    executable = tmp_path / "uv"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import sys, time\nfrom pathlib import Path\n"
        "print('installation progress', file=sys.stderr, flush=True)\n"
        f"Path({str(ready)!r}).touch()\n"
        f"while not Path({str(release)!r}).exists(): time.sleep(0.01)\n"
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    task = asyncio.create_task(run_uv("install", cwd=tmp_path, capture=False))
    try:
        async with asyncio.timeout(3):
            while not ready.exists():
                await asyncio.sleep(0.01)
        assert not task.done()
        assert "installation progress" in capfd.readouterr().err
    finally:
        release.touch()
        await asyncio.wait_for(task, 3)


async def test_per_module_resolution_failure_preserves_uv_error_details(
    tmp_path, monkeypatch
):
    from alira.kernel.module.dependencies import (
        ModuleRequirements,
        resolve_requirements,
    )

    uv_code = importlib.import_module("alira.kernel.module.dependencies.uv")
    work = tmp_path / "work"
    work.mkdir()
    left = tmp_path / "left.txt"
    right = tmp_path / "right.txt"
    left.touch()
    right.touch()

    async def compile(files, constraints, directory, cwd):
        if directory.name == "combined":
            return ModuleRequirements(None, {})
        raise DependencyError(f"detailed uv error for {files[0].name}")

    monkeypatch.setattr(uv_code, "_compile", compile)
    with pytest.raises(DependencyError, match="detailed uv error"):
        await resolve_requirements({"left": left, "right": right}, {}, work, tmp_path)

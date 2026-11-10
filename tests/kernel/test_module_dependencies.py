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

manager_module = importlib.import_module("alira.kernel.module.module_manager")


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
        archive.writestr(f"{info}/WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        archive.writestr(
            f"{info}/RECORD",
            "".join(f"{entry},,\n" for entry in [*archive.namelist(), f"{info}/RECORD"]),
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
        check=True, capture_output=True,
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
    monkeypatch.setattr(manager_module, "distributions", lambda: distributions(path=[str(site)]))
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
    module = Service(name)
    manager.register(module)
    return module


def installed(site):
    return {distribution.metadata["Name"]: distribution.version for distribution in distributions(path=[str(site)])}


async def test_shared_transitive_packages_resolve_once_and_uninstall_at_last_reference(environment, module_manager_factory):
    root, packages, site = environment
    manager = module_manager_factory(root)
    left = add_module(manager, packages, "left", "Left_Pkg>=1\nleft-pkg>=1")
    right = add_module(manager, packages, "right", "right-pkg>=1")
    await manager.load()
    assert left.running and right.running
    # A separate solve of left would choose shared 2.0; joint solving must find 1.0.
    assert installed(site) == {"left-pkg": "1.0", "right-pkg": "1.0", "shared-pkg": "1.0"}
    assert manager.dependencies["shared-pkg"].ref_count == 2
    await manager.unload("left")
    assert installed(site) == {"right-pkg": "1.0", "shared-pkg": "1.0"}
    assert manager.dependencies["shared-pkg"].modules == {"right"}
    await manager.stop()
    assert installed(site) == {}
    assert not manager.dependencies


async def test_conflicting_versions_fail_before_environment_or_startup_changes(environment, module_manager_factory):
    root, packages, site = environment
    manager = module_manager_factory(root)
    first = add_module(manager, packages, "a", "shared-pkg==1.0")
    second = add_module(manager, packages, "b", "shared-pkg==2.0")
    with pytest.raises(DependencyError, match="No solution found"):
        await manager.load()
    assert not first.running and not second.running
    assert not manager.dependencies
    assert installed(site) == {}


async def test_includes_constraints_extras_markers_and_reference_counts(environment, module_manager_factory):
    root, packages, site = environment
    manager = module_manager_factory(root)
    add_module(manager, packages, "extra", '-r included.txt\n-c constraints.txt\nmissing-pkg; python_version < "2"')
    (root / "extra" / "included.txt").write_text("extra-pkg[feature]\n")
    (root / "extra" / "constraints.txt").write_text("extra-pkg==1.0\n")
    add_module(manager, packages, "leaf", "leaf-pkg")
    await manager.load()
    assert installed(site) == {"extra-pkg": "1.0", "leaf-pkg": "1.0"}
    assert manager.dependencies["leaf-pkg"].ref_count == 2
    await manager.unload("extra")
    assert installed(site) == {"leaf-pkg": "1.0"}
    await manager.stop()
    assert installed(site) == {}


async def test_preinstalled_packages_are_pinned_and_never_removed(environment, module_manager_factory):
    root, packages, site = environment
    await run_uv("install", "--no-index", "--find-links", str(packages), "shared-pkg==1.0", cwd=root)
    manager = module_manager_factory(root)
    add_module(manager, packages, "a", "left-pkg")
    await manager.load()
    assert manager.dependencies["shared-pkg"].preinstalled
    add_module(manager, packages, "incompatible", "shared-pkg==2.0")
    with pytest.raises(DependencyError, match="No solution found"):
        await manager.load("incompatible")
    assert manager.loaded == {"a"}
    await manager.stop()
    assert installed(site) == {"shared-pkg": "1.0"}


async def test_running_module_versions_cannot_be_changed_by_later_load(environment, module_manager_factory):
    root, packages, site = environment
    manager = module_manager_factory(root)
    add_module(manager, packages, "left", "left-pkg")
    await manager.load("left")
    assert installed(site)["shared-pkg"] == "2.0"
    add_module(manager, packages, "right", "right-pkg")
    with pytest.raises(DependencyError, match="No solution found"):
        await manager.load("right")
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


async def test_failed_startup_removes_newly_installed_packages(environment, module_manager_factory):
    root, packages, site = environment
    manager = module_manager_factory(root)
    module = add_module(manager, packages, "broken", "left-pkg")

    async def fail():
        raise RuntimeError("business startup failed")

    module.startup = fail
    with pytest.raises(ExceptionGroup) as caught:
        await manager.load()
    assert "business startup failed" in str(caught.value.exceptions[0])
    assert installed(site) == {}
    assert not manager.dependencies
    assert not manager.loaded


async def test_discovery_and_unselected_modules_never_install_packages(environment, module_manager_factory):
    root, packages, site = environment
    manager = module_manager_factory(root)
    selected = add_module(manager, packages, "selected", "left-pkg")
    inactive = add_module(manager, packages, "inactive", "unavailable-pkg==99")
    inactive.required_modules = ["missing"]
    await manager.start()
    assert installed(site) == {}
    assert not selected.running and not inactive.running
    await manager.load("selected")
    assert selected.running and not inactive.running
    assert installed(site) == {"left-pkg": "1.0", "shared-pkg": "2.0"}
    await manager.stop()
    assert installed(site) == {}

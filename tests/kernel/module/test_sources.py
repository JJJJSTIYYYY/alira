"""Module sources are installed separately from their Python dependencies."""

import asyncio
import importlib
import io
import stat
import sys
from threading import Event
from urllib.error import URLError
from uuid import uuid4
from zipfile import ZipFile, ZipInfo

import pytest

from alira.kernel.module.dependencies import DependencyError, ModuleRequirements
from alira.kernel.module.dependencies.uv import EMPTY_LOCK
from alira.kernel.module.sources import prepare_source

sources = importlib.import_module("alira.kernel.module.sources")
deployment_code = importlib.import_module("alira.kernel.module.dependencies.deployment")


def module_zip(path, *, name="plugin", wrapper="", entry=None):
    if entry is None:
        entry = (
            "from alira.kernel.module import BaseModule\n"
            "class Plugin(BaseModule):\n"
            "    async def startup(self):\n"
            "        from .src.service import value\n"
            "        self.value = value\n"
            f"module = Plugin({name!r}, '1.0')\n"
        )
    prefix = wrapper + "/" if wrapper else ""
    with ZipFile(path, "w") as archive:
        archive.writestr(prefix + "main.py", entry)
        archive.writestr(prefix + "requirements.txt", "missing-package==999\n")
        archive.writestr(prefix + "src/__init__.py", "")
        archive.writestr(prefix + "src/service.py", "value = 42\n")
    return path


@pytest.fixture
def source_manager(tmp_path, monkeypatch, module_manager_factory):
    package = f"modules_{uuid4().hex}"
    directory = tmp_path / package
    directory.mkdir()
    (directory / "__init__.py").touch()
    monkeypatch.syspath_prepend(str(tmp_path))
    manager = module_manager_factory(directory, package=package)
    monkeypatch.setattr(deployment_code, "kernel_packages", dict)
    monkeypatch.setattr(manager._dependencies, "installed", dict)

    async def resolve(sources, pins, directory, cwd):
        combined = directory / "combined"
        combined.mkdir()
        (combined / "pylock.toml").write_bytes(EMPTY_LOCK)
        return {name: ModuleRequirements(None, {}) for name in sources}

    monkeypatch.setattr(deployment_code, "resolve_requirements", resolve)
    return manager


@pytest.mark.parametrize("wrapper", ["", "repository-commit"])
async def test_install_reuses_add_without_importing_or_initializing_entries(
    source_manager,
    tmp_path,
    monkeypatch,
    wrapper,
):
    from unittest.mock import AsyncMock

    archive = module_zip(tmp_path / "plugin.zip", wrapper=wrapper)
    add = AsyncMock(wraps=source_manager.add)
    monkeypatch.setattr(source_manager, "add", add)
    await source_manager.install(str(archive))
    add.assert_awaited_once_with("plugin", background=False)
    target = source_manager.modules_dir / "plugin"
    assert (target / "src/service.py").is_file()
    assert source_manager.list() == ["plugin"]
    assert source_manager.deployments == {"plugin"}
    assert not source_manager.modules
    assert not source_manager.loaded
    assert not any(
        name.startswith(source_manager.package + ".plugin") for name in sys.modules
    )
    before = {
        path.relative_to(target): path.read_bytes()
        for path in target.rglob("*")
        if path.is_file()
    }
    await source_manager.remove("plugin")
    assert {
        path.relative_to(target): path.read_bytes()
        for path in target.rglob("*")
        if path.is_file()
    } == before
    assert not source_manager.deployments


async def test_add_preserves_existing_sources(source_manager, tmp_path):
    archive = module_zip(tmp_path / "plugin.zip")
    existing = source_manager.modules_dir / "plugin"
    existing.mkdir()
    sentinel = existing / "config.yaml"
    sentinel.write_text("local settings")
    with pytest.raises(DependencyError, match="already exists"):
        await source_manager.install(str(archive))
    assert sentinel.read_text() == "local settings"
    assert not (existing / "main.py").exists()
    assert not source_manager._dependencies.ledger.path.exists()


@pytest.mark.parametrize("wrapper", ["", "repository-commit"])
async def test_install_resolves_explicit_requirements_after_source_move(
    source_manager, tmp_path, monkeypatch, wrapper
):
    archive = tmp_path / "plugin.zip"
    prefix = wrapper + "/" if wrapper else ""
    with ZipFile(archive, "w") as package:
        package.writestr(
            prefix + "main.py",
            "module=BaseModule('plugin', '1', requirements_txt='plugin/deps.txt')\n",
        )
        package.writestr(prefix + "src/__init__.py", "")
        package.writestr(prefix + "deps.txt", "package==1\n")
    original = deployment_code.resolve_requirements

    async def resolve(paths, *args):
        assert paths == {"plugin": source_manager.modules_dir / "plugin/deps.txt"}
        return await original(paths, *args)

    monkeypatch.setattr(deployment_code, "resolve_requirements", resolve)
    await source_manager.install(str(archive))
    assert source_manager.deployments == {"plugin"}


@pytest.mark.parametrize(
    "entry",
    [
        "invalid syntax !!!\n",
        "from alira.kernel.module import BaseModule\nmodule=BaseModule(get_name(), '1')\n",
    ],
)
async def test_invalid_declarations_leave_no_installed_module(
    source_manager, tmp_path, entry
):
    archive = module_zip(tmp_path / "plugin.zip", entry=entry)
    with pytest.raises(DependencyError, match="declaration|literal"):
        await source_manager.install(str(archive))
    assert not (source_manager.modules_dir / "plugin").exists()
    assert not source_manager._dependencies.ledger.path.exists()
    assert not list(source_manager.modules_dir.glob(".alira-add-*"))


@pytest.mark.parametrize("name", ["../outside", "bad-name", "class", "__init__"])
async def test_invalid_package_names_are_rejected(source_manager, tmp_path, name):
    archive = module_zip(tmp_path / "plugin.zip", name=name)
    with pytest.raises(DependencyError, match="Invalid module name"):
        await source_manager.install(str(archive))
    assert not source_manager._dependencies.ledger.path.exists()


async def test_add_failure_after_move_rolls_back_source_and_imports(
    source_manager, tmp_path, monkeypatch
):
    archive = module_zip(tmp_path / "plugin.zip")

    def fail(*args):
        raise OSError("ledger unavailable")

    monkeypatch.setattr(source_manager._dependencies.ledger, "write", fail)
    with pytest.raises(OSError, match="ledger unavailable"):
        await source_manager.install(str(archive))
    assert not (source_manager.modules_dir / "plugin").exists()
    assert "plugin" not in source_manager.modules
    assert not any(
        name.startswith(source_manager.package + ".plugin") for name in sys.modules
    )


@pytest.mark.parametrize("download_fails", [False, True])
async def test_cancelled_download_finishes_before_staging_cleanup(
    source_manager,
    tmp_path,
    monkeypatch,
    download_fails,
):
    archive = module_zip(tmp_path / "plugin.zip")
    entered, release = Event(), Event()
    original = sources.prepare_source

    def paused(source, directory, ref):
        entered.set()
        assert release.wait(2)
        if download_fails:
            raise DependencyError("download failed after cancellation")
        return original(source, directory, ref)

    monkeypatch.setattr(sources, "prepare_source", paused)
    task = asyncio.create_task(source_manager.install(str(archive)))
    assert await asyncio.to_thread(entered.wait, 1)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not list(source_manager.modules_dir.glob(".alira-add-*"))
    assert not source_manager._dependencies.ledger.path.exists()
    monkeypatch.setattr(sources, "prepare_source", original)
    await source_manager.install(str(archive))
    assert "plugin" in source_manager.list()


@pytest.mark.parametrize("ref", [None, "v1.0", "feature/modules"])
async def test_github_source_uses_repository_archive(
    source_manager, tmp_path, monkeypatch, ref
):
    archive = module_zip(tmp_path / "plugin.zip", wrapper="owner-repository-sha")
    requested = []

    def download(request, *, timeout):
        requested.append(request.full_url)
        assert timeout == 30
        return io.BytesIO(archive.read_bytes())

    monkeypatch.setattr(sources, "urlopen", download)
    await source_manager.install("https://github.com/owner/repository.git", ref=ref)
    suffix = {None: "", "v1.0": "/v1.0", "feature/modules": "/feature%2Fmodules"}[ref]
    assert requested == [
        "https://api.github.com/repos/owner/repository/zipball" + suffix
    ]
    assert "plugin" in source_manager.list()
    assert source_manager.deployments == {"plugin"}


@pytest.mark.parametrize(
    "member", ["../outside", "/outside", "C:/outside", "C:outside", "..\\outside"]
)
def test_archive_paths_cannot_escape_staging(tmp_path, member):
    archive = tmp_path / "module.zip"
    with ZipFile(archive, "w") as package:
        package.writestr(member, "blocked")
    directory = tmp_path / "staging"
    directory.mkdir()
    with pytest.raises(DependencyError, match="Unsafe module archive"):
        prepare_source(str(archive), directory)
    assert not (tmp_path / "outside").exists()


def test_archive_symlinks_are_rejected(tmp_path):
    archive = tmp_path / "module.zip"
    member = ZipInfo("link")
    member.external_attr = (stat.S_IFLNK | 0o777) << 16
    with ZipFile(archive, "w") as package:
        package.writestr(member, "../outside")
    directory = tmp_path / "staging"
    directory.mkdir()
    with pytest.raises(DependencyError, match="Unsafe module archive"):
        prepare_source(str(archive), directory)


@pytest.mark.parametrize(
    "source",
    ["https://example.com/plugin.zip", "https://github.com/owner/repo/tree/main"],
)
def test_only_repository_github_urls_are_accepted(tmp_path, source):
    with pytest.raises(DependencyError, match="GitHub repository URL"):
        prepare_source(source, tmp_path)


def test_download_error_is_actionable(tmp_path, monkeypatch):
    def fail(request, **kwargs):
        raise URLError("offline")

    monkeypatch.setattr(sources, "urlopen", fail)
    with pytest.raises(DependencyError, match="Cannot download module source"):
        prepare_source("https://github.com/owner/repo", tmp_path)


def package_file(tmp_path, archives):
    import yaml

    path = tmp_path / "package.yml"
    path.write_text(
        yaml.safe_dump({"modules": [{"source": str(path)} for path in archives]})
    )
    return path


async def test_package_installs_dependent_sources_together_then_syncs_once(
    source_manager, tmp_path, monkeypatch
):
    from unittest.mock import AsyncMock

    # Order is deliberately reversed; no entry may execute during installation.
    child = module_zip(
        tmp_path / "child.zip",
        entry="raise AssertionError('entry executed')\nmodule=BaseModule('child','1',required_modules=['base'])\n",
    )
    base = module_zip(tmp_path / "base.zip", name="base")
    manifest = package_file(tmp_path, [child, base])
    ledger = source_manager._dependencies.ledger
    ledger.path.parent.mkdir()
    ledger.path.write_text("broken json")
    sync = AsyncMock(wraps=source_manager.sync)
    monkeypatch.setattr(source_manager, "sync", sync)
    await source_manager.install_package(manifest)
    sync.assert_awaited_once_with(background=False)
    assert source_manager.list() == ["base", "child"]
    assert source_manager.deployments == {"base", "child"}
    assert not source_manager.modules
    assert not list(source_manager.modules_dir.glob(".alira-package-*"))


async def test_package_retry_reuses_identical_sources_without_overwriting(
    source_manager, tmp_path
):
    archive = module_zip(tmp_path / "plugin.zip")
    manifest = package_file(tmp_path, [archive])
    await source_manager.install_package(manifest)
    target = source_manager.modules_dir / "plugin/main.py"
    timestamp = target.stat().st_mtime_ns
    bytecode = target.parent / "__pycache__/main.cpython-312.pyc"
    bytecode.parent.mkdir()
    bytecode.write_bytes(b"compiled cache")
    await source_manager.install_package(manifest)
    assert target.stat().st_mtime_ns == timestamp
    assert bytecode.read_bytes() == b"compiled cache"
    target.write_text("user changes")
    records = source_manager._dependencies.ledger.snapshot()
    with pytest.raises(DependencyError, match="different content"):
        await source_manager.install_package(manifest)
    assert target.read_text() == "user changes"
    assert source_manager._dependencies.ledger.snapshot() == records


@pytest.mark.parametrize(
    "failure", ["missing", "duplicate", "cycle", "unsafe", "structure"]
)
async def test_package_validates_entire_source_set_before_any_publication(
    source_manager, tmp_path, failure
):
    first = module_zip(tmp_path / "first.zip", name="first")
    if failure == "unsafe":
        second = tmp_path / "second.zip"
        with ZipFile(second, "w") as archive:
            archive.writestr("../escaped", "blocked")
    elif failure == "structure":
        second = tmp_path / "second.zip"
        with ZipFile(second, "w") as archive:
            archive.writestr("main.py", "")
    else:
        second = module_zip(
            tmp_path / "second.zip",
            entry=(
                "module=BaseModule('second','1',required_modules=['missing'])\n"
                if failure == "missing"
                else "module=BaseModule('first','1')\n"
                if failure == "duplicate"
                else "module=BaseModule('second','1',required_modules=['second'])\n"
            ),
        )
    with pytest.raises(DependencyError):
        await source_manager.install_package(package_file(tmp_path, [first, second]))
    assert source_manager.list() == []
    assert not source_manager._dependencies.ledger.path.exists()
    assert not list(source_manager.modules_dir.glob(".alira-package-*"))
    assert not (source_manager.modules_dir / "escaped").exists()


async def test_package_publication_failure_restores_preexisting_sources(
    source_manager, tmp_path, monkeypatch
):
    from pathlib import Path

    first = module_zip(tmp_path / "first.zip", name="first")
    second = module_zip(tmp_path / "second.zip", name="second")
    original = Path.rename

    def fail_second(path, target):
        if target == source_manager.modules_dir / "second":
            raise OSError("source move failed")
        return original(path, target)

    monkeypatch.setattr(Path, "rename", fail_second)
    with pytest.raises(OSError, match="source move failed"):
        await source_manager.install_package(package_file(tmp_path, [first, second]))
    assert source_manager.list() == []
    assert not source_manager._dependencies.ledger.path.exists()


async def test_package_cancelled_preparation_publishes_nothing(
    source_manager, tmp_path, monkeypatch
):
    first = module_zip(tmp_path / "first.zip", name="first")
    second = module_zip(tmp_path / "second.zip", name="second")
    manifest = package_file(tmp_path, [first, second])
    entered, release = Event(), Event()
    original = sources.prepare_source

    def pause_second(source, directory, ref):
        if source == str(second):
            entered.set()
            assert release.wait(2)
        return original(source, directory, ref)

    monkeypatch.setattr(sources, "prepare_source", pause_second)
    task = asyncio.create_task(source_manager.install_package(manifest))
    assert await asyncio.to_thread(entered.wait, 1)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert source_manager.list() == []
    assert not list(source_manager.modules_dir.glob(".alira-package-*"))
    assert not source_manager._dependencies.ledger.path.exists()


@pytest.mark.parametrize("cancelled", [False, True])
async def test_package_dependency_failure_keeps_sources_for_sync_repair(
    source_manager, tmp_path, monkeypatch, cancelled
):
    from unittest.mock import AsyncMock

    manifest = package_file(tmp_path, [module_zip(tmp_path / "plugin.zip")])
    original = source_manager._dependencies.rebuild
    failure = (
        asyncio.CancelledError() if cancelled else DependencyError("install failed")
    )
    monkeypatch.setattr(
        source_manager._dependencies, "rebuild", AsyncMock(side_effect=failure)
    )
    with pytest.raises(type(failure)):
        await source_manager.install_package(manifest)
    assert source_manager.list() == ["plugin"]
    monkeypatch.setattr(source_manager._dependencies, "rebuild", original)
    await source_manager.sync()
    assert source_manager.deployments == {"plugin"}


async def test_package_resolution_failure_withdraws_only_new_sources(
    source_manager, tmp_path, monkeypatch
):
    from unittest.mock import AsyncMock

    from alira.kernel.module.dependencies import DependencyResolutionError

    base = module_zip(tmp_path / "base.zip", name="base")
    await source_manager.install(str(base))
    before = source_manager._dependencies.ledger.snapshot()
    new = module_zip(tmp_path / "new.zip", name="new")
    rebuild = source_manager._dependencies.rebuild
    monkeypatch.setattr(
        source_manager._dependencies,
        "rebuild",
        AsyncMock(side_effect=DependencyResolutionError("conflicting requirements")),
    )
    with pytest.raises(DependencyResolutionError, match="conflicting"):
        await source_manager.install_package(package_file(tmp_path, [base, new]))
    assert source_manager.list() == ["base"]
    assert source_manager._dependencies.ledger.snapshot() == before
    monkeypatch.setattr(source_manager._dependencies, "rebuild", rebuild)
    await source_manager.sync()
    assert source_manager.deployments == {"base"}


@pytest.mark.parametrize(
    "content",
    [
        "",
        "[bad",
        "modules: []",
        "modules: {}",
        "modules: [null]",
        "modules: [one.zip]",
        "modules: [{source: 1}]",
        "modules: [{source: ''}]",
        "modules: [{source: one.zip, ref: 1}]",
        "modules: [{source: one.zip, typo: v1}]",
        "modules: [{source: one.zip}]\nother: true",
        "modules: [{source: one.zip, source: two.zip}]",
        "modules: [{source: one.zip}]\nmodules: [{source: two.zip}]",
        "!!python/object/apply:os.system ['exit 1']",
    ],
)
def test_package_rejects_invalid_manifest(tmp_path, content):
    from alira.kernel.module.package import read_package

    path = tmp_path / "package.yml"
    path.write_text(content)
    with pytest.raises(DependencyError, match="package"):
        read_package(path)


def test_package_local_paths_are_relative_to_manifest_and_refs_are_preserved(tmp_path):
    from alira.kernel.module.package import PackageSource, read_package

    path = tmp_path / "package.yml"
    path.write_text(
        "modules:\n  - source: archives/plugin.zip\n  - source: https://github.com/owner/repo\n    ref: v1\n"
    )
    assert read_package(path) == [
        PackageSource(str(tmp_path / "archives/plugin.zip")),
        PackageSource("https://github.com/owner/repo", "v1"),
    ]

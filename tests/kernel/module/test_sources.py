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

"""Project-local source publication, retry, isolation and archive boundaries."""

import asyncio
import io
import shutil
import stat
from pathlib import Path
from threading import Event
from urllib.error import URLError
from zipfile import ZipFile, ZipInfo

import pytest

from alira.kernel.module.dependencies import DependencyError
from alira.kernel.module.sources import archive as sources
from alira.kernel.module.sources.archive import (
    prepare_source,
    publish_source,
    source_hash,
    uninstall_sources,
)
from alira.kernel.module.sources.package import PackageSource, read_package


def module_zip(path, *, name="plugin", wrapper="", entry=None):
    prefix = wrapper + "/" if wrapper else ""
    with ZipFile(path, "w") as archive:
        archive.writestr(
            prefix + "main.py",
            entry or f"module=BaseModule({name!r}, '1.0')\n",
        )
        archive.writestr(prefix + "requirements.txt", "package==1\n")
        archive.writestr(prefix + "src/__init__.py", "")
        archive.writestr(prefix + "src/service.py", "value = 42\n")
    return path


@pytest.fixture
def modules_root(tmp_path):
    root = tmp_path / "modules"
    root.mkdir()
    return root


@pytest.mark.parametrize("wrapper", ["", "repository-commit"])
async def test_publication_inspects_without_executing_source(
    modules_root, tmp_path, wrapper
):
    source = module_zip(
        tmp_path / "plugin.zip",
        wrapper=wrapper,
        entry="raise AssertionError('entry executed')\nmodule=BaseModule('plugin','1.0')\n",
    )
    published = []
    name, metadata = await publish_source(
        str(source), modules_root, on_publish=published.append
    )
    assert name == "plugin"
    assert (modules_root / name / "src/service.py").read_text() == "value = 42\n"
    assert metadata == {
        "type": "path",
        "path": str(source),
        "sha256": source_hash(modules_root / name),
    }
    assert published == ["plugin"]
    staging = modules_root / ".alira-staging"
    assert len(list(staging.glob("receipt-*.json"))) == 1
    assert not list(staging.glob("source-*"))


async def test_identical_retry_leaves_files_and_bytecode_untouched(
    modules_root, tmp_path
):
    archive = module_zip(tmp_path / "plugin.zip")
    _, metadata = await publish_source(str(archive), modules_root)
    target = modules_root / "plugin/main.py"
    timestamp = target.stat().st_mtime_ns
    bytecode = target.parent / "__pycache__/main.cpython-312.pyc"
    bytecode.parent.mkdir()
    bytecode.write_bytes(b"compiled cache")
    published = []
    assert await publish_source(
        str(archive),
        modules_root,
        expected_hash=metadata["sha256"],
        on_publish=published.append,
    ) == ("plugin", metadata)
    assert target.stat().st_mtime_ns == timestamp
    assert bytecode.read_bytes() == b"compiled cache"
    assert published == []
    target.write_text("local edits")
    with pytest.raises(DependencyError, match="different content"):
        await publish_source(str(archive), modules_root)
    assert target.read_text() == "local edits"


async def test_partial_batch_retains_published_sources_for_retry(
    modules_root, tmp_path
):
    first = module_zip(tmp_path / "first.zip", name="first")
    second = tmp_path / "second.zip"
    with pytest.raises(DependencyError, match="local ZIP"):
        for archive in (first, second):
            await publish_source(str(archive), modules_root)
    assert (modules_root / "first/main.py").exists()
    timestamp = (modules_root / "first/main.py").stat().st_mtime_ns
    module_zip(second, name="second")
    for archive in (first, second):
        await publish_source(str(archive), modules_root)
    assert (modules_root / "first/main.py").stat().st_mtime_ns == timestamp
    assert (modules_root / "second/main.py").exists()


async def test_locked_hash_mismatch_never_publishes(modules_root, tmp_path):
    archive = module_zip(tmp_path / "plugin.zip")
    with pytest.raises(DependencyError, match="hash does not match"):
        await publish_source(str(archive), modules_root, expected_hash="0" * 64)
    assert not (modules_root / "plugin").exists()
    assert not list((modules_root / ".alira-staging").glob("source-*"))


async def test_directory_sources_are_copied_without_generated_files(
    modules_root, tmp_path
):
    archive = module_zip(tmp_path / "plugin.zip")
    unpacked = tmp_path / "original"
    unpacked.mkdir()
    root = prepare_source(str(archive), unpacked)
    (root / ".git").mkdir()
    (root / ".git/config").write_text("local git state")
    (root / "stray.pyc").write_bytes(b"bytecode")
    (root / "__pycache__").mkdir()
    (root / "__pycache__/main.pyc").write_bytes(b"bytecode")
    await publish_source(str(root), modules_root)
    assert not (modules_root / "plugin/.git").exists()
    assert not (modules_root / "plugin/stray.pyc").exists()
    assert not (modules_root / "plugin/__pycache__").exists()
    assert source_hash(root) == source_hash(modules_root / "plugin")
    assert (root / "stray.pyc").exists()


@pytest.mark.parametrize("is_directory", [False, True])
async def test_local_directory_symlinks_cannot_import_external_content(
    modules_root, tmp_path, is_directory
):
    root = tmp_path / "source"
    root.mkdir()
    (root / "main.py").write_text("module=BaseModule('plugin','1')\n")
    (root / "src").mkdir()
    target = tmp_path / "outside"
    if is_directory:
        target.mkdir()
    else:
        target.write_text("external")
    (root / "link").symlink_to(target, target_is_directory=is_directory)
    with pytest.raises(DependencyError, match="symbolic links|regular files"):
        await publish_source(str(root), modules_root)
    assert not (modules_root / "plugin").exists()


@pytest.mark.parametrize("download_fails", [False, True])
async def test_cancellation_joins_preparation_before_cleaning_staging(
    modules_root, tmp_path, monkeypatch, download_fails
):
    archive = module_zip(tmp_path / "plugin.zip")
    entered, release = Event(), Event()
    original = sources.prepare_source

    def paused(source, directory, ref):
        entered.set()
        assert release.wait(2)
        assert directory.exists()
        if download_fails:
            raise DependencyError("download failed after cancellation")
        return original(source, directory, ref)

    monkeypatch.setattr(sources, "prepare_source", paused)
    task = asyncio.create_task(publish_source(str(archive), modules_root))
    assert await asyncio.to_thread(entered.wait, 1)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not (modules_root / "plugin").exists()
    assert not list((modules_root / ".alira-staging").glob("source-*"))


@pytest.mark.parametrize("ref", [None, "v1.0", "feature/modules"])
async def test_github_source_preserves_revision(
    modules_root, tmp_path, monkeypatch, ref
):
    archive = module_zip(tmp_path / "plugin.zip", wrapper="owner-repository-sha")
    requested = []

    def download(request, *, timeout):
        requested.append(request.full_url)
        assert timeout == 30
        return io.BytesIO(archive.read_bytes())

    monkeypatch.setattr(sources, "urlopen", download)
    _, metadata = await publish_source(
        "https://github.com/owner/repository.git", modules_root, ref
    )
    suffix = {None: "", "v1.0": "/v1.0", "feature/modules": "/feature%2Fmodules"}[ref]
    assert requested == [
        "https://api.github.com/repos/owner/repository/zipball" + suffix
    ]
    assert metadata["type"] == "git"
    assert metadata["url"] == "https://github.com/owner/repository.git"
    assert metadata.get("ref") == ref
    assert await publish_source(
        "https://github.com/owner/repository.git", modules_root, ref
    ) == ("plugin", metadata)
    assert len(requested) == 1


@pytest.mark.parametrize(
    "member", ["../outside", "/outside", "C:/outside", "C:outside", "..\\outside"]
)
def test_archive_paths_cannot_escape_staging(tmp_path, member):
    archive = tmp_path / "module.zip"
    with ZipFile(archive, "w") as package:
        package.writestr(member, "blocked")
    staging = tmp_path / "staging"
    staging.mkdir()
    with pytest.raises(DependencyError, match="Unsafe module archive"):
        prepare_source(str(archive), staging)
    assert not (tmp_path / "outside").exists()


def test_archive_symlinks_are_rejected(tmp_path):
    archive = tmp_path / "module.zip"
    member = ZipInfo("link")
    member.external_attr = (stat.S_IFLNK | 0o777) << 16
    with ZipFile(archive, "w") as package:
        package.writestr(member, "../outside")
    with pytest.raises(DependencyError, match="Unsafe module archive"):
        prepare_source(str(archive), tmp_path)


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


@pytest.mark.parametrize("trash_fails", [False, True])
def test_source_trash_restores_partial_failure(
    modules_root, tmp_path, monkeypatch, trash_fails
):
    for name in ("one", "two"):
        path = modules_root / name
        path.mkdir()
        (path / "main.py").write_text(name)
    trash = tmp_path / "trash"
    trash.mkdir()
    calls = []

    def move(path):
        path = Path(path)
        calls.append(path.name)
        if trash_fails and len(calls) == 2:
            raise OSError("trash unavailable")
        path.rename(trash / path.name)

    monkeypatch.setattr("send2trash.send2trash", move)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    if trash_fails:
        with (
            pytest.raises(DependencyError, match="cancelled"),
            uninstall_sources(modules_root, ("one", "two", "one")),
        ):
            pass
        assert (modules_root / "one/main.py").read_text() == "one"
        assert (modules_root / "two/main.py").read_text() == "two"
    else:
        with uninstall_sources(modules_root, ("one", "two", "one")):
            pass
        assert list(modules_root.iterdir()) == []
    assert calls == ["one", "two"]


@pytest.mark.parametrize("input_error", [EOFError, OSError])
def test_source_removal_without_input_cancels(modules_root, monkeypatch, input_error):
    directory = modules_root / "one"
    directory.mkdir()
    (directory / "main.py").write_text("original")

    def unavailable(path):
        raise OSError("trash unavailable")

    def no_input(prompt):
        raise input_error("stdin unavailable")

    monkeypatch.setattr("send2trash.send2trash", unavailable)
    monkeypatch.setattr("builtins.input", no_input)
    with (
        pytest.raises(DependencyError, match="cancelled"),
        uninstall_sources(modules_root, ["one"]),
    ):
        pytest.fail("Cancellation must precede dependency changes")
    assert (directory / "main.py").read_text() == "original"


def test_partial_permanent_deletion_failure_restores_source(modules_root, monkeypatch):
    directory = modules_root / "one"
    directory.mkdir()
    (directory / "main.py").write_text("original")
    (directory / "data.txt").write_text("data")
    rmtree = sources.shutil.rmtree

    def unavailable(path):
        raise OSError("trash unavailable")

    def partial_delete(path, *args, **kwargs):
        if Path(path) == directory:
            (directory / "main.py").unlink()
            raise OSError("deletion failed")
        return rmtree(path, *args, **kwargs)

    monkeypatch.setattr("send2trash.send2trash", unavailable)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    monkeypatch.setattr(sources.shutil, "rmtree", partial_delete)
    with (
        pytest.raises(OSError, match="deletion failed"),
        uninstall_sources(modules_root, ["one"]),
    ):
        pytest.fail("Incomplete source deletion must not change dependencies")
    assert (directory / "main.py").read_text() == "original"
    assert (directory / "data.txt").read_text() == "data"


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
    path = tmp_path / "package.yml"
    path.write_text(content)
    with pytest.raises(DependencyError, match="package"):
        read_package(path)


def test_package_paths_are_relative_to_manifest_and_refs_are_preserved(tmp_path):
    path = tmp_path / "package.yml"
    path.write_text(
        "modules:\n  - source: archives/plugin.zip\n  - source: https://github.com/owner/repo\n    ref: v1\n"
    )
    assert read_package(path) == [
        PackageSource(str(tmp_path / "archives/plugin.zip")),
        PackageSource("https://github.com/owner/repo", "v1"),
    ]


def test_tree_hash_tracks_names_and_content_not_traversal_order(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    (first / "z.py").write_text("z")
    (first / "a.py").write_text("a")
    shutil.copytree(first, second)
    assert source_hash(first) == source_hash(second)
    (second / "a.py").rename(second / "b.py")
    assert source_hash(first) != source_hash(second)
    (second / "b.py").rename(second / "a.py")
    (second / "a.py").write_text("changed")
    assert source_hash(first) != source_hash(second)


async def test_receipt_retry_skips_extraction_and_download(
    modules_root, tmp_path, monkeypatch
):
    archive = module_zip(tmp_path / "plugin.zip")
    result = await publish_source(str(archive), modules_root)

    def unexpected(*args, **kwargs):
        raise AssertionError("source was prepared again")

    monkeypatch.setattr(sources, "prepare_source", unexpected)
    assert await publish_source(str(archive), modules_root) == result


async def test_modified_archive_invalidates_retry_receipt(modules_root, tmp_path):
    archive = module_zip(tmp_path / "plugin.zip")
    await publish_source(str(archive), modules_root)
    module_zip(archive, entry="module=BaseModule('plugin','2')\n")
    with pytest.raises(DependencyError, match="different content"):
        await publish_source(str(archive), modules_root)
    assert "'1.0'" in (modules_root / "plugin/main.py").read_text()

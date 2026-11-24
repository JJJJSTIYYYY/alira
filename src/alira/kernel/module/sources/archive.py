"""Validate and atomically publish project-local module source trees."""

import asyncio
import hashlib
import json
import os
import shutil
import stat
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath
from tempfile import TemporaryDirectory
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
from zipfile import BadZipFile, ZipFile

from alira.kernel.module.dependencies.ledger import DependencyLedger
from alira.kernel.module.dependencies.types import DependencyError
from alira.kernel.module.discovery.catalog import ModuleSpec, module_directory


def prepare_source(source: str, directory: Path, ref: str | None = None) -> Path:
    """Download or extract an archive into caller-owned staging without executing code.

    Args:
        source: Local ZIP or directory path, relative to the current working
            directory unless absolute, or a public GitHub repository URL.
        directory: Existing staging directory for downloaded and extracted files.
            The contents child directory must not already exist.
        ref: Optional GitHub branch, tag or commit; invalid for local sources.

    Returns:
        Extracted module root with main.py, either archive-root or one wrapper
        directory deep. Adds an empty __init__.py if absent; does not check src/.

    Raises:
        DependencyError: Unsupported source URL, unsafe archive paths/types,
            download/unpacking failure or ambiguous/missing entry structure.

    Notes:
        Rejects absolute/traversal paths, Windows drive paths, symlinks and special
        files. GitHub downloads use a bounded socket timeout. Caller owns staging
        cleanup; name, src/ and declaration checks happen in prepare_module_source.
    """
    parsed = urlsplit(source)
    if parsed.scheme == "https":
        parts = parsed.path.strip("/").split("/")
        if (
            parsed.netloc != "github.com"
            or len(parts) != 2
            or not all(parts)
            or parsed.query
            or parsed.fragment
        ):
            raise DependencyError(
                "Use a GitHub repository URL: https://github.com/owner/repo"
            )
        owner, repo = parts
        repo = repo.removesuffix(".git")
        url = f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/zipball"
        if ref is not None:
            url += "/" + quote(ref, safe="")
        archive = directory / "source.zip"
        request = Request(url, headers={"User-Agent": "alira-harness"})
        try:
            with urlopen(request, timeout=30) as response, archive.open("wb") as output:
                shutil.copyfileobj(response, output)
        except OSError as exc:
            raise DependencyError(f"Cannot download module source: {exc}") from exc
    else:
        if ref is not None:
            raise DependencyError("--ref is only supported for GitHub repositories")
        local = Path(source).expanduser()
        if local.is_symlink():
            raise DependencyError("Module source must not be a symbolic link")
        archive = local.resolve()
        if archive.is_dir():
            root = directory / "contents"
            shutil.copytree(
                archive, root, symlinks=True, ignore=_ignored_source_entries
            )
            source_hash(root)
            (root / "__init__.py").touch(exist_ok=True)
            return root
        if archive.suffix.lower() != ".zip" or not archive.is_file():
            raise DependencyError(
                "Module source must be a local ZIP, directory or a GitHub repository URL"
            )

    extracted = directory / "contents"
    extracted.mkdir()
    try:
        with ZipFile(archive) as package:
            for entry in package.infolist():
                path = PurePosixPath(entry.filename)
                kind = stat.S_IFMT(entry.external_attr >> 16)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or "\\" in entry.filename
                    or PureWindowsPath(entry.filename).drive
                    or kind not in (0, stat.S_IFREG, stat.S_IFDIR)
                ):
                    raise DependencyError(
                        f"Unsafe module archive entry: {entry.filename}"
                    )
                target = extracted / path
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with package.open(entry) as data, target.open("xb") as output:
                        shutil.copyfileobj(data, output)
    except (BadZipFile, OSError, RuntimeError) as exc:
        raise DependencyError(f"Cannot unpack module ZIP: {exc}") from exc

    if (extracted / "main.py").is_file():
        root = extracted
    else:
        entries = list(extracted.glob("*/main.py"))
        if len(entries) != 1:
            raise DependencyError(
                "Module ZIP must contain main.py at its root or in one top-level directory"
            )
        root = entries[0].parent
    (root / "__init__.py").touch(exist_ok=True)
    return root


def _ignored_source_entries(directory: str, names: list[str]) -> set[str]:
    """Exclude interpreter and VCS output from copied source trees."""
    return {
        name
        for name in names
        if name in {".git", "__pycache__"} or name.endswith(".pyc")
    }


def source_hash(directory: Path) -> str:
    """Return a stable SHA-256 digest of relative file names and file contents.

    Files are sorted by their POSIX relative paths. Generated bytecode and Git
    metadata do not participate. Symlinks and special files are rejected, so the
    digest always describes files contained in this module's source tree.
    Empty directories do not affect the digest.

    Args:
        directory: Existing source directory; symlink roots are rejected.

    Raises:
        DependencyError: The source contains a symlink or a nonregular file.
        OSError: A source file cannot be read.
    """
    if directory.is_symlink() or not directory.is_dir():
        raise DependencyError(f"Module source must be a directory: {directory}")
    files = []
    for current, directories, names in os.walk(directory):
        directories[:] = sorted(
            name for name in directories if name not in {".git", "__pycache__"}
        )
        for name in directories:
            if (Path(current) / name).is_symlink():
                raise DependencyError("Module sources must not contain symbolic links")
        for name in names:
            if name.endswith(".pyc") or name == ".git":
                continue
            path = Path(current) / name
            if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
                raise DependencyError(
                    "Module sources must contain only regular files and directories"
                )
            files.append((path.relative_to(directory).as_posix(), path))
    digest = hashlib.sha256()
    for relative, path in sorted(files):
        encoded = relative.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        with path.open("rb") as stream:
            digest.update(hashlib.file_digest(stream, "sha256").digest())
    return digest.hexdigest()


def _source_receipt(staging: Path, source: str, ref: str | None) -> Path:
    """Key a retry receipt by source identity and local input content."""
    parsed = urlsplit(source)
    if parsed.scheme == "https":
        identity = {"url": source, "ref": ref}
    else:
        local = Path(source).expanduser()
        if local.is_symlink():
            raise DependencyError("Module source must not be a symbolic link")
        local = local.resolve()
        if local.is_dir():
            digest = source_hash(local)
        elif local.is_file():
            with local.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
        else:
            raise DependencyError(
                "Module source must be a local ZIP, directory or a GitHub repository URL"
            )
        identity = {"path": str(local), "sha256": digest, "ref": ref}
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return staging / f"receipt-{key}.json"


async def publish_source(
    source: str,
    modules_root: Path,
    ref: str | None = None,
    *,
    expected_hash: str | None = None,
    on_publish: Callable[[str], None] | None = None,
) -> tuple[str, dict]:
    """Validate and publish one ZIP, directory or GitHub repository source.

    Args:
        source: Local ZIP/directory path or GitHub repository URL.
        modules_root: Existing project module root. Callers hold its state lock.
        ref: GitHub branch, tag or commit, rejected for local sources.
        expected_hash: Optional content digest from the declaration or lock.
        on_publish: Synchronous journal callback, called after a new source is
            published. Existing matching sources do not trigger this callback.

    Returns:
        The declared module name and its source declaration, including a SHA-256
        content digest. Existing identical destinations are left untouched.

    Raises:
        DependencyError: Invalid/unsafe source, digest mismatch or differing
            destination content. Existing sources are preserved.
        OSError: Staging or atomic publication fails.
        asyncio.CancelledError: Cancellation after the preparation worker exits.

    Notes:
        Staging lives under modules_root/.alira-staging so os.replace is atomic.
        Small publication receipts remain there for interrupted-batch retries;
        they contain source identities and digests, never source archives or trees.
        Matching installed content is reused without downloading or extracting it.
        Local input changes invalidate receipts. Git receipts identify the requested
        revision, and locked hashes still apply before reusing an installed tree.
        A source already published is retained if any later operation fails.
    """
    if not modules_root.is_dir():
        raise DependencyError(f"Module source root does not exist: {modules_root}")
    if ref is not None and urlsplit(source).scheme != "https":
        raise DependencyError("--ref is only supported for GitHub repositories")
    staging = modules_root / ".alira-staging"
    receipt = _source_receipt(staging, source, ref)
    expected = expected_hash.removeprefix("sha256:") if expected_hash else None
    if receipt.exists():
        record = json.loads(receipt.read_text(encoding="utf-8"))
        name, metadata = record["name"], record["source"]
        destination = module_directory(modules_root, name)
        digest = metadata["sha256"]
        if destination.is_dir() and (expected is None or expected == digest):
            if source_hash(destination) != digest:
                raise DependencyError(
                    f"Module '{name}' already exists with different content; existing sources are preserved"
                )
            return name, metadata
    staging.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="source-", dir=staging) as temporary:
        spec = await prepare_module_source(source, Path(temporary), modules_root, ref)
        digest = source_hash(spec.directory)
        if expected is not None and digest != expected:
            raise DependencyError(
                f"Module '{spec.name}' source hash does not match its lock"
            )
        destination = module_directory(modules_root, spec.name)
        if urlsplit(source).scheme == "https":
            metadata = {"type": "git", "url": source, "sha256": digest}
            if ref is not None:
                metadata["ref"] = ref
        else:
            metadata = {
                "type": "path",
                "path": str(Path(source).expanduser().resolve()),
                "sha256": digest,
            }
        exists = destination.exists()
        if exists and (not destination.is_dir() or source_hash(destination) != digest):
            raise DependencyError(
                f"Module '{spec.name}' already exists with different content; existing sources are preserved"
            )
        # Persist before publication so a crash after os.replace can be resumed.
        DependencyLedger.atomic_write(
            receipt,
            json.dumps(
                {"name": spec.name, "source": metadata}, sort_keys=True
            ).encode(),
        )
        if not exists:
            os.replace(spec.directory, destination)
            if on_publish is not None:
                on_publish(spec.name)
        return spec.name, metadata


@contextmanager
def uninstall_sources(modules_dir: Path, names: Collection[str]) -> Iterator[None]:
    """Back up selected sources and move them to system trash after the body succeeds.

    Args:
        modules_dir: Source root containing the selected module directories.
        names: Existing module names; duplicates identify the same directory once.

    Yields:
        None after source validation and backup. The caller removes dependency
        claims inside the body and owns the surrounding dependency transaction.

    Raises:
        DependencyError: A name is invalid or a source entry is absent.
        OSError: Backup, system trash or restoration failed.
        BaseExceptionGroup: The original failure and incomplete source restoration.

    Notes:
        Body failure, cancellation or partial trash failure restores missing sources
        before propagating to the caller's dependency transaction. Existing source
        directories are preserved. No dependency records or packages are accessed.
    """
    from send2trash import send2trash

    directories = [module_directory(modules_dir, name) for name in dict.fromkeys(names)]
    for directory in directories:
        if not (directory / "main.py").is_file():
            raise DependencyError(f"Module not found: {directory.name}")
    with TemporaryDirectory(prefix="alira-uninstall-") as temporary:
        backup = Path(temporary)
        for directory in directories:
            shutil.copytree(directory, backup / directory.name, symlinks=True)
        try:
            yield
            for directory in directories:
                send2trash(str(directory))
        except BaseException as failure:
            errors = []
            for directory in directories:
                if not directory.exists():
                    try:
                        shutil.copytree(
                            backup / directory.name, directory, symlinks=True
                        )
                    except OSError as exc:
                        errors.append(exc)
            if errors:
                raise BaseExceptionGroup(
                    "Module uninstall failed and source restoration was incomplete",
                    [failure, *errors],
                ) from None
            raise


async def prepare_module_source(
    source: str, directory: Path, modules_dir: Path, ref: str | None = None
) -> ModuleSpec:
    """Prepare a source asynchronously and inspect its static module specification.

    Args:
        source: Local ZIP/directory path or GitHub repository URL.
        directory: Existing extraction workspace owned by the caller.
        modules_dir: Eventual module root for resolving explicit requirements paths.
        ref: Optional GitHub revision, rejected for local archives.

    Returns:
        ModuleSpec pointing to extracted sources containing main.py and src/.

    Raises:
        DependencyError: Unsafe/invalid source structure or static declaration.
        asyncio.CancelledError: Cancellation after joining the extraction worker;
            caller-owned staging is safe to clean once this coroutine exits.

    Notes:
        Blocking copy/download/extraction runs in a thread. Cancellation waits for that
        worker instead of deleting its workspace underneath it. No entry is imported.
    """
    preparing = asyncio.create_task(
        asyncio.to_thread(prepare_source, str(source), directory, ref)
    )
    try:
        root = await asyncio.shield(preparing)
    except asyncio.CancelledError:
        await asyncio.gather(preparing, return_exceptions=True)
        raise
    if not (root / "src").is_dir():
        raise DependencyError("Module source must contain main.py and src/")
    fallback = Path(urlsplit(source).path).stem
    if root.name != "contents" and root.name.isidentifier():
        fallback = root.name
    return ModuleSpec.read(root, default_name=fallback, modules_dir=modules_dir)

"""Prepare and install module sources, rolling back failed registrations."""

import asyncio
import shutil
import stat
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath
from tempfile import TemporaryDirectory
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
from zipfile import BadZipFile, ZipFile

from alira.kernel.module.catalog import ModuleSpec, module_directory
from alira.kernel.module.dependencies.types import DependencyError
from alira.kernel.module.registry import ModuleRegistry


def prepare_source(source: str, directory: Path, ref: str | None = None) -> Path:
    """Download or extract an archive into caller-owned staging without executing code.

    Args:
        source: Local ZIP path, relative to the current working directory unless
            absolute, or a public https://github.com/owner/repo URL.
        directory: Existing staging directory for downloaded and extracted files.
            The contents child directory must not already exist.
        ref: Optional GitHub branch, tag or commit; invalid for local ZIPs.

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
        archive = Path(source).expanduser().resolve()
        if archive.suffix.lower() != ".zip" or not archive.is_file():
            raise DependencyError(
                "Module source must be a local ZIP or a GitHub repository URL"
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


@asynccontextmanager
async def install_source(
    registry: ModuleRegistry, source: str, ref: str | None = None
) -> AsyncIterator[str]:
    """Stage one validated source and temporarily publish its final module directory.

    Args:
        registry: Supplies the destination module root; runtime registrations are
            not changed or initialized by this context manager.
        source: Local ZIP or GitHub repository source accepted by prepare_source.
        ref: Optional GitHub revision.

    Yields:
        Declared module name after the source has been atomically moved into place.

    Raises:
        DependencyError: Source validation failed or the destination already exists.
        OSError: Copying, staging or moving source failed.
        BaseExceptionGroup: Context-body failure and failed source rollback.

    Notes:
        Normal exit retains source. An exceptional body exit attempts to move it
        back into staging for cleanup. This protects sources only; the caller must
        manage dependency transactions. Forced termination cannot execute rollback.
    """
    registry.directory.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="alira-install-") as temporary:
        spec = await prepare_module_source(
            source, Path(temporary), registry.directory, ref
        )
        root = spec.directory
        destination = module_directory(registry.directory, spec.name)
        if destination.exists() or destination.is_symlink():
            raise DependencyError(
                f"Module '{spec.name}' already exists; existing sources are preserved"
            )
        with TemporaryDirectory(
            prefix=".alira-install-", dir=registry.directory
        ) as staging:
            staged = Path(staging) / spec.name
            shutil.copytree(root, staged)
            staged.rename(destination)
            try:
                yield spec.name
            except BaseException as failure:
                try:
                    destination.rename(staged)
                except OSError as exc:
                    raise BaseExceptionGroup(
                        "Module install failed and source rollback was incomplete",
                        [failure, exc],
                    ) from None
                raise


async def prepare_module_source(
    source: str, directory: Path, modules_dir: Path, ref: str | None = None
) -> ModuleSpec:
    """Extract a source asynchronously and inspect its static module specification.

    Args:
        source: ZIP path or GitHub repository URL.
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
        Blocking download/extraction runs in a thread. Cancellation waits for that
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

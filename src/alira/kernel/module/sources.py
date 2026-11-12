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
    """Accept a local ZIP or a public GitHub repository URL."""
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
    """Validate and stage a module without executing any of its Python code."""
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
    """Download, extract and inspect an entry without executing module code."""
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

"""uv resolution and visible foreground/background environment operations."""

import asyncio
import importlib
import sys
import tomllib
from pathlib import Path

from packaging.utils import canonicalize_name

from alira.kernel.module.dependencies.types import DependencyError, ModuleRequirements

EMPTY_LOCK = b'lock-version = "1.0"\ncreated-by = "alira"\nrequires-python = ">=3.12"\npackages = []\n'


async def run_uv(*arguments: str, cwd: Path, capture: bool = True) -> None:
    """Execute uv pip against the current interpreter and wait for completion.

    Args:
        *arguments: pip subcommand and arguments, passed as separate argv elements
            without a shell. The --python target is appended as sys.executable.
        cwd: Existing process working directory, not a uv project to modify.
        capture: True discards stdout and captures stderr for failures. False
            inherits terminal streams so foreground progress appears in real time.

    Raises:
        DependencyError: uv could not be launched or returned a nonzero status.
        asyncio.CancelledError: Caller cancellation after killing and joining uv.

    Notes:
        Both modes wait for the subprocess; neither detaches it. Cancellation may
        leave partial Python package changes, requiring source-driven sync. Import
        caches are invalidated after successful completion. No shell is invoked.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            "uv",
            "pip",
            *arguments,
            "--python",
            sys.executable,
            cwd=cwd,
            stdout=asyncio.subprocess.DEVNULL if capture else None,
            stderr=asyncio.subprocess.PIPE if capture else None,
        )
    except OSError as exc:
        raise DependencyError(f"Cannot launch uv: {exc}") from exc
    try:
        _, stderr = await process.communicate()
    except asyncio.CancelledError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.communicate()
        raise
    if process.returncode:
        message = (
            stderr.decode(errors="replace").strip()
            if stderr
            else f"uv {' '.join(arguments[:1])} exited with code {process.returncode}"
        )
        raise DependencyError(message)
    importlib.invalidate_caches()


async def _compile(
    sources: list[Path], constraints: Path, directory: Path, cwd: Path
) -> ModuleRequirements:
    """Compile requirement sources under pins into a new workspace's PEP 751 lock.

    Returns a ModuleRequirements closure with canonical names and exact versions.
    Non-versioned lock entries raise DependencyError; caller owns workspace lifetime.
    """
    directory.mkdir()
    lockfile = directory / "pylock.toml"
    await run_uv(
        "compile",
        *(str(path) for path in sources),
        "--constraints",
        str(constraints),
        "--format",
        "pylock.toml",
        "--output-file",
        str(lockfile),
        cwd=cwd,
    )
    with lockfile.open("rb") as stream:
        lock = tomllib.load(stream)
    packages = {}
    for package in lock.get("packages", []):
        if "version" not in package:
            raise DependencyError(
                f"Dependency {package['name']} has no locked version; use a versioned distribution"
            )
        packages[canonicalize_name(package["name"])] = package["version"]
    return ModuleRequirements(lockfile, packages)


async def resolve_requirements(
    sources: dict[str, Path | None],
    pins: dict[str, str],
    directory: Path,
    cwd: Path,
) -> dict[str, ModuleRequirements]:
    """Jointly solve module requirements, then compute each exact pinned closure.

    Args:
        sources: Module names mapped to requirements files or None.
        pins: Canonical distribution/version constraints, normally the kernel closure.
        directory: Existing empty caller-owned workspace for generated locks and pins.
        cwd: Working directory for the combined resolution. Per-file closure
            resolution uses each requirements file's parent directory.

    Returns:
        Module-name-to-ModuleRequirements mapping. Modules without requirements get
        empty packages and no individual lock. The complete combined pylock.toml
        is written under directory/combined, even when no sources exist.

    Raises:
        DependencyError: uv solving failed, a lock lacks exact versions or individual
            closure compilation failed. Individual closures share the joint pins.
        OSError: Working directory or generated-file I/O failed.

    Notes:
        Does not install/update/remove packages or write the persistent ledger.
        Generated lock paths are valid only while the caller retains directory.
        Resolution may use the network and honors requirement include directives.
    """
    plans = {
        name: ModuleRequirements(None, {})
        for name, path in sources.items()
        if path is None
    }
    files = list(dict.fromkeys(path for path in sources.values() if path is not None))
    combined_path = directory / "combined" / "pylock.toml"
    if not files:
        combined_path.parent.mkdir()
        combined_path.write_bytes(EMPTY_LOCK)
        return plans
    constraints = directory / "constraints.txt"
    constraints.write_text(
        "".join(f"{name}=={version}\n" for name, version in sorted(pins.items()))
    )
    combined = await _compile(files, constraints, directory / "combined", cwd)
    constraints.write_text(
        "".join(
            f"{name}=={version}\n"
            for name, version in sorted((pins | combined.packages).items())
        )
    )
    if len(files) == 1:
        resolved = {files[0]: combined}
    else:
        try:
            async with asyncio.TaskGroup() as group:
                tasks = {
                    path: group.create_task(
                        _compile(
                            [path], constraints, directory / str(index), path.parent
                        )
                    )
                    for index, path in enumerate(files)
                }
        except ExceptionGroup as exc:
            raise DependencyError(
                "Dependency closure resolution failed:\n"
                + "\n".join(str(error) for error in exc.exceptions)
            ) from exc
        resolved = {path: task.result() for path, task in tasks.items()}
    plans.update(
        {name: resolved[path] for name, path in sources.items() if path is not None}
    )
    return plans

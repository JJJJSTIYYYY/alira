"""Resolve complete module requirements with uv, including transitive packages."""

import asyncio
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from packaging.utils import canonicalize_name


class DependencyError(RuntimeError):
    """Dependency resolution or an environment operation failed."""


@dataclass(frozen=True)
class DependencyInfo:
    name: str
    version: str
    modules: frozenset[str]
    preinstalled: bool

    @property
    def ref_count(self) -> int:
        return len(self.modules)


@dataclass(frozen=True)
class ModuleRequirements:
    lockfile: Path | None
    packages: dict[str, str]


async def run_uv(*arguments: str, cwd: Path) -> None:
    """Run uv without a shell and reap it on cancellation."""
    try:
        process = await asyncio.create_subprocess_exec(
            "uv", "pip", *arguments, "--python", sys.executable, "--no-progress",
            cwd=cwd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
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
        raise DependencyError(stderr.decode(errors="replace").strip())


async def _compile(
    sources: list[Path], constraints: Path, directory: Path, cwd: Path,
) -> ModuleRequirements:
    directory.mkdir()
    lockfile = directory / "pylock.toml"
    await run_uv(
        "compile", *(str(path) for path in sources),
        "--constraints", str(constraints), "--format", "pylock.toml",
        "--output-file", str(lockfile), cwd=cwd,
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
    sources: dict[str, Path | None], pins: dict[str, str], directory: Path, cwd: Path,
) -> dict[str, ModuleRequirements]:
    """Solve all new modules together, then obtain each module's exact closure.

    Existing environment versions are constraints, so loading a plugin cannot
    upgrade packages already imported by the host or other running modules.
    uv owns requirements syntax, markers, extras, includes and version solving.
    """
    plans = {name: ModuleRequirements(None, {}) for name, path in sources.items() if path is None}
    files = list(dict.fromkeys(path for path in sources.values() if path is not None))
    if not files:
        return plans
    constraints = directory / "constraints.txt"
    constraints.write_text("".join(f"{name}=={version}\n" for name, version in sorted(pins.items())))
    combined = await _compile(files, constraints, directory / "combined", cwd)
    constraints.write_text("".join(
        f"{name}=={version}\n" for name, version in sorted((pins | combined.packages).items())
    ))
    if len(files) == 1:
        resolved = {files[0]: combined}
    else:
        async with asyncio.TaskGroup() as group:
            tasks = {
                path: group.create_task(_compile(
                    [path], constraints, directory / str(index), path.parent,
                ))
                for index, path in enumerate(files)
            }
        resolved = {path: task.result() for path, task in tasks.items()}
    plans.update({name: resolved[path] for name, path in sources.items() if path is not None})
    return plans

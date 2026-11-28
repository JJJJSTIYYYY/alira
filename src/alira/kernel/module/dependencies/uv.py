"""uv resolution and visible foreground/background environment operations."""

import asyncio
import hashlib
import importlib
import json
import sys
import tomllib
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

from packaging.utils import canonicalize_name

from alira.kernel.module.dependencies.types import DependencyError, ModuleRequirements

EMPTY_LOCK = b'lock-version = "1.0"\ncreated-by = "alira"\nrequires-python = ">=3.12"\npackages = []\n'
RESOLVER_VERSION = "uv-pep751-v1"


def complete_local_artifacts(lock: dict, directory: Path) -> None:
    """Pin local archive bytes which uv leaves unhashed in PEP 751 output.

    PEP 751 paths are relative to the generated lock, so normalize them before its
    temporary workspace disappears. Remote artifacts must already carry hashes;
    this conversion never downloads or resolves dependencies.
    """
    for package in lock.get("packages", []):
        artifacts = [*package.get("wheels", [])]
        if "sdist" in package:
            artifacts.append(package["sdist"])
        for artifact in artifacts:
            path = None
            if "path" in artifact:
                path = (directory / artifact["path"]).resolve()
                artifact["path"] = str(path)
            elif "url" in artifact:
                location = urlsplit(artifact["url"])
                if location.scheme == "file" and location.netloc in {"", "localhost"}:
                    path = Path(unquote(location.path))
            if path is not None and "sha256" not in artifact.get("hashes", {}):
                with path.open("rb") as stream:
                    artifact.setdefault("hashes", {})["sha256"] = hashlib.file_digest(
                        stream, "sha256"
                    ).hexdigest()


def _toml_value(value, *, key: str | None = None) -> str:
    """Encode lock values as TOML, restoring JSON upload times as datetime literals."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        if key == "upload-time":
            return datetime.fromisoformat(value).isoformat()
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return (
            "{ "
            + ", ".join(
                f"{json.dumps(key)} = {_toml_value(item, key=key)}"
                for key, item in value.items()
            )
            + " }"
        )
    raise DependencyError(f"Unsupported dependency artifact value: {value!r}")


def installation_lock(lock: dict, names=None) -> bytes:
    """Build a temporary PEP 751 artifact solely from the authoritative JSON lock.

    Package URLs, local artifact paths, hashes, dependency markers and installation
    metadata survive the conversion, including typed upload timestamps. ``names``
    selects an already closed subset; it does not trigger resolution or read
    requirements files.
    """
    header = lock.get(
        "install-metadata",
        {"lock-version": "1.0", "created-by": "alira", "requires-python": ">=3.12"},
    )
    lines = [
        f"{json.dumps(key)} = {_toml_value(value)}\n" for key, value in header.items()
    ]
    packages = lock["packages"]
    selected = sorted(packages if names is None else names)
    if not selected:
        lines.append("packages = []\n")
    for name in selected:
        lines.append("\n[[packages]]\n")
        for key, value in packages[name].items():
            if key != "sha256":
                lines.append(f"{json.dumps(key)} = {_toml_value(value)}\n")
    return "".join(lines).encode("utf-8")


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
    directory.mkdir(exist_ok=True)
    lockfile = directory / "pylock.toml"
    await run_uv(
        "compile",
        *(str(path) for path in sources),
        "--constraints",
        str(constraints),
        "--format",
        "pylock.toml",
        "--generate-hashes",
        "--output-file",
        str(lockfile),
        cwd=cwd,
    )
    with lockfile.open("rb") as stream:
        lock = tomllib.load(stream)
    complete_local_artifacts(lock, lockfile.parent)
    lockfile.write_bytes(
        installation_lock(
            {
                "packages": {
                    canonicalize_name(package["name"]): package
                    for package in lock.get("packages", [])
                },
                "install-metadata": {
                    key: value for key, value in lock.items() if key != "packages"
                },
            }
        )
    )
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
    *,
    previous_lock: dict | None = None,
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
    if previous_lock is not None:
        combined_path.parent.mkdir()
        combined_path.write_bytes(installation_lock(previous_lock))
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

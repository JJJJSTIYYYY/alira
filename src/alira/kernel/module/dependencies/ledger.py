"""Separate module declarations and locks with serialized, recoverable writes."""

import asyncio
import base64
import hashlib
import json
import os
import platform
import socket
import sys
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from filelock import FileLock, Timeout
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version

from alira._version import __version__
from alira.kernel.module.dependencies.types import DependencyError, DependencyLockError
from alira.kernel.module.discovery.resolution import (
    parse_module_requirement,
    validate_module_name,
)


def _json_bytes(data: Mapping) -> bytes:
    """Encode human-readable state deterministically."""
    return (json.dumps(data, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _is_sha256(value: object) -> bool:
    """Recognize the lowercase hexadecimal representation written by the resolver."""
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _validate_source(source: dict) -> None:
    """Validate persisted source metadata before restoring module files from it."""
    kind = source["type"]
    if kind not in {"path", "git", "registry", "builtin"}:
        raise ValueError("unknown module source type")
    if kind == "path" and (not isinstance(source["path"], str) or not source["path"]):
        raise ValueError("invalid local module source")
    if kind == "git" and (
        not isinstance(source["url"], str)
        or not source["url"]
        or (source.get("ref") is not None and not isinstance(source["ref"], str))
    ):
        raise ValueError("invalid git module source")
    if "sha256" in source and not _is_sha256(source["sha256"]):
        raise ValueError("invalid module source hash")


def inputs_hash(
    declaration: Mapping,
    *,
    resolver_version: str,
    kernel_version: str = __version__,
    python_version: str | None = None,
) -> str:
    """Fingerprint normalized declarations and the complete resolver identity.

    Args:
        declaration: The format-2 module declaration, including requirement hashes.
        resolver_version: Version of the dependency resolver used to generate a lock.
        kernel_version: Alira version; defaults to the running kernel.
        python_version: Full Python version; defaults to the current interpreter.

    Returns:
        A ``sha256:``-prefixed digest independent of JSON formatting or key order.
    """
    inputs = {
        "declaration": declaration,
        "kernel": kernel_version,
        "python": python_version or platform.python_version(),
        "resolver": resolver_version,
    }
    content = json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(content).hexdigest()


class DependencyLedger:
    """Own the declaration, generated lock, process lock and recovery journal.

    Constructing a ledger performs no I/O. Read methods never create directories.
    Writers must use ``transaction`` around an entire dependency operation so a
    failed resolution or installation restores both files to their original bytes.
    Published source directories and Python package changes are retained for sync.
    """

    def __init__(self, directory: Path, *, lock_timeout: float = 30):
        """Store project-state paths and the cross-process lock timeout in seconds."""
        self.directory = directory
        self.path = directory / "modules.json"
        self.lockfile = directory / "modules.lock.json"
        self.journal = directory / "journal.json"
        self.lock_info = directory / "lock.info"
        self.lock_timeout = lock_timeout

    def read(self) -> dict:
        """Read a validated format-2 declaration, or an empty undeclared project.

        Raises:
            DependencyError: Existing JSON is unreadable or has invalid declarations.
                Format-1 state requires the explicit ``alira migrate`` command.
        """
        if not self.path.exists():
            return {"format": 2, "requires-kernel": "", "modules": {}}
        try:
            data = json.loads(self.path.read_bytes())
            if data["format"] != 2 or not isinstance(data["modules"], dict):
                raise ValueError("expected format-2 module declarations")
            SpecifierSet(data["requires-kernel"])
            for name, record in data["modules"].items():
                validate_module_name(name)
                Version(record["version"])
                _validate_source(record["source"])
                if not isinstance(record["required-modules"], list):
                    raise TypeError("required-modules must be a list")
                for requirement in record["required-modules"]:
                    parse_module_requirement(requirement)
                if record["requirements"] is not None and not isinstance(
                    record["requirements"], str
                ):
                    raise ValueError("invalid requirements path")
                fingerprint = record["requirements-hash"]
                if fingerprint is not None and not isinstance(fingerprint, str):
                    raise ValueError("invalid requirements fingerprint")
            return data
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise DependencyError(
                "Invalid module declarations; repair modules.json or run alira migrate."
            ) from exc

    def read_lock(self) -> dict | None:
        """Read the generated JSON lock without comparing it with declarations.

        Returns None if no lock exists. Staleness is checked by the caller, since
        frozen operations deliberately use a lock with different current inputs.
        Invalid package ownership or malformed metadata raises DependencyError.
        """
        if not self.lockfile.exists():
            return None
        try:
            data = json.loads(self.lockfile.read_bytes())
            if data["format"] != 1:
                raise ValueError("unknown lock format")
            if not all(
                isinstance(data[key], str) for key in ("generated-by", "inputs-hash")
            ):
                raise ValueError("invalid lock identity")
            Version(data["kernel"]["version"])
            Version(data["kernel"]["python"])
            if "install-metadata" in data and not isinstance(
                data["install-metadata"], dict
            ):
                raise TypeError("invalid installation metadata")
            packages = data["packages"]
            if not isinstance(packages, dict) or not isinstance(data["modules"], dict):
                raise TypeError("invalid lock mappings")
            for name, package in packages.items():
                if canonicalize_name(name, validate=True) != name:
                    raise ValueError("package names must be normalized")
                if package["name"] != name or not _is_sha256(package["sha256"]):
                    raise ValueError("invalid locked package identity")
                Version(package["version"])
                wheels = package.get("wheels", [])
                if not isinstance(wheels, list):
                    raise TypeError("wheels must be a list")
                artifacts = [*wheels]
                if "sdist" in package:
                    artifacts.append(package["sdist"])
                if not artifacts:
                    raise ValueError("locked package has no artifacts")
                for artifact in artifacts:
                    if not _is_sha256(artifact["hashes"]["sha256"]):
                        raise ValueError("invalid artifact SHA-256 hash")
                    locations = [
                        artifact[key] for key in ("url", "path") if key in artifact
                    ]
                    if not locations or not all(
                        isinstance(location, str) and location for location in locations
                    ):
                        raise ValueError("invalid artifact location")
            owned = set()
            for name, record in data["modules"].items():
                validate_module_name(name)
                Version(record["version"])
                _validate_source(record["source"])
                if not isinstance(record["required-modules"], list):
                    raise TypeError("required-modules must be a list")
                for requirement in record["required-modules"]:
                    parse_module_requirement(requirement)
                fingerprint = record["requirements-hash"]
                if fingerprint is not None and (
                    not isinstance(fingerprint, str)
                    or not fingerprint.startswith("sha256:")
                    or not _is_sha256(fingerprint.removeprefix("sha256:"))
                ):
                    raise ValueError("invalid locked requirements fingerprint")
                if not _is_sha256(record["source-sha256"]) or not isinstance(
                    record["packages"], list
                ):
                    raise TypeError("invalid locked module")
                for package in record["packages"]:
                    if package not in packages:
                        raise ValueError("module refers to an unlocked package")
                    owned.add(package)
            if owned != packages.keys():
                raise ValueError("unowned packages in lock")
            return data
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            DependencyError,
        ) as exc:
            raise DependencyError(
                "Invalid module lock; run alira lock to regenerate it."
            ) from exc

    @staticmethod
    def atomic_write(path: Path, content: bytes) -> None:
        """Flush bytes to a temporary sibling and atomically replace the destination."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile("wb", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        try:
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def write_declaration(self, data: dict) -> None:
        """Atomically replace declaration JSON inside an active transaction."""
        self.atomic_write(self.path, _json_bytes(data))

    def write_lock(self, data: dict) -> None:
        """Atomically replace generated lock JSON inside an active transaction."""
        self.atomic_write(self.lockfile, _json_bytes(data))

    def snapshot(self) -> dict[Path, bytes | None]:
        """Capture original bytes of both files, using None for missing files."""
        return {
            path: path.read_bytes() if path.exists() else None
            for path in (self.path, self.lockfile)
        }

    def restore(self, snapshot: dict[Path, bytes | None]) -> None:
        """Restore an exact snapshot; preserve files outside the two state paths."""
        for path, content in snapshot.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                self.atomic_write(path, content)

    def _lock_error(self) -> DependencyLockError:
        """Describe the current owner, using lock.info only for diagnostics."""
        message = "another alira command is using this modules directory"
        try:
            info = json.loads(self.lock_info.read_bytes())
            message += (
                f"\n  pid {info['pid']}, host {info['host']}, started {info['started']}, "
                f"operation: {info['operation']}"
            )
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return DependencyLockError(message)

    def _recover(self, *, repair: bool) -> None:
        """Check an interrupted journal and optionally restore its complete snapshot."""
        if not self.journal.exists():
            return
        try:
            journal = json.loads(self.journal.read_bytes())
            if journal.get("host", socket.gethostname()) == socket.gethostname():
                try:
                    os.kill(journal["pid"], 0)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    raise self._lock_error() from None
                else:
                    raise self._lock_error()
            snapshot = {
                path: (
                    base64.b64decode(journal["contents"][path.name], validate=True)
                    if journal["contents"][path.name] is not None
                    else None
                )
                for path in (self.path, self.lockfile)
            }
            changed = self.snapshot() != snapshot
            if changed and not repair:
                raise DependencyError(
                    "A previous module operation was interrupted; run alira sync --repair."
                )
            if changed:
                self.restore(snapshot)
            self.journal.unlink()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise DependencyError(
                "Cannot recover module state; inspect journal.json and run alira lock."
            ) from exc

    def record_source(self, name: str) -> None:
        """Record one published module source in the active operation journal."""
        journal = json.loads(self.journal.read_bytes())
        if name not in journal["installed-sources"]:
            journal["installed-sources"].append(name)
            self.atomic_write(self.journal, _json_bytes(journal))

    @asynccontextmanager
    async def transaction(
        self, operation: str, *, repair: bool = False
    ) -> AsyncIterator[None]:
        """Serialize a dependency operation and restore its files on every failure.

        Args:
            operation: Command name displayed to competing processes.
            repair: Restore a prior interrupted operation's snapshot before starting.

        Raises:
            DependencyLockError: Another process holds the lock for thirty seconds,
                or an interrupted journal still names a live process.
            DependencyError: Recovery requires explicit ``--repair``.
            BaseException: The body failed, including caller cancellation. File
                restoration completes under shield before the original error escapes.

        Package installation is idempotently repaired by sync; published sources
        remain in place. A recovery failure is printed without replacing the body's
        exception or exit code, and leaves the journal available for later repair.
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        lock = FileLock(self.directory / ".lock", timeout=self.lock_timeout)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.lock_timeout
        while True:
            try:
                lock.acquire(timeout=0)
                break
            except Timeout:
                if loop.time() >= deadline:
                    raise self._lock_error() from None
                await asyncio.sleep(min(0.05, max(0, deadline - loop.time())))
        owns_info = False
        try:
            self._recover(repair=repair)
            started = datetime.now(UTC).isoformat()
            owner = {
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "operation": operation,
                "started": started,
            }
            self.atomic_write(self.lock_info, _json_bytes(owner))
            owns_info = True
            snapshot = self.snapshot()
            journal = {
                "op": operation,
                "pid": owner["pid"],
                "host": owner["host"],
                "started": started,
                "snapshot": {
                    path.name: "sha256:" + hashlib.sha256(content).hexdigest()
                    if content is not None
                    else None
                    for path, content in snapshot.items()
                },
                "contents": {
                    path.name: base64.b64encode(content).decode("ascii")
                    if content is not None
                    else None
                    for path, content in snapshot.items()
                },
                "installed-sources": [],
            }
            self.atomic_write(self.journal, _json_bytes(journal))
            try:
                yield
                self.journal.unlink()
            except BaseException:
                restoring = asyncio.create_task(
                    asyncio.to_thread(self.restore, snapshot)
                )
                try:
                    while not restoring.done():
                        try:
                            await asyncio.shield(restoring)
                        except asyncio.CancelledError:
                            continue
                    restoring.result()
                    self.journal.unlink()
                except OSError as exc:
                    print(
                        f"Could not restore module state: {exc}; run alira lock to repair.",
                        file=sys.stderr,
                    )
                raise
        finally:
            try:
                if owns_info:
                    self.lock_info.unlink(missing_ok=True)
            finally:
                lock.release()

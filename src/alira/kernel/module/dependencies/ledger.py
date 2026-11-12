"""Atomic deployment manifests and persistent PEP 751 dependency locks."""

import hashlib
import json
import os
import tomllib
from pathlib import Path
from tempfile import NamedTemporaryFile

from packaging.utils import canonicalize_name
from packaging.version import Version

from alira.kernel.module.dependencies.types import DependencyError, sync_hint


class DependencyLedger:
    """Read and atomically replace dependency records and their PEP 751 lock.

    Attributes:
        directory: Directory containing this deployment's persisted state.
        path: modules.json record path.
        lockfile: pylock.toml artifact path.

    Each file is replaced atomically, but the pair is not a filesystem transaction.
    The JSON lock digest detects interrupted or mismatched updates. Callers recover
    with source-driven sync, which does not rely on old records.
    """

    def __init__(self, directory: Path):
        """Store state paths beneath directory without creating or reading files."""
        self.directory = directory
        self.path = directory / "modules.json"
        self.lockfile = directory / "pylock.toml"

    def read(self) -> dict:
        """Read and validate the JSON/lock pair without mutating it.

        Returns:
            Parsed record dictionary after digest, shape and package-ownership checks.
            If both files are absent, returns a dictionary with an empty modules mapping.

        Raises:
            DependencyError: Either file is missing while the other exists, unreadable,
                malformed or inconsistent. The public message only suggests sync;
                the original storage/parser error is retained as its cause.
        """
        if not self.path.exists() and not self.lockfile.exists():
            return {"modules": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            lock = self.lockfile.read_bytes()
            if data["lock_hash"] != hashlib.sha256(lock).hexdigest():
                raise ValueError("JSON and lock file do not match")
            packages = {
                item["name"]: item["version"]
                for item in tomllib.loads(lock.decode("utf-8")).get("packages", [])
            }
            if not isinstance(data["modules"], dict):
                raise TypeError("invalid module mapping")
            owned = {}
            for name, record in data["modules"].items():
                if not name.isidentifier() or not isinstance(record["packages"], dict):
                    raise ValueError("invalid module record")
                if not isinstance(record["required_modules"], list) or not all(
                    isinstance(item, str) for item in record["required_modules"]
                ):
                    raise ValueError("invalid required_modules")
                if record["requirements"] is not None and not isinstance(
                    record["requirements"], str
                ):
                    raise ValueError("invalid requirements path")
                for package, version in record["packages"].items():
                    canonicalize_name(package, validate=True)
                    Version(version)
                    if packages.get(package) != version:
                        raise ValueError(
                            "module dependencies do not match the lock file"
                        )
                owned.update(record["packages"])
            if owned != packages:
                raise ValueError("unowned packages in the lock file")
            return data
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise DependencyError(
                "Module dependencies need to be synchronized.",
                hint=sync_hint(),
            ) from exc

    @staticmethod
    def atomic_write(path: Path, content: bytes) -> None:
        """Flush bytes to a temporary sibling and atomically replace path.

        Args:
            path: Destination; missing parent directories are created.
            content: Full replacement bytes, not an append fragment.

        Raises:
            OSError: Writing, syncing or replacing failed. Temporary-file cleanup is
                attempted on failure. This provides atomic replacement of one file only.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile("wb", dir=path.parent, delete=False) as file:
            temporary = Path(file.name)
            try:
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        try:
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def write(self, data: dict, lock: bytes) -> None:
        """Replace the lock first, then JSON with its computed lock digest.

        Args:
            data: Complete record dictionary; its lock_hash is replaced in a new dict.
            lock: Complete PEP 751 artifact bytes whose digest is stored in JSON.

        Raises:
            OSError: A write failed. Interruption between writes leaves a pair that
                read rejects; rebuild both from source with sync.
        """
        state = data | {"lock_hash": hashlib.sha256(lock).hexdigest()}
        self.atomic_write(self.lockfile, lock)
        self.atomic_write(
            self.path,
            (json.dumps(state, sort_keys=True, indent=2) + "\n").encode("utf-8"),
        )

    def snapshot(self) -> dict[Path, bytes | None]:
        """Return raw bytes for each state path, using None for an absent file.

        Performs no schema validation. The snapshot supports best-effort rollback even
        when preexisting state is incomplete. File-read OSError exceptions propagate.
        """
        return {
            path: path.read_bytes() if path.exists() else None
            for path in (self.path, self.lockfile)
        }

    def restore(self, snapshot: dict[Path, bytes | None]) -> None:
        """Restore a raw state snapshot, removing files previously absent.

        Args:
            snapshot: Path/bytes-or-None mapping produced by snapshot.

        Raises:
            OSError: A removal or atomic replacement failed. The pair may then require sync.
        """
        for path, content in snapshot.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                self.atomic_write(path, content)

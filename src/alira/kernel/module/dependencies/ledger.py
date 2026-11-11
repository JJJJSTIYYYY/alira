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
    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "modules.json"
        self.lockfile = directory / "pylock.toml"

    def read(self) -> dict:
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
        state = data | {"lock_hash": hashlib.sha256(lock).hexdigest()}
        self.atomic_write(self.lockfile, lock)
        self.atomic_write(
            self.path,
            (json.dumps(state, sort_keys=True, indent=2) + "\n").encode("utf-8"),
        )

    def snapshot(self) -> dict[Path, bytes | None]:
        return {
            path: path.read_bytes() if path.exists() else None
            for path in (self.path, self.lockfile)
        }

    def restore(self, snapshot: dict[Path, bytes | None]) -> None:
        for path, content in snapshot.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                self.atomic_write(path, content)

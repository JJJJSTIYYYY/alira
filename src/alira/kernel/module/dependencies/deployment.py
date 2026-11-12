"""Rebuild dependency records from sources and synchronize the runtime environment."""

import csv
import importlib
import io
import os
import re
import tomllib
from collections.abc import AsyncIterator, Collection, Mapping
from contextlib import asynccontextmanager
from importlib.metadata import distributions
from pathlib import Path
from tempfile import TemporaryDirectory

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

from alira.kernel.module.dependencies.ledger import DependencyLedger
from alira.kernel.module.dependencies.requirements import requirements_fingerprint
from alira.kernel.module.dependencies.types import (
    DependencyError,
    DependencyInfo,
    DependencyResolutionError,
    sync_hint,
)
from alira.kernel.module.dependencies.uv import EMPTY_LOCK, resolve_requirements, run_uv


def _installed_distributions():
    """Incomplete installation metadata must not prevent source-driven repair."""
    installed, incomplete = {}, set()
    for dist in distributions():
        name = dist.metadata["Name"]
        name = canonicalize_name(name) if name else None
        if name is None or not dist.version:
            incomplete.add(name)
        else:
            installed[name] = dist
    return installed, incomplete


def kernel_packages() -> dict[str, str]:
    """Protect Alira's runtime closure and uv when installed in this interpreter.

    Ownership JSON never decides which packages belong to the kernel.
    """
    installed, _ = _installed_distributions()
    protected = {}

    def visit(name):
        if name in protected:
            return
        dist = installed[name]
        protected[name] = dist.version
        for text in dist.requires or ():
            requirement = Requirement(text)
            if requirement.marker is None or requirement.marker.evaluate({"extra": ""}):
                visit(canonicalize_name(requirement.name))

    visit("alira-harness")
    if "uv" in installed:
        visit("uv")
    return protected


def filter_lock(lock: bytes, keep: Collection[str]) -> bytes:
    """Keep complete PEP 751 package entries, including artifact URLs and hashes."""
    parts = re.split(rb"(?m)^(?=\[\[packages\]\]$)", lock)
    blocks = []
    for entry in parts[1:]:
        name = tomllib.loads(entry.decode("utf-8"))["packages"][0]["name"]
        if name in keep:
            blocks.append(entry)
    return parts[0] + b"".join(blocks) if blocks else EMPTY_LOCK


def _release_kernel_file_claims(
    removable: Collection[str], protected: Collection[str]
) -> None:
    """Prevent other distributions' uninstall records from deleting kernel files.

    Distribution names can differ while RECORD lists the same console script
    or Python file. Release those claims before invoking uv, so interruption
    during uninstall cannot remove files still owned by the kernel.
    """
    providers = [
        (canonicalize_name(name), dist)
        for dist in distributions()
        if (name := dist.metadata["Name"])
    ]
    owned = {
        os.path.abspath(dist.locate_file(file))
        for name, dist in providers
        if name in protected
        for file in dist.files or ()
    }
    for name, dist in providers:
        if name not in removable:
            continue
        record = dist.read_text("RECORD")
        if record is None:
            continue
        rows = list(csv.reader(io.StringIO(record)))
        retained = [
            row
            for row in rows
            if os.path.abspath(dist.locate_file(row[0])) not in owned
        ]
        if len(retained) == len(rows):
            continue
        record_path = next(
            dist.locate_file(file)
            for file in dist.files
            if file.name == "RECORD" and file.parent.name.endswith(".dist-info")
        )
        output = io.StringIO(newline="")
        csv.writer(output).writerows(retained)
        DependencyLedger.atomic_write(record_path, output.getvalue().encode("utf-8"))


class ModuleDependencies:
    def __init__(self, state_dir: Path):
        self.ledger = DependencyLedger(state_dir)
        self.claims: dict[str, dict[str, str]] = {}
        self.requirement_hashes = {}
        self.required_modules = {}

    @staticmethod
    def installed() -> dict[str, str]:
        return {
            name: dist.version for name, dist in _installed_distributions()[0].items()
        }

    def restore(self) -> dict:
        state = self.ledger.read()
        self.claims = {
            name: record["packages"] for name, record in state["modules"].items()
        }
        self.requirement_hashes = {
            name: record.get("requirements_hash")
            for name, record in state["modules"].items()
        }
        self.required_modules = {
            name: record["required_modules"]
            for name, record in state["modules"].items()
        }
        return state

    @property
    def info(self) -> dict[str, DependencyInfo]:
        self.restore()
        owners = {}
        versions = {}
        for name, packages in self.claims.items():
            for package, version in packages.items():
                owners.setdefault(package, set()).add(name)
                versions[package] = version
        protected = kernel_packages()
        return {
            name: DependencyInfo(
                name, versions[name], frozenset(modules), name in protected
            )
            for name, modules in owners.items()
        }

    def verify(self, name: str, path: Path | None, installed: dict[str, str]) -> None:
        if path is None:
            return
        fingerprint = requirements_fingerprint(path)
        if name not in self.claims or self.requirement_hashes[name] != fingerprint:
            raise DependencyError(
                "dependencies are not deployed or requirements changed",
                module=name,
                hint=sync_hint(),
            )
        for package, expected in self.claims[name].items():
            actual = installed.get(package)
            if actual is None or Version(actual) != Version(expected):
                reason = (
                    f"missing dependency {package}=={expected}"
                    if actual is None
                    else f"dependency {package}=={expected} required, installed {actual}"
                )
                raise DependencyError(
                    reason, module=name, missing=package, hint=sync_hint()
                )

    async def rebuild(
        self,
        specifications: Mapping,
        *,
        cwd: Path,
        no_install: bool = False,
        background: bool = False,
        prune: bool = False,
    ) -> None:
        """Resolve one complete desired management set without reading old locks."""
        protected = kernel_packages()
        sources = {name: spec.requirements for name, spec in specifications.items()}
        with TemporaryDirectory(prefix="alira-resolve-") as temporary:
            directory = Path(temporary)
            try:
                fingerprints = {
                    name: requirements_fingerprint(path)
                    for name, path in sources.items()
                }
                plans = await resolve_requirements(sources, protected, directory, cwd)
            except Exception as exc:
                raise DependencyResolutionError(
                    f"dependency resolution failed: {exc}", hint=sync_hint()
                ) from exc
            lock = (directory / "combined" / "pylock.toml").read_bytes()
            records = {
                name: {
                    "packages": plan.packages,
                    "requirements": str(sources[name])
                    if sources[name] is not None
                    else None,
                    "requirements_hash": fingerprints[name],
                    "required_modules": list(specifications[name].required_modules),
                }
                for name, plan in plans.items()
            }
            self.ledger.write({"format": 1, "modules": records}, lock)
        self.restore()
        if not no_install:
            desired = {
                package: version
                for packages in self.claims.values()
                for package, version in packages.items()
            }
            await self._install_locked(
                lock,
                {
                    name: version
                    for name, version in desired.items()
                    if name not in protected
                },
                cwd=cwd,
                background=background,
            )
            if prune:
                await self.uninstall_packages(
                    self.installed().keys() - desired.keys() - protected.keys(),
                    cwd=cwd,
                    background=background,
                )

    async def _install_locked(
        self, lock: bytes, packages: Mapping[str, str], *, cwd: Path, background: bool
    ) -> None:
        if not packages:
            return
        installed, incomplete = _installed_distributions()
        # uv can trust a dist-info directory's version even when METADATA is
        # incomplete. Force repair; a nameless fragment cannot identify which
        # managed package needs reinstallation, so repair the selected set.
        reinstall = (
            set(packages) if None in incomplete else packages.keys() & incomplete
        )
        reinstall.update(
            name
            for name, version in packages.items()
            if name in installed and installed[name].version != version
        )
        options = [
            argument
            for name in sorted(reinstall)
            for argument in ("--reinstall-package", name)
        ]
        with TemporaryDirectory(prefix="alira-install-deps-") as temporary:
            install_lock = Path(temporary) / "pylock.toml"
            install_lock.write_bytes(filter_lock(lock, packages))
            await run_uv(
                "install",
                "--requirements",
                str(install_lock),
                "--no-deps",
                *options,
                cwd=cwd,
                capture=background,
            )

    async def add(
        self,
        selected: Mapping,
        *,
        cwd: Path,
        no_install: bool = False,
        background: bool = False,
    ) -> None:
        from alira.kernel.module.catalog import ModuleSpec

        state = self.ledger.read()
        specifications = {
            name: ModuleSpec(
                name,
                cwd / name,
                Path(record["requirements"])
                if record["requirements"] is not None
                else None,
                tuple(record["required_modules"]),
            )
            for name, record in state["modules"].items()
            if name not in selected
        }
        specifications.update(selected)
        await self.rebuild(
            specifications, cwd=cwd, no_install=no_install, background=background
        )

    async def uninstall_packages(
        self, names: Collection[str], *, cwd: Path, background: bool = False
    ) -> None:
        protected = kernel_packages()
        removable = sorted(set(names) - protected.keys())
        if removable:
            _release_kernel_file_claims(removable, protected)
            await run_uv("uninstall", *removable, cwd=cwd, capture=background)
        importlib.invalidate_caches()

    async def remove(
        self, names: Collection[str], *, cwd: Path, background: bool = False
    ) -> None:
        state = self.ledger.read()
        records = state["modules"]
        selected = set(names)
        unknown = selected - records.keys()
        if unknown:
            raise DependencyError(
                f"Modules are not in dependency management records: {', '.join(sorted(unknown))}"
            )
        dependents = {
            name
            for name, record in records.items()
            if name not in selected
            and selected.intersection(record["required_modules"])
        }
        if dependents:
            raise DependencyError(
                f"Modules are required by: {', '.join(sorted(dependents))}"
            )
        retained = {
            name: record for name, record in records.items() if name not in selected
        }
        keep = {
            package for record in retained.values() for package in record["packages"]
        }
        old = {package for name in selected for package in records[name]["packages"]}
        lock = filter_lock(self.ledger.lockfile.read_bytes(), keep)
        self.ledger.write({"format": 1, "modules": retained}, lock)
        self.restore()
        await self.uninstall_packages(old - keep, cwd=cwd, background=background)

    @asynccontextmanager
    async def transaction(
        self, cwd: Path, *, background: bool = False
    ) -> AsyncIterator[None]:
        """Best-effort source-operation rollback, preserving the original failure."""
        snapshot = self.ledger.snapshot()
        before = self.installed()
        try:
            yield
        except BaseException as failure:
            errors = []
            try:
                after = self.installed()
                changed = {
                    name: version
                    for name, version in before.items()
                    if after.get(name) != version
                }
                previous_lock = snapshot[self.ledger.lockfile]
                if changed and previous_lock is not None:
                    locked = {
                        item["name"]: item["version"]
                        for item in tomllib.loads(previous_lock.decode("utf-8")).get(
                            "packages", []
                        )
                        if changed.get(item["name"]) == item["version"]
                    }
                    await self._install_locked(
                        previous_lock, locked, cwd=cwd, background=background
                    )
                    changed = {
                        name: version
                        for name, version in changed.items()
                        if name not in locked
                    }
                if changed:
                    await run_uv(
                        "install",
                        "--no-deps",
                        *(f"{name}=={version}" for name, version in changed.items()),
                        cwd=cwd,
                        capture=background,
                    )
            except Exception as exc:  # noqa: BLE001 - continue independent rollback steps
                errors.append(exc)
            try:
                await self.uninstall_packages(
                    self.installed().keys() - before.keys(),
                    cwd=cwd,
                    background=background,
                )
            except Exception as exc:  # noqa: BLE001 - report rollback failures alongside the original error
                errors.append(exc)
            try:
                self.ledger.restore(snapshot)
            except Exception as exc:  # noqa: BLE001 - report rollback failures alongside the original error
                errors.append(exc)
            if errors:
                raise BaseExceptionGroup(
                    "Module operation failed and rollback was incomplete; run uv run alira sync",
                    [failure, *errors],
                ) from None
            raise

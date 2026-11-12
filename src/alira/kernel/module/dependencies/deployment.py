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
    """Return complete distributions by canonical name and incomplete metadata names.

    A nameless fragment contributes None to the incomplete set so repair can force
    reinstall of the selected managed package set after interrupted installation.
    """
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
    """Read and pin the installed Alira runtime closure independently of user records.

    Returns:
        Canonical distribution names mapped to installed versions, starting at
        alira-harness and following applicable non-extra requirements. If uv is
        installed in this interpreter, its closure is included too.

    Raises:
        KeyError: Required kernel distribution metadata is absent. A working Alira
            installation is a precondition; editable ownership records cannot
            authorize changing or removing kernel packages.
    """
    installed, _ = _installed_distributions()
    protected = {}

    def visit(name):
        """Collect one installed distribution and its applicable runtime requirements once."""
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
    """Retain complete PEP 751 package blocks for a selected set of names.

    Args:
        lock: Resolver-generated full lock bytes with [[packages]] blocks.
        keep: Canonical distribution names to retain, without resolving dependencies.

    Returns:
        Filtered lock bytes preserving artifact URLs and hashes. Empty selections
        produce the standard empty lock. Callers must supply an already closed set.
    """
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
    """Implement dependency records, resolution and current-interpreter package updates.

    This lower-level component does not discover sources, manage module lifecycles
    or serialize clients. ModuleManager supplies validated specifications and holds
    its operation lock. Claims reflect persisted desired state, not proof of a
    completed environment installation.

    Attributes:
        ledger: Persistent deployment record/lock owner.
        claims: Restored module-to-package/version ownership map.
        requirement_hashes: Restored per-module input fingerprints.
        required_modules: Restored per-module prerequisite names.
    """

    def __init__(self, state_dir: Path):
        """Create empty in-memory ownership maps and a ledger rooted at state_dir; perform no I/O."""
        self.ledger = DependencyLedger(state_dir)
        self.claims: dict[str, dict[str, str]] = {}
        self.requirement_hashes = {}
        self.required_modules = {}

    @staticmethod
    def installed() -> dict[str, str]:
        """Return canonical distribution/version metadata for the current interpreter.

        Incomplete or nameless metadata fragments are omitted. This is not an audit
        that every installed package file is intact.
        """
        return {
            name: dist.version for name, dist in _installed_distributions()[0].items()
        }

    def restore(self) -> dict:
        """Read validated ledger state and replace all in-memory ownership maps.

        Returns:
            The validated record dictionary. Missing state initializes empty ownership.

        Raises:
            DependencyError: Existing records are damaged or inconsistent; run sync.
        """
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
        """Read ownership and return new per-package DependencyInfo summaries.

        The protected flag is computed from installed kernel metadata, not user-edited
        records. Ledger validation failures propagate as DependencyError.
        """
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
        """Verify one module's requirements fingerprint and locked installed versions offline.

        Args:
            name: Module whose restored claim and fingerprint should be verified.
            path: Requirements file, or None to skip deployment verification entirely.
            installed: Canonical distribution/version snapshot of the target interpreter.

        Raises:
            DependencyError: Requirements changed, a claim is absent or a dependency is
                missing/incompatible. Includes module ownership and the sync repair hint.

        Notes:
            The caller must restore claims first when a requirements path exists.
            This method reads local requirement includes but never installs or resolves.
        """
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
        """Resolve a complete desired source set and replace its deployment records.

        Args:
            specifications: Name-to-ModuleSpec mapping with valid prerequisite metadata.
            cwd: Working directory for combined uv resolution and package operations.
            no_install: Write resolved records/lock only; leave Python packages alone.
            background: Capture uv install/remove output instead of inheriting the terminal.
            prune: After installation, remove packages outside desired and protected sets.
                Has no environment effect when no_install is true.

        Raises:
            DependencyResolutionError: Input fingerprinting or solving failed before
                replacing records; kernel versions are pinned throughout resolution.
            DependencyError: Installation/removal failed or new records failed validation.
            OSError: Working-file or ledger I/O failed.

        Notes:
            Does not read old locks; it is the source-driven recovery path. Writes
            records before installing, so a successful write does not certify installed
            packages. Rebuild again from source after interruption. Never changes module
            source files and never removes distributions in the kernel closure.
        """
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
        """Install the selected package/version mapping from full lock artifact blocks.

        Reinstall mismatched or incomplete metadata, including the whole selected set
        when a nameless fragment cannot identify its owner. The caller excludes kernel
        packages and supplies a closed, already resolved set; uv is run with --no-deps.
        """
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
        """Merge selected source specifications with other recorded requirements and rebuild.

        Args:
            selected: Validated selected module/prerequisite name-to-spec mapping.
            cwd: Module source root and working directory for package operations.
            no_install: Update records/lock without installing or updating packages.
            background: Capture uv progress while awaiting completion.

        Raises:
            DependencyError: Existing records require repair or environment update failed.
            DependencyResolutionError: The merged requirements cannot be resolved.

        Notes:
            Retained modules use their recorded paths/metadata, without a full source
            scan. No unrelated packages are pruned and no source files are changed.
        """
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
        """Uninstall selected distributions while protecting the current kernel closure.

        Args:
            names: Canonical distribution names eligible for removal.
            cwd: Existing uv working directory.
            background: Capture uv output rather than stream it to the terminal.

        Raises:
            DependencyError: uv could not launch or remove a package.
            OSError: Installed ownership metadata could not be adjusted safely.

        Notes:
            Removes other distributions' claims on kernel-owned files before invoking
            uv, preventing obsolete/shared console-script ownership from deleting the
            active kernel. Does not change module claims or source directories.
        """
        protected = kernel_packages()
        removable = sorted(set(names) - protected.keys())
        if removable:
            _release_kernel_file_claims(removable, protected)
            await run_uv("uninstall", *removable, cwd=cwd, capture=background)
        importlib.invalidate_caches()

    async def remove(
        self, names: Collection[str], *, cwd: Path, background: bool = False
    ) -> None:
        """Drop recorded module claims and uninstall only packages no retained owner needs.

        Args:
            names: Module names currently present in validated ownership records.
            cwd: uv working directory.
            background: Capture uv progress while awaiting removal.

        Raises:
            DependencyError: A name is unrecorded, records are invalid, a retained module
                requires a removed module or package uninstall fails.
            OSError: Record, lock or package metadata I/O failed.

        Notes:
            Kernel packages remain protected. Writes retained records/lock before
            uninstall and does not roll them back on failure. Source files remain;
            full source-driven sync can repair or re-include removed claims.
        """
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
        """Snapshot dependency state and attempt recovery if a source operation fails.

        Args:
            cwd: Working directory for rollback package operations.
            background: Capture rollback uv output instead of streaming it.

        Yields:
            None after capturing raw ledger bytes and installed package versions.

        Raises:
            BaseException: The original operation failed and rollback completed.
            BaseExceptionGroup: Original and recovery failures when rollback is incomplete.

        Notes:
            Prefers original lock artifact URLs/hashes for changed versions; other
            prior versions use exact version requirements. Attempts new-package removal
            and raw record restoration even when version recovery fails. This is best
            effort, not process-crash durability; source backups are the caller's job.
            No-name/incomplete metadata or arbitrary package file damage is not a full
            environment snapshot. Use sync after incomplete recovery.
        """
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

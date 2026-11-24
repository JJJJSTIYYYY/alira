"""Rebuild dependency records from sources and synchronize the runtime environment."""

import csv
import hashlib
import io
import json
import os
import platform
import tomllib
from collections.abc import Collection, Mapping
from importlib.metadata import distributions
from pathlib import Path
from tempfile import TemporaryDirectory

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version

from alira._version import __version__
from alira.kernel.module.dependencies.ledger import DependencyLedger, inputs_hash
from alira.kernel.module.dependencies.requirements import (
    requirements_fingerprint,
    requirements_hash,
)
from alira.kernel.module.dependencies.types import (
    DependencyError,
    DependencyInfo,
    DependencyInstallError,
    DependencyResolutionError,
    sync_hint,
)
from alira.kernel.module.dependencies.uv import (
    RESOLVER_VERSION,
    complete_local_artifacts,
    installation_lock,
    resolve_requirements,
    run_uv,
)
from alira.kernel.module.discovery.resolution import resolve_module_graph
from alira.kernel.module.sources.archive import source_hash


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


def _artifact_digest(package: dict) -> str:
    """Validate all locked archives and fingerprint their exact artifact metadata."""
    artifacts = [*package.get("wheels", [])]
    if "sdist" in package:
        artifacts.append(package["sdist"])
    if not artifacts:
        raise DependencyError(
            f"Dependency {package['name']} has no hashed distribution artifact"
        )
    for artifact in artifacts:
        digest = artifact.get("hashes", {}).get("sha256", "")
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise DependencyError(
                f"Dependency {package['name']} has no valid SHA-256 artifact hash"
            )
        if not (artifact.get("url") or artifact.get("path")):
            raise DependencyError(
                f"Dependency {package['name']} has no artifact location"
            )
    return hashlib.sha256(
        json.dumps(artifacts, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class ModuleDependencies:
    """Resolve declarations and install authoritative locks into the current interpreter.

    Resolution has no persistent or environment effects. Installation consumes only
    a lock, never requirements files. The manager owns declaration updates, file
    locking and rollback; partial package installations converge on a later sync.
    """

    def __init__(self, state_dir: Path):
        """Store state paths and initialize empty ownership maps without I/O."""
        self.ledger = DependencyLedger(state_dir)
        self.claims: dict[str, dict[str, str]] = {}
        self.requirement_hashes: dict[str, str | None] = {}

    @staticmethod
    def installed() -> dict[str, str]:
        """Read complete canonical distribution/version metadata in this interpreter."""
        return {
            name: dist.version for name, dist in _installed_distributions()[0].items()
        }

    def restore(self) -> dict:
        """Restore module ownership from the JSON lock and return the declaration.

        A missing lock yields no deployed claims. The declaration remains the sole
        source of desired module membership; this method does not require freshness.
        """
        declaration = self.ledger.read()
        lock = self.ledger.read_lock()
        self.claims = (
            {}
            if lock is None
            else {
                name: {
                    package: lock["packages"][package]["version"]
                    for package in record["packages"]
                }
                for name, record in lock["modules"].items()
            }
        )
        self.requirement_hashes = (
            {}
            if lock is None
            else {
                name: record.get("requirements-hash")
                for name, record in lock["modules"].items()
            }
        )
        return declaration

    @property
    def info(self) -> dict[str, DependencyInfo]:
        """Return package versions and owners recorded in the lock, excluding the kernel."""
        self.restore()
        owners, versions = {}, {}
        for name, packages in self.claims.items():
            for package, version in packages.items():
                owners.setdefault(package, set()).add(name)
                versions[package] = version
        return {
            name: DependencyInfo(name, versions[name], frozenset(modules), False)
            for name, modules in owners.items()
        }

    def verify(self, name: str, path: Path | None, installed: dict[str, str]) -> None:
        """Check a loaded module's locked requirements and package versions offline.

        Call ``restore`` first. Missing or changed declarations and missing packages
        raise DependencyError with the owning module and a concrete sync command.
        Modules without a requirements file need no Python-package deployment.
        """
        if path is None:
            return
        if name not in self.claims or self.requirement_hashes[name] != requirements_hash(
            path
        ):
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

    def lock_is_current(
        self,
        declaration: dict,
        specifications: Mapping,
        *,
        lock: dict | None = None,
        allow_missing_sources: bool = False,
    ) -> bool:
        """Check input identity, static declarations and source fingerprints without uv.

        Missing locks or sources are stale unless ``allow_missing_sources`` permits
        a partial specification mapping while checking every declaration against the
        locked metadata. Existing sources still receive all physical checks. This
        lets locked sync restore missing sources after verifying input identity.
        No state files or directories are created.
        """
        lock = self.ledger.read_lock() if lock is None else lock
        if not SpecifierSet(declaration["requires-kernel"]).contains(
            Version(__version__)
        ):
            return False
        if lock is None or lock["inputs-hash"] != inputs_hash(
            declaration, resolver_version=RESOLVER_VERSION
        ):
            return False
        names = declaration["modules"].keys()
        if names != lock["modules"].keys() or not specifications.keys() <= names:
            return False
        if not allow_missing_sources and specifications.keys() != names:
            return False
        for name, record in declaration["modules"].items():
            locked = lock["modules"][name]
            if (
                record["version"] != locked["version"]
                or record["required-modules"] != locked["required-modules"]
                or record.get("requirements-hash") != locked.get("requirements-hash")
            ):
                return False
            spec = specifications.get(name)
            if spec is None:
                continue
            if (
                record["version"] != spec.version
                or record["required-modules"] != list(spec.required_modules)
                or record.get("requirements-hash") != requirements_hash(spec.requirements)
                or not spec.directory.is_dir()
                or source_hash(spec.directory) != locked["source-sha256"]
            ):
                return False
        return True

    async def resolve(
        self, declaration: dict, specifications: Mapping, *, cwd: Path
    ) -> dict:
        """Resolve all declared modules into an exact, hashed JSON lock in memory.

        Args:
            declaration: Validated v2 declaration, including current requirement hashes.
            specifications: Complete source closure corresponding to the declaration.
            cwd: Existing module source root for uv resolution.

        Returns:
            JSON-compatible lock with transitive package ownership and full PEP 751
            artifacts. Kernel packages are constrained during resolution and omitted
            from managed packages. Existing locked versions are preferences, allowing
            upgrades required by changed constraints.

        Raises:
            DependencyResolutionError: A graph, fingerprint or Python dependency cannot
                be resolved. No persistent files or installed packages have changed.
        """
        try:
            if not SpecifierSet(declaration["requires-kernel"]).contains(
                Version(__version__)
            ):
                raise DependencyError(
                    f"Project requires kernel{declaration['requires-kernel']}, current kernel is {__version__}"
                )
            resolve_module_graph(tuple(specifications), specifications.__getitem__)
            protected = kernel_packages()
            try:
                previous = self.ledger.read_lock()
            except DependencyError:
                # A damaged old lock cannot supply version preferences. The new
                # resolution is the explicit repair path.
                previous = None
            if previous is not None and self.lock_is_current(
                declaration, specifications, lock=previous
            ):
                return previous
            sources = {name: spec.requirements for name, spec in specifications.items()}
            with TemporaryDirectory(prefix="alira-resolve-") as temporary:
                directory = Path(temporary)
                plans = await resolve_requirements(
                    sources, protected, directory, cwd, previous_lock=previous
                )
                resolved = tomllib.loads(
                    (directory / "combined" / "pylock.toml").read_text()
                )
            return self._build_lock(
                declaration,
                specifications,
                resolved,
                {name: plan.packages for name, plan in plans.items()},
                protected,
            )
        except (DependencyError, OSError) as exc:
            raise DependencyResolutionError(
                f"dependency resolution failed: {exc}", hint=sync_hint()
            ) from exc

    def _build_lock(
        self,
        declaration: dict,
        specifications: Mapping,
        resolved: dict,
        ownership: Mapping,
        protected: Mapping,
    ) -> dict:
        """Convert resolved PEP 751 artifacts and module closures into one JSON lock."""
        packages = {}
        for original in resolved.get("packages", []):
            name = canonicalize_name(original["name"])
            if name in protected:
                continue
            package = original | {"name": name}
            packages[name] = package | {"sha256": _artifact_digest(package)}
        modules = {
            name: {
                "version": spec.version,
                "source": declaration["modules"][name]["source"],
                "source-sha256": source_hash(spec.directory),
                "required-modules": list(spec.required_modules),
                "requirements-hash": requirements_hash(spec.requirements),
                "packages": sorted(set(ownership[name]) - protected.keys()),
            }
            for name, spec in specifications.items()
        }
        return {
            "format": 1,
            "generated-by": f"alira {__version__}",
            "inputs-hash": inputs_hash(declaration, resolver_version=RESOLVER_VERSION),
            "resolver-version": RESOLVER_VERSION,
            "kernel": {"version": __version__, "python": platform.python_version()},
            "modules": modules,
            "packages": packages,
            "install-metadata": {
                key: value for key, value in resolved.items() if key != "packages"
            },
        }

    def migrate_lock(
        self,
        declaration: dict,
        specifications: Mapping,
        legacy_records: dict,
        legacy_lock: bytes,
        *,
        legacy_directory: Path | None = None,
    ) -> dict:
        """Convert a verified legacy ledger without resolving or downloading packages.

        The caller verifies the legacy lock digest before invoking this method.
        Exact artifact URLs/hashes and per-module transitive ownership are retained;
        installed kernel distributions remain excluded from managed package data.
        """
        try:
            resolved = tomllib.loads(legacy_lock.decode("utf-8"))
            versions = {
                canonicalize_name(item["name"]): item["version"]
                for item in resolved.get("packages", [])
            }
            protected = kernel_packages()
            for name in versions.keys() & protected.keys():
                if Version(versions[name]) != Version(protected[name]):
                    raise DependencyError(
                        f"Legacy lock requires {name}=={versions[name]}, but the current kernel "
                        f"uses {protected[name]}; run alira add to resolve current dependencies"
                    )
            resolved["packages"] = [
                package
                for package in resolved.get("packages", [])
                if canonicalize_name(package["name"]) not in protected
            ]
            complete_local_artifacts(
                resolved, legacy_directory or self.ledger.directory
            )
            records = legacy_records.get("modules", legacy_records)
            for name, spec in specifications.items():
                record = records[name]
                if (
                    record.get("requirements_hash")
                    != requirements_fingerprint(spec.requirements)
                    or ("version" in record and record["version"] != spec.version)
                    or (
                        "required_modules" in record
                        and record["required_modules"] != list(spec.required_modules)
                    )
                ):
                    raise DependencyError(
                        f"Legacy inputs changed for module {name}; restore the original inputs "
                        f"before migration or run alira add {name} to resolve current dependencies"
                    )
            ownership = {name: records[name]["packages"] for name in specifications}
            for name, packages in ownership.items():
                for package, version in packages.items():
                    if versions.get(package) != version:
                        raise DependencyError(
                            f"Legacy package ownership does not match lock: {name}/{package}"
                        )
            return self._build_lock(
                declaration, specifications, resolved, ownership, protected
            )
        except (
            ValueError,
            KeyError,
            TypeError,
            UnicodeError,
            OSError,
            DependencyError,
        ) as exc:
            raise DependencyResolutionError(
                f"Cannot migrate legacy dependency lock: {exc}"
            ) from exc

    async def install(
        self, lock: dict, *, cwd: Path, background: bool = False, prune: bool = True
    ) -> None:
        """Install exact lock artifacts and optionally remove all unneeded packages.

        Args:
            lock: Authoritative JSON lock; declarations and requirements are not read.
            cwd: Existing module source root used as the uv process directory.
            background: Capture installer output while awaiting completion.
            prune: Remove every package outside managed and kernel sets. ``run`` uses
                False to preserve unrelated packages; ``sync`` uses True.

        Raises:
            DependencyInstallError: Artifact hashes, kernel identity or installation
                fail. Package changes may be partial; repeating sync repairs them.
            asyncio.CancelledError: The uv child is killed and reaped, then propagated.

        PEP 751 is generated in a temporary directory and removed on exit. Kernel
        packages and their installed files are protected independently of lock data.
        """
        try:
            protected = kernel_packages()
            if lock["kernel"] != {
                "version": __version__,
                "python": platform.python_version(),
            }:
                raise DependencyError(
                    "Lock kernel or Python version does not match this interpreter; run alira lock"
                )
            packages = {
                name: package
                for name, package in lock["packages"].items()
                if name not in protected
            }
            for package in packages.values():
                if package["sha256"] != _artifact_digest(package):
                    raise DependencyError(
                        f"Dependency artifact fingerprint does not match: {package['name']}"
                    )
            if packages:
                installed, incomplete = _installed_distributions()
                reinstall = (
                    set(packages)
                    if None in incomplete
                    else packages.keys() & incomplete
                )
                reinstall.update(
                    name
                    for name, package in packages.items()
                    if name in installed
                    and installed[name].version != package["version"]
                )
                options = [
                    argument
                    for name in sorted(reinstall)
                    for argument in ("--reinstall-package", name)
                ]
                with TemporaryDirectory(prefix="alira-install-deps-") as temporary:
                    artifact = Path(temporary) / "pylock.toml"
                    artifact.write_bytes(installation_lock(lock, packages))
                    await run_uv(
                        "install",
                        "--requirements",
                        str(artifact),
                        "--no-deps",
                        *options,
                        cwd=cwd,
                        capture=background,
                    )
            if prune:
                await self.uninstall_packages(
                    self.installed().keys() - packages.keys() - protected.keys(),
                    cwd=cwd,
                    background=background,
                )
        except (DependencyError, OSError) as exc:
            raise DependencyInstallError(
                f"dependency installation failed: {exc}", hint=sync_hint()
            ) from exc

    async def uninstall_packages(
        self, names: Collection[str], *, cwd: Path, background: bool = False
    ) -> None:
        """Remove distributions while releasing claims on kernel-owned files first."""
        protected = kernel_packages()
        removable = sorted(set(names) - protected.keys())
        if removable:
            try:
                _release_kernel_file_claims(removable, protected)
                await run_uv("uninstall", *removable, cwd=cwd, capture=background)
            except (DependencyError, OSError) as exc:
                raise DependencyInstallError(
                    f"dependency removal failed: {exc}", hint=sync_hint()
                ) from exc

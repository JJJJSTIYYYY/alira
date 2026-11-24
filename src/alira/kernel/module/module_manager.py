"""Public module manager: serialize source, dependency and runtime operations."""

import asyncio
import hashlib
import json
import os
import platform
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import ClassVar, Self

from alira._version import __version__
from alira.kernel.module.base_module import BaseModule, ModuleStatus
from alira.kernel.module.dependencies.deployment import ModuleDependencies
from alira.kernel.module.dependencies.ledger import DependencyLedger
from alira.kernel.module.dependencies.requirements import requirements_hash
from alira.kernel.module.dependencies.types import (
    DependencyError,
    DependencyInfo,
    ModuleLoadError,
)
from alira.kernel.module.discovery.catalog import (
    ModuleCatalog,
    ModuleSpec,
    module_directory,
)
from alira.kernel.module.discovery.resolution import parse_module_requirement
from alira.kernel.module.runtime.lifecycle import ModuleRuntime
from alira.kernel.module.runtime.registry import ModuleRegistry
from alira.kernel.module.sources.archive import (
    publish_source,
    uninstall_sources,
)
from alira.kernel.module.sources.package import read_package
from alira.kernel.module.sources.scaffold import initialize_module


class ModuleManager:
    """Own module discovery, dependency management and application lifecycle.

    Sources belong to the checkout (or ALIRA_MODULES_DIR); declarations and the
    reproducible lock live in a ~/.alira project namespace. Mutations use both
    an event-loop lock and a process lock. Runtime hooks never hold a process lock.
    Constructing the singleton performs no filesystem writes or business imports.
    """

    _instance: ClassVar[Self | None] = None

    def __new__(cls) -> Self:
        """Return the process singleton, allocating it on the first call."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        """Initialize registry, dependency state and runtime bookkeeping once.

        ALIRA_MODULES_DIR may override checkout source discovery. Dependency
        state always belongs to ~/.alira/projects/<source-root-hash>.

        Notes:
            Construction creates no lifecycle registrations, imports no business
            entries and performs no dependency-state reads or environment changes.
        """
        if getattr(self, "_initialized", False):
            return
        override = os.environ.get("ALIRA_MODULES_DIR")
        if override:
            root = Path(override).expanduser().resolve()
        else:
            cwd = Path.cwd()
            root = next(
                (
                    candidate / "src/alira/modules"
                    for candidate in (cwd, *cwd.parents)
                    if (candidate / "src/alira/modules").is_dir()
                ),
                cwd / "src/alira/modules",
            ).resolve()
        self._home = Path.home() / ".alira"
        self._registry = ModuleRegistry(root, "alira.modules")
        self._dependencies = ModuleDependencies(self._state_directory(root))
        self._runtime = ModuleRuntime(self._registry)
        self._lock = asyncio.Lock()
        self._initialized = True

    @classmethod
    def _new_for_tests(cls) -> Self:
        """Create an isolated manager without replacing the singleton."""
        instance = object.__new__(cls)
        instance.__init__()
        return instance

    @property
    def modules_dir(self) -> Path:
        """Return the root directory used for source inspection and runtime discovery.

        Changing this root does not clear existing registry or loaded-module state.
        """
        return self._registry.directory

    @modules_dir.setter
    def modules_dir(self, directory: Path) -> None:
        """Set the module source root without moving files or resetting runtime state."""
        root = Path(directory).expanduser().resolve()
        self._registry.directory = root
        self._dependencies.ledger = DependencyLedger(self._state_directory(root))

    @property
    def package(self) -> str:
        """Return the Python package prefix used to import module main entries."""
        return self._registry.package

    @package.setter
    def package(self, package: str) -> None:
        """Set the discovery import prefix without importing or clearing existing entries."""
        self._registry.package = package

    @property
    def modules(self) -> Mapping[str, BaseModule]:
        """Return a live, read-only mapping of registered names to BaseModule instances.

        The mapping prevents key mutation; contained module instances remain mutable.
        Reading it does not discover additional modules.
        """
        return MappingProxyType(self._registry.modules)

    @property
    def unavailable(self) -> frozenset[str]:
        """Return entry names without initialized instances during discovery."""
        return frozenset(self._registry.unavailable)

    @property
    def loaded(self) -> frozenset[str]:
        """Return names whose startup succeeded and whose shutdown has not finished."""
        return frozenset(self._runtime.loaded)

    @property
    def deployments(self) -> frozenset[str]:
        """Read and return the set of modules recorded for dependency management.

        Returns:
            Immutable snapshot of recorded module names. Records may describe an
            interrupted installation and do not prove that packages are installed.

        Raises:
            DependencyError: Existing records are incomplete or inconsistent; run sync.
        """
        return frozenset(self._dependencies.ledger.read()["modules"])

    @property
    def dependencies(self) -> dict[str, DependencyInfo]:
        """Read aggregated Python dependency ownership from deployment records.

        Returns:
            A new mapping from normalized distribution names to DependencyInfo.
            Versions describe the lock, not a fresh verification of installed files.

        Raises:
            DependencyError: Deployment records need repair with sync.
        """
        return self._dependencies.info

    def register(self, module: BaseModule) -> None:
        """Register one initialized declaration without starting it.

        Args:
            module: BaseModule instance whose base constructor completed.

        Raises:
            ModuleLoadError: The instance is invalid or a different instance already
                owns its name. Registering the identical object again is a no-op.
        """
        self._registry.register(module)

    def discover(self) -> Mapping[str, BaseModule]:
        """Import main entries once and register their initialized module instances.

        Returns:
            Live, read-only mapping of registered modules. Entries without valid
            instances are exposed through unavailable. Once discovery succeeds,
            subsequent calls return this mapping without rescanning. Configure the
            module directory and package before discovery.

        Raises:
            ModuleLoadError: An entry has multiple instances, a directory/name
                mismatch or a duplicate name.
            Exception: Importing an entry failed; the original import error propagates.

        Notes:
            Does not start services, resolve dependency graphs or install packages.
            Entry imports must restrict themselves to metadata declarations.
        """
        self._registry.discover()
        return self.modules

    async def start(self) -> None:
        """Ensure declarations are discovered as the kernel lifecycle start hook.

        Returns:
            None after discovery, reusing declarations from a prior preflight scan.
            Services remain unloaded until load is called.

        Raises:
            ModuleLoadError: Discovery rejected a declaration.
            Exception: An entry import failed.
        """
        self.discover()

    async def check_health(self, *names: str) -> dict[str, ModuleStatus]:
        """Collect health reports from selected registered modules.

        Args:
            *names: Registered module names. No names selects all registered
                modules, including those not loaded. Repeated names are checked
                once; explicit selections do not include prerequisites implicitly.

        Returns:
            A new mapping from module name to its health report, in selection order
            or registration order when no names are given. An empty registry returns
            an empty mapping. Reports retain each module's health flag and message.
            If a hook raises an Exception, its report uses health=False and a
            message containing the exception type and text; other checks continue.

        Raises:
            ModuleLoadError: Any selected name is unknown. All names are validated
                before invoking health hooks.

        Notes:
            Call discover or start first if declarations have not been registered.
            This method does not discover, load or stop modules, read deployment
            records or install packages. Checks run in order under the manager's
            operation lock, so startup and application shutdown wait until they finish.
            Health hooks must not call manager operations that acquire that lock.
            No timeout is imposed; callers may use asyncio.timeout around the call.
            Cancellation propagates and releases the operation lock.

        Example:
            statuses = await module_manager.check_health("web")
            print(statuses["web"]["health"])
        """
        async with self._lock:
            selected = dict.fromkeys(names or self._registry.modules)
            unknown = selected.keys() - self._registry.modules.keys()
            if unknown:
                raise ModuleLoadError(f"Unknown modules: {', '.join(sorted(unknown))}")
            statuses: dict[str, ModuleStatus] = {}
            for name in selected:
                try:
                    statuses[name] = await self._registry.modules[name].health()
                except Exception as exc:  # noqa: BLE001 - isolate module health failures
                    statuses[name] = {
                        "health": False,
                        "message": f"{type(exc).__name__}: {exc}",
                    }
            return statuses

    def list(self) -> list[str]:
        """List source module directory names without executing their entries.

        Returns:
            Sorted names of visible directories containing main.py. A missing source
            root produces an empty list. No records or dependency graphs are read.
        """
        return ModuleCatalog(self.modules_dir).names()

    def _check_idle(self) -> None:
        """Reject dependency changes while module resources are active."""
        if self._runtime.active:
            raise DependencyError(
                "Stop running modules before modifying their Python environment."
            )

    async def init(self, name: str) -> Path:
        """Create a new module skeleton in the current module source directory.

        Args:
            name: Python module identifier, excluding keywords and __-prefixed names.
                The generated declaration uses this name and version "0.1.0".

        Returns:
            Path to modules_dir / name containing main.py, an empty requirements.txt,
            package markers and src/shared/events.py, as in the development guide.
            The declaration includes editable startup, shutdown and health hooks.

        Raises:
            DependencyError: The name is invalid, points outside modules_dir, is a
                symlink, or already has an existing source file or directory.
            OSError: Creating, writing or publishing the source skeleton failed.

        Notes:
            Serializes with manager operations but does not require services to stop.
            Does not import or register the module, resolve or install dependencies,
            or read or write deployment records. Use add to deploy dependencies and
            discover or start to register the declaration when ready to load it.
            Existing sources are preserved. Failed staging writes leave no partial
            module in the destination; no overwrite or merge mode is provided.
        """
        async with self._lock:
            return initialize_module(self.modules_dir, name)

    def _state_directory(self, root: Path) -> Path:
        """Return the state namespace for a canonical source root without writing it."""
        identity = hashlib.sha256(os.fsencode(root.resolve())).hexdigest()[:16]
        return self._home / "projects" / identity

    def _require_source_root(self) -> None:
        """Reject dependency operations outside a checkout before creating state."""
        if not self.modules_dir.is_dir():
            raise DependencyError(
                "alira must run from a source checkout or set ALIRA_MODULES_DIR"
            )

    @staticmethod
    def _record(spec, source: dict) -> dict:
        """Build a declaration from static metadata, keeping packages out of it."""
        return {
            "version": spec.version,
            "source": source,
            "required-modules": list(spec.required_modules),
            "requirements": os.path.relpath(spec.requirements, spec.directory)
            if spec.requirements is not None
            else None,
            "requirements-hash": requirements_hash(spec.requirements),
        }

    def _specifications(
        self, declaration: dict, *, allow_missing: bool = False
    ) -> dict:
        """Read source declarations, normally validating their complete dependency graph.

        Locked preflight may inspect only present sources before recovering absent
        ones from the unchanged lock. Full graph validation follows recovery.
        """
        if allow_missing:
            specifications = {
                name: ModuleSpec.read(self.modules_dir / name, name=name)
                for name in declaration["modules"]
                if (self.modules_dir / name / "main.py").is_file()
            }
        else:
            specifications = ModuleCatalog(self.modules_dir).select(
                tuple(declaration["modules"])
            )
        for name, spec in specifications.items():
            record = declaration["modules"].get(name)
            if record is None:
                raise DependencyError(
                    f"Undeclared prerequisite: {name}", hint=f"alira add {name}"
                )
            if record["version"] != spec.version:
                raise DependencyError(
                    f"Module {name} declares version {record['version']}, source has {spec.version}",
                    hint=f"alira add {name}",
                )
        return specifications

    async def _restore_sources(
        self, records: dict, *, previous: dict | None = None
    ) -> None:
        """Restore missing locked sources; refuse to overwrite differing user files."""
        for name, record in records.items():
            target = module_directory(self.modules_dir, name)
            expected = record.get("source-sha256")
            if target.is_dir():
                continue
            source = record.get("source", {})
            if expected is None and previous is not None and name in previous:
                locked = previous[name]
                if (
                    locked["source"] == source
                    and locked["version"] == record["version"]
                ):
                    expected = locked["source-sha256"]
            if source.get("type") not in {"path", "git"}:
                raise DependencyError(f"Module source is missing: {name}")
            location = source.get("path") or source["url"]
            if source["type"] == "path" and not Path(location).is_absolute():
                location = str(self.modules_dir / location)
            restored, _ = await publish_source(
                location,
                self.modules_dir,
                source.get("ref"),
                expected_hash=expected or source.get("sha256"),
                on_publish=self._dependencies.ledger.record_source,
            )
            if restored != name:
                raise DependencyError(
                    f"Source declares {restored!r}, expected {name!r}"
                )

    async def _resolve(self, declaration: dict, specifications: dict) -> dict:
        """Refresh source fingerprints, resolve in memory and atomically write state."""
        records = {}
        for name, spec in specifications.items():
            records[name] = self._record(spec, declaration["modules"][name]["source"])
        declaration = declaration | {"modules": records}
        lock = await self._dependencies.resolve(
            declaration, specifications, cwd=self.modules_dir
        )
        self._dependencies.ledger.write_declaration(declaration)
        self._dependencies.ledger.write_lock(lock)
        return lock

    async def lock(self, *, check: bool = False, repair: bool = False) -> None:
        """Resolve declared modules and write their lock without installing packages.

        ``check`` only reads inputs and raises DependencyError for a stale/missing
        lock. ``repair`` restores an interrupted transaction's persisted snapshot
        before resolving. Declared versions must match main.py exactly.
        """
        self._require_source_root()
        async with self._lock:
            self._check_idle()
            ledger = self._dependencies.ledger
            if check:
                declaration = ledger.read()
                specs = self._specifications(declaration)
                if not self._dependencies.lock_is_current(declaration, specs):
                    raise DependencyError(
                        "Module lock is missing or outdated", hint="alira lock"
                    )
                return
            async with ledger.transaction("lock", repair=repair):
                declaration = ledger.read()
                specs = self._specifications(declaration)
                try:
                    current = self._dependencies.lock_is_current(declaration, specs)
                except DependencyError:
                    current = False
                if not current:
                    await self._resolve(declaration, specs)

    async def sync(
        self,
        *,
        locked: bool = False,
        frozen: bool = False,
        repair: bool = False,
        background: bool = False,
        prune: bool = True,
    ) -> None:
        """Synchronize sources and packages from the project lock.

        Default mode resolves only stale inputs. ``locked`` requires a current lock;
        ``frozen`` installs the existing lock without checking declarations. Both
        modes prohibit lock replacement. Exact sync prunes non-kernel extra packages;
        application launch passes prune=False, matching uv run's inexact default.
        File snapshots are restored on errors/cancellation; installed sources and
        partial package changes remain available for an idempotent retry.
        """
        self._require_source_root()
        async with self._lock:
            self._check_idle()
            ledger = self._dependencies.ledger
            async with ledger.transaction("sync", repair=repair):
                try:
                    lock = ledger.read_lock()
                except DependencyError:
                    if locked or frozen:
                        raise
                    lock = None
                if frozen:
                    if lock is None:
                        raise DependencyError(
                            "Module lock is missing", hint="alira lock"
                        )
                    await self._restore_sources(lock["modules"])
                else:
                    declaration = ledger.read()
                    # A locked sync must reject stale inputs before publishing sources.
                    if locked:
                        specs = self._specifications(declaration, allow_missing=True)
                        if not self._dependencies.lock_is_current(
                            declaration, specs, lock=lock, allow_missing_sources=True
                        ):
                            raise DependencyError(
                                "Module lock is missing or outdated", hint="alira lock"
                            )
                        await self._restore_sources(lock["modules"])
                        self._specifications(declaration)
                    else:
                        await self._restore_sources(
                            declaration["modules"],
                            previous=lock["modules"] if lock is not None else None,
                        )
                        specs = self._specifications(declaration)
                        if lock is None or not self._dependencies.lock_is_current(
                            declaration, specs, lock=lock
                        ):
                            lock = await self._resolve(declaration, specs)
                await self._dependencies.install(
                    lock, cwd=self.modules_dir, background=background, prune=prune
                )

    async def add(
        self,
        *sources: str,
        ref: str | None = None,
        manifest: Path | str | None = None,
        no_sync: bool = False,
        background: bool = False,
        repair: bool = False,
    ) -> None:
        """Declare local modules, ZIP/directory sources or GitHub repositories.

        Module prerequisites are included automatically. A manifest's local sources
        resolve relative to that manifest. Sources publish atomically and survive
        later failures; rerunning the same operation reuses identical content.
        ``no_sync`` writes declarations and the lock without changing packages.
        """
        self._require_source_root()
        incoming = [(str(source), ref) for source in sources]
        if manifest is not None:
            if sources or ref is not None:
                raise DependencyError(
                    "Use sources or --manifest, with refs inside the manifest"
                )
            incoming = [
                (item.source, item.ref) for item in read_package(Path(manifest))
            ]
        if not incoming:
            raise DependencyError("Specify a module name, source or --manifest")
        async with self._lock:
            self._check_idle()
            ledger = self._dependencies.ledger
            async with ledger.transaction("add", repair=repair):
                declaration = ledger.read()
                names = []
                origins = {}
                for source, revision in incoming:
                    if source.isidentifier() and not Path(source).is_dir():
                        if revision is not None:
                            raise DependencyError(
                                "--ref requires a GitHub repository source"
                            )
                        name = source
                    else:
                        name, origin = await publish_source(
                            source,
                            self.modules_dir,
                            revision,
                            on_publish=ledger.record_source,
                        )
                        origins[name] = origin
                    names.append(name)
                specs = ModuleCatalog(self.modules_dir).select(
                    tuple(dict.fromkeys((*declaration["modules"], *names)))
                )
                records = dict(declaration["modules"])
                requested = set(names)
                for name, spec in specs.items():
                    if (
                        name in records
                        and name not in requested
                        and records[name]["version"] != spec.version
                    ):
                        raise DependencyError(
                            f"Module {name} declares version {records[name]['version']}, source has {spec.version}",
                            hint=f"alira add {name}",
                        )
                    origin = (
                        origins.get(name)
                        or records.get(name, {}).get("source")
                        or {"type": "builtin"}
                    )
                    records[name] = {"source": origin}
                lock = await self._resolve(declaration | {"modules": records}, specs)
                if not no_sync:
                    await self._dependencies.install(
                        lock, cwd=self.modules_dir, background=background, prune=False
                    )

    async def install(
        self,
        source: str,
        *,
        ref: str | None = None,
        no_sync: bool = False,
        background: bool = False,
        repair: bool = False,
    ) -> None:
        """Alias for add(source), preserving the module-source installation entry point."""
        await self.add(
            source, ref=ref, no_sync=no_sync, background=background, repair=repair
        )

    async def install_package(
        self,
        path: Path | str,
        *,
        no_sync: bool = False,
        background: bool = False,
        repair: bool = False,
    ) -> None:
        """Add sources from a package manifest through the same dependency pipeline."""
        await self.add(
            manifest=path, no_sync=no_sync, background=background, repair=repair
        )

    async def remove(
        self,
        *names: str,
        purge: bool = False,
        no_sync: bool = False,
        background: bool = False,
        repair: bool = False,
    ) -> None:
        """Release declarations and exclusive packages, preserving sources by default.

        Retained modules must not require a removed name. ``purge`` sends selected
        sources to the system trash after dependency installation succeeds. Errors
        restore declaration/lock snapshots; package recovery is an explicit sync.
        """
        self._require_source_root()
        async with self._lock:
            self._check_idle()
            ledger = self._dependencies.ledger
            async with ledger.transaction("remove", repair=repair):
                declaration = ledger.read()
                selected = set(names)
                unknown = selected - declaration["modules"].keys()
                if unknown:
                    raise DependencyError(
                        f"Modules are not declared: {', '.join(sorted(unknown))}"
                    )
                retained = {
                    name: record
                    for name, record in declaration["modules"].items()
                    if name not in selected
                }
                for name, record in retained.items():
                    if any(
                        parse_module_requirement(item)[0] in selected
                        for item in record["required-modules"]
                    ):
                        raise DependencyError(
                            f"Retained module {name} requires a removed module"
                        )
                declaration = declaration | {"modules": retained}
                specs = self._specifications(declaration)
                previous = ledger.read_lock()
                lock = await self._resolve(declaration, specs)
                if not no_sync:
                    await self._dependencies.install(
                        lock, cwd=self.modules_dir, background=background, prune=False
                    )
                    old = set(previous["packages"]) if previous is not None else set()
                    await self._dependencies.uninstall_packages(
                        old - lock["packages"].keys(),
                        cwd=self.modules_dir,
                        background=background,
                    )
                if purge:
                    with uninstall_sources(self.modules_dir, names):
                        pass

    async def uninstall(
        self, *names: str, background: bool = False, repair: bool = False
    ) -> None:
        """Remove declarations/packages and move their module sources to system trash."""
        await self.remove(*names, purge=True, background=background, repair=repair)

    async def prepare_run(
        self,
        *names: str,
        no_sync: bool = False,
        locked: bool = False,
        frozen: bool = False,
        force_sync: bool = False,
    ) -> None:
        """Prepare the environment before business imports, then release process locks.

        Default launch checks inputs and synchronizes missing packages. ``locked``
        prohibits resolving; ``frozen`` skips freshness checks but still installs
        locked packages. ``no_sync`` skips all dependency reads and mutations.
        """
        self._require_source_root()
        if no_sync:
            return
        ledger = self._dependencies.ledger
        for attempt in range(2):
            if ledger.journal.exists():
                await self.sync(
                    locked=locked, frozen=frozen, background=True, prune=False
                )
            before = ledger.snapshot()
            try:
                needs_sync = self._run_needs_sync(names, locked=locked, frozen=frozen)
            except DependencyError:
                if attempt == 0 and (
                    ledger.journal.exists() or ledger.snapshot() != before
                ):
                    continue
                raise
            if ledger.journal.exists() or ledger.snapshot() != before:
                if attempt == 0:
                    continue
                # A second concurrent update converges under the short process lock.
                await self.sync(
                    locked=locked, frozen=frozen, background=True, prune=False
                )
                self._run_needs_sync(names, locked=locked, frozen=frozen)
                return
            if force_sync or needs_sync:
                await self.sync(
                    locked=locked, frozen=frozen, background=True, prune=False
                )
            return

    def _run_needs_sync(
        self, names: tuple[str, ...], *, locked: bool, frozen: bool
    ) -> bool:
        """Read one dependency-state generation and decide whether launch needs sync."""
        ledger = self._dependencies.ledger
        try:
            lock = ledger.read_lock()
        except DependencyError:
            if locked or frozen:
                raise
            lock = None
        if not frozen:
            declaration = ledger.read()
            unknown = set(names) - declaration["modules"].keys()
            if unknown:
                raise DependencyError(
                    f"Modules are not declared: {', '.join(sorted(unknown))}",
                    hint="alira add " + " ".join(sorted(unknown)),
                )
            try:
                specs = self._specifications(declaration, allow_missing=locked)
                current = lock is not None and self._dependencies.lock_is_current(
                    declaration,
                    specs,
                    lock=lock,
                    allow_missing_sources=locked,
                )
            except DependencyError:
                if locked:
                    raise
                current = False
            if locked and not current:
                raise DependencyError(
                    "Module lock is missing or outdated", hint="alira lock"
                )
        else:
            current = lock is not None
            if lock is not None:
                unknown = set(names) - lock["modules"].keys()
                if unknown:
                    raise DependencyError(
                        f"Modules are not in the frozen lock: {', '.join(sorted(unknown))}"
                    )
        if not current or lock["kernel"] != {
            "version": __version__,
            "python": platform.python_version(),
        }:
            return True
        installed = self._dependencies.installed()
        return any(
            installed.get(name) != item["version"]
            for name, item in lock["packages"].items()
        ) or any(
            not (self.modules_dir / name / "main.py").is_file()
            for name in lock["modules"]
        )

    def tree(self, *, frozen: bool = False) -> str:
        """Render the current lock's module and package ownership without importing entries."""
        self._require_source_root()
        lock = self._dependencies.ledger.read_lock()
        if lock is None:
            raise DependencyError("Module lock is missing", hint="alira lock")
        if not frozen:
            declaration = self._dependencies.ledger.read()
            specs = self._specifications(declaration)
            if not self._dependencies.lock_is_current(declaration, specs, lock=lock):
                raise DependencyError(
                    "Module lock is missing or outdated", hint="alira lock"
                )
        lines = []
        for name, record in sorted(lock["modules"].items()):
            lines.append(f"{name} v{record['version']}")
            for dependency in record["required-modules"]:
                lines.append(f"  module: {dependency}")
            for package in record["packages"]:
                lines.append(f"  {package}=={lock['packages'][package]['version']}")
        return "\n".join(lines)

    async def migrate(self) -> None:
        """Convert legacy global records to this source root's declarations and lock.

        Old records are read only as migration input; source metadata is authoritative.
        Existing pylock artifacts are converted directly after validating legacy
        input fingerprints, retaining versions and hashes without a resolver.
        Existing backups are preserved. Failure restores new state and removes
        backups created by this operation; the original legacy files remain intact.
        """
        self._require_source_root()
        legacy = self._home / "modules.json"
        old_lock = self._home / "pylock.toml"
        ledger = self._dependencies.ledger
        if ledger.path.exists() or ledger.lockfile.exists():
            raise DependencyError(
                "Project state already exists; migration is unnecessary"
            )
        try:
            previous = json.loads(legacy.read_text())
            records = previous["modules"]
            artifact = old_lock.read_bytes()
            if previous.get("lock_hash") != hashlib.sha256(artifact).hexdigest():
                raise ValueError("legacy records and pylock.toml do not match")
            tomllib.loads(artifact.decode())
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise DependencyError(f"Cannot read legacy module state: {exc}") from exc
        async with self._lock:
            self._check_idle()
            created_backups = []
            try:
                async with ledger.transaction("migrate"):
                    if ledger.path.exists() or ledger.lockfile.exists():
                        raise DependencyError(
                            "Project state already exists; migration is unnecessary"
                        )
                    specs = ModuleCatalog(self.modules_dir).select(tuple(records))
                    declaration = {
                        "format": 2,
                        "requires-kernel": f">={__version__},<4",
                        "modules": {
                            name: self._record(spec, {"type": "builtin"})
                            for name, spec in specs.items()
                        },
                    }
                    lock = self._dependencies.migrate_lock(
                        declaration,
                        specs,
                        records,
                        artifact,
                        legacy_directory=self._home,
                    )
                    ledger.write_declaration(declaration)
                    ledger.write_lock(lock)
                    for path in (legacy, old_lock):
                        backup = path.with_name(path.name + ".v1.bak")
                        content = path.read_bytes()
                        if backup.exists():
                            if backup.read_bytes() != content:
                                raise DependencyError(
                                    f"Migration backup already exists: {backup}"
                                )
                        else:
                            DependencyLedger.atomic_write(backup, content)
                            created_backups.append(backup)

            except BaseException:
                for backup in created_backups:
                    try:
                        backup.unlink(missing_ok=True)
                    except OSError as exc:
                        print(
                            f"Could not remove migration backup {backup}: {exc}",
                            file=sys.stderr,
                        )
                raise

    async def load(self, *names: str, verify_dependencies: bool = True) -> None:
        """Verify dependencies and start the application's selected modules once.

        Args:
            *names: Registered module names; no names selects every registered module.
            verify_dependencies: Check deployed requirements and installed versions.
                CLI launch already performs this preflight before business imports.

        Raises:
            ModuleLoadError: A module or prerequisite is unknown or the graph cycles.
            DependencyError: Recorded requirements changed, a package version is wrong
                or missing, or explicit dependency installation fails.
            FileNotFoundError: An explicitly declared requirements file is missing.
            RuntimeError: Runtime modules have already been started in this process.
            asyncio.CancelledError: Loading was cancelled; partial startup is cleaned up.

        Notes:
            Call discovery or start first. This is the only runtime load for the
            application; later calls, including after stop, are rejected. Independent
            startups run concurrently and dependents wait for readiness.
            Startup failure skips dependent modules while independent branches finish.
            Each startup exception is logged immediately by the kernel and does not
            propagate to the application. Successful modules remain running.
            Dependency paths and their results are printed together after startup tasks
            finish; these display plans do not control scheduling. Failed startup
            attempts are cleaned up immediately; successful modules are closed by stop
            at application exit. Cleanup errors are logged without retries. This method
            does not start the kernel's other lifecycle services on its own.
        """
        async with self._lock:
            graph = self._registry.graph(names or self._registry.modules.keys())
            requirements = {}
            if verify_dependencies:
                for name in graph:
                    path = self._registry.requirements_path(name)
                    if path is not None:
                        requirements[name] = path
            if requirements:
                self._dependencies.restore()
                installed = self._dependencies.installed()
                for name in sorted(requirements):
                    self._dependencies.verify(name, requirements[name], installed)
            await self._runtime.load(graph)

    async def stop(self) -> None:
        """Close runtime modules once in reverse startup order at application exit.

        Returns:
            None after successful cleanup. The registry, source files, installed Python
            packages and dependency claims are preserved.

        Raises:
            asyncio.CancelledError: Application shutdown was interrupted.

        Notes:
            Ordinary shutdown errors are logged immediately and do not stop cleanup of
            other modules. There is no single-module unload, reload or cleanup retry.
        """
        async with self._lock:
            await self._runtime.stop()


module_manager = ModuleManager()

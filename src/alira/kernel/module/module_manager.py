"""Public module manager: serialize source, dependency and runtime operations."""

import asyncio
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import ClassVar, Self

from alira.kernel.module.base_module import BaseModule, ModuleStatus
from alira.kernel.module.dependencies.deployment import ModuleDependencies
from alira.kernel.module.dependencies.types import (
    DependencyError,
    DependencyInfo,
    DependencyResolutionError,
    ModuleLoadError,
    sync_hint,
)
from alira.kernel.module.discovery.catalog import ModuleCatalog
from alira.kernel.module.runtime.lifecycle import ModuleRuntime
from alira.kernel.module.runtime.registry import ModuleRegistry
from alira.kernel.module.sources.archive import install_source, uninstall_sources
from alira.kernel.module.sources.package import install_package_sources, read_package
from alira.kernel.module.sources.scaffold import initialize_module


class ModuleManager:
    """Process-wide facade for module discovery, deployment and runtime state.

    Use ``module_manager`` for application operations. Constructing ModuleManager
    again returns that same instance without resetting it. Discovery imports
    declarative entries; management commands instead inspect source statically.
    Runtime loading and dependency deployment are separate operations.

    Runtime modules are selected once at application startup and closed at exit.
    Dependency operations target the interpreter running Alira and require no
    active module resources. Runtime transitions and dependency updates use an
    event-loop lock; source transactions do not
    provide isolation between separate processes or clients.
    """

    _instance: ClassVar[Self | None] = None

    def __new__(cls, **kwargs) -> Self:
        """Return the process singleton, allocating it on the first call."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, *, state_dir: Path | None = None):
        """Initialize registry, dependency state and runtime bookkeeping once.

        Args:
            state_dir: Dependency-state directory; None uses ``Path.home() / ".alira"``.
                This is honored only when the instance is first initialized. Source
                discovery defaults to the installed Alira package's modules directory.

        Notes:
            Construction creates no lifecycle registrations, imports no business
            entries and performs no dependency-state reads or environment changes.
        """
        if getattr(self, "_initialized", False):
            return
        self._registry = ModuleRegistry(
            Path(__file__).resolve().parents[2] / "modules", "alira.modules"
        )
        self._dependencies = ModuleDependencies(
            Path(state_dir) if state_dir is not None else Path.home() / ".alira"
        )
        self._runtime = ModuleRuntime(self._registry)
        self._lock = asyncio.Lock()
        self._initialized = True

    @classmethod
    def _new_for_tests(cls, **kwargs) -> Self:
        """Create an isolated manager with constructor kwargs without replacing the singleton."""
        instance = object.__new__(cls)
        instance.__init__(**kwargs)
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
        self._registry.directory = directory

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
        self._dependencies.restore()
        return frozenset(self._dependencies.claims)

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

    async def sync(self, *, no_install: bool = False, background: bool = False) -> None:
        """Rebuild all dependency records and synchronize the current interpreter.

        Args:
            no_install: If true, resolve requirements and write records/lock only;
                skip every package installation, update and removal.
            background: Capture uv output instead of streaming it to the terminal.
                Both modes await completion; this does not detach the operation.

        Raises:
            DependencyResolutionError: Requirements cannot be resolved, including a
                conflict with pinned kernel dependencies. Existing records remain.
            DependencyError: Modules are active, static declarations are invalid,
                prerequisites are missing, a cycle exists or uv installation failed.
            OSError: Source or record I/O failed.

        Notes:
            Uses all actual module sources and ignores damaged old records. Modules
            removed from management but still on disk are included again. Unless
            no_install is true, packages outside the resolved module set and protected
            kernel closure are removed. Sources and project configuration are retained.
            Records are written before installation; repeat sync after an interruption
            to repair records and converge the environment. No kernel lifecycle starts.
        """
        async with self._lock:
            self._check_idle()
            catalog = ModuleCatalog(self.modules_dir)
            try:
                specifications = catalog.select(catalog.names())
            except DependencyError as exc:
                raise DependencyResolutionError(str(exc)) from exc
            await self._dependencies.rebuild(
                specifications,
                cwd=self.modules_dir,
                no_install=no_install,
                background=background,
                prune=True,
            )

    async def add(
        self, *names: str, no_install: bool = False, background: bool = False
    ) -> None:
        """Manage existing modules and their prerequisites, then install their dependencies.

        Args:
            *names: Source module names. No names means no new selections; already
                managed requirements still participate in resolution.
            no_install: Resolve and update records/lock without mutating Python packages.
            background: Capture uv progress while still awaiting completion.

        Raises:
            DependencyResolutionError: The combined managed requirements conflict.
            DependencyError: Modules are active, selected/prerequisite sources are
                absent, the graph is invalid, records need sync or installation failed.
            OSError: Requirements or records could not be accessed.

        Notes:
            Includes existing deployment owners and resolves their current declarations
            without scanning unrelated module directories. Damaged records require sync.
            Does not download or change sources, or prune unrelated packages. Records are not rolled back on installation failure; sync repairs
            from all remaining sources and may re-add previously removed modules.
        """
        async with self._lock:
            self._check_idle()
            specifications = ModuleCatalog(self.modules_dir).select(names)
            await self._dependencies.add(
                specifications,
                cwd=self.modules_dir,
                no_install=no_install,
                background=background,
            )

    async def install(
        self, source: str, *, ref: str | None = None, background: bool = False
    ) -> None:
        """Install one local ZIP or GitHub source and reuse add for its dependencies.

        Args:
            source: Local ZIP path, relative to the current working directory, or
                ``https://github.com/owner/repo``. Local paths support ``~`` expansion.
            ref: GitHub branch, tag or commit; None selects the default branch.
                Supplying a ref for a local ZIP is an error.
            background: Capture uv output rather than stream installation progress.

        Raises:
            DependencyError: Modules are active, the source is unsafe or invalid,
                its name already exists, prerequisites are missing, or deployment fails.
            OSError: Source staging or record operations failed.
            BaseExceptionGroup: The original failure and best-effort rollback failures.

        Notes:
            Requires main.py and src/, checks archive paths and never executes module
            code. Existing source directories are never overwritten. Ordinary failures
            and cancellation attempt to restore new sources, records and prior package
            versions. A forced termination cannot run rollback: inspect sources and
            run sync; this single-source interface rejects an already present name.
        """
        self._check_idle()
        async with (
            self._dependencies.transaction(self.modules_dir, background=background),
            install_source(self._registry, source, ref) as name,
        ):
            await self.add(name, background=background)

    async def uninstall(self, *names: str, background: bool = False) -> None:
        """Release recorded dependencies and move module sources to the system trash.

        Args:
            *names: Existing source module names also present in dependency records.
                Duplicate names identify the same source directory once.
            background: Capture uv output while awaiting dependency removal.

        Raises:
            DependencyError: Modules are active, sources or records are missing, a
                retained module requires a selection, or dependency removal failed.
            OSError: Backup, system trash, restoration or record I/O failed.
            BaseExceptionGroup: Uninstall and one or more recovery steps failed.

        Notes:
            Reuses remove; dependencies shared with retained modules or the kernel are
            protected. Uses the actual OS trash with no permanent-deletion fallback.
            On failure it attempts to restore source backups, records and package
            versions. If recovery is incomplete, restore missing sources before sync.
        """
        self._check_idle()
        async with self._dependencies.transaction(
            self.modules_dir, background=background
        ):
            with uninstall_sources(self.modules_dir, names):
                await self.remove(*names, background=background)

    async def install_package(
        self, path: Path | str, *, background: bool = False
    ) -> None:
        """Install a package.yml source set and run one complete sync.

        Args:
            path: Manifest path, relative to the current working directory unless
                absolute. Local source paths inside it are relative to its directory.
                The nonempty modules list contains source and optional GitHub ref keys.
            background: Capture uv output while awaiting dependency synchronization.

        Raises:
            DependencyResolutionError: Requirements are invalid or incompatible;
                newly published sources are withdrawn where rollback succeeds.
            DependencyError: The manifest, archive, module graph or existing content
                conflicts, modules are active, or dependency installation failed.
            OSError: Source, record or environment operations failed.
            BaseExceptionGroup: Source rollback was incomplete.

        Notes:
            Validates all sources before publishing prerequisites first. Matching
            existing content is reused; differing content is preserved and rejected.
            Resolution failure withdraws only newly published sources. Once resolution
            succeeds, installation failure or cancellation keeps sources for sync to
            repair. After forced termination during publication, rerun the same
            manifest to fill missing sources. Full sync also includes sources outside
            the manifest and prunes unrelated packages. Kernel lifecycle is not started.
        """
        sources = read_package(Path(path))
        self._check_idle()
        async with install_package_sources(self.modules_dir, sources):
            try:
                await self.sync(background=background)
            except DependencyResolutionError:
                raise
            except (DependencyError, OSError) as exc:
                raise DependencyError(
                    f"Package sources are installed; dependency synchronization failed: {exc}",
                    hint=None
                    if isinstance(exc, DependencyError) and exc.hint
                    else sync_hint(),
                ) from exc

    async def load(self, *names: str, install_missing: bool = False) -> None:
        """Verify dependencies and start the application's selected modules once.

        Args:
            *names: Registered module names; no names selects every registered module.
            install_missing: Explicitly call add first, permitting dependency resolution
                and installation. False performs offline verification only.

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
        if install_missing:
            await self.add(*(names or tuple(self._registry.modules)), background=True)
        async with self._lock:
            graph = self._registry.graph(names or self._registry.modules.keys())
            requirements = {}
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

    async def remove(self, *names: str, background: bool = False) -> None:
        """Remove selected dependency claims, retaining all module source files.

        Args:
            *names: Module names present in dependency-management records.
            background: Capture uv output while awaiting package removal.

        Raises:
            DependencyError: Modules are active, records need sync, a name is not
                managed, a retained module depends on it or uv removal failed.
            OSError: Record or environment I/O failed.

        Notes:
            Retains packages still required by another managed module or the kernel.
            Writes retained records and lock before uninstalling; on failure repeat
            sync rather than roll back records. Since sources remain, a later full
            sync deliberately manages these modules again.
        """
        async with self._lock:
            self._check_idle()
            await self._dependencies.remove(
                names, cwd=self.modules_dir, background=background
            )

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

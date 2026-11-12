"""Public module manager: serialize source, dependency and runtime operations."""

import asyncio
import shutil
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
from typing import ClassVar, Self

from alira.kernel.module.base_module import BaseModule
from alira.kernel.module.catalog import ModuleCatalog, module_directory
from alira.kernel.module.dependencies.deployment import ModuleDependencies
from alira.kernel.module.dependencies.types import (
    DependencyError,
    DependencyInfo,
    DependencyResolutionError,
    sync_hint,
)
from alira.kernel.module.package import install_package_sources, read_package
from alira.kernel.module.registry import ModuleRegistry
from alira.kernel.module.runtime import ModuleRuntime
from alira.kernel.module.sources import install_source


class ModuleManager:
    """Process-wide facade for module discovery, deployment and runtime state.

    Use ``module_manager`` for application operations. Constructing ModuleManager
    again returns that same instance without resetting it. Discovery imports
    declarative entries; management commands instead inspect source statically.
    Runtime loading and dependency deployment are separate operations.

    Dependency operations target the interpreter running Alira. They require all
    attempted module startups to have been cleaned up first. Runtime transitions
    and dependency updates use an event-loop lock; source transactions do not
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
        """Return a snapshot of entry names without initialized instances at the last discovery."""
        return frozenset(self._registry.unavailable)

    @property
    def loaded(self) -> frozenset[str]:
        """Return a snapshot of module names whose startup completed and cleanup has not completed."""
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
        """Import main entries and register their initialized module instances.

        Returns:
            Live, read-only mapping of registered modules. Entries without valid
            instances are exposed through unavailable. Previously registered modules
            are retained; discovery is not a reload or removal operation.

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
        """Discover declarations as the manager's kernel lifecycle start hook.

        Returns:
            None after discovery. Services remain unloaded until load is called.

        Raises:
            ModuleLoadError: Discovery rejected a declaration.
            Exception: An entry import failed.
        """
        self.discover()

    def list(self) -> list[str]:
        """List source module directory names without executing their entries.

        Returns:
            Sorted names of visible directories containing main.py. A missing source
            root produces an empty list. No records or dependency graphs are read.
        """
        return ModuleCatalog(self.modules_dir).names()

    def _check_idle(self) -> None:
        """Raise DependencyError until every attempted module startup has been cleaned up."""
        if self._runtime.startup_attempted:
            raise DependencyError(
                "Stop running modules before modifying their Python environment."
            )

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
            specifications = catalog.select(catalog.names())
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
            Reuses records for other managed modules without scanning unrelated module
            directories. Does not download or change sources, or prune unrelated
            packages. Records are not rolled back on installation failure; sync repairs
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
        from send2trash import send2trash

        self._check_idle()
        directories = [
            module_directory(self.modules_dir, name) for name in dict.fromkeys(names)
        ]
        for directory in directories:
            if not (directory / "main.py").is_file():
                raise DependencyError(f"Module not found: {directory.name}")
        with TemporaryDirectory(prefix="alira-uninstall-") as temporary:
            backup = Path(temporary)
            for directory in directories:
                shutil.copytree(directory, backup / directory.name, symlinks=True)
            async with self._dependencies.transaction(
                self.modules_dir, background=background
            ):
                try:
                    await self.remove(*names, background=background)
                    for directory in directories:
                        send2trash(str(directory))
                except BaseException as failure:
                    errors = []
                    for directory in directories:
                        if not directory.exists():
                            try:
                                shutil.copytree(
                                    backup / directory.name, directory, symlinks=True
                                )
                            except OSError as exc:
                                errors.append(exc)
                    if errors:
                        raise BaseExceptionGroup(
                            "Module uninstall failed and source restoration was incomplete",
                            [failure, *errors],
                        ) from None
                    raise

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
        """Verify dependencies, then start selected modules and their prerequisites.

        Args:
            *names: Registered module names; no names selects every registered module.
            install_missing: Explicitly call add first, permitting dependency resolution
                and installation. False performs offline verification only.

        Raises:
            ModuleLoadError: A module or prerequisite is unknown or the graph cycles.
                Deferred ImportError failures are wrapped in this type within a group.
            DependencyError: Recorded requirements changed, a package version is wrong
                or missing, or explicit dependency installation fails.
            FileNotFoundError: An explicitly declared requirements file is missing.
            RuntimeError: A previous failed attempt still requires cleanup.
            BaseExceptionGroup: Startup tasks or their subsequent cleanup failed.

        Notes:
            Call discovery or start first. Already loaded modules are skipped;
            independent startups run concurrently and dependents wait for readiness.
            Startup failure cancels pending tasks and cleans attempted modules that
            have not completed startup; successfully loaded modules may remain active.
            Use unload/stop to retry failed cleanup before loading again. This method
            does not start the kernel's other lifecycle services on its own.
        """
        if install_missing:
            await self.add(*(names or tuple(self._registry.modules)), background=True)
        async with self._lock:
            graph = self._registry.graph(names or self._registry.modules.keys())
            pending = graph.keys() - self._runtime.loaded
            if pending & self._runtime.startup_attempted:
                raise RuntimeError(
                    "A previous failed load needs unload() cleanup before retrying"
                )
            requirements = {
                name: self._registry.requirements_path(name) for name in pending
            }
            if any(path is not None for path in requirements.values()):
                self._dependencies.restore()
            installed = self._dependencies.installed()
            for name in sorted(pending):
                self._dependencies.verify(name, requirements[name], installed)
            await self._runtime.load(graph, pending)

    async def unload(self, name: str) -> None:
        """Stop one module without removing sources or dependency ownership.

        Args:
            name: Registered module name. A module with no pending runtime state is
                already unloaded and requires no hook invocation.

        Raises:
            ValueError: The name is unknown or an attempted dependent still needs it.
            Exception: The module's shutdown hook failed; runtime state is retained
                for a later cleanup attempt.

        Notes:
            Does not evict imported Python modules. Modules must unregister their own
            event handlers and close resources; this is not source-code hot reload.
        """
        async with self._lock:
            await self._runtime.unload(name)

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
        """Stop attempted modules in reverse dependency order, retaining deployment.

        Returns:
            None after successful cleanup. The registry, source files, installed Python
            packages and dependency claims are preserved for future starts and loads.

        Raises:
            ExceptionGroup: One or more shutdown hooks failed; other eligible cleanup
                is still attempted. Failed modules and required prerequisites remain
                tracked so stop or unload can retry.

        Notes:
            Also cleans partial startup attempts. Cancellation leaves retryable state.
        """
        async with self._lock:
            await self._runtime.stop()


module_manager = ModuleManager()

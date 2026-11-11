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
from alira.kernel.module.dependencies.types import DependencyError, DependencyInfo
from alira.kernel.module.registry import ModuleRegistry
from alira.kernel.module.runtime import ModuleRuntime
from alira.kernel.module.sources import install_source


class ModuleManager:
    """Process-wide owner of module declarations, deployments and running services."""

    _instance: ClassVar[Self | None] = None

    def __new__(cls, **kwargs) -> Self:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, *, state_dir: Path | None = None):
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
        instance = object.__new__(cls)
        instance.__init__(**kwargs)
        return instance

    @property
    def modules_dir(self) -> Path:
        return self._registry.directory

    @modules_dir.setter
    def modules_dir(self, directory: Path) -> None:
        self._registry.directory = directory

    @property
    def package(self) -> str:
        return self._registry.package

    @package.setter
    def package(self, package: str) -> None:
        self._registry.package = package

    @property
    def modules(self) -> Mapping[str, BaseModule]:
        return MappingProxyType(self._registry.modules)

    @property
    def unavailable(self) -> frozenset[str]:
        return frozenset(self._registry.unavailable)

    @property
    def loaded(self) -> frozenset[str]:
        return frozenset(self._runtime.loaded)

    @property
    def deployments(self) -> frozenset[str]:
        """Module names recorded in the ledger, including interrupted installs."""
        self._dependencies.restore()
        return frozenset(self._dependencies.claims)

    @property
    def dependencies(self) -> dict[str, DependencyInfo]:
        return self._dependencies.info

    def register(self, module: BaseModule) -> None:
        self._registry.register(module)

    def discover(self) -> Mapping[str, BaseModule]:
        self._registry.discover()
        return self.modules

    async def start(self) -> None:
        self.discover()

    def list(self) -> list[str]:
        """List source directories without reading records or importing entries."""
        return ModuleCatalog(self.modules_dir).names()

    def _check_idle(self) -> None:
        if self._runtime.startup_attempted:
            raise DependencyError(
                "Stop running modules before modifying their Python environment."
            )

    async def sync(self, *, no_install: bool = False, background: bool = False) -> None:
        """Rebuild all dependency records from actual module sources."""
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
        """Manage dependencies of existing modules and their prerequisites."""
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
        """Install validated sources and reuse add(), rolling back ordinary failures."""
        self._check_idle()
        async with (
            self._dependencies.transaction(self.modules_dir, background=background),
            install_source(self._registry, source, ref) as name,
        ):
            await self.add(name, background=background)

    async def uninstall(self, *names: str, background: bool = False) -> None:
        """Release dependencies, then move sources into the operating system trash."""
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

    async def load(self, *names: str, install_missing: bool = False) -> None:
        """Verify dependencies offline, then start services in dependency order."""
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
        async with self._lock:
            await self._runtime.unload(name)

    async def remove(self, *names: str, background: bool = False) -> None:
        """Release dependency records without changing any module sources."""
        async with self._lock:
            self._check_idle()
            await self._dependencies.remove(
                names, cwd=self.modules_dir, background=background
            )

    async def stop(self) -> None:
        async with self._lock:
            await self._runtime.stop()


module_manager = ModuleManager()

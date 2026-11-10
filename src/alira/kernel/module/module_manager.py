"""Discover modules, order lifecycle operations and own shared dependencies."""

import asyncio
import importlib
from collections.abc import Collection, Mapping
from graphlib import CycleError, TopologicalSorter
from importlib.metadata import distributions
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
from typing import ClassVar, Self

from packaging.utils import canonicalize_name

from alira.kernel.module.base_module import ALIRA_DIR, BaseModule
from alira.kernel.module.dependencies import (
    DependencyInfo,
    ModuleRequirements,
    resolve_requirements,
    run_uv,
)
from alira.utils.logger import logger


class ModuleManager:
    """Process-wide singleton owning module lifecycle and package mutations.

    main.py exports one BaseModule instance. Its imports and constructor must be
    declaration-only; business packages are imported by startup(). Public load,
    unload and stop operations are serialized; independent module starts run
    concurrently within a load operation.
    """

    _instance: ClassVar[Self | None] = None

    def __new__(cls) -> Self:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return
        self.modules_dir = ALIRA_DIR / "modules"
        self.package = "alira.modules"
        self._modules: dict[str, BaseModule] = {}
        self._directories: dict[str, Path] = {}
        self._loaded: set[str] = set()
        self._started: set[str] = set()
        self._claims: dict[str, dict[str, str]] = {}
        self._references: dict[str, set[str]] = {}
        self._versions: dict[str, str] = {}
        self._baseline = {
            canonicalize_name(dist.metadata["Name"]): dist.version for dist in distributions()
        }
        self._lock = asyncio.Lock()
        self._initialized = True

    @property
    def modules(self) -> Mapping[str, BaseModule]:
        return MappingProxyType(self._modules)

    @property
    def loaded(self) -> frozenset[str]:
        return frozenset(self._loaded)

    @property
    def dependencies(self) -> dict[str, DependencyInfo]:
        return {
            name: DependencyInfo(name, self._versions[name], frozenset(owners), name in self._baseline)
            for name, owners in self._references.items()
        }

    def register(self, module: BaseModule) -> None:
        """Register a declaration; validation of the complete graph happens at load."""
        if not isinstance(module, BaseModule):
            raise TypeError("Module declarations must be BaseModule instances")
        if module.name in self._modules:
            if self._modules[module.name] is module:
                return
            raise ValueError(f"Duplicate module name: {module.name}")
        self._modules[module.name] = module
        self._directories[module.name] = self.modules_dir / module.name

    def discover(self) -> Mapping[str, BaseModule]:
        """Import only main.py in immediate child directories; skip legacy modules."""
        for path in sorted(self.modules_dir.glob("*/main.py")):
            declaration = importlib.import_module(f"{self.package}.{path.parent.name}.main")
            instances = {
                id(value): value for value in vars(declaration).values()
                if isinstance(value, BaseModule)
            }
            if len(instances) != 1:
                raise ValueError(f"{path} must declare exactly one BaseModule instance")
            module = next(iter(instances.values()))
            self.register(module)
            self._directories[module.name] = path.parent
        return self.modules

    def _graph(self, names: Collection[str]) -> dict[str, set[str]]:
        graph = {}

        def select(name: str) -> None:
            if name in graph:
                return
            if name not in self._modules:
                raise ValueError(f"Unknown module: {name}")
            dependencies = set(self._modules[name].required_modules)
            missing = dependencies - self._modules.keys()
            if missing:
                raise ValueError(f"Module {name} requires unknown modules: {', '.join(sorted(missing))}")
            graph[name] = dependencies
            for dependency in dependencies:
                select(dependency)

        for name in names:
            select(name)
        try:
            tuple(TopologicalSorter(graph).static_order())
        except CycleError as exc:
            raise ValueError(f"Module dependency cycle: {exc.args[1]}") from exc
        return graph

    def _requirements_path(self, name: str) -> Path | None:
        declared = self._modules[name].requirements_txt
        path = (
            self.modules_dir / declared if declared is not None
            else self._directories[name] / "requirements.txt"
        ).resolve()
        if path.is_file():
            return path
        if declared is not None:
            raise FileNotFoundError(f"Module {name}: requirements file not found: {path}")
        return None

    async def start(self) -> None:
        """Discover declarations; installation and startup require explicit load()."""
        self.discover()

    async def load(self, *names: str) -> None:
        """Load selected modules and their prerequisites, or all declarations."""
        async with self._lock:
            graph = self._graph(names or self._modules.keys())
            pending = graph.keys() - self._loaded
            if not pending:
                return
            if pending & self._claims.keys():
                raise RuntimeError("A previous failed load needs unload() cleanup before retrying")
            with TemporaryDirectory(prefix="alira-modules-") as temporary:
                plans = await resolve_requirements(
                    {name: self._requirements_path(name) for name in sorted(pending)},
                    self._baseline | self._versions, Path(temporary), self.modules_dir,
                )
                # Reserve all owners before parallel installs or failure cleanup.
                for name, plan in plans.items():
                    self._claims[name] = plan.packages
                    for dependency, version in plan.packages.items():
                        self._versions[dependency] = version
                        self._references.setdefault(dependency, set()).add(name)
                sorter = TopologicalSorter({name: graph[name] & pending for name in sorted(pending)})
                sorter.prepare()
                try:
                    async with asyncio.TaskGroup() as group:
                        tasks = {}
                        while sorter.is_active():
                            for name in sorter.get_ready():
                                task = group.create_task(
                                    self._load_one(name, plans[name]), name=f"module:{name}"
                                )
                                tasks[task] = name
                            done, _ = await asyncio.wait(
                                tasks, return_when=asyncio.FIRST_COMPLETED
                            )
                            for task in done:
                                task.result()
                                sorter.done(tasks.pop(task))
                except BaseException as exc:
                    errors = []
                    for name in reversed(tuple(TopologicalSorter(graph).static_order())):
                        if name in pending and name not in self._loaded:
                            try:
                                await self._unload_one(name)
                            except Exception as cleanup_error:
                                errors.append(cleanup_error)
                    if errors:
                        raise BaseExceptionGroup(
                            "Module load and cleanup failed", [exc, *errors]
                        ) from None
                    raise

    async def _install(self, name: str, plan: ModuleRequirements) -> None:
        if plan.lockfile is not None:
            logger.info(f"Installing dependencies for module {name}")
            await run_uv(
                "install", "--requirements", str(plan.lockfile), "--no-deps",
                cwd=self.modules_dir,
            )
            importlib.invalidate_caches()

    async def _load_one(self, name: str, plan: ModuleRequirements) -> None:
        await self._install(name, plan)
        self._started.add(name)
        await self._modules[name].startup()
        self._loaded.add(name)
        logger.success(f"Started module: {name}")

    async def _uninstall(self, name: str) -> None:
        packages = self._claims.get(name)
        if packages is None:
            return
        removable = sorted(
            package for package in packages
            if self._references[package] == {name} and package not in self._baseline
        )
        if removable:
            await run_uv("uninstall", *removable, cwd=self.modules_dir)
            importlib.invalidate_caches()
        for package in packages:
            owners = self._references[package]
            owners.remove(name)
            if not owners:
                del self._references[package]
                del self._versions[package]
        del self._claims[name]

    async def _unload_one(self, name: str) -> None:
        dependents = {
            other for other in self._claims
            if name in self._modules[other].required_modules
        }
        if dependents:
            raise ValueError(f"Module {name} is required by: {', '.join(sorted(dependents))}")
        if name in self._started:
            await self._modules[name].shutdown()
            self._started.remove(name)
            self._loaded.discard(name)
        await self._uninstall(name)

    async def unload(self, name: str) -> None:
        """Unload a module only after all of its dependents have been unloaded."""
        async with self._lock:
            if name not in self._modules:
                raise ValueError(f"Unknown module: {name}")
            await self._unload_one(name)

    async def stop(self) -> None:
        """Unload in reverse dependency order, retaining failed cleanup for retry."""
        async with self._lock:
            errors = []
            graph = self._graph(self._claims.keys() | self._started)
            for name in reversed(tuple(TopologicalSorter(graph).static_order())):
                try:
                    await self._unload_one(name)
                except Exception as exc:
                    errors.append(exc)
            if errors:
                raise ExceptionGroup("Failed to unload modules", errors)


module_manager = ModuleManager()

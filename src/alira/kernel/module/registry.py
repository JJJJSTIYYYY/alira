"""Module declarations, discovery, requirement paths and dependency graphs."""

import importlib
from collections.abc import Collection, Mapping
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from types import ModuleType

from alira.kernel.module.base_module import BaseModule


class ModuleRegistry:
    """Own declaration metadata and dependency graphs without starting services."""

    def __init__(self, directory: Path, package: str):
        self.directory = directory
        self.package = package
        self.modules: dict[str, BaseModule] = {}
        self.directories: dict[str, Path] = {}
        self.unavailable: set[str] = set()

    def register(self, module: BaseModule) -> None:
        if not isinstance(module, BaseModule) or not module._initialized:
            raise TypeError(
                "Module declarations must be initialized BaseModule instances"
            )
        if module.name in self.modules:
            if self.modules[module.name] is module:
                return
            raise ValueError(f"Duplicate module name: {module.name}")
        self.modules[module.name] = module
        self.directories[module.name] = self.directory / module.name

    def discover(self) -> Mapping[str, BaseModule]:
        self.unavailable.clear()
        for path in sorted(self.directory.glob("*/main.py")):
            declaration = importlib.import_module(
                f"{self.package}.{path.parent.name}.main"
            )
            module = self.module_instance(declaration, path)
            if module is None:
                self.unavailable.add(path.parent.name)
                continue
            self.register(module)
            self.directories[module.name] = path.parent
        return self.modules

    @staticmethod
    def module_instance(declaration: ModuleType, path: Path) -> BaseModule | None:
        instances = {
            id(value): value
            for value in vars(declaration).values()
            if isinstance(value, BaseModule) and value._initialized
        }
        if len(instances) > 1:
            raise ValueError(
                f"{path} must declare exactly one initialized BaseModule instance"
            )
        return next(iter(instances.values()), None)

    def graph(self, names: Collection[str]) -> dict[str, set[str]]:
        graph = {}

        def select(name: str) -> None:
            if name in graph:
                return
            if name not in self.modules:
                raise ValueError(f"Unknown module: {name}")
            dependencies = set(self.modules[name].required_modules)
            missing = dependencies - self.modules.keys()
            if missing:
                raise ValueError(
                    f"Module {name} requires unknown modules: {', '.join(sorted(missing))}"
                )
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

    def requirements_path(self, name: str) -> Path | None:
        declared = self.modules[name].requirements_txt
        path = (
            self.directory / declared
            if declared is not None
            else self.directories[name] / "requirements.txt"
        ).resolve()
        if path.is_file():
            return path
        if declared is not None:
            raise FileNotFoundError(
                f"Module {name}: requirements file not found: {path}"
            )
        return None

"""Module declarations, discovery, requirement paths and dependency graphs."""

import importlib
from collections.abc import Collection, Mapping
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from types import ModuleType

from alira.kernel.module.base_module import BaseModule
from alira.kernel.module.dependencies.types import ModuleLoadError


class ModuleRegistry:
    """Own runtime declarations and discovery metadata without starting modules.

    Attributes:
        directory: Root used for runtime entry lookup and default requirements.
        package: Import prefix corresponding to the source root.
        modules: Registered names mapped to initialized BaseModule objects.
        directories: Registered names mapped to their actual source directories.
        unavailable: Entries lacking initialized instances on the last discovery.
    """

    def __init__(self, directory: Path, package: str):
        """Initialize an empty declaration registry.

        Args:
            directory: Module source root, not scanned by construction.
            package: Python import prefix used for <package>.<name>.main.
        """
        self.directory = directory
        self.package = package
        self.modules: dict[str, BaseModule] = {}
        self.directories: dict[str, Path] = {}
        self.unavailable: set[str] = set()

    def register(self, module: BaseModule) -> None:
        """Register a valid initialized instance without invoking its lifecycle.

        Args:
            module: BaseModule object with a completed base constructor.

        Raises:
            ModuleLoadError: Invalid object or another instance owns the same name.
                Registering the same object twice is harmless.
        """
        if not isinstance(module, BaseModule) or not module._initialized:
            raise ModuleLoadError(
                "Module declarations must be initialized BaseModule instances"
            )
        if module.name in self.modules:
            if self.modules[module.name] is module:
                return
            raise ModuleLoadError(f"Duplicate module name: {module.name}")
        self.modules[module.name] = module
        self.directories[module.name] = self.directory / module.name

    def discover(self) -> Mapping[str, BaseModule]:
        """Import main entries in directory order and retain their valid instances.

        Returns:
            Mutable registry mapping. Entries without instances populate unavailable;
            existing registrations are retained and imported modules remain cached.

        Raises:
            ModuleLoadError: Ambiguous instances, duplicate names or directory mismatch.
            Exception: Original entry import failure. Earlier registrations may remain.

        Notes:
            Does not resolve module dependencies or start services. Only unavailable
            is cleared before scanning; this method is not a full registry reset.
        """
        self.unavailable.clear()
        for path in sorted(self.directory.glob("*/main.py")):
            declaration = importlib.import_module(
                f"{self.package}.{path.parent.name}.main"
            )
            module = self.module_instance(declaration, path)
            if module is None:
                self.unavailable.add(path.parent.name)
                continue
            if module.name != path.parent.name:
                raise ModuleLoadError(
                    f"Module name {module.name!r} does not match "
                    f"directory {path.parent.name!r}"
                )
            self.register(module)
            self.directories[module.name] = path.parent
        return self.modules

    @staticmethod
    def module_instance(declaration: ModuleType, path: Path) -> BaseModule | None:
        """Find exactly one distinct initialized instance exported by an entry.

        Args:
            declaration: Imported module whose globals are inspected.
            path: Entry path used for diagnostic errors.

        Returns:
            BaseModule instance, or None if absent. Multiple aliases of the same
            instance count once; uninitialized instances and classes are ignored.

        Raises:
            ModuleLoadError: More than one distinct initialized instance is exported.
        """
        instances = {
            id(value): value
            for value in vars(declaration).values()
            if isinstance(value, BaseModule) and value._initialized
        }
        if len(instances) > 1:
            raise ModuleLoadError(
                f"{path} must declare exactly one initialized BaseModule instance"
            )
        return next(iter(instances.values()), None)

    def graph(self, names: Collection[str]) -> dict[str, set[str]]:
        """Validate and return the prerequisite closure of registered names.

        Args:
            names: Initial registered names; empty input selects nothing.

        Returns:
            Dict mapping each selected or prerequisite name to a set of prerequisites.

        Raises:
            ModuleLoadError: A name/prerequisite is unknown or the closure is cyclic.

        Notes:
            Unselected modules outside the closure are not validated. No lifecycle
            hooks, dependency resolution or package installation are performed.
        """
        graph = {}

        def select(name: str) -> None:
            """Accumulate one registered name and reject missing prerequisites immediately."""
            if name in graph:
                return
            if name not in self.modules:
                raise ModuleLoadError(f"Unknown module: {name}")
            dependencies = set(self.modules[name].required_modules)
            missing = dependencies - self.modules.keys()
            if missing:
                raise ModuleLoadError(
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
            raise ModuleLoadError(f"Module dependency cycle: {exc.args[1]}") from exc
        return graph

    def requirements_path(self, name: str) -> Path | None:
        """Resolve a registered module's explicit or default requirements file.

        Args:
            name: Name already present in modules and directories.

        Returns:
            Resolved existing file path, or None if the default requirements.txt is absent.
            Explicit relative paths are based on the module source root.

        Raises:
            FileNotFoundError: An explicit requirements path is not an existing file.
            KeyError: The caller supplied an unregistered name.
        """
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

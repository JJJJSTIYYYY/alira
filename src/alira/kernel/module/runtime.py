"""Concurrent module startup and reverse dependency cleanup."""

import asyncio
from graphlib import TopologicalSorter

from alira.kernel.module.dependencies.types import ModuleLoadError, sync_hint
from alira.kernel.module.registry import ModuleRegistry
from alira.shared.logger import logger


class ModuleRuntime:
    """Track runtime transitions after the caller verifies and locks the graph."""

    def __init__(self, registry: ModuleRegistry):
        self.registry = registry
        self.loaded: set[str] = set()
        self.startup_attempted: set[str] = set()

    async def load(self, graph: dict[str, set[str]], pending: set[str]) -> None:
        """Start a verified graph; retain cleanup state after failed transitions."""
        sorter = TopologicalSorter(
            {name: graph[name] & pending for name in sorted(pending)}
        )
        sorter.prepare()
        try:
            async with asyncio.TaskGroup() as group:
                tasks = {}
                while sorter.is_active():
                    for name in sorter.get_ready():
                        tasks[
                            group.create_task(
                                self._start_one(name), name=f"module:{name}"
                            )
                        ] = name
                    done, _ = await asyncio.wait(
                        tasks, return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in done:
                        task.result()
                        sorter.done(tasks.pop(task))
        except BaseException as exc:
            errors = []
            for name in reversed(tuple(TopologicalSorter(graph).static_order())):
                if name in pending and name not in self.loaded:
                    try:
                        await self._stop_one(name)
                    except Exception as cleanup_error:  # noqa: BLE001 - collect all module cleanup failures
                        errors.append(cleanup_error)
            if errors:
                raise BaseExceptionGroup(
                    "Module load and cleanup failed", [exc, *errors]
                ) from None
            raise

    async def _start_one(self, name: str) -> None:
        self.startup_attempted.add(name)
        try:
            await self.registry.modules[name].startup()
        except ImportError as exc:
            raise ModuleLoadError(
                f"missing or incompatible startup dependency {exc.name or '(unknown)'}: {exc}",
                module=name,
                missing=exc.name,
                hint=sync_hint(),
            ) from exc
        self.loaded.add(name)
        logger.success(f"[module:{name}] started")

    async def _stop_one(self, name: str) -> None:
        """Stop services; Python imports/handlers remain cached, so this is not hot reload.

        Deployed dependency ownership persists until an explicit remove operation.
        Module shutdown must unregister its own handlers and release its resources.
        """
        dependents = {
            other
            for other in self.startup_attempted
            if name in self.registry.modules[other].required_modules
        }
        if dependents:
            raise ValueError(
                f"Module {name} is required by: {', '.join(sorted(dependents))}"
            )
        if name in self.startup_attempted:
            await self.registry.modules[name].shutdown()
            self.startup_attempted.remove(name)
            self.loaded.discard(name)

    async def unload(self, name: str) -> None:
        if name not in self.registry.modules:
            raise ValueError(f"Unknown module: {name}")
        await self._stop_one(name)

    async def stop(self) -> None:
        errors = []
        graph = self.registry.graph(self.startup_attempted)
        for name in reversed(tuple(TopologicalSorter(graph).static_order())):
            try:
                await self._stop_one(name)
            except Exception as exc:  # noqa: BLE001 - stop every module before reporting failures
                errors.append(exc)
        if errors:
            raise ExceptionGroup("Failed to unload modules", errors)

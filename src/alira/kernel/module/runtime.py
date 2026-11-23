"""Concurrent module startup and reverse dependency cleanup."""

import asyncio
from dataclasses import dataclass
from graphlib import TopologicalSorter
from typing import Literal

from alira.kernel.module.dependencies.types import ModuleLoadError, sync_hint
from alira.kernel.module.registry import ModuleRegistry
from alira.shared.logger import logger


@dataclass
class _LoadPlan:
    """Display one dependency path and its results; never drive scheduling."""

    modules: tuple[str, ...]
    results: tuple[Literal["success", "failed", "skipped"], ...] = ()

    def render(self, loaded: set[str], failed: set[str]) -> str:
        """Record final outcomes and format failed/skipped modules with ANSI colors."""
        self.results = tuple(
            "success" if name in loaded else "failed" if name in failed else "skipped"
            for name in self.modules
        )
        chain = " -> ".join(
            name
            if result == "success"
            else (
                f"\033[31m{name}[x]\033[0m"
                if result == "failed"
                else f"\033[90m{name}\033[0m"
            )
            for name, result in zip(self.modules, self.results)
        )
        result = (
            "success" if all(item == "success" for item in self.results) else "failed"
        )
        return f"modules load {result}: {chain}"


def _load_plans(graph: dict[str, set[str]]) -> list[_LoadPlan]:
    """Expand maximal dependency paths in selection order for reporting only."""
    plans = []
    prerequisites = {
        dependency for dependencies in graph.values() for dependency in dependencies
    }

    def visit(name: str, suffix: tuple[str, ...]) -> None:
        path = (name, *suffix)
        if graph[name]:
            for dependency in sorted(graph[name]):
                visit(dependency, path)
        else:
            plans.append(_LoadPlan(path))

    for name in graph:
        if name not in prerequisites:
            visit(name, ())
    return plans


class ModuleRuntime:
    """Load one selected module graph and close its resources at process exit.

    Attributes:
        registry: Declaration owner supplying module hooks and dependency metadata.
        loaded: Names whose startup succeeded and have not been closed.

    ModuleManager must serialize callers and verify graphs/dependencies first.
    This component does not install Python packages or provide its own lock.
    """

    def __init__(self, registry: ModuleRegistry):
        """Initialize empty runtime state for registry without invoking module hooks."""
        self.registry = registry
        self.loaded: set[str] = set()
        self._started = False
        self._shutdown_order: list[str] = []

    @property
    def active(self) -> bool:
        """Whether module startup or running resources still need their shutdown hook."""
        return bool(self._shutdown_order)

    async def load(self, graph: dict[str, set[str]]) -> None:
        """Start one verified graph with dependency-aware concurrency.

        Args:
            graph: Acyclic mapping of registered names to known prerequisites.

        Raises:
            RuntimeError: This runtime has already attempted its one startup.
            asyncio.CancelledError: The operation was cancelled; cleanup is attempted.

        Notes:
            A failed startup skips its dependents without cancelling other branches.
            Startup exceptions are logged immediately and contained in this runtime;
            they do not terminate the application or stop successful prerequisites.
            All dependency paths and their results are printed together after tasks
            finish. These display plans do not control the topological scheduler.
            Failed or cancelled startups immediately close their partial resources.
            Successful modules remain running until stop; there is no runtime reload,
            individual unload or cleanup retry.
        """
        if self._started:
            raise RuntimeError("Modules can only be loaded once per application run")
        self._started = True
        plans = _load_plans(graph)
        failed: set[str] = set()

        sorter = TopologicalSorter({name: graph[name] for name in sorted(graph)})
        sorter.prepare()
        try:
            async with asyncio.TaskGroup() as group:
                tasks = {}
                while sorter.is_active():
                    for name in sorter.get_ready():
                        if not graph[name] <= self.loaded:
                            sorter.done(name)
                            continue
                        task = group.create_task(
                            self._start_one(name, failed), name=f"module:{name}"
                        )
                        tasks[task] = name
                    if not tasks:
                        continue
                    done, _ = await asyncio.wait(
                        tasks, return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in done:
                        task.result()
                        sorter.done(tasks.pop(task))
        finally:
            if plans:
                print("\n".join(plan.render(self.loaded, failed) for plan in plans))

    async def _start_one(self, name: str, failed: set[str]) -> None:
        """Own one startup's state, error reporting and partial-resource cleanup.

        Import errors receive a repair hint. Ordinary errors are logged immediately
        and added to the load's reporting set; cancellation propagates after cleanup.
        Successful modules remain in startup order until application shutdown.
        """
        self._shutdown_order.append(name)
        try:
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
        except Exception:  # noqa: BLE001 - isolate module startup failures at the kernel boundary
            failed.add(name)
            logger.exception(f"[module:{name}] startup failed")
        except asyncio.CancelledError:
            failed.add(name)
            raise
        finally:
            if name not in self.loaded:
                try:
                    await self._shutdown_one(name)
                finally:
                    self._shutdown_order.remove(name)

    async def _shutdown_one(self, name: str) -> None:
        """Finish one shutdown even if its caller is cancelled, logging ordinary errors."""

        async def shutdown() -> None:
            try:
                await self.registry.modules[name].shutdown()
            except Exception:  # noqa: BLE001 - process shutdown must continue after module cleanup failures
                logger.exception(f"[module:{name}] shutdown failed")
            finally:
                self.loaded.discard(name)

        cleanup = asyncio.create_task(shutdown(), name=f"module:{name}:shutdown")
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise

    async def stop(self) -> None:
        """Close remaining modules once in reverse startup order.

        Raises:
            asyncio.CancelledError: Application shutdown was interrupted.

        Startup order already respects dependencies, so reversing it closes dependents
        first without rebuilding the graph. Ordinary errors are logged immediately and
        remaining modules are still closed. Repeated stop calls have nothing to do.
        """
        while self._shutdown_order:
            await self._shutdown_one(self._shutdown_order.pop())

"""Concurrent module startup and reverse dependency cleanup."""

import asyncio
from dataclasses import dataclass
from graphlib import TopologicalSorter

from alira.kernel.module.dependencies.types import ModuleLoadError, sync_hint
from alira.kernel.module.runtime.registry import ModuleRegistry
from alira.shared.logger import logger


@dataclass
class _LoadPlan:
    """Display one compact dependency expression; never drive scheduling."""

    parts: tuple[str, ...]
    modules: tuple[str, ...]

    def render(self, loaded: set[str], failed: set[str]) -> str:
        """Format module outcomes while preserving branch grouping and references."""
        labels = {
            name: name
            if name in loaded
            else (
                f"\033[31m{name}[x]\033[0m"
                if name in failed
                else f"\033[90m{name}\033[0m"
            )
            for name in self.modules
        }
        expression = "".join(labels.get(part, part) for part in self.parts)
        result = "success" if all(name in loaded for name in self.modules) else "failed"
        return f"modules load {result}: {expression}"


def _load_plans(graph: dict[str, set[str]]) -> list[_LoadPlan]:
    """Build compact expressions iteratively without enumerating dependency paths.

    Each shared non-leaf prerequisite gets its own expression; consumers reference
    its name instead of expanding it again. Other prerequisites are grouped at
    joins. Every non-leaf is expanded once and every edge produces at most one
    reference, bounding display storage by O(V + E), including nested diamonds.
    """
    consumers = dict.fromkeys(graph, 0)
    for dependencies in graph.values():
        for name in dependencies:
            consumers[name] += 1
    shared = {name for name in graph if consumers[name] > 1 and graph[name]}
    roots = [name for name in graph if not consumers[name] or name in shared]
    plans = []
    for root in roots:
        parts = []
        pending = [(root, True)]
        while pending:
            name, expand = pending.pop()
            if not expand or not graph[name] or (name != root and name in shared):
                parts.append(name)
                continue
            dependencies = sorted(graph[name])
            pending.append((name, False))
            pending.append((" -> ", False))
            if len(dependencies) > 1:
                pending.append((")", False))
            for index in range(len(dependencies) - 1, -1, -1):
                pending.append((dependencies[index], True))
                if index:
                    pending.append((", ", False))
            if len(dependencies) > 1:
                pending.append(("(", False))
        plans.append(
            _LoadPlan(tuple(parts), tuple(part for part in parts if part in graph))
        )
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
            Compact dependency expressions and their results are printed after tasks
            finish. Shared subgraphs are expanded once and referenced by name.
            These display plans do not control the topological scheduler.
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

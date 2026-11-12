"""Concurrent module startup and reverse dependency cleanup."""

import asyncio
from graphlib import TopologicalSorter

from alira.kernel.module.dependencies.types import ModuleLoadError, sync_hint
from alira.kernel.module.registry import ModuleRegistry
from alira.shared.logger import logger


class ModuleRuntime:
    """Run verified module lifecycles while retaining incomplete cleanup state.

    Attributes:
        registry: Declaration owner supplying module hooks and dependency metadata.
        loaded: Names whose startup succeeded and shutdown has not succeeded.
        startup_attempted: Names needing cleanup, including partial startup failures.

    ModuleManager must serialize callers and verify graphs/dependencies first.
    This component does not install Python packages or provide its own lock.
    """

    def __init__(self, registry: ModuleRegistry):
        """Initialize empty runtime state for registry without invoking module hooks."""
        self.registry = registry
        self.loaded: set[str] = set()
        self.startup_attempted: set[str] = set()

    async def load(self, graph: dict[str, set[str]], pending: set[str]) -> None:
        """Start a verified pending set with dependency-aware concurrency.

        Args:
            graph: Acyclic mapping of registered names to known prerequisites.
            pending: Names in graph that still require startup. Existing loaded
                prerequisites are treated as ready.

        Raises:
            BaseExceptionGroup: Startup tasks failed, or startup and cleanup both failed.
            asyncio.CancelledError: The operation was cancelled; cleanup is attempted.

        Notes:
            Successfully started modules remain loaded after another task fails.
            Attempted modules not marked loaded are stopped in reverse graph order.
            Failed shutdowns remain tracked so callers can explicitly retry cleanup.
        """
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
        """Track one startup attempt, wrap ImportError as ModuleLoadError and mark success loaded."""
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
        """Stop one module only when no attempted dependents remain.

        A successful shutdown clears both runtime sets. Failure/cancellation preserves
        retry state. Import caches and dependency claims remain untouched; modules
        must unregister handlers and release owned resources themselves.
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
        """Invoke cleanup for one registered module, if it has pending state.

        Args:
            name: Registered module identifier.

        Raises:
            ValueError: Unknown name or an attempted dependent prevents shutdown.
            Exception: The shutdown hook failed; tracked state is kept for retry.
        """
        if name not in self.registry.modules:
            raise ValueError(f"Unknown module: {name}")
        await self._stop_one(name)

    async def stop(self) -> None:
        """Attempt reverse-dependency cleanup for every attempted module.

        Raises:
            ExceptionGroup: Ordinary shutdown failures, collected after other eligible
                modules have been attempted. Failed dependents protect prerequisites.
            asyncio.CancelledError: Shutdown was cancelled; remaining state is retained.

        Successfully cleaned modules leave both runtime sets; registrations persist.
        """
        errors = []
        graph = self.registry.graph(self.startup_attempted)
        for name in reversed(tuple(TopologicalSorter(graph).static_order())):
            try:
                await self._stop_one(name)
            except Exception as exc:  # noqa: BLE001 - stop every module before reporting failures
                errors.append(exc)
        if errors:
            raise ExceptionGroup("Failed to unload modules", errors)

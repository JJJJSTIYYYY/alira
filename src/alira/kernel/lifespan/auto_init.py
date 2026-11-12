"""Ordered lifecycle management for application-owned services."""

import asyncio
from typing import Protocol

from alira.shared.logger import logger


class LifecycleService(Protocol):
    """Structural contract for a kernel-owned asynchronous service.

    Implement start and stop without arguments. Start returns when the service
    is ready; stop releases complete or partially acquired resources. Hooks must
    support repeated calls and retry after failure or cancellation.
    """

    async def start(self) -> None:
        """Acquire service resources and return when ready; propagate startup failures."""
        ...

    async def stop(self) -> None:
        """Release service resources, including partial startup; allow cleanup retries."""
        ...


class AutoInit:
    """Process-wide ordered lifecycle registry for application services.

    Services start in registration order and stop in reverse order. Each transition
    is serialized on the owning event loop. Successful starts and incomplete
    cleanup are tracked separately; ordinary hook failures are grouped, while
    cancellation propagates immediately with cleanup state retained.

    Use auto_init rather than constructing independent production registries.
    Construction and registration do not invoke lifecycle hooks.
    """

    _instance = None

    def __new__(cls):
        """Return the process registry singleton without replacing an existing instance."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        """Initialize service and retry bookkeeping once; repeated construction preserves it."""
        if getattr(self, "_initialized", False):
            return

        self._services: list[LifecycleService] = []
        self._started_services: set[int] = set()
        self._pending_stop: set[int] = set()
        self._lock = asyncio.Lock()
        self._initialized = True

    @classmethod
    def _new_for_tests(cls):
        """Create an isolated registry without replacing the process singleton."""
        registry = object.__new__(cls)
        registry.__init__()
        return registry

    def register(self, service: LifecycleService) -> None:
        """Append a service once by object identity, preserving startup order.

        Args:
            service: Object with callable start and stop methods whose results can
                be awaited. The same instance may be registered repeatedly as a no-op.

        Raises:
            AttributeError: A required lifecycle method is absent.
            TypeError: A required method is not callable.

        Notes:
            Does not start the service or verify whether its call result is awaitable.
            Services registered after a start are picked up by the next start call.
        """
        for method in ("start", "stop"):
            if not hasattr(service, method):
                raise AttributeError(
                    f"{service.__class__.__name__} missing {method}() method"
                )
            if not callable(getattr(service, method)):
                raise TypeError(
                    f"{service.__class__.__name__}.{method} must be callable"
                )

        if any(existing is service for existing in self._services):
            return

        self._services.append(service)
        logger.debug(f"Registered service: {service.__class__.__name__}")

    async def start(self, *, restart: bool = False) -> None:
        """Start eligible services in registration order, collecting ordinary errors.

        Args:
            restart: Stop all services in reverse order before starting again. A
                failed stop aborts restart so old resources are not reopened unsafely.

        Raises:
            ExceptionGroup: One or more lifecycle hooks failed. Ordinary start errors
                do not prevent later services from being attempted; successes remain
                running. The group contains the original service exceptions.
            asyncio.CancelledError: Startup was cancelled; pending cleanup is retained.

        Notes:
            Already started services are skipped unless restarting. A service with a
            previous failed/cancelled start is stopped before another start attempt.
            Hooks are awaited sequentially, and the registry lock covers the transition.
        """
        async with self._lock:
            if restart:
                # Do not reopen resources while any previous cleanup has failed.
                await self._stop_services()

            errors: list[Exception] = []
            for service in self._services:
                key = id(service)
                if key in self._started_services:
                    continue
                try:
                    if key in self._pending_stop:
                        await self._stop_service(service)
                    # start() may acquire resources before raising or cancelling.
                    self._pending_stop.add(key)
                    await service.start()
                    self._started_services.add(key)
                    logger.debug(f"Started: {service.__class__.__name__}")
                except Exception as exc:  # noqa: BLE001 - aggregate service failures and continue cleanup
                    logger.exception(f"Error starting {service.__class__.__name__}")
                    errors.append(exc)

            if errors:
                raise ExceptionGroup("Failed to start services", errors)

    async def stop(self) -> None:
        """Stop all registered services in reverse order, even if not started here.

        Raises:
            ExceptionGroup: Ordinary cleanup errors, raised after attempting remaining
                services. Failed cleanup is tracked for a later stop/start retry.
            asyncio.CancelledError: Cleanup was cancelled; its pending state is retained.

        Notes:
            Services started directly by a caller are also stopped. Successful cleanup
            clears start/pending flags but does not remove services from registration.
        """
        async with self._lock:
            await self._stop_services()

    async def _stop_service(self, service: LifecycleService) -> None:
        """Mark cleanup pending, await one stop hook, and clear pending state only on success."""
        key = id(service)
        self._started_services.discard(key)
        self._pending_stop.add(key)
        logger.debug(f"Stopping: {service.__class__.__name__}")
        await service.stop()
        self._pending_stop.discard(key)

    async def _stop_services(self) -> None:
        """Attempt every reverse-order cleanup and group ordinary failures; caller holds the lock."""
        errors: list[Exception] = []
        for service in reversed(self._services):
            try:
                await self._stop_service(service)
            except Exception as exc:  # noqa: BLE001 - aggregate service failures and continue cleanup
                logger.exception(f"Error stopping {service.__class__.__name__}")
                errors.append(exc)
        if errors:
            raise ExceptionGroup("Failed to stop services", errors)


auto_init = AutoInit()

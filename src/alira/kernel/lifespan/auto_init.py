"""Ordered lifecycle management for application-owned services."""

from typing import Protocol

from alira.shared.logger import logger


class LifecycleService(Protocol):
    """Structural contract for a kernel-owned asynchronous service.

    Implement start and stop without arguments. Start returns when the service
    is ready; stop releases complete or partially acquired resources. Stop must
    also tolerate a registered service whose startup was never attempted.
    """

    async def start(self) -> None:
        """Acquire service resources and return when ready; propagate startup failures."""
        ...

    async def stop(self) -> None:
        """Release service resources, including partial or unattempted startup."""
        ...


class AutoInit:
    """Process-wide ordered lifecycle registry for application services.

    Services start once in registration order and stop in reverse order. A startup
    failure is logged and propagated immediately; the application's lifespan owns
    final cleanup. Shutdown logs individual failures and continues closing services.

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
        """Initialize the service list and one-time startup flag once."""
        if getattr(self, "_initialized", False):
            return

        self._services: list[LifecycleService] = []
        self._started = False
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
            Services registered during startup are started in the same pass. Later
            registrations are retained for shutdown, including services started
            directly by their owning module.
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

    async def start(self) -> None:
        """Start registered services once, awaiting each service's readiness.

        Raises:
            RuntimeError: Startup was already attempted on this registry.
            Exception: A service failed to start. The original exception is logged
                immediately and propagated; later services are not started.
            asyncio.CancelledError: Startup was cancelled.

        Notes:
            The application must call stop in its finally block, including after a
            failed or cancelled startup. A stopped registry cannot be restarted.
        """
        if self._started:
            raise RuntimeError("Kernel services can only be started once")
        self._started = True
        for service in self._services:
            try:
                await service.start()
                logger.debug(f"Started: {service.__class__.__name__}")
            except Exception:
                logger.exception(f"Error starting {service.__class__.__name__}")
                raise

    async def stop(self) -> None:
        """Close and consume all registered services in reverse order.

        Raises:
            asyncio.CancelledError: Cleanup was cancelled; services not yet visited
                remain registered.

        Notes:
            Services started directly by a caller are also stopped. Each service is
            removed before its hook runs. Ordinary failures are logged immediately
            and do not prevent later cleanup; there is no automatic retry. Calling
            stop again after completion has no effect.
        """
        while self._services:
            service = self._services.pop()
            try:
                logger.debug(f"Stopping: {service.__class__.__name__}")
                await service.stop()
            except Exception:  # noqa: BLE001 - close the remaining services after logging a shutdown failure
                logger.exception(f"Error stopping {service.__class__.__name__}")


auto_init = AutoInit()

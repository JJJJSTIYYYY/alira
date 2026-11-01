"""Ordered, grouped lifecycle management for application-owned services."""

import asyncio
from typing import Protocol

from apixis import start_core, stop_core
from alira.common.utils.logger import logger


class LifecycleService(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class AutoInit:
    """Singleton registry; services must support repeated start/stop calls.

    Each service belongs to one group. Lifecycle operations are serialized on
    the application event loop. Successful starts are remembered individually;
    failed or cancelled transitions retain cleanup state so they can be retried.
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return

        self._services: list[LifecycleService] = []
        self._service_groups: dict[int, str] = {}
        self._started_services: set[int] = set()
        self._pending_stop: set[int] = set()
        self._lock = asyncio.Lock()
        self._initialized = True

    def register(self, service: LifecycleService, group: str = "default") -> None:
        """Register once by identity, preserving global registration order.

        Re-registering the same instance in another group raises ValueError.
        """
        if not isinstance(group, str) or not group.strip():
            raise ValueError("Service group must be a non-empty string")

        for method in ("start", "stop"):
            if not hasattr(service, method):
                raise AttributeError(
                    f"{service.__class__.__name__} missing {method}() method"
                )
            if not callable(getattr(service, method)):
                raise TypeError(f"{service.__class__.__name__}.{method} must be callable")

        key = id(service)
        if key in self._service_groups:
            if self._service_groups[key] != group:
                raise ValueError("Service is already registered in another group")
            return

        self._services.append(service)
        self._service_groups[key] = group
        logger.debug(f"Registered service: {service.__class__.__name__} ({group})")

    def _select_services(self, group: str | None) -> list[LifecycleService]:
        if group is None:
            return list(self._services)
        if group not in self._service_groups.values():
            raise ValueError(f"Unknown service group: {group!r}")
        return [
            service for service in self._services
            if self._service_groups[id(service)] == group
        ]

    async def start(self, group: str | None = None, *, restart: bool = False) -> None:
        """Start selected services in registration order (None selects all).

        Already started services are skipped. Use restart=True after a runtime
        failure to stop the selection in reverse order before starting it again.
        A failed start is cleaned up before its next attempt. Ordinary failures
        are collected in an ExceptionGroup; successful services remain running.
        """
        async with self._lock:
            services = self._select_services(group)
            if restart:
                # Do not reopen resources while any previous cleanup has failed.
                await self._stop_services(services)

            errors: list[Exception] = []
            for service in services:
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
                except Exception as exc:
                    logger.exception(f"Error starting {service.__class__.__name__}")
                    errors.append(exc)

            if errors:
                raise ExceptionGroup("Failed to start services", errors)

    async def stop(self, group: str | None = None) -> None:
        """Stop selected services in reverse registration order.

        Also stop services started directly by their callers. Ordinary failures
        do not prevent the remaining services from stopping and are raised as an
        ExceptionGroup. Failed or cancelled cleanup can be retried.
        """
        async with self._lock:
            await self._stop_services(self._select_services(group))

    async def _stop_service(self, service: LifecycleService) -> None:
        key = id(service)
        self._started_services.discard(key)
        self._pending_stop.add(key)
        await service.stop()
        self._pending_stop.discard(key)
        logger.debug(f"Stopped: {service.__class__.__name__}")

    async def _stop_services(self, services: list[LifecycleService]) -> None:
        errors: list[Exception] = []
        for service in reversed(services):
            try:
                await self._stop_service(service)
            except Exception as exc:
                logger.exception(f"Error stopping {service.__class__.__name__}")
                errors.append(exc)
        if errors:
            raise ExceptionGroup("Failed to stop services", errors)


class ApixisCore:
    """first registered service to start/stop the apixis core automatically."""

    async def start(self):
        await start_core()

    async def stop(self):
        await stop_core()

    def get_version(self) -> str:
        from apixis import VERSION
        return VERSION

apixis_core = ApixisCore()


auto_init = AutoInit()
auto_init.register(logger, group="core")
auto_init.register(apixis_core, group="core")
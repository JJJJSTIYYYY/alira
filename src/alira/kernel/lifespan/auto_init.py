"""Ordered lifecycle management for application-owned services."""

import asyncio
from typing import Protocol

from apixis import start_core, stop_core
from alira.utils.logger import logger


class LifecycleService(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class AutoInit:
    """Singleton registry; services must support repeated start/stop calls.

    Lifecycle operations are serialized on the application event loop.
    Successful starts are remembered individually;
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
        self._started_services: set[int] = set()
        self._pending_stop: set[int] = set()
        self._lock = asyncio.Lock()
        self._initialized = True

    def register(self, service: LifecycleService) -> None:
        """Register once by identity, preserving registration order."""
        for method in ("start", "stop"):
            if not hasattr(service, method):
                raise AttributeError(
                    f"{service.__class__.__name__} missing {method}() method"
                )
            if not callable(getattr(service, method)):
                raise TypeError(f"{service.__class__.__name__}.{method} must be callable")

        if any(existing is service for existing in self._services):
            return

        self._services.append(service)
        logger.debug(f"Registered service: {service.__class__.__name__}")

    async def start(self, *, restart: bool = False) -> None:
        """Start services in registration order, including newly registered ones.

        Already started services are skipped. Use restart=True after a runtime
        failure to stop all services in reverse order before starting them again.
        A failed start is cleaned up before its next attempt. Ordinary failures
        are collected in an ExceptionGroup; successful services remain running.
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
                except Exception as exc:
                    logger.exception(f"Error starting {service.__class__.__name__}")
                    errors.append(exc)

            if errors:
                raise ExceptionGroup("Failed to start services", errors)

    async def stop(self) -> None:
        """Stop services in reverse registration order.

        Also stop services started directly by their callers. Ordinary failures
        do not prevent the remaining services from stopping and are raised as an
        ExceptionGroup. Failed or cancelled cleanup can be retried.
        """
        async with self._lock:
            await self._stop_services()

    async def _stop_service(self, service: LifecycleService) -> None:
        key = id(service)
        self._started_services.discard(key)
        self._pending_stop.add(key)
        logger.debug(f"Stopping: {service.__class__.__name__}")
        await service.stop()
        self._pending_stop.discard(key)

    async def _stop_services(self) -> None:
        errors: list[Exception] = []
        for service in reversed(self._services):
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

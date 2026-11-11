"""Schedule registered application resource cleanup callbacks."""

import asyncio
from collections.abc import Awaitable, Callable
from functools import wraps

from alira.shared.application.config import CACHE_CLEAN_INTERVAL
from alira.shared.logger import logger


class ResourceCleaner:
    """
    Global resource cleaner (singleton).

    Features:
        - Centralized cleanup scheduler
        - Supports async / sync cleanup functions
        - Auto registration via decorator
        - Fault isolation between cleanup tasks
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return

        self._cleaners: list[Callable[[], int | Awaitable[int]]] = []
        self._task: asyncio.Task | None = None
        self._interval = CACHE_CLEAN_INTERVAL
        self._running = False
        self._initialized = True

    @classmethod
    def _new_for_tests(cls):
        """Create an isolated cleaner without replacing the process singleton."""
        cleaner = object.__new__(cls)
        cleaner.__init__()
        return cleaner

    def register(self, func: Callable):
        """
        Register a cleanup function manually.

        Args:
            func: Function returning int or Awaitable[int]
        """
        if func not in self._cleaners:
            self._cleaners.append(func)
            logger.debug(f"Registered: {func.__name__}")

    def auto_clear(self, func: Callable):
        """
        Decorator to auto-register a cleanup function.

        Usage:
            @cleaner.auto_clear
            async def clean_xxx():
                return removed_count
        """

        self.register(func)

        @wraps(func)
        async def wrapper(*args, **kwargs):
            return await func(*args, **kwargs)

        return wrapper

    async def start(self):
        """
        Start background cleanup loop.

        """
        interval = CACHE_CLEAN_INTERVAL or 30

        if self._running:
            return

        self._interval = interval
        self._running = True

        self._task = asyncio.create_task(self._loop(), name="resource-cleaner")

    async def stop(self):
        """
        Stop background cleanup loop.
        """
        if not self._running:
            return

        self._running = False

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

            self._task = None

    async def _loop(self):
        """
        Background loop.
        """
        try:
            while self._running:
                await asyncio.sleep(self._interval)
                await self.run_once()
        except asyncio.CancelledError:
            logger.debug("Cleanup loop cancelled")

    async def run_once(self):
        """
        Execute all cleanup functions once.
        """
        if not self._cleaners:
            return

        total_removed = 0

        for func in list(self._cleaners):
            try:
                result = func()

                if asyncio.iscoroutine(result):
                    result = await result

                if isinstance(result, int):
                    total_removed += result

            except Exception as e:  # noqa: BLE001 - isolate failures between cleanup callbacks
                logger.error(f"Error in {func.__name__}: {e}")

        if total_removed:
            logger.info(f"Cleaned total={total_removed}")


resource_cleaner = ResourceCleaner()

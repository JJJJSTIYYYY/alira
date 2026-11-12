"""Schedule registered application resource cleanup callbacks."""

import asyncio
from collections.abc import Awaitable, Callable
from functools import wraps

from alira.shared.application.config import CACHE_CLEAN_INTERVAL
from alira.shared.logger import logger


class ResourceCleaner:
    """Singleton scheduler for application-owned cleanup callbacks.

    Callbacks execute sequentially in registration order and take no arguments.
    They may return an int count or a coroutine resolving to one. Exceptions from
    one callback are logged so remaining callbacks can run; cancellation propagates
    out of a pass. Synchronous callbacks run on the event-loop thread and must not
    block it. Registration alone does not start the background loop.
    """

    _instance = None

    def __new__(cls):
        """Return the process-wide cleanup scheduler instance."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        """Initialize callbacks and scheduling state once, without starting a task."""
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
        """Register a zero-argument synchronous or asynchronous cleanup callback.

        Args:
            func: Callable returning an optional int count, or a coroutine resolving
                to that result. Counts are used for aggregate logging, not returned
                by run_once. The same callable is only appended if not already present.

        Notes:
            No signature/result validation is performed at registration. Use this
            method for synchronous callbacks; auto_clear wraps asynchronous functions.
        """
        if func not in self._cleaners:
            self._cleaners.append(func)
            logger.debug(f"Registered: {func.__name__}")

    def auto_clear(self, func: Callable):
        """Register an async cleanup function and return a metadata-preserving wrapper.

        Args:
            func: Async callable that can be invoked without arguments by the scheduler
                and may return an int count. The original function is registered.

        Returns:
            Async wrapper forwarding its arguments to func and awaiting its result.
            Use register instead for a synchronous cleanup callback.

        Example:
            @resource_cleaner.auto_clear
            async def expire_cache():
                return 0
        """

        self.register(func)

        @wraps(func)
        async def wrapper(*args, **kwargs):
            """Await the decorated callback with the caller-supplied arguments."""
            return await func(*args, **kwargs)

        return wrapper

    async def start(self):
        """Start one background cleanup loop if it is not already running.

        The interval is read from CACHE_CLEAN_INTERVAL each time the loop is started;
        a false value selects 30 seconds. The first cleanup runs after that interval.
        Repeated start calls while running do nothing.

        Returns:
            None after scheduling the task; no cleanup pass is awaited here.
        """
        interval = CACHE_CLEAN_INTERVAL or 30

        if self._running:
            return

        self._interval = interval
        self._running = True

        self._task = asyncio.create_task(self._loop(), name="resource-cleaner")

    async def stop(self):
        """Cancel and join the background loop, preserving registered callbacks.

        Returns:
            None after the loop stops, or immediately if it was not running.

        Notes:
            Cancellation may interrupt the current callback. The callback is responsible
            for releasing its own resources. No final cleanup pass is run on shutdown.
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
        """Sleep between cleanup passes until stopped; log cancellation of the scheduler."""
        try:
            while self._running:
                await asyncio.sleep(self._interval)
                await self.run_once()
        except asyncio.CancelledError:
            logger.debug("Cleanup loop cancelled")

    async def run_once(self):
        """Execute a snapshot of registered callbacks once, in order.

        Coroutine results are awaited; integer results contribute to a logged total.
        Other return values are ignored. Ordinary callback exceptions are logged and
        do not prevent later callbacks from running. Registrations made during the
        pass take effect on the next pass.

        Returns:
            None; removed-item totals are diagnostic logging only.

        Raises:
            asyncio.CancelledError: The caller or active callback was cancelled.

        Notes:
            May be invoked without starting the background scheduler. Passes are not
            internally serialized; avoid overlapping manual and scheduled invocation.
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

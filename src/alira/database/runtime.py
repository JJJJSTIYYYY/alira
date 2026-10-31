"""Store services and request workers, accessed only on their owning thread."""

import asyncio
from concurrent.futures import CancelledError as QueryCancelledError
from concurrent.futures import Future
from pathlib import Path

from alira.database.executor import StoreExecutor
from alira.database.services.cache import CacheService
from alira.database.services.data import DataService
from alira.database.services.files import FileService
from alira.common.utils.logger import logger


class _StoreRuntime:
    def __init__(
        self,
        *,
        db_path: str | Path | None,
        cache_path: str | Path | None,
        worker_count: int,
    ):
        self._cache = CacheService(cache_path)
        self._data = DataService(db_path)
        self._executor = StoreExecutor(
            cache_store=self._cache,
            data_store=self._data,
            file_store=FileService(),
        )
        self._handlers = self._executor.export_handlers()
        self._worker_count = worker_count
        self._workers: list[asyncio.Task[None]] = []
        self._queue: asyncio.Queue[tuple[Future[dict], str, dict]] = asyncio.Queue()
        self._started_services: list[CacheService | DataService] = []

    async def start(self) -> None:
        for service in (self._cache, self._data):
            await service.start()
            self._started_services.append(service)
        self._workers = [
            asyncio.create_task(self._worker_loop(), name=f"store-worker-{index}")
            for index in range(self._worker_count)
        ]

    async def stop(self) -> None:
        for worker in self._workers:
            worker.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        while True:
            try:
                future, _, _ = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            future.cancel()
            self._queue.task_done()

        errors = []
        for service in reversed(self._started_services):
            try:
                await service.stop()
            except Exception as exc:  # noqa: BLE001 - close every owned service
                logger.exception(
                    f"Store shutdown failed for {type(service).__name__}: {exc}"
                )
                errors.append(exc)
        self._started_services.clear()
        if errors:
            raise ExceptionGroup("Store shutdown failed", errors)

    def submit(self, future: Future[dict], action: str, payload: dict) -> None:
        """Enqueue a thread-safe result handle on the store's event loop."""
        self._queue.put_nowait((future, action, payload))

    async def _worker_loop(self) -> None:
        while True:
            future, action, payload = await self._queue.get()
            try:
                # Claiming the result and cancelling queued work are atomic
                # across threads. Once claimed, an action runs to completion
                # unless the store itself shuts down.
                if not future.set_running_or_notify_cancel():
                    continue
                handler = self._handlers.get(action)
                result = (
                    {"success": False, "messages": f"unknown action: {action}"}
                    if handler is None
                    else await handler(payload)
                )
                future.set_result(result)
            except asyncio.CancelledError:
                future.set_exception(QueryCancelledError())
                if asyncio.current_task().cancelling():
                    raise
            except Exception as exc:  # noqa: BLE001 - isolate action failures
                logger.exception(f"Store action `{action}` failed: {exc}")
                future.set_result(
                    {"success": False, "messages": f"internal error: {exc}"}
                )
            finally:
                self._queue.task_done()

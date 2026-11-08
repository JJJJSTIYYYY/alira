"""Public async bridge to a store owned by one dedicated thread."""

import asyncio
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock, Thread
from typing import TYPE_CHECKING

from alira.lifespan.auto_init import auto_init
from alira.modules.database.utils.errors import StoreCode, StoreError
from alira.modules.database.utils.handlers import service_handler
from alira.shared.application.config import WORKER_COUNT

if TYPE_CHECKING:
    from alira.modules.database.runtime import _StoreRuntime

__all__ = ["StoreManager", "query_store"]


@dataclass
class _ThreadState:
    ready: Future[None] = field(default_factory=Future)
    finished: Future[None] = field(default_factory=Future)
    thread: Thread | None = None
    loop: asyncio.AbstractEventLoop | None = None
    stop_signal: asyncio.Event | None = None
    runtime: "_StoreRuntime | None" = None
    stopping: bool = False


async def _wait_for_transition(transition: Future[None]) -> None:
    waiter = asyncio.wrap_future(transition)
    try:
        await asyncio.shield(waiter)
    except asyncio.CancelledError:
        # The shared transition continues after this caller leaves. Retrieve a
        # later failure on its abandoned wrapper; other waiters still receive it.
        waiter.add_done_callback(
            lambda done: None if done.cancelled() else done.exception()
        )
        raise


class StoreManager:
    """Own a store thread, its event loop, workers, and service lifecycle.

    Construction is inert. ``start()`` or the first ``query()`` creates the
    thread; ``stop()`` closes services and joins it. Instances may be used from
    multiple caller event loops. New queries are rejected during shutdown.
    """

    def __init__(
        self,
        *,
        db_path: str | Path | None = None,
        cache_path: str | Path | None = None,
        worker_count: int = WORKER_COUNT,
    ):
        if worker_count < 1:
            raise ValueError("worker_count must be greater than zero")
        self._db_path = db_path
        self._cache_path = cache_path
        self._worker_count = worker_count
        self._guard = Lock()
        self._state: _ThreadState | None = None

    async def start(self) -> None:
        """Start once, waiting for thread-local services to become ready."""
        await self._ensure_started()

    @service_handler
    async def query(self, action: str, payload: dict) -> dict:
        """Execute an action on the store thread.

        Keep ``payload`` stable until this call completes. Storage code borrows
        it without copying it on the caller thread. Cancelling the call abandons
        its result; a write already being executed may still finish.

        Args:
            action: Registered StoreExecutor action name.
            payload: Action-specific dictionary documented on the executor.

        Returns:
            ``{"success": bool, "code": int, "messages": dict | list | str}``.
            Success uses code 0 and dict/list results; failure uses a code and
            standard message from database.utils.errors. Business rejections use
            success=True and a nonzero code; unexpected exceptions use success=False.
            Cancellation propagates.
        """
        state = await self._ensure_started()
        pending: Future[dict] = Future()
        with self._guard:
            if state.stopping:
                raise StoreError(StoreCode.STORE_UNAVAILABLE)
            state.loop.call_soon_threadsafe(
                state.runtime.submit, pending, action, payload
            )
        return await asyncio.wrap_future(pending)

    async def stop(self) -> None:
        """Stop accepting queries, cancel work, close services, and join the thread.

        Concurrent callers share the same shutdown. Cancelling a caller does
        not cancel the store thread's cleanup; a later call can await it.
        """
        with self._guard:
            state = self._state
            if state is None:
                return
            if not state.stopping:
                state.stopping = True
                if state.loop is not None:
                    state.loop.call_soon_threadsafe(state.stop_signal.set)
        try:
            await _wait_for_transition(state.finished)
        finally:
            if state.finished.done():
                await asyncio.to_thread(state.thread.join)
                with self._guard:
                    if self._state is state:
                        self._state = None

    async def _ensure_started(self) -> _ThreadState:
        with self._guard:
            state = self._state
            if state is None or state.finished.done():
                state = _ThreadState()
                state.thread = Thread(
                    target=self._run,
                    args=(state,),
                    name=f"alira-store-{id(self):x}",
                    daemon=True,
                )
                self._state = state
                try:
                    state.thread.start()
                except BaseException:
                    self._state = None
                    raise
            elif state.stopping:
                raise StoreError(StoreCode.STORE_UNAVAILABLE)
        await _wait_for_transition(state.ready)
        with self._guard:
            if state.stopping:
                raise StoreError(StoreCode.STORE_UNAVAILABLE)
        return state

    def _run(self, state: _ThreadState) -> None:
        failure = None
        try:
            with asyncio.Runner() as runner:
                try:
                    runner.run(self._serve(state))
                finally:
                    # Publish the transition before Runner closes its loop.
                    with self._guard:
                        state.stopping = True
        except BaseException as exc:  # noqa: BLE001 - propagate thread failures to callers
            failure = exc
        finally:
            with self._guard:
                if not state.ready.done():
                    state.ready.set_exception(
                        failure
                        or RuntimeError("Store stopped before startup completed")
                    )
                    state.finished.set_result(None)
                elif failure is not None:
                    state.finished.set_exception(failure)
                else:
                    state.finished.set_result(None)

    async def _serve(self, state: _ThreadState) -> None:
        # Importing the execution layer here also keeps service construction and
        # its SQLAlchemy imports on the store thread during ordinary application use.
        from alira.modules.database.runtime import _StoreRuntime

        with self._guard:
            state.loop = asyncio.get_running_loop()
            state.stop_signal = asyncio.Event()
            if state.stopping:
                state.stop_signal.set()
        runtime = _StoreRuntime(
            db_path=self._db_path,
            cache_path=self._cache_path,
            worker_count=self._worker_count,
        )
        state.runtime = runtime
        try:
            await runtime.start()
            with self._guard:
                if state.stopping:
                    raise StoreError(StoreCode.STORE_UNAVAILABLE)
                state.ready.set_result(None)
            await state.stop_signal.wait()
        finally:
            with self._guard:
                state.stopping = True
            await runtime.stop()


_store_manager = StoreManager()
auto_init.register(_store_manager, group="database")


async def query_store(action: str, payload: dict) -> dict:
    """Execute a database action through the application-owned manager.

    Args:
        action: Registered StoreExecutor action, for example ``create_user``,
            ``verify_user`` or ``list_all_user``.
        payload: Action-specific dictionary. Registration takes
            ``{"user_name": str}``; verification takes ``{"user_uid": str}``
            or ``{"user_name": str}`` (both require a single matching user).
            Listing users takes ``{}``. Other payloads are documented on
            StoreExecutor's corresponding action.

    Returns:
        Exactly ``{"success": bool, "code": int, "messages": dict | list | str}``.
        Success uses code 0 and dict/list results (``{}`` for a completed mutation).
        User registration/verification return ``{"user_uid": str, "user_name": str}``
        in messages; listing returns a list of those dictionaries. Failure uses
        a nonzero code and fixed string from database.utils.errors, without
        exception details. Business rejections use success=True; unexpected
        exceptions use success=False. Check code == 0 for business success. Cancellation propagates to the caller.
    """
    return await _store_manager.query(action, payload)

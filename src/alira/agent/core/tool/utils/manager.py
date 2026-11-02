"""Resident event consumers shared by function and MCP tool managers."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Any
from uuid import uuid4

from apixis.core.event import ApixEvent, ApixEventHandler, get_handler_registry
from apixis.core.event.base import suspend_process

from alira.agent.core.tool.base import BaseTool
from alira.agent.core.tool.utils.request import ToolRequest, send_request
from alira.common.type.identiity import ConversationIdentity


class ToolManager(ABC):
    """Own a request subscription and execution tasks across graph runs.

    The subscription admits requests. Execution owners publish a separate
    correlated result, independently of the request event handler lifetime.
    """

    def __init__(self) -> None:
        self.request_event = f"alira.tool.{uuid4().hex}"
        self._lifecycle_lock = asyncio.Lock()
        self._started = False
        self._stopping = False
        self._generation = 0
        self._tasks: set[asyncio.Task] = set()
        self._pending: dict[str, ToolRequest] = {}
        self._shutdown_task: asyncio.Task | None = None
        self._registry = None
        self._handler = ApixEventHandler(
            self._handle_request,
            name=self.request_event,
            on_cancelled=self._cancel_request,
        )

    @property
    def is_started(self) -> bool:
        return self._started and not self._stopping

    @property
    def generation(self) -> int:
        """Identify the service run that accepts a request."""
        return self._generation

    @abstractmethod
    def tools_for(self, identity: ConversationIdentity | None = None) -> list[BaseTool]:
        """Select definitions visible to this graph's identity."""

    def owns(self, tool: BaseTool) -> bool:
        return any(candidate is tool for candidate in self.tools_for())

    async def _start(self) -> None:
        """Initialize implementation-specific resources."""

    async def _stop(self) -> None:
        """Release implementation-specific resources after requests drain."""

    async def start(self) -> None:
        """Start once; failed initialization leaves no subscription behind."""
        async with self._lifecycle_lock:
            if self.is_started:
                return
            if self._shutdown_task is not None:
                await asyncio.shield(self._shutdown_task)
            registry = None
            try:
                await self._start()
                registry = get_handler_registry()
                self._handler.register(f"{self.request_event}.*", exist_ok=False)
            except BaseException:
                if registry is not None:
                    registry.unregister_handler(self._handler.name, missing_ok=True)
                await self._stop()
                raise
            self._registry = registry
            self._stopping = False
            self._generation += 1
            self._started = True
            self._shutdown_task = None

    async def stop(self) -> None:
        """Reject queued calls and drain active work before releasing resources."""
        async with self._lifecycle_lock:
            if not self._started:
                return
            self._stopping = True
            if self._registry is not None:
                self._registry.unregister_handler(self._handler.name, missing_ok=True)
            if self._shutdown_task is None or self._shutdown_task.done():
                self._shutdown_task = asyncio.create_task(self._finish_stop())
                self._shutdown_task.add_done_callback(self._observe_shutdown)
            await asyncio.shield(self._shutdown_task)

    @staticmethod
    def _observe_shutdown(task: asyncio.Task) -> None:
        if not task.cancelled():
            task.exception()

    async def _finish_stop(self) -> None:
        for request in list(self._pending.values()):
            if not request.admitted:
                await request.finish(
                    RuntimeError("Tool manager is unavailable for this call.")
                )
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._stop()
        self._started = False
        self._stopping = False
        self._registry = None

    async def invoke(self, request: ToolRequest) -> Any:
        """Publish one call and await its result, even across a stop race."""
        if not self.is_started or request.generation != self.generation:
            raise RuntimeError("Tool manager is unavailable for this call.")
        self._pending[request.request_id] = request
        try:
            return await send_request(
                f"{self.request_event}.{request.request_id}", request
            )
        finally:
            self._pending.pop(request.request_id, None)

    async def _dispatch(self, request: ToolRequest) -> None:
        async def execute() -> None:
            await request.run(lambda: request.tool.execute(request.state, request.call))

        async with suspend_process():
            task = asyncio.create_task(execute(), name=f"tool-{request.request_id}")
            request._task = task
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _handle_request(self, event: ApixEvent) -> None:
        request = event.context
        if not isinstance(request, ToolRequest):
            return
        if request.completed:
            return
        if request.cancelled:
            await request.finish(asyncio.CancelledError())
            return
        if (
            not self.is_started
            or request.generation != self._generation
            or not self.owns(request.tool)
        ):
            await request.finish(
                RuntimeError("Tool manager is unavailable for this call.")
            )
            return
        try:
            request.admitted = True
            await self._dispatch(request)
        except Exception as exc:
            await request.finish(exc)

    async def _cancel_request(self, event: ApixEvent) -> None:
        if isinstance(event.context, ToolRequest):
            await event.context.cancel()
            if not event.context.completed:
                await event.context.finish(asyncio.CancelledError())

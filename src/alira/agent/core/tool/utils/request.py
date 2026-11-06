"""Correlated event requests with explicit execution and cancellation ownership."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any
from uuid import uuid4

from apixis.core.event import EventType, get_event_pipe, wait_for_event
from apixis.core.event.base import suspend_process
from my_llm import ToolCall

from alira.agent.core.tool.base import BaseTool


@dataclass(kw_only=True, slots=True)
class EventRequest[T]:
    """An event's input owner, correlated reply and cancellable execution."""

    request_id: str = field(default_factory=lambda: uuid4().hex)
    result: T | None = None
    error: BaseException | None = None
    duration: float = 0
    completed: bool = False
    cancelled: bool = False
    _task: asyncio.Task | None = field(default=None, repr=False)

    @property
    def result_event(self) -> str:
        return f"alira.tool.result.{self.request_id}"

    async def finish(self, error: BaseException | None = None) -> None:
        if self.completed:
            return
        self.error = error
        self.completed = True
        await get_event_pipe().post_event(
            event_name=self.result_event,
            event_type=EventType.TRANSIENT,
            context=self,
        )

    async def run(self, operation: Callable[[], Awaitable[T]]) -> None:
        start = perf_counter()
        error = None
        try:
            if self.cancelled:
                raise asyncio.CancelledError
            self.result = await operation()
        except (Exception, asyncio.CancelledError) as exc:
            error = exc
        finally:
            self.duration = perf_counter() - start
        await self.finish(error)

    async def cancel(self) -> None:
        """Skip queued work or cancel and join the one admitted operation."""
        already_cancelled = self.cancelled
        self.cancelled = True
        if self._task is not None:
            if not already_cancelled:
                self._task.cancel()
            cleanup = asyncio.gather(self._task, return_exceptions=True)
            cancelled = None
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError as exc:
                    cancelled = exc
            if cancelled is not None:
                raise cancelled


@dataclass(slots=True)
class ToolRequest(EventRequest[Any]):
    tool: BaseTool
    state: dict[str, Any]
    call: ToolCall
    generation: int = 0
    admitted: bool = False


async def send_request[T](event_name: str, request: EventRequest[T]) -> T:
    """Register a result listener before publishing; admission is not completion."""
    try:
        async with suspend_process():
            async with wait_for_event(request.result_event) as waiter:
                await get_event_pipe().post_event(
                    event_name=event_name,
                    event_type=EventType.TRANSIENT,
                    context=request,
                )
                event = await waiter.wait()
        if event.context is not request or not request.completed:
            raise RuntimeError("Tool result did not match its request.")
        if request.error is not None:
            raise request.error
        return request.result
    except BaseException:
        await request.cancel()
        raise

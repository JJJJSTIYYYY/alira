"""One persistent task owns one MCP client and its event inbox."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from copy import deepcopy
from typing import Any
from uuid import uuid4

from apixis.core.event import (
    ApixEvent,
    ApixEventHandler,
    EventType,
    get_event_pipe,
    get_handler_registry,
    start_core,
)
from apixis.core.event.base import suspend_process
from fastmcp import Client

from alira.agent.core.tool.mcp.base import McpMetaSchema, _convert_call_tool_result
from alira.agent.core.tool.utils.request import EventRequest, ToolRequest, send_request


class MCPDiscoveryRequest(EventRequest[list[Any]]):
    """Discover definitions through the client's resident session."""


class MCPRuntimeStop(EventRequest[None]):
    """Stop admission, drain calls and close the owned client context."""


class MCPToolRuntime:
    """Keep client context entry, serving and exit in the same task.

    Permanent subscriptions feed an inbox even while calls or shutdown are in
    progress. Call tasks may run concurrently; only this runtime task operates
    the client context. A failed context exit is retried here on the next stop
    event, so retrying never transfers context ownership to another task.
    """

    def __init__(
        self,
        meta: McpMetaSchema,
        create_client: Callable[[McpMetaSchema], Awaitable[Client]],
    ) -> None:
        self.meta = deepcopy(meta)
        self._create_client = create_client
        self.event_name = f"alira.mcp.runtime.{uuid4().hex}"
        self.call_event = f"{self.event_name}.call"
        self.discovery_event = f"{self.event_name}.discover"
        self.lifecycle_event = f"{self.event_name}.stop"
        self.closing = False
        self._inbox: asyncio.Queue[EventRequest] = asyncio.Queue()
        self._tasks: set[asyncio.Task] = set()
        self._pending: dict[str, EventRequest] = {}
        self._finished = False
        self._task: asyncio.Task | None = None
        self._start_lock = asyncio.Lock()
        self._ready = asyncio.Event()
        self._startup_error: BaseException | None = None
        self._stop_requests: list[MCPRuntimeStop] = []
        self._registry = None
        self._client: Client | None = None
        self._handler = ApixEventHandler(self._receive, name=self.event_name)

    @property
    def is_running(self) -> bool:
        return (
            self._ready.is_set()
            and not self.closing
            and self._startup_error is None
            and self._task is not None
            and not self._task.done()
        )

    async def start(self) -> None:
        async with self._start_lock:
            await self._start_task()
        async with suspend_process():
            await self._ready.wait()
        if self._startup_error is not None:
            raise self._startup_error
        if not self.is_running:
            raise RuntimeError("MCP runtime is closing.")

    async def _start_task(self) -> None:
        if self._task is None:
            await start_core()
            self._registry = get_handler_registry()
            try:
                self._handler.register(
                    self.call_event,
                    self.discovery_event,
                    self.lifecycle_event,
                    exist_ok=False,
                )
            except BaseException:
                self._registry.unregister_handler(self._handler.name, missing_ok=True)
                raise
            async with suspend_process():
                self._task = asyncio.create_task(self._run(), name=self.event_name)
                self._task.add_done_callback(self._observe)

    @staticmethod
    def _observe(task: asyncio.Task) -> None:
        if not task.cancelled():
            task.exception()

    async def _receive(self, event: ApixEvent) -> None:
        request = event.context
        if not isinstance(request, (ToolRequest, MCPDiscoveryRequest, MCPRuntimeStop)):
            return
        if self._finished:
            await request.finish(
                None
                if isinstance(request, MCPRuntimeStop)
                else RuntimeError("MCP runtime is closed.")
            )
            return
        self._pending[request.request_id] = request
        if isinstance(request, MCPRuntimeStop):
            self.closing = True
        elif self.closing:
            await request.finish(RuntimeError("MCP runtime is closing."))
            self._pending.pop(request.request_id, None)
            return
        self._inbox.put_nowait(request)

    async def submit(self, request: ToolRequest) -> None:
        if not self.is_running:
            raise RuntimeError("MCP runtime is closing.")
        self._pending[request.request_id] = request
        try:
            await get_event_pipe().post_event(
                event_name=self.call_event,
                event_type=EventType.TRANSIENT,
                context=request,
            )
        except BaseException:
            self._pending.pop(request.request_id, None)
            raise

    async def discover(self) -> list[Any]:
        if not self.is_running:
            raise RuntimeError("MCP runtime is closing.")
        request = MCPDiscoveryRequest()
        self._pending[request.request_id] = request
        return await send_request(self.discovery_event, request)

    async def stop(self) -> None:
        if self._task is None or self._task.done():
            return
        if self._finished:
            await asyncio.shield(self._task)
            return
        self.closing = True
        request = MCPRuntimeStop()
        self._pending[request.request_id] = request
        await send_request(self.lifecycle_event, request)
        # Result means client exit completed; joining includes subscription cleanup.
        await asyncio.shield(self._task)

    async def _reply_stops(self, error: BaseException | None = None) -> None:
        requests, self._stop_requests = self._stop_requests, []
        for request in requests:
            await request.finish(error)
            self._pending.pop(request.request_id, None)

    async def _close_client(self) -> None:
        """Retry failed cleanup only within the original client owner task."""
        assert self._client is not None
        while True:
            try:
                await self._client.__aexit__(None, None, None)
            except Exception as exc:
                self.closing = True
                await self._reply_stops(exc)
                while True:
                    request = await self._inbox.get()
                    if isinstance(request, MCPRuntimeStop):
                        self._stop_requests.append(request)
                        break
                    await request.finish(RuntimeError("MCP runtime is closing."))
                    self._pending.pop(request.request_id, None)
            else:
                self._client = None
                await self._reply_stops()
                return

    @asynccontextmanager
    async def _client_context(self) -> AsyncIterator[Client]:
        self._client = await self._create_client(self.meta)
        try:
            await self._client.__aenter__()
        except BaseException as exc:
            self._startup_error = exc
            self.closing = True
            self._ready.set()
            await self._close_client()
            raise
        try:
            yield self._client
        finally:
            await self._close_client()

    async def _invoke(self, request: ToolRequest) -> str:
        assert self._client is not None
        result = await self._client.call_tool(
            request.call["function"]["name"],
            request.tool._tool_arguments(request.call),
            raise_on_error=False,
        )
        return _convert_call_tool_result(result)

    def _launch(self, request: EventRequest) -> None:
        assert self._client is not None
        operation = (
            self._client.list_tools
            if isinstance(request, MCPDiscoveryRequest)
            else lambda: self._invoke(request)
        )
        task = asyncio.create_task(
            request.run(operation), name=f"mcp-call-{request.request_id}"
        )
        request._task = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(lambda _: self._pending.pop(request.request_id, None))

    async def _serve(self) -> None:
        while True:
            request = await self._inbox.get()
            if isinstance(request, MCPRuntimeStop):
                self._stop_requests.append(request)
                break
            if self.closing:
                await request.finish(RuntimeError("MCP runtime is closing."))
                self._pending.pop(request.request_id, None)
            elif request.cancelled:
                await request.finish(asyncio.CancelledError())
                self._pending.pop(request.request_id, None)
            else:
                self._launch(request)
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _run(self) -> None:
        try:
            async with self._client_context():
                if self.closing:
                    raise RuntimeError("MCP runtime is closing.")
                self._ready.set()
                try:
                    await self._serve()
                finally:
                    if self._tasks:
                        for task in self._tasks:
                            task.cancel()
                        await asyncio.gather(*self._tasks, return_exceptions=True)
        except (Exception, asyncio.CancelledError) as exc:
            self._startup_error = exc
        finally:
            self.closing = True
            self._finished = True
            self._ready.set()
            self._registry.unregister_handler(self._handler.name, missing_ok=True)
            for request in list(self._pending.values()):
                await request.finish(
                    None
                    if isinstance(request, MCPRuntimeStop)
                    else RuntimeError("MCP runtime is closed.")
                )
            self._pending.clear()
            while not self._inbox.empty():
                self._inbox.get_nowait()

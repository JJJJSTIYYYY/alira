"""Real event delivery to task-owned MCP sessions at bounded core capacities."""

import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio
from apixis.core.event import (
    ApixEventPipe,
    BuiltinChannel,
    factory,
    is_registered,
    wait_for_event,
)

from alira.agent.core.tool.mcp import MCPRuntimeStop, MCPToolManager, MCPToolRuntime
from alira.agent.core.tool.utils.request import send_request

META = {
    "mcp_id": "server",
    "mcp_name": "server",
    "transport": "http",
    "endpoint": "http://unused.invalid",
    "config": {},
}
IDENTITY = {"id": "user", "platform": "test", "conversation_uid": "conversation"}

pytestmark = pytest.mark.parametrize(
    "event_runtime_capacity",
    [(1, 1), (1, 2), (2, 1), (2, 2)],
    indirect=True,
)


@pytest.fixture
def event_runtime_capacity(request, monkeypatch):
    capacity, queue_size = request.param
    monkeypatch.setattr(
        factory,
        "ApixEventPipe",
        lambda: ApixEventPipe(builtin=BuiltinChannel(maxsize=queue_size)),
    )
    return capacity


class Client:
    def __init__(self):
        self.owner = None
        self.closed = False
        self.invoke = None
        self.executions = []

    async def __aenter__(self):
        self.owner = asyncio.current_task()
        return self

    async def __aexit__(self, *args):
        assert asyncio.current_task() is self.owner
        self.closed = True

    async def list_tools(self):
        return [
            SimpleNamespace(name=name, inputSchema={"type": "object"})
            for name in ("echo", "wait", "nested", "fail")
        ]

    async def call_tool(self, name, arguments, **kwargs):
        assert not self.closed
        self.executions.append((name, arguments))
        value = (
            await self.invoke(name, arguments)
            if self.invoke
            else arguments.get("value", "ok")
        )
        return {"content": [{"type": "text", "text": value}]}


@pytest_asyncio.fixture
async def resident(monkeypatch):
    manager, client = MCPToolManager(), Client()

    async def create(meta):
        return client

    monkeypatch.setattr(manager, "create_mcp_client", create)
    await manager.start()
    await manager.get_mcp_tools(META, IDENTITY)
    try:
        yield manager, client
    finally:
        await manager.stop()


async def test_call_tool_waits_for_remote_result_after_request_event_is_processed(
    resident,
):
    manager, client = resident
    started, release = asyncio.Event(), asyncio.Event()

    async def invoke(name, arguments):
        started.set()
        await release.wait()
        return arguments["value"]

    client.invoke = invoke
    async with wait_for_event(
        f"{manager.request_event}.*", point="processed"
    ) as admitted:
        task = asyncio.create_task(
            manager.call_tool(IDENTITY, "server", "wait", {"value": "returned"})
        )
        event = await asyncio.wait_for(admitted.wait(), 2)
    try:
        await asyncio.wait_for(started.wait(), 2)
        assert not task.done()
        assert not event.context.completed
        assert event.context.result_event != event.event_name
    finally:
        release.set()
    assert await asyncio.wait_for(task, 2) == "returned"
    assert event.context.completed


async def test_runtime_receives_concurrent_calls_and_stop_while_another_call_is_blocked(
    resident,
):
    manager, client = resident
    started, release = asyncio.Event(), asyncio.Event()

    async def invoke(name, arguments):
        if name == "wait":
            started.set()
            await release.wait()
        return arguments["value"]

    client.invoke = invoke
    first = asyncio.create_task(
        manager.call_tool(IDENTITY, "server", "wait", {"value": "first"})
    )
    await asyncio.wait_for(started.wait(), 2)
    # Calls arriving during the first execution must all reach the permanent inbox.
    rest = [
        manager.call_tool(IDENTITY, "server", "echo", {"value": str(i)})
        for i in range(20)
    ]
    assert await asyncio.wait_for(asyncio.gather(*rest), 2) == [
        str(i) for i in range(20)
    ]
    closing = asyncio.create_task(manager.close_session(IDENTITY))
    await asyncio.sleep(0)
    try:
        assert not closing.done() and not client.closed
    finally:
        release.set()
    assert await asyncio.wait_for(first, 2) == "first"
    await asyncio.wait_for(closing, 2)
    assert client.closed


async def test_same_runtime_can_service_nested_event_requests(resident):
    manager, client = resident

    async def invoke(name, arguments):
        if name == "nested":
            return "nested:" + await manager.call_tool(
                IDENTITY, "server", "echo", arguments
            )
        return arguments["value"]

    client.invoke = invoke
    result = await asyncio.wait_for(
        manager.call_tool(IDENTITY, "server", "nested", {"value": "reply"}),
        2,
    )
    assert result == "nested:reply"
    assert [name for name, _ in client.executions] == ["nested", "echo"]


async def test_call_cancellation_joins_async_cleanup_without_stopping_runtime(resident):
    manager, client = resident
    started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def invoke(name, arguments):
        if name == "wait":
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
        return "ok"

    client.invoke = invoke
    task = asyncio.create_task(manager.call_tool(IDENTITY, "server", "wait"))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    try:
        await asyncio.wait_for(cleaning.wait(), 2)
        task.cancel()
        assert (
            await asyncio.wait_for(manager.call_tool(IDENTITY, "server", "echo"), 2)
            == "ok"
        )
        assert not task.done() and not client.closed
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
    assert await manager.call_tool(IDENTITY, "server", "echo") == "ok"


async def test_facade_propagates_original_failure_and_checks_catalog(resident):
    manager, client = resident
    error = ValueError("remote failure")

    async def invoke(name, arguments):
        raise error

    client.invoke = invoke
    with pytest.raises(ValueError) as caught:
        await manager.call_tool(IDENTITY, "server", "fail")
    assert caught.value is error
    with pytest.raises(ValueError, match="not discovered"):
        await manager.call_tool(IDENTITY, "missing", "echo")
    with pytest.raises(ValueError, match="not discovered"):
        await manager.call_tool({**IDENTITY, "id": "other"}, "server", "echo")
    with pytest.raises(TypeError, match="dictionary"):
        await manager.call_tool(IDENTITY, "server", "echo", [])


async def test_lifecycle_event_closes_context_in_its_owner_and_removes_subscription():
    client = Client()

    async def create(meta):
        return client

    runtime = MCPToolRuntime(META, create)
    await runtime.start()
    assert len(await runtime.discover()) == 4
    assert is_registered(runtime.event_name)
    try:
        await asyncio.wait_for(
            send_request(runtime.lifecycle_event, MCPRuntimeStop()), 2
        )
        await runtime.stop()
    finally:
        await runtime.stop()
    assert client.closed and not runtime.is_running
    assert not is_registered(runtime.event_name)


async def test_cancelled_discovery_does_not_abandon_client_startup(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowClient(Client):
        async def __aenter__(self):
            await super().__aenter__()
            entered.set()
            await release.wait()
            return self

    client, manager = SlowClient(), MCPToolManager()

    async def create(meta):
        return client

    monkeypatch.setattr(manager, "create_mcp_client", create)
    await manager.start()
    discovery = asyncio.create_task(manager.get_mcp_tools(META, IDENTITY))
    await asyncio.wait_for(entered.wait(), 2)
    discovery.cancel()
    with pytest.raises(asyncio.CancelledError):
        await discovery
    close = asyncio.create_task(manager.stop())
    release.set()
    await asyncio.wait_for(close, 2)
    assert client.closed


async def test_stop_resolves_a_call_whose_request_has_not_reached_the_dispatcher(
    resident, monkeypatch
):
    manager, client = resident
    pipe = factory.get_event_pipe()
    publishing, release = asyncio.Event(), asyncio.Event()
    original_post = pipe.post_event

    async def post(**kwargs):
        if kwargs["event_name"].startswith(manager.request_event + "."):
            publishing.set()
            await release.wait()
        await original_post(**kwargs)

    monkeypatch.setattr(pipe, "post_event", post)
    call = asyncio.create_task(manager.call_tool(IDENTITY, "server", "echo"))
    try:
        await asyncio.wait_for(publishing.wait(), 2)
        await asyncio.wait_for(manager.stop(), 2)
        assert client.closed and client.executions == []
    finally:
        release.set()
    with pytest.raises(RuntimeError, match="unavailable"):
        await asyncio.wait_for(call, 2)


async def test_concurrent_runtime_start_creates_only_one_owner():
    client = Client()
    created = []

    async def create(meta):
        created.append(client)
        return client

    runtime = MCPToolRuntime(META, create)
    try:
        await asyncio.wait_for(asyncio.gather(*(runtime.start() for _ in range(10))), 2)
        assert created == [client]
        assert runtime.is_running
    finally:
        await runtime.stop()
    assert client.closed


async def test_application_shutdown_keeps_event_core_alive_until_runtimes_close(
    resident, monkeypatch
):
    from unittest.mock import AsyncMock

    from alira.server import create_app

    manager, client = resident
    pipe = factory.get_event_pipe()
    monkeypatch.setattr("alira.server.auto_load_router", lambda app: None)
    monkeypatch.setattr("alira.server.auto_init.start", AsyncMock())

    async def stop_services():
        assert pipe.is_running
        await manager.stop()
        assert pipe.is_running and client.closed

    monkeypatch.setattr("alira.server.auto_init.stop", stop_services)
    app = create_app()
    async with app.router.lifespan_context(app):
        assert await manager.call_tool(IDENTITY, "server", "echo") == "ok"
    assert client.closed and not pipe.is_running

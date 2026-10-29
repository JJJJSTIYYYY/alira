"""Resident MCP clients, event execution and identity isolation without a server."""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from apix.agent.core.tool import BaseTool, FunctionToolManager, Tool, ToolNode
from apix.agent.core.tool.mcp import MCPTool, MCPToolError, MCPToolManager
from apix.agent.core.tool.mcp.base import _convert_call_tool_result
from apix.agent.core.utils.message import ApixAiMessage

META = {
    "mcp_id": "test-mcp",
    "mcp_name": "Test server",
    "transport": "http",
    "endpoint": "http://unused.invalid",
    "config": {},
}
IDENTITY = {"id": "user", "platform": "test", "conversation_uid": "conversation"}


class Client:
    def __init__(self):
        self.entered = 0
        self.closed = False
        self.calls = []

    async def __aenter__(self):
        self.entered += 1
        self.owner_task = asyncio.current_task()
        return self

    async def __aexit__(self, *args):
        assert (
            getattr(self, "owner_task", asyncio.current_task())
            is asyncio.current_task()
        )
        self.closed = True

    async def list_tools(self):
        assert not self.closed
        return [
            SimpleNamespace(
                name=name, description="Test tool", inputSchema={"type": "object"}
            )
            for name in ("first", "second")
        ]

    async def call_tool(self, name, arguments, **kwargs):
        assert not self.closed
        self.calls.append((name, arguments))
        return {"content": [{"type": "text", "text": f"output:{name}"}]}


def state(*names, identity=None):
    return {
        "target": dict(IDENTITY if identity is None else identity),
        "messages": [
            ApixAiMessage(
                tool_calls=[
                    {
                        "id": f"call-{index}",
                        "type": "function",
                        "function": {"name": name, "arguments": "{}"},
                    }
                    for index, name in enumerate(names)
                ]
            ),
        ],
    }


def call(name="first"):
    return {
        "id": "call",
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }


@pytest_asyncio.fixture
async def manager(monkeypatch):
    manager = MCPToolManager()
    manager.clients = []

    def create(meta):
        client = Client()
        manager.clients.append(client)
        return client

    monkeypatch.setattr(manager, "create_mcp_client", AsyncMock(side_effect=create))
    await manager.start()
    try:
        yield manager
    finally:
        await manager.stop()


async def blocked_call(manager, monkeypatch):
    await manager.get_mcp_tools(META, IDENTITY)
    client = manager.clients[0]
    started, release = asyncio.Event(), asyncio.Event()
    original = client.call_tool

    async def call_tool(*args, **kwargs):
        started.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(client, "call_tool", call_tool)
    task = asyncio.create_task(manager.call_tool(IDENTITY, META["mcp_id"], "first"))
    await asyncio.wait_for(started.wait(), 2)
    return client, task, release


@pytest.mark.asyncio
async def test_one_graph_node_routes_function_and_mcp_managers(manager):
    from typing import Annotated, TypedDict

    from apixis.core.graph import AutoMerge

    from apix.agent.core.bot.apix_bot import ApixBot
    from apix.agent.core.graph import AgentGraphCreator
    from apix.common.type import ApixIdentity

    class State(TypedDict):
        messages: Annotated[list, AutoMerge()]
        target: ApixIdentity

    async def local() -> str:
        return "local-result"

    functions = FunctionToolManager([local])
    mcp_tools = await manager.get_mcp_tools(META, IDENTITY)
    graph = (
        AgentGraphCreator(State)
        .add_tools([functions, manager], identity=IDENTITY)
        .compile_agent("tools")
    )
    result = await graph.invoke(state("first", "local", "second"))
    assert [message.content for message in result["messages"][1:]] == [
        "output:first",
        "local-result",
        "output:second",
    ]
    assert functions.is_started and manager.is_started
    assert not manager.clients[0].closed
    schemas = ApixBot._schemas([*functions.tool_set, *mcp_tools])
    assert [schema["function"]["name"] for schema in schemas] == [
        "local",
        "first",
        "second",
    ]


@pytest.mark.asyncio
async def test_clients_survive_tool_node_batches_and_graph_boundaries(manager):
    tools = await manager.get_mcp_tools(META, IDENTITY)
    first = tools[0]
    assert isinstance(first, BaseTool)
    assert not isinstance(first, Tool)
    assert not hasattr(first, "func")
    node = ToolNode(manager, identity=IDENTITY, bind_llm_node="model")
    result = await node.execute(state("first", "second", "first"))
    assert [message.content for message in result.update["messages"]] == [
        "output:first",
        "output:second",
        "output:first",
    ]
    assert result.goto == "model"
    await ToolNode(tools).execute(state("first"))
    manager.create_mcp_client.assert_awaited_once()
    assert manager.clients[0].closed is False
    assert manager.is_started
    await manager.stop()
    assert manager.clients[0].closed


@pytest.mark.asyncio
async def test_identity_isolation_and_discovery_catalog(manager):
    identities = [
        IDENTITY,
        {**IDENTITY, "id": "other"},
        {**IDENTITY, "platform": "other"},
        {**IDENTITY, "conversation_uid": "other"},
    ]
    discovered = [
        await manager.get_mcp_tools(META, identity) for identity in identities
    ]
    assert len(manager.clients) == len(identities)
    for identity, tools in zip(identities, discovered, strict=True):
        assert manager.tools_for(identity) == tools
    with pytest.raises(ValueError, match="identity"):
        await discovered[0][0].execute(state(identity=identities[1]), call())
    assert all(not client.calls for client in manager.clients)
    with pytest.raises((ValueError, TypeError), match="identity|Identity"):
        await discovered[0][0].execute({}, call())


@pytest.mark.asyncio
async def test_load_all_tools_uses_user_id_and_binds_full_identity(
    manager, monkeypatch
):
    get_meta = AsyncMock(return_value=[META])
    monkeypatch.setattr(manager, "get_mcp_meta", get_meta)
    tools = await manager.load_all_mcp_tools(IDENTITY)
    get_meta.assert_awaited_once_with(IDENTITY["id"])
    assert manager.tools_for(IDENTITY) == tools
    assert await tools[0].execute(state(), call()) == "output:first"


@pytest.mark.asyncio
async def test_close_session_drains_calls_and_cleanup_survives_requester_cancellation(
    manager, monkeypatch
):
    client, active, release = await blocked_call(manager, monkeypatch)
    close = asyncio.create_task(manager.close_session(IDENTITY))
    await asyncio.sleep(0)
    assert not close.done() and not client.closed
    with pytest.raises(RuntimeError, match="closing"):
        await manager.call_tool(IDENTITY, META["mcp_id"], "first")
    close.cancel()
    with pytest.raises(asyncio.CancelledError):
        await close
    release.set()
    assert await active == "output:first"
    await manager.close_session(IDENTITY)
    assert client.closed
    await manager.get_mcp_tools(META, IDENTITY)
    assert manager.clients[-1] is not client and not manager.clients[-1].closed


@pytest.mark.asyncio
async def test_close_user_blocks_all_conversations_but_preserves_other_platforms(
    manager, monkeypatch
):
    first, active, release = await blocked_call(manager, monkeypatch)
    other_conversation = {**IDENTITY, "conversation_uid": "other"}
    other_platform = {**IDENTITY, "platform": "other"}
    await manager.get_mcp_tools(META, other_conversation)
    second = manager.clients[-1]
    await manager.get_mcp_tools(META, other_platform)
    independent = manager.clients[-1]
    closing = asyncio.create_task(manager.close_user(IDENTITY))
    await asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="closing"):
        await manager.get_mcp_tools(META, {**IDENTITY, "conversation_uid": "new"})
    release.set()
    await active
    await closing
    assert first.closed and second.closed and not independent.closed


@pytest.mark.asyncio
async def test_configuration_replacement_waits_for_active_calls(manager, monkeypatch):
    first, active, release = await blocked_call(manager, monkeypatch)
    changed = {**META, "endpoint": "http://new.invalid"}
    discovery = asyncio.create_task(manager.get_mcp_tools(changed, IDENTITY))
    await asyncio.sleep(0)
    assert not discovery.done() and not first.closed
    assert len(manager.clients) == 1
    release.set()
    await active
    assert len(await discovery) == 2
    assert first.closed and len(manager.clients) == 2
    assert not manager.clients[1].closed


@pytest.mark.asyncio
async def test_failed_client_entry_cleans_partial_resources_and_allows_retry(
    manager, monkeypatch
):
    failed = Client()
    failed.__class__ = type(
        "FailedClient",
        (Client,),
        {
            "__aenter__": AsyncMock(side_effect=RuntimeError("connect failed")),
        },
    )
    healthy = Client()
    monkeypatch.setattr(
        manager, "create_mcp_client", AsyncMock(side_effect=[failed, healthy])
    )
    with pytest.raises(RuntimeError, match="connect failed"):
        await manager.get_mcp_tools(META, IDENTITY)
    assert failed.closed
    tools = await manager.get_mcp_tools(META, IDENTITY)
    assert len(tools) == 2 and not healthy.closed


@pytest.mark.asyncio
async def test_stop_racing_client_startup_rejects_admission_and_cleans_client(
    manager, monkeypatch
):
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowClient(Client):
        async def __aenter__(self):
            entered.set()
            await release.wait()
            return self

    client = SlowClient()
    monkeypatch.setattr(manager, "create_mcp_client", AsyncMock(return_value=client))
    discovery = asyncio.create_task(manager.get_mcp_tools(META, IDENTITY))
    await entered.wait()
    stop = asyncio.create_task(manager.stop())
    await asyncio.sleep(0)
    assert not stop.done()
    release.set()
    with pytest.raises(RuntimeError, match="started|closing"):
        await discovery
    await stop
    assert client.closed
    assert not manager.is_started


@pytest.mark.asyncio
async def test_mcp_failure_cancels_sibling_calls_and_keeps_client_resident(
    manager, monkeypatch
):
    started = asyncio.Event()
    events = []

    class FailingClient(Client):
        async def call_tool(self, name, arguments, **kwargs):
            if name == "first":
                await started.wait()
                return {
                    "isError": True,
                    "content": [{"type": "text", "text": "failed"}],
                }
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                events.append("cancelled")

    client = FailingClient()
    monkeypatch.setattr(manager, "create_mcp_client", AsyncMock(return_value=client))
    tools = await manager.get_mcp_tools(META, IDENTITY)
    with pytest.raises(MCPToolError, match="failed"):
        await asyncio.wait_for(ToolNode(tools).execute(state("first", "second")), 1)
    assert events == ["cancelled"]
    assert not client.closed
    await manager.close_session(IDENTITY)
    assert client.closed


@pytest.mark.asyncio
async def test_discovery_requires_running_manager():
    manager = MCPToolManager()
    with pytest.raises(RuntimeError, match="started"):
        await manager.get_mcp_tools(META, IDENTITY)


@pytest.mark.asyncio
async def test_transport_configuration_maps_to_fastmcp_and_resident_stdio(
    manager, monkeypatch
):
    import apix.agent.core.tool.mcp.mcp_tool_manager as module

    transport = SimpleNamespace()

    def make_transport(**kwargs):
        return transport

    recorded = {}

    def stdio(**kwargs):
        recorded.update(kwargs)
        return transport

    monkeypatch.setattr(module, "StdioTransport", stdio)
    monkeypatch.setattr(module, "Client", lambda value, **kwargs: (value, kwargs))
    meta = {
        **META,
        "transport": "stdio",
        "endpoint": "python",
        "config": {"args": ["server.py"], "timeout": 5},
    }
    value, options = await MCPToolManager.create_mcp_client(manager, meta)
    assert value is transport and options == {"timeout": 5}
    assert recorded["keep_alive"] is True
    assert recorded["command"] == "python"
    assert recorded["args"] == ["server.py"]
    monkeypatch.setattr(module, "StreamableHttpTransport", make_transport)
    await MCPToolManager.create_mcp_client(manager, META)
    with pytest.raises(ValueError, match="Unsupported"):
        await MCPToolManager.create_mcp_client(
            manager, {**META, "transport": "unknown"}
        )


def test_result_conversion_retains_non_text_data_and_errors():
    image = {"type": "image", "data": "encoded", "mimeType": "image/png"}
    result = _convert_call_tool_result(
        {"content": [{"type": "text", "text": "hello"}, image]}
    )
    text, serialized_image = result.split("\n")
    assert text == "hello" and json.loads(serialized_image) == image
    assert json.loads(
        _convert_call_tool_result({"structuredContent": {"answer": 42}})
    ) == {"answer": 42}
    with pytest.raises(MCPToolError, match="remote error"):
        _convert_call_tool_result(
            {"isError": True, "content": [{"type": "text", "text": "remote error"}]}
        )


def test_mcp_definition_snapshots_identity_metadata_and_schema():
    manager = MCPToolManager()
    identity, meta = deepcopy(IDENTITY), deepcopy(META)
    definition = {
        "name": "first",
        "description": "description",
        "inputSchema": {"type": "object", "properties": {}},
    }
    tool = MCPTool(definition, manager, meta, identity)
    definition["inputSchema"]["properties"]["changed"] = {}
    identity["id"] = "changed"
    schema = tool.get_schema()
    assert schema["function"]["parameters"]["properties"] == {}
    schema["function"]["description"] = "changed"
    assert tool.get_schema()["function"]["description"] == "description"
    assert tool.manager is manager


@pytest.mark.asyncio
async def test_cancelled_replacement_cleanup_is_joined_before_session_closes(
    manager, monkeypatch
):
    closing, release = asyncio.Event(), asyncio.Event()

    class SlowCloseClient(Client):
        async def __aexit__(self, *args):
            closing.set()
            await release.wait()
            self.closed = True

    client = SlowCloseClient()
    monkeypatch.setattr(manager, "create_mcp_client", AsyncMock(return_value=client))
    await manager.get_mcp_tools(META, IDENTITY)
    replacement = asyncio.create_task(
        manager.get_mcp_tools({**META, "endpoint": "http://new.invalid"}, IDENTITY)
    )
    await closing.wait()
    replacement.cancel()
    with pytest.raises(asyncio.CancelledError):
        await replacement
    close = asyncio.create_task(manager.close_session(IDENTITY))
    await asyncio.sleep(0)
    assert not close.done()
    release.set()
    await close
    assert client.closed
    assert manager.create_mcp_client.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("close_scope", ["session", "user", "manager"])
async def test_failed_close_keeps_resources_and_gates_until_retry_succeeds(
    manager, monkeypatch, close_scope
):
    class RetryCloseClient(Client):
        attempts = 0

        async def __aexit__(self, *args):
            assert asyncio.current_task() is self.owner_task
            self.attempts += 1
            if self.attempts == 1:
                raise OSError("temporary close failure")
            self.closed = True

    client = RetryCloseClient()
    monkeypatch.setattr(manager, "create_mcp_client", AsyncMock(return_value=client))
    await manager.get_mcp_tools(META, IDENTITY)
    closer = {
        "session": lambda: manager.close_session(IDENTITY),
        "user": lambda: manager.close_user(IDENTITY),
        "manager": manager.stop,
    }[close_scope]
    with pytest.raises(ExceptionGroup):
        await closer()
    assert not client.closed
    with pytest.raises(RuntimeError, match="closing|started"):
        await manager.get_mcp_tools(META, IDENTITY)
    if close_scope == "user":
        with pytest.raises(RuntimeError, match="closing"):
            await manager.get_mcp_tools(META, {**IDENTITY, "conversation_uid": "new"})
    await closer()
    assert client.closed and client.attempts == 2


@pytest.mark.asyncio
async def test_real_in_process_mcp_protocol_persists_across_agent_graph_invocations(
    monkeypatch,
):
    from typing import Annotated, TypedDict

    from apixis.core.graph import AutoMerge
    from fastmcp import Client as FastMCPClient
    from mcp.server.fastmcp import FastMCP

    from apix.agent.core.graph import AgentGraphCreator
    from apix.common.type import ApixIdentity

    class State(TypedDict):
        target: ApixIdentity
        messages: Annotated[list, AutoMerge()]

    server = FastMCP("in-process")
    calls = []

    @server.tool()
    async def multiply(left: int, right: int) -> str:
        calls.append((left, right))
        return str(left * right)

    manager = MCPToolManager()
    created = []

    async def create(meta):
        client = FastMCPClient(server)
        created.append(client)
        return client

    monkeypatch.setattr(manager, "create_mcp_client", create)
    await manager.start()
    try:
        tools = await manager.get_mcp_tools(META, IDENTITY)
        assert tools[0].name == "multiply"
        assert (
            tools[0].get_schema()["function"]["parameters"]["properties"]["left"][
                "type"
            ]
            == "integer"
        )
        graph = (
            AgentGraphCreator(State).add_node(ToolNode(tools)).compile_agent("tools")
        )
        for value in (2, 5):
            request = call("multiply")
            request["function"]["arguments"] = json.dumps({"left": value, "right": 3})
            result = await asyncio.wait_for(
                graph.invoke(
                    {
                        "target": IDENTITY,
                        "messages": [ApixAiMessage(tool_calls=[request])],
                    }
                ),
                3,
            )
            assert result["messages"][-1].content == str(value * 3)
            assert len(created) == 1 and created[0].is_connected()
        assert calls == [(2, 3), (5, 3)]
        assert (
            await manager.call_tool(
                IDENTITY, META["mcp_id"], "multiply", {"left": 7, "right": 3}
            )
            == "21"
        )
        assert calls[-1] == (7, 3) and len(created) == 1
    finally:
        await manager.stop()
    assert not created[0].is_connected()


@pytest.mark.asyncio
async def test_refreshed_discovery_rejects_old_tool_without_reopening_stale_endpoint(
    manager,
):
    old_tools = await manager.get_mcp_tools(META, IDENTITY)
    changed = {**META, "endpoint": "http://new.invalid"}
    current_tools = await manager.get_mcp_tools(changed, IDENTITY)
    assert manager.clients[0].closed
    with pytest.raises(RuntimeError, match="stale.*rediscover"):
        await ToolNode(old_tools).execute(state("first"))
    assert manager.create_mcp_client.await_count == 2
    assert (
        manager.create_mcp_client.await_args.args[0]["endpoint"] == changed["endpoint"]
    )
    assert not manager.clients[1].closed
    assert await current_tools[0].execute(state(), call()) == "output:first"


@pytest.mark.asyncio
async def test_queued_old_tool_cannot_revert_config_while_refresh_drains_calls(
    manager, monkeypatch
):
    _, active, release = await blocked_call(manager, monkeypatch)
    old_tool = manager.tools_for(IDENTITY)[0]
    refresh = asyncio.create_task(
        manager.get_mcp_tools({**META, "endpoint": "http://new.invalid"}, IDENTITY)
    )
    await asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="stale"):
        await old_tool.execute(state(), call())
    release.set()
    await active
    await refresh
    assert manager.create_mcp_client.await_count == 2


@pytest.mark.asyncio
async def test_failed_entry_and_failed_cleanup_retains_partial_client_for_retry(
    manager, monkeypatch
):
    class PartialClient(Client):
        close_attempts = 0

        async def __aenter__(self):
            self.owner_task = asyncio.current_task()
            raise RuntimeError("partial startup")

        async def __aexit__(self, *args):
            assert asyncio.current_task() is self.owner_task
            self.close_attempts += 1
            if self.close_attempts == 1:
                raise OSError("close failed")
            self.closed = True

    partial, healthy = PartialClient(), Client()
    create = AsyncMock(side_effect=[partial, healthy])
    monkeypatch.setattr(manager, "create_mcp_client", create)
    with pytest.raises(RuntimeError, match="partial startup"):
        await manager.get_mcp_tools(META, IDENTITY)
    assert partial.close_attempts == 1 and not partial.closed
    tools = await manager.get_mcp_tools(META, IDENTITY)
    assert partial.close_attempts == 2 and partial.closed
    assert await tools[0].execute(state(), call()) == "output:first"
    assert create.await_count == 2

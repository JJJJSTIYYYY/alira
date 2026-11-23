"""Alira graph configuration and tools at the Apixis runtime boundary."""

import asyncio
from typing import Annotated, TypedDict
from unittest.mock import AsyncMock, Mock

import pytest
from apixis.core.graph import AutoMerge
from httpx import ASGITransport, AsyncClient
from my_llm import ToolCall

from alira.modules.agent.src.core.graph import AgentGraphCreator, agent_graph
from alira.modules.agent.src.shared.message import (
    AliraAssistantMessage,
    AliraToolMessage,
)


class State(TypedDict, total=False):
    messages: Annotated[list, AutoMerge()]
    value: int


@pytest.mark.parametrize("compiler", ["compile_graph", "compile_agent"])
@pytest.mark.parametrize("no_snapshot", [False, True])
def test_compiler_forwards_graph_settings(monkeypatch, compiler, no_snapshot):
    def first(state):
        return {"value": 1}

    graph_type = Mock()
    monkeypatch.setattr(agent_graph, "AgentGraph", graph_type)
    creator = AgentGraphCreator(State).add_node(first)
    graph = getattr(creator, compiler)(
        "first",
        max_steps=2,
        max_snapshots=1,
        using_namespace="agent-settings",
        no_snapshot=no_snapshot,
        exist_ok=True,
    )
    assert graph is graph_type.return_value
    graph_type.assert_called_once_with(
        creator._nodes,
        "first",
        max_steps=2,
        max_snapshots=1,
        state_schema=State,
        using_namespace="agent-settings",
        no_snapshot=no_snapshot,
        exist_ok=True,
    )


async def test_custom_messages_key_reaches_tool_node():
    class CustomState(TypedDict):
        conversation: Annotated[list, AutoMerge()]

    def hello():
        return "hello"

    graph = (
        AgentGraphCreator(CustomState, messages_key="conversation")
        .add_tools([hello])
        .compile_agent("tools")
    )
    result = await graph.invoke(
        {
            "conversation": [
                AliraAssistantMessage(
                    content=[ToolCall(call_id="hello-1", name="hello", arguments={})]
                )
            ]
        }
    )
    assert isinstance(result["conversation"][-1], AliraToolMessage)
    assert result["conversation"][-1].content[0].text == "hello"


async def test_server_lifespan_handles_local_events_health_and_shutdown(
    monkeypatch,
    core_lifecycle,
    kernel_lifecycle,
):
    from apixis.core.event import (
        EventType,
        get_event_runtime_status,
        post_event,
        subscribe,
        unsubscribe,
    )

    from alira.modules.web.src.application import create_app

    # Exercise the real event runtime without opening unrelated storage services.
    start = AsyncMock(wraps=core_lifecycle.start)
    stop = AsyncMock(wraps=core_lifecycle.stop)
    monkeypatch.setattr(core_lifecycle, "start", start)
    monkeypatch.setattr(core_lifecycle, "stop", stop)
    monkeypatch.setattr("alira.modules.web.src.application.auto_load_router", lambda app: None)
    app = create_app()
    received = asyncio.Event()

    @subscribe("alira.migration.health")
    async def receive(event):
        received.set()

    try:
        async with kernel_lifecycle.lifespan(), app.router.lifespan_context(app):
            assert get_event_runtime_status().running
            await post_event(
                event_type=EventType.WORKFLOW, event_name="alira.migration.health"
            )
            await asyncio.wait_for(received.wait(), 1)
            async with AsyncClient(
                transport=ASGITransport(app), base_url="http://test"
            ) as client:
                response = await client.get("/health")
            assert response.status_code == 200
            assert response.json() == {"status": "ok", "service": "alira"}
        assert not get_event_runtime_status().running
    finally:
        unsubscribe("receive")
    start.assert_awaited_once()
    stop.assert_awaited_once()


async def test_add_tools_forwards_llm_binding_with_custom_messages_key():
    class CustomState(TypedDict, total=False):
        conversation: Annotated[list, AutoMerge()]
        observed: str

    def hello():
        return "hello"

    def model(state):
        assert len(state["conversation"]) == 2
        return {"observed": state["conversation"][-1].content[0].text}

    graph = (
        AgentGraphCreator(CustomState, messages_key="conversation")
        .add_tools([hello], bind_llm_node="model")
        .add_node(model)
        .compile_agent("tools")
    )
    result = await graph.invoke(
        {
            "conversation": [
                AliraAssistantMessage(
                    content=[ToolCall(call_id="hello-1", name="hello", arguments={})]
                )
            ]
        }
    )
    assert result["observed"] == "hello"

from my_llm import ToolCall

"""Application contracts at the Apixis runtime boundary."""

import asyncio
from contextlib import aclosing
from typing import Annotated, TypedDict
from unittest.mock import AsyncMock

import pytest
from apixis.core.graph import (
    AutoMerge,
    Command,
    get_current_run_id,
    get_stream_writer,
)
from apixis.core.utils.exception import InvalidContextError
from httpx import ASGITransport, AsyncClient

from alira.modules.agent.core.graph import AgentGraph, AgentGraphCreator
from alira.shared.models.message import AliraAssistantMessage, AliraToolMessage


class State(TypedDict, total=False):
    messages: Annotated[list, AutoMerge()]
    value: int


async def test_prepared_and_restored_contexts_keep_distinct_run_ids():
    runs = []

    def first(state):
        runs.append(get_current_run_id())
        return Command(update={"value": 1}, goto="second")

    def second(state):
        return {"value": state["value"] + 1}

    graph = AgentGraphCreator(State).add_nodes([first, second]).compile_agent("first")
    assert isinstance(graph, AgentGraph)
    context = graph.create_context({"messages": []})
    assert await graph.invoke(graph_context=context) == {"messages": [], "value": 2}
    assert context.status == "finished"
    restored = graph.restore_context(context.get_snapshot(0))
    assert await graph.invoke(graph_context=restored) == {"messages": [], "value": 2}
    assert restored.run_id != context.run_id
    assert restored.status == "finished"
    assert runs == [context.run_id, restored.run_id]


@pytest.mark.parametrize(
    "error", [RuntimeError("node failed"), asyncio.CancelledError()]
)
async def test_failure_and_node_cancellation_record_terminal_status(error):
    async def fail(state):
        raise error

    graph = AgentGraphCreator(State).add_node(fail).compile_agent("fail")
    context = graph.create_context({"messages": []})
    with pytest.raises(type(error)):
        await graph.invoke(graph_context=context)
    assert context.status == (
        "aborted" if isinstance(error, asyncio.CancelledError) else "failed"
    )


async def test_rejected_context_reuse_preserves_active_invocation():
    started, release = asyncio.Event(), asyncio.Event()

    async def wait(state):
        started.set()
        await release.wait()
        return {}

    graph = AgentGraphCreator(State).add_node(wait).compile_agent("wait")
    context = graph.create_context({"messages": []})
    task = asyncio.create_task(graph.invoke(graph_context=context))
    await asyncio.wait_for(started.wait(), 1)
    try:
        with pytest.raises(InvalidContextError):
            await graph.invoke(graph_context=context)
        with pytest.raises(TypeError):
            await graph.invoke({}, graph_context=context)
        assert not task.done()
    finally:
        release.set()
        await asyncio.wait_for(task, 1)
    assert context.status == "finished"


async def test_foreign_context_is_rejected_without_changing_its_status():
    first = AgentGraphCreator(State).compile_agent(None)
    second = AgentGraphCreator(State).compile_agent(None)
    assert first.namespace != second.namespace
    context = first.create_context({"messages": []})
    with pytest.raises(InvalidContextError):
        await second.invoke(graph_context=context)
    assert context.status == "pending"


async def test_closing_stream_aborts_context():
    release = asyncio.Event()

    async def emit(state):
        get_stream_writer()("chunk")
        await release.wait()
        return {}

    graph = AgentGraphCreator(State).add_node(emit).compile_agent("emit")
    context = graph.create_context({"messages": []})
    async with aclosing(graph.stream(graph_context=context)) as stream:
        assert await asyncio.wait_for(anext(stream), 1) == "chunk"
    assert context.status == "aborted"
    release.set()


@pytest.mark.parametrize("compiler", ["compile_graph", "compile_agent"])
@pytest.mark.parametrize("no_snapshot", [False, True])
async def test_compiler_forwards_checkpoint_settings(compiler, no_snapshot):
    def first(state):
        return Command(update={"value": 1}, goto="second")

    def second(state):
        return {"value": state["value"] + 1}

    creator = AgentGraphCreator(State).add_nodes([first, second])
    graph = getattr(creator, compiler)(
        "first", max_steps=2, max_snapshots=1, no_snapshot=no_snapshot
    )
    context = graph.create_context({"messages": []})
    assert await graph.invoke(graph_context=context) == {"messages": [], "value": 2}
    assert context.max_snapshots == 1
    assert len(context.get_all_snapshots()) == (0 if no_snapshot else 1)


@pytest.mark.parametrize("compiler", ["compile_graph", "compile_agent"])
async def test_compiler_forwards_execution_budget(compiler):
    def repeat(state):
        return Command(goto="repeat")

    creator = AgentGraphCreator(State).add_node(repeat)
    graph = getattr(creator, compiler)("repeat", max_steps=1)
    with pytest.raises(RecursionError, match="maximum of 1 steps"):
        await graph.invoke({"messages": []})


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


async def test_server_lifespan_can_restart_with_local_events_and_health_check(
    monkeypatch,
    core_lifecycle,
    kernel_lifecycle,
):
    from apixis.core.event import (
        BuiltinChannel,
        EventType,
        get_event_pipe,
        subscribe,
        unsubscribe,
    )

    from alira.modules.web.application import create_app

    # Exercise the real event runtime without opening unrelated storage services.
    start = AsyncMock(wraps=core_lifecycle.start)
    stop = AsyncMock(wraps=core_lifecycle.stop)
    monkeypatch.setattr(core_lifecycle, "start", start)
    monkeypatch.setattr(core_lifecycle, "stop", stop)
    monkeypatch.setattr("alira.modules.web.application.auto_load_router", lambda app: None)
    app = create_app()
    for _ in range(2):
        received = asyncio.Event()

        @subscribe("alira.migration.health")
        async def receive(event, received=received):
            received.set()

        try:
            async with kernel_lifecycle.lifespan(), app.router.lifespan_context(app):
                pipe = get_event_pipe()
                assert pipe.is_running
                assert isinstance(pipe.get_channel(), BuiltinChannel)
                assert pipe.maxsize > 0
                await pipe.post_event(
                    event_type=EventType.WORKFLOW, event_name="alira.migration.health"
                )
                await asyncio.wait_for(received.wait(), 1)
                async with AsyncClient(
                    transport=ASGITransport(app), base_url="http://test"
                ) as client:
                    response = await client.get("/health")
                assert response.status_code == 200
                assert response.json() == {"status": "ok", "service": "alira"}
            assert not pipe.is_running
        finally:
            unsubscribe("receive")
    assert start.await_count == 2
    assert stop.await_count == 2


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

from my_llm import ToolCall

"""Resident tool services stay usable across bounded graph request lifetimes."""

import asyncio
from typing import Annotated, TypedDict

import pytest
from apixis.core.event import (
    ApixEventHandler,
    ApixEventPipe,
    BuiltinChannel,
    EventType,
    factory,
    get_event_pipe,
    get_handler_registry,
    wait_for_event,
)
from apixis.core.graph import AutoMerge, GraphManager

from alira.agent.core.tool import FunctionToolManager, ToolNode
from alira.shared.message import AliraAssistantMessage

pytestmark = pytest.mark.parametrize(
    "event_runtime_capacity",
    [(1, 1), (1, 2), (2, 1), (2, 2)],
    indirect=True,
)


class State(TypedDict):
    messages: Annotated[list, AutoMerge()]


@pytest.fixture
def event_runtime_capacity(request, monkeypatch):
    """Construct the real core with independently bounded queue and dispatch."""
    capacity, queue_capacity = request.param
    monkeypatch.setattr(
        factory,
        "ApixEventPipe",
        lambda: ApixEventPipe(builtin=BuiltinChannel(maxsize=queue_capacity)),
    )
    return capacity


def _state(name: str, *, value: str = "ok", call_id: str = "call") -> dict:
    return {
        "messages": [
            AliraAssistantMessage(
                content=[
                    ToolCall(call_id=call_id, name=name, arguments={"value": value})
                ]
            )
        ]
    }


def _graph(node: ToolNode):
    return GraphManager(State).add_node(node).compile_graph(node.name)


async def test_nested_tool_requests_complete_at_small_runtime_capacities():
    async def inner(value: str) -> str:
        return f"inner:{value}"

    inner_node = ToolNode(FunctionToolManager([inner]), name="inner-tools")

    async def outer(value: str) -> str:
        command = await inner_node.execute(_state("inner", value=value))
        return f"outer:{command.update['messages'][0].content[0].text}"

    manager = FunctionToolManager([outer])
    result = await asyncio.wait_for(
        _graph(ToolNode(manager)).invoke(_state("outer")), 2
    )

    assert result["messages"][-1].content[0].text == "outer:inner:ok"


async def test_tools_in_one_batch_can_wait_for_each_other():
    second_started = asyncio.Event()
    completed = []

    async def first(value: str) -> str:
        await second_started.wait()
        completed.append("first")
        return value

    async def second(value: str) -> str:
        completed.append("second")
        second_started.set()
        return value

    manager = FunctionToolManager([first, second])
    state = _state("first", value="one", call_id="first-call")
    state["messages"][0].content.extend(
        _state("second", value="two", call_id="second-call")["messages"][0].content
    )
    result = await asyncio.wait_for(_graph(ToolNode(manager)).invoke(state), 2)

    assert completed == ["second", "first"]
    assert [message.content[0].text for message in result["messages"][1:]] == [
        "one",
        "two",
    ]


async def test_graph_cancellation_preserves_service_and_independent_active_work():
    started = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()

    async def work(value: str) -> str:
        if value == "block":
            started.set()
            try:
                await release.wait()
            finally:
                await asyncio.sleep(0)
                completed.set()
        return value

    manager = FunctionToolManager([work])
    graph = _graph(ToolNode(manager))
    task = asyncio.create_task(graph.invoke(_state("work", value="block")))
    try:
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)

        # Apixis aborts the graph context while its current node may continue.
        # The resident manager accepts a new graph's work in the meantime.
        assert not completed.is_set()
        result = await asyncio.wait_for(graph.invoke(_state("work", value="next")), 2)
        assert result["messages"][-1].content[0].text == "next"
    finally:
        release.set()
        await asyncio.wait_for(completed.wait(), 2)


async def test_tool_node_cancellation_joins_asynchronous_tool_cleanup():
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleaned = asyncio.Event()

    async def work(value: str) -> str:
        if value == "block":
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await release_cleanup.wait()
                cleaned.set()
        return value

    manager = FunctionToolManager([work])
    node = ToolNode(manager)
    task = asyncio.create_task(node.execute(_state("work", value="block")))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    try:
        await asyncio.wait_for(cleanup_started.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
    assert cleaned.is_set()
    result = await asyncio.wait_for(node.execute(_state("work", value="next")), 2)
    assert result.update["messages"][0].content[0].text == "next"


async def test_graph_timeout_cleans_request_and_preserves_service():
    cancelled = asyncio.Event()

    async def work(value: str) -> str:
        if value == "block":
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return value

    manager = FunctionToolManager([work])
    node = ToolNode(manager)
    graph = GraphManager(State).add_node(node, timeout=0.05).compile_graph(node.name)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(graph.invoke(_state("work", value="block")), 2)

    assert cancelled.is_set()
    result = await asyncio.wait_for(graph.invoke(_state("work", value="next")), 2)
    assert result["messages"][-1].content[0].text == "next"


async def test_service_stop_finishes_active_request_cleanup_and_can_restart():
    started = asyncio.Event()
    release = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleaned = asyncio.Event()

    async def work(value: str) -> str:
        if value == "block":
            started.set()
            try:
                await release.wait()
            finally:
                cleanup_started.set()
                await release_cleanup.wait()
                cleaned.set()
        return value

    manager = FunctionToolManager([work])
    node = ToolNode(manager)
    active = asyncio.create_task(node.execute(_state("work", value="block")))
    await asyncio.wait_for(started.wait(), 2)
    stopping = asyncio.create_task(manager.stop())
    # Both draining and cancelling are valid shutdown policies; return must
    # always include completion of the owned request's asynchronous cleanup.
    release.set()
    try:
        await asyncio.wait_for(cleanup_started.wait(), 2)
        assert not stopping.done()
    finally:
        release_cleanup.set()
        await asyncio.wait_for(stopping, 2)
        await asyncio.gather(active, return_exceptions=True)
    assert cleaned.is_set()

    await manager.start()
    result = await asyncio.wait_for(node.execute(_state("work", value="restart")), 2)
    assert result.update["messages"][0].content[0].text == "restart"


async def test_cancelled_queued_request_never_executes(event_runtime_capacity):
    capacity = event_runtime_capacity
    occupied = asyncio.Barrier(capacity + 1)
    release = asyncio.Event()
    observed = []

    async def hold(event):
        await occupied.wait()
        await release.wait()

    holder = ApixEventHandler(hold, name="test-tool-manager-capacity-holder")
    holder.register("test.tool-manager.hold")
    pipe = get_event_pipe()
    for _ in range(capacity):
        await pipe.post_event(
            event_type=EventType.TRANSIENT, event_name="test.tool-manager.hold"
        )
    await asyncio.wait_for(occupied.wait(), 2)

    def work(value: str) -> str:
        observed.append(value)
        return value

    manager = FunctionToolManager([work])
    node = ToolNode(manager)
    await manager.start()
    # A processed observation confirms node.execute has published before
    # cancellation, while occupied permits prevent its tool handler starting.
    published = asyncio.Event()
    original_post = pipe.post_event

    async def observe_publication(**kwargs):
        await original_post(**kwargs)
        published.set()

    pipe.post_event = observe_publication
    task = asyncio.create_task(node.execute(_state("work", value="cancelled")))
    try:
        await asyncio.wait_for(published.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
    finally:
        pipe.post_event = original_post
        release.set()
        holder.unregister()

    async with wait_for_event("test.tool-manager.drained", point="processed") as waiter:
        await pipe.post_event(
            event_type=EventType.TRANSIENT, event_name="test.tool-manager.drained"
        )
        await asyncio.wait_for(waiter.wait(), 2)
    assert observed == []


async def test_failed_manager_start_removes_partial_registration(monkeypatch):
    manager = FunctionToolManager([lambda value: value])
    registry = get_handler_registry()
    before = set(registry.registry)
    register = registry.register_handler

    def fail_after_register(*args, **kwargs):
        register(*args, **kwargs)
        raise RuntimeError("expected startup failure")

    with monkeypatch.context() as patch:
        patch.setattr(registry, "register_handler", fail_after_register)
        with pytest.raises(RuntimeError, match="expected startup failure"):
            await manager.start()
    assert set(registry.registry) == before

    await manager.start()
    result = await asyncio.wait_for(
        ToolNode(manager).execute(_state("<lambda>", value="recovered")), 2
    )
    assert result.update["messages"][0].content[0].text == "recovered"


async def test_stopped_manager_rejects_queued_calls_after_restart(
    event_runtime_capacity, monkeypatch
):
    capacity = event_runtime_capacity
    occupied = asyncio.Barrier(capacity + 1)
    release = asyncio.Event()
    observed = []

    async def hold(event):
        await occupied.wait()
        await release.wait()

    holder = ApixEventHandler(hold, name="test-tool-manager-restart-holder")
    holder.register("test.tool-manager.restart-hold")
    pipe = get_event_pipe()
    for _ in range(capacity):
        await pipe.post_event(
            event_type=EventType.TRANSIENT,
            event_name="test.tool-manager.restart-hold",
        )
    await asyncio.wait_for(occupied.wait(), 2)

    def work(value: str) -> str:
        observed.append(value)
        return value

    manager = FunctionToolManager([work])
    node = ToolNode(manager)
    await manager.start()
    published = asyncio.Event()
    original_post = pipe.post_event

    async def observe_publication(**kwargs):
        await original_post(**kwargs)
        published.set()

    with monkeypatch.context() as patch:
        patch.setattr(pipe, "post_event", observe_publication)
        task = asyncio.create_task(node.execute(_state("work", value="old")))
        try:
            await asyncio.wait_for(published.wait(), 2)
            await manager.stop()
            await manager.start()
        finally:
            release.set()
            holder.unregister()
        with pytest.raises(RuntimeError, match="unavailable"):
            await asyncio.wait_for(task, 2)

    assert observed == []
    result = await asyncio.wait_for(node.execute(_state("work", value="new")), 2)
    assert result.update["messages"][0].content[0].text == "new"
    assert observed == ["new"]


async def test_cancelled_stop_wait_keeps_owned_shutdown_and_allows_restart():
    started = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()

    async def work(value: str) -> str:
        if value == "block":
            started.set()
            try:
                await release.wait()
            finally:
                completed.set()
        return value

    manager = FunctionToolManager([work])
    node = ToolNode(manager)
    active = asyncio.create_task(node.execute(_state("work", value="block")))
    await asyncio.wait_for(started.wait(), 2)
    stopping = asyncio.create_task(manager.stop())
    # The gate closes synchronously before stop waits for its active work.
    async with asyncio.timeout(2):
        while manager.is_started:
            await asyncio.sleep(0)
    stopping.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(stopping, 2)
        assert not completed.is_set()
        assert not manager.is_started
    finally:
        release.set()
        await asyncio.wait_for(manager.stop(), 2)
        await asyncio.wait_for(active, 2)

    assert completed.is_set()
    await manager.start()
    result = await asyncio.wait_for(node.execute(_state("work", value="new")), 2)
    assert result.update["messages"][0].content[0].text == "new"

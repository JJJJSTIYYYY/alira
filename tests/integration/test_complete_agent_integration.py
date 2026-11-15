from my_llm import FunctionTool, TextContent, ToolCall

"""Complex graph/tool integration tests that simulate a full Agent turn."""

import asyncio
from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict

import pytest
from apixis.core.graph import (
    AutoMerge,
    Command,
    KeepRef,
)

from alira.modules.agent.src.core.graph import AgentGraphCreator
from alira.modules.tool.src import (
    AutoInjection,
    ToolInjectionContext,
    ToolNode,
    tool,
)
from alira.modules.agent.src.shared.message import (
    AliraAssistantMessage,
    AliraToolMessage,
    AliraUserMessage,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


@dataclass
class AgentMemory:
    """Mutable resource deliberately shared by live node state copies."""

    notes: list[str] = field(default_factory=list)
    call_ids: list[str] = field(default_factory=list)


@dataclass
class InMemoryMessageStore:
    """Persistence stand-in that records new messages in write batches."""

    messages: list[Any] = field(default_factory=list)
    message_uids: set[str] = field(default_factory=set)
    batches: list[list[str]] = field(default_factory=list)

    def __add__(self, messages: list[Any]) -> "InMemoryMessageStore":
        """Persist one AutoMerge update without replacing the store object."""
        self.messages.extend(messages)
        self.message_uids.update(message.message_uid for message in messages)
        self.batches.append([message.role for message in messages])
        return self


class CompleteAgentState(TypedDict, total=False):
    messages: Annotated[list[Any], AutoMerge()]
    audit: Annotated[list[str], AutoMerge()]
    lifecycle: Annotated[list[str], AutoMerge()]
    model_calls: Annotated[int, AutoMerge()]
    context_preparations: Annotated[int, AutoMerge()]
    memory: Annotated[AgentMemory, KeepRef()]
    message_store: Annotated[
        InMemoryMessageStore,
        AutoMerge(),
        KeepRef(),
    ]
    prepared_context: list[Any]
    calculation: int


def _call(tool_name: str, call_id: str, args: dict[str, Any]) -> ToolCall:
    return ToolCall(
        call_id=call_id, name=tool_name, arguments=args if args is not None else {}
    )


class ScriptedBot:
    """Deterministic stand-in for a model provider in an Agent loop."""

    def __init__(self) -> None:
        self.tool_schemas: list[FunctionTool] = []
        self.requests: list[list[Any]] = []

    def bind_tools(self, tool_node: ToolNode) -> "ScriptedBot":
        self.tool_schemas = tool_node.get_schemas()
        return self

    async def invoke(self, messages: list[Any]) -> AliraAssistantMessage:
        self.requests.append(list(messages))
        await asyncio.sleep(0)

        tool_messages = [
            message for message in messages if isinstance(message, AliraToolMessage)
        ]
        if not tool_messages:
            return AliraAssistantMessage(
                name="Alice",
                content=[
                    _call("lookup_weather", "call-weather", {"city": "Tokyo"}),
                    _call("add", "call-add", {"left": 19, "right": 23}),
                    _call("remember", "call-memory", {"note": "Tokyo is warm"}),
                ],
            )

        outputs = {message.name: message.content[0].text for message in tool_messages}
        return AliraAssistantMessage(
            name="Alice",
            content=(
                [
                    TextContent(
                        f"{outputs['lookup_weather']}; sum={outputs['add']}; memory={outputs['remember']}"
                    )
                ]
            ),
        )


async def test_full_agent_model_tool_model_loop_with_shared_runtime_state():
    """Prepare, model, persist, tools, persist, and final model work together."""
    remember_started = asyncio.Event()
    completion_order: list[str] = []

    @tool(description="Look up current weather for a city.")
    async def lookup_weather(city: str) -> str:
        await remember_started.wait()
        await asyncio.sleep(0.01)
        completion_order.append("lookup_weather")
        return f"weather:{city}:sunny"

    @tool(description="Add two integers.")
    async def add(left: int, right: int) -> int:
        await asyncio.sleep(0)
        completion_order.append("add")
        return left + right

    @tool(description="Remember one note for the current Agent run.")
    async def remember(
        note: str,
        runtime: Annotated[ToolInjectionContext, AutoInjection()],
    ) -> str:
        remember_started.set()
        runtime.state["memory"].notes.append(note)
        runtime.state["memory"].call_ids.append(runtime.tool_call_id)
        completion_order.append("remember")
        return "stored"

    tool_node = ToolNode(
        [lookup_weather, add, remember], bind_llm_node="persist_messages"
    )
    bot = ScriptedBot().bind_tools(tool_node)

    def prepare_context(state: dict[str, Any]) -> Command:
        """Build the exact message snapshot supplied to the next model call."""
        preparation_number = state.get("context_preparations", 0) + 1
        return Command(
            goto="model",
            update={
                "prepared_context": list(state["messages"]),
                "context_preparations": 1,
                "lifecycle": [f"prepare_context:{preparation_number}"],
            },
        )

    async def call_model(state: dict[str, Any]) -> Command:
        response = await bot.invoke(state["prepared_context"])
        return Command(
            goto="persist_messages",
            update={
                "messages": [response],
                "model_calls": 1,
                "lifecycle": ["model"],
            },
        )

    def persist_messages(state: dict[str, Any]) -> Command:
        """Persist only unseen messages and route from the stored message type."""
        store = state["message_store"]
        pending_messages = [
            message
            for message in state["messages"]
            if message.message_uid not in store.message_uids
        ]
        latest = state["messages"][-1]
        if isinstance(latest, AliraAssistantMessage):
            next_node = (
                tool_node.name
                if any(isinstance(part, ToolCall) for part in latest.content)
                else None
            )
        else:
            next_node = "prepare_context"

        update = {
            "message_store": pending_messages,
            "lifecycle": [f"persist:{latest.role}:{len(pending_messages)}"],
        }
        for message in pending_messages:
            if isinstance(message, AliraToolMessage) and message.name == "add":
                update["calculation"] = int(message.content[0].text)
                update["audit"] = ["calculated:19+23"]
        return Command(update=update, goto=next_node)

    graph = (
        AgentGraphCreator(CompleteAgentState)
        .add_node(prepare_context)
        .add_node(call_model, "model")
        .add_node(tool_node)
        .add_node(persist_messages)
        .compile_agent("prepare_context")
    )

    memory = AgentMemory()
    message_store = InMemoryMessageStore()
    user_message = AliraUserMessage(content=[TextContent("Tokyo weather and 19 + 23?")])
    initial_state = {
        "messages": [user_message],
        "audit": [],
        "lifecycle": [],
        "model_calls": 0,
        "context_preparations": 0,
        "memory": memory,
        "message_store": message_store,
    }

    result = await asyncio.wait_for(graph.invoke(initial_state), timeout=1)

    assert [schema.name for schema in bot.tool_schemas] == [
        "lookup_weather",
        "add",
        "remember",
    ]
    remember_parameters = bot.tool_schemas[2].parameters
    assert set(remember_parameters["properties"]) == {"note"}
    assert remember_parameters["required"] == ["note"]

    assert completion_order[-1] == "lookup_weather"
    assert set(completion_order[:2]) == {"add", "remember"}
    assert len(bot.requests) == 2
    assert [len(request) for request in bot.requests] == [1, 5]
    assert isinstance(bot.requests[0][0], AliraUserMessage)
    assert all(
        isinstance(message, AliraToolMessage) for message in bot.requests[1][-3:]
    )
    assert result["model_calls"] == 2
    assert result["context_preparations"] == 2
    assert result["calculation"] == 42
    assert result["audit"] == ["calculated:19+23"]
    assert result["lifecycle"] == [
        "prepare_context:1",
        "model",
        "persist:assistant:2",
        "persist:tool:3",
        "prepare_context:2",
        "model",
        "persist:assistant:1",
    ]

    assert result["memory"] is memory
    assert memory.notes == ["Tokyo is warm"]
    assert memory.call_ids == ["call-memory"]
    assert result["message_store"] is message_store
    assert message_store.batches == [
        ["user", "assistant"],
        ["tool", "tool", "tool"],
        ["assistant"],
    ]
    assert [message.role for message in message_store.messages] == [
        "user",
        "assistant",
        "tool",
        "tool",
        "tool",
        "assistant",
    ]
    assert len(message_store.message_uids) == 6

    messages = result["messages"]
    assert messages[0] is not user_message
    assert isinstance(messages[0], AliraUserMessage)
    assert isinstance(messages[1], AliraAssistantMessage)
    assert [message.name for message in messages[2:5]] == [
        "lookup_weather",
        "add",
        "remember",
    ]
    assert [message.call_id for message in messages[2:5]] == [
        "call-weather",
        "call-add",
        "call-memory",
    ]
    assert all(message.metadata["duration"] >= 0 for message in messages[2:5])
    assert isinstance(messages[-1], AliraAssistantMessage)
    assert not any(isinstance(part, ToolCall) for part in messages[-1].content)
    assert messages[-1].content[0].text == (
        "weather:Tokyo:sunny; sum=42; memory=stored"
    )

    # Ordinary input was isolated, while the explicitly marked resource was
    # intentionally shared back to its caller.
    assert initial_state["messages"] == [user_message]
    assert initial_state["audit"] == []
    assert initial_state["lifecycle"] == []

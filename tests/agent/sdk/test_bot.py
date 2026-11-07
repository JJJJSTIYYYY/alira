"""Alira application responsibilities around the library's standard Chat API."""

from copy import deepcopy
from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from my_llm import (
    AssistantMessage,
    Attribute,
    Config,
    InputParameter,
    ModelProfile,
    ProviderProfile,
    ReasoningContent,
    TextContent,
    ToolCall,
    UserMessage,
)

from alira.agent.core.bot import AliraBot, BaseProvider, CustomProvider
from alira.agent.core.tool import ToolNode, tool
from alira.utils.message import (
    AliraAssistantMessage,
    AliraSystemMessage,
    AliraUserMessage,
)


@tool
def weather(city: str) -> str:
    """Get weather for a city."""
    return f"sunny in {city}"


def make_profile_bot(*, provider, model, **kwargs):
    name = f"test-{uuid4().hex}"
    BaseProvider.register(
        CustomProvider(
            name=name,
            profile=replace(provider, models=Attribute(default_value=[model])),
        )
    )
    try:
        return AliraBot(provider=name, model=model.model.default_value, **kwargs)
    finally:
        BaseProvider.unregister(name)


def make_bot(**kwargs):
    return make_profile_bot(
        provider=ProviderProfile(
            protocol=Config(default_value="openai_chat"),
            api_key=InputParameter(
                path="$.api_key",
                default_value="test",
            ),
        ),
        model=ModelProfile(
            model=Attribute(default_value="test"),
            supports_function_calling=Config(default_value=True),
        ),
        **kwargs,
    )


def test_profiles_are_library_types_and_stdio_is_explicit():
    bot = make_bot()
    assert bot.llm.stdio_protocol == "middleware"
    assert bot.model == "test"
    assert isinstance(bot.provider, CustomProvider)


def test_tool_binding_filters_copies_replaces_and_rejects_duplicate_names():
    bot = make_bot()
    assert bot.bind_tools([weather]) is bot
    schemas = bot.tool_schemas
    schemas[0].name = "changed"
    assert bot.tool_schemas == [weather.get_schema()]
    bot.bind_tools(ToolNode(weather), tool_permission_set=set())
    assert bot.tool_schemas == []
    with pytest.raises(ValueError, match="unique"):
        bot.bind_tools([weather, weather])
    for invalid in ([object()], "weather", 12, {}):
        with pytest.raises(TypeError):
            bot.bind_tools(invalid)


@pytest.mark.asyncio
async def test_invoke_orders_context_and_delegates_standard_messages_and_options(
    monkeypatch,
):
    role = {"name": " Alice ", "definition": "Be concise", "title": None}
    bot = make_bot(role_schema=role).bind_tools([weather])
    role["name"] = "changed"
    assert bot.name == "Alice"
    call = ToolCall("c1", "weather", {"city": "Tokyo"})
    response = AssistantMessage(
        role="assistant", content=[ReasoningContent("why"), call]
    )
    generate = AsyncMock(return_value=deepcopy(response))
    monkeypatch.setattr(bot.llm, "generate", generate)
    result = await bot.invoke(
        [
            AliraUserMessage(
                content=[TextContent("weather")], extensions={"active_file": "notes"}
            )
        ],
        [AliraSystemMessage(content=[TextContent("global")])],
        reasoning=True,
        max_tokens=40,
    )
    sent = generate.await_args.kwargs
    assert sent["messages"][0].content[0].text == "global"
    assert "Your name: Alice" in sent["messages"][1].content[0].text
    assert sent["messages"][2].content[0].text == "weather"
    assert sent["tools"] == [weather.get_schema()]
    assert sent["reasoning"] is True and sent["max_tokens"] == 40
    assert isinstance(result, AliraAssistantMessage)
    assert result.content[1] == call
    assert result.content[0].reasoning == "why"
    assert result.extensions == {}
    await bot.invoke([UserMessage(role="user", content=[TextContent("hi")])], tools=[])
    assert generate.await_args.kwargs["tools"] == []
    assert bot.tool_schemas


@pytest.mark.asyncio
async def test_stream_returns_library_object_without_wrapping(monkeypatch):
    bot = make_bot()
    stream = object()
    generate = AsyncMock(return_value=stream)
    monkeypatch.setattr(bot.llm, "generate", generate)
    assert (
        await bot.stream([UserMessage(role="user", content=[TextContent("hi")])])
        is stream
    )
    assert generate.await_args.kwargs["stream"] is True


def test_input_and_role_validation():
    bot = make_bot()
    with pytest.raises(TypeError):
        bot._messages((), None)
    with pytest.raises(TypeError):
        bot._messages([], ())
    for role in ({}, {"name": "x", "definition": "x", "title": 1}):
        with pytest.raises(TypeError):
            bot.bind_role_schema(role)

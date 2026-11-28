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
    FunctionTool,
    InputParameter,
    ModelProfile,
    ProviderProfile,
    ReasoningContent,
    TextContent,
    ToolCall,
    UserMessage,
    omit,
)

from alira.modules.agent.src.core.bot import AliraBot, BaseProvider, CustomProvider
from alira.modules.tool.src import tool
from alira.modules.agent.src.shared.model.message import (
    AliraAssistantMessage,
    AliraCompressedMessage,
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


def test_tool_binding_exposes_independent_schema_copies():
    bot = make_bot()
    assert bot.bind_tools([weather.get_schema()]) is bot
    schemas = bot.tool_schemas
    schemas[0].name = "changed"
    schemas[0].parameters["properties"]["city"]["type"] = "integer"
    assert bot.tool_schemas == [weather.get_schema()]


@pytest.mark.parametrize("method", ["invoke", "stream", "generate_json"])
@pytest.mark.parametrize("max_tokens", [40, None, omit])
async def test_generation_options_are_forwarded(monkeypatch, method, max_tokens):
    bot = make_bot()
    generate = AsyncMock(
        return_value=AssistantMessage(role="assistant", content=[TextContent("ok")])
    )
    monkeypatch.setattr(bot.llm, "generate", generate)
    options = {
        "api_key": omit,
        "max_tokens": max_tokens,
        "reasoning": True,
        "reasoning_effort": "high",
        "extra_headers": {"X-Request-ID": "request-1"},
        "extra_body": {"temperature": 0.2},
        "previous_response_id": "response-1",
    }
    if method == "generate_json":
        options.update(schema={"type": "object"}, max_retries=2)
        generate.return_value = {"answer": "ok"}
        monkeypatch.setattr(bot.llm, "generate_json", generate)

    await getattr(bot, method)([AliraUserMessage(content=[TextContent("hi")])], **options)

    sent = generate.await_args.kwargs
    assert {name: sent[name] for name in options} == options
    if method == "generate_json":
        assert "stream" not in sent
    else:
        assert sent["stream"] is (method == "stream")


@pytest.mark.parametrize("method", ["invoke", "stream", "generate_json"])
async def test_unspecified_token_limit_is_left_to_library(monkeypatch, method):
    bot = make_bot()
    generate = AsyncMock(
        return_value=AssistantMessage(role="assistant", content=[TextContent("ok")])
    )
    monkeypatch.setattr(bot.llm, "generate", generate)
    options = {}
    if method == "generate_json":
        options["schema"] = {"type": "object"}
        generate.return_value = {}
        monkeypatch.setattr(bot.llm, "generate_json", generate)

    await getattr(bot, method)([AliraUserMessage(content=[TextContent("hi")])], **options)

    assert "max_tokens" not in generate.await_args.kwargs


async def test_generate_json_orders_context_and_returns_library_result(monkeypatch):
    bot = make_bot(role_schema={"name": "Alice", "definition": "Be concise"})
    bot.bind_tools([weather.get_schema()]).set_tool_permission({"weather"})
    result = {"answer": "ok"}
    generate_json = AsyncMock(return_value=result)
    monkeypatch.setattr(bot.llm, "generate_json", generate_json)
    history = [AliraUserMessage(content=[TextContent("question")])]
    prompts = [AliraSystemMessage(content=[TextContent("rules")])]
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}}

    assert await bot.generate_json(history, prompts, schema=schema) is result

    generate_json.assert_awaited_once()
    sent = generate_json.await_args.kwargs
    assert sent["schema"] is schema
    assert sent["max_retries"] == 0
    assert sent["messages"][0] is prompts[0]
    assert isinstance(sent["messages"][1], AliraSystemMessage)
    assert "Alice" in sent["messages"][1].content[0].text
    assert "Be concise" in sent["messages"][1].content[0].text
    assert sent["messages"][2] is history[0]
    assert len(history) == len(prompts) == 1
    assert "tools" not in sent and "stream" not in sent
    assert bot.tool_schemas == [weather.get_schema()]


@pytest.mark.parametrize("method", ["invoke", "stream", "generate_json"])
@pytest.mark.parametrize(
    "length,compressed_indices,expected_start",
    [(0, (), 0), (3, (), 0), (3, (0,), 0), (3, (1,), 1), (3, (2,), 2), (5, (1, 3), 3)],
)
async def test_generation_keeps_history_from_last_compressed_message(
    monkeypatch, method, length, compressed_indices, expected_start
):
    bot = make_bot(role_schema={"name": "Alice", "definition": "Be concise"})
    history = [AliraUserMessage(content=[TextContent(str(i))]) for i in range(length)]
    for index in compressed_indices:
        history[index] = AliraCompressedMessage(content=[TextContent(f"Summary {index}")])
    original_history = history.copy()
    prompts = [AliraSystemMessage(content=[TextContent("rules")])]
    generate = AsyncMock(return_value=AssistantMessage(role="assistant"))
    options = {}
    if method == "generate_json":
        generate.return_value = {}
        options["schema"] = {"type": "object"}
    monkeypatch.setattr(
        bot.llm, "generate_json" if method == "generate_json" else "generate", generate
    )

    await getattr(bot, method)(history, prompts, **options)

    sent = generate.await_args.kwargs["messages"]
    assert sent[0] is prompts[0]
    assert isinstance(sent[1], AliraSystemMessage)
    assert "Alice" in sent[1].content[0].text
    assert sent[2:] == original_history[expected_start:]
    assert history == original_history
    assert len(prompts) == 1


@pytest.mark.parametrize("streaming", [False, True])
async def test_bound_tools_are_available_without_an_explicit_permission_filter(
    monkeypatch, streaming
):
    bot = make_bot().bind_tools([weather.get_schema()])
    generate = AsyncMock(return_value=AssistantMessage(role="assistant", content=[TextContent("ok")]))
    monkeypatch.setattr(bot.llm, "generate", generate)

    request = bot.stream if streaming else bot.invoke
    await request([AliraUserMessage(content=[TextContent("hi")])])

    assert generate.await_args.kwargs["tools"] == [weather.get_schema()]


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("replacement", [[], [FunctionTool(name="other")]])
async def test_rebinding_tools_replaces_or_clears_tools_sent_to_the_model(
    monkeypatch, streaming, replacement
):
    bot = make_bot().bind_tools([weather.get_schema()]).set_tool_permission()
    assert bot.bind_tools(replacement) is bot
    generate = AsyncMock(return_value=AssistantMessage(role="assistant", content=[TextContent("ok")]))
    monkeypatch.setattr(bot.llm, "generate", generate)

    request = bot.stream if streaming else bot.invoke
    await request([AliraUserMessage(content=[TextContent("hi")])])

    assert generate.await_args.kwargs["tools"] == replacement


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("permissions,expected_names", [
    (None, ["weather", "other"]),
    (set(), []),
    ({"weather"}, ["weather"]),
])
async def test_permission_selection_survives_tool_binding_and_rebinding(
    monkeypatch, streaming, permissions, expected_names
):
    bot = make_bot().set_tool_permission(permissions)
    generate = AsyncMock(
        return_value=AssistantMessage(role="assistant", content=[TextContent("ok")])
    )
    monkeypatch.setattr(bot.llm, "generate", generate)
    request = bot.stream if streaming else bot.invoke

    for tools in ([], [weather.get_schema(), FunctionTool(name="other")]):
        bot.bind_tools(tools)
        await request([AliraUserMessage(content=[TextContent("hi")])])
        sent_tools = generate.await_args.kwargs["tools"]
        assert [schema.name for schema in sent_tools] == (expected_names if tools else [])


async def test_permission_selection_is_independent_of_the_callers_set(monkeypatch):
    permissions = {"weather"}
    bot = make_bot().set_tool_permission(permissions)
    permissions.clear()
    bot.bind_tools([weather.get_schema()])
    generate = AsyncMock(
        return_value=AssistantMessage(role="assistant", content=[TextContent("ok")])
    )
    monkeypatch.setattr(bot.llm, "generate", generate)

    await bot.invoke([AliraUserMessage(content=[TextContent("hi")])])

    assert generate.await_args.kwargs["tools"] == [weather.get_schema()]


async def test_tool_permissions_can_be_changed_and_reset(monkeypatch):
    schema = weather.get_schema()
    bot = make_bot().bind_tools([schema])
    generate = AsyncMock(return_value=AssistantMessage(role="assistant", content=[TextContent("ok")]))
    monkeypatch.setattr(bot.llm, "generate", generate)

    for permissions, expected in (
        (set(), []),
        ({"weather"}, [schema]),
        ({"unknown"}, []),
        (None, [schema]),
    ):
        assert bot.set_tool_permission(permissions) is bot
        await bot.invoke([AliraUserMessage(content=[TextContent("hi")])])
        assert generate.await_args.kwargs["tools"] == expected
        assert bot.tool_schemas == [schema]


async def test_generator_tool_binding_survives_permission_changes(monkeypatch):
    schema = weather.get_schema()
    bot = make_bot().bind_tools(item for item in [schema])
    generate = AsyncMock(return_value=AssistantMessage(role="assistant", content=[TextContent("ok")]))
    monkeypatch.setattr(bot.llm, "generate", generate)

    for permissions in ({"weather"}, None):
        bot.set_tool_permission(permissions)
        await bot.invoke([AliraUserMessage(content=[TextContent("hi")])])
        assert generate.await_args.kwargs["tools"] == [schema]


@pytest.mark.asyncio
async def test_invoke_orders_context_and_delegates_standard_messages_and_options(
    monkeypatch,
):
    role = {"name": " Alice ", "definition": "Be concise", "tag": " helper "}
    bot = (
        make_bot(role_schema=role)
        .bind_tools([weather.get_schema()])
        .set_tool_permission()
    )
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
        api_key="invoke-test-key",
        reasoning=True,
        max_tokens=40,
    )
    sent = generate.await_args.kwargs
    assert sent["messages"][0].content[0].text == "global"
    assert isinstance(sent["messages"][1], AliraSystemMessage)
    role_prompt = sent["messages"][1].content[0].text
    assert all(value in role_prompt for value in ("Alice", "helper", "Be concise"))
    assert "changed" not in role_prompt
    assert sent["messages"][2].content[0].text == "weather"
    assert sent["tools"] == [weather.get_schema()]
    assert sent["api_key"] == "invoke-test-key"
    assert sent["reasoning"] is True and sent["max_tokens"] == 40
    assert isinstance(result, AliraAssistantMessage)
    assert result.content[1] == call
    assert result.content[0].reasoning == "why"
    assert result.extensions == {}
    bot.set_tool_permission(set())
    await bot.invoke([AliraUserMessage(content=[TextContent("hi")])])
    assert generate.await_args.kwargs["tools"] == []
    assert generate.await_args.kwargs["api_key"] is None
    assert bot.llm.provider.api_key.default_value == "test"
    assert bot.tool_schemas


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("optional_fields", [{}, {"tag": None}, {"tag": ""}])
async def test_role_without_a_tag_can_generate(monkeypatch, streaming, optional_fields):
    bot = make_bot(role_schema={
        "name": "Alice", "definition": "Be concise", **optional_fields
    })
    generate = AsyncMock(return_value=AssistantMessage(role="assistant", content=[TextContent("ok")]))
    monkeypatch.setattr(bot.llm, "generate", generate)

    request = bot.stream if streaming else bot.invoke
    await request([AliraUserMessage(content=[TextContent("hi")])])

    prompt = generate.await_args.kwargs["messages"][0]
    assert isinstance(prompt, AliraSystemMessage)
    assert "Alice" in prompt.content[0].text
    assert "Be concise" in prompt.content[0].text


@pytest.mark.asyncio
@pytest.mark.parametrize("api_key", [None, "stream-test-key"])
async def test_stream_returns_library_object_without_wrapping(monkeypatch, api_key):
    bot = make_bot()
    stream = object()
    generate = AsyncMock(return_value=stream)
    monkeypatch.setattr(bot.llm, "generate", generate)
    assert (
        await bot.stream(
            [UserMessage(role="user", content=[TextContent("hi")])], api_key=api_key
        )
        is stream
    )
    assert generate.await_args.kwargs["stream"] is True
    assert generate.await_args.kwargs["api_key"] == api_key
    assert bot.llm.provider.api_key.default_value == "test"


def test_input_and_role_validation():
    bot = make_bot()
    with pytest.raises(TypeError):
        bot._messages((), None)
    with pytest.raises(TypeError):
        bot._messages([], ())
    for role in ({}, {"name": "x", "definition": "x", "tag": 1}):
        with pytest.raises(TypeError):
            bot.bind_role_schema(role)

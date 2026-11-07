"""Actual SDK transport tests through the library; no Alira codec or client injection."""

import json

import httpx
import pytest
from anthropic import AsyncAnthropic
from my_llm import (
    Attribute,
    Config,
    IncompleteStreamError,
    InputParameter,
    MessageEvent,
    ModelProfile,
    ProviderProfile,
    TextContent,
    UserMessage,
)
from openai import AsyncOpenAI

from alira.agent.core.tool import ToolNode
from alira.shared.message import AliraAssistantMessage, AliraUserMessage
from tests.agent.sdk.test_bot import make_profile_bot, weather


def wire(protocol):
    if protocol == "openai_chat":
        events = [
            {
                "id": "c",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "test",
                "choices": [
                    {"index": 0, "delta": {"content": "hello"}, "finish_reason": "stop"}
                ],
            }
        ]
    elif protocol == "openai_responses":
        events = [
            {
                "type": "response.completed",
                "sequence_number": 1,
                "response": {
                    "id": "r",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "id": "m",
                            "role": "assistant",
                            "status": "completed",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "hello",
                                    "annotations": [],
                                }
                            ],
                        }
                    ],
                },
            }
        ]
    elif protocol == "anthropic":
        events = [
            {
                "type": "message_start",
                "message": {
                    "id": "m",
                    "type": "message",
                    "role": "assistant",
                    "model": "test",
                    "content": [],
                    "stop_reason": None,
                    "usage": {"input_tokens": 1, "output_tokens": 0},
                },
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "hello"},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 1},
            },
            {"type": "message_stop"},
        ]
    return "".join(
        (f"event: {e['type']}\n" if "type" in e else "") + f"data: {json.dumps(e)}\n\n"
        for e in events
    )


def install_sdk(monkeypatch, protocol, handler):
    clients = []

    def openai_factory(**kwargs):
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        clients.append(http)
        return AsyncOpenAI(**kwargs, http_client=http)

    def anthropic_factory(**kwargs):
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        clients.append(http)
        return AsyncAnthropic(**kwargs, http_client=http)

    module, name, factory = {
        "openai_chat": ("openai.chat", "AsyncOpenAI", openai_factory),
        "openai_responses": ("openai.responses", "AsyncOpenAI", openai_factory),
        "anthropic": ("anthropic", "AsyncAnthropic", anthropic_factory),
    }[protocol]
    monkeypatch.setattr(f"my_llm.codec.{module}.transport.{name}", factory)
    return clients


@pytest.mark.parametrize("protocol", ["openai_chat", "openai_responses", "anthropic"])
@pytest.mark.parametrize("streaming", [False, True])
async def test_sdk_protocols_keep_chat_input_and_output(
    monkeypatch, protocol, streaming
):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            text=wire(protocol),
            headers={"content-type": "text/event-stream"},
        )

    clients = install_sdk(monkeypatch, protocol, handler)
    token_paths = {
        "openai_chat": "$.max_completion_tokens",
        "openai_responses": "$.max_output_tokens",
        "anthropic": "$.max_tokens",
    }
    bot = make_profile_bot(
        provider=ProviderProfile(
            protocol=Config(default_value=protocol),
            base_url=Config(default_value="https://local.test"),
        ),
        model=ModelProfile(
            model=Attribute(default_value="test"),
            max_tokens=InputParameter(
                path=token_paths[protocol],
                default_value=100,
            ),
            supports_function_calling=Config(default_value=True),
        ),
        api_key="test",
    )
    messages = [AliraUserMessage(content=[TextContent("hi")])]
    if streaming:
        stream = await bot.stream(messages, tools=[weather])
        async with stream:
            events = [event async for event in stream]
        assert all(isinstance(event, MessageEvent) for event in events)
        result = AliraAssistantMessage.from_middleware(stream.result)
    else:
        result = await bot.invoke(messages, tools=[weather])
    assert result.content[0].text == "hello"
    assert clients and all(not client.is_closed for client in clients)
    await bot.aclose()
    assert all(client.is_closed for client in clients)
    body = json.loads(requests[0].content)
    assert "hi" in str(body)
    assert "weather" in str(body)
    assert "message_uid" not in str(body) and "extensions" not in str(body)


async def test_sdk_tool_round_trip_is_standard_and_executable(monkeypatch):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        delta = (
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "c1",
                        "type": "function",
                        "function": {
                            "name": "weather",
                            "arguments": '{"city":"Tokyo"}',
                        },
                    }
                ]
            }
            if len(calls) == 1
            else {"content": "sunny"}
        )
        event = {
            "id": "c",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "test",
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": "tool_calls" if len(calls) == 1 else "stop",
                }
            ],
        }
        return httpx.Response(
            200,
            text=f"data: {json.dumps(event)}\n\n",
            headers={"content-type": "text/event-stream"},
        )

    clients = install_sdk(monkeypatch, "openai_chat", handler)
    bot = make_profile_bot(
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
    ).bind_tools([weather])
    prompt = AliraUserMessage(content=[TextContent("weather?")])
    first = await bot.invoke([prompt])
    output = await ToolNode(weather).execute({"messages": [first]})
    tool_result = output.update["messages"][0]
    assert tool_result.content[0].text == "sunny in Tokyo"
    assert tool_result.call_id == "c1"
    final = await bot.invoke([prompt, first, tool_result])
    assert final.content[0].text == "sunny"
    assert json.loads(
        calls[1]["messages"][1]["tool_calls"][0]["function"]["arguments"]
    ) == {"city": "Tokyo"}
    assert len(clients) == 1 and not clients[0].is_closed
    await bot.aclose()
    assert all(client.is_closed for client in clients)


async def test_bot_context_closes_shared_clients_on_application_error(monkeypatch):
    clients = install_sdk(
        monkeypatch,
        "openai_chat",
        lambda request: httpx.Response(
            200, text=wire("openai_chat"), headers={"content-type": "text/event-stream"}
        ),
    )
    bot = make_profile_bot(
        provider=ProviderProfile(
            protocol=Config(default_value="openai_chat"),
            api_key=InputParameter(
                path="$.api_key",
                default_value="test",
            ),
        ),
        model=ModelProfile(model=Attribute(default_value="test")),
    )
    with pytest.raises(ValueError, match="application error"):
        async with bot as active:
            assert active is bot
            await bot.invoke([UserMessage(role="user", content=[TextContent("hi")])])
            assert clients and not clients[0].is_closed
            raise ValueError("application error")
    assert all(client.is_closed for client in clients)


async def test_early_exit_and_missing_terminal_close_bodies_and_reuse_client(
    monkeypatch,
):
    event = {
        "id": "c",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "test",
        "choices": [
            {"index": 0, "delta": {"content": "partial"}, "finish_reason": None}
        ],
    }
    bodies = []

    class Body(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield f"data: {json.dumps(event)}\n\n".encode()

        async def aclose(self):
            self.closed = True

    def handler(request):
        body = Body()
        bodies.append(body)
        return httpx.Response(
            200, stream=body, headers={"content-type": "text/event-stream"}
        )

    clients = install_sdk(monkeypatch, "openai_chat", handler)
    bot = make_profile_bot(
        provider=ProviderProfile(
            protocol=Config(default_value="openai_chat"),
            api_key=InputParameter(
                path="$.api_key",
                default_value="test",
            ),
        ),
        model=ModelProfile(model=Attribute(default_value="test")),
    )
    stream = await bot.stream([UserMessage(role="user", content=[TextContent("hi")])])
    async with stream:
        await anext(stream)
    assert bodies[-1].closed
    assert not clients[-1].is_closed
    with pytest.raises(IncompleteStreamError):
        await bot.invoke([UserMessage(role="user", content=[TextContent("hi")])])
    assert all(body.closed for body in bodies)
    assert len(clients) == 1 and not clients[0].is_closed
    await bot.aclose()
    await bot.aclose()  # Library shutdown is idempotent.
    assert clients[0].is_closed
    with pytest.raises(RuntimeError, match="closed"):
        await bot.invoke([UserMessage(role="user", content=[TextContent("hi")])])


async def test_cancelling_one_request_closes_its_body_and_preserves_concurrent_stream(
    monkeypatch,
):
    import asyncio

    release = asyncio.Event()
    ready = asyncio.Barrier(3)
    bodies = {}

    class Body(httpx.AsyncByteStream):
        def __init__(self, label):
            self.label = label
            self.closed = False

        async def __aiter__(self):
            for delta, finish in (({"content": self.label}, None), ({}, "stop")):
                if finish:
                    await release.wait()
                event = {
                    "id": self.label,
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "test",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }
                yield f"data: {json.dumps(event)}\n\n".encode()

        async def aclose(self):
            self.closed = True

    def handler(request):
        label = json.loads(request.content)["messages"][0]["content"]
        body = bodies[label] = Body(label)
        return httpx.Response(
            200, stream=body, headers={"content-type": "text/event-stream"}
        )

    clients = install_sdk(monkeypatch, "openai_chat", handler)
    bot = make_profile_bot(
        provider=ProviderProfile(
            protocol=Config(default_value="openai_chat"),
            api_key=InputParameter(
                path="$.api_key",
                default_value="test",
            ),
        ),
        model=ModelProfile(model=Attribute(default_value="test")),
    )

    async def consume(label):
        stream = await bot.stream(
            [UserMessage(role="user", content=[TextContent(label)])]
        )
        async with stream:
            async for event in stream:
                if event.type == "delta" and event.field == "text":
                    await ready.wait()
        return stream.result

    first = asyncio.create_task(consume("first"))
    second = asyncio.create_task(consume("second"))
    try:
        await asyncio.wait_for(ready.wait(), 3)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert bodies["first"].closed
        assert not bodies["second"].closed
        release.set()
        assert (await asyncio.wait_for(second, 3)).content[0].text == "second"
        assert bodies["second"].closed
        assert len(clients) == 1 and not clients[0].is_closed
    finally:
        for task in (first, second):
            if not task.done():
                task.cancel()
        await asyncio.gather(first, second, return_exceptions=True)
        await bot.aclose()
    assert all(client.is_closed for client in clients)

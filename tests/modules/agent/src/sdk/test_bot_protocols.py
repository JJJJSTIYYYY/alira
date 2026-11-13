"""Actual SDK transport tests through the library; no Alira codec or client injection."""

import json

import httpx
import pytest
from anthropic import AsyncAnthropic
from my_llm import (
    Attribute,
    Config,
    InputParameter,
    MessageEvent,
    ModelProfile,
    ProviderProfile,
    TextContent,
    UserMessage,
)
from openai import AsyncOpenAI

from alira.modules.tool.src import ToolNode
from alira.modules.agent.src.shared.message import AliraAssistantMessage, AliraUserMessage
from tests.modules.agent.src.sdk.test_bot import make_profile_bot, weather


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
    def openai_factory(**kwargs):
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return AsyncOpenAI(**kwargs, http_client=http)

    def anthropic_factory(**kwargs):
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return AsyncAnthropic(**kwargs, http_client=http)

    module, name, factory = {
        "openai_chat": ("openai.chat", "AsyncOpenAI", openai_factory),
        "openai_responses": ("openai.responses", "AsyncOpenAI", openai_factory),
        "anthropic": ("anthropic", "AsyncAnthropic", anthropic_factory),
    }[protocol]
    monkeypatch.setattr(f"my_llm.codec.{module}.transport.{name}", factory)


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

    install_sdk(monkeypatch, protocol, handler)
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
    )
    messages = [AliraUserMessage(content=[TextContent("hi")])]
    if streaming:
        stream = await bot.stream(messages, tools=[weather], api_key="test")
        async with stream:
            events = [event async for event in stream]
        assert all(isinstance(event, MessageEvent) for event in events)
        result = AliraAssistantMessage.from_middleware(stream.result)
    else:
        result = await bot.invoke(messages, tools=[weather], api_key="test")
    assert result.content[0].text == "hello"
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

    install_sdk(monkeypatch, "openai_chat", handler)
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

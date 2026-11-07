"""Verify Alira business data against the actual SDK request serialization."""

import json
from copy import deepcopy

import httpx
import pytest
from my_llm import (
    Attribute,
    Config,
    InputParameter,
    ModelProfile,
    MyLLM,
    ProviderProfile,
    TextContent,
    ToolCall,
)

from alira.utils.message import (
    AliraAssistantMessage,
    AliraSystemMessage,
    AliraToolMessage,
    AliraUserMessage,
)
from tests.agent.sdk.test_bot import weather
from tests.agent.sdk.test_bot_protocols import install_sdk, wire


@pytest.mark.parametrize("protocol", ["openai_chat", "openai_responses", "anthropic"])
@pytest.mark.parametrize("cache", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
async def test_middleware_sdk_requests_exclude_business_fields(
    monkeypatch, protocol, cache, streaming
):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, text=wire(protocol), headers={"content-type": "text/event-stream"}
        )

    install_sdk(monkeypatch, protocol, handler)
    token_path = {
        "openai_chat": "$.max_completion_tokens",
        "openai_responses": "$.max_output_tokens",
        "anthropic": "$.max_tokens",
    }[protocol]
    business_fields = {
        "user_uid": "business-user",
        "conversation_uid": "business-conversation",
        "generation_id": "business-generation",
        "node_id": "business-node",
        "parent_id": "business-parent",
        "metadata": {"private": "business-metadata"},
        "extensions": {"task": {"private": "business-extension"}},
    }
    messages = [
        AliraSystemMessage(content=[TextContent("Use tools.")], **business_fields),
        AliraUserMessage(content=[TextContent("weather?")], **business_fields),
        AliraAssistantMessage(
            content=[ToolCall("call-1", "weather", {"city": "Tokyo"})],
            **business_fields,
        ),
        AliraToolMessage(
            call_id="call-1",
            name="weather",
            content=[TextContent("sunny")],
            **business_fields,
        ),
        AliraUserMessage(content=[TextContent("continue")], **business_fields),
    ]
    before = deepcopy(messages)
    async with MyLLM(
        provider=ProviderProfile(
            protocol=Config(default_value=protocol),
            api_key=InputParameter(path="$.api_key", default_value="test"),
        ),
        model=ModelProfile(
            model=Attribute(default_value="test"),
            max_tokens=InputParameter(path=token_path, default_value=100),
            supports_function_calling=Config(default_value=True),
        ),
        stdio_protocol="middleware",
        enable_message_cache=cache,
    ) as llm:
        # Repeat a batch to exercise warm-cache preparation as well.
        for _ in range(2):
            result = await llm.generate(
                messages=messages, tools=[weather.get_schema()], stream=streaming
            )
            if streaming:
                async with result:
                    async for _ in result:
                        pass
                result = result.result
            assert result.content[0].text == "hello"

    assert messages == before
    assert len(requests) == 2
    for request in requests:
        serialized = json.dumps(request)
        assert "business-" not in serialized
        assert "message_uid" not in serialized
        assert not business_fields.keys() & request.keys()
        assert "call-1" in serialized and "sunny" in serialized
        assert "weather" in serialized and "Tokyo" in serialized

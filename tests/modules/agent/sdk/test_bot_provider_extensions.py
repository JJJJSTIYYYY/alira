"""Provider-specific generation options are delegated to the library."""

from unittest.mock import AsyncMock

import pytest
from my_llm import (
    AssistantMessage,
    Attribute,
    Config,
    InputParameter,
    ModelProfile,
    ProviderProfile,
    TextContent,
    UserMessage,
)

from tests.modules.agent.sdk.test_bot import make_profile_bot


@pytest.mark.asyncio
async def test_provider_options_pass_to_library_without_native_request_translation(
    monkeypatch,
):
    bot = make_profile_bot(
        provider=ProviderProfile(
            protocol=Config(default_value="openai_chat"),
            api_key=InputParameter(
                path="$.api_key",
                default_value="test",
            ),
            reasoning_enable=InputParameter(
                path="$.extra_body.thinking",
                default_value={"type": "enabled"},
            ),
            should_feedback_reasoning_item=Config(default_value="when_tool_calls"),
        ),
        model=ModelProfile(
            model=Attribute(default_value="test"),
            supports_function_calling=Config(default_value=True),
        ),
    )
    generate = AsyncMock(
        return_value=AssistantMessage(role="assistant", content=[TextContent("answer")])
    )
    monkeypatch.setattr(bot.llm, "generate", generate)
    await bot.invoke(
        [UserMessage(role="user", content=[TextContent("hi")])],
        reasoning=True,
        extra_body={"temperature": 0.2},
    )
    assert generate.await_args.kwargs["reasoning"] is True
    assert generate.await_args.kwargs["extra_body"] == {"temperature": 0.2}

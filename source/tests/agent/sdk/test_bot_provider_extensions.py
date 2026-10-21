"""Provider-specific generation options are delegated to the library."""

from unittest.mock import AsyncMock

import pytest
from my_llm import Config, ModelProfile, Parameter, ProviderProfile

from tests.agent.sdk.test_bot import make_profile_bot


@pytest.mark.asyncio
async def test_provider_options_pass_to_library_without_native_request_translation(
    monkeypatch,
):
    bot = make_profile_bot(
        provider=ProviderProfile(
            protocol="openai_chat",
            api_key=Parameter(
                name="api_key", path="$.api_key", default_value="test", phase="decode"
            ),
            reasoning_enable=Parameter(
                name="reasoning_enable",
                path="$.extra_body.thinking",
                default_value={"type": "enabled"},
                phase="decode",
            ),
            replay_reasoning_content_when_tool_calls=Config(
                name="replay_reasoning_content_when_tool_calls", default_value=True
            ),
        ),
        model=ModelProfile(
            model="test",
            supports_function_calling=Config(
                name="supports_function_calling", default_value=True
            ),
        ),
    )
    generate = AsyncMock(return_value={"role": "assistant", "content": "answer"})
    monkeypatch.setattr(bot.llm, "generate", generate)
    await bot.invoke(
        [{"role": "user", "content": "hi"}],
        reasoning=True,
        extra_body={"temperature": 0.2},
    )
    assert generate.await_args.kwargs["reasoning"] is True
    assert generate.await_args.kwargs["extra_body"] == {"temperature": 0.2}

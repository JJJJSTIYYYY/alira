"""Opt-in provider smoke tests; credentials are supplied by the runner."""

import os

import pytest
from my_llm import Attribute, Config, Parameter

from alira.agent.core.bot import (
    AliraBot,
    BaseProvider,
    CustomProvider,
    ModelProfile,
    ProviderProfile,
)
from alira.agent.core.utils.message import AliraUserMessage

pytestmark = pytest.mark.skipif(
    not all(
        os.environ.get(name)
        for name in ("ALIRA_LIVE_PROTOCOL", "ALIRA_LIVE_MODEL", "ALIRA_LIVE_API_KEY")
    ),
    reason="Set ALIRA_LIVE_PROTOCOL, ALIRA_LIVE_MODEL and ALIRA_LIVE_API_KEY to enable paid tests",
)


@pytest.mark.parametrize("streaming", [False, True])
async def test_live_chat(streaming):
    protocol = os.environ["ALIRA_LIVE_PROTOCOL"]
    token_paths = {
        "openai_chat": "$.max_completion_tokens",
        "openai_responses": "$.max_output_tokens",
        "anthropic": "$.max_tokens",
        "gemini": "$.config.max_output_tokens",
        "ollama": "$.options.num_predict",
    }
    endpoint = os.environ.get("ALIRA_LIVE_ENDPOINT")
    BaseProvider.register(
        CustomProvider(
            name="live-test",
            profile=ProviderProfile(
                protocol=Config(default_value=protocol),
                base_url=Config(default_value=endpoint) if endpoint else None,
                models=Attribute(
                    default_value=[
                        ModelProfile(
                            model=Attribute(
                                default_value=os.environ["ALIRA_LIVE_MODEL"]
                            ),
                            max_tokens=Parameter(
                                path=token_paths[protocol],
                                default_value=100,
                            ),
                        )
                    ]
                ),
            ),
        ),
        replace_existing=True,
    )
    bot = AliraBot(
        provider="live-test",
        model=os.environ["ALIRA_LIVE_MODEL"],
        api_key=os.environ["ALIRA_LIVE_API_KEY"],
    )
    async with bot:
        history = [AliraUserMessage(content="Reply with exactly: OK")]
        if streaming:
            stream = await bot.stream(history)
            async with stream:
                async for event in stream:
                    assert "choices" in event
            assert stream.result["content"]
        else:
            assert (await bot.invoke(history)).content

"""Opt-in provider smoke tests; credentials are supplied by the runner."""

import os

import pytest
from my_llm import Attribute, Config, Parameter

from apix.agent.core.bot import (
    ApixBot,
    BaseProvider,
    CustomProvider,
    ModelProfile,
    ProviderProfile,
)
from apix.agent.core.utils.message import ApixUserMessage

pytestmark = pytest.mark.skipif(
    not all(
        os.environ.get(name)
        for name in ("APIX_LIVE_PROTOCOL", "APIX_LIVE_MODEL", "APIX_LIVE_API_KEY")
    ),
    reason="Set APIX_LIVE_PROTOCOL, APIX_LIVE_MODEL and APIX_LIVE_API_KEY to enable paid tests",
)


@pytest.mark.parametrize("streaming", [False, True])
async def test_live_chat(streaming):
    protocol = os.environ["APIX_LIVE_PROTOCOL"]
    token_paths = {
        "openai_chat": "$.max_completion_tokens",
        "openai_responses": "$.max_output_tokens",
        "anthropic": "$.max_tokens",
        "gemini": "$.config.max_output_tokens",
        "ollama": "$.options.num_predict",
    }
    endpoint = os.environ.get("APIX_LIVE_ENDPOINT")
    BaseProvider.register(
        CustomProvider(
            name="live-test",
            api_key_env="APIX_LIVE_API_KEY",
            profile=ProviderProfile(
                protocol=Config(default_value=protocol),
                base_url=Config(default_value=endpoint) if endpoint else None,
                models=Attribute(
                    default_value=[
                        ModelProfile(
                            model=Attribute(
                                default_value=os.environ["APIX_LIVE_MODEL"]
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
    bot = ApixBot(provider="live-test", model=os.environ["APIX_LIVE_MODEL"])
    async with bot:
        history = [ApixUserMessage(content="Reply with exactly: OK")]
        if streaming:
            stream = await bot.stream(history)
            async with stream:
                async for event in stream:
                    assert "choices" in event
            assert stream.result["content"]
        else:
            assert (await bot.invoke(history)).content

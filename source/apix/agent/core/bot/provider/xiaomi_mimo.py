"""MiMo V2.6 Chat presets. Field evidence: docs/provider-presets.md."""

from my_llm import Config, ModelProfile, Parameter, ProviderProfile

from apix.agent.core.bot.provider.base import BaseProvider


class XiaomiMiMOProvider(BaseProvider):
    def __init__(self) -> None:
        super().__init__(
            "mimo",
            ProviderProfile(
                protocol="openai_chat",
                base_url=Config(
                    name="base_url", default_value="https://api.xiaomimimo.com/v1"
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
                models=[
                    ModelProfile(
                        model="mimo-v2.6-pro",
                        max_context_window=1_048_576,
                        supports_reasoning=Config(
                            name="supports_reasoning", default_value=True
                        ),
                        supports_function_calling=Config(
                            name="supports_function_calling", default_value=True
                        ),
                    ),
                    ModelProfile(
                        model="mimo-v2.6-pro-ultraspeed",
                        max_context_window=1_048_576,
                        supports_reasoning=Config(
                            name="supports_reasoning", default_value=True
                        ),
                        supports_function_calling=Config(
                            name="supports_function_calling", default_value=True
                        ),
                    ),
                    ModelProfile(
                        model="mimo-v2.6-flash",
                        max_context_window=1_048_576,
                        supports_reasoning=Config(
                            name="supports_reasoning", default_value=True
                        ),
                        supports_function_calling=Config(
                            name="supports_function_calling", default_value=True
                        ),
                    ),
                ],
            ),
            api_key_env="MIMO_API_KEY",
        )

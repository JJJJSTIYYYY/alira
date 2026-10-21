"""DeepSeek Chat presets. Field evidence: docs/provider-presets.md."""

from my_llm import Config, ModelProfile, Parameter, ProviderProfile, omit

from apix.agent.core.bot.provider.base import BaseProvider


class DeepseekProvider(BaseProvider):
    def __init__(self) -> None:
        super().__init__(
            "deepseek",
            ProviderProfile(
                protocol="openai_chat",
                base_url=Config(
                    name="base_url", default_value="https://api.deepseek.com"
                ),
                max_tokens=Parameter(
                    name="max_tokens",
                    path="$.max_tokens",
                    default_value=omit,
                    phase="decode",
                ),
                supports_reasoning_effort=Config(
                    name="supports_reasoning_effort", default_value=True
                ),
                reasoning_enable=Parameter(
                    name="reasoning_enable",
                    path="$.extra_body.thinking",
                    default_value={"type": "enabled"},
                    phase="decode",
                ),
                reasoning_effort=Parameter(
                    name="reasoning_effort",
                    path="$.reasoning_effort",
                    default_value=omit,
                    phase="decode",
                ),
                reasoning_effort_mapping=Config(
                    name="reasoning_effort_mapping",
                    default_value={
                        "low": "low",
                        "medium": "high",
                        "high": "max",
                        "xhigh": None,
                        "max": None,
                    },
                ),
                replay_reasoning_content_when_tool_calls=Config(
                    name="replay_reasoning_content_when_tool_calls", default_value=True
                ),
                models=[
                    ModelProfile(
                        model="deepseek-flash",
                        max_context_window=1_048_576,
                        supports_reasoning=Config(
                            name="supports_reasoning", default_value=True
                        ),
                        supports_function_calling=Config(
                            name="supports_function_calling", default_value=True
                        ),
                    ),
                    ModelProfile(
                        model="deepseek-v4-pro",
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
            api_key_env="DEEPSEEK_API_KEY",
        )

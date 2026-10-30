"""DeepSeek Chat presets. Field evidence: docs/provider-presets.md."""

from my_llm import Attribute, Config, ModelProfile, Parameter, ProviderProfile, omit

from alira.agent.core.bot.provider.base import BaseProvider


class DeepseekProvider(BaseProvider):
    def __init__(self) -> None:
        super().__init__(
            "deepseek",
            ProviderProfile(
                protocol=Config(default_value="openai_chat"),
                base_url=Config(default_value="https://api.deepseek.com"),
                max_tokens=Parameter(
                    path="$.max_tokens",
                    default_value=omit,
                ),
                reasoning_enable=Parameter(
                    path="$.extra_body.thinking",
                    default_value={"type": "enabled"},
                ),
                reasoning_effort=Parameter(
                    path="$.reasoning_effort",
                    default_value=omit,
                ),
                reasoning_effort_mapping=Config(
                    default_value={
                        "low": "low",
                        "medium": "high",
                        "high": "max",
                        "xhigh": None,
                        "max": None,
                    },
                ),
                replay_reasoning_content_when_tool_calls=Config(default_value=True),
                models=Attribute(
                    default_value=[
                        ModelProfile(
                            model=Attribute(default_value="deepseek-flash"),
                            max_context_window=Attribute(default_value=1_048_576),
                            supports_function_calling=Config(default_value=True),
                        ),
                        ModelProfile(
                            model=Attribute(default_value="deepseek-v4-pro"),
                            max_context_window=Attribute(default_value=1_048_576),
                            supports_function_calling=Config(default_value=True),
                        ),
                    ]
                ),
            ),
            api_key_env="DEEPSEEK_API_KEY",
        )

"""MiMo V2.6 Chat presets. Field evidence: docs/provider-presets.md."""

from my_llm import Attribute, Config, ModelProfile, Parameter, ProviderProfile

from alira.agent.core.bot.provider.base import BaseProvider


class XiaomiMiMOProvider(BaseProvider):
    def __init__(self) -> None:
        super().__init__(
            "mimo",
            ProviderProfile(
                protocol=Config(default_value="openai_chat"),
                base_url=Config(default_value="https://api.xiaomimimo.com/v1"),
                reasoning_enable=Parameter(
                    path="$.extra_body.thinking",
                    default_value={"type": "enabled"},
                ),
                feedback_reasoning_content=Config(default_value="when_tool_calls"),
                models=Attribute(
                    default_value=[
                        ModelProfile(
                            model=Attribute(default_value="mimo-v2.6-pro"),
                            max_context_window=Attribute(default_value=1_048_576),
                            supports_function_calling=Config(default_value=True),
                        ),
                        ModelProfile(
                            model=Attribute(default_value="mimo-v2.6-pro-ultraspeed"),
                            max_context_window=Attribute(default_value=1_048_576),
                            supports_function_calling=Config(default_value=True),
                        ),
                        ModelProfile(
                            model=Attribute(default_value="mimo-v2.6-flash"),
                            max_context_window=Attribute(default_value=1_048_576),
                            supports_function_calling=Config(default_value=True),
                        ),
                    ]
                ),
            ),
            api_key_env="MIMO_API_KEY",
        )

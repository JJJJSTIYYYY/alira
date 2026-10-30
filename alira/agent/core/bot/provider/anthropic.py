"""Claude adaptive-thinking presets. Field evidence: docs/provider-presets.md."""

from my_llm import Attribute, Config, ModelProfile, Parameter, ProviderProfile, omit

from alira.agent.core.bot.provider.base import BaseProvider


class AnthropicProvider(BaseProvider):
    def __init__(self) -> None:
        super().__init__(
            "anthropic",
            ProviderProfile(
                protocol=Config(default_value="anthropic"),
                base_url=Config(default_value="https://api.anthropic.com"),
                usage_mode=Config(default_value="native"),
                reasoning_enable=omit,
                reasoning_effort=Parameter(path="$.output_config.effort"),
                extra_body=Config(default_value={"thinking": {"type": "adaptive"}}),
                models=Attribute(
                    default_value=[
                        ModelProfile(
                            model=Attribute(default_value="claude-fable-5-1"),
                            max_context_window=Attribute(default_value=1_000_000),
                            max_tokens=Parameter(
                                path="$.max_tokens",
                                default_value=4096,
                            ),
                            supports_function_calling=Config(default_value=True),
                        ),
                        ModelProfile(
                            model=Attribute(default_value="claude-opus-5-5"),
                            max_context_window=Attribute(default_value=1_000_000),
                            max_tokens=Parameter(
                                path="$.max_tokens",
                                default_value=4096,
                            ),
                            supports_function_calling=Config(default_value=True),
                        ),
                        ModelProfile(
                            model=Attribute(default_value="claude-sonnet-5-5"),
                            max_context_window=Attribute(default_value=1_000_000),
                            max_tokens=Parameter(
                                path="$.max_tokens",
                                default_value=4096,
                            ),
                            supports_function_calling=Config(default_value=True),
                        ),
                        ModelProfile(
                            model=Attribute(default_value="claude-haiku-5-5"),
                            max_context_window=Attribute(default_value=1_000_000),
                            max_tokens=Parameter(
                                path="$.max_tokens",
                                default_value=4096,
                            ),
                            supports_function_calling=Config(default_value=True),
                        ),
                    ]
                ),
            ),
            api_key_env="ANTHROPIC_API_KEY",
        )

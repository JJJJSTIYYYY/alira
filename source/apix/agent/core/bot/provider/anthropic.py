"""Claude adaptive-thinking presets. Field evidence: docs/provider-presets.md."""

from my_llm import Config, ModelProfile, Parameter, ProviderProfile

from apix.agent.core.bot.provider.base import BaseProvider


class AnthropicProvider(BaseProvider):
    def __init__(self) -> None:
        super().__init__(
            "anthropic",
            ProviderProfile(
                protocol="anthropic",
                base_url=Config(
                    name="base_url", default_value="https://api.anthropic.com"
                ),
                usage_mode=Config(name="usage_mode", default_value="native"),
                supports_reasoning_effort=Config(
                    name="supports_reasoning_effort", default_value=True
                ),
                extra_body=Config(
                    name="extra_body", default_value={"thinking": {"type": "adaptive"}}
                ),
                models=[
                    ModelProfile(
                        model="claude-fable-5-1",
                        max_context_window=1_000_000,
                        max_tokens=Parameter(
                            name="max_tokens",
                            path="$.max_tokens",
                            default_value=4096,
                            phase="decode",
                        ),
                        supports_reasoning=Config(
                            name="supports_reasoning", default_value=True
                        ),
                        supports_function_calling=Config(
                            name="supports_function_calling", default_value=True
                        ),
                    ),
                    ModelProfile(
                        model="claude-opus-5-5",
                        max_context_window=1_000_000,
                        max_tokens=Parameter(
                            name="max_tokens",
                            path="$.max_tokens",
                            default_value=4096,
                            phase="decode",
                        ),
                        supports_reasoning=Config(
                            name="supports_reasoning", default_value=True
                        ),
                        supports_function_calling=Config(
                            name="supports_function_calling", default_value=True
                        ),
                    ),
                    ModelProfile(
                        model="claude-sonnet-5-5",
                        max_context_window=1_000_000,
                        max_tokens=Parameter(
                            name="max_tokens",
                            path="$.max_tokens",
                            default_value=4096,
                            phase="decode",
                        ),
                        supports_reasoning=Config(
                            name="supports_reasoning", default_value=True
                        ),
                        supports_function_calling=Config(
                            name="supports_function_calling", default_value=True
                        ),
                    ),
                    ModelProfile(
                        model="claude-haiku-5-5",
                        max_context_window=1_000_000,
                        max_tokens=Parameter(
                            name="max_tokens",
                            path="$.max_tokens",
                            default_value=4096,
                            phase="decode",
                        ),
                        supports_reasoning=Config(
                            name="supports_reasoning", default_value=True
                        ),
                        supports_function_calling=Config(
                            name="supports_function_calling", default_value=True
                        ),
                    ),
                ],
            ),
            api_key_env="ANTHROPIC_API_KEY",
        )

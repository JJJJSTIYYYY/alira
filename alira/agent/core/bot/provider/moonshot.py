"""Kimi model-specific thinking and token fields. Evidence: docs/provider-presets.md."""

from my_llm import Attribute, Config, ModelProfile, Parameter, ProviderProfile, omit

from alira.agent.core.bot.provider.base import BaseProvider


class MoonShotProvider(BaseProvider):
    def __init__(self) -> None:
        super().__init__(
            "moonshot",
            ProviderProfile(
                protocol=Config(default_value="openai_chat"),
                base_url=Config(default_value="https://api.moonshot.ai/v1"),
                replay_reasoning_content_when_tool_calls=Config(default_value=True),
                models=Attribute(
                    default_value=[
                        ModelProfile(
                            model=Attribute(default_value="kimi-k3"),
                            max_context_window=Attribute(default_value=1_048_576),
                            supports_function_calling=Config(default_value=True),
                            reasoning_enable=omit,
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
                        ),
                        ModelProfile(
                            model=Attribute(default_value="kimi-k2.6"),
                            max_context_window=Attribute(default_value=262_144),
                            max_tokens=Parameter(
                                path="$.max_tokens",
                                default_value=omit,
                            ),
                            supports_function_calling=Config(default_value=True),
                            reasoning_enable=Parameter(
                                path="$.extra_body.thinking",
                                default_value={"type": "enabled", "keep": "all"},
                            ),
                        ),
                        ModelProfile(
                            model=Attribute(default_value="kimi-k2.7-code"),
                            max_context_window=Attribute(default_value=262_144),
                            max_tokens=Parameter(
                                path="$.max_tokens",
                                default_value=omit,
                            ),
                            supports_function_calling=Config(default_value=True),
                            reasoning_enable=Parameter(
                                path="$.extra_body.thinking",
                                default_value={"type": "enabled", "keep": "all"},
                            ),
                        ),
                        ModelProfile(
                            model=Attribute(default_value="kimi-k2.7-code-highspeed"),
                            max_context_window=Attribute(default_value=262_144),
                            max_tokens=Parameter(
                                path="$.max_tokens",
                                default_value=omit,
                            ),
                            supports_function_calling=Config(default_value=True),
                            reasoning_enable=Parameter(
                                path="$.extra_body.thinking",
                                default_value={"type": "enabled", "keep": "all"},
                            ),
                        ),
                    ]
                ),
            ),
            api_key_env="MOONSHOT_API_KEY",
        )

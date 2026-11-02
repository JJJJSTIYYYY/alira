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
                api_key=Parameter(path="$.api_key", default_value=omit, supports=True),
                timeout=Config(default_value=None),
                max_retries=Config(default_value=None),
                supports_stream=Config(default_value=True),
                usage_mode=Config(default_value="native"),
                extra_headers=Config(default_value={}),
                extra_body=Config(default_value={}),
                max_tokens=Parameter(path="$.max_tokens", default_value=omit, supports=True),
                content=Parameter(path="$.delta.text", default_value=omit, supports=True),
                reasoning_content=Parameter(
                    path="$.delta.thinking",
                    default_value=omit,
                    supports=True,
                ),
                reasoning_enable=Parameter(
                    path="$.thinking",
                    default_value={"type": "adaptive"},
                    supports=True,
                ),
                reasoning_effort=Parameter(
                    path="$.output_config.effort",
                    default_value=omit,
                    supports=True,
                ),
                reasoning_effort_mapping=Config(
                    default_value={
                        "none": "none",
                        "low": "low",
                        "medium": "medium",
                        "high": "high",
                        "xhigh": "xhigh",
                        "max": "max",
                    },
                ),
                feedback_reasoning_content=Config(default_value="always"),
                models=Attribute(
                    default_value=[
                        ModelProfile(
                            model=Attribute(default_value="claude-fable-5-1"),
                            max_context_window=Attribute(default_value=1_000_000),
                            supports_function_calling=Config(default_value=True),
                            supports_vision=Config(default_value=False),
                            supports_message_name=Config(default_value=True),
                            extra_headers=None,
                            extra_body=None,
                            max_tokens=Parameter(
                                path="$.max_tokens",
                                default_value=4096,
                                supports=True,
                            ),
                            content=None,
                            reasoning_content=None,
                            reasoning_enable=None,
                            reasoning_effort=None,
                            reasoning_effort_mapping=None,
                            feedback_reasoning_content=None,
                        ),
                        ModelProfile(
                            model=Attribute(default_value="claude-opus-5-5"),
                            max_context_window=Attribute(default_value=1_000_000),
                            supports_function_calling=Config(default_value=True),
                            supports_vision=Config(default_value=False),
                            supports_message_name=Config(default_value=True),
                            extra_headers=None,
                            extra_body=None,
                            max_tokens=Parameter(
                                path="$.max_tokens",
                                default_value=4096,
                                supports=True,
                            ),
                            content=None,
                            reasoning_content=None,
                            reasoning_enable=None,
                            reasoning_effort=None,
                            reasoning_effort_mapping=None,
                            feedback_reasoning_content=None,
                        ),
                        ModelProfile(
                            model=Attribute(default_value="claude-sonnet-5-5"),
                            max_context_window=Attribute(default_value=1_000_000),
                            supports_function_calling=Config(default_value=True),
                            supports_vision=Config(default_value=False),
                            supports_message_name=Config(default_value=True),
                            extra_headers=None,
                            extra_body=None,
                            max_tokens=Parameter(
                                path="$.max_tokens",
                                default_value=4096,
                                supports=True,
                            ),
                            content=None,
                            reasoning_content=None,
                            reasoning_enable=None,
                            reasoning_effort=None,
                            reasoning_effort_mapping=None,
                            feedback_reasoning_content=None,
                        ),
                        ModelProfile(
                            model=Attribute(default_value="claude-haiku-5-5"),
                            max_context_window=Attribute(default_value=1_000_000),
                            supports_function_calling=Config(default_value=True),
                            supports_vision=Config(default_value=False),
                            supports_message_name=Config(default_value=True),
                            extra_headers=None,
                            extra_body=None,
                            max_tokens=Parameter(
                                path="$.max_tokens",
                                default_value=4096,
                                supports=True,
                            ),
                            content=None,
                            reasoning_content=None,
                            reasoning_enable=None,
                            reasoning_effort=None,
                            reasoning_effort_mapping=None,
                            feedback_reasoning_content=None,
                        ),
                    ],
                ),
            ),
        )

"""Claude adaptive-thinking presets. Field evidence: docs/provider-presets.md."""

from my_llm import (
    Attribute,
    Config,
    InputParameter,
    ModelProfile,
    OutputParameter,
    ProviderProfile,
    omit,
)

from alira.modules.agent.src.core.bot.provider.base import BaseProvider


class AnthropicProvider(BaseProvider):
    def __init__(self) -> None:
        super().__init__(
            "anthropic",
            ProviderProfile(
                protocol=None,
                base_url=Config(default_value="https://api.anthropic.com"),
                api_key=InputParameter(path="$.api_key", default_value=omit, supports=True),
                timeout=Config(default_value=None),
                max_retries=Config(default_value=None),
                supports_stream=Config(default_value=True),
                usage_mode=Config(default_value="native"),
                extra_headers=Config(default_value={}),
                extra_body=Config(
                    default_value={"thinking": {"type": "adaptive", "display": "summarized"}}
                ),
                max_tokens=InputParameter(path="$.max_tokens", default_value=omit, supports=True),
                content=OutputParameter(path="$.delta.text", default_value=omit, supports=True),
                reasoning_content=OutputParameter(
                    path="$.delta.thinking",
                    default_value=omit,
                    supports=True,
                ),
                feedback_reasoning_item=OutputParameter(
                    path="$.content_block", default_value=omit, supports=True
                ),
                reasoning_enable=InputParameter(
                    path="$.thinking",
                    default_value={"type": "adaptive", "display": "summarized"},
                    supports=True,
                ),
                reasoning_effort=InputParameter(
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
                should_feedback_reasoning_item=Config(default_value="always"),
                models=Attribute(
                    default_value=[
                        ModelProfile(
                            protocol=Config(default_value="anthropic"),
                            model=Attribute(default_value="claude-fable-5-1"),
                            max_context_window=Attribute(default_value=1_000_000),
                            supports_function_calling=Config(default_value=True),
                            supports_vision=Config(default_value=False),
                            supports_message_name=Config(default_value=True),
                            extra_headers=None,
                            extra_body=None,
                            max_tokens=InputParameter(
                                path="$.max_tokens",
                                default_value=4096,
                                supports=True,
                            ),
                            content=None,
                            reasoning_content=None,
                            feedback_reasoning_item=None,
                            reasoning_enable=None,
                            reasoning_effort=None,
                            reasoning_effort_mapping=None,
                            should_feedback_reasoning_item=None,
                        ),
                        ModelProfile(
                            protocol=Config(default_value="anthropic"),
                            model=Attribute(default_value="claude-opus-5-5"),
                            max_context_window=Attribute(default_value=1_000_000),
                            supports_function_calling=Config(default_value=True),
                            supports_vision=Config(default_value=False),
                            supports_message_name=Config(default_value=True),
                            extra_headers=None,
                            extra_body=None,
                            max_tokens=InputParameter(
                                path="$.max_tokens",
                                default_value=4096,
                                supports=True,
                            ),
                            content=None,
                            reasoning_content=None,
                            feedback_reasoning_item=None,
                            reasoning_enable=None,
                            reasoning_effort=None,
                            reasoning_effort_mapping=None,
                            should_feedback_reasoning_item=None,
                        ),
                        ModelProfile(
                            protocol=Config(default_value="anthropic"),
                            model=Attribute(default_value="claude-sonnet-5-5"),
                            max_context_window=Attribute(default_value=1_000_000),
                            supports_function_calling=Config(default_value=True),
                            supports_vision=Config(default_value=False),
                            supports_message_name=Config(default_value=True),
                            extra_headers=None,
                            extra_body=None,
                            max_tokens=InputParameter(
                                path="$.max_tokens",
                                default_value=4096,
                                supports=True,
                            ),
                            content=None,
                            reasoning_content=None,
                            feedback_reasoning_item=None,
                            reasoning_enable=None,
                            reasoning_effort=None,
                            reasoning_effort_mapping=None,
                            should_feedback_reasoning_item=None,
                        ),
                        ModelProfile(
                            protocol=Config(default_value="anthropic"),
                            model=Attribute(default_value="claude-haiku-5-5"),
                            max_context_window=Attribute(default_value=1_000_000),
                            supports_function_calling=Config(default_value=True),
                            supports_vision=Config(default_value=False),
                            supports_message_name=Config(default_value=True),
                            extra_headers=None,
                            extra_body=None,
                            max_tokens=InputParameter(
                                path="$.max_tokens",
                                default_value=4096,
                                supports=True,
                            ),
                            content=None,
                            reasoning_content=None,
                            feedback_reasoning_item=None,
                            reasoning_enable=None,
                            reasoning_effort=None,
                            reasoning_effort_mapping=None,
                            should_feedback_reasoning_item=None,
                        ),
                    ],
                ),
            ),
        )

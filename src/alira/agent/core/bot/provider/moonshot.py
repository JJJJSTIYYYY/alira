"""Kimi Responses and model-specific Chat presets. Evidence: docs/provider-presets.md."""

from my_llm import (
    Attribute,
    Config,
    InputParameter,
    ModelProfile,
    OutputParameter,
    ProviderProfile,
    omit,
)

from alira.agent.core.bot.provider.base import BaseProvider


class MoonShotProvider(BaseProvider):
    def __init__(self) -> None:
        super().__init__(
            "moonshot",
            ProviderProfile(
                protocol=None,
                base_url=Config(default_value="https://api.moonshot.ai/v1"),
                api_key=InputParameter(path="$.api_key", default_value=omit, supports=True),
                timeout=Config(default_value=None),
                max_retries=Config(default_value=None),
                supports_stream=Config(default_value=True),
                usage_mode=Config(default_value="none"),
                extra_headers=Config(default_value={}),
                extra_body=Config(default_value={}),
                max_tokens=InputParameter(
                    path="$.max_output_tokens",
                    default_value=omit,
                    supports=True,
                ),
                content=OutputParameter(
                    path="$.delta",
                    default_value=omit,
                    supports=True,
                ),
                reasoning_content=OutputParameter(
                    path="$.delta",
                    default_value=omit,
                    supports=True,
                ),
                feedback_reasoning_item=OutputParameter(
                    path="$.item", default_value=omit, supports=True
                ),
                reasoning_enable=omit,
                reasoning_effort=InputParameter(
                    path="$.reasoning.effort",
                    default_value=omit,
                    supports=True,
                ),
                reasoning_effort_mapping=Config(
                    default_value={
                        "none": "none",
                        "low": "low",
                        "medium": "high",
                        "high": "max",
                        "xhigh": None,
                        "max": None,
                    },
                ),
                should_feedback_reasoning_item=Config(default_value="always"),
                models=Attribute(
                    default_value=[
                        ModelProfile(
                            model=Attribute(default_value="kimi-k3.1-pro"),
                            protocol=Config(default_value="openai_responses"),
                            max_context_window=Attribute(default_value=1_048_576),
                            supports_function_calling=Config(default_value=True),
                            supports_vision=Config(default_value=False),
                            supports_message_name=Config(default_value=True),
                            extra_headers=None,
                            extra_body=Config(
                                default_value={"reasoning": {"effort": "max"}},
                            ),
                            max_tokens=None,
                            content=None,
                            reasoning_content=None,
                            feedback_reasoning_item=None,
                            reasoning_enable=None,
                            reasoning_effort=None,
                            reasoning_effort_mapping=None,
                            should_feedback_reasoning_item=None,
                        ),
                        ModelProfile(
                            model=Attribute(default_value="kimi-k3.1-pro-highspeed"),
                            protocol=Config(default_value="openai_responses"),
                            max_context_window=Attribute(default_value=1_048_576),
                            supports_function_calling=Config(default_value=True),
                            supports_vision=Config(default_value=False),
                            supports_message_name=Config(default_value=True),
                            extra_headers=None,
                            extra_body=Config(
                                default_value={"reasoning": {"effort": "max"}},
                            ),
                            max_tokens=None,
                            content=None,
                            reasoning_content=None,
                            feedback_reasoning_item=None,
                            reasoning_enable=None,
                            reasoning_effort=None,
                            reasoning_effort_mapping=None,
                            should_feedback_reasoning_item=None,
                        ),
                        ModelProfile(
                            model=Attribute(default_value="kimi-k3"),
                            protocol=Config(default_value="openai_responses"),
                            max_context_window=Attribute(default_value=1_048_576),
                            supports_function_calling=Config(default_value=True),
                            supports_vision=Config(default_value=False),
                            supports_message_name=Config(default_value=True),
                            extra_headers=None,
                            extra_body=Config(
                                default_value={"reasoning": {"effort": "max"}},
                            ),
                            max_tokens=None,
                            content=None,
                            reasoning_content=None,
                            feedback_reasoning_item=None,
                            reasoning_enable=None,
                            reasoning_effort=None,
                            reasoning_effort_mapping=None,
                            should_feedback_reasoning_item=None,
                        ),
                        ModelProfile(
                            model=Attribute(default_value="kimi-k2.6"),
                            protocol=Config(default_value="openai_chat"),
                            max_context_window=Attribute(default_value=262_144),
                            supports_function_calling=Config(default_value=True),
                            supports_vision=Config(default_value=False),
                            supports_message_name=Config(default_value=True),
                            extra_headers=None,
                            extra_body=Config(
                                default_value={
                                    "thinking": {"type": "enabled", "keep": "all"},
                                },
                            ),
                            max_tokens=InputParameter(
                                path="$.max_tokens",
                                default_value=omit,
                                supports=True,
                            ),
                            content=OutputParameter(
                                path="$.choices[0].delta.content",
                                default_value=omit,
                                supports=True,
                            ),
                            reasoning_content=OutputParameter(
                                path="$.choices[0].delta.reasoning_content",
                                default_value=omit,
                                supports=True,
                            ),
                            feedback_reasoning_item=OutputParameter(
                                path="$.choices[0].delta.reasoning_content", default_value=omit, supports=True
                            ),
                            reasoning_enable=InputParameter(
                                path="$.extra_body.thinking",
                                default_value={"type": "enabled", "keep": "all"},
                                supports=True,
                            ),
                            reasoning_effort=InputParameter(
                                path="$.reasoning_effort",
                                default_value=omit,
                                supports=False,
                            ),
                            reasoning_effort_mapping=omit,
                            should_feedback_reasoning_item=None,
                        ),
                        ModelProfile(
                            model=Attribute(default_value="kimi-k2.7-code"),
                            protocol=Config(default_value="openai_chat"),
                            max_context_window=Attribute(default_value=262_144),
                            supports_function_calling=Config(default_value=True),
                            supports_vision=Config(default_value=False),
                            supports_message_name=Config(default_value=True),
                            extra_headers=None,
                            extra_body=Config(
                                default_value={
                                    "thinking": {"type": "enabled", "keep": "all"},
                                },
                            ),
                            max_tokens=InputParameter(
                                path="$.max_tokens",
                                default_value=omit,
                                supports=True,
                            ),
                            content=OutputParameter(
                                path="$.choices[0].delta.content",
                                default_value=omit,
                                supports=True,
                            ),
                            reasoning_content=OutputParameter(
                                path="$.choices[0].delta.reasoning_content",
                                default_value=omit,
                                supports=True,
                            ),
                            feedback_reasoning_item=OutputParameter(
                                path="$.choices[0].delta.reasoning_content", default_value=omit, supports=True
                            ),
                            reasoning_enable=InputParameter(
                                path="$.extra_body.thinking",
                                default_value={"type": "enabled", "keep": "all"},
                                supports=True,
                            ),
                            reasoning_effort=InputParameter(
                                path="$.reasoning_effort",
                                default_value=omit,
                                supports=False,
                            ),
                            reasoning_effort_mapping=omit,
                            should_feedback_reasoning_item=None,
                        ),
                        ModelProfile(
                            model=Attribute(default_value="kimi-k2.7-code-highspeed"),
                            protocol=Config(default_value="openai_chat"),
                            max_context_window=Attribute(default_value=262_144),
                            supports_function_calling=Config(default_value=True),
                            supports_vision=Config(default_value=False),
                            supports_message_name=Config(default_value=True),
                            extra_headers=None,
                            extra_body=Config(
                                default_value={
                                    "thinking": {"type": "enabled", "keep": "all"},
                                },
                            ),
                            max_tokens=InputParameter(
                                path="$.max_tokens",
                                default_value=omit,
                                supports=True,
                            ),
                            content=OutputParameter(
                                path="$.choices[0].delta.content",
                                default_value=omit,
                                supports=True,
                            ),
                            reasoning_content=OutputParameter(
                                path="$.choices[0].delta.reasoning_content",
                                default_value=omit,
                                supports=True,
                            ),
                            feedback_reasoning_item=OutputParameter(
                                path="$.choices[0].delta.reasoning_content", default_value=omit, supports=True
                            ),
                            reasoning_enable=InputParameter(
                                path="$.extra_body.thinking",
                                default_value={"type": "enabled", "keep": "all"},
                                supports=True,
                            ),
                            reasoning_effort=InputParameter(
                                path="$.reasoning_effort",
                                default_value=omit,
                                supports=False,
                            ),
                            reasoning_effort_mapping=omit,
                            should_feedback_reasoning_item=None,
                        ),
                    ],
                ),
            ),
        )

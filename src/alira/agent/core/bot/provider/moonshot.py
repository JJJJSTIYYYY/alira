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
                api_key=Parameter(path="$.api_key", default_value=omit, supports=True),
                timeout=Config(default_value=None),
                max_retries=Config(default_value=None),
                supports_stream=Config(default_value=True),
                usage_mode=Config(default_value="none"),
                extra_headers=Config(default_value={}),
                extra_body=Config(default_value={}),
                max_tokens=Parameter(
                    path="$.max_completion_tokens",
                    default_value=omit,
                    supports=True,
                ),
                content=Parameter(
                    path="$.choices[0].delta.content",
                    default_value=omit,
                    supports=True,
                ),
                reasoning_content=Parameter(
                    path="$.choices[0].delta.reasoning_content",
                    default_value=omit,
                    supports=True,
                ),
                reasoning_enable=Parameter(
                    path="$.reasoning_effort",
                    default_value="medium",
                    supports=True,
                ),
                reasoning_effort=Parameter(
                    path="$.reasoning_effort",
                    default_value=omit,
                    supports=False,
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
                            model=Attribute(default_value="kimi-k3"),
                            max_context_window=Attribute(default_value=1_048_576),
                            supports_function_calling=Config(default_value=True),
                            supports_vision=Config(default_value=False),
                            supports_message_name=Config(default_value=True),
                            extra_headers=None,
                            extra_body=None,
                            max_tokens=None,
                            content=None,
                            reasoning_content=None,
                            reasoning_enable=omit,
                            reasoning_effort=Parameter(
                                path="$.reasoning_effort",
                                default_value=omit,
                                supports=True,
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
                            feedback_reasoning_content=None,
                        ),
                        ModelProfile(
                            model=Attribute(default_value="kimi-k2.6"),
                            max_context_window=Attribute(default_value=262_144),
                            supports_function_calling=Config(default_value=True),
                            supports_vision=Config(default_value=False),
                            supports_message_name=Config(default_value=True),
                            extra_headers=None,
                            extra_body=None,
                            max_tokens=Parameter(
                                path="$.max_tokens",
                                default_value=omit,
                                supports=True,
                            ),
                            content=None,
                            reasoning_content=None,
                            reasoning_enable=Parameter(
                                path="$.extra_body.thinking",
                                default_value={"type": "enabled", "keep": "all"},
                                supports=True,
                            ),
                            reasoning_effort=None,
                            reasoning_effort_mapping=None,
                            feedback_reasoning_content=None,
                        ),
                        ModelProfile(
                            model=Attribute(default_value="kimi-k2.7-code"),
                            max_context_window=Attribute(default_value=262_144),
                            supports_function_calling=Config(default_value=True),
                            supports_vision=Config(default_value=False),
                            supports_message_name=Config(default_value=True),
                            extra_headers=None,
                            extra_body=None,
                            max_tokens=Parameter(
                                path="$.max_tokens",
                                default_value=omit,
                                supports=True,
                            ),
                            content=None,
                            reasoning_content=None,
                            reasoning_enable=Parameter(
                                path="$.extra_body.thinking",
                                default_value={"type": "enabled", "keep": "all"},
                                supports=True,
                            ),
                            reasoning_effort=None,
                            reasoning_effort_mapping=None,
                            feedback_reasoning_content=None,
                        ),
                        ModelProfile(
                            model=Attribute(default_value="kimi-k2.7-code-highspeed"),
                            max_context_window=Attribute(default_value=262_144),
                            supports_function_calling=Config(default_value=True),
                            supports_vision=Config(default_value=False),
                            supports_message_name=Config(default_value=True),
                            extra_headers=None,
                            extra_body=None,
                            max_tokens=Parameter(
                                path="$.max_tokens",
                                default_value=omit,
                                supports=True,
                            ),
                            content=None,
                            reasoning_content=None,
                            reasoning_enable=Parameter(
                                path="$.extra_body.thinking",
                                default_value={"type": "enabled", "keep": "all"},
                                supports=True,
                            ),
                            reasoning_effort=None,
                            reasoning_effort_mapping=None,
                            feedback_reasoning_content=None,
                        ),
                    ],
                ),
            ),
        )

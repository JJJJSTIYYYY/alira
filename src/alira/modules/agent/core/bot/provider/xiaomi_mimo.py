"""MiMo V2.6 Responses presets. Field evidence: docs/provider-presets.md."""

from my_llm import (
    Attribute,
    Config,
    InputParameter,
    ModelProfile,
    OutputParameter,
    ProviderProfile,
    omit,
)

from alira.modules.agent.core.bot.provider.base import BaseProvider


class XiaomiMiMOProvider(BaseProvider):
    def __init__(self) -> None:
        super().__init__(
            "mimo",
            ProviderProfile(
                protocol=None,
                base_url=Config(default_value="https://api.xiaomimimo.com/v1"),
                api_key=InputParameter(path="$.api_key", default_value=omit, supports=True),
                timeout=Config(default_value=None),
                max_retries=Config(default_value=None),
                supports_stream=Config(default_value=True),
                usage_mode=Config(default_value="none"),
                extra_headers=Config(default_value={}),
                extra_body=Config(default_value={"reasoning": {"effort": "medium"}}),
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
                reasoning_enable=InputParameter(
                    path="$.reasoning.effort",
                    default_value="medium",
                    supports=True,
                ),
                reasoning_effort=InputParameter(
                    path="$.reasoning.effort",
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
                should_feedback_reasoning_item=Config(default_value="when_tool_calls"),
                models=Attribute(
                    default_value=[
                        ModelProfile(
                            model=Attribute(default_value="mimo-v2.6-pro"),
                            protocol=Config(default_value="openai_responses"),
                            max_context_window=Attribute(default_value=1_048_576),
                            supports_function_calling=Config(default_value=True),
                            supports_vision=Config(default_value=False),
                            supports_message_name=Config(default_value=True),
                            extra_headers=None,
                            extra_body=None,
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
                            model=Attribute(default_value="mimo-v2.6-pro-ultraspeed"),
                            protocol=Config(default_value="openai_responses"),
                            max_context_window=Attribute(default_value=1_048_576),
                            supports_function_calling=Config(default_value=True),
                            supports_vision=Config(default_value=False),
                            supports_message_name=Config(default_value=True),
                            extra_headers=None,
                            extra_body=None,
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
                            model=Attribute(default_value="mimo-v2.6-flash"),
                            protocol=Config(default_value="openai_responses"),
                            max_context_window=Attribute(default_value=1_048_576),
                            supports_function_calling=Config(default_value=True),
                            supports_vision=Config(default_value=False),
                            supports_message_name=Config(default_value=True),
                            extra_headers=None,
                            extra_body=None,
                            max_tokens=None,
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

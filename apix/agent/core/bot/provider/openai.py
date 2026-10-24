"""OpenAI Responses presets. Field evidence: docs/provider-presets.md."""

from my_llm import Attribute, Config, ModelProfile, ProviderProfile

from apix.agent.core.bot.provider.base import BaseProvider


class OpenAIPorvider(BaseProvider):
    """OpenAI provider (class name follows the public API requested by Apix)."""

    def __init__(self) -> None:
        super().__init__(
            "openai",
            ProviderProfile(
                protocol=Config(default_value="openai_responses"),
                base_url=Config(default_value="https://api.openai.com/v1"),
                usage_mode=Config(default_value="native"),
                models=Attribute(
                    default_value=[
                        ModelProfile(
                            model=Attribute(default_value="gpt-6-astra"),
                            max_context_window=Attribute(default_value=1_050_000),
                            supports_function_calling=Config(default_value=True),
                            reasoning_effort_mapping=Config(
                                default_value={"none": None},
                            ),
                        ),
                        ModelProfile(
                            model=Attribute(default_value="gpt-6.1-sol"),
                            max_context_window=Attribute(default_value=1_050_000),
                            supports_function_calling=Config(default_value=True),
                            reasoning_effort_mapping=Config(
                                default_value={"none": None},
                            ),
                        ),
                        ModelProfile(
                            model=Attribute(default_value="gpt-6-sol"),
                            max_context_window=Attribute(default_value=1_050_000),
                            supports_function_calling=Config(default_value=True),
                        ),
                        ModelProfile(
                            model=Attribute(default_value="gpt-6-luna"),
                            max_context_window=Attribute(default_value=1_050_000),
                            supports_function_calling=Config(default_value=True),
                        ),
                        ModelProfile(
                            model=Attribute(default_value="gpt-5.4"),
                            max_context_window=Attribute(default_value=1_050_000),
                            supports_function_calling=Config(default_value=True),
                            reasoning_effort_mapping=Config(
                                default_value={"max": None}
                            ),
                        ),
                        ModelProfile(
                            model=Attribute(default_value="gpt-5.4-pro"),
                            max_context_window=Attribute(default_value=1_050_000),
                            supports_function_calling=Config(default_value=True),
                            reasoning_effort_mapping=Config(
                                default_value={
                                    "none": None,
                                    "low": "medium",
                                    "medium": "high",
                                    "high": "xhigh",
                                    "xhigh": None,
                                    "max": None,
                                },
                            ),
                        ),
                        ModelProfile(
                            model=Attribute(default_value="gpt-5.4-mini"),
                            max_context_window=Attribute(default_value=400_000),
                            supports_function_calling=Config(default_value=True),
                            reasoning_effort_mapping=Config(
                                default_value={"max": None}
                            ),
                        ),
                    ]
                ),
            ),
            api_key_env="OPENAI_API_KEY",
        )

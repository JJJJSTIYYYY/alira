"""OpenAI Responses presets. Field evidence: docs/provider-presets.md."""

from my_llm import Config, ModelProfile, ProviderProfile

from apix.agent.core.bot.provider.base import BaseProvider


class OpenAIPorvider(BaseProvider):
    """OpenAI provider (class name follows the public API requested by Apix)."""

    def __init__(self) -> None:
        super().__init__(
            "openai",
            ProviderProfile(
                protocol="openai_responses",
                base_url=Config(
                    name="base_url", default_value="https://api.openai.com/v1"
                ),
                usage_mode=Config(name="usage_mode", default_value="native"),
                models=[
                    ModelProfile(
                        model="gpt-6-astra",
                        max_context_window=1_050_000,
                        supports_reasoning=Config(
                            name="supports_reasoning", default_value=True
                        ),
                        supports_function_calling=Config(
                            name="supports_function_calling", default_value=True
                        ),
                        reasoning_effort_mapping=Config(
                            name="reasoning_effort_mapping",
                            default_value={"none": None},
                        ),
                    ),
                    ModelProfile(
                        model="gpt-6.1-sol",
                        max_context_window=1_050_000,
                        supports_reasoning=Config(
                            name="supports_reasoning", default_value=True
                        ),
                        supports_function_calling=Config(
                            name="supports_function_calling", default_value=True
                        ),
                        reasoning_effort_mapping=Config(
                            name="reasoning_effort_mapping",
                            default_value={"none": None},
                        ),
                    ),
                    ModelProfile(
                        model="gpt-6-sol",
                        max_context_window=1_050_000,
                        supports_reasoning=Config(
                            name="supports_reasoning", default_value=True
                        ),
                        supports_function_calling=Config(
                            name="supports_function_calling", default_value=True
                        ),
                    ),
                    ModelProfile(
                        model="gpt-6-luna",
                        max_context_window=1_050_000,
                        supports_reasoning=Config(
                            name="supports_reasoning", default_value=True
                        ),
                        supports_function_calling=Config(
                            name="supports_function_calling", default_value=True
                        ),
                    ),
                    ModelProfile(
                        model="gpt-5.4",
                        max_context_window=1_050_000,
                        supports_reasoning=Config(
                            name="supports_reasoning", default_value=True
                        ),
                        supports_function_calling=Config(
                            name="supports_function_calling", default_value=True
                        ),
                        reasoning_effort_mapping=Config(
                            name="reasoning_effort_mapping", default_value={"max": None}
                        ),
                    ),
                    ModelProfile(
                        model="gpt-5.4-pro",
                        max_context_window=1_050_000,
                        supports_reasoning=Config(
                            name="supports_reasoning", default_value=True
                        ),
                        supports_function_calling=Config(
                            name="supports_function_calling", default_value=True
                        ),
                        reasoning_effort_mapping=Config(
                            name="reasoning_effort_mapping",
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
                        model="gpt-5.4-mini",
                        max_context_window=400_000,
                        supports_reasoning=Config(
                            name="supports_reasoning", default_value=True
                        ),
                        supports_function_calling=Config(
                            name="supports_function_calling", default_value=True
                        ),
                        reasoning_effort_mapping=Config(
                            name="reasoning_effort_mapping", default_value={"max": None}
                        ),
                    ),
                ],
            ),
            api_key_env="OPENAI_API_KEY",
        )

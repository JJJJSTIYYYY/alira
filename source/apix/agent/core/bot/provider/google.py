"""Gemini GenerateContent presets. Field evidence: docs/provider-presets.md."""

from my_llm import Config, ModelProfile, ProviderProfile

from apix.agent.core.bot.provider.base import BaseProvider


class GoogleProvider(BaseProvider):
    def __init__(self) -> None:
        super().__init__(
            "google",
            ProviderProfile(
                protocol="gemini",
                usage_mode=Config(name="usage_mode", default_value="native"),
                supports_reasoning_effort=Config(
                    name="supports_reasoning_effort", default_value=True
                ),
                reasoning_effort_mapping=Config(
                    name="reasoning_effort_mapping",
                    default_value={
                        "low": "low",
                        "medium": "medium",
                        "high": "high",
                        "xhigh": None,
                        "max": None,
                    },
                ),
                models=[
                    ModelProfile(
                        model="gemini-3.8-flash",
                        max_context_window=1_048_576,
                        supports_reasoning=Config(
                            name="supports_reasoning", default_value=True
                        ),
                        supports_function_calling=Config(
                            name="supports_function_calling", default_value=True
                        ),
                    ),
                    ModelProfile(
                        model="gemini-3.6-flash",
                        max_context_window=1_048_576,
                        supports_reasoning=Config(
                            name="supports_reasoning", default_value=True
                        ),
                        supports_function_calling=Config(
                            name="supports_function_calling", default_value=True
                        ),
                    ),
                    ModelProfile(
                        model="gemini-3.5-flash-lite",
                        max_context_window=1_048_576,
                        supports_reasoning=Config(
                            name="supports_reasoning", default_value=True
                        ),
                        supports_function_calling=Config(
                            name="supports_function_calling", default_value=True
                        ),
                    ),
                    ModelProfile(
                        model="gemini-3.1-flash-lite",
                        max_context_window=1_048_576,
                        supports_reasoning=Config(
                            name="supports_reasoning", default_value=True
                        ),
                        supports_function_calling=Config(
                            name="supports_function_calling", default_value=True
                        ),
                    ),
                    ModelProfile(
                        model="gemini-3.1-pro-preview",
                        max_context_window=1_048_576,
                        supports_reasoning=Config(
                            name="supports_reasoning", default_value=True
                        ),
                        supports_function_calling=Config(
                            name="supports_function_calling", default_value=True
                        ),
                    ),
                    ModelProfile(
                        model="gemini-3.1-pro-preview-customtools",
                        max_context_window=1_048_576,
                        supports_reasoning=Config(
                            name="supports_reasoning", default_value=True
                        ),
                        supports_function_calling=Config(
                            name="supports_function_calling", default_value=True
                        ),
                    ),
                    ModelProfile(
                        model="gemini-3-flash-preview",
                        max_context_window=1_048_576,
                        supports_reasoning=Config(
                            name="supports_reasoning", default_value=True
                        ),
                        supports_function_calling=Config(
                            name="supports_function_calling", default_value=True
                        ),
                    ),
                ],
            ),
            api_key_env="GEMINI_API_KEY",
        )

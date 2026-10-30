"""Gemini GenerateContent presets. Field evidence: docs/provider-presets.md."""

from my_llm import Attribute, Config, ModelProfile, Parameter, ProviderProfile

from alira.agent.core.bot.provider.base import BaseProvider


class GoogleProvider(BaseProvider):
    def __init__(self) -> None:
        super().__init__(
            "google",
            ProviderProfile(
                protocol=Config(default_value="gemini"),
                usage_mode=Config(default_value="native"),
                reasoning_effort=Parameter(
                    path="$.config.thinking_config.thinking_level"
                ),
                reasoning_effort_mapping=Config(
                    default_value={
                        "low": "low",
                        "medium": "medium",
                        "high": "high",
                        "xhigh": None,
                        "max": None,
                    },
                ),
                models=Attribute(
                    default_value=[
                        ModelProfile(
                            model=Attribute(default_value="gemini-3.8-flash"),
                            max_context_window=Attribute(default_value=1_048_576),
                            supports_function_calling=Config(default_value=True),
                        ),
                        ModelProfile(
                            model=Attribute(default_value="gemini-3.6-flash"),
                            max_context_window=Attribute(default_value=1_048_576),
                            supports_function_calling=Config(default_value=True),
                        ),
                        ModelProfile(
                            model=Attribute(default_value="gemini-3.5-flash-lite"),
                            max_context_window=Attribute(default_value=1_048_576),
                            supports_function_calling=Config(default_value=True),
                        ),
                        ModelProfile(
                            model=Attribute(default_value="gemini-3.1-flash-lite"),
                            max_context_window=Attribute(default_value=1_048_576),
                            supports_function_calling=Config(default_value=True),
                        ),
                        ModelProfile(
                            model=Attribute(default_value="gemini-3.1-pro-preview"),
                            max_context_window=Attribute(default_value=1_048_576),
                            supports_function_calling=Config(default_value=True),
                        ),
                        ModelProfile(
                            model=Attribute(
                                default_value="gemini-3.1-pro-preview-customtools"
                            ),
                            max_context_window=Attribute(default_value=1_048_576),
                            supports_function_calling=Config(default_value=True),
                        ),
                        ModelProfile(
                            model=Attribute(default_value="gemini-3-flash-preview"),
                            max_context_window=Attribute(default_value=1_048_576),
                            supports_function_calling=Config(default_value=True),
                        ),
                    ]
                ),
            ),
            api_key_env="GEMINI_API_KEY",
        )

"""Named provider catalogues backed by my-llm-py profiles."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

from my_llm import (
    ModelProfile,
    MyLLM,
    ProviderProfile,
    resolve_profiles,
)
from my_llm.codec.openai.chat.prebuilt import chat_semantic_loss_hook


class BaseProvider:
    """Manage named catalogues; leave profile resolution and inference to MyLLM.

    Registry reads and writes copy configuration. Modify ``profile`` with the
    library's add_models/delete_model methods, then register the changed copy.
    """

    def __init__(
        self,
        name: str,
        profile: ProviderProfile,
    ) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("provider name must be a non-empty string")
        if not isinstance(profile, ProviderProfile):
            raise TypeError("profile must be a my_llm.ProviderProfile")
        self.name = name.strip().lower()
        self.profile = deepcopy(profile)

    @classmethod
    def get(cls, name: str) -> BaseProvider:
        from alira.modules.agent.core.bot.provider.registry import PROVIDERS

        if not isinstance(name, str):
            raise TypeError("provider must be a string")
        try:
            return deepcopy(PROVIDERS[name.strip().lower()])
        except KeyError:
            raise ValueError(
                f"unknown provider {name!r}; choose from {cls.list_providers()}"
            ) from None

    @classmethod
    def register(
        cls, provider: BaseProvider, *, replace_existing: bool = False
    ) -> None:
        from alira.modules.agent.core.bot.provider.registry import PROVIDERS

        if not isinstance(provider, BaseProvider):
            raise TypeError("provider must be a BaseProvider")
        if provider.name in PROVIDERS and not replace_existing:
            raise ValueError(f"provider {provider.name!r} is already registered")
        PROVIDERS[provider.name] = deepcopy(provider)

    @classmethod
    def list_providers(cls) -> list[str]:
        from alira.modules.agent.core.bot.provider.registry import PROVIDERS

        return list(PROVIDERS)

    @classmethod
    def unregister(cls, name: str) -> None:
        from alira.modules.agent.core.bot.provider.registry import PROVIDERS

        del PROVIDERS[name]

    def list_models(self) -> list[ModelProfile]:
        return self.profile.list_models()

    def get_profile(self) -> ProviderProfile:
        """Return an provider configuration without copying, including its models."""
        return self.profile

    def get_model(self, model: str) -> ModelProfile:
        if not isinstance(model, str):
            raise TypeError("model must be a string")
        for preset in self.profile.list_models():
            if preset.model.default_value == model:
                return preset
        available = [
            preset.model.default_value for preset in self.profile.list_models()
        ]
        raise ValueError(
            f"unknown model {model!r} for {self.name!r}; choose from {available}"
        )

    def create_llm(self, model: str, *, api_key: str | None = None) -> MyLLM:
        selected = self.get_model(model)
        profile = self.get_profile()
        if api_key is not None:
            binding = resolve_profiles(profile, selected).parameter(
                "api_key", source="input"
            )
            if binding is not None:
                profile = replace(
                    profile, api_key=replace(binding, default_value=api_key)
                )
        llm = MyLLM(
            provider=profile,
            model=selected,
            stdio_protocol="middleware",
            semantic_loss_protocol="ignore",
        )
        if llm.resolved_profile.protocol == "openai_chat":
            llm.set_semantic_loss_policy(
                semantic_loss_protocol="hook",
                input_semantic_loss_hook=chat_semantic_loss_hook,
            )
        return llm

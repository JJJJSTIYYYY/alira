"""Named provider catalogues backed by my-llm-py profiles."""

from __future__ import annotations

import os
from copy import deepcopy
from dataclasses import replace

from my_llm import (
    ConfigurationError,
    ModelProfile,
    MyLLM,
    ProviderProfile,
    omit,
    resolve_profiles,
)


class BaseProvider:
    """Manage named catalogues; leave profile resolution and inference to MyLLM.

    Registry reads and writes copy configuration. Modify ``profile`` with the
    library's add_models/delete_model methods, then register the changed copy.
    """

    def __init__(
        self,
        name: str,
        profile: ProviderProfile,
        *,
        api_key_env: str | None = None,
    ) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("provider name must be a non-empty string")
        if not isinstance(profile, ProviderProfile):
            raise TypeError("profile must be a my_llm.ProviderProfile")
        self.name = name.strip().lower()
        self.profile = deepcopy(profile)
        self.api_key_env = api_key_env

    @classmethod
    def get(cls, name: str) -> BaseProvider:
        from apix.agent.core.bot.provider.registry import PROVIDERS

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
        from apix.agent.core.bot.provider.registry import PROVIDERS

        if not isinstance(provider, BaseProvider):
            raise TypeError("provider must be a BaseProvider")
        if provider.name in PROVIDERS and not replace_existing:
            raise ValueError(f"provider {provider.name!r} is already registered")
        PROVIDERS[provider.name] = deepcopy(provider)

    @classmethod
    def list_providers(cls) -> list[str]:
        from apix.agent.core.bot.provider.registry import PROVIDERS

        return list(PROVIDERS)

    @classmethod
    def unregister(cls, name: str) -> None:
        from apix.agent.core.bot.provider.registry import PROVIDERS

        del PROVIDERS[name]

    def list_models(self) -> list[ModelProfile]:
        return deepcopy(self.profile.list_models())

    def get_profile(self) -> ProviderProfile:
        """Return an independent provider configuration, including its models.

        This snapshot contains configured credentials but does not resolve
        environment variables or apply call-specific credential overrides.
        """
        return deepcopy(self.profile)

    def get_model(self, model: str) -> ModelProfile:
        if not isinstance(model, str):
            raise TypeError("model must be a string")
        for preset in self.profile.list_models():
            if preset.model == model:
                return deepcopy(preset)
        available = [preset.model for preset in self.profile.list_models()]
        raise ValueError(
            f"unknown model {model!r} for {self.name!r}; choose from {available}"
        )

    def create_llm(self, model: str, *, api_key: str | None = None) -> MyLLM:
        selected = self.get_model(model)
        if api_key is not None and (
            not isinstance(api_key, str) or not api_key.strip()
        ):
            raise ValueError("api_key must be a non-empty string or None")
        profile = self.get_profile()
        binding = resolve_profiles(profile, selected).parameter(
            "api_key", phase="decode"
        )
        credential = binding.default_value if binding is not None else omit
        if api_key is not None:
            credential = api_key
            if binding is None:
                binding = resolve_profiles(
                    ProviderProfile(protocol=profile.protocol),
                    selected,
                ).parameter("api_key", phase="decode")
        elif binding is not None and credential in (None, omit) and self.api_key_env:
            credential = os.environ.get(self.api_key_env)
            if not credential:
                raise ConfigurationError(
                    f"set {self.api_key_env} or pass api_key for {self.name}"
                )
        if binding is not None and credential is not omit:
            profile = replace(
                profile, api_key=replace(binding, default_value=credential)
            )
        return MyLLM(provider=profile, model=selected, stdio_protocol="openai_chat")

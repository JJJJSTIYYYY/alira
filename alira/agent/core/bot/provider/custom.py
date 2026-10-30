"""User-supplied native profiles, registered and selected through string names."""

from my_llm import Attribute, ProviderProfile

from alira.agent.core.bot.provider.base import BaseProvider


class CustomProvider(BaseProvider):
    """Declare an exact catalogue for an additional endpoint or model family.

    Configuration belongs to the caller; no undocumented vendor defaults or
    model capabilities are inferred. Token paths use the native profile.
    """

    def __init__(
        self,
        *,
        name: str = "custom",
        profile: ProviderProfile | None = None,
        api_key_env: str | None = None,
    ) -> None:
        super().__init__(
            name,
            profile
            if profile is not None
            else ProviderProfile(models=Attribute(default_value=[])),
            api_key_env=api_key_env,
        )

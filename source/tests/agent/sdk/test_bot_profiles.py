"""Model/provider configuration is wholly owned by my-llm-py."""

from dataclasses import replace

from my_llm import Config, ModelProfile, Parameter, ProviderProfile

from tests.agent.sdk.test_bot import make_profile_bot


def test_profile_configuration_and_nested_values_are_isolated():
    body = {"thinking": {"type": "enabled"}}
    provider = ProviderProfile(
        protocol="openai_chat",
        api_key=Parameter(
            name="api_key", path="$.api_key", default_value="test", phase="decode"
        ),
        extra_body=Config(name="extra_body", default_value=body),
    )
    model = ModelProfile(
        model="test",
        supports_function_calling=Config(
            name="supports_function_calling", default_value=True
        ),
    )
    bot = make_profile_bot(provider=provider, model=model)
    body["thinking"]["type"] = "disabled"
    assert bot.llm.resolved_profile.get("extra_body") == {
        "thinking": {"type": "enabled"}
    }
    other = make_profile_bot(
        provider=replace(provider, protocol="anthropic"), model=model
    )
    assert other.llm.stdio_protocol == "openai_chat"
    assert other.llm.provider.protocol == "anthropic"

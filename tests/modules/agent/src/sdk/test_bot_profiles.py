"""Model/provider configuration is wholly owned by my-llm-py."""

from dataclasses import replace

from my_llm import (
    Attribute,
    Config,
    InputParameter,
    ModelProfile,
    ProviderProfile,
    TextContent,
    UserMessage,
)

from alira.modules.agent.src.core.bot import BaseProvider
from tests.modules.agent.src.sdk.test_bot import make_profile_bot
from tests.modules.agent.src.sdk.test_bot_protocols import install_sdk


def test_profile_configuration_and_nested_values_are_isolated():
    body = {"thinking": {"type": "enabled"}}
    provider = ProviderProfile(
        protocol=Config(default_value="openai_chat"),
        api_key=InputParameter(
            path="$.api_key",
            default_value="test",
        ),
        extra_body=Config(default_value=body),
    )
    model = ModelProfile(
        model=Attribute(default_value="test"),
        supports_function_calling=Config(default_value=True),
    )
    bot = make_profile_bot(provider=provider, model=model)
    body["thinking"]["type"] = "disabled"
    assert bot.llm.resolved_profile.get("extra_body") == {
        "thinking": {"type": "enabled"}
    }
    other = make_profile_bot(
        provider=replace(provider, protocol=Config(default_value="anthropic")),
        model=model,
    )
    assert other.llm.stdio_protocol == "middleware"
    assert other.llm.resolved_profile.protocol == "anthropic"


def test_native_profile_members_expose_catalogue_and_parameter_identity():
    profile = BaseProvider.get("deepseek").get_profile()
    assert profile.get_attributes()["models"] is profile.models
    assert profile.protocol is None
    parameter = profile.get_parameters()["reasoning_enable"]
    assert parameter.name == "reasoning_enable"
    assert parameter.source == "input"
    assert parameter.supports is True
    model = profile.list_models()[0]
    assert model.get_configs()["protocol"].default_value == "openai_responses"
    assert model.get_attributes()["model"].default_value == "deepseek-flash"
    assert model.get_attributes()["max_context_window"].default_value == 1_048_576


async def test_disabled_reasoning_is_ignored_and_request_reaches_transport(monkeypatch):
    requests = []
    import httpx

    from tests.modules.agent.src.sdk.test_bot_protocols import wire

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            text=wire("openai_responses"),
            headers={"content-type": "text/event-stream"},
        )

    clients = install_sdk(monkeypatch, "openai_responses", handler)
    provider = BaseProvider.get("deepseek")
    provider.profile.add_models(
        replace(
            provider.get_model("deepseek-flash"),
            reasoning_enable=InputParameter(
                path="$.extra_body.thinking", supports=False
            ),
        )
    )
    async with provider.create_llm("deepseek-flash", api_key="test") as llm:
        await llm.generate(
            messages=[UserMessage(role="user", content=[TextContent("hi")])],
            reasoning=True,
        )
    assert requests and all(client.is_closed for client in clients)

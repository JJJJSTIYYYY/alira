"""Named selection, isolated catalogues, and actual vendor request serialization."""

import json
from dataclasses import replace

import httpx
import pytest
from my_llm import (
    Attribute,
    Config,
    ConfigurationError,
    ModelProfile,
    Parameter,
    ProviderProfile,
    UnsupportedCapabilityError,
    omit,
)

from apix.agent.core.bot import ApixBot, BaseProvider, CustomProvider
from tests.agent.sdk.test_bot import weather
from tests.agent.sdk.test_bot_protocols import install_sdk, wire

PRESETS = [
    (name, model.model.default_value)
    for name in BaseProvider.list_providers()
    if name != "custom"
    for model in BaseProvider.get(name).list_models()
]


@pytest.mark.parametrize(
    "provider,model,enabled",
    [
        ("deepseek", "deepseek-flash", {"type": "enabled"}),
        ("moonshot", "kimi-k2.6", {"type": "enabled", "keep": "all"}),
    ],
)
@pytest.mark.parametrize("reasoning", [False, True])
async def test_generated_thinking_controls_take_precedence_over_native_body(
    monkeypatch,
    provider,
    model,
    enabled,
    reasoning,
):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, text=wire("openai_chat"), headers={"content-type": "text/event-stream"}
        )

    install_sdk(monkeypatch, "openai_chat", handler)
    native = {"thinking": {"type": "disabled"}}
    async with ApixBot(provider=provider, model=model, api_key="test") as bot:
        await bot.invoke(
            [{"role": "user", "content": "hi"}], reasoning=reasoning, extra_body=native
        )
    assert requests[0]["thinking"] == (enabled if reasoning else native["thinking"])
    assert native == {"thinking": {"type": "disabled"}}


@pytest.mark.parametrize("provider,model", PRESETS)
def test_builtin_effort_mappings_have_an_ordered_contiguous_prefix(provider, model):
    llm = BaseProvider.get(provider).create_llm(model, api_key="test")
    if llm.resolved_profile.parameter("reasoning_effort", source="input") is None:
        return
    levels = ["low", "medium", "high", "xhigh", "max"]
    mapping = llm.resolved_profile.get("reasoning_effort_mapping")
    supported = [level for level in levels if mapping[level] is not None]
    assert supported == levels[: len(supported)], (
        f"{provider}/{model} has a gap in {mapping}"
    )
    ranks = [levels.index(mapping[level]) for level in supported]
    assert ranks == sorted(ranks), f"{provider}/{model} has descending effort levels"


def test_required_provider_classes_and_exact_string_selection():
    from apix.agent.core.bot import provider as package

    names = {
        "BaseProvider",
        "OpenAIPorvider",
        "DeepseekProvider",
        "MoonShotProvider",
        "XiaomiMiMOProvider",
        "AnthropicProvider",
        "GoogleProvider",
        "CustomProvider",
    }
    assert set(package.__all__) == names
    assert {
        "openai",
        "deepseek",
        "moonshot",
        "mimo",
        "anthropic",
        "google",
        "custom",
    } <= set(BaseProvider.list_providers())
    bot = ApixBot(provider=" OpenAI ", model="gpt-5.4", api_key="test")
    assert bot.provider.name == "openai" and bot.model == "gpt-5.4"
    assert bot.llm.stdio_protocol == "openai_chat"
    for provider, model in (
        (
            ProviderProfile(
                protocol=Config(default_value="openai_chat"),
            ),
            "gpt-5.4",
        ),
        ("openai", ModelProfile(model=Attribute(default_value="x"))),
    ):
        with pytest.raises(TypeError):
            ApixBot(provider=provider, model=model, api_key="test")
    with pytest.raises(ValueError, match="unknown provider"):
        ApixBot(provider="missing", model="x", api_key="test")
    with pytest.raises(ValueError, match="unknown model"):
        ApixBot(provider="deepseek", model="gpt-5.4", api_key="test")
    with pytest.raises(ValueError, match="unknown model"):
        ApixBot(provider="custom", model="undeclared", api_key="test")


def test_credentials_use_provider_environment_and_explicit_key_wins(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-openai-test-key")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(ConfigurationError, match="DEEPSEEK_API_KEY"):
        ApixBot(provider="deepseek", model="deepseek-flash")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-test-key")
    bot = ApixBot(provider="deepseek", model="deepseek-flash")
    assert bot.llm.provider.api_key.default_value == "deepseek-test-key"
    explicit = ApixBot(
        provider="deepseek", model="deepseek-flash", api_key="explicit-test-key"
    )
    assert explicit.llm.provider.api_key.default_value == "explicit-test-key"
    assert BaseProvider.get("deepseek").profile.api_key is None


@pytest.mark.parametrize("source", ["profile", "environment", "explicit"])
async def test_credential_overrides_keep_custom_authentication_path(
    monkeypatch, source
):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CUSTOM_AUTH_KEY", "environment-test-key")
    binding = Parameter(
        path="$.extra_headers.X-Api-Key",
        default_value=omit if source == "environment" else "profile-test-key",
    )
    provider = CustomProvider(
        api_key_env="CUSTOM_AUTH_KEY",
        profile=ProviderProfile(
            protocol=Config(default_value="anthropic"),
            api_key=binding,
            models=Attribute(
                default_value=[
                    ModelProfile(
                        model=Attribute(default_value="test"),
                        max_tokens=Parameter(
                            path="$.max_tokens",
                            default_value=100,
                        ),
                    )
                ]
            ),
        ),
    )
    llm = provider.create_llm(
        "test", api_key="explicit-test-key" if source == "explicit" else None
    )
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200, text=wire("anthropic"), headers={"content-type": "text/event-stream"}
        )

    install_sdk(monkeypatch, "anthropic", handler)
    async with llm:
        await llm.generate(messages=[{"role": "user", "content": "hi"}])
    assert requests[0].headers["X-Api-Key"] == f"{source}-test-key"
    assert "api_key" not in json.loads(requests[0].content)
    assert llm.provider.api_key.path == binding.path
    assert provider.get_profile().api_key.path == binding.path
    assert provider.get_profile().api_key.default_value == binding.default_value


def test_omitted_credentials_skip_environment_and_explicit_key_uses_protocol_path(
    monkeypatch,
):
    monkeypatch.delenv("CUSTOM_AUTH_KEY", raising=False)
    provider = CustomProvider(
        api_key_env="CUSTOM_AUTH_KEY",
        profile=ProviderProfile(
            protocol=Config(default_value="ollama"),
            api_key=omit,
            models=Attribute(
                default_value=[ModelProfile(model=Attribute(default_value="test"))]
            ),
        ),
    )
    assert provider.create_llm("test").provider.api_key is omit
    explicit = provider.create_llm("test", api_key="Bearer explicit-test-key")
    assert explicit.provider.api_key.path == "$.extra_headers.Authorization"
    assert (
        explicit.client_parameters["headers"]["Authorization"]
        == "Bearer explicit-test-key"
    )


def test_registry_and_model_catalogues_use_independent_library_profiles():
    preset = BaseProvider.get("moonshot")
    preset.profile.add_models(ModelProfile(model=Attribute(default_value="new-model")))
    preset.profile.delete_model(["kimi-k2.6"])
    assert "new-model" not in [
        model.model.default_value
        for model in BaseProvider.get("moonshot").list_models()
    ]
    assert BaseProvider.get("moonshot").get_model("kimi-k2.6")
    changed = preset.get_model("kimi-k3")
    changed.reasoning_effort_mapping.default_value["high"] = None
    assert (
        preset.get_model("kimi-k3").reasoning_effort_mapping.default_value["high"]
        == "max"
    )
    custom = CustomProvider(
        name="isolated-test",
        profile=ProviderProfile(
            protocol=Config(default_value="openai_chat"),
            api_key=Parameter(
                path="$.api_key",
                default_value="test",
            ),
            models=Attribute(
                default_value=[ModelProfile(model=Attribute(default_value="my-model"))]
            ),
        ),
    )
    BaseProvider.register(custom)
    try:
        custom.profile.delete_model(["my-model"])
        assert ApixBot(provider="isolated-test", model="my-model").model == "my-model"
        with pytest.raises(ValueError, match="already registered"):
            BaseProvider.register(custom)
        BaseProvider.register(custom, replace_existing=True)
        assert BaseProvider.get("isolated-test").list_models() == []
    finally:
        BaseProvider.unregister("isolated-test")


def test_get_profile_returns_an_isolated_complete_configuration(monkeypatch):
    monkeypatch.setenv("SNAPSHOT_TEST_API_KEY", "environment-test-key")
    provider = CustomProvider(
        api_key_env="SNAPSHOT_TEST_API_KEY",
        profile=ProviderProfile(
            protocol=Config(default_value="openai_chat"),
            extra_body=Config(default_value={"thinking": {"type": "enabled"}}),
            models=Attribute(
                default_value=[
                    ModelProfile(
                        model=Attribute(default_value="snapshot-model"),
                        extra_body=Config(default_value={"options": {"seed": 1}}),
                    )
                ]
            ),
        ),
    )
    profile = provider.get_profile()
    assert isinstance(profile, ProviderProfile)
    assert profile == provider.profile
    assert (
        profile.api_key is None
    )  # Reading configuration does not resolve environment credentials.
    profile.extra_body.default_value["thinking"]["type"] = "disabled"
    profile.list_models()[0].extra_body.default_value["options"]["seed"] = 2
    profile.add_models(ModelProfile(model=Attribute(default_value="another-model")))
    original = provider.get_profile()
    assert original.extra_body.default_value == {"thinking": {"type": "enabled"}}
    assert [model.model.default_value for model in original.list_models()] == [
        "snapshot-model"
    ]
    assert original.list_models()[0].extra_body.default_value == {
        "options": {"seed": 1}
    }
    llm = provider.create_llm("snapshot-model", api_key="call-test-key")
    assert llm.provider.api_key.default_value == "call-test-key"
    assert provider.get_profile().api_key is None


@pytest.mark.parametrize("provider,model", PRESETS)
@pytest.mark.parametrize("streaming", [False, True])
async def test_each_builtin_preset_dispatches_through_official_sdk(
    monkeypatch, provider, model, streaming
):
    bot = ApixBot(provider=provider, model=model, api_key="test")
    protocol = bot.llm.provider.protocol.default_value
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200, text=wire(protocol), headers={"content-type": "text/event-stream"}
        )

    clients = install_sdk(monkeypatch, protocol, handler)
    history = [{"role": "user", "content": "hi"}]
    options = {"max_tokens": 1024, "reasoning": True, "tools": [weather]}
    if streaming:
        stream = await bot.stream(history, **options)
        async with stream:
            events = [event async for event in stream]
        assert events and stream.result["role"] == "assistant"
    else:
        assert (await bot.invoke(history, **options)).content == "hello"
    assert clients and all(not client.is_closed for client in clients)
    await bot.aclose()
    assert all(client.is_closed for client in clients)
    body = json.loads(requests[0].content)
    assert body.get("model", model) == model
    assert "weather" in str(body)
    assert "message_uid" not in str(body)
    if protocol == "openai_chat":
        field = (
            "max_tokens"
            if (provider == "deepseek" or provider == "moonshot" and model != "kimi-k3")
            else "max_completion_tokens"
        )
        assert body[field] == 1024
        assert (
            "max_completion_tokens" if field == "max_tokens" else "max_tokens"
        ) not in body
        if provider in {"deepseek", "mimo"} or model != "kimi-k3":
            assert body["thinking"]["type"] == "enabled"
        if provider == "deepseek":
            assert body["reasoning_effort"] == "high"
        elif model == "kimi-k3":
            assert body["reasoning_effort"] == "high"
            assert "thinking" not in body
        else:
            assert "reasoning_effort" not in body
    elif protocol == "openai_responses":
        assert body["max_output_tokens"] == 1024
        assert body["reasoning"]["effort"] == (
            "high" if model == "gpt-5.4-pro" else "medium"
        )
    elif protocol == "anthropic":
        assert body["max_tokens"] == 1024
        assert body["thinking"] == {"type": "adaptive"}
        assert body["output_config"]["effort"] == "medium"
        assert "budget_tokens" not in body["thinking"]
    else:
        assert body["generationConfig"]["maxOutputTokens"] == 1024
        thinking = body["generationConfig"]["thinkingConfig"]
        assert (
            thinking.get("thinkingLevel", thinking.get("thinking_level", "")).lower()
            == "medium"
        )
        assert "thinkingBudget" not in thinking and "thinking_budget" not in thinking


@pytest.mark.parametrize(
    "provider,model",
    [
        ("deepseek", "deepseek-flash"),
        ("moonshot", "kimi-k2.6"),
        ("moonshot", "kimi-k2.7-code"),
        ("moonshot", "kimi-k2.7-code-highspeed"),
    ],
)
@pytest.mark.parametrize("tokens", ["default", None, omit, 1536])
@pytest.mark.parametrize("manual_limit", [None, 999])
async def test_native_token_defaults_overrides_and_omit(
    monkeypatch, provider, model, tokens, manual_limit
):
    preset = BaseProvider.get(provider)
    selected = replace(
        preset.get_model(model),
        max_tokens=Parameter(
            path="$.max_tokens",
            default_value=2048,
        ),
    )
    preset.profile.add_models(selected)
    llm = preset.create_llm(model, api_key="test")
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, text=wire("openai_chat"), headers={"content-type": "text/event-stream"}
        )

    install_sdk(monkeypatch, "openai_chat", handler)
    options = {"temperature": 0.2}
    if manual_limit is not None:
        options["max_tokens"] = manual_limit
    async with llm:
        controls = {} if tokens == "default" else {"max_tokens": tokens}
        await llm.generate(
            messages=[{"role": "user", "content": "hi"}], extra_body=options, **controls
        )
    body = requests[0]
    assert "max_completion_tokens" not in body
    if tokens is omit:
        assert "max_tokens" not in body
    elif tokens == "default":
        assert body["max_tokens"] == (2048 if manual_limit is None else manual_limit)
    else:
        assert body["max_tokens"] == tokens
    assert body["temperature"] == 0.2
    assert options == (
        {"temperature": 0.2}
        if manual_limit is None
        else {"temperature": 0.2, "max_tokens": manual_limit}
    )


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "1024"])
async def test_native_token_limits_validate_before_dispatch(value):
    bot = ApixBot(provider="moonshot", model="kimi-k2.6", api_key="test")
    with pytest.raises(ConfigurationError, match="positive integer"):
        await bot.invoke([{"role": "user", "content": "hi"}], max_tokens=value)


@pytest.mark.parametrize(
    "provider,model,effort",
    [
        ("openai", "gpt-5.4", "max"),
        ("openai", "gpt-5.4-pro", "xhigh"),
        ("openai", "gpt-5.4-pro", "max"),
        ("google", "gemini-3.8-flash", "max"),
        ("google", "gemini-3.1-pro-preview", "xhigh"),
        ("moonshot", "kimi-k3", "xhigh"),
        ("moonshot", "kimi-k3", "max"),
        ("deepseek", "deepseek-flash", "xhigh"),
        ("deepseek", "deepseek-flash", "max"),
    ],
)
async def test_unsupported_model_efforts_are_rejected_by_myllm(provider, model, effort):
    bot = ApixBot(provider=provider, model=model, api_key="test")
    with pytest.raises(UnsupportedCapabilityError):
        await bot.invoke([{"role": "user", "content": "hi"}], reasoning_effort=effort)


@pytest.mark.parametrize(
    "provider,model", [("moonshot", "kimi-k3"), ("deepseek", "deepseek-flash")]
)
@pytest.mark.parametrize(
    "options,expected",
    [
        ({"reasoning": True}, "high"),
        ({"reasoning_effort": "low"}, "low"),
        ({"reasoning_effort": "medium"}, "high"),
        ({"reasoning_effort": "high"}, "max"),
        ({}, None),
    ],
)
async def test_three_level_efforts_follow_native_profile_mapping(
    monkeypatch, provider, model, options, expected
):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, text=wire("openai_chat"), headers={"content-type": "text/event-stream"}
        )

    install_sdk(monkeypatch, "openai_chat", handler)
    async with ApixBot(provider=provider, model=model, api_key="test") as bot:
        await bot.invoke([{"role": "user", "content": "hi"}], **options)
    if expected is None:
        assert "reasoning_effort" not in requests[0]
    else:
        assert requests[0]["reasoning_effort"] == expected
    if provider == "moonshot":
        assert "thinking" not in requests[0]


@pytest.mark.parametrize(
    "model,effort,expected",
    [
        ("gpt-6-astra", "max", "max"),
        ("gpt-6.1-sol", "max", "max"),
        ("gpt-6-sol", "max", "max"),
        ("gpt-6-luna", "max", "max"),
        ("gpt-5.4-pro", "low", "medium"),
        ("gpt-5.4-pro", "medium", "high"),
        ("gpt-5.4-pro", "high", "xhigh"),
    ],
)
async def test_openai_model_specific_efforts_reach_responses_sdk(
    monkeypatch, model, effort, expected
):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            text=wire("openai_responses"),
            headers={"content-type": "text/event-stream"},
        )

    install_sdk(monkeypatch, "openai_responses", handler)
    bot = ApixBot(provider="openai", model=model, api_key="test")
    async with bot:
        await bot.invoke([{"role": "user", "content": "hi"}], reasoning_effort=effort)
    assert requests[0]["reasoning"]["effort"] == expected
    assert requests[0]["model"] == model


@pytest.mark.parametrize(
    "model_path,field",
    [
        (None, "max_tokens"),
        ("$.max_completion_tokens", "max_completion_tokens"),
    ],
)
async def test_custom_provider_uses_native_profile_and_library_lifecycle(
    monkeypatch, model_path, field
):
    custom = CustomProvider(
        name="custom-test",
        profile=ProviderProfile(
            protocol=Config(default_value="openai_chat"),
            api_key=Parameter(
                path="$.api_key",
                default_value="test",
            ),
            base_url=Config(default_value="https://custom.test/v1"),
            max_tokens=Parameter(
                path="$.extra_body.max_tokens",
                default_value=2000,
            ),
            models=Attribute(
                default_value=[
                    ModelProfile(
                        model=Attribute(default_value="custom-model"),
                        max_tokens=None
                        if model_path is None
                        else Parameter(
                            path=model_path,
                            default_value=2000,
                        ),
                    )
                ]
            ),
        ),
    )
    BaseProvider.register(custom)
    try:
        bot = ApixBot(provider="custom-test", model="custom-model")
    finally:
        BaseProvider.unregister("custom-test")
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, text=wire("openai_chat"), headers={"content-type": "text/event-stream"}
        )

    clients = install_sdk(monkeypatch, "openai_chat", handler)
    options = {"extra_body": {"temperature": 0.2}}
    async with bot:
        assert (
            await bot.invoke([{"role": "user", "content": "hi"}], **options)
        ).content == "hello"
    assert requests[0][field] == 2000
    assert {key for key in requests[0] if key.startswith("max_")} == {field}
    assert all(client.is_closed for client in clients)
    assert options == {"extra_body": {"temperature": 0.2}}

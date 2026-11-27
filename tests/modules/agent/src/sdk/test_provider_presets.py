"""Named selection, isolated catalogues, and actual vendor request serialization."""

import ast
import inspect
import json
from dataclasses import fields
from unittest.mock import Mock

import httpx
import pytest
from my_llm import (
    Attribute,
    Config,
    InputParameter,
    ModelProfile,
    Parameter,
    ProviderProfile,
    ReasoningContent,
    TextContent,
    UserMessage,
    omit,
)
from my_llm.codec.openai.chat.prebuilt import chat_semantic_loss_hook

from alira.modules.agent.src.core.bot import AliraBot, BaseProvider, CustomProvider
from alira.modules.agent.src.core.bot.provider import base
from alira.modules.agent.src.shared.model.message import AliraUserMessage
from tests.modules.agent.src.sdk.test_bot import weather
from tests.modules.agent.src.sdk.test_bot_protocols import install_sdk, wire

PRESETS = [
    (name, model.model.default_value)
    for name in BaseProvider.list_providers()
    if name != "custom"
    for model in BaseProvider.get(name).list_models()
]

CHAT_MODELS = {"kimi-k2.6", "kimi-k2.7-code", "kimi-k2.7-code-highspeed"}
EXPECTED_PROTOCOLS = {
    "openai": "openai_responses",
    "deepseek": "openai_responses",
    "moonshot": "openai_responses",
    "mimo": "openai_responses",
    "anthropic": "anthropic",
}


@pytest.mark.parametrize("provider,model", PRESETS)
def test_builtin_models_select_their_declared_upstream_protocol(provider, model):
    llm = BaseProvider.get(provider).create_llm(model)
    expected = "openai_chat" if model in CHAT_MODELS else EXPECTED_PROTOCOLS[provider]
    assert isinstance(llm.model.protocol, Config)
    assert llm.model.protocol.default_value == expected
    assert llm.resolved_profile.protocol == expected
    assert llm.stdio_protocol == "middleware"


def test_kimi_catalogue_includes_both_k31_pro_variants():
    assert {
        "kimi-k3",
        "kimi-k3.1-pro",
        "kimi-k3.1-pro-highspeed",
        *CHAT_MODELS,
    } == {
        model.model.default_value
        for model in BaseProvider.get("moonshot").list_models()
    }


@pytest.mark.parametrize(
    "provider,model",
    [
        ("moonshot", "kimi-k2.6"),
        ("openai", "gpt-6-astra"),
        ("deepseek", "deepseek-flash"),
        ("anthropic", "claude-opus-5-5"),
    ],
)
def test_provider_configures_middleware_and_chat_loss_policy(
    monkeypatch, provider, model
):
    catalogue = BaseProvider.get(provider)
    selected = catalogue.get_model(model)
    factory = Mock()
    factory.return_value.resolved_profile.protocol = selected.protocol.default_value
    monkeypatch.setattr(base, "MyLLM", factory)

    llm = catalogue.create_llm(model)

    factory.assert_called_once_with(
        provider=catalogue.get_profile(),
        model=selected,
        stdio_protocol="middleware",
        semantic_loss_protocol="ignore",
        enable_message_cache=False,
    )
    assert llm is factory.return_value
    if selected.protocol.default_value == "openai_chat":
        llm.set_semantic_loss_policy.assert_called_once_with(
            semantic_loss_protocol="hook",
            input_semantic_loss_hook=chat_semantic_loss_hook,
        )
    else:
        llm.set_semantic_loss_policy.assert_not_called()


def reasoning_wire(protocol, *, raw=False):
    """Add documented reasoning events to the official SDK response fixtures."""
    events = [
        json.loads(line.removeprefix("data: "))
        for line in wire(protocol).splitlines()
        if line.startswith("data: ")
    ]
    if protocol == "openai_chat":
        events[0]["choices"][0]["delta"]["reasoning_content"] = "visible reasoning"
    elif protocol == "openai_responses":
        reasoning = {
            "type": "reasoning",
            "id": "rs1",
            "summary": [
                {"type": "summary_text", "text": "visible reasoning"},
            ],
        }
        if raw:
            reasoning["content"] = [
                {"type": "reasoning_text", "text": "visible reasoning"},
            ]
            reasoning["summary"] = []
        events[0]["response"]["output"].insert(0, reasoning)
        events.insert(
            0,
            {
                "type": "response.reasoning_text.delta"
                if raw
                else "response.reasoning_summary_text.delta",
                "sequence_number": 0,
                "item_id": "rs1",
                "output_index": 0,
                "content_index" if raw else "summary_index": 0,
                "delta": "visible reasoning",
            },
        )
    elif protocol == "anthropic":
        for event in events:
            if "index" in event:
                event["index"] += 1
        events[1:1] = [
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": "", "signature": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "visible reasoning"},
            },
            {"type": "content_block_stop", "index": 0},
        ]
    else:
        events[0]["candidates"][0]["content"]["parts"].insert(
            0, {"text": "visible reasoning", "thought": True}
        )
    return "".join(
        (f"event: {event['type']}\n" if "type" in event else "")
        + f"data: {json.dumps(event)}\n\n"
        for event in events
    )


@pytest.mark.parametrize(
    "provider,model",
    [
        ("openai", "gpt-6-astra"),
        ("deepseek", "deepseek-flash"),
        ("moonshot", "kimi-k3"),
        ("mimo", "mimo-v2.6-pro"),
        ("anthropic", "claude-opus-5-5"),
    ],
)
@pytest.mark.parametrize("streaming", [False, True])
async def test_middleware_bot_preserves_native_reasoning_feedback(
    monkeypatch, provider, model, streaming
):
    bot = AliraBot(provider=provider, model=model)
    protocol = bot.llm.resolved_profile.protocol
    install_sdk(
        monkeypatch,
        protocol,
        lambda request: httpx.Response(
            200,
            text=reasoning_wire(protocol, raw=provider in {"deepseek", "mimo"}),
            headers={"content-type": "text/event-stream"},
        ),
    )
    if streaming:
        stream = await bot.stream(
            [UserMessage(role="user", content=[TextContent("hi")])], api_key="test"
        )
        async with stream:
            async for _ in stream:
                pass
        result = stream.result
    else:
        result = await bot.invoke(
            [UserMessage(role="user", content=[TextContent("hi")])], api_key="test"
        )
    thinking = next(
        part for part in result.content if isinstance(part, ReasoningContent)
    )
    assert thinking.reasoning == "visible reasoning"
    assert thinking.feedback_item is not None


@pytest.mark.parametrize("provider,model", PRESETS)
def test_builtin_effort_mappings_have_an_ordered_contiguous_prefix(provider, model):
    llm = BaseProvider.get(provider).create_llm(model)
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


@pytest.mark.parametrize(
    "provider", [name for name in BaseProvider.list_providers() if name != "custom"]
)
def test_provider_catalogues_explicitly_declare_every_profile_field(provider):
    catalogue = BaseProvider.get(provider)
    assert catalogue.get_profile().provider == Attribute(default_value=provider)
    tree = ast.parse(inspect.getsource(type(catalogue)))
    contracts = {
        "ProviderProfile": {field.name for field in fields(ProviderProfile)},
        "ModelProfile": {field.name for field in fields(ModelProfile)},
        "InputParameter": {"path", "default_value", "supports"},
        "OutputParameter": {"path", "default_value", "supports"},
    }
    counts = {name: 0 for name in contracts}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id in contracts:
            counts[node.func.id] += 1
            declared = {keyword.arg for keyword in node.keywords}
            assert contracts[node.func.id] <= declared, (
                provider,
                node.func.id,
                contracts[node.func.id] - declared,
            )
    assert counts["ProviderProfile"] == 1
    assert counts["ModelProfile"] == len(catalogue.list_models())


@pytest.mark.parametrize("provider,model", PRESETS)
def test_builtin_profile_inheritance_always_retains_a_declaration(provider, model):
    catalogue = BaseProvider.get(provider)
    profile = catalogue.get_profile()
    selected = catalogue.get_model(model)
    for field in fields(profile):
        if field.name == "protocol":
            assert profile.protocol is None
            continue
        assert getattr(profile, field.name) is not None, (provider, field.name)
    for field in fields(selected):
        declaration = getattr(selected, field.name)
        if declaration is None:
            declaration = getattr(profile, field.name, None)
        assert (
            isinstance(declaration, (Config, Parameter, Attribute))
            or declaration is omit
        ), (provider, model, field.name)


def test_required_provider_classes_and_exact_string_selection():
    from alira.modules.agent.src.core.bot import provider as package

    names = {
        "BaseProvider",
        "OpenAIPorvider",
        "DeepseekProvider",
        "MoonShotProvider",
        "XiaomiMiMOProvider",
        "AnthropicProvider",
        "CustomProvider",
    }
    assert set(package.__all__) == names
    assert {
        "openai",
        "deepseek",
        "moonshot",
        "mimo",
        "anthropic",
        "custom",
    } <= set(BaseProvider.list_providers())
    assert BaseProvider.get("custom").get_profile() == ProviderProfile()
    assert BaseProvider.get("custom").list_models() == []
    bot = AliraBot(provider=" OpenAI ", model="gpt-5.4")
    assert bot.provider.name == "openai" and bot.model == "gpt-5.4"
    assert bot.llm.stdio_protocol == "middleware"
    assert bot.reuseable("openai", "gpt-5.4") is True
    assert bot.reuseable("deepseek", "gpt-5.4") is False
    assert bot.reuseable("openai", "gpt-5.4-mini") is False
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
            AliraBot(provider=provider, model=model)
    with pytest.raises(ValueError, match="unknown provider"):
        AliraBot(provider="missing", model="x")
    with pytest.raises(ValueError, match="unknown model"):
        AliraBot(provider="deepseek", model="gpt-5.4")
    with pytest.raises(ValueError, match="unknown model"):
        AliraBot(provider="custom", model="undeclared")


def test_credentials_are_not_read_from_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-openai-test-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-test-key")
    bot = AliraBot(provider="deepseek", model="deepseek-flash")
    assert bot.llm.provider.api_key.default_value is omit
    binding = bot.llm.resolved_profile.parameter("api_key", source="input")
    assert binding.default_value is omit
    assert BaseProvider.get("deepseek").profile.api_key.default_value is omit


def test_registry_and_model_catalogues_use_independent_library_profiles():
    preset = BaseProvider.get("moonshot")
    preset.profile.add_models(ModelProfile(model=Attribute(default_value="new-model")))
    preset.profile.delete_model(["kimi-k2.6"])
    assert "new-model" not in [
        model.model.default_value
        for model in BaseProvider.get("moonshot").list_models()
    ]
    assert BaseProvider.get("moonshot").get_model("kimi-k2.6")
    preset.profile.reasoning_effort_mapping.default_value["high"] = None
    assert (
        BaseProvider.get("moonshot").profile.reasoning_effort_mapping.default_value[
            "high"
        ]
        == "max"
    )
    custom = CustomProvider(
        name="isolated-test",
        profile=ProviderProfile(
            protocol=Config(default_value="openai_chat"),
            api_key=InputParameter(
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
        assert AliraBot(provider="isolated-test", model="my-model").model == "my-model"
        with pytest.raises(ValueError, match="already registered"):
            BaseProvider.register(custom)
        BaseProvider.register(custom, replace_existing=True)
        assert BaseProvider.get("isolated-test").list_models() == []
    finally:
        BaseProvider.unregister("isolated-test")


@pytest.mark.parametrize("provider,model", PRESETS)
@pytest.mark.parametrize("streaming", [False, True])
async def test_each_builtin_preset_dispatches_through_official_sdk(
    monkeypatch, provider, model, streaming
):
    bot = AliraBot(provider=provider, model=model)
    protocol = bot.llm.resolved_profile.protocol
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200, text=wire(protocol), headers={"content-type": "text/event-stream"}
        )

    install_sdk(monkeypatch, protocol, handler)
    history = [AliraUserMessage(content=[TextContent("hi")])]
    bot.bind_tools([weather.get_schema()])
    if streaming:
        stream = await bot.stream(history, api_key="test")
        async with stream:
            async for _ in stream:
                pass
        result = stream.result
    else:
        result = await bot.invoke(history, api_key="test")
    assert result.content[0].text == "hello"
    body = json.loads(requests[0].content)
    assert body["model"] == model
    assert "weather" in str(body)
    assert "message_uid" not in str(body)


@pytest.mark.parametrize("provider", ["moonshot", "deepseek"])
def test_three_level_effort_mapping_is_declared_by_catalogue(provider):
    catalogue = BaseProvider.get(provider)
    assert catalogue.get_profile().reasoning_effort_mapping.default_value == {
        "none": "none",
        "low": "low",
        "medium": "high",
        "high": "max",
        "xhigh": None,
        "max": None,
    }


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
def test_openai_model_specific_effort_mapping_is_declared_by_catalogue(
    model, effort, expected
):
    llm = BaseProvider.get("openai").create_llm(model)
    assert llm.resolved_profile.get("reasoning_effort_mapping")[effort] == expected

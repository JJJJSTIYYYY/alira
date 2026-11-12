"""Named selection, isolated catalogues, and actual vendor request serialization."""

import ast
import inspect
import json
from dataclasses import fields, replace

import httpx
import pytest
from my_llm import (
    AssistantMessage,
    Attribute,
    Config,
    ConfigurationError,
    InputParameter,
    ModelProfile,
    MyCodec,
    MyLLM,
    Parameter,
    ProviderProfile,
    ReasoningContent,
    TextContent,
    ToolCall,
    ToolMessage,
    UserMessage,
    omit,
    resolve_profiles,
)

from alira.modules.agent.src.core.bot import AliraBot, BaseProvider, CustomProvider
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
    llm = BaseProvider.get(provider).create_llm(model, api_key="test")
    expected = "openai_chat" if model in CHAT_MODELS else EXPECTED_PROTOCOLS[provider]
    assert isinstance(llm.model.protocol, Config)
    assert llm.model.protocol.default_value == expected
    assert llm.resolved_profile.protocol == expected
    assert llm.codec.codecs[expected].protocol_or_api_style == expected
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


@pytest.mark.parametrize("provider,model", PRESETS)
@pytest.mark.parametrize("streaming", [False, True])
async def test_each_preset_returns_visible_reasoning_without_protocol_conversion(
    monkeypatch, provider, model, streaming
):
    catalogue = BaseProvider.get(provider)
    selected = catalogue.get_model(model)
    protocol = selected.protocol.default_value
    llm = MyLLM(
        provider=replace(
            catalogue.get_profile(),
            api_key=InputParameter(path="$.api_key", default_value="test"),
        ),
        model=selected,
        stdio_protocol=protocol,
    )

    def handler(request):
        return httpx.Response(
            200,
            text=reasoning_wire(protocol, raw=provider in {"deepseek", "mimo"}),
            headers={"content-type": "text/event-stream"},
        )

    clients = install_sdk(monkeypatch, protocol, handler)
    messages = [{"role": "user", "content": "hi"}]
    async with llm:
        if streaming:
            stream = await llm.generate(messages=messages, stream=True, reasoning=True)
            async with stream:
                events = [event async for event in stream]
            result = stream.result
            if protocol == "openai_chat":
                deltas = [
                    e["choices"][0]["delta"].get("reasoning_content", "")
                    for e in events
                    if e.get("choices")
                ]
            elif protocol == "openai_responses":
                deltas = [
                    e["delta"]
                    for e in events
                    if e["type"]
                    in {
                        "response.reasoning_text.delta",
                        "response.reasoning_summary_text.delta",
                    }
                ]
            elif protocol == "anthropic":
                deltas = [
                    e["delta"]["thinking"]
                    for e in events
                    if e.get("delta", {}).get("type") == "thinking_delta"
                ]
            else:
                deltas = [
                    p["text"]
                    for e in events
                    for c in e.get("candidates", [])
                    for p in c.get("content", {}).get("parts", [])
                    if p.get("thought") and p.get("text")
                ]
            assert "".join(deltas) == "visible reasoning"
        else:
            result = await llm.generate(messages=messages, reasoning=True)
        if protocol == "openai_chat":
            assert result["content"] == "hello"
            assert result["reasoning_content"] == "visible reasoning"
        elif protocol == "openai_responses":
            thinking = next(item for item in result if item["type"] == "reasoning")
            field = "content" if provider in {"deepseek", "mimo"} else "summary"
            assert thinking[field][0]["text"] == "visible reasoning"
            message = next(item for item in result if item["type"] == "message")
            assert message["content"][0]["text"] == "hello"
        elif protocol == "anthropic":
            assert result["content"][0]["thinking"] == "visible reasoning"
            assert result["content"][1]["text"] == "hello"
        else:
            assert result["parts"][0]["text"] == "visible reasoning"
            assert result["parts"][0]["thought"] is True
            assert result["parts"][1]["text"] == "hello"
    assert clients and all(client.is_closed for client in clients)


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
    bot = AliraBot(provider=provider, model=model, api_key="test")
    protocol = bot.llm.resolved_profile.protocol
    clients = install_sdk(
        monkeypatch,
        protocol,
        lambda request: httpx.Response(
            200,
            text=reasoning_wire(protocol, raw=provider in {"deepseek", "mimo"}),
            headers={"content-type": "text/event-stream"},
        ),
    )
    async with bot:
        if streaming:
            stream = await bot.stream(
                [UserMessage(role="user", content=[TextContent("hi")])]
            )
            async with stream:
                async for _ in stream:
                    pass
            result = stream.result
        else:
            result = await bot.invoke(
                [UserMessage(role="user", content=[TextContent("hi")])]
            )
        thinking = next(
            part for part in result.content if isinstance(part, ReasoningContent)
        )
        assert thinking.reasoning == "visible reasoning"
        assert thinking.feedback_item is not None
    assert clients and all(client.is_closed for client in clients)


@pytest.mark.parametrize(
    "provider,model,replay_plain_reasoning",
    [
        ("deepseek", "deepseek-flash", True),
        ("deepseek", "deepseek-v4-pro", True),
        ("moonshot", "kimi-k3", True),
        ("moonshot", "kimi-k3.1-pro", True),
        ("moonshot", "kimi-k3.1-pro-highspeed", True),
        ("moonshot", "kimi-k2.6", True),
        ("moonshot", "kimi-k2.7-code", True),
        ("moonshot", "kimi-k2.7-code-highspeed", True),
        ("mimo", "mimo-v2.6-pro", False),
        ("mimo", "mimo-v2.6-pro-ultraspeed", False),
        ("mimo", "mimo-v2.6-flash", False),
    ],
)
@pytest.mark.parametrize("tool_round", [False, True])
async def test_history_replays_reasoning_according_to_provider_policy(
    monkeypatch, provider, model, replay_plain_reasoning, tool_round
):
    requests = []
    bot = AliraBot(provider=provider, model=model, api_key="test")
    protocol = bot.llm.resolved_profile.protocol

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, text=wire(protocol), headers={"content-type": "text/event-stream"}
        )

    clients = install_sdk(monkeypatch, protocol, handler)

    def reasoning(text):
        return ReasoningContent(
            text,
            feedback_item=text
            if protocol == "openai_chat"
            else {
                "type": "reasoning",
                "id": "r-" + text,
                "summary": [{"type": "summary_text", "text": text}],
            },
        )

    messages = [
        UserMessage(role="user", content=[TextContent("hi")]),
        AssistantMessage(
            role="assistant",
            protocol=protocol,
            content=[reasoning("greeting"), TextContent("hello")],
        ),
        UserMessage(role="user", content=[TextContent("weather in Tokyo?")]),
    ]
    if tool_round:
        messages.extend(
            [
                AssistantMessage(
                    role="assistant",
                    protocol=protocol,
                    content=[
                        reasoning("check weather"),
                        ToolCall("c1", "weather", {"city": "Tokyo"}),
                    ],
                ),
                ToolMessage(role="tool", content=[TextContent("sunny")], call_id="c1"),
            ]
        )
    async with bot:
        result = await bot.invoke(messages, tools=[weather] if tool_round else None)
        assert result.content[0].text == "hello"

    if protocol == "openai_chat":
        history = requests[0]["messages"]
        assert history[1]["reasoning_content"] == "greeting"
        if tool_round:
            assert history[3]["reasoning_content"] == "check weather"
            assert history[3]["tool_calls"][0]["id"] == "c1"
            assert history[4] == {
                "role": "tool",
                "tool_call_id": "c1",
                "content": "sunny",
            }
    else:
        assert "messages" not in requests[0]
        history = requests[0]["input"]
        reasoning = [item for item in history if item["type"] == "reasoning"]
        expected = (["greeting"] if replay_plain_reasoning else []) + (
            ["check weather"] if tool_round else []
        )
        assert [
            part["text"] for item in reasoning for part in item["summary"]
        ] == expected
        if tool_round:
            call = next(item for item in history if item["type"] == "function_call")
            assert call["call_id"] == "c1" and call["name"] == "weather"
            assert json.loads(call["arguments"]) == {"city": "Tokyo"}
            output = next(
                item for item in history if item["type"] == "function_call_output"
            )
            assert output["call_id"] == "c1"
            assert output["output"] == [{"type": "input_text", "text": "sunny"}]
    assert messages[1].content[0].reasoning == "greeting"
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize(
    "provider,model,field,native,enabled",
    [
        (
            "deepseek",
            "deepseek-flash",
            "reasoning",
            {"effort": "low"},
            {"effort": "high"},
        ),
        (
            "moonshot",
            "kimi-k2.6",
            "thinking",
            {"type": "disabled"},
            {"type": "enabled", "keep": "all"},
        ),
    ],
)
@pytest.mark.parametrize("reasoning", [False, True])
async def test_generated_thinking_controls_take_precedence_over_native_body(
    monkeypatch,
    provider,
    model,
    field,
    native,
    enabled,
    reasoning,
):
    requests = []
    bot = AliraBot(provider=provider, model=model, api_key="test")
    protocol = bot.llm.resolved_profile.protocol

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, text=wire(protocol), headers={"content-type": "text/event-stream"}
        )

    install_sdk(monkeypatch, protocol, handler)
    body = {field: native}
    async with bot:
        await bot.invoke(
            MyCodec()
            .encode(
                messages=[{"role": "user", "content": "hi"}], input_format="openai_chat"
            )
            .messages,
            reasoning=reasoning,
            extra_body=body,
        )
    assert requests[0][field] == (enabled if reasoning else native)
    assert body == {field: native}


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


@pytest.mark.parametrize(
    "provider", [name for name in BaseProvider.list_providers() if name != "custom"]
)
def test_explicit_null_connection_fields_do_not_reach_sdk_clients(provider):
    catalogue = BaseProvider.get(provider)
    model = catalogue.list_models()[0].model.default_value
    llm = catalogue.create_llm(model, api_key="test")
    for name in ("timeout", "max_retries"):
        declaration = getattr(llm.provider, name)
        assert isinstance(declaration, Config)
        assert declaration.default_value is None
        assert name in llm.resolved_profile.values
        assert llm.resolved_profile.get(name, "fallback") is None
        assert name not in llm.resolved_profile.connection
        assert name not in llm.client_parameters
    assert llm.client_parameters["base_url"] == catalogue.profile.base_url.default_value


@pytest.mark.parametrize("provider,model", PRESETS)
def test_builtin_profiles_resolve_identically_without_protocol_defaults(
    provider, model
):
    catalogue = BaseProvider.get(provider)
    profile = catalogue.get_profile()
    selected = catalogue.get_model(model)
    llm = catalogue.create_llm(model)
    defaults = llm.codec.defaults(llm.resolved_profile.protocol)
    empty = replace(defaults, **{field.name: omit for field in fields(defaults)})
    resolved = resolve_profiles(profile, selected, defaults_for=lambda protocol: empty)
    assert resolved == llm.resolved_profile


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
    bot = AliraBot(provider=" OpenAI ", model="gpt-5.4", api_key="test")
    assert bot.provider.name == "openai" and bot.model == "gpt-5.4"
    assert bot.llm.stdio_protocol == "middleware"
    assert bot.compare_profiles("openai", "gpt-5.4") is True
    assert bot.compare_profiles("deepseek", "gpt-5.4") is False
    assert bot.compare_profiles("openai", "gpt-5.4-mini") is False
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
            AliraBot(provider=provider, model=model, api_key="test")
    with pytest.raises(ValueError, match="unknown provider"):
        AliraBot(provider="missing", model="x", api_key="test")
    with pytest.raises(ValueError, match="unknown model"):
        AliraBot(provider="deepseek", model="gpt-5.4", api_key="test")
    with pytest.raises(ValueError, match="unknown model"):
        AliraBot(provider="custom", model="undeclared", api_key="test")


def test_credentials_ignore_environment_and_explicit_key_wins(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-openai-test-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-test-key")
    bot = AliraBot(provider="deepseek", model="deepseek-flash")
    assert bot.llm.provider.api_key.default_value is omit
    binding = bot.llm.resolved_profile.parameter("api_key", source="input")
    assert binding.default_value is omit
    explicit = AliraBot(
        provider="deepseek", model="deepseek-flash", api_key="explicit-test-key"
    )
    assert explicit.llm.provider.api_key.default_value == "explicit-test-key"
    assert BaseProvider.get("deepseek").profile.api_key.default_value is omit


@pytest.mark.parametrize("source", ["profile", "explicit"])
async def test_credential_overrides_keep_custom_authentication_path(
    monkeypatch, source
):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    binding = InputParameter(
        path="$.extra_headers.X-Api-Key",
        default_value="profile-test-key",
    )
    provider = CustomProvider(
        profile=ProviderProfile(
            protocol=Config(default_value="anthropic"),
            api_key=binding,
            models=Attribute(
                default_value=[
                    ModelProfile(
                        model=Attribute(default_value="test"),
                        max_tokens=InputParameter(
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
        await llm.generate(
            messages=[UserMessage(role="user", content=[TextContent("hi")])]
        )
    assert requests[0].headers["X-Api-Key"] == f"{source}-test-key"
    assert "api_key" not in json.loads(requests[0].content)
    assert llm.provider.api_key.path == binding.path
    assert provider.get_profile().api_key.path == binding.path
    assert provider.get_profile().api_key.default_value == binding.default_value


def test_omitted_credentials_stay_disabled_with_explicit_key():
    provider = CustomProvider(
        profile=ProviderProfile(
            protocol=Config(default_value="openai_chat"),
            api_key=omit,
            models=Attribute(
                default_value=[ModelProfile(model=Attribute(default_value="test"))]
            ),
        ),
    )
    assert provider.create_llm("test").provider.api_key is omit
    explicit = provider.create_llm("test", api_key="Bearer explicit-test-key")
    assert explicit.provider.api_key is omit
    assert "Authorization" not in explicit.client_parameters.get("headers", {})


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
@pytest.mark.parametrize("explicit_reasoning", [False, True])
async def test_each_builtin_preset_dispatches_through_official_sdk(
    monkeypatch, provider, model, streaming, explicit_reasoning
):
    bot = AliraBot(provider=provider, model=model, api_key="test")
    protocol = bot.llm.resolved_profile.protocol
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200, text=wire(protocol), headers={"content-type": "text/event-stream"}
        )

    clients = install_sdk(monkeypatch, protocol, handler)
    history = [{"role": "user", "content": "hi"}]
    options = {"max_tokens": 1024, "tools": [weather]}
    if explicit_reasoning:
        options["reasoning"] = True
    if streaming:
        stream = await bot.stream(
            MyCodec().encode(messages=history, input_format="openai_chat").messages,
            **options,
        )
        async with stream:
            events = [event async for event in stream]
        assert events and stream.result["role"] == "assistant"
    else:
        assert (
            await bot.invoke(
                MyCodec().encode(messages=history, input_format="openai_chat").messages,
                **options,
            )
        ).content[0].text == "hello"
    assert clients and all(not client.is_closed for client in clients)
    await bot.aclose()
    assert all(client.is_closed for client in clients)
    body = json.loads(requests[0].content)
    assert body.get("model", model) == model
    assert "weather" in str(body)
    assert "message_uid" not in str(body)
    if protocol == "openai_chat":
        assert requests[0].url.path == "/v1/chat/completions"
        assert model in CHAT_MODELS
        assert body["max_tokens"] == 1024
        assert "max_completion_tokens" not in body
        assert body["thinking"] == {"type": "enabled", "keep": "all"}
        assert "reasoning_effort" not in body
    elif protocol == "openai_responses":
        assert requests[0].url.path.endswith("/responses")
        assert "messages" not in body and body["input"]
        assert body["max_output_tokens"] == 1024
        assert "max_tokens" not in body and "max_completion_tokens" not in body
        assert "thinking" not in body and "reasoning_effort" not in body
        if provider == "deepseek":
            assert body["reasoning"] == {"effort": "high"}
        elif provider == "moonshot":
            assert body["reasoning"] == {
                "effort": "high" if explicit_reasoning else "max"
            }
        elif provider == "mimo":
            assert body["reasoning"] == {"effort": "medium"}
        else:
            assert body["reasoning"]["effort"] == (
                "high" if explicit_reasoning and model == "gpt-5.4-pro" else "medium"
            )
            assert body["reasoning"]["summary"] == "detailed"
    elif protocol == "anthropic":
        assert requests[0].url.path == "/v1/messages"
        assert body["max_tokens"] == 1024
        assert body["thinking"] == {"type": "adaptive", "display": "summarized"}
        if explicit_reasoning:
            assert body["output_config"]["effort"] == "medium"
        else:
            assert "output_config" not in body
        assert "budget_tokens" not in body["thinking"]
    else:
        assert body["generationConfig"]["maxOutputTokens"] == 1024
        thinking = body["generationConfig"]["thinkingConfig"]
        assert thinking.get("includeThoughts", thinking.get("include_thoughts")) is True
        if explicit_reasoning:
            assert (
                thinking.get(
                    "thinkingLevel", thinking.get("thinking_level", "")
                ).lower()
                == "medium"
            )
            assert (
                "thinkingBudget" not in thinking and "thinking_budget" not in thinking
            )
        else:
            assert thinking.get("thinkingBudget", thinking.get("thinking_budget")) == -1


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
    protocol = preset.create_llm(model).resolved_profile.protocol
    field = "max_output_tokens" if protocol == "openai_responses" else "max_tokens"
    selected = replace(
        preset.get_model(model),
        max_tokens=InputParameter(
            path=f"$.{field}",
            default_value=2048,
        ),
    )
    preset.profile.add_models(selected)
    llm = preset.create_llm(model, api_key="test")
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, text=wire(protocol), headers={"content-type": "text/event-stream"}
        )

    install_sdk(monkeypatch, protocol, handler)
    options = {"temperature": 0.2}
    if manual_limit is not None:
        options[field] = manual_limit
    async with llm:
        controls = {} if tokens == "default" else {"max_tokens": tokens}
        await llm.generate(
            messages=[UserMessage(role="user", content=[TextContent("hi")])],
            extra_body=options,
            **controls,
        )
    body = requests[0]
    assert "max_completion_tokens" not in body
    if tokens is omit:
        assert field not in body
    elif tokens == "default":
        assert body[field] == (2048 if manual_limit is None else manual_limit)
    else:
        assert body[field] == tokens
    assert body["temperature"] == 0.2
    assert options == (
        {"temperature": 0.2}
        if manual_limit is None
        else {"temperature": 0.2, field: manual_limit}
    )


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "1024"])
async def test_native_token_limits_validate_before_dispatch(value):
    bot = AliraBot(provider="moonshot", model="kimi-k2.6", api_key="test")
    with pytest.raises(ConfigurationError, match="positive integer"):
        await bot.invoke(
            MyCodec()
            .encode(
                messages=[{"role": "user", "content": "hi"}], input_format="openai_chat"
            )
            .messages,
            max_tokens=value,
        )


@pytest.mark.parametrize(
    "provider,model,effort",
    [
        ("openai", "gpt-5.4", "max"),
        ("openai", "gpt-5.4-pro", "xhigh"),
        ("openai", "gpt-5.4-pro", "max"),
        ("moonshot", "kimi-k3", "xhigh"),
        ("moonshot", "kimi-k3", "max"),
        ("deepseek", "deepseek-flash", "xhigh"),
        ("deepseek", "deepseek-flash", "max"),
    ],
)
async def test_unsupported_model_efforts_are_ignored(
    monkeypatch, provider, model, effort
):
    bot = AliraBot(provider=provider, model=model, api_key="test")
    protocol = bot.llm.resolved_profile.protocol
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, text=wire(protocol), headers={"content-type": "text/event-stream"}
        )

    install_sdk(monkeypatch, protocol, handler)
    async with bot:
        await bot.invoke(
            [UserMessage(role="user", content=[TextContent("hi")])],
            reasoning_effort=effort,
        )
    assert "reasoning_effort" not in requests[0]


@pytest.mark.parametrize(
    "provider,model",
    [
        ("moonshot", "kimi-k3"),
        ("moonshot", "kimi-k3.1-pro"),
        ("moonshot", "kimi-k3.1-pro-highspeed"),
        ("deepseek", "deepseek-flash"),
        ("deepseek", "deepseek-v4-pro"),
    ],
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
            200,
            text=wire("openai_responses"),
            headers={"content-type": "text/event-stream"},
        )

    install_sdk(monkeypatch, "openai_responses", handler)
    async with AliraBot(provider=provider, model=model, api_key="test") as bot:
        await bot.invoke(
            MyCodec()
            .encode(
                messages=[{"role": "user", "content": "hi"}], input_format="openai_chat"
            )
            .messages,
            **options,
        )
    if expected is None:
        assert requests[0]["reasoning"] == {
            "effort": "max" if provider == "moonshot" else "high"
        }
    else:
        assert requests[0]["reasoning"] == {"effort": expected}
    assert "thinking" not in requests[0] and "reasoning_effort" not in requests[0]


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
    bot = AliraBot(provider="openai", model=model, api_key="test")
    async with bot:
        await bot.invoke(
            MyCodec()
            .encode(
                messages=[{"role": "user", "content": "hi"}], input_format="openai_chat"
            )
            .messages,
            reasoning_effort=effort,
        )
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
            api_key=InputParameter(
                path="$.api_key",
                default_value="test",
            ),
            base_url=Config(default_value="https://custom.test/v1"),
            max_tokens=InputParameter(
                path="$.extra_body.max_tokens",
                default_value=2000,
            ),
            models=Attribute(
                default_value=[
                    ModelProfile(
                        model=Attribute(default_value="custom-model"),
                        max_tokens=None
                        if model_path is None
                        else InputParameter(
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
        bot = AliraBot(provider="custom-test", model="custom-model")
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
            await bot.invoke(
                MyCodec()
                .encode(
                    messages=[{"role": "user", "content": "hi"}],
                    input_format="openai_chat",
                )
                .messages,
                **options,
            )
        ).content[0].text == "hello"
    assert requests[0][field] == 2000
    assert {key for key in requests[0] if key.startswith("max_")} == {field}
    assert all(client.is_closed for client in clients)
    assert options == {"extra_body": {"temperature": 0.2}}

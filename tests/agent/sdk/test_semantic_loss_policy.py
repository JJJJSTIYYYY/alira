"""Apply Chat compensation only to the resolved Chat request direction."""

import json

import httpx
import pytest
from my_llm import (
    AssistantMessage,
    Attribute,
    Config,
    ImageContent,
    ImageSource,
    ModelProfile,
    ProviderProfile,
    ReasoningContent,
    TextContent,
    ToolCall,
    ToolMessage,
    UserMessage,
)
from my_llm.codec.openai.chat.prebuilt import chat_semantic_loss_hook

from alira.agent.core.bot import BaseProvider
from tests.agent.sdk.test_bot import make_profile_bot, weather
from tests.agent.sdk.test_bot_protocols import install_sdk, wire


@pytest.mark.parametrize(
    "provider,model,expected",
    [
        ("moonshot", "kimi-k2.6", "hook"),
        ("openai", "gpt-6-astra", "ignore"),
        ("deepseek", "deepseek-flash", "ignore"),
        ("anthropic", "claude-opus-5-5", "ignore"),
    ],
)
def test_policy_uses_resolved_model_protocol(provider, model, expected):
    llm = BaseProvider.get(provider).create_llm(model)
    adaptor = llm.codec.semantic_loss_adaptor
    assert llm.stdio_protocol == "middleware"
    assert adaptor.semantic_loss_protocol == expected
    if expected == "hook":
        assert adaptor.input_semantic_loss_hook is chat_semantic_loss_hook
        assert adaptor.output_semantic_loss_hook is not chat_semantic_loss_hook


async def test_chat_hook_compensates_visible_reasoning_and_tool_images(monkeypatch):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, text=wire("openai_chat"), headers={"content-type": "text/event-stream"}
        )

    install_sdk(monkeypatch, "openai_chat", handler)
    bot = make_profile_bot(
        provider=ProviderProfile(protocol=Config(default_value="openai_responses")),
        model=ModelProfile(
            model=Attribute(default_value="test"),
            protocol=Config(default_value="openai_chat"),
            supports_vision=Config(default_value=True),
            supports_function_calling=Config(default_value=True),
        ),
        api_key="test",
    )
    source = ImageSource("base64", data="YWJj", media_type="image/png")
    assistant = AssistantMessage(
        role="assistant",
        protocol="openai_responses",
        content=[
            ReasoningContent(
                "visible",
                feedback_item={"type": "reasoning", "encrypted_content": "opaque"},
            ),
            ToolCall("c1", "weather", {"city": "Tokyo"}),
        ],
    )
    result = ToolMessage(
        role="tool", call_id="c1", content=[TextContent("image:"), ImageContent(source)]
    )
    async with bot:
        await bot.invoke(
            [
                UserMessage(role="user", content=[TextContent("look")]),
                assistant,
                result,
            ],
            tools=[weather],
        )
    sent = requests[0]["messages"]
    assert sent[1]["reasoning_content"] == "visible"
    assert "opaque" not in json.dumps(sent)
    assert sent[2]["role"] == "tool" and sent[2]["tool_call_id"] == "c1"
    reference = sent[-1]["content"][0]["text"].strip()
    assert reference in sent[2]["content"]
    assert sent[-1]["role"] == "user"
    assert sent[-1]["content"][1]["image_url"]["url"] == "data:image/png;base64,YWJj"
    assert assistant.content[0].feedback_item["encrypted_content"] == "opaque"
    assert result.content[1].source is source


@pytest.mark.parametrize(
    "provider,model", [("deepseek", "deepseek-flash"), ("anthropic", "claude-opus-5-5")]
)
async def test_sdk_reasoning_survives_database_and_cache_restart(
    tmp_path, monkeypatch, provider, model
):
    from alira.agent.core.bot import AliraBot
    from alira.database.services.cache import CacheService
    from alira.database.services.data import DataService
    from alira.shared.message import AliraMessageExtension, AliraUserMessage
    from tests.agent.sdk.test_provider_presets import reasoning_wire
    from tests.agent.store.services.data.test_data_service import (
        create_user_and_conversation,
    )

    bot = AliraBot(provider=provider, model=model, api_key="test")
    protocol = bot.llm.resolved_profile.protocol
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            text=reasoning_wire(protocol, raw=provider == "deepseek").replace(
                '"signature": ""', '"signature": "signed"'
            ),
            headers={"content-type": "text/event-stream"},
        )

    install_sdk(monkeypatch, protocol, handler)
    db = DataService(tmp_path / "sdk.sqlite3")
    cache = CacheService(tmp_path / "cache.json")
    await db.start()
    await cache.start()
    try:
        await create_user_and_conversation(db, monkeypatch)
        payload = {"user_uid": "user-1", "conversation_uid": "conv-1"}
        async with bot:
            user = AliraUserMessage(content=[TextContent("hi")])
            answer = await bot.invoke([user])
            record = answer.to_record()
            assert (await db.append_message({**payload, "message": record}))["success"]
            assert (await cache.set_messages({**payload, "messages": [record]}))[
                "success"
            ]
            await db.stop()
            await db.start()
            await cache.stop()
            await cache.start()
            row = (await db.list_messages(payload))["messages"][0]
            cached = (await cache.get_messages(payload))["messages"][0]
            restored = AliraMessageExtension.from_record(row)
            assert restored.to_wire() == answer.to_wire()
            assert (
                AliraMessageExtension.from_record(cached).to_wire() == answer.to_wire()
            )
            await bot.invoke(
                [
                    user,
                    restored,
                    UserMessage(role="user", content=[TextContent("continue")]),
                ]
            )
            assert "visible reasoning" in json.dumps(requests[1])
    finally:
        await db.stop()
        await cache.stop()

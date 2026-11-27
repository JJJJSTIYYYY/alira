"""Alira persistence retains generated reasoning across database and cache restarts."""

import json

import httpx
import pytest
from my_llm import TextContent, UserMessage

from tests.modules.agent.src.sdk.test_bot_protocols import install_sdk


@pytest.mark.parametrize(
    "provider,model", [("deepseek", "deepseek-flash"), ("anthropic", "claude-opus-5-5")]
)
async def test_sdk_reasoning_survives_database_and_cache_restart(
    tmp_path, monkeypatch, provider, model
):
    from alira.modules.agent.src.core.bot import AliraBot
    from alira.modules.database.src.services.cache import CacheService
    from alira.modules.database.src.services.data import DataService
    from alira.modules.agent.src.shared.model.message import (
        AliraMessageExtension,
        AliraUserMessage,
    )
    from tests.modules.agent.src.sdk.test_provider_presets import reasoning_wire
    from tests.modules.database.src.services.data.test_data_service import (
        create_user_and_conversation,
    )

    bot = AliraBot(provider=provider, model=model)
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
        user = AliraUserMessage(content=[TextContent("hi")])
        answer = await bot.invoke([user], api_key="test")
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
            ],
            api_key="test",
        )
        assert "visible reasoning" in json.dumps(requests[1])
    finally:
        await db.stop()
        await cache.stop()

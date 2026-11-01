"""Persistence of canonical Chat messages without application adapters."""

import pytest

from alira.agent.core.utils.message import (
    AliraAiMessage,
    AliraSystemMessage,
    AliraToolMessage,
    AliraUserMessage,
)
from alira.database.services.data import DataService
from tests.agent.store.services.data.test_data_service import (
    create_user_and_conversation,
)


@pytest.fixture
async def db(tmp_path):
    service = DataService(tmp_path / "messages.sqlite3")
    await service.start()
    try:
        yield service
    finally:
        await service.stop()


@pytest.mark.parametrize(
    "content",
    [
        None,
        "",
        "null",
        '[{"type":"text"}]',
        [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}},
        ],
    ],
)
async def test_sqlite_content_and_protocol_fields_round_trip(
    tmp_path, monkeypatch, content
):
    db = DataService(tmp_path / "chat.sqlite3")
    await db.start()
    try:
        await create_user_and_conversation(db, monkeypatch)
        call = {
            "id": "c1",
            "type": "function",
            "function": {"name": "weather", "arguments": '{"city":"上海"}'},
        }
        message = AliraAiMessage(
            content=content,
            tool_calls=[call],
            refusal="refusal",
            reasoning_content="why",
            audio={"id": "audio-1"},
            annotations=[],
            metadata={"usage": {"total_tokens": 3}},
            extensions={"uploaded_files": ["brief.pdf"], "custom": {"x": 1}},
        )
        tool = AliraToolMessage(
            content="",
            tool_call_id="c1",
            extensions={"active_file": "notes.md"},
        )
        payload = {"user_uid": "user-1", "conversation_uid": "conv-1"}
        for current, node, parent in [
            (message, "assistant-node", "-"),
            (tool, "tool-node", "assistant-node"),
        ]:
            stored = {
                **current.to_dict(),
                "generation_id": "generation",
                "node_id": node,
                "parent_id": parent,
            }
            result = await db.append_message({**payload, "message": stored})
            assert result["success"]
            assert "timestamp" in result["messages"]
        await db.stop()
        await db.start()
        fetched = await db.list_messages(payload)
        assert fetched["success"]
        assert len(fetched["messages"]) == 2
        for original, row in zip((message, tool), fetched["messages"], strict=True):
            assert {key: row[key] for key in original.to_dict()} == original.to_dict()
            assert row["generation_id"] == "generation"
            assert row["timestamp"]
            assert not hasattr(original, "timestamp")
        assert fetched["messages"][0]["parent_id"] == "-"
        assert fetched["messages"][1]["parent_id"] == "assistant-node"
    finally:
        await db.stop()


async def test_sqlite_preserves_multimodal_user_and_system_messages(db, monkeypatch):
    await create_user_and_conversation(db, monkeypatch)
    parts = [{"type": "image_url", "image_url": {"url": "https://example.test/a.png"}}]
    messages = [
        AliraSystemMessage(content="rules"),
        AliraUserMessage(content=parts, extensions={"active_file": "notes.md"}),
    ]
    payload = {"user_uid": "user-1", "conversation_uid": "conv-1"}
    for message in messages:
        assert (await db.append_message({**payload, "message": message.to_dict()}))[
            "success"
        ]
    fetched = await db.list_messages(payload)
    assert fetched["success"]
    assert [row["role"] for row in fetched["messages"]] == ["system", "user"]
    assert fetched["messages"][0]["content"] == "rules"
    assert fetched["messages"][1]["content"] == parts
    assert fetched["messages"][1]["extensions"] == {"active_file": "notes.md"}
    assert parts == [
        {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}}
    ]

"""Persist typed middleware messages, identities and business fields."""

import pytest
from my_llm import (
    ImageContent,
    ImageSource,
    ItemIdentity,
    Origin,
    ReasoningContent,
    TextContent,
    ToolCall,
    ToolMessage,
)

from alira.agent.core.utils.message import (
    AliraAiMessage,
    AliraSystemMessage,
    AliraToolMessage,
    AliraUserMessage,
)
from alira.common.utils.message import message_from_record
from alira.database.services.data import DataService
from tests.agent.store.services.data.test_data_service import (
    create_user_and_conversation,
)


@pytest.mark.parametrize("text", ["", "null", '[{"type":"text"}]', "上海"])
async def test_sqlite_content_and_protocol_fields_round_trip(
    tmp_path, monkeypatch, text
):
    db = DataService(tmp_path / "middleware.sqlite3")
    await db.start()
    try:
        await create_user_and_conversation(db, monkeypatch)
        feedback = {
            "type": "reasoning",
            "id": "r1",
            "summary": [],
            "encrypted_content": "opaque",
        }
        original = [
            AliraUserMessage(content=[TextContent(text)]),
            AliraAiMessage(
                protocol="openai_responses",
                item=ItemIdentity("m1", "completed"),
                origin=Origin("$.output", {"id": "m1"}),
                content=[
                    ReasoningContent(None, feedback_item=feedback),
                    TextContent(text),
                    ToolCall("c1", "weather", {"city": "上海"}),
                ],
                metadata={"usage": {"total": 3}},
                extensions={"custom": {"x": 1}},
            ),
            AliraToolMessage(
                call_id="c1",
                name="weather",
                content=[
                    TextContent(text),
                    ImageContent(
                        ImageSource("base64", data="YWJj", media_type="image/png")
                    ),
                ],
            ),
            AliraAiMessage(content=[TextContent("done")]),
        ]
        payload = {"user_uid": "user-1", "conversation_uid": "conv-1"}
        for index, message in enumerate(original):
            message.generation_id = "generation-1"
            message.node_id = f"node-{index}"
            message.parent_id = f"node-{index - 1}" if index else "-"
            assert (await db.append_message({**payload, "message": message.to_dict()}))[
                "success"
            ]
        await db.stop()
        await db.start()
        result = await db.list_messages(payload)
        assert result["success"]
        for message, row in zip(original, result["messages"], strict=True):
            assert {key: row[key] for key in message.to_dict()} == message.to_dict()
            restored = message_from_record(row)
            assert restored.to_wire() == message.to_wire()
            assert "id" not in row and "conversation_id" not in row
            assert row["conversation_uid"] == "conv-1" and row["user_uid"] == "user-1"
            assert row["generation_id"] == "generation-1"
        assert isinstance(message_from_record(result["messages"][2]), ToolMessage)
        assert result["messages"][2]["call_id"] == "c1"
        assert result["messages"][2]["role"] == "tool"
    finally:
        await db.stop()


async def test_sqlite_preserves_multimodal_user_and_instruction_messages(
    tmp_path, monkeypatch
):
    db = DataService(tmp_path / "images.sqlite3")
    await db.start()
    try:
        await create_user_and_conversation(db, monkeypatch)
        original = [
            AliraSystemMessage(content=[TextContent("rules")]),
            AliraSystemMessage(role="developer", content=[TextContent("policy")]),
            AliraUserMessage(
                content=[
                    ImageContent(ImageSource("url", url="https://example.test/a.png"))
                ]
            ),
        ]
        payload = {"user_uid": "user-1", "conversation_uid": "conv-1"}
        for message in original:
            assert (await db.append_message({**payload, "message": message.to_dict()}))[
                "success"
            ]
        rows = (await db.list_messages(payload))["messages"]
        assert [message_from_record(row).to_wire() for row in rows] == [
            message.to_wire() for message in original
        ]
    finally:
        await db.stop()


@pytest.mark.parametrize("role,call_id", [("tool", None), ("tool", ""), ("user", "c1")])
async def test_storage_rejects_mismatched_tool_identity(
    tmp_path, monkeypatch, role, call_id
):
    db = DataService(tmp_path / "invalid-role.sqlite3")
    await db.start()
    try:
        await create_user_and_conversation(db, monkeypatch)
        payload = {"user_uid": "user-1", "conversation_uid": "conv-1"}
        result = await db.append_message(
            {
                **payload,
                "message": {
                    "message_uid": "invalid",
                    "role": role,
                    "call_id": call_id,
                    "content": [{"type": "text", "text": "result"}],
                },
            }
        )
        assert result["success"] is False
        assert (await db.list_messages(payload))["messages"] == []
        assert (await db.get_conversation(payload))["messages"][0]["latest_cursor"] == 0
    finally:
        await db.stop()

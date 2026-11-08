"""Shared business messages preserve native and application fields separately."""

import pytest
from my_llm import (
    AssistantMessage,
    InstructionMessage,
    ItemIdentity,
    Origin,
    TextContent,
    ToolMessage,
    UserMessage,
)

from alira.shared.models.message import (
    AliraAssistantMessage,
    AliraMessageExtension,
    AliraSystemMessage,
    AliraToolMessage,
    AliraUserMessage,
)


@pytest.mark.parametrize(
    "native_type,message_type,role,tool_fields",
    [
        (InstructionMessage, AliraSystemMessage, "system", {}),
        (UserMessage, AliraUserMessage, "user", {}),
        (AssistantMessage, AliraAssistantMessage, "assistant", {}),
        (ToolMessage, AliraToolMessage, "tool", {"call_id": "call-1"}),
    ],
)
def test_from_middleware_preserves_native_fields_without_copying(
    native_type, message_type, role, tool_fields
):
    native = native_type(
        role=role,
        content=[TextContent("上海")],
        name="sender",
        protocol="openai_responses",
        item=ItemIdentity("item-1", "completed"),
        origin=Origin("$.output", {"id": "item-1"}),
        **tool_fields,
    )
    message = message_type.from_middleware(
        native,
        user_uid="user-1",
        conversation_uid="conversation-1",
        extensions={"uploaded_files": ["brief.pdf"]},
    )
    assert type(message) is message_type
    assert message.to_wire() == native.to_wire()
    assert message.content is native.content
    assert message.item is native.item
    assert message.origin is native.origin
    assert message.user_uid == "user-1"
    assert message.conversation_uid == "conversation-1"
    assert message.uploaded_files == ["brief.pdf"]
    if tool_fields:
        assert message.call_id == "call-1"


@pytest.mark.parametrize(
    "message_type,role,tool_fields",
    [
        (AliraUserMessage, "user", {}),
        (AliraAssistantMessage, "assistant", {}),
        (AliraToolMessage, "tool", {"call_id": "call-1"}),
    ],
)
def test_record_round_trip_restores_business_message(message_type, role, tool_fields):
    business_fields = {
        "message_uid": "message-1",
        "user_uid": "user-1",
        "conversation_uid": "conversation-1",
        "generation_id": "generation-1",
        "node_id": "node-1",
        "parent_id": "root",
        "metadata": {"usage": {"total_tokens": 3}},
        "extensions": {"uploaded_files": ["brief.pdf"]},
    }
    message = message_type(
        role=role,
        content=[TextContent("上海")],
        name="sender",
        protocol="openai_responses",
        item=ItemIdentity("item-1", "completed"),
        origin=Origin("$.output", {"id": "item-1"}),
        **business_fields,
        **tool_fields,
    )

    record = message.to_record()
    assert {key: record[key] for key in business_fields} == business_fields
    assert record["content"] == [{"type": "text", "text": "上海"}]
    assert record["call_id"] == tool_fields.get("call_id")
    assert not business_fields.keys() & message.to_wire().keys()

    # Storage-only fields do not become model fields or protocol content.
    storage_fields = {
        "id": 1,
        "conversation_id": 2,
        "msg_cursor": 42,
        "timestamp": "2026-11-08T10:00:00",
        "is_deleted": 0,
    }
    restored = AliraMessageExtension.from_record({**record, **storage_fields})
    assert type(restored) is message_type
    assert restored.to_record() == record
    assert restored.to_wire() == message.to_wire()
    assert restored.item == message.item
    assert restored.origin == message.origin
    assert restored.uploaded_files == ["brief.pdf"]
    assert all(not hasattr(restored, key) for key in storage_fields)


@pytest.mark.parametrize("source", ["record", "middleware"])
def test_business_message_factory_rejects_developer_role(source):
    with pytest.raises(ValueError, match="unsupported message role: 'developer'"):
        if source == "record":
            AliraMessageExtension.from_record({"role": "developer", "content": []})
        else:
            AliraMessageExtension.from_middleware(InstructionMessage("developer"))


@pytest.mark.parametrize("operation", ["serialize", "restore"])
def test_system_message_is_runtime_only(operation):
    message = AliraSystemMessage(content=[TextContent("rules")])
    assert message.to_wire() == {
        "role": "system",
        "content": [{"type": "text", "text": "rules"}],
    }
    with pytest.raises(ValueError, match="system messages are runtime-only"):
        if operation == "serialize":
            message.to_record()
        else:
            AliraMessageExtension.from_record(message.to_wire())


@pytest.mark.parametrize("source", ["record", "middleware"])
def test_concrete_message_factory_rejects_a_different_role(source):
    with pytest.raises(ValueError, match="AliraUserMessage does not support role"):
        if source == "record":
            AliraUserMessage.from_record({"role": "assistant", "content": []})
        else:
            AliraUserMessage.from_middleware(AssistantMessage("assistant"))


def test_new_and_restored_messages_have_independent_business_defaults():
    first = AliraUserMessage()
    second = AliraUserMessage.from_record({"role": "user", "content": []})
    assert isinstance(second, AliraUserMessage)
    assert first.message_uid and second.message_uid
    assert first.message_uid != second.message_uid
    assert first.user_uid == second.user_uid == ""
    assert first.conversation_uid == second.conversation_uid == ""
    assert first.generation_id == second.generation_id == ""
    assert first.node_id == second.node_id == ""
    assert first.parent_id == second.parent_id == ""
    first.metadata["duration"] = 1
    first.uploaded_files = ["brief.pdf"]
    first.content.append(TextContent("question"))
    assert second.metadata == {}
    assert second.extensions == {}
    assert second.content == []

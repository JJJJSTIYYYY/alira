"""Flat persistence records for my-llm-py middleware messages."""

from typing import Any

from my_llm import ItemIdentity, Message, Origin, ToolMessage
from my_llm.middleware.models import messages_from_wire


def message_record(message: Message) -> dict[str, Any]:
    """Keep ToolMessage identity separate from its ordered result content."""
    return {
        "role": message.role,
        "content": [part.to_wire() for part in message.content],
        "name": message.name,
        "protocol": message.protocol,
        "item": message.item.to_wire() if message.item is not None else None,
        "origin": {"path": message.origin.path, "value": message.origin.value}
        if message.origin is not None
        else None,
        "call_id": message.call_id if isinstance(message, ToolMessage) else None,
    }


def message_from_record(record: dict[str, Any]) -> Message:
    """Restore a database record through the library's middleware parser."""
    if (record["role"] == "tool") != (record.get("call_id") is not None):
        raise ValueError("the tool role requires call_id and vice versa")
    wire = {
        key: record[key]
        for key in ("role", "content", "name", "protocol")
        if record.get(key) is not None
    }
    if record["role"] == "tool":
        wire["content"] = [
            {
                "type": "tool_result",
                "tool_use_id": record["call_id"],
                "content": record["content"],
                "name": record.get("name"),
            }
        ]
    (message,) = messages_from_wire([wire])
    message.item = (
        ItemIdentity(**record["item"]) if record.get("item") is not None else None
    )
    message.origin = (
        Origin(**record["origin"]) if record.get("origin") is not None else None
    )
    return message

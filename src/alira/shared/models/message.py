"""Middleware messages with application identity and flat persistence records."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, Literal, Self, TypeAlias
from uuid import uuid4

from my_llm import (
    AssistantMessage,
    InstructionMessage,
    ItemIdentity,
    Message,
    Origin,
    ToolMessage,
    UserMessage,
)
from my_llm.middleware.models import messages_from_wire


@dataclass(kw_only=True)
class AliraMessageExtension:
    """Application fields shared by the native middleware message subclasses."""

    # Concrete message dataclasses allocate these slots alongside native fields.
    __slots__ = ()

    message_uid: str = field(default_factory=lambda: uuid4().hex)
    user_uid: str = ""
    conversation_uid: str = ""
    generation_id: str = ""
    node_id: str = ""
    parent_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    extensions: dict[str, Any] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        """Project native and business fields, keeping tool identity outside content."""
        return {
            "role": self.role,
            "content": [part.to_wire() for part in self.content],
            "name": self.name,
            "protocol": self.protocol,
            "item": self.item.to_wire() if self.item is not None else None,
            "origin": {"path": self.origin.path, "value": self.origin.value}
            if self.origin is not None
            else None,
            "call_id": self.call_id if isinstance(self, ToolMessage) else None,
            **{f.name: getattr(self, f.name) for f in fields(AliraMessageExtension)},
        }

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> Self:
        """Restore a database record through the library's middleware parser."""
        (message,) = messages_from_wire([message_wire_from_record(record)])
        message.item = (
            ItemIdentity(**record["item"]) if record.get("item") is not None else None
        )
        message.origin = (
            Origin(**record["origin"]) if record.get("origin") is not None else None
        )
        return cls.from_middleware(
            message,
            **{
                f.name: record[f.name]
                for f in fields(AliraMessageExtension)
                if f.name in record
            },
        )

    @classmethod
    def from_middleware(cls, message: Message, **application_fields: Any) -> Self:
        """Restore the matching Alira type while borrowing native content."""
        message_class = _MESSAGE_CLASSES_BY_ROLE.get(message.role)
        if message_class is None:
            raise ValueError(f"unsupported message role: {message.role!r}")
        if cls is not AliraMessageExtension:
            if not issubclass(cls, message_class):
                raise ValueError(
                    f"{cls.__name__} does not support role {message.role!r}"
                )
            message_class = cls
        native_type = ToolMessage if isinstance(message, ToolMessage) else Message
        return message_class(
            **{f.name: getattr(message, f.name) for f in fields(native_type)},
            **application_fields,
        )

    @property
    def uploaded_files(self) -> list[str]:
        value = self.extensions.get("uploaded_files")
        return value if isinstance(value, list) else []

    @uploaded_files.setter
    def uploaded_files(self, value: list[str]) -> None:
        self.extensions["uploaded_files"] = value

    @property
    def active_file(self) -> str | None:
        value = self.extensions.get("active_file")
        return value if isinstance(value, str) else None

    @active_file.setter
    def active_file(self, value: str | None) -> None:
        if value is None:
            self.extensions.pop("active_file", None)
        else:
            self.extensions["active_file"] = value

    @property
    def referenced_message(self) -> dict[str, Any] | None:
        value = self.extensions.get("referenced_message")
        return value if isinstance(value, dict) else None

    @referenced_message.setter
    def referenced_message(self, value: dict[str, Any] | None) -> None:
        if value is None:
            self.extensions.pop("referenced_message", None)
        else:
            self.extensions["referenced_message"] = value

    @property
    def task(self) -> dict[str, Any] | None:
        value = self.extensions.get("task")
        return value if isinstance(value, dict) else None

    @task.setter
    def task(self, value: dict[str, Any] | None) -> None:
        if value is None:
            self.extensions.pop("task", None)
        else:
            self.extensions["task"] = value

    @property
    def todo_list(self) -> list[dict[str, Any]]:
        value = self.extensions.get("todo_list")
        return value if isinstance(value, list) else []

    @todo_list.setter
    def todo_list(self, value: list[dict[str, Any]]) -> None:
        self.extensions["todo_list"] = value

    @property
    def system_instruction(self) -> list[str]:
        value = self.extensions.get("system_instruction")
        return value if isinstance(value, list) else []

    @system_instruction.setter
    def system_instruction(self, value: list[str]) -> None:
        self.extensions["system_instruction"] = value


@dataclass(slots=True)
class AliraSystemMessage(InstructionMessage, AliraMessageExtension):
    """Runtime-only system instruction with application fields."""

    role: Literal["system"] = "system"

    def to_record(self) -> dict[str, Any]:
        raise ValueError("system messages are runtime-only and cannot be persisted")


@dataclass(slots=True)
class AliraUserMessage(UserMessage, AliraMessageExtension):
    """User message bound to an Alira DB record."""

    role: Literal["user"] = "user"


@dataclass(slots=True)
class AliraAssistantMessage(AssistantMessage, AliraMessageExtension):
    """Assistant message bound to an Alira DB record."""

    role: Literal["assistant"] = "assistant"


@dataclass(slots=True)
class AliraToolMessage(ToolMessage, AliraMessageExtension):
    """Tool message bound to an Alira DB record."""

    role: Literal["tool"] = "tool"


AliraMessage: TypeAlias = (
    AliraSystemMessage | AliraUserMessage | AliraAssistantMessage | AliraToolMessage
)
_MESSAGE_CLASSES_BY_ROLE = {
    "system": AliraSystemMessage,
    "user": AliraUserMessage,
    "assistant": AliraAssistantMessage,
    "tool": AliraToolMessage,
}


def message_wire_from_record(record: dict[str, Any]) -> dict[str, Any]:
    """Project a flat record to middleware wire fields without copying content."""
    role = record["role"]
    if role == "system":
        raise ValueError("system messages are runtime-only and cannot be persisted")
    if role not in _MESSAGE_CLASSES_BY_ROLE:
        raise ValueError(f"unsupported message role: {role!r}")
    call_id = record.get("call_id")
    if (role == "tool") != (call_id is not None):
        raise ValueError("the tool role requires call_id and vice versa")
    item = record.get("item")
    if item is not None and (
        not isinstance(item, dict) or not item.keys() <= {"id", "status"}
    ):
        raise ValueError("message item must contain only id and status")
    origin = record.get("origin")
    if origin is not None and (
        not isinstance(origin, dict) or origin.keys() != {"path", "value"}
    ):
        raise ValueError("message origin requires path and value")
    wire = {
        key: record[key]
        for key in ("role", "content", "name", "protocol", "item")
        if record.get(key) is not None
    }
    if role == "tool":
        if not isinstance(call_id, str) or not call_id:
            raise ValueError("the tool role requires a nonempty call_id")
        wire["content"] = [
            {
                "type": "tool_result",
                "tool_use_id": call_id,
                "content": record["content"],
                "name": record.get("name"),
            }
        ]
    return wire


__all__ = [
    "AliraAssistantMessage",
    "AliraMessage",
    "AliraMessageExtension",
    "AliraSystemMessage",
    "AliraToolMessage",
    "AliraUserMessage",
    "message_wire_from_record",
]

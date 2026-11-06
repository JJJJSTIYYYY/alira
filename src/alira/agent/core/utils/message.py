"""Middleware messages with application identity and business extensions."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, Literal, TypeAlias
from uuid import uuid4

from my_llm import (
    AssistantMessage,
    InstructionMessage,
    Message,
    ToolMessage,
    UserMessage,
)

from alira.common.utils.message import message_record


@dataclass(kw_only=True)
class AliraMessageBase(Message):
    role: str = "user"
    message_uid: str = field(default_factory=lambda: uuid4().hex)
    generation_id: str = ""
    node_id: str = ""
    parent_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    extensions: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Project flat middleware fields and application fields for storage."""
        return {
            **message_record(self),
            **{
                key: getattr(self, key)
                for key in (
                    "message_uid",
                    "generation_id",
                    "node_id",
                    "parent_id",
                    "metadata",
                    "extensions",
                )
            },
        }

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


@dataclass(kw_only=True)
class AliraAiMessage(AliraMessageBase, AssistantMessage):
    role: str = "assistant"

    @classmethod
    def from_middleware(
        cls, message: AssistantMessage, **application_fields: Any
    ) -> AliraAiMessage:
        """Attach business fields to the library's completed assistant message."""
        return cls(
            **{f.name: getattr(message, f.name) for f in fields(Message)},
            **application_fields,
        )


@dataclass(kw_only=True)
class AliraSystemMessage(AliraMessageBase, InstructionMessage):
    role: str = "system"


@dataclass(kw_only=True)
class AliraToolMessage(AliraMessageBase, ToolMessage):
    role: Literal["tool"] = "tool"


@dataclass(kw_only=True)
class AliraUserMessage(AliraMessageBase, UserMessage):
    role: str = "user"


CompleteMessage: TypeAlias = (
    AliraUserMessage | AliraAiMessage | AliraToolMessage | AliraSystemMessage
)
AnyMessage: TypeAlias = CompleteMessage

"""Shared schema and call contract for function and MCP tools."""

import json
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import Any

from apix.agent.core.utils.message import ToolCall

ToolFunction = Callable[..., Any] | Callable[..., Awaitable[Any]]


class BaseTool(ABC):
    """A model-facing definition with one executable call contract."""

    name: str
    description: str
    schema: dict[str, Any]

    @abstractmethod
    async def execute(self, state: dict[str, Any], tool_call: ToolCall) -> Any:
        """Validate and execute one selected call."""

    def get_schema(self) -> dict[str, Any]:
        """Return a copy safe to pass in the OpenAI ``tools`` list."""
        return deepcopy(self.schema)

    @staticmethod
    def _validate_tool_call(
        tool_call: ToolCall,
    ) -> None:
        """Validate the basic runtime structure of a tool call."""
        if not isinstance(tool_call, dict):
            raise TypeError("tool_call must be a ToolCall-compatible dictionary.")

        if not {"id", "type", "function"} <= tool_call.keys():
            raise ValueError("ToolCall is missing required fields: id, type, function")
        if tool_call.get("type") != "function":
            raise ValueError("ToolCall.type must be function")
        if not isinstance(tool_call.get("id"), str) or not tool_call["id"]:
            raise TypeError("ToolCall.id must be a non-empty string")
        function = tool_call.get("function")
        if not isinstance(function, dict):
            raise TypeError("ToolCall.function must be a dictionary")
        if not isinstance(function.get("name"), str) or not function["name"]:
            raise TypeError("ToolCall.function.name must be a non-empty string")
        BaseTool._tool_arguments(tool_call)

    @staticmethod
    def _tool_arguments(tool_call: ToolCall) -> dict[str, Any]:
        raw = tool_call["function"].get("arguments")
        if not isinstance(raw, str):
            raise TypeError("ToolCall.function.arguments must be a JSON string")
        arguments = json.loads(raw)
        if not isinstance(arguments, dict):
            raise TypeError("ToolCall.function.arguments must decode to an object")
        return arguments

    def _validate_call(self, state: dict[str, Any], tool_call: ToolCall) -> None:
        """Validate the shared local and MCP execution contract."""
        if not isinstance(state, dict):
            raise TypeError("state must be a dictionary.")

        self._validate_tool_call(tool_call)

        called_tool_name = tool_call["function"]["name"]

        if called_tool_name != self.name:
            raise ValueError(
                f"ToolCall targets {called_tool_name!r}, "
                f"but this tool is {self.name!r}."
            )

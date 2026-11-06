"""Shared schema and call contract for function and MCP tools."""

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import Any

from my_llm import FunctionTool, ToolCall

ToolFunction = Callable[..., Any] | Callable[..., Awaitable[Any]]


class BaseTool(ABC):
    """A model-facing definition with one executable call contract."""

    name: str
    description: str
    schema: FunctionTool

    @abstractmethod
    async def execute(self, state: dict[str, Any], tool_call: ToolCall) -> Any:
        """Validate and execute one selected call."""

    def get_schema(self) -> FunctionTool:
        """Return an independent middleware function definition."""
        return deepcopy(self.schema)

    @staticmethod
    def _validate_tool_call(
        tool_call: ToolCall,
    ) -> None:
        """Validate calls at the tool execution boundary."""
        if not isinstance(tool_call, ToolCall):
            raise TypeError("tool_call must be a middleware ToolCall")
        if not isinstance(tool_call.call_id, str) or not tool_call.call_id:
            raise ValueError("ToolCall.call_id must be a non-empty string")
        if not isinstance(tool_call.name, str) or not tool_call.name:
            raise ValueError("ToolCall.name must be a non-empty string")
        if not isinstance(tool_call.arguments, dict):
            raise TypeError("ToolCall.arguments must be an object")

    def _validate_call(self, state: dict[str, Any], tool_call: ToolCall) -> None:
        """Validate the shared local and MCP execution contract."""
        if not isinstance(state, dict):
            raise TypeError("state must be a dictionary.")

        self._validate_tool_call(tool_call)

        called_tool_name = tool_call.name

        if called_tool_name != self.name:
            raise ValueError(
                f"ToolCall targets {called_tool_name!r}, "
                f"but this tool is {self.name!r}."
            )

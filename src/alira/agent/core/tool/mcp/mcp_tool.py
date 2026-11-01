"""Adapt one remote tool definition without importing function-tool machinery."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from alira.agent.core.tool.base import BaseTool
from alira.agent.core.tool.mcp.base import (
    McpMetaSchema,
    _get_model_value,
)
from alira.agent.core.utils.message import ToolCall
from alira.interface.utils.identiity import ConversationIdentity

if TYPE_CHECKING:
    from alira.agent.core.tool.mcp.mcp_tool_manager import MCPToolManager


class MCPTool(BaseTool):
    """A remote tool bound to one manager, server and conversation identity."""

    def __init__(
        self,
        definition: Any,
        manager: MCPToolManager,
        mcp_meta: McpMetaSchema,
        identity: ConversationIdentity,
    ) -> None:
        name = _get_model_value(definition, "name")
        if not isinstance(name, str) or not name:
            raise ValueError("An MCP tool requires a non-empty name.")
        description = _get_model_value(definition, "description")
        if not description:
            description = _get_model_value(definition, "title", default="")
        input_schema = _get_model_value(
            definition,
            "input_schema",
            "inputSchema",
            default={},
        )
        if not isinstance(input_schema, Mapping):
            raise TypeError(f"MCP tool {name!r} input schema must be a mapping.")

        self.name = name
        self.description = str(description or "")
        self.mcp_id = mcp_meta["mcp_id"]
        self.mcp_name = mcp_meta["mcp_name"]
        self._manager = manager
        self._identity_key = manager.identity_key(identity)
        self.schema = {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": deepcopy(dict(input_schema)),
            },
        }

    @property
    def manager(self) -> MCPToolManager:
        """Return the background manager that owns this tool's execution."""
        return self._manager

    def validate_call(self, state: dict[str, Any], tool_call: ToolCall) -> None:
        """Reject cross-identity calls before routing to a client runtime."""
        self._validate_call(state, tool_call)
        if self._manager.identity_key(state.get("target")) != self._identity_key:
            raise ValueError("MCP tool identity does not match state['target'].")

    async def execute(self, state: dict[str, Any], tool_call: ToolCall) -> str:
        self.validate_call(state, tool_call)
        return await self._manager.execute_tool(self, state, tool_call)


__all__ = ["MCPTool"]

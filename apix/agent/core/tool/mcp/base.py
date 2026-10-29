"""Configuration and result conversion for the FastMCP integration."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Literal, TypedDict

MCPTransport = Literal["stdio", "http", "streamable_http", "sse"]


class McpMetaSchema(TypedDict):
    """Stored connection configuration for one MCP server."""

    mcp_id: str
    mcp_name: str
    transport: MCPTransport
    # A command for stdio, or a URL for network transports.
    endpoint: str
    config: dict[str, Any]


class MCPToolError(RuntimeError):
    """Raised when an MCP server returns a failed tool result."""


def _get_model_value(model: Any, *names: str, default: Any = None) -> Any:
    """Read the first available Python or wire-format model attribute."""
    for name in names:
        if isinstance(model, Mapping) and name in model:
            return model[name]
        if hasattr(model, name):
            return getattr(model, name)
    return default


def _dump_content_block(content: Any) -> str:
    """Serialize a non-text MCP content block with its metadata."""
    if hasattr(content, "model_dump"):
        value = content.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=True,
        )
    elif isinstance(content, Mapping):
        value = dict(content)
    else:
        return str(content)
    return json.dumps(value, ensure_ascii=False, default=str)


def _convert_call_tool_result(result: Any) -> str:
    """Convert a FastMCP call result into model-readable text."""
    output_parts: list[str] = []
    for content in _get_model_value(result, "content", default=[]) or []:
        if _get_model_value(content, "type") == "text":
            output_parts.append(str(_get_model_value(content, "text", default="")))
        else:
            output_parts.append(_dump_content_block(content))

    structured_content = _get_model_value(
        result,
        "structured_content",
        "structuredContent",
    )
    if not output_parts and structured_content is not None:
        output_parts.append(
            json.dumps(structured_content, ensure_ascii=False, default=str)
        )

    data = _get_model_value(result, "data")
    if not output_parts and data is not None:
        output_parts.append(
            data
            if isinstance(data, str)
            else json.dumps(data, ensure_ascii=False, default=str)
        )

    output = "\n".join(output_parts)
    if _get_model_value(result, "is_error", "isError", default=False):
        raise MCPToolError(output or "MCP tool execution failed.")
    return output


__all__ = ["MCPToolError", "MCPTransport", "McpMetaSchema"]

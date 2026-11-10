"""MCP metadata discovery consumes the database result envelope."""

from unittest.mock import AsyncMock

import pytest

from alira.modules.tool.src.mcp import mcp_tool_manager as manager_module
from alira.modules.tool.src.mcp.mcp_tool_manager import MCPToolManager


@pytest.mark.parametrize(("success", "code"), [(True, 0), (False, 3002), (True, 2001)])
async def test_get_mcp_meta_handles_structured_database_results(
    monkeypatch, success, code
):
    rows = [{"mcp_id": "server-1"}]
    query = AsyncMock(
        return_value={
            "success": success,
            "code": code,
            "messages": rows if code == 0 else "Standard error message.",
        }
    )
    monkeypatch.setattr(manager_module, "query_store", query)

    assert await MCPToolManager().get_mcp_meta() == (rows if code == 0 else [])
    query.assert_awaited_once_with(action="list_enabled_mcp_servers", payload={})

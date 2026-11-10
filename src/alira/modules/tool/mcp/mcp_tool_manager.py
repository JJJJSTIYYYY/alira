"""Identity-aware catalogs and routing for resident MCP runtimes."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from fastmcp import Client
from fastmcp.client.transports import (
    SSETransport,
    StdioTransport,
    StreamableHttpTransport,
)
from my_llm import ToolCall

from alira.kernel import auto_init
from alira.modules.database import query_store
from alira.modules.tool.base import BaseTool
from alira.modules.tool.mcp.base import McpMetaSchema
from alira.modules.tool.mcp.mcp_tool import MCPTool
from alira.modules.tool.mcp.mcp_tool_runtime import MCPToolRuntime
from alira.modules.tool.utils.manager import ToolManager
from alira.modules.tool.utils.request import ToolRequest
from alira.shared.models.identity import AliraIdentity, ConversationIdentity
from alira.utils import logger

IdentityKey = tuple[str, str, str]
UserKey = tuple[str, str]


@dataclass
class _MCPToolCatalog:
    """One identity's definitions and server runtimes; no client contexts."""

    closing: bool = False
    runtimes: dict[str, MCPToolRuntime] = field(default_factory=dict)
    tools: dict[str, list[MCPTool]] = field(default_factory=dict)
    configs: dict[str, McpMetaSchema] = field(default_factory=dict)
    locks: dict[str, asyncio.Lock] = field(default_factory=dict)


class MCPToolManager(ToolManager):
    """Route requests to runtimes isolated by user, platform and conversation."""

    def __init__(self) -> None:
        super().__init__()
        self._catalogs: dict[IdentityKey, _MCPToolCatalog] = {}
        self._closing_sessions: dict[IdentityKey, asyncio.Task[None]] = {}
        self._closing_users: dict[UserKey, asyncio.Task[None]] = {}

    @staticmethod
    def user_key(identity: AliraIdentity) -> UserKey:
        if not isinstance(identity, dict):
            raise TypeError("MCP requires an AliraIdentity dictionary.")
        values = tuple(identity.get(field) for field in ("id", "platform"))
        if any(not isinstance(value, str) or not value for value in values):
            raise ValueError("MCP identity requires non-empty id and platform.")
        return identity["id"], identity["platform"]

    @staticmethod
    def identity_key(identity: ConversationIdentity) -> IdentityKey:
        user_uid, platform = MCPToolManager.user_key(identity)
        conversation_uid = identity.get("conversation_uid")
        if not isinstance(conversation_uid, str) or not conversation_uid:
            raise ValueError(
                "MCP identity requires non-empty id, platform and conversation_uid."
            )
        return user_uid, platform, conversation_uid

    def owns(self, tool: BaseTool) -> bool:
        return isinstance(tool, MCPTool) and tool.manager is self

    def tools_for(self, identity: ConversationIdentity | None = None) -> list[MCPTool]:
        if identity is None:
            raise ValueError("An MCP manager requires a discovery identity.")
        catalog = self._catalogs.get(self.identity_key(identity))
        if catalog is None or catalog.closing:
            return []
        return [
            tool
            for mcp_id, tools in catalog.tools.items()
            if catalog.runtimes[mcp_id].is_running
            for tool in tools
        ]

    def _check_admission(
        self, key: IdentityKey, catalog: _MCPToolCatalog | None
    ) -> None:
        if not self.is_started:
            raise RuntimeError("MCPToolManager must be started before using MCP tools.")
        if (
            (catalog is not None and catalog.closing)
            or key in self._closing_sessions
            or key[:2] in self._closing_users
        ):
            raise RuntimeError("MCP session is closing.")

    def _get_catalog(self, key: IdentityKey) -> _MCPToolCatalog:
        catalog = self._catalogs.get(key)
        self._check_admission(key, catalog)
        if catalog is None:
            catalog = _MCPToolCatalog()
            self._catalogs[key] = catalog
        return catalog

    async def _dispatch(self, request: ToolRequest) -> None:
        tool = request.tool
        assert isinstance(tool, MCPTool)
        tool.validate_call(request.state, request.call)
        key = self.identity_key(request.state["target"])
        catalog = self._catalogs.get(key)
        self._check_admission(key, catalog)
        if catalog is None or not any(
            candidate is tool for candidate in catalog.tools.get(tool.mcp_id, [])
        ):
            raise RuntimeError(
                "MCP tool definition is stale; rediscover tools before calling it."
            )
        await catalog.runtimes[tool.mcp_id].submit(request)

    async def execute_tool(
        self, tool: MCPTool, state: dict[str, Any], call: ToolCall
    ) -> str:
        request = ToolRequest(tool, state, call, generation=self.generation)
        return await self.invoke(request)

    async def call_tool(
        self,
        identity: ConversationIdentity,
        mcp_id: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> str:
        """Publish a call and await the MCP result, including cancellation cleanup.

        Discover the server's tools first. Completion is the correlated result
        event, not admission of the request to a runtime inbox.
        """
        key = self.identity_key(identity)
        catalog = self._catalogs.get(key)
        self._check_admission(key, catalog)
        tool = next(
            (
                tool
                for tool in (catalog.tools.get(mcp_id, []) if catalog else [])
                if tool.name == tool_name
            ),
            None,
        )
        if tool is None:
            raise ValueError(
                f"MCP tool {tool_name!r} is not discovered for this server and identity."
            )
        if arguments is not None and not isinstance(arguments, dict):
            raise TypeError("MCP tool arguments must be a dictionary.")
        call = ToolCall(call_id=uuid4().hex, name=tool_name, arguments=arguments or {})
        return await self.execute_tool(tool, {"target": deepcopy(identity)}, call)

    async def get_mcp_meta(self) -> list[McpMetaSchema]:
        result = await query_store(
            action="list_enabled_mcp_servers",
            payload={},
        )
        if result["code"] != 0:
            logger.warning("Failed to fetch MCP meta: %s", result["messages"])
            return []
        return result["messages"]

    @staticmethod
    def _resolve_endpoint(mcp_meta: McpMetaSchema, config_key: str) -> str:
        value = mcp_meta.get("config", {}).get(config_key) or mcp_meta.get("endpoint")
        if not isinstance(value, str) or not value:
            raise ValueError(
                f"MCP {mcp_meta.get('mcp_name')!r} requires a non-empty {config_key}."
            )
        return value

    async def create_mcp_client(self, mcp_meta: McpMetaSchema) -> Client:
        """Build an unentered client for a runtime's owner task."""
        transport_name = mcp_meta["transport"]
        config = mcp_meta.get("config", {})
        transport_options = dict(config.get("transport_kwargs", {}))
        if transport_name == "stdio":
            transport_options.update(
                command=self._resolve_endpoint(mcp_meta, "command"),
                args=list(config.get("args", [])),
                env=config.get("env") or None,
                cwd=config.get("cwd"),
                keep_alive=True,
            )
            transport = StdioTransport(**transport_options)
        elif transport_name in {"http", "streamable_http", "sse"}:
            transport_options.update(
                url=self._resolve_endpoint(mcp_meta, "url"),
                headers=dict(config.get("headers", {})),
            )
            for option in ("auth", "sse_read_timeout", "verify"):
                if option in config:
                    transport_options[option] = config[option]
            transport_type = (
                SSETransport if transport_name == "sse" else StreamableHttpTransport
            )
            transport = transport_type(**transport_options)
        else:
            raise ValueError(f"Unsupported MCP transport {transport_name!r}.")

        client_options: dict[str, Any] = dict(config.get("session_kwargs", {}))
        client_options.update(config.get("client_kwargs", {}))
        for option in ("auto_initialize", "init_timeout", "timeout"):
            if option in config:
                client_options[option] = config[option]
        return Client(transport, **client_options)

    async def get_mcp_tools(
        self, mcp_meta: McpMetaSchema, identity: ConversationIdentity
    ) -> list[MCPTool]:
        """Discover through a runtime; only its task can enter or exit the client."""
        meta = deepcopy(mcp_meta)
        key = self.identity_key(identity)
        catalog = self._get_catalog(key)
        mcp_id = meta["mcp_id"]
        catalog.configs[mcp_id] = meta
        catalog.tools.pop(mcp_id, None)
        lock = catalog.locks.setdefault(mcp_id, asyncio.Lock())
        async with lock:
            self._check_admission(key, catalog)
            if catalog.configs[mcp_id] != meta:
                raise RuntimeError(
                    "MCP discovery was superseded by a configuration refresh."
                )
            runtime = catalog.runtimes.get(mcp_id)
            if runtime is not None and (runtime.meta != meta or not runtime.is_running):
                await runtime.stop()
                catalog.runtimes.pop(mcp_id)
                runtime = None
            self._check_admission(key, catalog)
            if runtime is None:
                runtime = MCPToolRuntime(meta, self.create_mcp_client)
                catalog.runtimes[mcp_id] = runtime
            await runtime.start()
            self._check_admission(key, catalog)
            definitions = await runtime.discover()
            self._check_admission(key, catalog)
            if catalog.configs[mcp_id] != meta:
                raise RuntimeError(
                    "MCP discovery was superseded by a configuration refresh."
                )
            tools = [
                MCPTool(definition, self, meta, identity) for definition in definitions
            ]
            catalog.tools[mcp_id] = tools
            return tools

    async def load_all_mcp_tools(self, identity: ConversationIdentity) -> list[MCPTool]:
        key = self.identity_key(identity)
        self._check_admission(key, self._catalogs.get(key))
        tools: list[MCPTool] = []
        for meta in await self.get_mcp_meta():
            tools.extend(await self.get_mcp_tools(meta, identity))
        return tools

    async def _close_catalog(self, catalog: _MCPToolCatalog) -> None:
        errors: list[Exception] = []
        for mcp_id in list(catalog.runtimes):
            # A discovery already holding this lock must observe the closed gate
            # and finish before a stop can retire its runtime.
            async with catalog.locks[mcp_id]:
                try:
                    await catalog.runtimes[mcp_id].stop()
                except Exception as exc:  # noqa: BLE001
                    # Close remaining runtimes before reporting any client failure.
                    errors.append(exc)
                else:
                    catalog.runtimes.pop(mcp_id, None)
                    catalog.tools.pop(mcp_id, None)
                    catalog.configs.pop(mcp_id, None)
        if errors:
            raise ExceptionGroup("Failed to close MCP runtimes", errors)

    def _begin_close_session(self, key: IdentityKey) -> asyncio.Task[None] | None:
        task = self._closing_sessions.get(key)
        if task is not None:
            return task
        session = self._catalogs.get(key)
        if session is None:
            return None
        session.closing = True

        async def close() -> None:
            try:
                await self._close_catalog(session)
                self._catalogs.pop(key, None)
            finally:
                self._closing_sessions.pop(key, None)

        task = asyncio.create_task(close())
        self._closing_sessions[key] = task
        # Observe background failures even if the caller cancels its wait.
        task.add_done_callback(self._observe_cleanup)
        return task

    @staticmethod
    def _observe_cleanup(task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()

    async def close_session(self, identity: ConversationIdentity) -> None:
        """Reject new calls and stop this conversation's runtimes through events."""
        task = self._begin_close_session(self.identity_key(identity))
        if task is not None:
            await asyncio.shield(task)

    def _begin_close_user(self, user_key: UserKey) -> asyncio.Task[None]:
        task = self._closing_users.get(user_key)
        if task is not None and not task.done():
            return task

        async def close() -> None:
            keys = set(self._catalogs) | set(self._closing_sessions)
            tasks = [
                task
                for key in keys
                if key[:2] == user_key
                if (task := self._begin_close_session(key)) is not None
            ]
            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                errors = [result for result in results if isinstance(result, Exception)]
                if errors:
                    raise ExceptionGroup("Failed to close MCP user sessions", errors)
            self._closing_users.pop(user_key, None)

        task = asyncio.create_task(close())
        self._closing_users[user_key] = task
        task.add_done_callback(self._observe_cleanup)
        return task

    async def close_user(self, identity: AliraIdentity) -> None:
        """Close every conversation for the same user and platform."""
        await asyncio.shield(self._begin_close_user(self.user_key(identity)))

    async def _stop(self) -> None:
        """Close resident clients after the event service has drained requests."""
        tasks = [self._begin_close_user(key) for key in list(self._closing_users)]
        keys = set(self._catalogs) | set(self._closing_sessions)
        tasks.extend(
            task for key in keys if (task := self._begin_close_session(key)) is not None
        )
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            errors = [result for result in results if isinstance(result, Exception)]
            if errors:
                raise ExceptionGroup("Failed to stop MCP clients", errors)


mcp_mgr = MCPToolManager()
auto_init.register(mcp_mgr, group="mcp")

__all__ = ["MCPToolManager", "mcp_mgr"]

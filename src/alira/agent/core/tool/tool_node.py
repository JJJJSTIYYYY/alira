"""Graph orchestration for independently running tool managers."""

import asyncio
from typing import Any

from apixis.core.event import start_core
from apixis.core.graph import BaseNode, Command

from alira.agent.core.tool.base import BaseTool, ToolFunction
from alira.agent.core.tool.function import FunctionToolManager, Tool
from alira.agent.core.tool.utils.manager import ToolManager
from alira.agent.core.tool.utils.request import ToolRequest
from alira.agent.core.utils.message import AliraAiMessage, AliraToolMessage, ToolCall
from alira.common.type import AliraIdentity


class ToolNode(BaseNode):
    """Publish tool calls, collect their outcomes, and route one graph command.

    Managers own execution and resources. This node only selects tools exposed
    to its graph; cancelling a batch cancels its calls without stopping managers.
    """

    def __init__(
        self,
        tool_set: BaseTool
        | ToolFunction
        | ToolManager
        | list[BaseTool | ToolFunction | ToolManager],
        name: str = "tools",
        messages_key: str = "messages",
        timeout: float | None = None,
        *,
        bind_llm_node: str | BaseNode | None = None,
        identity: AliraIdentity | None = None,
    ) -> None:
        if not isinstance(name, str) or not name:
            raise ValueError("A tool node requires a name.")
        if not isinstance(messages_key, str) or not messages_key:
            raise ValueError("A tool node requires a message key.")
        self.name = name
        self.messages_key = messages_key
        self.timeout = timeout
        self.bind_llm_node(bind_llm_node)
        self._tools_by_name: dict[str, BaseTool] = {}
        self._managers_by_name: dict[str, ToolManager] = {}
        local_tools: list[Tool] = []
        candidates = tool_set if isinstance(tool_set, list) else [tool_set]
        for candidate in candidates:
            if isinstance(candidate, ToolManager):
                for registered_tool in candidate.tools_for(identity):
                    self._register(registered_tool, candidate)
            elif isinstance(candidate, BaseTool):
                manager = getattr(candidate, "manager", None)
                self._register(candidate, manager)
                if manager is None:
                    if not isinstance(candidate, Tool):
                        raise ValueError("A tool requires an owning ToolManager.")
                    local_tools.append(candidate)
            elif callable(candidate):
                wrapped = Tool(candidate)
                self._register(wrapped, None)
                local_tools.append(wrapped)
            else:
                raise ValueError(
                    "A tool node requires Tool objects or callable functions or managers."
                )
        if local_tools:
            manager = FunctionToolManager(local_tools)
            for registered_tool in local_tools:
                self._managers_by_name[registered_tool.name] = manager

    def _register(self, registered_tool: BaseTool, manager: ToolManager | None) -> None:
        if registered_tool.name in self._tools_by_name:
            raise ValueError(
                f"Tool {registered_tool.name!r} is already registered in node {self.name!r}."
            )
        self._tools_by_name[registered_tool.name] = registered_tool
        if manager is not None:
            self._managers_by_name[registered_tool.name] = manager

    @property
    def tool_set(self) -> list[BaseTool]:
        return list(self._tools_by_name.values())

    @property
    def managers(self) -> list[ToolManager]:
        """Return the independent services used by this node."""
        return list(dict.fromkeys(self._managers_by_name.values()))

    def bind_llm_node(self, llm_node: str | BaseNode | None) -> "ToolNode":
        if isinstance(llm_node, BaseNode):
            llm_node = llm_node.name
        if llm_node is not None and (not isinstance(llm_node, str) or not llm_node):
            raise ValueError("An LLM binding requires a non-empty node name or None.")
        self._llm_node_name = llm_node
        return self

    def get_schemas(self, filter_names: set[str] | None = None) -> list[dict[str, Any]]:
        return [
            registered_tool.get_schema()
            for registered_tool in self.tool_set
            if filter_names is None or registered_tool.name in filter_names
        ]

    def _get_tool_calls(self, state: dict[str, Any]) -> list[ToolCall]:
        if not isinstance(state, dict):
            raise TypeError("state must be a dictionary.")
        messages = state.get(self.messages_key)
        if messages is None:
            return []
        if not isinstance(messages, list):
            raise ValueError(
                f"`{self.messages_key}` must be a message list, got {type(messages).__name__}."
            )
        if not messages or not isinstance(messages[-1], AliraAiMessage):
            return []
        tool_calls = messages[-1].tool_calls
        if tool_calls is None:
            return []
        if not isinstance(tool_calls, list):
            raise TypeError(
                "AliraAiMessage.tool_calls must be a list of valid ToolCall objects."
            )
        for call in tool_calls:
            BaseTool._validate_tool_call(call)
            if call["function"]["name"] not in self._tools_by_name:
                raise ValueError(
                    f"Tool {call['function']['name']!r} is not registered in node {self.name!r}."
                )
        return tool_calls

    async def _request_call(self, state: dict[str, Any], call: ToolCall) -> ToolRequest:
        name = call["function"]["name"]
        manager = self._managers_by_name[name]
        request = ToolRequest(
            self._tools_by_name[name],
            state,
            call,
            generation=manager.generation,
        )
        await manager.invoke(request)
        return request

    async def execute(self, state: dict[str, Any]) -> Command | list[Command]:
        calls = self._get_tool_calls(state)
        if not calls:
            return []
        await start_core()
        for manager in self.managers:
            await manager.start()
        tasks = [asyncio.create_task(self._request_call(state, call)) for call in calls]
        try:
            results = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        messages = [
            AliraToolMessage(
                content=str(result.result),
                name=call["function"]["name"],
                tool_call_id=call["id"],
                metadata={"duration": result.duration},
            )
            for call, result in zip(calls, results, strict=True)
        ]
        return Command(update={self.messages_key: messages}, goto=self._llm_node_name)


__all__ = ["ToolNode"]

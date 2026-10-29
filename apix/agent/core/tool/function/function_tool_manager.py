"""Resident manager for Python function tools."""

from apix.agent.core.tool.base import ToolFunction
from apix.agent.core.tool.function.tool import Tool
from apix.agent.core.tool.utils.manager import ToolManager
from apix.common.type import ApixIdentity


class FunctionToolManager(ToolManager):
    """Register function tools and execute their event requests."""

    def __init__(
        self, tool_set: Tool | ToolFunction | list[Tool | ToolFunction]
    ) -> None:
        candidates = tool_set if isinstance(tool_set, list) else [tool_set]
        self._tools_by_name: dict[str, Tool] = {}
        for candidate in candidates:
            if isinstance(candidate, Tool):
                wrapped = candidate
            elif callable(candidate):
                wrapped = Tool(candidate)
            else:
                raise ValueError(
                    "Function tools require Tool objects or callable functions."
                )
            if wrapped.name in self._tools_by_name:
                raise ValueError(f"Tool {wrapped.name!r} is already registered.")
            self._tools_by_name[wrapped.name] = wrapped
        super().__init__()

    @property
    def tool_set(self) -> list[Tool]:
        return list(self._tools_by_name.values())

    def tools_for(self, identity: ApixIdentity | None = None) -> list[Tool]:
        return self.tool_set

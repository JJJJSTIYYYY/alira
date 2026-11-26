"""Shared function schemas and tool execution results."""

from dataclasses import dataclass

from my_llm import FunctionTool, ToolCall
from alira.shared.type import ModuleNotLoaded
from alira.modules.agent.src.shared.model.message import AliraToolMessage


@dataclass(slots=True)
class ToolExecution:
    """Pair requested calls with results or an unavailable tool module marker.

    Args:
        tool_calls: Calls requested by the model.
        result: Completed tool messages, including an empty list when execution
            produced none. Defaults to an immutable marker for the tool module.
    """

    tool_calls: list[ToolCall]
    result: ModuleNotLoaded | list[AliraToolMessage] = ModuleNotLoaded("tool")


__all__ = [
    "FunctionTool",
    "ToolCall",
    "AliraToolMessage",
    "ToolExecution",
]

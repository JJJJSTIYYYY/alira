"""Tool execution distinguishes unavailable execution from completed results."""

import pytest
from my_llm import TextContent, ToolCall

from alira.modules.agent.src.shared.model.message import AliraToolMessage
from alira.modules.agent.src.shared.model.tools import ToolExecution
from alira.shared import ModuleNotLoaded


@pytest.mark.parametrize("calls", [[], [ToolCall("call-1", "weather", {})]])
def test_new_execution_reports_tool_module_unavailable(calls):
    execution = ToolExecution(tool_calls=calls)

    assert execution.tool_calls == calls
    assert isinstance(execution.result, ModuleNotLoaded)
    assert execution.result.module_name == "tool"
    assert not execution.result


@pytest.mark.parametrize("results", [
    [],
    [AliraToolMessage(call_id="call-1", content=[TextContent("sunny")])],
])
def test_completed_execution_keeps_results_distinct_from_unavailable(results):
    execution = ToolExecution(
        tool_calls=[ToolCall("call-1", "weather", {})], result=results
    )

    assert isinstance(execution.result, list)
    assert execution.result == results
    assert not isinstance(execution.result, ModuleNotLoaded)

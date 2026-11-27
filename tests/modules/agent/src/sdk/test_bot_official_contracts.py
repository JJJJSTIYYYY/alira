"""Library middleware models define the application's inference boundary."""

from my_llm import FunctionTool, Message, TextContent, ToolCall

from alira.modules.agent.src.shared.model.message import (
    AliraAssistantMessage,
    AliraSystemMessage,
    AliraToolMessage,
    AliraUserMessage,
)
from tests.modules.agent.src.sdk.test_bot import weather


def test_messages_and_tools_use_library_middleware_contracts():
    messages = [
        AliraSystemMessage(content=[TextContent("rules")]),
        AliraUserMessage(content=[TextContent("look")]),
        AliraAssistantMessage(content=[ToolCall("c1", "weather", {"city": "Tokyo"})]),
        AliraToolMessage(call_id="c1", content=[TextContent("")]),
    ]
    assert all(isinstance(message, Message) for message in messages)
    assert isinstance(weather.get_schema(), FunctionTool)
